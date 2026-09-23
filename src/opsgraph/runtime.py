"""Application assembly for the local OpsGraph control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from threading import RLock
from uuid import uuid4

from pydantic import SecretStr

from opsgraph.audit import SQLiteAuditChain
from opsgraph.config import Settings, get_settings
from opsgraph.domain import Obligation, ToolDefinition, ToolRegistry
from opsgraph.persistence import SQLiteWorkspaceStore
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator
from opsgraph.providers import ModelProvider, ProviderConfig, create_provider
from opsgraph.schema_service import PostgresSchemaParser
from opsgraph.setup import SetupError
from opsgraph.skills import SkillDefinition, SkillpackLoader, SkillRepository

STRICT_READ_ONLY_OBLIGATIONS = Obligation(
    max_rows=100,
    timeout_ms=5_000,
    allowed_schemas=("public",),
)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    policy: FailClosedPolicy
    schema_parser: PostgresSchemaParser
    audit: SQLiteAuditChain
    store: SQLiteWorkspaceStore
    tools: ToolRegistry
    skills: SkillRepository
    provider: ModelProvider
    provider_lock: object = field(default_factory=RLock)
    provider_revision: str = field(default_factory=lambda: uuid4().hex)
    skill_lock: object = field(default_factory=RLock)


def _provider(settings: Settings, audit: SQLiteAuditChain | None = None) -> ModelProvider:
    if settings.model_provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        config = ProviderConfig(
            kind="anthropic",
            provider_preset="anthropic",
            model=settings.anthropic_model,
            api_key=SecretStr(key) if key else None,
            egress_enabled=settings.egress_enabled,
            max_output_tokens=1_200,
            timeout_seconds=settings.provider_timeout_seconds,
        )
    elif settings.model_provider == "openai_compatible":
        # An explicitly empty dedicated key means this endpoint needs no key.
        # Do not forward an unrelated ambient OpenAI credential to that endpoint.
        key = os.getenv("OPSGRAPH_OPENAI_API_KEY")
        if key is None:
            key = os.getenv("OPENAI_API_KEY")
        config = ProviderConfig(
            kind="openai_compatible",
            provider_preset="custom_openai",
            model=settings.local_model,
            api_key=SecretStr(key) if key else None,
            base_url=settings.local_model_url,
            reasoning_effort=settings.local_reasoning_effort,
            schema_profile=settings.local_schema_profile,
            egress_enabled=settings.egress_enabled,
            max_output_tokens=1_200,
            timeout_seconds=settings.provider_timeout_seconds,
        )
    else:
        config = ProviderConfig(
            kind="deterministic",
            model="opsgraph-replay-v1",
            egress_enabled=False,
        )
    from opsgraph.provider_settings import load_provider_config

    config = load_provider_config(settings, config, audit=audit)
    settings.model_provider = config.kind
    return create_provider(config)


def _load_persisted_skills(
    skills: SkillRepository,
    records,
    audit: SQLiteAuditChain,
    *,
    workspace_id: str,
) -> None:
    """Restore immutable versions and their explicitly recorded active pointers."""

    published: dict[tuple[str, str], tuple[SkillDefinition, str]] = {}
    current: dict[str, tuple[str, str]] = {}
    drafts: list[SkillDefinition] = []
    for record in records:
        record_type = record.value.get("record_type")
        if record_type == "skill_published":
            skill = SkillDefinition.model_validate(record.value["definition"])
            expected_id = f"skill-published:{skill.id}:{skill.version}"
            if record.record_id != expected_id or (skill.id, skill.version) in published:
                raise SetupError("Persisted skill publication metadata is inconsistent.")
            published[(skill.id, skill.version)] = (skill, record.record_id)
        elif record_type == "skill_current":
            skill_id = str(record.value.get("skill_id", ""))
            version = str(record.value.get("version", ""))
            published_record_id = str(record.value.get("published_record_id", ""))
            if record.record_id != f"skill-current:{skill_id}" or skill_id in current:
                raise SetupError("Persisted active skill metadata is inconsistent.")
            current[skill_id] = (version, published_record_id)
        elif record_type == "skill_draft":
            drafts.append(SkillDefinition.model_validate(record.value["definition"]))

    receipts: dict[str, list[str]] = {}
    for entry in audit.entries:
        if (
            entry.workspace_id == workspace_id
            and entry.action == "core.skill.manage"
            and entry.outcome == "allowed"
            and entry.details.get("operation") == "publish"
            and isinstance(entry.details.get("version"), str)
        ):
            receipts.setdefault(entry.resource, []).append(entry.details["version"])

    by_skill: dict[str, list[SkillDefinition]] = {}
    for (skill_id, _), (skill, _) in published.items():
        by_skill.setdefault(skill_id, []).append(skill)
    for skill_id, definitions in sorted(by_skill.items()):
        versions = {skill.version for skill in definitions}
        audited = [version for version in receipts.get(skill_id, ()) if version in versions]
        if any(version not in audited for version in versions):
            raise SetupError("Persisted skill publication has no matching audit record.")
        pointer = current.get(skill_id)
        if pointer is None:
            if not audited:
                raise SetupError("Persisted active skill version cannot be established safely.")
            active_version = audited[-1]
        else:
            active_version, published_record_id = pointer
            target = published.get((skill_id, active_version))
            if target is None or target[1] != published_record_id or active_version not in audited:
                raise SetupError("Persisted active skill metadata is inconsistent.")
        for skill in sorted(definitions, key=lambda value: value.version):
            skills.save_draft(skill)
            skills.publish(skill.id)
        skills.activate(skill_id, active_version)

    if set(current) - set(by_skill):
        raise SetupError("Persisted active skill metadata points to a missing publication.")
    for draft in drafts:
        skills.save_draft(draft)


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    obligations = STRICT_READ_ONLY_OBLIGATIONS.model_copy(
        update={"allowed_schemas": settings.postgres_allowed_schemas}
    )
    schema_parser = PostgresSchemaParser()
    tools = ToolRegistry(schema_parser.inspect)
    tools.register(
        ToolDefinition(
            name="core.sql.select",
            description="Run one policy-bounded SELECT query against an approved source.",
            handler=lambda **_: None,
        )
    )
    store = SQLiteWorkspaceStore(settings.state_path)
    audit = SQLiteAuditChain(settings.state_path)
    audit.require_valid()
    skills = SkillRepository(tools=tools, policy_ceiling=obligations)
    loader = SkillpackLoader()
    skill_root = settings.web_root.parent / "skillpacks"
    if skill_root.is_dir():
        for skill in loader.load_all(skill_root):
            skills.save_draft(skill)
            skills.publish(skill.id)
    saved = store.list(workspace_id=settings.workspace_id)
    _load_persisted_skills(
        skills,
        saved,
        audit,
        workspace_id=settings.workspace_id,
    )
    policy = FailClosedPolicy(
        StaticPolicyEvaluator(
            {
                ("analyst", "core.investigation.connected"): obligations,
                ("analyst", "core.schema.inspect"): obligations,
                ("analyst", "core.query.validate"): obligations,
                ("analyst", "core.query.read"): obligations,
                ("analyst", "core.source.manage"): obligations,
                ("analyst", "core.source.readiness"): obligations,
                ("analyst", "core.skill.manage"): obligations,
                ("analyst", "core.provider.manage"): obligations,
                ("analyst", "core.provider.test"): obligations,
            }
        )
    )
    return Runtime(
        settings=settings,
        policy=policy,
        schema_parser=schema_parser,
        audit=audit,
        store=store,
        tools=tools,
        skills=skills,
        provider=_provider(settings, audit),
    )


@lru_cache
def get_runtime() -> Runtime:
    return build_runtime()
