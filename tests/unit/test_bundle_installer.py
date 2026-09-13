"""Offline bundle safety checks; no real package installation or network calls."""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bundle_installer.py"
SPEC = importlib.util.spec_from_file_location("bundle_installer_tests", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"Host cannot create test symlinks: {exc.__class__.__name__}")


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    identity = {"schema_version": 1, "platform": "macos-arm64-cp311", "python": "3.11"}
    build_id = installer.digest(installer.encoded(identity))
    files = {
        "Install.py": b"# installer fixture\n",
        "requirements.lock": b"example==1.0\n    --hash=sha256:" + b"a" * 64 + b"\n",
        "wheelhouse/example-1.0-py3-none-any.whl": b"dependency fixture",
        "wheels/opsgraph-0.1.0a4-py3-none-any.whl": b"application fixture",
        "build-identity.json": installer.encoded({**identity, "build_id": build_id}),
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    manifest = {
        **identity,
        "build_id": build_id,
        "wheel": "wheels/opsgraph-0.1.0a4-py3-none-any.whl",
        "files": [
            {"path": n, "size": len(d), "sha256": installer.digest(d)} for n, d in files.items()
        ],
    }
    (root / "manifest.json").write_bytes(installer.encoded(manifest))
    return root, manifest


def owned_runtime(root, manifest, complete=True):
    runtime = root / ".venv"
    runtime.mkdir()
    marker = {
        "owner": installer.OWNER,
        "build_id": manifest["build_id"],
        "directory": str(root),
        "complete": complete,
    }
    (runtime / installer.MARKER).write_text(json.dumps(marker))
    return runtime


def test_tampered_bundle_stops_before_runtime_or_pip(bundle, monkeypatch):
    root, _ = bundle
    (root / "requirements.lock").write_text("changed")
    monkeypatch.setattr(installer, "__file__", str(root / "Install.py"))
    monkeypatch.setattr(sys, "argv", ["Install.py", "install"])
    monkeypatch.setattr(installer.subprocess, "run", lambda *_a, **_kw: pytest.fail("pip ran"))
    with pytest.raises(SystemExit) as error:
        installer.main()
    assert error.value.code == 1
    assert not (root / ".venv").exists()


@pytest.mark.parametrize("value", [None, [], "text", {"schema_version": 1}])
def test_malformed_manifest_has_a_controlled_error(bundle, value):
    root, _ = bundle
    (root / "manifest.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        installer.verify(root)


@pytest.mark.parametrize("files", [None, {}, "text", [None], [{}], [{"path": []}]])
def test_malformed_manifest_file_records_are_rejected(bundle, files):
    root, manifest = bundle
    manifest["files"] = files
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        installer.verify(root)


def test_malformed_ownership_marker_is_refused(bundle):
    root, manifest = bundle
    runtime = owned_runtime(root, manifest)
    (runtime / installer.MARKER).write_text("[]")
    with pytest.raises(ValueError, match="ownership"):
        installer.ownership(root, manifest)


@pytest.mark.parametrize(
    "relative", ["../outside", "/outside", "C:/outside", "wheelhouse\\outside"]
)
def test_manifest_paths_cannot_escape_bundle(bundle, relative):
    root, _ = bundle
    with pytest.raises(ValueError, match="Invalid manifest path"):
        installer.checked_path(root, relative)


def test_extra_wheel_and_linked_input_are_rejected(bundle, tmp_path):
    root, _ = bundle
    extra = root / "wheelhouse" / "unexpected.whl"
    extra.write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="exactly"):
        installer.verify(root)
    extra.unlink()
    outside = tmp_path / "private"
    outside.write_bytes(b"unchanged")
    (root / "requirements.lock").unlink()
    symlink(root / "requirements.lock", outside)
    with pytest.raises(ValueError, match="links"):
        installer.verify(root)
    assert outside.read_bytes() == b"unchanged"


@pytest.mark.parametrize(
    "lock",
    [
        b"example @ https://example.invalid/a.whl",
        b"--index-url https://example.invalid",
        b"-r other.lock",
        b"./local.whl",
    ],
)
def test_lock_cannot_introduce_network_or_unpinned_inputs(lock):
    with pytest.raises(ValueError, match="pinned"):
        installer.validate_lock(lock)


def test_platform_and_disk_checked_before_creating_runtime(bundle, monkeypatch):
    root, manifest = bundle
    monkeypatch.setattr(installer.sys, "version_info", (3, 12))
    with pytest.raises(ValueError, match="CPython 3.11"):
        installer.install(root, manifest)
    monkeypatch.setattr(installer, "check_platform", lambda _: None)
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="2 GiB"):
        installer.install(root, manifest)
    assert not (root / ".venv").exists()


