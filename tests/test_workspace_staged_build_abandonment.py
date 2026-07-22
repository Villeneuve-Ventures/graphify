"""P5B2b0 regression coverage for stale staged-build abandonment.

The recovery API deliberately does not infer a replacement request: it closes
only the exact request after its authority has drifted.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
from typing import Any, cast

import pytest

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    CapacityPolicy,
    CompatibilityManifest,
    ContractError,
    PointerSet,
    StagedBuildAbandonmentEvidence,
    StagedBuildState,
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
from graphify.workspace.identity import IdentityAction, OperatorAuthorization, discover_source
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
GENERATION_ID = "gen-staged-abandonment"


def _observations(
    harness: RuntimeHarness, inventory: str = "c" * 64
) -> tuple[SourceObservation, SourceObservation]:
    observation = SourceObservation(
        source_commit=discover_source(harness.repo).head_commit,
        inventory_sha256=inventory,
        policy_sha256="b" * 64,
        detector_id="test-workspace-staged-build-abandonment",
        stable_inventory_passes=2,
        entries=(),
    )
    return observation, observation


def _runtime(
    tmp_path: Path, *, fault_hook: Any = None
) -> tuple[RuntimeHarness, GenerationStore, PointerStore]:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root, harness.leases, capabilities=harness.leases.state.capabilities
    )
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=QUEUE_POLICY,
        capabilities=harness.leases.state.capabilities,
    )
    store = GenerationStore(
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
        store,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    return harness, store, pointers


def _reopen_store(
    harness: RuntimeHarness,
    *,
    compatibility_manifest: CompatibilityManifest = COMPATIBILITY_MANIFEST,
) -> GenerationStore:
    leases = LeaseStore(
        harness.state_root,
        harness.registry,
        capabilities=harness.leases.state.capabilities,
    )
    journal = JournalStore(
        harness.state_root,
        leases,
        capabilities=leases.state.capabilities,
    )
    queue = SemanticQueueStore(
        harness.state_root,
        leases,
        policy=QUEUE_POLICY,
        capabilities=leases.state.capabilities,
    )
    return GenerationStore(
        harness.state_root,
        leases,
        journal,
        semantic_queue=queue,
        compatibility_manifest=compatibility_manifest,
        capabilities=leases.state.capabilities,
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


def _request(
    harness: RuntimeHarness, observations: tuple[SourceObservation, SourceObservation]
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


def _requested(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> None:
    trust_source_observations(store, observations)
    store.request_staged_build(REPO_UUID, GENERATION_ID, request, source_observations=observations)


def _publishing(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> tuple[StagedBuildOperation, Any]:
    _requested(store, harness, request, observations)
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
    return attempt, store.prepare_staged_build(attempt, allocation, monotonic_ns=10_002)


def _completed(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> tuple[StagedBuildOperation, StagedBuildCompletion]:
    attempt, preparation = _publishing(store, harness, request, observations)
    payload = preparation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text("{}\n", encoding="utf-8")
    trust_source_observations(store, observations)
    return attempt, store.complete_staged_build(
        preparation, source_observations=observations, monotonic_ns=10_003
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
        monotonic_ns=10_004,
    )
    queue.bind_sealed_inputs(
        attempt.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
        monotonic_ns=10_005,
    )


def _crash_with_durable_staging_receipt(
    tmp_path: Path,
) -> tuple[
    RuntimeHarness,
    GenerationStore,
    StructuralBuildRequest,
    StagedBuildOperation,
    tuple[SourceObservation, SourceObservation],
]:
    failpoint = f"generation:{GENERATION_ID}:receipt_durable"
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == failpoint:
            armed = False
            raise InjectedFault(event)

    harness, store, _pointers = _runtime(tmp_path, fault_hook=fail)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, completion = _completed(store, harness, request, observations)
    _bind_queue(store, build, completion, observations)
    armed = True
    trust_source_observations(store, observations)
    with pytest.raises(InjectedFault, match="receipt_durable"):
        store.certify(
            build.grant,
            completion.allocation,
            _certification_request(observations),
            source_observations=observations,
            declared_entries=completion.entries,
            staged_completion=completion,
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_006,
        )
    return harness, store, request, build, observations


def _certified(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> tuple[StagedBuildOperation, StagedBuildCompletion, Any]:
    attempt, completion = _completed(store, harness, request, observations)
    _bind_queue(store, attempt, completion, observations)
    trust_source_observations(store, observations)
    receipt = store.certify(
        attempt.grant,
        completion.allocation,
        _certification_request(observations),
        source_observations=observations,
        declared_entries=completion.entries,
        staged_completion=completion,
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=10_006,
    )
    return attempt, completion, receipt


def _abandon(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    drifted: tuple[SourceObservation, SourceObservation],
    *,
    tick: int = 2,
):
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=tick),
        monotonic_ns=tick * 10_000,
        ttl_ns=1_000_000,
    )
    return attempt, store.abandon_staged_build(
        attempt, source_observations=drifted, monotonic_ns=tick * 10_000 + 1
    )


def _advance_active_source_revision(harness: RuntimeHarness) -> None:
    """Model a separately-authoritative active-source change after request install."""

    state = harness.leases.inspect(REPO_UUID)
    with harness.registry.exclusive_lock():
        current = harness.registry._load_locked()
        assert current is not None
        entries = current.to_dict()["workspaces"]
        entry = entries[0]
        next_registry_revision = int(current.to_dict()["revision"]) + 1
        next_revision = int(entry["active_source_revision"]) + 1
        source = discover_source(harness.repo)
        evidence_sha256 = harness.registry._authorized_evidence(
            source,
            OperatorAuthorization(
                action=IdentityAction.ACTIVATE,
                operator_id="operator:staged-abandonment-test",
                reason="model active-source authority drift",
                issued_at="2026-07-16T19:00:00Z",
                nonce="staged-abandonment-active-source-drift",
            ),
            registry_revision=next_registry_revision,
            active_source_revision=next_revision,
            operation_epoch=state.operation_epoch + 1,
            fence_token=state.fence_high_watermark + 1,
        )
        entry["active_source_revision"] = next_revision
        entry["active_source_evidence"] = {
            "active_source_revision": next_revision,
            "source_sha256": source.source_sha256,
            "rebind_evidence_sha256": evidence_sha256,
            "operation_epoch": state.operation_epoch + 1,
            "fence_token": state.fence_high_watermark + 1,
        }
        harness.registry._commit_locked(
            harness.registry._document_value(
                current,
                next_registry_revision,
                entries,
            )
        )


def _advance_migration_epoch(harness: RuntimeHarness) -> None:
    """Model a completed migration whose epoch outlives the old request CAS."""

    with harness.registry.recovered_snapshot() as document:
        with harness.leases.workspace_lock(REPO_UUID):
            state = harness.leases._load_state_locked(document, REPO_UUID)
            harness.leases._commit_state_locked(
                replace(
                    state,
                    revision=state.revision + 1,
                    migration_epoch=state.migration_epoch + 1,
                )
            )


def _install_unrelated_visible_pointer(harness: RuntimeHarness, pointers: PointerStore) -> None:
    """Install valid visible CAS authority representing an external pointer advance."""

    pointer = PointerSet.from_mapping(
        {
            "contract": "graphify.workspace.pointer_set",
            "schema_version": 1,
            "repo_uuid": REPO_UUID,
            "pointer_revision": 1,
            "active_source_revision": 1,
            "source_epoch": 1,
            "operation_epoch": 1,
            "fence_token": 1,
            "state_schema_version": 1,
            "current": {
                "generation_id": "gen-unrelated-pointer",
                "receipt_sha256": "e" * 64,
            },
            "last_good": None,
        }
    )
    pointers.state.write_once(pointers._current(REPO_UUID), pointer.canonical)


@pytest.mark.parametrize("lifecycle", ["REQUESTED", "PUBLISHING", "COMPLETE", "CERTIFIED"])
def test_source_drift_can_terminally_abandon_each_recoverable_staged_state(
    tmp_path: Path, lifecycle: str
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    if lifecycle == "REQUESTED":
        _requested(store, harness, request, observations)
    elif lifecycle == "PUBLISHING":
        _publishing(store, harness, request, observations)
    elif lifecycle == "COMPLETE":
        _completed(store, harness, request, observations)
    else:
        certified_attempt, _completion, _receipt = _certified(store, harness, request, observations)
        harness.leases.release(certified_attempt.grant)
    drifted = _observations(harness, "d" * 64)

    # A stale caller may still inspect its exact durable request, but it cannot
    # start a new build from drifted evidence.
    replay = store.request_staged_build(
        REPO_UUID, GENERATION_ID, request, source_observations=drifted
    )
    attempt, abandoned = _abandon(store, harness, request, drifted)
    harness.leases.release(attempt.grant)

    assert replay.lifecycle_state == lifecycle
    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == lifecycle
    assert abandoned.abandon_reason == "SOURCE_CHANGED"
    assert abandoned.abandon_evidence is not None
    assert abandoned.abandon_evidence.reason_for(request) == "SOURCE_CHANGED"
    assert abandoned.abandon_evidence_sha256 == abandoned.abandon_evidence.sha256
    if lifecycle == "CERTIFIED":
        assert (
            harness.state_root / "workspaces" / REPO_UUID / "generations" / GENERATION_ID
        ).is_dir()
    else:
        assert not (
            harness.state_root / "workspaces" / REPO_UUID / "staging" / GENERATION_ID
        ).exists()


def test_abandoned_staged_build_releases_generic_lease_barrier(tmp_path: Path) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    attempt, _abandoned = _abandon(store, harness, request, _observations(harness, "d" * 64))
    harness.leases.release(attempt.grant)

    grant = acquire(harness, "BUILD", tick=4)
    assert grant.lease.to_dict()["operation"] == "BUILD"


def test_abandoned_staged_build_allows_a_new_exact_request(tmp_path: Path) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    drifted = _observations(harness, "d" * 64)
    attempt, _abandoned = _abandon(store, harness, request, drifted)
    harness.leases.release(attempt.grant)

    successor = _request(harness, drifted)
    trust_source_observations(store, drifted)
    requested = store.request_staged_build(
        REPO_UUID,
        "gen-staged-successor",
        successor,
        source_observations=drifted,
    )

    assert requested.lifecycle_state == "REQUESTED"
    assert requested.generation_id == "gen-staged-successor"
    assert requested.request == successor


def test_active_source_revision_drift_abandons_unallocated_request_without_replacement(
    tmp_path: Path,
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    _advance_active_source_revision(harness)

    attempt, abandoned = _abandon(store, harness, request, observations)

    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == "REQUESTED"
    assert abandoned.abandon_reason == "ACTIVE_SOURCE_CHANGED"
    assert abandoned.request == request
    harness.leases.release(attempt.grant)


def test_migration_epoch_drift_abandons_publishing_request_without_replacement(
    tmp_path: Path,
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, _preparation = _publishing(store, harness, request, observations)
    harness.leases.release(build.grant)
    _advance_migration_epoch(harness)

    attempt, abandoned = _abandon(store, harness, request, observations, tick=3)

    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == "PUBLISHING"
    assert abandoned.abandon_reason == "MIGRATION_CHANGED"
    assert abandoned.request == request
    harness.leases.release(attempt.grant)


def test_visible_pointer_cas_drift_abandons_unallocated_request_without_replacement(
    tmp_path: Path,
) -> None:
    harness, store, pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    _install_unrelated_visible_pointer(harness, pointers)

    attempt, abandoned = _abandon(store, harness, request, observations)

    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == "REQUESTED"
    assert abandoned.abandon_reason == "POINTER_CHANGED"
    assert abandoned.request == request
    harness.leases.release(attempt.grant)


def test_semantic_source_epoch_drift_terminally_abandons_completed_request(
    tmp_path: Path,
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, completion = _completed(store, harness, request, observations)
    queue = store.semantic_queue
    assert queue is not None
    queue.reconcile(
        build.grant,
        (),
        source_epoch=2,
        policy_sha256=observations[0].policy_sha256,
        source_observations=observations,
        desired_watermark=2,
        semantic_required=False,
        monotonic_ns=10_004,
    )
    bound = queue.bind_sealed_inputs(
        build.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256(
            "graphify-out", completion.entries
        ),
        monotonic_ns=10_005,
    )
    trust_source_observations(store, observations)

    with pytest.raises(SemanticCertificationBlocked, match="source epoch"):
        store.certify(
            build.grant,
            completion.allocation,
            _certification_request(observations),
            source_observations=observations,
            declared_entries=completion.entries,
            staged_completion=completion,
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_006,
        )
    harness.leases.release(build.grant)

    attempt, abandoned = _abandon(
        store,
        harness,
        request,
        observations,
        tick=3,
    )
    harness.leases.release(attempt.grant)

    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandon_reason == "SEMANTIC_SOURCE_EPOCH_CHANGED"
    assert abandoned.abandon_evidence is not None
    assert abandoned.abandon_evidence.semantic_source_epoch == 2
    assert abandoned.abandon_evidence.semantic_queue_watermark == 2
    assert abandoned.abandon_evidence.semantic_queue_state_sha256 == bound.sha256

    rewritten = abandoned.to_dict()
    evidence_value = cast(dict[str, Any], rewritten["abandon_evidence"])
    semantic_value = cast(dict[str, Any], evidence_value["semantic_queue"])
    semantic_value["source_epoch"] = request.source_epoch
    rewritten_evidence = StagedBuildAbandonmentEvidence.from_mapping(evidence_value)
    rewritten["abandon_evidence_sha256"] = rewritten_evidence.sha256
    with pytest.raises(ContractError, match="does not prove stale staged-build authority"):
        StagedBuildState.from_mapping(rewritten)

    generic = acquire(harness, "BUILD", tick=4)
    harness.leases.release(generic)


@pytest.mark.parametrize("lifecycle", ["REQUESTED", "PUBLISHING", "COMPLETE", "CERTIFIED"])
def test_fresh_manifest_drift_terminally_abandons_each_recoverable_staged_state(
    tmp_path: Path,
    lifecycle: str,
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    if lifecycle == "REQUESTED":
        _requested(store, harness, request, observations)
    elif lifecycle == "PUBLISHING":
        attempt, _preparation = _publishing(store, harness, request, observations)
        harness.leases.release(attempt.grant)
    elif lifecycle == "COMPLETE":
        attempt, _completion = _completed(store, harness, request, observations)
        harness.leases.release(attempt.grant)
    else:
        attempt, _completion, _receipt = _certified(
            store,
            harness,
            request,
            observations,
        )
        harness.leases.release(attempt.grant)
    alternate_manifest = _alternate_manifest()
    fresh_store = _reopen_store(
        harness,
        compatibility_manifest=alternate_manifest,
    )
    restored = trust_source_observations(fresh_store, observations)

    replay = fresh_store.request_staged_build(
        REPO_UUID,
        GENERATION_ID,
        request,
        source_observations=observations,
    )
    assert replay.lifecycle_state == lifecycle
    assert restored.calls == 0
    recovery = fresh_store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
        ttl_ns=1_000_000,
    )
    abandoned = fresh_store.abandon_staged_build(
        recovery,
        source_observations=observations,
        monotonic_ns=20_001,
    )
    fresh_store.leases.release(recovery.grant)

    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == lifecycle
    assert abandoned.abandon_reason == "COMPATIBILITY_CHANGED"
    assert abandoned.abandon_evidence is not None
    assert (
        abandoned.abandon_evidence.selected_compatibility_sha256
        == alternate_manifest.sha256
    )

    rewritten = abandoned.to_dict()
    evidence_value = cast(dict[str, Any], rewritten["abandon_evidence"])
    evidence_value["selected_compatibility_sha256"] = request.compatibility_sha256
    rewritten_evidence = StagedBuildAbandonmentEvidence.from_mapping(evidence_value)
    rewritten["abandon_evidence_sha256"] = rewritten_evidence.sha256
    with pytest.raises(ContractError, match="does not prove stale staged-build authority"):
        StagedBuildState.from_mapping(rewritten)

    generic = acquire(harness, "BUILD", tick=3)
    harness.leases.release(generic)
    successor = replace(
        _request(harness, observations),
        logical_request_sha256="f" * 64,
        compatibility_sha256=alternate_manifest.sha256,
    )
    trust_source_observations(fresh_store, observations)
    successor_state = fresh_store.request_staged_build(
        REPO_UUID,
        "gen-staged-after-compatibility-change",
        successor,
        source_observations=observations,
    )
    assert successor_state.lifecycle_state == "REQUESTED"


def test_abandoned_terminal_commit_unknown_recovers_by_exact_request_replay(
    tmp_path: Path,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == f"generation:{GENERATION_ID}:staged_abandoned_durable":
            armed = False
            raise InjectedFault(event)

    harness, store, _pointers = _runtime(tmp_path, fault_hook=fail)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    drifted = _observations(harness, "d" * 64)
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
        ttl_ns=1_000_000,
    )
    armed = True

    with pytest.raises((CommitUnknown, InjectedFault), match="staged_abandoned_durable"):
        store.abandon_staged_build(
            attempt,
            source_observations=drifted,
            monotonic_ns=20_001,
        )

    fresh_store = _reopen_store(harness)
    restored = trust_source_observations(fresh_store, observations)
    replay = fresh_store.request_staged_build(
        REPO_UUID,
        GENERATION_ID,
        request,
        source_observations=observations,
    )
    assert replay.lifecycle_state == "ABANDONED"
    assert restored.calls == 0
    assert replay.abandon_evidence is not None
    assert replay.abandon_evidence.source_inventory_sha256 == "d" * 64
    assert replay.abandon_evidence.reason_for(request) == "SOURCE_CHANGED"
    assert replay.abandon_evidence_sha256 == replay.abandon_evidence.sha256


def test_abandoned_evidence_rejects_digest_and_reason_rewrites(tmp_path: Path) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    attempt, abandoned = _abandon(
        store,
        harness,
        request,
        _observations(harness, "d" * 64),
    )
    harness.leases.release(attempt.grant)

    digest_rewrite = abandoned.to_dict()
    digest_evidence = cast(dict[str, Any], digest_rewrite["abandon_evidence"])
    digest_evidence["registry_revision"] = int(digest_evidence["registry_revision"]) + 1
    with pytest.raises(ContractError, match="must match canonical abandonment evidence"):
        StagedBuildState.from_mapping(digest_rewrite)

    reason_rewrite = abandoned.to_dict()
    reason_evidence = cast(dict[str, Any], reason_rewrite["abandon_evidence"])
    source = cast(dict[str, Any], reason_evidence["source"])
    observation = cast(dict[str, Any], source["observation"])
    observation["inventory_sha256"] = request.observation_manifest_sha256
    source["observation_evidence_sha256"] = request.observation_evidence_sha256
    rewritten = StagedBuildAbandonmentEvidence.from_mapping(reason_evidence)
    reason_rewrite["abandon_evidence_sha256"] = rewritten.sha256
    with pytest.raises(ContractError, match="does not prove stale staged-build authority"):
        StagedBuildState.from_mapping(reason_rewrite)


@pytest.mark.parametrize("fault_event", ["abandon_staging_removed", "abandon_capacity_cleared"])
def test_abandonment_crash_before_terminal_marker_keeps_prior_state_recoverable(
    tmp_path: Path, fault_event: str
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == f"generation:{GENERATION_ID}:{fault_event}":
            armed = False
            raise InjectedFault(event)

    harness, store, _pointers = _runtime(tmp_path, fault_hook=fail)
    observations = _observations(harness)
    request = _request(harness, observations)
    _publishing(store, harness, request, observations)
    drifted = _observations(harness, "d" * 64)
    armed = True
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
        ttl_ns=1_000_000,
    )

    with pytest.raises(InjectedFault, match=fault_event):
        store.abandon_staged_build(attempt, source_observations=drifted, monotonic_ns=20_001)
    assert store._load_staged_build_locked(REPO_UUID).lifecycle_state == "PUBLISHING"  # type: ignore[union-attr]
    harness.leases.release(attempt.grant)

    retry_attempt, recovered = _abandon(store, harness, request, drifted, tick=4)
    assert recovered.lifecycle_state == "ABANDONED"
    harness.leases.release(retry_attempt.grant)


@pytest.mark.parametrize("fault_event", ["abandon_staging_removed", "abandon_capacity_cleared"])
def test_durable_abandonment_intent_survives_complete_cleanup_crash_and_source_aba(
    tmp_path: Path,
    fault_event: str,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == f"generation:{GENERATION_ID}:{fault_event}":
            armed = False
            raise InjectedFault(event)

    harness, store, _pointers = _runtime(tmp_path, fault_hook=fail)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, _completion = _completed(store, harness, request, observations)
    harness.leases.release(build.grant)
    drifted = _observations(harness, "d" * 64)
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
        ttl_ns=1_000_000,
    )
    armed = True

    with pytest.raises(InjectedFault, match=fault_event):
        store.abandon_staged_build(
            attempt,
            source_observations=drifted,
            monotonic_ns=20_001,
        )
    interrupted = store._load_staged_build_locked(REPO_UUID)
    assert interrupted is not None
    assert interrupted.lifecycle_state == "COMPLETE"
    assert interrupted.abandonment_intent is not None
    assert interrupted.abandonment_intent.reason == "SOURCE_CHANGED"
    harness.leases.release(attempt.grant)

    fresh_store = _reopen_store(harness)
    restored = trust_source_observations(fresh_store, observations)
    with pytest.raises(LeaseRecoveryRequired, match="requires exact recovery"):
        fresh_store.acquire_staged_operation(
            REPO_UUID,
            GENERATION_ID,
            request,
            operation="BUILD",
            acquired_at=START + timedelta(seconds=3),
            monotonic_ns=30_000,
            ttl_ns=1_000_000,
        )
    recovery = fresh_store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=4),
        monotonic_ns=40_000,
        ttl_ns=1_000_000,
    )
    abandoned = fresh_store.abandon_staged_build(
        recovery,
        source_observations=observations,
        monotonic_ns=40_001,
    )

    assert restored.calls == 0
    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == "COMPLETE"
    assert abandoned.abandon_reason == "SOURCE_CHANGED"
    assert abandoned.abandonment_intent is None
    assert abandoned.abandon_evidence is not None
    assert abandoned.abandon_evidence.source_inventory_sha256 == "d" * 64
    fresh_store.leases.release(recovery.grant)

    successor = _request(harness, observations)
    trust_source_observations(fresh_store, observations)
    requested = fresh_store.request_staged_build(
        REPO_UUID,
        "gen-staged-after-aba",
        successor,
        source_observations=observations,
    )
    assert requested.lifecycle_state == "REQUESTED"


def test_staged_abandonment_refuses_intent_from_a_newer_fence(tmp_path: Path) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == f"generation:{GENERATION_ID}:abandon_intent_durable":
            armed = False
            raise InjectedFault(event)

    harness, store, _pointers = _runtime(tmp_path, fault_hook=fail)
    observations = _observations(harness)
    request = _request(harness, observations)
    _requested(store, harness, request, observations)
    drifted = _observations(harness, "d" * 64)
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000,
        ttl_ns=1_000_000,
    )
    armed = True
    with pytest.raises(InjectedFault, match="abandon_intent_durable"):
        store.abandon_staged_build(
            attempt,
            source_observations=drifted,
            monotonic_ns=20_001,
        )
    interrupted = store._load_staged_build_locked(REPO_UUID)
    assert interrupted is not None
    assert interrupted.abandonment_intent is not None
    with pytest.raises(LeaseRecoveryRequired, match="requires exact recovery"):
        store.allocate(
            attempt.grant,
            expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=POLICY,
            generation_id=GENERATION_ID,
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=20_002,
        )
    mismatched = interrupted.to_dict()
    mismatched_intent = cast(dict[str, Any], mismatched["abandonment_intent"])
    mismatched_intent["generation_id"] = "gen-mismatched-intent"
    with pytest.raises(ContractError, match="immediately preceding staged state"):
        StagedBuildState.from_mapping(mismatched)

    tampered = interrupted.to_dict()
    tampered_intent = cast(dict[str, Any], tampered["abandonment_intent"])
    tampered_evidence = cast(dict[str, Any], tampered_intent["evidence"])
    tampered_evidence["registry_revision"] = int(
        tampered_evidence["registry_revision"]
    ) + 1
    with pytest.raises(ContractError, match="must match canonical evidence"):
        StagedBuildState.from_mapping(tampered)

    forged_intent = replace(
        interrupted.abandonment_intent,
        fence_token=interrupted.abandonment_intent.fence_token + 10_000,
    )
    forged = StagedBuildState.from_mapping(
        {
            **interrupted.to_dict(),
            "abandonment_intent": forged_intent.to_dict(),
        }
    )
    staged_record = (
        harness.state_root / "workspaces" / REPO_UUID / "staged-build.json"
    )
    staged_record.write_bytes(forged.canonical)
    harness.leases.release(attempt.grant)

    fresh_store = _reopen_store(harness)
    recovery = fresh_store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )
    restored = trust_source_observations(fresh_store, observations)
    with pytest.raises(GenerationConflict, match="belongs to a newer fence"):
        fresh_store.abandon_staged_build(
            recovery,
            source_observations=observations,
            monotonic_ns=30_001,
        )
    assert restored.calls == 0
    retained = fresh_store._load_staged_build_locked(REPO_UUID)
    assert retained is not None
    assert retained.lifecycle_state == "REQUESTED"
    assert retained.abandonment_intent == forged_intent


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_abandonment_refuses_unsafe_staging_cleanup_without_terminalizing(
    tmp_path: Path,
    attack: str,
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, preparation = _publishing(store, harness, request, observations)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    attack_path = preparation.staging_path / "graphify-out"
    attack_path.mkdir()
    if attack == "symlink":
        (attack_path / "link").symlink_to(outside)
    else:
        os.link(outside, attack_path / "hardlink")
    harness.leases.release(build.grant)
    drifted = _observations(harness, "d" * 64)
    trust_source_observations(store, drifted)
    abandon = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    with pytest.raises(GenerationConflict, match="cannot be removed safely"):
        store.abandon_staged_build(
            abandon,
            source_observations=drifted,
            monotonic_ns=30_001,
        )

    state = store._load_staged_build_locked(REPO_UUID)
    assert state is not None
    assert state.lifecycle_state == "PUBLISHING"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_certified_abandonment_refuses_pending_pointer_intent(tmp_path: Path) -> None:
    harness, store, pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, _completion, receipt = _certified(store, harness, request, observations)
    harness.leases.release(build.grant)
    pending = pointers._pending(REPO_UUID)
    pointers.state.write_once(pending, b"{}")
    drifted = _observations(harness, "d" * 64)
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    with pytest.raises(GenerationConflict, match="pointer intent"):
        store.abandon_staged_build(attempt, source_observations=drifted, monotonic_ns=30_001)
    assert receipt.to_dict()["generation_id"] == GENERATION_ID
    assert store._load_staged_build_locked(REPO_UUID).lifecycle_state == "CERTIFIED"  # type: ignore[union-attr]


def test_active_source_drift_recovers_durable_certification_before_abandonment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store, _pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, completion = _completed(store, harness, request, observations)
    _bind_queue(store, build, completion, observations)

    original_marker = store._mark_staged_certified_locked
    armed = True

    def interrupt_staged_marker(*args: object, **kwargs: object):
        nonlocal armed
        if armed:
            armed = False
            raise InjectedFault("after_durable_staged_receipt")
        return original_marker(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_mark_staged_certified_locked", interrupt_staged_marker)
    trust_source_observations(store, observations)
    with pytest.raises(InjectedFault, match="after_durable_staged_receipt"):
        store.certify(
            build.grant,
            completion.allocation,
            _certification_request(observations),
            source_observations=observations,
            declared_entries=completion.entries,
            staged_completion=completion,
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_006,
        )
    assert store._load_staged_build_locked(REPO_UUID).lifecycle_state == "COMPLETE"  # type: ignore[union-attr]
    assert store.verify_generation(REPO_UUID, GENERATION_ID).sha256
    harness.leases.release(build.grant)
    _advance_active_source_revision(harness)
    trust_source_observations(store, observations)
    abandon = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    with pytest.raises(
        GenerationConflict,
        match="certification recovery is required before staged abandonment",
    ):
        store.abandon_staged_build(
            abandon,
            source_observations=observations,
            monotonic_ns=30_001,
        )
    recovered = store.recover_staged_certification(abandon, monotonic_ns=30_001)
    assert recovered.lifecycle_state == "CERTIFIED"
    harness.leases.release(abandon.grant)

    terminal_attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=4),
        monotonic_ns=40_000,
        ttl_ns=1_000_000,
    )
    terminal = store.abandon_staged_build(
        terminal_attempt,
        source_observations=observations,
        monotonic_ns=40_001,
    )
    assert terminal.lifecycle_state == "ABANDONED"
    assert terminal.abandoned_from == "CERTIFIED"
    assert terminal.abandon_reason == "ACTIVE_SOURCE_CHANGED"
    assert (
        harness.state_root / "workspaces" / REPO_UUID / "generations" / GENERATION_ID
    ).is_dir()


def test_active_source_drift_recovers_durable_staging_receipt(tmp_path: Path) -> None:
    harness, store, request, build, observations = _crash_with_durable_staging_receipt(
        tmp_path
    )
    staging = (
        harness.state_root / "workspaces" / REPO_UUID / "staging" / GENERATION_ID
    )
    final = (
        harness.state_root / "workspaces" / REPO_UUID / "generations" / GENERATION_ID
    )
    assert (staging / "receipt.json").is_file()
    assert not final.exists()
    harness.leases.release(build.grant)
    _advance_active_source_revision(harness)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    recovered = store.recover_staged_certification(attempt, monotonic_ns=30_001)

    assert recovered.lifecycle_state == "CERTIFIED"
    assert final.is_dir()
    assert not staging.exists()


def test_fresh_manifest_drift_recovers_durable_receipt_before_abandonment(
    tmp_path: Path,
) -> None:
    harness, _store, request, build, observations = _crash_with_durable_staging_receipt(
        tmp_path
    )
    harness.leases.release(build.grant)
    alternate_manifest = _alternate_manifest()
    fresh_store = _reopen_store(
        harness,
        compatibility_manifest=alternate_manifest,
    )
    recovery = fresh_store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    recovered = fresh_store.recover_staged_certification(
        recovery,
        monotonic_ns=30_001,
    )
    fresh_store.leases.release(recovery.grant)

    assert recovered.lifecycle_state == "CERTIFIED"
    terminal_attempt = fresh_store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=4),
        monotonic_ns=40_000,
        ttl_ns=1_000_000,
    )
    trust_source_observations(fresh_store, observations)
    terminal = fresh_store.abandon_staged_build(
        terminal_attempt,
        source_observations=observations,
        monotonic_ns=40_001,
    )

    assert terminal.lifecycle_state == "ABANDONED"
    assert terminal.abandoned_from == "CERTIFIED"
    assert terminal.abandon_reason == "COMPATIBILITY_CHANGED"


def test_stale_certification_recovery_requires_immutable_semantic_binding(
    tmp_path: Path,
) -> None:
    harness, store, request, build, observations = _crash_with_durable_staging_receipt(
        tmp_path
    )
    binding = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "queue"
        / "certifications"
        / f"{GENERATION_ID}.json"
    )
    assert binding.is_file()
    binding.unlink()
    harness.leases.release(build.grant)
    _advance_active_source_revision(harness)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=30_000,
        ttl_ns=1_000_000,
    )

    with pytest.raises(GenerationConflict, match="no semantic certification binding"):
        store.recover_staged_certification(attempt, monotonic_ns=30_001)

    state = store._load_staged_build_locked(REPO_UUID)
    assert state is not None
    assert state.lifecycle_state == "COMPLETE"
    assert (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staging"
        / GENERATION_ID
        / "receipt.json"
    ).is_file()


def test_certified_abandonment_refuses_pointer_already_visible(tmp_path: Path) -> None:
    harness, store, pointers = _runtime(tmp_path)
    observations = _observations(harness)
    request = _request(harness, observations)
    build, _completion, receipt = _certified(store, harness, request, observations)
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
        PointerCAS(
            expected_pointer_revision=0,
            expected_active_source_revision=promote.grant.active_source_revision,
            expected_source_epoch=1,
            expected_operation_epoch=promote.grant.operation_epoch,
            expected_migration_epoch=promote.grant.migration_epoch,
            expected_state_schema_version=1,
            expected_fence_token=int(promote.grant.lease.to_dict()["fence_token"]),
            candidate_generation_id=GENERATION_ID,
            candidate_receipt_sha256=receipt.sha256,
            expected_current_receipt_sha256=None,
        ),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=30_001,
    )
    harness.leases.release(promote.grant)
    drifted = _observations(harness, "d" * 64)
    trust_source_observations(store, drifted)
    attempt = store.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        request,
        acquired_at=START + timedelta(seconds=4),
        monotonic_ns=40_000,
        ttl_ns=1_000_000,
    )

    with pytest.raises(GenerationConflict, match="promotion"):
        store.abandon_staged_build(attempt, source_observations=drifted, monotonic_ns=40_001)
    assert pointer.to_dict()["current"]["generation_id"] == GENERATION_ID
    assert store._load_staged_build_locked(REPO_UUID).lifecycle_state == "CERTIFIED"  # type: ignore[union-attr]
