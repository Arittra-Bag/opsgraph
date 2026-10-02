"""Build and exercise one native CPython 3.11 offline release bundle.

This is hosted-runner packaging/lifecycle acceptance. It deliberately makes no
PostgreSQL or model request and cannot certify Windows 11 from Windows Server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TextIO

TARGETS = {
    "macos-arm64-cp311": ("darwin", {"arm64", "aarch64"}),
    "windows-x64-cp311": ("win32", {"amd64", "x86_64"}),
    "ubuntu-x64-cp311": ("linux", {"amd64", "x86_64"}),
}
MAX_LOG_CHARS = 8_000
MAX_EXTRACTED_BYTES = 2 * 1024**3
MAX_ARCHIVE_MEMBERS = 20_000
SAFE_INHERITED_ENVIRONMENT = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    # CPython 3.11 uses these OS values for platform.machine() on Windows.
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_ARCHITEW6432",
    "SYSTEMROOT",
    "WINDIR",
}
WINDOWS_JOB_BOOTSTRAP = (
    "import json,subprocess,sys; "
    "gate=sys.stdin.read(1); "
    "command=json.loads(sys.argv[1]); supplied=json.loads(sys.argv[2]); "
    "kwargs=({'stdin':subprocess.DEVNULL} if supplied is None else "
    "{'input':supplied,'text':True}); "
    "raise SystemExit(125 if gate != 'G' else subprocess.run(command, **kwargs).returncode)"
)


class AcceptanceError(RuntimeError):
    """A bounded, credential-free failure suitable for a public CI log."""


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            "launcher redirects are not accepted",
            headers,
            file_pointer,
        )


class _WindowsJob:
    """Kill-on-close Job Object retaining descendants after their parent exits."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        if os.name != "nt":
            raise AcceptanceError("Windows process containment is unavailable on this host")
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise AcceptanceError("could not create Windows subprocess containment")
        self._kernel32 = kernel32
        self._handle = handle
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            handle,
            wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
        )
        if not assigned:
            kernel32.CloseHandle(handle)
            self._handle = None
            raise AcceptanceError("could not contain the Windows subprocess tree")

    def terminate(self) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise AcceptanceError("Windows subprocess-tree termination failed")

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._kernel32.CloseHandle(handle):
                raise AcceptanceError("Windows subprocess containment could not be closed")


@dataclass
class _OutputTail:
    value: str = ""

    def drain(self, stream: TextIO) -> None:
        try:
            while chunk := stream.read(16_384):
                self.value = (self.value + chunk)[-MAX_LOG_CHARS:]
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass


@dataclass
class _ProcessTree:
    process: subprocess.Popen[str]
    job: _WindowsJob | None
    output: _OutputTail
    reader: threading.Thread
    output_fd: int


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _clean_environment(temporary: Path | None = None) -> dict[str, str]:
    """Pass only OS process essentials; never forward CI, cloud, app or database secrets."""
    environment: dict[str, str] = {}
    for name, value in os.environ.items():
        canonical = name.upper()
        if (os.name == "nt" and canonical in SAFE_INHERITED_ENVIRONMENT) or (
            os.name != "nt" and name in SAFE_INHERITED_ENVIRONMENT
        ):
            environment[canonical] = value
    environment.update(
        {
            "LANGSMITH_TRACING": "false",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    if temporary is not None:
        temporary.mkdir(mode=0o700, exist_ok=True)
        environment.update(
            {
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "TMPDIR": str(temporary),
            }
        )
    return environment


def _sanitized(
    text: str,
    private_paths: tuple[Path, ...] = (),
    private_values: tuple[str, ...] = (),
) -> str:
    clean = text
    for value in sorted((value for value in private_values if value), key=len, reverse=True):
        clean = clean.replace(value, "<redacted>")
    for path in sorted((str(path) for path in private_paths), key=len, reverse=True):
        clean = clean.replace(path, "<temporary-path>")
        clean = clean.replace(path.replace("\\", "/"), "<temporary-path>")
    clean = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1<credentials>@", clean)
    assignment = (
        r"(?i)([\"']?)("
        r"[A-Z0-9_-]*(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS?|DSN)[A-Z0-9_-]*"
        r"|DATABASE_URL"
        r")\1(\s*[=:]\s*)"
        r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;}\]]+)"
    )
    clean = re.sub(assignment, r"\1\2\1\3<redacted>", clean)
    return clean[-MAX_LOG_CHARS:]


