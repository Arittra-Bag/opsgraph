import sqlite3

import pytest

from opsgraph.audit import AuditIntegrityError, SQLiteAuditChain
from opsgraph.config import get_settings
from opsgraph.persistence import SQLiteWorkspaceStore, WorkspaceRecord
from opsgraph.runtime import build_runtime
from opsgraph.setup import SetupError


def definition(skill_id: str, version: str) -> dict:
    return {
        "id": skill_id,
        "version": version,
        "name": f"Recovery {version}",
        "origin": "custom",
        "purpose": "Verify exact active skill recovery after a process restart.",
        "tools": [
            {"tool": "core.schema.inspect", "enabled": True, "settings": {}},
            {"tool": "core.sql.select", "enabled": True, "settings": {}},
        ],
        "required_evidence": [],
        "conclusion_classes": ["supported", "possible", "unknown", "contradictory"],
        "egress": "forbidden",
        "risk": "read_only",
    }


def persisted_versions(tmp_path, *, pointer: str | None):
    settings = get_settings().model_copy(update={"state_path": tmp_path / "state.db"})
    store = SQLiteWorkspaceStore(settings.state_path)
    audit = SQLiteAuditChain(settings.state_path)
    skill_id = "restart-order"
    for version in ("1.2.0", "1.10.0"):
        record_id = f"skill-published:{skill_id}:{version}"
        store.put(
            WorkspaceRecord(
                settings.workspace_id,
                record_id,
                {"record_type": "skill_published", "definition": definition(skill_id, version)},
            )
        )
        audit.append(
            workspace_id=settings.workspace_id,
            actor="local-operator",
            action="core.skill.manage",
            resource=skill_id,
            outcome="allowed",
            details={"operation": "publish", "version": version},
        )
    if pointer is not None:
        store.put(
            WorkspaceRecord(
                settings.workspace_id,
                f"skill-current:{skill_id}",
                {
                    "record_type": "skill_current",
                    "skill_id": skill_id,
                    "version": pointer,
                    "published_record_id": f"skill-published:{skill_id}:{pointer}",
                },
            )
        )
    return settings


@pytest.mark.parametrize("pointer", ["1.10.0", None])
def test_runtime_recovers_active_skill_from_pointer_or_legacy_audit_order(tmp_path, pointer):
    settings = persisted_versions(tmp_path, pointer=pointer)

    runtime = build_runtime(settings)

    assert runtime.skills.versions("restart-order") == ("1.10.0", "1.2.0")
    assert runtime.skills.get_published("restart-order").version == "1.10.0"


def test_runtime_rejects_a_dangling_active_skill_pointer(tmp_path):
    settings = persisted_versions(tmp_path, pointer="9.9.9")

    with pytest.raises(SetupError, match="active skill metadata is inconsistent"):
        build_runtime(settings)


def test_runtime_rejects_a_corrupt_audit_chain_before_loading_state(tmp_path):
    settings = get_settings().model_copy(update={"state_path": tmp_path / "state.db"})
    SQLiteWorkspaceStore(settings.state_path)
    audit = SQLiteAuditChain(settings.state_path)
    audit.append(
        workspace_id=settings.workspace_id,
        actor="local-operator",
        action="test.append",
        resource="state",
        outcome="allowed",
        details={"original": True},
    )
    with sqlite3.connect(settings.state_path) as db:
        db.execute("UPDATE audit_entries SET details_json='{}' WHERE sequence=1")

    with pytest.raises(AuditIntegrityError, match="integrity verification failed"):
        build_runtime(settings)
