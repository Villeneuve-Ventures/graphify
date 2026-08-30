"""Capability-anchored coordination for project-local graph state.

The module deliberately exposes only closed-form filesystem commits.  Work
that can execute arbitrary Python (serialization, rendering, manifest
construction) must finish before entering :func:`commit_bytes`.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import runpy
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence, cast

import networkx as nx

if os.name != "nt":
    import fcntl


GRAPH_WATERMARK_KEY = "_graphify_protocol"
MANAGED_PUBLICATION_PATHS = (
    "graph.json",
    "GRAPH_REPORT.md",
    ".graphify_labels.json",
    ".graphify_labels.json.sig",
    ".graphify_analysis.json",
    ".graphify_root",
    "manifest.json",
    "graph.html",
    "graphify-callflow.html",
    "GRAPH_TREE.html",
    ".graphify_semantic_marker",
    "needs_update",
    ".graphify_build.json",
    "cost.json",
    "wiki/index.md",
    "obsidian/graph.canvas",
    "graph.graphml",
    "graph.svg",
    "cypher.txt",
)
PROTOCOL_FILE = ".graphify_protocol.json"
TRANSACTION_FILE = ".graphify_transaction.json"
RECEIPT_FILE = ".graphify_generation.json"
QUEUE_FILE = ".graphify_rebuild_queue.jsonl"
QUARANTINE_FILE = ".graphify_rebuild_quarantine.jsonl"
PREPARED_FILE = ".graphify_prepared.json"
LEGACY_PENDING_STATE_FILE = ".graphify_legacy_pending_state.json"
DRAINER_FILE = ".graphify_drainer.json"
TRANSITION_FILE = ".graphify_transition.json"
PREDECESSOR_FILE = ".graphify_predecessor.json"
_COORDINATION_FILES = frozenset(
    {
        PROTOCOL_FILE,
        TRANSACTION_FILE,
        RECEIPT_FILE,
        QUEUE_FILE,
        DRAINER_FILE,
        QUARANTINE_FILE,
        PREPARED_FILE,
        LEGACY_PENDING_STATE_FILE,
        TRANSITION_FILE,
        PREDECESSOR_FILE,
    }
)
_COORDINATION_PREFIXES = (
    ".graphify_transaction_token.",
    ".graphify_rebuild_inflight.",
)
_SAFE_GRAPHLESS_RUNTIME_ENTRIES = frozenset(
    {"cache", "memory", "reflections", ".graphify_python", ".rebuild.lock"}
)
_PLATFORM = "windows" if os.name == "nt" else "posix"
_MAX_STATE_BYTES = 1024 * 1024
_TOKEN_MAX_BYTES = 16 * 1024
_DETACHED_MAX_BYTES = 50 * 1024 * 1024
_DETACHED_MAX_NODES = 100_000
_MAX_RECEIPT_ARTIFACTS = 4096
_MAX_RECEIPT_AGGREGATE_BYTES = 1024 * 1024 * 1024
_MAX_QUEUE_ITEMS = 4096
_MAX_QUEUE_PATHS = 4096
_MAX_QUEUE_PATH_LENGTH = 4096


class PendingTransactionError(RuntimeError):
    """Managed graph state is incomplete, malformed, or owned elsewhere."""


class RecoverableTransactionError(PendingTransactionError):
    """Recovery stopped at its bounded attempt limit without deleting state."""


TransactionKind = Literal["full", "update", "runtime"]


@dataclass(frozen=True)
class OutputIdentity:
    device: int
    inode: int

    def json(self) -> dict[str, int]:
        return {"device": self.device, "inode": self.inode}


@dataclass
class OutputCapability:
    path: Path
    identity: OutputIdentity
    fd: int
    _closed: bool = False

    def validate(self) -> None:
        if self._closed:
            raise PendingTransactionError("output capability is closed")
        try:
            pinned = os.fstat(self.fd)
            named = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise PendingTransactionError("output directory identity is unavailable") from exc
        expected = (self.identity.device, self.identity.inode)
        if (
            not stat.S_ISDIR(pinned.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (pinned.st_dev, pinned.st_ino) != expected
            or (named.st_dev, named.st_ino) != expected
        ):
            raise PendingTransactionError("output directory identity changed")

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            self._closed = True

    def __enter__(self) -> OutputCapability:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass
class PreparedWorkspaceCapability:
    workspace: OutputCapability
    output: OutputCapability

    @property
    def path(self) -> Path:
        return self.workspace.path

    def validate(self) -> None:
        self.workspace.validate()
        self.output.validate()

    def close(self) -> None:
        self.output.close()
        self.workspace.close()


@dataclass(frozen=True)
class DrainerTuple:
    generation: int
    claim_epoch: int
    launch_nonce: str


@dataclass(frozen=True)
class Transaction:
    id: str
    kind: TransactionKind
    root: str
    output: Path
    output_identity: OutputIdentity
    generation: int
    token_digest: str
    token_identity: tuple[int, int] | None
    drainer: DrainerTuple
    phase: str = "building"


@dataclass(frozen=True)
class CancellationRecovery(Transaction):
    predecessor_generation: int = 0

    @property
    def transaction_id(self) -> str:
        return self.id


@dataclass(frozen=True)
class TransactionToken:
    id: str
    path: Path
    generation: int


@dataclass(frozen=True)
class QueueReceipt:
    id: str
    drainer: DrainerTuple
    drain_required: bool = True


@dataclass(frozen=True)
class RebuildClaim:
    transaction_id: str
    items: tuple[dict[str, object], ...]
    quarantined: tuple[dict[str, object], ...]
    inflight_path: Path | None
    drainer: DrainerTuple

    def __iter__(self):
        yield list(self.items)
        yield list(self.quarantined)


@dataclass(frozen=True)
class GenerationReceipt:
    digest: str
    generation: int


@dataclass(frozen=True)
class PublicationPlan:
    """Validated complete generation inventory plus exact intended deletions."""

    payloads: Mapping[str, bytes]
    deletions: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphSnapshot:
    data: dict[str, Any]
    generation: int | None
    graph_path: Path
    payload: bytes
    digest: str
    manifest_payload: bytes | None = None
    artifacts: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class _Authority:
    transaction_id: str
    generation: int
    token_digest: str
    token_identity: tuple[int, int] | None
    output_identity: OutputIdentity
    drainer: DrainerTuple
    root: str
    kind: TransactionKind
    phase: str


_AUTHORITY = contextvars.ContextVar[_Authority | None](
    "graphify_transaction_authority", default=None
)
_LOCKS: dict[tuple[int, int], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_FILE_LOCK_STATE = threading.local()


def _canonical_directory(path: Path) -> Path:
    return path.expanduser().resolve(strict=True)


def _ensure_output(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise PendingTransactionError(f"unsafe graphify output directory: {path}")


def pin_output(
    output: Path | str, *, create: bool = False, mutation: bool = True
) -> OutputCapability:
    """Pin one physical output directory for the operation lifetime.

    Windows is intentionally blocked until the final replace/unlink primitive
    is proven handle-relative on a native runner.  A check-then-use named path
    is not represented as equivalent protection.
    """
    path = Path(output)
    if create:
        _ensure_output(path)
    if _PLATFORM == "windows" and mutation:
        raise PendingTransactionError(
            "Windows non-retargetable final mutation is not proven on this runtime"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = _open_windows_read_directory(path) if _PLATFORM == "windows" else os.open(path, flags)
        info = os.fstat(fd)
        named = path.stat(follow_symlinks=False)
    except OSError as exc:
        with contextlib.suppress(UnboundLocalError, OSError):
            os.close(fd)
        raise PendingTransactionError(f"cannot pin output directory: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
        or bool(getattr(named, "st_reparse_tag", 0))
    ):
        os.close(fd)
        raise PendingTransactionError("output directory identity changed while pinning")
    capability = OutputCapability(
        path.resolve(strict=True), OutputIdentity(info.st_dev, info.st_ino), fd
    )
    capability.validate()
    return capability


def _open_windows_read_directory(path: Path) -> int:
    """Open a non-reparse Windows directory handle for read admission."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32: Any = getattr(ctypes, "windll").kernel32
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path.resolve(strict=True)),
        0x0001,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        get_last_error: Any = getattr(ctypes, "get_last_error", lambda: 0)
        raise OSError(get_last_error(), "cannot open Windows output directory")
    attributes = kernel32.GetFileAttributesW(str(path))
    if attributes == 0xFFFFFFFF or attributes & 0x400:
        kernel32.CloseHandle(handle)
        raise PendingTransactionError("Windows output directory is a reparse point")
    open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
    return open_osfhandle(handle, os.O_RDONLY)


