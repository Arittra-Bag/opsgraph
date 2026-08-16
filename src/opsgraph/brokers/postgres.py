"""Isolated PostgreSQL read-only execution boundary."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from opsgraph.domain.models import stable_hash
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema

from .query import QueryResult


class ConnectorUnavailable(RuntimeError):
    """Raised without driver/DSN details when the connector cannot operate."""


class UnsafeDatabaseRole(PermissionError):
    """Raised when the configured PostgreSQL role is not acceptably read-only."""


class PsycopgReadOnlyExecutor:
    """Execute validated SQL inside a forced read-only PostgreSQL transaction.

    The DSN is deliberately private and omitted from repr/errors. Callers should
    provide a secret-store-resolved DSN, never pass it through model/tool state.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout_seconds: int = 5,
        connector: Callable[..., Any] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        if not 1 <= connect_timeout_seconds <= 30:
            raise ValueError("connect timeout must be between 1 and 30 seconds")
        self._dsn = dsn
        self._connect_timeout_seconds = connect_timeout_seconds
        self._connector = connector

    def __repr__(self) -> str:
        return f"{type(self).__name__}(dsn=<redacted>)"

    def execute_readonly(self, sql: str, *, timeout_ms: int) -> QueryResult:
        if not 100 <= timeout_ms <= 30_000:
            raise ValueError("statement timeout must be between 100 and 30000 ms")
        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            cursor.execute("BEGIN READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{timeout_ms}ms",),
            )
            # Relations must already be schema-qualified by the validator. Keep
            # function/operator resolution inside trusted built-in pg_catalog.
            cursor.execute("SELECT set_config('search_path', 'pg_catalog', true)")
            self._verify_read_only_role(cursor)
            cursor.execute(sql)
            if cursor.description is None:
                raise ConnectorUnavailable("database returned no result set")
            columns = tuple(column.name for column in cursor.description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
            return QueryResult(columns=columns, rows=rows)
        except (UnsafeDatabaseRole, ConnectorUnavailable):
            raise
        except Exception:
            raise ConnectorUnavailable("read-only database operation failed") from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()

    def discover_schemas(self) -> tuple[str, ...]:
        """Return visible non-system schemas under the same verified role."""

        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            cursor.execute("BEGIN READ ONLY")
            self._verify_read_only_role(cursor)
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name <> 'information_schema' "
                "AND schema_name NOT LIKE 'pg\\_%' ESCAPE '\\' ORDER BY schema_name"
            )
            return tuple(str(row[0]) for row in cursor.fetchall())
        except (UnsafeDatabaseRole, ConnectorUnavailable):
            raise
        except Exception:
            raise ConnectorUnavailable("schema discovery failed") from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()

    def discover_snapshot(self, *, allowed_schemas: tuple[str, ...]) -> SchemaSnapshot:
        """Discover visible tables and columns without reading application rows."""

        if not allowed_schemas:
            raise ValueError("at least one allowed schema is required")
        if len(allowed_schemas) > 100 or any(
            not schema or len(schema) > 128 for schema in allowed_schemas
        ):
            raise ValueError("allowed schemas must contain 1-128 characters")
        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            cursor.execute("BEGIN READ ONLY")
            self._verify_read_only_role(cursor)
            cursor.execute(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable, "
                "column_default FROM information_schema.columns "
                "WHERE table_schema = ANY(%s) ORDER BY table_schema, table_name, ordinal_position",
                (list(allowed_schemas),),
            )
            grouped: dict[tuple[str, str], list[ColumnSchema]] = {}
            for schema, table, column, data_type, nullable, default in cursor.fetchall():
                grouped.setdefault((str(schema), str(table)), []).append(
                    ColumnSchema(
                        name=str(column),
                        data_type=str(data_type),
                        nullable=str(nullable).upper() == "YES",
                        default=None if default is None else str(default)[:500],
                    )
                )
            tables = tuple(
                TableSchema(schema_name=schema, table_name=table, columns=tuple(columns))
                for (schema, table), columns in sorted(grouped.items())
            )
            payload = [table.model_dump(mode="json") for table in tables]
            return SchemaSnapshot(tables=tables, fingerprint=stable_hash(payload))
        except (UnsafeDatabaseRole, ConnectorUnavailable):
            raise
        except Exception:
            raise ConnectorUnavailable("schema discovery failed") from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()

    def _connect(self):
        connector = self._connector
        if connector is None:
            try:
                import psycopg
            except ImportError:
                raise ConnectorUnavailable("PostgreSQL connector is unavailable") from None
            connector = psycopg.connect
        try:
            return connector(
                self._dsn,
                autocommit=True,
                connect_timeout=self._connect_timeout_seconds,
                application_name="opsgraph-readonly",
            )
        except Exception:
            raise ConnectorUnavailable("PostgreSQL connection failed") from None

    @staticmethod
    def _verify_read_only_role(cursor) -> None:  # noqa: ANN001 - psycopg cursor protocol
        cursor.execute("SHOW transaction_read_only")
        row = cursor.fetchone()
        if not row or str(row[0]).lower() != "on":
            raise UnsafeDatabaseRole("database transaction is not read-only")
        cursor.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
        flags = cursor.fetchone()
        if not flags or any(bool(value) for value in flags):
            raise UnsafeDatabaseRole("database role has unsafe elevated privileges")
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.role_table_grants "
            "WHERE privilege_type IN "
            "('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER'))"
        )
        write_grant = cursor.fetchone()
        if write_grant and bool(write_grant[0]):
            raise UnsafeDatabaseRole("database role has table write privileges")
