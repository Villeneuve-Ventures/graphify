"""Pointer reads must honor deadlines even when a pending intent exists."""

from datetime import timedelta

import pytest

from graphify.workspace.persistence import LockTimeout
from graphify.workspace.pointers import PointerCAS, PointerRecoveryRequired
from tests import test_workspace_lifecycle_s3 as fixtures
from tests.workspace_s3_helpers import REPO_UUID, START, tree_snapshot


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


@pytest.mark.parametrize("operation", ["PROMOTE", "ROLLBACK"])
@pytest.mark.parametrize("expiry_at", ["gc_intent", "pending", "current"])
def test_pointer_move_deadline_expires_during_preflight_probe(
    tmp_path, monkeypatch, operation, expiry_at,
):
    harness, _generations, pointers, _observations = fixtures._runtime(tmp_path)
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, operation, harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    cas = PointerCAS(
        0, 1, 1, grant.operation_epoch, grant.migration_epoch, 2,
        grant.lease.to_dict()["fence_token"], "gen-deadline", "a" * 64, None,
    )
    current = pointers.state.path(pointers._current(REPO_UUID))
    current.write_bytes(b"{}\n")
    current.chmod(0o600)
    before = tree_snapshot(harness.state_root)
    deadline_ns = 100
    now = deadline_ns - 1
    armed = False
    probes = []
    original_assert = pointers._assert_no_gc_intent
    original_exists = pointers.state.private_file_exists
    probe_order = (
        pointers._gc_intent(REPO_UUID),
        pointers._pending(REPO_UUID),
        pointers._current(REPO_UUID),
    )
    expiry_path = probe_order[("gc_intent", "pending", "current").index(expiry_at)]

    def arm_preflight(*args, **kwargs):
        nonlocal armed
        armed = True
        return original_assert(*args, **kwargs)

    def probe(relative):
        nonlocal now
        if armed:
            probes.append(relative)
        result = original_exists(relative)
        if armed and relative == expiry_path:
            now = deadline_ns
        return result

    monkeypatch.setattr(pointers, "_assert_no_gc_intent", arm_preflight)
    monkeypatch.setattr(pointers.state, "private_file_exists", probe)
    monkeypatch.setattr("graphify.workspace.persistence.time.monotonic_ns", lambda: now)
    move = pointers.promote if operation == "PROMOTE" else pointers.rollback
    with pytest.raises(LockTimeout):
        move(
            grant, cas, occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_001, deadline_ns=deadline_ns,
        )
    assert probes == list(probe_order[:probe_order.index(expiry_path) + 1])
    assert tree_snapshot(harness.state_root) == before
