"""Replacement node admission refuses malformed or foreign canonical identities."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case


def _fragment(path, identities):
    return {"backend": "codex_host_agent", "network_egress": False,
            "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()},
            "nodes": [{"id": identity, "label": "Alpha", "file_type": "concept",
                       "source_file": str(path)} for identity in identities],
            "edges": [], "hyperedges": []}


@pytest.mark.parametrize("directed", [False, True])
def test_semantic_rekey_cannot_shadow_an_ast_identity(case, directed):
    request, fragments = case
    request["directed"] = directed
    root = Path(request["root"])
    docs = root / "docs"
    docs.mkdir()
    (root / "a.py").rename(docs / "a.py")
    document = docs / "a.md"
    document.write_text("Alpha is a documented concept.\n")
    fragments.append(_fragment(document, ["a_alpha"]))
    original = copy.deepcopy(fragments)
    before = replacement._tree(request["output"])

    with pytest.raises(tx.PendingTransactionError, match="cross-source node identity"):
        replacement.build_data(request, fragments)

    assert fragments == original
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists() and not Path(request["journal"]).exists()


@pytest.mark.parametrize("directed", [False, True])
def test_semantic_rekey_refuses_collision_even_when_both_sources_survive(case, directed):
    request, fragments = case
    request["directed"] = directed
    root = Path(request["root"])
    docs = root / "docs"
    docs.mkdir()
    first, second = docs / "b.md", docs / "b.txt"
    first.write_text("First alpha concept.\n")
    second.write_text("Second alpha concept.\n")
    # Different raw IDs converge on docs_b_alpha. Each source also retains a
    # distinct node, so source-presence checks alone cannot detect the loss.
    fragments.extend([_fragment(first, ["b_alpha", "docs_b_first"]),
                      _fragment(second, ["docs_b_alpha", "docs_b_second"])])
    original = copy.deepcopy(fragments)
    before = replacement._tree(request["output"])

    with pytest.raises(tx.PendingTransactionError, match="cross-source node identity"):
        replacement.build_data(request, fragments)

    assert fragments == original
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists() and not Path(request["journal"]).exists()


def test_semantic_rekey_keeps_same_source_merges_and_original_fragments(case):
    request, fragments = case
    docs = Path(request["root"]) / "docs"
    docs.mkdir()
    document = docs / "alpha.md"
    document.write_text("Alpha has one canonical identity.\n")
    fragments.append(_fragment(document, ["alpha_item", "docs_alpha_item"]))
    original = copy.deepcopy(fragments)

    sealed = replacement.build_data(request, fragments)

    graph = json.loads(base64.b64decode(sealed["payloads"]["graph.json"]))
    nodes = [node for node in graph["nodes"] if node["source_file"] == "docs/alpha.md"]
    assert len(nodes) == 1
    assert nodes[0]["id"] == "docs_alpha_item" and nodes[0]["label"] == "Alpha"
    assert fragments == original


@pytest.mark.parametrize("label", [None, False, 0, [], {}])
def test_semantic_node_label_must_be_text(case, label):
    request, fragments = case
    fragments[0]["nodes"][0]["label"] = label
    original = copy.deepcopy(fragments)
    before = replacement._tree(request["output"])

    with pytest.raises(tx.PendingTransactionError, match="semantic node label"):
        replacement.build_data(request, fragments)

    assert fragments == original
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists() and not Path(request["journal"]).exists()


def test_semantic_node_requires_label_before_sealing(case):
    request, fragments = case
    del fragments[0]["nodes"][0]["label"]
    before = replacement._tree(request["output"])

    with pytest.raises(tx.PendingTransactionError, match="semantic node label"):
        replacement.build_data(request, fragments)

    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists() and not Path(request["journal"]).exists()
