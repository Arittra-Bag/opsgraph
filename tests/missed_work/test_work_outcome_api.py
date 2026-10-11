"""Opt-in API/control-path acceptance on disposable real PostgreSQL."""

import time
from copy import deepcopy

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from opsgraph.api.runs import RunAPI
from opsgraph.api.work_outcomes import router_for
from opsgraph.config import get_settings
from opsgraph.domain.models import stable_hash
from opsgraph.persistence import WorkspaceRecord
from opsgraph.policy import ActionRequest
from opsgraph.readiness import source_readiness_basis
from opsgraph.runtime import build_runtime

from .pg_fixture import DisposableDatabase
from .test_export_fixtures import large_cohort
from .test_postgres import ready_case


@pytest.fixture
def audit_api(postgres, tmp_path, monkeypatch):
    settings = get_settings().model_copy(
        update={
            "state_path": tmp_path / "state.db",
            "postgres_allowed_schemas": (postgres["schema"],),
            "postgres_secret_ref": "OPSGRAPH_MW_TEST_DSN",
            "allowed_postgres_secret_refs": ("OPSGRAPH_MW_TEST_DSN",),
        }
    )
    monkeypatch.setenv("OPSGRAPH_MW_TEST_DSN", postgres["dsn"])
    runtime = build_runtime(settings)
    monkeypatch.setattr(
        runtime.provider,
        "invoke_structured",
        lambda *args: pytest.fail("Outcome audits must not invoke a model"),
    )

    def authorize(principal, action, resource):
        decision = runtime.policy.authorize(
            ActionRequest(
                principal=principal,
                action=action,
                workspace_id=principal.workspace_id,
                resource=resource,
            )
        )
        if not decision.allowed:
            raise HTTPException(403, "not authorized")
        return decision.obligations

    source = {
        "record_type": "source",
        "id": "exports-db",
        "name": "Synthetic exports",
        "status": "ready",
        "secret_ref": "OPSGRAPH_MW_TEST_DSN",
        "allowed_schemas": [postgres["schema"]],
        "allowed_tables": list(postgres["bounds"].allowed_tables),
        "allow_external_egress": False,
        "schema_version": postgres["snapshot"].fingerprint,
    }
    from opsgraph.domain import Principal

    principal = Principal(subject="test", workspace_id=settings.workspace_id, roles={"analyst"})
    source["readiness"] = {
        "status": "ready",
        "source_revision": source_readiness_basis(source),
        "policy_revision": stable_hash(
            authorize(principal, "core.source.readiness", source["id"]).model_dump(mode="json")
        ),
    }
    runtime.store.put(WorkspaceRecord(settings.workspace_id, "source:exports-db", source))
    runtime.store.put(
        WorkspaceRecord(
            settings.workspace_id, "schema:exports-db", postgres["snapshot"].model_dump(mode="json")
        )
    )
    service = RunAPI(runtime, authorize)
    app = FastAPI(lifespan=service.lifespan)
    app.include_router(service.router)
    app.include_router(router_for(runtime, authorize))
    with TestClient(app) as client:
        yield DisposableDatabase(
            client=client,
            service=service,
            runtime=runtime,
            headers={"X-OpsGraph-Key": settings.api_key},
            source=source,
        )


def review(api, pg, definition=None):  # noqa: ANN001
    return api["client"].post(
        "/api/work-outcomes/contracts",
        headers=api["headers"],
        json={
            "definition": definition or pg["contract"].model_dump(mode="json"),
            "confirm_reviewed_meanings": True,
        },
    )


def body(pg):  # noqa: ANN001
    return {
        "question": "Which approved exports are overdue?",
        "source_id": "exports-db",
        "work_outcome": pg["scope"].model_dump(mode="json"),
    }


def wait(api, run_id):  # noqa: ANN001
    for _ in range(500):
        run = api["client"].get(f"/api/runs/{run_id}", headers=api["headers"]).json()
        if run["status"] in {"completed", "failed", "blocked", "cancelled"}:
            return run
        time.sleep(0.01)
    pytest.fail("audit did not finish")


