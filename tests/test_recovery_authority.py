"""Authority schemas and modeled stores; native helper proof is labeled below.

Disposable user-owned fixtures do not qualify an administrator-provisioned
store or the installed UID separation required by acceptance case A14.
"""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from graphify import recovery_authority as authority


def configuration(store):
    return {"schema": "graphify.recovery-authority.v1", "backend": "macos-apfs-v1",
            "administrator_uid": 0, "reader_uids": [os.getuid() or 501],
            "reader_gid": os.getgid() or 20, "store_path": str(store),
            "custody": "disposable modeled test", "failure_domain": "same test storage"}


@pytest.fixture
def modeled(tmp_path, monkeypatch):
    """Model root custody/platform only; use actual descriptors and flock/fsync."""
    root = tmp_path.resolve() / "authority"
    root.mkdir(mode=0o750)
    lock = root / ".authority.lock"
    lock.write_bytes(b"")
    lock.chmod(0o640)
    config_path = tmp_path.resolve() / "config.json"
    config = configuration(root)
    config_path.write_bytes(authority.canonical_bytes(config))
    config_path.chmod(0o640)
    monkeypatch.setattr(authority, "CONFIG_PATH", config_path)
    monkeypatch.setattr(authority, "_require_platform", lambda: None)
    monkeypatch.setattr(authority, "_filesystem", lambda fd: None)
    monkeypatch.setattr(authority, "_qualify_runtime", lambda: None)
    monkeypatch.setattr(authority, "_sync_file", os.fsync)
    monkeypatch.setattr(authority.os, "fchown", lambda *args: None)

    def safe(fd, *, directory, gid=None, exact=False):
        value = os.fstat(fd)
        if (not (stat.S_ISDIR if directory else stat.S_ISREG)(value.st_mode)
                or (not directory and value.st_nlink != 1) or not authority._acl_safe(fd)):
            raise authority.RecoveryAuthorityError("Modeled unsafe type, links or ACL")
        if exact and stat.S_IMODE(value.st_mode) != (0o750 if directory else 0o640):
            raise authority.RecoveryAuthorityError("Modeled unsafe exact permissions")
        return value
    monkeypatch.setattr(authority, "_safe", safe)
    return root, config_path, config


def test_canonical_and_domain_golden_vector():
    payload = authority.canonical_bytes({"z": [True, None, 1], "a": "é"})
    assert payload == b'{"a":"\\u00e9","z":[true,null,1]}'
    assert authority.digest("checkpoint", payload) == hashlib.sha256(
        b"graphify.recovery.v1\0checkpoint\0" + payload).hexdigest()
    assert authority.digest("head", payload) != authority.digest("checkpoint", payload)


@pytest.mark.parametrize("value", [1.0, float("nan"), (1,), {1: "value"}, 2**64, -(2**63)-1, object(), "\ud800", {"\ud800": 1}])
def test_canonical_rejects_non_schema_values(value):
    with pytest.raises(authority.RecoveryAuthorityError):
        authority.canonical_bytes(value)


def test_canonical_depth_is_bounded():
    value = None
    for _ in range(66):
        value = [value]
    with pytest.raises(authority.RecoveryAuthorityError, match="nesting"):
        authority.canonical_bytes(value)


@pytest.mark.parametrize("domain", ["", "bad\0domain", "has space", "é", "x" * 129, None])
def test_invalid_digest_domain(domain):
    with pytest.raises(authority.RecoveryAuthorityError):
        authority.digest(domain, b"value")


def test_config_exact_canonical_fields(tmp_path):
    value = configuration(tmp_path)
    assert authority._config(authority.canonical_bytes(value)) == value
    with pytest.raises(authority.RecoveryAuthorityError, match="canonical"):
        authority._config(json.dumps(value).encode())
    with pytest.raises(authority.RecoveryAuthorityError, match="Duplicate"):
        authority._config(b'{"schema":"one","schema":"two"}')


@pytest.mark.parametrize("field,value", [
    ("administrator_uid", True), ("administrator_uid", 501), ("backend", "linux"),
    ("reader_uids", [0]), ("reader_uids", [501, 501]), ("reader_uids", [502, 501]),
    ("reader_uids", [True]), ("reader_gid", 0), ("reader_gid", True),
    ("store_path", "relative"), ("store_path", "/private/../tmp"),
    ("store_path", 1), ("custody", ""), ("failure_domain", "x\n"), ("unexpected", 1),
])
def test_config_rejects_unsafe_or_unknown_fields(tmp_path, field, value):
    config = configuration(tmp_path)
    config[field] = value
    with pytest.raises(authority.RecoveryAuthorityError):
        authority._config(authority.canonical_bytes(config))


@pytest.mark.parametrize("name", ["", "/absolute", "../file", "a/../file", "a//b", "a\\b", "a\0b",
                                   ".authority.lock", ".write-secret", "x/" + "y" * 256,
                                   "/".join(["a"] * 33), "e\u0301"])
