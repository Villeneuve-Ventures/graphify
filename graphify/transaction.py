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
ENQUEUE_FILE = ".graphify_enqueue.json"
QUARANTINE_FILE = ".graphify_rebuild_quarantine.jsonl"
PREPARED_FILE = ".graphify_prepared.json"
LEGACY_PENDING_STATE_FILE = ".graphify_legacy_pending_state.json"
DRAINER_FILE = ".graphify_drainer.json"
TRANSITION_FILE = ".graphify_transition.json"
PREDECESSOR_FILE = ".graphify_predecessor.json"
TOKEN_TRANSITION_FILE = ".graphify_token_transition.json"  # nosec B105 - state filename
_COORDINATION_FILES = frozenset(
    {
        PROTOCOL_FILE,
        TRANSACTION_FILE,
        RECEIPT_FILE,
        QUEUE_FILE,
        ENQUEUE_FILE,
        DRAINER_FILE,
        QUARANTINE_FILE,
        PREPARED_FILE,
        LEGACY_PENDING_STATE_FILE,
        TRANSITION_FILE,
        PREDECESSOR_FILE,
        TOKEN_TRANSITION_FILE,
    }
)
_COORDINATION_PREFIXES = (
    ".graphify_transaction_token.",
    ".graphify_rebuild_inflight.",
)
_UNMANAGED_JOURNAL_PREFIX = ".graphify-unmanaged-journal-"
_UNMANAGED_DELETE_PREFIX = ".graphify-unmanaged-delete-"
_OBSIDIAN_BATCH_PREFIX = ".graphify-unmanaged-obsidian-"
_ANY_UNMANAGED_PREDECESSOR = object()
_SAFE_GRAPHLESS_RUNTIME_ENTRIES = frozenset(
    {"cache", "memory", "reflections", ".graphify_python", ".rebuild.lock"}
)
_PLATFORM = "windows" if os.name == "nt" else "posix"
_MAX_STATE_BYTES = 1024 * 1024
_MAX_OBSIDIAN_BATCH_JOURNAL_BYTES = _MAX_STATE_BYTES
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
class UnmanagedInventoryEntry:
    payload: bytes
    digest: str
    identity: OutputIdentity


@dataclass(frozen=True)
class UnmanagedObsidianInventory:
    vault_identity: OutputIdentity | None
    manifest_payload: bytes | None
    manifest_digest: str | None
    manifest_identity: OutputIdentity | None
    files: Mapping[str, UnmanagedInventoryEntry]
    manifest_names: frozenset[str]


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


def _list_windows_directory_entries(directory_fd: int) -> list[str]:
    """Enumerate one already-open Windows directory without resolving its name."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class FileIdBothDirectoryInfo(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.ULONG),
            ("FileIndex", wintypes.ULONG),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.ULONG),
            ("FileNameLength", wintypes.ULONG),
            ("EaSize", wintypes.ULONG),
            ("ShortNameLength", ctypes.c_ubyte),
            ("ShortName", wintypes.WCHAR * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", wintypes.WCHAR * 1),
        ]

    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle")
    handle = wintypes.HANDLE(get_osfhandle(directory_fd))
    ntdll: Any = getattr(ctypes, "windll").ntdll
    query = ntdll.NtQueryDirectoryFile
    query.restype = ctypes.c_long
    entries: list[str] = []
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(64 * 1024)
        io_status = IoStatusBlock()
        status_code = query(
            handle,
            None,
            None,
            None,
            ctypes.byref(io_status),
            buffer,
            len(buffer),
            37,
            False,
            None,
            restart,
        )
        restart = False
        if status_code == -2147483642:  # STATUS_NO_MORE_FILES
            break
        if status_code < 0:
            raise OSError(f"Windows directory enumeration failed: {status_code}")
        offset = 0
        while offset < int(io_status.Information):
            item = FileIdBothDirectoryInfo.from_buffer(buffer, offset)
            name_offset = offset + FileIdBothDirectoryInfo.FileName.offset
            name = ctypes.wstring_at(name_offset, item.FileNameLength // 2)
            if name not in {".", ".."}:
                entries.append(name)
            if item.NextEntryOffset == 0:
                break
            offset += item.NextEntryOffset
        if int(io_status.Information) == 0:
            break
    return entries


def _list_entries(capability: OutputCapability) -> list[str]:
    capability.validate()
    entries = (
        _list_windows_directory_entries(capability.fd)
        if _PLATFORM == "windows"
        else os.listdir(capability.fd)
    )
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
    capability: OutputCapability,
    relative_name: str,
    payload: bytes,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> None:
    relative = Path(_validated_relative_name(relative_name))
    parent_fd = os.dup(capability.fd)
    nested: OutputCapability | None = None
    try:
        for component in relative.parts[:-1]:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            if created:
                os.fsync(parent_fd)
                if failpoint is not None:
                    failpoint(f"after_mkdir:{component}")
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


def _unmanaged_stage_matches(
    capability: OutputCapability,
    name: str,
    expected_prefix: bytes,
    expected_digest: str,
) -> bool:
    """Validate an owned stage without retaining its potentially large payload."""
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=capability.fd,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size < len(expected_prefix)
            or info.st_size - len(expected_prefix) > _MAX_RECEIPT_AGGREGATE_BYTES
        ):
            return False
        prefix = b""
        while len(prefix) < len(expected_prefix):
            chunk = os.read(fd, len(expected_prefix) - len(prefix))
            if not chunk:
                return False
            prefix += chunk
        if prefix != expected_prefix:
            return False
        digest = hashlib.sha256()
        remaining = info.st_size - len(expected_prefix)
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                return False
            digest.update(chunk)
            remaining -= len(chunk)
        return not os.read(fd, 1) and digest.hexdigest() == expected_digest
    finally:
        os.close(fd)


def _atomic_exchange(capability: OutputCapability, left: str, right: str) -> None:
    """Atomically exchange two directory entries or fail closed."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        exchange = getattr(libc, "renameatx_np", None)
        flags = 0x00000002  # RENAME_SWAP
    elif sys.platform.startswith("linux"):
        exchange = getattr(libc, "renameat2", None)
        flags = 0x00000002  # RENAME_EXCHANGE
    else:
        exchange = None
        flags = 0
    if exchange is None:
        raise PendingTransactionError(
            "atomic unmanaged replacement is unavailable on this platform"
        )
    exchange.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    exchange.restype = ctypes.c_int
    if exchange(
        capability.fd,
        os.fsencode(left),
        capability.fd,
        os.fsencode(right),
        flags,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), right)


