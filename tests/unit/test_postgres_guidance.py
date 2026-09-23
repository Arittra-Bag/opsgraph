import pytest

from opsgraph.postgres_guidance import (
    build_postgres_role_guide,
    intersect_policy_tables,
)


def test_role_guide_is_exact_bounded_and_non_executing():
    guide = build_postgres_role_guide(
        role_name="opsgraph_reader",
        database="operations",
        tables=("public.incidents", "audit.events", "public.incidents"),
        timeout_ms=4_000,
    )

    assert guide.executed is False
    assert guide.allowed_tables == ("public.incidents", "audit.events")
    assert (
        'CREATE ROLE "opsgraph_reader" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
        "NOINHERIT NOREPLICATION NOBYPASSRLS;"
    ) in guide.sql
    assert 'ALTER ROLE "opsgraph_reader" SET default_transaction_read_only = on;' in guide.sql
    assert "ALTER ROLE \"opsgraph_reader\" SET statement_timeout = '4000ms';" in guide.sql
    assert 'GRANT CONNECT ON DATABASE "operations" TO "opsgraph_reader";' in guide.sql
    assert guide.sql.count('GRANT USAGE ON SCHEMA "public"') == 1
    assert guide.sql.count('GRANT USAGE ON SCHEMA "audit"') == 1
    assert 'GRANT SELECT ON TABLE "public"."incidents" TO "opsgraph_reader";' in guide.sql
    assert 'GRANT SELECT ON TABLE "audit"."events" TO "opsgraph_reader";' in guide.sql
    assert "PASSWORD" not in guide.sql
    assert "GRANT ALL" not in guide.sql
    assert "REVOKE" not in guide.sql
    assert "DEFAULT PRIVILEGES" not in guide.sql
    assert "--pwprompt" in guide.password_setup[0]
    assert "\\password opsgraph_reader" in guide.password_setup[1]


def test_database_connect_is_only_included_when_explicitly_requested():
    guide = build_postgres_role_guide(
        role_name="opsgraph_reader",
        tables=("public.incidents",),
        timeout_ms=5_000,
    )

    assert guide.database is None
    assert "GRANT CONNECT" not in guide.sql


def test_policy_intersection_requires_exact_qualified_table_matches():
    permitted = intersect_policy_tables(
        ("public.jobs", "public.events", "private.jobs"),
        allowed_schemas=("public", "private"),
        allowed_tables=("jobs", "public.jobs"),
    )

    assert permitted == ("public.jobs",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role_name", "reader;drop_role"),
        ("database", "ops-db"),
        ("tables", ("public.jobs;drop_table",)),
        ("tables", ("jobs",)),
    ],
)
def test_role_guide_rejects_ambiguous_or_injectable_identifiers(field, value):
    kwargs = {
        "role_name": "opsgraph_reader",
        "database": "operations",
        "tables": ("public.jobs",),
        "timeout_ms": 5_000,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        build_postgres_role_guide(**kwargs)


@pytest.mark.parametrize("timeout_ms", [99, 30_001])
def test_role_guide_rejects_unbounded_statement_timeouts(timeout_ms):
    with pytest.raises(ValueError, match="statement timeout"):
        build_postgres_role_guide(
            role_name="opsgraph_reader",
            tables=("public.jobs",),
            timeout_ms=timeout_ms,
        )
