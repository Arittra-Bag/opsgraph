"""Workspace-scoped in-memory storage.

This is deterministic process-local infrastructure, not durable or
multi-node persistence. Callers cannot address records without a workspace.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    record_id: str
    value: dict[str, Any]


class InMemoryWorkspaceStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], WorkspaceRecord] = {}
        self._lock = RLock()

    def put(self, record: WorkspaceRecord) -> None:
        key = self._key(record.workspace_id, record.record_id)
        with self._lock:
            self._records[key] = WorkspaceRecord(*key, deepcopy(record.value))

    def get(self, *, workspace_id: str, record_id: str) -> WorkspaceRecord:
        key = self._key(workspace_id, record_id)
        with self._lock:
            record = self._records.get(key)
        if record is None:
            raise KeyError(record_id)
        return WorkspaceRecord(record.workspace_id, record.record_id, deepcopy(record.value))

    def list(self, *, workspace_id: str) -> tuple[WorkspaceRecord, ...]:
        self._validate_component(workspace_id, "workspace_id")
        with self._lock:
            keys = tuple(sorted(self._records))
        return tuple(
            self.get(workspace_id=ws, record_id=record_id)
            for ws, record_id in keys
            if ws == workspace_id
        )

    def replace_if_unchanged(
        self,
        expected: WorkspaceRecord,
        records: tuple[WorkspaceRecord, ...],
        *,
        delete_record_ids: tuple[str, ...] = (),
    ) -> bool:
        """Process-local equivalent of the durable store's atomic transition."""

        expected_key = self._key(expected.workspace_id, expected.record_id)
        changes: list[tuple[tuple[str, str], WorkspaceRecord]] = []
        for record in records:
            key = self._key(record.workspace_id, record.record_id)
            if key[0] != expected_key[0]:
                raise ValueError("atomic metadata update must stay inside one workspace")
            changes.append((key, WorkspaceRecord(*key, deepcopy(record.value))))
        delete_keys = tuple(
            self._key(expected_key[0], record_id) for record_id in delete_record_ids
        )
        with self._lock:
            current = self._records.get(expected_key)
            if current != expected:
                return False
            if any(key not in self._records for key in delete_keys):
                raise KeyError("atomic metadata source disappeared")
            for key, record in changes:
                self._records[key] = record
            for key in delete_keys:
                del self._records[key]
        return True

    @classmethod
    def _key(cls, workspace_id: str, record_id: str) -> tuple[str, str]:
        cls._validate_component(workspace_id, "workspace_id")
        cls._validate_component(record_id, "record_id")
        return workspace_id, record_id

    @staticmethod
    def _validate_component(value: str, name: str) -> None:
        if not value or len(value) > 128:
            raise ValueError(f"{name} must contain 1-128 characters")