def _open_windows_component(parent_fd: int, name: str, *, directory: bool) -> int:
    """Open one child relative to an already pinned Windows directory handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    if not name or name in {".", ".."} or "\\" in name or "/" in name:
        raise PendingTransactionError("unsafe Windows managed path component")
    buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    object_name = UnicodeString(
        encoded_length,
        encoded_length + 2,
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle")
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        wintypes.HANDLE(get_osfhandle(parent_fd)),
        ctypes.pointer(object_name),
        0x40,
        None,
        None,
    )
    handle = wintypes.HANDLE()
    io_status = IoStatusBlock()
    ntdll: Any = getattr(ctypes, "windll").ntdll
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    nt_create_file.restype = ctypes.c_long
    status_code = nt_create_file(
        ctypes.byref(handle),
        0x0001 | 0x0080 | 0x100000,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        0x1 | 0x2 | 0x4,
        1,
        0x20 | 0x00200000 | (0x1 if directory else 0x40),
        None,
        0,
    )
    if status_code < 0:
        rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
        rtl_status_to_dos_error.argtypes = (ctypes.c_long,)
        rtl_status_to_dos_error.restype = wintypes.ULONG
        dos_error = int(rtl_status_to_dos_error(status_code))
        if dos_error in {2, 3}:
            raise FileNotFoundError(dos_error, name)
        raise OSError(dos_error, f"cannot open Windows managed component: {name}")
    open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
    return open_osfhandle(handle.value, os.O_RDONLY)


def _open_windows_relative_fd(
    capability: OutputCapability, relative_name: str, *, directory: bool = False
) -> int:
    relative = Path(relative_name)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {relative_name}")
    parent_fd = os.dup(capability.fd)
    try:
        for component in relative.parts[:-1]:
            child_fd = _open_windows_component(parent_fd, component, directory=True)
            os.close(parent_fd)
            parent_fd = child_fd
            info = os.fstat(parent_fd)
            if not stat.S_ISDIR(info.st_mode) or bool(
                getattr(info, "st_reparse_tag", 0)
            ):
                raise PendingTransactionError(
                    f"unsafe managed artifact: {relative_name}"
                )
        result = _open_windows_component(
            parent_fd, relative.parts[-1], directory=directory
        )
        info = os.fstat(result)
        if bool(getattr(info, "st_reparse_tag", 0)):
            os.close(result)
            raise PendingTransactionError(f"unsafe managed artifact: {relative_name}")
        return result
    finally:
        os.close(parent_fd)


def _list_entries(capability: OutputCapability) -> list[str]:
    capability.validate()
    entries = os.listdir(capability.path if _PLATFORM == "windows" else capability.fd)
    capability.validate()
    return entries


def _lock_for(capability: OutputCapability) -> threading.RLock:
    key = (capability.identity.device, capability.identity.inode)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def _locked(capability: OutputCapability) -> Iterator[None]:
    with _lock_for(capability):
        capability.validate()
        key = (capability.identity.device, capability.identity.inode)
        held = getattr(_FILE_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _FILE_LOCK_STATE.held = held
        if key in held:
            held[key] += 1
            try:
                yield
                capability.validate()
            finally:
                held[key] -= 1
            return
        lock_fd = os.dup(capability.fd)
        try:
            if _PLATFORM != "windows":
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            capability.validate()
            held[key] = 1
            try:
                yield
                capability.validate()
            finally:
                held.pop(key, None)
        finally:
            if _PLATFORM != "windows":
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _entry_stat(capability: OutputCapability, name: str) -> os.stat_result | None:
    if Path(name).name != name:
        raise PendingTransactionError(f"unsafe managed entry name: {name}")
    fd: int | None = None
    try:
        if _PLATFORM == "windows":
            fd = _open_windows_relative_fd(capability, name)
            info = os.fstat(fd)
        else:
            info = os.stat(name, dir_fd=capability.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        raise PendingTransactionError(f"unsafe non-regular managed entry: {name}")
    return info


def _read_bytes(capability: OutputCapability, name: str, limit: int = _MAX_STATE_BYTES) -> bytes:
    if _PLATFORM == "windows":
        fd = _open_windows_relative_fd(capability, name)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
                raise PendingTransactionError(f"unsafe managed entry: {name}")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > limit:
                raise PendingTransactionError(f"managed entry exceeds size limit: {name}")
            capability.validate()
            return payload
        finally:
            os.close(fd)
    before = _entry_stat(capability, name)
    if before is None:
        raise FileNotFoundError(name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=capability.fd)
    try:
        opened = os.fstat(fd)
        if opened.st_size > limit:
            raise PendingTransactionError(f"managed entry exceeds size limit: {name}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PendingTransactionError(f"managed entry identity changed: {name}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise PendingTransactionError(f"managed entry exceeds size limit: {name}")
        after = _entry_stat(capability, name)
        if after is None or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise PendingTransactionError(f"managed entry replaced while reading: {name}")
        return payload
    finally:
        os.close(fd)


def _read_relative_bytes(
    capability: OutputCapability, relative_name: str, limit: int = 512 * 1024 * 1024
) -> bytes:
    if _PLATFORM == "windows":
        digest, _size, payload = _hash_windows_relative(
            capability,
            relative_name,
            retain=True,
            aggregate_remaining=limit,
        )
        del digest
        if payload is None:
            raise PendingTransactionError(
                f"managed artifact payload was not retained: {relative_name}"
            )
        return payload
    relative = Path(relative_name)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {relative_name}")
    parent_fd = os.dup(capability.fd)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        leaf = relative.parts[-1]
        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise PendingTransactionError(f"unsafe managed artifact: {relative_name}")
        fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise PendingTransactionError(f"managed artifact identity changed: {relative_name}")
            payload = bytearray()
            while len(payload) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > limit:
                raise PendingTransactionError(f"managed artifact exceeds size limit: {relative_name}")
            after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                raise PendingTransactionError(f"managed artifact replaced while reading: {relative_name}")
            return bytes(payload)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PendingTransactionError(f"required managed artifact is missing: {relative_name}") from exc
    finally:
        os.close(parent_fd)


def _hash_relative_bytes(
    capability: OutputCapability,
    relative_name: str,
    *,
    retain: bool = False,
    aggregate_remaining: int,
) -> tuple[str, int, bytes | None]:
    """Hash one identity-pinned artifact without aggregating its body in memory."""
    if _PLATFORM == "windows":
        return _hash_windows_relative(
            capability,
            relative_name,
            retain=retain,
            aggregate_remaining=aggregate_remaining,
        )
    relative = Path(relative_name)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {relative_name}")
    parent_fd = os.dup(capability.fd)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        leaf = relative.parts[-1]
        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > aggregate_remaining:
            raise PendingTransactionError("generation receipt aggregate budget exceeded")
        fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise PendingTransactionError(
                    f"managed artifact identity changed: {relative_name}"
                )
            digest = hashlib.sha256()
            body = bytearray() if retain else None
            size = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > aggregate_remaining:
                    raise PendingTransactionError(
                        "generation receipt aggregate budget exceeded"
                    )
                digest.update(chunk)
                if body is not None:
                    body.extend(chunk)
            after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                raise PendingTransactionError(
                    f"managed artifact replaced while reading: {relative_name}"
                )
            return digest.hexdigest(), size, None if body is None else bytes(body)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PendingTransactionError(
            f"required managed artifact is missing: {relative_name}"
        ) from exc
    finally:
        os.close(parent_fd)


def _hash_windows_relative(
    capability: OutputCapability,
    relative_name: str,
    *,
    retain: bool,
    aggregate_remaining: int,
) -> tuple[str, int, bytes | None]:
    try:
        fd = _open_windows_relative_fd(capability, relative_name)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PendingTransactionError(
            f"required managed artifact is missing: {relative_name}"
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > aggregate_remaining:
            raise PendingTransactionError(f"unsafe managed artifact: {relative_name}")
        digest = hashlib.sha256()
        body = bytearray() if retain else None
        size = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > aggregate_remaining:
                raise PendingTransactionError(
                    "generation receipt aggregate budget exceeded"
                )
            digest.update(chunk)
            if body is not None:
                body.extend(chunk)
        capability.validate()
        return digest.hexdigest(), size, None if body is None else bytes(body)
    finally:
        os.close(fd)


def _replace_bytes(
    capability: OutputCapability,
    name: str,
    payload: bytes,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    capability.validate()
    prior = _entry_stat(capability, name)
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=capability.fd)
    try:
        if prior is not None:
            os.fchmod(fd, stat.S_IMODE(prior.st_mode))
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("atomic write made no progress")
            view = view[count:]
        os.fsync(fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=capability.fd)
        raise
    finally:
        os.close(fd)
    capability.validate()
    try:
        if expected_identity is not None:
            current = _entry_stat(capability, name)
            if current is None or (current.st_dev, current.st_ino) != expected_identity:
                raise PendingTransactionError(f"managed entry identity changed: {name}")
            quarantine = f".{name}.graphify-merge-backup.{secrets.token_hex(16)}"
            os.rename(
                name,
                quarantine,
                src_dir_fd=capability.fd,
                dst_dir_fd=capability.fd,
            )
            os.fsync(capability.fd)
            quarantined = _entry_stat(capability, quarantine)
            if quarantined is None or (
                quarantined.st_dev,
                quarantined.st_ino,
            ) != expected_identity:
                if _entry_stat(capability, name) is None:
                    os.rename(
                        quarantine,
                        name,
                        src_dir_fd=capability.fd,
                        dst_dir_fd=capability.fd,
                    )
                raise PendingTransactionError(f"managed entry changed during quarantine: {name}")
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=capability.fd,
                    dst_dir_fd=capability.fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                winner = _entry_stat(capability, name)
                if winner is None:
                    if _entry_stat(capability, quarantine) is not None:
                        os.rename(
                            quarantine,
                            name,
                            src_dir_fd=capability.fd,
                            dst_dir_fd=capability.fd,
                        )
                    raise PendingTransactionError(
                        f"managed entry replacement disappeared: {name}"
                    ) from exc
                with contextlib.suppress(OSError):
                    os.unlink(quarantine, dir_fd=capability.fd)
                os.fsync(capability.fd)
                capability.validate()
                raise PendingTransactionError(
                    f"managed entry replacement won final publication: {name}"
                ) from exc
            except Exception:
                if _entry_stat(capability, name) is None:
                    os.rename(
                        quarantine,
                        name,
                        src_dir_fd=capability.fd,
                        dst_dir_fd=capability.fd,
                    )
                else:
                    quarantined = _entry_stat(capability, quarantine)
                    if quarantined is None or (
                        quarantined.st_dev,
                        quarantined.st_ino,
                    ) != expected_identity:
                        raise PendingTransactionError(
                            f"managed entry quarantine identity changed: {name}"
                        )
                    os.unlink(quarantine, dir_fd=capability.fd)
                os.fsync(capability.fd)
                capability.validate()
                raise
            os.unlink(quarantine, dir_fd=capability.fd)
        else:
            os.replace(temporary, name, src_dir_fd=capability.fd, dst_dir_fd=capability.fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=capability.fd)
    os.fsync(capability.fd)
    capability.validate()


def _create_bytes(capability: OutputCapability, name: str, payload: bytes, mode: int = 0o600) -> tuple[int, int]:
    capability.validate()
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, mode, dir_fd=capability.fd)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("exclusive write made no progress")
            view = view[count:]
        os.fsync(fd)
        info = os.fstat(fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=capability.fd)
        raise
    finally:
        os.close(fd)
    try:
        os.link(
            temporary,
            name,
            src_dir_fd=capability.fd,
            dst_dir_fd=capability.fd,
            follow_symlinks=False,
        )
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=capability.fd)
        raise
    os.unlink(temporary, dir_fd=capability.fd)
    os.fsync(capability.fd)
    capability.validate()
    return info.st_dev, info.st_ino


def _replace_relative_bytes(
    capability: OutputCapability, relative_name: str, payload: bytes
) -> None:
    relative = Path(relative_name)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {relative_name}")
    parent_fd = os.dup(capability.fd)
    nested: OutputCapability | None = None
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        nested = OutputCapability(
            capability.path / Path(*relative.parts[:-1]),
            OutputIdentity(os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino),
            parent_fd,
        )
        parent_fd = -1
        _replace_bytes(nested, relative.parts[-1], payload)
    finally:
        if nested is not None:
            nested.close()
        if parent_fd >= 0:
            os.close(parent_fd)


def _unlink(capability: OutputCapability, name: str, *, expected: tuple[int, int] | None = None) -> None:
    info = _entry_stat(capability, name)
    if info is None:
        return
    if expected is not None and (info.st_dev, info.st_ino) != expected:
        raise PendingTransactionError(f"managed entry identity changed: {name}")
    os.unlink(name, dir_fd=capability.fd)
    os.fsync(capability.fd)
    capability.validate()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _load_json(capability: OutputCapability, name: str) -> dict[str, Any] | None:
    if _entry_stat(capability, name) is None:
        return None
    try:
        value = json.loads(_read_bytes(capability, name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError(f"malformed managed state: {name}") from exc
    if not isinstance(value, dict):
        raise PendingTransactionError(f"malformed managed state: {name}")
    return value


_PROTOCOL_COMMON_FIELDS = {
    "schema",
    "protocol_epoch",
    "generation",
    "kind",
    "root",
    "state",
    "output_identity",
    "owner_capability_digest",
    "bootstrap_claim_epoch",
    "bootstrap_nonce",
    "lease_deadline",
}


def _protocol_from_json(
    capability: OutputCapability, raw: object
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PendingTransactionError("malformed protocol state")
    state = raw.get("state")
    fields = set(_PROTOCOL_COMMON_FIELDS)
    if state == "INCOMPLETE":
        fields.update(("transaction_id", "token_identity"))
    elif state == "COMPLETE":
        fields.update(("transaction_id", "token_identity", "receipt_digest"))
    elif state != "BOOTSTRAP_PENDING":
        raise PendingTransactionError("malformed protocol state")
    if set(raw) != fields:
        raise PendingTransactionError("malformed protocol state")
    generation = raw.get("generation")
    root_value = raw.get("root")
    lease_deadline = raw.get("lease_deadline")
    if (
        type(raw.get("schema")) is not int
        or raw["schema"] != 1
        or type(raw.get("protocol_epoch")) is not int
        or raw["protocol_epoch"] != 1
        or type(generation) is not int
        or generation < 1
        or raw.get("kind") not in {"full", "update", "runtime"}
        or type(root_value) is not str
        or not _is_hex(raw.get("owner_capability_digest"))
        or type(raw.get("bootstrap_claim_epoch")) is not int
        or raw["bootstrap_claim_epoch"] < 0
        or type(raw.get("bootstrap_nonce")) is not str
        or len(raw["bootstrap_nonce"]) < 16
        or type(lease_deadline) not in {int, float}
        or _identity_from_json(raw.get("output_identity")) != capability.identity
    ):
        raise PendingTransactionError("malformed protocol state")
    if not math.isfinite(float(cast(int | float, lease_deadline))):
        raise PendingTransactionError("malformed protocol state")
    root = Path(root_value)
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise PendingTransactionError("malformed protocol root") from exc
    if not root.is_absolute() or canonical_root != root:
        raise PendingTransactionError("malformed protocol root")
    if state in {"INCOMPLETE", "COMPLETE"}:
        if not _is_hex(raw.get("transaction_id")):
            raise PendingTransactionError("malformed protocol owner")
        try:
            _token_identity_from_json(raw.get("token_identity"))
        except PendingTransactionError as exc:
            raise PendingTransactionError(
                "malformed protocol token identity"
            ) from exc
    if state == "COMPLETE" and not _is_hex(raw.get("receipt_digest")):
        raise PendingTransactionError("malformed protocol receipt")
    return dict(raw)


def _read_protocol(capability: OutputCapability) -> dict[str, Any] | None:
    raw = _load_json(capability, PROTOCOL_FILE)
    return None if raw is None else _protocol_from_json(capability, raw)


def _identity_from_json(value: object) -> OutputIdentity:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise PendingTransactionError("malformed output identity")
    if type(value["device"]) is not int or type(value["inode"]) is not int:
        raise PendingTransactionError("malformed output identity")
    if value["device"] < 0 or value["inode"] <= 0:
        raise PendingTransactionError("malformed output identity")
    return OutputIdentity(value["device"], value["inode"])


def _token_identity_from_json(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    identity = _identity_from_json(value)
    return identity.device, identity.inode


def _is_hex(value: object, length: int = 64) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _drainer_from_json(value: object) -> DrainerTuple:
    if not isinstance(value, dict):
        raise PendingTransactionError("malformed drainer authority")
    generation = value.get("generation")
    claim_epoch = value.get("claim_epoch")
    launch_nonce = value.get("launch_nonce")
    if (
        type(generation) is not int
        or type(claim_epoch) is not int
        or type(launch_nonce) is not str
    ):
        raise PendingTransactionError("malformed drainer authority")
    result = DrainerTuple(generation, claim_epoch, launch_nonce)
    if result.generation < 1 or result.claim_epoch < 0 or len(result.launch_nonce) < 16:
        raise PendingTransactionError("malformed drainer authority")
    return result


def _drainer_json(value: DrainerTuple) -> dict[str, object]:
    return {
        "generation": value.generation,
        "claim_epoch": value.claim_epoch,
        "launch_nonce": value.launch_nonce,
    }


_TRANSACTION_FIELDS = {
    "schema",
    "protocol_epoch",
    "id",
    "kind",
    "root",
    "output",
    "phase",
    "pid",
    "generation",
    "output_identity",
    "token_digest",
    "token_identity",
    "drainer",
}
_TRANSACTION_PHASES = {"awaiting-drainer", "building", "bootstrap-recovered"}


def _transaction_from_json(
    capability: OutputCapability,
    raw: object,
    *,
    allowed_phases: set[str] = _TRANSACTION_PHASES,
) -> Transaction:
    if not isinstance(raw, dict) or set(raw) != _TRANSACTION_FIELDS:
        raise PendingTransactionError("malformed live transaction")
    if (
        type(raw.get("schema")) is not int
        or raw.get("schema") != 1
        or type(raw.get("protocol_epoch")) is not int
        or raw.get("protocol_epoch") != 1
        or not _is_hex(raw.get("id"))
        or raw.get("kind") not in {"full", "update", "runtime"}
        or type(raw.get("root")) is not str
        or type(raw.get("output")) is not str
        or raw.get("phase") not in allowed_phases
        or type(raw.get("pid")) is not int
        or raw["pid"] <= 0
        or type(raw.get("generation")) is not int
        or raw["generation"] < 1
        or not _is_hex(raw.get("token_digest"))
    ):
        raise PendingTransactionError("malformed live transaction")
    root = Path(raw["root"])
    output = Path(raw["output"])
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise PendingTransactionError("malformed live transaction root") from exc
    if (
        not root.is_absolute()
        or canonical_root != root
        or not output.is_absolute()
        or output != capability.path
    ):
        raise PendingTransactionError("malformed live transaction path binding")
    tx = Transaction(
        id=raw["id"],
        kind=raw["kind"],
        root=raw["root"],
        output=output,
        output_identity=_identity_from_json(raw["output_identity"]),
        generation=raw["generation"],
        token_digest=raw["token_digest"],
        token_identity=_token_identity_from_json(raw["token_identity"]),
        drainer=_drainer_from_json(raw["drainer"]),
        phase=raw["phase"],
    )
    if tx.output_identity != capability.identity or tx.drainer.generation != tx.generation:
        raise PendingTransactionError("malformed live transaction binding")
    return tx


def _read_transaction(capability: OutputCapability) -> Transaction | None:
    raw = _load_json(capability, TRANSACTION_FILE)
    return None if raw is None else _transaction_from_json(capability, raw)


def _transaction_json(tx: Transaction, *, phase: str) -> dict[str, object]:
    return {
        "schema": 1,
        "protocol_epoch": 1,
        "id": tx.id,
        "kind": tx.kind,
        "root": tx.root,
        "output": str(tx.output),
        "phase": phase,
        "pid": os.getpid(),
        "generation": tx.generation,
        "output_identity": tx.output_identity.json(),
        "token_digest": tx.token_digest,
        "token_identity": (
            None
            if tx.token_identity is None
            else {"device": tx.token_identity[0], "inode": tx.token_identity[1]}
        ),
        "drainer": _drainer_json(tx.drainer),
    }


def _write_transaction(capability: OutputCapability, tx: Transaction, *, phase: str = "building") -> None:
    _replace_bytes(capability, TRANSACTION_FILE, _json_bytes(_transaction_json(tx, phase=phase)))


def _write_pending_transition(
    capability: OutputCapability,
    *,
    predecessor_drainer: tuple[DrainerTuple, str] | None,
    predecessor_protocol: Mapping[str, object],
    predecessor_transaction: Transaction | None,
    successor: Transaction,
    successor_protocol: Mapping[str, object],
) -> None:
    if predecessor_drainer is not None and predecessor_drainer[1] not in {
        "complete",
        "reserved",
        "launching",
        "claimed",
    }:
        raise PendingTransactionError("unsupported pending transition predecessor")
    record = {
        "schema": 1,
        "protocol_epoch": 1,
        "state": "pending",
        "output_identity": capability.identity.json(),
        "predecessor_drainer": (
            None
            if predecessor_drainer is None
            else {
                "tuple": _drainer_json(predecessor_drainer[0]),
                "state": predecessor_drainer[1],
            }
        ),
        "predecessor_protocol": dict(predecessor_protocol),
        "predecessor_transaction": (
            None
            if predecessor_transaction is None
            else _transaction_json(predecessor_transaction, phase="building")
        ),
        "successor_transaction": _transaction_json(successor, phase="awaiting-drainer"),
        "successor_protocol": dict(successor_protocol),
    }
    _replace_bytes(capability, TRANSITION_FILE, _json_bytes(record))


def _transaction_from_record(
    capability: OutputCapability,
    value: object,
    *,
    allowed_phases: set[str] | None = None,
) -> Transaction:
    try:
        return _transaction_from_json(
            capability,
            value,
            allowed_phases=allowed_phases or {"awaiting-drainer"},
        )
    except PendingTransactionError as exc:
        raise PendingTransactionError("malformed pending transition") from exc


def _read_pending_transition(
    capability: OutputCapability,
) -> tuple[tuple[DrainerTuple, str] | None, dict[str, Any], Transaction | None, Transaction, dict[str, Any]] | None:
    record = _load_json(capability, TRANSITION_FILE)
    if record is None:
        return None
    if (
        record.get("schema") != 1
        or record.get("protocol_epoch") != 1
        or record.get("state") != "pending"
        or _identity_from_json(record.get("output_identity")) != capability.identity
        or not isinstance(record.get("predecessor_protocol"), dict)
        or not isinstance(record.get("successor_protocol"), dict)
    ):
        raise PendingTransactionError("malformed pending transition")
    predecessor_raw = record.get("predecessor_drainer")
    predecessor = None
    if predecessor_raw is not None:
        if not isinstance(predecessor_raw, dict) or predecessor_raw.get("state") not in {
            "complete",
            "reserved",
            "launching",
            "claimed",
        }:
            raise PendingTransactionError("malformed pending transition")
        predecessor = (
            _drainer_from_json(predecessor_raw.get("tuple")),
            str(predecessor_raw["state"]),
        )
    prior_tx_raw = record.get("predecessor_transaction")
    prior_tx = None
    if prior_tx_raw is not None:
        prior_tx = _transaction_from_record(
            capability,
            prior_tx_raw,
            allowed_phases={"building", "bootstrap-recovered"},
        )
    successor = _transaction_from_record(capability, record.get("successor_transaction"))
    try:
        predecessor_protocol = _protocol_from_json(
            capability, record["predecessor_protocol"]
        )
    except PendingTransactionError as exc:
        raise PendingTransactionError(
            "pending transition protocol predecessor is malformed"
        ) from exc
    try:
        successor_protocol = _protocol_from_json(
            capability, record["successor_protocol"]
        )
    except PendingTransactionError as exc:
        raise PendingTransactionError(
            "pending transition protocol successor is malformed"
        ) from exc
    if (
        successor_protocol.get("schema") != 1
        or successor_protocol.get("protocol_epoch") != 1
        or successor_protocol.get("state") != "INCOMPLETE"
        or successor_protocol.get("generation") != successor.generation
        or successor_protocol.get("transaction_id") != successor.id
        or successor_protocol.get("root") != successor.root
        or successor_protocol.get("kind") != successor.kind
        or successor_protocol.get("owner_capability_digest") != successor.token_digest
        or successor_protocol.get("output_identity") != capability.identity.json()
        or successor_protocol.get("token_identity")
        != (
            None
            if successor.token_identity is None
            else {"device": successor.token_identity[0], "inode": successor.token_identity[1]}
        )
    ):
        raise PendingTransactionError("malformed pending transition successor binding")
    if prior_tx is not None:
        prior_token_identity = (
            None
            if prior_tx.token_identity is None
            else {
                "device": prior_tx.token_identity[0],
                "inode": prior_tx.token_identity[1],
            }
        )
        if (
            predecessor is None
            or predecessor[0] != prior_tx.drainer
            or predecessor_protocol.get("state") != "INCOMPLETE"
            or predecessor_protocol.get("generation") != prior_tx.generation
            or predecessor_protocol.get("transaction_id") != prior_tx.id
            or predecessor_protocol.get("root") != prior_tx.root
            or predecessor_protocol.get("kind") != prior_tx.kind
            or predecessor_protocol.get("owner_capability_digest")
            != prior_tx.token_digest
            or predecessor_protocol.get("token_identity") != prior_token_identity
            or predecessor_protocol.get("output_identity")
            != capability.identity.json()
        ):
            raise PendingTransactionError(
                "malformed pending transition predecessor binding"
            )
    if successor.token_identity is not None:
        token = _entry_stat(capability, f".graphify_transaction_token.{successor.id}")
        if token is None or (token.st_dev, token.st_ino) != successor.token_identity:
            raise PendingTransactionError("pending successor token identity changed")
        if hashlib.sha256(_read_bytes(capability, f".graphify_transaction_token.{successor.id}")).hexdigest() != successor.token_digest:
            raise PendingTransactionError("pending successor token digest changed")
    return predecessor, predecessor_protocol, prior_tx, successor, successor_protocol


def _validate_pending_transition_current(
    capability: OutputCapability,
    pending: tuple[
        tuple[DrainerTuple, str] | None,
        dict[str, Any],
        Transaction | None,
        Transaction,
        dict[str, Any],
    ],
) -> None:
    """Prove the current state is one exact point on the recorded transition."""
    predecessor, predecessor_protocol, predecessor_tx, successor, successor_protocol = pending
    protocol = _read_protocol(capability)
    protocol_is_predecessor = protocol == predecessor_protocol
    protocol_is_successor = protocol == successor_protocol
    if not protocol_is_predecessor and not protocol_is_successor:
        raise PendingTransactionError("pending transition protocol predecessor changed")
    live = _read_transaction(capability)
    if predecessor_tx is None:
        if live is not None and live != successor:
            raise PendingTransactionError("pending transition transaction predecessor changed")
    elif live != predecessor_tx and live != successor:
        raise PendingTransactionError("pending transition transaction predecessor changed")
    current_drainer = _read_drainer(capability)
    if current_drainer is None:
        if predecessor is not None:
            raise PendingTransactionError("pending transition drainer predecessor disappeared")
        if protocol_is_predecessor and live is not None:
            raise PendingTransactionError("pending transition ordering changed")
        return
    current_pair = current_drainer[:2]
    if current_pair == predecessor:
        if protocol_is_predecessor and live != predecessor_tx:
            raise PendingTransactionError("pending transition ordering changed")
        return
    if current_drainer[0] == successor.drainer and current_drainer[1] in {
        "reserved",
        "launching",
        "claimed",
    }:
        if not protocol_is_successor or live != successor:
            raise PendingTransactionError("pending transition ordering changed")
        return
    raise PendingTransactionError("pending transition drainer binding changed")


def _transaction_phase(capability: OutputCapability) -> str | None:
    raw = _load_json(capability, TRANSACTION_FILE)
    if raw is None:
        return None
    phase = raw.get("phase")
    if phase not in {"awaiting-drainer", "building", "bootstrap-recovered"}:
        raise PendingTransactionError("malformed live transaction phase")
    return str(phase)


def _authority_for(tx: Transaction, drainer: DrainerTuple | None = None) -> _Authority:
    return _Authority(
        tx.id,
        tx.generation,
        tx.token_digest,
        tx.token_identity,
        tx.output_identity,
        drainer or tx.drainer,
        tx.root,
        tx.kind,
        tx.phase,
    )


def _cancellation_recovery(successor: Transaction) -> CancellationRecovery:
    return CancellationRecovery(
        id=successor.id,
        kind=successor.kind,
        root=successor.root,
        output=successor.output,
        output_identity=successor.output_identity,
        generation=successor.generation,
        token_digest=successor.token_digest,
        token_identity=successor.token_identity,
        drainer=successor.drainer,
        phase=successor.phase,
        predecessor_generation=successor.generation - 1,
    )


def _retire_prepared_locked(capability: OutputCapability) -> Path | None:
    """Identity-retire a prepared workspace without path-based recursive deletion."""
    marker = _load_json(capability, PREPARED_FILE)
    if marker is None:
        return None
    transaction_id = marker.get("transaction_id")
    expected = _identity_from_json(marker.get("identity"))
    expected_output = _identity_from_json(marker.get("output_identity"))
    if not isinstance(transaction_id, str) or expected is None or expected_output is None:
        raise PendingTransactionError("prepared workspace binding is malformed")
    workspace = capability.path.parent / f".graphify-prepare-{transaction_id}"
    with pin_output(workspace.parent) as parent_capability:
        try:
            info = os.stat(
                workspace.name,
                dir_fd=parent_capability.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _unlink(capability, PREPARED_FILE)
            return None
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
            expected.device,
            expected.inode,
        ):
            raise PendingTransactionError("prepared workspace identity changed")
        with pin_output(workspace / "graphify-out") as output_capability:
            if output_capability.identity != expected_output:
                raise PendingTransactionError("prepared output identity changed")
        tombstone = f".graphify-retired-{transaction_id}-{secrets.token_hex(8)}"
        retirement_marker = {
            "schema": 1,
            "protocol_epoch": 1,
            "state": "retired",
            "transaction_id": transaction_id,
            "workspace_identity": expected.json(),
            "output_identity": expected_output.json(),
            "managed_output_identity": capability.identity.json(),
            "tombstone": tombstone,
            "current_name": tombstone,
            "quarantine_name": None,
        }
        with pin_output(workspace) as workspace_capability:
            if workspace_capability.identity != expected:
                raise PendingTransactionError("prepared workspace identity changed")
            _replace_bytes(
                workspace_capability,
                ".graphify_retired.json",
                _json_bytes(retirement_marker),
            )
        os.rename(
            workspace.name,
            tombstone,
            src_dir_fd=parent_capability.fd,
            dst_dir_fd=parent_capability.fd,
        )
        retired_info = os.stat(
            tombstone, dir_fd=parent_capability.fd, follow_symlinks=False
        )
        if (retired_info.st_dev, retired_info.st_ino) != (
            expected.device,
            expected.inode,
        ):
            try:
                os.stat(
                    workspace.name,
                    dir_fd=parent_capability.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.rename(
                    tombstone,
                    workspace.name,
                    src_dir_fd=parent_capability.fd,
                    dst_dir_fd=parent_capability.fd,
                )
            raise PendingTransactionError(
                "prepared workspace changed during identity retirement"
            )
        os.fsync(parent_capability.fd)
    _unlink(capability, PREPARED_FILE)
    return workspace.parent / tombstone


def _validated_retired_marker(
    marker: dict[str, Any] | None,
    *,
    candidate_name: str,
    candidate_identity: OutputIdentity,
    managed_output_identity: OutputIdentity,
) -> dict[str, Any] | None:
    """Return a strict marker for this output, ignoring only proven siblings."""
    if marker is None:
        return None
    raw_managed = marker.get("managed_output_identity")
    if raw_managed != managed_output_identity.json():
        return None
    state = marker.get("state")
    transaction_id = marker.get("transaction_id")
    tombstone = marker.get("tombstone")
    current_name = marker.get("current_name", tombstone)
    quarantine_name = marker.get("quarantine_name")
    if (
        not _is_retired_tombstone(tombstone, transaction_id)
        or not _is_single_component(current_name)
        or (
            quarantine_name is not None
            and not _is_gc_quarantine(quarantine_name)
        )
        or not _is_single_component(candidate_name)
    ):
        raise PendingTransactionError("retired workspace binding is malformed")
    allowed_names = {current_name}
    if state == "gc_pending" and isinstance(quarantine_name, str):
        allowed_names.add(quarantine_name)
    if (
        marker.get("schema") != 1
        or marker.get("protocol_epoch") != 1
        or state not in {"retired", "gc_pending", "gc_quarantined"}
        or not isinstance(transaction_id, str)
        or len(transaction_id) != 64
        or not isinstance(tombstone, str)
        or not isinstance(current_name, str)
        or candidate_name not in allowed_names
        or _identity_from_json(marker.get("workspace_identity")) != candidate_identity
        or not isinstance(_identity_from_json(marker.get("output_identity")), OutputIdentity)
    ):
        raise PendingTransactionError("retired workspace binding is malformed")
    if state == "retired" and (current_name != tombstone or quarantine_name is not None):
        raise PendingTransactionError("retired workspace binding is malformed")
    if state in {"gc_pending", "gc_quarantined"} and (
        not isinstance(quarantine_name, str)
        or not _is_gc_quarantine(quarantine_name)
    ):
        raise PendingTransactionError("retired workspace binding is malformed")
    if state == "gc_quarantined" and current_name != quarantine_name:
        raise PendingTransactionError("retired workspace binding is malformed")
    return marker


def _is_single_component(value: object) -> bool:
    return type(value) is str and Path(value).name == value and value not in {"", ".", ".."}


def _is_retired_tombstone(value: object, transaction_id: object = None) -> bool:
    if type(value) is not str or not _is_single_component(value):
        return False
    match = re.fullmatch(r"\.graphify-retired-([0-9a-f]{64})-([0-9a-f]{16})", value)
    return match is not None and (
        transaction_id is None or match.group(1) == transaction_id
    )


def _is_gc_quarantine(value: object) -> bool:
    return type(value) is str and _is_single_component(value) and re.fullmatch(
        r"\.graphify-gc-root-[0-9a-f]{32}", value
    ) is not None


def _is_gc_journal(value: object) -> bool:
    return type(value) is str and _is_single_component(value) and re.fullmatch(
        r"\.graphify-gc-journal-[0-9a-f]{64}\.json", value
    ) is not None


def _gc_journal_name(
    managed_output_identity: OutputIdentity, tombstone: str
) -> str:
    selector = _json_bytes(
        {
            "managed_output_identity": managed_output_identity.json(),
            "tombstone": tombstone,
        }
    )
    return f".graphify-gc-journal-{hashlib.sha256(selector).hexdigest()}.json"


def _validated_gc_journal(
    raw: dict[str, Any] | None,
    *,
    tombstone: str,
    workspace_identity: OutputIdentity,
    managed_output_identity: OutputIdentity,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if raw.get("managed_output_identity") != managed_output_identity.json():
        return None
    quarantine_name = raw.get("quarantine_name")
    if (
        set(raw)
        != {
            "schema",
            "protocol_epoch",
            "state",
            "tombstone",
            "quarantine_name",
            "workspace_identity",
            "managed_output_identity",
        }
        or type(raw.get("schema")) is not int
        or raw.get("schema") != 1
        or type(raw.get("protocol_epoch")) is not int
        or raw.get("protocol_epoch") != 1
        or raw.get("state") not in {"planned", "quarantined", "root_removed"}
        or raw.get("tombstone") != tombstone
        or not _is_retired_tombstone(tombstone)
        or not _is_gc_quarantine(quarantine_name)
        or _identity_from_json(raw.get("workspace_identity"))
        != workspace_identity
    ):
        raise PendingTransactionError("retired workspace GC journal is malformed")
    return raw


def _gc_journal_location(
    parent: OutputCapability,
    journal: Mapping[str, object],
    workspace_identity: OutputIdentity,
) -> str | None:
    tombstone = journal["tombstone"]
    quarantine = journal["quarantine_name"]
    if not _is_retired_tombstone(tombstone) or not _is_gc_quarantine(quarantine):
        raise PendingTransactionError("retired workspace GC journal is malformed")
    matches: list[str] = []
    controlled = [
        name
        for name in _list_entries(parent)
        if _is_retired_tombstone(name) or _is_gc_quarantine(name)
    ]
    if len(controlled) > _MAX_RECEIPT_ARTIFACTS:
        raise PendingTransactionError("retired workspace GC inventory exceeds bound")
    for name in controlled:
        try:
            info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        identity = OutputIdentity(info.st_dev, info.st_ino)
        if name in {tombstone, quarantine} and (
            not stat.S_ISDIR(info.st_mode) or identity != workspace_identity
        ):
            raise PendingTransactionError("retired workspace GC location changed")
        if stat.S_ISDIR(info.st_mode) and identity == workspace_identity:
            matches.append(name)
    state = journal["state"]
    if state == "root_removed":
        if matches:
            raise PendingTransactionError("retired workspace GC location is ambiguous")
        return None
    if state == "quarantined" and not matches:
        # The root removal and its following parent fsync are separate durable
        # boundaries.  The parent-scoped journal is the authority that makes
        # this exact, identity-bound absence resumable.
        return None
    if len(matches) != 1:
        raise PendingTransactionError("retired workspace GC location is ambiguous")
    if matches[0] not in {tombstone, quarantine}:
        raise PendingTransactionError("retired workspace GC location changed")
    if state == "quarantined" and matches[0] != quarantine:
        raise PendingTransactionError("retired workspace GC location is stale")
    return matches[0]


def _remove_retired_tree(
    parent_fd: int,
    name: str,
    expected: OutputIdentity,
    *,
    failpoint: Callable[[str], None] | None = None,
    selected_root: bool = True,
) -> None:
    """Remove one identity-proven retired workspace without following links."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (expected.device, expected.inode):
            raise PendingTransactionError("retired workspace identity changed")
        for child in os.listdir(directory_fd):
            info = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
            child_identity = OutputIdentity(info.st_dev, info.st_ino)
            quarantine = f".graphify-gc-{secrets.token_hex(16)}"
            os.rename(child, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            if failpoint is not None:
                failpoint("after_gc_child_quarantine")
            moved = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != (
                child_identity.device,
                child_identity.inode,
            ):
                with contextlib.suppress(FileExistsError):
                    os.rename(
                        quarantine,
                        child,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                raise PendingTransactionError("retired workspace child identity changed")
            if stat.S_ISDIR(info.st_mode):
                _remove_retired_tree(
                    directory_fd,
                    quarantine,
                    child_identity,
                    failpoint=failpoint,
                    selected_root=False,
                )
            elif stat.S_ISREG(info.st_mode):
                terminal = os.stat(
                    quarantine, dir_fd=directory_fd, follow_symlinks=False
                )
                if (terminal.st_dev, terminal.st_ino) != (
                    child_identity.device,
                    child_identity.inode,
                ):
                    raise PendingTransactionError(
                        "retired workspace child identity changed"
                    )
                os.unlink(quarantine, dir_fd=directory_fd)
                if failpoint is not None:
                    failpoint(
                        "after_gc_marker_retirement"
                        if child == ".graphify_retired.json"
                        else "after_gc_child_unlink"
                    )
            else:
                raise PendingTransactionError(
                    "retired workspace contains an unsafe filesystem entry"
                )
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    terminal = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (terminal.st_dev, terminal.st_ino) != (expected.device, expected.inode):
        raise PendingTransactionError("retired workspace identity changed")
    os.rmdir(name, dir_fd=parent_fd)
    if failpoint is not None and selected_root:
        failpoint("after_gc_root_removal")
    os.fsync(parent_fd)


def gc_retired_workspaces(
    output: Path | str,
    *,
    expected_output_identity: OutputIdentity,
    workspace: Path | str,
    expected_workspace_identity: OutputIdentity,
    dry_run: bool = True,
    failpoint: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    """Inspect or delete one exact unreachable, identity-bound retired workspace."""
    with pin_output(output, mutation=not dry_run) as capability, _locked(capability):
        if capability.identity != expected_output_identity:
            raise PendingTransactionError("stale output identity selector")
        prepared = _load_json(capability, PREPARED_FILE)
        reachable_identity = (
            None if prepared is None else _identity_from_json(prepared.get("identity"))
        )
        selected = Path(workspace).expanduser().absolute()
        if selected.parent != capability.path.parent or not _is_retired_tombstone(selected.name):
            raise PendingTransactionError("retired workspace selector is outside output scope")
        with pin_output(capability.path.parent, mutation=not dry_run) as parent:
            selected_name = selected.name
            journal_name = _gc_journal_name(capability.identity, selected.name)
            journal = _validated_gc_journal(
                _load_json(parent, journal_name),
                tombstone=selected.name,
                workspace_identity=expected_workspace_identity,
                managed_output_identity=capability.identity,
            )
            if journal is not None:
                _gc_journal_location(parent, journal, expected_workspace_identity)
            if journal is not None and journal.get("state") == "root_removed":
                if not dry_run:
                    _unlink(parent, journal_name)
                return (selected.name,)
            if journal is not None and journal.get("state") == "quarantined":
                quarantine_name = str(journal["quarantine_name"])
                try:
                    os.stat(
                        quarantine_name,
                        dir_fd=parent.fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not dry_run:
                        journal = dict(journal)
                        journal["state"] = "root_removed"
                        _replace_bytes(parent, journal_name, _json_bytes(journal))
                        if failpoint is not None:
                            failpoint("after_gc_parent_fsync")
                        _unlink(parent, journal_name)
                    return (selected.name,)
            try:
                info = os.stat(selected_name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                matches: list[tuple[str, os.stat_result, dict[str, Any]]] = []
                for name in sorted(_list_entries(parent)):
                    if not name.startswith(".graphify-gc-root-"):
                        continue
                    candidate_info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                    if not stat.S_ISDIR(candidate_info.st_mode):
                        continue
                    candidate_identity = OutputIdentity(candidate_info.st_dev, candidate_info.st_ino)
                    if candidate_identity != expected_workspace_identity:
                        continue
                    with pin_output(parent.path / name, mutation=False) as retired:
                        marker = _load_json(retired, ".graphify_retired.json")
                    bound = (
                        journal
                        if journal is not None
                        and journal.get("state") == "quarantined"
                        and journal.get("quarantine_name") == name
                        else _validated_retired_marker(
                            marker,
                            candidate_name=name,
                            candidate_identity=candidate_identity,
                            managed_output_identity=capability.identity,
                        )
                    )
                    if bound is not None and bound.get("tombstone") == selected.name:
                        matches.append((name, candidate_info, bound))
                if len(matches) != 1:
                    raise PendingTransactionError("retired workspace is unsafe, stale, or unreachable")
                selected_name, info, marker = matches[0]
            identity = OutputIdentity(info.st_dev, info.st_ino)
            if (
                not stat.S_ISDIR(info.st_mode)
                or identity != expected_workspace_identity
                or identity == reachable_identity
            ):
                raise PendingTransactionError("retired workspace is unsafe, stale, or reachable")
            with pin_output(parent.path / selected_name, mutation=False) as retired:
                marker = _load_json(retired, ".graphify_retired.json")
            if (
                journal is not None
                and journal.get("state") == "quarantined"
                and journal.get("quarantine_name") == selected_name
            ):
                marker = {
                    "state": "gc_quarantined",
                    "tombstone": selected.name,
                    "quarantine_name": selected_name,
                }
            else:
                marker = _validated_retired_marker(
                    marker,
                    candidate_name=selected_name,
                    candidate_identity=identity,
                    managed_output_identity=capability.identity,
                )
            if marker is None or marker.get("tombstone") != selected.name:
                raise PendingTransactionError("retired workspace binding is malformed")
            if not dry_run:
                if marker.get("state") == "retired":
                    quarantine = f".graphify-gc-root-{secrets.token_hex(16)}"
                    journal = {
                        "schema": 1,
                        "protocol_epoch": 1,
                        "state": "planned",
                        "tombstone": selected.name,
                        "quarantine_name": quarantine,
                        "workspace_identity": identity.json(),
                        "managed_output_identity": capability.identity.json(),
                    }
                    _replace_bytes(parent, journal_name, _json_bytes(journal))
                    pending_marker = dict(marker)
                    pending_marker.update(
                        state="gc_pending",
                        current_name=selected_name,
                        quarantine_name=quarantine,
                    )
                    with pin_output(parent.path / selected_name) as retired:
                        _replace_bytes(retired, ".graphify_retired.json", _json_bytes(pending_marker))
                    os.fsync(parent.fd)
                    os.rename(
                        selected_name,
                        quarantine,
                        src_dir_fd=parent.fd,
                        dst_dir_fd=parent.fd,
                    )
                else:
                    quarantine = str(marker["quarantine_name"])
                    if journal is None:
                        journal = {
                            "schema": 1,
                            "protocol_epoch": 1,
                            "state": "planned",
                            "tombstone": selected.name,
                            "quarantine_name": quarantine,
                            "workspace_identity": identity.json(),
                            "managed_output_identity": capability.identity.json(),
                        }
                        _replace_bytes(parent, journal_name, _json_bytes(journal))
                    if selected_name != quarantine:
                        os.rename(
                            selected_name,
                            quarantine,
                            src_dir_fd=parent.fd,
                            dst_dir_fd=parent.fd,
                        )
                moved = os.stat(quarantine, dir_fd=parent.fd, follow_symlinks=False)
                if (moved.st_dev, moved.st_ino) != (identity.device, identity.inode):
                    with contextlib.suppress(FileExistsError):
                        os.rename(
                            quarantine,
                            selected_name,
                            src_dir_fd=parent.fd,
                            dst_dir_fd=parent.fd,
                        )
                    raise PendingTransactionError("retired workspace identity changed")
                with pin_output(parent.path / quarantine) as retired:
                    quarantined_marker = dict(marker)
                    quarantined_marker.update(
                        state="gc_quarantined",
                        current_name=quarantine,
                        quarantine_name=quarantine,
                    )
                    _replace_bytes(
                        retired,
                        ".graphify_retired.json",
                        _json_bytes(quarantined_marker),
                    )
                os.fsync(parent.fd)
                journal = dict(journal)
                journal["state"] = "quarantined"
                _replace_bytes(parent, journal_name, _json_bytes(journal))
                if failpoint is not None:
                    failpoint("after_gc_quarantine")
                _remove_retired_tree(
                    parent.fd,
                    quarantine,
                    identity,
                    failpoint=failpoint,
                )
                journal["state"] = "root_removed"
                _replace_bytes(parent, journal_name, _json_bytes(journal))
                if failpoint is not None:
                    failpoint("after_gc_parent_fsync")
                _unlink(parent, journal_name)
            return (selected.name,)


def begin_transaction(
    kind: TransactionKind,
    root: Path | str,
    *,
    output: Path | str = "graphify-out",
    now: float | None = None,
    failpoint: Callable[[OutputCapability, dict[str, object]], None] | None = None,
    transition_failpoint: Callable[[str], None] | None = None,
) -> Transaction:
    root_path = _canonical_directory(Path(root))
    capability = pin_output(output, create=True)
    try:
        with _locked(capability):
            if _entry_stat(capability, TRANSACTION_FILE) is not None:
                raise PendingTransactionError("graph state already has bootstrap or live ownership")
            prior_protocol = _read_protocol(capability)
            receipt_present = _entry_stat(capability, RECEIPT_FILE) is not None
            current_drainer = _read_drainer(capability)
            if prior_protocol is None and not receipt_present:
                allowed = list(_SAFE_GRAPHLESS_RUNTIME_ENTRIES)
                if current_drainer is not None:
                    queued = _read_queue(capability)
                    matching = [
                        item for item in queued if item.get("root") == str(root_path)
                    ]
                    if (
                        current_drainer[1] != "reserved"
                        or current_drainer[0].generation != 1
                        or current_drainer[2].get("predecessor_receipt") is not None
                        or not matching
                    ):
                        raise PendingTransactionError(
                            "partial predecessor authority cannot bootstrap"
                        )
                    allowed.extend((DRAINER_FILE, QUEUE_FILE))
                    if _entry_stat(capability, LEGACY_PENDING_STATE_FILE) is not None:
                        legacy_name = _validated_legacy_pending_bridge(capability)
                        allowed.extend((legacy_name, LEGACY_PENDING_STATE_FILE))
                _validate_pristine_or_legacy_graph(
                    capability,
                    allowed_without_graph=allowed,
                )
            predecessor_receipt: str | None = None
            if prior_protocol is None:
                if receipt_present:
                    raise PendingTransactionError(
                        "partial predecessor authority cannot bootstrap"
                    )
                if current_drainer is None:
                    generation = 1
                else:
                    generation = 1
            else:
                if prior_protocol.get("state") != "COMPLETE":
                    raise PendingTransactionError(
                        "graph state already has bootstrap or live ownership"
                    )
                if current_drainer is None:
                    receipt, predecessor_receipt, _inventory = _validate_receipt_locked(
                        capability,
                        allow_missing_completed_drainer=True,
                    )
                elif current_drainer[1] == "complete":
                    receipt, predecessor_receipt, _inventory = _validate_receipt_locked(
                        capability,
                        require_closed=True,
                    )
                else:
                    receipt, predecessor_receipt, _inventory = _validate_receipt_locked(
                        capability
                    )
                generation = int(receipt["generation"]) + 1
                if current_drainer is not None and current_drainer[1] == "complete":
                    if predecessor_receipt is None:
                        raise PendingTransactionError(
                            "completed predecessor receipt binding is missing"
                        )
                    _write_predecessor_authority(
                        capability,
                        prior_protocol,
                        current_drainer,
                        predecessor_receipt,
                    )
                elif current_drainer is not None and current_drainer[1] == "reserved":
                    preserved = _read_predecessor_authority(capability)
                    if (
                        preserved is None
                        or preserved[0] != prior_protocol
                        or preserved[2] != predecessor_receipt
                        or preserved[1][0].generation + 1 != generation
                    ):
                        raise PendingTransactionError(
                            "reserved successor lost preserved predecessor authority"
                        )
            owner_secret = secrets.token_bytes(32)
            token_digest = hashlib.sha256(owner_secret).hexdigest()
            if current_drainer is not None and current_drainer[1] != "complete":
                if (
                    current_drainer[1] != "reserved"
                    or current_drainer[0].generation != generation
                ):
                    raise PendingTransactionError(
                        "graph state already has live drainer ownership"
                    )
                drainer = current_drainer[0]
            else:
                drainer = DrainerTuple(generation, 0, secrets.token_hex(16))
            _retire_prepared_locked(capability)
            protocol: dict[str, object] = {
                "schema": 1,
                "protocol_epoch": 1,
                "generation": generation,
                "kind": kind,
                "root": str(root_path),
                "state": "BOOTSTRAP_PENDING",
                "output_identity": capability.identity.json(),
                "owner_capability_digest": token_digest,
                "bootstrap_claim_epoch": 0,
                "bootstrap_nonce": secrets.token_hex(16),
                "lease_deadline": (time.time() if now is None else now) + 30.0,
            }
            if prior_protocol is None:
                try:
                    _create_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
                except FileExistsError as exc:
                    raise PendingTransactionError(
                        "another bootstrap owner won the absent-to-pending CAS"
                    ) from exc
            else:
                _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
            if failpoint is not None:
                failpoint(capability, protocol)
            tx = Transaction(
                secrets.token_hex(32),
                kind,
                str(root_path),
                capability.path,
                capability.identity,
                generation,
                token_digest,
                None,
                drainer,
            )
            successor_protocol = dict(protocol)
            successor_protocol.update(
                state="INCOMPLETE",
                transaction_id=tx.id,
                token_identity=None,
            )
            _write_pending_transition(
                capability,
                predecessor_drainer=(
                    None
                    if current_drainer is None
                    else (current_drainer[0], current_drainer[1])
                ),
                predecessor_protocol=protocol,
                predecessor_transaction=None,
                successor=tx,
                successor_protocol=successor_protocol,
            )
            if transition_failpoint is not None:
                transition_failpoint("after_transition_record")
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(successor_protocol))
            if transition_failpoint is not None:
                transition_failpoint("after_owner_protocol")
            _write_transaction(capability, tx, phase="awaiting-drainer")
            if transition_failpoint is not None:
                transition_failpoint("after_transaction")
            deadline = (time.time() if now is None else now) + 30.0
            if current_drainer is None or current_drainer[1] == "complete":
                reservation: dict[str, object] = {"lease_deadline": deadline}
                if predecessor_receipt is not None:
                    reservation["predecessor_receipt"] = predecessor_receipt
                _transition_drainer(
                    capability,
                    expected=(
                        None
                        if current_drainer is None
                        else (current_drainer[0], current_drainer[1])
                    ),
                    drainer=drainer,
                    state="reserved",
                    failpoint=transition_failpoint,
                    **reservation,
                )
            _transition_drainer(
                capability,
                expected=(drainer, "reserved"),
                drainer=drainer,
                state="launching",
                failpoint=transition_failpoint,
                lease_deadline=deadline,
            )
            _transition_drainer(
                capability,
                expected=(drainer, "launching"),
                drainer=drainer,
                state="claimed",
                failpoint=transition_failpoint,
                acked_ids=[],
                lease_deadline=deadline,
            )
            _write_transaction(capability, tx, phase="building")
            _unlink(capability, TRANSITION_FILE)
            _AUTHORITY.set(_authority_for(tx))
            return tx
    finally:
        capability.close()


def resume_transaction(
    transaction_id: str,
    root: Path | str,
    *,
    output: Path | str = "graphify-out",
) -> Transaction:
    root_path = _canonical_directory(Path(root))
    with pin_output(output) as capability, _locked(capability):
        tx = _read_transaction(capability)
        if tx is None:
            cancellation = _read_predecessor_authority(capability)
            if (
                cancellation is not None
                and cancellation[3]["state"] != "preserved-complete"
            ):
                tx = _transaction_from_json(
                    capability, cancellation[3]["successor_transaction"]
                )
        if tx is None or tx.id != transaction_id or tx.root != str(root_path):
            raise PendingTransactionError("transaction id or root does not match live state")
        return tx


def current_transaction() -> Transaction:
    authority = _AUTHORITY.get()
    output = os.environ.get("GRAPHIFY_TRANSACTION_OUTPUT")
    if authority is None or output is None:
        raise PendingTransactionError("exact owner context required")
    with pin_output(Path(output)) as capability:
        tx = _read_transaction(capability)
    if tx is None or tx.id != authority.transaction_id:
        raise PendingTransactionError("exact owner context required")
    return tx


def active_transaction_token_path(output: Path | str = "graphify-out") -> Path:
    """Return the exact live token object after capability-relative validation."""
    with pin_output(output) as capability, _locked(capability):
        live = _read_transaction(capability)
        if live is None or live.token_identity is None:
            raise PendingTransactionError("live transaction token is unavailable")
        name = f".graphify_transaction_token.{live.id}"
        info = _entry_stat(capability, name)
        if info is None or (info.st_dev, info.st_ino) != live.token_identity:
            raise PendingTransactionError("live transaction token identity changed")
        return capability.path / name


def _pin_prepared_workspace(
    transaction: Transaction, capability: OutputCapability
) -> PreparedWorkspaceCapability:
    """Open the exact preparation directory while the owner lock is held."""
    _validate_authority(capability, transaction)
    workspace = transaction.output.parent / f".graphify-prepare-{transaction.id}"
    marker = _load_json(capability, PREPARED_FILE)
    if marker is None:
        with pin_output(workspace.parent) as parent_capability:
            try:
                os.mkdir(workspace.name, 0o700, dir_fd=parent_capability.fd)
            except FileExistsError as exc:
                raise PendingTransactionError(
                    "prepared workspace already exists without owner binding"
                ) from exc
        workspace_capability = pin_output(workspace)
        try:
            os.mkdir("graphify-out", 0o700, dir_fd=workspace_capability.fd)
            output_capability = pin_output(workspace / "graphify-out")
            prior_receipt = _load_json(capability, RECEIPT_FILE)
            prior_inventory: tuple[str, ...] = ()
            if prior_receipt is not None:
                raw_required = prior_receipt.get("required_artifacts")
                raw_digests = prior_receipt.get("artifact_digests")
                if (
                    prior_receipt.get("schema") != 1
                    or prior_receipt.get("protocol_epoch") != 1
                    or prior_receipt.get("generation") != transaction.generation - 1
                    or not isinstance(raw_required, list)
                    or len(raw_required) > _MAX_RECEIPT_ARTIFACTS
                    or not isinstance(raw_digests, dict)
                    or set(raw_required) != set(raw_digests)
                ):
                    raise PendingTransactionError(
                        "prior generation inventory is malformed"
                    )
                prior_inventory = tuple(
                    _validated_relative_name(str(name)) for name in raw_required
                )
            marker = {
                "schema": 1,
                "transaction_id": transaction.id,
                "generation": transaction.generation,
                "token_digest": transaction.token_digest,
                "identity": workspace_capability.identity.json(),
                "output_identity": output_capability.identity.json(),
                "prior_inventory": list(prior_inventory),
            }
            _create_bytes(capability, PREPARED_FILE, _json_bytes(marker))
            seed_inventory = prior_inventory or MANAGED_PUBLICATION_PATHS
            for name in seed_inventory:
                try:
                    payload = _read_relative_bytes(capability, name)
                except PendingTransactionError as exc:
                    if isinstance(exc.__cause__, FileNotFoundError):
                        continue
                    raise
                if prior_receipt is not None and hashlib.sha256(payload).hexdigest() != prior_receipt["artifact_digests"][name]:
                    raise PendingTransactionError(
                        f"prior managed artifact digest changed: {name}"
                    )
                _replace_relative_bytes(output_capability, name, payload)
        except Exception:
            with contextlib.suppress(UnboundLocalError):
                output_capability.close()
            workspace_capability.close()
            raise
    else:
        workspace_capability = pin_output(workspace)
        try:
            output_capability = pin_output(workspace / "graphify-out")
        except Exception:
            workspace_capability.close()
            raise
    try:
        if (
            marker.get("schema") != 1
            or marker.get("transaction_id") != transaction.id
            or marker.get("generation") != transaction.generation
            or marker.get("token_digest") != transaction.token_digest
            or _identity_from_json(marker.get("identity"))
            != workspace_capability.identity
            or _identity_from_json(marker.get("output_identity"))
            != output_capability.identity
            or not isinstance(marker.get("prior_inventory", []), list)
        ):
            raise PendingTransactionError("prepared workspace owner binding changed")
        return PreparedWorkspaceCapability(workspace_capability, output_capability)
    except Exception:
        output_capability.close()
        workspace_capability.close()
        raise


def prepared_workspace_path() -> Path:
    """Return the identity-bound external preparation workspace for the owner."""
    transaction = current_transaction()
    with pin_output(transaction.output) as capability, _locked(capability):
        prepared_capability = _pin_prepared_workspace(transaction, capability)
        try:
            return prepared_capability.path
        finally:
            prepared_capability.close()


def stage_transaction_handoff(transaction: Transaction) -> TransactionToken:
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction)
        name = f".graphify_transaction_token.{live.id}"
        secret = secrets.token_hex(32)
        payload = _json_bytes(
            {
                "schema": 1,
                "id": live.id,
                "root": live.root,
                "output": str(live.output),
                "generation": live.generation,
                "drainer": _drainer_json(live.drainer),
                "secret": secret,
            }
        )
        identity = _create_bytes(capability, name, payload)
        digest = hashlib.sha256(payload).hexdigest()
        live = replace(live, token_digest=digest, token_identity=identity)
        _write_transaction(capability, live)
        protocol = _read_protocol(capability)
        assert protocol is not None
        protocol["owner_capability_digest"] = digest
        protocol["token_identity"] = {"device": identity[0], "inode": identity[1]}
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
        _AUTHORITY.set(_authority_for(live))
        return TransactionToken(live.id, capability.path / name, live.generation)


def _open_token(path: Path) -> tuple[dict[str, object], bytes, tuple[int, int]]:
    output = path.parent
    with pin_output(output) as capability, _locked(capability):
        if path.name.startswith(".graphify_transaction_token.") is False:
            raise PendingTransactionError("transaction token has an invalid name")
        before = _entry_stat(capability, path.name)
        if before is None:
            raise PendingTransactionError("transaction token is missing")
        payload = _read_bytes(capability, path.name, _TOKEN_MAX_BYTES)
        try:
            raw = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PendingTransactionError("malformed transaction token") from exc
        if not isinstance(raw, dict):
            raise PendingTransactionError("malformed transaction token")
        live = _read_transaction(capability)
        identity = (before.st_dev, before.st_ino)
        if (
            live is None
            or raw.get("id") != live.id
            or raw.get("generation") != live.generation
            or raw.get("root") != live.root
            or raw.get("output") != str(live.output)
            or hashlib.sha256(payload).hexdigest() != live.token_digest
            or identity != live.token_identity
            or _drainer_from_json(raw.get("drainer")) != live.drainer
        ):
            raise PendingTransactionError("transaction token digest or identity does not match live state")
        return raw, payload, identity


def run_token(
    token_path: Path | str,
    python_args: list[str],
    *,
    prepared: bool = False,
) -> None:
    path = Path(token_path).absolute()
    if len(python_args) < 2 or python_args[0] not in {"-c", "-m"}:
        _AUTHORITY.set(None)
        raise PendingTransactionError("runner accepts only exact -c or -m shapes")
    mode, target, *arguments = python_args
    if not target or (
        mode == "-m"
        and (target.startswith("-") or "/" in target or "\\" in target)
    ):
        _AUTHORITY.set(None)
        raise PendingTransactionError("ambiguous transaction runner target")
    with pin_output(path.parent) as capability, _locked(capability):
        raw, _payload, identity = _open_token(path)
        tx = resume_transaction(
            str(raw["id"]), str(raw["root"]), output=str(raw["output"])
        )
        tx = replace(tx, token_identity=identity)
        current_drainer = _read_drainer(capability)
        if (
            current_drainer is not None
            and current_drainer[0] == tx.drainer
            and current_drainer[1] in {"reserved", "launching"}
        ):
            deadline = time.time() + 30.0
            if current_drainer[1] == "reserved":
                _transition_drainer(
                    capability,
                    expected=(tx.drainer, "reserved"),
                    drainer=tx.drainer,
                    state="launching",
                    lease_deadline=deadline,
                )
            _transition_drainer(
                capability,
                expected=(tx.drainer, "launching"),
                drainer=tx.drainer,
                state="claimed",
                acked_ids=[],
                lease_deadline=deadline,
            )
        authority_token = _AUTHORITY.set(_authority_for(tx))
    old_argv = sys.argv
    old_environment = {
        key: os.environ.get(key)
        for key in (
            "GRAPHIFY_TRANSACTION_ID",
            "GRAPHIFY_TRANSACTION_ROOT",
            "GRAPHIFY_TRANSACTION_OUTPUT",
            "GRAPHIFY_TRANSACTION_TOKEN",
            "GRAPHIFY_PREPARED_OUTPUT",
        )
    }
    os.environ.update(
        GRAPHIFY_TRANSACTION_ID=tx.id,
        GRAPHIFY_TRANSACTION_ROOT=tx.root,
        GRAPHIFY_TRANSACTION_OUTPUT=str(tx.output),
        GRAPHIFY_TRANSACTION_TOKEN=str(path),
    )
    prepared_capability: PreparedWorkspaceCapability | None = None
    prior_cwd_fd: int | None = None
    try:
        if prepared:
            with pin_output(tx.output) as output_capability, _locked(output_capability):
                prepared_capability = _pin_prepared_workspace(tx, output_capability)
            prior_cwd_fd = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fchdir(prepared_capability.output.fd)
            prepared_capability.validate()
            os.environ["GRAPHIFY_PREPARED_OUTPUT"] = "1"
        if mode == "-c":
            sys.argv = ["-c", *arguments]
            namespace = {"__name__": "__main__", "__file__": None, "__package__": None}
            exec(  # nosec B102 - exact immutable token runner intentionally implements Python -c
                compile(target, "<graphify-transaction>", "exec"), namespace, namespace
            )
        else:
            sys.argv = [target, *arguments]
            runpy.run_module(target, run_name="__main__", alter_sys=True)
    finally:
        if prior_cwd_fd is not None:
            os.fchdir(prior_cwd_fd)
            os.close(prior_cwd_fd)
        if prepared_capability is not None:
            prepared_capability.close()
        _AUTHORITY.reset(authority_token)
        sys.argv = old_argv
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_prepared_token(token_path: Path | str, python_args: list[str]) -> None:
    """Execute one exact Python shape from the retained prepared capability."""
    run_token(token_path, python_args, prepared=True)


def commit_prepared_bytes(
    transaction: Transaction, relative_name: str, payload: bytes
) -> None:
    """Publish one immutable artifact only into the pinned prepared output."""
    if not isinstance(payload, bytes):
        raise TypeError("prepared commit payload must be immutable bytes")
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        prepared = _pin_prepared_workspace(transaction, capability)
        try:
            _replace_relative_bytes(prepared.output, relative_name, payload)
        finally:
            prepared.close()


def unlink_prepared(transaction: Transaction, name: str) -> None:
    """Remove one validated relative prepared artifact under exact live authority."""
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        prepared = _pin_prepared_workspace(transaction, capability)
        try:
            _unlink_relative(prepared.output, name)
        finally:
            prepared.close()


def _validate_authority(
    capability: OutputCapability,
    transaction: Transaction,
    *,
    allow_complete: bool = False,
) -> Transaction:
    authority = _AUTHORITY.get()
    live = _read_transaction(capability)
    if authority is None or live is None:
        raise PendingTransactionError("exact owner context required")
    current_drainer = _read_drainer(capability)
    if current_drainer is None:
        raise PendingTransactionError("durable claimed drainer authority is required")
    live_drainer, drainer_state, _drainer_raw = current_drainer
    protocol = _read_protocol(capability)
    if protocol is None:
        raise PendingTransactionError("durable protocol authority is required")
    allowed_protocol_states = {"INCOMPLETE", "COMPLETE"} if allow_complete else {"INCOMPLETE"}
    if (
        transaction != live
        or authority != _authority_for(live, live_drainer)
        or authority.output_identity != capability.identity
        or live.drainer != live_drainer
        or protocol.get("state") not in allowed_protocol_states
        or protocol.get("generation") != live.generation
        or protocol.get("transaction_id") != live.id
        or protocol.get("root") != live.root
        or protocol.get("kind") != live.kind
        or protocol.get("owner_capability_digest") != live.token_digest
        or _token_identity_from_json(protocol.get("token_identity"))
        != live.token_identity
    ):
        raise PendingTransactionError("exact live drainer owner context required")
    if drainer_state != "claimed" and not (allow_complete and drainer_state == "complete"):
        raise PendingTransactionError("drainer is not exactly claimed for publication")
    return live


def _validate_durable_live_binding(
    capability: OutputCapability,
    live: Transaction,
    *,
    protocol: Mapping[str, object] | None = None,
    drainer: tuple[DrainerTuple, str, dict[str, Any]] | None = None,
    allowed_protocol_states: frozenset[str] = frozenset({"INCOMPLETE"}),
) -> tuple[dict[str, Any], tuple[DrainerTuple, str, dict[str, Any]]]:
    """Bind durable owner records without relying on process-local authority."""
    durable_protocol = (
        _read_protocol(capability) if protocol is None else dict(protocol)
    )
    durable_drainer = _read_drainer(capability) if drainer is None else drainer
    if durable_protocol is None or durable_drainer is None:
        raise PendingTransactionError("durable live transaction binding is incomplete")
    token_identity = (
        None
        if live.token_identity is None
        else {"device": live.token_identity[0], "inode": live.token_identity[1]}
    )
    if durable_drainer[0] != live.drainer:
        raise PendingTransactionError("exact live drainer binding changed")
    if (
        durable_protocol.get("state") not in allowed_protocol_states
        or durable_protocol.get("generation") != live.generation
        or durable_protocol.get("transaction_id") != live.id
        or durable_protocol.get("root") != live.root
        or durable_protocol.get("kind") != live.kind
        or durable_protocol.get("owner_capability_digest") != live.token_digest
        or durable_protocol.get("token_identity") != token_identity
        or durable_protocol.get("output_identity") != capability.identity.json()
        or live.output != capability.path
        or live.output_identity != capability.identity
        or durable_drainer[0].generation != live.generation
    ):
        raise PendingTransactionError("durable live transaction binding changed")
    if live.token_identity is not None:
        token_name = f".graphify_transaction_token.{live.id}"
        token = _entry_stat(capability, token_name)
        if token is None or (token.st_dev, token.st_ino) != live.token_identity:
            raise PendingTransactionError("durable live transaction token changed")
        if hashlib.sha256(_read_bytes(capability, token_name)).hexdigest() != live.token_digest:
            raise PendingTransactionError("durable live transaction token changed")
    return durable_protocol, durable_drainer


@contextlib.contextmanager
def owned_step(transaction: Transaction, *, drainer: DrainerTuple | None = None) -> Iterator[None]:
    authority = _AUTHORITY.get()
    if authority is None or authority.transaction_id != transaction.id:
        raise PendingTransactionError("exact owner context required")
    if drainer is not None and drainer != authority.drainer:
        raise PendingTransactionError("caller-selected drainer authority is forbidden")
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
    yield


@contextlib.contextmanager
def closing_step(
    transaction: Transaction, *, drainer: DrainerTuple | None = None
) -> Iterator[None]:
    """Admit receipt acknowledgement and close coordination, never publication."""
    authority = _AUTHORITY.get()
    if authority is None or authority.transaction_id != transaction.id:
        raise PendingTransactionError("exact owner context required")
    if drainer is not None and drainer != authority.drainer:
        raise PendingTransactionError("caller-selected drainer authority is forbidden")
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction, allow_complete=True)
    yield


def commit_bytes(
    transaction: Transaction,
    name: str,
    payload: bytes,
    *,
    capability: OutputCapability | None = None,
    failpoint: Callable[[str], None] | None = None,
) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("commit payload must be prepared immutable bytes")
    owned = capability is None
    cap = pin_output(transaction.output) if owned else capability
    assert cap is not None
    try:
        with _locked(cap):
            _validate_authority(cap, transaction)
            if failpoint:
                failpoint("after_validate")
            _replace_bytes(cap, name, payload)
            if failpoint:
                failpoint("after_replace")
    finally:
        if owned:
            cap.close()


def commit_relative_bytes(
    transaction: Transaction,
    relative_name: str,
    payload: bytes,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> None:
    """Commit one prepared file below the pinned output without path fallback."""
    if not isinstance(payload, bytes):
        raise TypeError("commit payload must be prepared immutable bytes")
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        if failpoint:
            failpoint("after_validate")
        _replace_relative_bytes(capability, relative_name, payload)
        if failpoint:
            failpoint("after_replace")


def commit_unlink(
    transaction: Transaction,
    name: str,
    *,
    capability: OutputCapability | None = None,
) -> None:
    """Remove one prepared managed artifact under exact live authority."""
    owned = capability is None
    cap = pin_output(transaction.output) if owned else capability
    assert cap is not None
    try:
        with _locked(cap):
            _validate_authority(cap, transaction)
            _unlink(cap, name)
    finally:
        if owned:
            cap.close()


def _validated_relative_name(value: str) -> str:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {value}")
    name = relative.as_posix()
    if relative.parts[0].startswith(
        (
            ".graphify_transaction",
            ".graphify_protocol",
            ".graphify_generation",
            ".graphify_drainer",
            ".graphify_rebuild_",
            ".graphify_prepared",
            ".graphify_legacy_pending",
            ".graphify_transition",
        )
    ):
        raise PendingTransactionError(f"publication plan contains coordination state: {name}")
    return name


def publication_plan_from_directory(
    directory: Path | str,
    *,
    prior_inventory: Mapping[str, bytes] | Sequence[str] = (),
) -> PublicationPlan:
    """Inventory every prepared regular file and reconcile only prior managed paths."""
    root = Path(directory).resolve(strict=True)
    payloads: dict[str, bytes] = {}
    aggregate = 0
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise PendingTransactionError("prepared publication contains a symlink")
        if not entry.is_file():
            continue
        relative = _validated_relative_name(entry.relative_to(root).as_posix())
        payload = entry.read_bytes()
        aggregate += len(payload)
        if len(payloads) >= _MAX_RECEIPT_ARTIFACTS or aggregate > _MAX_RECEIPT_AGGREGATE_BYTES:
            raise PendingTransactionError("publication plan exceeds bounded inventory")
        payloads[relative] = payload
    prior_names = (
        tuple(prior_inventory)
        if isinstance(prior_inventory, Mapping)
        else tuple(prior_inventory)
    )
    deletions = tuple(
        sorted(
            _validated_relative_name(name)
            for name in prior_names
            if _validated_relative_name(name) not in payloads
        )
    )
    return PublicationPlan(payloads, deletions)


def _unlink_relative(capability: OutputCapability, relative_name: str) -> None:
    relative = Path(_validated_relative_name(relative_name))
    parent_fd = os.dup(capability.fd)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            info = os.stat(relative.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise PendingTransactionError(
                f"unsafe managed artifact deletion: {relative_name}"
            )
        os.unlink(relative.parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
        capability.validate()
    except (FileNotFoundError, NotADirectoryError):
        return
    finally:
        os.close(parent_fd)


def commit_publication_plan(
    transaction: Transaction,
    plan: PublicationPlan,
    *,
    graph_name: str = "graph.json",
) -> GenerationReceipt:
    """Publish one validated inventory, its explicit deletions, and receipt last."""
    graph_name = _validated_relative_name(graph_name)
    graph_payload = plan.payloads.get(graph_name)
    manifest_payload = plan.payloads.get("manifest.json")
    if graph_payload is None or manifest_payload is None:
        raise PendingTransactionError("publication plan requires graph and manifest")
    with owned_step(transaction):
        for name in plan.deletions:
            with pin_output(transaction.output) as capability, _locked(capability):
                _validate_authority(capability, transaction)
                _unlink_relative(capability, name)
        for name, payload in plan.payloads.items():
            if "/" in name:
                commit_relative_bytes(transaction, name, payload)
            else:
                commit_bytes(transaction, name, payload)
        return commit_generation(
            transaction,
            graph_payload=graph_payload,
            manifest_payload=manifest_payload,
            required_artifacts=tuple(plan.payloads),
            graph_name=graph_name,
        )


def _watermark(payload: bytes) -> dict[str, object]:
    try:
        graph = json.loads(payload.decode("utf-8"))
        metadata = graph["graph"][GRAPH_WATERMARK_KEY]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PendingTransactionError("graph payload has no valid protocol watermark") from exc
    if not isinstance(metadata, dict) or metadata.get("schema") != 1 or metadata.get("protocol_epoch") != 1:
        raise PendingTransactionError("unsupported graph watermark schema")
    return metadata


def _validate_pristine_or_legacy_graph(
    capability: OutputCapability,
    *,
    allowed_without_graph: Sequence[str] = (),
) -> None:
    """Allow only an empty output or a valid graph with no protocol watermark."""
    if _entry_stat(capability, "graph.json") is None:
        allowed = set(allowed_without_graph)
        entries = _list_entries(capability)
        if len(entries) > _MAX_RECEIPT_ARTIFACTS:
            raise PendingTransactionError("bootstrap output inventory exceeds bound")
        orphaned = [name for name in entries if name not in allowed]
        if orphaned:
            raise PendingTransactionError(
                "managed or coordination state exists without graph authority: "
                + ", ".join(sorted(orphaned))
            )
        return
    payload = _read_relative_bytes(capability, "graph.json")
    try:
        graph = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError(
            "malformed graph cannot be treated as legacy bootstrap state"
        ) from exc
    if not isinstance(graph, dict):
        raise PendingTransactionError(
            "malformed graph cannot be treated as legacy bootstrap state"
        )
    metadata = graph.get("graph", {})
    edges = graph.get("links", graph.get("edges"))
    if (
        not isinstance(metadata, dict)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(edges, list)
    ):
        raise PendingTransactionError(
            "malformed graph cannot be treated as legacy bootstrap state"
        )
    if GRAPH_WATERMARK_KEY in metadata:
        raise PendingTransactionError(
            "watermarked graph is partial predecessor authority"
        )


def _validated_legacy_pending_bridge(
    capability: OutputCapability,
    *,
    selected_name: str | None = None,
) -> str:
    bridge = _load_json(capability, LEGACY_PENDING_STATE_FILE)
    if not isinstance(bridge, dict) or bridge.get("schema") != 1:
        raise PendingTransactionError("legacy pending bridge is malformed")
    raw_name = bridge.get("name", selected_name or ".pending_changes")
    if (
        not isinstance(raw_name, str)
        or Path(raw_name).name != raw_name
        or (selected_name is not None and raw_name != selected_name)
    ):
        raise PendingTransactionError("legacy pending bridge name is malformed")
    info = _entry_stat(capability, raw_name)
    identity = bridge.get("identity")
    offset = bridge.get("offset")
    if (
        info is None
        or not isinstance(identity, dict)
        or type(identity.get("device")) is not int
        or type(identity.get("inode")) is not int
        or (identity["device"], identity["inode"]) != (info.st_dev, info.st_ino)
        or type(offset) is not int
        or not 0 <= offset <= info.st_size
    ):
        raise PendingTransactionError("legacy pending bridge identity is malformed")
    return raw_name


def commit_generation(
    transaction: Transaction,
    *,
    graph_payload: bytes,
    manifest_payload: bytes,
    required_artifacts: tuple[str, ...],
    graph_name: str = "graph.json",
) -> GenerationReceipt:
    watermark = _watermark(graph_payload)
    if graph_name not in required_artifacts or "manifest.json" not in required_artifacts:
        raise PendingTransactionError(
            "generation inventory must include graph and manifest"
        )
    if watermark.get("generation") != transaction.generation or watermark.get("state") != "active":
        raise PendingTransactionError("graph watermark does not match transaction generation")
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction)
        if _read_bytes(capability, graph_name, max(len(graph_payload), 1)) != graph_payload:
            raise PendingTransactionError("published graph differs from prepared graph")
        if _read_bytes(capability, "manifest.json", max(len(manifest_payload), 1)) != manifest_payload:
            raise PendingTransactionError("published manifest differs from prepared manifest")
        artifact_digests = {
            name: hashlib.sha256(_read_relative_bytes(capability, name)).hexdigest()
            for name in dict.fromkeys(required_artifacts)
        }
        receipt_body = {
            "schema": 1,
            "protocol_epoch": 1,
            "generation": live.generation,
            "transaction_id": live.id,
            "token_digest": live.token_digest,
            "output_identity": live.output_identity.json(),
            "drainer": _drainer_json(_AUTHORITY.get().drainer),  # type: ignore[union-attr]
            "graph_digest": hashlib.sha256(graph_payload).hexdigest(),
            "graph_name": graph_name,
            "manifest_digest": hashlib.sha256(manifest_payload).hexdigest(),
            "watermark": watermark,
            "required_artifacts": list(required_artifacts),
            "artifact_digests": artifact_digests,
        }
        receipt_payload = _json_bytes(receipt_body)
        digest = hashlib.sha256(receipt_payload).hexdigest()
        protocol = _read_protocol(capability)
        if protocol is None:
            raise PendingTransactionError("protocol state is missing")
        protocol.update(state="COMPLETE", generation=live.generation, receipt_digest=digest)
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
        _replace_bytes(capability, RECEIPT_FILE, receipt_payload)
        _unlink(capability, PREDECESSOR_FILE)
        return GenerationReceipt(digest, live.generation)


def _validate_receipt_locked(
    capability: OutputCapability,
    *,
    transaction: Transaction | None = None,
    graph_payload: bytes | None = None,
    require_closed: bool = False,
    allow_missing_completed_drainer: bool = False,
    retain_artifacts: Sequence[str] = (),
    protocol_override: Mapping[str, object] | None = None,
    drainer_override: tuple[DrainerTuple, str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, dict[str, bytes]]:
    try:
        receipt_payload = _read_bytes(capability, RECEIPT_FILE)
    except FileNotFoundError as exc:
        raise PendingTransactionError("generation receipt is missing") from exc
    try:
        receipt = json.loads(receipt_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("generation receipt is malformed") from exc
    protocol = (
        _read_protocol(capability)
        if protocol_override is None
        else _protocol_from_json(capability, protocol_override)
    )
    if not isinstance(receipt, dict) or protocol is None:
        raise PendingTransactionError("generation receipt is missing")
    digest = hashlib.sha256(receipt_payload).hexdigest()
    drainer = _drainer_from_json(receipt.get("drainer"))
    required = receipt.get("required_artifacts")
    artifact_digests = receipt.get("artifact_digests")
    if (
        receipt.get("schema") != 1
        or receipt.get("protocol_epoch") != 1
        or type(receipt.get("generation")) is not int
        or int(receipt["generation"]) < 1
        or drainer.generation != receipt.get("generation")
        or protocol.get("state") != "COMPLETE"
        or protocol.get("receipt_digest") != digest
        or protocol.get("generation") != receipt.get("generation")
        or protocol.get("transaction_id") != receipt.get("transaction_id")
        or protocol.get("owner_capability_digest") != receipt.get("token_digest")
        or _identity_from_json(protocol.get("output_identity")) != capability.identity
        or _identity_from_json(receipt.get("output_identity")) != capability.identity
        or not isinstance(required, list)
        or not required
        or len(required) > _MAX_RECEIPT_ARTIFACTS
        or not all(isinstance(name, str) for name in required)
        or len(required) != len(set(required))
        or receipt.get("graph_name", "graph.json") not in required
        or "manifest.json" not in required
        or not isinstance(artifact_digests, dict)
        or set(artifact_digests) != set(required)
    ):
        raise PendingTransactionError("generation receipt does not match protocol")
    if transaction is not None and (
        receipt.get("transaction_id") != transaction.id
        or receipt.get("generation") != transaction.generation
        or receipt.get("token_digest") != transaction.token_digest
        or drainer != transaction.drainer
    ):
        raise PendingTransactionError("generation receipt does not match live owner")
    current_drainer = (
        _read_drainer(capability)
        if drainer_override is None
        else drainer_override
    )
    reserved_successor = (
        transaction is None
        and current_drainer is not None
        and current_drainer[1] == "reserved"
        and current_drainer[0].generation == drainer.generation + 1
        and current_drainer[2].get("predecessor_receipt") == digest
    )
    missing_completed_drainer = (
        allow_missing_completed_drainer
        and transaction is None
        and current_drainer is None
        and protocol.get("state") == "COMPLETE"
    )
    if not missing_completed_drainer and (
        current_drainer is None
        or (current_drainer[0] != drainer and not reserved_successor)
    ):
        raise PendingTransactionError("generation receipt does not match live drainer")
    if require_closed and (
        current_drainer is None
        or current_drainer[1] != "complete"
        or current_drainer[2].get("receipt_digest") != digest
    ):
        raise PendingTransactionError("generation close is incomplete")
    inventory: dict[str, bytes] = {}
    retained = set(retain_artifacts)
    graph_name = str(receipt.get("graph_name", "graph.json"))
    if allow_missing_completed_drainer:
        retained.add(graph_name)
    retain_all = "*" in retained
    aggregate_size = 0
    actual_digests: dict[str, str] = {}
    for name in required:
        artifact_digest, artifact_size, artifact_body = _hash_relative_bytes(
            capability,
            name,
            retain=retain_all or name in retained,
            aggregate_remaining=_MAX_RECEIPT_AGGREGATE_BYTES - aggregate_size,
        )
        aggregate_size += artifact_size
        actual_digests[name] = artifact_digest
        if artifact_digest != artifact_digests[name]:
            raise PendingTransactionError(f"managed artifact digest changed: {name}")
        if artifact_body is not None:
            inventory[name] = artifact_body
    if actual_digests["manifest.json"] != receipt.get("manifest_digest"):
        raise PendingTransactionError("manifest digest changed after receipt")
    actual_graph_digest = (
        hashlib.sha256(graph_payload).hexdigest()
        if graph_payload is not None
        else actual_digests[graph_name]
    )
    if actual_graph_digest != receipt.get("graph_digest"):
        raise PendingTransactionError("graph digest changed after receipt")
    if graph_payload is not None:
        inventory[graph_name] = graph_payload
    if allow_missing_completed_drainer:
        retained_graph = inventory.get(graph_name)
        if retained_graph is None:
            raise PendingTransactionError("completed graph payload was not retained")
        watermark = _watermark(retained_graph)
        watermark_output = watermark.get("output_identity")
        if (
            watermark.get("state") != "active"
            or watermark.get("generation") != receipt.get("generation")
            or receipt.get("watermark") != watermark
            or (
                watermark.get("transaction_id") is not None
                and watermark.get("transaction_id") != receipt.get("transaction_id")
            )
            or (
                watermark_output is not None
                and _identity_from_json(watermark_output) != capability.identity
            )
        ):
            raise PendingTransactionError(
                "completed graph watermark does not match receipt authority"
            )
    return receipt, digest, inventory


def _coordination_present(capability: OutputCapability) -> bool:
    for name in _list_entries(capability):
        if name in _COORDINATION_FILES or name.startswith(_COORDINATION_PREFIXES):
            return True
    return False


def open_graph_snapshot(path: Path | str, *, purpose: str) -> GraphSnapshot:
    requested = Path(path).expanduser()
    output = requested.parent.resolve(strict=True)
    graph_path = output / requested.name
    with pin_output(output, mutation=False) as capability, _locked(capability):
        if _entry_stat(capability, graph_path.name) is None:
            protocol = _read_protocol(capability)
            if protocol is not None:
                label = (
                    "bootstrap"
                    if protocol.get("state") == "BOOTSTRAP_PENDING"
                    else "protocol"
                )
                raise PendingTransactionError(
                    f"{label} state exists without a graph receipt"
                )
            raise FileNotFoundError(graph_path)
        from graphify.security import _max_graph_file_bytes

        payload = _read_bytes(
            capability,
            graph_path.name,
            _max_graph_file_bytes(),
        )
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PendingTransactionError("malformed graph payload") from exc
        if not isinstance(data, dict):
            raise PendingTransactionError("malformed graph payload")
        graph_meta = data.get("graph")
        watermark = graph_meta.get(GRAPH_WATERMARK_KEY) if isinstance(graph_meta, dict) else None
        if watermark is None:
            if _coordination_present(capability):
                protocol = _read_protocol(capability)
                drainer = _read_drainer(capability)
                publication_active = (
                    protocol is not None
                    or _entry_stat(capability, TRANSACTION_FILE) is not None
                    or _entry_stat(capability, RECEIPT_FILE) is not None
                    or _entry_stat(capability, PREPARED_FILE) is not None
                    or any(
                        name.startswith(
                            (".graphify_transaction_token.", ".graphify_rebuild_inflight.")
                        )
                        for name in _list_entries(capability)
                    )
                    or drainer is None
                    or drainer[1] != "reserved"
                )
                if not publication_active:
                    protocol = None
                else:
                    state = None if protocol is None else protocol.get("state")
                    label = "bootstrap" if state == "BOOTSTRAP_PENDING" else "protocol"
                    raise PendingTransactionError(f"{label} state exists without a graph receipt")
            legacy_inventory = {graph_path.name: payload}
            manifest_payload = None
            for name in MANAGED_PUBLICATION_PATHS:
                if name == graph_path.name:
                    continue
                try:
                    artifact_payload = _read_relative_bytes(capability, name)
                except PendingTransactionError as exc:
                    if "is missing" in str(exc):
                        continue
                    raise
                legacy_inventory[name] = artifact_payload
                if name == "manifest.json":
                    manifest_payload = artifact_payload
            return GraphSnapshot(
                data,
                None,
                graph_path,
                payload,
                hashlib.sha256(payload).hexdigest(),
                manifest_payload,
                legacy_inventory,
            )
        if not isinstance(watermark, dict) or watermark.get("schema") != 1 or watermark.get("protocol_epoch") != 1:
            raise PendingTransactionError("unsupported graph watermark schema")
        if watermark.get("state") != "active":
            raise PendingTransactionError(f"graph watermark state {watermark.get('state')!r} is unavailable")
        generation = watermark.get("generation")
        if not isinstance(generation, int):
            raise PendingTransactionError("graph watermark generation is invalid")
        retain_by_purpose = {
            "reflect-community": (".graphify_analysis.json", ".graphify_labels.json"),
            "reflect-lessons": (".graphify_analysis.json", ".graphify_labels.json"),
            "cluster-only": (".graphify_labels.json", ".graphify_labels.json.sig"),
            "export": (".graphify_analysis.json", ".graphify_labels.json"),
        }
        retain = retain_by_purpose.get(purpose, ())
        if purpose in {
            "extract-baseline",
            "watch-prepare",
            "publication-prepare",
            "export-admission",
            "serve",
            "mcp-context-admission",
            "tree-prepare",
        }:
            retain = ("*",)
        receipt, _digest, inventory = _validate_receipt_locked(
            capability,
            graph_payload=payload,
            retain_artifacts=("manifest.json", *retain),
        )
        if (
            receipt.get("generation") != generation
            or receipt.get("graph_name", "graph.json") != graph_path.name
            or receipt.get("watermark") != watermark
        ):
            raise PendingTransactionError("generation receipt does not match graph watermark")
        return GraphSnapshot(
            data,
            int(generation),
            graph_path,
            payload,
            hashlib.sha256(payload).hexdigest(),
            inventory["manifest.json"],
            inventory,
        )


def open_prepared_graph(transaction: Transaction, path: Path | str) -> GraphSnapshot:
    """Read an unpublished graph only for its exact live transaction owner."""
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        prepared_capability = _pin_prepared_workspace(transaction, capability)
        try:
            requested = Path(path).expanduser().absolute()
            expected = prepared_capability.output.path / requested.name
            live_alias = transaction.output / requested.name
            if requested not in {expected, live_alias}:
                raise PendingTransactionError("prepared graph is outside the owned workspace")
            payload = _read_relative_bytes(prepared_capability.output, requested.name)
            artifacts: dict[str, bytes] = {requested.name: payload}
            marker = _load_json(capability, PREPARED_FILE)
            if marker is None or not isinstance(marker.get("prior_inventory"), list):
                raise PendingTransactionError("prepared inventory binding is missing")
            prior_inventory = tuple(
                _validated_relative_name(str(name))
                for name in marker["prior_inventory"]
            )
            for name in prior_inventory or MANAGED_PUBLICATION_PATHS:
                if name == requested.name:
                    continue
                try:
                    artifacts[name] = _read_relative_bytes(
                        prepared_capability.output, name
                    )
                except PendingTransactionError as exc:
                    if isinstance(exc.__cause__, FileNotFoundError):
                        continue
                    raise
        finally:
            prepared_capability.close()
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PendingTransactionError("malformed prepared graph payload") from exc
        if not isinstance(data, dict):
            raise PendingTransactionError("malformed prepared graph payload")
        return GraphSnapshot(
            data,
            None,
            expected,
            payload,
            hashlib.sha256(payload).hexdigest(),
            artifacts.get("manifest.json"),
            artifacts,
        )


def _validate_queue_item(item: object) -> dict[str, Any]:
    changed_paths = item.get("changed_paths") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or set(item)
        != {
            "schema",
            "id",
            "kind",
            "intent",
            "root",
            "changed_paths",
            "semantic",
            "source",
            "time",
        }
        or type(item.get("schema")) is not int
        or item.get("schema") != 1
        or type(item.get("kind")) is not str
        or item.get("kind") not in {"full", "update", "runtime"}
        or not _is_hex(item.get("id"))
        or type(item.get("root")) is not str
        or not Path(item["root"]).is_absolute()
        or "\x00" in item["root"]
        or not isinstance(changed_paths, (list, type(None)))
        or (
            isinstance(changed_paths, list)
            and (
                len(changed_paths) > _MAX_QUEUE_PATHS
                or not all(
                    type(value) is str
                    and 0 < len(value) <= _MAX_QUEUE_PATH_LENGTH
                    and "\x00" not in value
                    for value in changed_paths
                )
            )
        )
        or type(item.get("intent")) is not str
        or not 0 < len(item["intent"]) <= _MAX_QUEUE_PATH_LENGTH
        or "\x00" in item["intent"]
        or type(item.get("source")) is not str
        or not 0 < len(item["source"]) <= _MAX_QUEUE_PATH_LENGTH
        or "\x00" in item["source"]
        or type(item.get("semantic")) is not bool
        or type(item.get("time")) not in {int, float}
        or not math.isfinite(float(cast(int | float, item.get("time"))))
    ):
        raise PendingTransactionError("malformed rebuild queue")
    return item


def _read_queue(capability: OutputCapability, name: str = QUEUE_FILE) -> list[dict[str, Any]]:
    if _entry_stat(capability, name) is None:
        return []
    try:
        values = [json.loads(line) for line in _read_bytes(capability, name).decode().splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("malformed rebuild queue") from exc
    if len(values) > _MAX_QUEUE_ITEMS:
        raise PendingTransactionError("rebuild queue exceeds item bound")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in values:
        item = _validate_queue_item(item)
        item_id = str(item["id"])
        if item_id in seen_ids:
            raise PendingTransactionError("duplicate rebuild intent id")
        seen_ids.add(item_id)
        result.append(item)
    return result


def _queue_payload(items: list[dict[str, Any]]) -> bytes:
    if len(items) > _MAX_QUEUE_ITEMS:
        raise PendingTransactionError("rebuild queue exceeds item bound")
    validated = [_validate_queue_item(item) for item in items]
    payload = b"".join(_json_bytes(item) + b"\n" for item in validated)
    if len(payload) > _MAX_STATE_BYTES:
        raise PendingTransactionError("rebuild queue exceeds serialized budget")
    return payload


def _write_queue(capability: OutputCapability, name: str, items: list[dict[str, Any]]) -> None:
    payload = _queue_payload(items)
    _replace_bytes(capability, name, payload)


def _merge_intents(*groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            merged.setdefault(str(item["id"]), item)
    return list(merged.values())


def _drainer_state_from_json(
    capability: OutputCapability, raw: object
) -> tuple[DrainerTuple, str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise PendingTransactionError("malformed drainer state")
    base_fields = {
        "schema",
        "protocol_epoch",
        "generation",
        "claim_epoch",
        "launch_nonce",
        "state",
    }
    if (
        type(raw.get("schema")) is not int
        or raw.get("schema") != 1
        or type(raw.get("protocol_epoch")) is not int
        or raw.get("protocol_epoch") != 1
    ):
        raise PendingTransactionError("unsupported drainer schema or protocol epoch")
    state = raw.get("state")
    if state not in {"reserved", "launching", "claimed", "CLOSE_PENDING", "complete"}:
        raise PendingTransactionError("malformed drainer state")
    expected_fields = set(base_fields)
    if state in {"reserved", "launching", "claimed"}:
        expected_fields.add("lease_deadline")
        if state == "reserved" and "predecessor_receipt" in raw:
            expected_fields.add("predecessor_receipt")
        if state == "claimed":
            expected_fields.add("acked_ids")
            if "receipt_digest" in raw:
                expected_fields.add("receipt_digest")
        deadline = raw.get("lease_deadline")
        if type(deadline) not in {int, float} or not math.isfinite(
            float(cast(int | float, deadline))
        ):
            raise PendingTransactionError("malformed drainer lease")
    elif state in {"CLOSE_PENDING", "complete"}:
        expected_fields.update(
            {
                "receipt_digest",
                "acked_ids",
                "queue_epoch",
                "output_identity",
                "successor_generation",
                "transaction_id",
                "token_identity",
            }
        )
    if set(raw) != expected_fields:
        raise PendingTransactionError("malformed drainer state fields")
    drainer = _drainer_from_json(raw)
    predecessor_receipt = raw.get("predecessor_receipt")
    if predecessor_receipt is not None and not _is_hex(predecessor_receipt):
        raise PendingTransactionError("malformed drainer predecessor receipt")
    if state == "claimed":
        acked_ids = raw.get("acked_ids")
        if (
            not isinstance(acked_ids, list)
            or len(acked_ids) > _MAX_QUEUE_ITEMS
            or len(acked_ids) != len(set(acked_ids))
            or not all(_is_hex(value) for value in acked_ids)
            or (
                "receipt_digest" in raw
                and not _is_hex(raw.get("receipt_digest"))
            )
        ):
            raise PendingTransactionError("malformed claimed drainer acknowledgements")
    if state in {"CLOSE_PENDING", "complete"}:
        if (
            type(raw.get("successor_generation")) is not int
            or raw["successor_generation"] != drainer.generation + 1
        ):
            raise PendingTransactionError("close successor generation is malformed")
        if (
            not _is_hex(raw.get("receipt_digest"))
            or _identity_from_json(raw.get("output_identity")) != capability.identity
            or not _is_hex(raw.get("transaction_id"))
        ):
            raise PendingTransactionError("malformed close drainer binding")
    if state in {"CLOSE_PENDING", "complete"}:
        acked_ids = raw.get("acked_ids")
        if (
            type(raw.get("queue_epoch")) is not int
            or raw["queue_epoch"] != drainer.generation
            or not isinstance(acked_ids, list)
            or len(acked_ids) > _MAX_QUEUE_ITEMS
            or len(acked_ids) != len(set(acked_ids))
            or not all(_is_hex(value) for value in acked_ids)
        ):
            raise PendingTransactionError("malformed close drainer binding")
        _token_identity_from_json(raw.get("token_identity"))
    return drainer, str(state), raw


def _read_drainer(capability: OutputCapability) -> tuple[DrainerTuple, str, dict[str, Any]] | None:
    raw = _load_json(capability, DRAINER_FILE)
    return None if raw is None else _drainer_state_from_json(capability, raw)


def _predecessor_authority_from_json(
    capability: OutputCapability, raw: object
) -> tuple[
    dict[str, Any],
    tuple[DrainerTuple, str, dict[str, Any]],
    str,
    dict[str, Any],
]:
    states = {
        "preserved-complete",
        "cancelling",
        "protocol-restored",
        "drainer-restored",
        "prepared-retired",
        "token-removed",
        "live-removed",
    }
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "schema",
            "protocol_epoch",
            "state",
            "output_identity",
            "successor_generation",
            "receipt_digest",
            "protocol",
            "drainer",
            "successor_transaction",
            "prepared_workspace",
        }
        or raw.get("schema") != 1
        or raw.get("protocol_epoch") != 1
        or raw.get("state") not in states
        or _identity_from_json(raw.get("output_identity")) != capability.identity
        or not _is_hex(raw.get("receipt_digest"))
    ):
        raise PendingTransactionError("preserved predecessor authority is malformed")
    protocol = _protocol_from_json(capability, raw.get("protocol"))
    drainer = _drainer_state_from_json(capability, raw.get("drainer"))
    receipt_digest = str(raw["receipt_digest"])
    successor_raw = raw.get("successor_transaction")
    if raw["state"] == "preserved-complete":
        if successor_raw is not None:
            raise PendingTransactionError(
                "preserved predecessor successor binding is malformed"
            )
    else:
        successor = _transaction_from_json(capability, successor_raw)
        if successor.generation != raw.get("successor_generation"):
            raise PendingTransactionError(
                "preserved predecessor successor binding changed"
            )
        _prepared_cancellation_marker_from_json(
            raw.get("prepared_workspace"), successor
        )
    if raw["state"] == "preserved-complete" and raw.get("prepared_workspace") is not None:
        raise PendingTransactionError(
            "preserved predecessor prepared binding is malformed"
        )
    if (
        protocol.get("state") != "COMPLETE"
        or drainer[1] != "complete"
        or protocol.get("receipt_digest") != receipt_digest
        or drainer[2].get("receipt_digest") != receipt_digest
        or protocol.get("generation") != drainer[0].generation
        or protocol.get("transaction_id") != drainer[2].get("transaction_id")
        or protocol.get("token_identity") != drainer[2].get("token_identity")
        or protocol.get("output_identity") != drainer[2].get("output_identity")
        or type(raw.get("successor_generation")) is not int
        or raw["successor_generation"] != drainer[0].generation + 1
    ):
        raise PendingTransactionError("preserved predecessor authority binding changed")
    return protocol, drainer, receipt_digest, dict(raw)


def _read_predecessor_authority(
    capability: OutputCapability,
) -> tuple[
    dict[str, Any],
    tuple[DrainerTuple, str, dict[str, Any]],
    str,
    dict[str, Any],
] | None:
    raw = _load_json(capability, PREDECESSOR_FILE)
    return None if raw is None else _predecessor_authority_from_json(capability, raw)


def _write_predecessor_authority(
    capability: OutputCapability,
    protocol: Mapping[str, object],
    drainer: tuple[DrainerTuple, str, dict[str, Any]],
    receipt_digest: str,
) -> None:
    record = {
        "schema": 1,
        "protocol_epoch": 1,
        "state": "preserved-complete",
        "output_identity": capability.identity.json(),
        "successor_generation": drainer[0].generation + 1,
        "receipt_digest": receipt_digest,
        "protocol": dict(protocol),
        "drainer": dict(drainer[2]),
        "successor_transaction": None,
        "prepared_workspace": None,
    }
    _predecessor_authority_from_json(capability, record)
    _replace_bytes(capability, PREDECESSOR_FILE, _json_bytes(record))


def _advance_cancellation_authority(
    capability: OutputCapability,
    record: Mapping[str, object],
    *,
    state: str,
    successor: Transaction,
) -> dict[str, Any]:
    updated = {
        **record,
        "state": state,
        "successor_transaction": _transaction_json(
            successor, phase=successor.phase
        ),
    }
    parsed = _predecessor_authority_from_json(capability, updated)
    _replace_bytes(capability, PREDECESSOR_FILE, _json_bytes(updated))
    return parsed[3]


def _write_drainer(capability: OutputCapability, drainer: DrainerTuple, state: str, **extra: object) -> None:
    if state not in {"reserved", "launching", "claimed", "CLOSE_PENDING", "complete"}:
        raise PendingTransactionError("unsupported drainer state transition")
    _replace_bytes(
        capability,
        DRAINER_FILE,
        _json_bytes(
            {
                "schema": 1,
                "protocol_epoch": 1,
                **_drainer_json(drainer),
                "state": state,
                **extra,
            }
        ),
    )


def _transition_drainer(
    capability: OutputCapability,
    *,
    expected: tuple[DrainerTuple, str] | None,
    drainer: DrainerTuple,
    state: str,
    failpoint: Callable[[str], None] | None = None,
    **extra: object,
) -> None:
    """Perform one exact tuple/state CAS while the output lock is pinned."""
    current = _read_drainer(capability)
    if expected is None:
        if current is not None:
            raise PendingTransactionError("drainer transition lost absent-state CAS")
    elif current is None or (current[0], current[1]) != expected:
        raise PendingTransactionError("drainer transition lost exact tuple/state CAS")
    _write_drainer(capability, drainer, state, **extra)
    if failpoint is not None:
        failpoint(f"after_drainer_{state}")


def queue_rebuild(
    kind: TransactionKind,
    root: Path | str,
    *,
    output: Path | str = "graphify-out",
    changed_paths: Sequence[Path | str] | None = None,
    semantic: bool = False,
    source: str = "unknown",
    intent: str | None = None,
    now: float | None = None,
    legacy_pending_name: str | None = None,
) -> QueueReceipt:
    if type(kind) is not str or kind not in {"full", "update", "runtime"}:
        raise PendingTransactionError("malformed rebuild kind")
    if type(semantic) is not bool:
        raise PendingTransactionError("malformed rebuild semantic flag")
    if type(source) is not str or not 0 < len(source) <= _MAX_QUEUE_PATH_LENGTH or "\x00" in source:
        raise PendingTransactionError("malformed rebuild source")
    if intent is not None and (
        type(intent) is not str
        or not 0 < len(intent) <= _MAX_QUEUE_PATH_LENGTH
        or "\x00" in intent
    ):
        raise PendingTransactionError("malformed rebuild intent")
    if now is not None and (
        type(now) not in {int, float} or not math.isfinite(float(now))
    ):
        raise PendingTransactionError("malformed rebuild time")
    durable_paths: list[str] = []
    if changed_paths is not None:
        if isinstance(changed_paths, (str, bytes)):
            raise PendingTransactionError("malformed rebuild changed paths")
        for value in changed_paths:
            durable_value = os.fspath(value)
            if type(durable_value) is not str:
                raise PendingTransactionError("malformed rebuild changed path")
            durable_paths.append(durable_value)
    root_value = os.fspath(root)
    if type(root_value) is not str:
        raise PendingTransactionError("malformed rebuild root")
    root_path = _canonical_directory(Path(root_value))
    with pin_output(output, create=True) as capability, _locked(capability):
        protocol = _read_protocol(capability)
        receipt_present = _entry_stat(capability, RECEIPT_FILE) is not None
        existing_drainer = _read_drainer(capability)
        if protocol is None and not receipt_present:
            allowed_without_graph = list(_SAFE_GRAPHLESS_RUNTIME_ENTRIES)
            if existing_drainer is not None:
                if (
                    existing_drainer[1] != "reserved"
                    or existing_drainer[0].generation != 1
                    or existing_drainer[2].get("predecessor_receipt") is not None
                ):
                    raise PendingTransactionError(
                        "partial predecessor authority cannot bootstrap"
                    )
                _read_queue(capability)
                allowed_without_graph.append(DRAINER_FILE)
                if _entry_stat(capability, QUEUE_FILE) is not None:
                    allowed_without_graph.append(QUEUE_FILE)
            if legacy_pending_name is not None:
                legacy_info = _entry_stat(capability, legacy_pending_name)
                if legacy_info is not None:
                    allowed_without_graph.append(legacy_pending_name)
                    if _entry_stat(capability, LEGACY_PENDING_STATE_FILE) is not None:
                        _validated_legacy_pending_bridge(
                            capability,
                            selected_name=legacy_pending_name,
                        )
                        allowed_without_graph.append(LEGACY_PENDING_STATE_FILE)
            _validate_pristine_or_legacy_graph(
                capability,
                allowed_without_graph=allowed_without_graph,
            )
        if existing_drainer is not None and existing_drainer[1] == "CLOSE_PENDING":
            _finish_close_locked(capability, existing_drainer[2])
            existing_drainer = _read_drainer(capability)
        reserve_drainer = existing_drainer is None or existing_drainer[1] == "complete"
        predecessor_receipt: str | None = None
        if reserve_drainer:
            if existing_drainer is None:
                if _read_transaction(capability) is not None:
                    raise PendingTransactionError(
                        "missing drainer cannot coexist with a live transaction"
                    )
                if protocol is None and not receipt_present:
                    generation = 1
                elif protocol is not None and protocol.get("state") == "COMPLETE":
                    receipt, predecessor_receipt, _inventory = _validate_receipt_locked(
                        capability,
                        allow_missing_completed_drainer=True,
                    )
                    generation = int(receipt["generation"]) + 1
                else:
                    raise PendingTransactionError(
                        "missing drainer has no valid completed predecessor"
                    )
            else:
                receipt, predecessor_receipt, _inventory = _validate_receipt_locked(
                    capability,
                    require_closed=True,
                )
                generation = int(receipt["generation"]) + 1
                raw_generation = existing_drainer[2].get("successor_generation")
                if raw_generation is not None and (
                    type(raw_generation) is not int or raw_generation != generation
                ):
                    raise PendingTransactionError(
                        "close successor generation is malformed"
                    )
            drainer = DrainerTuple(generation, 0, secrets.token_hex(16))
        else:
            if existing_drainer is None:
                raise PendingTransactionError("live drainer disappeared")
            drainer = existing_drainer[0]
        legacy_checkpoint: dict[str, object] | None = None
        legacy_info = (
            None
            if legacy_pending_name is None
            else _entry_stat(capability, legacy_pending_name)
        )
        if legacy_pending_name is not None and legacy_info is not None:
            bridge = _load_json(capability, LEGACY_PENDING_STATE_FILE) or {}
            identity = {"device": legacy_info.st_dev, "inode": legacy_info.st_ino}
            offset = 0
            if bridge.get("identity") == identity:
                raw_offset = bridge.get("offset", 0)
                if isinstance(raw_offset, int) and 0 <= raw_offset <= legacy_info.st_size:
                    offset = raw_offset
            try:
                legacy_bytes = _read_bytes(capability, legacy_pending_name)
                unread = legacy_bytes[offset:]
                complete_length = unread.rfind(b"\n") + 1
                legacy_payload = unread[:complete_length].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PendingTransactionError("legacy pending changes are malformed") from exc
            durable_paths.extend(
                line.strip() for line in legacy_payload.splitlines() if line.strip()
            )
            durable_paths = list(dict.fromkeys(durable_paths))
            legacy_checkpoint = {
                "schema": 1,
                "name": legacy_pending_name,
                "identity": identity,
                "offset": offset + complete_length,
            }
        item = {
            "schema": 1,
            "id": secrets.token_hex(32),
            "kind": kind,
            "intent": intent or kind,
            "root": str(root_path),
            "changed_paths": None if changed_paths is None and not durable_paths else durable_paths,
            "semantic": bool(semantic),
            "source": source,
            "time": time.time() if now is None else now,
        }
        queued = _read_queue(capability)
        if kind == "full":
            item["semantic"] = bool(semantic) or any(
                value.get("root") == item["root"] and value.get("semantic") for value in queued
            )
            queued = [value for value in queued if value.get("root") != item["root"]]
            queued.append(item)
        elif not any(value.get("root") == item["root"] and value.get("kind") == "full" for value in queued):
            queued.append(item)
        _queue_payload(queued)
        if reserve_drainer:
            if existing_drainer is not None and existing_drainer[1] == "complete":
                if protocol is None or predecessor_receipt is None:
                    raise PendingTransactionError(
                        "completed predecessor authority is missing"
                    )
                _write_predecessor_authority(
                    capability,
                    protocol,
                    existing_drainer,
                    predecessor_receipt,
                )
            _write_drainer(
                capability,
                drainer,
                "reserved",
                lease_deadline=(time.time() if now is None else now) + 30,
                predecessor_receipt=predecessor_receipt,
            )
        _write_queue(capability, QUEUE_FILE, queued)
        if legacy_checkpoint is not None:
            _replace_bytes(
                capability,
                LEGACY_PENDING_STATE_FILE,
                _json_bytes(legacy_checkpoint),
            )
        return QueueReceipt(str(item["id"]), drainer)


def claim_rebuild_queue(
    transaction: Transaction,
    drainer: DrainerTuple,
    *,
    now: float | None = None,
    failpoint: Callable[[str], None] | None = None,
) -> RebuildClaim:
    current_time = time.time() if now is None else now
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction)
        current = _read_drainer(capability)
        if current is None or current[0] != drainer:
            raise PendingTransactionError("exact drainer is not live")
        queued = _read_queue(capability)
        accepted = [item for item in queued if item["root"] == live.root]
        quarantined = [item for item in queued if item["root"] != live.root]
        if failpoint:
            failpoint("before_inflight_durable")
        inflight_name = f".graphify_rebuild_inflight.{live.id}.jsonl"
        inflight_path: Path | None = None
        if accepted:
            existing = _read_queue(capability, inflight_name)
            _write_queue(capability, inflight_name, _merge_intents(existing, accepted))
            inflight_path = capability.path / inflight_name
        if failpoint:
            failpoint("before_quarantine_durable")
        if quarantined:
            existing_quarantine = _read_queue(capability, QUARANTINE_FILE)
            _write_queue(
                capability,
                QUARANTINE_FILE,
                _merge_intents(existing_quarantine, quarantined),
            )
        if failpoint:
            failpoint("before_queue_durable")
        _write_queue(capability, QUEUE_FILE, [])
        _write_drainer(
            capability,
            drainer,
            "claimed",
            acked_ids=[],
            lease_deadline=current_time + 30.0,
        )
        live = replace(live, drainer=drainer)
        _write_transaction(capability, live)
        _AUTHORITY.set(_authority_for(live))
        return RebuildClaim(live.id, tuple(accepted), tuple(quarantined), inflight_path, drainer)


def takeover_drainer(
    output: Path | str,
    *,
    now: float | None = None,
    lease_seconds: float = 30.0,
    transition_failpoint: Callable[[str], None] | None = None,
) -> DrainerTuple:
    with pin_output(output) as capability, _locked(capability):
        pending = _read_pending_transition(capability)
        if pending is not None:
            _validate_pending_transition_current(capability, pending)
            raise PendingTransactionError(
                "pending transition requires transaction recovery"
            )
        current = _read_drainer(capability)
        if current is None:
            raise PendingTransactionError("no drainer exists")
        drainer, state, raw = current
        current_time = time.time() if now is None else now
        if state not in {"reserved", "launching", "claimed"}:
            raise PendingTransactionError("drainer state does not permit takeover")
        deadline = float(raw.get("lease_deadline", 0.0))
        if current_time <= deadline:
            raise PendingTransactionError("drainer lease has not expired")
        live = _read_transaction(capability)
        if live is None:
            protocol = _read_protocol(capability)
            receipt_present = _entry_stat(capability, RECEIPT_FILE) is not None
            if state != "reserved":
                raise PendingTransactionError(
                    "tokenless drainer takeover requires an exact reservation"
                )
            if protocol is None and not receipt_present:
                if (
                    drainer.generation != 1
                    or raw.get("predecessor_receipt") is not None
                    or not _read_queue(capability)
                    or _entry_stat(capability, "graph.json") is not None
                ):
                    raise PendingTransactionError(
                        "tokenless drainer reservation is not pristine"
                    )
                allowed = [
                    *_SAFE_GRAPHLESS_RUNTIME_ENTRIES,
                    DRAINER_FILE,
                    QUEUE_FILE,
                ]
                _validate_pristine_or_legacy_graph(
                    capability,
                    allowed_without_graph=allowed,
                )
            elif protocol is not None and protocol.get("state") == "COMPLETE":
                receipt, receipt_digest, _inventory = _validate_receipt_locked(
                    capability
                )
                if (
                    drainer.generation != int(receipt["generation"]) + 1
                    or raw.get("predecessor_receipt") != receipt_digest
                ):
                    raise PendingTransactionError(
                        "tokenless successor reservation is not receipt-bound"
                    )
            else:
                raise PendingTransactionError(
                    "tokenless drainer has incomplete protocol authority"
                )
        successor = DrainerTuple(
            drainer.generation, drainer.claim_epoch + 1, secrets.token_hex(16)
        )
        if live is not None:
            protocol, _bound_drainer = _validate_durable_live_binding(
                capability,
                live,
                drainer=current,
            )
            predecessor_live = live
            _retire_prepared_locked(capability)
            inflight_name = f".graphify_rebuild_inflight.{live.id}.jsonl"
            inflight = _read_queue(capability, inflight_name)
            if inflight:
                _write_queue(
                    capability,
                    QUEUE_FILE,
                    _merge_intents(_read_queue(capability), inflight),
                )
            _unlink(capability, inflight_name)
            successor_id = secrets.token_hex(32)
            token_name = f".graphify_transaction_token.{successor_id}"
            token_payload = _json_bytes(
                {
                    "schema": 1,
                    "id": successor_id,
                    "root": live.root,
                    "output": str(live.output),
                    "generation": live.generation,
                    "drainer": _drainer_json(successor),
                    "secret": secrets.token_hex(32),
                }
            )
            token_identity = _create_bytes(capability, token_name, token_payload)
            live = replace(
                live,
                id=successor_id,
                token_digest=hashlib.sha256(token_payload).hexdigest(),
                token_identity=token_identity,
                drainer=successor,
            )
            successor_protocol = dict(protocol)
            successor_protocol.update(
                transaction_id=live.id,
                owner_capability_digest=live.token_digest,
                token_identity={
                    "device": token_identity[0],
                    "inode": token_identity[1],
                },
                lease_deadline=current_time + lease_seconds,
            )
            _write_pending_transition(
                capability,
                predecessor_drainer=(drainer, state),
                predecessor_protocol=protocol,
                predecessor_transaction=predecessor_live,
                successor=live,
                successor_protocol=successor_protocol,
            )
            if transition_failpoint is not None:
                transition_failpoint("after_successor_token")
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(successor_protocol))
            if transition_failpoint is not None:
                transition_failpoint("after_owner_protocol")
            _write_transaction(capability, live, phase="awaiting-drainer")
            if transition_failpoint is not None:
                transition_failpoint("after_transaction")
        _transition_drainer(
            capability,
            expected=(drainer, state),
            drainer=successor,
            state="reserved",
            failpoint=transition_failpoint,
            lease_deadline=current_time + lease_seconds,
        )
        if live is not None:
            _transition_drainer(
                capability,
                expected=(successor, "reserved"),
                drainer=successor,
                state="launching",
                failpoint=transition_failpoint,
                lease_deadline=current_time + lease_seconds,
            )
            _transition_drainer(
                capability,
                expected=(successor, "launching"),
                drainer=successor,
                state="claimed",
                failpoint=transition_failpoint,
                acked_ids=[],
                lease_deadline=current_time + lease_seconds,
            )
            _write_transaction(capability, live, phase="building")
            if predecessor_live.token_identity is not None:
                _unlink(
                    capability,
                    f".graphify_transaction_token.{predecessor_live.id}",
                    expected=predecessor_live.token_identity,
                )
            _unlink(capability, TRANSITION_FILE)
        return successor


def complete_rebuild_claim(
    transaction: Transaction,
    claim: RebuildClaim,
    *,
    receipt_digest: str,
    now: float | None = None,
) -> None:
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction, allow_complete=True)
        receipt, validated_digest, _inventory = _validate_receipt_locked(capability)
        if validated_digest != receipt_digest:
            raise PendingTransactionError("generation receipt is required before claim acknowledgement")
        expected_name = f".graphify_rebuild_inflight.{live.id}.jsonl"
        expected_path = capability.path / expected_name
        expected_claim_path = expected_path if claim.items else None
        if (
            claim.transaction_id != live.id
            or claim.drainer != live.drainer
            or claim.inflight_path != expected_claim_path
            or receipt.get("transaction_id") != live.id
            or receipt.get("generation") != live.generation
            or receipt.get("token_digest") != live.token_digest
            or _drainer_from_json(receipt.get("drainer")) != claim.drainer
        ):
            raise PendingTransactionError("claim is not bound to the exact live inflight record")
        current = _read_drainer(capability)
        if current is None or current[0] != claim.drainer or current[1] != "claimed":
            raise PendingTransactionError("claim drainer no longer matches")
        inflight = _read_queue(capability, expected_name)
        claimed_by_id = {str(item["id"]): item for item in claim.items}
        inflight_by_id = {str(item["id"]): item for item in inflight}
        if (
            len(claimed_by_id) != len(claim.items)
            or any(inflight_by_id.get(item_id) != item for item_id, item in claimed_by_id.items())
            or (not claim.items and inflight)
        ):
            raise PendingTransactionError("claim does not match durable inflight work")
        ids = list(claimed_by_id)
        residual = [item for item in inflight if str(item["id"]) not in claimed_by_id]
        if residual:
            _write_queue(capability, expected_name, residual)
        elif claim.items:
            _unlink(capability, expected_name)
        current_time = time.time() if now is None else now
        _write_drainer(
            capability,
            claim.drainer,
            "claimed",
            acked_ids=ids,
            receipt_digest=receipt_digest,
            lease_deadline=current_time + 30.0,
        )


def close_if_queue_empty(
    transaction: Transaction,
    *,
    receipt_digest: str,
    failpoint: Callable[[str], None] | None = None,
) -> bool:
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction, allow_complete=True)
        if _read_queue(capability):
            return False
        current = _read_drainer(capability)
        if current is None:
            raise PendingTransactionError("durable claimed drainer authority is required")
        drainer, state, raw = current
        if state != "claimed":
            raise PendingTransactionError("drainer is not claimable for close")
        acked = [str(value) for value in raw.get("acked_ids", [])]
        if raw.get("receipt_digest") != receipt_digest:
            raise PendingTransactionError("close receipt does not match acknowledged work")
        pending = {
            "schema": 1,
            "protocol_epoch": 1,
            **_drainer_json(drainer),
            "state": "CLOSE_PENDING",
            "receipt_digest": receipt_digest,
            "acked_ids": acked,
            "queue_epoch": drainer.generation,
            "output_identity": capability.identity.json(),
            "successor_generation": drainer.generation + 1,
            "transaction_id": live.id,
            "token_identity": (
                None
                if live.token_identity is None
                else {"device": live.token_identity[0], "inode": live.token_identity[1]}
            ),
        }
        _replace_bytes(capability, DRAINER_FILE, _json_bytes(pending))
        if failpoint:
            failpoint("after_close_pending")
        _finish_close_locked(capability, pending, failpoint=failpoint)
        complete_protocol = _read_protocol(capability)
        complete_drainer = _read_drainer(capability)
        if complete_protocol is None or complete_drainer is None:
            raise PendingTransactionError("completed predecessor authority disappeared")
        _write_predecessor_authority(
            capability,
            complete_protocol,
            complete_drainer,
            receipt_digest,
        )
        return True


def _validate_close_pending_locked(
    capability: OutputCapability,
    pending: dict[str, Any],
) -> Transaction | None:
    if (
        pending.get("schema") != 1
        or pending.get("protocol_epoch") != 1
        or pending.get("state") != "CLOSE_PENDING"
        or _identity_from_json(pending.get("output_identity")) != capability.identity
        or not isinstance(pending.get("transaction_id"), str)
        or not isinstance(pending.get("receipt_digest"), str)
        or len(str(pending["receipt_digest"])) != 64
    ):
        raise PendingTransactionError("malformed close-pending authority")
    pending_drainer = _drainer_from_json(pending)
    live = _read_transaction(capability)
    if live is not None and live.id != pending["transaction_id"]:
        raise PendingTransactionError("close-pending transaction identity changed")
    token_identity_raw = pending.get("token_identity")
    if token_identity_raw is None:
        pending_token_identity = None
    elif (
        isinstance(token_identity_raw, dict)
        and type(token_identity_raw.get("device")) is int
        and type(token_identity_raw.get("inode")) is int
    ):
        pending_token_identity = (
            token_identity_raw["device"],
            token_identity_raw["inode"],
        )
    else:
        raise PendingTransactionError("close-pending token identity is malformed")
    token_name = f".graphify_transaction_token.{pending['transaction_id']}"
    token_info = _entry_stat(capability, token_name)
    if live is not None:
        if pending_token_identity != live.token_identity:
            raise PendingTransactionError("close-pending token identity changed")
        if token_info is not None and (
            live.token_identity is None
            or (token_info.st_dev, token_info.st_ino) != live.token_identity
        ):
            raise PendingTransactionError("close-pending token file was replaced")
    elif any(
        name.startswith(".graphify_transaction_token.")
        for name in _list_entries(capability)
    ):
        raise PendingTransactionError("unexpected token remains after owner retirement")
    inflight_name = f".graphify_rebuild_inflight.{pending['transaction_id']}.jsonl"
    if _entry_stat(capability, inflight_name) is not None:
        raise PendingTransactionError("close-pending inflight work was recreated")
    receipt, receipt_digest, _inventory = _validate_receipt_locked(
        capability,
        transaction=live,
    )
    if (
        receipt_digest != pending["receipt_digest"]
        or receipt.get("transaction_id") != pending["transaction_id"]
        or _drainer_from_json(receipt.get("drainer")) != pending_drainer
    ):
        raise PendingTransactionError("close-pending receipt authority changed")
    return live


def _finish_close_locked(
    capability: OutputCapability,
    pending: dict[str, Any],
    *,
    failpoint: Callable[[str], None] | None = None,
) -> None:
    if pending.get("state") != "CLOSE_PENDING":
        return
    live = _validate_close_pending_locked(capability, pending)
    transaction_id = str(pending["transaction_id"])
    if failpoint:
        failpoint("after_inflight_remove")
    if live is not None and live.id == transaction_id:
        token_name = f".graphify_transaction_token.{transaction_id}"
        token_identity_raw = pending.get("token_identity")
        expected = None
        if isinstance(token_identity_raw, dict):
            expected = (int(token_identity_raw["device"]), int(token_identity_raw["inode"]))
        _unlink(capability, token_name, expected=expected)
        if failpoint:
            failpoint("after_token_unlink")
        _unlink(capability, TRANSACTION_FILE)
        if failpoint:
            failpoint("after_live_remove")
    complete = dict(pending)
    complete["state"] = "complete"
    _replace_bytes(capability, DRAINER_FILE, _json_bytes(complete))
    if failpoint:
        failpoint("after_complete")


def recover_close(output: Path | str) -> None:
    with pin_output(output) as capability, _locked(capability):
        current = _read_drainer(capability)
        if current is not None and current[1] == "CLOSE_PENDING":
            _finish_close_locked(capability, current[2])
            current = _read_drainer(capability)
        if current is not None and current[1] == "complete" and _read_queue(capability):
            receipt, predecessor_receipt, _inventory = _validate_receipt_locked(
                capability,
                require_closed=True,
            )
            protocol = _read_protocol(capability)
            if protocol is None:
                raise PendingTransactionError("completed predecessor protocol is missing")
            _write_predecessor_authority(
                capability,
                protocol,
                current,
                predecessor_receipt,
            )
            successor_generation = int(receipt["generation"]) + 1
            raw_generation = current[2].get("successor_generation")
            if raw_generation is not None and (
                type(raw_generation) is not int
                or raw_generation != successor_generation
            ):
                raise PendingTransactionError("close successor generation is malformed")
            successor = DrainerTuple(successor_generation, 0, secrets.token_hex(16))
            _write_drainer(
                capability,
                successor,
                "reserved",
                lease_deadline=time.time() + 30.0,
                predecessor_receipt=predecessor_receipt,
            )


def recover_selected_transaction(
    kind: TransactionKind | None,
    root: Path | str,
    *,
    output: Path | str,
    expected_generation: int,
    expected_output_identity: OutputIdentity,
    expected_transaction_id: str | None = None,
    now: float | None = None,
    max_attempts: int = 3,
) -> Transaction:
    """Validate exact selectors before any close or recovery mutation."""
    root_path = _canonical_directory(Path(root))
    cancellation_successor: Transaction | None = None
    with pin_output(output) as capability, _locked(capability):
        if capability.identity != expected_output_identity:
            raise PendingTransactionError("stale output identity selector")
        cancellation = _read_predecessor_authority(capability)
        if cancellation is not None and cancellation[3]["state"] != "preserved-complete":
            successor = _validate_cancellation_state_locked(capability, cancellation)
            if (
                successor.generation != expected_generation
                or successor.root != str(root_path)
                or (kind is not None and successor.kind != kind)
                or (
                    expected_transaction_id is not None
                    and successor.id != expected_transaction_id
                )
            ):
                raise PendingTransactionError("stale cancellation recovery selector")
            cancellation_successor = successor
    if cancellation_successor is not None:
        cancel_unpublished_transaction(cancellation_successor)
        return _cancellation_recovery(cancellation_successor)
    with pin_output(output) as capability, _locked(capability):
        if capability.identity != expected_output_identity:
            raise PendingTransactionError("stale output identity selector")
        current = _read_transaction(capability)
        pending = _read_pending_transition(capability)
        if pending is not None:
            _validate_pending_transition_current(capability, pending)
        pending_successor = None if pending is None else pending[3]
        protocol = _read_protocol(capability)
        drainer = _read_drainer(capability)
        if pending is None and current is not None:
            _validate_durable_live_binding(
                capability,
                current,
                protocol=protocol,
                drainer=drainer,
            )
        queue = _read_queue(capability)
        matching_queue = [
            item for item in queue if item.get("root") == str(root_path)
        ]
        selected_generation = (
            pending_successor.generation
            if pending_successor is not None
            else current.generation
            if current is not None
            else int((protocol or {}).get("generation", -1))
        )
        if selected_generation != expected_generation:
            raise PendingTransactionError("stale transaction generation selector")
        selected_transaction_id = (
            pending_successor.id
            if pending_successor is not None
            else current.id
            if current is not None
            else (
                str(drainer[2].get("transaction_id"))
                if drainer is not None
                and isinstance(drainer[2].get("transaction_id"), str)
                else None
            )
        )
        if (
            expected_transaction_id is not None
            and selected_transaction_id != expected_transaction_id
        ):
            raise PendingTransactionError("stale transaction id selector")
        if (
            pending_successor is None
            and current is None
            and protocol is not None
            and protocol.get("state") == "COMPLETE"
            and not queue
        ):
            raise PendingTransactionError("completed generation has no recoverable work")
        durable_kind: TransactionKind | None = (
            pending_successor.kind
            if pending_successor is not None
            else current.kind
            if current is not None
            else None
        )
        if durable_kind is None and queue:
            if not matching_queue:
                raise PendingTransactionError(
                    "durable rebuild queue has no intent for the selected root"
                )
            kinds = {str(item["kind"]) for item in matching_queue}
            durable_kind = (
                "full" if "full" in kinds else "update" if "update" in kinds else "runtime"
            )
        if (
            durable_kind is None
            and protocol is not None
            and protocol.get("state") == "BOOTSTRAP_PENDING"
            and protocol.get("kind") in {"full", "update", "runtime"}
        ):
            durable_kind = str(protocol["kind"])  # type: ignore[assignment]
        if durable_kind is None:
            raise PendingTransactionError("completed generation has no recoverable work")
        if kind is not None and kind != durable_kind:
            raise PendingTransactionError("selected transaction kind does not match durable state")
        if pending_successor is not None:
            return recover_transaction(
                durable_kind,
                root_path,
                output=output,
                now=now,
                max_attempts=max_attempts,
                expected_transaction_id=expected_transaction_id,
                expected_generation=expected_generation,
                expected_output_identity=expected_output_identity,
            )
        recover_close(output)
        if _read_transaction(capability) is None:
            if not queue:
                raise PendingTransactionError("completed generation has no recoverable work")
            return begin_transaction(durable_kind, root_path, output=output, now=now)
        return recover_transaction(
            durable_kind,
            root_path,
            output=output,
            now=now,
            max_attempts=max_attempts,
            expected_transaction_id=expected_transaction_id,
            expected_generation=expected_generation,
            expected_output_identity=expected_output_identity,
        )


def recover_transaction(
    kind: TransactionKind,
    root: Path | str,
    *,
    output: Path | str = "graphify-out",
    now: float | None = None,
    max_attempts: int = 3,
    expected_transaction_id: str | None = None,
    expected_generation: int | None = None,
    expected_output_identity: OutputIdentity | None = None,
    transition_failpoint: Callable[[str], None] | None = None,
) -> Transaction:
    if max_attempts <= 0:
        raise RecoverableTransactionError("recovery attempt bound exhausted")
    root_path = _canonical_directory(Path(root))
    cancellation_successor: Transaction | None = None
    with pin_output(output) as capability, _locked(capability):
        if (
            expected_output_identity is not None
            and capability.identity != expected_output_identity
        ):
            raise PendingTransactionError("stale output identity selector")
        cancellation = _read_predecessor_authority(capability)
        if cancellation is not None and cancellation[3]["state"] != "preserved-complete":
            successor = _validate_cancellation_state_locked(capability, cancellation)
            if (
                successor.root != str(root_path)
                or successor.kind != kind
                or (
                    expected_generation is not None
                    and successor.generation != expected_generation
                )
                or (
                    expected_transaction_id is not None
                    and successor.id != expected_transaction_id
                )
            ):
                raise PendingTransactionError("stale cancellation recovery selector")
            cancellation_successor = successor
    if cancellation_successor is not None:
        cancel_unpublished_transaction(cancellation_successor)
        return _cancellation_recovery(cancellation_successor)
    with pin_output(output) as capability, _locked(capability):
        current = _read_transaction(capability)
        pending = _read_pending_transition(capability)
        selected = current if pending is None else pending[3]
        if expected_output_identity is not None and capability.identity != expected_output_identity:
            raise PendingTransactionError("stale output identity selector")
        selected_generation = (
            selected.generation
            if selected is not None
            else int((_read_protocol(capability) or {}).get("generation", -1))
        )
        if expected_generation is not None and selected_generation != expected_generation:
            raise PendingTransactionError("stale transaction generation selector")
        if expected_transaction_id is not None and (
            selected is None or selected.id != expected_transaction_id
        ):
            raise PendingTransactionError("stale transaction id selector")
        if pending is not None:
            _validate_pending_transition_current(capability, pending)
            predecessor, predecessor_protocol, predecessor_tx, successor, successor_protocol = pending
            if successor.root != str(root_path) or successor.kind != kind:
                raise PendingTransactionError("recovery root or kind does not match pending transition")
            protocol = _read_protocol(capability)
            if protocol == predecessor_protocol:
                _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(successor_protocol))
                if transition_failpoint is not None:
                    transition_failpoint("after_recovery_protocol")
            elif protocol != successor_protocol:
                raise PendingTransactionError("pending transition protocol predecessor changed")
            live = _read_transaction(capability)
            if live is None or live == predecessor_tx:
                _write_transaction(capability, successor, phase="awaiting-drainer")
                if transition_failpoint is not None:
                    transition_failpoint("after_transaction")
            elif live != successor:
                raise PendingTransactionError("pending transition transaction predecessor changed")
            drainer_current = _read_drainer(capability)
            if drainer_current is None:
                if predecessor is not None:
                    raise PendingTransactionError("pending transition drainer predecessor disappeared")
                _transition_drainer(
                    capability,
                    expected=None,
                    drainer=successor.drainer,
                    state="reserved",
                    failpoint=transition_failpoint,
                    lease_deadline=(time.time() if now is None else now) + 30.0,
                )
                drainer_current = _read_drainer(capability)
            elif predecessor is not None and drainer_current[:2] == predecessor:
                _transition_drainer(
                    capability,
                    expected=predecessor,
                    drainer=successor.drainer,
                    state="reserved",
                    failpoint=transition_failpoint,
                    lease_deadline=(time.time() if now is None else now) + 30.0,
                )
                drainer_current = _read_drainer(capability)
            if drainer_current is None or drainer_current[0] != successor.drainer:
                raise PendingTransactionError("pending transition drainer binding changed")
            if drainer_current[1] == "reserved":
                _transition_drainer(
                    capability,
                    expected=(successor.drainer, "reserved"),
                    drainer=successor.drainer,
                    state="launching",
                    failpoint=transition_failpoint,
                    lease_deadline=(time.time() if now is None else now) + 30.0,
                )
                drainer_current = _read_drainer(capability)
            if drainer_current is not None and drainer_current[1] == "launching":
                _transition_drainer(
                    capability,
                    expected=(successor.drainer, "launching"),
                    drainer=successor.drainer,
                    state="claimed",
                    failpoint=transition_failpoint,
                    acked_ids=[],
                    lease_deadline=(time.time() if now is None else now) + 30.0,
                )
            claimed = _read_drainer(capability)
            if claimed is None or claimed[:2] != (successor.drainer, "claimed"):
                raise PendingTransactionError("pending transition did not reach exact claimed state")
            successor = replace(successor, phase="building")
            _write_transaction(capability, successor, phase="building")
            if predecessor_tx is not None and predecessor_tx.token_identity is not None and predecessor_tx.id != successor.id:
                _unlink(
                    capability,
                    f".graphify_transaction_token.{predecessor_tx.id}",
                    expected=predecessor_tx.token_identity,
                )
            _unlink(capability, TRANSITION_FILE)
            _AUTHORITY.set(_authority_for(successor))
            return successor
        if current is None:
            protocol = _read_protocol(capability)
            if protocol is None or protocol.get("state") != "BOOTSTRAP_PENDING":
                raise PendingTransactionError("no live transaction to recover")
            current_time = time.time() if now is None else now
            if current_time <= float(protocol.get("lease_deadline", 0.0)):
                raise PendingTransactionError("bootstrap lease has not expired")
            if (
                protocol.get("schema") != 1
                or protocol.get("protocol_epoch") != 1
                or _identity_from_json(protocol.get("output_identity"))
                != capability.identity
            ):
                raise PendingTransactionError("bootstrap protocol binding is malformed")
            if protocol.get("kind") not in {None, kind} or protocol.get("root") not in {
                None,
                str(root_path),
            }:
                raise PendingTransactionError("bootstrap kind or root binding changed")
            generation = int(protocol.get("generation", 0))
            claim_epoch = int(protocol.get("bootstrap_claim_epoch", 0)) + 1
            if claim_epoch > max_attempts:
                raise RecoverableTransactionError("recovery attempt bound exhausted")
            secret = secrets.token_bytes(32)
            token_digest = hashlib.sha256(secret).hexdigest()
            drainer = DrainerTuple(generation, claim_epoch, secrets.token_hex(16))
            predecessor_protocol = dict(protocol)
            protocol.update(
                bootstrap_claim_epoch=claim_epoch,
                bootstrap_nonce=secrets.token_hex(16),
                owner_capability_digest=token_digest,
                lease_deadline=current_time + 30.0,
            )
            tx = Transaction(
                secrets.token_hex(32),
                kind,
                str(root_path),
                capability.path,
                capability.identity,
                generation,
                token_digest,
                None,
                drainer,
            )
            successor_protocol = dict(protocol)
            successor_protocol.update(
                state="INCOMPLETE",
                transaction_id=tx.id,
                token_identity=None,
            )
            existing_drainer = _read_drainer(capability)
            _write_pending_transition(
                capability,
                predecessor_drainer=(
                    None
                    if existing_drainer is None
                    else (existing_drainer[0], existing_drainer[1])
                ),
                predecessor_protocol=predecessor_protocol,
                predecessor_transaction=None,
                successor=tx,
                successor_protocol=successor_protocol,
            )
            if transition_failpoint is not None:
                transition_failpoint("after_transition_record")
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(successor_protocol))
            if transition_failpoint is not None:
                transition_failpoint("after_recovery_protocol")
            _write_transaction(capability, tx, phase="awaiting-drainer")
            if transition_failpoint is not None:
                transition_failpoint("after_transaction")
            _transition_drainer(
                capability,
                expected=(
                    None
                    if existing_drainer is None
                    else (existing_drainer[0], existing_drainer[1])
                ),
                drainer=drainer,
                state="reserved",
                failpoint=transition_failpoint,
                lease_deadline=current_time + 30.0,
            )
            _transition_drainer(
                capability,
                expected=(drainer, "reserved"),
                drainer=drainer,
                state="launching",
                failpoint=transition_failpoint,
                lease_deadline=current_time + 30.0,
            )
            _transition_drainer(
                capability,
                expected=(drainer, "launching"),
                drainer=drainer,
                state="claimed",
                failpoint=transition_failpoint,
                acked_ids=[],
                lease_deadline=current_time + 30.0,
            )
            _write_transaction(capability, tx, phase="building")
            _unlink(capability, TRANSITION_FILE)
            _AUTHORITY.set(_authority_for(tx))
            return tx
        protocol = _read_protocol(capability)
        if protocol is None:
            raise PendingTransactionError("protocol state is missing")
        drainer_current = _read_drainer(capability)
        protocol, drainer_current = _validate_durable_live_binding(
            capability,
            current,
            protocol=protocol,
            drainer=drainer_current,
        )
        current_time = time.time() if now is None else now
        if current_time <= float(protocol.get("lease_deadline", 0.0)):
            raise PendingTransactionError("transaction lease has not expired")
        if current.root != str(root_path):
            raise PendingTransactionError("recovery root does not match live transaction")
        phase = _transaction_phase(capability)
        if phase == "awaiting-drainer":
            raise PendingTransactionError("awaiting drainer is missing its exact transition record")
        if drainer_current is None or drainer_current[0] != current.drainer:
            raise PendingTransactionError("exact live drainer is required for recovery")
        if drainer_current[1] in {"reserved", "launching"}:
            if drainer_current[1] == "reserved":
                _transition_drainer(
                    capability,
                    expected=(current.drainer, "reserved"),
                    drainer=current.drainer,
                    state="launching",
                    failpoint=transition_failpoint,
                    lease_deadline=current_time + 30.0,
                )
            _transition_drainer(
                capability,
                expected=(current.drainer, "launching"),
                drainer=current.drainer,
                state="claimed",
                failpoint=transition_failpoint,
                acked_ids=[],
                lease_deadline=current_time + 30.0,
            )
            protocol.update(state="INCOMPLETE", transaction_id=current.id)
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
            _write_transaction(capability, current, phase="building")
            _AUTHORITY.set(_authority_for(current))
            return current
        if drainer_current[1] != "claimed":
            raise PendingTransactionError("exact claimed drainer is required for recovery")
        claim_epoch = drainer_current[0].claim_epoch + 1
        if claim_epoch > max_attempts:
            raise RecoverableTransactionError("recovery attempt bound exhausted")
        queued = _read_queue(capability)
        for name in _list_entries(capability):
            if name.startswith(".graphify_rebuild_inflight.") and name.endswith(".jsonl"):
                queued.extend(_read_queue(capability, name))
        if queued:
            deduplicated = {str(item["id"]): item for item in queued}
            _write_queue(capability, QUEUE_FILE, list(deduplicated.values()))
        for name in list(_list_entries(capability)):
            if name.startswith(".graphify_rebuild_inflight.") and name.endswith(".jsonl"):
                _unlink(capability, name)
        _retire_prepared_locked(capability)
        generation = current.generation + 1
        drainer = DrainerTuple(generation, claim_epoch, secrets.token_hex(16))
        secret = secrets.token_bytes(32)
        tx = Transaction(
            secrets.token_hex(32),
            kind,
            str(root_path),
            capability.path,
            capability.identity,
            generation,
            hashlib.sha256(secret).hexdigest(),
            None,
            drainer,
        )
        predecessor_protocol = dict(protocol)
        successor_protocol = dict(protocol)
        successor_protocol.update(
            schema=1,
            protocol_epoch=1,
            generation=generation,
            kind=kind,
            root=str(root_path),
            state="INCOMPLETE",
            transaction_id=tx.id,
            output_identity=capability.identity.json(),
            owner_capability_digest=tx.token_digest,
            token_identity=None,
            lease_deadline=(time.time() if now is None else now) + 30,
        )
        _write_pending_transition(
            capability,
            predecessor_drainer=(drainer_current[0], "claimed"),
            predecessor_protocol=predecessor_protocol,
            predecessor_transaction=current,
            successor=tx,
            successor_protocol=successor_protocol,
        )
        if transition_failpoint is not None:
            transition_failpoint("after_transition_record")
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(successor_protocol))
        if transition_failpoint is not None:
            transition_failpoint("after_recovery_protocol")
        _write_transaction(capability, tx, phase="awaiting-drainer")
        if transition_failpoint is not None:
            transition_failpoint("after_transaction")
        _transition_drainer(
            capability,
            expected=(drainer_current[0], "claimed"),
            drainer=drainer,
            state="reserved",
            failpoint=transition_failpoint,
            lease_deadline=current_time + 30.0,
        )
        _transition_drainer(
            capability,
            expected=(drainer, "reserved"),
            drainer=drainer,
            state="launching",
            failpoint=transition_failpoint,
            lease_deadline=current_time + 30.0,
        )
        _transition_drainer(
            capability,
            expected=(drainer, "launching"),
            drainer=drainer,
            state="claimed",
            failpoint=transition_failpoint,
            acked_ids=[],
            lease_deadline=current_time + 30.0,
        )
        _write_transaction(capability, tx, phase="building")
        if current.token_identity is not None:
            _unlink(
                capability,
                f".graphify_transaction_token.{current.id}",
                expected=current.token_identity,
            )
        _unlink(capability, TRANSITION_FILE)
        _AUTHORITY.set(_authority_for(tx))
        return tx


