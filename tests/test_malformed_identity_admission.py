"""Malformed identities cannot reach normalization, dedup or retained indexing."""
from copy import deepcopy
import json

import networkx as nx
import pytest

from graphify.build import build, build_from_json, build_merge
from graphify.export import attach_hyperedges, write_json
from graphify.validate import validate_extraction


BAD_IDENTITIES = [None, False, 0, 3, 1.5, [], [1], {}, {"bad": 1}]
BAD_GROUP_IDS = [[], [1], {}, {"bad": 1}]


def node(identity, source="stable.md", **attrs):
    return dict(id=identity, label=attrs.pop("label", str(identity)), file_type="document",
                source_file=source, **attrs)


def edge(source, target):
    return dict(source=source, target=target, relation="supports",
                confidence="EXTRACTED", source_file="stable.md")


def seed(tmp_path, data):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
@pytest.mark.parametrize("dedup", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_node_identity_admission_preserves_siblings(tmp_path, capsys, identity, dedup, retained):
    stable = [node("anchor", quotation="raw retained evidence"), node("beacon")]
    bad = node(identity, "broken.md")
    path = seed(tmp_path, {"nodes": stable + ([bad] if retained else []), "links": []})
    before = path.read_bytes()
    fresh = {"nodes": [node("fresh", "changed.md")] + ([] if retained else [bad])}
    original = deepcopy(fresh)
    graph = build_merge([fresh], path, root=tmp_path, dedup=dedup)
    assert set(graph) == {"anchor", "beacon", "fresh"}
    assert graph.nodes["anchor"] == {k: v for k, v in stable[0].items() if k != "id"}
    assert "skipping" in capsys.readouterr().err
    assert path.read_bytes() == before and fresh == original


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
@pytest.mark.parametrize("entry", ["direct", "full-dedup", "full-no-dedup"])
@pytest.mark.parametrize("remap", ["none", "semantic", "document"])
def test_full_node_admission_precedes_remaps(identity, entry, remap, capsys):
    nodes = [node("anchor")]
    expected = {"anchor"}
    if remap == "semantic":
        nodes.append(node("readme_topic", "docs/api/README.md"))
        expected.add("docs_api_readme_topic")
    elif remap == "document":
        nodes.extend([node("fresh", "changed.md"), node("fresh_doc", "changed.md")])
        expected.add("fresh_doc")
    data = {"nodes": [*nodes, node(identity, "broken.md")], "edges": []}
    if entry == "direct":
        graph = build_from_json(data)
    else:
        graph = build([data], dedup=entry == "full-dedup")
    assert set(graph) == expected
    assert "skipping node" in capsys.readouterr().err


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
@pytest.mark.parametrize("endpoint", ["source", "target"])
@pytest.mark.parametrize("dedup", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_edge_admission_preserves_valid_evidence(tmp_path, capsys, identity, endpoint, dedup, retained):
    good = edge("anchor", "beacon")
    bad = dict(good, **{endpoint: identity})
    path = seed(tmp_path, {"nodes": [node("anchor"), node("beacon")],
                          "links": [good] + ([bad] if retained else [])})
    before = path.read_bytes()
    fresh = {"nodes": [node("fresh", "changed.md")], "edges": [] if retained else [bad]}
    graph = build_merge([fresh], path, root=tmp_path, dedup=dedup)
    assert set(graph) == {"anchor", "beacon", "fresh"}
    assert list(graph.edges) == [("anchor", "beacon")]
    assert graph.edges["anchor", "beacon"] == dict(
        {k: v for k, v in good.items() if k not in {"source", "target"}},
        _src="anchor", _tgt="beacon")
    assert "skipping edge" in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
@pytest.mark.parametrize("endpoint", ["source", "target"])
@pytest.mark.parametrize("entry", ["direct", "full-dedup", "full-no-dedup"])
def test_full_edge_admission_precedes_dedup_and_semantic_remap(identity, endpoint, entry, capsys):
    data = {"nodes": [node("readme_topic", "docs/api/README.md"), node("beacon"),
                       node("zz_duplicate", "docs/api/README.md", label="readme_topic")],
            "edges": [edge("readme_topic", "beacon"),
                      dict(edge("readme_topic", "beacon"), **{endpoint: identity})]}
    graph = build_from_json(data) if entry == "direct" else build([data], dedup=entry == "full-dedup")
    assert graph.has_edge("docs_api_readme_topic", "beacon")
    assert graph.number_of_edges() == 1
    assert "skipping edge" in capsys.readouterr().err


@pytest.mark.parametrize("identity", BAD_GROUP_IDS)
@pytest.mark.parametrize("dedup", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_group_admission_is_consistent_and_preserves_payloads(tmp_path, capsys, identity, dedup, retained):
    valid = [{"nodes": ["anchor"], "quotation": "anonymous"},
             {"id": None, "nodes": ["anchor"]}, {"id": "", "nodes": ["anchor"]},
             *[{"id": value, "nodes": ["anchor"], "quotation": str(value)}
               for value in [0, False, 1, True, "group"]]]
    bad = {"id": identity, "nodes": ["anchor"]}
    path = seed(tmp_path, {"nodes": [node("anchor")], "links": [],
                          "hyperedges": [*valid, *([bad] if retained else [])]})
    before = path.read_bytes()
    fresh = {"nodes": [node("fresh", "changed.md")], "hyperedges": [] if retained else [bad]}
    original = deepcopy(fresh)
    graph = build_merge([fresh], path, root=tmp_path, dedup=dedup)
    assert graph.graph["hyperedges"] == valid
    assert "hyperedge with non-hashable id" in capsys.readouterr().err
    assert path.read_bytes() == before and fresh == original
    from io import StringIO
    stream = StringIO()
    write_json(graph, {}, stream, built_at_commit="test")
    assert json.loads(stream.getvalue())["hyperedges"] == valid


@pytest.mark.parametrize("identity", BAD_GROUP_IDS)
@pytest.mark.parametrize("entry", ["direct", "full", "attachment"])
def test_full_and_export_group_admission(identity, entry, capsys):
    groups = [{"id": identity, "nodes": ["anchor"]},
              {"nodes": ["anchor"]}, {"id": 0, "nodes": ["anchor"]},
              {"id": False, "nodes": ["anchor"]}]
    if entry == "attachment":
        graph = nx.Graph()
        graph.add_node("anchor")
        graph.graph["hyperedges"] = deepcopy(groups[:1])
        attach_hyperedges(graph, groups)
    else:
        data = {"nodes": [node("anchor")], "edges": [], "hyperedges": deepcopy(groups)}
        graph = build_from_json(data) if entry == "direct" else build([data])
    # Attachment historically requires an ID; construction accepts anonymous groups.
    assert graph.graph["hyperedges"] == (groups[2:] if entry == "attachment" else groups[1:])
    assert "hyperedge with non-hashable id" in capsys.readouterr().err


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
def test_validator_requires_strings_even_without_node_siblings(identity):
    errors = validate_extraction({"nodes": [node(identity)], "edges": [edge(identity, identity)]})
    assert any("id" in error and "must be a string" in error for error in errors)
    assert sum("must be a string" in error for error in errors) == 3


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
@pytest.mark.parametrize("dedup", [False, True])
def test_fresh_singleton_cannot_bypass_identity_admission(identity, dedup, capsys):
    graph = build([{"nodes": [node(identity)], "edges": []}], dedup=dedup)
    assert len(graph) == 0
    assert "skipping node" in capsys.readouterr().err


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
@pytest.mark.parametrize("dedup", [False, True])
def test_ast_refresh_admits_retained_identities_before_indexing(tmp_path, identity, dedup, capsys):
    stable = node("anchor", quotation="raw retained evidence")
    path = seed(tmp_path, {"nodes": [stable, node(identity, "broken.md")],
                          "links": [edge(identity, "anchor")],
                          "hyperedges": [{"id": [1], "nodes": ["anchor"]},
                                         {"id": False, "nodes": ["anchor"]}]})
    before = path.read_bytes()
    graph = build_merge([{"nodes": [node("fresh", "changed.py", _origin="ast")]}],
                        path, root=tmp_path, dedup=dedup, ast_refresh_sources=["changed.py"])
    assert set(graph) == {"anchor", "fresh"}
    assert graph.nodes["anchor"] == {k: v for k, v in stable.items() if k != "id"}
    assert graph.number_of_edges() == 0
    assert graph.graph["hyperedges"] == [{"id": False, "nodes": ["anchor"]}]
    assert capsys.readouterr().err.count("skipping") == 3
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["node", "edge", "group"])
def test_malformed_records_do_not_mask_accepted_conflicts(tmp_path, kind):
    group = {"id": "group", "nodes": ["anchor"], "quotation": "accepted"}
    path = seed(tmp_path, {"nodes": [node("anchor"), node("beacon")],
                          "links": [edge("anchor", "beacon")], "hyperedges": [group]})
    before = path.read_bytes()
    fresh = {"nodes": [node([1], "broken.md")],
             "edges": [edge(3, "anchor")], "hyperedges": [{"id": [1], "nodes": ["anchor"]}]}
    if kind == "node":
        fresh["nodes"].append(node("anchor", "changed.md", quotation="conflict"))
    elif kind == "edge":
        fresh["edges"].append(dict(edge("anchor", "beacon"), quotation="conflict"))
    else:
        fresh["hyperedges"].append(dict(group, quotation="conflict"))
    with pytest.raises(ValueError, match="conflicts with retained"):
        build_merge([fresh], path, root=tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("member", [[], [1], {}, {"bad": 1}])
@pytest.mark.parametrize("remap", ["semantic", "document"])
def test_malformed_members_do_not_crash_identity_remaps(member, remap):
    if remap == "semantic":
        nodes, raw, canonical = [node("readme_topic", "docs/api/README.md")], "readme_topic", "docs_api_readme_topic"
    else:
        nodes, raw, canonical = [node("fresh", "changed.md"), node("fresh_doc", "changed.md")], "fresh", "fresh_doc"
    graph = build_from_json({"nodes": nodes, "edges": [],
                             "hyperedges": [{"id": 3, "nodes": [raw, member]}]})
    assert graph.graph["hyperedges"] == [{"id": 3, "nodes": [canonical]}]


@pytest.mark.parametrize("data", [
    {"nodes": [{"label": "missing identity"}], "edges": []},
    {"nodes": [node("anchor")], "edges": [{"target": "anchor"}]},
    {"nodes": [node("anchor")]},
    {"edges": []},
])
@pytest.mark.parametrize("entry", ["direct", "full-no-dedup"])
def test_admission_preserves_missing_field_diagnostics(data, entry, capsys):
    if entry == "direct":
        build_from_json(deepcopy(data))
    else:
        build([deepcopy(data)], dedup=False)
    # build() has always supplied the top-level collection defaults.
    expected = entry == "direct" or "nodes" in data and "edges" in data
    assert ("Extraction warning" in capsys.readouterr().err) is expected


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
def test_raw_dedupe_admits_identities_before_indexing(identity, capsys):
    from graphify.build import dedupe_edges, dedupe_nodes
    stable = node("anchor")
    good = edge("anchor", "beacon")
    assert dedupe_nodes([stable, node(identity)]) == [stable]
    assert dedupe_edges([good, edge(identity, "beacon"), edge("anchor", identity)]) == [good]
    assert capsys.readouterr().err.count("skipping") == 3
    # Raw writers historically retain incomplete records for later diagnostics.
    assert dedupe_nodes([{}]) == []
    assert dedupe_edges([{"source": "anchor"}]) == [{"source": "anchor"}]


@pytest.mark.parametrize("identity", BAD_IDENTITIES)
def test_raw_reconciliation_admits_before_retained_indexing(tmp_path, identity, capsys):
    from graphify.watch import _check_shrink, _reconcile_existing_graph
    (tmp_path / "stable.md").write_text("accepted evidence")
    stable = [node("anchor", quotation="accepted"), node("beacon")]
    group = {"id": "group", "nodes": ["anchor", "beacon"]}
    path = seed(tmp_path, {"nodes": [*stable, node(identity, "broken.md")],
                          "links": [edge("anchor", "beacon"), edge(identity, "anchor")],
                          "hyperedges": [group, {"id": [1], "nodes": ["anchor"]}]})
    before = path.read_bytes()
    result, admitted = _reconcile_existing_graph(
        path, {"nodes": [node("fresh", "changed.md")], "edges": []},
        out=tmp_path, project_root=tmp_path, watch_root=tmp_path, code_files=[],
        extract_targets=[], full_rebuild=False, deleted_paths=set(), deleted_source_identities=set())
    assert result["nodes"] == [node("fresh", "changed.md"), *stable]
    assert result["edges"] == [edge("anchor", "beacon")]
    assert result["hyperedges"] == [group]
    assert _check_shrink(False, admitted, result, rebuilt_sources=set())
    raw = json.loads(before)
    assert _check_shrink(False, raw, {"nodes": stable}, rebuilt_sources=set())
    assert not _check_shrink(False, raw, {"nodes": stable[:1]}, rebuilt_sources=set())
    assert "skipping" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_rejected_identities_do_not_reach_other_field_normalization(capsys):
    graph = build_from_json({"nodes": [node("anchor"), {"id": [], "file_type": []}],
                             "edges": [{"source": [], "target": "anchor", "confidence": []}],
                             "hyperedges": [{"id": [], "nodes": ["anchor"], "source_file": []}]})
    assert set(graph) == {"anchor"} and not graph.edges
    assert not graph.graph.get("hyperedges")
    assert capsys.readouterr().err.count("skipping") == 3


def test_raw_reconciliation_missing_ids_do_not_hide_retained_siblings(tmp_path, capsys):
    from graphify.watch import _reconcile_existing_graph
    path = seed(tmp_path, {"nodes": [node("anchor"), {"label": "missing id"}], "links": []})
    (tmp_path / "stable.md").write_text("accepted evidence")
    before = path.read_bytes()
    result, _ = _reconcile_existing_graph(
        path, {"nodes": [node("fresh", "changed.md"), {"label": "missing id"}], "edges": []},
        out=tmp_path, project_root=tmp_path, watch_root=tmp_path, code_files=[],
        extract_targets=[], full_rebuild=False, deleted_paths=set(), deleted_source_identities=set())
    assert [n["id"] for n in result["nodes"]] == ["fresh", "anchor"]
    assert path.read_bytes() == before
    warnings = capsys.readouterr().err
    assert warnings.count("missing required field 'id'") == 2
    assert warnings.count("Extraction warning") == 2


@pytest.mark.parametrize("dedup", [False, True])
@pytest.mark.parametrize("ast_refresh", [False, True])
def test_retained_missing_node_id_warns_before_indexing(tmp_path, capsys, dedup, ast_refresh):
    missing = node("bad", "broken.md")
    missing.pop("id")
    path = seed(tmp_path, {"nodes": [node("anchor"), missing], "links": []})
    before = path.read_bytes()
    fresh = {"nodes": [node("fresh", "changed.md")], "edges": []}
    original = deepcopy(fresh)
    graph = build_merge([fresh], path, root=tmp_path, dedup=dedup,
                        ast_refresh_sources=["changed.md"] if ast_refresh else None)
    assert set(graph) == {"anchor", "fresh"}
    warnings = capsys.readouterr().err
    assert warnings.count("missing required field 'id'") == 1
    assert "Extraction warning" in warnings
    assert path.read_bytes() == before and fresh == original


@pytest.mark.parametrize("no_cluster", [False, True])
def test_public_update_warns_before_skipping_missing_node_id(tmp_path, monkeypatch, capsys, no_cluster):
    import graphify.__main__ as mainmod
    import graphify.extract as extractmod
    source = tmp_path / "app.py"
    source.write_text("def anchor(): return 1\n")
    before = source.read_bytes()
    original_extract = extractmod.extract
    supplied = []

    def extract_with_missing_id(*args, **kwargs):
        result = original_extract(*args, **kwargs)
        missing = dict(result["nodes"][-1], label="missing identity")
        missing.pop("id")
        result["nodes"].append(missing)
        supplied.append((result, deepcopy(result)))
        return result

    monkeypatch.setattr(extractmod, "extract", extract_with_missing_id)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "update", str(tmp_path)]
                        + (["--no-cluster"] if no_cluster else []))
    mainmod.main()
    warnings = capsys.readouterr().err
    assert warnings.count("missing required field 'id'") == 1
    assert "Extraction warning" in warnings
    graph = json.loads((tmp_path / "graphify-out" / "graph.json").read_text())
    assert graph["nodes"] and all(isinstance(n.get("id"), str) for n in graph["nodes"])
    assert all(n.get("label") != "missing identity" for n in graph["nodes"])
    assert source.read_bytes() == before
    assert all(result == original for result, original in supplied)


@pytest.mark.parametrize("record", ["malformed", [], ["other"]])
@pytest.mark.parametrize("entry", ["watch", "semantic", "ast"])
def test_indexing_admission_keeps_non_object_skip_behavior(tmp_path, capsys, record, entry):
    path = seed(tmp_path, {"nodes": [node("anchor"), record], "links": []})
    (tmp_path / "stable.md").write_text("accepted evidence")
    before = path.read_bytes()
    fresh = {"nodes": [node("fresh", "changed.md")], "edges": []}
    if entry == "watch":
        from graphify.watch import _reconcile_existing_graph
        fresh["nodes"].append(deepcopy(record))
        original = deepcopy(fresh)
        result, _ = _reconcile_existing_graph(
            path, fresh, out=tmp_path, project_root=tmp_path, watch_root=tmp_path,
            code_files=[], extract_targets=[], full_rebuild=False,
            deleted_paths=set(), deleted_source_identities=set())
        assert {n["id"] for n in result["nodes"]} == {"anchor", "fresh"}
        assert fresh == original
    else:
        graph = build_merge([fresh], path, root=tmp_path, dedup=False,
                            ast_refresh_sources=["changed.md"] if entry == "ast" else None)
        assert set(graph) == {"anchor", "fresh"}
    assert capsys.readouterr().err == ""
    assert path.read_bytes() == before


@pytest.mark.parametrize("data, field", [
    ({"nodes": {}, "edges": []}, "nodes"),
    ({"nodes": [], "edges": {}}, "edges"),
    ({"nodes": "", "edges": []}, "nodes"),
])
def test_admission_preserves_non_list_collection_diagnostics(data, field, capsys):
    build_from_json(data)
    assert f"'{field}' must be a list" in capsys.readouterr().err


@pytest.mark.parametrize("anonymous", [{"nodes": ["anchor"]},
                                       {"id": None, "nodes": ["anchor"]},
                                       {"id": "", "nodes": ["anchor"]}])
def test_attachment_skips_new_anonymous_groups_but_preserves_existing(anonymous):
    graph = nx.Graph()
    existing = deepcopy(anonymous)
    graph.graph["hyperedges"] = [existing]
    groups = [anonymous, {"id": 0, "nodes": ["anchor"]},
              {"id": False, "nodes": ["anchor"]}]
    before = deepcopy(groups)
    attach_hyperedges(graph, groups)
    attach_hyperedges(graph, groups)
    assert graph.graph["hyperedges"] == [existing, *groups[1:]]
    assert groups == before


@pytest.mark.parametrize("endpoint", ["source", "target"])
@pytest.mark.parametrize("dedup", [False, True])
def test_incremental_incomplete_edge_preserves_warning_and_valid_siblings(tmp_path, capsys, endpoint, dedup):
    path = seed(tmp_path, {"nodes": [node("anchor"), node("beacon")],
                          "links": [edge("anchor", "beacon")]})
    before = path.read_bytes()
    incomplete = edge("anchor", "beacon")
    incomplete.pop(endpoint)
    fresh = {"nodes": [node("fresh", "changed.md")], "edges": [incomplete]}
    original = deepcopy(fresh)
    graph = build_merge([fresh], path, root=tmp_path, dedup=dedup)
    assert set(graph) == {"anchor", "beacon", "fresh"}
    assert list(graph.edges) == [("anchor", "beacon")]
    assert f"missing required field '{endpoint}'" in capsys.readouterr().err
    assert path.read_bytes() == before and fresh == original


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_raw_reconciliation_preserves_fresh_incomplete_edges(tmp_path, existing, endpoint):
    from graphify.build import dedupe_edges
    from graphify.watch import _reconcile_existing_graph
    path = tmp_path / "graph.json"
    (tmp_path / "stable.md").write_text("accepted evidence")
    if existing:
        seed(tmp_path, {"nodes": [node("anchor")], "links": []})
    before = path.read_bytes() if existing else None
    incomplete = edge("fresh", "fresh")
    incomplete.pop(endpoint)
    fresh = {"nodes": [node("fresh", "changed.md")], "edges": [incomplete]}
    original = deepcopy(fresh)
    result, _ = _reconcile_existing_graph(
        path, fresh, out=tmp_path, project_root=tmp_path, watch_root=tmp_path,
        code_files=[], extract_targets=[], full_rebuild=False,
        deleted_paths=set(), deleted_source_identities=set())
    assert result["edges"] == [incomplete]
    assert dedupe_edges(result["edges"]) == [incomplete]
    assert {n["id"] for n in result["nodes"]} == ({"anchor", "fresh"} if existing else {"fresh"})
    assert fresh == original
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_public_no_cluster_update_preserves_incomplete_extraction_edges(tmp_path, monkeypatch, endpoint):
    import graphify.__main__ as mainmod
    import graphify.extract as extractmod
    (tmp_path / "app.py").write_text("def anchor(): return 1\n")
    original_extract = extractmod.extract
    incomplete = dict(relation="supports", confidence="EXTRACTED", source_file="app.py",
                      **{endpoint: "app_anchor"})

    def extract_with_incomplete_edge(*args, **kwargs):
        result = original_extract(*args, **kwargs)
        result["edges"].append(deepcopy(incomplete))
        return result

    monkeypatch.setattr(extractmod, "extract", extract_with_incomplete_edge)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "update", str(tmp_path), "--no-cluster"])
    mainmod.main()
    graph = json.loads((tmp_path / "graphify-out" / "graph.json").read_text())
    assert incomplete in graph["links"]
    assert graph["nodes"]

    # Replay against stored raw output after an unrelated file changes. Only
    # the first extraction supplies the diagnostic record.
    monkeypatch.setattr(extractmod, "extract", original_extract)
    (tmp_path / "other.py").write_text("def beacon(): return 2\n")
    mainmod.main()
    replay = json.loads((tmp_path / "graphify-out" / "graph.json").read_text())
    assert replay["links"].count(incomplete) == 1
    assert any(n.get("source_file") == "other.py" for n in replay["nodes"])


