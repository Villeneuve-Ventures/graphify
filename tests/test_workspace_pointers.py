from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any, cast

import pytest

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.contracts import (
    CapacityPolicy,
    CompatibilityManifest,
    PriorPointerRecord,
)
from graphify.workspace.generations import (
    CertificationRequest,
    GenerationError,
    GenerationStore,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.journal import JournalConflict, JournalStore
from graphify.workspace.leases import LeaseRecoveryRequired
from graphify.workspace.persistence import CommitUnknown, InjectedFault, LockOrderError
from graphify.workspace.pointers import (
    PointerCAS,
    PointerConflict,
    PointerCorrupt,
    PointerRecoveryRequired,
    PointerStore,
    PointerSuperseded,
)

from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    REPO_UUID,
    START,
    acquire,
    authorization,
    create_harness,
    create_repo,
    tree_snapshot,
)


SECOND_UUID = "22222222-2222-4222-8222-222222222222"


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


def _alternate_manifest() -> CompatibilityManifest:
    return cast(
        CompatibilityManifest,
        CompatibilityManifest.from_mapping(
            {
                **COMPATIBILITY_MANIFEST.to_dict(),
                "distribution_build": "alternate-published-build",
            }
        ),
    )


def _request(commit: str) -> CertificationRequest:
    return CertificationRequest(
        source_commit=commit,
        source_epoch=1,
        policy_sha256="1" * 64,
        observation_manifest_sha256="2" * 64,
        queue_watermark=0,
        semantic_completeness="not_required",
        compatibility_sha256=COMPATIBILITY_SHA256,
        validations=("payload_manifest", "coordination_lock_precreated"),
    )


def _certify(
    store: GenerationStore,
    grant: Any,
    generation_id: str,
    content: str,
    *,
    monotonic_ns: int,
    compatibility_sha256: str = COMPATIBILITY_SHA256,
):
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id=generation_id,
        occurred_at=START,
        monotonic_ns=monotonic_ns,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text(content, encoding="utf-8")
    entries = store.inspect_staged_payload(allocation)
    return store.certify(
        grant,
        allocation,
        replace(
            _request(hashlib.sha1(generation_id.encode()).hexdigest()),
            compatibility_sha256=compatibility_sha256,
        ),
        declared_entries=entries,
        occurred_at=START,
        monotonic_ns=monotonic_ns + 1,
    )


def _cas(grant: Any, receipt: Any, *, revision: int, current_sha256: str | None) -> PointerCAS:
    value = receipt.to_dict()
    return PointerCAS(
        expected_pointer_revision=revision,
        expected_active_source_revision=grant.active_source_revision,
        expected_source_epoch=int(value["source_epoch"]),
        expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        expected_state_schema_version=1,
        expected_fence_token=int(grant.lease.to_dict()["fence_token"]),
        candidate_generation_id=str(value["generation_id"]),
        candidate_receipt_sha256=receipt.sha256,
        expected_current_receipt_sha256=current_sha256,
    )


def test_pointer_gc_barrier_rejects_linked_parent_without_mutation(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    workspace = harness.state_root / "workspaces" / REPO_UUID
    gc_directory = workspace / "gc"
    gc_directory.mkdir(mode=0o700)
    intent = gc_directory / "intent.json"
    intent.write_bytes(b"retained intent\n")
    intent.chmod(0o600)
    gc_directory.rename(workspace / "gc.parked")
    external = tmp_path / "external-gc"
    external.mkdir(mode=0o700)
    gc_directory.symlink_to(external, target_is_directory=True)
    before = tree_snapshot(harness.state_root)
    before_external = tree_snapshot(external)

    with pytest.raises(PointerCorrupt, match="pointer state path is unsafe"):
        pointers._assert_no_gc_intent(REPO_UUID)

    assert tree_snapshot(harness.state_root) == before
    assert tree_snapshot(external) == before_external


def _promotion_runtime(
    tmp_path: Path,
    *,
    fault_hook: Any = None,
):
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    receipts = {
        generation_id: _certify(
            generations,
            build,
            generation_id,
            f"{generation_id}\n",
            monotonic_ns=10_001 + offset * 2,
        )
        for offset, generation_id in enumerate(("gen-old", "gen-new", "gen-racer"))
    }
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
    )
    return harness, journal, generations, pointers, promote, receipts


