"""Malformed workspace headers remain authentication failures."""

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from opsgraph.api.dependencies import require_workspace
from opsgraph.config import get_settings


@pytest.mark.parametrize("header", [None, "", "wrong", "é", "\ud800"])
def test_invalid_workspace_header_returns_unauthorized(header):
    settings = SimpleNamespace(api_key="test-private-workspace-key-long-enough", workspace_id="one")
    with pytest.raises(HTTPException) as error:
        require_workspace(settings, header)
    assert error.value.status_code == 401
    assert error.value.detail == "Valid workspace API key required"


def test_workspace_credential_comparison_preserves_unicode_configuration():
    key = "unicode-workspace-key-é-long-enough"
    settings = SimpleNamespace(api_key=key, workspace_id="one")
    assert require_workspace(settings, key) == "one"


def test_non_ascii_http_header_does_not_cause_server_error():
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        api_key="test-private-workspace-key-long-enough", workspace_id="one"
    )

    @app.get("/protected")
    def protected(workspace: str = Depends(require_workspace)):
        return {"workspace": workspace}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers={b"X-OpsGraph-Key": b"\xe9"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Valid workspace API key required"}
