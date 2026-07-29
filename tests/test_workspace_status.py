from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, cast, Iterator

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.composition import (
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
)
from graphify.workspace.cli import run_workspace_command
from graphify.workspace.contracts import (
    CapacityPolicy,
    CompatibilityManifest,
    FreshnessRelease,
    GcIntentState,
    PointerSet,
    StagedBuildAbandonmentEvidence,
    StagedBuildState,
    StructuralBuildRequest,
    canonical_json_bytes,
)
from graphify.workspace.freshness import FreshnessAuthority
from graphify.workspace.gc import GcStore
from graphify.workspace.identity import discover_source
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseStore
from graphify.workspace.persistence import (
    DurableStateRoot,
    LockTimeout,
    RuntimeCapabilities,
    StatePathError,
)
from graphify.workspace.registry import RegistryStore
from graphify.workspace.pointers import PointerStore
from graphify.workspace.semantic_queue import (
    SemanticDesiredWork,
    SemanticQueueItem,
    SemanticQueuePolicy,
    SemanticQueueSnapshot,
    SemanticQueueStore,
)
from graphify.workspace.status import (
    ACTION_CODES,
    REASON_CODES,
    WorkspaceStatusReport,
    inspect_workspace_status,
    load_status_schema,
)
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    START,
    SUPPORTED,
    RuntimeHarness,
    acquire,
    authorization,
    create_harness,
    create_repo,
    metadata_snapshot,
    tree_snapshot,
)


SECOND_UUID = "22222222-2222-4222-8222-222222222222"
FIXTURES = Path(__file__).parent / "fixtures" / "workspace" / "v1"
QUEUE_POLICY = SemanticQueuePolicy(
    max_items=8,
    max_bytes=16 * 1024,
    retry_budget=1,
)
_STAGED_CAPACITY_POLICY = CapacityPolicy.from_mapping(
    {
        "contract": "graphify.workspace.capacity_policy.internal",
        "format_version": 1,
        "global_max_bytes": 32 * 1024 * 1024,
        "global_max_generations": 16,
        "workspace_max_bytes": 8 * 1024 * 1024,
        "workspace_max_generations": 8,
        "reserve_bytes": 1024,
    }
)


def _inputs(
    state_root: Path,
    *,
    compatibility_manifest: CompatibilityManifest = COMPATIBILITY_MANIFEST,
    capabilities: RuntimeCapabilities = SUPPORTED,
) -> WorkspaceRuntimeInputs:
    return WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=compatibility_manifest,
        semantic_queue_policy=QUEUE_POLICY,
        capabilities=capabilities,
    )


def _reason_codes(report: Any) -> set[str]:
    value = report.to_dict()
    return {
        str(value["reason_code"]),
        *(str(check["reason_code"]) for check in value["checks"]),
    }


def _fresh_runtime_with_staged_request(
    tmp_path: Path,
    *,
    queue_state: str | None = None,
) -> tuple[Any, Any, StructuralBuildRequest]:
    """Create a current pointer plus a successor staged request without a lease."""

    from tests.test_workspace_freshness import QUEUE_POLICY as FRESH_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as fresh_runtime

    fresh = fresh_runtime(tmp_path)
    inputs = WorkspaceRuntimeInputs(
        state_root=fresh.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=FRESH_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )
    runtime = compose_workspace_runtime(inputs)
    if queue_state is not None:
        from tests.test_workspace_semantic_queue import _host_claim_inputs

        harness = RuntimeHarness(
            repo=fresh.repo,
            state_root=fresh.state_root,
            registry=runtime.registry,
            leases=runtime.leases,
        )
        queue = runtime.generations.semantic_queue
        assert queue is not None
        build = acquire(harness, "BUILD", tick=3)
        queue.enqueue(
            build,
            SemanticDesiredWork(
                source_epoch=1,
                policy_sha256="1" * 64,
                operation="UPSERT",
                path="docs/staged-status.md",
                content_sha256="2" * 64,
                desired_revision=2,
            ),
            monotonic_ns=30_001,
        )
        harness.leases.release(build)
        if queue_state == "dead_letter":
            semantic = acquire(harness, "SEMANTIC_CLAIM", tick=4)
            claim = queue.claim(
                semantic,
                **_host_claim_inputs(harness),
                monotonic_ns=40_001,
            )
            assert claim is not None
            queue.fail(
                semantic,
                claim,
                error_code="staged_status_test",
                retryable=False,
                monotonic_ns=40_002,
            )
            harness.leases.release(semantic)
    registry = runtime.registry.load().to_dict()
    entry = registry["workspaces"][0]
    lease_state = runtime.leases.inspect(REPO_UUID)
    observations = (
        runtime.generations.adapter.observe(fresh.repo),
        runtime.generations.adapter.observe(fresh.repo),
    )
    observation = runtime.generations._source_observation_document(observations[0])
    pointer = runtime.pointers.load(REPO_UUID)
    assert pointer is not None
    pointer_value = pointer.to_dict()
    current = cast(dict[str, str], pointer_value["current"])
    request = StructuralBuildRequest.from_mapping(
        {
            "logical_request_sha256": "a" * 64,
            "expected_registry_revision": int(registry["revision"]),
            "expected_active_source_revision": int(entry["active_source_revision"]),
            "expected_operation_epoch": lease_state.operation_epoch,
            "expected_migration_epoch": lease_state.migration_epoch,
            "expected_pointer_revision": int(pointer_value["pointer_revision"]),
            "expected_current_receipt_sha256": current["receipt_sha256"],
            "source_commit": observations[0].source_commit,
            "source_epoch": int(pointer_value["source_epoch"]),
            "policy_sha256": observations[0].policy_sha256,
            "observation_manifest_sha256": observations[0].inventory_sha256,
            "observation_evidence_sha256": runtime.generations.structural_observation_evidence_sha256(
                observations
            ),
            "observation_detector_id": observation["detector_id"],
            "observation_entries_sha256": observation["entries_sha256"],
            "expected_payload_bytes": 4096,
            "capacity_policy_sha256": _STAGED_CAPACITY_POLICY.sha256,
            "compatibility_sha256": COMPATIBILITY_MANIFEST.sha256,
        }
    )
    state = runtime.generations.request_staged_build(
        REPO_UUID,
        "gen-status-staged",
        request,
        source_observations=observations,
    )
    assert state.lifecycle_state == "REQUESTED"
    return fresh, runtime, request


def _write_staged_state(runtime: Any, state: StagedBuildState) -> None:
    current, _previous, _pending = runtime.generations._staged_build_paths(REPO_UUID)
    path = runtime.generations.state.path(current)
    path.write_bytes(state.canonical)
    path.chmod(0o600)


def _staged_state(
    request: StructuralBuildRequest,
    lifecycle_state: str,
    *,
    pointer_revision: int | None = None,
) -> StagedBuildState:
    payload = "b" * 64 if lifecycle_state in {"COMPLETE", "CERTIFIED", "PROMOTED"} else None
    receipt = "c" * 64 if lifecycle_state in {"CERTIFIED", "PROMOTED"} else None
    return StagedBuildState.from_mapping(
        {
            "contract": "graphify.workspace.staged_build.internal",
            "format_version": 1,
            "revision": 2,
            "repo_uuid": REPO_UUID,
            "generation_id": "gen-status-staged",
            "request": request.to_dict(),
            "request_sha256": request.sha256,
            "lifecycle_state": lifecycle_state,
            "operation_epoch": None if lifecycle_state == "REQUESTED" else 1,
            "fence_token": None if lifecycle_state == "REQUESTED" else 1,
            "payload_manifest_sha256": payload,
            "receipt_sha256": receipt,
            "pointer_revision": pointer_revision,
            "abandonment_intent": None,
            "abandoned_from": None,
            "abandon_reason": None,
            "abandon_evidence": None,
            "abandon_evidence_sha256": None,
        }
    )


