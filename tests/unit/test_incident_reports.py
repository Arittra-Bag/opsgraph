import copy

import pytest
from pydantic import ValidationError

from opsgraph.reports import ReportOptions, ReportTooLarge, build_incident_report


@pytest.fixture
def record():
    return {
        "id": "run-123",
        "status": "completed",
        "question": "Sensitive question",
        "created_at": "2026-10-02T10:00:00Z",
        "updated_at": "2026-10-02T10:01:00Z",
        "configuration": {
            "source_id": "database",
            "source_hosting_profile": "neon",
            "limits": {"allowed_tables": ["public.records"], "max_rows": 10},
            "secret_ref": "FORBIDDEN_REFERENCE",
            "dsn": "FORBIDDEN_DSN",
            "api_key": "FORBIDDEN_KEY",
            "endpoint": "FORBIDDEN_ENDPOINT",
        },
        "answer": {
            "summary": "Sensitive summary",
            "findings": [
                {
                    "claim": "Sensitive claim",
                    "classification": "supported",
                    "evidence_ids": ["hash-123", "missing-hash"],
                }
            ],
            "limitations": ["Bounded sample"],
        },
        "evidence": [
            {
                "evidence_hash": "hash-123",
                "purpose": "Sensitive purpose",
                "columns": ["private_value"],
                "rows": [["Sensitive row"]],
                "truncated": True,
                "integrity": {"canonical_json": "FORBIDDEN_CANONICAL"},
                "provenance": {
                    "sql": "SELECT 'Sensitive SQL'",
                    "capture_id": "capture-123",
                    "proposed_sql": "FORBIDDEN_PROPOSED",
                },
            }
        ],
    }


def test_default_report_omits_source_content_and_never_changes_saved_record(record):
    before = copy.deepcopy(record)
    report = build_incident_report(record, ReportOptions())
    assert record == before
    assert report["filename"] == "run-123-report.md"
    assert report["included_sections"] == []
    assert "Evidence ledger" in report["markdown"]
    assert "Sensitive" not in report["markdown"]
    assert "FORBIDDEN" not in str(report)


def test_selected_report_includes_exact_retained_content_and_unresolved_references(record):
    report = build_incident_report(
        record, ReportOptions(**{field: True for field in ReportOptions.model_fields})
    )
    text = report["markdown"]
    assert "Sensitive question" in text
    assert "Sensitive row" in text
    assert "SELECT 'Sensitive SQL'" in text
    assert "Unresolved reference" in text
    assert "missing-hash" in text
    assert "Truncated: Yes" in text
    assert "FORBIDDEN" not in text
    assert "not independently verified" in text
    assert "not provider certification" in text


@pytest.mark.parametrize(
    "status", ["queued", "running", "failed", "blocked", "interrupted", "cancelled", "cancelling"]
)
def test_noncompleted_reports_identify_partial_evidence_without_concluding(record, status):
    record.update(status=status, answer=None)
    text = build_incident_report(record, ReportOptions(include_findings=True))["markdown"]
    assert "No completed model assessment" in text
    assert "Partial attempt" in text
    if status in {"queued", "running", "cancelling"}:
        assert "In progress" in text


def test_legacy_empty_failure_report_is_honest_and_selective():
    record = {
        "id": "../../unsafe",
        "status": "failed",
        "legacy_provenance": True,
        "retry_of": "earlier",
        "error": {
            "code": "source_unavailable",
            "message": "Sensitive failure",
            "diagnostic": {"steps": ["Sensitive step"]},
        },
    }
    report = build_incident_report(record, ReportOptions())
    assert report["filename"] == "______unsafe-report.md"
    assert "Historical record" in report["markdown"]
    assert "Sensitive" not in report["markdown"]
    assert "No captured evidence" in report["markdown"]
    text = build_incident_report(record, ReportOptions(include_findings=True))["markdown"]
    assert "Sensitive failure" in text
    assert "Sensitive step" in text


def test_untrusted_markdown_html_controls_and_fences_cannot_escape_sections(record):
    record["question"] = "[click](javascript:alert(1)) <img src=x onerror=alert(1)>\x00\u202e"
    record["evidence"][0]["provenance"]["sql"] = "SELECT 1;\n```\n# forged\n````"
    text = build_incident_report(record, ReportOptions(include_question=True, include_sql=True))[
        "markdown"
    ]
    assert "[click](javascript:" not in text
    assert "<img" not in text
    assert "\x00" not in text
    assert "\u202e" not in text
    assert "`````sql\n" in text
    assert "\n`````\n" in text


