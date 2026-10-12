"""Stable revision identifiers for explicitly reviewed source readiness."""

from __future__ import annotations

from typing import Any

from opsgraph.domain.models import stable_hash


def source_readiness_basis(source: dict[str, Any]) -> str:
    """Hash only fields that determine source access; exclude prior probe state."""

    return stable_hash(
        {
            key: source.get(key)
            for key in (
                "id",
                "secret_ref",
                "allowed_schemas",
                "allowed_tables",
                "allow_external_egress",
                "schema_version",
                "inspected_at",
                *(("connection_revision",) if "connection_revision" in source else ()),
            )
        }
    )


def reviewed_source_matches(
    review: dict[str, Any], source: dict[str, Any], workspace_id: str, receipts: tuple
) -> bool:
    """Match immutable reviews, including legacy bindings proved by verified receipts.

    Callers must verify the audit chain before trusting receipts. A successful
    readiness transaction records the exact previous source and its access basis;
    it can therefore bridge a legacy whole-source review without rewriting it.
    Older refreshes without that proof cannot safely bridge an unmatched review.
    """
    basis = source_readiness_basis(source)
    if "source_access_revision" in review:
        return review["source_access_revision"] == basis
    revision = review.get("source_revision")
    if not isinstance(revision, str):
        return False
    if revision == stable_hash(source):
        return True
    return any(
        entry.workspace_id == workspace_id
        and entry.action == "core.source.readiness"
        and entry.outcome == "allowed"
        and entry.resource == source["id"]
        and entry.details.get("previous_source_revision") == revision
        and entry.details.get("source_access_revision") == basis
        for entry in receipts
    )
