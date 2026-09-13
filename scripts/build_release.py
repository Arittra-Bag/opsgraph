"""Create a deterministic offline ZIP from an already built wheel and wheelhouse.

No build, download, signing, publication or platform-validation claim is performed.
"""

import argparse
import io
import json
import re
import shutil
import stat
import subprocess
import tomllib
import zipfile
from pathlib import Path

from bundle_installer import (
    PLATFORMS,
    checked_path,
    digest,
    encoded,
    linked,
    regular,
    validate_lock,
)

# Share the reviewed documentation allowlist with the source distribution.
# Adding a file beneath docs/ alone never makes it a distributable document.
_BUILD_CONFIG = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)
PUBLIC_DOCS = frozenset(
    _BUILD_CONFIG["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"]
)

ROOT_FILES = {
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "requirements.lock",
    "requirements-build.lock",
    "uv.lock",
    "build-constraints.txt",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".env.example",
    "Dockerfile",
}
ROOT_DIRS = {
    "src",
    "tests",
    "scripts",
    "web",
    "docs",
    "deploy",
    ".github",
    "skillpacks",
    "policies",
}
EXCLUDED = {
    ".git",
    ".venv",
    ".opsgraph",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "artifacts",
    "reviews",
    ".review",
    ".codex",
    ".agents",
}
PRIVATE_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".jsonl",
    ".whl",
    ".zip",
    ".tar",
    ".gz",
}
SOURCE_SCOPE = "product code, tests, tooling, docs, deployment and declarative resources"
MAX_INVENTORY_BYTES = 4 * 1024**2
MAX_SOURCE_FILES = 10_000
MAX_SOURCE_BYTES = 256 * 1024**2


def record(name, data):
    return {"path": name, "size": len(data), "sha256": digest(data)}


def allowed_source(name):
    path = Path(name)
    return (
        bool(path.parts)
        and (path.parts[0] in ROOT_DIRS or name in ROOT_FILES)
        and (path.parts[0] != "docs" or name in PUBLIC_DOCS)
        and not EXCLUDED.intersection(path.parts)
        and path.suffix not in PRIVATE_SUFFIXES
        and path.name != ".DS_Store"
        and not (path.name.startswith(".env") and path.name != ".env.example")
    )


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate source inventory JSON key")
        result[key] = value
    return result


