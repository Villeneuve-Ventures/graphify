"""Descriptor-anchored, resumable hook publication for macOS and Linux.

The private authority store is trusted; hook directories and their contents are
not. Installer locks exclude other installers, not editors. Completed staging
is retained: removing an object after a pathname comparison is not atomic CAS.
"""
from __future__ import annotations

from contextlib import contextmanager
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
_PREFIX = ".graphify-hook-batch-"
_SCHEMA = "graphify.hook-installation.v1"
_MKDIR_CODE = """import os, sys
try:
    os.mkdir(sys.argv[1], 0o700, dir_fd=int(sys.argv[2]))
except OSError as exc:
    print(exc.errno)
    sys.exit(1)
"""


def _state_root():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/graphify/hook-installations"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "graphify/hook-installations"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _identity(info):
    return [info.st_dev, info.st_ino]


def _mkdir_private(fd, name):
    # Set permissions at birth without changing this process's umask or
    # chmodding a pathname that could have been replaced after mkdir.
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", _MKDIR_CODE, name, str(fd)],
                            pass_fds=(fd,), umask=0o077, capture_output=True, text=True)
    if result.returncode:
        if result.returncode == 1 and result.stdout.strip().isdigit():
            error = int(result.stdout)
            raise OSError(error, os.strerror(error), name)
        raise RuntimeError("Private directory creation failed; retain uncertain state")


def _darwin_xattrs(fd, values=None):
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
    if sys.platform == "darwin":
        return {"xattrs": _darwin_xattrs(fd), "acl": _darwin_acl(fd)}
    return {"xattrs": {name: os.getxattr(fd, name).hex() for name in sorted(os.listxattr(fd))}, "acl": None}


def _copy_metadata(fd, metadata):
    current = _extended(fd)
    changed = {name: value for name, value in metadata["xattrs"].items()
               if current["xattrs"].get(name) != value}
    if sys.platform == "darwin":
        _darwin_xattrs(fd, changed)
        if metadata["acl"] is not None and metadata["acl"] != current["acl"]:
            _darwin_acl(fd, metadata["acl"])
    else:
        for name, value in changed.items():
            os.setxattr(fd, name, bytes.fromhex(value))


def _acl_safe(fd):
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
    info = os.fstat(fd)
    owners = (os.getuid(),) if private else (0, os.getuid())
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
            or info.st_mode & 0o022 or not _acl_safe(fd)
            or (private and stat.S_IMODE(info.st_mode) != 0o700)):
        raise RuntimeError("Unsafe installer authority directory owner, mode or ACL")
    return info


