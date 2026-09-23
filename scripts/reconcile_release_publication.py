#!/usr/bin/env python3
"""Reconcile a stable GitHub/GHCR release without replacing immutable artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if __package__:
    from scripts.verify_container_index import verify_container_index
else:  # pragma: no cover - exercised by the workflow CLI
    from verify_container_index import verify_container_index

MAX_JSON_BYTES = 1024 * 1024
MAX_NOTES_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 4 * 1024**3
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PACKAGE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")

IMAGE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
IMAGE_CONFIG_MEDIA_TYPES = {
    "application/vnd.docker.container.image.v1+json",
    "application/vnd.oci.image.config.v1+json",
}


class PublicationError(ValueError):
    """Publication state is ambiguous or conflicts with the accepted release."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicationError(message)


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _json_file(path: Path) -> Any:
    try:
        _require(not path.is_symlink() and path.is_file(), f"not a regular file: {path}")
        _require(path.stat().st_size <= MAX_JSON_BYTES, f"JSON file is too large: {path}")
        return json.loads(path.read_text(encoding="utf-8"))  # NOSONAR - local release input
    except PublicationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid JSON file: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(  # NOSONAR - local release state selected by operator
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _stable_version(tag: str) -> tuple[int, int, int]:
    match = _STABLE_TAG.fullmatch(tag)
    _require(match is not None, f"stable tag is not canonical vMAJOR.MINOR.PATCH: {tag}")
    return tuple(int(part) for part in match.groups())


def _repository_parts(repository: str) -> tuple[str, str]:
    _require(_REPOSITORY.fullmatch(repository) is not None, "invalid GitHub repository name")
    owner, name = repository.split("/", 1)
    return owner, name


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class GitHubClient:
    """Small fail-closed GitHub REST client used only by the release job."""

    def __init__(self, token: str, *, api_url: str = "https://api.github.com") -> None:
        _require(bool(token), "GITHUB_TOKEN is required")
        parsed = urllib.parse.urlparse(api_url)
        _require(
            parsed.scheme == "https"
            and bool(parsed.netloc)
            and not parsed.query
            and not parsed.fragment,
            "GitHub API URL must be an HTTPS origin",
        )
        self._token = token
        self._api_url = api_url.rstrip("/")

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected_status: int = 200,
    ) -> Any:
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - constructor URL is HTTPS-validated
            f"{self._api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "opsgraph-release-reconciler",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                _require(
                    response.status == expected_status,
                    f"GitHub API returned unexpected HTTP {response.status} for {method} {path}",
                )
                body = response.read(MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise PublicationError(
                f"GitHub API returned HTTP {exc.code} for {method} {path}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise PublicationError(f"GitHub API request failed for {method} {path}") from exc
        _require(len(body) <= MAX_JSON_BYTES, f"GitHub API response is too large: {path}")
        try:
            return json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PublicationError(f"GitHub API returned invalid JSON for {method} {path}") from exc

    def paginated(self, path: str) -> list[Any]:
        values: list[Any] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 1001):
            result = self.request_json("GET", f"{path}{separator}per_page=100&page={page}")
            _require(isinstance(result, list), f"GitHub API list response is invalid: {path}")
            values.extend(result)
            if len(result) < 100:
                return values
        raise PublicationError(f"GitHub API pagination limit exceeded: {path}")


def _release_path(repository: str) -> str:
    owner, name = _repository_parts(repository)
    return f"/repos/{_quote(owner)}/{_quote(name)}/releases"


def list_releases(client: GitHubClient, repository: str) -> list[dict[str, Any]]:
    values = client.paginated(_release_path(repository))
    _require(all(isinstance(value, dict) for value in values), "release list is invalid")
    return values


def find_release(releases: Sequence[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    matches = [release for release in releases if release.get("tag_name") == tag]
    _require(len(matches) <= 1, f"multiple GitHub releases use tag {tag}")
    return matches[0] if matches else None


def _validate_release(release: dict[str, Any], *, tag: str, title: str, notes: str) -> None:
    _require(release.get("tag_name") == tag, "GitHub release tag does not match")
    _require(release.get("name") == title, "GitHub release title does not match")
    _require(release.get("body") == notes, "GitHub release notes do not match")
    _require(release.get("prerelease") is False, "GitHub release is marked as a prerelease")
    _require(isinstance(release.get("draft"), bool), "GitHub release draft state is invalid")
    _require(
        isinstance(release.get("id"), int) and not isinstance(release.get("id"), bool),
        "GitHub release ID is invalid",
    )


def ensure_recovery_release(
    client: GitHubClient,
    *,
    repository: str,
    tag: str,
    source_commit: str,
    title: str,
    notes: str,
) -> dict[str, Any]:
    """Create or validate the draft recovery point before registry mutation."""

    _stable_version(tag)
    _require(_SOURCE_COMMIT.fullmatch(source_commit) is not None, "invalid source commit")
    releases = list_releases(client, repository)
    release = find_release(releases, tag)
    created = release is None
    if release is None:
        release = client.request_json(
            "POST",
            _release_path(repository),
            payload={
                "tag_name": tag,
                "target_commitish": source_commit,
                "name": title,
                "body": notes,
                "draft": True,
                "prerelease": False,
                "make_latest": "false",
            },
            expected_status=201,
        )
        _require(isinstance(release, dict), "created GitHub release is invalid")
    _validate_release(release, tag=tag, title=title, notes=notes)
    return {
        "schema_version": 1,
        "repository": repository,
        "tag": tag,
        "release_id": release["id"],
        "state": "draft" if release["draft"] else "published",
        "created": created,
    }


def alias_plan(releases: Sequence[dict[str, Any]], *, repository: str, tag: str) -> dict[str, Any]:
    """Choose only aliases that cannot move backward from a published stable release."""

    current = _stable_version(tag)
    published: set[tuple[int, int, int]] = set()
    for release in releases:
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        release_tag = release.get("tag_name")
        if not isinstance(release_tag, str) or _STABLE_TAG.fullmatch(release_tag) is None:
            continue
        published.add(_stable_version(release_tag))

    same_minor = [value for value in published if value[:2] == current[:2]]
    same_major = [value for value in published if value[0] == current[0]]
    move_minor = not same_minor or current >= max(same_minor)
    move_major = not same_major or current >= max(same_major)
    move_latest = not published or current >= max(published)
    aliases: list[str] = []
    if move_minor:
        aliases.append(f"{current[0]}.{current[1]}")
    if move_major:
        aliases.append(str(current[0]))
    if move_latest:
        aliases.append("latest")
    return {
        "schema_version": 1,
        "repository": repository,
        "tag": tag,
        "version": ".".join(str(value) for value in current),
        "container_aliases": aliases,
        "github_latest": move_latest,
    }


def container_tag_state(
    client: GitHubClient, *, owner: str, package: str, tag: str
) -> dict[str, Any]:
    """Return exact GHCR tag state from a complete authenticated package listing."""

    _require(_PACKAGE.fullmatch(owner) is not None, "invalid package owner")
    _require(_PACKAGE.fullmatch(package) is not None, "invalid package name")
    _require(_PACKAGE.fullmatch(tag) is not None, "invalid container tag")
    path = f"/users/{_quote(owner)}/packages/container/{_quote(package)}/versions"
    versions = client.paginated(path)
    found: list[str] = []
    for version in versions:
        _require(isinstance(version, dict), "container package version is invalid")
        digest = version.get("name")
        metadata = version.get("metadata")
        container = metadata.get("container") if isinstance(metadata, dict) else None
        tags = container.get("tags") if isinstance(container, dict) else None
        _require(isinstance(tags, list), "container package tags are invalid")
        if tag in tags:
            _require(
                isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None,
                f"container tag {tag} has an invalid registry digest",
            )
            found.append(digest)
    _require(len(found) <= 1, f"container tag {tag} resolves to multiple package versions")
    return {
        "schema_version": 1,
        "tag": tag,
        "state": "existing" if found else "absent",
        "digest": found[0] if found else None,
    }


def _manifest_bytes(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        _require(not path.is_symlink() and path.is_file(), "container manifest is not a file")
        _require(path.stat().st_size <= MAX_JSON_BYTES, "container manifest is too large")
        value = path.read_bytes()  # NOSONAR - validated local manifest
        manifest = json.loads(value)
    except PublicationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError("container manifest is not valid JSON") from exc
    _require(isinstance(manifest, dict), "container manifest must be a JSON object")
    return value, manifest


def verify_platform_manifest(
    path: Path, *, image_id: str, existing_digest: str | None = None
) -> str:
    """Bind one remote platform manifest to the accepted Docker image config."""

    _require(_DIGEST.fullmatch(image_id) is not None, "accepted image ID is invalid")
    raw, manifest = _manifest_bytes(path)
    _require(manifest.get("schemaVersion") == 2, "platform manifest schemaVersion must be 2")
    _require(
        manifest.get("mediaType") in IMAGE_MANIFEST_MEDIA_TYPES,
        "platform tag is not a supported image manifest",
    )
    _require("manifests" not in manifest, "platform tag unexpectedly identifies an image index")
    config = manifest.get("config")
    _require(isinstance(config, dict), "platform manifest config is missing")
    _require(config.get("digest") == image_id, "platform manifest config changed")
    _require(
        config.get("mediaType") in IMAGE_CONFIG_MEDIA_TYPES,
        "platform manifest config mediaType is unsupported",
    )
    _require(
        isinstance(config.get("size"), int)
        and not isinstance(config.get("size"), bool)
        and config["size"] > 0,
        "platform manifest config size is invalid",
    )
    layers = manifest.get("layers")
    _require(isinstance(layers, list) and bool(layers), "platform manifest layers are missing")
    layer_digests: set[str] = set()
    for layer in layers:
        _require(isinstance(layer, dict), "platform manifest layer is invalid")
        digest = layer.get("digest")
        _require(
            isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None,
            "platform manifest layer digest is invalid",
        )
        _require(digest not in layer_digests, "platform manifest repeats a layer")
        layer_digests.add(digest)
        _require(
            isinstance(layer.get("size"), int)
            and not isinstance(layer.get("size"), bool)
            and layer["size"] > 0,
            "platform manifest layer size is invalid",
        )
        _require(isinstance(layer.get("mediaType"), str), "platform layer mediaType is invalid")
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if existing_digest is not None:
        _require(_DIGEST.fullmatch(existing_digest) is not None, "existing tag digest is invalid")
        _require(digest == existing_digest, "package metadata and registry manifest disagree")
    return digest


def verify_index_manifest(
    path: Path,
    *,
    amd64_digest: str,
    arm64_digest: str,
    existing_digest: str | None = None,
) -> str:
    """Bind one remote index and optional existing tag to the accepted platform manifests."""

    raw, manifest = _manifest_bytes(path)
    verify_container_index(manifest, amd64_digest=amd64_digest, arm64_digest=arm64_digest)
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if existing_digest is not None:
        _require(_DIGEST.fullmatch(existing_digest) is not None, "existing tag digest is invalid")
        _require(digest == existing_digest, "package metadata and registry index disagree")
    return digest


def local_assets(directory: Path) -> dict[str, dict[str, Any]]:
    _require(
        not directory.is_symlink() and directory.is_dir(), "release asset directory is invalid"
    )
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        _require(not path.is_symlink() and path.is_file(), f"unexpected release asset: {path.name}")
        _require(
            _ASSET_NAME.fullmatch(path.name) is not None, f"unsafe release asset name: {path.name}"
        )
        size = path.stat().st_size
        _require(size <= MAX_ASSET_BYTES, f"release asset is too large: {path.name}")
        records[path.name] = {"path": path, "size": size, "digest": f"sha256:{_sha256(path)}"}
    _require(bool(records), "release asset directory is empty")
    return records


def release_assets(
    client: GitHubClient, *, repository: str, release_id: int
) -> list[dict[str, Any]]:
    _require(release_id > 0, "invalid GitHub release ID")
    path = f"{_release_path(repository)}/{release_id}/assets"
    values = client.paginated(path)
    _require(all(isinstance(value, dict) for value in values), "release asset list is invalid")
    return values


def compare_assets(
    expected: dict[str, dict[str, Any]],
    actual: Sequence[dict[str, Any]],
    *,
    allow_missing: bool,
) -> list[str]:
    remote: dict[str, dict[str, Any]] = {}
    for asset in actual:
        name = asset.get("name")
        _require(isinstance(name, str), "GitHub release asset name is invalid")
        _require(name not in remote, f"duplicate GitHub release asset: {name}")
        remote[name] = asset
    unexpected = sorted(set(remote) - set(expected))
    _require(not unexpected, f"unexpected GitHub release assets: {', '.join(unexpected)}")
    for name, asset in remote.items():
        record = expected[name]
        _require(asset.get("state") == "uploaded", f"GitHub release asset is not uploaded: {name}")
        _require(asset.get("size") == record["size"], f"GitHub release asset size changed: {name}")
        _require(
            asset.get("digest") == record["digest"],
            f"GitHub release asset digest changed or is unavailable: {name}",
        )
    missing = sorted(set(expected) - set(remote))
    _require(
        allow_missing or not missing, f"GitHub release assets are missing: {', '.join(missing)}"
    )
    return missing


def _upload_asset(repository: str, tag: str, path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        ["gh", "release", "upload", tag, str(path), "--repo", repository],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PublicationError(f"GitHub release asset upload failed: {path.name}")


def reconcile_assets(
    client: GitHubClient,
    *,
    repository: str,
    tag: str,
    directory: Path,
    uploader: Callable[[str, str, Path], None] = _upload_asset,
) -> None:
    release = find_release(list_releases(client, repository), tag)
    _require(release is not None, f"GitHub recovery release is missing: {tag}")
    release_id = release.get("id")
    _require(isinstance(release_id, int), "GitHub release ID is invalid")
    expected = local_assets(directory)
    missing = compare_assets(
        expected,
        release_assets(client, repository=repository, release_id=release_id),
        allow_missing=True,
    )
    for name in missing:
        uploader(repository, tag, expected[name]["path"])
    compare_assets(
        expected,
        release_assets(client, repository=repository, release_id=release_id),
        allow_missing=False,
    )


def publish_release(
    client: GitHubClient,
    *,
    repository: str,
    tag: str,
    title: str,
    notes: str,
    directory: Path,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Publish a fully reconciled draft, or accept an exact published retry."""

    releases = list_releases(client, repository)
    release = find_release(releases, tag)
    _require(release is not None, f"GitHub recovery release is missing: {tag}")
    _validate_release(release, tag=tag, title=title, notes=notes)
    live_plan = alias_plan(releases, repository=repository, tag=tag)
    _require(plan == live_plan, "published release set changed after alias planning")
    release_id = release["id"]
    expected = local_assets(directory)
    compare_assets(
        expected,
        release_assets(client, repository=repository, release_id=release_id),
        allow_missing=False,
    )
    latest: dict[str, Any] | None = None
    if release["draft"] is False:
        latest_value = client.request_json("GET", f"{_release_path(repository)}/latest")
        _require(isinstance(latest_value, dict), "latest GitHub release is invalid")
        latest = latest_value
    latest_is_current = latest is not None and latest.get("tag_name") == tag
    latest_matches = (
        latest_is_current if plan["github_latest"] else latest is not None and not latest_is_current
    )
    if release["draft"] is False and latest_matches:
        updated = release
    else:
        updated = client.request_json(
            "PATCH",
            f"{_release_path(repository)}/{release_id}",
            payload={
                "name": title,
                "body": notes,
                "draft": False,
                "prerelease": False,
                "make_latest": "true" if plan["github_latest"] else "false",
            },
        )
        _require(isinstance(updated, dict), "published GitHub release is invalid")
    _validate_release(updated, tag=tag, title=title, notes=notes)
    _require(updated.get("draft") is False, "GitHub release remained a draft")
    latest = client.request_json("GET", f"{_release_path(repository)}/latest")
    _require(isinstance(latest, dict), "latest GitHub release is invalid")
    latest_is_current = latest.get("tag_name") == tag
    _require(
        latest_is_current == plan["github_latest"],
        "GitHub latest release state does not match the monotonic plan",
    )
    return {
        "schema_version": 1,
        "repository": repository,
        "tag": tag,
        "release_id": release_id,
        "state": "published",
        "github_latest": plan["github_latest"],
    }


def _notes(path: Path) -> str:
    try:
        _require(not path.is_symlink() and path.is_file(), "release notes are not a file")
        _require(path.stat().st_size <= MAX_NOTES_BYTES, "release notes are too large")
        return path.read_text(encoding="utf-8")  # NOSONAR - validated local release notes
    except PublicationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise PublicationError("release notes could not be read") from exc


def _client() -> GitHubClient:
    return GitHubClient(
        os.environ.get("GITHUB_TOKEN", ""),
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-release")
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--source-commit", required=True)
    prepare.add_argument("--title", required=True)
    prepare.add_argument("--notes-file", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    aliases = subparsers.add_parser("plan-aliases")
    aliases.add_argument("--repository", required=True)
    aliases.add_argument("--tag", required=True)
    aliases.add_argument("--output", type=Path, required=True)

    tag_state = subparsers.add_parser("container-tag-state")
    tag_state.add_argument("--owner", required=True)
    tag_state.add_argument("--package", required=True)
    tag_state.add_argument("--tag", required=True)
    tag_state.add_argument("--output", type=Path, required=True)

    platform = subparsers.add_parser("verify-platform-manifest")
    platform.add_argument("--manifest", type=Path, required=True)
    platform.add_argument("--image-id", required=True)
    platform.add_argument("--existing-digest")
    platform.add_argument("--output", type=Path, required=True)

    index = subparsers.add_parser("verify-index-manifest")
    index.add_argument("--manifest", type=Path, required=True)
    index.add_argument("--amd64-digest", required=True)
    index.add_argument("--arm64-digest", required=True)
    index.add_argument("--existing-digest")
    index.add_argument("--output", type=Path, required=True)

    assets = subparsers.add_parser("reconcile-assets")
    assets.add_argument("--repository", required=True)
    assets.add_argument("--tag", required=True)
    assets.add_argument("--directory", type=Path, required=True)

    publish = subparsers.add_parser("publish-release")
    publish.add_argument("--repository", required=True)
    publish.add_argument("--tag", required=True)
    publish.add_argument("--title", required=True)
    publish.add_argument("--notes-file", type=Path, required=True)
    publish.add_argument("--directory", type=Path, required=True)
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-release":
            result = ensure_recovery_release(
                _client(),
                repository=args.repository,
                tag=args.tag,
                source_commit=args.source_commit,
                title=args.title,
                notes=_notes(args.notes_file),
            )
            _write_json(args.output, result)
        elif args.command == "plan-aliases":
            client = _client()
            result = alias_plan(
                list_releases(client, args.repository),
                repository=args.repository,
                tag=args.tag,
            )
            _write_json(args.output, result)
        elif args.command == "container-tag-state":
            result = container_tag_state(
                _client(), owner=args.owner, package=args.package, tag=args.tag
            )
            _write_json(args.output, result)
        elif args.command == "verify-platform-manifest":
            digest = verify_platform_manifest(
                args.manifest,
                image_id=args.image_id,
                existing_digest=args.existing_digest,
            )
            _write_json(args.output, {"schema_version": 1, "digest": digest})
        elif args.command == "verify-index-manifest":
            digest = verify_index_manifest(
                args.manifest,
                amd64_digest=args.amd64_digest,
                arm64_digest=args.arm64_digest,
                existing_digest=args.existing_digest,
            )
            _write_json(args.output, {"schema_version": 1, "digest": digest})
        elif args.command == "reconcile-assets":
            reconcile_assets(
                _client(),
                repository=args.repository,
                tag=args.tag,
                directory=args.directory,
            )
        elif args.command == "publish-release":
            plan = _json_file(args.plan)
            _require(isinstance(plan, dict), "publication plan must be an object")
            result = publish_release(
                _client(),
                repository=args.repository,
                tag=args.tag,
                title=args.title,
                notes=_notes(args.notes_file),
                directory=args.directory,
                plan=plan,
            )
            _write_json(args.output, result)
        else:  # pragma: no cover - argparse enforces the command set
            raise PublicationError("unknown publication command")
    except PublicationError as exc:
        print(f"release publication failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
