"""Optional setup with an isolated, resumable PostgreSQL practice environment."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opsgraph.setup import (
    SetupError,
    default_workspace_directory,
    ensure_private_directory,
    read_private_config,
    run_setup,
    write_private_config,
)
from opsgraph.terminal_ui import TerminalScreenError, TerminalUI

PRACTICE_TABLES = ("public.practice_orders", "public.practice_payments")
PRACTICE_QUESTION = (
    "Count payments by processor, status and error_code. Which processor has failed "
    "payments, how many are there, and what error is recorded? Cite the evidence."
)
POSTGRES_IMAGE = "postgres:17.11"


class NoCodeError(RuntimeError):
    """A fixed recovery message that does not contain subprocess output or secrets."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NoCodeError("Practice checks cannot redirect to another service.")


def child_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPSGRAPH_", "PG", "LANGCHAIN_", "LANGSMITH_"))
        and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DOCKER_HOST", "DOCKER_CONTEXT"}
    }
    environment["LC_ALL"] = "C"
    return environment


def command(args: list[str], *, timeout: float = 120, data: str | None = None) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            args,
            input=data,
            text=True,
            capture_output=True,
            check=False,
            env=child_environment(),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NoCodeError(
            "The practice service could not finish. Rerun No-code setup to retry."
        ) from None
    if result.returncode:
        raise NoCodeError(
            "The practice service stopped. Check its installation and free disk space, then retry."
        )
    return result.stdout


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def check_local_docker(binary: str) -> None:
    endpoint = command(
        [binary, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], timeout=15
    ).strip()
    if not endpoint.startswith(("unix://", "npipe://")):
        raise NoCodeError(
            "Practice setup requires local Docker Desktop. Remote Docker is not used."
        )


@contextmanager
def setup_lock(workspace: Path):
    """The operating system releases the lock on cancellation or process death."""
    path = workspace / ".no-code-lock"
    if path.is_symlink():
        raise NoCodeError("The setup lock must be a regular private file.")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))
        ):
            raise NoCodeError("The setup lock must be a private file owned by you.")
        if os.name == "nt":
            import msvcrt

            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise NoCodeError(
                    "Another No-code setup is using this workspace. Let it finish first."
                ) from None
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise NoCodeError(
                    "Another No-code setup is using this workspace. Let it finish first."
                ) from None
        yield


def postgres_bin() -> Path | None:
    executable = "initdb.exe" if os.name == "nt" else "initdb"
    found = shutil.which(executable)
    candidates = [Path(found).parent] if found else []
    if sys.platform == "darwin":
        for base in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
            candidates.extend(sorted(base.glob("postgresql@*/bin"), reverse=True))
    elif os.name == "nt":
        candidates.extend(sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"), reverse=True))
    else:
        candidates.extend(sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True))
    for candidate in candidates:
        suffix = ".exe" if os.name == "nt" else ""
        if all(
            (candidate / (name + suffix)).is_file()
            for name in ("initdb", "pg_ctl", "postgres", "pg_controldata")
        ):
            version = command([str(candidate / ("postgres" + suffix)), "--version"])
            match = re.search(r"PostgreSQL\) (\d+)", version)
            if match and 15 <= int(match[1]) <= 18:
                return candidate
    return None


def fixture_sql(password: str) -> sql.Composed:
    if not re.fullmatch(r"[a-f0-9]{64}", password):
        raise NoCodeError("Practice credentials are invalid. Choose a new practice workspace.")
    return sql.SQL("""
BEGIN;
CREATE ROLE opsgraph_practice_reader LOGIN PASSWORD {password}
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE opsgraph_practice_reader SET default_transaction_read_only = on;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE postgres TO opsgraph_practice_reader;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO opsgraph_practice_reader;
CREATE TABLE public.practice_orders (
  id integer PRIMARY KEY, tenant_id text NOT NULL, created_at timestamptz NOT NULL,
  amount_cents integer NOT NULL CHECK (amount_cents > 0), status text NOT NULL
);
CREATE TABLE public.practice_payments (
  id integer PRIMARY KEY, order_id integer NOT NULL REFERENCES public.practice_orders(id),
  processor text NOT NULL, status text NOT NULL, error_code text
);
INSERT INTO public.practice_orders
SELECT n, 'practice-shop', '2026-01-01 12:00:00+00'::timestamptz + n * interval '1 minute',
  1000 + n, CASE WHEN n <= 50 THEN 'payment_failed' ELSE 'paid' END
FROM generate_series(1, 1000) AS n;
INSERT INTO public.practice_payments
SELECT n, n, CASE WHEN n <= 500 THEN 'fastpay' ELSE 'steadypay' END,
  CASE WHEN n <= 50 THEN 'failed' ELSE 'succeeded' END,
  CASE WHEN n <= 50 THEN 'gateway_timeout' ELSE NULL END
FROM generate_series(1, 1000) AS n;
GRANT SELECT ON public.practice_orders, public.practice_payments TO opsgraph_practice_reader;
COMMIT;
""").format(password=sql.Literal(password))


