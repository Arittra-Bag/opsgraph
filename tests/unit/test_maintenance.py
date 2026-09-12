import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from opsgraph import maintenance
from opsgraph.config import Settings, StatePathError
from opsgraph.setup import ensure_private_directory, read_private_config, write_private_config


@pytest.fixture
def workspace(tmp_path):
    directory = ensure_private_directory(tmp_path / "workspace")
    state_dir = ensure_private_directory(directory / ".opsgraph")
    state = state_dir / "state.db"
    with sqlite3.connect(state) as connection:
        connection.execute("CREATE TABLE history (id TEXT PRIMARY KEY, answer TEXT)")
        connection.execute("INSERT INTO history VALUES ('run-1', 'preserved evidence')")
    write_private_config(
        directory / ".env",
        {
            "OPSGRAPH_API_KEY": "test-only-workspace-key-private-to-fixture",
            "OPSGRAPH_SOURCE_DSN": "host=127.0.0.1 password='hidden${LITERAL}'",
            "OPSGRAPH_STATE_PATH": str(state),
            "CUSTOM_SETTING": "literal\\value'\nsecond line",
            "UNSET_CUSTOM_SETTING": None,
        },
    )
    return directory


@pytest.fixture
def saved_backup(workspace, tmp_path):
    destination = tmp_path / "backup"
    maintenance.backup(workspace, destination)
    return destination


def rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM history").fetchall()


def rewrite_manifest(directory, mutate):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    mutate(manifest)
    path.write_text(json.dumps(manifest))


def test_stopped_backup_preserves_history_credentials_and_original(workspace, tmp_path):
    config = (workspace / ".env").read_bytes()
    state = workspace / ".opsgraph" / "state.db"
    original_rows = rows(state)
    destination = tmp_path / "saved"
    maintenance.backup(workspace, destination)
    assert (destination / ".env").read_bytes() == config
    assert rows(destination / "state.db") == original_rows
    assert (workspace / ".env").read_bytes() == config
    assert rows(state) == original_rows
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["contains_credentials_and_evidence"] is True
    assert manifest["files"] == {
        name: maintenance.digest(destination / name) for name in (".env", "state.db")
    }
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        for name in (".env", "state.db", "manifest.json"):
            assert stat.S_IMODE((destination / name).stat().st_mode) == 0o600


def test_backup_rejects_a_live_coordinator_without_creating_output(workspace, tmp_path):
    guard_path = workspace / ".opsgraph" / "state.db.coordinator"
    with sqlite3.connect(guard_path) as guard:
        guard.execute("BEGIN EXCLUSIVE")
        with pytest.raises(maintenance.MaintenanceError, match="Stop OpsGraph"):
            maintenance.backup(workspace, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


def test_restore_uses_a_new_directory_and_relocates_only_state_path(saved_backup, tmp_path):
    destination = tmp_path / "restored"
    original_files = {path.name: path.read_bytes() for path in saved_backup.iterdir()}
    original_values = read_private_config(saved_backup / ".env")
    maintenance.restore(saved_backup, destination)
    restored = read_private_config(destination / ".env")
    state = destination / ".opsgraph" / "state.db"
    assert restored == {**original_values, "OPSGRAPH_STATE_PATH": str(state)}
    assert rows(state) == [("run-1", "preserved evidence")]
    assert original_files == {path.name: path.read_bytes() for path in saved_backup.iterdir()}
    if os.name == "posix":
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_existing_destination_is_never_overwritten(operation, workspace, saved_backup, tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("original")
    action = maintenance.backup if operation == "backup" else maintenance.restore
    source = workspace if operation == "backup" else saved_backup
    with pytest.raises(maintenance.MaintenanceError, match="NEW destination"):
        action(source, destination)
    assert list(destination.iterdir()) == [sentinel]
    assert sentinel.read_text() == "original"


@pytest.mark.parametrize("payload", [None, [], "private-token-not-for-output", 7, True, {}])
def test_nonobject_or_incomplete_manifest_fails_safely(saved_backup, tmp_path, payload, capsys):
    (saved_backup / "manifest.json").write_text(json.dumps(payload))
    destination = tmp_path / "restored"
    assert maintenance.run_maintenance("restore", destination, saved_backup) == 2
    output = capsys.readouterr().out
    assert "manifest" in output
    assert "private-token-not-for-output" not in output
    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", True),
        ("format", "1"),
        ("format", 2),
        ("format", None),
        ("opsgraph_version", None),
        ("opsgraph_version", []),
        ("opsgraph_version", ""),
        ("opsgraph_version", "secret\nversion"),
        ("files", None),
        ("files", []),
        ("files", [".env", "state.db"]),
        ("files", {".env": None, "state.db": "a" * 64}),
        ("files", {".env": ["a" * 64], "state.db": "a" * 64}),
        ("files", {".env": "not-a-hash", "state.db": "a" * 64}),
        ("files", {"../private": "a" * 64, "state.db": "a" * 64}),
        ("contains_credentials_and_evidence", "true"),
        ("contains_credentials_and_evidence", 1),
        ("contains_credentials_and_evidence", False),
    ],
)
def test_manifest_field_shapes_are_validated_before_restore(saved_backup, tmp_path, field, value):
    rewrite_manifest(saved_backup, lambda manifest: manifest.update({field: value}))
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.restore(saved_backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "content",
    [
        b'{"format":1,"format":1}',
        b"not-json-private-secret",
        b"\xff\xfe",
        b" " * 16_385,
        b"[" * 2_000 + b"]" * 2_000,
    ],
)
def test_invalid_duplicate_large_or_deep_manifest_is_sanitized(
    saved_backup, tmp_path, content, capsys
):
    (saved_backup / "manifest.json").write_bytes(content)
    assert maintenance.run_maintenance("restore", tmp_path / "restored", saved_backup) == 2
    assert "private-secret" not in capsys.readouterr().out
    assert not (tmp_path / "restored").exists()