def _abandoned_staged_state(request: StructuralBuildRequest) -> StagedBuildState:
    evidence = StagedBuildAbandonmentEvidence(
        request_sha256=request.sha256,
        registry_revision=request.expected_registry_revision,
        active_source_revision=request.expected_active_source_revision + 1,
        operation_epoch=request.expected_operation_epoch,
        migration_epoch=request.expected_migration_epoch,
        pointer_revision=request.expected_pointer_revision,
        current_receipt_sha256=request.expected_current_receipt_sha256,
        selected_compatibility_sha256=request.compatibility_sha256,
        semantic_source_epoch=None,
        semantic_queue_watermark=None,
        semantic_queue_state_sha256=None,
        source_commit=request.source_commit,
        source_inventory_sha256=request.observation_manifest_sha256,
        source_policy_sha256=request.policy_sha256,
        source_detector_id=request.observation_detector_id,
        source_stable_inventory_passes=2,
        source_entries_sha256=request.observation_entries_sha256,
        source_observation_evidence_sha256=request.observation_evidence_sha256,
    )
    return StagedBuildState.from_mapping(
        {
            "contract": "graphify.workspace.staged_build.internal",
            "format_version": 1,
            "revision": 2,
            "repo_uuid": REPO_UUID,
            "generation_id": "gen-status-staged",
            "request": request.to_dict(),
            "request_sha256": request.sha256,
            "lifecycle_state": "ABANDONED",
            "operation_epoch": 1,
            "fence_token": 1,
            "payload_manifest_sha256": None,
            "receipt_sha256": None,
            "pointer_revision": None,
            "abandonment_intent": None,
            "abandoned_from": "REQUESTED",
            "abandon_reason": "ACTIVE_SOURCE_CHANGED",
            "abandon_evidence": evidence.to_dict(),
            "abandon_evidence_sha256": evidence.sha256,
        }
    )


def _gc_intent(pointer: PointerSet, *, capacity_policy_sha256: str) -> GcIntentState:
    pointer_value = pointer.to_dict()
    return GcIntentState.from_mapping(
        {
            "contract": "graphify.workspace.gc_intent.internal",
            "format_version": 1,
            "repo_uuid": REPO_UUID,
            "operation_epoch": 3,
            "fence_token": 3,
            "active_source_revision": 1,
            "migration_epoch": 0,
            "pointer_revision": pointer_value["pointer_revision"],
            "capacity_policy_sha256": capacity_policy_sha256,
            "plan_sha256": "a" * 64,
            "candidates": [],
            "occurred_at": "2026-07-16T19:00:00Z",
        }
    )


def _unsupported_manifest() -> CompatibilityManifest:
    value = {
        **COMPATIBILITY_MANIFEST.to_dict(),
        "engine_baseline": "0.9.15",
    }
    return CompatibilityManifest(
        contract=cast(str, CompatibilityManifest.CONTRACT),
        schema_version=1,
        canonical=canonical_json_bytes(value),
    )


def _set_remote(repo: Path, url: str) -> None:
    subprocess.run(
        ["git", "remote", "set-url", "origin", url],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _hold_exclusive_lock(path: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time; "
                "fd=os.open(sys.argv[1], os.O_RDONLY); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "print('READY', flush=True); time.sleep(60)"
            ),
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "READY"
    return holder


def _xattr_snapshot(root: Path) -> dict[str, tuple[tuple[str, bytes], ...]]:
    result: dict[str, tuple[tuple[str, bytes], ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        try:
            listxattr = getattr(os, "listxattr")
            getxattr = getattr(os, "getxattr")
            names = sorted(listxattr(path, follow_symlinks=False))
            values = tuple((name, getxattr(path, name, follow_symlinks=False)) for name in names)
        except (AttributeError, OSError):
            values = ()
        result[relative] = values
    return result


def test_status_reports_enrolled_uninitialized_workspace_as_degraded(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)

    report = inspect_workspace_status(_inputs(harness.state_root))
    value = report.to_dict()

    assert value["contract"] == "graphify.workspace.status"
    assert value["schema_version"] == 2
    assert value["cli_contract_version"] == 1
    assert value["state"] == "degraded"
    assert value["exit_code"] == 10
    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert "no_current_generation" in _reason_codes(report)


def test_status_reports_certified_visible_generation_as_ready(tmp_path: Path) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    inputs = WorkspaceRuntimeInputs(
        state_root=runtime.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )

    report = inspect_workspace_status(inputs)
    value = report.to_dict()

    assert report.exit_code == 0
    assert value["state"] == "ready"
    assert value["safe_to_query"] is True
    assert value["workspaces"][0]["freshness"]["state"] == "observed_current"
    assert value["workspaces"][0]["freshness"]["binding"] == {
        "active_source_revision": 1,
        "pointer_revision": 1,
        "receipt_sha256": value["workspaces"][0]["generations"]["current"]["receipt_sha256"],
    }
    assert value["workspaces"][0]["generations"]["pointer_revision"] == 1
    assert value["workspaces"][0]["generations"]["pending"] == []
    assert value["workspaces"][0]["generations"]["pending_reason_code"] == "ready"
    assert value["workspaces"][0]["repair"]["count"] == 0
    assert value["workspaces"][0]["journal"]["last_successful_transition"] == "PROMOTED"
    assert value["workspaces"][0]["journal"]["last_failure_classification"] is None
    assert value["workspaces"][0]["generations"]["current"]["generation_id"] == "gen-current"
    Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).validate(value)


@pytest.mark.parametrize("lifecycle_state", ["REQUESTED", "PUBLISHING", "COMPLETE", "CERTIFIED"])
def test_status_blocks_query_when_a_nonterminal_staged_build_has_no_live_lease(
    tmp_path: Path,
    lifecycle_state: str,
) -> None:
    fresh, runtime, request = _fresh_runtime_with_staged_request(tmp_path)
    from tests.test_workspace_freshness import QUEUE_POLICY as FRESH_QUEUE_POLICY

    if lifecycle_state != "REQUESTED":
        _write_staged_state(runtime, _staged_state(request, lifecycle_state))
    before_tree = tree_snapshot(fresh.state_root)
    before_metadata = metadata_snapshot(fresh.state_root)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=fresh.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=FRESH_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()
    workspace = value["workspaces"][0]

    assert value["schema_version"] == 2
    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert workspace["safe_to_query"] is False
    assert workspace["state"] == "degraded"
    assert workspace["reason_code"] == "staged_build_recovery_required"
    assert workspace["action_code"] == "resume_exact_workspace_sync"
    assert workspace["staged_build"] == {
        "present": True,
        "blocking": True,
        "revision": 1 if lifecycle_state == "REQUESTED" else 2,
        "generation_id": "gen-status-staged",
        "lifecycle_state": lifecycle_state,
        "logical_request_sha256": "a" * 64,
        "request_sha256": request.sha256,
    }
    assert any(
        check == {
            "component": f"workspace:{REPO_UUID}:staged_build",
            "state": "degraded",
            "reason_code": "staged_build_recovery_required",
            "action_code": "resume_exact_workspace_sync",
        }
        for check in value["checks"]
    )
    assert tree_snapshot(fresh.state_root) == before_tree
    assert metadata_snapshot(fresh.state_root) == before_metadata


@pytest.mark.parametrize("lifecycle_state", ["REQUESTED", "PUBLISHING", "COMPLETE", "CERTIFIED"])
@pytest.mark.parametrize(
    ("queue_state", "queue_reason"),
    [
        ("pending", "semantic_queue_pending"),
        ("dead_letter", "semantic_queue_dead_letter"),
    ],
)
def test_status_keeps_staged_resume_primary_over_semantic_queue_degradation(
    tmp_path: Path,
    lifecycle_state: str,
    queue_state: str,
    queue_reason: str,
) -> None:
    fresh, runtime, request = _fresh_runtime_with_staged_request(
        tmp_path,
        queue_state=queue_state,
    )
    from tests.test_workspace_freshness import QUEUE_POLICY as FRESH_QUEUE_POLICY

    if lifecycle_state != "REQUESTED":
        _write_staged_state(runtime, _staged_state(request, lifecycle_state))
    inputs = WorkspaceRuntimeInputs(
        state_root=fresh.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=FRESH_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )

    report = inspect_workspace_status(inputs)
    value = report.to_dict()
    workspace = value["workspaces"][0]

    assert report.exit_code == 10
    assert value["reason_code"] == "staged_build_recovery_required"
    assert value["action_code"] == "resume_exact_workspace_sync"
    assert workspace["reason_code"] == "staged_build_recovery_required"
    assert workspace["action_code"] == "resume_exact_workspace_sync"
    assert queue_reason in _reason_codes(report)
    Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).validate(value)

    doctor_stdout = StringIO()
    doctor_stderr = StringIO()
    assert run_workspace_command(
        ("doctor",),
        inputs=inputs,
        stdout=doctor_stdout,
        stderr=doctor_stderr,
    ) == 10
    assert "staged_build_recovery_required" in doctor_stdout.getvalue()
    assert "resume_exact_workspace_sync" in doctor_stdout.getvalue()
    assert doctor_stderr.getvalue() == ""

    wrong_primary = deepcopy(value)
    wrong_primary["reason_code"] = queue_reason
    wrong_primary["action_code"] = "drain_semantic_queue"
    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )
    assert not validator.is_valid(wrong_primary)

    wrong_workspace = deepcopy(value)
    wrong_workspace["workspaces"][0]["reason_code"] = queue_reason
    wrong_workspace["workspaces"][0]["action_code"] = "drain_semantic_queue"
    assert not validator.is_valid(wrong_workspace)


