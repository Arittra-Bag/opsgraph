"""Demo requests remain inside the explicit loopback service and API routes."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/missed_work_demo.py"
SPEC = importlib.util.spec_from_file_location("missed_work_demo_tests", SCRIPT)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:4187",
        "http://localhost:4187",
        "http://127.0.0.1:4187/",
        "http://127.0.0.1:4187?next=http://example.org",
        "http://127.0.0.1:4187#fragment",
        "http://127.0.0.1:4187/api/runs",
        "http://127.0.0.1:80",
        "http://127.0.0.1:65536",
        # URL userinfo is rejected before any request; this supplies no credential.
        "http://user@127.0.0.1:4187",
        "file:///private/tmp/config",
        "http://127.0.0.1:04187",
    ],
)
def test_rejects_alternate_origins_before_constructing_an_opener(origin, monkeypatch):
    opener = Mock()
    monkeypatch.setattr(demo, "build_opener", opener)
    with pytest.raises(demo.NoCodeError, match="exact loopback"):
        demo.provision(origin, "synthetic-test-key")
    opener.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/../../audit",
        "/api/runs/%2e%2e%2faudit",
        "/api/runs/inv-" + "a" * 32 + "/export",
        "/api/runs/inv-" + "a" * 32 + "?key=other",
        "/api/runs/inv-" + "a" * 32 + "#fragment",
        "/api/runs/inv-" + "a" * 32 + "\n",
        "/api/audit",
        "//other.example/api/runs",
        "https://other.example/api/runs",
    ],
)
def test_demo_request_routes_reject_traversal_and_unrelated_api_paths(path):
    with pytest.raises(demo.NoCodeError, match="invalid API path"):
        demo.demo_path(path)


def test_accepts_only_canonical_loopback_and_known_run_path():
    assert demo.demo_origin("http://127.0.0.1:4187") == "http://127.0.0.1:4187"
    path = "/api/runs/inv-" + "a" * 32
    assert demo.demo_path(path) == path
    assert demo.demo_path("/api/sources/exports-db/readiness")


def test_malicious_returned_run_id_never_reaches_a_poll_request(monkeypatch):
    opener = Mock()
    response = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(demo, "build_opener", lambda *_: opener)
    payloads = iter([{}, {"fingerprint": "synthetic-fingerprint"}, {}, {}, {"id": "../../audit"}])
    monkeypatch.setattr(demo.json, "load", lambda _: next(payloads))
    with pytest.raises(demo.NoCodeError, match="invalid audit identifier"):
        demo.provision("http://127.0.0.1:4187", "synthetic-test-key")
    assert opener.open.call_count == 5
    assert all(".." not in call.args[0].full_url for call in opener.open.call_args_list)
