"""Validated in-process skill drafts and immutable published versions."""

from __future__ import annotations

from opsgraph.domain import Obligation, ToolRegistry

from .models import SkillDefinition, ToolBinding, ToolSettings


class SkillValidationError(ValueError):
    """A skill requests unavailable or policy-incompatible capabilities."""


class SkillRepository:
    """Runtime repository for validated drafts and immutable published versions.

    Persistence is deliberately left to the caller. Objects are immutable, so a
    published definition cannot be changed through a retained reference.
    """

    def __init__(self, *, tools: ToolRegistry, policy_ceiling: Obligation) -> None:
        self._tools = tools
        self._policy_ceiling = policy_ceiling
        self._drafts: dict[str, SkillDefinition] = {}
        self._published: dict[tuple[str, str], SkillDefinition] = {}
        self._publication_order: dict[str, list[str]] = {}

    def save_draft(self, skill: SkillDefinition) -> SkillDefinition:
        self.validate(skill)
        if (skill.id, skill.version) in self._published:
            raise SkillValidationError("a published skill version is immutable")
        self._drafts[skill.id] = skill
        return skill

    def get_draft(self, skill_id: str) -> SkillDefinition:
        try:
            return self._drafts[skill_id]
        except KeyError as exc:
            raise KeyError(f"unknown skill draft: {skill_id}") from exc

    def publish(self, skill_id: str) -> SkillDefinition:
        skill = self.get_draft(skill_id)
        self.validate(skill)
        key = (skill.id, skill.version)
        if key in self._published:
            raise SkillValidationError("skill version is already published")
        self._published[key] = skill
        self._publication_order.setdefault(skill.id, []).append(skill.version)
        del self._drafts[skill.id]
        return skill

    def get_published(self, skill_id: str, version: str | None = None) -> SkillDefinition:
        if version is None:
            versions = self._publication_order.get(skill_id, [])
            if not versions:
                raise KeyError(f"unknown published skill: {skill_id}")
            version = versions[-1]
        try:
            return self._published[(skill_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown published skill version: {skill_id}@{version}") from exc

    def versions(self, skill_id: str) -> tuple[str, ...]:
        return tuple(self._publication_order.get(skill_id, ()))

    def list_published(self) -> tuple[SkillDefinition, ...]:
        return tuple(
            self._published[(skill_id, versions[-1])]
            for skill_id, versions in sorted(self._publication_order.items())
            if versions
        )

    def validate(self, skill: SkillDefinition) -> None:
        registered = self._tools.tools
        bindings = {binding.tool: binding for binding in skill.tools}
        unknown = sorted(set(bindings) - set(registered))
        if unknown:
            raise SkillValidationError(f"unknown tools: {', '.join(unknown)}")

        native = sorted(name for name, tool in registered.items() if tool.native)
        missing_native = [name for name in native if name not in bindings]
        disabled_native = [
            name for name in native if name in bindings and not bindings[name].enabled
        ]
        if missing_native:
            raise SkillValidationError(
                f"native tools cannot be removed: {', '.join(missing_native)}"
            )
        if disabled_native:
            raise SkillValidationError(
                f"native tools cannot be disabled: {', '.join(disabled_native)}"
            )

        for binding in skill.tools:
            self._validate_settings(binding)

    def _validate_settings(self, binding: ToolBinding) -> None:
        settings = binding.settings
        ceiling = self._policy_ceiling
        if settings.max_rows is not None and settings.max_rows > ceiling.max_rows:
            raise SkillValidationError(f"{binding.tool} max_rows exceeds policy ceiling")
        if settings.timeout_ms is not None and settings.timeout_ms > ceiling.timeout_ms:
            raise SkillValidationError(f"{binding.tool} timeout_ms exceeds policy ceiling")
        self._require_subset(binding.tool, "allowed_schemas", settings, ceiling)
        if ceiling.allowed_tables:
            self._require_subset(binding.tool, "allowed_tables", settings, ceiling)

    @staticmethod
    def _require_subset(tool: str, field: str, settings: ToolSettings, ceiling: Obligation) -> None:
        requested = getattr(settings, field)
        if requested is None:
            return
        permitted = set(getattr(ceiling, field))
        outside = sorted(set(requested) - permitted)
        if outside:
            raise SkillValidationError(
                f"{tool} {field} exceeds policy ceiling: {', '.join(outside)}"
            )
