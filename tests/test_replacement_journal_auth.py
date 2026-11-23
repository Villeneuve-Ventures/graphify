"""Synthetic journal trust and race checks; no account changes or native probes."""
import json
import os
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import hook_installation
from graphify import transaction as tx
from tests.test_replacement_guard_issuers import guarded_parent, protected_policy  # noqa: F401


@pytest.fixture(autouse=True)
def safe_fixture_acls(monkeypatch):
    monkeypatch.setattr(hook_installation, "_acl_safe", lambda fd: True)


@pytest.fixture
def target(guarded_parent):
    parent, marker, journal = guarded_parent
    control = journal.parent / "journal-control"
    control.mkdir()
    moved = control / journal.name
    journal.rename(moved)
    journal = moved
    record = json.loads(marker.read_bytes())
    record["entries"]["archive"]["role"] = "target"
    record["entries"]["archive"]["journal"] = str(journal)
    marker.write_text(json.dumps(record))
    return parent / "archive", marker, journal




@pytest.mark.parametrize("state", ["COMMITTED", "ROLLED_BACK_UNRECOVERED"])
def test_safe_terminal_journal_allows_target(protected_policy, target, state):
    path, _, journal = target
    journal.write_text(json.dumps({"request_digest": "exact", "state": state}))
    replacement.guard_output(path)




@pytest.mark.parametrize("marker_owner,journal_owner", [(987654, "caller"), (876543, 0), (0, 987654)])
def test_exact_marker_namespace_accepts_mixed_trusted_issuers(protected_policy, target, marker_owner, journal_owner):
    path, marker, journal = target
    protected_policy.install(marker.parent, legacy=(876543,))
    protected_policy.owner(marker, marker_owner)
    protected_policy.owner(journal, os.getuid() if journal_owner == "caller" else journal_owner)
    replacement.guard_output(path)


def test_mutator_of_other_namespace_does_not_authenticate_journal(protected_policy, target, tmp_path):
    path, _, journal = target
    other = tmp_path / "other-namespace"
    other.mkdir()
    protected_policy.install(other)
    protected_policy.owner(journal, 987654)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(path)


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "directory", "oversized", "noncanonical"])
def test_unsafe_journal_object_or_path_refuses(protected_policy, target, monkeypatch, damage):
    path, marker, journal = target
    if damage == "symlink":
        payload = journal.with_name("payload")
        journal.rename(payload)
        journal.symlink_to(payload)
    elif damage == "hardlink":
        os.link(journal, journal.with_name("linked"))
    elif damage == "directory":
        journal.unlink()
        journal.mkdir()
    elif damage == "oversized":
        monkeypatch.setattr(replacement, "MAX_BYTES", 10)
        with pytest.raises(tx.PendingTransactionError):
            with replacement._guard_journal(str(journal), {0, os.getuid()}):
                pytest.fail("oversized journal must not be admitted")
        return
    else:
        record = json.loads(marker.read_bytes())
        record["entries"]["archive"]["journal"] = str(journal.parent) + "/./" + journal.name
        marker.write_text(json.dumps(record))
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(path)


@pytest.mark.parametrize("damage", ["owner", "mode", "gid", "nlink", "parent_owner", "parent_mode", "parent_gid",
                                    "parent_nlink", "contents", "file_swap", "parent_swap", "file_acl", "parent_acl"])
def test_journal_observation_lives_through_authority_decision(protected_policy, target, monkeypatch, damage):
    path, _, journal = target
    real_fstat = os.fstat
    journal_inode = os.stat(journal).st_ino
    changed = False
    denied = set()
    monkeypatch.setattr(hook_installation, "_acl_safe", lambda fd: os.fstat(fd).st_ino not in denied)

    def authority_decision():
        nonlocal changed
        if changed:
            return None
        changed = True
        if damage in {"owner", "parent_owner"}:
            protected_policy.owner(journal.parent if damage.startswith("parent") else journal, 765432)
        elif damage in {"mode", "parent_mode"}:
            protected_policy.mode(journal.parent if damage.startswith("parent") else journal, 0o666)
        elif damage in {"gid", "nlink", "parent_gid", "parent_nlink"}:
            field = "st_gid" if damage.endswith("gid") else "st_nlink"
            selected_inode = os.stat(journal.parent).st_ino if damage.startswith("parent") else journal_inode
            def changed_metadata(fd):
                info = real_fstat(fd)
                return SimpleNamespace(**{name: (getattr(info, name) + 1 if name == field else getattr(info, name))
                                          for name in dir(info) if name.startswith("st_")}) if info.st_ino == selected_inode else info
            monkeypatch.setattr(os, "fstat", changed_metadata)
        elif damage == "contents":
            journal.write_text(json.dumps({"request_digest": "exact", "state": "PREPARED"}))
        elif damage == "file_swap":
            other = journal.with_name("substitution")
            other.write_bytes(journal.read_bytes())
            other.replace(journal)
        elif damage == "parent_swap":
            moved = journal.parent.with_name(journal.parent.name + "-moved")
            journal.parent.rename(moved)
            journal.parent.mkdir()
            journal.write_bytes((moved / journal.name).read_bytes())
        else:
            inode = os.stat(journal if damage == "file_acl" else journal.parent).st_ino
            denied.add(inode)
        return (str(journal), "exact")

    monkeypatch.setattr(replacement, "_AUTHORITY", SimpleNamespace(get=authority_decision))
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(path)
    assert changed


