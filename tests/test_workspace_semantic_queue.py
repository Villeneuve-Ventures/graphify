from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import errno
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
from typing import Any, Iterator, TypedDict, cast

import pytest

import graphify.workspace.semantic_queue as semantic_queue_module
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    CapacityPolicy,
    ContractError,
    WorkspaceConfig,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.generations import (
    CertificationRequest,
    GenerationStore,
)
from graphify.workspace.identity import (
    IdentityAction,
    OperatorAuthorization,
    SourceIdentity,
    discover_source,
)
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseGrant
from graphify.workspace.persistence import (
    CommitUnknown,
    FaultHook,
    InjectedFault,
    PosixSyscalls,
    Syscalls,
)
from graphify.workspace.semantic_queue import (
    SemanticCapabilityUnavailable,
    SemanticCapabilityDecision,
    SemanticCertificationBlocked,
    SemanticDesiredWork,
    SemanticQueueCapacityExceeded,
    SemanticQueueConflict,
    SemanticQueueCorrupt,
    SemanticQueuePolicy,
    SemanticQueueSnapshot,
    SemanticQueueStore,
    StaleSemanticClaim,
    decide_semantic_capability,
)
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    REPO_UUID,
    START,
    RuntimeHarness,
    acquire,
    authorization,
    create_harness,
    create_repo,
    trust_source_observations,
    tree_snapshot,
)


QUEUE_POLICY = SemanticQueuePolicy(
    max_items=16,
    max_bytes=64 * 1024,
    retry_budget=1,
)
GENERATION_POLICY = CapacityPolicy.from_mapping(
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


def _work(
    path: str,
    revision: int,
    *,
    operation: str = "UPSERT",
    digest: str | None = None,
    source_epoch: int = 1,
    policy_sha256: str = "1" * 64,
) -> SemanticDesiredWork:
    return SemanticDesiredWork(
        source_epoch=source_epoch,
        policy_sha256=policy_sha256,
        operation=operation,
        path=path,
        content_sha256=digest or f"{revision:x}".rjust(64, "0"),
        desired_revision=revision,
    )


def _queue(
    harness: RuntimeHarness,
    *,
    fault_hook: FaultHook | None = None,
    syscalls: Syscalls | None = None,
) -> SemanticQueueStore:
    return SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=QUEUE_POLICY,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
        syscalls=syscalls,
    )


class _ClaimInputs(TypedDict):
    config: WorkspaceConfig
    host_agent_active: bool
    explicit_backend: str | None


def _host_claim_inputs(harness: RuntimeHarness) -> _ClaimInputs:
    config = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    return {
        "config": config,
        "host_agent_active": True,
        "explicit_backend": None,
    }


def _source_commit(harness: RuntimeHarness) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=harness.repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source_observations(
    harness: RuntimeHarness,
    *,
    inventory_sha256: str = "2" * 64,
    policy_sha256: str = "1" * 64,
    source_commit: str | None = None,
) -> tuple[SourceObservation, SourceObservation]:
    observation = SourceObservation(
        source_commit=source_commit or _source_commit(harness),
        inventory_sha256=inventory_sha256,
        policy_sha256=policy_sha256,
        detector_id="test-semantic-queue",
        stable_inventory_passes=2,
        entries=(),
    )
    return (observation, observation)


def _rebind_linked_policy_source(
    harness: RuntimeHarness,
    tmp_path: Path,
) -> tuple[SourceIdentity, int, int]:
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "-b", "linked-policy", str(linked)],
        cwd=harness.repo,
        check=True,
    )
    linked_config_path = linked / ".graphify/workspace.toml"
    linked_config_path.write_text(
        'contract = "graphify.workspace.config"\n'
        "schema_version = 1\n"
        f'repo_uuid = "{REPO_UUID}"\n'
        "\n"
        "[policy]\n"
        'freshness = "current_only"\n'
        'semantic_mode = "explicit_backend"\n'
        "network_egress = true\n"
        'headless_backends = ["gemini"]\n',
        encoding="utf-8",
    )
    linked_source = discover_source(linked)
    registry = harness.registry.rebind(
        linked_source,
        OperatorAuthorization(
            action=IdentityAction.REBIND,
            operator_id="operator:p5a-test",
            reason="bind activation race fixture",
            issued_at="2026-07-16T19:00:00Z",
            nonce="bind-activation-race",
        ),
        expected_revision=1,
    )
    entry = registry.to_dict()["workspaces"][0]
    return (
        linked_source,
        int(registry.to_dict()["revision"]),
        int(entry["active_source_revision"]),
    )


def _activate_linked_policy_source(
    harness: RuntimeHarness,
    linked_source: SourceIdentity,
    *,
    registry_revision: int,
    active_source_revision: int,
    nonce: str,
    monotonic_ns: int,
) -> None:
    state = harness.leases.inspect(REPO_UUID)
    harness.registry.activate_source(
        linked_source,
        OperatorAuthorization(
            action=IdentityAction.ACTIVATE,
            operator_id="operator:p5a-test",
            reason="activate between semantic mutation locks",
            issued_at="2026-07-16T19:00:01Z",
            nonce=nonce,
        ),
        leases=harness.leases,
        owner=harness.leases.current_owner(),
        expected_registry_revision=registry_revision,
        expected_active_source_revision=active_source_revision,
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        acquired_at=START + timedelta(seconds=3),
        monotonic_ns=monotonic_ns,
        ttl_ns=1_000_000,
    )


def test_enqueue_coalesces_by_path_and_rejects_nonmonotonic_desired_work(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)

    first = queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    second = queue.enqueue(
        build,
        _work("docs/a.md", 2, digest="a" * 64),
        monotonic_ns=10_002,
    )
    duplicate = queue.enqueue(
        build,
        _work("docs/a.md", 2, digest="a" * 64),
        monotonic_ns=10_003,
    )

    assert first.desired_watermark == 1
    assert second.desired_watermark == 2
    assert duplicate.revision == second.revision
    assert len(second.items) == 1
    assert second.items[0].desired_revision == 2
    assert second.items[0].content_sha256 == "a" * 64
    with pytest.raises(SemanticQueueConflict, match="desired revision"):
        queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_004)


def test_read_only_inspect_of_missing_queue_is_empty_and_performs_no_writes(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    queue = _queue(harness)
    before = tree_snapshot(harness.state_root)

    snapshot = queue.inspect(REPO_UUID)

    assert snapshot.revision == 0
    assert snapshot.items == ()
    assert SemanticQueueSnapshot.from_json(snapshot.canonical) == snapshot
    assert tree_snapshot(harness.state_root) == before


def test_capacity_is_checked_after_coalescing_and_before_durable_mutation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=SemanticQueuePolicy(max_items=1, max_bytes=4096, retry_budget=1),
        capabilities=harness.leases.state.capabilities,
    )
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    queue.enqueue(build, _work("docs/a.md", 2), monotonic_ns=10_002)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticQueueCapacityExceeded, match="item capacity"):
        queue.enqueue(build, _work("docs/b.md", 3), monotonic_ns=10_003)

    assert tree_snapshot(harness.state_root) == before
    assert queue.inspect(REPO_UUID).items[0].desired_revision == 2