def _atomic_rename_no_replace(
    capability: OutputCapability, source: str, target: str
) -> None:
    """Atomically rename one entry without replacing an existing target."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flags = 0x00000001  # RENAME_NOREPLACE
    else:
        rename = None
        flags = 0
    if rename is None:
        raise PendingTransactionError(
            "atomic unmanaged deletion is unavailable on this platform"
        )
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    if rename(
        capability.fd,
        os.fsencode(source),
        capability.fd,
        os.fsencode(target),
        flags,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _unmanaged_aux_name(leaf: str, suffix: str) -> str:
    return f".{leaf}.graphify-unmanaged-{secrets.token_hex(16)}.{suffix}"


def _valid_unmanaged_aux_name(name: object, leaf: str, suffix: str) -> bool:
    return type(name) is str and re.fullmatch(
        rf"\.{re.escape(leaf)}\.graphify-unmanaged-[0-9a-f]{{32}}\.{suffix}",
        name,
    ) is not None


def _relative_identity(
    capability: OutputCapability, name: str
) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=capability.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino)


def _replace_unmanaged_relative_bytes(
    capability: OutputCapability,
    relative_name: str,
    payload: bytes,
    *,
    journal_capability: OutputCapability,
    journal_name: str,
    canonical_destination: str,
    failpoint: Callable[[str], None] | None = None,
    terminal_validate: Callable[[], None] | None = None,
    expected_predecessor_digest: str | None | object = _ANY_UNMANAGED_PREDECESSOR,
    expected_predecessor_identity: OutputIdentity | None | object = (
        _ANY_UNMANAGED_PREDECESSOR
    ),
) -> None:
    """Publish one unmanaged leaf with an identity-bound atomic exchange."""
    relative = Path(_validated_relative_name(relative_name))
    parent_fd = os.dup(capability.fd)
    nested: OutputCapability | None = None
    created: list[tuple[tuple[str, ...], tuple[int, int]]] = []
    backup: str | None = None
    predecessor_identity: tuple[int, int] | None = None
    new_identity: tuple[int, int] | None = None
    staged: str | None = None
    published = False
    exchange_attempted = False
    retain_journal = False
    leaf = relative.parts[-1]
    stage_nonce = secrets.token_hex(32)
    stage_prefix = b"GRAPHIFY-UNMANAGED-STAGE\0" + stage_nonce.encode() + b"\0"
    try:
        walked: list[str] = []
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, 0o700, dir_fd=parent_fd)
                info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                created.append((tuple((*walked, component)), (info.st_dev, info.st_ino)))
                os.fsync(parent_fd)
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
            walked.append(component)
        nested = OutputCapability(
            capability.path / Path(*relative.parts[:-1]),
            OutputIdentity(os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino),
            parent_fd,
        )
        parent_fd = -1
        prior_journal = _load_json(journal_capability, journal_name)
        if prior_journal is not None:
            retain_journal = True
            expected_fields = {
                "schema",
                "protocol_epoch",
                "state",
                "destination",
                "journal_output_identity",
                "predecessor_identity",
                "backup_name",
                "stage_name",
                "stage_identity",
                "restored_identity",
                "stage_nonce",
                "payload_digest",
            }
            if (
                set(prior_journal) != expected_fields
                or prior_journal.get("schema") != 1
                or prior_journal.get("protocol_epoch") != 1
                or prior_journal.get("state")
                not in {
                    "planned",
                    "backup",
                    "staged",
                    "exchange_attempt",
                    "exchanged",
                    "stage-retire-attempt",
                    "stage-retired",
                    "aux-retired",
                    "restored_predecessor",
                    "restored_competitor",
                }
                or prior_journal.get("destination") != canonical_destination
                or _identity_from_json(
                    prior_journal.get("journal_output_identity")
                )
                != journal_capability.identity
                or not _is_hex(prior_journal.get("stage_nonce"))
                or not _is_hex(prior_journal.get("payload_digest"))
                or not _valid_unmanaged_aux_name(
                    prior_journal.get("stage_name"), leaf, "stage"
                )
            ):
                raise PendingTransactionError(
                    "unmanaged publication journal is malformed"
                )
            predecessor_raw = prior_journal.get("predecessor_identity")
            prior_predecessor = (
                None
                if predecessor_raw is None
                else _identity_from_json(predecessor_raw)
            )
            prior_backup = prior_journal.get("backup_name")
            if prior_predecessor is None:
                if prior_backup is not None:
                    raise PendingTransactionError(
                        "unmanaged publication journal is malformed"
                    )
            elif not _valid_unmanaged_aux_name(prior_backup, leaf, "backup"):
                raise PendingTransactionError(
                    "unmanaged publication journal is malformed"
                )
            backup = cast(str | None, prior_backup)
            staged = cast(str, prior_journal["stage_name"])
            stage_raw = prior_journal.get("stage_identity")
            prior_stage = (
                None if stage_raw is None else _identity_from_json(stage_raw)
            )
            restored_raw = prior_journal.get("restored_identity")
            restored_identity = (
                None if restored_raw is None else _identity_from_json(restored_raw)
            )
            restored_state = str(prior_journal["state"]).startswith("restored_")
            if restored_state != (restored_identity is not None):
                raise PendingTransactionError(
                    "unmanaged publication journal is malformed"
                )
            stage_current_raw = _relative_identity(nested, staged)
            leaf_current = _relative_identity(nested, leaf)
            backup_current = (
                None if backup is None else _relative_identity(nested, backup)
            )
            predecessor_tuple = (
                None
                if prior_predecessor is None
                else (prior_predecessor.device, prior_predecessor.inode)
            )
            successor_identity = (
                None
                if prior_stage is None
                else (prior_stage.device, prior_stage.inode)
            )
            completed_successor = (
                (
                    prior_journal["state"] in {"stage-retired", "aux-retired"}
                    or (
                        prior_journal["state"] == "stage-retire-attempt"
                        and predecessor_tuple is None
                    )
                )
                and successor_identity is not None
                and leaf_current == successor_identity
                and stage_current_raw is None
                and backup_current is None
            )
            if prior_journal["state"] == "aux-retired" and not completed_successor:
                raise PendingTransactionError(
                    "unmanaged retired auxiliary state changed"
                )
            allowed_backup_identities = {predecessor_tuple}
            if (
                prior_journal["state"]
                in {"stage-retire-attempt", "stage-retired"}
                and leaf_current == predecessor_tuple
            ):
                allowed_backup_identities.add(successor_identity)
            if (
                backup_current is not None
                and backup_current not in allowed_backup_identities
            ):
                raise PendingTransactionError(
                    "unmanaged publication backup identity changed"
                )
            if (
                prior_journal["state"]
                in {"stage-retire-attempt", "stage-retired"}
                and leaf_current == predecessor_tuple
                and stage_current_raw is None
                and backup_current == successor_identity
            ):
                restored_tuple = cast(tuple[int, int], leaf_current)
                prior_journal["state"] = "restored_predecessor"
                prior_journal["restored_identity"] = {
                    "device": restored_tuple[0],
                    "inode": restored_tuple[1],
                }
                restored_identity = OutputIdentity(*restored_tuple)
                _replace_bytes(
                    journal_capability, journal_name, _json_bytes(prior_journal)
                )
                restored_state = True
            if (
                prior_journal["state"]
                in {"exchanged", "stage-retire-attempt", "stage-retired"}
                and leaf_current is not None
                and leaf_current not in {successor_identity, predecessor_tuple}
                and (
                    (predecessor_tuple is None and stage_current_raw in {successor_identity, None})
                    or (
                        predecessor_tuple is not None
                        and stage_current_raw in {predecessor_tuple, None}
                    )
                )
            ):
                prior_journal["state"] = "restored_competitor"
                prior_journal["restored_identity"] = {
                    "device": leaf_current[0],
                    "inode": leaf_current[1],
                }
                restored_identity = OutputIdentity(*leaf_current)
                _replace_bytes(
                    journal_capability, journal_name, _json_bytes(prior_journal)
                )
                restored_state = True
            recovered_competitor = prior_journal["state"] == "restored_competitor"
            if restored_state:
                restored_tuple = (
                    cast(OutputIdentity, restored_identity).device,
                    cast(OutputIdentity, restored_identity).inode,
                )
                if leaf_current != restored_tuple or (
                    prior_journal["state"] == "restored_predecessor"
                ) != (restored_tuple == predecessor_tuple):
                    raise PendingTransactionError(
                        "unmanaged restored publication identity changed"
                    )
            elif (
                successor_identity is not None
                and leaf_current == successor_identity
                and not completed_successor
            ):
                if predecessor_tuple is None:
                    os.unlink(leaf, dir_fd=nested.fd)
                    leaf_current = None
                elif stage_current_raw is not None:
                    recovered_competitor = stage_current_raw != predecessor_tuple
                    _atomic_exchange(nested, staged, leaf)
                    os.fsync(nested.fd)
                    leaf_current = stage_current_raw
                    stage_current_raw = successor_identity
                    if failpoint is not None:
                        failpoint("after_unmanaged_recovery_exchange")
                    prior_journal["state"] = (
                        "restored_competitor"
                        if recovered_competitor
                        else "restored_predecessor"
                    )
                    prior_journal["restored_identity"] = {
                        "device": leaf_current[0],
                        "inode": leaf_current[1],
                    }
                    _replace_bytes(
                        journal_capability, journal_name, _json_bytes(prior_journal)
                    )
                    restored_state = True
                    if failpoint is not None:
                        failpoint("after_unmanaged_restored_phase")
                elif (
                    prior_journal["state"]
                    in {"exchanged", "stage-retire-attempt", "stage-retired"}
                    and backup is not None
                    and backup_current == predecessor_tuple
                ):
                    _atomic_exchange(nested, backup, leaf)
                    os.fsync(nested.fd)
                    leaf_current = predecessor_tuple
                    backup_current = successor_identity
                    if failpoint is not None:
                        failpoint("after_unmanaged_recovery_exchange")
                    prior_journal["state"] = "restored_predecessor"
                    prior_journal["restored_identity"] = {
                        "device": leaf_current[0],
                        "inode": leaf_current[1],
                    }
                    _replace_bytes(
                        journal_capability, journal_name, _json_bytes(prior_journal)
                    )
                    restored_state = True
                    if failpoint is not None:
                        failpoint("after_unmanaged_restored_phase")
                elif not (
                    prior_journal["state"]
                    in {"stage-retire-attempt", "stage-retired", "aux-retired"}
                    and backup_current is None
                ):
                    raise PendingTransactionError(
                        "unmanaged exchange displacement is unavailable"
                    )
            elif (
                prior_journal["state"] == "exchange_attempt"
                and successor_identity is not None
                and stage_current_raw == successor_identity
                and leaf_current is not None
            ):
                recovered_competitor = leaf_current != predecessor_tuple
                prior_journal["state"] = (
                    "restored_competitor"
                    if recovered_competitor
                    else "restored_predecessor"
                )
                prior_journal["restored_identity"] = {
                    "device": leaf_current[0],
                    "inode": leaf_current[1],
                }
                _replace_bytes(
                    journal_capability, journal_name, _json_bytes(prior_journal)
                )
                restored_state = True
                if failpoint is not None:
                    failpoint("after_unmanaged_restored_phase")
            if stage_current_raw is not None:
                stage_current = OutputIdentity(*stage_current_raw)
                if prior_stage is not None:
                    allowed = {prior_stage}
                    if prior_predecessor is not None:
                        allowed.add(prior_predecessor)
                    if stage_current not in allowed:
                        raise PendingTransactionError(
                            "unmanaged publication stage identity changed"
                        )
                else:
                    expected_prefix = (
                        b"GRAPHIFY-UNMANAGED-STAGE\0"
                        + str(prior_journal["stage_nonce"]).encode()
                        + b"\0"
                    )
                    if not _unmanaged_stage_matches(
                        nested,
                        staged,
                        expected_prefix,
                        str(prior_journal["payload_digest"]),
                    ):
                        raise PendingTransactionError(
                            "unmanaged publication stage ownership is unproven"
                        )
                    os.unlink(staged, dir_fd=nested.fd)
                    stage_current_raw = None
            elif (
                prior_stage is not None
                and not restored_state
                and not completed_successor
                and leaf_current
                != (prior_stage.device, prior_stage.inode)
            ):
                raise PendingTransactionError(
                    "unmanaged publication stage identity is unavailable"
                )
            if (
                prior_stage is not None
                and not restored_state
                and not completed_successor
                and leaf_current == successor_identity
            ):
                if predecessor_tuple is None:
                    os.unlink(leaf, dir_fd=nested.fd)
                elif stage_current_raw == predecessor_tuple:
                    _atomic_exchange(nested, staged, leaf)
                elif backup_current == predecessor_tuple:
                    _atomic_exchange(nested, cast(str, backup), leaf)
                else:
                    raise PendingTransactionError(
                        "unmanaged predecessor recovery identity is unavailable"
                    )
            for index, (name, allowed_identity) in enumerate(
                ((staged, successor_identity), (backup, predecessor_tuple))
            ):
                if name is None:
                    continue
                current = _relative_identity(nested, name)
                if current is None:
                    continue
                allowed_current = {allowed_identity, predecessor_tuple}
                if restored_state:
                    allowed_current.add(successor_identity)
                if current not in allowed_current:
                    raise PendingTransactionError(
                        "unmanaged auxiliary identity changed"
                    )
                os.unlink(name, dir_fd=nested.fd)
                os.fsync(nested.fd)
                if failpoint is not None:
                    failpoint(
                        "after_unmanaged_recovery_stage_retirement"
                        if index == 0
                        else "after_unmanaged_recovery_backup_retirement"
                    )
            _unlink(journal_capability, journal_name)
            retain_journal = False
            if recovered_competitor:
                raise PendingTransactionError(
                    "unmanaged exchange competitor was restored; retry publication"
                )
            if completed_successor:
                return
        backup = None
        staged = None
        prior = (
            os.stat(leaf, dir_fd=nested.fd, follow_symlinks=False)
            if _entry_stat(nested, leaf) is not None
            else None
        )
        if expected_predecessor_digest is not _ANY_UNMANAGED_PREDECESSOR:
            if prior is None:
                if expected_predecessor_digest is not None:
                    raise PendingTransactionError(
                        "unmanaged destination predecessor is missing"
                    )
            else:
                if expected_predecessor_digest is None:
                    raise PendingTransactionError(
                        "unmanaged destination appeared before publication"
                    )
                prior_digest, _size, _body = _hash_relative_bytes(
                    nested,
                    leaf,
                    aggregate_remaining=_MAX_RECEIPT_AGGREGATE_BYTES,
                )
                if prior_digest != expected_predecessor_digest:
                    raise PendingTransactionError(
                        "unmanaged destination predecessor changed"
                    )
                if _relative_identity(nested, leaf) != (
                    prior.st_dev,
                    prior.st_ino,
                ):
                    raise PendingTransactionError(
                        "unmanaged destination predecessor changed"
                    )
        if expected_predecessor_identity is not _ANY_UNMANAGED_PREDECESSOR:
            expected_tuple = (
                None
                if expected_predecessor_identity is None
                else (
                    cast(OutputIdentity, expected_predecessor_identity).device,
                    cast(OutputIdentity, expected_predecessor_identity).inode,
                )
            )
            prior_tuple = (
                None if prior is None else (prior.st_dev, prior.st_ino)
            )
            if prior_tuple != expected_tuple:
                raise PendingTransactionError(
                    "unmanaged destination predecessor changed"
                )
        if prior is not None:
            if not stat.S_ISREG(prior.st_mode):
                raise PendingTransactionError("unmanaged destination leaf is unsafe")
            predecessor_identity = (prior.st_dev, prior.st_ino)
            backup = _unmanaged_aux_name(leaf, "backup")
        staged = _unmanaged_aux_name(leaf, "stage")
        journal = {
            "schema": 1,
            "protocol_epoch": 1,
            "state": "planned",
            "destination": canonical_destination,
            "journal_output_identity": journal_capability.identity.json(),
            "predecessor_identity": (
                None
                if predecessor_identity is None
                else {
                    "device": predecessor_identity[0],
                    "inode": predecessor_identity[1],
                }
            ),
            "backup_name": backup,
            "stage_name": staged,
            "stage_identity": None,
            "restored_identity": None,
            "stage_nonce": stage_nonce,
            "payload_digest": hashlib.sha256(payload).hexdigest(),
        }
        _replace_bytes(journal_capability, journal_name, _json_bytes(journal))
        if prior is not None:
            os.link(
                leaf,
                cast(str, backup),
                src_dir_fd=nested.fd,
                dst_dir_fd=nested.fd,
                follow_symlinks=False,
            )
            os.fsync(nested.fd)
            journal["state"] = "backup"
            _replace_bytes(journal_capability, journal_name, _json_bytes(journal))
            if failpoint is not None:
                failpoint("after_unmanaged_predecessor_backup")
        new_identity = _create_bytes(nested, staged, stage_prefix + payload)
        if failpoint is not None:
            failpoint("after_unmanaged_stage_create")
        journal["state"] = "staged"
        journal["stage_identity"] = {
            "device": new_identity[0],
            "inode": new_identity[1],
        }
        _replace_bytes(journal_capability, journal_name, _json_bytes(journal))
        stage_fd = os.open(
            staged,
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=nested.fd,
        )
        try:
            stage_info = os.fstat(stage_fd)
            if (stage_info.st_dev, stage_info.st_ino) != new_identity:
                raise PendingTransactionError(
                    "unmanaged publication stage identity changed"
                )
            os.ftruncate(stage_fd, 0)
            view = memoryview(payload)
            while view:
                count = os.write(stage_fd, view)
                if count <= 0:
                    raise OSError("unmanaged stage write made no progress")
                view = view[count:]
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        if failpoint is not None:
            failpoint("after_unmanaged_successor_stage")
        if predecessor_identity is not None:
            journal["state"] = "exchange_attempt"
            _replace_bytes(journal_capability, journal_name, _json_bytes(journal))
            exchange_attempted = True
            _atomic_exchange(nested, staged, leaf)
            published = True
            os.fsync(nested.fd)
            if failpoint is not None:
                failpoint("after_unmanaged_exchange")
            displaced = _relative_identity(nested, staged)
            if displaced != predecessor_identity:
                _atomic_exchange(nested, staged, leaf)
                published = False
                os.fsync(nested.fd)
                raise PendingTransactionError(
                    "unmanaged destination changed before publication"
                )
        else:
            try:
                os.link(
                    staged,
                    leaf,
                    src_dir_fd=nested.fd,
                    dst_dir_fd=nested.fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise PendingTransactionError(
                    "unmanaged destination changed before publication"
                ) from exc
            published = True
            os.fsync(nested.fd)
        published_info = os.stat(leaf, dir_fd=nested.fd, follow_symlinks=False)
        if (published_info.st_dev, published_info.st_ino) != new_identity:
            raise PendingTransactionError(
                "unmanaged destination publication identity changed"
            )
        journal["state"] = "exchanged"
        _replace_bytes(journal_capability, journal_name, _json_bytes(journal))
        if failpoint is not None:
            failpoint("after_unmanaged_leaf_publication")
        capability.validate()
        nested.validate()
        if terminal_validate is not None:
            terminal_validate()
        for index, (name, expected) in enumerate(
            ((staged, predecessor_identity), (backup, predecessor_identity))
        ):
            if name is None:
                continue
            current = _relative_identity(nested, name)
            if current is None:
                continue
            if predecessor_identity is None:
                expected = new_identity
            if current != expected:
                raise PendingTransactionError(
                    "unmanaged destination auxiliary identity changed"
                )
            if index == 0:
                journal["state"] = "stage-retire-attempt"
                _replace_bytes(
                    journal_capability, journal_name, _json_bytes(journal)
                )
                retain_journal = True
                published = False
            os.unlink(name, dir_fd=nested.fd)
            os.fsync(nested.fd)
            if index == 0:
                if failpoint is not None:
                    failpoint("after_unmanaged_stage_unlink_fsync")
                journal["state"] = "stage-retired"
                _replace_bytes(
                    journal_capability, journal_name, _json_bytes(journal)
                )
                retain_journal = True
                published = False
                if failpoint is not None:
                    failpoint("after_unmanaged_stage_retirement")
        journal["state"] = "aux-retired"
        _replace_bytes(journal_capability, journal_name, _json_bytes(journal))
        retain_journal = True
        published = False
        if failpoint is not None:
            failpoint("after_unmanaged_auxiliary_retirement")
        backup = None
        staged = None
        _unlink(journal_capability, journal_name)
        retain_journal = False
        published = False
        created.clear()
        if failpoint is not None:
            failpoint("after_unmanaged_backup_retirement")
    except Exception:
        if nested is not None:
            current_identity = _relative_identity(nested, leaf)
            if published and current_identity == new_identity:
                if predecessor_identity is not None and staged is not None:
                    displaced = _relative_identity(nested, staged)
                    if displaced is None:
                        raise PendingTransactionError(
                            "unmanaged exchange displacement is unavailable"
                        )
                    if exchange_attempted:
                        _atomic_exchange(nested, staged, leaf)
                elif predecessor_identity is None:
                    os.unlink(leaf, dir_fd=nested.fd)
            for name, allowed in (
                (staged, {new_identity, predecessor_identity}),
                (backup, {predecessor_identity}),
            ):
                if name is None:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    current = _relative_identity(nested, name)
                    if current not in allowed:
                        continue
                    os.unlink(name, dir_fd=nested.fd)
            os.fsync(nested.fd)
        if not retain_journal:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(journal_name, dir_fd=journal_capability.fd)
                os.fsync(journal_capability.fd)
        for parts, expected in reversed(created if not retain_journal else []):
            cleanup_fd = os.dup(capability.fd)
            try:
                for component in parts[:-1]:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=cleanup_fd,
                    )
                    os.close(cleanup_fd)
                    cleanup_fd = next_fd
                info = os.stat(parts[-1], dir_fd=cleanup_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != expected:
                    raise PendingTransactionError(
                        "unmanaged destination directory identity changed"
                    )
                os.rmdir(parts[-1], dir_fd=cleanup_fd)
                os.fsync(cleanup_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(cleanup_fd)
        raise
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
    if (
        value["device"] < 0
        or value["device"] > 18_446_744_073_709_551_615
        or value["inode"] <= 0
        or value["inode"] > 18_446_744_073_709_551_615
    ):
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
            _fence_pending_enqueue_locked(capability)
            token_transition = _read_token_transition(capability)
            if token_transition is not None:
                _validate_token_transition_current(capability, token_transition)
                raise PendingTransactionError(
                    "token publication transition requires operational recovery"
                )
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
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires operational recovery"
            )
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
            else:
                legacy_budget = _LegacyInventoryBudget()
                legacy_inventory = _legacy_owned_dynamic_inventory(
                    capability, budget=legacy_budget
                )
                for name in MANAGED_PUBLICATION_PATHS:
                    if name in legacy_inventory:
                        continue
                    try:
                        payload = _read_relative_bytes(
                            capability, name, legacy_budget.remaining
                        )
                    except PendingTransactionError as exc:
                        if isinstance(exc.__cause__, FileNotFoundError):
                            continue
                        raise
                    legacy_budget.retain(name, payload)
                    legacy_inventory[name] = payload
                prior_inventory = tuple(sorted(legacy_inventory))
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


def _read_token_transition(capability: OutputCapability) -> dict[str, Any] | None:
    raw = _load_json(capability, TOKEN_TRANSITION_FILE)
    if raw is None:
        return None
    fields = {
        "schema",
        "protocol_epoch",
        "state",
        "output_identity",
        "predecessor_transaction",
        "predecessor_protocol",
        "target_transaction",
        "target_protocol",
        "token_name",
        "token_payload",
        "token_digest",
        "token_identity",
        "pending_predecessor_drainer",
        "pending_predecessor_protocol",
        "pending_predecessor_transaction",
        "binding_order",
    }
    if (
        set(raw) != fields
        or raw.get("schema") != 1
        or raw.get("protocol_epoch") != 1
        or raw.get("state")
        not in {"planned", "token-created", "live-bound", "protocol-bound"}
        or raw.get("binding_order") not in {"live-protocol", "protocol-live"}
        or _identity_from_json(raw.get("output_identity")) != capability.identity
        or not isinstance(raw.get("token_payload"), dict)
        or not _is_hex(raw.get("token_digest"))
    ):
        raise PendingTransactionError("token publication transition is malformed")
    predecessor = _transaction_from_json(capability, raw["predecessor_transaction"])
    predecessor_protocol = _protocol_from_json(capability, raw["predecessor_protocol"])
    target = _transaction_from_json(capability, raw["target_transaction"])
    target_protocol = _protocol_from_json(capability, raw["target_protocol"])
    payload = _json_bytes(raw["token_payload"])
    expected_name = f".graphify_transaction_token.{target.id}"
    identity = _token_identity_from_json(raw.get("token_identity"))
    if (
        raw.get("token_name") != expected_name
        or hashlib.sha256(payload).hexdigest() != raw.get("token_digest")
        or target.token_digest != raw.get("token_digest")
        or target.token_identity is not None
        or target_protocol.get("transaction_id") != target.id
        or target_protocol.get("owner_capability_digest") != target.token_digest
        or target_protocol.get("token_identity") is not None
        or predecessor.output_identity != target.output_identity
        or predecessor.output != target.output
        or predecessor_protocol.get("transaction_id") != predecessor.id
        or (raw["state"] == "planned" and identity is not None)
        or (raw["state"] != "planned" and identity is None)
    ):
        raise PendingTransactionError("token publication transition binding changed")
    pending_drainer = raw.get("pending_predecessor_drainer")
    pending_protocol = raw.get("pending_predecessor_protocol")
    pending_transaction = raw.get("pending_predecessor_transaction")
    pending_values = (pending_drainer, pending_protocol, pending_transaction)
    if any(value is not None for value in pending_values):
        if not all(value is not None for value in pending_values):
            raise PendingTransactionError("token takeover transition is malformed")
        if (
            not isinstance(pending_drainer, dict)
            or set(pending_drainer) != {"drainer", "state"}
            or pending_drainer.get("state")
            not in {"reserved", "launching", "claimed"}
            or _drainer_from_json(pending_drainer.get("drainer"))
            != predecessor.drainer
            or _protocol_from_json(capability, pending_protocol).get("transaction_id")
            != predecessor.id
            or _transaction_from_json(capability, pending_transaction) != predecessor
        ):
            raise PendingTransactionError("token takeover transition binding changed")
    return raw


def _validate_token_transition_current(
    capability: OutputCapability, record: Mapping[str, Any]
) -> Transaction:
    """Validate one replay point without changing any durable state."""
    predecessor = _transaction_from_json(capability, record["predecessor_transaction"])
    target_base = _transaction_from_json(capability, record["target_transaction"])
    predecessor_protocol = _protocol_from_json(
        capability, record["predecessor_protocol"]
    )
    target_protocol_base = _protocol_from_json(capability, record["target_protocol"])
    token_payload = record["token_payload"]
    takeover = record.get("pending_predecessor_drainer") is not None
    expected_target = replace(
        predecessor,
        id=target_base.id if takeover else predecessor.id,
        token_digest=target_base.token_digest,
        token_identity=None,
        drainer=target_base.drainer if takeover else predecessor.drainer,
        phase="awaiting-drainer" if takeover else predecessor.phase,
    )
    if (
        set(token_payload) != {
            "schema",
            "id",
            "root",
            "output",
            "generation",
            "drainer",
            "secret",
        }
        or token_payload.get("schema") != 1
        or token_payload.get("id") != target_base.id
        or token_payload.get("root") != target_base.root
        or token_payload.get("output") != str(target_base.output)
        or token_payload.get("generation") != target_base.generation
        or _drainer_from_json(token_payload.get("drainer")) != target_base.drainer
        or not _is_hex(token_payload.get("secret"))
        or target_base != expected_target
        or predecessor.generation != target_base.generation
        or predecessor.root != target_base.root
        or predecessor.kind != target_base.kind
        or predecessor.output != target_base.output
        or predecessor.output_identity != target_base.output_identity
        or predecessor.drainer.generation != target_base.drainer.generation
        or (not takeover and predecessor.drainer != target_base.drainer)
        or (not takeover and predecessor.id != target_base.id)
        or (takeover and predecessor.id == target_base.id)
        or (takeover and target_base.drainer.claim_epoch != predecessor.drainer.claim_epoch + 1)
        or record["binding_order"] != ("protocol-live" if takeover else "live-protocol")
    ):
        raise PendingTransactionError("token publication transaction binding changed")
    predecessor_identity = (
        None
        if predecessor.token_identity is None
        else {"device": predecessor.token_identity[0], "inode": predecessor.token_identity[1]}
    )
    expected_target_protocol = dict(predecessor_protocol)
    expected_target_protocol.update(
        transaction_id=target_base.id,
        owner_capability_digest=target_base.token_digest,
        token_identity=None,
    )
    if takeover:
        expected_target_protocol["lease_deadline"] = target_protocol_base.get(
            "lease_deadline"
        )
    if (
        predecessor_protocol.get("state") != "INCOMPLETE"
        or predecessor_protocol.get("generation") != predecessor.generation
        or predecessor_protocol.get("transaction_id") != predecessor.id
        or predecessor_protocol.get("root") != predecessor.root
        or predecessor_protocol.get("kind") != predecessor.kind
        or predecessor_protocol.get("owner_capability_digest") != predecessor.token_digest
        or predecessor_protocol.get("token_identity") != predecessor_identity
        or target_protocol_base.get("state") != "INCOMPLETE"
        or target_protocol_base.get("generation") != target_base.generation
        or target_protocol_base.get("transaction_id") != target_base.id
        or target_protocol_base.get("root") != target_base.root
        or target_protocol_base.get("kind") != target_base.kind
        or target_protocol_base.get("owner_capability_digest") != target_base.token_digest
        or target_protocol_base.get("token_identity") is not None
        or target_protocol_base != expected_target_protocol
    ):
        raise PendingTransactionError("token publication protocol binding changed")

    name = str(record["token_name"])
    token_info = _entry_stat(capability, name)
    token_identity = None if token_info is None else (token_info.st_dev, token_info.st_ino)
    recorded_identity = _token_identity_from_json(record.get("token_identity"))
    if token_info is not None and hashlib.sha256(
        _read_bytes(capability, name, _TOKEN_MAX_BYTES)
    ).hexdigest() != record["token_digest"]:
        raise PendingTransactionError("published transaction token changed")
    if record["state"] == "planned":
        if recorded_identity is not None:
            raise PendingTransactionError("planned token transition has an identity")
    elif token_identity is None or token_identity != recorded_identity:
        raise PendingTransactionError("published transaction token identity changed")

    target = replace(target_base, token_identity=token_identity)
    target_protocol = dict(target_protocol_base)
    target_protocol["token_identity"] = (
        None
        if token_identity is None
        else {"device": token_identity[0], "inode": token_identity[1]}
    )
    live = _read_transaction(capability)
    protocol = _read_protocol(capability)
    state = str(record["state"])
    if not takeover:
        state_matrix = {
            "planned": ({predecessor}, [predecessor_protocol]),
            "token-created": ({predecessor, target}, [predecessor_protocol]),
            "live-bound": ({target}, [predecessor_protocol, target_protocol]),
            "protocol-bound": ({target}, [target_protocol]),
        }
    else:
        state_matrix = {
            "planned": ({predecessor}, [predecessor_protocol]),
            "token-created": ({predecessor}, [predecessor_protocol, target_protocol]),
            "protocol-bound": ({predecessor, target}, [target_protocol]),
            "live-bound": ({target}, [target_protocol]),
        }
    allowed_live, allowed_protocol = state_matrix[state]
    if live not in allowed_live or protocol not in allowed_protocol:
        raise PendingTransactionError("token publication filesystem binding changed")

    pending = _read_pending_transition(capability)
    if not takeover:
        if pending is not None:
            raise PendingTransactionError("ordinary token transition gained pending state")
    elif pending is not None:
        if state == "planned":
            raise PendingTransactionError(
                "planned token takeover gained pending state"
            )
        _validate_pending_transition_current(capability, pending)
        expected_pending_drainer = record["pending_predecessor_drainer"]
        if (
            pending[0]
            != (
                _drainer_from_json(expected_pending_drainer["drainer"]),
                expected_pending_drainer["state"],
            )
            or pending[1] != _protocol_from_json(
                capability, record["pending_predecessor_protocol"]
            )
            or pending[2] != predecessor
            or pending[3] != target
            or pending[4] != target_protocol
        ):
            raise PendingTransactionError("token takeover pending binding changed")
    elif state in {"live-bound", "protocol-bound"} or (
        state == "token-created" and protocol == target_protocol
    ):
        raise PendingTransactionError("token takeover pending transition disappeared")
    return target


def _resume_token_transition_locked(
    capability: OutputCapability,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> tuple[Transaction, dict[str, Any]]:
    loaded_record = _read_token_transition(capability)
    if loaded_record is None:
        raise PendingTransactionError("token publication transition is missing")
    record: dict[str, Any] = loaded_record
    _validate_token_transition_current(capability, record)
    predecessor = _transaction_from_json(capability, record["predecessor_transaction"])
    target = _transaction_from_json(capability, record["target_transaction"])
    payload = _json_bytes(record["token_payload"])
    name = str(record["token_name"])
    info = _entry_stat(capability, name)
    if info is None:
        if record["state"] != "planned":
            raise PendingTransactionError("published transaction token disappeared")
        identity = _create_bytes(capability, name, payload)
    else:
        if hashlib.sha256(_read_bytes(capability, name)).hexdigest() != record["token_digest"]:
            raise PendingTransactionError("published transaction token changed")
        identity = (info.st_dev, info.st_ino)
        recorded_identity = _token_identity_from_json(record.get("token_identity"))
        if recorded_identity is not None and recorded_identity != identity:
            raise PendingTransactionError("published transaction token identity changed")
    target = replace(target, token_identity=identity)
    target_protocol = dict(record["target_protocol"])
    target_protocol["token_identity"] = {"device": identity[0], "inode": identity[1]}
    record = {
        **record,
        "state": "token-created",
        "token_identity": {"device": identity[0], "inode": identity[1]},
    }
    _replace_bytes(capability, TOKEN_TRANSITION_FILE, _json_bytes(record))
    pending_drainer = record.get("pending_predecessor_drainer")
    predecessor_protocol = record["predecessor_protocol"]
    if pending_drainer is not None:
        _write_pending_transition(
            capability,
            predecessor_drainer=(
                _drainer_from_json(pending_drainer["drainer"]),
                str(pending_drainer["state"]),
            ),
            predecessor_protocol=cast(Mapping[str, object], predecessor_protocol),
            predecessor_transaction=predecessor,
            successor=target,
            successor_protocol=target_protocol,
        )
    if failpoint is not None:
        failpoint("after_token_created")
    live = _read_transaction(capability)
    protocol = _read_protocol(capability)
    def bind_live() -> None:
        nonlocal live, record
        if live == predecessor:
            _write_transaction(capability, target, phase=target.phase)
        elif live != target:
            raise PendingTransactionError("token publication live owner changed")
        live = target
        record["state"] = "live-bound"
        _replace_bytes(capability, TOKEN_TRANSITION_FILE, _json_bytes(record))
        if failpoint is not None:
            failpoint("after_token_live")

    def bind_protocol() -> None:
        nonlocal protocol, record
        if protocol == predecessor_protocol or (
            predecessor_protocol is None
            and protocol is not None
            and protocol.get("transaction_id") == predecessor.id
        ):
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(target_protocol))
        elif protocol != target_protocol:
            raise PendingTransactionError("token publication protocol changed")
        protocol = target_protocol
        record["state"] = "protocol-bound"
        _replace_bytes(capability, TOKEN_TRANSITION_FILE, _json_bytes(record))
        if failpoint is not None:
            failpoint("after_token_protocol")

    if record["binding_order"] == "protocol-live":
        bind_protocol()
        bind_live()
    else:
        bind_live()
        bind_protocol()
    _unlink(capability, TOKEN_TRANSITION_FILE)
    if failpoint is not None:
        failpoint("after_token_transition")
    _AUTHORITY.set(_authority_for(target))
    return target, target_protocol


def _start_token_transition_locked(
    capability: OutputCapability,
    *,
    predecessor: Transaction,
    target: Transaction,
    target_protocol: Mapping[str, object],
    token_payload: Mapping[str, object],
    predecessor_protocol: Mapping[str, object],
    pending_predecessor_drainer: tuple[DrainerTuple, str] | None = None,
    pending_predecessor_protocol: Mapping[str, object] | None = None,
    binding_order: str = "live-protocol",
    failpoint: Callable[[str], None] | None = None,
) -> tuple[Transaction, dict[str, Any]]:
    payload = _json_bytes(token_payload)
    record = {
        "schema": 1,
        "protocol_epoch": 1,
        "state": "planned",
        "output_identity": capability.identity.json(),
        "predecessor_transaction": _transaction_json(
            predecessor, phase=predecessor.phase
        ),
        "predecessor_protocol": dict(predecessor_protocol),
        "target_transaction": _transaction_json(target, phase=target.phase),
        "target_protocol": dict(target_protocol),
        "token_name": f".graphify_transaction_token.{target.id}",
        "token_payload": dict(token_payload),
        "token_digest": hashlib.sha256(payload).hexdigest(),
        "token_identity": None,  # nosec B105 - absent inode binding, not a password
        "pending_predecessor_drainer": (
            None
            if pending_predecessor_drainer is None
            else {
                "drainer": _drainer_json(pending_predecessor_drainer[0]),
                "state": pending_predecessor_drainer[1],
            }
        ),
        "pending_predecessor_protocol": (
            None
            if pending_predecessor_protocol is None
            else dict(pending_predecessor_protocol)
        ),
        "pending_predecessor_transaction": (
            None
            if pending_predecessor_drainer is None
            else _transaction_json(predecessor, phase=predecessor.phase)
        ),
        "binding_order": binding_order,
    }
    _replace_bytes(capability, TOKEN_TRANSITION_FILE, _json_bytes(record))
    _read_token_transition(capability)
    if failpoint is not None:
        failpoint("after_token_journal")
    return _resume_token_transition_locked(capability, failpoint=failpoint)


def stage_transaction_handoff(
    transaction: Transaction,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> TransactionToken:
    with pin_output(transaction.output) as capability, _locked(capability):
        existing = _read_token_transition(capability)
        if existing is not None:
            target = _validate_token_transition_current(capability, existing)
            if target.id != transaction.id:
                raise PendingTransactionError("token transition owner changed")
            raise PendingTransactionError(
                "token publication transition requires operational recovery"
            )
        live = _validate_authority(capability, transaction)
        secret = secrets.token_hex(32)
        token_payload = {
            "schema": 1,
            "id": live.id,
            "root": live.root,
            "output": str(live.output),
            "generation": live.generation,
            "drainer": _drainer_json(live.drainer),
            "secret": secret,
        }
        digest = hashlib.sha256(_json_bytes(token_payload)).hexdigest()
        target = replace(live, token_digest=digest, token_identity=None)
        protocol = _read_protocol(capability)
        if protocol is None:
            raise PendingTransactionError("protocol state is missing")
        protocol["owner_capability_digest"] = digest
        protocol["token_identity"] = None
        live, _protocol = _start_token_transition_locked(
            capability,
            predecessor=live,
            target=target,
            target_protocol=protocol,
            token_payload=token_payload,
            predecessor_protocol=_read_protocol(capability) or {},
            failpoint=failpoint,
        )
        return TransactionToken(
            live.id,
            capability.path / f".graphify_transaction_token.{live.id}",
            live.generation,
        )


def _open_token(path: Path) -> tuple[dict[str, object], bytes, tuple[int, int]]:
    output = path.parent
    with pin_output(output) as capability, _locked(capability):
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires operational recovery"
            )
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
    with pin_output(path.parent) as capability, _locked(capability):
        _fence_pending_enqueue_locked(capability)
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires operational recovery"
            )
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
        _fence_pending_enqueue_locked(capability)
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
    relative_name = _validated_relative_name(relative_name)
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        prepared = _pin_prepared_workspace(transaction, capability)
        try:
            _replace_relative_bytes(prepared.output, relative_name, payload)
        finally:
            prepared.close()


def unlink_prepared(transaction: Transaction, name: str) -> None:
    """Remove one validated relative prepared artifact under exact live authority."""
    name = _validated_relative_name(name)
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
    _fence_pending_enqueue_locked(capability)
    token_transition = _read_token_transition(capability)
    if token_transition is not None:
        _validate_token_transition_current(capability, token_transition)
        raise PendingTransactionError(
            "token publication transition blocks owner publication"
        )
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
    name = _validated_shallow_name(name)
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
    relative_name = _validated_relative_name(relative_name)
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        if failpoint:
            failpoint("after_validate")
        _replace_relative_bytes(
            capability, relative_name, payload, failpoint=failpoint
        )
        if failpoint:
            failpoint("after_replace")


def commit_unlink(
    transaction: Transaction,
    name: str,
    *,
    capability: OutputCapability | None = None,
) -> None:
    """Remove one prepared managed artifact under exact live authority."""
    name = _validated_shallow_name(name)
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
    if type(value) is not str or not value or "\\" in value:
        raise PendingTransactionError(f"unsafe managed relative path: {value}")
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {value}")
    name = relative.as_posix()
    coordination_files = {item.casefold() for item in _COORDINATION_FILES}
    coordination_prefixes = tuple(item.casefold() for item in _COORDINATION_PREFIXES)
    if name != value or any(
        part.casefold() in coordination_files
        or part.casefold().startswith(coordination_prefixes)
        or part.casefold().startswith(
            (
                ".graphify-prepare-",
                ".graphify-retired-",
                ".graphify-gc-root-",
                ".graphify-gc-journal-",
                ".graphify-gc-quarantine-",
                ".graphify-unmanaged-",
            )
        )
        for part in relative.parts
    ):
        raise PendingTransactionError(f"publication plan contains coordination state: {name}")
    return name


def _validated_shallow_name(value: str) -> str:
    name = _validated_relative_name(value)
    if "/" in name:
        raise PendingTransactionError(f"unsafe managed shallow path: {value}")
    return name


def _reject_casefold_collisions(names: Sequence[str]) -> None:
    folded: dict[str, str] = {}
    for name in names:
        previous = folded.setdefault(name.casefold(), name)
        if previous != name:
            raise PendingTransactionError(
                "managed publication paths collide on a case-insensitive filesystem"
            )


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
    payloads: dict[str, bytes] = {}
    for raw_name, payload in plan.payloads.items():
        name = _validated_relative_name(raw_name)
        if name in payloads or type(payload) is not bytes:
            raise PendingTransactionError("publication plan payloads are malformed")
        payloads[name] = payload
    deletions: list[str] = []
    for raw_name in plan.deletions:
        name = _validated_relative_name(raw_name)
        if name in deletions:
            raise PendingTransactionError("publication plan deletions are duplicated")
        deletions.append(name)
    _reject_casefold_collisions((*payloads, *deletions, graph_name))
    if set(payloads).intersection(deletions):
        raise PendingTransactionError("publication plan payload and deletion overlap")
    graph_payload = payloads.get(graph_name)
    manifest_payload = payloads.get("manifest.json")
    if graph_payload is None or manifest_payload is None:
        raise PendingTransactionError("publication plan requires graph and manifest")
    with owned_step(transaction):
        for name in deletions:
            with pin_output(transaction.output) as capability, _locked(capability):
                _validate_authority(capability, transaction)
                _unlink_relative(capability, name)
        for name, payload in payloads.items():
            if "/" in name:
                commit_relative_bytes(transaction, name, payload)
            else:
                commit_bytes(transaction, name, payload)
        return commit_generation(
            transaction,
            graph_payload=graph_payload,
            manifest_payload=manifest_payload,
            required_artifacts=tuple(payloads),
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
    failpoint: Callable[[str], None] | None = None,
) -> GenerationReceipt:
    graph_name = _validated_relative_name(graph_name)
    required_artifacts = tuple(
        _validated_relative_name(name) for name in required_artifacts
    )
    if len(required_artifacts) != len(set(required_artifacts)):
        raise PendingTransactionError("generation inventory contains duplicate artifacts")
    _reject_casefold_collisions((*required_artifacts, graph_name))
    watermark = _watermark(graph_payload)
    if graph_name not in required_artifacts or "manifest.json" not in required_artifacts:
        raise PendingTransactionError(
            "generation inventory must include graph and manifest"
        )
    if watermark.get("generation") != transaction.generation or watermark.get("state") != "active":
        raise PendingTransactionError("graph watermark does not match transaction generation")
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction, allow_complete=True)
        existing_protocol = _read_protocol(capability)
        if existing_protocol is not None and existing_protocol.get("state") == "COMPLETE":
            _receipt, existing_digest, _inventory = _validate_receipt_locked(
                capability, transaction=live
            )
            _unlink(capability, PREDECESSOR_FILE)
            return GenerationReceipt(existing_digest, live.generation)
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
        protocol = existing_protocol
        if protocol is None:
            raise PendingTransactionError("protocol state is missing")
        _replace_bytes(capability, RECEIPT_FILE, receipt_payload)
        if failpoint is not None:
            failpoint("after_receipt")
        protocol.update(state="COMPLETE", generation=live.generation, receipt_digest=digest)
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
        if failpoint is not None:
            failpoint("after_protocol")
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
    try:
        validated_required = [
            _validated_relative_name(cast(str, name)) for name in required
        ]
        graph_name = _validated_relative_name(
            cast(str, receipt.get("graph_name", "graph.json"))
        )
        _reject_casefold_collisions((*validated_required, graph_name))
    except PendingTransactionError as exc:
        raise PendingTransactionError(
            "generation receipt inventory is unsafe"
        ) from exc
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


def _coordination_present(
    capability: OutputCapability, *, ignored_names: frozenset[str] = frozenset()
) -> bool:
    files = {name.casefold() for name in _COORDINATION_FILES}
    prefixes = tuple(name.casefold() for name in _COORDINATION_PREFIXES) + (
        ".graphify-prepare-",
        ".graphify-retired-",
        ".graphify-gc-root-",
        ".graphify-gc-journal-",
        ".graphify-gc-quarantine-",
    )
    for name in _list_entries(capability):
        if name in ignored_names:
            continue
        folded = name.casefold()
        if folded in files or folded.startswith(prefixes):
            return True
    return False


def _managed_authority_present(
    capability: OutputCapability, *, ignored_names: frozenset[str] = frozenset()
) -> bool:
    if _coordination_present(capability, ignored_names=ignored_names):
        return True
    graph_entries = [
        name for name in _list_entries(capability) if name.casefold() == "graph.json"
    ]
    if not graph_entries:
        return False
    if len(graph_entries) != 1:
        raise PendingTransactionError("destination graph authority is ambiguous")
    from graphify.security import _max_graph_file_bytes

    try:
        graph = json.loads(
            _read_bytes(
                capability, graph_entries[0], _max_graph_file_bytes()
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("destination graph authority is malformed") from exc
    if not isinstance(graph, dict) or not isinstance(graph.get("graph", {}), dict):
        raise PendingTransactionError("destination graph authority is malformed")
    return GRAPH_WATERMARK_KEY in graph.get("graph", {})


def managed_output_containing(path: Path | str) -> Path | None:
    """Return the nearest identity-pinned managed output containing a destination."""
    requested = Path(path).expanduser().absolute()
    candidate = requested if requested.is_dir() else requested.parent
    while True:
        if candidate.exists() and candidate.is_dir():
            with pin_output(candidate, mutation=False) as capability:
                if _managed_authority_present(capability):
                    return capability.path
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent


def commit_unmanaged_bytes(
    path: Path | str,
    payload: bytes,
    *,
    failpoint: Callable[[str], None] | None = None,
    expected_predecessor_digest: str | None | object = _ANY_UNMANAGED_PREDECESSOR,
    expected_predecessor_identity: OutputIdentity | None | object = (
        _ANY_UNMANAGED_PREDECESSOR
    ),
) -> Path:
    """Publish below one pinned destination only after proving it unmanaged."""
    if type(payload) is not bytes:
        raise TypeError("unmanaged commit payload must be immutable bytes")
    if (expected_predecessor_digest is _ANY_UNMANAGED_PREDECESSOR) != (
        expected_predecessor_identity is _ANY_UNMANAGED_PREDECESSOR
    ):
        raise TypeError("unmanaged predecessor identity and digest must be paired")
    if expected_predecessor_identity is not _ANY_UNMANAGED_PREDECESSOR and (
        expected_predecessor_identity is not None
        and not isinstance(expected_predecessor_identity, OutputIdentity)
    ):
        raise TypeError("unmanaged predecessor identity is malformed")
    if expected_predecessor_digest is not _ANY_UNMANAGED_PREDECESSOR and (
        expected_predecessor_digest is not None
        and not _is_hex(expected_predecessor_digest)
    ):
        raise PendingTransactionError("unmanaged predecessor digest is malformed")
    if (expected_predecessor_digest is None) != (
        expected_predecessor_identity is None
    ):
        raise PendingTransactionError(
            "unmanaged predecessor identity and digest are inconsistent"
        )
    if _PLATFORM == "windows":
        raise PendingTransactionError(
            "native Windows unmanaged final mutation is not proven"
        )
    destination = Path(path).expanduser().resolve(strict=False)
    existing_parent = destination.parent
    while not existing_parent.exists():
        if existing_parent.parent == existing_parent:
            raise PendingTransactionError("unmanaged destination has no existing parent")
        existing_parent = existing_parent.parent
    existing_parent = existing_parent.resolve(strict=True)
    try:
        relative = destination.relative_to(existing_parent).as_posix()
    except ValueError as exc:
        raise PendingTransactionError("unmanaged destination identity changed") from exc
    relative = _validated_relative_name(relative)
    canonical_destination = os.fspath(destination)
    recovery_journal = (
        _UNMANAGED_JOURNAL_PREFIX
        + hashlib.sha256(canonical_destination.encode("utf-8")).hexdigest()[:32]
        + ".json"
    )

    ancestor_paths = [existing_parent]
    ancestor = existing_parent.parent
    while True:
        ancestor_paths.append(ancestor)
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent

    with contextlib.ExitStack() as stack:
        capabilities = [
            stack.enter_context(pin_output(path, mutation=True))
            for path in ancestor_paths
        ]
        destination_capability = capabilities[0]
        for pinned in reversed(capabilities):
            stack.enter_context(_locked(pinned))

        journal_matches: list[OutputCapability] = []
        for pinned in capabilities:
            for name in _list_entries(pinned):
                if not name.casefold().startswith(
                    _UNMANAGED_JOURNAL_PREFIX.casefold()
                ):
                    continue
                if name == recovery_journal:
                    journal_matches.append(pinned)
                else:
                    raise PendingTransactionError(
                        "unrelated unmanaged publication journal is present"
                    )
        if len(journal_matches) > 1:
            raise PendingTransactionError(
                "unmanaged publication journal identity is ambiguous"
            )
        journal_capability = (
            journal_matches[0] if journal_matches else destination_capability
        )

        def validate_chain() -> None:
            for pinned in capabilities:
                pinned.validate()
                journals = [
                    name
                    for name in _list_entries(pinned)
                    if name.casefold().startswith(
                        _UNMANAGED_JOURNAL_PREFIX.casefold()
                    )
                ]
                expected = (
                    [recovery_journal] if pinned is journal_capability else []
                )
                if journals not in ([], expected):
                    raise PendingTransactionError(
                        "unmanaged publication journal identity changed"
                    )
                if _managed_authority_present(pinned):
                    raise PendingTransactionError(
                        "external destination is inside managed graph authority"
                    )

        validate_chain()
        _replace_unmanaged_relative_bytes(
            destination_capability,
            relative,
            payload,
            journal_capability=journal_capability,
            journal_name=recovery_journal,
            canonical_destination=canonical_destination,
            failpoint=failpoint,
            terminal_validate=validate_chain,
            expected_predecessor_digest=expected_predecessor_digest,
            expected_predecessor_identity=expected_predecessor_identity,
        )
    return destination


def _relative_regular_identity(
    capability: OutputCapability, relative_name: str
) -> OutputIdentity:
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
        fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PendingTransactionError(
                    f"unsafe unmanaged artifact: {relative_name}"
                )
            return OutputIdentity(info.st_dev, info.st_ino)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PendingTransactionError(
            f"unmanaged artifact is missing: {relative_name}"
        ) from exc
    finally:
        os.close(parent_fd)


def _relative_inventory_regular_identity(
    capability: OutputCapability, relative_name: str
) -> OutputIdentity | None:
    """Return one pinned regular identity, omitting proven nonregular entries."""
    if _PLATFORM == "windows":
        return _relative_regular_identity(capability, relative_name)
    relative = Path(_validated_relative_name(relative_name))
    parent_fd = os.dup(capability.fd)
    try:
        for component in relative.parts[:-1]:
            try:
                before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(before.st_mode):
                return None
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(next_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                os.close(next_fd)
                raise PendingTransactionError(
                    f"unmanaged artifact identity changed: {relative_name}"
                )
            os.close(parent_fd)
            parent_fd = next_fd
        leaf = relative.parts[-1]
        try:
            before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            return None
        fd = os.open(
            leaf,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise PendingTransactionError(
                    f"unmanaged artifact identity changed: {relative_name}"
                )
            return OutputIdentity(opened.st_dev, opened.st_ino)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def open_unmanaged_obsidian_inventory(
    path: Path | str,
) -> UnmanagedObsidianInventory:
    """Read only manifest-owned vault files through one pinned vault handle."""
    vault = Path(path).expanduser()
    if not vault.exists():
        return UnmanagedObsidianInventory(None, None, None, None, {}, frozenset())
    with pin_output(vault, mutation=False) as capability, _locked(capability):
        if _managed_authority_present(capability):
            raise PendingTransactionError(
                "external destination is inside managed graph authority"
            )
        name = ".graphify_obsidian_manifest.json"
        if _entry_stat(capability, name) is None:
            return UnmanagedObsidianInventory(
                capability.identity, None, None, None, {}, frozenset()
            )
        manifest_identity = _relative_regular_identity(capability, name)
        manifest_payload = _read_bytes(capability, name, 1_048_576)
        try:
            raw = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PendingTransactionError(
                "external Obsidian ownership manifest is malformed"
            ) from exc
        files = raw.get("files") if isinstance(raw, dict) else None
        if (
            not isinstance(files, list)
            or len(files) > _MAX_RECEIPT_ARTIFACTS
            or any(type(item) is not str for item in files)
        ):
            raise PendingTransactionError(
                "external Obsidian ownership manifest is malformed"
            )
        inventory: dict[str, UnmanagedInventoryEntry] = {}
        manifest_names: set[str] = set()
        remaining = _MAX_RECEIPT_AGGREGATE_BYTES - len(manifest_payload)
        folded: set[str] = set()
        for item in files:
            relative = _validated_relative_name(item)
            if relative.casefold() in folded:
                raise PendingTransactionError(
                    "external Obsidian ownership manifest is ambiguous"
                )
            folded.add(relative.casefold())
            manifest_names.add(relative)
            identity = _relative_inventory_regular_identity(capability, relative)
            if identity is None:
                continue
            payload = _read_relative_bytes(capability, relative, limit=remaining)
            if _relative_regular_identity(capability, relative) != identity:
                raise PendingTransactionError(
                    f"unmanaged artifact identity changed: {relative}"
                )
            remaining -= len(payload)
            inventory[relative] = UnmanagedInventoryEntry(
                payload,
                hashlib.sha256(payload).hexdigest(),
                identity,
            )
        return UnmanagedObsidianInventory(
            capability.identity,
            manifest_payload,
            hashlib.sha256(manifest_payload).hexdigest(),
            manifest_identity,
            inventory,
            frozenset(manifest_names),
        )


def commit_unmanaged_unlink(
    path: Path | str,
    *,
    expected_identity: OutputIdentity,
    expected_digest: str,
    failpoint: Callable[[str], None] | None = None,
    foreign_callback: Callable[[], None] | None = None,
    deleted_callback: Callable[[], None] | None = None,
) -> Literal["deleted", "foreign"]:
    """Remove one identity-bound unmanaged file through a durable quarantine."""
    if not isinstance(expected_identity, OutputIdentity):
        raise TypeError("unmanaged deletion identity is required")
    if not _is_hex(expected_digest):
        raise PendingTransactionError("unmanaged deletion digest is malformed")
    if _PLATFORM == "windows":
        raise PendingTransactionError(
            "native Windows unmanaged final mutation is not proven"
        )
    requested = Path(path).expanduser().absolute()
    parent = requested.parent.resolve(strict=True)
    destination = parent / requested.name
    journal_name = (
        _UNMANAGED_DELETE_PREFIX
        + hashlib.sha256(os.fspath(destination).encode()).hexdigest()[:32]
        + ".json"
    )
    ancestor_paths = [parent]
    ancestor = parent.parent
    while True:
        ancestor_paths.append(ancestor)
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    with contextlib.ExitStack() as stack:
        capabilities = [
            stack.enter_context(pin_output(candidate, mutation=True))
            for candidate in ancestor_paths
        ]
        for pinned in reversed(capabilities):
            stack.enter_context(_locked(pinned))
        for pinned in capabilities:
            pinned.validate()
            journals = {
                name
                for name in _list_entries(pinned)
                if name.casefold().startswith(_UNMANAGED_JOURNAL_PREFIX.casefold())
                or name.casefold().startswith(_UNMANAGED_DELETE_PREFIX.casefold())
            }
            if journals - ({journal_name} if pinned is capabilities[0] else set()):
                raise PendingTransactionError(
                    "unmanaged publication recovery is required before deletion"
                )
            if _managed_authority_present(pinned):
                raise PendingTransactionError(
                    "external destination is inside managed graph authority"
                )
        capability = capabilities[0]
        raw = _load_json(capability, journal_name)
        expected_tuple = (expected_identity.device, expected_identity.inode)
        if raw is None:
            live = _relative_identity(capability, destination.name)
            if live is None:
                return "deleted"
            digest, _size, _body = _hash_relative_bytes(
                capability,
                destination.name,
                aggregate_remaining=_MAX_RECEIPT_AGGREGATE_BYTES,
            )
            if live != expected_tuple or digest != expected_digest:
                raise PendingTransactionError(
                    "unmanaged deletion predecessor changed"
                )
            quarantine = _unmanaged_aux_name(destination.name, "delete")
            raw = {
                "schema": 1,
                "protocol_epoch": 1,
                "operation": "unmanaged-delete",
                "state": "planned",
                "destination": os.fspath(destination),
                "parent_identity": capability.identity.json(),
                "expected_identity": expected_identity.json(),
                "expected_digest": expected_digest,
                "quarantine_name": quarantine,
                "quarantine_identity": None,
                "restored_identity": None,
            }
            _replace_bytes(capability, journal_name, _json_bytes(raw))
        expected_fields = {
            "schema", "protocol_epoch", "operation", "state", "destination",
            "parent_identity", "expected_identity", "expected_digest",
            "quarantine_name", "quarantine_identity",
            "restored_identity",
        }
        if (
            set(raw) != expected_fields
            or raw.get("schema") != 1
            or raw.get("protocol_epoch") != 1
            or raw.get("operation") != "unmanaged-delete"
            or raw.get("state")
            not in {
                "planned",
                "delete-attempt",
                "quarantined",
                "retire-attempt",
                "restored-foreign",
                "deleted",
            }
            or raw.get("destination") != os.fspath(destination)
            or _identity_from_json(raw.get("parent_identity")) != capability.identity
            or _identity_from_json(raw.get("expected_identity")) != expected_identity
            or raw.get("expected_digest") != expected_digest
            or not _valid_unmanaged_aux_name(
                raw.get("quarantine_name"), destination.name, "delete"
            )
        ):
            raise PendingTransactionError("unmanaged deletion journal is malformed")
        quarantine = cast(str, raw["quarantine_name"])
        quarantine_identity_raw = raw.get("quarantine_identity")
        quarantine_identity = (
            None
            if quarantine_identity_raw is None
            else _identity_from_json(quarantine_identity_raw)
        )
        if (raw["state"] in {"quarantined", "retire-attempt"}) != (
            quarantine_identity is not None
        ):
            raise PendingTransactionError("unmanaged deletion journal is malformed")
        restored_raw = raw.get("restored_identity")
        restored_identity = (
            None if restored_raw is None else _identity_from_json(restored_raw)
        )
        if (raw["state"] == "restored-foreign") != (
            restored_identity is not None
        ):
            raise PendingTransactionError("unmanaged deletion journal is malformed")
        live = _relative_identity(capability, destination.name)
        quarantined = _relative_identity(capability, quarantine)
        if raw["state"] == "deleted":
            if quarantined is not None:
                raise PendingTransactionError(
                    "unmanaged deletion completed geometry changed"
                )
            if live is not None:
                raw["state"] = "restored-foreign"
                raw["restored_identity"] = {
                    "device": live[0],
                    "inode": live[1],
                }
                _replace_bytes(capability, journal_name, _json_bytes(raw))
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_restored_foreign")
                if foreign_callback is not None:
                    foreign_callback()
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_callback")
                _unlink(capability, journal_name)
                for pinned in capabilities:
                    pinned.validate()
                return "foreign"
            if deleted_callback is not None:
                deleted_callback()
            if failpoint is not None:
                failpoint("after_unmanaged_delete_callback")
            _unlink(capability, journal_name)
            for pinned in capabilities:
                pinned.validate()
            return "deleted"
        if raw["state"] == "restored-foreign":
            restored_tuple = (
                cast(OutputIdentity, restored_identity).device,
                cast(OutputIdentity, restored_identity).inode,
            )
            if live != restored_tuple or quarantined is not None:
                raise PendingTransactionError(
                    "unmanaged deletion restored identity changed"
                )
            if foreign_callback is not None:
                foreign_callback()
            if failpoint is not None:
                failpoint("after_unmanaged_delete_callback")
            _unlink(capability, journal_name)
            for pinned in capabilities:
                pinned.validate()
            return "foreign"
        if raw["state"] in {"planned", "delete-attempt"} and quarantined is None:
            if live is None:
                raw["state"] = "deleted"
                _replace_bytes(capability, journal_name, _json_bytes(raw))
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_completed_absent")
                if deleted_callback is not None:
                    deleted_callback()
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_callback")
                _unlink(capability, journal_name)
                for pinned in capabilities:
                    pinned.validate()
                return "deleted"
            if live != expected_tuple:
                raw["state"] = "restored-foreign"
                raw["restored_identity"] = {
                    "device": live[0],
                    "inode": live[1],
                }
                _replace_bytes(capability, journal_name, _json_bytes(raw))
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_restored_foreign")
                if foreign_callback is not None:
                    foreign_callback()
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_callback")
                _unlink(capability, journal_name)
                for pinned in capabilities:
                    pinned.validate()
                return "foreign"
            digest, _size, _body = _hash_relative_bytes(
                capability,
                destination.name,
                aggregate_remaining=_MAX_RECEIPT_AGGREGATE_BYTES,
            )
            if digest != expected_digest:
                raise PendingTransactionError("unmanaged deletion predecessor changed")
            raw["state"] = "delete-attempt"
            _replace_bytes(capability, journal_name, _json_bytes(raw))
            if failpoint is not None:
                failpoint("before_unmanaged_delete_rename")
            live = _relative_identity(capability, destination.name)
            if live is None:
                raw["state"] = "deleted"
                _replace_bytes(capability, journal_name, _json_bytes(raw))
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_completed_absent")
                if deleted_callback is not None:
                    deleted_callback()
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_callback")
                _unlink(capability, journal_name)
                for pinned in capabilities:
                    pinned.validate()
                return "deleted"
            if live != expected_tuple:
                raw["state"] = "restored-foreign"
                raw["restored_identity"] = {
                    "device": live[0],
                    "inode": live[1],
                }
                _replace_bytes(capability, journal_name, _json_bytes(raw))
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_restored_foreign")
                if foreign_callback is not None:
                    foreign_callback()
                if failpoint is not None:
                    failpoint("after_unmanaged_delete_callback")
                _unlink(capability, journal_name)
                for pinned in capabilities:
                    pinned.validate()
                return "foreign"
            _atomic_rename_no_replace(capability, destination.name, quarantine)
            os.fsync(capability.fd)
            quarantined = _relative_identity(capability, quarantine)
            if quarantined != expected_tuple:
                if (
                    quarantined is not None
                    and _relative_identity(capability, destination.name) is None
                ):
                    _atomic_rename_no_replace(
                        capability, quarantine, destination.name
                    )
                    os.fsync(capability.fd)
                    if _relative_identity(
                        capability, destination.name
                    ) != quarantined or _relative_identity(
                        capability, quarantine
                    ) is not None:
                        raise PendingTransactionError(
                            "unmanaged deletion restored identity changed"
                        )
                    raw["state"] = "restored-foreign"
                    raw["restored_identity"] = {
                        "device": quarantined[0],
                        "inode": quarantined[1],
                    }
                    _replace_bytes(capability, journal_name, _json_bytes(raw))
                    if failpoint is not None:
                        failpoint("after_unmanaged_delete_restored_foreign")
                    if foreign_callback is not None:
                        foreign_callback()
                    if failpoint is not None:
                        failpoint("after_unmanaged_delete_callback")
                    _unlink(capability, journal_name)
                    for pinned in capabilities:
                        pinned.validate()
                    return "foreign"
                raise PendingTransactionError("unmanaged deletion quarantine changed")
            raw["state"] = "quarantined"
            raw["quarantine_identity"] = expected_identity.json()
            _replace_bytes(capability, journal_name, _json_bytes(raw))
            if failpoint is not None:
                failpoint("after_unmanaged_delete_quarantine")
        elif quarantined == expected_tuple and live != expected_tuple:
            raw["state"] = "quarantined"
            raw["quarantine_identity"] = expected_identity.json()
            _replace_bytes(capability, journal_name, _json_bytes(raw))
        elif raw["state"] == "retire-attempt" and quarantined is None:
            _unlink(capability, journal_name)
            for pinned in capabilities:
                pinned.validate()
            return "deleted"
        else:
            raise PendingTransactionError("unmanaged deletion geometry changed")
        digest, _size, _body = _hash_relative_bytes(
            capability,
            quarantine,
            aggregate_remaining=_MAX_RECEIPT_AGGREGATE_BYTES,
        )
        if digest != expected_digest:
            raise PendingTransactionError("unmanaged deletion quarantine changed")
        if _relative_identity(capability, quarantine) != expected_tuple:
            raise PendingTransactionError("unmanaged deletion quarantine changed")
        raw["state"] = "retire-attempt"
        _replace_bytes(capability, journal_name, _json_bytes(raw))
        os.unlink(quarantine, dir_fd=capability.fd)
        os.fsync(capability.fd)
        if failpoint is not None:
            failpoint("after_unmanaged_delete_unlink_fsync")
        _unlink(capability, journal_name)
        for pinned in capabilities:
            pinned.validate()
    return "deleted"


def _unmanaged_obsidian_current(
    capability: OutputCapability, name: str
) -> tuple[OutputIdentity, str] | None:
    try:
        identity = _relative_regular_identity(capability, name)
        digest, _size, _body = _hash_relative_bytes(
            capability,
            name,
            aggregate_remaining=_MAX_RECEIPT_AGGREGATE_BYTES,
        )
    except PendingTransactionError as exc:
        if "missing" in str(exc):
            return None
        raise
    return identity, digest


def _obsidian_leaf_journal_present(
    capability: OutputCapability,
    *,
    vault: Path,
    relative_name: str,
    prefix: str = _UNMANAGED_JOURNAL_PREFIX,
) -> bool:
    relative = Path(_validated_relative_name(relative_name))
    parent_fd = os.dup(capability.fd)
    try:
        canonical = os.fspath(vault / relative)
        journal_name = (
            prefix
            + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
            + ".json"
        )
        for component in relative.parts[:-1]:
            probe = OutputCapability(vault, capability.identity, parent_fd)
            if _entry_stat(probe, journal_name) is not None:
                return True
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        parent = OutputCapability(
            vault / Path(*relative.parts[:-1]),
            OutputIdentity(os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino),
            parent_fd,
        )
        parent_fd = -1
        return _entry_stat(parent, journal_name) is not None
    except (FileNotFoundError, NotADirectoryError):
        return False
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        elif "parent" in locals():
            parent.close()


def _unmanaged_directory_stat(
    capability: OutputCapability, name: str
) -> os.stat_result | None:
    name = _validated_shallow_name(name)
    try:
        info = os.stat(name, dir_fd=capability.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise PendingTransactionError("external Obsidian vault path is unsafe")
    return info


def _open_unmanaged_directory_relative(
    anchor: OutputCapability,
    relative_name: str,
    *,
    create: bool,
) -> tuple[OutputCapability | None, bool]:
    relative = Path(_validated_relative_name(relative_name))
    parent_fd = os.dup(anchor.fd)
    created_any = False
    try:
        for component in relative.parts:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    created_any = True
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None, created_any
            os.close(parent_fd)
            parent_fd = next_fd
        info = os.fstat(parent_fd)
        capability = OutputCapability(
            anchor.path / relative,
            OutputIdentity(info.st_dev, info.st_ino),
            parent_fd,
        )
        parent_fd = -1
        capability.validate()
        return capability, created_any
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _obsidian_identity_json(identity: OutputIdentity | None) -> dict[str, int] | None:
    return None if identity is None else identity.json()


def _obsidian_batch_bytes(raw: Mapping[str, Any]) -> bytes:
    payload = _json_bytes(raw)
    if len(payload) > _MAX_OBSIDIAN_BATCH_JOURNAL_BYTES:
        raise PendingTransactionError(
            "unmanaged Obsidian batch journal exceeds bounds"
        )
    return payload


def _load_obsidian_batch_journal(
    capability: OutputCapability, name: str
) -> dict[str, Any] | None:
    if _entry_stat(capability, name) is None:
        return None
    try:
        raw = json.loads(
            _read_bytes(
                capability, name, limit=_MAX_OBSIDIAN_BATCH_JOURNAL_BYTES
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError(
            "unmanaged Obsidian batch journal is malformed"
        ) from exc
    if not isinstance(raw, dict):
        raise PendingTransactionError(
            "unmanaged Obsidian batch journal is malformed"
        )
    return raw


def _parse_obsidian_batch_journal(
    raw: dict[str, Any],
    *,
    destination: str,
    anchor_identity: OutputIdentity,
    candidate_digests: Mapping[str, str],
) -> dict[str, Any]:
    fields = {
        "schema",
        "protocol_epoch",
        "operation",
        "state",
        "destination",
        "anchor_identity",
        "vault_identity",
        "prior_manifest",
        "candidate_digests",
        "items",
        "skipped",
        "stale",
        "manifest",
    }
    if (
        set(raw) != fields
        or raw.get("schema") != 1
        or raw.get("protocol_epoch") != 1
        or raw.get("operation") != "obsidian-batch"
        or raw.get("state") not in {"planned", "active", "manifest-committed"}
        or raw.get("destination") != destination
        or _identity_from_json(raw.get("anchor_identity")) != anchor_identity
        or raw.get("candidate_digests") != dict(sorted(candidate_digests.items()))
    ):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    vault_raw = raw.get("vault_identity")
    if vault_raw is not None:
        _identity_from_json(vault_raw)
    prior_manifest = raw.get("prior_manifest")
    if not isinstance(prior_manifest, dict) or set(prior_manifest) != {
        "path",
        "identity",
        "digest",
    } or prior_manifest.get("path") != ".graphify_obsidian_manifest.json":
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    prior_identity = prior_manifest.get("identity")
    prior_digest = prior_manifest.get("digest")
    if (prior_identity is None) != (prior_digest is None) or (
        prior_identity is not None
        and (
            _identity_from_json(prior_identity) is None
            or not _is_hex(prior_digest)
        )
    ):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    entry_names: dict[str, set[str]] = {}
    for key, allowed_states in (
        ("items", {"pending", "published", "foreign"}),
        (
            "stale",
            {"pending", "delete-attempt", "quarantined", "deleted", "foreign"},
        ),
    ):
        entries = raw.get(key)
        if not isinstance(entries, list) or len(entries) > _MAX_RECEIPT_ARTIFACTS:
            raise PendingTransactionError(
                "unmanaged Obsidian batch journal is malformed"
            )
        seen: set[str] = set()
        for entry in entries:
            expected = {
                "name",
                "digest",
                "predecessor_identity",
                "predecessor_digest",
                "state",
                "successor_identity",
            }
            if not isinstance(entry, dict) or set(entry) != expected:
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            if type(entry.get("name")) is not str:
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            name = _validated_relative_name(cast(str, entry["name"]))
            folded = name.casefold()
            if folded in seen or not _is_hex(entry.get("digest")):
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            seen.add(folded)
            if key == "items" and candidate_digests.get(name) != entry.get("digest"):
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            predecessor_identity = entry.get("predecessor_identity")
            predecessor_digest = entry.get("predecessor_digest")
            if (predecessor_identity is None) != (predecessor_digest is None) or (
                predecessor_identity is not None
                and (
                    _identity_from_json(predecessor_identity) is None
                    or not _is_hex(predecessor_digest)
                )
            ):
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            if key == "stale" and (
                predecessor_identity is None
                or entry.get("digest") != predecessor_digest
            ):
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            if entry.get("state") not in allowed_states:
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            successor = entry.get("successor_identity")
            if (key == "items" and (entry.get("state") == "published") != (
                successor is not None
            )) or (key == "stale" and successor is not None):
                raise PendingTransactionError(
                    "unmanaged Obsidian batch journal is malformed"
                )
            if successor is not None:
                _identity_from_json(successor)
        entry_names[key] = {cast(str, entry["name"]) for entry in entries}
    skipped = raw.get("skipped")
    if (
        not isinstance(skipped, list)
        or any(type(name) is not str for name in skipped)
        or len({cast(str, name).casefold() for name in skipped}) != len(skipped)
    ):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    skipped_names = {_validated_relative_name(cast(str, name)) for name in skipped}
    complete_namespace = (
        list(entry_names["items"])
        + list(entry_names["stale"])
        + list(skipped_names)
    )
    if len({name.casefold() for name in complete_namespace}) != len(
        complete_namespace
    ):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    if (
        entry_names["items"] & skipped_names
        or entry_names["items"] | skipped_names != set(candidate_digests)
        or entry_names["stale"] & (entry_names["items"] | skipped_names)
    ):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    manifest = raw.get("manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "digest",
        "state",
        "successor_identity",
    }:
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    if manifest.get("digest") is not None and not _is_hex(manifest.get("digest")):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    if manifest.get("state") not in {"pending", "committed"} or (
        manifest.get("state") == "committed"
    ) != (manifest.get("successor_identity") is not None):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    if manifest.get("successor_identity") is not None:
        _identity_from_json(manifest["successor_identity"])
    if (raw["state"] == "manifest-committed") != (
        manifest["state"] == "committed"
    ):
        raise PendingTransactionError("unmanaged Obsidian batch journal is malformed")
    return raw


def commit_unmanaged_obsidian_batch(
    path: Path | str,
    inventory: UnmanagedObsidianInventory,
    payloads: Mapping[str, bytes],
    *,
    failpoint: Callable[[str], None] | None = None,
) -> frozenset[str]:
    """Publish one external vault through a durable manifest-last batch journal."""
    if not isinstance(inventory, UnmanagedObsidianInventory):
        raise TypeError("external Obsidian inventory is required")
    if not isinstance(payloads, Mapping) or len(payloads) > _MAX_RECEIPT_ARTIFACTS:
        raise PendingTransactionError("external Obsidian batch exceeds bounds")
    candidates: dict[str, bytes] = {}
    total = 0
    for raw_name, payload in payloads.items():
        name = _validated_relative_name(raw_name)
        if name == ".graphify_obsidian_manifest.json" or type(payload) is not bytes:
            raise PendingTransactionError("external Obsidian batch is malformed")
        total += len(payload)
        if total > _MAX_RECEIPT_AGGREGATE_BYTES:
            raise PendingTransactionError("external Obsidian batch exceeds bounds")
        candidates[name] = payload
    _reject_casefold_collisions(tuple(candidates))
    candidate_digests = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(candidates.items())
    }
    owned_folded = {name.casefold(): name for name in inventory.manifest_names}
    for name in candidates:
        owned_name = owned_folded.get(name.casefold())
        if owned_name is not None and owned_name != name:
            raise PendingTransactionError(
                "external Obsidian ownership namespace is ambiguous"
            )
    destination = Path(path).expanduser().absolute()
    anchor_path = destination.parent
    while not anchor_path.exists():
        if anchor_path.parent == anchor_path:
            raise PendingTransactionError(
                "external Obsidian vault has no existing ancestor"
            )
        anchor_path = anchor_path.parent
    anchor_path = anchor_path.resolve(strict=True)
    try:
        relative_vault = _validated_relative_name(
            destination.relative_to(anchor_path).as_posix()
        )
    except ValueError as exc:
        raise PendingTransactionError(
            "external Obsidian vault identity changed"
        ) from exc
    canonical_destination = os.fspath(anchor_path / relative_vault)
    journal_name = (
        _OBSIDIAN_BATCH_PREFIX
        + hashlib.sha256(canonical_destination.encode("utf-8")).hexdigest()[:32]
        + ".json"
    )
    ancestor_paths = [anchor_path]
    ancestor = anchor_path.parent
    while True:
        ancestor_paths.append(ancestor)
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    with contextlib.ExitStack() as ancestor_stack:
        capabilities = [
            ancestor_stack.enter_context(pin_output(candidate, mutation=True))
            for candidate in ancestor_paths
        ]
        for pinned in reversed(capabilities):
            ancestor_stack.enter_context(_locked(pinned))
        anchor = capabilities[0]

        def validate_ancestor_chain() -> None:
            for pinned in capabilities:
                pinned.validate()
                if _managed_authority_present(pinned):
                    raise PendingTransactionError(
                        "external destination is inside managed graph authority"
                    )

        validate_ancestor_chain()
        raw = _load_obsidian_batch_journal(anchor, journal_name)
        validate_ancestor_chain()
        vault_capability: OutputCapability | None = None
        vault_stack = contextlib.ExitStack()
        try:
            if raw is None:
                current_capability, _created = _open_unmanaged_directory_relative(
                    anchor, relative_vault, create=False
                )
                if inventory.vault_identity is None:
                    if current_capability is not None:
                        current_capability.close()
                        raise PendingTransactionError(
                            "external Obsidian vault appeared before batch publication"
                        )
                elif (
                    current_capability is None
                    or current_capability.identity != inventory.vault_identity
                ):
                    if current_capability is not None:
                        current_capability.close()
                    raise PendingTransactionError(
                        "external Obsidian vault identity changed"
                    )
                if current_capability is not None:
                    vault_capability = vault_stack.enter_context(current_capability)
                    vault_stack.enter_context(_locked(vault_capability))
                    if _managed_authority_present(vault_capability):
                        raise PendingTransactionError(
                            "external destination is inside managed graph authority"
                        )
                    manifest_current = _unmanaged_obsidian_current(
                        vault_capability, ".graphify_obsidian_manifest.json"
                    )
                    expected_manifest = (
                        None
                        if inventory.manifest_identity is None
                        else (inventory.manifest_identity, inventory.manifest_digest)
                    )
                    if manifest_current != expected_manifest:
                        raise PendingTransactionError(
                            "external Obsidian ownership manifest changed"
                        )
                    for name, entry in inventory.files.items():
                        if _unmanaged_obsidian_current(vault_capability, name) != (
                            entry.identity,
                            entry.digest,
                        ):
                            raise PendingTransactionError(
                                "external Obsidian owned artifact changed"
                            )
                items: list[dict[str, Any]] = []
                accepted: set[str] = set()
                skipped: list[str] = []
                for name, digest in candidate_digests.items():
                    prior = inventory.files.get(name)
                    current = (
                        None
                        if vault_capability is None
                        else _unmanaged_obsidian_current(vault_capability, name)
                    )
                    if prior is None and current is not None:
                        skipped.append(name)
                        continue
                    accepted.add(name)
                    items.append(
                        {
                            "name": name,
                            "digest": digest,
                            "predecessor_identity": _obsidian_identity_json(
                                None if prior is None else prior.identity
                            ),
                            "predecessor_digest": (
                                None if prior is None else prior.digest
                            ),
                            "state": "pending",
                            "successor_identity": None,
                        }
                    )
                stale: list[dict[str, Any]] = []
                for name, prior in sorted(inventory.files.items()):
                    if name in accepted:
                        continue
                    current = (
                        None
                        if vault_capability is None
                        else _unmanaged_obsidian_current(vault_capability, name)
                    )
                    if current != (prior.identity, prior.digest):
                        continue
                    stale.append(
                        {
                            "name": name,
                            "digest": prior.digest,
                            "predecessor_identity": prior.identity.json(),
                            "predecessor_digest": prior.digest,
                            "state": "pending",
                            "successor_identity": None,
                        }
                    )
                raw = {
                    "schema": 1,
                    "protocol_epoch": 1,
                    "operation": "obsidian-batch",
                    "state": "planned",
                    "destination": canonical_destination,
                    "anchor_identity": anchor.identity.json(),
                    "vault_identity": (
                        None
                        if vault_capability is None
                        else vault_capability.identity.json()
                    ),
                    "prior_manifest": {
                        "path": ".graphify_obsidian_manifest.json",
                        "identity": _obsidian_identity_json(
                            inventory.manifest_identity
                        ),
                        "digest": inventory.manifest_digest,
                    },
                    "candidate_digests": dict(sorted(candidate_digests.items())),
                    "items": items,
                    "skipped": sorted(skipped),
                    "stale": stale,
                    "manifest": {
                        "digest": None,
                        "state": "pending",
                        "successor_identity": None,
                    },
                }
                future = json.loads(_obsidian_batch_bytes(raw).decode("utf-8"))
                worst_identity = {
                    "device": 18_446_744_073_709_551_615,
                    "inode": 18_446_744_073_709_551_615,
                }
                future["vault_identity"] = worst_identity
                future["state"] = "manifest-committed"
                for entry in future["items"]:
                    entry["state"] = "published"
                    entry["successor_identity"] = worst_identity
                for entry in future["stale"]:
                    entry["state"] = "delete-attempt"
                future["manifest"] = {
                    "digest": "f" * 64,
                    "state": "committed",
                    "successor_identity": worst_identity,
                }
                _obsidian_batch_bytes(future)
                validate_ancestor_chain()
                _replace_bytes(anchor, journal_name, _obsidian_batch_bytes(raw))
            raw = _parse_obsidian_batch_journal(
                raw,
                destination=canonical_destination,
                anchor_identity=anchor.identity,
                candidate_digests=candidate_digests,
            )
            if vault_capability is None:
                vault_identity_raw = raw["vault_identity"]
                if vault_identity_raw is None:
                    validate_ancestor_chain()
                    created, created_any = _open_unmanaged_directory_relative(
                        anchor, relative_vault, create=True
                    )
                    if created is None:
                        raise PendingTransactionError(
                            "external Obsidian vault creation failed"
                        )
                    vault_capability = vault_stack.enter_context(created)
                    vault_stack.enter_context(_locked(vault_capability))
                    if not created_any and _list_entries(vault_capability):
                        raise PendingTransactionError(
                            "external Obsidian vault creation is ambiguous"
                        )
                    raw["vault_identity"] = vault_capability.identity.json()
                    raw["state"] = "active"
                    _replace_bytes(anchor, journal_name, _obsidian_batch_bytes(raw))
                else:
                    existing, _created = _open_unmanaged_directory_relative(
                        anchor, relative_vault, create=False
                    )
                    if existing is None:
                        raise PendingTransactionError(
                            "external Obsidian vault identity changed"
                        )
                    vault_capability = vault_stack.enter_context(existing)
                    vault_stack.enter_context(_locked(vault_capability))
            expected_vault = _identity_from_json(raw["vault_identity"])
            if vault_capability.identity != expected_vault:
                raise PendingTransactionError(
                    "external Obsidian vault identity changed"
                )
            if _managed_authority_present(vault_capability):
                raise PendingTransactionError(
                    "external destination is inside managed graph authority"
                )

            def persist() -> None:
                validate_ancestor_chain()
                _replace_bytes(anchor, journal_name, _obsidian_batch_bytes(raw))

            def reopen_committed_manifest() -> None:
                manifest_state = raw["manifest"]
                if manifest_state["state"] != "committed":
                    return
                current_manifest = _unmanaged_obsidian_current(
                    vault_capability, ".graphify_obsidian_manifest.json"
                )
                expected_manifest = (
                    _identity_from_json(manifest_state["successor_identity"]),
                    manifest_state["digest"],
                )
                if current_manifest != expected_manifest:
                    raise PendingTransactionError(
                        "external Obsidian manifest identity changed"
                    )
                raw["prior_manifest"] = {
                    "path": ".graphify_obsidian_manifest.json",
                    "identity": expected_manifest[0].json(),
                    "digest": expected_manifest[1],
                }
                raw["manifest"] = {
                    "digest": None,
                    "state": "pending",
                    "successor_identity": None,
                }
                raw["state"] = "active"

            if raw["manifest"]["state"] == "committed" and (
                _obsidian_leaf_journal_present(
                    vault_capability,
                    vault=destination,
                    relative_name=".graphify_obsidian_manifest.json",
                )
            ):
                committed_payload = json.dumps(
                    {
                        "files": sorted(
                            cast(str, item["name"])
                            for item in raw["items"]
                            if item["state"] == "published"
                        )
                    },
                    indent=2,
                ).encode()
                if hashlib.sha256(committed_payload).hexdigest() != raw["manifest"][
                    "digest"
                ]:
                    raise PendingTransactionError(
                        "external Obsidian committed manifest changed"
                    )

                def checkpoint_committed_manifest(boundary: str) -> None:
                    if boundary != "after_unmanaged_leaf_publication":
                        return
                    live = _unmanaged_obsidian_current(
                        vault_capability, ".graphify_obsidian_manifest.json"
                    )
                    if live is None or live[1] != raw["manifest"]["digest"]:
                        raise PendingTransactionError(
                            "external Obsidian manifest identity changed"
                        )
                    raw["manifest"]["successor_identity"] = live[0].json()
                    persist()

                commit_unmanaged_bytes(
                    destination / ".graphify_obsidian_manifest.json",
                    committed_payload,
                    failpoint=checkpoint_committed_manifest,
                    expected_predecessor_digest=raw["prior_manifest"]["digest"],
                    expected_predecessor_identity=(
                        None
                        if raw["prior_manifest"]["identity"] is None
                        else _identity_from_json(
                            raw["prior_manifest"]["identity"]
                        )
                    ),
                )

            for item in raw["items"]:
                name = cast(str, item["name"])
                payload = candidates[name]
                current = _unmanaged_obsidian_current(vault_capability, name)
                successor_raw = item["successor_identity"]
                if item["state"] == "foreign":
                    continue
                predecessor_raw = item["predecessor_identity"]
                predecessor = (
                    None
                    if predecessor_raw is None
                    else _identity_from_json(predecessor_raw)
                )
                expected_current = (
                    None
                    if predecessor is None
                    else (predecessor, item["predecessor_digest"])
                )

                def checkpoint(boundary: str, *, entry: dict[str, Any] = item) -> None:
                    if boundary != "after_unmanaged_leaf_publication":
                        return
                    live = _unmanaged_obsidian_current(vault_capability, name)
                    if live is None or live[1] != entry["digest"]:
                        raise PendingTransactionError(
                            "external Obsidian successor identity changed"
                        )
                    entry["state"] = "published"
                    entry["successor_identity"] = live[0].json()
                    persist()
                    if failpoint is not None and predecessor is None:
                        failpoint("after_obsidian_new_leaf")

                if item["state"] == "published":
                    successor = _identity_from_json(successor_raw)
                    if current != (successor, item["digest"]):
                        if _obsidian_leaf_journal_present(
                            vault_capability,
                            vault=destination,
                            relative_name=name,
                        ):
                            try:
                                commit_unmanaged_bytes(
                                    destination / name,
                                    payload,
                                    failpoint=checkpoint,
                                    expected_predecessor_digest=item[
                                        "predecessor_digest"
                                    ],
                                    expected_predecessor_identity=predecessor,
                                )
                            except PendingTransactionError as exc:
                                if "competitor was restored" not in str(exc):
                                    raise
                        item["state"] = "foreign"
                        item["successor_identity"] = None
                        reopen_committed_manifest()
                        persist()
                        continue
                    if not _obsidian_leaf_journal_present(
                        vault_capability,
                        vault=destination,
                        relative_name=name,
                    ):
                        continue
                elif current != expected_current:
                    if not (
                        current is not None
                        and current[1] == item["digest"]
                        and _obsidian_leaf_journal_present(
                            vault_capability,
                            vault=destination,
                            relative_name=name,
                        )
                    ):
                        item["state"] = "foreign"
                        reopen_committed_manifest()
                        persist()
                        continue

                try:
                    commit_unmanaged_bytes(
                        destination / name,
                        payload,
                        failpoint=checkpoint,
                        expected_predecessor_digest=item["predecessor_digest"],
                        expected_predecessor_identity=predecessor,
                    )
                except PendingTransactionError as exc:
                    if "competitor was restored" not in str(exc):
                        raise
                    item["state"] = "foreign"
                    item["successor_identity"] = None
                    persist()
                    continue
            for item in raw["stale"]:
                name = cast(str, item["name"])
                delete_journal_present = _obsidian_leaf_journal_present(
                    vault_capability,
                    vault=destination,
                    relative_name=name,
                    prefix=_UNMANAGED_DELETE_PREFIX,
                )
                if item["state"] == "deleted" and not delete_journal_present:
                    continue
                if item["state"] == "foreign" and not delete_journal_present:
                    continue
                predecessor = _identity_from_json(item["predecessor_identity"])
                recovering_delete = item["state"] in {
                    "delete-attempt",
                    "quarantined",
                } or (
                    item["state"] in {"deleted", "foreign"}
                    and delete_journal_present
                )
                current = (
                    None
                    if recovering_delete
                    else _unmanaged_obsidian_current(vault_capability, name)
                )
                if current is None and not recovering_delete:
                    item["state"] = "deleted"
                    persist()
                elif not recovering_delete and current != (
                    predecessor,
                    item["predecessor_digest"],
                ):
                    item["state"] = "foreign"
                    persist()
                else:
                    def delete_checkpoint(
                        boundary: str, *, entry: dict[str, Any] = item
                    ) -> None:
                        if boundary == "before_unmanaged_delete_rename":
                            entry["state"] = "delete-attempt"
                            persist()
                            if failpoint is not None:
                                failpoint("before_obsidian_stale_delete_rename")
                        elif boundary == "after_unmanaged_delete_quarantine":
                            entry["state"] = "quarantined"
                            persist()
                            if failpoint is not None:
                                failpoint("after_obsidian_stale_quarantine")
                        elif boundary == "after_unmanaged_delete_restored_foreign":
                            if failpoint is not None:
                                failpoint("after_obsidian_stale_restored_foreign")
                        elif boundary == "after_unmanaged_delete_callback":
                            if failpoint is not None:
                                failpoint("after_obsidian_stale_delete_callback")

                    def foreign_checkpoint(
                        *, entry: dict[str, Any] = item
                    ) -> None:
                        entry["state"] = "foreign"
                        persist()

                    def deleted_checkpoint(
                        *, entry: dict[str, Any] = item
                    ) -> None:
                        entry["state"] = "deleted"
                        persist()

                    outcome = commit_unmanaged_unlink(
                        destination / name,
                        expected_identity=predecessor,
                        expected_digest=cast(str, item["predecessor_digest"]),
                        failpoint=delete_checkpoint,
                        foreign_callback=foreign_checkpoint,
                        deleted_callback=deleted_checkpoint,
                    )
                    item["state"] = outcome
                    persist()
                    if failpoint is not None and outcome == "deleted":
                        failpoint("after_obsidian_stale_deletion")
            published = {
                cast(str, item["name"])
                for item in raw["items"]
                if item["state"] == "published"
                and _unmanaged_obsidian_current(
                    vault_capability, cast(str, item["name"])
                )
                == (
                    _identity_from_json(item["successor_identity"]),
                    item["digest"],
                )
            }
            manifest_payload = json.dumps(
                {"files": sorted(published)}, indent=2
            ).encode()
            manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
            manifest = raw["manifest"]

            def manifest_checkpoint(boundary: str) -> None:
                if boundary != "after_unmanaged_leaf_publication":
                    return
                live = _unmanaged_obsidian_current(
                    vault_capability, ".graphify_obsidian_manifest.json"
                )
                if live is None or live[1] != manifest_digest:
                    raise PendingTransactionError(
                        "external Obsidian manifest identity changed"
                    )
                manifest["digest"] = manifest_digest
                manifest["state"] = "committed"
                manifest["successor_identity"] = live[0].json()
                raw["state"] = "manifest-committed"
                persist()
                if failpoint is not None:
                    failpoint("after_obsidian_manifest_commit")

            if manifest["state"] == "pending":
                prior = raw["prior_manifest"]

                commit_unmanaged_bytes(
                    destination / ".graphify_obsidian_manifest.json",
                    manifest_payload,
                    failpoint=manifest_checkpoint,
                    expected_predecessor_digest=prior["digest"],
                    expected_predecessor_identity=(
                        None
                        if prior["identity"] is None
                        else _identity_from_json(prior["identity"])
                    ),
                )
            elif _obsidian_leaf_journal_present(
                vault_capability,
                vault=destination,
                relative_name=".graphify_obsidian_manifest.json",
            ):
                prior = raw["prior_manifest"]
                try:
                    commit_unmanaged_bytes(
                        destination / ".graphify_obsidian_manifest.json",
                        manifest_payload,
                        failpoint=manifest_checkpoint,
                        expected_predecessor_digest=prior["digest"],
                        expected_predecessor_identity=(
                            None
                            if prior["identity"] is None
                            else _identity_from_json(prior["identity"])
                        ),
                    )
                except PendingTransactionError as exc:
                    if "competitor was restored" not in str(exc):
                        raise
            live_manifest = _unmanaged_obsidian_current(
                vault_capability, ".graphify_obsidian_manifest.json"
            )
            if live_manifest != (
                _identity_from_json(manifest["successor_identity"]),
                manifest["digest"],
            ):
                raise PendingTransactionError(
                    "external Obsidian manifest identity changed"
                )
            os.fsync(vault_capability.fd)
            os.fsync(anchor.fd)
            validate_ancestor_chain()
            _unlink(anchor, journal_name)
            validate_ancestor_chain()
            return frozenset(published)
        finally:
            vault_stack.close()


@dataclass
class _LegacyInventoryBudget:
    remaining: int = -1
    count: int = 0

    def __post_init__(self) -> None:
        if self.remaining < 0:
            self.remaining = _MAX_RECEIPT_AGGREGATE_BYTES

    def retain(self, name: str, payload: bytes) -> None:
        if self.count >= _MAX_RECEIPT_ARTIFACTS or len(payload) > self.remaining:
            raise PendingTransactionError("legacy managed inventory exceeds bounds")
        self.count += 1
        self.remaining -= len(payload)


def _read_open_regular(fd: int, *, limit: int, label: str) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise PendingTransactionError(f"unsafe legacy artifact: {label}")
    payload = bytearray()
    while len(payload) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    after = os.fstat(fd)
    if (
        len(payload) > limit
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise PendingTransactionError(f"legacy artifact identity changed: {label}")
    return bytes(payload)


def _legacy_owned_dynamic_inventory(
    capability: OutputCapability,
    *,
    budget: _LegacyInventoryBudget | None = None,
) -> dict[str, bytes]:
    """Inventory receiptless dynamic exports through pinned relative handles."""
    inventory: dict[str, bytes] = {}
    ledger = _LegacyInventoryBudget() if budget is None else budget
    scanned = 0

    def retain(name: str, payload: bytes) -> None:
        name = _validated_relative_name(name)
        if (
            len(name) > _MAX_QUEUE_PATH_LENGTH
            or len(Path(name).parts) > 32
            or len(inventory) >= _MAX_RECEIPT_ARTIFACTS
        ):
            raise PendingTransactionError("legacy dynamic path exceeds bound")
        ledger.retain(name, payload)
        inventory[name] = payload

    def walk_wiki(parent: OutputCapability, parts: tuple[str, ...]) -> None:
        nonlocal scanned
        if len(parts) > 32:
            raise PendingTransactionError("legacy dynamic path exceeds bound")
        for child in sorted(_list_entries(parent)):
            if child in {".", ".."}:
                continue
            scanned += 1
            if scanned > _MAX_RECEIPT_ARTIFACTS:
                raise PendingTransactionError("legacy dynamic inventory exceeds bounds")
            relative = (*parts, child)
            parent.validate()
            if _PLATFORM == "windows":
                try:
                    child_fd = _open_windows_relative_fd(
                        parent, child, directory=True
                    )
                except OSError:
                    child_fd = _open_windows_relative_fd(
                        parent, child, directory=False
                    )
            else:
                info = os.stat(child, dir_fd=parent.fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise PendingTransactionError("legacy wiki contains a symlink")
                child_fd = os.open(
                    child,
                    os.O_RDONLY
                    | (getattr(os, "O_DIRECTORY", 0) if stat.S_ISDIR(info.st_mode) else 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent.fd,
                )
            try:
                opened = os.fstat(child_fd)
                if bool(getattr(opened, "st_reparse_tag", 0)):
                    raise PendingTransactionError(
                        "legacy wiki contains a reparse point"
                    )
                if stat.S_ISDIR(opened.st_mode):
                    nested = OutputCapability(
                        parent.path / child,
                        OutputIdentity(opened.st_dev, opened.st_ino),
                        child_fd,
                    )
                    child_fd = -1
                    try:
                        walk_wiki(nested, relative)
                    finally:
                        nested.close()
                elif stat.S_ISREG(opened.st_mode) and child.endswith(".md"):
                    label = Path(*relative).as_posix()
                    retain(
                        label,
                        _read_open_regular(
                            child_fd, limit=ledger.remaining, label=label
                        ),
                    )
                elif not stat.S_ISREG(opened.st_mode):
                    raise PendingTransactionError("legacy wiki contains an unsafe entry")
                parent.validate()
            finally:
                if child_fd >= 0:
                    os.close(child_fd)

    try:
        wiki_fd = (
            _open_windows_relative_fd(capability, "wiki", directory=True)
            if _PLATFORM == "windows"
            else os.open(
                "wiki",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=capability.fd,
            )
        )
    except FileNotFoundError:
        pass
    else:
        wiki_info = os.fstat(wiki_fd)
        wiki = OutputCapability(
            capability.path / "wiki",
            OutputIdentity(wiki_info.st_dev, wiki_info.st_ino),
            wiki_fd,
        )
        try:
            walk_wiki(wiki, ("wiki",))
        finally:
            wiki.close()

    manifest_name = ".graphify_obsidian_manifest.json"

    def retain_vault_manifest(vault: OutputCapability, parts: tuple[str, ...]) -> None:
        if _entry_stat(vault, manifest_name) is None:
            return
        scoped_manifest = Path(*parts, manifest_name).as_posix()
        if ledger.count >= _MAX_RECEIPT_ARTIFACTS or ledger.remaining <= 0:
            raise PendingTransactionError("legacy dynamic inventory exceeds bounds")
        manifest_payload = _read_bytes(
            vault, manifest_name, min(_MAX_STATE_BYTES, ledger.remaining)
        )
        try:
            manifest = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PendingTransactionError("legacy Obsidian manifest is malformed") from exc
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"files"}
            or not isinstance(files, list)
            or len(files) > _MAX_RECEIPT_ARTIFACTS
            or not all(type(name) is str for name in files)
        ):
            raise PendingTransactionError("legacy Obsidian manifest is malformed")
        scoped_manifest = _validated_relative_name(scoped_manifest)
        if scoped_manifest in inventory:
            raise PendingTransactionError("legacy Obsidian manifest is malformed")
        ledger.retain(scoped_manifest, manifest_payload)
        inventory[scoped_manifest] = manifest_payload
        scoped_names: list[str] = []
        for raw_name in files:
            name = _validated_relative_name(cast(str, raw_name))
            scoped = _validated_relative_name(Path(*parts, name).as_posix())
            if name == manifest_name or scoped in inventory:
                raise PendingTransactionError("legacy Obsidian manifest is malformed")
            scoped_names.append(scoped)
        _reject_casefold_collisions((scoped_manifest, *scoped_names))
        for raw_name, scoped in zip(cast(list[str], files), scoped_names, strict=True):
            retain(
                scoped,
                _read_relative_bytes(vault, raw_name, ledger.remaining),
            )

    def discover_vaults(parent: OutputCapability, parts: tuple[str, ...]) -> None:
        nonlocal scanned
        if len(parts) > 32:
            raise PendingTransactionError("legacy dynamic path exceeds bound")
        retain_vault_manifest(parent, parts)
        for child in sorted(_list_entries(parent)):
            scanned += 1
            if scanned > _MAX_RECEIPT_ARTIFACTS:
                raise PendingTransactionError("legacy dynamic inventory exceeds bounds")
            if child in {"wiki", ".", ".."} or child.startswith(".graphify"):
                continue
            try:
                if _PLATFORM == "windows":
                    child_fd = _open_windows_relative_fd(
                        parent, child, directory=True
                    )
                else:
                    info = os.stat(child, dir_fd=parent.fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    child_fd = os.open(
                        child,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent.fd,
                    )
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                if _PLATFORM != "windows":
                    raise
                try:
                    file_fd = _open_windows_relative_fd(
                        parent, child, directory=False
                    )
                except FileNotFoundError:
                    continue
                try:
                    file_info = os.fstat(file_fd)
                    if bool(getattr(file_info, "st_reparse_tag", 0)):
                        raise PendingTransactionError(
                            "legacy vault contains a reparse point"
                        )
                finally:
                    os.close(file_fd)
                continue
            opened = os.fstat(child_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child_fd)
                continue
            if bool(getattr(opened, "st_reparse_tag", 0)):
                os.close(child_fd)
                raise PendingTransactionError("legacy vault contains a reparse point")
            nested = OutputCapability(
                parent.path / child,
                OutputIdentity(opened.st_dev, opened.st_ino),
                child_fd,
            )
            try:
                parent.validate()
                discover_vaults(nested, (*parts, child))
                parent.validate()
            finally:
                nested.close()

    discover_vaults(capability, ())
    _reject_casefold_collisions(tuple(inventory))
    return inventory


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
            legacy_budget = _LegacyInventoryBudget()
            legacy_budget.retain(graph_path.name, payload)
            legacy_inventory = {graph_path.name: payload}
            manifest_payload = None
            for name in MANAGED_PUBLICATION_PATHS:
                if name == graph_path.name:
                    continue
                try:
                    artifact_payload = _read_relative_bytes(
                        capability, name, legacy_budget.remaining
                    )
                except PendingTransactionError as exc:
                    if "is missing" in str(exc):
                        continue
                    raise
                legacy_budget.retain(name, artifact_payload)
                legacy_inventory[name] = artifact_payload
                if name == "manifest.json":
                    manifest_payload = artifact_payload
            legacy_inventory.update(
                _legacy_owned_dynamic_inventory(
                    capability, budget=legacy_budget
                )
            )
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


def open_external_graph_snapshot(
    path: Path | str, *, retain_artifacts: Sequence[str] = ()
) -> GraphSnapshot:
    """Read an explicit unmanaged graph and explicitly selected sibling leaves."""
    requested = Path(path).expanduser()
    output = requested.parent.resolve(strict=True)
    graph_name = _validated_shallow_name(requested.name)
    retain = tuple(_validated_shallow_name(name) for name in retain_artifacts)
    _reject_casefold_collisions((graph_name, *retain))
    graph_path = output / graph_name
    with pin_output(output, mutation=False) as capability:
        if _coordination_present(capability):
            raise PendingTransactionError(
                "explicit graph has managed coordination authority"
            )
        from graphify.security import _max_graph_file_bytes

        fd = (
            _open_windows_relative_fd(capability, graph_name)
            if _PLATFORM == "windows"
            else os.open(
                graph_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=capability.fd,
            )
        )
        try:
            opened_graph = os.fstat(fd)
            payload = _read_open_regular(
                fd, limit=_max_graph_file_bytes(), label=graph_name
            )
        finally:
            os.close(fd)
        artifacts = {graph_name: payload}
        selected_identities: dict[str, tuple[int, int] | None] = {
            graph_name: (opened_graph.st_dev, opened_graph.st_ino)
        }
        remaining = _MAX_RECEIPT_AGGREGATE_BYTES - len(payload)
        for name in retain:
            before = _entry_stat(capability, name)
            if before is None:
                selected_identities[name] = None
                continue
            retained = _read_bytes(capability, name, remaining)
            after = _entry_stat(capability, name)
            identity = (before.st_dev, before.st_ino)
            if after is None or (after.st_dev, after.st_ino) != identity:
                raise PendingTransactionError(
                    f"external snapshot entry identity changed: {name}"
                )
            remaining -= len(retained)
            artifacts[name] = retained
            selected_identities[name] = identity
        for name, expected in selected_identities.items():
            current = _entry_stat(capability, name)
            current_identity = (
                None if current is None else (current.st_dev, current.st_ino)
            )
            if current_identity != expected:
                raise PendingTransactionError(
                    f"external snapshot entry identity changed: {name}"
                )
        if _coordination_present(capability):
            raise PendingTransactionError(
                "explicit graph has managed coordination authority"
            )
        capability.validate()
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("malformed external graph payload") from exc
    if not isinstance(data, dict):
        raise PendingTransactionError("malformed external graph payload")
    graph_meta = data.get("graph")
    if isinstance(graph_meta, dict) and GRAPH_WATERMARK_KEY in graph_meta:
        raise PendingTransactionError("explicit graph has managed watermark authority")
    return GraphSnapshot(
        data,
        None,
        graph_path,
        payload,
        hashlib.sha256(payload).hexdigest(),
        None,
        artifacts,
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
            for name in tuple(
                dict.fromkeys((*prior_inventory, *MANAGED_PUBLICATION_PATHS))
            ):
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
    ids = [str(item["id"]) for item in validated]
    if len(ids) != len(set(ids)):
        raise PendingTransactionError("duplicate rebuild intent id")
    payload = b"".join(_json_bytes(item) + b"\n" for item in validated)
    if len(payload) > _MAX_STATE_BYTES:
        raise PendingTransactionError("rebuild queue exceeds serialized budget")
    return payload


def _write_queue(capability: OutputCapability, name: str, items: list[dict[str, Any]]) -> None:
    payload = _queue_payload(items)
    _replace_bytes(capability, name, payload)


def _canonical_enqueue_transform(
    predecessor: list[dict[str, Any]],
    candidate: dict[str, Any],
    operation: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the one legal queue successor and represented receipt item."""
    _queue_payload(predecessor)
    candidate = _validate_queue_item(candidate)
    candidate_identity = {
        "schema": candidate["schema"],
        "kind": candidate["kind"],
        "intent": candidate["intent"],
        "root": candidate["root"],
        "changed_paths": candidate["changed_paths"],
        "semantic": candidate["semantic"]
        or (
            candidate["kind"] == "full"
            and any(
                value["root"] == candidate["root"] and value["semantic"]
                for value in predecessor
            )
        ),
        "source": candidate["source"],
    }
    canonical_candidate = {
        **candidate_identity,
        "id": hashlib.sha256(_json_bytes(candidate_identity)).hexdigest(),
        "time": candidate["time"],
    }
    if candidate != canonical_candidate:
        raise PendingTransactionError("enqueue candidate identity changed")
    candidate = canonical_candidate
    existing = next(
        (value for value in predecessor if value["id"] == candidate["id"]), None
    )
    represented = candidate if existing is None else existing
    successor = list(predecessor)
    covering = next(
        (
            value
            for value in predecessor
            if value["root"] == candidate["root"] and value["kind"] == "full"
        ),
        None,
    )
    expected_operation = (
        "replace-root"
        if candidate["kind"] == "full"
        else "covered-by-full"
        if covering is not None
        else "append"
    )
    if operation != expected_operation:
        raise PendingTransactionError("enqueue operation is malformed")
    if operation == "replace-root":
        successor = [
            value for value in predecessor if value["root"] != candidate["root"]
        ]
        successor.append(represented)
    elif operation == "covered-by-full":
        represented = cast(dict[str, Any], covering)
    elif existing is None:
        successor.append(candidate)
    _queue_payload(successor)
    return successor, represented


