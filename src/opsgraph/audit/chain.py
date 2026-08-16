"""Process-local append-only hash-chain audit log.

The chain detects modification, deletion, and reordering within an exported
sequence. It is not immutable storage, a signature, or an external timestamp;
production deployments must anchor/export heads to independent durable storage.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opsgraph.domain.models import stable_hash

GENESIS_HASH = "sha256:" + "0" * 64


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    workspace_id: str
    actor: str
    action: str
    resource: str
    outcome: str
    details: dict[str, Any]
    occurred_at: datetime
    previous_hash: str
    entry_hash: str


class AuditVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    checked_entries: int
    failure_sequence: int | None = None
    reason: str = "valid"


class AuditChain:
    """Append entries and verify a complete workspace-scoped chain."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._entries: list[AuditEntry] = []
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        with self._lock:
            return tuple(self._entries)

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
        with self._lock:
            sequence = len(self._entries) + 1
            previous_hash = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
            payload = {
                "sequence": sequence,
                "workspace_id": workspace_id,
                "actor": actor,
                "action": action,
                "resource": resource,
                "outcome": outcome,
                "details": details or {},
                "occurred_at": self._clock().astimezone(UTC).isoformat(),
                "previous_hash": previous_hash,
            }
            entry_data = dict(payload)
            entry_data["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])
            entry = AuditEntry(**entry_data, entry_hash=stable_hash(payload))
            self._entries.append(entry)
            return entry

    @classmethod
    def verify(cls, entries: tuple[AuditEntry, ...] | list[AuditEntry]) -> AuditVerification:
        previous_hash = GENESIS_HASH
        for expected_sequence, entry in enumerate(entries, start=1):
            if entry.sequence != expected_sequence:
                return AuditVerification(
                    valid=False,
                    checked_entries=expected_sequence - 1,
                    failure_sequence=entry.sequence,
                    reason="sequence gap or reorder detected",
                )
            if entry.previous_hash != previous_hash:
                return AuditVerification(
                    valid=False,
                    checked_entries=expected_sequence - 1,
                    failure_sequence=entry.sequence,
                    reason="previous hash mismatch",
                )
            payload = {
                "sequence": entry.sequence,
                "workspace_id": entry.workspace_id,
                "actor": entry.actor,
                "action": entry.action,
                "resource": entry.resource,
                "outcome": entry.outcome,
                "details": entry.details,
                "occurred_at": entry.occurred_at.astimezone(UTC).isoformat(),
                "previous_hash": entry.previous_hash,
            }
            if entry.entry_hash != stable_hash(payload):
                return AuditVerification(
                    valid=False,
                    checked_entries=expected_sequence - 1,
                    failure_sequence=entry.sequence,
                    reason="entry hash mismatch",
                )
            previous_hash = entry.entry_hash
        return AuditVerification(valid=True, checked_entries=len(entries))
