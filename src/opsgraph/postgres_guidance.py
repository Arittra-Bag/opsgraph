"""Generate PostgreSQL least-privilege guidance without executing it."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MAX_TABLES = 100


@dataclass(frozen=True, slots=True)
class PostgresRoleGuide:
    """A reviewable, non-executing role-provisioning plan."""

    executed: bool
    role_name: str
    database: str | None
    allowed_tables: tuple[str, ...]
    sql: str
    password_setup: tuple[str, ...]
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation for the API boundary."""

        return asdict(self)


def validate_identifier(value: str, *, label: str) -> str:
    """Accept only unambiguous lowercase PostgreSQL identifiers."""

    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must be a lowercase PostgreSQL identifier containing only "
            "letters, numbers, and underscores"
        )
    return value


def validate_qualified_table(value: str) -> str:
    """Validate an exact schema-qualified relation name."""

    parts = value.split(".")
    if len(parts) != 2:
        raise ValueError("tables must be schema-qualified lowercase PostgreSQL identifiers")
    validate_identifier(parts[0], label="table schema")
    validate_identifier(parts[1], label="table name")
    return value


def intersect_policy_tables(
    requested_tables: tuple[str, ...],
    *,
    allowed_schemas: tuple[str, ...],
    allowed_tables: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the exact requested relations permitted by current policy."""

    requested = _unique_validated_tables(requested_tables)
    schema_ceiling = frozenset(allowed_schemas)
    table_ceiling = frozenset(allowed_tables)
    return tuple(
        table
        for table in requested
        if table.split(".", 1)[0] in schema_ceiling
        and (not table_ceiling or table in table_ceiling)
    )


def build_postgres_role_guide(
    *,
    role_name: str,
    tables: tuple[str, ...],
    timeout_ms: int,
    database: str | None = None,
) -> PostgresRoleGuide:
    """Build SQL for one explicitly bounded, read-only PostgreSQL role."""

    role = validate_identifier(role_name, label="role name")
    permitted_tables = _unique_validated_tables(tables)
    if not permitted_tables:
        raise ValueError("at least one policy-approved table is required")
    if not 100 <= timeout_ms <= 30_000:
        raise ValueError("statement timeout must be between 100 and 30000 milliseconds")
    database_name = (
        validate_identifier(database, label="database name") if database is not None else None
    )

    quoted_role = _quote_identifier(role)
    statements = [
        (
            f"CREATE ROLE {quoted_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;"
        ),
        f"ALTER ROLE {quoted_role} SET default_transaction_read_only = on;",
        f"ALTER ROLE {quoted_role} SET statement_timeout = '{timeout_ms}ms';",
    ]
    if database_name is not None:
        statements.append(
            f"GRANT CONNECT ON DATABASE {_quote_identifier(database_name)} TO {quoted_role};"
        )
    for schema in _unique_schemas(permitted_tables):
        statements.append(f"GRANT USAGE ON SCHEMA {_quote_identifier(schema)} TO {quoted_role};")
    statements.extend(
        f"GRANT SELECT ON TABLE {_quote_qualified_table(table)} TO {quoted_role};"
        for table in permitted_tables
    )

    createuser = (
        "createuser --pwprompt --no-superuser --no-createdb --no-createrole "
        f"--no-inherit --no-replication --no-bypassrls {role}"
    )
    return PostgresRoleGuide(
        executed=False,
        role_name=role,
        database=database_name,
        allowed_tables=permitted_tables,
        sql="\n".join(statements),
        password_setup=(
            f"Use `{createuser}` instead of the CREATE ROLE statement to set the password "
            "interactively, then apply the remaining SQL.",
            f"Or, after applying CREATE ROLE, run `\\password {role}` inside psql.",
        ),
        notes=(
            "Review and run this script manually as a PostgreSQL administrator.",
            "OpsGraph did not connect to PostgreSQL or execute any statement.",
            "Grant only the listed tables and set the password outside this script.",
            "Audit existing PUBLIC and function privileges separately; this script makes no "
            "global privilege changes.",
        ),
    )


def _unique_validated_tables(tables: tuple[str, ...]) -> tuple[str, ...]:
    if len(tables) > _MAX_TABLES:
        raise ValueError(f"at most {_MAX_TABLES} tables may be requested")
    return tuple(dict.fromkeys(validate_qualified_table(table) for table in tables))


def _unique_schemas(tables: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(table.split(".", 1)[0] for table in tables))


def _quote_identifier(value: str) -> str:
    validate_identifier(value, label="identifier")
    return f'"{value}"'


def _quote_qualified_table(value: str) -> str:
    validate_qualified_table(value)
    schema, table = value.split(".", 1)
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
