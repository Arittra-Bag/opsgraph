"""Definition admission checks, independent of production source/API execution."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsgraph.domain.work_outcome import FixedDeadline, RecordedDeadline, WorkOutcomeContract

ROOT = Path(__file__).parent


@pytest.fixture
def definition():
    return json.loads(ROOT.joinpath("export-contract.json").read_text(encoding="utf-8"))


def replace(data, path, value):  # noqa: ANN001
    edited = deepcopy(data)
    target = edited
    for part in path.split(".")[:-1]:
        target = target[part]
    target[path.split(".")[-1]] = value
    return edited


def test_export_definition_round_trips_with_frozen_meanings(definition):
    contract = WorkOutcomeContract.model_validate(definition)
    assert isinstance(contract.deadline, RecordedDeadline)
    assert contract.unit == "logical_export"
    assert contract.join == "tenant_and_promise"
    assert contract.failure_basis == "terminal_outcome_record"
    assert contract.cancellation_history == "preserve_deadline_breach"
    assert WorkOutcomeContract.model_validate_json(contract.model_dump_json()) == contract
    with pytest.raises(ValidationError, match="frozen"):
        contract.version = 2
    with pytest.raises(ValidationError, match="frozen"):
        contract.outcomes.ready_statuses = ("succeeded",)


def test_fixed_deadline_has_explicit_seconds_and_no_due_column(definition):
    contract = WorkOutcomeContract.model_validate(
        replace(definition, "deadline", {"kind": "fixed_sla", "seconds": 600})
    )
    assert isinstance(contract.deadline, FixedDeadline)
    assert contract.deadline.seconds == 600


@pytest.mark.parametrize(
    "path,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("schema_version", 1.0),
        ("version", 0),
        ("version", True),
        ("version", "1"),
        ("version", 1.5),
        ("source_id", ""),
        ("source_id", "other source"),
        ("schema_fingerprint", "unreviewed"),
        ("schema_fingerprint", "sha256:" + "a" * 63),
        ("promises.relation", "export_requests"),
        ("promises.relation", "private.exports; DROP TABLE users"),
        ("promises.relation", 'public."Exports"'),
        ("promises.relation", "public." + "a" * 64),
        ("promises.relation", "public.exports\n"),
        ("outcomes.promise_key", "promise_id::text"),
        ("outcomes.promise_key", ""),
        ("outcomes.promise_key", "x" * 64),
        ("outcomes.promise_key", 123),
        ("outcomes.ready_statuses", []),
        ("outcomes.ready_statuses", [""]),
        ("outcomes.ready_statuses", [" "]),
        ("outcomes.ready_statuses", ["ready "]),
        ("outcomes.ready_statuses", ["ready", "ready"]),
        ("outcomes.ready_statuses", ["x" * 65]),
        ("outcomes.ready_statuses", [1]),
        ("outcomes.ready_statuses", [str(index) for index in range(33)]),
        ("outcomes.terminal_failure_statuses", ["ready"]),
        ("attempts.failed_statuses", ["unmapped_failure"]),
        ("promises.tenant_key", "promise_id"),
        ("attempts.event_key", "promise_id"),
        ("attempts.event_key", "tenant_id"),
        ("attempts.relation", "public.export_outcomes"),
        ("coverage.meaning", "latest_event_timestamp"),
        ("join", "promise_only"),
        ("unit", "attempt"),
        ("timezone", "assume_local"),
        ("timestamp_meaning", "naive"),
        ("population", "all_retained_rows"),
        ("failure_basis", "failed_attempt"),
        ("cancellation_history", "erase_deadline_breach"),
        ("deadline", {"kind": "recorded"}),
        ("deadline", {"kind": "expression", "sql": "now() + interval '10 minutes'"}),
        ("deadline", {"kind": "fixed_sla", "seconds": 0}),
        ("deadline", {"kind": "fixed_sla", "seconds": 31_536_001}),
        ("deadline", {"kind": "fixed_sla", "seconds": True}),
        ("deadline", {"kind": "fixed_sla", "seconds": "600"}),
        ("deadline", {"kind": "fixed_sla", "seconds": 600.5}),
        ("deadline", {"kind": "fixed_sla", "seconds": 600, "due_at": "due_at"}),
    ],
)
def test_ambiguous_or_unsupported_definitions_are_rejected(definition, path, value):
    with pytest.raises(ValidationError):
        WorkOutcomeContract.model_validate(replace(definition, path, value))


@pytest.mark.parametrize("path", ["sql", "outcomes.sql", "promises.role", "coverage.probe"])
def test_executable_or_extra_fields_are_rejected(definition, path):
    with pytest.raises(ValidationError, match="Extra inputs"):
        WorkOutcomeContract.model_validate(replace(definition, path, "not executable"))


@pytest.mark.parametrize("field", ["promises", "outcomes", "attempts", "cancellations", "coverage"])
def test_missing_mappings_do_not_get_inferred(definition, field):
    del definition[field]
    with pytest.raises(ValidationError, match="Field required"):
        WorkOutcomeContract.model_validate(definition)
