import os
import stat
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

from opsgraph import setup


def private_directory(tmp_path):
    return setup.ensure_private_directory(tmp_path / "workspace")


def prompts(*answers):
    values = iter(answers)
    return lambda _label: next(values)


def test_fresh_setup_stores_hidden_literal_dsn_without_network(tmp_path, monkeypatch):
    directory = private_directory(tmp_path)
    dsn = "postgresql://reader:p%40ss%24%7Bword%7D@127.0.0.1:5432/example"
    output = []
    monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: pytest.fail("network attempted"))
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("", "", "", "", "", "", ""),
            secret_fn=lambda _label: dsn,
            output_fn=output.append,
        )
        == 0
    )
    values = setup.read_private_config(directory / ".env")
    assert conninfo_to_dict(values["OPSGRAPH_SOURCE_DSN"]) == {
        **conninfo_to_dict(dsn),
        "passfile": os.devnull,
    }
    assert len(values["OPSGRAPH_API_KEY"]) >= 40
    assert dsn not in " ".join(output)
    assert values["OPSGRAPH_API_KEY"] not in " ".join(output)
    assert values["OPSGRAPH_EGRESS_ENABLED"] == "false"
    assert values["LANGSMITH_TRACING"] == "false"
    assert values["OPSGRAPH_PROVIDER_TIMEOUT_SECONDS"] == "300.0"
    assert values["OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS"] == "public"
    assert values["OPSGRAPH_STATE_PATH"] == str(directory / ".opsgraph" / "state.db")
    if os.name == "posix":
        assert stat.S_IMODE((directory / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_empty_dsn_is_a_visible_deferred_step(tmp_path):
    directory = private_directory(tmp_path)
    output = []
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("", "", "", "", "", "", ""),
            secret_fn=lambda _label: "",
            output_fn=output.append,
        )
        == 0
    )
    assert setup.read_private_config(directory / ".env")["OPSGRAPH_SOURCE_DSN"] == ""
    assert any("credential skipped" in line for line in output)


def test_reconfigure_requires_consent_and_preserves_existing_bytes(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(
        path, {"UNKNOWN": "kept", "OPSGRAPH_SOURCE_DSN": "secret", "PGSERVICE": "legacy"}
    )
    before = path.read_bytes()
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("no"),
            secret_fn=lambda _label: pytest.fail("credential prompt before consent"),
            output_fn=lambda _value: None,
        )
        == 0
    )
    assert path.read_bytes() == before


def test_reconfigure_preserves_unknown_values_key_and_existing_dsn(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    existing = {
        "OPSGRAPH_API_KEY": "a" * 43,
        "OPSGRAPH_SOURCE_DSN": (
            "host=127.0.0.1 port=5432 user=reader dbname=test password='literal${SECRET}'"
        ),
        "OPSGRAPH_EGRESS_ENABLED": "true",
        "OPSGRAPH_STATE_PATH": "saved-state.db",
        "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS": "OPSGRAPH_SECOND_DSN",
        "OPSGRAPH_SECOND_DSN": "second secret",
        "CUSTOM": "\\quoted' ${UNSET}\nsecond line",
        "UNSET": None,
    }
    setup.write_private_config(path, existing)
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("yes", "public,reporting", "", "", "", "standard", "omit", ""),
            secret_fn=lambda _label: "",
            output_fn=lambda _value: None,
        )
        == 0
    )
    updated = setup.read_private_config(path)
    for key in (
        "OPSGRAPH_API_KEY",
        "OPSGRAPH_STATE_PATH",
        "CUSTOM",
        "UNSET",
        "OPSGRAPH_SECOND_DSN",
    ):
        assert updated[key] == existing[key]
    assert conninfo_to_dict(updated["OPSGRAPH_SOURCE_DSN"]) == {
        **conninfo_to_dict(existing["OPSGRAPH_SOURCE_DSN"]),
        "passfile": os.devnull,
    }
    assert updated["OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS"] == "public,reporting"
    assert updated["OPSGRAPH_EGRESS_ENABLED"] == "false"
    assert "OPSGRAPH_LOCAL_REASONING_EFFORT" not in updated
    assert (
        updated["OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS"]
        == "OPSGRAPH_SECOND_DSN,OPSGRAPH_SOURCE_DSN"  # noqa: S105
    )


def test_invalid_hidden_dsn_is_retried_without_echo(tmp_path):
    directory = private_directory(tmp_path)
    private = "invalid-secret-do-not-print"
    output = []
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("", "", "", "", "", "", ""),
            secret_fn=prompts(private, ""),
            output_fn=output.append,
        )
        == 0
    )
    assert private not in " ".join(output)
    assert any("syntax is invalid" in line for line in output)