def _derive_enqueue_predecessor_mode(
    capability: OutputCapability,
    *,
    expected: tuple[DrainerTuple, str, dict[str, Any]] | None,
    protocol: Mapping[str, object] | None,
) -> str:
    """Derive the only predecessor class admitted by pinned durable authority."""
    live = _read_transaction(capability)
    preserved = _read_predecessor_authority(capability)
    if preserved is not None and preserved[3]["state"] != "preserved-complete":
        _validate_cancellation_state_locked(capability, preserved)
        raise PendingTransactionError("operational recovery required")
    if protocol is None:
        if live is not None or preserved is not None:
            raise PendingTransactionError("enqueue pristine predecessor is incomplete")
        if expected is None:
            return "pristine"
        if (
            expected[1] == "reserved"
            and expected[0].generation == 1
            and expected[2].get("predecessor_receipt") is None
        ):
            return "pristine"
        raise PendingTransactionError("enqueue pristine predecessor is malformed")
    if protocol.get("state") == "COMPLETE":
        if live is not None:
            if expected is None or expected[0] != live.drainer:
                raise PendingTransactionError("enqueue live predecessor changed")
            return "live"
        if expected is None:
            return "missing"
        if expected[1] == "complete":
            return "complete"
        if expected[1] == "reserved":
            if (
                preserved is None
                or preserved[3].get("successor_generation")
                != expected[0].generation
                or expected[2].get("predecessor_receipt") != preserved[2]
            ):
                raise PendingTransactionError("enqueue reserved predecessor changed")
            return "reserved"
        raise PendingTransactionError("enqueue completed predecessor is malformed")
    if live is None or expected is None or expected[0] != live.drainer:
        raise PendingTransactionError("enqueue live predecessor changed")
    return "live"


