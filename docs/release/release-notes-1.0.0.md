# OpsGraph 1.0.0

OpsGraph is a private, self-hosted workspace for investigating PostgreSQL data.
Ask a question, inspect the SQL and captured records, then continue in the same
conversation. This release supports one trusted operator and bounded read-only
access.

## Changes

- Persistent investigation conversations with multiple turns, fresh follow-up
  queries, retries and retained evidence.
- No-code setup with practice data, an interactive terminal flow and table
  discovery before explicit access approval.
- Connection guidance for local PostgreSQL, Supabase, Neon, AWS RDS, Google
  Cloud SQL, Azure and DigitalOcean, with safe connection diagnostics.
- Readable Markdown incident reports with a preview and a choice of which
  evidence sections to include.
- Backend-held model configuration, explicit consent for external processing
  and a real model connection check before investigating.
- Stronger handling of malformed credentials and policy decisions, outdated
  browser responses and overlapping playbook saves.
- Private temporary practice credentials and the urllib3 2.8.0 security update.
- A release badge that includes earlier prereleases and links to the complete
  release history.

## Getting started

After cloning the repository:

```sh
cd opsgraph
python3 Start.py --no-code
```

On Windows, use `py Start.py --no-code`. Choose practice data or connect your
own database. A model service and a database login with read-only access are
required. PostgreSQL and model files are not installed automatically.

## Upgrading

Back up your complete private workspace first. Reinspect existing sources,
run their readiness checks and test the saved model connection after restarting.
Remote PostgreSQL connections require `sslmode=verify-full` by default.

See the [upgrade guide](../migration.md) and [setup guide](../quickstart.md).

## Packages and validation

The release workflow checks the Python and frontend suites, the connected
PostgreSQL control path, native bundles for Linux, macOS and Windows, and Linux
amd64 and arm64 container images. The arm64 container check runs under QEMU.

Download matching artifacts and verify `SHA256SUMS` before installation.
Packages include the application wheel, source distribution, offline bundles,
third-party source archives and validation receipts. See the
[distribution guide](distribution.md) for platforms, files and checksum scopes.

## Scope and limitations

PostgreSQL is the only database connector. Keep the workspace on loopback or a
private network. Team accounts, database writes and automatic remediation are
not included.

Provider fixtures test the control path, not model answer quality. Hosted
connection guidance does not certify every provider or network configuration.
Review the SQL, captured records and missing business definitions before acting
on a finding. Reports do not automatically redact sensitive data.

See the [support matrix](support-matrix.md) for tested environments and limits.
