"""Capture exact broker evidence, then derive separately hashed outcome facts."""

import json
from datetime import UTC, datetime

from opsgraph.brokers.query import QueryBroker, SelectOnlyValidator
from opsgraph.domain.models import stable_hash
from opsgraph.orchestration.coordinator import RunBlocked
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator

from .queries import COLUMNS, METHOD, STATES, fact_query


def checked_facts(artifact):  # noqa: ANN001
    if artifact.truncated or tuple(artifact.columns) != COLUMNS:
        raise RunBlocked("Fact capture is incomplete or has an unexpected shape. Narrow the scope.")
    summaries, metadata, examples = {}, {}, []
    for values in artifact.rows:
        row = dict(zip(COLUMNS, values, strict=True))
        if row["kind"] in {"summary", "quality", "scope"}:
            value = row["count_value"]
            if type(value) is not int or value < 0:
                raise RunBlocked("A captured count is not a nonnegative integer.")
            target = summaries if row["kind"] == "summary" else metadata
            if row["state"] in target:
                raise RunBlocked("Fact capture contains duplicate aggregate rows.")
            target[row["state"]] = value
        elif row["kind"] == "example":
            if row["state"] not in STATES:
                raise RunBlocked("Fact capture contains an unrecognized outcome state.")
            examples.append(
                {k: row[k] for k in ("promise_id", "customer_id", "due_at", "state", "breach")}
            )
        else:
            raise RunBlocked("Fact capture contains an unrecognized row kind.")
    if set(summaries) != set(STATES) or set(metadata) != {
        "invalid_population",
        "orphan_events",
        "unassignable_events",
        "population",
        "affected_customers",
    }:
        raise RunBlocked("Fact capture omits required totals or quality checks.")
    if metadata["invalid_population"]:
        raise RunBlocked(
            "Missing/duplicate promise keys, tenant keys or acceptance times "
            "prevent a valid population."
        )
    if sum(summaries.values()) != metadata["population"]:
        raise RunBlocked("Outcome states do not reconcile with the captured promise population.")
    ids = [r["promise_id"] for r in examples]
    if len(ids) != len(set(ids)) or any(not isinstance(v, str) or not v.strip() for v in ids):
        raise RunBlocked("Example membership is ambiguous.")
    if (
        len(examples) > metadata["population"]
        or metadata["affected_customers"] > summaries["overdue_unresolved"]
        or any(
            sum(r["state"] == state for r in examples) > total for state, total in summaries.items()
        )
    ):
        raise RunBlocked("Example membership or customer totals exceed the captured population.")
    for example in examples:
        if example["due_at"] is not None:
            if not isinstance(example["due_at"], datetime) or example["due_at"].tzinfo is None:
                raise RunBlocked("A captured deadline has no known time semantics.")
            example["due_at"] = example["due_at"].isoformat()
    return {
        "counts": summaries,
        "population": metadata["population"],
        "affected_customers": metadata["affected_customers"],
        "orphan_events": metadata["orphan_events"],
        "unassignable_events": metadata["unassignable_events"],
        "examples": examples,
        "examples_complete": len(examples) == metadata["population"],
        "overdue_examples_complete": sum(r["state"] == "overdue_unresolved" for r in examples)
        == summaries["overdue_unresolved"],
    }


def execute(*, contract, scope, principal, obligations, executor, observe, check, provenance):
    try:
        sql = fact_query(contract, scope, obligations.max_rows)
    except ValueError as exc:
        raise RunBlocked(str(exc)) from exc
    validator = SelectOnlyValidator()
    plan = validator.validate(workspace_id=principal.workspace_id, sql=sql, obligations=obligations)
    query_plan = {
        "queries": [
            {"purpose": "Reconcile the approved export promises at one snapshot", "sql": sql}
        ],
        "rationale": "Deterministic contract audit; no model invocation",
        "clarification": None,
    }
    observe("plan_completed", {"plan": query_plan})
    broker = QueryBroker(
        policy=FailClosedPolicy(
            StaticPolicyEvaluator({("analyst", "core.query.read"): obligations})
        ),
        validator=validator,
        executor=executor,
    )
    check()
    started = datetime.now(UTC).isoformat()
    observe("query_started", {"index": 0, "purpose": query_plan["queries"][0]["purpose"]})
    artifact = broker.query(principal=principal, sql=sql)
    # Persist the completed capture before another authorization check or interpretation.
    payload = artifact.model_dump(mode="json")
    payload["integrity"] = {
        "format": "opsgraph-canonical-json-v1",
        "scope": "query-result",
        "canonical_json": artifact.canonical_hash_input(),
    }
    payload["provenance"] = {
        **provenance,
        "sql": plan.sql,
        "proposed_sql": sql,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    payload["purpose"] = query_plan["queries"][0]["purpose"]
    if len(json.dumps(payload).encode()) > 393_216:
        raise RunBlocked("Complete fact capture exceeds the existing evidence payload bound.")
    observe("evidence_captured", {"index": 0, "evidence": payload})
    check()
    facts = checked_facts(artifact)
    calculation = {
        "version": 1,
        "method": METHOD,
        "input_evidence_hashes": [artifact.evidence_hash],
        "contract": contract.model_dump(mode="json"),
        "scope": scope.model_dump(mode="json"),
        "facts": facts,
    }
    derived = {**calculation, "calculation_hash": stable_hash(calculation)}
    counts = facts["counts"]
    summary = (
        f"{counts['overdue_unresolved']} overdue unresolved exports across "
        f"{facts['affected_customers']} customers; {counts['terminal_failure']} terminal failures "
        f"and {counts['unknown']} unknown among {facts['population']} accepted promises."
    )
    return {
        "plan": query_plan,
        "answer": {
            "summary": summary,
            "work_outcome": derived,
            "findings": [
                {
                    "claim": summary,
                    "classification": "supported",
                    "evidence_ids": [artifact.evidence_hash],
                }
            ],
            "limitations": [
                "Outcome definitions and source coverage are operator-reviewed premises, "
                "not verified business truth.",
                "Examples are bounded; state totals come from the full selected population.",
                "Ready database records do not prove customer access or an incident cause.",
            ],
        },
    }
