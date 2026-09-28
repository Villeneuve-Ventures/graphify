"""Real v8 engine + S3 lifecycle in disposable external roots."""
import os
import sys
import time
import pytest

from graphify.workspace.adapters.base import QueryRequest
from graphify.workspace.composition import (
    WorkspaceRuntimeAuthority, WorkspaceRuntimeInputs, compose_workspace_runtime,
)
from graphify.workspace.identity import discover_source
from graphify.workspace.query import query_structural
from graphify.workspace.sync import StructuralSyncRequest, prepare_structural_sync, synchronize_structural
from tests.test_workspace_contracts import compatibility
from tests.workspace_s3_helpers import REPO_UUID, authorization, create_repo, tree_snapshot


def runtime_fixture(tmp_path, monkeypatch, *, native=False):
    import graphify.workspace.composition as composition
    # Unit/integration fixtures use an explicit synthetic package identity. The
    # separate installed-wheel proof exercises the real package admission path.
    monkeypatch.setattr(composition, "verify_installed_candidate", lambda expected: None)
    # Preserve synthetic identity only in these unit fixtures. Installed-wheel
    # tests use the unchanged worker bootstrap and real child admission.
    from graphify.workspace import _readonly
    monkeypatch.setattr(_readonly, '_CHILD_BOOTSTRAP', _readonly._CHILD_BOOTSTRAP.replace(
        "exec(operation,", "import graphify.workspace.composition as _fixture_composition\n"
        "_fixture_composition.verify_installed_candidate = lambda expected: None\nexec(operation,"))
    from graphify.workspace.persistence import RuntimeCapabilities
    for key in tuple(os.environ):
        if key.endswith("API_KEY") or key in {"GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_AUTH_TOKEN"}:
            monkeypatch.delenv(key)
    monkeypatch.setitem(sys.modules, "graphify.llm", None)
    if not native:
        monkeypatch.setattr(RuntimeCapabilities, "detect", classmethod(lambda cls, path: RuntimeCapabilities.supported_test_fixture()))
    root = tmp_path.resolve()
    repo = create_repo(root / "repo")
    (repo / "main.py").write_text("def leaf():\n    return 42\n\ndef caller():\n    return leaf()\n")
    state = root / "state"
    state.mkdir(mode=0o700)
    candidate = compatibility()
    auth = WorkspaceRuntimeAuthority.from_mapping({
        "contract": "graphify.workspace.runtime_authority.internal", "format_version": 2,
        "compatibility_manifest": candidate.to_dict(), "structural_policy": {
            "max_pending_tasks": 8, "max_pending_bytes": 16384, "max_claimed_tasks": 1,
            "max_generations": 8, "max_payload_bytes": 1024 * 1024,
        },
    })
    path = state / "runtime-manifest.json"
    path.write_bytes(auth.canonical)
    path.chmod(0o600)
    runtime = compose_workspace_runtime(WorkspaceRuntimeInputs(state, auth, candidate)).require_runtime()
    runtime.stores.registry.enroll(discover_source(repo), authorization("enroll-s4"), expected_revision=0)
    return runtime, repo


def request_for(runtime, generation="gen-s4"):
    return prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id=generation,
        source_epoch=1, desired_watermark=1, expected_payload_bytes=1024 * 1024)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS/APFS proof")
