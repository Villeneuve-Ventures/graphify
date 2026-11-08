"""Fragment nesting admission uses disposable inputs and never live maintenance."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case


def _fragment_text(fragments, location, depth):
    """Add nested metadata to a valid envelope without recursive test serialization."""
    fragment = fragments[0]
    if location in {"edge", "group"}:
        template = fragment["nodes"][0]
        fragment["nodes"].extend({**template, "id": name} for name in ("guide_beta", "guide_gamma"))
        relation = {"source_file": template["source_file"], "confidence": "EXTRACTED",
                    "confidence_score": 1.0}
        if location == "edge":
            fragment["edges"] = [{**relation, "source": template["id"], "target": "guide_beta",
                                  "relation": "references"}]
        else:
            fragment["hyperedges"] = [{**relation, "nodes": [template["id"], "guide_beta", "guide_gamma"],
                                       "relation": "participate_in"}]
    if location == "fragment":
        container = fragment
    elif location == "node":
        container = fragment["nodes"][0]
    elif location == "edge":
        container = fragment["edges"][0]
    else:
        container = fragment["hyperedges"][0]
    marker = "deep-fragment-placeholder"
    container["rationale"] = marker
    encoded = json.dumps(fragments)
    assert encoded.count(json.dumps(marker)) == 1
    return encoded.replace(json.dumps(marker), "[" * depth + "0" + "]" * depth)


@pytest.mark.parametrize("location", ["node", "edge", "group", "fragment"])
@pytest.mark.parametrize("overflow", [False, True])
def test_fragment_container_depth_boundary(case, location, overflow):
    request, fragments = case
    # Outer list + fragment object + record list + record object precede a
    # record attribute; a fragment attribute has only the first two containers.
    parents = 2 if location == "fragment" else 4
    depth = replacement.MAX_FRAGMENT_NESTING - parents + int(overflow)
    fragments = json.loads(_fragment_text(fragments, location, depth))
    original = replacement._bytes(fragments)
    before = replacement._tree(request["output"])
    if overflow:
        with pytest.raises(tx.PendingTransactionError, match="fragment nesting"):
            replacement.build_data(request, fragments)
    else:
        sealed = replacement.build_data(request, fragments)
        assert sealed["fragments"] == fragments
        assert replacement.preview(request, sealed)["seal_sha256"] == replacement._sha(replacement._bytes(sealed))
    assert replacement._bytes(fragments) == original
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists() and not Path(request["journal"]).exists()


def test_fragment_width_does_not_consume_depth_budget(case):
    request, fragments = case
    fragments[0]["metadata"] = [[0] for _ in range(replacement.MAX_FRAGMENT_NESTING + 1)]
    sealed = replacement.build_data(request, fragments)
    assert sealed["fragments"][0]["metadata"] == fragments[0]["metadata"]


def test_cyclic_fragment_attribute_is_refused_without_recursive_copy(case):
    request, fragments = case
    value = []
    value.append(value)
    fragments[0]["nodes"][0]["rationale"] = value
    before = replacement._tree(request["output"])
    with pytest.raises(tx.PendingTransactionError, match="fragment nesting"):
        replacement.build_data(request, fragments)
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists() and not Path(request["journal"]).exists()
