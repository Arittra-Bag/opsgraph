import pytest

from opsgraph.brokers import QueryResult
from opsgraph.domain import EvidenceBinding, Obligation, Principal, ToolDefinition, ToolRegistry
from opsgraph.orchestration.connected import (
    ClarificationRequired,
    InvestigationAnswer,
    ModelAnswerInconsistentError,
    ModelOutputInvalidError,
    _apply_application_answer_contract,
    _validate_answer_consistency,
    run_connected,
)
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
                    }
                ],
                "rationale": "The question asks about job failures.",
                "clarification": None,
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
            identifier = evidence.split('"', 1)[0]
            assert request.response_schema["$defs"]["CitedFinding"]["properties"]["evidence_ids"][
                "items"
            ]["enum"] == [identifier]
            output["findings"][0]["evidence_ids"] = [identifier]
        return StructuredResponse(provider="deterministic", model="stub", output=output)


class StubExecutor:
    def execute_readonly(self, sql, *, timeout_ms):
        assert "SELECT * FROM" in sql
        assert timeout_ms == 5_000
        return QueryResult(columns=("id", "status"), rows=((1, "failed"),))


@pytest.mark.parametrize("corrected", [True, False])
def test_empty_parent_replan_runs_only_a_corrected_query(corrected):
    calls, executed, events = [], [], []

    class Provider(StubProvider):
        def invoke_structured(self, request):
            response = super().invoke_structured(request)
            if request.response_schema.get("title") == "InvestigationPlan":
                calls.append(request)
                response.output["queries"][0]["sql"] = (
                    "SELECT j.id, j.status FROM public.jobs j LEFT JOIN public.queues q "
                    "ON q.job_id = j.id"
                    if corrected and len(calls) == 2
                    else "SELECT j.id, j.status FROM public.jobs j JOIN public.queues q "
                    "ON q.job_id = j.id"
                )
            return response

    class Executor(StubExecutor):
        def execute_readonly(self, sql, *, timeout_ms):
            executed.append(sql)
            return super().execute_readonly(sql, timeout_ms=timeout_ms)

    def run():
        return run_connected(
            question=(
                "For each job, including jobs without queues, show status. "
                "queues.job_id references jobs.id."
            ),
            provider=Provider(),
            principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
            obligations=Obligation(allowed_schemas=("public",)),
            skills=skills(),
            executor=Executor(),
            snapshot=SchemaSnapshot(
                tables=tuple(
                    TableSchema(
                        schema_name="public",
                        table_name=name,
                        columns=(ColumnSchema(name="id", data_type="bigint"),),
                    )
                    for name in ("jobs", "queues")
                ),
                fingerprint="fixture",
            ),
            evidence_bindings=bindings(),
            observe=lambda kind, data: events.append(kind),
        )

    if corrected:
        result = run()
        assert len(executed) == len(result["evidence"]) == 1
        assert "LEFT JOIN" in executed[0]
    else:
        with pytest.raises(ModelOutputInvalidError, match="No query executed"):
            run()
        assert executed == []
    assert len(calls) == 2
    assert events.count("plan_validation_retry") == 1


def test_admitted_unsupplied_unit_assumption_clarifies_without_query():
    class Provider(StubProvider):
        def invoke_structured(self, request):
            response = super().invoke_structured(request)
            response.output["rationale"] = (
                "The question does not provide definitions for units. "
                "We must assume that the value is stored in the requested unit."
            )
            return response

    class NoQuery(StubExecutor):
        def execute_readonly(self, *args, **kwargs):
            pytest.fail("Unknown units must be clarified before collecting evidence")

    with pytest.raises(ClarificationRequired, match="unit"):
        run_connected(
            question="Which values exceed 20 kg? All other definitions are unavailable.",
            skill_id="failed-jobs",
            provider=Provider(),
            principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
            obligations=Obligation(allowed_schemas=("public",)),
            skills=skills(),
            executor=NoQuery(),
            snapshot=SchemaSnapshot(tables=(), fingerprint="fixture"),
            evidence_bindings=bindings(),
        )


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


