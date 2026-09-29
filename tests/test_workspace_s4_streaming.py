"""Preflight and streaming admission regressions from PR #167."""
import io
import json
import os
import subprocess
from types import SimpleNamespace

import networkx as nx
from networkx.readwrite import json_graph
import pytest

from graphify.export import write_json
from graphify.workspace.adapters.base import QueryRejected, QueryRequest
from graphify.workspace.identity import SourceDiscoveryError, discover_source
from graphify.workspace.query import query_structural
from tests.workspace_s3_helpers import REPO_UUID, create_repo, git_output


@pytest.mark.parametrize('name', ['alternates', 'http-alternates'])
@pytest.mark.parametrize('kind', ['file', 'fifo', 'symlink'])
@pytest.mark.parametrize('linked', [False, True])
def test_alternate_stores_refused_before_git(tmp_path, monkeypatch, name, kind, linked):
    repo = create_repo(tmp_path.resolve() / 'repo')
    source = repo
    if linked:
        source = tmp_path.resolve() / 'linked'
        git_output(repo, 'worktree', 'add', '--detach', str(source), 'HEAD')
    path = repo / '.git/objects/info' / name
    if kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'symlink':
        path.symlink_to(tmp_path / 'external')
    else:
        path.write_text('/external/objects\n')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('Git started before alternates rejection'))
    with pytest.raises(SourceDiscoveryError):
        discover_source(source)


@pytest.mark.parametrize('graph_type', [nx.Graph, nx.DiGraph, nx.MultiGraph, nx.MultiDiGraph])
@pytest.mark.parametrize('empty', [False, True])
def test_streamed_graph_matches_node_link_json(graph_type, empty):
    graph = graph_type()
    if not empty:
        graph.add_node('a', id='ignored', label='Café')
        graph.add_node('b', label='Leaf')
        graph.add_edge('a', 'b', source='ignored', target='ignored',
                       _src='b', _tgt='a', confidence='INFERRED')
        if graph.is_multigraph():
            graph.add_edge('a', 'b', key='second', confidence_score=0.7)
    graph.graph['hyperedges'] = [{'nodes': ['a', 'b']}]
    expected = json_graph.node_link_data(graph, edges='links')
    for node in expected['nodes']:
        node.update(community=0, community_name='Example', norm_label='cafe' if node['id'] == 'a' else 'leaf')
    for edge in expected['links']:
        edge.setdefault('confidence_score', 0.5 if edge.get('confidence') == 'INFERRED' else 1.0)
        src, tgt = edge.pop('_src', None), edge.pop('_tgt', None)
        if src is not None and tgt is not None:
            edge.update(source=src, target=tgt)
    expected.update(hyperedges=graph.graph['hyperedges'], built_at_commit='abc')
    before = json_graph.node_link_data(graph, edges='links')
    stream = io.StringIO()
    write_json(graph, {0: ['a', 'b']}, stream, built_at_commit='abc', community_labels={0: 'Example'})
    assert stream.getvalue() == json.dumps(expected, indent=2)
    assert json_graph.node_link_data(graph, edges='links') == before


def test_budget_interrupts_node_serialization_before_whole_graph_copy(monkeypatch):
    class CountingGraph(nx.Graph):
        visited = 0
        def __iter__(self):
            for node in super().__iter__():
                self.visited += 1
                yield node
    class LimitedStream:
        size = 0
        def write(self, chunk):
            self.size += len(chunk.encode())
            if self.size > 256:
                raise OverflowError('budget')
    graph = CountingGraph()
    graph.add_nodes_from((str(n), {'label': 'x' * 100}) for n in range(100))
    monkeypatch.setattr(json_graph, 'node_link_data', lambda *a, **kw: pytest.fail('whole graph materialized'))
    with pytest.raises(OverflowError, match='budget'):
        write_json(graph, {}, LimitedStream())
    assert graph.visited < len(graph)


@pytest.mark.parametrize('deadline', [None, True, 0, -1, 'later'])
def test_query_requires_explicit_positive_deadline_before_authority(deadline):
    runtime = SimpleNamespace(validate_authority=lambda: pytest.fail('authority read before deadline admission'))
    with pytest.raises(QueryRejected, match='deadline'):
        query_structural(runtime, REPO_UUID, QueryRequest('caller'), deadline_ns=deadline)


def test_shared_clone_alternates_refused_before_discovery(tmp_path, monkeypatch):
    origin = create_repo(tmp_path.resolve() / 'origin')
    clone = tmp_path.resolve() / 'clone'
    git_output(origin, 'clone', '--shared', str(origin), str(clone))
    assert (clone / '.git/objects/info/alternates').is_file()
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('Git read shared objects'))
    with pytest.raises(SourceDiscoveryError, match='alternate'):
        discover_source(clone)


@pytest.mark.parametrize('budget_seconds', [30, 60])
def test_caller_deadline_covers_slow_query_and_withholds_expired_output(tmp_path, monkeypatch, budget_seconds):
    import time
    from graphify.workspace import query
    from graphify.workspace.persistence import LockTimeout
    from graphify.workspace.sync import synchronize_structural
    from tests.test_workspace_structural_s4 import runtime_fixture, request_for
    from tests.workspace_s3_helpers import tree_snapshot

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    synchronize_structural(runtime, request_for(runtime), attempt_sha256='a' * 64)
    before = tree_snapshot(repo), tree_snapshot(runtime.inputs.state_root)
    start = time.monotonic_ns()
    deadline = start + budget_seconds * 1_000_000_000
    elapsed = 0
    observed_deadlines = []
    original = runtime.adapter.observe_lifecycle
    def observe(*args, **kwargs):
        nonlocal elapsed
        observed_deadlines.append(kwargs['deadline_ns'])
        result = original(*args, **kwargs)
        elapsed = 31_000_000_000
        return result
    def check(value, message):
        if start + elapsed >= value:
            raise LockTimeout(message)
    # Model slow I/O without sleeping or changing clocks shared by lease stores.
    monkeypatch.setattr(runtime.adapter, 'observe_lifecycle', observe)
    monkeypatch.setattr(query, 'require_before_deadline', check)
    if budget_seconds == 30:
        with pytest.raises(LockTimeout, match='deadline'):
            query_structural(runtime, REPO_UUID, QueryRequest('caller'), deadline_ns=deadline)
    else:
        assert 'leaf' in query_structural(runtime, REPO_UUID, QueryRequest('caller'), deadline_ns=deadline)
    assert observed_deadlines == [deadline, deadline]
    assert (tree_snapshot(repo), tree_snapshot(runtime.inputs.state_root)) == before