def test_raw_journal_caller_cannot_append_pointer_owned_transition(
    tmp_path: Path,
) -> None:
    _harness, journal, _generations, pointers, promote, receipts = _promotion_runtime(
        tmp_path
    )
    receipt = receipts["gen-new"]
    pointer = pointers.promote(
        promote,
        _cas(promote, receipt, revision=0, current_sha256=None),
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )

    with pytest.raises(JournalConflict, match="PROMOTED must be appended by PointerStore"):
        journal.append(
            promote,
            transition="PROMOTED",
            generation_id="gen-new",
            receipt_sha256=receipt.sha256,
            pointer_revision=int(pointer.to_dict()["pointer_revision"]),
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=20_002,
        )


def test_promotion_rejects_different_manifest_generation_without_state_mutation(
    tmp_path: Path,
) -> None:
    alternate_manifest = _alternate_manifest()
    assert alternate_manifest.sha256 != COMPATIBILITY_SHA256
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    alternate_generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=alternate_manifest,
        capabilities=harness.leases.state.capabilities,
    )
    receipt = _certify(
        alternate_generations,
        build,
        "gen-alternate-manifest",
        "alternate\n",
        monotonic_ns=10_002,
        compatibility_sha256=alternate_manifest.sha256,
    )
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    current_generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        current_generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        UnsupportedCompatibility,
        match="pointer receipt does not match",
    ):
        pointers.promote(
            promote,
            _cas(promote, receipt, revision=0, current_sha256=None),
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=20_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_rollback_rejects_different_manifest_generation_without_state_mutation(
    tmp_path: Path,
) -> None:
    alternate_manifest = _alternate_manifest()
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    current_generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    current = _certify(
        current_generations,
        build,
        "gen-current-manifest",
        "current\n",
        monotonic_ns=10_001,
    )
    alternate_generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=alternate_manifest,
        capabilities=harness.leases.state.capabilities,
    )
    alternate = _certify(
        alternate_generations,
        build,
        "gen-rollback-alternate",
        "alternate\n",
        monotonic_ns=10_003,
        compatibility_sha256=alternate_manifest.sha256,
    )
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        current_generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    pointers.promote(
        promote,
        _cas(promote, current, revision=0, current_sha256=None),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    rollback = acquire(harness, "ROLLBACK", tick=3)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        UnsupportedCompatibility,
        match="pointer receipt does not match",
    ):
        pointers.rollback(
            rollback,
            _cas(rollback, alternate, revision=1, current_sha256=current.sha256),
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=30_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_recovery_rejects_incompatible_pending_pointer_without_state_mutation(
    tmp_path: Path,
) -> None:
    alternate_manifest = _alternate_manifest()
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    alternate_generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=alternate_manifest,
        capabilities=harness.leases.state.capabilities,
    )
    alternate = _certify(
        alternate_generations,
        build,
        "gen-pending-alternate",
        "alternate\n",
        monotonic_ns=10_001,
        compatibility_sha256=alternate_manifest.sha256,
    )
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)

    def interrupt_pending(event: str) -> None:
        if event == "pointer:promoted:pending_durable":
            raise InjectedFault(event)

    alternate_pointers = PointerStore(
        harness.state_root,
        harness.leases,
        alternate_generations,
        journal,
        compatibility_manifest=alternate_manifest,
        capabilities=harness.leases.state.capabilities,
        fault_hook=interrupt_pending,
    )
    with pytest.raises(InjectedFault, match="pending_durable"):
        alternate_pointers.promote(
            promote,
            _cas(promote, alternate, revision=0, current_sha256=None),
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_001,
        )
    harness.leases.release(promote)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)
    current_generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    current_pointers = PointerStore(
        harness.state_root,
        harness.leases,
        current_generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    pending_path = current_pointers.state.path(current_pointers._pending(REPO_UUID))
    assert pending_path.is_file()
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        UnsupportedCompatibility,
        match="pointer receipt does not match",
    ):
        current_pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=30_001,
        )

    assert tree_snapshot(harness.state_root) == before
    assert pending_path.is_file()


def test_two_certified_candidates_race_promotion_and_only_one_wins(tmp_path: Path) -> None:
    harness, journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    barrier = threading.Barrier(2)

    def race(generation_id: str) -> tuple[str, str]:
        receipt = receipts[generation_id]
        barrier.wait(timeout=5)
        try:
            pointer = pointers.promote(
                promote,
                _cas(promote, receipt, revision=0, current_sha256=None),
                occurred_at=START + timedelta(seconds=2),
                monotonic_ns=20_001,
            )
        except PointerSuperseded:
            return ("superseded", generation_id)
        return ("promoted", str(pointer.to_dict()["current"]["generation_id"]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(race, ("gen-new", "gen-racer")))

    assert [outcome[0] for outcome in outcomes] == ["promoted", "superseded"]
    winner = next(outcome[1] for outcome in outcomes if outcome[0] == "promoted")
    loaded = pointers.load(REPO_UUID)
    assert loaded is not None
    assert loaded.to_dict()["pointer_revision"] == 1
    assert loaded.to_dict()["current"]["generation_id"] == winner
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(promote, monotonic_ns=20_002).events
        if event.to_dict()["generation_id"] in {"gen-new", "gen-racer"}
    ]
    assert transitions.count("PROMOTED") == 1
    assert transitions.count("SUPERSEDED") == 1
    harness.leases.release(promote)


