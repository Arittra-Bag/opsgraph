# Migration from the replay alpha

Back up private configuration and state before upgrading. Keep an untouched
copy and validate the new version against a separate state copy first. No
migration grants access to a database or model implicitly.

## Configuration

The product now requires `OPSGRAPH_MODE=connected`, a random workspace key of at
least 24 characters, and a real model provider. `opsgraph init` generates private
configuration only when `.env` does not exist; it never overwrites an earlier
installation. Edit an existing file privately after reviewing these changes:

- Replace the old shared/sample key with a randomly generated secret. Re-enter
  the new key in the browser. Do not reuse public examples.
- Replace `deterministic` with `openai_compatible` and configure a running local
  model such as Ollama with `qwen3:8b`. External inference remains optional and
  requires deployment and source-level egress approval.
- Configure a dedicated read-only PostgreSQL DSN on the backend. The browser
  stores only an allowlisted environment-variable reference.
- Select explicit schema-qualified tables and inspect the source. The general
  read-only playbook needs no specialist evidence bindings; specialist
  playbooks still enforce their source-owned mappings.

A sample/offline configuration must display setup guidance and block live
execution until explicitly updated. It must never start querying stored
credentials automatically. Public `POST /api/investigations/sample` now returns
`410 Gone`; deterministic runner helpers remain available for automated tests.

## Existing records and new runs

Existing real evidence/results stay available in local storage. Historical
records lacking capture provenance must say that provenance is unavailable;
upgrades cannot invent source identity, timestamps or SQL for them. Earlier
synthetic records are not converted into real investigations or mixed into live
history. Keep the backup if historical sample inspection is needed.

New captures include the exact canonical input used for their existing content
hash, allowing exported typed values such as decimals to be verified. Earlier
captures cannot be backfilled reliably from JSON display rows and are labeled
unavailable for this verification method. Their original bytes are preserved.

The synchronous real `/api/investigations` API remains supported. New clients
can use `/api/runs` for durable submission, sequenced events, cancellation,
retry, follow-up links and export. A reconnect attaches to an existing run;
a retry creates a separate attempt and collects fresh evidence. Queued work
can survive restart. Previously active work becomes interrupted, preserving
completed captures without automatically repeating queries.

Run only one backend coordinator per state file. Prepare rollback by retaining
the previous wheel and dependency lock together with the pre-upgrade backup.
Do not point an older binary at a migrated live database as a rollback shortcut.
