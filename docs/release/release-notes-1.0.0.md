# OpsGraph 1.0 release candidate

These notes describe the intended stable release inside a deliberately narrow boundary:
one trusted operator, a private self-hosted instance, PostgreSQL sources, and
bounded read-only investigations.

Publication is pending the exact-revision gates in production readiness.
The package version alone does not establish a published stable release.

## What changed

- Model readiness requires a real connection check. Checks expire after 15
  minutes and are invalidated by new tests, configuration changes, or restart.
  Concurrent and failed checks cannot restore an older successful result.
- Incident Markdown exports preserve readable quotes, timestamps, and ordinary
  punctuation while retaining protection against report markup injection.

- A four-step first-run path connects the workspace, inspects an exact source,
  tests a real model configuration, and requires an operator-approved bounded
  database readiness read.
- Source setup generates least-privilege PostgreSQL role SQL for administrator
  review. OpsGraph never executes the guide or creates a password.
- Remote PostgreSQL requires certificate and hostname verification by default.
  Effective relation and column privileges, inherited roles, ownership, and
  `PUBLIC` grants are checked before a source is accepted.
- Query execution binds the schema fingerprint and SQL to one repeatable-read,
  read-only snapshot.
- Provider settings expose the preset, adapter, exact model, profile, reasoning
  option, timeout, output-token bound, endpoint, and egress choice. API keys
  remain write-only.
- Durable runs fail visibly when projection, audit, or terminal-state recording
  cannot complete. Startup recovers interrupted work without replaying database
  queries.
- The readiness endpoint validates local state schemas, rollback-only writes,
  and coordinator health.
- Linux CI exercises a connected PostgreSQL 17.7 control path with a real
  least-privilege role and deterministic model-protocol fixture. Native package
  and bundle checks continue on Linux, macOS, and Windows.

## Upgrade notes

Back up the complete private workspace before upgrading. Existing sources must
be inspected again and pass the new readiness check. Open and re-save the
provider configuration, then run the actual model probe. Remote PostgreSQL DSNs
need `sslmode=verify-full` unless the deployment deliberately enables the
documented compatibility override.

Read [migration](../migration.md), [installation](../installation.md), and the
[support matrix](support-matrix.md) before replacing an existing instance.

## Supported boundary

Keep OpsGraph on loopback or a private network. The workspace key is a local
control, not team identity, SSO, tenant isolation, or enterprise RBAC.
PostgreSQL is the only source connector. Database writes, automatic remediation,
executable plug-ins, model discovery, and automatic provider fallback are not
included.

Citations and hashes identify captured evidence; they do not prove the model's
business interpretation. Review SQL, records, scope, collection time, and
missing definitions before acting on a finding.

## Release artifacts

When `v1.0.0` is published, use matching artifacts and verify the release-level
`SHA256SUMS` before installation. Native CPython 3.11 bundles are intended for
Ubuntu 24.04 x64, Windows Server 2025 x64, and macOS 26 arm64. Each bundle
contains a generated inventory for its exact wheelhouse and has a matching
acceptance receipt.

The application wheel and source distribution are accompanied by a verified
third-party source supplement. The container package contains Linux amd64 and
Linux arm64 images only; the arm64 runtime check uses QEMU on Ubuntu. The
published index is assembled from the exact accepted platform images and its
digest is recorded with both platform digests. It is not a Windows or macOS
container image.

See [stable distribution](distribution.md) for the complete file list, checksum
scopes, source/notice relationship, receipts, and container tag contract.

The final tag gate is recorded in
[production readiness](../production-readiness.md#evidence-required-before-publishing-v100).

## Guided PostgreSQL investigations

- First-class connection guidance for local PostgreSQL, remote or self-hosted
  servers, Supabase, Neon, AWS RDS, Google Cloud SQL, Azure, and DigitalOcean.
  All routes share the existing read-only connector and require actual inspection
  and readiness checks.
- Safe connection diagnostics explain recoverable categories and next steps
  without returning credentials or raw driver errors.
- Selectable incident reports use saved evidence and recorded execution scope.
  Preview, review confirmation, local Markdown download, and copy are available.
  SQL and source records remain excluded until explicitly selected.
- Partial captures remain visible when an investigation ends without a model
  assessment. Report previews are invalidated when their saved snapshot changes.

Managed-service guidance is not provider certification. Network routes, database
grants, certificate trust, cloud resources, and token renewal remain separately
managed by the operator. Model assessments still require evidence review.

## Guided first run from source

- A single `python3 Start.py` command installs locked dependencies and opens the
  private workspace. Windows uses `py Start.py`. Downloads require consent.
- Quick setup uses recommended defaults. Advanced setup exposes schema, endpoint,
  output-profile, reasoning and timeout controls. Both show a credential-free
  review before saving and require explicit consent for external model egress.
- Terminal setup offers the same named provider presets as browser Settings.
  Source inspection, bounded readiness and real model probing remain mandatory.
- Missing database credentials can be deferred to the browser's administrator
  role guide. Cancelling or retrying setup preserves existing private configuration.
- Hosted guidance clarifies Supabase address-family/pooler choices and DigitalOcean
  networking, database/user selection and cluster-CA configuration.

## Optional-child count correctness

- Planning guidance distinguishes actual child rows from an empty outer-join row.
  Empty parents should have zero child rows and zero missing measurements.
- A narrow pre-execution check rejects wildcard or constant counts of synthetic
  outer-join rows for explicit child-row and NULL-value metrics on an
  operator-defined relationship. The existing single correction attempt applies,
  then an inconsistent plan stops without querying.
- A measurement threshold with an explicitly unavailable source-unit definition
  requires clarification before inference or querying. Requested units are never
  treated as a definition of the stored values by this check.
- Real-model metering scenarios now include a probe with no readings. Exact-result
  checks cover its zero counts and NULL minimum and maximum values.

These checks do not prove general query correctness. Review model conclusions
against the recorded SQL, bounded source values and operator definitions.
