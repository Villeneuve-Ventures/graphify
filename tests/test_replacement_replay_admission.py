"""Bounded replay admission and retained-object reporting on fixture roots."""
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def test_preview_refuses_json_escaped_inventory_over_budget(case, monkeypatch):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    record = {"device": 1, "inode": 100000, "mode": 33188, "sha256": "0" * 64}
    tree = {"\x01" * 240 + f"/f{number:05d}": dict(record, inode=100000 + number)
            for number in range(16)}
    monkeypatch.setattr(replacement, "_tree", lambda _: tree)
    monkeypatch.setattr(replacement, "MAX_BYTES", 16 * 1024)
    monkeypatch.setattr(replacement, "_FORWARD_JOURNAL_RESERVE", 128)

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement.preview(request, sealed)

    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert (Path(request["output"]) / "graph.json").read_bytes() == b'{"old":true}'


@pytest.mark.parametrize("remaining", [0, -1])
def test_preview_complete_journal_budget_exact_boundary(case, monkeypatch, remaining):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    initial = {**plan, "request_digest": replacement._sha(replacement._bytes(request)),
               "state": "PREPARED", "new_identity": None, "container": None, "owner": None}
    limit = len(replacement._bytes(initial)) + replacement._FORWARD_JOURNAL_RESERVE + remaining
    monkeypatch.setattr(replacement, "MAX_BYTES", limit)

    if remaining == 0:
        assert replacement.preview(request, sealed) == plan
    else:
        with pytest.raises(tx.PendingTransactionError, match="bound"):
            replacement.preview(request, sealed)
    assert not Path(request["journal"]).exists()


def test_guard_growth_refuses_without_rewriting_held_guard(tmp_path, monkeypatch):
    slot = tmp_path / "archive"
    journal = tmp_path / "journal.json"
    replacement._register(slot, journal, "digest", "archive")
    marker = tmp_path / replacement.GUARD
    before = marker.read_bytes()
    monkeypatch.setattr(replacement, "MAX_BYTES", len(before))

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement._register(tmp_path / "second", journal, "digest", "archive")

    assert marker.read_bytes() == before


def test_unreadable_guard_is_not_rewritten(tmp_path, monkeypatch):
    marker = tmp_path / replacement.GUARD
    before = replacement._bytes({"schema": 1, "entries": {"held": {"note": "x" * 256}}})
    marker.write_bytes(before)
    monkeypatch.setattr(replacement, "MAX_BYTES", 128)

    with pytest.raises(tx.PendingTransactionError, match="oversized"):
        replacement._register(tmp_path / "next", tmp_path / "journal.json", "digest", "archive")

    assert marker.read_bytes() == before
