import json
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opsgraph.api.app as app_module
from opsgraph.audit import SQLiteAuditChain
from opsgraph.orchestration.coordinator import RunCoordinator
from opsgraph.persistence import SQLiteWorkspaceStore
from opsgraph.persistence.runs import RunStore


class HealthyCoordinator:
    def health(self):
        return {
            "status": "ready",
            "detail": "Investigation worker and state ownership are active.",
        }


def service_for(store, coordinator=None):
    return SimpleNamespace(store=store, coordinator=coordinator or HealthyCoordinator())


def full_store(path):
    SQLiteWorkspaceStore(path)
    SQLiteAuditChain(path)
    return RunStore(path)


def test_service_readiness_checks_local_state_without_provider_or_source(tmp_path):
    service = service_for(full_store(tmp_path / "state.db"))

    ready, components = app_module._service_readiness(service)

    assert ready is True
    assert {name: value["status"] for name, value in components.items()} == {
        "state_database": "ready",
        "run_schema": "ready",
        "audit_chain": "ready",
        "coordinator": "ready",
    }


@pytest.mark.parametrize("details", ['{"changed":true}', "not-json"])
def test_service_readiness_rejects_a_corrupt_audit_chain(tmp_path, details):
    store = full_store(tmp_path / "state.db")
    audit = SQLiteAuditChain(store.path)
    audit.append(
        workspace_id="alpha",
        actor="tester",
        action="test.append",
        resource="one",
        outcome="allowed",
        details={"original": True},
    )
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE audit_entries SET details_json=? WHERE sequence=1", (details,))

    ready, components = app_module._service_readiness(service_for(store))

    assert ready is False
    assert components["state_database"]["status"] == "ready"
    assert components["run_schema"]["status"] == "ready"
    assert components["audit_chain"] == {
        "status": "corrupt",
        "detail": "Local audit integrity requires operator attention.",
    }


def test_service_readiness_rejects_an_unsupported_run_schema(tmp_path):
    store = full_store(tmp_path / "state.db")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE run_schema_version SET version=2")

    ready, components = app_module._service_readiness(service_for(store))

    assert ready is False
    assert components["state_database"]["status"] == "ready"
    assert components["run_schema"] == {
        "status": "unsupported",
        "detail": "Local state schema requires operator attention.",
    }


def test_service_readiness_rejects_a_read_only_state_database(tmp_path):
    store = full_store(tmp_path / "state.db")

    class ReadOnlyStore:
        @contextmanager
        def connect(self):
            db = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
            try:
                yield db
            finally:
                db.close()

    ready, components = app_module._service_readiness(service_for(ReadOnlyStore()))

    assert ready is False
    assert components["state_database"]["status"] == "unavailable"
    assert components["run_schema"]["status"] == "ready"


def test_ready_endpoint_returns_component_status_without_authentication(monkeypatch, tmp_path):
    service = service_for(full_store(tmp_path / "state.db"))
    monkeypatch.setattr(app_module, "run_api", service)
    client = TestClient(app_module.app)

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["components"]["coordinator"]["status"] == "ready"

    service.coordinator = SimpleNamespace(
        health=lambda: {
            "status": "unavailable",
            "detail": "Investigation worker or state ownership is unavailable.",
        }
    )
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert "detail" not in response.json()


@pytest.mark.parametrize("table", ["audit_entries", "workspace_records"])
def test_service_readiness_requires_all_persistent_state_tables(tmp_path, table):
    store = full_store(tmp_path / "state.db")
    with sqlite3.connect(store.path) as db:
        db.execute(f'DROP TABLE "{table}"')

    ready, components = app_module._service_readiness(service_for(store))

    assert ready is False
    assert components["run_schema"]["status"] == "unsupported"


def test_service_readiness_rejects_structurally_incompatible_state_table(tmp_path):
    store = full_store(tmp_path / "state.db")
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE workspace_records RENAME TO workspace_records_old")
        db.execute("CREATE TABLE workspace_records (workspace_id TEXT)")

    ready, components = app_module._service_readiness(service_for(store))

    assert ready is False
    assert components["run_schema"]["status"] == "unsupported"


def test_ready_endpoint_tracks_the_application_lifespan():
    assert app_module.run_api.coordinator.health()["status"] == "unavailable"
    with TestClient(app_module.app) as client:
        response = client.get("/api/ready")
        assert response.status_code == 200
        assert response.json()["ready"] is True
    assert app_module.run_api.coordinator.health()["status"] == "unavailable"


