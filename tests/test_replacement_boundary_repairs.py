"""Focused replacement repairs on disposable fixtures and modeled maintenance."""
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


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
