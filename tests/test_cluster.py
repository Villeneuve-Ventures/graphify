import json
import sys
import types
import networkx as nx
import pytest
from pathlib import Path
from graphify.build import build_from_json
from graphify.cluster import (
    _canonical_node_key,
    _partition,
    _split_community,
    cluster,
    cohesion_score,
    remap_communities_to_previous,
    score_all,
)

FIXTURES = Path(__file__).parent / "fixtures"

def make_graph():
    return build_from_json(json.loads((FIXTURES / "extraction.json").read_text()))


def _install_native_stub(monkeypatch, leiden):
    module = types.ModuleType("graspologic_native")
    setattr(module, "leiden", leiden)
    monkeypatch.setitem(sys.modules, "graspologic_native", module)


def _normalized_membership(partition):
    communities = {}
    for node, cid in partition.items():
        communities.setdefault(cid, set()).add(node)
    return {frozenset(nodes) for nodes in communities.values()}

def test_cluster_returns_dict():
    G = make_graph()
    communities = cluster(G)
    assert isinstance(communities, dict)

def test_cluster_covers_all_nodes():
    G = make_graph()
    communities = cluster(G)
    all_nodes = {n for nodes in communities.values() for n in nodes}
    assert all_nodes == set(G.nodes)


def test_cluster_preserves_spokes_isolated_by_hub_exclusion(monkeypatch):
    edges = [("hub", f"leaf-{index}") for index in range(4)]
    forward = nx.Graph(edges)
    reverse = nx.Graph(reversed(edges))

    def unexpected_partition(*args, **kwargs):
        pytest.fail("edge-less spokes must not be sent to Leiden")

    monkeypatch.setattr("graphify.cluster._partition", unexpected_partition)
    forward_communities = cluster(forward, exclude_hubs_percentile=80)
    reverse_communities = cluster(reverse, exclude_hubs_percentile=80)
    forward_membership = {frozenset(nodes) for nodes in forward_communities.values()}
    reverse_membership = {frozenset(nodes) for nodes in reverse_communities.values()}

    assert {node for nodes in forward_communities.values() for node in nodes} == set(forward)
    assert sorted(map(len, forward_communities.values())) == [1, 1, 1, 2]
    assert forward_membership == reverse_membership
    assert frozenset({"hub", "leaf-0"}) in forward_membership

def test_cohesion_score_complete_graph():
    G = nx.complete_graph(4)
    G = nx.relabel_nodes(G, {i: str(i) for i in G.nodes})
    score = cohesion_score(G, list(G.nodes))
    assert score == 1.0

def test_cohesion_score_single_node():
    G = nx.Graph()
    G.add_node("a")
    score = cohesion_score(G, ["a"])
    assert score == 1.0

def test_cohesion_score_disconnected():
    G = nx.Graph()
    G.add_nodes_from(["a", "b", "c"])
    score = cohesion_score(G, ["a", "b", "c"])
    assert score == 0.0

def test_cohesion_score_range():
    G = make_graph()
    communities = cluster(G)
    for cid, nodes in communities.items():
        score = cohesion_score(G, nodes)
        assert 0.0 <= score <= 1.0

def test_score_all_keys_match_communities():
    G = make_graph()
    communities = cluster(G)
    scores = score_all(G, communities)
    assert set(scores.keys()) == set(communities.keys())


def test_cluster_does_not_write_to_stdout(capsys):
    """Clustering should not emit ANSI escape codes or other output.

    Leiden implementations can emit ANSI escape sequences that break
    PowerShell 5.1's scroll buffer on Windows (issue #19). The output
    suppression in _partition() should prevent any output from leaking.
    """
    G = make_graph()
    cluster(G)
    captured = capsys.readouterr()
    assert captured.out == "", f"cluster() wrote to stdout: {captured.out!r}"


def test_cluster_does_not_write_to_stderr(capsys):
    """Same as above but for stderr — ANSI codes can go to either stream."""
    G = make_graph()
    cluster(G)
    captured = capsys.readouterr()
    # Allow logging output (starts with [graphify]) but no raw ANSI codes
    for line in captured.err.splitlines():
        assert "\x1b" not in line, f"cluster() wrote ANSI to stderr: {line!r}"


def test_canonical_node_keys_are_exact_and_type_tagged():
    assert _canonical_node_key("1") == 'str:"1"'
    assert _canonical_node_key(1) == "int:1"
    assert _canonical_node_key(True) == "bool:1"
    assert _canonical_node_key(False) == "bool:0"
    assert _canonical_node_key(b"\x00\xff") == "bytes:00ff"
    assert _canonical_node_key(None) == "none:"


