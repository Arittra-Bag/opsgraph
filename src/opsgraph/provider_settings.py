"""Private persistent model configuration, separate from investigation evidence."""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from opsgraph.domain.models import stable_hash
from opsgraph.providers import ProviderConfig
from opsgraph.providers.models import (
    FIXED_HOSTED_PRESETS,
    PROVIDER_DEFAULT_ENDPOINTS,
    ProviderPreset,
)
from opsgraph.setup import SetupError, _loopback_url, _model_name, _model_url, read_private_config

RequestProvider = ProviderPreset | Literal["openai_compatible"]

DEFAULT_ENDPOINTS = PROVIDER_DEFAULT_ENDPOINTS


class ProviderSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: RequestProvider
    model: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    clear_api_key: bool = False
    schema_profile: Literal["standard", "ollama"] | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    timeout_seconds: float = Field(default=300, ge=0.1, le=600)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32_768)
    allow_external_egress: bool = False


def settings_path(settings):
    return settings.state_path.parent / (settings.state_path.name + ".provider") / "settings.env"


def pending_settings_path(settings):
    return settings_path(settings).with_name("settings.pending.env")


def provider_audit_fingerprint(config: ProviderConfig, *, binding_key: str) -> str:
    """Bind an audit receipt to settings without disclosing the credential."""

    if not binding_key:
        raise ValueError("Provider audit fingerprints require a local binding key.")
    credential = config.api_key.get_secret_value() if config.api_key else ""
    credential_binding = hmac.new(
        binding_key.encode("utf-8"),
        b"opsgraph-provider-credential-v1\x00" + credential.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return stable_hash(
        {
            "configuration": config.model_dump(mode="json", exclude={"api_key"}),
            "credential_binding": credential_binding,
        }
    )


def _matching_audit(
    entries,
    config: ProviderConfig,
    revision: str,
    *,
    workspace_id: str,
    binding_key: str,
) -> bool:
    expected_fingerprint = provider_audit_fingerprint(config, binding_key=binding_key)
    for entry in reversed(entries):
        recorded_fingerprint = entry.details.get("configuration_fingerprint")
        if (
            entry.workspace_id == workspace_id
            and entry.action == "core.provider.manage"
            and entry.resource == "current-provider"
            and entry.outcome == "allowed"
            and entry.details.get("reason") == "configuration_saved"
            and entry.details.get("preset") == config.provider_preset
            and entry.details.get("adapter") == config.kind
            and entry.details.get("external_egress") == config.egress_enabled
            and entry.details.get("revision") == revision
            and isinstance(recorded_fingerprint, str)
            and hmac.compare_digest(recorded_fingerprint, expected_fingerprint)
        ):
            return True
    return False


def load_provider_config(settings, fallback, *, audit=None):
    path = settings_path(settings)
    if not path.parent.exists():
        return fallback
    pending = pending_settings_path(settings)
    try:
        pending.unlink(missing_ok=True)
    except OSError:
        raise SetupError("Incomplete model configuration could not be removed safely.") from None
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
            }
        )
        revision = values.get("PROVIDER_REVISION", "").strip()
        if not revision:
            raise SetupError(
                "Saved model configuration is missing its audit revision. "
                "Restore its private backup or save it again."
            )
        if audit is None or not _matching_audit(
            audit.entries,
            config,
            revision,
            workspace_id=settings.workspace_id,
            binding_key=settings.api_key,
        ):
            raise SetupError(
                "Saved model configuration has no matching audit record. "
                "Restore its private backup or save it again."
            )
        config = config.model_copy(
            update={
                "egress_enabled": config.egress_enabled and settings.egress_enabled,
            }
        )
        return config
    except SetupError:
        raise
    except (ValueError, KeyError, TypeError):
        raise SetupError(
            "Saved model configuration is invalid. Restore its private backup."
        ) from None


def _preset(value: RequestProvider) -> ProviderPreset:
    # Keep the pre-preset API spelling as a compatibility alias. It has no
    # independent identity once persisted.
    return "custom_openai" if value == "openai_compatible" else value


def _endpoint(body, previous, preset: ProviderPreset) -> tuple[str | None, bool]:
    if preset == "anthropic":
        submitted = (body.endpoint or "").rstrip("/")
        if submitted not in ("", "https://api.anthropic.com"):
            raise SetupError("Anthropic uses its official endpoint; leave endpoint empty.")
        return None, True

    default = DEFAULT_ENDPOINTS[preset]
    submitted = (body.endpoint or "").rstrip("/")
    if preset in FIXED_HOSTED_PRESETS:
        if submitted and submitted != default:
            raise SetupError(
                f"{preset.replace('_', ' ').title()} uses its official endpoint; "
                "leave endpoint empty."
            )
        candidate = default or ""
    elif preset == "custom_openai":
        if not submitted:
            raise SetupError("Custom OpenAI-compatible providers require an explicit endpoint.")
        candidate = submitted
    else:
        candidate = submitted or default or ""

    # Preserve an explicitly deployed HTTP endpoint (for example a private
    # container network). Browser edits cannot introduce a new remote HTTP host.
    if candidate == previous.base_url and candidate.startswith("http://"):
        _model_url("https://" + candidate[len("http://") :], allow_remote=True)
        endpoint = candidate
    else:
        endpoint = _model_url(candidate, allow_remote=True)
    return endpoint, not _loopback_url(endpoint)


def make_config(body, previous, ceiling):
    model = _model_name(body.model.strip())
    preset = _preset(body.provider)
    endpoint, remote = _endpoint(body, previous, preset)
    if remote and not body.allow_external_egress:
        raise SetupError("Explicitly allow external model processing before saving this provider.")
    if remote and not ceiling:
        raise PermissionError("External model processing is disabled by deployment configuration.")
    key = body.api_key.get_secret_value()
    if len(key) > 8192 or any(ord(c) < 32 for c in key):
        raise SetupError("API key must contain at most 8192 characters and no control characters.")
    kind = "anthropic" if preset == "anthropic" else "openai_compatible"
    same_destination = kind == previous.kind and endpoint == previous.base_url
    if not key and same_destination and not body.clear_api_key:
        key = previous.api_key.get_secret_value() if previous.api_key else ""
    if body.clear_api_key:
        key = ""
    return ProviderConfig(
        kind=kind,
        provider_preset=preset,
        model=model,
        base_url=endpoint,
        api_key=SecretStr(key) if key else None,
        schema_profile=(
            "standard"
            if kind == "anthropic"
            else (body.schema_profile or ("ollama" if preset == "ollama" else "standard"))
        ),
        reasoning_effort=None if kind == "anthropic" else body.reasoning_effort,
        timeout_seconds=body.timeout_seconds,
        egress_enabled=remote and body.allow_external_egress and ceiling,
        max_output_tokens=(
            body.max_output_tokens
            if body.max_output_tokens is not None
            else previous.max_output_tokens
        ),
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
    legacy_provider = config.kind if config.kind != "openai_compatible" else "custom_openai"
    return {
        "provider": config.provider_preset or legacy_provider,
        "adapter": config.kind,
        "model": config.model,
        "endpoint": endpoint,
        "schema_profile": config.schema_profile,
        "reasoning_effort": config.reasoning_effort,
        "timeout_seconds": config.timeout_seconds,
        "max_output_tokens": config.max_output_tokens,
        "allow_external_egress": config.egress_enabled,
        "deployment_egress_enabled": ceiling,
        "api_key_configured": bool(config.api_key and config.api_key.get_secret_value()),
        "revision": revision,
    }
