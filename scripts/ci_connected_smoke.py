#!/usr/bin/env python3
"""Connected control-path smoke for CI.

This check uses a real PostgreSQL server and a local OpenAI-compatible protocol
fixture. The fixture proves request/response compatibility and application
controls only; it does not evaluate model quality.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]
TABLE = "public.opsgraph_ci_records"
SOURCE_ID = "ci-source"
SOURCE_DSN_ENV = "OPSGRAPH_CI_SOURCE_DSN"
QUESTION = (
    "List every record with its id, status, and duration_ms ordered by id. "
    "Treat duration_ms as a literal recorded value. Cite the captured evidence."
)
EXPECTED_ROWS = [[1, "succeeded", 120], [2, "failed", 2400], [3, "succeeded", 180]]


class SmokeFailure(RuntimeError):
    """A credential-safe connected-smoke assertion failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def response_for_schema(schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    properties = set(schema.get("properties", {}))
    if properties == {"ok"}:
        return "probe", {"ok": True}
    if {"rationale", "clarification", "queries"} <= properties:
        return (
            "plan",
            {
                "rationale": "Read the three requested columns directly without inference.",
                "clarification": None,
                "queries": [
                    {
                        "purpose": "Capture the approved records in identifier order.",
                        "sql": (
                            "SELECT id, status, duration_ms "
                            "FROM public.opsgraph_ci_records ORDER BY id"
                        ),
                    }
                ],
            },
        )
    if {"summary", "findings", "limitations"} <= properties:
        evidence_ids: list[str] = []
        for definition in schema.get("$defs", {}).values():
            candidate = (
                definition.get("properties", {})
                .get("evidence_ids", {})
                .get("items", {})
                .get("enum", [])
            )
            if candidate:
                evidence_ids = [str(value) for value in candidate]
                break
        require(bool(evidence_ids), "protocol fixture received no evidence citation identity")
        return (
            "answer",
            {
                "summary": "The bounded capture contains three records.",
                "findings": [
                    {
                        "claim": (
                            "Captured IDs 1, 2, and 3 have statuses succeeded, failed, "
                            "and succeeded, respectively."
                        ),
                        "classification": "supported",
                        "evidence_ids": [evidence_ids[0]],
                    }
                ],
                "limitations": [
                    "This bounded capture does not establish causes or source completeness."
                ],
            },
        )
    raise SmokeFailure("protocol fixture received an unsupported response schema")


class ProtocolFixture(AbstractContextManager["ProtocolFixture"]):
    """Minimal loopback Chat Completions fixture with no request logging."""

    def __init__(self, provider_key: str) -> None:
        self._authorization = f"Bearer {provider_key}"
        self.calls: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ProtocolFixture:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "OpsGraphCIProtocolFixture"

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != fixture._authorization:
                    body = b'{"error":{"message":"provider authentication required"}}'
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    require(0 < length <= 1_000_000, "protocol request size is invalid")
                    request = json.loads(self.rfile.read(length))
                    schema = request["response_format"]["json_schema"]["schema"]
                    call, output = response_for_schema(schema)
                    fixture.calls.append(call)
                    payload = {
                        "id": f"chatcmpl-opsgraph-ci-{len(fixture.calls)}",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "opsgraph-ci-protocol-fixture",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(output, separators=(",", ":")),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                    body = json.dumps(payload, separators=(",", ":")).encode()
                except Exception:
                    body = b'{"error":{"message":"invalid structured request"}}'
                    self.send_response(422)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="opsgraph-ci-protocol-fixture",
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def endpoint(self) -> str:
        require(self._server is not None, "protocol fixture is not running")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        del exc_type, exc, traceback
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class AppProcess(AbstractContextManager["AppProcess"]):
    def __init__(self, environment: dict[str, str]) -> None:
        self.port = loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._environment = environment
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> AppProcess:
        self._process = subprocess.Popen(  # noqa: S603 - fixed interpreter and module
            [
                sys.executable,
                "-m",
                "uvicorn",
                "opsgraph.api.app:app",
                "--app-dir",
                str(ROOT / "src"),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=self._environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        with httpx.Client(base_url=self.base_url, timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise SmokeFailure("application process exited during startup; output withheld")
                try:
                    response = client.get("/api/health")
                    if response.status_code == 200 and response.json().get("ok") is True:
                        return self
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.2)
        raise SmokeFailure("application process did not become live within 20 seconds")

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        del exc_type, exc, traceback
        if self._process is None or self._process.poll() is not None:
            return
        self._process.send_signal(signal.SIGTERM)
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)


class API:
    def __init__(self, app: AppProcess, workspace_key: str) -> None:
        self.client = httpx.Client(
            base_url=app.base_url,
            headers={"X-OpsGraph-Key": workspace_key},
            timeout=15,
            trust_env=False,
        )
        self.public_values: list[Any] = []

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        phase: str,
        expected: int = 200,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = self.client.request(method, path, json=body, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {"non_json_response": True}
        self.public_values.append(payload)
        if response.status_code != expected:
            raise SmokeFailure(f"{phase} returned HTTP {response.status_code}; body withheld")
        return payload


def owner_dsn() -> str:
    value = os.getenv("OPSGRAPH_CI_POSTGRES_OWNER_DSN", "")
    require(bool(value), "OPSGRAPH_CI_POSTGRES_OWNER_DSN is required")
    try:
        parsed = conninfo_to_dict(value)
    except Exception:
        raise SmokeFailure(
            "PostgreSQL owner connection settings are invalid; value withheld"
        ) from None
    host = str(parsed.get("host", "")).strip("[]").casefold()
    database = str(parsed.get("dbname", ""))
    require(host in {"127.0.0.1", "localhost", "::1"}, "CI PostgreSQL must be loopback-only")
    require(database.startswith("opsgraph_ci"), "CI PostgreSQL requires an opsgraph_ci* database")
    return value


def provision_database(dsn: str, role_name: str, role_password: str) -> str:
    created_resources = False
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
            database = connection.info.dbname
            existing = connection.execute("SELECT pg_catalog.to_regclass(%s)", (TABLE,)).fetchone()
            require(
                existing == (None,), "CI source relation already exists; refusing to replace it"
            )
            role_exists = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
                (role_name,),
            ).fetchone()
            require(
                role_exists == (False,), "CI source login already exists; refusing to replace it"
            )
            connection.execute(
                "CREATE TABLE public.opsgraph_ci_records "
                "(id integer PRIMARY KEY, status text NOT NULL, duration_ms integer NOT NULL)"
            )
            created_resources = True
            connection.execute(
                "INSERT INTO public.opsgraph_ci_records (id, status, duration_ms) VALUES "
                "(1, 'succeeded', 120), (2, 'failed', 2400), (3, 'succeeded', 180)"
            )
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5"
                ).format(sql.Identifier(role_name), sql.Literal(role_password))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                    sql.Identifier(role_name)
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role_name)
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role_name))
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                    sql.Identifier(*TABLE.split(".")), sql.Identifier(role_name)
                )
            )
            connection.execute("REVOKE ALL ON TABLE public.opsgraph_ci_records FROM PUBLIC")
            privileges = connection.execute(
                "SELECT pg_catalog.has_table_privilege(%s, %s, 'SELECT'), "
                "pg_catalog.has_table_privilege(%s, %s, 'INSERT,UPDATE,DELETE,TRUNCATE')",
                (role_name, TABLE, role_name, TABLE),
            ).fetchone()
            require(privileges == (True, False), "CI database role privilege boundary is invalid")
            parsed = conninfo_to_dict(dsn)
            host = parsed.get("host", "127.0.0.1")
            port = parsed.get("port", "5432")
            return (
                f"postgresql://{quote(role_name, safe='')}:{quote(role_password, safe='')}@"
                f"{host}:{port}/{quote(database, safe='')}"
            )
    except SmokeFailure:
        if created_resources:
            cleanup_database(dsn, role_name)
        raise
    except Exception:
        if created_resources:
            cleanup_database(dsn, role_name)
        raise SmokeFailure("CI PostgreSQL provisioning failed; driver detail withheld") from None