def test_names_refuse_traversal_and_reserved_entries(name):
    with pytest.raises(authority.RecoveryAuthorityError):
        authority._parts(name)


def test_modeled_store_round_trip_and_exclusive_objects(modeled):
    root, _, _ = modeled
    with authority.open_store(write=True) as store:
        assert store.path == root
        assert store.identity == {"device": root.stat().st_dev, "inode": root.stat().st_ino}
        assert len(store.config_digest) == 64
        store.create_directory("checkpoints/digest/artifacts")
        store.create_directory("heads")
        with pytest.raises(FileExistsError):
            store.create_directory("heads")
        store.write_new("checkpoints/digest/artifacts/graph.json", b"graph")
        with pytest.raises(FileExistsError):
            store.write_new("checkpoints/digest/artifacts/graph.json", b"changed")
        assert store.read("checkpoints/digest/artifacts/graph.json", 5) == b"graph"
        store.replace("heads/source.json", b"head1")
        store.replace("heads/source.json", b"head2")
        assert store.read("heads/source.json", 5) == b"head2"
        assert store.exists("heads") and not store.exists("absent/file")
        with pytest.raises(FileNotFoundError):
            store.read("absent", 100)
        store.fsync()
    with pytest.raises(authority.RecoveryAuthorityError, match="closed"):
        store.exists("heads")
    assert not list(root.glob(".write-*"))


def test_modeled_reader_never_writes(modeled):
    if os.getuid() == 0:
        pytest.skip("Unprivileged reader policy cannot be modeled by root")
    root, _, _ = modeled
    before = sorted(root.iterdir())
    with authority.open_store() as store:
        assert not store.exists("missing")
        for action in (lambda: store.create_directory("new"), lambda: store.write_new("new", b"x"),
                       lambda: store.replace("new", b"x"), store.fsync):
            with pytest.raises(authority.RecoveryAuthorityError, match="read-only"):
                action()
    assert sorted(root.iterdir()) == before


def test_modeled_oversized_read_and_write(modeled, monkeypatch):
    with authority.open_store(write=True) as store:
        store.write_new("value", b"12345")
        with pytest.raises(authority.RecoveryAuthorityError, match="bound"):
            store.read("value", 4)
        assert store.read("value", 5) == b"12345"
        monkeypatch.setattr(authority, "_MAX_BYTES", authority._CONFIG_LIMIT)
        with pytest.raises(authority.RecoveryAuthorityError, match="payload"):
            store.write_new("over", b"x" * (authority._CONFIG_LIMIT + 1))
        with pytest.raises(authority.RecoveryAuthorityError, match="bound"):
            store.read("value", True)
        assert not store.exists("over")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "mode", "acl"])
def test_modeled_unsafe_record_refuses_without_content_change(modeled, monkeypatch, kind):
    root, _, _ = modeled
    target = root / "unsafe"
    original = root / "original"
    original.write_bytes(b"retained")
    original.chmod(0o640)
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o640)
    else:
        target.write_bytes(b"retained")
        target.chmod(0o666 if kind == "mode" else 0o640)
    with authority.open_store(write=True) as store:
        if kind == "acl":
            real_acl = authority._acl_safe
            monkeypatch.setattr(authority, "_acl_safe", lambda fd: False if os.fstat(fd).st_ino == target.stat().st_ino else real_acl(fd))
        for action in (lambda: store.read("unsafe", 100), lambda: store.exists("unsafe"),
                       lambda: store.replace("unsafe", b"changed")):
            with pytest.raises((authority.RecoveryAuthorityError, OSError)):
                action()
    assert original.read_bytes() == b"retained"
    if kind in ("mode", "acl"):
        assert target.read_bytes() == b"retained"


def test_modeled_stable_lock_replacement_refuses(modeled):
    root, _, _ = modeled
    with pytest.raises(authority.RecoveryAuthorityError, match="stable lock"):
        with authority.open_store(write=True) as store:
            (root / ".authority.lock").rename(root / "old-lock")
            (root / ".authority.lock").write_bytes(b"")
            (root / ".authority.lock").chmod(0o640)
            store.exists("missing")


def test_modeled_changed_config_refuses(modeled):
    _, path, config = modeled
    with pytest.raises(authority.RecoveryAuthorityError, match="configuration changed"):
        with authority.open_store(write=True) as store:
            config["custody"] = "changed"
            path.write_bytes(authority.canonical_bytes(config))
            store.exists("missing")


