from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

import pytest

from graphify.workspace.contracts import CapacityPolicy
from graphify.workspace.gc import GcProtection, GcStore
from graphify.workspace.generations import CertificationRequest, GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from graphify.workspace.pointers import PointerCAS, PointerRecoveryRequired, PointerStore

from tests.workspace_p3_helpers import REPO_UUID, START, acquire, create_harness, tree_snapshot


POLICY = CapacityPolicy.from_mapping(
    {
        "contract": "graphify.workspace.capacity_policy.internal",
        "format_version": 1,
        "global_max_bytes": 32 * 1024 * 1024,
        "global_max_generations": 16,
        "workspace_max_bytes": 8 * 1024 * 1024,
        "workspace_max_generations": 8,
        "reserve_bytes": 1024,
    }
)
EMPTY_PROTECTION = GcProtection(
    migration_sources=frozenset(),
    rollback_sources=frozenset(),
    active_lease_generations=frozenset(),
    fixture_generations=frozenset(),
    proof_generations=frozenset(),
    rollback_artifact_generations=frozenset(),
)
PROTECTION_FIELDS = (
    ("migration_sources", "migration_source"),
    ("rollback_sources", "rollback_source"),
    ("active_lease_generations", "active_lease"),
    ("fixture_generations", "fixture"),
    ("proof_generations", "proof"),
    ("rollback_artifact_generations", "rollback_artifact"),
)
GC_SERIALIZATION_PHASES = (
    "gc:reachability_enumerated",
    "gc:intent_durable",
    "gc:generation_locks_acquired",
    "gc:reachability_rechecked",
    "gc:gen-unused:quarantine:renamed",
    "gc:gen-unused:quarantine:source_parent_durable",
    "gc:gen-unused:quarantine:destination_parent_durable",
    "gc:completion_durable",
)
GC_RECOVERY_PHASES = (
    "gc:intent:installed",
    "gc:intent_durable",
    "gc:generation_locks_acquired",
    "gc:reachability_rechecked",
    "gc:gen-unused:quarantine:before_rename",
    "gc:gen-unused:quarantine:renamed",
    "gc:gen-unused:quarantine:source_parent_durable",
    "gc:gen-unused:quarantine:destination_parent_durable",
    "gc:completion:installed",
    "gc:completion_durable",
    "gc:intent_clear:unlinked",
    "gc:complete",
)
SUCCESSOR_WRITER_OPERATIONS = (
    "ACTIVATE",
    "MIGRATE",
    "PROMOTE",
    "ROLLBACK",
    "REPAIR",
    "POINTER_RECOVERY",
)