def _enqueue_journal_from_json(
    capability: OutputCapability, raw: object
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "protocol_epoch",
        "state",
        "output_identity",
        "item",
        "item_digest",
        "operation",
        "candidate",
        "predecessor_queue",
        "predecessor_queue_digest",
        "successor_queue",
        "successor_queue_digest",
        "predecessor_mode",
        "predecessor_protocol",
        "predecessor_receipt_digest",
        "predecessor_graph_digest",
        "legacy_pending_name",
        "expected_drainer",
        "successor_drainer",
        "mutate_drainer",
    }:
        raise PendingTransactionError("malformed enqueue journal")
    item = _validate_queue_item(raw.get("item"))
    candidate = _validate_queue_item(raw.get("candidate"))
    predecessor_queue = raw.get("predecessor_queue")
    successor_queue = raw.get("successor_queue")
    if not isinstance(predecessor_queue, list) or not isinstance(successor_queue, list):
        raise PendingTransactionError("malformed enqueue journal")
    predecessor_payload = _queue_payload(cast(list[dict[str, Any]], predecessor_queue))
    successor_payload = _queue_payload(cast(list[dict[str, Any]], successor_queue))
    canonical_successor, canonical_item = _canonical_enqueue_transform(
        cast(list[dict[str, Any]], predecessor_queue),
        candidate,
        cast(str, raw.get("operation")),
    )
    item_identity = {
        key: item[key]
        for key in (
            "schema",
            "kind",
            "intent",
            "root",
            "changed_paths",
            "semantic",
            "source",
        )
    }
    expected_raw = raw.get("expected_drainer")
    expected = (
        None
        if expected_raw is None
        else _drainer_state_from_json(capability, expected_raw)
    )
    successor = _drainer_state_from_json(capability, raw.get("successor_drainer"))
    raw_legacy_name = raw.get("legacy_pending_name")
    if (
        raw.get("schema") != 1
        or raw.get("protocol_epoch") != 1
        or raw.get("state") not in {"planned", "queued", "reserved"}
        or raw.get("output_identity") != capability.identity.json()
        or item["id"] != hashlib.sha256(_json_bytes(item_identity)).hexdigest()
        or raw.get("item_digest") != hashlib.sha256(_json_bytes(item)).hexdigest()
        or item != canonical_item
        or successor_queue != canonical_successor
        or raw.get("predecessor_queue_digest")
        != hashlib.sha256(predecessor_payload).hexdigest()
        or raw.get("successor_queue_digest")
        != hashlib.sha256(successor_payload).hexdigest()
        or raw.get("predecessor_mode")
        not in {"pristine", "missing", "complete", "reserved", "live"}
        or (
            raw.get("predecessor_protocol") is not None
            and not isinstance(raw.get("predecessor_protocol"), dict)
        )
        or (
            raw.get("predecessor_receipt_digest") is not None
            and not _is_hex(raw.get("predecessor_receipt_digest"))
        )
        or (
            raw.get("predecessor_graph_digest") is not None
            and not _is_hex(raw.get("predecessor_graph_digest"))
        )
        or (
            raw_legacy_name is not None
            and (
                type(raw_legacy_name) is not str
                or _validated_shallow_name(cast(str, raw_legacy_name))
                != raw_legacy_name
            )
        )
        or type(raw.get("mutate_drainer")) is not bool
        or (raw["mutate_drainer"] and successor[1] != "reserved")
        or (
            raw["mutate_drainer"]
            and expected is not None
            and (
                expected[1] != "complete"
                or successor[0].generation != expected[0].generation + 1
            )
        )
        or (
            not raw["mutate_drainer"]
            and (expected is None or successor != expected)
        )
    ):
        raise PendingTransactionError("malformed enqueue journal")
    return dict(raw)


