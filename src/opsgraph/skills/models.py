"""Strict, versioned contracts for configurable investigation skills."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_EVIDENCE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class ToolSettings(BaseModel):
    """Per-skill restrictions; omitted values inherit the policy ceiling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rows: int | None = Field(default=None, ge=1, le=1_000)
    timeout_ms: int | None = Field(default=None, ge=100, le=30_000)
    allowed_schemas: tuple[str, ...] | None = None
    allowed_tables: tuple[str, ...] | None = None

    @field_validator("allowed_schemas", "allowed_tables")
    @classmethod
    def validate_names(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is None:
            return None
        if len(values) > 100:
            raise ValueError("tool settings may contain at most 100 names")
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("tool setting names must contain 1-128 characters")
        return tuple(dict.fromkeys(values))


class ToolBinding(BaseModel):
    """Enable a registered tool for one skill with tighter local bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    settings: ToolSettings = Field(default_factory=ToolSettings)


class SkillDefinition(BaseModel):
    """Immutable skill content. Draft/published state belongs to the repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=5, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    origin: Literal["core", "custom"] = "custom"
    purpose: str = Field(min_length=8, max_length=2_000)
    tools: tuple[ToolBinding, ...]
    required_evidence: tuple[str, ...] = ()
    conclusion_classes: tuple[Literal["supported", "possible", "unknown", "contradictory"], ...] = (
        "supported",
        "possible",
        "unknown",
        "contradictory",
    )
    egress: Literal["forbidden", "allowlisted"] = "forbidden"
    risk: Literal["read_only"] = "read_only"

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("skill id must be a lowercase dotted or hyphenated identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("skill version must use semantic version syntax")
        return value

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, values: tuple[ToolBinding, ...]) -> tuple[ToolBinding, ...]:
        names = [binding.tool for binding in values]
        if len(names) != len(set(names)):
            raise ValueError("a tool may be bound only once per skill")
        return values

    @field_validator("required_evidence")
    @classmethod
    def validate_evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _EVIDENCE_IDENTIFIER.fullmatch(value) for value in values):
            raise ValueError("required evidence must use lowercase identifiers")
        return tuple(dict.fromkeys(values))
