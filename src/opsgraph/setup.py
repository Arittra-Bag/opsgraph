"""Private, terminal-guided configuration for the local launcher.

This module never opens a database connection, probes a model or downloads anything.
Configuration values are decoded without dotenv interpolation; the launcher must
load these literal values before importing the application settings.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import stat
import sys
import tempfile
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from dotenv.parser import parse_stream
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from opsgraph.brokers.postgres import ConnectorUnavailable, PsycopgReadOnlyExecutor
from opsgraph.postgres_diagnostics import connection_diagnostic
from opsgraph.postgres_hosting import HOSTING_GUIDES, hosting_guide
from opsgraph.providers.models import FIXED_HOSTED_PRESETS, PROVIDER_DEFAULT_ENDPOINTS
from opsgraph.terminal_ui import TerminalUI

ConfigValues = dict[str, str | None]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROVIDER_LABELS = {
    "ollama": "Ollama (on this computer)",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "together": "Together AI",
    "mistral": "Mistral AI",
    "lm_studio": "LM Studio (on this computer)",
    "vllm": "vLLM (your model server)",
    "anthropic": "Anthropic",
    "custom_openai": "Other compatible service (custom address)",
}


class SetupError(ValueError):
    """An operator-safe error that never includes a supplied credential."""


def default_workspace_directory() -> Path:
    """Return a stable per-user location, independent of the current directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpsGraph"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (
            Path(base) if base and Path(base).is_absolute() else Path.home() / "AppData" / "Local"
        ) / "OpsGraph"
    base = os.environ.get("XDG_DATA_HOME")
    return (
        Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "share"
    ) / "opsgraph"


def _check_private(info: os.stat_result, *, directory: bool) -> None:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SetupError("Configuration paths must not be symbolic links or reparse points.")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise SetupError("Expected a private directory or regular configuration file.")
    if not directory and info.st_nlink != 1:
        raise SetupError("Configuration files must not have multiple hard links.")
    if os.name == "posix":
        if info.st_uid != os.getuid():
            raise SetupError("Configuration must belong to the current user.")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SetupError(
                "Existing configuration permissions are too broad; "
                "use a private directory (0700) and file (0600)."
            )


