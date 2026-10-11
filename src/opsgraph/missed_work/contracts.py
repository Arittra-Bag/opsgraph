"""Scope and physical admission for operator-reviewed outcome definitions."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from opsgraph.domain import Obligation
from opsgraph.domain.work_outcome import RecordedDeadline, WorkOutcomeContract
from opsgraph.schema_service import SchemaSnapshot


class WorkOutcomeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")]
    version: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    tenant_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    start: datetime
    end: datetime
    as_of: datetime

    @field_validator("start", "end", "as_of", mode="before")
    @classmethod
    def explicit_instant(cls, value):  # noqa: ANN001
        if not isinstance(value, (str, datetime)):
            raise ValueError("provide an explicit timezone-aware timestamp, not an epoch number")
        return value

    @field_validator("start", "end", "as_of")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("an explicit timezone is required")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_window(self):
        if not self.start < self.end <= self.as_of:
            raise ValueError("require start < end <= observation cutoff")
        if self.end - self.start > timedelta(days=31) or not self.tenant_id.strip():
            raise ValueError("choose a nonblank tenant and a population window up to 31 days")
        return self


def record_id(contract_id: str, version: int) -> str:
    return f"work-outcome:{contract_id}:{version}"


def admit(
    contract: WorkOutcomeContract, source_id: str, snapshot: SchemaSnapshot, obligations: Obligation
) -> None:
    """Reject drift/scope/type ambiguity; this does not validate business meanings."""
    if contract.source_id != source_id or contract.schema_fingerprint != snapshot.fingerprint:
        raise ValueError("Contract source/schema changed. Review and save a new contract version.")
    mappings = (
        contract.promises,
        contract.outcomes,
        contract.attempts,
        contract.cancellations,
        contract.coverage,
    )
    if not {m.relation for m in mappings}.issubset(obligations.allowed_tables):
        raise ValueError("Contract relations are outside the currently approved source scope.")
    tables = {f"{t.schema_name}.{t.table_name}": t for t in snapshot.tables}
    types = {}
    identifier_types = {"text", "character varying", "uuid", "integer", "bigint", "smallint"}
    for mapping in mappings:
        table = tables.get(mapping.relation)
        if table is None:
            raise ValueError("Contract relation is absent from the inspected schema.")
        columns = {c.name: c.data_type.lower() for c in table.columns}
        values = mapping.model_dump()
        for field, name in values.items():
            if field == "relation" or field == "meaning" or isinstance(name, tuple):
                continue
            if name not in columns:
                raise ValueError("Contract column is absent from the inspected schema.")
            expected = (
                {"timestamp with time zone"}
                if field in {"accepted_at", "occurred_at", "complete_through"}
                else (
                    {"text", "character varying"} if field == "status_column" else identifier_types
                )
            )
            if columns[name] not in expected:
                raise ValueError("Contract column type does not establish its declared meaning.")
        types[mapping.relation] = columns
    if isinstance(contract.deadline, RecordedDeadline):
        if (
            types[contract.promises.relation].get(contract.deadline.due_at)
            != "timestamp with time zone"
        ):
            raise ValueError("Recorded deadline requires an inspected timezone-aware timestamp.")
    parent = types[contract.promises.relation]
    for mapping in mappings[1:]:
        child = types[mapping.relation]
        if child[mapping.tenant_key] != parent[contract.promises.tenant_key]:
            raise ValueError("Tenant join types must agree; review an approved normalized view.")
        if hasattr(mapping, "promise_key") and (
            child[mapping.promise_key] != parent[contract.promises.promise_key]
        ):
            raise ValueError("Promise join types must agree; review an approved normalized view.")
