"""RC1: source ownership must survive collisions in fallback node IDs."""
import copy
import hashlib
from itertools import permutations
from types import SimpleNamespace

import pytest

from graphify.build import _semantic_id_remap, build_from_json
from graphify.extract import extract
from graphify.extractors import resolution
from graphify.ids import make_id, normalize_id


RC1_PATHS = (
    "a_a.a.a_a.a_a.a.a_a_a.a_a.a.a.a.a.a.a.a.a.py",
    "a.a_a_a_a_a.a.a.a_a.a_a_a_a.a.a.a.a.a.a.a.py",
)


def test_rc1_fixed_pair_retains_nodes_and_source_edges(tmp_path):
    assert {hashlib.sha1(name.encode()).hexdigest()[:6] for name in RC1_PATHS} == {"ecad21"}
    paths = [tmp_path / name for name in RC1_PATHS]
    for path in paths:
        path.write_text("def marker():\n    return 1\n", encoding="utf-8")

    assignments = []
    for order in (paths, paths[::-1]):
        result = extract(order, strict=True, parallel=False, ambient_output=False, quiet=True)
        assert len(result["nodes"]) == 4
        assert len({node["id"] for node in result["nodes"]}) == 4
        by_source = {}
        for name in RC1_PATHS:
            owned = [node for node in result["nodes"] if node["source_file"] == name]
            file_node = next(node for node in owned if node["label"] == name)
            function = next(node for node in owned if node["label"] == "marker()")
            by_source[name] = (file_node["id"], function["id"])
        assert len(result["edges"]) == 2
        for edge in result["edges"]:
            assert edge["relation"] == "contains"
            assert (edge["source"], edge["target"]) == by_source[edge["source_file"]]

        graph = build_from_json(result, root=tmp_path)
        assert len(graph) == 4
        assert graph.number_of_edges() == 2
        assert sum(data["label"] == "marker()" for _, data in graph.nodes(data=True)) == 2
        for name, (file_id, function_id) in by_source.items():
            assert graph.nodes[file_id]["source_file"] == name
            assert graph.nodes[function_id]["source_file"] == name
            assert graph.has_edge(file_id, function_id)
            assert graph[file_id][function_id]["source_file"] == name
        assignments.append(by_source)
        semantic_nodes = [{**node, "_origin": "semantic"} for node in result["nodes"]]
        assert _semantic_id_remap(semantic_nodes, str(tmp_path)) == {}
        combined = {"nodes": result["nodes"] + semantic_nodes, "edges": result["edges"]}
        assert set(build_from_json(combined, root=tmp_path)) == set(graph)
    assert assignments[0] == assignments[1]


def test_rc1_member_calls_remain_attributed_to_their_source(tmp_path):
    paths = [tmp_path / name for name in RC1_PATHS]
    for path in paths:
        path.write_text("class Worker:\n"
                        "    def run(self):\n"
                        "        return 1\n\n"
                        "def marker():\n"
                        "    worker = Worker()\n"
                        "    return worker.run()\n", encoding="utf-8")
    assignments = []
    for order in (paths, paths[::-1]):
        result = extract(order, strict=True, parallel=False, ambient_output=False, quiet=True)
        ids = _assignment(result["nodes"])
        assert len(set(ids.values())) == 8
        calls = [edge for edge in result["edges"] if edge["relation"] == "calls"]
        assert len(calls) == 4
        for source in RC1_PATHS:
            owned_calls = [edge for edge in calls if edge["source_file"] == source]
            assert {edge["source"] for edge in owned_calls} == {ids[(source, "marker()")]}
            assert {edge["target"] for edge in owned_calls} == {
                ids[(source, "Worker")], ids[(source, ".run()")],
            }
        graph = build_from_json(result, root=tmp_path)
        assert len(graph) == 8
        for edge in calls:
            assert graph.has_edge(edge["source"], edge["target"])
            assert graph.nodes[edge["source"]]["source_file"] == edge["source_file"]
            assert graph.nodes[edge["target"]]["source_file"] == edge["source_file"]
        assignments.append(ids)
    assert assignments[0] == assignments[1]


def _node(node_id, source, **attributes):
    return {"id": node_id, "source_file": source, **attributes}


def _assignment(nodes):
    return {(node.get("source_file", node.get("origin_file")), node.get("label")): node["id"]
            for node in nodes}


