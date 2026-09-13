"""Private persistent model configuration, separate from investigation evidence."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from opsgraph.providers import ProviderConfig
from opsgraph.setup import SetupError, _loopback_url, _model_name, _model_url, read_private_config


class ProviderSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["ollama", "openai_compatible", "anthropic"]
    model: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    clear_api_key: bool = False
    schema_profile: Literal["standard", "ollama"] = "standard"
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    timeout_seconds: float = Field(default=300, ge=0.1, le=600)
    allow_external_egress: bool = False


def settings_path(settings):
    return settings.state_path.parent / (settings.state_path.name + ".provider") / "settings.env"


def load_provider_config(settings, fallback):
    path = settings_path(settings)
    if not path.parent.exists():
        return fallback
    values = read_private_config(path)
    if not values:
        return fallback
    try:
        config = ProviderConfig.model_validate_json(values["PROVIDER_CONFIG"])
        config = config.model_copy(
            update={
                "api_key": SecretStr(values["PROVIDER_KEY"])
                if values.get("PROVIDER_KEY")
                else None,
                "egress_enabled": config.egress_enabled and settings.egress_enabled,
            }
        )
        return config
    except (ValueError, KeyError, TypeError):
        raise SetupError(
            "Saved model configuration is invalid. Restore its private backup."
        ) from None


def make_config(body, previous, ceiling):
    model = _model_name(body.model.strip())
    if body.provider == "anthropic":
        if body.endpoint not in (None, "", "https://api.anthropic.com"):
            raise SetupError("Anthropic uses its official endpoint; leave endpoint empty.")
        endpoint = None
        remote = True
    else:
        candidate = body.endpoint or ""
        # Preserve an explicitly deployed HTTP endpoint (for example a private
        # container network). Browser edits cannot introduce a new remote HTTP host.
        if candidate == previous.base_url and candidate.startswith("http://"):
            _model_url("https://" + candidate[len("http://") :], allow_remote=True)
            endpoint = candidate.rstrip("/")
        else:
            endpoint = _model_url(candidate, allow_remote=True)
        remote = not _loopback_url(endpoint)
    if remote and not body.allow_external_egress:
        raise SetupError("Explicitly allow external model processing before saving this provider.")
    if remote and not ceiling:
        raise PermissionError("External model processing is disabled by deployment configuration.")
    key = body.api_key.get_secret_value()
    if len(key) > 8192 or any(ord(c) < 32 for c in key):
        raise SetupError("API key must contain at most 8192 characters and no control characters.")
    kind = "anthropic" if body.provider == "anthropic" else "openai_compatible"
    same_destination = kind == previous.kind and endpoint == previous.base_url
    if not key and same_destination and not body.clear_api_key:
        key = previous.api_key.get_secret_value() if previous.api_key else ""
    if body.clear_api_key:
        key = ""
    return ProviderConfig(
        kind=kind,
        model=model,
        base_url=endpoint,
        api_key=SecretStr(key) if key else None,
        schema_profile="standard"
        if kind == "anthropic"
        else ("ollama" if body.provider == "ollama" else body.schema_profile),
        reasoning_effort=None if kind == "anthropic" else body.reasoning_effort,
        timeout_seconds=body.timeout_seconds,
        egress_enabled=remote and body.allow_external_egress and ceiling,
        max_output_tokens=previous.max_output_tokens,
    )


def public_config(config, ceiling, revision):
    endpoint = config.base_url or ""
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            endpoint = ""
    except ValueError:
        endpoint = ""
    return {
        "provider": "ollama" if config.schema_profile == "ollama" else config.kind,
        "model": config.model,
        "endpoint": endpoint,
        "schema_profile": config.schema_profile,
        "reasoning_effort": config.reasoning_effort,
        "timeout_seconds": config.timeout_seconds,
        "allow_external_egress": config.egress_enabled,
        "deployment_egress_enabled": ceiling,
        "api_key_configured": bool(config.api_key and config.api_key.get_secret_value()),
        "revision": revision,
    }
