"""Certified recovery uses positive drift evidence, never a failed read alone."""
import pytest

from graphify.workspace.composition import compose_workspace_runtime
from graphify.workspace.generations import GenerationConflict
from graphify.workspace.lifecycle_contracts import StagedBuildState
from graphify.workspace.persistence import InjectedFault
from graphify.workspace.sync import _manifest, prepare_structural_sync, synchronize_structural
from tests.test_workspace_structural_s4 import request_for, runtime_fixture
from tests.workspace_s3_helpers import REPO_UUID


def certified(tmp_path, monkeypatch):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    (repo / 'app.ts').write_text("import {value} from '@lib'; console.log(value);")
    (repo / 'lib.ts').write_text('export const value = 1;')
    (repo / 'tsconfig.json').write_text('{"extends":"./.config/base.json"}')
    (repo / '.config').mkdir()
    support = repo / '.config/base.json'
    support.write_text('{"compilerOptions":{"paths":{"@lib":["../lib.ts"]}}}')
    (repo / '.graphifyignore').write_text('tsconfig.json\n.config/\n')
    request = request_for(runtime)
    def fault(label):
        if label.endswith(':generation_certified'):
            raise InjectedFault(label)
    runtime.stores.generations.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    runtime.stores.generations.fault_hook = lambda label: None
    manifest = _manifest(runtime.stores, request, certified=True)
    assert any(e['operation'] == 'read' and e['path'] == '.config/base.json'
               for e in manifest.to_dict()['evidence'])
    return runtime, repo, support, request, manifest


@pytest.mark.parametrize('change', ['detected', 'support_bytes', 'support_missing',
                                    'support_directory', 'support_ancestor_file'])
def test_certified_drift_abandons_and_allows_successor(tmp_path, monkeypatch, change):
    runtime, repo, support, request, _manifest_value = certified(tmp_path, monkeypatch)
    if change == 'detected':
        (repo / 'README.md').write_text('changed after certification\n')
    elif change == 'support_missing':
        support.unlink()
    elif change == 'support_directory':
        support.unlink()
        support.mkdir()
    elif change == 'support_ancestor_file':
        support.unlink()
        support.parent.rmdir()
        support.parent.write_text('replacement')
    else:
        support.write_text('{"compilerOptions":{}}')
    if change != 'detected':
        changed_detection = runtime.adapter.observe(repo).initial_detection.sha256
        if change == 'support_ancestor_file':
            assert changed_detection != request.build.observation_manifest_sha256
        else:
            assert changed_detection == request.build.observation_manifest_sha256
    with pytest.raises(GenerationConflict, match='abandoned'):
        synchronize_structural(runtime, request, attempt_sha256='b' * 64)
    stage = runtime.stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)
    assert stage.lifecycle_state == 'ABANDONED'
    assert stage.abandon_reason == 'SOURCE_CHANGED'
    assert StagedBuildState.from_json(stage.canonical) == stage
    assert not runtime.stores.leases.inspect(REPO_UUID).leases
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
    runtime = compose_workspace_runtime(runtime.inputs).require_runtime()
    with pytest.raises(GenerationConflict, match='abandoned'):
        synchronize_structural(runtime, request, attempt_sha256='c' * 64)
    if support.parent.is_file():
        support.parent.unlink()
        support.parent.mkdir()
    if support.is_dir():
        support.rmdir()
    support.write_text('{"compilerOptions":{}}')
    successor = prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id='gen-successor',
        source_epoch=2, desired_watermark=2, expected_payload_bytes=1024 * 1024)
    assert synchronize_structural(runtime, successor, attempt_sha256='d' * 64).pointer_revision == 1
