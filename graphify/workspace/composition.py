"""Existing-only runtime authority inspection and structural composition boundary.

This is not a lifecycle implementation or installer. Constructors are pure; the
explicit loader only reads. No platform persistence or semantic modules import
here. S3 owns storage/capability qualification and S4 owns adapter execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import csv
from email.parser import Parser
import io
from importlib import metadata
import os
from pathlib import Path
import stat
import sysconfig

from .adapters import AdapterIntent, CompatibilityTuple, select_adapter
from .contracts import (CompatibilityManifest, ContractError, Document, canonical_json_bytes,
                        DISTRIBUTION_VERSION, INSTALLATION_METADATA,
                        SUPPORTED_CONSOLE_SCRIPTS, exact, integer)

RUNTIME_AUTHORITY_FILENAME = "runtime-manifest.json"
RUNTIME_AUTHORITY_MAX_BYTES = 1024 * 1024


class WorkspaceAuthorityInvalid(ContractError):
    """Missing, malformed, mismatched or unsafe explicit authority."""


@dataclass(frozen=True)
class StructuralPolicy:
    """Explicit policy only; fixture values must not become operational defaults."""
    max_pending_tasks: int
    max_pending_bytes: int
    max_claimed_tasks: int
    max_generations: int
    max_payload_bytes: int

    def __post_init__(self):
        for value in self.to_dict().values():
            integer(value, minimum=1)
        if self.max_claimed_tasks > self.max_pending_tasks:
            raise ContractError("claim limit exceeds pending task limit")

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value):
        exact(value, cls.__dataclass_fields__)
        return cls(**value)


class WorkspaceRuntimeAuthority(Document):
    @staticmethod
    def validate(value):
        exact(value, {"contract", "format_version", "compatibility_manifest", "structural_policy"})
        if (value["contract"] != "graphify.workspace.runtime_authority.internal"
                or type(value["format_version"]) is not int or value["format_version"] != 2):
            raise WorkspaceAuthorityInvalid("unsupported runtime authority envelope")
        CompatibilityManifest.from_mapping(value["compatibility_manifest"])
        StructuralPolicy.from_mapping(value["structural_policy"])
        if len(canonical_json_bytes(value)) > RUNTIME_AUTHORITY_MAX_BYTES:
            raise WorkspaceAuthorityInvalid("runtime authority byte limit exceeded")

    @property
    def compatibility(self):
        return CompatibilityManifest.from_mapping(self.to_dict()["compatibility_manifest"])


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkspaceAuthorityInvalid("an explicit absolute root is required")
    return path


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_authority(state_root):
    """Walk existing ancestors without following links; never mkdir or repair.

    Ancestors may be root-owned/shared (e.g. /private/tmp); each must be owned by
    this user or root and non-writable by others unless sticky. The state root
    itself must be owned 0700 and its singular authority file owned 0600.
    """
    if (any(not hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "geteuid"))
            or not {os.open, os.stat}.issubset(os.supports_dir_fd)):
        raise WorkspaceAuthorityInvalid("existing-only descriptor inspection is unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    uid = os.geteuid()
    if uid == 0:
        raise WorkspaceAuthorityInvalid("elevated workspace authority inspection is unsupported")
    fds, links = [], []
    try:
        fd = os.open(state_root.anchor, flags | os.O_DIRECTORY)
        fds.append(fd)
        for part in state_root.parts[1:]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            fds.append(child)
            info = os.fstat(child)
            if info.st_uid not in {0, uid} or (stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise WorkspaceAuthorityInvalid("unsafe state ancestor")
            links.append((fd, part, info.st_dev, info.st_ino))
            fd = child
        root_info = os.fstat(fd)
        if root_info.st_uid != uid or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise WorkspaceAuthorityInvalid("state root must be owned and private 0700")
        file_fd = os.open(RUNTIME_AUTHORITY_FILENAME, flags, dir_fd=fd)
        fds.append(file_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != uid or before.st_nlink != 1
                or before.st_size > RUNTIME_AUTHORITY_MAX_BYTES):
            raise WorkspaceAuthorityInvalid("authority must be a bounded owned singular 0600 file")
        chunks, size = [], 0
        while True:
            chunk = os.read(file_fd, min(65536, RUNTIME_AUTHORITY_MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > RUNTIME_AUTHORITY_MAX_BYTES:
                raise WorkspaceAuthorityInvalid("authority byte limit exceeded")
        if (_file_identity(before) != _file_identity(os.fstat(file_fd))
                or _file_identity(before) != _file_identity(os.stat(
                    RUNTIME_AUTHORITY_FILENAME, dir_fd=fd, follow_symlinks=False))
                or size != before.st_size):
            raise WorkspaceAuthorityInvalid("authority changed while reading")
        for index, (parent, name, dev, ino) in enumerate(links):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (dev, ino):
                raise WorkspaceAuthorityInvalid("state ancestor changed while reading")
            if index == len(links) - 1:
                if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise WorkspaceAuthorityInvalid("state root changed while reading")
            elif (info.st_uid not in {0, uid}
                  or (stat.S_IMODE(info.st_mode) & 0o022
                      and not info.st_mode & stat.S_ISVTX)):
                raise WorkspaceAuthorityInvalid("state ancestor changed while reading")
        return b"".join(chunks)
    except OSError as exc:
        raise WorkspaceAuthorityInvalid("authority missing or unsafe") from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def verify_installed_candidate(expected):
    """Compare the explicit tuple with the active noneditable distribution's bytes.

    Wheel digest provenance belongs to the fixture builder; installed files are
    checked independently. A wheel's RECORD alone is not trusted as file content.
    Current-interpreter caches are compared by code fields and constant values,
    including unrecorded caches; immutable constant reference sharing is excluded.
    This is on-disk admission evidence, not proof of already imported code or
    protection against later writes, import hooks, or a hostile interpreter.
    """
    import hashlib
    import graphify

    if type(expected) is not CompatibilityManifest:
        raise WorkspaceAuthorityInvalid("expected a validated compatibility manifest")
    value = expected.to_dict()
    try:
        dist = metadata.distribution("graphifyy")
        prefix = f"graphifyy-{DISTRIBUTION_VERSION}.dist-info/"
        verified_metadata, captured_metadata = {}, {}
        for name in ("METADATA", "RECORD"):
            path = Path(str(dist.locate_file(prefix + name)))
            payload, resolved, identity = _read_installed_member(path)
            captured_metadata[name] = payload
            verified_metadata[path] = (resolved, identity)
        files = _parse_installed_metadata(captured_metadata["METADATA"],
                                          captured_metadata["RECORD"],
                                          value["distribution_version"])
        actual = {str(p): p for p in files if str(p).startswith("graphify/")
                  and "__pycache__" not in p.parts and not str(p).endswith(".pyc")}
        if set(actual) != set(value["package_members"]):
            raise WorkspaceAuthorityInvalid("installed package inventory mismatch")
        active = Path(graphify.__file__).resolve()
        if Path(str(dist.locate_file("graphify/__init__.py"))).resolve() != active:
            raise WorkspaceAuthorityInvalid("active import is not the authorized installed package")
        owned = {str(p): p for p in files}
        allowed_metadata = {prefix + name for name in (*INSTALLATION_METADATA, "RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json")}
        scripts = Path(sysconfig.get_path("scripts")).resolve()
        script_paths = {scripts / (name + suffix): name
                        for name in SUPPORTED_CONSOLE_SCRIPTS for suffix in ("", ".exe")}
        verified_scripts, seen_script_names = {}, set()
        for name, member in owned.items():
            if name in actual or name in allowed_metadata:
                continue
            if name.startswith("graphify/") and "__pycache__" in member.parts and name.endswith(".pyc"):
                continue
            installed_path = Path(str(dist.locate_file(member)))
            try:
                before = installed_path.lstat()
                resolved = installed_path.resolve(strict=True)
                after = installed_path.lstat()
            except OSError as exc:
                raise WorkspaceAuthorityInvalid("recorded console script missing or unsafe") from exc
            if (resolved not in script_paths or not stat.S_ISREG(before.st_mode)
                    or _file_identity(before) != _file_identity(after)):
                raise WorkspaceAuthorityInvalid("unexpected installed distribution member")
            seen_script_names.add(script_paths[resolved])
            verified_scripts[installed_path] = (resolved, _file_identity(before))
        if seen_script_names != SUPPORTED_CONSOLE_SCRIPTS:
            raise WorkspaceAuthorityInvalid("missing declared console script inventory")
        expected_files = dict(value["package_members"])
        expected_files.update({prefix + name: sha for name, sha in value["installation_metadata"].items()})
        verified_package_files, verified_caches, verified_cache_files = {}, {}, {}
        for name, wanted in expected_files.items():
            if name not in owned:
                raise WorkspaceAuthorityInvalid("missing installed distribution metadata")
            path = Path(str(dist.locate_file(owned[name])))
            if name == prefix + "METADATA":
                source = captured_metadata["METADATA"]
                resolved, identity = verified_metadata[path]
            else:
                source, resolved, identity = _read_installed_member(path)
            if hashlib.sha256(source).hexdigest() != wanted:
                raise WorkspaceAuthorityInvalid("installed package member mismatch")
            if name.startswith("graphify/"):
                verified_package_files[resolved] = identity
            else:
                previous = verified_metadata.get(path)
                if previous is not None and previous != (resolved, identity):
                    raise WorkspaceAuthorityInvalid("installed metadata changed while parsing")
                verified_metadata[path] = (resolved, identity)
            if name.endswith(".py"):
                tree_caches, captured_caches = _verify_source_caches(path, source)
                verified_caches.update(tree_caches)
                verified_cache_files.update(captured_caches)
        _verify_package_tree(active.parent, verified_package_files, verified_caches)
        _verify_captured_files(verified_metadata, "metadata")
        _verify_captured_files(verified_cache_files, "bytecode cache")
        _verify_captured_files(verified_scripts, "console script")
    except metadata.PackageNotFoundError as exc:
        raise WorkspaceAuthorityInvalid("candidate distribution not installed") from exc


def _parse_installed_metadata(metadata_payload, record_payload, expected_version):
    """Parse only the bounded captures, never Distribution's rereading properties."""
    try:
        headers = Parser().parsestr(metadata_payload.decode("utf-8"), headersonly=True)
        record_text = record_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceAuthorityInvalid("installed metadata is not UTF-8") from exc
    if headers.defects or headers.get_all("Version", []) != [expected_version]:
        raise WorkspaceAuthorityInvalid("installed package version mismatch or malformed metadata")
    files, seen = [], set()
    try:
        for row in csv.reader(io.StringIO(record_text, newline=""), strict=True):
            if len(row) != 3 or not row[0] or "\x00" in row[0]:
                raise WorkspaceAuthorityInvalid("malformed installed RECORD row")
            name, recorded_hash, size = row
            member = metadata.PackagePath(name)
            if str(member) in seen:
                raise WorkspaceAuthorityInvalid("duplicate installed RECORD path")
            # RECORD hash/size fields are optional (not authority for content).
            # Validate their syntax without converting attacker-controlled integers.
            if size and (not size.isascii() or not size.isdecimal()):
                raise WorkspaceAuthorityInvalid("malformed installed RECORD size")
            if recorded_hash:
                algorithm, separator, encoded = recorded_hash.partition("=")
                if not separator or not algorithm or not encoded:
                    raise WorkspaceAuthorityInvalid("malformed installed RECORD hash")
            seen.add(str(member))
            files.append(member)
    except csv.Error as exc:
        raise WorkspaceAuthorityInvalid("malformed installed RECORD CSV") from exc
    return tuple(files)


