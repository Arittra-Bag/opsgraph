import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from opsgraph import setup
from opsgraph.config import Settings
from opsgraph.providers.models import PROVIDER_DEFAULT_ENDPOINTS
from opsgraph.runtime import _provider

SOURCE = Path(__file__).resolve().parents[2] / "Start.py"
spec = importlib.util.spec_from_file_location("first_run", SOURCE)
first_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(first_run)


def wizard(tmp_path, *, flow="quick", answers=(), secret="", existing=None):
    directory = setup.ensure_private_directory(tmp_path / "workspace")
    if existing:
        setup.write_private_config(directory / ".env", existing)
    supplied = iter(answers)
    labels, output = [], []

    def ask(label):
        labels.append(label)
        return "" if label.startswith("Connection options") else next(supplied, "")

    result = setup.run_setup(
        directory, flow=flow, input_fn=ask, secret_fn=lambda _: secret, output_fn=output.append
    )
    return result, directory, labels, "\n".join(output)


def test_quick_setup_defers_credentials_and_skips_advanced_prompts(tmp_path):
    result, directory, labels, output = wizard(tmp_path)
    assert result == 0
    config = setup.read_private_config(directory / ".env")
    assert config["OPSGRAPH_MODEL_PRESET"] == "ollama"
    assert config["OPSGRAPH_LOCAL_SCHEMA_PROFILE"] == "ollama"
    assert "OPSGRAPH_LOCAL_REASONING_EFFORT" not in config
    assert config["OPSGRAPH_EGRESS_ENABLED"] == "false"
    assert not any("Approved schemas" in label or "timeout" in label for label in labels)
    assert not any("Schema profile" in label or "Reasoning effort" in label for label in labels)
    assert "Not tested yet: database access and model responses" in output
    assert "Database connection skipped" in output
    assert "Browser sign-in key: created or kept privately" in output
    assert config["OPSGRAPH_API_KEY"] not in output
    assert labels[-1].startswith("Save configuration?")


@pytest.mark.parametrize("preset", ["openai", "openrouter", "groq", "together", "mistral"])
def test_hosted_quick_presets_require_consent_and_use_official_endpoints(tmp_path, preset):
    # Hosting, provider, exact model, external consent and save review.
    result, directory, _, output = wizard(
        tmp_path, answers=("local", preset, "chosen-model", "yes", "save")
    )
    assert result == 0
    config = setup.read_private_config(directory / ".env")
    assert config["OPSGRAPH_LOCAL_MODEL_URL"] == PROVIDER_DEFAULT_ENDPOINTS[preset]
    assert config["OPSGRAPH_MODEL_PRESET"] == preset
    assert config["OPSGRAPH_EGRESS_ENABLED"] == "true"
    assert "captured evidence" in " ".join(output.split())
    settings = Settings(_env_file=None, **{k: v for k, v in config.items() if v is not None})
    assert _provider(settings).config.provider_preset == preset


def test_hosted_quick_setup_cannot_enable_egress_without_consent(tmp_path):
    result, directory, _, _ = wizard(
        tmp_path, answers=("local", "openrouter", "chosen-model", "no")
    )
    assert result == 1
    assert not (directory / ".env").exists()


def test_review_cancellation_preserves_existing_configuration_and_hides_values(tmp_path):
    existing = {
        "OPSGRAPH_API_KEY": "fixture-only-workspace-key-that-must-stay-private",
        "OPSGRAPH_SOURCE_DSN": (
            "host=127.0.0.1 port=5432 dbname=test user=reader password=fixture-only-hidden-password"
        ),
        "OPSGRAPH_LOCAL_MODEL_URL": PROVIDER_DEFAULT_ENDPOINTS["ollama"],
        "OPSGRAPH_LOCAL_MODEL": "fixture-model",
        "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "ollama",
        "OPSGRAPH_LOCAL_REASONING_EFFORT": "none",
        "CUSTOM": "literal${value}",
    }
    directory = setup.ensure_private_directory(tmp_path / "workspace")
    setup.write_private_config(directory / ".env", existing)
    before = (directory / ".env").read_bytes()
    result, _, _, output = wizard(
        tmp_path, existing=existing, answers=("yes", "local", "", "", "", "cancel")
    )
    assert result == 1
    assert (directory / ".env").read_bytes() == before
    assert existing["OPSGRAPH_API_KEY"] not in output
    assert existing["OPSGRAPH_SOURCE_DSN"] not in output
    assert "fixture-only-hidden-password" not in output


