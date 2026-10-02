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
            )
        }
    )
