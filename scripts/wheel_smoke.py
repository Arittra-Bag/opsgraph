"""Install the built wheel into an isolated environment; no source/model traffic.

Run after ``uv build``. Uses the committed hashed runtime dependency lock.
This is package validation, never real connector or platform acceptance.
"""

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    wheels = list(root.joinpath("dist").glob("opsgraph-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected one wheel in dist; build into an empty output directory.")
    with tempfile.TemporaryDirectory(prefix="opsgraph-wheel-") as directory:
        work = Path(directory)
        environment = work / "venv"
        venv.create(environment, with_pip=True)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("OPSGRAPH_")
            and key not in {"PYTHONPATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
        }
        env["LANGSMITH_TRACING"] = "false"
        subprocess.run(  # noqa: S603
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "-r",
                str(root / "requirements.lock"),
            ],
            check=True,
            cwd=work,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
            check=True,
            cwd=work,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [str(python), "-m", "opsgraph.cli", "init"],
            check=True,
            cwd=work,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [
                str(python),
                "-c",
                "from importlib.metadata import version; "
                "from opsgraph.config import get_settings; "
                "from opsgraph.runtime import build_runtime; "
                "s = get_settings(); r = build_runtime(s); "
                "assert s.web_root.joinpath('index.html').is_file(); "
                "assert s.mode == 'connected'; "
                "assert s.model_provider == 'openai_compatible'; "
                "assert s.local_reasoning_effort == 'none'; "
                "assert s.local_schema_profile == 'ollama'; "
                "assert any(k.id == 'generic-readonly' for k in r.skills.list_published()); "
                "print('Installed OpsGraph version:', version('opsgraph'))",
            ],
            check=True,
            cwd=work,
            env=env,
        )
    print(
        f"Wheel install/configuration smoke passed on {sys.platform}; no live acceptance performed."
    )


if __name__ == "__main__":
    main()
