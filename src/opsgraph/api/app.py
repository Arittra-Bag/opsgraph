import os
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from opsgraph import __version__
from opsgraph.api.dependencies import require_principal, require_workspace
from opsgraph.audit import AuditChain
from opsgraph.brokers import (
    ConnectorUnavailable,
    PsycopgReadOnlyExecutor,
    SelectOnlyValidator,
    UnsafeDatabaseRole,
    UnsafeQuery,
)
from opsgraph.domain import EvidenceBinding, Obligation, Principal
from opsgraph.domain.models import stable_hash
from opsgraph.orchestration.connected import run_connected
from opsgraph.orchestration.sample import run_sample
from opsgraph.persistence import WorkspaceRecord
from opsgraph.policy import ActionRequest
from opsgraph.providers import ProviderError
from opsgraph.runtime import get_runtime
from opsgraph.schema_service import SchemaParseError, SchemaSnapshot
from opsgraph.skills import SkillDefinition, SkillValidationError

runtime = get_runtime()
WEB = runtime.settings.web_root

app = FastAPI(title="OpsGraph Alpha", version=__version__)
if WEB.exists():
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")

query_validator = SelectOnlyValidator()


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=8, max_length=800)


class ConnectedInvestigationRequest(InvestigationRequest):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    skill_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9.-]{0,127}$")


class SchemaRequest(BaseModel):
    ddl: str = Field(min_length=8, max_length=500_000)


class QueryRequest(BaseModel):
    sql: str = Field(min_length=8, max_length=50_000)


class SourceRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    name: str = Field(min_length=2, max_length=120)
    secret_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    allowed_schemas: tuple[str, ...] = ("public",)
    allowed_tables: tuple[str, ...] = ()
    evidence_bindings: tuple[EvidenceBinding, ...] = ()
    allow_external_egress: bool = False

    @field_validator("allowed_schemas")
    @classmethod
    def validate_allowed_schemas(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) > 32:
            raise ValueError("allowed_schemas must contain 1-32 schema names")
        if any(
            not value
            or len(value) > 63
            or not value.replace("_", "a").isalnum()
            or not (value[0].isalpha() or value[0] == "_")
            for value in values
        ):
            raise ValueError("allowed schema names must be PostgreSQL identifiers")
        return tuple(dict.fromkeys(values))

    @field_validator("allowed_tables")
    @classmethod
    def validate_allowed_tables(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 100:
            raise ValueError("allowed_tables may contain at most 100 tables")
        normalized = tuple(dict.fromkeys(values))
        for value in normalized:
            parts = value.split(".")
            if len(parts) != 2 or any(
                not part
                or len(part) > 63
                or not part.replace("_", "a").isalnum()
                or not (part[0].isalpha() or part[0] == "_")
                for part in parts
            ):
                raise ValueError("allowed tables must be schema-qualified PostgreSQL identifiers")
        return normalized

    @field_validator("evidence_bindings")
    @classmethod
    def unique_evidence_bindings(
        cls, values: tuple[EvidenceBinding, ...]
    ) -> tuple[EvidenceBinding, ...]:
        evidence_types = [binding.evidence_type for binding in values]
        if len(evidence_types) != len(set(evidence_types)):
            raise ValueError("each evidence type may be bound only once per source")
        return values


def authorize(principal: Principal, action: str, resource: str) -> Obligation:
    decision = runtime.policy.authorize(
        ActionRequest(
            principal=principal,
            action=action,
            workspace_id=principal.workspace_id,
            resource=resource,
        )
    )
    if not decision.allowed or decision.obligations is None:
        runtime.audit.append(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            action=action,
            resource=resource,
            outcome="denied",
            details={"policy_id": decision.policy_id, "reason": decision.reason},
        )
        raise HTTPException(status_code=403, detail=decision.reason)
    return decision.obligations


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    settings = runtime.settings
    provider_health = runtime.provider.health()
    return {
        # Sample replay deliberately makes no provider call, so a configured
        # external provider cannot make the safe sample deployment unhealthy.
        "ok": settings.mode == "sample" or provider_health.status == "ready",
        "version": __version__,
        "mode": settings.mode,
        "model": settings.model_provider,
        "egress": settings.egress_enabled,
        "provider": provider_health.model_dump(mode="json"),
    }


@app.get("/api/bootstrap")
def bootstrap():
    settings = runtime.settings
    return {
        "product": "OpsGraph Alpha",
        "mode": settings.mode,
        "trust": {
            "deployment": "self-hosted",
            "access": "read-only",
            "model": settings.model_provider,
            "sample_model_calls": 0,
            "egress": settings.egress_enabled,
            "policy": "strict-read-only@1",
        },
        "authentication": "Set X-OpsGraph-Key for protected API requests.",
        "limitations": [
            "Sample mode uses synthetic data and performs no database or model call.",
            "Connected mode requires a separately provisioned read-only PostgreSQL role.",
        ],
    }


@app.get("/api/sources")
def sources(workspace_id: Annotated[str, Depends(require_workspace)]):
    saved = [record.value for record in runtime.store.list(workspace_id=workspace_id)]
    connected = [value for value in saved if value.get("record_type") == "source"]
    return [
        {
            "id": "sample-saas",
            "workspace_id": workspace_id,
            "name": "Fictional SaaS sample",
            "kind": "synthetic",
            "status": "ready",
            "read_only": True,
            "schema_version": "sha256:sample-v1",
        },
        *connected,
    ]


@app.get("/api/playbooks")
def playbooks(_: Annotated[str, Depends(require_workspace)]):
    skills = []
    for skill in runtime.skills.list_published():
        skills.append(
            {
                "id": skill.id,
                "name": skill.name,
                "version": skill.version,
                "tools": [binding.model_dump(mode="json") for binding in skill.tools],
            }
        )
    return skills


@app.get("/api/providers/current")
def provider_status(_: Annotated[str, Depends(require_workspace)]):
    return {
        "health": runtime.provider.health().model_dump(mode="json"),
        "capabilities": runtime.provider.capabilities.model_dump(mode="json"),
    }


@app.get("/api/policies/current")
def current_policy(principal: Annotated[Principal, Depends(require_principal)]):
    """Expose the effective server policy, never an editable policy file."""

    obligations = authorize(principal, "core.query.read", "policy-inspection")
    return {
        "id": "strict-read-only@1",
        "default": "deny",
        "allowed_actions": [
            "core.schema.inspect",
            "core.query.read",
            "core.investigation.sample",
            "core.investigation.connected",
        ],
        "obligations": obligations.model_dump(mode="json"),
        "rejected": ["DDL", "DML", "stacked SQL", "unbounded result sets"],
    }


@app.post("/api/sources")
def create_source(
    body: SourceRequest,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.source.manage", body.id)
    allowed_refs = set(runtime.settings.allowed_postgres_secret_refs)
    if runtime.settings.postgres_secret_ref:
        allowed_refs.add(runtime.settings.postgres_secret_ref)
    if body.secret_ref not in allowed_refs:
        raise HTTPException(
            status_code=422,
            detail="secret reference is not approved by this deployment",
        )
    record = {
        "record_type": "source",
        "id": body.id,
        "workspace_id": principal.workspace_id,
        "name": body.name,
        "kind": "postgresql",
        "secret_ref": body.secret_ref,
        "allowed_schemas": list(body.allowed_schemas),
        "allowed_tables": list(body.allowed_tables),
        "evidence_bindings": [
            binding.model_dump(mode="json") for binding in body.evidence_bindings
        ],
        "allow_external_egress": body.allow_external_egress,
        "status": "configured",
        "read_only": True,
    }
    runtime.store.put(WorkspaceRecord(principal.workspace_id, f"source:{body.id}", record))
    runtime.audit.append(
        workspace_id=principal.workspace_id,
        actor=principal.subject,
        action="core.source.manage",
        resource=body.id,
        outcome="allowed",
        details={
            "secret_ref": body.secret_ref,
            "allowed_schemas": list(body.allowed_schemas),
            "allowed_tables": list(body.allowed_tables),
            "evidence_bindings": [
                binding.model_dump(mode="json") for binding in body.evidence_bindings
            ],
            "allow_external_egress": body.allow_external_egress,
        },
    )
    return record


@app.post("/api/sources/{source_id}/inspect")
def inspect_source(
    source_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.schema.inspect", source_id)
    try:
        stored = runtime.store.get(
            workspace_id=principal.workspace_id, record_id=f"source:{source_id}"
        ).value
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="source not found") from exc
    secret_ref = str(stored["secret_ref"])
    dsn = os.getenv(secret_ref)
    if not dsn:
        raise HTTPException(
            status_code=409,
            detail=f"secret reference is not configured: {secret_ref}",
        )
    try:
        snapshot = PsycopgReadOnlyExecutor(dsn).discover_snapshot(
            allowed_schemas=tuple(stored["allowed_schemas"])
        )
    except (ConnectorUnavailable, UnsafeDatabaseRole) as exc:
        runtime.audit.append(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            action="core.schema.inspect",
            resource=source_id,
            outcome="rejected",
            details={"reason": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    allowed_tables = tuple(stored.get("allowed_tables", ()))
    discovered = {f"{table.schema_name}.{table.table_name}" for table in snapshot.tables}
    missing_tables = set(allowed_tables).difference(discovered)
    if missing_tables:
        raise HTTPException(
            status_code=422,
            detail=f"configured table scope is absent from schema: {sorted(missing_tables)[0]}",
        )
    bindings = tuple(
        EvidenceBinding.model_validate(value) for value in stored.get("evidence_bindings", ())
    )
    binding_tables = {table for binding in bindings for table in binding.source_tables}
    missing_binding_tables = binding_tables.difference(discovered)
    if missing_binding_tables:
        raise HTTPException(
            status_code=422,
            detail=(
                "configured evidence binding table is absent from schema: "
                f"{sorted(missing_binding_tables)[0]}"
            ),
        )
    if allowed_tables and not binding_tables.issubset(set(allowed_tables)):
        outside_scope = binding_tables.difference(allowed_tables)
        raise HTTPException(
            status_code=422,
            detail=(
                "configured evidence binding table is outside source scope: "
                f"{sorted(outside_scope)[0]}"
            ),
        )
    scoped_tables = tuple(
        table
        for table in snapshot.tables
        if not allowed_tables or f"{table.schema_name}.{table.table_name}" in allowed_tables
    )
    scoped_snapshot = snapshot.model_copy(
        update={
            "tables": scoped_tables,
            "fingerprint": stable_hash([table.model_dump(mode="json") for table in scoped_tables]),
        }
    )
    updated = {**stored, "status": "ready", "schema_version": scoped_snapshot.fingerprint}
    runtime.store.put(WorkspaceRecord(principal.workspace_id, f"source:{source_id}", updated))
    runtime.store.put(
        WorkspaceRecord(
            principal.workspace_id,
            f"schema:{source_id}",
            {"record_type": "schema", **scoped_snapshot.model_dump(mode="json")},
        )
    )
    return scoped_snapshot.model_dump(mode="json")


@app.get("/api/skills")
def skills(_: Annotated[str, Depends(require_workspace)]):
    return playbooks(_)


@app.post("/api/skills/drafts")
def save_skill(
    body: SkillDefinition,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.skill.manage", body.id)
    try:
        skill = runtime.skills.save_draft(body)
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.store.put(
        WorkspaceRecord(
            principal.workspace_id,
            f"skill-draft:{skill.id}",
            {"record_type": "skill_draft", "definition": skill.model_dump(mode="json")},
        )
    )
    return skill.model_dump(mode="json")


@app.post("/api/skills/{skill_id}/publish")
def publish_skill(
    skill_id: str,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.skill.manage", skill_id)
    try:
        skill = runtime.skills.publish(skill_id)
    except (KeyError, SkillValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.store.put(
        WorkspaceRecord(
            principal.workspace_id,
            f"skill-published:{skill.id}:{skill.version}",
            {"record_type": "skill_published", "definition": skill.model_dump(mode="json")},
        )
    )
    runtime.store.delete(
        workspace_id=principal.workspace_id,
        record_id=f"skill-draft:{skill.id}",
    )
    return skill.model_dump(mode="json")


@app.post("/api/investigations/sample")
def investigate(
    body: InvestigationRequest,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.investigation.sample", "sample-saas")
    result = run_sample(body.question)
    runtime.audit.append(
        workspace_id=principal.workspace_id,
        actor=principal.subject,
        action="core.investigation.sample",
        resource=result.id,
        outcome="allowed",
        details={"policy_version": result.policy_version, "evidence_count": len(result.evidence)},
    )
    return result.model_dump(mode="json")


@app.post("/api/investigations")
def investigate_connected(
    body: ConnectedInvestigationRequest,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.investigation.connected", body.source_id)
    if runtime.provider.config.kind == "deterministic":
        raise HTTPException(
            status_code=409,
            detail="connected investigations require an enabled model provider",
        )
    try:
        source = runtime.store.get(
            workspace_id=principal.workspace_id,
            record_id=f"source:{body.source_id}",
        ).value
        snapshot_value = runtime.store.get(
            workspace_id=principal.workspace_id,
            record_id=f"schema:{body.source_id}",
        ).value
    except KeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="source must be configured and inspected first",
        ) from exc
    secret_ref = str(source["secret_ref"])
    dsn = os.getenv(secret_ref)
    if not dsn:
        raise HTTPException(status_code=409, detail="source secret is not configured")
    obligations = Obligation(
        max_rows=100,
        timeout_ms=5_000,
        allowed_schemas=tuple(source["allowed_schemas"]),
        allowed_tables=tuple(source.get("allowed_tables", ())),
    )
    if not obligations.allowed_tables:
        raise HTTPException(
            status_code=409,
            detail="connected sources require an explicit table allowlist before investigation",
        )
    if runtime.provider.capabilities.external_egress and not source.get(
        "allow_external_egress", False
    ):
        raise HTTPException(
            status_code=403,
            detail="source does not permit bounded evidence to leave this host",
        )
    snapshot = SchemaSnapshot.model_validate(
        {key: value for key, value in snapshot_value.items() if key != "record_type"}
    )
    evidence_bindings = tuple(
        EvidenceBinding.model_validate(value) for value in source.get("evidence_bindings", ())
    )
    if body.skill_id:
        try:
            runtime.skills.get_published(body.skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="selected skill is not published") from exc
    try:
        state = run_connected(
            question=body.question,
            provider=runtime.provider,
            principal=principal,
            obligations=obligations,
            skills=runtime.skills,
            executor=PsycopgReadOnlyExecutor(dsn),
            snapshot=snapshot,
            skill_id=body.skill_id,
            evidence_bindings=evidence_bindings,
        )
    except PermissionError as exc:
        runtime.audit.append(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            action="core.investigation.connected",
            resource=body.source_id,
            outcome="denied",
            details={"reason": str(exc)},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (
        ConnectorUnavailable,
        UnsafeDatabaseRole,
        UnsafeQuery,
        ProviderError,
        ValueError,
    ) as exc:
        runtime.audit.append(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            action="core.investigation.connected",
            resource=body.source_id,
            outcome="rejected",
            details={"reason": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    investigation_id = f"inv-{uuid4().hex[:12]}"
    result = {
        "id": investigation_id,
        "source_id": body.source_id,
        "question": body.question,
        "skill_id": state["skill_id"],
        "plan": state["plan"],
        "evidence": state["evidence"],
        "answer": state["answer"],
    }
    runtime.store.put(
        WorkspaceRecord(
            principal.workspace_id,
            f"investigation:{investigation_id}",
            {"record_type": "investigation", **result},
        )
    )
    runtime.audit.append(
        workspace_id=principal.workspace_id,
        actor=principal.subject,
        action="core.investigation.connected",
        resource=investigation_id,
        outcome="allowed",
        details={
            "source_id": body.source_id,
            "skill_id": state["skill_id"],
            "evidence_count": len(state["evidence"]),
        },
    )
    return result


@app.post("/api/schema/inspect")
def inspect_schema(
    body: SchemaRequest,
    principal: Annotated[Principal, Depends(require_principal)],
):
    authorize(principal, "core.schema.inspect", "uploaded-schema")
    try:
        snapshot = runtime.schema_parser.inspect(body.ddl)
    except SchemaParseError as exc:
        runtime.audit.append(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            action="core.schema.inspect",
            resource="uploaded-schema",
            outcome="rejected",
            details={"reason": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.audit.append(
        workspace_id=principal.workspace_id,
        actor=principal.subject,
        action="core.schema.inspect",
        resource=snapshot.fingerprint,
        outcome="allowed",
        details={"tables": len(snapshot.tables)},
    )
    return snapshot.model_dump(mode="json")


@app.post("/api/query/validate")
def validate_query(
    body: QueryRequest,
    principal: Annotated[Principal, Depends(require_principal)],
):
    obligations = authorize(principal, "core.query.validate", "query-preview")
    try:
        plan = query_validator.validate(
            workspace_id=principal.workspace_id,
            sql=body.sql,
            obligations=obligations,
        )
    except UnsafeQuery as exc:
        runtime.audit.append(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            action="core.query.validate",
            resource="query-preview",
            outcome="rejected",
            details={"reason": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.audit.append(
        workspace_id=principal.workspace_id,
        actor=principal.subject,
        action="core.query.validate",
        resource=plan.fingerprint,
        outcome="allowed",
        details={"tables": list(plan.referenced_tables)},
    )
    return {
        "allowed": True,
        "fingerprint": plan.fingerprint,
        "normalized_sql": plan.normalized_sql,
        "referenced_tables": plan.referenced_tables,
        "obligations": plan.obligations.model_dump(mode="json"),
        "executed": False,
    }


@app.get("/api/audit")
def audit(workspace_id: Annotated[str, Depends(require_workspace)]):
    entries = tuple(entry for entry in runtime.audit.entries if entry.workspace_id == workspace_id)
    # A filtered view cannot preserve a global sequence when more workspaces are added.
    verification = (
        AuditChain.verify(entries) if len(entries) == len(runtime.audit.entries) else None
    )
    return {
        "verification": verification.model_dump(mode="json")
        if verification
        else {
            "valid": None,
            "checked_entries": len(entries),
            "reason": "indeterminate workspace-filtered view; verify the exported global chain",
        },
        "events": [entry.model_dump(mode="json") for entry in entries],
    }