def bindings() -> tuple[EvidenceBinding, ...]:
    return (
        EvidenceBinding(
            evidence_type="job_status",
            source_tables=("public.jobs",),
        ),
        EvidenceBinding(
            evidence_type="queue_state",
            source_tables=("public.jobs",),
        ),
    )


def answer(
    *,
    summary="Two failed records were captured.",
    claims=("Records 2 and 4 have failed status.",),
    limitations=(),
):
    return InvestigationAnswer.model_validate(
        {
            "summary": summary,
            "findings": [
                {
                    "claim": claim,
                    "classification": "supported",
                    "evidence_ids": ["sha256:" + "a" * 64],
                }
                for claim in claims
            ],
            "limitations": list(limitations),
        }
    )


def consistency_evidence(*, proposed_query="SELECT id, status FROM public.jobs"):
    return [
        {
            "columns": ["id", "status"],
            "rows": [[2, "failed"], [4, "failed"]],
            "captured_row_count": 2,
            "truncated": False,
            "proposed_query": proposed_query,
            "limits": {"max_rows": 100},
        }
    ]


def test_answer_consistency_rejects_known_collection_limit_contradictions():
    with pytest.raises(ModelAnswerInconsistentError) as conflict:
        _validate_answer_consistency(
            "Summarize statuses.",
            answer(limitations=("The actual row limit was not enforced.",)),
            consistency_evidence(),
        )
    assert conflict.value.reason == "collection_limit_conflict"

    with pytest.raises(ModelAnswerInconsistentError) as count_limit:
        _validate_answer_consistency(
            "Summarize statuses.",
            answer(limitations=("The query was limited to 2 rows.",)),
            consistency_evidence(),
        )
    assert count_limit.value.reason == "captured_count_is_not_limit"

    with pytest.raises(ModelAnswerInconsistentError) as unsupported_hedge:
        _validate_answer_consistency(
            "Summarize statuses.",
            answer(
                limitations=(
                    "The query used a LIMIT clause to detect truncation, which may affect "
                    "the completeness of the result.",
                )
            ),
            consistency_evidence(),
        )
    assert unsupported_hedge.value.reason == "unsupported_limit_hedge"

    with pytest.raises(ModelAnswerInconsistentError) as affected_rows:
        _validate_answer_consistency(
            "Summarize statuses.",
            answer(
                limitations=(
                    "The executed query included a LIMIT clause that may have affected the "
                    "captured rows, though the actual row limit was not reached.",
                )
            ),
            consistency_evidence(),
        )
    assert affected_rows.value.reason == "unsupported_limit_hedge"

    with pytest.raises(ModelAnswerInconsistentError) as status_filter:
        _validate_answer_consistency(
            "Summarize statuses.",
            answer(
                limitations=(
                    "The query did not filter for a specific status, so the observed statuses "
                    "may not represent the full range of statuses in the table.",
                )
            ),
            consistency_evidence(),
        )
    assert status_filter.value.reason == "unsupported_status_filter_hedge"

    _validate_answer_consistency(
        "Summarize statuses.",
        answer(
            limitations=(
                "The query did not filter for a specific status, so the joined rows may not "
                "represent the full range of statuses in the source table.",
            )
        ),
        consistency_evidence(
            proposed_query=(
                "SELECT jobs.id, jobs.status FROM public.jobs "
                "INNER JOIN public.queues ON queues.job_id = jobs.id"
            )
        ),
    )

    for phrase in (
        "The result was capped at 2 rows.",
        "Only 2 rows were returned because of the row cap.",
        "The maximum result size was 2 rows.",
    ):
        with pytest.raises(ModelAnswerInconsistentError) as rephrased:
            _validate_answer_consistency(
                "Summarize statuses.", answer(limitations=(phrase,)), consistency_evidence()
            )
        assert rephrased.value.reason == "captured_count_is_not_limit"

    _validate_answer_consistency(
        "Summarize statuses.",
        answer(limitations=("The query was explicitly limited to 2 rows.",)),
        consistency_evidence(proposed_query="SELECT id, status FROM public.jobs LIMIT 2"),
    )
    _validate_answer_consistency(
        "Summarize statuses.",
        answer(limitations=("The inner query was limited to two rows.",)),
        consistency_evidence(
            proposed_query=(
                "SELECT id, status FROM "
                "(SELECT id, status FROM public.jobs LIMIT 2) AS bounded_jobs"
            )
        ),
    )
    _validate_answer_consistency(
        "Summarize statuses.",
        answer(
            limitations=(
                "The query did not filter for a specific status and was limited to two rows, "
                "so it may not show all statuses.",
            )
        ),
        consistency_evidence(proposed_query="SELECT id, status FROM public.jobs LIMIT 2"),
    )

    with pytest.raises(ModelAnswerInconsistentError) as spelled_count:
        _validate_answer_consistency(
            "Summarize statuses.",
            answer(limitations=("The query was limited to two rows.",)),
            consistency_evidence(),
        )
    assert spelled_count.value.reason == "captured_count_is_not_limit"


