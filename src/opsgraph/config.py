import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class StatePathError(ValueError):
    """A safe path or migration error, without exposing private configuration."""


def resolve_state_path(value: Path | str, *, workspace: Path | str | None = None) -> Path:
    """Give metadata, runs, the coordinator and maintenance one database identity.

    Older workspace storage used a literal leading tilde while run storage expanded
    it. Existing files at the old literal location require an explicit migration;
    choosing either database automatically could hide the other one's records.
    """
    try:
        base = Path(workspace if workspace is not None else Path.cwd()).expanduser().resolve()
        configured = Path(value)
        expanded = configured.expanduser()
        canonical = (expanded if expanded.is_absolute() else base / expanded).resolve()
        if not configured.is_absolute() and configured.parts[0].startswith("~"):
            legacy = base / configured
            if legacy.resolve() != canonical and any(
                candidate.exists() or candidate.is_symlink()
                for suffix in ("", "-wal", "-shm", "-journal", ".coordinator")
                for candidate in (Path(str(legacy) + suffix),)
            ):
                raise StatePathError(
                    "Legacy tilde state path detected. Stop OpsGraph and preserve existing files "
                    "at both the literal-tilde and expanded locations. Reconcile metadata and "
                    "run history, then set OPSGRAPH_STATE_PATH to one explicit absolute path. "
                    "No files were moved or merged."
                )
        return canonical
    except StatePathError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, IndexError):
        raise StatePathError(
            "State path could not be resolved. Set OPSGRAPH_STATE_PATH to a valid absolute "
            "database path, or a path relative to the selected workspace."
        ) from None


class Settings(BaseSettings):
    """Local-first settings. External egress is disabled unless explicitly enabled."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

    mode: Literal["sample", "connected"] = Field(default="connected", alias="OPSGRAPH_MODE")
    api_key: str = Field(default="", alias="OPSGRAPH_API_KEY", repr=False)
    workspace_id: str = Field(default="local-workspace", alias="OPSGRAPH_WORKSPACE_ID")
    egress_enabled: bool = Field(default=False, alias="OPSGRAPH_EGRESS_ENABLED")
    model_provider: Literal["deterministic", "anthropic", "openai_compatible"] = Field(
        default="openai_compatible", alias="OPSGRAPH_MODEL_PROVIDER"
    )
    provider_timeout_seconds: float = Field(
        default=30.0, ge=0.1, le=600.0, alias="OPSGRAPH_PROVIDER_TIMEOUT_SECONDS"
    )
    local_model: str = Field(default="qwen3:8b", alias="OPSGRAPH_LOCAL_MODEL")
    local_reasoning_effort: Literal["none", "low", "medium", "high"] | None = Field(
        default=None, alias="OPSGRAPH_LOCAL_REASONING_EFFORT"
    )
    local_schema_profile: Literal["standard", "ollama"] = Field(
        default="standard", alias="OPSGRAPH_LOCAL_SCHEMA_PROFILE"
    )
    local_model_url: str = Field(
        default="http://127.0.0.1:11434/v1", alias="OPSGRAPH_LOCAL_MODEL_URL"
    )
    anthropic_model: str = Field(default="claude-sonnet-5", alias="OPSGRAPH_ANTHROPIC_MODEL")
    state_path: Path = Field(default=Path(".opsgraph/state.db"), alias="OPSGRAPH_STATE_PATH")
    postgres_secret_ref: str | None = Field(default=None, alias="OPSGRAPH_POSTGRES_SECRET_REF")
    allowed_postgres_secret_refs: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("OPSGRAPH_SOURCE_DSN",), alias="OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS"
    )
    postgres_allowed_schemas: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("public",), alias="OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS"
    )
    web_root: Path = Path(__file__).resolve().parent / "web"

    @field_validator("state_path")
    @classmethod
    def canonical_state_path(cls, value: Path) -> Path:
        return resolve_state_path(value)

    @model_validator(mode="after")
    def reject_unsafe_connected_defaults(self) -> "Settings":
        """Keep the convenient sample default from becoming connected-mode auth."""

        if (
            self.api_key
            in {
                "sample-local-key-change-me",
                "replace-with-a-long-random-value",
            }
            or len(self.api_key) < 24
        ):
            raise ValueError(
                "OPSGRAPH_API_KEY requires at least 24 characters; run opsgraph init "
                "in a new configuration directory or replace the legacy key"
            )
        return self

    @field_validator("model_provider", mode="before")
    @classmethod
    def map_legacy_provider(cls, value: object) -> object:
        return {"local": "openai_compatible", "external": "anthropic"}.get(value, value)

    @field_validator("mode", mode="before")
    @classmethod
    def map_demo_mode(cls, value: object) -> object:
        """Treat the legacy demo's offline mode as the alpha's sample mode."""

        return "sample" if value == "offline" else value

    @field_validator("allowed_postgres_secret_refs", "postgres_allowed_schemas", mode="before")
    @classmethod
    def parse_allowed_postgres_secret_refs(cls, value: object) -> object:
        """Accept a comma-separated deployment allowlist without exposing values."""

        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("postgres_allowed_schemas")
    @classmethod
    def validate_postgres_allowed_schemas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 100:
            raise ValueError("configure 1-100 approved PostgreSQL schema names")
        if any(not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", item) for item in value):
            raise ValueError("approved PostgreSQL schemas require simple lowercase identifiers")
        return tuple(dict.fromkeys(value))

    @field_validator("allowed_postgres_secret_refs")
    @classmethod
    def validate_allowed_postgres_secret_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 32:
            raise ValueError("configure 1-32 PostgreSQL secret reference names")
        if any(not item.startswith("OPSGRAPH_") or not item.endswith("_DSN") for item in value):
            raise ValueError("PostgreSQL secret references must use OPSGRAPH_*_DSN names")
        return tuple(dict.fromkeys(value))


@lru_cache
def get_settings() -> Settings:
    # Secret references are resolved from the backend process environment. Match
    # CLI startup when running the ASGI application directly; never override an
    # environment value supplied by the operator or deployment.
    load_dotenv(Path.cwd() / ".env", override=False)
    return Settings()