def test_full_digest_collision_remaps_edges_and_raw_callers(tmp_path, monkeypatch):
    monkeypatch.setattr(resolution.hashlib, "sha1",
                        lambda data: SimpleNamespace(hexdigest=lambda: "0" * 40))
    sources = [str(tmp_path / name) for name in ("a.b.py", "a_b.py", "a/b.py")]
    nodes = [_node(node_id, source, label=node_id)
             for source in sources for node_id in ("file", "marker")]
    # Duplicate records for one owner must still share its allocated ID.
    nodes.append(copy.deepcopy(nodes[0]))
    edges = [{"source": "file", "target": "marker", "source_file": source,
              "relation": "contains"} for source in sources]
    calls = [{"caller_nid": "marker", "source_file": source, "callee_name": "run"}
             for source in sources]
    assignments = []
    for ordered_nodes in (nodes, nodes[::-1]):
        current, current_edges, current_calls = copy.deepcopy((ordered_nodes, edges, calls))
        resolution._disambiguate_colliding_node_ids(current, current_edges, current_calls,
                                                   tmp_path, strict=True)
        ids = _assignment(current)
        assert len(set(ids.values())) == 6
        assert len(current) == 7
        for edge, call in zip(current_edges, current_calls):
            source = edge["source_file"]
            assert edge["source"] == ids[(source, "file")]
            assert edge["target"] == call["caller_nid"] == ids[(source, "marker")]
            assert call["callee_name"] == "run"
        assert all(normalize_id(nid) == nid for nid in ids.values())
        assignments.append(ids)
    assert assignments[0] == assignments[1]


@pytest.mark.parametrize("occupied_type", ["function", "module", "namespace"])
def test_allocation_preserves_occupied_ids_and_other_preferred_ids(tmp_path, occupied_type):
    # Two distinct groups request a_py_x. A third requests a_py_x_2, so a
    # fallback must not take that group's otherwise distinct preferred ID.
    nodes = [_node(nid, str(tmp_path / source), label=f"{source}:{nid}")
             for nid, sources in (("x", ("a.py", "b.py")),
                                  ("py_x", ("a", "c")),
                                  ("x_2", ("a.py", "z.py")))
             for source in sources]
    occupied = [_node("a_py_x", str(tmp_path / "occupied"), type=occupied_type),
                _node("a_py_x_3", str(tmp_path / "occupied3"), type=occupied_type)]
    nodes.extend(occupied)
    expected = None
    for groups in permutations((nodes[:2], nodes[2:4], nodes[4:6], nodes[6:])):
        current = copy.deepcopy([node for group in groups for node in group])
        resolution._disambiguate_colliding_node_ids(current, [], [], tmp_path, strict=True)
        ids = _assignment(current)
        assert len(set(ids.values())) == len(current)
        assert ids[(str(tmp_path / "occupied"), None)] == "a_py_x"
        assert ids[(str(tmp_path / "occupied3"), None)] == "a_py_x_3"
        assert ids[(str(tmp_path / "a.py"), "a.py:x_2")] == "a_py_x_2"
        assert ids[(str(tmp_path / "b.py"), "b.py:x")] == "b_py_x"
        assert ids[(str(tmp_path / "c"), "c:py_x")] == "c_py_x"
        assert ids == (expected if expected is not None else ids)
        expected = ids


def test_unaffected_and_existing_distinct_disambiguated_ids_stay_compatible(tmp_path):
    sources = ("foo/bar_baz.py", "foo_bar/baz.py")
    nodes = [_node("symbol", str(tmp_path / source)) for source in sources]
    unaffected = [_node("plain", str(tmp_path / "plain.py")),
                  _node("shared", str(tmp_path / "a.py"), type="module"),
                  _node("shared", str(tmp_path / "b.py"), type="module"),
                  _node("same_owner", str(tmp_path / "same.py")),
                  _node("same_owner", str(tmp_path / "same.py"))]
    original = copy.deepcopy(unaffected)
    nodes.extend(unaffected)
    resolution._disambiguate_colliding_node_ids(nodes, [], [], tmp_path, strict=True)
    assert unaffected == original
    for node, source in zip(nodes, sources):
        salt = hashlib.sha1(source.encode()).hexdigest()[:6]
        assert node["id"] == make_id(source, "symbol", salt)
    # A second pass must leave the distinct IDs untouched.
    before = copy.deepcopy(nodes)
    resolution._disambiguate_colliding_node_ids(nodes, [], [], tmp_path, strict=True)
    assert nodes == before
