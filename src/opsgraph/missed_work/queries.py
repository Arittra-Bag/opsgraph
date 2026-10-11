"""One bounded fact pack; all totals/quality checks/examples share one snapshot."""

# All identifiers/literals below use psycopg quoting. Remaining fragments are
# fixed templates or bounded integer limits; the resulting SQL still goes through
# the existing AST validator, exact policy scope and read-only broker.
# ruff: noqa: S608

from psycopg import sql

from opsgraph.domain.work_outcome import FixedDeadline, WorkOutcomeContract

from .contracts import WorkOutcomeRequest

COLUMNS = ("kind", "state", "promise_id", "customer_id", "due_at", "count_value", "breach")
METHOD = "export-reconciliation-v1"
STATES = (
    "ready",
    "cancelled",
    "terminal_failure",
    "pending_within_deadline",
    "overdue_unresolved",
    "unknown",
)
# Match Python str.strip() whitespace without trimming or normalizing valid keys.
IDENTITY_WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def literal(value):  # noqa: ANN001
    return sql.Literal(value).as_string()


def ident(value: str) -> str:
    return sql.Identifier(*value.split(".")).as_string()


def vocabulary(values: tuple[str, ...]) -> str:
    return "(" + ",".join(literal(v) for v in values) + ")"


def blank_identity(expression: str) -> str:
    # A fixed regex operator needs no additional executable function permission.
    return f"CAST({expression} AS text) !~ {literal('[^' + IDENTITY_WHITESPACE + ']')}"


