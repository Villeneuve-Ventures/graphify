"""S4 request admission must reject invalid calls before durable staging."""
from dataclasses import replace

import pytest

from graphify.workspace.generations import CapacityExceeded, GenerationConflict
from graphify.workspace.lifecycle_contracts import StructuralBuildRequest
from graphify.workspace.registry import RevisionConflict
from graphify.workspace.sync import (
    StructuralSyncRequest, prepare_structural_sync, synchronize_structural,
)
from tests.test_workspace_structural_s4 import runtime_fixture
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


def prepare(runtime, generation="gen-next", *, watermark=2, epoch=1, size=1024 * 1024):
    return prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id=generation,
        source_epoch=epoch, desired_watermark=watermark, expected_payload_bytes=size)


def test_oversized_reservation_already_refuses_before_durable_staging(tmp_path, monkeypatch):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    before = tree_snapshot(runtime.inputs.state_root)
    source = tree_snapshot(repo)
    request = prepare(runtime, size=runtime.stores.capacity_policy.reserve_bytes + 1)
    with pytest.raises(CapacityExceeded, match="per-generation payload limit"):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert tree_snapshot(runtime.inputs.state_root) == before
    assert tree_snapshot(repo) == source
    assert synchronize_structural(runtime, prepare(runtime), attempt_sha256="b" * 64).pointer_revision == 1


@pytest.mark.parametrize("invalid", ["backward", "epoch", "source"])
def test_preparation_rejects_incompatible_watermark_without_writes(tmp_path, monkeypatch, invalid):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    synchronize_structural(runtime, prepare(runtime, "gen-first"), attempt_sha256="a" * 64)
    if invalid == "source":
        (repo / "README.md").write_text("changed source evidence")
    before = tree_snapshot(runtime.inputs.state_root)
    source = tree_snapshot(repo)
    with pytest.raises(GenerationConflict, match="watermark"):
        prepare(runtime, watermark=1 if invalid == "backward" else 2,
                epoch=2 if invalid == "epoch" else 1)
    assert tree_snapshot(runtime.inputs.state_root) == before
    assert tree_snapshot(repo) == source
    assert synchronize_structural(runtime, prepare(runtime, watermark=3),
                                  attempt_sha256="b" * 64).pointer_revision == 2


@pytest.mark.parametrize("origin", ["prepared_earlier", "deserialized_backward", "deserialized_rebound"])
def test_execution_rechecks_watermark_before_staging(tmp_path, monkeypatch, origin):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    early = prepare(runtime, watermark=1)
    synchronize_structural(runtime, prepare(runtime, "gen-first"), attempt_sha256="a" * 64)
    if origin == "prepared_earlier":
        request = early
    else:
        valid = prepare(runtime, watermark=3)
        build = valid.build.to_dict()
        watermark = 1 if origin == "deserialized_backward" else 2
        if origin == "deserialized_rebound":
            build["source_epoch"] = 2
        build["logical_request_sha256"] = StructuralSyncRequest.identity(
            REPO_UUID, valid.generation_id, watermark, build)
        request = StructuralSyncRequest.from_json(replace(valid, desired_watermark=watermark,
            build=StructuralBuildRequest.from_mapping(build)).canonical)
    before = tree_snapshot(runtime.inputs.state_root)
    source = tree_snapshot(repo)
    with pytest.raises((GenerationConflict, RevisionConflict), match="watermark|operation_epoch"):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    assert tree_snapshot(runtime.inputs.state_root) == before
    assert tree_snapshot(repo) == source
    assert synchronize_structural(runtime, prepare(runtime, watermark=3),
                                  attempt_sha256="c" * 64).pointer_revision == 2


def test_equal_watermark_same_evidence_and_exact_retry_remain_valid(tmp_path, monkeypatch):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    synchronize_structural(runtime, prepare(runtime, "gen-first"), attempt_sha256="a" * 64)
    request = prepare(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    assert result.pointer_revision == 2
    before = tree_snapshot(runtime.inputs.state_root)
    assert synchronize_structural(runtime, request, attempt_sha256="c" * 64) == result
    assert tree_snapshot(runtime.inputs.state_root) == before


@pytest.mark.parametrize("boundary", ["preparation", "execution"])
def test_existing_lease_cannot_advance_queue_between_admission_and_staging(tmp_path, monkeypatch, boundary):
    from datetime import datetime, timezone
    import time
    from graphify.workspace.sync import _observe

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    initial = prepare(runtime, watermark=1)
    build = initial.build
    leases = runtime.stores.leases
    grant = leases.acquire(REPO_UUID, "BUILD", leases.current_owner(),
        expected_registry_revision=build.expected_registry_revision,
        expected_active_source_revision=build.expected_active_source_revision,
        expected_operation_epoch=build.expected_operation_epoch,
        expected_migration_epoch=build.expected_migration_epoch,
        acquired_at=datetime.now(timezone.utc), monotonic_ns=time.monotonic_ns(), ttl_ns=10**12)
    # A caller can reconstruct a valid request with the already-held lease's
    # epoch. Releasing that lease does not advance the epoch used for staging CAS.
    value = build.to_dict()
    value["expected_operation_epoch"] = grant.operation_epoch
    value["logical_request_sha256"] = StructuralSyncRequest.identity(
        REPO_UUID, initial.generation_id, 1, value)
    request = replace(initial, build=StructuralBuildRequest.from_mapping(value))
    original = runtime.stores.generations.request_staged_build
    advanced = []
    def advance_before_staging(*args, **kwargs):
        _, observations = _observe(runtime, REPO_UUID)
        runtime.stores.queue.reconcile(grant, (), source_epoch=1,
            policy_sha256=build.policy_sha256, source_observations=observations,
            desired_watermark=2, semantic_required=False, monotonic_ns=time.monotonic_ns())
        leases.release(grant)
        advanced.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.stores.generations, "request_staged_build", advance_before_staging)
    before = tree_snapshot(runtime.inputs.state_root)
    source = tree_snapshot(repo)
    with pytest.raises(GenerationConflict, match="active lease"):
        if boundary == "preparation":
            prepare(runtime, watermark=1)
        else:
            synchronize_structural(runtime, request, attempt_sha256="d" * 64)
    assert not advanced
    assert tree_snapshot(runtime.inputs.state_root) == before
    assert tree_snapshot(repo) == source
    monkeypatch.setattr(runtime.stores.generations, "request_staged_build", original)
    leases.release(grant)
    assert synchronize_structural(runtime, prepare(runtime, watermark=1),
                                  attempt_sha256="e" * 64).pointer_revision == 1