def test_native_leiden_maps_nodes_weights_and_exact_arguments(monkeypatch, capsys):
    calls = []

    def leiden(edges, **kwargs):
        calls.append((edges, kwargs))
        print("hidden stdout")
        print("hidden stderr", file=sys.stderr)
        return 0.75, {"0": 4, "1": 4, "2": 9}

    _install_native_stub(monkeypatch, leiden)
    graph = nx.Graph()
    graph.add_edge("1", 1, weight=2)
    graph.add_edge(1, b"node")

    assert _partition(graph, resolution=2.0) == {b"node": 4, 1: 4, "1": 9}
    assert calls == [
        (
            [("0", "1", 1.0), ("1", "2", 2.0)],
            {
                "resolution": 2.0,
                "randomness": 0.001,
                "iterations": 1,
                "use_modularity": True,
                "seed": 42,
                "trials": 1,
            },
        )
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_native_leiden_is_insertion_order_invariant(monkeypatch):
    calls = []

    def leiden(edges, **kwargs):
        calls.append(edges)
        return 1.0, {node: int(node) % 2 for edge in edges for node in edge[:2]}

    _install_native_stub(monkeypatch, leiden)
    forward = nx.Graph()
    forward.add_edge("z", 1, weight=0.5)
    forward.add_edge(b"node", "z", weight=2)
    reverse = nx.Graph()
    reverse.add_edge("z", b"node", weight=2)
    reverse.add_edge(1, "z", weight=0.5)

    assert _partition(forward) == _partition(reverse)
    assert calls[0] == calls[1]


def test_native_leiden_rejects_unsupported_node_before_call(monkeypatch):
    called = False

    def leiden(edges, **kwargs):
        nonlocal called
        called = True
        return 0.0, {}

    _install_native_stub(monkeypatch, leiden)
    graph = nx.Graph()
    graph.add_edge(object(), "supported")

    with pytest.raises(TypeError, match="Leiden node IDs must be"):
        _partition(graph)
    assert not called


def test_native_leiden_rejects_canonical_key_collisions(monkeypatch):
    _install_native_stub(monkeypatch, lambda edges, **kwargs: (0.0, {}))
    monkeypatch.setattr("graphify.cluster._canonical_node_key", lambda node: "same:key")
    with pytest.raises(ValueError, match="canonical node-key collision"):
        _partition(nx.Graph([("a", "b")]))


def test_louvain_fallback_only_for_missing_top_level_native_module(monkeypatch):
    def missing_native(name):
        raise ModuleNotFoundError("missing", name=name)

    monkeypatch.delitem(sys.modules, "graspologic_native", raising=False)
    monkeypatch.setattr("graphify.cluster.importlib.import_module", missing_native)
    graph = nx.Graph([("a", "b"), ("c", "d")])
    partition = _partition(graph)
    assert set(partition) == set(graph)


def test_broken_native_import_propagates(monkeypatch):
    def broken_native(name):
        raise ModuleNotFoundError("broken dependency", name="native_dependency")

    monkeypatch.delitem(sys.modules, "graspologic_native", raising=False)
    monkeypatch.setattr("graphify.cluster.importlib.import_module", broken_native)
    with pytest.raises(ModuleNotFoundError, match="broken dependency"):
        _partition(nx.Graph([("a", "b")]))


def test_native_import_error_propagates(monkeypatch):
    def broken_native(name):
        raise ImportError("broken native extension")

    monkeypatch.delitem(sys.modules, "graspologic_native", raising=False)
    monkeypatch.setattr("graphify.cluster.importlib.import_module", broken_native)
    with pytest.raises(ImportError, match="broken native extension"):
        _partition(nx.Graph([("a", "b")]))


@pytest.mark.parametrize("error", [ImportError("broken call"), RuntimeError("native failed")])
def test_native_call_exceptions_propagate(monkeypatch, error):
    def leiden(edges, **kwargs):
        raise error

    _install_native_stub(monkeypatch, leiden)
    with pytest.raises(type(error), match=str(error)):
        _partition(nx.Graph([("a", "b")]))


def test_native_call_exceptions_propagate_during_community_split(monkeypatch):
    def leiden(edges, **kwargs):
        raise RuntimeError("native split failed")

    _install_native_stub(monkeypatch, leiden)
    graph = nx.Graph([("a", "b")])
    with pytest.raises(RuntimeError, match="native split failed"):
        _split_community(graph, ["a", "b"])


def test_native_leiden_rejects_node_loss(monkeypatch):
    _install_native_stub(monkeypatch, lambda edges, **kwargs: (0.0, {"0": 0}))
    with pytest.raises(RuntimeError, match="incomplete or unknown node mapping"):
        _partition(nx.Graph([("a", "b")]))


@pytest.mark.parametrize("resolution", [0.5, 1.0, 2.0])
def test_native_leiden_resolution_parity_fixture(resolution):
    pytest.importorskip("graspologic_native")
    graph = nx.Graph()
    for start in (0, 4, 8):
        for source in range(start, start + 4):
            for target in range(source + 1, start + 4):
                graph.add_edge(source, target, weight=1.0)
    graph.add_edge(3, 4, weight=0.2)
    graph.add_edge(7, 8, weight=0.2)
    expected = {
        frozenset(range(0, 4)),
        frozenset(range(4, 8)),
        frozenset(range(8, 12)),
    }
    first = _partition(graph, resolution=resolution)
    second = _partition(graph, resolution=resolution)
    assert _normalized_membership(first) == expected
    assert _normalized_membership(second) == expected


def test_remap_communities_to_previous_reuses_old_ids():
    communities = {
        10: ["a", "b", "c"],
        11: ["d", "e"],
    }
    previous = {"a": 5, "b": 5, "c": 5, "d": 1, "e": 1}
    remapped = remap_communities_to_previous(communities, previous)
    assert set(remapped.keys()) == {1, 5}
    assert remapped[5] == ["a", "b", "c"]
    assert remapped[1] == ["d", "e"]


def test_remap_communities_to_previous_assigns_deterministic_new_ids():
    communities = {
        7: ["x", "y", "z"],
        8: ["m"],
    }
    previous = {"a": 3}
    remapped = remap_communities_to_previous(communities, previous)
    assert list(remapped.keys()) == [0, 1]
    assert remapped[0] == ["x", "y", "z"]
    assert remapped[1] == ["m"]
