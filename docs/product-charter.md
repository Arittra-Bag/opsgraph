# Product charter

## Promise

Investigate operational incidents locally, with external egress disabled by default.

The target product connects to an explicitly read-only PostgreSQL source or
accepts a schema-only snapshot, produces a reviewable investigation plan,
executes only policy-approved bounded queries, and returns evidence-linked
conclusions. This milestone ships the synthetic path plus an explicitly enabled
PostgreSQL connector that verifies the source role, discovers approved schemas,
and executes only AST-validated, policy-bounded SELECT statements inside
read-only transactions.

## Initial user

An engineering support lead, platform engineer, or SRE at a software vendor who
needs to reconstruct a difficult data-backed incident without granting another
service production access.

## Alpha success

- Sample investigation completes in under five minutes.
- A PostgreSQL schema can be reviewed within fifteen minutes.
- Every factual claim opens its exact evidence.
- Unsafe SQL and data-bearing dumps fail closed.
- A new user sees source, model, egress, and policy status before a run.

## Explicit exclusions

Generic BI, arbitrary SQL, remediation, executable plug-ins, full dump restore,
continuous monitoring, hosted multi-tenancy, and a marketplace are not alpha
features.
