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
import os
import runpy
import secrets
import shutil
import stat
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

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
    ".graphify_semantic_marker",
    "needs_update",
    ".graphify_build.json",
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
DRAINER_FILE = ".graphify_drainer.json"
_PLATFORM = "windows" if os.name == "nt" else "posix"
_MAX_STATE_BYTES = 1024 * 1024
_TOKEN_MAX_BYTES = 16 * 1024


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


def pin_output(output: Path | str, *, create: bool = False) -> OutputCapability:
    """Pin one physical output directory for the operation lifetime.

    Windows is intentionally blocked until the final replace/unlink primitive
    is proven handle-relative on a native runner.  A check-then-use named path
    is not represented as equivalent protection.
    """
    path = Path(output)
    if create:
        _ensure_output(path)
    if _PLATFORM == "windows":
        raise PendingTransactionError(
            "Windows non-retargetable final mutation is not proven on this runtime"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        named = path.stat(follow_symlinks=False)
    except OSError as exc:
        with contextlib.suppress(UnboundLocalError, OSError):
            os.close(fd)
        raise PendingTransactionError(f"cannot pin output directory: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
    ):
        os.close(fd)
        raise PendingTransactionError("output directory identity changed while pinning")
    capability = OutputCapability(
        path.resolve(strict=True), OutputIdentity(info.st_dev, info.st_ino), fd
    )
    capability.validate()
    return capability


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
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            capability.validate()
            held[key] = 1
            try:
                yield
                capability.validate()
            finally:
                held.pop(key, None)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _entry_stat(capability: OutputCapability, name: str) -> os.stat_result | None:
    if Path(name).name != name:
        raise PendingTransactionError(f"unsafe managed entry name: {name}")
    try:
        info = os.stat(name, dir_fd=capability.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise PendingTransactionError(f"unsafe non-regular managed entry: {name}")
    return info


def _read_bytes(capability: OutputCapability, name: str, limit: int = _MAX_STATE_BYTES) -> bytes:
    before = _entry_stat(capability, name)
    if before is None:
        raise FileNotFoundError(name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=capability.fd)
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > limit
        ):
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


def _replace_bytes(capability: OutputCapability, name: str, payload: bytes) -> None:
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
    os.replace(temporary, name, src_dir_fd=capability.fd, dst_dir_fd=capability.fd)
    os.fsync(capability.fd)
    capability.validate()


def _create_bytes(capability: OutputCapability, name: str, payload: bytes, mode: int = 0o600) -> tuple[int, int]:
    capability.validate()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, mode, dir_fd=capability.fd)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("exclusive write made no progress")
            view = view[count:]
        os.fsync(fd)
        info = os.fstat(fd)
    finally:
        os.close(fd)
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
        nested.close()
    finally:
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


def _identity_from_json(value: object) -> OutputIdentity:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise PendingTransactionError("malformed output identity")
    try:
        return OutputIdentity(int(value["device"]), int(value["inode"]))
    except (TypeError, ValueError) as exc:
        raise PendingTransactionError("malformed output identity") from exc


