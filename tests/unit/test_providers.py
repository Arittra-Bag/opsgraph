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