def _runtime(tmp_path: Path):
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    receipts = {}
    for offset, generation_id in enumerate(("gen-current", "gen-unused")):
        allocation = generations.allocate(
            build,
            expected_payload_bytes=4096,
            capacity_policy=POLICY,
            generation_id=generation_id,
            occurred_at=START,
            monotonic_ns=10_001 + offset * 2,
        )
        payload = allocation.staging_path / "graphify-out"
        payload.mkdir()
        (payload / "graph.json").write_text(f"{generation_id}\n", encoding="utf-8")
        entries = generations.inspect_staged_payload(allocation)
        request = CertificationRequest(
            source_commit=hashlib.sha1(generation_id.encode()).hexdigest(),
            source_epoch=1,
            policy_sha256="1" * 64,
            observation_manifest_sha256="2" * 64,
            queue_watermark=0,
            semantic_completeness="not_required",
            compatibility_sha256="3" * 64,
            validations=("payload_manifest", "coordination_lock_precreated"),
        )
        receipts[generation_id] = generations.certify(
            build,
            allocation,
            request,
            declared_entries=entries,
            occurred_at=START,
            monotonic_ns=10_002 + offset * 2,
        )
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    current = receipts["gen-current"]
    pointers.promote(
        promote,
        _cas(promote, current),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    return harness, generations, pointers, receipts


def _cas(grant: Any, receipt: Any) -> PointerCAS:
    return PointerCAS(
        expected_pointer_revision=0,
        expected_active_source_revision=grant.active_source_revision,
        expected_source_epoch=1,
        expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        expected_state_schema_version=1,
        expected_fence_token=int(grant.lease.to_dict()["fence_token"]),
        candidate_generation_id=str(receipt.to_dict()["generation_id"]),
        candidate_receipt_sha256=receipt.sha256,
        expected_current_receipt_sha256=None,
    )


def test_gc_is_dry_run_first_protects_reader_then_quarantines_and_purges(
    tmp_path: Path,
) -> None:
    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
    )
    lock = generations.state.path(generations._lock(REPO_UUID, "gen-unused"))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDONLY); "
                "fcntl.flock(fd, fcntl.LOCK_SH); print('READY', flush=True); input()"
            ),
            str(lock),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "READY"
    try:
        blocked_plan = gc.plan(
            gc_grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=30_001,
        )
        assert blocked_plan.candidates == ()
        assert dict(blocked_plan.protected)["gen-unused"] == ("shared_lock",)
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=5)

    before = tree_snapshot(harness.state_root)
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_002,
    )
    assert tree_snapshot(harness.state_root) == before
    assert plan.candidates == ("gen-unused",)
    assert "gen-current" in dict(plan.protected)

    completion = gc.execute(
        gc_grant,
        plan,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        occurred_at=START,
        monotonic_ns=30_003,
    )
    quarantine = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "quarantine"
        / "gc"
        / f"gen-unused.{completion.operation_epoch}"
    )
    assert quarantine.is_dir()
    assert not (harness.state_root / "workspaces" / REPO_UUID / "generations/gen-unused").exists()
    retained_lock = (
        harness.state_root / "workspaces" / REPO_UUID / "locks/generations/gen-unused.lock"
    )
    inode = retained_lock.stat().st_ino

    purge = gc.purge(
        gc_grant,
        plan_sha256=plan.sha256,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        completed_at=START,
        monotonic_ns=30_004,
    )
    assert purge.purged == ("gen-unused",)
    assert not quarantine.exists()
    assert retained_lock.stat().st_ino == inode


def test_gc_commit_unknown_blocks_pointer_writer_until_successor_reconciles(
    tmp_path: Path,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "gc:gen-unused:quarantine:renamed":
            armed = False
            raise InjectedFault(event)

    harness, generations, pointers, receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    with pytest.raises(CommitUnknown):
        gc.execute(
            gc_grant,
            plan,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            occurred_at=START,
            monotonic_ns=30_002,
        )
    harness.leases.release(gc_grant)

    promote = acquire(harness, "PROMOTE", tick=4)
    current = receipts["gen-current"]
    stale = PointerCAS(
        expected_pointer_revision=1,
        expected_active_source_revision=promote.active_source_revision,
        expected_source_epoch=1,
        expected_operation_epoch=promote.operation_epoch,
        expected_migration_epoch=promote.migration_epoch,
        expected_state_schema_version=1,
        expected_fence_token=int(promote.lease.to_dict()["fence_token"]),
        candidate_generation_id="gen-current",
        candidate_receipt_sha256=current.sha256,
        expected_current_receipt_sha256=current.sha256,
    )
    with pytest.raises(PointerRecoveryRequired, match="GC intent"):
        pointers.promote(
            promote,
            stale,
            occurred_at=START,
            monotonic_ns=40_001,
        )
    harness.leases.release(promote)

    recovery = acquire(harness, "POINTER_RECOVERY", tick=5)
    completion = gc.reconcile(
        recovery,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        completed_at=START,
        monotonic_ns=50_001,
    )
    assert completion is not None and completion.quarantined == ("gen-unused",)
    assert not (harness.state_root / "workspaces" / REPO_UUID / "gc/intent.json").exists()


@pytest.mark.parametrize(("field", "reason"), PROTECTION_FIELDS)
def test_gc_protects_each_caller_owned_reachability_class(
    tmp_path: Path,
    field: str,
    reason: str,
) -> None:
    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    values = {
        name: (frozenset({"gen-unused"}) if name == field else frozenset())
        for name, _reason in PROTECTION_FIELDS
    }
    protections = GcProtection(**values)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
    )

    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=protections,
        monotonic_ns=30_001,
    )

    assert "gen-unused" not in plan.candidates
    assert dict(plan.protected)["gen-unused"] == (reason,)
    harness.leases.release(gc_grant)


