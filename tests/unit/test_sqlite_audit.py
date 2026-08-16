from concurrent.futures import ThreadPoolExecutor

from opsgraph.audit import AuditChain, SQLiteAuditChain


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
