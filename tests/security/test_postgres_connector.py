from dataclasses import dataclass

import pytest

from opsgraph.brokers import (
    ConnectorUnavailable,
    PsycopgReadOnlyExecutor,
    QueryExecutionFailed,
    SourceSchemaChanged,
    UnsafeDatabaseRole,
)


@dataclass
class Column:
    name: str


class FakeCursor:
    def __init__(
        self,
        *,
        unsafe_role: bool = False,
        inherited_write: bool = False,
        column_write: bool = False,
        unapproved_descendant: bool = False,
        server_version: str = "180000",
    ) -> None:
        self.unsafe_role = unsafe_role
        self.inherited_write = inherited_write
        self.column_write = column_write
        self.unapproved_descendant = unapproved_descendant
        self.server_version = server_version
        self.executed = []
        self.description = None
        self._one = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql == "SHOW transaction_read_only":
            self._one = ("on",)
        elif "FROM pg_catalog.pg_roles" in sql:
            self._one = (self.unsafe_role, False, False, False, False)
        elif sql == "SHOW server_version_num":
            self._one = (self.server_version,)
        elif "has_table_privilege" in sql:
            self._one = (self.inherited_write or self.column_write,)
        elif sql.startswith("WITH RECURSIVE descendants"):
            self._one = (self.unapproved_descendant,)
        elif sql.startswith("SELECT * FROM"):
            self.description = (Column("id"),)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return [(1,), (2,)]


class FakeConnection:
    def __init__(
        self,
        *,
        unsafe_role: bool = False,
        inherited_write: bool = False,
        column_write: bool = False,
        unapproved_descendant: bool = False,
        server_version: str = "180000",
    ) -> None:
        self._cursor = FakeCursor(
            unsafe_role=unsafe_role,
            inherited_write=inherited_write,
            column_write=column_write,
            unapproved_descendant=unapproved_descendant,
            server_version=server_version,
        )
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class SchemaCursor(FakeCursor):
    def __init__(
        self,
        *,
        relation_kind="r",
        relation_definition=None,
        foreign_server=None,
        foreign_wrapper=None,
        foreign_table_options=None,
        foreign_server_options=None,
    ) -> None:
        super().__init__()
        self.relation_kind = relation_kind
        self.relation_definition = relation_definition
        self.foreign_server = foreign_server
        self.foreign_wrapper = foreign_wrapper
        self.foreign_table_options = foreign_table_options
        self.foreign_server_options = foreign_server_options

    def fetchall(self):
        if self.executed and "information_schema.columns" in self.executed[-1][0]:
            return [
                ("public", "jobs", "id", "bigint", "NO", None),
                ("public", "jobs", "status", "text", "YES", "'queued'::text"),
            ]
        if self.executed and "FROM pg_catalog.pg_class AS relation" in self.executed[-1][0]:
            return [
                (
                    "public",
                    "jobs",
                    self.relation_kind,
                    self.relation_definition,
                    self.foreign_server,
                    self.foreign_wrapper,
                    self.foreign_table_options,
                    self.foreign_server_options,
                )
            ]
        return super().fetchall()


class SchemaConnection(FakeConnection):
    def __init__(self, **relation_metadata) -> None:
        self._cursor = SchemaCursor(
            **relation_metadata,
        )
        self.rolled_back = False
        self.closed = False


def test_executor_forces_read_only_timeout_and_redacts_dsn() -> None:
    connection = FakeConnection()
    secret = "postgresql://operator:never-print-this@db/app?sslmode=verify-full"  # noqa: S105
    executor = PsycopgReadOnlyExecutor(
        secret,
        allowed_tables=("public.incidents",),
        connector=lambda *args, **kwargs: connection,
    )

    result = executor.execute_readonly("SELECT * FROM public.incidents LIMIT 3", timeout_ms=900)

    assert repr(executor) == "PsycopgReadOnlyExecutor(dsn=<redacted>)"
    assert secret not in repr(executor)
    assert connection._cursor.executed[0] == (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
        None,
    )
    assert connection._cursor.executed[1][1] == ("900ms",)
    assert connection._cursor.executed[2] == (
        "SELECT set_config('search_path', 'pg_catalog', true)",
        None,
    )
    assert result.rows == ((1,), (2,))
    assert connection.rolled_back and connection.closed


