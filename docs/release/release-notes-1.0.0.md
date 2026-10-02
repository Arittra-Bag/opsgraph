# OpsGraph 1.0 — v1.0.0

OpsGraph 1.0 is the first stable release inside a deliberately narrow boundary:
one trusted operator, a private self-hosted instance, PostgreSQL sources, and
bounded read-only investigations.

## What changed

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

Use only artifacts that match `v1.0.0` and verify the release-level
`SHA256SUMS` before installation. Native CPython 3.11 bundles are published for
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
