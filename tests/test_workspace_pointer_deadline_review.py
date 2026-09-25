"""Pointer reads must honor deadlines even when a pending intent exists."""

import pytest

from graphify.workspace.persistence import LockTimeout
from graphify.workspace.pointers import PointerRecoveryRequired
from tests import test_workspace_lifecycle_s3 as fixtures
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


@pytest.mark.parametrize("expiry", ["before_probe", "during_probe", "not_expired"])
def test_load_pending_intent_honors_deadline(tmp_path, monkeypatch, expiry):
    harness, _generations, pointers, _observations = fixtures._runtime(tmp_path)
    pending = pointers.state.path(pointers._pending(REPO_UUID))
    pending.write_bytes(b"{}\n")
    pending.chmod(0o600)
    before = tree_snapshot(harness.state_root)
    deadline_ns = 100
    now = deadline_ns if expiry == "before_probe" else deadline_ns - 1
    monkeypatch.setattr("graphify.workspace.persistence.time.monotonic_ns", lambda: now)
    original_exists = pointers.state.private_file_exists
    probes = []

    def inspect_pending(relative):
        nonlocal now
        probes.append(relative)
        result = original_exists(relative)
        if expiry == "during_probe":
            now = deadline_ns
        return result

    monkeypatch.setattr(pointers.state, "private_file_exists", inspect_pending)
    expected_error = PointerRecoveryRequired if expiry == "not_expired" else LockTimeout
    with pytest.raises(expected_error):
        pointers.load(REPO_UUID, deadline_ns=deadline_ns)

    assert probes == ([] if expiry == "before_probe" else [pointers._pending(REPO_UUID)])
    assert tree_snapshot(harness.state_root) == before
