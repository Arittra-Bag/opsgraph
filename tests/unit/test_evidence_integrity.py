"""Typed query results retain their original canonical digest input."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from opsgraph.domain import EvidenceArtifact
from opsgraph.domain.models import stable_hash


def test_canonical_input_preserves_typed_rows_and_legacy_display_contract():
    rows = (
        (
            Decimal("12.3400"),
            date(2026, 9, 12),
            datetime(2026, 9, 12, tzinfo=UTC),
            UUID("12345678-1234-5678-1234-567812345678"),
            b"bytes",
            1.0,
            {"nested": [Decimal("0.10"), b"\xff"]},
        ),
    )
    fields = dict(
        workspace_id="test",
        query_fingerprint="query-hash",
        referenced_tables=("public.records",),
        columns=("decimal", "date", "datetime", "uuid", "bytes", "float", "nested"),
        rows=rows,
        truncated=False,
    )
    artifact = EvidenceArtifact.from_result(**fields)
    exact = artifact.canonical_hash_input()
    assert "sha256:" + hashlib.sha256(exact.encode("utf-8")).hexdigest() == artifact.evidence_hash
    assert artifact.evidence_hash == stable_hash(fields)
    canonical_rows = json.loads(exact)["rows"][0]
    assert canonical_rows[0] == {"$decimal": "12.3400"}
    assert canonical_rows[1] == {"$date": "2026-09-12"}
    assert canonical_rows[3] == {"$uuid": str(rows[0][3])}
    assert canonical_rows[4] == {"$bytes_b64": "Ynl0ZXM="}
    assert canonical_rows[6]["nested"][1] == {"$bytes_b64": "/w=="}
    # Existing JSON rows cannot represent arbitrary non-UTF8 bytea. Do not silently
    # change that response contract while adding independent canonical verification.
    with pytest.raises(UnicodeDecodeError):
        artifact.model_dump(mode="json")
    display = EvidenceArtifact.from_result(
        **{**fields, "rows": (rows[0][:6],), "columns": fields["columns"][:6]}
    )
    assert display.model_dump(mode="json")["rows"][0] == [
        "12.3400",
        "2026-09-12",
        "2026-09-12T00:00:00Z",
        str(rows[0][3]),
        "bytes",
        1.0,
    ]
    assert "integrity" not in display.model_dump(mode="json")
