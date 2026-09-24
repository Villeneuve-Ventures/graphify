"""Generation admission and certification capacity regressions from PR 163."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from graphify.workspace.generations import CapacityExceeded, CertificationRequest, GenerationConflict
from graphify.workspace.lifecycle_contracts import CapacityPolicy, StructuralBuildRequest, payload_manifest_sha256
from tests import test_workspace_lifecycle_s3 as fixtures
from tests.test_workspace_coordination_review import acquire
from tests.workspace_s3_helpers import COMPATIBILITY_MANIFEST, REPO_UUID, START, tree_snapshot


@pytest.mark.parametrize("operation", ["BUILD", "MIGRATE"])
def test_nonstaged_allocation_rejected_without_writes(tmp_path, operation):
    harness, generations, _, _ = fixtures._runtime(tmp_path)
    grant = acquire(harness, operation)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationConflict, match="staged"):
        generations.allocate(
            grant, expected_payload_bytes=16384, capacity_policy=fixtures.POLICY,
            generation_id=fixtures.GENERATION_ID, occurred_at=START,
            monotonic_ns=10_001,
        )
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("root_free", [0, 100_000])
def test_capacity_queries_actual_state_volume(tmp_path, monkeypatch, root_free):
    harness, generations, _, observations = fixtures._runtime(tmp_path)
    request = fixtures._request(harness, observations)
    generations.request_staged_build(
        REPO_UUID, fixtures.GENERATION_ID, request, source_observations=observations,
    )
    attempt = generations.acquire_staged_operation(
        REPO_UUID, fixtures.GENERATION_ID, request, attempt_sha256="5" * 64,
        operation="BUILD", acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    queried = []

    def disk_usage(path):
        queried.append(path)
        return SimpleNamespace(free=root_free if path == harness.state_root else 100_000 - root_free)

    monkeypatch.setattr("graphify.workspace.generations.shutil.disk_usage", disk_usage)
    before = tree_snapshot(harness.state_root)
    kwargs = dict(expected_payload_bytes=request.expected_payload_bytes,
                  capacity_policy=fixtures.POLICY, generation_id=fixtures.GENERATION_ID,
                  occurred_at=START, monotonic_ns=10_001)
    if root_free:
        generations.allocate(attempt.grant, **kwargs)
    else:
        with pytest.raises(CapacityExceeded, match="filesystem reserve"):
            generations.allocate(attempt.grant, **kwargs)
        assert tree_snapshot(harness.state_root) == before
    assert queried == [harness.state_root]


def _certification(generations, attempt, completion, observations):
    observed = observations[0]
    queue = generations.semantic_queue
    queue.reconcile(
        attempt.grant, (), source_epoch=1, policy_sha256=observed.policy_sha256,
        source_observations=observations, desired_watermark=1, semantic_required=False,
        monotonic_ns=10_004,
    )
    queue.bind_sealed_inputs(
        attempt.grant,
        sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
        monotonic_ns=10_005,
    )
    return CertificationRequest(
        source_commit=observed.source_commit, source_epoch=1,
        policy_sha256=observed.policy_sha256,
        observation_manifest_sha256=observed.inventory_sha256,
        queue_watermark=1, semantic_completeness="not_required",
        compatibility_sha256=COMPATIBILITY_MANIFEST.sha256,
        validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue"),
    )


@pytest.mark.parametrize("shortfall", [0, 1])
def test_receipt_charged_with_payload_before_any_certification_write(tmp_path, monkeypatch, shortfall):
    # Determine the exact canonical receipt size with the same lifecycle inputs.
    harness, generations, _, observations = fixtures._runtime(tmp_path / "measure")
    _, attempt, completion = fixtures._complete(harness, generations, observations)
    request = _certification(generations, attempt, completion, observations)
    with harness.leases.current_operation(attempt.grant, monotonic_ns=10_006) as operation:
        receipt = generations._receipt(operation, completion.allocation, request, completion.entries)
    required = sum(entry["size"] for entry in completion.entries) + len(receipt.canonical)
    budget = required - shortfall
    policy_value = fixtures.POLICY.to_dict()
    policy_value.update(workspace_max_bytes=budget, global_max_bytes=budget)
    policy = CapacityPolicy.from_mapping(policy_value)
    original_request = fixtures._request

    def bounded_request(harness, observations):
        value = original_request(harness, observations).to_dict()
        value.update(expected_payload_bytes=budget, capacity_policy_sha256=policy.sha256)
        return StructuralBuildRequest.from_mapping(value)

    monkeypatch.setattr(fixtures, "_request", bounded_request)
    monkeypatch.setattr(fixtures, "POLICY", policy)
    harness, generations, _, observations = fixtures._runtime(tmp_path / "bounded")
    _, attempt, completion = fixtures._complete(harness, generations, observations)
    request = _certification(generations, attempt, completion, observations)
    before = tree_snapshot(harness.state_root)
    kwargs = dict(source_observations=observations, declared_entries=completion.entries,
                  staged_completion=completion, occurred_at=START + timedelta(seconds=1),
                  monotonic_ns=10_006)
    if shortfall:
        with pytest.raises(CapacityExceeded, match="receipt"):
            generations.certify(attempt.grant, completion.allocation, request, **kwargs)
        assert tree_snapshot(harness.state_root) == before
    else:
        receipt = generations.certify(attempt.grant, completion.allocation, request, **kwargs)
        assert sum(entry["size"] for entry in completion.entries) + len(receipt.canonical) == budget
        assert generations.verify_generation(REPO_UUID, fixtures.GENERATION_ID) == receipt
