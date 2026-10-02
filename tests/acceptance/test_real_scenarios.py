"""Opt-in, real-model scenarios; offline checks never count as live acceptance.

The operator provisions fixture.sql in a new dedicated database. This module
never creates resources or changes data, except for the separately enabled,
restored schema-drift column. Human semantic review is always a separate result.
"""

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from opsgraph_acceptance_checks import (
    ERROR_CODES,
    RUN_DEADLINE_SECONDS,
    STAGES,
    STATUSES,
    completed,
    observed_stage,
    require,
    safe_run_id,
)
from psycopg.conninfo import conninfo_to_dict
from test_real_workflow import live as live
from test_real_workflow import payload, verify_export, write_attempt

from opsgraph.brokers.query import SelectOnlyValidator
from opsgraph.domain import Obligation

FIXTURE = Path(__file__).parent / "scenarios" / "fixture.sql"
ENABLED = os.getenv("OPSGRAPH_ACCEPTANCE_SCENARIOS") == "1"
live_scenarios = pytest.mark.skipif(
    not ENABLED, reason="Real scenarios require explicit OPSGRAPH_ACCEPTANCE_SCENARIOS=1"
)
schema_change = pytest.mark.skipif(
    os.getenv("OPSGRAPH_ACCEPTANCE_SCHEMA_CHANGE") != "1",
    reason="Schema mutation requires explicit OPSGRAPH_ACCEPTANCE_SCHEMA_CHANGE=1",
)


@dataclass(frozen=True)
class Scenario:
    name: str
    tables: tuple[str, ...]
    question: str
    oracle_sql: str
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    clarification: bool = False


LEDGER = Scenario(
    name="ledger_posted_totals",
    tables=(
        "acceptance_ledger.accounts",
        "acceptance_ledger.invoices",
        "acceptance_ledger.settlements",
    ),
    question=(
        "For every account, including accounts without invoices or posted settlements, return "
        "account_key, currency, posted_total, posted_count, ordered by account_key. "
        "Operator definitions: invoices.account_id references accounts.account_id; "
        "settlements.invoice_id references invoices.invoice_id; each settlement signed_amount "
        "uses its account's currency. Sum signed_amount only where settlement_state = 'posted', "
        "including negative entries, and count those settlement rows. Use numeric zero when "
        "none exist. Keep currencies separate. Cite the observed totals and state that these "
        "definitions are operator assumptions, with no causal or revenue-recognition claim."
    ),
    oracle_sql="""
        SELECT a.account_key, a.currency,
               COALESCE(SUM(s.signed_amount), 0) AS posted_total,
               COUNT(s.settlement_id) AS posted_count
        FROM acceptance_ledger.accounts a
        LEFT JOIN acceptance_ledger.invoices i ON i.account_id = a.account_id
        LEFT JOIN acceptance_ledger.settlements s
          ON s.invoice_id = i.invoice_id AND s.settlement_state = 'posted'
        GROUP BY a.account_key, a.currency ORDER BY a.account_key LIMIT 100
    """,
    columns=("account_key", "currency", "posted_total", "posted_count"),
    rows=(
        ("A-01", "USD", Decimal("90.00"), 3),
        ("A-02", "EUR", Decimal("100.00"), 2),
        ("A-03", "USD", Decimal("0"), 0),
        ("A-04", "INR", Decimal("2000.00"), 2),
        ("A-05", "GBP", Decimal("50.00"), 2),
        ("A-06", "USD", Decimal("0"), 0),
    ),
)
DISPATCH = Scenario(
    name="dispatch_missing_scans",
    tables=(
        "acceptance_dispatch.routes",
        "acceptance_dispatch.parcels",
        "acceptance_dispatch.scans",
    ),
    question=(
        "For each route, including the route without parcels, return route_code, parcel_count, "
        "dropoff_parcels, unscanned_parcels, ordered by route_code. Operator relationship "
        "definitions: parcels.route_id references routes.route_id; scans.parcel_id references "
        "parcels.parcel_id. Count each parcel once. dropoff_parcels counts parcels with at "
        "least one scan_kind = 'dropoff'; unscanned_parcels counts parcels with no scan rows. "
        "Report only these observed records with citations. Missing scans do not establish "
        "delivery failure or a physical location, and a dropoff code is not independent proof "
        "of delivery. Keep those limitations explicit."
    ),
    oracle_sql="""
        SELECT r.route_code, COUNT(DISTINCT p.parcel_id) AS parcel_count,
               COUNT(DISTINCT CASE WHEN s.scan_kind = 'dropoff'
                     THEN p.parcel_id END) AS dropoff_parcels,
               COUNT(DISTINCT p.parcel_id) - COUNT(DISTINCT s.parcel_id) AS unscanned_parcels
        FROM acceptance_dispatch.routes r
        LEFT JOIN acceptance_dispatch.parcels p ON p.route_id = r.route_id
        LEFT JOIN acceptance_dispatch.scans s ON s.parcel_id = p.parcel_id
        GROUP BY r.route_code ORDER BY r.route_code LIMIT 100
    """,
    columns=("route_code", "parcel_count", "dropoff_parcels", "unscanned_parcels"),
    rows=(("R-A", 4, 2, 1), ("R-B", 3, 1, 1), ("R-C", 3, 1, 1), ("R-D", 2, 1, 1), ("R-E", 0, 0, 0)),
)
METERING = Scenario(
    name="metering_nulls_and_decimals",
    tables=("acceptance_metering.probes", "acceptance_metering.readings"),
    question=(
        "For each probe, return probe_key, sample_count, missing_value_count, min_reading, "
        "max_reading, ordered by probe_key. Operator relationship definition: readings.probe_id "
        "references probes.probe_id. Count reading rows, count NULL reading_value separately, "
        "and report the numeric minimum and maximum of non-NULL reading_value. Use exact "
        "numeric values. Cite observations. Do not infer measurement units, elapsed periods, "
        "timezones, equipment health or the meaning of reading_state from these records."
    ),
    oracle_sql="""
        SELECT p.probe_key, COUNT(r.reading_id) AS sample_count,
               COUNT(r.reading_id) - COUNT(r.reading_value) AS missing_value_count,
               MIN(r.reading_value) AS min_reading, MAX(r.reading_value) AS max_reading
        FROM acceptance_metering.probes p
        LEFT JOIN acceptance_metering.readings r ON r.probe_id = p.probe_id
        GROUP BY p.probe_key ORDER BY p.probe_key LIMIT 100
    """,
    columns=("probe_key", "sample_count", "missing_value_count", "min_reading", "max_reading"),
    rows=(
        ("P-A", 3, 1, Decimal("12.3400"), Decimal("13.1000")),
        ("P-B", 3, 0, Decimal("0.0000"), Decimal("0.0020")),
        ("P-C", 3, 1, Decimal("999.9900"), Decimal("1001.2300")),
    ),
)


