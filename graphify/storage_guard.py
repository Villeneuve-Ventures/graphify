"""Deny ordinary Graphify output inside a certified workspace state tree.

This module deliberately has no workspace runtime imports.  Its marker is a
denial signal, not authority to read or write workspace state.
"""

from __future__ import annotations

import errno
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO, BinaryIO


WORKSPACE_ROOT_MARKER = ".graphify-workspace-root.json"
_OLD_REGISTRY_NAMES = frozenset({"registry.json", "registry.previous.json", "registry.lock"})
_OLD_WORKSPACE_NAMES = frozenset({"generations", "staging", "quarantine"})


class ManagedWorkspaceOutputError(ValueError):
    """An ordinary output would enter workspace-owned storage."""


def _entry(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _is_directory(directory_fd: int, name: str) -> bool:
    entry = _entry(directory_fd, name)
    return entry is not None and stat.S_ISDIR(entry.st_mode)


def _historical_root(directory_fd: int) -> bool:
    """Recognize the retained workspace/v1 layout without reading its records."""
    if not _is_directory(directory_fd, "workspaces"):
        return False
    if any(_entry(directory_fd, name) is not None for name in _OLD_REGISTRY_NAMES):
        return True
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    workspaces_fd = os.open("workspaces", flags, dir_fd=directory_fd)
    try:
        with os.scandir(workspaces_fd) as members:
            workspace_names = [item.name for item in members]
        for name in workspace_names:
            try:
                if str(uuid.UUID(name)) != name:
                    continue
            except ValueError:
                continue
            if not _is_directory(workspaces_fd, name):
                continue
            workspace_fd = os.open(name, flags, dir_fd=workspaces_fd)
            try:
                if any(_entry(workspace_fd, part) is not None for part in _OLD_WORKSPACE_NAMES):
                    return True
            finally:
                os.close(workspace_fd)
    finally:
        os.close(workspaces_fd)
    return False


def _historical_path(directory: Path) -> bool:
    workspaces = directory / "workspaces"
    if not workspaces.is_dir():
        return False
    if any((directory / name).exists() for name in _OLD_REGISTRY_NAMES):
        return True
    for workspace in workspaces.iterdir():
        try:
            if str(uuid.UUID(workspace.name)) == workspace.name and workspace.is_dir():
                if any((workspace / name).exists() for name in _OLD_WORKSPACE_NAMES):
                    return True
        except ValueError:
            continue
    return False


def require_ordinary_output(path: str | os.PathLike[str]) -> None:
    """Inspect existing ancestry before an ordinary write creates any output.

    Existing directories are opened from the filesystem root with no-follow
    descriptors.  A symlink in the supplied path is resolved and inspected as
    its target; callers must repeat admission at their final write boundary.
    Unknown or racing ancestry refuses instead of granting a writable path.
    """
    supplied = Path(path).expanduser().absolute()
    resolved = Path(os.path.realpath(supplied))
    if os.name == "nt":
        for ancestor in (resolved, *resolved.parents):
            if (ancestor / WORKSPACE_ROOT_MARKER).exists() or _historical_path(ancestor):
                raise ManagedWorkspaceOutputError("ordinary output is inside workspace state")
        return
    anchor = Path(resolved.anchor)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(anchor, flags)
    current = anchor
    try:
        parts = resolved.relative_to(anchor).parts
        for index, part in enumerate(("", *parts)):
            if part:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    return
                except NotADirectoryError as exc:
                    if index == len(parts) and _entry(descriptor, part) is not None:
                        return
                    raise ManagedWorkspaceOutputError("output ancestry is unsafe") from exc
                except OSError as exc:
                    _raise_unsafe_path(exc)
                os.close(descriptor)
                descriptor = child
                current /= part
            opened = os.fstat(descriptor)
            try:
                named = current.lstat()
            except FileNotFoundError as exc:
                raise ManagedWorkspaceOutputError("output ancestry changed") from exc
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise ManagedWorkspaceOutputError("output ancestry changed")
            if _entry(descriptor, WORKSPACE_ROOT_MARKER) is not None or _historical_root(descriptor):
                raise ManagedWorkspaceOutputError("ordinary output is inside workspace state")
    finally:
        os.close(descriptor)


def _require_directory_binding(path: Path, descriptor: int) -> None:
    """Recheck that a held directory still has its admitted ancestry and name."""

    require_ordinary_output(path)
    _require_opened_directory(path, descriptor)


def _require_opened_directory(path: Path, descriptor: int) -> None:
    """Check one traversal step without reopening its already inspected parents."""
    try:
        named = path.lstat()
        opened = os.fstat(descriptor)
    except FileNotFoundError as exc:
        raise ManagedWorkspaceOutputError("ordinary output ancestry changed") from exc
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise ManagedWorkspaceOutputError("ordinary output ancestry changed")
    if _entry(descriptor, WORKSPACE_ROOT_MARKER) is not None or _historical_root(descriptor):
        raise ManagedWorkspaceOutputError("ordinary output is inside workspace state")


def _raise_unsafe_path(exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise ManagedWorkspaceOutputError("output ancestry changed or is unsafe") from exc
    raise exc


@contextmanager
def ordinary_directory(
    path: str | os.PathLike[str], *, create: bool = False,
) -> Iterator[tuple[Path, int]]:
    """Pin an admitted directory; optional creation stays descriptor-relative."""

    resolved = Path(os.path.realpath(Path(path).expanduser().absolute()))
    if os.name == "nt":
        require_ordinary_output(resolved)
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise ManagedWorkspaceOutputError("ordinary output parent is missing")
        yield resolved, -1
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved.anchor, flags)
    try:
        parts = resolved.relative_to(resolved.anchor).parts
        current = Path(resolved.anchor)
        for part in ("", *parts):
            _require_opened_directory(current, descriptor)
            if not part:
                continue
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                # Recheck all ancestors before the traversal itself mutates disk.
                _require_directory_binding(current, descriptor)
                try:
                    os.mkdir(part, dir_fd=descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError as exc:
                    _raise_unsafe_path(exc)
            except OSError as exc:
                _raise_unsafe_path(exc)
            os.close(descriptor)
            descriptor = child
            current /= part
            _require_opened_directory(current, descriptor)
        _require_directory_binding(current, descriptor)
        yield resolved, descriptor
    finally:
        os.close(descriptor)


def ordinary_mkdir(path: str | os.PathLike[str]) -> Path:
    """Create an ordinary directory chain and return its resolved safe location."""

    with ordinary_directory(path, create=True) as (resolved, _descriptor):
        return resolved


@contextmanager
def ordinary_open(
    path: str | os.PathLike[str], mode: str, *, encoding: str | None = None,
) -> Iterator[TextIO | BinaryIO]:
    """Open a regular output relative to a held, inspected parent directory."""

    if mode not in {"w", "a", "wb", "ab"}:
        raise ValueError("unsupported ordinary output mode")
    resolved = Path(os.path.realpath(Path(path).expanduser().absolute()))
    if os.name == "nt":
        require_ordinary_output(resolved)
        with open(resolved, mode, encoding=encoding) as stream:
            yield stream
        return
    with ordinary_directory(resolved.parent) as (_parent, parent_descriptor):
        _require_directory_binding(resolved.parent, parent_descriptor)
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        if "a" in mode:
            flags |= os.O_APPEND
        try:
            descriptor = os.open(
                resolved.name, flags | os.O_CREAT | os.O_EXCL, 0o666,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            try:
                descriptor = os.open(resolved.name, flags, dir_fd=parent_descriptor)
            except IsADirectoryError:
                raise
            except OSError as exc:
                _raise_unsafe_path(exc)
        except OSError as exc:
            _raise_unsafe_path(exc)
        try:
            _require_directory_binding(resolved.parent, parent_descriptor)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ManagedWorkspaceOutputError("ordinary output is not a singular regular file")
            if "w" in mode:
                os.ftruncate(descriptor, 0)
            with os.fdopen(descriptor, mode, encoding=encoding) as stream:
                descriptor = -1
                yield stream
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def ordinary_temporary_file(
    directory: str | os.PathLike[str],
    *,
    mode: str = "w+",
    encoding: str = "utf-8",
    newline: str = "",
) -> TextIO:
    """Create an unlinked temporary stream through an admitted directory fd."""

    if mode != "w+":
        raise ValueError("unsupported ordinary temporary mode")
    with ordinary_directory(directory) as (resolved, parent_fd):
        if os.name == "nt":
            import tempfile

            return tempfile.TemporaryFile(mode=mode, encoding=encoding, newline=newline, dir=resolved)
        _require_directory_binding(resolved, parent_fd)
        name = f".graphify-stage-{uuid.uuid4().hex}"
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        unlinked = False
        try:
            _require_directory_binding(resolved, parent_fd)
            os.unlink(name, dir_fd=parent_fd)
            unlinked = True
            stream = os.fdopen(descriptor, mode, encoding=encoding, newline=newline)
            descriptor = -1
            return stream
        except Exception:
            if not unlinked:
                try:
                    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    opened = os.fstat(descriptor)
                    if (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino):
                        os.unlink(name, dir_fd=parent_fd)
                except OSError:
                    # Preserve the original staging failure if cleanup also fails.
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def ordinary_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Replace one ordinary file using pinned source and destination parents."""

    src = Path(source).expanduser().absolute()
    dst = Path(destination).expanduser().absolute()
    if os.name == "nt":
        require_ordinary_output(src)
        require_ordinary_output(dst)
        os.replace(src, dst)
        return
    with ordinary_directory(src.parent) as (src_parent, src_fd):
        with ordinary_directory(dst.parent) as (dst_parent, dst_fd):
            _require_directory_binding(src_parent, src_fd)
            _require_directory_binding(dst_parent, dst_fd)
            os.replace(src.name, dst.name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def ordinary_unlink(path: str | os.PathLike[str]) -> None:
    """Remove one file through an inspected parent; never follow its final link."""

    target = Path(path).expanduser().absolute()
    if os.name == "nt":
        require_ordinary_output(target)
        target.unlink()
        return
    with ordinary_directory(target.parent) as (parent, parent_fd):
        _require_directory_binding(parent, parent_fd)
        os.unlink(target.name, dir_fd=parent_fd)


def ordinary_atomic_bytes(path: str | os.PathLike[str], payload: bytes) -> None:
    """Write and replace a regular file under one held ordinary directory."""

    target = Path(path).expanduser().absolute()
    if os.name == "nt":
        import tempfile

        require_ordinary_output(target)
        ordinary_mkdir(target.parent)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(payload)
        try:
            ordinary_replace(temp, target)
        finally:
            try:
                ordinary_unlink(temp)
            except FileNotFoundError:
                # Replacement may already have consumed the temporary name.
                pass
        return
    with ordinary_directory(target.parent, create=True) as (parent, parent_fd):
        temporary = f".{target.name}.tmp-{uuid.uuid4().hex}"
        _require_directory_binding(parent, parent_fd)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=parent_fd,
        )
        try:
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("ordinary output short write")
                    remaining = remaining[written:]
            finally:
                os.close(fd)
            _require_directory_binding(parent, parent_fd)
            os.replace(temporary, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                # Replacement may already have consumed the temporary name.
                pass
