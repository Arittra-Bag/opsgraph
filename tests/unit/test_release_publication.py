import copy
import hashlib
import json

import pytest

from scripts.reconcile_release_publication import (
    PublicationError,
    alias_plan,
    compare_assets,
    container_tag_state,
    ensure_recovery_release,
    publish_release,
    reconcile_assets,
    verify_index_manifest,
    verify_platform_manifest,
)

REPOSITORY = "Arittra-Bag/opsgraph"
TAG = "v1.2.3"
COMMIT = "a" * 40
AMD64_DIGEST = f"sha256:{'b' * 64}"
ARM64_DIGEST = f"sha256:{'c' * 64}"
IMAGE_ID = f"sha256:{'d' * 64}"


def release(
    tag=TAG,
    *,
    draft=False,
    prerelease=False,
    title="OpsGraph 1.2.3",
    notes="notes\n",
    release_id=7,
):
    return {
        "id": release_id,
        "tag_name": tag,
        "name": title,
        "body": notes,
        "draft": draft,
        "prerelease": prerelease,
    }


class FakeClient:
    def __init__(self, *, releases=None, versions=None, assets=None, latest=None):
        self.releases = list(releases or [])
        self.versions = list(versions or [])
        self.assets = list(assets or [])
        self.latest = latest
        self.requests = []

    def paginated(self, path):
        if path.endswith("/versions"):
            return copy.deepcopy(self.versions)
        if path.endswith("/assets"):
            return copy.deepcopy(self.assets)
        if path.endswith("/releases"):
            return copy.deepcopy(self.releases)
        raise AssertionError(path)

    def request_json(self, method, path, *, payload=None, expected_status=200):
        self.requests.append((method, path, payload, expected_status))
        if method == "POST" and path.endswith("/releases"):
            created = release(
                payload["tag_name"],
                draft=True,
                title=payload["name"],
                notes=payload["body"],
                release_id=17,
            )
            self.releases.append(created)
            return copy.deepcopy(created)
        if method == "PATCH":
            current = next(item for item in self.releases if item["tag_name"] == TAG)
            current.update(
                name=payload["name"],
                body=payload["body"],
                draft=payload["draft"],
                prerelease=payload["prerelease"],
            )
            if payload["make_latest"] == "true":
                self.latest = current
            return copy.deepcopy(current)
        if method == "GET" and path.endswith("/latest"):
            if self.latest is None:
                published = [item for item in self.releases if not item["draft"]]
                return copy.deepcopy(max(published, key=lambda item: item["tag_name"]))
            return copy.deepcopy(self.latest)
        raise AssertionError((method, path, payload, expected_status))


def test_alias_plan_advances_every_alias_for_the_first_stable_release():
    plan = alias_plan([], repository=REPOSITORY, tag=TAG)

    assert plan["container_aliases"] == ["1.2", "1", "latest"]
    assert plan["github_latest"] is True


def test_alias_plan_never_regresses_to_an_older_patch():
    plan = alias_plan(
        [release("v1.2.4"), release("v1.3.0", release_id=8), release("v2.0.0", release_id=9)],
        repository=REPOSITORY,
        tag=TAG,
    )

    assert plan["container_aliases"] == []
    assert plan["github_latest"] is False


def test_alias_plan_can_advance_its_own_minor_line_but_not_major_or_latest():
    plan = alias_plan(
        [release("v1.3.0", release_id=8), release("v2.0.0", release_id=9)],
        repository=REPOSITORY,
        tag=TAG,
    )

    assert plan["container_aliases"] == ["1.2"]
    assert plan["github_latest"] is False


def test_alias_plan_ignores_drafts_prereleases_and_non_stable_tags():
    plan = alias_plan(
        [
            release("v9.0.0", draft=True),
            release("v8.0.0", prerelease=True, release_id=8),
            release("v7.0.0-rc1", release_id=9),
        ],
        repository=REPOSITORY,
        tag=TAG,
    )

    assert plan["container_aliases"] == ["1.2", "1", "latest"]


