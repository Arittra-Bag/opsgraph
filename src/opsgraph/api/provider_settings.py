"""Authenticated model settings: saving never invokes a provider."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from opsgraph.api.dependencies import require_workspace
from opsgraph.provider_settings import (
    ProviderSettingsRequest,
    make_config,
    public_config,
    settings_path,
)
from opsgraph.providers import create_provider
from opsgraph.setup import SetupError, write_private_config


def router_for(runtime, run_api):
    router = APIRouter()

    @router.get("/api/providers/configuration")
    def read(_: Annotated[str, Depends(require_workspace)]):
        with runtime.provider_lock:
            return public_config(
                runtime.provider.config, runtime.settings.egress_enabled, runtime.provider_revision
            )

    @router.put("/api/providers/configuration")
    async def update(request: Request, workspace: Annotated[str, Depends(require_workspace)]):
        # Require a browser's exact origin as well as the workspace credential.
        origin = request.headers.get("origin")
        if origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Model settings must be saved from this application's origin.")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
            raise HTTPException(403, "Cross-origin model settings changes are not allowed.")
        raw = await request.body()
        if len(raw) > 16384:
            raise HTTPException(413, "Model configuration is too large.")
        try:
            body = ProviderSettingsRequest.model_validate_json(raw)
        except ValidationError:
            # FastAPI's default validation errors can echo a submitted credential.
            raise HTTPException(
                422, "Invalid model settings. Check provider, model, endpoint and timeout."
            ) from None
        with runtime.provider_lock:
            with run_api.store.connect() as db:
                busy = db.execute(
                    "SELECT 1 FROM runs WHERE workspace_id=? AND status IN "
                    "('queued','running','cancelling') LIMIT 1",
                    (workspace,),
                ).fetchone()
            if busy:
                raise HTTPException(
                    409, "Wait for or cancel unfinished investigations before changing the model."
                )
            try:
                config = make_config(body, runtime.provider.config, runtime.settings.egress_enabled)
                provider = create_provider(config)
                write_private_config(
                    settings_path(runtime.settings),
                    {
                        "PROVIDER_CONFIG": config.model_dump_json(exclude={"api_key"}),
                        "PROVIDER_KEY": config.api_key.get_secret_value() if config.api_key else "",
                    },
                )
            except PermissionError:
                raise HTTPException(
                    403, "External model processing is disabled by deployment configuration."
                ) from None
            except (SetupError, ValueError) as exc:
                detail = str(exc) if isinstance(exc, SetupError) else "Invalid model configuration."
                raise HTTPException(422, detail) from None
            except OSError:
                raise HTTPException(
                    503,
                    "Model settings could not be saved. Check private workspace "
                    "storage permissions.",
                ) from None
            runtime.provider = provider
            runtime.provider_revision = uuid4().hex
            runtime.settings.model_provider = config.kind
            runtime.settings.local_model = config.model
            runtime.settings.anthropic_model = config.model
            return public_config(config, runtime.settings.egress_enabled, runtime.provider_revision)

    return router
