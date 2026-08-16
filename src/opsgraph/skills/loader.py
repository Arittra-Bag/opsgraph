"""Strict loader for the repository's split YAML skillpack format."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import SkillDefinition, ToolBinding


class SkillpackLoadError(ValueError):
    """A skillpack is missing, malformed, or contains undeclared fields."""


class SkillpackLoader:
    _MANIFEST_FIELDS = {"id", "version", "name", "origin", "capabilities", "egress", "risk"}
    _SKILL_FIELDS = {"purpose", "required_evidence", "conclusion_classes"}

    def load(self, directory: str | Path) -> SkillDefinition:
        root = Path(directory)
        manifest = self._read_mapping(root / "manifest.yaml")
        behavior = self._read_mapping(root / "skill.yaml")
        self._reject_unknown(manifest, self._MANIFEST_FIELDS, "manifest")
        self._reject_unknown(behavior, self._SKILL_FIELDS, "skill")

        capabilities = manifest.pop("capabilities", None)
        if not isinstance(capabilities, list) or not capabilities:
            raise SkillpackLoadError("manifest capabilities must be a non-empty list")
        if any(not isinstance(item, str) for item in capabilities):
            raise SkillpackLoadError("manifest capabilities must contain strings")

        try:
            return SkillDefinition(
                **manifest,
                **behavior,
                tools=tuple(ToolBinding(tool=capability) for capability in capabilities),
            )
        except (TypeError, ValidationError) as exc:
            raise SkillpackLoadError(f"invalid skillpack {root.name}: {exc}") from exc

    def load_all(self, root: str | Path) -> tuple[SkillDefinition, ...]:
        path = Path(root)
        if not path.is_dir():
            raise SkillpackLoadError(f"skillpack root is not a directory: {path}")
        return tuple(
            self.load(directory) for directory in sorted(path.iterdir()) if directory.is_dir()
        )

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, Any]:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SkillpackLoadError(f"could not read {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SkillpackLoadError(f"{path.name} must contain a mapping")
        return dict(payload)

    @staticmethod
    def _reject_unknown(payload: dict[str, Any], allowed: set[str], label: str) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise SkillpackLoadError(f"unknown {label} fields: {', '.join(unknown)}")
