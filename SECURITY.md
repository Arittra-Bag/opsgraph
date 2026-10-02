# Security policy

OpsGraph 1.0 supports a single trusted operator on a private host. Do not expose
it directly to the public internet. Connect only sources and data the operator is
authorized to inspect, through a dedicated least-privilege PostgreSQL role.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through the
[GitHub security advisory form](https://github.com/Arittra-Bag/opsgraph/security/advisories/new).
Do not open a public issue with exploit details, credentials or captured source
records. Include the affected version and a sanitized description privately.

## Supported versions

| Version | Security maintenance |
| --- | --- |
| Latest published `1.0.x` | Supported; update to the latest patch for fixes |
| Development on `main` | Fixes land here before the next release |
| `0.1.x` beta and `0.1.0-alpha.*` builds | Not maintained; upgrade |

Security maintenance is best effort and does not establish an SLA or response-time
guarantee. Release-specific support evidence is recorded in the
[support matrix](docs/release/support-matrix.md).

## Supported boundary

- Actual connected PostgreSQL investigations; public sample execution is retired
- PostgreSQL schema-only SQL parsed as data, never executed
- Live PostgreSQL discovery and SELECT execution through a separately provisioned,
  verified read-only role and read-only transactions
- Read-only query plans passing deterministic policy and PostgreSQL AST brokers
- Declarative built-in and custom skills only

OpsGraph 1.0 does not support executable plug-ins, write operations, automatic
remediation, full database dumps, archive extraction, or transparent fallback
to cloud inference.

## 1.0 limitations

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
- Remote PostgreSQL requires `sslmode=verify-full` by default. The
  `OPSGRAPH_ALLOW_INSECURE_REMOTE_POSTGRES` compatibility override weakens that
  transport requirement and must be an explicit deployment decision.
- Production use is supported only inside the documented single-operator,
  private deployment boundary. Team and public-service deployments are outside
  the security model.

## Non-negotiable rules

- Database credentials never enter prompts, traces, evidence, or audit payloads.
- Policy-engine failure is a denial.
- Uploaded SQL is never executed.
- The model cannot authorize a tool or query.
- External inference and tracing remain disabled by default.
