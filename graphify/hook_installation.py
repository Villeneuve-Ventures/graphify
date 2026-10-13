"""Descriptor-anchored, resumable hook publication for macOS and Linux.

The private authority store is trusted; hook directories and their contents are
not. Installer locks exclude other installers, not editors. Completed staging
is retained: removing an object after a pathname comparison is not atomic CAS.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys

NAMES = ("post-commit", "post-checkout", "post-merge")
MERGE_GUARD_NAMES = (*NAMES, "pre-merge-commit", "pre-commit")
_PREFIX = ".graphify-hook-batch-"
_SCHEMA = "graphify.hook-installation.v1"
_LIFECYCLE_SCHEMA = "graphify.hook-installation.v2"
_INITIALIZING = ".initializing-hook-authority-"
_ARCHIVE_PREFIX = ".hook-stage-archive-"
_RECORD_LIMIT = 16 * 1024 * 1024
_MKDIR_CODE = """import os, sys
try:
    os.mkdir(sys.argv[1], 0o700, dir_fd=int(sys.argv[2]))
except OSError as exc:
    print(exc.errno)
    sys.exit(1)
"""


def _state_root():
    """Return the authority path without creating it.

    Use Application Support on macOS; elsewhere use nonempty XDG_STATE_HOME
    or ~/.local/state, with graphify/hook-installations appended.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/graphify/hook-installations"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "graphify/hook-installations"


def _canonical(value):
    """Encode sorted, compact ASCII JSON bytes, rejecting nonfinite numbers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _identity(info):
    """Return the device and inode numbers used to recognize a filesystem object."""
    return [info.st_dev, info.st_ino]


def _mkdir_private(fd, name):
    # Set permissions at birth without changing this process's umask or
    # chmodding a pathname that could have been replaced after mkdir.
    """Request mode 0700 relative to fd with a private umask.

    Linux can inherit setgid from the parent. Do not normalize the resulting
    mode or change the calling process's umask. Propagate filesystem errors as
    OSError; raise RuntimeError if the child fails without a recognized errno.
    """
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", _MKDIR_CODE, name, str(fd)],
                            pass_fds=(fd,), umask=0o077, capture_output=True, text=True)
    if result.returncode:
        if result.returncode == 1 and result.stdout.strip().isdigit():
            error = int(result.stdout)
            raise OSError(error, os.strerror(error), name)
        raise RuntimeError("Private directory creation failed; retain uncertain state")


def _darwin_xattrs(fd, values=None):
    """Read macOS extended attributes as a name-to-hex mapping, or set values.

    Passing values sets only those attributes and returns None. Native access
    failures raise OSError; inconsistent reads raise RuntimeError.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if values is not None:
        libc.fsetxattr.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
        for name, value in values.items():
            data = bytes.fromhex(value)
            if libc.fsetxattr(fd, os.fsencode(name), data, len(data), 0, 0):
                raise OSError(ctypes.get_errno(), f"Cannot preserve hook attribute {name}")
        return
    libc.flistxattr.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    libc.flistxattr.restype = ctypes.c_ssize_t
    libc.fgetxattr.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
    libc.fgetxattr.restype = ctypes.c_ssize_t

    def read(function, prefix, suffix):
        """Read a native variable-length attribute buffer, rejecting size changes."""
        size = function(*prefix, None, 0, *suffix)
        if size < 0:
            raise OSError(ctypes.get_errno(), "Cannot inspect hook attributes")
        buffer = ctypes.create_string_buffer(size)
        if function(*prefix, buffer, size, *suffix) != size:
            raise RuntimeError("Hook attributes changed or could not be read")
        return buffer.raw

    names = read(libc.flistxattr, (fd,), (0,)).split(b"\0")[:-1]
    return {os.fsdecode(name): read(libc.fgetxattr, (fd, name), (0, 0)).hex() for name in sorted(names)}


def _darwin_acl(fd, value=...):
    """Capture a macOS extended ACL as hex, or restore the supplied hex value.

    With value omitted, return None if a linked file has no extended ACL.
    Restoring returns None. Native ACL failures propagate as OSError.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_free.argtypes = (ctypes.c_void_p,)
    if value is not ...:
        libc.acl_copy_int.argtypes = (ctypes.c_void_p,)
        libc.acl_copy_int.restype = ctypes.c_void_p
        libc.acl_set_fd_np.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        acl = libc.acl_copy_int(bytes.fromhex(value))
    else:
        libc.acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
        libc.acl_get_fd_np.restype = ctypes.c_void_p
        acl = libc.acl_get_fd_np(fd, 0x100)
        if not acl and ctypes.get_errno() == errno.ENOENT and os.fstat(fd).st_nlink:
            return None
    if not acl:
        raise OSError(ctypes.get_errno(), "Cannot inspect or preserve hook ACL")
    try:
        if value is not ...:
            if libc.acl_set_fd_np(fd, acl, 0x100):
                raise OSError(ctypes.get_errno(), "Cannot preserve hook ACL")
            return
        libc.acl_size.argtypes = (ctypes.c_void_p,)
        libc.acl_size.restype = ctypes.c_ssize_t
        libc.acl_copy_ext.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t)
        libc.acl_copy_ext.restype = ctypes.c_ssize_t
        size = libc.acl_size(acl)
        if size < 0:
            raise OSError(ctypes.get_errno(), "Cannot inspect hook ACL size")
        buffer = ctypes.create_string_buffer(size)
        count = libc.acl_copy_ext(buffer, acl, size)
        if count < 0:
            raise OSError(ctypes.get_errno(), "Cannot capture hook ACL")
        return buffer.raw[:count].hex()
    finally:
        libc.acl_free(acl)


def _extended(fd):
    """Capture hex-encoded extended attributes and macOS ACL and BSD flags.

    On Linux, ACLs are included among xattrs and the separate acl field is None.
    Metadata access errors propagate.
    """
    if sys.platform == "darwin":
        return {"xattrs": _darwin_xattrs(fd), "acl": _darwin_acl(fd), "flags": os.fstat(fd).st_flags}
    return {"xattrs": {name: os.getxattr(fd, name).hex() for name in sorted(os.listxattr(fd))}, "acl": None}


def _copy_metadata(fd, metadata):
    """Apply captured metadata to fd, leaving unlisted attributes in place.

    Raise RuntimeError for macOS flags other than UF_HIDDEN and UF_NODUMP;
    native metadata access and update failures propagate as OSError.
    """
    current = _extended(fd)
    changed = {name: value for name, value in metadata["xattrs"].items()
               if current["xattrs"].get(name) != value}
    if sys.platform == "darwin":
        flags = metadata["flags"]
        if flags & ~(stat.UF_HIDDEN | stat.UF_NODUMP):
            raise RuntimeError("Unsupported BSD hook flags; retain the original hook and staging")
        _darwin_xattrs(fd, changed)
        if metadata["acl"] is not None and metadata["acl"] != current["acl"]:
            _darwin_acl(fd, metadata["acl"])
        if flags != current["flags"]:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.fchflags.argtypes = (ctypes.c_int, ctypes.c_uint32)
            libc.fchflags.restype = ctypes.c_int
            if libc.fchflags(fd, flags):
                raise OSError(ctypes.get_errno(), "Cannot preserve BSD hook flags")
    else:
        for name, value in changed.items():
            os.setxattr(fd, name, bytes.fromhex(value))


def _acl_safe(fd):
    """Return whether the ACL passes the installer authority policy.

    Linux requires no POSIX ACL attributes; macOS permits only deny entries
    and inspects at most 1,000 entries. Inspection errors propagate as OSError.
    """
    if sys.platform != "darwin":
        return not {"system.posix_acl_access", "system.posix_acl_default"}.intersection(os.listxattr(fd))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    libc.acl_get_tag_type.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    libc.acl_free.argtypes = (ctypes.c_void_p,)
    acl = libc.acl_get_fd_np(fd, 0x100)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT and os.fstat(fd).st_nlink:
            return True  # No FILESEC_ACL property on the admitted descriptor.
        raise OSError(ctypes.get_errno(), "Cannot inspect installer authority ACL")
    try:
        for index in range(1000):
            entry, tag = ctypes.c_void_p(), ctypes.c_int()
            if libc.acl_get_entry(acl, index, ctypes.byref(entry)):
                if ctypes.get_errno() == errno.EINVAL:
                    return True
                raise OSError(ctypes.get_errno(), "Cannot inspect ACL entry")
            if libc.acl_get_tag_type(entry, ctypes.byref(tag)):
                raise OSError(ctypes.get_errno(), "Cannot inspect ACL tag")
            if tag.value != 2:  # Deny-only ACLs cannot grant ancestor mutation.
                return False
        return False
    finally:
        libc.acl_free(acl)


def _admit(fd, private=False):
    """Return directory stat data after checking owner, mode, and ACL safety.

    Allow root or the current user as owner and forbid group/other writes.
    With private=True, require current-user ownership and exactly mode 0700,
    also allowing 02700 on Linux, where directories can inherit setgid.
    Raise RuntimeError for rejected metadata; propagate inspection errors.
    """
    info = os.fstat(fd)
    owners = (os.getuid(),) if private else (0, os.getuid())
    private_modes = (0o700, 0o2700) if sys.platform.startswith("linux") else (0o700,)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
            or info.st_mode & 0o022 or not _acl_safe(fd)
            or (private and stat.S_IMODE(info.st_mode) not in private_modes)):
        raise RuntimeError("Unsafe installer authority directory owner, mode or ACL")
    return info


@contextmanager
def _authority(path, create):
    """Yield an authority directory descriptor and a callback to recheck its path.

    Require an absolute path without '..', safe ancestors, and a private final
    directory. If create is true, create missing components; otherwise missing
    components raise FileNotFoundError. Close all opened descriptors on exit.
    Unsafe or replaced directories raise RuntimeError; other I/O errors propagate.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Installer authority path must be absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    parents = []
    try:
        for name in path.parts[1:]:
            _admit(fd)
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    _mkdir_private(fd, name)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            parents.append((fd, name, _identity(os.fstat(child))))
            fd = child
        _admit(fd, private=True)

        def check():
            """Revalidate authority ancestors, path identities, and the private directory."""
            for parent, name, identity in parents:
                _admit(parent)
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise RuntimeError("Installer authority ancestor changed; retain all recovery files")
            _admit(fd, private=True)

        check()
        yield fd, check
        check()
    finally:
        os.close(fd)
        for parent, _, _ in parents:
            os.close(parent)


