import json
import os
import platform
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace

import pytest

from scripts import ci_bundle_acceptance as acceptance


def test_public_acceptance_environment_removes_app_database_model_and_proxy_secrets(monkeypatch):
    inherited = {
        "OPSGRAPH_SOURCE_DSN": "private-dsn",
        "OPENAI_API_KEY": "private-model-key",
        "ANTHROPIC_API_KEY": "private-model-key",
        "PGPASSWORD": "private-database-password",
        "PIP_INDEX_URL": "https://private-index.example",
        "PYTHONPATH": "/private/import/path",
        "LANGSMITH_API_KEY": "private-tracing-key",
        "HTTPS_PROXY": "http://private-proxy.example",
        "GITHUB_TOKEN": "private-github-token",
        "CIRCLE_OIDC_TOKEN_V2": "private-circle-token",
        "AWS_SECRET_ACCESS_KEY": "private-cloud-key",
        "DATABASE_URL": "postgresql://private-database",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "safe-path")

    environment = acceptance._clean_environment()

    assert not inherited.keys() & environment.keys()
    assert environment["PATH"] == "safe-path"
    assert environment["LANGSMITH_TRACING"] == "false"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost,::1"


@pytest.mark.skipif(os.name == "nt", reason="Windows environment names are case-insensitive")
def test_public_acceptance_environment_rejects_mixed_case_posix_aliases(monkeypatch):
    monkeypatch.setenv("pAtH", "untrusted-path")
    monkeypatch.setenv("LaNg", "untrusted-locale")

    environment = acceptance._clean_environment()

    assert "pAtH" not in environment
    assert "LaNg" not in environment
    assert "untrusted-path" not in environment.values()
    assert "untrusted-locale" not in environment.values()


def test_public_acceptance_environment_preserves_windows_architecture_without_secrets(monkeypatch):
    monkeypatch.setattr(
        acceptance,
        "os",
        SimpleNamespace(
            name="nt",
            environ={
                "Processor_Architecture": "x86",
                "Processor_ArchiteW6432": "AMD64",
                "PROCESSOR_PRIVATE_TOKEN": "private-processor-token",
                "GITHUB_TOKEN": "private-github-token",
                "OPENAI_API_KEY": "private-model-key",
            },
        ),
    )

    assert acceptance._clean_environment() == {
        "PROCESSOR_ARCHITECTURE": "x86",
        "PROCESSOR_ARCHITEW6432": "AMD64",
        "LANGSMITH_TRACING": "false",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows architecture detection")
def test_clean_windows_subprocess_retains_detected_architecture():
    detected = subprocess.run(
        [sys.executable, "-c", "import platform; print(platform.machine())"],
        env=acceptance._clean_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()

    assert detected
    assert detected == platform.machine()


def test_public_failure_output_redacts_credentials_and_private_paths(tmp_path):
    private = tmp_path / "workspace"
    message = (
        f"path={private} "
        "dsn=postgresql://operator:private-password@127.0.0.1/test "
        "api_key=private-model-key token:private-launch-token "
        "GITHUB_TOKEN=private-github-token CIRCLE_OIDC_TOKEN_V2=private-circle-token "
        "AWS_SECRET_ACCESS_KEY=private-cloud-key DATABASE_URL=private-database-url "
        '"api_key": "private-json-key" '
        "'CIRCLE_TOKEN': 'private-python-token' "
        "https://operator:private-basic-password@example.invalid/path"
    )

    sanitized = acceptance._sanitized(message, (private,))

    assert "<temporary-path>" in sanitized
    assert "operator" not in sanitized
    assert "private-password" not in sanitized
    assert "private-model-key" not in sanitized
    assert "private-launch-token" not in sanitized
    assert "private-github-token" not in sanitized
    assert "private-circle-token" not in sanitized
    assert "private-cloud-key" not in sanitized
    assert "private-database-url" not in sanitized
    assert "private-json-key" not in sanitized
    assert "private-python-token" not in sanitized
    assert "private-basic-password" not in sanitized


def test_public_failure_output_redacts_an_unlabelled_generated_workspace_key():
    key = "generated-workspace-key-that-must-remain-private"

    sanitized = acceptance._sanitized(f"launcher output: {key}", private_values=(key,))

    assert key not in sanitized
    assert "<redacted>" in sanitized


def test_authenticated_request_redirects_are_rejected():
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/sources",
        headers={"X-OpsGraph-Key": "private-key"},
    )

    with pytest.raises(urllib.error.HTTPError, match="redirects are not accepted"):
        acceptance._RejectRedirect().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://example.invalid/collect",
        )


