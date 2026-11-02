"""Synthetic issuer metadata and disposable files; no foreign accounts or native probes."""
import os
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import replacement_guard_issuers as issuers
from graphify import transaction as tx


@pytest.fixture
def protected_policy(tmp_path, monkeypatch):
    """Model root ownership/ACL evidence on ordinary disposable files only."""
    directory = tmp_path / "controller-policy"
    directory.mkdir()
    records = directory / issuers._RECORDS
    records.mkdir()
    owners = {}
    real_stat, real_fstat = os.stat, os.fstat

    def owner(path, uid=0):
        info = real_stat(path, follow_symlinks=False)
        owners[(info.st_dev, info.st_ino)] = uid

    for path in (directory, *directory.parents, records):
        owner(path)

    def metadata(info):
        uid = owners.get((info.st_dev, info.st_ino), info.st_uid)
        return SimpleNamespace(**{name: (uid if name == "st_uid" else getattr(info, name))
                                  for name in dir(info) if name.startswith("st_")})

    monkeypatch.setattr(issuers, "_DIRECTORY", directory)
    monkeypatch.setattr(issuers, "_acl_safe", lambda fd: True)
    monkeypatch.setattr(os, "stat", lambda *a, **kw: metadata(real_stat(*a, **kw)))
    monkeypatch.setattr(os, "fstat", lambda fd: metadata(real_fstat(fd)))

    def install(parent, *, generation=1, mutator=987654, legacy=()):
        info = real_stat(parent)
        policy = issuers.NamespaceIssuerPolicy("enrollment-one", str(parent),
                                               (info.st_dev, info.st_ino), generation, mutator, legacy)
        record = records / (policy.sha256 + ".json")
        record.write_bytes(issuers._bytes(policy.to_record()))
        owner(record)
        entry = {key: value for key, value in policy.to_record().items()
                 if key in {"enrollment_id", "namespace_path", "namespace_identity", "policy_generation"}}
        entry["policy_sha256"] = policy.sha256
        index = directory / issuers._INDEX
        index.write_bytes(issuers._bytes({"schema": issuers._INDEX_SCHEMA, "namespaces": [entry]}))
        owner(index)
        return policy, index, record

    return SimpleNamespace(directory=directory, records=records, owner=owner, install=install)


@pytest.fixture
def guarded_parent(tmp_path):
    parent = tmp_path / "namespace"
    parent.mkdir()
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({"request_digest": "exact", "state": "COMMITTED"}))
    marker = parent / replacement.GUARD
    marker.write_text(json.dumps({"schema": 1, "entries": {"archive": {
        "journal": str(journal), "digest": "exact", "role": "archive"}}}))
    return parent, marker, journal


@pytest.mark.parametrize("kind", ["malformed", "directory", "symlink"])
def test_foreign_shared_ancestor_marker_does_not_block_unregistered_output(tmp_path, monkeypatch, kind):
    shared = tmp_path / "shared"
    shared.mkdir()
    marker = shared / replacement.GUARD
    if kind == "malformed":
        marker.write_bytes(b"not-json")
    elif kind == "directory":
        marker.mkdir()
    else:
        marker.symlink_to(shared / "absent")
    real_stat = os.stat
    shared_identity = real_stat(shared).st_ino

    def foreign_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == replacement.GUARD and kwargs.get("dir_fd") is not None:
            if os.fstat(kwargs["dir_fd"]).st_ino == shared_identity:
                return SimpleNamespace(**{name: (987654 if name == "st_uid" else getattr(info, name))
                                          for name in dir(info) if name.startswith("st_")})
        return info

    real_load = replacement._load

    def no_foreign_payload(path, **kwargs):
        if path == replacement.GUARD and kwargs.get("parent") is not None:
            if kwargs["parent"].identity.inode == shared_identity:
                pytest.fail("foreign marker payload must not be read")
        return real_load(path, **kwargs)

    monkeypatch.setattr(os, "stat", foreign_stat)
    monkeypatch.setattr(replacement, "_load", no_foreign_payload)
    before = sorted(p.name for p in shared.iterdir())
    replacement.guard_output(shared / "ordinary" / "graphify-out")
    assert sorted(p.name for p in shared.iterdir()) == before
    assert not (shared / "ordinary").exists()