def _drainer_from_json(value: object) -> DrainerTuple:
    if not isinstance(value, dict):
        raise PendingTransactionError("malformed drainer authority")
    try:
        result = DrainerTuple(
            int(value["generation"]), int(value["claim_epoch"]), str(value["launch_nonce"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PendingTransactionError("malformed drainer authority") from exc
    if result.generation < 0 or result.claim_epoch < 0 or len(result.launch_nonce) < 16:
        raise PendingTransactionError("malformed drainer authority")
    return result


def _drainer_json(value: DrainerTuple) -> dict[str, object]:
    return {
        "generation": value.generation,
        "claim_epoch": value.claim_epoch,
        "launch_nonce": value.launch_nonce,
    }


def _read_transaction(capability: OutputCapability) -> Transaction | None:
    raw = _load_json(capability, TRANSACTION_FILE)
    if raw is None:
        return None
    try:
        token_identity_raw = raw["token_identity"]
        token_identity = (
            None
            if token_identity_raw is None
            else (int(token_identity_raw["device"]), int(token_identity_raw["inode"]))
        )
        tx = Transaction(
            id=str(raw["id"]),
            kind=str(raw["kind"]),  # type: ignore[arg-type]
            root=str(raw["root"]),
            output=capability.path,
            output_identity=_identity_from_json(raw["output_identity"]),
            generation=int(raw["generation"]),
            token_digest=str(raw["token_digest"]),
            token_identity=token_identity,
            drainer=_drainer_from_json(raw["drainer"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PendingTransactionError("malformed live transaction") from exc
    if (
        len(tx.id) != 64
        or tx.kind not in {"full", "update", "runtime"}
        or tx.output_identity != capability.identity
        or len(tx.token_digest) != 64
    ):
        raise PendingTransactionError("malformed live transaction")
    return tx


def _write_transaction(capability: OutputCapability, tx: Transaction, *, phase: str = "building") -> None:
    _replace_bytes(
        capability,
        TRANSACTION_FILE,
        _json_bytes(
            {
                "schema": 1,
                "protocol_epoch": 1,
                "id": tx.id,
                "kind": tx.kind,
                "root": tx.root,
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
        ),
    )


def _authority_for(tx: Transaction, drainer: DrainerTuple | None = None) -> _Authority:
    return _Authority(
        tx.id,
        tx.generation,
        tx.token_digest,
        tx.token_identity,
        tx.output_identity,
        drainer or tx.drainer,
    )


def begin_transaction(
    kind: TransactionKind,
    root: Path | str,
    *,
    output: Path | str = "graphify-out",
    now: float | None = None,
    failpoint: Callable[[OutputCapability, dict[str, object]], None] | None = None,
) -> Transaction:
    root_path = _canonical_directory(Path(root))
    capability = pin_output(output, create=True)
    try:
        with _locked(capability):
            if _entry_stat(capability, TRANSACTION_FILE) is not None:
                raise PendingTransactionError("graph state already has bootstrap or live ownership")
            prior_protocol = _load_json(capability, PROTOCOL_FILE)
            if prior_protocol is not None and prior_protocol.get("state") != "COMPLETE":
                raise PendingTransactionError("graph state already has bootstrap or live ownership")
            generation = (
                1
                if prior_protocol is None
                else int(prior_protocol.get("generation", 0)) + 1
            )
            owner_secret = secrets.token_bytes(32)
            token_digest = hashlib.sha256(owner_secret).hexdigest()
            current_drainer = _read_drainer(capability)
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
            protocol: dict[str, object] = {
                "schema": 1,
                "protocol_epoch": 1,
                "generation": generation,
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
            _write_transaction(capability, tx)
            if current_drainer is None or current_drainer[1] == "complete":
                _write_drainer(
                    capability,
                    drainer,
                    "claimed",
                    acked_ids=[],
                    lease_deadline=(time.time() if now is None else now) + 30.0,
                )
            protocol["state"] = "INCOMPLETE"
            protocol["transaction_id"] = tx.id
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
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


def prepared_workspace_path() -> Path:
    """Return the identity-bound external preparation workspace for the owner."""
    transaction = current_transaction()
    workspace = transaction.output.parent / f".graphify-prepare-{transaction.id}"
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        marker = _load_json(capability, PREPARED_FILE)
        if marker is None:
            try:
                workspace.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise PendingTransactionError(
                    "prepared workspace already exists without owner binding"
                ) from exc
            (workspace / "graphify-out").mkdir(mode=0o700)
            with pin_output(workspace) as prepared_capability:
                marker = {
                    "schema": 1,
                    "transaction_id": transaction.id,
                    "generation": transaction.generation,
                    "token_digest": transaction.token_digest,
                    "identity": prepared_capability.identity.json(),
                }
                _create_bytes(capability, PREPARED_FILE, _json_bytes(marker))
                for name in MANAGED_PUBLICATION_PATHS:
                    try:
                        payload = _read_relative_bytes(capability, name)
                    except PendingTransactionError as exc:
                        if isinstance(exc.__cause__, FileNotFoundError):
                            continue
                        raise
                    _replace_relative_bytes(
                        prepared_capability, f"graphify-out/{name}", payload
                    )
        with pin_output(workspace) as prepared_capability:
            try:
                prepared_output_info = os.stat(
                    "graphify-out",
                    dir_fd=prepared_capability.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise PendingTransactionError(
                    "prepared output directory is missing"
                ) from exc
            if (
                marker.get("schema") != 1
                or marker.get("transaction_id") != transaction.id
                or marker.get("generation") != transaction.generation
                or marker.get("token_digest") != transaction.token_digest
                or _identity_from_json(marker.get("identity"))
                != prepared_capability.identity
                or not stat.S_ISDIR(prepared_output_info.st_mode)
            ):
                raise PendingTransactionError("prepared workspace owner binding changed")
    return workspace


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
        protocol = _load_json(capability, PROTOCOL_FILE)
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


def run_token(token_path: Path | str, python_args: list[str]) -> None:
    path = Path(token_path).absolute()
    if len(python_args) < 2 or python_args[0] not in {"-c", "-m"}:
        raise PendingTransactionError("runner accepts only exact -c or -m shapes")
    with pin_output(path.parent) as capability, _locked(capability):
        raw, _payload, identity = _open_token(path)
        tx = resume_transaction(
            str(raw["id"]), str(raw["root"]), output=str(raw["output"])
        )
        tx = replace(tx, token_identity=identity)
        authority_token = _AUTHORITY.set(_authority_for(tx))
    mode, target, *arguments = python_args
    if not target or (mode == "-m" and (target.startswith("-") or "/" in target or "\\" in target)):
        raise PendingTransactionError("ambiguous transaction runner target")
    old_argv = sys.argv
    old_environment = {
        key: os.environ.get(key)
        for key in (
            "GRAPHIFY_TRANSACTION_ID",
            "GRAPHIFY_TRANSACTION_ROOT",
            "GRAPHIFY_TRANSACTION_OUTPUT",
            "GRAPHIFY_TRANSACTION_TOKEN",
        )
    }
    os.environ.update(
        GRAPHIFY_TRANSACTION_ID=tx.id,
        GRAPHIFY_TRANSACTION_ROOT=tx.root,
        GRAPHIFY_TRANSACTION_OUTPUT=str(tx.output),
        GRAPHIFY_TRANSACTION_TOKEN=str(path),
    )
    try:
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
        _AUTHORITY.reset(authority_token)
        sys.argv = old_argv
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
    drainer_raw = _load_json(capability, DRAINER_FILE)
    live_drainer = live.drainer if drainer_raw is None else _drainer_from_json(drainer_raw)
    if drainer_raw is not None and drainer_raw.get("state") in {"CLOSE_PENDING", "complete"} and not allow_complete:
        raise PendingTransactionError("drainer is closing and no longer permits publication")
    if (
        authority.transaction_id != live.id
        or transaction.id != live.id
        or authority.generation != live.generation
        or authority.output_identity != capability.identity
        or authority.token_digest != live.token_digest
        or authority.token_identity != live.token_identity
        or authority.drainer != live_drainer
    ):
        raise PendingTransactionError("exact live drainer owner context required")
    return live


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
    relative = Path(relative_name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise PendingTransactionError(f"unsafe managed relative path: {relative_name}")
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        if failpoint:
            failpoint("after_validate")
        parent_fd = os.dup(capability.fd)
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
            leaf = relative.parts[-1]
            temporary = f".{leaf}.{secrets.token_hex(16)}.tmp"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                view = memoryview(payload)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError("atomic write made no progress")
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            capability.validate()
            os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            capability.validate()
            if failpoint:
                failpoint("after_replace")
        finally:
            os.close(parent_fd)


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


def _watermark(payload: bytes) -> dict[str, object]:
    try:
        graph = json.loads(payload.decode("utf-8"))
        metadata = graph["graph"][GRAPH_WATERMARK_KEY]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PendingTransactionError("graph payload has no valid protocol watermark") from exc
    if not isinstance(metadata, dict) or metadata.get("schema") != 1 or metadata.get("protocol_epoch") != 1:
        raise PendingTransactionError("unsupported graph watermark schema")
    return metadata


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
        protocol = _load_json(capability, PROTOCOL_FILE)
        if protocol is None:
            raise PendingTransactionError("protocol state is missing")
        protocol.update(state="COMPLETE", generation=live.generation, receipt_digest=digest)
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
        _replace_bytes(capability, RECEIPT_FILE, receipt_payload)
        return GenerationReceipt(digest, live.generation)


def _validate_receipt_locked(
    capability: OutputCapability,
    *,
    transaction: Transaction | None = None,
    graph_payload: bytes | None = None,
    require_closed: bool = False,
) -> tuple[dict[str, Any], str, dict[str, bytes]]:
    try:
        receipt_payload = _read_bytes(capability, RECEIPT_FILE)
    except FileNotFoundError as exc:
        raise PendingTransactionError("generation receipt is missing") from exc
    try:
        receipt = json.loads(receipt_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("generation receipt is malformed") from exc
    protocol = _load_json(capability, PROTOCOL_FILE)
    if not isinstance(receipt, dict) or protocol is None:
        raise PendingTransactionError("generation receipt is missing")
    digest = hashlib.sha256(receipt_payload).hexdigest()
    drainer = _drainer_from_json(receipt.get("drainer"))
    required = receipt.get("required_artifacts")
    artifact_digests = receipt.get("artifact_digests")
    if (
        receipt.get("schema") != 1
        or receipt.get("protocol_epoch") != 1
        or protocol.get("state") != "COMPLETE"
        or protocol.get("receipt_digest") != digest
        or protocol.get("generation") != receipt.get("generation")
        or protocol.get("transaction_id") != receipt.get("transaction_id")
        or protocol.get("owner_capability_digest") != receipt.get("token_digest")
        or _identity_from_json(protocol.get("output_identity")) != capability.identity
        or _identity_from_json(receipt.get("output_identity")) != capability.identity
        or not isinstance(required, list)
        or not required
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
    current_drainer = _read_drainer(capability)
    if current_drainer is None or current_drainer[0] != drainer:
        raise PendingTransactionError("generation receipt does not match live drainer")
    if require_closed and (
        current_drainer is None
        or current_drainer[1] != "complete"
        or current_drainer[2].get("receipt_digest") != digest
    ):
        raise PendingTransactionError("generation close is incomplete")
    inventory: dict[str, bytes] = {}
    for name in required:
        payload = _read_relative_bytes(capability, name)
        if hashlib.sha256(payload).hexdigest() != artifact_digests[name]:
            raise PendingTransactionError(f"managed artifact digest changed: {name}")
        inventory[name] = payload
    manifest = inventory["manifest.json"]
    if hashlib.sha256(manifest).hexdigest() != receipt.get("manifest_digest"):
        raise PendingTransactionError("manifest digest changed after receipt")
    graph_name = str(receipt.get("graph_name", "graph.json"))
    actual_graph = graph_payload or inventory[graph_name]
    if hashlib.sha256(actual_graph).hexdigest() != receipt.get("graph_digest"):
        raise PendingTransactionError("graph digest changed after receipt")
    inventory[graph_name] = actual_graph
    return receipt, digest, inventory


def _coordination_present(capability: OutputCapability) -> bool:
    markers = {PROTOCOL_FILE, TRANSACTION_FILE, RECEIPT_FILE, QUEUE_FILE, DRAINER_FILE, QUARANTINE_FILE}
    for name in os.listdir(capability.fd):
        if name in markers or name.startswith(
            (".graphify_transaction_token.", ".graphify_rebuild_inflight.")
        ):
            return True
    return False


def open_graph_snapshot(path: Path | str, *, purpose: str) -> GraphSnapshot:
    del purpose  # carried by callers for diagnostics/audit inventory
    requested = Path(path).expanduser()
    output = requested.parent.resolve(strict=True)
    graph_path = output / requested.name
    with pin_output(output) as capability, _locked(capability):
        if _entry_stat(capability, graph_path.name) is None:
            protocol = _load_json(capability, PROTOCOL_FILE)
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
        payload = _read_bytes(capability, graph_path.name, 512 * 1024 * 1024)
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
                protocol = _load_json(capability, PROTOCOL_FILE)
                state = None if protocol is None else protocol.get("state")
                label = "bootstrap" if state == "BOOTSTRAP_PENDING" else "protocol"
                raise PendingTransactionError(f"{label} state exists without a graph receipt")
            legacy_inventory = {graph_path.name: payload}
            manifest_payload = None
            try:
                manifest_payload = _read_relative_bytes(capability, "manifest.json")
                legacy_inventory["manifest.json"] = manifest_payload
            except PendingTransactionError as exc:
                if "is missing" not in str(exc):
                    raise
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
        receipt, _digest, inventory = _validate_receipt_locked(
            capability, graph_payload=payload
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
    workspace = prepared_workspace_path()
    requested = Path(path).expanduser().absolute()
    expected = workspace / "graphify-out" / requested.name
    live_alias = transaction.output / requested.name
    if requested not in {expected, live_alias}:
        raise PendingTransactionError("prepared graph is outside the owned workspace")
    with pin_output(workspace) as prepared_capability:
        payload = _read_relative_bytes(
            prepared_capability, f"graphify-out/{requested.name}"
        )
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
        )


def _read_queue(capability: OutputCapability, name: str = QUEUE_FILE) -> list[dict[str, Any]]:
    if _entry_stat(capability, name) is None:
        return []
    try:
        values = [json.loads(line) for line in _read_bytes(capability, name).decode().splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingTransactionError("malformed rebuild queue") from exc
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in values:
        if (
            not isinstance(item, dict)
            or item.get("schema") != 1
            or item.get("kind") not in {"full", "update", "runtime"}
            or not isinstance(item.get("id"), str)
            or len(str(item["id"])) != 64
            or not isinstance(item.get("root"), str)
            or not isinstance(item.get("changed_paths"), (list, type(None)))
        ):
            raise PendingTransactionError("malformed rebuild queue")
        item_id = str(item["id"])
        if item_id in seen_ids:
            raise PendingTransactionError("duplicate rebuild intent id")
        seen_ids.add(item_id)
        result.append(item)
    return result


def _write_queue(capability: OutputCapability, name: str, items: list[dict[str, Any]]) -> None:
    payload = b"".join(_json_bytes(item) + b"\n" for item in items)
    _replace_bytes(capability, name, payload)


def _merge_intents(*groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            merged.setdefault(str(item["id"]), item)
    return list(merged.values())


def _read_drainer(capability: OutputCapability) -> tuple[DrainerTuple, str, dict[str, Any]] | None:
    raw = _load_json(capability, DRAINER_FILE)
    if raw is None:
        return None
    state = str(raw.get("state"))
    if state not in {"reserved", "launching", "claimed", "closing", "CLOSE_PENDING", "complete"}:
        raise PendingTransactionError("malformed drainer state")
    return _drainer_from_json(raw), state, raw


def _write_drainer(capability: OutputCapability, drainer: DrainerTuple, state: str, **extra: object) -> None:
    _replace_bytes(
        capability,
        DRAINER_FILE,
        _json_bytes({"schema": 1, **_drainer_json(drainer), "state": state, **extra}),
    )


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
    root_path = _canonical_directory(Path(root))
    with pin_output(output, create=True) as capability, _locked(capability):
        existing_drainer = _read_drainer(capability)
        if existing_drainer is not None and existing_drainer[1] == "CLOSE_PENDING":
            _finish_close_locked(capability, existing_drainer[2])
            existing_drainer = _read_drainer(capability)
        if existing_drainer is None or existing_drainer[1] == "complete":
            generation = 1 if existing_drainer is None else existing_drainer[0].generation + 1
            drainer = DrainerTuple(generation, 0, secrets.token_hex(16))
            _write_drainer(capability, drainer, "reserved", lease_deadline=(time.time() if now is None else now) + 30)
        else:
            drainer = existing_drainer[0]
        durable_paths = [] if changed_paths is None else [os.fspath(value) for value in changed_paths]
        if legacy_pending_name is not None and _entry_stat(capability, legacy_pending_name) is not None:
            try:
                legacy_payload = _read_bytes(capability, legacy_pending_name).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PendingTransactionError("legacy pending changes are malformed") from exc
            durable_paths.extend(
                line.strip() for line in legacy_payload.splitlines() if line.strip()
            )
            durable_paths = list(dict.fromkeys(durable_paths))
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
        _write_queue(capability, QUEUE_FILE, queued)
        if legacy_pending_name is not None:
            _unlink(capability, legacy_pending_name)
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
) -> DrainerTuple:
    with pin_output(output) as capability, _locked(capability):
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
        successor = DrainerTuple(drainer.generation, drainer.claim_epoch + 1, secrets.token_hex(16))
        _write_drainer(capability, successor, "reserved", lease_deadline=current_time + lease_seconds)
        live = _read_transaction(capability)
        if live is not None:
            _write_transaction(capability, replace(live, drainer=successor))
        return successor


def complete_rebuild_claim(
    transaction: Transaction,
    claim: RebuildClaim,
    *,
    receipt_digest: str,
    now: float | None = None,
) -> None:
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        receipt = _load_json(capability, RECEIPT_FILE)
        if receipt is None or hashlib.sha256(_json_bytes(receipt)).hexdigest() != receipt_digest:
            raise PendingTransactionError("generation receipt is required before claim acknowledgement")
        current = _read_drainer(capability)
        if current is None or current[0] != claim.drainer or current[1] != "claimed":
            raise PendingTransactionError("claim drainer no longer matches")
        ids = [str(item["id"]) for item in claim.items]
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
        _validate_authority(capability, transaction)
        if _read_queue(capability):
            return False
        current = _read_drainer(capability)
        if current is None:
            drainer = transaction.drainer
            acked: list[str] = []
        else:
            drainer, state, raw = current
            if state != "claimed":
                raise PendingTransactionError("drainer is not claimable for close")
            acked = [str(value) for value in raw.get("acked_ids", [])]
            if raw.get("receipt_digest") != receipt_digest:
                raise PendingTransactionError("close receipt does not match acknowledged work")
        pending = {
            "schema": 1,
            **_drainer_json(drainer),
            "state": "CLOSE_PENDING",
            "receipt_digest": receipt_digest,
            "acked_ids": acked,
            "queue_epoch": drainer.generation,
            "output_identity": capability.identity.json(),
            "successor_generation": drainer.generation + 1,
            "transaction_id": transaction.id,
            "token_identity": (
                None
                if transaction.token_identity is None
                else {"device": transaction.token_identity[0], "inode": transaction.token_identity[1]}
            ),
        }
        _replace_bytes(capability, DRAINER_FILE, _json_bytes(pending))
        if failpoint:
            failpoint("after_close_pending")
        _finish_close_locked(capability, pending, failpoint=failpoint)
        return True


def _finish_close_locked(
    capability: OutputCapability,
    pending: dict[str, Any],
    *,
    failpoint: Callable[[str], None] | None = None,
) -> None:
    if pending.get("state") != "CLOSE_PENDING":
        return
    transaction_id = str(pending["transaction_id"])
    inflight_name = f".graphify_rebuild_inflight.{transaction_id}.jsonl"
    _unlink(capability, inflight_name)
    if failpoint:
        failpoint("after_inflight_remove")
    live = _read_transaction(capability)
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


def recover_transaction(
    kind: TransactionKind,
    root: Path | str,
    *,
    output: Path | str = "graphify-out",
    now: float | None = None,
    max_attempts: int = 3,
) -> Transaction:
    if max_attempts <= 0:
        raise RecoverableTransactionError("recovery attempt bound exhausted")
    root_path = _canonical_directory(Path(root))
    with pin_output(output) as capability, _locked(capability):
        current = _read_transaction(capability)
        if current is None:
            protocol = _load_json(capability, PROTOCOL_FILE)
            if protocol is None or protocol.get("state") != "BOOTSTRAP_PENDING":
                raise PendingTransactionError("no live transaction to recover")
            current_time = time.time() if now is None else now
            if current_time <= float(protocol.get("lease_deadline", 0.0)):
                raise PendingTransactionError("bootstrap lease has not expired")
            generation = int(protocol.get("generation", 0))
            claim_epoch = int(protocol.get("bootstrap_claim_epoch", 0)) + 1
            secret = secrets.token_bytes(32)
            token_digest = hashlib.sha256(secret).hexdigest()
            drainer = DrainerTuple(generation, claim_epoch, secrets.token_hex(16))
            protocol.update(
                bootstrap_claim_epoch=claim_epoch,
                bootstrap_nonce=secrets.token_hex(16),
                owner_capability_digest=token_digest,
                lease_deadline=current_time + 30.0,
            )
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
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
            _write_transaction(capability, tx, phase="bootstrap-recovered")
            _write_drainer(
                capability,
                drainer,
                "claimed",
                acked_ids=[],
                lease_deadline=current_time + 30.0,
            )
            protocol.update(state="INCOMPLETE", transaction_id=tx.id)
            _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
            _AUTHORITY.set(_authority_for(tx))
            return tx
        protocol = _load_json(capability, PROTOCOL_FILE)
        if protocol is None:
            raise PendingTransactionError("protocol state is missing")
        current_time = time.time() if now is None else now
        if current_time <= float(protocol.get("lease_deadline", 0.0)):
            raise PendingTransactionError("transaction lease has not expired")
        if current.root != str(root_path):
            raise PendingTransactionError("recovery root does not match live transaction")
        drainer_current = _read_drainer(capability)
        claim_epoch = 0 if drainer_current is None else drainer_current[0].claim_epoch + 1
        if claim_epoch > max_attempts:
            raise RecoverableTransactionError("recovery attempt bound exhausted")
        queued = _read_queue(capability)
        for name in os.listdir(capability.fd):
            if name.startswith(".graphify_rebuild_inflight.") and name.endswith(".jsonl"):
                queued.extend(_read_queue(capability, name))
        if queued:
            deduplicated = {str(item["id"]): item for item in queued}
            _write_queue(capability, QUEUE_FILE, list(deduplicated.values()))
        for name in list(os.listdir(capability.fd)):
            if name.startswith(".graphify_rebuild_inflight.") and name.endswith(".jsonl"):
                _unlink(capability, name)
        if current.token_identity is not None:
            _unlink(
                capability,
                f".graphify_transaction_token.{current.id}",
                expected=current.token_identity,
            )
        generation = current.generation + 1
        drainer = (
            DrainerTuple(generation, 0, secrets.token_hex(16))
            if drainer_current is None
            else DrainerTuple(
                generation,
                claim_epoch,
                secrets.token_hex(16),
            )
        )
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
        protocol.update(
            schema=1,
            protocol_epoch=1,
            generation=generation,
            state="INCOMPLETE",
            transaction_id=tx.id,
            output_identity=capability.identity.json(),
            owner_capability_digest=tx.token_digest,
            lease_deadline=(time.time() if now is None else now) + 30,
        )
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(protocol))
        _write_transaction(capability, tx)
        _write_drainer(
            capability,
            drainer,
            "claimed",
            acked_ids=[],
            lease_deadline=current_time + 30.0,
        )
        _AUTHORITY.set(_authority_for(tx))
        return tx


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
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > 512 * 1024 * 1024:
                raise PendingTransactionError("unsafe detached merge snapshot")
            payload = bytearray()
            while len(payload) <= 512 * 1024 * 1024:
                chunk = os.read(fd, min(65536, 512 * 1024 * 1024 + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > 512 * 1024 * 1024:
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
    merged = dict(snapshots[1])
    nodes: dict[str, dict[str, object]] = {}
    for snapshot in snapshots[1:]:
        for node in snapshot.get("nodes", []):
            if isinstance(node, dict) and "id" in node:
                nodes[str(node["id"])] = node
    links: dict[str, dict[str, object]] = {}
    for snapshot in snapshots[1:]:
        for link in snapshot.get("links", []):
            if isinstance(link, dict):
                key = hashlib.sha256(_json_bytes(link)).hexdigest()
                links[key] = link
    merged["nodes"] = list(nodes.values())
    merged["links"] = list(links.values())
    graph_meta = dict(merged.get("graph") or {})
    graph_meta[GRAPH_WATERMARK_KEY] = {
        "schema": 1,
        "protocol_epoch": 1,
        "state": "merge_pending",
        "snapshot_generation": hashlib.sha256("".join(digests).encode()).hexdigest(),
        "input_digests": digests,
    }
    merged["graph"] = graph_meta
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
            json.dumps(merged, sort_keys=True).encode("utf-8"),
        )


def finish_transaction(transaction: Transaction) -> None:
    """Close a direct transaction after a committed receipt."""
    with pin_output(transaction.output) as capability, _locked(capability):
        _validate_authority(capability, transaction)
        _receipt, receipt_digest, _inventory = _validate_receipt_locked(
            capability, transaction=transaction
        )
        current = _read_drainer(capability)
        if current is None:
            _write_drainer(capability, transaction.drainer, "claimed", acked_ids=[], receipt_digest=receipt_digest)
        elif (
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
            )
    if not close_if_queue_empty(transaction, receipt_digest=receipt_digest):
        with pin_output(transaction.output) as capability, _locked(capability):
            _validate_authority(capability, transaction)
            if not _read_queue(capability):
                raise PendingTransactionError("successor queue disappeared during close")
            current = _read_drainer(capability)
            if current is None or current[0] != transaction.drainer or current[1] != "claimed":
                raise PendingTransactionError("successor handoff lost exact drainer")
            pending = {
                "schema": 1,
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
    workspace = prepared_workspace_path()

    with pin_output(workspace) as prepared_capability, pin_output(
        transaction.output
    ) as capability, _locked(capability):
        _validate_authority(capability, transaction)

        def prepared_bytes(name: str) -> bytes:
            return _read_relative_bytes(
                prepared_capability, f"graphify-out/{name}"
            )

        graph_name = "graph.json"
        graph_data = json.loads(
            prepared_bytes(graph_name).decode("utf-8")
        )
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
        except FileNotFoundError as exc:
            raise PendingTransactionError(
                "prepared manifest is required before finalization"
            ) from exc
        prepared: dict[str, bytes] = {}
        for name in MANAGED_PUBLICATION_PATHS:
            if name in {graph_name, "manifest.json"}:
                continue
            try:
                prepared[name] = prepared_bytes(name)
            except PendingTransactionError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    continue
                raise
    artifacts = [graph_name]
    with owned_step(transaction):
        commit_bytes(transaction, graph_name, graph_payload)
        for name, payload in prepared.items():
            if "/" in name:
                commit_relative_bytes(transaction, name, payload)
            else:
                commit_bytes(transaction, name, payload)
            artifacts.append(name)
        commit_bytes(transaction, "manifest.json", manifest_payload)
        artifacts.append("manifest.json")
        commit_generation(
            transaction,
            graph_payload=graph_payload,
            manifest_payload=manifest_payload,
            required_artifacts=tuple(artifacts),
        )
    finish_transaction(transaction)
    with pin_output(transaction.output) as capability, _locked(capability):
        marker = _load_json(capability, PREPARED_FILE)
        if marker is None:
            raise PendingTransactionError("prepared workspace binding is missing")
        expected = _identity_from_json(marker.get("identity"))
        with pin_output(workspace) as prepared_capability:
            if prepared_capability.identity != expected:
                raise PendingTransactionError("prepared workspace identity changed")
        parent = workspace.parent
        with pin_output(parent) as parent_capability:
            try:
                info = os.stat(
                    workspace.name,
                    dir_fd=parent_capability.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise PendingTransactionError(
                    "prepared workspace identity changed"
                ) from exc
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                expected.device,
                expected.inode,
            ):
                raise PendingTransactionError("prepared workspace identity changed")
            tombstone = f".graphify-retired-{transaction.id}-{secrets.token_hex(8)}"
            os.rename(
                workspace.name,
                tombstone,
                src_dir_fd=parent_capability.fd,
                dst_dir_fd=parent_capability.fd,
            )
            _unlink(capability, PREPARED_FILE)
            shutil.rmtree(parent / tombstone)


def cancel_unpublished_transaction(transaction: Transaction) -> None:
    """Restore the prior complete generation after a proven no-op preparation.

    This is intentionally narrower than abort/recovery: it succeeds only when
    the still-live generation has published no receipt and the retained graph
    plus previous receipt prove the immediately preceding complete generation.
    """
    with pin_output(transaction.output) as capability, _locked(capability):
        live = _validate_authority(capability, transaction)
        protocol = _load_json(capability, PROTOCOL_FILE)
        receipt = _load_json(capability, RECEIPT_FILE)
        if (
            protocol is None
            or protocol.get("state") != "INCOMPLETE"
            or int(protocol.get("generation", -1)) != live.generation
            or receipt is None
            or int(receipt.get("generation", -1)) != live.generation - 1
        ):
            raise PendingTransactionError("no-op rollback has no prior complete generation")
        graph_name = str(receipt.get("graph_name", "graph.json"))
        graph_payload = _read_bytes(capability, graph_name, 512 * 1024 * 1024)
        watermark = _watermark(graph_payload)
        receipt_payload = _json_bytes(receipt)
        receipt_digest = hashlib.sha256(receipt_payload).hexdigest()
        if (
            watermark.get("generation") != receipt.get("generation")
            or watermark.get("state") != "active"
            or hashlib.sha256(graph_payload).hexdigest() != receipt.get("graph_digest")
            or receipt.get("watermark") != watermark
            or _identity_from_json(receipt.get("output_identity")) != capability.identity
        ):
            raise PendingTransactionError("no-op rollback prior generation is inconsistent")
        restored_protocol = {
            "schema": 1,
            "protocol_epoch": 1,
            "generation": int(receipt["generation"]),
            "state": "COMPLETE",
            "output_identity": capability.identity.json(),
            "owner_capability_digest": str(receipt["token_digest"]),
            "transaction_id": str(receipt["transaction_id"]),
            "receipt_digest": receipt_digest,
        }
        _replace_bytes(capability, PROTOCOL_FILE, _json_bytes(restored_protocol))
        _unlink(capability, TRANSACTION_FILE)
        prior_drainer = _drainer_from_json(receipt.get("drainer"))
        _write_drainer(
            capability,
            prior_drainer,
            "complete",
            receipt_digest=receipt_digest,
            transaction_id=str(receipt["transaction_id"]),
            successor_generation=prior_drainer.generation + 1,
        )
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
    if len(args) < 5 or args[0] != "run-token" or args[2] != "--":
        raise SystemExit(
            "usage: python -P -m graphify.transaction run-token TOKEN -- (-c CODE | -m MODULE) [args...]"
        )
    run_token(args[1], args[3:])
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess integration
    current_module = sys.modules[__name__]
    canonical = sys.modules.get("graphify.transaction")
    if canonical is not None and canonical is not current_module:
        raise SystemExit(canonical._main())
    sys.modules["graphify.transaction"] = current_module
    raise SystemExit(_main())
