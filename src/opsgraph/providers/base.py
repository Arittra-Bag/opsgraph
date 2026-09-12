"""Provider protocol and safe shared enforcement."""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from opsgraph.providers.models import (
    ProviderCapabilities,
    ProviderConfig,
    ProviderHealth,
    StructuredRequest,
    StructuredResponse,
)


class ProviderError(RuntimeError):
    """Base error that must never include credentials."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderInvocationError(ProviderError):
    pass


class ProviderOutputTruncatedError(ProviderInvocationError):
    """The provider stopped at its output-token limit; output is incomplete."""


class ProviderTimeoutError(ProviderInvocationError):
    """A bounded model call expired without a usable structured result."""


class EgressDeniedError(ProviderError):
    pass


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def config(self) -> ProviderConfig: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def health(self) -> ProviderHealth: ...

    def invoke_structured(self, request: StructuredRequest) -> StructuredResponse: ...


def require_external_egress(config: ProviderConfig) -> None:
    if not config.egress_enabled:
        raise EgressDeniedError(f"external egress is disabled for provider {config.kind}")


def require_api_key(config: ProviderConfig) -> str:
    if config.api_key is None or not config.api_key.get_secret_value():
        raise ProviderConfigurationError(f"API key is required for provider {config.kind}")
    return config.api_key.get_secret_value()


def parse_json_object(value: Any) -> dict[str, Any]:
    """Normalize provider text/object output without exposing raw failures."""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ProviderInvocationError("provider returned unsupported structured output")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ProviderInvocationError("provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderInvocationError("provider JSON output must be an object")
    return parsed


def is_timeout_error(exc: Exception) -> bool:
    """Recognize built-in and optional SDK timeout types without exposing their messages."""
    return isinstance(exc, TimeoutError) or (
        type(exc).__name__ == "APITimeoutError"
        and type(exc).__module__.split(".")[0] in {"openai", "anthropic"}
    )


def invocation_error(exc: Exception, provider: str) -> ProviderInvocationError:
    """Classify failures using status metadata only, never provider bodies or URLs."""
    if is_timeout_error(exc):
        return ProviderTimeoutError(f"{provider} model call timed out")
    status = getattr(exc, "status_code", None)
    if status in {401, 403}:
        detail = "access denied; verify the backend API key and model permissions"
    elif status == 404:
        detail = "model or API route not found; verify the model ID and endpoint base URL"
    elif status in {400, 422}:
        detail = (
            "request rejected; verify structured JSON-schema support, model ID "
            "and configured request options"
        )
    elif status == 429:
        detail = "rate or quota limit reached; check provider limits before retrying"
    elif isinstance(status, int) and 500 <= status <= 599:
        detail = "service unavailable; check the model server and retry when healthy"
    elif type(exc).__name__ == "APIConnectionError" and type(exc).__module__.split(".")[0] in {
        "openai",
        "anthropic",
    }:
        detail = "connection failed; verify the endpoint, server availability and TLS setup"
    else:
        detail = "invocation failed; verify the model server and configuration"
    return ProviderInvocationError(f"{provider} {detail}")
