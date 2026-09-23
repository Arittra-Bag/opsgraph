"""Verify release-job artifacts before any package or GitHub publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

NATIVE_TARGETS = (
    "ubuntu-x64-cp311",
    "windows-x64-cp311",
    "macos-arm64-cp311",
)
CONTAINER_TARGETS = {
    "linux-amd64": ("linux/amd64", False),
    "linux-arm64": ("linux/arm64", True),
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DOCKERFILE = PROJECT_ROOT / "deploy" / "container" / "Dockerfile"
MAX_JSON_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024**3
MAX_TAR_MEMBERS = 100_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_IMAGE_ID = re.compile(r"^sha256:([0-9a-f]{64})$")
_CREATED = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[^\s]+$")
_PYTHON_311 = re.compile(r"^3\.11\.[0-9]+$")


class ReleaseAssetError(ValueError):
    """Release artifacts do not form one complete verified set."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _artifact(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ReleaseAssetError(f"release artifact is not a regular file: {path.name}")
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ReleaseAssetError(f"release artifact exceeds the size limit: {path.name}")
    return path


def _json(path: Path) -> dict[str, Any]:
    _artifact(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ReleaseAssetError(f"release receipt exceeds the size limit: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"invalid release receipt: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseAssetError(f"release receipt must be an object: {path.name}")
    return value


def _require(value: bool, message: str) -> None:
    if not value:
        raise ReleaseAssetError(message)


def _tar_member_bytes(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    *,
    limit: int,
) -> bytes:
    member = members.get(name)
    _require(member is not None and member.isfile(), f"container archive omitted {name}")
    _require(member.size <= limit, f"container archive member is too large: {name}")
    stream = archive.extractfile(member)
    _require(stream is not None, f"container archive could not read {name}")
    value = stream.read(limit + 1)
    _require(len(value) == member.size, f"container archive member size mismatch: {name}")
    return value


def _tar_json(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> Any:
    data = _tar_member_bytes(archive, members, name, limit=MAX_JSON_BYTES)
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"container archive has invalid JSON: {name}") from exc


def _safe_tar_name(name: str) -> str:
    normalized = name.removeprefix("./").rstrip("/")
    parts = normalized.split("/")
    _require(
        bool(normalized)
        and not name.startswith("/")
        and "\\" not in name
        and all(part not in {"", ".", ".."} for part in parts),
        "container archive contains an unsafe member name",
    )
    return normalized


def _expected_image_labels(*, source_commit: str, version: str) -> dict[str, str]:
    return {
        "org.opencontainers.image.title": "OpsGraph",
        "org.opencontainers.image.description": (
            "Evidence-first, read-only PostgreSQL investigations"
        ),
        "org.opencontainers.image.url": "https://opsgraph-site-seven.vercel.app/",
        "org.opencontainers.image.source": "https://github.com/Arittra-Bag/opsgraph",
        "org.opencontainers.image.documentation": (
            "https://github.com/Arittra-Bag/opsgraph#readme"
        ),
        "org.opencontainers.image.licenses": "Apache-2.0",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": source_commit,
    }


def _verify_docker_image_archive(
    path: Path,
    *,
    platform: str,
    reference: str,
    image_id: str,
    source_commit: str,
    version: str,
) -> None:
    """Validate the identity-bearing parts of one Docker image archive."""

    expected_os, expected_architecture = platform.split("/", 1)
    image_match = _IMAGE_ID.fullmatch(image_id)
    _require(image_match is not None, "invalid container image ID")
    try:
        with tarfile.open(_artifact(path), mode="r:") as archive:
            archive_members = archive.getmembers()
            _require(
                0 < len(archive_members) <= MAX_TAR_MEMBERS,
                "container archive has an invalid member count",
            )
            members: dict[str, tarfile.TarInfo] = {}
            total = 0
            for member in archive_members:
                if member.name.removeprefix("./").rstrip("/") in {"", "."}:
                    _require(member.isdir(), "container archive root entry is invalid")
                    continue
                normalized = _safe_tar_name(member.name)
                _require(normalized not in members, "container archive has duplicate members")
                _require(
                    member.isfile() or member.isdir(),
                    f"container archive member is not a regular file: {normalized}",
                )
                total += member.size
                _require(total <= MAX_ARTIFACT_BYTES, "container archive expands beyond its limit")
                members[normalized] = member

            manifest = _tar_json(archive, members, "manifest.json")
            _require(
                isinstance(manifest, list) and len(manifest) == 1,
                "container archive must contain exactly one image manifest",
            )
            record = manifest[0]
            _require(isinstance(record, dict), "container archive manifest record is invalid")
            _require(
                record.get("RepoTags") == [reference],
                "container archive reference mismatch",
            )
            config_name = record.get("Config")
            _require(
                isinstance(config_name, str)
                and re.fullmatch(r"[0-9a-f]{64}\.json", config_name) is not None,
                "container archive config path is invalid",
            )
            config_bytes = _tar_member_bytes(archive, members, config_name, limit=MAX_JSON_BYTES)
            config_digest = hashlib.sha256(config_bytes).hexdigest()
            _require(
                config_name == f"{config_digest}.json",
                "container archive config filename does not match its SHA-256",
            )
            _require(
                image_match.group(1) == config_digest,
                "container archive config does not match the accepted image ID",
            )
            try:
                config = json.loads(config_bytes)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseAssetError("container archive image config is invalid") from exc
            _require(isinstance(config, dict), "container archive image config is invalid")
            _require(
                config.get("os") == expected_os
                and config.get("architecture") == expected_architecture,
                "container archive platform mismatch",
            )
            image_config = config.get("config")
            labels = image_config.get("Labels") if isinstance(image_config, dict) else None
            _require(isinstance(labels, dict), "container archive image labels are missing")
            for label, expected in _expected_image_labels(
                source_commit=source_commit, version=version
            ).items():
                _require(labels.get(label) == expected, f"container image label mismatch: {label}")
            created = labels.get("org.opencontainers.image.created")
            _require(
                isinstance(created, str) and _CREATED.fullmatch(created) is not None,
                "container image created label is invalid",
            )

            layers = record.get("Layers")
            _require(isinstance(layers, list) and bool(layers), "container archive has no layers")
            _require(len(layers) == len(set(layers)), "container archive repeats a layer reference")
            rootfs = config.get("rootfs")
            diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
            _require(
                isinstance(rootfs, dict)
                and rootfs.get("type") == "layers"
                and isinstance(diff_ids, list)
                and len(diff_ids) == len(layers)
                and all(
                    isinstance(value, str) and _IMAGE_ID.fullmatch(value) is not None
                    for value in diff_ids
                ),
                "container archive root filesystem identity is invalid",
            )
            for layer in layers:
                _require(isinstance(layer, str), "container archive layer path is invalid")
                normalized = _safe_tar_name(layer)
                _require(
                    normalized == layer and layer.endswith("/layer.tar"),
                    "container archive layer path is invalid",
                )
                member = members.get(layer)
                _require(
                    member is not None and member.isfile(),
                    f"container archive omitted layer: {layer}",
                )
                stream = archive.extractfile(member)
                _require(stream is not None, f"container archive could not read layer: {layer}")
                while stream.read(1024 * 1024):
                    pass
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseAssetError(f"invalid Docker image archive: {path.name}") from exc


def _expected_names(version: str) -> set[str]:
    names = {
        f"opsgraph-{version}-py3-none-any.whl",
        f"opsgraph-{version}.tar.gz",
        f"opsgraph-{version}-third-party-sources.tar.gz",
        "source-build-receipt.json",
        "connected-smoke-receipt.json",
    }
    for target in NATIVE_TARGETS:
        archive = f"opsgraph-{version}-{target}.zip"
        names.update({archive, f"{archive}.sha256", f"acceptance-{target}.json"})
    names.update(f"container-smoke-{name}.json" for name in CONTAINER_TARGETS)
    return names


def _verify_source_receipt(root: Path, *, source_commit: str, tag: str, version: str) -> str:
    receipt = _json(root / "source-build-receipt.json")
    _require(receipt.get("schema_version") == 1, "unsupported source receipt schema")
    _require(receipt.get("result") == "pass", "source build did not pass")
    _require(receipt.get("source_commit") == source_commit, "source receipt commit mismatch")
    _require(receipt.get("tag") == tag, "source receipt tag mismatch")
    _require(receipt.get("version") == version, "source receipt version mismatch")
    records = receipt.get("artifacts")
    _require(isinstance(records, list) and len(records) == 3, "invalid source artifact records")
    expected = {
        f"opsgraph-{version}-py3-none-any.whl",
        f"opsgraph-{version}.tar.gz",
        f"opsgraph-{version}-third-party-sources.tar.gz",
    }
    seen: set[str] = set()
    application_wheel_sha256 = ""
    for record in records:
        _require(isinstance(record, dict), "invalid source artifact record")
        name = record.get("name")
        checksum = record.get("sha256")
        _require(isinstance(name, str) and name in expected, "unexpected source artifact")
        _require(name not in seen, "duplicate source artifact record")
        _require(
            isinstance(checksum, str) and _SHA256.fullmatch(checksum) is not None,
            "invalid source artifact checksum",
        )
        _require(
            digest(_artifact(root / name)) == checksum, f"source artifact hash mismatch: {name}"
        )
        if name == f"opsgraph-{version}-py3-none-any.whl":
            application_wheel_sha256 = checksum
        seen.add(name)
    _require(seen == expected, "source receipt omitted an artifact")
    _require(bool(application_wheel_sha256), "source receipt omitted the application wheel")
    return application_wheel_sha256


def _verify_connected_receipt(root: Path, *, source_commit: str, tag: str) -> None:
    receipt = _json(root / "connected-smoke-receipt.json")
    _require(receipt.get("schema_version") == 1, "unsupported connected receipt schema")
    _require(receipt.get("result") == "pass", "connected smoke did not pass")
    _require(receipt.get("source_commit") == source_commit, "connected receipt commit mismatch")
    _require(receipt.get("tag") == tag, "connected receipt tag mismatch")
    checks = receipt.get("checks")
    _require(
        isinstance(checks, dict) and checks.get("ok") is True,
        "connected smoke receipt is incomplete",
    )
    for name in (
        "bounded_readiness",
        "investigation_capture_export",
        "database_write_denial",
        "application_write_sql_denial",
        "history_after_restart",
    ):
        _require(checks.get(name) is True, f"connected smoke omitted required check: {name}")
    _require(
        checks.get("postgresql") == "real loopback service"
        and checks.get("source_role") == "dedicated SELECT-only login"
        and checks.get("provider") == "local OpenAI-compatible protocol fixture",
        "connected smoke did not exercise the expected real and fixture boundaries",
    )
    _require(
        checks.get("provider_save_restart_probe") is True
        and checks.get("checked_api_response_credential_leakage") is False
        and checks.get("source_password_in_state_database") is False
        and checks.get("model_quality_tested") is False,
        "connected smoke security or evidence boundary is incomplete",
    )


def _verify_native_receipts(
    root: Path,
    *,
    source_commit: str,
    version: str,
    application_wheel_sha256: str,
) -> None:
    build_ids: set[str] = set()
    inventory_hashes: set[str] = set()
    expected_systems = {
        "ubuntu-x64-cp311": "Linux",
        "windows-x64-cp311": "Windows",
        "macos-arm64-cp311": "Darwin",
    }
    for target in NATIVE_TARGETS:
        archive_name = f"opsgraph-{version}-{target}.zip"
        archive = _artifact(root / archive_name)
        receipt = _json(root / f"acceptance-{target}.json")
        _require(receipt.get("schema_version") == 1, f"unsupported receipt schema: {target}")
        _require(receipt.get("result") == "pass", f"native acceptance did not pass: {target}")
        _require(receipt.get("target") == target, f"native target mismatch: {target}")
        _require(
            receipt.get("source_base_commit") == source_commit,
            f"native source commit mismatch: {target}",
        )
        build_id = receipt.get("build_id")
        archive_hash = receipt.get("archive_sha256")
        inventory_hash = receipt.get("source_inventory_sha256")
        _require(
            isinstance(inventory_hash, str) and _SHA256.fullmatch(inventory_hash) is not None,
            f"invalid source inventory hash: {target}",
        )
        inventory_hashes.add(inventory_hash)
        _require(
            receipt.get("application_wheel_sha256") == application_wheel_sha256,
            f"native application wheel mismatch: {target}",
        )
        _require(
            receipt.get("host_system") == expected_systems[target]
            and isinstance(receipt.get("host"), str)
            and bool(receipt["host"].strip())
            and isinstance(receipt.get("python"), str)
            and _PYTHON_311.fullmatch(receipt["python"]) is not None,
            f"native host evidence mismatch: {target}",
        )
        _require(
            isinstance(build_id, str) and _SHA256.fullmatch(build_id) is not None,
            f"invalid build ID: {target}",
        )
        _require(build_id not in build_ids, "native build IDs must be platform-specific")
        build_ids.add(build_id)
        _require(receipt.get("archive") == archive_name, f"native archive mismatch: {target}")
        _require(
            isinstance(archive_hash, str) and _SHA256.fullmatch(archive_hash) is not None,
            f"invalid native archive checksum: {target}",
        )
        _require(digest(archive) == archive_hash, f"native archive hash mismatch: {target}")
        checksum_file = _artifact(root / f"{archive_name}.sha256")
        _require(
            checksum_file.read_text(encoding="utf-8") == f"{archive_hash}  {archive_name}\n",
            f"native checksum file mismatch: {target}",
        )
    _require(
        len(inventory_hashes) == 1,
        "native bundles were built from different source inventories",
    )


def _verify_container_receipts(
    root: Path,
    container_root: Path,
    *,
    source_commit: str,
    version: str,
) -> None:
    _require(
        container_root.is_dir() and not container_root.is_symlink(),
        "container artifact root is invalid",
    )
    container_entries = tuple(container_root.iterdir())
    expected_archives = {f"opsgraph-container-{name}.tar" for name in CONTAINER_TARGETS}
    _require(
        all(path.is_file() and not path.is_symlink() for path in container_entries)
        and {path.name for path in container_entries} == expected_archives,
        "container artifact set is incomplete or unexpected",
    )
    dockerfile_hashes: set[str] = set()
    for name, (platform_name, emulated) in CONTAINER_TARGETS.items():
        receipt = _json(root / f"container-smoke-{name}.json")
        _require(receipt.get("schema_version") == 1, f"unsupported container receipt: {name}")
        _require(receipt.get("result") == "pass", f"container smoke did not pass: {name}")
        _require(
            receipt.get("source_commit") == source_commit,
            f"container source commit mismatch: {name}",
        )
        _require(receipt.get("version") == version, f"container version mismatch: {name}")
        _require(receipt.get("platform") == platform_name, f"container platform mismatch: {name}")
        _require(
            receipt.get("emulated") is emulated, f"container emulation marker mismatch: {name}"
        )
        _require(receipt.get("runtime_ready") is True, f"container runtime was not ready: {name}")
        image_id = receipt.get("image_id")
        dockerfile_hash = receipt.get("dockerfile_sha256")
        archive_name = receipt.get("image_archive")
        archive_hash = receipt.get("image_archive_sha256")
        _require(
            isinstance(image_id, str) and _IMAGE_ID.fullmatch(image_id) is not None,
            f"invalid container image ID: {name}",
        )
        _require(
            isinstance(dockerfile_hash, str) and _SHA256.fullmatch(dockerfile_hash),
            f"invalid Dockerfile hash: {name}",
        )
        _require(
            archive_name == f"opsgraph-container-{name}.tar"
            and isinstance(archive_hash, str)
            and _SHA256.fullmatch(archive_hash) is not None,
            f"invalid container archive identity: {name}",
        )
        archive = _artifact(container_root / archive_name)
        _require(
            digest(archive) == archive_hash,
            f"container archive hash mismatch: {name}",
        )
        _verify_docker_image_archive(
            archive,
            platform=platform_name,
            reference=f"opsgraph-release-smoke:{name}",
            image_id=image_id,
            source_commit=source_commit,
            version=version,
        )
        dockerfile_hashes.add(dockerfile_hash)
    _require(len(dockerfile_hashes) == 1, "container platforms used different Dockerfiles")
    _require(
        dockerfile_hashes == {digest(_artifact(RELEASE_DOCKERFILE))},
        "container receipts do not match the release Dockerfile",
    )


def verify_and_stage(
    root: Path,
    container_root: Path,
    output: Path,
    *,
    source_commit: str,
    tag: str,
    version: str,
) -> tuple[Path, ...]:
    """Verify one exact artifact set and stage it with an aggregate checksum file."""

    _require(_SOURCE_COMMIT.fullmatch(source_commit) is not None, "invalid source commit")
    _require(_VERSION.fullmatch(version) is not None, "invalid stable version")
    _require(tag == f"v{version}", "release tag and version do not match")
    _require(root.is_dir() and not root.is_symlink(), "release artifact root is invalid")
    expected = _expected_names(version)
    entries = tuple(root.iterdir())
    _require(
        all(path.is_file() or path.is_symlink() for path in entries),
        "downloaded release artifacts must be flat files",
    )
    actual = {path.name for path in entries}
    _require(actual == expected, "downloaded release artifact set is incomplete or unexpected")

    application_wheel_sha256 = _verify_source_receipt(
        root, source_commit=source_commit, tag=tag, version=version
    )
    _verify_connected_receipt(root, source_commit=source_commit, tag=tag)
    _verify_native_receipts(
        root,
        source_commit=source_commit,
        version=version,
        application_wheel_sha256=application_wheel_sha256,
    )
    _verify_container_receipts(
        root,
        container_root,
        source_commit=source_commit,
        version=version,
    )

    if output.exists():
        raise ReleaseAssetError("release staging directory already exists")
    output.mkdir(parents=True)  # NOSONAR - operator-selected staging directory
    staged = []
    for name in sorted(expected):
        source = _artifact(root / name)
        destination = output / name
        shutil.copyfile(source, destination)
        _require(digest(destination) == digest(source), f"staged artifact hash mismatch: {name}")
        staged.append(destination)
    sums = output / "SHA256SUMS"
    sums.write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in staged),
        encoding="utf-8",
        newline="\n",
    )
    return (*staged, sums)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--container-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        staged = verify_and_stage(
            args.directory,
            args.container_directory,
            args.output,
            source_commit=args.source_commit,
            tag=args.tag,
            version=args.version,
        )
    except (OSError, ReleaseAssetError) as exc:
        parser.exit(1, f"Release assets: {exc}\n")
    print(f"Verified and staged {len(staged)} release files.")


if __name__ == "__main__":
    main()
