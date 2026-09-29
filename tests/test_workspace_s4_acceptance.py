"""Regressions from the integrated S4 acceptance review."""
import shutil
import subprocess
import time
import os

import pytest

from graphify.workspace import sync
from graphify.workspace.composition import (
    WorkspaceAuthorityInvalid, WorkspaceRuntimeAuthority,
    compose_workspace_runtime, load_workspace_runtime_inputs,
)
from graphify.workspace.identity import SourceDiscoveryError, discover_source
from tests.test_workspace_structural_s4 import runtime_fixture, request_for
from tests.workspace_s3_helpers import REPO_UUID, create_repo, git_output, tree_snapshot


@pytest.mark.parametrize('boundary', ['sync', 'store'])
def test_authority_change_during_observation_refuses_before_staging(tmp_path, monkeypatch, boundary):
    runtime, _repo = runtime_fixture(tmp_path, monkeypatch)
    request = request_for(runtime)
    owner, name = (sync, '_observe') if boundary == 'sync' else (
        runtime.stores.generations, '_trusted_structural_observations')
    original = getattr(owner, name)
    changed = []

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            authority = runtime.inputs.authority.to_dict()
            authority['structural_policy']['max_generations'] = 7
            (runtime.inputs.state_root / 'runtime-manifest.json').write_bytes(
                WorkspaceRuntimeAuthority.from_mapping(authority).canonical)
            changed.append(tree_snapshot(runtime.inputs.state_root))
        return result

    monkeypatch.setattr(owner, name, observe)
    with pytest.raises(WorkspaceAuthorityInvalid, match='authority changed'):
        sync.synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    assert tree_snapshot(runtime.inputs.state_root) == changed[0]
    monkeypatch.setattr(owner, name, original)
    fresh = compose_workspace_runtime(load_workspace_runtime_inputs(
        state_root=runtime.inputs.state_root, expected=runtime.inputs.expected)).require_runtime()
    result = sync.synchronize_structural(fresh, request_for(fresh, 'gen-fresh'),
                                        attempt_sha256='b' * 64)
    assert result.pointer_revision == 1


@pytest.mark.parametrize('linked', [False, True])
@pytest.mark.parametrize('ancestor', ['objects', 'objects/info', 'objects/pack'])
@pytest.mark.parametrize('kind', ['symlink', 'file', 'fifo'])
def test_unsafe_git_object_ancestry_refuses_before_git(tmp_path, monkeypatch, linked, ancestor, kind):
    root = tmp_path.resolve()
    repo = create_repo(root / 'repo')
    source = repo
    if linked:
        source = root / 'linked'
        git_output(repo, 'worktree', 'add', '--detach', str(source), 'HEAD')
    path = repo / '.git' / ancestor
    outside = root / 'external-objects'
    shutil.move(path, outside)
    if kind == 'symlink':
        path.symlink_to(outside, target_is_directory=True)
    elif kind == 'file':
        path.write_bytes(b'not a directory')
    else:
        os.mkfifo(path)
    before = tree_snapshot(repo), tree_snapshot(outside)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Git started with unsafe object ancestry'))
    with pytest.raises(SourceDiscoveryError):
        discover_source(source)
    assert (tree_snapshot(repo), tree_snapshot(outside)) == before


def test_missing_optional_git_info_directory_remains_supported(tmp_path):
    repo = create_repo(tmp_path.resolve() / 'repo')
    (repo / '.git/objects/info').rmdir()
    assert discover_source(repo).repo_uuid == REPO_UUID


