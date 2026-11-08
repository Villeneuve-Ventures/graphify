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
    """Model root ownership, safe ancestor modes and ACLs on disposable files.

    Host ancestors can be shared writable directories. Their synthetic policy
    modes are independent of that host layout; mode() injects explicit directory
    faults. Policy-file permissions remain real and are not masked.
    """
    directory = tmp_path / "controller-policy"
    directory.mkdir()
    records = directory / issuers._RECORDS
    records.mkdir()
    owners = {}
    modes = {}
    real_stat, real_fstat = os.stat, os.fstat

    def owner(path, uid=0):
        info = real_stat(path, follow_symlinks=False)
        owners[(info.st_dev, info.st_ino)] = uid

    for path in (directory, *directory.parents, records):
        owner(path)
        info = real_stat(path, follow_symlinks=False)
        modes[(info.st_dev, info.st_ino)] = info.st_mode & ~0o7022

    def mode(path, permissions):
        info = real_stat(path, follow_symlinks=False)
        modes[(info.st_dev, info.st_ino)] = (info.st_mode & ~0o7777) | permissions

    def metadata(info):
        key = info.st_dev, info.st_ino
        override = {"st_uid": owners.get(key, info.st_uid), "st_mode": modes.get(key, info.st_mode)}
        return SimpleNamespace(**{name: override.get(name, getattr(info, name))
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

    return SimpleNamespace(directory=directory, records=records, owner=owner, mode=mode, install=install)


def test_bootstrap_index_appearance_invalidates_preflight(protected_policy, tmp_path):
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    with pytest.raises(issuers.IssuerPolicyError, match="index appeared"):
        with issuers.issuer_snapshot():
            protected_policy.install(namespace)


@pytest.mark.parametrize("change", [{"namespace_path": "/a\x00b"}, {"policy_generation": True},
                                   {"mutator_uid": True}, {"legacy_issuer_uids": [1]},
                                   {"legacy_issuer_uids": (2, 1)}, {"namespace_identity": (True, 1)}])
def test_policy_data_does_not_admit_ambiguous_fields(change):
    policy = issuers.NamespaceIssuerPolicy("enrolled", "/namespace", (1, 2), 1, 55, (44,))
    with pytest.raises(issuers.IssuerPolicyError):
        replace(policy, **change)
