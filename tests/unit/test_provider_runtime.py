from opsgraph.config import Settings
from opsgraph.runtime import _provider


def test_explicit_empty_endpoint_key_does_not_forward_ambient_credential(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-ambient-secret")
    monkeypatch.setenv("OPSGRAPH_OPENAI_API_KEY", "")
    provider = _provider(
        Settings(api_key="k" * 24, model_provider="openai_compatible", _env_file=None)
    )
    assert provider.config.api_key is None


def test_legacy_ambient_key_remains_available_when_dedicated_key_absent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-secret")
    monkeypatch.delenv("OPSGRAPH_OPENAI_API_KEY", raising=False)
    provider = _provider(
        Settings(api_key="k" * 24, model_provider="openai_compatible", _env_file=None)
    )
    assert provider.config.api_key.get_secret_value() == "legacy-secret"
