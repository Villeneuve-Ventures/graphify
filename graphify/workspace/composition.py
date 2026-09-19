"""Existing-only runtime authority inspection and structural composition boundary.

This is not a lifecycle implementation or installer. Constructors are pure; the
explicit loader only reads. No platform persistence or semantic modules import
here. S3 owns storage/capability qualification and S4 owns adapter execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import os
from pathlib import Path
import stat
import sysconfig

from .adapters import AdapterIntent, CompatibilityTuple, select_adapter
from .contracts import (CompatibilityManifest, ContractError, Document, canonical_json_bytes,
                        DISTRIBUTION_VERSION, INSTALLATION_METADATA, exact, integer)

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
        for parent, name, dev, ino in links:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (dev, ino):
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
    """
    import hashlib
    import graphify

    value = expected.to_dict()
    try:
        dist = metadata.distribution("graphifyy")
        if dist.version != value["distribution_version"]:
            raise WorkspaceAuthorityInvalid("installed package version mismatch")
        actual = {str(p): p for p in (dist.files or ()) if str(p).startswith("graphify/")
                  and "__pycache__" not in p.parts and not str(p).endswith(".pyc")}
        if set(actual) != set(value["package_members"]):
            raise WorkspaceAuthorityInvalid("installed package inventory mismatch")
        active = Path(graphify.__file__).resolve()
        if Path(str(dist.locate_file("graphify/__init__.py"))).resolve() != active:
            raise WorkspaceAuthorityInvalid("active import is not the authorized installed package")
        prefix = f"graphifyy-{DISTRIBUTION_VERSION}.dist-info/"
        owned = {str(p): p for p in (dist.files or ())}
        allowed_metadata = {prefix + name for name in (*INSTALLATION_METADATA, "RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json")}
        scripts = Path(sysconfig.get_path("scripts"))
        for name, member in owned.items():
            if name in actual or name in allowed_metadata:
                continue
            if name.startswith("graphify/") and "__pycache__" in member.parts and name.endswith(".pyc"):
                continue
            installed_path = Path(str(dist.locate_file(member))).resolve()
            if installed_path not in {scripts / "graphify", scripts / "graphify-mcp",
                                     scripts / "graphify.exe", scripts / "graphify-mcp.exe"}:
                raise WorkspaceAuthorityInvalid("unexpected installed distribution member")
        expected_files = dict(value["package_members"])
        expected_files.update({prefix + name: sha for name, sha in value["installation_metadata"].items()})
        for name, wanted in expected_files.items():
            if name not in owned:
                raise WorkspaceAuthorityInvalid("missing installed distribution metadata")
            path = Path(str(dist.locate_file(owned[name])))
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise WorkspaceAuthorityInvalid("unsafe installed package member")
            if hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
                raise WorkspaceAuthorityInvalid("installed package member mismatch")
    except metadata.PackageNotFoundError as exc:
        raise WorkspaceAuthorityInvalid("candidate distribution not installed") from exc


@dataclass(frozen=True)
class WorkspaceRuntimeInputs:
    state_root: Path
    authority: WorkspaceRuntimeAuthority
    expected: CompatibilityManifest

    def __post_init__(self):
        _absolute(self.state_root)
        if not isinstance(self.authority, WorkspaceRuntimeAuthority) or not isinstance(self.expected, CompatibilityManifest):
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
    if not isinstance(inputs, WorkspaceRuntimeInputs):
        raise WorkspaceAuthorityInvalid("explicit runtime inputs required")
    select_adapter(CompatibilityTuple(inputs.authority.compatibility),
                   expected=CompatibilityTuple(inputs.expected), intent=AdapterIntent.PROBE)
    return StructuralComposition(inputs)
