from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from opsgraph.audit import AuditChain
from opsgraph.auth import LocalDevAuthenticator
from opsgraph.domain import Obligation, PolicyDecision, Principal
from opsgraph.persistence import InMemoryWorkspaceStore, WorkspaceRecord
from opsgraph.policy import ActionRequest, FailClosedPolicy, StaticPolicyEvaluator


def test_local_dev_auth_is_opt_in_and_invalid_tokens_fail() -> None:
    disabled = LocalDevAuthenticator()
    with pytest.raises(PermissionError):
        disabled.issue(subject="dev", workspace_id="one")

    auth = LocalDevAuthenticator(enabled=True)
    token, credential = auth.issue(subject="dev", workspace_id="one")
    assert token not in credential.token_hash
    assert auth.authenticate(token).workspace_id == "one"
    with pytest.raises(PermissionError):
        auth.authenticate("wrong")


def test_policy_denies_workspace_mismatch_and_allow_without_obligations() -> None:
    principal = Principal(subject="dev", workspace_id="one", roles=frozenset({"analyst"}))
    policy = FailClosedPolicy(StaticPolicyEvaluator({("analyst", "inspect"): Obligation()}))
    mismatch = policy.authorize(
        ActionRequest(principal=principal, action="inspect", workspace_id="two", resource="schema")
    )
    assert mismatch.allowed is False

    class MalformedAllow:
        def evaluate(self, request):
            return PolicyDecision(allowed=True, reason="oops")

    decision = FailClosedPolicy(MalformedAllow()).authorize(
        ActionRequest(principal=principal, action="inspect", workspace_id="one", resource="schema")
    )
    assert decision.allowed is False


def test_workspace_store_never_crosses_scope() -> None:
    store = InMemoryWorkspaceStore()
    store.put(WorkspaceRecord("one", "same", {"value": 1}))
    store.put(WorkspaceRecord("two", "same", {"value": 2}))
    assert store.get(workspace_id="one", record_id="same").value == {"value": 1}
    assert store.get(workspace_id="two", record_id="same").value == {"value": 2}
    assert len(store.list(workspace_id="one")) == 1


def test_audit_hash_chain_detects_tampering_and_reordering() -> None:
    fixed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    chain = AuditChain(clock=lambda: fixed)
    first = chain.append(
        workspace_id="one",
        actor="dev",
        action="schema.inspect",
        resource="snapshot",
        outcome="allowed",
    )
    second = chain.append(
        workspace_id="one",
        actor="dev",
        action="query.read",
        resource="incidents",
        outcome="allowed",
        details={"rows": 3},
    )
    assert AuditChain.verify(chain.entries).valid is True
    tampered = second.model_copy(update={"details": {"rows": 3000}})
    result = AuditChain.verify([first, tampered])
    assert result.valid is False
    assert result.failure_sequence == 2
    assert AuditChain.verify([second, first]).valid is False


def test_audit_chain_remains_valid_under_concurrent_appends() -> None:
    chain = AuditChain()

    def append(index: int) -> None:
        chain.append(
            workspace_id="one",
            actor="tester",
            action="test.append",
            resource=str(index),
            outcome="allowed",
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(append, range(500)))

    verification = AuditChain.verify(chain.entries)
    assert verification.valid is True
    assert verification.checked_entries == 500


def test_obligations_are_hard_bounded() -> None:
    with pytest.raises(ValueError):
        Obligation(max_rows=10_000)
    with pytest.raises(ValueError):
        Obligation(timeout_ms=90_000)
