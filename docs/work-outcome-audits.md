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
benchmark.

## Browser workflow

Open **Export audits** in the existing workspace. Select a reviewed contract and
enter a tenant, acceptance window and cutoff. Every date/time field is labelled
UTC. Run the audit to see all six full-population totals, affected customers,
unknowns and the bounded overdue list. The source must pass normal readiness
checks; a model is not required for this explicit audit.

Use **Check these same exports again** to submit a later cutoff with the saved contract,
tenant and window. Changes in overdue counts are not themselves evidence of
recovery. The comparison counts previously overdue IDs with captured readiness
in the later examples; incomplete membership is labelled as a lower bound. It
does not establish causality. Population-count changes block that comparison;
equal counts alone do not prove the entire population's identities were stable.

**Developer evidence** retains the SQL, original captures and calculation.
**Open saved evidence / report** uses the existing investigation evidence and
selectable Markdown report workflow. Contract audits are labelled as application
calculations. **Export audit JSON** retains the original evidence hash and the
separate calculation hash. Sensitive data may be present in any export.

Operator setup remains explicit: use the reviewed-contract JSON form after
configuring, inspecting and verifying the source. The confirmation covers the
promise grain, joins, deadline, terminal vocabulary, legitimate cancellations
and authoritative completeness meaning. Editing JSON clears confirmation.
Versions are immutable; use a new version after source/schema changes.

## Run your own synthetic smoke test

This development-only script requires a source checkout with its installed
dependencies, plus local PostgreSQL 15–18 or local Docker Desktop. It reuses the
existing isolated practice-database machinery. It never uses an ambient database
connection, existing OpsGraph configuration, or a model. If it uses Docker, the
existing practice setup may pull its pinned PostgreSQL image.

From the repository root, after the normal `python Start.py --yes --install-only`
installation:

```sh
PYTHONPATH=src .venv/bin/python scripts/missed_work_demo.py
```

On Windows PowerShell, use `$env:PYTHONPATH="src"` and
`.venv\Scripts\python.exe scripts/missed_work_demo.py`. The command runs in the
foreground, opens a browser with the existing one-use connection handoff, and
creates a fresh private temporary workspace and database service. With
`--no-browser`, connect using the private key in that temporary workspace's
`.env`, as printed by the launcher; never paste the key into a URL.

1. Open **Export audits**, then open its saved completed synthetic audit. Confirm
   **1,000 accepted promises**, **12 overdue**, **8 affected customers**, **930
   ready**, **20 cancelled**, **5 terminal failures**, **30 within deadline**, and
   **3 unknown**. All 12 overdue IDs fit in the example list. There are 96 failed
   attempt records; these do not become 96 missed promises.
2. Recheck at **2026-10-11 12:05 UTC**. Confirm **0 overdue**, **942 ready**, and
   **1,000 accepted promises**. The other four state totals are unchanged. The
   comparison identifies the 12 previously overdue IDs as ready. Their captured
   `breach` stays `true`. The new request accepted at 12:02 is outside the original
   acceptance window and cannot inflate this cohort's recovery count.
3. Inspect the SQL and calculation; export JSON. Open saved evidence/report,
   select the sections you want, and inspect the preview before downloading or
   copying it. Reopen the original saved audit to confirm its evidence remains
   unchanged.
4. Stop with **Ctrl+C** in the demo terminal. The script stops its isolated
   practice service and removes its temporary workspace. Save any exports you
   want before stopping. It does not stop any existing database/server.

The records are invented and the fixed dates are a historical replay. The ready
events already exist at 12:01; changing the cutoff reveals them. No repair or
remediation is performed. Screenshots from the local browser smoke show the
[baseline](assets/missed-work/baseline.jpg) and
[later recheck](assets/missed-work/recheck.jpg).

## Readiness refresh and existing reviews

A successful bounded readiness check does not change the reviewed export rules.
New reviews record the source access settings separately from probe metadata.
Existing contract records and their original review receipts are never rewritten.
A readiness refresh records the previous source revision and unchanged access
settings in the same transaction, so an existing review can remain usable.
Changed access settings or schema inspection still require a new reviewed version.

An older review already invalidated by a refresh before this compatibility fix
has no recorded link establishing unchanged access. It remains blocked: explicitly
review a new contract version rather than edit the original record or receipt.
Saved audit calculations, query captures and hashes remain unchanged.
