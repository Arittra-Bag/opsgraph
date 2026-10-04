"""Exercise the release tag guard against real temporary Git repositories."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
BASH = shutil.which("bash")


def git(directory, *args):
    return subprocess.check_output(  # noqa: S603 - fixed Git commands in temporary fixtures
        [GIT, "-C", str(directory), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def checkout(tmp_path):
    if os.name == "nt" or not GIT or not BASH:
        pytest.skip("The release tag guard runs in a POSIX Git and Bash environment")
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Release test")
    git(source, "config", "user.email", "release@example.invalid")
    files = {
        "pyproject.toml": '[project]\nversion = "1.0.0"\n',
        "src/opsgraph/__init__.py": '__version__ = "1.0.0"\n',
        "docs/release/release-notes-1.0.0.md": "Release notes\n",
        "docs/release/notices/source-manifest-1.0.0.json": "{}\n",
    }
    for name, value in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    git(source, "add", ".")
    git(source, "-c", "commit.gpgsign=false", "commit", "-m", "Prepare release")
    commit = git(source, "rev-parse", "HEAD")
    git(source, "-c", "tag.gpgsign=false", "tag", "-a", "v1.0.0", "-m", "Release")
    remote = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    work = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "checkout", "--detach", commit)
    return work, commit, tmp_path / "output"


def guard(work, output, tag="v1.0.0"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["validate-tag"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "release")
    return subprocess.run(  # noqa: S603 - exercise the checked-in release guard
        [BASH, "-c", script],
        cwd=work,
        env={**os.environ, "TAG_NAME": tag, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )


def test_explicit_commit_checkout_retains_annotated_tag_and_exact_outputs(checkout):
    work, commit, output = checkout
    assert git(work, "cat-file", "-t", "refs/tags/v1.0.0") == "tag"
    result = guard(work, output)
    assert result.returncode == 0, result.stderr
    assert f"source_commit={commit}\n" in output.read_text()
    assert "tag=v1.0.0\n" in output.read_text()
    assert "version=1.0.0\n" in output.read_text()


def test_peeled_lightweight_tag_is_rejected(checkout):
    work, commit, output = checkout
    git(work, "update-ref", "refs/tags/v1.0.0", commit)
    result = guard(work, output)
    assert result.returncode != 0
    assert "require an annotated tag" in result.stderr
    assert not output.exists()


def test_dirty_tag_checkout_is_rejected(checkout):
    work, _, output = checkout
    (work / "unexpected.txt").write_text("untracked")
    result = guard(work, output)
    assert result.returncode != 0
    assert "checkout is not clean" in result.stderr
    assert not output.exists()


def test_recovery_preserves_version_matching(checkout):
    work, _, output = checkout
    git(work, "-c", "tag.gpgsign=false", "tag", "-a", "v2.0.0", "-m", "Wrong version")
    result = guard(work, output, tag="v2.0.0")
    assert result.returncode != 0
    assert "does not match project version" in result.stderr
    assert not output.exists()


def test_push_and_recovery_checkout_keep_tag_identity():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert events["workflow_dispatch"]["inputs"]["tag"]["required"] is True
    checkout_step = workflow["jobs"]["validate-tag"]["steps"][0]
    assert checkout_step["with"]["ref"] == "${{ inputs.tag || github.sha }}"
    binding = next(
        step for step in workflow["jobs"]["validate-tag"]["steps"] if step.get("id") == "release"
    )
    assert binding["env"]["TAG_NAME"] == "${{ inputs.tag || github.ref_name }}"
