from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from opsgraph.providers import (
    ChatMessage,
    EgressDeniedError,
    ModelProvider,
    ProviderConfig,
    ProviderInvocationError,
    StructuredRequest,
    create_provider,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
REQUEST = StructuredRequest(
    system="Return an evidence-grounded result.",
    messages=(ChatMessage(role="user", content="What changed?"),),
    response_schema=SCHEMA,
)


def test_config_enforces_model_allowlist_and_hides_api_key() -> None:
    config = ProviderConfig(
        kind="anthropic",
        model="claude-test",
        allowed_models=("claude-test",),
        api_key=SecretStr("super-secret-value"),
        egress_enabled=True,
    )
    assert "super-secret-value" not in repr(config)
    assert "super-secret-value" not in str(config.model_dump())

    with pytest.raises(ValidationError, match="model allowlist"):
        ProviderConfig(
            kind="anthropic",
            model="blocked-model",
            allowed_models=("approved-model",),
        )


def test_deterministic_provider_is_offline_and_satisfies_protocol() -> None:
    provider = create_provider(
        ProviderConfig(kind="deterministic", model="fixture-v1"),
        deterministic_responder=lambda request: {"answer": request.messages[-1].content},
    )
    assert isinstance(provider, ModelProvider)
    assert provider.health().status == "ready"
    assert provider.capabilities.external_egress is False
    assert provider.invoke_structured(REQUEST).output == {"answer": "What changed?"}


def test_anthropic_contract_is_bounded_and_parses_structured_output() -> None:
    calls: list[dict] = []

    class Messages:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps({"answer": "bounded"}))],
                usage=SimpleNamespace(input_tokens=10, output_tokens=3),
            )

    client = SimpleNamespace(messages=Messages())
    config = ProviderConfig(
        kind="anthropic",
        model="claude-test",
        allowed_models=("claude-test",),
        api_key=SecretStr("anthropic-secret"),
        egress_enabled=True,
        timeout_seconds=4.5,
        max_output_tokens=321,
    )
    provider = create_provider(config, client_factory=lambda _: client)
    response = provider.invoke_structured(REQUEST)

    assert response.output == {"answer": "bounded"}
    assert response.usage.output_tokens == 3
    assert calls[0]["model"] == "claude-test"
    assert calls[0]["timeout"] == 4.5
    assert calls[0]["max_tokens"] == 321
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert calls[0]["output_config"]["format"]["schema"] == SCHEMA
    assert "anthropic-secret" not in repr(calls)


def test_openai_compatible_contract_supports_custom_local_base_url() -> None:
    calls: list[dict] = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"local"}'))],
                usage=SimpleNamespace(prompt_tokens=8, completion_tokens=2),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    config = ProviderConfig(
        kind="openai_compatible",
        model="qwen3:8b",
        allowed_models=("qwen3:8b",),
        base_url="http://ollama:11434/v1/",
        egress_enabled=True,
        timeout_seconds=9,
        max_output_tokens=456,
    )
    provider = create_provider(config, client_factory=lambda _: client)
    response = provider.invoke_structured(REQUEST)

    assert config.base_url == "http://ollama:11434/v1"
    assert response.output == {"answer": "local"}
    assert calls[0]["timeout"] == 9
    assert calls[0]["max_tokens"] == 456
    assert calls[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("kind", ["anthropic", "openai_compatible"])
def test_external_providers_fail_before_client_creation_when_egress_is_off(kind: str) -> None:
    created = False

    def client_factory(_):
        nonlocal created
        created = True
        return object()

    kwargs = {"base_url": "https://models.example/v1"} if kind == "openai_compatible" else {}
    config = ProviderConfig(
        kind=kind,
        model="approved",
        allowed_models=("approved",),
        api_key=SecretStr("never-used"),
        egress_enabled=False,
        **kwargs,
    )
    provider = create_provider(config, client_factory=client_factory)
    with pytest.raises(EgressDeniedError):
        provider.invoke_structured(REQUEST)
    assert created is False


def test_health_reports_disabled_egress_without_constructing_client() -> None:
    provider = create_provider(
        ProviderConfig(
            kind="openai_compatible",
            model="local-model",
            base_url="https://models.example/v1",
        ),
        client_factory=lambda _: pytest.fail("client must not be constructed"),
    )
    health = provider.health()
    assert health.status == "unavailable"
    assert health.detail == "external egress is disabled for provider openai_compatible"


def test_loopback_provider_does_not_require_external_egress() -> None:
    calls: list[dict] = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"local"}'))],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
            )

    provider = create_provider(
        ProviderConfig(
            kind="openai_compatible",
            model="local-model",
            base_url="http://127.0.0.1:11434/v1",
            egress_enabled=False,
        ),
        client_factory=lambda _: SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )

    assert provider.capabilities.external_egress is False
    assert provider.invoke_structured(REQUEST).output == {"answer": "local"}
    assert calls