def test_symlinked_loose_git_object_refuses_before_git(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    repo = create_repo(root / 'repo')
    obj = next(path for path in (repo / '.git/objects').glob('??/*') if path.is_file())
    outside = root / 'external-object'
    shutil.move(obj, outside)
    obj.symlink_to(outside)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Git read an external object'))
    with pytest.raises(SourceDiscoveryError):
        discover_source(repo)


def test_observation_deadline_stops_detection(tmp_path, monkeypatch):
    from graphify.workspace.adapters import v8
    from graphify.workspace.persistence import LockTimeout
    import graphify.detect as detect
    repo = create_repo(tmp_path.resolve() / 'repo')
    original = detect.detect
    def slow(*args, **kwargs):
        time.sleep(8)
        return original(*args, **kwargs)
    monkeypatch.setattr(detect, 'detect', slow)
    # The same stall in the disposable interpreter, once that boundary exists.
    script = '''import time
from graphify.workspace.adapters import v8
import graphify.detect as detect
original = detect.detect
def slow(*args, **kwargs):
    time.sleep(8)
    return original(*args, **kwargs)
detect.detect = slow
v8._observation_child()
'''
    monkeypatch.setattr(v8, '_OBSERVATION_CHILD_CODE', script, raising=False)
    started = time.monotonic()
    with pytest.raises(LockTimeout, match='deadline'):
        v8.V8Adapter().observe_lifecycle(repo, deadline_ns=time.monotonic_ns() + 3_000_000_000)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize('phase', ['detection', 'replay', 'authority'])
@pytest.mark.parametrize('occurrence', [1, 2])
def test_query_deadline_releases_locks_in_every_read_phase(tmp_path, monkeypatch, phase, occurrence):
    from concurrent.futures import ThreadPoolExecutor
    from graphify.workspace import _readonly, composition
    from graphify.workspace.adapters import v8
    from graphify.workspace.adapters.base import QueryRequest
    from graphify.workspace.persistence import LockTimeout
    from graphify.workspace.query import query_structural

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    result = sync.synchronize_structural(runtime, request_for(runtime), attempt_sha256='a' * 64)
    before = tree_snapshot(repo), tree_snapshot(runtime.inputs.state_root)
    target = {'detection': 'detect.detect', 'replay': 'v8._replay',
              'authority': 'composition.load_workspace_runtime_inputs'}[phase]
    entry = 'composition._authority_child()' if phase == 'authority' else 'v8._observation_child()'
    script = f'''import os,time
from graphify.workspace import composition
from graphify.workspace.adapters import v8
import graphify.detect as detect
original = {target}
def stalled(*args, **kwargs):
    os.write(2, b'PHASE-ENTERED')
    os.write(1, b'partial output must not escape')
    time.sleep(60)
    return original(*args, **kwargs)
{target} = stalled
{entry}
'''
    selected = 0
    processes, diagnostics = [], bytearray()
    original_popen, original_read = subprocess.Popen, os.read
    def popen(command, *args, **kwargs):
        nonlocal selected
        wanted = composition._AUTHORITY_CHILD_CODE if phase == 'authority' else v8._OBSERVATION_CHILD_CODE
        command = list(command)
        if wanted in command:
            selected += 1
            if selected == occurrence:
                command[command.index(wanted)] = script
        process = original_popen(command, *args, **kwargs)
        if script in command:
            processes.append(process)
        return process
    def read(fd, size):
        chunk = original_read(fd, size)
        if processes and not processes[0].stderr.closed and fd == processes[0].stderr.fileno():
            diagnostics.extend(chunk)
        return chunk
    monkeypatch.setattr(_readonly.subprocess, 'Popen', popen)
    monkeypatch.setattr(_readonly.os, 'read', read)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(query_structural, runtime, REPO_UUID, QueryRequest('caller'),
                             deadline_ns=time.monotonic_ns() + 6_000_000_000)
        with pytest.raises(LockTimeout, match='deadline'):
            future.result(timeout=10)
    assert time.monotonic() - started < 10
    assert b'PHASE-ENTERED' in diagnostics
    assert len(processes) == 1 and processes[0].poll() is not None
    with runtime.stores.leases.read_only_workspace_lock(REPO_UUID,
            deadline_ns=time.monotonic_ns() + 1_000_000_000):
        with runtime.stores.generations.state.existing_generation_lock(
                runtime.stores.generations._lock(REPO_UUID, result.generation_id),
                generation_id=result.generation_id, exclusive=True,
                deadline_ns=time.monotonic_ns() + 1_000_000_000):
            pass
    assert (tree_snapshot(repo), tree_snapshot(runtime.inputs.state_root)) == before


def test_worker_audit_precedes_candidate_import(tmp_path, monkeypatch):
    from graphify.workspace._readonly import ReadOnlyFailure, run_readonly
    module_root = tmp_path.resolve()
    target = module_root / 'unexpected-output'
    (module_root / 'startup_probe.py').write_text(f'open({str(target)!r}, "w").write("bad")\n')
    monkeypatch.syspath_prepend(str(module_root))
    with pytest.raises(ReadOnlyFailure, match='computation failed'):
        run_readonly('import startup_probe', b'', deadline_ns=time.monotonic_ns() + 5_000_000_000,
                     max_input_bytes=1024, max_output_bytes=65536)
    assert not target.exists()


def test_deadline_terminates_worker_helper_group(monkeypatch):
    from graphify.workspace import _readonly
    from graphify.workspace.persistence import LockTimeout
    script = r'''import subprocess,sys
subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
               "import os,time; os.write(2, ('HELPER:%d\\n' % os.getpid()).encode()); time.sleep(60)"], check=True)
'''
    diagnostics = bytearray()
    processes = []
    original_popen, original_read = subprocess.Popen, os.read
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    def read(fd, size):
        value = original_read(fd, size)
        if processes and not processes[0].stderr.closed and fd == processes[0].stderr.fileno():
            diagnostics.extend(value)
        return value
    monkeypatch.setattr(_readonly.subprocess, 'Popen', popen)
    monkeypatch.setattr(_readonly.os, 'read', read)
    with pytest.raises(LockTimeout):
        _readonly.run_readonly(script, b'', deadline_ns=time.monotonic_ns() + 2_000_000_000,
                               max_input_bytes=1024, max_output_bytes=65536)
    assert processes[0].poll() is not None
    helper = int(bytes(diagnostics).split(b'HELPER:')[1].splitlines()[0])
    # A killed orphan can briefly remain as an OS-owned zombie; it cannot run
    # or retain descriptors. Wait only for normal OS reaping, never signal an
    # unowned/reused PID from the test process.
    for _ in range(100):
        try:
            os.kill(helper, 0)
        except ProcessLookupError:
            break
        time.sleep(.01)
    else:
        pytest.fail('read-only helper survived deadline cleanup')