def test_report_preserves_readable_quotes_timestamps_and_identifiers(record):
    record["answer"]["summary"] = "Processor fastpay recorded 'failed' payments."
    record["answer"]["findings"][0]["claim"] = 'The error was "gateway_timeout" (50 payments).'
    before = copy.deepcopy(record)
    text = build_incident_report(record, ReportOptions(include_findings=True, include_scope=True))[
        "markdown"
    ]
    assert "Investigation: run-123" in text
    assert "2026-10-02T10:00:00Z" in text
    assert "Permitted tables: public.records" in text
    assert "Processor fastpay recorded 'failed' payments." in text
    assert 'The error was "gateway\\_timeout" (50 payments).' in text
    assert "&#x27;" not in text
    assert "&quot;" not in text
    assert record == before


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("# Forged heading", "\\# Forged heading"),
        ("- Forged list", "\\- Forged list"),
        ("+ Forged list", "\\+ Forged list"),
        ("---", "\\---"),
        ("===", "\\==="),
        ("1. Forged list", "1\\. Forged list"),
        ("1) Forged list", "1\\) Forged list"),
        ("> Forged quote", "&gt; Forged quote"),
        ("[link](https://example.com)", "\\[link\\](https://example.com)"),
        ("<https://example.com>", "&lt;https://example.com&gt;"),
        ("&lt;script&gt;", "&amp;lt;script&amp;gt;"),
    ],
)
def test_readable_prose_still_blocks_untrusted_markdown_structure(record, value, escaped):
    record["question"] = value
    text = build_incident_report(record, ReportOptions(include_question=True))["markdown"]
    assert f"## Question\n\n{escaped}\n\n" in text


def test_oversized_selection_can_be_retried_without_rows(record):
    record["evidence"][0]["rows"] = [["x" * 2_000_000]]
    options = ReportOptions(include_rows=True)
    with pytest.raises(ReportTooLarge, match="Omit captured rows"):
        build_incident_report(record, options)
    assert build_incident_report(record, ReportOptions())["markdown"]


@pytest.mark.parametrize(
    "options", [{"include_rows": "true"}, {"include_sql": 1}, {"unknown": True}]
)
def test_options_do_not_coerce_sharing_consent(options):
    with pytest.raises(ValidationError):
        ReportOptions(**options)


def test_outcome_report_distinguishes_calculation_and_honors_section_selection(record):
    record["configuration"]["execution_kind"] = "work_outcome"
    record["work_outcome"] = {"tenant_id": "sensitive-tenant", "as_of": "2026-10-11T12:00:00Z"}
    record["answer"]["work_outcome"] = {
        "method": "export-reconciliation-v1",
        "calculation_hash": "separate-calculation-hash",
        "facts": {
            "population": 1000,
            "affected_customers": 8,
            "counts": {"overdue_unresolved": 12, "unknown": 3},
            "overdue_examples_complete": False,
        },
    }
    metadata = build_incident_report(record, ReportOptions())["markdown"]
    assert "separate-calculation-hash" not in metadata and "sensitive-tenant" not in metadata
    findings = build_incident_report(record, ReportOptions(include_findings=True))["markdown"]
    assert "## Export check" in findings and "## Model assessment" not in findings
    assert (
        "Accepted export requests: 1000" in findings
        and "Overdue and still not ready: 12" in findings
    )
    assert "Overdue examples complete: False" in findings
    assert "separate-calculation-hash" in findings and "sensitive-tenant" not in findings
    scope = build_incident_report(record, ReportOptions(include_scope=True))["markdown"]
    assert "sensitive-tenant" in scope and "separate-calculation-hash" not in scope


def test_outcome_report_plain_prose_keeps_saved_snapshot_and_hashes(record):
    import copy

    record["configuration"]["execution_kind"] = "work_outcome"
    record["answer"]["summary"] = (
        "12 overdue unresolved exports across 8 customers; 5 terminal failures "
        "and 3 unknown among 1000 accepted promises."
    )
    record["answer"]["limitations"] = [
        "Outcome definitions and source coverage are operator-reviewed premises, "
        "not verified business truth.",
        "Ready database records do not prove customer access or an incident cause.",
    ]
    before = copy.deepcopy(record)
    report = build_incident_report(record, ReportOptions(include_findings=True))["markdown"]
    assert "accepted export requests" in report
    assert "cannot verify those assumptions" in report
    assert "does not prove the customer can open" in report
    assert "Model assessments" not in report
    assert "operator-reviewed premises" not in report
    assert record == before


def test_pending_outcome_report_does_not_invent_a_model_assessment(record):
    record["configuration"]["execution_kind"] = "work_outcome"
    record["answer"] = None
    report = build_incident_report(record, ReportOptions(include_findings=True))["markdown"]
    assert "No completed export calculation" in report
    assert "model assessment" not in report.lower()
