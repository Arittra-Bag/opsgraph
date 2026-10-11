# Opt-in customer export audits

This development workflow extends the existing private, single-operator PostgreSQL
boundary. Ordinary V1 investigations retain their current model, policy and source
checks. An explicit `work_outcome` request uses an operator-reviewed contract and
deterministic application calculations; it never calls a model.

The source must already be configured, inspected and verified by the existing
bounded readiness workflow. Use a dedicated read-only database role and approved
relations. Contract validation cannot prove business meanings or source completeness.
Ready database records do not prove that a customer can access an artifact.

## Review a contract

`POST /api/work-outcomes/contracts` accepts:

```json
{
  "definition": {"...": "a complete WorkOutcomeContract"},
  "confirm_reviewed_meanings": true
}
```

Use the latest inspected source schema fingerprint. All mapped relations must be
within current source and policy scope, every column must be inspected, timestamp
columns must be timezone-aware, and tenant/promise join types must agree. The
synthetic test contract is an example vocabulary, not a production definition.
After reviewing meanings, save a version. Saved versions are immutable and bound
to the reviewed source revision. Review a new version after configuration/schema
changes; edits to persisted definitions without matching audit receipts cannot run.
`GET /api/work-outcomes/contracts` lists this workspace's reviewed definitions.

## Run an audit

`POST /api/runs` accepts the existing fields plus an explicit scope:

```json
{
  "question": "Which customer exports should already be ready?",
  "source_id": "exports-db",
  "work_outcome": {
    "contract_id": "customer-exports",
    "version": 1,
    "tenant_id": "tenant-a",
    "start": "2026-10-11T11:00:00Z",
    "end": "2026-10-11T12:00:00Z",
    "as_of": "2026-10-11T12:00:00Z"
  }
}
```

The accepted-at population is `[start, end)` for one tenant, at most 31 days, with
`end <= as_of`. Explicit timezones are required. The question labels the audit; it
does not change the contract or SQL semantics. Each audit has one generated,
AST-validated SELECT through the existing broker and forced read-only executor.
Source/policy/playbook limits still apply, along with fresh physical-schema checks,
credential-reference checks, cancellation, durable events and restart recovery.
The audit does not enable new SQL functions or widen permissions.

The fact pack retains six full-population state totals and five required quality/
scope rows. It requires at least 11 retained rows. Remaining room, up to 50 rows,
holds examples ordered with overdue work first. Total counts are computed before
sample limits; the example list is never the population. Missing/duplicate promise
identities block the result. Unknown statuses, contradictory terminal records,
ambiguous deadlines and incomplete coverage remain visible. A recent timestamp
alone is not an authoritative coverage watermark.

The saved answer contains `work_outcome`: method version, input evidence hashes,
frozen definition/scope, checked facts, example-completeness flags and a separate
calculation hash. Query-result hashes remain unchanged. Digests check their covered
data structures; they do not authenticate database origin or establish causality.
JSON exports preserve both original capture integrity and the separate calculation.

Recheck by submitting the same contract/tenant/window with a later explicit cutoff
and `parent_run_id` referencing the completed audit. This rereads the database;
historical captures are preserved. Retries retain the original explicit scope and
revalidate current permissions. New accepted requests outside the original window
cannot inflate that cohort's recovery count. Late readiness/cancellation preserves
historical deadline-breach information when coverage supports it.

## Validation boundary

Independent expected results live under `tests/missed_work`. The real PostgreSQL
tests provision temporary relations and a dedicated SELECT-only role on a caller-
supplied loopback test database, then clean up only those temporary resources.
Set `OPSGRAPH_MISSED_WORK_OWNER_DSN` and run:

```sh
.venv/bin/python -m pytest -q tests/missed_work
```

The existing connected CI job runs these PostgreSQL comparisons. Offline fixture
jobs skip live tests explicitly. None of this is a model-quality or competitor
benchmark. The UI and self-contained practice/smoke flow are a separate PR.
