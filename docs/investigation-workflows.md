# Create an investigation workflow

A workflow is the configurable investigation harness carried by a **Playbook**.
It selects purpose, registered tools, local SQL restrictions, optional follow-up
query rounds and report size. A **Policy** decides permission and its ceilings.
A workflow can narrow permission; it cannot grant it. This is an investigation
workflow, not a model evaluation harness or arbitrary Python/script runner.

In **Playbooks and workflows → Create your own workflow**:

1. Name it, give it a stable identifier and a new semantic version, and describe
   the operational question it should investigate.
2. Choose one to three queries per attempt, whether later query rounds may use
   earlier results, and the model finding/limitation budgets.
3. Generate the policy-bounded JSON. This performs no database/model call and
   saves nothing. Review tools, restrictions and model-sharing permission.
4. **Validate and save draft**, then **Publish saved draft**. Published content
   is immutable. Change the version before publishing a revision.
5. Choose the published playbook next to your approved source when asking a
   question. Review the effective row, time and query limits before submitting.

Every data investigation retains scope review, planning, bounded read-only SQL
and a cited report. The workflow may omit the optional second planning round;
it cannot omit source checks, clarification, native tools, SQL validation,
read-only-role checks, citations or application-required caveats. The application
enforces the query budget across all rounds, not just per model request. A plan
that exceeds it or requests a disabled follow-up fails before SQL. Report limits
constrain generated model prose; required application caveats are still added.
Conversational replies may complete without a database query, as in V1.

## Example JSON

```json
{
  "id": "my-order-review",
  "version": "1.0.0",
  "name": "Order review",
  "purpose": "Observe explicitly defined order outcomes in the approved source, preserving uncertainty.",
  "tools": [
    {"tool": "core.schema.inspect"},
    {"tool": "core.sql.select", "settings": {
      "max_rows": 25,
      "timeout_ms": 1000,
      "allowed_schemas": ["public"]
    }}
  ],
  "required_evidence": [],
  "conclusion_classes": ["supported", "possible", "unknown", "contradictory"],
  "egress": "forbidden",
  "risk": "read_only",
  "workflow": {
    "version": 1,
    "max_queries": 1,
    "follow_up_queries": false,
    "max_findings": 4,
    "max_limitations": 3
  }
}
```

| Field | Meaning and constraint |
| --- | --- |
| `id` / `version` | Lowercase dotted/hyphenated identity and immutable semantic version. |
| `purpose` | Investigation instructions, not authority or proof of source meaning. |
| `tools` | Registered tools only; native inspection is mandatory and connected SQL needs `core.sql.select`. |
| SQL `settings` | `max_rows`, `timeout_ms`, `allowed_schemas`, optional exact schema-qualified `allowed_tables`. Omitted/null values inherit ceilings; source scope and database grants still apply. |
| `required_evidence` | Specialist evidence tags mapped to tables in Sources. Empty for general SQL investigations. Tags do not define business meaning. |
| `egress` | `forbidden` for local inference; `allowlisted` permits external inference only if source and deployment also approve it. |
| `risk` | Always `read_only`. |
| `conclusion_classes` | Keep all standard classes so unknowns and contradictions can be reported. |
| `workflow` | Optional. Version 1; total queries 1–3; follow-up boolean; findings 1–4; model limitations 1–3. Unknown keys, larger values and ambiguous types are rejected. |

The generated starting definition is bounded by the current operator's query
policy. Handwritten examples still require validation. Extra tool headers,
executable hooks and arbitrary graph stages are not accepted. No new plugin
system or permission model is introduced.

Split YAML skillpacks can use the same `workflow` mapping in `skill.yaml`.
Older playbooks without it retain the V1 three-query/four-finding/three-limitation
behavior and their exact serialized content. Existing publication receipts,
source reviews, export contracts, saved evidence and calculation hashes remain
unchanged. Custom workflow settings and fingerprint are recorded with each new
attempt, and retained in JSON exports. Historical attempts keep their original
configuration even after a new version is published.

## Policy JSON is a view

**Policies** shows a readable summary and an expandable exact JSON response.
It does not accept edits or grant new permissions. `default: deny` requires an
explicit allowed action. `obligations` contains the deployment row/time/schema/
table ceilings; source settings and database privileges can narrow them further.
An empty deployment `allowed_tables` ceiling does not automatically approve all
tables: explicit source approval is still required. Model egress is separately
controlled and is not granted by this query-policy response. Deployment changes
belong to the operator and must be reviewed on the backend.
