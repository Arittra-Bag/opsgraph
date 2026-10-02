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
        with self._lock, self._connect() as connection:
            self.put_in_transaction(connection, record)

    def put_in_transaction(self, connection: sqlite3.Connection, record: WorkspaceRecord) -> None:
        """Save one record through a caller-owned transaction on this store."""

        self._require_store_connection(connection)
        workspace_id, record_id = self._key(record.workspace_id, record.record_id)
        payload = json.dumps(record.value, sort_keys=True, separators=(",", ":"))
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

    def put_if_unchanged(
        self, expected: WorkspaceRecord, records: tuple[WorkspaceRecord, ...]
    ) -> bool:
        """Atomically save related metadata only while its source revision is unchanged."""
        return self.replace_if_unchanged(expected, records)

    def replace_if_unchanged(
        self,
        expected: WorkspaceRecord,
        records: tuple[WorkspaceRecord, ...],
        *,
        delete_record_ids: tuple[str, ...] = (),
    ) -> bool:
        """Atomically replace related records while the audited source is unchanged."""
        workspace, record_id = self._key(expected.workspace_id, expected.record_id)
        expected_json = json.dumps(expected.value, sort_keys=True, separators=(",", ":"))
        values = []
        for record in records:
            key = self._key(record.workspace_id, record.record_id)
            if key[0] != workspace:
                raise ValueError("atomic metadata update must stay inside one workspace")
            values.append((*key, json.dumps(record.value, sort_keys=True, separators=(",", ":"))))
        delete_ids = []
        for delete_record_id in delete_record_ids:
            delete_ids.append(self._key(workspace, delete_record_id)[1])
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._replace_prepared(
                connection,
                workspace=workspace,
                record_id=record_id,
                expected_json=expected_json,
                values=values,
                delete_ids=delete_ids,
            )

    def replace_if_unchanged_in_transaction(
        self,
        connection: sqlite3.Connection,
        expected: WorkspaceRecord,
        records: tuple[WorkspaceRecord, ...],
        *,
        delete_record_ids: tuple[str, ...] = (),
    ) -> bool:
        """Apply a revision-guarded replacement in a caller-owned transaction."""

        self._require_store_connection(connection)
        workspace, record_id = self._key(expected.workspace_id, expected.record_id)
        expected_json = json.dumps(expected.value, sort_keys=True, separators=(",", ":"))
        values = []
        for record in records:
            key = self._key(record.workspace_id, record.record_id)
            if key[0] != workspace:
                raise ValueError("atomic metadata update must stay inside one workspace")
            values.append((*key, json.dumps(record.value, sort_keys=True, separators=(",", ":"))))
        delete_ids = [self._key(workspace, value)[1] for value in delete_record_ids]
        with self._lock:
            return self._replace_prepared(
                connection,
                workspace=workspace,
                record_id=record_id,
                expected_json=expected_json,
                values=values,
                delete_ids=delete_ids,
            )

    @staticmethod
    def _replace_prepared(
        connection: sqlite3.Connection,
        *,
        workspace: str,
        record_id: str,
        expected_json: str,
        values: list[tuple[str, str, str]],
        delete_ids: list[str],
    ) -> bool:
        row = connection.execute(
            "SELECT value_json FROM workspace_records WHERE workspace_id=? AND record_id=?",
            (workspace, record_id),
        ).fetchone()
        if row is None or row[0] != expected_json:
            return False
        connection.executemany(
            "INSERT INTO workspace_records (workspace_id, record_id, value_json) "
            "VALUES (?,?,?) ON CONFLICT(workspace_id, record_id) DO UPDATE SET "
            "value_json=excluded.value_json, "
            "updated_at=CURRENT_TIMESTAMP",
            values,
        )
        for delete_record_id in delete_ids:
            deleted = connection.execute(
                "DELETE FROM workspace_records WHERE workspace_id=? AND record_id=?",
                (workspace, delete_record_id),
            )
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError("atomic metadata source disappeared")
        return True

    def _require_store_connection(self, connection: sqlite3.Connection) -> None:
        databases = connection.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in databases if row[1] == "main"), "")
        if not main or Path(main).resolve() != self.path.resolve():
            raise ValueError("transaction connection does not belong to this workspace store")

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
