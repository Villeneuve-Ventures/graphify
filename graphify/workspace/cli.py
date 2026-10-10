"""Public dispatch: bounded transport over the delivered structural library."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

from .cli_contracts import (
    COMMANDS, READ_COMMANDS, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
    WorkspaceCommandRequest, WorkspaceCommandResponse, authorization, response,
)
from .contracts import CompatibilityManifest, ContractError, canonical_json_bytes


class _ProtocolError(RuntimeError):
    """The worker outcome cannot be established from its bounded response."""


def _emit(reply, stream):
    # Preserve canonical UTF-8/LF bytes even on hosts whose text streams
    # translate newlines. The text fallback supports in-memory embedding streams.
    if hasattr(stream, "buffer"):
        stream.buffer.write(reply.canonical)
    else:
        stream.write(reply.canonical.decode("utf-8"))
    stream.flush()


def _read_request(name):
    """Bound even cold stdin acquisition; never open a FIFO request path."""
    if name == "-":
        fd = sys.stdin.fileno()
        deadline = time.monotonic() + 10
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return _read_regular_request(fd, deadline=deadline)
        chunks, size = [], 0
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ContractError("request acquisition expired")
                chunk = os.read(fd, min(65536, MAX_REQUEST_BYTES + 1 - size))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_REQUEST_BYTES:
                    raise ContractError("request byte limit exceeded")
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        return _read_regular_request(fd)
    finally:
        os.close(fd)


def _read_regular_request(fd, *, deadline=None):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REQUEST_BYTES:
        raise ContractError("bounded regular request file required")
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    chunks, size = [], 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise ContractError("request acquisition expired")
        chunk = os.read(fd, min(65536, MAX_REQUEST_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise ContractError("request byte limit exceeded")
    after = os.fstat(fd)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or size != before.st_size - offset):
        raise ContractError("request changed while reading")
    return b"".join(chunks)


# Workspace computations use installation paths, never caller cwd/PYTHONPATH.
# Read-only guards are installed before importing the command implementation.
# Mutation workers are time bounded too; killing one cannot prove rollback.
_BOOTSTRAP = r'''
import sys, os, stat, json
paths, cache_prefix, readonly, deadline_ns = json.loads(sys.argv[1])
sys.path[:] = paths
sys.pycache_prefix = cache_prefix
if readonly:
    def audit(event, args):
        mutation = event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod',
                            'os.link', 'os.symlink', 'os.truncate', 'os.utime', 'os.chown'}
        if event == 'open':
            path, _, flags = args
            mutation = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if type(path) is int and stat.S_ISFIFO(os.fstat(path).st_mode):
                mutation = False
            if isinstance(path, (str, bytes)) and 'jieba.cache' in os.fsdecode(path):
                raise RuntimeError('ambient tokenizer cache access')
        if mutation or event in {'socket.connect', 'socket.bind', 'os.system', 'os.fork', 'os.forkpty'}:
            raise RuntimeError('read-only command attempted a side effect')
    sys.addaudithook(audit)
from graphify.workspace.cli import _command_child
_command_child(deadline_ns)
'''


def _run_bounded(request, deadline_ns):
    from ._readonly import _worker_import_paths
    from .persistence import LockTimeout, require_before_deadline
    import json

    value = request.to_dict()
    readonly = value["command"] in READ_COMMANDS or (
        value["command"] == "sync" and value["parameters"]["operation"] == "prepare")
    # Prepare uses the same no-write observer path, but host qualification uses
    # df/diskutil. The outer audit permits those bounded read-only helpers.
    startup = json.dumps([_worker_import_paths(deadline_ns), sys.pycache_prefix,
                          readonly, deadline_ns])
    command = [sys.executable, "-I", "-S", "-B", "-c", _BOOTSTRAP, startup]
    def remaining():
        require_before_deadline(deadline_ns, "command deadline expired")
        return (deadline_ns - time.monotonic_ns()) / 1_000_000_000
    with subprocess.Popen(command, close_fds=True, start_new_session=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C", "LC_ALL": "C"}) as process:
        output = bytearray()
        sent = total = 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream, event in ((process.stdin, selectors.EVENT_WRITE),
                                      (process.stdout, selectors.EVENT_READ),
                                      (process.stderr, selectors.EVENT_READ)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event)
                while selector.get_map():
                    events = selector.select(remaining())
                    remaining()
                    for key, _ in events:
                        if key.fileobj is process.stdin:
                            try:
                                sent += os.write(key.fd, request.canonical[sent:])
                            except BrokenPipeError:
                                sent = len(request.canonical)
                            if sent == len(request.canonical):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        chunk = os.read(key.fd, min(65536, MAX_RESPONSE_BYTES - total + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise _ProtocolError("command output byte limit exceeded")
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            process.wait(timeout=remaining())
            remaining()
        except subprocess.TimeoutExpired:
            raise LockTimeout("command deadline expired") from None
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            result = WorkspaceCommandResponse.from_json(bytes(output))
        except (ContractError, TypeError):
            raise _ProtocolError("invalid command response") from None
        fields = result.to_dict()
        expected_exit = _exit_code(fields["error_code"])
        if (process.returncode != expected_exit or fields["command"] != value["command"]
                or fields["request_id"] != value["request_id"]):
            raise _ProtocolError("command response differs")
        return result, process.returncode


def _execute(value, deadline_ns):
    from .composition import load_workspace_runtime_inputs, compose_workspace_runtime
    from .identity import discover_source
    from .persistence import require_before_deadline
    from .sync import StructuralSyncRequest, prepare_structural_sync, synchronize_structural

    expected = CompatibilityManifest.from_mapping(value["compatibility_manifest"])
    runtime = compose_workspace_runtime(load_workspace_runtime_inputs(
        state_root=Path(value["state_root"]), expected=expected)).require_runtime()
    runtime.validate_authority(deadline_ns=deadline_ns)
    command, p = value["command"], value["parameters"]
    stores = runtime.stores
    if command in {"register", "activate"}:
        source = discover_source(Path(p["source_root"]), deadline_ns=deadline_ns)
        action = p["operation"] if command == "register" else "activate"
        auth = authorization(p["authorization"], action)
        runtime.validate_authority(deadline_ns=deadline_ns)
        require_before_deadline(deadline_ns, "command deadline expired")
        if command == "register":
            method = stores.registry.rotate_enrollment_evidence if action == "rotate" else getattr(stores.registry, action)
            registry = method(source, auth, expected_revision=p["expected_registry_revision"])
            extra = {}
        else:
            activation = stores.registry.activate_source(source, auth,
                leases=stores.leases, owner=stores.leases.current_owner(),
                **{key: p[key] for key in ("expected_registry_revision", "expected_active_source_revision",
                    "expected_operation_epoch", "expected_migration_epoch", "ttl_ns")},
                acquired_at=datetime.now(timezone.utc), monotonic_ns=time.monotonic_ns())
            registry = activation.registry
            extra = {"operation_epoch": activation.grant.operation_epoch}
        entry = next(e for e in registry.to_dict()["workspaces"] if e["repo_uuid"] == source.repo_uuid)
        return {"registry_revision": registry.to_dict()["revision"], "repo_uuid": source.repo_uuid,
                "active_source_revision": entry["active_source_revision"], **extra}
    if command == "sync":
        if p["operation"] == "prepare":
            frozen = prepare_structural_sync(runtime, **{k: v for k, v in p.items() if k != "operation"})
            return {"sync_request": frozen.to_dict()}
        frozen = StructuralSyncRequest.from_json(canonical_json_bytes(p["sync_request"]))
        return asdict(synchronize_structural(runtime, frozen, attempt_sha256=p["attempt_sha256"]))
    if command == "rollback":
        from .rollback import rollback_structural
        return {"pointer": rollback_structural(runtime, p, deadline_ns=deadline_ns).to_dict()}
    if command == "query":
        from .adapters.base import QueryRequest
        from .query import query_structural
        query = QueryRequest(**{k: v for k, v in p.items() if k != "repo_uuid"})
        return {"text": query_structural(runtime, p["repo_uuid"], query, deadline_ns=deadline_ns)}
    from .status import inspect_structural
    return inspect_structural(runtime, p["repo_uuid"], deadline_ns=deadline_ns, doctor=command == "doctor")


def _error(exc, *, mutation=False):
    from .composition import WorkspaceAuthorityInvalid
    from .persistence import CommitUnknown, LockTimeout, UnsupportedRuntime, WorkspaceRuntimeError
    from .identity import IdentityError, SourceDiscoveryTimeout
    from .registry import RegistryError
    from .generations import GenerationError
    from .pointers import PointerError
    from .leases import LeaseError
    if isinstance(exc, (LockTimeout, SourceDiscoveryTimeout, TimeoutError)):
        return "execution_unknown" if mutation else "deadline_exceeded"
    if isinstance(exc, CommitUnknown):
        return "execution_unknown"
    if isinstance(exc, UnsupportedRuntime):
        return "unsupported_runtime"
    if isinstance(exc, WorkspaceAuthorityInvalid):
        return "authority_invalid"
    if isinstance(exc, (ContractError, WorkspaceRuntimeError, IdentityError,
                        RegistryError, GenerationError, PointerError, LeaseError)):
        return "workspace_refused"
    return "execution_unknown" if mutation else "execution_failed"


def _exit_code(code):
    if code is None:
        return 0
    if code == "bad_request":
        return 2
    if code in {"unsupported_runtime", "authority_invalid"}:
        return 3
    if code in {"deadline_exceeded", "execution_unknown"}:
        return 5
    return 4


def _command_child(deadline_ns):
    from . import _readonly
    if os.getpgrp() != os.getpid():
        raise RuntimeError("command supervisor group required")
    _readonly._SUPERVISED_GROUP = os.getpgrp()
    value = WorkspaceCommandRequest.from_json(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)).to_dict()
    mutation = value["command"] not in READ_COMMANDS and not (
        value["command"] == "sync" and value["parameters"]["operation"] == "prepare")
    try:
        result = _execute(value, deadline_ns)
        # A mutation can have succeeded after its budget. Do not claim success.
        from .persistence import require_before_deadline
        require_before_deadline(deadline_ns, "command deadline expired")
        reply = response(value["command"], value["request_id"], result=result)
    except Exception as exc:
        reply = response(value["command"], value["request_id"], error_code=_error(exc, mutation=mutation))
    sys.stdout.buffer.write(reply.canonical)
    raise SystemExit(_exit_code(reply.to_dict()["error_code"]))


def run_workspace_cli(arguments):
    if not arguments or arguments == ["--help"] or arguments == ["-h"]:
        print("Usage: graphify workspace <register|activate|sync|rollback|query|status|doctor> --request FILE")
        print("Use '-' for bounded stdin. Requests must be canonical JSON and carry explicit candidate authority.")
        print("Launch with PYTHONDONTWRITEBYTECODE=1 or python -B before importing Graphify.")
        return 0
    if not sys.dont_write_bytecode:
        _emit(response(error_code="unsupported_runtime"), sys.stderr)
        return 3
    if any(not hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "killpg", "getpgrp")):
        _emit(response(error_code="unsupported_runtime"), sys.stderr)
        return 3
    request = None
    try:
        if (len(arguments) != 3 or arguments[0] not in COMMANDS
                or arguments[1] != "--request" or not arguments[2]):
            raise ContractError("command and one request required")
        request = WorkspaceCommandRequest.from_json(_read_request(arguments[2]))
        value = request.to_dict()
        if value["command"] != arguments[0]:
            raise ContractError("request command differs")
    except Exception:
        _emit(response(error_code="bad_request"), sys.stderr)
        return 2
    mutation = value["command"] not in READ_COMMANDS and not (
        value["command"] == "sync" and value["parameters"]["operation"] == "prepare")
    deadline_ns = time.monotonic_ns() + value["timeout_ms"] * 1_000_000
    try:
        reply, code = _run_bounded(request, deadline_ns)
    except Exception as exc:
        reply = response(value["command"], value["request_id"], error_code=_error(exc, mutation=mutation))
        code = _exit_code(reply.to_dict()["error_code"])
    stream = sys.stdout if code == 0 else sys.stderr
    _emit(reply, stream)
    return code
