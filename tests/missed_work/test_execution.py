"""Scope/type admission, compiler escaping and capture-shape guards (no network)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsgraph.brokers.query import SelectOnlyValidator
from opsgraph.domain import EvidenceArtifact, Obligation
from opsgraph.domain.work_outcome import WorkOutcomeContract
from opsgraph.missed_work.contracts import WorkOutcomeRequest, admit
from opsgraph.missed_work.queries import COLUMNS, STATES, fact_query
from opsgraph.missed_work.service import checked_facts
from opsgraph.orchestration.coordinator import RunBlocked
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema

ROOT = Path(__file__).parent


@pytest.fixture
def admitted():
    raw = json.loads(ROOT.joinpath("export-contract.json").read_text())
    tables = []
    for key in ("promises", "outcomes", "attempts", "cancellations", "coverage"):
        mapping = raw[key]
        columns = [
            ColumnSchema(
                name=value,
                data_type="timestamp with time zone"
                if field in {"accepted_at", "occurred_at", "complete_through"}
                else "text",
            )
            for field, value in mapping.items()
            if isinstance(value, str) and field not in {"relation", "meaning"}
        ]
        if key == "promises":
            columns.append(ColumnSchema(name="due_at", data_type="timestamp with time zone"))
        tables.append(
            TableSchema(
                schema_name="public",
                table_name=mapping["relation"].split(".")[1],
                columns=tuple(columns),
            )
        )
    bounds = Obligation(
        allowed_tables=tuple(
            m["relation"]
            for m in (
                raw[k] for k in ("promises", "outcomes", "attempts", "cancellations", "coverage")
            )
        )
    )
    snapshot = SchemaSnapshot(tables=tuple(tables), fingerprint="unused").scoped(
        bounds.allowed_tables
    )
    raw["schema_fingerprint"] = snapshot.fingerprint
    contract = WorkOutcomeContract.model_validate(raw)
    scope = WorkOutcomeRequest(
        contract_id=contract.id,
        version=1,
        **json.loads(ROOT.joinpath("export-cases.json").read_text())["scope"],
    )
    return contract, scope, snapshot, bounds


def test_admission_and_literal_escaping_use_existing_ast_and_scope(admitted):
    contract, scope, snapshot, bounds = admitted
    admit(contract, contract.source_id, snapshot, bounds)
    attack = scope.model_copy(update={"tenant_id": "tenant-a' OR 'x'='x"})
    query = fact_query(contract, attack, 100)
    assert "tenant-a'' OR ''x''=''x" in query
    plan = SelectOnlyValidator().validate(workspace_id="test", sql=query, obligations=bounds)
    assert set(plan.referenced_tables) == set(bounds.allowed_tables)


@pytest.mark.parametrize(
    "change", ["source", "fingerprint", "relation", "column", "timestamp", "join_type"]
)
def test_admission_rejects_unreviewed_scope_or_types(admitted, change):
    contract, _, snapshot, bounds = admitted
    data = contract.model_dump()
    if change == "source":
        data["source_id"] = "another-source"
    elif change == "fingerprint":
        data["schema_fingerprint"] = "sha256:" + "0" * 64
    elif change == "relation":
        data["outcomes"]["relation"] = "private.unapproved"
    elif change == "column":
        data["outcomes"]["promise_key"] = "uninspected_column"
    else:
        tables = list(snapshot.tables)
        table = tables[1]
        target = "occurred_at" if change == "timestamp" else "promise_id"
        columns = tuple(
            c.model_copy(
                update={
                    "data_type": "timestamp without time zone"
                    if change == "timestamp"
                    else "integer"
                }
            )
            if c.name == target
            else c
            for c in table.columns
        )
        tables[1] = table.model_copy(update={"columns": columns})
        snapshot = snapshot.model_copy(update={"tables": tuple(tables)})
    with pytest.raises(ValueError):
        admit(WorkOutcomeContract.model_validate(data), contract.source_id, snapshot, bounds)


@pytest.mark.parametrize(
    "change",
    [
        {"start": "2026-10-11T11:00:00"},
        {"end": "2026-10-11T13:00:00Z"},
        {"tenant_id": " "},
        {"start": "2025-01-01T00:00:00Z"},
    ],
)
def test_scope_requires_known_instants_and_a_bounded_population(admitted, change):
    _, scope, _, _ = admitted
    with pytest.raises(ValidationError):
        WorkOutcomeRequest.model_validate({**scope.model_dump(), **change})


def artifact(rows, *, truncated=False):  # noqa: ANN001
    return EvidenceArtifact.from_result(
        workspace_id="test",
        query_fingerprint="fixture",
        referenced_tables=("public.exports",),
        columns=COLUMNS,
        rows=tuple(rows),
        truncated=truncated,
    )


def healthy_rows():
    rows = [("summary", s, None, None, None, 1 if s == "ready" else 0, None) for s in STATES]
    rows += [
        ("quality", "invalid_population", None, None, None, 0, None),
        ("quality", "orphan_events", None, None, None, 0, None),
        ("quality", "unassignable_events", None, None, None, 0, None),
        ("scope", "population", None, None, None, 1, None),
        ("scope", "affected_customers", None, None, None, 0, None),
    ]
    return rows


@pytest.mark.parametrize(
    "problem",
    ["truncated", "missing", "duplicate", "negative", "boolean", "mismatch", "bad_population"],
)
def test_incomplete_or_inconsistent_captures_cannot_become_checked_facts(problem):
    rows = healthy_rows()
    if problem == "missing":
        rows.pop()
    if problem == "duplicate":
        rows.append(rows[0])
    if problem in {"negative", "boolean", "mismatch"}:
        rows[0] = (*rows[0][:5], {"negative": -1, "boolean": True, "mismatch": 2}[problem], None)
    if problem == "bad_population":
        rows[6] = (*rows[6][:5], 1, None)
    with pytest.raises(RunBlocked):
        checked_facts(artifact(rows, truncated=problem == "truncated"))


def test_empty_examples_do_not_imply_complete_population():
    facts = checked_facts(artifact(healthy_rows()))
    assert facts["population"] == 1 and not facts["examples_complete"]
