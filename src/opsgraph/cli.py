"""OpsGraph command-line entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsgraph", description="OpsGraph local control plane")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the local web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    commands.add_parser("doctor", help="validate the local runtime configuration")
    init = commands.add_parser("init", help="create a local state directory and env template")
    init.add_argument("--directory", type=Path, default=Path.cwd())
    return parser


def _doctor() -> int:
    from opsgraph.config import get_settings

    settings = get_settings()
    checks = {
        "web_assets": settings.web_root.joinpath("index.html").is_file(),
        "api_key_changed": settings.api_key != "sample-local-key-change-me",
        "egress_explicit": not settings.egress_enabled
        or settings.model_provider != "deterministic",
        "state_parent_writable": os.access(
            settings.state_path.parent if settings.state_path.parent.exists() else Path.cwd(),
            os.W_OK,
        ),
    }
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


def _init(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / ".opsgraph"
    state.mkdir(exist_ok=True)
    env = directory / ".env.example"
    if not env.exists():
        env.write_text(
            "OPSGRAPH_MODE=sample\n"
            "OPSGRAPH_API_KEY=change-me\n"
            "OPSGRAPH_WORKSPACE_ID=local\n"
            "OPSGRAPH_EGRESS_ENABLED=false\n"
            "OPSGRAPH_MODEL_PROVIDER=deterministic\n"
            "OPSGRAPH_STATE_PATH=.opsgraph/state.db\n",
            encoding="utf-8",
        )
    print(f"Initialized OpsGraph in {directory}")
    return 0


def main() -> None:
    args = _parser().parse_args()
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