@pytest.mark.parametrize("links_key", ["links", "edges"])
@pytest.mark.parametrize("missing", [("source",), ("target",), ("source", "target")])
def test_raw_reconciliation_preserves_retained_incomplete_edges(tmp_path, links_key, missing):
    from graphify.watch import _reconcile_existing_graph
    (tmp_path / "stable.md").write_text("accepted evidence")
    incomplete = edge("anchor", "beacon")
    for endpoint in missing:
        incomplete.pop(endpoint)
    good = edge("anchor", "beacon")
    path = seed(tmp_path, {"nodes": [node("anchor"), node("beacon")],
                          links_key: [good, incomplete]})
    before = path.read_bytes()
    fresh = {"nodes": [node("fresh", "changed.md")], "edges": []}
    original = deepcopy(fresh)
    result, admitted = _reconcile_existing_graph(
        path, fresh, out=tmp_path, project_root=tmp_path, watch_root=tmp_path,
        code_files=[], extract_targets=[], full_rebuild=False,
        deleted_paths=set(), deleted_source_identities=set())
    assert admitted[links_key] == [good, incomplete]
    assert result["edges"] == [good, incomplete]
    assert {n["id"] for n in result["nodes"]} == {"anchor", "beacon", "fresh"}
    assert path.read_bytes() == before and fresh == original