def test_quick_reconfiguration_preserves_explicit_reasoning_and_schema_scope(tmp_path):
    existing = {
        "OPSGRAPH_LOCAL_MODEL_URL": PROVIDER_DEFAULT_ENDPOINTS["ollama"],
        "OPSGRAPH_LOCAL_SCHEMA_PROFILE": "ollama",
        "OPSGRAPH_LOCAL_REASONING_EFFORT": "none",
        "OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS": "reporting",
    }
    result, directory, _, _ = wizard(tmp_path, existing=existing, answers=("yes",))
    assert result == 0
    values = setup.read_private_config(directory / ".env")
    assert values["OPSGRAPH_LOCAL_REASONING_EFFORT"] == "none"
    assert values["OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS"] == "reporting"


def test_interrupted_review_does_not_save_configuration(tmp_path):
    directory = setup.ensure_private_directory(tmp_path / "workspace")

    def ask(label):
        if label.startswith("Save configuration?"):
            raise EOFError
        return ""

    assert setup.run_setup(directory, flow="quick", input_fn=ask, secret_fn=lambda _: "") == 1
    assert not (directory / ".env").exists()


def checkout(tmp_path, monkeypatch):
    root = tmp_path / "source checkout"
    root.mkdir()
    for name in ("pyproject.toml", "uv.lock", "requirements-build.lock", "Start.py"):
        (root / name).touch()
    monkeypatch.setattr(first_run, "__file__", str(root / "Start.py"))
    return root


