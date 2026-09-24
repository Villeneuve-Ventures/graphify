"""Exact fenced recovery for terminal abandonment and pre-receipt crashes."""

from datetime import timedelta
from dataclasses import replace
import json

import pytest

from graphify.workspace.generations import CapacityExceeded, GenerationConflict
from graphify.workspace.leases import (
    LeaseBusy, LeaseRecoveryRequired, StagedBuildLeaseRecoveryRequired, StaleLease,
)
from graphify.workspace.lifecycle_contracts import canonical_json_bytes
from graphify.workspace.persistence import InjectedFault
from tests import test_workspace_lifecycle_s3 as fixtures
from tests.test_workspace_generation_capacity_review import _certification
from tests.workspace_s3_helpers import REPO_UUID, START, tree_snapshot, trust_source_observations


def _recover(generations, request, *, attempt_sha256="5" * 64, monotonic_ns=2_000_000):
    return generations.acquire_staged_recovery(
        REPO_UUID, fixtures.GENERATION_ID, request,
        attempt_sha256=attempt_sha256, acquired_at=START + timedelta(seconds=3),
        monotonic_ns=monotonic_ns, ttl_ns=1_000_000,
    )


def _abandoned(tmp_path):
    harness, generations, _, observations = fixtures._runtime(tmp_path)
    request, attempt, preparation = fixtures._prepare(harness, generations, observations)
    (preparation.staging_path / "graphify-out" / "graph.json").write_bytes(
        b"x" * (preparation.allocation.expected_payload_bytes + 1)
    )
    with pytest.raises(CapacityExceeded):
        generations.complete_staged_build(
            preparation, source_observations=observations, monotonic_ns=10_003,
        )
    return harness, generations, observations, request, attempt


def _restart_owner(harness, monkeypatch):
    old = harness.leases.current_owner()
    new = replace(old, pid=old.pid + 1, process_start_id="d" * 64)
    monkeypatch.setattr(harness.leases.identity_provider, "current_owner", lambda: new)
    return new


def test_abandoned_expired_build_lease_can_be_fenced_and_released(tmp_path, monkeypatch):
    harness, generations, observations, request, attempt = _abandoned(tmp_path)
    abandoned = generations.recover_staged_build(REPO_UUID)
    assert abandoned is not None
    assert abandoned.lifecycle_state == "ABANDONED"
    new_owner = _restart_owner(harness, monkeypatch)
    recovered = _recover(generations, request)
    assert recovered.state == abandoned
    assert recovered.grant.lease.to_dict()["owner"] == new_owner.to_dict()
    assert recovered.grant.lease.to_dict()["fence_token"] > attempt.grant.lease.to_dict()["fence_token"]
    assert _recover(generations, request).grant == recovered.grant
    with pytest.raises(StaleLease):
        harness.leases.release(attempt.grant)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationConflict, match="staged"):
        generations.allocate(
            recovered.grant, expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=fixtures.POLICY, generation_id=fixtures.GENERATION_ID,
            occurred_at=START, monotonic_ns=2_000_001,
        )
    assert tree_snapshot(harness.state_root) == before
    harness.leases.release(recovered.grant)
    fresh = fixtures._request(harness, observations)
    assert generations.request_staged_build(
        REPO_UUID, "gen-after-abandonment", fresh, source_observations=observations,
    ).lifecycle_state == "REQUESTED"


@pytest.mark.parametrize("damage", ["staging", "predecessor"])
def test_abandoned_cleanup_requires_durable_postconditions(tmp_path, damage):
    harness, generations, _, request, _ = _abandoned(tmp_path)
    if damage == "staging":
        generations.state.path(generations._staging(REPO_UUID, fixtures.GENERATION_ID)).mkdir(mode=0o700)
    else:
        _, previous, _ = generations._staged_build_paths(REPO_UUID)
        generations.state.path(previous).unlink()
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationConflict):
        _recover(generations, request)
    assert tree_snapshot(harness.state_root) == before


def test_unbound_lease_cannot_invalidate_abandoned_cleanup_fence(tmp_path):
    harness, generations, _, _, _ = _abandoned(tmp_path)
    lease = harness.leases.inspect(REPO_UUID)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(StagedBuildLeaseRecoveryRequired):
        harness.leases.acquire(
            REPO_UUID, "BUILD", harness.leases.current_owner(),
            expected_registry_revision=1, expected_active_source_revision=1,
            expected_operation_epoch=lease.operation_epoch,
            expected_migration_epoch=lease.migration_epoch,
            acquired_at=START, monotonic_ns=2_000_000, ttl_ns=1_000_000,
        )
    assert tree_snapshot(harness.state_root) == before


def test_abandoned_cleanup_rejects_wrong_attempt_without_writes(tmp_path):
    harness, generations, _, request, _ = _abandoned(tmp_path)
    before = tree_snapshot(harness.state_root)
    with pytest.raises((GenerationConflict, LeaseRecoveryRequired)):
        _recover(generations, request, attempt_sha256="9" * 64)
    assert tree_snapshot(harness.state_root) == before


