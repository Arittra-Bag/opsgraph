"""Narrow checks of explicit empty-parent requirements, not SQL semantic proof."""

import re

from pglast import ast, parse_sql
from pglast.enums import JoinType
from pglast.parser import ParseError


def _column_parts(node):
    if isinstance(node, ast.ColumnRef) and all(isinstance(x, ast.String) for x in node.fields):
        return tuple(x.sval for x in node.fields)
    return ()


def _synthetic_row_count(node):
    return (
        isinstance(node, ast.FuncCall)
        and tuple(x.sval for x in node.funcname) == ("count",)
        and (
            node.agg_star
            or (
                len(node.args or ()) == 1
                and isinstance(node.args[0], ast.A_Const)
                and not node.args[0].isnull
            )
        )
        and not node.agg_filter
        and not node.over
    )


def _missing_child_count(node, child_alias, question):
    if (
        not isinstance(node, ast.A_Expr)
        or tuple(x.sval for x in node.name or ()) != ("-",)
        or not _synthetic_row_count(node.lexpr)
    ):
        return False
    count = node.rexpr
    if (
        not isinstance(count, ast.FuncCall)
        or tuple(x.sval for x in count.funcname) != ("count",)
        or count.agg_filter
        or count.agg_distinct
        or count.over
        or len(count.args or ()) != 1
    ):
        return False
    value = _column_parts(count.args[0])
    return (
        len(value) == 2
        and value[0] == child_alias
        and bool(re.search(r"\bcount\s+null\s+" + re.escape(value[1]) + r"\b", question, re.I))
    )


def _direct_child_join(node, child, parent, child_key, parent_key):
    if (
        not isinstance(node, ast.JoinExpr)
        or node.jointype != JoinType.JOIN_LEFT
        or not isinstance(node.larg, ast.RangeVar)
        or not isinstance(node.rarg, ast.RangeVar)
        or node.larg.schemaname != parent.schema_name
        or node.larg.relname != parent.table_name
        or node.rarg.schemaname != child.schema_name
        or node.rarg.relname != child.table_name
    ):
        return False
    predicate = node.quals
    if not isinstance(predicate, ast.A_Expr) or tuple(x.sval for x in predicate.name or ()) != (
        "=",
    ):
        return False
    expected = {
        (node.larg.alias.aliasname if node.larg.alias else node.larg.relname, parent_key),
        (node.rarg.alias.aliasname if node.rarg.alias else node.rarg.relname, child_key),
    }
    return {_column_parts(predicate.lexpr), _column_parts(predicate.rexpr)} == expected


def _simple_unfiltered_select(sql):
    try:
        statements = parse_sql(sql)
    except ParseError:
        return None
    if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
        return None
    stmt = statements[0].stmt
    if (
        stmt.whereClause
        or stmt.havingClause
        or stmt.withClause
        or stmt.larg
        or stmt.rarg
        or len(stmt.fromClause or ()) != 1
    ):
        return None
    return stmt


def _defined_child_pairs(noun, definitions, snapshot):
    children = [
        table
        for table in snapshot.tables
        if table.table_name.casefold() in {noun.casefold(), noun.casefold() + "s"}
    ]
    if len(children) != 1:
        return
    child = children[0]
    for child_name, child_key, parent_name, parent_key in definitions:
        parents = [t for t in snapshot.tables if t.table_name == parent_name]
        if child_name == child.table_name and len(parents) == 1:
            yield child, parents[0], child_key, parent_key


def _child_count_target_conflict(stmt, metric, question):
    child_relation = stmt.fromClause[0].rarg
    child_alias = child_relation.alias.aliasname if child_relation.alias else child_relation.relname
    for target in stmt.targetList or ():
        row_count = (
            metric is None or (target.name or "").casefold() == metric.casefold()
        ) and _synthetic_row_count(target.val)
        if row_count or _missing_child_count(target.val, child_alias, question):
            return True
    return False


def _child_rule_conflict(stmt, metric, noun, definitions, snapshot, question):
    for child, parent, child_key, parent_key in _defined_child_pairs(noun, definitions, snapshot):
        if not _direct_child_join(stmt.fromClause[0], child, parent, child_key, parent_key):
            continue
        if _child_count_target_conflict(stmt, metric, question):
            return child.table_name
    return None


