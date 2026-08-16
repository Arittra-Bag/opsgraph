from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Local-first settings. External egress is disabled unless explicitly enabled."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    mode: Literal["sample", "connected"] = Field(default="sample", alias="OPSGRAPH_MODE")
    api_key: str = Field(default="sample-local-key-change-me", alias="OPSGRAPH_API_KEY")
    workspace_id: str = Field(default="sample-workspace", alias="OPSGRAPH_WORKSPACE_ID")
    egress_enabled: bool = Field(default=False, alias="OPSGRAPH_EGRESS_ENABLED")
    model_provider: Literal["deterministic", "anthropic", "openai_compatible"] = Field(
        default="deterministic", alias="OPSGRAPH_MODEL_PROVIDER"
    )
    local_model: str = Field(default="qwen3:8b", alias="OPSGRAPH_LOCAL_MODEL")
    local_model_url: str = Field(
        default="http://127.0.0.1:11434/v1", alias="OPSGRAPH_LOCAL_MODEL_URL"
    )
    anthropic_model: str = Field(default="claude-sonnet-5", alias="OPSGRAPH_ANTHROPIC_MODEL")
    state_path: Path = Field(default=Path(".opsgraph/state.db"), alias="OPSGRAPH_STATE_PATH")
    postgres_secret_ref: str | None = Field(default=None, alias="OPSGRAPH_POSTGRES_SECRET_REF")
    allowed_postgres_secret_refs: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("OPSGRAPH_SOURCE_DSN",), alias="OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS"
    )
    web_root: Path = Path(__file__).resolve().parent / "web"

    @model_validator(mode="after")
    def reject_unsafe_connected_defaults(self) -> "Settings":
        """Keep the convenient sample default from becoming connected-mode auth."""

        if self.mode == "connected" and (
            self.api_key == "sample-local-key-change-me" or len(self.api_key) < 24
        ):
            raise ValueError("connected mode requires OPSGRAPH_API_KEY with at least 24 characters")
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

    @field_validator("allowed_postgres_secret_refs", mode="before")
    @classmethod
    def parse_allowed_postgres_secret_refs(cls, value: object) -> object:
        """Accept a comma-separated deployment allowlist without exposing values."""

        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

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
    return Settings()
