# Beta threat model

## Protected assets

- Datasource credentials and schema metadata
- Query results and evidence artifacts
- Workspace identity and policy decisions
- Investigation history and audit-chain integrity

## Primary trust boundaries

```text
browser -> authenticated API -> orchestrator -> policy/tool broker
                                             -> source broker -> read-only DB
                                             -> evidence + audit persistence
```

Datasource credentials are resolved from explicitly allowlisted backend
environment references only when a connector is created. They must never enter
model messages, playbooks, browser responses, persisted run state or exports.

## Hostile inputs

Treat prompts, database values, identifiers, comments, logs, uploaded SQL, and
playbook text as untrusted. Stored prompt injection must remain inert data. The
authorization result must be correct even when model output is malicious.

## Current mitigations

- Workspace-scoped API identity
- Reserved `core.*` tool namespace
- Fail-closed policy decisions with enforceable obligations
- Schema-only parser rejecting data and executable database constructs
- Single-statement `SELECT` validation and bounded result obligations
- PostgreSQL AST validation, verified role restrictions, and read-only transactions
- Stable evidence digests and append-only hash-chained audit events
- Backend-held datasource/model credentials and sanitized provider errors
- Same-origin browser API requests with redirects rejected
- Bounded persisted evidence, authenticated event streams and explicit retry

## Deferred controls

Team authentication, encrypted credential storage, isolated source workers,
central policy deployment, signing, an SBOM and external security review are not
provided by this beta. Operators must keep the service private, restrict source
roles and protect the workspace filesystem and backups.
