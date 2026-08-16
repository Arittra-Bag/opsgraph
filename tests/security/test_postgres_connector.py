from dataclasses import dataclass

import pytest

from opsgraph.brokers import (
    ConnectorUnavailable,
    PsycopgReadOnlyExecutor,
    UnsafeDatabaseRole,
)


@dataclass
class Column:
    name: str


class FakeCursor:
    def __init__(self, *, unsafe_role: bool = False, inherited_write: bool = False) -> None:
        self.unsafe_role = unsafe_role
        self.inherited_write = inherited_write
        self.executed = []
        self.description = None
        self._one = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql == "SHOW transaction_read_only":
            self._one = ("on",)
        elif "FROM pg_catalog.pg_roles" in sql:
            self._one = (self.unsafe_role, False, False, False, False)
        elif "role_table_grants" in sql:
            self._one = (self.inherited_write,)
        elif sql.startswith("SELECT * FROM"):
            self.description = (Column("id"),)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return [(1,), (2,)]


class FakeConnection:
    def __init__(self, *, unsafe_role: bool = False, inherited_write: bool = False) -> None:
        self._cursor = FakeCursor(
            unsafe_role=unsafe_role,
            inherited_write=inherited_write,
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
    def fetchall(self):
        if self.executed and "information_schema.columns" in self.executed[-1][0]:
            return [
                ("public", "jobs", "id", "bigint", "NO", None),
                ("public", "jobs", "status", "text", "YES", "'queued'::text"),
            ]
        return super().fetchall()


class SchemaConnection(FakeConnection):
    def __init__(self) -> None:
        self._cursor = SchemaCursor()
        self.rolled_back = False
        self.closed = False


def test_executor_forces_read_only_timeout_and_redacts_dsn() -> None:
    connection = FakeConnection()
    secret = "postgresql://operator:never-print-this@db/app"  # noqa: S105
    executor = PsycopgReadOnlyExecutor(secret, connector=lambda *args, **kwargs: connection)

    result = executor.execute_readonly("SELECT * FROM public.incidents LIMIT 3", timeout_ms=900)

    assert repr(executor) == "PsycopgReadOnlyExecutor(dsn=<redacted>)"
    assert secret not in repr(executor)
    assert connection._cursor.executed[0] == ("BEGIN READ ONLY", None)
    assert connection._cursor.executed[1][1] == ("900ms",)
    assert connection._cursor.executed[2] == (
        "SELECT set_config('search_path', 'pg_catalog', true)",
        None,
    )
    assert result.rows == ((1,), (2,))
    assert connection.rolled_back and connection.closed


def test_executor_rejects_privileged_role_before_user_query() -> None:
    connection = FakeConnection(unsafe_role=True)
    executor = PsycopgReadOnlyExecutor("secret-dsn", connector=lambda *args, **kwargs: connection)

    with pytest.raises(UnsafeDatabaseRole, match="elevated privileges"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)

    assert not any(
        sql.startswith("SELECT * FROM public.incidents") for sql, _ in connection._cursor.executed
    )


def test_executor_rejects_write_grants_from_enabled_inherited_roles() -> None:
    connection = FakeConnection(inherited_write=True)
    executor = PsycopgReadOnlyExecutor("secret-dsn", connector=lambda *args, **kwargs: connection)

    with pytest.raises(UnsafeDatabaseRole, match="write privileges"):
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)

    grant_check = next(sql for sql, _ in connection._cursor.executed if "role_table_grants" in sql)
    assert "grantee = current_user" not in grant_check


def test_connector_errors_do_not_expose_dsn_or_driver_message() -> None:
    secret = "postgresql://operator:password@db/app"  # noqa: S105

    def fail(*args, **kwargs):
        raise RuntimeError(f"could not connect using {secret}")

    executor = PsycopgReadOnlyExecutor(secret, connector=fail)
    with pytest.raises(ConnectorUnavailable) as caught:
        executor.execute_readonly("SELECT * FROM public.incidents", timeout_ms=900)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


def test_schema_discovery_reads_metadata_only_and_hashes_snapshot() -> None:
    connection = SchemaConnection()
    executor = PsycopgReadOnlyExecutor("secret-dsn", connector=lambda *args, **kwargs: connection)

    snapshot = executor.discover_snapshot(allowed_schemas=("public",))

    assert snapshot.fingerprint.startswith("sha256:")
    assert snapshot.tables[0].table_name == "jobs"
    assert [column.name for column in snapshot.tables[0].columns] == ["id", "status"]
    discovery = next(
        (sql, params)
        for sql, params in connection._cursor.executed
        if "information_schema.columns" in sql
    )
    assert discovery[1] == (["public"],)
    assert connection.rolled_back and connection.closed
