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
    ProviderUnavailableError,
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
            raise ProviderInvocationError("Anthropic provider invocation failed") from exc
        return StructuredResponse(
            provider=self.config.kind,
            model=self.config.model,
            output=output,
            usage=provider_usage,
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
        return module.Anthropic(api_key=require_api_key(config))


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
            response = client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                max_tokens=self.config.max_output_tokens,
                timeout=self.config.timeout_seconds,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "opsgraph_response",
                        "strict": True,
                        "schema": request.response_schema,
                    },
                },
            )
            choice = response.choices[0]
            output = parse_json_object(choice.message.content)
            usage = getattr(response, "usage", None)
            provider_usage = ProviderUsage(
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            )
        except ProviderInvocationError:
            raise
        except Exception as exc:
            raise ProviderInvocationError("OpenAI-compatible provider invocation failed") from exc
        return StructuredResponse(
            provider=self.config.kind,
            model=self.config.model,
            output=output,
            usage=provider_usage,
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
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )
