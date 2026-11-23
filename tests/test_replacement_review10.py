"""Review regressions use disposable files and an inert maintenance fixture."""
import json
import os
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled
from tests.test_replacement_guard_issuers import guarded_parent as guarded_parent, protected_policy as protected_policy


@pytest.mark.parametrize("failure", ["collision", "io-error"])
@pytest.mark.parametrize("remove_winner", [False, True])
def test_failed_guard_update_retains_predecessor_and_blocks_archive(
    protected_policy, guarded_parent, monkeypatch, failure, remove_winner,
):
    parent, marker, journal = guarded_parent
    before = marker.read_bytes()
    real_link = os.link

    def winner(source, target, *args, **kwargs):
        if target == replacement.GUARD:
            marker.write_bytes(b"foreign winner")
            protected_policy.owner(marker, 987654)
            if failure == "io-error":
                raise OSError("injected publication failure")
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", winner)
    with pytest.raises((tx.PendingTransactionError, OSError)):
        replacement._register(parent / "next", journal, "exact", "archive")
    assert marker.read_bytes() == b"foreign winner"
    backups = list(parent.glob("*graphify-merge-backup*"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    if remove_winner:
        marker.unlink()
    with pytest.raises(tx.PendingTransactionError, match="guard update.*recovery"):
        replacement.guard_output(parent / "archive" / "new", mutation=True)


@pytest.mark.parametrize("kind", ["regular", "directory", "symlink"])
@pytest.mark.parametrize("foreign", [False, True])
def test_guard_backup_issuer_controls_recovery_hold(
    protected_policy, guarded_parent, kind, foreign,
):
    parent, marker, _ = guarded_parent
    marker.unlink()
    backup = parent / ("." + replacement.GUARD + ".graphify-merge-backup." + "a" * 32)
    if kind == "regular":
        backup.write_bytes(b"opaque recovery evidence")
    elif kind == "directory":
        backup.mkdir()
    else:
        backup.symlink_to(parent / "absent")
    if foreign:
        protected_policy.owner(backup, 987654)
        replacement.guard_output(parent / "ordinary")
    else:
        with pytest.raises(tx.PendingTransactionError, match="guard update.*recovery"):
            replacement.guard_output(parent / "ordinary")


def test_guard_backup_lookalike_does_not_claim_recovery(guarded_parent):
    parent, marker, _ = guarded_parent
    marker.unlink()
    (parent / ("." + replacement.GUARD + ".graphify-merge-backup.not-a-token")).write_bytes(b"unrelated")
    replacement.guard_output(parent / "ordinary")


@pytest.mark.parametrize("initial", ["missing", "foreign"])
def test_completed_update_cannot_leave_stale_public_marker_observation(
    protected_policy, guarded_parent, monkeypatch, initial,
):
    parent, marker, _ = guarded_parent
    trusted = marker.read_bytes()
    if initial == "missing":
        marker.unlink()
    else:
        protected_policy.owner(marker, 987654)
    real_scan = replacement._guard_recovery_entries
    scans = []

    def complete_update(cap, issuers):
        result = real_scan(cap, issuers)
        if cap.path == parent:
            scans.append(True)
            if len(scans) == 2:
                published = parent / "published"
                published.write_bytes(trusted)
                os.replace(published, marker)
        return result

    monkeypatch.setattr(replacement, "_guard_recovery_entries", complete_update)
    with pytest.raises(tx.PendingTransactionError, match="replacement guard changed"):
        replacement.guard_output(parent / "archive" / "new")
    assert len(scans) == 2


def test_guard_recovery_scan_is_bounded_during_iteration(guarded_parent, monkeypatch):
    parent, _, _ = guarded_parent
    for name in ("a", "b", "c"):
        (parent / name).touch()
    with tx.pin_output(parent, mutation=False) as cap:
        monkeypatch.setattr(replacement, "MAX_GUARD_SCAN_ENTRIES", 2)
        with pytest.raises(tx.PendingTransactionError, match="scan exceeds bound"):
            replacement._guard_recovery_entries(cap, {os.getuid()})


def test_successful_guard_update_leaves_no_recovery_hold(guarded_parent):
    parent, marker, journal = guarded_parent
    replacement._register(parent / "next", journal, "exact", "archive")
    assert set(json.loads(marker.read_bytes())["entries"]) == {"archive", "next"}
    assert not list(parent.glob("*graphify-merge-backup*"))
    replacement.guard_output(parent / "ordinary")
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement.guard_output(parent / "archive" / "new")


@pytest.mark.parametrize("selector", [None, "0" * 64])
def test_wrong_rollback_selector_does_not_register_quarantine(case, modeled, selector):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    journal_path = Path(request["journal"])
    journal_before = journal_path.read_bytes()
    marker = journal_path.parent / replacement.GUARD
    marker_before = marker.read_bytes()
    old_tree, new_tree = replacement._tree(request["archive"]), replacement._tree(request["output"])
    quarantine = journal_path.parent / "unused-quarantine"

    with pytest.raises(tx.PendingTransactionError, match="new-graph selector"):
        replacement.rollback(request, replacement._sha(journal_before), quarantine,
                             expected_new_tree=selector)

    assert marker.read_bytes() == marker_before
    assert journal_path.read_bytes() == journal_before
    assert not quarantine.exists()
    assert replacement._tree(request["archive"]) == old_tree
    assert replacement._tree(request["output"]) == new_tree
    replacement.guard_output(quarantine / "ordinary", mutation=True)


@pytest.mark.parametrize("failure", ["short", "zero", "corrupt"])
def test_new_lock_is_complete_and_verified_before_yield(case, monkeypatch, failure):
    request, _ = case
    slot = Path(request["journal"]).name
    payload = ("replacement-lock-v1:" + replacement._sha(os.fsencode(slot))).encode()
    path = Path(request["journal"]).parent / (".replacement-lock-" + replacement._sha(os.fsencode(slot)))
    real_write, real_fsync = os.write, os.fsync
    calls = []

    def selected(fd):
        return path.exists() and os.fstat(fd).st_ino == path.stat().st_ino

    def write(fd, data):
        if selected(fd):
            calls.append(len(data))
            if failure == "zero":
                return 0
            if failure == "short":
                return real_write(fd, data[:max(1, len(data) // 2)])
        return real_write(fd, data)

    def fsync(fd):
        real_fsync(fd)
        if failure == "corrupt" and selected(fd):
            os.pwrite(fd, b"X", 0)

    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "fsync", fsync)
    if failure == "short":
        with replacement._coordinator_lock(request):
            assert path.read_bytes() == payload
        assert len(calls) > 1
    else:
        with pytest.raises(tx.PendingTransactionError, match="lock.*(progress|binding)"):
            with replacement._coordinator_lock(request):
                pytest.fail("incomplete or corrupt lock must not admit operation work")
    assert not Path(request["journal"]).exists()
