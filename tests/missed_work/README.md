# Export contract and reconciliation reference

The export audit flow is implemented in `src/opsgraph/missed_work/*`, the reviewed
contract and run APIs, reports, and the browser workspace. It uses the existing
bounded read-only broker and does not invoke a model. See
[the operator guide](../../docs/work-outcome-audits.md) for the current integration
and evidence boundaries. No runtime defaults, dependency locks, source grants,
or release version change for this feature.

The contract is bounded declarative data: distinct schema-qualified relations,
column identifiers, status vocabularies, explicit seconds or a recorded deadline,
tenant-plus-promise joins, timezone-aware instants, logical-export units and a
reviewed-schema fingerprint. This first definition supports an accepted export
ledger and separate outcome, attempt, cancellation and coverage relations. It is
not a generic semantic catalog. Syntax validation does not approve these meanings,
authorize access, check actual columns/types, or prove source completeness.

`export-contract.json` describes the synthetic fixture vocabulary. Its fingerprint
is a test marker, not a captured schema. `export-cases.json` contains hand-labelled
inputs and expected states. `export_oracle.py` is an independent test-only Python
reference; it imports no production contract, reducer, broker or model. Its complete
in-memory inputs must not be substituted with a retained-row query sample.
Future SQL and interpretation tests can compare with these independently labelled
outcomes rather than deriving their expected results from the code under test.

## Fixed semantics

- The population is one tenant's accepted promises in `[start, end)`, with `end`
  no later than the observation cutoff. Logical IDs are unique within that scope.
  Missing/ambiguous population keys or acceptance times and truncated populations
  fail explicitly. Events are joined on tenant and promise; foreign tenants do not
  supply outcomes or affect results.
- A timezone-aware instant is converted to UTC. A naive timestamp is not guessed.
  Events after the observation cutoff are excluded. A ready or cancelled event at
  the deadline satisfies the boundary; an unresolved promise at the deadline is
  overdue. A deadline before acceptance is invalid.
- Customer readiness requires an outcome record. Successful/failed attempts cannot
  substitute for it. Retry attempts do not multiply logical promises. An exact
  duplicate event is deduplicated; conflicting event identities, incompatible
  terminal outcomes and unrecognized status meanings become unknown. No hidden
  precedence between contradictory terminal facts is invented.
- Legitimate cancellation removes current backlog. Cancellation after the deadline
  and late readiness preserve the breach when observation coverage establishes
  absence through the deadline. Ready evidence at/before the deadline proves the
  positive outcome even without full coverage. A late ready event with incomplete
  history proves current readiness but cannot exclude an earlier ready event.
- The synthetic coverage watermark means **all required records for this entire
  scoped population are complete through the stated instant**. It is an explicit
  fixture premise, not `MAX(event_time)`, a database connection check, or a claim
  that the product has verified completeness. Stale/missing coverage downgrades
  negative membership. Orphan/unassignable events also prevent a clean absence
  conclusion; events belonging to known promises outside the selected window do
  not count as orphans.
- States are ready, cancelled, terminal failure, pending within deadline, overdue
  unresolved and unknown. `deadline_breached` is a separate historical fact and is
  null when evidence cannot establish it. Unknowns never become measured zero.
  Missing customer attribution makes that promise unknown in this reference.

The 1,000-promise scenario independently checks 930 ready, 20 cancelled, five
terminal failures, 30 pending, 12 overdue across eight customers, and three unknown.
It includes 96 failed attempts, 81 preceding ready outcomes. Five minutes later,
the same accepted-at cohort has 942 ready and zero currently overdue; all 12
recoveries retain their breach history and a newly accepted request is excluded.

Run `.venv/bin/python -m pytest -q tests/missed_work` from the repository root.
Offline tests establish definition and fixture behavior. `test_postgres.py` and
`test_work_outcome_api.py` additionally run against disposable real PostgreSQL in
the connected CI job; they skip without an explicitly supplied loopback owner
DSN. They are not model-quality evaluations or competitor benchmarks.

Continuous monitoring, additional connectors, team identity and remediation
remain outside this export-audit implementation. Existing V1 investigations and
the supported private single-operator boundary remain in place.