def ensure_private_directory(directory: Path) -> Path:
    """Create a private leaf directory; preserve and validate any existing leaf."""
    directory = Path(directory).expanduser().absolute()
    # Reject traversal through a symlink, including junctions on Windows.
    for component in reversed((directory, *directory.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            component.mkdir(mode=0o700)
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SetupError(
                "Configuration paths must not traverse symbolic links or reparse points."
            )
        if not stat.S_ISDIR(info.st_mode):
            raise SetupError("A configuration directory path is not a directory.")
    _check_private(directory.lstat(), directory=True)
    return directory


def read_private_config(path: Path) -> ConfigValues:
    """Read a private .env *file* without variable interpolation or secret output."""
    path = Path(path)
    _check_private(path.parent.lstat(), directory=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {}
    _check_private(before, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise SetupError("Private configuration could not be opened safely.") from None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        opened = os.fstat(handle.fileno())
        _check_private(opened, directory=False)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise SetupError("Configuration changed while opening it. Retry setup.")
        values: ConfigValues = {}
        try:
            for binding in parse_stream(handle):
                if binding.error:
                    raise SetupError("Private configuration contains invalid dotenv syntax.")
                if binding.key is None:
                    continue
                if not _ENV_KEY.fullmatch(binding.key) or binding.key in values:
                    raise SetupError("Private configuration contains an invalid or duplicate name.")
                values[binding.key] = binding.value
        except UnicodeError:
            raise SetupError("Private configuration must use UTF-8 text.") from None
    return values


def write_private_config(path: Path, values: Mapping[str, str | None]) -> None:
    """Atomically replace a private .env, preserving literal values and unset keys."""
    path = Path(path)
    ensure_private_directory(path.parent)
    read_private_config(path)  # Validate an existing destination before replacement.
    lines = ["# Private OpsGraph configuration. Never share or commit this file.\n"]
    for key, value in values.items():
        if not _ENV_KEY.fullmatch(key) or (value is not None and "\x00" in value):
            raise SetupError("Configuration contains an invalid name or value.")
        if value is None:
            lines.append(f"{key}\n")
        else:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"{key}='{escaped}'\n")
    descriptor, name = tempfile.mkstemp(prefix=".opsgraph-config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        # A replaced destination symlink is not followed; reject one if seen here.
        read_private_config(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _schemas(value: str) -> str:
    names = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    if not 1 <= len(names) <= 32 or any(not _IDENTIFIER.fullmatch(name) for name in names):
        raise SetupError("Enter 1-32 comma-separated lowercase PostgreSQL schema names.")
    return ",".join(names)


def normalize_guided_dsn(value: str) -> str:
    """Name one explicit source and disable libpq password-file fallback.

    Parsing conninfo does not expand services, read password files or connect.
    Legacy operator configuration remains unchanged outside the guided wizard.
    """
    try:
        if "\x00" in value:
            raise ValueError
        parameters = conninfo_to_dict(value)
    except Exception:
        raise SetupError(
            "PostgreSQL connection syntax is invalid. Paste a complete connection string "
            "with the host, port, database and user. Type ? for connection instructions."
        ) from None
    if "service" in parameters or "servicefile" in parameters:
        raise SetupError(
            "Guided setup does not use PostgreSQL service files. "
            "Enter host, port, database and user directly in the connection string."
        )
    if "passfile" in parameters and parameters["passfile"] != os.devnull:
        raise SetupError(
            "Guided setup does not read password files. Remove the passfile option "
            "and provide a password in this hidden prompt if your source requires one."
        )
    if any(not parameters.get(name, "").strip() for name in ("host", "port", "dbname", "user")):
        raise SetupError(
            "Name the source explicitly: include host, port, database (dbname) and user. "
            "Guided setup does not use the default local database or operating-system user."
        )
    # Empty host-list members or omitted port-list members can reactivate libpq defaults.
    # The guided path deliberately supports one named host and one numeric port.
    if any("," in parameters.get(name, "") for name in ("host", "hostaddr", "port")) or any(
        character.isspace() for character in parameters["host"]
    ):
        raise SetupError(
            "Guided setup requires one explicit host and port. "
            "Ask your administrator to configure multi-host connections outside this wizard."
        )
    port = parameters["port"]
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise SetupError("Enter an explicit PostgreSQL port between 1 and 65535.")
    parameters["passfile"] = os.devnull
    try:
        normalized = make_conninfo(**dict(sorted(parameters.items())))
    except Exception:
        raise SetupError(
            "PostgreSQL connection syntax is invalid. Re-enter the complete connection string."
        ) from None
    try:
        PsycopgReadOnlyExecutor(normalized)
    except ConnectorUnavailable as error:
        diagnostic = connection_diagnostic(error.diagnostic_code)
        raise SetupError(" ".join((diagnostic.message, *diagnostic.steps))) from None
    return normalized


def _model_url(value: str, *, allow_remote: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            len(value) <= 2_048
            and parsed.scheme in {"http", "https"}
            and (
                parsed.hostname in {"127.0.0.1", "::1"}
                or (allow_remote and parsed.scheme == "https" and bool(parsed.hostname))
            )
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not any(character.isspace() or ord(character) < 32 for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SetupError(
            "For a local model, use http://127.0.0.1:<port>/v1 or http://[::1]:<port>/v1. "
            + ("Hosted services must use https://. " if allow_remote else "")
            + "Keep API keys out of the address. Remove anything after ? or #."
        )
    return value.rstrip("/")


def _loopback_url(value: str) -> bool:
    try:
        return urlsplit(value).hostname in {"127.0.0.1", "::1"}
    except ValueError:
        return False


def _model_name(value: str) -> str:
    if not value or len(value) > 200 or any(ord(character) < 32 for character in value):
        raise SetupError("Enter a model identifier between 1 and 200 characters.")
    return value


def _choice(value: str, choices: set[str]) -> str:
    if value not in choices:
        raise SetupError("Choose one of the listed options.")
    return value


def _menu_choice(value: str, choices: tuple[str, ...]) -> str:
    """Accept a displayed number or case-insensitive configuration name."""
    if len(value) <= 3 and value.isascii() and value.isdigit() and 1 <= int(value) <= len(choices):
        return choices[int(value) - 1]
    return _choice(value.lower(), set(choices))


def _timeout(value: str) -> str:
    try:
        seconds = float(value)
        if 0.1 <= seconds <= 600:
            return str(seconds)
    except ValueError:
        pass
    raise SetupError("Enter a model timeout from 0.1 to 600 seconds.")


def run_setup(
    directory: Path | None = None,
    *,
    flow: str = "advanced",
    input_fn: Callable[[str], str] | None = None,
    secret_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], object] = print,
) -> int:
    """Prompt for backend configuration; browser inspection/probing stays mandatory."""
    ui = TerminalUI(output_fn)
    ask = input_fn or (lambda label: input(ui.question(label)))
    hidden = secret_fn or (lambda label: getpass.getpass(ui.question(label)))
    output_fn = ui.write
    try:
        workspace = ensure_private_directory(directory or default_workspace_directory())
        path = workspace / ".env"
        existing = read_private_config(path)
        if path.exists():
            output_fn(
                "Saved settings found. Your history will stay safe. Setup uses the database "
                "details you enter here, removing PostgreSQL environment overrides (PG*) "
                "and saved password-file fallback. Other settings are preserved."
            )
            if ask("Reconfigure these settings? [y/N]: ").strip().lower() not in {"y", "yes"}:
                output_fn("Existing configuration preserved.")
                return 0
        if os.name == "nt":
            output_fn(
                "Windows: keep this workspace within your private user profile and verify "
                "inherited ACLs; POSIX permission bits do not verify Windows privacy."
            )
        output_fn(
            "Three steps: database, model service, then review and save. "
            "Press Ctrl+C at any time to cancel without replacing your settings."
        )
        values = {name: value for name, value in existing.items() if not name.startswith("PG")}

        def prompt(
            label: str,
            default: str,
            validate: Callable[[str], str],
            help_text: str = "",
            display_default: str | None = None,
        ) -> str:
            while True:
                output_fn(
                    f"Press Enter to use: {display_default or default}. Type ? for help."
                    if default
                    else "Enter a value. Type ? for help."
                )
                value = ask(f"{label}: ").strip()
                if value == "?":
                    output_fn(
                        help_text
                        or "Choose one of the shown values. Press Enter to keep the default."
                    )
                    continue
                value = value or default
                try:
                    return validate(value)
                except SetupError as error:
                    output_fn(str(error))

        if flow == "choose":
            ui.heading("Choose how to set up")
            ui.menu(
                (
                    ("quick", "Quick setup (recommended)"),
                    ("advanced", "Advanced setup (more options)"),
                ),
                "quick",
            )
            flow = prompt(
                "Setup",
                "quick",
                lambda value: _menu_choice(value, ("quick", "advanced")),
            )
        if flow not in {"quick", "advanced"}:
            raise SetupError("Choose quick or advanced setup.")
        ui.heading("Step 1 of 3: Your database")
        output_fn(
            "Use a PostgreSQL login that can read approved tables but cannot change data. "
            "No login yet? You can skip the connection and finish it in Sources later."
        )
        known_hosting = {guide.id for guide in HOSTING_GUIDES}
        stored_hosting = existing.get("OPSGRAPH_POSTGRES_HOSTING")
        hosting_default = stored_hosting if stored_hosting in known_hosting else "self_hosted"
        ui.menu(tuple((guide.id, guide.name) for guide in HOSTING_GUIDES), hosting_default)
        selected_hosting = prompt(
            "PostgreSQL hosting: where does your database run?",
            hosting_default,
            lambda value: _menu_choice(value, tuple(guide.id for guide in HOSTING_GUIDES)),
            "Pick the company hosting your database, or Local PostgreSQL for this computer. "
            "This selects instructions. It does not create or connect a database.",
            hosting_guide(hosting_default).name,
        )
        guide = hosting_guide(selected_hosting)
        output_fn(f"Selected: {guide.name}.")
        output_fn(
            "Paste your read-only connection string below. It contains the database address, "
            "port, database name and login. Typing is hidden to protect its password."
        )
        output_fn(
            "For a database on this computer, use its local address and port."
            if selected_hosting == "local"
            else "Remote connections must use sslmode=verify-full "
            "and the provider's trusted certificate."
        )
        output_fn(
            "Need connection instructions? Enter ? below. Connection checks run later in Sources."
        )
        database_help = "\n".join(
            (
                "A connection string is also called a DSN. Ask your database administrator for "
                "a read-only login. Sources includes an administrator role guide.",
                guide.endpoint,
                guide.network,
                guide.tls,
                *guide.steps,
                *guide.checks,
                f"Provider instructions: {guide.documentation}",
            )
        )

        while True:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                dsn = hidden(
                    "Database connection string (hidden). Enter keeps a saved connection or skips. "
                    "? shows help: "
                ).strip()
            if dsn == "?":
                output_fn(database_help)
                continue
            if not dsn:
                dsn = existing.get("OPSGRAPH_SOURCE_DSN") or ""
            if dsn:
                try:
                    dsn = normalize_guided_dsn(dsn)
                except SetupError as error:
                    output_fn(str(error))
                    continue
            break
        output_fn(
            "Database connection entered. It will be saved after you confirm."
            if dsn
            else "Database connection skipped. You can add it later in Sources."
        )
        schemas = existing.get("OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS") or "public"
        if flow == "advanced":
            schemas = prompt(
                "Approved schemas (database groups, separated by commas)",
                schemas,
                _schemas,
                "Most databases use public. Enter only the groups OpsGraph may inspect. "
                "You will choose individual tables in Sources later.",
            )
        else:
            schemas = _schemas(schemas)
            output_fn(
                "Database groups: saved choices kept, or public for a new workspace. "
                "Choose individual tables in Sources after setup."
            )
        ui.heading("Step 2 of 3: Your model service")
        output_fn("Choose a model running on your computer, or a hosted service with an API key.")
        if existing.get("OPSGRAPH_MODEL_PROVIDER") in {"anthropic", "external"}:
            provider_default = "anthropic"
        elif existing.get("OPSGRAPH_LOCAL_SCHEMA_PROFILE", "ollama") == "ollama" and _loopback_url(
            existing.get("OPSGRAPH_LOCAL_MODEL_URL") or "http://127.0.0.1"
        ):
            provider_default = "ollama"
        else:
            provider_default = "openai_compatible"
        if flow == "quick":
            if provider_default != "anthropic":
                saved_endpoint = existing.get("OPSGRAPH_LOCAL_MODEL_URL")
                provider_default = next(
                    (
                        name
                        for name, url in PROVIDER_DEFAULT_ENDPOINTS.items()
                        if url is not None and url == saved_endpoint
                    ),
                    "custom_openai" if saved_endpoint else "ollama",
                )
            ui.menu(
                tuple((name, _PROVIDER_LABELS[name]) for name in PROVIDER_DEFAULT_ENDPOINTS),
                provider_default,
            )
            selected = prompt(
                "Provider",
                provider_default,
                lambda value: _menu_choice(value, tuple(PROVIDER_DEFAULT_ENDPOINTS)),
            )
        else:
            ui.menu(
                tuple((name, _PROVIDER_LABELS[name]) for name in PROVIDER_DEFAULT_ENDPOINTS)
                + (("openai_compatible", "Other compatible service"),),
                provider_default,
            )
            selected = prompt(
                "Provider",
                provider_default,
                lambda value: _menu_choice(
                    value, (*PROVIDER_DEFAULT_ENDPOINTS, "openai_compatible")
                ),
            )
        reasoning = None
        if selected == "anthropic":
            output_fn("Anthropic uses its official API endpoint, https://api.anthropic.com.")
            values["OPSGRAPH_ANTHROPIC_MODEL"] = prompt(
                "Model name",
                existing.get("OPSGRAPH_ANTHROPIC_MODEL") or "claude-sonnet-5",
                _model_name,
            )
            credential_name = "ANTHROPIC_API_KEY"
            remote = True
        else:
            same_selection = selected == provider_default
            endpoint_default = (
                (existing.get("OPSGRAPH_LOCAL_MODEL_URL") if same_selection else None)
                or PROVIDER_DEFAULT_ENDPOINTS.get(selected)
                or ("https://api.openai.com/v1" if selected == "openai_compatible" else "")
            )
            endpoint = (
                endpoint_default
                if selected in FIXED_HOSTED_PRESETS
                else prompt(
                    "Model service address",
                    endpoint_default,
                    lambda value: _model_url(value, allow_remote=selected != "ollama"),
                )
            )
            if selected == "ollama":
                output_fn("Ollama must be running with an installed model. For the default, run:")
                output_fn("  ollama pull qwen3:8b")
                output_fn("Model downloads use several GB. Setup does not download them.")
            elif selected == "vllm":
                output_fn("Default vLLM uses port 8000. Launch OpsGraph with --port 8010.")
            model = prompt(
                "Model name",
                (existing.get("OPSGRAPH_LOCAL_MODEL") if same_selection else None)
                or ("qwen3:8b" if selected == "ollama" else ""),
                _model_name,
                "Use the exact model name from your provider's model list. "
                "Ollama uses names such as qwen3:8b. Hosted services may use company/model names. "
                "This setup does not fetch the available models or check account access.",
            )
            if flow == "quick":
                profile = (
                    existing.get("OPSGRAPH_LOCAL_SCHEMA_PROFILE") if same_selection else None
                ) or ("ollama" if selected == "ollama" else "standard")
                profile = _choice(profile, {"ollama", "standard"})
                reasoning = (
                    existing.get("OPSGRAPH_LOCAL_REASONING_EFFORT") if same_selection else None
                ) or "omit"
                output_fn("Recommended response settings selected. Saved model options are kept.")
            else:
                profile = prompt(
                    "Response format (Schema profile): ollama / standard",
                    (existing.get("OPSGRAPH_LOCAL_SCHEMA_PROFILE") if same_selection else None)
                    or ("ollama" if selected == "ollama" else "standard"),
                    lambda value: _choice(value, {"ollama", "standard"}),
                    "Choose ollama for Ollama. Choose standard for hosted services and other "
                    "compatible servers. This controls how OpsGraph asks for structured answers.",
                )
                reasoning = prompt(
                    "Reasoning effort: omit / none / low / medium / high",
                    (existing.get("OPSGRAPH_LOCAL_REASONING_EFFORT") if same_selection else None)
                    or ("none" if profile == "ollama" else "omit"),
                    lambda value: _choice(value, {"none", "low", "medium", "high", "omit"}),
                    "Omit lets the provider use its own setting. Other choices request a specific "
                    "thinking level. Some models do not support this option.",
                )
            values.update(
                {
                    "OPSGRAPH_LOCAL_MODEL_URL": endpoint,
                    "OPSGRAPH_LOCAL_MODEL": model,
                    "OPSGRAPH_LOCAL_SCHEMA_PROFILE": profile,
                }
            )
            credential_name = "OPSGRAPH_OPENAI_API_KEY"
            remote = urlsplit(endpoint).hostname not in {"127.0.0.1", "::1"}
        if remote:
            output_fn(
                "Hosted model privacy: your questions, selected table descriptions and captured "
                "evidence, which may contain actual database values, will be sent to this provider "
                "during investigations. Setup itself sends nothing."
            )
            if ask(
                "Allow sending investigation data to this provider? [y/N]: "
            ).strip().lower() not in {"y", "yes"}:
                output_fn("Setup cancelled. Existing configuration was not replaced.")
                return 1
        else:
            output_fn(
                "Model stays on this computer. Sending data to external model services is disabled."
            )
        endpoint_changed = selected != "anthropic" and (
            endpoint != (existing.get("OPSGRAPH_LOCAL_MODEL_URL") or "")
        )
        if selected == "ollama":
            if endpoint_changed or not values.get(credential_name):
                values[credential_name] = ""
        else:
            if endpoint_changed:
                output_fn(
                    "Model service changed. Enter its API key below. "
                    "The previous key will not be reused."
                )
            output_fn(
                "Your API key is hidden while you type or paste. It will not appear on screen."
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                credential = hidden(
                    "Model API key (hidden). Enter keeps a key for the same service or skips: "
                ).strip()
            if credential:
                values[credential_name] = credential
            elif endpoint_changed or not values.get(credential_name):
                values[credential_name] = ""
                output_fn(
                    "Model credential skipped. Add the required API key in Settings "
                    "before testing this service."
                )
        timeout_default = existing.get("OPSGRAPH_PROVIDER_TIMEOUT_SECONDS") or "300"
        timeout = (
            _timeout(timeout_default)
            if flow == "quick"
            else prompt(
                "Maximum wait for a model response, in seconds",
                timeout_default,
                _timeout,
                "How long each model request may take before stopping. The allowed range is "
                "0.1 to 600 seconds. Slower models may need more time.",
            )
        )
        state = ensure_private_directory(workspace / ".opsgraph")
        key = existing.get("OPSGRAPH_API_KEY") or ""
        if len(key) < 24 or key in {
            "sample-local-key-change-me",
            "replace-with-a-long-random-value",
        }:
            key = secrets.token_urlsafe(32)
        refs = [
            part.strip()
            for part in (existing.get("OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS") or "").split(",")
            if part.strip()
        ]
        if "OPSGRAPH_SOURCE_DSN" not in refs:
            refs.append("OPSGRAPH_SOURCE_DSN")
        values.update(
            {
                "OPSGRAPH_MODE": "connected",
                "OPSGRAPH_API_KEY": key,
                "OPSGRAPH_WORKSPACE_ID": existing.get("OPSGRAPH_WORKSPACE_ID") or "local",
                "OPSGRAPH_EGRESS_ENABLED": "true" if remote else "false",
                "OPSGRAPH_MODEL_PROVIDER": "anthropic"
                if selected == "anthropic"
                else "openai_compatible",
                "OPSGRAPH_PROVIDER_TIMEOUT_SECONDS": timeout,
                "OPSGRAPH_STATE_PATH": existing.get("OPSGRAPH_STATE_PATH")
                or str(state / "state.db"),
                "OPSGRAPH_POSTGRES_SECRET_REF": "OPSGRAPH_SOURCE_DSN",
                "OPSGRAPH_POSTGRES_HOSTING": selected_hosting,
                "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS": ",".join(refs),
                "OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS": schemas,
                "OPSGRAPH_SOURCE_DSN": dsn,
                "LANGSMITH_TRACING": "false",
            }
        )
        if reasoning == "omit":
            values.pop("OPSGRAPH_LOCAL_REASONING_EFFORT", None)
        elif reasoning is not None:
            values["OPSGRAPH_LOCAL_REASONING_EFFORT"] = reasoning
        if selected in PROVIDER_DEFAULT_ENDPOINTS:
            values["OPSGRAPH_MODEL_PRESET"] = selected
        else:
            values.pop("OPSGRAPH_MODEL_PRESET", None)
        ui.heading("Step 3 of 3: Review and save")
        output_fn(f"Setup: {flow}. Hosting: {guide.name}.")
        output_fn("Database credential: " + ("configured privately" if dsn else "deferred"))
        output_fn(
            "Model provider: "
            + selected
            + ". Credential: "
            + ("configured privately" if values.get(credential_name) else "not configured")
        )
        output_fn(
            "Send investigation data to provider: " + ("yes, with your consent" if remote else "no")
        )
        output_fn("Workspace key: generated or preserved privately. It is never printed.")
        output_fn("Not tested yet: database access and model responses. Test both in the browser.")
        output_fn("Browser model settings take precedence. Change providers in Settings.")
        decision = prompt(
            "Save configuration? save / cancel",
            "save",
            lambda value: _choice(value, {"save", "cancel"}),
        )
        if decision == "cancel":
            output_fn("Setup cancelled. Existing configuration was not replaced.")
            return 1
        write_private_config(path, values)
        output_fn("Private settings saved. Your browser workspace is next.")
        ui.heading("Next: finish connecting in the browser")
        output_fn(
            "1. Sources: choose your tables and check the read-only login. "
            "Approve a small test read to check that queries work."
        )
        output_fn(
            "2. Settings: click Test model to check a real response from your selected model."
        )
        output_fn("3. Ask a narrow question and review each finding against its captured evidence.")
        output_fn("No model files were installed. Local model services must already be running.")
        if not dsn:
            output_fn(
                "PostgreSQL credential skipped. Run opsgraph setup again when it is available."
            )
        return 0
    except (EOFError, KeyboardInterrupt):
        output_fn("Setup cancelled. Existing configuration was not replaced.")
        return 1
    except getpass.GetPassWarning:
        output_fn(
            "A private terminal is required for hidden credential entry. "
            "Configuration was not replaced."
        )
        return 1
    except (SetupError, OSError):
        output_fn(
            "Setup could not safely read or save private configuration. Check ownership, "
            "permissions, path safety and configuration syntax; existing files were preserved."
        )
        return 1