@pytest.mark.parametrize(
    ("field", "stale_value"),
    [
        ("expected_pointer_revision", 0),
        ("expected_active_source_revision", 2),
        ("expected_source_epoch", 2),
        ("expected_operation_epoch", 2),
        ("expected_migration_epoch", 2),
        ("expected_state_schema_version", 2),
        ("expected_fence_token", 2),
        ("expected_current_receipt_sha256", "f" * 64),
    ],
)
def test_idempotent_replay_rejects_every_stale_cas_field(
    tmp_path: Path,
    field: str,
    stale_value: int | str,
) -> None:
    harness, journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    receipt = receipts["gen-old"]
    current = pointers.promote(
        promote,
        _cas(promote, receipt, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    valid_replay = _cas(
        promote,
        receipt,
        revision=1,
        current_sha256=receipt.sha256,
    )

    with pytest.raises((PointerConflict, PointerSuperseded)):
        pointers.promote(
            promote,
            replace(valid_replay, **{field: stale_value}),
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_002,
        )

    loaded = pointers.load(REPO_UUID)
    assert loaded is not None and loaded.canonical == current.canonical
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(promote, monotonic_ns=20_003).for_generation("gen-old")
    ]
    assert transitions.count("PROMOTED") == 1
    assert "SUPERSEDED" not in transitions
    replayed = pointers.promote(
        promote,
        valid_replay,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_004,
    )
    assert replayed.canonical == current.canonical
    harness.leases.release(promote)


@pytest.mark.parametrize(
    "boundary",
    [
        "pointer:promoted:prior:replaced",
        "pointer:promoted:prior_durable",
        "pointer:promoted:pending:replaced",
        "pointer:promoted:pending_durable",
        "pointer:promoted:visible:replaced",
        "pointer:promoted:visible",
        "pointer:promoted:journal_durable",
        "pointer:promoted:complete:unlinked",
    ],
)
def test_reader_during_each_promotion_boundary_observes_one_complete_pointer(
    tmp_path: Path,
    boundary: str,
) -> None:
    paused = threading.Event()
    resume = threading.Event()
    armed = False

    def pause_at_boundary(event: str) -> None:
        nonlocal armed
        if armed and event == boundary:
            armed = False
            paused.set()
            if not resume.wait(timeout=5):
                raise TimeoutError(f"reader schedule did not release {boundary}")

    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(
        tmp_path,
        fault_hook=pause_at_boundary,
    )
    old = receipts["gen-old"]
    new = receipts["gen-new"]
    old_pointer = pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    armed = True
    reader_started = threading.Event()

    def promote_new():
        return pointers.promote(
            promote,
            _cas(promote, new, revision=1, current_sha256=old.sha256),
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_002,
        )

    def read_current():
        reader_started.set()
        with pointers.read_current(REPO_UUID) as read:
            return read.pointer

    with ThreadPoolExecutor(max_workers=2) as executor:
        promotion = executor.submit(promote_new)
        assert paused.wait(timeout=5), boundary
        reader = executor.submit(read_current)
        assert reader_started.wait(timeout=5)
        assert not reader.done()
        resume.set()
        new_pointer = promotion.result(timeout=5)
        observed = reader.result(timeout=5)

    assert observed.canonical in {old_pointer.canonical, new_pointer.canonical}
    assert pointers.verify_pointer(observed)["current"].sha256 in {old.sha256, new.sha256}
    harness.leases.release(promote)


def test_reader_that_locks_first_delays_promotion_without_a_durable_pin(tmp_path: Path) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    old = receipts["gen-old"]
    new = receipts["gen-new"]
    pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    promotion_started = threading.Event()

    def promote_new():
        promotion_started.set()
        return pointers.promote(
            promote,
            _cas(promote, new, revision=1, current_sha256=old.sha256),
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_002,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pointers.read_current(REPO_UUID) as read:
            assert read.receipt.sha256 == old.sha256
            promotion = executor.submit(promote_new)
            assert promotion_started.wait(timeout=5)
            assert not promotion.done()
        promoted = promotion.result(timeout=5)

    assert promoted.to_dict()["current"]["generation_id"] == "gen-new"
    harness.leases.release(promote)


@pytest.mark.parametrize(
    "boundary",
    [
        "pointer:promoted:prior:replaced",
        "pointer:promoted:prior_durable",
        "pointer:promoted:pending:replaced",
        "pointer:promoted:pending_durable",
        "pointer:promoted:visible:replaced",
        "pointer:promoted:visible",
        "pointer:promoted:journal_durable",
        "pointer:promoted:complete:unlinked",
        "pointer:promoted:complete",
    ],
)
def test_pointer_visibility_boundary_failure_leaves_old_new_or_recoverable_intent(
    tmp_path: Path,
    boundary: str,
) -> None:
    armed = False

    def fail_at_boundary(event: str) -> None:
        nonlocal armed
        if armed and event == boundary:
            armed = False
            raise InjectedFault(event)

    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(
        tmp_path,
        fault_hook=fail_at_boundary,
    )
    old = receipts["gen-old"]
    new = receipts["gen-new"]
    pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        pointers.promote(
            promote,
            _cas(promote, new, revision=1, current_sha256=old.sha256),
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_002,
        )

    visible = pointers._read_pointer(pointers._current(REPO_UUID), allow_missing=False)
    assert visible is not None
    assert visible.to_dict()["current"]["generation_id"] in {"gen-old", "gen-new"}
    pointers.verify_pointer(visible)
    pending = harness.state_root / "workspaces" / REPO_UUID / "pointers.pending.json"
    harness.leases.release(promote)
    if pending.exists():
        recovery = acquire(harness, "POINTER_RECOVERY", tick=3)
        repaired = pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=30_001,
        )
        assert repaired.to_dict()["current"]["generation_id"] == "gen-new"
        pointers.verify_pointer(repaired)
        harness.leases.release(recovery)


def test_pointer_promotion_retains_prior_before_visibility_and_loser_is_superseded(
    tmp_path: Path,
) -> None:
    events: list[str] = []
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    first = _certify(generations, build, "gen-a", "a\n", monotonic_ns=10_001)
    second = _certify(generations, build, "gen-b", "b\n", monotonic_ns=10_003)
    loser = _certify(generations, build, "gen-c", "c\n", monotonic_ns=10_005)
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=events.append,
    )

    pointer_one = pointers.promote(
        promote,
        _cas(promote, first, revision=0, current_sha256=None),
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )
    pointer_two = pointers.promote(
        promote,
        _cas(promote, second, revision=1, current_sha256=first.sha256),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=20_002,
    )

    assert pointer_one.to_dict()["pointer_revision"] == 1
    assert pointer_two.to_dict()["pointer_revision"] == 2
    assert pointer_two.to_dict()["last_good"] == {
        "generation_id": "gen-a",
        "receipt_sha256": first.sha256,
    }
    prior_path = harness.state_root / "workspaces" / REPO_UUID / "pointers.previous.json"
    prior = PriorPointerRecord.from_json(prior_path.read_bytes())
    assert prior.to_dict()["pointer_set"] == pointer_one.to_dict()
    prior_index = max(
        index for index, event in enumerate(events) if event == "pointer:promoted:prior_durable"
    )
    visible_index = max(
        index for index, event in enumerate(events) if event == "pointer:promoted:visible"
    )
    assert prior_index < visible_index

    with pytest.raises(PointerSuperseded):
        pointers.promote(
            promote,
            _cas(promote, loser, revision=1, current_sha256=first.sha256),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=20_003,
        )
    loaded = pointers.load(REPO_UUID)
    assert loaded is not None and loaded.canonical == pointer_two.canonical
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(promote, monotonic_ns=20_004).for_generation("gen-c")
    ]
    assert transitions[-1] == "SUPERSEDED"
    with pytest.raises(LockOrderError, match="lexical"):
        with pointers.state.existing_generation_locks(
            [
                ("gen-b", generations._lock(REPO_UUID, "gen-b")),
                ("gen-a", generations._lock(REPO_UUID, "gen-a")),
            ],
            exclusive=True,
        ):
            pass
    lock_barrier = threading.Barrier(2)

    def take_sorted_locks() -> bool:
        lock_barrier.wait(timeout=5)
        with pointers.state.existing_generation_locks(
            [
                ("gen-a", generations._lock(REPO_UUID, "gen-a")),
                ("gen-b", generations._lock(REPO_UUID, "gen-b")),
            ],
            exclusive=True,
        ):
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _index: take_sorted_locks(), range(2))) == [True, True]
    harness.leases.release(promote)
    rollback = acquire(harness, "ROLLBACK", tick=3)
    rolled_back = pointers.rollback(
        rollback,
        _cas(rollback, first, revision=2, current_sha256=second.sha256),
        occurred_at=START + timedelta(seconds=5),
        monotonic_ns=30_001,
    )
    assert rolled_back.to_dict()["pointer_revision"] == 3
    assert rolled_back.to_dict()["current"]["generation_id"] == "gen-a"
    assert rolled_back.to_dict()["last_good"]["generation_id"] == "gen-b"


