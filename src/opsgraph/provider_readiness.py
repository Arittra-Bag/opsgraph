"""Process-local verification of an explicitly requested model connection check."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

PROBE_FRESHNESS_SECONDS = 15 * 60


@dataclass
class ProviderVerification:
    """Access under the runtime provider lock. Restart never restores verification."""

    revision: str | None = None
    generation: str | None = None
    status: str = "untested"
    checked_at: str | None = None
    expires_at: str | None = None
    deadline: float = 0

    def begin(self, revision: str) -> str:
        self.revision = revision
        self.generation = uuid4().hex
        self.status = "checking"
        self.checked_at = self.expires_at = None
        self.deadline = 0
        return self.generation

    def finish(self, revision: str, generation: str, *, success: bool) -> bool:
        if (revision, generation) != (self.revision, self.generation):
            return False
        now = datetime.now(UTC)
        self.status = "verified" if success else "failed"
        self.checked_at = now.isoformat()
        self.expires_at = (
            (now + timedelta(seconds=PROBE_FRESHNESS_SECONDS)).isoformat() if success else None
        )
        self.deadline = monotonic() + PROBE_FRESHNESS_SECONDS if success else 0
        return True

    def public(self, revision: str) -> dict:
        status = self.status if revision == self.revision else "untested"
        if status == "verified" and monotonic() >= self.deadline:
            status = "expired"
        details = {
            "untested": "Save your model settings, then run a connection check.",
            "checking": "A model connection check is running.",
            "verified": "Recent model connection check passed. Source readiness is separate.",
            "failed": "The last model connection check failed. Correct the setup and try again.",
            "expired": "The model connection check has expired. Run it again before investigating.",
        }
        return {
            "status": status,
            "configuration_revision": revision,
            "checked_at": self.checked_at if revision == self.revision else None,
            "expires_at": self.expires_at if revision == self.revision else None,
            "valid_for_seconds": max(0, self.deadline - monotonic()) if status == "verified" else 0,
            "detail": details[status],
        }