def test_executor_rejects_privileged_role_before_user_query() -> None:
    connection = FakeConnection(unsafe_role=True)
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *args, **kwargs: connection,
    )

    with pytest.raises(UnsafeDatabaseRole, match="elevated privileges"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)

    assert not any(
        sql.startswith("SELECT * FROM public.incidents") for sql, _ in connection._cursor.executed
    )


def test_executor_rejects_write_grants_from_enabled_inherited_roles() -> None:
    connection = FakeConnection(inherited_write=True)
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *args, **kwargs: connection,
    )

    with pytest.raises(UnsafeDatabaseRole, match="write privileges"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)

    grant_check, params = next(
        (sql, params) for sql, params in connection._cursor.executed if "has_table_privilege" in sql
    )
    assert "information_schema.role_table_grants" not in grant_check
    assert "has_any_column_privilege" in grant_check
    assert "JOIN pg_catalog.pg_namespace AS relation_schema" in grant_check
    assert "relation_schema.nspname <> 'information_schema'" in grant_check
    assert "relation_schema.nspname NOT LIKE 'pg\\_%%'" in grant_check
    assert params == (
        ["INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"],
        ["INSERT", "UPDATE", "REFERENCES"],
    )


def test_executor_rejects_effective_column_write_privileges() -> None:
    connection = FakeConnection(column_write=True)
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *args, **kwargs: connection,
    )

    with pytest.raises(UnsafeDatabaseRole, match="write privileges"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)


def test_executor_uses_effective_privileges_supported_by_pre_17_servers() -> None:
    connection = FakeConnection(server_version="160000")
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *args, **kwargs: connection,
    )

    executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)

    _, params = next(
        (sql, params) for sql, params in connection._cursor.executed if "has_table_privilege" in sql
    )
    assert params == (
        ["INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"],
        ["INSERT", "UPDATE", "REFERENCES"],
    )


def test_executor_rejects_unapproved_inheritance_or_partition_descendant() -> None:
    connection = FakeConnection(unapproved_descendant=True)
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *args, **kwargs: connection,
    )

    with pytest.raises(UnsafeDatabaseRole, match="unapproved inherited or partition relation"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)

    scope_sql, scope_params = next(
        (sql, params) for sql, params in connection._cursor.executed if "pg_inherits" in sql
    )
    assert "to_regclass" not in scope_sql
    assert "parent_schema.nspname || '.' || parent.relname" in scope_sql
    assert scope_params == (["public.incidents"], ["public.incidents"])
    assert not any(
        sql.startswith("SELECT * FROM public.incidents") for sql, _ in connection._cursor.executed
    )


def test_executor_refuses_queries_without_an_explicit_table_scope() -> None:
    connection = FakeConnection()
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app", connector=lambda *args, **kwargs: connection
    )

    with pytest.raises(UnsafeDatabaseRole, match="explicit approved table scope"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)


