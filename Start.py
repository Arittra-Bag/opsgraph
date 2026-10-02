"""Install the locked source checkout and launch a private OpsGraph workspace."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from runpy import run_path

# The source installer must work before application dependencies are installed.
TerminalUI = run_path(str(Path(__file__).parent / "src/opsgraph/terminal_ui.py"))["TerminalUI"]

UV_VERSION = "0.9.26"


class StartError(RuntimeError):
    """A safe installation error without subprocess output or credentials."""


def install_environment() -> dict[str, str]:
    """Do not pass application credentials or package-manager overrides to installers."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPSGRAPH_", "PG", "LANGSMITH_", "LANGCHAIN_", "UV_", "PIP_"))
        and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH", "PYTHONHOME"}
    }


def command(args: list[str], root: Path, environment: dict[str, str], failure: str) -> None:
    result = subprocess.run(  # noqa: S603
        args, cwd=root, env=environment, capture_output=True, check=False
    )
    if result.returncode:
        raise StartError(failure)


def bootstrap_uv(root: Path, environment: dict[str, str]) -> str:
    installed = shutil.which("uv", path=environment.get("PATH"))
    if installed:
        return installed
    runtime = root / ".bootstrap"
    if runtime.exists() or runtime.is_symlink():
        info = runtime.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))
        ):
            raise StartError("The bootstrap directory must be a private directory you own.")
    else:
        runtime.mkdir(mode=0o700)
    binary = runtime / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv")
    if binary.is_file():
        return str(binary)
    try:
        venv.EnvBuilder(with_pip=True).create(runtime)
    except (OSError, subprocess.SubprocessError):
        raise StartError(
            "Python could not create the installer environment. Install Python's venv support "
            "or install uv using its official instructions, then rerun this command."
        ) from None
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--index-url",
            "https://pypi.org/simple",
            f"uv=={UV_VERSION}",
        ],
        root,
        environment,
        "uv installation failed. Check internet access, trusted certificates and free disk space. "
        "Rerun Start.py to retry. No workspace configuration was changed.",
    )
    if not binary.is_file():
        raise StartError("uv was not installed successfully. Install uv separately and retry.")
    return str(binary)


def start(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install and open OpsGraph from this checkout")
    parser.add_argument(
        "--yes", action="store_true", help="approve dependency and Python downloads"
    )
    parser.add_argument("--install-only", action="store_true", help="install without opening setup")
    parser.add_argument(
        "--configure", action="store_true", help="review existing workspace settings"
    )
    parser.add_argument("--flow", choices=("quick", "advanced"))
    parser.add_argument("--directory", type=Path, help="private workspace location")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    ui = TerminalUI()
    try:
        if sys.version_info < (3, 9):  # noqa: UP036
            raise StartError(
                "Start.py requires Python 3.9 or newer. OpsGraph uses Python 3.11–3.13."
            )
        if not all(
            (root / name).is_file()
            for name in (
                "pyproject.toml",
                "uv.lock",
                "requirements-build.lock",
            )
        ):
            raise StartError("Run Start.py from a complete OpsGraph source checkout.")
        if not 1024 <= args.port <= 65535:
            raise StartError("Choose a local port between 1024 and 65535.")
        ui.heading("Welcome to OpsGraph")
        ui.write("Ask questions about your PostgreSQL data. Check the records behind each answer.")
        ui.write("\nWhat happens next:")
        ui.write("  1. Install OpsGraph and the software it needs.")
        ui.write("  2. Choose your database and model service.")
        ui.write("  3. Open your private workspace in the browser.")
        if args.install_only:
            ui.write("You chose installation only. Setup and the browser will not open yet.")
        ui.write(
            "\nDownloads use PyPI and uv's Python service. Python may be downloaded if needed."
        )
        ui.write("No administrator access needed. Your existing settings and history stay safe.")
        ui.write("This installs OpsGraph, not PostgreSQL or model files.")
        if not args.yes and input(
            ui.question("Install and continue? Press Enter for Yes, or type n to cancel")
        ).strip().lower() not in {
            "",
            "y",
            "yes",
        }:
            ui.write("Installation cancelled. No files were changed.")
            return 1
        runtime = root / ".venv"
        if runtime.exists() or runtime.is_symlink():
            info = runtime.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (os.name == "posix" and info.st_uid != os.getuid())
            ):
                raise StartError(
                    "The existing .venv must be a directory you own, without a symlink or "
                    "reparse point. Use a fresh checkout to avoid changing another runtime."
                )
        environment = install_environment()
        ui.heading("Installation 1 of 2: Prepare the software")
        with ui.progress("Preparing the installer"):
            uv = bootstrap_uv(root, environment)
        with ui.progress("Installing required software"):
            command(
                [
                    uv,
                    "sync",
                    "--locked",
                    "--extra",
                    "providers",
                    "--no-install-project",
                    "--no-build",
                    "--python",
                    ">=3.11,<3.14",
                ],
                root,
                environment,
                "Dependency installation failed. Check internet access, certificates, disk space "
                "and Python compatibility. Rerun Start.py to retry. "
                "Private configuration is unchanged.",
            )
        ui.heading("Installation 2 of 2: Install OpsGraph")
        python = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        with tempfile.TemporaryDirectory(prefix="opsgraph-install-") as directory:
            with ui.progress("Building OpsGraph"):
                command(
                    [
                        uv,
                        "build",
                        "--wheel",
                        "--build-constraints",
                        "requirements-build.lock",
                        "--require-hashes",
                        "--out-dir",
                        directory,
                    ],
                    root,
                    environment,
                    "OpsGraph build failed. Restore a complete, consistent source checkout "
                    "and retry.",
                )
            wheels = list(Path(directory).glob("opsgraph-*-py3-none-any.whl"))
            if len(wheels) != 1:
                raise StartError(
                    "The build did not produce exactly one OpsGraph application wheel."
                )
            with ui.progress("Installing OpsGraph"):
                command(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        "--no-deps",
                        "--reinstall-package",
                        "opsgraph",
                        str(wheels[0]),
                    ],
                    root,
                    environment,
                    "OpsGraph installation failed. Check disk space and retry.",
                )
        if args.install_only:
            ui.write("\nInstalled. Run Start.py again to set up and open your private workspace.")
            return 0
        ui.heading("Installation complete. Let's set up your workspace.")
        launch = [
            uv,
            "run",
            "--locked",
            "--no-sync",
            "opsgraph",
            "launch",
            "--port",
            str(args.port),
        ]
        for flag in ("configure", "no_browser"):
            if getattr(args, flag):
                launch.append("--" + flag.replace("_", "-"))
        if args.directory is not None:
            launch.extend(("--directory", str(args.directory)))
        if args.flow is not None:
            launch.extend(("--flow", args.flow))
        return subprocess.call(launch, cwd=root, env=environment)  # noqa: S603
    except (EOFError, KeyboardInterrupt):
        print("\nStopped. Rerun Start.py to continue. Existing workspace history is preserved.")
        return 1
    except (OSError, StartError):
        # Installer diagnostics must not expose proxy credentials or environment values.
        error = sys.exc_info()[1]
        print(
            str(error)
            if isinstance(error, StartError)
            else "Installation could not start. Check Python, permissions and free disk space."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(start())