@pytest.mark.parametrize("field", ["summary", "finding", "limitation"])
def test_collection_conflict_is_rejected_from_every_displayed_answer_field(field):
    phrase = "The actual row limit was not enforced in the captured result."
    values = {
        "summary": answer(summary=phrase),
        "finding": answer(claims=(phrase,)),
        "limitation": answer(limitations=(phrase,)),
    }
    with pytest.raises(ModelAnswerInconsistentError) as error:
        _validate_answer_consistency("Summarize statuses.", values[field], consistency_evidence())
    assert error.value.reason == "collection_limit_conflict"


def test_application_owns_requested_causal_limitation_without_judging_model_prose():
    question = "Which records have failed status? Cite evidence and state causal limitations."
    expected = (
        "OpsGraph checks that citations reference captured evidence; this does not validate "
        "source completeness, semantic correctness or causality."
    )
    for model_limitation in (
        "The captured evidence does not establish the cause of the sky being blue.",
        "The captured data show correlation but do not establish causality.",
    ):
        contracted = _apply_application_answer_contract(
            question,
            answer(
                limitations=(
                    model_limitation,
                    "The capture reflects one authorized database snapshot.",
                )
            ),
        )
        assert contracted.limitations == (
            model_limitation,
            "The capture reflects one authorized database snapshot.",
            expected,
        )

    assert _apply_application_answer_contract(question, answer(limitations=())).limitations == (
        expected,
    )

    material = (
        "The query covers only recent rows, which is why older history is unavailable.",
        "Region is limited to eu-west.",
        "Null durations were retained.",
    )
    assert _apply_application_answer_contract(
        question, answer(limitations=material)
    ).limitations == (*material, expected)


def test_answer_consistency_enforces_explicit_standard_identifiers():
    question = "Which records have failed status? Cite evidence and state causal limitations."

    with pytest.raises(ModelAnswerInconsistentError) as identifiers:
        _validate_answer_consistency(
            question,
            answer(
                claims=("Two records have failed status.",),
                limitations=("The observed status does not establish a cause.",),
            ),
            consistency_evidence(),
        )
    assert identifiers.value.reason == "requested_identifiers_missing"

    with pytest.raises(ModelAnswerInconsistentError) as count_spoof:
        _validate_answer_consistency(
            question,
            answer(
                claims=("2 of 4 records have failed status.",),
                limitations=("The observed status does not establish a cause.",),
            ),
            consistency_evidence(),
        )
    assert count_spoof.value.reason == "requested_identifiers_missing"

    with pytest.raises(ModelAnswerInconsistentError) as nearby_id_spoof:
        _validate_answer_consistency(
            question,
            answer(
                claims=("Records 2 of 4 are failed, and ID 4 is present.",),
                limitations=("The observed status does not establish a cause.",),
            ),
            consistency_evidence(),
        )
    assert nearby_id_spoof.value.reason == "requested_identifiers_missing"

    with pytest.raises(ModelAnswerInconsistentError) as singular_id_with_count:
        _validate_answer_consistency(
            question,
            answer(
                claims=("ID 2 is failed among 4 records.",),
                limitations=("The captured evidence does not establish causality.",),
            ),
            consistency_evidence(),
        )
    assert singular_id_with_count.value.reason == "requested_identifiers_missing"

    _validate_answer_consistency(
        question,
        answer(
            claims=("Jobs 2 and 4 have failed status.",),
            limitations=("The captured evidence does not establish causality.",),
        ),
        consistency_evidence(),
    )


