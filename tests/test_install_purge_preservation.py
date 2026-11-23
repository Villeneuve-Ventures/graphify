"""Purge preservation uses disposable trees and modeled maintenance only."""
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled
from tests.test_replacement_bounded_repairs import uninstall as uninstall


@pytest.mark.parametrize("nested", [False, True])
def test_purge_preserves_archive_below_ordinary_output(case, modeled, tmp_path, uninstall, nested):
    original_request, fragments = case
    project = tmp_path / "consumer"
    output = project / "graphify-out"
    archive_parent = output / "retained" if nested else output
    archive_parent.mkdir(parents=True)
    (output / "ordinary.txt").write_bytes(b"ordinary output must also survive refusal")
    request = {key: value for key, value in original_request.items() if key != "parent_identities"}
    request["archive"] = str(archive_parent / "old-graph")
    request = replacement.bind_request(request)
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    assert replacement.execute(request, seal, plan["preview_sha256"])["status"] == "complete"
    archive = Path(request["archive"])
    journal = Path(request["journal"])
    before = replacement._tree(output), archive.stat().st_ino, journal.read_bytes()

    with pytest.raises(tx.PendingTransactionError, match="registered replacement"):
        uninstall(project, purge=True)

    assert replacement._tree(output) == before[0]
    assert archive.stat().st_ino == before[1]
    assert journal.read_bytes() == before[2]


@pytest.mark.parametrize("nested", [False, True])
def test_purge_preserves_displaced_guard_evidence(tmp_path, uninstall, nested):
    project = tmp_path / "consumer"
    output = project / "graphify-out"
    evidence_parent = output / "retained" if nested else output
    evidence_parent.mkdir(parents=True)
    evidence = evidence_parent / ("." + replacement.GUARD + ".graphify-merge-backup." + "a" * 32)
    evidence.write_bytes(b"retain displaced guard evidence")
    (output / "ordinary.txt").write_bytes(b"ordinary output must also survive refusal")
    before = replacement._tree(output)

    with pytest.raises(tx.PendingTransactionError, match="registered replacement or recovery record"):
        uninstall(project, purge=True)

    assert replacement._tree(output) == before
    assert evidence.read_bytes() == b"retain displaced guard evidence"
