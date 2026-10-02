import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from opsgraph.api.app import app, runtime
from opsgraph.config import get_settings

client = TestClient(app)


def auth() -> dict[str, str]:
    return {"X-OpsGraph-Key": get_settings().api_key}


def test_health_and_bootstrap_are_public():
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["pragma"] == "no-cache"
    assert health.json()["ok"] is True
    assert health.json()["provider"]["status"] in {"ready", "misconfigured", "unavailable"}
    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["trust"]["egress"] is False
    assert bootstrap["trust"]["sample_model_calls"] == 0
    assert client.get("/").status_code == 200
    assert client.get("/assets/static/app.js").status_code == 200


def test_authenticated_api_responses_prohibit_caching():
    response = client.get("/api/sources", headers=auth())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_product_endpoints_require_workspace_key():
    assert client.get("/api/sources").status_code == 401
    assert client.get("/api/playbooks").status_code == 401
    response = client.post("/api/investigations/sample", json={"question": "what happened?"})
    assert response.status_code == 401
    schema_response = client.post("/api/schema/inspect", json={"ddl": "CREATE TABLE x(id int)"})
    assert schema_response.status_code == 401
    assert client.post("/api/query/validate", json={"sql": "SELECT id FROM x"}).status_code == 401
    assert client.get("/api/audit").status_code == 401


def test_sample_endpoint_is_retired_and_sources_contain_no_synthetic_records():
    response = client.post(
        "/api/investigations/sample",
        headers=auth(),
        json={"question": "Investigate webhook failures after the deployment"},
    )
    assert response.status_code == 410
    assert "real source and model" in response.json()["detail"]
    assert all(
        source["kind"] != "synthetic"
        for source in client.get("/api/sources", headers=auth()).json()
    )


def test_schema_inspection_parses_structure_but_rejects_data_statements():
    safe = client.post(
        "/api/schema/inspect",
        headers=auth(),
        json={"ddl": "CREATE TABLE public.jobs (id bigint PRIMARY KEY, status text);"},
    )
    assert safe.status_code == 200
    assert safe.json()["tables"][0]["table_name"] == "jobs"

    unsafe = client.post(
        "/api/schema/inspect",
        headers=auth(),
        json={"ddl": "CREATE TABLE jobs (id bigint); INSERT INTO jobs VALUES (1);"},
    )
    assert unsafe.status_code == 422


def test_query_preview_is_select_only_and_never_executes():
    safe = client.post(
        "/api/query/validate",
        headers=auth(),
        json={"sql": "SELECT id, status FROM public.jobs"},
    )
    assert safe.status_code == 200
    assert safe.json()["executed"] is False
    assert safe.json()["referenced_tables"] == ["public.jobs"]

    unsafe = client.post(
        "/api/query/validate",
        headers=auth(),
        json={"sql": "DELETE FROM public.jobs WHERE id = 1"},
    )
    assert unsafe.status_code == 422


