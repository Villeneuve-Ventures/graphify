"""Disposable S3 lifecycle fixtures; S4 native adapter proof is separate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import time

import pytest

from graphify.source_io import SourceIO
from graphify.workspace.adapters.base import SourceObservation as StructuralObservation
from graphify.workspace.contracts import InputManifest
from graphify.workspace.generations import (
    CapacityExceeded, CertificationRequest, GenerationError, GenerationStore,
    StructuralBuildRequest,
)
from graphify.workspace.gc import GcPlanStale, GcProtection, GcRecoveryRequired, GcStore
from graphify.workspace.identity import IdentityAction, OperatorAuthorization, discover_source
from graphify.workspace.journal import JournalRecoveryProjection, JournalSnapshot, JournalStore
from graphify.workspace.leases import StagedBuildLeaseRecoveryRequired, StaleLease
from graphify.workspace.lifecycle_contracts import (
    CapacityPolicy, ContractError, GcCompletionState, GenerationReceipt, PointerSet, PriorPointerRecord,
    payload_manifest_sha256,
)
from graphify.workspace.lifecycle_observation import SourceObservation
from graphify.workspace.persistence import CommitUnknown, InjectedFault, LockTimeout
from graphify.workspace.pointers import (
    PointerCAS, PointerConflict, PointerCorrupt, PointerRepairPlan, PointerStore,
    PointerSuperseded,
)
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
    promoted_state = tree_snapshot(harness.state_root)
    assert pointers.promote(
        promotion.grant, cas,
        occurred_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
    ) == pointer
    assert tree_snapshot(harness.state_root) == promoted_state
    with pytest.raises(PointerSuperseded):
        pointers.promote(
            promotion.grant,
            replace(cas, expected_current_receipt_sha256="f" * 64),
            occurred_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
        )
    assert tree_snapshot(harness.state_root) == promoted_state
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


def test_exact_rollback_replay_uses_journal_and_retained_prior(tmp_path, monkeypatch):
    harness, _generations, pointers, _observations = _runtime(tmp_path)
    registry = harness.registry.load().to_dict()
    lease = harness.leases.inspect(REPO_UUID)
    grant = harness.leases.acquire(
        REPO_UUID, "ROLLBACK", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    prior_pointer = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 1,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": 1, "fence_token": 1, "state_schema_version": 2,
        "current": {"generation_id": "gen-newer", "receipt_sha256": "b" * 64},
        "last_good": {"generation_id": "gen-older", "receipt_sha256": "a" * 64},
    })
    current = PointerSet.from_mapping({
        **prior_pointer.to_dict(), "pointer_revision": 2,
        "operation_epoch": grant.operation_epoch,
        "fence_token": grant.lease.to_dict()["fence_token"],
        "current": {"generation_id": "gen-older", "receipt_sha256": "a" * 64},
        "last_good": {"generation_id": "gen-newer", "receipt_sha256": "b" * 64},
    })
    retained = PriorPointerRecord.from_mapping({
        "contract": "graphify.workspace.prior_pointer", "schema_version": 2,
        "retained_at": "2026-07-16T19:00:00Z", "replaced_by_revision": 2,
        "pointer_set": prior_pointer.to_dict(),
    })
    candidate = SimpleNamespace(
        sha256="a" * 64,
        to_dict=lambda: {
            "generation_id": "gen-older", "active_source_revision": 1,
            "source_epoch": 1,
        },
    )
    event = SimpleNamespace(to_dict=lambda: {
        "transition": "ROLLED_BACK", "generation_id": "gen-older",
        "receipt_sha256": "a" * 64, "pointer_revision": 2,
        "operation_epoch": grant.operation_epoch,
        "fence_token": grant.lease.to_dict()["fence_token"],
    })
    monkeypatch.setattr(pointers, "_preliminary_pointer", lambda _repo: current)
    monkeypatch.setattr(pointers, "_lock_set", lambda *args: [])
    monkeypatch.setattr(pointers, "_verify_generation", lambda *args, **kw: candidate)
    monkeypatch.setattr(pointers, "retained_prior", lambda *args, **kw: retained)
    monkeypatch.setattr(
        pointers.journal, "read_stable", lambda *args, **kw: JournalSnapshot(None, (event,)),
    )
    cas = PointerCAS(
        expected_pointer_revision=1, expected_active_source_revision=1,
        expected_source_epoch=1, expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        expected_state_schema_version=2,
        expected_fence_token=grant.lease.to_dict()["fence_token"],
        candidate_generation_id="gen-older", candidate_receipt_sha256="a" * 64,
        expected_current_receipt_sha256="b" * 64,
    )
    before = tree_snapshot(harness.state_root)
    assert pointers.rollback(
        grant, cas, occurred_at=START + timedelta(seconds=1), monotonic_ns=10_001,
    ) == current
    assert tree_snapshot(harness.state_root) == before
    with pytest.raises(PointerConflict):
        pointers.rollback(
            grant, replace(cas, expected_current_receipt_sha256="f" * 64),
            occurred_at=START + timedelta(seconds=1), monotonic_ns=10_001,
        )
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("missing_pointer", [False, True])
def test_promotion_refuses_visible_pointer_without_journal_authority(
    tmp_path, monkeypatch, missing_pointer,
):
    harness, _generations, pointers, _observations = _runtime(tmp_path)
    visible = None if missing_pointer else PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 1,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": 1, "fence_token": 1, "state_schema_version": 2,
        "current": {"generation_id": "gen-older", "receipt_sha256": "a" * 64},
        "last_good": None,
    })
    event = SimpleNamespace(to_dict=lambda: {
        "transition": "PROMOTED", "generation_id": "gen-newer",
        "receipt_sha256": "b" * 64, "pointer_revision": 3,
        "operation_epoch": 3, "fence_token": 3,
    })
    candidate = SimpleNamespace(
        sha256="c" * 64,
        to_dict=lambda: {"generation_id": "gen-candidate", "source_epoch": 1},
    )
    operation = SimpleNamespace(repo_uuid=REPO_UUID)
    touched = []
    monkeypatch.setattr(pointers.leases, "current_operation", lambda *a, **kw: nullcontext(operation))
    monkeypatch.setattr(pointers, "_assert_no_gc_intent", lambda *a, **kw: None)
    monkeypatch.setattr(pointers, "_preliminary_pointer", lambda *a: visible)
    monkeypatch.setattr(pointers, "_lock_set", lambda *a: [])
    monkeypatch.setattr(pointers.state, "existing_generation_locks", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pointers.state, "private_directory_exists", lambda *a: True)
    monkeypatch.setattr(pointers.journal, "read_stable", lambda *a, **kw: JournalSnapshot(None, (event,)))
    monkeypatch.setattr(pointers, "_verify_generation", lambda *a, **kw: candidate)
    monkeypatch.setattr(pointers, "_journal_certifies", lambda *a, **kw: True)
    monkeypatch.setattr(pointers, "_validate_cas", lambda *a: None)
    monkeypatch.setattr(pointers, "_verify_ref", lambda *a, **kw: candidate)
    monkeypatch.setattr(pointers, "_pointer_document", lambda *a, **kw: visible)
    monkeypatch.setattr(pointers.state, "cleanup_atomic_temps", lambda *a, **kw: touched.append("cleanup"))
    monkeypatch.setattr(pointers.journal, "recover_locked", lambda *a, **kw: touched.append("journal") or JournalSnapshot(None, (event,)))
    monkeypatch.setattr(pointers, "_persist_move", lambda *a, **kw: touched.append("persist") or visible)
    cas = PointerCAS(
        expected_pointer_revision=0 if missing_pointer else 1,
        expected_active_source_revision=1, expected_source_epoch=1,
        expected_operation_epoch=1, expected_migration_epoch=0,
        expected_state_schema_version=2, expected_fence_token=1,
        candidate_generation_id="gen-candidate", candidate_receipt_sha256="c" * 64,
        expected_current_receipt_sha256=None,
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(PointerCorrupt, match="visible pointer"):
        pointers.promote(
            SimpleNamespace(), cas,
            occurred_at=START, monotonic_ns=10_000,
        )
    assert touched == []
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


@pytest.mark.parametrize("old_source_revision", [1, 2])
@pytest.mark.parametrize("current_corrupt", [False, True])
def test_promotion_retains_only_current_source_last_good(
    tmp_path, monkeypatch, old_source_revision, current_corrupt,
):
    _harness, _generations, pointers, _observations = _runtime(tmp_path)
    current = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 1,
        "active_source_revision": old_source_revision, "source_epoch": 1,
        "operation_epoch": 1, "fence_token": 1, "state_schema_version": 2,
        "current": {"generation_id": "gen-old", "receipt_sha256": "a" * 64},
        "last_good": {"generation_id": "gen-older", "receipt_sha256": "c" * 64},
    })
    operation = SimpleNamespace(
        repo_uuid=REPO_UUID, fence_token=2,
        grant=SimpleNamespace(active_source_revision=2, operation_epoch=2),
    )
    candidate = SimpleNamespace(
        sha256="b" * 64,
        to_dict=lambda: {
            "generation_id": "gen-new", "active_source_revision": 2,
            "source_epoch": 1,
        },
    )
    old = SimpleNamespace(
        sha256="a" * 64,
        to_dict=lambda: {
            "generation_id": "gen-old", "active_source_revision": old_source_revision,
        },
    )
    older = SimpleNamespace(
        sha256="c" * 64,
        to_dict=lambda: {
            "generation_id": "gen-older", "active_source_revision": old_source_revision,
        },
    )

    def verify_ref(_repo_uuid, ref, **_kwargs):
        if ref["generation_id"] == "gen-old":
            if current_corrupt:
                raise GenerationError("invalid old receipt")
            return old
        return older

    monkeypatch.setattr(pointers.leases, "current_operation", lambda *a, **kw: nullcontext(operation))
    monkeypatch.setattr(pointers, "_assert_no_gc_intent", lambda *a, **kw: None)
    monkeypatch.setattr(pointers, "_preliminary_pointer", lambda *a, **kw: current)
    monkeypatch.setattr(pointers, "_lock_set", lambda *a, **kw: [])
    monkeypatch.setattr(pointers, "_verify_visible_pointer_journal", lambda *a, **kw: None)
    monkeypatch.setattr(pointers, "_verify_generation", lambda *a, **kw: candidate)
    monkeypatch.setattr(pointers, "_verify_ref", verify_ref)
    monkeypatch.setattr(pointers.state, "cleanup_atomic_temps", lambda *a, **kw: None)
    monkeypatch.setattr(pointers.journal, "recover_locked", lambda *a, **kw: JournalSnapshot(None, ()))
    monkeypatch.setattr(pointers, "_journal_certifies", lambda *a, **kw: True)
    monkeypatch.setattr(pointers, "_validate_cas", lambda *a, **kw: None)
    monkeypatch.setattr(
        pointers, "_persist_move", lambda _operation, *, pointer, **_kw: pointer,
    )
    cas = PointerCAS(1, 2, 1, 2, 0, 2, 2, "gen-new", "b" * 64, "a" * 64)
    moved = pointers.promote(
        SimpleNamespace(), cas, occurred_at=START, monotonic_ns=10_001,
    )
    expected = None if old_source_revision == 1 else {
        "generation_id": "gen-older" if current_corrupt else "gen-old",
        "receipt_sha256": "c" * 64 if current_corrupt else "a" * 64,
    }
    assert moved.to_dict()["last_good"] == expected


@pytest.mark.parametrize(
    ("switch_source", "repair_fault", "corrupt_current"),
    [
        (False, None, False), (True, None, False),
        (False, "prior_durable", False), (True, "prior_durable", False),
        (False, None, True),
        (False, "prior_durable", True),
        (False, "pending_durable", True),
        (False, "visible", True),
        (False, "journal_durable", True),
        (False, "quarantine_renamed", True),
    ],
)
def test_successor_pointer_recovery_retains_only_active_source_last_good(
    tmp_path, monkeypatch, switch_source, repair_fault, corrupt_current,
):
    harness, generations, pointers, observations = _runtime(tmp_path)

    def certify_and_promote(generation_id, tick, current=None, *, interrupt=False):
        request_value = _request(harness, observations).to_dict()
        request_value.update(
            expected_pointer_revision=0 if current is None else current.to_dict()["pointer_revision"],
            expected_current_receipt_sha256=(
                None if current is None else current.to_dict()["current"]["receipt_sha256"]
            ),
        )
        request = StructuralBuildRequest.from_mapping(request_value)
        generations.request_staged_build(
            REPO_UUID, generation_id, request, source_observations=observations,
        )
        attempt = generations.acquire_staged_operation(
            REPO_UUID, generation_id, request, attempt_sha256="5" * 64,
            operation="BUILD", acquired_at=START, monotonic_ns=tick, ttl_ns=1_000_000,
        )
        allocation = generations.allocate(
            attempt.grant, expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=POLICY, generation_id=generation_id,
            occurred_at=START, monotonic_ns=tick + 1,
        )
        preparation = generations.prepare_staged_build(attempt, allocation, monotonic_ns=tick + 2)
        payload = preparation.staging_path / "graphify-out"
        payload.mkdir(mode=0o700)
        (payload / "graph.json").write_bytes(b"{}\n")
        (payload / "input-manifest.json").write_bytes(observations[0].consumed_inputs.canonical)
        completion = generations.complete_staged_build(
            preparation, source_observations=observations, monotonic_ns=tick + 3,
        )
        queue = generations.semantic_queue
        queue.reconcile(
            attempt.grant, (), source_epoch=1, policy_sha256=observations[0].policy_sha256,
            source_observations=observations,
            desired_watermark=request.expected_active_source_revision, semantic_required=False,
            monotonic_ns=tick + 4,
        )
        queue.bind_sealed_inputs(
            attempt.grant,
            sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
            monotonic_ns=tick + 5,
        )
        receipt = generations.certify(
            attempt.grant, allocation,
            CertificationRequest(
                source_commit=observations[0].source_commit, source_epoch=1,
                policy_sha256=observations[0].policy_sha256,
                observation_manifest_sha256=observations[0].inventory_sha256,
                queue_watermark=request.expected_active_source_revision,
                semantic_completeness="not_required",
                compatibility_sha256=COMPATIBILITY_MANIFEST.sha256,
                validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue"),
            ),
            source_observations=observations, declared_entries=completion.entries,
            staged_completion=completion, occurred_at=START, monotonic_ns=tick + 6,
        )
        promotion = generations.acquire_staged_recovery(
            REPO_UUID, generation_id, request, attempt_sha256="7" * 64,
            acquired_at=START, monotonic_ns=tick + 2_000_000, ttl_ns=1_000_000,
        )
        cas = PointerCAS(
            request.expected_pointer_revision, promotion.grant.active_source_revision, 1,
            promotion.grant.operation_epoch, promotion.grant.migration_epoch, 2,
            promotion.grant.lease.to_dict()["fence_token"], generation_id, receipt.sha256,
            request.expected_current_receipt_sha256,
        )
        if interrupt:
            def fault(label):
                if label == "pointer:promoted:pending_durable":
                    raise InjectedFault(label)

            pointers.fault_hook = fault
            with pytest.raises(InjectedFault, match="pending_durable"):
                pointers.promote(
                    promotion.grant, cas, occurred_at=START, monotonic_ns=tick + 2_000_001,
                )
            pointers.fault_hook = lambda _label: None
            successor = generations.acquire_staged_recovery(
                REPO_UUID, generation_id, request, attempt_sha256="8" * 64,
                acquired_at=START, monotonic_ns=tick + 4_000_000, ttl_ns=1_000_000,
            )
            if repair_fault is not None:
                def repair_interrupt(label):
                    if label == f"pointer:repaired:{repair_fault}":
                        raise InjectedFault(label)

                pointers.fault_hook = repair_interrupt
                with pytest.raises(InjectedFault, match=repair_fault):
                    pointers.recover(
                        successor.grant, occurred_at=START,
                        monotonic_ns=tick + 4_000_001,
                    )
                pointers.fault_hook = lambda _label: None
            pointer = pointers.recover(
                successor.grant, occurred_at=START, monotonic_ns=tick + 4_000_001,
            )
            assert successor.grant.operation_epoch > promotion.grant.operation_epoch
            promotion = successor
            completed_at = tick + 4_000_002
        else:
            pointer = pointers.promote(
                promotion.grant, cas, occurred_at=START, monotonic_ns=tick + 2_000_001,
            )
            completed_at = tick + 2_000_002
        generations.complete_staged_promotion(promotion, pointer, monotonic_ns=completed_at)
        harness.leases.release(promotion.grant)
        return pointer

    first = certify_and_promote("gen-a1", 10_000)
    second = certify_and_promote("gen-a2", 3_000_000, first)
    assert second.to_dict()["last_good"] == first.to_dict()["current"]
    if corrupt_current:
        generation = generations.state.path(generations._generation(REPO_UUID, "gen-a2"))
        graph = next(generation.rglob("graph.json"))
        graph.write_bytes(b"corrupt payload\n")
        state = harness.leases.inspect(REPO_UUID)
        repair = harness.leases.acquire(
            REPO_UUID, "POINTER_RECOVERY", harness.leases.current_owner(),
            expected_registry_revision=1, expected_active_source_revision=1,
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START, monotonic_ns=6_000_000, ttl_ns=1_000_000,
        )
        plan = pointers.analyze_repair(
            REPO_UUID, active_source_revision=1,
            operation_epoch=repair.operation_epoch,
            fence_token=repair.lease.to_dict()["fence_token"],
        )
        assert plan.candidate == first.to_dict()["current"]
        assert plan.pointer_action == "replace"
        if repair_fault == "quarantine_renamed":
            def quarantine_interrupt(label):
                if label == "pointer:quarantine:gen-a2:renamed":
                    raise InjectedFault(label)

            pointers.state.fault_hook = quarantine_interrupt
            with pytest.raises(CommitUnknown, match="before both directories were durable"):
                pointers.recover(
                    repair, occurred_at=START, monotonic_ns=6_000_001,
                )
            pointers.state.fault_hook = lambda _label: None
            source = generations._generation(REPO_UUID, "gen-a2")
            destination = (
                pointers._workspace(REPO_UUID) / "quarantine" / "corrupt"
                / f"gen-a2.{plan.next_pointer_revision}"
            )
            assert not pointers.state.path(source).exists()
            assert pointers.state.path(destination).is_dir()
            parents = {}
            for relative, name in ((source.parent, "source"), (destination.parent, "destination")):
                stat = pointers.state.path(relative).stat()
                parents[(stat.st_dev, stat.st_ino)] = name
            original_fsync = pointers.state.syscalls.fsync
            observed = []
            fail_retry = True

            def record_fsync(descriptor):
                name = parents.get((os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino))
                if name is not None:
                    observed.append(name)
                    if fail_retry and name == "source":
                        raise OSError("injected quarantine parent sync failure")
                original_fsync(descriptor)

            monkeypatch.setattr(pointers.state.syscalls, "fsync", record_fsync)
            with pytest.raises(OSError, match="injected quarantine parent sync failure"):
                pointers.recover(
                    repair, occurred_at=START, monotonic_ns=6_000_001,
                )
            assert pointers.state.path(pointers._pending(REPO_UUID)).exists()
            fail_retry = False
            observed.clear()
        elif repair_fault is not None:
            def repair_interrupt(label):
                if label == f"pointer:repaired:{repair_fault}":
                    raise InjectedFault(label)

            pointers.fault_hook = repair_interrupt
            with pytest.raises(InjectedFault, match=repair_fault):
                pointers.recover(
                    repair, occurred_at=START, monotonic_ns=6_000_001,
                )
            pointers.fault_hook = lambda _label: None
        recovered = pointers.recover(
            repair, occurred_at=START, monotonic_ns=6_000_001,
        )
        if repair_fault == "quarantine_renamed":
            assert "source" in observed and "destination" in observed
        assert recovered.to_dict()["current"] == first.to_dict()["current"]
        assert recovered.to_dict()["pointer_revision"] >= second.to_dict()["pointer_revision"] + 1
        assert not pointers.state.path(pointers._pending(REPO_UUID)).exists()
        assert pointers.analyze_repair(
            REPO_UUID, active_source_revision=1,
        ).classification == "no_op"
        return
    if switch_source:
        linked = tmp_path.resolve() / "linked"
        git_output(harness.repo, "worktree", "add", "--quiet", str(linked))
        source = discover_source(linked)
        harness.registry.adopt(
            source, OperatorAuthorization(
                IdentityAction.ADOPT, "operator:s3-test", "linked fixture",
                "2026-07-16T19:00:00Z", "adopt-linked",
            ), expected_revision=1,
        )
        state = harness.leases.inspect(REPO_UUID)
        harness.registry.activate_source(
            source, OperatorAuthorization(
                IdentityAction.ACTIVATE, "operator:s3-test", "select linked fixture",
                "2026-07-16T19:00:00Z", "activate-linked",
            ), leases=harness.leases, owner=harness.leases.current_owner(),
            expected_registry_revision=2, expected_active_source_revision=1,
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START, monotonic_ns=6_000_000, ttl_ns=1_000_000,
        )
        observations = _observations(linked)
        trust_source_observations(generations, observations)
    recovered = certify_and_promote("gen-new", 7_000_000, second, interrupt=True)
    value = recovered.to_dict()
    assert value["current"]["generation_id"] == "gen-new"
    assert value["active_source_revision"] == (2 if switch_source else 1)
    assert value["last_good"] == (None if switch_source else second.to_dict()["current"])

    if not switch_source:
        state = harness.leases.inspect(REPO_UUID)
        rollback = harness.leases.acquire(
            REPO_UUID, "ROLLBACK", harness.leases.current_owner(),
            expected_registry_revision=1, expected_active_source_revision=1,
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START, monotonic_ns=12_000_000, ttl_ns=1_000_000,
        )
        rolled_back = pointers.rollback(
            rollback,
            PointerCAS(
                value["pointer_revision"], 1, 1, rollback.operation_epoch,
                rollback.migration_epoch, 2, rollback.lease.to_dict()["fence_token"],
                "gen-a2", second.to_dict()["current"]["receipt_sha256"],
                value["current"]["receipt_sha256"],
            ),
            occurred_at=START, monotonic_ns=12_000_001,
        )
        assert rolled_back.to_dict()["current"] == second.to_dict()["current"]
        harness.leases.release(rollback)


def test_operator_repair_replays_only_matching_durable_result(tmp_path, monkeypatch):
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
    fence_token = grant.lease.to_dict()["fence_token"]
    current = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 3,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": grant.operation_epoch, "fence_token": fence_token,
        "state_schema_version": 2,
        "current": {"generation_id": "gen-repaired", "receipt_sha256": "a" * 64},
        "last_good": None,
    })
    event = SimpleNamespace(to_dict=lambda: {
        "transition": "REPAIRED", "generation_id": "gen-repaired",
        "receipt_sha256": "a" * 64, "pointer_revision": 3,
        "operation_epoch": grant.operation_epoch, "fence_token": fence_token,
    })
    snapshot = JournalSnapshot(None, (event,))
    approved = PointerRepairPlan(
        "repairable", current.to_dict()["current"], None, 3, "prior",
        "replace", ("append_repair",), (), "b" * 64,
    )
    observed = PointerRepairPlan(
        "no_op", current.to_dict()["current"], None, 3, "current",
        "none", (), (), "c" * 64,
    )
    analysis = SimpleNamespace(
        plan=observed, current=current, pending=None,
        journal=JournalRecoveryProjection(snapshot, (), "d" * 64),
    )
    monkeypatch.setattr(
        pointers, "_repair_analysis_locked", lambda *a, **kw: nullcontext(analysis),
    )
    before = tree_snapshot(harness.state_root)
    assert pointers.recover(
        grant, expected_plan=approved, occurred_at=START + timedelta(seconds=1),
        monotonic_ns=10_001,
    ) == current
    assert tree_snapshot(harness.state_root) == before

    wrong_result = replace(approved, next_pointer_revision=2)
    with pytest.raises(PointerSuperseded, match="plan changed"):
        pointers.recover(
            grant, expected_plan=wrong_result,
            occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
        )


def test_repair_accepts_authoritative_rollback_after_superseded_attempt(tmp_path, monkeypatch):
    _harness, _generations, pointers, _observations = _runtime(tmp_path)
    previous = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 2,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": 2, "fence_token": 2, "state_schema_version": 2,
        "current": {"generation_id": "gen-newer", "receipt_sha256": "b" * 64},
        "last_good": {"generation_id": "gen-older", "receipt_sha256": "a" * 64},
    })
    rolled_back = PointerSet.from_mapping({
        **previous.to_dict(), "pointer_revision": 3,
        "operation_epoch": 3, "fence_token": 3,
        "current": {"generation_id": "gen-older", "receipt_sha256": "a" * 64},
        "last_good": {"generation_id": "gen-newer", "receipt_sha256": "b" * 64},
    })
    prior = PriorPointerRecord.from_mapping({
        "contract": "graphify.workspace.prior_pointer", "schema_version": 2,
        "retained_at": "2026-07-16T19:00:00Z", "replaced_by_revision": 3,
        "pointer_set": previous.to_dict(),
    })

    def event(transition, revision, *, operation_epoch=3, fence_token=3):
        return SimpleNamespace(
            to_dict=lambda: {
                "transition": transition, "generation_id": "gen-older",
                "receipt_sha256": "a" * 64, "pointer_revision": revision,
                "operation_epoch": operation_epoch, "fence_token": fence_token,
            },
            sha256=transition,
        )

    certified, superseded, rollback = (
        event("CERTIFIED", None), event("SUPERSEDED", 2), event("ROLLED_BACK", 3),
    )
    snapshot = JournalSnapshot(None, (certified, superseded, rollback))
    projection = JournalRecoveryProjection(snapshot, (), "c" * 64)
    monkeypatch.setattr(pointers.journal, "project_recovery", lambda *a, **kw: projection)
    older = SimpleNamespace(
        sha256="a" * 64,
        to_dict=lambda: {"generation_id": "gen-older", "active_source_revision": 1},
    )
    newer = SimpleNamespace(
        sha256="b" * 64,
        to_dict=lambda: {"generation_id": "gen-newer", "active_source_revision": 1},
    )
    def verified_refs(_repo_uuid, pointer, *, deadline_ns):
        value = pointer.to_dict()
        current_receipt = older if value["current"]["generation_id"] == "gen-older" else newer
        last_good_receipt = (
            older if value["last_good"]["generation_id"] == "gen-older" else newer
        )
        return {"current": current_receipt, "last_good": last_good_receipt}, set()

    monkeypatch.setattr(pointers, "_verify_repair_refs", verified_refs)
    analysis = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=None,
        fence_token=None, current=rolled_back, pending=None, prior=prior,
        raw_evidence={"current_sha256": rolled_back.sha256},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert analysis.plan.classification == "no_op"
    assert analysis.plan.candidate["generation_id"] == "gen-older"

    # A rollback interrupted before its journal append is still bound to the
    # exact prior last_good reference by the retained prior record.
    assert pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, (certified, superseded)),
        rolled_back, rolled_back, prior, deadline_ns=None,
    )
    assert not pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, (certified, superseded, rollback, event("SUPERSEDED", 3))),
        rolled_back, None, prior, deadline_ns=None,
    )
    interrupted = JournalRecoveryProjection(
        JournalSnapshot(None, (certified, superseded)), (), "d" * 64,
    )
    monkeypatch.setattr(pointers.journal, "project_recovery", lambda *a, **kw: interrupted)
    monkeypatch.setattr(
        pointers, "verify_pointer",
        lambda pointer, **kw: verified_refs(REPO_UUID, pointer, deadline_ns=None)[0],
    )
    pending_analysis = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=3,
        fence_token=3, current=rolled_back, pending=rolled_back, prior=prior,
        raw_evidence={"current_sha256": rolled_back.sha256},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert pending_analysis.plan.pointer_action == "resume_pending"

    # Successor recovery advances the interrupted rollback's revision while
    # preserving its exact prior last_good. Both another interruption and a
    # completed repair must retain that rollback authority.
    repaired = PointerSet.from_mapping({
        **rolled_back.to_dict(), "pointer_revision": 4,
        "operation_epoch": 4, "fence_token": 4,
    })
    repair_prior = PriorPointerRecord.from_mapping({
        **prior.to_dict(), "replaced_by_revision": 4,
    })
    advanced_prior_analysis = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=4,
        fence_token=4, current=rolled_back, pending=rolled_back, prior=repair_prior,
        raw_evidence={"current_sha256": rolled_back.sha256},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert advanced_prior_analysis.plan.pointer_action == "replace"
    assert advanced_prior_analysis.plan.next_pointer_revision == 5
    twice_advanced_prior = PriorPointerRecord.from_mapping({
        **prior.to_dict(), "replaced_by_revision": 5,
    })
    repeated_analysis = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=4,
        fence_token=4, current=rolled_back, pending=rolled_back,
        prior=twice_advanced_prior,
        raw_evidence={"current_sha256": rolled_back.sha256},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert repeated_analysis.plan.pointer_action == "replace"
    assert repeated_analysis.plan.next_pointer_revision == 6
    with pytest.raises(PointerCorrupt, match="retained prior"):
        pointers._validate_pending_relationship(
            rolled_back, repaired, twice_advanced_prior,
        )
    assert not pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, (certified, superseded)),
        rolled_back, None, twice_advanced_prior, deadline_ns=None,
    )
    for visible in (rolled_back, repaired):
        analysis = pointers._derive_repair_analysis(
            REPO_UUID, active_source_revision=1, operation_epoch=4,
            fence_token=4, current=visible, pending=repaired, prior=repair_prior,
            raw_evidence={"current_sha256": visible.sha256},
            allow_atomic_temps=False, deadline_ns=None,
        )
        assert analysis.plan.pointer_action == "resume_pending"

    repair_event = event("REPAIRED", 4, operation_epoch=4, fence_token=4)
    repaired_snapshot = JournalSnapshot(None, (certified, superseded, repair_event))
    monkeypatch.setattr(
        pointers.journal, "project_recovery",
        lambda *a, **kw: JournalRecoveryProjection(repaired_snapshot, (), "e" * 64),
    )
    finalized = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=4,
        fence_token=4, current=repaired, pending=repaired, prior=repair_prior,
        raw_evidence={"current_sha256": repaired.sha256},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert finalized.plan.pointer_action == "finalize_pending"
    complete = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=None,
        fence_token=None, current=repaired, pending=None, prior=repair_prior,
        raw_evidence={"current_sha256": repaired.sha256},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert complete.plan.classification == "no_op"
    assert not pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, (*repaired_snapshot.events, event("SUPERSEDED", 4))),
        repaired, None, repair_prior, deadline_ns=None,
    )
    unrelated_prior = PriorPointerRecord.from_mapping({
        **repair_prior.to_dict(),
        "pointer_set": {
            **previous.to_dict(),
            "last_good": {"generation_id": "gen-unrelated", "receipt_sha256": "f" * 64},
        },
    })
    assert not pointers._rollback_authorizes_superseded(
        repaired_snapshot, repaired, None, unrelated_prior, deadline_ns=None,
    )
    unrelated_move = SimpleNamespace(to_dict=lambda: {
        "transition": "PROMOTED", "generation_id": "gen-unrelated",
        "receipt_sha256": "f" * 64, "pointer_revision": 3,
        "operation_epoch": 3, "fence_token": 3,
    })
    assert not pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, (certified, superseded, unrelated_move)),
        repaired, repaired, repair_prior, deadline_ns=None,
    )

    monkeypatch.setattr(pointers.journal, "project_recovery", lambda *a, **kw: interrupted)
    with pytest.raises(PointerCorrupt, match="superseded"):
        pointers._derive_repair_analysis(
            REPO_UUID, active_source_revision=1, operation_epoch=None,
            fence_token=None, current=rolled_back, pending=None, prior=None,
            raw_evidence={"current_sha256": rolled_back.sha256},
            allow_atomic_temps=False, deadline_ns=None,
        )


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


def test_operational_gc_plan_accepts_policy_above_preview_limit(tmp_path, monkeypatch):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    accepted = POLICY.to_dict()
    accepted["global_max_generations"] = 5000
    accepted["workspace_max_generations"] = 5000
    policy = CapacityPolicy.from_mapping(accepted)
    generations_found = tuple(f"gen-{number:04d}" for number in range(4097))
    monkeypatch.setattr(gc, "_generation_ids", lambda *a, **kw: generations_found)
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
            operation, capacity_policy=policy, protections=protections,
            probe_locks=False,
        )
    assert plan.candidates == generations_found


@pytest.mark.parametrize("missing_pointer", [False, True])
def test_gc_plan_rejects_pointer_behind_durable_journal(
    tmp_path, monkeypatch, missing_pointer,
):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    stale_pointer = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 1,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": 1, "fence_token": 1, "state_schema_version": 2,
        "current": {"generation_id": "gen-old", "receipt_sha256": "a" * 64},
        "last_good": None,
    })
    monkeypatch.setattr(
        pointers, "load", lambda *args, **kw: None if missing_pointer else stale_pointer,
    )
    monkeypatch.setattr(pointers, "verify_pointer", lambda *args, **kw: {})
    event = SimpleNamespace(to_dict=lambda: {
        "transition": "PROMOTED", "pointer_revision": 2,
    })
    monkeypatch.setattr(
        pointers.journal, "read_stable", lambda *args, **kw: JournalSnapshot(None, (event,)),
    )
    if missing_pointer:
        pointers.state.ensure_directory(pointers.journal._directory(REPO_UUID))
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
    before = tree_snapshot(harness.state_root)
    with pytest.raises(PointerCorrupt, match="stale relative|missing relative"):
        gc.plan(
            grant, capacity_policy=POLICY,
            protections=GcProtection(empty, empty, empty, empty, empty, empty),
            monotonic_ns=10_001,
        )
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


def test_gc_rejects_oversized_intent_before_decoding(tmp_path):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    path = gc.state.path(gc._intent_path(REPO_UUID))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = b" " * (1024 * 1024 + 1)
    path.write_bytes(payload)
    path.chmod(0o600)
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
    with pytest.raises(GcRecoveryRequired, match="read limit"):
        gc.plan(
            grant, capacity_policy=POLICY,
            protections=GcProtection(empty, empty, empty, empty, empty, empty),
            monotonic_ns=10_001,
        )
    assert path.read_bytes() == payload


def test_gc_keeps_shared_lock_protection_during_locked_recheck(tmp_path):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    for generation_id in ("gen-candidate", "gen-reader"):
        gc.state.ensure_directory(generations._generation(REPO_UUID, generation_id))
        lock = gc.state.path(generations._lock(REPO_UUID, generation_id))
        gc.state.ensure_directory(generations._lock(REPO_UUID, generation_id).parent)
        lock.write_bytes(b"")
        lock.chmod(0o600)
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
    held, release = Event(), Event()

    def hold_reader():
        with gc.state.existing_generation_lock(
            generations._lock(REPO_UUID, "gen-reader"),
            generation_id="gen-reader", exclusive=False,
        ):
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(hold_reader)
        assert held.wait(5)
        try:
            plan = gc.plan(
                grant, capacity_policy=POLICY, protections=protections,
                monotonic_ns=10_001,
            )
            assert plan.candidates == ("gen-candidate",)
            assert ("gen-reader", ("shared_lock",)) in plan.protected
            completion = gc.execute(
                grant, plan, capacity_policy=POLICY, protections=protections,
                occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
            )
            completed_state = tree_snapshot(harness.state_root)
            assert gc.execute(
                grant, plan, capacity_policy=POLICY, protections=protections,
                occurred_at=START + timedelta(seconds=2), monotonic_ns=10_003,
            ) == completion
            assert tree_snapshot(harness.state_root) == completed_state
            changed_policy = POLICY.to_dict()
            changed_policy["reserve_bytes"] += 1
            with pytest.raises(GcPlanStale, match="replay authority"):
                gc.execute(
                    grant, plan,
                    capacity_policy=CapacityPolicy.from_mapping(changed_policy),
                    protections=protections,
                    occurred_at=START + timedelta(seconds=2), monotonic_ns=10_004,
                )
            assert tree_snapshot(harness.state_root) == completed_state
        finally:
            release.set()
            reader.result(timeout=5)
    assert gc.state.path(generations._generation(REPO_UUID, "gen-reader")).is_dir()
    assert gc.state.path(
        gc._quarantine(REPO_UUID, "gen-candidate", grant.operation_epoch)
    ).is_dir()


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


@pytest.mark.parametrize("changed_field", ["operation_epoch", "quarantined"])
def test_gc_purge_requires_index_bound_completion(tmp_path, changed_field):
    harness, generations, pointers, _observations = _runtime(tmp_path)
    gc = GcStore(
        harness.state_root, harness.leases, generations, pointers,
        capabilities=harness.leases.state.capabilities,
    )
    candidate = "gen-purge-candidate"
    gc.state.ensure_directory(generations._generation(REPO_UUID, candidate))
    lock = gc.state.path(generations._lock(REPO_UUID, candidate))
    gc.state.ensure_directory(generations._lock(REPO_UUID, candidate).parent)
    lock.write_bytes(b"")
    lock.chmod(0o600)
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
    plan = gc.plan(grant, capacity_policy=POLICY, protections=protections, monotonic_ns=10_001)
    completion = gc.execute(
        grant, plan, capacity_policy=POLICY, protections=protections,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
    )
    assert completion.quarantined == (candidate,)
    changed = completion.to_dict()
    changed[changed_field] = (
        completion.operation_epoch + 1
        if changed_field == "operation_epoch" else []
    )
    completion_path = gc.state.path(gc._completion_path(REPO_UUID, plan.sha256))
    completion_path.write_bytes(GcCompletionState.from_mapping(changed).canonical)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GcRecoveryRequired, match="index does not bind"):
        gc.purge(
            grant, plan_sha256=plan.sha256, capacity_policy=POLICY,
            protections=protections, completed_at=START + timedelta(seconds=2),
            monotonic_ns=10_003,
        )
    assert tree_snapshot(harness.state_root) == before
    assert gc.state.path(gc._quarantine(REPO_UUID, candidate, grant.operation_epoch)).is_dir()


def test_gc_reconcile_retry_returns_indexed_completion(tmp_path):
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
    plan = gc.plan(grant, capacity_policy=POLICY, protections=protections, monotonic_ns=10_001)

    def interrupt_after_completion(point):
        if point == "gc:completion_durable":
            raise InjectedFault(point)

    gc.fault_hook = interrupt_after_completion
    with pytest.raises(InjectedFault):
        gc.execute(
            grant, plan, capacity_policy=POLICY, protections=protections,
            occurred_at=START + timedelta(seconds=1), monotonic_ns=10_002,
        )
    gc.fault_hook = lambda _point: None
    successor = harness.leases.acquire(
        REPO_UUID, "GC", harness.leases.current_owner(),
        expected_registry_revision=registry["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=grant.operation_epoch,
        expected_migration_epoch=grant.migration_epoch,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=2_000_000, ttl_ns=1_000_000,
    )
    completed = gc.reconcile(
        successor, capacity_policy=POLICY, protections=protections,
        completed_at=START + timedelta(seconds=3), monotonic_ns=2_000_001,
    )
    assert completed is not None
    before = tree_snapshot(harness.state_root)
    assert gc.reconcile(
        successor, capacity_policy=POLICY, protections=protections,
        completed_at=START + timedelta(seconds=4), monotonic_ns=2_000_002,
    ) == completed
    assert tree_snapshot(harness.state_root) == before


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
