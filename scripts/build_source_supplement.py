"""Build a verified OpsGraph stable-release third-party source supplement."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import stat
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RELEASE = "1.0.0"
ARCHIVE_NAME = f"opsgraph-{RELEASE}-third-party-sources.tar.gz"
ARCHIVE_ROOT = f"opsgraph-{RELEASE}-third-party-sources"
MANIFEST_NAME = f"source-manifest-{RELEASE}.json"
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_SOURCE_BYTES = 128 * 1024**2
MAX_TOTAL_SOURCE_BYTES = 512 * 1024**2
MAX_SOURCES = 256
MAX_NOTICES = 128
CHUNK_SIZE = 128 * 1024
REQUIRED_EXPLICIT_MAPPINGS = frozenset({"orjson", "pglast", "psycopg", "psycopg-binary"})
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
SHA256 = re.compile(r"[0-9a-f]{64}")
LOCK_REQUIREMENT = re.compile(r"(?m)^([A-Za-z0-9_.-]+)==([^\s;\\]+)")
STABLE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class SupplementError(ValueError):
    """A release supplement input failed closed validation."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:  # NOSONAR - trusted local release input
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SupplementError("duplicate JSON key in source manifest")
        result[key] = value
    return result


def _regular_bytes(path: Path, limit: int, label: str) -> bytes:
    if path.is_symlink():
        raise SupplementError(f"{label} must not be a symbolic link")
    try:
        metadata = path.stat()
    except FileNotFoundError:
        raise SupplementError(f"{label} does not exist") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise SupplementError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise SupplementError(f"{label} must not be hard-linked")
    if metadata.st_size > limit:
        raise SupplementError(f"{label} exceeds the size limit")
    data = path.read_bytes()  # NOSONAR - trusted local release input
    if len(data) != metadata.st_size:
        raise SupplementError(f"{label} changed while it was read")
    return data


