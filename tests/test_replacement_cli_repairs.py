"""CLI refusal regressions preserve disposable data without live maintenance."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from graphify import canonical_replacement as replacement
from tests.test_canonical_replacement import case as case
from tests.test_replacement_admission import request_case as request_case


def _build_args(tmp_path, request, fragments):
    request_path, fragment_path = tmp_path / "request.json", tmp_path / "fragments.json"
    request_path.write_text(json.dumps(request))
    fragment_path.write_text(json.dumps(fragments))
    return ["build-data", "--request", str(request_path), "--fragments", str(fragment_path)]


def _assert_unpublished(request, old_tree):
    assert replacement._tree(request["output"]) == old_tree
    assert not Path(request["stage"]).exists()
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()


@pytest.mark.parametrize("role,state", [("archive", "ROLLED_BACK_UNRECOVERED"), ("target", "PREPARED")])
def test_build_data_preserves_registered_absent_stage(request_case, tmp_path, capsys, role, state):
    stage = Path(request_case["stage"])
    old_journal = tmp_path / "retained-operation.json"
    old_journal.write_bytes(replacement._bytes({"request_digest": "prior", "state": state}))
    replacement._register(stage, old_journal, "prior", role)
    marker = stage.parent / replacement.GUARD
    before = old_journal.read_bytes(), marker.read_bytes(), replacement._tree(request_case["output"])

    assert replacement.main(_build_args(tmp_path, request_case, [])) == 1

    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "archive" in response["error"] if role == "archive" else "incomplete" in response["error"]
    assert (old_journal.read_bytes(), marker.read_bytes()) == before[:2]
    _assert_unpublished(request_case, before[2])


def test_build_data_cli_reports_source_byte_refusal(request_case, tmp_path):
    oversized = Path(request_case["root"]) / "oversized.py"
    with oversized.open("wb") as stream:
        stream.truncate(replacement.source.BOUNDS["max_file_bytes"] + 1)
    before = replacement._tree(request_case["output"])
    args = _build_args(tmp_path, request_case, [])

    result = subprocess.run([sys.executable, "-B", "-m", "graphify", "replacement", *args],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)

    _assert_unpublished(request_case, before)
    assert result.returncode == 1
    assert not result.stderr, result.stderr
    response = json.loads(result.stdout)
    assert response["status"] == "blocked"
    assert "input byte limit exceeded" in response["error"]


@pytest.mark.parametrize("field", ["edges", "hyperedges"])
def test_build_data_reports_oversized_integer_confidence(case, tmp_path, capsys, field):
    request, fragments = case
    fragment = fragments[0]
    original = fragment["nodes"][0]
    fragment["nodes"] = [{**original, "id": f"guide_{index}", "label": f"Guide {index}"}
                         for index in range(3)]
    record = {"source_file": original["source_file"], "confidence": "EXTRACTED", "confidence_score": 1.0}
    if field == "edges":
        record.update(source="guide_0", target="guide_1", relation="conceptually_related_to")
    else:
        record.update(id="guide_flow", nodes=[node["id"] for node in fragment["nodes"]], relation="participate_in")
    fragment[field] = [record]
    assert replacement.build_data(request, fragments)["producer"] == replacement.PROFILE
    record["confidence_score"] = 10**1000
    before = replacement._tree(request["output"])

    assert replacement.main(_build_args(tmp_path, request, fragments)) == 1

    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "too large" in response["error"]
    _assert_unpublished(request, before)