def test_provider_errors_do_not_echo_sdk_exception_or_key() -> None:
    secret = "sensitive-provider-key"  # noqa: S105 -- synthetic leak-detection fixture

    class Messages:
        @staticmethod
        def create(**_kwargs):
            raise RuntimeError(f"upstream rejected {secret}")

    config = ProviderConfig(
        kind="anthropic",
        model="claude-test",
        api_key=SecretStr(secret),
        egress_enabled=True,
    )
    provider = create_provider(
        config, client_factory=lambda _: SimpleNamespace(messages=Messages())
    )
    with pytest.raises(ProviderInvocationError) as captured:
        provider.invoke_structured(REQUEST)
    assert secret not in str(captured.value)


@pytest.mark.parametrize("kind", ["anthropic", "openai_compatible"])
def test_sdk_clients_disable_automatic_retries(monkeypatch, kind):
    from opsgraph.providers.adapters import AnthropicProvider, OpenAICompatibleProvider

    calls = []

    def constructor(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "opsgraph.providers.adapters.importlib.import_module",
        lambda _: SimpleNamespace(Anthropic=constructor, OpenAI=constructor),
    )
    config = ProviderConfig(
        kind=kind,
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://localhost/v1" if kind == "openai_compatible" else None,
    )
    adapter = AnthropicProvider if kind == "anthropic" else OpenAICompatibleProvider
    adapter._default_client_factory(config)
    assert calls[0]["max_retries"] == 0
    http_client = calls[0]["http_client"]
    try:
        assert http_client.follow_redirects is False
        assert http_client.trust_env is False
        assert http_client.timeout.read == config.timeout_seconds
    finally:
        http_client.close()


@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high"])
def test_reasoning_effort_is_explicit_and_omitted_by_default(effort):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"local"}'))],
            usage=None,
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ProviderConfig(
        kind="openai_compatible",
        model="local",
        base_url="http://localhost/v1",
        reasoning_effort=effort,
    )
    create_provider(config, client_factory=lambda _: client).invoke_structured(REQUEST)
    if effort is None:
        assert "reasoning_effort" not in calls[0]
    else:
        assert calls[0]["reasoning_effort"] == effort


def test_openai_sdk_timeout_is_classified_without_exposing_request_details():
    import httpx

    from opsgraph.providers import ProviderTimeoutError

    sdk = pytest.importorskip("openai")

    def create(**kwargs):
        raise sdk.APITimeoutError(request=httpx.Request("POST", "http://localhost/private-token"))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ProviderConfig(kind="openai_compatible", model="local", base_url="http://localhost/v1")
    provider = create_provider(config, client_factory=lambda _: client)
    with pytest.raises(ProviderTimeoutError) as exc:
        provider.invoke_structured(REQUEST)
    assert "timed out" in str(exc.value)
    assert "private-token" not in str(exc.value)


@pytest.mark.parametrize("profile", ["standard", "ollama"])
def test_schema_profile_is_explicit_wire_projection_without_mutating_contract(profile):
    import copy

    canonical = {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "minLength": 3, "maxLength": 8},
            "minLength": {"type": "string", "minLength": 1},
            "nested": {"$ref": "#/$defs/Nested"},
        },
        "$defs": {
            "Nested": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "maxLength": 20},
                },
            }
        },
        "required": ["answer"],
        "additionalProperties": False,
    }
    original = copy.deepcopy(canonical)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"valid"}'))],
            usage=None,
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ProviderConfig(
        kind="openai_compatible",
        model="local",
        base_url="http://localhost/v1",
        schema_profile=profile,
    )
    request = StructuredRequest(messages=REQUEST.messages, response_schema=canonical)
    create_provider(config, client_factory=lambda _: client).invoke_structured(request)
    sent = calls[0]["response_format"]["json_schema"]["schema"]
    assert canonical == original and request.response_schema == original
    if profile == "standard":
        assert sent == original
    else:
        assert sent["properties"]["answer"] == {"type": "string"}
        assert sent["properties"]["minLength"] == {"type": "string"}
        assert sent["$defs"]["Nested"]["properties"]["label"] == {"type": "string"}
        assert sent["required"] == ["answer"] and sent["additionalProperties"] is False


def test_operator_timeout_remains_bounded_for_slower_local_hardware():
    config = ProviderConfig(
        kind="openai_compatible", model="local", base_url="http://localhost/v1", timeout_seconds=600
    )
    assert config.timeout_seconds == 600
    with pytest.raises(ValidationError):
        ProviderConfig(
            kind="openai_compatible",
            model="local",
            base_url="http://localhost/v1",
            timeout_seconds=601,
        )
    assert (
        ProviderConfig(
            kind="openai_compatible", model="local", base_url="http://localhost/v1"
        ).timeout_seconds
        == 30
    )


