"""Crash-durable primitives for external workspace lifecycle state."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import time
from typing import Callable, Iterator, Protocol, Sequence, TypeVar
import uuid


FaultHook = Callable[[str], None]
REGISTRY_LOCK_RANK = 10
WORKSPACE_LOCK_RANK = 20
GENERATION_LOCK_RANK = 30
_LOCK_STACK: ContextVar[tuple[tuple[int, str], ...]] = ContextVar(
    "graphify_workspace_lock_stack",
    default=(),
)
_ATOMIC_TEMP_RE = re.compile(
    r"^\.(?P<destination>.+)\.tmp-(?P<pid>[1-9][0-9]*)-(?P<nonce>[0-9a-f]{32})$",
    re.ASCII,
)
_PRIVATE_DIRECTORY_MODES = frozenset({0o700})
_PRIVATE_FILE_MODES = frozenset({0o600})


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


class StateRecordMissing(StateCorrupt):
    """A required current durable record is absent."""


class StateRecoveryRequired(StateCorrupt):
    """A stable read found a durable pending record requiring recovery."""


class CommitUnknown(WorkspaceRuntimeError):
    code = "commit_unknown"


class LockOrderError(WorkspaceRuntimeError):
    code = "lock_order"


class LockTimeout(WorkspaceRuntimeError):
    code = "lock_timeout"

    def __init__(
        self,
        detail: str,
        *,
        phase: str = "deadline",
        kind: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.phase = phase
        self.kind = kind


def require_before_deadline(deadline_ns: int | None, detail: str) -> None:
    """Raise the stable timeout error after an absolute monotonic deadline."""

    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        raise LockTimeout(detail)


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

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None: ...

    def unlink(self, path: Path) -> None: ...

    def unlink_at(self, path: str, *, dir_fd: int) -> None: ...

    def rmdir(self, path: Path) -> None: ...

    def rmdir_at(self, path: str, *, dir_fd: int) -> None: ...

    def mkdir(self, path: Path, mode: int) -> None: ...

    def mkdir_at(self, path: str, mode: int, *, dir_fd: int) -> None: ...


class PosixSyscalls:
    """Injectable POSIX syscall surface used by durability tests."""

    def write(self, descriptor: int, data: memoryview) -> int:
        return os.write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        os.replace(
            source,
            destination,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=destination_dir_fd,
        )

    def unlink(self, path: Path) -> None:
        os.unlink(path)

    def unlink_at(self, path: str, *, dir_fd: int) -> None:
        os.unlink(path, dir_fd=dir_fd)

    def rmdir(self, path: Path) -> None:
        os.rmdir(path)

    def rmdir_at(self, path: str, *, dir_fd: int) -> None:
        os.rmdir(path, dir_fd=dir_fd)

    def mkdir(self, path: Path, mode: int) -> None:
        os.mkdir(path, mode)

    def mkdir_at(self, path: str, mode: int, *, dir_fd: int) -> None:
        os.mkdir(path, mode, dir_fd=dir_fd)


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
        _require_supported_runtime: bool = True,
    ) -> None:
        if not root.is_absolute():
            raise StatePathError("state root must be an absolute path")
        lexical_root = Path(os.path.abspath(root))
        if lexical_root.is_symlink():
            raise StatePathError(f"state root must not be a symbolic link: {lexical_root}")
        self.root = lexical_root
        self.capabilities = capabilities or RuntimeCapabilities.detect(self.root)
        if _require_supported_runtime:
            self.capabilities.require_supported()
        self.fault_hook = fault_hook or (lambda _event: None)
        self.syscalls = syscalls or PosixSyscalls()

    @classmethod
    def read_optional_bytes_for_inspection(
        cls,
        root: Path,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> bytes | None:
        """Read existing bytes safely before the runtime-support verdict."""

        inspection_capabilities = capabilities or RuntimeCapabilities(
            system="inspection",
            filesystem="unknown",
            elevated=False,
            local=True,
        )
        state = cls(
            root,
            capabilities=inspection_capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
            _require_supported_runtime=False,
        )
        return state.read_optional_existing_bytes(relative, max_bytes=max_bytes)

    def _open_root_parent(self, *, allow_missing: bool) -> int | None:
        """Open the lexical root parent without following any ancestor link."""

        parent = self.root.parent
        anchor = Path(parent.anchor)
        try:
            descriptor = os.open(anchor, self._directory_open_flags())
        except OSError as exc:
            raise StatePathError("filesystem root cannot be inspected safely") from exc
        current = anchor
        for part in parent.relative_to(anchor).parts:
            candidate = current / part
            try:
                child_descriptor = os.open(
                    part,
                    self._directory_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError as exc:
                os.close(descriptor)
                if allow_missing:
                    return None
                raise StatePathError(
                    f"state root parent must already exist: {candidate}"
                ) from exc
            except OSError as exc:
                os.close(descriptor)
                raise StatePathError(
                    "state root ancestor is a symbolic link or not a directory: "
                    f"{candidate}"
                ) from exc
            try:
                details = os.fstat(child_descriptor)
                if not stat.S_ISDIR(details.st_mode):
                    raise StatePathError(
                        f"state root ancestor is not a directory: {candidate}"
                    )
            except BaseException:
                os.close(child_descriptor)
                os.close(descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current = candidate
        try:
            self._require_owned_directory_descriptor(descriptor, parent)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _root_directory(
        self,
        *,
        ensure: bool,
        allow_missing: bool = False,
    ) -> Iterator[int | None]:
        """Hold the root and its verified parent across one contained operation."""

        parent = self.root.parent
        if parent == self.root:
            raise StatePathError("state root must not be the filesystem root")
        parent_descriptor = self._open_root_parent(allow_missing=allow_missing)
        if parent_descriptor is None:
            yield None
            return
        try:
            opened_parent = os.fstat(parent_descriptor)

            def require_parent_binding() -> None:
                try:
                    bound_parent = parent.lstat()
                except OSError as exc:
                    raise StatePathError(
                        f"state root parent changed while opening: {parent}"
                    ) from exc
                if (
                    not stat.S_ISDIR(bound_parent.st_mode)
                    or (opened_parent.st_dev, opened_parent.st_ino)
                    != (bound_parent.st_dev, bound_parent.st_ino)
                ):
                    raise StatePathError(
                        f"state root parent changed while opening: {parent}"
                    )

            created = False
            try:
                root_descriptor = self._open_owned_directory_at(
                    parent_descriptor,
                    self.root.name,
                    self.root,
                )
            except FileNotFoundError:
                if not ensure:
                    if allow_missing:
                        require_parent_binding()
                        yield None
                        return
                    raise StatePathError(f"state directory is missing: {self.root}")
                try:
                    self.syscalls.mkdir_at(
                        self.root.name,
                        0o700,
                        dir_fd=parent_descriptor,
                    )
                    created = True
                except FileExistsError:
                    pass
                root_descriptor = self._open_owned_directory_at(
                    parent_descriptor,
                    self.root.name,
                    self.root,
                )
            try:
                if ensure:
                    os.fchmod(root_descriptor, 0o700)
                opened = self._require_private_directory_descriptor(
                    root_descriptor,
                    self.root,
                )
                try:
                    bound = os.stat(
                        self.root.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise StatePathError(
                        f"state root changed while opening: {self.root}"
                    ) from exc
                if (
                    not stat.S_ISDIR(bound.st_mode)
                    or (opened.st_dev, opened.st_ino) != (bound.st_dev, bound.st_ino)
                ):
                    raise StatePathError(
                        f"state root changed while opening: {self.root}"
                    )
                require_parent_binding()
                if created:
                    self.syscalls.fsync(parent_descriptor)
                yield root_descriptor
            finally:
                os.close(root_descriptor)
        finally:
            os.close(parent_descriptor)

    def _ensure_root(self) -> None:
        with self._root_directory(ensure=True):
            pass

    def root_exists_for_inspection(self) -> bool:
        """Probe the owned state root without creating it or suppressing unsafe paths."""

        with self._root_directory(ensure=False, allow_missing=True) as descriptor:
            return descriptor is not None

    @staticmethod
    def _require_owner(details: os.stat_result, path: Path) -> None:
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            raise StatePathError(f"state path is not owned by the current user: {path}")

    @staticmethod
    def _directory_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    @staticmethod
    def _regular_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )

    @staticmethod
    def _stat_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        return (
            details.st_dev,
            details.st_ino,
            details.st_mode,
            details.st_nlink,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
        )

    def _require_directory_descriptor(
        self,
        descriptor: int,
        path: Path,
        *,
        allowed_modes: frozenset[int],
    ) -> os.stat_result:
        details = self._require_owned_directory_descriptor(descriptor, path)
        if stat.S_IMODE(details.st_mode) not in allowed_modes:
            if allowed_modes == _PRIVATE_DIRECTORY_MODES:
                raise StatePathError(f"state directory mode is not 0700: {path}")
            modes = ", ".join(f"{mode:04o}" for mode in sorted(allowed_modes))
            raise StatePathError(f"state directory mode is not allowed ({modes}): {path}")
        return details

    def _require_owned_directory_descriptor(
        self,
        descriptor: int,
        path: Path,
    ) -> os.stat_result:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise StatePathError(f"state directory is not a real directory: {path}")
        self._require_owner(details, path)
        return details

    def _require_private_directory_descriptor(
        self,
        descriptor: int,
        path: Path,
    ) -> os.stat_result:
        return self._require_directory_descriptor(
            descriptor,
            path,
            allowed_modes=_PRIVATE_DIRECTORY_MODES,
        )

    def _open_directory_at(
        self,
        parent_descriptor: int,
        name: str,
        path: Path,
        *,
        allowed_modes: frozenset[int] | None,
        allow_missing: bool = False,
    ) -> int | None:
        try:
            descriptor = os.open(name, self._directory_open_flags(), dir_fd=parent_descriptor)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        except OSError as exc:
            raise StatePathError(
                f"state directory is linked or not a directory: {path}"
            ) from exc
        try:
            if allowed_modes is None:
                self._require_owned_directory_descriptor(descriptor, path)
            else:
                self._require_directory_descriptor(
                    descriptor,
                    path,
                    allowed_modes=allowed_modes,
                )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_owned_directory_at(
        self,
        parent_descriptor: int,
        name: str,
        path: Path,
    ) -> int:
        descriptor = self._open_directory_at(
            parent_descriptor,
            name,
            path,
            allowed_modes=None,
        )
        if descriptor is None:  # pragma: no cover - allow_missing is false
            raise StatePathError(f"state directory is missing: {path}")
        return descriptor

    def _require_regular_details(
        self,
        details: os.stat_result,
        path: Path,
        *,
        allowed_modes: frozenset[int],
    ) -> None:
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise StatePathError(f"state file is not a singular regular file: {path}")
        self._require_owner(details, path)
        if stat.S_IMODE(details.st_mode) not in allowed_modes:
            raise StatePathError(f"state file mode is not allowed: {path}")

    def _require_regular_descriptor(
        self,
        descriptor: int,
        path: Path,
        *,
        allowed_modes: frozenset[int],
    ) -> os.stat_result:
        details = os.fstat(descriptor)
        self._require_regular_details(details, path, allowed_modes=allowed_modes)
        return details

    @contextmanager
    def _existing_private_directory(
        self,
        relative: str | Path,
        *,
        allow_missing: bool = False,
    ) -> Iterator[int | None]:
        """Open one private directory through descriptor-relative no-follow traversal."""

        if Path(relative) in {Path(), Path(".")}:
            destination = self.root
            parts: tuple[str, ...] = ()
        else:
            destination = self.path(relative)
            parts = destination.relative_to(self.root).parts
        with self._root_directory(
            ensure=False,
            allow_missing=allow_missing,
        ) as root_descriptor:
            if root_descriptor is None:
                yield None
                return
            descriptor = os.dup(root_descriptor)
            try:
                current = self.root
                for part in parts:
                    candidate = current / part
                    try:
                        child = self._open_directory_at(
                            descriptor,
                            part,
                            candidate,
                            allowed_modes=_PRIVATE_DIRECTORY_MODES,
                        )
                    except FileNotFoundError as exc:
                        if allow_missing:
                            yield None
                            return
                        raise StatePathError(
                            f"state directory is missing: {candidate}"
                        ) from exc
                    if child is None:  # pragma: no cover - allow_missing is false
                        raise StatePathError(f"state directory is missing: {candidate}")
                    os.close(descriptor)
                    descriptor = child
                    current = candidate
                yield descriptor
            finally:
                os.close(descriptor)

    def _open_existing_file(
        self,
        path: Path,
        *,
        allow_missing_parent: bool = False,
    ) -> int | None:
        try:
            relative_parent = path.parent.relative_to(self.root)
        except ValueError as exc:
            raise StatePathError(f"state file escapes root: {path}") from exc
        flags = self._regular_open_flags()
        with self._existing_private_directory(
            relative_parent,
            allow_missing=allow_missing_parent,
        ) as parent_descriptor:
            if parent_descriptor is None:
                return None
            return os.open(path.name, flags, dir_fd=parent_descriptor)

    @staticmethod
    def _contained_parts(relative: str | Path) -> tuple[str, ...]:
        if Path(relative) in {Path(), Path(".")}:
            return ()
        pure = PurePosixPath(Path(relative).as_posix())
        if pure.is_absolute() or not pure.parts or ".." in pure.parts or "." in pure.parts:
            raise StatePathError(f"contained path must be relative: {relative}")
        return pure.parts

    @contextmanager
    def _existing_directory_beneath(
        self,
        anchor_descriptor: int,
        anchor_path: Path,
        relative: str | Path,
        *,
        allowed_modes: frozenset[int],
    ) -> Iterator[int]:
        """Open a directory beneath a held anchor without following links."""

        descriptor = os.dup(anchor_descriptor)
        current = anchor_path
        try:
            for part in self._contained_parts(relative):
                candidate = current / part
                try:
                    child = self._open_directory_at(
                        descriptor,
                        part,
                        candidate,
                        allowed_modes=allowed_modes,
                    )
                except OSError as exc:
                    raise StatePathError(
                        f"state directory is linked or not a directory: {candidate}"
                    ) from exc
                if child is None:  # pragma: no cover - allow_missing is false
                    raise StatePathError(f"state directory is missing: {candidate}")
                os.close(descriptor)
                descriptor = child
                current = candidate
            yield descriptor
        finally:
            os.close(descriptor)

    def _require_private_directory_chain(self, destination: Path) -> None:
        """Validate an existing state-directory chain without repairing it."""

        if destination != self.root and self.root not in destination.parents:
            raise StatePathError(f"state directory escapes root: {destination}")
        relative = Path(".") if destination == self.root else destination.relative_to(self.root)
        with self._existing_private_directory(relative):
            pass

    def path(self, relative: str | Path) -> Path:
        pure = PurePosixPath(Path(relative).as_posix())
        if pure.is_absolute() or not pure.parts or ".." in pure.parts or "." in pure.parts:
            raise StatePathError(f"state path must be a contained relative path: {relative}")
        candidate = self.root.joinpath(*pure.parts)
        if self.root not in candidate.parents:
            raise StatePathError(f"state path escapes root: {relative}")
        return candidate

    def ensure_directory(self, relative: str | Path) -> Path:
        destination = (
            self.root
            if Path(relative) in {Path(), Path(".")}
            else self.path(relative)
        )
        with self._root_directory(ensure=True) as root_descriptor:
            if root_descriptor is None:  # pragma: no cover - ensure is true
                raise StatePathError(f"state directory is missing: {self.root}")
            if destination == self.root:
                return destination
            descriptor = os.dup(root_descriptor)
            try:
                current = self.root
                for part in destination.relative_to(self.root).parts:
                    child_path = current / part
                    try:
                        child_descriptor = self._open_owned_directory_at(
                            descriptor,
                            part,
                            child_path,
                        )
                    except FileNotFoundError:
                        try:
                            self.syscalls.mkdir_at(part, 0o700, dir_fd=descriptor)
                        except FileExistsError:
                            pass
                        else:
                            self.syscalls.fsync(descriptor)
                        child_descriptor = self._open_owned_directory_at(
                            descriptor,
                            part,
                            child_path,
                        )
                    try:
                        os.fchmod(child_descriptor, 0o700)
                        opened = self._require_private_directory_descriptor(
                            child_descriptor,
                            child_path,
                        )
                        bound = os.stat(
                            part,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISDIR(bound.st_mode)
                            or (opened.st_dev, opened.st_ino)
                            != (bound.st_dev, bound.st_ino)
                        ):
                            raise StatePathError(
                                f"state directory changed while opening: {child_path}"
                            )
                    except BaseException:
                        os.close(child_descriptor)
                        raise
                    os.close(descriptor)
                    descriptor = child_descriptor
                    current = child_path
                return destination
            finally:
                os.close(descriptor)

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

    def require_existing_directory_chain(self, relative: str | Path) -> Path:
        """Validate a private directory chain without mutating or following links."""

        destination = self.path(relative)
        self._require_private_directory_chain(destination)
        return destination

    @contextmanager
    def existing_private_directory(self, relative: str | Path) -> Iterator[int]:
        """Hold an owned 0700 directory opened by descriptor-relative traversal."""

        with self._existing_private_directory(relative) as descriptor:
            if descriptor is None:  # pragma: no cover - allow_missing is false
                raise StatePathError(f"state directory is missing: {self.path(relative)}")
            yield descriptor

    def list_existing_private_directories(
        self,
        relative: str | Path,
        *,
        allow_missing: bool = False,
    ) -> tuple[str, ...]:
        """List owned 0700 child directories without following any path component."""

        destination = self.path(relative)
        with self._existing_private_directory(
            destination.relative_to(self.root),
            allow_missing=allow_missing,
        ) as descriptor:
            if descriptor is None:
                return ()
            try:
                with os.scandir(descriptor) as entries:
                    names = sorted(entry.name for entry in entries)
            except OSError as exc:
                raise StatePathError(
                    f"state directory cannot be enumerated safely: {destination}: {exc}"
                ) from exc
            for name in names:
                child_path = destination / name
                child = self._open_directory_at(
                    descriptor,
                    name,
                    child_path,
                    allowed_modes=_PRIVATE_DIRECTORY_MODES,
                )
                if child is None:  # pragma: no cover - allow_missing is false
                    raise StatePathError(f"state directory is missing: {child_path}")
                os.close(child)
            return tuple(names)

    def private_directory_exists(self, relative: str | Path) -> bool:
        """Probe one private directory without following any path component."""

        destination = self.path(relative)
        parent_relative = destination.parent.relative_to(self.root)
        with self._existing_private_directory(
            parent_relative,
            allow_missing=True,
        ) as parent_descriptor:
            if parent_descriptor is None:
                return False
            descriptor = self._open_directory_at(
                parent_descriptor,
                destination.name,
                destination,
                allowed_modes=_PRIVATE_DIRECTORY_MODES,
                allow_missing=True,
            )
            if descriptor is None:
                return False
            os.close(descriptor)
            return True

    def private_file_exists(self, relative: str | Path) -> bool:
        """Probe one owned 0600 file without following any path component."""

        path = self.path(relative)
        try:
            descriptor = self._open_existing_file(path, allow_missing_parent=True)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StatePathError(f"state file cannot be opened safely: {path}") from exc
        if descriptor is None:
            return False
        try:
            self._require_regular_descriptor(
                descriptor,
                path,
                allowed_modes=_PRIVATE_FILE_MODES,
            )
        finally:
            os.close(descriptor)
        return True

    def _tree_bytes_descriptor(
        self,
        descriptor: int,
        path: Path,
        *,
        allowed_directory_modes: frozenset[int],
        allowed_file_modes: frozenset[int],
    ) -> int:
        before = os.fstat(descriptor)
        try:
            names = sorted(entry.name for entry in os.scandir(descriptor))
        except OSError as exc:
            raise StatePathError(f"state tree cannot be enumerated safely: {path}: {exc}") from exc
        total = 0
        for name in names:
            candidate = path / name
            try:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise StatePathError(
                    f"state tree entry cannot be inspected safely: {candidate}: {exc}"
                ) from exc
            if stat.S_ISDIR(details.st_mode):
                child = self._open_directory_at(
                    descriptor,
                    name,
                    candidate,
                    allowed_modes=allowed_directory_modes,
                )
                if child is None:  # pragma: no cover - allow_missing is false
                    raise StatePathError(f"state tree directory is missing: {candidate}")
                try:
                    opened_directory = os.fstat(child)
                    if (opened_directory.st_dev, opened_directory.st_ino) != (
                        details.st_dev,
                        details.st_ino,
                    ):
                        raise StatePathError(
                            f"state tree directory changed while opening: {candidate}"
                        )
                    total += self._tree_bytes_descriptor(
                        child,
                        candidate,
                        allowed_directory_modes=allowed_directory_modes,
                        allowed_file_modes=allowed_file_modes,
                    )
                finally:
                    os.close(child)
                continue
            self._require_regular_details(
                details,
                candidate,
                allowed_modes=allowed_file_modes,
            )
            try:
                file_descriptor = os.open(
                    name,
                    self._regular_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise StatePathError(f"state tree file cannot be opened safely: {candidate}") from exc
            try:
                opened = self._require_regular_descriptor(
                    file_descriptor,
                    candidate,
                    allowed_modes=allowed_file_modes,
                )
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                expected_identity = self._stat_identity(details)
                if (
                    self._stat_identity(opened) != expected_identity
                    or self._stat_identity(current) != expected_identity
                ):
                    raise StatePathError(f"state tree file changed while opening: {candidate}")
                total += opened.st_size
            finally:
                os.close(file_descriptor)
        after_names = sorted(entry.name for entry in os.scandir(descriptor))
        after = os.fstat(descriptor)
        if names != after_names or self._stat_identity(before) != self._stat_identity(after):
            raise StatePathError(f"state tree changed while scanning: {path}")
        return total

    def tree_bytes(
        self,
        relative: str | Path,
        *,
        allowed_directory_modes: frozenset[int],
        allowed_file_modes: frozenset[int],
    ) -> int:
        """Measure a private tree through a held no-follow root descriptor."""

        path = self.path(relative)
        with self._existing_private_directory(
            relative,
            allow_missing=True,
        ) as descriptor:
            if descriptor is None:
                raise FileNotFoundError(path)
            return self._tree_bytes_descriptor(
                descriptor,
                path,
                allowed_directory_modes=allowed_directory_modes,
                allowed_file_modes=allowed_file_modes,
            )

    def _remove_tree_contents_descriptor(
        self,
        descriptor: int,
        path: Path,
        *,
        allowed_directory_modes: frozenset[int],
        allowed_file_modes: frozenset[int],
    ) -> None:
        names = sorted(entry.name for entry in os.scandir(descriptor))
        for name in names:
            candidate = path / name
            details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                child = self._open_directory_at(
                    descriptor,
                    name,
                    candidate,
                    allowed_modes=allowed_directory_modes,
                )
                if child is None:  # pragma: no cover - allow_missing is false
                    raise StatePathError(f"state tree directory is missing: {candidate}")
                try:
                    self._remove_tree_contents_descriptor(
                        child,
                        candidate,
                        allowed_directory_modes=allowed_directory_modes,
                        allowed_file_modes=allowed_file_modes,
                    )
                finally:
                    os.close(child)
                self.syscalls.rmdir_at(name, dir_fd=descriptor)
                continue
            self._require_regular_details(
                details,
                candidate,
                allowed_modes=allowed_file_modes,
            )
            self.syscalls.unlink_at(name, dir_fd=descriptor)

    def remove_private_tree(
        self,
        relative: str | Path,
        *,
        allowed_directory_modes: frozenset[int],
        allowed_file_modes: frozenset[int],
    ) -> bool:
        """Validate and remove one private tree through held no-follow descriptors."""

        path = self.path(relative)
        parent_relative = path.parent.relative_to(self.root)
        with self._existing_private_directory(
            parent_relative,
            allow_missing=True,
        ) as parent_descriptor:
            if parent_descriptor is None:
                return False
            descriptor = self._open_directory_at(
                parent_descriptor,
                path.name,
                path,
                allowed_modes=_PRIVATE_DIRECTORY_MODES,
                allow_missing=True,
            )
            if descriptor is None:
                return False
            try:
                self._tree_bytes_descriptor(
                    descriptor,
                    path,
                    allowed_directory_modes=allowed_directory_modes,
                    allowed_file_modes=allowed_file_modes,
                )
                self._remove_tree_contents_descriptor(
                    descriptor,
                    path,
                    allowed_directory_modes=allowed_directory_modes,
                    allowed_file_modes=allowed_file_modes,
                )
            finally:
                os.close(descriptor)
            self.syscalls.rmdir_at(path.name, dir_fd=parent_descriptor)
            self.syscalls.fsync(parent_descriptor)
        return True

    def fsync_contained_regular_file(
        self,
        anchor: str | Path,
        relative: str | Path,
        *,
        allowed_directory_modes: frozenset[int],
        allowed_file_modes: frozenset[int],
    ) -> None:
        """Sync one file beneath a held private anchor without path re-resolution."""

        parts = self._contained_parts(relative)
        if not parts:
            raise StatePathError("contained regular-file path must not be empty")
        anchor_path = self.path(anchor)
        relative_path = Path(*parts)
        with self.existing_private_directory(anchor) as anchor_descriptor:
            with self._existing_directory_beneath(
                anchor_descriptor,
                anchor_path,
                relative_path.parent,
                allowed_modes=allowed_directory_modes,
            ) as parent_descriptor:
                path = anchor_path / relative_path
                try:
                    descriptor = os.open(
                        relative_path.name,
                        self._regular_open_flags(),
                        dir_fd=parent_descriptor,
                    )
                except OSError as exc:
                    raise StatePathError(
                        f"state file cannot be opened safely: {path}: {exc}"
                    ) from exc
                try:
                    self._require_regular_descriptor(
                        descriptor,
                        path,
                        allowed_modes=allowed_file_modes,
                    )
                    self.syscalls.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def fsync_contained_directory(
        self,
        anchor: str | Path,
        relative: str | Path,
        *,
        allowed_directory_modes: frozenset[int],
    ) -> None:
        """Sync one directory beneath a held private anchor without following links."""

        anchor_path = self.path(anchor)
        with self.existing_private_directory(anchor) as anchor_descriptor:
            with self._existing_directory_beneath(
                anchor_descriptor,
                anchor_path,
                relative,
                allowed_modes=allowed_directory_modes,
            ) as descriptor:
                self.syscalls.fsync(descriptor)

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
        if stack and (
            rank < stack[-1][0]
            or (rank == stack[-1][0] and name <= stack[-1][1])
        ):
            raise LockOrderError(
                f"{name} lock cannot be acquired after {stack[-1][1]} lock"
            )
        path = self.path(relative)
        self._ensure_parent(path)
        flags = os.O_RDWR
        flags |= (
            getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        parent_relative = path.parent.relative_to(self.root)
        with self.existing_private_directory(parent_relative) as parent_descriptor:
            descriptor: int | None = None
            try:
                for _attempt in range(5):
                    try:
                        descriptor = os.open(
                            path.name,
                            flags | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=parent_descriptor,
                        )
                    except FileExistsError:
                        try:
                            descriptor = os.open(
                                path.name,
                                flags,
                                dir_fd=parent_descriptor,
                            )
                        except FileNotFoundError:
                            continue
                    break
            except OSError as exc:
                raise StatePathError(
                    f"state lock cannot be opened safely: {path}: {exc}"
                ) from exc
            if descriptor is None:
                raise StatePathError(f"state lock binding did not stabilize: {path}")
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise StatePathError(f"state lock is not a regular file: {path}")
                self._require_owner(details, path)
                os.fchmod(descriptor, 0o600)
                try:
                    bound = os.stat(
                        path.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise StatePathError(
                        f"state lock binding cannot be inspected safely: {path}"
                    ) from exc
                if (
                    not stat.S_ISREG(bound.st_mode)
                    or (details.st_dev, details.st_ino) != (bound.st_dev, bound.st_ino)
                ):
                    raise StatePathError(f"state lock changed while opening: {path}")
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

    @contextmanager
    def existing_lock(
        self,
        relative: str | Path,
        *,
        rank: int,
        name: str,
        exclusive: bool = True,
        blocking: bool = True,
        deadline_ns: int | None = None,
        kind: str = "state",
    ) -> Iterator[None]:
        """Lock existing coordination state without mutation, bounded if requested."""

        stack = _LOCK_STACK.get()
        if stack and (
            rank < stack[-1][0]
            or (rank == stack[-1][0] and name <= stack[-1][1])
        ):
            raise LockOrderError(
                f"{name} lock cannot be acquired after {stack[-1][1]} lock"
            )
        path = self.path(relative)
        try:
            descriptor = self._open_existing_file(path)
        except FileNotFoundError as exc:
            raise StatePathError(f"{kind} lock is missing: {path}") from exc
        except OSError as exc:
            raise StatePathError(f"{kind} lock cannot be opened safely: {path}") from exc
        if descriptor is None:  # pragma: no cover - allow_missing_parent is false
            raise StatePathError(f"{kind} lock is missing: {path}")
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise StatePathError(f"state lock is not a singular regular file: {path}")
            self._require_owner(details, path)
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise StatePathError(f"state lock mode is not 0600: {path}")
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - rejected by capability gate
                raise UnsupportedRuntime("fcntl is required for workspace locking") from exc
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking or deadline_ns is not None:
                operation |= fcntl.LOCK_NB
            while True:
                if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                    raise LockTimeout(
                        f"{kind} lock acquisition timed out: {path}",
                        phase="acquire",
                        kind=kind,
                    )
                try:
                    fcntl.flock(descriptor, operation)
                except InterruptedError:
                    continue
                except BlockingIOError:
                    if deadline_ns is None:
                        raise
                    remaining_ns = deadline_ns - time.monotonic_ns()
                    if remaining_ns <= 0:
                        raise LockTimeout(
                            f"{kind} lock acquisition timed out: {path}",
                            phase="acquire",
                            kind=kind,
                        ) from None
                    time.sleep(min(0.001, remaining_ns / 1_000_000_000))
                    continue
                if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                    raise LockTimeout(
                        f"{kind} lock acquisition timed out: {path}",
                        phase="acquire",
                        kind=kind,
                    )
                break
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

    def install_once_bytes(
        self,
        relative: str | Path,
        data: bytes,
        *,
        label: str,
    ) -> Path:
        """Durably install immutable bytes once, preserving a stable inode on retry."""

        self._ensure_root()
        path = self.path(relative)
        self._ensure_parent(path)
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            if self._read_regular(path) != data:
                raise StateCorrupt(f"immutable state conflicts at {path}")
            return path
        visible = False

        def replaced() -> None:
            nonlocal visible
            visible = True
            self.fault_hook(f"{label}:installed")

        try:
            self._atomic_replace(path, data, after_replace=replaced)
        except BaseException as exc:
            if visible:
                raise CommitUnknown(
                    f"{label} became visible before durability acknowledgement"
                ) from exc
            raise
        if self._read_regular(path) != data:
            raise StateCorrupt(f"immutable state verification failed at {path}")
        return path

    def atomic_replace_bytes(
        self,
        relative: str | Path,
        data: bytes,
        *,
        label: str,
    ) -> Path:
        """Durably replace one contained record and surface uncertain visibility."""

        destination = self.path(relative)
        visible = False

        def replaced() -> None:
            nonlocal visible
            visible = True
            self.fault_hook(f"{label}:replaced")

        try:
            self._atomic_replace(destination, data, after_replace=replaced)
        except BaseException as exc:
            if visible:
                raise CommitUnknown(
                    f"{label} became visible before durability acknowledgement"
                ) from exc
            raise
        return destination

    def rename_contained(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        label: str,
    ) -> Path:
        """Atomically move one private directory and sync both held parents."""

        self._ensure_root()
        source_path = self.path(source)
        destination_path = self.path(destination)
        self._ensure_parent(destination_path)
        source_parent_relative = source_path.parent.relative_to(self.root)
        destination_parent_relative = destination_path.parent.relative_to(self.root)
        visible = False
        with self.existing_private_directory(source_parent_relative) as source_parent:
            with self.existing_private_directory(
                destination_parent_relative
            ) as destination_parent:
                source_descriptor = self._open_directory_at(
                    source_parent,
                    source_path.name,
                    source_path,
                    allowed_modes=_PRIVATE_DIRECTORY_MODES,
                    allow_missing=True,
                )
                if source_descriptor is None:
                    raise StatePathError(f"rename source is missing: {source_path}")
                try:
                    try:
                        os.stat(
                            destination_path.name,
                            dir_fd=destination_parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise StatePathError(
                            f"rename destination already exists: {destination_path}"
                        )
                    self.fault_hook(f"{label}:before_rename")
                    try:
                        self.syscalls.replace_at(
                            source_path.name,
                            destination_path.name,
                            source_dir_fd=source_parent,
                            destination_dir_fd=destination_parent,
                        )
                        visible = True
                        self.fault_hook(f"{label}:renamed")
                        self.syscalls.fsync(source_parent)
                        self.fault_hook(f"{label}:source_parent_durable")
                        if destination_parent_relative != source_parent_relative:
                            self.syscalls.fsync(destination_parent)
                        self.fault_hook(f"{label}:destination_parent_durable")
                    except BaseException as exc:
                        if visible:
                            raise CommitUnknown(
                                f"{label} rename became visible before both directories were durable"
                            ) from exc
                        raise
                finally:
                    os.close(source_descriptor)
        return destination_path

    def unlink_and_sync(self, relative: str | Path, *, label: str) -> None:
        self._unlink_and_sync(self.path(relative), label=label)

    def fsync_directory(self, relative: str | Path) -> None:
        with self.existing_private_directory(relative) as descriptor:
            self.syscalls.fsync(descriptor)

    def fsync_regular_file(
        self,
        relative: str | Path,
        *,
        allowed_modes: frozenset[int] = frozenset({0o600}),
    ) -> None:
        path = self.path(relative)
        try:
            descriptor = self._open_existing_file(path)
        except OSError as exc:
            raise StatePathError(f"state file cannot be opened safely: {path}: {exc}") from exc
        if descriptor is None:  # pragma: no cover - allow_missing_parent is false
            raise StatePathError(f"state file parent is missing: {path.parent}")
        try:
            self._require_regular_descriptor(
                descriptor,
                path,
                allowed_modes=allowed_modes,
            )
            self.syscalls.fsync(descriptor)
        finally:
            os.close(descriptor)

    def cleanup_atomic_temps(
        self,
        relative: str | Path,
        *,
        destination_name: str | None = None,
    ) -> tuple[Path, ...]:
        """Remove exact owned orphan files created by ``_atomic_replace``.

        The caller must hold the writer lock for ``relative``. Unsafe entries
        that match the private temp namespace fail closed; unrelated dotfiles
        remain visible to the caller's ordinary directory validation.
        """

        directory = self.root if Path(relative) in {Path(), Path(".")} else self.path(relative)
        relative_directory = (
            Path(".") if directory == self.root else directory.relative_to(self.root)
        )
        removed: list[Path] = []
        with self._existing_private_directory(
            relative_directory,
            allow_missing=True,
        ) as descriptor:
            if descriptor is None:
                return ()
            try:
                names = sorted(entry.name for entry in os.scandir(descriptor))
            except OSError as exc:
                raise StatePathError(
                    f"atomic-temp parent cannot be enumerated safely: {directory}: {exc}"
                ) from exc
            for name in names:
                match = _ATOMIC_TEMP_RE.fullmatch(name)
                if match is None or (
                    destination_name is not None
                    and match.group("destination") != destination_name
                ):
                    continue
                entry = directory / name
                try:
                    entry_details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    raise StatePathError(
                        f"atomic-temp orphan cannot be inspected safely: {entry}"
                    ) from exc
                try:
                    self._require_regular_details(
                        entry_details,
                        entry,
                        allowed_modes=_PRIVATE_FILE_MODES,
                    )
                except StatePathError as exc:
                    raise StatePathError(f"atomic-temp orphan is unsafe: {entry}") from exc
                self.syscalls.unlink_at(name, dir_fd=descriptor)
                removed.append(entry)
            if removed:
                self.syscalls.fsync(descriptor)
        return tuple(removed)

    @contextmanager
    def existing_generation_lock(
        self,
        relative: str | Path,
        *,
        generation_id: str,
        exclusive: bool,
        blocking: bool = True,
        deadline_ns: int | None = None,
    ) -> Iterator[None]:
        """Lock a retained coordination object without any mutating syscall."""

        name = f"generation:{generation_id}"
        with self.existing_lock(
            relative,
            rank=GENERATION_LOCK_RANK,
            name=name,
            exclusive=exclusive,
            blocking=blocking,
            deadline_ns=deadline_ns,
            kind="generation",
        ):
            yield

    @contextmanager
    def existing_generation_locks(
        self,
        locks: Sequence[tuple[str, str | Path]],
        *,
        exclusive: bool,
        blocking: bool = True,
    ) -> Iterator[None]:
        ordered = list(locks)
        if ordered != sorted(ordered, key=lambda item: item[0]):
            raise LockOrderError("generation locks must be requested in lexical generation order")
        if len({generation_id for generation_id, _path in ordered}) != len(ordered):
            raise LockOrderError("generation locks must be unique")
        with ExitStack() as stack:
            for generation_id, path in ordered:
                stack.enter_context(
                    self.existing_generation_lock(
                        path,
                        generation_id=generation_id,
                        exclusive=exclusive,
                        blocking=blocking,
                    )
                )
            yield

    def read_bytes(
        self,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        """Read one contained regular state file without following its final path."""

        self._ensure_root()
        return self._read_regular(
            self.path(relative),
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    def read_existing_bytes(
        self,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        """Read existing state without mkdir, chmod, replacement, or cleanup."""

        return self._read_regular(
            self.path(relative),
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    def read_optional_existing_bytes(
        self,
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes | None:
        """Read optional existing state while still validating its private parent chain."""

        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        require_before_deadline(deadline_ns, "state record read exceeded its deadline")
        path = self.path(relative)
        try:
            descriptor = self._open_existing_file(path, allow_missing_parent=True)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateCorrupt(f"state record cannot be opened safely: {path}: {exc}") from exc
        if descriptor is None:
            return None
        return self._read_regular_descriptor(
            descriptor,
            path,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    def read_current(
        self,
        relative: str | Path,
        *,
        decoder: Callable[[bytes], RecordT],
        allow_missing: bool = False,
        label: str,
    ) -> RecordT | None:
        data = self.read_optional_existing_bytes(relative)
        if data is None:
            if allow_missing:
                return None
            raise StateRecordMissing(f"{label} current record is missing")
        try:
            return decoder(data)
        except Exception as exc:
            if isinstance(exc, StateCorrupt):
                raise
            raise StateCorrupt(f"{label} current record is invalid: {exc}") from exc

    def read_stable_record(
        self,
        *,
        label: str,
        current: str | Path,
        previous: str | Path,
        pending: str | Path,
        decoder: Callable[[bytes], RecordT],
        revision: Callable[[RecordT], int],
        allow_missing: bool = False,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> RecordT | None:
        """Read current authority only when no durable recovery is required."""

        paths = {
            "current": self.path(current),
            "pending": self.path(pending),
            "previous": self.path(previous),
        }
        parents = {path.parent for path in paths.values()}
        if len(parents) != 1:
            raise StatePathError(f"{label} record paths must share one directory")
        parent = next(iter(parents))
        self._require_private_directory_chain(parent)

        def load_candidate(name: str) -> tuple[bytes, RecordT, int] | None:
            path = paths[name]
            try:
                path.lstat()
            except FileNotFoundError:
                return None
            try:
                data = self._read_regular(
                    path,
                    max_bytes=max_bytes,
                    deadline_ns=deadline_ns,
                )
                record = decoder(data)
                return data, record, revision(record)
            except Exception as exc:
                if isinstance(exc, (LockTimeout, StateCorrupt)):
                    raise
                raise StateCorrupt(f"{label} {name} record is invalid: {exc}") from exc

        try:
            paths["pending"].lstat()
        except FileNotFoundError:
            pass
        else:
            raise StateRecoveryRequired(
                f"{label} has an unresolved pending commit"
            )

        current_candidate = load_candidate("current")
        previous_candidate = load_candidate("previous")
        if current_candidate is None:
            if allow_missing and previous_candidate is None:
                return None
            raise StateRecordMissing(f"{label} current record is missing")
        current_bytes, current_record, current_revision = current_candidate
        if previous_candidate is None:
            return current_record
        previous_bytes, _previous_record, previous_revision = previous_candidate
        if previous_revision > current_revision:
            raise StateCorrupt(f"{label} previous record is newer than current")
        if previous_revision == current_revision and previous_bytes != current_bytes:
            raise StateCorrupt(f"{label} has divergent records at revision {current_revision}")
        return current_record

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
        max_bytes: int | None = None,
    ) -> RecordT | None:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        paths = {
            "current": self.path(current),
            "pending": self.path(pending),
            "previous": self.path(previous),
        }
        parents = {path.parent for path in paths.values()}
        if len(parents) != 1:
            raise StatePathError(f"{label} record paths must share one directory")
        parent = next(iter(parents))
        self.cleanup_atomic_temps(parent.relative_to(self.root))
        candidates: dict[str, tuple[bytes, RecordT, int]] = {}
        invalid: dict[str, Exception] = {}
        for name, path in paths.items():
            try:
                data = self.read_optional_existing_bytes(
                    path.relative_to(self.root),
                    max_bytes=max_bytes,
                )
            except Exception as exc:
                invalid[name] = exc
                continue
            if data is None:
                continue
            try:
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
        parents = {current_path.parent, previous_path.parent, pending_path.parent}
        if len(parents) != 1:
            raise StatePathError(f"{label} record paths must share one directory")
        parent = next(iter(parents))
        self.cleanup_atomic_temps(parent.relative_to(self.root))
        commit_may_recover = False

        def pending_replaced() -> None:
            nonlocal commit_may_recover
            commit_may_recover = True

        def current_replaced() -> None:
            self.fault_hook(f"{label}:current_replaced")

        try:
            self._atomic_replace(pending_path, payload, after_replace=pending_replaced)
            self.fault_hook(f"{label}:pending_durable")

            current_bytes = self.read_optional_existing_bytes(current)
            if current_bytes is not None:
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

    def _read_regular(
        self,
        path: Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        try:
            descriptor = self._open_existing_file(path)
        except OSError as exc:
            raise StateCorrupt(f"state record cannot be opened safely: {path}: {exc}") from exc
        if descriptor is None:  # pragma: no cover - allow_missing_parent is false
            raise StateCorrupt(f"state record parent is missing: {path.parent}")
        return self._read_regular_descriptor(
            descriptor,
            path,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    def _read_regular_descriptor(
        self,
        descriptor: int,
        path: Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes:
        try:
            require_before_deadline(deadline_ns, "state record read exceeded its deadline")
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise StateCorrupt(f"state record is not a regular file: {path}")
            self._require_owner(details, path)
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise StateCorrupt(f"state record mode is not 0600: {path}")
            if max_bytes is not None and details.st_size > max_bytes:
                raise StateCorrupt(
                    f"state record exceeds its read limit of {max_bytes} bytes: {path}"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                require_before_deadline(deadline_ns, "state record read exceeded its deadline")
                read_size = 1024 * 1024
                if max_bytes is not None:
                    read_size = min(read_size, (max_bytes - total) + 1)
                try:
                    chunk = os.read(descriptor, read_size)
                except InterruptedError:
                    continue
                require_before_deadline(deadline_ns, "state record read exceeded its deadline")
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise StateCorrupt(
                        f"state record exceeds its read limit of {max_bytes} bytes: {path}"
                    )
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
        self.cleanup_atomic_temps(
            destination.parent.relative_to(self.root),
            destination_name=destination.name,
        )
        parent_relative = destination.parent.relative_to(self.root)
        with self.existing_private_directory(parent_relative) as parent_descriptor:
            try:
                details = os.stat(
                    destination.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                self._require_regular_details(
                    details,
                    destination,
                    allowed_modes=_PRIVATE_FILE_MODES,
                )
            temporary_name = f".{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            replaced = False
            try:
                try:
                    os.fchmod(descriptor, 0o600)
                    self._write_all(descriptor, data)
                    self.syscalls.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self.syscalls.replace_at(
                    temporary_name,
                    destination.name,
                    source_dir_fd=parent_descriptor,
                    destination_dir_fd=parent_descriptor,
                )
                replaced = True
                if after_replace is not None:
                    after_replace()
                self.syscalls.fsync(parent_descriptor)
                try:
                    installed_descriptor = os.open(
                        destination.name,
                        self._regular_open_flags(),
                        dir_fd=parent_descriptor,
                    )
                except OSError as exc:
                    raise StateCorrupt(
                        f"installed state cannot be opened safely: {destination}: {exc}"
                    ) from exc
                if self._read_regular_descriptor(installed_descriptor, destination) != data:
                    raise StateCorrupt(f"installed state verification failed at {destination}")
            finally:
                if not replaced:
                    try:
                        self.syscalls.unlink_at(temporary_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass

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

    def _unlink_and_sync(self, path: Path, *, label: str | None = None) -> None:
        parent_relative = path.parent.relative_to(self.root)
        with self._existing_private_directory(
            parent_relative,
            allow_missing=True,
        ) as parent_descriptor:
            if parent_descriptor is None:
                return
            try:
                details = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StatePathError(
                    f"state cleanup target cannot be inspected safely: {path}"
                ) from exc
            self._require_regular_details(
                details,
                path,
                allowed_modes=_PRIVATE_FILE_MODES,
            )
            if label is not None:
                self.fault_hook(f"{label}:before_unlink")
            visible = False
            try:
                self.syscalls.unlink_at(path.name, dir_fd=parent_descriptor)
                visible = True
                if label is not None:
                    self.fault_hook(f"{label}:unlinked")
                self.syscalls.fsync(parent_descriptor)
                if label is not None:
                    self.fault_hook(f"{label}:parent_durable")
            except BaseException as exc:
                if label is not None and visible:
                    raise CommitUnknown(
                        f"{label} cleanup became visible before durability acknowledgement"
                    ) from exc
                raise

__all__ = [
    "CommitUnknown",
    "DurableStateRoot",
    "FaultHook",
    "GENERATION_LOCK_RANK",
    "InjectedFault",
    "LockOrderError",
    "PosixSyscalls",
    "REGISTRY_LOCK_RANK",
    "RuntimeCapabilities",
    "StateCorrupt",
    "StatePathError",
    "StateRecoveryRequired",
    "Syscalls",
    "UnsupportedRuntime",
    "WORKSPACE_LOCK_RANK",
    "WorkspaceRuntimeError",
]
