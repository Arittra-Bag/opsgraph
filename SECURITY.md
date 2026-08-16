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

- Local sample mode with synthetic data
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
- Connected runs are not resumable mid-graph.
- No customer or production data is approved for this release.

## Non-negotiable rules

- Database credentials never enter prompts, traces, evidence, or audit payloads.
- Policy-engine failure is a denial.
- Uploaded SQL is never executed.
- The model cannot authorize a tool or query.
- External inference and tracing remain disabled by default.
