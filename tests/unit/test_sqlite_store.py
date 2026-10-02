import sqlite3

import pytest

from opsgraph.persistence import SQLiteWorkspaceStore, WorkspaceRecord


def test_sqlite_store_is_durable_and_workspace_scoped(tmp_path):
    path = tmp_path / "opsgraph.db"
    first = SQLiteWorkspaceStore(path)
    first.put(WorkspaceRecord("workspace-a", "source:one", {"name": "Primary"}))
    first.put(WorkspaceRecord("workspace-b", "source:one", {"name": "Other"}))

    reopened = SQLiteWorkspaceStore(path)
    assert reopened.get(workspace_id="workspace-a", record_id="source:one").value == {
        "name": "Primary"
    }
    assert [record.value["name"] for record in reopened.list(workspace_id="workspace-b")] == [
        "Other"
    ]


def test_sqlite_store_upserts_without_crossing_workspace(tmp_path):
    store = SQLiteWorkspaceStore(tmp_path / "opsgraph.db")
    store.put(WorkspaceRecord("workspace-a", "skill:one", {"version": 1}))
    store.put(WorkspaceRecord("workspace-a", "skill:one", {"version": 2}))

    assert store.get(workspace_id="workspace-a", record_id="skill:one").value["version"] == 2
    assert len(store.list(workspace_id="workspace-a")) == 1

    store.delete(workspace_id="workspace-a", record_id="skill:one")
    assert store.list(workspace_id="workspace-a") == ()


def test_revision_guard_atomically_saves_source_and_schema(tmp_path):
    store = SQLiteWorkspaceStore(tmp_path / "opsgraph.db")
    source = WorkspaceRecord("workspace-a", "source:one", {"status": "configured"})
    store.put(source)
    records = (
        WorkspaceRecord("workspace-a", "source:one", {"status": "ready"}),
        WorkspaceRecord("workspace-a", "schema:one", {"fingerprint": "reviewed"}),
    )
    assert store.put_if_unchanged(source, records)
    assert store.get(workspace_id="workspace-a", record_id="source:one").value == {
        "status": "ready"
    }
    assert store.get(workspace_id="workspace-a", record_id="schema:one").value == {
        "fingerprint": "reviewed"
    }


def test_revision_guard_preserves_concurrent_edits_and_prevents_partial_schema_write(tmp_path):
    store = SQLiteWorkspaceStore(tmp_path / "opsgraph.db")
    previous = WorkspaceRecord("workspace-a", "source:one", {"tables": ["public.old"]})
    store.put(previous)
    latest = WorkspaceRecord("workspace-a", "source:one", {"tables": ["public.reviewed"]})
    store.put(latest)
    assert not store.put_if_unchanged(
        previous,
        (
            WorkspaceRecord("workspace-a", "source:one", {"status": "ready"}),
            WorkspaceRecord("workspace-a", "schema:one", {"tables": ["public.old"]}),
        ),
    )
    assert store.get(workspace_id="workspace-a", record_id="source:one") == latest
    with pytest.raises(KeyError):
        store.get(workspace_id="workspace-a", record_id="schema:one")


def test_revision_guard_rejects_cross_workspace_update_before_writing(tmp_path):
    store = SQLiteWorkspaceStore(tmp_path / "opsgraph.db")
    source = WorkspaceRecord("workspace-a", "source:one", {"status": "configured"})
    store.put(source)
    with pytest.raises(ValueError, match="one workspace"):
        store.put_if_unchanged(source, (WorkspaceRecord("workspace-b", "schema:one", {}),))
    assert store.get(workspace_id="workspace-a", record_id="source:one") == source
    assert store.list(workspace_id="workspace-b") == ()


@pytest.mark.parametrize("operation", ["write", "delete"])
def test_atomic_replace_rolls_back_published_record_when_any_statement_fails(tmp_path, operation):
    store = SQLiteWorkspaceStore(tmp_path / "opsgraph.db")
    draft = WorkspaceRecord("workspace-a", "skill-draft:one", {"version": "1.0.0"})
    published = WorkspaceRecord("workspace-a", "skill-published:one:1.0.0", {"version": "1.0.0"})
    store.put(draft)
    with sqlite3.connect(store.path) as db:
        if operation == "write":
            db.execute(
                "CREATE TRIGGER fail_atomic_write BEFORE INSERT ON workspace_records "
                "WHEN NEW.record_id LIKE 'skill-published:%' BEGIN "
                "SELECT RAISE(ABORT, 'injected write failure'); END"
            )
        else:
            db.execute(
                "CREATE TRIGGER fail_atomic_delete BEFORE DELETE ON workspace_records "
                "WHEN OLD.record_id LIKE 'skill-draft:%' BEGIN "
                "SELECT RAISE(ABORT, 'injected delete failure'); END"
            )

    with pytest.raises(sqlite3.IntegrityError):
        store.replace_if_unchanged(
            draft,
            (published,),
            delete_record_ids=(draft.record_id,),
        )

    assert store.get(workspace_id="workspace-a", record_id=draft.record_id) == draft
    with pytest.raises(KeyError):
        store.get(workspace_id="workspace-a", record_id=published.record_id)
