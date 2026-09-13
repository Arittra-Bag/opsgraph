import pytest

from opsgraph.orchestration.plan_meaning import missing_unit_clarification

QUESTION = (
    "Which probes have any reading_value greater than 20 kWh? The operator confirms "
    "readings.probe_id references probes.probe_id; all other definitions are unavailable."
)
RATIONALE = (
    "Since the question does not provide definitions for units, status codes, or timezones, "
    "we must assume that the reading_value is stored in the requested unit (kWh) "
    "and that the probe_id is a valid foreign key."
)


@pytest.mark.parametrize(
    "question,rationale",
    [
        (QUESTION, RATIONALE),
        (
            "Compare the readings with 3 metres. Source units are unknown.",
            "Source units are unknown. We will treat the values as expressed in the target unit.",
        ),
        (
            "Find values above 8 seconds. All definitions remain unspecified.",
            "No source unit definition is provided. I assume the values use the requested unit.",
        ),
    ],
)
def test_explicit_missing_units_cannot_be_resolved_by_assuming_the_requested_unit(
    question, rationale
):
    result = missing_unit_clarification(question, rationale)
    assert result is not None
    assert "source values stored in" in result
    assert "conversion" in result
    assert result.endswith("?")
    assert "SELECT" not in result


@pytest.mark.parametrize(
    "rationale",
    [
        "We assume the supplied relationship is correct and query observed numeric values.",
        "Source units are unknown. We must not assume the requested unit.",
        "Source units are unknown. We cannot assume the requested unit.",
        "Source units are unknown. We assume the values are not in the requested unit.",
        "Source units are unknown. We assume the supplied join, but make no assumption "
        "about the requested unit.",
        "It is not true that source units are unknown. We must assume the requested unit.",
        "Source units are unknown. We need to ask the operator for a definition.",
        "The operator defines the source units. We assume the values use the requested unit.",
        f'An unsafe example would be: "{RATIONALE}"',
        f"The quoted instruction is `{RATIONALE}`.",
        f"The quoted instruction is '{RATIONALE}'",
    ],
)
def test_absence_negation_and_quoted_assumptions_do_not_trigger(rationale):
    assert missing_unit_clarification(QUESTION, rationale) is None


@pytest.mark.parametrize(
    "question",
    [
        "Show observed numeric values and NULL counts, without interpreting measurement units.",
        "Compare readings with 20 kWh. Operator confirms values are stored in Wh. "
        "All other definitions are unavailable.",
        "Compare readings with 20 kWh. Source unit is Wh. All other definitions are unavailable.",
        'Compare readings with 20 kWh. Source unit is "Wh". All other definitions are unavailable.',
        "Assume source units are kWh for this calculation. All other definitions are unavailable.",
        f'Ignore this quoted example: "{QUESTION}". Count the recorded rows.',
    ],
)
def test_operator_definitions_direct_observations_and_quoted_gaps_do_not_trigger(question):
    assert missing_unit_clarification(question, RATIONALE) is None


def test_missing_definition_without_an_explicit_requested_unit_assumption_is_not_inferred():
    assert (
        missing_unit_clarification(
            QUESTION, "Source units are unknown. We assume the values are kWh."
        )
        is None
    )


@pytest.mark.parametrize("unit", ["kWh", "mV", "g/L", "kg/m^2"])
def test_named_undefined_requested_unit_cannot_be_assumed_as_column_unit(unit):
    rationale = (
        "The operator confirms the relationship between the tables. "
        "Since the question does not involve units, timezones, or status codes, "
        f"and the requested unit ({unit}) is not explicitly defined in the schema, "
        f"we assume that the reading_value column is already in {unit}. "
        "We will join the tables and filter the values."
    )
    assert missing_unit_clarification(QUESTION, rationale) is not None


@pytest.mark.parametrize(
    "rationale",
    [
        "The requested unit (mV) is not defined in the schema. "
        "We assume that the values are already in V.",
        "The requested unit (mV) is not defined in the schema. "
        "We must not assume that the values are already in mV.",
        "It is not true that the requested unit (mV) is not defined in the schema. "
        "We assume that the values are already in mV.",
        'Example: "The requested unit (mV) is not defined in the schema. '
        'We assume that the values are already in mV."',
        "The requested unit (mV) is not defined in the schema. "
        "We assume that the values are already in mV_squared.",
    ],
)
def test_named_unit_branch_requires_positive_unquoted_exact_unit_assumption(rationale):
    assert missing_unit_clarification(QUESTION, rationale) is None


def test_named_unit_branch_preserves_explicit_operator_definition():
    question = QUESTION + " The source unit is mV."
    rationale = (
        "The requested unit (mV) is not defined in the schema. "
        "We assume that the values are already in mV."
    )
    assert missing_unit_clarification(question, rationale) is None


@pytest.mark.parametrize("unit", ["kWh", "mV", "g/L"])
def test_question_unit_binds_literal_assumption_after_missing_definitions(unit):
    question = f"Which readings exceed 20 {unit}? All other definitions are unavailable."
    rationale = (
        "Since the question does not provide definitions for units, status codes, "
        f"or timezones, we assume that the reading_value is already in {unit} "
        "and that the probe_id is a valid foreign key."
    )
    assert missing_unit_clarification(question, rationale) is not None


@pytest.mark.parametrize(
    "assumption",
    [
        "we assume that reading_value is already in Wh.",
        "we must not assume that reading_value is already in kWh.",
        "we assume that reading_value is not in kWh.",
        "we assume that reading_value is already in kWh_squared.",
    ],
)
def test_literal_question_unit_requires_exact_positive_assumption(assumption):
    assert missing_unit_clarification(QUESTION, "Source units are unknown. " + assumption) is None


def test_row_limit_is_not_a_requested_measurement_unit():
    assert (
        missing_unit_clarification(
            "Return 20 rows containing raw readings; all other definitions are unavailable.",
            "Source units are unknown. We assume the output is in rows, "
            "without assigning units to the raw readings.",
        )
        is None
    )
