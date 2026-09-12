"""Foreground local launcher with a short-lived, one-use browser handoff."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware


class BrowserHandoff:
    """Keep the workspace credential out of URLs and connection logs."""

    def __init__(self, workspace_key: str, origin: str, *, lifetime: float = 60.0):
        self.token = secrets.token_urlsafe(32)
        self._workspace_key = workspace_key
        self.origin = origin
        self._expires = time.monotonic() + lifetime
        self._lock = threading.Lock()

    def consume(self, token: str) -> str | None:
        with self._lock:
            if (
                not self._workspace_key
                or time.monotonic() >= self._expires
                or not secrets.compare_digest(token.encode("utf-8"), self.token.encode("ascii"))
            ):
                return None
            key, self._workspace_key = self._workspace_key, ""
            return key


def attach_handoff(app: FastAPI, handoff: BrowserHandoff) -> None:
    """Only the opt-in loopback launcher registers this endpoint."""

    @app.post("/api/launcher/session", include_in_schema=False)
    async def connect(request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        denied = {
            "detail": "Local connection expired. Relaunch OpsGraph or enter the workspace key."
        }
        if (
            request.headers.get("origin") != handoff.origin
            or request.headers.get("host") != handoff.origin.removeprefix("http://")
            or request.headers.get("content-type", "").split(";")[0] != "application/json"
        ):
            return JSONResponse(denied, status_code=403, headers=headers)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 1024:
                return JSONResponse(denied, status_code=413, headers=headers)
        try:
            body = json.loads(payload)
            token = body.get("token") if isinstance(body, dict) else None
            key = handoff.consume(token) if isinstance(token, str) and len(token) <= 128 else None
        except (ValueError, UnicodeError):
            key = None
        if not key:
            return JSONResponse(denied, status_code=403, headers=headers)
        return JSONResponse({"key": key}, headers=headers)


def bind_loopback(port: int) -> socket.socket:
    if not 1024 <= port <= 65535:
        raise ValueError("Choose a local port from 1024 to 65535.")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # Windows address reuse can permit another listener to share a port.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name == "posix":
            # Allow immediate restart after accepted connections enter TIME_WAIT;
            # this does not enable sharing an active listener with SO_REUSEPORT.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
    except OSError:
        listener.close()
        raise OSError(
            f"Port {port} is unavailable. Stop your existing OpsGraph terminal or "
            f"choose another port with --port. No existing process was changed."
        ) from None
    return listener


def launch(directory: Path | None, port: int, *, configure: bool, browser: bool) -> int:
    from opsgraph.config import StatePathError, get_settings, resolve_state_path
    from opsgraph.setup import default_workspace_directory, read_private_config, run_setup

    workspace = (directory or default_workspace_directory()).expanduser().absolute()
    try:
        if workspace.resolve().is_relative_to(Path(sys.prefix).resolve()):
            print(
                "Choose a workspace directory outside the installed Python runtime. "
                "Configuration and history must survive runtime removal."
            )
            return 2
    except (OSError, RuntimeError):
        print("Workspace location could not be resolved safely. Choose another directory.")
        return 2
    try:
        listener = bind_loopback(port)
    except (OSError, ValueError) as exc:
        print(str(exc))
        return 2
    try:
        if configure or not (workspace / ".env").exists():
            if run_setup(workspace) != 0:
                return 2
        values = read_private_config(workspace / ".env")
        # A launcher has one explicit config directory. Ambient app credentials
        # must not silently switch it to a different database/provider/workspace.
        for name in tuple(os.environ):
            # libpq fills omitted DSN fields from PG* settings, including service
            # and password files. Clear inherited defaults in this process only;
            # the private configuration below may explicitly supply its own.
            if name.startswith(("OPSGRAPH_", "LANGSMITH_", "LANGCHAIN_", "PG")) or name in {
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
            }:
                os.environ.pop(name, None)
        os.environ.update({name: value for name, value in values.items() if value is not None})
        os.chdir(workspace)
        configured_state = values.get("OPSGRAPH_STATE_PATH") or ".opsgraph/state.db"
        os.environ["OPSGRAPH_STATE_PATH"] = str(
            resolve_state_path(configured_state, workspace=workspace)
        )
        get_settings.cache_clear()
        settings = get_settings()
        state_path = settings.state_path
        if not state_path.is_absolute():
            state_path = workspace / state_path
        if state_path.resolve().is_relative_to(Path(sys.prefix).resolve()):
            print(
                "Choose a state database outside the installed Python runtime. "
                "Update OPSGRAPH_STATE_PATH in the private configuration before launching; "
                "history must survive runtime removal."
            )
            return 2
        import uvicorn

        from opsgraph.api.app import app

        origin = f"http://127.0.0.1:{port}"
        handoff = BrowserHandoff(settings.api_key, origin)
        attach_handoff(app, handoff)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False))

        def open_workspace() -> None:
            deadline = time.monotonic() + 30
            while not server.started and time.monotonic() < deadline and not server.should_exit:
                time.sleep(0.05)
            if server.started and browser:
                # Fragments are not sent to the server. The UI removes this
                # one-use token immediately, before making a request.
                try:
                    opened = webbrowser.open(f"{origin}/#connect={handoff.token}")
                except webbrowser.Error:
                    opened = False
                if not opened:
                    print("Browser could not open. Open the local URL and use your private key.")

        print(f"OpsGraph workspace: {workspace}")
        print(f"Local browser address: {origin}")
        print("Stop with Ctrl+C in this terminal. Relaunch using the same launcher and directory.")
        print("Configuration and history stay in the workspace. No model download is automatic.")
        if not browser:
            print("Automatic browser connection disabled. Use the workspace key from private .env.")
        threading.Thread(target=open_workspace, daemon=True).start()
        server.run(sockets=[listener])
        if not server.started:
            print(
                "OpsGraph did not start. Check the terminal diagnostics and whether "
                "another OpsGraph instance already owns this workspace."
            )
            return 2
        return 0
    except KeyboardInterrupt:
        print("Stopped. Configuration and investigation history are preserved.")
        return 0
    except StatePathError as exc:
        print(str(exc))
        return 2
    except (OSError, ValueError, ImportError):
        # Configuration/driver errors can contain private input. Never echo them.
        print(
            "Launch failed. Check private configuration and installed dependencies; "
            "run the launcher with --configure to repair setup."
        )
        return 2
    finally:
        listener.close()
