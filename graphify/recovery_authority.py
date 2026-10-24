"""UID-separated, descriptor-anchored macOS/APFS checkpoint authority.

Provisioning is an administrator operation, never performed by this module.
The fixed CONFIG_PATH contains an exact canonical JSON object::

    {"schema":"graphify.recovery-authority.v1", "backend":"macos-apfs-v1",
     "administrator_uid":0, "reader_uids":[501], "reader_gid":20,
     "store_path":"/private/var/db/graphify/recovery",
     "custody":"administrator approval", "failure_domain":"same host storage"}

All ancestors must be UID 0, non-group/other-writable, deny-only ACL directories.
Configuration and records are UID 0, reader_gid, 0640; store directories are
0750. The administrator preprovisions the stable 0640 '.authority.lock' file.
Every traversed filesystem must be local APFS with ownership enforcement.
Readers must have both an allowed positive UID and membership in reader_gid.
Writers need real/effective UID 0 and a protected isolated installed runtime.
Custody/failure_domain are declarations, not proof of a separate storage device.

Advisory locks coordinate cooperating administrators/readers. They do not fence
root, malicious same-UID processes, host rollback, or physical substitution.
File fsync/F_FULLFSYNC and directory fsync failures propagate; these primitives
alone are not a measured power-loss guarantee. No signing or remote service is
implied. Native protected-store qualification requires separate provisioned proof.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import unicodedata

try:
    import fcntl
except ImportError:  # Keep unsupported hosts importable for bounded refusal.
    fcntl = None

from .hook_installation import _acl_safe

CONFIG_PATH = Path("/private/etc/graphify/recovery-authority.json")
_SCHEMA = "graphify.recovery-authority.v1"
_LOCK = ".authority.lock"
_CONFIG_LIMIT = 16 * 1024
_MAX_BYTES = 1024 * 1024 * 1024


class RecoveryAuthorityError(RuntimeError):
    """The configured authority cannot safely authorize this operation."""


def _fcntl_module():
    module = fcntl
    if module is None:
        raise RecoveryAuthorityError("Recovery authority locking requires native macOS/APFS")
    return module


def canonical_bytes(value) -> bytes:
    """Encode bounded JSON types, without coerced keys, tuples or floats.

    UTF-8 ASCII escapes, sorted keys, compact separators and no trailing newline
    freeze the v1 encoding. Integers are signed 64-bit or unsigned 64-bit values.
    """
    def validate(item, depth=0):
        if depth > 64:
            raise RecoveryAuthorityError("Authority JSON nesting exceeds 64")
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise RecoveryAuthorityError("Authority JSON contains invalid Unicode") from exc
            return
        if item is None or type(item) is bool:
            return
        if type(item) is int and -(2**63) <= item < 2**64:
            return
        if type(item) is list:
            for child in item:
                validate(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                validate(key, depth + 1)
                validate(child, depth + 1)
            return
        raise RecoveryAuthorityError("Authority JSON contains an unsupported value")
    validate(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecoveryAuthorityError("Authority JSON cannot be encoded") from exc


def digest(domain: str, payload: bytes) -> str:
    """Return SHA256('graphify.recovery.v1\\0' + domain + '\\0' + payload)."""
    if (type(domain) is not str or not domain or len(domain) > 128
            or any(ord(char) < 33 or ord(char) > 126 for char in domain)
            or type(payload) is not bytes):
        raise RecoveryAuthorityError("Invalid authority digest domain or payload")
    return hashlib.sha256(b"graphify.recovery.v1\0" + domain.encode("ascii") + b"\0" + payload).hexdigest()


class _StatFS(ctypes.Structure):
    # Darwin SDK sys/mount.h __DARWIN_STRUCT_STATFS64 (64-bit inode ABI).
    _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
                ("blocks", ctypes.c_uint64), ("bfree", ctypes.c_uint64),
                ("bavail", ctypes.c_uint64), ("files", ctypes.c_uint64),
                ("ffree", ctypes.c_uint64), ("fsid", ctypes.c_int32 * 2),
                ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
                ("fstypename", ctypes.c_char * 16),
                ("mntonname", ctypes.c_char * 1024),
                ("mntfromname", ctypes.c_char * 1024),
                ("flags_ext", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 7)]


def _require_platform():
    if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RecoveryAuthorityError("Recovery authority requires native 64-bit macOS/APFS")


def _filesystem(fd):
    _require_platform()
    libc = ctypes.CDLL(None, use_errno=True)
    # Explicit 64-bit-inode symbol avoids the SDK's C preprocessor alias rules.
    function = libc.fstatfs64
    function.argtypes = (ctypes.c_int, ctypes.POINTER(_StatFS))
    function.restype = ctypes.c_int
    value = _StatFS()
    if function(fd, ctypes.byref(value)):
        raise OSError(ctypes.get_errno(), "Cannot inspect recovery authority filesystem")
    if value.fstypename != b"apfs" or not value.flags & 0x1000 or value.flags & 0x200000:
        raise RecoveryAuthorityError("Authority requires local APFS with ownership enforcement")


def _safe(fd, *, directory, gid=None, exact=False):
    value = os.fstat(fd)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected_type(value.st_mode) or value.st_uid != 0
            or value.st_mode & 0o7022 or (not directory and value.st_nlink != 1)
            or not _acl_safe(fd)):
        raise RecoveryAuthorityError("Unsafe authority owner, mode, type, links or ACL")
    if exact and (value.st_gid != gid or stat.S_IMODE(value.st_mode) != (0o750 if directory else 0o640)):
        raise RecoveryAuthorityError("Authority reader group or exact permissions differ")
    _filesystem(fd)
    return value


def _same(left, right):
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev, right.st_ino, stat.S_IFMT(right.st_mode))


def _absolute(path):
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise RecoveryAuthorityError("Invalid authority path") from exc
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or value != os.path.normpath(value) or len(value.encode("utf-8")) > 4096):
        raise RecoveryAuthorityError("Authority path must be absolute and canonical")
    if len(Path(value).parts) > 64:
        raise RecoveryAuthorityError("Authority path has too many components")
    return Path(value)


class _PinnedPath:
    """Hold every ancestor descriptor; recheck names without following links."""

    def __init__(self, path):
        self.path = _absolute(path)
        self.entries = []
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.entries.append((None, None, fd))
            _safe(fd, directory=True)
            for part in self.path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                self.entries.append((fd, part, child))
                _safe(child, directory=True)
                fd = child
            self.fd = fd
            self.validate()
        except BaseException:
            self.close()
            raise

    def validate(self):
        for parent, name, fd in self.entries:
            value = _safe(fd, directory=True)
            named = os.stat("/", follow_symlinks=False) if parent is None else os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not _same(value, named):
                raise RecoveryAuthorityError("Authority ancestor was replaced")

    def close(self):
        for _, _, fd in reversed(self.entries):
            os.close(fd)
        self.entries.clear()


def _parts(name):
    if type(name) is not str or len(name.encode("utf-8")) > 4096:
        raise RecoveryAuthorityError("Invalid authority relative name")
    parts = name.split("/")
    if len(parts) > 32 or any(not part or part in (".", "..", _LOCK)
            or part.startswith(".write-") or len(part.encode("utf-8")) > 255
            or unicodedata.normalize("NFC", part) != part or "\\" in part
            or any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        raise RecoveryAuthorityError("Unsafe or oversized authority relative name")
    return parts


def _read(fd, limit):
    if type(limit) is not int or not 0 <= limit <= _MAX_BYTES:
        raise RecoveryAuthorityError("Invalid authority read bound")
    before = os.fstat(fd)
    if before.st_size > limit:
        raise RecoveryAuthorityError("Authority file exceeds read bound")
    chunks, remaining = [], limit + 1
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if remaining == 0 or limit + 1 - remaining != after.st_size or signature(before) != signature(after):
        raise RecoveryAuthorityError("Authority file changed or exceeds read bound")
    return b"".join(chunks)


def _config(payload):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise RecoveryAuthorityError("Duplicate authority configuration field")
            result[key] = value
        return result
    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecoveryAuthorityError("Invalid authority configuration JSON") from exc
    fields = {"schema", "backend", "administrator_uid", "reader_uids", "reader_gid", "store_path", "custody", "failure_domain"}
    if (type(value) is not dict or set(value) != fields or value["schema"] != _SCHEMA
            or value["backend"] != "macos-apfs-v1" or type(value["administrator_uid"]) is not int
            or value["administrator_uid"] != 0 or type(value["reader_gid"]) is not int
            or not 0 < value["reader_gid"] < 2**32 or type(value["reader_uids"]) is not list
            or not 1 <= len(value["reader_uids"]) <= 128
            or any(type(uid) is not int or not 0 < uid < 2**32 for uid in value["reader_uids"])
            or value["reader_uids"] != sorted(set(value["reader_uids"]))):
        raise RecoveryAuthorityError("Unsupported authority configuration fields or reader policy")
    for field in ("custody", "failure_domain"):
        text = value[field]
        if type(text) is not str or not text.strip() or len(text.encode("utf-8")) > 1024 or any(ord(char) < 32 for char in text):
            raise RecoveryAuthorityError("Authority custody and failure domain must be declared")
    _absolute(value["store_path"])
    if canonical_bytes(value) != payload:
        raise RecoveryAuthorityError("Authority configuration is not canonical JSON")
    return value


def _qualify_runtime():
    """Refuse privileged editable/startup imports; this is not a safe launcher.

    Administrators must invoke the reviewed installed interpreter with -I -B.
    This check cannot undo imports made before it and must not be used to justify
    invoking an untrusted program as root in the first place.
    """
    if os.getuid() != 0 or os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.dont_write_bytecode:
        raise RecoveryAuthorityError("Capture requires UID 0 and an isolated installed Python (-I -B)")
    paths = {Path(sys.executable), Path(__file__)}
    for module in tuple(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if origin:
            paths.add(Path(origin))
    for entry in sys.path:
        if not entry or not Path(entry).is_absolute():
            raise RecoveryAuthorityError("Capture runtime has an unsafe import path")
        path = Path(entry)
        if path.exists():
            paths.add(path)
        else:
            paths.add(path.parent)
    for path in paths:
        # Installed interpreter aliases are resolved only here, never store data.
        path = path.resolve(strict=True)
        directory = path if path.is_dir() else path.parent
        chain = _PinnedPath(directory)
        try:
            if not path.is_dir():
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=chain.fd)
                try:
                    _safe(fd, directory=False)
                finally:
                    os.close(fd)
            chain.validate()
        finally:
            chain.close()


def _sync_file(fd):
    os.fsync(fd)
    _fcntl_module().fcntl(fd, 51)  # Darwin F_FULLFSYNC: errors must not become success.


def qualify_destination(fd):
    """Require a native APFS private directory owned by the unprivileged caller."""
    _require_platform()
    value = os.fstat(fd)
    if (os.getuid() == 0 or os.getuid() != os.geteuid() or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700
            or not _acl_safe(fd)):
        raise RecoveryAuthorityError("Destination requires a private caller-owned directory and safe ACL")
    _filesystem(fd)


class Store:
    """An already locked, protected authority; use only within open_store()."""

    def __init__(self, chain, config, config_digest, writable, revalidate):
        self.path = chain.path
        self.config_digest = config_digest
        value = os.fstat(chain.fd)
        self._identity = {"device": value.st_dev, "inode": value.st_ino}
        self._chain, self._gid, self._writable = chain, config["reader_gid"], writable
        self._revalidate = revalidate
        self._active = True

    @property
    def identity(self):
        return dict(self._identity)

    def _check(self, write=False):
        if not self._active or (write and not self._writable):
            raise RecoveryAuthorityError("Authority store is closed or read-only")
        self._revalidate()

    @contextmanager
    def _parent(self, name, create=False):
        parts = _parts(name)
        self._check(write=create)
        opened, fd = [], self._chain.fd
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o750, dir_fd=fd)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    os.fchown(child, 0, self._gid)
                    os.fchmod(child, 0o750)
                    os.fsync(child)
                    os.fsync(fd)
                opened.append((fd, part, child))
                _safe(child, directory=True, gid=self._gid, exact=True)
                fd = child
            yield fd, parts[-1]
            for parent, part, child in opened:
                _safe(child, directory=True, gid=self._gid, exact=True)
                if not _same(os.fstat(child), os.stat(part, dir_fd=parent, follow_symlinks=False)):
                    raise RecoveryAuthorityError("Authority record ancestor was replaced")
            self._check()
        finally:
            for _, _, child in reversed(opened):
                os.close(child)

    def read(self, relative_name, max_bytes):
        with self._parent(relative_name) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                _safe(fd, directory=False, gid=self._gid, exact=True)
                result = _read(fd, max_bytes)
                if not _same(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    raise RecoveryAuthorityError("Authority record was replaced")
                return result
            finally:
                os.close(fd)

    def exists(self, relative_name):
        try:
            with self._parent(relative_name) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                try:
                    value = os.fstat(fd)
                    _safe(fd, directory=stat.S_ISDIR(value.st_mode), gid=self._gid, exact=True)
                    if not _same(value, os.stat(name, dir_fd=parent, follow_symlinks=False)):
                        raise RecoveryAuthorityError("Authority entry was replaced")
                    return True
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return False

    def create_directory(self, relative_name):
        self._check(write=True)
        with self._parent(relative_name, create=True) as (parent, name):
            os.mkdir(name, 0o750, dir_fd=parent)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                os.fchown(fd, 0, self._gid)
                os.fchmod(fd, 0o750)
                _safe(fd, directory=True, gid=self._gid, exact=True)
                os.fsync(fd)
                os.fsync(parent)
            finally:
                os.close(fd)

    def _write(self, relative_name, payload, replace):
        self._check(write=True)
        if type(payload) is not bytes or len(payload) > _MAX_BYTES:
            raise RecoveryAuthorityError("Invalid authority write payload")
        with self._parent(relative_name) as (parent, name):
            if replace:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        _safe(fd, directory=False, gid=self._gid, exact=True)
                    finally:
                        os.close(fd)
            temporary = ".write-" + secrets.token_hex(16) if replace else name
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=parent)
            try:
                os.fchown(fd, 0, self._gid)
                os.fchmod(fd, 0o640)
                _safe(fd, directory=False, gid=self._gid, exact=True)
                offset = 0
                while offset < len(payload):
                    count = os.write(fd, payload[offset:])
                    if count <= 0:
                        raise RecoveryAuthorityError("Authority write made no progress")
                    offset += count
                _sync_file(fd)
                if not _same(os.fstat(fd), os.stat(temporary, dir_fd=parent, follow_symlinks=False)):
                    raise RecoveryAuthorityError("Authority new record was replaced")
                if replace:
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)

    def write_new(self, relative_name, payload):
        self._write(relative_name, payload, False)

    def replace(self, relative_name, payload):
        self._write(relative_name, payload, True)

    def fsync(self):
        self._check(write=True)
        os.fsync(self._chain.fd)


@contextmanager
def open_store(*, write=False, timeout=10.0):
    """Open only the fixed protected configuration and stable store lock.

    Reads require an unprivileged configured UID. Writes require the protected
    installed administrator runtime. No directory, configuration or lock is
    created implicitly. Lock acquisition is bounded to at most 60 seconds.
    """
    _require_platform()
    if type(write) is not bool or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
        raise RecoveryAuthorityError("Invalid authority access or lock deadline")
    if write:
        _qualify_runtime()
    elif os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise RecoveryAuthorityError("Restoration requires an unprivileged configured reader")
    config_chain = store_chain = None
    config_fd = lock_fd = None
    store = None
    try:
        config_chain = _PinnedPath(CONFIG_PATH.parent)
        config_fd = os.open(CONFIG_PATH.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=config_chain.fd)
        _safe(config_fd, directory=False)
        payload = _read(config_fd, _CONFIG_LIMIT)
        config = _config(payload)
        _safe(config_fd, directory=False, gid=config["reader_gid"], exact=True)
        if not write and (os.getuid() not in config["reader_uids"] or config["reader_gid"] not in {os.getgid(), *os.getgroups()}):
            raise RecoveryAuthorityError("Caller is outside the configured recovery reader policy")
        store_chain = _PinnedPath(config["store_path"])
        _safe(store_chain.fd, directory=True, gid=config["reader_gid"], exact=True)
        lock_fd = os.open(_LOCK, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=store_chain.fd)
        _safe(lock_fd, directory=False, gid=config["reader_gid"], exact=True)

        def revalidate():
            config_chain.validate()
            store_chain.validate()
            _safe(store_chain.fd, directory=True, gid=config["reader_gid"], exact=True)
            _safe(config_fd, directory=False, gid=config["reader_gid"], exact=True)
            _safe(lock_fd, directory=False, gid=config["reader_gid"], exact=True)
            for fd, parent, name in ((config_fd, config_chain.fd, CONFIG_PATH.name), (lock_fd, store_chain.fd, _LOCK)):
                if not _same(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    raise RecoveryAuthorityError("Authority configuration or stable lock was replaced")
            os.lseek(config_fd, 0, os.SEEK_SET)
            if _read(config_fd, _CONFIG_LIMIT) != payload:
                raise RecoveryAuthorityError("Authority configuration changed")

        end = time.monotonic() + timeout
        locking = _fcntl_module()
        while True:
            try:
                locking.flock(lock_fd, (locking.LOCK_EX if write else locking.LOCK_SH) | locking.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise RecoveryAuthorityError("Recovery authority lock deadline exceeded") from exc
                time.sleep(min(0.02, remaining))
        revalidate()
        store = Store(store_chain, config, digest("config", payload), write, revalidate)
        yield store
        revalidate()
    except (OSError, ValueError) as exc:
        if store is not None:
            raise
        raise RecoveryAuthorityError("Cannot access qualified recovery authority: " + str(exc)) from exc
    finally:
        if store is not None:
            store._active = False
        if lock_fd is not None:
            os.close(lock_fd)
        if config_fd is not None:
            os.close(config_fd)
        if store_chain is not None:
            store_chain.close()
        if config_chain is not None:
            config_chain.close()
