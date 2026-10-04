import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlunsplit

import pytest

from opsgraph import no_code
from opsgraph.setup import ensure_private_directory, read_private_config, run_setup


def test_commands_do_not_inherit_credentials_or_database_defaults(monkeypatch):
    for name in (
        "PGHOST",
        "PGPASSWORD",
        "OPSGRAPH_SOURCE_DSN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LANGSMITH_API_KEY",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
    ):
        monkeypatch.setenv(name, "private-value")
    environment = no_code.child_environment()
    assert not any(
        name in environment
        for name in (
            "PGHOST",
            "PGPASSWORD",
            "OPSGRAPH_SOURCE_DSN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "LANGSMITH_API_KEY",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
        )
    )


def test_command_failure_never_prints_subprocess_output(monkeypatch):
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "private-password", "private-key")

    monkeypatch.setattr(no_code.subprocess, "run", fail)
    with pytest.raises(no_code.NoCodeError) as error:
        no_code.command(["unavailable"])
    assert "private-" not in str(error.value)


def test_timeout_has_safe_retry_message(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output="private-value")

    monkeypatch.setattr(no_code.subprocess, "run", fail)
    with pytest.raises(no_code.NoCodeError, match="retry"):
        no_code.command(["unavailable"])


def test_fixture_credentials_cannot_inject_sql():
    for value in ("", "'; DROP DATABASE postgres; --", "a" * 65):
        with pytest.raises(no_code.NoCodeError):
            no_code.fixture_sql(value)
    assert "NOSUPERUSER" in no_code.fixture_sql(secrets.token_hex(32)).as_string()


def test_same_workspace_cannot_have_concurrent_setup(tmp_path):
    workspace = ensure_private_directory(tmp_path / "workspace")
    with no_code.setup_lock(workspace):
        with pytest.raises(no_code.NoCodeError, match="Another"):
            with no_code.setup_lock(workspace):
                pytest.fail("Second setup acquired lock")
    with no_code.setup_lock(workspace):
        pass


def test_lock_rejects_hard_links_without_changing_target(tmp_path):
    workspace = ensure_private_directory(tmp_path / "workspace")
    target = tmp_path / "target"
    target.write_text("preserved")
    os.link(target, workspace / ".no-code-lock")
    with pytest.raises(no_code.NoCodeError, match="private file"):
        with no_code.setup_lock(workspace):
            pytest.fail("Unsafe lock accepted")
    assert target.read_text() == "preserved"


def test_prepare_preserves_existing_workspace(tmp_path):
    workspace = ensure_private_directory(tmp_path / "workspace")
    config = workspace / ".env"
    config.write_text("existing configuration")
    with pytest.raises(no_code.NoCodeError, match="already contains"):
        no_code.PracticeDatabase(workspace).prepare()
    assert config.read_text() == "existing configuration"


def test_prepare_saves_private_manifest_and_can_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(no_code, "postgres_bin", lambda: Path("/runtime/bin"))
    monkeypatch.setattr(no_code, "free_port", lambda: 15432)
    database = no_code.PracticeDatabase(tmp_path / "workspace")
    database.prepare()
    before = database.manifest.read_bytes()
    database.prepare()
    assert database.manifest.read_bytes() == before
    database.validate_manifest()
    assert "user=opsgraph_practice_reader" in database.dsn()
    assert database.values["ADMIN_PASSWORD"] not in database.dsn()
    if os.name == "posix":
        assert database.manifest.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("PORT", "5432;exit"),
        ("PORT", "0"),
        ("ID", "../other"),
        ("BACKEND", "external"),
        ("BINARY", "relative"),
    ],
)
def test_manifest_validation_fails_closed(tmp_path, monkeypatch, key, value):
    monkeypatch.setattr(no_code, "postgres_bin", lambda: Path("/runtime/bin"))
    monkeypatch.setattr(no_code, "free_port", lambda: 15432)
    database = no_code.PracticeDatabase(tmp_path / "workspace")
    database.prepare()
    database.values[key] = value
    with pytest.raises(no_code.NoCodeError):
        database.validate_manifest()


