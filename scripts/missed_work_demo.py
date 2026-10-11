"""Repository-only synthetic export audit smoke; isolated services, no model needed.

Run: PYTHONPATH=src .venv/bin/python scripts/missed_work_demo.py
Stop with Ctrl+C. The new temporary workspace and practice database are removed.
"""

import argparse
import json
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import psycopg

from opsgraph.launcher import launch
from opsgraph.no_code import NoCodeError, NoRedirect, PracticeDatabase, free_port
from opsgraph.setup import write_private_config

ROOT = Path(__file__).resolve().parents[1]
TABLES = tuple(
    "public.export_" + suffix
    for suffix in ("requests", "outcomes", "attempts", "cancellations", "coverage")
)
SCOPE = {
    "contract_id": "customer-exports",
    "version": 1,
    "tenant_id": "tenant-a",
    "start": "2026-10-11T11:00:00Z",
    "end": "2026-10-11T12:00:00Z",
    "as_of": "2026-10-11T12:00:00Z",
}

# Owner writes only to the newly created disposable practice cluster. Application
# traffic uses its existing SELECT-only role. No connection to an ambient DSN.
SEED = """
BEGIN;
CREATE TABLE public.export_requests (
 promise_id text, tenant_id text, customer_id text, accepted_at timestamptz, due_at timestamptz
);
CREATE TABLE public.export_outcomes (
 event_id text, promise_id text, tenant_id text, occurred_at timestamptz, status text
);
CREATE TABLE public.export_attempts (LIKE public.export_outcomes);
CREATE TABLE public.export_cancellations (LIKE public.export_outcomes);
CREATE TABLE public.export_coverage (tenant_id text, complete_through timestamptz);
INSERT INTO public.export_requests
SELECT state || '-' || n, 'tenant-a', 'customer-' || n % 8,
 CASE WHEN state='pending_within_deadline' THEN '2026-10-11 11:59:00+00'::timestamptz
 ELSE '2026-10-11 11:00:00+00'::timestamptz END,
 CASE WHEN state='unknown' THEN NULL
 WHEN state='pending_within_deadline' THEN '2026-10-11 12:09:00+00'::timestamptz
 ELSE '2026-10-11 11:10:00+00'::timestamptz END
FROM (VALUES ('ready',930),('cancelled',20),('terminal_failure',5),
 ('pending_within_deadline',30),('overdue_unresolved',12),('unknown',3)) groups(state,total)
CROSS JOIN LATERAL generate_series(0,total-1) n;
INSERT INTO public.export_outcomes
SELECT 'event-' || promise_id,promise_id,tenant_id,'2026-10-11 11:05:00+00',
 CASE WHEN promise_id LIKE 'ready-%' THEN 'ready' ELSE 'terminal_failure' END
FROM public.export_requests WHERE promise_id LIKE 'ready-%' OR promise_id LIKE 'terminal_failure-%';
INSERT INTO public.export_cancellations
SELECT 'event-' || promise_id,promise_id,tenant_id,'2026-10-11 11:05:00+00','cancelled'
FROM public.export_requests WHERE promise_id LIKE 'cancelled-%';
INSERT INTO public.export_attempts
SELECT 'attempt-' || promise_id || '-' || a,promise_id,tenant_id,'2026-10-11 11:02:00+00','failed'
FROM public.export_requests CROSS JOIN LATERAL generate_series(1,
 CASE WHEN promise_id LIKE 'terminal_failure-%' THEN 3
 WHEN promise_id LIKE 'ready-%' AND split_part(promise_id,'-',2)::integer < 81 THEN 1
 ELSE 0 END) a;
INSERT INTO public.export_outcomes
SELECT 'recovery-' || promise_id,promise_id,tenant_id,'2026-10-11 12:01:00+00','ready'
FROM public.export_requests WHERE promise_id LIKE 'overdue_unresolved-%';
INSERT INTO public.export_requests VALUES
 ('new-request','tenant-a','new-customer','2026-10-11 12:02:00+00','2026-10-11 12:12:00+00');
INSERT INTO public.export_coverage VALUES ('tenant-a','2026-10-11 12:05:00+00');
GRANT SELECT ON public.export_requests,public.export_outcomes,public.export_attempts,
 public.export_cancellations,public.export_coverage TO opsgraph_practice_reader;
COMMIT;
"""


def demo_origin(value: str) -> str:
    try:
        address = urlsplit(value)
        port = address.port
    except ValueError:
        raise NoCodeError("Synthetic setup requires its exact loopback launcher address.") from None
    if (
        address.scheme != "http"
        or port is None
        or not 1024 <= port <= 65535
        or value != f"http://127.0.0.1:{port}"
    ):
        raise NoCodeError("Synthetic setup requires its exact loopback launcher address.")
    return value