def ambiguous(name, question):
    # The same independent fixture oracle validates the source before these runs;
    # its values are never injected into the model's question or context.
    return Scenario(
        name,
        METERING.tables,
        question,
        METERING.oracle_sql,
        METERING.columns,
        METERING.rows,
        clarification=True,
    )


SCENARIOS = (
    LEDGER,
    DISPATCH,
    METERING,
    ambiguous(
        "clarify_units",
        "Which probes have any reading_value greater than 20 kWh? The operator confirms "
        "readings.probe_id references probes.probe_id; all other definitions are unavailable.",
    ),
    ambiguous(
        "clarify_timezone",
        "How many readings occurred on 2026-09-11 in UTC, using recorded_local? "
        "The source timezone of recorded_local is unavailable.",
    ),
    ambiguous(
        "clarify_status",
        "Which probes failed, based on reading_state? The operator confirms readings.probe_id "
        "references probes.probe_id; the status-code definitions are unavailable.",
    ),
)
DRIFT = Scenario(
    "schema_drift",
    ("acceptance_drift.records",),
    "Read record_id and marker from acceptance_drift.records ordered by record_id. "
    "Cite direct observations and state limitations.",
    "SELECT record_id, marker FROM acceptance_drift.records ORDER BY record_id LIMIT 100",
    ("record_id", "marker"),
    ((1, "one"), (2, "two"), (3, "three")),
)


def exact_result(columns, rows, scenario):
    """Compare values and Python types; Decimal scale is covered by capture hashing."""
    return (
        tuple(columns) == scenario.columns
        and len(rows) == len(scenario.rows)
        and all(
            len(actual) == len(expected)
            and all(type(a) is type(e) and a == e for a, e in zip(actual, expected, strict=True))
            for actual, expected in zip(rows, scenario.rows, strict=True)
        )
    )


@contextmanager
def readonly(dsn):
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        yield connection