@pytest.mark.parametrize(
    "question",
    (
        "List records whose status is failed.",
        "Which records have a failed status?",
        "Which jobs have status failed?",
        "List failed records.",
        "Which records are in a failed status?",
        "Show records with status failed.",
        "Show me the failed records.",
        "Show all records with a failed status.",
    ),
)
def test_alternate_status_list_wording_requires_captured_identifiers(question):
    with pytest.raises(ModelAnswerInconsistentError) as error:
        _validate_answer_consistency(
            question, answer(claims=("Two records match.",)), consistency_evidence()
        )
    assert error.value.reason == "requested_identifiers_missing"


def test_nonstandard_identifier_and_status_columns_remain_deliberately_unvalidated():
    evidence = consistency_evidence()
    evidence[0].update(
        columns=["job_id", "state"],
        rows=[[2, "failed"], [4, "failed"]],
    )
    _validate_answer_consistency(
        (
            "Show all records with a failed status. The operator defines state as status and "
            "job_id as the record identifier."
        ),
        answer(claims=("Two records have the requested state.",)),
        evidence,
    )


def test_status_identifier_requirement_uses_first_twenty_unique_matches():
    rows = [[identifier, "failed"] for identifier in range(1, 22)]
    evidence = consistency_evidence()
    evidence[0].update(rows=rows, captured_row_count=len(rows))

    with pytest.raises(ModelAnswerInconsistentError) as missing:
        _validate_answer_consistency(
            "Show me the failed records.",
            answer(summary="Twenty-one failed records were captured.", claims=("21 jobs failed.",)),
            evidence,
        )
    assert missing.value.reason == "requested_identifiers_missing"

    first_twenty = ", ".join(str(identifier) for identifier in range(1, 20)) + ", and 20"
    _validate_answer_consistency(
        "Show me the failed records.",
        answer(
            summary="Twenty-one failed records were captured.",
            claims=(f"Job IDs: {first_twenty} have failed status.",),
        ),
        evidence,
    )


