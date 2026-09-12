"""Transactional local investigation records and replayable execution events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

TERMINAL = frozenset({"completed", "failed", "blocked", "interrupted", "cancelled"})


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class QueueFull(ValueError):
    pass


class RunStore:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    workspace_id TEXT NOT NULL, id TEXT NOT NULL,
                    request_id TEXT, status TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, id), UNIQUE(workspace_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS run_schema_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO run_schema_version VALUES (1);
            """)
        self.import_legacy()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_legacy(self):
        """Expose preserved real alpha results without inventing missing provenance."""
        with self.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_records'"
            ).fetchone()
            if not exists:
                return
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT workspace_id,value_json FROM workspace_records").fetchall()
            for workspace, payload in rows:
                record = json.loads(payload)
                # Alpha persisted connected results only. Still require its exact real-result
                # shape and explicitly exclude known replay/synthetic provenance.
                if (
                    record.get("record_type") != "investigation"
                    or not record.get("source_id")
                    or record["source_id"] == "sample-saas"
                    or record.get("kind") == "synthetic"
                    or record.get("mode") == "sample"
                    or not isinstance(record.get("answer"), dict)
                    or not isinstance(record.get("plan"), dict)
                ):
                    continue
                if any(
                    item.get("source") == "sample-saas" or item.get("kind") == "synthetic"
                    for item in record.get("evidence", [])
                ):
                    continue
                if (
                    not record.get("id")
                    or db.execute(
                        "SELECT 1 FROM runs WHERE workspace_id=? AND id=?",
                        (workspace, record["id"]),
                    ).fetchone()
                ):
                    continue
                value = {
                    "id": record["id"],
                    "source_id": record["source_id"],
                    "question": record.get("question", ""),
                    "skill_id": record.get("skill_id"),
                    "parent_run_id": None,
                    "retry_of": None,
                    "request_id": None,
                    "status": "completed",
                    "created_at": None,
                    "updated_at": timestamp(),
                    "started_at": None,
                    "finished_at": None,
                    "partial_evidence": False,
                    "error": None,
                    "plan": record["plan"],
                    "evidence": record.get("evidence", []),
                    "answer": record["answer"],
                    "configuration": None,
                    "last_event_id": 0,
                    "legacy_provenance": True,
                }
                db.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?)",
                    (workspace, value["id"], None, "completed", json.dumps(value)),
                )
                self._event(
                    db,
                    workspace,
                    value,
                    "legacy_imported",
                    {"note": "Original run timestamps and missing provenance are unavailable."},
                )

    def create(self, workspace: str, body: dict[str, Any], *, retry_of=None):
        now = timestamp()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if body.get("request_id"):
                existing = db.execute(
                    "SELECT value FROM runs WHERE workspace_id=? AND request_id=?",
                    (workspace, body["request_id"]),
                ).fetchone()
                if existing:
                    value = json.loads(existing[0])
                    for key in ("question", "source_id", "skill_id", "parent_run_id"):
                        expected = body.get(key) or (
                            "generic-readonly" if key == "skill_id" else None
                        )
                        if value.get(key) != expected:
                            raise ValueError("request_id already belongs to a different request")
                    return value
            count = db.execute(
                "SELECT COUNT(*) FROM runs WHERE workspace_id=? AND status='queued'",
                (workspace,),
            ).fetchone()[0]
            if count >= 10:
                raise QueueFull("Investigation queue is full; wait for a run to finish.")
            value = dict(
                id="inv-" + uuid4().hex,
                source_id=body["source_id"],
                question=body["question"],
                skill_id=body.get("skill_id") or "generic-readonly",
                parent_run_id=body.get("parent_run_id"),
                retry_of=retry_of,
                request_id=body.get("request_id"),
                status="queued",
                created_at=now,
                updated_at=now,
                started_at=None,
                finished_at=None,
                partial_evidence=False,
                error=None,
                plan=None,
                evidence=[],
                answer=None,
                configuration=None,
                last_event_id=0,
            )
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                (workspace, value["id"], value["request_id"], "queued", json.dumps(value)),
            )
            return self._event(db, workspace, value, "queued", {})

    def get(self, workspace: str, run_id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM runs WHERE workspace_id=? AND id=?", (workspace, run_id)
            ).fetchone()
        if row is None:
            raise KeyError("run not found")
        return json.loads(row[0])

    def list(self, workspace: str, limit: int = 100):
        with self.connect() as db:
            rows = db.execute(
                "SELECT value FROM runs WHERE workspace_id=? ORDER BY rowid DESC LIMIT ?",
                (workspace, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def events(self, workspace: str, run_id: str, after: int):
        self.get(workspace, run_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT value FROM run_events WHERE workspace_id=? AND run_id=? "
                "AND sequence>? ORDER BY sequence",
                (workspace, run_id, after),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, workspace: str, run_id: str, kind: str, data=None, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM runs WHERE workspace_id=? AND id=?", (workspace, run_id)
            ).fetchone()
            if row is None:
                raise KeyError("run not found")
            value = json.loads(row[0])
            if value["status"] in TERMINAL:
                return value
            if value["status"] == "cancelling" and changes.get("status") != "cancelled":
                return value
            value.update(changes)
            if kind == "evidence_captured":
                value["evidence"].append(data["evidence"])
            value["partial_evidence"] = bool(value["evidence"]) and value["status"] != "completed"
            return self._event(db, workspace, value, kind, data or {})

    def cancel(self, workspace: str, run_id: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM runs WHERE workspace_id=? AND id=?", (workspace, run_id)
            ).fetchone()
            if row is None:
                raise KeyError("run not found")
            value = json.loads(row[0])
            if value["status"] in TERMINAL or value["status"] == "cancelling":
                return value
            value["status"] = "cancelled" if value["status"] == "queued" else "cancelling"
            if value["status"] == "cancelled":
                value["finished_at"] = timestamp()
            value["partial_evidence"] = bool(value["evidence"])
            return self._event(db, workspace, value, value["status"], {})

    def claim(self, workspace: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT workspace_id,value FROM runs WHERE workspace_id=? "
                "AND status='queued' ORDER BY rowid LIMIT 1",
                (workspace,),
            ).fetchone()
            if row is None:
                return None
            workspace, payload = row
            value = json.loads(payload)
            value.update(status="running", started_at=timestamp())
            return workspace, self._event(db, workspace, value, "started", {})

    def recover(self, workspace: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT workspace_id,value FROM runs WHERE workspace_id=? "
                "AND status IN ('running','cancelling')",
                (workspace,),
            ).fetchall()
            for workspace, payload in rows:
                value = json.loads(payload)
                value.update(
                    status="interrupted",
                    finished_at=timestamp(),
                    partial_evidence=bool(value["evidence"]),
                    error={
                        "code": "backend_interrupted",
                        "message": (
                            "Backend stopped. Captured evidence is preserved; "
                            "retry starts a fresh attempt."
                        ),
                    },
                )
                self._event(db, workspace, value, "interrupted", {})

    @staticmethod
    def _event(db, workspace, value, kind, data):
        value["last_event_id"] += 1
        value["updated_at"] = timestamp()
        event = {
            "id": value["last_event_id"],
            "run_id": value["id"],
            "type": kind,
            "created_at": value["updated_at"],
            "data": data,
        }
        if kind == "evidence_captured":
            event["data"] = {
                "index": data["index"],
                "evidence_hash": data["evidence"]["evidence_hash"],
                "capture_id": data["evidence"].get("provenance", {}).get("capture_id"),
            }
        elif kind == "plan_completed":
            event["data"] = {"query_count": len(data["plan"]["queries"])}
        db.execute(
            "INSERT INTO run_events VALUES(?,?,?,?)",
            (workspace, value["id"], event["id"], json.dumps(event)),
        )
        db.execute(
            "UPDATE runs SET status=?,value=? WHERE workspace_id=? AND id=?",
            (value["status"], json.dumps(value), workspace, value["id"]),
        )
        return value