def test_windows_bootstrap_waits_for_job_assignment_gate(tmp_path):
    marker = tmp_path / "started"
    command = [
        sys.executable,
        "-I",
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')",
        str(marker),
    ]
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-c",
            acceptance.WINDOWS_JOB_BOOTSTRAP,
            json.dumps(command),
            "null",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        time.sleep(0.1)
        assert not marker.exists()
        assert process.stdin is not None
        process.stdin.write("G")
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert marker.read_text() == "started"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_x64_target_rejects_a_32_bit_interpreter(monkeypatch):
    monkeypatch.setattr(acceptance.sys, "version_info", (3, 11))
    monkeypatch.setattr(acceptance.sys, "platform", "linux")
    monkeypatch.setattr(acceptance.platform, "python_implementation", lambda: "CPython")
    monkeypatch.setattr(acceptance.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(acceptance.struct, "calcsize", lambda _: 4)
    monkeypatch.setattr(
        acceptance,
        "_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )

    with pytest.raises(acceptance.AcceptanceError, match="native CPython 3.11"):
        acceptance._validate_host("ubuntu-x64-cp311")


def test_candidate_checksum_accepts_windows_line_endings(tmp_path):
    candidate = tmp_path / "candidate.zip"
    candidate.write_bytes(b"candidate")
    digest = acceptance._sha256(candidate)
    candidate.with_suffix(".zip.sha256").write_bytes(
        f"{digest}  {candidate.name}\r\n".encode("ascii")
    )

    assert acceptance._verify_checksum(candidate) == digest


@pytest.mark.parametrize(
    "member",
    [
        "opsgraph-macos-arm64-cp311/../../outside",
        "opsgraph-macos-arm64-cp311/C:\\outside",
        "/opsgraph-macos-arm64-cp311/outside",
    ],
)
def test_bundle_extraction_rejects_unsafe_members_without_writing_outside(tmp_path, member):
    candidate = tmp_path / "candidate.zip"
    info = zipfile.ZipInfo(member)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    with zipfile.ZipFile(candidate, "w") as archive:
        archive.writestr(info, b"untrusted")

    with pytest.raises(acceptance.AcceptanceError, match="unsafe"):
        acceptance._extract(
            candidate,
            tmp_path / "extracted",
            "macos-arm64-cp311",
        )

    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    ("manifest_id", "identity_id"),
    [
        (None, None),
        ("g" * 64, "g" * 64),
        ("a" * 64, None),
        ("a" * 64, "b" * 64),
    ],
)
def test_candidate_identity_requires_one_matching_sha256_build_id(
    tmp_path, manifest_id, identity_id
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "platform": "macos-arm64-cp311",
                "python": "3.11",
                "schema_version": 1,
                "build_id": manifest_id,
            }
        )
    )
    (bundle / "build-identity.json").write_text(
        json.dumps(
            {
                "platform": "macos-arm64-cp311",
                "python": "3.11",
                "schema_version": 1,
                "build_id": identity_id,
                "validation": "not_assessed",
            }
        )
    )

    with pytest.raises(acceptance.AcceptanceError, match="identity"):
        acceptance._candidate_build_id(bundle, "macos-arm64-cp311")


def test_launcher_is_stopped_when_private_key_loading_fails(tmp_path, monkeypatch):
    class Reservation:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def bind(self, _):
            return None

        def getsockname(self):
            return ("127.0.0.1", 43210)

    process = object()

    class Tree:
        pass

    tree = Tree()
    tree.process = process
    stopped = []

    def invalid_key(_):
        raise acceptance.AcceptanceError("invalid generated key")

    monkeypatch.setattr(acceptance.socket, "socket", lambda *_: Reservation())
    monkeypatch.setattr(acceptance, "_start_tree", lambda *_, **__: tree)
    monkeypatch.setattr(acceptance, "_generated_key", invalid_key)
    monkeypatch.setattr(
        acceptance,
        "_stop_launcher",
        lambda candidate: stopped.append(candidate) or (0, ""),
    )

    with pytest.raises(acceptance.AcceptanceError, match="invalid generated key"):
        acceptance._exercise_launcher(tmp_path, tmp_path / "workspace", {}, (tmp_path,))

    assert stopped == [tree]


