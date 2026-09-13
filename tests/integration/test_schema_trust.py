"""Physical schema readiness uses explicit scope and revision-safe persistence."""

from datetime import UTC, datetime
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from opsgraph.config import get_settings
from opsgraph.domain import Obligation
from opsgraph.persistence import WorkspaceRecord
from opsgraph.runtime import build_runtime
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema


@pytest.fixture
def schema_api(tmp_path, monkeypatch):
    module = import_module("opsgraph.api.app")
    runtime = build_runtime(get_settings().model_copy(update={"state_path": tmp_path / "state.db"}))
    monkeypatch.setattr(module, "runtime", runtime)
    monkeypatch.setenv("OPSGRAPH_SOURCE_DSN", "isolated-contract-secret")
    workspace = runtime.settings.workspace_id
    source = {
        "record_type": "source",
        "id": "schema-data",
        "name": "Schema contract",
        "status": "ready",
        "secret_ref": "OPSGRAPH_SOURCE_DSN",
        "allowed_schemas": ["public"],
        "allowed_tables": ["public.records"],
    }
    snapshot = SchemaSnapshot(
        tables=(
            TableSchema(
                schema_name="public",
                table_name="records",
                columns=(
                    ColumnSchema(name="id", data_type="integer", nullable=False),
                    ColumnSchema(name="occurred_at", data_type="timestamp without time zone"),
                ),
            ),
        ),
        fingerprint="unused",
        inspected_at=datetime(2026, 9, 12, tzinfo=UTC),
    ).scoped(("public.records",))
    runtime.store.put(WorkspaceRecord(workspace, "source:schema-data", source))
    runtime.store.put(
        WorkspaceRecord(workspace, "schema:schema-data", snapshot.model_dump(mode="json"))
    )

    class Executor:
        calls = []

        def __init__(self, dsn):
            assert dsn == "isolated-contract-secret"

        def discover_snapshot(self, **kwargs):
            self.calls.append(kwargs)
            return snapshot

    monkeypatch.setattr(module, "PsycopgReadOnlyExecutor", Executor)
    return (
        TestClient(module.app),
        runtime,
        Executor,
        snapshot,
        {"X-OpsGraph-Key": runtime.settings.api_key},
    )


def test_saved_schema_is_authenticated_scoped_and_honest_about_metadata(schema_api):
    client, runtime, executor, snapshot, headers = schema_api
    assert client.get("/api/sources/schema-data/schema").status_code == 401
    result = client.get("/api/sources/schema-data/schema", headers=headers)
    assert result.status_code == 200
    payload = result.json()
    assert payload["freshness"] == "last_inspection" and payload["status"] == "ready"
    assert payload["inspected_at"] and payload["fingerprint"] == snapshot.fingerprint
    assert payload["tables"][0]["columns"][0]["name"] == "id"
    assert payload["type_warnings"][0]["column"] == "occurred_at"
    assert "never assumes UTC" in payload["type_warnings"][0]["message"]
    assert "Relationships" in " ".join(payload["metadata_limits"])
    assert executor.calls == []
    legacy = snapshot.model_dump(mode="json")
    legacy.pop("inspected_at")
    runtime.store.put(WorkspaceRecord(runtime.settings.workspace_id, "schema:schema-data", legacy))
    assert (
        client.get("/api/sources/schema-data/schema", headers=headers).json()["inspected_at"]
        is None
    )
    assert client.get("/api/sources/other-data/schema", headers=headers).status_code == 404


def test_inspection_records_time_and_scoped_metadata_atomically(schema_api):
    client, runtime, executor, snapshot, headers = schema_api
    response = client.post("/api/sources/schema-data/inspect", headers=headers)
    assert response.status_code == 200
    assert response.json()["inspected_at"] == snapshot.model_dump(mode="json")["inspected_at"]
    assert executor.calls == [
        {"allowed_schemas": ("public",), "allowed_tables": ("public.records",), "timeout_ms": 5000}
    ]
    source = runtime.store.get(
        workspace_id=runtime.settings.workspace_id, record_id="source:schema-data"
    )
    assert source.value["status"] == "ready"
    assert source.value["inspected_at"] == response.json()["inspected_at"]


