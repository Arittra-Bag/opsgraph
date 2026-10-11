"""Independent, in-memory reference for the hand-labelled export fixtures.

No production reducer, SQL, model, database, or source authorization is used.
Inputs are complete synthetic records, never a broker's retained-row sample.
The vocabulary is deliberately fixed independently of WorkOutcomeContract.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta

STATES = (
    "ready",
    "cancelled",
    "terminal_failure",
    "pending_within_deadline",
    "overdue_unresolved",
    "unknown",
)


class InvalidPopulation(ValueError):
    """The reference cannot establish a unique, scoped promise population."""


def instant(value):  # noqa: ANN001
    if not isinstance(value, str):
        raise ValueError("a recorded, timezone-aware timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive timestamps do not establish an instant")
    return parsed.astimezone(UTC)


def reconcile(data, scope, *, sla_seconds=None):  # noqa: ANN001
    if data.get("promises_truncated", False):
        raise InvalidPopulation("truncated promises cannot establish the population")
    start, end, cutoff = (instant(scope[key]) for key in ("start", "end", "as_of"))
    if not start < end <= cutoff or not scope["tenant_id"]:
        raise InvalidPopulation("invalid tenant or population window")
    promises = {}
    known_keys = set()
    for row in data["promises"]:
        if row["tenant_id"] is None:
            raise InvalidPopulation("promise tenant is missing")
        if row["tenant_id"] != scope["tenant_id"]:
            continue
        if isinstance(row["promise_id"], str) and row["promise_id"].strip():
            known_keys.add(row["promise_id"])
        try:
            accepted = instant(row["accepted_at"])
        except ValueError as exc:
            raise InvalidPopulation("promise acceptance time is ambiguous") from exc
        if not start <= accepted < end:
            continue
        key = row["promise_id"]
        if not isinstance(key, str) or not key.strip():
            raise InvalidPopulation("promise key is missing")
        if key in promises:
            raise InvalidPopulation("duplicate promise key")
        promises[key] = row

    events = {
        kind: {key: [] for key in promises} for kind in ("outcomes", "attempts", "cancellations")
    }
    bad = set()
    orphan_count = 0
    unassignable = False
    for kind in events:
        seen = {}
        for row in data.get(kind, []):
            if row["tenant_id"] is None:
                unassignable = True
                continue
            if row["tenant_id"] != scope["tenant_id"]:
                continue
            key = row["promise_id"]
            try:
                occurred = instant(row["occurred_at"])
            except ValueError:
                if key in promises:
                    bad.add(key)
                else:
                    unassignable = True
                continue
            if occurred > cutoff:
                continue
            if key not in promises:
                if key not in known_keys:
                    orphan_count += 1
                continue
            event_id = row["event_id"]
            if not isinstance(event_id, str) or not event_id.strip():
                bad.add(key)
                continue
            if event_id in seen:
                if seen[event_id] != row:
                    bad.update((key, seen[event_id]["promise_id"]))
                continue
            seen[event_id] = row
            events[kind][key].append(row)

    try:
        complete = instant(data["coverage"]["complete_through"]) >= cutoff
    except (KeyError, TypeError, ValueError):
        complete = False
    # Unassignable/orphan evidence cannot establish clean negative membership.
    complete = complete and not unassignable and not orphan_count
    records = {}
    for key, promise in promises.items():
        state, breach = "unknown", None
        try:
            due = (
                instant(promise["accepted_at"]) + timedelta(seconds=sla_seconds)
                if sla_seconds is not None
                else instant(promise["due_at"])
            )
            if due < instant(promise["accepted_at"]):
                raise ValueError("deadline precedes acceptance")
        except (KeyError, TypeError, ValueError):
            records[key] = {"state": state, "deadline_breached": breach}
            continue
        rows = {kind: events[kind][key] for kind in events}
        valid_statuses = {
            "outcomes": {"ready", "terminal_failure"},
            "attempts": {"queued", "running", "failed", "succeeded"},
            "cancellations": {"cancelled"},
        }
        ambiguous = key in bad or not promise["customer_id"]
        for kind, values in rows.items():
            ambiguous |= any(
                row["status"] not in valid_statuses[kind]
                or instant(row["occurred_at"]) < instant(promise["accepted_at"])
                for row in values
            )
        ready = [
            instant(row["occurred_at"]) for row in rows["outcomes"] if row["status"] == "ready"
        ]
        failed = any(row["status"] == "terminal_failure" for row in rows["outcomes"])
        cancelled = [instant(row["occurred_at"]) for row in rows["cancellations"]]
        # Conflicting customer terminal facts require clarification, not precedence invented by us.
        ambiguous |= sum((bool(ready), failed, bool(cancelled))) > 1
        if not ambiguous:
            if ready:
                state = "ready"
                breach = False if min(ready) <= due else (True if complete else None)
            elif cancelled:
                state = "cancelled"
                breach = False if min(cancelled) <= due else (True if complete else None)
            elif failed:
                state = "terminal_failure"
                breach = cutoff >= due if complete else None
            elif complete:
                breach = cutoff >= due
                state = "overdue_unresolved" if breach else "pending_within_deadline"
        records[key] = {"state": state, "deadline_breached": breach}

    counts = dict.fromkeys(STATES, 0)
    counts.update(Counter(row["state"] for row in records.values()))
    overdue = sorted(
        key for key, value in records.items() if value["state"] == "overdue_unresolved"
    )
    customers = {promises[key]["customer_id"] for key in overdue}
    return {
        "counts": counts,
        "records": records,
        "overdue_ids": overdue,
        "affected_customers": len(customers),
        "orphan_events": orphan_count,
    }
