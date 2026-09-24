"""Public queue regressions for the PR 162 review findings."""

from dataclasses import replace

import pytest

from graphify.workspace.adapters.base import SourceObservation as StructuralObservation
from graphify.workspace.identity import read_workspace_config
from graphify.workspace.lifecycle_observation import SourceObservation
from graphify.workspace.persistence import CommitUnknown
from graphify.workspace.semantic_queue import (
    SemanticDesiredWork,
    SemanticQueueConflict,
    SemanticQueueCorrupt,
    SemanticQueueError,
    SemanticQueuePolicy,
    SemanticQueueSnapshot,
    SemanticQueueStore,
)
from tests.test_workspace_contracts import manifests
from tests.test_workspace_coordination_review import acquire
from tests.workspace_s3_helpers import REPO_UUID, SUPPORTED, create_harness, tree_snapshot


@pytest.fixture
def queue_case(tmp_path):
    harness = create_harness(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    initial, _ = manifests(evidence)
    observation = SourceObservation("a" * 40, "b" * 64, StructuralObservation(initial, None, 2))
    queue = SemanticQueueStore(
        harness.state_root, harness.leases,
        policy=SemanticQueuePolicy(max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=1),
        capabilities=SUPPORTED,
    )
    work = SemanticDesiredWork(1, "b" * 64, "UPSERT", "main.py", "c" * 64, 2)
    build = acquire(harness, "BUILD")
    queue.reconcile(
        build, source_epoch=1, policy_sha256="b" * 64,
        source_observations=(observation, observation), desired_watermark=2,
        semantic_required=True, desired=(work,), monotonic_ns=10_001,
    )
    harness.leases.release(build)
    worker = acquire(harness, "SEMANTIC_CLAIM")
    return harness, queue, worker, observation, work


def claim_next(harness, queue, worker):
    claim = queue.claim(
        worker, config=read_workspace_config(harness.repo),
        host_agent_active=True, explicit_backend=None, monotonic_ns=10_001,
    )
    assert claim is not None
    return claim


@pytest.mark.parametrize("checkpoint", [None, "progress:parsed", "result:bad", "result:" + "A" * 64])
def test_completion_requires_durable_result_checkpoint(queue_case, checkpoint):
    harness, queue, worker, _, _ = queue_case
    claim = claim_next(harness, queue, worker)
    if checkpoint is not None:
        queue.checkpoint(worker, claim, checkpoint=checkpoint, monotonic_ns=10_002)
    before = tree_snapshot(harness.state_root)
    # Forging the returned token's checkpoint must not substitute for durable evidence.
    forged = replace(claim, checkpoint="result:" + "d" * 64)
    with pytest.raises(SemanticQueueError, match="result checkpoint"):
        queue.complete(worker, forged, monotonic_ns=10_003)
    assert tree_snapshot(harness.state_root) == before
    assert queue.inspect(REPO_UUID).completed_watermark == 0
    queue.checkpoint(worker, claim, checkpoint="result:" + "d" * 64, monotonic_ns=10_004)
    # The caller may still hold the original token; the stored checkpoint is authoritative.
    completed = queue.complete(worker, claim, monotonic_ns=10_005)
    assert completed.completed_watermark == completed.desired_watermark == 2


@pytest.mark.parametrize("field", ["expected_registry_revision", "expected_queue_revision", "expected_desired_watermark"])
@pytest.mark.parametrize("missing", [False, True])
def test_rejected_recovery_preserves_pending_commit(queue_case, field, missing):
    harness, queue, worker, _, _ = queue_case
    claim = claim_next(harness, queue, worker)
    current = queue.inspect(REPO_UUID)

    def interrupt(event):
        if event == "semantic_queue:pending_durable":
            raise RuntimeError("interrupted after durable intent")

    queue.state.fault_hook = interrupt
    with pytest.raises(CommitUnknown):
        queue.checkpoint(worker, claim, checkpoint="result:" + "d" * 64, monotonic_ns=10_002)
    queue.state.fault_hook = lambda event: None
    expected = dict(
        expected_registry_revision=1,
        expected_queue_revision=current.revision,
        expected_desired_watermark=current.desired_watermark,
    )
    invalid = dict(expected)
    invalid[field] = None if missing else expected[field] - 1
    before = tree_snapshot(harness.state_root)
    with pytest.raises(SemanticQueueConflict):
        queue.recover_uncertain_snapshot(worker, monotonic_ns=10_003, **invalid)
    assert tree_snapshot(harness.state_root) == before
    recovered = queue.recover_uncertain_snapshot(worker, monotonic_ns=10_004, **expected)
    assert recovered.revision == current.revision + 1
    assert recovered.items[0].claim.checkpoint == "result:" + "d" * 64
    assert not queue.state.path(queue._paths(REPO_UUID)[2]).exists()


@pytest.mark.parametrize("record_index", [0, 1, 2])
@pytest.mark.parametrize("recover", [False, True])
def test_queue_records_are_bounded_before_decoding(queue_case, monkeypatch, record_index, recover):
    harness, queue, worker, _, _ = queue_case
    claim_next(harness, queue, worker)
    path = queue.state.path(queue._paths(REPO_UUID)[record_index])
    # Valid JSON larger than the policy limit must never reach the decoder.
    oversized = b" " * queue.policy.max_bytes + b"{}"
    path.write_bytes(oversized)
    path.chmod(0o600)
    decoded_sizes = []
    original = SemanticQueueSnapshot.from_json

    def decode(raw):
        decoded_sizes.append(len(raw))
        return original(raw)

    monkeypatch.setattr(SemanticQueueSnapshot, "from_json", staticmethod(decode))
    if recover and record_index == 1:
        # Recovery may ignore a damaged backup when current authority is valid.
        queue._load_locked(REPO_UUID, recover=True)
    else:
        with pytest.raises(SemanticQueueCorrupt):
            queue._load_locked(REPO_UUID, recover=recover)
    assert all(size <= queue.policy.max_bytes for size in decoded_sizes)


def test_causal_invalidation_does_not_spend_execution_retries(queue_case):
    harness, queue, worker, observation, work = queue_case
    claim = claim_next(harness, queue, worker)
    queue.checkpoint(worker, claim, checkpoint="result:" + "d" * 64, monotonic_ns=10_002)
    queue.complete(worker, claim, monotonic_ns=10_003)
    harness.leases.release(worker)
    build = acquire(harness, "BUILD")
    predecessor = replace(work, operation="DELETE", desired_revision=1)
    reconciled = queue.reconcile(
        build, source_epoch=1, policy_sha256="b" * 64,
        source_observations=(observation, observation), desired_watermark=3,
        semantic_required=True, desired=(predecessor, work), monotonic_ns=10_001,
    )
    invalidated = next(item for item in reconciled.items if item.work == work)
    assert invalidated.status == "pending"
    assert invalidated.failure_count == 0
    harness.leases.release(build)
    worker = acquire(harness, "SEMANTIC_CLAIM")
    for expected_work in (predecessor, work):
        claim = claim_next(harness, queue, worker)
        assert claim.work == expected_work
        queue.checkpoint(worker, claim, checkpoint="result:" + "e" * 64, monotonic_ns=10_002)
        completed = queue.complete(worker, claim, monotonic_ns=10_003)
    assert completed.completed_watermark == completed.desired_watermark == 3


def test_policy_rejects_unsupported_parallel_claims():
    with pytest.raises(SemanticQueueError, match="one"):
        SemanticQueuePolicy(max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=2)
