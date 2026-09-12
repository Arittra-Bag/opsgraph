"""Application assembly for the local OpsGraph control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from pydantic import SecretStr

from opsgraph.audit import AuditChain, SQLiteAuditChain
from opsgraph.config import Settings, get_settings
from opsgraph.domain import Obligation, ToolDefinition, ToolRegistry
from opsgraph.persistence import SQLiteWorkspaceStore
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator
from opsgraph.providers import ModelProvider, ProviderConfig, create_provider
from opsgraph.schema_service import PostgresSchemaParser
from opsgraph.skills import SkillDefinition, SkillpackLoader, SkillRepository

ALPHA_OBLIGATIONS = Obligation(max_rows=100, timeout_ms=5_000, allowed_schemas=("public",))


@dataclass(slots=True)
class Runtime:
    settings: Settings
    policy: FailClosedPolicy
    schema_parser: PostgresSchemaParser
    audit: AuditChain
    store: SQLiteWorkspaceStore
    tools: ToolRegistry
    skills: SkillRepository
    provider: ModelProvider


def _provider(settings: Settings) -> ModelProvider:
    if settings.model_provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        config = ProviderConfig(
            kind="anthropic",
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
    return create_provider(config)


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    obligations = ALPHA_OBLIGATIONS.model_copy(
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
    skills = SkillRepository(tools=tools, policy_ceiling=obligations)
    loader = SkillpackLoader()
    skill_root = settings.web_root.parent / "skillpacks"
    if skill_root.is_dir():
        for skill in loader.load_all(skill_root):
            skills.save_draft(skill)
            skills.publish(skill.id)
    saved = store.list(workspace_id=settings.workspace_id)
    for record in saved:
        if record.value.get("record_type") != "skill_published":
            continue
        skill = SkillDefinition.model_validate(record.value["definition"])
        skills.save_draft(skill)
        skills.publish(skill.id)
    for record in saved:
        if record.value.get("record_type") != "skill_draft":
            continue
        skills.save_draft(SkillDefinition.model_validate(record.value["definition"]))
    policy = FailClosedPolicy(
        StaticPolicyEvaluator(
            {
                ("analyst", "core.investigation.sample"): obligations,
                ("analyst", "core.investigation.connected"): obligations,
                ("analyst", "core.schema.inspect"): obligations,
                ("analyst", "core.query.validate"): obligations,
                ("analyst", "core.query.read"): obligations,
                ("analyst", "core.source.manage"): obligations,
                ("analyst", "core.skill.manage"): obligations,
                ("analyst", "core.provider.test"): obligations,
            }
        )
    )
    return Runtime(
        settings=settings,
        policy=policy,
        schema_parser=schema_parser,
        audit=SQLiteAuditChain(settings.state_path),
        store=store,
        tools=tools,
        skills=skills,
        provider=_provider(settings),
    )


@lru_cache
def get_runtime() -> Runtime:
    return build_runtime()
