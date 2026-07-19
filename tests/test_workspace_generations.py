from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import shutil
from types import SimpleNamespace
import threading
from typing import Any, cast

import pytest

from graphify.workspace.adapters import (
    AdapterIntent,
    SourceObservation,
    UnsupportedCompatibility,
)
from graphify.workspace.contracts import (
    CapacityPolicy,
    CapacityReservationState,
    CompatibilityManifest,
    ContractError,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.leases import LeaseGrant, LeaseRecoveryRequired
from graphify.workspace.generations import (
    CapacityExceeded,
    CertificationRequest,
    GenerationConflict,
    GenerationError,
    GenerationStore as RuntimeGenerationStore,
    PayloadChanged,
)
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault, StatePathError
from graphify.workspace.semantic_queue import (
    SemanticCertificationBlocked,
    SemanticQueueConflict,
    SemanticQueuePolicy,
    SemanticQueueStore,
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
    metadata_snapshot,
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

QUEUE_POLICY = SemanticQueuePolicy(
    max_items=16,
    max_bytes=64 * 1024,
    retry_budget=1,
)
QUEUE_WATERMARK = 1


class GenerationStore(RuntimeGenerationStore):
    """Test store that gives certification cases explicit durable queue authority."""

    def __init__(self, state_root: Path, leases: Any, journal: Any, **kwargs: Any) -> None:
        kwargs.setdefault(
            "semantic_queue",
            SemanticQueueStore(
                state_root,
                leases,
                policy=QUEUE_POLICY,
                capabilities=kwargs.get("capabilities"),
            ),
        )
        super().__init__(state_root, leases, journal, **kwargs)

    def certify(
        self,
        grant: LeaseGrant,
        allocation: Any,
        request: CertificationRequest,
        *,
        source_observations: Any = None,
        declared_entries: Any,
        occurred_at: Any,
        monotonic_ns: int,
    ) -> Any:
        observations = source_observations
        if observations is None:
            observation = SourceObservation(
                source_commit=request.source_commit,
                inventory_sha256=request.observation_manifest_sha256,
                policy_sha256=request.policy_sha256,
                detector_id="test-workspace-generations",
                stable_inventory_passes=2,
                entries=(),
            )
            observations = (observation, observation)
            assert self.semantic_queue is not None
            self.semantic_queue.reconcile(
                grant,
                (),
                source_epoch=request.source_epoch,
                policy_sha256=request.policy_sha256,
                source_observations=observations,
                desired_watermark=request.queue_watermark,
                semantic_required=False,
                monotonic_ns=monotonic_ns,
            )
            self.semantic_queue.bind_sealed_inputs(
                grant,
                sealed_input_manifest_sha256=payload_manifest_sha256(
                    "graphify-out", declared_entries
                ),
                monotonic_ns=monotonic_ns,
            )
        return super().certify(
            grant,
            allocation,
            request,
            source_observations=observations,
            declared_entries=declared_entries,
            occurred_at=occurred_at,
            monotonic_ns=monotonic_ns,
        )


def _install_legacy_capacity_state(
    harness: Any,
    grant: LeaseGrant,
    *,
    generation_id: str,
    compatibility_sha256: str | None = None,
) -> None:
    reservation: dict[str, object] = {
        "repo_uuid": REPO_UUID,
        "generation_id": generation_id,
        "reserved_bytes": 1024,
        "policy_sha256": POLICY.sha256,
        "active_source_revision": grant.active_source_revision,
        "operation_epoch": grant.operation_epoch,
        "fence_token": int(grant.lease.to_dict()["fence_token"]),
        "created_at": "2026-07-16T19:00:01Z",
    }
    if compatibility_sha256 is not None:
        reservation["compatibility_sha256"] = compatibility_sha256
    payload = canonical_json_bytes(
        {
            "contract": "graphify.workspace.capacity_reservations.internal",
            "format_version": 1,
            "revision": 1,
            "reservations": [reservation],
        }
    )
    harness.leases.state.commit_record(
        label="capacity",
        current=Path("capacity.json"),
        previous=Path("capacity.previous.json"),
        pending=Path("capacity.pending.json"),
        payload=payload,
        decoder=CapacityReservationState.from_json,
    )


def test_allocation_migrates_legacy_capacity_state_without_claiming_compatibility(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    _install_legacy_capacity_state(
        harness,
        grant,
        generation_id="gen-legacy-unbound",
    )
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )

    allocation = store.allocate(
        grant,
        expected_payload_bytes=2048,
        capacity_policy=POLICY,
        generation_id="gen-after-upgrade",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=10_001,
    )

    state = CapacityReservationState.from_json((harness.state_root / "capacity.json").read_bytes())
    reservations = {item.generation_id: item for item in state.reservations}
    assert state.format_version == 2
    assert state.revision == 2
    assert reservations["gen-legacy-unbound"].compatibility_sha256 == "legacy-unbound"
    assert reservations["gen-after-upgrade"].compatibility_sha256 == COMPATIBILITY_SHA256
    assert allocation.compatibility_sha256 == COMPATIBILITY_SHA256


def test_allocation_rejects_reusing_legacy_unbound_reservation_without_state_mutation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    _install_legacy_capacity_state(
        harness,
        grant,
        generation_id="gen-legacy-unbound",
    )
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        GenerationConflict,
        match="different durable capacity reservation",
    ):
        store.allocate(
            grant,
            expected_payload_bytes=1024,
            capacity_policy=POLICY,
            generation_id="gen-legacy-unbound",
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_allocation_recovers_bound_format_one_current_and_format_two_pending(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    _install_legacy_capacity_state(
        harness,
        grant,
        generation_id="gen-bound-format-one",
        compatibility_sha256=COMPATIBILITY_SHA256,
    )
    current_path = harness.state_root / "capacity.json"
    original = current_path.read_bytes()
    bound_state = CapacityReservationState.from_json(original)
    assert bound_state.canonical == original
    assert bound_state.format_version == 1
    assert bound_state.reservations[0].compatibility_sha256 == COMPATIBILITY_SHA256

    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )

    def fail_after_pending(event: str) -> None:
        if event == "capacity:pending_durable":
            raise InjectedFault(event)

    failing_store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail_after_pending,
    )
    with pytest.raises(CommitUnknown, match="capacity recovery intent"):
        failing_store.allocate(
            grant,
            expected_payload_bytes=2048,
            capacity_policy=POLICY,
            generation_id="gen-after-bound-upgrade",
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=10_001,
        )

    pending_path = harness.state_root / "capacity.pending.json"
    assert CapacityReservationState.from_json(current_path.read_bytes()).format_version == 1
    assert CapacityReservationState.from_json(pending_path.read_bytes()).format_version == 2

    recovered_store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = recovered_store.allocate(
        grant,
        expected_payload_bytes=2048,
        capacity_policy=POLICY,
        generation_id="gen-after-bound-upgrade",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=10_001,
    )

    recovered = CapacityReservationState.from_json(current_path.read_bytes())
    previous = CapacityReservationState.from_json(
        (harness.state_root / "capacity.previous.json").read_bytes()
    )
    reservations = {item.generation_id: item for item in recovered.reservations}
    assert recovered.format_version == 2
    assert previous.format_version == 1
    assert not pending_path.exists()
    assert reservations["gen-bound-format-one"].compatibility_sha256 == COMPATIBILITY_SHA256
    assert reservations["gen-after-bound-upgrade"].compatibility_sha256 == COMPATIBILITY_SHA256
    assert allocation.compatibility_sha256 == COMPATIBILITY_SHA256


def test_generation_store_selects_stage_adapter_before_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root)
    intents: list[AdapterIntent] = []

    def reject(_compatibility: object, *, intent: AdapterIntent) -> object:
        intents.append(intent)
        raise UnsupportedCompatibility("injected unsupported tuple")

    monkeypatch.setattr("graphify.workspace.generations.select_adapter", reject)

    with pytest.raises(UnsupportedCompatibility, match="injected unsupported tuple"):
        GenerationStore(
            harness.state_root,
            harness.leases,
            journal,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            capabilities=harness.leases.state.capabilities,
        )

    assert intents == [AdapterIntent.STAGE]
    assert tree_snapshot(harness.state_root) == before


