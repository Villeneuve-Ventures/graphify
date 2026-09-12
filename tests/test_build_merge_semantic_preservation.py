"""Accepted graph records are not fresh extraction input during semantic updates."""
from copy import deepcopy
import json

import pytest

from graphify.build import build, build_merge
from graphify.export import to_json


def node(identity, label=None, source="stable.md", **attrs):
    return dict(id=identity, label=label or identity, source_file=source,
                file_type="document", **attrs)


def edge(source, target, owner="stable.md", **attrs):
    return dict(source=source, target=target, source_file=owner,
                relation="references", confidence="EXTRACTED", **attrs)


def seed(tmp_path, nodes=None, edges=None, hyperedges=None):
    path = tmp_path / "graph.json"
    data = {"nodes": nodes or [
        node("price", "Raw CoinGecko fixture price array", "fixture.json"),
        node("volume", "Raw CoinGecko fixture volume array", "fixture.json"),
        node("anchor", source="fixture.json"),
    ], "links": edges if edges is not None else [
        edge("anchor", "price", "fixture.json", source_location="lines 2-5"),
        edge("anchor", "volume", "fixture.json", source_location="lines 12-16"),
    ], "hyperedges": hyperedges or []}
    path.write_text(json.dumps(data))
    return path, data


def assert_retained(graph, data):
    for record in data["nodes"]:
        assert graph.nodes[record["id"]] == {k: v for k, v in record.items() if k != "id"}
    for record in data["links"]:
        attrs = dict(record)
        source, target = attrs.pop("source"), attrs.pop("target")
        assert graph.edges[source, target] == dict(attrs, _src=source, _tgt=target)


@pytest.mark.parametrize("directed", [False, True])
def test_unrelated_semantic_facts_survive_update_and_repeat(tmp_path, directed):
    path, data = seed(tmp_path)
    fresh = {"nodes": [node("beacon", "Unrelated isolation beacon", "changed.md")], "edges": []}
    for _ in range(2):
        data = json.loads(path.read_text())
        data["nodes"] = [record for record in data["nodes"] if record["id"] != "beacon"]
        graph = build_merge([deepcopy(fresh)], path, root=tmp_path, directed=directed)
        assert_retained(graph, data)
        assert "beacon" in graph
        to_json(graph, {}, path)


@pytest.mark.parametrize("fresh", [False, True])
def test_retained_legacy_doc_twins_and_ghosts_are_never_reinterpreted(tmp_path, fresh):
    nodes = [node("old_item", source="nested/old.md"),
             node("guide", source="guide.md"), node("guide_doc", source="guide.md"),
             node("real", "shared", "stable.py", _origin="ast", source_location="L1"),
             node("ghost", "shared", "stable.py", source_location="L1")]
    path, data = seed(tmp_path, nodes, [edge("old_item", "ghost")])
    chunks = [{"nodes": [node("fresh", source="changed.md")]}] if fresh else []
    graph = build_merge(chunks, path, root=tmp_path)
    assert_retained(graph, data)


@pytest.mark.parametrize("alias", ["docs_policy_rule", "policy_rule"])
@pytest.mark.parametrize("directed", [False, True])
def test_fresh_references_and_hyperedges_resolve_retained_namespace(tmp_path, alias, directed):
    path, _ = seed(tmp_path, [node("docs_policy_rule", source="docs/policy.md")], [])
    fresh = {"nodes": [node("fresh", source="changed.md")],
             "edges": [edge("fresh", alias, "changed.md")],
             "hyperedges": [{"id": "mixed", "source_file": "changed.md", "nodes": ["fresh", alias]}]}
    graph = build_merge([fresh], path, root=tmp_path, directed=directed)
    assert graph.has_edge("fresh", "docs_policy_rule")
    assert graph.graph["hyperedges"][0]["nodes"] == ["fresh", "docs_policy_rule"]


def test_ambiguous_normalized_retained_alias_never_selects_target(tmp_path):
    path, _ = seed(tmp_path, [node("Rule-One"), node("rule_one")], [])
    graph = build_merge([{"nodes": [node("fresh", source="changed.md")],
                          "edges": [edge("fresh", "RULE ONE", "changed.md")]}], path)
    assert graph.number_of_edges() == 0


