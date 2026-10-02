"""Readable, selectable reports derived exclusively from a saved investigation."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool

from opsgraph.persistence.runs import TERMINAL


class ReportTooLarge(ValueError):
    """The selected report exceeds its bounded output size."""


class ReportOptions(BaseModel):
    """Source content is included only through explicit section selection."""

    model_config = ConfigDict(extra="forbid")
    include_question: StrictBool = False
    include_findings: StrictBool = False
    include_scope: StrictBool = False
    include_sql: StrictBool = False
    include_rows: StrictBool = False


def _clean(value: Any) -> str:
    return "".join(
        character
        for character in str(value if value is not None else "Not recorded")
        if not unicodedata.category(character).startswith("C") or character in "\n\t"
    )


def _text(value: Any) -> str:
    """Escape untrusted text so report prose cannot introduce links or HTML."""
    value = html.escape(" ".join(_clean(value).split()), quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", value)


def _block(value: str, language: str = "text") -> str:
    value = _clean(value)
    longest = max((len(match[0]) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{value}\n{fence}"


def _append_attempt_context(
    lines: list[str], run: dict[str, Any], status: str, partial: bool
) -> None:
    if status not in TERMINAL:
        lines.extend(
            [
                "**In progress:** this snapshot is incomplete. Generate a new report after "
                "the attempt reaches a terminal state.",
                "",
            ]
        )
    elif partial:
        lines.extend(
            [
                "**Partial attempt:** execution did not complete successfully. Retained "
                "captures remain evidence of their original bounded reads, not a completed "
                "investigation conclusion.",
                "",
            ]
        )
    if run.get("legacy_provenance"):
        lines.extend(
            [
                "**Historical record:** some execution provenance was not retained. Current "
                "settings cannot establish this attempt's historical scope.",
                "",
            ]
        )
    parent = run.get("retry_of") or run.get("parent_run_id")
    if parent:
        relation = "Previous attempt" if run.get("retry_of") else "Previous turn"
        lines.extend(
            [
                f"- {relation}: {_text(parent)}",
                "",
            ]
        )


def _append_failure(lines: list[str], run: dict[str, Any], options: ReportOptions) -> None:
    if run.get("error"):
        error = run["error"]
        lines.extend([f"- Recorded failure category: {_text(error.get('code'))}", ""])
        if options.include_findings:
            lines.extend(["## Recorded failure", "", _text(error.get("message")), ""])
            for step in (error.get("diagnostic") or {}).get("steps", ()):
                lines.append(f"- {_text(step)}")
            lines.append("")


def _append_scope(lines: list[str], configuration: dict[str, Any]) -> None:
    lines.extend(["## Recorded execution scope", ""])
    if not configuration:
        lines.extend(["Execution scope was not retained for this attempt.", ""])
    else:
        bounds = configuration.get("limits") or {}
        fields = {
            "Source": configuration.get("source_id"),
            "Hosting guidance selected": configuration.get("source_hosting_profile"),
            "Permitted tables": ", ".join(bounds.get("allowed_tables") or ()) or "Not recorded",
            "Rows per query": bounds.get("max_rows"),
            "Query timeout in milliseconds": bounds.get("timeout_ms"),
            "Maximum queries": configuration.get("max_queries"),
            "Playbook": configuration.get("skill_id"),
            "Playbook version": configuration.get("skill_version"),
            "Provider": configuration.get("provider"),
            "Configured model": configuration.get("model"),
            "Source revision": configuration.get("source_revision"),
            "Schema fingerprint": configuration.get("schema_fingerprint"),
            "Schema inspected": configuration.get("schema_inspected_at"),
        }
        lines.extend(f"- {label}: {_text(value)}" for label, value in fields.items())
        lines.extend(
            [
                "",
                "These are recorded execution settings. Current source settings may "
                "differ. A hosting choice is guidance, not provider certification.",
                "",
            ]
        )


def _append_assessment(lines: list[str], answer: dict[str, Any], captured_ids: set[str]) -> None:
    lines.extend(["## Model assessment", ""])
    if not answer:
        lines.extend(["No completed model assessment was recorded in this snapshot.", ""])
        return
    lines.extend([_text(answer.get("summary")), ""])
    findings = answer.get("findings") or []
    for index, finding in enumerate(findings, 1):
        lines.extend(
            [
                f"### Finding {index}: {_text(finding.get('classification', 'unknown'))}",
                "",
                _text(finding.get("claim")),
                "",
            ]
        )
        refs = finding.get("evidence_ids") or []
        if not refs:
            lines.append("No evidence reference recorded. Treat this claim as unsupported.")
        for reference in refs:
            qualifier = "Capture reference" if reference in captured_ids else "Unresolved reference"
            lines.append(f"- {qualifier}: {_text(reference)}")
        lines.append("")
    if not findings:
        lines.extend(["No individual findings were recorded.", ""])
    lines.extend(["## Recorded limitations", ""])
    lines.extend(
        f"- {_text(item)}"
        for item in answer.get("limitations")
        or ("No additional limitation recorded. This does not establish completeness.",)
    )
    lines.append("")


def _append_captures(
    lines: list[str], evidence: list[dict[str, Any]], options: ReportOptions, partial: bool
) -> None:
    lines.extend(["## Evidence ledger", ""])
    if not evidence:
        lines.extend(["No captured evidence was recorded in this snapshot.", ""])
    for index, item in enumerate(evidence, 1):
        provenance = item.get("provenance") or {}
        lines.extend(
            [
                f"### Capture {index}",
                "",
                f"- Capture identity: {_text(provenance.get('capture_id'))}",
                f"- Evidence reference: {_text(item.get('evidence_hash'))}",
                f"- Collected: {_text(provenance.get('finished_at') or item.get('created_at'))}",
                f"- Captured rows: {len(item.get('rows') or [])}",
                f"- Truncated: {'Yes' if item.get('truncated') else 'No'}",
                f"- Capture status: {'Partial attempt' if partial else 'Recorded capture'}",
                "",
            ]
        )
        if options.include_findings and item.get("purpose"):
            lines.extend([f"Purpose: {_text(item['purpose'])}", ""])
        if options.include_sql:
            executed = provenance.get("sql")
            lines.extend(
                [
                    "Executed SQL:",
                    "",
                    _block(executed, "sql")
                    if executed
                    else "Executed SQL was not retained for this capture.",
                    "",
                ]
            )
        if options.include_rows:
            data = {"columns": item.get("columns") or [], "rows": item.get("rows") or []}
            lines.extend(
                [
                    "Captured records:",
                    "",
                    _block(json.dumps(data, ensure_ascii=False, indent=2), "json"),
                    "",
                ]
            )


def build_incident_report(run: dict[str, Any], options: ReportOptions) -> dict[str, Any]:
    """Use an immutable saved snapshot, never current configuration or fresh evidence."""
    status = run.get("status", "unknown")
    evidence = run.get("evidence") or []
    answer = run.get("answer") or {}
    configuration = run.get("configuration") or {}
    partial = status != "completed"
    lines = [
        "# PostgreSQL investigation report",
        "",
        f"- Investigation: {_text(run.get('id'))}",
        f"- Recorded state: {_text(status)}",
        f"- Started: {_text(run.get('created_at'))}",
        f"- Snapshot updated: {_text(run.get('updated_at'))}",
        f"- Finished: {_text(run.get('finished_at'))}",
        f"- Captures retained: {len(evidence)}",
        "",
        "This report is a selected view of a saved investigation. Model assessments are not "
        "independently verified. Citations identify referenced captures and do not prove "
        "support, causality, completeness, or authenticity.",
        "",
    ]
    _append_attempt_context(lines, run, status, partial)
    _append_failure(lines, run, options)
    if options.include_question:
        lines.extend(["## Question", "", _text(run.get("question")), ""])
    if options.include_scope:
        _append_scope(lines, configuration)
    captured_ids = {item.get("evidence_hash") for item in evidence}
    if options.include_findings:
        _append_assessment(lines, answer, captured_ids)
    _append_captures(lines, evidence, options, partial)
    included = [
        name.removeprefix("include_") for name, value in options.model_dump().items() if value
    ]
    omitted = [
        name.removeprefix("include_") for name, value in options.model_dump().items() if not value
    ]
    lines.extend(
        [
            "## Report selection",
            "",
            f"- Included content: {', '.join(included) or 'Metadata and evidence ledger only'}",
            f"- Omitted content: {', '.join(omitted) or 'None'}",
            "",
            "Review all included text before sharing. This report does not automatically "
            "redact sensitive source content. It contains no credential configuration. "
            "The result digests describe captured query results, not this report or the "
            "truth of its conclusions. Use the original JSON export for retained canonical "
            "hash input and detailed provenance.",
            "",
        ]
    )
    markdown = "\n".join(lines)
    if len(markdown.encode("utf-8")) > 2_000_000:
        raise ReportTooLarge(
            "Selected report exceeds its size limit. Omit captured rows or SQL and retry."
        )
    identifier = re.sub(r"[^A-Za-z0-9_-]", "_", str(run.get("id", "investigation")))[:128]
    return {
        "report_version": 1,
        "filename": f"{identifier}-report.md",
        "run_id": run.get("id"),
        "snapshot_updated_at": run.get("updated_at"),
        "run_status": status,
        "included_sections": included,
        "markdown": markdown,
    }