@pytest.mark.parametrize(
    "limitation, reason, guidance",
    [
        (
            "The actual row limit was not enforced.",
            "collection_limit_conflict",
            "enforced the recorded row cap",
        ),
        (
            "The executed query included an explicit LIMIT 101, which may have affected the "
            "captured rows, but the captured_row_count is below the limit and truncated is "
            "false, so no truncation was observed.",
            "unsupported_limit_hedge",
            "sentinel LIMIT did not remove any returned rows",
        ),
    ],
)
def test_consistency_retries_one_answer_with_same_capture_and_no_second_query(
    limitation, reason, guidance
):
    class CorrectingProvider(StubProvider):
        answer_calls = 0

        def invoke_structured(self, request):
            response = super().invoke_structured(request)
            if request.response_schema.get("title") == "InvestigationPlan":
                return response
            self.answer_calls += 1
            evidence_id = response.output["findings"][0]["evidence_ids"][0]
            if self.answer_calls == 1:
                output = {
                    "summary": "One failed record was captured.",
                    "findings": [
                        {
                            "claim": "Record ID: 1 has failed status.",
                            "classification": "supported",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "limitations": [limitation],
                }
            else:
                assert reason in request.messages[0].content
                assert guidance in request.messages[0].content
                output = {
                    "summary": "One failed record was captured.",
                    "findings": [
                        {
                            "claim": "Record ID: 1 has failed status.",
                            "classification": "supported",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "limitations": [],
                }
            return response.model_copy(update={"output": output})

    class CountingExecutor(StubExecutor):
        calls = 0

        def execute_readonly(self, sql, *, timeout_ms):
            self.calls += 1
            return super().execute_readonly(sql, timeout_ms=timeout_ms)

    provider = CorrectingProvider()
    executor = CountingExecutor()
    events = []
    result = run_connected(
        question="Which records have failed status?",
        provider=provider,
        principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
        obligations=Obligation(allowed_schemas=("public",)),
        skills=skills(),
        executor=executor,
        snapshot=SchemaSnapshot(tables=(), fingerprint="contract-snapshot"),
        skill_id="failed-jobs",
        evidence_bindings=bindings(),
        observe=lambda kind, data: events.append((kind, data)),
    )
    assert provider.answer_calls == 2
    assert executor.calls == 1
    assert result["answer"]["findings"][0]["claim"] == "Record ID: 1 has failed status."
    assert len([event for event in events if event[0] == "evidence_captured"]) == 1
    assert [event for event in events if event[0] == "answer_validation_retry"] == [
        ("answer_validation_retry", {"stage": "reconcile", "reason": reason})
    ]


@pytest.mark.parametrize("overflow", ("findings", "limitations"))
def test_runtime_enforces_narrower_model_answer_presentation_limits(overflow):
    class OverflowProvider(StubProvider):
        def invoke_structured(self, request):
            response = super().invoke_structured(request)
            if request.response_schema.get("title") == "InvestigationPlan":
                return response
            output = dict(response.output)
            if overflow == "findings":
                output["findings"] = output["findings"] * 5
            else:
                output["limitations"] = [f"Limitation {index}." for index in range(4)]
            return response.model_copy(update={"output": output})

    with pytest.raises(ModelOutputInvalidError, match="exceeded the displayed"):
        run_connected(
            question="Summarize statuses.",
            provider=OverflowProvider(),
            principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
            obligations=Obligation(allowed_schemas=("public",)),
            skills=skills(),
            executor=StubExecutor(),
            snapshot=SchemaSnapshot(tables=(), fingerprint="contract-snapshot"),
            skill_id="failed-jobs",
            evidence_bindings=bindings(),
        )


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
        evidence_bindings=bindings(),
    )

    assert result["skill_id"] == "failed-jobs"
    assert result["evidence"][0]["rows"] == [[1, "failed"]]
    assert result["answer"]["findings"][0]["evidence_ids"] == [
        result["evidence"][0]["evidence_hash"]
    ]


@pytest.mark.parametrize("table_count,joined_query", [(1, False), (2, False), (2, True)])
def test_provider_guidance_follows_planning_scope_and_actual_capture_relations(
    table_count, joined_query
):
    requests = []

    class RecordingProvider(StubProvider):
        def invoke_structured(self, request):
            requests.append(request)
            response = super().invoke_structured(request)
            if request.response_schema.get("title") == "InvestigationPlan" and joined_query:
                response.output["queries"][0]["sql"] = (
                    "SELECT j.id, j.status FROM public.jobs j "
                    "LEFT JOIN public.queues q ON j.id = q.id"
                )
            return response

    tables = tuple(
        TableSchema(
            schema_name="public",
            table_name=name,
            columns=(ColumnSchema(name="id", data_type="bigint"),),
        )
        for name in ("jobs", "queues")[:table_count]
    )
    result = run_connected(
        question="Summarize the observed job statuses with citations.",
        provider=RecordingProvider(),
        principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
        obligations=Obligation(allowed_schemas=("public",)),
        skills=skills(),
        executor=StubExecutor(),
        snapshot=SchemaSnapshot(tables=tables, fingerprint="guidance-snapshot"),
        evidence_bindings=bindings(),
    )

    assert len(requests) == 2
    planner, reconciler = requests
    if table_count == 1:
        assert planner.system == "You are a cautious database investigation planner."
    else:
        assert "sequential joins" in planner.system
        assert "DISTINCT entity identifiers" in planner.system
    expected_relations = {"public.jobs", "public.queues"} if joined_query else {"public.jobs"}
    assert set(result["evidence"][0]["referenced_tables"]) == expected_relations
    if joined_query:
        assert "one-to-many join" in reconciler.system
        assert "A mismatch makes the requested answer unknown" in reconciler.system
    else:
        assert reconciler.system == "You reconcile operational evidence without inventing facts."
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
            evidence_bindings=bindings(),
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
        evidence_bindings=bindings(),
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
        evidence_bindings=bindings(),
    )
    assert result["skill_id"] == custom.id


def test_connected_graph_rejects_missing_source_owned_evidence_before_planning():
    class CountingProvider(StubProvider):
        calls = 0

        def invoke_structured(self, request):
            self.calls += 1
            return super().invoke_structured(request)

    import pytest

    provider = CountingProvider()
    with pytest.raises(ValueError, match="source lacks evidence bindings"):
        run_connected(
            question="Why did the worker job fail?",
            provider=provider,
            principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
            obligations=Obligation(allowed_schemas=("public",)),
            skills=skills(),
            executor=StubExecutor(),
            snapshot=SchemaSnapshot(
                tables=(
                    TableSchema(
                        schema_name="public",
                        table_name="jobs",
                        columns=(ColumnSchema(name="id", data_type="bigint"),),
                    ),
                ),
                fingerprint="sha256:" + "3" * 64,
            ),
            evidence_bindings=(
                EvidenceBinding(evidence_type="job_status", source_tables=("public.jobs",)),
            ),
        )
    assert provider.calls == 0


def test_empty_skill_table_intersection_fails_closed():
    import pytest

    from opsgraph.orchestration.connected import _effective_obligations

    with pytest.raises(PermissionError, match="table scopes do not overlap"):
        _effective_obligations(
            Obligation(allowed_tables=("public.jobs",)),
            ToolSettings(allowed_tables=("public.other",)),
        )
    with pytest.raises(PermissionError, match="table scopes do not overlap"):
        _effective_obligations(
            Obligation(allowed_tables=("public.jobs",)),
            ToolSettings(allowed_tables=()),
        )


def test_specialist_missing_collected_coverage_is_typed_after_capture():
    import pytest

    from opsgraph.orchestration.connected import MappingRequiredError

    events = []
    with pytest.raises(
        MappingRequiredError, match="plan does not cover required evidence: queue_state"
    ):
        run_connected(
            question="Inspect failed worker jobs and queue state?",
            provider=StubProvider(),
            principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
            obligations=Obligation(allowed_schemas=("public",)),
            skills=skills(),
            executor=StubExecutor(),
            snapshot=SchemaSnapshot(tables=(), fingerprint="contract-snapshot"),
            evidence_bindings=(
                EvidenceBinding(evidence_type="job_status", source_tables=("public.jobs",)),
                EvidenceBinding(evidence_type="queue_state", source_tables=("public.queues",)),
            ),
            observe=lambda kind, data: events.append((kind, data)),
        )
    assert len([event for event in events if event[0] == "evidence_captured"]) == 1
    assert not any(event[1].get("stage") == "reconcile" for event in events)


def test_skill_schema_narrowing_also_narrows_executable_tables():
    from opsgraph.orchestration.connected import _effective_obligations

    effective = _effective_obligations(
        Obligation(
            allowed_schemas=("public", "private"), allowed_tables=("public.jobs", "private.jobs")
        ),
        ToolSettings(allowed_schemas=("public",)),
    )
    assert effective.allowed_tables == ("public.jobs",)


def test_followup_operator_context_reaches_planning_and_interpretation():
    prompts = []

    class RecordingProvider(StubProvider):
        def invoke_structured(self, request):
            prompts.append(request.messages[0].content)
            response = super().invoke_structured(request)
            if request.response_schema.get("title") == "InvestigationAnswer":
                response.output["findings"][0]["claim"] = "Record ID: 1 has failed status."
            return response

    context = '{"question":"Operator definition: failed means status failed; units unknown."}'
    result = run_connected(
        question="Show the observed failed records again.",
        provider=RecordingProvider(),
        principal=Principal(subject="tester", workspace_id="workspace", roles={"analyst"}),
        obligations=Obligation(allowed_schemas=("public",)),
        skills=skills(),
        executor=StubExecutor(),
        snapshot=SchemaSnapshot(tables=(), fingerprint="contract-snapshot"),
        evidence_bindings=bindings(),
        parent_context=context,
        skill_id="failed-jobs",
    )
    assert len(prompts) == 2
    assert all(context in prompt and "historical" in prompt for prompt in prompts)
    assert result["answer"]["findings"][0]["evidence_ids"] == [
        result["evidence"][0]["evidence_hash"]
    ]
