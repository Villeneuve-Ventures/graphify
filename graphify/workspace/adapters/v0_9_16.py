"""Graphify 0.9.16 engine adapter and read-only source observer."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import hashlib
from importlib.metadata import PackageNotFoundError, version as distribution_version
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from threading import RLock
import time
from typing import Callable, Iterator, Mapping, NoReturn

from networkx.exception import NetworkXException
from networkx.readwrite import json_graph

# Engine-private imports are deliberately confined to this versioned adapter.
from graphify.build import build_from_json
from graphify.cache import ephemeral_stat_index
from graphify.detect import detect
from graphify.export import to_json
from graphify.extract import extract
from graphify.security import _max_graph_file_bytes
from graphify.workspace.contracts import CANDIDATE_DISTRIBUTION_VERSION, canonical_json_bytes

from .base import (
    ObservationHook,
    ObservationTimeout,
    ObservationUnavailable,
    ObservationUnstable,
    ObservationUnsupported,
    QueryRejected,
    QueryRequest,
    SourceEntry,
    SourceObservation,
    StructuralBuild,
    UnsupportedCompatibility,
)


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_CONTROL_FILE_MAX_BYTES = 4_096
_PACKED_REFS_MAX_BYTES = 16 * 1024 * 1024
_MAX_SYMBOLIC_REF_DEPTH = 8
_VCS_MARKERS = (".git", ".hg", ".svn", "_darcs", ".fossil")
_STABLE_STAT_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
_ENGINE_CWD_LOCK = RLock()


class _RelativePathMissing(ObservationUnstable):
    """Internal marker for an absent descriptor-relative path component."""


class _AuthorityChanged(ObservationUnstable):
    """A pinned directory identity changed and must not become a new baseline."""


@dataclass(frozen=True)
class _InventoryPass:
    source_commit: str
    inventory_sha256: str
    policy_sha256: str
    entries: tuple[SourceEntry, ...]


@dataclass(frozen=True)
class _PinnedRegularPath:
    relative: Path
    ancestor_identities: tuple[tuple[int, int], ...]
    details: os.stat_result


@dataclass(frozen=True)
class _PinnedAbsentPath:
    relative: Path
    ancestor_identities: tuple[tuple[int, int], ...]
    container_details: os.stat_result


@dataclass(frozen=True)
class _PinnedUnsafePath:
    relative: Path
    details: os.stat_result


_PinnedOptionalPath = _PinnedRegularPath | _PinnedAbsentPath | _PinnedUnsafePath


def _emit(
    hook: ObservationHook | None,
    event: str,
    **details: object,
) -> None:
    if hook is not None:
        hook(event, details)


def _deadline(deadline_ns: int | None) -> None:
    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        raise ObservationTimeout("source observation exceeded its deadline")


def _validate_read_options(
    *,
    collect: bool,
    max_bytes: int | None,
    size_cap: int | None,
    chunk_consumer: Callable[[bytes], object] | None,
) -> None:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if size_cap is not None and size_cap < 0:
        raise ValueError("size_cap must be non-negative")
    if collect and chunk_consumer is not None:
        raise ValueError("collection and chunk consumption are mutually exclusive")


def _stat_changed(reference: os.stat_result, candidate: os.stat_result) -> bool:
    return any(
        getattr(reference, field) != getattr(candidate, field)
        for field in _STABLE_STAT_FIELDS
    )


def _directory_identity_changed(
    reference: os.stat_result,
    candidate: os.stat_result,
) -> bool:
    return (
        reference.st_dev,
        reference.st_ino,
        stat.S_IFMT(reference.st_mode),
    ) != (
        candidate.st_dev,
        candidate.st_ino,
        stat.S_IFMT(candidate.st_mode),
    )


def _source_file_fstat(
    descriptor: int,
    path: Path,
    *,
    phase: str,
) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError as exc:
        raise ObservationUnavailable(
            f"source file cannot be inspected {phase}: {path}: {exc}"
        ) from exc


def _source_directory_fstat(
    descriptor: int,
    path: Path,
    *,
    phase: str,
) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError as exc:
        raise ObservationUnavailable(
            f"source directory cannot be inspected {phase}: {path}: {exc}"
        ) from exc


def _read_descriptor_once(
    descriptor: int,
    path: Path,
    before: os.stat_result,
    *,
    collect: bool,
    max_bytes: int | None,
    size_cap: int | None,
    chunk_consumer: Callable[[bytes], object] | None,
    deadline_ns: int | None = None,
) -> tuple[str, os.stat_result, bytes | None]:
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ObservationUnsupported(f"source entry is not a singular regular file: {path}")
    if size_cap is not None and before.st_size > size_cap:
        raise ObservationUnsupported(
            f"source file exceeds the {size_cap}-byte safe-read limit: {path}"
        )
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if collect else None
    remaining = max_bytes
    total = 0
    while remaining is None or remaining > 0:
        _deadline(deadline_ns)
        try:
            read_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if size_cap is not None:
                read_size = min(read_size, size_cap - total + 1)
            chunk = os.read(descriptor, read_size)
        except InterruptedError:
            continue
        except OSError as exc:
            raise ObservationUnavailable(
                f"source file cannot be read safely: {path}: {exc}"
            ) from exc
        _deadline(deadline_ns)
        if not chunk:
            break
        total += len(chunk)
        if size_cap is not None and total > size_cap:
            raise ObservationUnsupported(
                f"source file exceeds the {size_cap}-byte safe-read limit: {path}"
            )
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
        if chunk_consumer is not None:
            chunk_consumer(chunk)
        _deadline(deadline_ns)
        if remaining is not None:
            remaining -= len(chunk)
    after = _source_file_fstat(descriptor, path, phase="after hashing")
    if _stat_changed(before, after):
        raise ObservationUnstable(f"source file changed while hashing: {path}")
    payload = None if chunks is None else b"".join(chunks)
    return digest.hexdigest(), after, payload


def _read_regular_once(
    path: Path,
    *,
    collect: bool = False,
    max_bytes: int | None = None,
    size_cap: int | None = None,
    chunk_consumer: Callable[[bytes], object] | None = None,
    deadline_ns: int | None = None,
) -> tuple[str, os.stat_result, bytes | None]:
    _validate_read_options(
        collect=collect,
        max_bytes=max_bytes,
        size_cap=size_cap,
        chunk_consumer=chunk_consumer,
    )
    _deadline(deadline_ns)
    try:
        installed_before = path.lstat()
    except FileNotFoundError as exc:
        raise ObservationUnstable(f"source file disappeared before open: {path}") from exc
    except OSError as exc:
        raise ObservationUnavailable(f"source file cannot be inspected safely: {path}: {exc}") from exc
    if not stat.S_ISREG(installed_before.st_mode) or installed_before.st_nlink != 1:
        raise ObservationUnsupported(f"source entry is not a singular regular file: {path}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ObservationUnstable(f"source file disappeared before open: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ObservationUnsupported(
                f"source entry is not a singular regular file: {path}"
            ) from exc
        raise ObservationUnavailable(f"source file cannot be opened safely: {path}: {exc}") from exc
    try:
        before = _source_file_fstat(descriptor, path, phase="before hashing")
        try:
            installed_at_open = path.lstat()
        except FileNotFoundError as exc:
            raise ObservationUnstable(f"source file disappeared before hashing: {path}") from exc
        except OSError as exc:
            raise ObservationUnavailable(
                f"source file cannot be inspected before hashing: {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(installed_at_open.st_mode):
            raise ObservationUnstable(f"source file changed before hashing: {path}")
        if _stat_changed(before, installed_before) or _stat_changed(before, installed_at_open):
            raise ObservationUnstable(f"source file changed before hashing: {path}")
        digest, after, payload = _read_descriptor_once(
            descriptor,
            path,
            before,
            collect=collect,
            max_bytes=max_bytes,
            size_cap=size_cap,
            chunk_consumer=chunk_consumer,
            deadline_ns=deadline_ns,
        )
    finally:
        os.close(descriptor)
    try:
        installed = path.lstat()
    except FileNotFoundError as exc:
        raise ObservationUnstable(f"source file disappeared after hashing: {path}") from exc
    except OSError as exc:
        raise ObservationUnavailable(f"source file cannot be re-inspected safely: {path}: {exc}") from exc
    if not stat.S_ISREG(installed.st_mode) or _stat_changed(after, installed):
        raise ObservationUnstable(f"source file changed after hashing: {path}")
    return digest, after, payload


def _anchored_directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if (
        not no_follow
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise ObservationUnsupported("descriptor-anchored traversal is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | no_follow


def _open_absolute_source_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = _anchored_directory_flags()
    try:
        descriptor = os.open(absolute.anchor, flags)
    except OSError as exc:
        raise ObservationUnavailable(
            f"source anchor cannot be opened safely: {absolute.anchor}: {exc}"
        ) from exc
    for component in absolute.parts[1:]:
        try:
            child = os.open(component, flags, dir_fd=descriptor)
        except FileNotFoundError as exc:
            os.close(descriptor)
            raise ObservationUnstable(
                f"source directory disappeared while opening: {absolute}"
            ) from exc
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ObservationUnsupported(
                    f"source ancestor is not a real directory: {absolute}"
                ) from exc
            raise ObservationUnavailable(
                f"source ancestor cannot be opened safely: {absolute}: {exc}"
            ) from exc
        os.close(descriptor)
        descriptor = child
    return descriptor


def _open_relative_directories(
    root_descriptor: int,
    components: tuple[str, ...],
    path: Path,
    *,
    expected_identities: tuple[tuple[int, int], ...] | None = None,
    captured_details: list[os.stat_result] | None = None,
) -> int:
    flags = _anchored_directory_flags()
    try:
        parent_descriptor = os.dup(root_descriptor)
    except OSError as exc:
        raise ObservationUnavailable(f"source root descriptor cannot be duplicated: {exc}") from exc
    if expected_identities is not None and len(expected_identities) != len(components):
        os.close(parent_descriptor)
        raise _AuthorityChanged(f"source ancestry changed before open: {path}")
    for index, component in enumerate(components):
        try:
            next_descriptor = os.open(component, flags, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            os.close(parent_descriptor)
            if expected_identities is not None:
                raise _AuthorityChanged(
                    f"source ancestor disappeared before open: {path}"
                ) from exc
            raise _RelativePathMissing(
                f"source ancestor disappeared before open: {path}"
            ) from exc
        except OSError as exc:
            os.close(parent_descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ObservationUnsupported(
                    f"source ancestor is not a real directory: {path}"
                ) from exc
            raise ObservationUnavailable(
                f"source ancestor cannot be opened safely: {path}: {exc}"
            ) from exc
        try:
            details = os.fstat(next_descriptor)
        except OSError as exc:
            os.close(next_descriptor)
            os.close(parent_descriptor)
            raise ObservationUnavailable(
                f"source ancestor cannot be inspected safely: {path}: {exc}"
            ) from exc
        if not stat.S_ISDIR(details.st_mode):
            os.close(next_descriptor)
            os.close(parent_descriptor)
            raise ObservationUnsupported(f"source ancestor is not a real directory: {path}")
        if (
            expected_identities is not None
            and (details.st_dev, details.st_ino) != expected_identities[index]
        ):
            os.close(next_descriptor)
            os.close(parent_descriptor)
            raise _AuthorityChanged(f"source ancestor changed before open: {path}")
        if captured_details is not None:
            captured_details.append(details)
        os.close(parent_descriptor)
        parent_descriptor = next_descriptor
    return parent_descriptor


def _open_relative_parent(
    root_descriptor: int,
    parts: tuple[str, ...],
    path: Path,
    *,
    expected_identities: tuple[tuple[int, int], ...] | None = None,
    captured_details: list[os.stat_result] | None = None,
) -> int:
    return _open_relative_directories(
        root_descriptor,
        parts[:-1],
        path,
        expected_identities=expected_identities,
        captured_details=captured_details,
    )


def _relative_lstat(
    parent_descriptor: int,
    name: str,
    path: Path,
    *,
    phase: str,
) -> os.stat_result:
    try:
        return os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise ObservationUnstable(f"source file disappeared {phase}: {path}") from exc
    except OSError as exc:
        raise ObservationUnavailable(
            f"source file cannot be inspected safely {phase}: {path}: {exc}"
        ) from exc


def _read_relative_regular_once(
    root_descriptor: int,
    relative: Path,
    path: Path,
    *,
    collect: bool = False,
    max_bytes: int | None = None,
    size_cap: int | None = None,
    chunk_consumer: Callable[[bytes], object] | None = None,
    expected_path: _PinnedRegularPath | None = None,
    deadline_ns: int | None = None,
) -> tuple[str, os.stat_result, bytes | None]:
    _validate_read_options(
        collect=collect,
        max_bytes=max_bytes,
        size_cap=size_cap,
        chunk_consumer=chunk_consumer,
    )
    _deadline(deadline_ns)
    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ObservationUnsupported(f"detected source has an unsafe relative path: {path}")
    if expected_path is not None and expected_path.relative != relative:
        raise ObservationUnstable(f"source path changed before open: {path}")
    expected_identities = (
        None if expected_path is None else expected_path.ancestor_identities
    )
    parent_descriptor = _open_relative_parent(
        root_descriptor,
        parts,
        path,
        expected_identities=expected_identities,
    )
    name = parts[-1]
    try:
        installed_before = _relative_lstat(
            parent_descriptor,
            name,
            path,
            phase="before open",
        )
        if not stat.S_ISREG(installed_before.st_mode) or installed_before.st_nlink != 1:
            raise ObservationUnsupported(f"source entry is not a singular regular file: {path}")
        if expected_path is not None and _stat_changed(
            expected_path.details,
            installed_before,
        ):
            raise ObservationUnstable(f"source file changed before open: {path}")
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(name, file_flags, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            raise ObservationUnstable(f"source file disappeared before open: {path}") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ObservationUnsupported(
                    f"source entry is not a singular regular file: {path}"
                ) from exc
            raise ObservationUnavailable(
                f"source file cannot be opened safely: {path}: {exc}"
            ) from exc
        try:
            before = _source_file_fstat(descriptor, path, phase="before hashing")
            installed_at_open = _relative_lstat(
                parent_descriptor,
                name,
                path,
                phase="before hashing",
            )
            if (
                not stat.S_ISREG(installed_at_open.st_mode)
                or _stat_changed(before, installed_before)
                or _stat_changed(before, installed_at_open)
            ):
                raise ObservationUnstable(f"source file changed before hashing: {path}")
            digest, after, payload = _read_descriptor_once(
                descriptor,
                path,
                before,
                collect=collect,
                max_bytes=max_bytes,
                size_cap=size_cap,
                chunk_consumer=chunk_consumer,
                deadline_ns=deadline_ns,
            )
        finally:
            os.close(descriptor)
        installed_pinned = _relative_lstat(
            parent_descriptor,
            name,
            path,
            phase="after hashing",
        )
        if not stat.S_ISREG(installed_pinned.st_mode) or _stat_changed(after, installed_pinned):
            raise ObservationUnstable(f"source file changed after hashing: {path}")
    finally:
        os.close(parent_descriptor)

    installed_parent = _open_relative_parent(
        root_descriptor,
        parts,
        path,
        expected_identities=expected_identities,
    )
    try:
        installed = _relative_lstat(
            installed_parent,
            name,
            path,
            phase="after rooted revalidation",
        )
    finally:
        os.close(installed_parent)
    if not stat.S_ISREG(installed.st_mode) or _stat_changed(after, installed):
        raise ObservationUnstable(f"source path changed after hashing: {path}")
    return digest, after, payload


@dataclass
class _PinnedSourceReader:
    anchor: Path
    descriptor: int
    opened: os.stat_result
    deadline_ns: int | None = None

    @classmethod
    def open(
        cls,
        anchor: Path,
        *,
        deadline_ns: int | None = None,
    ) -> _PinnedSourceReader:
        _deadline(deadline_ns)
        descriptor = _open_absolute_source_directory(anchor)
        try:
            opened = _source_directory_fstat(
                descriptor,
                anchor,
                phase="while opening",
            )
            rebound_descriptor = _open_absolute_source_directory(anchor)
            try:
                rebound = _source_directory_fstat(
                    rebound_descriptor,
                    anchor,
                    phase="while opening",
                )
            finally:
                os.close(rebound_descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _stat_changed(opened, rebound)
            ):
                raise ObservationUnstable(
                    f"source root changed while opening: {anchor}"
                )
            _deadline(deadline_ns)
            return cls(
                anchor=anchor,
                descriptor=descriptor,
                opened=opened,
                deadline_ns=deadline_ns,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        os.close(self.descriptor)

    def _relative(self, path: Path) -> Path:
        try:
            relative = path.relative_to(self.anchor)
        except ValueError as exc:
            raise ObservationUnsupported(
                f"comparison input escapes the pinned source root: {path}"
            ) from exc
        if not relative.parts:
            raise ObservationUnsupported(
                f"comparison input is not a regular file: {path}"
            )
        return relative

    def observe(
        self,
        path: Path,
        *,
        collect: bool = False,
        max_bytes: int | None = None,
        size_cap: int | None = None,
        chunk_consumer: Callable[[bytes], object] | None = None,
        expected_path: _PinnedRegularPath | None = None,
    ) -> tuple[str, os.stat_result, bytes | None]:
        return _read_relative_regular_once(
            self.descriptor,
            self._relative(path),
            path,
            collect=collect,
            max_bytes=max_bytes,
            size_cap=size_cap,
            chunk_consumer=chunk_consumer,
            expected_path=expected_path,
            deadline_ns=self.deadline_ns,
        )

    def pin_regular(self, path: Path) -> _PinnedRegularPath:
        relative = self._relative(path)
        captured: list[os.stat_result] = []
        parent_descriptor = _open_relative_parent(
            self.descriptor,
            relative.parts,
            path,
            captured_details=captured,
        )
        try:
            details = _relative_lstat(
                parent_descriptor,
                relative.parts[-1],
                path,
                phase="while pinning",
            )
        finally:
            os.close(parent_descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ObservationUnsupported(
                f"source entry is not a singular regular file: {path}"
            )
        return _PinnedRegularPath(
            relative=relative,
            ancestor_identities=tuple(
                (item.st_dev, item.st_ino) for item in captured
            ),
            details=details,
        )

    def pin_optional_regular(self, path: Path) -> _PinnedOptionalPath:
        relative = self._relative(path)
        captured: list[os.stat_result] = []
        try:
            parent_descriptor = _open_relative_parent(
                self.descriptor,
                relative.parts,
                path,
                captured_details=captured,
            )
        except _RelativePathMissing:
            container_details = (
                captured[-1]
                if captured
                else _source_directory_fstat(
                    self.descriptor,
                    self.anchor,
                    phase="while pinning an absent path",
                )
            )
            return _PinnedAbsentPath(
                relative=relative,
                ancestor_identities=tuple(
                    (item.st_dev, item.st_ino) for item in captured
                ),
                container_details=container_details,
            )
        try:
            container_before = _source_directory_fstat(
                parent_descriptor,
                path.parent,
                phase="before pinning an optional path",
            )
            try:
                details = os.stat(
                    relative.parts[-1],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                details = None
            except OSError as exc:
                raise ObservationUnavailable(
                    f"source entry cannot be inspected while pinning: {path}: {exc}"
                ) from exc
            container_after = _source_directory_fstat(
                parent_descriptor,
                path.parent,
                phase="after pinning an optional path",
            )
            if _stat_changed(container_before, container_after):
                raise ObservationUnstable(
                    f"source directory changed while pinning optional path: {path}"
                )
        finally:
            os.close(parent_descriptor)
        ancestor_identities = tuple(
            (item.st_dev, item.st_ino) for item in captured
        )
        if details is None:
            return _PinnedAbsentPath(
                relative=relative,
                ancestor_identities=ancestor_identities,
                container_details=container_after,
            )
        if stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
            return _PinnedRegularPath(
                relative=relative,
                ancestor_identities=ancestor_identities,
                details=details,
            )
        return _PinnedUnsafePath(relative=relative, details=details)

    def require_absent(self, path: Path, pinned: _PinnedAbsentPath) -> None:
        relative = self._relative(path)
        if relative != pinned.relative:
            raise ObservationUnstable(f"comparison input changed after absent pin: {path}")
        existing_count = len(pinned.ancestor_identities)
        if existing_count >= len(relative.parts):
            raise ObservationUnstable(f"comparison absence pin is invalid: {path}")
        container_descriptor = _open_relative_directories(
            self.descriptor,
            relative.parts[:existing_count],
            path,
            expected_identities=pinned.ancestor_identities,
        )
        try:
            container_before = _source_directory_fstat(
                container_descriptor,
                path,
                phase="before revalidating an absent path",
            )
            if _stat_changed(pinned.container_details, container_before):
                raise ObservationUnstable(
                    f"comparison input changed after absent pin: {path}"
                )
            try:
                os.stat(
                    relative.parts[existing_count],
                    dir_fd=container_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ObservationUnavailable(
                    f"comparison absence cannot be revalidated safely: {path}: {exc}"
                ) from exc
            else:
                raise ObservationUnstable(
                    f"comparison input appeared after absent pin: {path}"
                )
            container_after = _source_directory_fstat(
                container_descriptor,
                path,
                phase="after revalidating an absent path",
            )
            if _stat_changed(container_before, container_after):
                raise ObservationUnstable(
                    f"comparison input changed while revalidating absence: {path}"
                )
        finally:
            os.close(container_descriptor)

    def entry_details(
        self,
        path: Path,
        *,
        allow_missing: bool = False,
    ) -> os.stat_result | None:
        relative = self._relative(path)
        try:
            parent_descriptor = _open_relative_parent(
                self.descriptor,
                relative.parts,
                path,
            )
        except _RelativePathMissing:
            if allow_missing:
                return None
            raise
        try:
            try:
                return os.stat(
                    relative.parts[-1],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if allow_missing:
                    return None
                raise ObservationUnstable(
                    f"source entry disappeared while inspecting: {path}"
                ) from None
            except OSError as exc:
                raise ObservationUnavailable(
                    f"source entry cannot be inspected safely: {path}: {exc}"
                ) from exc
        finally:
            os.close(parent_descriptor)

    def directory_details(self, path: Path) -> os.stat_result:
        try:
            relative = path.relative_to(self.anchor)
        except ValueError as exc:
            raise ObservationUnsupported(
                f"source directory escapes the pinned root: {path}"
            ) from exc
        descriptor = _open_relative_directories(
            self.descriptor,
            relative.parts,
            path,
        )
        try:
            details = _source_directory_fstat(
                descriptor,
                path,
                phase="during rooted inspection",
            )
        finally:
            os.close(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise ObservationUnsupported(
                f"source path is not a real directory: {path}"
            )
        return details

    def list_directory(
        self,
        path: Path,
        *,
        expected: os.stat_result | None,
        expected_identities: tuple[tuple[int, int], ...] | None,
    ) -> tuple[os.stat_result, list[tuple[str, os.stat_result]]]:
        try:
            relative = path.relative_to(self.anchor)
        except ValueError as exc:
            raise ObservationUnsupported(
                f"source directory escapes the pinned root: {path}"
            ) from exc
        descriptor = _open_relative_directories(
            self.descriptor,
            relative.parts,
            path,
            expected_identities=expected_identities,
        )
        try:
            before = _source_directory_fstat(
                descriptor,
                path,
                phase="before descriptor-bound enumeration",
            )
            if expected is not None:
                if _directory_identity_changed(expected, before):
                    raise _AuthorityChanged(
                        f"source directory identity changed before enumeration: {path}"
                    )
                if _stat_changed(expected, before):
                    raise _AuthorityChanged(
                        f"source directory changed before enumeration: {path}"
                    )
            discovered: list[tuple[str, os.stat_result]] = []
            try:
                _deadline(self.deadline_ns)
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        _deadline(self.deadline_ns)
                        try:
                            details = entry.stat(follow_symlinks=False)
                        except FileNotFoundError as exc:
                            raise ObservationUnstable(
                                f"source entry disappeared during enumeration: {path / entry.name}"
                            ) from exc
                        except OSError as exc:
                            raise ObservationUnavailable(
                                f"source entry cannot be inspected safely during enumeration: "
                                f"{path / entry.name}: {exc}"
                            ) from exc
                        _deadline(self.deadline_ns)
                        discovered.append((entry.name, details))
                _deadline(self.deadline_ns)
            except OSError as exc:
                raise ObservationUnavailable(
                    f"source directory cannot be enumerated safely: {path}: {exc}"
                ) from exc
            after = _source_directory_fstat(
                descriptor,
                path,
                phase="after descriptor-bound enumeration",
            )
            if _directory_identity_changed(before, after):
                raise _AuthorityChanged(
                    f"source directory identity changed during enumeration: {path}"
                )
            if _stat_changed(before, after):
                raise _AuthorityChanged(
                    f"source directory changed during enumeration: {path}"
                )
            return after, discovered
        finally:
            os.close(descriptor)

    def require_directory_binding(
        self,
        path: Path,
        expected: os.stat_result,
    ) -> None:
        current = self.directory_details(path)
        if _directory_identity_changed(expected, current):
            raise _AuthorityChanged(
                f"source directory identity changed during observation: {path}"
            )
        if _stat_changed(expected, current):
            raise _AuthorityChanged(
                f"source directory changed during observation: {path}"
            )

    def require_anchor_binding(self) -> None:
        descriptor = _open_absolute_source_directory(self.anchor)
        try:
            current = _source_directory_fstat(
                descriptor,
                self.anchor,
                phase="during binding revalidation",
            )
        finally:
            os.close(descriptor)
        if _stat_changed(self.opened, current):
            raise ObservationUnstable(
                f"source root changed during observation: {self.anchor}"
            )


def _control_path(payload: bytes, *, prefix: str, base: Path, label: str) -> Path:
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ObservationUnsupported(f"{label} is not valid UTF-8") from exc
    if prefix:
        if not value.startswith(prefix):
            raise ObservationUnsupported(f"{label} has an unsupported format")
        value = value[len(prefix) :].strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ObservationUnsupported(f"{label} has an unsupported path")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


@dataclass
class _PinnedReadAuthority:
    source: _PinnedSourceReader
    git_dir: _PinnedSourceReader | None
    common_dir: _PinnedSourceReader | None
    git_file: Path | None
    commondir_file: Path | None
    comparison_paths: dict[Path, _PinnedOptionalPath] = field(default_factory=dict)
    comparison_readers: dict[Path, _PinnedSourceReader] = field(default_factory=dict)
    comparison_directories: dict[Path, os.stat_result] = field(default_factory=dict)

    @staticmethod
    def _comparison_key(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    @classmethod
    def open(
        cls,
        source_anchor: Path,
        *,
        deadline_ns: int | None = None,
    ) -> _PinnedReadAuthority:
        source = _PinnedSourceReader.open(source_anchor, deadline_ns=deadline_ns)
        git_dir: _PinnedSourceReader | None = None
        common_dir: _PinnedSourceReader | None = None
        git_file: Path | None = None
        commondir_file: Path | None = None
        comparison_paths: dict[Path, _PinnedOptionalPath] = {}
        comparison_readers: dict[Path, _PinnedSourceReader] = {}
        try:
            dot_git = source_anchor / ".git"
            dot_git_details = source.entry_details(dot_git, allow_missing=True)
            if dot_git_details is None:
                return cls(source, None, None, None, None)
            if stat.S_ISDIR(dot_git_details.st_mode):
                git_dir = _PinnedSourceReader.open(dot_git, deadline_ns=deadline_ns)
                if hasattr(os, "geteuid") and git_dir.opened.st_uid != os.geteuid():
                    raise ObservationUnsupported(
                        "Git metadata directory is not owned by the current user"
                    )
                return cls(
                    source,
                    git_dir,
                    git_dir,
                    None,
                    None,
                )
            if not stat.S_ISREG(dot_git_details.st_mode) or dot_git_details.st_nlink != 1:
                raise ObservationUnsupported(
                    "Git metadata entry must be a real directory or singular regular file"
                )
            pinned_git_file = source.pin_regular(dot_git)
            comparison_paths[cls._comparison_key(dot_git)] = pinned_git_file
            _digest, _details, payload = source.observe(
                dot_git,
                collect=True,
                size_cap=_GIT_CONTROL_FILE_MAX_BYTES,
                expected_path=pinned_git_file,
            )
            assert payload is not None
            git_dir_path = _control_path(
                payload,
                prefix="gitdir:",
                base=source_anchor,
                label="Git metadata pointer",
            )
            git_dir = _PinnedSourceReader.open(
                git_dir_path,
                deadline_ns=deadline_ns,
            )
            if hasattr(os, "geteuid") and git_dir.opened.st_uid != os.geteuid():
                raise ObservationUnsupported(
                    "Git worktree metadata directory is not owned by the current user"
                )
            git_file = dot_git
            candidate = git_dir_path / "commondir"
            candidate_key = cls._comparison_key(candidate)
            pinned_commondir = git_dir.pin_optional_regular(candidate)
            comparison_paths[candidate_key] = pinned_commondir
            comparison_readers[candidate_key] = git_dir
            if isinstance(pinned_commondir, _PinnedAbsentPath):
                common_dir = git_dir
            elif isinstance(pinned_commondir, _PinnedUnsafePath):
                raise ObservationUnsupported(
                    "Git common-directory pointer must be a singular regular file"
                )
            else:
                _digest, _details, commondir_payload = git_dir.observe(
                    candidate,
                    collect=True,
                    size_cap=_GIT_CONTROL_FILE_MAX_BYTES,
                    expected_path=pinned_commondir,
                )
                assert commondir_payload is not None
                common_dir_path = _control_path(
                    commondir_payload,
                    prefix="",
                    base=git_dir_path,
                    label="Git common-directory pointer",
                )
                common_dir = (
                    git_dir
                    if common_dir_path == git_dir_path
                    else _PinnedSourceReader.open(
                        common_dir_path,
                        deadline_ns=deadline_ns,
                    )
                )
                if (
                    hasattr(os, "geteuid")
                    and common_dir.opened.st_uid != os.geteuid()
                ):
                    raise ObservationUnsupported(
                        "Git common metadata directory is not owned by the current user"
                    )
                commondir_file = candidate
            return cls(
                source,
                git_dir,
                common_dir,
                git_file,
                commondir_file,
                comparison_paths,
                comparison_readers,
            )
        except BaseException:
            if common_dir is not None and common_dir is not git_dir:
                common_dir.close()
            if git_dir is not None:
                git_dir.close()
            source.close()
            raise

    @property
    def routing_policy_paths(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in (self.git_file, self.commondir_file)
            if path is not None
        )

    def _reader_for(self, path: Path) -> _PinnedSourceReader:
        if (
            self.git_dir is not None
            and self.commondir_file is not None
            and path == self.commondir_file
        ):
            return self.git_dir
        if self.common_dir is not None and path == self.common_dir.anchor / "info" / "exclude":
            return self.common_dir
        try:
            path.relative_to(self.source.anchor)
        except ValueError:
            pass
        else:
            return self.source
        raise ObservationUnsupported(
            f"comparison input escapes the pinned read authority: {path}"
        )

    def _git_payload(
        self,
        reader: _PinnedSourceReader,
        path: Path,
        *,
        size_cap: int,
        allow_missing: bool = False,
    ) -> bytes | None:
        key = self._comparison_key(path)
        pinned = self._pin_optional_with_reader(reader, key)
        if isinstance(pinned, _PinnedAbsentPath):
            if allow_missing:
                return None
            raise ObservationUnstable(f"Git metadata input is unavailable: {path}")
        if isinstance(pinned, _PinnedUnsafePath):
            raise ObservationUnsupported(
                f"Git metadata input is not a singular regular file: {path}"
            )
        _digest, _details, payload = reader.observe(
            key,
            collect=True,
            size_cap=size_cap,
            expected_path=pinned,
        )
        assert payload is not None
        return payload

    def _git_ref_readers(self) -> tuple[_PinnedSourceReader, ...]:
        if self.git_dir is None:
            return ()
        if self.common_dir is None or self.common_dir is self.git_dir:
            return (self.git_dir,)
        return (self.git_dir, self.common_dir)

    def _resolve_git_ref(
        self,
        ref_name: str,
        *,
        depth: int,
        seen: frozenset[str],
    ) -> str:
        if depth > _MAX_SYMBOLIC_REF_DEPTH or ref_name in seen:
            raise ObservationUnsupported("Git HEAD contains a symbolic-reference cycle")
        _validate_git_ref_name(ref_name)
        loose_values: list[bytes] = []
        for reader in self._git_ref_readers():
            payload = self._git_payload(
                reader,
                reader.anchor / Path(ref_name),
                size_cap=_GIT_CONTROL_FILE_MAX_BYTES,
                allow_missing=True,
            )
            if payload is not None and payload not in loose_values:
                loose_values.append(payload)
        if len(loose_values) > 1:
            raise ObservationUnsupported(
                f"Git reference has conflicting loose values: {ref_name}"
            )
        if loose_values:
            return self._resolve_git_head_payload(
                loose_values[0],
                label=f"Git reference {ref_name}",
                depth=depth,
                seen=seen | {ref_name},
            )
        if self.common_dir is None:
            raise ObservationUnavailable("Git common metadata directory is unavailable")
        packed_payload = self._git_payload(
            self.common_dir,
            self.common_dir.anchor / "packed-refs",
            size_cap=_PACKED_REFS_MAX_BYTES,
            allow_missing=True,
        )
        if packed_payload is None:
            raise ObservationUnavailable(f"Git HEAD reference is unavailable: {ref_name}")
        return _packed_ref_commit(packed_payload, ref_name)

    def _resolve_git_head_payload(
        self,
        payload: bytes,
        *,
        label: str,
        depth: int,
        seen: frozenset[str],
    ) -> str:
        try:
            value = payload.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ObservationUnsupported(f"{label} is not valid ASCII") from exc
        if _COMMIT_RE.fullmatch(value) is not None:
            return value
        if value.startswith("ref:"):
            ref_name = value.removeprefix("ref:").strip()
            return self._resolve_git_ref(
                ref_name,
                depth=depth + 1,
                seen=seen,
            )
        raise ObservationUnsupported(f"{label} has an unsupported value")

    def git_head(self) -> str:
        if self.git_dir is None:
            raise ObservationUnavailable("Git metadata directory is unavailable")
        payload = self._git_payload(
            self.git_dir,
            self.git_dir.anchor / "HEAD",
            size_cap=_GIT_CONTROL_FILE_MAX_BYTES,
        )
        assert payload is not None
        return self._resolve_git_head_payload(
            payload,
            label="Git HEAD",
            depth=0,
            seen=frozenset(),
        )

    def read(self, path: Path, max_bytes: int | None) -> bytes:
        reader = self._reader_for(path)
        expected_path = self.require_comparison_path(path)
        _digest, _details, payload = reader.observe(
            path,
            collect=True,
            max_bytes=max_bytes,
            expected_path=expected_path,
        )
        assert payload is not None
        return payload

    def _require_comparison_directory_bindings(self, path: Path) -> None:
        key = self._comparison_key(path)
        for directory, expected in sorted(
            self.comparison_directories.items(),
            key=lambda item: len(item[0].parts),
        ):
            if directory == key or directory in key.parents:
                self.source.require_directory_binding(directory, expected)

    def bind_comparison_directory(
        self,
        path: Path,
        details: os.stat_result,
    ) -> None:
        key = self._comparison_key(path)
        expected = self.comparison_directories.get(key)
        if expected is not None:
            if _directory_identity_changed(expected, details):
                raise _AuthorityChanged(
                    f"source directory identity changed during enumeration: {path}"
                )
            if _stat_changed(expected, details):
                raise _AuthorityChanged(
                    f"source directory changed during enumeration: {path}"
                )
            return
        self.comparison_directories[key] = details

    def _comparison_directory_identities(
        self,
        path: Path,
    ) -> tuple[tuple[int, int], ...] | None:
        try:
            relative = path.relative_to(self.source.anchor)
        except ValueError as exc:
            raise ObservationUnsupported(
                f"comparison directory escapes the pinned source root: {path}"
            ) from exc
        identities: list[tuple[int, int]] = []
        for index in range(1, len(relative.parts) + 1):
            ancestor = self.source.anchor.joinpath(*relative.parts[:index])
            expected = self.comparison_directories.get(ancestor)
            if expected is None:
                return None
            identities.append((expected.st_dev, expected.st_ino))
        return tuple(identities)

    def list_comparison_directory(
        self,
        path: Path,
    ) -> tuple[list[str], list[str]]:
        key = self._comparison_key(path)
        expected = self.comparison_directories.get(key)
        current, entries = self.source.list_directory(
            key,
            expected=expected,
            expected_identities=self._comparison_directory_identities(key),
        )
        self.source.require_directory_binding(key, current)
        self.bind_comparison_directory(key, current)
        dirnames: list[str] = []
        filenames: list[str] = []
        for name, details in entries:
            if stat.S_ISDIR(details.st_mode):
                self.bind_comparison_directory(key / name, details)
                dirnames.append(name)
            else:
                filenames.append(name)
        return dirnames, filenames

    def _pin_optional_with_reader(
        self,
        reader: _PinnedSourceReader,
        key: Path,
    ) -> _PinnedOptionalPath:
        pinned = self.comparison_paths.get(key)
        if pinned is not None:
            recorded_reader = self.comparison_readers.get(key)
            if recorded_reader is not None and recorded_reader is not reader:
                raise ObservationUnsupported(
                    f"comparison input crossed pinned read authorities: {key}"
                )
            self.comparison_readers[key] = reader
            if isinstance(pinned, _PinnedAbsentPath):
                reader.require_absent(key, pinned)
            return pinned
        self._require_comparison_directory_bindings(key.parent)
        pinned = reader.pin_optional_regular(key)
        self._require_comparison_directory_bindings(key.parent)
        self.comparison_paths[key] = pinned
        self.comparison_readers[key] = reader
        return pinned

    def pin_comparison(self, path: Path) -> bool:
        key = self._comparison_key(path)
        reader = self._reader_for(key)
        pinned = self._pin_optional_with_reader(reader, key)
        if isinstance(pinned, _PinnedUnsafePath):
            raise ObservationUnsupported(
                f"comparison input is not a singular regular file: {path}"
            )
        return isinstance(pinned, _PinnedRegularPath)

    def pin_optional_comparison(self, path: Path) -> _PinnedOptionalPath:
        key = self._comparison_key(path)
        reader = self._reader_for(key)
        return self._pin_optional_with_reader(reader, key)

    def require_comparison_path(self, path: Path) -> _PinnedRegularPath:
        key = self._comparison_key(path)
        if key not in self.comparison_paths:
            raise ObservationUnstable(
                f"comparison input was not pinned during enumeration: {path}"
            )
        pinned = self.comparison_paths[key]
        reader = self.comparison_readers.get(key) or self._reader_for(key)
        if isinstance(pinned, _PinnedAbsentPath):
            reader.require_absent(key, pinned)
            raise ObservationUnstable(
                f"comparison input disappeared after enumeration: {path}"
            )
        if isinstance(pinned, _PinnedUnsafePath):
            raise ObservationUnsupported(
                f"comparison input is not a singular regular file: {path}"
            )
        return pinned

    def require_optional_comparison_path(
        self,
        path: Path,
        pinned: _PinnedOptionalPath,
    ) -> _PinnedRegularPath | None:
        if isinstance(pinned, _PinnedRegularPath):
            return self.require_comparison_path(path)
        if isinstance(pinned, _PinnedUnsafePath):
            raise ObservationUnsupported(
                f"comparison input is not a singular regular file: {path}"
            )
        key = self._comparison_key(path)
        reader = self.comparison_readers.get(key) or self._reader_for(key)
        reader.require_absent(key, pinned)
        return None

    def observe(
        self,
        path: Path,
        *,
        expected_path: _PinnedRegularPath | None = None,
    ) -> tuple[str, os.stat_result, bytes | None]:
        return self._reader_for(path).observe(path, expected_path=expected_path)

    def require_bindings(self) -> None:
        self.source.require_anchor_binding()
        if self.git_dir is not None:
            self.git_dir.require_anchor_binding()
        if self.common_dir is not None and self.common_dir is not self.git_dir:
            self.common_dir.require_anchor_binding()
        for path, pinned in sorted(
            self.comparison_paths.items(),
            key=lambda item: os.fspath(item[0]),
        ):
            if isinstance(pinned, _PinnedAbsentPath):
                reader = self.comparison_readers.get(path) or self._reader_for(path)
                reader.require_absent(path, pinned)

    def policy_label(self, path: Path, source_root: Path) -> str:
        if path.name == "exclude" and path.parent.name == "info":
            return "@git/info/exclude"
        if self.commondir_file is not None and path == self.commondir_file:
            return "@git/worktree/commondir"
        try:
            return path.relative_to(source_root).as_posix()
        except ValueError:
            try:
                return f"@vcs/{path.relative_to(self.source.anchor).as_posix()}"
            except ValueError as exc:
                raise ObservationUnsupported(
                    f"detector returned an external policy path: {path}"
                ) from exc

    def close(self) -> None:
        if self.common_dir is not None and self.common_dir is not self.git_dir:
            self.common_dir.close()
        if self.git_dir is not None:
            self.git_dir.close()
        self.source.close()


def _entry(
    reader: _PinnedSourceReader,
    path: Path,
    root: Path,
    file_type: str,
    *,
    expected_path: _PinnedRegularPath | None = None,
) -> SourceEntry:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ObservationUnsupported(f"detected source escapes the active root: {path}") from exc
    digest, details, _payload = reader.observe(path, expected_path=expected_path)
    return SourceEntry(
        path=relative,
        file_type=file_type,
        size=details.st_size,
        sha256=digest,
        mode=f"{stat.S_IMODE(details.st_mode):04o}",
    )


def _snapshot_code_files(
    code_files: tuple[str, ...],
    pinned_files: Mapping[Path, _PinnedRegularPath],
    root: Path,
    snapshot_root: Path,
    reader: _PinnedSourceReader,
    source_details: os.stat_result,
) -> tuple[tuple[Path, ...], dict[Path, str]]:
    snapshots: list[Path] = []
    source_digests: dict[Path, str] = {}
    reader.require_directory_binding(root, source_details)
    reader.require_anchor_binding()
    for raw_path in code_files:
        source = Path(raw_path)
        try:
            relative = source.relative_to(root)
            expected_path = pinned_files[source]
        except (KeyError, ValueError) as exc:
            raise ObservationUnsupported(
                f"detected source escapes the active root: {source}"
            ) from exc
        target = snapshot_root / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                digest, _details, _payload = reader.observe(
                    source,
                    chunk_consumer=handle.write,
                    expected_path=expected_path,
                )
                source_digests[source] = digest
            target.chmod(0o600)
        except OSError as exc:
            raise ObservationUnavailable(
                f"source snapshot could not be staged safely: {source}: {exc}"
            ) from exc
        snapshots.append(target)
    reader.require_directory_binding(root, source_details)
    reader.require_anchor_binding()
    return tuple(snapshots), source_digests


def _require_structural_input_digests(
    authority: _PinnedReadAuthority,
    pinned_paths: Mapping[Path, _PinnedRegularPath],
    expected_digests: Mapping[Path, str],
    *,
    kind: str,
) -> None:
    for path in sorted(expected_digests, key=os.fspath):
        try:
            digest, _details, _payload = authority.observe(
                path,
                expected_path=pinned_paths[path],
            )
        except ObservationUnstable as exc:
            raise ObservationUnstable(
                f"{kind} input changed during structural build: {path}"
            ) from exc
        if digest != expected_digests[path]:
            raise ObservationUnstable(
                f"{kind} input changed during structural build: {path}"
            )


def _normalize_structural_output(output: Path) -> None:
    details = output.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise ObservationUnsupported("engine output root must be a real directory")
    os.chmod(output, 0o700, follow_symlinks=False)
    payload_root = output / "graphify-out"
    payload_details = payload_root.lstat()
    if not stat.S_ISDIR(payload_details.st_mode):
        raise ObservationUnsupported("engine payload root must be a real directory")
    # The generation contract requires non-writable, traversable payload directories.
    os.chmod(payload_root, 0o755, follow_symlinks=False)  # nosec B103
    for path in sorted(payload_root.rglob("*")):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            os.chmod(path, 0o755, follow_symlinks=False)  # nosec B103
            continue
        if stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
            os.chmod(path, 0o644, follow_symlinks=False)
            continue
        raise ObservationUnsupported(f"engine output contains an unsafe entry: {path}")


def _absolute_output_path(output_root: Path) -> Path:
    output = Path(os.path.abspath(os.fspath(output_root)))
    if output == Path(output.anchor):
        raise ObservationUnsupported("engine output root must not be the filesystem root")
    return output


def _absolute_source_path(source_root: Path) -> Path:
    return Path(os.path.abspath(os.fspath(source_root)))


def _require_descriptor_cwd(descriptor: int) -> None:
    try:
        current = os.stat(".", follow_symlinks=False)
        expected = os.fstat(descriptor)
    except OSError as exc:
        raise ObservationUnavailable(
            f"engine working directory cannot be inspected safely: {exc}"
        ) from exc
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ObservationUnstable("engine working directory changed while staging")


@contextmanager
def _pinned_descriptor_cwd(descriptor: int) -> Iterator[None]:
    if not hasattr(os, "fchdir"):
        raise ObservationUnsupported("descriptor-pinned engine execution is unavailable")
    with _ENGINE_CWD_LOCK:
        try:
            previous = os.open(".", _anchored_directory_flags())
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine working directory cannot be pinned safely: {exc}"
            ) from exc
        try:
            try:
                os.fchdir(descriptor)
                _require_descriptor_cwd(descriptor)
            except OSError as exc:
                raise ObservationUnavailable(
                    f"engine working directory cannot be pinned safely: {exc}"
                ) from exc
            yield
        finally:
            try:
                os.fchdir(previous)
            finally:
                os.close(previous)


def _remove_directory_contents(descriptor: int) -> None:
    try:
        with os.scandir(descriptor) as entries:
            names = tuple(entry.name for entry in entries)
        for name in names:
            details = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(details.st_mode):
                child = os.open(
                    name,
                    _anchored_directory_flags(),
                    dir_fd=descriptor,
                )
                try:
                    _remove_directory_contents(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ObservationUnavailable(
            f"engine temporary directory cannot be removed safely: {exc}"
        ) from exc


def _remove_open_directory_at(
    parent_descriptor: int,
    name: str,
    descriptor: int,
) -> None:
    quarantine_name: str | None = None
    try:
        opened = os.fstat(descriptor)
        try:
            installed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise ObservationUnstable(
                "engine temporary directory disappeared before removal"
            ) from exc
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine temporary directory cannot be inspected safely: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(installed.st_mode)
            or (installed.st_dev, installed.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise ObservationUnstable(
                "engine temporary directory changed before removal"
            )
        quarantine_name = f".graphify-workspace-cleanup-{secrets.token_hex(16)}"
        try:
            os.rename(
                name,
                quarantine_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            quarantined = os.stat(
                quarantine_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine temporary directory cannot be quarantined safely: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(quarantined.st_mode)
            or (quarantined.st_dev, quarantined.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise ObservationUnstable(
                "engine temporary directory changed during quarantine"
            )
        _remove_directory_contents(descriptor)
        try:
            os.rmdir(quarantine_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine temporary directory cannot be removed safely: {exc}"
            ) from exc
    finally:
        os.close(descriptor)


def _create_private_directory_at(parent_descriptor: int, prefix: str) -> str:
    for _attempt in range(128):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine temporary directory cannot be created safely: {exc}"
            ) from exc
        return name
    raise ObservationUnavailable("engine temporary directory name space is exhausted")


@contextmanager
def _temporary_directory_at(
    parent_descriptor: int,
    *,
    prefix: str,
) -> Iterator[tuple[Path, int]]:
    _require_descriptor_cwd(parent_descriptor)
    name = _create_private_directory_at(parent_descriptor, prefix)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                name,
                _anchored_directory_flags(),
                dir_fd=parent_descriptor,
            )
            installed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(installed.st_mode)
                or (installed.st_dev, installed.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise ObservationUnstable(
                    "engine temporary directory changed while opening"
                )
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine temporary directory cannot be used safely: {exc}"
            ) from exc
        yield Path(name), descriptor
    finally:
        if descriptor is not None:
            _remove_open_directory_at(parent_descriptor, name, descriptor)


@contextmanager
def _engine_temporary_directory(
    output_descriptor: int | None,
) -> Iterator[tuple[Path, str, int | None]]:
    if output_descriptor is None:
        with tempfile.TemporaryDirectory(
            prefix="graphify-workspace-build-"
        ) as temporary:
            root = Path(temporary)
            yield root, root.name, None
        return
    with _pinned_descriptor_cwd(output_descriptor):
        with _temporary_directory_at(
            output_descriptor,
            prefix="graphify-workspace-build-",
        ) as (name, descriptor):
            os.fchdir(descriptor)
            _require_descriptor_cwd(descriptor)
            try:
                yield Path("."), name.name, descriptor
            finally:
                os.fchdir(output_descriptor)


@contextmanager
def _source_snapshot_directory(
    engine_output: Path,
    engine_descriptor: int | None,
) -> Iterator[Path]:
    if engine_descriptor is None:
        with tempfile.TemporaryDirectory(
            prefix=".graphify-source-",
            dir=engine_output,
        ) as temporary:
            yield Path(temporary)
        return
    with _temporary_directory_at(
        engine_descriptor,
        prefix=".graphify-source-",
    ) as (name, _descriptor):
        yield name


def _require_initial_source_root(root: Path) -> None:
    try:
        reader = _PinnedSourceReader.open(root)
    except ObservationUnstable as exc:
        raise ObservationUnavailable(f"source root is unavailable: {root}") from exc
    else:
        reader.close()


def _policy_root_for(root: Path) -> Path:
    current = root
    home = _absolute_source_path(Path.home())
    while True:
        for marker in _VCS_MARKERS:
            try:
                (current / marker).lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ObservationUnavailable(
                    f"source policy root cannot be inspected safely: {current}: {exc}"
                ) from exc
            return current
        parent = current.parent
        if parent == current or current == home:
            return root
        current = parent


def _require_policy_root_binding(root: Path, expected: Path) -> None:
    if _policy_root_for(root) != expected:
        raise ObservationUnstable(
            f"source policy root changed during structural build: {root}"
        )


def _open_output_parent(output: Path) -> int:
    flags = _anchored_directory_flags()
    parts = output.parts
    try:
        descriptor = os.open(output.anchor, flags)
    except OSError as exc:
        raise ObservationUnavailable(
            f"engine output anchor cannot be opened safely: {output.anchor}: {exc}"
        ) from exc
    for component in parts[1:-1]:
        try:
            child = os.open(component, flags, dir_fd=descriptor)
        except FileNotFoundError as exc:
            os.close(descriptor)
            raise ObservationUnavailable(
                f"engine output parent must already exist: {output.parent}"
            ) from exc
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ObservationUnsupported(
                    f"engine output ancestor is not a real directory: {output}"
                ) from exc
            raise ObservationUnavailable(
                f"engine output ancestor cannot be opened safely: {output}: {exc}"
            ) from exc
        os.close(descriptor)
        descriptor = child
    return descriptor


def _raise_output_root_error(
    output: Path,
    exc: OSError,
    *,
    missing_detail: str,
) -> NoReturn:
    if exc.errno == errno.ENOENT:
        raise ObservationUnstable(f"{missing_detail}: {output}") from exc
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise ObservationUnsupported(
            f"engine output root is not a real directory: {output}"
        ) from exc
    raise ObservationUnavailable(
        f"engine output root cannot be accessed safely: {output}: {exc}"
    ) from exc


def _open_existing_output_root(output: Path) -> tuple[int, os.stat_result]:
    parent_descriptor = _open_output_parent(output)
    flags = _anchored_directory_flags()
    try:
        try:
            descriptor = os.open(output.name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            raise ObservationUnstable(f"engine output root disappeared: {output}") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ObservationUnsupported(
                    f"engine output root is not a real directory: {output}"
                ) from exc
            raise ObservationUnavailable(
                f"engine output root cannot be opened safely: {output}: {exc}"
            ) from exc
        try:
            details = os.fstat(descriptor)
            installed = os.stat(
                output.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            os.close(descriptor)
            _raise_output_root_error(
                output,
                exc,
                missing_detail="engine output root disappeared while opening",
            )
        except BaseException:
            os.close(descriptor)
            raise
        if (
            not stat.S_ISDIR(details.st_mode)
            or not stat.S_ISDIR(installed.st_mode)
            or (details.st_dev, details.st_ino) != (installed.st_dev, installed.st_ino)
        ):
            os.close(descriptor)
            raise ObservationUnstable(f"engine output root changed while opening: {output}")
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            os.close(descriptor)
            raise ObservationUnsupported(
                f"engine output root is not owned by the current user: {output}"
            )
        return descriptor, details
    finally:
        os.close(parent_descriptor)


def _open_empty_output_root(
    output: Path,
    *,
    require_private: bool = False,
) -> tuple[int, tuple[int, int]]:
    parent_descriptor = _open_output_parent(output)
    try:
        try:
            descriptor = os.open(
                output.name,
                _anchored_directory_flags(),
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError as exc:
            if require_private:
                raise ObservationUnsupported(
                    "engine scratch root must already exist"
                ) from exc
            try:
                os.mkdir(output.name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                _raise_output_root_error(
                    output,
                    exc,
                    missing_detail="engine output root disappeared while creating",
                )
            try:
                descriptor = os.open(
                    output.name,
                    _anchored_directory_flags(),
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                _raise_output_root_error(
                    output,
                    exc,
                    missing_detail="engine output root disappeared after creation",
                )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ObservationUnsupported(
                    f"engine output root is not a real directory: {output}"
                ) from exc
            raise ObservationUnavailable(
                f"engine output root cannot be opened safely: {output}: {exc}"
            ) from exc
        try:
            details = os.fstat(descriptor)
            installed = os.stat(
                output.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(details.st_mode)
                or not stat.S_ISDIR(installed.st_mode)
                or (details.st_dev, details.st_ino)
                != (installed.st_dev, installed.st_ino)
            ):
                raise ObservationUnstable(
                    f"engine output root changed while opening: {output}"
                )
            if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
                raise ObservationUnsupported(
                    f"engine output root is not owned by the current user: {output}"
                )
            if require_private and stat.S_IMODE(details.st_mode) != 0o700:
                raise ObservationUnsupported(
                    "engine scratch root mode must already be 0700"
                )
            with os.scandir(descriptor) as entries:
                if next(entries, None) is not None:
                    raise ObservationUnsupported("engine output root must be empty")
            os.fchmod(descriptor, 0o700)
            opened = os.fstat(descriptor)
            return descriptor, (opened.st_dev, opened.st_ino)
        except OSError as exc:
            os.close(descriptor)
            _raise_output_root_error(
                output,
                exc,
                missing_detail="engine output root disappeared while opening",
            )
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent_descriptor)


def _require_output_binding(output: Path, identity: tuple[int, int]) -> None:
    descriptor, details = _open_existing_output_root(output)
    try:
        if (details.st_dev, details.st_ino) != identity:
            raise ObservationUnstable(f"engine output root changed while staging: {output}")
        if stat.S_IMODE(details.st_mode) != 0o700:
            raise ObservationUnsupported("engine output root mode must remain 0700")
    finally:
        os.close(descriptor)


def _require_output_contents(
    descriptor: int,
    expected: tuple[str, ...],
) -> None:
    scan_descriptor: int | None = None
    try:
        scan_descriptor = os.open(
            ".",
            _anchored_directory_flags(),
            dir_fd=descriptor,
        )
        with os.scandir(scan_descriptor) as entries:
            names = tuple(sorted(entry.name for entry in entries))
    except OSError as exc:
        raise ObservationUnavailable(
            f"engine output root cannot be enumerated safely: {exc}"
        ) from exc
    finally:
        if scan_descriptor is not None:
            os.close(scan_descriptor)
    if names != expected:
        raise ObservationUnstable("engine output root contents changed while staging")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except InterruptedError:
            continue
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine output file write failed safely: {exc}"
            ) from exc
        if written <= 0:
            raise ObservationUnavailable("engine output file could not be written completely")
        offset += written


def _copy_structural_directory(source: Path, destination_descriptor: int) -> None:
    for entry in sorted(source.iterdir(), key=lambda path: path.name):
        details = entry.lstat()
        if stat.S_ISDIR(details.st_mode):
            if stat.S_IMODE(details.st_mode) != 0o755:
                raise ObservationUnsupported(
                    f"engine output directory mode is not normalized: {entry}"
                )
            try:
                os.mkdir(entry.name, 0o700, dir_fd=destination_descriptor)
                child_descriptor = os.open(
                    entry.name,
                    _anchored_directory_flags(),
                    dir_fd=destination_descriptor,
                )
            except OSError as exc:
                raise ObservationUnavailable(
                    f"engine output directory cannot be staged safely: {entry}: {exc}"
                ) from exc
            try:
                _copy_structural_directory(entry, child_descriptor)
                try:
                    os.fchmod(child_descriptor, 0o755)  # nosec B103
                except OSError as exc:
                    raise ObservationUnavailable(
                        f"engine output directory mode could not be finalized: {entry}: {exc}"
                    ) from exc
            finally:
                os.close(child_descriptor)
            continue
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o644
        ):
            raise ObservationUnsupported(f"engine output contains an unsafe entry: {entry}")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_descriptor = os.open(
                entry.name,
                flags,
                0o600,
                dir_fd=destination_descriptor,
            )
        except OSError as exc:
            raise ObservationUnavailable(
                f"engine output file cannot be staged safely: {entry}: {exc}"
            ) from exc
        try:
            _read_regular_once(
                entry,
                chunk_consumer=lambda chunk: _write_all(file_descriptor, chunk),
            )
            try:
                os.fchmod(file_descriptor, 0o644)
            except OSError as exc:
                raise ObservationUnavailable(
                    f"engine output file mode could not be finalized: {entry}: {exc}"
                ) from exc
        finally:
            os.close(file_descriptor)


def _publish_structural_output(source: Path, destination_descriptor: int) -> None:
    names = tuple(sorted(path.name for path in source.iterdir()))
    if names != ("graphify-out",):
        raise ObservationUnsupported("engine output root contains unexpected entries")
    _copy_structural_directory(source, destination_descriptor)


def _validate_git_ref_name(ref_name: str) -> None:
    parts = ref_name.split("/")
    if (
        not ref_name.startswith("refs/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.startswith(".") or part.endswith(".lock") for part in parts)
        or ref_name.endswith(".")
        or ".." in ref_name
        or "@{" in ref_name
        or any(character in ref_name for character in " \\~^:?*[")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in ref_name)
    ):
        raise ObservationUnsupported("Git HEAD contains an unsupported reference name")


def _packed_ref_commit(payload: bytes, ref_name: str) -> str:
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ObservationUnsupported("Git packed references are not valid ASCII") from exc
    matched: str | None = None
    for line in lines:
        if not line or line.startswith(("#", "^")):
            continue
        fields = line.split(" ")
        if len(fields) != 2:
            raise ObservationUnsupported("Git packed references have an unsupported format")
        commit, candidate = fields
        _validate_git_ref_name(candidate)
        if _COMMIT_RE.fullmatch(commit) is None:
            raise ObservationUnsupported("Git packed references contain an invalid commit")
        if candidate == ref_name:
            if matched is not None and matched != commit:
                raise ObservationUnsupported(
                    f"Git packed reference has conflicting values: {ref_name}"
                )
            matched = commit
    if matched is None:
        raise ObservationUnavailable(f"Git HEAD reference is unavailable: {ref_name}")
    return matched


def _policy_entry(
    authority: _PinnedReadAuthority,
    path: Path,
    root: Path,
    *,
    expected_path: _PinnedRegularPath | None = None,
) -> SourceEntry:
    digest, details, _payload = authority.observe(
        path,
        expected_path=expected_path,
    )
    return SourceEntry(
        path=authority.policy_label(path, root),
        file_type="policy",
        size=details.st_size,
        sha256=digest,
        mode=f"{stat.S_IMODE(details.st_mode):04o}",
    )


def _policy_paths(
    root: Path,
    authority: _PinnedReadAuthority,
    detection: Mapping[str, object],
    workspace_policy: _PinnedOptionalPath,
) -> tuple[Path, ...]:
    raw_paths = detection.get("comparison_policy_paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(value, str) for value in raw_paths
    ):
        raise ObservationUnavailable("detector did not report comparison policy inputs")
    paths = {Path(value) for value in raw_paths}
    paths.update(authority.routing_policy_paths)
    workspace = root / ".graphify" / "workspace.toml"
    if authority.require_optional_comparison_path(workspace, workspace_policy) is not None:
        paths.add(workspace)
    return tuple(
        sorted(paths, key=lambda path: authority.policy_label(path, root))
    )


def _excluded_label(value: str, root: Path) -> str:
    path_text, marker, detail = value.partition(" [")
    try:
        relative = Path(path_text).relative_to(root).as_posix()
    except ValueError:
        relative = hashlib.sha256(path_text.encode("utf-8", errors="replace")).hexdigest()
    return relative if not marker else f"{relative} [{detail}"


class Graphify0916Adapter:
    """The sole executable v1 adapter, pinned to published Graphify 0.9.16."""

    adapter_id = "graphify-0.9.16/workspace-adapter-v1"
    engine_baseline = "0.9.16"
    detector_id = "graphify-0.9.16/workspace-observer-v1"

    def __init__(self) -> None:
        try:
            installed = distribution_version("graphifyy")
        except PackageNotFoundError as exc:
            raise UnsupportedCompatibility("graphifyy distribution is unavailable") from exc
        if installed != CANDIDATE_DISTRIBUTION_VERSION:
            raise UnsupportedCompatibility(
                "adapter requires distribution "
                f"{CANDIDATE_DISTRIBUTION_VERSION}, found {installed}"
            )

    def build_structural(
        self,
        source_root: Path,
        *,
        output_root: Path,
        scratch_root: Path | None = None,
    ) -> StructuralBuild:
        root = _absolute_source_path(source_root)
        _require_initial_source_root(root)
        policy_root = _policy_root_for(root)
        output = _absolute_output_path(output_root)
        scratch = (
            None if scratch_root is None else _absolute_output_path(scratch_root)
        )
        if scratch is not None and scratch != output:
            raise ObservationUnsupported("engine scratch root must equal output root")
        if output == policy_root or policy_root in output.parents:
            raise ObservationUnsupported(
                "engine output root must be external to source checkout"
            )
        output_descriptor, output_identity = _open_empty_output_root(
            output,
            require_private=scratch is not None,
        )
        try:
            with _engine_temporary_directory(
                output_descriptor if scratch is not None else None
            ) as (engine_output, engine_output_name, engine_descriptor):
                published_output_names = (
                    tuple(sorted((engine_output_name, "graphify-out")))
                    if scratch is not None
                    else ("graphify-out",)
                )
                authority = _PinnedReadAuthority.open(policy_root)
                try:
                    with _source_snapshot_directory(
                        engine_output,
                        engine_descriptor,
                    ) as snapshot_root:
                        source_details = authority.source.directory_details(root)
                        authority.bind_comparison_directory(root, source_details)
                        workspace_policy = authority.pin_optional_comparison(
                            root / ".graphify" / "workspace.toml"
                        )
                        detection = detect(
                            root,
                            cache_root=engine_output,
                            read_only=True,
                            comparison_reader=authority.read,
                            comparison_pinner=authority.pin_comparison,
                            comparison_directory_lister=(
                                authority.list_comparison_directory
                            ),
                        )
                        authority.source.require_directory_binding(
                            root,
                            source_details,
                        )
                        authority.require_bindings()
                        if detection.get("walk_errors"):
                            raise ObservationUnavailable(
                                "source directory enumeration was incomplete"
                            )
                        if detection.get("comparison_unsupported"):
                            raise ObservationUnsupported(
                                "Google Workspace shortcuts require an unsupported remote comparison"
                            )
                        code_files = tuple(
                            str(path)
                            for path in detection["files"].get("code", [])
                        )
                        pinned_files = {
                            Path(raw_path): authority.require_comparison_path(Path(raw_path))
                            for raw_path in code_files
                        }
                        policy_paths = _policy_paths(
                            root,
                            authority,
                            detection,
                            workspace_policy,
                        )
                        pinned_policies = {
                            path: authority.require_comparison_path(path)
                            for path in policy_paths
                        }
                        policy_digests = {
                            path: authority.observe(
                                path,
                                expected_path=pinned_policies[path],
                            )[0]
                            for path in policy_paths
                        }
                        authority.source.require_directory_binding(
                            root,
                            source_details,
                        )
                        authority.require_bindings()
                        omitted = tuple(
                            sorted(
                                Path(path).relative_to(root).as_posix()
                                for file_type, paths in detection["files"].items()
                                if file_type != "code"
                                for path in paths
                            )
                        )
                        snapshot_files, source_digests = _snapshot_code_files(
                            code_files,
                            pinned_files,
                            root,
                            snapshot_root,
                            authority.source,
                            source_details,
                        )
                        authority.require_bindings()
                        with ephemeral_stat_index(engine_output):
                            extraction = extract(
                                list(snapshot_files),
                                cache_root=engine_output,
                                source_root=snapshot_root,
                            )
                        if extraction.get("errors"):
                            raise ObservationUnavailable(
                                "structural extraction failed for detected code input"
                            )
                        graph = build_from_json(
                            extraction,
                            directed=True,
                            root=snapshot_root,
                        )
                    payload_root = engine_output / "graphify-out"
                    payload_root.mkdir(parents=True, exist_ok=True)
                    if not to_json(
                        graph,
                        {},
                        str(payload_root / "graph.json"),
                        built_at_commit="",
                    ):
                        raise ObservationUnavailable(
                            "structural graph artifact could not be persisted"
                        )
                    _normalize_structural_output(engine_output)
                    authority.source.require_directory_binding(root, source_details)
                    authority.require_bindings()
                    _require_structural_input_digests(
                        authority,
                        pinned_files,
                        source_digests,
                        kind="source",
                    )
                    _require_structural_input_digests(
                        authority,
                        pinned_policies,
                        policy_digests,
                        kind="policy",
                    )
                    _require_policy_root_binding(root, policy_root)
                    _require_output_binding(output, output_identity)
                    _require_output_contents(
                        output_descriptor,
                        (engine_output_name,) if scratch is not None else (),
                    )
                    _publish_structural_output(engine_output, output_descriptor)
                    _require_output_contents(
                        output_descriptor,
                        published_output_names,
                    )
                    _require_output_binding(output, output_identity)
                    authority.source.require_directory_binding(root, source_details)
                    authority.require_bindings()
                    _require_structural_input_digests(
                        authority,
                        pinned_files,
                        source_digests,
                        kind="source",
                    )
                    _require_structural_input_digests(
                        authority,
                        pinned_policies,
                        policy_digests,
                        kind="policy",
                    )
                    _require_policy_root_binding(root, policy_root)
                    _require_output_contents(
                        output_descriptor,
                        published_output_names,
                    )
                    _require_output_binding(output, output_identity)
                finally:
                    authority.close()
            _require_output_binding(output, output_identity)
            _require_output_contents(output_descriptor, ("graphify-out",))
        finally:
            os.close(output_descriptor)
        return StructuralBuild(
            engine_baseline=self.engine_baseline,
            node_count=graph.number_of_nodes(),
            edge_count=graph.number_of_edges(),
            detected_code_files=tuple(
                Path(path).relative_to(root).as_posix() for path in code_files
            ),
            omitted_dispatched_files=omitted,
        )

    def query_structural(self, payload_root: Path, request: QueryRequest) -> str:
        """Run the published 0.9.16 traversal without logs or side effects."""

        from graphify.serve import _query_graph_text

        try:
            root = _absolute_source_path(payload_root)
            graph_path = root / "graph.json"
            reader = _PinnedSourceReader.open(root)
            try:
                root_details = reader.directory_details(root)
                pinned_graph = reader.pin_regular(graph_path)
                _digest, _details, payload = reader.observe(
                    graph_path,
                    collect=True,
                    size_cap=_max_graph_file_bytes(),
                    expected_path=pinned_graph,
                )
                reader.require_directory_binding(root, root_details)
                reader.require_anchor_binding()
            finally:
                reader.close()
            assert payload is not None
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise QueryRejected("graph payload must be an object")
            if "links" not in raw and "edges" in raw:
                raw = dict(raw, links=raw["edges"])
            raw = dict(raw, directed=True)
            graph = json_graph.node_link_graph(raw, edges="links")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
            NetworkXException,
        ) as exc:
            raise QueryRejected(f"graph payload cannot be queried: {exc}") from exc
        try:
            return _query_graph_text(
                graph,
                request.question,
                mode=request.mode,
                depth=request.depth,
                token_budget=request.token_budget,
                context_filters=list(request.context_filters),
            )
        except (KeyError, TypeError, ValueError, NetworkXException) as exc:
            raise QueryRejected(f"graph traversal failed safely: {exc}") from exc

    def _inventory_pass(
        self,
        root: Path,
        *,
        pass_index: int,
        deadline_ns: int | None,
        hook: ObservationHook | None,
    ) -> _InventoryPass:
        _deadline(deadline_ns)
        policy_root = _policy_root_for(root)
        authority = _PinnedReadAuthority.open(
            policy_root,
            deadline_ns=deadline_ns,
        )
        try:
            source_details = authority.source.directory_details(root)
            authority.bind_comparison_directory(root, source_details)
            commit_before = authority.git_head()
            workspace_policy = authority.pin_optional_comparison(
                root / ".graphify" / "workspace.toml"
            )
            detection = detect(
                root,
                read_only=True,
                google_workspace=False,
                comparison_reader=authority.read,
                comparison_pinner=authority.pin_comparison,
                comparison_directory_lister=authority.list_comparison_directory,
            )
            _emit(hook, "inventory_detected", pass_index=pass_index)
            authority.source.require_directory_binding(root, source_details)
            authority.require_bindings()
            if detection.get("walk_errors"):
                raise ObservationUnavailable("source directory enumeration was incomplete")
            if detection.get("comparison_unsupported"):
                raise ObservationUnsupported(
                    "Google Workspace shortcuts require an unsupported remote comparison"
                )

            detected_paths = tuple(
                (str(file_type), Path(raw_path))
                for file_type, paths in sorted(detection["files"].items())
                for raw_path in paths
            )
            pinned_entries = {
                path: authority.require_comparison_path(path)
                for _file_type, path in detected_paths
            }
            policy_paths = _policy_paths(
                root,
                authority,
                detection,
                workspace_policy,
            )
            pinned_policies = {
                path: authority.require_comparison_path(path) for path in policy_paths
            }
            authority.source.require_directory_binding(root, source_details)
            authority.require_bindings()

            entries: list[SourceEntry] = []
            seen: set[str] = set()
            for file_type, path in detected_paths:
                source_entry = _entry(
                    authority.source,
                    path,
                    root,
                    file_type,
                    expected_path=pinned_entries[path],
                )
                if source_entry.path in seen:
                    raise ObservationUnsupported(
                        f"detector returned duplicate source path: {source_entry.path}"
                    )
                seen.add(source_entry.path)
                entries.append(source_entry)
                _emit(
                    hook,
                    "inventory_file_hashed",
                    pass_index=pass_index,
                    path=source_entry.path,
                )
                _deadline(deadline_ns)
            entries.sort(key=lambda item: item.path)
            authority.source.require_directory_binding(root, source_details)

            policy_entries = tuple(
                _policy_entry(
                    authority,
                    path,
                    root,
                    expected_path=pinned_policies[path],
                ).to_dict()
                for path in policy_paths
            )
            authority.source.require_directory_binding(root, source_details)
            authority.require_bindings()
            excluded = sorted(
                _excluded_label(value, root)
                for bucket in ("skipped_sensitive", "unclassified")
                for value in detection.get(bucket, [])
            )
            commit_after = authority.git_head()
            if commit_before != commit_after:
                raise ObservationUnstable("Git HEAD changed during source observation")
            authority.source.require_directory_binding(root, source_details)
            authority.require_bindings()
            inventory_sha256 = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "detector_id": self.detector_id,
                        "entries": [entry.to_dict() for entry in entries],
                        "excluded": excluded,
                    }
                )
            ).hexdigest()
            policy_sha256 = hashlib.sha256(
                canonical_json_bytes({"entries": list(policy_entries)})
            ).hexdigest()
            result = _InventoryPass(
                source_commit=commit_after,
                inventory_sha256=inventory_sha256,
                policy_sha256=policy_sha256,
                entries=tuple(entries),
            )
            _emit(
                hook,
                "inventory_complete",
                pass_index=pass_index,
                inventory_sha256=inventory_sha256,
            )
            return result
        finally:
            authority.close()

    def observe(
        self,
        source_root: Path,
        *,
        max_inventory_passes: int = 6,
        deadline_ns: int | None = None,
        hook: ObservationHook | None = None,
    ) -> SourceObservation:
        root = _absolute_source_path(source_root)
        _require_initial_source_root(root)
        if max_inventory_passes < 2:
            raise ObservationUnstable("at least two complete inventory passes are required")
        previous: _InventoryPass | None = None
        last_unstable: ObservationUnstable | None = None
        for pass_index in range(1, max_inventory_passes + 1):
            _deadline(deadline_ns)
            try:
                current = self._inventory_pass(
                    root,
                    pass_index=pass_index,
                    deadline_ns=deadline_ns,
                    hook=hook,
                )
            except _AuthorityChanged:
                raise
            except ObservationUnstable as exc:
                previous = None
                last_unstable = exc
                continue
            if previous is not None and current == previous:
                return SourceObservation(
                    source_commit=current.source_commit,
                    inventory_sha256=current.inventory_sha256,
                    policy_sha256=current.policy_sha256,
                    detector_id=self.detector_id,
                    stable_inventory_passes=2,
                    entries=current.entries,
                )
            previous = current
        detail = "source inventory did not produce two consecutive equal passes"
        if last_unstable is not None:
            detail = f"{detail}: {last_unstable}"
        raise ObservationUnstable(detail)

__all__ = ["Graphify0916Adapter"]
