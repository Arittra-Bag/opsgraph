import concurrent.futures
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import psycopg
import pytest

from opsgraph.brokers.postgres import PsycopgReadOnlyExecutor
from opsgraph.no_code import PracticeDatabase, child_environment, connect_practice, free_port
from opsgraph.setup import write_private_config


@pytest.mark.skipif(
    os.environ.get("OPSGRAPH_TEST_PRACTICE") != "1",
    reason="Opt in to temporary PostgreSQL and loopback API servers",
)
def test_native_practice_regression_and_stress():
    ROOT = Path(__file__).resolve().parents[2]
    original = ROOT / ".venv/pyvenv.cfg"
    before = hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
    with TemporaryDirectory(prefix="opsgraph-no-code-regression-", dir="/private/tmp") as root:
        workspace = Path(root)
        database = PracticeDatabase(workspace)
        server = None
        try:
            database.start()
            password = database.values["READER_PASSWORD"]
            with psycopg.connect(database.dsn()) as connection:
                groups = connection.execute(
                    "SELECT processor,status,error_code,count(*) FROM public.practice_payments "
                    "GROUP BY 1,2,3 ORDER BY 1,2,3"
                ).fetchall()
                assert groups == [
                    ("fastpay", "failed", "gateway_timeout", 50),
                    ("fastpay", "succeeded", None, 450),
                    ("steadypay", "succeeded", None, 500),
                ]
            print("PASS: actual practice data matches known aggregate")
            for query in [
                "INSERT INTO public.practice_orders VALUES (1001,'x',now(),1,'paid')",
                "DELETE FROM public.practice_payments",
                "CREATE TABLE public.forbidden(id int)",
                "CREATE TEMP TABLE forbidden(id int)",
                "CREATE ROLE forbidden",
                "SET ROLE postgres",
            ]:
                try:
                    with psycopg.connect(database.dsn()) as connection:
                        connection.execute("SET default_transaction_read_only=off")
                        connection.commit()
                        connection.execute(query)
                except psycopg.Error:
                    continue
                raise AssertionError("Write/privilege escalation was permitted")
            print(
                "PASS: six write and privilege escalation attempts denied "
                "even after disabling role default"
            )
            result = PsycopgReadOnlyExecutor(
                database.dsn(),
                allowed_schemas=("public",),
                allowed_tables=("public.practice_orders", "public.practice_payments"),
            ).execute_readonly("SELECT count(*) FROM public.practice_payments", timeout_ms=5000)
            assert result.rows == ((1000,),)
            print("PASS: production executor accepts practice reader")

            def read(_):
                with psycopg.connect(database.dsn()) as connection:
                    return connection.execute(
                        "SELECT count(*) FROM public.practice_payments WHERE status='failed'"
                    ).fetchone()[0]

            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                counts = list(pool.map(read, range(256)))
            assert counts == [50] * 256
            print(
                f"PASS: 256 concurrent reads, 16 workers, "
                f"{time.monotonic() - started:.2f}s, zero errors"
            )
            database.stop()
            database.start()
            assert read(0) == 50
            print("PASS: stop/restart preserves exact practice data and credentials")
            port = free_port()
            key = secrets.token_urlsafe(32)
            values = {
                "OPSGRAPH_API_KEY": key,
                "OPSGRAPH_MODE": "connected",
                "OPSGRAPH_WORKSPACE_ID": "practice-test",
                "OPSGRAPH_STATE_PATH": str(workspace / "state.db"),
                "OPSGRAPH_MODEL_PROVIDER": "openai_compatible",
                "OPSGRAPH_MODEL_PRESET": "ollama",
                "OPSGRAPH_LOCAL_MODEL_URL": "http://127.0.0.1:9/v1",
                "OPSGRAPH_LOCAL_MODEL": "not-installed",
                "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "ollama",
                "OPSGRAPH_EGRESS_ENABLED": "false",
                "OPSGRAPH_POSTGRES_SECRET_REF": "OPSGRAPH_SOURCE_DSN",
                "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS": "OPSGRAPH_SOURCE_DSN",
                "OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS": "public",
                "OPSGRAPH_SOURCE_DSN": database.dsn(),
            }
            write_private_config(workspace / ".env", values)
            env = child_environment()
            env.update(values)
            env["PYTHONPATH"] = str(ROOT / "src")
            with (workspace / "api.log").open("w") as log:
                server = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "opsgraph.cli", "serve", "--port", str(port)],
                    cwd=workspace,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
            opener = build_opener(ProxyHandler({}))
            origin = f"http://127.0.0.1:{port}"

            def get(path, auth=True):
                req = Request(  # noqa: S310
                    origin + path, headers={"X-OpsGraph-Key": key} if auth else {}
                )
                with opener.open(req, timeout=10) as response:
                    return json.load(response)

            deadline = time.monotonic() + 30
            while True:
                try:
                    health = get("/api/health", False)
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("API startup failed") from None
                    time.sleep(0.1)
            assert health["investigation_ready"] is False
            connect_practice(origin, values, external=False)
            source = get("/api/sources")[0]
            assert source["readiness"]["status"] == "ready"
            assert not source["allow_external_egress"]
            assert get("/api/health", False)["investigation_ready"] is False
            connect_practice(origin, values, external=False)
            assert len(get("/api/sources")) == 1
            print(
                "PASS: real authenticated API registration, schema inspection, "
                "readiness and idempotent resume"
            )
            try:
                get("/api/sources", False)
            except HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError("Unauthenticated source request accepted")
            assert password not in json.dumps(source)
            audit = get("/api/audit")
            assert key not in json.dumps(audit) and password not in json.dumps(audit)
            print(
                "PASS: API authentication, no credentials in source metadata or audit, "
                "absent model remains unready"
            )
        finally:
            if server:
                server.terminate()
                server.wait(timeout=10)
            if database.values:
                database.stop()
    if before is not None:
        assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    print("PASS: existing Python runtime untouched; temporary API and database stopped")
