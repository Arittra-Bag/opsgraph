import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opsgraph import launcher
from opsgraph.launcher import BrowserHandoff, attach_handoff


def test_browser_handoff_is_atomic_single_use_and_expires():
    handoff = BrowserHandoff("private-key", "http://127.0.0.1:8000")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(handoff.consume, [handoff.token] * 8))
    assert results.count("private-key") == 1
    assert results.count(None) == 7
    expired = BrowserHandoff("private-key", "http://127.0.0.1:8000", lifetime=-1)
    assert expired.consume(expired.token) is None


def test_browser_handoff_requires_exact_local_origin_and_never_returns_key_on_failure():
    app = FastAPI()
    origin = "http://127.0.0.1:8000"
    handoff = BrowserHandoff("private-workspace-key", origin)
    attach_handoff(app, handoff)
    with TestClient(app, base_url=origin) as client:
        for headers in (
            {},
            {"Origin": "https://example.invalid"},
            {"Origin": origin, "Host": "another.invalid"},
        ):
            result = client.post(
                "/api/launcher/session", headers=headers, json={"token": handoff.token}
            )
            assert result.status_code == 403
            assert "private-workspace-key" not in result.text
        wrong = client.post(
            "/api/launcher/session",
            headers={"Origin": origin},
            json={"token": "invalid-unicode-\u03bb"},
        )
        assert wrong.status_code == 403
        result = client.post(
            "/api/launcher/session", headers={"Origin": origin}, json={"token": handoff.token}
        )
        assert result.status_code == 200
        assert result.json() == {"key": "private-workspace-key"}
        assert result.headers["cache-control"] == "no-store"
        repeated = client.post(
            "/api/launcher/session", headers={"Origin": origin}, json={"token": handoff.token}
        )
        assert repeated.status_code == 403


def test_browser_handoff_rejects_oversized_or_malformed_body_without_consuming_token():
    app = FastAPI()
    origin = "http://127.0.0.1:8000"
    handoff = BrowserHandoff("private-key", origin)
    attach_handoff(app, handoff)
    with TestClient(app, base_url=origin) as client:
        headers = {"Origin": origin, "Content-Type": "application/json"}
        assert (
            client.post("/api/launcher/session", headers=headers, content="x" * 1025).status_code
            == 413
        )
        assert client.post("/api/launcher/session", headers=headers, content="{").status_code == 403
        assert (
            client.post(
                "/api/launcher/session", headers=headers, json={"token": handoff.token}
            ).status_code
            == 200
        )


class RecordingListener:
    """Socket boundary double: these checks never open or bind a network socket."""

    def __init__(self, *, busy=False):
        self.calls = []
        self.busy = busy

    def setsockopt(self, *args):
        self.calls.append(("option", *args))

    def bind(self, address):
        self.calls.append(("bind", address))
        if self.busy:
            raise OSError("fixture listener busy")

    def listen(self, backlog):
        self.calls.append(("listen", backlog))

    def close(self):
        self.calls.append(("close",))


@pytest.mark.parametrize("platform,expected_option", [("posix", 2), ("nt", -5)])
def test_listener_sets_platform_ownership_option_before_binding(
    monkeypatch, platform, expected_option
):
    listener = RecordingListener()
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(
        launcher,
        "socket",
        SimpleNamespace(
            AF_INET=2,
            SOCK_STREAM=1,
            SOL_SOCKET=1,
            SO_REUSEADDR=2,
            SO_EXCLUSIVEADDRUSE=-5,
            socket=lambda *_: listener,
        ),
    )
    assert launcher.bind_loopback(8000) is listener
    assert listener.calls == [
        ("option", 1, expected_option, 1),
        ("bind", ("127.0.0.1", 8000)),
        ("listen", 128),
    ]


def test_busy_listener_is_closed_without_replacing_an_existing_process(monkeypatch):
    listener = RecordingListener(busy=True)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        launcher,
        "socket",
        SimpleNamespace(
            AF_INET=2,
            SOCK_STREAM=1,
            SOL_SOCKET=1,
            SO_REUSEADDR=2,
            socket=lambda *_: listener,
        ),
    )
    with pytest.raises(OSError, match="Port 8000 is unavailable"):
        launcher.bind_loopback(8000)
    assert listener.calls[-1] == ("close",)
    assert not any(call[0] == "listen" for call in listener.calls)


