import json
import os
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from opsgraph.config import get_settings
from opsgraph.maintenance import backup, restore
from opsgraph.persistence import WorkspaceRecord
from opsgraph.runtime import build_runtime
from opsgraph.setup import read_private_config, write_private_config
from opsgraph.source_connections import connection_path, resolve_connection

DSN = "postgresql://reader:fixture-private-password@127.0.0.1:5432/operations"
CA = Path(__file__).parents[1] / "fixtures/supabase-root-2021.crt"


@pytest.fixture
def connections_api(tmp_path, monkeypatch):
    module = import_module("opsgraph.api.app")
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir(mode=0o700)
    settings = get_settings().model_copy(update={"state_path": workspace / ".opsgraph/state.db"})
    runtime = build_runtime(settings)
    monkeypatch.setattr(module, "runtime", runtime)
    monkeypatch.setattr(
        "psycopg.connect", lambda *a, **k: pytest.fail("Saving connections must not connect")
    )
    headers = {"X-OpsGraph-Key": settings.api_key, "Origin": "https://testserver"}
    return (
        module,
        TestClient(module.app, base_url="https://testserver"),
        runtime,
        headers,
        workspace,
    )


def save(api, body=None):
    _, client, _, headers, _ = api
    return client.put("/api/postgres/connections", headers=headers, json=body or {"dsn": DSN})


def test_save_is_private_attested_and_usable_after_runtime_restart(connections_api):
    _, client, runtime, headers, _ = connections_api
    response = save(connections_api)
    assert response.status_code == 200
    ref = response.json()["secret_ref"]
    assert resolve_connection(runtime, ref)
    path = connection_path(runtime.settings, ref)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "fixture-private-password" not in response.text
    assert (
        "fixture-private-password"
        not in client.get("/api/postgres/connections", headers=headers).text
    )
    assert "fixture-private-password" not in json.dumps(
        [entry.model_dump(mode="json") for entry in runtime.audit.entries]
    )
    restarted = build_runtime(runtime.settings)
    assert resolve_connection(restarted, ref) == resolve_connection(runtime, ref)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://other.example"},
        {"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_auth_and_exact_origin_required(connections_api, headers):
    _, client, runtime, auth, _ = connections_api
    request_headers = headers if not headers else {**auth, **headers}
    response = client.put("/api/postgres/connections", headers=request_headers, json={"dsn": DSN})
    assert response.status_code in (401, 403)
    assert not (runtime.settings.state_path.parent / "state.db.sources").exists()


def test_non_loopback_plain_http_cannot_receive_a_private_connection(connections_api):
    module, _, runtime, _, _ = connections_api
    client = TestClient(module.app, base_url="http://remote.example")
    response = client.put(
        "/api/postgres/connections",
        json={"dsn": DSN},
        headers={
            "Origin": "http://remote.example",
            "X-OpsGraph-Key": runtime.settings.api_key,
        },
    )
    assert response.status_code == 403
    assert "fixture-private-password" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"dsn": DSN, "unknown": "fixture-private-password"},
        {"dsn": DSN, "secret_ref": "../../private"},
        {"dsn": "https://fixture-private-password.example"},
        {"dsn": DSN, "ca_pem": "-----BEGIN PRIVATE KEY-----fixture-private-password"},
        {"dsn": "postgresql://reader:fixture-private-password@remote.example:5432/db"},
    ],
)
def test_invalid_requests_are_redacted_and_do_not_change_sources(connections_api, body):
    _, client, runtime, headers, _ = connections_api
    before = runtime.store.list(workspace_id=runtime.settings.workspace_id)
    response = save(connections_api, body)
    assert response.status_code == 422
    assert "fixture-private-password" not in response.text
    assert runtime.store.list(workspace_id=runtime.settings.workspace_id) == before
    assert client.get("/api/sources", headers=headers).status_code == 200


def test_unapproved_reference_and_oversized_body_rejected(connections_api):
    _, client, _, headers, _ = connections_api
    assert (
        save(connections_api, {"dsn": DSN, "secret_ref": "OPSGRAPH_UNKNOWN_DSN"}).status_code == 422
    )
    assert (
        client.put("/api/postgres/connections", headers=headers, content=b"x" * 100_001).status_code
        == 413
    )


def test_gui_does_not_override_tui_or_deployment_environment_connections(
    connections_api, monkeypatch
):
    _, _, runtime, _, _ = connections_api
    monkeypatch.setenv("OPSGRAPH_SOURCE_DSN", DSN)
    assert (
        save(connections_api, {"dsn": DSN, "secret_ref": "OPSGRAPH_SOURCE_DSN"}).status_code == 422
    )
    assert resolve_connection(runtime, "OPSGRAPH_SOURCE_DSN") == DSN


