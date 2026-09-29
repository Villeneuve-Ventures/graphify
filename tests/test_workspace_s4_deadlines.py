"""Unsafe HEAD preflight and cancellable certified graph computation."""
import os
import subprocess
import time

import pytest

from graphify.workspace.adapters import v8
from graphify.workspace import _readonly
from graphify.workspace.adapters.base import QueryRequest
from graphify.workspace.identity import SourceDiscoveryError, discover_source
from graphify.workspace.persistence import LockTimeout
from tests.workspace_s3_helpers import create_repo, git_output


@pytest.mark.parametrize('linked', [False, True])
@pytest.mark.parametrize('kind', ['fifo', 'directory', 'symlink', 'oversized'])
def test_unsafe_head_refused_before_git(tmp_path, monkeypatch, linked, kind):
    repo = create_repo(tmp_path.resolve() / 'repo')
    source, git_dir = repo, repo / '.git'
    if linked:
        source = tmp_path.resolve() / 'linked'
        git_output(repo, 'worktree', 'add', '--detach', str(source), 'HEAD')
        git_dir = repo / '.git/worktrees/linked'
    head = git_dir / 'HEAD'
    original = head.read_bytes()
    head.unlink()
    if kind == 'fifo':
        os.mkfifo(head)
    elif kind == 'directory':
        head.mkdir()
    elif kind == 'oversized':
        head.write_bytes(b'x' * (1024 * 1024 + 1))
    else:
        outside = tmp_path / 'external-head'
        outside.write_bytes(original)
        head.symlink_to(outside)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('Git started with unsafe HEAD'))
    with pytest.raises(SourceDiscoveryError):
        discover_source(source)


def test_query_adapter_rejects_expired_deadline_before_payload_read(monkeypatch):
    monkeypatch.setattr(v8, 'read_payload_file', lambda *a, **kw: pytest.fail('payload read after deadline'))
    with pytest.raises(LockTimeout):
        v8.V8Adapter().query_structural(-1, QueryRequest('caller'), deadline_ns=time.monotonic_ns() - 1)


@pytest.fixture
def graph_payload(tmp_path):
    import json
    payload = tmp_path / 'payload'
    payload.mkdir(mode=0o700)
    graph = payload / 'graph.json'
    graph.write_text(json.dumps({'directed': True, 'multigraph': False,
        'graph': {}, 'nodes': [{'id': 'a', 'label': 'caller'}, {'id': 'b', 'label': 'leaf'}],
        'links': [{'source': 'a', 'target': 'b', 'relation': 'calls'}]}))
    graph.chmod(0o600)
    fd = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def slow_child(monkeypatch, phase):
    # Instrument only the disposable child's selected engine phase. The parent
    # retains its real monotonic clock and deadline enforcement.
    modules = {'read': ('v8', 'read_payload_file'), 'decode': ('v8.json', 'loads'),
               'graph': ('json_graph', 'node_link_graph'),
               'tokenizer': ('serve', 'memory_query_segmenter'),
               'ranking': ('serve', '_score_query'), 'traversal': ('serve', '_bfs')}
    module, name = modules[phase]
    script = f'''import time, os
from graphify.workspace.adapters import v8
from graphify import serve
from networkx.readwrite import json_graph
original = {module}.{name}
def slow(*args, **kwargs):
    # Let the request envelope decode before stalling graph JSON decoding.
    if {phase!r} == 'decode' and b'"nodes"' not in args[0]:
        return original(*args, **kwargs)
    os.write(2, b'PHASE-ENTERED')
    os.write(1, b'partial output must be withheld')
    time.sleep(60)
    return original(*args, **kwargs)
{module}.{name} = slow
v8._query_child()
'''
    monkeypatch.setattr(v8, '_QUERY_CHILD_CODE', script)
    processes, diagnostics = [], bytearray()
    original_popen, original_read = subprocess.Popen, os.read
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if script in args[0]:
            processes.append(process)
        return process
    def read(fd, size):
        chunk = original_read(fd, size)
        if processes and not processes[0].stderr.closed and fd == processes[0].stderr.fileno():
            diagnostics.extend(chunk)
        return chunk
    monkeypatch.setattr(_readonly.subprocess, 'Popen', popen)
    monkeypatch.setattr(v8.os, 'read', read)
    return processes, diagnostics


@pytest.mark.parametrize('phase', ['read', 'decode', 'graph', 'tokenizer', 'ranking', 'traversal'])
def test_deadline_kills_and_reaps_each_query_phase(graph_payload, monkeypatch, phase):
    processes, diagnostics = slow_child(monkeypatch, phase)
    started = time.monotonic()
    with pytest.raises(LockTimeout, match='deadline'):
        v8.V8Adapter().query_structural(graph_payload, QueryRequest('caller'),
            deadline_ns=time.monotonic_ns() + 2_000_000_000)
    assert time.monotonic() - started < 5
    assert b'PHASE-ENTERED' in diagnostics
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].returncode < 0