def test_status_terminal_promoted_staged_build_does_not_block_query(tmp_path: Path) -> None:
    fresh, runtime, request = _fresh_runtime_with_staged_request(tmp_path)
    _write_staged_state(runtime, _staged_state(request, "PROMOTED", pointer_revision=1))
    from tests.test_workspace_freshness import QUEUE_POLICY as FRESH_QUEUE_POLICY

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=fresh.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=FRESH_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 0
    assert report.to_dict()["safe_to_query"] is True
    assert report.to_dict()["workspaces"][0]["staged_build"]["blocking"] is False


def test_status_corrupt_staged_build_fails_closed_without_creating_state(
    tmp_path: Path,
) -> None:
    fresh, runtime, _request = _fresh_runtime_with_staged_request(tmp_path)
    current, _previous, _pending = runtime.generations._staged_build_paths(REPO_UUID)
    runtime.generations.state.path(current).write_bytes(b'{"not":"a staged build"}\n')
    before_tree = tree_snapshot(fresh.state_root)
    before_metadata = metadata_snapshot(fresh.state_root)
    from tests.test_workspace_freshness import QUEUE_POLICY as FRESH_QUEUE_POLICY

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=fresh.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=FRESH_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 20
    assert report.to_dict()["safe_to_query"] is False
    assert report.to_dict()["workspaces"][0]["reason_code"] == "staged_build_invalid"
    assert Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).is_valid(report.to_dict())
    assert tree_snapshot(fresh.state_root) == before_tree
    assert metadata_snapshot(fresh.state_root) == before_metadata


def test_status_reports_pending_staged_commit_as_exact_sync_recovery(
    tmp_path: Path,
) -> None:
    fresh, runtime, _request = _fresh_runtime_with_staged_request(tmp_path)
    current, _previous, pending = runtime.generations._staged_build_paths(REPO_UUID)
    current_path = runtime.generations.state.path(current)
    pending_path = runtime.generations.state.path(pending)
    pending_path.write_bytes(current_path.read_bytes())
    pending_path.chmod(0o600)
    before_tree = tree_snapshot(fresh.state_root)
    before_metadata = metadata_snapshot(fresh.state_root)
    from tests.test_workspace_freshness import QUEUE_POLICY as FRESH_QUEUE_POLICY

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=fresh.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=FRESH_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()
    workspace = value["workspaces"][0]

    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert workspace["reason_code"] == "staged_build_recovery_required"
    assert workspace["action_code"] == "resume_exact_workspace_sync"
    assert workspace["repair"] == {"required": False, "count": None}
    assert workspace["staged_build"] == {
        "present": True,
        "blocking": True,
        "revision": None,
        "generation_id": None,
        "lifecycle_state": None,
        "logical_request_sha256": None,
        "request_sha256": None,
    }
    assert Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).is_valid(value)
    assert tree_snapshot(fresh.state_root) == before_tree
    assert metadata_snapshot(fresh.state_root) == before_metadata


@pytest.mark.parametrize("mutation", ["edit", "create", "delete", "policy"])
def test_status_withholds_query_safety_after_source_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    if mutation == "edit":
        (runtime.repo / "README.md").write_text("changed\n", encoding="utf-8")
    elif mutation == "create":
        (runtime.repo / "new.py").write_text("value = 1\n", encoding="utf-8")
    elif mutation == "delete":
        (runtime.repo / "README.md").unlink()
    else:
        config = runtime.repo / ".graphify/workspace.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                'semantic_mode = "host_agent_only"',
                'semantic_mode = "disabled"',
            ),
            encoding="utf-8",
        )
    inputs = WorkspaceRuntimeInputs(
        state_root=runtime.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )

    report = inspect_workspace_status(inputs)
    value = report.to_dict()

    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert value["workspaces"][0]["safe_to_query"] is False
    assert value["workspaces"][0]["freshness"]["state"] != "observed_current"
    assert value["workspaces"][0]["freshness"]["observation_boundary"] != "two_sided"
    assert value["workspaces"][0]["reason_code"] in {
        "source_drift",
        "source_unavailable",
    }


def test_status_document_validates_against_the_versioned_cli_schema(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    report = inspect_workspace_status(_inputs(harness.state_root))

    Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).validate(report.to_dict())


def test_status_schema_rejects_unknown_codes_and_contradictory_exit_state(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    value = inspect_workspace_status(_inputs(harness.state_root)).to_dict()
    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )

    unknown_code = {**value, "reason_code": "future_unversioned_reason"}
    contradictory = {
        **value,
        "state": "ready",
        "exit_code": 20,
        "safe_to_query": True,
    }

    assert not validator.is_valid(unknown_code)
    assert not validator.is_valid(contradictory)


def test_status_schema_and_runtime_reject_semantically_unready_ready_documents(
    tmp_path: Path,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    value = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    ).to_dict()
    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )
    workspace = {**value["workspaces"][0]}
    workspace["freshness"] = {
        **workspace["freshness"],
        "state": "not_observed",
        "duration_ms": None,
        "observation_boundary": "not_observed",
        "binding": None,
    }
    not_observed = {**value, "workspaces": [workspace]}
    no_workspaces = {**value, "workspaces": []}
    contradictory_checks = {
        **value,
        "checks": [
            {**value["checks"][0], "state": "degraded"},
            *value["checks"][1:],
        ],
    }

    for document in (not_observed, no_workspaces, contradictory_checks):
        assert not validator.is_valid(document)
        with pytest.raises(ValueError):
            WorkspaceStatusReport(document)


def test_status_schema_code_catalog_matches_runtime_catalog() -> None:
    schema = load_status_schema()

    assert set(schema["$defs"]["reason_code"]["enum"]) == REASON_CODES
    assert set(schema["$defs"]["action_code"]["enum"]) == ACTION_CODES


def test_status_runtime_validator_fails_closed_on_unknown_schema_keyword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    document = inspect_workspace_status(_inputs(harness.state_root)).to_dict()
    schema = deepcopy(load_status_schema())
    schema["properties"]["checks"]["maxItems"] = 0
    monkeypatch.setattr("graphify.workspace.status.load_status_schema", lambda: schema)

    with pytest.raises(ValueError, match="unsupported schema keyword.*maxItems"):
        WorkspaceStatusReport(document)


def test_status_report_rejects_unknown_codes_and_contradictory_exit_state(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    value = inspect_workspace_status(_inputs(harness.state_root)).to_dict()

    with pytest.raises(ValueError, match="reason code"):
        WorkspaceStatusReport({**value, "reason_code": "future_unversioned_reason"})
    with pytest.raises(ValueError, match="state and exit code"):
        WorkspaceStatusReport(
            {
                **value,
                "state": "ready",
                "exit_code": 20,
                "safe_to_query": True,
            }
        )


@pytest.mark.parametrize(
    "path",
    [
        ("reason_code",),
        ("action_code",),
        ("correlation_id",),
        ("runtime",),
        ("runtime", "distribution_version"),
        ("checks", 0, "component"),
        ("workspaces", 0, "source_identity_sha256"),
        ("workspaces", 0, "generations", "current"),
        ("workspaces", 0, "queue", "revision"),
        ("workspaces", 0, "leases", "workspace", "present"),
        ("workspaces", 0, "journal", "sequence"),
        ("workspaces", 0, "freshness", "state"),
        ("workspaces", 0, "watcher", "heartbeat"),
        ("workspaces", 0, "resources", "pressure"),
        ("workspaces", 0, "repair", "required"),
    ],
)
def test_status_report_enforces_required_schema_fields(
    tmp_path: Path,
    path: tuple[str | int, ...],
) -> None:
    harness = create_harness(tmp_path)
    document = deepcopy(inspect_workspace_status(_inputs(harness.state_root)).to_dict())
    parent: Any = document
    for part in path[:-1]:
        parent = parent[part]
    del parent[path[-1]]

    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )
    assert not validator.is_valid(document)
    with pytest.raises(ValueError, match="schema"):
        WorkspaceStatusReport(document)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("correlation_id",), "status-not-a-correlation-id"),
        (("runtime", "compatibility_sha256"), "not-a-digest"),
        (("checks", 0, "unexpected"), True),
        (("workspaces", 0, "repo_uuid"), "not-a-uuid"),
        (("workspaces", 0, "repo_uuid"), "11111111111141118111111111111111"),
        (("workspaces", 0, "active_source_revision"), 0),
        (("workspaces", 0, "queue", "depth"), -1),
        (("workspaces", 0, "freshness", "duration_ms"), -1),
    ],
)
def test_status_report_enforces_structural_schema_constraints(
    tmp_path: Path,
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    harness = create_harness(tmp_path)
    document = deepcopy(inspect_workspace_status(_inputs(harness.state_root)).to_dict())
    parent: Any = document
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = replacement

    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )
    assert not validator.is_valid(document)
    with pytest.raises(ValueError, match="schema"):
        WorkspaceStatusReport(document)


