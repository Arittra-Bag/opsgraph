import json

import pytest

from opsgraph.postgres_diagnostics import classify_postgres_failure, connection_diagnostic


@pytest.mark.parametrize(
    ("message", "state", "expected"),
    [
        ("password authentication failed secret-marker", None, "authentication_failed"),
        ("no pg_hba.conf entry secret-marker", None, "access_denied"),
        ("certificate verify failed secret-marker", None, "tls_verification"),
        ("does not match host name secret-marker", None, "tls_verification"),
        ("root certificate file secret-marker", None, "tls_configuration"),
        ("could not translate host name secret-marker", None, "dns_failed"),
        ("connection refused secret-marker", None, "network_unavailable"),
        ("connection timed out secret-marker", None, "connection_timeout"),
        ("secret-marker", "28P01", "authentication_failed"),
        ("secret-marker", "42501", "access_denied"),
        ("secret-marker", "3D000", "database_missing"),
        ("secret-marker", "53300", "capacity_unavailable"),
        ("secret-marker", "57014", "query_timeout"),
        ("secret-marker", None, "connection_failed"),
    ],
)
def test_failure_classification_retains_only_fixed_safe_content(message, state, expected):
    error = RuntimeError(message)
    error.sqlstate = state
    code = classify_postgres_failure(error, connecting=True)
    assert code == expected
    diagnostic = connection_diagnostic(code).as_dict()
    assert diagnostic["steps"] and diagnostic["title"]
    assert "secret-marker" not in json.dumps(diagnostic)


def test_unknown_diagnostic_code_never_echoes_input():
    assert connection_diagnostic("secret-marker").code == "connection_failed"
    assert classify_postgres_failure(RuntimeError("connection refused")) == "connection_failed"


@pytest.mark.parametrize("code", ["schema_too_large", "scope_metadata_incomplete"])
def test_schema_diagnostics_have_scope_recovery_instead_of_network_advice(code):
    diagnostic = connection_diagnostic(code)
    assert diagnostic.code == code
    assert "table" in " ".join(diagnostic.steps)
