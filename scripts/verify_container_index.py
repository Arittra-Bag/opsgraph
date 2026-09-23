#!/usr/bin/env python3
"""Verify that a release container index identifies the accepted platform images."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
EXPECTED_PLATFORMS = {"linux/amd64", "linux/arm64"}
MAX_MANIFEST_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContainerIndexError(ValueError):
    """The container index does not identify the accepted platform images."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContainerIndexError(message)


def _expected_digest(value: str, platform: str) -> str:
    _require(
        _SHA256.fullmatch(value) is not None,
        f"expected digest for {platform} is not a valid SHA-256 digest",
    )
    return value


def verify_container_index(
    index: Any,
    *,
    amd64_digest: str,
    arm64_digest: str,
) -> None:
    """Validate one exact two-platform OCI or Docker image index."""

    expected = {
        "linux/amd64": _expected_digest(amd64_digest, "linux/amd64"),
        "linux/arm64": _expected_digest(arm64_digest, "linux/arm64"),
    }
    _require(isinstance(index, dict), "container index must be a JSON object")
    _require(index.get("schemaVersion") == 2, "container index schemaVersion must be 2")
    _require(
        index.get("mediaType") in INDEX_MEDIA_TYPES,
        "container index has an unsupported mediaType",
    )

    manifests = index.get("manifests")
    _require(
        isinstance(manifests, list) and len(manifests) == 2,
        "container index must contain exactly two image descriptors",
    )

    found: set[str] = set()
    for descriptor in manifests:
        _require(isinstance(descriptor, dict), "container index descriptor must be an object")
        _require(
            descriptor.get("mediaType") in MANIFEST_MEDIA_TYPES,
            "container index descriptor has an unsupported manifest mediaType",
        )
        digest = descriptor.get("digest")
        _require(
            isinstance(digest, str) and _SHA256.fullmatch(digest) is not None,
            "container index descriptor has an invalid SHA-256 digest",
        )
        size = descriptor.get("size")
        _require(
            isinstance(size, int) and not isinstance(size, bool) and size > 0,
            "container index descriptor size must be a positive integer",
        )
        platform = descriptor.get("platform")
        _require(isinstance(platform, dict), "container index descriptor platform is missing")
        os_name = platform.get("os")
        architecture = platform.get("architecture")
        _require(
            isinstance(os_name, str) and isinstance(architecture, str),
            "container index descriptor platform is invalid",
        )
        platform_name = f"{os_name}/{architecture}"
        _require(
            platform_name in EXPECTED_PLATFORMS,
            f"container index contains an unexpected platform: {platform_name}",
        )
        _require(
            platform_name not in found,
            f"container index repeats platform: {platform_name}",
        )
        _require(
            digest == expected[platform_name],
            f"container index digest mismatch for {platform_name}",
        )
        found.add(platform_name)

    _require(found == EXPECTED_PLATFORMS, "container index is missing a required platform")


def verify_manifest_file(
    path: Path,
    *,
    amd64_digest: str,
    arm64_digest: str,
) -> None:
    """Load and validate an index JSON file within a conservative size bound."""

    try:
        if path.is_symlink() or not path.is_file():
            raise ContainerIndexError("container index is not a regular file")
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ContainerIndexError("container index exceeds the size limit")
        index = json.loads(path.read_text(encoding="utf-8"))  # NOSONAR - local CI input
    except ContainerIndexError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContainerIndexError("container index is not valid JSON") from exc
    verify_container_index(
        index,
        amd64_digest=amd64_digest,
        arm64_digest=arm64_digest,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an exact linux/amd64 and linux/arm64 container index."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--amd64-digest", required=True)
    parser.add_argument("--arm64-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verify_manifest_file(
            args.manifest,
            amd64_digest=args.amd64_digest,
            arm64_digest=args.arm64_digest,
        )
    except ContainerIndexError as exc:
        print(f"container index verification failed: {exc}", file=sys.stderr)
        return 1
    print("Verified container index for linux/amd64 and linux/arm64.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
