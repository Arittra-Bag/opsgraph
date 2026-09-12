from types import SimpleNamespace

import pytest

from opsgraph.orchestration.plan_joins import conditional_count_conflict, empty_parent_join_conflict
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
