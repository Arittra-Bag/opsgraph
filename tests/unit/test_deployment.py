from pathlib import Path

import yaml


def test_compose_passes_the_postgres_schema_ceiling() -> None:
    compose_path = Path(__file__).parents[2] / "deploy" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    environment = compose["services"]["api"]["environment"]
    assert environment["OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS"] == (
        "${OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS:-public}"
    )
