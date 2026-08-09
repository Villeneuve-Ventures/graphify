"""P5B2 internal semantic-generation promotion finalization coverage."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, cast

import pytest

import graphify.workspace.sync as workspace_sync
from graphify.workspace.composition import WorkspaceRuntime
from graphify.workspace.contracts import (
    FencedLease,
    GenerationReceipt,
    JournalEvent,
    PointerSet,
    StagedBuildState,
    StructuralBuildRequest,
    WorkspaceLeaseState,
    canonical_json_bytes,
    encode_journal_frame,
)
from graphify.workspace.generations import (
    GenerationConflict,
    GenerationError,
    StagedBuildReadRecoveryRequired,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.journal import JournalCorrupt
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseGrant,
    LeaseRecoveryRequired,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    StateCorrupt,
    StatePathError,
    StateRecoveryRequired,
)
from graphify.workspace.pointers import PointerCAS
from graphify.workspace.semantic_handoff import (
    CarriedSemanticResultEvidence,
    SemanticHandoffConflict,
)
from graphify.workspace.sync import SyncRequest
from tests.test_workspace_semantic_generation_certification_finalization import (
    GENERATION_ID,
    _complete_handoff,
)
from tests.test_workspace_semantic_result_handoff import (
    _ArmedFault,
    _carried_ready_runtime,
    _handoff_path,
)
from tests.test_workspace_staged_build_abandonment import (
    _advance_active_source_revision,
)
from tests.test_workspace_sync import _compose
from tests.workspace_p3_helpers import (
    REPO_UUID,
    RuntimeHarness,
    authorization,
    create_repo,
    tree_snapshot,
)


_WORKSPACE_COMMIT_BOUNDARIES = (
    "workspace:pending_durable",
    "workspace:previous_durable",
    "workspace:current_replaced",
    "workspace:current_durable",
    "workspace:pending_cleared",
)


@dataclass(frozen=True)
class _PromotedCleanupCase:
    harness: RuntimeHarness
    runtime: WorkspaceRuntime
    request: SyncRequest
    structural_request: StructuralBuildRequest
    promoted: StagedBuildState
    attempt_sha256: str
    retained_grant: LeaseGrant


@dataclass(frozen=True)
class _CertifiedPromotionCase:
    harness: RuntimeHarness
    runtime: WorkspaceRuntime
    request: SyncRequest
    certified: StagedBuildState
    receipt: GenerationReceipt


def _certified_promotion_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fault_hook: Any = None,
) -> _CertifiedPromotionCase:
    harness, runtime, request, _, _ = _complete_handoff(
        tmp_path,
        monkeypatch,
        fault_hook=fault_hook,
    )
    workspace_sync._finalize_semantic_generation_certification(runtime, request)
    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    assert certified.lifecycle_state == "CERTIFIED"
    receipt = runtime.generations.verify_generation(REPO_UUID, GENERATION_ID)
    assert receipt.sha256 == certified.receipt_sha256
    return _CertifiedPromotionCase(
        harness=harness,
        runtime=runtime,
        request=request,
        certified=certified,
        receipt=receipt,
    )


def _carried_certified_promotion_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _CertifiedPromotionCase:
    harness, runtime, _first, request, work, _pointer = _carried_ready_runtime(
        tmp_path,
        monkeypatch,
    )
    workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (CarriedSemanticResultEvidence(work),),
    )
    workspace_sync._finalize_semantic_generation_certification(runtime, request)
    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    assert certified.lifecycle_state == "CERTIFIED"
    receipt = runtime.generations.verify_generation(REPO_UUID, request.generation_id)
    assert receipt.sha256 == certified.receipt_sha256
    return _CertifiedPromotionCase(
        harness=harness,
        runtime=runtime,
        request=request,
        certified=certified,
        receipt=receipt,
    )


def _semantic_evidence(case: _CertifiedPromotionCase) -> dict[str, Any]:
    workspace = _workspace_root(case)
    handoff = (
        workspace
        / "semantic-staging"
        / "handoffs"
        / case.request.generation_id
        / f"{case.certified.request.sha256}.json"
    )
    return {
        "generation": tree_snapshot(
            workspace / "generations" / case.request.generation_id
        ),
        "handoff": handoff.read_bytes(),
        "binding": _binding_path(case).read_bytes(),
        "receipt": case.runtime.generations.verify_generation(
            REPO_UUID,
            case.request.generation_id,
        ).canonical,
    }


def _forbidden_finalization(name: str):
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"forbidden promotion-finalization call: {name}")

    return fail


def _leave_promotion_grant_after_process_death(
    case: _CertifiedPromotionCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def process_died_before_release(*_args: Any, **_kwargs: Any) -> None:
        raise CommitUnknown("promotion process died before grant release")

    with monkeypatch.context() as patch:
        patch.setattr(
            workspace_sync,
            "_release_semantic_promotion_grant",
            process_died_before_release,
        )
        with pytest.raises(CommitUnknown, match="process died"):
            workspace_sync._finalize_semantic_generation_promotion(
                case.runtime,
                case.request,
            )


def _leave_promotion_acquisition_pending_after_process_death(
    case: _CertifiedPromotionCase,
    monkeypatch: pytest.MonkeyPatch,
    fault: _ArmedFault,
) -> Callable[..., Any]:
    acquire = case.runtime.generations.acquire_staged_recovery

    def interrupt(*args: Any, **kwargs: Any):
        try:
            return acquire(*args, **kwargs)
        except CommitUnknown as exc:
            raise InjectedFault("process died during acquisition") from exc

    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        interrupt,
    )
    fault.armed = True
    with pytest.raises(InjectedFault, match="process died"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )
    return acquire


def _assert_promotion_terminal(
    case: _CertifiedPromotionCase,
    proof: workspace_sync._SemanticGenerationPromotionFinalization,
) -> None:
    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "PROMOTED"
    assert staged.revision == case.certified.revision + 1
    assert staged.request.canonical == case.certified.request.canonical
    assert staged.payload_manifest_sha256 == case.certified.payload_manifest_sha256
    assert staged.receipt_sha256 == case.certified.receipt_sha256
    assert staged.pointer_revision is not None
    pointer = case.runtime.pointers.load(REPO_UUID)
    assert pointer is not None
    pointer_value = pointer.to_dict()
    current = cast(dict[str, Any], pointer_value["current"])
    assert current == {
        "generation_id": case.request.generation_id,
        "receipt_sha256": case.receipt.sha256,
    }
    assert pointer_value["pointer_revision"] == staged.pointer_revision
    assert pointer_value["operation_epoch"] == staged.operation_epoch
    assert pointer_value["fence_token"] == staged.fence_token
    events = [
        event.to_dict()
        for event in case.runtime.journal.project_recovery(REPO_UUID).snapshot.for_generation(
            case.request.generation_id
        )
    ]
    matching = [
        event
        for event in events
        if event["transition"] in {"PROMOTED", "REPAIRED"}
        and event["receipt_sha256"] == case.receipt.sha256
        and event["pointer_revision"] == staged.pointer_revision
        and event["operation_epoch"] == staged.operation_epoch
        and event["fence_token"] == staged.fence_token
    ]
    assert len(matching) == 1
    assert events[-1] == matching[0]
    assert proof.repo_uuid == REPO_UUID
    assert proof.target_generation_id == case.request.generation_id
    assert proof.request_sha256 == case.request.sha256
    assert proof.payload_manifest_sha256 == case.certified.payload_manifest_sha256
    assert proof.receipt_sha256 == case.receipt.sha256
    assert proof.staged_revision == staged.revision
    assert proof.pointer_revision == staged.pointer_revision
    assert proof.pointer_operation_epoch == staged.operation_epoch
    assert proof.pointer_fence_token == staged.fence_token
    assert proof.journal_transition == matching[0]["transition"]
    lease_state = case.runtime.leases.inspect(REPO_UUID)
    assert lease_state.leases.get("workspace") is None
    assert lease_state.staged_attempt_sha256 is None


def _workspace_root(case: _PromotedCleanupCase | _CertifiedPromotionCase) -> Path:
    return case.harness.state_root / "workspaces" / REPO_UUID


def _binding_path(case: _PromotedCleanupCase | _CertifiedPromotionCase) -> Path:
    return (
        _workspace_root(case)
        / "queue"
        / "certifications"
        / f"{case.request.generation_id}.json"
    )


def _retained_grant(runtime: WorkspaceRuntime) -> LeaseGrant:
    state = runtime.leases.inspect(REPO_UUID)
    lease = state.leases.get("workspace")
    lease_epoch = state.lease_epochs.get("workspace")
    assert lease is not None
    assert lease_epoch is not None
    return LeaseGrant(
        lease=lease,
        registry_revision=1,
        active_source_revision=1,
        operation_epoch=lease_epoch,
        migration_epoch=state.migration_epoch,
    )


def _advance_visible_pointer(
    case: _CertifiedPromotionCase,
    grant: LeaseGrant,
    receipt: GenerationReceipt,
) -> PointerSet:
    acquired_at = datetime.now(timezone.utc)
    monotonic_ns = time.monotonic_ns()
    current = case.runtime.pointers.load(REPO_UUID, allow_missing=True)
    current_value = None if current is None else current.to_dict()
    current_ref = (
        None
        if current_value is None
        else cast(dict[str, Any], current_value["current"])
    )
    receipt_value = receipt.to_dict()
    return case.runtime.pointers.promote(
        grant,
        PointerCAS(
            expected_pointer_revision=(
                0 if current_value is None else int(current_value["pointer_revision"])
            ),
            expected_active_source_revision=grant.active_source_revision,
            expected_source_epoch=int(receipt_value["source_epoch"]),
            expected_operation_epoch=grant.operation_epoch,
            expected_migration_epoch=grant.migration_epoch,
            expected_state_schema_version=1,
            expected_fence_token=int(grant.lease.to_dict()["fence_token"]),
            candidate_generation_id=str(receipt_value["generation_id"]),
            candidate_receipt_sha256=receipt.sha256,
            expected_current_receipt_sha256=(
                None if current_ref is None else str(current_ref["receipt_sha256"])
            ),
        ),
        occurred_at=acquired_at,
        monotonic_ns=monotonic_ns,
    )


def _promoted_cleanup_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pointer_recovery: bool = False,
    fault_hook: Any = None,
) -> _PromotedCleanupCase:
    harness, runtime, request, _, _ = _complete_handoff(
        tmp_path,
        monkeypatch,
        fault_hook=fault_hook,
    )
    workspace_sync._finalize_semantic_generation_certification(runtime, request)
    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    assert certified.lifecycle_state == "CERTIFIED"
    receipt = runtime.generations.verify_generation(REPO_UUID, GENERATION_ID)
    _, observations = workspace_sync._observe_structural_source(
        runtime,
        REPO_UUID,
    )
    label = b"pointer-recovery-cleanup" if pointer_recovery else b"promote-cleanup"
    attempt_sha256 = hashlib.sha256(label).hexdigest()

    if not pointer_recovery:
        with monkeypatch.context() as patch:
            patch.setattr(workspace_sync, "_release_grant", lambda *_args: None)
            promoted = workspace_sync._promote(
                runtime,
                request,
                certified.request,
                receipt,
                observations,
                attempt_sha256=attempt_sha256,
            )
    else:
        original_fault_hook = runtime.pointers.fault_hook

        def interrupt_pending(event: str) -> None:
            if event == "pointer:promoted:pending_durable":
                raise InjectedFault(event)
            original_fault_hook(event)

        with monkeypatch.context() as patch:
            patch.setattr(runtime.pointers, "fault_hook", interrupt_pending)
            patch.setattr(workspace_sync, "_release_grant", lambda *_args: None)
            with pytest.raises(
                GenerationConflict,
                match="promotion returned no terminal staged state",
            ):
                workspace_sync._promote(
                    runtime,
                    request,
                    certified.request,
                    receipt,
                    observations,
                    attempt_sha256=attempt_sha256,
                )

        prior_owner = runtime.leases.current_owner()
        rebooted_owner = replace(
            prior_owner,
            boot_id="rebooted-pointer-recovery-cleanup-owner",
        )
        monkeypatch.setattr(runtime.leases, "current_owner", lambda: rebooted_owner)
        recovery = runtime.generations.acquire_staged_recovery(
            REPO_UUID,
            GENERATION_ID,
            certified.request,
            attempt_sha256=attempt_sha256,
            acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(),
            ttl_ns=60_000_000_000,
        )
        assert recovery.grant.lease.to_dict()["operation"] == "POINTER_RECOVERY"
        pointer = runtime.pointers.recover(
            recovery.grant,
            occurred_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(),
        )
        promoted = runtime.generations.complete_staged_promotion(
            recovery,
            pointer,
            monotonic_ns=time.monotonic_ns(),
        )

    assert promoted is not None
    assert promoted.lifecycle_state == "PROMOTED"
    retained = runtime.leases.inspect(REPO_UUID)
    assert retained.staged_attempt_sha256 == attempt_sha256
    retained_grant = _retained_grant(runtime)
    expected_operation = "POINTER_RECOVERY" if pointer_recovery else "PROMOTE"
    assert retained_grant.lease.to_dict()["operation"] == expected_operation
    return _PromotedCleanupCase(
        harness=harness,
        runtime=runtime,
        request=request,
        structural_request=certified.request,
        promoted=promoted,
        attempt_sha256=attempt_sha256,
        retained_grant=retained_grant,
    )


def _terminal_evidence(case: _PromotedCleanupCase) -> dict[str, Any]:
    workspace = _workspace_root(case)
    handoff = (
        workspace
        / "semantic-staging"
        / "handoffs"
        / GENERATION_ID
        / f"{case.structural_request.sha256}.json"
    )
    return {
        "staged": (workspace / "staged-build.json").read_bytes(),
        "pointer": (workspace / "pointers.json").read_bytes(),
        "journal": tree_snapshot(workspace / "journal"),
        "generation": tree_snapshot(workspace / "generations" / GENERATION_ID),
        "handoff": handoff.read_bytes(),
        "binding": _binding_path(case).read_bytes(),
        "receipt": case.runtime.generations.verify_generation(
            REPO_UUID,
            GENERATION_ID,
        ).canonical,
    }


def _acquire_cleanup(
    case: _PromotedCleanupCase,
    *,
    generation_id: str = GENERATION_ID,
    request: StructuralBuildRequest | None = None,
    attempt_sha256: str | None = None,
    monotonic_ns: int | None = None,
):
    return case.runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        generation_id,
        case.structural_request if request is None else request,
        attempt_sha256=(case.attempt_sha256 if attempt_sha256 is None else attempt_sha256),
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
        ttl_ns=60_000_000_000,
    )


def _commit_lease_state(
    case: _PromotedCleanupCase,
    transform: Callable[[WorkspaceLeaseState], WorkspaceLeaseState],
) -> WorkspaceLeaseState:
    with case.runtime.registry.recovered_snapshot() as document:
        with case.runtime.leases.workspace_lock(REPO_UUID):
            current = case.runtime.leases._load_state_locked(document, REPO_UUID)
            changed = cast(WorkspaceLeaseState, transform(current))
            return case.runtime.leases._commit_state_locked(changed)


def _replace_retained_operation(
    case: _PromotedCleanupCase,
    operation: str,
) -> WorkspaceLeaseState:
    def transform(current: WorkspaceLeaseState) -> WorkspaceLeaseState:
        leases = dict(current.leases)
        lease = leases["workspace"].to_dict()
        lease["operation"] = operation
        leases["workspace"] = cast(FencedLease, FencedLease.from_mapping(lease))
        return replace(current, revision=current.revision + 1, leases=leases)

    return _commit_lease_state(case, transform)


def _remove_retained_pair(
    case: _PromotedCleanupCase,
    *,
    grant: bool,
    attempt: bool,
) -> WorkspaceLeaseState:
    def transform(current: WorkspaceLeaseState) -> WorkspaceLeaseState:
        leases = dict(current.leases)
        lease_epochs = dict(current.lease_epochs)
        if grant:
            leases.pop("workspace", None)
            lease_epochs.pop("workspace", None)
        return replace(
            current,
            revision=current.revision + 1,
            leases=leases,
            lease_epochs=lease_epochs,
            staged_attempt_sha256=(None if attempt else current.staged_attempt_sha256),
        )

    return _commit_lease_state(case, transform)


def test_promoted_terminal_cleanup_reuses_same_owner_exact_promote_grant_and_releases_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    before = _terminal_evidence(case)

    cleanup = _acquire_cleanup(case)

    assert cleanup.state.canonical == case.promoted.canonical
    assert cleanup.grant == case.retained_grant
    assert cleanup.grant.lease.to_dict()["operation"] == "PROMOTE"
    released = case.runtime.leases.release(cleanup.grant)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None
    assert _terminal_evidence(case) == before


def test_promoted_terminal_cleanup_tolerates_unrelated_global_registry_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    before = _terminal_evidence(case)
    other_repo = create_repo(
        tmp_path / "repo-two",
        "22222222-2222-4222-8222-222222222222",
    )
    registry = case.harness.registry.enroll(
        discover_source(other_repo),
        authorization("enroll-unrelated-before-promoted-cleanup"),
        expected_revision=case.structural_request.expected_registry_revision,
    )
    current_registry_revision = int(registry.to_dict()["revision"])
    assert current_registry_revision > case.structural_request.expected_registry_revision

    cleanup = _acquire_cleanup(case)

    assert cleanup.state.canonical == case.promoted.canonical
    assert cleanup.grant.registry_revision == current_registry_revision
    assert cleanup.grant.active_source_revision == case.retained_grant.active_source_revision
    assert cleanup.grant.lease == case.retained_grant.lease
    assert cleanup.grant.operation_epoch == case.retained_grant.operation_epoch
    released = case.runtime.leases.release(cleanup.grant)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None
    assert _terminal_evidence(case) == before


@pytest.mark.parametrize("pointer_recovery", [False, True])
@pytest.mark.parametrize("replacement", ["rebooted", "expired"])
def test_promoted_terminal_cleanup_replaces_only_exact_retained_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_recovery: bool,
    replacement: str,
) -> None:
    case = _promoted_cleanup_case(
        tmp_path,
        monkeypatch,
        pointer_recovery=pointer_recovery,
    )
    before = _terminal_evidence(case)
    retained = case.runtime.leases.inspect(REPO_UUID)
    old_lease = cast(FencedLease, retained.leases["workspace"])
    old_fence = int(old_lease.to_dict()["fence_token"])
    monotonic_ns = time.monotonic_ns()
    if replacement == "rebooted":
        owner = case.runtime.leases.current_owner()
        rebooted = replace(owner, boot_id="rebooted-promotion-cleanup-owner")
        monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted)
    else:
        monotonic_ns = int(old_lease.to_dict()["liveness_deadline_monotonic_ns"])

    cleanup = _acquire_cleanup(case, monotonic_ns=monotonic_ns)

    acquired = case.runtime.leases.inspect(REPO_UUID)
    expected_operation = "POINTER_RECOVERY" if pointer_recovery else "PROMOTE"
    assert cleanup.grant.lease.to_dict()["operation"] == expected_operation
    assert cleanup.grant.operation_epoch == retained.operation_epoch + 1
    assert int(cleanup.grant.lease.to_dict()["fence_token"]) == old_fence + 1
    assert acquired.staged_attempt_sha256 == case.attempt_sha256
    assert case.runtime.generations.recover_staged_build(REPO_UUID) == case.promoted
    assert _terminal_evidence(case) == before
    case.runtime.leases.release(cleanup.grant)


def test_promoted_terminal_cleanup_rejects_operation_drift_between_lock_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    prior_owner = case.runtime.leases.current_owner()
    rebooted_owner = replace(
        prior_owner,
        boot_id="rebooted-after-cleanup-classification",
    )
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted_owner)
    real_acquire = case.runtime.leases._acquire_under_registry_lock
    drifted: WorkspaceLeaseState | None = None

    def acquire_after_operation_drift(
        document: Any,
        *args: Any,
        **kwargs: Any,
    ) -> LeaseGrant:
        nonlocal drifted
        with case.runtime.leases.workspace_lock(REPO_UUID):
            current = case.runtime.leases._load_state_locked(document, REPO_UUID)
            leases = dict(current.leases)
            retained = leases["workspace"].to_dict()
            retained["operation"] = "POINTER_RECOVERY"
            leases["workspace"] = cast(FencedLease, FencedLease.from_mapping(retained))
            drifted = case.runtime.leases._commit_state_locked(
                replace(current, revision=current.revision + 1, leases=leases)
            )
        return real_acquire(document, *args, **kwargs)

    monkeypatch.setattr(
        case.runtime.leases,
        "_acquire_under_registry_lock",
        acquire_after_operation_drift,
    )

    with pytest.raises(
        LeaseRecoveryRequired,
        match="promotion cleanup operation changed before acquisition",
    ):
        _acquire_cleanup(case)

    assert drifted is not None
    assert case.runtime.leases.inspect(REPO_UUID).canonical == drifted.canonical


def test_promoted_terminal_cleanup_rejects_later_unrelated_fence_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    retained = case.runtime.leases.inspect(REPO_UUID)
    semantic = case.runtime.leases.acquire(
        REPO_UUID,
        "SEMANTIC_CLAIM",
        case.runtime.leases.current_owner(),
        expected_registry_revision=case.request.expected_registry_revision,
        expected_active_source_revision=case.request.expected_active_source_revision,
        expected_operation_epoch=retained.operation_epoch,
        expected_migration_epoch=retained.migration_epoch,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    advanced = case.runtime.leases.inspect(REPO_UUID)
    assert semantic.operation_epoch == retained.operation_epoch + 1
    assert advanced.fence_high_watermark == retained.fence_high_watermark + 1

    with pytest.raises(
        LeaseRecoveryRequired,
        match="promotion cleanup grant is not the latest fenced authority",
    ):
        _acquire_cleanup(case)

    assert case.runtime.leases.inspect(REPO_UUID).canonical == advanced.canonical
    case.runtime.leases.release(semantic)


def test_promoted_terminal_cleanup_retains_pointer_recovery_operation_without_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(
        tmp_path,
        monkeypatch,
        pointer_recovery=True,
    )
    before = _terminal_evidence(case)

    cleanup = _acquire_cleanup(case)

    assert cleanup.state.canonical == case.promoted.canonical
    assert cleanup.grant == case.retained_grant
    assert cleanup.grant.lease.to_dict()["operation"] == "POINTER_RECOVERY"
    case.runtime.leases.release(cleanup.grant)
    assert _terminal_evidence(case) == before


def test_promoted_terminal_cleanup_rejects_live_foreign_owner_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    retained = case.runtime.leases.inspect(REPO_UUID)
    owner = case.runtime.leases.current_owner()
    foreign = replace(
        owner,
        pid=owner.pid + 1,
        process_start_id=f"{owner.process_start_id}-foreign",
    )
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: foreign)

    with pytest.raises(LeaseBusy, match="workspace lease is held"):
        _acquire_cleanup(case)

    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


@pytest.mark.parametrize(
    "mismatch",
    ["attempt", "request", "target", "operation"],
)
def test_promoted_terminal_cleanup_rejects_changed_binding_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    generation_id = GENERATION_ID
    request = case.structural_request
    attempt_sha256 = case.attempt_sha256
    if mismatch == "attempt":
        attempt_sha256 = hashlib.sha256(b"wrong-cleanup-attempt").hexdigest()
    elif mismatch == "request":
        request = replace(
            request,
            expected_payload_bytes=request.expected_payload_bytes + 1,
        )
    elif mismatch == "target":
        generation_id = "gen-unrelated-promoted-target"
    else:
        _replace_retained_operation(case, "BUILD")
    retained = case.runtime.leases.inspect(REPO_UUID)

    with pytest.raises((GenerationConflict, LeaseRecoveryRequired)):
        _acquire_cleanup(
            case,
            generation_id=generation_id,
            request=request,
            attempt_sha256=attempt_sha256,
        )

    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


@pytest.mark.parametrize(
    ("missing_grant", "missing_attempt"),
    [(True, False), (False, True), (True, True)],
)
def test_promoted_terminal_cleanup_rejects_missing_or_unpaired_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_grant: bool,
    missing_attempt: bool,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    if missing_grant and not missing_attempt:
        lease_current = case.runtime.leases.state.path(case.runtime.leases._paths(REPO_UUID)[0])
        value = json.loads(lease_current.read_bytes())
        value["leases"].pop("workspace")
        value["lease_epochs"].pop("workspace")
        lease_current.write_bytes(canonical_json_bytes(value))
        lease_tree = tree_snapshot(lease_current.parent)

        with pytest.raises((GenerationError, LeaseRecoveryRequired)):
            _acquire_cleanup(case)

        assert tree_snapshot(lease_current.parent) == lease_tree
        return
    retained = _remove_retained_pair(
        case,
        grant=missing_grant,
        attempt=missing_attempt,
    )

    with pytest.raises((GenerationConflict, LeaseRecoveryRequired)):
        _acquire_cleanup(case)

    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


@pytest.mark.parametrize(
    "invalid_evidence",
    [
        "staged_pointer_revision",
        "staged_pending",
        "pending_pointer",
        "visible_pointer",
        "journal_pending",
        "installed_payload",
        "receipt",
        "binding",
        "binding_observation",
        "handoff",
    ],
)
def test_promoted_terminal_cleanup_rejects_incomplete_terminal_evidence_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_evidence: str,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    workspace = _workspace_root(case)
    if invalid_evidence == "staged_pointer_revision":
        assert case.promoted.pointer_revision is not None
        invalid = replace(
            case.promoted,
            revision=case.promoted.revision + 1,
            pointer_revision=case.promoted.pointer_revision + 1,
        )
        with case.runtime.registry.recovered_snapshot():
            with case.runtime.leases.workspace_lock(REPO_UUID):
                case.runtime.generations._commit_staged_build_locked(invalid)
    elif invalid_evidence == "staged_pending":
        pending = workspace / "staged-build.pending.json"
        pending.write_bytes((workspace / "staged-build.json").read_bytes())
        pending.chmod(0o600)
    elif invalid_evidence == "pending_pointer":
        pending = workspace / "pointers.pending.json"
        pending.write_bytes((workspace / "pointers.json").read_bytes())
        pending.chmod(0o600)
    elif invalid_evidence == "visible_pointer":
        pointer = workspace / "pointers.json"
        value = json.loads(pointer.read_bytes())
        value["pointer_revision"] += 1
        pointer.write_bytes(canonical_json_bytes(value))
    elif invalid_evidence == "journal_pending":
        pending = workspace / "journal" / "head.pending.json"
        pending.write_bytes((workspace / "journal" / "head.json").read_bytes())
        pending.chmod(0o600)
    elif invalid_evidence == "installed_payload":
        semantic_input = (
            workspace / "generations" / GENERATION_ID / "graphify-out" / "semantic-inputs.json"
        )
        semantic_input.write_bytes(b"{}")
    elif invalid_evidence == "receipt":
        receipt = workspace / "generations" / GENERATION_ID / "receipt.json"
        value = json.loads(receipt.read_bytes())
        value["source_commit"] = "f" * 40
        receipt.write_bytes(canonical_json_bytes(value))
    elif invalid_evidence == "binding":
        _binding_path(case).unlink()
    elif invalid_evidence == "binding_observation":
        binding = _binding_path(case)
        value = json.loads(binding.read_bytes())
        value["view"]["observation_evidence_sha256"] = "0" * 64
        binding.write_bytes(canonical_json_bytes(value))
    else:
        _handoff_path(case.harness, case.runtime, case.request).unlink()
    retained = case.runtime.leases.inspect(REPO_UUID)
    before = tree_snapshot(workspace)

    with pytest.raises((GenerationConflict, GenerationError, LeaseRecoveryRequired)):
        _acquire_cleanup(case)

    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical
    assert tree_snapshot(workspace) == before


@pytest.mark.parametrize(
    ("substitution", "expected_message"),
    [
        ("coordination_lock", "coordination lock identity does not match"),
        ("staged_request", "PROMOTED state does not advance"),
        ("staged_target", "semantic promotion staged request differs"),
        ("staged_manifest", "PROMOTED state does not advance"),
    ],
)
def test_finalizer_rejects_substituted_terminal_proof_before_cleanup_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
    expected_message: str,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    workspace = _workspace_root(case)
    if substitution == "coordination_lock":
        lock = case.runtime.generations.state.path(
            case.runtime.generations._lock(REPO_UUID, GENERATION_ID)
        )
        substituted_lock = case.runtime.generations._lock_document(
            "gen-substituted-terminal-lock"
        )
        lock.write_bytes(substituted_lock.canonical)
        assert lock.read_bytes() == substituted_lock.canonical
    else:
        staged = case.promoted
        if substitution == "staged_request":
            staged = replace(
                staged,
                request=replace(
                    staged.request,
                    expected_payload_bytes=staged.request.expected_payload_bytes + 1,
                ),
            )
        elif substitution == "staged_target":
            staged = replace(
                staged,
                generation_id="gen-substituted-terminal-target",
            )
        else:
            staged = replace(
                staged,
                payload_manifest_sha256=hashlib.sha256(
                    b"substituted-terminal-manifest"
                ).hexdigest(),
            )
        staged_path = workspace / "staged-build.json"
        staged_path.write_bytes(staged.canonical)
        assert StagedBuildState.from_json(staged_path.read_bytes()) == staged

    before = tree_snapshot(case.harness.state_root)
    lease_before = case.runtime.leases.inspect(REPO_UUID).canonical
    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        _forbidden_finalization("substituted terminal cleanup acquisition"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("substituted terminal promotion"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "recover",
        _forbidden_finalization("substituted terminal pointer recovery"),
    )
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("substituted terminal source observation"),
    )

    with pytest.raises(
        (GenerationConflict, GenerationError),
        match=expected_message,
    ):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert tree_snapshot(case.harness.state_root) == before
    assert case.runtime.leases.inspect(REPO_UUID).canonical == lease_before


@pytest.mark.parametrize(
    "pointer_recovery",
    [False, True],
    ids=["promoted-journal", "repaired-journal"],
)
@pytest.mark.parametrize("substituted_field", ["operation_epoch", "fence_token"])
def test_finalizer_rejects_authoritative_terminal_journal_field_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_recovery: bool,
    substituted_field: str,
) -> None:
    case = _promoted_cleanup_case(
        tmp_path,
        monkeypatch,
        pointer_recovery=pointer_recovery,
    )
    projection = case.runtime.journal.project_recovery(REPO_UUID)
    transition = "REPAIRED" if pointer_recovery else "PROMOTED"
    events = list(projection.snapshot.events)
    matching_indices = [
        index
        for index, event in enumerate(events)
        if event.to_dict()["generation_id"] == GENERATION_ID
        and event.to_dict()["transition"] == transition
    ]
    assert matching_indices == [len(events) - 1]
    index = matching_indices[0]
    substituted_value = events[index].to_dict()
    substituted_value[substituted_field] = int(
        substituted_value[substituted_field]
    ) + 1
    logical = {
        key: substituted_value[key]
        for key in (
            "transition",
            "generation_id",
            "receipt_sha256",
            "pointer_revision",
            "operation_epoch",
            "fence_token",
            "occurred_at",
        )
    }
    substituted_value["event_id"] = case.runtime.journal._event_id(
        REPO_UUID,
        logical,
    )
    substituted_event = cast(
        JournalEvent,
        JournalEvent.from_mapping(substituted_value),
    )
    case.runtime.journal._require_repo_event_id(REPO_UUID, substituted_event)
    events[index] = substituted_event
    case.runtime.journal._validate_lifecycle(events)
    frame = encode_journal_frame(substituted_event)
    substituted_projection = replace(
        projection,
        snapshot=replace(
            projection.snapshot,
            head=case.runtime.journal._head_for(
                REPO_UUID,
                substituted_event,
                frame,
            ),
            events=tuple(events),
        ),
    )

    before = tree_snapshot(case.harness.state_root)
    lease_before = case.runtime.leases.inspect(REPO_UUID).canonical
    monkeypatch.setattr(
        case.runtime.journal,
        "project_recovery",
        lambda *_args, **_kwargs: substituted_projection,
    )
    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        _forbidden_finalization("substituted journal cleanup acquisition"),
    )
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("substituted journal source observation"),
    )

    with pytest.raises(
        GenerationConflict,
        match="visible promotion has no exact authoritative journal event",
    ):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert tree_snapshot(case.harness.state_root) == before
    assert case.runtime.leases.inspect(REPO_UUID).canonical == lease_before


@pytest.mark.parametrize(
    ("error", "wrapped"),
    [
        (JournalCorrupt("invalid recovery journal"), True),
        (StateCorrupt("invalid journal state"), True),
        (StatePathError("unsafe journal path"), False),
        (LockTimeout("journal read timed out"), False),
        (RuntimeError("unexpected journal failure"), False),
    ],
)
def test_promoted_terminal_cleanup_preserves_journal_projection_exception_taxonomy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    wrapped: bool,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    retained = case.runtime.leases.inspect(REPO_UUID)

    def fail_projection(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(case.runtime.journal, "project_recovery", fail_projection)

    if wrapped:
        with pytest.raises(
            GenerationConflict, match="promoted lifecycle journal is invalid"
        ) as caught:
            _acquire_cleanup(case)
        assert caught.value.__cause__ is error
    else:
        with pytest.raises(type(error)) as caught:
            _acquire_cleanup(case)
        assert caught.value is error

    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


def test_promoted_terminal_cleanup_rejects_abandoned_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, _, _ = _complete_handoff(tmp_path, monkeypatch)
    workspace_sync._finalize_semantic_generation_certification(runtime, request)
    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    _, observations = workspace_sync._observe_structural_source(runtime, REPO_UUID)
    _advance_active_source_revision(harness)
    attempt_sha256 = hashlib.sha256(b"abandoned-terminal").hexdigest()
    attempt = runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        certified.request,
        attempt_sha256=attempt_sha256,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    abandoned = runtime.generations.abandon_staged_build(
        attempt,
        source_observations=observations,
        monotonic_ns=time.monotonic_ns(),
    )
    assert abandoned.lifecycle_state == "ABANDONED"
    retained = runtime.leases.inspect(REPO_UUID)

    with pytest.raises(GenerationConflict, match="already ABANDONED"):
        runtime.generations.acquire_staged_recovery(
            REPO_UUID,
            GENERATION_ID,
            certified.request,
            attempt_sha256=attempt_sha256,
            acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(),
            ttl_ns=60_000_000_000,
        )

    assert runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


@pytest.mark.parametrize("operation", ["BUILD", "PROMOTE", "GC"])
def test_generic_acquisition_cannot_replace_promoted_cleanup_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    retained = case.runtime.leases.inspect(REPO_UUID)
    lease = cast(FencedLease, retained.leases["workspace"])

    with pytest.raises(LeaseRecoveryRequired):
        case.runtime.leases.acquire(
            REPO_UUID,
            operation,
            case.runtime.leases.current_owner(),
            expected_registry_revision=case.request.expected_registry_revision,
            expected_active_source_revision=case.request.expected_active_source_revision,
            expected_operation_epoch=retained.operation_epoch,
            expected_migration_epoch=retained.migration_epoch,
            acquired_at=datetime.now(timezone.utc),
            monotonic_ns=int(lease.to_dict()["liveness_deadline_monotonic_ns"]),
            ttl_ns=60_000_000_000,
        )

    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


def test_promoted_terminal_cleanup_releases_exact_grant_when_under_grant_proof_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _promoted_cleanup_case(tmp_path, monkeypatch)
    retained = case.runtime.leases.inspect(REPO_UUID)
    owner = case.runtime.leases.current_owner()
    rebooted = replace(owner, boot_id="reboot-before-promotion-revalidation")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted)
    real_acquire = case.runtime.leases._acquire_under_registry_lock

    def acquire_then_drift(*args: Any, **kwargs: Any) -> LeaseGrant:
        grant = real_acquire(*args, **kwargs)
        _binding_path(case).unlink()
        return grant

    monkeypatch.setattr(
        case.runtime.leases,
        "_acquire_under_registry_lock",
        acquire_then_drift,
    )

    with pytest.raises((GenerationConflict, GenerationError)):
        _acquire_cleanup(case)

    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.operation_epoch == retained.operation_epoch + 1
    assert released.fence_high_watermark == retained.fence_high_watermark + 1
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None


def test_exact_certified_direct_promotion_and_terminal_replay_are_narrow_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    semantic_before = _semantic_evidence(case)
    acquired: list[Any] = []
    promoted: list[Any] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery
    real_promote = case.runtime.pointers.promote

    def acquire(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        acquired.append(attempt)
        return attempt

    def promote(grant: LeaseGrant, pointer_cas: Any, **kwargs: Any):
        assert pointer_cas.expected_pointer_revision == case.request.expected_pointer_revision
        assert (
            pointer_cas.expected_current_receipt_sha256
            == case.request.expected_current_receipt_sha256
        )
        assert pointer_cas.expected_active_source_revision == grant.active_source_revision
        assert pointer_cas.expected_source_epoch == case.receipt.to_dict()["source_epoch"]
        assert pointer_cas.expected_operation_epoch == grant.operation_epoch
        assert pointer_cas.expected_migration_epoch == grant.migration_epoch
        assert pointer_cas.expected_fence_token == grant.lease.to_dict()["fence_token"]
        assert pointer_cas.candidate_generation_id == case.request.generation_id
        assert pointer_cas.candidate_receipt_sha256 == case.receipt.sha256
        promoted.append(pointer_cas)
        return real_promote(grant, pointer_cas, **kwargs)

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)
    monkeypatch.setattr(case.runtime.pointers, "promote", promote)

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert len(acquired) == 1
    assert acquired[0].state.canonical == case.certified.canonical
    assert acquired[0].grant.lease.to_dict()["operation"] == "PROMOTE"
    assert len(promoted) == 1
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before

    before_replay = tree_snapshot(case.harness.state_root)
    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        _forbidden_finalization("terminal replay acquisition"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("terminal replay promotion"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "recover",
        _forbidden_finalization("terminal replay pointer recovery"),
    )
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("terminal replay source observation"),
    )

    replay = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert replay == proof
    assert tree_snapshot(case.harness.state_root) == before_replay


def test_exact_current_rejects_authoritative_unrelated_visible_generation_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _carried_certified_promotion_case(tmp_path, monkeypatch)
    initial = case.runtime.pointers.load(REPO_UUID)
    assert initial is not None
    initial_ref = cast(dict[str, Any], initial.to_dict()["current"])
    unrelated_receipt = case.runtime.generations.verify_generation(
        REPO_UUID,
        str(initial_ref["generation_id"]),
    )

    attempt_sha256 = hashlib.sha256(b"unrelated-exact-current-setup").hexdigest()
    attempt = case.runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        case.request.generation_id,
        case.certified.request,
        attempt_sha256=attempt_sha256,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    try:
        target_visible = _advance_visible_pointer(case, attempt.grant, case.receipt)
        assert target_visible.to_dict()["current"] == {
            "generation_id": case.request.generation_id,
            "receipt_sha256": case.receipt.sha256,
        }
        unrelated_visible = _advance_visible_pointer(
            case,
            attempt.grant,
            unrelated_receipt,
        )
    finally:
        case.runtime.leases.release(attempt.grant)
    unrelated_value = unrelated_visible.to_dict()
    assert unrelated_value["current"] == initial_ref
    assert int(unrelated_value["pointer_revision"]) > case.request.expected_pointer_revision
    verified = case.runtime.pointers.verify_pointer(
        unrelated_visible,
        expected_repo_uuid=REPO_UUID,
    )
    assert verified["current"].canonical == unrelated_receipt.canonical
    case.runtime.pointers._verify_visible_pointer_journal(
        REPO_UUID,
        unrelated_visible,
    )

    before = tree_snapshot(case.harness.state_root)
    lease_before = case.runtime.leases.inspect(REPO_UUID).canonical
    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        _forbidden_finalization("unrelated exact-current acquisition"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("unrelated exact-current promotion"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "recover",
        _forbidden_finalization("unrelated exact-current recovery"),
    )
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("unrelated exact-current source observation"),
    )

    with pytest.raises(GenerationConflict, match="pointer CAS is no longer current"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert tree_snapshot(case.harness.state_root) == before
    assert case.runtime.leases.inspect(REPO_UUID).canonical == lease_before


def test_direct_promotion_acquires_registry_then_workspace_then_sorted_generation_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _carried_certified_promotion_case(tmp_path, monkeypatch)
    semantic_before = _semantic_evidence(case)
    current = case.runtime.pointers.load(REPO_UUID)
    assert current is not None
    current_generation_id = str(
        cast(dict[str, Any], current.to_dict()["current"])["generation_id"]
    )
    events: list[str] = []
    for state in (
        case.runtime.registry.state,
        case.runtime.leases.state,
        case.runtime.generations.state,
        case.runtime.pointers.state,
    ):
        state.fault_hook = events.append
    pointer_events: list[str] = []
    real_promote = case.runtime.pointers.promote

    def promote(*args: Any, **kwargs: Any):
        start = len(events)
        result = real_promote(*args, **kwargs)
        pointer_events.extend(events[start:])
        return result

    monkeypatch.setattr(case.runtime.pointers, "promote", promote)

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    expected_generation_ids = sorted(
        {current_generation_id, case.request.generation_id}
    )
    generation_acquisitions = [
        event
        for event in pointer_events
        if event.startswith("lock:generation:") and event.endswith(":acquired")
    ]
    assert generation_acquisitions == [
        f"lock:generation:{generation_id}:acquired"
        for generation_id in expected_generation_ids
    ]
    registry_index = pointer_events.index("lock:registry:acquired")
    workspace_index = pointer_events.index("lock:workspace:acquired")
    generation_indices = [
        pointer_events.index(event) for event in generation_acquisitions
    ]
    assert registry_index < workspace_index < generation_indices[0]
    assert generation_indices[-1] < pointer_events.index("lock:workspace:released")
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before


def test_direct_promotion_revalidates_handoff_before_staged_promoted_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    workspace = case.harness.state_root / "workspaces" / REPO_UUID
    handoff = _handoff_path(case.harness, case.runtime, case.request)
    generation_before = tree_snapshot(
        workspace / "generations" / case.request.generation_id
    )
    binding = _binding_path(case)
    binding_before = binding.read_bytes()
    real_promote = case.runtime.pointers.promote

    def promote_then_remove_handoff(*args: Any, **kwargs: Any):
        pointer = real_promote(*args, **kwargs)
        handoff.unlink()
        return pointer

    monkeypatch.setattr(case.runtime.pointers, "promote", promote_then_remove_handoff)

    with pytest.raises((GenerationConflict, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "CERTIFIED"
    pointer = case.runtime.pointers.load(REPO_UUID)
    assert pointer is not None
    assert pointer.to_dict()["current"] == {
        "generation_id": case.request.generation_id,
        "receipt_sha256": case.receipt.sha256,
    }
    assert not (workspace / "pointers.pending.json").exists()
    assert tree_snapshot(
        workspace / "generations" / case.request.generation_id
    ) == generation_before
    assert binding.read_bytes() == binding_before
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None


def test_direct_promotion_keeps_revalidation_locked_through_staged_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    handoff = _handoff_path(case.harness, case.runtime, case.request)
    real_complete = case.runtime.generations.complete_staged_promotion

    def complete_after_handoff_substitution(*args: Any, **kwargs: Any):
        handoff.unlink()
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(
        case.runtime.generations,
        "complete_staged_promotion",
        complete_after_handoff_substitution,
    )

    with pytest.raises((GenerationConflict, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "CERTIFIED"
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None


def test_promotion_rejects_complete_entry_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, _, complete = _complete_handoff(tmp_path, monkeypatch)
    before = tree_snapshot(harness.state_root)
    monkeypatch.setattr(
        runtime.generations,
        "acquire_staged_recovery",
        _forbidden_finalization("COMPLETE entry acquisition"),
    )

    with pytest.raises(
        (GenerationConflict, SemanticHandoffConflict),
        match="CERTIFIED|certification",
    ):
        workspace_sync._finalize_semantic_generation_promotion(runtime, request)

    assert runtime.generations.recover_staged_build(REPO_UUID) == complete
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("substitution", ["request", "binding"])
def test_promotion_rejects_substituted_entry_before_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    request = case.request
    if substitution == "request":
        request = replace(request, generation_id="gen-substituted-promotion-target")
    else:
        _binding_path(case).unlink()
    before = tree_snapshot(case.harness.state_root)
    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        _forbidden_finalization("substituted entry acquisition"),
    )

    with pytest.raises((GenerationConflict, GenerationError, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_promotion(case.runtime, request)

    assert tree_snapshot(case.harness.state_root) == before


def test_promotion_revalidates_source_after_acquisition_and_preserves_semantic_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    semantic_before = _semantic_evidence(case)
    real_acquire = case.runtime.generations.acquire_staged_recovery
    real_observe = case.runtime.generations.adapter.observe
    acquired = False

    def acquire_then_drift(*args: Any, **kwargs: Any):
        nonlocal acquired
        attempt = real_acquire(*args, **kwargs)
        acquired = True
        return attempt

    def observe_after_drift(*args: Any, **kwargs: Any):
        observation = real_observe(*args, **kwargs)
        if acquired:
            return replace(observation, source_commit="f" * 40)
        return observation

    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        acquire_then_drift,
    )
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        observe_after_drift,
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("pointer move after source drift"),
    )

    with pytest.raises((GenerationConflict, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert _semantic_evidence(case) == semantic_before
    assert case.runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    assert case.runtime.generations.recover_staged_build(REPO_UUID) == case.certified
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None


def test_promotion_acquisition_commit_unknown_reuses_one_attempt_and_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    real_acquire = case.runtime.generations.acquire_staged_recovery
    calls: list[tuple[str, LeaseGrant]] = []

    def acquire_then_unknown(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        calls.append((kwargs["attempt_sha256"], attempt.grant))
        if len(calls) == 1:
            raise CommitUnknown("promotion acquisition acknowledgement was lost")
        return attempt

    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        acquire_then_unknown,
    )

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert len(calls) == 2
    assert calls[0] == calls[1]
    _assert_promotion_terminal(case, proof)


@pytest.mark.parametrize(
    "boundary",
    _WORKSPACE_COMMIT_BOUNDARIES,
)
@pytest.mark.parametrize(
    "retained_recovery",
    [False, True],
    ids=["fresh", "rebooted-retained"],
)
def test_promotion_acquisition_recovers_each_durable_commit_boundary_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    retained_recovery: bool,
) -> None:
    fault = _ArmedFault(boundary)
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    pre_acquisition = case.runtime.leases.inspect(REPO_UUID)
    assert pre_acquisition.leases.get("workspace") is None
    assert pre_acquisition.staged_attempt_sha256 is None
    if retained_recovery:
        retained_attempt = hashlib.sha256(b"retained-acquisition-recovery").hexdigest()
        retained = case.runtime.generations.acquire_staged_recovery(
            REPO_UUID,
            GENERATION_ID,
            case.certified.request,
            attempt_sha256=retained_attempt,
            acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(),
            ttl_ns=60_000_000_000,
        )
        assert retained.grant.lease.to_dict()["operation"] == "PROMOTE"
        prior_owner = case.runtime.leases.current_owner()
        rebooted_owner = replace(
            prior_owner,
            boot_id=f"rebooted-during-{boundary}",
        )
        monkeypatch.setattr(
            case.runtime.leases,
            "current_owner",
            lambda: rebooted_owner,
        )
    semantic_before = _semantic_evidence(case)
    acquired_attempts: list[str] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery

    def acquire_before_process_death(*args: Any, **kwargs: Any):
        acquired_attempts.append(str(kwargs["attempt_sha256"]))
        try:
            return real_acquire(*args, **kwargs)
        except CommitUnknown as exc:
            raise InjectedFault(f"process died during {boundary}") from exc

    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        acquire_before_process_death,
    )
    fault.armed = True

    with pytest.raises(InjectedFault, match="process died"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert fault.fired
    workspace = _workspace_root(case)
    pending = workspace / "workspace.pending.json"
    durable = pending if pending.exists() else workspace / "workspace.json"
    persisted = WorkspaceLeaseState.from_json(durable.read_bytes())
    persisted_attempt = persisted.staged_attempt_sha256
    assert persisted_attempt is not None

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert acquired_attempts == [persisted_attempt, persisted_attempt]
    assert not pending.exists()
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before


def test_promotion_acquisition_accepts_registry_floored_durable_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("workspace:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    workspace = _workspace_root(case)
    raw_predecessor = WorkspaceLeaseState.from_json(
        (workspace / "workspace.json").read_bytes()
    )
    _advance_active_source_revision(case.harness)
    registry = case.runtime.registry.load()
    registry_entry = registry.to_dict()["workspaces"][0]
    active_evidence = cast(dict[str, Any], registry_entry["active_source_evidence"])
    attempt_sha256 = hashlib.sha256(b"registry-floored-promotion").hexdigest()
    fault.armed = True

    with pytest.raises(CommitUnknown):
        case.runtime.generations.acquire_staged_recovery(
            REPO_UUID,
            GENERATION_ID,
            case.certified.request,
            attempt_sha256=attempt_sha256,
            acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(),
            ttl_ns=60_000_000_000,
        )

    deadline_ns = time.monotonic_ns() + 5_000_000_000
    with case.runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as document:
        with case.runtime.leases.read_only_workspace_lock(
            REPO_UUID,
            deadline_ns=deadline_ns,
        ):
            projected, requires_recovery = (
                case.runtime.leases.project_uncertain_snapshot_locked(
                    document,
                    REPO_UUID,
                    deadline_ns=deadline_ns,
                )
            )
            predecessor = (
                workspace_sync._promotion_grant_acquisition_predecessor_locked(
                    case.runtime,
                    document.to_dict(),
                    projected,
                    pointer_mode="fresh",
                    deadline_ns=deadline_ns,
                )
            )

    assert requires_recovery
    assert raw_predecessor.operation_epoch < active_evidence["operation_epoch"]
    assert raw_predecessor.fence_high_watermark < active_evidence["fence_token"]
    assert predecessor.revision == raw_predecessor.revision
    assert predecessor.operation_epoch == active_evidence["operation_epoch"]
    assert predecessor.fence_high_watermark == active_evidence["fence_token"]
    assert projected.operation_epoch == predecessor.operation_epoch + 1
    assert projected.fence_high_watermark == predecessor.fence_high_watermark + 1


def test_promotion_capture_recovery_uses_one_deadline_and_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("workspace:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    real_acquire = _leave_promotion_acquisition_pending_after_process_death(
        case,
        monkeypatch,
        fault,
    )

    pending = _workspace_root(case) / "workspace.pending.json"
    assert pending.exists()
    monkeypatch.setattr(
        case.runtime.generations,
        "acquire_staged_recovery",
        real_acquire,
    )
    prior_owner = case.runtime.leases.current_owner()
    rebooted_owner = replace(prior_owner, boot_id="rebooted-during-capture-recovery")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted_owner)

    read_deadlines: list[int | None] = []
    recovery_deadlines: list[int | None] = []
    real_read = case.runtime.leases.read_only_snapshot_locked
    real_recover = case.runtime.leases.recover_uncertain_snapshot

    def read_snapshot(*args: Any, **kwargs: Any):
        read_deadlines.append(kwargs.get("deadline_ns"))
        return real_read(*args, **kwargs)

    def recover_then_install_replacement(*args: Any, **kwargs: Any):
        recovery_deadlines.append(kwargs.get("deadline_ns"))
        if len(recovery_deadlines) != 1:
            raise AssertionError("promotion acquisition recovery repeated")
        recovered = real_recover(*args, **kwargs)
        attempt_sha256 = recovered.staged_attempt_sha256
        assert attempt_sha256 is not None
        fault.fired = False
        fault.armed = True
        with pytest.raises(CommitUnknown):
            real_acquire(
                REPO_UUID,
                GENERATION_ID,
                case.certified.request,
                attempt_sha256=attempt_sha256,
                acquired_at=datetime.now(timezone.utc),
                monotonic_ns=time.monotonic_ns(),
                ttl_ns=60_000_000_000,
            )
        assert pending.exists()
        return recovered

    monkeypatch.setattr(
        case.runtime.leases,
        "read_only_snapshot_locked",
        read_snapshot,
    )
    monkeypatch.setattr(
        case.runtime.leases,
        "recover_uncertain_snapshot",
        recover_then_install_replacement,
    )

    with pytest.raises(CommitUnknown, match="did not converge"):
        workspace_sync._capture_semantic_promotion_entry(
            case.runtime,
            case.request,
            case.certified,
        )

    assert len(recovery_deadlines) == 1
    assert read_deadlines
    bounded_read_deadlines = [value for value in read_deadlines if value is not None]
    assert len(set(bounded_read_deadlines)) == 1
    assert recovery_deadlines == [bounded_read_deadlines[0]]
    assert pending.exists()
    assert case.runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    assert _semantic_evidence(case) == semantic_before


def test_promotion_acquisition_recovery_rejects_concurrent_staged_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("workspace:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    _leave_promotion_acquisition_pending_after_process_death(
        case,
        monkeypatch,
        fault,
    )

    pending = _workspace_root(case) / "workspace.pending.json"
    pending_before = pending.read_bytes()

    def concurrent_staged_pending(*_args: Any, **_kwargs: Any):
        raise StagedBuildReadRecoveryRequired("concurrent staged pending commit")

    monkeypatch.setattr(
        case.runtime.generations,
        "read_only_staged_build_locked",
        concurrent_staged_pending,
    )
    monkeypatch.setattr(
        case.runtime.leases,
        "recover_uncertain_snapshot",
        _forbidden_finalization("cross-kind lease recovery"),
    )

    with pytest.raises(CommitUnknown, match="did not converge"):
        workspace_sync._capture_semantic_promotion_entry(
            case.runtime,
            case.request,
            case.certified,
        )

    assert pending.read_bytes() == pending_before
    assert case.runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    assert _semantic_evidence(case) == semantic_before


def test_promotion_capture_recovers_pending_staged_transition_before_rejecting_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault(f"staged-build:{REPO_UUID}:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    fault.armed = True
    _leave_promotion_grant_after_process_death(case, monkeypatch)
    pending = _workspace_root(case) / "staged-build.pending.json"
    assert pending.exists()

    recovery_deadlines: list[int | None] = []
    real_recover = case.runtime.generations.recover_staged_build

    def recover_staged(*args: Any, **kwargs: Any):
        recovery_deadlines.append(kwargs.get("deadline_ns"))
        return real_recover(*args, **kwargs)

    monkeypatch.setattr(
        case.runtime.generations,
        "recover_staged_build",
        recover_staged,
    )

    with pytest.raises(GenerationConflict, match="promotion staged entry changed"):
        workspace_sync._capture_semantic_promotion_entry(
            case.runtime,
            case.request,
            case.certified,
        )

    assert recovery_deadlines and recovery_deadlines[0] is not None
    assert not pending.exists()
    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "PROMOTED"
    assert _semantic_evidence(case) == semantic_before
    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )
    _assert_promotion_terminal(case, proof)


def test_promotion_capture_chains_staged_recovery_before_rejecting_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    recovery_error = StagedBuildReadRecoveryRequired(
        "staged build has a pending durable commit"
    )

    def require_staged_recovery(*_args: Any, **_kwargs: Any):
        raise recovery_error

    monkeypatch.setattr(
        workspace_sync,
        "_capture_semantic_promotion_entry_once",
        require_staged_recovery,
    )
    monkeypatch.setattr(
        workspace_sync,
        "_project_and_recover_semantic_promotion_staged_state",
        lambda *_args, **_kwargs: replace(
            case.certified,
            revision=case.certified.revision + 1,
        ),
    )

    with pytest.raises(
        GenerationConflict,
        match="promotion staged entry changed",
    ) as raised:
        workspace_sync._capture_semantic_promotion_entry(
            case.runtime,
            case.request,
            case.certified,
        )

    assert raised.value.__cause__ is recovery_error


def test_promotion_staged_recovery_rejects_concurrent_lease_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault(f"staged-build:{REPO_UUID}:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    fault.armed = True
    _leave_promotion_grant_after_process_death(case, monkeypatch)
    pending = _workspace_root(case) / "staged-build.pending.json"
    pending_before = pending.read_bytes()
    real_read = case.runtime.leases.read_only_snapshot_locked
    reads = 0

    def concurrent_lease_pending(*args: Any, **kwargs: Any):
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_read(*args, **kwargs)
        raise StateRecoveryRequired("concurrent lease pending commit")

    monkeypatch.setattr(
        case.runtime.leases,
        "read_only_snapshot_locked",
        concurrent_lease_pending,
    )
    monkeypatch.setattr(
        case.runtime.generations,
        "recover_staged_build",
        _forbidden_finalization("cross-kind staged recovery"),
    )

    with pytest.raises(CommitUnknown, match="did not converge"):
        workspace_sync._capture_semantic_promotion_entry(
            case.runtime,
            case.request,
            case.certified,
        )

    assert reads == 2
    assert pending.read_bytes() == pending_before
    assert _semantic_evidence(case) == semantic_before


def test_promotion_acquisition_rejects_substituted_pending_outcome_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("workspace:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    pre_acquisition = case.runtime.leases.inspect(REPO_UUID)
    assert pre_acquisition.leases.get("workspace") is None
    assert pre_acquisition.staged_attempt_sha256 is None
    semantic_before = _semantic_evidence(case)
    _leave_promotion_acquisition_pending_after_process_death(
        case,
        monkeypatch,
        fault,
    )

    pending = _workspace_root(case) / "workspace.pending.json"
    projected = WorkspaceLeaseState.from_json(pending.read_bytes())
    substituted = replace(
        projected,
        migration_epoch=projected.migration_epoch + 1,
    ).canonical
    pending.write_bytes(substituted)
    monkeypatch.setattr(
        case.runtime.leases,
        "recover_uncertain_snapshot",
        _forbidden_finalization("substituted acquisition recovery"),
    )

    with pytest.raises(CommitUnknown, match="acquisition"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert pending.read_bytes() == substituted
    assert case.runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    assert case.runtime.generations.recover_staged_build(REPO_UUID) == case.certified
    assert _semantic_evidence(case) == semantic_before


@pytest.mark.parametrize(
    ("boundary", "expected_operation"),
    [
        ("pointer:promoted:pending:replaced", "POINTER_RECOVERY"),
        ("pointer:promoted:pending_durable", "POINTER_RECOVERY"),
        ("pointer:promoted:visible:replaced", "POINTER_RECOVERY"),
        ("pointer:promoted:visible", "POINTER_RECOVERY"),
        ("journal:PROMOTED:segment:installed", "POINTER_RECOVERY"),
        ("journal:PROMOTED:head:pending_durable", "POINTER_RECOVERY"),
        ("pointer:promoted:journal_durable", "POINTER_RECOVERY"),
        ("pointer:promoted:complete:unlinked", "PROMOTE"),
        ("pointer:promoted:complete", "PROMOTE"),
        (f"staged-build:{REPO_UUID}:pending_durable", None),
        (f"staged-build:{REPO_UUID}:previous_durable", None),
        (f"staged-build:{REPO_UUID}:current_durable", None),
        (f"generation:{GENERATION_ID}:staged_promoted_durable", None),
    ],
)
def test_promotion_commit_uncertainty_recovers_each_durable_boundary_without_recapture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_operation: str | None,
) -> None:
    fault = _ArmedFault(boundary)
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    fault.armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert fault.fired
    assert _semantic_evidence(case) == semantic_before
    acquired_operations: list[str] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery
    real_recover = case.runtime.pointers.recover
    recovery_plans: list[Any] = []

    def acquire(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        acquired_operations.append(str(attempt.grant.lease.to_dict()["operation"]))
        return attempt

    def recover(*args: Any, expected_plan: Any = None, **kwargs: Any):
        assert expected_plan is not None
        assert expected_plan.candidate == {
            "generation_id": case.request.generation_id,
            "receipt_sha256": case.receipt.sha256,
        }
        assert expected_plan.selected_from in {"pending", "current"}
        assert expected_plan.quarantine == ()
        recovery_plans.append(expected_plan)
        return real_recover(*args, expected_plan=expected_plan, **kwargs)

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)
    monkeypatch.setattr(case.runtime.pointers, "recover", recover)
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source recapture after pointer intent or visibility"),
    )

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    if expected_operation is None:
        assert acquired_operations == []
    else:
        assert acquired_operations == [expected_operation]
    assert bool(recovery_plans) == (expected_operation == "POINTER_RECOVERY")
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before


def test_retained_prior_commit_uncertainty_retries_the_same_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _carried_certified_promotion_case(tmp_path, monkeypatch)
    semantic_before = _semantic_evidence(case)
    pointer_before = case.runtime.pointers.load(REPO_UUID)
    assert pointer_before is not None
    fault = _ArmedFault("pointer:promoted:prior_durable")
    case.runtime.pointers.fault_hook = fault
    fault.armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert fault.fired
    assert case.runtime.pointers.load(REPO_UUID) == pointer_before
    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    _assert_promotion_terminal(case, proof)
    assert proof.pointer_revision == int(pointer_before.to_dict()["pointer_revision"]) + 1
    assert _semantic_evidence(case) == semantic_before


def test_process_death_after_pointer_intent_reclassifies_retained_promote_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("pointer:promoted:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    fault.armed = True

    _leave_promotion_grant_after_process_death(case, monkeypatch)

    retained = case.runtime.leases.inspect(REPO_UUID)
    retained_grant = cast(FencedLease, retained.leases["workspace"])
    retained_attempt = retained.staged_attempt_sha256
    assert retained_attempt is not None
    assert retained_grant.to_dict()["operation"] == "PROMOTE"
    workspace = case.harness.state_root / "workspaces" / REPO_UUID
    assert (workspace / "pointers.pending.json").is_file()
    prior_owner = case.runtime.leases.current_owner()
    rebooted_owner = replace(prior_owner, boot_id="rebooted-after-pointer-intent")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted_owner)
    acquired: list[tuple[str, str]] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery

    def acquire(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        acquired.append(
            (
                str(kwargs["attempt_sha256"]),
                str(attempt.grant.lease.to_dict()["operation"]),
            )
        )
        return attempt

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source recapture after retained pointer intent"),
    )

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert acquired == [(retained_attempt, "POINTER_RECOVERY")]
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before


def test_process_death_after_pointer_recovery_reclassifies_visible_replay_to_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("pointer:promoted:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    fault.armed = True

    _leave_promotion_grant_after_process_death(case, monkeypatch)

    retained = case.runtime.leases.inspect(REPO_UUID)
    attempt_sha256 = retained.staged_attempt_sha256
    assert attempt_sha256 is not None
    first_owner = case.runtime.leases.current_owner()
    recovery_owner = replace(first_owner, boot_id="rebooted-for-pointer-recovery")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: recovery_owner)
    recovery = case.runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        case.certified.request,
        attempt_sha256=attempt_sha256,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    assert recovery.grant.lease.to_dict()["operation"] == "POINTER_RECOVERY"
    pointer = case.runtime.pointers.recover(
        recovery.grant,
        occurred_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
    )
    assert pointer.to_dict()["current"]["generation_id"] == GENERATION_ID
    workspace = case.harness.state_root / "workspaces" / REPO_UUID
    assert not (workspace / "pointers.pending.json").exists()
    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "CERTIFIED"

    finalizer_owner = replace(recovery_owner, boot_id="rebooted-after-pointer-recovery")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: finalizer_owner)
    acquired: list[tuple[str, str]] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery

    def acquire(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        acquired.append(
            (
                str(kwargs["attempt_sha256"]),
                str(attempt.grant.lease.to_dict()["operation"]),
            )
        )
        return attempt

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source recapture after exact pointer visibility"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("pointer rewrite during exact-current replay"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "recover",
        _forbidden_finalization("pointer recovery during exact-current replay"),
    )

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert acquired == [(attempt_sha256, "PROMOTE")]
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before


@pytest.mark.parametrize(
    "boundary",
    [
        f"staged-build:{REPO_UUID}:pending_durable",
        f"staged-build:{REPO_UUID}:previous_durable",
        f"staged-build:{REPO_UUID}:current_durable",
        f"generation:{GENERATION_ID}:staged_promoted_durable",
    ],
)
def test_process_death_during_staged_completion_recovers_then_cleans_exact_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    fault = _ArmedFault(boundary)
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    fault.armed = True

    _leave_promotion_grant_after_process_death(case, monkeypatch)

    retained = case.runtime.leases.inspect(REPO_UUID)
    retained_attempt = retained.staged_attempt_sha256
    retained_grant = cast(FencedLease, retained.leases["workspace"])
    assert retained_attempt is not None
    assert retained_grant.to_dict()["operation"] == "PROMOTE"
    prior_owner = case.runtime.leases.current_owner()
    rebooted_owner = replace(prior_owner, boot_id=f"rebooted-after-{boundary}")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted_owner)
    acquired_attempts: list[str] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery

    def acquire(*args: Any, **kwargs: Any):
        acquired_attempts.append(str(kwargs["attempt_sha256"]))
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source recapture after staged promotion commit"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("pointer rewrite after staged promotion commit"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "recover",
        _forbidden_finalization("pointer recovery after staged promotion commit"),
    )

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert acquired_attempts == [retained_attempt]
    _assert_promotion_terminal(case, proof)
    assert _semantic_evidence(case) == semantic_before


def test_projected_promoted_commit_rejects_semantic_substitution_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = f"staged-build:{REPO_UUID}:pending_durable"
    fault = _ArmedFault(boundary)
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    fault.armed = True

    _leave_promotion_grant_after_process_death(case, monkeypatch)

    workspace = case.harness.state_root / "workspaces" / REPO_UUID
    binding = _binding_path(case)
    binding.unlink()
    before = tree_snapshot(workspace)
    retained = case.runtime.leases.inspect(REPO_UUID)
    monkeypatch.setattr(
        case.runtime.generations,
        "recover_staged_build",
        _forbidden_finalization("staged recovery after semantic substitution"),
    )

    with pytest.raises((GenerationConflict, GenerationError)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert tree_snapshot(workspace) == before
    assert case.runtime.leases.inspect(REPO_UUID).canonical == retained.canonical


@pytest.mark.parametrize(
    ("substitution", "expected_message"),
    [
        ("selected_from_prior", "exact target-bound residue"),
        ("selected_from_last_good", "exact target-bound residue"),
        ("nonempty_quarantine", "exact target-bound residue"),
        ("disallowed_pointer_action", "exact target-bound residue"),
        ("disallowed_journal_actions", "exact target-bound residue"),
        ("candidate_generation", "exact target-bound residue"),
        ("candidate_receipt", "exact target-bound residue"),
        ("retained_revision", "changed the retained revision"),
    ],
)
def test_pointer_recovery_rejects_substituted_plan_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
    expected_message: str,
) -> None:
    fault = _ArmedFault("pointer:promoted:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    fault.armed = True
    _leave_promotion_grant_after_process_death(case, monkeypatch)
    retained = case.runtime.leases.inspect(REPO_UUID)
    attempt_sha256 = retained.staged_attempt_sha256
    assert attempt_sha256 is not None
    prior_owner = case.runtime.leases.current_owner()
    rebooted_owner = replace(
        prior_owner,
        boot_id=f"rebooted-for-{substitution}",
    )
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted_owner)
    recovery = case.runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        case.certified.request,
        attempt_sha256=attempt_sha256,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    assert recovery.grant.lease.to_dict()["operation"] == "POINTER_RECOVERY"
    before = tree_snapshot(case.harness.state_root)
    lease_before = case.runtime.leases.inspect(REPO_UUID).canonical
    real_analyze = case.runtime.pointers.analyze_repair
    analyze_calls = 0

    def substituted_plan(*args: Any, **kwargs: Any):
        nonlocal analyze_calls
        analyze_calls += 1
        plan = real_analyze(*args, **kwargs)
        if substitution == "selected_from_prior":
            substituted = replace(plan, selected_from="prior")
        elif substitution == "selected_from_last_good":
            substituted = replace(plan, selected_from="last_good")
        elif substitution == "nonempty_quarantine":
            substituted = replace(plan, quarantine=(case.request.generation_id,))
        elif substitution == "disallowed_pointer_action":
            substituted = replace(plan, pointer_action="none")
        elif substitution == "disallowed_journal_actions":
            substituted = replace(
                plan,
                journal_actions=(*plan.journal_actions, "append_rollback"),
            )
        elif substitution == "candidate_generation":
            substituted = replace(
                plan,
                candidate={
                    **plan.candidate,
                    "generation_id": "gen-substituted-pointer-recovery",
                },
            )
        elif substitution == "candidate_receipt":
            substituted = replace(
                plan,
                candidate={
                    **plan.candidate,
                    "receipt_sha256": hashlib.sha256(
                        b"substituted-pointer-recovery-receipt"
                    ).hexdigest(),
                },
            )
        else:
            substituted = replace(
                plan,
                pointer_action="resume_pending",
                next_pointer_revision=plan.next_pointer_revision + 1,
            )
        canonical_json_bytes(substituted.to_dict())
        return substituted

    monkeypatch.setattr(case.runtime.pointers, "analyze_repair", substituted_plan)
    recover_called = False

    def recover(*_args: Any, **_kwargs: Any):
        nonlocal recover_called
        recover_called = True
        raise AssertionError("substituted pointer recovery reached PointerStore.recover")

    monkeypatch.setattr(case.runtime.pointers, "recover", recover)
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source observation during pointer recovery"),
    )
    with pytest.raises(GenerationConflict, match=expected_message):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert analyze_calls == 1
    assert not recover_called
    assert tree_snapshot(case.harness.state_root) == before
    assert case.runtime.leases.inspect(REPO_UUID).canonical == lease_before


def test_pointer_recovery_revalidates_handoff_before_staged_promoted_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("pointer:promoted:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    workspace = case.harness.state_root / "workspaces" / REPO_UUID
    binding = _binding_path(case)
    binding_before = binding.read_bytes()
    handoff = _handoff_path(
        case.harness,
        case.runtime,
        case.request,
    )
    generation_before = tree_snapshot(
        workspace / "generations" / case.request.generation_id
    )
    fault.armed = True

    with pytest.raises((CommitUnknown, InjectedFault)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert fault.fired
    assert (workspace / "pointers.pending.json").is_file()
    real_recover = case.runtime.pointers.recover

    def recover_then_remove_handoff(*args: Any, **kwargs: Any):
        pointer = real_recover(*args, **kwargs)
        handoff.unlink()
        return pointer

    monkeypatch.setattr(case.runtime.pointers, "recover", recover_then_remove_handoff)
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source recapture during pointer recovery"),
    )

    with pytest.raises((GenerationConflict, SemanticHandoffConflict)):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "CERTIFIED"
    pointer = case.runtime.pointers.load(REPO_UUID)
    assert pointer is not None
    assert pointer.to_dict()["current"] == {
        "generation_id": case.request.generation_id,
        "receipt_sha256": case.receipt.sha256,
    }
    assert not (workspace / "pointers.pending.json").exists()
    assert tree_snapshot(
        workspace / "generations" / case.request.generation_id
    ) == generation_before
    assert binding.read_bytes() == binding_before
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None


@pytest.mark.parametrize("pointer_recovery", [False, True])
def test_finalizer_adopts_only_the_exact_retained_promoted_grant_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_recovery: bool,
) -> None:
    case = _promoted_cleanup_case(
        tmp_path,
        monkeypatch,
        pointer_recovery=pointer_recovery,
    )
    before = _terminal_evidence(case)
    attempts: list[str] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery

    def acquire(*args: Any, **kwargs: Any):
        attempts.append(str(kwargs["attempt_sha256"]))
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)
    monkeypatch.setattr(
        case.runtime.generations,
        "complete_staged_promotion",
        _forbidden_finalization("staged rewrite during terminal cleanup"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "promote",
        _forbidden_finalization("pointer move during terminal cleanup"),
    )
    monkeypatch.setattr(
        case.runtime.pointers,
        "recover",
        _forbidden_finalization("pointer recovery during terminal cleanup"),
    )
    monkeypatch.setattr(
        case.runtime.generations.adapter,
        "observe",
        _forbidden_finalization("source observation during terminal cleanup"),
    )

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert attempts == [case.attempt_sha256]
    assert proof.target_generation_id == GENERATION_ID
    assert proof.receipt_sha256 == case.promoted.receipt_sha256
    assert proof.pointer_revision == case.promoted.pointer_revision
    assert proof.pointer_operation_epoch == case.promoted.operation_epoch
    assert proof.pointer_fence_token == case.promoted.fence_token
    assert _terminal_evidence(case) == before
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None


@pytest.mark.parametrize("pointer_recovery", [False, True])
def test_finalizer_replaces_rebooted_terminal_cleanup_without_rewriting_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_recovery: bool,
) -> None:
    case = _promoted_cleanup_case(
        tmp_path,
        monkeypatch,
        pointer_recovery=pointer_recovery,
    )
    before = _terminal_evidence(case)
    prior_owner = case.runtime.leases.current_owner()
    rebooted_owner = replace(prior_owner, boot_id="rebooted-terminal-cleanup-owner")
    monkeypatch.setattr(case.runtime.leases, "current_owner", lambda: rebooted_owner)
    acquired: list[LeaseGrant] = []
    real_acquire = case.runtime.generations.acquire_staged_recovery

    def acquire(*args: Any, **kwargs: Any):
        attempt = real_acquire(*args, **kwargs)
        acquired.append(attempt.grant)
        return attempt

    monkeypatch.setattr(case.runtime.generations, "acquire_staged_recovery", acquire)

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert len(acquired) == 1
    assert acquired[0].operation_epoch == case.retained_grant.operation_epoch + 1
    assert proof.pointer_operation_epoch == case.promoted.operation_epoch
    assert proof.pointer_fence_token == case.promoted.fence_token
    assert _terminal_evidence(case) == before
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None
    replay = workspace_sync._finalize_semantic_generation_promotion(
        _compose(case.harness),
        case.request,
    )
    assert replay == proof


@pytest.mark.parametrize("release_mode", ["before_commit", "after_commit"])
def test_promotion_release_commit_unknown_retries_or_adopts_only_exact_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
) -> None:
    case = _certified_promotion_case(tmp_path, monkeypatch)
    real_release = case.runtime.leases.release
    calls: list[LeaseGrant] = []

    def release_then_unknown(grant: LeaseGrant, **kwargs: Any):
        calls.append(grant)
        if release_mode == "before_commit" and len(calls) == 1:
            raise CommitUnknown("promotion release outcome is unknown before commit")
        released = real_release(grant, **kwargs)
        if release_mode == "after_commit" and len(calls) == 1:
            raise CommitUnknown("promotion release acknowledgement was lost")
        return released

    monkeypatch.setattr(case.runtime.leases, "release", release_then_unknown)

    proof = workspace_sync._finalize_semantic_generation_promotion(
        case.runtime,
        case.request,
    )

    assert len(calls) == (2 if release_mode == "before_commit" else 1)
    assert all(grant == calls[0] for grant in calls)
    _assert_promotion_terminal(case, proof)


@pytest.mark.parametrize(
    "boundary",
    _WORKSPACE_COMMIT_BOUNDARIES,
)
@pytest.mark.parametrize(
    "case_kind",
    ["direct", "cleanup-promote", "cleanup-pointer-recovery"],
)
def test_promotion_release_recovers_each_durable_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    case_kind: str,
) -> None:
    fault = _ArmedFault(boundary)
    case: _CertifiedPromotionCase | _PromotedCleanupCase
    if case_kind == "direct":
        case = _certified_promotion_case(
            tmp_path,
            monkeypatch,
            fault_hook=fault,
        )
        evidence_before = _semantic_evidence(case)
    else:
        case = _promoted_cleanup_case(
            tmp_path,
            monkeypatch,
            pointer_recovery=case_kind == "cleanup-pointer-recovery",
            fault_hook=fault,
        )
        evidence_before = _terminal_evidence(case)

    def release_before_process_death(
        runtime: WorkspaceRuntime,
        grant: LeaseGrant,
        *,
        attempt_sha256: str,
    ) -> None:
        del attempt_sha256
        fault.armed = True
        try:
            runtime.leases.release(grant)
        except CommitUnknown as exc:
            raise InjectedFault(f"process died during release at {boundary}") from exc

    monkeypatch.setattr(
        workspace_sync,
        "_release_semantic_promotion_grant",
        release_before_process_death,
    )

    with pytest.raises(InjectedFault, match="process died during release"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert fault.fired
    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "PROMOTED"
    restarted = replace(case, runtime=_compose(case.harness))
    proof = workspace_sync._finalize_semantic_generation_promotion(
        restarted.runtime,
        restarted.request,
    )

    if isinstance(restarted, _CertifiedPromotionCase):
        _assert_promotion_terminal(restarted, proof)
        assert _semantic_evidence(restarted) == evidence_before
    else:
        assert proof.target_generation_id == restarted.request.generation_id
        assert proof.receipt_sha256 == restarted.promoted.receipt_sha256
        assert proof.pointer_revision == restarted.promoted.pointer_revision
        assert proof.pointer_operation_epoch == restarted.promoted.operation_epoch
        assert proof.pointer_fence_token == restarted.promoted.fence_token
        assert _terminal_evidence(restarted) == evidence_before
        released = restarted.runtime.leases.inspect(REPO_UUID)
        assert released.leases.get("workspace") is None
        assert released.staged_attempt_sha256 is None
    assert not (
        _workspace_root(case) / "workspace.pending.json"
    ).exists()


@pytest.mark.parametrize("uncertain_read", [1, 2])
def test_promotion_release_recovery_converts_staged_read_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uncertain_read: int,
) -> None:
    fault = _ArmedFault("workspace:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)

    def release_before_process_death(
        runtime: WorkspaceRuntime,
        grant: LeaseGrant,
        *,
        attempt_sha256: str,
    ) -> None:
        del attempt_sha256
        fault.armed = True
        try:
            runtime.leases.release(grant)
        except CommitUnknown as exc:
            raise InjectedFault("process died during release") from exc

    monkeypatch.setattr(
        workspace_sync,
        "_release_semantic_promotion_grant",
        release_before_process_death,
    )
    with pytest.raises(InjectedFault, match="process died during release"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    pending = _workspace_root(case) / "workspace.pending.json"
    pending_before = pending.read_bytes()
    restarted = replace(case, runtime=_compose(case.harness))
    real_read = restarted.runtime.generations.read_only_staged_build_locked
    recovery_error = StagedBuildReadRecoveryRequired(
        "concurrent staged pending commit"
    )
    reads = 0

    def staged_read(*args: Any, **kwargs: Any):
        nonlocal reads
        reads += 1
        if reads == uncertain_read:
            raise recovery_error
        return real_read(*args, **kwargs)

    monkeypatch.setattr(
        restarted.runtime.generations,
        "read_only_staged_build_locked",
        staged_read,
    )
    if uncertain_read == 1:
        monkeypatch.setattr(
            restarted.runtime.leases,
            "recover_uncertain_snapshot",
            _forbidden_finalization("release recovery before staged proof"),
        )

    with pytest.raises(
        CommitUnknown,
        match="promotion release recovery did not converge",
    ) as raised:
        workspace_sync._semantic_promotion_terminal_after_release(
            restarted.runtime,
            restarted.request,
            expected_entry=None,
        )

    assert raised.value.__cause__ is recovery_error
    assert reads == uncertain_read
    if uncertain_read == 1:
        assert pending.read_bytes() == pending_before
    else:
        assert not pending.exists()
    assert _semantic_evidence(restarted) == semantic_before


def test_promotion_release_rejects_substituted_pending_outcome_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("workspace:pending_durable")
    case = _certified_promotion_case(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    semantic_before = _semantic_evidence(case)
    real_release = workspace_sync._release_semantic_promotion_grant
    real_inspect = workspace_sync._inspect_promotion_grant_release
    pending = _workspace_root(case) / "workspace.pending.json"
    substituted: bytes | None = None

    def release_at_boundary(
        runtime: WorkspaceRuntime,
        grant: LeaseGrant,
        *,
        attempt_sha256: str,
    ) -> None:
        fault.armed = True
        real_release(
            runtime,
            grant,
            attempt_sha256=attempt_sha256,
        )

    def inspect_after_substitution(
        runtime: WorkspaceRuntime,
        grant: LeaseGrant,
        *,
        attempt_sha256: str,
    ) -> str:
        nonlocal substituted
        projected = WorkspaceLeaseState.from_json(pending.read_bytes())
        substituted = replace(
            projected,
            migration_epoch=projected.migration_epoch + 1,
        ).canonical
        pending.write_bytes(substituted)
        return real_inspect(
            runtime,
            grant,
            attempt_sha256=attempt_sha256,
        )

    monkeypatch.setattr(
        workspace_sync,
        "_release_semantic_promotion_grant",
        release_at_boundary,
    )
    monkeypatch.setattr(
        workspace_sync,
        "_inspect_promotion_grant_release",
        inspect_after_substitution,
    )

    with pytest.raises(CommitUnknown, match="release"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert fault.fired
    assert substituted is not None
    assert pending.read_bytes() == substituted
    assert _semantic_evidence(case) == semantic_before


@pytest.mark.parametrize("retained_cleanup", [False, True])
def test_terminal_release_rejects_later_unrelated_fenced_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retained_cleanup: bool,
) -> None:
    case = (
        _promoted_cleanup_case(tmp_path, monkeypatch)
        if retained_cleanup
        else _certified_promotion_case(tmp_path, monkeypatch)
    )
    real_release = workspace_sync._release_semantic_promotion_grant
    later_grants: list[LeaseGrant] = []

    def release_then_advance_authority(
        runtime: WorkspaceRuntime,
        grant: LeaseGrant,
        *,
        attempt_sha256: str,
    ) -> None:
        real_release(
            runtime,
            grant,
            attempt_sha256=attempt_sha256,
        )
        released = runtime.leases.inspect(REPO_UUID)
        unrelated = runtime.leases.acquire(
            REPO_UUID,
            "SEMANTIC_CLAIM",
            runtime.leases.current_owner(),
            expected_registry_revision=case.request.expected_registry_revision,
            expected_active_source_revision=(
                case.request.expected_active_source_revision
            ),
            expected_operation_epoch=released.operation_epoch,
            expected_migration_epoch=released.migration_epoch,
            acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(),
            ttl_ns=60_000_000_000,
        )
        later_grants.append(unrelated)
        runtime.leases.release(unrelated)

    monkeypatch.setattr(
        workspace_sync,
        "_release_semantic_promotion_grant",
        release_then_advance_authority,
    )

    with pytest.raises(CommitUnknown, match="replacement authority"):
        workspace_sync._finalize_semantic_generation_promotion(
            case.runtime,
            case.request,
        )

    assert len(later_grants) == 1
    staged = case.runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "PROMOTED"
    released = case.runtime.leases.inspect(REPO_UUID)
    assert released.operation_epoch == later_grants[0].operation_epoch
    assert released.fence_high_watermark == int(
        later_grants[0].lease.to_dict()["fence_token"]
    )
    assert released.leases.get("workspace") is None
    assert released.staged_attempt_sha256 is None
    before_replay = tree_snapshot(case.harness.state_root)
    restarted = _compose(case.harness)
    with pytest.raises(CommitUnknown, match="release predecessor"):
        workspace_sync._finalize_semantic_generation_promotion(
            restarted,
            case.request,
        )
    assert tree_snapshot(case.harness.state_root) == before_replay
