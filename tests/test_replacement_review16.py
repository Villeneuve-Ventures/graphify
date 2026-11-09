"""Malformed and lost group evidence must not produce a replacement data seal."""
import base64
import copy
import json

import pytest

from graphify import canonical_replacement as replacement
from tests.test_canonical_replacement import case as case
from tests.test_replacement_review14 import _group


@pytest.mark.parametrize("has_identity, identity", [
    (False, None), (True, None), (True, ""), (True, 0),
    (True, False), (True, 1.5), (True, "guide_flow"),
])
def test_profile_retains_hashable_and_anonymous_groups(case, has_identity, identity):
    request, fragments = case
    _group(fragments[0], ["guide_one", "guide_two", "guide_three"])
    group = fragments[0]["hyperedges"][0]
    if has_identity:
        group["id"] = identity
    else:
        group.pop("id")
    # Repeated/anonymous group records keep their original multiplicity.
    fragments[0]["hyperedges"].append(copy.deepcopy(group))
    seal = replacement.build_data(request, fragments)
    graph = json.loads(base64.b64decode(seal["payloads"]["graph.json"]))
    assert len(graph["hyperedges"]) == 2
    for admitted in graph["hyperedges"]:
        assert admitted["nodes"] == group["nodes"]
        assert ("id" in admitted) is has_identity
        if has_identity:
            assert admitted["id"] == identity
            assert type(admitted["id"]) is type(identity)
