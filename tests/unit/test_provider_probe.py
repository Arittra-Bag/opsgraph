"""Probe contract doubles; these do not certify a live model endpoint."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opsgraph.api.runs import RunAPI
from opsgraph.config import get_settings
from opsgraph.providers import (
    ProviderConfig,
    ProviderInvocationError,
    ProviderTimeoutError,
    StructuredResponse,
)
from opsgraph.runtime import build_runtime


@pytest.fixture
def probe_api(tmp_path):
    settings = get_settings().model_copy(update={"state_path": tmp_path / "state.db"})
    runtime = build_runtime(settings)
    calls = []
    runtime.provider = SimpleNamespace(
        config=ProviderConfig(
            kind="openai_compatible", model="probe-double", base_url="http://localhost/v1"
        ),
        invoke_structured=lambda request: calls.append(request),
        audit=runtime.audit,
        runtime=runtime,
    )
    service = RunAPI(runtime, lambda *args: None)
    app = FastAPI()
    app.include_router(service.router)
    with TestClient(app) as client:
        yield runtime.provider, client, {"X-OpsGraph-Key": settings.api_key}, calls


@pytest.mark.parametrize(
    "output",
    [
        {"ok": 1},
        {"ok": 1.0},
        {"ok": "true"},
        {"ok": False},
        {},
        {"ok": True, "extra": "unexpected"},
    ],
)
def test_probe_rejects_nonliteral_true_and_extra_properties(probe_api, output):
    provider, client, headers, _ = probe_api
    provider.invoke_structured = lambda _: StructuredResponse(
        provider=provider.config.kind, model=provider.config.model, output=output
    )
    response = client.post("/api/providers/current/test", headers=headers)
    assert response.status_code == 422
    assert "expected exactly" in response.json()["detail"]


def test_probe_calls_model_with_no_source_data_and_preserves_success_contract(probe_api):
    provider, client, headers, calls = probe_api

    def invoke(request):
        calls.append(request)
        return StructuredResponse(
            provider=provider.config.kind,
            model=provider.config.model,
            output={"ok": True},
            reported_model="actual-revision",
        )

    provider.invoke_structured = invoke
    response = client.post("/api/providers/current/test", headers=headers)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["reported_model"] == "actual-revision"
    assert response.json()["checked_at"]
    assert len(calls) == 1
    assert "no source data is included" in calls[0].messages[0].content
    assert calls[0].response_schema["additionalProperties"] is False
    audit = provider.audit.entries[-1]
    assert audit.action == "core.provider.test" and audit.outcome == "allowed"
    assert audit.details["reason"] == "structured_probe_succeeded"
    assert set(audit.details) == {
        "reason",
        "adapter",
        "preset",
        "duration_ms",
        "configuration_revision",
        "probe_generation",
    }
    assert audit.details["configuration_revision"] == response.json()["configuration_revision"]
    assert response.json()["verification"]["status"] == "verified"


@pytest.mark.parametrize(
    "error,status,detail",
    [
        (ProviderInvocationError("access denied; verify backend API key"), 422, "access denied"),
        (ProviderTimeoutError("timed out"), 504, "Warm or choose a smaller model"),
        (RuntimeError("private-key-in-raw-SDK-error"), 422, "Verify model availability"),
    ],
)
def test_probe_surfaces_only_safe_provider_details(probe_api, error, status, detail):
    provider, client, headers, _ = probe_api

    def fail(_):
        raise error

    provider.invoke_structured = fail
    response = client.post("/api/providers/current/test", headers=headers)
    assert response.status_code == status
    assert detail in response.json()["detail"]
    assert "private-key" not in response.text


def test_probe_requires_authentication_before_invocation(probe_api):
    _, client, _, calls = probe_api
    assert client.post("/api/providers/current/test").status_code == 401
    assert calls == []


def test_new_check_clears_success_before_invocation_and_failure_keeps_it_cleared(probe_api):
    provider, client, headers, _ = probe_api
    runtime = provider.runtime
    provider.invoke_structured = lambda _: StructuredResponse(
        provider=provider.config.kind, model=provider.config.model, output={"ok": True}
    )
    assert client.post("/api/providers/current/test", headers=headers).status_code == 200

    def fail(_):
        assert (
            runtime.provider_verification.public(runtime.provider_revision)["status"] == "checking"
        )
        raise ProviderTimeoutError("timed out")

    provider.invoke_structured = fail
    assert client.post("/api/providers/current/test", headers=headers).status_code == 504
    assert runtime.provider_verification.public(runtime.provider_revision)["status"] == "failed"


def test_older_success_cannot_replace_a_newer_check(probe_api):
    provider, client, headers, _ = probe_api
    runtime = provider.runtime

    def newer_check(_):
        generation = runtime.provider_verification.begin(runtime.provider_revision)
        runtime.provider_verification.finish(runtime.provider_revision, generation, success=False)
        return StructuredResponse(
            provider=provider.config.kind, model=provider.config.model, output={"ok": True}
        )

    provider.invoke_structured = newer_check
    assert client.post("/api/providers/current/test", headers=headers).status_code == 409
    assert runtime.provider_verification.public(runtime.provider_revision)["status"] == "failed"


def test_configuration_change_during_check_does_not_verify_new_configuration(probe_api):
    provider, client, headers, _ = probe_api
    runtime = provider.runtime

    def change(_):
        runtime.provider_revision = "replacement-revision"
        return StructuredResponse(
            provider=provider.config.kind, model=provider.config.model, output={"ok": True}
        )

    provider.invoke_structured = change
    assert client.post("/api/providers/current/test", headers=headers).status_code == 409
    assert runtime.provider_verification.public(runtime.provider_revision)["status"] == "untested"


def test_audit_failure_never_leaves_success_or_exposes_storage_details(probe_api, monkeypatch):
    provider, client, headers, _ = probe_api
    runtime = provider.runtime
    provider.invoke_structured = lambda _: StructuredResponse(
        provider=provider.config.kind, model=provider.config.model, output={"ok": True}
    )

    def unavailable(**_):
        raise OSError("private storage path and key")

    monkeypatch.setattr(runtime.audit, "append", unavailable)
    response = client.post("/api/providers/current/test", headers=headers)
    assert response.status_code == 503
    assert "private storage path" not in response.text
    assert runtime.provider_verification.public(runtime.provider_revision)["status"] == "failed"


def test_ollama_failure_explains_missing_model_recovery(probe_api):
    provider, client, headers, _ = probe_api
    provider.config = provider.config.model_copy(update={"provider_preset": "ollama"})

    def missing(_):
        raise ProviderInvocationError("model or API route not found")

    provider.invoke_structured = missing
    response = client.post("/api/providers/current/test", headers=headers)
    assert response.status_code == 422
    assert "ollama list" in response.text and "ollama pull" in response.text
