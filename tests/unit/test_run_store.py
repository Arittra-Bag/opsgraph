import threading
import time

import pytest

from opsgraph.orchestration.coordinator import RunCoordinator
from opsgraph.persistence.runs import RunStore

BODY = {"question": "What happened to the records?", "source_id": "local-data"}


def test_runs_persist_events_isolate_workspaces_and_deduplicate(tmp_path):
    store = RunStore(tmp_path / "state.db")
    first = store.create("alpha", {**BODY, "request_id": "retry-safe"})
    assert store.create("alpha", {**BODY, "request_id": "retry-safe"})["id"] == first["id"]
    with pytest.raises(ValueError, match="different request"):
        store.create(
            "alpha", {**BODY, "request_id": "retry-safe", "question": "A different question?"}
        )
    reopened = RunStore(store.path)
    assert reopened.get("alpha", first["id"]) == first
    assert reopened.events("alpha", first["id"], 0)[0]["type"] == "queued"
    with pytest.raises(KeyError):
        reopened.get("beta", first["id"])


def test_recovery_preserves_queued_and_partial_captures(tmp_path):
    store = RunStore(tmp_path / "state.db")
    active = store.create("alpha", BODY)
    queued = store.create("alpha", BODY)
    store.claim("alpha")
    artifact = {"evidence_hash": "hash", "rows": [[1]], "provenance": {"capture_id": "capture"}}
    store.update("alpha", active["id"], "evidence_captured", {"index": 0, "evidence": artifact})
    store.recover("alpha")
    recovered = store.get("alpha", active["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["partial_evidence"] is True
    assert recovered["evidence"] == [artifact]
    assert store.get("alpha", queued["id"])["status"] == "queued"
    captured_event = store.events("alpha", active["id"], 0)[2]
    assert captured_event["data"] == {"index": 0, "evidence_hash": "hash", "capture_id": "capture"}
    assert store.events("alpha", active["id"], captured_event["id"])[0]["type"] == "interrupted"


def test_cancel_and_claim_serialized_terminal_cannot_be_overwritten(tmp_path):
    store = RunStore(tmp_path / "state.db")
    queued = store.create("alpha", BODY)
    assert store.cancel("alpha", queued["id"])["status"] == "cancelled"
    assert store.claim("alpha") is None
    active = store.create("alpha", BODY)
    store.claim("alpha")
    assert store.cancel("alpha", active["id"])["status"] == "cancelling"
    assert (
        store.update("alpha", active["id"], "completed", status="completed")["status"]
        == "cancelling"
    )
    assert (
        store.update("alpha", active["id"], "cancelled", status="cancelled")["status"]
        == "cancelled"
    )
    assert (
        store.update("alpha", active["id"], "completed", status="completed")["status"]
        == "cancelled"
    )


def test_only_one_coordinator_and_cancel_waits_for_operation_exit(tmp_path):
    store = RunStore(tmp_path / "state.db")
    entered, release = threading.Event(), threading.Event()

    def execute(workspace, run, observe, check, coordinator):
        entered.set()
        assert release.wait(5)
        check()
        return {"answer": {}, "plan": {}}

    first = RunCoordinator(store, execute, workspace_id="alpha")
    second = RunCoordinator(RunStore(store.path), execute, workspace_id="alpha")
    first.start()
    try:
        with pytest.raises(RuntimeError, match="Another OpsGraph"):
            second.start()
        run = store.create("alpha", BODY)
        first.notify()
        assert entered.wait(5)
        store.cancel("alpha", run["id"])
        assert store.get("alpha", run["id"])["status"] == "cancelling"
        release.set()
        for _ in range(100):
            if store.get("alpha", run["id"])["status"] == "cancelled":
                break
            time.sleep(0.01)
        assert store.get("alpha", run["id"])["status"] == "cancelled"
    finally:
        release.set()
        first.close()
    second.start()
    second.close()


def test_queue_capacity_counts_only_waiting_work(tmp_path):
    from opsgraph.persistence.runs import QueueFull

    store = RunStore(tmp_path / "state.db")
    first = store.create("alpha", BODY)
    store.claim("alpha")
    for _ in range(10):
        store.create("alpha", BODY)
    with pytest.raises(QueueFull):
        store.create("alpha", BODY)
    store.cancel("alpha", first["id"])
    assert store.get("alpha", first["id"])["status"] == "cancelling"


def test_legacy_import_is_idempotent_and_excludes_sample(tmp_path):
    from opsgraph.persistence import SQLiteWorkspaceStore, WorkspaceRecord

    path = tmp_path / "state.db"
    old = SQLiteWorkspaceStore(path)
    real = {
        "record_type": "investigation",
        "id": "inv-old",
        "source_id": "local-data",
        "question": "What was observed?",
        "skill_id": "generic-readonly",
        "plan": {},
        "evidence": [{"evidence_hash": "original-digest", "rows": [[7]]}],
        "answer": {},
    }
    old.put(WorkspaceRecord("alpha", "investigation:inv-old", real))
    old.put(
        WorkspaceRecord(
            "alpha",
            "investigation:inv-sample",
            {**real, "id": "inv-sample", "source_id": "sample-saas"},
        )
    )
    store = RunStore(path)
    imported = store.get("alpha", "inv-old")
    assert imported["legacy_provenance"] is True
    assert imported["created_at"] is None and imported["finished_at"] is None
    assert imported["evidence"] == real["evidence"]
    assert len(RunStore(path).list("alpha")) == 1
    assert len(store.events("alpha", "inv-old", 0)) == 1
    with pytest.raises(KeyError):
        store.get("alpha", "inv-sample")


def test_claim_and_recovery_are_scoped_to_runtime_workspace(tmp_path):
    store = RunStore(tmp_path / "state.db")
    other = store.create("old-workspace", BODY)
    assert store.claim("new-workspace") is None
    store.claim("old-workspace")
    store.recover("new-workspace")
    assert store.get("old-workspace", other["id"])["status"] == "running"
    store.recover("old-workspace")
    assert store.get("old-workspace", other["id"])["status"] == "interrupted"


def test_delayed_cancel_cannot_cancel_subsequent_run(tmp_path):
    calls = []
    coordinator = RunCoordinator(
        RunStore(tmp_path / "state.db"), lambda *args: None, workspace_id="alpha"
    )
    coordinator.bind_cancellation("alpha", "run-A", lambda: calls.append("A"))
    coordinator.bind_cancellation("alpha", "run-B", lambda: calls.append("B"))
    coordinator.request_cancel("alpha", "run-A")
    coordinator.request_cancel("other-workspace", "run-B")
    assert calls == []
    coordinator.request_cancel("alpha", "run-B")
    assert calls == ["B"]


def test_other_workspace_queue_does_not_consume_current_capacity(tmp_path):
    store = RunStore(tmp_path / "state.db")
    for _ in range(10):
        store.create("old-workspace", BODY)
    current = store.create("new-workspace", BODY)
    assert store.claim("new-workspace")[1]["id"] == current["id"]
    assert len(store.list("old-workspace")) == 10


def test_storage_failure_stops_admission_and_retains_owner_lock(tmp_path):
    import sqlite3

    failed = threading.Event()

    class FailingStore(RunStore):
        def claim(self, workspace):
            failed.set()
            raise sqlite3.OperationalError("private-diagnostic-must-not-leak")

    store = FailingStore(tmp_path / "state.db")
    queued = store.create("alpha", BODY)
    coordinator = RunCoordinator(store, lambda *args: None, workspace_id="alpha")
    competing = RunCoordinator(RunStore(store.path), lambda *args: None, workspace_id="alpha")
    coordinator.start()
    try:
        assert failed.wait(5)
        coordinator._thread.join(timeout=5)
        with pytest.raises(RuntimeError, match="Restart OpsGraph") as error:
            coordinator.start()
        assert "private-diagnostic" not in str(error.value)
        assert store.get("alpha", queued["id"])["status"] == "queued"
        with pytest.raises(RuntimeError, match="Another OpsGraph"):
            competing.start()
    finally:
        coordinator.close()
