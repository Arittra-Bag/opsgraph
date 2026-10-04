import sqlite3
import threading
import time

import pytest

from opsgraph.orchestration.coordinator import RunCoordinator
from opsgraph.persistence.runs import RunSchemaMigrationError, RunStore

BODY = {"question": "What happened to the records?", "source_id": "local-data"}


def test_run_schema_version_is_initialized_once_and_reopened(tmp_path):
    path = tmp_path / "state.db"
    RunStore(path)
    RunStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM run_schema_version").fetchall() == [(1,)]


@pytest.mark.parametrize("versions", [(2,), (0,), (1, 2)])
def test_unknown_run_schema_versions_are_refused_without_mutation(tmp_path, versions):
    path = tmp_path / "private-state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE run_schema_version (version INTEGER PRIMARY KEY)")
        db.executemany(
            "INSERT INTO run_schema_version(version) VALUES (?)",
            [(version,) for version in versions],
        )
        db.execute("CREATE TABLE preserved (value TEXT)")
        db.execute("INSERT INTO preserved VALUES ('unchanged')")

    with pytest.raises(RunSchemaMigrationError) as caught:
        RunStore(path)

    message = str(caught.value)
    assert "private-state" not in message
    assert not any(str(version) in message for version in versions)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM run_schema_version ORDER BY version").fetchall() == [
            (version,) for version in versions
        ]
        assert db.execute("SELECT value FROM preserved").fetchone() == ("unchanged",)
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "runs" not in tables and "run_events" not in tables


def test_run_schema_initialization_rolls_back_as_one_transaction(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE preserved (value TEXT)")
        db.execute("CREATE INDEX runs ON preserved(value)")

    with pytest.raises(RunSchemaMigrationError, match="could not be initialized safely"):
        RunStore(path)

    with sqlite3.connect(path) as db:
        objects = db.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name IN ('runs', 'run_events', 'run_schema_version') ORDER BY name"
        ).fetchall()
        assert objects == [("index", "runs")]


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


def test_exception_handler_storage_failure_defers_running_run_to_restart_recovery(tmp_path):
    path = tmp_path / "state.db"
    fail_reads = threading.Event()

    class FailingReadStore(RunStore):
        def get(self, workspace, run_id):
            if fail_reads.is_set():
                raise sqlite3.OperationalError("private-diagnostic-must-not-leak")
            return super().get(workspace, run_id)

    def execute(*_):
        fail_reads.set()
        raise RuntimeError("execution failed")

    store = FailingReadStore(path)
    run = store.create("alpha", BODY)
    coordinator = RunCoordinator(store, execute, workspace_id="alpha")
    coordinator.start()
    coordinator.notify()
    try:
        coordinator._thread.join(timeout=5)
        assert not coordinator._thread.is_alive()
        with pytest.raises(RuntimeError, match="Restart OpsGraph") as error:
            coordinator.start()
        assert "private-diagnostic" not in str(error.value)
        assert RunStore(path).get("alpha", run["id"])["status"] == "running"
    finally:
        coordinator.close()

    recovered_store = RunStore(path)
    recovered = RunCoordinator(recovered_store, lambda *_: None, workspace_id="alpha")
    recovered.start()
    try:
        assert recovered_store.get("alpha", run["id"])["status"] == "interrupted"
    finally:
        recovered.close()


def test_cancel_racing_failure_finishes_before_terminal_callback(tmp_path):
    finished = threading.Event()
    notified = []

    class RacingStore(RunStore):
        def update(self, workspace, run_id, event_type, **changes):
            if changes.get("status") == "failed":
                self.cancel(workspace, run_id)
            return super().update(workspace, run_id, event_type, **changes)

    store = RacingStore(tmp_path / "state.db")

    def fail(*args):
        raise ValueError("fixture failure")

    def terminal(workspace, run_id, status, code):
        notified.append(status)
        assert store.get(workspace, run_id)["status"] == "cancelled"
        finished.set()

    coordinator = RunCoordinator(store, fail, workspace_id="alpha", on_terminal=terminal)
    store.create("alpha", BODY)
    coordinator.start()
    try:
        assert finished.wait(5)
        assert notified == ["cancelled"]
    finally:
        coordinator.close()


def test_followups_and_retries_keep_conversation_and_retry_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("opsgraph.persistence.runs.timestamp", lambda: "2026-10-04T12:00:00Z")
    store = RunStore(tmp_path / "state.db")
    root = store.create("alpha", BODY)
    store.cancel("alpha", root["id"])
    follow = store.create("alpha", {**BODY, "parent_run_id": root["id"]})
    store.cancel("alpha", follow["id"])
    monkeypatch.setattr("opsgraph.persistence.runs.timestamp", lambda: "2026-10-04T12:01:00Z")
    retry = store.create("alpha", {**BODY, "parent_run_id": root["id"]}, retry_of=follow["id"])
    assert root["conversation_id"] == follow["conversation_id"] == retry["conversation_id"]
    assert root["turn_id"] != follow["turn_id"] == retry["turn_id"]
    detail = store.conversation("alpha", root["conversation_id"])
    assert len(detail["turns"]) == 2
    assert detail["turns"][1]["turn_created_at"] == follow["created_at"]
    assert detail["turns"][1]["turn_created_at"] != retry["created_at"]
    assert [r["id"] for r in detail["turns"][1]["attempts"]] == [follow["id"], retry["id"]]
    assert len(store.conversations("alpha")) == 1
    with pytest.raises(KeyError):
        store.conversation("beta", root["conversation_id"])