def fact_query(contract: WorkOutcomeContract, scope: WorkOutcomeRequest, max_rows: int) -> str:
    if max_rows < 11:
        raise ValueError("This audit needs room for six state totals and five quality/scope rows.")
    samples = min(50, max_rows - 11)
    p = contract.promises
    tenant = literal(scope.tenant_id)
    start, end, cutoff = (literal(v) for v in (scope.start, scope.end, scope.as_of))
    due = (
        f"p.{ident(p.accepted_at)} + {literal(contract.deadline.seconds)} * INTERVAL '1 second'"
        if isinstance(contract.deadline, FixedDeadline)
        else f"p.{ident(contract.deadline.due_at)}"
    )
    ctes = [
        f"""population AS (
      SELECT p.{ident(p.promise_key)} AS pid, p.{ident(p.tenant_key)} AS tenant,
        CAST(p.{ident(p.customer_key)} AS text) AS customer,
        p.{ident(p.accepted_at)} AS accepted, {due} AS due
      FROM {ident(p.relation)} p
      WHERE (CAST(p.{ident(p.tenant_key)} AS text) = {tenant} OR p.{ident(p.tenant_key)} IS NULL)
        AND ((p.{ident(p.accepted_at)} >= {start} AND p.{ident(p.accepted_at)} < {end})
             OR p.{ident(p.accepted_at)} IS NULL)
    )""",
        "population_quality AS (SELECT COUNT(*) AS bad FROM (SELECT pid FROM population "
        "GROUP BY pid HAVING pid IS NULL OR COUNT(*) > 1) q)",
    ]
    orphan_terms, unassignable_terms = [], []
    for name, mapping, valid, positive in (
        (
            "out",
            contract.outcomes,
            (*contract.outcomes.ready_statuses, *contract.outcomes.terminal_failure_statuses),
            contract.outcomes.ready_statuses,
        ),
        ("att", contract.attempts, contract.attempts.known_statuses, ()),
        (
            "can",
            contract.cancellations,
            contract.cancellations.legitimate_statuses,
            contract.cancellations.legitimate_statuses,
        ),
    ):
        ek, pk, tk, at, status = map(
            ident,
            (
                mapping.event_key,
                mapping.promise_key,
                mapping.tenant_key,
                mapping.occurred_at,
                mapping.status_column,
            ),
        )
        relation = ident(mapping.relation)
        ctes.append(f"""{name}_events AS (
          SELECT DISTINCT e.{ek} AS eid, e.{pk} AS pid, e.{tk} AS tenant,
            e.{at} AS occurred, e.{status} AS status
          FROM {relation} e
          WHERE CAST(e.{tk} AS text) = {tenant} AND (e.{at} <= {cutoff} OR e.{at} IS NULL)
        )""")
        ctes.append(
            f"{name}_conflicts AS (SELECT eid FROM {name}_events GROUP BY eid HAVING COUNT(*) > 1)"
        )
        earliest = (
            f"MIN(e.occurred) FILTER (WHERE e.status IN {vocabulary(positive)})"
            if positive
            else "CAST(NULL AS timestamp with time zone)"
        )
        terminal = (
            f"COUNT(e.eid) FILTER (WHERE e.status IN "
            f"{vocabulary(contract.outcomes.terminal_failure_statuses)})"
            if name == "out"
            else "CAST(0 AS bigint)"
        )
        ctes.append(f"""{name}_facts AS (
          SELECT p.pid, {earliest} AS positive_at, {terminal} AS terminal,
            COUNT(*) FILTER (WHERE e.tenant IS NOT NULL AND
              (e.eid IS NULL OR {blank_identity("e.eid")}
               OR e.occurred IS NULL OR e.occurred < p.accepted
               OR e.status IS NULL OR e.status NOT IN {vocabulary(valid)}
               OR x.eid IS NOT NULL)) AS bad
          FROM population p LEFT JOIN {name}_events e ON e.pid = p.pid AND e.tenant = p.tenant
          LEFT JOIN {name}_conflicts x ON x.eid = e.eid GROUP BY p.pid
        )""")
        orphan_terms.append(f"""(SELECT COUNT(*) FROM {relation} e
          WHERE e.{at} <= {cutoff}
          AND (CAST(e.{tk} AS text) = {tenant} AND NOT EXISTS
            (SELECT 1 FROM {ident(p.relation)} parent WHERE
             parent.{ident(p.promise_key)} = e.{pk} AND parent.{ident(p.tenant_key)} = e.{tk})))""")
        unassignable_terms.append(f"""(SELECT COUNT(*) FROM {relation} e WHERE e.{tk} IS NULL
           OR (CAST(e.{tk} AS text) = {tenant} AND e.{at} IS NULL AND NOT EXISTS
               (SELECT 1 FROM population selected WHERE selected.pid=e.{pk})))""")
    cov = contract.coverage
    ctes.append(f"""coverage AS (SELECT
       (COUNT(*) = 1 AND MIN(c.{ident(cov.complete_through)}) >= {cutoff}
        AND {" + ".join(orphan_terms + unassignable_terms)} = 0) AS complete,
       {" + ".join(orphan_terms)} AS orphans, {" + ".join(unassignable_terms)} AS unassignable
       FROM {ident(cov.relation)} c WHERE CAST(c.{ident(cov.tenant_key)} AS text) = {tenant})""")
    ctes.append("""joined AS (SELECT p.*, o.positive_at AS ready_at, o.terminal,
       c.positive_at AS cancelled_at, v.complete,
       (o.bad + a.bad + c.bad > 0 OR p.customer IS NULL OR p.customer = ''
        OR p.due IS NULL OR p.due < p.accepted OR
        (CASE WHEN o.positive_at IS NOT NULL THEN 1 ELSE 0 END
         + CASE WHEN o.terminal > 0 THEN 1 ELSE 0 END
         + CASE WHEN c.positive_at IS NOT NULL THEN 1 ELSE 0 END) > 1) AS ambiguous
       FROM population p LEFT JOIN out_facts o ON o.pid = p.pid
       LEFT JOIN att_facts a ON a.pid = p.pid LEFT JOIN can_facts c ON c.pid = p.pid
       CROSS JOIN coverage v)""")
    ctes.append(f"""classified AS (SELECT *, CASE
       WHEN ambiguous THEN 'unknown' WHEN ready_at IS NOT NULL THEN 'ready'
       WHEN cancelled_at IS NOT NULL THEN 'cancelled' WHEN terminal > 0 THEN 'terminal_failure'
       WHEN NOT COALESCE(complete, false) THEN 'unknown'
       WHEN due <= {cutoff} THEN 'overdue_unresolved' ELSE 'pending_within_deadline' END AS state,
       CASE WHEN ambiguous THEN NULL
       WHEN ready_at <= due OR cancelled_at <= due THEN false
       WHEN NOT COALESCE(complete, false) THEN NULL ELSE due <= {cutoff} END AS breach
       FROM joined)""")
    labels = ",".join(f"({literal(s)})" for s in STATES)
    ctes.append(
        f"totals AS (SELECT labels.state, COUNT(c.pid) AS total "
        f"FROM (VALUES {labels}) labels(state) LEFT JOIN classified c ON c.state=labels.state "
        "GROUP BY labels.state)"
    )
    ctes.append(
        f"examples AS (SELECT * FROM classified ORDER BY "
        "CASE state WHEN 'overdue_unresolved' THEN 0 WHEN 'unknown' THEN 1 "
        "WHEN 'terminal_failure' THEN 2 ELSE 3 END, due, CAST(pid AS text) "
        f"LIMIT {samples})"
    )
    query = "WITH " + ",\n".join(ctes) + "\n"
    query += f"""SELECT 'summary' AS kind, state, CAST(NULL AS text) AS promise_id,
       CAST(NULL AS text) AS customer_id, CAST(NULL AS timestamp with time zone) AS due_at,
       total AS count_value, CAST(NULL AS boolean) AS breach FROM totals
       UNION ALL SELECT 'quality', 'invalid_population', NULL, NULL, NULL,
       (SELECT bad FROM population_quality) + (SELECT COUNT(*) FROM population WHERE
        tenant IS NULL OR accepted IS NULL OR {blank_identity("pid")}), NULL
       UNION ALL SELECT 'quality', 'orphan_events', NULL, NULL, NULL, orphans, NULL FROM coverage
       UNION ALL SELECT 'quality', 'unassignable_events', NULL, NULL, NULL,
       unassignable, NULL FROM coverage
       UNION ALL SELECT 'scope', 'population', NULL, NULL, NULL, COUNT(*), NULL FROM population
       UNION ALL SELECT 'scope', 'affected_customers', NULL, NULL, NULL,
       COUNT(DISTINCT customer), NULL FROM classified WHERE state='overdue_unresolved'
       UNION ALL SELECT 'example', state, CAST(pid AS text), customer, due, NULL, breach
       FROM examples
    """
    return query
