"""PR feedback: admission, receipt capacity, retained leases and Git includes."""
import os
import subprocess
import time

import pytest

from graphify.workspace import sync
from graphify.workspace.generations import CapacityExceeded
from graphify.workspace.identity import SourceDiscoveryError, discover_source
from tests.test_workspace_structural_s4 import runtime_fixture, request_for
from tests.workspace_s3_helpers import REPO_UUID, create_repo, tree_snapshot


def test_receipt_headroom_allows_certification_and_bounds_maximal_receipt(tmp_path, monkeypatch):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    original = runtime.adapter.build_structural
    def fill_reservation(root, **kwargs):
        built = original(root, **kwargs)
        fd = kwargs['payload_fd']
        # Valid JSON whitespace makes payload alone fit exactly, leaving no receipt room.
        used = sum(os.stat(name, dir_fd=fd).st_size for name in os.listdir(fd))
        graph = os.open('graph.json', os.O_WRONLY | os.O_APPEND, dir_fd=fd)
        try:
            os.write(graph, b' ' * (kwargs['max_payload_bytes'] - used))
        finally:
            os.close(graph)
        return built
    monkeypatch.setattr(runtime.adapter, 'build_structural', fill_reservation)
    before = tree_snapshot(repo)
    result = sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    assert result.pointer_revision == 1
    receipt = runtime.stores.generations.verify_generation(REPO_UUID, request.generation_id)
    assert sum(e['size'] for e in receipt.to_dict()['sealed_query_payload']['entries']) + len(receipt.canonical) <= request.build.expected_payload_bytes
    assert tree_snapshot(repo) == before
    # Maximal admitted counters, generation/lock names, and entry sizes bound
    # every variable-width field; hashes and structural proof names are fixed.
    from graphify.workspace.lifecycle_contracts import GenerationReceipt, payload_manifest_sha256
    maximal = receipt.to_dict()
    maximal['generation_id'] = 'gen-' + 'z' * 63
    maximal['coordination_lock_id'] = 'generation:' + 'z' * 63
    for key in ('source_epoch', 'active_source_revision', 'operation_epoch', 'fence_token', 'queue_watermark'):
        maximal[key] = 2**63 - 1
    payload = maximal['sealed_query_payload']
    for entry in payload['entries']:
        entry['size'] = 2**63 - 1
    payload['manifest_sha256'] = payload_manifest_sha256('graphify-out', payload['entries'])
    assert len(GenerationReceipt.from_mapping(maximal).canonical) <= sync._RECEIPT_HEADROOM_BYTES
    from dataclasses import replace
    from graphify.workspace.lifecycle_contracts import StructuralBuildRequest
    large = 10**1000
    value = request.build.to_dict()
    value.update(source_epoch=large, expected_active_source_revision=large)
    value['logical_request_sha256'] = sync.StructuralSyncRequest.identity(REPO_UUID, request.generation_id, 1, value)
    large_request = replace(request, build=StructuralBuildRequest.from_mapping(value))
    for key in ('source_epoch', 'active_source_revision', 'operation_epoch', 'fence_token'):
        maximal[key] = large
    assert len(GenerationReceipt.from_mapping(maximal).canonical) <= sync._receipt_headroom(large_request, large, large)


@pytest.mark.parametrize('boundary', ['prepare', 'execute'])
def test_adapter_ceiling_rejected_without_staging(tmp_path, monkeypatch, boundary):
    from graphify.workspace.adapters import v8
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    # Scale the independent engine limit down; the configured reservation stays
    # valid under the real runtime policy, exposing the original ceiling gap.
    monkeypatch.setattr(v8, 'MAX_TOTAL_BYTES', 16384)
    monkeypatch.setattr(sync, 'MAX_TOTAL_BYTES', 16384, raising=False)
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    with pytest.raises(CapacityExceeded, match='adapter'):
        if boundary == 'prepare':
            request_for(runtime)
        else:
            sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