def test_conversation_implicit_parent_idempotency_and_source_boundary(tmp_path):
    store = RunStore(tmp_path / "state.db")
    root = store.create("alpha", BODY)
    store.cancel("alpha", root["id"])
    request = {**BODY, "conversation_id": root["conversation_id"], "request_id": "turn-request"}
    follow = store.create("alpha", request)
    assert follow["parent_run_id"] == root["id"]
    assert store.create("alpha", request)["id"] == follow["id"]
    store.cancel("alpha", follow["id"])
    later = store.create("alpha", {**BODY, "conversation_id": root["conversation_id"]})
    assert later["parent_run_id"] == follow["id"]
    assert store.create("alpha", request)["id"] == follow["id"]
    with pytest.raises(ValueError, match="source"):
        store.create("alpha", {**request, "request_id": None, "source_id": "other-source"})
    with pytest.raises(ValueError, match="not found"):
        store.create("beta", {**request, "request_id": None})


def test_same_conversation_concurrent_admission_keeps_one_active_turn(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = RunStore(tmp_path / "state.db")
    root = store.create("alpha", BODY)
    store.cancel("alpha", root["id"])

    def create(_):
        try:
            return store.create("alpha", {**BODY, "conversation_id": root["conversation_id"]})
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(16)))
    assert sum(result is not None for result in results) == 1
    assert store.conversation("alpha", root["conversation_id"])["turn_count"] == 2


def test_historical_backfill_preserves_payloads_events_and_captures(tmp_path):
    import json

    path = tmp_path / "state.db"
    store = RunStore(path)
    root = store.create("alpha", BODY)
    store.cancel("alpha", root["id"])
    follow = store.create("alpha", {**BODY, "parent_run_id": root["id"]})
    store.cancel("alpha", follow["id"])
    retry = store.create("alpha", {**BODY, "parent_run_id": root["id"]}, retry_of=follow["id"])
    with store.connect() as db:
        for run in (root, follow, retry):
            value = json.loads(
                db.execute("SELECT value FROM runs WHERE id=?", (run["id"],)).fetchone()[0]
            )
            value.pop("conversation_id")
            value.pop("turn_id")
            value["evidence"] = [{"evidence_hash": "preserved", "rows": [[7]]}]
            db.execute("UPDATE runs SET value=? WHERE id=?", (json.dumps(value), run["id"]))
        before = db.execute("SELECT value FROM runs ORDER BY rowid").fetchall()
        events = db.execute("SELECT value FROM run_events ORDER BY rowid").fetchall()
        db.execute("DROP TABLE conversation_runs")
        db.execute("DROP TABLE conversations")
    restored = RunStore(path)
    assert len(restored.conversations("alpha")) == 1
    assert restored.conversations("alpha")[0]["turn_count"] == 2
    with restored.connect() as db:
        assert db.execute("SELECT value FROM runs ORDER BY rowid").fetchall() == before
        assert db.execute("SELECT value FROM run_events ORDER BY rowid").fetchall() == events
    assert RunStore(path).conversations("alpha") == restored.conversations("alpha")


def test_parallel_repeated_turn_request_is_exactly_once_across_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "state.db"
    store = RunStore(path)
    root = store.create("alpha", BODY)
    store.cancel("alpha", root["id"])
    request = {**BODY, "conversation_id": root["conversation_id"], "request_id": "shared-turn"}
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.create("alpha", request), range(128)))
    assert len({value["id"] for value in results}) == 1
    reopened = RunStore(path)
    assert reopened.create("alpha", request)["id"] == results[0]["id"]
    assert reopened.conversation("alpha", root["conversation_id"])["turn_count"] == 2


def test_backfill_deep_reverse_order_history_and_cycles_are_safe(tmp_path):
    import json

    path = tmp_path / "state.db"
    store = RunStore(path)
    with store.connect() as db:
        for index in range(1500):
            value = {
                "id": f"old-{index}",
                "question": "Historical question",
                "source_id": "local-data",
                "status": "completed",
                "parent_run_id": f"old-{index + 1}" if index < 1499 else None,
                "evidence": [{"evidence_hash": "unchanged"}],
            }
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                ("alpha", value["id"], None, "completed", json.dumps(value)),
            )
        for run_id, parent in (
            ("cycle-a", "cycle-b"),
            ("cycle-b", "cycle-a"),
            ("orphan", "absent"),
        ):
            value = {
                "id": run_id,
                "question": "Historical question",
                "source_id": "local-data",
                "status": "completed",
                "parent_run_id": parent,
            }
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                ("beta", run_id, None, "completed", json.dumps(value)),
            )
    migrated = RunStore(path)
    assert len(migrated.conversations("alpha")) == 1
    assert migrated.conversations("alpha")[0]["turn_count"] == 1500
    assert len(migrated.conversations("beta")) == 2
    assert migrated.get("alpha", "old-0")["evidence"] == [{"evidence_hash": "unchanged"}]
    assert migrated.conversations("beta") == RunStore(path).conversations("beta")
