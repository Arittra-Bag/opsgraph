import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from opsgraph.api.dependencies import require_workspace
from opsgraph.api.provider_settings import router_for
from opsgraph.config import Settings
from opsgraph.persistence.runs import RunStore
from opsgraph.policy import ActionRequest
from opsgraph.provider_settings import (
    DEFAULT_ENDPOINTS,
    ProviderSettingsRequest,
    make_config,
    pending_settings_path,
    public_config,
    settings_path,
)
from opsgraph.providers import ProviderConfig
from opsgraph.runtime import build_runtime
from opsgraph.setup import SetupError, read_private_config, write_private_config


@pytest.fixture
def configured(tmp_path):
    settings = Settings(
        api_key="k" * 24,
        model_provider="openai_compatible",
        state_path=tmp_path / "state.db",
        _env_file=None,
    )
    runtime = build_runtime(settings)
    authorizations = []
    runs = SimpleNamespace(
        store=RunStore(settings.state_path),
        authorize=lambda principal, action, resource: authorizations.append(
            (principal, action, resource)
        ),
        authorizations=authorizations,
    )
    app = FastAPI()
    app.include_router(router_for(runtime, runs))
    app.dependency_overrides[require_workspace] = lambda: settings.workspace_id
    return TestClient(app), runtime, runs


def payload(**updates):
    result = {
        "provider": "ollama",
        "model": "qwen3:8b",
        "endpoint": "http://127.0.0.1:11434/v1",
    }
    result.update(updates)
    return result


def test_save_is_private_and_survives_restart(configured):
    client, runtime, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key="private-provider-key"),
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "ollama"
    assert response.json()["adapter"] == "openai_compatible"
    assert response.json()["max_output_tokens"] == 1_200
    assert response.json()["api_key_configured"]
    assert "private-provider-key" not in response.text
    assert "private-provider-key" not in client.get("/api/providers/configuration").text
    audit = runtime.audit.entries[-1]
    assert audit.action == "core.provider.manage" and audit.outcome == "allowed"
    details = dict(audit.details)
    revision = details.pop("revision")
    fingerprint = details.pop("configuration_fingerprint")
    assert details == {
        "reason": "configuration_saved",
        "preset": "ollama",
        "adapter": "openai_compatible",
        "external_egress": False,
    }
    assert len(revision) == 32
    assert fingerprint.startswith("sha256:")
    assert "private-provider-key" not in audit.model_dump_json()
    restarted = build_runtime(runtime.settings)
    assert restarted.provider.config.provider_preset == "ollama"
    assert restarted.provider.config.api_key.get_secret_value() == "private-provider-key"
    if os.name == "posix":
        assert settings_path(runtime.settings).stat().st_mode & 0o077 == 0


def test_process_interruption_during_audit_never_activates_pending_provider(
    configured, monkeypatch
):
    client, runtime, _ = configured
    original_key = "original-private-provider-key"
    saved = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(model="original-model", api_key=original_key),
    )
    assert saved.status_code == 200
    active = settings_path(runtime.settings).read_bytes()

    class SimulatedProcessExit(BaseException):
        pass

    def interrupted(**_):
        raise SimulatedProcessExit()

    monkeypatch.setattr(runtime.audit, "append", interrupted)
    with pytest.raises(SimulatedProcessExit):
        client.put(
            "/api/providers/configuration",
            headers={"Origin": "http://testserver"},
            json=payload(model="uncommitted-model", api_key="uncommitted-private-key"),
        )

    assert settings_path(runtime.settings).read_bytes() == active
    assert pending_settings_path(runtime.settings).exists()
    restarted = build_runtime(runtime.settings)
    assert restarted.provider.config.model == "original-model"
    assert restarted.provider.config.api_key.get_secret_value() == original_key
    assert not pending_settings_path(runtime.settings).exists()


def test_restart_rejects_provider_configuration_changed_after_its_audit(configured):
    client, runtime, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(model="audited-model", api_key="private-provider-key"),
    )
    assert response.status_code == 200
    path = settings_path(runtime.settings)
    values = read_private_config(path)
    values["PROVIDER_CONFIG"] = values["PROVIDER_CONFIG"].replace(
        "audited-model", "unaudited-model"
    )
    write_private_config(path, values)

    with pytest.raises(SetupError, match="matching audit record"):
        build_runtime(runtime.settings)


def test_restart_rejects_provider_configuration_without_revision(configured):
    client, runtime, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key="private-provider-key"),
    )
    assert response.status_code == 200
    path = settings_path(runtime.settings)
    values = read_private_config(path)
    values.pop("PROVIDER_REVISION")
    write_private_config(path, values)

    with pytest.raises(SetupError, match="missing its audit revision"):
        build_runtime(runtime.settings)