def demo_path(path: str) -> str:
    allowed = {
        "/api/sources",
        "/api/sources/exports-db/inspect",
        "/api/sources/exports-db/readiness",
        "/api/work-outcomes/contracts",
        "/api/runs",
    }
    if path not in allowed and not re.fullmatch(r"/api/runs/inv-[a-f0-9]{32}", path):
        raise NoCodeError("The synthetic setup response contained an invalid API path.")
    return path


def provision(origin: str, key: str) -> None:
    """Review the synthetic mapping using the normal source/readiness/review APIs."""
    origin = demo_origin(origin)
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(path: str, body: dict | None = None):
        req = Request(  # noqa: S310 -- origin is the bound loopback launcher address
            origin + demo_path(path),
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-OpsGraph-Key": key, "Content-Type": "application/json"},
        )
        with opener.open(req, timeout=15) as response:  # noqa: S310
            return json.load(response)

    request(
        "/api/sources",
        {
            "id": "exports-db",
            "name": "Synthetic export replay — invented records",
            "secret_ref": "OPSGRAPH_SOURCE_DSN",
            "allowed_schemas": ["public"],
            "allowed_tables": list(TABLES),
            "hosting_profile": "local",
        },
    )
    snapshot = request("/api/sources/exports-db/inspect", {})
    request(
        "/api/sources/exports-db/readiness",
        {"table": TABLES[0], "confirm_bounded_read": True},
    )
    definition = json.loads(ROOT.joinpath("tests/missed_work/export-contract.json").read_text())
    definition["schema_fingerprint"] = snapshot["fingerprint"]
    request(
        "/api/work-outcomes/contracts",
        {"definition": definition, "confirm_reviewed_meanings": True},
    )
    created = request(
        "/api/runs",
        {
            "question": "Synthetic replay: which accepted exports missed their deadline?",
            "source_id": "exports-db",
            "work_outcome": SCOPE,
        },
    )
    run_id = created.get("id")
    if not isinstance(run_id, str) or not re.fullmatch(r"inv-[a-f0-9]{32}", run_id):
        raise NoCodeError("The synthetic setup response contained an invalid audit identifier.")
    run_path = "/api/runs/" + quote(run_id, safe="")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        saved = request(run_path)
        if saved["status"] == "completed":
            facts = saved["answer"]["work_outcome"]["facts"]
            if facts["population"] != 1000 or facts["counts"]["overdue_unresolved"] != 12:
                raise NoCodeError("Synthetic audit did not match its known baseline.")
            print("SYNTHETIC replay ready. Open Export audits, then its saved completed audit.")
            print("Expected: 1,000 promises; 12 overdue; 8 affected customers; 3 unknown.")
            print(
                "Recheck at 2026-10-11 12:05 UTC: 0 overdue; 942 ready; original cohort unchanged."
            )
            return
        if saved["status"] in {"failed", "blocked", "cancelled", "interrupted"}:
            raise NoCodeError("Synthetic audit stopped. Inspect its saved error in Export audits.")
        time.sleep(0.1)
    raise NoCodeError("Synthetic audit is still running. Reopen it in Export audits.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="opsgraph-export-demo-", dir=Path(tempfile.gettempdir()).resolve()
    ) as directory:
        workspace = Path(directory) / "workspace"
        database = PracticeDatabase(workspace)
        try:
            database.start()
            with psycopg.connect(database.dsn(admin=True)) as connection:
                connection.execute(SEED)
            key = secrets.token_urlsafe(32)
            write_private_config(
                workspace / ".env",
                {
                    "OPSGRAPH_API_KEY": key,
                    "OPSGRAPH_WORKSPACE_ID": "synthetic-export-replay",
                    "OPSGRAPH_MODE": "connected",
                    "OPSGRAPH_MODEL_PROVIDER": "openai_compatible",
                    "OPSGRAPH_EGRESS_ENABLED": "false",
                    "OPSGRAPH_SOURCE_DSN": database.dsn(),
                    "OPSGRAPH_POSTGRES_SECRET_REF": "OPSGRAPH_SOURCE_DSN",
                    "OPSGRAPH_STATE_PATH": str(workspace / "state.db"),
                },
            )
            print("Invented data only. Creates a fresh temporary workspace and PostgreSQL service.")
            print(
                "No model is configured. Ctrl+C stops both services and removes the demo workspace."
            )
            return launch(
                workspace,
                free_port(),
                configure=False,
                browser=not options.no_browser,
                on_ready=lambda origin: provision(origin, key),
            )
        except (NoCodeError, psycopg.Error):
            print(
                "Demo setup could not finish. Check local PostgreSQL 15–18 or local Docker Desktop."
            )
            return 1
        finally:
            database.stop()


if __name__ == "__main__":
    raise SystemExit(main())