def inventory_snapshot(root, inventory_path, expected_sha256):
    """Read only files authenticated by a separately trusted inventory digest.

    Unlisted files, including sdist-generated PKG-INFO, are outside this snapshot.
    No source content is imported, executed or added to Git.
    """
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("A trusted source inventory SHA-256 is required")
    if linked(root) or not root.is_dir():
        raise ValueError("Source must be a real directory")
    if linked(inventory_path) or not inventory_path.is_file():
        raise ValueError("Source inventory must be a regular file")
    with inventory_path.open("rb") as stream:
        data = stream.read(MAX_INVENTORY_BYTES + 1)
    if len(data) > MAX_INVENTORY_BYTES:
        raise ValueError("Source inventory exceeds the size limit")
    if digest(data) != expected_sha256:
        raise ValueError("Source inventory SHA-256 mismatch")
    try:
        inventory = json.loads(data, object_pairs_hook=unique_object)
    except RecursionError:
        raise ValueError("Source inventory nesting exceeds the parser limit") from None
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"schema_version", "source_base_commit", "scope", "excluded", "files"}
        or type(inventory.get("schema_version")) is not int
        or inventory["schema_version"] != 1
        or inventory.get("scope") != SOURCE_SCOPE
        or inventory.get("excluded") != sorted(EXCLUDED | PRIVATE_SUFFIXES)
        or not isinstance(inventory.get("source_base_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", inventory["source_base_commit"])
        or not isinstance(inventory.get("files"), list)
        or not 0 < len(inventory["files"]) <= MAX_SOURCE_FILES
    ):
        raise ValueError("Unsupported source inventory")
    files = {}
    total_size = 0
    for item in inventory["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or not allowed_source(item["path"])
            or type(item.get("size")) is not int
            or not 0 <= item["size"] <= MAX_SOURCE_BYTES
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ValueError("Invalid source inventory file record")
        name = item["path"]
        if name in files:
            raise ValueError("Duplicate source inventory path")
        total_size += item["size"]
        if total_size > MAX_SOURCE_BYTES:
            raise ValueError("Source inventory exceeds the total size limit")
        path = checked_path(root, name)
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise ValueError(f"Source inventory size mismatch: {name}")
        with path.open("rb") as stream:
            content = stream.read(item["size"] + 1)
        if len(content) != item["size"] or digest(content) != item["sha256"]:
            raise ValueError(f"Source inventory checksum mismatch: {name}")
        files[name] = content
    return inventory["source_base_commit"], files


def snapshot(root):
    git = shutil.which("git")
    if not git:
        raise ValueError("Git is required to inventory the source candidate")
    result = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    files = {}
    for name in sorted(set(result.stdout.decode().split("\0")) - {""}):
        path = Path(name)
        if not allowed_source(name):
            continue
        if not (root / path).exists():
            continue  # A deleted tracked file is absent from the content inventory.
        files[name] = regular(checked_path(root, name))
    base = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return base, files


def build(
    source,
    wheel,
    wheelhouse,
    output,
    target,
    *,
    source_inventory=None,
    source_inventory_sha256=None,
):
    if linked(wheelhouse) or not wheelhouse.is_dir():
        raise ValueError("Wheelhouse must be a real directory")
    if (source_inventory is None) != (source_inventory_sha256 is None):
        raise ValueError("Supply both --source-inventory and --source-inventory-sha256")
    base, sources = (
        snapshot(source)
        if source_inventory is None
        else inventory_snapshot(source, source_inventory, source_inventory_sha256)
    )
    lock = sources["requirements.lock"]
    validate_lock(lock)
    wheel_bytes = regular(wheel)
    if not re.fullmatch(r"opsgraph-[^/]+\.whl", wheel.name):
        raise ValueError("Expected the OpsGraph application wheel")
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        names = archive.namelist()
        package = {
            name: archive.read(name)
            for name in names
            if name.startswith("opsgraph/") and not name.endswith("/")
        }
        expected = {
            name[4:]: data for name, data in sources.items() if name.startswith("src/opsgraph/")
        }
        if len(names) != len(set(names)) or package != expected:
            raise ValueError(
                "Application wheel does not exactly match the inventoried package source"
            )
    payload = {name: data for name, data in sources.items() if name in PUBLIC_DOCS}
    payload.update(
        {
            name: sources[name]
            for name in ("LICENSE", "README.md", "SECURITY.md", "requirements.lock")
        }
    )
    payload["Install.py"] = sources["scripts/bundle_installer.py"]
    prefix = "scripts/bundle-launchers/"
    payload.update(
        {name[len(prefix) :]: data for name, data in sources.items() if name.startswith(prefix)}
    )
    wheel_name = "wheels/" + wheel.name
    payload[wheel_name] = wheel_bytes
    for dependency in sorted(wheelhouse.iterdir()):
        data = regular(dependency)
        if dependency.suffix != ".whl" or ("sha256:" + digest(data)).encode() not in lock:
            raise ValueError(
                f"Dependency is not a wheel pinned by requirements.lock: {dependency.name}"
            )
        payload["wheelhouse/" + dependency.name] = data
    if not any(name.startswith("wheelhouse/") for name in payload):
        raise ValueError("A populated offline wheelhouse is required")
    inventory = {
        "schema_version": 1,
        "source_base_commit": base,
        "scope": SOURCE_SCOPE,
        "excluded": sorted(EXCLUDED | PRIVATE_SUFFIXES),
        "files": [record(name, data) for name, data in sorted(sources.items())],
    }
    payload["source-inventory.json"] = encoded(inventory)
    identity = {
        "schema_version": 1,
        "platform": target,
        "python": "3.11",
        "source_base_commit": base,
        "source_inventory_sha256": digest(encoded(inventory)),
        "wheel": record(wheel_name, wheel_bytes),
        "validation": "not_assessed",
        "wheelhouse": [
            record(n, d) for n, d in sorted(payload.items()) if n.startswith("wheelhouse/")
        ],
    }
    build_id = digest(encoded(identity))
    payload["build-identity.json"] = encoded({**identity, "build_id": build_id})
    manifest = {
        "schema_version": 1,
        "build_id": build_id,
        "platform": target,
        "python": "3.11",
        "wheel": wheel_name,
        "files": [record(n, d) for n, d in sorted(payload.items())],
    }
    payload["manifest.json"] = encoded(manifest)
    payload["SHA256SUMS"] = "".join(
        f"{digest(d)}  {n}\n" for n, d in sorted(payload.items())
    ).encode()
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise ValueError("Output already exists; use a new candidate path")
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(f"opsgraph-{target}/{name}", (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (
                stat.S_IFREG | (0o755 if name.endswith(".command") else 0o644)
            ) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    checksum = digest(output.read_bytes())
    with output.with_suffix(output.suffix + ".sha256").open("x") as stream:
        stream.write(f"{checksum}  {output.name}\n")
    print(
        f"Created {output.name}\nBuild ID: {build_id}\nZIP SHA-256: {checksum}\n"
        "Platform validation: not assessed"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source-inventory", type=Path, help="Use an inventory instead of Git")
    parser.add_argument(
        "--source-inventory-sha256", help="Required SHA-256 from a trusted independent record"
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    args = parser.parse_args()
    try:
        build(
            args.source,
            args.wheel,
            args.wheelhouse,
            args.output,
            args.platform,
            source_inventory=args.source_inventory,
            source_inventory_sha256=args.source_inventory_sha256,
        )
    except (
        ValueError,
        OSError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.CalledProcessError,
    ) as exc:
        parser.exit(1, f"Bundle build: {exc}\n")


if __name__ == "__main__":
    main()
