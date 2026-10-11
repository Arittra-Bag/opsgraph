"""Real broker SQL versus independently labelled synthetic outcomes."""

import json
from pathlib import Path

import pytest

from opsgraph.brokers.postgres import PsycopgReadOnlyExecutor
from opsgraph.brokers.query import QueryBroker, SelectOnlyValidator
from opsgraph.domain import Principal
from opsgraph.domain.work_outcome import WorkOutcomeContract
from opsgraph.missed_work.contracts import admit
from opsgraph.missed_work.queries import fact_query
from opsgraph.missed_work.service import checked_facts
from opsgraph.orchestration.coordinator import RunBlocked
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator

from .export_oracle import reconcile
from .test_export_fixtures import large_cohort

FIXTURE = json.loads(Path(__file__).with_name("export-cases.json").read_text())


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
