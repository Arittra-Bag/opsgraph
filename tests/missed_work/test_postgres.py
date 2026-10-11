"""Real broker SQL versus independently labelled synthetic outcomes."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from psycopg import sql

from opsgraph.brokers.postgres import PsycopgReadOnlyExecutor
from opsgraph.brokers.query import QueryBroker, SelectOnlyValidator
from opsgraph.domain import Principal
from opsgraph.domain.work_outcome import WorkOutcomeContract
from opsgraph.missed_work.contracts import admit
from opsgraph.missed_work.queries import fact_query
from opsgraph.missed_work.service import checked_facts
from opsgraph.orchestration.coordinator import RunBlocked
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator

from .export_oracle import InvalidPopulation, reconcile
from .test_export_fixtures import large_cohort

FIXTURE = json.loads(Path(__file__).with_name("export-cases.json").read_text())
BLANK_IDS = ("", "   ", "\t\n\r\v\f", "\u00a0", "\u2003\u202f\u3000")


def ready_case():
    return deepcopy(next(c["data"] for c in FIXTURE["cases"] if c["id"] == "ready-on-time"))


def capture(pg, contract=None, max_rows=100):  # noqa: ANN001
    contract = contract or pg["contract"]
    bounds = pg["bounds"].model_copy(update={"max_rows": max_rows})
    admit(contract, contract.source_id, pg["snapshot"], bounds)
    broker = QueryBroker(
        policy=FailClosedPolicy(StaticPolicyEvaluator({("analyst", "core.query.read"): bounds})),
        validator=SelectOnlyValidator(),
        executor=PsycopgReadOnlyExecutor(
            pg["dsn"],
            allowed_tables=bounds.allowed_tables,
            allowed_schemas=bounds.allowed_schemas,
            expected_schema_fingerprint=pg["snapshot"].fingerprint,
        ),
    )
    artifact = broker.query(
        principal=Principal(subject="test", workspace_id="test", roles={"analyst"}),
        sql=fact_query(contract, pg["scope"], max_rows),
    )
    return artifact, checked_facts(artifact)


@pytest.mark.parametrize(
    "case",
    [c for c in FIXTURE["cases"] if not c["data"].get("promises_truncated")],
    ids=lambda c: c["id"],
)
def test_real_sql_matches_reference(postgres, case):
    postgres["load"](case["data"])
    contract = postgres["contract"]
    if "sla_seconds" in case:
        data = contract.model_dump()
        data["deadline"] = {"kind": "fixed_sla", "seconds": case["sla_seconds"]}
        contract = WorkOutcomeContract.model_validate(data)
    if "expected_error" in case:
        with pytest.raises(RunBlocked):
            capture(postgres, contract)
        return
    artifact, facts = capture(postgres, contract)
    assert not artifact.truncated
    expected = reconcile(case["data"], FIXTURE["scope"], sla_seconds=case.get("sla_seconds"))
    assert facts["counts"] == expected["counts"]
    assert facts["affected_customers"] == expected["affected_customers"]
    assert facts["orphan_events"] == expected["orphan_events"]
    assert {
        r["promise_id"]: {"state": r["state"], "deadline_breached": r["breach"]}
        for r in facts["examples"]
    } == case["expected_records"]


def test_full_totals_do_not_count_only_retained_examples(postgres):
    postgres["load"](large_cohort())
    artifact, facts = capture(postgres, max_rows=25)
    assert len(artifact.rows) == 25
    assert facts["population"] == 1000 and sum(facts["counts"].values()) == 1000
    assert facts["counts"]["overdue_unresolved"] == 12 and facts["affected_customers"] == 8
    assert len(facts["examples"]) == 14
    assert not facts["examples_complete"] and facts["overdue_examples_complete"]


def test_low_row_budget_cannot_hide_quality_checks(postgres):
    with pytest.raises(ValueError, match="six state totals"):
        fact_query(postgres["contract"], postgres["scope"], 9)


@pytest.mark.parametrize("event_id", BLANK_IDS)
@pytest.mark.parametrize("kind", ("outcomes", "attempts", "cancellations"))
def test_blank_event_identity_preserves_uncertainty(postgres, kind, event_id):
    data = ready_case()
    event = {**data["outcomes"][0], "event_id": event_id}
    if kind != "outcomes":
        event["status"] = "failed" if kind == "attempts" else "cancelled"
        data["outcomes"] = []
    data[kind] = [event]
    expected = reconcile(data, FIXTURE["scope"])
    assert expected["counts"]["unknown"] == 1
    postgres["load"](data)
    _, facts = capture(postgres)
    assert facts["counts"] == expected["counts"]
    assert facts["examples"][0]["breach"] is None


@pytest.mark.parametrize("promise_id", BLANK_IDS)
def test_blank_promise_identity_blocks_population(postgres, promise_id):
    data = ready_case()
    data["promises"][0]["promise_id"] = promise_id
    data["outcomes"][0]["promise_id"] = promise_id
    with pytest.raises(InvalidPopulation, match="promise key is missing"):
        reconcile(data, FIXTURE["scope"])
    postgres["load"](data)
    with pytest.raises(RunBlocked, match="prevent a valid population"):
        capture(postgres)


def test_nonblank_identity_is_not_trimmed_or_rejoined(postgres):
    data = ready_case()
    data["promises"][0]["promise_id"] = " export-1 "
    data["outcomes"][0].update(promise_id=" export-1 ", event_id=" event-1 ")
    postgres["load"](data)
    _, facts = capture(postgres)
    assert facts["counts"] == reconcile(data, FIXTURE["scope"])["counts"]
    assert facts["examples"][0]["promise_id"] == " export-1 "


@pytest.mark.parametrize("key_type", ("integer", "bigint", "uuid"))
def test_nonblank_typed_identities_remain_supported(postgres, key_type):
    data = ready_case()
    promise_id = "00000000-0000-0000-0000-000000000001" if key_type == "uuid" else "0"
    event_id = "00000000-0000-0000-0000-000000000002" if key_type == "uuid" else "1"
    data["promises"][0]["promise_id"] = promise_id
    data["outcomes"][0].update(promise_id=promise_id, event_id=event_id)
    postgres["load"](data)
    columns = [("export_requests", "promise_id")]
    columns += [
        (table, column)
        for table in ("export_outcomes", "export_attempts", "export_cancellations")
        for column in ("promise_id", "event_id")
    ]
    changed = []

    def change(table, column, target):  # noqa: ANN001
        postgres["owner"].execute(
            sql.SQL("ALTER TABLE {}.{} ALTER COLUMN {} TYPE {} USING {}::{}").format(
                sql.Identifier(postgres["schema"]),
                sql.Identifier(table),
                sql.Identifier(column),
                sql.SQL(target),
                sql.Identifier(column),
                sql.SQL(target),
            )
        )

    try:
        for table, column in columns:
            change(table, column, key_type)
            changed.append((table, column))
        snapshot = PsycopgReadOnlyExecutor(postgres["dsn"]).discover_snapshot(
            allowed_schemas=postgres["bounds"].allowed_schemas,
            allowed_tables=postgres["bounds"].allowed_tables,
        )
        definition = postgres["contract"].model_dump()
        definition["schema_fingerprint"] = snapshot.fingerprint
        typed = {**postgres, "snapshot": snapshot, "contract": WorkOutcomeContract(**definition)}
        _, facts = capture(typed)
        assert facts["counts"] == reconcile(data, FIXTURE["scope"])["counts"]
        assert facts["examples"][0]["promise_id"] == promise_id
    finally:
        for table, column in changed:
            change(table, column, "text")
