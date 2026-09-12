"""Built-in deterministic, Anthropic, and OpenAI-compatible adapters."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from opsgraph.providers.base import (
    EgressDeniedError,
    ProviderConfigurationError,
    ProviderInvocationError,
    ProviderOutputTruncatedError,
    ProviderUnavailableError,
    invocation_error,
    parse_json_object,
    require_api_key,
    require_external_egress,
)
from opsgraph.providers.models import (
    ProviderCapabilities,
    ProviderConfig,
    ProviderHealth,
    ProviderUsage,
    StructuredRequest,
    StructuredResponse,
)

ClientFactory = Callable[[ProviderConfig], Any]
DeterministicResponder = Callable[[StructuredRequest], dict[str, Any]]


def _default_deterministic_response(request: StructuredRequest) -> dict[str, Any]:
    return {"status": "deterministic", "content": request.messages[-1].content}


def _reported_model(response: Any) -> str | None:
    value = getattr(response, "model", None)
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _provider_http_client(config: ProviderConfig) -> Any:
    # HTTPX is installed with either optional provider SDK. Keep core imports
    # usable without those optional dependencies.
    import httpx

    return httpx.Client(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False)


class DeterministicProvider:
    """Offline provider for tests, replay, and zero-egress installations."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        responder: DeterministicResponder | None = None,
    ) -> None:
        if config.kind != "deterministic":
            raise ProviderConfigurationError(
                "deterministic adapter received the wrong provider kind"
            )
        self._config = config
        self._responder = responder or _default_deterministic_response

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(external_egress=False)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            status="ready",
            provider=self.config.kind,
            model=self.config.model,
            detail="offline deterministic provider ready",
        )

    def invoke_structured(self, request: StructuredRequest) -> StructuredResponse:
        try:
            output = self._responder(request)
        except Exception as exc:
            raise ProviderInvocationError("deterministic provider invocation failed") from exc
        if not isinstance(output, dict):
            raise ProviderInvocationError("deterministic responder must return an object")
        return StructuredResponse(provider=self.config.kind, model=self.config.model, output=output)


class AnthropicProvider:
    def __init__(
        self, config: ProviderConfig, *, client_factory: ClientFactory | None = None
    ) -> None:
        if config.kind != "anthropic":
            raise ProviderConfigurationError("Anthropic adapter received the wrong provider kind")
        self._config = config
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any | None = None

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(external_egress=True)

    def health(self) -> ProviderHealth:
        try:
            require_external_egress(self.config)
            self._get_client()
        except (EgressDeniedError, ProviderConfigurationError, ProviderUnavailableError) as exc:
            return ProviderHealth(
                status="unavailable",
                provider=self.config.kind,
                model=self.config.model,
                detail=str(exc),
            )
        except Exception:
            return ProviderHealth(
                status="unavailable",
                provider=self.config.kind,
                model=self.config.model,
                detail="provider client could not be initialized",
            )
        return ProviderHealth(
            status="ready",
            provider=self.config.kind,
            model=self.config.model,
            detail="provider client configured; network not probed",
        )

    def invoke_structured(self, request: StructuredRequest) -> StructuredResponse:
        require_external_egress(self.config)
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_output_tokens,
                timeout=self.config.timeout_seconds,
                system=request.system or "Return a valid structured response.",
                output_config={
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": request.response_schema,
                    },
                },
                messages=[message.model_dump(mode="json") for message in request.messages],
            )
            if getattr(response, "stop_reason", None) == "max_tokens":
                raise ProviderOutputTruncatedError("Anthropic model output reached its token limit")
            text = "".join(
                str(getattr(block, "text", ""))
                for block in getattr(response, "content", ())
                if getattr(block, "type", "text") == "text"
            )
            usage = getattr(response, "usage", None)
            provider_usage = ProviderUsage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
            output = parse_json_object(text)
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise invocation_error(exc, "Anthropic") from None
        return StructuredResponse(
            provider=self.config.kind,
            model=self.config.model,
            output=output,
            usage=provider_usage,
            reported_model=_reported_model(response),
        )

    def _get_client(self) -> Any:
        require_api_key(self.config)
        if self._client is None:
            self._client = self._client_factory(self.config)
        return self._client

    @staticmethod
    def _default_client_factory(config: ProviderConfig) -> Any:
        try:
            module = importlib.import_module("anthropic")
        except ImportError as exc:
            raise ProviderUnavailableError(
                "Anthropic provider requires the optional 'anthropic' package"
            ) from exc
        return module.Anthropic(
            api_key=require_api_key(config),
            # Match the endpoint shown in setup; never inherit an ambient SDK URL.
            base_url="https://api.anthropic.com",
            max_retries=0,
            http_client=_provider_http_client(config),
        )