def test_review_run_export_and_restart_preserve_independent_calculation(audit_api, postgres):
    postgres["load"](large_cohort())
    assert review(audit_api, postgres).status_code == 200
    created = audit_api["client"].post(
        "/api/runs", headers=audit_api["headers"], json=body(postgres)
    )
    assert created.status_code == 202
    run = wait(audit_api, created.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert len(run["evidence"]) == 1 and run["configuration"]["execution_kind"] == "work_outcome"
    calculation = run["answer"]["work_outcome"]
    assert calculation["facts"]["population"] == 1000
    assert calculation["facts"]["counts"]["overdue_unresolved"] == 12
    assert calculation["facts"]["affected_customers"] == 8
    assert calculation["input_evidence_hashes"] == [run["evidence"][0]["evidence_hash"]]
    assert (
        stable_hash({k: v for k, v in calculation.items() if k != "calculation_hash"})
        == calculation["calculation_hash"]
    )
    exported = (
        audit_api["client"]
        .get(f"/api/runs/{run['id']}/export", headers=audit_api["headers"])
        .json()
    )
    assert exported["answer"]["work_outcome"] == calculation
    from opsgraph.persistence.runs import RunStore

    assert (
        RunStore(audit_api["runtime"].settings.state_path).get(
            audit_api["runtime"].settings.workspace_id, run["id"]
        )["answer"]["work_outcome"]
        == calculation
    )


def test_review_requires_workspace_access_and_explicit_confirmation(audit_api, postgres):
    definition = postgres["contract"].model_dump(mode="json")
    assert audit_api["client"].post(
        "/api/work-outcomes/contracts",
        json={"definition": definition, "confirm_reviewed_meanings": True},
    ).status_code in {401, 403}
    assert (
        audit_api["client"]
        .post(
            "/api/work-outcomes/contracts",
            headers=audit_api["headers"],
            json={"definition": definition, "confirm_reviewed_meanings": False},
        )
        .status_code
        == 422
    )


def test_contract_version_is_immutable_and_source_scope_cannot_expand(audit_api, postgres):
    assert review(audit_api, postgres).status_code == 200
    changed = postgres["contract"].model_dump(mode="json")
    changed["deadline"] = {"kind": "fixed_sla", "seconds": 600}
    assert review(audit_api, postgres, changed).status_code == 409
    changed["version"] = 2
    changed["outcomes"]["relation"] = "private.forbidden"
    assert review(audit_api, postgres, changed).status_code == 422


def test_audit_needs_review_and_current_readiness(audit_api, postgres):
    postgres["load"](large_cohort())
    response = audit_api["client"].post(
        "/api/runs", headers=audit_api["headers"], json=body(postgres)
    )
    assert wait(audit_api, response.json()["id"])["status"] == "blocked"
    assert review(audit_api, postgres).status_code == 200
    workspace = audit_api["runtime"].settings.workspace_id
    source = {**audit_api["source"], "readiness": {"status": "pending"}}
    audit_api["runtime"].store.put(WorkspaceRecord(workspace, "source:exports-db", source))
    response = audit_api["client"].post(
        "/api/runs", headers=audit_api["headers"], json=body(postgres)
    )
    assert wait(audit_api, response.json()["id"])["status"] == "blocked"


def test_request_id_cannot_reuse_a_different_outcome_scope(audit_api, postgres):
    postgres["load"](large_cohort())
    assert review(audit_api, postgres).status_code == 200
    request = {**body(postgres), "request_id": "fixed-request"}
    response = audit_api["client"].post("/api/runs", headers=audit_api["headers"], json=request)
    assert wait(audit_api, response.json()["id"])["status"] == "completed"
    changed = deepcopy(request)
    changed["work_outcome"]["tenant_id"] = "different-tenant"
    assert (
        audit_api["client"]
        .post("/api/runs", headers=audit_api["headers"], json=changed)
        .status_code
        == 409
    )


def test_changed_definition_without_matching_review_receipt_cannot_execute(audit_api, postgres):
    postgres["load"](large_cohort())
    assert review(audit_api, postgres).status_code == 200
    runtime = audit_api["runtime"]
    key = "work-outcome:customer-exports:1"
    record = runtime.store.get(workspace_id=runtime.settings.workspace_id, record_id=key)
    value = deepcopy(record.value)
    value["definition"]["deadline"] = {"kind": "fixed_sla", "seconds": 600}
    runtime.store.put(WorkspaceRecord(record.workspace_id, key, value))
    response = audit_api["client"].post(
        "/api/runs", headers=audit_api["headers"], json=body(postgres)
    )
    run = wait(audit_api, response.json()["id"])
    assert run["status"] == "blocked" and run["evidence"] == []


def test_retry_retains_explicit_audit_scope(audit_api, postgres):
    postgres["load"](large_cohort())
    assert review(audit_api, postgres).status_code == 200
    response = audit_api["client"].post(
        "/api/runs", headers=audit_api["headers"], json=body(postgres)
    )
    first = wait(audit_api, response.json()["id"])
    retry = audit_api["client"].post(f"/api/runs/{first['id']}/retry", headers=audit_api["headers"])
    second = wait(audit_api, retry.json()["id"])
    assert second["status"] == "completed"
    assert second["work_outcome"] == first["work_outcome"]
    assert second["id"] != first["id"] and len(second["evidence"]) == 1


@pytest.mark.parametrize("kind", ("promises", "outcomes", "attempts", "cancellations"))
def test_blank_identity_cannot_produce_definitive_api_outcome(audit_api, postgres, kind):
    data = ready_case()
    if kind == "promises":
        data["promises"][0]["promise_id"] = "\t\u00a0 "
        data["outcomes"][0]["promise_id"] = "\t\u00a0 "
    else:
        event = {**data["outcomes"][0], "event_id": "\t\u00a0 "}
        if kind != "outcomes":
            event["status"] = "failed" if kind == "attempts" else "cancelled"
            data["outcomes"] = []
        data[kind] = [event]
    postgres["load"](data)
    assert review(audit_api, postgres).status_code == 200
    response = audit_api["client"].post(
        "/api/runs", headers=audit_api["headers"], json=body(postgres)
    )
    assert response.status_code == 202
    run = wait(audit_api, response.json()["id"])
    assert len(run["evidence"]) == 1
    if kind == "promises":
        assert run["status"] == "blocked" and run["answer"] is None
    else:
        assert run["status"] == "completed"
        facts = run["answer"]["work_outcome"]["facts"]
        assert facts["population"] == 1 and facts["counts"]["unknown"] == 1
        assert sum(facts["counts"].values()) == 1
        assert facts["examples"][0]["breach"] is None
