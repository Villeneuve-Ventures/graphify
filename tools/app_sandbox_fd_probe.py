"""Opt-in macOS App Sandbox FD-export diagnostic on new synthetic files only.

This tool builds and ad-hoc signs a task-local sender, not an installed backend.
It changes no accounts, launchers, graph state, or system policy. The controller,
broker and outside receiver remain unsandboxed; only the native sender enters
App Sandbox. A completed counterexample exits 3, a bounded blocked observation
exits 0, and missing prerequisites or invalid evidence exit 2. No outcome is
native containment qualification. Fixtures and all evidence remain for review.
"""
from __future__ import annotations

import argparse
import array
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import traceback
from typing import Any, BinaryIO
import uuid

TIMEOUT = 10.0
MAX_OUTPUT = 64 * 1024
SCRIPT = Path(__file__).resolve()
SOURCE = SCRIPT.parent / "fixtures/app_sandbox_fd_sender.c"
REPOSITORY = SCRIPT.parent.parent
DENIALS = {errno.EACCES, errno.EPERM}


class ProbeError(RuntimeError):
    """A missing prerequisite or invalid diagnostic; never a containment pass."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_verifier(fd: int) -> bytes:
    _require(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
             "verifier descriptor must be read-only")
    return os.pread(fd, 128, 0)


def validate_output_root(value: Path) -> Path:
    """Resolve a fresh external fixture location without creating or changing it.

    Existing destinations, graphify-out ancestry and source-checkout overlap
    refuse with ProbeError. Parent resolution handles macOS /var aliases.
    """
    _require(value.is_absolute(), "absolute output root required")
    parent = value.parent.resolve(strict=True)
    target = parent / value.name
    _require(not os.path.lexists(value) and not os.path.lexists(target), "output root must be absent")
    _require("graphify-out" not in target.parts, "graph output cannot be a probe fixture")
    _require(not target.is_relative_to(REPOSITORY) and not REPOSITORY.is_relative_to(target),
             "probe output must be external to the source checkout")
    return target


def entitlements_for(allowed: Path) -> dict[str, Any]:
    """Return the narrow diagnostic sandbox grant; no inheritance-only entry or network grant."""
    return {"com.apple.security.app-sandbox": True,
            "com.apple.security.temporary-exception.files.absolute-path.read-write":
                [str(allowed.resolve(strict=True)) + "/"]}


def classify_case(case: dict[str, Any], *, sandboxed: bool) -> str:
    """Validate recorded ordering and effects, then classify this one diagnostic.

    The records are evidence inputs, not authority. Missing actor retirement,
    wrong file identity, invalid controls or contradictory effects raise
    ProbeError. A blocked observation is limited to this exact route/profile.
    """
    required_fields = {"sender_ready", "receiver_received", "receiver_write", "spawned", "receiver_ready",
                       "controller_pid", "coordinator_reaped", "broker_returncode", "receiver_returncode",
                       "order", "canary_identity", "sender_sent", "marker_length", "marker_observed",
                       "after_sha256", "before_gate_sha256", "canary_mode_at_transfer", "verifier_read_only",
                       "verifier_access_mode", "cleanup"}
    _require(isinstance(case, dict) and required_fields <= case.keys(), "incomplete case evidence")
    ready, received, write = case["sender_ready"], case["receiver_received"], case["receiver_write"]
    spawned, receiver = case["spawned"], case["receiver_ready"]
    _require(ready["pid"] == spawned["coordinator_pid"] and ready["ppid"] == spawned["broker_pid"]
             and ready["pgid"] == spawned["broker_pid"], "sender ancestry differs")
    _require(receiver["pid"] != spawned["broker_pid"] and receiver["ppid"] == case["controller_pid"]
             and receiver["outside_errno"] == 0, "outside receiver control failed")
    _require(case["verifier_read_only"] is True and case["verifier_access_mode"] == os.O_RDONLY
             and case["canary_mode_at_transfer"] == 0
             and received["pathname_errno"] in DENIALS, "pathname denial or read-only verification missing")
    _require(ready["allowed_write"] is True and ready["allowed_errno"] == 0,
             "authorized sender write failed")
    expected_outside = ready["outside_errno"] in DENIALS if sandboxed else ready["outside_errno"] == 0
    _require(expected_outside, "functional sandbox/control evidence missing")
    _require(case["coordinator_reaped"]["coordinator_pid"] == ready["pid"]
             and case["coordinator_reaped"]["returncode"] == -signal.SIGKILL
             and case["broker_returncode"] == -signal.SIGKILL
             and case["receiver_returncode"] == 0, "exact actor retirement missing")
    cleanup = case["cleanup"]
    _require(isinstance(cleanup, list) and len(cleanup) == 3
             and all(isinstance(item, dict) and "error" not in item for item in cleanup),
             "actor cleanup incomplete or uncertain")
    retired = {item.get("role"): item for item in cleanup}
    _require(set(retired) == {"broker", "receiver", "coordinator"}, "actor cleanup roles differ")
    _require(retired["broker"].get("pid") == spawned["broker_pid"]
             and retired["broker"].get("returncode") == case["broker_returncode"]
             and retired["receiver"].get("pid") == receiver["pid"]
             and retired["receiver"].get("returncode") == case["receiver_returncode"]
             and retired["coordinator"].get("pid") == ready["pid"]
             and retired["coordinator"].get("returncode") == -signal.SIGKILL,
             "actor cleanup identity or wait result differs")
    order = case["order"]
    required = ["canary_pathname_revoked", "coordinator_reaped", "broker_reaped",
                "receiver_gate_released", "receiver_write", "observation_recorded", "canary_mode_restored"]
    _require(all(order.count(name) == 1 for name in required)
             and [order.index(name) for name in required] == sorted(order.index(name) for name in required),
             "receiver write preceded exact actor retirement")
    identity = case["canary_identity"]
    _require([ready["device"], ready["inode"]] == identity, "sender selected another file")
    sent = case["sender_sent"]
    if sent["sent"] is True:
        _require(sent["errno"] == 0 and received["received"] is True
                 and [received["device"], received["inode"]] == identity
                 and received["access_mode"] == os.O_RDWR,
                 "FD transfer evidence differs from the selected canary")
        if write["wrote"] is True:
            _require(write["errno"] == 0 and 0 < write["count"] <= case["marker_length"]
                     and case["marker_observed"] is True
                     and case["after_sha256"] != case["before_gate_sha256"],
                     "receiver write effect is unproved")
            return "fd-write-after-death"
        _require(write["errno"] in DENIALS and case["after_sha256"] == case["before_gate_sha256"],
                 "post-death write failed for an unrelated reason")
        return "write-blocked-observation"
    _require(sent["sent"] is False and sent["errno"] in DENIALS
             and received["received"] is False and write["wrote"] is False
             and case["marker_observed"] is False
             and case["after_sha256"] == case["before_gate_sha256"],
             "FD export failed without valid denial evidence")
    return "export-blocked-observation"


class _Reader:
    def __init__(self, stream, raw: list[dict[str, Any]], role: str):
        self.stream, self.raw, self.role = stream, raw, role
        self.buffer = b""

    def event(self) -> dict[str, Any]:
        deadline = time.monotonic() + TIMEOUT
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            _require(remaining > 0 and bool(select.select([self.stream], [], [], max(0, remaining))[0]),
                     self.role + " event deadline")
            data = os.read(self.stream.fileno(), 4096)
            _require(bool(data), self.role + " ended before expected event")
            self.buffer += data
            _require(len(self.buffer) <= MAX_OUTPUT, self.role + " output exceeds bound")
        line, self.buffer = self.buffer.split(b"\n", 1)
        value = json.loads(line)
        _require(isinstance(value, dict) and isinstance(value.get("event"), str), "malformed actor event")
        self.raw.append({"role": self.role, "record": value})
        return value


def _command(argv: list[str], commands: list[dict[str, Any]], env: dict[str, str]) -> bytes:
    """Record command intent and bounded diagnostics; timeout and launch errors propagate."""
    record: dict[str, Any] = {"argv": list(argv), "returncode": None,
                              "outcome": "started", "stdout": "", "stderr": ""}
    commands.append(record)

    def captured(value: bytes | str | None) -> str:
        data = value.encode() if isinstance(value, str) else value or b""
        return data[:MAX_OUTPUT].decode(errors="replace")

    try:
        result = subprocess.run(argv, capture_output=True, timeout=30, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        record.update(outcome="timed-out" if isinstance(exc, subprocess.TimeoutExpired) else "launch-failed",
                      error=repr(exc))
        if isinstance(exc, subprocess.TimeoutExpired):
            record.update(timeout=exc.timeout, stdout=captured(exc.stdout), stderr=captured(exc.stderr))
        raise
    record.update(returncode=result.returncode, outcome="completed",
                  stdout=captured(result.stdout), stderr=captured(result.stderr))
    _require(result.returncode == 0, "prerequisite command failed: " + argv[0])
    _require(len(result.stdout) <= MAX_OUTPUT and len(result.stderr) <= MAX_OUTPUT,
             "prerequisite command output exceeds bound")
    return result.stdout + b"\n" + result.stderr


def _child_env(root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GRAPHIFY_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_", "DYLD_"))
           and key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
           and not key.endswith(("_API_KEY", "_TOKEN"))}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", TMPDIR=str(root / "tmp"))
    return env


def _build(root: Path, allowed: Path, commands: list[dict[str, Any]], env: dict[str, str]) -> dict[str, Any]:
    clang = _command(["/usr/bin/xcrun", "--find", "clang"], commands, env).decode().strip()
    sdk = _command(["/usr/bin/xcrun", "--show-sdk-path"], commands, env).decode().strip()
    _require(Path(clang).is_absolute() and Path(sdk).is_absolute(), "absolute compiler and SDK required")
    unsigned = root / "unsigned-sender"
    _command([clang, "-isysroot", sdk, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O0",
              str(SOURCE), "-o", str(unsigned)], commands, env)
    token = uuid.uuid4().hex
    result: dict[str, Any] = {"sdk": sdk, "clang": clang, "unsigned_sha256": _sha(unsigned)}
    for label, sandboxed in (("control", False), ("sandbox", True)):
        identifier = "org.graphify.fdprobe." + label + "." + token
        app = root / (label + ".app")
        executable = app / "Contents/MacOS/sender"
        executable.parent.mkdir(parents=True)
        shutil.copyfile(unsigned, executable)
        executable.chmod(0o700)
        info = {"CFBundleIdentifier": identifier, "CFBundleExecutable": "sender",
                "CFBundleName": "Graphify FD Probe", "CFBundlePackageType": "APPL"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        entitlements = entitlements_for(allowed) if sandboxed else {}
        entitlements_path = root / (label + ".entitlements.plist")
        entitlements_path.write_bytes(plistlib.dumps(entitlements))
        container = Path.home() / "Library/Containers" / identifier
        _require(not os.path.lexists(container), "unique diagnostic container already exists")
        _command(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
                  "--generate-entitlement-der", "--entitlements", str(entitlements_path), str(app)], commands, env)
        _command(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(app)], commands, env)
        displayed = _command(["/usr/bin/codesign", "--display", "--entitlements", ":-", str(app)], commands, env)
        start, end = displayed.find(b"<?xml"), displayed.find(b"</plist>")
        _require(start >= 0 and end >= start, "signed entitlements are not readable")
        actual = plistlib.loads(displayed[start:end + len(b"</plist>")])
        _require(actual == entitlements, "signed entitlements differ from the exact diagnostic policy")
        result[label] = {"executable": str(executable), "bundle_id": identifier,
                         "signed_sha256": _sha(executable), "entitlements": actual,
                         "container": str(container), "container_existed_before": False}
    return result


def _broker(spec: dict[str, Any]) -> None:
    gate_read, gate_write = os.pipe()
    child = subprocess.Popen([spec["sender"], str(spec["socket_fd"]), str(gate_read),
                              spec["canary"], spec["denied"]],
                             stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                             pass_fds=(spec["socket_fd"], gate_read))
    os.close(gate_read)
    os.close(spec["socket_fd"])
    _emit({"event": "spawned", "broker_pid": os.getpid(), "coordinator_pid": child.pid})
    reaped = False
    try:
        command = sys.stdin.readline().strip()
        _require(command in {"go", "abort"}, "broker go/abort command required")
        if command == "go":
            os.write(gate_write, b"G")
            os.close(gate_write)
            gate_write = -1
            _require(sys.stdin.readline().strip() == "kill", "broker kill command required")
        child.kill()
        returncode = child.wait(timeout=TIMEOUT)
        _emit({"event": "coordinator_reaped", "coordinator_pid": child.pid, "returncode": returncode})
        reaped = True
        # The controller kills and reaps this exact broker after that acknowledgment.
        sys.stdin.read(1)
    finally:
        if not reaped:
            if child.returncode is None:
                child.kill()
            returncode = child.wait(timeout=TIMEOUT)
            _emit({"event": "coordinator_reaped", "coordinator_pid": child.pid, "returncode": returncode})
        if gate_write >= 0:
            os.close(gate_write)


def _receiver(spec: dict[str, Any]) -> None:
    outside = os.open(spec["denied"], os.O_WRONLY | os.O_NOFOLLOW)
    os.close(outside)
    _emit({"event": "receiver_ready", "pid": os.getpid(), "ppid": os.getppid(),
           "outside_errno": 0})
    channel = socket.socket(fileno=spec["socket_fd"])
    message, ancillary, flags, _ = channel.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize))
    _require(not flags & socket.MSG_CTRUNC, "descriptor message truncated")
    descriptors = array.array("i")
    for level, kind, data in ancillary:
        _require(level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS, "unexpected ancillary data")
        descriptors.frombytes(data)
    _require((message == b"F" and len(descriptors) == 1) or
             (message in {b"", b"N"} and not descriptors), "unexpected descriptor protocol")
    fd = descriptors[0] if descriptors else None
    info = os.fstat(fd) if fd is not None else None
    mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE if fd is not None else None
    if info is not None:
        _require(stat.S_ISREG(info.st_mode) and [info.st_dev, info.st_ino] == spec["canary_identity"]
                 and mode == os.O_RDWR, "received descriptor is not the selected writable canary")
    pathname_errno = 0
    try:
        reopened = os.open(spec["canary"], os.O_WRONLY | os.O_NOFOLLOW)
    except OSError as exc:
        pathname_errno = exc.errno or errno.EIO
    else:
        os.close(reopened)
    _require(pathname_errno in DENIALS, "receiver can reopen the protected canary by pathname")
    _emit({"event": "receiver_received", "received": fd is not None,
           "device": info.st_dev if info else None, "inode": info.st_ino if info else None,
           "access_mode": mode, "pathname_errno": pathname_errno})
    _require(os.read(spec["gate_fd"], 1) == b"W", "post-death write gate missing")
    count, error, sync_error = 0, 0, 0
    if fd is not None:
        try:
            count = os.pwrite(fd, spec["marker"].encode(), 0)
        except OSError as exc:
            error = exc.errno or errno.EIO
        finally:
            if count > 0:
                try:
                    os.fsync(fd)
                except OSError as exc:
                    sync_error = exc.errno or errno.EIO
            os.close(fd)
    _emit({"event": "receiver_write", "wrote": count > 0,
           "count": count, "errno": error, "sync_errno": sync_error})
    channel.close()


def _actor(role: str, spec: dict[str, Any], *, stderr: BinaryIO,
           pass_fds: tuple[int, ...], env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-I", "-B", str(SCRIPT), "--run-native",
                             "--_role", role, "--_spec", json.dumps(spec)],
                            stdin=subprocess.PIPE if role == "broker" else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=stderr, pass_fds=pass_fds,
                            start_new_session=True, env=env, bufsize=0, text=False)


def _run_case(root: Path, sender: str, *, sandboxed: bool, env: dict[str, str]) -> dict[str, Any]:
    label = "sandbox" if sandboxed else "control"
    directory = root / label
    allowed, denied = directory / "allowed/canary.bin", directory / "denied/canary.bin"
    allowed.parent.mkdir(parents=True)
    denied.parent.mkdir()
    for file in (allowed, denied):
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"." * 128)
    identity = allowed.stat()
    verifier = os.open(allowed, os.O_RDONLY | os.O_NOFOLLOW)
    marker = label + "-AFTER-DEATH-" + uuid.uuid4().hex
    case: dict[str, Any] = {"controller_pid": os.getpid(), "canary": str(allowed),
                           "canary_identity": [identity.st_dev, identity.st_ino],
                           "marker_length": len(marker), "order": [], "raw_events": [], "cleanup": [],
                           "canary_initial_mode": stat.S_IMODE(identity.st_mode),
                           "verifier_access_mode": fcntl.fcntl(verifier, fcntl.F_GETFL) & os.O_ACCMODE}
    case["verifier_read_only"] = case["verifier_access_mode"] == os.O_RDONLY
    broker = receiver = None
    broker_reader = broker_input = None
    sockets = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    gate_read, gate_write = os.pipe()
    try:
        with (directory / "broker.stderr").open("wb") as broker_errors, \
             (directory / "receiver.stderr").open("wb") as receiver_errors:
            receiver_spec = {"socket_fd": sockets[1].fileno(), "gate_fd": gate_read,
                             "denied": str(denied), "marker": marker, "canary": str(allowed),
                             "canary_identity": case["canary_identity"]}
            receiver = _actor("receiver", receiver_spec, stderr=receiver_errors,
                              pass_fds=(sockets[1].fileno(), gate_read), env=env)
            broker_spec = {"sender": sender, "socket_fd": sockets[0].fileno(),
                           "canary": str(allowed), "denied": str(denied)}
            broker = _actor("broker", broker_spec, stderr=broker_errors,
                            pass_fds=(sockets[0].fileno(),), env=env)
            for channel in sockets:
                channel.close()
            os.close(gate_read)
            gate_read = -1
            if broker.stdout is None or broker.stdin is None or receiver.stdout is None:
                raise ProbeError("actor pipes unavailable")
            broker_input = broker.stdin
            broker_reader = _Reader(broker.stdout, case["raw_events"], "broker/sender")
            receiver_reader = _Reader(receiver.stdout, case["raw_events"], "receiver")
            for _ in range(2):
                event = broker_reader.event()
                _require(event["event"] in {"spawned", "sender_ready"}
                         and event["event"] not in case, "unexpected startup event")
                case[event["event"]] = event
            event = receiver_reader.event()
            _require(event["event"] == "receiver_ready", "receiver startup failed")
            case["receiver_ready"] = event
            ready = case["sender_ready"]
            _require(case["spawned"]["broker_pid"] == broker.pid
                     and ready["pid"] == case["spawned"]["coordinator_pid"]
                     and ready["ppid"] == broker.pid and ready["pgid"] == broker.pid
                     and [ready["device"], ready["inode"]] == case["canary_identity"],
                     "sender ancestry or selected file differs before transfer")
            _require(event["pid"] == receiver.pid and event["ppid"] == os.getpid()
                     and event["outside_errno"] == 0, "outside receiver identity differs")
            _require(ready["allowed_write"] is True and ready["allowed_errno"] == 0,
                     "authorized canary write is unavailable")
            _require(ready["outside_errno"] in DENIALS if sandboxed else ready["outside_errno"] == 0,
                     "functional sandbox/control denial is unavailable")
            case["before_gate_sha256"] = hashlib.sha256(_read_verifier(verifier)).hexdigest()
            os.fchmod(verifier, 0)
            case["canary_mode_at_transfer"] = stat.S_IMODE(os.fstat(verifier).st_mode)
            _require(case["canary_mode_at_transfer"] == 0, "canary pathname revocation failed")
            case["order"].append("canary_pathname_revoked")
            broker_input.write(b"go\n")
            broker_input.flush()
            case["go_sent"] = True
            event = broker_reader.event()
            _require(event["event"] == "sender_sent", "sender export result missing")
            case["sender_sent"] = event
            broker_input.write(b"kill\n")
            broker_input.flush()
            event = broker_reader.event()
            _require(event["event"] == "coordinator_reaped", "coordinator retirement missing")
            case["coordinator_reaped"] = event
            case["order"].append("coordinator_reaped")
            os.kill(broker.pid, signal.SIGKILL)
            case["broker_returncode"] = broker.wait(timeout=TIMEOUT)
            case["order"].append("broker_reaped")
            event = receiver_reader.event()
            _require(event["event"] == "receiver_received", "receiver transfer result missing")
            case["receiver_received"] = event
            _require(event["pathname_errno"] in DENIALS, "receiver can reopen canary pathname")
            if event["received"]:
                _require([event["device"], event["inode"]] == case["canary_identity"]
                         and event["access_mode"] == os.O_RDWR,
                         "receiver selected another descriptor before its write gate")
            os.write(gate_write, b"W")
            os.close(gate_write)
            gate_write = -1
            case["order"].append("receiver_gate_released")
            event = receiver_reader.event()
            _require(event["event"] == "receiver_write", "receiver write result missing")
            case["receiver_write"] = event
            case["order"].append("receiver_write")
            case["receiver_returncode"] = receiver.wait(timeout=TIMEOUT)
            observed = _read_verifier(verifier)
            case["after_sha256"] = hashlib.sha256(observed).hexdigest()
            count = case["receiver_write"]["count"]
            case["marker_observed"] = 0 < count <= len(marker) and observed.startswith(marker[:count].encode())
            case["after_bytes_hex"] = observed.hex()
            return case
    finally:
        if (broker is not None and broker.returncode is None and broker_input is not None
                and broker_reader is not None and "coordinator_reaped" not in case):
            try:
                broker_input.write(b"kill\n" if case.get("go_sent") else b"abort\n")
                broker_input.flush()
                for _ in range(3):
                    event = broker_reader.event()
                    if event["event"] == "coordinator_reaped":
                        case["coordinator_reaped"] = event
                        break
            except (OSError, ProbeError, ValueError) as exc:
                case["cleanup_retirement_error"] = repr(exc)
        for fd in (gate_read, gate_write):
            if fd >= 0:
                os.close(fd)
        for channel in sockets:
            channel.close()
        for role, process in (("broker", broker), ("receiver", receiver)):
            if process is None:
                continue
            record: dict[str, Any] = {"role": role, "pid": process.pid}
            try:
                if process.returncode is None:
                    # Popen has not reaped this session leader, so its PID cannot
                    # be reused; only our fixed, non-daemonizing actors share it.
                    os.killpg(process.pid, signal.SIGKILL)
                record["returncode"] = process.wait(timeout=TIMEOUT)
            except (OSError, subprocess.TimeoutExpired) as exc:
                record["error"] = repr(exc)
            case["cleanup"].append(record)
        coordinator = case.get("coordinator_reaped")
        case["cleanup"].append(
            {"role": "coordinator", "pid": coordinator["coordinator_pid"],
             "returncode": coordinator["returncode"]} if coordinator else
            {"role": "coordinator", "pid": case.get("spawned", {}).get("coordinator_pid"),
             "error": "no exact broker wait acknowledgment; task-owned group cleanup is not a reap proof"})
        (directory / "case.json").write_text(json.dumps(case, indent=2) + "\n")
        case["order"].append("observation_recorded")
        try:
            if all("error" not in item for item in case["cleanup"]):
                os.fchmod(verifier, stat.S_IMODE(identity.st_mode))
                case["canary_retained_mode"] = stat.S_IMODE(os.fstat(verifier).st_mode)
                case["order"].append("canary_mode_restored")
        finally:
            os.close(verifier)
        (directory / "case.json").write_text(json.dumps(case, indent=2) + "\n")


def run_probe(output_root: Path) -> dict[str, Any]:
    """Build and run control then App Sandbox observations in an absent external root.

    All mutations are task-local except a possible OS-created unique sandbox
    container, which is recorded and never deleted here. Failures retain their
    receipts and fixtures. This creates no graph/backend authority.
    """
    _require(sys.platform == "darwin" and 0 < os.getuid() == os.geteuid(),
             "non-root macOS execution without UID elevation required")
    root = validate_output_root(output_root)
    root.mkdir(mode=0o700)
    (root / "tmp").mkdir()
    # The entitlement directory exists before signing; the case fills it later.
    (root / "sandbox/allowed").mkdir(parents=True)
    env = _child_env(root)
    receipt: dict[str, Any] = {"schema": "graphify.app-sandbox-fd-probe.v1", "output_root": str(root),
                              "platform": platform.platform(), "python": sys.version,
                              "uid": os.getuid(), "source_sha256": _sha(SOURCE),
                              "runner_sha256": _sha(SCRIPT), "commands": [], "cases": {},
                              "qualified": False, "live_replacement_enabled": False}
    try:
        receipt["os_version"] = _command(["/usr/bin/sw_vers"], receipt["commands"], env).decode()
        build = _build(root, root / "sandbox/allowed", receipt["commands"], env)
        receipt["build"] = build
        # _run_case owns creation of each new case directory.
        (root / "sandbox/allowed").rmdir()
        (root / "sandbox").rmdir()
        control = _run_case(root, build["control"]["executable"], sandboxed=False, env=env)
        control["classification"] = classify_case(control, sandboxed=False)
        (root / "control/case.json").write_text(json.dumps(control, indent=2) + "\n")
        receipt["cases"]["control"] = control
        _require(control["classification"] == "fd-write-after-death", "positive transfer control failed")
        sandbox = _run_case(root, build["sandbox"]["executable"], sandboxed=True, env=env)
        sandbox["classification"] = classify_case(sandbox, sandboxed=True)
        (root / "sandbox/case.json").write_text(json.dumps(sandbox, indent=2) + "\n")
        receipt["cases"]["sandbox"] = sandbox
        receipt["status"] = ("containment-failed" if sandbox["classification"] == "fd-write-after-death"
                             else "bounded-blocked-observation")
        receipt["exit_code"] = 3 if receipt["status"] == "containment-failed" else 0
        return receipt
    except Exception as exc:
        receipt.update(status="blocked-or-harness-invalid", exit_code=2,
                       error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        for item in receipt.get("build", {}).values():
            if isinstance(item, dict) and "container" in item:
                item["container_exists_after"] = Path(item["container"]).exists()
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Opt in explicitly; exit 3 for a counterexample, 2 for invalid/blocked execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-native", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--_role", choices=("broker", "receiver"), help=argparse.SUPPRESS)
    parser.add_argument("--_spec", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.run_native:
        _emit({"status": "blocked", "reason": "explicit --run-native required", "qualified": False})
        return 2
    if args._role:
        _require(args._spec is not None, "internal actor specification required")
        (_broker if args._role == "broker" else _receiver)(json.loads(args._spec))
        return 0
    if args.output_root is None:
        parser.error("--output-root is required with --run-native")
    try:
        receipt = run_probe(args.output_root)
    except Exception as exc:
        _emit({"status": "blocked-or-harness-invalid", "reason": repr(exc), "qualified": False})
        return 2
    _emit({"status": receipt["status"], "qualified": False, "receipt": str(Path(receipt["output_root"]) / "receipt.json")})
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