def test_prepared_database_uses_quick_model_setup_without_database_copy_paste(tmp_path):
    workspace = ensure_private_directory(tmp_path / "workspace")
    labels, output = [], []

    def ask(label):
        labels.append(label)
        return ""

    assert (
        run_setup(
            workspace,
            flow="quick",
            input_fn=ask,
            secret_fn=lambda _: "",
            output_fn=output.append,
            prepared_database=(
                "host=127.0.0.1 port=15432 dbname=postgres user=opsgraph_practice_reader"
            ),
        )
        == 0
    )
    assert not any("hosting" in label.lower() for label in labels)
    config = read_private_config(workspace / ".env")
    assert "opsgraph_practice_reader" in config["OPSGRAPH_SOURCE_DSN"]
    assert config["OPSGRAPH_POSTGRES_HOSTING"] == "local"
    assert config["OPSGRAPH_API_KEY"] not in "\n".join(output)


@pytest.mark.parametrize(
    "origin",
    [
        "https://remote.invalid",
        "file:///tmp/test",
        urlunsplit(("http", "@".join(("127.0.0.1:8000", "remote.invalid")), "", "", "")),
    ],
)
def test_practice_api_cannot_send_workspace_key_to_remote_address(origin):
    with pytest.raises(no_code.NoCodeError):
        no_code.connect_practice(origin, {}, external=False)


def test_cancel_before_database_start_preserves_existing_flow(tmp_path, monkeypatch):
    from opsgraph import launcher

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(launcher, "bind_loopback", lambda port: Listener())
    monkeypatch.setattr(no_code, "free_port", lambda: 18001)
    answers = iter(("1", "n"))
    assert no_code.run_no_code(tmp_path / "workspace", input_fn=lambda _: next(answers)) == 1
    assert not (tmp_path / "workspace/.practice").exists()


@pytest.mark.parametrize("endpoint", ["ssh://remote.invalid", "tcp://remote.invalid:2376"])
def test_remote_docker_is_rejected_without_contacting_server(monkeypatch, endpoint):
    calls = []
    monkeypatch.setattr(no_code, "command", lambda args, **kwargs: calls.append(args) or endpoint)
    with pytest.raises(no_code.NoCodeError, match="Remote Docker"):
        no_code.check_local_docker("/runtime/docker")
    assert len(calls) == 1
    assert "info" not in calls[0]


def test_docker_start_is_isolated_and_does_not_put_password_on_command_line(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(no_code, "postgres_bin", lambda: None)
    monkeypatch.setattr(no_code.shutil, "which", lambda _: "/runtime/docker")
    monkeypatch.setattr(no_code, "free_port", lambda: 15432)

    def run(args, **kwargs):
        calls.append(args)
        return "unix:///local/docker.sock" if "context" in args else "ready"

    monkeypatch.setattr(no_code, "command", run)
    monkeypatch.setattr(
        no_code.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b""),
    )
    database = no_code.PracticeDatabase(tmp_path / "workspace")
    database.prepare()
    database.start_docker()
    start = next(args for args in calls if "run" in args)
    assert "127.0.0.1:15432:5432" in start
    assert database.values["ADMIN_PASSWORD"] not in " ".join(start)
    assert "--env-file" in start
    assert any(value.startswith("type=volume,source=opsgraph-practice-") for value in start)
    assert "--privileged" not in start and "--network" not in start
    assert not (database.directory / "container.env").exists()


def test_no_redirect_never_forwards_a_workspace_key():
    with pytest.raises(no_code.NoCodeError, match="redirect"):
        no_code.NoRedirect().redirect_request(None, None, 302, "", {}, "https://remote.invalid")