def test_prepare_creates_a_draft_recovery_point_before_publication():
    client = FakeClient()

    result = ensure_recovery_release(
        client,
        repository=REPOSITORY,
        tag=TAG,
        source_commit=COMMIT,
        title="OpsGraph 1.2.3",
        notes="notes\n",
    )

    assert result["state"] == "draft"
    assert result["created"] is True
    assert client.requests[0][0] == "POST"
    assert client.requests[0][2]["target_commitish"] == COMMIT
    assert client.requests[0][2]["make_latest"] == "false"


@pytest.mark.parametrize("draft", [True, False])
def test_prepare_reuses_an_exact_draft_or_published_release(draft):
    client = FakeClient(releases=[release(draft=draft)])

    result = ensure_recovery_release(
        client,
        repository=REPOSITORY,
        tag=TAG,
        source_commit=COMMIT,
        title="OpsGraph 1.2.3",
        notes="notes\n",
    )

    assert result["created"] is False
    assert result["state"] == ("draft" if draft else "published")
    assert client.requests == []


def test_prepare_rejects_conflicting_release_notes():
    client = FakeClient(releases=[release(notes="changed")])

    with pytest.raises(PublicationError, match="release notes do not match"):
        ensure_recovery_release(
            client,
            repository=REPOSITORY,
            tag=TAG,
            source_commit=COMMIT,
            title="OpsGraph 1.2.3",
            notes="notes\n",
        )


def test_container_tag_absence_requires_a_successful_complete_listing():
    client = FakeClient(versions=[])

    result = container_tag_state(client, owner="Arittra-Bag", package="opsgraph", tag=TAG)

    assert result == {"schema_version": 1, "tag": TAG, "state": "absent", "digest": None}


def test_container_tag_reuses_one_exact_registry_digest():
    client = FakeClient(
        versions=[
            {
                "name": AMD64_DIGEST,
                "metadata": {"container": {"tags": [TAG, "1.2"]}},
            }
        ]
    )

    result = container_tag_state(client, owner="Arittra-Bag", package="opsgraph", tag=TAG)

    assert result["state"] == "existing"
    assert result["digest"] == AMD64_DIGEST


def test_container_tag_rejects_duplicate_tag_ownership():
    client = FakeClient(
        versions=[
            {"name": AMD64_DIGEST, "metadata": {"container": {"tags": [TAG]}}},
            {"name": ARM64_DIGEST, "metadata": {"container": {"tags": [TAG]}}},
        ]
    )

    with pytest.raises(PublicationError, match="multiple package versions"):
        container_tag_state(client, owner="Arittra-Bag", package="opsgraph", tag=TAG)


def image_manifest():
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": 100,
            "digest": IMAGE_ID,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 200,
                "digest": f"sha256:{'e' * 64}",
            }
        ],
    }


def test_platform_manifest_binds_remote_tag_to_the_accepted_image(tmp_path):
    path = tmp_path / "manifest.json"
    raw = json.dumps(image_manifest(), separators=(",", ":")).encode()
    path.write_bytes(raw)
    expected = f"sha256:{hashlib.sha256(raw).hexdigest()}"

    assert verify_platform_manifest(path, image_id=IMAGE_ID, existing_digest=expected) == expected


def test_platform_manifest_rejects_a_different_image_config(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = image_manifest()
    manifest["config"]["digest"] = f"sha256:{'f' * 64}"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PublicationError, match="config changed"):
        verify_platform_manifest(path, image_id=IMAGE_ID)


def image_index():
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": AMD64_DIGEST,
                "size": 123,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": ARM64_DIGEST,
                "size": 456,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ],
    }


def test_index_manifest_binds_existing_tag_to_exact_platform_digests(tmp_path):
    path = tmp_path / "index.json"
    raw = json.dumps(image_index(), separators=(",", ":")).encode()
    path.write_bytes(raw)
    expected = f"sha256:{hashlib.sha256(raw).hexdigest()}"

    assert (
        verify_index_manifest(
            path,
            amd64_digest=AMD64_DIGEST,
            arm64_digest=ARM64_DIGEST,
            existing_digest=expected,
        )
        == expected
    )