def test_shared_reader_is_nonmutating_and_holds_retained_generation_lock(tmp_path: Path) -> None:
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    receipt = _certify(generations, build, "gen-reader", "reader\n", monotonic_ns=10_001)
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
    pointers.promote(
        promote,
        _cas(promote, receipt, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    before = tree_snapshot(harness.state_root)
    lock = generations._lock(REPO_UUID, "gen-reader")
    lock_inode = pointers.state.path(lock).stat().st_ino

    with pointers.read_current(REPO_UUID) as read:
        assert read.receipt.sha256 == receipt.sha256
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, os, sys; "
                    "fd=os.open(sys.argv[1], os.O_RDONLY); "
                    "\ntry: fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)"
                    "\nexcept BlockingIOError: raise SystemExit(75)"
                    "\nraise SystemExit(0)"
                ),
                str(pointers.state.path(lock)),
            ],
            check=False,
        )
        assert probe.returncode == 75

    assert tree_snapshot(harness.state_root) == before
    assert pointers.state.path(lock).stat().st_ino == lock_inode
    reopened = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "from graphify.workspace.persistence import RuntimeCapabilities\n"
                "from graphify.workspace.contracts import CompatibilityManifest\n"
                "from graphify.workspace.registry import RegistryStore\n"
                "from graphify.workspace.leases import LeaseStore\n"
                "from graphify.workspace.journal import JournalStore\n"
                "from graphify.workspace.generations import GenerationStore\n"
                "from graphify.workspace.pointers import PointerStore\n"
                "root=Path(__import__('sys').argv[1]); repo=__import__('sys').argv[2]\n"
                "manifest=CompatibilityManifest.from_json(__import__('sys').argv[3].encode())\n"
                "caps=RuntimeCapabilities.supported_test_fixture()\n"
                "registry=RegistryStore(root, capabilities=caps)\n"
                "leases=LeaseStore(root, registry, capabilities=caps)\n"
                "journal=JournalStore(root, leases, capabilities=caps)\n"
                "generations=GenerationStore(root, leases, journal, compatibility_manifest=manifest, capabilities=caps)\n"
                "pointers=PointerStore(root, leases, generations, journal, compatibility_manifest=manifest, capabilities=caps)\n"
                "pointer=pointers.load(repo); receipt=generations.verify_generation(repo, 'gen-reader')\n"
                "print(pointer.to_dict()['pointer_revision'], receipt.sha256)\n"
            ),
            str(harness.state_root),
            REPO_UUID,
            COMPATIBILITY_MANIFEST.canonical.decode("utf-8"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert reopened.stdout.strip() == f"1 {receipt.sha256}"
    assert tree_snapshot(harness.state_root) == before


def test_clean_reboot_identity_reopens_completed_generation_journal_and_pointer(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    receipt = receipts["gen-old"]
    pointer = pointers.promote(
        promote,
        _cas(promote, receipt, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    script = (
        "from datetime import datetime, timezone\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "from graphify.workspace.generations import GenerationStore\n"
        "from graphify.workspace.contracts import CompatibilityManifest\n"
        "from graphify.workspace.journal import JournalStore\n"
        "from graphify.workspace.leases import LeaseOwner, LeaseStore\n"
        "from graphify.workspace.persistence import RuntimeCapabilities\n"
        "from graphify.workspace.pointers import PointerStore\n"
        "from graphify.workspace.registry import RegistryStore\n"
        "class RebootIdentity:\n"
        "    def current_owner(self):\n"
        "        return LeaseOwner('boot-after-clean-reboot', os.getpid(), 'reboot:1')\n"
        "root=Path(sys.argv[1]); repo_uuid=sys.argv[2]\n"
        "manifest=CompatibilityManifest.from_json(sys.argv[3].encode())\n"
        "caps=RuntimeCapabilities.supported_test_fixture()\n"
        "registry=RegistryStore(root, capabilities=caps)\n"
        "leases=LeaseStore(root, registry, capabilities=caps, "
        "identity_provider=RebootIdentity())\n"
        "journal=JournalStore(root, leases, capabilities=caps)\n"
        "generations=GenerationStore(root, leases, journal, compatibility_manifest=manifest, capabilities=caps)\n"
        "pointers=PointerStore(root, leases, generations, journal, compatibility_manifest=manifest, capabilities=caps)\n"
        "document=registry.load(); entry=document.to_dict()['workspaces'][0]\n"
        "state=leases.inspect(repo_uuid)\n"
        "grant=leases.acquire(repo_uuid, 'POINTER_RECOVERY', leases.current_owner(), "
        "expected_registry_revision=int(document.to_dict()['revision']), "
        "expected_active_source_revision=int(entry['active_source_revision']), "
        "expected_operation_epoch=state.operation_epoch, "
        "expected_migration_epoch=state.migration_epoch, "
        "acquired_at=datetime(2026,7,16,19,5,tzinfo=timezone.utc), "
        "monotonic_ns=100, ttl_ns=1000)\n"
        "pointer=pointers.recover(grant, occurred_at=datetime(2026,7,16,19,5,"
        "tzinfo=timezone.utc), monotonic_ns=101)\n"
        "current=pointer.to_dict()['current']; receipt=generations.verify_generation("
        "repo_uuid, current['generation_id'])\n"
        "events=journal.recover(grant, monotonic_ns=102).for_generation("
        "current['generation_id'])\n"
        "leases.release(grant)\n"
        "print(pointer.to_dict()['pointer_revision'], receipt.sha256, len(events))\n"
    )
    rebooted = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(harness.state_root),
            REPO_UUID,
            COMPATIBILITY_MANIFEST.canonical.decode("utf-8"),
        ],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )

    assert rebooted.stdout.strip() == f"1 {receipt.sha256} 6"
    loaded = pointers.load(REPO_UUID)
    assert loaded is not None and loaded.canonical == pointer.canonical


def test_visible_pointer_commit_unknown_repairs_to_new_monotonic_revision(tmp_path: Path) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "pointer:promoted:visible:replaced":
            armed = False
            raise InjectedFault(event)

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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    receipt = _certify(generations, build, "gen-repair", "repair\n", monotonic_ns=10_001)
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    with pytest.raises(CommitUnknown):
        pointers.promote(
            promote,
            _cas(promote, receipt, revision=0, current_sha256=None),
            occurred_at=START,
            monotonic_ns=20_001,
        )
    harness.leases.release(promote)
    with pytest.raises(LeaseRecoveryRequired, match="pointer intent"):
        acquire(harness, "ACTIVATE", tick=3)
    repair = acquire(harness, "POINTER_RECOVERY", tick=3)
    barrier = threading.Barrier(2)

    def recover_one(offset: int):
        barrier.wait(timeout=5)
        return pointers.recover(
            repair,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001 + offset,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        repaired_results = list(executor.map(recover_one, (0, 1)))
    repaired = repaired_results[0]

    assert repaired.to_dict()["pointer_revision"] == 2
    assert all(item.canonical == repaired.canonical for item in repaired_results)
    loaded = pointers.load(REPO_UUID)
    assert loaded is not None and loaded.canonical == repaired.canonical
    assert not (harness.state_root / "workspaces" / REPO_UUID / "pointers.pending.json").exists()
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(repair, monotonic_ns=30_002).for_generation("gen-repair")
    ]
    assert transitions[-1] == "REPAIRED"
    assert transitions.count("REPAIRED") == 1
    harness.leases.release(repair)
    prior = harness.state_root / "workspaces" / REPO_UUID / "pointers.previous.json"
    prior.write_bytes(b"{}\n")
    prior.chmod(0o600)
    corrupt_repair = acquire(harness, "REPAIR", tick=4)
    with pytest.raises(PointerCorrupt, match="prior pointer"):
        pointers.recover(
            corrupt_repair,
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )


@pytest.mark.parametrize(
    ("recovery_failpoint", "expected_revision"),
    [
        ("pointer:repaired:prior_durable", 5),
        ("pointer:repaired:pending_durable", 4),
        ("pointer:repaired:visible", 4),
        ("pointer:repaired:journal_durable", 4),
        ("pointer:gen-new:quarantined", 4),
    ],
)
def test_recovery_quarantines_only_the_corrupt_ref_and_preserves_last_good(
    tmp_path: Path,
    recovery_failpoint: str,
    expected_revision: int,
) -> None:
    failpoint: str | None = None

    def fail_after_journal(event: str) -> None:
        nonlocal failpoint
        if event == failpoint:
            failpoint = None
            raise InjectedFault(event)

    harness, _journal, generations, pointers, promote, receipts = _promotion_runtime(
        tmp_path,
        fault_hook=fail_after_journal,
    )
    old = receipts["gen-old"]
    new = receipts["gen-new"]
    racer = receipts["gen-racer"]
    pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    pointers.promote(
        promote,
        _cas(promote, new, revision=1, current_sha256=old.sha256),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_002,
    )
    corrupt = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / "gen-new"
        / "graphify-out"
        / "graph.json"
    )
    corrupt.write_text("corrupt\n", encoding="utf-8")
    failpoint = "pointer:promoted:journal_durable"
    with pytest.raises(InjectedFault):
        pointers.promote(
            promote,
            _cas(promote, racer, revision=2, current_sha256=new.sha256),
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=20_003,
        )
    harness.leases.release(promote)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)

    failpoint = recovery_failpoint
    with pytest.raises(InjectedFault):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001,
        )
    repaired = pointers.recover(
        recovery,
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_002,
    )

    assert repaired.to_dict()["pointer_revision"] == expected_revision
    assert repaired.to_dict()["current"]["generation_id"] == "gen-racer"
    assert repaired.to_dict()["last_good"]["generation_id"] == "gen-old"
    assert pointers.verify_pointer(repaired)["last_good"].sha256 == old.sha256
    assert pointers.state.path(generations._generation(REPO_UUID, "gen-old")).is_dir()
    assert not pointers.state.path(
        pointers._workspace(REPO_UUID)
        / "quarantine"
        / "corrupt"
        / f"gen-old.{expected_revision}"
    ).exists()
    assert pointers.state.path(
        pointers._workspace(REPO_UUID)
        / "quarantine"
        / "corrupt"
        / f"gen-new.{expected_revision}"
    ).is_dir()
    harness.leases.release(recovery)