@pytest.mark.parametrize(
    "dsn",
    [
        "dbname=test",
        "postgresql://reader@127.0.0.1/example",
        "postgresql://127.0.0.1:5432/example",
        "postgresql://reader@127.0.0.1:5432",
        "host='' port=5432 dbname=test user=reader",
        "host=127.0.0.1 port='' dbname=test user=reader",
        "host=127.0.0.1 port=5432 dbname='' user=reader",
        "host=127.0.0.1 port=5432 dbname=test user=''",
    ],
)
def test_guided_dsn_requires_explicit_source_even_with_ambient_defaults(dsn, monkeypatch):
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSERVICE"):
        monkeypatch.setenv(name, "ambient-private-value")
    with pytest.raises(setup.SetupError, match="explicitly") as error:
        setup.normalize_guided_dsn(dsn)
    assert "ambient-private-value" not in str(error.value)


@pytest.mark.parametrize(
    "option",
    [
        "service=private-service",
        "service=''",
        "passfile=/private/credentials.pgpass",
        "passfile=''",
    ],
)
def test_guided_dsn_rejects_service_and_password_file_indirection(option):
    dsn = f"host=127.0.0.1 port=5432 dbname=test user=reader {option}"
    with pytest.raises(setup.SetupError) as error:
        setup.normalize_guided_dsn(dsn)
    assert "private-service" not in str(error.value)
    assert "/private/credentials.pgpass" not in str(error.value)


@pytest.mark.parametrize(
    "fields",
    [
        "host=127.0.0.1, port=5432",
        "host=127.0.0.1,127.0.0.2 port=5432,5432",
        "host=127.0.0.1 hostaddr=127.0.0.1,127.0.0.2 port=5432",
        "host=127.0.0.1 port=5432,",
        "host=127.0.0.1 port=0",
        "host=127.0.0.1 port=65536",
        "host=127.0.0.1 port=postgres",
    ],
)
def test_guided_dsn_rejects_defaultable_host_lists_or_invalid_ports(fields):
    with pytest.raises(setup.SetupError):
        setup.normalize_guided_dsn(f"{fields} dbname=test user=reader")


@pytest.mark.parametrize(
    "authentication",
    [
        "",
        "password='explicit secret'",
        "sslmode=verify-full sslcert=/explicit/client.crt sslkey=/explicit/client.key",
    ],
)
def test_guided_dsn_always_disables_password_file_fallback(authentication, monkeypatch):
    monkeypatch.setenv("PGPASSFILE", "/must-not-use/ambient.pgpass")
    monkeypatch.setenv("PGSERVICEFILE", "/must-not-use/ambient.pg_service.conf")
    monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: pytest.fail("network attempted"))
    original = f"host=127.0.0.1 port=5432 dbname=test user=reader {authentication}"
    normalized = setup.normalize_guided_dsn(original)
    assert conninfo_to_dict(normalized) == {**conninfo_to_dict(original), "passfile": os.devnull}
    assert setup.normalize_guided_dsn(normalized) == normalized


def test_consented_reconfiguration_removes_pg_overrides_after_disclosure(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    original = "host=127.0.0.1 port=5432 dbname=test user=reader"
    setup.write_private_config(
        path,
        {
            "OPSGRAPH_SOURCE_DSN": original,
            "PGSERVICE": "private-legacy-service",
            "PGPASSWORD": "private-legacy-password",
            "PGPASSFILE": "/legacy/password-file",
            "PGSERVICEFILE": "/legacy/service-file",
            "PGHOST": "legacy-host",
            "CUSTOM": "preserved",
        },
    )
    output = []

    def consent(label):
        if "Reconfigure" in label:
            assert any("PostgreSQL environment overrides (PG*)" in line for line in output)
            return "yes"
        return ""

    assert (
        setup.run_setup(
            directory, input_fn=consent, secret_fn=lambda _: "", output_fn=output.append
        )
        == 0
    )
    saved = setup.read_private_config(path)
    assert not any(name.startswith("PG") for name in saved)
    assert saved["CUSTOM"] == "preserved"
    assert conninfo_to_dict(saved["OPSGRAPH_SOURCE_DSN"]) == {
        **conninfo_to_dict(original),
        "passfile": os.devnull,
    }
    assert "private-legacy" not in " ".join(output)
    assert "/legacy/" not in " ".join(output)


def test_incomplete_existing_dsn_requires_reentry_and_cancellation_preserves_it(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(path, {"OPSGRAPH_SOURCE_DSN": "dbname=legacy", "PGUSER": "old-user"})
    before = path.read_bytes()
    output = []
    attempted = False

    def hidden(_label):
        nonlocal attempted
        if attempted:
            raise KeyboardInterrupt
        attempted = True
        return ""

    assert (
        setup.run_setup(
            directory, input_fn=prompts("yes"), secret_fn=hidden, output_fn=output.append
        )
        == 1
    )
    assert any("Name the source explicitly" in line for line in output)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "https://example.com/v1",
        "http://127.0.0.1.evil/v1",
        "http://key@127.0.0.1/v1",
        "http://127.0.0.1/v1?key=secret",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1/v1#token",
        "http://127.0.0.1:99999/v1",
        "http://127.0.0.1/\nv1",
    ],
)
def test_model_endpoint_denies_nonliteral_or_credential_bearing_urls(url):
    with pytest.raises(setup.SetupError):
        setup._model_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434/v1", "http://[::1]:11434/v1"])