def _read_enqueue_journal(capability: OutputCapability) -> dict[str, Any] | None:
    raw = _load_json(capability, ENQUEUE_FILE)
    return None if raw is None else _enqueue_journal_from_json(capability, raw)


def _validate_enqueue_journal_current(
    capability: OutputCapability, journal: Mapping[str, object]
) -> None:
    """Validate one exact durable enqueue phase without changing it."""
    item = cast(dict[str, Any], journal["item"])
    queue = _read_queue(capability)
    predecessor_queue = cast(list[dict[str, Any]], journal["predecessor_queue"])
    successor_queue = cast(list[dict[str, Any]], journal["successor_queue"])
    expected_raw = journal["expected_drainer"]
    expected = (
        None
        if expected_raw is None
        else _drainer_state_from_json(capability, expected_raw)
    )
    successor = _drainer_state_from_json(capability, journal["successor_drainer"])
    current = _read_drainer(capability)
    state = journal["state"]
    if state == "planned":
        valid = current == expected and queue in (predecessor_queue, successor_queue)
    elif state == "queued":
        valid = queue == successor_queue and (current == expected or current == successor)
    else:
        valid = state == "reserved" and queue == successor_queue and current == successor
    if not valid:
        raise PendingTransactionError("enqueue journal durable state changed")
    protocol = _read_protocol(capability)
    mode = _derive_enqueue_predecessor_mode(
        capability, expected=expected, protocol=protocol
    )
    if mode != journal["predecessor_mode"]:
        raise PendingTransactionError("enqueue predecessor mode changed")
    if mode == "pristine" and (
        successor[0].generation != 1
        or successor[2].get("predecessor_receipt") is not None
    ):
        raise PendingTransactionError("enqueue pristine successor is malformed")
    if protocol != journal["predecessor_protocol"]:
        raise PendingTransactionError("enqueue predecessor protocol changed")
    if mode == "pristine":
        allowed = [ENQUEUE_FILE, *_SAFE_GRAPHLESS_RUNTIME_ENTRIES]
        if queue:
            allowed.append(QUEUE_FILE)
        if current is not None:
            allowed.append(DRAINER_FILE)
        legacy_name = journal.get("legacy_pending_name")
        if isinstance(legacy_name, str):
            allowed.append(legacy_name)
            if _entry_stat(capability, LEGACY_PENDING_STATE_FILE) is not None:
                _validated_legacy_pending_bridge(
                    capability, selected_name=legacy_name
                )
                allowed.append(LEGACY_PENDING_STATE_FILE)
        _validate_pristine_or_legacy_graph(
            capability, allowed_without_graph=allowed
        )
        graph_info = _entry_stat(capability, "graph.json")
        from graphify.security import _max_graph_file_bytes

        graph_digest = (
            None
            if graph_info is None
            else hashlib.sha256(
                _read_relative_bytes(
                    capability, "graph.json", _max_graph_file_bytes()
                )
            ).hexdigest()
        )
        if graph_digest != journal["predecessor_graph_digest"]:
            raise PendingTransactionError("enqueue predecessor graph changed")
    elif protocol is not None and protocol.get("state") == "COMPLETE":
        live = _read_transaction(capability) if mode == "live" else None
        receipt, receipt_digest, _inventory = _validate_receipt_locked(
            capability,
            transaction=live,
            require_closed=mode == "complete" and current == expected,
            allow_missing_completed_drainer=mode == "missing",
        )
        graph_name = cast(str, receipt.get("graph_name", "graph.json"))
        if (
            receipt_digest != journal["predecessor_receipt_digest"]
            or receipt["artifact_digests"].get(graph_name)
            != journal["predecessor_graph_digest"]
            or (
                mode == "complete"
                and (
                    expected is None
                    or expected[1] != "complete"
                    or _drainer_from_json(receipt.get("drainer")) != expected[0]
                )
            )
        ):
            raise PendingTransactionError("enqueue completed predecessor changed")
        if journal["mutate_drainer"] and (
            successor[0].generation != int(receipt["generation"]) + 1
            or successor[2].get("predecessor_receipt") != receipt_digest
        ):
            raise PendingTransactionError("enqueue completed successor changed")
        if mode == "reserved":
            preserved = _read_predecessor_authority(capability)
            if (
                expected is None
                or expected[1] != "reserved"
                or preserved is None
                or preserved[2] != receipt_digest
            ):
                raise PendingTransactionError("enqueue reserved predecessor changed")
    else:
        live = _read_transaction(capability)
        if live is None:
            raise PendingTransactionError("enqueue live predecessor disappeared")
        _validate_durable_live_binding(
            capability, live, protocol=protocol, drainer=current
        )