def test_coordinator_health_and_terminal_callback_are_process_local(tmp_path):
    store = RunStore(tmp_path / "state.db")
    terminal = []

    def execute(workspace, run, observe, check, coordinator):
        return {"answer": {}, "plan": {}}

    coordinator = RunCoordinator(
        store,
        execute,
        workspace_id="alpha",
        on_terminal=lambda *event: terminal.append(event),
    )
    assert coordinator.health()["status"] == "unavailable"
    coordinator.start()
    try:
        assert coordinator.health()["status"] == "ready"
        run = store.create(
            "alpha",
            {"question": "What happened to these records?", "source_id": "local-data"},
        )
        coordinator.notify()
        for _ in range(200):
            if terminal:
                break
            time.sleep(0.01)
        assert terminal == [("alpha", run["id"], "completed", None)]
    finally:
        coordinator.close()
    assert coordinator.health()["status"] == "unavailable"


def test_terminal_callback_exposes_a_code_without_exception_text(tmp_path):
    store = RunStore(tmp_path / "state.db")
    terminal = []

    def fail(*_):
        raise RuntimeError("private provider response must not leave the worker")

    coordinator = RunCoordinator(
        store,
        fail,
        workspace_id="alpha",
        on_terminal=lambda *event: terminal.append(event),
    )
    coordinator.start()
    try:
        run = store.create(
            "alpha",
            {"question": "What happened to these records?", "source_id": "local-data"},
        )
        coordinator.notify()
        for _ in range(200):
            if terminal:
                break
            time.sleep(0.01)
        assert terminal == [("alpha", run["id"], "failed", "failed")]
        assert "private" not in json.dumps(terminal)
    finally:
        coordinator.close()


def test_terminal_audit_failure_stops_worker_and_replays_once(tmp_path):
    store = RunStore(tmp_path / "state.db")
    replayed = []

    def execute(workspace, run, observe, check, coordinator):
        return {"answer": {}, "plan": {}}

    def unavailable(*_):
        raise OSError("private audit path must not escape")

    coordinator = RunCoordinator(
        store,
        execute,
        workspace_id="alpha",
        on_terminal=unavailable,
    )
    coordinator.start()
    run = store.create(
        "alpha", {"question": "What happened to these records?", "source_id": "local-data"}
    )
    coordinator.notify()
    for _ in range(200):
        if store.get("alpha", run["id"])["status"] == "completed" and (
            coordinator.health()["status"] == "unavailable"
        ):
            break
        time.sleep(0.01)
    assert coordinator.health()["status"] == "unavailable"
    assert store.get("alpha", run["id"])["terminal_audited"] is False
    coordinator.close()

    recovered = RunCoordinator(
        store,
        execute,
        workspace_id="alpha",
        on_terminal=lambda *event: replayed.append(event),
    )
    recovered.start()
    recovered.close()
    assert replayed == [("alpha", run["id"], "completed", None)]
    assert store.get("alpha", run["id"])["terminal_audited"] is True

    reopened = RunCoordinator(
        store,
        execute,
        workspace_id="alpha",
        on_terminal=lambda *event: replayed.append(event),
    )
    reopened.start()
    reopened.close()
    assert replayed == [("alpha", run["id"], "completed", None)]


def test_startup_recovery_records_interrupted_terminal_transition(tmp_path):
    store = RunStore(tmp_path / "state.db")
    run = store.create(
        "alpha", {"question": "What happened to these records?", "source_id": "local-data"}
    )
    assert store.claim("alpha")[1]["status"] == "running"
    terminal = []
    coordinator = RunCoordinator(
        store,
        lambda *_: None,
        workspace_id="alpha",
        on_terminal=lambda *event: terminal.append(event),
    )

    coordinator.start()
    coordinator.close()

    assert terminal == [("alpha", run["id"], "interrupted", "backend_interrupted")]
    assert store.get("alpha", run["id"])["terminal_audited"] is True


def test_projection_failure_after_terminal_audit_stops_worker(tmp_path):
    store = RunStore(tmp_path / "state.db")
    terminal = []

    def projection_failure(*_):
        raise OSError("private workspace path must not escape")

    coordinator = RunCoordinator(
        store,
        lambda *_: {"answer": {}, "plan": {}},
        projection_failure,
        workspace_id="alpha",
        on_terminal=lambda *event: terminal.append(event),
    )
    coordinator.start()
    try:
        run = store.create(
            "alpha",
            {"question": "What happened to these records?", "source_id": "local-data"},
        )
        coordinator.notify()
        for _ in range(200):
            if coordinator.health()["status"] == "unavailable":
                break
            time.sleep(0.01)
        assert terminal == [("alpha", run["id"], "completed", None)]
        assert coordinator.health()["status"] == "unavailable"
    finally:
        coordinator.close()


def test_container_healthcheck_uses_readiness_contract():
    dockerfile = app_module.runtime.settings.web_root.parents[2] / "deploy/container/Dockerfile"
    content = dockerfile.read_text()
    assert "/api/ready" in content
    assert "/api/health" not in content
