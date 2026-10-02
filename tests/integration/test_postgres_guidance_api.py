from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from opsgraph.config import get_settings
from opsgraph.domain import Obligation
from opsgraph.runtime import build_runtime


@pytest.fixture
def guidance_api(tmp_path, monkeypatch):
    module = import_module("opsgraph.api.app")
    runtime = build_runtime(get_settings().model_copy(update={"state_path": tmp_path / "state.db"}))
    monkeypatch.setattr(module, "runtime", runtime)
    return module, TestClient(module.app), runtime, {"X-OpsGraph-Key": runtime.settings.api_key}


def test_role_guide_requires_authentication_and_source_management_authorization(
    guidance_api, monkeypatch
):
    module, client, _, headers = guidance_api
    body = {"role_name": "opsgraph_reader", "tables": ["public.jobs"]}
    assert client.post("/api/postgres/role-guide", json=body).status_code == 401

    calls = []

    def authorize(principal, action, resource):
        calls.append((principal.subject, action, resource))
        return Obligation(allowed_schemas=("public",))

    monkeypatch.setattr(module, "authorize", authorize)
    response = client.post("/api/postgres/role-guide", headers=headers, json=body)

    assert response.status_code == 200
    assert calls == [("local-operator", "core.source.manage", "postgres-role-guide")]


def test_role_guide_intersects_exact_policy_scope_without_executing(guidance_api, monkeypatch):
    module, client, runtime, headers = guidance_api
    monkeypatch.setattr(
        module,
        "authorize",
        lambda *_: Obligation(
            timeout_ms=1_500,
            allowed_schemas=("public", "private"),
            allowed_tables=("public.jobs",),
        ),
    )
    response = client.post(
        "/api/postgres/role-guide",
        headers=headers,
        json={
            "role_name": "opsgraph_reader",
            "database": "operations",
            "tables": ["public.jobs", "public.events", "private.jobs"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["executed"] is False
    assert payload["allowed_tables"] == ["public.jobs"]
    assert payload["excluded_tables"] == ["public.events", "private.jobs"]
    assert 'GRANT SELECT ON TABLE "public"."jobs"' in payload["sql"]
    assert "events" not in payload["sql"] and "private" not in payload["sql"]
    assert 'GRANT CONNECT ON DATABASE "operations"' in payload["sql"]
    event = runtime.audit.entries[-1]
    assert event.action == "core.source.manage" and event.resource == "postgres-role-guide"
    assert event.details == {
        "executed": False,
        "requested_table_count": 3,
        "granted_table_count": 1,
        "excluded_table_count": 2,
        "database_connect_included": True,
        "statement_timeout_ms": 1_500,
    }
    serialized_event = event.model_dump_json()
    assert "opsgraph_reader" not in serialized_event
    assert "GRANT" not in serialized_event


def test_role_guide_rejects_a_scope_without_policy_overlap(guidance_api, monkeypatch):
    module, client, runtime, headers = guidance_api
    monkeypatch.setattr(
        module,
        "authorize",
        lambda *_: Obligation(
            allowed_schemas=("public",),
            allowed_tables=("public.approved",),
        ),
    )
    response = client.post(
        "/api/postgres/role-guide",
        headers=headers,
        json={"role_name": "opsgraph_reader", "tables": ["public.other"]},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Requested tables are outside the current deployment policy."
    )
    assert runtime.audit.entries[-1].details == {
        "reason": "no_policy_overlap",
        "executed": False,
        "requested_table_count": 1,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"role_name": "reader;drop_role", "tables": ["public.jobs"]},
        {"role_name": "opsgraph_reader", "tables": ["public.jobs;drop_table"]},
        {
            "role_name": "opsgraph_reader",
            "database": "operations;drop_database",
            "tables": ["public.jobs"],
        },
        {"role_name": "OpsGraphReader", "tables": ["public.jobs"]},
    ],
)
def test_role_guide_api_rejects_identifier_injection(guidance_api, body):
    _, client, runtime, headers = guidance_api
    before = len(runtime.audit.entries)

    response = client.post("/api/postgres/role-guide", headers=headers, json=body)

    assert response.status_code == 422
    assert len(runtime.audit.entries) == before


def test_hosting_catalog_covers_all_routes_without_network(guidance_api, monkeypatch):
    _, client, _, headers = guidance_api
    monkeypatch.setattr(
        "psycopg.connect", lambda *_args, **_kwargs: pytest.fail("network attempted")
    )
    assert client.get("/api/postgres/hosting-guides").status_code == 401
    response = client.get("/api/postgres/hosting-guides", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    profiles = response.json()["profiles"]
    assert {item["id"] for item in profiles} == {
        "local",
        "self_hosted",
        "supabase",
        "neon",
        "aws_rds",
        "google_cloud_sql",
        "azure",
        "digitalocean",
    }
    assert all(
        item["steps"] and item["checks"] and "Guidance available" in item["validation"]
        for item in profiles
    )


def test_source_hosting_is_validated_and_retained_as_guidance(guidance_api):
    _, client, _, headers = guidance_api
    body = {
        "id": "hosted-test",
        "name": "Hosted test",
        "secret_ref": "OPSGRAPH_SOURCE_DSN",
        "allowed_schemas": ["public"],
        "allowed_tables": ["public.jobs"],
    }
    for profile in ["supabase", "neon", "aws_rds", "google_cloud_sql", "azure", "digitalocean"]:
        response = client.post(
            "/api/sources", headers=headers, json={**body, "hosting_profile": profile}
        )
        assert response.status_code == 200
        assert response.json()["hosting_profile"] == profile
        assert response.json()["status"] != "ready"
    assert (
        client.post(
            "/api/sources", headers=headers, json={**body, "hosting_profile": "unknown"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "code",
    [
        "authentication_failed",
        "tls_verification",
        "schema_too_large",
        "scope_metadata_incomplete",
        "network_unavailable",
    ],
)
def test_inspection_returns_fixed_diagnostics_and_audit_without_driver_content(
    guidance_api, monkeypatch, code
):
    from opsgraph.brokers import ConnectorUnavailable

    module, client, runtime, headers = guidance_api
    monkeypatch.setenv("OPSGRAPH_SOURCE_DSN", "private-test-connection")

    def unavailable(*_args, **_kwargs):
        raise ConnectorUnavailable("private-driver-marker", diagnostic_code=code)

    monkeypatch.setattr(module, "PsycopgReadOnlyExecutor", unavailable)
    body = {
        "id": "diagnostic-source",
        "name": "Test",
        "secret_ref": "OPSGRAPH_SOURCE_DSN",
        "allowed_schemas": ["public"],
        "allowed_tables": ["public.jobs"],
    }
    assert client.post("/api/sources", headers=headers, json=body).status_code == 200
    response = client.post("/api/sources/diagnostic-source/inspect", headers=headers)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert response.json()["diagnostic"]["code"] == code
    assert response.json()["diagnostic"]["steps"]
    assert "private-driver-marker" not in response.text
    assert "private-test-connection" not in response.text
    assert runtime.audit.entries[-1].details == {"reason": code}