def test_allocation_rejects_reservation_from_different_manifest_without_state_mutation(
    tmp_path: Path,
) -> None:
    alternate_manifest = cast(
        CompatibilityManifest,
        CompatibilityManifest.from_mapping(
            {
                **COMPATIBILITY_MANIFEST.to_dict(),
                "distribution_build": "alternate-published-build",
            }
        ),
    )
    harness = create_harness(tmp_path)
    first_grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    first_store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    first_store.allocate(
        first_grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-manifest-switch",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    harness.leases.release(first_grant)
    second_grant = acquire(harness, "BUILD", tick=2)
    alternate_store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=alternate_manifest,
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        GenerationConflict,
        match="different durable capacity reservation",
    ):
        alternate_store.allocate(
            second_grant,
            expected_payload_bytes=4096,
            capacity_policy=POLICY,
            generation_id="gen-manifest-switch",
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_001,
        )

    assert tree_snapshot(harness.state_root) == before


def _observations(
    harness: Any,
    *,
    inventory_sha256: str = "2" * 64,
) -> tuple[SourceObservation, SourceObservation]:
    observation = SourceObservation(
        source_commit=discover_source(harness.repo).head_commit,
        inventory_sha256=inventory_sha256,
        policy_sha256="1" * 64,
        detector_id="test-workspace-generations",
        stable_inventory_passes=2,
        entries=(),
    )
    return (observation, observation)