def test_asset_comparison_allows_only_missing_assets_during_recovery(tmp_path):
    one = tmp_path / "one.zip"
    two = tmp_path / "two.tar.gz"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    expected = {
        "one.zip": {
            "path": one,
            "size": 3,
            "digest": f"sha256:{hashlib.sha256(b'one').hexdigest()}",
        },
        "two.tar.gz": {
            "path": two,
            "size": 3,
            "digest": f"sha256:{hashlib.sha256(b'two').hexdigest()}",
        },
    }
    actual = [
        {
            "name": "one.zip",
            "size": 3,
            "digest": expected["one.zip"]["digest"],
            "state": "uploaded",
        }
    ]

    assert compare_assets(expected, actual, allow_missing=True) == ["two.tar.gz"]
    with pytest.raises(PublicationError, match="assets are missing"):
        compare_assets(expected, actual, allow_missing=False)


def test_asset_comparison_rejects_same_name_with_different_bytes(tmp_path):
    path = tmp_path / "one.zip"
    path.write_bytes(b"one")
    expected = {
        "one.zip": {
            "path": path,
            "size": 3,
            "digest": f"sha256:{hashlib.sha256(b'one').hexdigest()}",
        }
    }

    with pytest.raises(PublicationError, match="digest changed"):
        compare_assets(
            expected,
            [
                {
                    "name": "one.zip",
                    "size": 3,
                    "digest": f"sha256:{'0' * 64}",
                    "state": "uploaded",
                }
            ],
            allow_missing=True,
        )


def test_reconcile_assets_resumes_after_a_partial_upload(tmp_path):
    path = tmp_path / "one.zip"
    path.write_bytes(b"one")
    digest = f"sha256:{hashlib.sha256(b'one').hexdigest()}"
    client = FakeClient(releases=[release(draft=True)], assets=[])
    uploaded = []

    def upload(repository, tag, asset):
        uploaded.append((repository, tag, asset.name))
        client.assets.append({"name": asset.name, "size": 3, "digest": digest, "state": "uploaded"})

    reconcile_assets(
        client,
        repository=REPOSITORY,
        tag=TAG,
        directory=tmp_path,
        uploader=upload,
    )

    assert uploaded == [(REPOSITORY, TAG, "one.zip")]


def test_publish_accepts_an_exact_already_published_release_without_patch(tmp_path):
    path = tmp_path / "one.zip"
    path.write_bytes(b"one")
    current = release()
    client = FakeClient(
        releases=[current],
        assets=[
            {
                "name": "one.zip",
                "size": 3,
                "digest": f"sha256:{hashlib.sha256(b'one').hexdigest()}",
                "state": "uploaded",
            }
        ],
        latest=current,
    )
    plan = alias_plan(client.releases, repository=REPOSITORY, tag=TAG)

    result = publish_release(
        client,
        repository=REPOSITORY,
        tag=TAG,
        title="OpsGraph 1.2.3",
        notes="notes\n",
        directory=tmp_path,
        plan=plan,
    )

    assert result["state"] == "published"
    assert not any(request[0] == "PATCH" for request in client.requests)


def test_publish_rejects_a_stale_alias_plan(tmp_path):
    path = tmp_path / "one.zip"
    path.write_bytes(b"one")
    current = release(draft=True)
    client = FakeClient(
        releases=[current, release("v2.0.0", release_id=8)],
        assets=[
            {
                "name": "one.zip",
                "size": 3,
                "digest": f"sha256:{hashlib.sha256(b'one').hexdigest()}",
                "state": "uploaded",
            }
        ],
    )
    stale = alias_plan([current], repository=REPOSITORY, tag=TAG)

    with pytest.raises(PublicationError, match="changed after alias planning"):
        publish_release(
            client,
            repository=REPOSITORY,
            tag=TAG,
            title="OpsGraph 1.2.3",
            notes="notes\n",
            directory=tmp_path,
            plan=stale,
        )
