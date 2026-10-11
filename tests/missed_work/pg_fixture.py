"""Disposable relations/role on an explicitly supplied loopback test database."""

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opsgraph.brokers.postgres import PsycopgReadOnlyExecutor
from opsgraph.domain import Obligation
from opsgraph.domain.work_outcome import WorkOutcomeContract
from opsgraph.missed_work.contracts import WorkOutcomeRequest

ROOT = Path(__file__).parent


class DisposableDatabase(dict):
    def __repr__(self):
        return "<disposable loopback database; connection details redacted>"


def aware_or_null(value):  # noqa: ANN001
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else None


@pytest.fixture(scope="module")
def postgres():
    owner_dsn = os.environ.get("OPSGRAPH_MISSED_WORK_OWNER_DSN")
    if not owner_dsn:
        pytest.skip("Requires explicitly supplied loopback disposable PostgreSQL owner DSN")
    if urlparse(owner_dsn).hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Fixture provisioning is restricted to a loopback test database")
    schema, role = "mw_" + secrets.token_hex(6), "mw_reader_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    owner = psycopg.connect(owner_dsn, autocommit=True)
    try:
        owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        owner.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        owner.execute(
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                sql.Identifier(role)
            )
        )
        owner.execute(
            sql.SQL(
                "CREATE TABLE {}.export_requests (promise_id text, tenant_id text, "
                "customer_id text, accepted_at timestamptz, due_at timestamptz)"
            ).format(sql.Identifier(schema))
        )
        for table in ("export_outcomes", "export_attempts", "export_cancellations"):
            owner.execute(
                sql.SQL(
                    "CREATE TABLE {}.{} (event_id text, promise_id text, tenant_id text, "
                    "occurred_at timestamptz, status text)"
                ).format(sql.Identifier(schema), sql.Identifier(table))
            )
        owner.execute(
            sql.SQL(
                "CREATE TABLE {}.export_coverage (tenant_id text, complete_through timestamptz)"
            ).format(sql.Identifier(schema))
        )
        owner.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        owner.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        dsn = make_conninfo(owner_dsn, user=role, password=password, sslmode="disable")
        definition = json.loads(ROOT.joinpath("export-contract.json").read_text())
        tables = []
        for key in ("promises", "outcomes", "attempts", "cancellations", "coverage"):
            definition[key]["relation"] = definition[key]["relation"].replace(
                "public.", schema + "."
            )
            tables.append(definition[key]["relation"])
        snapshot = (
            PsycopgReadOnlyExecutor(dsn)
            .discover_snapshot(allowed_schemas=(schema,), allowed_tables=tuple(tables))
            .scoped(tuple(tables))
        )
        definition["schema_fingerprint"] = snapshot.fingerprint
        contract = WorkOutcomeContract.model_validate(definition)
        bounds = Obligation(allowed_schemas=(schema,), allowed_tables=tuple(tables))

        def load(data):  # noqa: ANN001
            for key, table in (
                ("promises", "export_requests"),
                ("outcomes", "export_outcomes"),
                ("attempts", "export_attempts"),
                ("cancellations", "export_cancellations"),
            ):
                owner.execute(
                    sql.SQL("TRUNCATE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
                )
                columns = (
                    ("promise_id", "tenant_id", "customer_id", "accepted_at", "due_at")
                    if key == "promises"
                    else ("event_id", "promise_id", "tenant_id", "occurred_at", "status")
                )
                values = [
                    tuple(
                        aware_or_null(row.get(c)) if c.endswith("_at") else row.get(c)
                        for c in columns
                    )
                    for row in data.get(key, [])
                ]
                if values:
                    with owner.cursor() as cursor:
                        cursor.executemany(
                            sql.SQL("INSERT INTO {}.{} VALUES (%s,%s,%s,%s,%s)").format(
                                sql.Identifier(schema), sql.Identifier(table)
                            ),
                            values,
                        )
            owner.execute(sql.SQL("TRUNCATE {}.export_coverage").format(sql.Identifier(schema)))
            if data.get("coverage") is not None:
                owner.execute(
                    sql.SQL("INSERT INTO {}.export_coverage VALUES (%s,%s)").format(
                        sql.Identifier(schema)
                    ),
                    ("tenant-a", aware_or_null(data["coverage"].get("complete_through"))),
                )

        fixture = json.loads(ROOT.joinpath("export-cases.json").read_text())
        scope = WorkOutcomeRequest(
            contract_id=contract.id, version=contract.version, **fixture["scope"]
        )
        yield DisposableDatabase(
            owner=owner,
            dsn=dsn,
            schema=schema,
            contract=contract,
            snapshot=snapshot,
            bounds=bounds,
            scope=scope,
            load=load,
        )
    finally:
        owner.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        owner.close()