def verify_fixture(live, scenario):
    with readonly(live.dsn) as connection:
        cursor = connection.execute(scenario.oracle_sql)
        require(
            exact_result([c.name for c in cursor.description], cursor.fetchmany(102), scenario),
            "Dedicated fixture differs from its independently specified expected facts",
        )


@pytest.fixture(scope="module")
def scenario_context(live):
    """Additional opt-in identity and private-output requirements; no provisioning."""
    artifact = os.getenv("OPSGRAPH_ACCEPTANCE_ARTIFACT_SHA256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", artifact):
        pytest.fail("Set the frozen artifact's 64-character SHA-256", pytrace=False)
    log = Path(os.getenv("OPSGRAPH_ACCEPTANCE_SCENARIOS_LOG", ""))
    if not log.is_absolute():
        pytest.fail("Set an absolute private OPSGRAPH_ACCEPTANCE_SCENARIOS_LOG", pytrace=False)
    return live, artifact, log


@contextmanager
def attempt(context, scenario):
    _, artifact, log = context
    record = {
        "attempt_id": uuid4().hex,
        "scenario": scenario.name,
        "artifact_sha256": artifact,
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "started_at": datetime.now(UTC).isoformat(),
        "phase": "prerequisites",
        "runs": [],
        "outcome": "failed",
        "expected_facts_verified": None if scenario.clarification or scenario == DRIFT else False,
        "export_values_and_hashes_verified": (
            None if scenario.clarification or scenario == DRIFT else False
        ),
        "semantic_review": "not_applicable" if scenario == DRIFT else "pending_human_review",
    }
    started = time.monotonic()
    try:
        yield record
        record["outcome"] = "passed"
        record["phase"] = "complete"
    except Exception as error:
        # No model text, SQL, rows, DSN, HTTP headers or exception messages enter logs.
        record["failure_kind"] = (
            "assertion"
            if isinstance(error, AssertionError)
            else "transport"
            if isinstance(error, httpx.HTTPError)
            else "database"
            if isinstance(error, psycopg.Error)
            else "check_failed"
        )
        pytest.fail(
            f"Live scenario {scenario.name} failed during {record['phase']}; "
            "see its private bounded attempt log and persisted run. Raw output withheld.",
            pytrace=False,
        )
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        record["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_attempt(log, record)


def prepare(live, scenario, record):
    health = payload(live.client.get("/api/health"))
    require(health["mode"] == "connected", "Scenario requires a connected application")
    require(health["model"] == "openai_compatible", "Scenario requires real model inference")
    record["phase"] = "fixture_oracle"
    verify_fixture(live, scenario)
    record["fixture_oracle_verified"] = True
    source_id = f"scenario-{uuid4().hex[:12]}"
    record["phase"] = "source_inspection"
    source = payload(
        live.client.post(
            "/api/sources",
            json={
                "id": source_id,
                "name": "Purpose-made broader acceptance",
                "secret_ref": os.getenv("OPSGRAPH_ACCEPTANCE_SECRET_REF", "OPSGRAPH_SOURCE_DSN"),
                "allowed_schemas": sorted({table.split(".")[0] for table in scenario.tables}),
                "allowed_tables": list(scenario.tables),
                "evidence_bindings": [],
                "allow_external_egress": os.getenv("OPSGRAPH_ACCEPTANCE_SOURCE_EGRESS") == "true",
            },
        )
    )
    require(source["id"] == source_id, "Source identity differs")
    inspection = payload(live.client.post(f"/api/sources/{source_id}/inspect"))
    require(inspection["status"] == "ready", "Source inspection did not become ready")
    record["phase"] = "source_readiness"
    readiness = payload(
        live.client.post(
            f"/api/sources/{source_id}/readiness",
            json={"table": scenario.tables[0], "confirm_bounded_read": True},
        )
    )
    require(readiness["status"] == "ready", "Bounded source readiness did not pass")
    require(readiness["source_values_returned"] == 0, "Readiness retained a source value")
    return source_id, inspection


def submit(live, source_id, scenario):
    return payload(
        live.client.post(
            "/api/runs",
            json={
                "source_id": source_id,
                "skill_id": "generic-readonly",
                "question": scenario.question,
                "request_id": f"scenario-{uuid4().hex}",
            },
        ),
        202,
    )


def blocked(live, run, record, *, deadline_seconds=RUN_DEADLINE_SECONDS):
    started = time.monotonic()
    while (
        run["status"] in {"queued", "running", "cancelling"}
        and time.monotonic() - started < deadline_seconds
    ):
        time.sleep(0.5)
        run = payload(live.client.get(f"/api/runs/{run['id']}", timeout=10.0))
    code = (run.get("error") or {}).get("code")
    record["runs"].append(
        {
            "role": "expected_block",
            "run_id": safe_run_id(run.get("id")),
            "status": run["status"] if run["status"] in STATUSES else "unavailable",
            "error_code": code if code in ERROR_CODES else "unavailable",
            "stage": observed_stage(live.client, run),
            "captures": len(run.get("evidence", [])),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "harness_deadline_reached": run["status"] in {"queued", "running", "cancelling"},
        }
    )
    require(run["status"] == "blocked", "Expected a durable blocked result")
    require(not run["evidence"], "Blocked-before-query scenario captured unexpected evidence")
    require(run["plan"] is None and run["answer"] is None, "Blocked result retained a plan/answer")
    return run


def verify_blocked_export(live, run):
    exported = payload(live.client.get(f"/api/runs/{run['id']}/export"))
    require(exported.get("export_version") == 1, "Unexpected export version")
    require(
        {key: value for key, value in exported.items() if key != "export_version"} == run,
        "Blocked export differs from the persisted run",
    )
    serialized = json.dumps(exported)
    credentials = (
        live.dsn,
        os.environ["OPSGRAPH_ACCEPTANCE_KEY"],
        conninfo_to_dict(live.dsn).get("password"),
        conninfo_to_dict(os.environ["OPSGRAPH_ACCEPTANCE_OWNER_DSN"]).get("password")
        if os.getenv("OPSGRAPH_ACCEPTANCE_OWNER_DSN")
        else None,
    )
    require(
        not any(value and value in serialized for value in credentials),
        "Blocked export contains a credential; value withheld",
    )


def verify_expected_result(live, run, scenario):
    matched = False
    with readonly(live.dsn) as connection:
        for capture in run["evidence"]:
            require(
                set(capture["referenced_tables"]) <= set(scenario.tables),
                "Capture used a relation outside the selected scenario",
            )
            cursor = connection.execute(capture["provenance"]["sql"])
            matches = exact_result(
                [column.name for column in cursor.description], cursor.fetchmany(102), scenario
            )
            matched = matched or (
                matches and set(capture["referenced_tables"]) == set(scenario.tables)
            )
    require(matched, "No captured query matches the requested typed result and relation coverage")


@live_scenarios
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.name)
def test_real_unfamiliar_scenario(scenario_context, scenario):
    live, _, _ = scenario_context
    with attempt(scenario_context, scenario) as record:
        source_id, _ = prepare(live, scenario, record)
        record["phase"] = "model_investigation"
        accepted = submit(live, source_id, scenario)
        if scenario.clarification:
            run = blocked(live, accepted, record)
            require(
                run["error"]["code"] == "clarification_required",
                "Ambiguous semantics did not produce a clarification",
            )
            prefix = "Clarification needed before querying: "
            message = run["error"].get("message", "")
            require(
                message.startswith(prefix) and 8 <= len(message[len(prefix) :]) <= 600,
                "Clarification is missing or exceeds its bounded contract",
            )
            stages = stage_history(live, run)
            require(
                "plan" in stages and stages <= {"route", "plan"},
                "Clarification did not stop before query execution",
            )
            verify_blocked_export(live, run)
            record["blocked_export_verified"] = True
            record["clarification_contract_verified"] = True
            # No evidence exists, so value/hash checks are inapplicable, never 'passed'.
            return
        run = completed(live.client, accepted, "scenario", record)
        require(
            run["configuration"]["provider"] == "openai_compatible",
            "A replay provider cannot satisfy this scenario",
        )
        record["phase"] = "independent_capture_verification"
        record["runs"][-1]["classification_counts"] = verify_export(run, live)
        record["export_values_and_hashes_verified"] = True
        record["phase"] = "expected_result_verification"
        verify_expected_result(live, run, scenario)
        record["expected_facts_verified"] = True


