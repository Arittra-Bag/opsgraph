from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

FindingClass = Literal["supported", "possible", "unknown", "contradictory"]


class Evidence(BaseModel):
    id: str
    source: str
    observed_at: datetime
    excerpt: str
    query: str
    digest: str


class Finding(BaseModel):
    classification: FindingClass
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    limitation: str | None = None


class TraceStep(BaseModel):
    id: str
    label: str
    detail: str
    status: Literal["queued", "running", "complete", "denied", "failed"] = "complete"


class InvestigationResult(BaseModel):
    id: str
    title: str
    status: Literal["complete", "partial", "denied"] = "complete"
    source: str
    playbook: str
    policy_version: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    summary: str
    findings: list[Finding]
    evidence: list[Evidence]
    trace: list[TraceStep]
    queries: list[str]
    limitations: list[str]
