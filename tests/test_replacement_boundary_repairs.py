"""Focused replacement repairs on disposable fixtures and modeled maintenance."""
from contextlib import contextmanager
import json

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def test_registration_refuses_mutation_inside_retained_archive(tmp_path):
    archive = tmp_path / "prior-archive"
    archive.mkdir()
    (archive / "preserved").write_bytes(b"retained")
    journal = tmp_path / "prior-journal.json"
    journal.write_text(json.dumps({"request_digest": "prior", "state": "COMMITTED"}))
    replacement._register(archive, journal, "prior", "archive")
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement._register(archive / "next", tmp_path / "next-journal.json", "next", "archive")

    assert replacement._tree(archive) == before
    assert not (archive / replacement.GUARD).exists()


def test_registration_rechecks_mutation_after_waiting_for_parent_lock(tmp_path, monkeypatch):
    archive = tmp_path / "prior-archive"
    archive.mkdir()
    (archive / "preserved").write_bytes(b"retained")
    journal = tmp_path / "prior-journal.json"
    journal.write_text(json.dumps({"request_digest": "prior", "state": "COMMITTED"}))
    before = replacement._tree(archive)
    real_locks = replacement._fd_locks
    registered = []

    @contextmanager
    def register_while_waiting(paths, deadline):
        with real_locks(paths, deadline) as caps:
            if list(paths) == [archive]:
                replacement._register(archive, journal, "prior", "archive")
                registered.append(True)
            yield caps

    monkeypatch.setattr(replacement, "_fd_locks", register_while_waiting)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement._register(archive / "next", tmp_path / "next-journal.json", "next", "archive")

    assert registered == [True]
    assert replacement._tree(archive) == before
    assert not (archive / replacement.GUARD).exists()


@pytest.mark.parametrize("depth", [128, 129])
def test_preservation_depth_boundary_closes_opened_directories(tmp_path, monkeypatch, depth):
    root = tmp_path / "inventory"
    root.mkdir()
    path = root
    for _ in range(depth):
        path = path / "d"
        path.mkdir()
    (path / "held").write_bytes(b"retained")
    real_open, real_close = replacement.os.open, replacement.os.close
    opened = set()

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def tracked_close(fd):
        opened.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(replacement.os, "open", tracked_open)
    monkeypatch.setattr(replacement.os, "close", tracked_close)
    if depth == 128:
        inventory = replacement._tree(root)
        assert len(inventory) == depth + 2  # Root metadata plus child directories and file.
        assert inventory["/".join(["d"] * depth + ["held"])]["sha256"] == replacement._sha(b"retained")
    else:
        with pytest.raises(tx.PendingTransactionError, match="preservation directory depth bound exceeded"):
            replacement._tree(root)
    assert not opened
