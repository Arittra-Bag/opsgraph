"""Workspace-scoped in-memory storage.

This is deterministic process-local alpha infrastructure, not durable or
multi-node persistence. Callers cannot address records without a workspace.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    record_id: str
    value: dict[str, Any]


class InMemoryWorkspaceStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], WorkspaceRecord] = {}

    def put(self, record: WorkspaceRecord) -> None:
        key = self._key(record.workspace_id, record.record_id)
        self._records[key] = WorkspaceRecord(*key, deepcopy(record.value))

    def get(self, *, workspace_id: str, record_id: str) -> WorkspaceRecord:
        key = self._key(workspace_id, record_id)
        record = self._records.get(key)
        if record is None:
            raise KeyError(record_id)
        return WorkspaceRecord(record.workspace_id, record.record_id, deepcopy(record.value))

    def list(self, *, workspace_id: str) -> tuple[WorkspaceRecord, ...]:
        self._validate_component(workspace_id, "workspace_id")
        return tuple(
            self.get(workspace_id=ws, record_id=record_id)
            for ws, record_id in sorted(self._records)
            if ws == workspace_id
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