def test_executor_binds_schema_fingerprint_and_query_to_one_snapshot() -> None:
    connection = SchemaConnection()
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_schemas=("public",),
        allowed_tables=("public.jobs",),
        expected_schema_fingerprint="reviewed-fingerprint",
        connector=lambda *args, **kwargs: connection,
    )

    with pytest.raises(SourceSchemaChanged, match="schema changed after review"):
        executor.execute_readonly("SELECT * FROM public.jobs", timeout_ms=900)

    statements = [sql for sql, _ in connection._cursor.executed]
    assert statements[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert any("information_schema.columns" in sql for sql in statements)
    assert "SELECT * FROM public.jobs" not in statements


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql:///app",
        "host=/var/run/postgresql dbname=app",
        "postgresql://reader@localhost/app",
        "postgresql://reader@127.0.0.1/app",
        "postgresql://reader@[::1]/app",
        "host=localhost,127.0.0.1,::1 dbname=app",
    ],
)
def test_connector_permits_local_destinations_without_tls(dsn) -> None:
    PsycopgReadOnlyExecutor(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://private-user:private-password@db.internal/app",
        "host=db.internal dbname=app sslmode=require",
        "host=localhost,db.internal dbname=app",
        "host=localhost hostaddr=10.0.0.5 dbname=app",
        "service=private-production",
        "service=private-production host=localhost",
    ],
)
def test_connector_requires_hostname_verification_for_remote_or_hidden_destinations(dsn) -> None:
    with pytest.raises(ConnectorUnavailable, match="sslmode=verify-full") as caught:
        PsycopgReadOnlyExecutor(dsn)

    message = str(caught.value)
    assert "private-user" not in message
    assert "private-password" not in message
    assert "db.internal" not in message
    assert "private-production" not in message


def test_connector_accepts_remote_multi_host_only_with_verify_full() -> None:
    PsycopgReadOnlyExecutor("host=db-a.internal,db-b.internal dbname=app sslmode=verify-full")

    with pytest.raises(ValueError, match="must be boolean"):
        PsycopgReadOnlyExecutor(
            "postgresql://reader@db.internal/app", allow_insecure_remote="false"
        )


def test_connector_insecure_remote_override_is_explicit_and_narrow() -> None:
    PsycopgReadOnlyExecutor("postgresql://reader@db.internal/app", allow_insecure_remote=True)


def test_malformed_dsn_is_rejected_without_echoing_it() -> None:
    secret = "private-user:private-password@db.internal"  # noqa: S105
    with pytest.raises(ConnectorUnavailable, match="settings are invalid") as caught:
        PsycopgReadOnlyExecutor(secret)
    assert secret not in str(caught.value)


def test_connector_errors_do_not_expose_dsn_or_driver_message() -> None:
    secret = "postgresql://operator:password@db/app?sslmode=verify-full"  # noqa: S105

    def fail(*args, **kwargs):
        raise RuntimeError(f"could not connect using {secret}")

    executor = PsycopgReadOnlyExecutor(secret, allowed_tables=("public.incidents",), connector=fail)
    with pytest.raises(ConnectorUnavailable) as caught:
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("sqlstate", ["21000", "22012", "42601", "42703"])
def test_invalid_query_is_actionable_redacted_and_rolled_back(sqlstate) -> None:
    connection = FakeConnection()
    original = connection._cursor.execute

    class DriverError(RuntimeError):
        pass

    error = DriverError("private-driver-message-with-record-values")
    error.sqlstate = sqlstate

    def execute(sql, params=None):
        if sql.startswith("SELECT * FROM public.incidents"):
            raise error
        return original(sql, params)

    connection._cursor.execute = execute
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *a, **kw: connection,
    )
    with pytest.raises(QueryExecutionFailed) as caught:
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)
    assert "recorded query" in str(caught.value)
    assert "private-driver-message" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert connection.rolled_back and connection.closed


def test_role_check_error_is_not_mislabeled_as_a_model_query_failure() -> None:
    connection = FakeConnection()

    class DriverError(RuntimeError):
        sqlstate = "42703"

    def fail(sql, params=None):
        raise DriverError("private-driver-details")

    connection._cursor.execute = fail
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_tables=("public.incidents",),
        connector=lambda *a, **kw: connection,
    )
    with pytest.raises(ConnectorUnavailable) as caught:
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)
    assert not isinstance(caught.value, QueryExecutionFailed)
    assert "private-driver-details" not in str(caught.value)
    assert connection.rolled_back and connection.closed


