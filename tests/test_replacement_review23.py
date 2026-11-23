"""Disposable ownership regressions for reviewed replacement admission."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def _ownership(monkeypatch, path, field):
    inode = path.stat().st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def changed(info):
        if info.st_ino != inode:
            return info
        return SimpleNamespace(**{name: getattr(info, name) + 1 if name == field else getattr(info, name)
                                  for name in dir(info) if name.startswith("st_")})

    monkeypatch.setattr(os, "stat", lambda *args, **kwargs: changed(real_stat(*args, **kwargs)))
    monkeypatch.setattr(os, "fstat", lambda *args: changed(real_fstat(*args)))


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("field", ["st_uid", "st_gid"])
def test_child_ownership_drift_refuses_before_effects(case, modeled, monkeypatch, kind, field):
    request, fragments = case
    output = Path(request["output"])
    child = output / "child"
    if kind == "directory":
        child.mkdir()
    else:
        child.write_text("preserved")
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    before = replacement._tree(output)
    _ownership(monkeypatch, child, field)
    assert replacement._tree(output) != before
    with pytest.raises(tx.PendingTransactionError, match="preview changed"):
        replacement.execute(request, seal, plan["preview_sha256"])
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert not (output.parent / replacement.GUARD).exists()



def _prepared(request, fragments):
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    journal = {**plan, "request_digest": replacement._sha(replacement._bytes(request)),
               "state": "PREPARED", "container": None, "new_identity": None, "owner": None}
    return sealed, plan, journal


@pytest.mark.parametrize("state", ["CORRUPT", None, [], {}, 1, True, "ROLLBACK_INTENT", "MISSING"])
@pytest.mark.parametrize("existing_marker", [False, True])
def test_invalid_forward_state_preserves_guards(case, modeled, monkeypatch, state, existing_marker):
    request, fragments = case
    sealed, plan, journal = _prepared(request, fragments)
    if state == "MISSING":
        journal.pop("state")
    else:
        journal["state"] = state
    journal_path = Path(request["journal"])
    journal_path.write_bytes(replacement._bytes(journal))
    marker = Path(request["output"]).parent / replacement.GUARD
    if existing_marker:
        replacement._register(marker.parent / "unrelated-slot", journal_path, "other", "target")
    before = marker.read_bytes() if marker.exists() else None
    original_journal = journal_path.read_bytes()
    calls = []
    original = replacement._register
    def tracked(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(replacement, "_register", tracked)
    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, sealed, plan["preview_sha256"])
    assert calls == []
    assert (marker.read_bytes() if marker.exists() else None) == before
    assert journal_path.read_bytes() == original_journal
    assert not Path(request["archive"]).exists()
    assert not list(journal_path.parent.glob(".replacement-container-*"))


@pytest.mark.parametrize("state,field,value", [
    ("PREPARED", "schema", True),
    ("PREPARED", "old_identity", None),
    ("PREPARED", "parent_identity", []),
    ("PREPARED", "archive_parent_identity", {}),
    ("PREPARED", "old_tree", []),
    ("PREPARED", "owner", {}),
    ("PREPARED", "container", "unexpected"),
    ("PREPARED", "new_identity", "MISSING"),
    ("ALLOCATED", "new_identity", None),
    ("ALLOCATED", "container", None),
    ("ALLOCATED", "container", "relative-container"),
    ("ALLOCATED", "allocation", []),
    ("ALLOCATED", "provenance", None),
    ("ALLOCATED", "binding", "invalid"),
    ("NEW_INSTALLED", "owner", {}),
    ("PUBLISHING", "owner", []),
    ("PUBLISHING", "owner", {}),
    ("PUBLISHING", "owner", {"id": "invalid"}),
    ("COMMITTED", "receipt_sha256", None),
    ("COMMITTED", "old_tree", []),
])
def test_bad_state_fields_refuse_before_registration(case, modeled, monkeypatch, state, field, value):
    request, fragments = case
    sealed, plan, journal = _prepared(request, fragments)
    if state != "PREPARED":
        def stop(point):
            if point == state:
                raise RuntimeError("selected interruption")
        with pytest.raises(RuntimeError, match="selected interruption"):
            replacement.execute(request, sealed, plan["preview_sha256"], failpoint=stop)
        journal = replacement._load(request["journal"])
    if value == "MISSING":
        journal.pop(field)
    else:
        journal[field] = value
    journal_path = Path(request["journal"])
    journal_path.write_bytes(replacement._bytes(journal))
    before = journal_path.read_bytes()
    def unexpected(*args, **kwargs):
        pytest.fail("malformed journal reached guard registration")
    monkeypatch.setattr(replacement, "_register", unexpected)
    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, sealed, plan["preview_sha256"])
    assert journal_path.read_bytes() == before
