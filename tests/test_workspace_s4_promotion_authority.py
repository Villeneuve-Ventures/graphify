"""Runtime authority remains required at pointer intent and recovery writes."""
import pytest

from graphify.workspace.composition import WorkspaceAuthorityInvalid, WorkspaceRuntimeAuthority
from graphify.workspace.persistence import InjectedFault
from graphify.workspace.pointers import PointerRecoveryRequired
from graphify.workspace.sync import prepare_structural_sync, synchronize_structural
from tests.test_workspace_structural_s4 import request_for, runtime_fixture
from tests.workspace_s3_helpers import REPO_UUID


def rotate(runtime):
    authority = runtime.inputs.authority.to_dict()
    authority['structural_policy']['max_generations'] = 7
    (runtime.inputs.state_root / 'runtime-manifest.json').write_bytes(
        WorkspaceRuntimeAuthority.from_mapping(authority).canonical)


@pytest.mark.parametrize('boundary', ['before_persist', 'prior_durable', 'pending_durable'])
def test_authority_rotation_cannot_replace_prior_pointer(tmp_path, monkeypatch, boundary):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    synchronize_structural(runtime, request_for(runtime), attempt_sha256='a' * 64)
    stores = runtime.stores
    prior = stores.pointers.load(REPO_UUID).canonical
    request = prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id='gen-next',
        source_epoch=1, desired_watermark=2, expected_payload_bytes=1024 * 1024)
    reached = []
    original = stores.pointers._persist_move
    def persist(*args, **kwargs):
        if boundary == 'before_persist':
            reached.append(boundary)
            rotate(runtime)
        return original(*args, **kwargs)
    def fault(label):
        if label == f'pointer:promoted:{boundary}':
            reached.append(boundary)
            rotate(runtime)
    monkeypatch.setattr(stores.pointers, '_persist_move', persist)
    stores.pointers.fault_hook = fault
    with pytest.raises(WorkspaceAuthorityInvalid, match='authority changed'):
        synchronize_structural(runtime, request, attempt_sha256='b' * 64)
    assert reached
    assert (stores.pointers.state.root / stores.pointers._current(REPO_UUID)).read_bytes() == prior
    pending = stores.pointers.state.root / stores.pointers._pending(REPO_UUID)
    assert pending.exists() == (boundary == 'pending_durable')
    if pending.exists():
        with pytest.raises(PointerRecoveryRequired):
            stores.pointers.load(REPO_UUID)

    monkeypatch.setattr(stores.pointers, '_persist_move', original)
    stores.pointers.fault_hook = lambda label: None
    (runtime.inputs.state_root / 'runtime-manifest.json').write_bytes(runtime.inputs.authority.canonical)
    result = synchronize_structural(runtime, request, attempt_sha256='c' * 64)
    assert result.generation_id == 'gen-next'
    pointer = stores.pointers.load(REPO_UUID).canonical
    assert synchronize_structural(runtime, request, attempt_sha256='d' * 64) == result
    assert stores.pointers.load(REPO_UUID).canonical == pointer


def test_authority_rotation_during_pending_recovery_preserves_pending(tmp_path, monkeypatch):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    stores = runtime.stores
    request = request_for(runtime)
    def fault(label):
        if label == 'pointer:promoted:pending_durable':
            raise InjectedFault(label)
    stores.pointers.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    stores.pointers.fault_hook = lambda label: None
    pending_path = stores.pointers.state.root / stores.pointers._pending(REPO_UUID)
    pending = pending_path.read_bytes()
    original = stores.pointers.recover
    def recover(*args, **kwargs):
        rotate(runtime)
        return original(*args, **kwargs)
    monkeypatch.setattr(stores.pointers, 'recover', recover)
    with pytest.raises(WorkspaceAuthorityInvalid, match='authority changed'):
        synchronize_structural(runtime, request, attempt_sha256='b' * 64)
    assert not (stores.pointers.state.root / stores.pointers._current(REPO_UUID)).exists()
    with pytest.raises(PointerRecoveryRequired):
        stores.pointers.load(REPO_UUID, allow_missing=True)
    assert pending_path.read_bytes() == pending
    monkeypatch.setattr(stores.pointers, 'recover', original)
    (runtime.inputs.state_root / 'runtime-manifest.json').write_bytes(runtime.inputs.authority.canonical)
    result = synchronize_structural(runtime, request, attempt_sha256='c' * 64)
    assert result.generation_id == request.generation_id
    assert not pending_path.exists()
    assert synchronize_structural(runtime, request, attempt_sha256='d' * 64) == result
