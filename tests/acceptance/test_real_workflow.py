"""Opt-in connector/model acceptance. No mocks, replay provider, or fixture server.

Run only against a dedicated local acceptance server and PostgreSQL database.
An enabled run fails on missing prerequisites rather than silently skipping them.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
from opsgraph_acceptance_checks import (
    PROBE_TIMEOUT_SECONDS,
    completed,
    require,
    verify_answer,
    verify_capture_values,
    verify_fresh_attempt,
)
from psycopg.conninfo import conninfo_to_dict

live_acceptance = pytest.mark.skipif(
    os.getenv("OPSGRAPH_ACCEPTANCE") != "1",
    reason="real PostgreSQL/model acceptance requires explicit OPSGRAPH_ACCEPTANCE=1",
)


def repetitions():
    try:
        count = int(os.getenv("OPSGRAPH_ACCEPTANCE_REPETITIONS", "1"))
    except ValueError:
        raise pytest.UsageError("Acceptance repetitions must be an integer from 1 to 5") from None
    if not 1 <= count <= 5:
        raise pytest.UsageError("Acceptance repetitions must be from 1 to 5")
    return range(1, count + 1)


@dataclass(frozen=True, repr=False)
class LiveAcceptance:
    client: httpx.Client
    dsn: str
    table: str

    def __repr__(self):
        return "LiveAcceptance(client=<configured>, dsn=<redacted>, table=<configured>)"

    def __iter__(self):
        # Preserve the original fixture's unpacking contract; its repr stays private.
        return iter((self.client, self.dsn, self.table))


def test_acceptance_fixture_repr_never_exposes_credentials():
    credential = "fixture-only-credential"
    with httpx.Client() as client:
        config = LiveAcceptance(client, credential, "public.acceptance")
        assert credential not in repr(config)
        assert "redacted" in repr(config)
        assert tuple(config) == (client, credential, "public.acceptance")


@pytest.fixture(scope="module")
def live():
    required = ("OPSGRAPH_ACCEPTANCE_URL", "OPSGRAPH_ACCEPTANCE_KEY", "OPSGRAPH_ACCEPTANCE_DSN")
    if any(not os.getenv(name) for name in required):
        pytest.fail(
            "Enabled acceptance requires URL, KEY and DSN environment variables", pytrace=False
        )
    url = os.environ["OPSGRAPH_ACCEPTANCE_URL"]
    endpoint = urlsplit(url)
    if endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} or endpoint.username:
        pytest.fail("Acceptance server must use a loopback URL without credentials", pytrace=False)
    dsn = os.environ["OPSGRAPH_ACCEPTANCE_DSN"]
    try:
        parsed = conninfo_to_dict(dsn)
    except Exception:
        pytest.fail("Acceptance DSN could not be parsed; value withheld", pytrace=False)
    if parsed.get("host") not in {"127.0.0.1", "localhost", "::1"} or not parsed.get(
        "dbname", ""
    ).startswith("opsgraph_acceptance"):
        pytest.fail("Use a dedicated loopback opsgraph_acceptance* database", pytrace=False)
    table = os.getenv("OPSGRAPH_ACCEPTANCE_TABLE", "public.opsgraph_acceptance_records")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", table):
        pytest.fail("Acceptance table must be a schema-qualified identifier", pytrace=False)
    with httpx.Client(
        base_url=url,
        headers={"X-OpsGraph-Key": os.environ["OPSGRAPH_ACCEPTANCE_KEY"]},
        timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=10.0),
        trust_env=False,
    ) as client:
        yield LiveAcceptance(client, dsn, table)


def payload(response, expected=200):
    assert response.status_code == expected, f"API status {response.status_code}; body withheld"
    return response.json()


def verify_captures(run, live):
    """Compare each actual executed bounded query with an independent DB read."""
    try:
        with psycopg.connect(live.dsn, connect_timeout=5) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            for capture in run["evidence"]:
                provenance = capture["provenance"]
                cursor = conn.execute(provenance["sql"])
                rows = tuple(cursor.fetchmany(102))
                verify_capture_values(
                    capture, run, rows, [column.name for column in cursor.description]
                )
    except psycopg.Error:
        pytest.fail(
            "Independent read-only database verification failed; credentials withheld",
            pytrace=False,
        )


def verify_export(run, live):
    exported = payload(live.client.get(f"/api/runs/{run['id']}/export"))
    require(exported["export_version"] == 1, "Unexpected export version")
    require(
        {key: value for key, value in exported.items() if key != "export_version"} == run,
        "Export differs from the persisted run",
    )
    serialized = json.dumps(exported)
    credentials = (
        live.dsn,
        os.environ["OPSGRAPH_ACCEPTANCE_KEY"],
        conninfo_to_dict(live.dsn).get("password"),
    )
    require(
        not any(value and value in serialized for value in credentials),
        "Export contains a credential; value withheld",
    )
    # Verify the exported capture bytes themselves, not just the detail response.
    verify_captures(exported, live)
    return verify_answer(exported)


def write_attempt(path, record):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "a") as output:
        require(
            stat.S_ISREG(os.fstat(output.fileno()).st_mode), "Acceptance log must be a regular file"
        )
        if os.name != "nt":
            require(
                os.fstat(output.fileno()).st_mode & 0o077 == 0,
                "Acceptance log must remain private (mode 0600)",
            )
        output.write(json.dumps(record, sort_keys=True) + "\n")


@live_acceptance
@pytest.mark.parametrize("repetition", repetitions())
def test_actual_model_postgres_history_export_retry_and_followup(live, repetition, tmp_path):
    record = {
        "attempt_id": uuid4().hex,
        "repetition": repetition,
        "started_at": datetime.now(UTC).isoformat(),
        "runs": [],
        "outcome": "failed",
        "phase": "prerequisites",
    }
    log = Path(os.environ.get("OPSGRAPH_ACCEPTANCE_LOG", tmp_path / "attempts.jsonl"))
    try:
        actual_workflow(live, record)
        record["outcome"] = "passed"
    except BaseException as error:
        # Never write exception messages, model output, SQL, rows or credentials.
        record["failure_kind"] = (
            "assertion"
            if isinstance(error, AssertionError)
            else "transport"
            if isinstance(error, httpx.HTTPError)
            else "interrupted"
            if isinstance(error, KeyboardInterrupt)
            else "check_failed"
        )
        raise
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        write_attempt(log, record)


def actual_workflow(live, record):
    client, table = live.client, live.table
    health = payload(client.get("/api/health"))
    assert health["mode"] == "connected"
    assert health["model"] == "openai_compatible", "Local-model acceptance requires real inference"
    record["phase"] = "model_probe"
    probe = payload(client.post("/api/providers/current/test"))
    assert probe["ok"] is True
    assert probe["provider"] == "openai_compatible"
    assert probe["model"] and probe["checked_at"]
    record["phase"] = "source_setup"
    source_id = f"acceptance-{uuid4().hex[:12]}"
    source = payload(
        client.post(
            "/api/sources",
            json={
                "id": source_id,
                "name": "Local acceptance PostgreSQL",
                "secret_ref": os.getenv("OPSGRAPH_ACCEPTANCE_SECRET_REF", "OPSGRAPH_SOURCE_DSN"),
                "allowed_schemas": [table.split(".")[0]],
                "allowed_tables": [table],
                "evidence_bindings": [],
                # Explicit opt-in only for a dedicated container -> host model route.
                "allow_external_egress": os.getenv("OPSGRAPH_ACCEPTANCE_SOURCE_EGRESS") == "true",
            },
        )
    )
    assert source["id"] == source_id
    payload(client.post(f"/api/sources/{source_id}/inspect"))
    record["phase"] = "first_investigation"
    request = {
        "source_id": source_id,
        "skill_id": "generic-readonly",
        "question": f"Use one query to read id, status and duration_ms from {table}, ordered "
        "by id. Summarize the observed statuses with citations. Put limitations in the answer.",
        "request_id": f"acceptance-{uuid4().hex}",
    }
    accepted = payload(client.post("/api/runs", json=request), 202)
    repeated = payload(client.post("/api/runs", json=request), 202)
    assert repeated["id"] == accepted["id"], "Retrying HTTP submission must not duplicate execution"
    run = completed(client, accepted, "first", record)
    require(
        run["configuration"]["provider"] == "openai_compatible",
        "Replay provider cannot satisfy live acceptance",
    )
    require(
        run["configuration"]["provider_timeout_seconds"] <= 300,
        "Provider timeout exceeds this harness's bounded budget",
    )
    assert any(item["id"] == run["id"] for item in payload(client.get("/api/runs")))
    record["runs"][-1]["classification_counts"] = verify_export(run, live)
    record["runs"][-1]["export_values_and_hashes_verified"] = True
    record["phase"] = "retry"
    retry = completed(
        client, payload(client.post(f"/api/runs/{run['id']}/retry"), 202), "retry", record
    )
    verify_fresh_attempt(run, retry, relationship="retry_of")
    require(retry["question"] == run["question"], "Retry changed the original question")
    record["runs"][-1]["classification_counts"] = verify_export(retry, live)
    record["runs"][-1]["export_values_and_hashes_verified"] = True
    record["phase"] = "followup"
    followup = completed(
        client,
        payload(
            client.post(
                "/api/runs",
                json={
                    **request,
                    "request_id": f"acceptance-{uuid4().hex}",
                    "parent_run_id": run["id"],
                    "question": f"Use one query to read id, status and duration_ms from {table} "
                    "ordered by id. Which records have failed status? Cite evidence and state "
                    "causal limitations in the answer.",
                },
            ),
            202,
        ),
        "followup",
        record,
    )
    verify_fresh_attempt(run, followup, relationship="parent_run_id")
    require(followup["retry_of"] is None, "Follow-up was stored as a retry")
    require(followup["id"] != retry["id"], "Follow-up reused retry identity")
    record["runs"][-1]["classification_counts"] = verify_export(followup, live)
    record["runs"][-1]["export_values_and_hashes_verified"] = True
    record["phase"] = "complete"
