from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    CapacityPolicy,
    GcIntentState,
    payload_manifest_sha256,
)
from graphify.workspace.gc import (
    GC_PREVIEW_MAX_GENERATIONS,
    GcError,
    GcProtection,
    GcRecoveryRequired,
    GcStore,
)
from graphify.workspace.generations import CertificationRequest, GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseRecoveryRequired
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    InjectedFault,
    PosixSyscalls,
    StateCorrupt,
    StatePathError,
)
from graphify.workspace.pointers import PointerCAS, PointerStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy, SemanticQueueStore

from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    REPO_UUID,
    START,
    SUPPORTED,
    acquire,
    create_harness,
    metadata_snapshot,
    trust_source_observations,
    tree_snapshot,
)


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
    "gc:completion_epoch:installed",
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
POST_CRASH_BLOCKED_OPERATIONS = (
    "ACTIVATE",
    "BUILD",
    "MIGRATE",
    "PROMOTE",
    "REPAIR",
    "ROLLBACK",
)


class _FailOncePurgeSyscalls(PosixSyscalls):
    def __init__(self, operation: str, error_number: int) -> None:
        self.operation = operation
        self.error_number = error_number
        self.failed = False

    def _fail(self, operation: str) -> None:
        if not self.failed and self.operation == operation:
            self.failed = True
            raise OSError(self.error_number, f"injected purge {operation}")

    def unlink(self, path: Path) -> None:
        self._fail("unlink")
        super().unlink(path)

    def unlink_at(self, path: str, *, dir_fd: int) -> None:
        self._fail("unlink")
        super().unlink_at(path, dir_fd=dir_fd)

    def rmdir(self, path: Path) -> None:
        self._fail("rmdir")
        super().rmdir(path)

    def rmdir_at(self, path: str, *, dir_fd: int) -> None:
        self._fail("rmdir")
        super().rmdir_at(path, dir_fd=dir_fd)

    def fsync(self, descriptor: int) -> None:
        self._fail("fsync")
        super().fsync(descriptor)


def _runtime(tmp_path: Path):
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    semantic_queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=SemanticQueuePolicy(
            max_items=16,
            max_bytes=64 * 1024,
            retry_budget=1,
        ),
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue=semantic_queue,
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
        source_commit = hashlib.sha1(generation_id.encode()).hexdigest()
        observation_manifest_sha256 = hashlib.sha256(generation_id.encode()).hexdigest()
        observation = SourceObservation(
            source_commit=source_commit,
            inventory_sha256=observation_manifest_sha256,
            policy_sha256="1" * 64,
            detector_id="test-workspace-gc",
            stable_inventory_passes=2,
            entries=(),
        )
        source_observations = (observation, observation)
        queue_watermark = offset + 1
        semantic_queue.reconcile(
            build,
            (),
            source_epoch=1,
            policy_sha256="1" * 64,
            source_observations=source_observations,
            desired_watermark=queue_watermark,
            semantic_required=False,
            monotonic_ns=10_002 + offset * 2,
        )
        semantic_queue.bind_sealed_inputs(
            build,
            sealed_input_manifest_sha256=payload_manifest_sha256(
                "graphify-out", entries
            ),
            monotonic_ns=10_002 + offset * 2,
        )
        request = CertificationRequest(
            source_commit=source_commit,
            source_epoch=1,
            policy_sha256="1" * 64,
            observation_manifest_sha256=observation_manifest_sha256,
            queue_watermark=queue_watermark,
            semantic_completeness="not_required",
            compatibility_sha256=COMPATIBILITY_SHA256,
            validations=(
                "payload_manifest",
                "coordination_lock_precreated",
                "stable_semantic_queue",
            ),
        )
        trust_source_observations(generations, source_observations)
        receipts[generation_id] = generations.certify(
            build,
            allocation,
            request,
            source_observations=source_observations,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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


class _BoundedScandir:
    """A hostile directory iterator that fails if enumeration reads past a bound."""

    def __init__(self, names: tuple[str, ...], *, maximum_entries: int) -> None:
        self.names = names
        self.maximum_entries = maximum_entries
        self.consumed = 0

    def __enter__(self) -> _BoundedScandir:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> _BoundedScandir:
        return self

    def __next__(self) -> Any:
        if self.consumed >= self.maximum_entries + 1:
            raise AssertionError("generation enumeration consumed beyond maximum + 1")
        if self.consumed >= len(self.names):
            raise StopIteration
        name = self.names[self.consumed]
        self.consumed += 1
        return type("Entry", (), {"name": name})()


def test_private_generation_enumeration_stops_at_maximum_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)
    generations = state.ensure_directory("generations")
    names = tuple(f"generation-{index}" for index in range(4))
    for name in names:
        (generations / name).mkdir(mode=0o700)
    maximum = 2
    scandir = _BoundedScandir(names, maximum_entries=maximum)
    monkeypatch.setattr("graphify.workspace.persistence.os.scandir", lambda _fd: scandir)

    with pytest.raises(StatePathError):
        state.list_existing_private_directories(
            "generations",
            maximum_entries=maximum,
        )

    assert scandir.consumed == maximum + 1


