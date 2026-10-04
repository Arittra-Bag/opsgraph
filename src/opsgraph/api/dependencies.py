import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from opsgraph.config import Settings, get_settings
from opsgraph.domain import Principal


def require_workspace(
    settings: Annotated[Settings, Depends(get_settings)],
    x_opsgraph_key: Annotated[str | None, Header()] = None,
) -> str:
    """Authenticate the one trusted operator for this private workspace."""

    try:
        valid = bool(x_opsgraph_key) and secrets.compare_digest(
            x_opsgraph_key.encode("utf-8"), settings.api_key.encode("utf-8")
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="Valid workspace API key required")
    return settings.workspace_id


def require_principal(
    workspace_id: Annotated[str, Depends(require_workspace)],
) -> Principal:
    return Principal(
        subject="local-operator",
        workspace_id=workspace_id,
        roles=frozenset({"analyst"}),
    )
