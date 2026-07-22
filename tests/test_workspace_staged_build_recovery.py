"""P5B2b0 staged structural-build recovery contract.

These tests intentionally stop at ``COMPLETE``. Certification and pointer
promotion remain owned by the existing generation and pointer suites.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path

import pytest

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import CapacityPolicy, ContractError
from graphify.workspace.generations import (
    CapacityExceeded,
    GenerationConflict,
    GenerationError,
    GenerationStore,
    StagedBuildCompletion,
    StagedBuildOperation,
    StagedBuildPreparation,
    StagedBuildState,
    StructuralBuildRequest,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseBusy, LeaseRecoveryRequired
from graphify.workspace.persistence import CommitUnknown, FaultHook, InjectedFault

from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    RuntimeHarness,
    START,
    acquire,
    authorization,
    create_harness,
    create_repo,
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
ATTEMPT_SHA256 = "7" * 64


def _store(harness: RuntimeHarness, *, fault_hook: FaultHook | None = None) -> GenerationStore:
    return GenerationStore(
        harness.state_root,
        harness.leases,
        JournalStore(
            harness.state_root,
            harness.leases,
            capabilities=harness.leases.state.capabilities,
        ),
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
    )


def _source_observations(harness: RuntimeHarness) -> tuple[SourceObservation, SourceObservation]:
    observation = SourceObservation(
        source_commit=discover_source(harness.repo).head_commit,
        inventory_sha256="c" * 64,
        policy_sha256="b" * 64,
        detector_id="test-workspace-staged-build-recovery",
        stable_inventory_passes=2,
        entries=(),
    )
    return observation, observation


def _request(
    harness: RuntimeHarness,
    observations: tuple[SourceObservation, SourceObservation],
    **changes: object,
) -> StructuralBuildRequest:
    registry = harness.registry.load().to_dict()
    entry = registry["workspaces"][0]
    lease_state = harness.leases.inspect(REPO_UUID)
    observation = GenerationStore._source_observation_document(observations[0])
    values: dict[str, object] = {
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
        "observation_detector_id": observation["detector_id"],
        "observation_entries_sha256": observation["entries_sha256"],
        "expected_payload_bytes": 1024,
        "capacity_policy_sha256": POLICY.sha256,
        "compatibility_sha256": COMPATIBILITY_MANIFEST.sha256,
    }
    values.update(changes)
    return StructuralBuildRequest.from_mapping(values)


def _request_staged_build(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> StagedBuildState:
    trust_source_observations(store, observations)
    return store.request_staged_build(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        source_observations=observations,
    )


def _request_and_acquire(
    store: GenerationStore,
    harness: RuntimeHarness,
    request: StructuralBuildRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> tuple[StagedBuildOperation, StagedBuildPreparation]:
    requested = _request_staged_build(store, harness, request, observations)
    assert requested.lifecycle_state == "REQUESTED"
    attempt = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256="1" * 64,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        attempt.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id="gen-staged-recovery",
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=10_001,
    )
    return attempt, store.prepare_staged_build(attempt, allocation, monotonic_ns=10_002)


def test_staged_lease_commit_unknown_reuses_only_the_exact_attempt(
    tmp_path: Path,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "workspace:pending_durable":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path, fault_hook=fail)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    armed = True

    with pytest.raises(CommitUnknown):
        store.acquire_staged_operation(
            REPO_UUID,
            "gen-staged-recovery",
            request,
            attempt_sha256="2" * 64,
            operation="BUILD",
            acquired_at=START + timedelta(seconds=1),
            monotonic_ns=10_000,
            ttl_ns=1_000_000,
        )

    with pytest.raises(LeaseBusy):
        store.acquire_staged_operation(
            REPO_UUID,
            "gen-staged-recovery",
            request,
            attempt_sha256="3" * 64,
            operation="BUILD",
            acquired_at=START + timedelta(seconds=1),
            monotonic_ns=10_001,
            ttl_ns=1_000_000,
        )

    recovered = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256="2" * 64,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_002,
        ttl_ns=1_000_000,
    )

    assert recovered.grant.lease.to_dict()["fence_token"] == 2
    assert harness.leases.inspect(REPO_UUID).staged_attempt_sha256 == "2" * 64
    renewed = harness.leases.heartbeat(
        recovered.grant,
        heartbeat_at=START + timedelta(seconds=1),
        monotonic_ns=10_003,
        ttl_ns=1_000_000,
    )
    assert harness.leases.inspect(REPO_UUID).staged_attempt_sha256 == "2" * 64
    harness.leases.release(renewed)
    assert harness.leases.inspect(REPO_UUID).staged_attempt_sha256 is None


def _write_payload(preparation: StagedBuildPreparation, content: str = "{}") -> None:
    payload = preparation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text(content, encoding="utf-8")


def test_requested_build_is_durable_idempotent_and_blocks_generic_build_acquisition(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)

    first = _request_staged_build(store, harness, request, observations)
    before_retry = tree_snapshot(harness.state_root)
    retry = _request_staged_build(store, harness, request, observations)

    assert first == retry
    assert retry.lifecycle_state == "REQUESTED"
    assert tree_snapshot(harness.state_root) == before_retry
    with pytest.raises(LeaseRecoveryRequired, match="staged build"):
        acquire(harness, "BUILD", tick=1)


def test_requested_build_mismatch_does_not_mutate_durable_state(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="staged build request"):
        _request_staged_build(
            store,
            harness,
            replace(request, logical_request_sha256="d" * 64),
            observations,
        )

    assert tree_snapshot(harness.state_root) == before


def test_requested_build_rejects_untrusted_source_evidence_without_mutation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    forged_observation = replace(observations[0], inventory_sha256="d" * 64)
    trust_source_observations(store, (forged_observation, forged_observation))
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="caller evidence"):
        store.request_staged_build(
            REPO_UUID,
            "gen-staged-recovery",
            request,
            source_observations=observations,
        )

    assert tree_snapshot(harness.state_root) == before


def test_requested_build_rejects_stale_pointer_cas_without_mutation(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(
        harness,
        observations,
        expected_pointer_revision=1,
        expected_current_receipt_sha256="d" * 64,
    )
    trust_source_observations(store, observations)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="pointer CAS"):
        store.request_staged_build(
            REPO_UUID,
            "gen-staged-recovery",
            request,
            source_observations=observations,
        )

    assert tree_snapshot(harness.state_root) == before


def test_bound_build_acquisition_retries_exactly_after_committed_lease_expires(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)

    first = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000,
        ttl_ns=1,
    )
    recovered = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=10_001,
        ttl_ns=1_000_000,
    )

    assert first.state.lifecycle_state == "REQUESTED"
    assert recovered.state.lifecycle_state == "REQUESTED"
    assert recovered.grant.operation_epoch > first.grant.operation_epoch
    assert recovered.grant.lease.to_dict()["fence_token"] > first.grant.lease.to_dict()["fence_token"]


def test_bound_recovery_tolerates_unrelated_global_registry_revision(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    other = create_repo(
        tmp_path / "other-repo",
        repo_uuid="22222222-2222-4222-8222-222222222222",
    )
    harness.registry.enroll(
        discover_source(other),
        authorization("enroll-other"),
        expected_revision=request.expected_registry_revision,
    )

    attempt = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000,
        ttl_ns=1_000_000,
    )

    assert attempt.grant.registry_revision > request.expected_registry_revision
    assert attempt.state.lifecycle_state == "REQUESTED"


def test_empty_publishing_is_idempotent_but_nonempty_same_fence_is_refused(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    attempt, first = _request_and_acquire(store, harness, request, observations)

    retry = store.prepare_staged_build(attempt, first.allocation, monotonic_ns=10_003)

    assert first == retry
    assert first.state.lifecycle_state == "PUBLISHING"
    _write_payload(first)
    with pytest.raises(GenerationConflict, match="nonempty.*same fence"):
        store.prepare_staged_build(attempt, first.allocation, monotonic_ns=10_004)


def test_successor_fence_resets_interrupted_partial_stage(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    first, publishing = _request_and_acquire(store, harness, request, observations)
    _write_payload(publishing, "partial")

    successor = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        successor.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id="gen-staged-recovery",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_001,
    )
    reset = store.prepare_staged_build(successor, allocation, monotonic_ns=2_000_002)

    assert successor.grant.lease.to_dict()["fence_token"] > first.grant.lease.to_dict()["fence_token"]
    assert reset.state.lifecycle_state == "PUBLISHING"
    assert not (reset.staging_path / "graphify-out").exists()


def test_successor_reset_rejects_publishing_state_without_fence_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _first, publishing = _request_and_acquire(store, harness, request, observations)
    successor = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        successor.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id="gen-staged-recovery",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_001,
    )
    invalid = replace(
        publishing.state,
        operation_epoch=None,
        fence_token=None,
    )
    monkeypatch.setattr(store, "_load_staged_build_locked", lambda _repo_uuid: invalid)

    with pytest.raises(GenerationConflict, match="missing durable fence authority"):
        store.prepare_staged_build(successor, allocation, monotonic_ns=2_000_002)


def test_complete_binds_manifest_and_exact_retry_reuses_completion(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _, publishing = _request_and_acquire(store, harness, request, observations)
    _write_payload(publishing)

    trust_source_observations(store, observations)
    completed = store.complete_staged_build(
        publishing,
        source_observations=observations,
        monotonic_ns=10_003,
    )
    trust_source_observations(store, observations)
    retry = store.complete_staged_build(
        publishing,
        source_observations=observations,
        monotonic_ns=10_004,
    )

    assert isinstance(completed, StagedBuildCompletion)
    assert completed == retry
    assert completed.state.lifecycle_state == "COMPLETE"
    assert completed.manifest_sha256


def test_completed_staged_build_rejects_missing_durable_payload_manifest(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _, publishing = _request_and_acquire(store, harness, request, observations)
    _write_payload(publishing)
    trust_source_observations(store, observations)
    completed = store.complete_staged_build(
        publishing,
        source_observations=observations,
        monotonic_ns=10_003,
    )
    invalid = replace(
        completed,
        state=replace(completed.state, payload_manifest_sha256=None),
    )

    with pytest.raises(GenerationConflict, match="missing a durable payload manifest"):
        _ = invalid.manifest_sha256


def test_corrupt_staged_build_record_fails_closed_without_replacement(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    record = harness.state_root / "workspaces" / REPO_UUID / "staged-build.json"
    record.write_bytes(b"{not json")
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationError, match="staged build.*corrupt"):
        _request_staged_build(store, harness, request, observations)

    assert tree_snapshot(harness.state_root) == before


def test_oversize_staged_build_record_fails_closed_without_replacement(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    record = harness.state_root / "workspaces" / REPO_UUID / "staged-build.json"
    record.write_bytes(b"{" + b" " * (64 * 1024) + b"}")
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationError, match="staged build.*corrupt"):
        _request_staged_build(store, harness, request, observations)

    assert tree_snapshot(harness.state_root) == before


def test_structural_request_requires_exact_pointer_receipt_binding(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    invalid = request.to_dict()
    invalid["expected_current_receipt_sha256"] = "d" * 64

    with pytest.raises(ContractError, match="null exactly when"):
        StructuralBuildRequest.from_mapping(invalid)


def test_staged_build_state_roundtrips_canonical_internal_record(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    state = StagedBuildState.from_mapping(
        {
            "contract": "graphify.workspace.staged_build.internal",
            "format_version": 1,
            "revision": 3,
            "repo_uuid": REPO_UUID,
            "generation_id": "gen-staged-recovery",
            "request": request.to_dict(),
            "request_sha256": request.sha256,
            "lifecycle_state": "COMPLETE",
            "operation_epoch": request.expected_operation_epoch + 1,
            "fence_token": 2,
            "payload_manifest_sha256": "e" * 64,
            "receipt_sha256": None,
            "pointer_revision": None,
            "abandonment_intent": None,
            "abandoned_from": None,
            "abandon_reason": None,
            "abandon_evidence": None,
            "abandon_evidence_sha256": None,
        }
    )

    assert StagedBuildState.from_json(state.canonical) == state
    assert len(state.canonical) < 64 * 1024


def test_staged_allocation_authority_fails_before_capacity_mutation(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    attempt = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000,
        ttl_ns=1_000_000,
    )
    alternate = CapacityPolicy.from_mapping(
        {**POLICY.to_dict(), "reserve_bytes": POLICY.reserve_bytes + 1}
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="allocation request"):
        store.allocate(
            attempt.grant,
            expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=alternate,
            generation_id="gen-staged-recovery",
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_read_only_operation_does_not_recover_staged_pending_record(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _request_staged_build(store, harness, request, observations)
    attempt = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000,
        ttl_ns=1_000_000,
    )
    directory = harness.state_root / "workspaces" / REPO_UUID
    current = directory / "staged-build.json"
    pending = directory / "staged-build.pending.json"
    pending.write_bytes(current.read_bytes())
    pending.chmod(0o600)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(LeaseRecoveryRequired, match="staged build"):
        with harness.leases.current_operation_read_only(
            attempt.grant,
            monotonic_ns=10_001,
            allowed_operations=frozenset({"BUILD"}),
        ):
            pass

    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_successor_reset_rejects_link_attacks_without_advancing_fence(
    tmp_path: Path,
    attack: str,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _first, publishing = _request_and_acquire(store, harness, request, observations)
    payload = publishing.staging_path / "graphify-out"
    payload.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    candidate = payload / "attack"
    if attack == "symlink":
        candidate.symlink_to(outside)
    else:
        os.link(outside, candidate)
    successor = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        successor.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id="gen-staged-recovery",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_001,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="cannot be reset safely"):
        store.prepare_staged_build(successor, allocation, monotonic_ns=2_000_002)

    durable = store._load_staged_build_locked(REPO_UUID)
    assert durable is not None
    assert durable.fence_token == publishing.state.fence_token
    assert outside.read_text(encoding="utf-8") == "outside"
    assert tree_snapshot(harness.state_root) == before


def test_requested_record_recovers_unknown_commit_without_duplicate_revision(
    tmp_path: Path,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == f"staged-build:{REPO_UUID}:pending_durable":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    store = _store(harness, fault_hook=fail)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    trust_source_observations(store, observations)
    armed = True

    with pytest.raises(CommitUnknown):
        store.request_staged_build(
            REPO_UUID,
            "gen-staged-recovery",
            request,
            source_observations=observations,
        )

    trust_source_observations(store, observations)
    recovered = store.request_staged_build(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        source_observations=observations,
    )
    pending = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staged-build.pending.json"
    )
    assert recovered.lifecycle_state == "REQUESTED"
    assert recovered.revision == 1
    assert not pending.exists()


def test_successor_reset_retries_after_empty_tree_precedes_fence_commit(
    tmp_path: Path,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "generation:gen-staged-recovery:successor_staging_empty":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    store = _store(harness, fault_hook=fail)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _first, publishing = _request_and_acquire(store, harness, request, observations)
    _write_payload(publishing, "partial")
    successor = store.acquire_staged_operation(
        REPO_UUID,
        "gen-staged-recovery",
        request,
        attempt_sha256=ATTEMPT_SHA256,
        operation="BUILD",
        acquired_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_000,
        ttl_ns=1_000_000,
    )
    allocation = store.allocate(
        successor.grant,
        expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY,
        generation_id="gen-staged-recovery",
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=2_000_001,
    )
    armed = True

    with pytest.raises(InjectedFault):
        store.prepare_staged_build(successor, allocation, monotonic_ns=2_000_002)

    predecessor = store._load_staged_build_locked(REPO_UUID)
    assert predecessor is not None
    assert predecessor.fence_token == publishing.state.fence_token
    recovered = store.prepare_staged_build(
        successor,
        allocation,
        monotonic_ns=2_000_003,
    )
    assert recovered.state.fence_token == successor.grant.lease.to_dict()["fence_token"]
    assert not any(recovered.staging_path.iterdir())


def test_completion_retries_after_durable_payload_precedes_completion_record(
    tmp_path: Path,
) -> None:
    armed = False

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "generation:gen-staged-recovery:before_completion_reinventory":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    store = _store(harness, fault_hook=fail)
    observations = _source_observations(harness)
    request = _request(harness, observations)
    _attempt, publishing = _request_and_acquire(store, harness, request, observations)
    _write_payload(publishing)
    trust_source_observations(store, observations)
    armed = True

    with pytest.raises(InjectedFault):
        store.complete_staged_build(
            publishing,
            source_observations=observations,
            monotonic_ns=10_003,
        )

    durable = store._load_staged_build_locked(REPO_UUID)
    assert durable is not None
    assert durable.lifecycle_state == "PUBLISHING"
    trust_source_observations(store, observations)
    completed = store.complete_staged_build(
        publishing,
        source_observations=observations,
        monotonic_ns=10_004,
    )
    assert completed.state.lifecycle_state == "COMPLETE"