def test_status_v2_schema_rejects_unknown_nested_workspace_fields(tmp_path: Path) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    document = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    ).to_dict()
    document["workspaces"][0]["generations"]["future_unversioned_field"] = True

    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )
    assert not validator.is_valid(document)


@pytest.mark.parametrize(
    "mutation",
    [
        "absent_with_detail",
        "nonterminal_not_blocking",
        "terminal_blocking",
        "blocking_ready",
    ],
)
def test_status_v2_schema_rejects_contradictory_staged_builds(
    tmp_path: Path,
    mutation: str,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    document = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    ).to_dict()
    staged = document["workspaces"][0]["staged_build"]
    populated = {
        "present": True,
        "blocking": True,
        "revision": 1,
        "generation_id": "gen-status-staged",
        "lifecycle_state": "REQUESTED",
        "logical_request_sha256": "a" * 64,
        "request_sha256": "b" * 64,
    }

    if mutation == "absent_with_detail":
        staged["request_sha256"] = "b" * 64
    elif mutation == "nonterminal_not_blocking":
        document["workspaces"][0]["staged_build"] = {**populated, "blocking": False}
    elif mutation == "terminal_blocking":
        document["workspaces"][0]["staged_build"] = {
            **populated,
            "lifecycle_state": "PROMOTED",
        }
    else:
        document["workspaces"][0]["staged_build"] = populated

    validator = Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    )
    assert not validator.is_valid(document)


def test_status_report_rejects_nested_reason_codes_outside_schema_constraints(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)

    unknown_pending = inspect_workspace_status(_inputs(harness.state_root)).to_dict()
    unknown_pending["workspaces"][0]["generations"]["pending_reason_code"] = (
        "future_unversioned_reason"
    )
    with pytest.raises(ValueError, match="reason code"):
        WorkspaceStatusReport(unknown_pending)

    unsupported_age = inspect_workspace_status(_inputs(harness.state_root)).to_dict()
    unsupported_age["workspaces"][0]["queue"]["age_reason_code"] = "ready"
    with pytest.raises(ValueError, match="age reason code"):
        WorkspaceStatusReport(unsupported_age)


