"""Opt-in synchronous engine input I/O. No workspace lifecycle or publication.

SourceIO owns bounded no-follow reads and evidence for one engine operation (or
several operations sharing a context). It is not an atomic filesystem snapshot.
An adapter may override read_bytes/probe/listdir to supply its pinned descriptors;
those implementations must retain bounds, repeat consistency and failure latching.
All engine source operations use these helpers; ordinary calls retain Path I/O.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import builtins
import hashlib
import os
from pathlib import Path
import stat


class SourceError(RuntimeError):
    code = 'source_error'


class SourceChanged(SourceError):
    code = 'inconsistent_input'


class SourceUnsupported(SourceError):
    code = 'unsupported_input'


class SourceUnavailable(SourceError):
    code = 'failed_read'


class SourceEnumerationFailed(SourceUnavailable):
    code = 'partial_enumeration'


_ACTIVE = ContextVar('graphify_source_io', default=None)
_QUIET = ContextVar('graphify_quiet', default=False)


def current_source_io():
    return _ACTIVE.get()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _binding(info):
    # Directory mtime/size include unconsumed operational output; membership is
    # recorded separately. Names, type, mode and stable binding remain material.
    return (info.st_dev, info.st_ino, info.st_mode)


class SourceIO:
    """Rooted input reader with explicit optional policy/Git roots and hard bounds.

    Paths must be absolute or relative to source_root. Roots are pinned on entry;
    extra_roots is a label -> absolute directory allowlist supplied by the caller,
    never inferred from corpus text. Evidence is in-memory and uses rooted labels.
    No symlinks, devices, subprocesses, persistent caches or providers are allowed.
    Close the context to release root descriptors. A failed operation poisons it.
    """
    def __init__(self, source_root, *, extra_roots=None, max_file_bytes=64 * 1024 * 1024,
                 max_total_bytes=512 * 1024 * 1024, max_entries=100_000):
        self.root = Path(os.path.abspath(source_root))
        self.roots = {'source': self.root, **(extra_roots or {})}
        if self.roots['source'] != self.root or any(
            not isinstance(label, str) or not label or '/' in label or ':' in label
            or not Path(root).is_absolute() for label, root in self.roots.items()
        ):
            raise ValueError('invalid source root allowlist')
        if any(type(n) is not int or n <= 0 for n in
               (max_file_bytes, max_total_bytes, max_entries)):
            raise ValueError('input bounds must be positive integers')
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self._bytes = 0
        self._records = {}
        self._evidence_units = 0
        self._bindings = {}
        self._fds = {}
        self.failure = None

    def __enter__(self):
        if self._fds:
            raise SourceUnsupported('input context is already open')
        try:
            for label, root in self.roots.items():
                # Walk every ancestor without following a substituted symlink.
                path = Path(os.path.abspath(root))
                fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    for part in path.parts[1:]:
                        child = os.open(part, self._dir_flags(), dir_fd=fd)
                        os.close(fd)
                        fd = child
                    self._fds[label] = fd
                    self._record("directory", "." if label == "source" else f"{label}:.",
                                 _binding(os.fstat(fd)))
                except BaseException:
                    os.close(fd)
                    raise
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()

    @staticmethod
    def _dir_flags():
        if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
            raise SourceUnsupported('no-follow directory descriptors unavailable')
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK

    def refuse(self, detail, error=SourceUnsupported):
        self.failure = self.failure or error(detail)
        raise self.failure

    def check(self):
        if self.failure is not None:
            raise self.failure
        if not self._fds:
            raise SourceUnsupported('input context must be opened with a context manager')

    def _locate(self, path):
        self.check()
        path = Path(path)
        path = Path(os.path.abspath(path if path.is_absolute() else self.root / path))
        for label, root in sorted(self.roots.items(), key=lambda item: -len(Path(item[1]).parts)):
            root = Path(root)
            if path.is_relative_to(root):
                rel = path.relative_to(root)
                key = rel.as_posix() if label == 'source' else f'{label}:{rel.as_posix()}'
                return label, rel, key
        self.refuse(f'input outside declared roots: {path}')

    def _record(self, operation, key, value):
        record_key = operation, key
        if record_key in self._records and self._records[record_key] != value:
            self.refuse(f'{operation} changed: {key}', SourceChanged)
        if record_key not in self._records:
            cost = 1 + (len(value[1]) if operation == 'list' else 0)
            if self._evidence_units + cost > self.max_entries:
                self.refuse('input evidence entry limit exceeded')
            self._evidence_units += cost
        self._records[record_key] = value

    def _bind(self, key, value):
        if key in self._bindings and self._bindings[key] != value:
            self.refuse(f'input binding changed: {key}', SourceChanged)
        # Every binding belongs to an evidence record or a charged list member.
        if key not in self._bindings and len(self._bindings) >= self.max_entries:
            self.refuse('input binding entry limit exceeded')
        self._bindings[key] = value

    @property
    def evidence(self):
        return tuple({'operation': op, 'path': key, 'value': value}
                     for (op, key), value in sorted(self._records.items()))

    @contextmanager
    def _parent(self, path):
        label, rel, key = self._locate(path)
        fd = os.dup(self._fds[label])
        try:
            # Compare the live pathname walk with the pinned root on every use.
            root = Path(self.roots[label])
            live = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in root.parts[1:]:
                    child = os.open(part, self._dir_flags(), dir_fd=live)
                    os.close(live)
                    live = child
                if _binding(os.fstat(live)) != _binding(os.fstat(fd)):
                    self.refuse(f'root binding changed: {label}', SourceChanged)
            finally:
                os.close(live)
            traversed = Path('.')
            for part in rel.parts[:-1]:
                child = os.open(part, self._dir_flags(), dir_fd=fd)
                traversed /= part
                os.close(fd)
                fd = child
                binding_key = str(traversed) if label == 'source' else f'{label}:{traversed}'
                binding = _binding(os.fstat(fd))
                self._record('directory', binding_key, binding)
                self._bind(binding_key, binding)
            yield fd, rel.name if rel.parts else '.', key
        finally:
            os.close(fd)

    def probe(self, path):
        try:
            with self._parent(path) as (fd, name, key):
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except (FileNotFoundError, NotADirectoryError):
                    info = None
                if info is not None and stat.S_ISLNK(info.st_mode):
                    self.refuse(f'symlink input: {key}')
                value = None if info is None else (
                    _binding(info) if stat.S_ISDIR(info.st_mode) else _identity(info))
                self._record('probe', key, value)
                self._bind(key, None if info is None else _binding(info))
                return info
        except (FileNotFoundError, NotADirectoryError):
            _, _, key = self._locate(path)
            self._record('probe', key, None)
            self._bind(key, None)
            return None
        except OSError as exc:
            self.refuse(str(exc), SourceUnavailable)

    def read_bytes(self, path):
        try:
            info = self.probe(path)
            if info is None:
                self.refuse(f'missing input: {path}', SourceUnavailable)
            if not stat.S_ISREG(info.st_mode):
                self.refuse(f'not a regular input: {path}')
            if info.st_size > self.max_file_bytes or self._bytes + info.st_size > self.max_total_bytes:
                self.refuse('input byte limit exceeded')
            with self._parent(path) as (parent, name, key):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    if _identity(info) != _identity(os.fstat(fd)):
                        self.refuse(f'input changed during open: {key}', SourceChanged)
                    chunks = []
                    size = 0
                    while True:
                        chunk = os.read(fd, min(65536, self.max_file_bytes - size + 1,
                                                self.max_total_bytes - self._bytes + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        self._bytes += len(chunk)
                        if size > self.max_file_bytes or self._bytes > self.max_total_bytes:
                            self.refuse('input byte limit exceeded')
                        chunks.append(chunk)
                    if _identity(info) != _identity(os.fstat(fd)):
                        self.refuse(f'input changed during read: {key}', SourceChanged)
                    payload = b''.join(chunks)
                finally:
                    os.close(fd)
            after = self.probe(path)
            if after is None or _identity(info) != _identity(after) or len(payload) != info.st_size:
                self.refuse(f'input changed after read: {key}', SourceChanged)
            self._record('read', key, (_identity(info), hashlib.sha256(payload).hexdigest()))
            return payload
        except OSError as exc:
            self.refuse(str(exc), SourceUnavailable)

    def listdir(self, path):
        try:
            with self._parent(path) as (parent, name, key):
                fd = os.open(name, self._dir_flags(), dir_fd=parent)
                try:
                    before = os.fstat(fd)
                    names = []
                    previous = self._records.get(('list', key))
                    old_cost = 0 if previous is None else 1 + len(previous[1])
                    available = self.max_entries - self._evidence_units + old_cost - 1
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if len(names) >= available:
                                self.refuse('directory entry limit exceeded')
                            info = entry.stat(follow_symlinks=False)
                            _, _, child_key = self._locate(Path(path) / entry.name)
                            self._bind(child_key, _binding(info))
                            names.append((entry.name, _binding(info)))
                    if _identity(before) != _identity(os.fstat(fd)):
                        self.refuse(f'directory changed during listing: {key}', SourceChanged)
                    self._record('list', key, (_binding(before), tuple(sorted(names))))
                finally:
                    os.close(fd)
            after = self.probe(path)
            if after is None or _binding(before) != _binding(after):
                self.refuse(f'directory binding changed: {key}', SourceChanged)
            return [(Path(path) / name, mode[2]) for name, mode in names]
        except OSError as exc:
            self.refuse(f'partial enumeration: {exc}', SourceEnumerationFailed)


@contextmanager
def engine_inputs(source_io=None, *, quiet=False):
    existing = current_source_io()
    if existing is not None and source_io is not None and existing is not source_io:
        existing.refuse('nested input context substitution')
    source_io = source_io or existing
    if source_io is not None:
        source_io.check()
    token = _ACTIVE.set(source_io)
    quiet_token = _QUIET.set(quiet or _QUIET.get())
    try:
        yield
        if source_io is not None:
            source_io.check()
    finally:
        _ACTIVE.reset(token)
        _QUIET.reset(quiet_token)


def source_read_bytes(path):
    scope = current_source_io()
    return scope.read_bytes(path) if scope else Path(path).read_bytes()


def source_read_text(path, encoding=None, errors=None):
    scope = current_source_io()
    if scope is None:
        return Path(path).read_text(encoding=encoding, errors=errors)
    # Match text-mode universal-newline decoding.
    import io
    with io.TextIOWrapper(io.BytesIO(scope.read_bytes(path)), encoding=encoding or 'utf-8',
                          errors=errors) as stream:
        return stream.read()


def source_stat(path, *, follow_symlinks=True):
    scope = current_source_io()
    if scope is None:
        return Path(path).stat(follow_symlinks=follow_symlinks)
    info = scope.probe(path)
    if info is None:
        raise FileNotFoundError(path)
    return info


def source_resolve(path, strict=False):
    if current_source_io() is None:
        return Path(path).resolve(strict=strict)
    # Lexical reference normalization does not consume a target. Actual reads,
    # probes and enumeration enforce containment and reject symlinks.
    return Path(os.path.abspath(path))


def source_ancestors(path):
    path = Path(path)
    scope = current_source_io()
    for candidate in (path, *path.parents):
        yield candidate
        if scope is not None and candidate == scope.root:
            break


def at_source_root(path):
    scope = current_source_io()
    return scope is not None and Path(path) == scope.root


def engine_print(*args, **kwargs):
    if not _QUIET.get():
        builtins.print(*args, **kwargs)


def source_walk(root, *, followlinks=False, onerror=None):
    scope = current_source_io()
    if scope is None:
        yield from os.walk(root, followlinks=followlinks, onerror=onerror)
        return
    if followlinks:
        scope.refuse('scoped scans cannot follow symlinks')
    stack = [Path(root)]
    while stack:
        directory = stack.pop()
        entries = scope.listdir(directory)
        dirs = [path.name for path, mode in entries if stat.S_ISDIR(mode)]
        files = [path.name for path, mode in entries if not stat.S_ISDIR(mode)]
        yield str(directory), dirs, files
        stack.extend(directory / name for name in reversed(dirs))


def diagnostics_enabled():
    return not _QUIET.get()