def test_journal_parse_failure_closes_every_opened_descriptor(protected_policy, target, monkeypatch):
    path, _, journal = target
    journal.write_bytes(b"not-json")
    opened = []
    real_open = os.open
    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(path)
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("read_pass", [1, 2, 3])
@pytest.mark.parametrize("damage", ["file_swap", "parent_swap", "file_mode", "parent_mode", "parent_acl"])
def test_read_time_journal_changes_refuse(protected_policy, target, monkeypatch, read_pass, damage):
    path, _, journal = target
    journal_inode = os.stat(journal).st_ino
    real_read = os.read
    reads = 0
    denied = set()
    monkeypatch.setattr(hook_installation, "_acl_safe", lambda fd: os.fstat(fd).st_ino not in denied)

    def changed_read(fd, length):
        nonlocal reads
        data = real_read(fd, length)
        if os.fstat(fd).st_ino == journal_inode and data:
            reads += 1
            if reads == read_pass:
                if damage == "file_swap":
                    substitute = journal.with_name("substitute")
                    substitute.write_bytes(data)
                    substitute.replace(journal)
                elif damage == "parent_swap":
                    moved = journal.parent.with_name("moved-control")
                    journal.parent.rename(moved)
                    journal.parent.mkdir()
                    journal.write_bytes(data)
                elif damage.endswith("mode"):
                    protected_policy.mode(journal.parent if damage.startswith("parent") else journal, 0o777)
                else:
                    denied.add(os.stat(journal.parent).st_ino)
        return data

    monkeypatch.setattr(os, "read", changed_read)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(path)
    assert reads >= read_pass


@pytest.mark.parametrize("failure", ["foreign_file", "foreign_parent", "file_acl", "parent_acl", "parse", "decision"])
def test_journal_refusal_closes_parent_and_file(protected_policy, target, monkeypatch, failure):
    _, _, journal = target
    if failure.startswith("foreign"):
        protected_policy.owner(journal.parent if failure.endswith("parent") else journal, 765432)
    elif failure.endswith("acl"):
        inode = os.stat(journal.parent if failure.startswith("parent") else journal).st_ino
        monkeypatch.setattr(hook_installation, "_acl_safe", lambda fd: os.fstat(fd).st_ino != inode)
    elif failure == "parse":
        journal.write_bytes(b"not-json")
    opened = []
    real_open = os.open

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises((tx.PendingTransactionError, ValueError)):
        with replacement._guard_journal(str(journal), {0, os.getuid()}):
            if failure == "decision":
                raise tx.PendingTransactionError("decision refusal")
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_unsupported_journal_inspection_refuses_only_selected_entry(protected_policy, target, monkeypatch):
    path, _, _ = target
    monkeypatch.setattr(hook_installation.sys, "platform", "unsupported-posix")
    replacement.guard_output(path.parent / "unregistered")
    with pytest.raises(tx.PendingTransactionError, match="inspection unavailable"):
        replacement.guard_output(path)


def test_unavailable_acl_inspection_refuses(protected_policy, target, monkeypatch):
    path, _, _ = target

    def unavailable(fd):
        raise NotImplementedError("no ACL inspection")

    monkeypatch.setattr(hook_installation, "_acl_safe", unavailable)
    with pytest.raises(tx.PendingTransactionError, match="ACL inspection unavailable"):
        replacement.guard_output(path)


def test_shared_writable_higher_ancestor_does_not_require_new_trust(protected_policy, target, tmp_path):
    path, marker, journal = target
    shared = tmp_path / "shared-journal-ancestor"
    shared.mkdir()
    protected_policy.owner(shared, 765432)
    protected_policy.mode(shared, 0o777)
    control = shared / "private-control"
    control.mkdir(mode=0o700)
    moved = control / journal.name
    journal.rename(moved)
    record = json.loads(marker.read_bytes())
    record["entries"]["archive"]["journal"] = str(moved)
    marker.write_text(json.dumps(record))
    replacement.guard_output(path)


def test_unavailable_journal_identity_refuses(protected_policy, target, monkeypatch):
    _, _, journal = target
    real_fstat = os.fstat

    def without_owner(fd):
        info = real_fstat(fd)
        return SimpleNamespace(**{name: getattr(info, name) for name in dir(info)
                                  if name.startswith("st_") and name != "st_uid"})

    monkeypatch.setattr(os, "fstat", without_owner)
    with pytest.raises(tx.PendingTransactionError, match="identity inspection unavailable"):
        with replacement._guard_journal(str(journal), {0, os.getuid()}):
            pytest.fail("identity metadata must be available")


def test_unavailable_acl_module_refuses_selected_entry(protected_policy, target, monkeypatch):
    import builtins
    path, _, _ = target
    real_import = builtins.__import__

    def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
        if (name == "hook_installation" and level == 1 and globals is not None
                and globals.get("__name__") == replacement.__name__):
            raise ImportError("unavailable POSIX ACL module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    replacement.guard_output(path.parent / "unregistered")
    with pytest.raises(tx.PendingTransactionError, match="ACL inspection unavailable"):
        replacement.guard_output(path)
