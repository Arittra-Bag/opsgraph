# Production readiness and product boundary

OpsGraph 1.0 is a self-hosted PostgreSQL incident-investigation workspace for
one operator. A model proposes bounded queries; deterministic application
policy and the database role decide what executes. Every finding can open the
exact SQL and captured records behind it.

“Production-ready” in this project means reliable operation inside that narrow
boundary. It does not mean Internet-facing SaaS, team identity, unrestricted
natural-language SQL, or a guarantee that a model interpreted business data
correctly.

## Supported 1.0 boundary

- One trusted operator and one private OpsGraph instance per local state file.
- PostgreSQL sources reached through separately provisioned, least-privilege
  roles and exact schema-qualified table allowlists.
- Loopback or Unix-socket PostgreSQL, or remote PostgreSQL with
  `sslmode=verify-full`. An explicitly configured compatibility override exists
  for deployments that accept weaker transport; it is not the recommended path.
- Bounded, AST-validated `SELECT` statements inside repeatable-read, read-only
  transactions. Application tables with effective write privileges are rejected.
- Explicit source inspection followed by an operator-approved one-row readiness
  query. The readiness response retains no source value and is invalidated when
  source scope, inspection, or deployment policy changes.
- Ollama, LM Studio, vLLM, OpenAI, Anthropic, OpenRouter, Groq, Together, Mistral,
  and a manual OpenAI-compatible endpoint. The operator chooses the exact model,
  compatible output profile, reasoning setting, timeout, and output-token bound.
- Local SQLite history, exports, and hash-chained audit records on one host.
- Native package and fixture coverage on Linux, macOS, and Windows. The connected
  PostgreSQL/provider control path runs in Linux CI; the support matrix records
  the exact native and live evidence available for each platform.

Keep the service on loopback or a private network. The workspace key is a local
control, not multi-user identity or SSO. Protect the host, state database,
provider settings, backups, and exports as sensitive operational data.

## What 1.0 deliberately does not include

- Team accounts, tenant isolation, SSO, approval workflows, or enterprise RBAC.
- A hosted control plane, public-Internet deployment profile, or an SLA.
- Database writes, automatic remediation, arbitrary SQL, or executable plug-ins.
- Databases other than PostgreSQL.
- Generic BI, dashboards, continuous monitoring, or a semantic/business glossary.
- Automatic model discovery or a promise that every model/provider combination
  supports the required structured-output protocol.
- A guarantee that a cited conclusion is correct. Citations prove which captured
  evidence a model referenced; the operator must still review meaning and scope.

## SWOT

### Strengths

- Authorization is outside the model: policy, exact table scope, SQL validation,
  effective database privileges, transaction mode, timeouts, and row limits all
  fail closed.
- Findings remain tied to inspectable SQL, captured rows, collection time, source
  revision, schema fingerprint, and execution limits.
- Local-first operation supports local models and keeps credentials on the
  backend. Hosted inference requires deployment and source approval.
- Durable history, exports, retries, follow-ups, terminal-state audit events, and
  service readiness make failures visible instead of substituting sample output.

### Weaknesses

- Physical schema does not establish business meaning. Units, status semantics,
  time rules, and join cardinality still need operator input.
- The single-operator API key and local SQLite store do not provide team identity,
  centralized policy, high availability, or an externally anchored audit log.
- PostgreSQL is the only data connector, and the workflow investigates rather
  than remediates.
- Result quality still depends on the selected model. Provider connectivity and
  protocol compatibility do not establish analytical correctness.

### Opportunities

- The generated non-executing role guide and bounded readiness check lower the
  risk and friction of connecting an unfamiliar PostgreSQL source.
- Evidence-linked incident work is a narrower and more auditable use case than a
  general “chat with your database” interface.
- Local and hosted provider presets let operators match privacy, cost, latency,
  and model quality without moving authorization into the model.
- Real operator feedback can now identify the next highest-value workflow before
  expanding to team identity or additional databases.

### Threats

- Mature products offer broader semantic layers, governance, or team access.
- Ambiguous schemas can produce plausible but wrong interpretations even when SQL
  and citations are valid.
- Expensive queries, database topology changes, provider outages, and local host
  compromise remain operational risks outside model correctness.
- Expanding into generic BI, monitoring, or autonomous remediation would weaken
  the product’s evidence-first, read-only focus.

## Market boundary

This comparison is based on the projects’ own current documentation and selected
Hacker News operator discussions. It is a scope check, not a feature score.

| Product or signal | Documented emphasis | OpsGraph 1.0 boundary |
| --- | --- | --- |
| [Wren AI](https://docs.getwren.ai/oss/introduction) | A semantic layer supplies business context for natural-language analytics. | OpsGraph does not build a semantic layer; it asks the operator to provide missing meanings and focuses on incident evidence. |
| [Vanna](https://vanna.ai/data-security) | Local credential handling plus RBAC and audit capabilities for conversational data access. | OpsGraph keeps credentials local and audits actions, but remains intentionally single-operator without team RBAC. |
| [Bytebase](https://www.bytebase.com/database-governance-ai-agents/) | Agent identity, access policies, masking, approval, and audit for governed database access. | OpsGraph uses one local operator and a database role; it does not claim enterprise agent identity or approval workflows. |
| [Text-to-SQL operator discussion](https://news.ycombinator.com/item?id=38992601) | Fine-grained roles, table/column scope, limits, and semantic ambiguity recur as practical concerns. | Exact table allowlists, database grants, query bounds, and visible evidence are first-class controls. |
| [Safe database access discussion](https://news.ycombinator.com/item?id=46620990) | Dedicated read-only users and minimal tool access are recurring deployment advice. | The UI generates a reviewable least-privilege role script and never executes it automatically. |
| [PostgreSQL `SELECT` side effects discussion](https://news.ycombinator.com/item?id=37315667) | `SELECT` alone is not a complete safety boundary. | OpsGraph combines an allowlisted SQL subset, safe-function list, role checks, table scope, and read-only transactions. |

The product remains differentiated when it stays specific: investigate a
PostgreSQL incident, show exactly what ran, preserve what was captured, and make
uncertainty reviewable.

## Evidence required before publishing `v1.0.0`

The tag must point to the reviewed merge commit. From that exact revision:

1. All required GitHub checks pass, including lint, formatting, the complete
   Python and frontend suites, cross-platform fixture/package jobs, container
   readiness, and the connected PostgreSQL plus model-protocol control path.
2. The connected job proves source inspection, explicit readiness, a real
   PostgreSQL capture, export, database and API write denial, restart recovery,
   provider-key use, audit events, and absence of known fixture secrets in public
   responses or the state database. It does not prove model quality.
3. A clean deep security scan of the final diff is triaged. “No report” or a
   cancelled scan is not a passing result.
4. The final wheel, source distribution, native bundles, container image, source
   supplement, dependency/native inventories, notices, SBOMs where produced, and
   SHA-256 checksums are built from the tag. Artifact checksums and source commit
   must agree; CI artifacts from another revision cannot be promoted.
5. The UI is exercised at desktop and narrow viewport sizes, using keyboard
   navigation and 200% zoom. Saved screenshots must contain only disposable data.
6. Release notes and the support matrix record actual outcomes and remaining
   gaps. Provider protocol fixtures, package installation, and live model quality
   remain separate claims.

## Deferred after 1.0

Team identity, per-user RBAC, external audit anchoring, HA state storage,
additional database engines, a governed semantic layer, and signed/attested
automated release publication require separate design and validation. They are
valid future work; they are not hidden requirements for the supported 1.0
single-operator deployment.
