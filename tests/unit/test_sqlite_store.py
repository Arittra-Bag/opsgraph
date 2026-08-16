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