def verify_database_write_denial(reader_dsn: str, owner_connection: str) -> None:
    denied = False
    try:
        with psycopg.connect(reader_dsn, connect_timeout=5) as connection:
            connection.execute(
                "INSERT INTO public.opsgraph_ci_records (id, status, duration_ms) "
                "VALUES (4, 'unexpected', 1)"
            )
    except psycopg.Error:
        denied = True
    require(denied, "dedicated source login unexpectedly accepted a write")
    try:
        with psycopg.connect(owner_connection, connect_timeout=5) as connection:
            count = connection.execute("SELECT count(*) FROM public.opsgraph_ci_records").fetchone()
    except psycopg.Error:
        raise SmokeFailure("CI PostgreSQL verification failed; driver detail withheld") from None
    require(count == (3,), "write-denial check changed the source table")


def cleanup_database(dsn: str, role_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
            connection.execute("DROP TABLE IF EXISTS public.opsgraph_ci_records CASCADE")
            connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name)))
    except Exception:
        # The GitHub service database is ephemeral. Cleanup is best effort and never
        # hides the actual smoke result.
        return


def wait_for_run(api: API, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        run = api.request("GET", f"/api/runs/{run_id}", phase="investigation status")
        if run["status"] in {"completed", "failed", "blocked", "cancelled"}:
            require(run["status"] == "completed", "investigation did not complete; detail withheld")
            return run
        time.sleep(0.2)
    raise SmokeFailure("investigation did not reach a terminal state within 45 seconds")


def assert_no_public_secret(values: list[Any], secrets_to_check: tuple[str, ...]) -> None:
    serialized = json.dumps(values, sort_keys=True)
    require(
        not any(secret and secret in serialized for secret in secrets_to_check),
        "a credential appeared in an API response; value withheld",
    )


def run_smoke() -> dict[str, Any]:
    owner_connection = owner_dsn()
    owner_password = str(conninfo_to_dict(owner_connection).get("password", ""))
    suffix = secrets.token_hex(6)
    role_name = f"opsgraph_ci_reader_{suffix}"
    role_password = secrets.token_urlsafe(24)
    workspace_key = secrets.token_urlsafe(32)
    provider_key = secrets.token_urlsafe(24)
    public_values: list[Any] = []

    with (
        TemporaryDirectory(prefix="opsgraph-connected-ci-") as state_dir,
        ProtocolFixture(provider_key) as fixture,
    ):
        reader_dsn = provision_database(owner_connection, role_name, role_password)
        state_path = Path(state_dir) / "state.db"
        environment = dict(os.environ)
        environment.pop("OPSGRAPH_CI_POSTGRES_OWNER_DSN", None)
        environment.update(
            {
                "LANGSMITH_TRACING": "false",
                "OPSGRAPH_API_KEY": workspace_key,
                "OPSGRAPH_WORKSPACE_ID": "connected-ci",
                "OPSGRAPH_MODE": "connected",
                "OPSGRAPH_EGRESS_ENABLED": "false",
                "OPSGRAPH_MODEL_PROVIDER": "openai_compatible",
                "OPSGRAPH_LOCAL_MODEL": "opsgraph-ci-protocol-fixture",
                "OPSGRAPH_LOCAL_MODEL_URL": fixture.endpoint,
                "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "standard",
                "OPSGRAPH_PROVIDER_TIMEOUT_SECONDS": "10",
                "OPSGRAPH_OPENAI_API_KEY": "",
                "OPSGRAPH_STATE_PATH": str(state_path),
                "OPSGRAPH_POSTGRES_SECRET_REF": SOURCE_DSN_ENV,
                "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS": SOURCE_DSN_ENV,
                "OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS": "public",
                SOURCE_DSN_ENV: reader_dsn,
            }
        )
        environment.pop("OPSGRAPH_LOCAL_REASONING_EFFORT", None)
        try:
            verify_database_write_denial(reader_dsn, owner_connection)

            # Save through the authenticated API; do not probe until a fresh process
            # has reloaded the private provider configuration.
            with AppProcess(environment) as app:
                api = API(app, workspace_key)
                try:
                    saved = api.request(
                        "PUT",
                        "/api/providers/configuration",
                        phase="provider save",
                        body={
                            "provider": "custom_openai",
                            "model": "opsgraph-ci-protocol-fixture",
                            "endpoint": fixture.endpoint,
                            "api_key": provider_key,
                            "schema_profile": "standard",
                            "timeout_seconds": 10,
                            "max_output_tokens": 2048,
                            "allow_external_egress": False,
                        },
                        headers={
                            "Origin": app.base_url,
                            "Sec-Fetch-Site": "same-origin",
                        },
                    )
                    require(saved["api_key_configured"] is True, "provider key was not retained")
                    public_values.extend(api.public_values)
                finally:
                    api.close()

            provider_path = state_path.parent / (state_path.name + ".provider") / "settings.env"
            require(provider_path.is_file(), "private provider configuration was not created")
            require(
                provider_path.stat().st_mode & 0o077 == 0,
                "private provider configuration is accessible outside its owner",
            )

            with AppProcess(environment) as app:
                api = API(app, workspace_key)
                try:
                    configuration = api.request(
                        "GET", "/api/providers/configuration", phase="provider reload"
                    )
                    require(
                        configuration["provider"] == "custom_openai"
                        and configuration["adapter"] == "openai_compatible"
                        and configuration["model"] == "opsgraph-ci-protocol-fixture"
                        and configuration["api_key_configured"] is True,
                        "restarted process did not load the saved provider",
                    )
                    probe = api.request(
                        "POST", "/api/providers/current/test", phase="provider protocol probe"
                    )
                    require(probe["ok"] is True, "provider protocol probe did not pass")

                    source = api.request(
                        "POST",
                        "/api/sources",
                        phase="source save",
                        body={
                            "id": SOURCE_ID,
                            "name": "Connected CI PostgreSQL",
                            "secret_ref": SOURCE_DSN_ENV,
                            "allowed_schemas": ["public"],
                            "allowed_tables": [TABLE],
                            "evidence_bindings": [],
                            "allow_external_egress": False,
                        },
                    )
                    require(
                        source["secret_ref"] == SOURCE_DSN_ENV,
                        "source reference was not saved",
                    )
                    inspection = api.request(
                        "POST", f"/api/sources/{SOURCE_ID}/inspect", phase="source inspection"
                    )
                    require(
                        inspection["status"] == "ready"
                        and inspection["tables"][0]["schema_name"] == "public"
                        and inspection["tables"][0]["table_name"] == "opsgraph_ci_records",
                        "source inspection did not return the approved relation",
                    )
                    readiness = api.request(
                        "POST",
                        f"/api/sources/{SOURCE_ID}/readiness",
                        phase="bounded source readiness",
                        body={"table": TABLE, "confirm_bounded_read": True},
                    )
                    require(
                        readiness["status"] == "ready"
                        and readiness["source_values_returned"] == 0
                        and readiness["table"] == TABLE
                        and readiness["source_revision"].startswith("sha256:")
                        and readiness["policy_revision"].startswith("sha256:"),
                        "bounded source readiness contract did not pass",
                    )

                    denied = api.request(
                        "POST",
                        "/api/query/validate",
                        phase="application write denial",
                        expected=422,
                        body={"sql": "DELETE FROM public.opsgraph_ci_records"},
                    )
                    require("detail" in denied, "write denial did not return a bounded error")

                    accepted = api.request(
                        "POST",
                        "/api/runs",
                        phase="investigation submission",
                        expected=202,
                        body={
                            "source_id": SOURCE_ID,
                            "skill_id": "generic-readonly",
                            "question": QUESTION,
                            "request_id": f"connected-ci-{suffix}",
                        },
                    )
                    completed = wait_for_run(api, accepted["id"])
                    require(len(completed["evidence"]) == 1, "investigation capture count changed")
                    capture = completed["evidence"][0]
                    require(
                        capture["columns"] == ["id", "status", "duration_ms"]
                        and capture["rows"] == EXPECTED_ROWS
                        and capture["referenced_tables"] == [TABLE]
                        and capture["evidence_hash"].startswith("sha256:"),
                        "investigation capture does not match the real PostgreSQL result",
                    )
                    exported = api.request(
                        "GET", f"/api/runs/{completed['id']}/export", phase="investigation export"
                    )
                    require(
                        exported["export_version"] == 1
                        and {
                            key: value for key, value in exported.items() if key != "export_version"
                        }
                        == completed,
                        "investigation export differs from persisted state",
                    )
                    run_id = completed["id"]
                    public_values.extend(api.public_values)
                finally:
                    api.close()

            # Reopen durable history and export from another fresh application process.
            with AppProcess(environment) as app:
                api = API(app, workspace_key)
                try:
                    reopened = api.request(
                        "GET", f"/api/runs/{run_id}", phase="history after restart"
                    )
                    require(
                        reopened == completed, "restarted process changed investigation history"
                    )
                    history = api.request("GET", "/api/runs", phase="history listing")
                    require(
                        any(item["id"] == run_id for item in history), "history omitted the run"
                    )
                    reopened_export = api.request(
                        "GET", f"/api/runs/{run_id}/export", phase="export after restart"
                    )
                    require(
                        {
                            key: value
                            for key, value in reopened_export.items()
                            if key != "export_version"
                        }
                        == completed,
                        "restarted process changed the export",
                    )
                    audit = api.request("GET", "/api/audit", phase="audit verification")
                    require(audit["verification"]["valid"] is True, "audit chain is invalid")
                    public_values.extend(api.public_values)
                finally:
                    api.close()

            secrets_to_check = (
                owner_connection,
                owner_password,
                reader_dsn,
                role_password,
                workspace_key,
                provider_key,
            )
            assert_no_public_secret(public_values, secrets_to_check)
            require(
                role_password.encode() not in state_path.read_bytes(),
                "source password entered state",
            )
            require(
                fixture.calls.count("probe") >= 1
                and fixture.calls.count("plan") >= 1
                and fixture.calls.count("answer") >= 1,
                "OpenAI-compatible protocol fixture did not exercise probe, plan and answer",
            )
            return {
                "ok": True,
                "postgresql": "real loopback service",
                "source_role": "dedicated SELECT-only login",
                "provider": "local OpenAI-compatible protocol fixture",
                "provider_save_restart_probe": True,
                "bounded_readiness": True,
                "investigation_capture_export": True,
                "database_write_denial": True,
                "application_write_sql_denial": True,
                "history_after_restart": True,
                "checked_api_response_credential_leakage": False,
                "source_password_in_state_database": False,
                "model_quality_tested": False,
            }
        finally:
            cleanup_database(owner_connection, role_name)


def main() -> int:
    try:
        result = run_smoke()
    except SmokeFailure as exc:
        print(f"connected control-path smoke failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("connected control-path smoke failed unexpectedly; detail withheld", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
