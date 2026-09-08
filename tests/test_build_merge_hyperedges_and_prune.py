"""Incremental --update: hyperedge preservation (#1574) and root-less prune (#1571).

build_merge backs `graphify --update`. Two regressions covered here:

- #1574: it read only nodes+edges from the existing graph.json, never hyperedges,
  so every incremental update collapsed the graph's hyperedge set down to just the
  re-extracted files'. Now existing hyperedges are carried forward, with
  re-extracted files' replaced (by source_file) and deleted files' pruned.
- #1571: when a caller omits `root` (the skill's --update runbook does), absolute
  prune_sources never relativized to match the stored relative source_file keys, so
  deleted files' nodes survived as ghosts. build_merge now infers a fallback root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify.build import build_merge, _infer_merge_root


def _write_graph(graph_path: Path, nodes, edges, hyperedges) -> None:
    """Write a graph.json in the shape to_json emits (top-level hyperedges)."""
    graph_path.write_text(
        json.dumps({"nodes": nodes, "edges": edges, "hyperedges": hyperedges}),
        encoding="utf-8",
    )


def _he_ids(G) -> set[str]:
    return {h["id"] for h in G.graph.get("hyperedges", [])}


# ── #1574: hyperedge preservation ─────────────────────────────────────────────

def _seed_two_file_graph(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    graph_path = tmp_path / "graph.json"
    nodes = [
        {"id": "a1", "label": "a1", "file_type": "document", "source_file": "a.md"},
        {"id": "b1", "label": "b1", "file_type": "document", "source_file": "b.md"},
    ]
    hyperedges = [
        {"id": "he_a", "label": "flow A", "source_file": "a.md", "nodes": ["a1"]},
        {"id": "he_b", "label": "flow B", "source_file": "b.md", "nodes": ["b1"]},
        {"id": "he_global", "label": "cross-file flow", "nodes": ["a1", "b1"]},  # no source_file
    ]
    _write_graph(graph_path, nodes, [], hyperedges)
    return root, graph_path


def test_update_preserves_hyperedges_of_unchanged_files(tmp_path):
    root, graph_path = _seed_two_file_graph(tmp_path)
    # Re-extract only b.md, with a fresh hyperedge for it.
    new_chunk = {
        "nodes": [{"id": "b1", "label": "b1", "file_type": "document", "source_file": "b.md"}],
        "edges": [],
        "hyperedges": [{"id": "he_b_v2", "label": "flow B v2", "source_file": "b.md", "nodes": ["b1"]}],
    }
    G = build_merge([new_chunk], graph_path, dedup=False, root=root)
    ids = _he_ids(G)
    assert "he_a" in ids           # unchanged file's hyperedge preserved (the bug)
    assert "he_global" in ids      # source_file-less hyperedge preserved
    assert "he_b_v2" in ids        # re-extracted file's new hyperedge present
    assert "he_b" not in ids       # re-extracted file's OLD hyperedge replaced


def test_update_without_root_still_preserves_hyperedges(tmp_path):
    """The runbook omits root; the fallback root must not break preservation."""
    root, graph_path = _seed_two_file_graph(tmp_path)
    new_chunk = {
        "nodes": [{"id": "b1", "label": "b1", "file_type": "document", "source_file": "b.md"}],
        "edges": [],
        "hyperedges": [{"id": "he_b_v2", "source_file": "b.md", "nodes": ["b1"]}],
    }
    G = build_merge([new_chunk], graph_path, dedup=False)  # no root
    ids = _he_ids(G)
    assert {"he_a", "he_global", "he_b_v2"} <= ids
    assert "he_b" not in ids


def test_deleted_file_hyperedges_are_pruned(tmp_path):
    root, graph_path = _seed_two_file_graph(tmp_path)
    deleted_abs = [str(root / "a.md")]
    G = build_merge([], graph_path, prune_sources=deleted_abs, dedup=False, root=root)
    ids = _he_ids(G)
    assert "he_a" not in ids        # deleted file's hyperedge pruned
    assert "he_b" in ids            # untouched file's hyperedge kept
    assert "he_global" in ids       # global hyperedge kept
    # and its node is gone too
    assert "a1" not in set(G.nodes)


# ── #1571: root-less prune (absolute deleted paths vs relative node keys) ──────

def test_prune_without_root_removes_ghost_nodes_via_grandparent_fallback(tmp_path):
    root = tmp_path / "corpus"
    (root / "graphify-out").mkdir(parents=True)
    graph_path = root / "graphify-out" / "graph.json"
    nodes = [
        {"id": "h1", "label": "handoff", "file_type": "document", "source_file": "HANDOFF.md"},
        {"id": "k1", "label": "keep", "file_type": "document", "source_file": "KEEP.md"},
    ]
    _write_graph(graph_path, nodes, [], [])
    # Runbook-style call: absolute prune path, NO root passed.
    deleted_abs = [str(root / "HANDOFF.md")]
    G = build_merge([], graph_path, prune_sources=deleted_abs, dedup=False)
    labels = {d["label"] for _, d in G.nodes(data=True)}
    assert "handoff" not in labels, "deleted file's ghost node must be pruned without root"
    assert "keep" in labels


def test_prune_without_root_uses_graphify_root_marker(tmp_path):
    # graph.json not under a <root>/graphify-out layout, so grandparent wouldn't
    # help — the committed .graphify_root marker must be honored instead.
    out = tmp_path / "out"
    out.mkdir()
    graph_path = out / "graph.json"
    real_root = tmp_path / "elsewhere" / "repo"
    real_root.mkdir(parents=True)
    (out / ".graphify_root").write_text(str(real_root), encoding="utf-8")
    nodes = [{"id": "h1", "label": "handoff", "file_type": "document", "source_file": "HANDOFF.md"}]
    _write_graph(graph_path, nodes, [], [])
    assert _infer_merge_root(graph_path) == str(real_root.resolve())
    G = build_merge([], graph_path, prune_sources=[str(real_root / "HANDOFF.md")], dedup=False)
    assert "handoff" not in {d["label"] for _, d in G.nodes(data=True)}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_prune_matches_across_symlinked_root(tmp_path):
    """A symlinked scan root (macOS /var -> /private/var, symlinked home/worktree)
    makes the absolute prune path and the resolved root differ by prefix. The prune
    must still match — lexical relative_to fails, so normalization resolves both
    sides. Regression for the edge case a canonical-tmp unit test can't reach."""
    real = tmp_path / "real"
    (real / "graphify-out").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    graph_path = real / "graphify-out" / "graph.json"
    _write_graph(graph_path, [
        {"id": "h1", "label": "handoff", "file_type": "document", "source_file": "HANDOFF.md"},
        {"id": "k1", "label": "keep", "file_type": "document", "source_file": "KEEP.md"},
    ], [], [])
    # prune path addressed via the SYMLINK, root resolved to the real dir
    G = build_merge([], graph_path=graph_path,
                    prune_sources=[str(link / "HANDOFF.md")], root=str(real), dedup=False)
    labels = {d["label"] for _, d in G.nodes(data=True)}
    assert "handoff" not in labels and "keep" in labels


