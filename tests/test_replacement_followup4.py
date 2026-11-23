"""Four review regressions use disposable registrations and modeled custody."""
import contextlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import interpreter_pointer
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case
from tests.test_replacement_integration import registration as registration

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX replacement fixtures")


@pytest.fixture
def issuer_policy(monkeypatch):
    from graphify import replacement_guard_issuers as issuers

    policy = SimpleNamespace(
        trusted_issuers=lambda *args, **kwargs: frozenset({os.getuid()}),
        validate=lambda: None,
    )
    monkeypatch.setattr(issuers, "issuer_snapshot", lambda: contextlib.nullcontext(policy))


def test_new_guard_retains_owner_access_under_restrictive_umask(tmp_path, issuer_policy):
    parent = tmp_path.resolve()
    mask = os.umask(0o777)
    try:
        replacement._register(parent / "Archive", parent / "journal.json", "digest", "archive")
    finally:
        os.umask(mask)
    marker = parent / replacement.GUARD
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert json.loads(marker.read_text())["entries"]["Archive"]["role"] == "archive"


def archive_registration(registration):
    parent, marker, journal = registration
    record = json.loads(marker.read_text())
    entry = record["entries"].pop("graphify-out")
    record["entries"]["Archive"] = {**entry, "role": "archive"}
    marker.write_text(json.dumps(record))
    return parent, marker, journal


def model_case_lookup(monkeypatch, insensitive):
    original = replacement._guard_slot_info

    def lookup(cap, name):
        if name == replacement.GUARD.upper():
            return original(cap, replacement.GUARD) if insensitive else None
        return original(cap, name)

    monkeypatch.setattr(replacement, "_guard_slot_info", lookup)


@pytest.mark.parametrize("present", [False, True])
def test_case_variant_refuses_registered_archive(registration, monkeypatch, present):
    parent, marker, _ = archive_registration(registration)
    if present:
        (parent / "Archive").mkdir()
    model_case_lookup(monkeypatch, True)
    original = marker.read_bytes()
    with pytest.raises(tx.PendingTransactionError, match="archive"):
        replacement.guard_output(parent / "archive" / "new.txt")
    replacement.guard_output(parent / "archive" / "new.txt", mutation=False)
    assert marker.read_bytes() == original
    assert not (parent / "archive" / "new.txt").exists()


def test_case_sensitive_parent_keeps_distinct_absent_slot(registration, monkeypatch):
    parent, _, _ = archive_registration(registration)
    model_case_lookup(monkeypatch, False)
    replacement.guard_output(parent / "archive" / "new.txt")


def test_distinct_uppercase_marker_directory_does_not_change_case_admission(registration, monkeypatch):
    parent, marker, _ = archive_registration(registration)
    original = replacement._guard_slot_info
    info = marker.stat()

    def lookup(cap, name):
        if name == replacement.GUARD.upper():
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1,
                                   st_mode=stat.S_IFDIR | 0o700)
        return original(cap, name)

    monkeypatch.setattr(replacement, "_guard_slot_info", lookup)
    replacement.guard_output(parent / "archive" / "new.txt")


def test_case_equivalent_registration_refuses_alias(registration, monkeypatch, issuer_policy):
    parent, marker, journal = archive_registration(registration)
    model_case_lookup(monkeypatch, True)
    before = marker.read_bytes()
    with pytest.raises(tx.PendingTransactionError, match="case"):
        replacement._register(parent / "archive", journal, "a" * 64, "archive")
    assert marker.read_bytes() == before


def test_ambiguous_legacy_registration_refuses_even_exact_spelling(registration, monkeypatch):
    parent, marker, _ = archive_registration(registration)
    record = json.loads(marker.read_text())
    record["entries"]["archive"] = record["entries"]["Archive"]
    marker.write_text(json.dumps(record))
    model_case_lookup(monkeypatch, True)
    with pytest.raises(tx.PendingTransactionError, match="ambiguous"):
        replacement.guard_output(parent / "Archive" / "new.txt")


def test_actual_case_insensitive_archive_alias(registration):
    parent, _, _ = archive_registration(registration)
    archive = parent / "Archive"
    archive.mkdir()
    alternate = parent / "archive"
    if not alternate.exists():
        pytest.skip("fixture filesystem has case-sensitive names")
    assert os.path.samefile(archive, alternate)
    with pytest.raises(tx.PendingTransactionError, match="archive"):
        replacement.guard_output(alternate / "new.txt")