def _read(fd, name, private=False):
    """Return (stat, bytes, extended metadata) for a stable file, or None if absent.

    Resolve name relative to fd without following symlinks. With private=True,
    require current-user ownership, mode 0600, and a safe ACL, and omit extended
    metadata. Reject nonregular files, hard links, and detected changes with
    RuntimeError. Other I/O errors propagate; only absence at open returns None.
    """
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            raise RuntimeError(f"Unsafe non-regular {name} hook or recovery file") from exc
        raise
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"Unsafe non-regular {name} hook or recovery file")
        if not private and before.st_nlink != 1:
            raise RuntimeError(f"Unsafe hard-linked {name} hook or recovery file; retain all aliases")
        if private and (before.st_uid != os.getuid() or before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) != 0o600 or not _acl_safe(source)):
            raise RuntimeError(f"Unsafe installer authority file: {name}")
        metadata = None if private else _extended(source)
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(source, min(remaining, 65536))
            if not chunk:
                raise RuntimeError(f"File changed while reading: {name}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if not private and _extended(source) != metadata:
            raise RuntimeError(f"Hook metadata changed while reading: {name}")
        after = os.fstat(source)
        stable = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (os.read(source, 1) or stable(before) != stable(after)
                or _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(after)):
            raise RuntimeError(f"File changed while reading: {name}")
        data = b"".join(chunks)
        return before, data, metadata
    finally:
        os.close(source)


def _signature(snapshot):
    """Summarize snapshot identity, mode, content hash, byte size, and metadata.

    Bind hook UID/GID with extended metadata. Private authority signatures keep
    their existing format so authenticated older completion records still load.
    Return None for an absent snapshot; omit metadata when it was not captured.
    """
    if snapshot is None:
        return None
    info, data, metadata = snapshot
    result = {"identity": _identity(info), "mode": stat.S_IMODE(info.st_mode),
              "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    if metadata is not None:
        result["metadata"] = metadata
        result["ownership"] = [info.st_uid, info.st_gid]
    return result


def _write(fd, name, data: bytes | Callable[[list[int]], bytes], mode, metadata=None, ownership=None):
    """Exclusively create and flush a file relative to fd with the requested mode.

    If supplied, preserve and verify ownership before mode and metadata: chown
    can clear special mode bits or capabilities. Never change a live hook.
    Copy and verify extended metadata before reapplying mode, which
    may adjust POSIX ACL masks. Raise RuntimeError if metadata cannot be preserved;
    propagate I/O errors. A failure may leave the newly created file in place.
    """
    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    try:
        if callable(data):
            data = data(_identity(os.fstat(target)))
        view = memoryview(data)
        while view:
            count = os.write(target, view)
            if count <= 0:
                raise OSError("Short installer stage write")
            view = view[count:]
        if ownership is not None:
            info = os.fstat(target)
            if [info.st_uid, info.st_gid] != ownership:
                try:
                    os.fchown(target, *ownership)
                except OSError as exc:
                    raise RuntimeError("Cannot preserve hook UID/GID; retain the original hooks and staging") from exc
            info = os.fstat(target)
            if [info.st_uid, info.st_gid] != ownership:
                raise RuntimeError("Hook UID/GID could not be preserved; retain staging")
        os.fchmod(target, mode)
        if metadata is not None:
            _copy_metadata(target, metadata)
            if _extended(target) != metadata:
                raise RuntimeError("Hook metadata could not be preserved; retain staging")
            # Preserve the prior installer's chmod effects on POSIX ACL masks.
            os.fchmod(target, mode)
        if ownership is not None:
            info = os.fstat(target)
            if [info.st_uid, info.st_gid] != ownership:
                raise RuntimeError("Hook UID/GID changed during preparation; retain staging")
        os.fsync(target)
        if sys.platform == "darwin":
            fcntl.fcntl(target, 51)  # F_FULLFSYNC; errors are not success.
    finally:
        os.close(target)


def _rename(source_fd, source, target_fd, target, exchange=False):
    """Atomically move between directory descriptors without replacing a target.

    With exchange=True, swap two existing entries instead. Raise RuntimeError
    if the native rename function is unavailable and OSError if it fails.
    This does not flush either directory.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameatx_np" if sys.platform == "darwin" else "renameat2", None)
    if function is None:
        raise RuntimeError("Safe atomic hook publication is unavailable on this host")
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    flag = 2 if exchange else (4 if sys.platform == "darwin" else 1)
    if function(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


# Separate directory-issuance fault seam from live-hook file publication.
_rename_directory = _rename


class _Store:
    def __init__(self, fd, hooks_identity, create, *, schema=_SCHEMA):
        """Load authenticated recovery state bound to the hooks directory identity.

        If create is true, initialize a missing capability only in an empty store.
        Reconcile completion evidence in memory and validate retained history.
        Invalid authority or evidence raises RuntimeError; I/O errors propagate.
        The caller retains ownership of fd.
        """
        self.fd = fd
        self.check: Callable[[], None] | None = None
        self.hooks_identity = hooks_identity
        parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            self.parent_identity = _identity(os.fstat(parent))
        finally:
            os.close(parent)
        key = _read(fd, "capability", private=True)
        if key is None:
            if not create or os.listdir(fd):
                raise RuntimeError("Missing installer authority; retain all recovery files")
            _write(fd, "capability", secrets.token_bytes(32), 0o600)
            os.fsync(fd)
            key = _read(fd, "capability", private=True)
            self.key = key[1]
            self.identity = _signature(key)
            self.body = {"schema": schema, "authority": _identity(os.fstat(fd)),
                         "key": self.identity, "hooks": hooks_identity, "history": [], "active": None}
            if schema == _LIFECYCLE_SCHEMA:
                self.body.update(authority_parent=self.parent_identity, retained=[], contract=1, issuer_uid=os.getuid())
            self.save()
        else:
            self.key, self.identity = key[1], _signature(key)
            document = _read(fd, "journal", private=True)
            if len(self.key) != 32 or document is None:
                raise RuntimeError("Missing or invalid installer journal; retain all recovery files")
            self.body = self._decode(document[1])
            if self.body["schema"] == _LIFECYCLE_SCHEMA and self.body["journal_identity"] != _identity(document[0]):
                raise RuntimeError("Copied or replaced installer journal; retain all evidence")
        self.journal_signature = _signature(_read(fd, "journal", private=True))
        self.guard = _read(fd, "completion", private=True)
        if self.guard is not None:
            obligation = self._decode(self.guard[1])
            batch = obligation["active"]
            if batch is None or not batch.get("retirement", "").startswith("completed-"):
                raise RuntimeError("Invalid completion obligation; retain installer authority")
            terminal = {**obligation, "active": None,
                        "history": obligation["history"] + [self._completion_record(batch)]}
            prior = {**obligation, "active": {k: v for k, v in batch.items() if k != "retirement"}}
            comparable = lambda body: {k: v for k, v in body.items() if k != "journal_identity"}
            if comparable(self.body) not in tuple(comparable(body) for body in (obligation, prior, terminal)):
                raise RuntimeError("Conflicting completion obligation; retain installer authority")
            self.body = obligation
        for completed in self.body["history"]:
            retired = _read(fd, completed["retired"], private=True)
            if _signature(retired) != completed["completion"]:
                raise RuntimeError("Missing or changed completion evidence; retain installer authority")
            self._decode(retired[1])

    def _decode(self, data):
        """Return the journal body after authenticating its seal and directory bindings.

        Convert malformed data and binding failures to RuntimeError; descriptor
        inspection errors propagate as OSError.
        """
        try:
            envelope = json.loads(data)
            body = envelope["body"]
            signature = hmac.new(self.key, _canonical(body), hashlib.sha256).hexdigest()
            if (not hmac.compare_digest(signature, envelope["seal"])
                    or body["schema"] not in (_SCHEMA, _LIFECYCLE_SCHEMA) or body["key"] != self.identity
                    or body["authority"] != _identity(os.fstat(self.fd))
                    or body["hooks"] != self.hooks_identity):
                raise ValueError("authority binding")
            if body["schema"] == _LIFECYCLE_SCHEMA:
                if (body["authority_parent"] != self.parent_identity
                        or type(body["issuer_uid"]) is not int or body["issuer_uid"] != os.getuid()
                        or type(body["contract"]) is not int or body["contract"] not in (1, 2)
                        or len(data) > _RECORD_LIMIT):
                    raise ValueError("authority parent or record size")
                if _canonical(envelope) != data:
                    raise ValueError("noncanonical authority record")
                for record in [*body["history"], *body["retained"], *([body["active"]] if body["active"] else [])]:
                    for field in ("name", "stage", "retired"):
                        if field in record:
                            _component(record[field])
                    if "inventory" in record:
                        for member in record["inventory"]:
                            if member not in {".gitignore", *(str(i) for i in range(5)), *("pre-" + str(i) for i in range(5))}:
                                raise ValueError("unsupported stage inventory")
                active = body["active"]
                if active is not None:
                    if active["phase"] not in ("PREPARING", "EXPOSING", "PREPARED", "APPLYING"):
                        raise ValueError("unsupported batch phase")
                    names = MERGE_GUARD_NAMES if active["binding"]["request"].get("merge_guard_hooks") else NAMES
                    entries = active["entries"]
                    if len(entries) > len(names) or (active["phase"] != "PREPARING" and len(entries) != len(names)):
                        raise ValueError("invalid batch inventory")
                    for index, entry in enumerate(entries):
                        if entry["name"] != names[index] or entry["slot"] != str(index) or type(entry["changed"]) is not bool:
                            raise ValueError("invalid hook entry")
            return body
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Unauthenticated installer journal; retain all recovery files") from exc

    def _sealed(self, body=None):
        """Seal the current or capacity-preflight body as canonical JSON bytes."""
        body = self.body if body is None else body
        data = _canonical({"body": body,
                           "seal": hmac.new(self.key, _canonical(body), hashlib.sha256).hexdigest()})
        if body["schema"] == _LIFECYCLE_SCHEMA and len(data) > _RECORD_LIMIT:
            raise RuntimeError("Installer journal capacity exceeded; retain all evidence")
        return data

    def _completion_record(self, batch):
        """Describe the retained stage and completion evidence for a finished batch."""
        result = {"name": batch["stage"], "identity": batch["identity"],
                  "retired": batch["retirement"], "completion": _signature(self.guard)}
        if self.body["schema"] == _LIFECYCLE_SCHEMA:
            result["inventory"] = batch["completed_inventory"]
        return result

    def save(self):
        """Replace and flush the sealed journal after rechecking the capability.

        Changed authority raises RuntimeError; I/O errors propagate and may leave
        a temporary journal file or an unconfirmed replacement.
        """
        check = getattr(self, "check", None)
        if check is not None:
            check()
        _admit(self.fd, private=True)
        if _signature(_read(self.fd, "capability", private=True)) != self.identity:
            raise RuntimeError("Installer authority changed; retain all recovery files")
        if self.body["schema"] == _LIFECYCLE_SCHEMA:
            # Reserve the maximum native 64-bit inode width before exclusive
            # creation. The callback still binds the actual new file identity.
            self._sealed({**self.body, "journal_identity": [os.fstat(self.fd).st_dev, (1 << 64) - 1]})
        name = "journal-" + secrets.token_hex(16)
        def bound(identity):
            """Bind the exclusive journal object before sealing its actual identity."""
            self.body["journal_identity"] = identity
            return self._sealed()

        _write(self.fd, name, bound if self.body["schema"] == _LIFECYCLE_SCHEMA else self._sealed(), 0o600)
        os.replace(name, "journal", src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        self.journal_signature = _signature(_read(self.fd, "journal", private=True))
        if check is not None:
            check()

    def finish(self, check):
        """Keep an authenticated obligation until terminal durability is acknowledged.

        Call check before and after saving terminal state; its errors propagate.
        Retain completed stages and completion evidence. Validation and I/O failures
        propagate except rename/flush errors after verified completion retirement,
        which leave the durable terminal journal usable and return normally.
        """
        batch = self.body["active"]
        if self.guard is None:
            batch["retirement"] = "completed-" + secrets.token_hex(16)
            _write(self.fd, "completion", self._sealed(), 0o600)
            self.guard = _read(self.fd, "completion", private=True)
        if _signature(_read(self.fd, "completion", private=True)) != _signature(self.guard):
            raise RuntimeError("Completion obligation changed; retain installer authority")
        os.fsync(self.fd)
        check()
        self.body = {**self.body, "active": None,
                     "history": self.body["history"] + [self._completion_record(batch)]}
        self.save()  # Durability fence. A failure leaves the canonical guard pending.
        check()
        retirement_error = None
        try:
            _rename(self.fd, "completion", self.fd, batch["retirement"])
        except OSError as exc:
            retirement_error = exc
            if _read(self.fd, "completion", private=True) is not None:
                raise  # The authenticated obligation remains pending.
        if (_read(self.fd, "completion", private=True) is not None
                or _signature(_read(self.fd, batch["retirement"], private=True)) != _signature(self.guard)):
            raise RuntimeError("Uncertain completion retirement; retain installer authority")
        try:
            os.fsync(self.fd)
        except OSError as exc:
            retirement_error = exc
        if retirement_error is not None:
            # Only optional retirement is uncertain; the terminal journal is durable.
            # A guard that reappears after a crash conservatively requires exact retry.
            print("Hooks durable; completion metadata retirement persistence is uncertain. "
                  f"Retain hook staging and installer authority at {_state_root()}; "
                  "status may report pending recovery after restart.", file=sys.stderr)


def _slot(identity):
    """Return a SHA-256 hex directory name for a hooks directory identity."""
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _component(name):
    """Reject record paths that are not one bounded literal directory entry."""
    if (not isinstance(name, str) or not name or len(os.fsencode(name)) > 255
            or name in (".", "..") or any(c in name for c in ("/", "\\", "\0"))):
        raise ValueError("Invalid installer evidence component")
    return name


def _open_store(authority_fd, hooks_identity, hooks_fd):
    """Open existing authority, or issue a complete v2 slot without replacement.

    Unpublished candidates are preserved on every failure. They are never
    adopted by scanning names, and a canonical collision is not overwritten.
    The caller owns the returned descriptor.
    """
    name = _slot(hooks_identity)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=authority_fd)
    except FileNotFoundError:
        if any(name.startswith(_PREFIX) for name in os.listdir(hooks_fd)):
            raise RuntimeError("Unrecognized hook staging; missing ownership authority. Retain it for manual reconciliation")
        if os.fstat(authority_fd).st_dev != hooks_identity[0]:
            raise RuntimeError("V2 hook authority requires the hooks filesystem; retain all existing evidence")
        # Slot names are diagnostic hints only, never adoption authority.
        candidate = _INITIALIZING + name + "-" + secrets.token_hex(16)
        _mkdir_private(authority_fd, candidate)
        os.fsync(authority_fd)
        fd = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=authority_fd)
        try:
            _admit(fd, private=True)
            store = _Store(fd, hooks_identity, True, schema=_LIFECYCLE_SCHEMA)
            if _identity(os.stat(candidate, dir_fd=authority_fd, follow_symlinks=False)) != _identity(os.fstat(fd)):
                raise RuntimeError("Authority candidate changed; preserve it")
            _rename_directory(authority_fd, candidate, authority_fd, name)
            os.fsync(authority_fd)
            return fd, store
        except BaseException:
            os.close(fd)
            raise
    try:
        _admit(fd, private=True)
        return fd, _Store(fd, hooks_identity, False)
    except BaseException:
        os.close(fd)
        raise


def _active_stage_parent(fd, store):
    """Observe the exact v2 stage at one legal location without changing it."""
    active = store.body["active"]
    name = active["stage"]

    def identity(parent):
        """Read a literal stage entry without following links or claiming ownership."""
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Foreign active stage object; preserve it")
        return _identity(info)

    private, exposed = identity(store.fd), identity(fd)
    expected = active["identity"]
    if exposed is not None and exposed != expected:
        raise RuntimeError("Foreign staging destination; preserve all artifacts")
    if active["phase"] == "PREPARING":
        parent = store.fd if private == expected and exposed is None else None
    elif active["phase"] == "EXPOSING":
        parent = (store.fd if private == expected and exposed is None else
                  fd if private is None and exposed == expected else None)
    else:
        parent = fd if private is None and exposed == expected else None
    if parent is None:
        raise RuntimeError("Active stage missing, substituted or at conflicting locations; preserve all evidence")
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if _identity(_admit(child, private=True)) != expected:
            raise RuntimeError("Active stage changed during inspection; preserve it")
        if active["phase"] == "EXPOSING":
            _completed_inventory(child, active, prepared=True)
        if (identity(store.fd), identity(fd)) != (private, exposed):
            raise RuntimeError("Active stage path changed during inspection; preserve it")
        return parent
    finally:
        os.close(child)


def _completed_stage_present(fd, record):
    """Observe an original's presence after authority and any archive admission.

    Allow absence only for an archived record. A present directory must retain
    its recorded identity; names and this observation grant no ownership rights.
    """
    try:
        info = os.stat(record["name"], dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        if "archive" in record:
            return False
        raise RuntimeError("Retained staging directory missing; preserve recovery evidence") from exc
    if not stat.S_ISDIR(info.st_mode) or _identity(info) != record["identity"]:
        raise RuntimeError("Retained staging directory changed; preserve recovery evidence")
    return True


def _check_partial_archive(store, archive):
    """Check a journal-bound private archive directory without consuming its files.

    Require the recorded identity and recheck its path before closing the
    descriptor. Partial contents remain unverified and untouched.
    """
    name = _component(archive["name"])
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
    try:
        if (_identity(_admit(child, private=True)) != archive["identity"]
                or _identity(os.stat(name, dir_fd=store.fd, follow_symlinks=False)) != archive["identity"]):
            raise RuntimeError("Interrupted archive substituted; preserve it")
    finally:
        os.close(child)


def _check_stages(fd, store):
    """Check retained identities and v2 completed inventories before admission.

    Raise RuntimeError for missing, replaced, or unknown stages without removing
    them. Reject unknown or changed v2 completed members and missing, unsafe or
    substituted bound partial archives. Other filesystem errors propagate.
    Return the observed parent
    descriptor for an active v2 stage, otherwise None.
    """
    known = {item["name"] for item in store.body["history"]}
    active_parent = None
    for item in store.body["history"]:
        if "archive" in item:
            _validate_archive(store, item)
        if not _completed_stage_present(fd, item):
            continue
        if store.body["schema"] == _LIFECYCLE_SCHEMA or "archive" in item:
            _stage_inventory(fd, item)
    if store.body["active"]:
        active = store.body["active"]
        known.add(active["stage"])
        if store.body["schema"] == _LIFECYCLE_SCHEMA:
            active_parent = _active_stage_parent(fd, store)
    if store.body["schema"] == _LIFECYCLE_SCHEMA:
        for item in store.body["retained"]:
            child = os.open(item["name"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
            try:
                if _identity(_admit(child, private=True)) != item["identity"]:
                    raise RuntimeError("Retained preparation changed; preserve all evidence")
            finally:
                os.close(child)
        maintenance = store.body.get("maintenance")
        if maintenance is not None and maintenance.get("archive") is not None:
            _check_partial_archive(store, maintenance["archive"])
    if any(name.startswith(_PREFIX) and name not in known for name in os.listdir(fd)):
        raise RuntimeError("Unrecognized hook staging; missing ownership authority. Retain it for manual reconciliation")
    return active_parent


def _desired(plan, snapshot, operation):
    """Return desired hook bytes and permission bits for an install or uninstall.

    Install uses mode 0755 for new hooks and adds execute bits to existing modes.
    Uninstall preserves the old mode; deletion or absence yields (None, None).
    """
    old = None if snapshot is None else snapshot[1]
    mode = None if snapshot is None else stat.S_IMODE(snapshot[0].st_mode)
    if operation == "install":
        data = plan.data if plan.data is not None else (plan.text.encode("utf-8") if plan.text is not None else old)
        return data, 0o755 if plan.create else mode | 0o111
    return (None, None) if plan.delete else (plan.data if plan.data is not None else old, mode)


def _prepare(fd, store, snapshots, plans, request, operation, names=NAMES):
    """Return a matching pending batch, stage a new one, or return None for no change.

    Plans and snapshots must follow names order. Stage preimages and successors
    for changed hooks and save recovery state before any live publication. A pending
    batch must match request, operation, and rendered content and modes.
    Mismatch or denied stage creation raises RuntimeError; other I/O errors
    propagate. Staging remains in place after failure.
    """
    desired = [_desired(plan, snapshots[name], operation) for name, plan in zip(names, plans, strict=True)]
    rendered = [{"sha256": None if data is None else hashlib.sha256(data).hexdigest(), "mode": mode}
                for data, mode in desired]
    binding = {"request": request, "operation": operation, "rendered": rendered}
    if store.body["active"] is not None:
        active = store.body["active"]
        if active["binding"] != binding:
            raise RuntimeError("Pending hook batch belongs to a different request; retry the original root, operation, interpreter and output")
        if store.body["schema"] == _LIFECYCLE_SCHEMA and active["phase"] == "PREPARING":
            _check_new_hook_acl(fd, any(snapshots[name] is None and data is not None
                                      for name, (data, _) in zip(names, desired, strict=True)))
            if {name: _signature(value) for name, value in snapshots.items()} != active["originals"]:
                raise RuntimeError("Original hooks changed during preparation; retain all evidence")
            child = os.open(active["stage"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
            try:
                if _identity(_admit(child, private=True)) != active["identity"]:
                    raise RuntimeError("Preparation directory changed; preserve it")
            finally:
                os.close(child)
            store.body["retained"].append({"name": active["stage"], "identity": active["identity"],
                                            "binding": active["binding"], "originals": active["originals"],
                                            "entries": active["entries"], "exclusion": active.get("exclusion"),
                                            "reason": "incomplete preparation"})
            store.body["active"] = None
            store.save()
        else:
            if store.body["schema"] == _LIFECYCLE_SCHEMA and active["phase"] == "EXPOSING":
                _expose_stage(fd, store)
            return store.body["active"]
    if all(snapshot is None and data is None or snapshot is not None
           and data == snapshot[1] and mode == stat.S_IMODE(snapshot[0].st_mode)
           for snapshot, (data, mode) in zip(snapshots.values(), desired, strict=True)):
        return None
    if store.body["schema"] == _LIFECYCLE_SCHEMA:
        return _prepare_authenticated(fd, store, snapshots, plans, binding, desired, names)
    stage = _PREFIX + secrets.token_hex(16)
    try:
        _mkdir_private(fd, stage)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM) or exc.filename != stage:
            raise
        raise RuntimeError("Cannot create hook staging: atomic hook updates require permission to create and rename entries "
                           "in the hooks directory. Ask the directory administrator to grant suitable directory access, "
                           "then retry the exact original command") from exc
    stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        _admit(stage_fd, private=True)
        _write(stage_fd, ".gitignore", b"*\n", 0o600)
        os.fsync(stage_fd)  # Persist exclusion before any preimage or successor.
        entries = []
        for index, (name, (data, mode)) in enumerate(zip(names, desired, strict=True)):
            snapshot = snapshots[name]
            old = _signature(snapshot)
            ownership = None if snapshot is None else [snapshot[0].st_uid, snapshot[0].st_gid]
            unchanged = snapshot is None and data is None or snapshot is not None and data == snapshot[1] and mode == old["mode"]
            entry = {"name": name, "old": old, "new": old if unchanged else None,
                     "changed": not unchanged, "slot": str(index), "preimage": None}
            if not unchanged:
                if snapshot is not None:
                    _write(stage_fd, "pre-" + str(index), snapshot[1], old["mode"], snapshot[2], ownership)
                    entry["preimage"] = _signature(_read(stage_fd, "pre-" + str(index)))
                if data is not None:
                    _write(stage_fd, str(index), data, mode, None if snapshot is None else snapshot[2],
                           ownership)
                    entry["new"] = _signature(_read(stage_fd, str(index)))
            entries.append(entry)
        os.fsync(stage_fd)
        os.fsync(fd)
        batch = {"stage": stage, "identity": _identity(os.fstat(stage_fd)), "binding": binding,
                 "entries": entries, "messages": [p.message for p in plans], "phase": "PREPARED", "metadata_version": 1,
                 "ownership_version": 1}
        store.body["active"] = batch
        store.save()
        return batch
    finally:
        os.close(stage_fd)


def _expose_stage(fd, store):
    """Reconcile one authenticated directory at its private or hook-local location."""
    batch = store.body["active"]
    name = batch["stage"]
    _check_new_hook_acl(fd, any(entry["old"] is None and entry["new"] is not None
                              for entry in batch["entries"]))
    def identity(parent):
        """Observe a stage entry for no-replace exposure without changing it."""
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Foreign stage exposure object; preserve it")
        return _identity(info)

    source, destination = identity(store.fd), identity(fd)
    if source == batch["identity"] and destination is None:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
        try:
            if _identity(_admit(child, private=True)) != batch["identity"]:
                raise RuntimeError("Private stage changed before exposure; preserve it")
            _completed_inventory(child, batch, prepared=True)
            try:
                _rename_directory(store.fd, name, fd, name)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EPERM) or exc.filename != name:
                    raise
                raise RuntimeError("Cannot expose hook staging: atomic hook updates require permission to create and rename entries "
                                   "in the hooks directory. Ask the directory administrator to grant suitable directory access, "
                                   "then retry the exact original command") from exc
            if identity(fd) != batch["identity"] or identity(store.fd) is not None:
                raise RuntimeError("Uncertain stage exposure; preserve both locations")
        finally:
            os.close(child)
    elif source is not None or destination != batch["identity"]:
        raise RuntimeError("Foreign or uncertain stage exposure; preserve both locations")
    else:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if _identity(_admit(child, private=True)) != batch["identity"]:
                raise RuntimeError("Exposed stage changed; preserve it")
            _completed_inventory(child, batch, prepared=True)
        finally:
            os.close(child)
    os.fsync(store.fd)
    os.fsync(fd)
    batch["phase"] = "PREPARED"
    store.save()


def _check_new_hook_acl(fd, creating):
    """Refuse new hooks when private staging cannot preserve target ACLs or labels."""
    if not creating:
        return
    if sys.platform == "linux":
        attributes = os.listxattr(fd)
        if "security.selinux" in attributes:
            raise RuntimeError("Cannot preserve hooks-directory SELinux creation context for new hooks during private preparation; "
                               "retain all hooks and evidence")
        unsupported = "system.posix_acl_default" in attributes
    else:
        unsupported = sys.platform == "darwin" and _darwin_acl(fd) is not None
    if unsupported:
        raise RuntimeError("Cannot preserve hooks-directory default or extended ACL for new hooks during private preparation; "
                           "retain all hooks and evidence")


def _prepare_authenticated(fd, store, snapshots, plans, binding, desired, names):
    """Construct a complete stage beneath private authority before exposing it.

    PREPARING permits no publication. Its interrupted objects stay intact when
    an exact retry records a retained attempt and constructs a fresh one.
    """
    if os.fstat(store.fd).st_dev != os.fstat(fd).st_dev:
        raise RuntimeError("V2 preparation requires authority on the hooks filesystem")
    _check_new_hook_acl(fd, any(snapshots[name] is None and data is not None
                              for name, (data, _) in zip(names, desired, strict=True)))
    stage = _PREFIX + secrets.token_hex(16)
    try:
        _mkdir_private(store.fd, stage)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM) or exc.filename != stage:
            raise
        raise RuntimeError("Cannot create private hook preparation. Ask the directory administrator for suitable "
                           f"authority directory access at {_state_root()}, then retry the exact original command") from exc
    child = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
    try:
        batch = {"stage": stage, "identity": _identity(_admit(child, private=True)),
                 "binding": binding, "entries": [], "messages": [p.message for p in plans],
                 "phase": "PREPARING", "metadata_version": 1, "ownership_version": 1,
                 "originals": {name: _signature(value) for name, value in snapshots.items()}}
        os.fsync(store.fd)
        store.body["active"] = batch
        store.save()
        _write(child, ".gitignore", b"*\n", 0o600)
        os.fsync(child)
        batch["exclusion"] = _signature(_read(child, ".gitignore"))
        store.save()
        for index, (name, (data, mode)) in enumerate(zip(names, desired, strict=True)):
            snapshot = snapshots[name]
            old = _signature(snapshot)
            unchanged = (snapshot is None and data is None or snapshot is not None
                         and old is not None and data == snapshot[1] and mode == old["mode"])
            entry = {"name": name, "old": old, "new": old if unchanged else None,
                     "changed": not unchanged, "slot": str(index), "preimage": None}
            parent = os.fstat(fd)
            new_group = parent.st_gid if sys.platform == "darwin" or parent.st_mode & stat.S_ISGID else os.getegid()
            ownership = [os.getuid(), new_group] if snapshot is None else [snapshot[0].st_uid, snapshot[0].st_gid]
            if not unchanged:
                if snapshot is not None:
                    if old is None:
                        raise RuntimeError("Invalid original hook signature; preserve all evidence")
                    _write(child, "pre-" + str(index), snapshot[1], old["mode"], snapshot[2], ownership)
                    entry["preimage"] = _signature(_read(child, "pre-" + str(index)))
                if data is not None:
                    _write(child, str(index), data, mode, None if snapshot is None else snapshot[2], ownership)
                    entry["new"] = _signature(_read(child, str(index)))
            batch["entries"].append(entry)
            os.fsync(child)
            store.save()
        os.fsync(child)
        batch["phase"] = "EXPOSING"
        store.save()
        _expose_stage(fd, store)
        return batch
    finally:
        os.close(child)


def _completed_inventory(stage_fd, batch, *, prepared=False):
    """Require the complete prepared or completed inventory at its boundary."""
    expected = {".gitignore": batch["exclusion"]}
    for entry in batch["entries"]:
        if entry["preimage"] is not None:
            expected["pre-" + entry["slot"]] = entry["preimage"]
        signature = entry["new"] if prepared else entry["old"]
        if entry["changed"] and signature is not None:
            expected[entry["slot"]] = signature
    if set(os.listdir(stage_fd)) != set(expected):
        raise RuntimeError("Unknown completed-stage objects; retain the pending batch")
    if any(_signature(_read(stage_fd, name)) != signature for name, signature in expected.items()):
        raise RuntimeError("Completed stage evidence changed; retain the pending batch")
    return expected


def _check_pending_inventory(stage_fd, batch):
    """Reject changed exclusion or unknown members during mixed-state retries.

    _apply verifies slot presence and signatures against each entry's original
    or published state. Both states must have the same authenticated exclusion
    and may contain only that entry's named evidence.
    """
    allowed = {".gitignore"}
    for entry in batch["entries"]:
        if entry["preimage"] is not None:
            allowed.add("pre-" + entry["slot"])
        if entry["changed"]:
            allowed.add(entry["slot"])
    if (not set(os.listdir(stage_fd)).issubset(allowed)
            or _signature(_read(stage_fd, ".gitignore")) != batch["exclusion"]):
        raise RuntimeError("Pending stage exclusion or inventory changed; preserve all evidence")


def _apply(fd, stage_fd, entry, publish=True):
    """Validate one batch entry and optionally publish its atomic file operation.

    With publish=False, accept either the original or already applied state
    without modifying files. With publish=True, flush directories for changed
    entries, including on retries of an already applied entry. Raise RuntimeError for
    unexpected live, staged, or preimage state; propagate I/O errors. Displaced
    files and preimages remain in staging.
    """
    name, slot = entry["name"], entry["slot"]
    live = _signature(_read(fd, name))
    old, new = entry["old"], entry["new"]
    if entry["preimage"] is not None and _signature(_read(stage_fd, "pre-" + slot)) != entry["preimage"]:
        raise RuntimeError("Hook preimage changed; retain all recovery files")
    staged = _signature(_read(stage_fd, slot))
    if not entry["changed"]:
        if live != old or staged is not None:
            raise RuntimeError(f"Concurrent edit to {name}; retain the pending batch")
        return
    # A prior atomic operation may have succeeded before process/persistence failure.
    if live == new and staged == old:
        if publish:
            os.fsync(stage_fd)
            os.fsync(fd)
        return
    if live != old or staged != new:
        raise RuntimeError(f"Foreign or uncertain state for {name}; retain staging and preimage for manual reconciliation")
    if not publish:
        return
    if old is None:
        _rename(stage_fd, slot, fd, name)
    elif new is None:
        _rename(fd, name, stage_fd, slot)
    else:
        _rename(stage_fd, slot, fd, name, exchange=True)
    os.fsync(stage_fd)
    os.fsync(fd)
    if _signature(_read(fd, name)) != new or _signature(_read(stage_fd, slot)) != old:
        raise RuntimeError(f"Concurrent replacement of {name}; displaced object and preimage retained for manual reconciliation")


def run(root, hooks_dir, operation, request, prepare, *, recapture_request, finalize, names=NAMES):
    """Publish a prepared batch and finalize registration under lifecycle locks.

    operation is 'install' or 'uninstall'. prepare receives a mapping from names
    to snapshots returned by _read and returns plans in that order. Return the
    per-hook messages and finalize result, reusing saved hook messages on retry.
    recapture_request observes context under the lock; it must match the original
    request at admission and later checks. Changed or unreadable context refuses.

    Lock the physical hooks and common Git directories without waiting, prepare
    all selected hooks, and publish atomically per file, not across the batch.
    Retain staging and
    authenticated recovery records; retries must match the original request,
    operation, and rendered result. Call finalize after hook completion (including
    no-ops), holding both locks through driver registration/removal. Registration
    is not part of the hook recovery journal or a crash-atomic transaction.

    Opening or closing hooks_dir can raise OSError. During batch processing,
    OSError, ValueError, KeyError, TypeError, CalledProcessError and RuntimeError
    are wrapped in
    RuntimeError with recovery guidance. Failure may leave partial publication.
    """
    if names not in (NAMES, MERGE_GUARD_NAMES):
        raise ValueError("Unsupported hook batch")

    def check_request():
        """Reject changed or unreadable context without adopting a new binding."""
        try:
            current = recapture_request()
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Cannot revalidate hook request: {exc}; retain all recovery files") from exc
        if current != request:
            raise RuntimeError("Hook request context changed; retry the original request and retain all recovery files")

    fd = os.open(hooks_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    common_fd = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Linked worktrees may select different hooks but git config --local
        # writes their common config. Lock the stable directory, not config's
        # inode, which git replaces on every write.
        common_raw = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True).stdout.strip()
        common_dir = (root / common_raw).resolve()
        common_fd = os.open(common_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        common_identity = _identity(os.fstat(common_fd))
        if common_identity != _identity(os.fstat(fd)):
            fcntl.flock(common_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_request()
        hooks_identity = _identity(os.fstat(fd))
        bound_request = {**request, "root": str(root), "hooks_path": str(hooks_dir)}
        with _authority(_state_root(), True) as (authority_fd, authority_check):
            name = _slot(hooks_identity)
            authority_check()
            state_fd, store = _open_store(authority_fd, hooks_identity, fd)
            try:
                state_identity = _identity(_admit(state_fd, private=True))
                if store.body.get("maintenance") is not None:
                    raise RuntimeError("Pending stage archival; retry the exact retirement request and retain all evidence")
                if store.body["active"] is not None and store.body["active"].get("metadata_version") != 1:
                    raise RuntimeError("Legacy pending batch lacks hook metadata binding; retain all evidence for manual reconciliation")
                if store.body["active"] is not None:
                    active = store.body["active"]
                    if (active.get("ownership_version") != 1
                            or any(not isinstance(signature.get("ownership"), list)
                                   or len(signature["ownership"]) != 2
                                   or any(type(value) is not int or value < 0 for value in signature["ownership"])
                                   for entry in active["entries"] for key in ("old", "new", "preimage")
                                   if (signature := entry[key]) is not None)):
                        raise RuntimeError("Pending batch lacks hook UID/GID binding; retain all evidence for manual reconciliation")
                _check_stages(fd, store)

                def check():
                    """Recheck request, authority and hook identities before proceeding."""
                    check_request()
                    authority_check()
                    if _identity(os.stat(common_dir, follow_symlinks=False)) != common_identity:
                        raise RuntimeError("Common Git directory changed; retain all recovery files")
                    _admit(state_fd, private=True)
                    if (_identity(os.stat(name, dir_fd=authority_fd, follow_symlinks=False)) != state_identity
                            or _identity(os.stat(hooks_dir, follow_symlinks=False)) != hooks_identity):
                        raise RuntimeError("Hook or authority directory changed; retain all recovery files")

                store.check = check
                snapshots = {name: _read(fd, name) for name in names}
                check()
                plans = prepare(snapshots)
                check()
                batch = _prepare(fd, store, snapshots, plans, bound_request, operation, names)
                if batch is None:
                    check()
                    if any(_signature(_read(fd, name)) != _signature(snapshots[name]) for name in names):
                        raise RuntimeError("Hook set changed during no-op installation")
                    check()
                    return [plan.message for plan in plans], finalize()
                stage_fd = os.open(batch["stage"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(_admit(stage_fd, private=True)) != batch["identity"]:
                        raise RuntimeError("Hook staging directory changed; retain all recovery files")
                    check()
                    if store.body["schema"] == _LIFECYCLE_SCHEMA:
                        _check_pending_inventory(stage_fd, batch)
                        _check_new_hook_acl(fd, any(entry["old"] is None and entry["new"] is not None
                                                  for entry in batch["entries"]))
                    for entry in batch["entries"]:
                        _apply(fd, stage_fd, entry, publish=False)
                    if batch["phase"] != "APPLYING":
                        batch["phase"] = "APPLYING"
                        store.save()
                    for entry in batch["entries"]:
                        check()
                        _admit(stage_fd, private=True)
                        if _identity(os.stat(batch["stage"], dir_fd=fd, follow_symlinks=False)) != batch["identity"]:
                            raise RuntimeError("Hook staging path changed; retain the admitted directory")
                        if store.body["schema"] == _LIFECYCLE_SCHEMA:
                            _check_pending_inventory(stage_fd, batch)
                            _check_new_hook_acl(fd, any(entry["old"] is None and entry["new"] is not None
                                                      for entry in batch["entries"]))
                        _apply(fd, stage_fd, entry)
                    def completed():
                        """Require the admitted stage and live hooks to match the completed batch."""
                        check()
                        info = os.stat(batch["stage"], dir_fd=fd, follow_symlinks=False)
                        if (not stat.S_ISDIR(info.st_mode) or _identity(info) != batch["identity"]
                                or _identity(_admit(stage_fd, private=True)) != batch["identity"]):
                            raise RuntimeError("Hook staging path changed before completion; retain the pending batch")
                        for entry in batch["entries"]:
                            _apply(fd, stage_fd, entry, publish=False)
                            if _signature(_read(fd, entry["name"])) != entry["new"]:
                                raise RuntimeError("Hook set changed before completion; retain the pending batch")
                        check()

                    completed()
                    os.fsync(stage_fd)
                    os.fsync(fd)
                    if store.body["schema"] == _LIFECYCLE_SCHEMA:
                        batch["completed_inventory"] = _completed_inventory(stage_fd, batch)
                        store.save()
                    store.finish(completed)
                    check()
                    return batch["messages"], finalize()
                finally:
                    os.close(stage_fd)
            finally:
                os.close(state_fd)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Hook installation incomplete: {exc}. Recovery files: {hooks_dir}. "
                           "Retry the exact original command after resolving the reported cause; do not delete unverified staging.") from exc
    finally:
        if common_fd is not None:
            os.close(common_fd)
        os.close(fd)


def _pending_record(hooks_dir):
    """Inspect pending authority without creating, updating, or repairing it.

    Return None when no pending or unverified recovery is found, otherwise a
    authenticated active binding and message, a history summary, or diagnostic.
    Warnings accompany authenticated observations without granting ownership.
    Missing authority
    with retained staging is unverified;
    invalid authority or recovery records become diagnostic strings. Errors
    opening or initially inspecting hooks_dir propagate as OSError.
    """
    fd = os.open(hooks_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = _identity(os.fstat(fd))
        admitted = False

        def absent():
            """Report unverified staging without authority, or None when no staging exists."""
            if any(name.startswith(_PREFIX) for name in os.listdir(fd)):
                return "pending/unverified: installer authority missing; retain hook staging"
            return None

        try:
            with _authority(_state_root(), False) as (authority_fd, _):
                admitted = True
                try:
                    slot_fd = os.open(_slot(identity), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=authority_fd)
                except FileNotFoundError:
                    result = absent()
                else:
                    try:
                        _admit(slot_fd, private=True)
                        store = _Store(slot_fd, identity, False)
                        active_parent = _check_stages(fd, store)
                        active = store.body["active"]
                        result = None if active is None else {
                            "binding": active["binding"],
                            "message": f"pending {active['binding']['operation']} ({active['phase']}); recovery files: "
                                       f"{(_state_root() / _slot(identity) if active_parent == slot_fd else hooks_dir) / active['stage']}",
                        }
                        maintenance = store.body.get("maintenance")
                        if result is None and maintenance is not None:
                            result = {"binding": maintenance["binding"],
                                      "message": f"pending stage archival for {maintenance['binding']['batch']}; "
                                                 "retry the exact retirement request; original stages retained"}
                        if store.body["schema"] == _LIFECYCLE_SCHEMA:
                            known = {item["name"] for item in store.body["retained"]}
                            known.update(item["archive"]["name"] for item in store.body["history"] if "archive" in item)
                            if active is not None:
                                known.add(active["stage"])
                            if maintenance is not None and maintenance.get("archive") is not None:
                                known.add(maintenance["archive"]["name"])
                            unclaimed = sum(name.startswith((_PREFIX, _ARCHIVE_PREFIX)) and name not in known
                                            for name in os.listdir(slot_fd))
                            candidate_prefix = _INITIALIZING + _slot(identity) + "-"
                            candidates = sum(name.startswith(candidate_prefix) for name in os.listdir(authority_fd))
                            if result is None and (store.body["history"] or store.body["retained"] or unclaimed or candidates):
                                result = {"binding": None, "message": None, "summary": {
                                    "completed": [item["name"] for item in store.body["history"]],
                                    "retained_stages": sum(_completed_stage_present(fd, item) for item in store.body["history"]),
                                    "archived": sum("archive" in item for item in store.body["history"]),
                                    "archive_payload_bytes": sum(item["archive"]["payload_bytes"] for item in store.body["history"] if "archive" in item),
                                    "retained_incomplete": len(store.body["retained"]), "unclaimed": unclaimed + candidates}}
                            if unclaimed and result is not None:
                                warning = "unverified authority: unclaimed private preparation or archive artifacts retained"
                                if result["message"] is None:
                                    result["warning"] = warning
                                else:
                                    result["message"] += "; " + warning
                    finally:
                        os.close(slot_fd)
                candidate_prefix = _INITIALIZING + _slot(identity) + "-"
                if any(name.startswith(candidate_prefix) for name in os.listdir(authority_fd)):
                    warning = "unverified authority: unpublished authority candidates retained"
                    if isinstance(result, dict):
                        if result["message"] is None:
                            result["warning"] = "; ".join(filter(None, (result.get("warning"), warning)))
                        else:
                            result["message"] += "; " + warning
                    else:
                        result = warning if result is None else result + "; " + warning
            return result
        except FileNotFoundError as exc:
            return f"unverified authority: {exc}" if admitted else absent()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            label = "pending/unverified" if admitted else "unverified authority"
            return f"{label}: {exc}"
    finally:
        os.close(fd)


def pending(hooks_dir):
    """Return a read-only pending recovery diagnostic, or None when complete."""
    result = _pending_record(hooks_dir)
    if isinstance(result, dict):
        return result["message"] or result.get("warning")
    return result


def recovery_record(hooks_dir):
    """Return one read-only recovery observation, including authenticated history IDs."""
    return _pending_record(hooks_dir)


def pending_merge_guard_uninstall(hooks_dir):
    """Preserve five-hook retry selection from authenticated pending evidence.

    This is routing only. The remover still requires the complete original
    request and validates recovery under its lock before changing any hook.
    """
    result = _pending_record(hooks_dir)
    if not isinstance(result, dict) or result["binding"] is None:
        return False
    binding = result["binding"]
    return (binding["operation"] == "uninstall"
            and binding["request"].get("merge_guard_hooks") == list(MERGE_GUARD_NAMES))


def _stage_inventory(fd, record):
    """Read only the exact closed inventory of an authenticated completed stage."""
    inventory = record.get("inventory")
    if not isinstance(inventory, dict) or ".gitignore" not in inventory:
        raise RuntimeError("Legacy completed inventory is unverified; preserve all evidence")
    child = os.open(_component(record["name"]), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if _identity(_admit(child, private=True)) != record["identity"]:
            raise RuntimeError("Completed stage substituted; preserve it")
        if set(os.listdir(child)) != set(inventory):
            raise RuntimeError("Unknown or missing completed-stage members; preserve all content")
        snapshots = {}
        for name, signature in inventory.items():
            _component(name)
            if (not isinstance(signature, dict) or "ownership" not in signature or "metadata" not in signature):
                raise RuntimeError("Legacy preservation evidence is unverified; preserve all evidence")
            if sys.platform == "darwin" and "flags" not in signature["metadata"]:
                raise RuntimeError("Legacy BSD flag evidence is unverified; preserve all evidence")
            snapshot = _read(child, name)
            if _signature(snapshot) != signature:
                raise RuntimeError("Completed stage member changed; preserve all content")
            snapshots[name] = snapshot
        if (set(os.listdir(child)) != set(inventory)
                or _identity(os.stat(record["name"], dir_fd=fd, follow_symlinks=False)) != record["identity"]):
            raise RuntimeError("Completed stage changed during inspection; preserve it")
        return snapshots
    finally:
        os.close(child)


def _completion_closure(store, record):
    """Validate all immutable private completion references without dropping history."""
    pending = [record]
    snapshots = {}
    signatures = {}
    while pending:
        reference = pending.pop()
        name = _component(reference["retired"])
        if name in snapshots:
            if signatures[name] != reference["completion"]:
                raise RuntimeError("Conflicting completion reference; preserve all evidence")
            continue
        snapshot = _read(store.fd, name, private=True)
        if _signature(snapshot) != reference["completion"] or snapshot is None:
            raise RuntimeError("Completion closure missing or changed; preserve all evidence")
        document = store._decode(snapshot[1])
        batch = document["active"]
        if batch is None or batch["stage"] != reference["name"] or batch["retirement"] != name:
            raise RuntimeError("Completion reference does not describe its recorded batch")
        if "inventory" in reference and reference["inventory"] != batch.get("completed_inventory"):
            raise RuntimeError("Completion inventory does not match its immutable evidence")
        signatures[name], snapshots[name] = reference["completion"], snapshot
        pending.extend(document["history"])
    return snapshots


def _validate_archive(store, record):
    """Authenticate an archive and every copy using its own object identities."""
    archive = record["archive"]
    child = os.open(_component(archive["name"]), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
    try:
        if _identity(_admit(child, private=True)) != archive["identity"]:
            raise RuntimeError("Archive directory substituted; preserve all evidence")
        manifest = _read(child, "manifest", private=True)
        if manifest is None or _signature(manifest) != archive["manifest"]:
            raise RuntimeError("Archive manifest missing or replaced; preserve all evidence")
        if len(manifest[1]) > _RECORD_LIMIT:
            raise RuntimeError("Archive manifest exceeds its format limit")
        envelope = json.loads(manifest[1])
        body = envelope["body"]
        seal = hmac.new(store.key, _canonical(body), hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(seal, envelope["seal"]) or _canonical(envelope) != manifest[1]
                or body["schema"] != "graphify.hook-stage-archive.v1"
                or body["authority"] != store.body["authority"] or body["key"] != store.identity
                or body["authority_parent"] != store.parent_identity or body["hooks"] != store.hooks_identity
                or body["issuer_uid"] != os.getuid()
                or body["directory"] != archive["identity"] or body["manifest_identity"] != _identity(manifest[0])
                or body["source"] != {"name": record["name"], "identity": record["identity"], "inventory": record["inventory"]}):
            raise RuntimeError("Unauthenticated archive binding; preserve all evidence")
        copies = body["payloads"]
        completions = body["completions"]
        if set(copies) != set(record["inventory"]) or record["retired"] not in completions:
            raise RuntimeError("Incomplete archive evidence closure")
        expected = {"manifest", ".gitignore"}
        if _signature(_read(child, ".gitignore", private=True)) != body["exclusion"]:
            raise RuntimeError("Archive exclusion changed; preserve all evidence")
        for name, entry in [*copies.items(), *completions.items()]:
            member = _component(entry["file"])
            if member in expected:
                raise RuntimeError("Aliased archive inventory")
            expected.add(member)
            snapshot = _read(child, member, private=True)
            if _signature(snapshot) != entry["stored"] or snapshot is None:
                raise RuntimeError("Archive payload changed; preserve all evidence")
            original = record["inventory"][name] if name in copies else entry["original"]
            if (entry["original"] != original or len(snapshot[1]) != original["size"]
                    or hashlib.sha256(snapshot[1]).hexdigest() != original["sha256"]):
                raise RuntimeError("Archive copy does not match its original evidence")
        if set(os.listdir(child)) != expected:
            raise RuntimeError("Unknown archive members; preserve them")
        closure = _completion_closure(store, record)
        if set(completions) != set(closure) or any(
                completions[name]["original"] != _signature(snapshot) for name, snapshot in closure.items()):
            raise RuntimeError("Archive completion closure changed")
        if _identity(os.stat(archive["name"], dir_fd=store.fd, follow_symlinks=False)) != archive["identity"]:
            raise RuntimeError("Archive path changed during verification")
        return body
    finally:
        os.close(child)


def _copy_archive(fd, store, record):
    """Create a private archive; keep all originals and every interrupted copy."""
    maintenance = store.body["maintenance"]
    check = store.check
    if check is None:
        raise RuntimeError("Archive mutation requires its admitted lifecycle check")
    snapshots = _stage_inventory(fd, record)
    closure = _completion_closure(store, record)
    if maintenance.get("archive") is not None:
        prior = maintenance["archive"]
        _check_partial_archive(store, prior)
        store.body["retained"].append({"name": prior["name"], "identity": prior["identity"],
                                       "reason": "incomplete archive", "binding": maintenance["binding"]})
        maintenance["archive"] = None
        store.save()
    name = _ARCHIVE_PREFIX + secrets.token_hex(16)
    _mkdir_private(store.fd, name)
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
    try:
        archive = {"name": name, "identity": _identity(_admit(child, private=True)),
                   "token": maintenance["token"], "binding": maintenance["binding"]}
        maintenance["archive"] = archive
        os.fsync(store.fd)
        store.save()
        _write(child, ".gitignore", b"*\n", 0o600)
        os.fsync(child)
        body = {"schema": "graphify.hook-stage-archive.v1", "authority": store.body["authority"],
                "issuer_uid": os.getuid(),
                "authority_parent": store.parent_identity, "key": store.identity, "hooks": store.hooks_identity,
                "directory": archive["identity"], "source": {"name": record["name"], "identity": record["identity"],
                                                               "inventory": record["inventory"]},
                "exclusion": _signature(_read(child, ".gitignore", private=True)), "payloads": {}, "completions": {}}
        for category, sources in (("payloads", snapshots), ("completions", closure)):
            for index, (original, snapshot) in enumerate(sorted(sources.items())):
                if snapshot is None:
                    raise RuntimeError("Required archive source disappeared")
                member = category + "-" + str(index)
                _write(child, member, snapshot[1], 0o600)
                body[category][original] = {"file": member, "original": _signature(snapshot),
                                            "stored": _signature(_read(child, member, private=True))}
        def manifest(identity):
            body["manifest_identity"] = identity
            data = _canonical({"body": body, "seal": hmac.new(store.key, _canonical(body), hashlib.sha256).hexdigest()})
            if len(data) > _RECORD_LIMIT:
                raise RuntimeError("Archive manifest exceeds its format limit")
            return data
        _write(child, "manifest", manifest, 0o600)
        os.fsync(child)
        os.fsync(store.fd)
        archive["manifest"] = _signature(_read(child, "manifest", private=True))
        archive["payload_bytes"] = sum(len(value[1]) for value in [*snapshots.values(), *closure.values()] if value is not None)
        _validate_archive(store, {**record, "archive": archive})
        _stage_inventory(fd, record)
        check()
        record["archive"] = archive
        store.body["maintenance"] = None
        store.save()
        return archive
    finally:
        os.close(child)


def retire(root, hooks_dir, batch, *, expected=None, recapture_request):
    """Preview, or explicitly archive one completed stage and retire its reference.

    Preview performs no writes or locking. Execution requires its exact token,
    both lifecycle locks and authenticated complete v2 evidence. Nothing is
    removed or moved. Interrupted archives remain private and exact retry keeps
    them before constructing a fresh copy. Existing v1 stores are not migrated.
    """
    _component(batch)
    fd = os.open(hooks_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    common_fd = None
    try:
        if expected is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        common_raw = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                                    capture_output=True, text=True, check=True).stdout.strip()
        common = (root / common_raw).resolve()
        common_fd = os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        hooks_identity, common_identity = _identity(os.fstat(fd)), _identity(os.fstat(common_fd))
        if expected is not None and hooks_identity != common_identity:
            fcntl.flock(common_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = {**recapture_request(), "root": str(root), "hooks_path": str(hooks_dir), "common": common_identity}
        binding = {"operation": "archive-stage", "batch": batch, "request": request}
        with _authority(_state_root(), False) as (authority_fd, authority_check):
            name = _slot(hooks_identity)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=authority_fd)
            try:
                identity = _identity(_admit(child, private=True))
                store = _Store(child, hooks_identity, False)
                if store.body["schema"] != _LIFECYCLE_SCHEMA:
                    raise RuntimeError("Legacy history is retained but not eligible for automatic retirement")
                if store.guard is not None or store.body["active"] is not None:
                    raise RuntimeError("Active hook work or completion obligation blocks retirement")
                def check():
                    authority_check()
                    _admit(child, private=True)
                    current = {**recapture_request(), "root": str(root), "hooks_path": str(hooks_dir), "common": common_identity}
                    if (current != request or _identity(os.stat(common, follow_symlinks=False)) != common_identity
                            or _identity(os.stat(hooks_dir, follow_symlinks=False)) != hooks_identity
                            or _identity(os.stat(name, dir_fd=authority_fd, follow_symlinks=False)) != identity
                            or _signature(_read(child, "capability", private=True)) != store.identity):
                        raise RuntimeError("Retirement authority or request changed; preserve all evidence")
                check()
                _check_stages(fd, store)
                records = [record for record in store.body["history"] if record["name"] == batch]
                if len(records) != 1:
                    raise RuntimeError("Select one authenticated completed batch; names alone grant no authority")
                record = records[0]
                maintenance = store.body.get("maintenance")
                if "archive" in record:
                    archive = record["archive"]
                    if expected is not None:
                        if expected != archive["token"] or binding != archive["binding"]:
                            raise RuntimeError("Retirement retry belongs to a different request")
                        os.fsync(child)
                    check()
                    return {"state": "RETIRED_ARCHIVED", "batch": batch,
                            "physical_stage": "retained" if _completed_stage_present(fd, record) else "absent",
                            "archive": str(_state_root() / name / archive["name"]), "payload_bytes": archive["payload_bytes"]}
                if maintenance is not None:
                    if (binding != maintenance["binding"]
                            or expected is not None and expected != maintenance["token"]):
                        raise RuntimeError("Pending retirement requires its exact original request")
                    if expected is None:
                        check()
                        return {"state": "RETIREMENT_PENDING", "batch": maintenance["binding"]["batch"],
                                "token": maintenance["token"], "physical_stage": "retained"}
                else:
                    snapshots = _stage_inventory(fd, record)
                    closure = _completion_closure(store, record)
                    plan = {"binding": binding, "journal": store.journal_signature, "record": record}
                    token = hmac.new(store.key, _canonical(plan), hashlib.sha256).hexdigest()
                    check()
                    if _signature(_read(child, "journal", private=True)) != store.journal_signature:
                        raise RuntimeError("Retirement preview changed during inspection")
                    if expected is None:
                        return {"state": "COMPLETED_RETAINED", "batch": batch, "token": token,
                                "payload_bytes": sum(len(value[1]) for value in [*snapshots.values(), *closure.values()] if value is not None),
                                "physical_stage": "retained"}
                    if expected != token:
                        raise RuntimeError("Stale retirement preview; inspect again without changing evidence")
                    store.body["contract"] = 2
                    store.body["maintenance"] = {"binding": binding, "token": token, "archive": None}
                    store.check = check
                    store.save()
                store.check = check
                archive = _copy_archive(fd, store, record)
                check()
                return {"state": "RETIRED_ARCHIVED", "batch": batch,
                        "physical_stage": "retained" if _completed_stage_present(fd, record) else "absent",
                        "archive": str(_state_root() / name / archive["name"]), "payload_bytes": archive["payload_bytes"]}
            finally:
                os.close(child)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Stage archival incomplete: {exc}. Preserve hook stages and private authority; "
                           "retry only the exact retirement request or retain evidence for manual reconciliation. No stage was removed.") from exc
    finally:
        if common_fd is not None:
            os.close(common_fd)
        os.close(fd)
