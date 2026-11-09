"""Profile cardinality and guard modes use disposable, synthetic custody fixtures."""
import base64
import json


from graphify import canonical_replacement as replacement
from tests.test_canonical_replacement import case as case
from tests.test_replacement_guard_issuers import protected_policy as protected_policy


def _group(fragment, members):
    node = fragment["nodes"][0]
    fragment["nodes"] = [{**node, "id": identity, "label": identity}
                         for identity in ("guide_one", "guide_two", "guide_three")]
    fragment["hyperedges"] = [{"id": "guide_flow", "nodes": members,
        "source_file": node["source_file"], "relation": "participate_in",
        "confidence": "EXTRACTED", "confidence_score": 1.0}]


def test_profile_keeps_three_distinct_group_members(case):
    request, fragments = case
    _group(fragments[0], ["guide_one", "guide_two", "guide_three"])
    seal = replacement.build_data(request, fragments)
    graph = json.loads(base64.b64decode(seal["payloads"]["graph.json"]))
    assert graph["hyperedges"][0]["nodes"] == ["guide_one", "guide_two", "guide_three"]
