from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import shutil
from types import SimpleNamespace
import threading
from typing import Any

import pytest

from graphify.workspace.contracts import CapacityPolicy, ContractError
from graphify.workspace.identity import discover_source
from graphify.workspace.leases import LeaseGrant, LeaseRecoveryRequired
from graphify.workspace.generations import (
    CapacityExceeded,
    CertificationRequest,
    GenerationError,
    GenerationStore,
    PayloadChanged,
)
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault, StatePathError

from tests.workspace_p3_helpers import (
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


def _request(source_commit: str) -> CertificationRequest:
    return CertificationRequest(
        source_commit=source_commit,
        source_epoch=1,
        policy_sha256="1" * 64,
        observation_manifest_sha256="2" * 64,
        queue_watermark=0,
        semantic_completeness="not_required",
        compatibility_sha256="3" * 64,
        validations=("payload_manifest", "coordination_lock_precreated"),
    )


def test_capacity_policy_is_internal_explicit_and_has_no_implicit_limits() -> None:
    with pytest.raises(ContractError, match="workspace_max_bytes"):
        CapacityPolicy.from_mapping(
            {
                **POLICY.to_dict(),
                "workspace_max_bytes": POLICY.global_max_bytes + 1,
            }
        )
    with pytest.raises(ContractError, match="unexpected field"):
        CapacityPolicy.from_mapping({**POLICY.to_dict(), "public_schema_override": 1})


def test_capacity_preflight_fails_before_allocation_mutates_workspace_state(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root / "workspaces" / REPO_UUID)
    tiny = replace(POLICY, workspace_max_bytes=64, global_max_bytes=64)

    with pytest.raises(CapacityExceeded, match="workspace byte limit"):
        store.allocate(
            grant,
            expected_payload_bytes=65,
            capacity_policy=tiny,
            generation_id="gen-capacity",
            occurred_at=START,
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root / "workspaces" / REPO_UUID) == before


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (replace(POLICY, global_max_bytes=64), "global byte limit"),
        (replace(POLICY, workspace_max_generations=1), "workspace generation limit"),
    ],
)
def test_capacity_preflight_enforces_global_bytes_and_workspace_generation_count(
    tmp_path: Path,
    policy: CapacityPolicy,
    message: str,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    if message == "workspace generation limit":
        store.allocate(
            grant,
            expected_payload_bytes=32,
            capacity_policy=policy,
            generation_id="gen-capacity-one",
            occurred_at=START,
            monotonic_ns=10_001,
        )
        requested_bytes = 32
    else:
        requested_bytes = 65

    with pytest.raises(CapacityExceeded, match=message):
        store.allocate(
            grant,
            expected_payload_bytes=requested_bytes,
            capacity_policy=policy,
            generation_id="gen-capacity-two",
            occurred_at=START,
            monotonic_ns=10_002,
        )


def test_capacity_preflight_enforces_filesystem_reserve_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    monkeypatch.setattr(
        "graphify.workspace.generations.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=POLICY.reserve_bytes),
    )
    before = tree_snapshot(harness.state_root / "workspaces" / REPO_UUID)

    with pytest.raises(CapacityExceeded, match="filesystem reserve"):
        store.allocate(
            grant,
            expected_payload_bytes=1,
            capacity_policy=POLICY,
            generation_id="gen-reserve",
            occurred_at=START,
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root / "workspaces" / REPO_UUID) == before


def test_filesystem_reserve_counts_unconsumed_cross_workspace_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    policy = replace(
        POLICY,
        global_max_bytes=1_000,
        workspace_max_bytes=1_000,
        reserve_bytes=10,
    )
    monkeypatch.setattr(
        "graphify.workspace.generations.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100),
    )
    store.allocate(
        acquire(harness, "BUILD", tick=1),
        expected_payload_bytes=60,
        capacity_policy=policy,
        generation_id="gen-reserved-one",
        occurred_at=START,
        monotonic_ns=10_001,
    )

    second_repo = create_repo(tmp_path / "repo-two", SECOND_UUID)
    harness.registry.enroll(
        discover_source(second_repo),
        authorization("enroll-second"),
        expected_revision=1,
    )
    registry = harness.registry.load()
    entry = next(
        item
        for item in registry.to_dict()["workspaces"]
        if item["repo_uuid"] == SECOND_UUID
    )
    lease_state = harness.leases.inspect(SECOND_UUID)
    second_grant = harness.leases.acquire(
        SECOND_UUID,
        "BUILD",
        harness.leases.current_owner(),
        expected_registry_revision=int(registry.to_dict()["revision"]),
        expected_active_source_revision=int(entry["active_source_revision"]),
        expected_operation_epoch=lease_state.operation_epoch,
        expected_migration_epoch=lease_state.migration_epoch,
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
        ttl_ns=1_000_000,
    )
    before = tree_snapshot(harness.state_root / "workspaces" / SECOND_UUID)

    with pytest.raises(CapacityExceeded, match="filesystem reserve"):
        store.allocate(
            second_grant,
            expected_payload_bytes=60,
            capacity_policy=policy,
            generation_id="gen-reserved-two",
            occurred_at=START,
            monotonic_ns=20_001,
        )

    assert tree_snapshot(harness.state_root / "workspaces" / SECOND_UUID) == before


def test_global_capacity_reservation_serializes_cross_workspace_allocations(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    second_repo = create_repo(tmp_path / "repo-two", SECOND_UUID)
    harness.registry.enroll(
        discover_source(second_repo),
        authorization("enroll-second"),
        expected_revision=1,
    )

    def grant_for(repo_uuid: str, tick: int) -> LeaseGrant:
        registry = harness.registry.load()
        entry = next(
            item for item in registry.to_dict()["workspaces"] if item["repo_uuid"] == repo_uuid
        )
        state = harness.leases.inspect(repo_uuid)
        return harness.leases.acquire(
            repo_uuid,
            "BUILD",
            harness.leases.current_owner(),
            expected_registry_revision=int(registry.to_dict()["revision"]),
            expected_active_source_revision=int(entry["active_source_revision"]),
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START + timedelta(seconds=tick),
            monotonic_ns=tick * 10_000,
            ttl_ns=1_000_000,
        )

    grants = [(REPO_UUID, grant_for(REPO_UUID, 1)), (SECOND_UUID, grant_for(SECOND_UUID, 2))]
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    policy = replace(
        POLICY,
        global_max_generations=1,
        workspace_max_generations=1,
    )
    barrier = threading.Barrier(2)

    def allocate_one(item: tuple[str, LeaseGrant]) -> str:
        repo_uuid, grant = item
        barrier.wait()
        try:
            store.allocate(
                grant,
                expected_payload_bytes=1024,
                capacity_policy=policy,
                generation_id="gen-global-capacity",
                occurred_at=START,
                monotonic_ns=30_000,
            )
        except CapacityExceeded:
            return f"rejected:{repo_uuid}"
        return f"allocated:{repo_uuid}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(allocate_one, grants))

    assert sum(outcome.startswith("allocated:") for outcome in outcomes) == 1
    assert sum(outcome.startswith("rejected:") for outcome in outcomes) == 1


def test_cross_workspace_certification_does_not_hold_the_global_registry_lock(
    tmp_path: Path,
) -> None:
    paused = threading.Event()
    resume = threading.Event()

    def pause_first(event: str) -> None:
        if event == "generation:gen-workspace-a:before_reinventory":
            paused.set()
            if not resume.wait(timeout=5):
                raise TimeoutError("cross-workspace certification did not complete")

    harness = create_harness(tmp_path)
    second_repo = create_repo(tmp_path / "repo-two", SECOND_UUID)
    harness.registry.enroll(
        discover_source(second_repo),
        authorization("enroll-second-concurrency"),
        expected_revision=1,
    )

    def grant_for(repo_uuid: str, tick: int) -> LeaseGrant:
        registry = harness.registry.load()
        entry = next(
            item for item in registry.to_dict()["workspaces"] if item["repo_uuid"] == repo_uuid
        )
        state = harness.leases.inspect(repo_uuid)
        return harness.leases.acquire(
            repo_uuid,
            "BUILD",
            harness.leases.current_owner(),
            expected_registry_revision=int(registry.to_dict()["revision"]),
            expected_active_source_revision=int(entry["active_source_revision"]),
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START + timedelta(seconds=tick),
            monotonic_ns=tick * 10_000,
            ttl_ns=1_000_000,
        )

    first_grant = grant_for(REPO_UUID, 1)
    second_grant = grant_for(SECOND_UUID, 2)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=pause_first,
    )
    work = []
    for repo_uuid, grant, generation_id, tick in (
        (REPO_UUID, first_grant, "gen-workspace-a", 1),
        (SECOND_UUID, second_grant, "gen-workspace-b", 2),
    ):
        allocation = store.allocate(
            grant,
            expected_payload_bytes=4096,
            capacity_policy=POLICY,
            generation_id=generation_id,
            occurred_at=START,
            monotonic_ns=tick * 10_000 + 1,
        )
        payload = allocation.staging_path / "graphify-out"
        payload.mkdir()
        (payload / "graph.json").write_text(f"{generation_id}\n", encoding="utf-8")
        work.append((grant, allocation, store.inspect_staged_payload(allocation)))

    def certify(item: tuple[LeaseGrant, Any, Any], monotonic_ns: int):
        grant, allocation, entries = item
        return store.certify(
            grant,
            allocation,
            _request("d" * 40),
            declared_entries=entries,
            occurred_at=START,
            monotonic_ns=monotonic_ns,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(certify, work[0], 10_002)
        assert paused.wait(timeout=5)
        second = executor.submit(certify, work[1], 20_002)
        second_receipt = second.result(timeout=5)
        assert second_receipt.to_dict()["repo_uuid"] == SECOND_UUID
        assert not first.done()
        resume.set()
        assert first.result(timeout=5).to_dict()["repo_uuid"] == REPO_UUID


def test_certification_seals_exact_payload_and_installs_lock_before_certified_event(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    harness = create_harness(tmp_path, fault_hook=events.append)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        fault_hook=events.append,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=events.append,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-certified",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text('{"nodes":[]}\n', encoding="utf-8")
    (payload / "nested").mkdir()
    (payload / "nested/report.md").write_text("# report\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)

    receipt = store.certify(
        grant,
        allocation,
        _request("a" * 40),
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_002,
    )

    assert [entry["path"] for entry in receipt.to_dict()["sealed_query_payload"]["entries"]] == [
        "graphify-out/graph.json",
        "graphify-out/nested/report.md",
    ]
    assert store.verify_generation(REPO_UUID, "gen-certified").canonical == receipt.canonical
    lock_path = (
        harness.state_root / "workspaces" / REPO_UUID / "locks/generations/gen-certified.lock"
    )
    lock_inode = lock_path.stat().st_ino
    assert events.index("generation:gen-certified:lock_durable") < events.index(
        "journal:CERTIFIED:segment_durable"
    )
    assert store.verify_generation(REPO_UUID, "gen-certified").canonical == receipt.canonical
    assert lock_path.stat().st_ino == lock_inode


@pytest.mark.parametrize(
    "bad_kind",
    ["symlink", "hardlink", "fifo", "extra_root", "root_mode"],
)
def test_certification_rejects_links_special_files_and_root_extras(
    tmp_path: Path,
    bad_kind: str,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id=f"gen-{bad_kind}",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    source = payload / "graph.json"
    source.write_text("{}\n", encoding="utf-8")
    if bad_kind == "symlink":
        (payload / "linked.json").symlink_to(source)
    elif bad_kind == "hardlink":
        os.link(source, payload / "hardlinked.json")
    elif bad_kind == "fifo":
        os.mkfifo(payload / "pipe")
    elif bad_kind == "extra_root":
        (allocation.staging_path / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    else:
        payload.chmod(0o750)

    with pytest.raises(GenerationError):
        store.inspect_staged_payload(allocation)


def test_certification_reloads_the_durable_reservation_and_stable_lock_error(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=64,
        capacity_policy=POLICY,
        generation_id="gen-durable-reservation",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("reservation\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)

    with pytest.raises(GenerationError, match="durable capacity reservation"):
        store.certify(
            grant,
            replace(allocation, expected_payload_bytes=4096),
            _request("a" * 40),
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )

    store.state.path(store._lock(REPO_UUID, allocation.generation_id)).unlink()
    with pytest.raises(StatePathError, match="generation lock is missing"):
        store.certify(
            grant,
            allocation,
            _request("a" * 40),
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_003,
        )


def test_certification_rejects_noncanonical_manifest_and_mutation_during_sealing(
    tmp_path: Path,
) -> None:
    mutated = False

    def mutate(event: str) -> None:
        nonlocal mutated
        if event == "generation:gen-mutation:before_reinventory" and not mutated:
            mutated = True
            payload_file.write_text("changed\n", encoding="utf-8")

    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=mutate,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-mutation",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    payload_file = payload / "graph.json"
    payload_file.write_text("initial\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)

    with pytest.raises(PayloadChanged, match="changed during sealing"):
        store.certify(
            grant,
            allocation,
            _request("a" * 40),
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )
    assert mutated

    bad = [dict(declared[0])]
    bad[0]["path"] = "graphify-out/../escape"
    with pytest.raises((ContractError, GenerationError)):
        store.certify(
            grant,
            allocation,
            _request("a" * 40),
            declared_entries=bad,
            occurred_at=START,
            monotonic_ns=10_003,
        )


@pytest.mark.parametrize(
    "failpoint",
    [
        "generation:gen-allocate:capacity_reserved",
        "generation:gen-allocate:lock:installed",
        "generation:gen-allocate:lock_durable",
        "journal:ALLOCATED:segment:installed",
        "journal:ALLOCATED:segment_durable",
        "journal:ALLOCATED:head:current_replaced",
        "journal:ALLOCATED:head_durable",
        "journal:STAGING:segment:installed",
        "journal:STAGING:head_durable",
    ],
)
def test_allocation_recovers_each_reservation_lock_and_journal_boundary(
    tmp_path: Path,
    failpoint: str,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == failpoint:
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )

    with pytest.raises((CommitUnknown, InjectedFault)):
        store.allocate(
            grant,
            expected_payload_bytes=4096,
            capacity_policy=POLICY,
            generation_id="gen-allocate",
            occurred_at=START,
            monotonic_ns=10_001,
        )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-allocate",
        occurred_at=START,
        monotonic_ns=10_002,
    )

    assert allocation.staging_path.is_dir()
    assert store.state.path(store._lock(REPO_UUID, "gen-allocate")).is_file()
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(grant, monotonic_ns=10_003).for_generation("gen-allocate")
    ]
    assert transitions == ["ALLOCATED", "STAGING"]


def test_successor_fence_adopts_dead_builder_reservation_and_staging(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    dead = acquire(harness, "BUILD", tick=1, ttl_ns=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        dead,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-successor-staging",
        occurred_at=START,
        monotonic_ns=10_000,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("successor\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)

    with pytest.raises(LeaseRecoveryRequired, match="generation reservation"):
        acquire(harness, "ACTIVATE", tick=2)
    successor = acquire(harness, "BUILD", tick=2)
    adopted = store.allocate(
        successor,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-successor-staging",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )

    assert adopted.operation_epoch == successor.operation_epoch
    assert adopted.fence_token == int(successor.lease.to_dict()["fence_token"])
    receipt = store.certify(
        successor,
        adopted,
        _request("a" * 40),
        declared_entries=declared,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_002,
    )
    assert store.verify_generation(REPO_UUID, adopted.generation_id).canonical == receipt.canonical


def test_successor_revalidates_when_predecessor_died_before_sealing_receipt(
    tmp_path: Path,
) -> None:
    armed = True

    def fail_after_validating(event: str) -> None:
        nonlocal armed
        if armed and event == "journal:VALIDATING:head_durable":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    dead = acquire(harness, "BUILD", tick=1, ttl_ns=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail_after_validating,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        dead,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-successor-revalidate",
        occurred_at=START,
        monotonic_ns=10_000,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("revalidate\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request = _request("d" * 40)
    with pytest.raises(InjectedFault):
        store.certify(
            dead,
            allocation,
            request,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_000,
        )
    assert not (allocation.staging_path / "receipt.json").exists()

    successor = acquire(harness, "BUILD", tick=2)
    adopted = store.allocate(
        successor,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id=allocation.generation_id,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )
    receipt = store.certify(
        successor,
        adopted,
        request,
        declared_entries=declared,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_002,
    )

    events = journal.recover(successor, monotonic_ns=20_003).for_generation(
        adopted.generation_id
    )
    validating = [
        event.to_dict()
        for event in events
        if event.to_dict()["transition"] == "VALIDATING"
    ]
    assert [(event["operation_epoch"], event["fence_token"]) for event in validating] == [
        (dead.operation_epoch, int(dead.lease.to_dict()["fence_token"])),
        (successor.operation_epoch, int(successor.lease.to_dict()["fence_token"])),
    ]
    assert receipt.to_dict()["operation_epoch"] == successor.operation_epoch
    assert receipt.to_dict()["fence_token"] == int(
        successor.lease.to_dict()["fence_token"]
    )


def test_successor_fence_finishes_installed_generation_before_certified_event(
    tmp_path: Path,
) -> None:
    armed = True

    def fail_after_install(event: str) -> None:
        nonlocal armed
        if armed and event == "generation:gen-successor-installed:installed":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    dead = acquire(harness, "BUILD", tick=1, ttl_ns=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail_after_install,
    )
    allocation = store.allocate(
        dead,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-successor-installed",
        occurred_at=START,
        monotonic_ns=10_000,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("installed\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request = _request("b" * 40)
    with pytest.raises(InjectedFault):
        store.certify(
            dead,
            allocation,
            request,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_000,
        )

    successor = acquire(harness, "BUILD", tick=2)
    adopted = store.allocate(
        successor,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-successor-installed",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )
    receipt = store.certify(
        successor,
        adopted,
        request,
        declared_entries=declared,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_002,
    )

    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(successor, monotonic_ns=20_003).for_generation(
            adopted.generation_id
        )
    ]
    assert transitions == ["ALLOCATED", "STAGING", "BUILT", "VALIDATING", "CERTIFIED"]
    assert receipt.to_dict()["operation_epoch"] == dead.operation_epoch
    capacity = store._load_capacity_locked()
    assert capacity is not None and not capacity.reservations


def test_successor_retries_certification_after_durable_capacity_release(
    tmp_path: Path,
) -> None:
    armed = True

    def fail_after_capacity_release(event: str) -> None:
        nonlocal armed
        if armed and event == "generation:gen-capacity-release:capacity_released":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    dead = acquire(harness, "BUILD", tick=1, ttl_ns=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail_after_capacity_release,
    )
    allocation = store.allocate(
        dead,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-capacity-release",
        occurred_at=START,
        monotonic_ns=10_000,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("released\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request = _request("e" * 40)

    with pytest.raises(InjectedFault, match="capacity_released"):
        store.certify(
            dead,
            allocation,
            request,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_000,
        )
    capacity = store._load_capacity_locked()
    assert capacity is not None and not capacity.reservations

    successor = acquire(harness, "BUILD", tick=2)
    receipt = store.certify(
        successor,
        allocation,
        request,
        declared_entries=declared,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )

    assert store.verify_generation(REPO_UUID, allocation.generation_id).canonical == receipt.canonical
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(successor, monotonic_ns=20_002).for_generation(
            allocation.generation_id
        )
    ]
    assert transitions == ["ALLOCATED", "STAGING", "BUILT", "VALIDATING", "CERTIFIED"]


def test_capacity_counts_corrupt_quarantine_and_rejects_ambiguous_locations(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    policy = replace(POLICY, workspace_max_generations=1)
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=policy,
        generation_id="gen-corrupt-retained",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("retained\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    store.certify(
        grant,
        allocation,
        _request("c" * 40),
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_002,
    )
    store.state.rename_contained(
        store._generation(REPO_UUID, allocation.generation_id),
        store._workspace(REPO_UUID) / "quarantine" / "corrupt" / "gen-corrupt-retained.1",
        label="test:corrupt-quarantine",
    )

    with pytest.raises(CapacityExceeded, match="workspace generation limit"):
        store.allocate(
            grant,
            expected_payload_bytes=1,
            capacity_policy=policy,
            generation_id="gen-after-corrupt",
            occurred_at=START,
            monotonic_ns=10_003,
        )

    quarantine = store.state.path(
        store._workspace(REPO_UUID)
        / "quarantine"
        / "corrupt"
        / "gen-corrupt-retained.1"
    )
    duplicate = store.state.path(store._generation(REPO_UUID, "gen-corrupt-retained"))
    shutil.copytree(quarantine, duplicate)
    with pytest.raises(CapacityExceeded, match="multiple active/staging/quarantine"):
        store._usage(())


def test_capacity_scan_retries_a_transient_cross_workspace_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
    )
    original = store._scan_usage_once
    calls = 0

    def transient_scan() -> dict[tuple[str, str], int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError("generation moved during capacity scan")
        return original()

    monkeypatch.setattr(store, "_scan_usage_once", transient_scan)

    assert store._usage(()).global_bytes == 0
    assert calls == 3


@pytest.mark.parametrize(
    "failpoint",
    [
        "generation:gen-crash:payload_file_durable:graphify-out/graph.json",
        "generation:gen-crash:payload_durable",
        "generation:gen-crash:before_reinventory",
        "generation:gen-crash:receipt:installed",
        "generation:gen-crash:receipt_durable",
        "generation:gen-crash:install:before_rename",
        "generation:gen-crash:install:renamed",
        "generation:gen-crash:install:source_parent_durable",
        "generation:gen-crash:install:destination_parent_durable",
        "generation:gen-crash:installed",
        "journal:CERTIFIED:segment:installed",
        "journal:CERTIFIED:segment_durable",
        "journal:CERTIFIED:head:current_replaced",
        "journal:CERTIFIED:head_durable",
    ],
)
def test_certification_recovers_receipt_and_generation_visibility_boundaries(
    tmp_path: Path,
    failpoint: str,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == failpoint:
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-crash",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("crash boundary\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    with pytest.raises((CommitUnknown, InjectedFault)):
        store.certify(
            grant,
            allocation,
            _request("a" * 40),
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )

    receipt = store.certify(
        grant,
        allocation,
        _request("a" * 40),
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_003,
    )
    assert store.verify_generation(REPO_UUID, "gen-crash").canonical == receipt.canonical
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(grant, monotonic_ns=10_004).for_generation("gen-crash")
    ]
    assert transitions == ["ALLOCATED", "STAGING", "BUILT", "VALIDATING", "CERTIFIED"]
