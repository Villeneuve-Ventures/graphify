"""Read-only, namespace-scoped replacement-marker issuer policy.

The fixed administrator-owned index is independent of graph requests, markers
and environment variables. This module never enrolls a namespace or writes
policy. Bootstrap absence permits legacy issuers only under the administrator
invariant that the index is never removed while enrolled namespaces/guards live.
Loss or rollback of the entire index is not detectable here. Native custody and
deployment qualification remain separate from these metadata checks.
Policy records remain importable without Unix helpers; POSIX admission loads
its ACL checker on demand and never treats its unavailability as safe custody.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import cast

_DIRECTORY = Path("/private/etc/graphify" if sys.platform == "darwin" else "/etc/graphify")
_INDEX = "replacement-guard-issuers.json"
_RECORDS = "replacement-guard-issuers.d"
_LIMIT = 64 * 1024
_MAX_NAMESPACES = 64
_INDEX_SCHEMA = "graphify.guard-issuer-index.v1"
_POLICY_SCHEMA = "graphify.guard-issuer-policy.v1"


class IssuerPolicyError(ValueError):
    """Issuer policy is malformed, unavailable or changed; admission must stop."""


def _require(value, message):
    if not value:
        raise IssuerPolicyError(message)


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("ascii")


def _path(value):
    _require(type(value) is str and value.startswith("/") and not value.startswith("//")
             and str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts
             and not any(ord(c) < 32 or ord(c) == 127 for c in value)
             and len(value.encode("utf-8")) <= 4096, "canonical issuer namespace path required")
    return value


def _identity(value):
    return (type(value) is tuple and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value))


def _uid(value):
    return type(value) is int and 0 <= value < 2**32


@dataclass(frozen=True)
class NamespaceIssuerPolicy:
    """Immutable policy data, not authenticated enrollment or filesystem authority.

    A reader must obtain this record through the protected index and verify its
    exact digest, generation, namespace path and pinned parent identity. Nested
    namespaces name different marker locations; their issuer sets never inherit.
    Legacy issuers must remain enrolled while their guards survive retirement.
    Invalid fields raise IssuerPolicyError; construction performs no I/O.
    """

    enrollment_id: str
    namespace_path: str
    namespace_identity: tuple[int, int]
    policy_generation: int
    mutator_uid: int
    legacy_issuer_uids: tuple[int, ...]

    def __post_init__(self):
        _require(type(self.enrollment_id) is str and 0 < len(self.enrollment_id) <= 128
                 and all(32 <= ord(c) < 127 for c in self.enrollment_id), "issuer enrollment ID required")
        _path(self.namespace_path)
        _require(_identity(self.namespace_identity)
                 and type(self.policy_generation) is int and self.policy_generation > 0
                 and _uid(self.mutator_uid) and type(self.legacy_issuer_uids) is tuple
                 and len(self.legacy_issuer_uids) <= 128 and all(_uid(uid) for uid in self.legacy_issuer_uids)
                 and self.legacy_issuer_uids == tuple(sorted(set(self.legacy_issuer_uids))),
                 "complete immutable issuer policy required")

    def to_record(self):
        """Return a fresh exact v1 JSON record, with no shared mutable containers."""
        return {"schema": _POLICY_SCHEMA, "enrollment_id": self.enrollment_id,
                "namespace_path": self.namespace_path,
                "namespace_identity": {"device": self.namespace_identity[0], "inode": self.namespace_identity[1]},
                "policy_generation": self.policy_generation, "mutator_uid": self.mutator_uid,
                "legacy_issuer_uids": list(self.legacy_issuer_uids)}

    @property
    def sha256(self):
        """Digest the canonical record; integrity alone does not establish its issuer."""
        return hashlib.sha256(_bytes(self.to_record())).hexdigest()


def _decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate issuer policy field")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs)
        _require(_bytes(value) == data, "issuer policy must use canonical JSON")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IssuerPolicyError("invalid issuer policy JSON") from exc


def _namespace(value):
    _require(type(value) is dict and set(value) == {"device", "inode"}, "issuer parent identity required")
    identity = (value["device"], value["inode"])
    _require(_identity(identity), "issuer parent identity required")
    return identity


def _policy(value):
    fields = {"schema", "enrollment_id", "namespace_path", "namespace_identity",
              "policy_generation", "mutator_uid", "legacy_issuer_uids"}
    _require(type(value) is dict and set(value) == fields and value["schema"] == _POLICY_SCHEMA
             and type(value["legacy_issuer_uids"]) is list, "complete issuer policy record required")
    # The immutable record constructor validates each scalar and UID sequence.
    return NamespaceIssuerPolicy(cast(str, value["enrollment_id"]), cast(str, value["namespace_path"]),
                                 _namespace(value["namespace_identity"]), cast(int, value["policy_generation"]),
                                 cast(int, value["mutator_uid"]), tuple(cast(list[int], value["legacy_issuer_uids"])))


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _acl_safe(fd):
    """Load the Unix custody helper only for actual descriptor admission."""
    from .hook_installation import _acl_safe as check_acl
    return check_acl(fd)


def _admit(fd, *, directory):
    info = os.fstat(fd)
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and info.st_uid == 0 and not info.st_mode & 0o7022
             and (directory or info.st_nlink == 1) and _acl_safe(fd),
             "unsafe issuer policy owner, permissions, type, links or ACL")
    return info


class _RootChain:
    """Pin root-owned policy ancestors and retain a proved-missing component."""

    def __init__(self, path):
        self.entries = []
        self.missing = None
        self.fd = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.entries.append((None, None, fd))
            _admit(fd, directory=True)
            for name in path.parts[1:]:
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=fd)
                except FileNotFoundError:
                    self.missing = (fd, name)
                    break
                self.entries.append((fd, name, child))
                fd = child
                _admit(fd, directory=True)
            else:
                self.fd = fd
            self.validate()
        except BaseException:
            self.close()
            raise

    def validate(self):
        for parent, name, fd in self.entries:
            info = _admit(fd, directory=True)
            named = os.stat("/", follow_symlinks=False) if parent is None else os.stat(
                name, dir_fd=parent, follow_symlinks=False)
            _require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino),
                     "issuer policy ancestor changed")
        if self.missing is not None:
            parent, name = self.missing
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise IssuerPolicyError("issuer policy ancestor appeared")

    def close(self):
        for _, _, fd in reversed(self.entries):
            os.close(fd)
        self.entries.clear()


class _Snapshot:
    def __init__(self, uid):
        self.uid = uid
        self.chains = []
        self.files = []
        self.absent = []
        self.entries = {}
        self.namespace_paths = {}
        self.policies = {}
        self.chain = _RootChain(_DIRECTORY)
        self.chains.append(self.chain)
        if self.chain.fd is None:
            return
        data = self._read(self.chain, _INDEX, optional=True)
        if data is None:
            return
        index = _decode(data)
        _require(type(index) is dict and set(index) == {"schema", "namespaces"}
                 and index["schema"] == _INDEX_SCHEMA and type(index["namespaces"]) is list
                 and len(index["namespaces"]) <= _MAX_NAMESPACES, "complete issuer enrollment index required")
        identities = set()
        for entry in cast(list[dict], index["namespaces"]):
            _require(type(entry) is dict and set(entry) == {"enrollment_id", "namespace_path",
                     "namespace_identity", "policy_generation", "policy_sha256"}, "complete issuer index entry required")
            path = _path(entry["namespace_path"])
            identity = _namespace(entry["namespace_identity"])
            digest = entry["policy_sha256"]
            _require(path not in self.entries and identity not in identities
                     and type(entry["enrollment_id"]) is str and 0 < len(entry["enrollment_id"]) <= 128
                     and all(32 <= ord(c) < 127 for c in entry["enrollment_id"])
                     and type(entry["policy_generation"]) is int and entry["policy_generation"] > 0
                     and type(digest) is str and len(digest) == 64
                     and all(c in "0123456789abcdef" for c in digest), "invalid or duplicate issuer enrollment")
            self.entries[path] = entry
            self.namespace_paths[identity] = path
            identities.add(identity)

    def _read(self, chain, name, *, optional=False):
        _require(chain.fd is not None, "enrolled issuer policy directory missing")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=chain.fd)
        except FileNotFoundError:
            if not optional:
                raise IssuerPolicyError("enrolled issuer policy record missing") from None
            self.absent.append((chain.fd, name))
            return None
        try:
            before = _admit(fd, directory=False)
            data = self._payload(fd)
            _require(_signature(before) == _signature(os.fstat(fd)), "issuer policy record changed")
        except BaseException:
            os.close(fd)
            raise
        self.files.append((chain.fd, name, fd, _signature(before), data))
        return data

    @staticmethod
    def _payload(fd):
        _require(os.fstat(fd).st_size <= _LIMIT, "issuer policy record exceeds bound")
        os.lseek(fd, 0, os.SEEK_SET)
        chunks, remaining = [], _LIMIT + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        _require(remaining > 0, "issuer policy record exceeds bound")
        return b"".join(chunks)

    def trusted_issuers(self, parent, identity, *, selected_parent=None):
        """Resolve one exact guard-parent scope; changed/missing enrollment holds."""
        if selected_parent is not None and str(selected_parent) in self.entries:
            _require(str(selected_parent) == str(parent), "enrolled issuer namespace path changed")
        entry = self.entries.get(str(parent))
        result = {0, self.uid}
        if entry is None:
            _require(identity not in self.namespace_paths, "enrolled issuer namespace path changed")
            return result
        _require(identity == _namespace(entry["namespace_identity"]), "enrolled issuer parent identity changed")
        if str(parent) not in self.policies:
            chain = _RootChain(_DIRECTORY / _RECORDS)
            self.chains.append(chain)
            data = self._read(chain, entry["policy_sha256"] + ".json")
            _require(data is not None, "enrolled issuer policy record missing")
            data = cast(bytes, data)
            _require(hashlib.sha256(data).hexdigest() == entry["policy_sha256"], "issuer policy digest changed")
            policy = _policy(_decode(data))
            _require(policy.namespace_path == str(parent) and policy.namespace_identity == identity
                     and policy.enrollment_id == entry["enrollment_id"]
                     and policy.policy_generation == entry["policy_generation"], "issuer policy binding changed")
            self.policies[str(parent)] = policy
        policy = self.policies[str(parent)]
        result.update((policy.mutator_uid, *policy.legacy_issuer_uids))
        return result

    def validate(self):
        for chain in self.chains:
            chain.validate()
        for parent, name in self.absent:
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise IssuerPolicyError("issuer enrollment index appeared")
        for parent, name, fd, signature, data in self.files:
            info = _admit(fd, directory=False)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _require(_signature(info) == signature == _signature(named)
                     and self._payload(fd) == data and _signature(os.fstat(fd)) == signature,
                     "issuer policy changed during admission")

    def close(self):
        for _, _, fd, _, _ in reversed(self.files):
            os.close(fd)
        for chain in reversed(self.chains):
            chain.close()


@contextmanager
def issuer_snapshot():
    """Yield a read-only protected issuer snapshot for one POSIX guard preflight.

    Real/effective UID mismatch, unsafe metadata, unavailable index evidence,
    changed bindings or missing referenced policies refuse. Only proved bootstrap
    absence permits UID0/current-real-UID fallback. This creates no policy files,
    grants no mutation authority and does not detect protected-index rollback.
    OSError and IssuerPolicyError propagate; every admitted descriptor is closed.
    """
    _require(os.name == "posix" and os.getuid() == os.geteuid(), "matching POSIX caller identities required")
    _require(all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")),
             "safe issuer policy descriptor operations unavailable")
    snapshot = _Snapshot.__new__(_Snapshot)
    try:
        snapshot.__init__(os.getuid())
        snapshot.validate()
        yield snapshot
        snapshot.validate()
    finally:
        snapshot.close()