@pytest.mark.parametrize("failure", ["empty_scope", "missing_ref", "unapproved_ref", "no_select"])
def test_failed_inspection_never_leaves_ready_state(schema_api, monkeypatch, failure):
    client, runtime, executor, snapshot, headers = schema_api
    source = runtime.store.get(
        workspace_id=runtime.settings.workspace_id, record_id="source:schema-data"
    )
    if failure == "empty_scope":
        source.value["allowed_tables"] = []
        runtime.store.put(source)
    elif failure == "missing_ref":
        monkeypatch.delenv("OPSGRAPH_SOURCE_DSN")
    elif failure == "unapproved_ref":
        source.value["secret_ref"] = "UNAPPROVED_REFERENCE"  # noqa: S105 - variable name, no secret.
        runtime.store.put(source)
    else:
        monkeypatch.setattr(
            executor, "discover_snapshot", lambda self, **kwargs: snapshot.scoped(())
        )
    response = client.post("/api/sources/schema-data/inspect", headers=headers)
    assert response.status_code in {409, 422}
    source = runtime.store.get(
        workspace_id=runtime.settings.workspace_id, record_id="source:schema-data"
    )
    assert source.value["status"] == "stale"
    if failure != "no_select":
        assert executor.calls == []


def test_inspection_does_not_overwrite_concurrent_source_edit(schema_api, monkeypatch):
    client, runtime, executor, snapshot, headers = schema_api
    workspace = runtime.settings.workspace_id
    before = runtime.store.get(workspace_id=workspace, record_id="schema:schema-data").value

    def changed(self, **kwargs):
        source = runtime.store.get(workspace_id=workspace, record_id="source:schema-data")
        source.value.update(
            {"name": "Operator changed this", "allowed_tables": [], "status": "configured"}
        )
        runtime.store.put(source)
        return snapshot

    monkeypatch.setattr(executor, "discover_snapshot", changed)
    response = client.post("/api/sources/schema-data/inspect", headers=headers)
    assert response.status_code == 409 and "configuration changed" in response.json()["detail"]
    source = runtime.store.get(workspace_id=workspace, record_id="source:schema-data").value
    assert source["name"] == "Operator changed this" and source["allowed_tables"] == []
    assert runtime.store.get(workspace_id=workspace, record_id="schema:schema-data").value == before
    assert client.get("/api/sources/schema-data/schema", headers=headers).json()["tables"] == []


def test_inspection_and_saved_schema_honor_current_deployment_scope(schema_api, monkeypatch):
    client, runtime, executor, snapshot, headers = schema_api
    module = import_module("opsgraph.api.app")
    monkeypatch.setattr(module, "authorize", lambda *args: Obligation(allowed_schemas=("private",)))
    saved = client.get("/api/sources/schema-data/schema", headers=headers).json()
    assert saved["tables"] == [] and saved["status"] == "stale"
    response = client.post("/api/sources/schema-data/inspect", headers=headers)
    assert response.status_code == 403 and "deployment policy" in response.json()["detail"]
    assert executor.calls == []
    source = runtime.store.get(
        workspace_id=runtime.settings.workspace_id, record_id="source:schema-data"
    )
    assert source.value["status"] == "stale"


def test_policy_change_during_inspection_prevents_ready_state(schema_api, monkeypatch):
    client, runtime, executor, snapshot, headers = schema_api
    module = import_module("opsgraph.api.app")
    policy = [Obligation()]
    monkeypatch.setattr(module, "authorize", lambda *args: policy[0])

    def change_policy(self, **kwargs):
        policy[0] = Obligation(max_rows=1)
        return snapshot

    monkeypatch.setattr(executor, "discover_snapshot", change_policy)
    response = client.post("/api/sources/schema-data/inspect", headers=headers)
    assert response.status_code == 409 and "policy changed" in response.json()["detail"]
    source = runtime.store.get(
        workspace_id=runtime.settings.workspace_id, record_id="source:schema-data"
    )
    assert source.value["status"] == "stale"
