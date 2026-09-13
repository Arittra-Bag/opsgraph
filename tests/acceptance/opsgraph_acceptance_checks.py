"""Strict live-acceptance checks with bounded, credential-free diagnostics."""

import hashlib
import json
import re
import time
from datetime import datetime

import httpx
from pydantic import ValidationError
from pydantic_core import to_json

from opsgraph.domain.models import canonical_json
from opsgraph.orchestration.connected import InvestigationAnswer

# Two bounded 300-second model calls, three five-second SQL operations, 30s overhead.
RUN_DEADLINE_SECONDS = 645
PROBE_TIMEOUT_SECONDS = 315
STAGES = {"route", "plan", "execute", "reconcile"}
STATUSES = {
    "queued",
    "running",
    "blocked",
    "failed",
    "interrupted",
    "cancelling",
    "cancelled",
    "completed",
}
ERROR_CODES = {
    *STATUSES,
    "model_citation_invalid",
    "model_answer_inconsistent",
    "model_output_invalid",
    "model_timeout",
    "model_output_truncated",
    "model_failed",
    "source_role_rejected",
    "source_unavailable",
    "evidence_too_large",
    "query_rejected",
    "query_failed",
    "evidence_type_unsupported",
    "schema_changed",
    "clarification_required",
    "mapping_required",
    "planning_context_too_large",
}


def require(condition, message):
    # Avoid pytest assertion expansion printing captured rows or malformed output.
    if not condition:
        raise AssertionError(message)


def safe_run_id(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"inv-[a-f0-9]{32}", value)
        else "unavailable"
    )


def observed_stage(client, run):
    """Read only the already-persisted SSE prefix; never wait for new progress."""
    target = run.get("last_event_id", 0)
    if not isinstance(target, int) or target < 1 or safe_run_id(run.get("id")) == "unavailable":
        return "unavailable"
    stage = "unavailable"
    deadline = time.monotonic() + 5
    try:
        with client.stream("GET", f"/api/runs/{run['id']}/events", timeout=5.0) as response:
            if response.status_code != 200:
                return stage
            for index, line in enumerate(response.iter_lines()):
                if index >= 256 or time.monotonic() >= deadline:
                    break
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                candidate = event.get("data", {}).get("stage")
                if (
                    event.get("type") in {"stage_started", "stage_completed"}
                    and candidate in STAGES
                ):
                    stage = candidate
                if event.get("id", 0) >= target:
                    break
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return stage
    return stage


def completed(client, run, role, record, *, deadline_seconds=RUN_DEADLINE_SECONDS):
    started = time.monotonic()
    deadline = started + deadline_seconds
    while run["status"] in {"queued", "running", "cancelling"} and time.monotonic() < deadline:
        time.sleep(0.5)
        response = client.get(f"/api/runs/{run['id']}", timeout=10.0)
        require(response.status_code == 200, "Run polling failed; response body withheld")
        run = response.json()
    timed_out = run["status"] in {"queued", "running", "cancelling"}
    error_code = (run.get("error") or {}).get("code")
    summary = {
        "role": role,
        "run_id": safe_run_id(run.get("id")),
        "status": run["status"] if run["status"] in STATUSES else "unavailable",
        "error_code": error_code if error_code in ERROR_CODES else "unavailable",
        "stage": observed_stage(client, run),
        "captures": len(run.get("evidence", [])),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "harness_deadline_reached": timed_out,
    }
    record["runs"].append(summary)
    require(
        run["status"] == "completed",
        "Live run did not complete: "
        + json.dumps(summary, sort_keys=True)
        + ". No automatic cancellation, repair or retry was performed.",
    )
    require(bool(run["evidence"]), "A successful live run must capture evidence")
    return run


def verify_answer(run):
    try:
        answer = InvestigationAnswer.model_validate(run["answer"])
    except (ValidationError, KeyError):
        raise AssertionError(
            "Completed answer violates the canonical schema; output withheld"
        ) from None
    require(bool(answer.findings), "Completed investigation must contain inspectable findings")
    allowed = {capture["evidence_hash"] for capture in run["evidence"]}
    for finding in answer.findings:
        require(set(finding.evidence_ids) <= allowed, "Finding references unavailable evidence")
        require(
            finding.classification == "unknown" or bool(finding.evidence_ids),
            "Non-unknown finding lacks evidence references",
        )
    # Classification labels remain model judgments; these checks do not establish truth.
    return {
        classification: sum(f.classification == classification for f in answer.findings)
        for classification in ("supported", "possible", "unknown", "contradictory")
    }


