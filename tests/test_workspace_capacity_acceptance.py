"""Composed stores enforce the operator's per-generation reservation bound."""

from datetime import timedelta

import pytest

from graphify.workspace.composition import (
    StructuralPolicy, WorkspaceRuntimeAuthority, WorkspaceRuntimeInputs,
    compose_workspace_runtime,
)
from graphify.workspace.generations import CapacityExceeded, StructuralBuildRequest
from graphify.workspace.persistence import RuntimeCapabilities
from tests import test_workspace_lifecycle_s3 as fixtures
from tests.test_workspace_generation_recovery_review import _bound_without_receipt, _recover
from tests.workspace_s3_helpers import (
    COMPATIBILITY_MANIFEST, REPO_UUID, START, tree_snapshot, trust_source_observations,
)


LIMIT = 1024 * 1024
GENERATION = "gen-payload-limit"


def _composed(harness, observations, monkeypatch, *, limit=LIMIT):
    monkeypatch.setattr(
        RuntimeCapabilities, "detect",
        classmethod(lambda cls, path: harness.leases.state.capabilities),
    )
    authority = WorkspaceRuntimeAuthority.from_mapping({
        "contract": "graphify.workspace.runtime_authority.internal",
        "format_version": 2,
        "compatibility_manifest": COMPATIBILITY_MANIFEST.to_dict(),
        "structural_policy": StructuralPolicy(8, 16384, 1, 4, limit).to_dict(),
    })
    stores = compose_workspace_runtime(WorkspaceRuntimeInputs(
        harness.state_root, authority, COMPATIBILITY_MANIFEST,
    )).require_lifecycle_stores()
    trust_source_observations(stores.generations, observations)
    return stores


def _request(harness, observations, policy, size):
    value = fixtures._request(harness, observations).to_dict()
    value.update(expected_payload_bytes=size, capacity_policy_sha256=policy.sha256)
    return StructuralBuildRequest.from_mapping(value)


def _allocate(generations, request, observations, policy):
    generations.request_staged_build(
        REPO_UUID, GENERATION, request, source_observations=observations,
    )
    attempt = generations.acquire_staged_operation(
        REPO_UUID, GENERATION, request, attempt_sha256="5" * 64,
        operation="BUILD", acquired_at=START + timedelta(seconds=1),
        monotonic_ns=10_000, ttl_ns=1_000_000,
    )
    allocation = generations.allocate(
        attempt.grant, expected_payload_bytes=request.expected_payload_bytes,
        capacity_policy=policy, generation_id=GENERATION,
        occurred_at=START + timedelta(seconds=1), monotonic_ns=10_001,
    )
    return attempt, allocation


@pytest.mark.parametrize("size", [LIMIT + 1, 3 * LIMIT])
def test_composition_rejects_oversized_request_without_state_change(tmp_path, monkeypatch, size):
    harness, _, _, observations = fixtures._runtime(tmp_path)
    stores = _composed(harness, observations, monkeypatch)
    request = _request(harness, observations, stores.capacity_policy, size)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(CapacityExceeded, match="per-generation"):
        stores.generations.request_staged_build(
            REPO_UUID, GENERATION, request, source_observations=observations,
        )
    assert tree_snapshot(harness.state_root) == before


def test_stale_certification_recovery_enforces_composed_limit(tmp_path, monkeypatch):
    harness, _, request, completion = _bound_without_receipt(tmp_path, monkeypatch)
    stores = _composed(harness, fixtures._observations(harness.repo), monkeypatch, limit=1)
    recovered = _recover(stores.generations, request)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(CapacityExceeded, match="per-generation"):
        stores.generations.recover_staged_certification(recovered, monotonic_ns=2_000_001)
    assert tree_snapshot(harness.state_root) == before
    assert not (completion.allocation.staging_path / "receipt.json").exists()


def test_composition_accepts_exact_reservation_limit(tmp_path, monkeypatch):
    harness, _, _, observations = fixtures._runtime(tmp_path)
    stores = _composed(harness, observations, monkeypatch)
    request = _request(harness, observations, stores.capacity_policy, LIMIT)
    _, allocation = _allocate(stores.generations, request, observations, stores.capacity_policy)
    assert allocation.expected_payload_bytes == LIMIT


@pytest.mark.parametrize("operation", ["allocate", "complete"])
def test_composed_store_refuses_existing_oversized_allocation(tmp_path, monkeypatch, operation):
    harness, generations, _, observations = fixtures._runtime(tmp_path)
    request = _request(harness, observations, fixtures.POLICY, 3 * LIMIT)
    attempt, allocation = _allocate(generations, request, observations, fixtures.POLICY)
    preparation = generations.prepare_staged_build(attempt, allocation, monotonic_ns=10_002)
    payload = preparation.staging_path / "graphify-out"
    payload.mkdir(mode=0o700)
    (payload / "graph.json").write_bytes(b" " * (2 * LIMIT) + b"{}\n")
    (payload / "input-manifest.json").write_bytes(observations[0].consumed_inputs.canonical)
    stores = _composed(harness, observations, monkeypatch)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(CapacityExceeded, match="per-generation"):
        if operation == "allocate":
            stores.generations.allocate(
                attempt.grant, expected_payload_bytes=request.expected_payload_bytes,
                capacity_policy=fixtures.POLICY, generation_id=GENERATION,
                occurred_at=START, monotonic_ns=10_003,
            )
        else:
            stores.generations.complete_staged_build(
                preparation, source_observations=observations, monotonic_ns=10_003,
            )
    assert tree_snapshot(harness.state_root) == before