def outer_join_row_count_conflict(question, queries, snapshot):
    """Reject synthetic-row counts for an explicitly requested child-row metric.

    Only direct, unfiltered LEFT JOINs on operator-supplied physical keys and
    explicit child-row or NULL-value counts are checked. Conditional aggregates
    and reconstructed relations remain outside this narrow check.
    """
    question = re.sub(r'"[^"\n]*"|`[^`\n]*`', " ", question)
    rules = re.findall(r"\b(\w+)\s+counts\s+(\w+)\s+rows\b", question, re.I)
    if not rules:
        rules = [(None, noun) for noun in re.findall(r"\bcount\s+(\w+)\s+rows\b", question, re.I)]
    definitions = re.findall(r"\b(\w+)\.(\w+)\s+references\s+(\w+)\.(\w+)\b", question, re.I)
    if not rules or not definitions:
        return None
    for query in queries:
        stmt = _simple_unfiltered_select(query.sql)
        if stmt is None:
            continue
        for metric, noun in rules:
            child = _child_rule_conflict(stmt, metric, noun, definitions, snapshot, question)
            if child:
                return (
                    f"The requested child count concerns {child} rows, but "
                    "a wildcard or non-null constant count includes the synthetic "
                    "LEFT JOIN row when no child exists. "
                    "Count a non-null child row identifier, preserve empty parents, "
                    "and return zero child rows and zero missing measurements for "
                    "an empty parent. Do not count a nullable measurement as row identity."
                )
    return None


def conditional_count_conflict(question, queries):
    """Check an explicit named 'counts ... with at least one field = value' rule.

    COUNT(DISTINCT left.id) is unaffected by a filter solely in an optional
    right-side join. This only recognizes a direct column count and an exact
    operator-supplied equality; conditional aggregates and other SQL are left
    untouched. It neither repairs SQL nor establishes general query correctness.
    """
    question = re.sub(r'"[^"\n]*"|`[^`\n]*`', " ", question)
    rules = re.findall(
        r"\b(\w+)\s+counts\s+\w+\s+with\s+at\s+least\s+one\s+"
        r"(\w+)\s*=\s*'([^']*)'",
        question,
    )
    if not rules:
        return None

    def column(node):
        if isinstance(node, ast.ColumnRef) and all(isinstance(x, ast.String) for x in node.fields):
            return tuple(x.sval for x in node.fields)
        return ()

    def matches(node, alias, field, value):
        if isinstance(node, ast.BoolExpr):
            return any(matches(part, alias, field, value) for part in node.args)
        if not isinstance(node, ast.A_Expr) or tuple(x.sval for x in node.name or ()) != ("=",):
            return False
        for left, right in ((node.lexpr, node.rexpr), (node.rexpr, node.lexpr)):
            if column(left) == (alias, field) and isinstance(right, ast.A_Const):
                if isinstance(right.val, ast.String) and right.val.sval == value:
                    return True
        return False

    def unfiltered_relations(node):
        if isinstance(node, ast.RangeVar):
            return {node.alias.aliasname if node.alias else node.relname}
        if isinstance(node, ast.JoinExpr) and node.jointype == JoinType.JOIN_LEFT:
            predicate = node.quals
            if (
                isinstance(predicate, ast.A_Expr)
                and tuple(x.sval for x in predicate.name or ()) == ("=",)
                and column(predicate.lexpr)
                and column(predicate.rexpr)
                and isinstance(node.rarg, ast.RangeVar)
            ):
                left = unfiltered_relations(node.larg)
                if left:
                    return left | unfiltered_relations(node.rarg)
        return set()

    def optional_match(node, counted_alias, field, value):
        if not isinstance(node, ast.JoinExpr):
            return False
        if node.jointype == JoinType.JOIN_LEFT and isinstance(node.rarg, ast.RangeVar):
            right = node.rarg.alias.aliasname if node.rarg.alias else node.rarg.relname
            if counted_alias in unfiltered_relations(node.larg) and matches(
                node.quals, right, field, value
            ):
                return True
        # A later INNER/RIGHT/FULL join can restrict the preserved entity set.
        return node.jointype == JoinType.JOIN_LEFT and optional_match(
            node.larg, counted_alias, field, value
        )

    for query in queries:
        try:
            statements = parse_sql(query.sql)
        except ParseError:
            continue
        if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
            continue
        stmt = statements[0].stmt
        if (
            stmt.whereClause
            or stmt.havingClause
            or stmt.withClause
            or stmt.larg
            or stmt.rarg
            or len(stmt.fromClause or ()) != 1
        ):
            continue
        for metric, field, value in rules:
            for target in stmt.targetList or ():
                count = target.val
                if (
                    target.name != metric
                    or not isinstance(count, ast.FuncCall)
                    or tuple(x.sval for x in count.funcname) != ("count",)
                    or not count.agg_distinct
                    or count.agg_filter
                    or count.over
                    or len(count.args or ()) != 1
                ):
                    continue
                counted = column(count.args[0])
                if len(counted) == 2 and optional_match(
                    stmt.fromClause[0], counted[0], field, value
                ):
                    return (
                        f"The requested {metric} requires a matching child row, but its "
                        "unconditional COUNT(DISTINCT left identifier) counts entities even "
                        "when the filtered LEFT JOIN finds no child. Correct the conditional "
                        "count while preserving empty parents and counting each entity once. "
                        "Prefer one grouped query for the requested table of output columns."
                    )
    return None