def _start_tree(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    input_text: str | None = None,
) -> _ProcessTree:
    options: dict[str, object] = {"start_new_session": True}
    invoked_arguments = arguments
    stdin_required = input_text is not None
    if os.name == "nt":
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        # The trusted wrapper blocks on a one-byte gate until its Job Object is
        # assigned, closing the otherwise unavoidable CreateProcess/Assign race.
        invoked_arguments = [
            sys.executable,
            "-I",
            "-c",
            WINDOWS_JOB_BOOTSTRAP,
            json.dumps(arguments),
            json.dumps(input_text),
        ]
        stdin_required = True
    process = subprocess.Popen(  # noqa: S603
        invoked_arguments,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE if stdin_required else subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **options,
    )
    job: _WindowsJob | None = None
    try:
        if os.name == "nt":
            job = _WindowsJob(process)
        if process.stdout is None:
            raise AcceptanceError("subprocess output containment could not be created")
        output_fd = process.stdout.fileno()
        output = _OutputTail()
        reader = threading.Thread(
            target=output.drain,
            args=(process.stdout,),
            name="bundle-acceptance-output",
            daemon=True,
        )
        reader.start()
        supplied_input = "G" if os.name == "nt" else input_text
        if supplied_input is not None:
            if process.stdin is None:
                raise AcceptanceError("subprocess input containment could not be created")
            try:
                process.stdin.write(supplied_input)
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        return _ProcessTree(process, job, output, reader, output_fd)
    except BaseException:
        if job is not None:
            try:
                job.terminate()
            finally:
                job.close()
        elif os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        raise


def _terminate_tree(tree: _ProcessTree) -> None:
    """Kill the retained tree even when its direct parent has already exited."""
    process = tree.process
    if os.name == "nt":
        if tree.job is None:
            if process.poll() is None:
                process.kill()
            raise AcceptanceError("Windows subprocess tree was not contained")
        try:
            tree.job.terminate()
        except AcceptanceError:
            if process.poll() is None:
                process.kill()
            raise
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()
        raise AcceptanceError("POSIX subprocess-group cleanup failed") from None


def _cleanup_tree(tree: _ProcessTree) -> None:
    """Terminate descendants, reap the leader and finish the bounded output drain."""
    failure: AcceptanceError | None = None
    try:
        _terminate_tree(tree)
    except AcceptanceError as error:
        failure = error
    try:
        if tree.process.poll() is None:
            tree.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        tree.process.kill()
        try:
            tree.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            failure = AcceptanceError("subprocess leader could not be reaped")
    if tree.job is not None:
        try:
            tree.job.close()
        except AcceptanceError as error:
            failure = failure or error
    tree.reader.join(timeout=5)
    if tree.reader.is_alive():
        try:
            os.close(tree.output_fd)
        except OSError:
            pass
        tree.reader.join(timeout=1)
    if tree.reader.is_alive():
        failure = failure or AcceptanceError("subprocess output pipe could not be closed")
    if failure is not None:
        raise failure