def test_reextracted_file_in_prune_sources_is_not_deleted(tmp_path):
    """#1796: a file present in BOTH new_chunks (re-extracted) and prune_sources
    must be REPLACED, not deleted. The old edit-workflow passed the changed file
    in prune_sources; combined with dedup keeping a same-label node, that used to
    silently delete the freshly re-extracted concept. Replace wins over delete."""
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    _write_graph(
        graph_path,
        nodes=[
            {"id": "foo_widget_cache", "label": "Widget Cache Design",
             "file_type": "concept", "source_file": "docs/foo.md", "source_location": "L1"},
            {"id": "bar_other", "label": "Other",
             "file_type": "concept", "source_file": "docs/bar.md", "source_location": "L1"},
        ],
        edges=[],
        hyperedges=[],
    )
    # foo.md edited: same-label node re-extracted (new content/line)
    new_chunk = {"nodes": [
        {"id": "foo_widget_cache", "label": "Widget Cache Design",
         "file_type": "concept", "source_file": "docs/foo.md", "source_location": "L2"}
    ], "edges": []}

    G = build_merge([new_chunk], graph_path=str(graph_path),
                    prune_sources=["docs/foo.md"], root=str(tmp_path))
    labels = {G.nodes[n].get("label") for n in G.nodes()}
    assert "Widget Cache Design" in labels, "re-extracted node was wrongly pruned"


