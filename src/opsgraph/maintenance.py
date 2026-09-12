"""Explicit offline backups and restore into a new private workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path

from opsgraph import __version__
from opsgraph.config import StatePathError, resolve_state_path


class MaintenanceError(ValueError):
    """A sanitized, actionable backup or restore error."""


def _safe_path(path: Path, *, directory: bool = False) -> None:
    """Reject links and nonregular files before reading credentials or evidence."""
    for parent in path.parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MaintenanceError("Choose a local path without symbolic links or reparse points.")
        if not stat.S_ISDIR(info.st_mode):
            raise MaintenanceError("Choose an existing local directory for this operation.")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise MaintenanceError("Choose a local path without symbolic links or reparse points.")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise MaintenanceError("Backup inputs must be regular local files in a local directory.")
    if not directory and info.st_nlink != 1:
        raise MaintenanceError("Backup inputs must not have multiple hard links.")


def _new_destination(path: Path) -> None:
    _safe_path(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        raise MaintenanceError("Choose a NEW destination directory; existing paths are preserved.")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MaintenanceError("Backup manifest has duplicate fields. Use a complete backup.")
        result[key] = value
    return result


def _read_manifest(path: Path) -> dict:
    _safe_path(path)
    # Manifests contain only two hashes and format metadata, never evidence rows.
    with path.open("rb") as stream:
        content = stream.read(16_385)
    if len(content) > 16_384:
        raise MaintenanceError("Backup manifest is too large. Use a complete OpsGraph backup.")
    try:
        manifest = json.loads(content, object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MaintenanceError(
            "Backup manifest is invalid JSON. Use a complete OpsGraph backup."
        ) from None
    required = {"format", "opsgraph_version", "files", "contains_credentials_and_evidence"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise MaintenanceError(
            "Backup manifest has missing or unexpected fields. Use a complete backup."
        )
    if type(manifest["format"]) is not int or manifest["format"] != 1:
        raise MaintenanceError("Unsupported backup format. Use a compatible OpsGraph version.")
    version = manifest["opsgraph_version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+!-]{0,63}", version):
        raise MaintenanceError("Backup manifest has an invalid version. Use a complete backup.")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {".env", "state.db"}:
        raise MaintenanceError(
            "Backup manifest must describe exactly the configuration and state files."
        )
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in files.values()
    ):
        raise MaintenanceError("Backup manifest has invalid checksums. Use a complete backup.")
    if manifest["contains_credentials_and_evidence"] is not True:
        raise MaintenanceError("Backup manifest is missing its sensitive-content marker.")
    return manifest


def _check_database(path: Path) -> None:
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise MaintenanceError(
                "Backup database is empty or corrupt. Use another complete backup."
            )
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MaintenanceError(
                    "Backup database integrity failed. Use another complete backup."
                )
    except sqlite3.Error:
        raise MaintenanceError(
            "Backup database is unreadable or corrupt. Use another complete backup."
        ) from None


@contextmanager
def stopped_state(path: Path):
    """Coordinate with the same lifetime lock used by the running application."""
    canonical = resolve_state_path(path)
    original = Path(path).expanduser()
    if not original.is_absolute():
        original = Path.cwd() / original
    path = canonical
    _safe_path(original)
    guard_path = Path(str(path) + ".coordinator")
    _safe_path(path)
    if guard_path.exists() or guard_path.is_symlink():
        _safe_path(guard_path)
    guard = sqlite3.connect(str(guard_path), timeout=0)
    try:
        guard.execute("BEGIN EXCLUSIVE")
        yield
    except sqlite3.OperationalError:
        raise MaintenanceError(
            "Stop OpsGraph before backup. Its state is busy or unavailable."
        ) from None
    finally:
        guard.rollback()
        guard.close()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def private_bytes(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)


def backup(directory: Path, destination: Path) -> None:
    from opsgraph.setup import read_private_config

    directory = directory.expanduser().absolute()
    _safe_path(directory, directory=True)
    config = directory / ".env"
    _safe_path(config)
    values = read_private_config(config)
    configured_state = values.get("OPSGRAPH_STATE_PATH") or ".opsgraph/state.db"
    state = resolve_state_path(configured_state, workspace=directory)
    # Preserve link rejection for the operator's spelling, then use the canonical
    # identity for both SQLite and the coordinator lock.
    original_state = Path(configured_state).expanduser()
    if not original_state.is_absolute():
        original_state = directory / original_state
    _safe_path(original_state)
    destination = destination.expanduser().absolute()
    _new_destination(destination)
    with stopped_state(state):
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        private_bytes(destination / ".env", config.read_bytes())
        saved = destination / "state.db"
        private_bytes(saved, b"")
        with closing(sqlite3.connect(state.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(saved)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise MaintenanceError("Backup integrity check failed; do not use this backup.")
        manifest = {
            "format": 1,
            "opsgraph_version": __version__,
            "files": {name: digest(destination / name) for name in (".env", "state.db")},
            "contains_credentials_and_evidence": True,
        }
        private_bytes(destination / "manifest.json", json.dumps(manifest, indent=2).encode())


def restore(source: Path, destination: Path) -> None:
    from opsgraph.setup import read_private_config, write_private_config

    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    _safe_path(source, directory=True)
    _new_destination(destination)
    manifest = _read_manifest(source / "manifest.json")
    for name, expected in manifest["files"].items():
        item = source / name
        _safe_path(item)
        if digest(item) != expected:
            raise MaintenanceError("Backup checksum mismatch. Use a complete, unchanged backup.")
    values = read_private_config(source / ".env")
    _check_database(source / "state.db")
    # Never replace a live or existing destination. Restore is reviewable before
    # the operator explicitly starts the new workspace.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    (destination / ".opsgraph").mkdir(mode=0o700)
    private_bytes(destination / ".opsgraph/state.db", (source / "state.db").read_bytes())
    values["OPSGRAPH_STATE_PATH"] = str(destination / ".opsgraph/state.db")
    write_private_config(destination / ".env", values)


def run_maintenance(action: str, directory: Path | None, other: Path) -> int:
    from opsgraph.setup import default_workspace_directory

    try:
        if action == "backup":
            backup(directory or default_workspace_directory(), other)
            print(
                "Private backup completed. It contains credentials and evidence; do not share it."
            )
        elif action == "restore":
            restore(other, directory or default_workspace_directory())
            print("Restored into a new workspace. Reopen history and inspect exports before use.")
        else:
            raise MaintenanceError("Choose either the backup or restore operation.")
        return 0
    except (MaintenanceError, StatePathError) as error:
        print(f"Operation failed. {error} Existing workspaces were not replaced.")
        return 2
    except (OSError, ValueError, sqlite3.Error):
        print(
            "Operation failed. Stop OpsGraph, verify the private backup/checksums, "
            "and choose a new writable destination. Existing workspaces were not replaced."
        )
        return 2
