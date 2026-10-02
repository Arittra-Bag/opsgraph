import copy
import json

import pytest

from scripts.verify_container_index import (
    ContainerIndexError,
    main,
    verify_container_index,
)

AMD64_DIGEST = f"sha256:{'a' * 64}"
ARM64_DIGEST = f"sha256:{'b' * 64}"


def valid_index() -> dict[str, object]:
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
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "digest": ARM64_DIGEST,
                "size": 456,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ],
    }


def verify(index: object) -> None:
    verify_container_index(
        index,
        amd64_digest=AMD64_DIGEST,
        arm64_digest=ARM64_DIGEST,
    )


def test_accepts_the_exact_two_platform_index():
    verify(valid_index())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value.update(schemaVersion=1), "schemaVersion must be 2"),
        (lambda value: value.update(mediaType="application/json"), "unsupported mediaType"),
        (lambda value: value["manifests"].pop(), "exactly two image descriptors"),
        (
            lambda value: value["manifests"][1]["platform"].update(architecture="s390x"),
            "unexpected platform: linux/s390x",
        ),
        (
            lambda value: value["manifests"][1]["platform"].update(architecture="amd64"),
            "repeats platform: linux/amd64",
        ),
        (
            lambda value: value["manifests"][0].update(digest=f"sha256:{'c' * 64}"),
            "digest mismatch for linux/amd64",
        ),
        (
            lambda value: value["manifests"][0].update(digest="sha256:not-a-digest"),
            "invalid SHA-256 digest",
        ),
        (
            lambda value: value["manifests"][0].update(size=0),
            "size must be a positive integer",
        ),
        (
            lambda value: value["manifests"][0].update(mediaType="application/json"),
            "unsupported manifest mediaType",
        ),
    ],
)
def test_rejects_malformed_or_unexpected_descriptors(change, message):
    index = copy.deepcopy(valid_index())
    change(index)

    with pytest.raises(ContainerIndexError, match=message):
        verify(index)


def test_rejects_an_invalid_expected_digest():
    with pytest.raises(ContainerIndexError, match="expected digest for linux/amd64"):
        verify_container_index(
            valid_index(),
            amd64_digest="not-a-digest",
            arm64_digest=ARM64_DIGEST,
        )


def test_cli_reports_invalid_json_and_returns_nonzero(tmp_path, capsys):
    manifest = tmp_path / "index.json"
    manifest.write_text("{", encoding="utf-8")

    result = main(
        [
            "--manifest",
            str(manifest),
            "--amd64-digest",
            AMD64_DIGEST,
            "--arm64-digest",
            ARM64_DIGEST,
        ]
    )

    assert result == 1
    assert "container index verification failed: container index is not valid JSON" in (
        capsys.readouterr().err
    )


def test_cli_verifies_a_valid_index(tmp_path, capsys):
    manifest = tmp_path / "index.json"
    manifest.write_text(json.dumps(valid_index()), encoding="utf-8")

    result = main(
        [
            "--manifest",
            str(manifest),
            "--amd64-digest",
            AMD64_DIGEST,
            "--arm64-digest",
            ARM64_DIGEST,
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == (
        "Verified container index for linux/amd64 and linux/arm64.\n"
    )
