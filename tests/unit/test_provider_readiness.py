"""Verification freshness does not depend on SDK configuration or wall-clock changes."""

from threading import RLock
from types import SimpleNamespace

import opsgraph.api.app as app_module
import opsgraph.provider_readiness as verification_module
from opsgraph.provider_readiness import ProviderVerification
from opsgraph.providers import OpenAICompatibleProvider, ProviderConfig


def test_check_is_revision_bound_and_expires_monotonically(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(verification_module, "monotonic", lambda: clock[0])
    check = ProviderVerification()
    assert check.public("one")["status"] == "untested"
    generation = check.begin("one")
    assert check.public("one")["status"] == "checking"
    assert check.finish("one", generation, success=True)
    result = check.public("one")
    assert result["status"] == "verified"
    assert result["valid_for_seconds"] == 900
    assert result["checked_at"]
    assert result["expires_at"]
    assert check.public("two")["status"] == "untested"
    clock[0] = 1000
    assert check.public("one")["status"] == "expired"
    assert check.public("one")["valid_for_seconds"] == 0
    assert ProviderVerification().public("one")["status"] == "untested"


def test_older_failure_cannot_overwrite_newer_success():
    check = ProviderVerification()
    old = check.begin("one")
    new = check.begin("one")
    assert check.finish("one", new, success=True)
    assert not check.finish("one", old, success=False)
    assert check.public("one")["status"] == "verified"


def test_health_never_equates_client_configuration_with_model_verification(monkeypatch):
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            kind="openai_compatible", model="not-installed", base_url="http://127.0.0.1:9/v1"
        ),
        client_factory=lambda _: object(),
    )
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            mode="connected", model_provider="openai_compatible", egress_enabled=False
        ),
        provider=provider,
        provider_revision="one",
        provider_lock=RLock(),
        provider_verification=ProviderVerification(),
    )
    monkeypatch.setattr(app_module, "runtime", runtime)
    assert app_module.health()["ok"] is True
    assert app_module.health()["investigation_ready"] is False
    generation = runtime.provider_verification.begin("one")
    runtime.provider_verification.finish("one", generation, success=True)
    assert app_module.health()["investigation_ready"] is True
    runtime.provider_verification.deadline = 0
    assert app_module.health()["investigation_ready"] is False
