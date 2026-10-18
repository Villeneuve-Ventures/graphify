"""Readiness races must not corrupt or abort bounded workspace transports."""
import errno
import hashlib
import os
import subprocess
import time

import pytest

from graphify.workspace import _readonly, cli
from graphify.workspace.cli_contracts import WorkspaceCommandRequest, response
from graphify.workspace.persistence import LockTimeout
from tests.test_workspace_contracts import compatibility
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


@pytest.fixture(params=["readonly", "command"])
def supervisor(request, tmp_path, monkeypatch):
    """Exercise real isolated children while keeping workspace data disposable."""
    transport = request.param
    if transport == "readonly":
        payload = bytes(range(256)) * 2048
        expected = hashlib.sha256(payload).digest()
    else:
        value = {"contract": "graphify.workspace.cli.request", "format_version": 1,
            "command": "query", "request_id": "pipe-io", "state_root": str(tmp_path.resolve()),
            "compatibility_manifest": compatibility().to_dict(), "timeout_ms": 60000,
            "parameters": {"repo_uuid": REPO_UUID, "question": "caller", "mode": "bfs",
                "depth": 2, "token_budget": 4096, "context_filters": []}}
        command = WorkspaceCommandRequest.from_mapping(value)
        payload = command.canonical
        expected = response("query", "pipe-io",
            result={"text": hashlib.sha256(payload).hexdigest()}).canonical
    diagnostic = b"child diagnostic\n"
    program = f'''import hashlib,sys
raw = sys.stdin.buffer.read()
assert hashlib.sha256(raw).hexdigest() == {hashlib.sha256(payload).hexdigest()!r}
sys.stderr.buffer.write({diagnostic!r})
sys.stderr.buffer.flush()
sys.stdout.buffer.write({expected!r})
sys.stdout.buffer.flush()
'''
    protected = tmp_path / "protected.json"
    protected.write_bytes(b'{"retained":"unchanged"}\n')
    before = tree_snapshot(tmp_path)

    def run(*, timeout=10, output_limit=None, stall_after_output=False):
        limit = len(expected) + len(diagnostic) if output_limit is None else output_limit
        deadline = time.monotonic_ns() + int(timeout * 1_000_000_000)
        code = program + ("\nimport time; time.sleep(60)\n" if stall_after_output else "")
        if transport == "readonly":
            return _readonly.run_readonly(code, payload, deadline_ns=deadline,
                max_input_bytes=len(payload), max_output_bytes=limit)
        monkeypatch.setattr(cli, "_BOOTSTRAP", code)
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", limit)
        reply, code = cli._run_bounded(command, deadline)
        assert code == 0
        return reply.canonical

    return {"run": run, "expected": expected, "limit": len(expected) + len(diagnostic),
        "error": _readonly.ReadOnlyFailure if transport == "readonly" else cli._ProtocolError,
        "preserved": lambda: tree_snapshot(tmp_path) == before}


def _pipe_faults(monkeypatch, fault):
    """Inject faults only into the actual supervisor child's pipe descriptors."""
    real_popen, real_write, real_read = subprocess.Popen, os.write, os.read
    processes, descriptors, injected = [], {}, set()
    partial = False

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        descriptors.update({stream.fileno(): name for name, stream in (
            ("stdin", process.stdin), ("stdout", process.stdout), ("stderr", process.stderr))})
        return process

    def write(fd, data):
        nonlocal partial
        if descriptors.get(fd) != "stdin":
            return real_write(fd, data)
        if fault == "fatal":
            injected.add("stdin")
            raise OSError(errno.EIO, "injected pipe failure")
        if fault == "blocked-stdin":
            injected.add("stdin")
            raise BlockingIOError(errno.EAGAIN, "injected persistent backpressure")
        if not partial and data:
            partial = True
            return real_write(fd, data[:7])
        if fault in {"write", "mixed"} and "stdin" not in injected:
            injected.add("stdin")
            raise BlockingIOError(errno.EAGAIN, "injected write readiness race")
        return real_write(fd, data[:4096])

    def read(fd, size):
        endpoint = descriptors.get(fd)
        if endpoint == "stdout" and fault == "blocked-stdout":
            injected.add(endpoint)
            raise BlockingIOError(errno.EWOULDBLOCK, "injected persistent read race")
        if endpoint in {"stdout", "stderr"} and endpoint not in injected and fault in {endpoint, "mixed"}:
            injected.add(endpoint)
            raise BlockingIOError(errno.EWOULDBLOCK, "injected read readiness race")
        return real_read(fd, size)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "read", read)
    return processes, injected


@pytest.mark.parametrize("fault", ["write", "stdout", "stderr", "mixed"])
def test_readiness_races_preserve_complete_request_and_response(supervisor, monkeypatch, fault):
    processes, injected = _pipe_faults(monkeypatch, fault)
    assert supervisor["run"]() == supervisor["expected"]
    assert injected == ({"stdin", "stdout", "stderr"} if fault == "mixed"
                        else {"stdin" if fault == "write" else fault})
    assert len(processes) == 1 and processes[0].poll() is not None
    assert supervisor["preserved"]()


@pytest.mark.parametrize("endpoint", ["stdin", "stdout"])
def test_persistent_would_block_expires_and_reaps_child(supervisor, monkeypatch, endpoint):
    processes, injected = _pipe_faults(monkeypatch, "blocked-" + endpoint)
    with pytest.raises(LockTimeout, match="deadline expired"):
        supervisor["run"](timeout=2, stall_after_output=endpoint == "stdout")
    assert injected == {endpoint}
    assert len(processes) == 1 and processes[0].poll() is not None
    assert supervisor["preserved"]()


def test_transient_readiness_does_not_bypass_combined_output_limit(supervisor, monkeypatch):
    processes, injected = _pipe_faults(monkeypatch, "mixed")
    with pytest.raises(supervisor["error"], match="byte limit"):
        supervisor["run"](output_limit=supervisor["limit"] - 1)
    assert injected == {"stdin", "stdout", "stderr"}
    assert len(processes) == 1 and processes[0].poll() is not None
    assert supervisor["preserved"]()


def test_fatal_pipe_error_is_not_retried_or_hidden(supervisor, monkeypatch):
    processes, injected = _pipe_faults(monkeypatch, "fatal")
    with pytest.raises(OSError) as error:
        supervisor["run"]()
    assert error.value.errno == errno.EIO and injected == {"stdin"}
    assert len(processes) == 1 and processes[0].poll() is not None
    assert supervisor["preserved"]()
