"""S4 exact retries close leases left behind by terminal abandonment."""

import time

import pytest

from graphify.workspace.adapters.base import QueryRequest, StructuralBuildIncomplete
from graphify.workspace.generations import GenerationConflict
from graphify.workspace.leases import LeaseRecoveryRequired
from graphify.workspace.persistence import CommitUnknown, InjectedFault
from graphify.workspace.query import query_structural
from graphify.workspace.sync import synchronize_structural
from tests.test_workspace_structural_s4 import request_for, runtime_fixture
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


@pytest.mark.parametrize("boundary", ["before_release", "pending_durable"])
def test_abandoned_exact_retry_cleans_interrupted_lease_release(tmp_path, monkeypatch, boundary):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    stores = runtime.stores
    (repo / "script.r").write_text("x <- 1\n")
    request = request_for(runtime)
    original_release = stores.leases.release
    reached = []

    def fault(label):
        if label == "workspace:pending_durable":
            reached.append(label)
            raise InjectedFault(label)

    def interrupted_release(grant):
        if boundary == "before_release":
            reached.append(boundary)
            raise OSError("interrupted lease release")
        stores.leases.state.fault_hook = fault
        return original_release(grant)

    monkeypatch.setattr(stores.leases, "release", interrupted_release)
    with pytest.raises((StructuralBuildIncomplete, CommitUnknown)):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert reached
    monkeypatch.setattr(stores.leases, "release", original_release)
    stores.leases.state.fault_hook = lambda label: None
    staged = stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
    assert staged.lifecycle_state == "ABANDONED"

    if boundary == "before_release":
        assert stores.leases.inspect(REPO_UUID).leases
        before = tree_snapshot(runtime.inputs.state_root)
        with pytest.raises((GenerationConflict, LeaseRecoveryRequired)):
            synchronize_structural(runtime, request, attempt_sha256="b" * 64)
        assert tree_snapshot(runtime.inputs.state_root) == before

    # The exact request stays abandoned, but its paired lease must be released.
    with pytest.raises(GenerationConflict, match="abandoned"):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert not stores.leases.inspect(REPO_UUID).leases
    before = tree_snapshot(runtime.inputs.state_root)
    with pytest.raises(GenerationConflict, match="abandoned"):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    assert tree_snapshot(runtime.inputs.state_root) == before
    assert stores.pointers.load(REPO_UUID, allow_missing=True) is None

    (repo / "script.r").unlink()
    result = synchronize_structural(
        runtime, request_for(runtime, "gen-after-cleanup"), attempt_sha256="c" * 64,
    )
    assert result.pointer_revision == 1
    assert "leaf" in query_structural(
        runtime, REPO_UUID, QueryRequest("caller"),
        deadline_ns=time.monotonic_ns() + 60_000_000_000,
    )
