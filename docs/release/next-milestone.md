# Application incident verification plan

Finish the current release gates before expanding the investigation engine.
The next milestone targets one PostgreSQL application-data incident workflow
and a finding another engineer can check without source access.

## Order of work

1. Select one recurring incident, such as missing job events. Define an
   independent expected result and compare the current workflow with manual SQL.
2. Add a versioned operator-approved contract for entity keys, tenant scope,
   time windows and timezones, units, status meanings, and approved joins.
   Missing definitions must remain visible rather than inferred silently.
3. Add typed count, comparison, and membership claims with deterministic checks
   against retained results. Keep model interpretations separate from checked
   calculations. Verifying a captured aggregate does not prove population scope.
4. Add a privacy-reviewed offline packet with original and transformed evidence
   clearly separated, a transformation manifest, omissions, and an offline
   verifier. Redaction must not leave removed values in secondary representations.
5. Measure failures, incorrect supported claims, useful abstention, affected
   entity accuracy, latency, and reviewer effort on held-out incident cases.
6. Add bounded adaptive queries only if those cases demonstrate that a fixed
   initial plan prevents useful disconfirmation. Retain total query limits,
   authorization, fresh schema checks, and explicit stopping rules.

## Acceptance boundaries

Fixtures must cover zero records, NULLs, duplicates, empty parents, join
multiplication, tenant filters, timezone boundaries, unit ambiguity, and
truncation. An offline verifier must reject tampered or incomplete inputs and
state exactly what it checked. Digests do not authenticate database origin or
establish causality. Publish measured model combinations and failures without
claiming universal accuracy.

Additional connectors, team identity, continuous monitoring, automatic
remediation, and generic analytics remain outside this milestone. Calendar
targets depend on measured workflow value and available review capacity.