def _safe_filename(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_NAME.fullmatch(value) is None:
        raise SupplementError(f"{label} has an unsafe file name")
    windows_stem = value.casefold().split(".", 1)[0]
    windows_devices = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    if (
        value in {".", "..", "SHA256SUMS"}
        or re.fullmatch(r"source-manifest-[0-9]+\.[0-9]+\.[0-9]+\.json", value) is not None
        or value.endswith(".")
        or windows_stem in windows_devices
    ):
        raise SupplementError(f"{label} uses a reserved file name")
    return value


def _safe_https_url(value: object) -> str:
    if not isinstance(value, str):
        raise SupplementError("source URL must be a string")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SupplementError("source URL must be credential-free HTTPS without a fragment")
    return value


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Permit credential-free HTTPS redirects without forwarding request headers."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del file_pointer, message, headers
        if code not in {301, 302, 303, 307, 308}:
            raise urllib.error.HTTPError(request.full_url, code, "unsupported redirect", {}, None)
        resolved = urllib.parse.urljoin(request.full_url, new_url)
        _safe_https_url(request.full_url)
        _safe_https_url(resolved)
        if request.get_method() not in {"GET", "HEAD"}:
            raise urllib.error.HTTPError(request.full_url, code, "unsafe redirect method", {}, None)
        return urllib.request.Request(  # noqa: S310 - both URLs were restricted to HTTPS.
            resolved, method=request.get_method()
        )


def _opener():
    # Ignore ambient proxy configuration so proxy credentials and headers cannot
    # enter release downloads.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _SafeRedirect())


def _positive_int(value: object, label: str, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise SupplementError(f"{label} has an invalid size")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise SupplementError(f"{label} has an invalid SHA-256")
    return value


def _parse_lock(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SupplementError("requirements lock must be UTF-8") from None
    packages: dict[str, str] = {}
    for raw_name, version in LOCK_REQUIREMENT.findall(text):
        name = _normalise_package(raw_name)
        if name in packages:
            raise SupplementError(f"duplicate locked package: {name}")
        packages[name] = version
    if not packages:
        raise SupplementError("requirements lock contains no pinned packages")
    return packages


def _load_manifest(path: Path, release_version: str = RELEASE) -> tuple[dict, bytes]:
    data = _regular_bytes(path, MAX_MANIFEST_BYTES, "source manifest")
    try:
        manifest = json.loads(data, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError):
        raise SupplementError("source manifest is not valid bounded JSON") from None
    required = {
        "schema_version",
        "release_version",
        "recorded_on",
        "scope",
        "dependency_lock",
        "historical_source_manifest",
        "package_source_mappings",
        "sources",
        "notices",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise SupplementError("source manifest has an unsupported shape")
    if manifest["schema_version"] != 2 or manifest["release_version"] != release_version:
        raise SupplementError(f"source manifest does not describe OpsGraph {release_version}")
    if (
        not isinstance(manifest["recorded_on"], str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", manifest["recorded_on"]) is None
        or not isinstance(manifest["scope"], str)
        or not manifest["scope"].strip()
    ):
        raise SupplementError("source manifest provenance is invalid")
    return manifest, data


def _validate_manifest(
    manifest: dict, lock_path: Path, notices_directory: Path
) -> tuple[list[dict], list[tuple[str, bytes]]]:
    if notices_directory.is_symlink() or not notices_directory.is_dir():
        raise SupplementError("notice directory must be a real directory")
    lock_data = _regular_bytes(lock_path, MAX_MANIFEST_BYTES, "requirements lock")
    lock = manifest["dependency_lock"]
    if (
        not isinstance(lock, dict)
        or set(lock) != {"file", "sha256", "package_count"}
        or lock["file"] != "requirements.lock"
        or _digest(lock.get("sha256"), "requirements lock") != _sha256(lock_data)
    ):
        raise SupplementError("source manifest does not match requirements.lock")
    locked_packages = _parse_lock(lock_data)
    if lock.get("package_count") != len(locked_packages):
        raise SupplementError("source manifest package count does not match requirements.lock")

    history = manifest["historical_source_manifest"]
    if (
        not isinstance(history, dict)
        or set(history) != {"file", "sha256", "relationship"}
        or history.get("file") != "beta-source-manifest.json"
        or SHA256.fullmatch(str(history.get("sha256", ""))) is None
        or not isinstance(history.get("relationship"), str)
        or not history["relationship"].strip()
    ):
        raise SupplementError("historical source-manifest provenance is invalid")
    history_data = _regular_bytes(
        notices_directory / history["file"],
        MAX_MANIFEST_BYTES,
        "historical source manifest",
    )
    if _sha256(history_data) != history["sha256"]:
        raise SupplementError("historical source-manifest identity mismatch")

    sources = manifest["sources"]
    if not isinstance(sources, list) or not 0 < len(sources) <= MAX_SOURCES:
        raise SupplementError("source manifest has an invalid source count")
    files: dict[str, dict] = {}
    source_urls: set[str] = set()
    named_packages: dict[str, str] = {}
    total_size = 0
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SupplementError(f"source {index} is not an object")
        unnamed_keys = {"file", "source", "sha256", "size"}
        documented_unnamed_keys = unnamed_keys | {"purpose"}
        named_keys = unnamed_keys | {"name", "version"}
        if frozenset(source) not in {
            frozenset(unnamed_keys),
            frozenset(documented_unnamed_keys),
            frozenset(named_keys),
        }:
            raise SupplementError(f"source {index} has an unsupported shape")
        if "purpose" in source and (
            not isinstance(source["purpose"], str) or not source["purpose"].strip()
        ):
            raise SupplementError(f"source {index} has an invalid purpose")
        filename = _safe_filename(source.get("file"), f"source {index}")
        identity = filename.casefold()
        if identity in files:
            raise SupplementError(f"duplicate source file name: {filename}")
        source_url = _safe_https_url(source.get("source"))
        if source_url in source_urls:
            raise SupplementError(f"duplicate source URL: {source_url}")
        source_urls.add(source_url)
        _digest(source.get("sha256"), f"source {filename}")
        size = _positive_int(source.get("size"), f"source {filename}", MAX_SOURCE_BYTES)
        total_size += size
        if total_size > MAX_TOTAL_SOURCE_BYTES:
            raise SupplementError("declared source set exceeds the global size limit")
        files[identity] = source
        if "name" in source:
            if not isinstance(source["name"], str) or not isinstance(source["version"], str):
                raise SupplementError(f"source {filename} has invalid package metadata")
            name = _normalise_package(source["name"])
            if name in named_packages:
                raise SupplementError(f"duplicate named package source: {name}")
            if locked_packages.get(name) != source["version"]:
                raise SupplementError(f"source version does not match locked package: {name}")
            named_packages[name] = source["version"]

    mappings = manifest["package_source_mappings"]
    if not isinstance(mappings, dict) or set(mappings) != REQUIRED_EXPLICIT_MAPPINGS:
        raise SupplementError("required explicit package-source mappings are missing")
    mapped_packages: set[str] = set()
    mapped_files: set[str] = set()
    for raw_name, mapping in mappings.items():
        name = _normalise_package(raw_name)
        if (
            not isinstance(mapping, dict)
            or set(mapping) != {"version", "files", "rationale"}
            or locked_packages.get(name) != mapping.get("version")
            or not isinstance(mapping.get("rationale"), str)
            or not mapping["rationale"].strip()
            or not isinstance(mapping.get("files"), list)
            or not mapping["files"]
        ):
            raise SupplementError(f"invalid explicit package-source mapping: {name}")
        for mapped_file in mapping["files"]:
            filename = _safe_filename(mapped_file, f"mapping {name}")
            identity = filename.casefold()
            if identity not in files:
                raise SupplementError(f"mapping {name} references an unknown source: {filename}")
            if identity in mapped_files:
                raise SupplementError(f"source is mapped more than once: {filename}")
            mapped_files.add(identity)
        mapped_packages.add(name)
    if named_packages.keys() & mapped_packages:
        raise SupplementError("a package has both named and explicit source mappings")
    if set(locked_packages) != set(named_packages) | mapped_packages:
        missing = sorted(set(locked_packages) - set(named_packages) - mapped_packages)
        extra = sorted((set(named_packages) | mapped_packages) - set(locked_packages))
        raise SupplementError(f"locked dependency source coverage mismatch: {missing or extra}")
    unnamed_files = {key for key, value in files.items() if "name" not in value}
    if unnamed_files != mapped_files:
        raise SupplementError("supporting source files are not covered by explicit mappings")

    notices = manifest["notices"]
    if not isinstance(notices, list) or not 0 < len(notices) <= MAX_NOTICES:
        raise SupplementError("source manifest has an invalid notice count")
    notice_payload: list[tuple[str, bytes]] = []
    notice_names: set[str] = set()
    for index, notice in enumerate(notices):
        if not isinstance(notice, dict) or set(notice) != {"file", "size", "sha256"}:
            raise SupplementError(f"notice {index} has an unsupported shape")
        filename = _safe_filename(notice.get("file"), f"notice {index}")
        identity = filename.casefold()
        if identity in notice_names:
            raise SupplementError(f"duplicate notice file name: {filename}")
        notice_names.add(identity)
        size = _positive_int(notice.get("size"), f"notice {filename}", MAX_SOURCE_BYTES)
        data = _regular_bytes(notices_directory / filename, size, f"notice {filename}")
        if len(data) != size or _sha256(data) != _digest(
            notice.get("sha256"), f"notice {filename}"
        ):
            raise SupplementError(f"notice identity mismatch: {filename}")
        notice_payload.append((filename, data))
    return sources, notice_payload


def _download(source: dict, destination: Path, opener) -> None:
    url = _safe_https_url(source["source"])
    expected_size = source["size"]
    expected_digest = source["sha256"]
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - URL is HTTPS-only.
    digest = hashlib.sha256()
    written = 0
    try:
        response_context = opener.open(request, timeout=30)
        with response_context as response, destination.open("xb") as output:
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                raise SupplementError(f"source download returned HTTP {status}")
            final_url = getattr(response, "geturl", lambda: url)()
            _safe_https_url(final_url)
            content_length = response.headers.get("Content-Length") if response.headers else None
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    raise SupplementError("source response has an invalid Content-Length") from None
                if declared != expected_size:
                    raise SupplementError("source response size does not match the manifest")
            while True:
                chunk = response.read(min(CHUNK_SIZE, expected_size - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size or written > MAX_SOURCE_BYTES:
                    raise SupplementError("source download exceeds the declared size")
                digest.update(chunk)
                output.write(chunk)
    except (OSError, urllib.error.URLError) as error:
        raise SupplementError(f"source download failed: {error.__class__.__name__}") from None
    if written != expected_size or digest.hexdigest() != expected_digest:
        raise SupplementError("source download identity does not match the manifest")


def _tar_info(name: str, size: int, archive_root: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(f"{archive_root}/{name}")
    info.size = size
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def _write_archive(output: Path, payload: dict[str, bytes | Path], archive_root: str) -> None:
    created = False
    try:
        with output.open("xb") as raw:  # NOSONAR - operator-selected release output
            created = True
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
            ) as zipped:
                with tarfile.open(
                    fileobj=zipped, mode="w|", format=tarfile.USTAR_FORMAT
                ) as archive:
                    for name, value in sorted(payload.items()):
                        size = value.stat().st_size if isinstance(value, Path) else len(value)
                        info = _tar_info(name, size, archive_root)
                        if isinstance(value, Path):
                            with value.open("rb") as stream:  # NOSONAR - validated cache file
                                archive.addfile(info, stream)  # NOSONAR - fixed archive member
                        else:
                            archive.addfile(info, io.BytesIO(value))  # NOSONAR - fixed member
    except BaseException:
        if created:
            try:
                output.unlink()  # NOSONAR - same operator-selected output
            except FileNotFoundError:
                pass
        raise


def build(
    manifest_path: Path,
    output: Path,
    *,
    requirements_lock: Path,
    notices_directory: Path,
    opener=None,
    release_version: str = RELEASE,
) -> str:
    if STABLE_VERSION.fullmatch(release_version) is None:
        raise SupplementError("release version must be stable MAJOR.MINOR.PATCH")
    manifest_name = f"source-manifest-{release_version}.json"
    archive_root = f"opsgraph-{release_version}-third-party-sources"
    archive_name = f"{archive_root}.tar.gz"
    if manifest_path.name != manifest_name:
        raise SupplementError(f"manifest file must be named {manifest_name}")
    if output.name != archive_name:
        raise SupplementError(f"output file must be named {archive_name}")
    if output.exists() or output.is_symlink():
        raise SupplementError("output already exists")
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise SupplementError("output parent must be a real directory")
    manifest, manifest_bytes = _load_manifest(manifest_path, release_version)
    sources, notices = _validate_manifest(manifest, requirements_lock, notices_directory)
    download_opener = opener or _opener()
    payload: dict[str, bytes | Path] = {
        manifest_name: manifest_bytes,
        **{f"notices/{name}": data for name, data in notices},
    }
    with tempfile.TemporaryDirectory(prefix="opsgraph-source-supplement-") as temporary:
        temporary_root = Path(temporary)
        for index, source in enumerate(sources):
            path = temporary_root / f"{index:03d}"
            _download(source, path, download_opener)
            payload[f"sources/{source['file']}"] = path
        sums = []
        for name, value in sorted(payload.items()):
            data_hash = _file_sha256(value) if isinstance(value, Path) else _sha256(value)
            sums.append(f"{data_hash}  {name}\n")
        payload["SHA256SUMS"] = "".join(sums).encode("ascii")
        _write_archive(output, payload, archive_root)
    result = _file_sha256(output)
    print(f"Created {output.name}\nSHA-256: {result}")
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--requirements-lock", type=Path, default=root / "requirements.lock")
    parser.add_argument(
        "--notices-directory", type=Path, default=root / "docs" / "release" / "notices"
    )
    args = parser.parse_args()
    try:
        build(
            args.manifest,
            args.output,
            requirements_lock=args.requirements_lock,
            notices_directory=args.notices_directory,
            release_version=args.release_version,
        )
    except (SupplementError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"Source supplement: {error}\n")


if __name__ == "__main__":
    main()
