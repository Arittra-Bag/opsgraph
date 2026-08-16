"""Small policy protocol with an explicit fail-closed adapter.

The bundled evaluator is intentionally not presented as enterprise IAM. It
provides deterministic alpha behavior and a seam for OPA or another external
policy decision point without changing enforcement call sites.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from opsgraph.domain import Obligation, PolicyDecision, Principal


class ActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal: Principal
    action: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=256)
    context: dict[str, Any] = Field(default_factory=dict)


class PolicyEvaluator(Protocol):
    def evaluate(self, request: ActionRequest) -> PolicyDecision: ...


class FailClosedPolicy:
    """Convert evaluator failures and malformed allows into explicit denies."""

    def __init__(self, evaluator: PolicyEvaluator) -> None:
        self.evaluator = evaluator

    def authorize(self, request: ActionRequest) -> PolicyDecision:
        if request.workspace_id != request.principal.workspace_id:
            return PolicyDecision(reason="workspace boundary mismatch")
        try:
            decision = self.evaluator.evaluate(request)
        except Exception:
            return PolicyDecision(reason="policy evaluator unavailable")
        if decision.allowed and decision.obligations is None:
            return PolicyDecision(reason="allow decision omitted mandatory obligations")
        return decision


class StaticPolicyEvaluator:
    """Exact-match alpha policy; unmatched action-role pairs are denied."""

    def __init__(self, rules: dict[tuple[str, str], Obligation]) -> None:
        self._rules = dict(rules)

    def evaluate(self, request: ActionRequest) -> PolicyDecision:
        matches = [
            (role, self._rules[(role, request.action)])
            for role in sorted(request.principal.roles)
            if (role, request.action) in self._rules
        ]
        if not matches:
            return PolicyDecision(reason="no matching allow rule")
        role, obligation = matches[0]
        return PolicyDecision(
            allowed=True,
            reason=f"allowed by role {role}",
            policy_id=f"alpha-static:{role}:{request.action}",
            obligations=obligation,
        )
