import os
import time
from typing import Annotated

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
from opsgraph.brokers.query import intersect_obligations
from opsgraph.domain import EvidenceBinding, Obligation, Principal
from opsgraph.persistence import WorkspaceRecord
from opsgraph.policy import ActionRequest
from opsgraph.runtime import get_runtime
from opsgraph.schema_service import SchemaParseError, SchemaSnapshot
from opsgraph.skills import SkillDefinition, SkillValidationError

runtime = get_runtime()
WEB = runtime.settings.web_root

app = FastAPI(title="OpsGraph Beta", version=__version__)
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
        # Liveness is independent of model readiness; provider test performs a real call.
        "ok": True,
        "investigation_ready": settings.mode == "connected"
        and settings.model_provider != "deterministic"
        and provider_health.status == "ready",
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
        "product": "OpsGraph Beta",
        "mode": settings.mode,
        "trust": {
            "deployment": "self-hosted",
            "access": "read-only",
            "model": settings.model_provider,
            "sample_model_calls": 0,
            "real_execution_only": True,
            "egress": settings.egress_enabled,
            "policy": "strict-read-only@1",
        },
        "authentication": "Set X-OpsGraph-Key for protected API requests.",
        "limitations": [
            "Investigations require real PostgreSQL and a configured model. No sample fallback.",
            "Connected mode requires a separately provisioned read-only PostgreSQL role.",
        ],
    }