def _queue(harness: Any) -> SemanticQueueStore:
    return SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=QUEUE_POLICY,
        capabilities=harness.leases.state.capabilities,
    )


def _request(
    source: str | tuple[SourceObservation, SourceObservation],
    *,
    queue_watermark: int = QUEUE_WATERMARK,
) -> CertificationRequest:
    source_commit = source if isinstance(source, str) else source[0].source_commit
    return CertificationRequest(
        source_commit=source_commit,
        source_epoch=1,
        policy_sha256="1" * 64,
        observation_manifest_sha256="2" * 64,
        queue_watermark=queue_watermark,
        semantic_completeness="not_required",
        compatibility_sha256=COMPATIBILITY_SHA256,
        validations=("payload_manifest", "coordination_lock_precreated"),
    )


def _bind_certification(
    harness: Any,
    queue: SemanticQueueStore,
    grant: LeaseGrant,
    declared_entries: Any,
    *,
    monotonic_ns: int,
    queue_watermark: int = QUEUE_WATERMARK,
) -> tuple[CertificationRequest, tuple[SourceObservation, SourceObservation]]:
    observations = _observations(harness)
    queue.reconcile(
        grant,
        (),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=observations,
        desired_watermark=queue_watermark,
        semantic_required=False,
        monotonic_ns=monotonic_ns,
    )
    queue.bind_sealed_inputs(
        grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", declared_entries),
        monotonic_ns=monotonic_ns,
    )
    return _request(observations, queue_watermark=queue_watermark), observations


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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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