def test_abandoned_active_foreign_lease_cannot_be_taken_over(tmp_path, monkeypatch):
    harness, generations, _, request, _ = _abandoned(tmp_path)
    _restart_owner(harness, monkeypatch)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(LeaseBusy):
        _recover(generations, request, monotonic_ns=10_004)
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("abandoned_from", ["REQUESTED", "CERTIFIED"])
def test_terminal_cleanup_recovers_other_abandonment_states(tmp_path, monkeypatch, abandoned_from):
    harness, generations, _, observations = fixtures._runtime(tmp_path)
    if abandoned_from == "REQUESTED":
        request = fixtures._request(harness, observations)
        generations.request_staged_build(
            REPO_UUID, fixtures.GENERATION_ID, request, source_observations=observations,
        )
        operation = "BUILD"
    else:
        request, build, completion = fixtures._complete(harness, generations, observations)
        certification = _certification(generations, build, completion, observations)
        generations.certify(
            build.grant, completion.allocation, certification,
            source_observations=observations, declared_entries=completion.entries,
            staged_completion=completion, occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_006,
        )
        harness.leases.release(build.grant)
        operation = "PROMOTE"
    attempt = generations.acquire_staged_operation(
        REPO_UUID, fixtures.GENERATION_ID, request, attempt_sha256="7" * 64,
        operation=operation, acquired_at=START + timedelta(seconds=2),
        monotonic_ns=20_000, ttl_ns=1_000_000,
    )
    (harness.repo / "main.py").write_text("answer = 43\n")
    changed = fixtures._observations(harness.repo)
    trust_source_observations(generations, changed)
    abandoned = generations.abandon_staged_build(
        attempt, source_observations=changed, monotonic_ns=20_001,
    )
    assert abandoned.lifecycle_state == "ABANDONED"
    assert abandoned.abandoned_from == abandoned_from
    assert abandoned.abandon_reason == "SOURCE_CHANGED"
    _restart_owner(harness, monkeypatch)
    recovered = _recover(generations, request, attempt_sha256="7" * 64)
    assert recovered.state == abandoned
    assert recovered.grant.lease.to_dict()["operation"] == operation
    released = harness.leases.release(recovered.grant)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None
    if abandoned_from == "CERTIFIED":
        assert generations.verify_generation(REPO_UUID, fixtures.GENERATION_ID).sha256 == abandoned.receipt_sha256


def test_promoted_only_lease_api_rejects_abandoned_state(tmp_path):
    harness, _, _, request, attempt = _abandoned(tmp_path)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(LeaseRecoveryRequired):
        harness.leases.acquire_promoted_staged_cleanup(
            REPO_UUID, fixtures.GENERATION_ID, request, attempt_sha256="5" * 64,
            acquired_at=START, monotonic_ns=2_000_000, ttl_ns=1_000_000,
        )
    with pytest.raises(LeaseRecoveryRequired):
        with harness.leases.current_promoted_staged_cleanup(
            attempt.grant, fixtures.GENERATION_ID, request,
            attempt_sha256="5" * 64, monotonic_ns=10_004,
        ):
            pytest.fail("ABANDONED must not enter the promoted-only lane")
    assert tree_snapshot(harness.state_root) == before


def _bound_without_receipt(tmp_path, monkeypatch):
    harness, generations, _, observations = fixtures._runtime(tmp_path)
    staged_request, attempt, completion = fixtures._complete(harness, generations, observations)
    request = _certification(generations, attempt, completion, observations)
    assert generations.semantic_queue is not None
    original = generations.semantic_queue.ensure_certification_binding_locked

    def crash_after_binding(*args, **kwargs):
        original(*args, **kwargs)
        raise InjectedFault("binding installed before receipt")

    monkeypatch.setattr(generations.semantic_queue, "ensure_certification_binding_locked", crash_after_binding)
    with pytest.raises(InjectedFault):
        generations.certify(
            attempt.grant, completion.allocation, request,
            source_observations=observations, declared_entries=completion.entries,
            staged_completion=completion, occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_006,
        )
    assert not (completion.allocation.staging_path / "receipt.json").exists()
    return harness, generations, staged_request, completion


def test_stale_certification_recovers_binding_before_receipt_crash(tmp_path, monkeypatch):
    harness, generations, request, completion = _bound_without_receipt(tmp_path, monkeypatch)
    _restart_owner(harness, monkeypatch)
    recovered = _recover(generations, request, attempt_sha256="9" * 64)
    certified = generations.recover_staged_certification(recovered, monotonic_ns=2_000_001)
    assert certified.lifecycle_state == "CERTIFIED"
    receipt = generations.verify_generation(REPO_UUID, fixtures.GENERATION_ID)
    assert receipt.sha256 == certified.receipt_sha256
    assert receipt.to_dict()["sealed_query_payload"]["manifest_sha256"] == completion.manifest_sha256
    assert receipt.to_dict()["fence_token"] == recovered.grant.lease.to_dict()["fence_token"]


def test_binding_only_recovery_refuses_changed_completed_payload(tmp_path, monkeypatch):
    harness, generations, request, completion = _bound_without_receipt(tmp_path, monkeypatch)
    (completion.allocation.staging_path / "graphify-out" / "graph.json").write_bytes(b"changed")
    recovered = _recover(generations, request, attempt_sha256="9" * 64)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationConflict):
        generations.recover_staged_certification(recovered, monotonic_ns=2_000_001)
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("damage", ["missing", "request_digest"])
def test_binding_only_recovery_requires_exact_immutable_authority(tmp_path, monkeypatch, damage):
    harness, generations, request, _ = _bound_without_receipt(tmp_path, monkeypatch)
    assert generations.semantic_queue is not None
    path = generations.state.path(
        generations.semantic_queue._certification_binding_path(REPO_UUID, fixtures.GENERATION_ID)
    )
    if damage == "missing":
        path.unlink()
    else:
        value = json.loads(path.read_bytes())
        value["request_sha256"] = "f" * 64
        path.write_bytes(canonical_json_bytes(value))
    recovered = _recover(generations, request, attempt_sha256="9" * 64)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(GenerationConflict, match="binding"):
        generations.recover_staged_certification(recovered, monotonic_ns=2_000_001)
    assert tree_snapshot(harness.state_root) == before