@pytest.mark.parametrize("kind", ["malformed", "directory", "symlink"])
@pytest.mark.parametrize("owner_uid", [0, "caller"])
def test_trusted_unsafe_marker_still_blocks(protected_policy, guarded_parent, kind, owner_uid):
    parent, marker, _ = guarded_parent
    marker.unlink()
    if kind == "malformed":
        marker.write_bytes(b"not-json")
    elif kind == "directory":
        marker.mkdir()
    else:
        marker.symlink_to(parent / "absent")
    protected_policy.owner(marker, os.getuid() if owner_uid == "caller" else owner_uid)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(parent / "unrelated")


def test_enrolled_mutator_and_legacy_guards_remain_trusted_without_global_uid_trust(
        protected_policy, guarded_parent, tmp_path):
    parent, marker, _ = guarded_parent
    policy, _, _ = protected_policy.install(parent, legacy=(876543,))
    for uid in (policy.mutator_uid, 876543):
        protected_policy.owner(marker, uid)
        replacement.guard_output(parent / "archive", mutation=False)
        with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
            replacement.guard_output(parent / "archive" / "new")
    other = tmp_path / "other"
    other.mkdir()
    other_marker = other / replacement.GUARD
    other_marker.write_bytes(b"untrusted payload")
    protected_policy.owner(other_marker, policy.mutator_uid)
    replacement.guard_output(other / "ordinary")


@pytest.mark.parametrize("damage", ["missing", "bytes", "generation", "namespace", "identity"])
def test_missing_stale_or_misbound_enrolled_record_blocks(protected_policy, guarded_parent, damage):
    parent, marker, _ = guarded_parent
    policy, index, record = protected_policy.install(parent)
    protected_policy.owner(marker, policy.mutator_uid)
    if damage == "missing":
        record.unlink()
    elif damage == "bytes":
        record.write_bytes(b"{}")
        protected_policy.owner(record)
    else:
        value = json.loads(index.read_bytes())
        if damage == "generation":
            value["namespaces"][0]["policy_generation"] += 1
        elif damage == "namespace":
            value["namespaces"][0]["enrollment_id"] = "other"
        else:
            value["namespaces"][0]["namespace_identity"]["inode"] += 1
        index.write_bytes(issuers._bytes(value))
    before = marker.read_bytes()
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(parent / "ordinary")
    assert marker.read_bytes() == before
    assert not (parent / "ordinary").exists()


def test_enrolled_missing_parent_does_not_fall_back(protected_policy, tmp_path):
    parent = tmp_path / "enrolled"
    parent.mkdir()
    protected_policy.install(parent)
    parent.rmdir()
    with pytest.raises(tx.PendingTransactionError) as error:
        replacement.guard_output(parent / "graphify-out")
    assert "identity changed" in str(error.value.__cause__)
    assert not parent.exists()


@pytest.mark.parametrize("selected", ["original", "renamed"])
def test_renamed_enrollment_cannot_make_broker_archive_guard_foreign(
        protected_policy, guarded_parent, selected):
    parent, marker, _ = guarded_parent
    policy, _, _ = protected_policy.install(parent)
    protected_policy.owner(marker, policy.mutator_uid)
    retained = parent.with_name("moved-namespace")
    parent.rename(retained)
    parent.symlink_to(retained, target_is_directory=True)
    path = parent if selected == "original" else retained
    before = (retained / replacement.GUARD).read_bytes()
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(path / "archive", mutation=True)
    assert (retained / replacement.GUARD).read_bytes() == before


def test_alias_to_unchanged_enrollment_retains_archive_fencing(protected_policy, guarded_parent, tmp_path):
    parent, marker, _ = guarded_parent
    policy, _, _ = protected_policy.install(parent)
    protected_policy.owner(marker, policy.mutator_uid)
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    replacement.guard_output(alias / "archive", mutation=False)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement.guard_output(alias / "archive", mutation=True)