def _wire_schema(schema: dict[str, Any], profile: str) -> dict[str, Any]:
    """Explicit Ollama grammar projection; canonical application validation stays strict."""
    if profile == "standard":
        return schema

    def project(value, named_schemas=False):
        if isinstance(value, dict):
            schema_maps = {"properties", "$defs", "definitions", "patternProperties"}
            return {
                key: project(child, key in schema_maps)
                for key, child in value.items()
                if named_schemas or key not in {"minLength", "maxLength"}
            }
        if isinstance(value, list):
            return [project(child) for child in value]
        return value

    return project(schema)


class OpenAICompatibleProvider:
    """OpenAI Chat Completions adapter for hosted APIs, vLLM, and Ollama."""

    def __init__(
        self, config: ProviderConfig, *, client_factory: ClientFactory | None = None
    ) -> None:
        if config.kind != "openai_compatible":
            raise ProviderConfigurationError(
                "OpenAI-compatible adapter received the wrong provider kind"
            )
        self._config = config
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any | None = None

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(external_egress=not self._is_loopback())

    def health(self) -> ProviderHealth:
        try:
            if not self._is_loopback():
                require_external_egress(self.config)
            self._get_client()
        except (EgressDeniedError, ProviderConfigurationError, ProviderUnavailableError) as exc:
            return ProviderHealth(
                status="unavailable",
                provider=self.config.kind,
                model=self.config.model,
                detail=str(exc),
            )
        except Exception:
            return ProviderHealth(
                status="unavailable",
                provider=self.config.kind,
                model=self.config.model,
                detail="provider client could not be initialized",
            )
        return ProviderHealth(
            status="ready",
            provider=self.config.kind,
            model=self.config.model,
            detail="provider client configured; network not probed",
        )

    def invoke_structured(self, request: StructuredRequest) -> StructuredResponse:
        if not self._is_loopback():
            require_external_egress(self.config)
        client = self._get_client()
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(message.model_dump(mode="json") for message in request.messages)
        try:
            options = {}
            if self.config.schema_profile == "ollama":
                options["temperature"] = 0
            if self.config.reasoning_effort is not None:
                options["reasoning_effort"] = self.config.reasoning_effort
            response = client.chat.completions.create(
                **options,
                model=self.config.model,
                messages=messages,
                max_tokens=self.config.max_output_tokens,
                timeout=self.config.timeout_seconds,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "opsgraph_response",
                        "strict": True,
                        "schema": _wire_schema(request.response_schema, self.config.schema_profile),
                    },
                },
            )
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise ProviderOutputTruncatedError(
                    "OpenAI-compatible model output reached its token limit"
                )
            output = parse_json_object(choice.message.content)
            usage = getattr(response, "usage", None)
            provider_usage = ProviderUsage(
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            )
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise invocation_error(exc, "OpenAI-compatible") from None
        return StructuredResponse(
            provider=self.config.kind,
            model=self.config.model,
            output=output,
            usage=provider_usage,
            reported_model=_reported_model(response),
        )

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self.config)
        return self._client

    def _is_loopback(self) -> bool:
        hostname = urlparse(self.config.base_url or "").hostname
        return hostname in {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _default_client_factory(config: ProviderConfig) -> Any:
        try:
            module = importlib.import_module("openai")
        except ImportError as exc:
            raise ProviderUnavailableError(
                "OpenAI-compatible provider requires the optional 'openai' package"
            ) from exc
        return module.OpenAI(
            # Local OpenAI-compatible servers commonly do not authenticate. The
            # SDK still requires a non-empty value, so use a non-secret marker.
            api_key=(
                config.api_key.get_secret_value()
                if config.api_key is not None
                else "opsgraph-no-key"
            ),
            max_retries=0,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            # Egress approval applies to this endpoint. Redirects and ambient
            # proxy configuration must not silently change its destination.
            http_client=_provider_http_client(config),
        )
