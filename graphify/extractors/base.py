# DO NOT import from graphify.extract here — direction is extract.py → extractors/ only.
from __future__ import annotations

import errno
import fnmatch
import os
from pathlib import Path
import stat

from graphify.ids import make_id

# Language built-in globals that AST may classify as call targets when used as
# constructors or coercion functions (e.g. String(x), Number(x), Boolean(x)).
# Without this filter they become god-nodes accumulating spurious edges from
# every call site. Filter applied at same-file and cross-file resolution.
# See issue #726.
_LANGUAGE_BUILTIN_GLOBALS: frozenset[str] = frozenset({
    # JavaScript / TypeScript ECMAScript built-ins
    "String", "Number", "Boolean", "Object", "Array", "Symbol", "BigInt",
    "Date", "RegExp", "Error", "TypeError", "RangeError", "SyntaxError",
    "ReferenceError", "EvalError", "URIError",
    "Promise", "Map", "Set", "WeakMap", "WeakSet", "JSON", "Math",
    "Reflect", "Proxy", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    # Browser / Node common globals
    "URL", "URLSearchParams", "FormData", "Blob", "File",
    "Headers", "Request", "Response", "AbortController", "AbortSignal",
    "TextEncoder", "TextDecoder", "console",
    # Python built-in callables
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
    "len", "range", "enumerate", "zip", "map", "filter", "sum", "min", "max",
    "print", "open", "isinstance", "type", "super", "sorted", "reversed",
    "any", "all", "abs", "round", "next", "iter", "hash", "id", "repr",
    "callable", "getattr", "setattr", "hasattr", "delattr", "vars", "dir",
})


def _make_id(*parts: str) -> str:
    return make_id(*parts)


def _file_stem(path: Path) -> str:
    """Stem used as the node-ID prefix for a file and its symbols.

    The full path (extension dropped) is preserved as path segments; ``make_id``
    later collapses the separators to underscores. Using every segment — not just
    the immediate parent dir (#1504) — means same-named files in different
    directories get distinct IDs instead of colliding into one
    last-writer-wins node:

        docs/v1/api/README.md -> docs/v1/api/README -> docs_v1_api_readme
        docs/v2/api/README.md -> docs/v2/api/README -> docs_v2_api_readme

    Top-level files keep a bare stem (``setup.py`` -> ``setup``). When passed an
    absolute path the whole path is encoded; the extract() id-remap post-pass
    re-derives the canonical repo-relative form from ``source_file`` so the on-disk
    location can't leak into the persisted IDs (#502).

    Returns "" for a path with no name (``Path('.')`` — a source_file that equals
    the scan root, so it has no per-file stem). Guarding here keeps
    ``path.with_suffix("")`` from raising ``ValueError: '.' has an empty name`` and
    protects every caller, not just ``_semantic_id_remap`` (#1618)."""
    if not path.name:
        return ""
    return path.with_suffix("").as_posix()


def _read_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def strict_aware(function):
    """Opt a built-in into explicit strict dispatch without wrapping its identity."""
    function._graphify_strict_aware = True
    return function


def call_with_strict(function, *args, strict=False):
    """Keep legacy positional callbacks intact; never retry a failed invocation."""
    if strict and getattr(function, "_graphify_strict_aware", False):
        return function(*args, strict=True)
    return function(*args)


def _optional_stat(path, *, follow_symlinks=True):
    try:
        return os.stat(path, follow_symlinks=follow_symlinks)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
            raise
        return None


def checked_exists(path: Path, *, strict=False):
    return _optional_stat(path) is not None if strict else path.exists()


def checked_is_file(path: Path, *, strict=False):
    if not strict:
        return path.is_file()
    info = _optional_stat(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def checked_is_dir(path: Path, *, strict=False):
    if not strict:
        return path.is_dir()
    info = _optional_stat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def checked_glob(root: Path, pattern: str, *, strict=False):
    """Select native relative glob components without suppressing selected I/O errors.

    Literal components use filesystem lookup, wildcard components use native case
    matching and scandir order. Recursive components visit directories in the
    same stack order as Path.glob and do not recurse through symlink directories.
    Materialize each selected directory before yielding so a partial scan fails.
    """
    if not strict:
        return root.glob(pattern)
    pattern = os.fspath(pattern)
    drive, tail = os.path.splitdrive(pattern)
    if drive or os.path.isabs(tail):
        raise NotImplementedError("Non-relative patterns are unsupported")
    if os.altsep:
        tail = tail.replace(os.altsep, os.sep)
    parts = [part for part in tail.split(os.sep) if part and part != "."]
    if not parts:
        raise ValueError(f"Unacceptable pattern: {pattern!r}")
    if tail.endswith(os.sep):
        parts.append("")

    def scan(path):
        with os.scandir(path) as entries:
            return list(entries)

    def directory(entry):
        info = _optional_stat(entry.path) if entry.is_symlink() else entry.stat()
        return info is not None and stat.S_ISDIR(info.st_mode)

    def select(path, index, known=False):
        if index == len(parts):
            if known or _optional_stat(path, follow_symlinks=False) is not None:
                yield Path(path)
            return
        part = parts[index]
        if part == "**":
            if not known and not checked_is_dir(Path(path), strict=True):
                return
            while index < len(parts) and parts[index] == "**":
                index += 1
            yield from select(path, index, known)
            stack = [path]
            while stack:
                current = stack.pop()
                for entry in scan(current):
                    is_dir = stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)
                    if is_dir or index == len(parts):
                        child = entry.path + os.sep if is_dir else entry.path
                        yield from select(child, index, True)
                        if is_dir:
                            stack.append(child)
            return
        if part in ("", ".."):
            child = path + part + (os.sep if index + 1 < len(parts) else "")
            yield from select(child, index + 1, known)
            return
        if not any(char in part for char in "*?["):
            # Fuse literal components, preserving filesystem case lookup and
            # avoiding scans of unrelated directories along a known path.
            while index + 1 < len(parts) and not any(char in parts[index + 1] for char in "*?["):
                index += 1
                part += os.sep + parts[index]
            child = path + part + (os.sep if index + 1 < len(parts) else "")
            yield from select(child, index + 1)
            return
        if not known and not checked_is_dir(Path(path), strict=True):
            return
        for entry in scan(path):
            if not fnmatch.fnmatch(entry.name, part):
                continue
            if index + 1 < len(parts):
                if directory(entry):
                    yield from select(entry.path + os.sep, index + 1, True)
            else:
                yield Path(entry.path)

    return select(str(root) + os.sep, 0, True)


def checked_rglob(root: Path, pattern: str, *, strict=False):
    if not strict:
        return root.rglob(pattern)
    return checked_glob(root, os.path.join("**", pattern), strict=True)
