from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading

import pytest

from graphify.workspace.contracts import CapacityPolicy, ContractError
from graphify.workspace.identity import discover_source
from graphify.workspace.leases import LeaseGrant
from graphify.workspace.generations import (
    CapacityExceeded,
    CertificationRequest,
    GenerationError,
    GenerationStore,
    PayloadChanged,
)
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault

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


@pytest.mark.parametrize("bad_kind", ["symlink", "hardlink", "fifo", "extra_root"])
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
    else:
        (allocation.staging_path / "unexpected.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(GenerationError):
        store.inspect_staged_payload(allocation)


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
