"""S5 diagnostics preserve source/state bytes and withhold incomplete reads."""
import json
import os
import stat
import time

import pytest

from graphify.workspace.status import inspect_structural
from graphify.workspace.sync import _observe, synchronize_structural
from tests.test_workspace_structural_s4 import request_for, runtime_fixture
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


def inspect(runtime, *, doctor=False):
    return inspect_structural(runtime, REPO_UUID, doctor=doctor,
                              deadline_ns=time.monotonic_ns() + 60_000_000_000)


def build(runtime):
    return synchronize_structural(runtime, request_for(runtime), attempt_sha256="a" * 64)


@pytest.mark.parametrize("doctor", [False, True])
def test_unbuilt_and_current_are_zero_write(tmp_path, monkeypatch, doctor):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    unbuilt = inspect(runtime, doctor=doctor)
    assert unbuilt["state"] == "unbuilt"
    assert not unbuilt["safe_to_query"]
    assert unbuilt["operation_epoch"] == 1
    assert unbuilt["desired_watermark"] == 0
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before
    result = build(runtime)
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    current = inspect(runtime, doctor=doctor)
    assert current["state"] == "current"
    assert current["safe_to_query"] is True
    assert current["registry_revision"] == 1
    assert current["active_source_revision"] == 1
    assert current["pointer_revision"] == result.pointer_revision
    assert current["generation_id"] == result.generation_id
    assert current["receipt_sha256"] == result.receipt_sha256
    assert current["migration_epoch"] == 0
    assert current["desired_watermark"] == 1
    assert current["staged_lifecycle_state"] == "PROMOTED"
    assert current["lease_states"] == {}
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


def test_current_diagnostics_intercept_mutations(tmp_path, monkeypatch):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    build(runtime)
    original = os.open
    original_write = os.write
    attempts = []

    def read_only_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            attempts.append(path)
            raise AssertionError("write attempted")
        return original(path, flags, *args, **kwargs)

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("mutation attempted")

    def write_pipe_only(fd, value):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return refuse(fd, value)
        return original_write(fd, value)

    monkeypatch.setattr(os, "open", read_only_open)
    # Capability checks use function identity; retain the wrapped primitive's
    # genuine dir_fd support rather than making the read runtime unavailable.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {read_only_open})
    monkeypatch.setattr(os, "write", write_pipe_only)
    for name in ("mkdir", "rename", "replace", "unlink", "rmdir", "chmod", "fchmod", "truncate"):
        monkeypatch.setattr(os, name, refuse)
    assert inspect(runtime)["safe_to_query"] is True, attempts
    assert inspect(runtime, doctor=True)["safe_to_query"] is True, attempts
    assert attempts == []


@pytest.mark.parametrize("doctor", [False, True])
def test_changed_source_is_withheld(tmp_path, monkeypatch, doctor):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    build(runtime)
    (repo / "main.py").write_text("def changed():\n    return 9\n")
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    result = inspect(runtime, doctor=doctor)
    assert result["state"] == "stale"
    assert result["safe_to_query"] is False
    assert "generation_id" not in result
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


def test_staged_request_is_pending_without_recovery(tmp_path, monkeypatch):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    _, observations = _observe(runtime, REPO_UUID)
    runtime.stores.generations.request_staged_build(
        REPO_UUID, request.generation_id, request.build, source_observations=observations)
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    result = inspect(runtime)
    assert result["state"] == "pending"
    assert result["safe_to_query"] is False
    assert result["staged_lifecycle_state"] == "REQUESTED"
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


@pytest.mark.parametrize("target", ["workspace_lock", "generation_lock", "receipt", "authority", "pointer"])
def test_missing_state_withholds_without_creating(tmp_path, monkeypatch, target):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    result = build(runtime)
    stores = runtime.stores
    paths = {
        "workspace_lock": stores.leases.state.path(stores.leases._directory(REPO_UUID) / "workspace.lock"),
        "generation_lock": stores.generations.state.path(stores.generations._lock(REPO_UUID, result.generation_id)),
        "receipt": stores.generations.state.path(stores.generations._generation(REPO_UUID, result.generation_id) / "receipt.json"),
        "authority": runtime.inputs.state_root / "runtime-manifest.json",
        "pointer": stores.pointers.state.path(stores.pointers._current(REPO_UUID)),
    }
    paths[target].unlink()
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    output = inspect(runtime, doctor=True)
    assert output["state"] == "unavailable"
    assert output["safe_to_query"] is False
    assert "generation_id" not in output
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


def test_errors_are_redacted_and_deadlines_precede_traversal(tmp_path, monkeypatch):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    calls = []

    def unavailable(self, **kwargs):
        calls.append(kwargs)
        raise ValueError("/private/operator/path secret reason")

    monkeypatch.setattr(type(runtime), "validate_authority", unavailable)
    result = inspect(runtime)
    assert result == {"state": "unavailable", "safe_to_query": False,
                      "reason_code": "diagnostic_unavailable"}
    assert "private" not in json.dumps(result)
    calls.clear()
    expired = inspect_structural(runtime, REPO_UUID, deadline_ns=1)
    assert expired["reason_code"] == "deadline_exceeded"
    assert calls == []
    invalid = inspect_structural(runtime, REPO_UUID, deadline_ns=True)
    assert invalid["reason_code"] == "invalid_request"
    assert calls == []


def test_doctor_holds_read_locks_and_withholds_drift(tmp_path, monkeypatch):
    from graphify.workspace.persistence import _LOCK_STACK

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    build(runtime)
    locks = []

    def traverse(payload, request, *, deadline_ns):
        locks.extend(name for _rank, name in _LOCK_STACK.get())
        (repo / "main.py").write_text("def moved():\n    return 7\n")
        return "buffered private result"

    monkeypatch.setattr(runtime.adapter, "query_structural", traverse)
    before = tree_snapshot(runtime.inputs.state_root)
    result = inspect(runtime, doctor=True)
    assert result == {"state": "stale", "safe_to_query": False, "reason_code": "source_stale"}
    assert locks == ["registry", "workspace", "generation:gen-s4"]
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_late_authority_failure_discards_provisional_fields(tmp_path, monkeypatch):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    original = type(runtime).validate_authority
    calls = []

    def change_authority(self, **kwargs):
        calls.append(None)
        if len(calls) == 2:
            raise ValueError("private authority error")
        return original(self, **kwargs)

    monkeypatch.setattr(type(runtime), "validate_authority", change_authority)
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    assert inspect(runtime) == {"state": "unavailable", "safe_to_query": False,
                                "reason_code": "diagnostic_unavailable"}
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_unsafe_authority_is_zero_write_and_redacted(tmp_path, monkeypatch, unsafe):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    authority = runtime.inputs.state_root / "runtime-manifest.json"
    if unsafe == "mode":
        authority.chmod(0o644)
    else:
        content = authority.read_bytes()
        authority.unlink()
        target = tmp_path / "private-authority.json"
        target.write_bytes(content)
        target.chmod(0o600)
        authority.symlink_to(target)
    before = tree_snapshot(tmp_path)
    result = inspect(runtime, doctor=True)
    assert result == {"state": "unavailable", "safe_to_query": False,
                      "reason_code": "diagnostic_unavailable"}
    assert tree_snapshot(tmp_path) == before