@pytest.mark.parametrize("reported", ["actual-model-revision", None])
def test_reported_model_identity_does_not_fall_back_to_configured_alias(reported):
    def create(**kwargs):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"local"}'))],
            usage=None,
        )
        if reported is not None:
            response.model = reported
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ProviderConfig(
        kind="openai_compatible", model="configured-alias", base_url="http://localhost/v1"
    )
    response = create_provider(config, client_factory=lambda _: client).invoke_structured(REQUEST)
    assert response.model == "configured-alias"
    assert response.reported_model == reported


@pytest.mark.parametrize("profile", ["standard", "ollama"])
def test_ollama_profile_uses_zero_temperature_without_changing_standard(profile):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"local"}'))],
            usage=None,
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ProviderConfig(
        kind="openai_compatible",
        model="local",
        base_url="http://localhost/v1",
        schema_profile=profile,
    )
    create_provider(config, client_factory=lambda _: client).invoke_structured(REQUEST)
    assert calls[0].get("temperature") == (0 if profile == "ollama" else None)
    if profile == "standard":
        assert "temperature" not in calls[0]


def test_token_limit_is_reported_before_json_parsing_even_for_valid_partial_json():
    from opsgraph.providers import ProviderOutputTruncatedError

    def create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"answer":"apparently valid"}'),
                    finish_reason="length",
                )
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = ProviderConfig(kind="openai_compatible", model="local", base_url="http://localhost/v1")
    provider = create_provider(config, client_factory=lambda _: client)
    with pytest.raises(ProviderOutputTruncatedError, match="token limit"):
        provider.invoke_structured(REQUEST)


@pytest.mark.parametrize("kind", ["anthropic", "openai_compatible"])
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "API key"),
        (403, "permissions"),
        (404, "model ID and endpoint"),
        (400, "JSON-schema"),
        (422, "JSON-schema"),
        (429, "quota"),
        (503, "service unavailable"),
        (409, "verify the model server"),
    ],
)
def test_provider_http_errors_are_actionable_without_raw_details(kind, status, expected):
    class SDKFailure(Exception):
        status_code = status

    def fail(**kwargs):
        raise SDKFailure("private-key and private-endpoint-response")

    client = SimpleNamespace(
        messages=SimpleNamespace(create=fail),
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail)),
    )
    config = ProviderConfig(
        kind=kind,
        model="test-model",
        api_key=SecretStr("private-key"),
        egress_enabled=True,
        base_url="http://localhost/v1" if kind == "openai_compatible" else None,
    )
    provider = create_provider(config, client_factory=lambda _: client)
    with pytest.raises(ProviderInvocationError) as captured:
        provider.invoke_structured(REQUEST)
    assert expected in str(captured.value)
    assert "private-" not in str(captured.value)
    assert captured.value.__suppress_context__ is True


def test_openai_connection_error_has_safe_endpoint_guidance():
    import httpx

    sdk = pytest.importorskip("openai")

    def fail(**kwargs):
        raise sdk.APIConnectionError(request=httpx.Request("POST", "https://private-token/v1"))

    provider = create_provider(
        ProviderConfig(kind="openai_compatible", model="local", base_url="http://localhost/v1"),
        client_factory=lambda _: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fail))
        ),
    )
    with pytest.raises(ProviderInvocationError) as captured:
        provider.invoke_structured(REQUEST)
    assert "verify the endpoint" in str(captured.value)
    assert "private-token" not in str(captured.value)


def test_anthropic_constructor_pins_official_endpoint_despite_ambient_url(monkeypatch):
    from opsgraph.providers.adapters import AnthropicProvider

    calls = []
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unapproved.invalid")

    def constructor(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "opsgraph.providers.adapters.importlib.import_module",
        lambda _: SimpleNamespace(Anthropic=constructor),
    )
    AnthropicProvider._default_client_factory(
        ProviderConfig(kind="anthropic", model="test-model", api_key=SecretStr("test-key"))
    )
    try:
        assert calls[0]["base_url"] == "https://api.anthropic.com"
        assert calls[0]["api_key"] == "test-key"
    finally:
        calls[0]["http_client"].close()


def test_actual_anthropic_client_ignores_ambient_endpoint(monkeypatch):
    pytest.importorskip("anthropic")
    from opsgraph.providers.adapters import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unapproved.invalid")
    client = AnthropicProvider._default_client_factory(
        ProviderConfig(kind="anthropic", model="test-model", api_key=SecretStr("test-key"))
    )
    try:
        assert str(client.base_url) == "https://api.anthropic.com"
    finally:
        client.close()