@pytest.mark.parametrize("case", ["deleted", "replaced-ast", "dangling", "non-string", "from", "to"])
def test_retained_incomplete_edges_still_obey_eviction_and_identity_rules(tmp_path, case):
    from graphify.watch import _reconcile_existing_graph
    (tmp_path / "unrelated.md").write_text("accepted evidence")
    incomplete = edge("anchor", "beacon")
    incomplete.pop("target")
    if case == "replaced-ast":
        incomplete["_origin"] = "ast"
    elif case == "dangling":
        incomplete["source"] = "missing"
    elif case == "non-string":
        incomplete["source"] = []
    elif case == "from":
        incomplete["from"] = incomplete.pop("source")
    elif case == "to":
        incomplete["to"] = "beacon"
    good = dict(edge("anchor", "beacon"), source_file="unrelated.md")
    path = seed(tmp_path, {"nodes": [node("anchor", "unrelated.md"),
                                    node("beacon", "unrelated.md")],
                          "links": [good, incomplete]})
    before = path.read_bytes()
    result, _ = _reconcile_existing_graph(
        path, {"nodes": [], "edges": []}, out=tmp_path,
        project_root=tmp_path, watch_root=tmp_path, code_files=[],
        extract_targets=[tmp_path / "stable.md"] if case == "replaced-ast" else [],
        full_rebuild=False, deleted_paths=set(),
        deleted_source_identities={str(tmp_path / "stable.md")} if case == "deleted" else set())
    assert result["edges"] == [good]
    assert {n["id"] for n in result["nodes"]} == {"anchor", "beacon"}
    assert path.read_bytes() == before
