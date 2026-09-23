"""Durable SQLite-backed audit chain for the self-hosted workspace."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from opsgraph.domain.models import stable_hash

from .chain import GENESIS_HASH, AuditChain, AuditEntry, AuditVerification


class AuditIntegrityError(RuntimeError):
    """The durable audit log is unreadable or fails hash-chain verification."""


class SQLiteAuditChain(AuditChain):
    """Append a single global hash chain transactionally to local SQLite."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_entries (
                    sequence INTEGER PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        with self._lock, self._connect() as connection:
            return self._entries_from_connection(connection)

    def require_valid(self) -> None:
        """Refuse to trust persisted receipts when the complete chain is invalid."""

        with self._lock, self._connect() as connection:
            self._require_valid_connection(connection)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a verified transaction that can include related state changes."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_valid_connection(connection)
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            else:
                connection.commit()

    def append(
        self,
        *,
        workspace_id: str,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        with self.transaction() as connection:
            return self.append_in_transaction(
                connection,
                workspace_id=workspace_id,
                actor=actor,
                action=action,
                resource=resource,
                outcome=outcome,
                details=details,
            )

    def append_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append using a caller-owned verified transaction on this state database."""

        if not connection.in_transaction:
            raise RuntimeError("audit append requires an active transaction")
        last = connection.execute(
            "SELECT sequence, entry_hash FROM audit_entries ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = int(last[0]) + 1 if last else 1
        previous_hash = str(last[1]) if last else GENESIS_HASH
        occurred_at = self._clock().astimezone(UTC)
        payload = {
            "sequence": sequence,
            "workspace_id": workspace_id,
            "actor": actor,
            "action": action,
            "resource": resource,
            "outcome": outcome,
            "details": details or {},
            "occurred_at": occurred_at.isoformat(),
            "previous_hash": previous_hash,
        }
        entry = AuditEntry(
            sequence=sequence,
            workspace_id=workspace_id,
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            details=details or {},
            occurred_at=occurred_at,
            previous_hash=previous_hash,
            entry_hash=stable_hash(payload),
        )
        connection.execute(
            "INSERT INTO audit_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.sequence,
                entry.workspace_id,
                entry.actor,
                entry.action,
                entry.resource,
                entry.outcome,
                json.dumps(entry.details, sort_keys=True, separators=(",", ":")),
                entry.occurred_at.isoformat(),
                entry.previous_hash,
                entry.entry_hash,
            ),
        )
        return entry

    @classmethod
    def verify_connection(cls, connection: sqlite3.Connection) -> AuditVerification | None:
        """Verify the complete chain visible through an existing connection."""

        try:
            entries = cls._entries_from_connection(connection)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return AuditChain.verify(entries)

    @classmethod
    def _require_valid_connection(cls, connection: sqlite3.Connection) -> None:
        verification = cls.verify_connection(connection)
        if verification is None or not verification.valid:
            raise AuditIntegrityError(
                "Local audit integrity verification failed; preserve the state database "
                "and restore or inspect it before continuing."
            )

    @classmethod
    def _entries_from_connection(cls, connection: sqlite3.Connection) -> tuple[AuditEntry, ...]:
        rows = connection.execute(
            "SELECT sequence, workspace_id, actor, action, resource, outcome, "
            "details_json, occurred_at, previous_hash, entry_hash "
            "FROM audit_entries ORDER BY sequence"
        ).fetchall()
        return tuple(cls._entry(row) for row in rows)

    @staticmethod
    def _entry(row: tuple[Any, ...]) -> AuditEntry:
        return AuditEntry(
            sequence=row[0],
            workspace_id=row[1],
            actor=row[2],
            action=row[3],
            resource=row[4],
            outcome=row[5],
            details=json.loads(row[6]),
            occurred_at=datetime.fromisoformat(row[7]),
            previous_hash=row[8],
            entry_hash=row[9],
        )
