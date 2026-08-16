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