def _recover_enqueue_journal(
    capability: OutputCapability,
    *,
    failpoint: Callable[[str], None] | None = None,
) -> QueueReceipt | None:
    journal = _read_enqueue_journal(capability)
    if journal is None:
        return None
    _validate_enqueue_journal_current(capability, journal)
    item = cast(dict[str, Any], journal["item"])
    queue = _read_queue(capability)
    predecessor_queue = cast(list[dict[str, Any]], journal["predecessor_queue"])
    successor_queue = cast(list[dict[str, Any]], journal["successor_queue"])
    if queue == predecessor_queue:
        _write_queue(capability, QUEUE_FILE, successor_queue)
    elif queue != successor_queue:
        raise PendingTransactionError("enqueue queue predecessor changed")
    journal["state"] = "queued"
    _replace_bytes(capability, ENQUEUE_FILE, _json_bytes(journal))
    if failpoint is not None:
        failpoint("after_enqueue_queue")
    expected_raw = journal["expected_drainer"]
    expected = (
        None
        if expected_raw is None
        else _drainer_state_from_json(capability, expected_raw)
    )
    successor = _drainer_state_from_json(capability, journal["successor_drainer"])
    current = _read_drainer(capability)
    if current != successor:
        if current != expected:
            raise PendingTransactionError("enqueue drainer predecessor changed")
        if not journal["mutate_drainer"]:
            raise PendingTransactionError("enqueue drainer authority changed")
        if expected is not None and expected[1] == "complete":
            protocol = _read_protocol(capability)
            receipt, receipt_digest, _inventory = _validate_receipt_locked(
                capability, require_closed=True
            )
            if protocol is None or int(receipt["generation"]) + 1 != successor[0].generation:
                raise PendingTransactionError("enqueue predecessor authority changed")
            _write_predecessor_authority(
                capability, protocol, expected, receipt_digest
            )
        _replace_bytes(
            capability,
            DRAINER_FILE,
            _json_bytes(cast(dict[str, Any], journal["successor_drainer"])),
        )
    journal["state"] = "reserved"
    _replace_bytes(capability, ENQUEUE_FILE, _json_bytes(journal))
    if failpoint is not None:
        failpoint("after_enqueue_drainer")
    _unlink(capability, ENQUEUE_FILE)
    if failpoint is not None:
        failpoint("after_enqueue_retired")
    return QueueReceipt(str(item["id"]), successor[0])


