import hashlib
import io
import json
import tarfile
import tomllib
from pathlib import Path

import pytest

from opsgraph import __version__
from scripts.verify_release_assets import (
    CONTAINER_TARGETS,
    NATIVE_TARGETS,
    RELEASE_DOCKERFILE,
    ReleaseAssetError,
    digest,
    verify_and_stage,
)

VERSION = "1.0.0"
TAG = "v1.0.0"
COMMIT = "a" * 40
WHEEL_NAME = f"opsgraph-{VERSION}-py3-none-any.whl"


def test_runtime_version_matches_project_metadata():
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert __version__ == project["project"]["version"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def add_tar_file(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mtime = 0
    archive.addfile(member, io.BytesIO(data))


def image_labels(**overrides: str) -> dict[str, str]:
    labels = {
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
        "org.opencontainers.image.version": VERSION,
        "org.opencontainers.image.revision": COMMIT,
        "org.opencontainers.image.created": "2026-09-22T12:00:00Z",
    }
    labels.update(overrides)
    return labels


def write_docker_archive(
    path: Path,
    *,
    slug: str,
    platform_name: str,
    labels: dict[str, str] | None = None,
    config_name: str | None = None,
) -> str:
    os_name, architecture = platform_name.split("/", 1)
    layer = f"layer bytes for {slug}".encode()
    config = {
        "architecture": architecture,
        "os": os_name,
        "config": {"Labels": labels or image_labels()},
        "rootfs": {"type": "layers", "diff_ids": [f"sha256:{sha(layer)}"]},
    }
    config_bytes = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
    image_hash = sha(config_bytes)
    config_member = config_name or f"{image_hash}.json"
    manifest = [
        {
            "Config": config_member,
            "RepoTags": [f"opsgraph-release-smoke:{slug}"],
            "Layers": [f"{slug}/layer.tar"],
        }
    ]
    with tarfile.open(path, "w") as archive:
        add_tar_file(archive, "manifest.json", json.dumps(manifest).encode())
        add_tar_file(archive, config_member, config_bytes)
        add_tar_file(archive, f"{slug}/layer.tar", layer)
    return f"sha256:{image_hash}"


def release_set(root: Path) -> Path:
    source_records = []
    source_artifacts = (
        (WHEEL_NAME, b"wheel"),
        (f"opsgraph-{VERSION}.tar.gz", b"sdist"),
        (f"opsgraph-{VERSION}-third-party-sources.tar.gz", b"third-party sources"),
    )
    for name, data in source_artifacts:
        (root / name).write_bytes(data)
        source_records.append({"name": name, "sha256": sha(data)})
    write_json(
        root / "source-build-receipt.json",
        {
            "schema_version": 1,
            "result": "pass",
            "source_commit": COMMIT,
            "tag": TAG,
            "version": VERSION,
            "artifacts": source_records,
        },
    )
    write_json(
        root / "connected-smoke-receipt.json",
        {
            "schema_version": 1,
            "result": "pass",
            "source_commit": COMMIT,
            "tag": TAG,
            "checks": {
                "ok": True,
                "bounded_readiness": True,
                "investigation_capture_export": True,
                "database_write_denial": True,
                "application_write_sql_denial": True,
                "history_after_restart": True,
                "postgresql": "real loopback service",
                "source_role": "dedicated SELECT-only login",
                "provider": "local OpenAI-compatible protocol fixture",
                "provider_save_restart_probe": True,
                "checked_api_response_credential_leakage": False,
                "source_password_in_state_database": False,
                "model_quality_tested": False,
            },
        },
    )
    host_systems = {
        "ubuntu-x64-cp311": "Linux",
        "windows-x64-cp311": "Windows",
        "macos-arm64-cp311": "Darwin",
    }
    for index, target in enumerate(NATIVE_TARGETS):
        archive = f"opsgraph-{VERSION}-{target}.zip"
        data = target.encode()
        checksum = sha(data)
        (root / archive).write_bytes(data)
        (root / f"{archive}.sha256").write_text(f"{checksum}  {archive}\n", encoding="utf-8")
        write_json(
            root / f"acceptance-{target}.json",
            {
                "schema_version": 1,
                "result": "pass",
                "target": target,
                "host": "fixture host",
                "host_system": host_systems[target],
                "python": "3.11.10",
                "source_base_commit": COMMIT,
                "source_inventory_sha256": "c" * 64,
                "application_wheel_sha256": sha(b"wheel"),
                "build_id": f"{index + 1:064x}",
                "archive": archive,
                "archive_sha256": checksum,
            },
        )
    container_root = root.parent / "container-images"
    container_root.mkdir()
    for name, (platform_name, emulated) in CONTAINER_TARGETS.items():
        archive_name = f"opsgraph-container-{name}.tar"
        archive = container_root / archive_name
        image_id = write_docker_archive(archive, slug=name, platform_name=platform_name)
        write_json(
            root / f"container-smoke-{name}.json",
            {
                "schema_version": 1,
                "result": "pass",
                "source_commit": COMMIT,
                "version": VERSION,
                "platform": platform_name,
                "emulated": emulated,
                "runtime_ready": True,
                "image_id": image_id,
                "dockerfile_sha256": digest(RELEASE_DOCKERFILE),
                "image_archive": archive_name,
                "image_archive_sha256": digest(archive),
            },
        )
    return container_root


def verify(incoming: Path, container_images: Path, output: Path) -> tuple[Path, ...]:
    return verify_and_stage(
        incoming,
        container_images,
        output,
        source_commit=COMMIT,
        tag=TAG,
        version=VERSION,
    )


def rewrite_receipt(path: Path, **changes: object) -> None:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt.update(changes)
    write_json(path, receipt)


def rewrite_container_archive(
    incoming: Path,
    container_images: Path,
    slug: str,
    **options: object,
) -> None:
    receipt_path = incoming / f"container-smoke-{slug}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archive = container_images / receipt["image_archive"]
    image_id = write_docker_archive(
        archive,
        slug=slug,
        platform_name=str(options.pop("platform_name", receipt["platform"])),
        **options,
    )
    receipt["image_id"] = image_id
    receipt["image_archive_sha256"] = digest(archive)
    write_json(receipt_path, receipt)


def test_verifies_and_stages_one_complete_release_set(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)

    staged = verify(incoming, container_images, tmp_path / "release")

    assert staged[-1].name == "SHA256SUMS"
    lines = staged[-1].read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(staged) - 1
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])


