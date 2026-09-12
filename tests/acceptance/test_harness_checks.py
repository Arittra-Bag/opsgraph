"""Fixtures validate the acceptance checker; they never satisfy live acceptance."""

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from opsgraph_acceptance_checks import (
    completed,
    verify_answer,
    verify_capture_values,
    verify_fresh_attempt,
)

from opsgraph.domain import EvidenceArtifact


def fixture_capture():
    rows = (
        (
            Decimal("12.3400"),
            date(2026, 9, 12),
            datetime(2026, 9, 12, tzinfo=UTC),
            UUID("12345678-1234-5678-1234-567812345678"),
        ),
    )
    columns = ("amount", "day", "observed_at", "record_id")
    artifact = EvidenceArtifact.from_result(
        workspace_id="fixture",
        query_fingerprint="sha256:fixture",
        referenced_tables=("public.acceptance",),
        columns=columns,
        rows=rows,
        truncated=False,
    )
    capture = artifact.model_dump(mode="json")
    capture["created_at"] = "2026-09-12T00:00:02+00:00"
    capture["integrity"] = {
        "format": "opsgraph-canonical-json-v1",
        "scope": "query-result",
        "canonical_json": artifact.canonical_hash_input(),
    }
    capture["provenance"] = {
        "source_id": "acceptance",
        "run_id": "inv-" + "1" * 32,
        "capture_id": "capture-" + "2" * 32,
        "sql": "SELECT * FROM public.acceptance LIMIT 101",
        "limits": {"max_rows": 100},
        "started_at": "2026-09-12T00:00:01+00:00",
        "finished_at": "2026-09-12T00:00:03+00:00",
    }
    run = {
        "id": "inv-" + "1" * 32,
        "source_id": "acceptance",
        "created_at": "2026-09-12T00:00:00+00:00",
        "finished_at": "2026-09-12T00:00:04+00:00",
        "evidence": [capture],
    }
    return capture, run, rows, columns


def test_acceptance_verifies_typed_database_values_and_export_digest():
    verify_capture_values(*fixture_capture())


@pytest.mark.parametrize("mutation", ["display", "digest", "canonical", "source", "column", "time"])
def test_acceptance_rejects_evidence_mismatch(mutation):
    capture, run, rows, columns = fixture_capture()
    if mutation == "display":
        capture["rows"][0][0] = "999.0000"
    elif mutation == "digest":
        capture["evidence_hash"] = "sha256:" + "0" * 64
    elif mutation == "canonical":
        content = json.loads(capture["integrity"]["canonical_json"])
        content["rows"][0][0] = {"$decimal": "999.0000"}
        text = json.dumps(content, sort_keys=True, separators=(",", ":"))
        capture["integrity"]["canonical_json"] = text
        capture["evidence_hash"] = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    elif mutation == "source":
        capture["provenance"]["source_id"] = "wrong-source"
    elif mutation == "column":
        columns = ("incorrect", *columns[1:])
    else:
        capture["provenance"]["finished_at"] = "2026-09-11T00:00:00+00:00"
    with pytest.raises(AssertionError):
        verify_capture_values(capture, run, rows, columns)


def test_failure_diagnostics_keep_stage_and_code_but_drop_raw_fields():
    private_marker = "credential-that-must-not-appear"
    run = {
        "id": "inv-" + "1" * 32,
        "status": "failed",
        "last_event_id": 2,
        "error": {"code": "model_citation_invalid", "message": private_marker},
        "evidence": [],
    }
    events = (
        'data: {"id":1,"type":"stage_started","data":{"stage":"reconcile"}}\n\n'
        'data: {"id":2,"type":"failed","data":{}}\n\n'
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=events)),
        base_url="http://127.0.0.1",
    ) as client:
        record = {"runs": []}
        with pytest.raises(AssertionError) as error:
            completed(client, run, "first", record)
    assert private_marker not in str(error.value)
    assert private_marker not in json.dumps(record)
    assert record["runs"][0]["stage"] == "reconcile"
    assert record["runs"][0]["error_code"] == "model_citation_invalid"
    assert not record["runs"][0]["harness_deadline_reached"]


def test_harness_deadline_is_distinct_and_does_not_cancel_or_resubmit():
    requests = []
    run = {
        "id": "inv-" + "1" * 32,
        "status": "running",
        "last_event_id": 0,
        "error": None,
        "evidence": [],
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: requests.append(req)), base_url="http://127.0.0.1"
    ) as client:
        record = {"runs": []}
        with pytest.raises(AssertionError):
            completed(client, run, "first", record, deadline_seconds=0)
    assert record["runs"][0]["harness_deadline_reached"]
    assert requests == []


@pytest.mark.parametrize(
    "classification,references", [("supported", []), ("possible", ["wrong"]), ("invented", [])]
)
def test_answer_contract_is_not_weakened(classification, references):
    run = {
        "evidence": [{"evidence_hash": "sha256:known"}],
        "answer": {
            "summary": "Fixture assessment",
            "findings": [
                {
                    "claim": "Fixture claim",
                    "classification": classification,
                    "evidence_ids": references,
                }
            ],
            "limitations": [],
        },
    }
    with pytest.raises(AssertionError):
        verify_answer(run)


def test_new_attempt_requires_fresh_capture_even_when_content_hash_matches():
    _, first, _, _ = fixture_capture()
    current = copy.deepcopy(first)
    current.update(
        id="inv-" + "3" * 32, retry_of=first["id"], created_at="2026-09-12T00:00:05+00:00"
    )
    capture = current["evidence"][0]
    capture["provenance"].update(
        capture_id="capture-" + "4" * 32, started_at="2026-09-12T00:00:06+00:00"
    )
    verify_fresh_attempt(first, current, relationship="retry_of")
    capture["provenance"]["capture_id"] = first["evidence"][0]["provenance"]["capture_id"]
    with pytest.raises(AssertionError, match="historical capture identity"):
        verify_fresh_attempt(first, current, relationship="retry_of")
