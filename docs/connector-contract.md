# Connector contract

PostgreSQL is the only implemented source connector. A catalog entry, mock,
static sample or successful socket connection does not constitute support for
another connector. Every supported source must pass actual integration tests.

## Required responsibilities

| Boundary | Required behavior |
| --- | --- |
| Configuration | Typed source identity, secret reference, explicit scope and configuration revision; reject unknown connector kinds. |
| Credentials | Resolve allowlisted references only on the backend. Never put credential values in source metadata, model messages, logs, events or exports. |
| Discovery | Read current authorized schema/capabilities using the configured role; report capture time and actionable sanitized errors. |
| Authorization | Recheck source, policy, playbook and egress permissions before external work. Empty intersections deny access. |
| Execution | Accept application-validated bounded requests, enforce connector-native read-only behavior and return actual results. |
| Progress | Emit only actual lifecycle events. Record connection health separately from execution progress. |
| Cancellation | Advertise real cancellation support. Stop further scheduling, request native cancellation when supported, retain timeout backstops, and report terminal status only after work exits. |
| Evidence | Preserve actual query, source/capture/run identity, configuration/schema revisions, timestamps, columns, bounded rows, truncation and content hash. |
| Failures | Distinguish unavailable source, invalid scope, policy denial, timeout, malformed data and incomplete evidence without leaking credentials or fabricating results. |
| Recovery | Persist each completed capture before further work. Preserve interrupted attempts and retry explicitly with revalidated permissions and fresh capture times. |

The PostgreSQL implementation uses approved schema-qualified tables, validated
SELECT statements, read-only transactions, role checks and statement timeouts.
Global bounds remain three queries, 100 rows per query and five seconds per
query; a playbook may tighten them. Bounds are 16 KiB per cell, 128 KiB per
query's canonical rows/columns, 384 KiB per complete capture including integrity
metadata, and 128 KiB for the model's evidence context. Exceeding a bound fails
visibly and preserves earlier captures.

A model's classification is an interpretation. A citation is a link, not proof
that the cited record supports the claim. Consumers must expose contradictions,
missing evidence, collection limits and historical freshness. Integrity hashes
only verify the bytes/structure covered by the hash.

New captures retain the exact original hash input at
`integrity.canonical_json`, with format `opsgraph-canonical-json-v1` and scope
`query-result`. Hash its UTF-8 bytes with SHA-256 and compare the prefixed digest
with `evidence_hash`. This preserves typed decimal, date and other scalar input
without changing existing display rows or digests. The input covers workspace,
query fingerprint, relations, columns, rows and truncation; it does not cover
capture timestamps, other provenance, or model interpretation. Matching hashes
do not prove authenticity or truth. The duplicate verification text is excluded
from model inputs.

Historical captures without this input remain available. Do not reconstruct
missing types from display strings or claim their original hash was reproduced.
Arbitrary non-UTF8 binary result cells remain unsupported by the existing JSON
response serialization; select supported columns instead. This is a known
connector limitation, not a silently coerced result.

## Admission checks for another connector

Before declaring a connector supported, provide its typed implementation,
document credential/scoping requirements, and pass real discovery, bounded
execution, provenance, cancellation/timeout, reconnect and sanitized-failure
tests. Validate it through actual model-driven investigation and independent
source/result comparison. Fixture tests remain useful but insufficient. Record
the source version and tested operating systems. No placeholder implementation
may report ready or return production-shaped fabricated evidence.

Continuous ingestion and CDC are separate future work. They additionally need
explicit offset/checkpoint semantics, replay and deduplication rules, schema
change handling, freshness/lag reporting, deletion handling and bounded durable
storage. A request/response connector must not imply CDC support.

## Physical schema and meaning in this release

Discovery includes only columns visible through SELECT privileges and schemas
with USAGE. The browser selects tables, not a separate column ACL. To exclude
columns, restrict the database role's column grants or expose an approved view.
A table-wide SELECT grant overrides attempts to restrict individual columns.
Database privileges remain the authority for column access.

Saved snapshots contain physical names, data types and nullability. Discovery
does not load foreign keys, business definitions, measurement units, status
vocabularies or business time zones. Identically named columns do not establish
a join or shared meaning. Supply the missing context in the question, or use a
view whose meaning the operator has reviewed. The planner can ask a bounded
clarification without running queries; its interpretation is still fallible.
A broad semantic layer is deferred. Specialist evidence bindings assign approved
tables to required evidence categories; they do not establish business semantics.

Each attempt records configuration and schema revisions and inspection time.
Live scoped metadata is checked before planning and each query. Drift blocks
further collection with reinspection guidance; earlier captures remain historical.
Metadata checks and queries use separate connections: concurrent DDL after a
check is a residual race, not a frozen schema guarantee. Newly discovered
columns still require operator review of database grants and approved views.

Unsupported values fail with an explicit type diagnostic. In particular, a
`timestamp without time zone` has no known timezone and is not silently converted
to UTC. Select a timezone-aware expression only when the source timezone is
known, or exclude the column. Time-only, interval, range/network/custom values
and arbitrary non-UTF8 binary results are not generally supported. Warnings
before collection describe this limitation; no replacement evidence is created.