def test_checksum_failure_does_not_create_destination(saved_backup, tmp_path):
    with (saved_backup / ".env").open("a") as stream:
        stream.write("CHANGED=secret\n")
    with pytest.raises(maintenance.MaintenanceError, match="checksum mismatch"):
        maintenance.restore(saved_backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert "CHANGED=secret" in (saved_backup / ".env").read_text()


@pytest.mark.parametrize("content", [b"", b"invalid database private contents"])
def test_corrupt_database_fails_even_with_matching_manifest(saved_backup, tmp_path, content):
    (saved_backup / "state.db").write_bytes(content)
    rewrite_manifest(
        saved_backup,
        lambda manifest: manifest["files"].update(
            {"state.db": maintenance.digest(saved_backup / "state.db")}
        ),
    )
    with pytest.raises(maintenance.MaintenanceError, match="corrupt") as error:
        maintenance.restore(saved_backup, tmp_path / "restored")
    assert "private contents" not in str(error.value)
    assert not (tmp_path / "restored").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixtures")
@pytest.mark.parametrize("name", ["manifest.json", ".env", "state.db"])
def test_restore_rejects_symlinked_backup_members(saved_backup, tmp_path, name):
    member = saved_backup / name
    target = tmp_path / "original-member"
    member.rename(target)
    original = target.read_bytes()
    member.symlink_to(target)
    with pytest.raises(maintenance.MaintenanceError, match="symbolic links"):
        maintenance.restore(saved_backup, tmp_path / "restored")
    assert target.read_bytes() == original
    assert not (tmp_path / "restored").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixtures")
def test_source_and_destination_symlink_directories_are_rejected(saved_backup, tmp_path):
    link = tmp_path / "backup-link"
    link.symlink_to(saved_backup, target_is_directory=True)
    with pytest.raises(maintenance.MaintenanceError, match="symbolic links"):
        maintenance.restore(link, tmp_path / "restored")
    with pytest.raises(maintenance.MaintenanceError, match="symbolic links"):
        maintenance.restore(saved_backup, link / "nested-restore")
    assert not (saved_backup / "nested-restore").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixtures")
@pytest.mark.parametrize("name", ["state.db", "state.db.coordinator"])
def test_backup_rejects_linked_state_or_coordinator(workspace, tmp_path, name):
    item = workspace / ".opsgraph" / name
    target = tmp_path / "original-target"
    if item.exists():
        item.rename(target)
    else:
        target.write_bytes(b"keep")
    original = target.read_bytes()
    item.symlink_to(target)
    with pytest.raises(maintenance.MaintenanceError, match="symbolic links"):
        maintenance.backup(workspace, tmp_path / "blocked")
    assert target.read_bytes() == original
    assert not (tmp_path / "blocked").exists()


def test_failed_destination_write_keeps_original_and_partial_output(
    workspace, tmp_path, monkeypatch
):
    original = (workspace / ".env").read_bytes()
    destination = tmp_path / "partial"
    actual = maintenance.private_bytes

    def fail_state(path, content):
        if path.name == "state.db":
            raise OSError("simulated disk failure with private-secret")
        actual(path, content)

    monkeypatch.setattr(maintenance, "private_bytes", fail_state)
    assert maintenance.run_maintenance("backup", workspace, destination) == 2
    assert (workspace / ".env").read_bytes() == original
    assert (destination / ".env").read_bytes() == original
    assert not (destination / "manifest.json").exists()


def test_invalid_action_is_rejected_without_touching_directories(tmp_path, capsys):
    assert maintenance.run_maintenance("typo", tmp_path / "unused", tmp_path / "other") == 2
    assert "backup or restore" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = ensure_private_directory(tmp_path / "test-home")
    original = Path.expanduser

    def expanduser(path):
        if path.parts and path.parts[0] == "~":
            return home.joinpath(*path.parts[1:])
        return original(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    return home


@pytest.mark.parametrize("spelling", ["relative", "tilde", "absolute"])
def test_backup_restore_keeps_workspace_metadata_and_run_history_in_one_database(
    tmp_path, monkeypatch, isolated_home, spelling
):
    from opsgraph.persistence import SQLiteWorkspaceStore, WorkspaceRecord
    from opsgraph.persistence.runs import RunStore

    directory = ensure_private_directory(tmp_path / "workspace")
    configured = {
        "relative": ".opsgraph/state.db",
        "tilde": "~/state.db",
        "absolute": str(directory / ".opsgraph/state.db"),
    }[spelling]
    write_private_config(directory / ".env", {"OPSGRAPH_STATE_PATH": configured})
    monkeypatch.chdir(directory)
    settings = Settings(state_path=configured, _env_file=None)
    metadata = SQLiteWorkspaceStore(settings.state_path)
    record = WorkspaceRecord("local", "source:test", {"record_type": "source", "id": "test"})
    metadata.put(record)
    runs = RunStore(settings.state_path)
    run = runs.create("local", {"source_id": "test", "question": "A temporary fixture question"})
    run = runs.update(
        "local", run["id"], "completed", status="completed", answer={"summary": "Saved history"}
    )
    events = runs.events("local", run["id"], after=0)
    unrelated = ensure_private_directory(tmp_path / "different-cwd")
    monkeypatch.chdir(unrelated)
    saved = tmp_path / "backup"
    restored = tmp_path / "restored"
    maintenance.backup(directory, saved)
    with sqlite3.connect(saved / "state.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_records").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone() == (len(events),)
    maintenance.restore(saved, restored)
    restored_settings = Settings(**read_private_config(restored / ".env"), _env_file=None)
    restored_metadata = SQLiteWorkspaceStore(restored_settings.state_path)
    restored_runs = RunStore(restored_settings.state_path)
    assert restored_metadata.get(workspace_id="local", record_id="source:test") == record
    assert restored_runs.get("local", run["id"]) == run
    assert restored_runs.events("local", run["id"], after=0) == events
    assert restored_settings.state_path == restored / ".opsgraph/state.db"
    assert not (directory / "~").exists()


def test_tilde_backup_checks_the_same_live_coordinator_lock(tmp_path, monkeypatch, isolated_home):
    directory = ensure_private_directory(tmp_path / "workspace")
    state = isolated_home / "state.db"
    with sqlite3.connect(state) as connection:
        connection.execute("CREATE TABLE history (id INTEGER)")
    write_private_config(directory / ".env", {"OPSGRAPH_STATE_PATH": "~/state.db"})
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect(Path(str(state) + ".coordinator")) as guard:
        guard.execute("BEGIN EXCLUSIVE")
        with pytest.raises(maintenance.MaintenanceError, match="Stop OpsGraph"):
            maintenance.backup(directory, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()
    assert not (directory / "~").exists()


def test_backup_refuses_legacy_split_tilde_state_without_copying_either_database(
    tmp_path, isolated_home, capsys
):
    directory = ensure_private_directory(tmp_path / "workspace")
    literal = directory / "~" / "state.db"
    literal.parent.mkdir()
    literal.write_bytes(b"original workspace metadata")
    expanded = isolated_home / "state.db"
    expanded.write_bytes(b"original run history")
    write_private_config(directory / ".env", {"OPSGRAPH_STATE_PATH": "~/state.db"})
    with pytest.raises(StatePathError, match="Legacy tilde state path"):
        maintenance.backup(directory, tmp_path / "blocked")
    assert maintenance.run_maintenance("backup", directory, tmp_path / "blocked") == 2
    assert "Reconcile metadata and run history" in capsys.readouterr().out
    assert not (tmp_path / "blocked").exists()
    assert literal.read_bytes() == b"original workspace metadata"
    assert expanded.read_bytes() == b"original run history"


def test_backup_restore_preserves_optional_provider_settings(workspace, tmp_path):
    provider_path = workspace / ".opsgraph/state.db.provider/settings.env"
    values = {"PROVIDER_CONFIG": '{"model":"model-one"}', "PROVIDER_KEY": "test-private-key"}
    write_private_config(provider_path, values)
    destination = tmp_path / "provider-backup"
    maintenance.backup(workspace, destination)
    assert "provider.env" in json.loads((destination / "manifest.json").read_text())["files"]
    restored = tmp_path / "provider-restored"
    maintenance.restore(destination, restored)
    assert read_private_config(restored / ".opsgraph/state.db.provider/settings.env") == values
