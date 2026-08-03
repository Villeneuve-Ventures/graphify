"""P5B2 semantic-result handoff and sealed-input terminal coverage."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Mapping, cast

import pytest

import graphify.workspace.semantic_handoff as semantic_handoff
import graphify.workspace.semantic_worker as semantic_worker
import graphify.workspace.sync as workspace_sync
from graphify.workspace.composition import WorkspaceRuntime
from graphify.workspace.contracts import PointerSet, payload_manifest_sha256
from graphify.workspace.generations import CertificationRequest
from graphify.workspace.persistence import InjectedFault, PosixSyscalls
from graphify.workspace.semantic_handoff import (
    CarriedSemanticResultEvidence,
    FreshWorkerSessionEvidence,
    SEMANTIC_INPUT_PATH,
    SemanticHandoffConflict,
    SemanticHandoffInvalid,
    parse_semantic_result_handoff,
)
from graphify.workspace.semantic_queue import SemanticDesiredWork
from graphify.workspace.sync import SyncRequest
from tests.test_workspace_sync import (
    POLICY,
    _adapter,
    _install_adapter,
    _replace_request,
    _request,
    _runtime,
)
from tests.workspace_p3_helpers import REPO_UUID, RuntimeHarness, tree_snapshot


class _ProtocolOutput:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, value: bytes) -> int:
        self.frames.append(bytes(value))
        return len(value)

    def flush(self) -> None:
        return None


class _CompleteAfterWorkInput:
    def __init__(
        self,
        begin: bytes,
        output: _ProtocolOutput,
        payload: Mapping[str, object] | Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        self.begin = begin
        self.output = output
        self.payload = payload
        self.calls = 0

    def readline(self, _maximum: int = -1) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return self.begin
        if self.calls == 2:
            work = semantic_worker.parse_result_frame(self.output.frames[0])
            payload = self.payload(work) if callable(self.payload) else self.payload
            return semantic_worker.canonical_protocol_bytes(
                {
                    "action": "complete",
                    "begin_request_sha256": work["begin_request_sha256"],
                    "claim_id": work["claim_id"],
                    "cli_contract_version": 1,
                    "contract": "graphify.workspace.semantic_worker_request",
                    "payload": deepcopy(dict(payload)),
                    "schema_version": 1,
                }
            )
        return b""


class _ArmedFault:
    def __init__(self, target: str) -> None:
        self.target = target
        self.armed = False
        self.fired = False

    def __call__(self, event: str) -> None:
        if self.armed and not self.fired and event == self.target:
            self.fired = True
            raise InjectedFault(event)


def _fragment(path: str = "README.md") -> dict[str, object]:
    return {
        "nodes": [
            {
                "id": "readme",
                "label": "README",
                "file_type": "document",
                "source_file": path,
                "source_location": "L1",
                "source_url": None,
                "captured_at": None,
                "author": None,
                "contributor": None,
            },
            {
                "id": "workspace",
                "label": "Workspace",
                "file_type": "concept",
                "source_file": path,
                "source_location": None,
                "source_url": None,
                "captured_at": None,
                "author": None,
                "contributor": None,
            },
        ],
        "edges": [
            {
                "source": "readme",
                "target": "workspace",
                "relation": "conceptually_related_to",
                "confidence": "INFERRED",
                "confidence_score": Decimal("0.75"),
                "source_file": path,
                "source_location": "L1",
                "weight": Decimal("1"),
            }
        ],
        "hyperedges": [],
    }


def _acquire_build(runtime: WorkspaceRuntime):
    registry = runtime.registry.load()
    entry = registry.to_dict()["workspaces"][0]
    state = runtime.leases.inspect(REPO_UUID)
    now_ns = time.monotonic_ns()
    return runtime.leases.acquire(
        REPO_UUID,
        "BUILD",
        runtime.leases.current_owner(),
        expected_registry_revision=int(registry.to_dict()["revision"]),
        expected_active_source_revision=int(entry["active_source_revision"]),
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=now_ns,
        ttl_ns=60_000_000_000,
    )


def _begin(runtime: WorkspaceRuntime) -> dict[str, object]:
    registry = runtime.registry.load()
    entry = registry.to_dict()["workspaces"][0]
    lease_state = runtime.leases.inspect(REPO_UUID)
    queue = runtime.semantic_queue.inspect(REPO_UUID)
    return {
        "action": "begin",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "executor": "host_agent",
        "expected_active_source_revision": entry["active_source_revision"],
        "expected_desired_watermark": queue.desired_watermark,
        "expected_migration_epoch": lease_state.migration_epoch,
        "expected_operation_epoch": lease_state.operation_epoch,
        "expected_queue_revision": queue.revision,
        "expected_registry_revision": registry.to_dict()["revision"],
        "host_agent_active": True,
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "timeout_ms": 5_000,
    }


def _drain_one_worker(
    runtime: WorkspaceRuntime,
    *,
    payload: Mapping[str, object] | Callable[[Mapping[str, object]], Mapping[str, object]],
) -> FreshWorkerSessionEvidence:
    begin = semantic_worker.canonical_protocol_bytes(_begin(runtime))
    output = _ProtocolOutput()
    protocol_input = _CompleteAfterWorkInput(begin, output, payload)
    exit_code = semantic_worker.run_semantic_worker(
        runtime,
        stdin=protocol_input,  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )
    assert exit_code == 0
    assert semantic_worker.parse_result_frame(output.frames[-1])["outcome"] == "completed"
    return FreshWorkerSessionEvidence(
        begin_request_bytes=begin,
        stdout_bytes=b"".join(output.frames),
        process_exit_code=exit_code,
    )


def _fragment_for_work(frame: Mapping[str, object]) -> Mapping[str, object]:
    work = cast(Mapping[str, object], frame["work"])
    return {
        "kind": "semantic_fragment",
        "fragment": _fragment(cast(str, work["path"])),
    }


def _fresh_ready_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation_id: str = "gen-semantic-handoff",
    fault_hook: Any = None,
) -> tuple[
    RuntimeHarness,
    WorkspaceRuntime,
    SyncRequest,
    FreshWorkerSessionEvidence,
]:
    harness, runtime = _runtime(tmp_path, fault_hook=fault_hook)
    adapter = _adapter(harness)
    _install_adapter(runtime, adapter)
    work = SemanticDesiredWork(
        source_epoch=1,
        policy_sha256=adapter.observation.policy_sha256,
        operation="UPSERT",
        path="README.md",
        content_sha256=hashlib.sha256((harness.repo / "README.md").read_bytes()).hexdigest(),
        desired_revision=1,
    )
    build = _acquire_build(runtime)
    runtime.semantic_queue.reconcile(
        build,
        (work,),
        source_epoch=1,
        policy_sha256=adapter.observation.policy_sha256,
        source_observations=(adapter.observation, adapter.observation),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=time.monotonic_ns(),
    )
    runtime.leases.release(build)
    monkeypatch.chdir(harness.repo)
    evidence = _drain_one_worker(
        runtime,
        payload={"kind": "semantic_fragment", "fragment": _fragment()},
    )
    request = _request(
        runtime,
        generation_id=generation_id,
        expected_payload_bytes=256 * 1024,
    )
    return harness, runtime, request, evidence


def _handoff_path(
    harness: RuntimeHarness,
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> Path:
    _source, observations = workspace_sync._observe_structural_source(runtime, REPO_UUID)
    structural_request = workspace_sync._structural_request(runtime, request, observations)
    return (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-staging"
        / "handoffs"
        / request.generation_id
        / f"{structural_request.sha256}.json"
    )


def _result_path(
    harness: RuntimeHarness,
    evidence: FreshWorkerSessionEvidence,
) -> Path:
    return (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-staging"
        / hashlib.sha256(evidence.begin_request_bytes).hexdigest()
        / "result.json"
    )


def _archived_result_markers(result_path: Path) -> tuple[Path, ...]:
    archive = result_path.parent / ".semantic-result-consumed"
    if not archive.exists():
        return ()
    return tuple(sorted(archive.iterdir()))


def _certify_and_promote_terminal(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> None:
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "COMPLETE"
    _source, observations = workspace_sync._observe_structural_source(runtime, REPO_UUID)
    acquired_at = datetime.now(timezone.utc)
    attempt_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    attempt = runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        request.generation_id,
        staged.request,
        attempt_sha256=attempt_sha256,
        acquired_at=acquired_at,
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )
    try:
        allocation = runtime.generations.allocate(
            attempt.grant,
            expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=request.capacity_policy,
            generation_id=request.generation_id,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
        preparation = runtime.generations.prepare_staged_build(
            attempt,
            allocation,
            monotonic_ns=time.monotonic_ns(),
        )
        completion = runtime.generations.complete_staged_build(
            preparation,
            source_observations=observations,
            monotonic_ns=time.monotonic_ns(),
        )
        queue = runtime.semantic_queue.inspect(REPO_UUID)
        receipt = runtime.generations.certify(
            attempt.grant,
            completion.allocation,
            CertificationRequest(
                source_commit=staged.request.source_commit,
                source_epoch=request.source_epoch,
                policy_sha256=staged.request.policy_sha256,
                observation_manifest_sha256=staged.request.observation_manifest_sha256,
                queue_watermark=queue.desired_watermark,
                semantic_completeness="complete",
                compatibility_sha256=runtime.generations.compatibility_sha256,
                validations=(
                    "payload_manifest",
                    "coordination_lock_precreated",
                    "stable_semantic_queue",
                ),
            ),
            source_observations=observations,
            declared_entries=completion.entries,
            staged_completion=completion,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
    finally:
        runtime.leases.release(attempt.grant)
    terminal = workspace_sync._promote(
        runtime,
        request,
        staged.request,
        receipt,
        observations,
        attempt_sha256=attempt_sha256,
    )
    assert terminal is not None
    assert terminal.lifecycle_state == "PROMOTED"


def _carried_ready_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    RuntimeHarness,
    WorkspaceRuntime,
    SyncRequest,
    SyncRequest,
    SemanticDesiredWork,
    PointerSet,
]:
    harness, runtime, first_request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
    )
    workspace_sync._finalize_semantic_result_handoff(
        runtime,
        first_request,
        (evidence,),
    )
    _certify_and_promote_terminal(runtime, first_request)
    current_pointer = runtime.pointers.load(REPO_UUID)
    assert current_pointer is not None
    adapter = _adapter(harness)
    work = SemanticDesiredWork(
        source_epoch=1,
        policy_sha256=adapter.observation.policy_sha256,
        operation="UPSERT",
        path="README.md",
        content_sha256=hashlib.sha256((harness.repo / "README.md").read_bytes()).hexdigest(),
        desired_revision=1,
    )
    build = _acquire_build(runtime)
    runtime.semantic_queue.reconcile(
        build,
        (work,),
        source_epoch=1,
        policy_sha256=adapter.observation.policy_sha256,
        source_observations=(adapter.observation, adapter.observation),
        desired_watermark=2,
        semantic_required=True,
        monotonic_ns=time.monotonic_ns(),
    )
    runtime.leases.release(build)
    carried_request = _request(
        runtime,
        generation_id="gen-semantic-carried",
        semantic_desired_watermark=2,
        expected_payload_bytes=256 * 1024,
    )
    return (
        harness,
        runtime,
        first_request,
        carried_request,
        work,
        current_pointer,
    )


def test_fresh_handoff_reaches_only_the_reopened_sealed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    source_before = tree_snapshot(harness.repo)

    proof = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )

    queue = runtime.semantic_queue.inspect(REPO_UUID)
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "COMPLETE"
    staged_root = harness.state_root / "workspaces" / REPO_UUID / "staging" / request.generation_id
    reopened_inventory = runtime.generations._inventory(
        staged_root,
        allowed_root_entries=frozenset({"graphify-out"}),
    )
    recomputed_manifest = payload_manifest_sha256(
        "graphify-out",
        reopened_inventory.entries,
    )
    assert queue.reconciliation is not None
    assert (
        queue.reconciliation.sealed_input_manifest_sha256
        == staged.payload_manifest_sha256
        == recomputed_manifest
        == proof.payload_manifest_sha256
    )
    handoff_path = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-staging"
        / "handoffs"
        / request.generation_id
        / f"{staged.request.sha256}.json"
    )
    semantic_input = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staging"
        / request.generation_id
        / SEMANTIC_INPUT_PATH
    )
    assert handoff_path.read_bytes() == semantic_input.read_bytes()
    parsed = parse_semantic_result_handoff(
        semantic_input.read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    assert parsed.sha256 == proof.handoff_sha256
    assert parsed.carried_source_generation_id is None
    assert [entry["origin"] for entry in parsed.results] == ["fresh_worker_session"]
    result_path = _result_path(harness, evidence)
    assert not result_path.exists()
    consumed = _archived_result_markers(result_path)
    assert len(consumed) == 1
    assert consumed[0].name.startswith(
        f".result.json.consumed-{cast(str, parsed.results[0]['result_binding_sha256'])}-"
    )
    assert consumed[0].read_bytes() == b""
    assert semantic_input.stat().st_mode & 0o777 == 0o600
    assert semantic_input.parent.stat().st_mode & 0o777 == 0o755
    assert not (
        harness.state_root / "workspaces" / REPO_UUID / "generations" / request.generation_id
    ).exists()
    assert runtime.pointers.load(REPO_UUID, allow_missing=True) is None
    assert tree_snapshot(harness.repo) == source_before
    assert "CERTIFIED" not in "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (harness.state_root / "workspaces" / REPO_UUID / "journal").rglob("*")
        if path.is_file()
    )


def test_multiple_fresh_sessions_bind_their_own_accepted_operation_epochs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime = _runtime(tmp_path)
    adapter = _adapter(harness)
    _install_adapter(runtime, adapter)
    paths = (".graphify/workspace.toml", "README.md")
    desired = tuple(
        SemanticDesiredWork(
            source_epoch=1,
            policy_sha256=adapter.observation.policy_sha256,
            operation="UPSERT",
            path=path,
            content_sha256=hashlib.sha256((harness.repo / path).read_bytes()).hexdigest(),
            desired_revision=1,
        )
        for path in paths
    )
    build = _acquire_build(runtime)
    runtime.semantic_queue.reconcile(
        build,
        desired,
        source_epoch=1,
        policy_sha256=adapter.observation.policy_sha256,
        source_observations=(adapter.observation, adapter.observation),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=time.monotonic_ns(),
    )
    runtime.leases.release(build)
    monkeypatch.chdir(harness.repo)
    evidence = (
        _drain_one_worker(runtime, payload=_fragment_for_work),
        _drain_one_worker(runtime, payload=_fragment_for_work),
    )
    request = _request(
        runtime,
        generation_id="gen-semantic-multiple-fresh",
        expected_payload_bytes=256 * 1024,
    )

    proof = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        evidence,
    )

    staged = runtime.generations.recover_staged_build(REPO_UUID)
    queue = runtime.semantic_queue.inspect(REPO_UUID)
    assert staged is not None
    semantic_input = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staging"
        / request.generation_id
        / SEMANTIC_INPUT_PATH
    )
    parsed = parse_semantic_result_handoff(
        semantic_input.read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    result_epochs = [
        cast(Mapping[str, object], entry["result_binding"])["operation_epoch"]
        for entry in parsed.results
    ]
    assert len(set(result_epochs)) == 2
    assert max(cast(list[int], result_epochs)) == request.expected_operation_epoch
    assert staged.payload_manifest_sha256 == proof.payload_manifest_sha256
    assert queue.reconciliation is not None
    assert queue.reconciliation.sealed_input_manifest_sha256 == proof.payload_manifest_sha256


def test_exact_replay_reopens_the_same_handoff_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)

    first = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )
    handoff_path = _handoff_path(harness, runtime, request)
    handoff_before = handoff_path.read_bytes()
    terminal_before = runtime.generations.recover_staged_build(REPO_UUID)
    queue_before = runtime.semantic_queue.inspect(REPO_UUID)

    replay = workspace_sync._finalize_semantic_result_handoff(runtime, request, ())

    assert replay == first
    assert handoff_path.read_bytes() == handoff_before
    assert runtime.generations.recover_staged_build(REPO_UUID) == terminal_before
    assert runtime.semantic_queue.inspect(REPO_UUID) == queue_before


@pytest.mark.parametrize(
    "boundary",
    [
        "semantic_result_handoff:gen-semantic-handoff:installed",
        "semantic_input_copy:gen-semantic-handoff:installed",
        "semantic_queue:current_replaced",
    ],
)
def test_post_visibility_faults_are_adopted_only_as_the_exact_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    fault = _ArmedFault(boundary)
    _harness, runtime, request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    fault.armed = True

    proof = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )

    assert fault.fired
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    queue = runtime.semantic_queue.inspect(REPO_UUID)
    assert staged is not None
    assert staged.lifecycle_state == "COMPLETE"
    assert staged.payload_manifest_sha256 == proof.payload_manifest_sha256
    assert queue.reconciliation is not None
    assert queue.reconciliation.sealed_input_manifest_sha256 == proof.payload_manifest_sha256


@pytest.mark.parametrize("evidence_mode", ["missing", "duplicate"])
def test_first_handoff_rejects_a_non_bijective_result_set_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_mode: str,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    supplied = () if evidence_mode == "missing" else (evidence, evidence)

    with pytest.raises(
        SemanticHandoffInvalid,
        match="bijection|nonempty|not ascending",
    ):
        workspace_sync._finalize_semantic_result_handoff(runtime, request, supplied)

    assert not _handoff_path(harness, runtime, request).exists()
    assert runtime.generations.recover_staged_build(REPO_UUID) is None
    assert runtime.semantic_queue.inspect(REPO_UUID).reconciliation is not None


def test_fresh_result_is_reopened_before_exclusive_authority_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    owner = runtime.semantic_handoffs
    assert owner is not None
    inside_authority_lock = False
    fresh_reopens = 0
    bound_request_state = runtime.leases._bound_request_state
    fresh_entry = owner._fresh_entry

    @contextmanager
    def observed_bound_request_state(repo_uuid: str):
        nonlocal inside_authority_lock
        with bound_request_state(repo_uuid) as snapshot:
            inside_authority_lock = True
            try:
                yield snapshot
            finally:
                inside_authority_lock = False

    def observed_fresh_entry(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal fresh_reopens
        assert not inside_authority_lock
        fresh_reopens += 1
        return fresh_entry(*args, **kwargs)

    monkeypatch.setattr(runtime.leases, "_bound_request_state", observed_bound_request_state)
    monkeypatch.setattr(owner, "_fresh_entry", observed_fresh_entry)

    workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))

    assert fresh_reopens == 1


def test_nonzero_worker_exit_is_redacted_and_rejected_before_state_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    secret = "PRIVATE-SEMANTIC-SECRET"
    invalid = replace(
        evidence, process_exit_code=7, stdout_bytes=evidence.stdout_bytes + secret.encode()
    )

    with pytest.raises(SemanticHandoffInvalid) as raised:
        workspace_sync._finalize_semantic_result_handoff(runtime, request, (invalid,))

    rendered = str(raised.value)
    assert secret not in rendered
    assert str(harness.state_root) not in rendered
    assert "exit exactly zero" in rendered
    assert not _handoff_path(harness, runtime, request).exists()


def test_shared_capacity_charges_handoff_plus_target_reservation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    limited_policy = {
        **POLICY,
        "workspace_max_bytes": request.expected_payload_bytes,
    }
    limited = _replace_request(request, capacity_policy=limited_policy)

    with pytest.raises(SemanticHandoffConflict, match="shared generation capacity"):
        workspace_sync._finalize_semantic_result_handoff(
            runtime,
            limited,
            (evidence,),
        )

    assert not _handoff_path(harness, runtime, limited).exists()
    assert runtime.generations.recover_staged_build(REPO_UUID) is None


def test_handoff_final_path_is_no_follow_and_failure_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    handoff_path = _handoff_path(harness, runtime, request)
    handoff_path.parent.mkdir(parents=True, mode=0o700)
    handoff_path.symlink_to(harness.repo / "README.md")

    with pytest.raises(SemanticHandoffConflict) as raised:
        workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))

    rendered = str(raised.value)
    assert "unsafe or unreadable" in rendered
    assert str(harness.repo) not in rendered
    assert runtime.generations.recover_staged_build(REPO_UUID) is None


def test_extra_handoff_sibling_fails_before_install_or_downstream_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    expected = _handoff_path(harness, runtime, request)
    sibling = expected.with_name(f"{'f' * 64}.json")
    assert sibling != expected
    owner = runtime.semantic_handoffs
    assert owner is not None
    owner.state.ensure_directory(sibling.parent.relative_to(harness.state_root))
    sibling.write_bytes(b"{}\n")
    sibling.chmod(0o600)
    queue_before = runtime.semantic_queue.inspect(REPO_UUID)

    with pytest.raises(SemanticHandoffConflict, match="extra|directory"):
        workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))

    assert not expected.exists()
    assert sibling.read_bytes() == b"{}\n"
    assert _result_path(harness, evidence).exists()
    assert runtime.generations.recover_staged_build(REPO_UUID) is None
    assert runtime.semantic_queue.inspect(REPO_UUID) == queue_before


@pytest.mark.parametrize(
    ("boundary", "envelope_remains"),
    [
        ("before_unlink", True),
        ("unlinked", False),
        ("parent_durable", False),
    ],
)
def test_cleanup_failure_preserves_success_and_exact_replay_retries_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    envelope_remains: bool,
) -> None:
    fault = _ArmedFault("")
    harness, runtime, request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    begin_sha256 = hashlib.sha256(evidence.begin_request_bytes).hexdigest()
    fault.target = f"semantic_result_cleanup:{begin_sha256}:{boundary}"
    fault.armed = True

    first = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )

    assert fault.fired
    assert _result_path(harness, evidence).exists() is envelope_remains
    staged = runtime.generations.recover_staged_build(REPO_UUID)
    queue = runtime.semantic_queue.inspect(REPO_UUID)
    assert staged is not None
    assert queue.reconciliation is not None
    assert (
        staged.payload_manifest_sha256
        == queue.reconciliation.sealed_input_manifest_sha256
        == first.payload_manifest_sha256
    )

    replay = workspace_sync._finalize_semantic_result_handoff(runtime, request, ())

    assert replay == first
    assert not _result_path(harness, evidence).exists()


def test_cleanup_preserves_changed_or_linked_semantic_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("")
    harness, runtime, request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    begin_sha256 = hashlib.sha256(evidence.begin_request_bytes).hexdigest()
    fault.target = f"semantic_result_cleanup:{begin_sha256}:before_unlink"
    fault.armed = True
    workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))
    result_path = _result_path(harness, evidence)
    handoff = parse_semantic_result_handoff(
        _handoff_path(harness, runtime, request).read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    owner = runtime.semantic_handoffs
    assert owner is not None

    expected = result_path.read_bytes()
    retained_path = result_path.with_name("retained-result.json")
    foreign = b'{"foreign":true}\n'
    swapped = False

    def swap_before_unlink(event: str) -> None:
        nonlocal swapped
        if swapped or event != f"semantic_result_cleanup:{begin_sha256}:before_unlink":
            return
        swapped = True
        result_path.replace(retained_path)
        result_path.write_bytes(foreign)
        result_path.chmod(0o600)

    owner.state.fault_hook = swap_before_unlink
    owner.cleanup_consumed_fresh_results(handoff)

    assert swapped
    assert result_path.read_bytes() == foreign
    assert retained_path.read_bytes() == expected
    source_path = harness.repo / "README.md"
    source_before = source_path.read_bytes()
    result_path.unlink()
    result_path.symlink_to(source_path)
    owner.state.fault_hook = lambda _event: None

    owner.cleanup_consumed_fresh_results(handoff)

    assert result_path.is_symlink()
    assert source_path.read_bytes() == source_before


def test_cleanup_preserves_a_replacement_swapped_at_the_rename_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("")
    harness, runtime, request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    begin_sha256 = hashlib.sha256(evidence.begin_request_bytes).hexdigest()
    fault.target = f"semantic_result_cleanup:{begin_sha256}:before_unlink"
    fault.armed = True
    workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))
    result_path = _result_path(harness, evidence)
    handoff = parse_semantic_result_handoff(
        _handoff_path(harness, runtime, request).read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    expected = result_path.read_bytes()
    retained_path = result_path.with_name("retained-result.json")
    foreign = b'{"foreign":true}\n'

    class SwapAtExclusiveRename(PosixSyscalls):
        swapped = False

        def rename_exclusive_at(
            self,
            source: str,
            destination: str,
            *,
            source_dir_fd: int,
            destination_dir_fd: int,
        ) -> None:
            if source == result_path.name and not self.swapped:
                self.swapped = True
                result_path.replace(retained_path)
                result_path.write_bytes(foreign)
                result_path.chmod(0o600)
            super().rename_exclusive_at(
                source,
                destination,
                source_dir_fd=source_dir_fd,
                destination_dir_fd=destination_dir_fd,
            )

    syscalls = SwapAtExclusiveRename()
    owner = runtime.semantic_handoffs
    assert owner is not None
    owner.state.syscalls = syscalls
    owner.state.fault_hook = lambda _event: None

    owner.cleanup_consumed_fresh_results(handoff)

    assert syscalls.swapped
    assert result_path.read_bytes() == foreign
    assert retained_path.read_bytes() == expected
    assert not tuple(result_path.parent.glob(".result.json.consumed-*"))
    assert not _archived_result_markers(result_path)


def test_cleanup_replay_consumes_a_republished_exact_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)

    first = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )
    result_path = _result_path(harness, evidence)
    markers = _archived_result_markers(result_path)
    assert len(markers) == 1
    expected = cast(
        Mapping[str, object],
        parse_semantic_result_handoff(
            _handoff_path(harness, runtime, request).read_bytes(),
            max_bytes=request.expected_payload_bytes,
        ).results[0]["result_binding"],
    )
    result_path.write_bytes(semantic_worker.canonical_protocol_bytes(expected))
    result_path.chmod(0o600)

    replay = workspace_sync._finalize_semantic_result_handoff(runtime, request, ())

    assert replay == first
    assert not result_path.exists()
    markers = _archived_result_markers(result_path)
    assert len(markers) == 2
    assert all(marker.read_bytes() == b"" for marker in markers)


def test_cleanup_replay_zeroizes_a_retained_marker_beside_a_foreign_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("")
    harness, runtime, request, evidence = _fresh_ready_runtime(
        tmp_path,
        monkeypatch,
        fault_hook=fault,
    )
    begin_sha256 = hashlib.sha256(evidence.begin_request_bytes).hexdigest()
    fault.target = f"semantic_result_cleanup:{begin_sha256}:unlinked"
    fault.armed = True

    first = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )
    result_path = _result_path(harness, evidence)
    markers = tuple(result_path.parent.glob(".result.json.consumed-*"))
    assert len(markers) == 1
    assert markers[0].stat().st_size > 0
    marker_name = markers[0].name
    foreign = b'{"foreign":true}\n'
    result_path.write_bytes(foreign)
    result_path.chmod(0o600)

    replay = workspace_sync._finalize_semantic_result_handoff(runtime, request, ())

    assert replay == first
    assert result_path.read_bytes() == foreign
    assert not markers[0].exists()
    assert (
        result_path.parent / ".semantic-result-consumed" / marker_name
    ).read_bytes() == b""


def test_cleanup_preserves_a_marker_with_a_false_encoded_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))
    result_path = _result_path(harness, evidence)
    handoff = parse_semantic_result_handoff(
        _handoff_path(harness, runtime, request).read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    binding = cast(Mapping[str, object], handoff.results[0]["result_binding"])
    expected = semantic_worker.canonical_protocol_bytes(binding)
    digest = hashlib.sha256(expected).hexdigest()
    false_marker = result_path.with_name(
        f".result.json.consumed-{digest}-dead-beef"
    )
    false_marker.write_bytes(expected)
    false_marker.chmod(0o600)
    owner = runtime.semantic_handoffs
    assert owner is not None

    owner.cleanup_consumed_fresh_results(handoff)

    assert false_marker.read_bytes() == expected


def test_current_result_cleanup_is_independent_of_a_full_marker_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    first = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        request,
        (evidence,),
    )
    result_path = _result_path(harness, evidence)
    handoff = parse_semantic_result_handoff(
        _handoff_path(harness, runtime, request).read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    binding = cast(Mapping[str, object], handoff.results[0]["result_binding"])
    expected = semantic_worker.canonical_protocol_bytes(binding)
    digest = hashlib.sha256(expected).hexdigest()
    for index in range(64):
        temporary = result_path.with_name(f".cleanup-marker-{index}")
        temporary.write_bytes(b"")
        temporary.chmod(0o600)
        details = temporary.stat()
        temporary.rename(
            result_path.with_name(
                f".result.json.consumed-{digest}-{details.st_dev:x}-{details.st_ino:x}"
            )
        )
    result_path.write_bytes(expected)
    result_path.chmod(0o600)

    replay = workspace_sync._finalize_semantic_result_handoff(runtime, request, ())

    assert replay == first
    assert not result_path.exists()
    assert len(_archived_result_markers(result_path)) == 2


def test_cleanup_does_not_move_a_foreign_post_archive_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, runtime, request, evidence = _fresh_ready_runtime(tmp_path, monkeypatch)
    workspace_sync._finalize_semantic_result_handoff(runtime, request, (evidence,))
    result_path = _result_path(harness, evidence)
    handoff = parse_semantic_result_handoff(
        _handoff_path(harness, runtime, request).read_bytes(),
        max_bytes=request.expected_payload_bytes,
    )
    binding = cast(Mapping[str, object], handoff.results[0]["result_binding"])
    expected = semantic_worker.canonical_protocol_bytes(binding)
    result_path.write_bytes(expected)
    result_path.chmod(0o600)
    foreign = b"FOREIGN-EVIDENCE"
    retained_zero = result_path.with_name("retained-zero-marker")
    replaced_archive: Path | None = None

    class SwapAfterArchiveRename(PosixSyscalls):
        swapped = False

        def rename_exclusive_at(
            self,
            source: str,
            destination: str,
            *,
            source_dir_fd: int,
            destination_dir_fd: int,
        ) -> None:
            nonlocal replaced_archive
            super().rename_exclusive_at(
                source,
                destination,
                source_dir_fd=source_dir_fd,
                destination_dir_fd=destination_dir_fd,
            )
            if self.swapped or source_dir_fd == destination_dir_fd:
                return
            self.swapped = True
            replaced_archive = (
                result_path.parent / ".semantic-result-consumed" / destination
            )
            replaced_archive.replace(retained_zero)
            replaced_archive.write_bytes(foreign)
            replaced_archive.chmod(0o600)

    syscalls = SwapAfterArchiveRename()
    owner = runtime.semantic_handoffs
    assert owner is not None
    owner.state.syscalls = syscalls

    owner.cleanup_consumed_fresh_results(handoff)

    assert syscalls.swapped
    assert replaced_archive is not None
    assert replaced_archive.read_bytes() == foreign
    assert retained_zero.read_bytes() == b""
    assert not result_path.exists()
    assert not (
        result_path.parent / replaced_archive.name
    ).exists()


def test_carried_result_is_byte_identical_evidence_from_current_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        harness,
        runtime,
        first_request,
        carried_request,
        work,
        current_pointer,
    ) = _carried_ready_runtime(
        tmp_path,
        monkeypatch,
    )
    owner = runtime.semantic_handoffs
    assert owner is not None
    inside_authority_lock = False
    source_reopens = 0
    authority_lock_entries = 0
    bound_request_state = runtime.leases._bound_request_state
    current_source_handoff = owner._current_source_handoff

    @contextmanager
    def observed_bound_request_state(repo_uuid: str):
        nonlocal authority_lock_entries, inside_authority_lock
        if authority_lock_entries == 0:
            assert source_reopens == 1
        authority_lock_entries += 1
        with bound_request_state(repo_uuid) as snapshot:
            inside_authority_lock = True
            try:
                yield snapshot
            finally:
                inside_authority_lock = False

    def observed_current_source_handoff(*args: Any, **kwargs: Any):
        nonlocal source_reopens
        assert not inside_authority_lock
        source_reopens += 1
        return current_source_handoff(*args, **kwargs)

    monkeypatch.setattr(runtime.leases, "_bound_request_state", observed_bound_request_state)
    monkeypatch.setattr(owner, "_current_source_handoff", observed_current_source_handoff)

    proof = workspace_sync._finalize_semantic_result_handoff(
        runtime,
        carried_request,
        (CarriedSemanticResultEvidence(work),),
    )

    staged = runtime.generations.recover_staged_build(REPO_UUID)
    assert staged is not None
    semantic_input = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "staging"
        / carried_request.generation_id
        / SEMANTIC_INPUT_PATH
    )
    carried = parse_semantic_result_handoff(
        semantic_input.read_bytes(),
        max_bytes=carried_request.expected_payload_bytes,
    )
    first_semantic_input = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / first_request.generation_id
        / SEMANTIC_INPUT_PATH
    )
    source = parse_semantic_result_handoff(
        first_semantic_input.read_bytes(),
        max_bytes=first_request.expected_payload_bytes,
    )
    assert carried.carried_source_generation_id == first_request.generation_id
    assert proof.carried_source_generation_id == first_request.generation_id
    assert carried.results[0]["origin"] == "carried_current_generation"
    assert {key: value for key, value in carried.results[0].items() if key != "origin"} == {
        key: value for key, value in source.results[0].items() if key != "origin"
    }
    assert runtime.pointers.load(REPO_UUID) == current_pointer
    assert source_reopens >= 1
    assert staged.lifecycle_state == "COMPLETE"
    assert staged.payload_manifest_sha256 == proof.payload_manifest_sha256


def test_carried_source_receipt_is_revalidated_before_handoff_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        harness,
        runtime,
        first_request,
        carried_request,
        work,
        _current_pointer,
    ) = _carried_ready_runtime(
        tmp_path,
        monkeypatch,
    )
    expected_handoff = _handoff_path(harness, runtime, carried_request)
    staged_before = runtime.generations.recover_staged_build(REPO_UUID)
    queue_before = runtime.semantic_queue.inspect(REPO_UUID)
    receipt_path = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / first_request.generation_id
        / "receipt.json"
    )
    bound_request_state = runtime.leases._bound_request_state
    corrupted = False

    @contextmanager
    def corrupting_bound_request_state(repo_uuid: str):
        nonlocal corrupted
        with bound_request_state(repo_uuid) as snapshot:
            if not corrupted:
                corrupted = True
                receipt_path.write_bytes(b"{}\n")
                receipt_path.chmod(0o600)
            yield snapshot

    monkeypatch.setattr(
        runtime.leases,
        "_bound_request_state",
        corrupting_bound_request_state,
    )

    with pytest.raises(
        SemanticHandoffConflict,
        match="prepared carried source generation is invalid",
    ):
        workspace_sync._finalize_semantic_result_handoff(
            runtime,
            carried_request,
            (CarriedSemanticResultEvidence(work),),
        )

    assert corrupted
    assert not expected_handoff.exists()
    assert runtime.generations.recover_staged_build(REPO_UUID) == staged_before
    assert runtime.semantic_queue.inspect(REPO_UUID) == queue_before


def _materialized_entry(
    work: SemanticDesiredWork,
    payload: Mapping[str, object],
    marker: str,
) -> dict[str, object]:
    digest = marker * 64
    return {
        "result_binding": {
            "work": work.to_dict(),
            "work_sha256": digest,
            "payload": dict(payload),
            "payload_bytes": 1,
            "payload_sha256": digest,
        },
        "result_binding_sha256": digest,
    }


def test_materialization_applies_same_path_upserts_and_deletes_in_revision_order() -> None:
    policy = "b" * 64
    content = "c" * 64
    path_a = SemanticDesiredWork(1, policy, "UPSERT", "a.md", content, 1)
    delete_a = SemanticDesiredWork(1, policy, "DELETE", "a.md", content, 2)
    replacement_a = SemanticDesiredWork(1, policy, "UPSERT", "a.md", content, 3)
    path_b = SemanticDesiredWork(1, policy, "UPSERT", "b.md", content, 1)
    fragment_a = {"kind": "semantic_fragment", "fragment": _fragment("a.md")}
    fragment_b = {"kind": "semantic_fragment", "fragment": _fragment("b.md")}
    results = (
        _materialized_entry(path_a, fragment_a, "1"),
        _materialized_entry(delete_a, {"kind": "delete_tombstone"}, "2"),
        _materialized_entry(replacement_a, fragment_a, "3"),
        _materialized_entry(path_b, fragment_b, "4"),
    )

    materialized = semantic_handoff._materialize(results)
    materialized_works = [cast(Mapping[str, object], entry["work"]) for entry in materialized]

    assert [work["path"] for work in materialized_works] == ["a.md", "b.md"]
    assert materialized_works[0]["desired_revision"] == 3
    with pytest.raises(SemanticHandoffInvalid, match="not ascending"):
        semantic_handoff._materialize((results[1], results[0]))
