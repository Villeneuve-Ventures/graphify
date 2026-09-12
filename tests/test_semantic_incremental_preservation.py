"""Accepted semantic records survive unrelated incremental extraction."""
from copy import deepcopy
import json

import pytest

from graphify.build import build, build_from_json, build_merge
from graphify.export import to_json


def _node(nid, label, source="raw.json", **attrs):
    return dict(id=nid, label=label, source_file=source, file_type="concept", **attrs)


def _edge(source, target, **attrs):
    return dict(source=source, target=target, relation="references",
                confidence="EXTRACTED", confidence_score=1.0, source_file="raw.json", **attrs)


def _facts():
    return {"nodes": [_node("raw_doc", "Raw fixture"),
                      _node("raw_price", "Raw CoinGecko fixture price array", source_location="2-6"),
                      _node("raw_volume", "Raw CoinGecko fixture volume array", source_location="12-16")],
            "edges": [_edge("raw_doc", "raw_price", source_location="2-6"),
                      _edge("raw_doc", "raw_volume", source_location="12-16")]}


def _save(tmp_path, data, *, directed=False):
    graph = build_from_json(deepcopy(data), directed=directed, root=tmp_path)
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(exist_ok=True)
    assert to_json(graph, {}, path)
    # The persisted graph (including export defaults) is the accepted context.
    stored = json.loads(path.read_text())
    for node in stored["nodes"]:
        graph.nodes[node["id"]].clear()
        graph.nodes[node["id"]].update({k: v for k, v in node.items() if k != "id"})
    return graph, path


@pytest.mark.parametrize("directed", [False, True])
def test_unrelated_facts_and_references_survive_repeated_update(tmp_path, directed):
    accepted, path = _save(tmp_path, _facts(), directed=directed)
    for version in ("one", "two"):
        fresh = {"nodes": [_node("changed", f"Policy {version}", "changed.md")], "edges": []}
        graph = build_merge([fresh], path, directed=directed, root=tmp_path)
        for nid, attrs in accepted.nodes(data=True):
            assert graph.nodes[nid] == attrs
        for source, target, attrs in accepted.edges(data=True):
            assert graph[source][target] == attrs
        assert to_json(graph, {}, path)


def test_fresh_dedup_still_runs_without_retained_llm_candidates(tmp_path, monkeypatch):
    import graphify.dedup as dedup
    accepted, path = _save(tmp_path, _facts())
    fresh = {"nodes": [_node("new_price", "Fresh CoinGecko fixture price array", "changed.md"),
                       _node("new_volume", "Fresh CoinGecko fixture volume array", "changed.md"),
                       _node("other", "Fresh CoinGecko fixture price array", "changed.md")], "edges": []}
    expected = build([deepcopy(fresh)])
    seen = []
    original = dedup.deduplicate_entities
    def inspect(nodes, edges, **kwargs):
        seen.extend(n["id"] for n in nodes)
        return original(nodes, edges, **kwargs)
    monkeypatch.setattr(dedup, "deduplicate_entities", inspect)
    monkeypatch.setattr(dedup, "_llm_tiebreak", lambda *a, **kw: None)
    graph = build_merge([fresh], path, dedup_llm_backend="local-test", root=tmp_path)
    assert set(seen).isdisjoint(accepted)
    assert len(expected) == 1
    assert set(graph) == set(accepted) | set(expected)


@pytest.mark.parametrize("endpoint", ["docs_nested_api_readme_topic", "api_readme_topic"])
def test_fresh_edges_resolve_exact_and_legacy_retained_references(tmp_path, endpoint):
    nid = "docs_nested_api_readme_topic"
    accepted, path = _save(tmp_path, {"nodes": [_node(nid, "Topic", "docs/nested/api/README.md")], "edges": []})
    graph = build_merge([{"nodes": [_node("fresh", "Novel policy", "changed.md")],
                          "edges": [_edge("fresh", endpoint)]}], path, root=tmp_path)
    assert graph.has_edge("fresh", nid)
    assert graph.nodes[nid] == accepted.nodes[nid]


