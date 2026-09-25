"""Disposable S3 lifecycle fixtures; S4 native adapter proof is separate."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import time

import pytest

from graphify.source_io import SourceIO
from graphify.workspace.adapters.base import SourceObservation as StructuralObservation
from graphify.workspace.contracts import InputManifest
from graphify.workspace.generations import (
    CapacityExceeded, CertificationRequest, GenerationError, GenerationStore,
    StructuralBuildRequest,
)
from graphify.workspace.gc import GcPlanStale, GcProtection, GcStore
from graphify.workspace.identity import IdentityAction, OperatorAuthorization, discover_source
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import StagedBuildLeaseRecoveryRequired, StaleLease
from graphify.workspace.lifecycle_contracts import (
    CapacityPolicy, ContractError, GenerationReceipt, payload_manifest_sha256,
)
from graphify.workspace.lifecycle_observation import SourceObservation
from graphify.workspace.persistence import InjectedFault, LockTimeout
from graphify.workspace.pointers import PointerCAS, PointerConflict, PointerStore
from graphify.workspace.registry import RevisionConflict, SourceAlreadyActive
from graphify.workspace.semantic_queue import (
    SemanticCertificationBlocked, SemanticQueueConflict, SemanticQueueError, SemanticQueuePolicy,
    SemanticQueueStore,
)
from tests.workspace_s3_helpers import (
    COMPATIBILITY_MANIFEST, REPO_UUID, START, create_harness, git_output,
    tree_snapshot, trust_source_observations,
)


POLICY = CapacityPolicy.from_mapping({
    "contract": "graphify.workspace.capacity_policy.internal", "format_version": 1,
    "global_max_bytes": 32 * 1024 * 1024, "global_max_generations": 16,
    "workspace_max_bytes": 8 * 1024 * 1024, "workspace_max_generations": 8,
    "reserve_bytes": 1024,
})
QUEUE_POLICY = SemanticQueuePolicy(
    max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=1,
)
GENERATION_ID = "gen-s3-fixture"


def _observations(repo: Path):
    code = repo / "main.py"
    with SourceIO(repo) as source_io:
        source_io.listdir(repo)
        source_io.probe(code)
        initial = InputManifest.from_engine(source_io, phase="detection", code_inputs=[code])
        source_io.read_bytes(code)
        consumed = InputManifest.from_engine(
            source_io, phase="consumed", code_inputs=[code],
            outcomes=[{"path": code, "status": "success"}],
        )
    structural = StructuralObservation(initial, consumed, 2)
    observation = SourceObservation(
        discover_source(repo).head_commit, "b" * 64, structural,
    )
    return observation, observation


def _runtime(tmp_path: Path, *, fault_hook=None):
    harness = create_harness(tmp_path)
    root, leases = harness.state_root, harness.leases
    capabilities = leases.state.capabilities
    journal = JournalStore(root, leases, capabilities=capabilities)
    queue = SemanticQueueStore(root, leases, policy=QUEUE_POLICY, capabilities=capabilities)
    generations = GenerationStore(
        root, leases, journal, compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue=queue, capabilities=capabilities, fault_hook=fault_hook,
    )
    pointers = PointerStore(
        root, leases, generations, journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST, capabilities=capabilities,
    )
    observations = _observations(harness.repo)
    trust_source_observations(generations, observations)
    return harness, generations, pointers, observations


def _request(harness, observations):
    registry = harness.registry.load().to_dict()
    entry = registry["workspaces"][0]
    lease_state = harness.leases.inspect(REPO_UUID)
    observation = GenerationStore._source_observation_document(observations[0])
    return StructuralBuildRequest.from_mapping({
        "logical_request_sha256": "a" * 64,
        "expected_registry_revision": registry["revision"],
        "expected_active_source_revision": entry["active_source_revision"],
        "expected_operation_epoch": lease_state.operation_epoch,
        "expected_migration_epoch": lease_state.migration_epoch,
        "expected_pointer_revision": 0,
        "expected_current_receipt_sha256": None,
        "source_commit": observations[0].source_commit,
        "source_epoch": 1,
        "policy_sha256": observations[0].policy_sha256,
        "observation_manifest_sha256": observations[0].inventory_sha256,
        "observation_evidence_sha256": GenerationStore.structural_observation_evidence_sha256(observations),
        "observation_detector_id": observation["detector_id"],
        "observation_entries_sha256": observation["entries_sha256"],
        "expected_payload_bytes": 16384,
        "capacity_policy_sha256": POLICY.sha256,
        "compatibility_sha256": COMPATIBILITY_MANIFEST.sha256,
    })


def _prepare(harness, generations, observations, *, staged_consumed=None):
    request = _request(harness, observations)
    generations.request_staged_build(REPO_UUID, GENERATION_ID, request,
                                     source_observations=observations)
    attempt = generations.acquire_staged_operation(
        REPO_UUID, GENERATION_ID, request, attempt_sha256="5" * 64,
        operation="BUILD", acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    allocation = generations.allocate(
        attempt.grant, expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=POLICY, generation_id=GENERATION_ID,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_001,
    )
    preparation = generations.prepare_staged_build(attempt, allocation, monotonic_ns=10_002)
    payload = preparation.staging_path / "graphify-out"
    payload.mkdir(mode=0o700)
    (payload / "graph.json").write_bytes(b"{}\n")
    consumed = staged_consumed or observations[0].consumed_inputs
    (payload / "input-manifest.json").write_bytes(consumed.canonical)
    return request, attempt, preparation


def _complete(harness, generations, observations):
    request, attempt, preparation = _prepare(harness, generations, observations)
    completion = generations.complete_staged_build(
        preparation, source_observations=observations, monotonic_ns=10_003,
    )
    return request, attempt, completion


@pytest.mark.parametrize("missing_consumed", [False, True])
def test_s3_completion_refuses_unobserved_consumed_inputs(tmp_path, missing_consumed):
    harness, generations, _pointers, observations = _runtime(tmp_path)
    if missing_consumed:
        structural = StructuralObservation(observations[0].initial_detection, None, 2)
        final = SourceObservation(
            observations[0].source_commit, observations[0].policy_sha256, structural,
        )
        trusted = (final, final)
        staged = observations[0].consumed_inputs
    else:
        trusted = observations
        changed = observations[0].consumed_inputs.to_dict()
        changed["outcomes"][0]["status"] = "empty"
        staged = InputManifest.from_mapping(changed)
    _request_value, _attempt, preparation = _prepare(
        harness, generations, observations, staged_consumed=staged,
    )
    trust_source_observations(generations, trusted)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationError, match="consumed inputs"):
        generations.complete_staged_build(
            preparation, source_observations=trusted, monotonic_ns=10_003,
        )
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("missing_consumed", [False, True])
def test_s3_certification_refuses_changed_trusted_consumed_inputs(tmp_path, missing_consumed):
    harness, generations, _pointers, observations = _runtime(tmp_path)
    _request_value, attempt, completion = _complete(harness, generations, observations)
    changed = observations[0].consumed_inputs.to_dict()
    changed["outcomes"][0]["status"] = "empty"
    consumed = InputManifest.from_mapping(changed)
    structural = StructuralObservation(
        observations[0].initial_detection, None if missing_consumed else consumed, 2,
    )
    final = SourceObservation(
        observations[0].source_commit, observations[0].policy_sha256, structural,
    )
    trusted = (final, final)
    trust_source_observations(generations, trusted)
    queue = generations.semantic_queue
    queue.reconcile(
        attempt.grant, (), source_epoch=1, policy_sha256=final.policy_sha256,
        source_observations=trusted, desired_watermark=1, semantic_required=False,
        monotonic_ns=10_004,
    )
    queue.bind_sealed_inputs(
        attempt.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
        monotonic_ns=10_005,
    )
    request = CertificationRequest(
        source_commit=final.source_commit, source_epoch=1,
        policy_sha256=final.policy_sha256,
        observation_manifest_sha256=final.inventory_sha256,
        queue_watermark=1, semantic_completeness="not_required",
        compatibility_sha256=COMPATIBILITY_MANIFEST.sha256,
        validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue"),
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(SemanticCertificationBlocked, match="consumed inputs"):
        generations.certify(
            attempt.grant, completion.allocation, request,
            source_observations=trusted, declared_entries=completion.entries,
            staged_completion=completion, occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_006,
        )
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("interrupt_promotion", [False, True])
def test_s3_stage_persists_exact_input_completion_and_queue_barrier(tmp_path, interrupt_promotion):
    harness, generations, pointers, observations = _runtime(tmp_path)
    request, attempt, completion = _complete(harness, generations, observations)
    binding = completion.state.completion_binding.to_dict()
    assert binding["initial_detection_sha256"] == observations[0].initial_detection.sha256
    assert binding["consumed_inputs_sha256"] == observations[0].consumed_inputs.sha256
    assert completion.state.request.sha256 == request.sha256
    assert generations._reuse_staged_completion_locked(completion.state, completion.allocation).state == completion.state
    queue = generations.semantic_queue
    assert queue is not None
    queue.reconcile(
        attempt.grant, (), source_epoch=1, policy_sha256=observations[0].policy_sha256,
        source_observations=observations, desired_watermark=1, semantic_required=False,
        monotonic_ns=10_004,
    )
    queue.bind_sealed_inputs(
        attempt.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
        monotonic_ns=10_005,
    )
    receipt = generations.certify(
        attempt.grant, completion.allocation,
        CertificationRequest(
            source_commit=observations[0].source_commit, source_epoch=1,
            policy_sha256=observations[0].policy_sha256,
            observation_manifest_sha256=observations[0].inventory_sha256,
            queue_watermark=1, semantic_completeness="not_required",
            compatibility_sha256=COMPATIBILITY_MANIFEST.sha256,
            validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue"),
        ),
        source_observations=observations, declared_entries=completion.entries,
        staged_completion=completion, occurred_at=START + timedelta(seconds=1),
        monotonic_ns=10_006,
    )
    assert receipt.to_dict()["semantic_completeness"] == "not_required"
    assert receipt.to_dict()["completion_binding"] == binding
    assert generations.verify_generation(REPO_UUID, GENERATION_ID) == receipt
    insufficient = receipt.to_dict()
    insufficient["validations"] = ["coordination_lock_precreated", "payload_manifest"]
    with pytest.raises(ContractError, match="structural certification proofs"):
        GenerationReceipt.from_mapping(insufficient)
    promotion = generations.acquire_staged_recovery(
        REPO_UUID, GENERATION_ID, request, attempt_sha256="7" * 64,
        acquired_at=START + timedelta(seconds=3), monotonic_ns=2_000_000,
        ttl_ns=1_000_000,
    )
    lease = promotion.grant.lease.to_dict()
    cas = PointerCAS(
        expected_pointer_revision=0,
        expected_active_source_revision=1,
        expected_source_epoch=1,
        expected_operation_epoch=promotion.grant.operation_epoch,
        expected_migration_epoch=promotion.grant.migration_epoch,
        expected_state_schema_version=2,
        expected_fence_token=lease["fence_token"],
        candidate_generation_id=GENERATION_ID,
        candidate_receipt_sha256=receipt.sha256,
        expected_current_receipt_sha256=None,
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(LockTimeout):
        pointers.promote(
            promotion.grant, cas,
            occurred_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
            deadline_ns=time.monotonic_ns() - 1,
        )
    assert tree_snapshot(harness.state_root) == before
    pointer = pointers.promote(
        promotion.grant, cas,
        occurred_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
        deadline_ns=time.monotonic_ns() + 5_000_000_000,
    )
    assert pointer.to_dict()["pointer_revision"] == 1
    if interrupt_promotion:
        def interrupt(label):
            if label.endswith(":staged_promoted_durable"):
                raise InjectedFault(label)

        generations.fault_hook = interrupt
        with pytest.raises(InjectedFault):
            generations.complete_staged_promotion(
                promotion, pointer, monotonic_ns=2_000_002,
            )
        generations.fault_hook = lambda _label: None
        counters = harness.leases.inspect(REPO_UUID)
        before = tree_snapshot(harness.state_root)
        with pytest.raises(StagedBuildLeaseRecoveryRequired, match="terminal cleanup"):
            harness.leases.acquire(
                REPO_UUID, "SEMANTIC_CLAIM", harness.leases.current_owner(),
                expected_registry_revision=1, expected_active_source_revision=1,
                expected_operation_epoch=counters.operation_epoch,
                expected_migration_epoch=counters.migration_epoch,
                acquired_at=START + timedelta(seconds=3), monotonic_ns=2_000_003,
                ttl_ns=1_000_000,
            )
        assert tree_snapshot(harness.state_root) == before
        assert harness.leases.inspect(REPO_UUID) == counters
    else:
        assert generations.complete_staged_promotion(
            promotion, pointer, monotonic_ns=2_000_002,
        ).lifecycle_state == "PROMOTED"
    terminal = generations.acquire_staged_recovery(
        REPO_UUID, GENERATION_ID, request, attempt_sha256="7" * 64,
        acquired_at=START + timedelta(seconds=3), monotonic_ns=2_000_003,
        ttl_ns=1_000_000,
    )
    assert terminal.state.lifecycle_state == "PROMOTED"
    assert generations.complete_staged_promotion(
        terminal, pointer, monotonic_ns=2_000_004,
    ).lifecycle_state == "PROMOTED"
    binding_path = harness.state_root / queue._certification_binding_path(REPO_UUID, GENERATION_ID)
    saved_binding = binding_path.read_bytes()
    binding_path.unlink()
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationError, match="certification binding"):
        generations.acquire_staged_recovery(
            REPO_UUID, GENERATION_ID, request, attempt_sha256="7" * 64,
            acquired_at=START + timedelta(seconds=3), monotonic_ns=2_000_004,
            ttl_ns=1_000_000,
        )
    assert tree_snapshot(harness.state_root) == before

    binding_path.write_bytes(saved_binding)
    binding_path.chmod(0o600)
    released = harness.leases.release(terminal.grant)
    assert released.staged_attempt_sha256 is None
    assert "workspace" not in released.leases

    registry = harness.registry.load().to_dict()
    rollback_grant = harness.leases.acquire(
        REPO_UUID, "ROLLBACK", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=released.operation_epoch,
        expected_migration_epoch=released.migration_epoch,
        acquired_at=START + timedelta(seconds=4),
        monotonic_ns=2_100_000, ttl_ns=1_000_000,
    )
    rollback_lease = rollback_grant.lease.to_dict()
    before = tree_snapshot(harness.state_root)
    with pytest.raises(PointerConflict, match="last_good"):
        pointers.rollback(
            rollback_grant,
            PointerCAS(
                expected_pointer_revision=pointer.to_dict()["pointer_revision"],
                expected_active_source_revision=1,
                expected_source_epoch=1,
                expected_operation_epoch=rollback_grant.operation_epoch,
                expected_migration_epoch=rollback_grant.migration_epoch,
                expected_state_schema_version=2,
                expected_fence_token=rollback_lease["fence_token"],
                candidate_generation_id=GENERATION_ID,
                candidate_receipt_sha256=receipt.sha256,
                expected_current_receipt_sha256=receipt.sha256,
            ),
            occurred_at=START + timedelta(seconds=4), monotonic_ns=2_100_001,
        )
    assert tree_snapshot(harness.state_root) == before


def test_source_activation_requires_adopted_linked_worktree_and_exact_cas(tmp_path):
    harness = create_harness(tmp_path)
    linked = tmp_path.resolve() / "linked"
    git_output(harness.repo, "worktree", "add", "--quiet", str(linked))
    source_b = discover_source(linked)
    auth = OperatorAuthorization(
        action=IdentityAction.ADOPT, operator_id="operator:s3-test", reason="linked fixture",
        issued_at="2026-07-16T19:00:00Z", nonce="adopt-linked",
    )
    adopted = harness.registry.adopt(source_b, auth, expected_revision=1)
    assert adopted.to_dict()["revision"] == 2
    lease_state = harness.leases.inspect(REPO_UUID)
    activate = OperatorAuthorization(
        action=IdentityAction.ACTIVATE, operator_id="operator:s3-test", reason="select linked fixture",
        issued_at="2026-07-16T19:00:00Z", nonce="activate-linked",
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(RevisionConflict):
        harness.registry.activate_source(
            source_b, activate, leases=harness.leases, owner=harness.leases.current_owner(),
            expected_registry_revision=1, expected_active_source_revision=1,
            expected_operation_epoch=lease_state.operation_epoch,
            expected_migration_epoch=lease_state.migration_epoch,
            acquired_at=START, monotonic_ns=10_000, ttl_ns=1_000_000,
        )
    assert tree_snapshot(harness.state_root) == before
    selected = harness.registry.activate_source(
        source_b, activate, leases=harness.leases, owner=harness.leases.current_owner(),
        expected_registry_revision=2, expected_active_source_revision=1,
        expected_operation_epoch=lease_state.operation_epoch,
        expected_migration_epoch=lease_state.migration_epoch,
        acquired_at=START, monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    assert selected.registry.to_dict()["workspaces"][0]["active_source"]["path"] == str(linked)
    assert harness.registry.resolve_active_source(REPO_UUID).root == linked
    current = selected.registry.to_dict()
    lease_state = harness.leases.inspect(REPO_UUID)
    with pytest.raises(SourceAlreadyActive):
        harness.registry.activate_source(
            source_b, activate, leases=harness.leases, owner=harness.leases.current_owner(),
            expected_registry_revision=current["revision"], expected_active_source_revision=2,
            expected_operation_epoch=lease_state.operation_epoch,
            expected_migration_epoch=lease_state.migration_epoch,
            acquired_at=START, monotonic_ns=11_000, ttl_ns=1_000_000,
        )


@pytest.mark.parametrize("fault_stage", [
    "receipt_durable", "installed", "staged_certified_durable",
])
def test_certification_recovers_exact_receipt_after_durable_fault(tmp_path, fault_stage):
    fired = False

    def fault(point):
        nonlocal fired
        if point == f"generation:{GENERATION_ID}:{fault_stage}" and not fired:
            fired = True
            raise InjectedFault(point)

    harness, generations, _pointers, observations = _runtime(tmp_path, fault_hook=fault)
    _request_value, attempt, completion = _complete(harness, generations, observations)
    queue = generations.semantic_queue
    assert queue is not None
    queue.reconcile(
        attempt.grant, (), source_epoch=1, policy_sha256=observations[0].policy_sha256,
        source_observations=observations, desired_watermark=1, semantic_required=False,
        monotonic_ns=10_004,
    )
    queue.bind_sealed_inputs(
        attempt.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
        monotonic_ns=10_005,
    )
    request = CertificationRequest(
        source_commit=observations[0].source_commit, source_epoch=1,
        policy_sha256=observations[0].policy_sha256,
        observation_manifest_sha256=observations[0].inventory_sha256,
        queue_watermark=1, semantic_completeness="not_required",
        compatibility_sha256=COMPATIBILITY_MANIFEST.sha256,
        validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue"),
    )
    kwargs = dict(
        source_observations=observations, declared_entries=completion.entries,
        staged_completion=completion, occurred_at=START + timedelta(seconds=1),
        monotonic_ns=10_006,
    )
    with pytest.raises(InjectedFault):
        generations.certify(attempt.grant, completion.allocation, request, **kwargs)
    assert fired
    if fault_stage == "staged_certified_durable":
        with pytest.raises(StagedBuildLeaseRecoveryRequired):
            generations.certify(attempt.grant, completion.allocation, request, **kwargs)
        receipt = generations.verify_generation(REPO_UUID, GENERATION_ID)
        resumed = generations.acquire_staged_recovery(
            REPO_UUID, GENERATION_ID, _request_value,
            attempt_sha256="9" * 64,
            acquired_at=START + timedelta(seconds=3), monotonic_ns=2_000_000,
            ttl_ns=1_000_000,
        )
        assert resumed.state.lifecycle_state == "CERTIFIED"
    else:
        receipt = generations.certify(attempt.grant, completion.allocation, request, **kwargs)
    assert generations.verify_generation(REPO_UUID, GENERATION_ID) == receipt
    events = generations.journal.read_stable(REPO_UUID).for_generation(GENERATION_ID)
    assert sum(event.to_dict()["transition"] == "CERTIFIED" for event in events) == 1


def test_successor_fence_rejects_old_attempt_without_writes(tmp_path):
    harness, generations, _pointers, observations = _runtime(tmp_path)
    request, attempt, _completion = _complete(harness, generations, observations)
    successor = generations.acquire_staged_recovery(
        REPO_UUID, GENERATION_ID, request, attempt_sha256="6" * 64,
        acquired_at=START + timedelta(seconds=3), monotonic_ns=2_000_000,
        ttl_ns=1_000_000,
    )
    assert (successor.grant.lease.to_dict()["fence_token"]
            > attempt.grant.lease.to_dict()["fence_token"])
    before = tree_snapshot(harness.state_root)
    with pytest.raises(StaleLease):
        with harness.leases.current_operation(attempt.grant, monotonic_ns=2_000_001):
            pass
    assert tree_snapshot(harness.state_root) == before


def test_empty_queue_without_sealed_binding_cannot_certify(tmp_path):
    harness, generations, _pointers, observations = _runtime(tmp_path)
    _request_value, attempt, completion = _complete(harness, generations, observations)
    queue = generations.semantic_queue
    assert queue is not None
    queue.reconcile(
        attempt.grant, (), source_epoch=1, policy_sha256=observations[0].policy_sha256,
        source_observations=observations, desired_watermark=2, semantic_required=False,
        monotonic_ns=10_004,
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(SemanticQueueConflict):
        queue.reconcile(
            attempt.grant, (), source_epoch=1, policy_sha256=observations[0].policy_sha256,
            source_observations=observations, desired_watermark=1, semantic_required=False,
            monotonic_ns=10_005,
        )
    assert tree_snapshot(harness.state_root) == before
    with pytest.raises(SemanticCertificationBlocked):
        generations.certify(
            attempt.grant, completion.allocation,
            CertificationRequest(
                source_commit=observations[0].source_commit, source_epoch=1,
                policy_sha256=observations[0].policy_sha256,
                observation_manifest_sha256=observations[0].inventory_sha256,
                queue_watermark=2, semantic_completeness="not_required",
                compatibility_sha256=COMPATIBILITY_MANIFEST.sha256,
                validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue"),
            ),
            source_observations=observations, declared_entries=completion.entries,
            staged_completion=completion, occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_005,
        )
    assert tree_snapshot(harness.state_root) == before


def test_explicit_capacity_limit_rejects_reservation(tmp_path):
    harness, generations, _pointers, observations = _runtime(tmp_path)
    limited = CapacityPolicy.from_mapping({
        "contract": "graphify.workspace.capacity_policy.internal", "format_version": 1,
        "global_max_bytes": 1024, "global_max_generations": 1,
        "workspace_max_bytes": 1024, "workspace_max_generations": 1,
        "reserve_bytes": 1024,
    })
    requested = _request(harness, observations).to_dict()
    requested["capacity_policy_sha256"] = limited.sha256
    request = StructuralBuildRequest.from_mapping(requested)
    generations.request_staged_build(
        REPO_UUID, GENERATION_ID, request, source_observations=observations,
    )
    attempt = generations.acquire_staged_operation(
        REPO_UUID, GENERATION_ID, request, attempt_sha256="8" * 64,
        operation="BUILD", acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(CapacityExceeded):
        generations.allocate(
            attempt.grant, expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=limited, generation_id=GENERATION_ID,
            occurred_at=START + timedelta(seconds=1), monotonic_ns=10_001,
        )
    assert tree_snapshot(harness.state_root) == before


def test_operator_repair_requires_approved_plan_before_execution(tmp_path):
    harness, _generations, pointers, _observations = _runtime(tmp_path)
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "REPAIR", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(PointerConflict, match="approved repair plan"):
        pointers.recover(
            grant, occurred_at=START + timedelta(seconds=1), monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_gc_preview_is_read_only_and_does_not_adopt_unowned_generations(tmp_path):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    empty = frozenset()
    before = tree_snapshot(harness.state_root)
    preview = gc.preview(
        REPO_UUID, expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        expected_pointer_revision=0, capacity_policy=POLICY,
        protections=GcProtection(empty, empty, empty, empty, empty, empty),
        deadline_ns=time.monotonic_ns() + 5_000_000_000,
    )
    assert preview.candidates == ()
    assert tree_snapshot(harness.state_root) == before


def test_gc_reused_epoch_rejects_changed_plan_before_durable_intent(tmp_path):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        first_plan = gc._plan_locked(
            operation, capacity_policy=POLICY, protections=protections,
            probe_locks=True,
        )
    assert first_plan.candidates == ()
    gc.execute(
        grant, first_plan, capacity_policy=POLICY, protections=protections,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
    )

    changed_policy = POLICY.to_dict()
    changed_policy["reserve_bytes"] += 1
    second_policy = CapacityPolicy.from_mapping(changed_policy)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_003, allowed_operations=frozenset({"GC"}),
    ) as operation:
        second_plan = gc._plan_locked(
            operation, capacity_policy=second_policy, protections=protections,
            probe_locks=True,
        )
    assert second_plan.sha256 != first_plan.sha256
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GcPlanStale, match="already completed another plan"):
        gc.execute(
            grant, second_plan, capacity_policy=second_policy,
            protections=protections,
            occurred_at=START + timedelta(seconds=2), monotonic_ns=10_004,
        )
    assert tree_snapshot(harness.state_root) == before
    assert not gc._intent_path(REPO_UUID).exists()


@pytest.mark.parametrize("write_label", [
    "gc:intent", "gc:completion", "gc:completion_epoch",
])
def test_gc_deadline_reaches_each_durable_install(tmp_path, monkeypatch, write_label):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        plan = gc._plan_locked(
            operation, capacity_policy=POLICY, protections=protections,
            probe_locks=True,
        )
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    original_install = gc.state.install_once_bytes

    def expire_at_install(relative, data, *, label, **kwargs):
        if label == write_label:
            with monkeypatch.context() as clock:
                clock.setattr(
                    "graphify.workspace.persistence.time.monotonic_ns",
                    lambda: deadline_ns,
                )
                return original_install(relative, data, label=label, **kwargs)
        return original_install(relative, data, label=label, **kwargs)

    monkeypatch.setattr(gc.state, "install_once_bytes", expire_at_install)
    with pytest.raises(LockTimeout):
        gc.execute(
            grant, plan, capacity_policy=POLICY, protections=protections,
            occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
            deadline_ns=deadline_ns,
        )

    intent_path = gc.state.path(gc._intent_path(REPO_UUID))
    completion_path = gc.state.path(gc._completion_path(REPO_UUID, plan.sha256))
    index_path = gc.state.path(
        gc._operation_completion_path(REPO_UUID, grant.operation_epoch)
    )
    assert intent_path.exists() == (write_label != "gc:intent")
    assert completion_path.exists() == (write_label == "gc:completion_epoch")
    assert not index_path.exists()


def test_gc_deadline_reaches_candidate_quarantine_rename(tmp_path, monkeypatch):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        plan = gc._plan_locked(
            operation, capacity_policy=POLICY, protections=protections,
            probe_locks=True,
        )
        intent = gc._intent(
            operation, replace(plan, candidates=("gen-orphan",)), occurred_at=START,
        )
    source = gc.state.ensure_directory(generations._generation(REPO_UUID, "gen-orphan"))
    destination = gc.state.path(gc._quarantine(REPO_UUID, "gen-orphan", grant.operation_epoch))
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    original_rename = gc.state.rename_contained

    def expire_at_rename(source_relative, destination_relative, **kwargs):
        with monkeypatch.context() as clock:
            clock.setattr(
                "graphify.workspace.persistence.time.monotonic_ns",
                lambda: deadline_ns,
            )
            return original_rename(source_relative, destination_relative, **kwargs)

    monkeypatch.setattr(gc.state, "rename_contained", expire_at_rename)
    with pytest.raises(LockTimeout):
        gc._rename_candidates(intent, deadline_ns=deadline_ns)

    assert source.is_dir()
    assert not destination.exists()


def test_gc_deadline_reaches_purge_record_install(tmp_path, monkeypatch):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        plan = gc._plan_locked(
            operation, capacity_policy=POLICY, protections=protections,
            probe_locks=True,
        )
    gc.execute(
        grant, plan, capacity_policy=POLICY, protections=protections,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
    )
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    original_install = gc.state.install_once_bytes

    def expire_at_purge(relative, data, *, label, **kwargs):
        if label == "gc:purge":
            with monkeypatch.context() as clock:
                clock.setattr(
                    "graphify.workspace.persistence.time.monotonic_ns",
                    lambda: deadline_ns,
                )
                return original_install(relative, data, label=label, **kwargs)
        return original_install(relative, data, label=label, **kwargs)

    monkeypatch.setattr(gc.state, "install_once_bytes", expire_at_purge)
    with pytest.raises(LockTimeout):
        gc.purge(
            grant, plan_sha256=plan.sha256, capacity_policy=POLICY,
            protections=protections, completed_at=START + timedelta(seconds=2),
            monotonic_ns=10_003, deadline_ns=deadline_ns,
        )

    assert not gc.state.path(gc._purge_path(REPO_UUID, plan.sha256)).exists()


@pytest.mark.parametrize("site", ["completion", "purge_state", "purge_completion"])
def test_gc_completion_read_timeouts_remain_retryable(tmp_path, monkeypatch, site):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    empty = frozenset()
    protections = GcProtection(empty, empty, empty, empty, empty, empty)
    with harness.leases.current_operation(
        grant, monotonic_ns=10_001, allowed_operations=frozenset({"GC"}),
    ) as operation:
        plan = gc._plan_locked(
            operation, capacity_policy=POLICY, protections=protections,
            probe_locks=True,
        )
        intent = gc._intent(operation, plan, occurred_at=START + timedelta(seconds=1))
    gc.execute(
        grant, plan, capacity_policy=POLICY, protections=protections,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
    )
    before = tree_snapshot(harness.state_root)
    target = (
        gc._purge_path(REPO_UUID, plan.sha256)
        if site == "purge_state"
        else gc._completion_path(REPO_UUID, plan.sha256)
    )
    method = "read_existing_bytes" if site == "purge_completion" else "read_optional_existing_bytes"
    original = getattr(gc.state, method)

    def timeout_on_target(relative, *args, **kwargs):
        if relative == target:
            if site == "purge_completion":
                assert kwargs["max_bytes"] == 1024 * 1024
            raise LockTimeout("injected GC read deadline")
        return original(relative, *args, **kwargs)

    monkeypatch.setattr(gc.state, method, timeout_on_target)
    with pytest.raises(LockTimeout, match="injected GC read deadline"):
        if site == "completion":
            gc._read_completion(intent)
        else:
            gc.purge(
                grant, plan_sha256=plan.sha256, capacity_policy=POLICY,
                protections=protections, completed_at=START + timedelta(seconds=2),
                monotonic_ns=10_003,
            )
    assert tree_snapshot(harness.state_root) == before


def test_semantic_queue_policy_has_explicit_bounds():
    policy = SemanticQueuePolicy(max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=1)
    assert SemanticQueuePolicy.from_mapping(policy.to_dict()) == policy
    with pytest.raises(SemanticQueueError):
        SemanticQueuePolicy(max_items=1, max_bytes=16, retry_budget=0, max_claimed_tasks=2)


def test_semantic_queue_constructor_does_not_create_state(tmp_path):
    harness = create_harness(tmp_path)
    before = tree_snapshot(harness.state_root)
    queue = SemanticQueueStore(
        harness.state_root, harness.leases,
        policy=SemanticQueuePolicy(max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=1),
        capabilities=harness.leases.state.capabilities,
    )
    assert queue.state.root == harness.state_root
    assert tree_snapshot(harness.state_root) == before
