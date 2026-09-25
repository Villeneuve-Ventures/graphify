"""GC recovery must not acknowledge unreadable or unsynced durable state."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import errno
import os

import pytest

from graphify.workspace.gc import GcError, GcProtection, GcStore, _MAX_GC_INTENT_BYTES
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from tests import test_workspace_lifecycle_s3 as fixtures
from tests.workspace_s3_helpers import REPO_UUID, START, tree_snapshot


def _gc_with_candidate(tmp_path, *, fault_hook):
    harness, generations, pointers, _observations = fixtures._runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities, fault_hook=fault_hook,
    )
    candidate = "gen-uncertain-rename"
    gc.state.ensure_directory(generations._generation(REPO_UUID, candidate))
    lock = gc.state.path(generations._lock(REPO_UUID, candidate))
    gc.state.ensure_directory(generations._lock(REPO_UUID, candidate).parent)
    lock.write_bytes(b"")
    lock.chmod(0o600)
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    plan = gc.plan(
        grant, capacity_policy=fixtures.POLICY, protections=protections,
        monotonic_ns=10_001,
    )
    return harness, generations, gc, grant, plan, protections, candidate, registry


@pytest.mark.parametrize("fault_stage", ["renamed", "source_parent_durable"])
@pytest.mark.parametrize("retry_failure", [None, "source_parent", "destination_parent"])
def test_reconcile_syncs_visible_quarantine_before_completion(
    tmp_path, monkeypatch, fault_stage, retry_failure,
):
    candidate = "gen-uncertain-rename"

    def fault(point):
        if point == f"gc:{candidate}:quarantine:{fault_stage}":
            raise InjectedFault(point)

    harness, generations, gc, grant, plan, protections, candidate, registry = (
        _gc_with_candidate(tmp_path, fault_hook=fault)
    )
    with pytest.raises(CommitUnknown):
        gc.execute(
            grant, plan, capacity_policy=fixtures.POLICY, protections=protections,
            occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
        )
    assert gc.state.path(gc._intent_path(REPO_UUID)).is_file()
    source = generations._generation(REPO_UUID, candidate)
    destination = gc._quarantine(REPO_UUID, candidate, grant.operation_epoch)
    assert not gc.state.path(source).exists()
    assert gc.state.path(destination).is_dir()

    gc.state.fault_hook = lambda _point: None
    gc.fault_hook = lambda _point: None
    successor = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=2_000_000, ttl_ns=1_000_000,
    )
    source_parent = gc.state.path(source.parent).stat()
    destination_parent = gc.state.path(destination.parent).stat()
    identities = {
        (source_parent.st_dev, source_parent.st_ino): "source_parent",
        (destination_parent.st_dev, destination_parent.st_ino): "destination_parent",
    }
    events = []
    fail_retry = retry_failure is not None
    original_sync = gc.state.syscalls.fsync
    original_install = gc.state.install_once_bytes

    def record_sync(descriptor):
        observed = os.fstat(descriptor)
        label = identities.get((observed.st_dev, observed.st_ino))
        if label is not None:
            events.append(label)
            if fail_retry and label == retry_failure:
                raise OSError(errno.EIO, "injected parent sync failure")
        original_sync(descriptor)

    def record_completion(relative, data, *, label, **kwargs):
        if label == "gc:completion":
            events.append("completion")
        return original_install(relative, data, label=label, **kwargs)

    monkeypatch.setattr(gc.state.syscalls, "fsync", record_sync)
    monkeypatch.setattr(gc.state, "install_once_bytes", record_completion)
    if retry_failure is not None:
        with pytest.raises(OSError, match="injected parent sync failure"):
            gc.reconcile(
                successor, capacity_policy=fixtures.POLICY, protections=protections,
                completed_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
            )
        assert gc.state.path(gc._intent_path(REPO_UUID)).is_file()
        assert not gc.state.path(gc._completion_path(REPO_UUID, plan.sha256)).exists()
        fail_retry = False
        events.clear()
    completion = gc.reconcile(
        successor, capacity_policy=fixtures.POLICY, protections=protections,
        completed_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
    )
    assert completion is not None
    assert events.index("source_parent") < events.index("completion")
    assert events.index("destination_parent") < events.index("completion")
    assert not gc.state.path(gc._intent_path(REPO_UUID)).exists()


def test_gc_rejects_unreadable_intent_before_install(tmp_path, monkeypatch):
    harness, generations, pointers, _observations = fixtures._runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        base = gc._plan_locked(
            operation, capacity_policy=fixtures.POLICY, protections=protections,
            probe_locks=False,
        )
        candidates = tuple(f"gen-{number:05d}-{'x' * 56}" for number in range(16_000))
        plan = replace(base, candidates=candidates)
        intent = gc._intent(operation, plan, occurred_at=START)
    assert len(intent.canonical) > _MAX_GC_INTENT_BYTES
    monkeypatch.setattr(gc, "_plan_locked", lambda *a, **kw: plan)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GcError, match="read limit"):
        gc.execute(
            grant, plan, capacity_policy=fixtures.POLICY, protections=protections,
            occurred_at=START, monotonic_ns=10_002,
        )
    assert tree_snapshot(harness.state_root) == before
    assert not gc.state.path(gc._intent_path(REPO_UUID)).exists()
