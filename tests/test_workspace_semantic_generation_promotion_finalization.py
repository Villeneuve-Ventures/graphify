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
    StagedBuildState,
    StructuralBuildRequest,
    WorkspaceLeaseState,
    canonical_json_bytes,
)
from graphify.workspace.generations import GenerationConflict, GenerationError
from graphify.workspace.journal import JournalCorrupt
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseGrant,
    LeaseRecoveryRequired,
)
from graphify.workspace.persistence import (
    InjectedFault,
    LockTimeout,
    StateCorrupt,
    StatePathError,
)
from graphify.workspace.sync import SyncRequest
from tests.test_workspace_semantic_generation_certification_finalization import (
    GENERATION_ID,
    _complete_handoff,
)
from tests.test_workspace_semantic_result_handoff import _handoff_path
from tests.test_workspace_staged_build_abandonment import (
    _advance_active_source_revision,
)
from tests.workspace_p3_helpers import REPO_UUID, RuntimeHarness, tree_snapshot


@dataclass(frozen=True)
class _PromotedCleanupCase:
    harness: RuntimeHarness
    runtime: WorkspaceRuntime
    request: SyncRequest
    structural_request: StructuralBuildRequest
    promoted: StagedBuildState
    attempt_sha256: str
    retained_grant: LeaseGrant


def _workspace_root(case: _PromotedCleanupCase) -> Path:
    return case.harness.state_root / "workspaces" / REPO_UUID


def _binding_path(case: _PromotedCleanupCase) -> Path:
    return _workspace_root(case) / "queue" / "certifications" / f"{GENERATION_ID}.json"


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


def _promoted_cleanup_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pointer_recovery: bool = False,
) -> _PromotedCleanupCase:
    harness, runtime, request, _, _ = _complete_handoff(
        tmp_path,
        monkeypatch,
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
    handoff = _handoff_path(case.harness, case.runtime, case.request)
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
