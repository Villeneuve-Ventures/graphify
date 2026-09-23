"""Coordination boundary tests for the S3 stack."""

import pytest

from graphify.workspace.identity import IdentityAction, OperatorAuthorization, discover_source
from graphify.workspace.registry import RevisionConflict, SourceAlreadyActive
from graphify.workspace.semantic_queue import SemanticQueueError, SemanticQueuePolicy, SemanticQueueStore
from tests.workspace_s3_helpers import REPO_UUID, START, create_harness, git_output, tree_snapshot


def test_source_activation_requires_adopted_linked_worktree_and_exact_cas(tmp_path):
    harness = create_harness(tmp_path)
    linked = tmp_path.resolve() / "linked"
    git_output(harness.repo, "worktree", "add", "--quiet", str(linked))
    source_b = discover_source(linked)
    auth = OperatorAuthorization(
        action=IdentityAction.ADOPT, operator_id="operator:s3-test", reason="linked fixture",
        issued_at="2026-07-16T19:00:00Z", nonce="adopt-linked",
    )
    adopted = harness.registry.adopt(source_b, auth, expected_revision=1)
    assert adopted.to_dict()["revision"] == 2
    lease_state = harness.leases.inspect(REPO_UUID)
    activate = OperatorAuthorization(
        action=IdentityAction.ACTIVATE, operator_id="operator:s3-test", reason="select linked fixture",
        issued_at="2026-07-16T19:00:00Z", nonce="activate-linked",
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(RevisionConflict):
        harness.registry.activate_source(
            source_b, activate, leases=harness.leases, owner=harness.leases.current_owner(),
            expected_registry_revision=1, expected_active_source_revision=1,
            expected_operation_epoch=lease_state.operation_epoch,
            expected_migration_epoch=lease_state.migration_epoch,
            acquired_at=START, monotonic_ns=10_000, ttl_ns=1_000_000,
        )
    assert tree_snapshot(harness.state_root) == before
    selected = harness.registry.activate_source(
        source_b, activate, leases=harness.leases, owner=harness.leases.current_owner(),
        expected_registry_revision=2, expected_active_source_revision=1,
        expected_operation_epoch=lease_state.operation_epoch,
        expected_migration_epoch=lease_state.migration_epoch,
        acquired_at=START, monotonic_ns=10_000, ttl_ns=1_000_000,
        require_source_change=True,
    )
    assert selected.registry.to_dict()["workspaces"][0]["active_source"]["path"] == str(linked)
    assert harness.registry.resolve_active_source(REPO_UUID).root == linked
    current = selected.registry.to_dict()
    lease_state = harness.leases.inspect(REPO_UUID)
    with pytest.raises(SourceAlreadyActive):
        harness.registry.activate_source(
            source_b, activate, leases=harness.leases, owner=harness.leases.current_owner(),
            expected_registry_revision=current["revision"], expected_active_source_revision=2,
            expected_operation_epoch=lease_state.operation_epoch,
            expected_migration_epoch=lease_state.migration_epoch,
            acquired_at=START, monotonic_ns=11_000, ttl_ns=1_000_000,
            require_source_change=True,
        )


def test_semantic_queue_policy_has_explicit_bounds():
    policy = SemanticQueuePolicy(max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=1)
    assert SemanticQueuePolicy.from_mapping(policy.to_dict()) == policy
    with pytest.raises(SemanticQueueError):
        SemanticQueuePolicy(max_items=1, max_bytes=16, retry_budget=0, max_claimed_tasks=2)


def test_semantic_queue_constructor_does_not_create_state(tmp_path):
    harness = create_harness(tmp_path)
    before = tree_snapshot(harness.state_root)
    queue = SemanticQueueStore(
        harness.state_root, harness.leases,
        policy=SemanticQueuePolicy(max_items=8, max_bytes=16384, retry_budget=0, max_claimed_tasks=1),
        capabilities=harness.leases.state.capabilities,
    )
    assert queue.state.root == harness.state_root
    assert tree_snapshot(harness.state_root) == before