def stage_history(live, run):
    """Require the complete persisted event prefix, bounded by count and time."""
    target = run["last_event_id"]
    require(isinstance(target, int) and 0 < target <= 256, "Unexpected event prefix size")
    reached = False
    stages = set()
    started = time.monotonic()
    with live.client.stream("GET", f"/api/runs/{run['id']}/events", timeout=5.0) as response:
        require(response.status_code == 200, "Event prefix unavailable")
        for index, line in enumerate(response.iter_lines()):
            require(index < 2048 and time.monotonic() - started < 5, "Event prefix budget exceeded")
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            candidate = event.get("data", {}).get("stage")
            if event.get("type") == "stage_started" and candidate in STAGES:
                stages.add(candidate)
            if event.get("id") == target:
                reached = True
                break
    require(reached, "Incomplete event prefix cannot establish absence of planning/query stages")
    return stages


@live_scenarios
@schema_change
def test_real_schema_change_blocks_before_planning(scenario_context):
    live, _, _ = scenario_context
    with attempt(scenario_context, DRIFT) as record:
        owner_dsn = os.getenv("OPSGRAPH_ACCEPTANCE_OWNER_DSN")
        require(bool(owner_dsn), "Enabled schema change requires a private dedicated owner DSN")
        owner_info, reader_info = conninfo_to_dict(owner_dsn), conninfo_to_dict(live.dsn)
        require(
            all(owner_info.get(key) == reader_info.get(key) for key in ("host", "port", "dbname")),
            "Schema owner and reader must target the same dedicated loopback database",
        )
        require(owner_info.get("user") != reader_info.get("user"), "Use a separate owner role")
        source_id, before = prepare(live, DRIFT, record)
        record["phase"] = "explicit_schema_change"
        with psycopg.connect(owner_dsn, connect_timeout=5, autocommit=True) as owner:
            owner.execute("SET statement_timeout = '5s'")
            owner.execute("SET lock_timeout = '2s'")
            # No IF NOT EXISTS: a pre-existing column must fail before mutation.
            owner.execute(
                "ALTER TABLE acceptance_drift.records ADD COLUMN acceptance_probe boolean"
            )
            record["schema_column_added"] = True
            try:
                record["phase"] = "schema_change_investigation"
                run = blocked(live, submit(live, source_id, DRIFT), record, deadline_seconds=45)
                require(run["error"]["code"] == "blocked", "Unexpected schema-change error code")
                require(
                    "SELECT-visible columns changed" in run["error"]["message"],
                    "Run blocked for an unrelated reason",
                )
                require(not stage_history(live, run), "Schema drift reached a graph/model stage")
                verify_blocked_export(live, run)
                record["blocked_export_verified"] = True
                record["blocked_before_planning_verified"] = True
            finally:
                record["cleanup_phase"] = "schema_restoration"
                owner.execute("ALTER TABLE acceptance_drift.records DROP COLUMN acceptance_probe")
                record["schema_column_restored"] = True
        record["phase"] = "restored_fixture_verification"
        restored = payload(live.client.post(f"/api/sources/{source_id}/inspect"))
        require(restored["fingerprint"] == before["fingerprint"], "Restored schema differs")
        readiness = payload(
            live.client.post(
                f"/api/sources/{source_id}/readiness",
                json={"table": DRIFT.tables[0], "confirm_bounded_read": True},
            )
        )
        require(readiness["status"] == "ready", "Restored source readiness did not pass")
        verify_fixture(live, DRIFT)
        record["fixture_restored_verified"] = True


def test_scenario_oracles_obey_current_strict_query_policy():
    """Offline fixture-authoring check only; never recorded as real acceptance."""
    for scenario in (*SCENARIOS, DRIFT):
        plan = SelectOnlyValidator().validate(
            workspace_id="scenario-authoring",
            sql=scenario.oracle_sql,
            obligations=Obligation(
                allowed_schemas=tuple({table.split(".")[0] for table in scenario.tables}),
                allowed_tables=scenario.tables,
                max_rows=100,
                timeout_ms=5000,
            ),
        )
        require(set(plan.referenced_tables) == set(scenario.tables), "Oracle scope differs")


def test_scenario_expected_result_requires_exact_values_and_types():
    require(exact_result(LEDGER.columns, LEDGER.rows, LEDGER), "Expected fixture is inconsistent")
    changed = [list(row) for row in LEDGER.rows]
    changed[0][2] = 90.0
    require(not exact_result(LEDGER.columns, changed, LEDGER), "Float replaced exact Decimal")
    changed[0][2] = Decimal("91.00")
    require(not exact_result(LEDGER.columns, changed, LEDGER), "Incorrect total was accepted")
    require(not exact_result(LEDGER.columns, LEDGER.rows[:-1], LEDGER), "Missing account accepted")