def test_same_fence_recovery_rejects_tampered_pending_before_visible_mutation(
    tmp_path: Path,
) -> None:
    failpoint: str | None = None

    def fail(event: str) -> None:
        nonlocal failpoint
        if event == failpoint:
            failpoint = None
            raise InjectedFault(event)

    harness, journal, _generations, pointers, promote, receipts = _promotion_runtime(
        tmp_path,
        fault_hook=fail,
    )
    old = receipts["gen-old"]
    new = receipts["gen-new"]
    racer = receipts["gen-racer"]
    pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    pointers.promote(
        promote,
        _cas(promote, new, revision=1, current_sha256=old.sha256),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_002,
    )
    corrupt = pointers.state.path(
        pointers.generations._generation(REPO_UUID, "gen-new")
    ) / "graphify-out" / "graph.json"
    corrupt.write_text("corrupt\n", encoding="utf-8")
    failpoint = "pointer:promoted:journal_durable"
    with pytest.raises(InjectedFault):
        pointers.promote(
            promote,
            _cas(promote, racer, revision=2, current_sha256=new.sha256),
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=20_003,
        )
    harness.leases.release(promote)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)
    failpoint = "pointer:repaired:pending_durable"
    with pytest.raises(InjectedFault):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001,
        )

    pending_path = pointers.state.path(pointers._pending(REPO_UUID))
    pending_before = pending_path.read_bytes()
    last_good_payload = pointers.state.path(
        pointers.generations._generation(REPO_UUID, "gen-old")
    ) / "graphify-out" / "graph.json"
    last_good_payload.write_text("corrupt last good\n", encoding="utf-8")
    visible_before = pointers.state.read_existing_bytes(pointers._current(REPO_UUID))

    with pytest.raises(GenerationError, match="payload does not match"):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_002,
        )

    assert pointers.state.read_existing_bytes(pointers._current(REPO_UUID)) == visible_before
    assert pending_path.read_bytes() == pending_before
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(recovery, monotonic_ns=30_003).events
    ]
    assert "REPAIRED" not in transitions


