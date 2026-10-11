"""Explicit contract review endpoints; existing source authorization is the ceiling."""

import json
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from opsgraph.api.dependencies import require_principal
from opsgraph.audit.sqlite import AuditIntegrityError
from opsgraph.brokers.query import intersect_obligations
from opsgraph.domain import Obligation, Principal
from opsgraph.domain.models import stable_hash
from opsgraph.domain.work_outcome import WorkOutcomeContract
from opsgraph.missed_work.contracts import admit, record_id
from opsgraph.persistence import WorkspaceRecord
from opsgraph.readiness import reviewed_source_matches, source_readiness_basis
from opsgraph.schema_service import SchemaSnapshot


class ContractReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: WorkOutcomeContract
    confirm_reviewed_meanings: Literal[True]

    @field_validator("confirm_reviewed_meanings", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):  # noqa: ANN001
        if value is not True:
            raise ValueError("explicit review confirmation is required")
        return value


def router_for(runtime, authorize):
    router = APIRouter()

    @router.get("/api/work-outcomes/contracts")
    def listing(principal: Annotated[Principal, Depends(require_principal)]):
        result = []
        for record in runtime.store.list(workspace_id=principal.workspace_id):
            if record.value.get("record_type") != "work_outcome_contract":
                continue
            source_id = record.value["definition"]["source_id"]
            authorize(principal, "core.source.manage", source_id)
            result.append(record.value)
        return result

    @router.post("/api/work-outcomes/contracts")
    def review(body: ContractReview, principal: Annotated[Principal, Depends(require_principal)]):
        contract = body.definition
        actions = ("core.source.manage", "core.schema.inspect", "core.query.read")
        permissions = {
            action: authorize(principal, action, contract.source_id) for action in actions
        }
        try:
            source_record = runtime.store.get(
                workspace_id=principal.workspace_id, record_id=f"source:{contract.source_id}"
            )
            source = source_record.value
            snapshot = SchemaSnapshot.model_validate(
                {
                    k: v
                    for k, v in runtime.store.get(
                        workspace_id=principal.workspace_id,
                        record_id=f"schema:{contract.source_id}",
                    ).value.items()
                    if k != "record_type"
                }
            )
        except KeyError:
            raise HTTPException(409, "Configure and inspect the approved source first.") from None
        if source.get("status") != "ready":
            raise HTTPException(409, "Inspect this source before reviewing an outcome contract.")
        bounds = Obligation(
            allowed_schemas=tuple(source["allowed_schemas"]),
            allowed_tables=tuple(source["allowed_tables"]),
        )
        try:
            for permission in permissions.values():
                bounds = intersect_obligations(permission, bounds)
            admit(contract, source["id"], snapshot, bounds)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(422, str(exc)) from None
        value = {
            "record_type": "work_outcome_contract",
            "definition": contract.model_dump(mode="json"),
            "source_revision": stable_hash(source),
            "source_access_revision": source_readiness_basis(source),
        }
        if any(
            authorize(principal, action, contract.source_id) != permissions[action]
            for action in actions
        ):
            raise HTTPException(409, "Policy changed during review. Review again.")
        key = record_id(contract.id, contract.version)
        try:
            with runtime.audit.transaction() as connection:
                existing = connection.execute(
                    "SELECT value_json FROM workspace_records WHERE workspace_id=? AND record_id=?",
                    (principal.workspace_id, key),
                ).fetchone()
                if existing:
                    saved = json.loads(existing[0])
                    if saved["definition"] != value["definition"] or not reviewed_source_matches(
                        saved, source, principal.workspace_id, runtime.audit.entries
                    ):
                        raise HTTPException(
                            409, "Reviewed contract versions are immutable. Save a new version."
                        )
                    return saved
                if not runtime.store.replace_if_unchanged_in_transaction(
                    connection,
                    source_record,
                    (WorkspaceRecord(principal.workspace_id, key, value),),
                ):
                    raise HTTPException(409, "Source configuration changed. Review again.")
                runtime.audit.append_in_transaction(
                    connection,
                    workspace_id=principal.workspace_id,
                    actor=principal.subject,
                    action="core.source.manage",
                    resource=contract.source_id,
                    outcome="allowed",
                    details={
                        "operation": "review_work_outcome",
                        "contract_id": contract.id,
                        "version": contract.version,
                        "definition_hash": stable_hash(value),
                    },
                )
        except (AuditIntegrityError, OSError, sqlite3.Error):
            raise HTTPException(
                503, "Contract was not saved because state/audit storage is unavailable."
            ) from None
        return value

    return router
