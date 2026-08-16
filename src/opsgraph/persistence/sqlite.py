"""Durable, workspace-scoped local control store.

The target database is never used as OpsGraph's control database. This store
contains metadata and investigation artifacts only; connector secrets remain
outside it and are represented by secret-reference names.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from opsgraph.persistence.workspace import WorkspaceRecord


class SQLiteWorkspaceStore:
    """Small durable store with strict workspace isolation."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_records (
                    workspace_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (workspace_id, record_id)
                )
                """
            )

    def put(self, record: WorkspaceRecord) -> None:
        workspace_id, record_id = self._key(record.workspace_id, record.record_id)
        payload = json.dumps(record.value, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspace_records (workspace_id, record_id, value_json)
                VALUES (?, ?, ?)
                ON CONFLICT(workspace_id, record_id) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (workspace_id, record_id, payload),
            )

    def get(self, *, workspace_id: str, record_id: str) -> WorkspaceRecord:
        workspace_id, record_id = self._key(workspace_id, record_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM workspace_records WHERE workspace_id = ? AND record_id = ?",
                (workspace_id, record_id),
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValueError("stored workspace record must be a JSON object")
        return WorkspaceRecord(workspace_id, record_id, value)

    def list(self, *, workspace_id: str) -> tuple[WorkspaceRecord, ...]:
        self._validate_component(workspace_id, "workspace_id")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT record_id, value_json FROM workspace_records "
                "WHERE workspace_id = ? ORDER BY record_id",
                (workspace_id,),
            ).fetchall()
        records: list[WorkspaceRecord] = []
        for record_id, payload in rows:
            value: Any = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("stored workspace record must be a JSON object")
            records.append(WorkspaceRecord(workspace_id, record_id, value))
        return tuple(records)

    def delete(self, *, workspace_id: str, record_id: str) -> None:
        workspace_id, record_id = self._key(workspace_id, record_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM workspace_records WHERE workspace_id = ? AND record_id = ?",
                (workspace_id, record_id),
            )

    @classmethod
    def _key(cls, workspace_id: str, record_id: str) -> tuple[str, str]:
        cls._validate_component(workspace_id, "workspace_id")
        cls._validate_component(record_id, "record_id")
        return workspace_id, record_id

    @staticmethod
    def _validate_component(value: str, name: str) -> None:
        if not value or len(value) > 128:
            raise ValueError(f"{name} must contain 1-128 characters")
