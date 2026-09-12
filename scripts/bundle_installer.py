"""Standard-library entry point copied to Install.py in an offline bundle."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import venv
from pathlib import Path, PurePosixPath

PLATFORMS = {
    "macos-arm64-cp311": ("darwin", {"arm64", "aarch64"}),
    "windows-x64-cp311": ("win32", {"amd64", "x86_64"}),
    "ubuntu-x64-cp311": ("linux", {"amd64", "x86_64"}),
}
OWNER = "opsgraph-offline-bundle-v1"
MARKER = ".opsgraph-bundle.json"
HEADROOM = 2 * 1024**3


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def regular(path):
    if linked(path) or not path.is_file():
        raise ValueError(f"Expected a regular file: {path.name}")
    return path.read_bytes()


def checked_path(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or relative != "/".join(parts) or any(p in {"..", "."} for p in parts):
        raise ValueError("Invalid manifest path")
    if "\\" in relative or ":" in relative or relative.startswith("/"):
        raise ValueError("Invalid manifest path")
    path = root
    for part in parts:
        path = path / part
        if linked(path):
            raise ValueError("Bundle paths must not be links or junctions")
    return path


def validate_lock(data):
    requirement = r"[\w.-]+(?:\[[\w,.-]+\])?==[\w.+!-]+(?:\s*;[^\r\n]*)?"
    for line in data.decode("utf-8").splitlines():
        line = line.strip().removesuffix("\\").strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(requirement, line) and not re.fullmatch(
            r"--hash=sha256:[0-9a-f]{64}", line
        ):
            raise ValueError("Lock must contain only pinned packages, markers and SHA-256 hashes")


def verify(root):
    if linked(root):
        raise ValueError("The bundle directory must not be a link or junction")
    manifest = json.loads(regular(root / "manifest.json"))
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("platform"), str)
        or manifest["platform"] not in PLATFORMS
    ):
        raise ValueError("Unsupported bundle manifest")
    build_id = manifest.get("build_id")
    if (
        manifest.get("python") != "3.11"
        or not isinstance(build_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", build_id)
    ):
        raise ValueError("Invalid bundle identity")
    if not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("wheel"), str):
        raise ValueError("Invalid manifest files or wheel")
    seen = set()
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or type(item.get("size")) is not int
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ValueError("Invalid manifest file record")
        name = item["path"]
        if name in seen or name.startswith(".venv/"):
            raise ValueError("Duplicate or runtime manifest path")
        seen.add(name)
        data = regular(checked_path(root, name))
        if len(data) != item["size"] or digest(data) != item["sha256"]:
            raise ValueError(f"Checksum mismatch: {name}; obtain a fresh bundle")
    required = {"Install.py", "requirements.lock", "build-identity.json", manifest["wheel"]}
    if not required <= seen or not manifest["wheel"].startswith("wheels/"):
        raise ValueError("Bundle is missing required files")
    identity = json.loads(regular(root / "build-identity.json"))
    if (
        not isinstance(identity, dict)
        or identity.pop("build_id", None) != manifest["build_id"]
        or digest(encoded(identity)) != manifest["build_id"]
        or identity.get("platform") != manifest["platform"]
        or identity.get("python") != manifest["python"]
    ):
        raise ValueError("Build identity does not match the manifest")
    wheelhouse = checked_path(root, "wheelhouse")
    actual = {"wheelhouse/" + p.name for p in wheelhouse.iterdir()}
    expected = {p for p in seen if p.startswith("wheelhouse/")}
    if not expected or actual != expected or any(not p.endswith(".whl") for p in actual):
        raise ValueError("Wheelhouse must contain exactly the manifested dependency wheels")
    validate_lock(regular(root / "requirements.lock"))
    return manifest


def check_platform(manifest):
    system, machines = PLATFORMS[manifest["platform"]]
    if sys.version_info[:2] != (3, 11) or platform.python_implementation() != "CPython":
        raise ValueError("Install CPython 3.11 first; this bundle does not download Python")
    if sys.platform != system or platform.machine().lower() not in machines:
        raise ValueError(f"This bundle targets {manifest['platform']}; choose a matching bundle")


def ownership(root, manifest):
    runtime = root / ".venv"
    if linked(runtime) or not runtime.is_dir():
        raise ValueError("Runtime must be a real directory owned by this bundle")
    marker = json.loads(regular(runtime / MARKER))
    expected = {"owner": OWNER, "build_id": manifest["build_id"], "directory": str(root)}
    if not isinstance(marker, dict) or any(
        marker.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("Runtime ownership differs; it will not be changed or launched")
    return marker


def run(arguments, root):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("PIP_", "PYTHON"))
    }
    for key in ("VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    # --isolated ignores user settings, but global/site config can still redirect
    # find-links, target or prefix. The explicit null config disables all files.
    env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run(arguments, cwd=root, env=env, check=True)  # noqa: S603


def install(root, manifest):
    check_platform(manifest)
    runtime = root / ".venv"
    if runtime.exists() or runtime.is_symlink():
        if ownership(root, manifest).get("complete"):
            print("Already installed. Use Launch; your workspace data is preserved.")
            return
        raise ValueError("An incomplete runtime exists. Run Uninstall, then Install again")
    if shutil.disk_usage(root).free < HEADROOM:
        raise ValueError(
            "Installation needs at least 2 GiB free disk space; model storage is extra"
        )
    runtime.mkdir(mode=0o700)
    marker = {"owner": OWNER, "build_id": manifest["build_id"], "directory": str(root)}
    (runtime / MARKER).write_text(json.dumps(marker), encoding="utf-8")
    venv.create(runtime, with_pip=True, symlinks=False)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pip = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    offline = ["install", "--no-index", "--no-cache-dir", "--only-binary=:all:"]
    run(
        pip
        + offline
        + [
            "--find-links",
            str(root / "wheelhouse"),
            "--require-hashes",
            "-r",
            str(root / "requirements.lock"),
        ],
        root,
    )
    run(pip + offline + ["--no-deps", str(root / manifest["wheel"])], root)
    marker["complete"] = True
    (runtime / MARKER).write_text(json.dumps(marker), encoding="utf-8")
    print("Installed offline. Use Launch. Python, PostgreSQL and model setup remain separate.")


def uninstall(root, manifest):
    ownership(root, manifest)
    runtime = root / ".venv"
    print(f"Remove only this installed runtime: {runtime}\nWorkspace data will be retained.")
    if input("Type REMOVE to continue: ") != "REMOVE":
        print("Uninstall cancelled.")
        return
    ownership(root, manifest)
    # rmtree does not follow nested symbolic links. Refuse Windows reparse points
    # explicitly; normal Windows venvs do not need directory junctions.
    if os.name == "nt":
        for directory, folders, files in os.walk(runtime, followlinks=False):
            if any(linked(Path(directory) / name) for name in folders + files):
                raise ValueError("Runtime contains a link/junction; remove it manually first")
    shutil.rmtree(runtime)
    print("Runtime removed. Workspace data and the downloaded bundle were retained.")


def launch_arguments(arguments):
    """Rebuild only supported launcher options; never forward opaque CLI input."""
    parser = argparse.ArgumentParser(prog="Install.py launch", allow_abbrev=False)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args(arguments)
    if options.port is not None and not 1 <= options.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    result = []
    if options.directory is not None:
        # The equals form keeps even a dash-prefixed directory a value, not an option.
        result.append("--directory=" + str(options.directory))
    if options.port is not None:
        result.extend(["--port", str(options.port)])
    if options.configure:
        result.append("--configure")
    if options.no_browser:
        result.append("--no-browser")
    return result


def main():
    parser = argparse.ArgumentParser(description="Offline OpsGraph bundle; CPython 3.11 required")
    parser.add_argument(
        "action", choices=("install", "launch", "uninstall"), nargs="?", default="install"
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    launch_options = launch_arguments(args.arguments) if args.action == "launch" else []
    root = Path(__file__).absolute().parent
    try:
        manifest = verify(root)
        if args.action == "launch":
            check_platform(manifest)
            if not ownership(root, manifest).get("complete"):
                raise ValueError("Install must finish before Launch")
            executable = (
                root / ".venv" / ("Scripts/opsgraph.exe" if os.name == "nt" else "bin/opsgraph")
            )
            regular(checked_path(root, executable.relative_to(root).as_posix()))
            run([str(executable), "launch", *launch_options], root)
        elif args.arguments:
            raise ValueError("Additional arguments are accepted only for launch")
        elif args.action == "install":
            install(root, manifest)
        else:
            uninstall(root, manifest)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        EOFError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.exit(1, f"OpsGraph bundle: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nStopped. Workspace data is retained.\n")


if __name__ == "__main__":
    main()
