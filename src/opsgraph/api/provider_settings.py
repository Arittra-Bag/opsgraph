"""Authenticated model settings: saving never invokes a provider."""

import os
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from opsgraph.api.dependencies import require_principal, require_workspace
from opsgraph.domain import Principal
from opsgraph.provider_settings import (
    ProviderSettingsRequest,
    make_config,
    pending_settings_path,
    provider_audit_fingerprint,
    public_config,
    settings_path,
)
from opsgraph.providers import create_provider
from opsgraph.setup import SetupError, read_private_config, write_private_config


def router_for(runtime, run_api):
    router = APIRouter()

    @router.get("/api/providers/configuration")
    def read(_: Annotated[str, Depends(require_workspace)]):
        with runtime.provider_lock:
            return public_config(
                runtime.provider.config, runtime.settings.egress_enabled, runtime.provider_revision
            )

    @router.put("/api/providers/configuration")
    async def update(
        request: Request,
        principal: Annotated[Principal, Depends(require_principal)],
    ):
        def audit(outcome: str, reason: str, **details):
            return runtime.audit.append(
                workspace_id=principal.workspace_id,
                actor=principal.subject,
                action="core.provider.manage",
                resource="current-provider",
                outcome=outcome,
                details={"reason": reason, **details},
            )

        # Require a browser's exact origin as well as the workspace credential.
        origin = request.headers.get("origin")
        if origin != str(request.base_url).rstrip("/"):
            audit("rejected", "origin_check_failed")
            raise HTTPException(403, "Model settings must be saved from this application's origin.")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
            audit("rejected", "cross_origin_request")
            raise HTTPException(403, "Cross-origin model settings changes are not allowed.")
        run_api.authorize(principal, "core.provider.manage", "current-provider")
        workspace = principal.workspace_id
        raw = await request.body()
        if len(raw) > 16384:
            audit("rejected", "request_too_large")
            raise HTTPException(413, "Model configuration is too large.")
        try:
            body = ProviderSettingsRequest.model_validate_json(raw)
        except ValidationError:
            # FastAPI's default validation errors can echo a submitted credential.
            audit("rejected", "invalid_configuration")
            raise HTTPException(
                422, "Invalid model settings. Check provider, model, endpoint, limits and timeout."
            ) from None
        with runtime.provider_lock:
            with run_api.store.connect() as db:
                busy = db.execute(
                    "SELECT 1 FROM runs WHERE workspace_id=? AND status IN "
                    "('queued','running','cancelling') LIMIT 1",
                    (workspace,),
                ).fetchone()
            if busy:
                audit("rejected", "investigation_active")
                raise HTTPException(
                    409, "Wait for or cancel unfinished investigations before changing the model."
                )
            path = settings_path(runtime.settings)
            pending = pending_settings_path(runtime.settings)
            try:
                if path.exists():
                    read_private_config(path)
                config = make_config(body, runtime.provider.config, runtime.settings.egress_enabled)
                provider = create_provider(config)
                revision = uuid4().hex
                write_private_config(
                    pending,
                    {
                        "PROVIDER_CONFIG": config.model_dump_json(exclude={"api_key"}),
                        "PROVIDER_KEY": config.api_key.get_secret_value() if config.api_key else "",
                        "PROVIDER_REVISION": revision,
                    },
                )
            except PermissionError:
                audit("rejected", "external_egress_disabled")
                raise HTTPException(
                    403, "External model processing is disabled by deployment configuration."
                ) from None
            except (SetupError, ValueError) as exc:
                detail = str(exc) if isinstance(exc, SetupError) else "Invalid model configuration."
                audit("rejected", "invalid_configuration")
                raise HTTPException(422, detail) from None
            except OSError:
                audit("rejected", "private_storage_unavailable")
                raise HTTPException(
                    503,
                    "Model settings could not be saved. Check private workspace "
                    "storage permissions.",
                ) from None
            try:
                audit(
                    "allowed",
                    "configuration_saved",
                    preset=config.provider_preset,
                    adapter=config.kind,
                    external_egress=config.egress_enabled,
                    revision=revision,
                    configuration_fingerprint=provider_audit_fingerprint(
                        config,
                        binding_key=runtime.settings.api_key,
                    ),
                )
            except Exception:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    raise HTTPException(
                        503,
                        "Model settings could not be audited or cleaned up. "
                        "Restore private workspace storage before restarting OpsGraph.",
                    ) from None
                raise HTTPException(
                    503,
                    "Model settings were not changed because local audit storage is unavailable.",
                ) from None
            try:
                os.replace(pending, path)
            except OSError:
                pending.unlink(missing_ok=True)
                raise HTTPException(
                    503,
                    "Model settings were audited but could not be activated. "
                    "Check private workspace storage permissions and retry.",
                ) from None
            runtime.provider = provider
            runtime.provider_revision = revision
            runtime.settings.model_provider = config.kind
            runtime.settings.local_model = config.model
            runtime.settings.anthropic_model = config.model
            return public_config(config, runtime.settings.egress_enabled, runtime.provider_revision)

    return router
