"""Safely publish the advisory Graphify Python interpreter pointer.

The pointer is write-only diagnostic state.  It is never an authority for
selecting a program to execute; operational readers must validate and bind the
interpreter independently.

On POSIX, publication is anchored to an opened, non-symlink parent directory
and uses descriptor-relative operations. The Windows standard library does not
expose an equivalent handle-relative, reparse-point-safe atomic replace, so
Windows fails closed without publishing this optional advisory state.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path


_WINDOWS = os.name == "nt"
_POSIX_DIR_FD_TRAVERSAL = (
    bool(getattr(os, "O_DIRECTORY", 0))
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
)


class InterpreterPointerError(RuntimeError):
    """Raised when the advisory interpreter pointer cannot be safely written."""


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _validate_interpreter(interpreter: Path | str | None) -> tuple[Path, str]:
    raw = sys.executable if interpreter is None else os.fspath(interpreter)
    if not isinstance(raw, str):
        raise InterpreterPointerError("interpreter path must be text")
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise InterpreterPointerError("interpreter path is malformed")

    path = Path(raw)
    if not path.is_absolute():
        raise InterpreterPointerError("interpreter path must be absolute")
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            raise InterpreterPointerError("interpreter target is not executable")
    except OSError as exc:
        raise InterpreterPointerError("interpreter target is not executable") from exc
    return path, raw


def _open_parent_directory(parent: Path) -> int:
    """Walk every parent component through directory descriptors."""
    if not _POSIX_DIR_FD_TRAVERSAL:
        raise InterpreterPointerError("safe descriptor-relative parent traversal is unavailable")

    directory_flag = os.O_DIRECTORY
    nofollow_flag = os.O_NOFOLLOW
    if parent.is_absolute():
        anchor = parent.anchor
        parts = parent.parts[1:]
    else:
        anchor = "."
        parts = parent.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise InterpreterPointerError("pointer parent contains a traversal component")

    flags = os.O_RDONLY | directory_flag | nofollow_flag | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(anchor, flags)
    except OSError as exc:
        raise InterpreterPointerError("pointer parent anchor could not be opened safely") from exc

    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
    except OSError as exc:
        os.close(directory_fd)
        raise InterpreterPointerError("pointer parent could not be traversed safely") from exc
    return directory_fd


def _validate_parent_info(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise InterpreterPointerError("pointer parent must be a directory")
    if _is_reparse_point(info):
        raise InterpreterPointerError("pointer parent must not be a reparse point")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise InterpreterPointerError("pointer parent must be owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise InterpreterPointerError("pointer parent must not be group- or other-writable")


def _validate_destination_info(info: os.stat_result | None) -> None:
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise InterpreterPointerError("pointer destination must not be a link or reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise InterpreterPointerError("pointer destination must be a regular file")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise InterpreterPointerError("pointer destination must be owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise InterpreterPointerError("pointer destination must not be group- or other-writable")


def _lstat_at(name: str, directory_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InterpreterPointerError("pointer destination cannot be inspected") from exc


def _temp_name(destination_name: str) -> str:
    return f".{destination_name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"


def _write_all(file_descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing interpreter pointer")
        view = view[written:]


def _write_posix(pointer: Path, payload: bytes) -> None:
    parent = pointer.parent
    try:
        parent_before = parent.lstat()
    except OSError as exc:
        raise InterpreterPointerError("pointer parent is unavailable") from exc
    _validate_parent_info(parent_before)
    directory_fd = _open_parent_directory(parent)

    temp_name = _temp_name(pointer.name)
    temp_created = False
    try:
        opened_parent = os.fstat(directory_fd)
        _validate_parent_info(opened_parent)
        if (opened_parent.st_dev, opened_parent.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            raise InterpreterPointerError("pointer parent changed during validation")
        _validate_destination_info(_lstat_at(pointer.name, directory_fd))

        temp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        temp_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        file_descriptor = os.open(temp_name, temp_flags, 0o600, dir_fd=directory_fd)
        temp_created = True
        try:
            temp_info = os.fstat(file_descriptor)
            if not stat.S_ISREG(temp_info.st_mode):
                raise InterpreterPointerError("temporary pointer is not a regular file")
            os.fchmod(file_descriptor, 0o600)
            _write_all(file_descriptor, payload)
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)

        current_parent = parent.lstat()
        _validate_parent_info(current_parent)
        if (current_parent.st_dev, current_parent.st_ino) != (opened_parent.st_dev, opened_parent.st_ino):
            raise InterpreterPointerError("pointer parent changed before publication")
        _validate_parent_info(os.fstat(directory_fd))
        _validate_destination_info(_lstat_at(pointer.name, directory_fd))
        os.replace(temp_name, pointer.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temp_created = False

        published_parent = parent.lstat()
        _validate_parent_info(published_parent)
        opened_parent = os.fstat(directory_fd)
        _validate_parent_info(opened_parent)
        if (published_parent.st_dev, published_parent.st_ino) != (
            opened_parent.st_dev,
            opened_parent.st_ino,
        ):
            raise InterpreterPointerError("pointer parent changed during publication")
        published_destination = _lstat_at(pointer.name, directory_fd)
        if published_destination is None:
            raise InterpreterPointerError("pointer destination disappeared during publication")
        _validate_destination_info(published_destination)
    except OSError as exc:
        raise InterpreterPointerError("interpreter pointer atomic publication failed") from exc
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        os.close(directory_fd)


def write_interpreter_pointer(
    pointer_path: Path | str = Path("graphify-out/.graphify_python"),
    *,
    interpreter: Path | str | None = None,
) -> Path:
    """Safely publish an absolute executable path and return its lexical form."""
    interpreter_path, interpreter_text = _validate_interpreter(interpreter)
    pointer = Path(pointer_path)
    if pointer.name in {"", ".", ".."}:
        raise InterpreterPointerError("pointer destination is invalid")
    if _WINDOWS:
        raise InterpreterPointerError(
            "safe advisory pointer publication is unavailable on Windows"
        )

    payload = interpreter_text.encode("utf-8")
    _write_posix(pointer, payload)
    return interpreter_path


def main(argv: list[str] | None = None) -> int:
    """Run the write-only interpreter-pointer command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write", help="publish the current Python interpreter")
    write_parser.add_argument("pointer_path", nargs="?", default="graphify-out/.graphify_python")
    arguments = parser.parse_args(argv)

    if arguments.command == "write":
        try:
            write_interpreter_pointer(arguments.pointer_path)
        except InterpreterPointerError as exc:
            parser.exit(1, f"interpreter pointer error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