@pytest.mark.parametrize("unsafe_child", ("linked", "wrong-mode", "unowned"))
def test_private_generation_enumeration_rejects_unsafe_enumerated_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_child: str,
) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)
    generations = state.ensure_directory("generations")
    child = generations / unsafe_child
    if unsafe_child == "linked":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        child.symlink_to(target, target_is_directory=True)
    elif unsafe_child == "wrong-mode":
        child.mkdir(mode=0o755)
        child.chmod(0o755)
    else:
        child.mkdir(mode=0o700)
        original_require_owner = DurableStateRoot._require_owner

        def reject_unowned(details: os.stat_result, path: Path) -> None:
            if path == child:
                raise StatePathError(f"state path is not owned by the current user: {path}")
            original_require_owner(details, path)

        monkeypatch.setattr(
            DurableStateRoot,
            "_require_owner",
            staticmethod(reject_unowned),
        )

    with pytest.raises(StatePathError):
        state.list_existing_private_directories(
            "generations",
            maximum_entries=2,
        )


def test_gc_preview_and_plan_apply_the_same_generation_enumeration_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    observed_bounds: list[int | None] = []
    original_list = gc.state.list_existing_private_directories

    def capture_bound(
        relative: str | Path,
        *,
        allow_missing: bool = False,
        maximum_entries: int | None = None,
    ) -> tuple[str, ...]:
        observed_bounds.append(maximum_entries)
        return original_list(
            relative,
            allow_missing=allow_missing,
            maximum_entries=maximum_entries,
        )

    monkeypatch.setattr(gc.state, "list_existing_private_directories", capture_bound)

    gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    pointer = pointers.load(REPO_UUID)
    assert pointer is not None
    gc.preview(
        REPO_UUID,
        expected_registry_revision=gc_grant.registry_revision,
        expected_active_source_revision=gc_grant.active_source_revision,
        expected_operation_epoch=gc_grant.operation_epoch,
        expected_migration_epoch=gc_grant.migration_epoch,
        expected_pointer_revision=int(pointer.to_dict()["pointer_revision"]),
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        deadline_ns=time.monotonic_ns() + 5_000_000_000,
    )

    assert observed_bounds == [
        GC_PREVIEW_MAX_GENERATIONS,
        GC_PREVIEW_MAX_GENERATIONS,
        GC_PREVIEW_MAX_GENERATIONS,
    ]


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
    queue = generations.semantic_queue
    assert queue is not None
    current_binding = queue.state.path(
        queue._certification_binding_path(REPO_UUID, "gen-current")
    )
    unused_binding = queue.state.path(
        queue._certification_binding_path(REPO_UUID, "gen-unused")
    )
    assert current_binding.is_file()
    assert unused_binding.is_file()
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

    gc_directory = harness.state_root / "workspaces" / REPO_UUID / "gc"
    gc_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace_directory = harness.state_root / "workspaces" / REPO_UUID
    orphans = (
        harness.state_root / f".registry.json.tmp-121-{'a' * 32}",
        workspace_directory / f".workspace.json.tmp-122-{'b' * 32}",
        gc_directory / f".intent.json.tmp-123-{'c' * 32}",
    )
    for orphan in orphans:
        orphan.write_bytes(b"orphan")
        orphan.chmod(0o600)
    before = metadata_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_002,
    )
    assert metadata_snapshot(harness.state_root) == before
    assert metadata_snapshot(harness.state_root) == before_metadata
    assert all(orphan.read_bytes() == b"orphan" for orphan in orphans)
    assert plan.candidates == ("gen-unused",)
    assert "gen-current" in dict(plan.protected)
    assert plan.fence_token == int(gc_grant.lease.to_dict()["fence_token"])
    assert "fence_token" in plan.to_dict()

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
    assert current_binding.is_file()
    assert unused_binding.is_file()
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
    assert current_binding.is_file()
    assert not unused_binding.exists()
    assert retained_lock.stat().st_ino == inode


