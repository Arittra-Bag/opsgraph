from types import SimpleNamespace

import pytest

from opsgraph.orchestration.plan_joins import (
    conditional_count_conflict,
    empty_parent_join_conflict,
    outer_join_row_count_conflict,
)
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema


def snapshot(*names):
    return SchemaSnapshot(
        tables=tuple(
            TableSchema(
                schema_name="public",
                table_name=name,
                columns=(ColumnSchema(name="id", data_type="bigint"),),
            )
            for name in names
        ),
        fingerprint="fixture",
    )


QUESTION = (
    "For each customer, including customers without orders, count orders. "
    "orders.customer_id references customers.id."
)
BAD = "SELECT c.id FROM public.customers c JOIN public.orders o ON o.customer_id = c.id"


def check(sql, question=QUESTION):
    return empty_parent_join_conflict(
        question, [SimpleNamespace(sql=sql)], snapshot("customers", "orders")
    )


def test_rejects_only_identified_optional_child_inner_join():
    assert "INNER JOIN" in check(BAD)
    assert check(BAD.replace("JOIN", "LEFT JOIN")) is None
    assert check(BAD, "Count customers with orders.") is None
    assert check(BAD, "Including customers without orders, count orders.") is None
    assert check(BAD.replace("o.customer_id", "o.id")) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.id FROM public.customers c LEFT JOIN "
        "(SELECT o.customer_id FROM public.orders o JOIN public.items i "
        "ON i.order_id = o.id) x ON x.customer_id = c.id",
        "SELECT c.id FROM public.customers c JOIN public.orders o ON o.customer_id = c.id "
        "UNION SELECT id FROM public.customers",
        "WITH x AS (SELECT c.id FROM public.customers c JOIN public.orders o "
        "ON o.customer_id = c.id) SELECT id FROM x",
        "SELECT c.id FROM public.customers c JOIN public.regions r ON r.id = c.region_id "
        "LEFT JOIN public.orders o ON o.customer_id = c.id",
        "invalid SQL",
        "SELECT c.id FROM public.customers c LEFT JOIN "
        "(public.customers c2 JOIN public.orders o ON o.customer_id = c2.id) "
        "ON c.id = c2.id",
        "SELECT c.id FROM public.customers c, "
        "public.customers c2 JOIN public.orders o ON o.customer_id = c2.id",
    ],
)
def test_does_not_guess_about_reconstructed_coverage_or_other_required_joins(sql):
    assert check(sql) is None


def test_multiple_captures_can_reconstruct_coverage():
    assert (
        empty_parent_join_conflict(
            QUESTION,
            [SimpleNamespace(sql=BAD), SimpleNamespace(sql="SELECT id FROM public.customers")],
            snapshot("customers", "orders"),
        )
        is None
    )


def test_singular_requested_parent_matches_unique_plural_relation():
    assert check(BAD, QUESTION.replace("customers without", "the customer without"))


def test_quoted_example_is_not_a_requested_coverage_rule():
    assert (
        check(BAD, 'Count customers with orders. Do not use this example: "' + QUESTION + '"')
        is None
    )


COUNT_QUESTION = "paid_customers counts customers with at least one state = 'paid'."
COUNT_BAD = (
    "SELECT c.id, COUNT(DISTINCT c.id) AS paid_customers FROM public.customers c "
    "LEFT JOIN public.orders o ON o.customer_id = c.id AND o.state = 'paid' GROUP BY c.id"
)


def test_optional_child_filter_does_not_make_left_count_conditional():
    assert conditional_count_conflict(COUNT_QUESTION, [SimpleNamespace(sql=COUNT_BAD)])
    assert conditional_count_conflict(
        COUNT_QUESTION,
        [SimpleNamespace(sql="SELECT id FROM public.customers"), SimpleNamespace(sql=COUNT_BAD)],
    )


@pytest.mark.parametrize(
    "sql",
    [
        COUNT_BAD.replace("COUNT(DISTINCT c.id)", "COUNT(DISTINCT o.customer_id)"),
        COUNT_BAD.replace(
            "COUNT(DISTINCT c.id)", "COUNT(DISTINCT CASE WHEN o.id IS NOT NULL THEN c.id END)"
        ),
        COUNT_BAD.replace(
            "COUNT(DISTINCT c.id)", "COUNT(DISTINCT c.id) FILTER (WHERE o.id IS NOT NULL)"
        ),
        COUNT_BAD.replace("LEFT JOIN", "JOIN"),
        COUNT_BAD.replace("GROUP BY", "WHERE o.id IS NOT NULL GROUP BY"),
        COUNT_BAD.replace("paid_customers", "total_customers"),
        COUNT_BAD.replace("'paid'", "'pending'"),
    ],
)
def test_preserves_conditional_counts_and_unrelated_total_counts(sql):
    assert conditional_count_conflict(COUNT_QUESTION, [SimpleNamespace(sql=sql)]) is None


