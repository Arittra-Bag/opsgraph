# Public launch kit — OpsGraph Alpha

Use this copy only while the repository remains within the boundary in
[`SECURITY.md`](../SECURITY.md): **public validation alpha; not approved for
production or customer data**.

## Product Hunt

| Field | Copy |
| --- | --- |
| Name | `OpsGraph` |
| Tagline | `Self-hosted, evidence-first PostgreSQL investigations` |
| Link | `https://github.com/Arittra-Bag/opsgraph` |
| Open source | Yes — Apache-2.0 |
| Tags | `Open Source`, `Developer Tools`, `AI Infrastructure` |

### Description

> OpsGraph is an open-source public-validation alpha for software teams. It
> maps an approved PostgreSQL source, runs policy-gated read-only
> investigations, and returns evidence-linked conclusions with a
> tamper-evident audit chain.

### First maker comment

> Hi Product Hunt — I’m Arittra, maker of OpsGraph.
>
> I built it around a simple constraint: an AI system investigating
> operational data should not receive an unrestricted database connection and
> a vague prompt.
>
> OpsGraph is deliberately narrow in this alpha:
>
> - a separately provisioned, read-only PostgreSQL role
> - AST-validated, bounded `SELECT` queries in read-only transactions
> - source-owned evidence bindings, so a model cannot self-label evidence
>   coverage
> - evidence hashes and a local tamper-evident audit chain
> - local-first operation; cloud-model egress is opt-in
>
> This is a public validation alpha, not a production or customer-data tool.
> I’d value feedback from platform, SRE, and data teams: what would you need
> before trusting a self-hosted investigation workspace against a
> non-production replica?

### Images

Use real screenshots only. Mark the current sample as **Synthetic sample** in
the caption or image itself. Recommended order:

1. Investigation workspace and evidence-linked conclusion.
2. Source setup: allowlisted tables and source-owned evidence bindings.
3. Policy-gated query preview or rejection state.
4. Audit/evidence detail view.

Do not present the sample dataset as a customer environment, imply autonomous
remediation, or call the system production ready.

## LinkedIn post

> Today I’m opening **OpsGraph Alpha**.
>
> It began with a constraint I keep coming back to: if an AI system
> investigates operational data, it should not receive an unrestricted
> database connection and a vague prompt.
>
> OpsGraph is a self-hosted, evidence-first investigation workspace for an
> approved PostgreSQL source. It discovers a scoped schema, runs
> policy-gated read-only queries, and returns conclusions linked to the
> underlying evidence and audit trail.
>
> In this alpha, the model can propose work; the application still enforces
> table scope, query shape, row/time limits, read-only execution, and the
> evidence contract.
>
> It is intentionally a public validation alpha — not production or
> customer-data ready. I’m looking for direct feedback from platform, SRE,
> and data engineers on the trust boundary and the workflow.
>
> Source: https://github.com/Arittra-Bag/opsgraph
>
> #OpenSource #PostgreSQL #SRE

## Claim guardrails

Say:

- “public validation alpha”
- “self-hosted, read-only PostgreSQL investigations”
- “policy-gated and evidence-linked”
- “local-first; external model egress is opt-in”

Do not say:

- “production ready” or “safe for customer data”
- “fully autonomous” or “remediates incidents”
- “tamper-proof audit” (the current audit chain is local, not externally
  anchored)
- “works with every database” (PostgreSQL is the supported live connector)

## Launch-day checklist

- Confirm the default branch CI run is green.
- Confirm the GitHub repository has this launch kit, `README.md`,
  `SECURITY.md`, `LICENSE`, and the synthetic screenshots.
- Use the repository URL as the product link until a hosted demo exists.
- Answer questions with the alpha boundary first; ask for feedback rather
  than votes.
- Keep a short list of reported gaps and reply with facts, not promises.