def test_audit_chain_verifies_after_protected_operations():
    response = client.get("/api/audit", headers=auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["verification"]["valid"] in {True, None}
    assert payload["events"]


def test_source_metadata_uses_secret_reference_and_fails_closed_without_secret():
    response = client.post(
        "/api/sources",
        headers=auth(),
        json={
            "id": "test-readonly",
            "name": "Test read-only source",
            "secret_ref": "OPSGRAPH_SOURCE_DSN",
            "allowed_schemas": ["public"],
            "allowed_tables": ["public.jobs"],
            "evidence_bindings": [
                {"evidence_type": "job_status", "source_tables": ["public.jobs"]}
            ],
            "allow_external_egress": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["secret_ref"] == "OPSGRAPH_SOURCE_DSN"  # noqa: S105
    assert response.json()["allowed_tables"] == ["public.jobs"]
    assert response.json()["evidence_bindings"][0]["evidence_type"] == "job_status"
    assert "postgresql://" not in response.text

    inspect = client.post("/api/sources/test-readonly/inspect", headers=auth())
    assert inspect.status_code == 409
    assert "OPSGRAPH_SOURCE_DSN" in inspect.json()["detail"]

    invalid = client.post(
        "/api/sources",
        headers=auth(),
        json={
            "id": "invalid-scope",
            "name": "Invalid scope",
            "secret_ref": "OPSGRAPH_SOURCE_DSN",
            "allowed_schemas": ["public; DROP SCHEMA public"],
        },
    )
    assert invalid.status_code == 422


def test_source_audit_failure_does_not_save_configuration(monkeypatch):
    source_id = "audit-failure-source"

    def unavailable(*_, **__):
        raise OSError("audit unavailable")

    monkeypatch.setattr(runtime.audit, "append_in_transaction", unavailable)
    response = client.post(
        "/api/sources",
        headers=auth(),
        json={
            "id": source_id,
            "name": "Audit failure source",
            "secret_ref": "OPSGRAPH_SOURCE_DSN",
            "allowed_schemas": ["public"],
            "allowed_tables": ["public.jobs"],
        },
    )

    assert response.status_code == 503
    with pytest.raises(KeyError):
        runtime.store.get(
            workspace_id=runtime.settings.workspace_id,
            record_id=f"source:{source_id}",
        )


def test_source_rejects_unqualified_table_scope():
    response = client.post(
        "/api/sources",
        headers=auth(),
        json={
            "id": "bad-table-scope",
            "name": "Bad table scope",
            "secret_ref": "OPSGRAPH_INVALID_DSN",
            "allowed_tables": ["jobs"],
        },
    )
    assert response.status_code == 422


def test_source_rejects_duplicate_evidence_bindings() -> None:
    response = client.post(
        "/api/sources",
        headers=auth(),
        json={
            "id": "duplicate-evidence",
            "name": "Duplicate evidence source",
            "secret_ref": "OPSGRAPH_SOURCE_DSN",
            "allowed_tables": ["public.jobs"],
            "evidence_bindings": [
                {"evidence_type": "job_status", "source_tables": ["public.jobs"]},
                {"evidence_type": "job_status", "source_tables": ["public.jobs"]},
            ],
        },
    )
    assert response.status_code == 422


def test_source_rejects_unapproved_secret_reference() -> None:
    response = client.post(
        "/api/sources",
        headers=auth(),
        json={
            "id": "unapproved-secret",
            "name": "Unapproved secret reference",
            "secret_ref": "OPSGRAPH_UNAPPROVED_DSN",
            "allowed_tables": ["public.jobs"],
        },
    )
    assert response.status_code == 422
    assert "not approved" in response.json()["detail"]


def test_policy_view_is_authenticated_and_server_derived():
    assert client.get("/api/policies/current").status_code == 401
    response = client.get("/api/policies/current", headers=auth())
    assert response.status_code == 200
    assert response.json()["default"] == "deny"


def test_skill_draft_supports_bounded_per_tool_customization():
    audit_before = len(runtime.audit.entries)
    response = client.post(
        "/api/skills/drafts",
        headers=auth(),
        json={
            "id": "custom-test",
            "version": "0.1.0",
            "name": "Custom test",
            "origin": "custom",
            "purpose": "Investigate a bounded custom operational question.",
            "tools": [
                {"tool": "core.schema.inspect", "enabled": True, "settings": {}},
                {
                    "tool": "core.sql.select",
                    "enabled": True,
                    "settings": {
                        "max_rows": 25,
                        "timeout_ms": 2_000,
                        "allowed_schemas": ["public"],
                    },
                },
            ],
        },
    )
    assert response.status_code == 200
    sql_tool = next(tool for tool in response.json()["tools"] if tool["tool"] == "core.sql.select")
    assert sql_tool["settings"]["max_rows"] == 25

    published = client.post("/api/skills/custom-test/publish", headers=auth())
    assert published.status_code == 200
    current = runtime.store.get(
        workspace_id=runtime.settings.workspace_id,
        record_id="skill-current:custom-test",
    )
    assert current.value == {
        "record_type": "skill_current",
        "skill_id": "custom-test",
        "version": "0.1.0",
        "published_record_id": "skill-published:custom-test:0.1.0",
    }
    catalog = client.get("/api/skills", headers=auth()).json()
    assert any(skill["id"] == "custom-test" for skill in catalog)
    events = runtime.audit.entries[audit_before:]
    assert [(event.outcome, event.details["operation"]) for event in events] == [
        ("allowed", "save_draft"),
        ("allowed", "publish"),
    ]


def test_skill_draft_audit_failure_does_not_change_runtime_or_storage(monkeypatch):
    skill_id = "audit-failure-draft"

    def unavailable(*_, **__):
        raise OSError("audit unavailable")

    audit_before = len(runtime.audit.entries)
    monkeypatch.setattr(runtime.audit, "append_in_transaction", unavailable)
    response = client.post(
        "/api/skills/drafts",
        headers=auth(),
        json={
            "id": skill_id,
            "version": "1.0.0",
            "name": "Audit failure draft",
            "purpose": "Verify that failed audit persistence leaves no draft.",
            "tools": [
                {"tool": "core.schema.inspect"},
                {"tool": "core.sql.select"},
            ],
        },
    )

    assert response.status_code == 503
    with pytest.raises(KeyError):
        runtime.skills.get_draft(skill_id)
    with pytest.raises(KeyError):
        runtime.store.get(
            workspace_id=runtime.settings.workspace_id,
            record_id=f"skill-draft:{skill_id}",
        )
    assert len(runtime.audit.entries) == audit_before


def test_skill_publish_audit_failure_keeps_the_draft_unpublished(monkeypatch):
    skill_id = "audit-failure-publish"
    saved = client.post(
        "/api/skills/drafts",
        headers=auth(),
        json={
            "id": skill_id,
            "version": "1.0.0",
            "name": "Audit failure publish",
            "purpose": "Verify that failed audit persistence cannot publish a skill.",
            "tools": [
                {"tool": "core.schema.inspect"},
                {"tool": "core.sql.select"},
            ],
        },
    )
    assert saved.status_code == 200

    def unavailable(*_, **__):
        raise OSError("audit unavailable")

    audit_before = len(runtime.audit.entries)
    monkeypatch.setattr(runtime.audit, "append_in_transaction", unavailable)
    response = client.post(f"/api/skills/{skill_id}/publish", headers=auth())

    assert response.status_code == 503
    assert runtime.skills.get_draft(skill_id).version == "1.0.0"
    with pytest.raises(KeyError):
        runtime.skills.get_published(skill_id)
    stored = runtime.store.get(
        workspace_id=runtime.settings.workspace_id,
        record_id=f"skill-draft:{skill_id}",
    )
    assert stored.value["definition"]["version"] == "1.0.0"
    with pytest.raises(KeyError):
        runtime.store.get(
            workspace_id=runtime.settings.workspace_id,
            record_id=f"skill-published:{skill_id}:1.0.0",
        )
    assert len(runtime.audit.entries) == audit_before


def test_skill_publish_storage_failure_keeps_durable_and_runtime_draft(monkeypatch):
    skill_id = "storage-failure-publish"
    definition = {
        "id": skill_id,
        "version": "1.0.0",
        "name": "Storage failure publish",
        "purpose": "Verify a failed atomic commit never changes runtime publication state.",
        "tools": [{"tool": "core.schema.inspect"}, {"tool": "core.sql.select"}],
    }
    assert client.post("/api/skills/drafts", headers=auth(), json=definition).status_code == 200

    def unavailable(*_, **__):
        raise sqlite3.OperationalError("private storage detail")

    audit_before = len(runtime.audit.entries)
    monkeypatch.setattr(runtime.store, "replace_if_unchanged_in_transaction", unavailable)
    response = client.post(f"/api/skills/{skill_id}/publish", headers=auth())

    assert response.status_code == 503
    assert "private storage detail" not in response.text
    assert runtime.skills.get_draft(skill_id).version == "1.0.0"
    with pytest.raises(KeyError):
        runtime.skills.get_published(skill_id)
    assert (
        runtime.store.get(
            workspace_id=runtime.settings.workspace_id,
            record_id=f"skill-draft:{skill_id}",
        ).value["definition"]["version"]
        == "1.0.0"
    )
    assert len(runtime.audit.entries) == audit_before


def test_skill_publish_serializes_concurrent_draft_save(monkeypatch):
    skill_id = "concurrent-publish"
    original = {
        "id": skill_id,
        "version": "1.0.0",
        "name": "Concurrent publish",
        "purpose": "Verify the audited version is exactly the version committed.",
        "tools": [{"tool": "core.schema.inspect"}, {"tool": "core.sql.select"}],
    }
    replacement = {**original, "version": "1.0.1", "name": "Concurrent replacement"}
    assert client.post("/api/skills/drafts", headers=auth(), json=original).status_code == 200
    entered, release = threading.Event(), threading.Event()
    real_append = runtime.audit.append_in_transaction

    def blocking_append(connection, **entry):
        if entry.get("details", {}).get("operation") == "publish":
            entered.set()
            assert release.wait(5)
        return real_append(connection, **entry)

    monkeypatch.setattr(runtime.audit, "append_in_transaction", blocking_append)
    results = {}
    publishing = threading.Thread(
        target=lambda: results.setdefault(
            "publish", client.post(f"/api/skills/{skill_id}/publish", headers=auth())
        )
    )
    saving = threading.Thread(
        target=lambda: results.setdefault(
            "save", client.post("/api/skills/drafts", headers=auth(), json=replacement)
        )
    )
    publishing.start()
    assert entered.wait(5)
    saving.start()
    time.sleep(0.05)
    assert saving.is_alive()
    release.set()
    publishing.join(timeout=5)
    saving.join(timeout=5)

    assert results["publish"].status_code == 200
    assert results["publish"].json()["version"] == "1.0.0"
    assert results["save"].status_code == 200
    assert runtime.skills.get_published(skill_id, "1.0.0").version == "1.0.0"
    assert runtime.skills.get_draft(skill_id).version == "1.0.1"