def test_model_endpoint_accepts_literal_loopback(url):
    assert setup._model_url(url) == url


@pytest.mark.parametrize(
    "names", ["", "Public", "public,", "public;other", ",".join(f"schema{i}" for i in range(33))]
)
def test_schema_ceiling_rejects_empty_quoted_or_oversized_input(names):
    with pytest.raises(setup.SetupError):
        setup._schemas(names)


def test_cancel_does_not_replace_existing_configuration(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(path, {"CUSTOM": "unchanged"})
    before = path.read_bytes()

    def cancel(_label):
        raise KeyboardInterrupt

    assert (
        setup.run_setup(
            directory, input_fn=prompts("yes"), secret_fn=cancel, output_fn=lambda _: None
        )
        == 1
    )
    assert path.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and permission contract")
def test_existing_broad_permissions_fail_without_modifying_files(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    path.write_text("SECRET=value\n")
    path.chmod(0o644)
    with pytest.raises(setup.SetupError, match="too broad"):
        setup.write_private_config(path, {"SECRET": "new"})
    assert path.read_text() == "SECRET=value\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixtures")
def test_symlink_and_hardlink_configuration_never_replace_targets(tmp_path):
    directory = private_directory(tmp_path)
    target = tmp_path / "target"
    target.write_text("SECRET=original\n")
    target.chmod(0o600)
    path = directory / ".env"
    path.symlink_to(target)
    with pytest.raises(setup.SetupError):
        setup.write_private_config(path, {"SECRET": "new"})
    path.unlink()
    os.link(target, path)
    with pytest.raises(setup.SetupError, match="hard links"):
        setup.read_private_config(path)
    assert target.read_text() == "SECRET=original\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixtures")
def test_symlink_directory_is_rejected(tmp_path):
    target = private_directory(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(setup.SetupError):
        setup.ensure_private_directory(link / "nested")
    assert not (target / "nested").exists()


def test_atomic_write_failure_leaves_original_and_removes_temporary_file(tmp_path, monkeypatch):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(path, {"SECRET": "original"})
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(setup.os, "replace", fail_replace)
    with pytest.raises(OSError):
        setup.write_private_config(path, {"SECRET": "new"})
    assert path.read_bytes() == before
    assert list(directory.iterdir()) == [path]


def test_malformed_private_config_does_not_echo_secret(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    path.write_text("PASSWORD='never-show-this\n")
    path.chmod(0o600)
    with pytest.raises(setup.SetupError) as error:
        setup.read_private_config(path)
    assert "never-show-this" not in str(error.value)


@pytest.mark.parametrize(
    ("platform", "os_name", "relative_directory"),
    [
        ("linux", "posix", (".local", "share", "opsgraph")),
        ("darwin", "posix", ("Library", "Application Support", "OpsGraph")),
        ("win32", "nt", ("AppData", "Local", "OpsGraph")),
    ],
)
def test_default_directory_ignores_cwd_and_relative_xdg(
    tmp_path, monkeypatch, platform, os_name, relative_directory
):
    # Replace only the setup module's platform view. Mutating global os.name
    # also changes pathlib's concrete path class on the host running the test.
    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(setup, "os", SimpleNamespace(name=os_name, environ=os.environ))
    monkeypatch.setattr(setup.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", "relative-untrusted-directory")
    monkeypatch.setenv("LOCALAPPDATA", "relative-untrusted-directory")
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    assert setup.default_workspace_directory() == tmp_path.joinpath(*relative_directory)


def test_anthropic_setup_requires_consent_and_hides_key(tmp_path):
    directory = private_directory(tmp_path)
    output = []
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("", "anthropic", "claude-test", "yes", ""),
            secret_fn=prompts("", "private-provider-key"),
            output_fn=output.append,
        )
        == 0
    )
    saved = setup.read_private_config(directory / ".env")
    assert saved["OPSGRAPH_MODEL_PROVIDER"] == "anthropic"
    assert saved["OPSGRAPH_ANTHROPIC_MODEL"] == "claude-test"
    assert saved["ANTHROPIC_API_KEY"] == "private-provider-key"
    assert saved["OPSGRAPH_EGRESS_ENABLED"] == "true"
    assert "private-provider-key" not in " ".join(output)
    assert any("captured evidence" in line for line in output)


def test_remote_egress_decline_preserves_existing_configuration(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(path, {"CUSTOM": "preserved"})
    original = path.read_bytes()
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("yes", "", "anthropic", "claude-test", "no"),
            secret_fn=prompts(""),
            output_fn=lambda _: None,
        )
        == 1
    )
    assert path.read_bytes() == original


