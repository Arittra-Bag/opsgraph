# Readable incident reports

Open a saved investigation and choose **Create report**. Select the question,
findings and limitations, execution scope, executed SQL, or captured records.
Generate a readable preview with optional raw Markdown, review it, then confirm that you have checked the
selected content before copying or downloading it. Changing any selection
invalidates the preview and its sharing confirmation.

The browser initially selects question, findings, and scope. SQL and captured
records are excluded until selected. All sections can contain sensitive
information, including names, questions, claims, and failure descriptions.
There is no automatic source-content redaction. Review the full preview for the
intended recipient. The API defaults every optional section to excluded.
Metadata and the evidence ledger remain present, including run identity,
state, timestamps, capture identities, result digests, row counts, and truncation.

Reports use only the saved run snapshot. Creating one makes no database or
model request, changes no saved evidence, and contacts no sharing service.
Downloads stay local. The report records its saved state and snapshot time.
An investigation that changes while the preview is open requires a new preview
before sharing. A completed report does not establish a complete dataset or a
correct interpretation.

## What a report establishes

- Recorded execution scope describes that attempt. Current source settings
  cannot fill missing historical provenance.
- Model classifications remain assessments. References identify captures,
  not independent proof of support, contradiction, or causality.
- Missing references are labeled unresolved. Findings without references are
  explicitly unsupported.
- Failed, blocked, cancelled, or interrupted attempts retain partial evidence.
  In-progress reports are incomplete snapshots, not completed conclusions.
- Executed SQL is included only when retained. Proposed SQL never substitutes
  for a missing executed query.
- Result hashes describe retained query-result bytes. They do not authenticate
  the report, its provenance, or the truth of a conclusion.

Untrusted prose is escaped and SQL or record blocks use bounded section fences.
Quotes, timestamps, identifiers, and ordinary punctuation remain readable in
the Markdown source. Escaping protects report structure without modifying the
saved evidence or canonical JSON export.
Reports contain no credential configuration, provider endpoints, or canonical
hash-input dump. Source content can still contain secrets if a selected question,
query, claim, or record contains them. Use the original JSON export for detailed
provenance and retained canonical hash input.

## API

`POST /api/runs/{run_id}/report` requires the workspace key and scopes the run to
that workspace. It returns `report_version`, `filename`, `run_id`,
`snapshot_updated_at`, `run_status`, `included_sections`, and `markdown`.

```json
{
  "include_question": true,
  "include_findings": true,
  "include_scope": true,
  "include_sql": false,
  "include_rows": false
}
```

Selections require actual JSON booleans. Unknown options are rejected. Responses
are not cached. Output is limited to 2 MB. If a selection exceeds the limit,
omit records or SQL and generate the report again. The underlying saved run
and JSON export are unchanged.
