import pytest
from pydantic import ValidationError

from opsgraph.config import Settings


def test_connected_mode_rejects_the_sample_workspace_key():
    with pytest.raises(ValidationError, match="at least 24 characters"):
        Settings(mode="connected", api_key="sample-local-key-change-me")


def test_connected_mode_accepts_a_strong_workspace_key():
    settings = Settings(mode="connected", api_key="k" * 24)
    assert settings.mode == "connected"