def parse_timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(timestamp.tzinfo is not None, "Capture timestamp must specify a timezone")
        return timestamp
    except (AttributeError, TypeError, ValueError):
        raise AssertionError("Capture timestamp is unavailable or invalid") from None


def verify_capture_values(capture, run, independent_rows, independent_columns):
    provenance = capture.get("provenance")
    require(bool(provenance), "Real evidence must expose query/capture provenance")
    require(provenance["source_id"] == run["source_id"], "Capture source identity differs")
    require(provenance["run_id"] == run["id"], "Capture run identity differs")
    require(
        bool(re.fullmatch(r"capture-[a-f0-9]{32}", provenance["capture_id"])),
        "Invalid capture identity",
    )
    require(bool(provenance["sql"]), "Capture lacks executed SQL")
    limit = provenance["limits"]["max_rows"]
    require(
        isinstance(limit, int) and 1 <= limit <= 100, "Capture row limit exceeds acceptance scope"
    )
    require(len(independent_rows) <= limit + 1, "Exported SQL is not bounded to the capture limit")
    require(
        capture["truncated"] == (len(independent_rows) > limit),
        "Truncation differs from independent read",
    )
    rows = tuple(independent_rows[:limit])
    columns = tuple(independent_columns)
    require(list(columns) == capture["columns"], "Columns differ from independent PostgreSQL read")
    require(
        json.loads(to_json(rows)) == capture["rows"],
        "Display rows differ from independent PostgreSQL values",
    )
    expected = {
        "workspace_id": capture["workspace_id"],
        "query_fingerprint": capture["query_fingerprint"],
        "referenced_tables": capture["referenced_tables"],
        "columns": columns,
        "rows": rows,
        "truncated": len(independent_rows) > limit,
    }
    integrity = capture.get("integrity", {})
    require(
        integrity.get("format") == "opsgraph-canonical-json-v1",
        "Canonical integrity format unavailable",
    )
    require(integrity.get("scope") == "query-result", "Unexpected integrity scope")
    exact = integrity.get("canonical_json")
    require(isinstance(exact, str), "Exact typed canonical hash input unavailable")
    require(
        exact.encode("utf-8") == canonical_json(expected),
        "Typed canonical input differs from PostgreSQL values",
    )
    digest = "sha256:" + hashlib.sha256(exact.encode("utf-8")).hexdigest()
    require(digest == capture["evidence_hash"], "Exported evidence SHA-256 does not reproduce")
    require(
        parse_timestamp(run["created_at"])
        <= parse_timestamp(provenance["started_at"])
        <= parse_timestamp(capture["created_at"])
        <= parse_timestamp(provenance["finished_at"]),
        "Capture timestamps do not follow actual run/query order",
    )


def verify_fresh_attempt(previous, current, *, relationship):
    require(current["id"] != previous["id"], "New attempt reused the previous run identity")
    require(current["source_id"] == previous["source_id"], "Attempt changed source unexpectedly")
    require(current[relationship] == previous["id"], "Attempt relationship is missing or incorrect")
    require(
        parse_timestamp(current["created_at"]) >= parse_timestamp(previous["finished_at"]),
        "Attempt predates the completed parent",
    )
    old_ids = {item["provenance"]["capture_id"] for item in previous["evidence"]}
    new_ids = {item["provenance"]["capture_id"] for item in current["evidence"]}
    require(old_ids.isdisjoint(new_ids), "New attempt reused a historical capture identity")
    latest = max(
        parse_timestamp(item["provenance"]["finished_at"]) for item in previous["evidence"]
    )
    require(
        all(
            parse_timestamp(item["provenance"]["started_at"]) > latest
            for item in current["evidence"]
        ),
        "New attempt did not collect fresh evidence",
    )
