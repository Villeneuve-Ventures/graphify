"""Fresh-process recovery of staged certification crash boundaries."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import CapacityPolicy, payload_manifest_sha256
from graphify.workspace.generations import (
    CertificationRequest,
    GenerationStore,
    StagedBuildCompletion,
    StagedBuildOperation,
    StructuralBuildRequest,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseStore
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from graphify.workspace.semantic_queue import SemanticQueuePolicy, SemanticQueueStore

from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    REPO_UUID,
    START,
    RuntimeHarness,
    create_harness,
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
GENERATION_ID = "gen-staged-certification-recovery"


def _observations(harness: RuntimeHarness) -> tuple[SourceObservation, SourceObservation]:
    observation = SourceObservation(
        source_commit=discover_source(harness.repo).head_commit,
        inventory_sha256="c" * 64,
        policy_sha256="b" * 64,
        detector_id="test-staged-certification-recovery",
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


def _stores(
    harness: RuntimeHarness,
    *,
    fault_hook: Any = None,
    fresh_leases: bool = False,
) -> tuple[GenerationStore, LeaseStore]:
    leases = (
        LeaseStore(
            harness.state_root,
            harness.registry,
            capabilities=harness.leases.state.capabilities,
        )
        if fresh_leases
        else harness.leases
    )
    journal = JournalStore(
        harness.state_root,
        leases,
        capabilities=leases.state.capabilities,
        fault_hook=fault_hook,
    )
    queue = SemanticQueueStore(
        harness.state_root,
        leases,
        policy=QUEUE_POLICY,
        capabilities=leases.state.capabilities,
    )
    return (
        GenerationStore(
            harness.state_root,
            leases,
            journal,
            semantic_queue=queue,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            capabilities=leases.state.capabilities,
            fault_hook=fault_hook,
        ),
        leases,
    )


def _certification_request(
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


def _bind_queue(
    store: GenerationStore,
    attempt: StagedBuildOperation,
    completion: StagedBuildCompletion,
    observations: tuple[SourceObservation, SourceObservation],
    *,
    monotonic_ns: int,
) -> None:
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


def _certify(
    store: GenerationStore,
    attempt: StagedBuildOperation,
    completion: StagedBuildCompletion,
    observations: tuple[SourceObservation, SourceObservation],
    *,
    monotonic_ns: int,
):
    _bind_queue(store, attempt, completion, observations, monotonic_ns=monotonic_ns)
    trust_source_observations(store, observations)
    return store.certify(
        attempt.grant,
        completion.allocation,
        _certification_request(observations),
        source_observations=observations,
        declared_entries=completion.entries,
        staged_completion=completion,
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=monotonic_ns + 2,
    )


def _complete(
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


def _reconstruct_completion(
    harness: RuntimeHarness,
    store: GenerationStore,
    request: StructuralBuildRequest,
    grant: Any,
    observations: tuple[SourceObservation, SourceObservation],
) -> tuple[StagedBuildOperation, StagedBuildCompletion]:
    state = store._load_staged_build_locked(REPO_UUID)
    assert state is not None
    assert state.lifecycle_state == "COMPLETE"
    attempt = StagedBuildOperation(state=state, grant=grant)
    allocation = store.allocate(
        grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id=GENERATION_ID,
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
    )
    preparation = store.prepare_staged_build(attempt, allocation, monotonic_ns=20_001)
    trust_source_observations(store, observations)
    completion = store.complete_staged_build(
        preparation,
        source_observations=observations,
        monotonic_ns=20_002,
    )
    return attempt, completion


_FAILPOINTS = (
    "generation:gen-staged-certification-recovery:payload_file_durable:graphify-out/graph.json",
    "generation:gen-staged-certification-recovery:payload_durable",
    "generation:gen-staged-certification-recovery:before_reinventory",
    "generation:gen-staged-certification-recovery:receipt:installed",
    "generation:gen-staged-certification-recovery:install:before_rename",
    "generation:gen-staged-certification-recovery:install:renamed",
    "generation:gen-staged-certification-recovery:install:source_parent_durable",
    "generation:gen-staged-certification-recovery:install:destination_parent_durable",
    "generation:gen-staged-certification-recovery:installed",
    "journal:CERTIFIED:segment:installed",
    "journal:CERTIFIED:segment_durable",
    "journal:CERTIFIED:head:current_replaced",
    "journal:CERTIFIED:head_durable",
)


@pytest.mark.parametrize("failpoint", _FAILPOINTS)
def test_fresh_process_reconstructs_complete_wrapper_after_certification_crash(
    tmp_path: Path,
    failpoint: str,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == failpoint:
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    store, _leases = _stores(harness, fault_hook=fail)
    observations = _observations(harness)
    request, attempt, _completion = _complete(harness, store, observations)
    armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        _certify(store, attempt, _completion, observations, monotonic_ns=10_004)

    fresh_store, _fresh_leases = _stores(harness, fresh_leases=True)
    recovered_attempt, recovered_completion = _reconstruct_completion(
        harness,
        fresh_store,
        request,
        attempt.grant,
        observations,
    )
    receipt = _certify(
        fresh_store,
        recovered_attempt,
        recovered_completion,
        observations,
        monotonic_ns=20_003,
    )
    recovered = fresh_store._load_staged_build_locked(REPO_UUID)

    assert recovered_completion.state.lifecycle_state == "COMPLETE"
    assert recovered_completion.entries == _completion.entries
    assert receipt.to_dict()["generation_id"] == GENERATION_ID
    assert recovered is not None
    assert recovered.lifecycle_state == "CERTIFIED"
    assert recovered.receipt_sha256 == receipt.sha256


def test_fresh_process_reconstructs_completion_from_durable_staging_receipt(
    tmp_path: Path,
) -> None:
    failpoint = f"generation:{GENERATION_ID}:receipt_durable"
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == failpoint:
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    store, _leases = _stores(harness, fault_hook=fail)
    observations = _observations(harness)
    request, attempt, completion = _complete(harness, store, observations)
    armed = True

    with pytest.raises(InjectedFault, match="receipt_durable"):
        _certify(store, attempt, completion, observations, monotonic_ns=10_004)

    staging_receipt = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staging"
        / GENERATION_ID
        / "receipt.json"
    )
    assert staging_receipt.is_file()
    fresh_store, _fresh_leases = _stores(harness, fresh_leases=True)
    recovered_attempt, recovered_completion = _reconstruct_completion(
        harness,
        fresh_store,
        request,
        attempt.grant,
        observations,
    )

    receipt = _certify(
        fresh_store,
        recovered_attempt,
        recovered_completion,
        observations,
        monotonic_ns=20_003,
    )

    final_receipt = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / GENERATION_ID
        / "receipt.json"
    )
    assert final_receipt.read_bytes() == receipt.canonical