def test_byte_capacity_is_deterministic_and_fails_without_partial_state(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=SemanticQueuePolicy(max_items=16, max_bytes=1, retry_budget=1),
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticQueueCapacityExceeded, match="byte capacity"):
        queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)

    assert tree_snapshot(harness.state_root) == before


def test_semantic_required_reconciliation_rejects_an_empty_desired_set(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(ContractError, match="semantic-required"):
        queue.reconcile(
            build,
            (),
            source_epoch=1,
            policy_sha256="1" * 64,
            source_observations=_source_observations(harness),
            desired_watermark=1,
            semantic_required=True,
            monotonic_ns=10_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_claim_selection_rotates_operations_before_retrying_one_class(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.reconcile(
        build,
        (
            _work("docs/remove.md", 1, operation="DELETE"),
            _work("docs/upsert.md", 2, operation="UPSERT"),
        ),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=2,
        semantic_required=True,
        monotonic_ns=10_001,
    )
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    capability = _host_claim_inputs(harness)

    first = queue.claim(semantic, **capability, monotonic_ns=20_001)
    assert first is not None and first.work.operation == "DELETE"
    queue.fail(
        semantic,
        first,
        error_code="temporary",
        retryable=True,
        monotonic_ns=20_002,
    )
    second = queue.claim(semantic, **capability, monotonic_ns=20_003)
    assert second is not None and second.work.operation == "UPSERT"
    queue.complete(semantic, second, monotonic_ns=20_004)
    third = queue.claim(semantic, **capability, monotonic_ns=20_005)
    assert third is not None and third.work.operation == "DELETE"


def test_claim_ids_are_namespaced_by_workspace(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path / "primary")
    other_uuid = "22222222-2222-4222-8222-222222222222"
    other_repo = create_repo(tmp_path / "other-repo", other_uuid)
    harness.registry.enroll(
        discover_source(other_repo),
        authorization("enroll-other-workspace"),
        expected_revision=1,
    )
    queue = _queue(harness)

    def acquire_for(repo_uuid: str, operation: str, *, tick: int) -> LeaseGrant:
        registry = harness.registry.load()
        entry = next(
            workspace
            for workspace in registry.to_dict()["workspaces"]
            if workspace["repo_uuid"] == repo_uuid
        )
        state = harness.leases.inspect(repo_uuid)
        return harness.leases.acquire(
            repo_uuid,
            operation,
            harness.leases.current_owner(),
            expected_registry_revision=int(registry.to_dict()["revision"]),
            expected_active_source_revision=int(entry["active_source_revision"]),
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START + timedelta(seconds=tick),
            monotonic_ns=tick * 10_000,
            ttl_ns=1_000_000,
        )

    primary_build = acquire_for(REPO_UUID, "BUILD", tick=1)
    other_build = acquire_for(other_uuid, "BUILD", tick=1)
    work = _work("docs/a.md", 1)
    queue.enqueue(primary_build, work, monotonic_ns=10_001)
    queue.enqueue(other_build, work, monotonic_ns=10_001)
    primary_semantic = acquire_for(REPO_UUID, "SEMANTIC_CLAIM", tick=2)
    other_semantic = acquire_for(other_uuid, "SEMANTIC_CLAIM", tick=2)
    primary_claim = queue.claim(
        primary_semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    other_config = WorkspaceConfig.from_toml(
        (other_repo / ".graphify/workspace.toml").read_bytes()
    )
    other_claim = queue.claim(
        other_semantic,
        config=other_config,
        host_agent_active=True,
        explicit_backend=None,
        monotonic_ns=20_001,
    )

    assert primary_claim is not None
    assert other_claim is not None
    assert primary_claim.claim_id != other_claim.claim_id
    before = queue.inspect(other_uuid).canonical
    with pytest.raises(StaleSemanticClaim, match="claim is no longer current"):
        queue.checkpoint(
            other_semantic,
            primary_claim,
            checkpoint="wrong-workspace",
            monotonic_ns=20_002,
        )
    with pytest.raises(StaleSemanticClaim, match="claim is no longer current"):
        queue.complete(
            other_semantic,
            primary_claim,
            monotonic_ns=20_003,
        )
    with pytest.raises(StaleSemanticClaim, match="claim is no longer current"):
        queue.fail(
            other_semantic,
            primary_claim,
            error_code="wrong_workspace",
            retryable=True,
            monotonic_ns=20_004,
        )
    assert queue.inspect(other_uuid).canonical == before


def test_checkpoint_and_completion_require_the_exact_fenced_claim(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None

    checkpointed = queue.checkpoint(
        semantic,
        claim,
        checkpoint="semantic-cache-written",
        monotonic_ns=20_002,
    )
    assert checkpointed.checkpoint == "semantic-cache-written"
    completed = queue.complete(semantic, checkpointed, monotonic_ns=20_003)

    assert completed.items[0].status == "completed"
    with pytest.raises(StaleSemanticClaim):
        queue.complete(semantic, checkpointed, monotonic_ns=20_004)


def test_failed_claim_attempt_cannot_complete_after_reclaim_under_same_grant(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)

    failed_attempt = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert failed_attempt is not None
    queue.fail(
        semantic,
        failed_attempt,
        error_code="temporary",
        retryable=True,
        monotonic_ns=20_002,
    )
    retried_attempt = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_003,
    )
    assert retried_attempt is not None

    assert retried_attempt.claim_id != failed_attempt.claim_id
    with pytest.raises(StaleSemanticClaim):
        queue.complete(semantic, failed_attempt, monotonic_ns=20_004)
    completed = queue.complete(semantic, retried_attempt, monotonic_ns=20_005)
    assert completed.items[0].status == "completed"


def test_same_path_preserves_revision_order_across_upsert_then_delete(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    queue = _queue(harness)
    capability = _host_claim_inputs(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    queue.enqueue(
        build,
        _work("docs/a.md", 2, operation="DELETE"),
        monotonic_ns=10_002,
    )

    first = queue.claim(semantic, **capability, monotonic_ns=20_001)
    assert first is not None
    assert (first.work.operation, first.work.desired_revision) == ("UPSERT", 1)
    queue.complete(semantic, first, monotonic_ns=20_002)
    second = queue.claim(semantic, **capability, monotonic_ns=20_003)
    assert second is not None
    assert (second.work.operation, second.work.desired_revision) == ("DELETE", 2)


def test_mixed_arrivals_preserve_per_path_causality_without_starvation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    queue = _queue(harness)
    capability = _host_claim_inputs(harness)
    arrivals = (
        _work("docs/a.md", 1),
        _work("docs/b.md", 2, operation="DELETE"),
        _work("docs/a.md", 3, operation="DELETE"),
        _work("docs/c.md", 4),
        _work("docs/d.md", 5, operation="DELETE"),
        _work("docs/e.md", 6),
    )
    for offset, work in enumerate(arrivals):
        queue.enqueue(build, work, monotonic_ns=10_001 + offset)

    served: list[tuple[str, str, int]] = []
    for offset in range(len(arrivals)):
        claim = queue.claim(
            semantic,
            **capability,
            monotonic_ns=20_001 + 2 * offset,
        )
        assert claim is not None
        served.append((claim.work.path, claim.work.operation, claim.work.desired_revision))
        queue.complete(
            semantic,
            claim,
            monotonic_ns=20_002 + 2 * offset,
        )

    assert served.index(("docs/a.md", "UPSERT", 1)) < served.index(("docs/a.md", "DELETE", 3))
    assert {entry[2] for entry in served} == {work.desired_revision for work in arrivals}
    assert all(item.status == "completed" for item in queue.inspect(REPO_UUID).items)


def test_failure_or_completion_cannot_discard_newer_desired_work(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None

    queue.enqueue(
        build,
        _work("docs/a.md", 2, digest="f" * 64),
        monotonic_ns=20_002,
    )
    with pytest.raises(StaleSemanticClaim, match="newer desired work"):
        queue.fail(
            semantic,
            claim,
            error_code="poison",
            retryable=False,
            monotonic_ns=20_003,
        )
    with pytest.raises(StaleSemanticClaim, match="newer desired work"):
        queue.complete(semantic, claim, monotonic_ns=20_004)

    current = queue.inspect(REPO_UUID)
    assert len(current.items) == 1
    assert current.items[0].desired_revision == 2
    assert current.items[0].status == "pending"


def test_retry_budget_dead_letters_poison_work_and_new_revision_revives_it(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    capability = _host_claim_inputs(harness)

    first = queue.claim(semantic, **capability, monotonic_ns=20_001)
    assert first is not None
    retried = queue.fail(
        semantic,
        first,
        error_code="temporary",
        retryable=True,
        monotonic_ns=20_002,
    )
    assert retried.items[0].status == "pending"
    second = queue.claim(semantic, **capability, monotonic_ns=20_003)
    assert second is not None
    poisoned = queue.fail(
        semantic,
        second,
        error_code="still-broken",
        retryable=True,
        monotonic_ns=20_004,
    )
    assert poisoned.items[0].status == "dead_letter"
    assert poisoned.items[0].failure_count == 2

    revived = queue.enqueue(build, _work("docs/a.md", 2), monotonic_ns=20_005)
    assert revived.items[0].status == "pending"
    assert revived.items[0].failure_count == 0


def test_nonretryable_failure_dead_letters_immediately_and_blocks_completion(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    queue = _queue(harness)
    queue.reconcile(
        build,
        (_work("docs/a.md", 1),),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=10_001,
    )
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None

    dead_lettered = queue.fail(
        semantic,
        claim,
        error_code="invalid_payload",
        retryable=False,
        monotonic_ns=20_002,
    )

    assert dead_lettered.items[0].status == "dead_letter"
    assert dead_lettered.items[0].failure_count == 1
    assert dead_lettered.completed_watermark < dead_lettered.desired_watermark
    assert (
        queue.claim(
            semantic,
            **_host_claim_inputs(harness),
            monotonic_ns=20_003,
        )
        is None
    )
    with pytest.raises(StaleSemanticClaim):
        queue.complete(semantic, claim, monotonic_ns=20_004)
    compacted = queue.compact(build, monotonic_ns=20_005)
    assert compacted.items[0].status == "dead_letter"


def test_reconcile_preserves_newer_desired_work_and_revives_dead_letter(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    queue = _queue(harness)
    old = _work("docs/a.md", 1)
    queue.enqueue(build, old, monotonic_ns=10_001)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None
    queue.fail(
        semantic,
        claim,
        error_code="poison",
        retryable=False,
        monotonic_ns=20_002,
    )
    newer = _work("docs/a.md", 2, digest="f" * 64)
    queue.enqueue(build, newer, monotonic_ns=20_003)

    reconciled = queue.reconcile(
        build,
        (newer,),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=3,
        semantic_required=True,
        monotonic_ns=20_004,
    )

    assert len(reconciled.items) == 1
    assert reconciled.items[0].work == newer
    assert reconciled.items[0].status == "pending"
    assert reconciled.items[0].failure_count == 0


def test_successor_recovers_an_expired_claim_under_a_higher_fence(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    first_grant = acquire(harness, "SEMANTIC_CLAIM", tick=2, ttl_ns=5)
    first_claim = queue.claim(
        first_grant,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert first_claim is not None

    successor = acquire(harness, "SEMANTIC_CLAIM", tick=3)
    recovered = queue.claim(
        successor,
        **_host_claim_inputs(harness),
        monotonic_ns=30_001,
    )

    assert recovered is not None
    assert recovered.fence_token > first_claim.fence_token
    assert recovered.work.desired_revision == first_claim.work.desired_revision
    assert queue.inspect(REPO_UUID).items[0].failure_count == 1
    with pytest.raises(StaleSemanticClaim):
        queue.complete(first_grant, first_claim, monotonic_ns=30_002)


def test_expired_claim_recovery_persists_when_same_path_successor_is_blocked(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    policy = SemanticQueuePolicy(
        max_items=16,
        max_bytes=64 * 1024,
        retry_budget=0,
    )
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=policy,
        capabilities=harness.leases.state.capabilities,
    )
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    first_grant = acquire(harness, "SEMANTIC_CLAIM", tick=2, ttl_ns=5)
    first_claim = queue.claim(
        first_grant,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert first_claim is not None
    queue.enqueue(
        build,
        _work("docs/a.md", 2, operation="DELETE"),
        monotonic_ns=20_002,
    )

    successor = acquire(harness, "SEMANTIC_CLAIM", tick=3)
    assert (
        queue.claim(
            successor,
            **_host_claim_inputs(harness),
            monotonic_ns=30_001,
        )
        is None
    )

    persisted = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=policy,
        capabilities=harness.leases.state.capabilities,
    ).inspect(REPO_UUID)
    old, newer = sorted(persisted.items, key=lambda item: item.desired_revision)
    assert old.status == "dead_letter"
    assert old.failure_count == 1
    assert old.last_error == "claim_expired"
    assert old.claim is None
    assert newer.status == "pending"
    assert newer.failure_count == 0


def test_exact_reconciliation_not_queue_emptiness_creates_certification_view(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)

    with pytest.raises(SemanticCertificationBlocked, match="exact reconciliation"):
        queue.certification_view(
            build,
            source_epoch=1,
            source_observations=_source_observations(harness),
            sealed_input_manifest_sha256="3" * 64,
            monotonic_ns=10_001,
        )

    queue.reconcile(
        build,
        (),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=1,
        semantic_required=False,
        monotonic_ns=10_002,
    )
    with pytest.raises(SemanticCertificationBlocked, match="not bound"):
        queue.certification_view(
            build,
            source_epoch=1,
            source_observations=_source_observations(harness),
            sealed_input_manifest_sha256="3" * 64,
            monotonic_ns=10_003,
        )
    queue.bind_sealed_inputs(
        build,
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=10_004,
    )
    view = queue.certification_view(
        build,
        source_epoch=1,
        source_observations=_source_observations(harness),
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=10_005,
    )

    assert view.queue_watermark == 1
    assert view.semantic_completeness == "not_required"
    with pytest.raises(SemanticCertificationBlocked, match="exactly two"):
        queue.certification_view(
            build,
            source_epoch=1,
            source_observations=_source_observations(harness)[:1],
            sealed_input_manifest_sha256="3" * 64,
            monotonic_ns=10_006,
        )
    mismatched = (
        _source_observations(harness)[0],
        _source_observations(harness, inventory_sha256="4" * 64)[0],
    )
    with pytest.raises(SemanticCertificationBlocked, match="differ"):
        queue.certification_view(
            build,
            source_epoch=1,
            source_observations=mismatched,
            sealed_input_manifest_sha256="3" * 64,
            monotonic_ns=10_007,
        )


def test_completion_and_compaction_preserve_the_stable_certification_watermark(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.reconcile(
        build,
        (_work("docs/a.md", 1),),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=10_001,
    )
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None
    queue.complete(semantic, claim, monotonic_ns=20_002)
    queue.bind_sealed_inputs(
        build,
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=20_003,
    )

    before = queue.certification_view(
        build,
        source_epoch=1,
        source_observations=_source_observations(harness),
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=20_004,
    )
    compacted = queue.compact(build, monotonic_ns=20_005)
    after = queue.certification_view(
        build,
        source_epoch=1,
        source_observations=_source_observations(harness),
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=20_006,
    )

    assert before.semantic_completeness == "complete"
    assert compacted.items == ()
    assert after.queue_watermark == before.queue_watermark == 1
    assert after.completed_watermark == before.completed_watermark == 1
    assert after.compaction_epoch == before.compaction_epoch + 1
    duplicate = queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=20_007)
    assert duplicate.revision == compacted.revision


@pytest.mark.parametrize(
    "failpoint",
    [
        "semantic_queue:pending_durable",
        "semantic_queue:previous_durable",
        "semantic_queue:current_replaced",
        "semantic_queue:current_durable",
        "semantic_queue:pending_cleared",
    ],
)
def test_compaction_recovers_after_process_death_at_each_commit_boundary(
    tmp_path: Path,
    failpoint: str,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.reconcile(
        build,
        (),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=1,
        semantic_required=False,
        monotonic_ns=10_001,
    )

    class CrashOnce:
        fired = False

        def __call__(self, event: str) -> None:
            if event == failpoint and not self.fired:
                self.fired = True
                raise InjectedFault(event)

    crashing = _queue(harness, fault_hook=CrashOnce())
    with pytest.raises(CommitUnknown, match="semantic_queue recovery intent"):
        crashing.compact(build, monotonic_ns=10_002)

    recovered = _queue(harness).compact(build, monotonic_ns=10_003)
    assert recovered.compaction_epoch == 2
    assert not (
        harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.pending.jsonl"
    ).exists()


@pytest.mark.parametrize("seed", range(6))
def test_seeded_queue_schedule_is_replayable_and_preserves_model_invariants(
    tmp_path: Path,
    seed: int,
) -> None:
    def run(root: Path) -> bytes:
        harness = create_harness(root)
        build = acquire(harness, "BUILD", tick=1)
        semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
        capability = _host_claim_inputs(harness)
        queue = _queue(harness)
        rng = random.Random(seed)
        desired: dict[tuple[str, str], SemanticDesiredWork] = {}
        watermark = 0

        for step in range(40):
            action = rng.choice(("enqueue", "enqueue", "claim", "compact", "reconcile"))
            monotonic_ns = 30_000 + step
            if action == "enqueue":
                watermark += 1
                operation = rng.choice(("DELETE", "UPSERT"))
                path = f"docs/{rng.randrange(3)}.md"
                work = _work(path, watermark, operation=operation)
                desired[(operation, path)] = work
                queue.enqueue(build, work, monotonic_ns=monotonic_ns)
            elif action == "claim":
                claim = queue.claim(
                    semantic,
                    **capability,
                    monotonic_ns=monotonic_ns,
                )
                if claim is not None:
                    if rng.randrange(3) == 0:
                        queue.fail(
                            semantic,
                            claim,
                            error_code="seeded_failure",
                            retryable=bool(rng.randrange(2)),
                            monotonic_ns=monotonic_ns,
                        )
                    else:
                        queue.complete(
                            semantic,
                            claim,
                            monotonic_ns=monotonic_ns,
                        )
            elif action == "compact":
                queue.compact(build, monotonic_ns=monotonic_ns)
            else:
                watermark += 1
                queue.reconcile(
                    build,
                    tuple(desired.values()),
                    source_epoch=1,
                    policy_sha256="1" * 64,
                    source_observations=_source_observations(
                        harness,
                        source_commit="a" * 40,
                    ),
                    desired_watermark=watermark,
                    semantic_required=bool(desired),
                    monotonic_ns=monotonic_ns,
                )
            snapshot = queue.inspect(REPO_UUID)
            assert SemanticQueueSnapshot.from_json(snapshot.canonical) == snapshot
            assert snapshot.completed_watermark <= snapshot.desired_watermark
            assert len({item.work.coalescing_key for item in snapshot.items}) == len(snapshot.items)
            assert all(
                item.desired_revision <= snapshot.desired_watermark for item in snapshot.items
            )
        return queue.inspect(REPO_UUID).canonical

    assert run(tmp_path / "first") == run(tmp_path / "replay")


@pytest.mark.parametrize(
    "failpoint",
    [
        "semantic_queue:pending_durable",
        "semantic_queue:previous_durable",
        "semantic_queue:current_replaced",
        "semantic_queue:current_durable",
        "semantic_queue:pending_cleared",
    ],
)
@pytest.mark.parametrize(
    "transition",
    [
        "enqueue",
        "reconcile",
        "claim",
        "checkpoint",
        "complete",
        "fail",
        "expired_claim_recovery",
        "bind_sealed_inputs",
        "compact",
    ],
)
def test_each_queue_transition_recovers_at_every_durable_commit_boundary(
    tmp_path: Path,
    transition: str,
    failpoint: str,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    capability = _host_claim_inputs(harness)
    queue = _queue(harness)
    semantic = acquire(
        harness,
        "SEMANTIC_CLAIM",
        tick=2,
        ttl_ns=5 if transition == "expired_claim_recovery" else 90_000_000_000,
    )
    claim = None
    successor = None

    if transition in {"enqueue", "reconcile"}:
        queue.enqueue(build, _work("docs/seed.md", 1), monotonic_ns=10_001)
    if transition in {"claim", "checkpoint", "complete", "fail"}:
        queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    if transition in {"checkpoint", "complete", "fail"}:
        claim = queue.claim(
            semantic,
            **capability,
            monotonic_ns=20_001,
        )
        assert claim is not None
    if transition == "expired_claim_recovery":
        queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
        claim = queue.claim(
            semantic,
            **capability,
            monotonic_ns=20_001,
        )
        assert claim is not None
        successor = acquire(harness, "SEMANTIC_CLAIM", tick=3)
    if transition in {"bind_sealed_inputs", "compact"}:
        queue.reconcile(
            build,
            (),
            source_epoch=1,
            policy_sha256="1" * 64,
            source_observations=_source_observations(harness),
            desired_watermark=1,
            semantic_required=False,
            monotonic_ns=10_001,
        )

    class CrashOnce:
        fired = False

        def __call__(self, event: str) -> None:
            if event == failpoint and not self.fired:
                self.fired = True
                raise InjectedFault(event)

    crashing = _queue(harness, fault_hook=CrashOnce())

    def apply(store: SemanticQueueStore) -> object:
        if transition == "enqueue":
            return store.enqueue(build, _work("docs/a.md", 2), monotonic_ns=40_001)
        if transition == "reconcile":
            return store.reconcile(
                build,
                (_work("docs/a.md", 2),),
                source_epoch=1,
                policy_sha256="1" * 64,
                source_observations=_source_observations(harness),
                desired_watermark=2,
                semantic_required=True,
                monotonic_ns=40_001,
            )
        if transition == "claim":
            return store.claim(
                semantic,
                **capability,
                monotonic_ns=40_001,
            )
        if transition == "checkpoint":
            assert claim is not None
            return store.checkpoint(
                semantic,
                claim,
                checkpoint="durable-boundary",
                monotonic_ns=40_001,
            )
        if transition == "complete":
            assert claim is not None
            return store.complete(semantic, claim, monotonic_ns=40_001)
        if transition == "fail":
            assert claim is not None
            return store.fail(
                semantic,
                claim,
                error_code="temporary",
                retryable=True,
                monotonic_ns=40_001,
            )
        if transition == "expired_claim_recovery":
            assert successor is not None
            return store.claim(
                successor,
                **capability,
                monotonic_ns=40_001,
            )
        if transition == "bind_sealed_inputs":
            return store.bind_sealed_inputs(
                build,
                sealed_input_manifest_sha256="3" * 64,
                monotonic_ns=40_001,
            )
        assert transition == "compact"
        return store.compact(build, monotonic_ns=40_001)

    with pytest.raises(CommitUnknown, match="semantic_queue recovery intent"):
        apply(crashing)

    recovered = _queue(harness)
    try:
        apply(recovered)
    except StaleSemanticClaim:
        assert transition in {"complete", "fail"}
    snapshot = recovered.inspect(REPO_UUID)
    assert SemanticQueueSnapshot.from_json(snapshot.canonical) == snapshot
    pending = harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.pending.jsonl"
    assert not pending.exists()

    if transition == "enqueue":
        assert any(item.work == _work("docs/a.md", 2) for item in snapshot.items)
    elif transition == "reconcile":
        assert snapshot.reconciliation is not None
        assert snapshot.reconciliation.desired == (_work("docs/a.md", 2),)
    elif transition == "claim":
        assert snapshot.items[0].status == "claimed"
    elif transition == "checkpoint":
        assert snapshot.items[0].claim is not None
        assert snapshot.items[0].claim.checkpoint == "durable-boundary"
    elif transition == "complete":
        assert snapshot.items[0].status == "completed"
    elif transition == "fail":
        assert snapshot.items[0].status == "pending"
        assert snapshot.items[0].last_error == "temporary"
    elif transition == "expired_claim_recovery":
        assert snapshot.items[0].status == "claimed"
        assert snapshot.items[0].failure_count == 1
        assert snapshot.items[0].claim is not None
        assert claim is not None
        assert snapshot.items[0].claim.fence_token > claim.fence_token
    elif transition == "bind_sealed_inputs":
        assert snapshot.reconciliation is not None
        assert snapshot.reconciliation.sealed_input_manifest_sha256 == "3" * 64
    else:
        assert snapshot.compaction_epoch >= 1


@pytest.mark.parametrize(
    "failpoint",
    [
        "semantic_queue:pending_durable",
        "semantic_queue:previous_durable",
        "semantic_queue:current_replaced",
        "semantic_queue:current_durable",
        "semantic_queue:pending_cleared",
    ],
)
def test_real_process_exit_recovers_idempotent_sealed_input_binding(
    tmp_path: Path,
    failpoint: str,
) -> None:
    def prepare(root: Path) -> tuple[RuntimeHarness, LeaseGrant, SemanticQueueStore]:
        prepared = create_harness(root)
        grant = acquire(prepared, "BUILD", tick=1)
        prepared_queue = _queue(prepared)
        prepared_queue.reconcile(
            grant,
            (),
            source_epoch=1,
            policy_sha256="1" * 64,
            source_observations=_source_observations(
                prepared,
                source_commit="a" * 40,
            ),
            desired_watermark=1,
            semantic_required=False,
            monotonic_ns=10_001,
        )
        return prepared, grant, prepared_queue

    harness, build, queue = prepare(tmp_path / "crash")
    before = queue.inspect(REPO_UUID).canonical
    harness.leases.release(build)

    _expected_harness, expected_build, expected_queue = prepare(tmp_path / "expected")
    expected = expected_queue.bind_sealed_inputs(
        expected_build,
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=10_002,
    ).canonical

    child = r"""
import os
from pathlib import Path
import sys

from graphify.workspace.leases import LeaseStore
from graphify.workspace.registry import RegistryStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy, SemanticQueueStore
from tests.workspace_p3_helpers import RuntimeHarness, SUPPORTED, acquire

state_root = Path(sys.argv[1])
repo = Path(sys.argv[2])
failpoint = sys.argv[3]
registry = RegistryStore(state_root, capabilities=SUPPORTED)
leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
harness = RuntimeHarness(repo=repo, state_root=state_root, registry=registry, leases=leases)
grant = acquire(harness, "BUILD", tick=2, ttl_ns=5)

class ExitAtBoundary:
    def __call__(self, event: str) -> None:
        if event == failpoint:
            os._exit(91)

queue = SemanticQueueStore(
    state_root,
    leases,
    policy=SemanticQueuePolicy(max_items=16, max_bytes=64 * 1024, retry_budget=1),
    capabilities=SUPPORTED,
    fault_hook=ExitAtBoundary(),
)
queue.bind_sealed_inputs(
    grant,
    sealed_input_manifest_sha256="3" * 64,
    monotonic_ns=20_001,
)
os._exit(0)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            os.fspath(harness.state_root),
            os.fspath(harness.repo),
            failpoint,
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 91, result.stderr
    current = harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.jsonl"
    observed = SemanticQueueSnapshot.from_json(current.read_bytes()).canonical
    assert observed in {before, expected}

    successor = acquire(harness, "BUILD", tick=3)
    recovered = _queue(harness).bind_sealed_inputs(
        successor,
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=30_001,
    )
    replayed = _queue(harness).bind_sealed_inputs(
        successor,
        sealed_input_manifest_sha256="3" * 64,
        monotonic_ns=30_002,
    )

    assert recovered.canonical == expected
    assert replayed.canonical == expected
    assert replayed.revision == recovered.revision
    pending = harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.pending.jsonl"
    assert not pending.exists()


class ShortWriteAndEintrSyscalls(PosixSyscalls):
    def __init__(self) -> None:
        self.interrupted = False

    def write(self, descriptor: int, data: memoryview) -> int:
        if not self.interrupted:
            self.interrupted = True
            raise InterruptedError(errno.EINTR, "injected EINTR")
        return super().write(descriptor, data[: max(1, len(data) // 3)])


class FailOnceSyscalls(PosixSyscalls):
    def __init__(self, operation: str, error_number: int) -> None:
        self.operation = operation
        self.error_number = error_number
        self.failed = False

    def _fail(self, operation: str) -> None:
        if self.operation == operation and not self.failed:
            self.failed = True
            raise OSError(self.error_number, f"injected {operation}")

    def write(self, descriptor: int, data: memoryview) -> int:
        self._fail("write")
        return super().write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        self._fail("fsync")
        super().fsync(descriptor)

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._fail("replace")
        super().replace_at(
            source,
            destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )


def test_short_write_and_eintr_install_one_canonical_queue_state(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness, syscalls=ShortWriteAndEintrSyscalls())

    snapshot = queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)

    assert snapshot.revision == 1
    assert queue.inspect(REPO_UUID).canonical == snapshot.canonical


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
@pytest.mark.parametrize("operation", ["write", "fsync", "replace"])
def test_syscall_failures_never_acknowledge_partial_queue_state(
    tmp_path: Path,
    error_number: int,
    operation: str,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness, syscalls=FailOnceSyscalls(operation, error_number))

    with pytest.raises((OSError, CommitUnknown)):
        queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)

    recovered = _queue(harness).enqueue(
        build,
        _work("docs/a.md", 1),
        monotonic_ns=10_002,
    )
    assert recovered.desired_watermark == 1


def test_corrupt_or_noncanonical_queue_state_fails_closed(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    current = harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.jsonl"
    current.write_bytes(b'{"contract":"wrong"}\n')
    before = current.read_bytes()

    with pytest.raises(SemanticQueueCorrupt):
        queue.inspect(REPO_UUID)

    assert current.read_bytes() == before


def test_tampered_claim_id_fails_closed_without_repair_or_mutation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None
    current = harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.jsonl"
    document = SemanticQueueSnapshot.from_json(current.read_bytes()).to_dict()
    items = cast(list[dict[str, Any]], document["items"])
    stored_claim = cast(dict[str, Any], items[0]["claim"])
    stored_claim["claim_id"] = "0" * 64
    current.write_bytes(canonical_json_bytes(document))
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticQueueCorrupt, match="claim_id"):
        queue.inspect(REPO_UUID)

    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        b'{"contract":"wrong"}\n',
        b'{"contract": "graphify.workspace.semantic_queue.internal"}\n',
        b'{"contract":',
    ],
    ids=("wrong-contract", "noncanonical", "truncated"),
)
def test_corruption_classes_fail_closed_without_repair_or_mutation(
    tmp_path: Path,
    corrupt_payload: bytes,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    current = harness.state_root / "workspaces" / REPO_UUID / "queue" / "semantic.jsonl"
    current.write_bytes(corrupt_payload)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticQueueCorrupt):
        queue.inspect(REPO_UUID)

    assert tree_snapshot(harness.state_root) == before
    assert current.read_bytes() == corrupt_payload


def test_active_policy_mismatch_fails_closed_without_queue_mutation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    incompatible = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=SemanticQueuePolicy(
            max_items=QUEUE_POLICY.max_items + 1,
            max_bytes=QUEUE_POLICY.max_bytes,
            retry_budget=QUEUE_POLICY.retry_budget,
        ),
        capabilities=harness.leases.state.capabilities,
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticQueueConflict, match="durable queue policy"):
        incompatible.inspect(REPO_UUID)

    assert tree_snapshot(harness.state_root) == before


def test_capability_decision_never_selects_ambient_or_disallowed_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    host_only = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-secret")
    monkeypatch.setenv("GRAPHIFY_GEMINI_MODEL", "ambient-model")

    host = decide_semantic_capability(
        host_only,
        host_agent_active=True,
        explicit_backend=None,
    )
    rejected = decide_semantic_capability(
        host_only,
        host_agent_active=False,
        explicit_backend="gemini",
    )
    explicit = cast(
        WorkspaceConfig,
        WorkspaceConfig.from_mapping(
            {
                **host_only.to_dict(),
                "policy": {
                    **host_only.to_dict()["policy"],
                    "semantic_mode": "explicit_backend",
                    "network_egress": True,
                    "headless_backends": ["gemini", "local"],
                },
            }
        ),
    )
    missing = decide_semantic_capability(
        explicit,
        host_agent_active=False,
        explicit_backend=None,
    )
    selected = decide_semantic_capability(
        explicit,
        host_agent_active=False,
        explicit_backend="gemini",
    )

    assert host.available and host.executor == "host_agent" and host.backend is None
    assert not rejected.available and rejected.executor is None
    assert not missing.available and missing.backend is None
    assert selected.available and selected.executor == "explicit_backend"
    assert selected.backend == "gemini"
    assert set(selected.to_dict()) == {"available", "executor", "backend", "reason"}


@pytest.mark.parametrize(
    ("backend", "network_egress", "allowlist", "reason"),
    [
        ("", True, ["gemini"], "explicit_backend_invalid"),
        ("gemini", True, ["local"], "explicit_backend_not_allowlisted"),
        ("gemini", False, ["gemini"], "network_egress_forbidden"),
    ],
    ids=("empty", "not-allowlisted", "egress-denied"),
)
def test_explicit_backend_rejection_is_stable_and_contains_no_ambient_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    network_egress: bool,
    allowlist: list[str],
    reason: str,
) -> None:
    harness = create_harness(tmp_path)
    base = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    explicit = cast(
        WorkspaceConfig,
        WorkspaceConfig.from_mapping(
            {
                **base.to_dict(),
                "policy": {
                    **base.to_dict()["policy"],
                    "semantic_mode": "explicit_backend",
                    "network_egress": network_egress,
                    "headless_backends": allowlist,
                },
            }
        ),
    )
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-secret")
    monkeypatch.setenv("GRAPHIFY_GEMINI_MODEL", "ambient-model")

    decision = decide_semantic_capability(
        explicit,
        host_agent_active=False,
        explicit_backend=backend,
    )

    assert decision.to_dict() == {
        "available": False,
        "executor": None,
        "backend": None,
        "reason": reason,
    }
    assert "ambient-secret" not in repr(decision.to_dict())
    assert "ambient-model" not in repr(decision.to_dict())


@pytest.mark.parametrize("backend", ("provider/model", "provider:model"))
def test_explicit_backend_treats_schema_valid_allowlisted_ids_as_opaque(
    tmp_path: Path,
    backend: str,
) -> None:
    harness = create_harness(tmp_path)
    base = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    explicit = cast(
        WorkspaceConfig,
        WorkspaceConfig.from_mapping(
            {
                **base.to_dict(),
                "policy": {
                    **base.to_dict()["policy"],
                    "semantic_mode": "explicit_backend",
                    "network_egress": True,
                    "headless_backends": [backend],
                },
            }
        ),
    )

    decision = decide_semantic_capability(
        explicit,
        host_agent_active=False,
        explicit_backend=backend,
    )

    assert decision.available
    assert decision.executor == "explicit_backend"
    assert decision.backend == backend
    assert decision.reason == "explicit_backend_selected"


def test_claim_accepts_only_an_explicit_backend_from_the_active_source_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    config_path = harness.repo / ".graphify/workspace.toml"
    config_path.write_text(
        'contract = "graphify.workspace.config"\n'
        "schema_version = 1\n"
        f'repo_uuid = "{REPO_UUID}"\n'
        "\n"
        "[policy]\n"
        'freshness = "current_only"\n'
        'semantic_mode = "explicit_backend"\n'
        "network_egress = true\n"
        'headless_backends = ["gemini"]\n',
        encoding="utf-8",
    )
    config = WorkspaceConfig.from_toml(config_path.read_bytes())
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    source_before = tree_snapshot(harness.repo)
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-secret")
    monkeypatch.setenv("GRAPHIFY_GEMINI_MODEL", "ambient-model")
    monkeypatch.setattr(
        harness.registry,
        "resolve_active_source",
        lambda _repo_uuid: pytest.fail("claim performed full source discovery"),
    )

    claim = queue.claim(
        semantic,
        config=config,
        host_agent_active=False,
        explicit_backend="gemini",
        monotonic_ns=20_001,
    )

    assert claim is not None
    assert claim.work.path == "docs/a.md"
    assert tree_snapshot(harness.repo) == source_before


def test_claim_rejects_source_path_replacement_before_reading_active_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    forged_bytes = (
        'contract = "graphify.workspace.config"\n'
        "schema_version = 1\n"
        f'repo_uuid = "{REPO_UUID}"\n'
        "\n"
        "[policy]\n"
        'freshness = "current_only"\n'
        'semantic_mode = "explicit_backend"\n'
        "network_egress = true\n"
        'headless_backends = ["gemini"]\n'
    ).encode()
    forged = WorkspaceConfig.from_toml(forged_bytes)
    original_read = semantic_queue_module.read_workspace_config_with_digest
    swapped = False
    read_calls = 0

    def replace_before_read(source_root: Path):
        nonlocal read_calls, swapped
        read_calls += 1
        if read_calls == 1:
            return original_read(source_root)
        if not swapped:
            swapped = True
            harness.repo.rename(tmp_path / "original-repo")
            (harness.repo / ".graphify").mkdir(parents=True)
            (harness.repo / ".graphify/workspace.toml").write_bytes(forged_bytes)
        return original_read(source_root)

    monkeypatch.setattr(
        semantic_queue_module,
        "read_workspace_config_with_digest",
        replace_before_read,
    )
    before = queue.inspect(REPO_UUID).canonical

    with pytest.raises(
        SemanticCapabilityUnavailable,
        match="workspace_config_(mismatch|unavailable)",
    ):
        queue.claim(
            semantic,
            config=forged,
            host_agent_active=False,
            explicit_backend="gemini",
            monotonic_ns=20_001,
        )

    assert swapped
    assert read_calls == 2
    assert queue.inspect(REPO_UUID).canonical == before


def test_claim_rejects_same_policy_source_replacement_during_locked_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    config_bytes = (harness.repo / ".graphify/workspace.toml").read_bytes()
    config = WorkspaceConfig.from_toml(config_bytes)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    original_read = semantic_queue_module.read_workspace_config_with_digest
    swapped = False
    read_calls = 0

    def replace_before_read(source_root: Path):
        nonlocal read_calls, swapped
        read_calls += 1
        if read_calls == 1:
            return original_read(source_root)
        if not swapped:
            swapped = True
            harness.repo.rename(tmp_path / "original-repo")
            (harness.repo / ".graphify").mkdir(parents=True)
            (harness.repo / ".graphify/workspace.toml").write_bytes(config_bytes)
        return original_read(source_root)

    monkeypatch.setattr(
        semantic_queue_module,
        "read_workspace_config_with_digest",
        replace_before_read,
    )
    before = queue.inspect(REPO_UUID).canonical

    with pytest.raises(
        SemanticCapabilityUnavailable,
        match="workspace_config_unavailable",
    ):
        queue.claim(
            semantic,
            config=config,
            host_agent_active=True,
            explicit_backend=None,
            monotonic_ns=20_001,
        )

    assert swapped
    assert read_calls == 2
    assert queue.inspect(REPO_UUID).canonical == before


def test_claim_rejects_aba_policy_replacement_during_config_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    forged_bytes = (
        'contract = "graphify.workspace.config"\n'
        "schema_version = 1\n"
        f'repo_uuid = "{REPO_UUID}"\n'
        "\n"
        "[policy]\n"
        'freshness = "current_only"\n'
        'semantic_mode = "explicit_backend"\n'
        "network_egress = true\n"
        'headless_backends = ["gemini"]\n'
    ).encode()
    forged = WorkspaceConfig.from_toml(forged_bytes)
    original_read = semantic_queue_module.read_workspace_config_with_digest
    read_calls = 0

    def read_during_replacement(source_root: Path):
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            return original_read(source_root)
        original_root = tmp_path / "original-repo"
        harness.repo.rename(original_root)
        (harness.repo / ".graphify").mkdir(parents=True)
        (harness.repo / ".graphify/workspace.toml").write_bytes(forged_bytes)
        try:
            return original_read(source_root)
        finally:
            shutil.rmtree(harness.repo)
            original_root.rename(harness.repo)

    monkeypatch.setattr(
        semantic_queue_module,
        "read_workspace_config_with_digest",
        read_during_replacement,
    )
    before = queue.inspect(REPO_UUID).canonical

    with pytest.raises(
        SemanticCapabilityUnavailable,
        match="workspace_config_mismatch",
    ):
        queue.claim(
            semantic,
            config=forged,
            host_agent_active=False,
            explicit_backend="gemini",
            monotonic_ns=20_001,
        )

    assert read_calls == 2
    assert queue.inspect(REPO_UUID).canonical == before


def test_claim_rejects_an_unavailable_capability_without_queue_mutation(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    config = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticCapabilityUnavailable):
        queue.claim(
            semantic,
            config=config,
            host_agent_active=False,
            explicit_backend=None,
            monotonic_ns=20_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_claim_boundary_rejects_forged_or_policy_forbidden_capability(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    config = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    forged = SemanticCapabilityDecision(
        available=True,
        executor="explicit_backend",
        backend="not-allowlisted",
        reason="forged",
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(TypeError, match="capability"):
        cast(Any, queue.claim)(
            semantic,
            capability=forged,
            monotonic_ns=20_001,
        )
    with pytest.raises(
        SemanticCapabilityUnavailable,
        match="explicit_backend_forbidden",
    ):
        queue.claim(
            semantic,
            config=config,
            host_agent_active=False,
            explicit_backend="not-allowlisted",
            monotonic_ns=20_002,
        )

    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize(
    "config_repo_uuid",
    ("22222222-2222-4222-8222-222222222222", REPO_UUID),
    ids=("foreign-uuid", "relabeled-current-uuid"),
)
def test_claim_rejects_substituted_capability_policy_without_mutation(
    tmp_path: Path,
    config_repo_uuid: str,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    config = WorkspaceConfig.from_toml((harness.repo / ".graphify/workspace.toml").read_bytes())
    foreign = cast(
        WorkspaceConfig,
        WorkspaceConfig.from_mapping(
            {
                **config.to_dict(),
                "repo_uuid": config_repo_uuid,
                "policy": {
                    **config.to_dict()["policy"],
                    "semantic_mode": "explicit_backend",
                    "network_egress": True,
                    "headless_backends": ["gemini"],
                },
            }
        ),
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        SemanticCapabilityUnavailable,
        match="workspace_config_mismatch",
    ):
        queue.claim(
            semantic,
            config=foreign,
            host_agent_active=False,
            explicit_backend="gemini",
            monotonic_ns=20_001,
        )

    assert tree_snapshot(harness.state_root) == before


def test_activation_between_registry_snapshot_and_queue_lock_rejects_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    linked_source, registry_revision, active_source_revision = (
        _rebind_linked_policy_source(harness, tmp_path)
    )
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    queue.enqueue(build, _work("docs/a.md", 1), monotonic_ns=10_001)
    harness.leases.release(build)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    config = WorkspaceConfig.from_toml(
        (harness.repo / ".graphify/workspace.toml").read_bytes()
    )
    queue_before = queue.inspect(REPO_UUID).canonical
    original_snapshot = harness.registry.recovered_snapshot
    activated = False

    @contextmanager
    def snapshot_then_activate() -> Iterator[Any]:
        nonlocal activated
        with original_snapshot() as document:
            yield document
        if activated:
            return
        activated = True
        _activate_linked_policy_source(
            harness,
            linked_source,
            registry_revision=registry_revision,
            active_source_revision=active_source_revision,
            nonce="activate-claim-gap",
            monotonic_ns=30_000,
        )

    monkeypatch.setattr(harness.registry, "recovered_snapshot", snapshot_then_activate)

    with pytest.raises(StaleSemanticClaim, match="active source revision"):
        queue.claim(
            semantic,
            config=config,
            host_agent_active=True,
            explicit_backend=None,
            monotonic_ns=20_001,
        )

    assert activated
    assert queue.inspect(REPO_UUID).canonical == queue_before


def test_activation_cannot_commit_before_semantic_grant_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    linked_source, registry_revision, active_source_revision = (
        _rebind_linked_policy_source(harness, tmp_path)
    )
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    before = queue.reconcile(
        build,
        (),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=1,
        semantic_required=False,
        monotonic_ns=10_001,
    )
    harness.leases.release(build)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    original_snapshot = harness.registry.recovered_snapshot
    queue_path = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "queue"
        / "semantic.jsonl"
    )
    queue_at_activation: bytes | None = None

    @contextmanager
    def snapshot_then_activate() -> Iterator[Any]:
        nonlocal queue_at_activation
        with original_snapshot() as document:
            yield document
        if queue_at_activation is not None:
            return
        queue_at_activation = queue_path.read_bytes()
        _activate_linked_policy_source(
            harness,
            linked_source,
            registry_revision=registry_revision,
            active_source_revision=active_source_revision,
            nonce="activate-compaction-gap",
            monotonic_ns=30_000,
        )

    monkeypatch.setattr(harness.registry, "recovered_snapshot", snapshot_then_activate)

    compacted = queue.compact(semantic, monotonic_ns=20_001)

    assert compacted.compaction_epoch == before.compaction_epoch + 1
    assert queue_at_activation == compacted.canonical
    assert queue.inspect(REPO_UUID).canonical == compacted.canonical


def test_queue_transitions_never_write_the_source_checkout(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    source_before = tree_snapshot(harness.repo)
    build = acquire(harness, "BUILD", tick=1)
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    queue = _queue(harness)
    queue.reconcile(
        build,
        (_work("docs/a.md", 1),),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(harness),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=20_001,
    )
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_002,
    )
    assert claim is not None
    queue.checkpoint(
        semantic,
        claim,
        checkpoint="source-pure",
        monotonic_ns=20_003,
    )
    queue.complete(semantic, claim, monotonic_ns=20_004)
    queue.compact(build, monotonic_ns=20_005)

    assert tree_snapshot(harness.repo) == source_before


def test_generation_certification_revalidates_a_stable_queue_view(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    observation = "2" * 64
    queue.reconcile(
        build,
        (_work("docs/a.md", 1),),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(
            harness,
            inventory_sha256=observation,
        ),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=10_001,
    )
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None
    queue.complete(semantic, claim, monotonic_ns=20_002)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )

    def enqueue_newer_after_view(event: str) -> None:
        if event == "generation:gen-queue-race:queue_view_captured":
            queue.enqueue(
                build,
                _work("docs/a.md", 2, digest="f" * 64),
                monotonic_ns=20_004,
            )

    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue=queue,
        capabilities=harness.leases.state.capabilities,
        fault_hook=enqueue_newer_after_view,
    )
    allocation = generations.allocate(
        build,
        expected_payload_bytes=4096,
        capacity_policy=GENERATION_POLICY,
        generation_id="gen-queue-race",
        occurred_at=START,
        monotonic_ns=20_003,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text(
        '{"nodes": [], "edges": []}\n',
        encoding="utf-8",
    )
    entries = generations.inspect_staged_payload(allocation)
    observations = _source_observations(
        harness,
        inventory_sha256=observation,
    )
    queue.bind_sealed_inputs(
        build,
        sealed_input_manifest_sha256=payload_manifest_sha256(
            "graphify-out",
            entries,
        ),
        monotonic_ns=20_004,
    )
    trust_source_observations(generations, observations)

    with pytest.raises(SemanticCertificationBlocked, match="queue view changed"):
        generations.certify(
            build,
            allocation,
            CertificationRequest(
                source_commit=_source_commit(harness),
                source_epoch=1,
                policy_sha256="1" * 64,
                observation_manifest_sha256=observation,
                queue_watermark=1,
                semantic_completeness="complete",
                compatibility_sha256=COMPATIBILITY_SHA256,
                validations=("payload_manifest", "stable_semantic_queue"),
            ),
            source_observations=observations,
            declared_entries=entries,
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=20_005,
        )

    assert queue.inspect(REPO_UUID).desired_watermark == 2
    assert allocation.staging_path.is_dir()


def test_generation_receipt_uses_the_exact_durable_queue_watermark(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    build = acquire(harness, "BUILD", tick=1)
    queue = _queue(harness)
    observation = "2" * 64
    queue.reconcile(
        build,
        (_work("docs/a.md", 1),),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=_source_observations(
            harness,
            inventory_sha256=observation,
        ),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=10_001,
    )
    semantic = acquire(harness, "SEMANTIC_CLAIM", tick=2)
    claim = queue.claim(
        semantic,
        **_host_claim_inputs(harness),
        monotonic_ns=20_001,
    )
    assert claim is not None
    queue.complete(semantic, claim, monotonic_ns=20_002)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue=queue,
        capabilities=harness.leases.state.capabilities,
    )
    allocation = generations.allocate(
        build,
        expected_payload_bytes=4096,
        capacity_policy=GENERATION_POLICY,
        generation_id="gen-queue-certified",
        occurred_at=START,
        monotonic_ns=20_003,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text(
        '{"nodes": [], "edges": []}\n',
        encoding="utf-8",
    )
    entries = generations.inspect_staged_payload(allocation)
    observations = _source_observations(
        harness,
        inventory_sha256=observation,
    )
    queue.bind_sealed_inputs(
        build,
        sealed_input_manifest_sha256=payload_manifest_sha256(
            "graphify-out",
            entries,
        ),
        monotonic_ns=20_004,
    )
    trust_source_observations(generations, observations)
    receipt = generations.certify(
        build,
        allocation,
        CertificationRequest(
            source_commit=_source_commit(harness),
            source_epoch=1,
            policy_sha256="1" * 64,
            observation_manifest_sha256=observation,
            queue_watermark=1,
            semantic_completeness="complete",
            compatibility_sha256=COMPATIBILITY_SHA256,
            validations=("payload_manifest", "stable_semantic_queue"),
        ),
        source_observations=observations,
        declared_entries=entries,
        occurred_at=START + timedelta(seconds=1),
        monotonic_ns=20_005,
    )

    value = receipt.to_dict()
    assert value["queue_watermark"] == 1
    assert value["semantic_completeness"] == "complete"
    assert generations.verify_generation(REPO_UUID, "gen-queue-certified").canonical == (
        receipt.canonical
    )
