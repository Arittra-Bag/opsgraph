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

ConfigValues = dict[str, str | None]
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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
            "PostgreSQL connection syntax is invalid. Enter a PostgreSQL URL "
            "or libpq connection string with host, port, database and user."
        ) from None
    if "service" in parameters or "servicefile" in parameters:
        raise SetupError(
            "Guided setup does not use PostgreSQL service files. "
            "Enter host, port, database and user directly in the DSN."
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
            "Use operator-managed configuration for multi-host connection strings."
        )
    port = parameters["port"]
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise SetupError("Enter an explicit PostgreSQL port between 1 and 65535.")
    parameters["passfile"] = os.devnull
    try:
        return make_conninfo(**dict(sorted(parameters.items())))
    except Exception:
        raise SetupError(
            "PostgreSQL connection syntax is invalid. Re-enter the complete DSN."
        ) from None


def _model_url(value: str, *, allow_remote: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
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
            "Use a literal loopback http(s) URL"
            + (" or an HTTPS provider URL" if allow_remote else "")
            + ", "
            "without credentials, query or fragment."
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
    input_fn: Callable[[str], str] | None = None,
    secret_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], object] = print,
) -> int:
    """Prompt for backend configuration; browser inspection/probing stays mandatory."""
    ask = input_fn or input
    hidden = secret_fn or getpass.getpass
    try:
        workspace = ensure_private_directory(directory or default_workspace_directory())
        path = workspace / ".env"
        existing = read_private_config(path)
        if path.exists():
            output_fn(
                "Existing configuration found. Guided setup selects a provider and asks before "
                "external model egress. It also removes PostgreSQL environment overrides (PG*) "
                "and disables password-file fallback; other settings are preserved."
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
            "Credentials stay on this backend. Setup validates syntax only; "
            "inspect source scope and test the real model in the browser."
        )
        output_fn(
            "Provide the source host, port, database and user explicitly. "
            "Saved PostgreSQL services and password files are not used by guided setup."
        )
        output_fn(
            "For local inference, use an already installed model. Downloads require several GB "
            "and enough RAM; this setup does not install a runtime or download a model."
        )
        values = {name: value for name, value in existing.items() if not name.startswith("PG")}

        def prompt(label: str, default: str, validate: Callable[[str], str]) -> str:
            while True:
                value = ask(f"{label} [Enter keeps current/default]: ").strip() or default
                try:
                    return validate(value)
                except SetupError as error:
                    output_fn(str(error))

        while True:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                dsn = hidden(
                    "Read-only PostgreSQL DSN (hidden; Enter keeps existing or skips): "
                ).strip()
            if not dsn:
                dsn = existing.get("OPSGRAPH_SOURCE_DSN") or ""
            if dsn:
                try:
                    dsn = normalize_guided_dsn(dsn)
                except SetupError as error:
                    output_fn(str(error))
                    continue
            break
        schemas = prompt(
            "Approved schemas, comma-separated (default public)",
            existing.get("OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS") or "public",
            _schemas,
        )
        if existing.get("OPSGRAPH_MODEL_PROVIDER") in {"anthropic", "external"}:
            provider_default = "anthropic"
        elif existing.get("OPSGRAPH_LOCAL_SCHEMA_PROFILE", "ollama") == "ollama" and _loopback_url(
            existing.get("OPSGRAPH_LOCAL_MODEL_URL") or "http://127.0.0.1"
        ):
            provider_default = "ollama"
        else:
            provider_default = "openai_compatible"
        selected = prompt(
            f"Provider: ollama / openai_compatible / anthropic (default {provider_default})",
            provider_default,
            lambda value: _choice(value, {"ollama", "openai_compatible", "anthropic"}),
        )
        reasoning = None
        if selected == "anthropic":
            output_fn("Anthropic uses its official API endpoint, https://api.anthropic.com.")
            values["OPSGRAPH_ANTHROPIC_MODEL"] = prompt(
                "Anthropic model identifier (default claude-sonnet-5)",
                existing.get("OPSGRAPH_ANTHROPIC_MODEL") or "claude-sonnet-5",
                _model_name,
            )
            credential_name = "ANTHROPIC_API_KEY"
            remote = True
        else:
            same_selection = selected == provider_default
            endpoint = prompt(
                "Model API URL (Ollama default http://127.0.0.1:11434/v1)",
                (existing.get("OPSGRAPH_LOCAL_MODEL_URL") if same_selection else None)
                or (
                    "http://127.0.0.1:11434/v1"
                    if selected == "ollama"
                    else "https://api.openai.com/v1"
                ),
                lambda value: _model_url(value, allow_remote=selected != "ollama"),
            )
            model = prompt(
                "Model identifier (Ollama default qwen3:8b; "
                "compatible APIs require their model ID)",
                (existing.get("OPSGRAPH_LOCAL_MODEL") if same_selection else None)
                or ("qwen3:8b" if selected == "ollama" else ""),
                _model_name,
            )
            profile = prompt(
                "Schema profile: ollama / standard",
                (existing.get("OPSGRAPH_LOCAL_SCHEMA_PROFILE") if same_selection else None)
                or ("ollama" if selected == "ollama" else "standard"),
                lambda value: _choice(value, {"ollama", "standard"}),
            )
            reasoning = prompt(
                "Reasoning effort: none / low / medium / high / omit",
                (existing.get("OPSGRAPH_LOCAL_REASONING_EFFORT") if same_selection else None)
                or ("none" if profile == "ollama" else "omit"),
                lambda value: _choice(value, {"none", "low", "medium", "high", "omit"}),
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
                "External model use sends questions, scoped schema and captured evidence to "
                "the selected provider. This enables external model egress; "
                "setup itself sends nothing."
            )
            if ask("Allow external model egress? [y/N]: ").strip().lower() not in {"y", "yes"}:
                output_fn("Setup cancelled. Existing configuration was not replaced.")
                return 1
        else:
            output_fn("Literal loopback model selected; external model egress will be disabled.")
        endpoint_changed = selected != "anthropic" and (
            endpoint != (existing.get("OPSGRAPH_LOCAL_MODEL_URL") or "")
        )
        if selected == "ollama":
            if endpoint_changed or not values.get(credential_name):
                values[credential_name] = ""
        else:
            if endpoint_changed:
                output_fn(
                    "Endpoint changed: enter its key explicitly; "
                    "the previous key will not be reused."
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                credential = hidden(
                    "Model API key (hidden; Enter keeps existing or skips): "
                ).strip()
            if credential:
                values[credential_name] = credential
            elif endpoint_changed or not values.get(credential_name):
                values[credential_name] = ""
                output_fn(
                    "Model credential skipped; authenticated providers require it before probing."
                )
        timeout = prompt(
            "Model timeout in seconds (default 300)",
            existing.get("OPSGRAPH_PROVIDER_TIMEOUT_SECONDS") or "300",
            _timeout,
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
        write_private_config(path, values)
        output_fn(
            "Private configuration saved. Run opsgraph launch, select explicit tables in Sources, "
            "inspect actual access, and test the model in Settings."
        )
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
