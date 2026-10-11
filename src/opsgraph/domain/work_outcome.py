"""Declarative definitions for the first, customer-export outcome contract.

This module only defines semantics; approval and execution are separate opt-in steps.
Validation establishes a bounded definition, not source authority, authorization,
operator approval, completeness, or the existence of the mapped schema.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

Column = Annotated[StrictStr, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
Relation = Annotated[StrictStr, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}$")]
Status = Annotated[StrictStr, Field(min_length=1, max_length=64)]
Statuses = Annotated[tuple[Status, ...], Field(min_length=1, max_length=32)]


class ContractDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def distinct_status_values(cls, value):  # noqa: ANN001
        if isinstance(value, tuple):
            if len(value) != len(set(value)):
                raise ValueError("status meanings must not contain duplicate values")
            if any(not item.strip() or item != item.strip() for item in value):
                raise ValueError("status meanings must contain nonblank, unpadded values")
        return value


class PromiseMapping(ContractDefinition):
    """One accepted customer promise per (tenant, logical ID), before attempts."""

    relation: Relation
    promise_key: Column
    tenant_key: Column
    customer_key: Column
    accepted_at: Column


class EventMapping(ContractDefinition):
    """Event identity is independent of its tenant-scoped promise join key."""

    relation: Relation
    event_key: Column
    promise_key: Column
    tenant_key: Column
    occurred_at: Column
    status_column: Column


class OutcomeMapping(EventMapping):
    """Terminal records describe the customer postcondition, not worker status."""

    ready_statuses: Statuses
    terminal_failure_statuses: Statuses

    @model_validator(mode="after")
    def disjoint_meanings(self):
        if set(self.ready_statuses).intersection(self.terminal_failure_statuses):
            raise ValueError("ready and terminal failure meanings must be disjoint")
        return self


class AttemptMapping(EventMapping):
    known_statuses: Statuses
    failed_statuses: Statuses

    @model_validator(mode="after")
    def failure_is_known(self):
        if not set(self.failed_statuses).issubset(self.known_statuses):
            raise ValueError("failed attempt meanings must be included in known statuses")
        return self


class CancellationMapping(EventMapping):
    legitimate_statuses: Statuses


class CoverageMapping(ContractDefinition):
    """An owner-defined coverage watermark; MAX(event_time) is not sufficient."""

    relation: Relation
    tenant_key: Column
    complete_through: Column
    meaning: Literal["authoritative_complete_through"] = "authoritative_complete_through"


class RecordedDeadline(ContractDefinition):
    kind: Literal["recorded"]
    due_at: Column


class FixedDeadline(ContractDefinition):
    kind: Literal["fixed_sla"]
    seconds: Annotated[StrictInt, Field(ge=1, le=31_536_000)]


class WorkOutcomeContract(ContractDefinition):
    """Versioned export semantics, separate from policy's resource Obligation.

    A future invocation must independently bind approved source/schema scope,
    one tenant, an accepted-at [start, end) population, and an observation cutoff.
    Each relation is joined by tenant AND promise ID. Positive outcome evidence
    and absence-based conclusions have different coverage requirements.
    """

    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    id: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")]
    version: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    source_id: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")]
    schema_fingerprint: Annotated[StrictStr, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    work_kind: Literal["customer_export"] = "customer_export"
    unit: Literal["logical_export"] = "logical_export"
    timezone: Literal["UTC"] = "UTC"
    timestamp_meaning: Literal["timezone_aware"] = "timezone_aware"
    population: Literal["accepted_at_half_open"] = "accepted_at_half_open"
    join: Literal["tenant_and_promise"] = "tenant_and_promise"
    due_boundary: Literal["ready_at_or_before_due"] = "ready_at_or_before_due"
    failure_basis: Literal["terminal_outcome_record"] = "terminal_outcome_record"
    cancellation_history: Literal["preserve_deadline_breach"] = "preserve_deadline_breach"
    promises: PromiseMapping
    outcomes: OutcomeMapping
    attempts: AttemptMapping
    cancellations: CancellationMapping
    coverage: CoverageMapping
    deadline: Annotated[RecordedDeadline | FixedDeadline, Field(discriminator="kind")]

    @model_validator(mode="after")
    def unambiguous_keys_and_relations(self):
        mappings = (self.promises, self.outcomes, self.attempts, self.cancellations)
        for mapping in mappings:
            if mapping.tenant_key == mapping.promise_key:
                raise ValueError("tenant and promise keys must be distinct columns")
            if isinstance(mapping, EventMapping) and mapping.event_key in (
                mapping.tenant_key,
                mapping.promise_key,
            ):
                raise ValueError("event identity must be distinct from its promise join keys")
        relations = [mapping.relation for mapping in (*mappings, self.coverage)]
        if len(relations) != len(set(relations)):
            raise ValueError("the first export contract requires distinct source relations")
        return self
