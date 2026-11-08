"""Archive read admission uses modeled registration, never live replacement."""

import contextlib
import json
import os
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import checkpoint_restore as recovery
from graphify import transaction as tx


def _register_archive(archive: Path) -> None:
    """Model an existing archive registration without invoking replacement."""
    journal = archive.parent / "journal.json"
    digest = "a" * 64
    journal.write_text(json.dumps({"request_digest": digest, "state": "COMMITTED"}))
    (archive.parent / replacement.GUARD).write_text(json.dumps({
        "schema": 1,
        "entries": {archive.name: {
            "role": "archive", "journal": str(journal), "digest": digest,
        }},
    }))


@pytest.fixture
def archive(tmp_path):
    output = tmp_path / "archive"
    output.mkdir()
    (output / "graph.json").write_bytes(b'{"graph":{},"nodes":[],"links":[]}')
    return output


def test_registered_legacy_archive_snapshot_is_read_only(archive):
    _register_archive(archive)
    before = {path: path.read_bytes() for path in archive.parent.rglob("*") if path.is_file()}

    snapshot = tx.open_graph_snapshot(archive / "graph.json", purpose="test")

    assert snapshot.generation is None
    assert snapshot.payload == before[archive / "graph.json"]
    assert snapshot.data == {"graph": {}, "nodes": [], "links": []}
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        tx.pin_output(archive)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        tx.publish_snapshot_auxiliary(
            snapshot, canonical_name=".graphify_learning.json", payload=b"{}",
        )
    assert {path: path.read_bytes() for path in archive.parent.rglob("*") if path.is_file()} == before


def test_initial_archive_read_lock_preserves_mode(archive):
    _register_archive(archive)
    with tx.pin_output(archive, mutation=False) as reader, tx._locked(reader):
        assert tx._read_bytes(reader, "graph.json") == (archive / "graph.json").read_bytes()


def test_reentrant_archive_read_checks_its_own_mode(archive):
    # Admit the outer mutating capability before the registration appears. The
    # nested read must use its own mode, not inherit this holder's mode.
    with tx.pin_output(archive) as outer, tx._locked(outer):
        _register_archive(archive)
        with tx.pin_output(archive, mutation=False) as reader, tx._locked(reader):
            assert tx._read_bytes(reader, "graph.json") == (archive / "graph.json").read_bytes()


@pytest.mark.parametrize("reentrant", [False, True])
def test_default_constructed_capability_cannot_escalate_archive_read(archive, reentrant):
    _register_archive(archive)
    with tx.pin_output(archive, mutation=False) as reader:
        # Preserve the existing fourth positional _closed argument and the
        # conservative mutation admission of manually constructed capabilities.
        with tx.OutputCapability(reader.path, reader.identity, os.dup(reader.fd), False) as writer:
            def refuse_writer():
                with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
                    with tx._locked(writer):
                        pytest.fail("archive mutation lock was admitted")

            if reentrant:
                with tx._locked(reader):
                    refuse_writer()
                    # Refusal must leave the enclosing read lock usable.
                    with tx._locked(reader):
                        reader.validate()
            else:
                refuse_writer()


@pytest.mark.parametrize("options,expected", [
    ({}, True),
    ({"mutation": False}, False),
    ({"create": True, "mutation": False}, True),
])
def test_pin_and_lock_rechecks_keep_effective_admission(archive, monkeypatch, options, expected):
    modes = []
    monkeypatch.setattr(tx, "_replacement_guard_output",
                        lambda path, *, mutation=True: modes.append(mutation))
    with tx.pin_output(archive, **options) as capability:
        with tx._locked(capability), tx._locked(capability):
            capability.validate()
    assert modes == [expected, expected, expected]


def test_create_cannot_downgrade_archive_admission(archive):
    _register_archive(archive)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        tx.pin_output(archive / "new-output", create=True, mutation=False)
    assert not (archive / "new-output").exists()


@pytest.mark.parametrize("options", [{}, {"create": True, "mutation": False}])
def test_windows_mutation_remains_blocked(archive, monkeypatch, options):
    monkeypatch.setattr(tx, "_PLATFORM", "windows")
    with pytest.raises(tx.PendingTransactionError, match="Windows.*mutation.*not proven"):
        tx.pin_output(archive / "new-output", **options)
    assert not (archive / "new-output").exists()
