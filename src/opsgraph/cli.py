"""OpsGraph command-line entry point."""

from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsgraph", description="OpsGraph local control plane")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the local web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    commands.add_parser("doctor", help="check configuration without database or model requests")
    init = commands.add_parser("init", help="create private local state and a configured .env")
    init.add_argument("--directory", type=Path, default=Path.cwd())
    setup = commands.add_parser(
        "setup", help="guide private source and model-provider configuration"
    )
    setup.add_argument("--directory", type=Path)
    launch = commands.add_parser(
        "launch", help="launch your private workspace and open its browser"
    )
    launch.add_argument("--directory", type=Path)
    launch.add_argument("--port", type=int, default=8000)
    launch.add_argument("--configure", action="store_true")
    launch.add_argument("--no-browser", action="store_true")
    backup = commands.add_parser("backup", help="back up a stopped workspace to a new directory")
    backup.add_argument("--directory", type=Path)
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="restore a backup into a new workspace directory")
    restore.add_argument("--directory", type=Path, required=True)
    restore.add_argument("--backup", type=Path, required=True)
    return parser


def _doctor() -> int:
    from opsgraph.config import get_settings

    try:
        settings = get_settings()
    except ValueError:
        # Validation errors can include the supplied environment and credentials.
        print("FAIL  configuration: run opsgraph init in a new directory or repair .env")
        print("      Use connected mode, a random workspace key, and a real model provider.")
        return 1
    parent = settings.state_path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    secret_ref = settings.postgres_secret_ref
    source_configured = bool(
        secret_ref and secret_ref in settings.allowed_postgres_secret_refs and os.getenv(secret_ref)
    )
    try:
        local_host = urlsplit(settings.local_model_url).hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    except ValueError:
        local_host = False
    provider_package = {"openai_compatible": "openai", "anthropic": "anthropic"}.get(
        settings.model_provider
    )
    checks = {
        "web_assets": (
            settings.web_root.joinpath("index.html").is_file(),
            "reinstall the OpsGraph wheel",
        ),
        "connected_mode": (
            settings.mode == "connected",
            "sample mode retired; follow docs/migration.md",
        ),
        "workspace_key": (
            len(settings.api_key) >= 24
            and settings.api_key
            not in {"sample-local-key-change-me", "replace-with-a-long-random-value"},
            "generate a private key using opsgraph init in a new directory",
        ),
        "source_reference": (
            source_configured,
            "set OPSGRAPH_POSTGRES_SECRET_REF and its allowed DSN environment variable",
        ),
        "real_model_provider": (
            provider_package is not None,
            "set OPSGRAPH_MODEL_PROVIDER=openai_compatible for local inference",
        ),
        "provider_package": (
            bool(provider_package and importlib.util.find_spec(provider_package)),
            "install OpsGraph with the providers extra",
        ),
        "provider_egress": (
            settings.egress_enabled
            or (settings.model_provider == "openai_compatible" and local_host),
            "use a loopback model endpoint or enable deployment egress; "
            "source and playbook egress permissions are also required for investigations",
        ),
        "provider_key": (
            settings.model_provider != "anthropic" or bool(os.getenv("ANTHROPIC_API_KEY")),
            "set the selected provider credential in the backend environment",
        ),
        "state_parent_writable": (
            parent.is_dir() and os.access(parent, os.W_OK),
            "choose a writable local state path",
        ),
    }
    for name, (passed, action) in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {action}" if not passed else ""))
    print("Configuration checks only. Use Sources inspection and model probe for live validation.")
    return 0 if all(passed for passed, _ in checks.values()) else 1


def _init(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / ".opsgraph"
    state.mkdir(mode=0o700, exist_ok=True)
    env = directory / ".env"
    try:
        # Exclusive creation preserves existing configuration and avoids following symlinks.
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved. No credentials or settings replaced.")
        return 0
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            "# Private configuration. Never commit or share this file.\n"
            "OPSGRAPH_MODE=connected\n"
            f"OPSGRAPH_API_KEY={secrets.token_urlsafe(32)}\n"
            "OPSGRAPH_WORKSPACE_ID=local\n"
            "OPSGRAPH_EGRESS_ENABLED=false\n"
            "OPSGRAPH_MODEL_PROVIDER=openai_compatible\n"
            "OPSGRAPH_LOCAL_MODEL_URL=http://127.0.0.1:11434/v1\n"
            "OPSGRAPH_LOCAL_MODEL=qwen3:8b\n"
            "# Omit for endpoints/models without reasoning_effort support.\n"
            "OPSGRAPH_LOCAL_REASONING_EFFORT=none\n"
            "OPSGRAPH_LOCAL_SCHEMA_PROFILE=ollama\n"
            "OPSGRAPH_STATE_PATH=.opsgraph/state.db\n"
            "OPSGRAPH_POSTGRES_SECRET_REF=OPSGRAPH_SOURCE_DSN\n"
            "OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS=OPSGRAPH_SOURCE_DSN\n"
            "OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS=public\n"
            "# Set the dedicated read-only PostgreSQL DSN before source inspection.\n"
            "OPSGRAPH_SOURCE_DSN=\n"
            "LANGSMITH_TRACING=false\n"
        )
    print("Created private .env with a random workspace key; existing assets preserved.")
    print("Open .env privately to configure PostgreSQL and copy the workspace key into the UI.")
    print("Run opsgraph doctor, then opsgraph serve from this directory.")
    return 0


def main() -> None:
    args = _parser().parse_args()
    if args.command in {"backup", "restore"}:
        from opsgraph.maintenance import run_maintenance

        other = args.output if args.command == "backup" else args.backup
        raise SystemExit(run_maintenance(args.command, args.directory, other))
    if args.command == "setup":
        from opsgraph.setup import run_setup

        raise SystemExit(run_setup(args.directory))
    if args.command == "launch":
        from opsgraph.launcher import launch

        raise SystemExit(
            launch(args.directory, args.port, configure=args.configure, browser=not args.no_browser)
        )
    # Includes DSN/provider variables not modeled by Settings; shell values win.
    load_dotenv(Path.cwd() / ".env", override=False)
    if args.command == "doctor":
        raise SystemExit(_doctor())
    if args.command == "init":
        raise SystemExit(_init(args.directory))
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "opsgraph.api.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )


if __name__ == "__main__":
    main()