def test_schema_discovery_reads_metadata_only_and_hashes_snapshot() -> None:
    connection = SchemaConnection()
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app", connector=lambda *args, **kwargs: connection
    )

    snapshot = executor.discover_snapshot(allowed_schemas=("public",))

    assert connection._cursor.executed[0] == (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
        None,
    )
    assert snapshot.fingerprint.startswith("sha256:")
    assert snapshot.tables[0].table_name == "jobs"
    assert snapshot.tables[0].relation_fingerprint.startswith("sha256:")
    assert [column.name for column in snapshot.tables[0].columns] == ["id", "status"]
    assert all(column.primary_key is None for column in snapshot.tables[0].columns)
    discovery = next(
        (sql, params)
        for sql, params in connection._cursor.executed
        if "information_schema.columns" in sql
    )
    assert discovery[1] == (["public"], None, None)
    assert "has_column_privilege" in discovery[0] and "'SELECT'" in discovery[0]
    assert "has_schema_privilege" in discovery[0] and "'USAGE'" in discovery[0]
    relation_discovery = next(
        (sql, params)
        for sql, params in connection._cursor.executed
        if "FROM pg_catalog.pg_class AS relation" in sql and "pg_foreign_table" in sql
    )
    assert "has_any_column_privilege" in relation_discovery[0]
    assert "has_schema_privilege" in relation_discovery[0]
    assert relation_discovery[1] == (["public"], None, None)
    assert snapshot.inspected_at is not None
    assert connection.rolled_back and connection.closed


def test_schema_fingerprint_binds_view_definition_without_exposing_it() -> None:
    reviewed = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        connector=lambda *args, **kwargs: SchemaConnection(
            relation_kind="v",
            relation_definition="SELECT id, status FROM private.queue_a",
        ),
    ).discover_snapshot(allowed_schemas=("public",), allowed_tables=("public.jobs",))
    changed_connection = SchemaConnection(
        relation_kind="v",
        relation_definition="SELECT id, status FROM private.queue_b",
    )
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_schemas=("public",),
        allowed_tables=("public.jobs",),
        expected_schema_fingerprint=reviewed.fingerprint,
        connector=lambda *args, **kwargs: changed_connection,
    )

    with pytest.raises(SourceSchemaChanged, match="schema changed after review"):
        executor.execute_readonly("SELECT * FROM public.jobs", timeout_ms=900)

    payload = reviewed.model_dump_json()
    assert "private.queue_a" not in payload
    assert not any(
        sql.startswith("SELECT * FROM public.jobs")
        for sql, _ in changed_connection._cursor.executed
    )


def test_schema_fingerprint_binds_foreign_routing_without_exposing_options() -> None:
    reviewed = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        connector=lambda *args, **kwargs: SchemaConnection(
            relation_kind="f",
            foreign_server="warehouse-a",
            foreign_wrapper="postgres_fdw",
            foreign_table_options=("table_name=private_jobs",),
            foreign_server_options=("host=private-a.internal",),
        ),
    ).discover_snapshot(allowed_schemas=("public",), allowed_tables=("public.jobs",))
    changed_connection = SchemaConnection(
        relation_kind="f",
        foreign_server="warehouse-b",
        foreign_wrapper="postgres_fdw",
        foreign_table_options=("table_name=private_jobs",),
        foreign_server_options=("host=private-b.internal",),
    )
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app",
        allowed_schemas=("public",),
        allowed_tables=("public.jobs",),
        expected_schema_fingerprint=reviewed.fingerprint,
        connector=lambda *args, **kwargs: changed_connection,
    )

    with pytest.raises(SourceSchemaChanged, match="schema changed after review"):
        executor.execute_readonly("SELECT * FROM public.jobs", timeout_ms=900)

    payload = reviewed.model_dump_json()
    assert "private_jobs" not in payload
    assert "private-a.internal" not in payload


def test_schema_discovery_rejects_unapproved_descendants_before_metadata_read() -> None:
    connection = FakeConnection(unapproved_descendant=True)
    executor = PsycopgReadOnlyExecutor(
        "host=localhost dbname=app", connector=lambda *args, **kwargs: connection
    )

    with pytest.raises(UnsafeDatabaseRole, match="unapproved inherited or partition relation"):
        executor.discover_snapshot(
            allowed_schemas=("public",), allowed_tables=("public.incidents",)
        )

    assert not any("information_schema.columns" in sql for sql, _ in connection._cursor.executed)
