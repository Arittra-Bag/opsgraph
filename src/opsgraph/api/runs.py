"""Durable investigation API; every progress event follows committed execution state."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from opsgraph.api.dependencies import require_principal, require_workspace
from opsgraph.brokers import PsycopgReadOnlyExecutor, SourceSchemaChanged
from opsgraph.brokers.query import intersect_obligations
from opsgraph.domain import EvidenceBinding, Obligation, Principal
from opsgraph.domain.models import stable_hash
from opsgraph.orchestration.connected import _effective_obligations, run_connected
from opsgraph.orchestration.coordinator import RunBlocked, RunCoordinator
from opsgraph.persistence import WorkspaceRecord
from opsgraph.persistence.runs import TERMINAL, QueueFull, RunStore
from opsgraph.providers import (
    ChatMessage,
    ProviderError,
    ProviderInvocationError,
    ProviderTimeoutError,
    StructuredRequest,
)
from opsgraph.readiness import source_readiness_basis
from opsgraph.reports import ReportOptions, ReportTooLarge, build_incident_report
from opsgraph.schema_service import SchemaSnapshot
from opsgraph.skills import SkillRepository


class RunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=800)
    conversation_id: str | None = Field(default=None, max_length=128)
    source_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    skill_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9.-]{0,127}$")
    parent_run_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("question")
    @classmethod
    def meaningful_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a message before sending")
        return value


class RunAPI:
    def __init__(self, runtime, authorize):
        self.runtime = runtime
        self.authorize = authorize
        self.store = RunStore(runtime.settings.state_path)
        self.coordinator = RunCoordinator(
            self.store,
            self.execute,
            self.completed,
            workspace_id=runtime.settings.workspace_id,
            on_terminal=self.terminal,
        )
        self.router = APIRouter()
        self._routes()

    def start(self):
        try:
            self.coordinator.start()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    def get(self, workspace, run_id):
        try:
            return self.store.get(workspace, run_id)
        except KeyError as exc:
            raise HTTPException(404, "investigation not found") from exc

    def submit(self, body, principal, *, retry_of=None):
        with self.runtime.provider_lock:
            return self._submit(body, principal, retry_of=retry_of)

    def _submit(self, body, principal, *, retry_of=None):
        if self.runtime.settings.mode != "connected":
            raise HTTPException(
                409, "Migration mode cannot execute investigations. Configure connected mode."
            )
        self.authorize(principal, "core.investigation.connected", body.source_id)
        self.start()
        if body.parent_run_id:
            parent = self.get(principal.workspace_id, body.parent_run_id)
            if parent["status"] not in TERMINAL or parent["source_id"] != body.source_id:
                raise HTTPException(
                    409, "follow-up requires a terminal investigation of the same source"
                )
        if body.conversation_id and not body.parent_run_id and not retry_of:
            try:
                conversation = self.store.conversation(principal.workspace_id, body.conversation_id)
            except KeyError:
                raise HTTPException(404, "conversation not found") from None
            if conversation["source_id"] != body.source_id:
                raise HTTPException(409, "conversation source cannot be changed")
        try:
            run = self.store.create(principal.workspace_id, body.model_dump(), retry_of=retry_of)
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        self.coordinator.notify()
        return run

    def execute(self, workspace, run, observe, check, coordinator):
        runtime = self.runtime
        if runtime.settings.mode != "connected":
            raise RunBlocked(
                "Migration mode cannot execute investigations. Configure connected mode."
            )
        if workspace != runtime.settings.workspace_id:
            raise RunBlocked("Investigation does not belong to the configured workspace.")
        principal = Principal(subject="local-operator", workspace_id=workspace, roles={"analyst"})
        from opsgraph.orchestration.conversation import capability_reply

        reply = capability_reply(run["question"])
        if reply:
            self.authorize(principal, "core.investigation.connected", run["source_id"])
            check()
            return {
                "response_kind": "conversation",
                "assistant_message": reply,
                "answer": {"summary": reply, "findings": [], "limitations": []},
                "plan": {
                    "queries": [],
                    "rationale": "Conversation without database access",
                    "clarification": None,
                },
            }
        policy_actions = (
            "core.investigation.connected",
            "core.query.read",
            "core.schema.inspect",
            "core.source.readiness",
        )
        try:
            policy_obligations = {
                action: self.authorize(principal, action, run["source_id"])
                for action in policy_actions
            }
            source = runtime.store.get(
                workspace_id=workspace, record_id=f"source:{run['source_id']}"
            ).value
            snapshot_record = runtime.store.get(
                workspace_id=workspace, record_id=f"schema:{run['source_id']}"
            ).value
        except (KeyError, HTTPException) as exc:
            raise RunBlocked(
                "Configure and inspect this source, and select an available playbook.",
                exc.status_code if isinstance(exc, HTTPException) else 409,
            ) from exc
        try:
            skill = runtime.skills.get_published(run["skill_id"])
        except KeyError as exc:
            raise RunBlocked("Selected playbook is not published.", 422) from exc
        if runtime.provider.config.kind == "deterministic":
            raise RunBlocked(
                "Configure a real local or hosted model. "
                "Replay providers cannot investigate sources."
            )
        if source.get("status") != "ready":
            raise RunBlocked("Inspect the source before starting an investigation.")
        readiness = source.get("readiness") or {}
        if (
            readiness.get("status") != "ready"
            or readiness.get("source_revision") != source_readiness_basis(source)
            or readiness.get("policy_revision")
            != stable_hash(policy_obligations["core.source.readiness"].model_dump(mode="json"))
        ):
            raise RunBlocked(
                "Run and review this source's bounded readiness check before investigating."
            )
        tables = tuple(source.get("allowed_tables", ()))
        if not tables:
            raise RunBlocked("Select an explicit table allowlist for this source.")
        approved_refs = set(runtime.settings.allowed_postgres_secret_refs)
        if runtime.settings.postgres_secret_ref:
            approved_refs.add(runtime.settings.postgres_secret_ref)
        if source.get("secret_ref") not in approved_refs:
            raise RunBlocked(
                "Source credential reference is no longer approved by this deployment."
            )
        dsn = os.getenv(source["secret_ref"])
        if not dsn:
            raise RunBlocked("Source credential reference is not configured on the server.")
        if runtime.provider.capabilities.external_egress and not source.get(
            "allow_external_egress"
        ):
            raise RunBlocked("This source does not permit external model processing.", 403)
        snapshot = SchemaSnapshot.model_validate(
            {k: v for k, v in snapshot_record.items() if k != "record_type"}
        )
        discovered = {f"{table.schema_name}.{table.table_name}" for table in snapshot.tables}
        if not set(tables).issubset(discovered):
            raise RunBlocked("Source schema changed; inspect and review its table scope again.")
        obligations = Obligation(
            max_rows=100,
            timeout_ms=5000,
            allowed_schemas=tuple(source["allowed_schemas"]),
            allowed_tables=tables,
        )
        sql_binding = next((item for item in skill.tools if item.tool == "core.sql.select"), None)
        if sql_binding is None or not sql_binding.enabled:
            raise RunBlocked("Selected playbook does not enable read-only SQL.")
        try:
            for action in policy_actions:
                obligations = intersect_obligations(policy_obligations[action], obligations)
            obligations = _effective_obligations(obligations, sql_binding.settings)
        except PermissionError as exc:
            raise RunBlocked(
                "Source, deployment policy and playbook scopes do not overlap. "
                "Choose a compatible playbook or review the backend allowlist and source scope."
            ) from exc
        snapshot = snapshot.scoped(obligations.allowed_tables)
        tables = obligations.allowed_tables
        frozen_skills = SkillRepository(tools=runtime.tools, policy_ceiling=obligations)
        # Apply stricter current policy to this execution copy, preserving published content.
        bounded_sql = sql_binding.model_copy(
            update={"settings": sql_binding.settings.model_copy(update=obligations.model_dump())}
        )
        frozen_skills.save_draft(
            skill.model_copy(
                update={
                    "tools": tuple(
                        bounded_sql if binding.tool == "core.sql.select" else binding
                        for binding in skill.tools
                    )
                }
            )
        )
        frozen_skills.publish(skill.id)
        source_revision = stable_hash(source)
        policy_revision = stable_hash(
            {action: value.model_dump(mode="json") for action, value in policy_obligations.items()}
        )
        configuration = {
            "run_id": run["id"],
            "source_id": source["id"],
            "source_name": source["name"],
            "source_hosting_profile": source.get("hosting_profile", "self_hosted"),
            "source_revision": source_revision,
            "schema_fingerprint": snapshot.fingerprint,
            "schema_inspected_at": snapshot.model_dump(mode="json")["inspected_at"],
            "skill_id": skill.id,
            "skill_version": skill.version,
            "skill_fingerprint": stable_hash(skill.model_dump(mode="json")),
            "policy": "strict-read-only@1",
            "policy_fingerprint": policy_revision,
            "limits": obligations.model_dump(mode="json"),
            "max_queries": 3,
            "provider": runtime.provider.config.kind,
            "model": runtime.provider.config.model,
            "reasoning_effort": runtime.provider.config.reasoning_effort,
            "schema_profile": runtime.provider.config.schema_profile,
            "provider_timeout_seconds": runtime.provider.config.timeout_seconds,
        }
        observe("configured", {"configuration": configuration})
        parent_context = ""
        if run["parent_run_id"]:
            parent = self.store.get(workspace, run["parent_run_id"])
            if parent["source_id"] != run["source_id"] or parent["status"] not in TERMINAL:
                raise RunBlocked("Follow-up parent is unavailable or outside this source.")
            captures = [
                {
                    "evidence_hash": item["evidence_hash"],
                    "created_at": item.get("created_at"),
                    "referenced_tables": item.get("referenced_tables", []),
                }
                for item in parent["evidence"]
                if item.get("referenced_tables") and set(item["referenced_tables"]).issubset(tables)
            ]
            # Historical conclusions remain hypotheses; only current evidence supports this run.
            previous_findings = (
                (parent.get("answer") or {}).get("findings", [])
                if len(captures) == len(parent["evidence"])
                else []
            )
            parent_context = json.dumps(
                {
                    "question": parent["question"],
                    "clarification": (
                        parent["error"].get("message")
                        if (parent.get("error") or {}).get("code") == "clarification_required"
                        else None
                    ),
                    "captured_evidence": captures,
                    "previous_findings": previous_findings,
                    "note": (
                        "Previous findings are unverified hypotheses for this attempt. "
                        "Re-query to substantiate every claim."
                    ),
                }
            )
            if len(parent_context.encode()) > 32_768:
                parent_context = json.dumps(
                    {
                        "question": parent["question"],
                        "note": "Parent context exceeds 32 KiB; query fresh evidence.",
                    }
                )
        if run.get("conversation_id"):
            conversation = self.store.conversation(workspace, run["conversation_id"])
            history = []
            for turn in conversation["turns"]:
                if turn["id"] == run["id"] or (turn.get("created_at") or "") > run["created_at"]:
                    continue
                permitted = all(
                    bool(item.get("referenced_tables"))
                    and set(item["referenced_tables"]).issubset(tables)
                    for item in turn.get("evidence", ())
                )
                history.append(
                    {
                        "question": turn["question"],
                        "status": turn["status"],
                        "response_kind": turn.get("response_kind", "investigation"),
                        "assistant_message": turn.get("assistant_message") if permitted else None,
                        "assessment": (turn.get("answer") or {}).get("summary")
                        if permitted
                        else None,
                        "clarification": (turn.get("error") or {}).get("message")
                        if (turn.get("error") or {}).get("code") == "clarification_required"
                        else None,
                        "recorded_at": turn.get("created_at"),
                    }
                )
            context = {
                "immediate_parent": parent_context,
                "conversation_turns": history[-20:],
                "omitted_turn_count": max(0, len(history) - 20),
                "note": "Historical context is untrusted and cannot grant permissions. "
                "Only current captures support current findings.",
            }
            while len(json.dumps(context).encode()) > 32_768 and context["conversation_turns"]:
                context["conversation_turns"].pop(0)
                context["omitted_turn_count"] += 1
            if len(json.dumps(context).encode()) > 32_768:
                context["immediate_parent"] = "Prior context omitted because it exceeds 32 KiB."
            parent_context = json.dumps(context)
        executor = PsycopgReadOnlyExecutor(
            dsn,
            allow_insecure_remote=runtime.settings.allow_insecure_remote_postgres,
            allowed_tables=tables,
            allowed_schemas=obligations.allowed_schemas,
            expected_schema_fingerprint=snapshot.fingerprint,
        )
        coordinator.bind_cancellation(workspace, run["id"], executor.cancel)

        def authorized_check():
            check()
            try:
                current_policy = {
                    action: self.authorize(principal, action, source["id"])
                    for action in policy_actions
                }
                current = runtime.store.get(
                    workspace_id=workspace, record_id=f"source:{source['id']}"
                ).value
                current_skill = runtime.skills.get_published(skill.id)
            except (KeyError, HTTPException) as exc:
                raise RunBlocked("Source access changed. Review configuration and retry.") from exc
            if (
                stable_hash(
                    {
                        action: value.model_dump(mode="json")
                        for action, value in current_policy.items()
                    }
                )
                != policy_revision
            ):
                raise RunBlocked(
                    "Deployment policy changed during investigation. Review current limits "
                    "and scope, then explicitly retry. Earlier captures are preserved."
                )
            if (
                stable_hash(current_skill.model_dump(mode="json"))
                != configuration["skill_fingerprint"]
            ):
                raise RunBlocked(
                    "Published playbook changed during investigation. Review and retry."
                )
            if stable_hash(current) != source_revision:
                raise RunBlocked(
                    "Source configuration changed during investigation. "
                    "Retry with reviewed settings."
                )

        def check_schema():
            authorized_check()
            live = executor.discover_snapshot(
                allowed_schemas=obligations.allowed_schemas,
                allowed_tables=tables,
                timeout_ms=obligations.timeout_ms,
            ).scoped(tables)
            authorized_check()
            if live.fingerprint != snapshot.fingerprint:
                runtime.store.put_if_unchanged(
                    WorkspaceRecord(workspace, f"source:{source['id']}", source),
                    (
                        WorkspaceRecord(
                            workspace,
                            f"source:{source['id']}",
                            {
                                **source,
                                "status": "stale",
                            },
                        ),
                    ),
                )
                raise RunBlocked(
                    "Source schema or SELECT-visible columns changed since inspection. "
                    "Review the saved scope, inspect the source again and explicitly retry. "
                    "Earlier captures are preserved."
                )
            observe(
                "schema_checked",
                {
                    "fingerprint": live.fingerprint,
                    "checked_at": live.model_dump(mode="json")["inspected_at"],
                },
            )

        check_schema()

        try:
            return run_connected(
                question=run["question"],
                provider=runtime.provider,
                principal=principal,
                obligations=obligations,
                skills=frozen_skills,
                executor=executor,
                snapshot=snapshot,
                skill_id=skill.id,
                evidence_bindings=tuple(
                    binding.model_copy(update={"source_tables": permitted})
                    for value in source.get("evidence_bindings", ())
                    if (binding := EvidenceBinding.model_validate(value))
                    and (
                        permitted := tuple(
                            table for table in binding.source_tables if table in tables
                        )
                    )
                ),
                observe=observe,
                check=authorized_check,
                provenance=configuration,
                parent_context=parent_context,
                before_query=authorized_check,
            )
        except SourceSchemaChanged as exc:
            current_record = runtime.store.get(
                workspace_id=workspace, record_id=f"source:{source['id']}"
            )
            current = current_record.value
            if stable_hash(current) == source_revision:
                runtime.store.put_if_unchanged(
                    current_record,
                    (
                        WorkspaceRecord(
                            workspace,
                            f"source:{source['id']}",
                            {
                                **current,
                                "status": "stale",
                                "readiness": {
                                    "status": "pending",
                                    "reason": "Inspect and rerun readiness after schema changes.",
                                },
                            },
                        ),
                    ),
                )
            raise RunBlocked(
                "Source schema or SELECT-visible columns changed before query execution. "
                "Inspect and review the source again, rerun readiness, and explicitly retry. "
                "Earlier captures are preserved."
            ) from exc

    def completed(self, workspace, run):
        legacy = {
            "record_type": "investigation",
            **{
                key: run[key]
                for key in ("id", "source_id", "question", "skill_id", "plan", "evidence", "answer")
            },
        }
        self.runtime.store.put(WorkspaceRecord(workspace, f"investigation:{run['id']}", legacy))

    def terminal(self, workspace, run_id, status, code):
        """Audit one terminal transition without exception text or captured data."""

        # The durable run marker is written after this callback. If a process exits
        # between those operations, startup replays the callback. A prior entry for
        # this globally unique run makes that replay idempotent.
        if any(
            entry.workspace_id == workspace
            and entry.action == "core.investigation.connected"
            and entry.resource == run_id
            for entry in self.runtime.audit.entries
        ):
            return
        run = self.store.get(workspace, run_id)
        outcome = "allowed" if status == "completed" else "rejected"
        details = {
            "source_id": run["source_id"],
            "evidence_count": len(run["evidence"]),
        }
        if status != "completed":
            details = {"status": status, "code": code or status, **details}
        self.runtime.audit.append(
            workspace_id=workspace,
            actor="local-operator",
            action="core.investigation.connected",
            resource=run_id,
            outcome=outcome,
            details=details,
        )

    def _routes(self):
        router = self.router

        @router.post("/api/runs", status_code=202)
        def create(body: RunRequest, principal: Annotated[Principal, Depends(require_principal)]):
            return self.submit(body, principal)

        @router.get("/api/conversations")
        def conversations(
            workspace: Annotated[str, Depends(require_workspace)],
            limit: int = Query(default=100, ge=1, le=100),
        ):
            return self.store.conversations(workspace, limit)

        @router.get("/api/conversations/{conversation_id}")
        def conversation(
            conversation_id: str, workspace: Annotated[str, Depends(require_workspace)]
        ):
            try:
                return self.store.conversation(workspace, conversation_id)
            except KeyError:
                raise HTTPException(404, "conversation not found") from None

        @router.get("/api/runs")
        def listing(
            workspace: Annotated[str, Depends(require_workspace)],
            limit: int = Query(default=100, ge=1, le=100),
        ):
            return self.store.list(workspace, limit)

        @router.get("/api/runs/{run_id}")
        def detail(run_id: str, workspace: Annotated[str, Depends(require_workspace)]):
            return self.get(workspace, run_id)

        @router.get("/api/runs/{run_id}/export")
        def export(run_id: str, workspace: Annotated[str, Depends(require_workspace)]):
            run = self.get(workspace, run_id)
            return JSONResponse(
                {"export_version": 1, **run},
                headers={"Content-Disposition": f'attachment; filename="{run["id"]}.json"'},
            )

        @router.post("/api/runs/{run_id}/report")
        def report(
            run_id: str,
            body: ReportOptions,
            workspace: Annotated[str, Depends(require_workspace)],
        ):
            try:
                value = build_incident_report(self.get(workspace, run_id), body)
            except ReportTooLarge as error:
                raise HTTPException(422, str(error)) from None
            return JSONResponse(value, headers={"Cache-Control": "no-store"})

        @router.post("/api/runs/{run_id}/cancel")
        def cancel(run_id: str, principal: Annotated[Principal, Depends(require_principal)]):
            run = self.get(principal.workspace_id, run_id)
            self.authorize(principal, "core.investigation.connected", run["source_id"])
            value, terminal_transition = self.store.cancel_with_transition(
                principal.workspace_id, run_id
            )
            if terminal_transition:
                try:
                    self.coordinator.notify_terminal(
                        principal.workspace_id,
                        run_id,
                        value["status"],
                        value["status"],
                    )
                except RuntimeError as exc:
                    raise HTTPException(503, str(exc)) from None
                value = self.store.get(principal.workspace_id, run_id)
            if value["status"] == "cancelling":
                self.coordinator.request_cancel(principal.workspace_id, run_id)
            self.coordinator.notify()
            return value

        @router.post("/api/runs/{run_id}/retry", status_code=202)
        def retry(run_id: str, principal: Annotated[Principal, Depends(require_principal)]):
            run = self.get(principal.workspace_id, run_id)
            if run["status"] not in TERMINAL:
                raise HTTPException(409, "only a terminal investigation can be retried")
            body = RunRequest(
                question=run["question"],
                conversation_id=run["conversation_id"],
                source_id=run["source_id"],
                skill_id=run["skill_id"],
                parent_run_id=run["parent_run_id"],
            )
            return self.submit(body, principal, retry_of=run_id)

        @router.get("/api/runs/{run_id}/events")
        async def events(
            run_id: str,
            request: Request,
            workspace: Annotated[str, Depends(require_workspace)],
            after: int = Query(default=0, ge=0),
        ):
            self.get(workspace, run_id)
            header_cursor = request.headers.get("last-event-id")
            if header_cursor:
                try:
                    after = max(after, int(header_cursor))
                except ValueError as exc:
                    raise HTTPException(422, "Last-Event-ID must be a nonnegative integer") from exc

            async def stream():
                cursor = after
                heartbeat = time.monotonic()
                while not await request.is_disconnected():
                    for event in self.store.events(workspace, run_id, cursor):
                        cursor = event["id"]
                        yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event)}\n\n"
                    run = self.store.get(workspace, run_id)
                    if run["status"] in TERMINAL and cursor >= run["last_event_id"]:
                        return
                    if time.monotonic() - heartbeat >= 10:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.2)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @router.post("/api/providers/current/test")
        def probe(principal: Annotated[Principal, Depends(require_principal)]):
            self.authorize(principal, "core.provider.test", "current-provider")
            started = time.monotonic()
            with self.runtime.provider_lock:
                provider = self.runtime.provider
                configuration_revision = self.runtime.provider_revision
                generation = self.runtime.provider_verification.begin(configuration_revision)

            def audit(outcome: str, reason: str, provider):
                with self.runtime.provider_lock:
                    current = (
                        self.runtime.provider is provider
                        and self.runtime.provider_revision == configuration_revision
                    )
                    try:
                        self.runtime.audit.append(
                            workspace_id=principal.workspace_id,
                            actor=principal.subject,
                            action="core.provider.test",
                            resource="current-provider",
                            outcome=outcome,
                            details={
                                "reason": reason,
                                "adapter": provider.config.kind,
                                "preset": provider.config.provider_preset,
                                "configuration_revision": configuration_revision,
                                "probe_generation": generation,
                                "duration_ms": round((time.monotonic() - started) * 1_000),
                            },
                        )
                    except Exception:
                        if current:
                            self.runtime.provider_verification.finish(
                                configuration_revision, generation, success=False
                            )
                        raise HTTPException(
                            503,
                            "Model check could not be recorded. Check private workspace storage.",
                        ) from None
                    if current:
                        self.runtime.provider_verification.finish(
                            configuration_revision,
                            generation,
                            success=reason == "structured_probe_succeeded",
                        )

            if provider.config.kind == "deterministic":
                audit("rejected", "provider_not_configured", provider)
                raise HTTPException(409, "Configure a real local or hosted model provider.")
            try:
                response = provider.invoke_structured(
                    StructuredRequest(
                        messages=(
                            ChatMessage(
                                role="user",
                                content=(
                                    'Return {"ok":true}. This checks structured output; '
                                    "no source data is included."
                                ),
                            ),
                        ),
                        response_schema={
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    )
                )
                if set(response.output) != {"ok"} or response.output["ok"] is not True:
                    raise ProviderInvocationError(
                        "Model returned an incompatible probe response; "
                        'expected exactly {"ok":true}. '
                        "Choose a model/endpoint with structured JSON-schema support."
                    )
            except ProviderTimeoutError as exc:
                audit("rejected", "timeout", provider)
                raise HTTPException(
                    504,
                    "Model test timed out. Warm or choose a smaller model; "
                    "review the configured timeout.",
                ) from exc
            except ProviderError as exc:
                # Adapter errors are sanitized at their boundary; never expose SDK bodies.
                audit("rejected", "provider_error", provider)
                recovery = (
                    " For Ollama, run ollama list on the model server. If the selected model "
                    "is missing, download it with ollama pull followed by its model name, "
                    "then try this check again. OpsGraph does not download models."
                    if provider.config.provider_preset == "ollama"
                    else ""
                )
                raise HTTPException(422, f"Model test failed. {exc}{recovery}") from None
            except Exception as exc:
                audit("rejected", "invalid_probe_response", provider)
                raise HTTPException(
                    422,
                    "Model test failed. Verify model availability, structured-output support "
                    "and server configuration.",
                ) from exc
            with self.runtime.provider_lock:
                if (
                    self.runtime.provider is not provider
                    or self.runtime.provider_revision != configuration_revision
                    or self.runtime.provider_verification.generation != generation
                ):
                    audit("rejected", "configuration_changed", provider)
                    raise HTTPException(
                        409,
                        "Model configuration or connection check changed during this test. "
                        "Try again.",
                    )
                audit("allowed", "structured_probe_succeeded", provider)
                verification = self.runtime.provider_verification.public(configuration_revision)
                return {
                    "ok": True,
                    "configuration_revision": configuration_revision,
                    "provider": provider.config.kind,
                    "model": provider.config.model,
                    "reported_model": response.reported_model,
                    "checked_at": verification["checked_at"],
                    "verification": verification,
                    "health": {
                        "status": "ready",
                        "detail": "Real structured model call succeeded.",
                    },
                }

    @asynccontextmanager
    async def lifespan(self, app):
        self.start()
        try:
            yield
        finally:
            await asyncio.to_thread(self.coordinator.close)
