from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.composition import (
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
)
from graphify.workspace.contracts import (
    CompatibilityManifest,
    FreshnessRelease,
    PointerSet,
    canonical_json_bytes,
)
from graphify.workspace.freshness import FreshnessAuthority
from graphify.workspace.identity import discover_source
from graphify.workspace.persistence import (
    DurableStateRoot,
    RuntimeCapabilities,
    StatePathError,
)
from graphify.workspace.registry import RegistryStore
from graphify.workspace.semantic_queue import (
    SemanticDesiredWork,
    SemanticQueuePolicy,
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
    assert value["schema_version"] == 1
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


def test_status_reports_pointer_pending_recovery_as_invalid(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    pointer = PointerSet.from_json((FIXTURES / "positive" / "pointer-set.json").read_bytes())
    pending = harness.state_root / "workspaces" / REPO_UUID / "pointers.pending.json"
    pending.write_bytes(pointer.canonical)
    pending.chmod(0o600)

    report = inspect_workspace_status(_inputs(harness.state_root))

    assert report.exit_code == 20
    assert "pointer_recovery_required" in _reason_codes(report)


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
            deadline_ns=time.monotonic_ns() + 10_000_000,
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