def test_genuine_deletion_still_prunes(tmp_path):
    """#1796 guard must not break real deletions: a file in prune_sources but NOT
    in new_chunks is still removed."""
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    _write_graph(
        graph_path,
        nodes=[
            {"id": "foo_widget_cache", "label": "Widget Cache Design",
             "file_type": "concept", "source_file": "docs/foo.md", "source_location": "L1"},
            {"id": "bar_other", "label": "Other",
             "file_type": "concept", "source_file": "docs/bar.md", "source_location": "L1"},
        ],
        edges=[],
        hyperedges=[],
    )
    new_chunk = {"nodes": [
        {"id": "foo_widget_cache", "label": "Widget Cache Design",
         "file_type": "concept", "source_file": "docs/foo.md", "source_location": "L2"}
    ], "edges": []}
    # bar.md genuinely deleted (not re-extracted)
    G = build_merge([new_chunk], graph_path=str(graph_path),
                    prune_sources=["docs/bar.md"], root=str(tmp_path))
    labels = {G.nodes[n].get("label") for n in G.nodes()}
    assert "Other" not in labels, "genuinely deleted file's node should be pruned"
    assert "Widget Cache Design" in labels


def _ast_refresh_seed(tmp_path):
    graph = tmp_path / "graph.json"
    ast = {"id": "symbol", "label": "Symbol", "file_type": "code",
           "source_file": "module.py", "_origin": "ast"}
    fact = {"id": "fact", "label": "Accepted invariant", "file_type": "concept",
            "source_file": "module.py", "quotation": "retained bytes", "confidence": "EXTRACTED"}
    global_fact = {"id": "global", "label": "Global accepted assertion", "custom": {"value": 3}}
    edge = {"source": "fact", "target": "symbol", "relation": "describes",
            "source_file": "module.py", "confidence": "EXTRACTED", "quotation": "retained bytes"}
    hyperedge = {"id": "bundle", "nodes": ["fact", "symbol", "global"],
                 "source_file": "module.py", "custom": ["do", "not", "normalize"]}
    _write_graph(graph, [ast, fact, global_fact], [edge], [hyperedge])
    return graph, ast, fact, global_fact, edge, hyperedge


def test_ast_refresh_preserves_unchanged_semantic_payloads(tmp_path):
    graph, ast, fact, global_fact, edge, hyperedge = _ast_refresh_seed(tmp_path)
    result = build_merge([{"nodes": [ast], "edges": []}], graph, root=tmp_path,
                         ast_refresh_sources=[], directed=True)
    assert result.is_directed() and not result.has_edge("symbol", "fact")
    assert dict(result.nodes["fact"]) == {k: v for k, v in fact.items() if k != "id"}
    assert dict(result.nodes["global"]) == {k: v for k, v in global_fact.items() if k != "id"}
    assert all(result.edges["fact", "symbol"].get(k) == v
               for k, v in edge.items() if k not in {"source", "target"})
    assert result.graph["hyperedges"] == [hyperedge]


@pytest.mark.parametrize("removed", ["changed", "deleted"])
def test_ast_refresh_removes_only_authorized_source_contributions(tmp_path, removed):
    graph, ast, *_ = _ast_refresh_seed(tmp_path)
    result = build_merge([{"nodes": [ast] if removed == "changed" else [], "edges": []}],
                         graph, root=tmp_path,
                         ast_refresh_sources=[str(tmp_path / "module.py")] if removed == "changed" else [],
                         prune_sources=[str(tmp_path / "module.py")])
    assert "fact" not in result
    assert "global" in result
    assert ("symbol" in result) == (removed == "changed")
    assert not result.graph.get("hyperedges")


@pytest.mark.parametrize("conflict", ["endpoint", "node", "edge"])
def test_ast_refresh_refuses_retained_identity_conflicts(tmp_path, conflict):
    graph, ast, *_ = _ast_refresh_seed(tmp_path)
    fresh = {"nodes": [ast], "edges": []}
    if conflict == "endpoint":
        fresh["nodes"] = []
    elif conflict == "node":
        fresh["nodes"].append({"id": "fact", "label": "Different AST object", "_origin": "ast"})
    else:
        fresh["nodes"].append({"id": "fact", "label": "Accepted invariant", "file_type": "concept",
                               "source_file": "module.py", "quotation": "retained bytes", "confidence": "EXTRACTED"})
        fresh["edges"] = [{"source": "fact", "target": "symbol", "relation": "calls", "_origin": "ast"}]
    original = graph.read_bytes()
    with pytest.raises(ValueError, match={"endpoint": "endpoint", "node": "node identity", "edge": "edge slot"}[conflict]):
        build_merge([fresh], graph, root=tmp_path, ast_refresh_sources=[])
    assert graph.read_bytes() == original