def test_modeled_lock_timeout_is_bounded(modeled):
    root, _, _ = modeled
    fd = os.open(root / ".authority.lock", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        with pytest.raises(authority.RecoveryAuthorityError, match="deadline"):
            with authority.open_store(write=True, timeout=0.03):
                pytest.fail("Locked store was admitted")
        assert time.monotonic() - start < 1
    finally:
        os.close(fd)


def test_modeled_missing_lock_is_not_created(modeled):
    root, _, _ = modeled
    (root / ".authority.lock").unlink()
    with pytest.raises(authority.RecoveryAuthorityError):
        with authority.open_store(write=True):
            pytest.fail("Missing provisioned lock admitted")
    assert list(root.iterdir()) == []


def test_modeled_reader_policy_refuses_unlisted_uid(modeled):
    _, path, config = modeled
    config["reader_uids"] = [os.getuid() + 1]
    path.write_bytes(authority.canonical_bytes(config))
    with pytest.raises(authority.RecoveryAuthorityError, match="reader policy"):
        with authority.open_store():
            pytest.fail("Unlisted caller admitted")


@pytest.mark.parametrize("mode,uid,nlink", [
    (stat.S_IFREG | 0o666, 0, 1), (stat.S_IFREG | 0o640, 501, 1),
    (stat.S_IFREG | 0o640, 0, 2), (stat.S_IFIFO | 0o640, 0, 1),
    (stat.S_IFREG | 0o4640, 0, 1),
])
def test_modeled_native_admission_refuses_unsafe_metadata(monkeypatch, mode, uid, nlink):
    monkeypatch.setattr(authority.os, "fstat", lambda fd: SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink))
    monkeypatch.setattr(authority, "_acl_safe", lambda fd: True)
    with pytest.raises(authority.RecoveryAuthorityError, match="Unsafe"):
        authority._safe(0, directory=False)


@pytest.mark.parametrize("kind,flags", [(b"nfs", 0x1000), (b"apfs", 0), (b"apfs", 0x201000)])
def test_modeled_native_filesystem_policy_refuses_unqualified_mount(monkeypatch, kind, flags):
    class Function:
        def __call__(self, fd, pointer):
            pointer._obj.fstypename = kind
            pointer._obj.flags = flags
            return 0
    monkeypatch.setattr(authority, "_require_platform", lambda: None)
    monkeypatch.setattr(authority.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(fstatfs64=Function()))
    with pytest.raises(authority.RecoveryAuthorityError, match="local APFS"):
        authority._filesystem(0)


def test_modeled_file_sync_failure_retains_unselected_bytes(modeled, monkeypatch):
    root, _, _ = modeled
    with authority.open_store(write=True) as store:
        store.write_new("head", b"old")
        monkeypatch.setattr(authority, "_sync_file", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "injected sync")))
        with pytest.raises(OSError, match="injected sync"):
            store.replace("head", b"new")
        assert store.read("head", 10) == b"old"
    assert len(list(root.glob(".write-*"))) == 1


def test_modeled_replaced_store_refuses(modeled):
    root, _, _ = modeled
    with pytest.raises(authority.RecoveryAuthorityError, match="ancestor was replaced"):
        with authority.open_store(write=True) as store:
            root.rename(root.with_name("retained-old"))
            root.mkdir(mode=0o750)
            store.create_directory("do-not-create")
    assert not (root / "do-not-create").exists()
    assert not (root.with_name("retained-old") / "do-not-create").exists()


def test_non_macos_refuses_before_configuration(monkeypatch):
    monkeypatch.setattr(authority.sys, "platform", "linux")
    with pytest.raises(authority.RecoveryAuthorityError, match="native"):
        with authority.open_store():
            pytest.fail("Unsupported backend admitted")


def test_privileged_runtime_refuses_editable_nonisolated_python():
    if os.getuid() == 0 and sys.flags.isolated and sys.flags.dont_write_bytecode:
        pytest.skip("This process may be a qualified installed administrator")
    with pytest.raises(authority.RecoveryAuthorityError, match="isolated"):
        authority._qualify_runtime()


@pytest.mark.skipif(sys.platform != "darwin" or os.getuid() == 0,
                    reason="Native unprivileged macOS/APFS helper proof")
def test_native_destination_acl_and_filesystem_helpers(tmp_path):
    """Actual ACL/fstatfs calls; does NOT prove root-store provisioning/A14."""
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert authority._acl_safe(fd)
        authority._filesystem(fd)
        authority.qualify_destination(fd)
        os.fchmod(fd, 0o750)
        with pytest.raises(authority.RecoveryAuthorityError, match="private"):
            authority.qualify_destination(fd)
        with pytest.raises(authority.RecoveryAuthorityError, match="owner"):
            authority._safe(fd, directory=True)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin" or os.getuid() == 0,
                    reason="Native unprivileged macOS ACL rejection proof")
def test_native_destination_write_grant_acl_refuses(tmp_path):
    """Actual native ACL rejection on a disposable caller-owned directory."""
    path = tmp_path / "acl-private"
    path.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a", "everyone allow write", str(path)], check=True, capture_output=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert not authority._acl_safe(fd)
        with pytest.raises(authority.RecoveryAuthorityError, match="ACL"):
            authority.qualify_destination(fd)
    finally:
        os.close(fd)
        subprocess.run(["/bin/chmod", "-a", "everyone allow write", str(path)], check=True, capture_output=True)
