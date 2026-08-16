"""Typed contracts for model providers.

Provider configuration is deliberately independent from application settings so
deployments can construct it from environment variables, a secret store, or a
workspace-scoped configuration record without leaking credentials.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ProviderKind = Literal["deterministic", "anthropic", "openai_compatible"]


class ProviderConfig(BaseModel):
    """Validated provider configuration with secret-safe representation."""

    model_config = ConfigDict(frozen=True)

    kind: ProviderKind
    model: str = Field(min_length=1, max_length=256)
    allowed_models: tuple[str, ...] = ()
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, max_length=2_048)
    egress_enabled: bool = False
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=120.0)
    max_output_tokens: int = Field(default=1_024, ge=1, le=32_768)

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 100:
            raise ValueError("model allowlist may contain at most 100 models")
        if any(not value or len(value) > 256 for value in values):
            raise ValueError("allowlisted model names must contain 1-256 characters")
        return tuple(dict.fromkeys(values))

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        return normalized

    @model_validator(mode="after")
    def validate_provider_shape(self) -> ProviderConfig:
        if self.allowed_models and self.model not in self.allowed_models:
            raise ValueError("configured model is not in the model allowlist")
        if self.kind == "anthropic" and self.base_url is not None:
            raise ValueError("Anthropic provider does not accept a custom base_url")
        if self.kind == "openai_compatible" and self.base_url is None:
            raise ValueError("OpenAI-compatible provider requires base_url")
        return self


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    structured_output: bool = True
    tool_calling: bool = False
    streaming: bool = False
    external_egress: bool


class ProviderHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "misconfigured", "unavailable"]
    provider: ProviderKind
    model: str
    detail: str = Field(min_length=1, max_length=500)


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000)


class StructuredRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...] = Field(min_length=1, max_length=200)
    response_schema: dict[str, Any]
    system: str | None = Field(default=None, max_length=100_000)

    @field_validator("response_schema")
    @classmethod
    def require_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("response_schema must describe a JSON object")
        return value


class ProviderUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class StructuredResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: ProviderKind
    model: str
    output: dict[str, Any]
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