def test_restart_rejects_provider_credential_changed_after_its_audit(configured):
    client, runtime, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key="audited-private-provider-key"),
    )
    assert response.status_code == 200
    path = settings_path(runtime.settings)
    values = read_private_config(path)
    values["PROVIDER_KEY"] = "replacement-private-provider-key"
    write_private_config(path, values)

    with pytest.raises(SetupError, match="matching audit record"):
        build_runtime(runtime.settings)


def test_audit_failure_rolls_back_private_and_runtime_provider_state(configured, monkeypatch):
    client, runtime, _ = configured
    original_key = "original-private-provider-key"
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(model="original-model", api_key=original_key),
    )
    assert response.status_code == 200
    path = settings_path(runtime.settings)
    before_file = path.read_bytes()
    before_provider = runtime.provider
    before_revision = runtime.provider_revision
    before_models = (
        runtime.settings.model_provider,
        runtime.settings.local_model,
        runtime.settings.anthropic_model,
    )

    def unavailable(**_):
        raise OSError("private audit location must not escape")

    monkeypatch.setattr(runtime.audit, "append", unavailable)
    replacement_key = "replacement-private-provider-key"
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(model="replacement-model", api_key=replacement_key),
    )

    assert response.status_code == 503
    assert original_key not in response.text and replacement_key not in response.text
    assert runtime.provider is before_provider
    assert runtime.provider_revision == before_revision
    assert (
        runtime.settings.model_provider,
        runtime.settings.local_model,
        runtime.settings.anthropic_model,
    ) == before_models
    assert path.read_bytes() == before_file


def test_reject_cross_origin_and_secret_validation_echo(configured):
    client, runtime, _ = configured
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "https://elsewhere.invalid"},
        json=payload(),
    )
    assert response.status_code == 403
    assert runtime.audit.entries[-1].details["reason"] == "origin_check_failed"
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(api_key={"secret": "DO-NOT-ECHO"}),
    )
    assert response.status_code == 422
    assert "DO-NOT-ECHO" not in response.text
    assert runtime.audit.entries[-1].details["reason"] == "invalid_configuration"
    assert "DO-NOT-ECHO" not in runtime.audit.entries[-1].model_dump_json()


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


def test_update_requires_provider_manage_authorization(configured):
    client, runtime, runs = configured
    response = client.put(
        "/api/providers/configuration", headers={"Origin": "http://testserver"}, json=payload()
    )
    assert response.status_code == 200
    principal, action, resource = runs.authorizations[-1]
    assert principal.workspace_id == runtime.settings.workspace_id
    assert (action, resource) == ("core.provider.manage", "current-provider")

    def deny(*_):
        raise HTTPException(403, "no matching allow rule")

    runs.authorize = deny
    before = runtime.provider.config
    saved = settings_path(runtime.settings).read_bytes()
    response = client.put(
        "/api/providers/configuration",
        headers={"Origin": "http://testserver"},
        json=payload(model="blocked-model"),
    )
    assert response.status_code == 403
    assert runtime.provider.config == before
    assert settings_path(runtime.settings).read_bytes() == saved


def test_runtime_policy_allows_provider_management_for_local_analyst(configured):
    from opsgraph.domain import Principal

    _, runtime, _ = configured
    principal = Principal(
        subject="local-operator",
        workspace_id=runtime.settings.workspace_id,
        roles=frozenset({"analyst"}),
    )
    decision = runtime.policy.authorize(
        ActionRequest(
            principal=principal,
            action="core.provider.manage",
            workspace_id=principal.workspace_id,
            resource="current-provider",
        )
    )
    assert decision.allowed is True


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


def test_key_retention_tracks_destination_instead_of_preset_label():
    previous = ProviderConfig(
        kind="openai_compatible",
        provider_preset="openai",
        model="old",
        base_url=DEFAULT_ENDPOINTS["openai"],
        api_key=SecretStr("secret"),
        egress_enabled=True,
    )
    same = ProviderSettingsRequest(
        provider="custom_openai",
        model="new",
        endpoint=DEFAULT_ENDPOINTS["openai"],
        allow_external_egress=True,
    )
    assert make_config(same, previous, True).api_key.get_secret_value() == "secret"
    changed = same.model_copy(
        update={"provider": "openrouter", "endpoint": DEFAULT_ENDPOINTS["openrouter"]}
    )
    assert make_config(changed, previous, True).api_key is None


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