def test_native_structural_round_trip(tmp_path, monkeypatch):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch, native=True)
    before_source = tree_snapshot(repo)
    request = request_for(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert result.pointer_revision == 1
    before_state = tree_snapshot(runtime.inputs.state_root)
    text = query_structural(runtime, REPO_UUID, QueryRequest("caller"), deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert "caller" in text and "leaf" in text
    assert tree_snapshot(runtime.inputs.state_root) == before_state
    assert tree_snapshot(repo) == before_source
    assert synchronize_structural(runtime, request, attempt_sha256="b" * 64) == result
    assert tree_snapshot(runtime.inputs.state_root) == before_state


def test_linked_source_is_explicitly_adopted_and_activated(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    import time
    from graphify.workspace.identity import IdentityAction
    from graphify.workspace.registry import SourceAlreadyActive
    from tests.workspace_s3_helpers import git_output

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    linked = tmp_path.resolve() / "linked"
    git_output(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    (linked / "main.py").write_text((repo / "main.py").read_text())
    source = discover_source(linked)
    stores = runtime.stores
    stores.registry.adopt(source, replace(authorization("adopt-s4"), action=IdentityAction.ADOPT),
                          expected_revision=1)
    lease = stores.leases.inspect(REPO_UUID)
    kwargs = dict(leases=stores.leases, owner=stores.leases.current_owner(),
        expected_registry_revision=2, expected_active_source_revision=1,
        expected_operation_epoch=lease.operation_epoch, expected_migration_epoch=lease.migration_epoch,
        acquired_at=datetime.now(timezone.utc), monotonic_ns=time.monotonic_ns(), ttl_ns=10**12)
    stores.registry.activate_source(source, replace(authorization("activate-s4"), action=IdentityAction.ACTIVATE), **kwargs)
    assert stores.registry.resolve_active_source(REPO_UUID).root == linked
    lease = stores.leases.inspect(REPO_UUID)
    kwargs.update(expected_registry_revision=3, expected_active_source_revision=2,
                  expected_operation_epoch=lease.operation_epoch)
    with pytest.raises(SourceAlreadyActive):
        stores.registry.activate_source(source, replace(authorization("again-s4"), action=IdentityAction.ACTIVATE), **kwargs)
    before = tree_snapshot(repo), tree_snapshot(linked)
    request = request_for(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    receipt = stores.generations.verify_generation(REPO_UUID, result.generation_id).to_dict()
    assert receipt["active_source_revision"] == 2
    assert receipt["semantic_completeness"] == "not_required"
    assert receipt["completion_binding"]["initial_detection_sha256"] == request.build.observation_manifest_sha256
    assert "leaf" in query_structural(runtime, REPO_UUID, QueryRequest("caller"), deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert (tree_snapshot(repo), tree_snapshot(linked)) == before


@pytest.mark.parametrize("boundary", [
    "request_staged", "build_acquired", "generation_allocated", "staging_prepared", "adapter_built",
    "staging_completed", "queue_reconciled", "sealed_inputs_bound", "generation_certified",
    "build_released", "promotion_acquired", "pointer_moved", "promotion_completed", "promotion_released",
    "receipt_durable", "installed", "staged_certified_durable",
    "pointer:promoted:pending_durable", "pointer:promoted:visible", "pointer:promoted:journal_durable",
])
def test_exact_retry_at_durable_boundaries(tmp_path, monkeypatch, boundary):
    from graphify.workspace.persistence import InjectedFault
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    seen = []
    def fault(label):
        if label == boundary or label.endswith(":" + boundary):
            seen.append(label)
            raise InjectedFault(label)
    runtime.stores.generations.fault_hook = fault
    runtime.stores.pointers.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert seen
    runtime.stores.generations.fault_hook = lambda label: None
    runtime.stores.pointers.fault_hook = lambda label: None
    runtime = compose_workspace_runtime(runtime.inputs).require_runtime()
    request = StructuralSyncRequest.from_json(request.canonical)
    result = synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    assert result.pointer_revision == (2 if boundary.startswith("pointer:") else 1)
    before = tree_snapshot(runtime.inputs.state_root)
    assert synchronize_structural(runtime, request, attempt_sha256="c" * 64) == result
    assert tree_snapshot(runtime.inputs.state_root) == before
    assert "leaf" in query_structural(runtime, REPO_UUID, QueryRequest("caller"), deadline_ns=time.monotonic_ns() + 60_000_000_000)


@pytest.mark.parametrize("moment", ["before", "after"])
def test_query_suppresses_source_drift_without_state_writes(tmp_path, monkeypatch, moment):
    from graphify.source_io import SourceError
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    synchronize_structural(runtime, request_for(runtime), attempt_sha256="a" * 64)
    before = tree_snapshot(runtime.inputs.state_root)
    called = []
    original = runtime.adapter.query_structural
    def traverse(fd, request, **kwargs):
        called.append(True)
        result = original(fd, request, **kwargs)
        (repo / "README.md").write_text("changed original non-code input")
        return result
    monkeypatch.setattr(runtime.adapter, "query_structural", traverse)
    if moment == "before":
        (repo / "README.md").write_text("changed original non-code input")
    with pytest.raises((SourceError, ValueError)):
        query_structural(runtime, REPO_UUID, QueryRequest("caller"), deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert bool(called) == (moment == "after")
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_build_drift_cannot_complete_or_promote(tmp_path, monkeypatch):
    from graphify.source_io import SourceError
    from graphify.workspace.generations import GenerationConflict
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    def fault(label):
        if label.endswith(":adapter_built"):
            (repo / "main.py").write_text("def changed(): pass\n")
    runtime.stores.generations.fault_hook = fault
    with pytest.raises((SourceError, ValueError)):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
    runtime.stores.generations.fault_hook = lambda label: None
    with pytest.raises(GenerationConflict, match="abandoned"):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None


@pytest.mark.parametrize("inputs", [
    {"script.r": "x <- 1\n"},
    {"script.F90": "program a\nend program\n"},
    {"a.r": "x <- 1\n", "z.F90": "program a\nend program\n"},
])
def test_incomplete_extraction_closes_staging_and_allows_new_request(tmp_path, monkeypatch, inputs):
    from graphify.workspace.adapters.base import StructuralBuildIncomplete
    from graphify.workspace.generations import GenerationConflict
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    for name, content in inputs.items():
        (repo / name).write_text(content)
    request = request_for(runtime)
    with pytest.raises(StructuralBuildIncomplete):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    state = runtime.stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
    assert state.lifecycle_state == "ABANDONED"
    assert state.abandon_reason == "EXTRACTION_INCOMPLETE"
    assert len(state.canonical) < 64 * 1024
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
    with pytest.raises(GenerationConflict, match="abandoned"):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    for name in inputs:
        (repo / name).unlink()
    successor = request_for(runtime, "gen-repaired")
    result = synchronize_structural(runtime, successor, attempt_sha256="c" * 64)
    assert result.pointer_revision == 1
    assert "leaf" in query_structural(
        runtime, REPO_UUID, QueryRequest("caller"),
        deadline_ns=time.monotonic_ns() + 60_000_000_000,
    )


def test_incomplete_extraction_abandon_intent_recovers_exactly(tmp_path, monkeypatch):
    from graphify.workspace.generations import GenerationConflict
    from graphify.workspace.persistence import InjectedFault
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    (repo / "script.r").write_text("x <- 1\n")
    request = request_for(runtime)
    def fault(label):
        if label.endswith(":abandon_intent_durable"):
            raise InjectedFault(label)
    runtime.stores.generations.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    runtime.stores.generations.fault_hook = lambda label: None
    runtime = compose_workspace_runtime(runtime.inputs).require_runtime()
    with pytest.raises(GenerationConflict, match="abandoned"):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    state = runtime.stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
    assert state.lifecycle_state == "ABANDONED"
    assert state.abandon_reason == "EXTRACTION_INCOMPLETE"


def test_query_keeps_generation_lock_and_refuses_ordinary_writes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from graphify.workspace.persistence import LockTimeout
    from graphify.export import to_json
    import networkx as nx
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    result = synchronize_structural(runtime, request_for(runtime), attempt_sha256="a" * 64)
    original = runtime.adapter.query_structural
    def traverse(fd, request, **kwargs):
        def try_gc_lock():
            with runtime.stores.generations.state.existing_generation_lock(
                runtime.stores.generations._lock(REPO_UUID, result.generation_id),
                generation_id=result.generation_id, exclusive=True,
                deadline_ns=time.monotonic_ns() + 50_000_000):
                pytest.fail("GC obtained the protected generation")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(LockTimeout):
                pool.submit(try_gc_lock).result(timeout=5)
        return original(fd, request, **kwargs)
    monkeypatch.setattr(runtime.adapter, "query_structural", traverse)
    before = tree_snapshot(runtime.inputs.state_root)
    assert "leaf" in query_structural(runtime, REPO_UUID, QueryRequest("caller"), deadline_ns=time.monotonic_ns() + 60_000_000_000)
    generation = runtime.inputs.state_root / "workspaces" / REPO_UUID / "generations" / result.generation_id
    with pytest.raises(Exception, match="workspace|managed"):
        to_json(nx.DiGraph(), {}, str(generation / "graphify-out/graph.json"), force=True)
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_competing_request_and_authority_mismatch_do_not_mutate(tmp_path, monkeypatch):
    from dataclasses import replace
    from graphify.workspace.generations import GenerationConflict
    from graphify.workspace.persistence import InjectedFault
    from graphify.workspace.composition import WorkspaceAuthorityInvalid
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    other = request_for(runtime, "gen-other")
    def fault(label):
        if label.endswith(":request_staged"):
            raise InjectedFault(label)
    runtime.stores.generations.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    before = tree_snapshot(runtime.inputs.state_root)
    with pytest.raises(GenerationConflict, match="another exact request"):
        synchronize_structural(runtime, other, attempt_sha256="b" * 64)
    assert tree_snapshot(runtime.inputs.state_root) == before
    with pytest.raises(GenerationConflict, match="sync coordinates"):
        replace(request, desired_watermark=2)
    authority = runtime.inputs.authority.to_dict()
    authority["structural_policy"]["max_generations"] = 7
    (runtime.inputs.state_root / "runtime-manifest.json").write_bytes(WorkspaceRuntimeAuthority.from_mapping(authority).canonical)
    before = tree_snapshot(runtime.inputs.state_root)
    with pytest.raises(WorkspaceAuthorityInvalid, match="authority changed"):
        synchronize_structural(runtime, request, attempt_sha256="c" * 64)
    assert tree_snapshot(runtime.inputs.state_root) == before


@pytest.mark.parametrize("record", ["request", "lease_acquire", "lease_heartbeat", "lease_release", "queue"])
def test_pending_record_commit_retries_exactly(tmp_path, monkeypatch, record):
    from graphify.workspace.persistence import CommitUnknown, InjectedFault
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    stores = runtime.stores
    counter = 0
    labels = []
    def fault(label):
        nonlocal counter
        prefix = {"request": f"staged-build:{REPO_UUID}", "lease_acquire": "workspace", "lease_heartbeat": "workspace",
                  "lease_release": "workspace", "queue": "semantic_queue"}[record]
        if label == prefix + ":pending_durable":
            counter += 1
            if counter == {"lease_heartbeat": 2, "lease_release": 3}.get(record, 1):
                labels.append(label)
                raise InjectedFault(label)
    target = {"request": stores.generations.state, "lease_acquire": stores.leases.state, "lease_heartbeat": stores.leases.state,
              "lease_release": stores.leases.state, "queue": stores.queue.state}[record]
    target.fault_hook = fault
    with pytest.raises((CommitUnknown, InjectedFault)):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert labels
    target.fault_hook = lambda label: None
    result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert result.pointer_revision == 1
    assert "leaf" in query_structural(runtime, REPO_UUID, QueryRequest("caller"), deadline_ns=time.monotonic_ns() + 60_000_000_000)


def test_retained_semantic_state_blocks_before_writes(tmp_path, monkeypatch):
    from graphify.workspace.generations import GenerationConflict
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    runtime.stores.generations.state.ensure_directory(
        runtime.stores.leases._directory(REPO_UUID) / "semantic-staging")
    before = tree_snapshot(runtime.inputs.state_root)
    with pytest.raises(GenerationConflict, match="semantic lifecycle"):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_expired_successor_fence_refuses_adapter_writes(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    import time
    from graphify.workspace.leases import StaleLease
    from graphify.workspace.lifecycle_contracts import FencedLease
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    stores = runtime.stores
    original = runtime.adapter.build_structural
    successor = []
    def replace_fence(*args, **kwargs):
        with stores.registry.read_only_snapshot() as document:
            with stores.leases.workspace_lock(REPO_UUID):
                state = stores.leases.read_only_snapshot_locked(document, REPO_UUID)
                lease = state.leases["workspace"].to_dict()
                lease["liveness_deadline_monotonic_ns"] = lease["liveness_deadline_monotonic_ns"] - 3_600_000_000_000 + 1
                expired = FencedLease.from_mapping(lease)
                stores.leases._commit_state_locked(replace(state, revision=state.revision + 1, leases={"workspace": expired}))
        successor.append(stores.generations.acquire_staged_recovery(REPO_UUID, request.generation_id,
            request.build, attempt_sha256="b" * 64, acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(), ttl_ns=10**12))
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.adapter, "build_structural", replace_fence)
    with pytest.raises(StaleLease):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    payload = stores.generations.state.path(stores.generations._staging(REPO_UUID, request.generation_id)) / "graphify-out"
    assert list(payload.iterdir()) == []
    assert stores.pointers.load(REPO_UUID, allow_missing=True) is None
    stores.leases.release(successor[0].grant)


def test_incomplete_extraction_cannot_close_after_successor_fence(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from graphify.workspace.leases import StaleLease
    from graphify.workspace.lifecycle_contracts import FencedLease
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    (repo / "script.r").write_text("x <- 1\n")
    request = request_for(runtime)
    stores = runtime.stores
    original = runtime.adapter.build_structural
    successor = []
    def replace_fence(*args, **kwargs):
        with stores.registry.read_only_snapshot() as document:
            with stores.leases.workspace_lock(REPO_UUID):
                state = stores.leases.read_only_snapshot_locked(document, REPO_UUID)
                lease = state.leases["workspace"].to_dict()
                lease["liveness_deadline_monotonic_ns"] = (
                    lease["liveness_deadline_monotonic_ns"] - 3_600_000_000_000 + 1
                )
                expired = FencedLease.from_mapping(lease)
                stores.leases._commit_state_locked(
                    replace(state, revision=state.revision + 1, leases={"workspace": expired})
                )
        successor.append(stores.generations.acquire_staged_recovery(
            REPO_UUID, request.generation_id, request.build,
            attempt_sha256="b" * 64, acquired_at=datetime.now(timezone.utc),
            monotonic_ns=time.monotonic_ns(), ttl_ns=10**12,
        ))
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.adapter, "build_structural", replace_fence)
    with pytest.raises(StaleLease):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    staged = stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
    assert staged.lifecycle_state == "PUBLISHING"
    assert staged.abandonment_intent is None
    assert stores.pointers.load(REPO_UUID, allow_missing=True) is None
    stores.leases.release(successor[0].grant)


def test_watermark_change_blocks_certification(tmp_path, monkeypatch):
    from dataclasses import replace
    from graphify.workspace.semantic_queue import SemanticCertificationBlocked
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    queue = runtime.stores.queue
    original = queue.bind_sealed_inputs
    def advance(grant, **kwargs):
        result = original(grant, **kwargs)
        with runtime.stores.leases.current_operation(grant, monotonic_ns=kwargs["monotonic_ns"]):
            state = queue._load_locked(REPO_UUID)
            reconciliation = replace(state.reconciliation, desired_watermark=2)
            queue._commit_locked(state, replace(state, revision=state.revision + 1,
                desired_watermark=2, completed_watermark=2, reconciliation=reconciliation))
        return result
    monkeypatch.setattr(queue, "bind_sealed_inputs", advance)
    with pytest.raises(SemanticCertificationBlocked):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