class PracticeDatabase:
    def __init__(self, workspace: Path):
        self.workspace = ensure_private_directory(workspace)
        self.directory = ensure_private_directory(self.workspace / ".practice")
        self.manifest = self.directory / "service.env"
        self.values = read_private_config(self.manifest)

    def prepare(self) -> None:
        if self.values:
            self.validate_manifest()
            return
        if (self.workspace / ".env").exists() or any(self.directory.iterdir()):
            raise NoCodeError(
                "This workspace already contains other files. Choose a new practice workspace."
            )
        binary = postgres_bin()
        if binary:
            backend = "native"
        else:
            docker = shutil.which("docker")
            if not docker:
                raise NoCodeError(
                    "Practice data needs PostgreSQL 15-18 or Docker Desktop. Install either, "
                    "then rerun this command. PostgreSQL: https://www.postgresql.org/download/ "
                    "Docker: https://www.docker.com/products/docker-desktop/ "
                    "You can also choose Connect my database without installing either."
                )
            check_local_docker(docker)
            try:
                command([docker, "info", "--format", "{{.ServerVersion}}"], timeout=15)
            except NoCodeError:
                raise NoCodeError(
                    "Open Docker Desktop and wait until it is running, then retry."
                ) from None
            backend = "docker"
        self.values = {
            "VERSION": "1",
            "BACKEND": backend,
            "BINARY": str(binary or docker),
            "ID": secrets.token_hex(12),
            "PORT": str(free_port()),
            "ADMIN_PASSWORD": secrets.token_hex(32),
            "READER_PASSWORD": secrets.token_hex(32),
        }
        write_private_config(self.manifest, self.values)

    def validate_manifest(self) -> None:
        values = self.values
        if (
            set(values).difference({"NATIVE_SYSTEM_ID"})
            != {"VERSION", "BACKEND", "BINARY", "ID", "PORT", "ADMIN_PASSWORD", "READER_PASSWORD"}
            or values["VERSION"] != "1"
            or values["BACKEND"] not in {"native", "docker"}
            or not re.fullmatch(r"[a-f0-9]{24}", values.get("ID") or "")
            or any(
                not re.fullmatch(r"[a-f0-9]{64}", values.get(key) or "")
                for key in ("ADMIN_PASSWORD", "READER_PASSWORD")
            )
            or not (values.get("PORT") or "").isdigit()
            or not 1024 <= int(values["PORT"]) <= 65535
            or not Path(values.get("BINARY") or "").is_absolute()
            or (
                "NATIVE_SYSTEM_ID" in values
                and not re.fullmatch(r"[0-9]{1,24}", values["NATIVE_SYSTEM_ID"] or "")
            )
        ):
            raise NoCodeError(
                "Practice configuration is invalid. Preserve it and choose a new workspace."
            )

    def dsn(self, *, admin: bool = False) -> str:
        return make_conninfo(
            host="127.0.0.1",
            port=self.values["PORT"],
            dbname="postgres",
            user="postgres" if admin else "opsgraph_practice_reader",
            password=self.values["ADMIN_PASSWORD" if admin else "READER_PASSWORD"],
            sslmode="disable",
            passfile=os.devnull,
            connect_timeout="2",
        )

    def native_command(self, name: str) -> str:
        return str(Path(self.values["BINARY"]) / (name + (".exe" if os.name == "nt" else "")))

    def native_identity(self) -> str:
        result = command(
            [self.native_command("pg_controldata"), "-D", str(self.directory / "data")]
        )
        match = re.search(r"Database system identifier:\s+([0-9]+)", result)
        if not match:
            raise NoCodeError(
                "Practice database identity could not be verified. No data was changed."
            )
        return match[1]

    def verify_native_identity(self) -> None:
        ensure_private_directory(self.directory / "data")
        if self.native_identity() != self.values.get("NATIVE_SYSTEM_ID"):
            raise NoCodeError(
                "This database does not belong to practice setup. No service was changed."
            )

    def start(self) -> None:
        self.prepare()
        self.validate_manifest()
        if self.values["BACKEND"] == "native":
            data = self.directory / "data"
            if data.exists() or data.is_symlink():
                ensure_private_directory(data)
            if not (data / "PG_VERSION").exists():
                if data.exists():
                    raise NoCodeError(
                        "Practice database initialization was interrupted. Preserve this "
                        "workspace and choose a new one."
                    )
                password_file = self.directory / "init-password"
                descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(descriptor, "w") as handle:
                        handle.write(self.values["ADMIN_PASSWORD"])
                    command(
                        [
                            self.native_command("initdb"),
                            "-D",
                            str(data),
                            "-U",
                            "postgres",
                            "--encoding=UTF8",
                            "--no-locale",
                            "--auth=scram-sha-256",
                            "--pwfile",
                            str(password_file),
                        ]
                    )
                finally:
                    password_file.unlink(missing_ok=True)
                self.values["NATIVE_SYSTEM_ID"] = self.native_identity()
                write_private_config(self.manifest, self.values)
            self.verify_native_identity()
            if not self._running():
                options = f"-h 127.0.0.1 -p {self.values['PORT']} -c unix_socket_directories=''"
                command(
                    [
                        self.native_command("pg_ctl"),
                        "-D",
                        str(data),
                        "-l",
                        str(self.directory / "server.log"),
                        "-w",
                        "-t",
                        "30",
                        "-o",
                        options,
                        "start",
                    ]
                )
        else:
            self.start_docker()
        deadline = time.monotonic() + 30
        while not self._running():
            if time.monotonic() >= deadline:
                raise NoCodeError(
                    "Practice database is not ready. Check port availability and rerun setup."
                )
            time.sleep(0.2)
        with psycopg.connect(self.dsn(admin=True), autocommit=True) as connection:
            exists = connection.execute("SELECT to_regclass('public.practice_orders')").fetchone()[
                0
            ]
            if not exists:
                connection.execute(fixture_sql(self.values["READER_PASSWORD"]))
        with psycopg.connect(self.dsn()) as connection:
            result = connection.execute("SELECT count(*) FROM public.practice_payments").fetchone()
            if result != (1000,):
                raise NoCodeError(
                    "Practice data changed. Preserve this workspace and choose a new one."
                )

    def _running(self) -> bool:
        try:
            with psycopg.connect(self.dsn(admin=True)) as connection:
                if self.values["BACKEND"] == "native":
                    actual = Path(connection.execute("SHOW data_directory").fetchone()[0]).resolve()
                    if actual != (self.directory / "data").resolve():
                        raise NoCodeError(
                            "The practice port belongs to another database. No service was changed."
                        )
                return True
        except psycopg.Error:
            return False

    def verify_docker_identity(self, entry: dict) -> None:
        name = "opsgraph-practice-" + self.values["ID"]
        ports = entry.get("HostConfig", {}).get("PortBindings", {}).get("5432/tcp")
        mount = any(
            item.get("Type") == "volume"
            and item.get("Name") == name
            and item.get("Destination") == "/var/lib/postgresql/data"
            for item in entry.get("Mounts", [])
        )
        if (
            entry.get("Config", {}).get("Labels", {}).get("opsgraph.practice") != self.values["ID"]
            or entry.get("Config", {}).get("Image") != POSTGRES_IMAGE
            or ports != [{"HostIp": "127.0.0.1", "HostPort": self.values["PORT"]}]
            or not mount
        ):
            raise NoCodeError("Container ownership or isolation changed. No container was changed.")

    def start_docker(self) -> None:
        docker = self.values["BINARY"]
        check_local_docker(docker)
        name = "opsgraph-practice-" + self.values["ID"]
        inspect = subprocess.run(  # noqa: S603
            [docker, "container", "inspect", name],
            capture_output=True,
            check=False,
            env=child_environment(),
            timeout=15,
        )
        if inspect.returncode == 0:
            entry = json.loads(inspect.stdout)[0]
            self.verify_docker_identity(entry)
            command([docker, "start", name])
            return
        env_file = self.directory / "container.env"
        descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("POSTGRES_PASSWORD=" + self.values["ADMIN_PASSWORD"] + "\n")
        try:
            command(
                [
                    docker,
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--label",
                    "opsgraph.practice=" + self.values["ID"],
                    "--env-file",
                    str(env_file),
                    "-p",
                    f"127.0.0.1:{self.values['PORT']}:5432",
                    "--mount",
                    f"type=volume,source={name},target=/var/lib/postgresql/data",
                    POSTGRES_IMAGE,
                ],
                timeout=600,
            )
        finally:
            env_file.unlink(missing_ok=True)

    def stop(self) -> None:
        self.validate_manifest()
        if self.values["BACKEND"] == "native":
            if (self.directory / "data").exists() or (self.directory / "data").is_symlink():
                self.verify_native_identity()
            if self._running():
                command(
                    [
                        self.native_command("pg_ctl"),
                        "-D",
                        str(self.directory / "data"),
                        "-w",
                        "-t",
                        "30",
                        "-m",
                        "fast",
                        "stop",
                    ]
                )
        else:
            check_local_docker(self.values["BINARY"])
            name = "opsgraph-practice-" + self.values["ID"]
            entry = json.loads(command([self.values["BINARY"], "container", "inspect", name]))[0]
            self.verify_docker_identity(entry)
            command([self.values["BINARY"], "stop", name])