@pytest.mark.parametrize(
    "preset,expected_endpoint,expected_adapter,expected_profile",
    [
        ("ollama", "http://127.0.0.1:11434/v1", "openai_compatible", "ollama"),
        ("openai", "https://api.openai.com/v1", "openai_compatible", "standard"),
        (
            "openrouter",
            "https://openrouter.ai/api/v1",
            "openai_compatible",
            "standard",
        ),
        ("groq", "https://api.groq.com/openai/v1", "openai_compatible", "standard"),
        ("together", "https://api.together.xyz/v1", "openai_compatible", "standard"),
        ("mistral", "https://api.mistral.ai/v1", "openai_compatible", "standard"),
        ("lm_studio", "http://127.0.0.1:1234/v1", "openai_compatible", "standard"),
        ("vllm", "http://127.0.0.1:8000/v1", "openai_compatible", "standard"),
        ("anthropic", None, "anthropic", "standard"),
        (
            "custom_openai",
            "https://models.example/v1",
            "openai_compatible",
            "standard",
        ),
    ],
)
def test_presets_resolve_to_bounded_adapter_configuration(
    preset, expected_endpoint, expected_adapter, expected_profile
):
    previous = ProviderConfig(kind="deterministic", model="fixture")
    request = ProviderSettingsRequest(
        provider=preset,
        model="model-id",
        endpoint="https://models.example/v1" if preset == "custom_openai" else None,
        allow_external_egress=expected_endpoint is None or expected_endpoint.startswith("https://"),
    )
    config = make_config(request, previous, True)
    assert config.provider_preset == preset
    assert config.kind == expected_adapter
    assert config.base_url == expected_endpoint
    assert config.schema_profile == expected_profile
    assert config.reasoning_effort is None


@pytest.mark.parametrize("preset", ["openai", "openrouter", "groq", "together", "mistral"])
def test_hosted_presets_reject_endpoint_override(preset):
    previous = ProviderConfig(kind="deterministic", model="fixture")
    body = ProviderSettingsRequest(
        provider=preset,
        model="model-id",
        endpoint="https://attacker.example/v1",
        allow_external_egress=True,
    )
    with pytest.raises(SetupError, match="official endpoint"):
        make_config(body, previous, True)


@pytest.mark.parametrize("preset", ["ollama", "lm_studio", "vllm", "custom_openai"])
def test_local_and_custom_presets_accept_explicit_loopback_endpoint(preset):
    previous = ProviderConfig(kind="deterministic", model="fixture")
    body = ProviderSettingsRequest(
        provider=preset,
        model="model-id",
        endpoint="http://127.0.0.1:9999/v1",
    )
    assert make_config(body, previous, True).base_url == "http://127.0.0.1:9999/v1"


def test_public_config_does_not_infer_provider_from_schema_profile():
    legacy = ProviderConfig(
        kind="openai_compatible",
        model="model-id",
        base_url="http://127.0.0.1:9999/v1",
        schema_profile="ollama",
    )
    result = public_config(legacy, False, "revision")
    assert result["provider"] == "custom_openai"
    assert result["adapter"] == "openai_compatible"
    assert result["schema_profile"] == "ollama"


def test_output_token_limit_is_validated_preserved_and_public():
    previous = ProviderConfig(
        kind="openai_compatible",
        model="old",
        base_url="http://127.0.0.1:11434/v1",
        max_output_tokens=2_048,
    )
    body = ProviderSettingsRequest(provider="ollama", model="new")
    assert make_config(body, previous, True).max_output_tokens == 2_048
    configured = make_config(body.model_copy(update={"max_output_tokens": 4_096}), previous, True)
    assert public_config(configured, False, "revision")["max_output_tokens"] == 4_096
    for invalid in (0, 32_769):
        with pytest.raises(ValidationError):
            ProviderSettingsRequest(provider="ollama", model="new", max_output_tokens=invalid)


def test_provider_preset_and_adapter_must_agree():
    with pytest.raises(ValidationError, match="Anthropic preset"):
        ProviderConfig(
            kind="openai_compatible",
            provider_preset="anthropic",
            model="model-id",
            base_url="http://127.0.0.1:11434/v1",
        )
    with pytest.raises(ValidationError, match="OpenAI-compatible presets"):
        ProviderConfig(kind="anthropic", provider_preset="openai", model="model-id")
    with pytest.raises(ValidationError, match="official endpoint"):
        ProviderConfig(
            kind="openai_compatible",
            provider_preset="openai",
            model="model-id",
            base_url="https://attacker.example/v1",
        )


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
