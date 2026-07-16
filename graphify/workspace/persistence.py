"""Crash-durable primitives for external workspace lifecycle state."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
from typing import Callable, Iterator, Protocol, TypeVar
import uuid


FaultHook = Callable[[str], None]
REGISTRY_LOCK_RANK = 10
WORKSPACE_LOCK_RANK = 20
_LOCK_STACK: ContextVar[tuple[tuple[int, str], ...]] = ContextVar(
    "graphify_workspace_lock_stack",
    default=(),
)


class WorkspaceRuntimeError(RuntimeError):
    """Base class for stable P2 runtime failures."""

    code = "workspace_runtime_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class UnsupportedRuntime(WorkspaceRuntimeError):
    code = "unsupported_runtime"


class StatePathError(WorkspaceRuntimeError):
    code = "unsafe_state_path"


class StateCorrupt(WorkspaceRuntimeError):
    code = "state_corrupt"


class CommitUnknown(WorkspaceRuntimeError):
    code = "commit_unknown"


class LockOrderError(WorkspaceRuntimeError):
    code = "lock_order"


class InjectedFault(RuntimeError):
    """Test-only process-death analogue raised by named failpoint hooks."""


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Detected support boundary for lifecycle mutation."""

    system: str
    filesystem: str
    elevated: bool
    local: bool

    @classmethod
    def detect(cls, path: Path) -> "RuntimeCapabilities":
        existing = path.resolve(strict=False)
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        system = platform.system()
        filesystem = "unknown"
        if system == "Darwin":
            result = subprocess.run(
                ["stat", "-f", "%T", str(existing)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                filesystem = result.stdout.strip().lower()
        elevated = bool(hasattr(os, "geteuid") and os.geteuid() == 0)
        network_filesystems = {"nfs", "smbfs", "fusefs", "webdav", "afpfs"}
        return cls(
            system=system,
            filesystem=filesystem,
            elevated=elevated,
            local=filesystem not in network_filesystems,
        )

    @classmethod
    def supported_test_fixture(cls) -> "RuntimeCapabilities":
        """Explicit capability seam for deterministic non-production tests."""

        return cls(system="Darwin", filesystem="apfs", elevated=False, local=True)

    def require_supported(self) -> None:
        if (
            self.system != "Darwin"
            or self.filesystem.lower() != "apfs"
            or self.elevated
            or not self.local
        ):
            raise UnsupportedRuntime(
                "workspace lifecycle mutation requires non-elevated macOS on local APFS"
            )


class Syscalls(Protocol):
    def write(self, descriptor: int, data: memoryview) -> int: ...

    def fsync(self, descriptor: int) -> None: ...

    def replace(self, source: Path, destination: Path) -> None: ...


class PosixSyscalls:
    """Injectable POSIX syscall surface used by durability tests."""

    def write(self, descriptor: int, data: memoryview) -> int:
        return os.write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)


RecordT = TypeVar("RecordT")


class DurableStateRoot:
    """Secure path, locking, and record-install authority for one state root."""

    def __init__(
        self,
        root: Path,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        if not root.is_absolute():
            raise StatePathError("state root must be an absolute path")
        if root.is_symlink():
            raise StatePathError(f"state root must not be a symbolic link: {root}")
        self.root = root.resolve(strict=False)
        self.capabilities = capabilities or RuntimeCapabilities.detect(self.root)
        self.capabilities.require_supported()
        self.fault_hook = fault_hook or (lambda _event: None)
        self.syscalls = syscalls or PosixSyscalls()

    def _ensure_root(self) -> None:
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise StatePathError(f"state root is not a real directory: {self.root}")
        if not self.root.exists():
            try:
                self._require_owned_directory(self.root.parent)
            except OSError as exc:
                raise StatePathError(
                    f"state root parent must already be an owned directory: {self.root.parent}"
                ) from exc
            try:
                os.mkdir(self.root, 0o700)
            except FileExistsError:
                pass
            if self.root.is_symlink() or not self.root.is_dir():
                raise StatePathError(f"state root is not a real directory: {self.root}")
            os.chmod(self.root, 0o700)
            self._fsync_directory(self.root.parent)
        else:
            os.chmod(self.root, 0o700)
        self._require_owned_directory(self.root)

    @staticmethod
    def _require_owner(details: os.stat_result, path: Path) -> None:
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            raise StatePathError(f"state path is not owned by the current user: {path}")

    def _require_owned_directory(self, path: Path) -> None:
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
            raise StatePathError(f"state directory is not a real directory: {path}")
        self._require_owner(details, path)

    def path(self, relative: str | Path) -> Path:
        pure = PurePosixPath(Path(relative).as_posix())
        if pure.is_absolute() or not pure.parts or ".." in pure.parts or "." in pure.parts:
            raise StatePathError(f"state path must be a contained relative path: {relative}")
        candidate = self.root.joinpath(*pure.parts)
        if self.root not in candidate.parents:
            raise StatePathError(f"state path escapes root: {relative}")
        return candidate

    def ensure_directory(self, relative: str | Path) -> Path:
        self._ensure_root()
        if Path(relative) in {Path(), Path(".")}:
            return self.root
        destination = self.path(relative)
        current = self.root
        for part in destination.relative_to(self.root).parts:
            child = current / part
            try:
                child.lstat()
            except FileNotFoundError:
                os.mkdir(child, 0o700)
                self._fsync_directory(current)
            self._require_owned_directory(child)
            os.chmod(child, 0o700)
            current = child
        return destination

    def _ensure_parent(self, path: Path) -> None:
        relative = path.parent.relative_to(self.root)
        if relative.parts:
            self.ensure_directory(relative)

    def assert_external_to(self, source_root: Path) -> None:
        source = source_root.resolve(strict=True)
        if self.root == source or self.root in source.parents or source in self.root.parents:
            raise StatePathError(
                f"external state root {self.root} overlaps source checkout {source}"
            )

    @contextmanager
    def lock(
        self,
        relative: str | Path,
        *,
        rank: int,
        name: str,
    ) -> Iterator[None]:
        self._ensure_root()
        stack = _LOCK_STACK.get()
        if stack and rank < stack[-1][0]:
            raise LockOrderError(f"{name} lock cannot be acquired after {stack[-1][1]} lock")
        path = self.path(relative)
        self._ensure_parent(path)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            os.close(descriptor)
            raise StatePathError(f"state lock is not a regular file: {path}")
        self._require_owner(details, path)
        os.fchmod(descriptor, 0o600)
        try:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - rejected by capability gate
                raise UnsupportedRuntime("fcntl is required for workspace locking") from exc
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    continue
            token = _LOCK_STACK.set((*stack, (rank, name)))
            self.fault_hook(f"lock:{name}:acquired")
            try:
                yield
            finally:
                _LOCK_STACK.reset(token)
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        break
                    except InterruptedError:
                        continue
                self.fault_hook(f"lock:{name}:released")
        finally:
            os.close(descriptor)

    def write_once(self, relative: str | Path, data: bytes) -> Path:
        self._ensure_root()
        path = self.path(relative)
        self._ensure_parent(path)
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            if self._read_regular(path) != data:
                raise StateCorrupt(f"content-addressed state conflicts at {path}")
            return path
        self._atomic_replace(path, data)
        if self._read_regular(path) != data:
            raise StateCorrupt(f"content-addressed state verification failed at {path}")
        return path

    def read_bytes(self, relative: str | Path) -> bytes:
        """Read one contained regular state file without following its final path."""

        self._ensure_root()
        return self._read_regular(self.path(relative))

    def read_current(
        self,
        relative: str | Path,
        *,
        decoder: Callable[[bytes], RecordT],
        allow_missing: bool = False,
        label: str,
    ) -> RecordT | None:
        path = self.path(relative)
        if not path.exists():
            if allow_missing:
                return None
            raise StateCorrupt(f"{label} current record is missing")
        try:
            return decoder(self._read_regular(path))
        except Exception as exc:
            if isinstance(exc, StateCorrupt):
                raise
            raise StateCorrupt(f"{label} current record is invalid: {exc}") from exc

    def recover_record(
        self,
        *,
        label: str,
        current: str | Path,
        previous: str | Path,
        pending: str | Path,
        decoder: Callable[[bytes], RecordT],
        revision: Callable[[RecordT], int],
        allow_missing: bool = False,
    ) -> RecordT | None:
        paths = {
            "current": self.path(current),
            "pending": self.path(pending),
            "previous": self.path(previous),
        }
        candidates: dict[str, tuple[bytes, RecordT, int]] = {}
        invalid: dict[str, Exception] = {}
        for name, path in paths.items():
            if not path.exists():
                continue
            try:
                data = self._read_regular(path)
                record = decoder(data)
                candidates[name] = (data, record, revision(record))
            except Exception as exc:
                invalid[name] = exc

        if not candidates:
            if allow_missing and not invalid:
                return None
            detail = (
                "; ".join(f"{name}: {invalid[name]}" for name in sorted(invalid))
                or "all records are missing"
            )
            raise StateCorrupt(f"{label} has no valid recoverable record: {detail}")
        if "current" in invalid and "pending" not in candidates:
            raise StateCorrupt(f"{label} current is corrupt without a durable pending commit")
        if "pending" in invalid:
            raise StateCorrupt(f"{label} pending commit is corrupt")
        if "current" not in candidates and "pending" not in candidates:
            raise StateCorrupt(f"{label} current is missing and no pending commit can recover it")

        highest_revision = max(item[2] for item in candidates.values())
        highest = [(name, item) for name, item in candidates.items() if item[2] == highest_revision]
        highest_bytes = {item[0] for _name, item in highest}
        if len(highest_bytes) != 1:
            raise StateCorrupt(f"{label} has divergent records at revision {highest_revision}")
        preferred_name = (
            "current" if any(name == "current" for name, _ in highest) else highest[0][0]
        )
        selected = candidates[preferred_name]
        current_candidate = candidates.get("current")
        if current_candidate is None or current_candidate[0] != selected[0]:
            if current_candidate is not None:
                self._atomic_replace(paths["previous"], current_candidate[0])
            self._atomic_replace(paths["current"], selected[0])
            self.fault_hook(f"{label}:recovered")
        if paths["pending"].exists():
            self._unlink_and_sync(paths["pending"])
        return selected[1]

    def commit_record(
        self,
        *,
        label: str,
        current: str | Path,
        previous: str | Path,
        pending: str | Path,
        payload: bytes,
        decoder: Callable[[bytes], RecordT],
    ) -> RecordT:
        record = decoder(payload)
        current_path = self.path(current)
        previous_path = self.path(previous)
        pending_path = self.path(pending)
        commit_may_recover = False

        def pending_replaced() -> None:
            nonlocal commit_may_recover
            commit_may_recover = True

        def current_replaced() -> None:
            self.fault_hook(f"{label}:current_replaced")

        try:
            self._atomic_replace(pending_path, payload, after_replace=pending_replaced)
            self.fault_hook(f"{label}:pending_durable")

            if current_path.exists():
                current_bytes = self._read_regular(current_path)
                decoder(current_bytes)
                self._atomic_replace(previous_path, current_bytes)
                self.fault_hook(f"{label}:previous_durable")

            self._atomic_replace(current_path, payload, after_replace=current_replaced)
            self.fault_hook(f"{label}:current_durable")
            self._unlink_and_sync(pending_path)
            self.fault_hook(f"{label}:pending_cleared")
        except BaseException as exc:
            if commit_may_recover:
                raise CommitUnknown(
                    f"{label} recovery intent became visible before completion acknowledgement"
                ) from exc
            raise
        return record

    def _read_regular(self, path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StateCorrupt(f"state record cannot be opened safely: {path}: {exc}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise StateCorrupt(f"state record is not a regular file: {path}")
            self._require_owner(details, path)
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise StateCorrupt(f"state record mode is not 0600: {path}")
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = os.read(descriptor, 1024 * 1024)
                except InterruptedError:
                    continue
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)

    def _atomic_replace(
        self,
        destination: Path,
        data: bytes,
        *,
        after_replace: Callable[[], None] | None = None,
    ) -> None:
        self._ensure_root()
        self._ensure_parent(destination)
        try:
            details = destination.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or destination.is_symlink()
            ):
                raise StatePathError(
                    f"state replacement target is not a regular file: {destination}"
                )
            self._require_owner(details, destination)
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise StatePathError(f"state replacement target mode is not 0600: {destination}")
        temporary = destination.parent / f".{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        replaced = False
        try:
            os.fchmod(descriptor, 0o600)
            self._write_all(descriptor, data)
            self.syscalls.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        try:
            self.syscalls.replace(temporary, destination)
            replaced = True
            if after_replace is not None:
                after_replace()
            self._fsync_directory(destination.parent)
        finally:
            if not replaced:
                temporary.unlink(missing_ok=True)

    def _write_all(self, descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            try:
                written = self.syscalls.write(descriptor, view[offset:])
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            if written <= 0:
                raise OSError(errno.EIO, "write returned no progress")
            offset += written

    def _unlink_and_sync(self, path: Path) -> None:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or path.is_symlink():
            raise StatePathError(f"state cleanup target is not a regular file: {path}")
        self._require_owner(details, path)
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise StatePathError(f"state cleanup target mode is not 0600: {path}")
        path.unlink()
        self._fsync_directory(path.parent)

    def _fsync_directory(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISDIR(details.st_mode):
                raise StatePathError(f"state directory cannot be synced safely: {path}")
            self._require_owner(details, path)
            self.syscalls.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "CommitUnknown",
    "DurableStateRoot",
    "FaultHook",
    "InjectedFault",
    "LockOrderError",
    "PosixSyscalls",
    "REGISTRY_LOCK_RANK",
    "RuntimeCapabilities",
    "StateCorrupt",
    "StatePathError",
    "Syscalls",
    "UnsupportedRuntime",
    "WORKSPACE_LOCK_RANK",
    "WorkspaceRuntimeError",
]
