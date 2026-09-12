# Security policy

OpsGraph Alpha is a public validation build. Do not connect production systems,
upload customer data, or expose it directly to the internet.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through the
[GitHub security advisory form](https://github.com/Arittra-Bag/opsgraph/security/advisories/new).
Do not open a public issue with exploit details. Only the latest `0.1.x` alpha
line is supported; security fixes are released on `main` until a stable release
policy exists.

## Supported boundary

- Actual connected PostgreSQL investigations; public sample execution is retired
- PostgreSQL schema-only SQL parsed as data, never executed
- Live PostgreSQL discovery and SELECT execution through a separately provisioned,
  verified read-only role and read-only transactions
- Read-only query plans passing deterministic policy and PostgreSQL AST brokers
- Declarative built-in and custom skills only

The alpha does not support executable plug-ins, write operations, automatic
remediation, full database dumps, archive extraction, or transparent fallback
to cloud inference.

## Alpha limitations

- Local API-key authentication is not production identity or SSO.
- Connector secrets remain environment-variable references; there is no vault.
- Investigation metadata, skills, and hash-chained audit events use local SQLite.
  The audit head is not externally anchored or cryptographically signed.
- Interrupted runs preserve captured evidence and require explicit fresh retry;
  automatic mid-graph continuation is not supported.
- One narrowly detected answer inconsistency may cause one additional model
  answer call against the same captured evidence. It never reruns planning or
  SQL. A second inconsistent response fails without accepting the prose; this
  guard is not a general factuality or business-semantics check.
- The packaged launcher binds only to 127.0.0.1. Its browser handoff expires
  after 60 seconds and is single-use; the workspace key never enters the URL.
  Normal API access continues to require the workspace key.
- Private workspace files and backups contain credentials and evidence. POSIX
  permission checks do not establish equivalent Windows ACL protection.
- No customer or production data is approved for this release.

## Non-negotiable rules

- Database credentials never enter prompts, traces, evidence, or audit payloads.
- Policy-engine failure is a denial.
- Uploaded SQL is never executed.
- The model cannot authorize a tool or query.
- External inference and tracing remain disabled by default.
