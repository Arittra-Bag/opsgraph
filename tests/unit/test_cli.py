import os
import stat

import pytest
from dotenv import dotenv_values

from opsgraph.cli import _doctor, _init
from opsgraph.config import get_settings


def test_init_generates_private_real_configuration_without_printing_key(tmp_path, capsys):
    assert _init(tmp_path) == 0
    values = dotenv_values(tmp_path / ".env")
    key = values["OPSGRAPH_API_KEY"]
    assert len(key) >= 40
    assert key not in capsys.readouterr().out
    assert values["OPSGRAPH_MODE"] == "connected"
    assert values["OPSGRAPH_MODEL_PROVIDER"] == "openai_compatible"
    assert values["OPSGRAPH_LOCAL_REASONING_EFFORT"] == "none"
    assert values["OPSGRAPH_LOCAL_SCHEMA_PROFILE"] == "ollama"
    assert values["OPSGRAPH_SOURCE_DSN"] == ""
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / ".opsgraph").stat().st_mode) == 0o700


def test_init_preserves_existing_configuration_and_assets(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("EXISTING=value\n")
    asset = tmp_path / "sketch.png"
    asset.write_bytes(b"existing asset")
    assert _init(tmp_path) == 0
    assert env.read_text() == "EXISTING=value\n"
    assert asset.read_bytes() == b"existing asset"
    assert "preserved" in capsys.readouterr().out


def test_init_generates_distinct_keys(tmp_path):
    _init(tmp_path / "first")
    _init(tmp_path / "second")
    assert (
        dotenv_values(tmp_path / "first/.env")["OPSGRAPH_API_KEY"]
        != dotenv_values(tmp_path / "second/.env")["OPSGRAPH_API_KEY"]
    )


def test_doctor_reports_configuration_errors_without_echoing_secrets(monkeypatch, capsys):
    monkeypatch.setenv("OPSGRAPH_API_KEY", "private")
    monkeypatch.setenv("OPSGRAPH_MODE", "connected")
    get_settings.cache_clear()
    try:
        assert _doctor() == 1
        output = capsys.readouterr().out
        assert "private" not in output
        assert "FAIL  configuration" in output
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("hostname", ["host.docker.internal", "ollama"])
def test_doctor_requires_egress_for_container_model_hosts(monkeypatch, capsys, hostname):
    monkeypatch.setenv("OPSGRAPH_API_KEY", "k" * 32)
    monkeypatch.setenv("OPSGRAPH_MODE", "connected")
    monkeypatch.setenv("OPSGRAPH_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPSGRAPH_LOCAL_MODEL_URL", f"http://{hostname}:11434/v1")
    monkeypatch.setenv("OPSGRAPH_EGRESS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert _doctor() == 1
        output = capsys.readouterr().out
        assert "FAIL  provider_egress" in output
        assert "source and playbook egress permissions" in output
    finally:
        get_settings.cache_clear()


def test_doctor_reports_absent_source_without_network(monkeypatch, capsys):
    monkeypatch.setenv("OPSGRAPH_API_KEY", "k" * 32)
    monkeypatch.setenv("OPSGRAPH_MODE", "connected")
    monkeypatch.setenv("OPSGRAPH_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPSGRAPH_POSTGRES_SECRET_REF", "OPSGRAPH_MISSING_DSN")
    monkeypatch.setenv("OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS", "OPSGRAPH_MISSING_DSN")
    monkeypatch.delenv("OPSGRAPH_MISSING_DSN", raising=False)
    get_settings.cache_clear()
    try:
        assert _doctor() == 1
        output = capsys.readouterr().out
        assert "FAIL  source_reference" in output
        assert "Configuration checks only" in output
    finally:
        get_settings.cache_clear()
