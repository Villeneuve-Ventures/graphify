"""P5B2 internal semantic-generation certification finalization coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import pytest

import graphify.workspace.sync as workspace_sync
from graphify.workspace.contracts import StagedBuildState, WorkspaceLeaseState
from graphify.workspace.generations import GenerationConflict, GenerationError
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from graphify.workspace.semantic_handoff import SemanticHandoffConflict
from graphify.workspace.semantic_queue import (
    SemanticCertificationBlocked,
    SemanticDesiredWork,
    SemanticQueueStore,
)
from tests.test_workspace_semantic_result_handoff import (
    _ArmedFault,
    _fresh_ready_runtime,
    _handoff_path,
)
from tests.test_workspace_sync import _replace_request
from tests.workspace_p3_helpers import REPO_UUID, RuntimeHarness, tree_snapshot


GENERATION_ID = "gen-semantic-certification"


def _complete_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fault_hook: Any = None,
):
    harness, runtime, request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
        generation_id=GENERATION_ID,
        fault_hook=fault_hook,
    )
    handoff_proof = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "COMPLETE"
    return harness, runtime, request, handoff_proof, staged


def _workspace_root(harness: RuntimeHarness) -> Path:
    return harness.state_root / "workspaces" / REPO_UUID


def _binding_path(harness: RuntimeHarness) -> Path:
    return (
        _workspace_root(harness)
        / "queue"
        / "certifications"
        / f"{GENERATION_ID}.json"
    )


def _forbidden(name: str):
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"forbidden certification-finalization call: {name}")

    return fail


def test_exact_complete_advances_to_certified_and_terminal_replay_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    owner = runtime.semantic_handoffs
    assert owner is not None
    handoff_path = _handoff_path(harness, runtime, request)
    handoff_bytes = handoff_path.read_bytes()
    staging = _workspace_root(harness) / "staging" / GENERATION_ID
    staging_before = tree_snapshot(staging)
    pointer_before = runtime.pointers.load(REPO_UUID, allow_missing=True)

    monkeypatch.setattr(
        runtime.generations,
        "request_staged_build",
        _forbidden("request_staged_build"),
    )
    monkeypatch.setattr(
        runtime.generations,
        "acquire_staged_operation",
        _forbidden("acquire_staged_operation"),
    )
    monkeypatch.setattr(
        runtime.generations.adapter,
        "build_structural",
        _forbidden("adapter.build_structural"),
    )
    monkeypatch.setattr(
        owner,
        "install_generation_copy",
        _forbidden("install_generation_copy"),
    )

    acquired: list[Any] = []
    real_acquire = runtime.generations.acquire_staged_recovery

    def acquire(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        assert attempt.grant.lease.to_dict()["operation"] == "BUILD"
        assert attempt.state.canonical == complete.canonical
        acquired.append(attempt)
        return attempt

    monkeypatch.setattr(runtime.generations, "acquire_staged_recovery", acquire)

    views: list[Any] = []
    real_view = runtime.semantic_queue.certification_view

    def certification_view(*args: Any, **kwargs: Any):
        view = real_view(*args, **kwargs)
        views.append(view)
        return view

    monkeypatch.setattr(runtime.semantic_queue, "certification_view", certification_view)

    binding_installed = False
    real_binding = runtime.semantic_queue.ensure_certification_binding_locked

    def ensure_binding(*args: Any, **kwargs: Any):
        nonlocal binding_installed
        result = real_binding(*args, **kwargs)
        binding_installed = True
        return result

    monkeypatch.setattr(
        runtime.semantic_queue,
        "ensure_certification_binding_locked",
        ensure_binding,
    )
    real_certify_locked = runtime.generations._certify_locked

    def certify_locked(*args: Any, **kwargs: Any):
        assert binding_installed
        return real_certify_locked(*args, **kwargs)

    monkeypatch.setattr(runtime.generations, "_certify_locked", certify_locked)

    certification_calls: list[tuple[Any, tuple[Any, ...]]] = []
    real_certify = runtime.generations.certify

    def certify(grant: Any, allocation: Any, certification_request: Any, **kwargs: Any):
        current = runtime.generations._load_staged_build_locked(REPO_UUID)
        assert current is not None
        assert current.canonical == complete.canonical
        assert tree_snapshot(staging) == staging_before
        declared_entries = tuple(kwargs["declared_entries"])
        certification_calls.append((certification_request, declared_entries))
        return real_certify(
            grant,
            allocation,
            certification_request,
            **kwargs,
        )

    monkeypatch.setattr(runtime.generations, "certify", certify)

    proof = workspace_sync._finalize_semantic_generation_certification(
        runtime,
        request,
    )

    assert len(acquired) == 1
    assert len(certification_calls) == 1
    assert len(views) == 2
    assert views[0] == views[1]
    view = views[0]
    certification_request, declared_entries = certification_calls[0]
    assert certification_request.source_commit == view.source_commit
    assert certification_request.source_epoch == view.source_epoch
    assert certification_request.policy_sha256 == view.policy_sha256
    assert (
        certification_request.observation_manifest_sha256
        == view.observation_manifest_sha256
    )
    assert certification_request.queue_watermark == view.queue_watermark
    assert certification_request.semantic_completeness == "complete"
    assert certification_request.compatibility_sha256 == complete.request.compatibility_sha256
    assert certification_request.validations == (
        "coordination_lock_precreated",
        "payload_manifest",
        "stable_semantic_queue",
    )

    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    assert certified.lifecycle_state == "CERTIFIED"
    assert certified.revision == complete.revision + 1
    assert certified.request.canonical == complete.request.canonical
    assert certified.payload_manifest_sha256 == complete.payload_manifest_sha256
    assert certified.pointer_revision is None
    assert certified.abandonment_intent is None
    assert certified.receipt_sha256 == proof.receipt_sha256

    receipt = runtime.generations.verify_generation(REPO_UUID, GENERATION_ID)
    receipt_value = receipt.to_dict()
    assert receipt.sha256 == proof.receipt_sha256
    assert receipt_value["semantic_completeness"] == "complete"
    assert receipt_value["validations"] == [
        "coordination_lock_precreated",
        "payload_manifest",
        "stable_semantic_queue",
    ]
    assert receipt_value["operation_epoch"] == acquired[0].grant.operation_epoch
    assert receipt_value["fence_token"] == acquired[0].grant.lease.to_dict()["fence_token"]
    assert complete.operation_epoch is not None
    assert complete.fence_token is not None
    assert receipt_value["operation_epoch"] == complete.operation_epoch + 1
    assert receipt_value["fence_token"] == complete.fence_token + 1
    assert tuple(receipt_value["sealed_query_payload"]["entries"]) == declared_entries

    request_sha256 = runtime.generations._semantic_request_sha256(certification_request)
    binding_view = SemanticQueueStore.verify_certification_binding_at(
        runtime.semantic_queue.state,
        REPO_UUID,
        generation_id=GENERATION_ID,
        request_sha256=request_sha256,
        sealed_input_manifest_sha256=proof.payload_manifest_sha256,
    )
    assert binding_view == view
    assert proof.repo_uuid == REPO_UUID
    assert proof.target_generation_id == GENERATION_ID
    assert proof.request_sha256 == request.sha256
    assert proof.handoff_sha256 == handoff_proof.handoff_sha256
    assert proof.payload_manifest_sha256 == handoff_proof.payload_manifest_sha256
    assert proof.certification_request_sha256 == request_sha256
    assert proof.queue_revision == view.queue_revision
    assert proof.queue_sha256 == view.queue_state_sha256
    assert proof.staged_revision == certified.revision

    final = _workspace_root(harness) / "generations" / GENERATION_ID
    assert not staging.exists()
    assert final.is_dir()
    assert handoff_path.read_bytes() == handoff_bytes
    assert (final / "graphify-out" / "semantic-inputs.json").read_bytes() == handoff_bytes

    capacity = json.loads((harness.state_root / "capacity.json").read_text(encoding="utf-8"))
    assert all(
        (item["repo_uuid"], item["generation_id"]) != (REPO_UUID, GENERATION_ID)
        for item in capacity["reservations"]
    )
    projection = runtime.journal.project_recovery(REPO_UUID)
    assert projection.actions == ()
    target_events = [event.to_dict() for event in projection.snapshot.for_generation(GENERATION_ID)]
    assert target_events[-1]["transition"] == "CERTIFIED"
    assert target_events[-1]["receipt_sha256"] == receipt.sha256
    assert target_events[-1]["pointer_revision"] == 0
    assert all(event["transition"] != "PROMOTED" for event in target_events)
    assert runtime.pointers.load(REPO_UUID, allow_missing=True) == pointer_before
    lease_state = runtime.leases.inspect(REPO_UUID)
    assert lease_state.leases.get("workspace") is None
    assert lease_state.staged_attempt_sha256 is None

    state_before_replay = tree_snapshot(harness.state_root)
    monkeypatch.setattr(
        runtime.generations,
        "acquire_staged_recovery",
        _forbidden("terminal replay lease acquisition"),
    )
    replay = workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert replay == proof
    assert tree_snapshot(harness.state_root) == state_before_replay


def test_certification_finalization_bounds_reopen_and_post_install_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, _handoff_proof, _complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    owner = runtime.semantic_handoffs
    assert owner is not None

    reopen_calls: list[tuple[int, int | None]] = []
    real_reopen = owner._reopen_for_certification

    def reopen(*args: Any, deadline_ns: int | None = None, **kwargs: Any):
        reopen_calls.append((time.monotonic_ns(), deadline_ns))
        return real_reopen(*args, deadline_ns=deadline_ns, **kwargs)

    monkeypatch.setattr(owner, "_reopen_for_certification", reopen)

    certify_returned = False
    real_certify = runtime.generations.certify

    def certify(*args: Any, **kwargs: Any):
        nonlocal certify_returned
        result = real_certify(*args, **kwargs)
        certify_returned = True
        return result

    monkeypatch.setattr(runtime.generations, "certify", certify)

    verification_calls: list[tuple[int, int | None]] = []
    real_verify = runtime.generations.verify_generation

    def verify(*args: Any, deadline_ns: int | None = None, **kwargs: Any):
        if certify_returned:
            verification_calls.append((time.monotonic_ns(), deadline_ns))
        return real_verify(*args, deadline_ns=deadline_ns, **kwargs)

    monkeypatch.setattr(runtime.generations, "verify_generation", verify)

    workspace_sync._finalize_semantic_generation_certification(runtime, request)

    for calls in (reopen_calls, verification_calls):
        assert calls
        for called_ns, deadline_ns in calls:
            assert deadline_ns is not None
            assert 0 < deadline_ns - called_ns <= workspace_sync._SYNC_READ_TIMEOUT_NS


def test_certification_reopen_propagates_deadline_to_semantic_input_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    owner = runtime.semantic_handoffs
    assert owner is not None

    semantic_input_deadlines: list[int | None] = []
    real_read = owner.state._read_regular_descriptor

    def read_regular_descriptor(
        descriptor: int,
        path: Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        if path.name == "semantic-inputs.json":
            semantic_input_deadlines.append(deadline_ns)
        return real_read(
            descriptor,
            path,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(owner.state, "_read_regular_descriptor", read_regular_descriptor)
    deadline_ns = time.monotonic_ns() + workspace_sync._SYNC_READ_TIMEOUT_NS

    reopened = owner._reopen_for_certification(
        request,
        complete.request,
        deadline_ns=deadline_ns,
    )

    assert reopened.sha256 == handoff_proof.handoff_sha256
    assert semantic_input_deadlines == [deadline_ns]


def test_certification_entry_propagates_deadline_to_completion_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, _handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )

    inventory_deadlines: list[int] = []
    real_inventory = runtime.generations._inventory

    def inventory(*args: Any, deadline_ns: int | None = None, **kwargs: Any):
        assert deadline_ns is not None
        inventory_deadlines.append(deadline_ns)
        return real_inventory(*args, deadline_ns=deadline_ns, **kwargs)

    monkeypatch.setattr(runtime.generations, "_inventory", inventory)

    reuse_calls: list[tuple[int, int]] = []
    real_reuse = runtime.generations._reuse_staged_completion_locked

    def reuse(*args: Any, deadline_ns: int | None = None, **kwargs: Any):
        assert deadline_ns is not None
        reuse_calls.append((time.monotonic_ns(), deadline_ns))
        return real_reuse(*args, deadline_ns=deadline_ns, **kwargs)

    monkeypatch.setattr(runtime.generations, "_reuse_staged_completion_locked", reuse)

    entry = workspace_sync._capture_semantic_certification_entry(
        runtime,
        request,
        complete,
        source_observations=None,
    )

    assert entry.staged.canonical == complete.canonical
    assert len(reuse_calls) == 1
    called_ns, deadline_ns = reuse_calls[0]
    assert 0 < deadline_ns - called_ns <= workspace_sync._SYNC_READ_TIMEOUT_NS
    assert inventory_deadlines == [deadline_ns]


def test_mismatched_and_ambiguous_staged_entry_fail_before_recovery_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, _handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    before = tree_snapshot(harness.state_root)
    monkeypatch.setattr(
        runtime.generations,
        "acquire_staged_recovery",
        _forbidden("mismatched entry recovery"),
    )
    mismatched = _replace_request(request, generation_id="gen-semantic-certification-other")
    with pytest.raises((GenerationConflict, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_certification(runtime, mismatched)
    assert tree_snapshot(harness.state_root) == before

    previous = _workspace_root(harness) / "staged-build.previous.json"
    assert complete.operation_epoch is not None
    assert complete.fence_token is not None
    divergent = StagedBuildState.from_mapping(
        replace(
            complete,
            operation_epoch=complete.operation_epoch + 1,
            fence_token=complete.fence_token + 1,
        ).to_dict()
    )
    previous.write_bytes(divergent.canonical)
    with pytest.raises(GenerationError, match="staged build state is corrupt"):
        workspace_sync._finalize_semantic_generation_certification(runtime, request)
    assert runtime.leases.inspect(REPO_UUID).leases.get("workspace") is None


def test_unbound_complete_in_final_location_fails_before_recovery_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, _handoff_proof, _complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    workspace = _workspace_root(harness)
    staging = workspace / "staging" / GENERATION_ID
    final = workspace / "generations" / GENERATION_ID
    final.parent.mkdir(mode=0o700)
    staging.rename(final)
    before = tree_snapshot(harness.state_root)
    monkeypatch.setattr(
        runtime.generations,
        "acquire_staged_recovery",
        _forbidden("unbound final-location recovery"),
    )

    with pytest.raises((GenerationConflict, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert tree_snapshot(harness.state_root) == before
    assert runtime.leases.inspect(REPO_UUID).leases.get("workspace") is None


def test_prebinding_queue_drift_fails_closed_and_releases_exact_recovery_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, _handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    queue = runtime.semantic_queue.inspect(REPO_UUID)
    assert queue.reconciliation is not None
    work = queue.reconciliation.desired[0]
    drifted = SemanticDesiredWork(
        source_epoch=work.source_epoch,
        policy_sha256=work.policy_sha256,
        operation=work.operation,
        path=work.path,
        content_sha256=hashlib.sha256(b"post-acquisition drift").hexdigest(),
        desired_revision=queue.desired_watermark + 1,
    )
    real_acquire = runtime.generations.acquire_staged_recovery

    def acquire_with_drift(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        runtime.semantic_queue.enqueue(
            attempt.grant,
            drifted,
            monotonic_ns=time.monotonic_ns(),
        )
        return attempt

    monkeypatch.setattr(runtime.generations, "acquire_staged_recovery", acquire_with_drift)

    with pytest.raises(
        (GenerationConflict, SemanticCertificationBlocked, SemanticHandoffConflict)
    ):
        workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert not _binding_path(harness).exists()
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.canonical == complete.canonical
    lease_state = runtime.leases.inspect(REPO_UUID)
    assert lease_state.leases.get("workspace") is None
    assert lease_state.staged_attempt_sha256 is None


def test_recovery_acquisition_commit_unknown_reuses_one_attempt_and_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, _handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    real_acquire = runtime.generations.acquire_staged_recovery
    calls: list[tuple[str, Any]] = []

    def acquire_then_unknown(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        calls.append((kwargs["attempt_sha256"], attempt.grant))
        if len(calls) == 1:
            raise CommitUnknown("certification recovery acquisition acknowledgement was lost")
        return attempt

    monkeypatch.setattr(
        runtime.generations,
        "acquire_staged_recovery",
        acquire_then_unknown,
    )

    proof = workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert proof.target_generation_id == GENERATION_ID
    assert len(calls) == 2
    assert calls[0] == calls[1]
    receipt = runtime.generations.verify_generation(REPO_UUID, GENERATION_ID)
    receipt_value = receipt.to_dict()
    assert complete.operation_epoch is not None
    assert complete.fence_token is not None
    assert receipt_value["operation_epoch"] == complete.operation_epoch + 1
    assert receipt_value["fence_token"] == complete.fence_token + 1


def test_durable_binding_recovers_exact_view_after_source_and_queue_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault(f"semantic_certification:{GENERATION_ID}:installed")
    harness, runtime, request, _handoff_proof, complete = _complete_handoff(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    fault.armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert fault.fired
    assert _binding_path(harness).is_file()
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.canonical == complete.canonical

    attempt = runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        complete.request,
        attempt_sha256=hashlib.sha256(b"post-binding-drift").hexdigest(),
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    queue = runtime.semantic_queue.inspect(REPO_UUID)
    assert queue.reconciliation is not None
    work = queue.reconciliation.desired[0]
    runtime.semantic_queue.enqueue(
        attempt.grant,
        SemanticDesiredWork(
            source_epoch=work.source_epoch,
            policy_sha256=work.policy_sha256,
            operation=work.operation,
            path=work.path,
            content_sha256=hashlib.sha256(b"bound queue drift").hexdigest(),
            desired_revision=queue.desired_watermark + 1,
        ),
        monotonic_ns=time.monotonic_ns(),
    )
    runtime.leases.release(attempt.grant)
    (harness.repo / "README.md").write_text("source changed after binding\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime.generations.adapter,
        "observe",
        _forbidden("source observation after durable binding"),
    )

    proof = workspace_sync._finalize_semantic_generation_certification(runtime, request)

    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    assert certified.lifecycle_state == "CERTIFIED"
    assert certified.receipt_sha256 == proof.receipt_sha256
    assert runtime.semantic_queue.inspect(REPO_UUID).desired_watermark == (
        queue.desired_watermark + 1
    )
    assert runtime.leases.inspect(REPO_UUID).leases.get("workspace") is None


@pytest.mark.parametrize(
    "boundary",
    [
        f"semantic_certification:{GENERATION_ID}:installed",
        f"generation:{GENERATION_ID}:receipt:installed",
        f"generation:{GENERATION_ID}:installed",
        "journal:CERTIFIED:head_durable",
        f"generation:{GENERATION_ID}:capacity_released",
        f"generation:{GENERATION_ID}:staged_certified_durable",
    ],
)
def test_certification_commit_uncertainty_recovers_without_destructive_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    fault = _ArmedFault(boundary)
    harness, runtime, request, _handoff_proof, _complete = _complete_handoff(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    handoff = _handoff_path(harness, runtime, request)
    handoff_bytes = handoff.read_bytes()
    fault.armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        workspace_sync._finalize_semantic_generation_certification(runtime, request)
    assert fault.fired

    proof = workspace_sync._finalize_semantic_generation_certification(runtime, request)

    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "CERTIFIED"
    assert staged.receipt_sha256 == proof.receipt_sha256
    assert handoff.read_bytes() == handoff_bytes
    assert runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    assert runtime.leases.inspect(REPO_UUID).leases.get("workspace") is None


def test_release_commit_unknown_is_adopted_only_after_exact_absence_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, _handoff_proof, _complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    real_release = runtime.leases.release
    release_calls = 0

    def release_then_unknown(grant: Any, **kwargs: Any):
        nonlocal release_calls
        release_calls += 1
        result = real_release(grant, **kwargs)
        if release_calls == 1:
            raise CommitUnknown("certification release acknowledgement was lost")
        return result

    monkeypatch.setattr(runtime.leases, "release", release_then_unknown)

    proof = workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert proof.target_generation_id == GENERATION_ID
    assert release_calls == 1
    lease_state = runtime.leases.inspect(REPO_UUID)
    assert lease_state.leases.get("workspace") is None
    assert lease_state.staged_attempt_sha256 is None


def test_release_commit_unknown_retries_only_the_exact_unchanged_live_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, _handoff_proof, _complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    real_release = runtime.leases.release
    release_calls = 0
    released_grants: list[Any] = []

    def unknown_then_release(grant: Any, **kwargs: Any):
        nonlocal release_calls
        release_calls += 1
        released_grants.append(grant)
        if release_calls == 1:
            raise CommitUnknown("certification release outcome is unknown before commit")
        return real_release(grant, **kwargs)

    monkeypatch.setattr(runtime.leases, "release", unknown_then_release)

    proof = workspace_sync._finalize_semantic_generation_certification(runtime, request)

    assert proof.target_generation_id == GENERATION_ID
    assert release_calls == 2
    assert released_grants[0] == released_grants[1]
    lease_state = runtime.leases.inspect(REPO_UUID)
    assert lease_state.leases.get("workspace") is None
    assert lease_state.staged_attempt_sha256 is None


def test_release_commit_unknown_rejects_absence_after_replacement_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, _handoff_proof, _complete = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    real_release = runtime.leases.release

    def release_then_replace(grant: Any, **kwargs: Any):
        released = real_release(grant, **kwargs)
        advanced = WorkspaceLeaseState(
            repo_uuid=released.repo_uuid,
            revision=released.revision + 1,
            fence_high_watermark=released.fence_high_watermark + 1,
            operation_epoch=released.operation_epoch + 1,
            migration_epoch=released.migration_epoch,
            leases=dict(released.leases),
            lease_epochs=dict(released.lease_epochs),
            staged_attempt_sha256=released.staged_attempt_sha256,
        )
        with runtime.registry.recovered_snapshot():
            with runtime.leases.workspace_lock(REPO_UUID):
                runtime.leases._commit_state_locked(advanced)
        raise CommitUnknown("injected release acknowledgement loss")

    monkeypatch.setattr(runtime.leases, "release", release_then_replace)

    with pytest.raises(
        CommitUnknown,
        match="recovery lease absence follows replacement authority",
    ):
        workspace_sync._finalize_semantic_generation_certification(runtime, request)

    lease_state = runtime.leases.inspect(REPO_UUID)
    assert lease_state.leases.get("workspace") is None
    assert lease_state.staged_attempt_sha256 is None
