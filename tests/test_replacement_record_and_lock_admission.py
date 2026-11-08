"""Bounded regressions for replacement review findings, using modeled fixtures."""
import copy
import json
from pathlib import Path
import shutil

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def _relationship(fragment, field, relation, confidence="INFERRED", score=0.75):
    fragment["nodes"] += [dict(fragment["nodes"][0], id=name, label=name)
                          for name in ("guide_beta", "guide_gamma")]
    record = {"relation": relation, "confidence": confidence, "confidence_score": score,
              "source_file": fragment["nodes"][0]["source_file"]}
    if field == "edges":
        record.update(source="guide_alpha", target="guide_beta")
    else:
        record["nodes"] = [node["id"] for node in fragment["nodes"]]
    fragment[field] = [record]


@pytest.mark.parametrize("field,relation,confidence,score", [
    ("edges", "participate_in", "INFERRED", 0.75),
    ("hyperedges", "calls", "INFERRED", 0.75),
    ("hyperedges", "form", "AMBIGUOUS", 0.2),
])
def test_relationship_kind_uses_its_own_host_rubric(case, field, relation, confidence, score):
    request, fragments = case
    _relationship(fragments[0], field, relation, confidence, score)
    before = replacement._tree(request["output"])
    with pytest.raises(tx.PendingTransactionError, match="semantic (relation|confidence)"):
        replacement.build_data(request, fragments)
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()


@pytest.mark.parametrize("field,relation,confidence,score", [
    ("edges", "calls", "AMBIGUOUS", 0.2),
    ("hyperedges", "participate_in", "INFERRED", 0.75),
])
def test_relationship_kind_retains_valid_host_records(case, field, relation, confidence, score):
    request, fragments = case
    _relationship(fragments[0], field, relation, confidence, score)
    assert replacement.build_data(request, fragments)["request_sha256"] == replacement._sha(replacement._bytes(request))


@pytest.mark.parametrize("field", ["archive", "stage", "journal"])
def test_external_slot_cannot_equal_registration_marker(case, field):
    request, _ = case
    before = replacement._tree(request["output"])
    external = Path(request[field]).parent
    before_entries = sorted(p.name for p in external.iterdir())
    raw = {key: value for key, value in request.items() if key != "parent_identities"}
    raw[field] = str(external / replacement.GUARD)
    with pytest.raises(tx.PendingTransactionError, match="reserved replacement guard name"):
        replacement.bind_request(raw)
    assert replacement._tree(request["output"]) == before
    assert sorted(p.name for p in external.iterdir()) == before_entries