def test_install_cancellation_has_no_side_effects(tmp_path, monkeypatch):
    root = checkout(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    monkeypatch.setattr(first_run, "bootstrap_uv", lambda *_: pytest.fail("installed after cancel"))
    assert first_run.start([]) == 1
    assert sorted(p.name for p in root.iterdir()) == [
        "Start.py",
        "pyproject.toml",
        "requirements-build.lock",
        "uv.lock",
    ]


def test_install_environment_removes_secrets_and_redirect_overrides(monkeypatch):
    for name in (
        "OPSGRAPH_SOURCE_DSN",
        "PGPASSWORD",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "UV_PROJECT_ENVIRONMENT",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "PYTHONPATH",
        "LANGSMITH_API_KEY",
    ):
        monkeypatch.setenv(name, "fixture-only-secret")
    assert "fixture-only-secret" not in first_run.install_environment().values()
    assert first_run.install_environment().get("PATH") == os.environ.get("PATH")


def test_install_uses_lock_hashes_no_shell_and_forwards_only_launcher_options(
    tmp_path, monkeypatch
):
    root = checkout(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(first_run, "bootstrap_uv", lambda *_: "/fixture/uv")

    def install(args, cwd, env, failure):
        assert cwd == root
        calls.append(args)
        if "build" in args:
            (Path(args[-1]) / "opsgraph-1.0.0-py3-none-any.whl").touch()

    monkeypatch.setattr(first_run, "command", install)

    def launch(args, *, cwd, env):
        assert cwd == root
        calls.append(args)
        return 7

    monkeypatch.setattr(first_run.subprocess, "call", launch)
    assert (
        first_run.start(
            [
                "--yes",
                "--flow",
                "quick",
                "--configure",
                "--no-browser",
                "--port",
                "8010",
                "--directory",
                str(tmp_path / "private workspace"),
            ]
        )
        == 7
    )
    assert "--locked" in calls[0] and "--no-build" in calls[0]
    assert "--require-hashes" in calls[1] and "requirements-build.lock" in calls[1]
    assert "--no-deps" in calls[2]
    assert "--reinstall-package" in calls[2]
    assert calls[-1][-2:] == ["--flow", "quick"]
    assert "--yes" not in calls[-1]
    assert "--no-browser" in calls[-1] and "--configure" in calls[-1]


def test_installer_errors_do_not_echo_subprocess_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        first_run.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, stdout=b"private-output", stderr=b"private-key"
        ),
    )
    with pytest.raises(first_run.StartError, match="safe failure"):
        first_run.command(["fixture"], tmp_path, {}, "safe failure")
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ("failed to open cache: Read-only file system (os error 30)", "installer cache"),
        ("cache: Permission denied (os error 13)", "installer cache"),
        ("cache: Access is denied", "installer cache"),
        ("No space left on device (os error 28)", "free disk space"),
        ("Disk quota exceeded", "free disk space"),
        ("Permission denied writing build directory", "cannot write to a required directory"),
        ("invalid peer certificate: UnknownIssuer", "certificate could not be verified"),
        ("CERTIFICATE_VERIFY_FAILED", "certificate could not be verified"),
        ("dns error: failed to lookup address", "could not reach a download service"),
        ("Connection timed out", "could not reach a download service"),
        ("No interpreter found for Python >=3.11,<3.14", "compatible Python runtime"),
        ("package requires a different Python", "compatible Python runtime"),
    ],
)
def test_installer_classifies_failure_without_exposing_raw_output(
    tmp_path, monkeypatch, details, expected
):
    private_value = "fixture-only-proxy-password"
    private_path = "/private/fixture-only-local-path"
    monkeypatch.setattr(
        first_run.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a,
            1,
            stdout=f"https://user:{private_value}@example.invalid {private_path}".encode(),
            stderr=details.encode(),
        ),
    )
    with pytest.raises(first_run.StartError) as failure:
        first_run.command(["fixture"], tmp_path, {}, "safe fallback")
    message = str(failure.value)
    assert expected in message
    assert "Private workspace configuration is unchanged" in message
    assert private_value not in message
    assert private_path not in message
    assert "example.invalid" not in message


def test_cache_recovery_uses_supported_environment_without_restoring_uv_overrides(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "/fixture/writable-cache")
    monkeypatch.setenv("UV_CACHE_DIR", "/fixture/unapproved-cache")
    environment = first_run.install_environment()
    assert environment["XDG_CACHE_HOME"] == "/fixture/writable-cache"
    assert "UV_CACHE_DIR" not in environment
    message = first_run.installation_failure(b"", b"cache: Read-only file system", "fallback")
    assert 'XDG_CACHE_HOME="$HOME/opsgraph-cache"' in message
    assert "UV_CACHE_DIR" not in message


def test_successful_installer_output_is_not_treated_as_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        first_run.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout=b"cached previous Permission denied", stderr=b""
        ),
    )
    assert first_run.command(["fixture"], tmp_path, {}, "safe fallback") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_missing_uv_rejects_symlink_bootstrap_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(first_run.shutil, "which", lambda *a, **k: None)
    (tmp_path / ".bootstrap").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(first_run.StartError, match="private directory"):
        first_run.bootstrap_uv(tmp_path, {})


def test_numbered_choose_flow_defaults_to_quick_and_accepts_provider_number(tmp_path):
    result, directory, _, _ = wizard(tmp_path, flow="choose", answers=("1", "1", "1"))
    assert result == 0
    assert setup.read_private_config(directory / ".env")["OPSGRAPH_POSTGRES_HOSTING"] == "local"


@pytest.mark.parametrize("value", ["999999", "9" * 5000, "0", "11", "not-a-provider"])
def test_menu_invalid_values_are_safe_and_recoverable(value):
    with pytest.raises(setup.SetupError, match="listed options"):
        setup._menu_choice(value, tuple(PROVIDER_DEFAULT_ENDPOINTS))