def test_cancelled_configuration_stops_only_owned_practice_database(tmp_path, monkeypatch):
    from opsgraph import launcher

    stopped = []

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Database:
        values = {"owned": "service"}

        def __init__(self, workspace):
            self.workspace = workspace

        def start(self):
            pass

        def stop(self):
            stopped.append(self.workspace)

        def dsn(self):
            return "private-read-only-connection"

    monkeypatch.setattr(launcher, "bind_loopback", lambda port: Listener())
    monkeypatch.setattr(no_code, "PracticeDatabase", Database)
    monkeypatch.setattr(no_code, "run_setup", lambda *args, **kwargs: 1)
    monkeypatch.setattr(no_code, "free_port", lambda: 18001)
    assert no_code.run_no_code(tmp_path / "workspace", input_fn=lambda _: "") == 1
    assert stopped == [tmp_path / "workspace"]


def test_occupied_app_port_cannot_start_or_modify_practice_database(tmp_path, monkeypatch):
    from opsgraph import launcher

    def occupied(port):
        raise OSError("occupied")

    monkeypatch.setattr(launcher, "bind_loopback", occupied)
    monkeypatch.setattr(no_code, "PracticeDatabase", lambda _: pytest.fail("Database started"))
    monkeypatch.setattr(no_code, "free_port", lambda: 18001)
    assert no_code.run_no_code(tmp_path / "workspace", input_fn=lambda _: "") == 1


def test_native_cluster_symlink_cannot_redirect_setup_to_existing_database(tmp_path, monkeypatch):
    monkeypatch.setattr(no_code, "postgres_bin", lambda: Path("/runtime/bin"))
    monkeypatch.setattr(no_code, "free_port", lambda: 15432)
    database = no_code.PracticeDatabase(tmp_path / "workspace")
    database.prepare()
    existing = ensure_private_directory(tmp_path / "existing-database")
    marker = existing / "PG_VERSION"
    marker.write_text("16")
    (database.directory / "data").symlink_to(existing, target_is_directory=True)
    with pytest.raises(no_code.SetupError, match="symbolic"):
        database.start()
    assert marker.read_text() == "16"


def test_native_cluster_identity_mismatch_cannot_start_or_stop_service(tmp_path, monkeypatch):
    monkeypatch.setattr(no_code, "postgres_bin", lambda: Path("/runtime/bin"))
    monkeypatch.setattr(no_code, "free_port", lambda: 15432)
    database = no_code.PracticeDatabase(tmp_path / "workspace")
    database.prepare()
    data = ensure_private_directory(database.directory / "data")
    (data / "PG_VERSION").write_text("16")
    database.values["NATIVE_SYSTEM_ID"] = "12345"
    monkeypatch.setattr(database, "native_identity", lambda: "54321")
    monkeypatch.setattr(no_code, "command", lambda *args, **kwargs: pytest.fail("Service changed"))
    with pytest.raises(no_code.NoCodeError, match="does not belong"):
        database.start()
    with pytest.raises(no_code.NoCodeError, match="does not belong"):
        database.stop()


@pytest.mark.parametrize("field", ["label", "image", "ports", "volume"])
def test_docker_identity_requires_owned_volume_and_loopback_binding(tmp_path, monkeypatch, field):
    monkeypatch.setattr(no_code, "postgres_bin", lambda: Path("/runtime/bin"))
    monkeypatch.setattr(no_code, "free_port", lambda: 15432)
    database = no_code.PracticeDatabase(tmp_path / "workspace")
    database.prepare()
    identity = database.values["ID"]
    entry = {
        "Config": {"Labels": {"opsgraph.practice": identity}, "Image": no_code.POSTGRES_IMAGE},
        "HostConfig": {
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15432"}]}
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "opsgraph-practice-" + identity,
                "Destination": "/var/lib/postgresql/data",
            }
        ],
    }
    database.verify_docker_identity(entry)
    if field == "label":
        entry["Config"]["Labels"] = {}
    elif field == "image":
        entry["Config"]["Image"] = "unrelated"
    elif field == "ports":
        entry["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"  # noqa: S104
    else:
        entry["Mounts"][0]["Name"] = "existing-database"
    with pytest.raises(no_code.NoCodeError, match="isolation changed"):
        database.verify_docker_identity(entry)
