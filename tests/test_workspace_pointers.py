from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

import pytest

from graphify.workspace.contracts import CapacityPolicy, PriorPointerRecord
from graphify.workspace.generations import CertificationRequest, GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault, LockOrderError
from graphify.workspace.pointers import (
    PointerCAS,
    PointerCorrupt,
    PointerStore,
    PointerSuperseded,
)

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


def _request(commit: str) -> CertificationRequest:
    return CertificationRequest(
        source_commit=commit,
        source_epoch=1,
        policy_sha256="1" * 64,
        observation_manifest_sha256="2" * 64,
        queue_watermark=0,
        semantic_completeness="not_required",
        compatibility_sha256="3" * 64,
        validations=("payload_manifest", "coordination_lock_precreated"),
    )


def _certify(
    store: GenerationStore,
    grant: Any,
    generation_id: str,
    content: str,
    *,
    monotonic_ns: int,
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
        _request(hashlib.sha1(generation_id.encode()).hexdigest()),
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
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
    )
    return harness, journal, generations, pointers, promote, receipts


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
                "from graphify.workspace.registry import RegistryStore\n"
                "from graphify.workspace.leases import LeaseStore\n"
                "from graphify.workspace.journal import JournalStore\n"
                "from graphify.workspace.generations import GenerationStore\n"
                "from graphify.workspace.pointers import PointerStore\n"
                "root=Path(__import__('sys').argv[1]); repo=__import__('sys').argv[2]\n"
                "caps=RuntimeCapabilities.supported_test_fixture()\n"
                "registry=RegistryStore(root, capabilities=caps)\n"
                "leases=LeaseStore(root, registry, capabilities=caps)\n"
                "journal=JournalStore(root, leases, capabilities=caps)\n"
                "generations=GenerationStore(root, leases, journal, capabilities=caps)\n"
                "pointers=PointerStore(root, leases, generations, journal, capabilities=caps)\n"
                "pointer=pointers.load(repo); receipt=generations.verify_generation(repo, 'gen-reader')\n"
                "print(pointer.to_dict()['pointer_revision'], receipt.sha256)\n"
            ),
            str(harness.state_root),
            REPO_UUID,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert reopened.stdout.strip() == f"1 {receipt.sha256}"
    assert tree_snapshot(harness.state_root) == before


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