def connect_practice(origin: str, values: dict[str, str | None], *, external: bool) -> None:
    """Use authenticated, audited APIs after explicit terminal scope approval."""
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{4,5}", origin):
        raise NoCodeError("Practice checks require this launcher's local address.")
    ui = TerminalUI()
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(path: str, body: dict | None = None):
        payload = None if body is None else json.dumps(body).encode()
        req = Request(  # noqa: S310
            origin + path,
            data=payload,
            headers={
                "X-OpsGraph-Key": values["OPSGRAPH_API_KEY"],
                "Content-Type": "application/json",
                "Origin": origin,
            },
        )
        with opener.open(req, timeout=15) as response:  # noqa: S310
            return json.load(response)

    sources = request("/api/sources")
    source = next((item for item in sources if item["id"] == "practice-data"), None)
    if source is None:
        request(
            "/api/sources",
            {
                "id": "practice-data",
                "name": "Practice data (invented records)",
                "hosting_profile": "local",
                "secret_ref": "OPSGRAPH_SOURCE_DSN",
                "allowed_schemas": ["public"],
                "allowed_tables": list(PRACTICE_TABLES),
                "allow_external_egress": external,
            },
        )
    elif (
        set(source.get("allowed_tables", [])) != set(PRACTICE_TABLES)
        or source.get("secret_ref") != "OPSGRAPH_SOURCE_DSN"
        or source.get("allow_external_egress") != external
    ):
        ui.write("Your saved source choices were preserved. Review Practice data in Sources.")
        return
    with ui.progress("Checking practice tables and read-only access"):
        request("/api/sources/practice-data/inspect", {})
        request(
            "/api/sources/practice-data/readiness",
            {
                "table": PRACTICE_TABLES[0],
                "confirm_bounded_read": True,
            },
        )
    ui.write("Practice source checked. Settings: test your model before your first investigation.")