def empty_parent_join_conflict(question, queries, snapshot):
    """Identify a direct inner join that contradicts a supplied relationship.

    Only a single SELECT, named physical parent/child, explicit ``references``
    definition and direct equality join are considered. Subqueries, set queries
    and multiple captures may legitimately reconstruct coverage; leave them to
    evidence inspection rather than guessing or rewriting their SQL.
    """
    if len(queries) != 1:
        return None
    # Quoted examples are not instructions. Ambiguous/quoted definitions are
    # outside this deliberately narrow check.
    question = re.sub(r'"[^"\n]*"|`[^`\n]*`|(?<!\w)\'[^\'\n]*\'(?!\w)', " ", question)
    requirement = re.search(
        r"\bincluding\s+(?:the\s+)?(\w+)\s+(?:without|with\s+no)\s+(\w+)\b",
        question,
        re.I,
    )
    if not requirement:
        return None

    def names(noun):
        return {noun.casefold(), noun.casefold() + "s"}

    parent_word, child_word = requirement.groups()
    parents = [t for t in snapshot.tables if t.table_name.casefold() in names(parent_word)]
    children = [t for t in snapshot.tables if t.table_name.casefold() in names(child_word)]
    if len(parents) != 1 or len(children) != 1:
        return None
    parent, child = parents[0], children[0]
    definitions = re.findall(r"\b(\w+)\.(\w+)\s+references\s+(\w+)\.(\w+)\b", question)
    keys = {
        (child_key, parent_key)
        for child_name, child_key, parent_name, parent_key in definitions
        if child_name == child.table_name and parent_name == parent.table_name
    }
    if not keys:
        return None
    try:
        statements = parse_sql(queries[0].sql)
    except ParseError:
        return None  # The existing SQL validator handles invalid SQL.
    if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
        return None
    statement = statements[0].stmt
    if (
        statement.larg
        or statement.rarg
        or statement.withClause
        or len(statement.fromClause or ()) != 1
    ):
        return None

    def identity(relation, table):
        return relation.schemaname == table.schema_name and relation.relname == table.table_name

    def column(value):
        if not isinstance(value, ast.ColumnRef):
            return None
        fields = value.fields or ()
        if not all(isinstance(field, ast.String) for field in fields):
            return None
        return tuple(field.sval for field in fields)

    def direct_conflict(node):
        if not isinstance(node, ast.JoinExpr):
            return False
        if node.jointype not in (JoinType.JOIN_INNER, JoinType.JOIN_LEFT):
            return False
        if (
            node.jointype == JoinType.JOIN_INNER
            and isinstance(node.larg, ast.RangeVar)
            and isinstance(node.rarg, ast.RangeVar)
        ):
            for p, c in ((node.larg, node.rarg), (node.rarg, node.larg)):
                if not (identity(p, parent) and identity(c, child)):
                    continue
                predicate = node.quals
                if not isinstance(predicate, ast.A_Expr) or tuple(
                    x.sval for x in predicate.name or ()
                ) != ("=",):
                    continue
                actual = {column(predicate.lexpr), column(predicate.rexpr)}
                for child_key, parent_key in keys:
                    expected = {
                        (c.alias.aliasname if c.alias else c.relname, child_key),
                        (p.alias.aliasname if p.alias else p.relname, parent_key),
                    }
                    if actual == expected:
                        return True
        # An optional right subtree cannot prove loss of the outer left parent.
        if node.jointype == JoinType.JOIN_LEFT:
            return direct_conflict(node.larg)
        return direct_conflict(node.larg) or direct_conflict(node.rarg)

    if any(direct_conflict(node) for node in statement.fromClause or ()):
        return (
            f"The question explicitly includes {parent.table_name} without {child.table_name}, "
            "but the direct INNER JOIN on the supplied relationship excludes those parents. "
            "Replan to preserve them, including subsequent joins and filters. Do not invent "
            "rows, change the requested calculation, or count multiplied child rows as parents."
        )
    return None
