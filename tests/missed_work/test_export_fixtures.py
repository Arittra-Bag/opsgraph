"""Hand-labelled semantics and large-cohort controls for future SQL/reducers."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from .export_oracle import STATES, InvalidPopulation, reconcile

FIXTURE = json.loads(Path(__file__).with_name("export-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_independent_reference_matches_hand_labelled_records(case):
    data = deepcopy(case["data"])
    kwargs = {"sla_seconds": case["sla_seconds"]} if "sla_seconds" in case else {}
    if "expected_error" in case:
        with pytest.raises(InvalidPopulation, match=case["expected_error"]):
            reconcile(data, FIXTURE["scope"], **kwargs)
        return
    result = reconcile(data, FIXTURE["scope"], **kwargs)
    assert result["records"] == case["expected_records"]
    assert sum(result["counts"].values()) == len(case["expected_records"])
    for state in STATES:
        assert result["counts"][state] == sum(
            label["state"] == state for label in case["expected_records"].values()
        )
    assert result["overdue_ids"] == sorted(
        key
        for key, label in case["expected_records"].items()
        if label["state"] == "overdue_unresolved"
    )
    assert result["orphan_events"] == case.get("expected_orphan_events", 0)
    assert data == case["data"]
    # Input order must not determine first/last status or hide conflicting duplicates.
    for key in ("promises", "outcomes", "attempts", "cancellations"):
        data[key].reverse()
    assert reconcile(data, FIXTURE["scope"], **kwargs) == result


def large_cohort():
    data = {
        "promises": [],
        "outcomes": [],
        "attempts": [],
        "cancellations": [],
        "coverage": {"complete_through": FIXTURE["scope"]["as_of"]},
    }
    groups = [
        ("ready", 930),
        ("cancelled", 20),
        ("terminal_failure", 5),
        ("pending_within_deadline", 30),
        ("overdue_unresolved", 12),
        ("unknown", 3),
    ]
    for state, count in groups:
        for index in range(count):
            key = f"{state}-{index}"
            promise = {
                "promise_id": key,
                "tenant_id": "tenant-a",
                "customer_id": f"customer-{index % 8}",
                "accepted_at": "2026-10-11T11:00:00Z",
                "due_at": "2026-10-11T11:10:00Z",
            }
            if state == "pending_within_deadline":
                promise.update(accepted_at="2026-10-11T11:59:00Z", due_at="2026-10-11T12:09:00Z")
            if state == "unknown":
                promise["due_at"] = None
            data["promises"].append(promise)
            event = {
                "event_id": f"event-{key}",
                "promise_id": key,
                "tenant_id": "tenant-a",
                "occurred_at": "2026-10-11T11:05:00Z",
                "status": state,
            }
            if state in {"ready", "terminal_failure"}:
                data["outcomes"].append(event)
            elif state == "cancelled":
                data["cancellations"].append(event)
            failed_attempts = (
                1 if state == "ready" and index < 81 else (3 if state == "terminal_failure" else 0)
            )
            for attempt in range(failed_attempts):
                data["attempts"].append(
                    {
                        **event,
                        "event_id": f"attempt-{key}-{attempt}",
                        "status": "failed",
                        "occurred_at": "2026-10-11T11:02:00Z",
                    }
                )
    return data


def test_thousand_promises_reconcile_at_outcome_grain_not_attempt_grain():
    data = large_cohort()
    result = reconcile(data, FIXTURE["scope"])
    assert result["counts"] == dict(zip(STATES, (930, 20, 5, 30, 12, 3), strict=True))
    assert sum(result["counts"].values()) == 1000
    assert len(data["attempts"]) == 96
    assert len(result["overdue_ids"]) == 12
    assert result["affected_customers"] == 8


def test_recheck_excludes_new_promises_and_preserves_late_breach_history():
    data = large_cohort()
    before = reconcile(data, FIXTURE["scope"])
    for key in before["overdue_ids"]:
        data["outcomes"].append(
            {
                "event_id": f"recovery-{key}",
                "promise_id": key,
                "tenant_id": "tenant-a",
                "status": "ready",
                "occurred_at": "2026-10-11T12:01:00Z",
            }
        )
    data["promises"].append(
        {
            "promise_id": "new-request",
            "tenant_id": "tenant-a",
            "customer_id": "new-customer",
            "accepted_at": "2026-10-11T12:02:00Z",
            "due_at": "2026-10-11T12:12:00Z",
        }
    )
    data["coverage"]["complete_through"] = "2026-10-11T12:05:00Z"
    after = reconcile(data, {**FIXTURE["scope"], "as_of": "2026-10-11T12:05:00Z"})
    assert after["counts"] == dict(zip(STATES, (942, 20, 5, 30, 0, 3), strict=True))
    assert "new-request" not in after["records"]
    assert all(
        after["records"][key] == {"state": "ready", "deadline_breached": True}
        for key in before["overdue_ids"]
    )