def test_recovery_fails_closed_without_mutating_a_corrupt_pending_intent(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    receipt = receipts["gen-old"]
    current = pointers.promote(
        promote,
        _cas(promote, receipt, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    pending_path = pointers.state.path(pointers._pending(REPO_UUID))
    pending_path.write_bytes(b"{}\n")
    pending_path.chmod(0o600)
    pending_before = pending_path.read_bytes()
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)

    with pytest.raises(PointerCorrupt, match="pointer record is invalid.*pointers.pending"):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=30_001,
        )

    assert pending_path.read_bytes() == pending_before
    visible = pointers._read_pointer(pointers._current(REPO_UUID), allow_missing=False)
    assert visible is not None and visible.canonical == current.canonical
    with pytest.raises(PointerRecoveryRequired):
        pointers.load(REPO_UUID)
    harness.leases.release(recovery)


def test_recovery_rejects_current_pointer_without_certification_journal(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    current = pointers.promote(
        promote,
        _cas(promote, receipts["gen-old"], revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    shutil.rmtree(harness.state_root / "workspaces" / REPO_UUID / "journal")
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)

    with pytest.raises(PointerCorrupt, match="journal event|uncertified"):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001,
        )

    visible = pointers._read_pointer(pointers._current(REPO_UUID), allow_missing=False)
    assert visible is not None and visible.canonical == current.canonical


def test_recovery_rejects_stale_pending_pointer_without_rolling_back_current(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    old = pointers.promote(
        promote,
        _cas(promote, receipts["gen-old"], revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    current = pointers.promote(
        promote,
        _cas(
            promote,
            receipts["gen-new"],
            revision=1,
            current_sha256=receipts["gen-old"].sha256,
        ),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_002,
    )
    harness.leases.release(promote)
    pending_path = pointers.state.path(pointers._pending(REPO_UUID))
    pending_path.write_bytes(old.canonical)
    pending_path.chmod(0o600)
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)

    with pytest.raises(PointerCorrupt, match="pending pointer is stale"):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001,
        )

    assert pointers.state.read_existing_bytes(pointers._current(REPO_UUID)) == current.canonical


def test_recovery_rejects_stale_visible_pointer_behind_durable_journal(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    old = pointers.promote(
        promote,
        _cas(promote, receipts["gen-old"], revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    current = pointers.promote(
        promote,
        _cas(
            promote,
            receipts["gen-new"],
            revision=1,
            current_sha256=receipts["gen-old"].sha256,
        ),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_002,
    )
    harness.leases.release(promote)
    pointers.state.atomic_replace_bytes(
        pointers._current(REPO_UUID),
        old.canonical,
        label="test:stale-visible",
    )
    recovery = acquire(harness, "POINTER_RECOVERY", tick=3)

    with pytest.raises(PointerCorrupt, match="stale relative to durable journal"):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001,
        )

    assert pointers.state.read_existing_bytes(pointers._current(REPO_UUID)) == old.canonical
    assert current.to_dict()["pointer_revision"] == 2


def test_reader_rejects_stale_visible_pointer_behind_durable_journal(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    old = pointers.promote(
        promote,
        _cas(promote, receipts["gen-old"], revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    pointers.promote(
        promote,
        _cas(
            promote,
            receipts["gen-new"],
            revision=1,
            current_sha256=receipts["gen-old"].sha256,
        ),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_002,
    )
    harness.leases.release(promote)
    pointers.state.atomic_replace_bytes(
        pointers._current(REPO_UUID),
        old.canonical,
        label="test:stale-visible-reader",
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(PointerCorrupt, match="stale relative to durable journal"):
        with pointers.read_current(REPO_UUID):
            pytest.fail("stale pointer must not yield a generation read")

    assert tree_snapshot(harness.state_root) == before


def test_pointer_records_are_bound_to_their_containing_workspace(
    tmp_path: Path,
) -> None:
    harness, _journal, _generations, pointers, promote, receipts = _promotion_runtime(tmp_path)
    current = pointers.promote(
        promote,
        _cas(promote, receipts["gen-old"], revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    second_repo = create_repo(tmp_path / "repo-two", SECOND_UUID)
    harness.registry.enroll(
        discover_source(second_repo),
        authorization("enroll-second-pointer"),
        expected_revision=1,
    )
    second_pointer = pointers.state.path(pointers._current(SECOND_UUID))
    second_pointer.parent.mkdir(parents=True, exist_ok=True)
    second_pointer.write_bytes(current.canonical)
    second_pointer.chmod(0o600)

    with pytest.raises(PointerCorrupt, match="another workspace"):
        pointers.load(SECOND_UUID)

    registry = harness.registry.load()
    entry = next(
        item for item in registry.to_dict()["workspaces"] if item["repo_uuid"] == SECOND_UUID
    )
    state = harness.leases.inspect(SECOND_UUID)
    recovery = harness.leases.acquire(
        SECOND_UUID,
        "POINTER_RECOVERY",
        harness.leases.current_owner(),
        expected_registry_revision=int(registry.to_dict()["revision"]),
        expected_active_source_revision=int(entry["active_source_revision"]),
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )
    with pytest.raises(PointerCorrupt, match="no fully verified pointer source"):
        pointers.recover(
            recovery,
            occurred_at=START + timedelta(seconds=3),
            monotonic_ns=30_001,
        )
    harness.leases.release(recovery)
    loaded = pointers.load(REPO_UUID)
    assert loaded is not None and loaded.canonical == current.canonical