@pytest.mark.parametrize("damage", ["json", "duplicate", "owner", "permissions", "symlink", "hardlink", "acl"])
def test_unsafe_index_is_not_bootstrap_absence(protected_policy, guarded_parent, tmp_path, monkeypatch, damage):
    parent, _, _ = guarded_parent
    _, index, _ = protected_policy.install(parent)
    if damage == "json":
        index.write_bytes(b"not-json")
    elif damage == "duplicate":
        value = json.loads(index.read_bytes())
        value["namespaces"] *= 2
        index.write_bytes(issuers._bytes(value))
    elif damage == "owner":
        protected_policy.owner(index, 123456)
    elif damage == "permissions":
        index.chmod(0o666)
    elif damage == "symlink":
        original = index.read_bytes()
        index.unlink()
        other = tmp_path / "index-copy"
        other.write_bytes(original)
        index.symlink_to(other)
    elif damage == "hardlink":
        os.link(index, tmp_path / "index-link")
    else:
        monkeypatch.setattr(issuers, "_acl_safe", lambda fd: False)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(parent / "ordinary")


def test_index_permission_error_is_not_absence(protected_policy, guarded_parent, monkeypatch):
    parent, _, _ = guarded_parent
    protected_policy.install(parent)
    real_open = os.open

    def denied(name, *args, **kwargs):
        if name == issuers._INDEX:
            raise PermissionError("modeled index access denial")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(parent / "ordinary")


def test_changed_policy_after_marker_read_refuses(protected_policy, guarded_parent, monkeypatch):
    parent, marker, _ = guarded_parent
    _, index, _ = protected_policy.install(parent)
    real_load = replacement._load

    def changed(path, **kwargs):
        value = real_load(path, **kwargs)
        if path == replacement.GUARD:
            replacement_file = index.with_name("next-index")
            replacement_file.write_bytes(index.read_bytes())
            protected_policy.owner(replacement_file)
            os.replace(replacement_file, index)
        return value

    monkeypatch.setattr(replacement, "_load", changed)
    before = marker.read_bytes()
    with pytest.raises(tx.PendingTransactionError) as error:
        replacement.guard_output(parent / "ordinary")
    assert isinstance(error.value.__cause__, issuers.IssuerPolicyError)
    assert marker.read_bytes() == before


def test_foreign_collision_cannot_be_adopted_by_registration(protected_policy, guarded_parent):
    parent, marker, journal = guarded_parent
    protected_policy.owner(marker, 987654)
    before = marker.read_bytes()
    with pytest.raises(tx.PendingTransactionError, match="foreign.*collision"):
        replacement._register(parent / "other", journal, "exact", "archive")
    assert marker.read_bytes() == before


@pytest.mark.parametrize("damage", ["new", "replace", "content"])
def test_registration_preserves_entry_changed_while_staging_payload(
        protected_policy, guarded_parent, monkeypatch, damage):
    import stat

    parent, marker, journal = guarded_parent
    if damage == "new":
        marker.unlink()
    real_fsync = os.fsync
    changed = []
    winner = b"concurrent-winner"

    def race(fd):
        real_fsync(fd)
        if not changed and stat.S_ISREG(os.fstat(fd).st_mode):
            if damage == "content":
                marker.write_bytes(winner)
            else:
                candidate = marker.with_name("foreign-winner")
                candidate.write_bytes(winner)
                protected_policy.owner(candidate, 987654)
                os.replace(candidate, marker)
            changed.append(True)

    monkeypatch.setattr(os, "fsync", race)
    with pytest.raises((tx.PendingTransactionError, FileExistsError)):
        replacement._register(parent / "next", journal, "exact", "archive")
    assert changed == [True]
    assert marker.read_bytes() == winner


def test_registration_preserves_foreign_winner_at_final_publication(protected_policy, guarded_parent, monkeypatch):
    parent, marker, journal = guarded_parent
    real_link = os.link
    changed = []
    winner = b"final-foreign-winner"

    def race(source, target, *args, **kwargs):
        if target == replacement.GUARD and not changed:
            marker.write_bytes(winner)
            protected_policy.owner(marker, 987654)
            changed.append(True)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(tx.PendingTransactionError):
        replacement._register(parent / "next", journal, "exact", "archive")
    assert changed == [True]
    assert marker.read_bytes() == winner


