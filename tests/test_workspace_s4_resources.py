"""Resource limits cover structural serialization and the full lease lifetime."""
import os
import threading
import time

import pytest

from graphify.workspace.adapters.base import PayloadBudgetExceeded
from graphify.workspace.generations import CapacityExceeded
from tests.test_workspace_adapter_v8 import adapter
from tests.workspace_s3_helpers import create_repo
from graphify.workspace import sync
from tests.test_workspace_structural_s4 import runtime_fixture, request_for
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


@pytest.mark.parametrize('chunk', ['x' * 65536, '\U0001f600' * 20000], ids=['ascii', 'utf8'])
def test_payload_writer_stops_at_durable_reservation(tmp_path, monkeypatch, chunk):
    from graphify import export

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    source = tree_snapshot(repo)
    original = export.write_json
    chunks = []
    def oversized(graph, communities, stream, **kwargs):
        for _ in range(32):
            stream.write(chunk)
            chunks.append(True)
    monkeypatch.setattr(export, 'write_json', oversized)
    with pytest.raises(CapacityExceeded):
        sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    payload = runtime.stores.generations.state.path(
        runtime.stores.generations._staging(REPO_UUID, request.generation_id)) / 'graphify-out'
    assert not payload.exists()
    assert len(chunks) < 16  # The manifest also consumes the 1 MiB reservation.
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
    assert tree_snapshot(repo) == source
    with runtime.stores.registry.read_only_snapshot():
        with runtime.stores.leases.read_only_workspace_lock(REPO_UUID):
            staged = runtime.stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
            assert staged.lifecycle_state == 'ABANDONED'
    assert not runtime.stores.leases.inspect(REPO_UUID).leases
    assert not any(t.name == 'graphify-structural-heartbeat' for t in threading.enumerate())
    monkeypatch.setattr(export, 'write_json', original)
    assert sync.synchronize_structural(runtime, request_for(runtime, 'gen-next'),
                                       attempt_sha256='b' * 64).pointer_revision == 1


@pytest.mark.parametrize('phase', ['initial', 'final', 'promotion'])
def test_slow_observation_keeps_acquired_lease_alive(tmp_path, monkeypatch, phase):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    original = sync._observe
    delayed = []
    monkeypatch.setattr(sync, '_LEASE_TTL_NS', 5_000_000_000)
    def observe(runtime, repo_uuid, manifest=None):
        state = runtime.stores.leases.inspect(repo_uuid)
        lease = state.leases.get('workspace')
        operation = None if lease is None else lease.to_dict()['operation']
        target = ((phase == 'initial' and operation == 'BUILD' and manifest is None)
                  or (phase == 'final' and operation == 'BUILD' and manifest is not None)
                  or (phase == 'promotion' and operation == 'PROMOTE' and manifest is not None))
        if target and not delayed:
            delayed.append(True)
            time.sleep(6)  # Exceeds the original lease lifetime; renewals must cover this scan.
        return original(runtime, repo_uuid, manifest)
    monkeypatch.setattr(sync, '_observe', observe)
    before = tree_snapshot(repo)
    assert sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64).pointer_revision == 1
    assert delayed
    assert not runtime.stores.leases.inspect(REPO_UUID).leases
    assert not any(t.name == 'graphify-structural-heartbeat' for t in threading.enumerate())
    assert tree_snapshot(repo) == before


@pytest.mark.parametrize('budget_kind', ['exact', 'short', 'manifest'])
def test_adapter_budget_includes_manifest_before_any_write(tmp_path, budget_kind):
    repo = create_repo(tmp_path.resolve() / 'repo')
    (repo / 'main.py').write_text('def leaf(): return 1\n')
    engine = adapter()
    initial = engine.observe(repo).initial_detection
    baseline = tmp_path / 'baseline'
    baseline.mkdir(mode=0o700)
    fd = os.open(baseline, os.O_RDONLY | os.O_DIRECTORY)
    try:
        built = engine.build_structural(repo, payload_fd=fd, scratch_fd=fd, initial_detection=initial)
    finally:
        os.close(fd)
    total = sum(p.stat().st_size for p in baseline.iterdir())
    budget = total if budget_kind == 'exact' else total - 1 if budget_kind == 'short' else len(built.input_manifest.canonical)
    output = tmp_path / 'bounded'
    output.mkdir(mode=0o700)
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if budget_kind == 'exact':
            engine.build_structural(repo, payload_fd=fd, scratch_fd=fd,
                                    initial_detection=initial, max_payload_bytes=budget)
            assert sum(p.stat().st_size for p in output.iterdir()) == budget
        else:
            with pytest.raises(PayloadBudgetExceeded) as raised:
                engine.build_structural(repo, payload_fd=fd, scratch_fd=fd,
                                        initial_detection=initial, max_payload_bytes=budget)
            assert raised.value.required_bytes > budget
            assert not list(output.iterdir())
    finally:
        os.close(fd)


def test_bounded_capacity_failure_recovers_durable_abandonment(tmp_path, monkeypatch):
    from graphify import export
    from graphify.workspace.generations import GenerationConflict
    from graphify.workspace.persistence import InjectedFault

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    original = export.write_json
    def oversized(graph, communities, stream, **kwargs):
        stream.write('x' * request.build.expected_payload_bytes)
    def fault(label):
        if label.endswith(':abandon_intent_durable'):
            raise InjectedFault(label)
    monkeypatch.setattr(export, 'write_json', oversized)
    runtime.stores.generations.fault_hook = fault
    before = tree_snapshot(repo)
    with pytest.raises(InjectedFault):
        sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    runtime.stores.generations.fault_hook = lambda label: None
    with pytest.raises(GenerationConflict, match='abandoned'):
        sync.synchronize_structural(runtime, request, attempt_sha256='b' * 64)
    monkeypatch.setattr(export, 'write_json', original)
    assert sync.synchronize_structural(runtime, request_for(runtime, 'gen-next'),
                                       attempt_sha256='c' * 64).pointer_revision == 1
    assert tree_snapshot(repo) == before