def test_retained_definer_accepts_reference_without_joining_fresh_dedup(tmp_path):
    retained = node("stable_rule", "Canonical rule", "stable.md", quotation="original")
    path, data = seed(tmp_path, [retained], [])
    reference = node("stable_rule", "Mentioned rule", "changed.md")
    fresh = {"nodes": [reference, node("fresh", source="changed.md")],
             "edges": [edge("fresh", "stable_rule", "changed.md")]}
    graph = build_merge([fresh], path)
    assert_retained(graph, data)
    assert graph.has_edge("fresh", "stable_rule")


def test_raw_conflicting_definition_cannot_disappear_in_dedup(tmp_path):
    path, _ = seed(tmp_path, [node("stable_rule", source="a/stable.md")], [])
    fresh = {"nodes": [node("stable_rule", "Conflicting definition", "b/stable.md"),
                       node("stable_rule", source="changed.md")]}
    before = path.read_bytes()
    with pytest.raises(ValueError, match="retained node"):
        build_merge([fresh], path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("reverse", [False, True])
def test_raw_occupied_edge_slot_conflict_refuses(tmp_path, reverse):
    path, _ = seed(tmp_path)
    source, target = ("price", "anchor") if reverse else ("anchor", "price")
    fresh = {"nodes": [node("fresh", source="changed.md")],
             "edges": [edge(source, target, "changed.md", quotation="different")]}
    with pytest.raises(ValueError, match="retained edge"):
        build_merge([fresh], path)


def test_fresh_dedup_remaps_hyperedge_members(tmp_path):
    path, _ = seed(tmp_path, [node("retained")], [])
    fresh = {"nodes": [node("fresh", "A duplicated source concept", "changed.md"),
                       node("fresh_chunk1", "A duplicated source concept", "changed.md")],
             "hyperedges": [{"id": "mixed", "source_file": "changed.md",
                             "nodes": ["fresh_chunk1", "retained"]}]}
    graph = build_merge([fresh], path)
    assert "fresh" in graph and "fresh_chunk1" not in graph
    assert graph.graph["hyperedges"][0]["nodes"] == ["fresh", "retained"]
    assert len(build([{"nodes": deepcopy(fresh["nodes"])}])) == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_retained_hyperedge_identity_collision(tmp_path, conflict):
    group = {"id": "group", "source_file": "stable.md", "nodes": ["retained"], "label": "original"}
    path, _ = seed(tmp_path, [node("retained")], [], [group])
    incoming = dict(group, label="conflict") if conflict else dict(group)
    fresh = {"nodes": [node("fresh", source="changed.md")], "hyperedges": [incoming]}
    if conflict:
        with pytest.raises(ValueError, match="retained hyperedge"):
            build_merge([fresh], path)
    else:
        graph = build_merge([fresh], path)
        assert graph.graph["hyperedges"] == [group]


@pytest.mark.parametrize("prune", [False, True])
def test_explicit_retirement_drops_only_affected_endpoints(tmp_path, prune):
    nodes = [node("kept"), node("retired", source="changed.md")]
    path, _ = seed(tmp_path, nodes, [edge("kept", "retired")],
                   [{"id": "group", "nodes": ["kept", "retired"], "source_file": "stable.md"}])
    chunks = [] if prune else [{"nodes": [node("replacement", source="changed.md")]}]
    graph = build_merge(chunks, path, root=tmp_path,
                        prune_sources=[str(tmp_path / "changed.md")] if prune else None)
    assert "kept" in graph and "retired" not in graph
    assert graph.number_of_edges() == 0
    assert graph.graph["hyperedges"][0]["nodes"] == ["kept"]


def test_incremental_dedup_still_rejects_cross_project_graph(tmp_path):
    path, _ = seed(tmp_path, [node("retained", repo="first")], [])
    with pytest.raises(ValueError, match="Cross-project dedup"):
        build_merge([{"nodes": [node("fresh", source="changed.md", repo="second")]}], path)


@pytest.mark.parametrize("directed", [False, True])
@pytest.mark.parametrize("alias", ["Z_POLICY_RULE", "policy_rule"])
def test_resolved_edge_conflict_cannot_be_masked_by_compatible_copy(tmp_path, directed, alias):
    accepted = edge("anchor", "z_policy_rule")
    path, _ = seed(tmp_path, [node("anchor"), node("z_policy_rule", source="z/policy.md")],
                   [accepted])
    fresh = {"nodes": [node("fresh", source="changed.md")],
             "edges": [dict(accepted, target=alias, confidence="INFERRED"), dict(accepted)]}
    before = path.read_bytes()
    with pytest.raises(ValueError, match="retained edge"):
        build_merge([fresh], path, root=tmp_path, directed=directed)
    assert path.read_bytes() == before


@pytest.mark.parametrize("directed", [False, True])
@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("absolute", [False, True])
def test_fresh_edge_pruning_respects_normalized_owner_and_replacement(tmp_path, directed, replaced, absolute):
    path, _ = seed(tmp_path, [node("anchor"), node("other")], [])
    owner = str(tmp_path / "deleted.md") if absolute else "deleted.md"
    fresh = {"nodes": [node("fresh", source="deleted.md" if replaced else "changed.md")],
             "edges": [edge("anchor", "other", owner)]}
    graph = build_merge([fresh], path, root=tmp_path, directed=directed,
                        prune_sources=[str(tmp_path / "deleted.md")])
    assert graph.has_edge("anchor", "other") is replaced
    assert "fresh" in graph


@pytest.mark.parametrize("directed", [False, True])
@pytest.mark.parametrize("exact", [False, True])
def test_fresh_ghost_alias_cannot_redirect_ambiguous_retained_reference(tmp_path, directed, exact):
    path, _ = seed(tmp_path, [node("Rule-One")], [])
    fresh = {"nodes": [node("actual", "Shared symbol", "changed.py", _origin="ast", source_location="L1"),
                       node("rule_one", "Shared symbol", "changed.py", source_location="L1"),
                       node("fresh", source="changed.md")],
             "edges": [edge("fresh", "Rule-One" if exact else "RULE ONE", "changed.md")]}
    graph = build_merge([fresh], path, directed=directed, dedup=False)
    assert "actual" in graph and "rule_one" not in graph
    assert not graph.has_edge("fresh", "actual")
    assert graph.has_edge("fresh", "Rule-One") is exact


@pytest.mark.parametrize("directed", [False, True])
def test_retained_legacy_edge_endpoints_remain_readable(tmp_path, directed):
    legacy = dict(edge("a", "b"))
    legacy["from"], legacy["to"] = legacy.pop("source"), legacy.pop("target")
    path, _ = seed(tmp_path, [node("a"), node("b")], [legacy])
    before = path.read_bytes()
    graph = build_merge([{"nodes": [node("fresh", source="changed.md")]}], path,
                        directed=directed)
    assert graph.has_edge("a", "b")
    assert graph.edges["a", "b"]["_src"] == "a"
    assert "from" not in graph.edges["a", "b"] and "to" not in graph.edges["a", "b"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("member_key", ["members", "node_ids"])
@pytest.mark.parametrize("prune", [False, True])
def test_retained_group_member_alias_preserves_payload_and_retirement(tmp_path, member_key, prune):
    group = {"id": "group", "label": "Evidence", "source_file": "stable.md", member_key: ["a", "b"]}
    path, _ = seed(tmp_path, [node("a"), node("b", source="deleted.md")], [], [group])
    graph = build_merge([], path, prune_sources=["deleted.md"] if prune else None)
    expected = dict(group, **{member_key: ["a"]}) if prune else group
    assert graph.graph["hyperedges"] == [expected]


@pytest.mark.parametrize("prune", [False, True])
def test_retained_legacy_node_owner_is_used_without_rewriting_payload(tmp_path, prune):
    legacy = node("a", source="deleted.md")
    legacy["source"] = legacy.pop("source_file")
    path, _ = seed(tmp_path, [legacy, node("b")], [])
    graph = build_merge([], path, root=tmp_path,
                        prune_sources=[str(tmp_path / "deleted.md")] if prune else None)
    assert ("a" not in graph) is prune
    if not prune:
        assert graph.nodes["a"] == {k: v for k, v in legacy.items() if k != "id"}


@pytest.mark.parametrize("malformed", [{}, {"id": []}, {"id": {}}])
def test_malformed_retained_node_ids_do_not_block_recovery(tmp_path, malformed, capsys):
    bad = dict(label="Invalid", source_file="broken.md", file_type="document", **malformed)
    path, _ = seed(tmp_path, [node("a"), bad], [])
    graph = build_merge([{"nodes": [node("fresh", source="changed.md")]}], path, dedup=False)
    assert set(graph) == {"a", "fresh"}
    assert ("skipping node with non-hashable id" in capsys.readouterr().err) is ("id" in malformed)


@pytest.mark.parametrize("member_key", ["nodes", "members", "node_ids"])
@pytest.mark.parametrize("alias", ["docs_policy_rule", "policy_rule"])
def test_equivalent_group_claim_uses_actual_member_resolution(tmp_path, member_key, alias):
    group = {"id": "group", "label": "Evidence", "source_file": "stable.md", "nodes": ["anchor", "docs_policy_rule"]}
    path, _ = seed(tmp_path, [node("anchor"), node("docs_policy_rule", source="docs/policy.md")], [], [group])
    incoming = {k: v for k, v in group.items() if k != "nodes"}
    incoming[member_key] = ["anchor", alias]
    graph = build_merge([{"nodes": [node("fresh", source="changed.md")], "hyperedges": [incoming]}], path)
    assert graph.graph["hyperedges"] == [group]


def test_group_conflict_cannot_disappear_during_member_filtering(tmp_path):
    group = {"id": "group", "source_file": "stable.md", "nodes": ["a"]}
    path, _ = seed(tmp_path, [node("a")], [], [group])
    with pytest.raises(ValueError, match="retained hyperedge"):
        build_merge([{"hyperedges": [dict(group, nodes=["a", "unknown"])]}], path)


@pytest.mark.parametrize("malformed", [[], {}])
def test_fresh_dedup_tolerates_unhashable_group_members(tmp_path, malformed):
    path, _ = seed(tmp_path, [node("a")], [])
    fresh = {"nodes": [node("fresh", "Same entity", "changed.md"),
                       node("fresh_chunk1", "Same entity", "changed.md")],
             "hyperedges": [{"id": "new", "source_file": "changed.md", "nodes": ["a", "fresh_chunk1", malformed]}]}
    graph = build_merge([fresh], path)
    assert graph.graph["hyperedges"][0]["nodes"] == ["a", "fresh"]


@pytest.mark.parametrize("confidence", ["EXTRACTED", "INFERRED", "AMBIGUOUS"])
@pytest.mark.parametrize("score_mode", ["default", "explicit_equal", "explicit_conflict"])
def test_exported_edge_default_score_is_semantically_compatible(tmp_path, confidence, score_mode):
    accepted = dict(edge("a", "b"), confidence=confidence)
    incoming = dict(accepted)
    if score_mode != "default":
        accepted["confidence_score"] = 0.73
    if score_mode == "explicit_equal":
        incoming["confidence_score"] = 0.73
    graph = build([{"nodes": [node("a"), node("b")], "edges": [accepted]}], dedup=False)
    path = tmp_path / "graph.json"
    assert to_json(graph, {}, path)
    before = json.loads(path.read_text())["links"][0]
    fresh = {"nodes": [node("fresh", source="changed.md")], "edges": [incoming]}
    if score_mode == "explicit_conflict":
        with pytest.raises(ValueError, match="retained edge"):
            build_merge([fresh], path)
    else:
        result = build_merge([fresh], path)
        assert result.edges["a", "b"]["confidence_score"] == before["confidence_score"]


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_malformed_retained_edge_endpoints_preserve_recovery_warning(tmp_path, endpoint, capsys):
    edge = {"source": "a", "target": "b", endpoint: [], "relation": "supports"}
    path, _ = seed(tmp_path, [node("a"), node("b")], [edge])
    graph = build_merge([], path, root=tmp_path)
    assert set(graph) == {"a", "b"} and graph.number_of_edges() == 0
    assert "skipping edge with non-hashable endpoint" in capsys.readouterr().err
