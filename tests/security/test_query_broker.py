import pytest

from opsgraph.brokers import QueryBroker, QueryResult, SelectOnlyValidator, UnsafeQuery
from opsgraph.brokers import query as query_module
from opsgraph.domain import Obligation, Principal
from opsgraph.policy import FailClosedPolicy, StaticPolicyEvaluator


class RecordingExecutor:
    def __init__(self) -> None:
        self.sql = ""
        self.timeout_ms = 0

    def execute_readonly(self, sql: str, *, timeout_ms: int) -> QueryResult:
        self.sql = sql
        self.timeout_ms = timeout_ms
        return QueryResult(("id",), tuple((value,) for value in range(4)))


def obligations() -> Obligation:
    return Obligation(
        max_rows=3,
        timeout_ms=900,
        allowed_schemas=("public",),
        allowed_tables=("public.incidents",),
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM incidents; DELETE FROM incidents",
        "UPDATE incidents SET title = 'x'",
        "DELETE FROM incidents RETURNING *",
        "CREATE TABLE stolen(id int)",
        "COPY incidents TO '/tmp/leak'",
        "CALL do_work()",
        "DO $$ BEGIN END $$",
        "SELECT * INTO copied FROM incidents",
        "SELECT * FROM incidents FOR UPDATE",
        "SELECT pg_read_file('/etc/passwd') FROM incidents",
        "SELECT unknown_extension(id) FROM incidents",
        "SELECT * FROM private.incidents",
        "SELECT * FROM public.users",
        "SELECT * FROM incidents -- hide mutation",
        "SELECT * FROM incidents",
        'SELECT * FROM "private.x".secrets',
        "SELECT public.count(id) FROM public.incidents",
        "SELECT id OPERATOR(public.+) 1 FROM public.incidents",
        "SELECT id::public.custom_type FROM public.incidents",
        "SELECT CAST(id AS public.custom_type) FROM public.incidents",
    ],
)
def test_validator_rejects_unsafe_sql(sql: str) -> None:
    with pytest.raises(UnsafeQuery):
        SelectOnlyValidator().validate(workspace_id="local", sql=sql, obligations=obligations())


def test_validator_rejects_postgres_only_scope_bypass() -> None:
    pytest.importorskip("pglast")
    with pytest.raises(UnsafeQuery, match="outside policy scope"):
        SelectOnlyValidator().validate(
            workspace_id="local",
            sql="SELECT * FROM ONLY private.incidents",
            obligations=obligations(),
        )


def test_validator_scopes_cte_source_without_treating_cte_as_a_table() -> None:
    pytest.importorskip("pglast")
    plan = SelectOnlyValidator().validate(
        workspace_id="local",
        sql="WITH recent AS (SELECT id FROM public.incidents) SELECT id FROM recent",
        obligations=obligations(),
    )
    assert plan.referenced_tables == ("public.incidents",)


def test_validator_fails_closed_when_ast_parser_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(query_module, "_parse_sql", None)
    monkeypatch.setattr(query_module, "_pg_ast", None)
    with pytest.raises(UnsafeQuery, match="AST parser is unavailable"):
        SelectOnlyValidator().validate(
            workspace_id="local",
            sql="SELECT * FROM public.incidents",
            obligations=obligations(),
        )


def test_broker_authorizes_bounds_and_hashes_evidence() -> None:
    executor = RecordingExecutor()
    policy = FailClosedPolicy(
        StaticPolicyEvaluator({("analyst", "core.query.read"): obligations()})
    )
    broker = QueryBroker(policy=policy, validator=SelectOnlyValidator(), executor=executor)
    principal = Principal(subject="dev", workspace_id="local", roles=frozenset({"analyst"}))
    evidence = broker.query(
        principal=principal,
        sql="SELECT id, count(id) FROM public.incidents GROUP BY id",
    )
    assert executor.timeout_ms == 900
    assert executor.sql.endswith("LIMIT 4")
    assert len(evidence.rows) == 3
    assert evidence.truncated is True
    assert evidence.evidence_hash.startswith("sha256:")


def test_normalization_preserves_literal_content() -> None:
    plan = SelectOnlyValidator().validate(
        workspace_id="local",
        sql="SELECT *  FROM public.incidents WHERE title = 'delete  this; no'",
        obligations=obligations(),
    )
    assert "'delete  this; no'" in plan.normalized_sql


def test_policy_failure_denies_without_calling_executor() -> None:
    class BrokenPolicy:
        def evaluate(self, request):
            raise RuntimeError("unavailable")

    executor = RecordingExecutor()
    broker = QueryBroker(
        policy=FailClosedPolicy(BrokenPolicy()),
        validator=SelectOnlyValidator(),
        executor=executor,
    )
    principal = Principal(subject="dev", workspace_id="local", roles=frozenset({"analyst"}))
    with pytest.raises(PermissionError, match="unavailable"):
        broker.query(principal=principal, sql="SELECT * FROM incidents")
    assert executor.sql == ""
