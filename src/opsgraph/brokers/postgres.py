"""Isolated PostgreSQL read-only execution boundary."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from opsgraph.domain.models import stable_hash
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema

from .query import QueryResult


class ConnectorUnavailable(RuntimeError):
    """Raised without driver/DSN details when the connector cannot operate."""


class QueryExecutionFailed(ConnectorUnavailable):
    """PostgreSQL rejected a query's syntax, cardinality or data operation.

    Retains the legacy synchronous connector-error contract. The durable API
    distinguishes this from connectivity failures without exposing driver text.
    """


class UnsafeDatabaseRole(PermissionError):
    """Raised when the configured PostgreSQL role is not acceptably read-only."""


class SourceSchemaChanged(RuntimeError):
    """Raised when query execution does not share the reviewed schema fingerprint."""


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
        allow_insecure_remote: bool = False,
        allowed_tables: tuple[str, ...] | None = None,
        allowed_schemas: tuple[str, ...] | None = None,
        expected_schema_fingerprint: str | None = None,
        connector: Callable[..., Any] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        if not 1 <= connect_timeout_seconds <= 30:
            raise ValueError("connect timeout must be between 1 and 30 seconds")
        if not isinstance(allow_insecure_remote, bool):
            raise ValueError("insecure PostgreSQL transport override must be boolean")
        self._validate_transport(dsn, allow_insecure_remote=allow_insecure_remote)
        self._dsn = dsn
        self._connect_timeout_seconds = connect_timeout_seconds
        self._allowed_tables = allowed_tables
        self._allowed_schemas = allowed_schemas
        self._expected_schema_fingerprint = expected_schema_fingerprint
        if expected_schema_fingerprint and (not allowed_schemas or not allowed_tables):
            raise ValueError("schema-bound execution requires explicit schema and table scope")
        self._connector = connector
        self._active = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(dsn=<redacted>)"

    def cancel(self) -> None:
        """Request driver cancellation when supported; statement timeout remains fallback."""
        with self._lock:
            connection = self._active
            if connection is not None and hasattr(connection, "cancel_safe"):
                connection.cancel_safe(timeout=1.0)

    def execute_readonly(self, sql: str, *, timeout_ms: int) -> QueryResult:
        if not 100 <= timeout_ms <= 30_000:
            raise ValueError("statement timeout must be between 100 and 30000 ms")
        connection = None
        query_started = False
        try:
            connection = self._connect()
            with self._lock:
                self._active = connection
            cursor = connection.cursor()
            cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{timeout_ms}ms",),
            )
            # Relations must already be schema-qualified by the validator. Keep
            # function/operator resolution inside trusted built-in pg_catalog.
            cursor.execute("SELECT set_config('search_path', 'pg_catalog', true)")
            self._verify_read_only_role(cursor)
            if not self._allowed_tables:
                raise UnsafeDatabaseRole("an explicit approved table scope is required")
            self._verify_relation_scope(cursor, self._allowed_tables)
            if self._expected_schema_fingerprint:
                live = self._snapshot_from_cursor(
                    cursor,
                    allowed_schemas=self._allowed_schemas or (),
                    allowed_tables=self._allowed_tables,
                )
                if live.fingerprint != self._expected_schema_fingerprint:
                    raise SourceSchemaChanged(
                        "source schema changed after review; inspect it again before querying"
                    )
            query_started = True
            cursor.execute(sql)
            if cursor.description is None:
                raise ConnectorUnavailable("database returned no result set")
            columns = tuple(column.name for column in cursor.description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
            return QueryResult(columns=columns, rows=rows)
        except (SourceSchemaChanged, UnsafeDatabaseRole, ConnectorUnavailable):
            raise
        except Exception as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            if (
                query_started
                and isinstance(sqlstate, str)
                and sqlstate[:2] in {"21", "22", "42"}
                and sqlstate != "42501"
            ):
                raise QueryExecutionFailed(
                    "PostgreSQL could not execute the proposed query. "
                    "Inspect the recorded query's joins, columns, grouping and value types. "
                    "Clarify the question or source definitions before a fresh retry."
                ) from None
            raise ConnectorUnavailable("read-only database operation failed") from None
        finally:
            with self._lock:
                self._active = None
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
            with self._lock:
                self._active = connection
            cursor = connection.cursor()
            cursor.execute("BEGIN READ ONLY")
            cursor.execute("SELECT set_config('statement_timeout', '5000ms', true)")
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
            with self._lock:
                self._active = None
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()

    def discover_snapshot(
        self,
        *,
        allowed_schemas: tuple[str, ...],
        allowed_tables: tuple[str, ...] | None = None,
        timeout_ms: int = 5_000,
    ) -> SchemaSnapshot:
        """Discover visible tables and columns without reading application rows."""

        if not allowed_schemas:
            raise ValueError("at least one allowed schema is required")
        if not 100 <= timeout_ms <= 30_000:
            raise ValueError("schema timeout must be between 100 and 30000 ms")
        if len(allowed_schemas) > 100 or any(
            not schema or len(schema) > 128 for schema in allowed_schemas
        ):
            raise ValueError("allowed schemas must contain 1-128 characters")
        connection = None
        try:
            connection = self._connect()
            with self._lock:
                self._active = connection
            cursor = connection.cursor()
            cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
            self._verify_read_only_role(cursor)
            table_scope = list(allowed_tables) if allowed_tables is not None else None
            if table_scope:
                self._verify_relation_scope(cursor, tuple(table_scope))
            return self._snapshot_from_cursor(
                cursor,
                allowed_schemas=allowed_schemas,
                allowed_tables=tuple(table_scope) if table_scope is not None else None,
            )
        except (UnsafeDatabaseRole, ConnectorUnavailable):
            raise
        except Exception:
            raise ConnectorUnavailable("schema discovery failed") from None
        finally:
            with self._lock:
                self._active = None
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()

    @staticmethod
    def _snapshot_from_cursor(
        cursor,  # noqa: ANN001 - psycopg cursor protocol
        *,
        allowed_schemas: tuple[str, ...],
        allowed_tables: tuple[str, ...] | None,
    ) -> SchemaSnapshot:
        """Read visible schema metadata inside the caller's database snapshot."""

        table_scope = list(allowed_tables) if allowed_tables is not None else None
        cursor.execute(
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, "
            "column_default FROM information_schema.columns "
            "WHERE table_schema = ANY(%s) "
            "AND pg_catalog.has_schema_privilege(table_schema, 'USAGE') "
            "AND pg_catalog.has_column_privilege("
            "pg_catalog.format('%%I.%%I', table_schema, table_name), column_name, 'SELECT') "
            "AND (%s::text[] IS NULL OR (table_schema || '.' || table_name) = ANY(%s)) "
            "ORDER BY table_schema, table_name, ordinal_position LIMIT 10001",
            (list(allowed_schemas), table_scope, table_scope),
        )
        grouped: dict[tuple[str, str], list[ColumnSchema]] = {}
        metadata_rows = cursor.fetchall()
        if len(metadata_rows) > 10_000:
            raise ConnectorUnavailable("schema metadata is too large; select fewer tables")
        for schema, table, column, data_type, nullable, default in metadata_rows:
            grouped.setdefault((str(schema), str(table)), []).append(
                ColumnSchema(
                    name=str(column),
                    data_type=str(data_type),
                    nullable=str(nullable).upper() == "YES",
                    default=None if default is None else str(default)[:500],
                )
            )
        cursor.execute(
            "SELECT relation_schema.nspname, relation.relname, relation.relkind, "
            "CASE WHEN relation.relkind IN ('v', 'm') "
            "THEN pg_catalog.pg_get_viewdef(relation.oid, false) ELSE NULL END, "
            "foreign_server.srvname, foreign_wrapper.fdwname, "
            "foreign_table.ftoptions, foreign_server.srvoptions "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS relation_schema "
            "ON relation_schema.oid = relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_foreign_table AS foreign_table "
            "ON foreign_table.ftrelid = relation.oid "
            "LEFT JOIN pg_catalog.pg_foreign_server AS foreign_server "
            "ON foreign_server.oid = foreign_table.ftserver "
            "LEFT JOIN pg_catalog.pg_foreign_data_wrapper AS foreign_wrapper "
            "ON foreign_wrapper.oid = foreign_server.srvfdw "
            "WHERE relation_schema.nspname = ANY(%s) "
            "AND relation.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND pg_catalog.has_schema_privilege(relation_schema.nspname, 'USAGE') "
            "AND pg_catalog.has_any_column_privilege(relation.oid, 'SELECT') "
            "AND (%s::text[] IS NULL OR "
            "(relation_schema.nspname || '.' || relation.relname) = ANY(%s)) "
            "ORDER BY relation_schema.nspname, relation.relname",
            (list(allowed_schemas), table_scope, table_scope),
        )
        relation_metadata = {
            (str(schema), str(table)): stable_hash(
                {
                    "kind": str(kind),
                    "definition": None if definition is None else str(definition),
                    "foreign_server": None if server is None else str(server),
                    "foreign_data_wrapper": None if wrapper is None else str(wrapper),
                    "foreign_table_options": sorted(str(item) for item in (table_options or ())),
                    "foreign_server_options": sorted(str(item) for item in (server_options or ())),
                }
            )
            for (
                schema,
                table,
                kind,
                definition,
                server,
                wrapper,
                table_options,
                server_options,
            ) in cursor.fetchall()
        }
        if set(relation_metadata) != set(grouped):
            raise ConnectorUnavailable("schema relation metadata is incomplete")
        tables = tuple(
            TableSchema(
                schema_name=schema,
                table_name=table,
                columns=tuple(columns),
                relation_fingerprint=relation_metadata[(schema, table)],
            )
            for (schema, table), columns in sorted(grouped.items())
        )
        payload = [table.model_dump(mode="json") for table in tables]
        return SchemaSnapshot(
            tables=tables, fingerprint=stable_hash(payload), inspected_at=datetime.now(UTC)
        )

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
    def _validate_transport(dsn: str, *, allow_insecure_remote: bool) -> None:
        """Require hostname-verifying TLS for every non-local destination."""

        try:
            params = conninfo_to_dict(dsn)
        except Exception:
            raise ConnectorUnavailable("PostgreSQL connection settings are invalid") from None
        if allow_insecure_remote:
            return

        host = params.get("host", os.getenv("PGHOST"))
        hostaddr = params.get("hostaddr", os.getenv("PGHOSTADDR"))
        service = params.get("service", os.getenv("PGSERVICE"))
        destinations = []
        for value in (host, hostaddr):
            if value is not None:
                destinations.extend(str(value).split(","))

        # A libpq service can hide a remote destination. It is safe only when
        # the resulting connection is required to authenticate its hostname.
        ambiguous = bool(service)
        remote = ambiguous or any(
            not PsycopgReadOnlyExecutor._is_local_host(item) for item in destinations
        )
        # Service files can supply both a hidden host address and their own TLS
        # mode. Only an sslmode in the parsed DSN can safely override a service.
        sslmode = params.get("sslmode")
        if sslmode is None and not service:
            sslmode = os.getenv("PGSSLMODE", "")
        if remote and str(sslmode).casefold() != "verify-full":
            raise ConnectorUnavailable(
                "Remote PostgreSQL requires sslmode=verify-full; an explicit deployment "
                "override is required to allow insecure remote transport"
            )

    @staticmethod
    def _is_local_host(value: str) -> bool:
        host = value.strip()
        if not host or host.startswith("/"):
            return True
        normalized = host.strip("[]").rstrip(".").casefold()
        if normalized == "localhost":
            return True
        try:
            return ip_address(normalized).is_loopback
        except ValueError:
            return False

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
        cursor.execute("SHOW server_version_num")
        version = cursor.fetchone()
        if not version:
            raise UnsafeDatabaseRole("database version could not be verified")
        privileges = ["INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
        if int(version[0]) >= 170000:
            privileges.append("MAINTAIN")
        cursor.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS relation_schema "
            "ON relation_schema.oid = relation.relnamespace "
            "WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND relation_schema.nspname <> 'information_schema' "
            "AND relation_schema.nspname NOT LIKE 'pg\\_%%' ESCAPE '\\' "
            "AND ("
            "EXISTS (SELECT 1 FROM pg_catalog.unnest(%s::text[]) AS privilege(name) "
            "WHERE pg_catalog.has_table_privilege(relation.oid, privilege.name)) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.unnest(%s::text[]) AS privilege(name) "
            "WHERE pg_catalog.has_any_column_privilege(relation.oid, privilege.name))"
            ")"
            ")",
            (privileges, ["INSERT", "UPDATE", "REFERENCES"]),
        )
        write_grant = cursor.fetchone()
        if write_grant and bool(write_grant[0]):
            raise UnsafeDatabaseRole("database role has table write privileges")

    @staticmethod
    def _verify_relation_scope(cursor, allowed_tables: tuple[str, ...]) -> None:  # noqa: ANN001
        """Reject approved parents whose descendants are outside the recorded scope."""

        cursor.execute(
            "WITH RECURSIVE descendants(root_oid, child_oid) AS ("
            "SELECT inheritance.inhparent, inheritance.inhrelid "
            "FROM pg_catalog.pg_inherits AS inheritance "
            "UNION ALL "
            "SELECT descendants.root_oid, inheritance.inhrelid "
            "FROM descendants JOIN pg_catalog.pg_inherits AS inheritance "
            "ON inheritance.inhparent = descendants.child_oid"
            ") "
            "SELECT EXISTS ("
            "SELECT 1 FROM descendants "
            "JOIN pg_catalog.pg_class AS parent ON parent.oid = descendants.root_oid "
            "JOIN pg_catalog.pg_namespace AS parent_schema "
            "ON parent_schema.oid = parent.relnamespace "
            "JOIN pg_catalog.pg_class AS child ON child.oid = descendants.child_oid "
            "JOIN pg_catalog.pg_namespace AS child_schema "
            "ON child_schema.oid = child.relnamespace "
            "WHERE (parent_schema.nspname || '.' || parent.relname) = ANY(%s::text[]) "
            "AND NOT ((child_schema.nspname || '.' || child.relname) = ANY(%s::text[]))"
            ")",
            (list(allowed_tables), list(allowed_tables)),
        )
        outside_scope = cursor.fetchone()
        if outside_scope and bool(outside_scope[0]):
            raise UnsafeDatabaseRole(
                "approved table scope includes an unapproved inherited or partition relation"
            )