def test_receipt_headroom_overflow_abandons_and_allows_next_request(tmp_path, monkeypatch):
    from graphify import export
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    original = export.write_json
    def near_limit(graph, communities, stream, **kwargs):
        # Graph + manifest fit the original reservation, but not with receipt room.
        stream.write(' ' * (stream.max_bytes + 1))
    monkeypatch.setattr(export, 'write_json', near_limit)
    before = tree_snapshot(repo)
    with pytest.raises(CapacityExceeded):
        sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    with runtime.stores.registry.read_only_snapshot():
        with runtime.stores.leases.read_only_workspace_lock(REPO_UUID):
            staged = runtime.stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
    assert staged.lifecycle_state == 'ABANDONED'
    assert not runtime.stores.leases.inspect(REPO_UUID).leases
    monkeypatch.setattr(export, 'write_json', original)
    assert sync.synchronize_structural(runtime, request_for(runtime, 'gen-next'), attempt_sha256='b' * 64).pointer_revision == 1
    assert tree_snapshot(repo) == before


def test_git_include_with_bom_is_refused_before_git(tmp_path, monkeypatch):
    repo = create_repo(tmp_path.resolve() / 'repo')
    config = repo / '.git/config'
    config.write_bytes(b'\xef\xbb\xbf[include]\npath = /unread-external-config\n' + config.read_bytes())
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('Git read a BOM-prefixed include'))
    with pytest.raises(SourceDiscoveryError, match='include'):
        discover_source(repo)


@pytest.mark.parametrize('boundary', ['prepare', 'execute'])
def test_insufficient_receipt_allowance_rejected_before_staging(tmp_path, monkeypatch, boundary):
    from dataclasses import replace
    from graphify.workspace.lifecycle_contracts import StructuralBuildRequest
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    valid = request_for(runtime)
    value = valid.build.to_dict()
    value['expected_payload_bytes'] = 4096
    value['logical_request_sha256'] = sync.StructuralSyncRequest.identity(REPO_UUID, valid.generation_id, 1, value)
    small = replace(valid, build=StructuralBuildRequest.from_mapping(value))
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    with pytest.raises(CapacityExceeded, match='receipt'):
        if boundary == 'prepare':
            sync.prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id='gen-small',
                source_epoch=1, desired_watermark=1, expected_payload_bytes=4096)
        else:
            sync.synchronize_structural(runtime, small, attempt_sha256='a' * 64)
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


def test_retained_lease_is_renewed_on_heartbeat_entry(tmp_path, monkeypatch):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    _, observations = sync._observe(runtime, REPO_UUID)
    runtime.stores.generations.request_staged_build(REPO_UUID, request.generation_id, request.build, source_observations=observations)
    monkeypatch.setattr(sync, '_LEASE_TTL_NS', 30_000_000_000)
    attempt = runtime.stores.generations.acquire_staged_operation(REPO_UUID, request.generation_id, request.build,
        attempt_sha256='a' * 64, operation='BUILD', acquired_at=sync._now(),
        monotonic_ns=time.monotonic_ns(), ttl_ns=1_000_000_000)
    retained = sync._acquire(runtime, request, 'a' * 64, recovering=True)
    assert retained.grant.lease.to_dict() == attempt.grant.lease.to_dict()
    with sync._released(runtime, retained.grant), sync._heartbeat(runtime, retained.grant):
        time.sleep(1.2)
        runtime.stores.leases.assert_current(retained.grant, monotonic_ns=time.monotonic_ns())
    assert not runtime.stores.leases.inspect(REPO_UUID).leases


@pytest.mark.parametrize('section', ['include', 'includeIf "gitdir:**"'])
@pytest.mark.parametrize('routing', ['main', 'linked', 'worktree_config'])
def test_git_include_refused_before_any_discovery_subprocess(tmp_path, monkeypatch, section, routing):
    from tests.workspace_s3_helpers import git_output
    repo = create_repo(tmp_path.resolve() / 'repo')
    config = repo / '.git/config'
    if routing != 'main':
        linked = tmp_path.resolve() / 'linked'
        git_output(repo, 'worktree', 'add', '--detach', str(linked), 'HEAD')
        if routing == 'worktree_config':
            config = repo / '.git/worktrees/linked/config.worktree'
        repo = linked
    outside = tmp_path / 'outside-fifo'
    os.mkfifo(outside)
    with config.open('a') as stream:
        stream.write(f'\n[{section}]\n\tpath = {outside}\n')
    before = tree_snapshot(repo)
    def forbidden(*args, **kwargs):
        pytest.fail('Git started before rejecting external include authority')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(SourceDiscoveryError, match='include'):
        discover_source(repo)
    assert tree_snapshot(repo) == before