@app.get("/api/sources")
def sources(workspace_id: Annotated[str, Depends(require_workspace)]):
    saved = [record.value for record in runtime.store.list(workspace_id=workspace_id)]
    connected = [value for value in saved if value.get("record_type") == "source"]
    return connected


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
    policy_obligations = authorize(principal, "core.schema.inspect", source_id)
    try:
        stored = runtime.store.get(
            workspace_id=principal.workspace_id, record_id=f"source:{source_id}"
        ).value
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="source not found") from exc
    if stored.get("status") == "ready":
        stale = {**stored, "status": "stale"}
        if not runtime.store.put_if_unchanged(
            WorkspaceRecord(principal.workspace_id, f"source:{source_id}", stored),
            (WorkspaceRecord(principal.workspace_id, f"source:{source_id}", stale),),
        ):
            raise HTTPException(409, "Source configuration changed. Inspect again.")
        stored = stale
    allowed_tables = tuple(stored.get("allowed_tables", ()))
    if not allowed_tables:
        raise HTTPException(422, "Select at least one explicit allowed table before inspection.")
    try:
        inspection_scope = intersect_obligations(
            policy_obligations,
            Obligation(
                allowed_schemas=tuple(stored["allowed_schemas"]),
                allowed_tables=allowed_tables,
            ),
        )
    except PermissionError as exc:
        raise HTTPException(
            403,
            "Source scope is outside current deployment policy. "
            "Review the backend schema allowlist and explicit tables.",
        ) from exc
    if set(inspection_scope.allowed_tables) != set(allowed_tables):
        raise HTTPException(
            403,
            "Some source tables are outside current deployment policy. "
            "Review the backend schema allowlist and explicit tables.",
        )
    secret_ref = str(stored["secret_ref"])
    approved_refs = set(runtime.settings.allowed_postgres_secret_refs)
    if runtime.settings.postgres_secret_ref:
        approved_refs.add(runtime.settings.postgres_secret_ref)
    if secret_ref not in approved_refs:
        raise HTTPException(422, "Source credential reference is no longer approved.")
    dsn = os.getenv(secret_ref)
    if not dsn:
        raise HTTPException(
            status_code=409,
            detail=f"secret reference is not configured: {secret_ref}",
        )
    try:
        snapshot = PsycopgReadOnlyExecutor(dsn).discover_snapshot(
            allowed_schemas=inspection_scope.allowed_schemas,
            allowed_tables=allowed_tables,
            timeout_ms=inspection_scope.timeout_ms,
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
    discovered = {f"{table.schema_name}.{table.table_name}" for table in snapshot.tables}
    missing_tables = set(allowed_tables).difference(discovered)
    if missing_tables:
        raise HTTPException(
            status_code=422,
            detail="Configured table is absent or has no SELECT-visible columns: "
            f"{sorted(missing_tables)[0]}. Check schema USAGE and column/table SELECT grants.",
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
    scoped_snapshot = snapshot.scoped(allowed_tables)
    if authorize(principal, "core.schema.inspect", source_id) != policy_obligations:
        raise HTTPException(409, "Deployment policy changed during inspection. Inspect again.")
    updated = {
        **stored,
        "status": "ready",
        "schema_version": scoped_snapshot.fingerprint,
        "inspected_at": scoped_snapshot.model_dump(mode="json")["inspected_at"],
    }
    if not runtime.store.put_if_unchanged(
        WorkspaceRecord(principal.workspace_id, f"source:{source_id}", stored),
        (
            WorkspaceRecord(principal.workspace_id, f"source:{source_id}", updated),
            WorkspaceRecord(
                principal.workspace_id,
                f"schema:{source_id}",
                {"record_type": "schema", **scoped_snapshot.model_dump(mode="json")},
            ),
        ),
    ):
        raise HTTPException(409, "Source configuration changed during inspection. Inspect again.")
    return scoped_snapshot.inspection_payload(status="ready")


@app.get("/api/sources/{source_id}/schema")
def source_schema(source_id: str, principal: Annotated[Principal, Depends(require_principal)]):
    """Read the last saved inspection, never claim that cached metadata is live."""
    policy_obligations = authorize(principal, "core.schema.inspect", source_id)
    try:
        source = runtime.store.get(
            workspace_id=principal.workspace_id, record_id=f"source:{source_id}"
        ).value
        record = runtime.store.get(
            workspace_id=principal.workspace_id, record_id=f"schema:{source_id}"
        ).value
    except KeyError as exc:
        raise HTTPException(
            404, "No saved schema inspection. Save and inspect this source."
        ) from exc
    snapshot = SchemaSnapshot.model_validate(record)
    # A reconfigured source must not expose columns from its previous wider scope.
    tables = tuple(source.get("allowed_tables", ()))
    if tables:
        try:
            tables = intersect_obligations(
                policy_obligations,
                Obligation(
                    allowed_schemas=tuple(source["allowed_schemas"]),
                    allowed_tables=tables,
                ),
            ).allowed_tables
        except PermissionError:
            tables = ()
    snapshot = snapshot.scoped(tables)
    status = source.get("status", "configured")
    if set(tables) != set(source.get("allowed_tables", ())):
        status = "stale"
    return snapshot.inspection_payload(status=status)


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
    raise HTTPException(
        status_code=410,
        detail="Sample investigations have been retired. Configure a real source and model.",
    )


@app.post("/api/investigations")
def investigate_connected(
    body: ConnectedInvestigationRequest,
    principal: Annotated[Principal, Depends(require_principal)],
):
    run = run_api.submit(RunRequest(**body.model_dump()), principal)
    while run["status"] not in TERMINAL:
        time.sleep(0.1)
        run = run_api.get(principal.workspace_id, run["id"])
    if run["status"] != "completed":
        code = (run["error"] or {}).get("http_status", 422)
        raise HTTPException(code, run["error"]["message"] if run["error"] else run["status"])
    return {
        key: run[key]
        for key in ("id", "source_id", "question", "skill_id", "plan", "evidence", "answer")
    }


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


from opsgraph.api.runs import RunAPI, RunRequest  # noqa: E402
from opsgraph.persistence.runs import TERMINAL  # noqa: E402

run_api = RunAPI(runtime, authorize)
app.include_router(run_api.router)
app.router.lifespan_context = run_api.lifespan

from opsgraph.api.provider_settings import router_for  # noqa: E402

app.include_router(router_for(runtime, run_api))
