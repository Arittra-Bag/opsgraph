import pytest

from opsgraph.domain import CORE_SCHEMA_INSPECT, ToolDefinition, ToolRegistry
from opsgraph.schema_service import PostgresSchemaParser
from opsgraph.schema_service.postgres import SchemaParseError


def test_schema_parser_is_deterministic_and_schema_only() -> None:
    parser = PostgresSchemaParser()
    ddl = """
    -- schema only
    CREATE TABLE public.incidents (
      id bigint PRIMARY KEY,
      title character varying(200) NOT NULL,
      severity integer DEFAULT 2,
      created_at timestamp with time zone
    );
    """
    first = parser.parse(ddl)
    second = parser.parse(ddl)
    assert first.fingerprint == second.fingerprint
    assert first.tables[0].table_name == "incidents"
    assert first.tables[0].columns[0].primary_key is True


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO incidents VALUES (1);",
        "COPY incidents FROM STDIN;",
        "CREATE FUNCTION steal() RETURNS void AS $$ BEGIN END $$ LANGUAGE plpgsql;",
        "CREATE TRIGGER t AFTER INSERT ON incidents EXECUTE FUNCTION steal();",
        "CREATE EXTENSION dblink;",
        "ALTER TABLE incidents ADD COLUMN secret text;",
        "SELECT * FROM incidents;",
    ],
)
def test_schema_parser_rejects_data_executable_and_unknown_statements(statement: str) -> None:
    with pytest.raises(SchemaParseError):
        PostgresSchemaParser().parse(statement)


def test_native_schema_tool_cannot_be_replaced_or_removed() -> None:
    registry = ToolRegistry(PostgresSchemaParser().inspect)
    assert CORE_SCHEMA_INSPECT in registry.tools
    with pytest.raises(ValueError):
        registry.remove(CORE_SCHEMA_INSPECT)
    with pytest.raises(ValueError):
        registry.register(ToolDefinition(CORE_SCHEMA_INSPECT, "replacement", lambda: None))
