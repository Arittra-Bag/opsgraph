from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from opsgraph.api.dependencies import require_workspace
from opsgraph.api.provider_settings import router_for
from opsgraph.config import Settings
from opsgraph.persistence.runs import RunStore
from opsgraph.provider_settings import ProviderSettingsRequest, make_config, settings_path
from opsgraph.providers import ProviderConfig
from opsgraph.runtime import build_runtime


@pytest.fixture
def configured(tmp_path):
    settings = Settings(
        api_key="k" * 24,
        model_provider="openai_compatible",
        state_path=tmp_path / "state.db",
        _env_file=None,
    )
    runtime = build_runtime(settings)
    runs = SimpleNamespace(store=RunStore(settings.state_path))
    app = FastAPI()
    app.include_router(router_for(runtime, runs))
    app.dependency_overrides[require_workspace] = lambda: settings.workspace_id
    return TestClient(app), runtime, runs


def payload(**updates):
    return dict(
        provider="ollama", model="qwen3:8b", endpoint="http://127.0.0.1:11434/v1", **updates
    )


def test_save_is_private_and_survives_restart(configured):
    client, runtime, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key="private-provider-key"),
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "ollama"
    assert response.json()["api_key_configured"]
    assert "private-provider-key" not in response.text
    assert "private-provider-key" not in client.get("/api/providers/configuration").text
    restarted = build_runtime(runtime.settings)
    assert restarted.provider.config.api_key.get_secret_value() == "private-provider-key"
    assert settings_path(runtime.settings).stat().st_mode & 0o077 == 0


def test_reject_cross_origin_and_secret_validation_echo(configured):
    client, _, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "https://elsewhere.invalid"},
        json=payload(),
    )
    assert response.status_code == 403
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key={"secret": "DO-NOT-ECHO"}),
    )
    assert response.status_code == 422
    assert "DO-NOT-ECHO" not in response.text


def test_unfinished_run_blocks_configuration(configured):
    client, runtime, runs = configured
    runs.store.create(
        runtime.settings.workspace_id, {"question": "What happened?", "source_id": "source-one"}
    )
    response = client.put(
        "/api/providers/configuration", headers={"Origin": "http://testserver"}, json=payload()
    )
    assert response.status_code == 409
    assert not settings_path(runtime.settings).exists()


def test_endpoint_change_does_not_forward_key():
    previous = ProviderConfig(
        kind="openai_compatible",
        model="old",
        base_url="https://old.example/v1",
        api_key=SecretStr("secret"),
    )
    body = ProviderSettingsRequest(
        provider="openai_compatible",
        model="new",
        endpoint="https://new.example/v1",
        allow_external_egress=True,
    )
    assert make_config(body, previous, True).api_key is None
    body.endpoint = previous.base_url
    assert make_config(body, previous, True).api_key.get_secret_value() == "secret"
    body.clear_api_key = True
    assert make_config(body, previous, True).api_key is None


def test_external_ceiling_and_opt_in(configured):
    client, _, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json={"provider": "anthropic", "model": "model-id", "allow_external_egress": True},
    )
    assert response.status_code == 403
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json={"provider": "anthropic", "model": "model-id"},
    )
    assert response.status_code == 422


def test_settings_api_requires_auth(configured):
    client, _, _ = configured
    client.app.dependency_overrides.clear()
    assert client.get("/api/providers/configuration").status_code == 401
    assert client.put("/api/providers/configuration", json=payload()).status_code == 401


def test_existing_deployed_http_container_endpoint_is_preserved():
    previous = ProviderConfig(
        kind="openai_compatible",
        model="old",
        base_url="http://host.docker.internal:11439/v1",
        egress_enabled=True,
    )
    body = ProviderSettingsRequest(
        provider="ollama", model="qwen3:8b", endpoint=previous.base_url, allow_external_egress=True
    )
    assert make_config(body, previous, True).base_url == previous.base_url
    body.endpoint = "http://different.internal/v1"
    with pytest.raises(ValueError):
        make_config(body, previous, True)


def test_credentials_change_revision_and_legacy_endpoint_never_echoes_secrets(configured):
    from opsgraph.providers import create_provider

    client, runtime, _ = configured
    before = client.get("/api/providers/configuration").json()["revision"]
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key="new-key"),
    )
    assert response.json()["revision"] != before
    runtime.provider = create_provider(
        ProviderConfig(
            kind="openai_compatible",
            model="x",
            base_url="https://user:private@example.test/v1?key=hidden",
        )
    )
    response = client.get("/api/providers/configuration")
    assert response.json()["endpoint"] == ""
    assert "private" not in response.text and "hidden" not in response.text


def test_probe_rejects_result_from_replaced_provider(configured):
    from opsgraph.api.runs import RunAPI
    from opsgraph.providers import StructuredResponse

    client, runtime, _ = configured
    api = RunAPI(runtime, lambda *args: None)
    client.app.include_router(api.router)
    original = runtime.provider

    def invoke(_):
        runtime.provider = object()
        return StructuredResponse(
            provider="openai_compatible", model=original.config.model, output={"ok": True}
        )

    original.invoke_structured = invoke
    response = client.post("/api/providers/current/test")
    assert response.status_code == 409
    assert "changed during" in response.json()["detail"]