def test_killed_reader_releases_kernel_protection_without_a_durable_pin(
    tmp_path: Path,
) -> None:
    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
    )
    lock = generations.state.path(generations._lock(REPO_UUID, "gen-unused"))
    before = tree_snapshot(harness.state_root)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time; fd=os.open(sys.argv[1], os.O_RDONLY); "
                "fcntl.flock(fd, fcntl.LOCK_SH); print('READY', flush=True); time.sleep(60)"
            ),
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "READY"
    holder.terminate()
    holder.wait(timeout=5)

    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )

    assert plan.candidates == ("gen-unused",)
    assert tree_snapshot(harness.state_root) == before
    harness.leases.release(gc_grant)


@pytest.mark.parametrize("phase", GC_SERIALIZATION_PHASES)
@pytest.mark.parametrize("successor_operation", SUCCESSOR_WRITER_OPERATIONS)
def test_gc_phase_holds_the_exclusive_workspace_domain_against_successor_writers(
    tmp_path: Path,
    phase: str,
    successor_operation: str,
) -> None:
    paused = threading.Event()
    resume = threading.Event()
    armed = False

    def pause_at_phase(event: str) -> None:
        nonlocal armed
        if armed and event == phase:
            armed = False
            paused.set()
            if not resume.wait(timeout=5):
                raise TimeoutError(f"successor writer did not release {phase}")

    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
        fault_hook=pause_at_phase,
    )
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    armed = True
    successor_started = threading.Event()

    def acquire_successor():
        successor_started.set()
        return acquire(harness, successor_operation, tick=200)

    with ThreadPoolExecutor(max_workers=2) as executor:
        collection = executor.submit(
            gc.execute,
            gc_grant,
            plan,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            occurred_at=START,
            monotonic_ns=30_002,
        )
        assert paused.wait(timeout=5), phase
        successor = executor.submit(acquire_successor)
        assert successor_started.wait(timeout=5)
        assert not successor.done()
        resume.set()
        completion = collection.result(timeout=5)
        successor_grant = successor.result(timeout=5)

    assert completion.quarantined == ("gen-unused",)
    assert successor_grant.lease.to_dict()["operation"] == successor_operation
    harness.leases.release(successor_grant)


@pytest.mark.parametrize("phase", GC_RECOVERY_PHASES)
def test_gc_reconciles_every_durable_intent_quarantine_and_completion_boundary(
    tmp_path: Path,
    phase: str,
) -> None:
    armed = True

    def fail_at_phase(event: str) -> None:
        nonlocal armed
        if armed and event == phase:
            armed = False
            raise InjectedFault(event)

    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail_at_phase,
    )
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    with pytest.raises((CommitUnknown, InjectedFault)):
        gc.execute(
            gc_grant,
            plan,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            occurred_at=START,
            monotonic_ns=30_002,
        )
    harness.leases.release(gc_grant)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=4)

    reconciled = gc.reconcile(
        recovery,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        completed_at=START,
        monotonic_ns=40_001,
    )

    if reconciled is not None:
        assert reconciled.quarantined == ("gen-unused",)
    assert not (harness.state_root / "workspaces" / REPO_UUID / "gc/intent.json").exists()
    assert not (harness.state_root / "workspaces" / REPO_UUID / "generations/gen-unused").exists()
    assert (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "quarantine"
        / "gc"
        / f"gen-unused.{gc_grant.operation_epoch}"
    ).is_dir()
    harness.leases.release(recovery)


@pytest.mark.parametrize(
    "phase",
    ["gc:gen-unused:purged", "gc:purge:installed", "gc:purge_complete"],
)
def test_gc_purge_retries_after_each_visibility_boundary(tmp_path: Path, phase: str) -> None:
    armed = False

    def fail_at_phase(event: str) -> None:
        nonlocal armed
        if armed and event == phase:
            armed = False
            raise InjectedFault(event)

    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail_at_phase,
    )
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    gc.execute(
        gc_grant,
        plan,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        occurred_at=START,
        monotonic_ns=30_002,
    )
    armed = True
    with pytest.raises((CommitUnknown, InjectedFault)):
        gc.purge(
            gc_grant,
            plan_sha256=plan.sha256,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=30_003,
        )

    purge = gc.purge(
        gc_grant,
        plan_sha256=plan.sha256,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        completed_at=START,
        monotonic_ns=30_004,
    )

    assert purge.purged == ("gen-unused",)
    assert not (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "quarantine"
        / "gc"
        / f"gen-unused.{gc_grant.operation_epoch}"
    ).exists()
