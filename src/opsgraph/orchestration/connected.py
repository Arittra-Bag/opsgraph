"""Question-dependent, policy-gated connected investigation graph."""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from opsgraph.brokers import QueryBroker, ReadOnlyExecutor, SelectOnlyValidator
from opsgraph.domain import Obligation, Principal
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator
from opsgraph.providers import ChatMessage, ModelProvider, StructuredRequest
from opsgraph.schema_service import SchemaSnapshot
from opsgraph.skills import SkillRepository, ToolSettings


class QueryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str = Field(min_length=3, max_length=300)
    sql: str = Field(min_length=8, max_length=20_000)
    evidence_types: tuple[str, ...] = Field(min_length=1, max_length=12)


class InvestigationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queries: tuple[QueryProposal, ...] = Field(min_length=1, max_length=3)
    rationale: str = Field(min_length=3, max_length=1_000)


class CitedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: str = Field(min_length=3, max_length=2_000)
    classification: Literal["supported", "possible", "unknown", "contradictory"]
    evidence_ids: tuple[str, ...]


class InvestigationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=3, max_length=4_000)
    findings: tuple[CitedFinding, ...] = Field(max_length=12)
    limitations: tuple[str, ...] = Field(max_length=12)


class ConnectedState(TypedDict, total=False):
    question: str
    requested_skill_id: str | None
    skill_id: str
    plan: dict[str, Any]
    evidence: list[dict[str, Any]]
    answer: dict[str, Any]


def _select_skill(question: str) -> str:
    lowered = question.lower()
    if any(word in lowered for word in ("job", "queue", "worker", "scheduler")):
        return "failed-jobs"
    if any(word in lowered for word in ("schema", "column", "migration", "table")):
        return "schema-impact"
    return "incident-correlation"


def build_connected_graph(
    *,
    provider: ModelProvider,
    principal: Principal,
    obligations: Obligation,
    skills: SkillRepository,
    executor: ReadOnlyExecutor,
    snapshot: SchemaSnapshot,
):
    def route(state: ConnectedState) -> dict[str, Any]:
        requested = state.get("requested_skill_id")
        return {"skill_id": requested or _select_skill(state["question"])}

    def plan(state: ConnectedState) -> dict[str, Any]:
        skill = skills.get_published(state["skill_id"])
        if provider.capabilities.external_egress and skill.egress != "allowlisted":
            raise PermissionError("selected skill forbids external model egress")
        schema = snapshot.model_dump(mode="json")
        prompt = (
            "Create one to three PostgreSQL SELECT-only queries to investigate the question. "
            "Use only listed tables and columns. Keep every query narrow and evidence-oriented. "
            "Never use comments, functions beyond common aggregates, system schemas, or writes.\n"
            "Across the plan, cover every required evidence type: "
            f"{list(skill.required_evidence)}.\n"
            f"Selected skill: {state['skill_id']}\n"
            f"Question: {state['question']}\n"
            f"Schema: {json.dumps(schema, sort_keys=True)}"
        )
        response = provider.invoke_structured(
            StructuredRequest(
                messages=(ChatMessage(role="user", content=prompt),),
                response_schema=InvestigationPlan.model_json_schema(),
                system="You are a cautious database investigation planner.",
            )
        )
        validated = InvestigationPlan.model_validate(response.output)
        return {"plan": validated.model_dump(mode="json")}

    def execute(state: ConnectedState) -> dict[str, Any]:
        skill = skills.get_published(state["skill_id"])
        binding = next(
            (item for item in skill.tools if item.tool == "core.sql.select"),
            None,
        )
        if binding is None or not binding.enabled:
            raise PermissionError("selected skill does not enable core.sql.select")
        effective = _effective_obligations(obligations, binding.settings)
        policy = FailClosedPolicy(
            StaticPolicyEvaluator({("analyst", "core.query.read"): effective})
        )
        broker = QueryBroker(
            policy=policy,
            validator=SelectOnlyValidator(),
            executor=executor,
        )
        plan_value = InvestigationPlan.model_validate(state["plan"])
        evidence: list[dict[str, Any]] = []
        for proposal in plan_value.queries:
            artifact = broker.query(principal=principal, sql=proposal.sql)
            payload = artifact.model_dump(mode="json")
            payload["purpose"] = proposal.purpose
            payload["evidence_types"] = list(proposal.evidence_types)
            evidence.append(payload)
        covered = {tag for item in evidence for tag in item["evidence_types"]}
        missing = set(skill.required_evidence).difference(covered)
        if missing:
            raise ValueError(f"plan does not cover required evidence: {sorted(missing)[0]}")
        return {"evidence": evidence}

    def reconcile(state: ConnectedState) -> dict[str, Any]:
        evidence = state.get("evidence", [])
        skill = skills.get_published(state["skill_id"])
        evidence_ids = [item["evidence_hash"] for item in evidence]
        prompt = (
            "Answer only from the bounded evidence. Every non-unknown finding must cite one or "
            "more exact evidence_hash values. If evidence is insufficient, say unknown.\n"
            f"Question: {state['question']}\n"
            f"Evidence: {json.dumps(evidence, sort_keys=True)}"
        )
        response = provider.invoke_structured(
            StructuredRequest(
                messages=(ChatMessage(role="user", content=prompt),),
                response_schema=InvestigationAnswer.model_json_schema(),
                system="You reconcile operational evidence without inventing facts.",
            )
        )
        answer = InvestigationAnswer.model_validate(response.output)
        allowed = set(evidence_ids)
        for finding in answer.findings:
            if finding.classification not in skill.conclusion_classes:
                raise ValueError(
                    "provider returned a classification forbidden by the selected skill"
                )
            if not set(finding.evidence_ids) <= allowed:
                raise ValueError("provider returned an unknown evidence citation")
            if finding.classification != "unknown" and not finding.evidence_ids:
                raise ValueError("non-unknown findings require evidence citations")
        return {"answer": answer.model_dump(mode="json")}

    builder = StateGraph(ConnectedState)
    builder.add_node("route", route)
    builder.add_node("plan", plan)
    builder.add_node("execute", execute)
    builder.add_node("reconcile", reconcile)
    builder.add_edge(START, "route")
    builder.add_edge("route", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", "reconcile")
    builder.add_edge("reconcile", END)
    return builder.compile()


def run_connected(
    *,
    question: str,
    provider: ModelProvider,
    principal: Principal,
    obligations: Obligation,
    skills: SkillRepository,
    executor: ReadOnlyExecutor,
    snapshot: SchemaSnapshot,
    skill_id: str | None = None,
) -> ConnectedState:
    graph = build_connected_graph(
        provider=provider,
        principal=principal,
        obligations=obligations,
        skills=skills,
        executor=executor,
        snapshot=snapshot,
    )
    return graph.invoke({"question": question, "requested_skill_id": skill_id})


def _effective_obligations(base: Obligation, settings: ToolSettings) -> Obligation:
    def narrowed(current: tuple[str, ...], requested: tuple[str, ...] | None) -> tuple[str, ...]:
        if requested is None:
            return current
        if not current:
            return requested
        return tuple(value for value in requested if value in set(current))

    schemas = narrowed(base.allowed_schemas, settings.allowed_schemas)
    if not schemas:
        raise PermissionError("skill and source schema scopes do not overlap")
    return Obligation(
        max_rows=min(base.max_rows, settings.max_rows or base.max_rows),
        timeout_ms=min(base.timeout_ms, settings.timeout_ms or base.timeout_ms),
        allowed_schemas=schemas,
        allowed_tables=narrowed(base.allowed_tables, settings.allowed_tables),
    )