def _run(
    label: str,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    private_paths: tuple[Path, ...] = (),
    input_text: str | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    tree = _start_tree(
        arguments,
        cwd=cwd,
        environment=environment,
        input_text=input_text,
    )
    try:
        tree.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _cleanup_tree(tree)
        detail = _sanitized(tree.output.value, private_paths)
        raise AcceptanceError(f"{label} timed out" + (f"\n{detail}" if detail else "")) from None
    except BaseException:
        _cleanup_tree(tree)
        raise
    returncode = tree.process.returncode
    _cleanup_tree(tree)
    output = tree.output.value
    if returncode:
        detail = _sanitized(output.strip(), private_paths)
        raise AcceptanceError(
            f"{label} failed with exit code {returncode}" + (f"\n{detail}" if detail else "")
        )
    print(f"[bundle-acceptance] PASS {label}", flush=True)
    return subprocess.CompletedProcess(arguments, returncode, output, "")


def _os_release() -> dict[str, str]:
    values = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            values[name] = value.strip().strip('"')
    return values


def _windows_product() -> str:
    import winreg  # type: ignore[import-not-found]  # Windows-only standard library.

    path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
        return str(winreg.QueryValueEx(key, "ProductName")[0])


def _validate_host(target: str) -> str:
    expected_system, expected_machines = TARGETS[target]
    machine = platform.machine().lower()
    if (
        sys.version_info[:2] != (3, 11)
        or platform.python_implementation() != "CPython"
        or struct.calcsize("P") != 8
        or sys.platform != expected_system
        or machine not in expected_machines
    ):
        raise AcceptanceError(
            f"{target} requires native CPython 3.11 on "
            f"{expected_system}/{sorted(expected_machines)}"
        )
    if target == "ubuntu-x64-cp311":
        release = _os_release()
        if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
            raise AcceptanceError("ubuntu-x64-cp311 acceptance requires native Ubuntu 24.04")
        return "Ubuntu 24.04 x64"
    if target == "macos-arm64-cp311":
        version = platform.mac_ver()[0]
        if version.split(".", 1)[0] != "26":
            raise AcceptanceError("macos-arm64-cp311 acceptance requires native macOS 26")
        return f"macOS {version} arm64"
    product = _windows_product()
    if "Windows Server 2025" not in product:
        raise AcceptanceError("windows-x64-cp311 acceptance requires Windows Server 2025")
    return "Windows Server 2025 x64"


def _resolve_wheel(source: Path, supplied: Path | None) -> Path:
    wheels = [supplied] if supplied else list((source / "dist").glob("opsgraph-*.whl"))
    if len(wheels) != 1 or wheels[0] is None:
        raise AcceptanceError("provide one candidate wheel or leave exactly one in dist")
    wheel = wheels[0].resolve()
    if not wheel.is_file() or not re.fullmatch(r"opsgraph-[^/\\]+\.whl", wheel.name):
        raise AcceptanceError("candidate wheel must be a regular opsgraph-*.whl file")
    return wheel


def _verify_checksum(candidate: Path) -> str:
    sidecar = candidate.with_suffix(candidate.suffix + ".sha256")
    lines = sidecar.read_text(encoding="ascii").splitlines()
    actual = _sha256(candidate)
    if len(lines) != 1 or lines[0].split("  ") != [actual, candidate.name]:
        raise AcceptanceError("candidate SHA-256 sidecar does not match the built archive")
    return actual


def _extract(candidate: Path, destination: Path, target: str) -> Path:
    prefix = f"opsgraph-{target}"
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(candidate) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise AcceptanceError("candidate has an invalid number of ZIP members")
        for member in members:
            name = member.filename
            parts = PurePosixPath(name).parts
            mode = member.external_attr >> 16
            if (
                not parts
                or parts[0] != prefix
                or name != "/".join(parts)
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in name
                or ":" in name
                or name in seen
                or member.is_dir()
                or member.flag_bits & 1
                or (mode and not stat.S_ISREG(mode))
            ):
                raise AcceptanceError("candidate contains an unsafe or duplicate ZIP member")
            seen.add(name)
            total += member.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise AcceptanceError("candidate exceeds the bounded extracted-size limit")
            output = destination.joinpath(*parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, output.open("xb") as sink:
                shutil.copyfileobj(source, sink)
            if os.name == "posix":
                output.chmod(mode & 0o777)
    root = destination / prefix
    if not seen or not root.is_dir():
        raise AcceptanceError("candidate did not extract to its expected platform directory")
    return root


def _candidate_build_id(bundle: Path, target: str) -> str:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    identity = json.loads((bundle / "build-identity.json").read_text(encoding="utf-8"))
    build_id = manifest.get("build_id") if isinstance(manifest, dict) else None
    unsigned_identity = dict(identity) if isinstance(identity, dict) else {}
    identity_build_id = unsigned_identity.pop("build_id", None)
    calculated_build_id = hashlib.sha256(
        (json.dumps(unsigned_identity, sort_keys=True, indent=2) + "\n").encode()
    ).hexdigest()
    if (
        not isinstance(build_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", build_id) is None
        or manifest.get("schema_version") != 1
        or manifest.get("platform") != target
        or manifest.get("python") != "3.11"
        or not isinstance(identity, dict)
        or identity.get("schema_version") != 1
        or identity_build_id != build_id
        or calculated_build_id != build_id
        or identity.get("platform") != target
        or identity.get("python") != "3.11"
        or identity.get("validation") != "not_assessed"
    ):
        raise AcceptanceError("extracted candidate identity does not match the requested target")
    return build_id


def _write_receipt(
    path: Path,
    *,
    target: str,
    host: str,
    identity: dict[str, object],
    build_id: str,
    archive: Path,
    archive_hash: str,
    application_wheel_hash: str,
) -> None:
    """Write one immutable, public, secret-free native acceptance receipt."""

    source_commit = identity.get("source_base_commit")
    source_inventory = identity.get("source_inventory_sha256")
    application_wheel = identity.get("wheel")
    identity_wheel_hash = (
        application_wheel.get("sha256") if isinstance(application_wheel, dict) else None
    )
    if (
        target not in TARGETS
        or not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or not isinstance(source_inventory, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_inventory) is None
        or not isinstance(identity_wheel_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", identity_wheel_hash) is None
        or re.fullmatch(r"[0-9a-f]{64}", application_wheel_hash) is None
        or identity_wheel_hash != application_wheel_hash
        or re.fullmatch(r"[0-9a-f]{64}", build_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", archive_hash) is None
        or archive.name != archive.name.replace("/", "").replace("\\", "")
    ):
        raise AcceptanceError("candidate identity cannot produce an acceptance receipt")
    record = {
        "schema_version": 1,
        "result": "pass",
        "validated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "host": host,
        "host_system": platform.system(),
        "host_release": platform.release(),
        "host_machine": platform.machine(),
        "python": platform.python_version(),
        "source_base_commit": source_commit,
        "source_inventory_sha256": source_inventory,
        "application_wheel_sha256": application_wheel_hash,
        "build_id": build_id,
        "archive": archive.name,
        "archive_sha256": archive_hash,
        "boundary": "native bundle lifecycle only; no PostgreSQL or model call was made",
    }
    with path.open("x", encoding="utf-8", newline="\n") as stream:  # NOSONAR - CI output
        json.dump(record, stream, sort_keys=True, indent=2)
        stream.write("\n")


def _request_json(url: str, *, key: str | None = None) -> object:
    headers = {"Accept": "application/json"}
    if key:
        headers["X-OpsGraph-Key"] = key
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirect())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=3) as response:  # noqa: S310
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise AcceptanceError("launcher response exceeded the acceptance limit")
    return json.loads(body)


def _generated_key(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPSGRAPH_API_KEY="):
            key = line.partition("=")[2]
            if len(key) >= 2 and key[0] == key[-1] == "'":
                key = key[1:-1]
            if len(key) >= 24 and re.fullmatch(r"[A-Za-z0-9_-]+", key):
                return key
    raise AcceptanceError("configuration bootstrap did not create a valid private workspace key")


def _stop_launcher(tree: _ProcessTree) -> tuple[int, str]:
    process = tree.process
    forced = False
    timed_out = False
    if process.poll() is None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.killpg(process.pid, signal.SIGINT)
        except (OSError, ValueError):
            forced = True
        if not forced:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                forced = True
                timed_out = True
    else:
        process.wait()
    if not forced:
        tree.reader.join(timeout=1)
        if tree.reader.is_alive():
            forced = True
    returncode = process.returncode
    _cleanup_tree(tree)
    if timed_out:
        raise AcceptanceError("launcher did not stop within 30 seconds") from None
    if forced:
        raise AcceptanceError("launcher required forced process-tree termination")
    if returncode is None:
        raise AcceptanceError("launcher leader did not provide an exit status")
    return returncode, tree.output.value


def _exercise_launcher(
    bundle: Path,
    workspace: Path,
    environment: dict[str, str],
    private_paths: tuple[Path, ...],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    arguments = [
        sys.executable,
        "-I",
        str(bundle / "Install.py"),
        "launch",
        "--directory",
        str(workspace),
        "--port",
        str(port),
        "--no-browser",
    ]
    tree = _start_tree(
        arguments,
        cwd=bundle,
        environment=environment,
    )
    process = tree.process
    failure: str | None = None
    key: str | None = None
    try:
        key = _generated_key(workspace / ".env")
        deadline = time.monotonic() + 45
        health: object | None = None
        while time.monotonic() < deadline and process.poll() is None:
            try:
                health = _request_json(f"http://127.0.0.1:{port}/api/health")
                break
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                time.sleep(0.2)
        if not isinstance(health, dict):
            failure = "launcher did not expose loopback health within 45 seconds"
        elif (
            health.get("ok") is not True
            or health.get("mode") != "connected"
            or health.get("model") != "openai_compatible"
            or health.get("egress") is not False
        ):
            failure = "launcher health did not retain connected, local-only configuration"
        else:
            sources = _request_json(f"http://127.0.0.1:{port}/api/sources", key=key)
            if sources != []:
                failure = "credential-free workspace unexpectedly contained configured sources"
    finally:
        returncode, output = _stop_launcher(tree)
    allowed = {0, 130, -signal.SIGINT, -1073741510, 3221225786}
    if failure or returncode not in allowed:
        detail = _sanitized(output, private_paths, (key,) if key else ())
        reason = failure or f"launcher stopped with unexpected exit code {returncode}"
        raise AcceptanceError(reason + (f"\n{detail}" if detail else ""))
    print(
        "[bundle-acceptance] PASS credential-free loopback launcher start and controlled stop",
        flush=True,
    )


def _validate_restored_config(
    python: Path,
    workspace: Path,
    restored: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    private_paths: tuple[Path, ...],
) -> None:
    program = (
        "import sys; from pathlib import Path; "
        "from opsgraph.setup import read_private_config; "
        "a=read_private_config(Path(sys.argv[1]) / '.env'); "
        "b=read_private_config(Path(sys.argv[2]) / '.env'); "
        "assert a['OPSGRAPH_API_KEY'] == b['OPSGRAPH_API_KEY']; "
        "assert b['OPSGRAPH_STATE_PATH'] == str(Path(sys.argv[2]) / '.opsgraph' / 'state.db')"
    )
    _run(
        "restored private configuration and relocated state",
        [str(python), "-I", "-c", program, str(workspace), str(restored)],
        cwd=cwd,
        environment=environment,
        private_paths=private_paths,
        timeout=60,
    )


def accept(
    source: Path,
    wheel: Path,
    wheelhouse: Path,
    output: Path,
    target: str,
    *,
    receipt: Path | None = None,
    error_paths: list[Path] | None = None,
) -> None:
    # Enforce the same target boundary for programmatic callers as for the CLI.
    if target not in TARGETS:
        raise AcceptanceError("unsupported platform target")
    host = _validate_host(target)
    if not wheelhouse.is_dir() or not any(wheelhouse.glob("*.whl")):
        raise AcceptanceError("wheelhouse must contain host-selected locked dependency wheels")
    output.parent.mkdir(parents=True, exist_ok=True)
    if receipt is not None:
        receipt = receipt.absolute()
        if receipt.exists():
            raise AcceptanceError("acceptance receipt already exists")
        receipt.parent.mkdir(parents=True, exist_ok=True)
    # macOS exposes /var and /tmp through symlinks. Maintenance correctly rejects
    # those ancestors, so keep private lifecycle fixtures beside the real checkout.
    with tempfile.TemporaryDirectory(
        prefix="opsgraph-bundle-acceptance-", dir=source.parent
    ) as temporary:
        private_root = Path(temporary)
        if error_paths is not None:
            error_paths.append(private_root)
        private_paths = (
            private_root,
            source,
            wheel,
            wheelhouse,
            output,
            output.with_suffix(output.suffix + ".sha256"),
        )
        environment = _clean_environment(private_root / "process-tmp")
        _run(
            "source-authenticated platform bundle build",
            [
                sys.executable,
                str(source / "scripts" / "build_release.py"),
                "--source",
                str(source),
                "--wheel",
                str(wheel),
                "--wheelhouse",
                str(wheelhouse),
                "--platform",
                target,
                "--output",
                str(output),
            ],
            cwd=source,
            environment=environment,
            private_paths=private_paths,
        )
        archive_hash = _verify_checksum(output)
        bundle = _extract(output, private_root / "extracted", target)
        build_id = _candidate_build_id(bundle, target)
        print(
            "[bundle-acceptance] PASS candidate checksum, identity, and safe extraction",
            flush=True,
        )

        _run(
            "offline Install.py dependency and application installation",
            [sys.executable, "-I", str(bundle / "Install.py"), "install"],
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
        )
        runtime_python = (
            bundle / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        executable = (
            bundle / ".venv" / ("Scripts/opsgraph.exe" if os.name == "nt" else "bin/opsgraph")
        )
        _run(
            "installed environment pip check",
            [str(runtime_python), "-I", "-m", "pip", "check"],
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
            timeout=120,
        )

        workspace = private_root / "workspace"
        workspace.mkdir(mode=0o700)
        setup_program = (
            "import sys; from pathlib import Path; from opsgraph.setup import run_setup; "
            "raise SystemExit(run_setup(Path(sys.argv[1]), input_fn=lambda _:'', "
            "secret_fn=lambda _:'', output_fn=lambda _:None))"
        )
        _run(
            "credential-free guided configuration",
            [str(runtime_python), "-I", "-c", setup_program, str(workspace)],
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
            timeout=60,
        )
        _exercise_launcher(bundle, workspace, environment, private_paths)

        backup = private_root / "backup"
        restored = private_root / "restored"
        _run(
            "stopped workspace backup",
            [str(executable), "backup", "--directory", str(workspace), "--output", str(backup)],
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
            timeout=120,
        )
        _run(
            "backup restore into a new workspace",
            [str(executable), "restore", "--backup", str(backup), "--directory", str(restored)],
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
            timeout=120,
        )
        _validate_restored_config(
            runtime_python,
            workspace,
            restored,
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
        )
        _run(
            "runtime-only uninstall",
            [sys.executable, "-I", str(bundle / "Install.py"), "uninstall"],
            cwd=bundle,
            environment=environment,
            private_paths=private_paths,
            input_text="REMOVE\n",
            timeout=120,
        )
        retained = (
            not (bundle / ".venv").exists()
            and (bundle / "manifest.json").is_file()
            and (workspace / ".env").is_file()
            and (workspace / ".opsgraph" / "state.db").is_file()
            and (backup / "manifest.json").is_file()
            and (restored / ".env").is_file()
            and (restored / ".opsgraph" / "state.db").is_file()
        )
        if not retained:
            raise AcceptanceError(
                "uninstall did not preserve the bundle and private workspace fixtures"
            )
        print(
            "[bundle-acceptance] PASS uninstall scope and retained workspace/backup/restore data",
            flush=True,
        )

        print(
            f"[bundle-acceptance] RESULT PASS: {host}, CPython {platform.python_version()}",
            flush=True,
        )
        print(f"[bundle-acceptance] Target: {target}", flush=True)
        print(f"[bundle-acceptance] Build ID: {build_id}", flush=True)
        print(f"[bundle-acceptance] ZIP SHA-256: {archive_hash}", flush=True)
        print(
            "[bundle-acceptance] Boundary: no PostgreSQL or model call was made; "
            "hosted Windows Server evidence does not certify Windows 11.",
            flush=True,
        )
        if receipt is not None:
            identity = json.loads((bundle / "build-identity.json").read_text(encoding="utf-8"))
            _write_receipt(
                receipt,
                target=target,
                host=host,
                identity=identity,
                build_id=build_id,
                archive=output,
                archive_hash=archive_hash,
                application_wheel_hash=_sha256(wheel),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=TARGETS, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    error_paths = [
        Path.cwd().absolute(),
        args.source.absolute(),
        args.wheelhouse.absolute(),
        args.output.absolute(),
    ]
    if args.wheel is not None:
        error_paths.append(args.wheel.absolute())
    if args.receipt is not None:
        error_paths.append(args.receipt.absolute())
    try:
        source = args.source.resolve(strict=True)
        wheel = _resolve_wheel(source, args.wheel)
        wheelhouse = args.wheelhouse.resolve(strict=True)
        output = args.output.absolute()
        accept(
            source,
            wheel,
            wheelhouse,
            output,
            args.platform,
            receipt=args.receipt,
            error_paths=error_paths,
        )
    except (
        AcceptanceError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        zipfile.BadZipFile,
    ) as error:
        parser.exit(1, f"[bundle-acceptance] FAIL {_sanitized(str(error), tuple(error_paths))}\n")


if __name__ == "__main__":
    main()
