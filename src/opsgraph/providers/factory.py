"""Provider construction kept separate from API and workspace configuration."""

from __future__ import annotations

from typing import Any

from opsgraph.providers.adapters import (
    AnthropicProvider,
    DeterministicProvider,
    OpenAICompatibleProvider,
)
from opsgraph.providers.base import ModelProvider, ProviderConfigurationError
from opsgraph.providers.models import ProviderConfig


def create_provider(
    config: ProviderConfig,
    *,
    client_factory: Any | None = None,
    deterministic_responder: Any | None = None,
) -> ModelProvider:
    """Create an adapter without importing optional SDKs until it is used."""

    if config.kind == "deterministic":
        return DeterministicProvider(config, responder=deterministic_responder)
    if config.kind == "anthropic":
        return AnthropicProvider(config, client_factory=client_factory)
    if config.kind == "openai_compatible":
        return OpenAICompatibleProvider(config, client_factory=client_factory)
    raise ProviderConfigurationError(f"unsupported provider kind: {config.kind}")
