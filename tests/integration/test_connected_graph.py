from opsgraph.brokers import QueryResult
from opsgraph.domain import Obligation, Principal, ToolDefinition, ToolRegistry
from opsgraph.orchestration.connected import run_connected
from opsgraph.providers import (
    ProviderCapabilities,
    ProviderConfig,
    ProviderHealth,
    StructuredResponse,
)
from opsgraph.schema_service import ColumnSchema, SchemaSnapshot, TableSchema
from opsgraph.skills import SkillDefinition, SkillRepository, ToolBinding, ToolSettings


class StubProvider:
    config = ProviderConfig(kind="deterministic", model="stub")
    capabilities = ProviderCapabilities(external_egress=False)

    def health(self):
        return ProviderHealth(
            status="ready", provider="deterministic", model="stub", detail="ready"
        )

    def invoke_structured(self, request):
        title = request.response_schema.get("title")
        if title == "InvestigationPlan":
            output = {
                "queries": [
                    {
                        "purpose": "Inspect failed jobs",
                        "sql": "SELECT id, status FROM public.jobs",
                        "evidence_types": ["job_status", "queue_state"],
                    }
                ],
                "rationale": "The question asks about job failures.",
            }
        else:
            output = {
                "summary": "One failed job is present.",
                "findings": [
                    {
                        "claim": "A failed job exists.",
                        "classification": "supported",
                        "evidence_ids": ["placeholder"],
                    }
                ],
                "limitations": ["Only one bounded query was run."],
            }
            # The graph requires the exact generated evidence identifier.
            evidence = request.messages[0].content.split('"evidence_hash": "', 1)[1]
            output["findings"][0]["evidence_ids"] = [evidence.split('"', 1)[0]]
        return StructuredResponse(provider="deterministic", model="stub", output=output)


class StubExecutor:
    def execute_readonly(self, sql, *, timeout_ms):
        assert "SELECT * FROM" in sql
        assert timeout_ms == 5_000
        return QueryResult(columns=("id", "status"), rows=((1, "failed"),))


def skills(*, max_rows: int = 100, egress: str = "forbidden") -> SkillRepository:
    registry = ToolRegistry(lambda ddl: ddl)
    registry.register(ToolDefinition(name="core.sql.select", description="read", handler=None))
    repository = SkillRepository(
        tools=registry,
        policy_ceiling=Obligation(max_rows=100, allowed_schemas=("public",)),
    )
    definition = SkillDefinition(
        id="failed-jobs",
        version="0.1.0",
        name="Failed jobs",
        purpose="Investigate failed background jobs safely.",
        egress=egress,
        required_evidence=("job_status", "queue_state"),
        tools=(
            ToolBinding(tool="core.schema.inspect"),
            ToolBinding(
                tool="core.sql.select",
                settings=ToolSettings(max_rows=max_rows, allowed_schemas=("public",)),
            ),
        ),
    )
    repository.save_draft(definition)
    repository.publish(definition.id)
    return repository


def test_connected_graph_routes_executes_and_cites_question_dependent_evidence():
    principal = Principal(subject="tester", workspace_id="workspace", roles={"analyst"})
    obligations = Obligation(allowed_schemas=("public",))
    snapshot = SchemaSnapshot(
        tables=(
            TableSchema(
                schema_name="public",
                table_name="jobs",
                columns=(ColumnSchema(name="id", data_type="bigint"),),
            ),
        ),
        fingerprint="sha256:" + "1" * 64,
    )

    result = run_connected(
        question="Why did the worker job fail?",
        provider=StubProvider(),
        principal=principal,
        obligations=obligations,
        skills=skills(),
        executor=StubExecutor(),
        snapshot=snapshot,
    )

    assert result["skill_id"] == "failed-jobs"
    assert result["evidence"][0]["rows"] == [[1, "failed"]]
    assert result["answer"]["findings"][0]["evidence_ids"] == [
        result["evidence"][0]["evidence_hash"]
    ]


def test_connected_graph_applies_skill_row_bound_and_egress_policy():
    class ExternalStub(StubProvider):
        capabilities = ProviderCapabilities(external_egress=True)

    principal = Principal(subject="tester", workspace_id="workspace", roles={"analyst"})
    snapshot = SchemaSnapshot(
        tables=(
            TableSchema(
                schema_name="public",
                table_name="jobs",
                columns=(ColumnSchema(name="id", data_type="bigint"),),
            ),
        ),
        fingerprint="sha256:" + "1" * 64,
    )

    import pytest

    with pytest.raises(PermissionError, match="forbids external model egress"):
        run_connected(
            question="Why did the worker job fail?",
            provider=ExternalStub(),
            principal=principal,
            obligations=Obligation(allowed_schemas=("public",)),
            skills=skills(egress="forbidden"),
            executor=StubExecutor(),
            snapshot=snapshot,
        )

    class RecordingExecutor(StubExecutor):
        sql = ""

        def execute_readonly(self, sql, *, timeout_ms):
            self.sql = sql
            return QueryResult(columns=("id", "status"), rows=((1, "failed"),))

    executor = RecordingExecutor()
    run_connected(
        question="Why did the worker job fail?",
        provider=ExternalStub(),
        principal=principal,
        obligations=Obligation(max_rows=100, allowed_schemas=("public",)),
        skills=skills(max_rows=5, egress="allowlisted"),
        executor=executor,
        snapshot=snapshot,
    )
    assert executor.sql.endswith("LIMIT 6")


def test_connected_graph_honours_an_explicit_custom_skill_selection():
    principal = Principal(subject="tester", workspace_id="workspace", roles={"analyst"})
    snapshot = SchemaSnapshot(
        tables=(
            TableSchema(
                schema_name="public",
                table_name="jobs",
                columns=(ColumnSchema(name="id", data_type="bigint"),),
            ),
        ),
        fingerprint="sha256:" + "2" * 64,
    )
    repository = skills()
    custom = SkillDefinition(
        id="my-readonly-skill",
        version="0.1.0",
        name="My read-only skill",
        purpose="Use bounded queries to inspect job state.",
        required_evidence=("job_status", "queue_state"),
        tools=(
            ToolBinding(tool="core.schema.inspect"),
            ToolBinding(tool="core.sql.select"),
        ),
    )
    repository.save_draft(custom)
    repository.publish(custom.id)
    result = run_connected(
        question="Why did the worker job fail?",
        provider=StubProvider(),
        principal=principal,
        obligations=Obligation(allowed_schemas=("public",)),
        skills=repository,
        executor=StubExecutor(),
        snapshot=snapshot,
        skill_id=custom.id,
    )
    assert result["skill_id"] == custom.id