def transaction_status(output: Path | str = "graphify-out") -> dict[str, Any]:
    """Return validated operational state without exposing capability material."""
    with pin_output(output, mutation=False) as capability, _locked(capability):
        protocol = _read_protocol(capability)
        live = _read_transaction(capability)
        pending = _read_pending_transition(capability)
        cancellation = _read_predecessor_authority(capability)
        cancellation_successor: Transaction | None = None
        cancellation_state: str | None = None
        if cancellation is not None and cancellation[3]["state"] != "preserved-complete":
            cancellation_successor = _validate_cancellation_state_locked(
                capability, cancellation
            )
            cancellation_state = str(cancellation[3]["state"])
        if pending is not None:
            _validate_pending_transition_current(capability, pending)
        drainer = _read_drainer(capability)
        queue = _read_queue(capability)
        quarantine = _read_queue(capability, QUARANTINE_FILE)
        inflight: dict[str, list[str]] = {}
        for name in sorted(_list_entries(capability)):
            if name.startswith(".graphify_rebuild_inflight.") and name.endswith(".jsonl"):
                inflight[name] = [str(item["id"]) for item in _read_queue(capability, name)]
        retained: list[dict[str, object]] = []
        journal_identities: set[OutputIdentity] = set()
        with pin_output(capability.path.parent, mutation=False) as parent:
            for name in sorted(_list_entries(parent)):
                if name.startswith(".graphify-gc-journal-") and name.endswith(".json"):
                    if not _is_gc_journal(name):
                        raise PendingTransactionError(
                            "retired workspace GC journal name is malformed"
                        )
                    raw_journal = _load_json(parent, name)
                    if raw_journal is None or raw_journal.get(
                        "managed_output_identity"
                    ) != capability.identity.json():
                        continue
                    tombstone = raw_journal.get("tombstone")
                    if not isinstance(tombstone, str):
                        raise PendingTransactionError(
                            "retired workspace GC journal is malformed"
                        )
                    workspace_identity = _identity_from_json(
                        raw_journal.get("workspace_identity")
                    )
                    journal = _validated_gc_journal(
                        raw_journal,
                        tombstone=tombstone,
                        workspace_identity=workspace_identity,
                        managed_output_identity=capability.identity,
                    )
                    if journal is None:
                        continue
                    expected_journal_name = _gc_journal_name(
                        capability.identity, tombstone
                    )
                    if name != expected_journal_name:
                        raise PendingTransactionError(
                            "retired workspace GC journal selector changed"
                        )
                    current_name = _gc_journal_location(
                        parent, journal, workspace_identity
                    )
                    retained.append(
                        {
                            "name": tombstone,
                            "current_name": current_name,
                            "state": f"gc_{journal['state']}",
                            "identity": workspace_identity.json(),
                        }
                    )
                    journal_identities.add(workspace_identity)
                    continue
                if not name.startswith((".graphify-retired-", ".graphify-gc-root-")):
                    continue
                info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                identity = OutputIdentity(info.st_dev, info.st_ino)
                if identity in journal_identities:
                    continue
                with pin_output(parent.path / name, mutation=False) as retired:
                    marker = _load_json(retired, ".graphify_retired.json")
                bound = _validated_retired_marker(
                    marker,
                    candidate_name=name,
                    candidate_identity=identity,
                    managed_output_identity=capability.identity,
                )
                if bound is None:
                    continue
                retained.append(
                    {
                        "name": str(bound["tombstone"]),
                        "current_name": name,
                        "state": str(bound["state"]),
                        "identity": identity.json(),
                    }
                )
        receipt_digest = None
        if _entry_stat(capability, RECEIPT_FILE) is not None:
            receipt_digest = hashlib.sha256(
                _read_bytes(capability, RECEIPT_FILE)
            ).hexdigest()
        return {
            "schema": 1,
            "protocol_epoch": 1,
            "output": str(capability.path),
            "output_identity": capability.identity.json(),
            "protocol_state": None if protocol is None else protocol.get("state"),
            "generation": (
                None if protocol is None else protocol.get("generation")
            ),
            "transaction": (
                None
                if live is None
                else {"id": live.id, "kind": live.kind, "root": live.root}
            ),
            "pending_transition": (
                None
                if pending is None
                else {
                    "state": "pending",
                    "transaction_id": pending[3].id,
                    "generation": pending[3].generation,
                    "output_identity": pending[3].output_identity.json(),
                }
            ),
            "cancellation_transition": (
                None
                if cancellation_successor is None
                else {
                    "state": cancellation_state,
                    "transaction_id": cancellation_successor.id,
                    "generation": cancellation_successor.generation,
                    "kind": cancellation_successor.kind,
                    "root": cancellation_successor.root,
                    "output_identity": cancellation_successor.output_identity.json(),
                }
            ),
            "drainer": (
                None
                if drainer is None
                else {
                    "generation": drainer[0].generation,
                    "claim_epoch": drainer[0].claim_epoch,
                    "state": drainer[1],
                }
            ),
            "queue": {"count": len(queue), "ids": [str(item["id"]) for item in queue]},
            "inflight": inflight,
            "quarantine": {
                "count": len(quarantine),
                "ids": [str(item["id"]) for item in quarantine],
            },
            "receipt_digest": receipt_digest,
            "retained_workspaces": retained,
        }