def test_exact_id_restatement_cannot_bleed_attributes_or_trigger_dedup(tmp_path):
    accepted, path = _save(tmp_path, _facts())
    graph = build_merge([{"nodes": [_node("raw_price", "Corrupt reference", None, surprise=True),
                                    _node("fresh", "Corrupt reference", "changed.md")],
                          "edges": [_edge("fresh", "raw_price")]}], path, root=tmp_path)
    assert graph.nodes["raw_price"] == accepted.nodes["raw_price"]
    assert "fresh" in graph and graph.has_edge("fresh", "raw_price")


def test_distinct_fresh_id_cannot_normalize_onto_retained_id(tmp_path):
    _, path = _save(tmp_path, {"nodes": [_node("docs_api_readme_topic", "Accepted", "old.md")], "edges": []})
    with pytest.raises(ValueError, match="retained node identity"):
        build_merge([{"nodes": [_node("api_readme_topic", "Fresh", "docs/api/README.md")], "edges": []}], path, root=tmp_path)


def test_retained_nodes_are_not_ghost_merged_into_fresh_ast(tmp_path):
    accepted, path = _save(tmp_path, {"nodes": [_node("accepted", "parse", "old/module.py", source_location="L10")], "edges": []})
    graph = build_merge([{"nodes": [_node("fresh", "parse", "new/module.py", _origin="ast")], "edges": []}], path, root=tmp_path)
    assert set(graph) == {"accepted", "fresh"}
    assert graph.nodes["accepted"] == accepted.nodes["accepted"]


@pytest.mark.parametrize("variation", ["equal", "evidence", "reverse"])
@pytest.mark.parametrize("directed", [False, True])
def test_retained_edge_slot_cannot_be_overwritten(tmp_path, variation, directed):
    accepted, path = _save(tmp_path, _facts(), directed=directed)
    edge = deepcopy(_facts()["edges"][0])
    if variation == "evidence":
        edge["source_location"] = "12-16"
    elif variation == "reverse":
        edge["source"], edge["target"] = edge["target"], edge["source"]
    chunk = {"nodes": [], "edges": [edge]}
    if variation == "evidence" or (variation == "reverse" and not directed):
        with pytest.raises(ValueError, match="retained edge"):
            build_merge([chunk], path, root=tmp_path, directed=directed)
    else:
        graph = build_merge([chunk], path, root=tmp_path, directed=directed)
        assert graph["raw_doc"]["raw_price"] == accepted["raw_doc"]["raw_price"]
        if variation == "reverse":
            assert graph.has_edge("raw_price", "raw_doc")


def test_replacement_pruning_and_retained_hyperedges(tmp_path):
    data = _facts()
    data["nodes"] += [_node("old", "Obsolete", "docs/changed.md"), _node("deleted", "Removed", "docs/deleted.md")]
    data["edges"] += [_edge("raw_doc", "old")]
    data["hyperedges"] = [{"id": "stable", "nodes": ["raw_price", "raw_volume"], "source_file": "raw.json"},
                          {"id": "obsolete", "nodes": ["old"], "source_file": "docs/changed.md"}]
    _, path = _save(tmp_path, data)
    graph = build_merge([{"nodes": [_node("new", "Replacement", str(tmp_path / "docs/changed.md"))], "edges": []}],
                        path, root=tmp_path, prune_sources=["docs\\deleted.md", "docs/changed.md"])
    assert set(graph) == {"raw_doc", "raw_price", "raw_volume", "new"}
    assert [he["id"] for he in graph.graph["hyperedges"]] == ["stable"]


@pytest.mark.parametrize("confidence,default", [("EXTRACTED", 1.0), ("INFERRED", 0.5), ("AMBIGUOUS", 0.2)])
def test_optional_confidence_defaults_do_not_conflict(tmp_path, confidence, default):
    data = _facts()
    edge = data["edges"][0]
    edge["confidence"] = confidence
    del edge["confidence_score"]
    _, path = _save(tmp_path, data)
    accepted = json.loads(path.read_text())["links"]
    for score in (None, default):
        fresh = deepcopy(edge)
        if score is not None:
            fresh["confidence_score"] = score
        graph = build_merge([{"nodes": [], "edges": [fresh]}], path, root=tmp_path)
        assert graph["raw_doc"]["raw_price"]["confidence_score"] == default
        assert to_json(graph, {}, path)
        assert json.loads(path.read_text())["links"] == accepted
    edge["confidence_score"] = default / 2
    with pytest.raises(ValueError, match="retained edge"):
        build_merge([{"nodes": [], "edges": [edge]}], path, root=tmp_path)
