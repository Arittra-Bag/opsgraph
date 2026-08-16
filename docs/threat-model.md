# Alpha threat model

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

Only the source-broker boundary may eventually decrypt datasource credentials.
The API, model, playbooks, and browser must never receive them.

## Hostile inputs

Treat prompts, database values, identifiers, comments, logs, uploaded SQL, and
playbook text as untrusted. Stored prompt injection must remain inert data. The
authorization result must be correct even when model output is malicious.

## Alpha mitigations

- Workspace-scoped API identity
- Reserved `core.*` tool namespace
- Fail-closed policy decisions with enforceable obligations
- Schema-only parser rejecting data and executable database constructs
- Single-statement `SELECT` validation and bounded result obligations
- PostgreSQL AST validation, verified role restrictions, and read-only transactions
- Stable evidence digests and append-only hash-chained audit events
- Offline deterministic sample mode

## Deferred controls

Production authentication, encrypted durable secrets, isolated
source workers, OPA sidecar deployment, local inference gateway hardening,
signed releases, SBOMs, and external security review are required before
customer-data pilots.