@pytest.mark.parametrize("startup_succeeded,expected_exit", [(False, 2), (True, 0)])
def test_launch_isolates_config_and_reports_actual_startup(
    monkeypatch, tmp_path, capsys, startup_succeeded, expected_exit
):
    from opsgraph import config, setup

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").touch()
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(prefix=str(tmp_path / "runtime")))
    parent_environment = {
        "PATH": "/fixture/bin",
        "HOME": "/fixture/home",
        "OPSGRAPH_OTHER_DSN": "fixture-only-ambient-source",
        "OPENAI_API_KEY": "fixture-only-ambient-model-key",
        "ANTHROPIC_API_KEY": "fixture-only-other-model-key",
        "LANGCHAIN_TRACING_V2": "true",
        "LANGSMITH_API_KEY": "fixture-only-tracing-key",
        **{
            name: "fixture-only-ambient-value"
            for name in (
                "PGHOST",
                "PGPORT",
                "PGDATABASE",
                "PGUSER",
                "PGPASSWORD",
                "PGSERVICE",
                "PGSERVICEFILE",
                "PGPASSFILE",
                "PGOPTIONS",
                "PGSSLMODE",
                "PGAPPNAME",
            )
        },
    }
    process_environment = dict(parent_environment)
    directories = []
    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            name="posix",
            environ=process_environment,
            chdir=directories.append,
        ),
    )
    configured_key = "fixture-only-configured-workspace-key"
    values = {
        "OPSGRAPH_API_KEY": configured_key,
        "OPSGRAPH_SOURCE_DSN": "dbname=fixture_only user=fixture_reader",
        "PGSSLMODE": "verify-full",
        "PGHOST": None,
    }
    monkeypatch.setattr(setup, "read_private_config", lambda _: values)

    def settings():
        return SimpleNamespace(api_key=configured_key, state_path=Path(".opsgraph/state.db"))

    settings.cache_clear = lambda: None
    monkeypatch.setattr(config, "get_settings", settings)
    app = FastAPI()
    monkeypatch.setitem(sys.modules, "opsgraph.api.app", SimpleNamespace(app=app))
    listener = RecordingListener()
    monkeypatch.setattr(launcher, "bind_loopback", lambda _: listener)
    supplied_sockets = []
    server = SimpleNamespace(
        started=startup_succeeded,
        should_exit=not startup_succeeded,
        run=lambda *, sockets: supplied_sockets.extend(sockets),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(
            Config=lambda *args, **kwargs: None,
            Server=lambda _: server,
        ),
    )
    monkeypatch.setattr(
        launcher,
        "threading",
        SimpleNamespace(
            Lock=launcher.threading.Lock,
            Thread=lambda **_: SimpleNamespace(start=lambda: None),
        ),
    )

    assert launcher.launch(workspace, 8000, configure=False, browser=False) == expected_exit
    assert directories == [workspace]
    assert supplied_sockets == [listener]
    assert listener.calls == [("close",)]
    assert process_environment == {
        "PATH": "/fixture/bin",
        "HOME": "/fixture/home",
        **{key: value for key, value in values.items() if value is not None},
        "OPSGRAPH_STATE_PATH": str(workspace / ".opsgraph/state.db"),
    }
    assert parent_environment["PGHOST"] == "fixture-only-ambient-value"
    output = capsys.readouterr().out
    assert ("OpsGraph did not start" in output) is not startup_succeeded
    assert configured_key not in output
    assert "fixture-only-ambient" not in output


@pytest.mark.parametrize("location", ["exact", "child", "normalized"])
def test_runtime_contained_workspace_is_rejected_before_setup_or_binding(
    monkeypatch, tmp_path, capsys, location
):
    from opsgraph import setup

    runtime = tmp_path / "runtime"
    workspace = {
        "exact": runtime,
        "child": runtime / "workspace",
        "normalized": tmp_path / "sibling" / ".." / "runtime" / "workspace",
    }[location]
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(prefix=str(runtime)))

    def forbidden(*args, **kwargs):
        raise AssertionError("Runtime-contained workspace reached setup or listener binding")

    monkeypatch.setattr(setup, "run_setup", forbidden)
    monkeypatch.setattr(launcher, "bind_loopback", forbidden)
    assert launcher.launch(workspace, 8000, configure=True, browser=False) == 2
    assert "outside the installed Python runtime" in capsys.readouterr().out
    assert not runtime.exists()


@pytest.mark.parametrize("relative", [False, True])
def test_external_workspace_with_runtime_state_is_rejected_before_app_import(
    monkeypatch, tmp_path, capsys, relative
):
    from opsgraph import config, setup

    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").touch()
    state_path = Path("..") / "runtime" / "state.db" if relative else runtime / "state.db"
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(prefix=str(runtime)))
    monkeypatch.setattr(launcher, "os", SimpleNamespace(environ={}, chdir=lambda _: None))
    monkeypatch.setattr(setup, "read_private_config", lambda _: {})

    def settings():
        return SimpleNamespace(state_path=state_path)

    settings.cache_clear = lambda: None
    monkeypatch.setattr(config, "get_settings", settings)
    listener = RecordingListener()
    monkeypatch.setattr(launcher, "bind_loopback", lambda _: listener)

    class UnavailableApp:
        def __getattr__(self, name):
            if name == "app":
                raise AssertionError("Runtime state reached application import")
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "opsgraph.api.app", UnavailableApp())
    assert launcher.launch(workspace, 8000, configure=False, browser=False) == 2
    assert "Choose a state database outside" in capsys.readouterr().out
    assert listener.calls == [("close",)]
    assert not runtime.exists()
