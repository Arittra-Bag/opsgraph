from pathlib import Path

import pytest
from pydantic import ValidationError

from opsgraph.domain import CORE_SCHEMA_INSPECT, Obligation, ToolDefinition, ToolRegistry
from opsgraph.skills import (
    SkillDefinition,
    SkillpackLoader,
    SkillpackLoadError,
    SkillRepository,
    SkillValidationError,
    ToolBinding,
    ToolSettings,
)

ROOT = Path(__file__).resolve().parents[2]


def registry() -> ToolRegistry:
    tools = ToolRegistry(lambda ddl: ddl)
    tools.register(ToolDefinition("core.sql.select", "Bounded SELECT", lambda sql: sql))
    return tools


def definition(*, version: str = "0.1.0", tools: tuple[ToolBinding, ...] | None = None):
    return SkillDefinition(
        id="custom-incidents",
        version=version,
        name="Custom incidents",
        purpose="Investigate incidents with locally bounded evidence.",
        tools=tools
        or (
            ToolBinding(tool=CORE_SCHEMA_INSPECT),
            ToolBinding(
                tool="core.sql.select",
                settings=ToolSettings(
                    max_rows=25,
                    timeout_ms=1_000,
                    allowed_schemas=("public",),
                    allowed_tables=("incidents",),
                ),
            ),
        ),
    )


def repository() -> SkillRepository:
    return SkillRepository(
        tools=registry(),
        policy_ceiling=Obligation(
            max_rows=100,
            timeout_ms=5_000,
            allowed_schemas=("public",),
            allowed_tables=("incidents", "events"),
        ),
    )


def test_existing_skillpacks_load_and_validate() -> None:
    repo = repository()
    skills = SkillpackLoader().load_all(ROOT / "skillpacks")
    assert {skill.id for skill in skills} == {
        "failed-jobs",
        "incident-correlation",
        "schema-impact",
    }
    for skill in skills:
        repo.validate(skill)


def test_unknown_yaml_fields_fail_closed(tmp_path: Path) -> None:
    pack = tmp_path / "bad"
    pack.mkdir()
    (pack / "manifest.yaml").write_text(
        "id: bad\nversion: 0.1.0\nname: Bad\norigin: custom\n"
        "capabilities: [core.schema.inspect]\negress: forbidden\nrisk: read_only\n"
        "prompt_override: unsafe\n",
        encoding="utf-8",
    )
    (pack / "skill.yaml").write_text(
        "purpose: A sufficiently long purpose.\nrequired_evidence: []\n"
        "conclusion_classes: [unknown]\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillpackLoadError, match="unknown manifest fields"):
        SkillpackLoader().load(pack)


def test_unknown_tool_and_native_removal_are_rejected() -> None:
    repo = repository()
    with pytest.raises(SkillValidationError, match="unknown tools"):
        repo.save_draft(
            definition(
                tools=(
                    ToolBinding(tool=CORE_SCHEMA_INSPECT),
                    ToolBinding(tool="extension.not-installed"),
                )
            )
        )
    with pytest.raises(SkillValidationError, match="cannot be removed"):
        repo.save_draft(definition(tools=(ToolBinding(tool="core.sql.select"),)))
    with pytest.raises(SkillValidationError, match="cannot be disabled"):
        repo.save_draft(
            definition(
                tools=(
                    ToolBinding(tool=CORE_SCHEMA_INSPECT, enabled=False),
                    ToolBinding(tool="core.sql.select"),
                )
            )
        )


@pytest.mark.parametrize(
    "settings, message",
    [
        (ToolSettings(max_rows=101), "max_rows"),
        (ToolSettings(timeout_ms=5_001), "timeout_ms"),
        (ToolSettings(allowed_schemas=("private",)), "allowed_schemas"),
        (ToolSettings(allowed_tables=("secrets",)), "allowed_tables"),
    ],
)
def test_tool_settings_cannot_exceed_policy(settings: ToolSettings, message: str) -> None:
    repo = repository()
    skill = definition(
        tools=(
            ToolBinding(tool=CORE_SCHEMA_INSPECT),
            ToolBinding(tool="core.sql.select", settings=settings),
        )
    )
    with pytest.raises(SkillValidationError, match=message):
        repo.save_draft(skill)


def test_disabled_tool_settings_are_still_policy_bounded() -> None:
    repo = repository()
    skill = definition(
        tools=(
            ToolBinding(tool=CORE_SCHEMA_INSPECT),
            ToolBinding(
                tool="core.sql.select",
                enabled=False,
                settings=ToolSettings(max_rows=101),
            ),
        )
    )
    with pytest.raises(SkillValidationError, match="max_rows"):
        repo.save_draft(skill)


def test_published_versions_are_immutable_and_versioned() -> None:
    repo = repository()
    first = repo.save_draft(definition())
    assert repo.publish(first.id) == first
    with pytest.raises(SkillValidationError, match="immutable"):
        repo.save_draft(first)

    second = repo.save_draft(definition(version="0.2.0"))
    repo.publish(second.id)
    assert repo.versions(first.id) == ("0.1.0", "0.2.0")
    assert repo.get_published(first.id) == second
    assert repo.get_published(first.id, "0.1.0") == first


def test_models_reject_unknown_fields_and_duplicate_tools() -> None:
    with pytest.raises(ValidationError):
        ToolSettings(max_rows=5, arbitrary=True)
    with pytest.raises(ValidationError, match="bound only once"):
        definition(
            tools=(
                ToolBinding(tool=CORE_SCHEMA_INSPECT),
                ToolBinding(tool=CORE_SCHEMA_INSPECT),
            )
        )
