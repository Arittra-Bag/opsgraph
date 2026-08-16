from fastapi.testclient import TestClient

from opsgraph.api.app import app
from opsgraph.config import get_settings

client = TestClient(app)


def auth() -> dict[str, str]:
    return {"X-OpsGraph-Key": get_settings().api_key}


def test_health_and_bootstrap_are_public():
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["provider"]["status"] in {"ready", "misconfigured", "unavailable"}
    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["trust"]["egress"] is False
    assert bootstrap["trust"]["sample_model_calls"] == 0
    assert client.get("/").status_code == 200
    assert client.get("/assets/static/app.js").status_code == 200


def test_product_endpoints_require_workspace_key():
    assert client.get("/api/sources").status_code == 401
    assert client.get("/api/playbooks").status_code == 401
    response = client.post("/api/investigations/sample", json={"question": "what happened?"})
    assert response.status_code == 401
    schema_response = client.post("/api/schema/inspect", json={"ddl": "CREATE TABLE x(id int)"})
    assert schema_response.status_code == 401
    assert client.post("/api/query/validate", json={"sql": "SELECT id FROM x"}).status_code == 401
    assert client.get("/api/audit").status_code == 401


def test_sample_investigation_is_cited_and_classified():
    response = client.post(
        "/api/investigations/sample",
        headers=auth(),
        json={"question": "Investigate webhook failures after the deployment"},
    )
    assert response.status_code == 200
    result = response.json()
    evidence_ids = {item["id"] for item in result["evidence"]}
    assert {"supported", "possible", "contradictory"} <= {
        finding["classification"] for finding in result["findings"]
    }
    assert all(set(finding["evidence_ids"]) <= evidence_ids for finding in result["findings"])
    assert result["limitations"]


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
    catalog = client.get("/api/skills", headers=auth()).json()
    assert any(skill["id"] == "custom-test" for skill in catalog)
