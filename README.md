# OpsGraph

**Self-hosted PostgreSQL investigations with inspectable evidence.** Ask a
question, follow real execution and inspect the SQL and records behind a finding.

[![CI](https://github.com/Arittra-Bag/opsgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/Arittra-Bag/opsgraph/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/source-Apache--2.0-65d6ce.svg)](LICENSE)

OpsGraph is a FOSS browser workspace for one operator, built with FastAPI,
LangGraph and SQLite. It runs on your machine or your own server. PostgreSQL is
the supported connector implementation. Local Ollama inference needs no paid
account; Anthropic and OpenAI-compatible services use explicit backend
configuration. Credentials stay on the backend.

OpsGraph is beta software. Review the
[support matrix](docs/release/support-matrix.md) before installing; it separates
tested configurations from combinations that remain unverified.

## What you can inspect

- Actual source access, selected schema/table scope and discovered columns.
- Backend-driven execution, saved investigations and per-query captures.
- Cited SQL, source identity, timestamps, records and collection limits.
- Findings, contradictions, missing evidence and model uncertainty.
- Exported evidence, linked follow-ups and explicit retries with fresh queries.

Normal product paths contain no sample replay, invented progress or deterministic
model fallback. A valid query, citation or hash does not prove an interpretation
true. Missing units, status meanings, time semantics or join relationships need
context or clarification; the model may still make mistakes. Inspect the records
before acting on a finding.

## Install and start

The tested beta configuration is **macOS 26.2 arm64 with CPython 3.11**. Download
the matching bundle and checksum from the GitHub release. Other environments have
narrower evidence; check the [support matrix](docs/release/support-matrix.md).

Prerequisites: Python 3.11, a modern browser, an authorized PostgreSQL read-only
login and an available model. Allow 2 GiB of installation headroom plus model
storage. `qwen3:8b` has an approximately 5.23 GB model layer and needs additional memory
for inference. Downloads and model startup are separate from installation.
If needed, deliberately install Ollama and run `ollama pull qwen3:8b` after
reviewing its download size. OpsGraph does not download models or create database
credentials for you.

Put the matching ZIP and adjacent `.sha256` file in the same directory, then:

```sh
shasum -a 256 -c opsgraph-0.1.0b1-macos-arm64-cp311.zip.sha256
unzip opsgraph-0.1.0b1-macos-arm64-cp311.zip
cd opsgraph-macos-arm64-cp311
python3.11 -I Install.py install
python3.11 -I Install.py launch
```

The installer checks bundled hashes and installs locked dependencies offline.
The launcher guides backend source/model setup, creates a private workspace key
and opens the connected browser. No checkout or manual configuration-file edits
are needed. Later launches use the same command and saved history. Ctrl+C in the
launch terminal stops the backend. An occupied port is reported; choose another
with `python3.11 -I Install.py launch --port 8010`.

In **Sources**, enter explicit schema-qualified tables and select **Inspect**.
Review the returned columns and unavailable business meanings. In **Settings**,
run **Test actual model connection**: this makes a real structured request without
source records. Select the general read-only playbook and ask a bounded question.
Open a finding's citation to inspect its SQL and records, then export if needed.

[Quick start](docs/quickstart.md) · [Installation and troubleshooting](docs/installation.md)
· [Upgrade, backup, restore and uninstall](docs/maintenance.md)
· [Migration](docs/migration.md)

The Mac bundle is unsigned. Do not disable system security globally to run it.
Windows Server/Ubuntu bundles have hosted installation/lifecycle evidence, not
full PostgreSQL/model acceptance. Windows 11 remains unverified. Container
configuration checks are separate from actual container investigations; see the
[support matrix](docs/release/support-matrix.md) before choosing a path.

## Investigation limits and recovery

Up to three SELECT queries, 100 captured rows per query and five seconds per
query are allowed; playbooks may impose stricter limits. PostgreSQL AST validation,
approved table scope, role checks and read-only transactions enforce access.
Database writes, automatic remediation, team accounts, CDC and executable
third-party plugins are outside this beta.

Reload/reconnect attaches to the saved run. Cancellation stops further scheduling
and waits for the active operation to exit. Interrupted runs preserve captures;
retry creates a new attempt instead of automatically replaying old queries.
Follow-ups link to prior investigations and collect fresh evidence.

A narrow conflict between a model answer and capture facts can trigger one
answer-only correction using the same evidence. It does not rerun SQL. A second
inconsistent answer fails visibly and preserves the evidence. This guard is not
a general semantic verifier.

## Develop and validate

```sh
uv sync --locked --all-extras
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python -m pytest -q
uv build --build-constraints requirements-build.lock --require-hashes
uv run python scripts/wheel_smoke.py
```

Ordinary tests use fixtures. Real PostgreSQL/model acceptance is opt-in and
requires dedicated disposable resources; it is never replaced by fixtures.

Read [SECURITY.md](SECURITY.md) before connecting data. Source records, questions,
SQL and exports can be sensitive; protect the workspace and backups. OpsGraph's
own source remains [Apache-2.0](LICENSE). Dependencies retain their licenses;
the integrated bundle's GPL/LGPL distribution obligations and accompanying
source are explained in [beta distribution](docs/release/beta-distribution.md).
