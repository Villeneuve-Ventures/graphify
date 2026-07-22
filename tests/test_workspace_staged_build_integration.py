"""P5B2b0 integration coverage from staged completion through promotion recovery."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    CapacityPolicy,
    CompatibilityManifest,
    payload_manifest_sha256,
)
from graphify.workspace.generations import (
    CertificationRequest,
    GenerationConflict,
    GenerationStore,
    StagedBuildCompletion,
    StagedBuildOperation,
    StructuralBuildRequest,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseRecoveryRequired, LeaseStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from graphify.workspace.pointers import PointerCAS, PointerStore
from graphify.workspace.semantic_queue import (
    SemanticCertificationBlocked,
    SemanticQueuePolicy,
    SemanticQueueStore,
)

from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    REPO_UUID,
    START,
    RuntimeHarness,
    acquire,
    create_harness,
    tree_snapshot,
    trust_source_observations,
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
QUEUE_POLICY = SemanticQueuePolicy(max_items=16, max_bytes=1024 * 1024, retry_budget=1)
GENERATION_ID = "gen-staged-integration"


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


def _observations(harness: RuntimeHarness) -> tuple[SourceObservation, SourceObservation]:
    observation = SourceObservation(
        source_commit=discover_source(harness.repo).head_commit,
        inventory_sha256="c" * 64,
        policy_sha256="b" * 64,
        detector_id="test-workspace-staged-build-integration",
        stable_inventory_passes=2,
        entries=(),
    )
    return observation, observation


def _request(
    harness: RuntimeHarness,
    observations: tuple[SourceObservation, SourceObservation],
) -> StructuralBuildRequest:
    registry = harness.registry.load().to_dict()
    entry = registry["workspaces"][0]
    lease_state = harness.leases.inspect(REPO_UUID)
    return StructuralBuildRequest.from_mapping(
        {
            "logical_request_sha256": "a" * 64,
            "expected_registry_revision": int(registry["revision"]),
            "expected_active_source_revision": int(entry["active_source_revision"]),
            "expected_operation_epoch": lease_state.operation_epoch,
            "expected_migration_epoch": lease_state.migration_epoch,
            "expected_pointer_revision": 0,
            "expected_current_receipt_sha256": None,
            "source_commit": observations[0].source_commit,
            "source_epoch": 1,
            "policy_sha256": observations[0].policy_sha256,
            "observation_manifest_sha256": observations[0].inventory_sha256,
            "observation_evidence_sha256": GenerationStore.structural_observation_evidence_sha256(
                observations
            ),
            "expected_payload_bytes": 4096,
            "capacity_policy_sha256": POLICY.sha256,
            "compatibility_sha256": COMPATIBILITY_MANIFEST.sha256,
        }
    )


def _runtime(
    tmp_path: Path,
    *,
    fault_hook: Any = None,
) -> tuple[RuntimeHarness, GenerationStore, PointerStore, tuple[SourceObservation, SourceObservation]]:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=QUEUE_POLICY,
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        semantic_queue=queue,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
    )
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    return harness, generations, pointers, _observations(harness)


def _completed_staged_build(
    harness: RuntimeHarness,
    store: GenerationStore,
    observations: tuple[SourceObservation, SourceObservation],
) -> tuple[StructuralBuildRequest, StagedBuildOperation, StagedBuildCompletion]:
    request = _request(harness, observations)
    trust_source_observations(store, observations)
    store.request_staged_build(REPO_UUID, GENERATION_ID, request, source_observations=observations)
    attempt = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        attempt.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id=GENERATION_ID,
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=10_001,
    )
    preparation = store.prepare_staged_build(attempt, allocation, monotonic_ns=10_002)
    payload = preparation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("{}\n", encoding="utf-8")
    trust_source_observations(store, observations)
    completion = store.complete_staged_build(
        preparation,
        source_observations=observations,
        monotonic_ns=10_003,
    )
    return request, attempt, completion


def _certification_request(
    completion: StagedBuildCompletion,
    observations: tuple[SourceObservation, SourceObservation],
) -> CertificationRequest:
    return CertificationRequest(
        source_commit=observations[0].source_commit,
        source_epoch=1,
        policy_sha256=observations[0].policy_sha256,
        observation_manifest_sha256=observations[0].inventory_sha256,
        queue_watermark=1,
        semantic_completeness="not_required",
        compatibility_sha256=COMPATIBILITY_SHA256,
        validations=(
            "payload_manifest",
            "coordination_lock_precreated",
            "stable_semantic_queue",
        ),
    )


def _certify_staged_build(
    store: GenerationStore,
    attempt: StagedBuildOperation,
    completion: StagedBuildCompletion,
    observations: tuple[SourceObservation, SourceObservation],
    *,
    monotonic_ns: int = 10_004,
):
    queue = store.semantic_queue
    assert queue is not None
    queue.reconcile(
        attempt.grant,
        (),
        source_epoch=1,
        policy_sha256=observations[0].policy_sha256,
        source_observations=observations,
        desired_watermark=1,
        semantic_required=False,
        monotonic_ns=monotonic_ns,
    )
    queue.bind_sealed_inputs(
        attempt.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
        monotonic_ns=monotonic_ns + 1,
    )
    trust_source_observations(store, observations)
    return store.certify(
        attempt.grant,
        completion.allocation,
        _certification_request(completion, observations),
        source_observations=observations,
        declared_entries=completion.entries,
        staged_completion=completion,
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=monotonic_ns + 2,
    )


def _cas(grant: Any, receipt: Any) -> PointerCAS:
    value = receipt.to_dict()
    return PointerCAS(
        expected_pointer_revision=0,
        expected_active_source_revision=grant.active_source_revision,
        expected_source_epoch=int(value["source_epoch"]),
        expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        expected_state_schema_version=1,
        expected_fence_token=int(grant.lease.to_dict()["fence_token"]),
        candidate_generation_id=GENERATION_ID,
        candidate_receipt_sha256=receipt.sha256,
        expected_current_receipt_sha256=None,
    )


def test_certify_marks_complete_staged_build_certified(tmp_path: Path) -> None:
    harness, store, _pointers, observations = _runtime(tmp_path)
    _request_value, attempt, completion = _completed_staged_build(harness, store, observations)

    receipt = _certify_staged_build(store, attempt, completion, observations)

    state = store._load_staged_build_locked(REPO_UUID)
    assert receipt.to_dict()["generation_id"] == GENERATION_ID
    assert state is not None
    assert state.lifecycle_state == "CERTIFIED"
    assert state.receipt_sha256 == receipt.sha256


def test_certification_rejects_missing_stable_queue_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store, _pointers, observations = _runtime(tmp_path)
    _request_value, attempt, completion = _completed_staged_build(
        harness,
        store,
        observations,
    )
    queue = store.semantic_queue
    assert queue is not None
    monkeypatch.setattr(
        queue,
        "certification_view",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        SemanticCertificationBlocked,
        match="requires a stable semantic queue view",
    ):
        _certify_staged_build(store, attempt, completion, observations)


def test_certification_rejects_missing_final_staged_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store, _pointers, observations = _runtime(tmp_path)
    _request_value, attempt, completion = _completed_staged_build(
        harness,
        store,
        observations,
    )
    original = store._require_staged_certification_locked

    def lose_final_authority(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        final = store._generation(REPO_UUID, GENERATION_ID)
        return None if store.state.private_directory_exists(final) else result

    monkeypatch.setattr(
        store,
        "_require_staged_certification_locked",
        lose_final_authority,
    )

    with pytest.raises(
        GenerationConflict,
        match="staged build authority disappeared during certification",
    ):
        _certify_staged_build(store, attempt, completion, observations)


def test_request_bound_promote_acquisition_bypasses_generic_barrier_and_clears_capacity(
    tmp_path: Path,
) -> None:
    armed = False

    def fail_after_staged_certification(event: str) -> None:
        nonlocal armed
        if armed and event == f"generation:{GENERATION_ID}:staged_certified_durable":
            armed = False
            raise InjectedFault(event)

    harness, store, _pointers, observations = _runtime(tmp_path, fault_hook=fail_after_staged_certification)
    request, attempt, completion = _completed_staged_build(harness, store, observations)
    armed = True
    with pytest.raises(InjectedFault):
        _certify_staged_build(store, attempt, completion, observations)
    harness.leases.release(attempt.grant)

    with pytest.raises(LeaseRecoveryRequired, match="staged build"):
        acquire(harness, "PROMOTE", tick=3)

    promote = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="PROMOTE",
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    with harness.leases.workspace_lock(REPO_UUID):
        capacity = store._load_capacity_locked()
    assert promote.state.lifecycle_state == "CERTIFIED"
    assert capacity is not None
    assert capacity.reservations == ()


def test_complete_staged_promotion_records_terminal_state_without_replaying_pointer_move(
    tmp_path: Path,
) -> None:
    harness, store, pointers, observations = _runtime(tmp_path)
    request, build, completion = _completed_staged_build(harness, store, observations)
    receipt = _certify_staged_build(store, build, completion, observations)
    harness.leases.release(build.grant)
    promote = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="PROMOTE",
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )
    pointer = pointers.promote(
        promote.grant,
        _cas(promote.grant, receipt),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_001,
    )

    terminal = store.complete_staged_promotion(promote, pointer, monotonic_ns=30_002)
    before_retry = tree_snapshot(harness.state_root)
    retry = store.complete_staged_promotion(promote, pointer, monotonic_ns=30_003)
    trust_source_observations(store, observations)
    request_retry = store.request_staged_build(
        REPO_UUID,
        GENERATION_ID,
        request,
        source_observations=observations,
    )

    assert terminal.lifecycle_state == "PROMOTED"
    assert retry == terminal
    assert request_retry == terminal
    assert tree_snapshot(harness.state_root) == before_retry


def test_fresh_manifest_completes_pointer_visible_promotion(
    tmp_path: Path,
) -> None:
    harness, store, pointers, observations = _runtime(tmp_path)
    request, build, completion = _completed_staged_build(harness, store, observations)
    receipt = _certify_staged_build(store, build, completion, observations)
    harness.leases.release(build.grant)
    promote = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="PROMOTE",
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )
    pointer = pointers.promote(
        promote.grant,
        _cas(promote.grant, receipt),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_001,
    )
    fresh_leases = LeaseStore(
        harness.state_root,
        harness.registry,
        capabilities=harness.leases.state.capabilities,
    )
    fresh_store = GenerationStore(
        harness.state_root,
        fresh_leases,
        JournalStore(
            harness.state_root,
            fresh_leases,
            capabilities=fresh_leases.state.capabilities,
        ),
        compatibility_manifest=_alternate_manifest(),
        capabilities=fresh_leases.state.capabilities,
    )

    terminal = fresh_store.complete_staged_promotion(
        promote,
        pointer,
        monotonic_ns=30_002,
    )

    assert terminal.lifecycle_state == "PROMOTED"
    assert terminal.pointer_revision == int(pointer.to_dict()["pointer_revision"])


def test_successor_build_recovers_after_certification_journal_precedes_staged_state_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store, _pointers, observations = _runtime(tmp_path)
    request, attempt, completion = _completed_staged_build(harness, store, observations)

    def interrupt_staged_state_commit(*_args: object, **_kwargs: object) -> None:
        raise InjectedFault("after_authoritative_certification")

    monkeypatch.setattr(store, "_mark_staged_certified_locked", interrupt_staged_state_commit)
    with pytest.raises(InjectedFault, match="after_authoritative_certification"):
        _certify_staged_build(store, attempt, completion, observations)
    assert store.journal.recover(attempt.grant, monotonic_ns=10_007).for_generation(GENERATION_ID)[-1].to_dict()[
        "transition"
    ] == "CERTIFIED"
    assert store._load_staged_build_locked(REPO_UUID).lifecycle_state == "COMPLETE"  # type: ignore[union-attr]
    harness.leases.release(attempt.grant)
    monkeypatch.undo()

    successor = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        successor.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id=GENERATION_ID,
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_001,
    )
    recovered_completion = store.complete_staged_build(
        store.prepare_staged_build(successor, allocation, monotonic_ns=30_002),
        source_observations=observations,
        monotonic_ns=30_003,
    )
    receipt = _certify_staged_build(
        store,
        successor,
        recovered_completion,
        observations,
        monotonic_ns=30_004,
    )
    recovered_state = store._load_staged_build_locked(REPO_UUID)

    assert recovered_completion.state.lifecycle_state == "COMPLETE"
    assert recovered_completion.allocation.operation_epoch == successor.grant.operation_epoch
    assert receipt.to_dict()["generation_id"] == GENERATION_ID
    assert recovered_state is not None
    assert recovered_state.lifecycle_state == "CERTIFIED"


@pytest.mark.parametrize("operation", ["PROMOTE", "POINTER_RECOVERY"])
def test_successor_request_bound_operation_acknowledges_already_current_pointer(
    tmp_path: Path,
    operation: str,
) -> None:
    harness, store, pointers, observations = _runtime(tmp_path)
    request, build, completion = _completed_staged_build(harness, store, observations)
    receipt = _certify_staged_build(store, build, completion, observations)
    harness.leases.release(build.grant)
    first = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="PROMOTE",
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1,
    )
    pointer = pointers.promote(
        first.grant,
        _cas(first.grant, receipt),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
    )

    successor = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation=operation,
        acquired_at=START + timedelta(seconds=4),
        monotonic_ns=40_000,
        ttl_ns=1_000_000,
    )
    terminal = store.complete_staged_promotion(successor, pointer, monotonic_ns=40_001)

    assert terminal.lifecycle_state == "PROMOTED"
    assert terminal.pointer_revision == int(pointer.to_dict()["pointer_revision"])


def test_terminal_promotion_record_recovers_unknown_commit(tmp_path: Path) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == f"staged-build:{REPO_UUID}:pending_durable":
            armed = False
            raise InjectedFault(event)

    harness, store, pointers, observations = _runtime(tmp_path, fault_hook=fail)
    request, build, completion = _completed_staged_build(harness, store, observations)
    receipt = _certify_staged_build(store, build, completion, observations)
    harness.leases.release(build.grant)
    promote = store.acquire_staged_operation(
        REPO_UUID,
        GENERATION_ID,
        request,
        operation="PROMOTE",
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )
    pointer = pointers.promote(
        promote.grant,
        _cas(promote.grant, receipt),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_001,
    )
    armed = True

    with pytest.raises(CommitUnknown):
        store.complete_staged_promotion(promote, pointer, monotonic_ns=30_002)

    recovered = store.complete_staged_promotion(
        promote,
        pointer,
        monotonic_ns=30_003,
    )
    assert recovered.lifecycle_state == "PROMOTED"