def test_rotation_invalidates_live_readiness_without_rewriting_old_receipts(connections_api):
    _, client, runtime, headers, _ = connections_api
    first = save(connections_api).json()
    ref = first["secret_ref"]
    assert (
        client.post(
            "/api/sources",
            headers=headers,
            json={
                "id": "private-db",
                "name": "Private database",
                "secret_ref": ref,
                "allowed_tables": ["public.jobs"],
            },
        ).status_code
        == 200
    )
    record = runtime.store.get(
        workspace_id=runtime.settings.workspace_id, record_id="source:private-db"
    )
    runtime.store.put(
        WorkspaceRecord(
            record.workspace_id,
            record.record_id,
            {**record.value, "status": "ready", "readiness": {"status": "ready"}},
        )
    )
    receipts = tuple(entry.model_dump_json() for entry in runtime.audit.entries)
    response = save(
        connections_api, {"dsn": DSN.replace("operations", "second"), "secret_ref": ref}
    )
    assert response.status_code == 200
    changed = runtime.store.get(workspace_id=record.workspace_id, record_id=record.record_id).value
    assert changed["status"] == "configured" and changed["readiness"]["status"] == "pending"
    assert changed["connection_revision"] != first["revision"]
    assert (
        tuple(entry.model_dump_json() for entry in runtime.audit.entries[: len(receipts)])
        == receipts
    )


def test_credential_file_tampering_fails_closed(connections_api):
    _, client, runtime, headers, _ = connections_api
    ref = save(connections_api).json()["secret_ref"]
    path = connection_path(runtime.settings, ref)
    values = read_private_config(path)
    write_private_config(path, {**values, "DSN": DSN.replace("operations", "other")})
    response = client.get("/api/postgres/connections", headers=headers)
    assert response.status_code == 200 and "fixture-private-password" not in response.text
    broken = next(item for item in response.json()["connections"] if item["secret_ref"] == ref)
    assert broken["configured"] is False and broken["status"] == "needs_attention"
    with pytest.raises(Exception, match="matching receipt"):
        resolve_connection(runtime, ref)
    repaired = save(connections_api, {"dsn": DSN, "secret_ref": ref})
    assert repaired.status_code == 200
    assert resolve_connection(runtime, ref)


def test_broken_private_connection_does_not_block_an_unrelated_environment_source(
    connections_api, monkeypatch
):
    _, client, runtime, headers, _ = connections_api
    ref = save(connections_api).json()["secret_ref"]
    path = connection_path(runtime.settings, ref)
    path.unlink()
    monkeypatch.setenv("OPSGRAPH_SOURCE_DSN", DSN)
    response = client.post(
        "/api/sources",
        headers=headers,
        json={
            "id": "legacy-db",
            "name": "Legacy database",
            "secret_ref": "OPSGRAPH_SOURCE_DSN",
            "allowed_tables": ["public.jobs"],
        },
    )
    assert response.status_code == 200
    assert resolve_connection(runtime, "OPSGRAPH_SOURCE_DSN") == DSN


def test_legacy_environment_reference_spelling_stays_supported(connections_api, monkeypatch):
    _, _, runtime, _, _ = connections_api
    runtime.settings.allowed_postgres_secret_refs = ("OPSGRAPH_DSN",)
    monkeypatch.setenv("OPSGRAPH_DSN", DSN)
    assert resolve_connection(runtime, "OPSGRAPH_DSN") == DSN


def test_active_run_blocks_connection_changes(connections_api):
    _, _, runtime, _, _ = connections_api
    with sqlite3.connect(runtime.settings.state_path) as db:
        db.execute("CREATE TABLE runs (workspace_id TEXT, status TEXT)")
        db.execute("INSERT INTO runs VALUES (?, 'running')", (runtime.settings.workspace_id,))
    assert save(connections_api).status_code == 409


def test_private_connection_and_ca_survive_backup_restore_to_new_path(connections_api, tmp_path):
    _, _, runtime, _, workspace = connections_api
    response = save(
        connections_api,
        {
            "dsn": "postgresql://reader:fixture-private-password@database.example:5432/db?sslmode=verify-full",
            "ca_pem": CA.read_text(),
        },
    )
    assert response.status_code == 200
    ref = response.json()["secret_ref"]
    before = conninfo_to_dict(resolve_connection(runtime, ref))
    write_private_config(
        workspace / ".env",
        {
            "OPSGRAPH_API_KEY": runtime.settings.api_key,
            "OPSGRAPH_WORKSPACE_ID": runtime.settings.workspace_id,
            "OPSGRAPH_STATE_PATH": str(runtime.settings.state_path),
        },
    )
    copied, target = tmp_path.resolve() / "backup", tmp_path.resolve() / "restored"
    backup(workspace, copied)
    restore(copied, target)
    restored = build_runtime(
        runtime.settings.model_copy(update={"state_path": target / ".opsgraph/state.db"})
    )
    after = conninfo_to_dict(resolve_connection(restored, ref))
    assert before["password"] == after["password"]
    assert before["sslrootcert"] != after["sslrootcert"]
    assert Path(after["sslrootcert"]).is_relative_to(target)
    assert Path(after["sslrootcert"]).read_bytes() == CA.read_bytes()
