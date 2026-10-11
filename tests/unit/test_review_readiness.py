"""Stable review bindings and authenticated legacy readiness bridges."""

from copy import deepcopy

import pytest

from opsgraph.audit import AuditChain
from opsgraph.domain.models import stable_hash
from opsgraph.readiness import reviewed_source_matches, source_readiness_basis

SOURCE = {
    "id": "exports-db",
    "secret_ref": "APPROVED_EXPORTS_DSN",
    "allowed_schemas": ["public"],
    "allowed_tables": ["public.exports"],
    "allow_external_egress": False,
    "schema_version": "reviewed-schema",
    "inspected_at": "2026-10-11T11:00:00Z",
    "readiness": {"checked_at": "before", "status": "ready"},
}


def bridge(source, **overrides):
    fields = {
        "workspace_id": "workspace-a",
        "actor": "operator",
        "action": "core.source.readiness",
        "resource": source["id"],
        "outcome": "allowed",
        "details": {
            "previous_source_revision": stable_hash(source),
            "source_access_revision": source_readiness_basis(source),
        },
    }
    return AuditChain().append(**{**fields, **overrides})


@pytest.mark.parametrize("legacy", [False, True])
def test_refresh_keeps_the_review_and_requires_a_proved_legacy_binding(legacy):
    review = {"source_revision": stable_hash(SOURCE)}
    if not legacy:
        review["source_access_revision"] = source_readiness_basis(SOURCE)
    original = deepcopy(review)
    refreshed = {**SOURCE, "readiness": {"checked_at": "after", "status": "ready"}}
    assert reviewed_source_matches(review, SOURCE, "workspace-a", ())
    assert reviewed_source_matches(review, refreshed, "workspace-a", ()) is not legacy
    assert reviewed_source_matches(review, refreshed, "workspace-a", (bridge(SOURCE),))
    assert review == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_id": "workspace-b"},
        {"resource": "other-source"},
        {"action": "core.source.manage"},
        {"outcome": "rejected"},
        {"details": {}},
        {
            "details": {
                "previous_source_revision": "wrong",
                "source_access_revision": source_readiness_basis(SOURCE),
            }
        },
        {
            "details": {
                "previous_source_revision": stable_hash(SOURCE),
                "source_access_revision": "wrong",
            }
        },
    ],
)
def test_unrelated_or_unproved_receipts_cannot_bridge_legacy_reviews(overrides):
    refreshed = {**SOURCE, "readiness": {"checked_at": "after"}}
    review = {"source_revision": stable_hash(SOURCE)}
    assert not reviewed_source_matches(
        review, refreshed, "workspace-a", (bridge(SOURCE, **overrides),)
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "different-source"),
        ("secret_ref", "DIFFERENT_APPROVED_DSN"),
        ("allowed_schemas", ["different"]),
        ("allowed_tables", ["public.exports", "public.extra"]),
        ("allow_external_egress", True),
        ("schema_version", "different-schema"),
        ("inspected_at", "2026-10-11T12:00:00Z"),
    ],
)
def test_access_or_inspection_drift_still_invalidates_review(legacy, field, value):
    review = {"source_revision": stable_hash(SOURCE)}
    if not legacy:
        review["source_access_revision"] = source_readiness_basis(SOURCE)
    changed = {**SOURCE, field: value}
    assert not reviewed_source_matches(review, changed, "workspace-a", (bridge(SOURCE),))
