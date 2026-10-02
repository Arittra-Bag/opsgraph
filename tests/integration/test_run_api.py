"""Contract tests with explicit doubles; real acceptance lives separately."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opsgraph.api.runs import RunAPI
from opsgraph.brokers import QueryExecutionFailed, QueryResult, SourceSchemaChanged
from opsgraph.config import get_settings
from opsgraph.domain import Obligation
from opsgraph.domain.models import stable_hash
from opsgraph.orchestration.connected import ModelPlanInconsistentError
from opsgraph.persistence import WorkspaceRecord
from opsgraph.providers import ProviderCapabilities, ProviderConfig, StructuredResponse
from opsgraph.readiness import source_readiness_basis
from opsgraph.runtime import build_runtime
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema


class ContractProvider:
    config = ProviderConfig(
        kind="openai_compatible", model="contract-double", base_url="http://localhost/v1"
    )
    capabilities = ProviderCapabilities(external_egress=False)

    def invoke_structured(self, request):
        if request.response_schema.get("title") == "InvestigationPlan":
            output = {
                "queries": [
                    {"purpose": "Count scoped rows", "sql": "SELECT id FROM public.records"}
                ],
                "rationale": "Read the allowed table.",
                "clarification": None,
            }
        else:
            evidence = json.loads(request.messages[0].content.split("Evidence: ", 1)[1])
            output = {
                "summary": "Inspect the finding and captured row.",
                "findings": [
                    {
                        "claim": "One record was observed.",
                        "classification": "supported",
                        "evidence_ids": [evidence[0]["evidence_hash"]],
                    }
                ],
                "limitations": ["One bounded read."],
            }
        return StructuredResponse(
            provider="openai_compatible", model=self.config.model, output=output
        )


class ContractExecutor:
    def __init__(self, dsn, **kwargs):
        assert dsn == "local-contract-only"
        assert kwargs["allowed_tables"] == ("public.records",)
        assert kwargs["allowed_schemas"] == ("public",)
        assert kwargs["expected_schema_fingerprint"] == contract_snapshot().fingerprint
        self.allowed_schemas = kwargs["allowed_schemas"]
        self.allowed_tables = kwargs["allowed_tables"]
        self.expected_schema_fingerprint = kwargs["expected_schema_fingerprint"]

    def cancel(self):
        return None

    def discover_snapshot(self, *, allowed_schemas, allowed_tables, timeout_ms=5000):
        return contract_snapshot()

    def execute_readonly(self, sql, *, timeout_ms):
        assert timeout_ms == 5000
        return QueryResult(columns=("id",), rows=((1,),))


def contract_snapshot():
    return SchemaSnapshot(
        tables=(
            TableSchema(
                schema_name="public",
                table_name="records",
                columns=(ColumnSchema(name="id", data_type="integer"),),
            ),
        ),
        fingerprint="unused-unscoped",
    ).scoped(("public.records",))


@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={"state_path": tmp_path / "state.db"})
    runtime = build_runtime(settings)
    runtime.provider = ContractProvider()
    workspace = settings.workspace_id
    source = {
        "record_type": "source",
        "id": "local-data",
        "name": "Contract data",
        "status": "ready",
        "secret_ref": "OPSGRAPH_SOURCE_DSN",
        "allowed_schemas": ["public"],
        "allowed_tables": ["public.records"],
        "allow_external_egress": False,
    }
    snapshot = contract_snapshot()
    source.update(
        {
            "schema_version": snapshot.fingerprint,
            "inspected_at": snapshot.model_dump(mode="json")["inspected_at"],
        }
    )
    source["readiness"] = {
        "status": "ready",
        "source_revision": source_readiness_basis(source),
        "policy_revision": stable_hash(Obligation().model_dump(mode="json")),
    }
    runtime.store.put(WorkspaceRecord(workspace, "source:local-data", source))
    runtime.store.put(
        WorkspaceRecord(workspace, "schema:local-data", snapshot.model_dump(mode="json"))
    )
    monkeypatch.setenv("OPSGRAPH_SOURCE_DSN", "local-contract-only")
    monkeypatch.setattr("opsgraph.api.runs.PsycopgReadOnlyExecutor", ContractExecutor)
    service = RunAPI(runtime, lambda *args: Obligation())
    app = FastAPI(lifespan=service.lifespan)
    app.include_router(service.router)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            service=service,
            runtime=runtime,
            headers={"X-OpsGraph-Key": settings.api_key},
        )


def wait(api, run_id):
    for _ in range(200):
        run = api.client.get(f"/api/runs/{run_id}", headers=api.headers).json()
        if run["status"] in {"completed", "failed", "blocked", "cancelled"}:
            return run
        time.sleep(0.01)
    pytest.fail("contract investigation did not complete")


def wait_for_terminal_audit(api, run_id):
    """Allow the worker to finish its audit callback after exposing terminal state."""

    for _ in range(200):
        entries = [
            entry
            for entry in api.runtime.audit.entries
            if entry.action == "core.investigation.connected" and entry.resource == run_id
        ]
        if entries:
            return entries
        time.sleep(0.01)
    pytest.fail("terminal investigation audit was not recorded")


def test_database_query_failure_is_distinct_from_connectivity(api, monkeypatch):
    def fail_query(self, sql, *, timeout_ms):
        raise QueryExecutionFailed("private driver detail must not enter events")

    monkeypatch.setattr(ContractExecutor, "execute_readonly", fail_query)
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect the observed record statuses?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed"
    assert run["error"]["code"] == "query_failed"
    assert "recorded query" in run["error"]["message"]
    assert run["evidence"] == [] and run["answer"] is None
    terminal_audit = wait_for_terminal_audit(api, run["id"])
    assert len(terminal_audit) == 1
    assert terminal_audit[0].outcome == "rejected"
    assert terminal_audit[0].details == {
        "status": "failed",
        "code": "query_failed",
        "source_id": "local-data",
        "evidence_count": 0,
    }
    exported = api.client.get(f"/api/runs/{run['id']}/export", headers=api.headers)
    assert exported.status_code == 200
    assert "private driver detail" not in exported.text


def test_real_only_run_contract_replay_export_followup(api):
    assert api.client.get("/api/runs").status_code == 401
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "How many scoped records exist?", "source_id": "local-data"},
    )
    assert response.status_code == 202
    run = wait(api, response.json()["id"])
    assert run["status"] == "completed", run
    completion_audit = wait_for_terminal_audit(api, run["id"])
    assert len(completion_audit) == 1
    assert completion_audit[0].outcome == "allowed"
    assert run["evidence"][0]["rows"] == [[1]]
    provenance = run["evidence"][0]["provenance"]
    assert provenance["source_id"] == "local-data"
    assert "SELECT" in provenance["sql"]
    assert provenance["capture_id"]
    events = api.client.get(f"/api/runs/{run['id']}/events", headers=api.headers).text
    assert "evidence_captured" in events and "completed" in events
    assert "local-contract-only" not in events
    exported = api.client.get(f"/api/runs/{run['id']}/export", headers=api.headers).json()
    assert exported["export_version"] == 1
    assert exported["evidence"] == run["evidence"]
    followup = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "What explains that observed count?",
            "source_id": "local-data",
            "parent_run_id": run["id"],
        },
    )
    assert followup.status_code == 202
    child = wait(api, followup.json()["id"])
    assert child["status"] == "completed"
    assert child["parent_run_id"] == run["id"] and child["id"] != run["id"]
    assert child["evidence"][0]["provenance"]["capture_id"] != provenance["capture_id"]
    wrong = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "What happened elsewhere?",
            "source_id": "other-source",
            "parent_run_id": run["id"],
        },
    )
    assert wrong.status_code == 409


def test_queued_cancellation_records_one_terminal_audit(api):
    # Keep the run queued so this endpoint, rather than the worker, creates the
    # terminal transition.
    api.service.coordinator.close()
    run = api.service.store.create(
        api.runtime.settings.workspace_id,
        {"question": "Cancel this queued investigation?", "source_id": "local-data"},
    )

    first = api.client.post(f"/api/runs/{run['id']}/cancel", headers=api.headers)
    second = api.client.post(f"/api/runs/{run['id']}/cancel", headers=api.headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "cancelled"
    assert first.json()["terminal_audited"] is True
    audits = [
        entry
        for entry in api.runtime.audit.entries
        if entry.action == "core.investigation.connected" and entry.resource == run["id"]
    ]
    assert len(audits) == 1
    assert audits[0].outcome == "rejected"
    assert audits[0].details == {
        "status": "cancelled",
        "code": "cancelled",
        "source_id": "local-data",
        "evidence_count": 0,
    }
    assert api.service.store.get(api.runtime.settings.workspace_id, run["id"])["terminal_audited"]


def test_failures_preserve_captures_and_retry_new_attempt(api):
    original = api.runtime.provider.invoke_structured

    def fail_answer(request):
        if request.response_schema.get("title") == "InvestigationAnswer":
            raise ValueError("credential-must-not-leak")
        return original(request)

    api.runtime.provider.invoke_structured = fail_answer
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "How many scoped records exist?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed"
    assert run["partial_evidence"] and len(run["evidence"]) == 1
    assert run["answer"] is None
    assert "credential-must-not-leak" not in json.dumps(run)
    api.runtime.provider.invoke_structured = original
    retried = api.client.post(f"/api/runs/{run['id']}/retry", headers=api.headers)
    assert retried.status_code == 202
    final = wait(api, retried.json()["id"])
    assert final["status"] == "completed" and final["retry_of"] == run["id"]
    assert final["id"] != run["id"]


def test_missing_source_is_a_durable_blocked_run(api):
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect this missing source?", "source_id": "not-configured"},
    )
    assert response.status_code == 202
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked"
    assert run["answer"] is None and run["evidence"] == []
    assert run["id"] in {
        item["id"] for item in api.client.get("/api/runs", headers=api.headers).json()
    }


def test_source_revision_change_stops_run_before_query(api):
    original = api.runtime.provider.invoke_structured

    def change_scope(request):
        response = original(request)
        if request.response_schema.get("title") == "InvestigationPlan":
            workspace = api.runtime.settings.workspace_id
            source = api.runtime.store.get(
                workspace_id=workspace, record_id="source:local-data"
            ).value
            source["allowed_tables"] = []
            api.runtime.store.put(WorkspaceRecord(workspace, "source:local-data", source))
        return response

    api.runtime.provider.invoke_structured = change_scope
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records after scope changes?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked"
    assert run["evidence"] == []
    assert "configuration changed" in run["error"]["message"]


def test_sample_migration_mode_blocks_new_and_queued_execution(api):
    api.runtime.settings = api.runtime.settings.model_copy(update={"mode": "sample"})
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records in migration mode?", "source_id": "local-data"},
    )
    assert response.status_code == 409
    queued = api.service.store.create(
        api.runtime.settings.workspace_id,
        {"question": "Previously accepted work must not execute?", "source_id": "local-data"},
    )
    api.service.coordinator.notify()
    run = wait(api, queued["id"])
    assert run["status"] == "blocked"
    assert run["evidence"] == [] and run["answer"] is None
    assert "Migration mode" in run["error"]["message"]


def test_model_timeout_becomes_persisted_actionable_failure(api):
    from opsgraph.providers import ProviderTimeoutError

    def timeout(request):
        raise ProviderTimeoutError("SDK timeout")

    api.runtime.provider.invoke_structured = timeout
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records with a slow model?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed"
    assert run["error"]["code"] == "model_timeout"
    assert "timeout" in run["error"]["message"]
    assert run["answer"] is None


def test_ollama_profile_keeps_canonical_answer_limits(api):
    api.runtime.provider.config = api.runtime.provider.config.model_copy(
        update={"schema_profile": "ollama"}
    )
    original = api.runtime.provider.invoke_structured

    def overlong_answer(request):
        response = original(request)
        if request.response_schema.get("title") == "InvestigationAnswer":
            assert (
                request.response_schema["$defs"]["CitedFinding"]["properties"]["claim"]["maxLength"]
                == 2000
            )
            response.output["findings"][0]["claim"] = "x" * 2001
        return response

    api.runtime.provider.invoke_structured = overlong_answer
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records with local grammar profile?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed" and run["answer"] is None
    assert run["partial_evidence"]
    assert run["evidence"][0]["provenance"]["run_id"] == run["id"]
    assert run["configuration"]["schema_profile"] == "ollama"


def test_reported_model_identity_follows_planning_and_answer_calls(api):
    original = api.runtime.provider.invoke_structured

    def reported_identity(request):
        response = original(request)
        name = (
            "reported-planner"
            if request.response_schema.get("title") == "InvestigationPlan"
            else "reported-reconciler"
        )
        return response.model_copy(update={"reported_model": name})

    api.runtime.provider.invoke_structured = reported_identity
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records with a model alias?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "completed"
    assert run["planning_reported_model"] == "reported-planner"
    assert run["answer_reported_model"] == "reported-reconciler"
    assert run["evidence"][0]["provenance"]["reported_model"] == "reported-planner"
    assert run["evidence"][0]["provenance"]["model"] == "contract-double"


def test_published_playbook_change_stops_later_operations(api):
    original = api.runtime.provider.invoke_structured

    def publish_changed_playbook(request):
        response = original(request)
        if request.response_schema.get("title") == "InvestigationPlan":
            old = api.runtime.skills.get_published("generic-readonly")
            api.runtime.skills.save_draft(old.model_copy(update={"version": "0.1.1"}))
            api.runtime.skills.publish(old.id)
        return response

    api.runtime.provider.invoke_structured = publish_changed_playbook
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records while the playbook changes?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked"
    assert run["evidence"] == []
    assert "playbook changed" in run["error"]["message"]


def test_compact_reconcile_context_retains_evidence_and_classification_guidance(api):
    original = api.runtime.provider.invoke_structured
    seen = []

    def inspect_context(request):
        if request.response_schema.get("title") == "InvestigationAnswer":
            prompt = request.messages[0].content
            context = json.loads(prompt.split("Evidence: ", 1)[1])
            assert context[0]["rows"] == [[1]]
            assert context[0]["source_id"] == "local-data"
            assert context[0]["executed_query"] and context[0]["captured_at"]
            assert context[0]["proposed_query"]
            assert context[0]["captured_row_count"] == 1
            assert context[0]["truncated"] is False
            assert context[0]["limits"] == {"max_rows": 100, "timeout_ms": 5000}
            assert "provenance" not in context[0]
            assert "contradictory means cited evidence refutes" in prompt
            assert "each observed status is supported" in prompt
            seen.append(True)
        return original(request)

    api.runtime.provider.invoke_structured = inspect_context
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect the observed record statuses?", "source_id": "local-data"},
    )
    assert wait(api, response.json()["id"])["status"] == "completed"
    assert seen == [True]


def test_output_truncation_preserves_evidence_without_partial_answer(api):
    from opsgraph.providers import ProviderOutputTruncatedError

    original = api.runtime.provider.invoke_structured

    def truncated_answer(request):
        if request.response_schema.get("title") == "InvestigationAnswer":
            raise ProviderOutputTruncatedError("response reached token limit")
        return original(request)

    api.runtime.provider.invoke_structured = truncated_answer
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect records with a truncated model response?",
            "source_id": "local-data",
        },
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed"
    assert run["partial_evidence"] and run["answer"] is None
    assert run["error"]["code"] == "model_output_truncated"


def test_oversized_evidence_has_distinct_failure_and_preserves_prior_capture(api, monkeypatch):
    original = api.runtime.provider.invoke_structured
    calls = []

    def two_queries(request):
        response = original(request)
        if request.response_schema.get("title") == "InvestigationPlan":
            response.output["queries"].append(dict(response.output["queries"][0]))
        return response

    def execute(self, sql, *, timeout_ms):
        calls.append(sql)
        if len(calls) == 1:
            return QueryResult(columns=("id",), rows=((1,),))
        return QueryResult(columns=("payload",), rows=(("x" * 16_385,),))

    api.runtime.provider.invoke_structured = two_queries
    monkeypatch.setattr(ContractExecutor, "execute_readonly", execute)
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect a large column after a small read?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed" and run["partial_evidence"]
    assert len(run["evidence"]) == 1 and run["answer"] is None
    assert run["error"]["code"] == "evidence_too_large"
    assert "fewer columns" in run["error"]["message"]


@pytest.mark.parametrize(
    "failure,expected_code",
    [
        ("invalid_plan", "model_output_invalid"),
        ("invalid_answer", "model_output_invalid"),
        ("forbidden_classification", "model_output_invalid"),
        ("unknown_citation", "model_citation_invalid"),
        ("missing_citation", "model_citation_invalid"),
    ],
)
def test_invalid_model_output_is_actionable_without_raw_response(api, failure, expected_code):
    original = api.runtime.provider.invoke_structured
    if failure == "forbidden_classification":
        skill = api.runtime.skills.get_published("generic-readonly")
        api.runtime.skills.save_draft(
            skill.model_copy(update={"version": "0.2.0", "conclusion_classes": ("unknown",)})
        )
        api.runtime.skills.publish(skill.id)

    def invalid_output(request):
        response = original(request)
        planning = request.response_schema.get("title") == "InvestigationPlan"
        if planning and failure == "invalid_plan":
            return response.model_copy(update={"output": {"private-output-marker": "hidden"}})
        if not planning:
            if failure == "invalid_answer":
                return response.model_copy(update={"output": {"private-output-marker": "hidden"}})
            if failure == "unknown_citation":
                response.output["findings"][0]["evidence_ids"] = ["private-output-marker"]
            if failure == "missing_citation":
                response.output["findings"][0]["evidence_ids"] = []
        return response

    api.runtime.provider.invoke_structured = invalid_output
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect records with malformed model output?",
            "source_id": "local-data",
        },
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed" and run["answer"] is None
    assert run["error"]["code"] == expected_code
    assert "no unvalidated answer" in run["error"]["message"]
    assert "private-output-marker" not in json.dumps(run)
    assert bool(run["evidence"]) is (failure != "invalid_plan")


def test_inconsistent_answer_retries_once_then_fails_with_capture_preserved(api, monkeypatch):
    original_provider = api.runtime.provider.invoke_structured
    original_execute = ContractExecutor.execute_readonly
    answer_calls = 0
    query_calls = 0

    def inconsistent_answer(request):
        nonlocal answer_calls
        response = original_provider(request)
        if request.response_schema.get("title") == "InvestigationAnswer":
            answer_calls += 1
            response.output["limitations"] = [
                "The actual row limit was not enforced in the captured result."
            ]
        return response

    def counted_execute(self, sql, *, timeout_ms):
        nonlocal query_calls
        query_calls += 1
        return original_execute(self, sql, timeout_ms=timeout_ms)

    api.runtime.provider.invoke_structured = inconsistent_answer
    monkeypatch.setattr(ContractExecutor, "execute_readonly", counted_execute)
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect the observed records?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed"
    assert run["answer"] is None and len(run["evidence"]) == 1
    assert run["partial_evidence"] is True
    assert run["error"]["code"] == "model_answer_inconsistent"
    assert "one bounded correction attempt" in run["error"]["message"]
    assert "actual row limit" not in json.dumps(run)
    assert answer_calls == 2 and query_calls == 1
    events = api.client.get(f"/api/runs/{run['id']}/events", headers=api.headers).text
    assert events.count('"type": "answer_validation_retry"') == 1
    assert events.count('"type": "evidence_captured"') == 1


def test_decimal_capture_export_reopen_verifies_original_digest(api, monkeypatch):
    import hashlib
    from decimal import Decimal

    from opsgraph.persistence.runs import RunStore

    original = api.runtime.provider.invoke_structured

    def decimal_result(self, sql, *, timeout_ms):
        return QueryResult(columns=("average_duration",), rows=((Decimal("12.3400"),),))

    def inspect_model_context(request):
        if request.response_schema.get("title") == "InvestigationAnswer":
            context = json.loads(request.messages[0].content.split("Evidence: ", 1)[1])
            assert context[0]["rows"] == [["12.3400"]]
            assert "integrity" not in context[0]
        return original(request)

    monkeypatch.setattr(ContractExecutor, "execute_readonly", decimal_result)
    api.runtime.provider.invoke_structured = inspect_model_context
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect a precise decimal aggregate?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "completed"
    exported = api.client.get(f"/api/runs/{run['id']}/export", headers=api.headers).json()
    capture = exported["evidence"][0]
    assert capture["rows"] == [["12.3400"]]
    integrity = capture["integrity"]
    assert integrity["format"] == "opsgraph-canonical-json-v1"
    assert integrity["scope"] == "query-result"
    assert (
        "sha256:" + hashlib.sha256(integrity["canonical_json"].encode()).hexdigest()
        == capture["evidence_hash"]
    )
    assert json.loads(integrity["canonical_json"])["rows"] == [[{"$decimal": "12.3400"}]]
    reopened = RunStore(api.service.store.path).get(api.runtime.settings.workspace_id, run["id"])
    assert reopened["evidence"] == exported["evidence"]


def test_complete_capture_bound_includes_exact_hash_input(api, monkeypatch):
    from opsgraph.domain.models import canonical_json

    original = api.runtime.provider.invoke_structured
    calls = []
    large_rows = tuple(("\\" * 8186,) for _ in range(8))
    assert len(canonical_json({"columns": ("payload",), "rows": large_rows})) <= 131_072
    assert all(len(canonical_json(row[0])) <= 16_384 for row in large_rows)

    def two_queries(request):
        response = original(request)
        if request.response_schema.get("title") == "InvestigationPlan":
            response.output["queries"].append(dict(response.output["queries"][0]))
        return response

    def execute(self, sql, *, timeout_ms):
        calls.append(sql)
        return QueryResult(columns=("payload",), rows=((1,),) if len(calls) == 1 else large_rows)

    api.runtime.provider.invoke_structured = two_queries
    monkeypatch.setattr(ContractExecutor, "execute_readonly", execute)
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect data whose escaped capture exceeds storage bounds?",
            "source_id": "local-data",
        },
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed" and run["partial_evidence"]
    assert run["error"]["code"] == "evidence_too_large"
    assert len(run["evidence"]) == 1 and run["answer"] is None


@pytest.mark.parametrize("change_after", [0, 1, 2])
def test_live_schema_drift_blocks_before_planning_or_next_query(api, monkeypatch, change_after):
    checks, reads, model_calls = [], [], []
    original = api.runtime.provider.invoke_structured

    def discover(self, **kwargs):
        snapshot = contract_snapshot()
        if len(checks) >= change_after:
            table = snapshot.tables[0].model_copy(
                update={
                    "columns": (
                        *snapshot.tables[0].columns,
                        ColumnSchema(name="new", data_type="text"),
                    )
                }
            )
            snapshot = snapshot.model_copy(update={"tables": (table,)}).scoped(("public.records",))
        checks.append(True)
        return snapshot

    def provider(request):
        model_calls.append(request.response_schema["title"])
        response = original(request)
        if request.response_schema["title"] == "InvestigationPlan":
            response.output["queries"].append(dict(response.output["queries"][0]))
        return response

    def read(self, sql, *, timeout_ms):
        live = self.discover_snapshot(
            allowed_schemas=self.allowed_schemas,
            allowed_tables=self.allowed_tables,
            timeout_ms=timeout_ms,
        )
        if live.fingerprint != self.expected_schema_fingerprint:
            raise SourceSchemaChanged("source schema changed after review")
        reads.append(True)
        return QueryResult(columns=("id",), rows=((1,),))

    monkeypatch.setattr(ContractExecutor, "discover_snapshot", discover)
    monkeypatch.setattr(ContractExecutor, "execute_readonly", read)
    api.runtime.provider.invoke_structured = provider
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect records with changing physical columns?",
            "source_id": "local-data",
        },
    )
    run = wait(api, response.json()["id"])
    assert (
        run["status"] == "blocked" and "SELECT-visible columns changed" in run["error"]["message"]
    )
    assert len(run["evidence"]) == len(reads) == max(0, change_after - 1)
    assert len(model_calls) == (0 if change_after == 0 else 1)
    assert run["answer"] is None
    source = api.runtime.store.get(
        workspace_id=api.runtime.settings.workspace_id, record_id="source:local-data"
    ).value
    assert source["status"] == "stale"


def test_clarification_is_durable_blocked_without_queries_or_answer(api, monkeypatch):
    reads = []
    question = "Which timezone defines the requested business day?"

    def clarify(request):
        assert "Never guess those meanings from names" in request.messages[0].content
        return StructuredResponse(
            provider="openai_compatible",
            model="contract-double",
            output={
                "queries": [],
                "rationale": "Time semantics are unspecified.",
                "clarification": question,
            },
        )

    monkeypatch.setattr(
        ContractExecutor, "execute_readonly", lambda *args, **kwargs: reads.append(1)
    )
    api.runtime.provider.invoke_structured = clarify
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "How many failed records occurred yesterday?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked" and run["error"]["code"] == "clarification_required"
    assert question in run["error"]["message"]
    assert run["evidence"] == reads == [] and run["answer"] is None and run["plan"] is None


@pytest.mark.parametrize(
    "values",
    [
        {"queries": [], "clarification": None},
        {
            "queries": [{"purpose": "Read records", "sql": "SELECT id FROM public.records"}],
            "clarification": "Which timezone defines the day?",
        },
        {"queries": [], "clarification": "Which timezone?\nHidden control line"},
    ],
)
def test_clarification_never_weakens_query_plan_contract(values):
    from pydantic import ValidationError

    from opsgraph.orchestration.connected import InvestigationPlan

    with pytest.raises(ValidationError):
        InvestigationPlan.model_validate({"rationale": "Contract validation.", **values})


def test_specialist_missing_mapping_blocks_with_actionable_error(api):
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect failed jobs and queue states?",
            "source_id": "local-data",
            "skill_id": "failed-jobs",
        },
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked" and run["error"]["code"] == "mapping_required"
    assert "job_status" in run["error"]["message"] and "queue_state" in run["error"]["message"]
    assert "General PostgreSQL" in run["error"]["message"]
    assert run["plan"] is None and run["evidence"] == []


def test_specialist_planner_receives_approved_contract(api):
    workspace = api.runtime.settings.workspace_id
    record = api.runtime.store.get(workspace_id=workspace, record_id="source:local-data")
    record.value["evidence_bindings"] = [
        {"evidence_type": name, "source_tables": ["public.records"]}
        for name in ("job_status", "queue_state")
    ]
    api.runtime.store.put(record)
    original = api.runtime.provider.invoke_structured

    def inspect(request):
        if request.response_schema["title"] == "InvestigationPlan":
            prompt = request.messages[0].content
            assert "Establish when jobs failed" in prompt
            assert 'Required evidence types: ["job_status", "queue_state"]' in prompt
            assert '"source_tables": ["public.records"]' in prompt
            assert "not semantic proof" in prompt
        return original(request)

    api.runtime.provider.invoke_structured = inspect
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect failed jobs and queue states?",
            "source_id": "local-data",
            "skill_id": "failed-jobs",
        },
    )
    assert wait(api, response.json()["id"])["status"] == "completed"


@pytest.mark.parametrize("kind", ["naive_datetime", "interval", "binary", "nonfinite"])
def test_unsupported_values_are_sanitized_and_preserve_earlier_capture(api, monkeypatch, kind):
    from datetime import datetime, timedelta

    values = {
        "naive_datetime": datetime(2026, 9, 12),
        "interval": timedelta(seconds=1),
        "binary": b"\xffprivate-record",
        "nonfinite": float("inf"),
    }
    original = api.runtime.provider.invoke_structured
    calls = []

    def provider(request):
        response = original(request)
        if request.response_schema["title"] == "InvestigationPlan":
            response.output["queries"].append(dict(response.output["queries"][0]))
        return response

    def read(self, sql, *, timeout_ms):
        calls.append(True)
        return QueryResult(columns=("value",), rows=((1 if len(calls) == 1 else values[kind],),))

    api.runtime.provider.invoke_structured = provider
    monkeypatch.setattr(ContractExecutor, "execute_readonly", read)
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect a supported then unsupported value?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed" and run["error"]["code"] == "evidence_type_unsupported"
    assert run["partial_evidence"] and len(run["evidence"]) == 1 and run["answer"] is None
    assert "private-record" not in json.dumps(run)


@pytest.mark.parametrize("action", ["core.query.read", "core.schema.inspect"])
def test_deployment_schema_policy_cannot_be_widened_by_source(api, monkeypatch, action):
    reads = []
    api.service.authorize = lambda principal, requested, resource: (
        Obligation(allowed_schemas=("approved_elsewhere",)) if requested == action else Obligation()
    )
    monkeypatch.setattr(
        ContractExecutor, "discover_snapshot", lambda *args, **kwargs: reads.append(1)
    )
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Inspect records outside deployment scope?", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked" and "deployment policy" in run["error"]["message"]
    assert reads == [] and run["plan"] is None and run["evidence"] == []


def test_current_policy_limits_are_applied_and_recorded(api, monkeypatch):
    tightened = Obligation(max_rows=1, timeout_ms=700)
    api.service.authorize = lambda *args: tightened
    source = api.runtime.store.get(
        workspace_id=api.runtime.settings.workspace_id, record_id="source:local-data"
    )
    source.value["readiness"]["policy_revision"] = stable_hash(tightened.model_dump(mode="json"))
    api.runtime.store.put(source)
    calls = []

    def execute(self, sql, *, timeout_ms):
        calls.append((sql, timeout_ms))
        assert sql.endswith("LIMIT 2") and timeout_ms == 700
        return QueryResult(columns=("id",), rows=((1,), (2,)))

    monkeypatch.setattr(ContractExecutor, "execute_readonly", execute)
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect records with stricter deployment limits?",
            "source_id": "local-data",
        },
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "completed" and len(calls) == 1
    assert run["configuration"]["limits"]["max_rows"] == 1
    assert run["configuration"]["limits"]["timeout_ms"] == 700
    assert run["configuration"]["policy_fingerprint"].startswith("sha256:")
    assert run["evidence"][0]["truncated"] and run["evidence"][0]["rows"] == [[1]]


def test_policy_revision_change_during_planning_blocks_before_query(api, monkeypatch):
    current = [Obligation()]
    api.service.authorize = lambda *args: current[0]
    original = api.runtime.provider.invoke_structured
    reads = []

    def tighten(request):
        response = original(request)
        if request.response_schema["title"] == "InvestigationPlan":
            current[0] = Obligation(max_rows=1)
        return response

    api.runtime.provider.invoke_structured = tighten
    monkeypatch.setattr(
        ContractExecutor, "execute_readonly", lambda *args, **kwargs: reads.append(1)
    )
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={
            "question": "Inspect records while deployment policy changes?",
            "source_id": "local-data",
        },
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "blocked" and "Deployment policy changed" in run["error"]["message"]
    assert reads == [] and run["evidence"] == []


def test_inconsistent_plan_error_is_actionable_without_collecting_evidence(api):
    def rejected_plan(request):
        raise ModelPlanInconsistentError("private model response must not be exposed")

    api.runtime.provider.invoke_structured = rejected_plan
    response = api.client.post(
        "/api/runs",
        headers=api.headers,
        json={"question": "Include empty parents", "source_id": "local-data"},
    )
    run = wait(api, response.json()["id"])
    assert run["status"] == "failed"
    assert run["error"]["code"] == "model_plan_inconsistent"
    assert "No query was executed" in run["error"]["message"]
    assert run["plan"] is None and run["answer"] is None and run["evidence"] == []
    assert "private model response" not in json.dumps(run)