def _load_detached_merge_snapshot_with_identity(
    path: Path | str, *, role: str
) -> tuple[dict[str, Any], tuple[int, int]]:
    if role not in {"ancestor", "current", "other"}:
        raise PendingTransactionError("invalid detached merge snapshot role")
    target = Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > _DETACHED_MAX_BYTES:
                raise PendingTransactionError("unsafe detached merge snapshot")
            payload = bytearray()
            while len(payload) <= _DETACHED_MAX_BYTES:
                chunk = os.read(fd, min(65536, _DETACHED_MAX_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > _DETACHED_MAX_BYTES:
                raise PendingTransactionError("unsafe detached merge snapshot")
            named = target.stat(follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise PendingTransactionError("detached merge snapshot identity changed")
        finally:
            os.close(fd)
        data = json.loads(bytes(payload).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("malformed detached merge snapshot") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("links", []), list):
        raise PendingTransactionError("malformed detached merge snapshot shape")
    if len(data["nodes"]) > _DETACHED_MAX_NODES:
        raise PendingTransactionError("unsafe detached merge snapshot node count")
    metadata = data.get("graph")
    watermark = metadata.get(GRAPH_WATERMARK_KEY) if isinstance(metadata, dict) else None
    if watermark is not None and (
        not isinstance(watermark, dict)
        or watermark.get("schema") != 1
        or watermark.get("protocol_epoch") != 1
    ):
        raise PendingTransactionError("unsupported detached watermark schema")
    if isinstance(watermark, dict):
        state = watermark.get("state")
        if state == "active":
            supported = isinstance(watermark.get("generation"), int)
        elif state == "merge_pending":
            supported = isinstance(watermark.get("snapshot_generation"), str) and isinstance(
                watermark.get("input_digests"), list
            )
        else:
            supported = False
        if not supported:
            raise PendingTransactionError("unsupported detached watermark shape")
    return data, (opened.st_dev, opened.st_ino)


def load_detached_merge_snapshot(path: Path | str, *, role: str) -> dict[str, Any]:
    return _load_detached_merge_snapshot_with_identity(path, role=role)[0]


def merge_detached_snapshots(
    ancestor: Path | str, current: Path | str, other: Path | str
) -> None:
    ancestor_snapshot, _ancestor_identity = _load_detached_merge_snapshot_with_identity(
        ancestor, role="ancestor"
    )
    current_snapshot, current_identity = _load_detached_merge_snapshot_with_identity(
        current, role="current"
    )
    other_snapshot, _other_identity = _load_detached_merge_snapshot_with_identity(
        other, role="other"
    )
    snapshots = [ancestor_snapshot, current_snapshot, other_snapshot]
    digests = [
        hashlib.sha256(_json_bytes(snapshot)).hexdigest() for snapshot in snapshots
    ]
    try:
        current_graph = nx.node_link_graph(snapshots[1], edges="links")
        other_graph = nx.node_link_graph(snapshots[2], edges="links")
        merged_graph = nx.compose(current_graph, other_graph)
        if merged_graph.number_of_nodes() > _DETACHED_MAX_NODES:
            raise PendingTransactionError(
                "composed detached merge exceeds node count limit"
            )
        merged = nx.node_link_data(merged_graph, edges="links")
    except (KeyError, TypeError, nx.NetworkXError) as exc:
        raise PendingTransactionError("malformed detached merge graph") from exc
    graph_meta = dict(merged.get("graph") or {})
    graph_meta[GRAPH_WATERMARK_KEY] = {
        "schema": 1,
        "protocol_epoch": 1,
        "state": "merge_pending",
        "snapshot_generation": hashlib.sha256("".join(digests).encode()).hexdigest(),
        "input_digests": digests,
    }
    merged["graph"] = graph_meta
    merged_payload = json.dumps(merged, sort_keys=True).encode("utf-8")
    if len(merged_payload) > _DETACHED_MAX_BYTES:
        raise PendingTransactionError("composed detached merge exceeds size limit")
    current_path = Path(current).absolute()
    with pin_output(current_path.parent) as capability, _locked(capability):
        current_entry = _entry_stat(capability, current_path.name)
        if current_entry is None or (
            current_entry.st_dev,
            current_entry.st_ino,
        ) != current_identity:
            raise PendingTransactionError("detached current snapshot identity changed")
        _replace_bytes(
            capability,
            current_path.name,
            merged_payload,
            expected_identity=current_identity,
        )


def finish_transaction(transaction: Transaction) -> None:
    """Close a direct transaction after a committed receipt."""
    lease_deadline = time.time() + 30.0
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction, allow_complete=True)
        _receipt, receipt_digest, _inventory = _validate_receipt_locked(
            capability, transaction=transaction
        )
        current = _read_drainer(capability)
        if current is None:
            raise PendingTransactionError("durable claimed drainer authority is required")
        if (
            current[0] == transaction.drainer
            and current[1] == "claimed"
            and current[2].get("receipt_digest") is None
        ):
            _write_drainer(
                capability,
                transaction.drainer,
                "claimed",
                acked_ids=[],
                receipt_digest=receipt_digest,
                lease_deadline=lease_deadline,
            )
    if not close_if_queue_empty(transaction, receipt_digest=receipt_digest):
        with pin_output(transaction.output) as capability, _locked(capability):
            _validate_authority(capability, transaction, allow_complete=True)
            if not _read_queue(capability):
                raise PendingTransactionError("successor queue disappeared during close")
            current = _read_drainer(capability)
            if current is None or current[0] != transaction.drainer or current[1] != "claimed":
                raise PendingTransactionError("successor handoff lost exact drainer")
            pending = {
                "schema": 1,
                "protocol_epoch": 1,
                **_drainer_json(transaction.drainer),
                "state": "CLOSE_PENDING",
                "receipt_digest": receipt_digest,
                "acked_ids": current[2].get("acked_ids", []),
                "queue_epoch": transaction.generation,
                "output_identity": capability.identity.json(),
                "successor_generation": transaction.generation + 1,
                "transaction_id": transaction.id,
                "token_identity": (
                    None
                    if transaction.token_identity is None
                    else {
                        "device": transaction.token_identity[0],
                        "inode": transaction.token_identity[1],
                    }
                ),
            }
            _replace_bytes(capability, DRAINER_FILE, _json_bytes(pending))
            _finish_close_locked(capability, pending)
            complete_protocol = _read_protocol(capability)
            complete_drainer = _read_drainer(capability)
            if complete_protocol is None or complete_drainer is None:
                raise PendingTransactionError(
                    "completed predecessor authority disappeared"
                )
            _write_predecessor_authority(
                capability,
                complete_protocol,
                complete_drainer,
                receipt_digest,
            )
            successor = DrainerTuple(
                transaction.generation + 1, 0, secrets.token_hex(16)
            )
            _write_drainer(
                capability,
                successor,
                "reserved",
                lease_deadline=time.time() + 30.0,
                predecessor_receipt=receipt_digest,
            )


def finalize_prepared_transaction() -> None:
    """Capability-commit an owner-prepared full-build generation and close it."""
    transaction = current_transaction()
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        prepared_capability = _pin_prepared_workspace(transaction, capability)

        def prepared_bytes(name: str) -> bytes:
            return _read_relative_bytes(prepared_capability.output, name)

        try:
            graph_name = "graph.json"
            graph_data = json.loads(prepared_bytes(graph_name).decode("utf-8"))
            metadata = graph_data.get("graph")
            if not isinstance(metadata, dict):
                metadata = {}
                graph_data["graph"] = metadata
            metadata[GRAPH_WATERMARK_KEY] = {
                "schema": 1,
                "protocol_epoch": 1,
                "generation": transaction.generation,
                "state": "active",
            }
            graph_payload = json.dumps(
                graph_data, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            try:
                manifest_payload = prepared_bytes("manifest.json")
            except PendingTransactionError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    raise PendingTransactionError(
                        "prepared manifest is required before finalization"
                    ) from exc
                raise
            prepared_marker = _load_json(capability, PREPARED_FILE) or {}
            prior_inventory = tuple(
                str(name) for name in prepared_marker.get("prior_inventory", [])
            )
            plan = publication_plan_from_directory(
                prepared_capability.output.path, prior_inventory=prior_inventory
            )
            prepared_capability.validate()
        finally:
            prepared_capability.close()
    payloads = dict(plan.payloads)
    payloads[graph_name] = graph_payload
    payloads["manifest.json"] = manifest_payload
    commit_publication_plan(transaction, PublicationPlan(payloads, plan.deletions))
    finish_transaction(transaction)
    with pin_output(transaction.output) as capability, _locked(capability):
        retired = _retire_prepared_locked(capability)
        if retired is None:
            raise PendingTransactionError("prepared workspace binding is missing")


def _validate_cancellation_predecessor(
    capability: OutputCapability,
    restored_protocol: Mapping[str, object],
    restored_drainer: tuple[DrainerTuple, str, dict[str, Any]],
    preserved_digest: str,
) -> None:
    receipt_preview = _load_json(capability, RECEIPT_FILE)
    graph_name = (
        None
        if receipt_preview is None
        else receipt_preview.get("graph_name", "graph.json")
    )
    if type(graph_name) is not str:
        raise PendingTransactionError("no-op rollback prior graph binding is malformed")
    receipt, receipt_digest, inventory = _validate_receipt_locked(
        capability,
        require_closed=True,
        protocol_override=restored_protocol,
        drainer_override=restored_drainer,
        retain_artifacts=(graph_name,),
    )
    graph_payload = inventory.get(graph_name)
    if graph_payload is None:
        raise PendingTransactionError("no-op rollback prior graph binding is missing")
    watermark = _watermark(graph_payload)
    watermark_output = watermark.get("output_identity")
    if (
        receipt_digest != preserved_digest
        or receipt.get("generation") != restored_protocol.get("generation")
        or receipt.get("transaction_id") != restored_protocol.get("transaction_id")
        or receipt.get("token_digest")
        != restored_protocol.get("owner_capability_digest")
        or receipt.get("watermark") != watermark
        or watermark.get("state") != "active"
        or watermark.get("generation") != receipt.get("generation")
        or (
            watermark.get("transaction_id") is not None
            and watermark.get("transaction_id") != receipt.get("transaction_id")
        )
        or (
            watermark_output is not None
            and _identity_from_json(watermark_output) != capability.identity
        )
    ):
        raise PendingTransactionError("no-op rollback prior generation is inconsistent")


def _prepared_cancellation_marker_from_json(
    raw: object, successor: Transaction
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PendingTransactionError("prepared cancellation binding changed")
    marker = raw
    prior_inventory = marker.get("prior_inventory")
    if (
        set(marker)
        != {
            "schema",
            "transaction_id",
            "generation",
            "token_digest",
            "identity",
            "output_identity",
            "prior_inventory",
        }
        or marker.get("schema") != 1
        or marker.get("transaction_id") != successor.id
        or marker.get("generation") != successor.generation
        or marker.get("token_digest") != successor.token_digest
        or _identity_from_json(marker.get("identity")) is None
        or _identity_from_json(marker.get("output_identity")) is None
        or not isinstance(prior_inventory, list)
        or len(prior_inventory) > _MAX_RECEIPT_ARTIFACTS
    ):
        raise PendingTransactionError("prepared cancellation binding changed")
    for name in prior_inventory:
        if type(name) is not str:
            raise PendingTransactionError("prepared cancellation binding changed")
        _validated_relative_name(name)
    return dict(marker)


def _validate_cancellation_prepared_marker(
    capability: OutputCapability, successor: Transaction
) -> dict[str, Any] | None:
    return _prepared_cancellation_marker_from_json(
        _load_json(capability, PREPARED_FILE), successor
    )


def _cancellation_retired_workspace_exists(
    capability: OutputCapability, marker: Mapping[str, object]
) -> bool:
    expected = _identity_from_json(marker.get("identity"))
    transaction_id = marker.get("transaction_id")
    if expected is None or not isinstance(transaction_id, str):
        raise PendingTransactionError("prepared cancellation binding changed")
    matches = 0
    with pin_output(capability.path.parent, mutation=False) as parent:
        for name in _list_entries(parent):
            if not name.startswith(f".graphify-retired-{transaction_id}-"):
                continue
            info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (expected.device, expected.inode):
                continue
            with pin_output(parent.path / name, mutation=False) as retired:
                retired_marker = _load_json(retired, ".graphify_retired.json")
            if (
                _validated_retired_marker(
                    retired_marker,
                    candidate_name=name,
                    candidate_identity=expected,
                    managed_output_identity=capability.identity,
                )
                is None
            ):
                raise PendingTransactionError(
                    "prepared cancellation retirement binding changed"
                )
            matches += 1
    if matches > 1:
        raise PendingTransactionError(
            "prepared cancellation retirement binding is ambiguous"
        )
    return matches == 1


def _validate_cancellation_state_locked(
    capability: OutputCapability,
    preserved: tuple[
        dict[str, Any],
        tuple[DrainerTuple, str, dict[str, Any]],
        str,
        dict[str, Any],
    ],
) -> Transaction:
    """Validate one exact durable cancellation phase without mutation."""
    restored_protocol, restored_drainer, preserved_digest, record = preserved
    state = str(record["state"])
    if state == "preserved-complete":
        raise PendingTransactionError("cancellation transition is not active")
    successor = _transaction_from_json(
        capability, record["successor_transaction"]
    )
    _validate_cancellation_predecessor(
        capability, restored_protocol, restored_drainer, preserved_digest
    )
    protocol = _read_protocol(capability)
    drainer = _read_drainer(capability)
    live = _read_transaction(capability)
    token_name = f".graphify_transaction_token.{successor.id}"
    token_info = _entry_stat(capability, token_name)
    token_present = token_info is not None
    if token_present and (
        successor.token_identity is None
        or (token_info.st_dev, token_info.st_ino) != successor.token_identity
    ):
        raise PendingTransactionError("cancellation token identity changed")

    if state == "cancelling":
        if protocol == restored_protocol:
            protocol_restored = True
        else:
            if live != successor:
                raise PendingTransactionError(
                    "cancellation live transaction binding changed"
                )
            _validate_durable_live_binding(capability, successor)
            protocol_restored = False
        if protocol_restored and (
            drainer is None
            or drainer[0] != successor.drainer
            or drainer[1] != "claimed"
        ):
            raise PendingTransactionError("cancellation drainer phase changed")
    elif protocol != restored_protocol:
        raise PendingTransactionError("cancellation protocol restore changed")

    if state == "protocol-restored":
        if drainer != restored_drainer and (
            drainer is None
            or drainer[0] != successor.drainer
            or drainer[1] != "claimed"
        ):
            raise PendingTransactionError("cancellation drainer restore changed")
    elif state not in {"cancelling"} and drainer != restored_drainer:
        raise PendingTransactionError("cancellation drainer restore changed")

    prepared = _prepared_cancellation_marker_from_json(
        record.get("prepared_workspace"), successor
    )
    live_prepared = _load_json(capability, PREPARED_FILE)
    if state in {"cancelling", "protocol-restored"}:
        if live_prepared != prepared:
            raise PendingTransactionError("prepared cancellation phase changed")
    elif state == "drainer-restored":
        if live_prepared != prepared:
            if (
                prepared is None
                or live_prepared is not None
                or not _cancellation_retired_workspace_exists(capability, prepared)
            ):
                raise PendingTransactionError("prepared cancellation phase changed")
    else:
        if live_prepared is not None:
            raise PendingTransactionError("cancelled prepared owner reappeared")
        if prepared is not None and not _cancellation_retired_workspace_exists(
            capability, prepared
        ):
            raise PendingTransactionError(
                "prepared cancellation retirement binding changed"
            )

    if state in {
        "cancelling",
        "protocol-restored",
        "drainer-restored",
    }:
        if token_present != (successor.token_identity is not None) or live != successor:
            raise PendingTransactionError("cancellation successor authority changed")
    elif state == "prepared-retired":
        if live != successor:
            raise PendingTransactionError("cancellation live transaction binding changed")
    elif state == "token-removed":
        if token_present or (live is not None and live != successor):
            raise PendingTransactionError("cancellation successor authority changed")
    elif state == "live-removed":
        if token_present or live is not None:
            raise PendingTransactionError("cancelled successor authority reappeared")
    return successor


def cancel_unpublished_transaction(
    transaction: Transaction,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> None:
    """Restore the prior complete generation after a proven no-op preparation.

    This is intentionally narrower than abort/recovery: it succeeds only when
    the still-live generation has published no receipt and the retained graph
    plus previous receipt prove the immediately preceding complete generation.
    """
    with pin_output(transaction.output) as capability, _locked(capability):
        preserved = _read_predecessor_authority(capability)
        if preserved is None:
            protocol = _read_protocol(capability)
            drainer = _read_drainer(capability)
            if (
                protocol is None
                or drainer is None
                or protocol.get("state") != "COMPLETE"
                or protocol.get("generation") != transaction.generation - 1
                or drainer[1] != "complete"
                or _read_transaction(capability) is not None
                or _entry_stat(
                    capability, f".graphify_transaction_token.{transaction.id}"
                )
                is not None
                or _entry_stat(capability, PREPARED_FILE) is not None
            ):
                raise PendingTransactionError(
                    "no-op rollback has no resumable predecessor authority"
                )
            receipt_digest = protocol.get("receipt_digest")
            if not _is_hex(receipt_digest):
                raise PendingTransactionError(
                    "no-op rollback predecessor receipt is malformed"
                )
            _validate_cancellation_predecessor(
                capability, protocol, drainer, str(receipt_digest)
            )
            _AUTHORITY.set(None)
            return

        restored_protocol, restored_drainer, preserved_digest, record = preserved
        state = str(record["state"])
        if state == "preserved-complete":
            live = _validate_authority(capability, transaction)
            if (
                restored_protocol.get("generation") != live.generation - 1
                or restored_drainer[0].generation + 1 != live.generation
            ):
                raise PendingTransactionError(
                    "no-op rollback has no prior complete generation"
                )
            successor = live
            _validate_cancellation_predecessor(
                capability,
                restored_protocol,
                restored_drainer,
                preserved_digest,
            )
            prepared_marker = _validate_cancellation_prepared_marker(
                capability, successor
            )
            record = {**record, "prepared_workspace": prepared_marker}
            record = _advance_cancellation_authority(
                capability, record, state="cancelling", successor=successor
            )
            _validate_cancellation_state_locked(
                capability,
                _predecessor_authority_from_json(capability, record),
            )
            state = "cancelling"
        else:
            successor = _validate_cancellation_state_locked(capability, preserved)
            if transaction != successor:
                raise PendingTransactionError(
                    "cancellation successor transaction binding changed"
                )

        states = (
            "cancelling",
            "protocol-restored",
            "drainer-restored",
            "prepared-retired",
            "token-removed",
            "live-removed",
        )
        state_index = states.index(state)
        protocol = _read_protocol(capability)
        if state_index == 0:
            if protocol != restored_protocol:
                current_live = _read_transaction(capability)
                if current_live is None or current_live != successor:
                    raise PendingTransactionError(
                        "cancellation live transaction binding changed"
                    )
                _validate_durable_live_binding(capability, current_live)
                _replace_bytes(
                    capability, PROTOCOL_FILE, _json_bytes(restored_protocol)
                )
            record = _advance_cancellation_authority(
                capability,
                record,
                state="protocol-restored",
                successor=successor,
            )
            state_index = 1
            if failpoint is not None:
                failpoint("after_cancel_protocol")
        elif protocol != restored_protocol:
            raise PendingTransactionError("cancellation protocol restore changed")

        current_drainer = _read_drainer(capability)
        if state_index == 1:
            if current_drainer != restored_drainer:
                if (
                    current_drainer is None
                    or current_drainer[0] != successor.drainer
                    or current_drainer[1] != "claimed"
                ):
                    raise PendingTransactionError(
                        "cancellation drainer restore changed"
                    )
                _replace_bytes(
                    capability, DRAINER_FILE, _json_bytes(restored_drainer[2])
                )
            record = _advance_cancellation_authority(
                capability,
                record,
                state="drainer-restored",
                successor=successor,
            )
            state_index = 2
            if failpoint is not None:
                failpoint("after_cancel_drainer")
        elif current_drainer != restored_drainer:
            raise PendingTransactionError("cancellation drainer restore changed")

        if state_index == 2:
            _validate_cancellation_prepared_marker(capability, successor)
            _retire_prepared_locked(capability)
            record = _advance_cancellation_authority(
                capability,
                record,
                state="prepared-retired",
                successor=successor,
            )
            state_index = 3
            if failpoint is not None:
                failpoint("after_cancel_prepared")
        elif _entry_stat(capability, PREPARED_FILE) is not None:
            raise PendingTransactionError("cancelled prepared owner reappeared")

        token_name = f".graphify_transaction_token.{successor.id}"
        if state_index == 3:
            token_info = _entry_stat(capability, token_name)
            if token_info is not None:
                if successor.token_identity is None or (
                    token_info.st_dev,
                    token_info.st_ino,
                ) != successor.token_identity:
                    raise PendingTransactionError(
                        "cancellation token identity changed"
                    )
                _unlink(capability, token_name, expected=successor.token_identity)
            record = _advance_cancellation_authority(
                capability, record, state="token-removed", successor=successor
            )
            state_index = 4
            if failpoint is not None:
                failpoint("after_cancel_token")
        elif _entry_stat(capability, token_name) is not None:
            raise PendingTransactionError("cancelled successor token reappeared")

        if state_index == 4:
            current_live = _read_transaction(capability)
            if current_live is not None:
                if current_live != successor:
                    raise PendingTransactionError(
                        "cancellation live transaction binding changed"
                    )
                _unlink(capability, TRANSACTION_FILE)
            record = _advance_cancellation_authority(
                capability, record, state="live-removed", successor=successor
            )
            state_index = 5
            if failpoint is not None:
                failpoint("after_cancel_live")
        elif _read_transaction(capability) is not None:
            raise PendingTransactionError("cancelled successor owner reappeared")

        if state_index == 5:
            if (
                _read_protocol(capability) != restored_protocol
                or _read_drainer(capability) != restored_drainer
                or _entry_stat(capability, token_name) is not None
                or _entry_stat(capability, PREPARED_FILE) is not None
                or _read_transaction(capability) is not None
            ):
                raise PendingTransactionError("cancellation terminal authority changed")
            _unlink(capability, PREDECESSOR_FILE)
            if failpoint is not None:
                failpoint("after_cancel_record")
    _AUTHORITY.set(None)


def abort_transaction(transaction: Transaction, *, leave_recoverable: bool = True) -> None:
    if not leave_recoverable:
        raise PendingTransactionError("destructive transaction abort is not supported")
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _read_transaction(capability)
        if live is None or live.id != transaction.id:
            raise PendingTransactionError("only the exact live transaction may abort")


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if (
        len(args) < 5
        or args[0] not in {"run-token", "run-prepared-token"}
        or args[2] != "--"
    ):
        raise SystemExit(
            "usage: python -P -m graphify.transaction "
            "(run-token|run-prepared-token) TOKEN -- (-c CODE | -m MODULE) [args...]"
        )
    run_token(args[1], args[3:], prepared=args[0] == "run-prepared-token")
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess integration
    current_module = sys.modules[__name__]
    canonical = sys.modules.get("graphify.transaction")
    if canonical is not None and canonical is not current_module:
        raise SystemExit(canonical._main())
    sys.modules["graphify.transaction"] = current_module
    raise SystemExit(_main())
