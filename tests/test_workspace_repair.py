"""Contract tests for explicit, fenced workspace pointer repair.

The repair surface intentionally has no CLI dependency: callers first obtain a
canonical, read-only public preview and then present that preview's SHA-256 with
a fresh REPAIR lease and explicit REPAIR_EXECUTE authorization.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import time

import pytest

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace import persistence as persistence_module
from graphify.workspace.composition import WorkspaceRuntime
from graphify.workspace.generations import GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import (
    GcIntentRecoveryRequired,
    LeaseGrant,
    StagedBuildLeaseRecoveryRequired,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    StatePathError,
    StateRecoveryRequired,
)
from graphify.workspace.pointers import PointerCorrupt, PointerRepairPlan, PointerSet, PointerStore
from graphify.workspace.repair import (
    RepairAuthorization,
    RepairError,
    RepairExecuteRequest,
    RepairObservedAuthority,
    RepairPlan,
    RepairPlanChanged,
    RepairPreviewRequest,
    RepairPreviewResult,
    WorkspaceRepair,
    classify_failure,
    repair_execute,
    repair_preview,
)
from graphify.workspace.semantic_queue import SemanticQueueError

from tests.test_workspace_pointers import POLICY, _cas, _certify, _semantic_queue
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    START,
    acquire,
    create_harness,
    tree_snapshot,
)


def _runtime(
    tmp_path: Path,
    *,
    fault_hook: Any = None,
) -> tuple[Any, JournalStore, GenerationStore, PointerStore, Any]:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        semantic_queue=_semantic_queue(harness),
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    old = _certify(generations, build, "gen-old", "old\n", monotonic_ns=10_001)
    new = _certify(generations, build, "gen-new", "new\n", monotonic_ns=10_011)
    racer = _certify(generations, build, "gen-racer", "racer\n", monotonic_ns=10_021)
    harness.leases.release(build)
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
    )
    return harness, journal, generations, pointers, (old, new, racer)


def _repair(
    harness: Any, journal: JournalStore, generations: GenerationStore, pointers: PointerStore
) -> WorkspaceRepair:
    return WorkspaceRepair(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        pointers,
        journal,
        capabilities=harness.leases.state.capabilities,
    )


def _request(harness: Any, *, timeout_ns: int = 100_000) -> RepairPreviewRequest:
    registry = harness.registry.load().to_dict()
    entry = registry["workspaces"][0]
    lease = harness.leases.inspect(REPO_UUID)
    return RepairPreviewRequest(
        repo_uuid=REPO_UUID,
        expected_registry_revision=int(registry["revision"]),
        expected_active_source_revision=int(entry["active_source_revision"]),
        expected_operation_epoch=lease.operation_epoch,
        expected_migration_epoch=lease.migration_epoch,
        timeout_ns=timeout_ns,
    )


def _authorization() -> RepairAuthorization:
    return RepairAuthorization(
        action="REPAIR_EXECUTE",
        operator_id="operator:p5b2-repair-test",
        reason="repair canonical preview",
        issued_at="2026-07-28T19:00:00Z",
        nonce="repair-execute-1",
    )


def _approved_preview_sha256(request: RepairPreviewRequest, plan: RepairPlan) -> str:
    return RepairPreviewResult(
        repo_uuid=request.repo_uuid,
        request_sha256=request.request_sha256,
        observed_authority=RepairObservedAuthority.from_request(request),
        plan=plan,
    ).sha256


def _promote(pointers: PointerStore, harness: Any, receipt: Any) -> None:
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers.promote(
        promote,
        _cas(promote, receipt, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)


def test_preview_is_read_only_and_returns_a_deterministic_no_op_plan(tmp_path: Path) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    before_tree = tree_snapshot(harness.state_root)

    preview = repair.preview(_request(harness), monotonic_ns=30_001)

    payload = preview.to_dict()
    decision_sha256 = payload.pop("decision_sha256")
    assert isinstance(decision_sha256, str)
    assert len(decision_sha256) == 64
    assert set(decision_sha256) <= set("0123456789abcdef")
    assert payload == {
        "classification": "no_op",
        "candidate": {"generation_id": "gen-old", "receipt_sha256": receipts[0].sha256},
        "last_good": None,
        "next_pointer_revision": 1,
        "selected_from": "current",
        "pointer_action": "none",
        "journal_actions": [],
        "quarantine": [],
    }
    assert tree_snapshot(harness.state_root) == before_tree


def test_preview_selects_verified_pending_candidate_and_plans_repair(tmp_path: Path) -> None:
    failpoint = "pointer:promoted:pending_durable"

    def interrupt_pending(event: str) -> None:
        if event == failpoint:
            raise InjectedFault(event)

    harness, journal, generations, pointers, receipts = _runtime(
        tmp_path, fault_hook=interrupt_pending
    )
    promote = acquire(harness, "PROMOTE", tick=2)
    with pytest.raises(InjectedFault, match="pending_durable"):
        pointers.promote(
            promote,
            _cas(promote, receipts[0], revision=0, current_sha256=None),
            occurred_at=START,
            monotonic_ns=20_001,
        )
    harness.leases.release(promote)
    repair = _repair(harness, journal, generations, pointers)
    before_tree = tree_snapshot(harness.state_root)

    preview = repair.preview(_request(harness), monotonic_ns=30_001)

    payload = preview.to_dict()
    assert payload["classification"] == "repairable"
    assert payload["candidate"] == {
        "generation_id": "gen-old",
        "receipt_sha256": receipts[0].sha256,
    }
    assert payload["selected_from"] == "pending"
    assert payload["pointer_action"] == "replace"
    assert payload["next_pointer_revision"] == 2
    assert tree_snapshot(harness.state_root) == before_tree


def test_preview_reports_irreparable_when_no_pointer_reference_verifies(tmp_path: Path) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    payload = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / "gen-old"
        / "graphify-out"
        / "graph.json"
    )
    payload.write_text("corrupt\n", encoding="utf-8")
    repair = _repair(harness, journal, generations, pointers)
    before_tree = tree_snapshot(harness.state_root)

    preview = repair.preview(_request(harness), monotonic_ns=30_001)

    assert preview.to_dict()["classification"] == "irreparable"
    assert preview.to_dict()["candidate"] is None
    assert tree_snapshot(harness.state_root) == before_tree


def test_preview_routes_gc_intent_outside_pointer_repair_without_writes(
    tmp_path: Path,
) -> None:
    from tests.test_workspace_status import _gc_intent

    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    pointer = pointers.load(REPO_UUID, allow_missing=False)
    assert pointer is not None
    intent = _gc_intent(pointer, capacity_policy_sha256=POLICY.sha256)
    gc_directory = harness.state_root / "workspaces" / REPO_UUID / "gc"
    gc_directory.mkdir(mode=0o700)
    intent_path = gc_directory / "intent.json"
    intent_path.write_bytes(intent.canonical)
    intent_path.chmod(0o600)
    repair = _repair(harness, journal, generations, pointers)
    before_tree = tree_snapshot(harness.state_root)

    with pytest.raises(RepairError) as raised:
        repair.preview(_request(harness), monotonic_ns=30_001)

    failure = classify_failure(raised.value, "preview")
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == "run_workspace_gc_reconcile"
    assert tree_snapshot(harness.state_root) == before_tree


def test_preview_routes_nonterminal_staged_build_outside_pointer_repair_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    monkeypatch.setattr(
        generations,
        "read_only_staged_build_locked",
        lambda *_args, **_kwargs: SimpleNamespace(lifecycle_state="REQUESTED"),
    )
    before_tree = tree_snapshot(harness.state_root)

    with pytest.raises(RepairError) as raised:
        repair.preview(_request(harness), monotonic_ns=30_001)

    failure = classify_failure(raised.value, "preview")
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == "resume_exact_workspace_sync"
    assert tree_snapshot(harness.state_root) == before_tree


def test_pointer_repair_analysis_preserves_unsafe_gc_intent_path() -> None:
    pointers = cast(Any, object.__new__(PointerStore))

    def reject_unsafe_path(_relative: Path) -> bool:
        raise StatePathError("private unsafe GC intent path")

    pointers.state = SimpleNamespace(private_file_exists=reject_unsafe_path)

    with pytest.raises(StatePathError, match="unsafe GC intent"):
        pointers.analyze_repair(
            REPO_UUID,
            active_source_revision=1,
        )


def test_pointer_repair_rejects_foreign_workspace_pointer_even_when_invalid_is_allowed() -> None:
    pointers = cast(Any, object.__new__(PointerStore))
    foreign = PointerSet.from_mapping(
        {
            "contract": "graphify.workspace.pointer_set",
            "schema_version": 1,
            "repo_uuid": "22222222-2222-4222-8222-222222222222",
            "pointer_revision": 1,
            "active_source_revision": 1,
            "source_epoch": 1,
            "operation_epoch": 1,
            "fence_token": 1,
            "state_schema_version": 1,
            "current": {
                "generation_id": "gen-foreign",
                "receipt_sha256": "a" * 64,
            },
            "last_good": None,
        }
    )
    pointers.state = SimpleNamespace(
        private_file_exists=lambda _relative: True,
        read_existing_bytes=lambda *_args, **_kwargs: foreign.canonical,
    )

    with pytest.raises(PointerCorrupt, match="another workspace"):
        pointers._read_repair_pointer(
            Path("workspaces") / REPO_UUID / "pointers.json",
            repo_uuid=REPO_UUID,
            allow_missing=True,
            allow_invalid=True,
            deadline_ns=None,
        )


def test_preview_rejects_corrupt_semantic_queue_without_writes(tmp_path: Path) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    semantic_queue = generations.semantic_queue
    assert semantic_queue is not None
    current, _previous, _pending = semantic_queue._paths(REPO_UUID)
    queue_path = semantic_queue.state.path(current)
    queue_path.write_bytes(b"{}\n")
    queue_path.chmod(0o600)
    repair = _repair(harness, journal, generations, pointers)
    before_tree = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticQueueError) as raised:
        repair.preview(_request(harness), monotonic_ns=30_001)

    failure = classify_failure(raised.value, "preview")
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == "inspect_semantic_queue"
    assert tree_snapshot(harness.state_root) == before_tree


def test_preview_requires_every_referenced_generation_lock(tmp_path: Path) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    generation_lock = generations.state.path(generations._lock(REPO_UUID, "gen-old"))
    generation_lock.unlink()
    repair = _repair(harness, journal, generations, pointers)
    before_tree = tree_snapshot(harness.state_root)

    preview = repair.preview(_request(harness), monotonic_ns=30_001)

    assert preview.to_dict()["classification"] == "irreparable"
    assert tree_snapshot(harness.state_root) == before_tree


@pytest.mark.parametrize(
    "probe_kind",
    ("current_pointer", "prior_pointer", "generation_lock"),
)
def test_repair_analysis_rechecks_deadline_after_existing_only_path_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_kind: str,
) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    target = {
        "current_pointer": pointers._current(REPO_UUID),
        "prior_pointer": pointers._prior(REPO_UUID),
        "generation_lock": generations._lock(REPO_UUID, "gen-old"),
    }[probe_kind]
    original_probe = pointers.state.private_file_exists
    expired = False

    def expiring_probe(relative: str | Path) -> bool:
        nonlocal expired
        if Path(relative) != target:
            return original_probe(relative)
        exists = original_probe(relative) if probe_kind == "generation_lock" else False
        expired = True
        return exists

    deadline_ns = 500
    monkeypatch.setattr(pointers.state, "private_file_exists", expiring_probe)
    monkeypatch.setattr(
        persistence_module.time,
        "monotonic_ns",
        lambda: deadline_ns if expired else deadline_ns - 1,
    )
    before_tree = tree_snapshot(harness.state_root)

    with pytest.raises(LockTimeout) as raised:
        if probe_kind == "current_pointer":
            pointers._read_repair_pointer(
                target,
                repo_uuid=REPO_UUID,
                allow_missing=True,
                allow_invalid=True,
                deadline_ns=deadline_ns,
            )
        elif probe_kind == "prior_pointer":
            pointers._read_repair_prior(REPO_UUID, deadline_ns=deadline_ns)
        else:
            repair.preview(request, deadline_ns=deadline_ns)

    failure = classify_failure(raised.value, "preview")
    assert failure.reason_code == "repair_lease_busy"
    assert failure.action_code == "retry_workspace_repair"
    assert tree_snapshot(harness.state_root) == before_tree


def test_execute_rejects_changed_plan_before_pointer_mutation(tmp_path: Path) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    before_tree = tree_snapshot(harness.state_root)
    stale = replace(request, expected_operation_epoch=request.expected_operation_epoch + 1)

    with pytest.raises(RepairPlanChanged, match="canonical preview no longer matches"):
        repair.execute(
            stale,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )

    assert tree_snapshot(harness.state_root) == before_tree


def test_execute_revalidates_semantic_queue_after_repair_fence_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    semantic_queue = generations.semantic_queue
    assert semantic_queue is not None
    current, _previous, _pending = semantic_queue._paths(REPO_UUID)
    queue_path = semantic_queue.state.path(current)
    pointer_path = pointers.state.path(pointers._current(REPO_UUID))
    pointer_before = pointer_path.read_bytes()
    original_acquire = harness.leases.acquire
    original_analysis = pointers._repair_analysis_locked
    fence_acquired = False

    def corrupt_queue_before_acquire(*args: Any, **kwargs: Any) -> LeaseGrant:
        nonlocal fence_acquired
        queue_path.write_bytes(b"{}\n")
        queue_path.chmod(0o600)
        grant = original_acquire(*args, **kwargs)
        fence_acquired = True
        return grant

    def reject_pointer_analysis_after_fence(*args: Any, **kwargs: Any) -> Any:
        if fence_acquired:
            raise AssertionError("pointer analysis ran before semantic-queue revalidation")
        return original_analysis(*args, **kwargs)

    monkeypatch.setattr(harness.leases, "acquire", corrupt_queue_before_acquire)
    monkeypatch.setattr(pointers, "_repair_analysis_locked", reject_pointer_analysis_after_fence)

    with pytest.raises(SemanticQueueError) as raised:
        repair.execute(
            request,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )

    failure = classify_failure(raised.value, "execute")
    assert fence_acquired is True
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == "inspect_semantic_queue"
    assert pointer_path.read_bytes() == pointer_before


def test_execute_routes_gc_intent_created_before_repair_lease_to_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_status import _gc_intent

    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    pointer = pointers.load(REPO_UUID, allow_missing=False)
    assert pointer is not None
    intent = _gc_intent(pointer, capacity_policy_sha256=POLICY.sha256)
    intent_path = harness.state_root / "workspaces" / REPO_UUID / "gc" / "intent.json"
    pointer_path = harness.state_root / "workspaces" / REPO_UUID / "pointers.json"
    pointer_before = pointer_path.read_bytes()
    operation_epoch_before = harness.leases.inspect(REPO_UUID).operation_epoch
    original_acquire = harness.leases.acquire

    def create_gc_intent_before_acquire(*args: Any, **kwargs: Any) -> LeaseGrant:
        intent_path.parent.mkdir(mode=0o700)
        intent_path.write_bytes(intent.canonical)
        intent_path.chmod(0o600)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(harness.leases, "acquire", create_gc_intent_before_acquire)

    with pytest.raises(GcIntentRecoveryRequired) as raised:
        repair.execute(
            request,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )

    failure = classify_failure(raised.value, "execute")
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == "run_workspace_gc_reconcile"
    assert pointer_path.read_bytes() == pointer_before
    assert harness.leases.inspect(REPO_UUID).operation_epoch == operation_epoch_before


def test_execute_routes_staged_build_created_before_repair_lease_to_exact_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    pointer_path = harness.state_root / "workspaces" / REPO_UUID / "pointers.json"
    pointer_before = pointer_path.read_bytes()
    operation_epoch_before = harness.leases.inspect(REPO_UUID).operation_epoch
    original_acquire = harness.leases.acquire

    def create_staged_build_before_acquire(*args: Any, **kwargs: Any) -> LeaseGrant:
        monkeypatch.setattr(
            harness.leases,
            "_load_staged_build_locked",
            lambda *_args, **_kwargs: SimpleNamespace(
                lifecycle_state="REQUESTED",
                abandonment_intent=None,
            ),
        )
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(harness.leases, "acquire", create_staged_build_before_acquire)

    with pytest.raises(StagedBuildLeaseRecoveryRequired) as raised:
        repair.execute(
            request,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )

    failure = classify_failure(raised.value, "execute")
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == "resume_exact_workspace_sync"
    assert pointer_path.read_bytes() == pointer_before
    assert harness.leases.inspect(REPO_UUID).operation_epoch == operation_epoch_before


@pytest.mark.parametrize(
    ("record", "expected_error", "expected_action"),
    [
        ("registry", StateRecoveryRequired, "inspect_workspace_state"),
        ("workspace", StateRecoveryRequired, "inspect_workspace_state"),
        ("staged_build", StagedBuildLeaseRecoveryRequired, "resume_exact_workspace_sync"),
    ],
)
def test_execute_does_not_recover_non_pointer_pending_state_before_repair_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: str,
    expected_error: type[Exception],
    expected_action: str,
) -> None:
    from tests.test_workspace_status import _fresh_runtime_with_staged_request

    staged_payload: bytes | None = None
    if record == "staged_build":
        _fresh, staged_runtime, _request_value = _fresh_runtime_with_staged_request(
            tmp_path / "staged-fixture"
        )
        staged_current, _staged_previous, _staged_pending = (
            staged_runtime.generations._staged_build_paths(REPO_UUID)
        )
        staged_payload = staged_runtime.generations.state.path(staged_current).read_bytes()

    harness, journal, generations, pointers, receipts = _runtime(tmp_path / "repair")
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    if record == "registry":
        current = harness.registry.state.path(harness.registry.CURRENT)
        pending = harness.registry.state.path(harness.registry.PENDING)
        pending_payload = current.read_bytes()
    elif record == "workspace":
        current_relative, _previous_relative, pending_relative = harness.leases._paths(REPO_UUID)
        current = harness.leases.state.path(current_relative)
        pending = harness.leases.state.path(pending_relative)
        pending_payload = current.read_bytes()
    else:
        _current_relative, _previous_relative, pending_relative = (
            harness.leases._staged_build_paths(REPO_UUID)
        )
        pending = harness.leases.state.path(pending_relative)
        assert staged_payload is not None
        pending_payload = staged_payload
    original_acquire = harness.leases.acquire
    before_attempt: dict[str, tuple[int, int, int, str | None]] | None = None

    def create_pending_record_before_acquire(*args: Any, **kwargs: Any) -> LeaseGrant:
        nonlocal before_attempt
        pending.write_bytes(pending_payload)
        pending.chmod(0o600)
        before_attempt = tree_snapshot(harness.state_root)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(harness.leases, "acquire", create_pending_record_before_acquire)

    with pytest.raises(expected_error) as raised:
        repair.execute(
            request,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )

    assert before_attempt is not None
    failure = classify_failure(raised.value, "execute")
    assert failure.reason_code == "repair_state_unsupported"
    assert failure.action_code == expected_action
    assert tree_snapshot(harness.state_root) == before_attempt


def test_library_execute_accepts_the_public_preview_result_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repair = cast(Any, object.__new__(WorkspaceRepair))
    request = RepairPreviewRequest(
        repo_uuid=REPO_UUID,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=0,
        expected_migration_epoch=0,
        timeout_ns=1_000_000_000,
    )
    plan = RepairPlan(
        classification="no_op",
        candidate={"generation_id": "gen-current", "receipt_sha256": "a" * 64},
        last_good=None,
        next_pointer_revision=1,
        selected_from="current",
        pointer_action="none",
        journal_actions=(),
        quarantine=(),
        decision_sha256="b" * 64,
        decision=cast(Any, object()),
    )
    execution = SimpleNamespace(plan=plan, pointer=None)
    monkeypatch.setattr(repair, "preview", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(repair, "_execute_plan", lambda *_args, **_kwargs: execution)
    approved_preview_sha256 = _approved_preview_sha256(request, plan)

    assert approved_preview_sha256 != plan.sha256
    assert (
        repair.execute(
            request,
            approved_preview_sha256=approved_preview_sha256,
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )
        is execution
    )


def test_public_execute_no_op_uses_a_fresh_repair_lease_and_pointer_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    runtime = cast(
        WorkspaceRuntime,
        SimpleNamespace(
            registry=harness.registry,
            leases=harness.leases,
            generations=generations,
            pointers=pointers,
            journal=journal,
        ),
    )
    request = _request(harness, timeout_ns=10_000_000_000)
    preview = repair_preview(runtime, request, monotonic_ns=30_001)
    assert preview.to_dict()["classification"] == "no_op"
    before_epoch = harness.leases.inspect(REPO_UUID).operation_epoch
    recover_calls: list[LeaseGrant] = []
    recover_deadlines: list[int | None] = []
    deadline_calls: list[int] = []
    deadline_ns = time.monotonic_ns() + 10_000_000_000
    original_recover = PointerStore.recover

    def fixed_deadline(_request: RepairPreviewRequest) -> int:
        deadline_calls.append(deadline_ns)
        return deadline_ns

    def observed_recover(
        store: PointerStore,
        grant: LeaseGrant,
        *,
        occurred_at: datetime,
        monotonic_ns: int,
        expected_plan: PointerRepairPlan | None = None,
        deadline_ns: int | None = None,
    ) -> PointerSet:
        recover_calls.append(grant)
        recover_deadlines.append(deadline_ns)
        return original_recover(
            store,
            grant,
            occurred_at=occurred_at,
            monotonic_ns=monotonic_ns,
            expected_plan=expected_plan,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(RepairPreviewRequest, "runtime_deadline", fixed_deadline)
    monkeypatch.setattr(PointerStore, "recover", observed_recover)
    result = repair_execute(
        runtime,
        RepairExecuteRequest(
            repo_uuid=request.repo_uuid,
            expected_registry_revision=request.expected_registry_revision,
            expected_active_source_revision=request.expected_active_source_revision,
            expected_operation_epoch=request.expected_operation_epoch,
            expected_migration_epoch=request.expected_migration_epoch,
            approved_preview_sha256=preview.sha256,
            authorization=_authorization(),
            timeout_ns=request.timeout_ns,
        ),
        occurred_at=START + timedelta(seconds=4),
        monotonic_clock=lambda: 40_001,
    )

    assert result.to_dict()["state"] == "no_op"
    assert deadline_calls == [deadline_ns]
    assert len(recover_calls) == 1
    assert recover_deadlines == [deadline_ns]
    assert recover_calls[0].lease.to_dict()["operation"] == "REPAIR"
    lease_state = harness.leases.inspect(REPO_UUID)
    assert lease_state.operation_epoch == before_epoch + 1
    assert lease_state.leases.get("workspace") is None


@pytest.mark.parametrize(
    ("error", "expected_state", "expected_exit", "expected_action"),
    [
        (
            StatePathError("private unsafe state"),
            "invalid",
            20,
            "configure_safe_state_root",
        ),
        (
            CommitUnknown("private commit uncertainty"),
            "invalid",
            20,
            "run_workspace_status_then_repair_dry_run",
        ),
        (
            LockTimeout(
                "private lock contention",
                phase="acquire",
                kind="workspace",
            ),
            "conflict",
            10,
            "retry_workspace_repair",
        ),
    ],
)
def test_execute_preserves_preview_failure_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_state: str,
    expected_exit: int,
    expected_action: str,
) -> None:
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    repair = _repair(harness, journal, generations, pointers)

    def fail_preview(*_args: object, **_kwargs: object) -> Any:
        raise error

    monkeypatch.setattr(repair, "preview", fail_preview)
    with pytest.raises(Exception) as raised:
        repair.execute(
            _request(harness),
            approved_preview_sha256="a" * 64,
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )

    assert raised.value is error
    failure = classify_failure(error, "execute")
    assert failure.state == expected_state
    assert failure.exit_code == expected_exit
    assert failure.action_code == expected_action


def test_failure_classifies_unsupported_compatibility_with_specific_guidance() -> None:
    failure = classify_failure(UnsupportedCompatibility("private compatibility detail"), "preview")

    assert failure.to_dict() == {
        "action_code": "install_supported_candidate",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.repair_preview_result",
        "exit_code": 20,
        "reason_code": "unsupported_compatibility",
        "schema_version": 1,
        "state": "invalid",
    }


@pytest.mark.parametrize(
    "failpoint",
    (
        "pointer:repaired:prior_durable",
        "pointer:repaired:pending_durable",
        "pointer:repaired:visible",
        "pointer:repaired:journal_durable",
    ),
)
def test_interrupted_execute_requires_fresh_preview_and_resumes_safely(
    tmp_path: Path,
    failpoint: str,
) -> None:
    active_failpoint: str | None = None

    def fail_once(event: str) -> None:
        nonlocal active_failpoint
        if event == active_failpoint:
            active_failpoint = None
            raise InjectedFault(event)

    harness, journal, generations, pointers, receipts = _runtime(tmp_path, fault_hook=fail_once)
    promote = acquire(harness, "PROMOTE", tick=2)
    with pytest.raises(InjectedFault, match="pending_durable"):
        active_failpoint = "pointer:promoted:pending_durable"
        pointers.promote(
            promote,
            _cas(promote, receipts[0], revision=0, current_sha256=None),
            occurred_at=START,
            monotonic_ns=20_001,
        )
    active_failpoint = None
    harness.leases.release(promote)
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    active_failpoint = failpoint

    with pytest.raises(InjectedFault):
        repair.execute(
            request,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=40_001,
        )
    with pytest.raises(RepairPlanChanged, match="canonical preview no longer matches"):
        repair.execute(
            request,
            approved_preview_sha256=_approved_preview_sha256(request, preview),
            authorization=_authorization(),
            occurred_at=START + timedelta(seconds=5),
            monotonic_ns=50_001,
        )
    fresh_request = _request(harness)
    fresh_preview = repair.preview(fresh_request, monotonic_ns=60_001)
    repaired = repair.execute(
        fresh_request,
        approved_preview_sha256=_approved_preview_sha256(fresh_request, fresh_preview),
        authorization=_authorization(),
        occurred_at=START + timedelta(seconds=6),
        monotonic_ns=70_001,
    )

    pointer = pointers.load(REPO_UUID)
    assert pointer is not None
    expected_revision = cast(int, preview.to_dict()["next_pointer_revision"])
    assert int(pointer.to_dict()["pointer_revision"]) >= expected_revision
    assert pointers.verify_pointer(pointer)["current"].sha256 == receipts[0].sha256
    assert pointers.state.path(generations._generation(REPO_UUID, "gen-old")).is_dir()
    assert repaired.to_dict()["classification"] == "repairable"


def test_execute_quarantines_only_corrupt_generation_excluded_from_repaired_pointer(
    tmp_path: Path,
) -> None:
    failpoint: str | None = None

    def interrupt_pending(event: str) -> None:
        if event == failpoint:
            raise InjectedFault(event)

    harness, journal, generations, pointers, receipts = _runtime(
        tmp_path,
        fault_hook=interrupt_pending,
    )
    old, current, racer = receipts
    promote = acquire(harness, "PROMOTE", tick=2)
    pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START,
        monotonic_ns=20_001,
    )
    pointers.promote(
        promote,
        _cas(promote, current, revision=1, current_sha256=old.sha256),
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_002,
    )
    failpoint = "pointer:promoted:pending_durable"
    with pytest.raises(InjectedFault, match="pending_durable"):
        pointers.promote(
            promote,
            _cas(promote, racer, revision=2, current_sha256=current.sha256),
            occurred_at=START + timedelta(seconds=2),
            monotonic_ns=20_003,
        )
    harness.leases.release(promote)
    corrupt_payload = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / "gen-new"
        / "graphify-out"
        / "graph.json"
    )
    corrupt_payload.write_text("corrupt\n", encoding="utf-8")
    repair = _repair(harness, journal, generations, pointers)
    request = _request(harness)
    preview = repair.preview(request, monotonic_ns=30_001)
    workspace = harness.state_root / "workspaces" / REPO_UUID
    unrelated_before = {
        "old": tree_snapshot(workspace / "generations" / "gen-old"),
        "racer": tree_snapshot(workspace / "generations" / "gen-racer"),
        "gc": tree_snapshot(workspace / "gc"),
        "staging": tree_snapshot(workspace / "staging"),
        "queue": tree_snapshot(workspace / "queue"),
    }

    assert preview.to_dict()["quarantine"] == ["gen-new"]

    repair.execute(
        request,
        approved_preview_sha256=_approved_preview_sha256(request, preview),
        authorization=_authorization(),
        occurred_at=START + timedelta(seconds=4),
        monotonic_ns=40_001,
    )

    assert pointers.state.path(generations._generation(REPO_UUID, "gen-old")).is_dir()
    assert pointers.state.path(generations._generation(REPO_UUID, "gen-racer")).is_dir()
    assert not pointers.state.path(generations._generation(REPO_UUID, "gen-new")).exists()
    assert tree_snapshot(workspace / "generations" / "gen-old") == unrelated_before["old"]
    assert tree_snapshot(workspace / "generations" / "gen-racer") == unrelated_before["racer"]
    assert tree_snapshot(workspace / "gc") == unrelated_before["gc"]
    assert tree_snapshot(workspace / "staging") == unrelated_before["staging"]
    assert tree_snapshot(workspace / "queue") == unrelated_before["queue"]