def test_rejects_a_tampered_native_bundle(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    (incoming / f"opsgraph-{VERSION}-ubuntu-x64-cp311.zip").write_bytes(b"tampered")

    with pytest.raises(ReleaseAssetError, match="native archive hash mismatch"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_a_native_bundle_built_from_another_application_wheel(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_receipt(
        incoming / "acceptance-windows-x64-cp311.json",
        application_wheel_sha256="f" * 64,
    )

    with pytest.raises(ReleaseAssetError, match="native application wheel mismatch"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_native_bundles_from_different_source_inventories(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_receipt(
        incoming / "acceptance-macos-arm64-cp311.json",
        source_inventory_sha256="f" * 64,
    )

    with pytest.raises(ReleaseAssetError, match="different source inventories"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_a_receipt_from_another_commit(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_receipt(incoming / "source-build-receipt.json", source_commit="f" * 40)

    with pytest.raises(ReleaseAssetError, match="source receipt commit mismatch"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_incomplete_connected_boundary_evidence(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    receipt_path = incoming / "connected-smoke-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["checks"]["provider"] = "unknown fixture"
    write_json(receipt_path, receipt)

    with pytest.raises(ReleaseAssetError, match="expected real and fixture boundaries"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_unexpected_downloaded_artifacts(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    (incoming / "unreviewed.bin").write_bytes(b"unexpected")

    with pytest.raises(ReleaseAssetError, match="incomplete or unexpected"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_nested_downloaded_artifacts(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    (incoming / "nested").mkdir()

    with pytest.raises(ReleaseAssetError, match="flat files"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_unexpected_container_artifacts(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    (container_images / "unreviewed.tar").write_bytes(b"unexpected")

    with pytest.raises(ReleaseAssetError, match="container artifact set"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_a_tampered_container_archive(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    with (container_images / "opsgraph-container-linux-amd64.tar").open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ReleaseAssetError, match="container archive hash mismatch"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_container_platform_mismatch_inside_archive(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_container_archive(
        incoming,
        container_images,
        "linux-amd64",
        platform_name="linux/arm64",
    )

    with pytest.raises(ReleaseAssetError, match="container archive platform mismatch"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_container_config_filename_that_is_not_its_digest(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_container_archive(
        incoming,
        container_images,
        "linux-amd64",
        config_name=f"{'f' * 64}.json",
    )

    with pytest.raises(ReleaseAssetError, match="config filename"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_container_label_mismatch_inside_archive(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_container_archive(
        incoming,
        container_images,
        "linux-amd64",
        labels=image_labels(**{"org.opencontainers.image.revision": "f" * 40}),
    )

    with pytest.raises(ReleaseAssetError, match="container image label mismatch"):
        verify(incoming, container_images, tmp_path / "release")


def test_rejects_container_receipt_for_another_dockerfile(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    container_images = release_set(incoming)
    rewrite_receipt(
        incoming / "container-smoke-linux-arm64.json",
        dockerfile_sha256="f" * 64,
    )

    with pytest.raises(ReleaseAssetError, match="different Dockerfiles"):
        verify(incoming, container_images, tmp_path / "release")