def test_timed_out_command_stops_its_real_child(tmp_path):
    heartbeat = tmp_path / "child-heartbeat"
    child = (
        "import pathlib,sys,time\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "while True:\n"
        "    p.write_text(str(time.monotonic()))\n"
        "    time.sleep(0.02)\n"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        "p=pathlib.Path(sys.argv[1]); "
        "subprocess.Popen([sys.executable, '-I', '-c', sys.argv[2], str(p)]); "
        "deadline=time.monotonic()+5; "
        'exec("while not p.exists() and time.monotonic() < deadline: time.sleep(0.01)"); '
        "time.sleep(60)"
    )

    with pytest.raises(acceptance.AcceptanceError, match="timed out"):
        acceptance._run(
            "real child timeout",
            [sys.executable, "-I", "-c", parent, str(heartbeat), child],
            cwd=tmp_path,
            environment=acceptance._clean_environment(tmp_path / "process-tmp"),
            timeout=1,
        )

    assert heartbeat.is_file()
    time.sleep(0.2)
    stopped_value = heartbeat.read_text()
    time.sleep(0.3)
    assert heartbeat.read_text() == stopped_value


def test_command_output_is_drained_and_kept_to_a_bounded_tail(tmp_path):
    result = acceptance._run(
        "bounded output",
        [sys.executable, "-I", "-c", "import sys; sys.stdout.write('x' * 2_000_000)"],
        cwd=tmp_path,
        environment=acceptance._clean_environment(tmp_path / "process-tmp"),
        timeout=10,
    )

    assert result.stdout == "x" * acceptance.MAX_LOG_CHARS


def test_launcher_cleanup_reaches_child_after_direct_parent_exits(tmp_path):
    heartbeat = tmp_path / "launcher-child-heartbeat"
    child = (
        "import pathlib,sys,time\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "while True:\n"
        "    p.write_text(str(time.monotonic()))\n"
        "    time.sleep(0.02)\n"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        "p=pathlib.Path(sys.argv[1]); "
        "subprocess.Popen([sys.executable, '-I', '-c', sys.argv[2], str(p)]); "
        "deadline=time.monotonic()+5; "
        'exec("while not p.exists() and time.monotonic() < deadline: time.sleep(0.01)")'
    )
    tree = acceptance._start_tree(
        [sys.executable, "-I", "-c", parent, str(heartbeat), child],
        cwd=tmp_path,
        environment=acceptance._clean_environment(tmp_path / "process-tmp"),
    )
    tree.process.wait(timeout=5)

    with pytest.raises(acceptance.AcceptanceError, match="forced process-tree"):
        acceptance._stop_launcher(tree)

    assert heartbeat.is_file()
    time.sleep(0.2)
    stopped_value = heartbeat.read_text()
    time.sleep(0.3)
    assert heartbeat.read_text() == stopped_value


def test_main_redacts_known_paths_from_top_level_failures(tmp_path, monkeypatch, capsys):
    source = tmp_path / "private-source"
    wheelhouse = tmp_path / "private-wheelhouse"
    wheel = tmp_path / "opsgraph-private.whl"
    output = tmp_path / "private-output.zip"
    source.mkdir()
    wheelhouse.mkdir()
    wheel.write_bytes(b"wheel")

    def fail(*_, **__):
        raise FileNotFoundError(str(output.with_suffix(".zip.sha256")))

    monkeypatch.setattr(acceptance, "accept", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci_bundle_acceptance.py",
            "--platform",
            "macos-arm64-cp311",
            "--source",
            str(source),
            "--wheel",
            str(wheel),
            "--wheelhouse",
            str(wheelhouse),
            "--output",
            str(output),
        ],
    )

    with pytest.raises(SystemExit) as error:
        acceptance.main()

    assert error.value.code == 1
    assert str(tmp_path) not in capsys.readouterr().err


@pytest.mark.parametrize("target", ["--help", "unsupported", "ubuntu-x64-cp311 --help"])
def test_accept_rejects_noncanonical_target_before_external_work(target, tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "_validate_host", lambda _: pytest.fail("invalid target"))
    with pytest.raises(acceptance.AcceptanceError, match="unsupported platform target"):
        acceptance.accept(tmp_path, tmp_path, tmp_path, tmp_path, target)
