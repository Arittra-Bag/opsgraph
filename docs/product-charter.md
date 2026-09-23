# Product charter

## Promise

Investigate operational incidents locally, with external egress disabled by default.

The target product connects to an explicitly read-only PostgreSQL source or
accepts a schema-only snapshot, produces a reviewable investigation plan,
executes only policy-approved bounded queries, and returns evidence-linked
conclusions. This milestone uses an explicitly configured real PostgreSQL
connector that verifies the source role, discovers approved schemas,
and executes only AST-validated, policy-bounded SELECT statements inside
read-only transactions.

## Initial user

An engineering support lead, platform engineer, or SRE at a software vendor who
needs to reconstruct a difficult data-backed incident without granting another
service production access.

## 1.0 goals

- Guided setup needs no manual configuration-file edits when prerequisites exist.
- A generated, non-executing role guide and explicit bounded readiness check make
  the first real source connection reviewable.
- A real investigation leads to inspectable records, SQL and stated uncertainty.
- A PostgreSQL schema can be reviewed within fifteen minutes.
- Every factual claim opens its exact evidence.
- Unsafe SQL and data-bearing dumps fail closed.
- A new user sees source, model, egress, and policy status before a run.

## Explicit exclusions

Generic BI, arbitrary SQL, remediation, executable plug-ins, full dump restore,
continuous monitoring, hosted multi-tenancy, team identity, and a marketplace
are not 1.0 features.

Production readiness applies only to the documented single-operator, private
deployment boundary in [production readiness](production-readiness.md).