def test_advanced_setup_supports_named_hosted_presets(tmp_path):
    result, directory, labels, _ = wizard(
        tmp_path,
        flow="advanced",
        answers=(
            "local",
            "reporting",
            "OpenRouter",
            "chosen-model",
            "standard",
            "omit",
            "yes",
            "45",
            "save",
        ),
    )
    assert result == 0
    values = setup.read_private_config(directory / ".env")
    assert values["OPSGRAPH_MODEL_PRESET"] == "openrouter"
    assert values["OPSGRAPH_LOCAL_MODEL_URL"] == PROVIDER_DEFAULT_ENDPOINTS["openrouter"]
    assert values["OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS"] == "reporting"
    assert values["OPSGRAPH_PROVIDER_TIMEOUT_SECONDS"] == "45.0"
    assert any("Schema profile" in label for label in labels)


def test_guided_model_endpoint_respects_provider_length_bound():
    with pytest.raises(setup.SetupError):
        setup._model_url("https://example.invalid/" + "x" * 2048, allow_remote=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_installer_does_not_modify_another_runtime_through_a_symlink(tmp_path, monkeypatch):
    root = checkout(tmp_path, monkeypatch)
    unrelated = tmp_path / "unrelated-runtime"
    unrelated.mkdir()
    marker = unrelated / "preserved"
    marker.write_text("unchanged")
    (root / ".venv").symlink_to(unrelated, target_is_directory=True)
    monkeypatch.setattr(first_run, "bootstrap_uv", lambda *_: pytest.fail("install attempted"))
    assert first_run.start(["--yes", "--install-only"]) == 2
    assert marker.read_text() == "unchanged"
    assert sorted(p.name for p in unrelated.iterdir()) == ["preserved"]


def test_no_code_installer_uses_separate_runtime_and_reuses_matching_install(tmp_path, monkeypatch):
    root = checkout(tmp_path, monkeypatch)
    original = root / ".venv"
    original.mkdir()
    marker = original / "existing-runtime"
    marker.write_text("preserved")
    installs, launches = [], []
    monkeypatch.setattr(first_run, "bootstrap_uv", lambda *_: "/fixture/uv")

    def install(args, cwd, env, failure):
        installs.append(args)
        assert env["UV_PROJECT_ENVIRONMENT"] == str(root / ".no-code-runtime")
        if "sync" in args:
            runtime = root / ".no-code-runtime"
            runtime.mkdir(exist_ok=True)
            directory = runtime / ("Scripts" if os.name == "nt" else "bin")
            directory.mkdir()
            (directory / ("python.exe" if os.name == "nt" else "python")).touch()
        if "build" in args:
            (Path(args[-1]) / "opsgraph-1.0.0-py3-none-any.whl").touch()

    monkeypatch.setattr(first_run, "command", install)
    monkeypatch.setattr(
        first_run.subprocess, "call", lambda args, **kwargs: launches.append(args) or 0
    )
    assert first_run.start(["--no-code", "--yes"]) == 0
    count = len(installs)
    assert first_run.start(["--no-code"]) == 0
    assert len(installs) == count
    assert "no-code" in launches[0]
    assert "--port" not in launches[0]
    assert marker.read_text() == "preserved"
    assert (root / ".no-code-runtime/.source-fingerprint").is_file()


def test_no_code_fingerprint_changes_when_application_changes(tmp_path):
    root = tmp_path
    for name in ("Start.py", "pyproject.toml", "uv.lock", "requirements-build.lock"):
        (root / name).write_text("initial")
    source = root / "src/opsgraph"
    source.mkdir(parents=True)
    module = source / "module.py"
    module.write_text("initial")
    before = first_run.source_fingerprint(root)
    module.write_text("updated")
    assert before != first_run.source_fingerprint(root)