def test_gc_execute_propagates_deadline_to_candidate_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    observed: list[int | None] = []
    original_locks = gc.state.existing_generation_locks

    def capture_deadline(
        locks: list[tuple[str, Path]],
        *,
        exclusive: bool,
        blocking: bool = True,
        deadline_ns: int | None = None,
    ) -> Any:
        observed.append(deadline_ns)
        return original_locks(
            locks,
            exclusive=exclusive,
            blocking=blocking,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(gc.state, "existing_generation_locks", capture_deadline)

    gc.execute(
        gc_grant,
        plan,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        occurred_at=START,
        monotonic_ns=30_002,
        deadline_ns=deadline_ns,
    )

    assert observed == [deadline_ns]


def test_gc_execute_rejects_dangling_completion_before_quarantine(
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
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    completion_relative = gc._completion_path(REPO_UUID, plan.sha256)
    gc.state.ensure_directory(completion_relative.parent)
    completion_path = gc.state.path(completion_relative)
    completion_path.symlink_to(tmp_path / "missing-completion-record")
    generation = gc.state.path(generations._generation(REPO_UUID, "gen-unused"))

    with pytest.raises(GcRecoveryRequired, match="completion is invalid"):
        gc.execute(
            gc_grant,
            plan,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            occurred_at=START,
            monotonic_ns=30_002,
        )

    assert completion_path.is_symlink()
    assert generation.is_dir()
    assert not gc.state.path(gc._intent_path(REPO_UUID)).exists()
    assert not gc.state.path(
        gc._quarantine(REPO_UUID, "gen-unused", gc_grant.operation_epoch)
    ).exists()


def test_gc_purge_rejects_dangling_record_before_quarantine_deletion(
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
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    completion = gc.execute(
        gc_grant,
        plan,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        occurred_at=START,
        monotonic_ns=30_002,
    )
    quarantine = gc.state.path(
        gc._quarantine(
            REPO_UUID,
            "gen-unused",
            completion.operation_epoch,
        )
    )
    purge_relative = gc._purge_path(REPO_UUID, plan.sha256)
    gc.state.ensure_directory(purge_relative.parent)
    purge_path = gc.state.path(purge_relative)
    purge_path.symlink_to(tmp_path / "missing-purge-record")
    before_quarantine = tree_snapshot(quarantine)
    before_quarantine_metadata = metadata_snapshot(quarantine)

    with pytest.raises(GcError, match="purge record is invalid"):
        gc.purge(
            gc_grant,
            plan_sha256=plan.sha256,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=30_003,
        )

    assert purge_path.is_symlink()
    assert tree_snapshot(quarantine) == before_quarantine
    assert metadata_snapshot(quarantine) == before_quarantine_metadata


def test_gc_purge_rejects_linked_quarantine_parent_without_external_deletion(
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
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    completion = gc.execute(
        gc_grant,
        plan,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        occurred_at=START,
        monotonic_ns=30_002,
    )
    quarantine_relative = gc._quarantine(
        REPO_UUID,
        "gen-unused",
        completion.operation_epoch,
    )
    quarantine_parent = gc.state.path(quarantine_relative.parent)
    external = tmp_path / "outside-purge-quarantine"
    quarantine_parent.rename(external)
    quarantine_parent.symlink_to(external, target_is_directory=True)
    victim = external / f"gen-unused.{completion.operation_epoch}" / "graphify-out" / "graph.json"
    before = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    before_external = tree_snapshot(external)
    before_external_metadata = metadata_snapshot(external)

    with pytest.raises(GcError, match="quarantine path is unsafe"):
        gc._remove_quarantine(quarantine_relative)
    assert victim.is_file()
    assert tree_snapshot(harness.state_root) == before
    assert metadata_snapshot(harness.state_root) == before_metadata
    assert tree_snapshot(external) == before_external
    assert metadata_snapshot(external) == before_external_metadata

    with pytest.raises(GcError, match="quarantine path is unsafe"):
        gc.purge(
            gc_grant,
            plan_sha256=plan.sha256,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=30_003,
        )
    assert victim.is_file()
    assert tree_snapshot(harness.state_root) == before
    assert tree_snapshot(external) == before_external
    assert metadata_snapshot(external) == before_external_metadata


def test_gc_plan_rejects_insecure_root_without_repairing_it(tmp_path: Path) -> None:
    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
    )
    harness.state_root.chmod(0o755)
    before = metadata_snapshot(harness.state_root)

    with pytest.raises(StatePathError, match="mode is not 0700"):
        gc.plan(
            gc_grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=30_001,
        )

    assert metadata_snapshot(harness.state_root) == before


def test_existing_fifo_lock_is_rejected_without_blocking_or_mutation(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    lock = harness.state_root / "registry.lock"
    lock.unlink()
    lock.parent.chmod(0o700)
    os.mkfifo(lock, 0o600)
    before = tree_snapshot(harness.state_root)
    timed_out = False

    def interrupt(_signum: int, _frame: Any) -> None:
        nonlocal timed_out
        timed_out = True
        raise TimeoutError("blocking FIFO open")

    previous_handler = signal.signal(signal.SIGALRM, interrupt)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.25)
    try:
        with pytest.raises(StatePathError):
            harness.registry.load()
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)

    assert not timed_out
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize(
    ("unsafe_path", "error"),
    (
        ("evidence_symlink", StateCorrupt),
        ("evidence_mode", StateCorrupt),
        ("gc_symlink", LeaseRecoveryRequired),
        ("generations_mode", StatePathError),
        ("generation_mode", StatePathError),
    ),
)
def test_gc_plan_rejects_unsafe_state_directory_chains_without_mutation(
    tmp_path: Path,
    unsafe_path: str,
    error: type[Exception],
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
    external: Path | None = None
    if unsafe_path.startswith("evidence"):
        target = harness.state_root / "evidence"
    elif unsafe_path == "gc_symlink":
        target = harness.state_root / "workspaces" / REPO_UUID / "gc"
        target.mkdir(mode=0o700)
    elif unsafe_path == "generations_mode":
        target = harness.state_root / "workspaces" / REPO_UUID / "generations"
    else:
        target = harness.state_root / "workspaces" / REPO_UUID / "generations" / "gen-current"
    if unsafe_path.endswith("symlink"):
        external = tmp_path / f"outside-{unsafe_path}"
        target.rename(external)
        target.symlink_to(external, target_is_directory=True)
    else:
        target.chmod(0o755)

    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    before_external_tree = tree_snapshot(external) if external is not None else None
    before_external_metadata = metadata_snapshot(external) if external is not None else None

    with pytest.raises(error):
        gc.plan(
            gc_grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=30_001,
        )

    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata
    if external is not None:
        assert tree_snapshot(external) == before_external_tree
        assert metadata_snapshot(external) == before_external_metadata


def test_gc_plan_preserves_legacy_error_for_missing_candidate_lock(
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
    lock.unlink()
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)

    with pytest.raises(GcError) as raised:
        gc.plan(
            gc_grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=30_001,
        )

    assert type(raised.value) is GcError
    assert raised.value.code == "gc_error"
    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata


@pytest.mark.parametrize(
    ("current_relative", "pending_relative"),
    [
        ("registry.json", "registry.pending.json"),
        (
            f"workspaces/{REPO_UUID}/workspace.json",
            f"workspaces/{REPO_UUID}/workspace.pending.json",
        ),
    ],
)
def test_gc_plan_fails_closed_on_pending_recovery_without_mutation(
    tmp_path: Path,
    current_relative: str,
    pending_relative: str,
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
    current = harness.state_root / current_relative
    pending = harness.state_root / pending_relative
    pending.write_bytes(current.read_bytes())
    pending.chmod(0o600)
    before = metadata_snapshot(harness.state_root)

    with pytest.raises(StateCorrupt, match="unresolved pending commit"):
        gc.plan(
            gc_grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=30_001,
        )

    assert metadata_snapshot(harness.state_root) == before


def test_gc_entry_points_revalidate_capacity_policy_before_state_access(
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
    valid_plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    invalid = replace(POLICY, reserve_bytes=-1)
    before = metadata_snapshot(harness.state_root)
    calls = (
        lambda: gc.plan(
            gc_grant,
            capacity_policy=invalid,
            protections=EMPTY_PROTECTION,
            monotonic_ns=30_002,
        ),
        lambda: gc.execute(
            gc_grant,
            valid_plan,
            capacity_policy=invalid,
            protections=EMPTY_PROTECTION,
            occurred_at=START,
            monotonic_ns=30_002,
        ),
        lambda: gc.reconcile(
            gc_grant,
            capacity_policy=invalid,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=30_002,
        ),
        lambda: gc.purge(
            gc_grant,
            plan_sha256=valid_plan.sha256,
            capacity_policy=invalid,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=30_002,
        ),
    )

    for call in calls:
        with pytest.raises(GcError, match="capacity policy is invalid"):
            call()
        assert metadata_snapshot(harness.state_root) == before


def test_gc_commit_unknown_blocks_pointer_writer_until_successor_reconciles(
    tmp_path: Path,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "gc:gen-unused:quarantine:renamed":
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

    with pytest.raises(LeaseRecoveryRequired, match="GC intent"):
        acquire(harness, "PROMOTE", tick=4)

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


def test_gc_reconcile_rejects_linked_quarantine_parent_without_clearing_intent(
    tmp_path: Path,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "gc:gen-unused:quarantine:renamed":
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
    quarantine_parent = harness.state_root / "workspaces" / REPO_UUID / "quarantine" / "gc"
    external = tmp_path / "outside-reconcile-quarantine"
    quarantine_parent.rename(external)
    quarantine_parent.symlink_to(external, target_is_directory=True)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=5)
    intent = harness.state_root / "workspaces" / REPO_UUID / "gc" / "intent.json"
    before = tree_snapshot(harness.state_root)
    before_external = tree_snapshot(external)
    before_external_metadata = metadata_snapshot(external)

    with pytest.raises(StatePathError):
        gc.reconcile(
            recovery,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=50_001,
        )

    assert intent.is_file()
    assert tree_snapshot(harness.state_root) == before
    assert tree_snapshot(external) == before_external
    assert metadata_snapshot(external) == before_external_metadata


@pytest.mark.parametrize("operation", POST_CRASH_BLOCKED_OPERATIONS)
def test_unresolved_gc_intent_blocks_every_nonrecovery_workspace_operation(
    tmp_path: Path,
    operation: str,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "gc:gen-unused:quarantine:renamed":
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

    with pytest.raises(LeaseRecoveryRequired, match="GC intent"):
        acquire(harness, operation, tick=4)

    recovery = acquire(harness, "POINTER_RECOVERY", tick=5)
    assert gc.reconcile(
        recovery,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        completed_at=START,
        monotonic_ns=50_001,
    ) is not None
    harness.leases.release(recovery)


def test_gc_recovery_rejects_intent_bound_to_another_workspace_path(
    tmp_path: Path,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "gc:intent_durable":
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
        fault_hook=fail,
    )
    plan = gc.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    with pytest.raises(InjectedFault):
        gc.execute(
            gc_grant,
            plan,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            occurred_at=START,
            monotonic_ns=30_002,
        )
    harness.leases.release(gc_grant)
    intent_path = gc.state.path(gc._intent_path(REPO_UUID))
    value = GcIntentState.from_json(intent_path.read_bytes()).to_dict()
    value["repo_uuid"] = "22222222-2222-4222-8222-222222222222"
    intent_path.write_bytes(GcIntentState.from_mapping(value).canonical)
    intent_path.chmod(0o600)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=4)

    with pytest.raises(GcRecoveryRequired, match="another workspace"):
        gc.reconcile(
            recovery,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=40_001,
        )

    assert (
        harness.state_root / "workspaces" / REPO_UUID / "generations" / "gen-unused"
    ).is_dir()
    harness.leases.release(recovery)


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


@pytest.mark.parametrize("phase", GC_SERIALIZATION_PHASES)
def test_current_reader_arrives_during_every_gc_phase_without_observing_quarantine(
    tmp_path: Path,
    phase: str,
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
                raise TimeoutError(f"reader did not release {phase}")

    harness, generations, pointers, receipts = _runtime(tmp_path)
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
    with ThreadPoolExecutor(max_workers=1) as executor:
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
        with pointers.read_current(REPO_UUID) as read:
            assert read.receipt.sha256 == receipts["gen-current"].sha256
            assert read.generation_path.name == "gen-current"
        resume.set()
        completion = collection.result(timeout=5)

    assert completion.quarantined == ("gen-unused",)
    loaded = pointers.load(REPO_UUID)
    assert loaded is not None
    assert loaded.to_dict()["current"]["generation_id"] == "gen-current"
    harness.leases.release(gc_grant)


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
    [
        "gc:gen-unused:purged",
        "gc:gen-unused:semantic_binding:unlinked",
        "gc:gen-unused:semantic_binding:parent_durable",
        "gc:gen-unused:semantic_binding_removed",
        "gc:purge:installed",
        "gc:purge_complete",
    ],
)
def test_gc_purge_retries_after_each_visibility_boundary(tmp_path: Path, phase: str) -> None:
    armed = False
    events: list[str] = []

    def fail_at_phase(event: str) -> None:
        nonlocal armed
        events.append(event)
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
    assert "gc:gen-unused:semantic_binding_parent_durable" in events
    binding = SemanticQueueStore._certification_binding_path(REPO_UUID, "gen-unused")
    assert not gc.state.path(binding).exists()
    assert not (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "quarantine"
        / "gc"
        / f"gen-unused.{gc_grant.operation_epoch}"
    ).exists()


@pytest.mark.parametrize(
    ("operation", "error_number"),
    [
        ("unlink", errno.EIO),
        ("unlink", errno.EINTR),
        ("rmdir", errno.EIO),
        ("rmdir", errno.EINTR),
        ("fsync", errno.EIO),
    ],
)
def test_gc_purge_syscall_failures_retry_to_one_durable_record(
    tmp_path: Path,
    operation: str,
    error_number: int,
) -> None:
    harness, generations, pointers, _receipts = _runtime(tmp_path)
    gc_grant = acquire(harness, "GC", tick=3)
    baseline = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
    )
    plan = baseline.plan(
        gc_grant,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        monotonic_ns=30_001,
    )
    baseline.execute(
        gc_grant,
        plan,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        occurred_at=START,
        monotonic_ns=30_002,
    )
    syscalls = _FailOncePurgeSyscalls(operation, error_number)
    faulty = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=harness.leases.state.capabilities,
        syscalls=syscalls,
    )

    with pytest.raises(OSError) as raised:
        faulty.purge(
            gc_grant,
            plan_sha256=plan.sha256,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            completed_at=START,
            monotonic_ns=30_003,
        )
    assert raised.value.errno == error_number

    purge = faulty.purge(
        gc_grant,
        plan_sha256=plan.sha256,
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        completed_at=START,
        monotonic_ns=30_004,
    )
    assert purge.purged == ("gen-unused",)
    purge_path = faulty.state.path(faulty._purge_path(REPO_UUID, plan.sha256))
    assert purge_path.is_file()
