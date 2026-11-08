"""Malformed and lost group evidence must not produce a replacement data seal."""
import base64
import copy
import json

import pytest

from graphify import canonical_replacement as replacement
from tests.test_canonical_replacement import case as case
from tests.test_replacement_review12 import _assert_cli_refusal
from tests.test_replacement_review14 import _group


@pytest.mark.parametrize("identity", [[], {}])
@pytest.mark.parametrize("valid_sibling", [False, True])
def test_profile_refuses_unhashable_group_identity(case, tmp_path, capsys, identity, valid_sibling):
    request, fragments = case
    _group(fragments[0], ["guide_one", "guide_two", "guide_three"])
    groups = fragments[0]["hyperedges"]
    if valid_sibling:
        groups.append(copy.deepcopy(groups[0]))
    groups[0]["id"] = identity
    _assert_cli_refusal(request, fragments, tmp_path, capsys)


@pytest.mark.parametrize("valid_sibling", [False, True])
def test_profile_refuses_group_loss_from_composition(case, tmp_path, capsys, monkeypatch, valid_sibling):
    request, fragments = case
    _group(fragments[0], ["guide_one", "guide_two", "guide_three"])
    if valid_sibling:
        fragments[0]["hyperedges"].append(copy.deepcopy(fragments[0]["hyperedges"][0]))
    original = build.build_from_json
    removed = []

    def lose_group(*args, **kwargs):
        graph = original(*args, **kwargs)
        removed.append(graph.graph["hyperedges"].pop())
        return graph

    monkeypatch.setattr(build, "build_from_json", lose_group)
    _assert_cli_refusal(request, fragments, tmp_path, capsys)
    assert len(removed) == 1


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