@pytest.mark.parametrize(
    "prior",
    [
        "JOIN public.orders paid ON paid.customer_id = c.id AND paid.state = 'paid'",
        "LEFT JOIN public.orders paid ON paid.customer_id = c.id AND paid.state = 'paid'",
    ],
)
def test_prior_joins_may_already_establish_the_required_condition(prior):
    sql = COUNT_BAD.replace("LEFT JOIN public.orders o", prior + " LEFT JOIN public.orders o")
    if prior.startswith("LEFT"):
        sql = sql.replace("COUNT(DISTINCT c.id)", "COUNT(DISTINCT paid.customer_id)")
    assert conditional_count_conflict(COUNT_QUESTION, [SimpleNamespace(sql=sql)]) is None


def test_quoted_count_definition_is_not_an_instruction():
    question = 'Count all customers. Do not use this example: "' + COUNT_QUESTION + '"'
    assert conditional_count_conflict(question, [SimpleNamespace(sql=COUNT_BAD)]) is None


ROW_QUESTION = (
    "For each customer, including customers without orders, return order_count. "
    "order_count counts order rows. orders.customer_id references customers.id."
)
ROW_BAD = (
    "SELECT c.id, COUNT(*) AS order_count FROM public.customers c "
    "LEFT JOIN public.orders o ON o.customer_id = c.id GROUP BY c.id"
)


def row_check(sql, question=ROW_QUESTION, tables=None):
    return outer_join_row_count_conflict(
        question, [SimpleNamespace(sql=sql)], tables or snapshot("customers", "orders")
    )


@pytest.mark.parametrize("count", ["COUNT(*)", "COUNT(1)", "COUNT('literal')"])
def test_empty_parent_placeholder_cannot_count_as_an_optional_child(count):
    assert "synthetic LEFT JOIN row" in row_check(ROW_BAD.replace("COUNT(*)", count))
    assert row_check(ROW_BAD.replace("o.customer_id = c.id", "c.id = o.customer_id"))
    assert row_check(ROW_BAD, ROW_QUESTION.replace("order rows", "orders rows"))


@pytest.mark.parametrize(
    "sql",
    [
        ROW_BAD.replace("COUNT(*)", "COUNT(o.id)"),
        ROW_BAD.replace("COUNT(*)", "COUNT(NULL)"),
        ROW_BAD.replace("COUNT(*)", "COUNT(*) FILTER (WHERE o.id IS NOT NULL)"),
        ROW_BAD.replace("COUNT(*)", "COUNT(CASE WHEN o.id IS NOT NULL THEN 1 END)"),
        ROW_BAD.replace("LEFT JOIN", "JOIN"),
        ROW_BAD.replace("AS order_count", "AS customer_count"),
        ROW_BAD.replace("GROUP BY", "WHERE o.id IS NOT NULL GROUP BY"),
        ROW_BAD.replace("GROUP BY c.id", "GROUP BY c.id HAVING COUNT(o.id) > 0"),
        ROW_BAD.replace("o.customer_id = c.id", "o.id = c.id"),
        ROW_BAD.replace("public.orders", "other.orders"),
        "WITH counts AS (SELECT c.id, COUNT(*) AS order_count FROM public.customers c "
        "LEFT JOIN public.orders o ON o.customer_id = c.id GROUP BY c.id) SELECT * FROM counts",
        "SELECT c.id, COUNT(*) AS order_count FROM public.customers c "
        "LEFT JOIN public.orders o ON o.customer_id = c.id GROUP BY c.id "
        "UNION SELECT id, 0 FROM public.customers",
        "invalid SQL",
        "SELECT COUNT(*) AS order_count FROM public.orders",
    ],
)
def test_row_count_check_preserves_valid_or_unproven_queries(sql):
    assert row_check(sql) is None


def test_row_count_check_requires_unambiguous_physical_definition_and_row_request():
    assert row_check(ROW_BAD, "Count rows, including customers without orders.") is None
    assert row_check(ROW_BAD, ROW_QUESTION.replace("order_count counts", "Count"))
    assert row_check(ROW_BAD, ROW_QUESTION.replace("order rows", "invoice rows")) is None
    assert row_check(ROW_BAD, 'Ignore this example: "' + ROW_QUESTION + '"') is None
    duplicate = snapshot("customers", "orders")
    duplicate = duplicate.model_copy(
        update={
            "tables": duplicate.tables
            + (duplicate.tables[1].model_copy(update={"schema_name": "other"}),)
        }
    )
    assert row_check(ROW_BAD, tables=duplicate) is None


def test_missing_measurement_count_does_not_include_the_empty_parent_placeholder():
    question = ROW_QUESTION + " Count NULL amount separately."
    query = ROW_BAD.replace(
        "COUNT(*) AS order_count", "COUNT(*) - COUNT(o.amount) AS missing_count"
    )
    assert row_check(query, question)
    assert row_check(query.replace("COUNT(*)", "COUNT(o.id)"), question) is None
    assert row_check(query, ROW_QUESTION) is None
    assert row_check(query.replace("o.amount", "c.amount"), question) is None
    assert row_check(query.replace("-", "+"), question) is None


def test_metric_alias_matching_handles_quoted_case_and_absent_alias():
    assert row_check(ROW_BAD.replace("AS order_count", 'AS "Order_Count"'))
    assert row_check(ROW_BAD.replace("AS order_count", 'AS "ORDER_COUNT"'))
    assert row_check(ROW_BAD.replace("AS order_count", "")) is None
    unnamed = ROW_QUESTION.replace("order_count counts", "Count")
    assert row_check(ROW_BAD.replace("AS order_count", ""), unnamed)