def test_gc_barrier_rejects_linked_parent_before_allocation_mutates_state(
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

    with pytest.raises(LeaseRecoveryRequired, match="recovery barrier is unsafe"):
        store.allocate(
            grant,
            expected_payload_bytes=1,
            capacity_policy=POLICY,
            generation_id="gen-linked-gc-barrier",
            occurred_at=START,
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before
    assert tree_snapshot(external) == before_external


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode"])
def test_capacity_scan_rejects_unsafe_generation_parent_before_mutation(
    tmp_path: Path,
    unsafe_kind: str,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    generations = harness.state_root / "workspaces" / REPO_UUID / "generations"
    external: Path | None = None
    if unsafe_kind == "symlink":
        external = tmp_path / "outside-generations"
        candidate = external / "gen-external"
        candidate.mkdir(parents=True, mode=0o700)
        candidate.chmod(0o700)
        (candidate / "graph.json").write_text("external\n", encoding="utf-8")
        generations.symlink_to(external, target_is_directory=True)
    else:
        generations.mkdir(mode=0o700)
        generations.chmod(0o755)
    before = tree_snapshot(harness.state_root)
    before_external = tree_snapshot(external) if external is not None else None
    before_external_metadata = metadata_snapshot(external) if external is not None else None

    with pytest.raises(CapacityExceeded, match="unsafe state path in capacity scan"):
        store.allocate(
            grant,
            expected_payload_bytes=1,
            capacity_policy=POLICY,
            generation_id="gen-rejected-unsafe-parent",
            occurred_at=START,
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before
    if external is not None:
        assert tree_snapshot(external) == before_external
        assert metadata_snapshot(external) == before_external_metadata


def test_allocate_revalidates_capacity_policy_before_any_state_mutation(
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    invalid = replace(POLICY, reserve_bytes=-1)
    before = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)

    with pytest.raises(CapacityExceeded, match="capacity policy is invalid"):
        store.allocate(
            grant,
            expected_payload_bytes=1,
            capacity_policy=invalid,
            generation_id="gen-invalid-policy",
            occurred_at=START,
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before
    assert metadata_snapshot(harness.state_root) == before_metadata


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            replace(POLICY, global_max_bytes=96, workspace_max_bytes=64),
            "global byte limit",
        ),
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        existing = harness.leases.state.ensure_directory(
            Path("workspaces") / SECOND_UUID / "generations" / "gen-existing"
        )
        (existing / "payload.bin").write_bytes(b"x" * 64)
        requested_bytes = 64

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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
    queue = _queue(harness)
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
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
        entries = store.inspect_staged_payload(allocation)
        request, observations = _bind_certification(
            harness,
            queue,
            grant,
            entries,
            monotonic_ns=tick * 10_000 + 2,
        )
        work.append((grant, allocation, entries, request, observations))

    def certify(item: tuple[LeaseGrant, Any, Any, Any, Any], monotonic_ns: int):
        grant, allocation, entries, request, observations = item
        return store.certify(
            grant,
            allocation,
            request,
            source_observations=observations,
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
    queue = _queue(harness)
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
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
    request, observations = _bind_certification(
        harness, queue, grant, declared, monotonic_ns=10_002
    )

    receipt = store.certify(
        grant,
        allocation,
        request,
        source_observations=observations,
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


@pytest.mark.parametrize("mismatch", ["allocation", "request"])
def test_certification_rejects_compatibility_mismatch_without_state_mutation(
    tmp_path: Path,
    mismatch: str,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    queue = _queue(harness)
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id=f"gen-compatibility-{mismatch}",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("{}\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request, observations = _bind_certification(
        harness, queue, grant, declared, monotonic_ns=10_002
    )
    if mismatch == "allocation":
        allocation = replace(allocation, compatibility_sha256="d" * 64)
    else:
        request = replace(request, compatibility_sha256="d" * 64)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        UnsupportedCompatibility,
        match="not bound to the selected compatibility manifest",
    ):
        store.certify(
            grant,
            allocation,
            request,
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )

    assert tree_snapshot(harness.state_root) == before


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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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


def test_certification_cleanup_rejects_linked_staging_parent_without_external_deletion(
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-cleanup-link",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("{}\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    staging = allocation.staging_path.parent
    external = tmp_path / "outside-staging"
    staging.rename(external)
    staging.symlink_to(external, target_is_directory=True)
    orphan = external / allocation.generation_id / f".receipt.json.tmp-123-{'a' * 32}"
    orphan.write_bytes(b"external orphan")
    orphan.chmod(0o600)
    before_external = tree_snapshot(external)
    before_external_metadata = metadata_snapshot(external)

    with pytest.raises(StatePathError):
        store.certify(
            grant,
            allocation,
            _request("a" * 40),
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )

    assert orphan.read_bytes() == b"external orphan"
    assert tree_snapshot(external) == before_external
    assert metadata_snapshot(external) == before_external_metadata
    assert not store.state.path(store._generation(REPO_UUID, allocation.generation_id)).exists()


def test_receipt_install_rejects_parent_swap_after_temp_cleanup_without_external_write(
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-receipt-link",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("{}\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    staging_relative = store._staging(REPO_UUID, allocation.generation_id)
    parked = tmp_path / "parked-receipt-staging"
    external = tmp_path / "outside-receipt-staging"
    external.mkdir(mode=0o755)
    before_external = tree_snapshot(external)
    before_external_metadata = metadata_snapshot(external)
    original_cleanup = store.state.cleanup_atomic_temps
    matching_calls = 0
    swapped = False

    def swap_after_cleanup(
        relative: str | Path,
        *,
        destination_name: str | None = None,
    ) -> tuple[Path, ...]:
        nonlocal matching_calls, swapped
        removed = original_cleanup(relative, destination_name=destination_name)
        if Path(relative) == staging_relative:
            matching_calls += 1
            if matching_calls == 2:
                allocation.staging_path.rename(parked)
                allocation.staging_path.symlink_to(external, target_is_directory=True)
                swapped = True
        return removed

    monkeypatch.setattr(store.state, "cleanup_atomic_temps", swap_after_cleanup)

    try:
        with pytest.raises(StatePathError):
            store.certify(
                grant,
                allocation,
                _request("d" * 40),
                declared_entries=declared,
                occurred_at=START,
                monotonic_ns=10_002,
            )
        assert swapped
        assert not (external / "receipt.json").exists()
        assert tree_snapshot(external) == before_external
        assert metadata_snapshot(external) == before_external_metadata
    finally:
        if allocation.staging_path.is_symlink():
            allocation.staging_path.unlink()
            parked.rename(allocation.staging_path)

    assert allocation.staging_path.is_dir()
    assert not store.state.path(store._generation(REPO_UUID, allocation.generation_id)).exists()


def test_certification_fsync_rejects_transient_payload_parent_link(
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-fsync-link",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("same bytes\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    parked = tmp_path / "parked-payload"
    external = tmp_path / "outside-payload"
    external.mkdir()
    (external / "graph.json").write_text("same bytes\n", encoding="utf-8")
    before_external = metadata_snapshot(external)
    original = store.state.fsync_contained_regular_file
    swapped = False

    def redirect_once(*args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if swapped:
            original(*args, **kwargs)
            return
        swapped = True
        payload.rename(parked)
        payload.symlink_to(external, target_is_directory=True)
        try:
            original(*args, **kwargs)
        finally:
            payload.unlink()
            parked.rename(payload)

    monkeypatch.setattr(store.state, "fsync_contained_regular_file", redirect_once)

    with pytest.raises(StatePathError):
        store.certify(
            grant,
            allocation,
            _request("b" * 40),
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )

    assert swapped
    assert metadata_snapshot(external) == before_external
    assert allocation.staging_path.is_dir()
    assert not store.state.path(store._generation(REPO_UUID, allocation.generation_id)).exists()


def test_certification_rename_rejects_transient_staging_parent_link(tmp_path: Path) -> None:
    staging_parent: Path | None = None
    parked = tmp_path / "outside-staging-race"
    swapped = False

    def race(event: str) -> None:
        nonlocal swapped
        if event == "generation:gen-rename-link:receipt_durable":
            assert staging_parent is not None
            staging_parent.rename(parked)
            staging_parent.symlink_to(parked, target_is_directory=True)
            swapped = True
        elif event == "generation:gen-rename-link:install:renamed":
            assert staging_parent is not None
            staging_parent.unlink()
            parked.rename(staging_parent)

    harness = create_harness(tmp_path, fault_hook=race)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        fault_hook=race,
    )
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=race,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-rename-link",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    staging_parent = allocation.staging_path.parent
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("{}\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)

    try:
        with pytest.raises(StatePathError):
            store.certify(
                grant,
                allocation,
                _request("c" * 40),
                declared_entries=declared,
                occurred_at=START,
                monotonic_ns=10_002,
            )
    finally:
        if staging_parent.is_symlink():
            staging_parent.unlink()
            parked.rename(staging_parent)

    assert swapped
    assert allocation.staging_path.is_dir()
    assert not store.state.path(store._generation(REPO_UUID, allocation.generation_id)).exists()


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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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

    malformed = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-noncanonical-manifest",
        occurred_at=START,
        monotonic_ns=10_003,
    )
    malformed_payload = malformed.staging_path / "graphify-out"
    malformed_payload.mkdir()
    (malformed_payload / "graph.json").write_text("initial\n", encoding="utf-8")
    bad = [dict(store.inspect_staged_payload(malformed)[0])]
    bad[0]["path"] = "graphify-out/../escape"
    with pytest.raises((ContractError, GenerationError)):
        store.certify(
            grant,
            malformed,
            _request("a" * 40, queue_watermark=2),
            declared_entries=bad,
            occurred_at=START,
            monotonic_ns=10_004,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    source_relative = store._generation(REPO_UUID, "gen-race")
    source = store.state.ensure_directory(source_relative)
    content = b"generation moved during capacity scan\n"
    (source / "graph.json").write_bytes(content)
    destination_relative = (
        store._workspace(REPO_UUID) / "quarantine" / "gc" / "gen-race.7"
    )
    original = store.state.tree_bytes
    moved = False

    def move_between_list_and_measure(
        relative: str | Path,
        *,
        allowed_directory_modes: frozenset[int],
        allowed_file_modes: frozenset[int],
    ) -> int:
        nonlocal moved
        if not moved and Path(relative) == source_relative:
            store.state.rename_contained(
                source_relative,
                destination_relative,
                label="test:capacity-list-rename",
            )
            moved = True
        return original(
            relative,
            allowed_directory_modes=allowed_directory_modes,
            allowed_file_modes=allowed_file_modes,
        )

    monkeypatch.setattr(store.state, "tree_bytes", move_between_list_and_measure)

    usage = store._usage(())

    assert moved
    assert usage.global_generations == 1
    assert usage.global_bytes == len(content)
    assert usage.workspace_generations(REPO_UUID) == 1
    assert store.state.path(destination_relative).is_dir()


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
        compatibility_manifest=COMPATIBILITY_MANIFEST,
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


def test_new_certification_without_durable_queue_authority_is_rejected(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = RuntimeGenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-queue-authority-required",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("queue required\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    observations = _observations(harness)

    with pytest.raises(
        SemanticCertificationBlocked,
        match="requires durable semantic queue authority",
    ):
        store.certify(
            grant,
            allocation,
            _request(observations),
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_002,
        )


def test_preseeded_staged_receipt_cannot_replace_durable_queue_authority(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    store = RuntimeGenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-preseeded-receipt",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("caller supplied\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    observations = _observations(harness)
    request = _request(observations)
    with harness.leases.current_operation(
        grant,
        monotonic_ns=10_002,
        allowed_operations=frozenset({"BUILD", "MIGRATE"}),
    ) as operation:
        receipt = store._receipt(operation, allocation, request, declared)
    (allocation.staging_path / "receipt.json").write_bytes(receipt.canonical)

    with pytest.raises(
        SemanticCertificationBlocked,
        match="requires durable semantic queue authority",
    ):
        store.certify(
            grant,
            allocation,
            request,
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_003,
        )

    assert (allocation.staging_path / "receipt.json").read_bytes() == receipt.canonical
    assert not store.state.private_directory_exists(
        store._generation(REPO_UUID, allocation.generation_id)
    )
    transitions = [
        event.to_dict()["transition"]
        for event in journal.recover(grant, monotonic_ns=10_004).for_generation(
            allocation.generation_id
        )
    ]
    assert transitions == ["ALLOCATED", "STAGING"]


def test_invalid_request_does_not_poison_certification_binding_retry(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    queue = _queue(harness)
    store = RuntimeGenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-invalid-request-retry",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("retryable request\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request, observations = _bind_certification(
        harness, queue, grant, declared, monotonic_ns=10_002
    )

    with pytest.raises(ContractError, match="at least one validation"):
        store.certify(
            grant,
            allocation,
            replace(request, validations=()),
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_003,
        )

    binding_path = queue._certification_binding_path(REPO_UUID, allocation.generation_id)
    assert not queue.state.path(binding_path).exists()
    receipt = store.certify(
        grant,
        allocation,
        request,
        source_observations=observations,
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_004,
    )
    assert receipt.to_dict()["generation_id"] == allocation.generation_id


def test_preseeded_receipt_with_queue_cannot_create_certification_binding(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    queue = _queue(harness)
    store = RuntimeGenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-preseeded-receipt-with-queue",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("queue cannot bless receipt\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request, observations = _bind_certification(
        harness, queue, grant, declared, monotonic_ns=10_002
    )
    with harness.leases.current_operation(
        grant,
        monotonic_ns=10_003,
        allowed_operations=frozenset({"BUILD", "MIGRATE"}),
    ) as operation:
        receipt = store._receipt(operation, allocation, request, declared)
    receipt_path = allocation.staging_path / "receipt.json"
    receipt_path.write_bytes(receipt.canonical)
    receipt_path.chmod(0o600)

    with pytest.raises(
        SemanticCertificationBlocked,
        match="staged receipt has no durable semantic certification binding",
    ):
        store.certify(
            grant,
            allocation,
            request,
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_004,
        )

    binding_path = queue._certification_binding_path(REPO_UUID, allocation.generation_id)
    assert not queue.state.path(binding_path).exists()
    receipt_path.unlink()
    installed = store.certify(
        grant,
        allocation,
        request,
        source_observations=observations,
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_005,
    )
    assert installed.to_dict()["generation_id"] == allocation.generation_id


def test_completed_queue_watermark_cannot_bind_or_certify_different_payload(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    queue = _queue(harness)
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
    )
    first = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-bound-payload-a",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    first_payload = first.staging_path / "graphify-out"
    first_payload.mkdir()
    (first_payload / "graph.json").write_text("payload a\n", encoding="utf-8")
    first_entries = store.inspect_staged_payload(first)
    request, observations = _bind_certification(
        harness, queue, grant, first_entries, monotonic_ns=10_002
    )

    second = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-bound-payload-b",
        occurred_at=START,
        monotonic_ns=10_003,
    )
    second_payload = second.staging_path / "graphify-out"
    second_payload.mkdir()
    (second_payload / "graph.json").write_text("payload b\n", encoding="utf-8")
    second_entries = store.inspect_staged_payload(second)

    with pytest.raises(SemanticQueueConflict, match="different staged inputs"):
        queue.bind_sealed_inputs(
            grant,
            sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", second_entries),
            monotonic_ns=10_004,
        )
    with pytest.raises(SemanticCertificationBlocked, match="staged inputs differ"):
        RuntimeGenerationStore.certify(
            store,
            grant,
            second,
            request,
            source_observations=observations,
            declared_entries=second_entries,
            occurred_at=START,
            monotonic_ns=10_005,
        )


@pytest.mark.parametrize(
    "failpoint",
    [
        "generation:gen-queue-recovery:receipt_durable",
        "generation:gen-queue-recovery:installed",
    ],
)
def test_durable_receipt_recovery_succeeds_after_queue_advances(
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
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    queue = _queue(harness)
    store = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
        fault_hook=fail,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-queue-recovery",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("recover me\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request, observations = _bind_certification(
        harness, queue, grant, declared, monotonic_ns=10_002
    )

    with pytest.raises(InjectedFault):
        RuntimeGenerationStore.certify(
            store,
            grant,
            allocation,
            request,
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_003,
        )

    queue.reconcile(
        grant,
        (),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=observations,
        desired_watermark=2,
        semantic_required=False,
        monotonic_ns=10_004,
    )
    receipt = RuntimeGenerationStore.certify(
        store,
        grant,
        allocation,
        request,
        source_observations=observations,
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_005,
    )

    assert receipt.to_dict()["queue_watermark"] == QUEUE_WATERMARK
    assert (
        store.verify_generation(REPO_UUID, allocation.generation_id).canonical == receipt.canonical
    )


def test_durable_certification_binding_recovers_before_receipt_after_queue_advances(
    tmp_path: Path,
) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "semantic_certification:gen-binding-recovery:installed":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=QUEUE_POLICY,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    store = RuntimeGenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        semantic_queue=queue,
    )
    allocation = store.allocate(
        grant,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-binding-recovery",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("binding recovery\n", encoding="utf-8")
    declared = store.inspect_staged_payload(allocation)
    request, observations = _bind_certification(
        harness, queue, grant, declared, monotonic_ns=10_002
    )

    with pytest.raises(CommitUnknown, match="semantic_certification:gen-binding-recovery"):
        store.certify(
            grant,
            allocation,
            request,
            source_observations=observations,
            declared_entries=declared,
            occurred_at=START,
            monotonic_ns=10_003,
        )

    binding_path = queue._certification_binding_path(REPO_UUID, allocation.generation_id)
    assert queue.state.read_existing_bytes(binding_path)
    assert not (allocation.staging_path / "receipt.json").exists()

    queue.reconcile(
        grant,
        (),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=observations,
        desired_watermark=2,
        semantic_required=False,
        monotonic_ns=10_004,
    )
    receipt = store.certify(
        grant,
        allocation,
        request,
        source_observations=observations,
        declared_entries=declared,
        occurred_at=START,
        monotonic_ns=10_005,
    )

    assert receipt.to_dict()["queue_watermark"] == QUEUE_WATERMARK
    assert queue.inspect(REPO_UUID).desired_watermark == 2
    assert (
        store.verify_generation(REPO_UUID, allocation.generation_id).canonical == receipt.canonical
    )
