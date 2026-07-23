"""P5B2b orchestration coverage for provider-neutral code-only sync."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from graphify.workspace.adapters.base import (
    ObservationHook,
    QueryRequest,
    SourceObservation,
    StructuralBuild,
)
from graphify.workspace.cli import run_workspace_command
from graphify.workspace.composition import (
    WorkspaceRuntime,
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
)
from graphify.workspace.contracts import canonical_json_bytes, canonical_sha256
from graphify.workspace.generations import (
    CapacityExceeded,
    GenerationConflict,
    GenerationError,
)
from graphify.workspace.identity import IdentityError, discover_source
from graphify.workspace.leases import LeaseBusy
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from graphify.workspace.registry import RevisionConflict
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from graphify.workspace.status import inspect_workspace_status
from graphify.workspace.sync import (
    SyncAuthorityConflict,
    SyncRequest,
    synchronize_code_only,
)
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    RuntimeHarness,
    SUPPORTED,
    authorization,
    create_harness,
    create_repo,
    metadata_snapshot,
    tree_snapshot,
)


POLICY = {
    "global_max_bytes": 32 * 1024 * 1024,
    "global_max_generations": 16,
    "workspace_max_bytes": 8 * 1024 * 1024,
    "workspace_max_generations": 8,
    "reserve_bytes": 1024,
}
GENERATION_ID = "gen-sync-integration"


class RecordingStructuralAdapter:
    """A local structural-only adapter that makes staging containment observable."""

    adapter_id = "test-sync-recording"
    engine_baseline = "test"
    detector_id = "test-sync-recording"

    def __init__(self, observation: SourceObservation, *, fail: bool = False) -> None:
        self.observation = observation
        self.fail = fail
        self.calls: list[tuple[Path, Path, Path]] = []

    def observe(
        self,
        source_root: Path,
        *,
        max_inventory_passes: int = 6,
        deadline_ns: int | None = None,
        hook: ObservationHook | None = None,
    ) -> SourceObservation:
        del source_root, max_inventory_passes, deadline_ns, hook
        return self.observation

    def build_structural(
        self,
        source_root: Path,
        *,
        output_root: Path,
        scratch_root: Path | None = None,
    ) -> StructuralBuild:
        if scratch_root is None:
            scratch_root = output_root
        self.calls.append((source_root, output_root, scratch_root))
        if self.fail:
            raise RuntimeError("intentional structural adapter failure")
        payload = output_root / "graphify-out"
        payload.mkdir()
        (payload / "graph.json").write_text("{}\n", encoding="utf-8")
        return StructuralBuild(
            engine_baseline=self.engine_baseline,
            node_count=0,
            edge_count=0,
            detected_code_files=(),
            omitted_dispatched_files=(),
        )

    def query_structural(self, payload_root: Path, request: QueryRequest) -> str:
        del payload_root, request
        return ""


def _inputs(
    harness: RuntimeHarness,
    *,
    fault_hook: Any = None,
) -> WorkspaceRuntimeInputs:
    return WorkspaceRuntimeInputs(
        state_root=harness.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=SemanticQueuePolicy(
            max_items=16,
            max_bytes=1024 * 1024,
            retry_budget=1,
        ),
        capabilities=harness.registry.state.capabilities,
        fault_hook=fault_hook,
    )


def _compose(
    harness: RuntimeHarness,
    *,
    fault_hook: Any = None,
) -> WorkspaceRuntime:
    return compose_workspace_runtime(_inputs(harness, fault_hook=fault_hook))


def _runtime(
    tmp_path: Path,
    *,
    fault_hook: Any = None,
) -> tuple[RuntimeHarness, WorkspaceRuntime]:
    harness = create_harness(tmp_path)
    return harness, _compose(harness, fault_hook=fault_hook)


def _adapter(harness: RuntimeHarness, *, fail: bool = False) -> RecordingStructuralAdapter:
    source = discover_source(harness.repo)
    observation = SourceObservation(
        source_commit=source.head_commit,
        inventory_sha256="c" * 64,
        policy_sha256="b" * 64,
        detector_id="test-sync-recording",
        stable_inventory_passes=2,
        entries=(),
    )
    return RecordingStructuralAdapter(observation, fail=fail)


def _request(
    runtime: WorkspaceRuntime,
    *,
    generation_id: str = GENERATION_ID,
    source_epoch: int = 1,
    semantic_desired_watermark: int = 1,
    expected_payload_bytes: int = 4096,
) -> SyncRequest:
    registry = runtime.registry.load().to_dict()
    entry = registry["workspaces"][0]
    lease_state = runtime.leases.inspect(REPO_UUID)
    pointer = runtime.pointers.load(REPO_UUID, allow_missing=True)
    pointer_revision = 0
    current_receipt: str | None = None
    if pointer is not None:
        pointer_value = pointer.to_dict()
        pointer_revision = int(pointer_value["pointer_revision"])
        current_receipt = str(pointer_value["current"]["receipt_sha256"])
    return SyncRequest.from_mapping(
        {
            "contract": "graphify.workspace.sync_request",
            "schema_version": 1,
            "cli_contract_version": 1,
            "mode": "code_only",
            "repo_uuid": REPO_UUID,
            "generation_id": generation_id,
            "expected_registry_revision": int(registry["revision"]),
            "expected_active_source_revision": int(entry["active_source_revision"]),
            "expected_operation_epoch": lease_state.operation_epoch,
            "expected_migration_epoch": lease_state.migration_epoch,
            "expected_pointer_revision": pointer_revision,
            "expected_current_receipt_sha256": current_receipt,
            "source_epoch": source_epoch,
            "semantic_desired_watermark": semantic_desired_watermark,
            "expected_payload_bytes": expected_payload_bytes,
            "capacity_policy": POLICY,
        }
    )


def _install_adapter(runtime: WorkspaceRuntime, adapter: RecordingStructuralAdapter) -> None:
    runtime.generations.adapter = adapter


def _replace_request(request: SyncRequest, **changes: object) -> SyncRequest:
    value = request.to_dict()
    value.update(changes)
    return SyncRequest.from_mapping(value)


def test_code_only_sync_promotes_generation_and_exact_replay_is_noop(tmp_path: Path) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness)
    _install_adapter(runtime, adapter)
    request = _request(runtime)

    first = synchronize_code_only(runtime, request)
    after_first = tree_snapshot(harness.state_root)
    metadata_after_first = metadata_snapshot(harness.state_root)
    second = synchronize_code_only(runtime, request)

    assert first.canonical == second.canonical
    assert first.to_dict() == {
        "cli_contract_version": 1,
        "contract": "graphify.workspace.sync",
        "exit_code": 0,
        "generation_id": GENERATION_ID,
        "mode": "code_only",
        "pointer_revision": 1,
        "receipt_sha256": first.to_dict()["receipt_sha256"],
        "repo_uuid": REPO_UUID,
        "request_sha256": canonical_sha256(request.to_dict()),
        "schema_version": 1,
        "state": "synchronized",
    }
    assert tree_snapshot(harness.state_root) == after_first
    assert metadata_snapshot(harness.state_root) == metadata_after_first
    assert len(adapter.calls) == 1
    with runtime.pointers.read_current(REPO_UUID) as current:
        assert current.receipt.to_dict()["generation_id"] == GENERATION_ID


def test_different_unresolved_request_and_live_lease_fail_before_mutation(tmp_path: Path) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness, fail=True)
    _install_adapter(runtime, adapter)
    request = _request(runtime)
    with pytest.raises(RuntimeError, match="intentional structural adapter failure"):
        synchronize_code_only(runtime, request)
    before_different = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="exact recovery"):
        synchronize_code_only(runtime, _request(runtime, generation_id="gen-sync-other"))

    assert tree_snapshot(harness.state_root) == before_different

    adapter.fail = False
    recovery = synchronize_code_only(runtime, request)
    assert recovery.to_dict()["state"] == "synchronized"
    next_request = _request(runtime, generation_id="gen-sync-with-live-lease")
    lease_state = runtime.leases.inspect(REPO_UUID)
    registry = runtime.registry.load().to_dict()
    held = runtime.leases.acquire(
        REPO_UUID,
        "BUILD",
        runtime.leases.current_owner(),
        expected_registry_revision=int(registry["revision"]),
        expected_active_source_revision=int(
            registry["workspaces"][0]["active_source_revision"]
        ),
        expected_operation_epoch=lease_state.operation_epoch,
        expected_migration_epoch=lease_state.migration_epoch,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    before_busy = tree_snapshot(harness.state_root)
    try:
        with pytest.raises(
            (GenerationConflict, LeaseBusy, RevisionConflict),
            match="lease|active|operation_epoch",
        ):
            synchronize_code_only(runtime, next_request)
        assert tree_snapshot(harness.state_root) == before_busy
    finally:
        harness.leases.release(held)


def test_adapter_failure_preserves_recoverable_staged_state_and_uses_external_staging(
    tmp_path: Path,
) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness, fail=True)
    _install_adapter(runtime, adapter)

    with pytest.raises(RuntimeError, match="intentional structural adapter failure"):
        synchronize_code_only(runtime, _request(runtime))

    staged = harness.state_root / "workspaces" / REPO_UUID / "staged-build.json"
    assert staged.is_file()
    assert adapter.calls
    source_root, output_root, scratch_root = adapter.calls[-1]
    assert source_root == harness.repo
    assert harness.state_root in output_root.parents
    assert harness.state_root in scratch_root.parents
    assert scratch_root == output_root


def test_changed_source_sync_promotes_a_new_structural_generation(tmp_path: Path) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness)
    _install_adapter(runtime, adapter)
    first = synchronize_code_only(runtime, _request(runtime))

    changed = harness.repo / "changed.py"
    changed.write_text("CHANGED = True\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "changed.py"],
        cwd=harness.repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "change structural source"],
        cwd=harness.repo,
        check=True,
        capture_output=True,
        text=True,
    )
    adapter.observation = _adapter(harness).observation
    second_request = _request(
        runtime,
        generation_id="gen-sync-changed",
        source_epoch=2,
        semantic_desired_watermark=2,
    )

    second = synchronize_code_only(runtime, second_request)

    assert first.to_dict()["pointer_revision"] == 1
    assert second.to_dict()["pointer_revision"] == 2
    with runtime.pointers.read_current(REPO_UUID) as current:
        pointer = current.pointer.to_dict()
        assert pointer["current"]["generation_id"] == "gen-sync-changed"
        assert pointer["last_good"]["generation_id"] == GENERATION_ID


@pytest.mark.parametrize(
    "stale_field",
    [
        "expected_registry_revision",
        "expected_active_source_revision",
        "expected_operation_epoch",
        "expected_migration_epoch",
        "expected_pointer_revision",
    ],
)
def test_stale_public_authority_fails_before_sync_mutation(
    tmp_path: Path,
    stale_field: str,
) -> None:
    harness, runtime = _runtime(tmp_path)
    _install_adapter(runtime, _adapter(harness))
    request = _request(runtime)
    if stale_field == "expected_pointer_revision":
        stale = _replace_request(
            request,
            expected_pointer_revision=1,
            expected_current_receipt_sha256="d" * 64,
        )
    else:
        stale = _replace_request(
            request,
            **{stale_field: cast(int, request.to_dict()[stale_field]) + 1},
        )
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)

    with pytest.raises((GenerationConflict, RevisionConflict)):
        synchronize_code_only(runtime, stale)

    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata


def test_missing_registration_and_selected_source_fail_without_state_mutation(
    tmp_path: Path,
) -> None:
    harness, runtime = _runtime(tmp_path)
    _install_adapter(runtime, _adapter(harness))
    request = _request(runtime)
    before_unregistered = tree_snapshot(harness.state_root)

    with pytest.raises(SyncAuthorityConflict, match="registered workspace"):
        synchronize_code_only(
            runtime,
            _replace_request(
                request,
                repo_uuid="22222222-2222-4222-8222-222222222222",
            ),
        )

    assert tree_snapshot(harness.state_root) == before_unregistered

    harness.repo.rename(tmp_path / "selected-source-missing")
    before_missing_source = tree_snapshot(harness.state_root)
    with pytest.raises(IdentityError):
        synchronize_code_only(runtime, request)
    assert tree_snapshot(harness.state_root) == before_missing_source


def test_corrupt_staged_state_fails_closed_without_replacement(tmp_path: Path) -> None:
    harness, runtime = _runtime(tmp_path)
    _install_adapter(runtime, _adapter(harness))
    staged = harness.state_root / "workspaces" / REPO_UUID / "staged-build.json"
    staged.write_bytes(b'{"private_path":"/tmp/operator-secret"}\n')
    staged.chmod(0o600)
    request = _request(runtime)
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)

    with pytest.raises(GenerationError, match="staged build state is corrupt"):
        synchronize_code_only(runtime, request)

    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata


def test_capacity_rejection_leaves_an_exact_recoverable_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime = _runtime(tmp_path)
    _install_adapter(runtime, _adapter(harness))
    monkeypatch.setattr(
        "graphify.workspace.generations.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=POLICY["reserve_bytes"]),
    )

    with pytest.raises(CapacityExceeded, match="filesystem reserve"):
        synchronize_code_only(runtime, _request(runtime))

    staged = runtime.generations._load_staged_build_locked(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "REQUESTED"
    assert runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    status = inspect_workspace_status(_inputs(harness))
    workspace = status.to_dict()["workspaces"][0]
    assert workspace["safe_to_query"] is False
    assert workspace["staged_build"]["blocking"] is True
    assert any(
        check["reason_code"] == "staged_build_recovery_required"
        for check in status.to_dict()["checks"]
    )


def test_unstable_initial_observation_fails_before_staging(tmp_path: Path) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness)
    first = adapter.observation
    second = replace(first, inventory_sha256="d" * 64)
    calls = 0

    def observe(_source_root: Path, **_kwargs: Any) -> SourceObservation:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    adapter.observe = observe  # type: ignore[method-assign]
    _install_adapter(runtime, adapter)
    before_tree = tree_snapshot(harness.state_root)

    with pytest.raises(GenerationConflict, match="not stable"):
        synchronize_code_only(runtime, _request(runtime))

    assert tree_snapshot(harness.state_root) == before_tree


def test_source_mutation_during_build_blocks_completion_and_promotion(
    tmp_path: Path,
) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness)
    build = adapter.build_structural

    def mutate_source(
        source_root: Path,
        *,
        output_root: Path,
        scratch_root: Path,
    ) -> StructuralBuild:
        result = build(
            source_root,
            output_root=output_root,
            scratch_root=scratch_root,
        )
        (source_root / "mutated-during-build.py").write_text(
            "MUTATED = True\n",
            encoding="utf-8",
        )
        adapter.observation = replace(
            adapter.observation,
            inventory_sha256="e" * 64,
        )
        return result

    adapter.build_structural = mutate_source  # type: ignore[method-assign]
    _install_adapter(runtime, adapter)
    request = _request(runtime)

    with pytest.raises(GenerationConflict, match="caller evidence differs"):
        synchronize_code_only(runtime, request)

    staged = runtime.generations._load_staged_build_locked(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "PUBLISHING"
    assert runtime.pointers.load(REPO_UUID, allow_missing=True) is None

    adapter.build_structural = build  # type: ignore[method-assign]
    with pytest.raises(SyncAuthorityConflict, match="terminally abandoned"):
        synchronize_code_only(runtime, request)
    abandoned = runtime.generations._load_staged_build_locked(REPO_UUID)
    assert abandoned is not None
    assert abandoned.lifecycle_state == "ABANDONED"

    successor = synchronize_code_only(
        runtime,
        _request(
            runtime,
            generation_id="gen-sync-after-source-drift",
            source_epoch=2,
            semantic_desired_watermark=2,
        ),
    )
    assert successor.to_dict()["generation_id"] == "gen-sync-after-source-drift"


def test_source_mutation_after_certification_blocks_pointer_promotion(
    tmp_path: Path,
) -> None:
    adapter: RecordingStructuralAdapter | None = None

    def mutate_after_certification(event: str) -> None:
        if event != f"sync:{GENERATION_ID}:generation_certified":
            return
        if adapter is None:  # pragma: no cover - test setup invariant
            raise AssertionError("adapter must be installed before certification")
        adapter.observation = replace(
            adapter.observation,
            inventory_sha256="e" * 64,
        )

    harness, runtime = _runtime(tmp_path, fault_hook=mutate_after_certification)
    adapter = _adapter(harness)
    _install_adapter(runtime, adapter)
    request = _request(runtime)

    with pytest.raises(GenerationConflict, match="caller evidence differs"):
        synchronize_code_only(runtime, request)

    staged = runtime.generations._load_staged_build_locked(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "CERTIFIED"
    assert runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    status = inspect_workspace_status(_inputs(harness))
    workspace = status.to_dict()["workspaces"][0]
    assert workspace["safe_to_query"] is False
    assert workspace["staged_build"]["blocking"] is True

    recovered_runtime = _compose(harness)
    recovered_adapter = _adapter(harness)
    recovered_adapter.observation = adapter.observation
    _install_adapter(recovered_runtime, recovered_adapter)
    with pytest.raises(SyncAuthorityConflict, match="terminally abandoned"):
        synchronize_code_only(recovered_runtime, request)
    abandoned = recovered_runtime.generations._load_staged_build_locked(REPO_UUID)
    assert abandoned is not None
    assert abandoned.lifecycle_state == "ABANDONED"

    successor = synchronize_code_only(
        recovered_runtime,
        _request(
            recovered_runtime,
            generation_id="gen-sync-after-certified-drift",
            source_epoch=2,
            semantic_desired_watermark=2,
        ),
    )
    assert successor.to_dict()["generation_id"] == "gen-sync-after-certified-drift"


_SYNC_FAULT_BOUNDARIES = (
    "source_observed",
    "request_staged",
    "build_acquired",
    "generation_allocated",
    "staging_prepared",
    "adapter_built",
    "staging_completed",
    "queue_reconciled",
    "sealed_inputs_bound",
    "generation_certified",
    "build_released",
    "promotion_acquired",
    "pointer_moved",
    "promotion_completed",
    "promotion_released",
)

_NONTERMINAL_SYNC_FAULT_BOUNDARIES = frozenset(
    {
        "request_staged",
        "build_acquired",
        "generation_allocated",
        "staging_prepared",
        "adapter_built",
        "staging_completed",
        "queue_reconciled",
        "sealed_inputs_bound",
        "generation_certified",
        "build_released",
        "promotion_acquired",
        "pointer_moved",
    }
)


@pytest.mark.parametrize("boundary", _SYNC_FAULT_BOUNDARIES)
def test_fresh_runtime_recovers_every_sync_orchestration_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    target = f"sync:{GENERATION_ID}:{boundary}"
    armed = True

    def fail_once(event: str) -> None:
        nonlocal armed
        if armed and event == target:
            armed = False
            raise InjectedFault(event)

    harness, runtime = _runtime(tmp_path, fault_hook=fail_once)
    _install_adapter(runtime, _adapter(harness))
    request = _request(runtime)

    with pytest.raises(InjectedFault, match=boundary):
        synchronize_code_only(runtime, request)
    assert armed is False

    if boundary in _NONTERMINAL_SYNC_FAULT_BOUNDARIES:
        before_inspection = metadata_snapshot(harness.state_root)
        status = inspect_workspace_status(_inputs(harness))
        status_value = status.to_dict()
        workspace = status_value["workspaces"][0]
        assert status_value["safe_to_query"] is False
        assert workspace["safe_to_query"] is False
        assert workspace["staged_build"]["blocking"] is True
        assert workspace["action_code"] == "resume_exact_workspace_sync"
        doctor_stdout = StringIO()
        doctor_stderr = StringIO()
        assert run_workspace_command(
            ("doctor",),
            inputs=_inputs(harness),
            stdout=doctor_stdout,
            stderr=doctor_stderr,
        ) == status.exit_code
        assert "staged_build_recovery_required" in doctor_stdout.getvalue()
        assert "resume_exact_workspace_sync" in doctor_stdout.getvalue()
        assert doctor_stderr.getvalue() == ""
        assert metadata_snapshot(harness.state_root) == before_inspection

    recovered_runtime = _compose(harness)
    recovered_adapter = _adapter(harness)
    _install_adapter(recovered_runtime, recovered_adapter)
    receipt = synchronize_code_only(recovered_runtime, request)
    after_recovery = metadata_snapshot(harness.state_root)
    replay = synchronize_code_only(recovered_runtime, request)

    assert receipt.canonical == replay.canonical
    assert metadata_snapshot(harness.state_root) == after_recovery
    if boundary in {
        "staging_completed",
        "queue_reconciled",
        "sealed_inputs_bound",
        "generation_certified",
        "build_released",
        "promotion_acquired",
        "pointer_moved",
        "promotion_completed",
        "promotion_released",
    }:
        assert recovered_adapter.calls == []


@pytest.mark.parametrize(
    "durable_event",
    [
        f"generation:{GENERATION_ID}:request_durable",
        f"generation:{GENERATION_ID}:completion_durable",
        f"generation:{GENERATION_ID}:receipt_durable",
        f"generation:{GENERATION_ID}:installed",
        "pointer:promoted:pending_durable",
        "pointer:promoted:visible",
        "pointer:promoted:journal_durable",
        "pointer:promoted:complete",
        f"generation:{GENERATION_ID}:staged_promoted_durable",
    ],
)
def test_fresh_runtime_recovers_each_ambiguous_durable_sync_boundary(
    tmp_path: Path,
    durable_event: str,
) -> None:
    armed = True

    def fail_once(event: str) -> None:
        nonlocal armed
        if armed and event == durable_event:
            armed = False
            raise InjectedFault(event)

    harness, runtime = _runtime(tmp_path, fault_hook=fail_once)
    _install_adapter(runtime, _adapter(harness))
    request = _request(runtime)

    with pytest.raises(InjectedFault):
        synchronize_code_only(runtime, request)
    assert armed is False

    recovered_runtime = _compose(harness)
    _install_adapter(recovered_runtime, _adapter(harness))
    receipt = synchronize_code_only(recovered_runtime, request)

    assert receipt.to_dict()["state"] == "synchronized"
    assert synchronize_code_only(recovered_runtime, request).canonical == receipt.canonical


def test_exact_replay_recovers_pending_staged_request_commit_unknown(
    tmp_path: Path,
) -> None:
    target = f"staged-build:{REPO_UUID}:pending_durable"
    armed = True

    def fail_once(event: str) -> None:
        nonlocal armed
        if armed and event == target:
            armed = False
            raise InjectedFault(event)

    harness, runtime = _runtime(tmp_path, fault_hook=fail_once)
    _install_adapter(runtime, _adapter(harness))
    request = _request(runtime)

    with pytest.raises(CommitUnknown, match="commit_unknown"):
        synchronize_code_only(runtime, request)
    assert armed is False
    assert (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staged-build.pending.json"
    ).is_file()

    recovered_runtime = _compose(harness)
    _install_adapter(recovered_runtime, _adapter(harness))
    receipt = synchronize_code_only(recovered_runtime, request)

    assert receipt.to_dict()["state"] == "synchronized"
    assert not (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staged-build.pending.json"
    ).exists()


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "mode"])
def test_sync_rejects_unsafe_adapter_payload_entries_before_promotion(
    tmp_path: Path,
    attack: str,
) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness)

    def unsafe_build(
        source_root: Path,
        *,
        output_root: Path,
        scratch_root: Path,
    ) -> StructuralBuild:
        adapter.calls.append((source_root, output_root, scratch_root))
        payload = output_root / "graphify-out"
        payload.mkdir()
        target = payload / "graph.json"
        target.write_text("{}\n", encoding="utf-8")
        if attack == "symlink":
            try:
                (payload / "linked.json").symlink_to(target.name)
            except OSError:
                pytest.skip("filesystem does not support symlinks")
        elif attack == "hardlink":
            os.link(target, payload / "hardlinked.json")
        else:
            target.chmod(0o666)
        return StructuralBuild(
            engine_baseline=adapter.engine_baseline,
            node_count=0,
            edge_count=0,
            detected_code_files=(),
            omitted_dispatched_files=(),
        )

    adapter.build_structural = unsafe_build  # type: ignore[method-assign]
    _install_adapter(runtime, adapter)

    with pytest.raises(GenerationError, match="symbolic link|hardlink|mode"):
        synchronize_code_only(runtime, _request(runtime))

    assert runtime.pointers.load(REPO_UUID, allow_missing=True) is None


def test_cli_register_sync_status_is_provider_neutral_and_external_state_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_repo(tmp_path / "registered-source")
    state_root = tmp_path / "external-state"
    inputs = WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=SemanticQueuePolicy(
            max_items=16,
            max_bytes=1024 * 1024,
            retry_budget=1,
        ),
        capabilities=SUPPORTED,
    )
    fake_home = tmp_path / "operator-home"
    fake_codex_home = tmp_path / "operator-codex-home"
    fake_global_install = tmp_path / "global-install"
    for directory in (fake_home, fake_codex_home, fake_global_install):
        directory.mkdir()
    monkeypatch.setenv("HOME", os.fspath(fake_home))
    monkeypatch.setenv("CODEX_HOME", os.fspath(fake_codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "operator-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-anthropic-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "operator-gemini-secret")
    monkeypatch.setenv("GRAPHIFY_PROVIDER", "must-not-be-read")
    monkeypatch.chdir(repo)

    registration_input = canonical_json_bytes(
        authorization("p5b2b-e2e-register").to_dict()
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(registration_input)),
    )
    register_stdout = StringIO()
    register_stderr = StringIO()
    assert run_workspace_command(
        (
            "register",
            "enroll",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ),
        inputs=inputs,
        stdout=register_stdout,
        stderr=register_stderr,
    ) == 0
    assert register_stderr.getvalue() == ""

    runtime = compose_workspace_runtime(inputs)
    request = _request(runtime, expected_payload_bytes=1024 * 1024)
    source_before = metadata_snapshot(repo)
    home_before = metadata_snapshot(fake_home)
    codex_before = metadata_snapshot(fake_codex_home)
    global_before = metadata_snapshot(fake_global_install)
    state_before = tree_snapshot(state_root)

    def reject_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("code-only sync attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(request.canonical)),
    )
    sync_stdout = StringIO()
    sync_stderr = StringIO()
    assert run_workspace_command(
        ("sync", "--code-only", "--request-stdin"),
        inputs=inputs,
        stdout=sync_stdout,
        stderr=sync_stderr,
    ) == 0
    assert sync_stderr.getvalue() == ""
    sync_receipt = json.loads(sync_stdout.getvalue())
    assert sync_receipt["state"] == "synchronized"
    assert "provider" not in sync_receipt

    state_after_sync = tree_snapshot(state_root)
    changed = {
        path
        for path in state_before.keys() | state_after_sync.keys()
        if state_before.get(path) != state_after_sync.get(path)
    }
    workspace_root = f"workspaces/{REPO_UUID}"
    expected_changed = {
        ".",
        "capacity.json",
        "capacity.previous.json",
        workspace_root,
        f"{workspace_root}/generations",
        f"{workspace_root}/generations/{GENERATION_ID}",
        f"{workspace_root}/generations/{GENERATION_ID}/graphify-out",
        f"{workspace_root}/generations/{GENERATION_ID}/graphify-out/graph.json",
        f"{workspace_root}/generations/{GENERATION_ID}/receipt.json",
        f"{workspace_root}/journal",
        f"{workspace_root}/journal/head.json",
        f"{workspace_root}/journal/head.previous.json",
        f"{workspace_root}/journal/segments",
        *{
            f"{workspace_root}/journal/segments/{sequence:020d}.gwf"
            for sequence in range(1, 7)
        },
        f"{workspace_root}/locks",
        f"{workspace_root}/locks/generations",
        f"{workspace_root}/locks/generations/{GENERATION_ID}.lock",
        f"{workspace_root}/pointers.json",
        f"{workspace_root}/queue",
        f"{workspace_root}/queue/certifications",
        f"{workspace_root}/queue/certifications/{GENERATION_ID}.json",
        f"{workspace_root}/queue/semantic.jsonl",
        f"{workspace_root}/queue/semantic.previous.jsonl",
        f"{workspace_root}/staged-build.json",
        f"{workspace_root}/staged-build.previous.json",
        f"{workspace_root}/staging",
        f"{workspace_root}/workspace.json",
        f"{workspace_root}/workspace.previous.json",
    }
    assert changed == expected_changed
    assert metadata_snapshot(repo) == source_before
    assert metadata_snapshot(fake_home) == home_before
    assert metadata_snapshot(fake_codex_home) == codex_before
    assert metadata_snapshot(fake_global_install) == global_before

    state_metadata_after_sync = metadata_snapshot(state_root)
    status_stdout = StringIO()
    status_stderr = StringIO()
    assert run_workspace_command(
        ("status", "--json"),
        inputs=inputs,
        stdout=status_stdout,
        stderr=status_stderr,
    ) == 0
    status = json.loads(status_stdout.getvalue())
    assert status["schema_version"] == 2
    assert status["state"] == "ready"
    assert status["safe_to_query"] is True
    assert status["workspaces"][0]["staged_build"]["blocking"] is False
    assert status_stderr.getvalue() == ""

    doctor_stdout = StringIO()
    doctor_stderr = StringIO()
    assert run_workspace_command(
        ("doctor",),
        inputs=inputs,
        stdout=doctor_stdout,
        stderr=doctor_stderr,
    ) == 0
    assert "workspace doctor: ready" in doctor_stdout.getvalue()
    assert doctor_stderr.getvalue() == ""
    assert metadata_snapshot(state_root) == state_metadata_after_sync
