"""GC recovery must not acknowledge unreadable or unsynced durable state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import errno
import os
import time

import pytest

from graphify.workspace.gc import (
    GcError, GcProtection, GcStore, _GcReachability, _MAX_GC_INTENT_BYTES,
)
from graphify.workspace.lifecycle_contracts import CapacityPolicy, GcPurgeState
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


def test_replayed_purge_must_match_indexed_completion(tmp_path):
    harness, generations, gc, grant, plan, protections, candidate, registry = (
        _gc_with_candidate(tmp_path, fault_hook=lambda _point: None)
    )
    completion = gc.execute(
        grant, plan, capacity_policy=fixtures.POLICY, protections=protections,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
    )
    assert completion.quarantined == (candidate,)
    purge = gc.purge(
        grant, plan_sha256=plan.sha256, capacity_policy=fixtures.POLICY,
        protections=protections, completed_at=START + timedelta(seconds=2),
        monotonic_ns=10_003,
    )
    assert purge.purged == completion.quarantined
    changed = purge.to_dict()
    changed["purged"] = []
    gc.state.path(gc._purge_path(REPO_UUID, plan.sha256)).write_bytes(
        GcPurgeState.from_mapping(changed).canonical
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GcError, match="purge record does not bind"):
        gc.purge(
            grant, plan_sha256=plan.sha256, capacity_policy=fixtures.POLICY,
            protections=protections, completed_at=START + timedelta(seconds=3),
            monotonic_ns=10_004,
        )
    with pytest.raises(GcError, match="purge record does not bind"):
        gc.preflight_lifecycle(
            REPO_UUID,
            expected_registry_revision=registry["revision"],
            expected_active_source_revision=grant.active_source_revision,
            expected_operation_epoch=grant.operation_epoch,
            expected_migration_epoch=grant.migration_epoch,
            expected_pointer_revision=plan.pointer_revision,
            plan_sha256=plan.sha256,
            deadline_ns=time.monotonic_ns() + 5_000_000_000,
        )
    assert tree_snapshot(harness.state_root) == before


def test_large_accepted_gc_policy_plans_recoverable_batches(tmp_path, monkeypatch):
    harness, _generations, gc, grant, _plan, protections, _candidate, _registry = (
        _gc_with_candidate(tmp_path, fault_hook=lambda _point: None)
    )
    policy_data = fixtures.POLICY.to_dict()
    policy_data["global_max_generations"] = 16_000
    policy_data["workspace_max_generations"] = 16_000
    policy = CapacityPolicy.from_mapping(policy_data)
    candidates = tuple(f"gen-{number:05d}-{'x' * 57}" for number in range(16_000))
    pending = candidates

    def reachability(*_args, **_kwargs):
        return _GcReachability(0, pending, ())

    monkeypatch.setattr(gc, "_reachability_locked", reachability)
    first = gc.plan(
        grant, capacity_policy=policy, protections=protections,
        monotonic_ns=10_001,
    )
    assert first.candidates == candidates[:8192]
    with harness.leases.current_operation_read_only(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        intent = gc._intent(operation, first, occurred_at=START)
    gc._require_recoverable_record_sizes(intent)
    assert len(intent.canonical) < _MAX_GC_INTENT_BYTES

    pending = candidates[8192:]
    second = gc.plan(
        grant, capacity_policy=policy, protections=protections,
        monotonic_ns=10_001,
    )
    assert second.candidates == pending


def test_operational_gc_batches_leave_remaining_generations_for_successor(tmp_path, monkeypatch):
    harness, generations, gc, grant, _plan, protections, candidate, registry = (
        _gc_with_candidate(tmp_path, fault_hook=lambda _point: None)
    )
    monkeypatch.setattr("graphify.workspace.gc._GC_OPERATION_MAX_CANDIDATES", 2)
    for generation_id in ("gen-batch-a", "gen-batch-b"):
        gc.state.ensure_directory(generations._generation(REPO_UUID, generation_id))
        lock = gc.state.path(generations._lock(REPO_UUID, generation_id))
        gc.state.ensure_directory(generations._lock(REPO_UUID, generation_id).parent)
        lock.write_bytes(b"")
        lock.chmod(0o600)

    first = gc.plan(
        grant, capacity_policy=fixtures.POLICY, protections=protections,
        monotonic_ns=10_001,
    )
    assert first.candidates == ("gen-batch-a", "gen-batch-b")
    first_completion = gc.execute(
        grant, first, capacity_policy=fixtures.POLICY, protections=protections,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
    )
    assert first_completion.quarantined == first.candidates
    gc.purge(
        grant, plan_sha256=first.sha256, capacity_policy=fixtures.POLICY,
        protections=protections, completed_at=START + timedelta(seconds=2),
        monotonic_ns=10_003,
    )
    successor = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=2_000_000, ttl_ns=1_000_000,
    )
    second = gc.plan(
        successor, capacity_policy=fixtures.POLICY, protections=protections,
        monotonic_ns=2_000_001,
    )
    assert second.candidates == (candidate,)
    second_completion = gc.execute(
        successor, second, capacity_policy=fixtures.POLICY,
        protections=protections, occurred_at=START + timedelta(seconds=3),
        monotonic_ns=2_000_002,
    )
    assert second_completion.quarantined == second.candidates


@pytest.mark.parametrize("stage", ["execute", "reconcile", "purge"])
def test_gc_batch_uses_bounded_simultaneous_generation_locks(tmp_path, monkeypatch, stage):
    harness, generations, gc, grant, _plan, protections, candidate, _registry = (
        _gc_with_candidate(tmp_path, fault_hook=lambda _point: None)
    )
    for number in range(23):
        generation_id = f"gen-descriptor-{number:02d}"
        gc.state.ensure_directory(generations._generation(REPO_UUID, generation_id))
        lock = gc.state.path(generations._lock(REPO_UUID, generation_id))
        gc.state.ensure_directory(generations._lock(REPO_UUID, generation_id).parent)
        lock.write_bytes(b"")
        lock.chmod(0o600)
    policy_value = fixtures.POLICY.to_dict()
    policy_value["global_max_generations"] = 64
    policy_value["workspace_max_generations"] = 64
    policy = CapacityPolicy.from_mapping(policy_value)
    plan = gc.plan(
        grant, capacity_policy=policy, protections=protections, monotonic_ns=10_001,
    )
    assert len(plan.candidates) == 24
    assert candidate in plan.candidates

    if stage == "reconcile":
        def interrupt(point):
            if point == "gc:intent_durable":
                raise InjectedFault(point)

        gc.fault_hook = interrupt
        with pytest.raises(InjectedFault, match="intent_durable"):
            gc.execute(
                grant, plan, capacity_policy=policy, protections=protections,
                occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
            )
        assert gc.state.path(gc._intent_path(REPO_UUID)).is_file()
        gc.fault_hook = lambda _point: None

    active_locks = 0
    peak_locks = 0
    original_lock = gc.state.existing_generation_lock

    @contextmanager
    def descriptor_limited_lock(*args, **kwargs):
        nonlocal active_locks, peak_locks
        with original_lock(*args, **kwargs):
            active_locks += 1
            peak_locks = max(peak_locks, active_locks)
            try:
                if active_locks > 2:
                    raise OSError(errno.EMFILE, "injected generation lock descriptor limit")
                yield
            finally:
                active_locks -= 1

    if stage != "purge":
        monkeypatch.setattr(gc.state, "existing_generation_lock", descriptor_limited_lock)
    if stage == "reconcile":
        completion = gc.reconcile(
            grant, capacity_policy=policy, protections=protections,
            completed_at=START + timedelta(seconds=2), monotonic_ns=10_003,
        )
    else:
        completion = gc.execute(
            grant, plan, capacity_policy=policy, protections=protections,
            occurred_at=START + timedelta(seconds=2), monotonic_ns=10_003,
        )
    assert completion is not None
    assert completion.quarantined == plan.candidates
    assert not gc.state.path(gc._intent_path(REPO_UUID)).exists()
    if stage == "purge":
        monkeypatch.setattr(gc.state, "existing_generation_lock", descriptor_limited_lock)
        purge = gc.purge(
            grant, plan_sha256=plan.sha256, capacity_policy=policy,
            protections=protections, completed_at=START + timedelta(seconds=3),
            monotonic_ns=10_004,
        )
        assert purge.purged == plan.candidates
    assert peak_locks <= 2