def run_no_code(
    directory: Path | None = None,
    *,
    port: int | None = None,
    browser: bool = True,
    configure: bool = False,
    stop: bool = False,
    input_fn: Callable[[str], str] | None = None,
) -> int:
    from opsgraph.launcher import bind_loopback, launch

    ui = TerminalUI()
    ask = input_fn or ui.ask
    database = None
    on_ready = None
    try:
        ui.heading("No-code setup")
        ui.write(
            "No SQL or configuration files to write. Python and a model service are still needed."
        )
        if stop:
            workspace = directory or default_workspace_directory().with_name("OpsGraph-Practice")
            database = PracticeDatabase(workspace)
            with setup_lock(database.workspace):
                database.stop()
            ui.write("Practice database stopped. Data, settings and history are preserved.")
            return 0
        ui.menu(
            (("practice", "Try with practice data"), ("connect", "Connect my database")), "practice"
        )
        while True:
            choice = ask("Choose 1 or 2. Enter uses practice data").strip().lower()
            if choice in {"", "1", "practice", "2", "connect"}:
                break
            ui.write("Type 1 for practice data or 2 for your own database.")
        practice = choice in {"", "1", "practice"}
        workspace = ensure_private_directory(
            directory
            or default_workspace_directory().with_name(
                "OpsGraph-Practice" if practice else "OpsGraph-NoCode"
            )
        )
        selected_port = free_port() if port is None else port
        with bind_loopback(selected_port):
            pass
        with setup_lock(workspace):
            try:
                if practice:
                    ui.write(
                        "This creates a separate database with invented orders and payments. "
                        "Your databases stay untouched."
                    )
                    ui.write(
                        "It uses an installed PostgreSQL runtime, or Docker Desktop. Docker may "
                        "download the practice database image."
                    )
                    if ask(
                        "Prepare or resume this practice database? [Y/n]"
                    ).strip().lower() not in {
                        "",
                        "y",
                        "yes",
                    }:
                        ui.write("Cancelled. No database was started.")
                        return 1
                    database = PracticeDatabase(workspace)
                    with ui.progress("Preparing your separate practice database"):
                        database.start()
                    ui.write(
                        "Practice data ready: 1,000 orders and 1,000 payments. Only a read-only "
                        "login is passed to OpsGraph."
                    )
                if configure or not (workspace / ".env").exists():
                    result = run_setup(
                        workspace,
                        flow="quick",
                        prepared_database=database.dsn() if database else None,
                    )
                    if result:
                        return result
                if database:
                    values = read_private_config(workspace / ".env")
                    if values.get("OPSGRAPH_SOURCE_DSN") != database.dsn():
                        from opsgraph.setup import normalize_guided_dsn

                        if values.get("OPSGRAPH_SOURCE_DSN") != normalize_guided_dsn(
                            database.dsn()
                        ):
                            raise NoCodeError(
                                "Saved connection differs from this practice database. Choose "
                                "another workspace."
                            )
                    ui.heading("Your first investigation")
                    ui.write("Only these two practice tables will be available:")
                    ui.write(", ".join(PRACTICE_TABLES))
                    ui.write(
                        "Approve the practice tables below to have setup check them for you. "
                        "You can also leave this step for Sources in the browser."
                    )
                    ui.write("Settings: test your model connection. Then ask:")
                    ui.write(PRACTICE_QUESTION)
                    ui.write(
                        "Check your answer: fastpay has 50 failed payments with "
                        "gateway_timeout. This practice result does not measure general model "
                        "accuracy."
                    )
                    external = values.get("OPSGRAPH_EGRESS_ENABLED") == "true"
                    if external:
                        ui.write(
                            "Your hosted model receives selected practice records during "
                            "investigations."
                        )
                    approved = ask(
                        "Approve these practice tables, a small test read, and "
                        + (
                            "sending their invented records to your hosted model? [y/N]"
                            if external
                            else "local model processing? [y/N]"
                        )
                    ).strip().lower() in {"y", "yes"}
                    if approved:

                        def on_ready(origin):
                            connect_practice(origin, values, external=external)
                    else:
                        ui.write("No source approval granted. You can finish in Sources later.")
                ui.flush()
                return launch(
                    workspace,
                    selected_port,
                    configure=False,
                    browser=browser,
                    flow="quick",
                    on_ready=on_ready,
                )
            finally:
                if database and database.values:
                    database.stop()
                    ui.write(
                        "Practice database stopped. Rerun No-code setup to resume your "
                        "saved workspace."
                    )
    except (KeyboardInterrupt, EOFError):
        ui.write("Cancelled. Saved settings and practice data are preserved.")
        return 1
    except TerminalScreenError as error:
        ui.write(str(error))
        return 1
    except (NoCodeError, SetupError) as error:
        ui.write(str(error))
        return 1
    except (OSError, ValueError, psycopg.Error, subprocess.SubprocessError):
        ui.write(
            "Setup could not finish safely. Check permissions, free disk space and local "
            "service availability. Saved data is preserved."
        )
        return 1
    finally:
        ui.flush()
