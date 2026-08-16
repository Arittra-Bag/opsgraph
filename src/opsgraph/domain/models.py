"""Typed, deterministic contracts for authorization, querying, and evidence.

The public alpha is single-tenant at deployment level. Every persisted or
authorized object still carries a workspace identifier so accidental
cross-workspace access fails closed and a later tenancy boundary is explicit.
"""

from __future__ import annotations

import hashlib
import json
import math
from base64 import b64encode
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def canonical_json(value: Any) -> bytes:
    """Serialize a JSON-compatible value with stable ordering."""

    return json.dumps(
        _canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _canonicalize(value: Any) -> Any:
    """Convert common database scalar types without unstable ``repr`` fallbacks."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be evidence")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetimes cannot be evidence")
        return {"$datetime": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, bytes):
        return {"$bytes_b64": b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("evidence object keys must be strings")
        return {key: _canonicalize(item) for key, item in value.items()}
    raise TypeError(f"unsupported evidence value type: {type(value).__name__}")


def stable_hash(value: Any) -> str:
    """Return a versioned SHA-256 identifier for reproducible evidence."""

    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


class Principal(BaseModel):
    """Locally authenticated actor; not an enterprise identity assertion."""

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    roles: frozenset[str] = Field(default_factory=lambda: frozenset({"viewer"}))
    auth_method: Literal["local_dev"] = "local_dev"


class Obligation(BaseModel):
    """Hard limits returned by policy and enforced again by the broker."""

    model_config = ConfigDict(frozen=True)

    max_rows: int = Field(default=100, ge=1, le=1_000)
    timeout_ms: int = Field(default=5_000, ge=100, le=30_000)
    allowed_schemas: tuple[str, ...] = ("public",)
    allowed_tables: tuple[str, ...] = ()

    @field_validator("allowed_schemas", "allowed_tables")
    @classmethod
    def bounded_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 100:
            raise ValueError("policy obligations may contain at most 100 names")
        if any(not item or len(item) > 128 for item in values):
            raise ValueError("policy obligation names must contain 1-128 characters")
        return tuple(dict.fromkeys(values))


class PolicyDecision(BaseModel):
    """Fail-closed policy response."""

    model_config = ConfigDict(frozen=True)

    allowed: bool = False
    reason: str = Field(min_length=1, max_length=500)
    policy_id: str = Field(default="default-deny", min_length=1, max_length=128)
    obligations: Obligation | None = None


class QueryPlan(BaseModel):
    """Validated SQL plus policy obligations; never raw model output alone."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    sql: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    obligations: Obligation
    fingerprint: str


class EvidenceArtifact(BaseModel):
    """Stable evidence envelope created from bounded query results."""

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    query_fingerprint: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_hash: str

    @classmethod
    def from_result(
        cls,
        *,
        workspace_id: str,
        query_fingerprint: str,
        columns: tuple[str, ...],
        rows: tuple[tuple[Any, ...], ...],
        truncated: bool,
    ) -> EvidenceArtifact:
        payload = {
            "workspace_id": workspace_id,
            "query_fingerprint": query_fingerprint,
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
        }
        return cls(
            **payload,
            evidence_hash=stable_hash(payload),
        )
