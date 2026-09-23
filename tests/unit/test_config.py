from pathlib import Path

import pytest
from pydantic import ValidationError

from opsgraph.config import Settings, StatePathError, resolve_state_path


def test_connected_mode_rejects_the_sample_workspace_key():
    with pytest.raises(ValidationError, match="at least 24 characters"):
        Settings(mode="connected", api_key="sample-local-key-change-me")


def test_connected_mode_accepts_a_strong_workspace_key():
    settings = Settings(mode="connected", api_key="k" * 24)
    assert settings.mode == "connected"


def test_known_example_key_is_rejected_without_echoing_configuration():
    placeholder = "replace-with-a-long-random-value"
    with pytest.raises(ValidationError) as error:
        Settings(mode="connected", api_key=placeholder)
    assert placeholder not in str(error.value)
    assert "opsgraph init" in str(error.value)


def test_allowed_postgres_secret_references_accept_comma_separated_environment_values(monkeypatch):
    monkeypatch.setenv(
        "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS",
        "OPSGRAPH_SOURCE_DSN, OPSGRAPH_ANALYTICS_DSN",
    )
    settings = Settings(api_key="k" * 24)
    assert settings.allowed_postgres_secret_refs == (
        "OPSGRAPH_SOURCE_DSN",
        "OPSGRAPH_ANALYTICS_DSN",
    )


def test_postgres_schema_allowlist_defaults_to_public_and_parses_explicit_scope(monkeypatch):
    monkeypatch.delenv("OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS", raising=False)
    assert Settings(api_key="k" * 24, _env_file=None).postgres_allowed_schemas == ("public",)
    monkeypatch.setenv("OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS", "public, reporting, public")
    assert Settings(api_key="k" * 24, _env_file=None).postgres_allowed_schemas == (
        "public",
        "reporting",
    )


def test_insecure_remote_postgres_override_is_disabled_unless_explicit(monkeypatch):
    monkeypatch.delenv("OPSGRAPH_ALLOW_INSECURE_REMOTE_POSTGRES", raising=False)
    assert Settings(api_key="k" * 24, _env_file=None).allow_insecure_remote_postgres is False
    monkeypatch.setenv("OPSGRAPH_ALLOW_INSECURE_REMOTE_POSTGRES", "true")
    assert Settings(api_key="k" * 24, _env_file=None).allow_insecure_remote_postgres is True


@pytest.mark.parametrize("scope", ["", "*", "public.*", "Private", "x;select", "x" * 64])
def test_postgres_schema_allowlist_rejects_empty_or_unsupported_identifiers(scope):
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(api_key="k" * 24, postgres_allowed_schemas=scope, _env_file=None)


def test_runtime_policy_uses_explicit_deployment_schema_scope(tmp_path):
    from opsgraph.domain import Principal
    from opsgraph.policy import ActionRequest
    from opsgraph.runtime import build_runtime

    runtime = build_runtime(
        Settings(
            api_key="k" * 24,
            model_provider="deterministic",
            state_path=tmp_path / "state.db",
            postgres_allowed_schemas=("public", "reporting"),
            _env_file=None,
        )
    )
    decision = runtime.policy.authorize(
        ActionRequest(
            principal=Principal(subject="test", workspace_id="local-workspace", roles={"analyst"}),
            workspace_id="local-workspace",
            action="core.query.read",
            resource="reporting.records",
        )
    )
    assert decision.obligations.allowed_schemas == ("public", "reporting")
    assert decision.obligations.max_rows == 100
    assert decision.obligations.timeout_ms == 5_000


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Exercise tilde handling without reading or creating real user-home resources."""
    home = tmp_path / "test-home"
    home.mkdir()
    original = Path.expanduser

    def expanduser(path):
        if path.parts and path.parts[0] == "~":
            return home.joinpath(*path.parts[1:])
        return original(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    return home


@pytest.mark.parametrize("spelling", [".opsgraph/state.db", "nested/../state.db"])
def test_state_path_resolves_relative_to_explicit_workspace(tmp_path, monkeypatch, spelling):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unrelated = tmp_path / "other-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    expected = (workspace / spelling).resolve()
    assert resolve_state_path(spelling, workspace=workspace) == expected
    monkeypatch.chdir(workspace)
    assert Settings(state_path=spelling, _env_file=None).state_path == expected
    assert not expected.exists()


def test_state_path_expands_tilde_before_settings_reaches_both_stores(
    tmp_path, monkeypatch, isolated_home
):
    from opsgraph.persistence import SQLiteWorkspaceStore, WorkspaceRecord
    from opsgraph.persistence.runs import RunStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings = Settings(state_path="~/data/state.db", _env_file=None)
    expected = isolated_home / "data" / "state.db"
    assert settings.state_path == expected
    metadata = SQLiteWorkspaceStore(settings.state_path)
    metadata.put(WorkspaceRecord("local", "source:test", {"record_type": "source"}))
    runs = RunStore(settings.state_path)
    assert metadata.path == runs.path == expected
    with runs.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_records").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    assert not (workspace / "~").exists()


def test_default_state_path_is_also_canonical(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPSGRAPH_STATE_PATH", raising=False)
    assert Settings(_env_file=None).state_path == tmp_path / ".opsgraph" / "state.db"


@pytest.mark.parametrize("expanded_exists", [False, True])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal", ".coordinator"])
def test_legacy_literal_tilde_artifacts_require_explicit_migration(
    tmp_path, isolated_home, expanded_exists, suffix
):
    workspace = tmp_path / "workspace"
    legacy = workspace / "~" / "private-state.db"
    legacy.parent.mkdir(parents=True)
    artifact = Path(str(legacy) + suffix)
    artifact.write_bytes(b"legacy state must stay intact")
    expanded = isolated_home / "private-state.db"
    if expanded_exists:
        expanded.write_bytes(b"expanded run history must stay intact")
    with pytest.raises(StatePathError, match="Legacy tilde state path") as error:
        resolve_state_path("~/private-state.db", workspace=workspace)
    assert "OPSGRAPH_STATE_PATH" in str(error.value)
    assert "private-state.db" not in str(error.value)
    assert artifact.read_bytes() == b"legacy state must stay intact"
    assert expanded.exists() is expanded_exists
    if expanded_exists:
        assert expanded.read_bytes() == b"expanded run history must stay intact"


def test_settings_reject_legacy_tilde_ambiguity_with_safe_guidance(
    tmp_path, monkeypatch, isolated_home
):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "~" / "state.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"metadata")
    with pytest.raises(ValidationError, match="Legacy tilde state path"):
        Settings(state_path="~/state.db", _env_file=None)
    assert not (isolated_home / "state.db").exists()