@pytest.mark.parametrize('question', ['caller', 'caller' + '\v' * 4000 + 'leaf'])
def test_deadline_query_matches_in_process_engine(graph_payload, question):
    adapter, request = v8.V8Adapter(), QueryRequest(question)
    expected = adapter.query_structural(graph_payload, request)
    assert adapter.query_structural(graph_payload, request,
        deadline_ns=time.monotonic_ns() + 10_000_000_000) == expected


def test_query_expiry_releases_workspace_and_generation_locks(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from graphify.workspace.query import query_structural
    from graphify.workspace.sync import synchronize_structural
    from tests.test_workspace_structural_s4 import runtime_fixture, request_for
    from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    result = synchronize_structural(runtime, request_for(runtime), attempt_sha256='a' * 64)
    before = tree_snapshot(repo), tree_snapshot(runtime.inputs.state_root)
    observations = []
    original = runtime.adapter.observe_lifecycle
    def observe(*args, **kwargs):
        observations.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.adapter, 'observe_lifecycle', observe)
    processes, diagnostics = slow_child(monkeypatch, 'traversal')
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(query_structural, runtime, REPO_UUID, QueryRequest('caller'),
                             deadline_ns=time.monotonic_ns() + 8_000_000_000)
        with pytest.raises(LockTimeout, match='deadline'):
            future.result(timeout=12)
    assert time.monotonic() - started < 12
    assert b'PHASE-ENTERED' in diagnostics and len(processes) == 1 and processes[0].poll() is not None
    assert observations == [True]  # No second freshness scan after cancellation.
    with runtime.stores.leases.read_only_workspace_lock(REPO_UUID, deadline_ns=time.monotonic_ns() + 500_000_000):
        with runtime.stores.generations.state.existing_generation_lock(
            runtime.stores.generations._lock(REPO_UUID, result.generation_id),
            generation_id=result.generation_id, exclusive=True,
            deadline_ns=time.monotonic_ns() + 500_000_000):
            pass
    assert (tree_snapshot(repo), tree_snapshot(runtime.inputs.state_root)) == before


def test_child_refuses_filesystem_writes_and_ambient_tokenizer_cache(graph_payload, tmp_path, monkeypatch):
    from graphify.workspace.adapters.base import QueryRejected
    protected = tmp_path / 'protected.txt'
    protected.write_text('original')
    cache = tmp_path / 'jieba.cache'
    cache.write_bytes(b'untrusted cache')
    for expression in (f'open({str(protected)!r}, "w").write("changed")', f'open({str(cache)!r}, "rb").read()'):
        monkeypatch.setattr(v8, '_QUERY_CHILD_CODE', f'''from graphify.workspace.adapters import v8
v8.V8Adapter.query_structural = lambda *args, **kwargs: {expression}
v8._query_child()
''')
        with pytest.raises(QueryRejected, match='computation failed'):
            v8.V8Adapter().query_structural(graph_payload, QueryRequest('caller'),
                deadline_ns=time.monotonic_ns() + 5_000_000_000)
        assert protected.read_text() == 'original' and cache.read_bytes() == b'untrusted cache'


def test_child_output_limit_kills_computation(graph_payload, monkeypatch):
    from graphify.workspace.adapters.base import QueryRejected
    processes = []
    original = subprocess.Popen
    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        processes.append(child)
        return child
    monkeypatch.setattr(_readonly.subprocess, 'Popen', popen)
    monkeypatch.setattr(v8, 'MAX_TOTAL_BYTES', 128)
    monkeypatch.setattr(v8, '_QUERY_CHILD_CODE', 'import os,time; os.write(1, b"x" * 129); time.sleep(60)')
    with pytest.raises(QueryRejected, match='output exceeds'):
        v8.V8Adapter().query_structural(graph_payload, QueryRequest('caller'),
            deadline_ns=time.monotonic_ns() + 5_000_000_000)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_query_ties_match_across_process_hash_seeds():
    import json
    script = '''import json
import networkx as nx
from graphify.serve import _query_graph_text
G = nx.DiGraph()
G.add_nodes_from((n, {'label': 'match ' + n}) for n in ('d', 'b', 'c', 'a'))
G.add_edges_from([('a', 'c'), ('b', 'd'), ('c', 'd'), ('d', 'a')])
print(json.dumps([_query_graph_text(G, 'match', mode=mode, token_budget=budget)
                  for mode in ('bfs', 'dfs') for budget in (20, 2000)]))
'''
    outputs = []
    for seed in ('1', '2', '3'):
        result = subprocess.run([v8.sys.executable, '-B', '-c', script],
            env={**os.environ, 'PYTHONHASHSEED': seed}, capture_output=True, text=True,
            check=True, timeout=10)
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1] == outputs[2]