def _fence_pending_enqueue_locked(
    capability: OutputCapability,
    *,
    recover: bool = False,
) -> QueueReceipt | None:
    """Validate one pending enqueue before any unrelated mutation."""
    journal = _read_enqueue_journal(capability)
    if journal is None:
        return None
    _validate_enqueue_journal_current(capability, journal)
    if not recover:
        raise PendingTransactionError(
            "enqueue transition requires operational recovery"
        )
    return _recover_enqueue_journal(capability)


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
    failpoint: Callable[[str], None] | None = None,
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
        _recover_enqueue_journal(capability, failpoint=failpoint)
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
        predecessor_graph_digest: str | None = None
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
                    graph_name = cast(str, receipt.get("graph_name", "graph.json"))
                    predecessor_graph_digest = cast(
                        str, receipt["artifact_digests"][graph_name]
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
                graph_name = cast(str, receipt.get("graph_name", "graph.json"))
                predecessor_graph_digest = cast(
                    str, receipt["artifact_digests"][graph_name]
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
        predecessor_queue = _read_queue(capability)
        candidate_identity = {
            "schema": 1,
            "kind": kind,
            "intent": intent or kind,
            "root": str(root_path),
            "changed_paths": None if changed_paths is None and not durable_paths else durable_paths,
            "semantic": bool(semantic) or (
                kind == "full"
                and any(
                    value["root"] == str(root_path) and value["semantic"]
                    for value in predecessor_queue
                )
            ),
            "source": source,
        }
        candidate = {
            **candidate_identity,
            "id": hashlib.sha256(_json_bytes(candidate_identity)).hexdigest(),
            "time": time.time() if now is None else now,
        }
        operation = (
            "replace-root"
            if kind == "full"
            else "covered-by-full"
            if any(
                value["root"] == candidate["root"] and value["kind"] == "full"
                for value in predecessor_queue
            )
            else "append"
        )
        queued, item = _canonical_enqueue_transform(
            predecessor_queue, candidate, operation
        )
        if protocol is None and not receipt_present:
            predecessor_mode = "pristine"
            graph_info = _entry_stat(capability, "graph.json")
            if graph_info is not None:
                from graphify.security import _max_graph_file_bytes

                predecessor_graph_digest = hashlib.sha256(
                    _read_relative_bytes(
                        capability, "graph.json", _max_graph_file_bytes()
                    )
                ).hexdigest()
        elif protocol is not None and protocol.get("state") == "COMPLETE":
            predecessor_mode = _derive_enqueue_predecessor_mode(
                capability, expected=existing_drainer, protocol=protocol
            )
            if predecessor_mode in {"live", "reserved"}:
                live = _read_transaction(capability)
                completed, predecessor_receipt, _inventory = _validate_receipt_locked(
                    capability,
                    transaction=live if predecessor_mode == "live" else None,
                )
                graph_name = cast(
                    str, completed.get("graph_name", "graph.json")
                )
                predecessor_graph_digest = cast(
                    str, completed["artifact_digests"][graph_name]
                )
        else:
            predecessor_mode = _derive_enqueue_predecessor_mode(
                capability, expected=existing_drainer, protocol=protocol
            )
        successor_drainer = (
            {
                "schema": 1,
                "protocol_epoch": 1,
                **_drainer_json(drainer),
                "state": "reserved",
                "lease_deadline": (time.time() if now is None else now) + 30,
                "predecessor_receipt": predecessor_receipt,
            }
            if reserve_drainer
            else cast(tuple[DrainerTuple, str, dict[str, Any]], existing_drainer)[2]
        )
        journal = {
            "schema": 1,
            "protocol_epoch": 1,
            "state": "planned",
            "output_identity": capability.identity.json(),
            "item": item,
            "item_digest": hashlib.sha256(_json_bytes(item)).hexdigest(),
            "operation": operation,
            "candidate": candidate,
            "predecessor_queue": predecessor_queue,
            "predecessor_queue_digest": hashlib.sha256(
                _queue_payload(predecessor_queue)
            ).hexdigest(),
            "successor_queue": queued,
            "successor_queue_digest": hashlib.sha256(
                _queue_payload(queued)
            ).hexdigest(),
            "predecessor_mode": predecessor_mode,
            "predecessor_protocol": protocol,
            "predecessor_receipt_digest": predecessor_receipt,
            "predecessor_graph_digest": predecessor_graph_digest,
            "legacy_pending_name": legacy_pending_name,
            "expected_drainer": (
                None if existing_drainer is None else existing_drainer[2]
            ),
            "successor_drainer": successor_drainer,
            "mutate_drainer": reserve_drainer,
        }
        _enqueue_journal_from_json(capability, journal)
        _validate_enqueue_journal_current(capability, journal)
        _replace_bytes(capability, ENQUEUE_FILE, _json_bytes(journal))
        if failpoint is not None:
            failpoint("after_enqueue_journal")
        receipt = _recover_enqueue_journal(capability, failpoint=failpoint)
        if receipt is None:
            raise PendingTransactionError("enqueue journal disappeared")
        if legacy_checkpoint is not None:
            _replace_bytes(
                capability,
                LEGACY_PENDING_STATE_FILE,
                _json_bytes(legacy_checkpoint),
            )
        return receipt


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
    def token_failpoint(name: str) -> None:
        if transition_failpoint is None:
            return
        transition_failpoint(name)
        alias = {
            "after_token_created": "after_successor_token",  # nosec B105 - failpoint
            "after_token_live": "after_transaction",  # nosec B105 - failpoint
            "after_token_protocol": "after_owner_protocol",  # nosec B105 - failpoint
        }.get(name)
        if alias is not None:
            transition_failpoint(alias)

    with pin_output(output) as capability, _locked(capability):
        _fence_pending_enqueue_locked(capability)
        pending = _read_pending_transition(capability)
        if pending is not None:
            _validate_pending_transition_current(capability, pending)
            raise PendingTransactionError(
                "pending transition requires transaction recovery"
            )
        if _read_token_transition(capability) is not None:
            token_transition = _read_token_transition(capability)
            if token_transition is not None:
                _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires transaction recovery"
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
            token_digest = hashlib.sha256(token_payload).hexdigest()
            target = replace(
                live,
                id=successor_id,
                token_digest=token_digest,
                token_identity=None,
                drainer=successor,
                phase="awaiting-drainer",
            )
            successor_protocol = dict(protocol)
            successor_protocol.update(
                transaction_id=target.id,
                owner_capability_digest=target.token_digest,
                token_identity=None,
                lease_deadline=current_time + lease_seconds,
            )
            target, successor_protocol = _start_token_transition_locked(
                capability,
                predecessor=predecessor_live,
                target=target,
                target_protocol=successor_protocol,
                token_payload=cast(Mapping[str, object], json.loads(token_payload)),
                predecessor_protocol=protocol,
                pending_predecessor_drainer=(drainer, state),
                pending_predecessor_protocol=protocol,
                binding_order="protocol-live",
                failpoint=token_failpoint,
            )
            live = target
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
        _fence_pending_enqueue_locked(capability, recover=True)
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires operational recovery"
            )
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
    if type(max_attempts) is not int or max_attempts <= 0:
        raise RecoverableTransactionError("recovery attempt bound exhausted")
    root_path = _canonical_directory(Path(root))
    cancellation_successor: Transaction | None = None
    with pin_output(output) as capability, _locked(capability):
        if capability.identity != expected_output_identity:
            raise PendingTransactionError("stale output identity selector")
        enqueue = _read_enqueue_journal(capability)
        if enqueue is not None:
            enqueue_item = cast(dict[str, Any], enqueue["item"])
            enqueue_successor = _drainer_state_from_json(
                capability, enqueue["successor_drainer"]
            )[0]
            if (
                enqueue_item["root"] != str(root_path)
                or enqueue_successor.generation != expected_generation
                or (kind is not None and enqueue_item["kind"] != kind)
                or expected_transaction_id is not None
            ):
                raise PendingTransactionError("stale enqueue recovery selector")
            _recover_enqueue_journal(capability)
        pending_before_token = _read_pending_transition(capability)
        if pending_before_token is not None:
            _validate_pending_transition_current(capability, pending_before_token)
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            token_target = _transaction_from_json(
                capability, token_transition["target_transaction"]
            )
            if (
                token_target.generation != expected_generation
                or token_target.root != str(root_path)
                or (kind is not None and token_target.kind != kind)
                or (
                    expected_transaction_id is not None
                    and token_target.id != expected_transaction_id
                )
            ):
                raise PendingTransactionError("stale token recovery selector")
            _resume_token_transition_locked(capability)
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
            else int(protocol["generation"])
            if protocol is not None
            else drainer[0].generation
            if drainer is not None
            else -1
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
    if type(max_attempts) is not int or max_attempts <= 0:
        raise RecoverableTransactionError("recovery attempt bound exhausted")
    root_path = _canonical_directory(Path(root))
    cancellation_successor: Transaction | None = None
    recovered_enqueue_generation: int | None = None
    with pin_output(output) as capability, _locked(capability):
        if (
            expected_output_identity is not None
            and capability.identity != expected_output_identity
        ):
            raise PendingTransactionError("stale output identity selector")
        enqueue = _read_enqueue_journal(capability)
        if enqueue is not None:
            enqueue_item = cast(dict[str, Any], enqueue["item"])
            enqueue_successor = _drainer_state_from_json(
                capability, enqueue["successor_drainer"]
            )[0]
            if (
                enqueue_item["root"] != str(root_path)
                or enqueue_item["kind"] != kind
                or (
                    expected_generation is not None
                    and enqueue_successor.generation != expected_generation
                )
                or expected_transaction_id is not None
            ):
                raise PendingTransactionError("stale enqueue recovery selector")
            _fence_pending_enqueue_locked(capability, recover=True)
            recovered_enqueue_generation = enqueue_successor.generation
        pending_before_token = _read_pending_transition(capability)
        if pending_before_token is not None:
            _validate_pending_transition_current(capability, pending_before_token)
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            token_target = _transaction_from_json(
                capability, token_transition["target_transaction"]
            )
            if (
                token_target.root != str(root_path)
                or token_target.kind != kind
                or (
                    expected_generation is not None
                    and token_target.generation != expected_generation
                )
                or (
                    expected_transaction_id is not None
                    and token_target.id != expected_transaction_id
                )
            ):
                raise PendingTransactionError("stale token recovery selector")
            _resume_token_transition_locked(
                capability, failpoint=transition_failpoint
            )
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
            else recovered_enqueue_generation
            if recovered_enqueue_generation is not None
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
        enqueue = _read_enqueue_journal(capability)
        if enqueue is not None:
            _validate_enqueue_journal_current(capability, enqueue)
        token_transition = _read_token_transition(capability)
        token_target = (
            None
            if token_transition is None
            else _validate_token_transition_current(capability, token_transition)
        )
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
            "enqueue_transition": (
                None
                if enqueue is None
                else {
                    "state": enqueue["state"],
                    "intent_id": enqueue["item"]["id"],
                    "kind": enqueue["item"]["kind"],
                    "intent": enqueue["item"]["intent"],
                    "root": enqueue["item"]["root"],
                    "generation": enqueue["successor_drainer"]["generation"],
                    "output_identity": enqueue["output_identity"],
                }
            ),
            "token_transition": (
                None
                if token_target is None or token_transition is None
                else {
                    "state": token_transition["state"],
                    "transaction_id": token_target.id,
                    "generation": token_target.generation,
                    "kind": token_target.kind,
                    "root": token_target.root,
                    "output_identity": token_target.output_identity.json(),
                }
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
        _fence_pending_enqueue_locked(capability)
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires transaction recovery"
            )
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
        _fence_pending_enqueue_locked(capability)
        token_transition = _read_token_transition(capability)
        if token_transition is not None:
            _validate_token_transition_current(capability, token_transition)
            raise PendingTransactionError(
                "token publication transition requires transaction recovery"
            )
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