@contextmanager
def _authority(path, create):
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
            for parent, name, identity in parents:
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
    if snapshot is None:
        return None
    info, data, metadata = snapshot
    result = {"identity": _identity(info), "mode": stat.S_IMODE(info.st_mode),
              "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    if metadata is not None:
        result["metadata"] = metadata
    return result


def _write(fd, name, data, mode, metadata=None):
    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    try:
        view = memoryview(data)
        while view:
            count = os.write(target, view)
            if count <= 0:
                raise OSError("Short installer stage write")
            view = view[count:]
        os.fchmod(target, mode)
        if metadata is not None:
            _copy_metadata(target, metadata)
            if _extended(target) != metadata:
                raise RuntimeError("Hook metadata could not be preserved; retain staging")
            # Preserve the prior installer's chmod effects on POSIX ACL masks.
            os.fchmod(target, mode)
        os.fsync(target)
        if sys.platform == "darwin":
            fcntl.fcntl(target, 51)  # F_FULLFSYNC; errors are not success.
    finally:
        os.close(target)


def _rename(source_fd, source, target_fd, target, exchange=False):
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


class _Store:
    def __init__(self, fd, hooks_identity, create):
        self.fd = fd
        self.hooks_identity = hooks_identity
        key = _read(fd, "capability", private=True)
        if key is None:
            if not create or os.listdir(fd):
                raise RuntimeError("Missing installer authority; retain all recovery files")
            _write(fd, "capability", secrets.token_bytes(32), 0o600)
            os.fsync(fd)
            key = _read(fd, "capability", private=True)
            self.key = key[1]
            self.identity = _signature(key)
            self.body = {"schema": _SCHEMA, "authority": _identity(os.fstat(fd)),
                         "key": self.identity, "hooks": hooks_identity, "history": [], "active": None}
            self.save()
        else:
            self.key, self.identity = key[1], _signature(key)
            document = _read(fd, "journal", private=True)
            if len(self.key) != 32 or document is None:
                raise RuntimeError("Missing or invalid installer journal; retain all recovery files")
            self.body = self._decode(document[1])
        self.guard = _read(fd, "completion", private=True)
        if self.guard is not None:
            obligation = self._decode(self.guard[1])
            batch = obligation["active"]
            if batch is None or not batch.get("retirement", "").startswith("completed-"):
                raise RuntimeError("Invalid completion obligation; retain installer authority")
            terminal = {**obligation, "active": None,
                        "history": obligation["history"] + [self._completion_record(batch)]}
            prior = {**obligation, "active": {k: v for k, v in batch.items() if k != "retirement"}}
            if self.body not in (obligation, prior, terminal):
                raise RuntimeError("Conflicting completion obligation; retain installer authority")
            self.body = obligation
        for completed in self.body["history"]:
            retired = _read(fd, completed["retired"], private=True)
            if _signature(retired) != completed["completion"]:
                raise RuntimeError("Missing or changed completion evidence; retain installer authority")
            self._decode(retired[1])

    def _decode(self, data):
        try:
            envelope = json.loads(data)
            body = envelope["body"]
            signature = hmac.new(self.key, _canonical(body), hashlib.sha256).hexdigest()
            if (not hmac.compare_digest(signature, envelope["seal"])
                    or body["schema"] != _SCHEMA or body["key"] != self.identity
                    or body["authority"] != _identity(os.fstat(self.fd))
                    or body["hooks"] != self.hooks_identity):
                raise ValueError("authority binding")
            return body
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Unauthenticated installer journal; retain all recovery files") from exc

    def _sealed(self):
        return _canonical({"body": self.body,
                           "seal": hmac.new(self.key, _canonical(self.body), hashlib.sha256).hexdigest()})

    def _completion_record(self, batch):
        return {"name": batch["stage"], "identity": batch["identity"],
                "retired": batch["retirement"], "completion": _signature(self.guard)}

    def save(self):
        if _signature(_read(self.fd, "capability", private=True)) != self.identity:
            raise RuntimeError("Installer authority changed; retain all recovery files")
        name = "journal-" + secrets.token_hex(16)
        _write(self.fd, name, self._sealed(), 0o600)
        os.replace(name, "journal", src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)

    def finish(self, check):
        """Keep an authenticated obligation until terminal durability is acknowledged."""
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
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _check_stages(fd, store):
    known = {item["name"] for item in store.body["history"]}
    for item in store.body["history"]:
        try:
            info = os.stat(item["name"], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RuntimeError("Retained staging directory missing; preserve recovery evidence") from exc
        if not stat.S_ISDIR(info.st_mode) or _identity(info) != item["identity"]:
            raise RuntimeError("Retained staging directory changed; preserve recovery evidence")
    if store.body["active"]:
        known.add(store.body["active"]["stage"])
    if any(name.startswith(_PREFIX) and name not in known for name in os.listdir(fd)):
        raise RuntimeError("Unrecognized hook staging; missing ownership authority. Retain it for manual reconciliation")


def _desired(plan, snapshot, operation):
    old = None if snapshot is None else snapshot[1]
    mode = None if snapshot is None else stat.S_IMODE(snapshot[0].st_mode)
    if operation == "install":
        data = plan.data if plan.data is not None else (plan.text.encode("utf-8") if plan.text is not None else old)
        return data, 0o755 if plan.create else mode | 0o111
    return (None, None) if plan.delete else (plan.data if plan.data is not None else old, mode)


def _prepare(fd, store, snapshots, plans, request, operation):
    desired = [_desired(plan, snapshots[name], operation) for name, plan in zip(NAMES, plans, strict=True)]
    rendered = [{"sha256": None if data is None else hashlib.sha256(data).hexdigest(), "mode": mode}
                for data, mode in desired]
    binding = {"request": request, "operation": operation, "rendered": rendered}
    if store.body["active"] is not None:
        if store.body["active"]["binding"] != binding:
            raise RuntimeError("Pending hook batch belongs to a different request; retry the original root, operation, interpreter and output")
        return store.body["active"]
    if all(snapshot is None and data is None or snapshot is not None
           and data == snapshot[1] and mode == stat.S_IMODE(snapshot[0].st_mode)
           for snapshot, (data, mode) in zip(snapshots.values(), desired, strict=True)):
        return None
    stage = _PREFIX + secrets.token_hex(16)
    _mkdir_private(fd, stage)
    stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        _admit(stage_fd, private=True)
        _write(stage_fd, ".gitignore", b"*\n", 0o600)
        os.fsync(stage_fd)  # Persist exclusion before any preimage or successor.
        entries = []
        for index, (name, (data, mode)) in enumerate(zip(NAMES, desired, strict=True)):
            snapshot = snapshots[name]
            old = _signature(snapshot)
            unchanged = snapshot is None and data is None or snapshot is not None and data == snapshot[1] and mode == old["mode"]
            entry = {"name": name, "old": old, "new": old if unchanged else None,
                     "changed": not unchanged, "slot": str(index), "preimage": None}
            if not unchanged:
                if snapshot is not None:
                    _write(stage_fd, "pre-" + str(index), snapshot[1], old["mode"], snapshot[2])
                    entry["preimage"] = _signature(_read(stage_fd, "pre-" + str(index)))
                if data is not None:
                    _write(stage_fd, str(index), data, mode, None if snapshot is None else snapshot[2])
                    entry["new"] = _signature(_read(stage_fd, str(index)))
            entries.append(entry)
        os.fsync(stage_fd)
        os.fsync(fd)
        batch = {"stage": stage, "identity": _identity(os.fstat(stage_fd)), "binding": binding,
                 "entries": entries, "messages": [p.message for p in plans], "phase": "PREPARED", "metadata_version": 1}
        store.body["active"] = batch
        store.save()
        return batch
    finally:
        os.close(stage_fd)


def _apply(fd, stage_fd, entry, publish=True):
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


def run(root, hooks_dir, operation, request, prepare):
    """Publish a complete prepared batch; callers register the driver afterward."""
    fd = os.open(hooks_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        hooks_identity = _identity(os.fstat(fd))
        request = {**request, "root": str(root), "hooks_path": str(hooks_dir)}
        with _authority(_state_root(), True) as (authority_fd, authority_check):
            name = _slot(hooks_identity)
            created = False
            try:
                _mkdir_private(authority_fd, name)
                created = True
                os.fsync(authority_fd)
            except FileExistsError:
                pass
            state_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=authority_fd)
            try:
                state_identity = _identity(_admit(state_fd, private=True))
                store = _Store(state_fd, hooks_identity, created)
                if store.body["active"] is not None and store.body["active"].get("metadata_version") != 1:
                    raise RuntimeError("Legacy pending batch lacks hook metadata binding; retain all evidence for manual reconciliation")
                _check_stages(fd, store)

                def check():
                    authority_check()
                    if (_identity(os.stat(name, dir_fd=authority_fd, follow_symlinks=False)) != state_identity
                            or _identity(os.stat(hooks_dir, follow_symlinks=False)) != hooks_identity):
                        raise RuntimeError("Hook or authority directory changed; retain all recovery files")

                snapshots = {name: _read(fd, name) for name in NAMES}
                plans = prepare(snapshots)
                batch = _prepare(fd, store, snapshots, plans, request, operation)
                if batch is None:
                    check()
                    if any(_signature(_read(fd, name)) != _signature(snapshots[name]) for name in NAMES):
                        raise RuntimeError("Hook set changed during no-op installation")
                    check()
                    return [plan.message for plan in plans]
                stage_fd = os.open(batch["stage"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(_admit(stage_fd, private=True)) != batch["identity"]:
                        raise RuntimeError("Hook staging directory changed; retain all recovery files")
                    check()
                    for entry in batch["entries"]:
                        _apply(fd, stage_fd, entry, publish=False)
                    if batch["phase"] != "APPLYING":
                        batch["phase"] = "APPLYING"
                        store.save()
                    for entry in batch["entries"]:
                        check()
                        if _identity(os.stat(batch["stage"], dir_fd=fd, follow_symlinks=False)) != batch["identity"]:
                            raise RuntimeError("Hook staging path changed; retain the admitted directory")
                        _apply(fd, stage_fd, entry)
                    def completed():
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
                    store.finish(completed)
                    return batch["messages"]
                finally:
                    os.close(stage_fd)
            finally:
                os.close(state_fd)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise RuntimeError(f"Hook installation incomplete: {exc}. Recovery files: {hooks_dir}. "
                           "Retry the exact original command after resolving the reported cause; do not delete unverified staging.") from exc
    finally:
        os.close(fd)


def pending(hooks_dir):
    """Inspect pending authority without creating, updating, or repairing it."""
    fd = os.open(hooks_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = _identity(os.fstat(fd))
        admitted = False

        def absent():
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
                        _check_stages(fd, store)
                        active = store.body["active"]
                        result = None if active is None else f"pending {active['binding']['operation']}; recovery files: {hooks_dir / active['stage']}"
                    finally:
                        os.close(slot_fd)
            return result
        except FileNotFoundError as exc:
            return f"unverified authority: {exc}" if admitted else absent()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            label = "pending/unverified" if admitted else "unverified authority"
            return f"{label}: {exc}"
    finally:
        os.close(fd)
