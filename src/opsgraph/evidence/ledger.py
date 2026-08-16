import hashlib
import json
from datetime import datetime
from typing import Any

from opsgraph.domain.investigation import Evidence


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def evidence_item(
    *, evidence_id: str, source: str, observed_at: datetime, excerpt: str, query: str
) -> Evidence:
    digest = canonical_digest(
        {
            "id": evidence_id,
            "source": source,
            "observed_at": observed_at.isoformat(),
            "excerpt": excerpt,
            "query": query,
        }
    )
    return Evidence(
        id=evidence_id,
        source=source,
        observed_at=observed_at,
        excerpt=excerpt,
        query=query,
        digest=digest,
    )