def test_status_serialization_is_byte_stable_and_sorted(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    second = create_repo(tmp_path / "repo-second", SECOND_UUID)
    first = create_repo(tmp_path / "repo-first", REPO_UUID)
    _set_remote(second, "https://github.com/example/status-second.git")
    _set_remote(first, "https://github.com/example/status-first.git")
    registry.enroll(discover_source(second), authorization("status-second"), expected_revision=0)
    registry.enroll(discover_source(first), authorization("status-first"), expected_revision=1)

    first_report = inspect_workspace_status(_inputs(state_root))
    second_report = inspect_workspace_status(_inputs(state_root))
    value = first_report.to_dict()

    assert first_report.canonical == canonical_json_bytes(value)
    assert second_report.canonical == first_report.canonical
    assert [item["repo_uuid"] for item in value["workspaces"]] == [REPO_UUID, SECOND_UUID]
    assert value["checks"] == sorted(
        value["checks"],
        key=lambda item: (
            item["component"],
            item["state"],
            item["reason_code"],
            item["action_code"],
        ),
    )


def test_status_reports_missing_state_root_as_invalid_without_creating_it(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "missing-state"

    report = inspect_workspace_status(_inputs(state_root))

    assert report.exit_code == 20
    assert report.to_dict()["state"] == "invalid"
    assert "state_root_missing" in _reason_codes(report)
    assert not state_root.exists()


def test_status_reports_uninspectable_state_root_as_unsafe_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    secret = "operator-secret-state-parent"
    before_tree = tree_snapshot(state_root)
    before_metadata = metadata_snapshot(state_root)

    original_stat = os.stat

    def reject_root_binding(
        path: os.PathLike[str] | str,
        *args: Any,
        **kwargs: Any,
    ) -> os.stat_result:
        if path == state_root.name and kwargs.get("dir_fd") is not None:
            raise FileNotFoundError(secret)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", reject_root_binding)

    report = inspect_workspace_status(_inputs(state_root))

    assert report.exit_code == 20
    assert "unsafe_state_path" in _reason_codes(report)
    assert "state_root_missing" not in _reason_codes(report)
    assert secret.encode("utf-8") not in report.canonical
    assert secret not in str(report.to_dict())
    assert tree_snapshot(state_root) == before_tree
    assert metadata_snapshot(state_root) == before_metadata


def test_status_reports_missing_registry_lock_without_recreating_it(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    lock = harness.state_root / RegistryStore.LOCK
    lock.unlink()

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "registry_lock_missing" in _reason_codes(report)
    assert not lock.exists()


def test_status_reports_malformed_registry_as_invalid_and_redacts_content(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    secret = "status-secret-token"
    registry = harness.state_root / RegistryStore.CURRENT
    registry.write_text(f'{{"token":"{secret}"}}\n', encoding="utf-8")
    registry.chmod(0o600)

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "registry_invalid" in _reason_codes(report)
    assert secret.encode("utf-8") not in report.canonical
    assert secret not in str(report.to_dict())


def test_status_reports_unsupported_runtime_as_invalid(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    unsupported = RuntimeCapabilities(
        system="Linux",
        filesystem="ext4",
        elevated=False,
        local=True,
    )

    report = inspect_workspace_status(_inputs(state_root, capabilities=unsupported))

    assert report.exit_code == 20
    assert "unsupported_runtime" in _reason_codes(report)


def test_status_reports_unsupported_compatibility_as_invalid(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)

    report = inspect_workspace_status(
        _inputs(state_root, compatibility_manifest=_unsupported_manifest())
    )

    assert report.exit_code == 20
    assert "unsupported_compatibility" in _reason_codes(report)


def test_status_reports_missing_workspace_record_as_invalid(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    workspace_record = harness.state_root / "workspaces" / REPO_UUID / "workspace.json"
    workspace_record.unlink()

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "workspace_record_missing" in _reason_codes(report)


def test_status_classifies_missing_workspace_record_from_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.persistence import StateRecordMissing

    harness = create_harness(tmp_path)

    def missing_workspace_record(*_args: object, **_kwargs: object) -> object:
        raise StateRecordMissing("wording deliberately omits the old message marker")

    monkeypatch.setattr(
        LeaseStore,
        "read_only_snapshot_locked",
        missing_workspace_record,
    )

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "workspace_record_missing" in _reason_codes(report)
    assert "workspace_record_invalid" not in _reason_codes(report)


def test_status_reports_malformed_workspace_record_without_leaking_it(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    secret = "workspace-private-secret"
    workspace_record = harness.state_root / "workspaces" / REPO_UUID / "workspace.json"
    workspace_record.write_text(f'{{"secret":"{secret}"}}\n', encoding="utf-8")
    workspace_record.chmod(0o600)

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "workspace_record_invalid" in _reason_codes(report)
    assert secret.encode("utf-8") not in report.canonical


def test_status_reports_malformed_semantic_queue_as_invalid(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    queue_directory = harness.state_root / "workspaces" / REPO_UUID / "queue"
    queue_directory.mkdir(mode=0o700)
    queue_record = queue_directory / "semantic.jsonl"
    queue_record.write_bytes(b"not canonical queue JSON\n")
    queue_record.chmod(0o600)

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "semantic_queue_invalid" in _reason_codes(report)


def test_status_rejects_policy_matching_semantic_queue_over_item_capacity(
    tmp_path: Path,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    queue = runtime.pointers.generations.semantic_queue
    assert queue is not None
    snapshot = queue.inspect(REPO_UUID)
    items = tuple(
        SemanticQueueItem(
            work=SemanticDesiredWork(
                source_epoch=1,
                policy_sha256="1" * 64,
                operation="UPSERT",
                path=f"docs/capacity-{index:02}.md",
                content_sha256=f"{index:064x}",
                desired_revision=index,
            ),
            status="completed",
            failure_count=0,
            last_error=None,
            claim=None,
        )
        for index in range(1, CERTIFIED_QUEUE_POLICY.max_items + 2)
    )
    oversized = SemanticQueueSnapshot.from_mapping(
        replace(
            snapshot,
            revision=snapshot.revision + 1,
            desired_watermark=len(items),
            completed_watermark=len(items),
            reconciliation=None,
            items=items,
        ).to_dict()
    )
    assert len(oversized.items) > CERTIFIED_QUEUE_POLICY.max_items
    current, _previous, _pending = queue._paths(REPO_UUID)
    current_path = queue.state.path(current)
    current_path.write_bytes(oversized.canonical)
    current_path.chmod(0o600)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()
    queue_check = next(
        check
        for check in value["checks"]
        if check["component"] == f"workspace:{REPO_UUID}:semantic_queue"
    )

    assert report.exit_code == 20
    assert queue_check["state"] == "invalid"
    assert queue_check["reason_code"] == "semantic_queue_invalid"
    assert value["workspaces"][0]["repair"]["required"] is True


def test_status_reports_pointer_pending_recovery_as_invalid(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    pointer = PointerSet.from_json((FIXTURES / "positive" / "pointer-set.json").read_bytes())
    pending = harness.state_root / "workspaces" / REPO_UUID / "pointers.pending.json"
    pending.write_bytes(pointer.canonical)
    pending.chmod(0o600)

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "pointer_recovery_required" in _reason_codes(report)


def test_status_reports_unresolved_gc_intent_as_repair_required(tmp_path: Path) -> None:
    from tests.test_workspace_freshness import POLICY
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    pointer = runtime.pointers.load(REPO_UUID, allow_missing=False)
    assert pointer is not None
    intent = _gc_intent(pointer, capacity_policy_sha256=POLICY.sha256)
    gc_directory = runtime.state_root / "workspaces" / REPO_UUID / "gc"
    gc_directory.mkdir(mode=0o700)
    intent_path = gc_directory / "intent.json"
    intent_path.write_bytes(intent.canonical)
    intent_path.chmod(0o600)
    before = tree_snapshot(runtime.state_root)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()
    gc_check = next(
        check
        for check in value["checks"]
        if check["component"] == f"workspace:{REPO_UUID}:gc"
    )

    assert report.exit_code == 20
    assert gc_check["state"] == "invalid"
    assert gc_check["reason_code"] == "workspace_state_invalid"
    assert gc_check["action_code"] == "run_workspace_gc_reconcile"
    assert value["workspaces"][0]["repair"]["required"] is True
    assert tree_snapshot(runtime.state_root) == before


def test_status_bounds_oversized_gc_intent_without_leaking_or_writing(
    tmp_path: Path,
) -> None:
    from graphify.workspace.gc import _MAX_GC_INTENT_BYTES
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    secret = b"gc-intent-secret"
    gc_directory = runtime.state_root / "workspaces" / REPO_UUID / "gc"
    gc_directory.mkdir(mode=0o700)
    intent_path = gc_directory / "intent.json"
    intent_path.write_bytes(secret + b"x" * _MAX_GC_INTENT_BYTES)
    intent_path.chmod(0o600)
    before = tree_snapshot(runtime.state_root)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 20
    assert "workspace_state_invalid" in _reason_codes(report)
    assert secret not in report.canonical
    assert tree_snapshot(runtime.state_root) == before


def test_status_derives_pending_generations_and_repair_count_from_journal(
    tmp_path: Path,
) -> None:
    from tests.test_workspace_freshness import POLICY

    harness = create_harness(tmp_path)
    runtime = compose_workspace_runtime(_inputs(harness.state_root))
    composed_harness = RuntimeHarness(
        repo=harness.repo,
        state_root=harness.state_root,
        registry=runtime.registry,
        leases=runtime.leases,
    )
    build = acquire(composed_harness, "BUILD", tick=1)
    runtime.generations.allocate(
        build,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-pending",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    runtime.leases.release(build)

    report = inspect_workspace_status(_inputs(harness.state_root))
    workspace = report.to_dict()["workspaces"][0]

    assert report.exit_code == 10
    assert workspace["generations"]["pointer_revision"] is None
    assert workspace["generations"]["pending"] == [
        {
            "generation_id": "gen-pending",
            "lifecycle_state": "STAGING",
            "receipt_sha256": None,
        }
    ]
    assert workspace["generations"]["pending_reason_code"] == "generation_pending"
    assert workspace["repair"]["count"] == 0
    assert workspace["journal"]["last_successful_transition"] == "STAGING"
    Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).validate(report.to_dict())


def test_status_rejects_freshness_release_for_a_different_generation_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    observed = runtime.authority.probe(REPO_UUID)
    assert observed.release is not None
    release_value = observed.release.to_dict()
    for phase in ("pre_observation", "post_observation"):
        release_value[phase] = {
            **release_value[phase],
            "pointer_revision": int(release_value[phase]["pointer_revision"]) + 1,
            "receipt_sha256": "f" * 64,
        }
    mismatched = replace(
        observed,
        release=FreshnessRelease.from_mapping(release_value),
    )
    monkeypatch.setattr(
        FreshnessAuthority,
        "probe",
        lambda *_args, **_kwargs: mismatched,
    )

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 10
    assert report.to_dict()["safe_to_query"] is False
    assert "status_snapshot_changed" in _reason_codes(report)


def test_status_revalidates_workspace_state_after_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    harness = RuntimeHarness(
        repo=runtime.repo,
        state_root=runtime.state_root,
        registry=runtime.registry,
        leases=runtime.pointers.generations.leases,
    )
    queue = runtime.pointers.generations.semantic_queue
    assert queue is not None
    original_probe = FreshnessAuthority.probe
    mutated = False

    def mutate_after_probe(
        authority: FreshnessAuthority,
        repo_uuid: str,
        **kwargs: Any,
    ) -> Any:
        nonlocal mutated
        result = original_probe(authority, repo_uuid, **kwargs)
        if not mutated:
            build = acquire(harness, "BUILD", tick=3)
            queue.enqueue(
                build,
                SemanticDesiredWork(
                    source_epoch=1,
                    policy_sha256="1" * 64,
                    operation="UPSERT",
                    path="docs/status-race.md",
                    content_sha256="2" * 64,
                    desired_revision=2,
                ),
                monotonic_ns=30_001,
            )
            harness.leases.release(build)
            mutated = True
        return result

    monkeypatch.setattr(FreshnessAuthority, "probe", mutate_after_probe)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert mutated is True
    assert report.exit_code == 10
    assert report.to_dict()["safe_to_query"] is False
    assert "status_snapshot_changed" in _reason_codes(report)


def test_status_revalidates_journal_after_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import POLICY
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    harness = RuntimeHarness(
        repo=runtime.repo,
        state_root=runtime.state_root,
        registry=runtime.registry,
        leases=runtime.pointers.generations.leases,
    )
    original_probe = FreshnessAuthority.probe
    appended = False

    def append_after_probe(
        authority: FreshnessAuthority,
        repo_uuid: str,
        **kwargs: Any,
    ) -> Any:
        nonlocal appended
        result = original_probe(authority, repo_uuid, **kwargs)
        if not appended:
            build = acquire(harness, "BUILD", tick=3)
            runtime.pointers.generations.allocate(
                build,
                expected_payload_bytes=4096,
                capacity_policy=POLICY,
                generation_id="gen-late",
                occurred_at=START + timedelta(seconds=3),
                monotonic_ns=30_001,
            )
            harness.leases.release(build)
            appended = True
        return result

    monkeypatch.setattr(FreshnessAuthority, "probe", append_after_probe)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert appended is True
    assert report.exit_code == 10
    assert report.to_dict()["safe_to_query"] is False
    assert "status_snapshot_changed" in _reason_codes(report)


def test_status_revalidates_gc_intent_after_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import POLICY
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    pointer = runtime.pointers.load(REPO_UUID, allow_missing=False)
    assert pointer is not None
    intent = _gc_intent(pointer, capacity_policy_sha256=POLICY.sha256)
    original_probe = FreshnessAuthority.probe
    injected_snapshot: Any = None

    def install_after_probe(
        authority: FreshnessAuthority,
        repo_uuid: str,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected_snapshot
        result = original_probe(authority, repo_uuid, **kwargs)
        if injected_snapshot is None:
            gc_directory = runtime.state_root / "workspaces" / REPO_UUID / "gc"
            gc_directory.mkdir(mode=0o700)
            intent_path = gc_directory / "intent.json"
            intent_path.write_bytes(intent.canonical)
            intent_path.chmod(0o600)
            injected_snapshot = tree_snapshot(runtime.state_root)
        return result

    monkeypatch.setattr(FreshnessAuthority, "probe", install_after_probe)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()

    assert injected_snapshot is not None
    assert report.exit_code == 20
    assert value["safe_to_query"] is False
    assert value["workspaces"][0]["repair"]["required"] is True
    assert any(
        check == {
            "component": f"workspace:{REPO_UUID}:gc",
            "state": "invalid",
            "reason_code": "workspace_state_invalid",
            "action_code": "run_workspace_gc_reconcile",
        }
        for check in value["checks"]
    )
    assert tree_snapshot(runtime.state_root) == injected_snapshot


def test_status_revalidates_registry_after_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    late_repo = create_repo(tmp_path / "repo-late", SECOND_UUID)
    _set_remote(late_repo, "https://github.com/example/status-late.git")
    original_probe = FreshnessAuthority.probe
    mutated = False

    def enroll_after_probe(
        authority: FreshnessAuthority,
        repo_uuid: str,
        **kwargs: Any,
    ) -> Any:
        nonlocal mutated
        result = original_probe(authority, repo_uuid, **kwargs)
        if not mutated:
            runtime.registry.enroll(
                discover_source(late_repo),
                authorization("status-late"),
                expected_revision=1,
            )
            mutated = True
        return result

    monkeypatch.setattr(FreshnessAuthority, "probe", enroll_after_probe)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()

    assert mutated is True
    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert value["workspaces"][0]["safe_to_query"] is False
    assert [workspace["repo_uuid"] for workspace in value["workspaces"]] == [REPO_UUID]
    assert "status_snapshot_changed" in _reason_codes(report)


def test_status_revalidates_complete_registry_bytes_after_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path / "primary")
    alternate = create_harness(tmp_path / "alternate").registry.load()
    original_snapshot = RegistryStore.read_only_snapshot
    snapshot_count = 0

    @contextmanager
    def replace_final_snapshot(
        store: RegistryStore,
        *,
        deadline_ns: int | None = None,
    ) -> Iterator[Any]:
        nonlocal snapshot_count
        if store.state.root == runtime.state_root:
            snapshot_count += 1
            if snapshot_count == 3:
                yield alternate
                return
        with original_snapshot(store, deadline_ns=deadline_ns) as document:
            yield document

    initial = runtime.registry.load()
    assert initial.to_dict()["revision"] == alternate.to_dict()["revision"]
    assert initial.sha256 != alternate.sha256
    monkeypatch.setattr(RegistryStore, "read_only_snapshot", replace_final_snapshot)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert snapshot_count == 3
    assert report.exit_code == 10
    assert report.to_dict()["safe_to_query"] is False
    assert "status_snapshot_changed" in _reason_codes(report)


def test_status_revalidates_all_workspace_authority_under_one_final_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    original_workspace_lock = LeaseStore.read_only_workspace_lock
    original_gc_intent = GcStore.read_only_intent_locked
    original_queue_snapshot = SemanticQueueStore.read_only_snapshot_locked
    original_journal_snapshot = JournalStore.read_stable
    original_pointer_snapshot = PointerStore.read_current
    lock_epoch = 0
    active_epoch: int | None = None
    observed: dict[str, list[int | None]] = {
        "gc": [],
        "queue": [],
        "journal": [],
        "pointer": [],
    }

    @contextmanager
    def tracked_workspace_lock(
        store: LeaseStore,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Iterator[None]:
        nonlocal active_epoch, lock_epoch
        with original_workspace_lock(store, repo_uuid, deadline_ns=deadline_ns):
            lock_epoch += 1
            active_epoch = lock_epoch
            try:
                yield
            finally:
                active_epoch = None

    def tracked_gc_intent(
        store: GcStore,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Any:
        observed["gc"].append(active_epoch)
        return original_gc_intent(store, repo_uuid, deadline_ns=deadline_ns)

    def tracked_queue_snapshot(
        store: SemanticQueueStore,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Any:
        observed["queue"].append(active_epoch)
        return original_queue_snapshot(store, repo_uuid, deadline_ns=deadline_ns)

    def tracked_journal_snapshot(
        store: JournalStore,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Any:
        observed["journal"].append(active_epoch)
        return original_journal_snapshot(store, repo_uuid, deadline_ns=deadline_ns)

    @contextmanager
    def tracked_pointer_snapshot(
        store: PointerStore,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Iterator[Any]:
        observed["pointer"].append(active_epoch)
        with original_pointer_snapshot(store, repo_uuid, deadline_ns=deadline_ns) as reading:
            yield reading

    monkeypatch.setattr(LeaseStore, "read_only_workspace_lock", tracked_workspace_lock)
    monkeypatch.setattr(GcStore, "read_only_intent_locked", tracked_gc_intent)
    monkeypatch.setattr(
        SemanticQueueStore,
        "read_only_snapshot_locked",
        tracked_queue_snapshot,
    )
    monkeypatch.setattr(JournalStore, "read_stable", tracked_journal_snapshot)
    monkeypatch.setattr(PointerStore, "read_current", tracked_pointer_snapshot)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 0
    final_epochs = {name: epochs[-1] for name, epochs in observed.items()}
    assert None not in final_epochs.values()
    assert len(set(final_epochs.values())) == 1


def test_status_propagates_deadline_into_stable_record_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    original_read_stable_record = DurableStateRoot.read_stable_record
    observed: list[tuple[str, int | None]] = []

    def capture_deadline(
        state: DurableStateRoot,
        *,
        label: str,
        current: str | Path,
        previous: str | Path,
        pending: str | Path,
        decoder: Any,
        revision: Any,
        allow_missing: bool = False,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> Any:
        observed.append((label, deadline_ns))
        kwargs = {
            "label": label,
            "current": current,
            "previous": previous,
            "pending": pending,
            "decoder": decoder,
            "revision": revision,
            "allow_missing": allow_missing,
        }
        if deadline_ns is not None:
            kwargs["deadline_ns"] = deadline_ns
        if max_bytes is not None:
            kwargs["max_bytes"] = max_bytes
        return original_read_stable_record(state, **kwargs)

    monkeypatch.setattr(DurableStateRoot, "read_stable_record", capture_deadline)
    absolute_deadline = time.monotonic_ns() + 5_000_000_000

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        ),
        deadline_ns=absolute_deadline,
    )

    assert report.exit_code == 0
    for label in ("registry", "workspace", "semantic_queue", "journal_head"):
        deadlines = [deadline for observed_label, deadline in observed if observed_label == label]
        assert deadlines
        assert None not in deadlines
        assert absolute_deadline in deadlines


def test_status_bounds_visible_pointer_read_by_size_and_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.pointers import _MAX_POINTER_RECORD_BYTES
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    original_read_existing_bytes = DurableStateRoot.read_existing_bytes
    observed: list[tuple[int | None, int | None]] = []
    before = tree_snapshot(runtime.state_root)

    def capture_pointer_read(
        state: DurableStateRoot,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        if Path(relative).name == "pointers.json":
            observed.append((max_bytes, deadline_ns))
            raise LockTimeout("injected pointer record deadline")
        return original_read_existing_bytes(
            state,
            relative,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(DurableStateRoot, "read_existing_bytes", capture_pointer_read)
    absolute_deadline = time.monotonic_ns() + 5_000_000_000

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        ),
        deadline_ns=absolute_deadline,
    )

    assert report.exit_code == 10
    assert observed == [(_MAX_POINTER_RECORD_BYTES, absolute_deadline)]
    assert "inspection_deadline_exceeded" in _reason_codes(report)
    assert "pointer_invalid" not in _reason_codes(report)
    assert tree_snapshot(runtime.state_root) == before


def test_status_bounds_generation_verification_reads_and_propagates_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.generations import (
        GenerationStore,
        _MAX_GENERATION_COORDINATION_LOCK_BYTES,
    )
    from graphify.workspace.semantic_queue import (
        _MAX_SEMANTIC_CERTIFICATION_BINDING_BYTES,
    )
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    absolute_deadline = time.monotonic_ns() + 5_000_000_000
    original_inventory = GenerationStore._inventory
    original_read_existing_bytes = DurableStateRoot.read_existing_bytes
    original_read_optional_existing_bytes = DurableStateRoot.read_optional_existing_bytes
    inventory_deadlines: list[int | None] = []
    record_reads: dict[str, list[tuple[int | None, int | None]]] = {
        "receipt": [],
        "coordination_lock": [],
        "semantic_binding": [],
    }
    before = tree_snapshot(runtime.state_root)

    def capture_inventory(
        store: GenerationStore,
        container: Path,
        *,
        allowed_root_entries: frozenset[str],
        deadline_ns: int | None = None,
    ) -> Any:
        if allowed_root_entries == frozenset({"graphify-out", "receipt.json"}):
            inventory_deadlines.append(deadline_ns)
        return original_inventory(
            store,
            container,
            allowed_root_entries=allowed_root_entries,
            deadline_ns=deadline_ns,
        )

    def capture_existing_read(
        state: DurableStateRoot,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        path = Path(relative)
        if path.name == "receipt.json" and "generations" in path.parts:
            record_reads["receipt"].append((max_bytes, deadline_ns))
        elif path.suffix == ".lock" and "generations" in path.parts:
            record_reads["coordination_lock"].append((max_bytes, deadline_ns))
        return original_read_existing_bytes(
            state,
            relative,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    def capture_optional_read(
        state: DurableStateRoot,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes | None:
        if "certifications" in Path(relative).parts:
            record_reads["semantic_binding"].append((max_bytes, deadline_ns))
        return original_read_optional_existing_bytes(
            state,
            relative,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(GenerationStore, "_inventory", capture_inventory)
    monkeypatch.setattr(DurableStateRoot, "read_existing_bytes", capture_existing_read)
    monkeypatch.setattr(
        DurableStateRoot,
        "read_optional_existing_bytes",
        capture_optional_read,
    )

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        ),
        deadline_ns=absolute_deadline,
    )

    assert report.exit_code == 0
    assert inventory_deadlines
    assert None not in inventory_deadlines
    assert absolute_deadline in inventory_deadlines
    assert record_reads["receipt"]
    assert all(
        max_bytes is None and deadline_ns is not None
        for max_bytes, deadline_ns in record_reads["receipt"]
    )
    assert absolute_deadline in {
        deadline_ns for _max_bytes, deadline_ns in record_reads["receipt"]
    }
    assert record_reads["coordination_lock"]
    assert all(
        max_bytes == _MAX_GENERATION_COORDINATION_LOCK_BYTES
        and deadline_ns is not None
        for max_bytes, deadline_ns in record_reads["coordination_lock"]
    )
    assert absolute_deadline in {
        deadline_ns for _max_bytes, deadline_ns in record_reads["coordination_lock"]
    }
    assert record_reads["semantic_binding"]
    assert all(
        max_bytes == _MAX_SEMANTIC_CERTIFICATION_BINDING_BYTES
        and deadline_ns is not None
        for max_bytes, deadline_ns in record_reads["semantic_binding"]
    )
    assert absolute_deadline in {
        deadline_ns for _max_bytes, deadline_ns in record_reads["semantic_binding"]
    }
    assert tree_snapshot(runtime.state_root) == before


@pytest.mark.parametrize("record", ["coordination_lock", "semantic_binding"])
def test_status_rejects_oversized_generation_verification_records_without_writing(
    tmp_path: Path,
    record: str,
) -> None:
    from graphify.workspace.generations import _MAX_GENERATION_COORDINATION_LOCK_BYTES
    from graphify.workspace.semantic_queue import (
        _MAX_SEMANTIC_CERTIFICATION_BINDING_BYTES,
    )
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    workspace = runtime.state_root / "workspaces" / REPO_UUID
    if record == "coordination_lock":
        target = workspace / "locks/generations/gen-current.lock"
        target.write_bytes(b"x" * (_MAX_GENERATION_COORDINATION_LOCK_BYTES + 1))
    else:
        target = workspace / "queue/certifications/gen-current.json"
        target.write_bytes(b"x" * (_MAX_SEMANTIC_CERTIFICATION_BINDING_BYTES + 1))
    before = tree_snapshot(runtime.state_root)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 20
    assert "generation_or_pointer_invalid" in _reason_codes(report)
    assert tree_snapshot(runtime.state_root) == before


def test_status_revalidates_pointer_after_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime
    from tests.test_workspace_pointers import _cas, _certify

    runtime = certified_runtime(tmp_path)
    harness = RuntimeHarness(
        repo=runtime.repo,
        state_root=runtime.state_root,
        registry=runtime.registry,
        leases=runtime.pointers.generations.leases,
    )
    build = acquire(harness, "BUILD", tick=3)
    next_receipt = _certify(
        runtime.pointers.generations,
        build,
        "gen-next",
        '{"nodes": [], "edges": []}\n',
        monotonic_ns=30_001,
    )
    harness.leases.release(build)
    original_pointer = runtime.pointers.load(REPO_UUID, allow_missing=False)
    assert original_pointer is not None
    original_current = cast(dict[str, Any], original_pointer.to_dict()["current"])
    original_probe = FreshnessAuthority.probe
    promoted = False

    def promote_after_probe(
        authority: FreshnessAuthority,
        repo_uuid: str,
        **kwargs: Any,
    ) -> Any:
        nonlocal promoted
        result = original_probe(authority, repo_uuid, **kwargs)
        if not promoted:
            promote = acquire(harness, "PROMOTE", tick=4)
            runtime.pointers.promote(
                promote,
                _cas(
                    promote,
                    next_receipt,
                    revision=1,
                    current_sha256=str(original_current["receipt_sha256"]),
                ),
                occurred_at=START + timedelta(seconds=4),
                monotonic_ns=40_001,
            )
            harness.leases.release(promote)
            promoted = True
        return result

    monkeypatch.setattr(FreshnessAuthority, "probe", promote_after_probe)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()

    assert promoted is True
    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert value["workspaces"][0]["safe_to_query"] is False
    assert value["workspaces"][0]["generations"]["pointer_revision"] == 1
    assert "status_snapshot_changed" in _reason_codes(report)


def test_status_bounds_pointer_journal_verification_by_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    original_read_stable = JournalStore.read_stable
    observed_deadlines: list[int | None] = []

    def expire_on_pointer_verification(
        store: JournalStore,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Any:
        observed_deadlines.append(deadline_ns)
        if len(observed_deadlines) == 2 and deadline_ns is not None:
            raise LockTimeout("pointer journal verification exceeded its deadline")
        return original_read_stable(store, repo_uuid, deadline_ns=deadline_ns)

    monkeypatch.setattr(JournalStore, "read_stable", expire_on_pointer_verification)
    absolute_deadline = time.monotonic_ns() + 5_000_000_000

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        ),
        deadline_ns=absolute_deadline,
    )

    assert observed_deadlines[:2] == [absolute_deadline, absolute_deadline]
    assert report.exit_code == 10
    assert report.to_dict()["safe_to_query"] is False
    assert "inspection_deadline_exceeded" in _reason_codes(report)


def test_journal_summary_honors_deadline_during_event_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.status import _journal_summary
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    snapshot = runtime.pointers.journal.read_stable(REPO_UUID)
    monotonic_tick = 0

    def monotonic_ns() -> int:
        nonlocal monotonic_tick
        monotonic_tick += 1
        return monotonic_tick

    monkeypatch.setattr("graphify.workspace.persistence.time.monotonic_ns", monotonic_ns)

    with pytest.raises(LockTimeout, match="journal summary exceeded its deadline"):
        _journal_summary(snapshot, deadline_ns=2)


def test_status_classifies_journal_summary_deadline_at_journal_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)

    def expired_summary(*_args: object, **_kwargs: object) -> object:
        raise LockTimeout("journal summary exceeded its deadline")

    monkeypatch.setattr("graphify.workspace.status._journal_summary", expired_summary)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()
    journal_check = next(
        check
        for check in value["checks"]
        if check["component"] == f"workspace:{REPO_UUID}:journal" and check["state"] == "degraded"
    )

    assert report.exit_code == 10
    assert journal_check["reason_code"] == "inspection_deadline_exceeded"
    assert journal_check["action_code"] == "retry_status"


def test_status_stops_registry_traversal_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.workspace.status as status_module

    harness = create_harness(tmp_path)
    second_repo = create_repo(tmp_path / "repo-second", SECOND_UUID)
    _set_remote(second_repo, "https://github.com/example/status-second.git")
    harness.registry.enroll(
        discover_source(second_repo),
        authorization("status-second"),
        expected_revision=1,
    )
    expired = False
    inspected: list[str] = []

    def inspect_once(
        _runtime: object,
        _registry: object,
        entry: dict[str, object],
        **_kwargs: object,
    ) -> tuple[dict[str, object], list[dict[str, str]]]:
        nonlocal expired
        inspected.append(str(entry["repo_uuid"]))
        workspace = status_module._workspace_shell(entry)
        workspace_checks: list[dict[str, str]] = []
        status_module._deadline_failure(
            workspace,
            workspace_checks,
            component=f"workspace:{entry['repo_uuid']}:inspection",
        )
        expired = True
        return workspace, workspace_checks

    monkeypatch.setattr(status_module, "_inspect_workspace", inspect_once)
    monkeypatch.setattr(
        "graphify.workspace.persistence.time.monotonic_ns",
        lambda: 1 if expired else 0,
    )

    report = inspect_workspace_status(_inputs(harness.state_root), deadline_ns=1)

    assert inspected == [REPO_UUID]
    assert report.exit_code == 10
    assert report.to_dict()["safe_to_query"] is False
    assert "inspection_deadline_exceeded" in _reason_codes(report)


@pytest.mark.parametrize(
    ("queue_state", "expected_reason"),
    [
        ("pending", "semantic_queue_pending"),
        ("dead_letter", "semantic_queue_dead_letter"),
    ],
)
def test_status_reports_valid_unhealthy_semantic_queue_as_degraded(
    tmp_path: Path,
    queue_state: str,
    expected_reason: str,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime
    from tests.test_workspace_semantic_queue import _host_claim_inputs

    runtime = certified_runtime(tmp_path)
    harness = RuntimeHarness(
        repo=runtime.repo,
        state_root=runtime.state_root,
        registry=runtime.registry,
        leases=runtime.pointers.generations.leases,
    )
    queue = runtime.pointers.generations.semantic_queue
    assert queue is not None
    build = acquire(harness, "BUILD", tick=3)
    queue.enqueue(
        build,
        SemanticDesiredWork(
            source_epoch=1,
            policy_sha256="1" * 64,
            operation="UPSERT",
            path="docs/status.md",
            content_sha256="2" * 64,
            desired_revision=2,
        ),
        monotonic_ns=30_001,
    )
    harness.leases.release(build)
    if queue_state == "dead_letter":
        semantic = acquire(harness, "SEMANTIC_CLAIM", tick=4)
        claim = queue.claim(
            semantic,
            **_host_claim_inputs(harness),
            monotonic_ns=40_001,
        )
        assert claim is not None
        queue.fail(
            semantic,
            claim,
            error_code="status_test",
            retryable=False,
            monotonic_ns=40_002,
        )
        harness.leases.release(semantic)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )
    value = report.to_dict()

    assert report.exit_code == 10
    assert value["safe_to_query"] is False
    assert expected_reason in _reason_codes(report)
    assert value["workspaces"][0]["freshness"]["state"] == "observed_current"


def test_status_inspection_does_not_write_content_or_metadata(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    before_xattrs = _xattr_snapshot(harness.state_root)

    inspect_workspace_status(_inputs(harness.state_root))

    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata
    assert _xattr_snapshot(harness.state_root) == before_xattrs


@pytest.mark.parametrize(
    ("lock_name", "reason_code"),
    [
        ("registry", "registry_lock_contended"),
        ("workspace", "workspace_lock_contended"),
    ],
)
def test_status_bounds_existing_lock_contention_without_writes(
    tmp_path: Path,
    lock_name: str,
    reason_code: str,
) -> None:
    harness = create_harness(tmp_path)
    lock = (
        harness.state_root / RegistryStore.LOCK
        if lock_name == "registry"
        else harness.state_root / "workspaces" / REPO_UUID / "workspace.lock"
    )
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    before_xattrs = _xattr_snapshot(harness.state_root)
    holder = _hold_exclusive_lock(lock)
    try:
        started = time.monotonic()
        report = inspect_workspace_status(
            _inputs(harness.state_root),
            deadline_ns=time.monotonic_ns() + 250_000_000,
        )
        elapsed = time.monotonic() - started
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert elapsed < 1.0
    assert report.exit_code == 10
    assert reason_code in _reason_codes(report)
    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata
    assert _xattr_snapshot(harness.state_root) == before_xattrs


@pytest.mark.parametrize("lock_name", ["registry", "workspace"])
def test_read_only_lock_contention_preserves_structured_kind(
    tmp_path: Path,
    lock_name: str,
) -> None:
    harness = create_harness(tmp_path)
    lock = (
        harness.state_root / RegistryStore.LOCK
        if lock_name == "registry"
        else harness.state_root / "workspaces" / REPO_UUID / "workspace.lock"
    )
    holder = _hold_exclusive_lock(lock)
    try:
        with pytest.raises(LockTimeout) as captured:
            context = (
                harness.registry.read_only_snapshot(deadline_ns=time.monotonic_ns() + 10_000_000)
                if lock_name == "registry"
                else harness.leases.read_only_workspace_lock(
                    REPO_UUID,
                    deadline_ns=time.monotonic_ns() + 10_000_000,
                )
            )
            with context:
                pass
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert captured.value.phase == "acquire"
    assert captured.value.kind == lock_name


def test_status_classifies_generation_lock_contention_at_its_boundary(
    tmp_path: Path,
) -> None:
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)
    lock = (
        runtime.state_root / "workspaces" / REPO_UUID / "locks" / "generations" / "gen-current.lock"
    )
    holder = _hold_exclusive_lock(lock)
    try:
        report = inspect_workspace_status(
            WorkspaceRuntimeInputs(
                state_root=runtime.state_root,
                compatibility_manifest=COMPATIBILITY_MANIFEST,
                semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
                capabilities=SUPPORTED,
            ),
            # Leave enough budget to traverse the earlier read-only stores on a
            # loaded runner before the held generation lock consumes the deadline.
            deadline_ns=time.monotonic_ns() + 1_000_000_000,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert report.exit_code == 10
    assert "generation_lock_contended" in _reason_codes(report)
    assert "workspace_lock_contended" not in _reason_codes(report)


def test_status_classifies_generation_lock_timeout_from_structured_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.persistence import LockTimeout
    from graphify.workspace.pointers import PointerStore
    from tests.test_workspace_freshness import QUEUE_POLICY as CERTIFIED_QUEUE_POLICY
    from tests.test_workspace_freshness import _runtime as certified_runtime

    runtime = certified_runtime(tmp_path)

    def acquisition_timeout(*_args: object, **_kwargs: object) -> object:
        raise LockTimeout(
            "message intentionally omits the old contention marker",
            phase="acquire",
            kind="generation",
        )

    monkeypatch.setattr(PointerStore, "read_current", acquisition_timeout)

    report = inspect_workspace_status(
        WorkspaceRuntimeInputs(
            state_root=runtime.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=CERTIFIED_QUEUE_POLICY,
            capabilities=SUPPORTED,
        )
    )

    assert report.exit_code == 10
    assert "generation_lock_contended" in _reason_codes(report)
    assert "inspection_deadline_exceeded" not in _reason_codes(report)


def test_status_classifies_post_lock_deadline_without_claiming_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)

    def deadline_exceeded(*_args: object, **_kwargs: object) -> object:
        from graphify.workspace.persistence import LockTimeout

        raise LockTimeout("injected post-lock deadline")

    monkeypatch.setattr(
        SemanticQueueStore,
        "read_only_snapshot_locked",
        deadline_exceeded,
    )

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 10
    assert "inspection_deadline_exceeded" in _reason_codes(report)
    assert "workspace_lock_contended" not in _reason_codes(report)


def test_status_classifies_unsafe_lease_snapshot_at_its_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    before_xattrs = _xattr_snapshot(harness.state_root)

    def unsafe_lease_snapshot(*_args: object, **_kwargs: object) -> object:
        raise StatePathError("injected unsafe lease record path")

    monkeypatch.setattr(
        type(harness.leases),
        "read_only_snapshot_locked",
        unsafe_lease_snapshot,
    )

    report = inspect_workspace_status(_inputs(harness.state_root))
    value = report.to_dict()
    lease_check = next(
        check for check in value["checks"] if check["component"] == f"workspace:{REPO_UUID}:leases"
    )

    assert report.exit_code == 20
    assert lease_check["reason_code"] == "workspace_state_invalid"
    assert lease_check["action_code"] == "run_workspace_repair"
    assert "workspace_lock_invalid" not in _reason_codes(report)
    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata
    assert _xattr_snapshot(harness.state_root) == before_xattrs


def test_status_uses_no_recovery_or_mutating_persistence_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)

    def unexpected_mutation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read-only status called a mutating persistence primitive")

    for name in ("lock", "recover_record", "commit_record", "write_once"):
        monkeypatch.setattr(DurableStateRoot, name, unexpected_mutation)

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 10
    assert "no_current_generation" in _reason_codes(report)


def test_status_emits_no_transient_filesystem_write_events(tmp_path: Path) -> None:
    watchdog = pytest.importorskip("watchdog.observers")
    events_module = pytest.importorskip("watchdog.events")
    harness = create_harness(tmp_path)
    observed: list[tuple[str, str]] = []

    class Handler(events_module.FileSystemEventHandler):  # type: ignore[misc]
        def on_any_event(self, event: Any) -> None:
            if event.event_type not in {"opened", "closed", "closed_no_write"}:
                observed.append((event.event_type, event.src_path))

    observer = watchdog.Observer()
    observer.schedule(Handler(), str(harness.state_root), recursive=True)
    observer.start()
    try:
        time.sleep(0.1)
        observed.clear()
        inspect_workspace_status(_inputs(harness.state_root))
        time.sleep(0.1)
    finally:
        observer.stop()
        observer.join(timeout=5)

    assert observed == []