def test_uninstall_requires_confirmation_and_retains_data(bundle, tmp_path, monkeypatch):
    root, manifest = bundle
    runtime = owned_runtime(root, manifest)
    data = tmp_path / "workspace"
    data.mkdir()
    (data / "evidence.db").write_bytes(b"retained")
    monkeypatch.setattr("builtins.input", lambda _: "no")
    installer.uninstall(root, manifest)
    assert runtime.exists()
    symlink(runtime / "external", data, directory=True)
    monkeypatch.setattr("builtins.input", lambda _: "REMOVE")
    if installer.os.name == "nt":
        with pytest.raises(ValueError, match="link/junction"):
            installer.uninstall(root, manifest)
    else:
        installer.uninstall(root, manifest)
        assert not runtime.exists()
    assert (data / "evidence.db").read_bytes() == b"retained"


def test_uninstall_refuses_unowned_or_linked_runtime(bundle, tmp_path, monkeypatch):
    root, manifest = bundle
    outside = tmp_path / "unrelated"
    outside.mkdir()
    symlink(root / ".venv", outside, directory=True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must refuse before confirmation"))
    with pytest.raises(ValueError, match="real directory"):
        installer.uninstall(root, manifest)
    assert outside.is_dir()


def test_install_uses_offline_hashes_and_exact_wheel(bundle, monkeypatch):
    root, manifest = bundle
    calls = []
    monkeypatch.setattr(installer, "check_platform", lambda _: None)
    monkeypatch.setattr(
        installer.shutil, "disk_usage", lambda _: SimpleNamespace(free=installer.HEADROOM)
    )
    monkeypatch.setattr(installer.venv, "create", lambda *_a, **_kw: None)
    monkeypatch.setattr(installer, "run", lambda arguments, _root: calls.append(arguments))
    installer.install(root, installer.verify(root))
    assert len(calls) == 2
    assert all("--no-index" in call and "--isolated" in call for call in calls)
    assert "--require-hashes" in calls[0] and "--find-links" in calls[0]
    assert calls[1][-2:] == ["--no-deps", str(root / manifest["wheel"])]
    assert installer.ownership(root, manifest)["complete"]


def test_child_process_disables_pip_config_and_inherited_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("PIP_CONFIG_FILE", "/private/unrelated-pip.conf")
    monkeypatch.setenv("PIP_FIND_LINKS", "https://example.invalid/wheels")
    monkeypatch.setenv("PIP_TARGET", "/private/unrelated-target")
    monkeypatch.setenv("PYTHONPATH", "/private/unrelated-modules")
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda args, **kwargs: calls.append(kwargs))
    installer.run(["python", "-m", "pip", "--isolated"], tmp_path)
    env = calls[0]["env"]
    assert env["PIP_CONFIG_FILE"] == installer.os.devnull
    assert "PIP_FIND_LINKS" not in env
    assert "PIP_TARGET" not in env
    assert "PYTHONPATH" not in env