def _read_installed_member(path):
    """Capture bounded member bytes and the exact filesystem identity hashed."""
    descriptor = None
    limit = 64 * 1024 * 1024
    try:
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode) or named.st_size > limit:
            raise WorkspaceAuthorityInvalid("unsafe installed package member")
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (_file_identity(named) != _file_identity(opened)
                or not stat.S_ISREG(opened.st_mode) or opened.st_size > limit):
            raise WorkspaceAuthorityInvalid("installed package member changed")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise WorkspaceAuthorityInvalid("installed package member exceeds byte limit")
        after = os.fstat(descriptor)
        resolved = path.resolve(strict=True)
        if (_file_identity(opened) != _file_identity(after)
                or _file_identity(opened) != _file_identity(path.lstat())
                or size != opened.st_size):
            raise WorkspaceAuthorityInvalid("installed package member changed")
        return b"".join(chunks), resolved, _file_identity(opened)
    except OSError as exc:
        raise WorkspaceAuthorityInvalid("installed package member unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_package_tree(package_root, verified_files, verified_caches):
    """Refuse executable or data entries omitted from the wheel-derived inventory."""
    allowed = {**verified_files, **verified_caches}
    required = set(verified_files)
    seen = set()
    try:
        for path in package_root.rglob("*"):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceAuthorityInvalid("unsafe installed package tree entry")
            resolved = path.resolve(strict=True)
            if _file_identity(info) != _file_identity(path.lstat()):
                raise WorkspaceAuthorityInvalid("installed package tree changed")
            if resolved not in allowed:
                raise WorkspaceAuthorityInvalid("unrecorded installed package member")
            if allowed[resolved] is not None and _file_identity(info) != allowed[resolved]:
                raise WorkspaceAuthorityInvalid("installed package member changed after verification")
            seen.add(resolved)
        if not required <= seen:
            raise WorkspaceAuthorityInvalid("installed package member disappeared after verification")
    except OSError as exc:
        raise WorkspaceAuthorityInvalid("installed package tree unreadable") from exc


def _verify_captured_files(verified, kind):
    """Require non-package files to retain the exact named and resolved identities read."""
    try:
        for path, (resolved, identity) in verified.items():
            before = path.lstat()
            if (path.resolve(strict=True) != resolved
                    or _file_identity(before) != identity
                    or _file_identity(path.lstat()) != identity):
                raise WorkspaceAuthorityInvalid(
                    f"installed {kind} changed after verification")
    except OSError as exc:
        raise WorkspaceAuthorityInvalid(f"installed {kind} unreadable") from exc


def _verify_source_caches(path, source):
    """Inspect executable caches; deserialize only in a bounded child interpreter.

    cache_from_source follows sys.pycache_prefix, just like the source loader.
    Other interpreter tags and legacy caches beside present .py files are not
    selected by this loader. Nonstandard compiler/filename caches are explicitly
    refused rather than silently treated as authorized. Never rewrite a cache.
    """
    from importlib.util import MAGIC_NUMBER, cache_from_source
    import base64
    import sys

    caches, verified, captured = [], {}, {}
    limit = 64 * 1024 * 1024
    # Resolve symlinked installation ancestors consistently, while supporting
    # caches created with either the installation spelling or its real path.
    filenames = tuple(dict.fromkeys((str(path), str(path.resolve()))))
    if sys.pycache_prefix is not None:
        # With an external prefix, the source loader does not select ordinary
        # adjacent __pycache__ entries. Permit only the canonical cache names
        # belonging to an authorized source; the package-tree walk still rejects
        # arbitrary .pyc shadow packages and non-regular entries.
        tag = sys.implementation.cache_tag
        for filename in filenames:
            source_path = Path(filename)
            for optimize in (0, 1, 2):
                suffix = f".opt-{optimize}" if optimize else ""
                verified[(source_path.parent / "__pycache__" /
                          f"{source_path.stem}.{tag}{suffix}.pyc").resolve()] = None
    for filename in filenames:
        for optimize in (0, 1, 2):
            cache = Path(cache_from_source(filename, optimization=str(optimize) if optimize else ""))
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                info = cache.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WorkspaceAuthorityInvalid("installed bytecode cache unreadable") from exc
            try:
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise WorkspaceAuthorityInvalid("unsafe installed bytecode cache")
                fd = os.open(cache, flags)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if _file_identity(info) != _file_identity(opened):
                        raise WorkspaceAuthorityInvalid("installed bytecode cache changed")
                    payload = stream.read(limit + 1)
                    if (len(payload) > limit
                            or _file_identity(opened) != _file_identity(os.fstat(stream.fileno()))
                            or _file_identity(opened) != _file_identity(cache.lstat())):
                        raise WorkspaceAuthorityInvalid("installed bytecode cache changed")
                    resolved = cache.resolve(strict=True)
            except OSError as exc:
                raise WorkspaceAuthorityInvalid("installed bytecode cache unreadable") from exc
            if (len(payload) < 16 or payload[:4] != MAGIC_NUMBER
                    or int.from_bytes(payload[4:8], "little") not in (0, 1, 3)):
                raise WorkspaceAuthorityInvalid("unverified installed bytecode cache header")
            caches.append([optimize, base64.b64encode(payload[16:]).decode("ascii")])
            verified[resolved] = _file_identity(opened)
            captured[cache] = (resolved, _file_identity(opened))
    if caches:
        _compare_cached_code(source, filenames, caches)
    return verified, captured


# CPython code equality omits some fields. Compare every serialized code field,
# recursively, preserving constant types and float bits. Marshal string interning
# and reference flags vary across ordinary compilation history. This compares
# code fields and immutable constant types/values, not constant object sharing
# or observational equivalence of identity-sensitive programs.
_CACHE_COMPARISON = r"""
import base64, io, json, marshal, struct, sys, types
try:
    # Windows has no resource module. Darwin rejects a useful address-space
    # cap for this interpreter. Input/wall bounds apply on every platform.
    if sys.platform != 'win32':
        import resource
        if sys.platform != 'darwin':
            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    fields = (
        'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals',
        'co_stacksize', 'co_flags', 'co_code', 'co_consts', 'co_names',
        'co_varnames', 'co_filename', 'co_name', 'co_qualname', 'co_firstlineno',
        'co_linetable', 'co_exceptiontable', 'co_freevars', 'co_cellvars',
    )
    def identity(value):
        kind = type(value)
        if kind is types.CodeType:
            return ('code', tuple(identity(getattr(value, field)) for field in fields))
        if kind is tuple:
            return ('tuple', tuple(identity(item) for item in value))
        if kind is frozenset:
            return ('frozenset', frozenset(identity(item) for item in value))
        if kind is slice:
            return ('slice', identity(value.start), identity(value.stop), identity(value.step))
        if kind is float:
            return ('float', struct.pack('>d', value))
        if kind is complex:
            return ('complex', struct.pack('>dd', value.real, value.imag))
        if kind in (type(None), type(Ellipsis), bool, int, str, bytes):
            return (kind.__name__, value)
        raise ValueError('unsupported code constant')
    raw = sys.stdin.buffer.read(256 * 1024 * 1024 + 1)
    if len(raw) > 256 * 1024 * 1024:
        raise ValueError('comparison input limit')
    request = json.loads(raw)
    source = base64.b64decode(request['source'], validate=True)
    expected = {}
    for optimize, encoded in request['caches']:
        stream = io.BytesIO(base64.b64decode(encoded, validate=True))
        cached = marshal.load(stream)
        if type(cached) is not types.CodeType or stream.read(1):
            raise ValueError('invalid cache payload')
        if optimize not in expected:
            expected[optimize] = []
            for filename in request['filenames']:
                code = compile(source, filename, 'exec', dont_inherit=True, optimize=optimize)
                # Model the loader's reconstruction, not compiler object reuse.
                expected[optimize].append(identity(marshal.loads(marshal.dumps(code))))
        if identity(cached) not in expected[optimize]:
            raise ValueError('code mismatch')
    sys.stdout.write('verified')
except BaseException:
    sys.exit(1)
"""


def _compare_cached_code(source, filenames, caches):
    """A disposable interpreter, not an OS sandbox; cached code is never executed.

    Deserialization is outside the admitting process, with input/wall limits,
    POSIX CPU limits, and an address-space cap except on Darwin/Windows.
    Malformed caches fail closed.
    No provider environment, site initialization, or bytecode writes are needed.
    """
    import base64
    import json
    import subprocess
    import sys

    # This is a private transport, not a canonical contract document: filenames
    # must retain their exact Unicode spelling for co_filename comparison.
    request = json.dumps({"source": base64.b64encode(source).decode("ascii"),
                          "filenames": filenames, "caches": caches}).encode("utf-8")
    if len(request) > 256 * 1024 * 1024:
        raise WorkspaceAuthorityInvalid("installed bytecode comparison input limit exceeded")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", _CACHE_COMPARISON],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceAuthorityInvalid("installed bytecode comparison unavailable") from exc
    if result.returncode != 0 or result.stdout != b"verified":
        raise WorkspaceAuthorityInvalid("installed bytecode cache could not be verified against authorized source")



@dataclass(frozen=True)
class WorkspaceRuntimeInputs:
    state_root: Path
    authority: WorkspaceRuntimeAuthority
    expected: CompatibilityManifest

    def __post_init__(self):
        _absolute(self.state_root)
        if (type(self.authority) is not WorkspaceRuntimeAuthority
                or type(self.expected) is not CompatibilityManifest):
            raise WorkspaceAuthorityInvalid("validated explicit authority is required")
        if self.authority.compatibility != self.expected:
            raise WorkspaceAuthorityInvalid("runtime authority candidate mismatch")


def load_workspace_runtime_inputs(*, state_root, expected):
    """No environment defaults, no synthesized authority, no state creation."""
    root = _absolute(state_root)
    payload = _read_authority(root)
    authority = WorkspaceRuntimeAuthority.from_json(payload)
    inputs = WorkspaceRuntimeInputs(root, authority, expected)
    verify_installed_candidate(expected)
    return inputs


@dataclass(frozen=True)
class StructuralComposition:
    inputs: WorkspaceRuntimeInputs

    def require_runtime(self):
        raise WorkspaceAuthorityInvalid("S3 stores and S4 operational adapter are not implemented")


def compose_workspace_runtime(inputs):
    """Validate explicit contract inputs and return a pure, non-operational plan."""
    if type(inputs) is not WorkspaceRuntimeInputs:
        raise WorkspaceAuthorityInvalid("explicit runtime inputs required")
    select_adapter(CompatibilityTuple(inputs.authority.compatibility),
                   expected=CompatibilityTuple(inputs.expected), intent=AdapterIntent.PROBE)
    return StructuralComposition(inputs)