def test_changed_compatible_endpoint_does_not_reuse_saved_key(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(
        path,
        {
            "OPSGRAPH_MODEL_PROVIDER": "openai_compatible",
            "OPSGRAPH_LOCAL_MODEL_URL": "https://old.example/v1",
            "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "standard",
            "OPSGRAPH_LOCAL_MODEL": "old-model",
            "OPSGRAPH_OPENAI_API_KEY": "must-not-reuse",
        },
    )
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts(
                "yes",
                "",
                "openai_compatible",
                "https://new.example/v1",
                "new-model",
                "",
                "omit",
                "yes",
                "",
            ),
            secret_fn=prompts("", ""),
            output_fn=lambda _: None,
        )
        == 0
    )
    saved = setup.read_private_config(path)
    assert saved["OPSGRAPH_OPENAI_API_KEY"] == ""
    assert saved["OPSGRAPH_LOCAL_MODEL_URL"] == "https://new.example/v1"
    assert saved["OPSGRAPH_LOCAL_SCHEMA_PROFILE"] == "standard"
    assert "OPSGRAPH_LOCAL_REASONING_EFFORT" not in saved


def test_unchanged_compatible_endpoint_preserves_hidden_key(tmp_path):
    directory = private_directory(tmp_path)
    path = directory / ".env"
    setup.write_private_config(
        path,
        {
            "OPSGRAPH_MODEL_PROVIDER": "openai_compatible",
            "OPSGRAPH_LOCAL_MODEL_URL": "https://same.example/v1",
            "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "standard",
            "OPSGRAPH_LOCAL_MODEL": "existing-model",
            "OPSGRAPH_OPENAI_API_KEY": "existing-private-key",
        },
    )
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("yes", "", "", "", "", "", "", "yes", ""),
            secret_fn=prompts("", ""),
            output_fn=lambda _: None,
        )
        == 0
    )
    saved = setup.read_private_config(path)
    assert saved["OPSGRAPH_OPENAI_API_KEY"] == "existing-private-key"
    assert saved["OPSGRAPH_LOCAL_MODEL"] == "existing-model"


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "https://key@remote.example/v1",
        "https://remote.example/v1?key=secret",
    ],
)
def test_remote_endpoint_requires_https_without_inline_secrets(url):
    with pytest.raises(setup.SetupError):
        setup._model_url(url, allow_remote=True)


@pytest.mark.parametrize("unset_entry", [False, True])
def test_reconfigured_ollama_explicitly_disables_ambient_key(tmp_path, monkeypatch, unset_entry):
    directory = private_directory(tmp_path)
    setup.write_private_config(
        directory / ".env",
        {
            "OPSGRAPH_MODEL_PROVIDER": "openai_compatible",
            "OPSGRAPH_LOCAL_MODEL_URL": "http://127.0.0.1:11434/v1",
            "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "ollama",
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    if unset_entry:
        with (directory / ".env").open("a") as config:
            config.write("OPSGRAPH_OPENAI_API_KEY\n")
    assert (
        setup.run_setup(
            directory,
            input_fn=prompts("y", "", "", "", "", "", "", ""),
            secret_fn=lambda _: "",
            output_fn=lambda _: None,
        )
        == 0
    )
    values = setup.read_private_config(directory / ".env")
    assert values["OPSGRAPH_OPENAI_API_KEY"] == ""
    assert "unrelated-secret" not in (directory / ".env").read_text()