def test_registration_restore_never_overwrites_a_late_foreign_winner(
        protected_policy, guarded_parent, monkeypatch):
    parent, marker, journal = guarded_parent
    before = marker.read_bytes()
    real_read = replacement._read
    real_restore = tx._atomic_rename_no_replace
    restored = []

    def reject_quarantined(name, *args, **kwargs):
        if "graphify-merge-backup" in str(name):
            raise tx.PendingTransactionError("injected predecessor refusal")
        return real_read(name, *args, **kwargs)

    def concurrent_restore(cap, source, target):
        if "graphify-merge-backup" in source and target == replacement.GUARD:
            marker.write_bytes(b"restore-winner")
            protected_policy.owner(marker, 987654)
            restored.append(True)
        return real_restore(cap, source, target)

    monkeypatch.setattr(replacement, "_read", reject_quarantined)
    monkeypatch.setattr(tx, "_atomic_rename_no_replace", concurrent_restore)
    with pytest.raises((tx.PendingTransactionError, FileExistsError)):
        replacement._register(parent / "next", journal, "exact", "archive")
    assert restored == [True]
    assert marker.read_bytes() == b"restore-winner"
    backups = list(parent.glob("*graphify-merge-backup*"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    with pytest.raises(tx.PendingTransactionError, match="guard update.*recovery"):
        replacement.guard_output(parent / "archive" / "new")


def test_foreign_owner_change_is_not_ignored(protected_policy, guarded_parent, monkeypatch):
    parent, marker, _ = guarded_parent
    protected_policy.owner(marker, 987654)
    real_entry = replacement._guard_entry
    calls = []

    def retarget(cap):
        info = real_entry(cap)
        if cap.path == parent:
            calls.append(True)
            if len(calls) == 1:
                protected_policy.owner(marker, os.getuid())
        return info

    monkeypatch.setattr(replacement, "_guard_entry", retarget)
    with pytest.raises(tx.PendingTransactionError, match="foreign replacement guard changed"):
        replacement.guard_output(parent / "ordinary")


def test_real_effective_uid_mismatch_refuses_before_policy_read(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(tx.PendingTransactionError) as error:
        replacement.guard_output(tmp_path / "ordinary")
    assert "matching POSIX" in str(error.value.__cause__)


def test_bootstrap_index_appearance_invalidates_preflight(protected_policy, tmp_path):
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    with pytest.raises(issuers.IssuerPolicyError, match="index appeared"):
        with issuers.issuer_snapshot():
            protected_policy.install(namespace)


def test_index_parse_failure_closes_every_admitted_descriptor(protected_policy, guarded_parent, monkeypatch):
    parent, _, _ = guarded_parent
    _, index, _ = protected_policy.install(parent)
    index.write_bytes(b"not-json")
    real_open, real_fstat = os.open, os.fstat
    opened = []

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(parent / "ordinary")
    assert opened
    for fd in set(opened):
        with pytest.raises(OSError):
            real_fstat(fd)


def test_windows_guard_keeps_existing_policy_route(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(replacement, "os", SimpleNamespace(name="nt", path=os.path))
    monkeypatch.setattr(replacement, "_guard_path", lambda path, **kwargs: calls.append(kwargs))

    def unexpected_snapshot():
        pytest.fail("Windows must not use the POSIX issuer reader")

    monkeypatch.setattr(issuers, "issuer_snapshot", unexpected_snapshot)
    replacement.guard_output(tmp_path / "ordinary")
    assert calls and all(call["issuer_policy"] is None for call in calls)


@pytest.mark.parametrize("change", [{"namespace_path": "/a\x00b"}, {"policy_generation": True},
                                   {"mutator_uid": True}, {"legacy_issuer_uids": [1]},
                                   {"legacy_issuer_uids": (2, 1)}, {"namespace_identity": (True, 1)}])
def test_policy_data_does_not_admit_ambiguous_fields(change):
    policy = issuers.NamespaceIssuerPolicy("enrolled", "/namespace", (1, 2), 1, 55, (44,))
    with pytest.raises(issuers.IssuerPolicyError):
        replace(policy, **change)