@pytest.mark.parametrize("owners", [(1, 2), (2, 1)])
def test_ast_refresh_reference_owner_transitions_match_full(tmp_path, owners):
    from graphify.build import build
    from graphify.extract import extract
    from graphify.export import to_json

    paths = []
    for name in ("first", "second"):
        path = tmp_path / f"{name}.py"
        path.write_text(f"from typing import Any\ndef {name}(value: Any):\n    return value\n")
        paths.append(path)
    graph = tmp_path / "graph.json"
    initial = build([extract(paths[:owners[0]], cache_root=tmp_path, parallel=False)], root=tmp_path)
    to_json(initial, {}, str(graph))
    fresh = extract(paths[:owners[1]], cache_root=tmp_path, parallel=False)
    expected = build([fresh], root=tmp_path)
    actual = build_merge([fresh], graph, root=tmp_path, ast_refresh_sources=[],
                         prune_sources=[str(paths[1])] if owners[1] == 1 else None)
    assert set(actual) == set(expected)
    def records(graph):
        return {(attrs["_src"], attrs["_tgt"], attrs.get("relation"), attrs.get("confidence"),
                 attrs.get("source_file"), attrs.get("source_location"), attrs.get("_origin"))
                for _, _, attrs in graph.edges(data=True)}
    assert records(actual) == records(expected)


@pytest.mark.parametrize("backend", ["gemini", None])
def test_ast_refresh_explicit_dedup_backend_preserves_retained(tmp_path, monkeypatch, backend):
    from copy import deepcopy
    from graphify import dedup
    from graphify.build import build

    graph, ast, fact, global_fact, edge, hyperedge = _ast_refresh_seed(tmp_path)
    rationale = {"id": "rationale", "label": "Quasar indexing balances recovery latency",
                 "file_type": "rationale", "source_file": "module.py", "_origin": "ast"}
    concept = {"id": "concept", "label": "Zephyr transactions preserve durable provenance",
               "file_type": "concept", "source_file": "module.py", "_origin": "ast"}
    fresh = {"nodes": [ast, rationale, concept], "edges": [
        {"source": "rationale", "target": "symbol", "relation": "explains", "_origin": "ast"}]}
    calls = []

    def tiebreak(candidates, uf, communities, *, backend):
        assert {node["id"] for node in candidates} == {"rationale", "concept"}
        assert all(node["_origin"] == "ast" for node in candidates)
        assert uf.find("rationale") != uf.find("concept")
        calls.append(backend)
        uf.union("rationale", "concept")

    monkeypatch.setattr(dedup, "_llm_tiebreak", tiebreak)
    expected = build([deepcopy(fresh)], root=tmp_path, dedup_llm_backend=backend)
    assert calls == ([backend] if backend is not None else [])
    assert len(expected) == (2 if backend is not None else 3)
    calls.clear()
    before = graph.read_bytes()
    actual = build_merge([deepcopy(fresh)], graph, root=tmp_path,
                         ast_refresh_sources=[], dedup_llm_backend=backend)
    assert calls == ([backend] if backend is not None else [])
    actual_fresh = actual.subgraph(expected.nodes)
    assert {n: dict(a) for n, a in actual_fresh.nodes(data=True)} == dict(expected.nodes(data=True))
    assert list(actual_fresh.edges(data=True)) == list(expected.edges(data=True))
    assert set(actual) == set(expected) | {"fact", "global"}
    assert dict(actual.nodes["fact"]) == {k: v for k, v in fact.items() if k != "id"}
    assert dict(actual.nodes["global"]) == {k: v for k, v in global_fact.items() if k != "id"}
    assert dict(actual.edges["fact", "symbol"]) == {
        **{k: v for k, v in edge.items() if k not in {"source", "target"}},
        "_src": "fact", "_tgt": "symbol"}
    assert actual.graph["hyperedges"] == [hyperedge]
    assert graph.read_bytes() == before