def test_launch_passes_dedicated_workspace_arguments(bundle, monkeypatch):
    root, manifest = bundle
    runtime = owned_runtime(root, manifest)
    executable = runtime / ("Scripts/opsgraph.exe" if installer.os.name == "nt" else "bin/opsgraph")
    executable.parent.mkdir()
    executable.write_bytes(b"fixture")
    args = ["--directory", str(root.parent / "private-workspace"), "--port", "8767", "--no-browser"]
    calls = []
    monkeypatch.setattr(installer, "check_platform", lambda _: None)
    monkeypatch.setattr(installer, "__file__", str(root / "Install.py"))
    monkeypatch.setattr(sys, "argv", ["Install.py", "launch", *args])
    monkeypatch.setattr(installer, "run", lambda arguments, _root: calls.append(arguments))
    installer.main()
    assert calls == [[str(executable), "launch", "--directory=" + args[1], *args[2:]]]


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.setitem(sys.modules, "bundle_installer", installer)
    spec = importlib.util.spec_from_file_location(
        "bundle_builder_tests", SCRIPT.with_name("build_release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_includes_new_source_and_excludes_private_runtime(builder, tmp_path, monkeypatch):
    files = {
        "src/opsgraph/new.py": b"new source",
        "README.md": b"edited readme",
        "CHANGELOG.md": b"release history",
        ".dockerignore": b"private inputs excluded",
        "build-constraints.txt": b"pinned build tools",
        "skillpacks/custom/skill.yaml": b"declarative skill",
        "policies/readonly.yaml": b"declarative policy",
        ".env": b"private",
        "scripts/.env.local": b"private",
        "docs/reviews/raw.md": b"review",
        "docs/internal-session.md": b"private working notes",
        "docs/screenshots/private.png": b"private image",
        "docs/quickstart.md": b"public setup",
        "src/opsgraph/__pycache__/cache.pyc": b"cache",
        "notes.txt": b"personal",
    }
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def git(arguments, **kwargs):
        return SimpleNamespace(stdout="base\n" if kwargs.get("text") else "\0".join(files).encode())

    monkeypatch.setattr(builder.subprocess, "run", git)
    base, snapshot = builder.snapshot(tmp_path)
    assert base == "base"
    assert snapshot == {
        "src/opsgraph/new.py": b"new source",
        "README.md": b"edited readme",
        "CHANGELOG.md": b"release history",
        ".dockerignore": b"private inputs excluded",
        "build-constraints.txt": b"pinned build tools",
        "skillpacks/custom/skill.yaml": b"declarative skill",
        "policies/readonly.yaml": b"declarative policy",
        "docs/quickstart.md": b"public setup",
    }


def test_bundle_is_reproducible_and_rejects_stale_wheel(builder, tmp_path, monkeypatch):
    dependency = b"test dependency"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(dependency)
    lock = f"example==1.0\n    --hash=sha256:{installer.digest(dependency)}\n".encode()
    sources = {
        ".gitattributes": b"* text=auto eol=lf\n",
        "src/opsgraph/__init__.py": b"VERSION = 'fixture'\n",
        "requirements.lock": lock,
        "LICENSE": b"test license",
        "README.md": b"test readme",
        "SECURITY.md": b"test boundary",
        "scripts/bundle_installer.py": SCRIPT.read_bytes(),
        "scripts/bundle-launchers/Install.command": b"#!/bin/sh\n",
    }
    monkeypatch.setattr(builder, "snapshot", lambda _: ("f" * 40, sources))
    wheel = tmp_path / "opsgraph-0.1.0a4-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("opsgraph/__init__.py", sources["src/opsgraph/__init__.py"])
    outputs = [tmp_path / "first.zip", tmp_path / "second.zip"]
    for output in outputs:
        builder.build(tmp_path, wheel, wheelhouse, output, "macos-arm64-cp311")
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with zipfile.ZipFile(outputs[0]) as archive:
        name = "opsgraph-macos-arm64-cp311/manifest.json"
        manifest = json.loads(archive.read(name))
        assert manifest["platform"] == "macos-arm64-cp311"
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
    sources["src/opsgraph/__init__.py"] = b"changed since the wheel build"
    with pytest.raises(ValueError, match="does not exactly match"):
        builder.build(tmp_path, wheel, wheelhouse, tmp_path / "stale.zip", "macos-arm64-cp311")
    assert not (tmp_path / "stale.zip").exists()


@pytest.fixture
def source_archive(builder, tmp_path):
    root = tmp_path / "extracted-source"
    root.mkdir()
    files = {"README.md": b"readme", "src/opsgraph/__init__.py": b"source"}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    inventory = {
        "schema_version": 1,
        "source_base_commit": "a" * 40,
        "scope": builder.SOURCE_SCOPE,
        "excluded": sorted(builder.EXCLUDED | builder.PRIVATE_SUFFIXES),
        "files": [builder.record(name, content) for name, content in sorted(files.items())],
    }
    path = tmp_path / "source-inventory.json"
    path.write_bytes(installer.encoded(inventory))
    return root, path, inventory


def test_archive_build_matches_git_inventory_without_git(builder, tmp_path, monkeypatch):
    dependency = b"test dependency"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(dependency)
    sources = {
        "src/opsgraph/__init__.py": b"VERSION = 'fixture'\n",
        "CHANGELOG.md": b"release history",
        ".gitattributes": b"* text=auto eol=lf\n",
        "requirements.lock": (
            f"example==1.0\n    --hash=sha256:{installer.digest(dependency)}\n".encode()
        ),
        "LICENSE": b"test license",
        "README.md": b"test readme",
        "SECURITY.md": b"test boundary",
        "scripts/bundle_installer.py": SCRIPT.read_bytes(),
    }
    root = tmp_path / "source"
    root.mkdir()
    for name, content in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(builder, "snapshot", lambda _: ("a" * 40, sources))
    wheel = tmp_path / "opsgraph-0.1.0a4-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("opsgraph/__init__.py", sources["src/opsgraph/__init__.py"])
    original = tmp_path / "git.zip"
    builder.build(root, wheel, wheelhouse, original, "macos-arm64-cp311")
    with zipfile.ZipFile(original) as archive:
        inventory_bytes = archive.read("opsgraph-macos-arm64-cp311/source-inventory.json")
    inventory_path = tmp_path / "source-inventory.json"
    inventory_path.write_bytes(inventory_bytes)
    # Generated metadata and unlisted private files must not enter the new snapshot.
    (root / "PKG-INFO").write_text("generated by the source distribution")
    (root / ".env").write_text("private fixture")
    monkeypatch.setattr(builder, "snapshot", lambda _: pytest.fail("Git inventory ran"))
    monkeypatch.setattr(builder.subprocess, "run", lambda *_a, **_kw: pytest.fail("Git ran"))
    rebuilt = tmp_path / "archive.zip"
    builder.build(
        root,
        wheel,
        wheelhouse,
        rebuilt,
        "macos-arm64-cp311",
        source_inventory=inventory_path,
        source_inventory_sha256=installer.digest(inventory_bytes),
    )
    assert original.read_bytes() == rebuilt.read_bytes()


def test_source_inventory_requires_separate_trusted_digest(builder, source_archive):
    root, path, _ = source_archive
    with pytest.raises(ValueError, match="SHA-256"):
        builder.inventory_snapshot(root, path, None)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        builder.inventory_snapshot(root, path, "0" * 64)


@pytest.mark.parametrize("provided", ["source_inventory", "source_inventory_sha256"])
def test_archive_options_must_be_supplied_together(builder, source_archive, provided):
    root, path, _ = source_archive
    value = path if provided == "source_inventory" else installer.digest(path.read_bytes())
    with pytest.raises(ValueError, match="Supply both"):
        builder.build(
            root, root / "wheel.whl", root, root / "output.zip", "target", **{provided: value}
        )
    assert not (root / "output.zip").exists()


def test_linked_inventory_is_refused(builder, source_archive, tmp_path):
    root, path, _ = source_archive
    alias = tmp_path / "inventory-alias.json"
    symlink(alias, path)
    with pytest.raises(ValueError, match="regular file"):
        builder.inventory_snapshot(root, alias, installer.digest(path.read_bytes()))


@pytest.mark.parametrize("change", ["modified", "missing", "symlink", "parent_symlink"])
def test_inventory_rejects_changed_or_linked_source(builder, source_archive, tmp_path, change):
    root, path, _ = source_archive
    source = root / "src/opsgraph/__init__.py"
    source.unlink()
    if change == "modified":
        source.write_bytes(b"edited")  # Same size; verification must check the digest.
    elif change == "symlink":
        outside = tmp_path / "external"
        outside.write_bytes(b"source")
        symlink(source, outside)
    elif change == "parent_symlink":
        source.parent.rmdir()
        outside = tmp_path / "external"
        outside.mkdir()
        (outside / "__init__.py").write_bytes(b"source")
        symlink(source.parent, outside, directory=True)
    with pytest.raises((ValueError, OSError)):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


@pytest.mark.parametrize(
    "name",
    [
        "src/../../outside",
        "/etc/passwd",
        "C:/outside",
        "src\\outside",
        "src/.env",
        "docs/reviews/raw.md",
        "src/private.db",
        "PKG-INFO",
    ],
)
def test_inventory_rejects_traversal_and_out_of_scope_paths(builder, source_archive, name):
    root, path, inventory = source_archive
    inventory["files"][0]["path"] = name
    path.write_bytes(installer.encoded(inventory))
    with pytest.raises(ValueError):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


@pytest.mark.parametrize("change", ["schema", "base", "duplicate", "negative", "oversize"])
def test_inventory_rejects_invalid_identity_and_records(builder, source_archive, change):
    root, path, inventory = source_archive
    if change == "schema":
        inventory["schema_version"] = True
    elif change == "base":
        inventory["source_base_commit"] = "unverified"
    elif change == "duplicate":
        inventory["files"].append(inventory["files"][0])
    elif change == "negative":
        inventory["files"][0]["size"] = -1
    elif change == "oversize":
        inventory["files"][0]["size"] = builder.MAX_SOURCE_BYTES + 1
    path.write_bytes(installer.encoded(inventory))
    with pytest.raises(ValueError):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


def test_inventory_is_bounded_before_parsing(builder, source_archive, monkeypatch):
    root, path, _ = source_archive
    monkeypatch.setattr(builder, "MAX_INVENTORY_BYTES", 10)
    with pytest.raises(ValueError, match="size limit"):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


def test_inventory_rejects_duplicate_json_keys(builder, source_archive):
    root, path, _ = source_archive
    path.write_bytes(b'{"schema_version": 1, "schema_version": 2}')
    with pytest.raises(ValueError, match="Duplicate"):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


def test_deeply_nested_inventory_has_a_controlled_error(builder, source_archive):
    root, path, _ = source_archive
    path.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
    # JSON decoder recursion limits vary by Python version. A decoder that can
    # parse this input must still reject its unsupported inventory shape.
    with pytest.raises(
        ValueError,
        match=r"^(Source inventory nesting exceeds the parser limit|Unsupported source inventory)$",
    ):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


def test_inventory_parser_recursion_has_a_controlled_error(builder, source_archive, monkeypatch):
    root, path, _ = source_archive

    def recursion_limit(*_args, **_kwargs):
        raise RecursionError("private parser details")

    monkeypatch.setattr(builder, "json", SimpleNamespace(loads=recursion_limit))
    with pytest.raises(ValueError, match="^Source inventory nesting exceeds the parser limit$"):
        builder.inventory_snapshot(root, path, installer.digest(path.read_bytes()))


def test_only_reviewed_documentation_is_distributable(builder):
    assert builder.allowed_source("docs/quickstart.md")
    assert builder.allowed_source("docs/release/notices/GPL-3.0.txt")
    for name in (
        "docs/session-notes.md",
        "docs/release/new-internal-report.md",
        "docs/release/screenshots/private.png",
        "docs/release/notices/internal-notes.md",
    ):
        assert not builder.allowed_source(name)
    assert all(name.startswith("docs/") for name in builder.PUBLIC_DOCS)


def test_repository_docs_require_explicit_distribution_review(builder):
    root = SCRIPT.parents[1]
    actual = {
        path.relative_to(root).as_posix() for path in (root / "docs").rglob("*") if path.is_file()
    }
    assert actual == builder.PUBLIC_DOCS


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "127.0.0.1"],
        ["--no-b"],
        ["serve"],
        ["--port", "0"],
        ["--port", "65536"],
        ["--port", "no"],
    ],
)
def test_launch_rejects_unknown_or_invalid_options_before_verification(arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Install.py", "launch", *arguments])
    monkeypatch.setattr(installer, "verify", lambda _: pytest.fail("must reject before verifying"))
    with pytest.raises(SystemExit) as error:
        installer.main()
    assert error.value.code == 2


def test_launch_preserves_path_values_and_supported_flags():
    assert installer.launch_arguments(
        ["--directory=-workspace with spaces", "--port=8767", "--configure", "--no-browser"]
    ) == ["--directory=-workspace with spaces", "--port", "8767", "--configure", "--no-browser"]


def test_launch_help_needs_no_runtime(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["Install.py", "launch", "--help"])
    monkeypatch.setattr(installer, "verify", lambda _: pytest.fail("help needs no runtime"))
    with pytest.raises(SystemExit) as error:
        installer.main()
    assert error.value.code == 0
    assert "--configure" in capsys.readouterr().out
