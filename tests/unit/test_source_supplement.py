"""Stable source-supplement assembly tests; no network calls are made."""

import hashlib
import importlib.util
import io
import json
import tarfile
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_source_supplement.py"
REPO_ROOT = SCRIPT.parents[1]
SPEC = importlib.util.spec_from_file_location("source_supplement_tests", SCRIPT)
supplement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supplement)


class Response(io.BytesIO):
    status = 200

    def __init__(self, content, url, *, content_length=True):
        super().__init__(content)
        self._url = url
        self.headers = {"Content-Length": str(len(content))} if content_length else {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Opener:
    def __init__(self, payloads):
        self.payloads = payloads
        self.requests = []

    def open(self, request, *, timeout):
        assert timeout == 30
        assert request.header_items() == []
        self.requests.append(request)
        return Response(self.payloads[request.full_url], request.full_url)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def candidate(tmp_path):
    notices = tmp_path / "notices"
    notices.mkdir()
    historical = b'{"historical":"verified"}\n'
    notice = b"fixture notice\n"
    (notices / "beta-source-manifest.json").write_bytes(historical)
    (notices / "NOTICE.txt").write_bytes(notice)

    payloads = {
        "https://sources.example/orjson.tar.gz": b"orjson-source",
        "https://sources.example/pglast.tar.gz": b"pglast-source",
        "https://sources.example/psycopg.tar.gz": b"psycopg-source",
        "https://sources.example/psycopg-binary.tar.gz": b"psycopg-binary-source",
    }
    lock = "".join(
        f"{name}=={version} \\\n+    --hash=sha256:{'a' * 64}\n"
        for name, version in (
            ("orjson", "3.12.0"),
            ("pglast", "7.18"),
            ("psycopg", "3.3.5"),
            ("psycopg-binary", "3.3.5"),
        )
    ).encode()
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_bytes(lock)

    sources = []
    mappings = {}
    for name, version, filename, url in (
        ("orjson", "3.12.0", "orjson.tar.gz", "https://sources.example/orjson.tar.gz"),
        ("pglast", "7.18", "pglast.tar.gz", "https://sources.example/pglast.tar.gz"),
        ("psycopg", "3.3.5", "psycopg.tar.gz", "https://sources.example/psycopg.tar.gz"),
        (
            "psycopg-binary",
            "3.3.5",
            "psycopg-binary.tar.gz",
            "https://sources.example/psycopg-binary.tar.gz",
        ),
    ):
        data = payloads[url]
        sources.append(
            {
                "file": filename,
                "source": url,
                "sha256": digest(data),
                "size": len(data),
            }
        )
        mappings[name] = {
            "version": version,
            "files": [filename],
            "rationale": "fixture explicit mapping",
        }
    manifest = {
        "schema_version": 2,
        "release_version": "1.0.0",
        "recorded_on": "2026-09-22",
        "scope": "test stable dependency source set",
        "dependency_lock": {
            "file": "requirements.lock",
            "sha256": digest(lock),
            "package_count": 4,
        },
        "historical_source_manifest": {
            "file": "beta-source-manifest.json",
            "sha256": digest(historical),
            "relationship": "fixture historical identity",
        },
        "package_source_mappings": mappings,
        "sources": sources,
        "notices": [{"file": "NOTICE.txt", "size": len(notice), "sha256": digest(notice)}],
    }
    manifest_path = tmp_path / supplement.MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return tmp_path, manifest_path, lock_path, notices, manifest, payloads


def build(candidate, directory, *, opener=None):
    _, manifest, lock, notices, _, payloads = candidate
    directory.mkdir()
    output = directory / supplement.ARCHIVE_NAME
    supplement.build(
        manifest,
        output,
        requirements_lock=lock,
        notices_directory=notices,
        opener=opener or Opener(payloads),
    )
    return output


def test_build_is_deterministic_complete_and_header_free(candidate, tmp_path):
    opener = Opener(candidate[-1])
    first = build(candidate, tmp_path / "first", opener=opener)
    second = build(candidate, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    assert len(opener.requests) == 4

    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.isfile() for member in members)
        assert all(member.mtime == 0 and member.uid == 0 and member.gid == 0 for member in members)
        prefix = supplement.ARCHIVE_ROOT + "/"
        payload = {
            member.name.removeprefix(prefix): archive.extractfile(member).read()
            for member in members
        }
    assert supplement.MANIFEST_NAME in payload
    assert "notices/NOTICE.txt" in payload
    assert {name for name in payload if name.startswith("sources/")} == {
        "sources/orjson.tar.gz",
        "sources/pglast.tar.gz",
        "sources/psycopg.tar.gz",
        "sources/psycopg-binary.tar.gz",
    }
    expected_sums = "".join(
        f"{digest(data)}  {name}\n"
        for name, data in sorted(payload.items())
        if name != "SHA256SUMS"
    ).encode()
    assert payload["SHA256SUMS"] == expected_sums


def test_build_supports_a_future_stable_version(candidate, tmp_path):
    version = "1.2.3"
    manifest = candidate[4]
    manifest["release_version"] = version
    manifest_path = candidate[0] / f"source-manifest-{version}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_dir = tmp_path / "future"
    output_dir.mkdir()
    output = output_dir / f"opsgraph-{version}-third-party-sources.tar.gz"

    supplement.build(
        manifest_path,
        output,
        requirements_lock=candidate[2],
        notices_directory=candidate[3],
        opener=Opener(candidate[-1]),
        release_version=version,
    )

    with tarfile.open(output, "r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    assert f"opsgraph-{version}-third-party-sources/source-manifest-{version}.json" in names


@pytest.mark.parametrize(
    "url",
    [
        "http://sources.example/source.tar.gz",
        "file:///tmp/source.tar.gz",
        "https://user:secret@sources.example/source.tar.gz",
        "https://sources.example/source.tar.gz#fragment",
    ],
)
def test_manifest_rejects_non_https_credentials_and_fragments(candidate, tmp_path, url):
    *_, manifest, _ = candidate
    manifest["sources"][0]["source"] = url
    candidate[1].write_text(json.dumps(manifest))
    with pytest.raises(supplement.SupplementError, match="credential-free HTTPS"):
        build(candidate, tmp_path / "bad-url")


def test_redirects_forbid_scheme_changes_and_drop_all_headers():
    handler = supplement._SafeRedirect()
    request = urllib.request.Request(
        "https://sources.example/original", headers={"Authorization": "secret"}
    )
    redirected = handler.redirect_request(
        request, None, 302, "moved", {}, "https://cdn.example/archive"
    )
    assert redirected.full_url == "https://cdn.example/archive"
    assert redirected.header_items() == []
    with pytest.raises(supplement.SupplementError, match="credential-free HTTPS"):
        handler.redirect_request(request, None, 302, "moved", {}, "http://cdn.example/archive")


def test_download_rejects_wrong_size_or_digest_without_output(candidate, tmp_path):
    payloads = dict(candidate[-1])
    first_url = candidate[4]["sources"][0]["source"]
    payloads[first_url] += b"unexpected"
    output_dir = tmp_path / "wrong-download"
    with pytest.raises(supplement.SupplementError, match="size"):
        build(candidate, output_dir, opener=Opener(payloads))
    assert not (output_dir / supplement.ARCHIVE_NAME).exists()

    payloads = dict(candidate[-1])
    payloads[first_url] = payloads[first_url][::-1]
    digest_dir = tmp_path / "wrong-digest"
    with pytest.raises(supplement.SupplementError, match="identity"):
        build(candidate, digest_dir, opener=Opener(payloads))
    assert not (digest_dir / supplement.ARCHIVE_NAME).exists()


def test_manifest_requires_complete_locked_and_explicit_source_coverage(candidate, tmp_path):
    manifest = candidate[4]
    del manifest["package_source_mappings"]["pglast"]
    candidate[1].write_text(json.dumps(manifest))
    with pytest.raises(supplement.SupplementError, match="explicit package-source"):
        build(candidate, tmp_path / "missing-mapping")


def test_manifest_rejects_duplicate_names_and_global_bound(candidate, tmp_path, monkeypatch):
    manifest = candidate[4]
    duplicate = dict(manifest["sources"][0])
    duplicate["file"] = manifest["sources"][0]["file"].upper()
    manifest["sources"].append(duplicate)
    candidate[1].write_text(json.dumps(manifest))
    with pytest.raises(supplement.SupplementError, match="duplicate source"):
        build(candidate, tmp_path / "duplicate")

    manifest["sources"].pop()
    candidate[1].write_text(json.dumps(manifest))
    monkeypatch.setattr(supplement, "MAX_TOTAL_SOURCE_BYTES", 1)
    with pytest.raises(supplement.SupplementError, match="global size"):
        build(candidate, tmp_path / "too-large")


@pytest.mark.parametrize("filename", ["../escape.tar.gz", "CON", "trailing."])
def test_manifest_rejects_unsafe_cross_platform_names(candidate, tmp_path, filename):
    manifest = candidate[4]
    manifest["sources"][0]["file"] = filename
    manifest["package_source_mappings"]["orjson"]["files"] = [filename]
    candidate[1].write_text(json.dumps(manifest))
    with pytest.raises(supplement.SupplementError, match="unsafe|reserved"):
        build(candidate, tmp_path / "unsafe-name")


def test_checked_in_history_and_notices_are_identity_checked(candidate, tmp_path):
    candidate[3].joinpath("beta-source-manifest.json").write_text("changed")
    with pytest.raises(supplement.SupplementError, match="historical.*identity"):
        build(candidate, tmp_path / "changed-history")

    candidate[3].joinpath("beta-source-manifest.json").write_bytes(b'{"historical":"verified"}\n')
    candidate[3].joinpath("NOTICE.txt").write_text("changed")
    with pytest.raises(supplement.SupplementError, match="notice identity"):
        build(candidate, tmp_path / "changed-notice")


def test_reserved_or_existing_output_is_rejected(candidate, tmp_path):
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    output = output_dir / supplement.ARCHIVE_NAME
    output.write_text("do not replace")
    with pytest.raises(supplement.SupplementError, match="already exists"):
        supplement.build(
            candidate[1],
            output,
            requirements_lock=candidate[2],
            notices_directory=candidate[3],
            opener=Opener(candidate[-1]),
        )
    assert output.read_text() == "do not replace"


def test_checked_in_manifest_covers_lock_and_preserves_verified_source_identities():
    notices = REPO_ROOT / "docs" / "release" / "notices"
    manifest, _ = supplement._load_manifest(notices / supplement.MANIFEST_NAME)
    sources, checked_notices = supplement._validate_manifest(
        manifest,
        REPO_ROOT / "requirements.lock",
        notices,
    )
    historical = json.loads((notices / "beta-source-manifest.json").read_bytes())

    assert sources == historical["sources"]
    assert len(sources) == 74
    assert len(checked_notices) == 21
