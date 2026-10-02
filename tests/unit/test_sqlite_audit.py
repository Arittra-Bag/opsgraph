import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from opsgraph.audit import AuditChain, AuditIntegrityError, SQLiteAuditChain


def test_sqlite_audit_is_durable_and_concurrency_safe(tmp_path):
    path = tmp_path / "state.db"
    chain = SQLiteAuditChain(path)

    def append(index: int) -> None:
        chain.append(
            workspace_id="one",
            actor="tester",
            action="test.append",
            resource=str(index),
            outcome="allowed",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(100)))

    reopened = SQLiteAuditChain(path)
    verification = AuditChain.verify(reopened.entries)
    assert verification.valid is True
    assert verification.checked_entries == 100


@pytest.mark.parametrize("mutation", ["tamper", "delete"])
def test_sqlite_audit_refuses_to_extend_a_corrupt_retained_chain(tmp_path, mutation):
    path = tmp_path / "state.db"
    chain = SQLiteAuditChain(path)
    for resource in ("one", "two", "three"):
        chain.append(
            workspace_id="alpha",
            actor="tester",
            action="test.append",
            resource=resource,
            outcome="allowed",
            details={"resource": resource},
        )
    with sqlite3.connect(path) as db:
        if mutation == "tamper":
            db.execute("UPDATE audit_entries SET details_json='{}' WHERE sequence=1")
        else:
            db.execute("DELETE FROM audit_entries WHERE sequence=2")

    with pytest.raises(AuditIntegrityError, match="integrity verification failed"):
        chain.append(
            workspace_id="alpha",
            actor="tester",
            action="test.append",
            resource="four",
            outcome="allowed",
        )

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM audit_entries").fetchone()[0] == (
            3 if mutation == "tamper" else 2
        )


def test_verification_does_not_reserve_writer_and_stale_snapshot_fails_closed(tmp_path):
    chain = SQLiteAuditChain(tmp_path / "state.db")
    chain.append(workspace_id="one", actor="test", action="test", resource="one", outcome="allowed")
    with pytest.raises(sqlite3.OperationalError):
        with chain.transaction() as verified:
            # A separate writer can commit while the verified read snapshot remains open.
            with sqlite3.connect(chain.path, timeout=0.1) as other:
                other.execute(
                    "UPDATE audit_entries SET details_json=? WHERE sequence=1",
                    ('{"changed":true}',),
                )
            chain.append_in_transaction(
                verified,
                workspace_id="one",
                actor="test",
                action="test",
                resource="two",
                outcome="allowed",
            )
    assert len(chain.entries) == 1
