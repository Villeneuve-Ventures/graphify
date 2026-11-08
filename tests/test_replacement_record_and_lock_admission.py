"""Bounded regressions for replacement review findings, using modeled fixtures."""
import copy
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


@pytest.mark.parametrize("journal", [None, [], "journal", 1])
def test_status_nonobject_journal_is_structured_and_readonly(case, tmp_path, capsys, journal):
    request, _ = case
    path = Path(request["journal"])
    path.write_text(json.dumps(journal))
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    before = replacement._tree(request["output"])
    control_before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert replacement.main(["status", "--request", str(request_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert "journal must be an object" in result["error"]
    assert replacement._tree(request["output"]) == before
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == control_before


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


def test_quarantine_marker_name_refuses_before_maintenance(case, monkeypatch):
    request, _ = case
    before = replacement._tree(request["output"])

    def unexpected_maintenance(_request):
        pytest.fail("reserved quarantine reached maintenance")

    monkeypatch.setattr(replacement, "_maintenance", unexpected_maintenance)
    with pytest.raises(tx.PendingTransactionError, match="reserved replacement guard name"):
        replacement.rollback(request, "unused", Path(request["archive"]).parent / replacement.GUARD)
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()


def test_copied_control_parent_during_lock_read_cannot_start_rollback(case, modeled, tmp_path, monkeypatch):
    request, fragments = case
    control = tmp_path / "control"
    control.mkdir()
    raw = {key: copy.deepcopy(value) for key, value in request.items() if key != "parent_identities"}
    raw["journal"] = str(control / "journal.json")
    request = replacement.bind_request(raw)
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    journal = replacement._load(request["journal"])
    original_journal = Path(request["journal"]).read_bytes()
    before_output = replacement._tree(request["output"])
    before_archive = replacement._tree(request["archive"])
    quarantine_parent = tmp_path / "quarantines"
    quarantine_parent.mkdir()
    quarantine = quarantine_parent / "new-tree"
    real_read = replacement._read
    moved = tmp_path / "original-control"
    swaps = []

    def swap_on_binding_read(path, *args, **kwargs):
        if Path(path).name.startswith(".replacement-lock-") and not swaps:
            control.rename(moved)
            shutil.copytree(moved, control)
            swaps.append(True)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(replacement, "_read", swap_on_binding_read)
    with pytest.raises(tx.PendingTransactionError, match="changed"):
        replacement.rollback(request, replacement._sha(replacement._bytes(journal)), quarantine,
                             expected_new_tree=replacement._sha(replacement._bytes(before_output)))
    assert swaps == [True]
    assert replacement._tree(request["output"]) == before_output
    assert replacement._tree(request["archive"]) == before_archive
    assert (moved / "journal.json").read_bytes() == original_journal
    assert Path(request["journal"]).read_bytes() == original_journal
    assert list(quarantine_parent.iterdir()) == []
