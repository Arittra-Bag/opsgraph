import pytest
from pydantic import ValidationError

from opsgraph.config import Settings


def test_connected_mode_rejects_the_sample_workspace_key():
    with pytest.raises(ValidationError, match="at least 24 characters"):
        Settings(mode="connected", api_key="sample-local-key-change-me")


def test_connected_mode_accepts_a_strong_workspace_key():
    settings = Settings(mode="connected", api_key="k" * 24)
    assert settings.mode == "connected"


def test_allowed_postgres_secret_references_accept_comma_separated_environment_values(monkeypatch):
    monkeypatch.setenv(
        "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS",
        "OPSGRAPH_SOURCE_DSN, OPSGRAPH_ANALYTICS_DSN",
    )
    settings = Settings(api_key="k" * 24)
    assert settings.allowed_postgres_secret_refs == (
        "OPSGRAPH_SOURCE_DSN",
        "OPSGRAPH_ANALYTICS_DSN",
    )
