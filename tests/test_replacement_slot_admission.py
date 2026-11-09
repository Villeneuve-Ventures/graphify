"""Selected-slot admission uses disposable files and modeled issuer custody."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import checkpoint_restore as recovery
from graphify import storage_guard as storage
from graphify import transaction as tx
from tests.test_checkpoint_restore import fixture as restore_case, modeled as modeled
from tests.test_replacement_guard_issuers import protected_policy as protected_policy


def _register_slot(policy, output, role):
    """Install a trusted registration without granting mutation authority."""
    journal = output.parent.parent / "slot-journal.json"
    journal.write_text(json.dumps({"request_digest": "slot", "state": "PREPARED"}))
    marker = output.parent / replacement.GUARD
    marker.write_text(json.dumps({"schema": 1, "entries": {output.name: {
        "journal": str(journal), "digest": "slot", "role": role}}}))
    policy.owner(marker)
    policy.owner(journal)
    policy.owner(journal.parent)


@pytest.mark.parametrize("role", ["archive", "target"])
@pytest.mark.parametrize("operation,occupied", [
    ("mkdir", False), ("nested-mkdir", False), ("open", False), ("atomic", False),
    ("replace-destination", False), ("open", True), ("atomic", True),
    ("replace-destination", True), ("replace-source", True), ("unlink", True),
])
def test_ordinary_mutations_preserve_registered_slots(
        protected_policy, tmp_path, role, operation, occupied):
    parent = tmp_path / "namespace"
    parent.mkdir()
    output = parent / "reserved"
    outside = tmp_path / "ordinary"
    outside.write_bytes(b"ordinary bytes")
    if occupied:
        output.write_bytes(b"reserved bytes")
    _register_slot(protected_policy, output, role)
    before = replacement._tree(tmp_path)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only|replacement is incomplete"):
        if operation == "mkdir":
            storage.ordinary_mkdir(output)
        elif operation == "nested-mkdir":
            storage.ordinary_mkdir(output / "nested")
        elif operation == "open":
            with storage.ordinary_open(output, "wb") as stream:
                stream.write(b"changed")
        elif operation == "atomic":
            storage.ordinary_atomic_bytes(output, b"changed")
        elif operation == "replace-destination":
            storage.ordinary_replace(outside, output)
        elif operation == "replace-source":
            storage.ordinary_replace(output, outside)
        else:
            storage.ordinary_unlink(output)

    assert replacement._tree(tmp_path) == before
    assert output.exists() == occupied


@pytest.mark.parametrize("role", ["archive", "target"])
@pytest.mark.parametrize("operation", ["open", "atomic"])
def test_ordinary_write_rechecks_registration_after_open(
        protected_policy, tmp_path, monkeypatch, role, operation):
    parent = tmp_path / "namespace"
    parent.mkdir()
    output = parent / "reserved"
    output.write_bytes(b"reserved bytes")
    real_open = os.open
    expected = []

    def register_after_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        selected = name == output.name if operation == "open" else str(name).startswith(".reserved.tmp-")
        if selected and flags & os.O_WRONLY and not expected:
            _register_slot(protected_policy, output, role)
            inventory = replacement._tree(tmp_path)
            if operation == "atomic":
                del inventory["namespace/" + str(name)]
            expected.append(inventory)
        return fd

    monkeypatch.setattr(os, "open", register_after_open)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only|replacement is incomplete"):
        if operation == "open":
            with storage.ordinary_open(output, "wb") as stream:
                stream.write(b"changed")
        else:
            storage.ordinary_atomic_bytes(output, b"changed")

    assert len(expected) == 1
    assert replacement._tree(tmp_path) == expected[0]


@pytest.mark.parametrize("role", ["archive", "target"])
@pytest.mark.parametrize("registration_time", ["before-execute", "after-lock"])
def test_checkpoint_restore_refuses_registered_slot_before_directory_creation(
        restore_case, protected_policy, monkeypatch, role, registration_time):
    store, _root, original, request, _approved = restore_case
    output = Path(request["output"])
    fingerprint = recovery.preview(request)["preview_sha256"]
    real_locked = recovery._locked
    observations = []

    def register():
        _register_slot(protected_policy, output, role)
        observations.append((replacement._tree(output.parent), replacement._tree(original),
                             replacement._tree(store)))

    @contextmanager
    def register_after_lock(cap, deadline):
        with real_locked(cap, deadline):
            if cap.path == output.parent:
                register()
            yield

    if registration_time == "before-execute":
        register()
    else:
        monkeypatch.setattr(recovery, "_locked", register_after_lock)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only|replacement is incomplete"):
        recovery.execute(request, fingerprint)

    assert len(observations) == 1
    assert not output.exists()
    assert (replacement._tree(output.parent), replacement._tree(original),
            replacement._tree(store)) == observations[0]
