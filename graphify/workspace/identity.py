"""Read-only source discovery and operator authorization for workspace identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import os
from pathlib import Path, PosixPath, PurePath
import re
import selectors
import stat
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from graphify.source_io import SourceError, SourceIO
from graphify.workspace.lifecycle_contracts import (
    ContractError,
    WorkspaceConfig,
    canonical_registry_source,
    canonical_sha256,
)


WORKSPACE_CONFIG_MAX_BYTES = 1024 * 1024
GIT_OUTPUT_MAX_BYTES = 1024 * 1024
# Set only by the read-only worker bootstrap from its parent's selected helper.
_PINNED_GIT_EXECUTABLE: str | None = None


_RFC3339_UTC = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?Z$"
)


class IdentityError(RuntimeError):
    """Base class for stable identity failures."""

    code = "identity_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class AuthorizationError(IdentityError):
    code = "authorization_error"


class UUIDCollisionError(IdentityError):
    code = "uuid_collision"


class SourceAmbiguousError(IdentityError):
    code = "source_ambiguous"


class SourceDiscoveryError(IdentityError):
    code = "source_discovery_error"


class SourceDiscoveryTimeout(SourceDiscoveryError):
    code = "source_discovery_timeout"


class IdentityAction(str, Enum):
    ENROLL = "ENROLL"
    ADOPT = "ADOPT"
    ROTATE = "ROTATE"
    REBIND = "REBIND"
    ACTIVATE = "ACTIVATE"
    ROLLBACK = "ROLLBACK"
    GC_EXECUTE = "GC_EXECUTE"
    REPAIR_EXECUTE = "REPAIR_EXECUTE"
    GC_RECONCILE = "GC_RECONCILE"
    GC_PURGE = "GC_PURGE"


@dataclass(frozen=True)
class OperatorAuthorization:
    """Explicit, content-addressed operator approval for one identity action."""

    action: IdentityAction
    operator_id: str
    reason: str
    issued_at: str
    nonce: str

    def __post_init__(self) -> None:
        for field_name in ("operator_id", "reason", "nonce"):
            value = getattr(self, field_name)
            if not value or value.strip() != value:
                raise AuthorizationError(f"{field_name} must be non-empty and trimmed")
        if _RFC3339_UTC.fullmatch(self.issued_at) is None:
            raise AuthorizationError("issued_at must be an RFC 3339 UTC timestamp")
        try:
            datetime.fromisoformat(self.issued_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AuthorizationError("issued_at must name a real calendar timestamp") from exc

    def require(self, expected: IdentityAction) -> None:
        if self.action is not expected:
            raise AuthorizationError(
                f"{expected.value} requires matching operator authorization, "
                f"got {self.action.value}"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action.value,
            "issued_at": self.issued_at,
            "nonce": self.nonce,
            "operator_id": self.operator_id,
            "reason": self.reason,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class SourceIdentity:
    """Read-only source facts used to construct frozen registry records."""

    root: Path
    repo_uuid: str
    registry_source: dict[str, Any]
    source_sha256: str
    head_commit: str
    history_roots: tuple[str, ...]
    config_sha256: str
    git_common_device: int
    git_common_inode: int
    remote_evidence: tuple[dict[str, str], ...]

    def evidence(self) -> dict[str, Any]:
        return {
            "config_sha256": self.config_sha256,
            "git_common_device": self.git_common_device,
            "git_common_inode": self.git_common_inode,
            "head_commit": self.head_commit,
            "history_roots": list(self.history_roots),
            "repo_uuid": self.repo_uuid,
            "source": self.registry_source,
            "source_sha256": self.source_sha256,
        }


def _remaining_timeout_seconds(deadline_ns: int | None) -> float | None:
    if deadline_ns is None:
        return None
    remaining_ns = deadline_ns - time.monotonic_ns()
    if remaining_ns <= 0:
        raise SourceDiscoveryTimeout("source discovery deadline expired")
    return remaining_ns / 1_000_000_000


def _check_deadline(deadline_ns: int | None) -> None:
    _remaining_timeout_seconds(deadline_ns)


def _git_path_is_relative_to(path: PurePath, parent: PurePath) -> bool:
    # Exact POSIX paths have case-sensitive lexical component comparisons.
    # Keep pathlib behavior for other flavours and caller-defined subclasses.
    if type(path) is PosixPath and type(parent) is PosixPath:
        parent_parts = parent.parts
        return path.anchor == parent.anchor and path.parts[:len(parent_parts)] == parent_parts
    return path.is_relative_to(parent)


def _git_search_path(root: Path, git_common_dir: Path) -> str:
    """Keep operator installation paths; never search relative to the source."""
    root = root.resolve(strict=True)
    directories = []
    for entry in os.get_exec_path():
        if not os.path.isabs(entry):
            continue
        directory = Path(os.path.abspath(entry))
        try:
            resolved = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if any(_git_path_is_relative_to(directory, denied) or _git_path_is_relative_to(resolved, denied)
               for denied in (root, git_common_dir)):
            continue
        if resolved.is_dir():
            directories.append(str(resolved))
    return os.pathsep.join(dict.fromkeys(directories))


def _git_executable(root: Path, *, deadline_ns: int | None = None,
                    git_common_dir: Path | None = None) -> str:
    """Select an absolute Git outside source authority, or reuse the worker pin."""
    root = root.resolve(strict=True)
    if git_common_dir is None:
        _git_dir, git_common_dir = _git_routing(root, deadline_ns=deadline_ns)
    candidates = (
        [_PINNED_GIT_EXECUTABLE] if _PINNED_GIT_EXECUTABLE is not None else
        [str(Path(entry) / "git")
         for entry in _git_search_path(root, git_common_dir).split(os.pathsep) if entry]
    )
    for candidate in candidates:
        _check_deadline(deadline_ns)
        path = Path(candidate)
        try:
            resolved = path.resolve(strict=True)
            if (path.is_absolute() and not any(
                    _git_path_is_relative_to(path, denied) or _git_path_is_relative_to(resolved, denied)
                    for denied in (root, git_common_dir))
                    and resolved.is_file() and os.access(resolved, os.X_OK)):
                return str(resolved)
        except (OSError, RuntimeError):
            continue
    raise SourceDiscoveryError("no eligible Git executable outside source root")


def _git(
    root: Path,
    *arguments: str,
    deadline_ns: int | None = None,
    strip_output: bool = True,
    inspect_object_tree: bool = True,
) -> str:
    git_common_dir = _preflight_git_inputs(
        root, deadline_ns=deadline_ns, inspect_object_tree=inspect_object_tree,
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    command = [_git_executable(root, deadline_ns=deadline_ns,
                               git_common_dir=git_common_dir), *arguments]
    environment["PATH"] = _git_search_path(root, git_common_dir)
    _check_deadline(deadline_ns)
    with subprocess.Popen(
        command, cwd=root, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        stdout = bytearray()
        total_bytes = 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    events = selector.select(_remaining_timeout_seconds(deadline_ns))
                    _check_deadline(deadline_ns)
                    for key, _events in events:
                        chunk = os.read(key.fd, min(65536, GIT_OUTPUT_MAX_BYTES - total_bytes + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > GIT_OUTPUT_MAX_BYTES:
                            raise SourceDiscoveryError("Git output exceeds byte limit")
                        if key.fileobj is process.stdout:
                            stdout.extend(chunk)
            process.wait(timeout=_remaining_timeout_seconds(deadline_ns))
            _check_deadline(deadline_ns)
        except subprocess.TimeoutExpired:
            raise SourceDiscoveryTimeout("source discovery deadline expired") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode != 0:
            # Git diagnostics can contain credentials from repository configuration.
            raise SourceDiscoveryError(f"Git command failed with status {process.returncode}")
        try:
            decoded = stdout.decode("utf-8")
            return decoded.strip() if strip_output else decoded
        except UnicodeDecodeError:
            raise SourceDiscoveryError("Git output is not valid UTF-8") from None



def _normalize_remote(raw: str) -> str:
    value = raw.strip()
    if "://" not in value:
        match = re.fullmatch(r"(?P<user>[^@/:\s]+)@(?P<host>[^:/\s]+):(?P<path>.+)", value)
        if match is None:
            raise SourceDiscoveryError("unsupported remote URL")
        value = (
            f"ssh://{match.group('user')}@{match.group('host')}/{match.group('path').lstrip('/')}"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SourceDiscoveryError("invalid remote URL") from None
    if parsed.scheme.lower() not in {"https", "ssh"} or not parsed.hostname:
        raise SourceDiscoveryError("workspace remotes must use https:// or ssh://")
    if parsed.password is not None or port is not None or parsed.query or parsed.fragment:
        raise SourceDiscoveryError(
            "remote credentials, ports, queries, and fragments are forbidden"
        )
    if parsed.scheme.lower() == "https" and parsed.username is not None:
        raise SourceDiscoveryError("HTTPS workspace remotes must not contain userinfo")
    path = "/" + parsed.path.lstrip("/").rstrip("/")
    if path == "/":
        raise SourceDiscoveryError("remote repository path is empty")
    host = parsed.hostname.lower()
    userinfo = f"{parsed.username}@" if parsed.username is not None else ""
    return urlunsplit((parsed.scheme.lower(), f"{userinfo}{host}", path, "", ""))


def _resolve_git_path(
    root: Path,
    value: str,
    *,
    deadline_ns: int | None = None,
) -> Path:
    _check_deadline(deadline_ns)
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=True)
    _check_deadline(deadline_ns)
    return resolved


def _read_source_regular(
    root: Path,
    relative: Path,
    *,
    deadline_ns: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0
    ):
        raise ValueError("max_bytes must be a positive integer")
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("source identity path must be a contained relative path")
    _check_deadline(deadline_ns)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    path = root / relative
    directory_descriptors: list[int] = []
    directory_bindings: list[tuple[int, str, int, Path]] = []
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise SourceDiscoveryError(
            f"cannot open source identity directory {root}: {exc}"
        ) from exc
    directory_descriptors.append(root_descriptor)
    try:
        current_descriptor = root_descriptor
        current_path = root
        for part in relative.parent.parts:
            _check_deadline(deadline_ns)
            try:
                child_descriptor = os.open(
                    part,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
            except OSError as exc:
                raise SourceDiscoveryError(
                    f"cannot open source identity directory {current_path / part}: {exc}"
                ) from exc
            directory_descriptors.append(child_descriptor)
            try:
                child = os.fstat(child_descriptor)
            except OSError as exc:
                raise SourceDiscoveryError(
                    f"cannot inspect source identity directory {current_path / part}: {exc}"
                ) from exc
            if not stat.S_ISDIR(child.st_mode):
                raise SourceDiscoveryError(
                    f"source identity path is not a directory: {current_path / part}"
                )
            directory_bindings.append(
                (current_descriptor, part, child_descriptor, current_path / part)
            )
            current_descriptor = child_descriptor
            current_path /= part

        try:
            descriptor = os.open(relative.name, file_flags, dir_fd=current_descriptor)
        except OSError as exc:
            raise SourceDiscoveryError(
                f"cannot open source identity file {path}: {exc}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SourceDiscoveryError(
                    f"source identity file is not a singular regular file: {path}"
                )
            if max_bytes is not None and before.st_size > max_bytes:
                raise SourceDiscoveryError(
                    f"source identity file exceeds byte limit {max_bytes}: {path}"
                )
            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                _check_deadline(deadline_ns)
                try:
                    read_size = (
                        1024 * 1024
                        if max_bytes is None
                        else min(1024 * 1024, max_bytes - total_bytes + 1)
                    )
                    chunk = os.read(descriptor, read_size)
                except InterruptedError:
                    continue
                if not chunk:
                    break
                total_bytes += len(chunk)
                if max_bytes is not None and total_bytes > max_bytes:
                    raise SourceDiscoveryError(
                        f"source identity file exceeds byte limit {max_bytes}: {path}"
                    )
                chunks.append(chunk)
            _check_deadline(deadline_ns)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise SourceDiscoveryError(f"source identity file changed while it was read: {path}")
        try:
            installed = os.stat(
                relative.name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SourceDiscoveryError(
                f"source identity file disappeared after read: {path}"
            ) from exc
        if (
            not stat.S_ISREG(installed.st_mode)
            or installed.st_dev != after.st_dev
            or installed.st_ino != after.st_ino
        ):
            raise SourceDiscoveryError(f"source identity file was replaced while it was read: {path}")
        for parent_descriptor, name, child_descriptor, child_path in reversed(
            directory_bindings
        ):
            opened = os.fstat(child_descriptor)
            try:
                bound = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise SourceDiscoveryError(
                    f"source identity directory disappeared after read: {child_path}"
                ) from exc
            if (
                not stat.S_ISDIR(bound.st_mode)
                or (opened.st_dev, opened.st_ino) != (bound.st_dev, bound.st_ino)
            ):
                raise SourceDiscoveryError(
                    f"source identity directory changed while it was read: {child_path}"
                )
        root_opened = os.fstat(root_descriptor)
        try:
            root_bound = root.lstat()
        except OSError as exc:
            raise SourceDiscoveryError(
                f"source identity root disappeared after read: {root}"
            ) from exc
        if (
            not stat.S_ISDIR(root_bound.st_mode)
            or (root_opened.st_dev, root_opened.st_ino)
            != (root_bound.st_dev, root_bound.st_ino)
        ):
            raise SourceDiscoveryError(f"source identity root changed while it was read: {root}")
        return b"".join(chunks)
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)


def _preflight_git_refs(
    git_dir: Path, common: Path, head: bytes, *, deadline_ns: int | None,
) -> None:
    """Check Git's selected loose-ref chain and packed-ref route without following links."""
    extra_roots = {"worktree": git_dir} if git_dir != common else None
    with SourceIO(
        common, extra_roots=extra_roots,
        max_file_bytes=WORKSPACE_CONFIG_MAX_BYTES,
        max_total_bytes=9 * WORKSPACE_CONFIG_MAX_BYTES,
    ) as inputs:
        packed = inputs.probe(common / "packed-refs")
        if packed is not None and not stat.S_ISREG(packed.st_mode):
            raise SourceDiscoveryError("unsafe Git packed references")
        seen: set[bytes] = set()
        while True:
            _check_deadline(deadline_ns)
            head = head.strip()
            if not head.startswith(b"ref:"):
                if re.fullmatch(rb"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", head) is None:
                    raise SourceDiscoveryError("unsupported Git reference")
                return
            # Git accepts tabs, repeated spaces and newlines after ``ref:``.
            # Normalize only its surrounding ASCII whitespace; reject control
            # bytes inside the selected path before any Git process starts.
            ref = head[4:].strip()
            parts = ref.split(b"/")
            if (
                len(seen) >= 8 or ref in seen or len(parts) < 2
                or parts[0] != b"refs" or any(
                    not part or part in {b".", b".."}
                    or b"\\" in part or any(byte < 32 or byte == 127 for byte in part)
                    for part in parts
                )
            ):
                raise SourceDiscoveryError("unsupported Git reference")
            seen.add(ref)
            relative = Path(os.fsdecode(ref))
            # Standard refs are shared by linked worktrees. Per-worktree
            # namespaces live in the linked Git directory.
            local_ref = ref.startswith((b"refs/worktree/", b"refs/bisect/", b"refs/rewritten/"))
            selected = git_dir if local_ref else common
            loose: dict[Path, bytes] = {}
            for directory in (common, git_dir) if git_dir != common else (common,):
                path = directory
                missing = False
                for part in parts[:-1]:
                    _check_deadline(deadline_ns)
                    path /= os.fsdecode(part)
                    info = inputs.probe(path)
                    if info is None:
                        missing = True
                        break
                    if not stat.S_ISDIR(info.st_mode):
                        raise SourceDiscoveryError("unsafe Git reference directory")
                if missing:
                    continue
                path = directory / relative
                info = inputs.probe(path)
                if info is None:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise SourceDiscoveryError("unsafe Git loose reference")
                if directory == selected:
                    loose[directory] = inputs.read_bytes(
                        path, max_bytes=WORKSPACE_CONFIG_MAX_BYTES,
                    )
            if selected not in loose:
                # An absent loose ref may be packed (or an unborn branch).
                return
            head = loose[selected]


def _git_routing(root: Path, *, deadline_ns: int | None) -> tuple[Path, Path]:
    """Admit ordinary or linked routing without reading Git refs or objects."""
    def read(directory: Path, name: str) -> bytes:
        return _read_source_regular(directory, Path(name), deadline_ns=deadline_ns,
                                    max_bytes=WORKSPACE_CONFIG_MAX_BYTES)

    _check_deadline(deadline_ns)
    try:
        marker = root / ".git"
        if stat.S_ISDIR(marker.lstat().st_mode):
            git_dir = marker
            # An ordinary checkout owns its common directory. Git must not
            # follow an injected commondir into a separate metadata store.
            try:
                (git_dir / "commondir").lstat()
            except FileNotFoundError:
                pass
            else:
                raise SourceDiscoveryError("unsupported Git routing")
            common = git_dir
        else:
            routing = read(root, ".git").decode("utf-8").strip()
            if not routing.startswith("gitdir: "):
                raise SourceDiscoveryError("unsupported Git routing")
            git_dir = Path(os.path.abspath(root / routing[8:]))
            if git_dir.parent.name != "worktrees" or not git_dir.name:
                raise SourceDiscoveryError("unsupported linked Git routing")
            common = git_dir.parent.parent
            # Route components and the selected source must be real directories;
            # a symlink here could make the lexical worktree shape misleading.
            for directory in (root, common, git_dir.parent, git_dir):
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise SourceDiscoveryError("unsupported linked Git routing")
            if git_dir.resolve(strict=True) != git_dir:
                raise SourceDiscoveryError("unsupported linked Git routing")
            cd = read(git_dir, "commondir").decode("utf-8").strip()
            if Path(os.path.abspath(git_dir / cd)) != common:
                raise SourceDiscoveryError("unsupported linked Git common directory")
            back = read(git_dir, "gitdir").decode("utf-8").strip()
            if Path(os.path.abspath(git_dir / back)) != marker:
                raise SourceDiscoveryError("unsupported linked Git backlink")
        return git_dir, common
    except (OSError, UnicodeError, SourceError) as exc:
        raise SourceDiscoveryError("cannot preflight local Git routing") from exc


def _preflight_git_inputs(
    root: Path, *, deadline_ns: int | None, inspect_object_tree: bool = True,
) -> Path:
    """Refuse external config and object readers before any Git command starts."""
    def read(directory: Path, name: str) -> bytes:
        return _read_source_regular(directory, Path(name), deadline_ns=deadline_ns,
                                    max_bytes=WORKSPACE_CONFIG_MAX_BYTES)

    try:
        git_dir, common = _git_routing(root, deadline_ns=deadline_ns)
        # Even rev-parse opens HEAD. Reject FIFOs, symlinks and oversized inputs
        # before a subprocess can block on them, including linked-worktree HEADs.
        head = read(git_dir, "HEAD")
        _preflight_git_refs(git_dir, common, head, deadline_ns=deadline_ns)
        # Probe through no-follow descriptors. Presence alone is unsupported;
        # never open an alternate-store file (which could itself be a FIFO).
        with SourceIO(common) as inputs:
            # A missing leaf is meaningful only beneath admitted directories.
            # SourceIO probes can report ENOTDIR as absence, including a refused
            # symlink ancestor; Git itself would follow that object-store route.
            for relative in ("objects", "objects/info", "objects/pack"):
                info = inputs.probe(common / relative)
                if info is None and relative != "objects":
                    continue
                if info is None or not stat.S_ISDIR(info.st_mode):
                    raise SourceDiscoveryError("unsafe Git object directory")
            for name in ("alternates", "http-alternates"):
                if inputs.probe(common / "objects" / "info" / name) is not None:
                    raise SourceDiscoveryError("Git alternate object stores are unsupported")
            # Git also follows pack/loose-object paths. Validate their whole
            # bounded directory tree, without reading object contents, so a
            # lower symlink cannot recreate the same external-reader escape.
            if inspect_object_tree:
                pending = [common / "objects"]
                while pending:
                    _check_deadline(deadline_ns)
                    for path, mode in inputs.listdir(pending.pop()):
                        _check_deadline(deadline_ns)
                        if stat.S_ISDIR(mode):
                            pending.append(path)
                        elif not stat.S_ISREG(mode):
                            raise SourceDiscoveryError("unsafe Git object tree entry")
        for directory, name in ((common, "config"), (git_dir, "config.worktree")):
            try:
                (directory / name).lstat()
            except FileNotFoundError:
                continue
            raw = read(directory, name).removeprefix(b"\xef\xbb\xbf")
            if re.search(rb'(?im)^\s*\[\s*include(?:if)?(?:\s|\]|\.)', raw):
                raise SourceDiscoveryError("Git includes require unsupported external input authority")
    except (OSError, UnicodeError, SourceError) as exc:
        raise SourceDiscoveryError("cannot preflight local Git inputs") from exc
    return common


def _read_workspace_config(
    root: Path,
    *,
    deadline_ns: int | None = None,
    max_bytes: int | None = None,
) -> tuple[WorkspaceConfig, bytes]:
    if max_bytes is None:
        max_bytes = WORKSPACE_CONFIG_MAX_BYTES
    elif isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    else:
        max_bytes = min(max_bytes, WORKSPACE_CONFIG_MAX_BYTES)
    config_bytes = _read_source_regular(
        root,
        Path(".graphify") / "workspace.toml",
        deadline_ns=deadline_ns,
        max_bytes=max_bytes,
    )
    try:
        config = WorkspaceConfig.from_toml(config_bytes)
    except ContractError as exc:
        raise SourceDiscoveryError(f"invalid workspace config: {exc}") from exc
    return config, config_bytes


def read_workspace_config(
    source_root: Path,
    *,
    deadline_ns: int | None = None,
    max_bytes: int | None = None,
) -> WorkspaceConfig:
    """Safely read validated policy from an already selected source root."""

    config, _digest = read_workspace_config_with_digest(
        source_root,
        deadline_ns=deadline_ns,
        max_bytes=max_bytes,
    )
    return config


def read_workspace_config_with_digest(
    source_root: Path,
    *,
    deadline_ns: int | None = None,
    max_bytes: int | None = None,
) -> tuple[WorkspaceConfig, str]:
    """Safely read policy plus the raw-byte digest used by source identity."""

    _check_deadline(deadline_ns)
    root = source_root.resolve(strict=True)
    _check_deadline(deadline_ns)
    if not root.is_dir():
        raise SourceDiscoveryError(f"source root is not a directory: {root}")
    config, config_bytes = _read_workspace_config(
        root,
        deadline_ns=deadline_ns,
        max_bytes=max_bytes,
    )
    return config, hashlib.sha256(config_bytes).hexdigest()


def verify_source_checkout(
    source_root: Path,
    *,
    expected_git_common_dir: Path,
    expected_worktree_id: str,
    expected_git_common_device: int,
    expected_git_common_inode: int,
    expected_root_identity: tuple[int, int],
    expected_head_commit: str | None = None,
    deadline_ns: int | None = None,
) -> None:
    """Verify the selected checkout with one live local Git identity read."""

    _check_deadline(deadline_ns)
    root = source_root.resolve(strict=True)
    expected_common = expected_git_common_dir.resolve(strict=True)
    _check_deadline(deadline_ns)
    before = root.stat()
    if not stat.S_ISDIR(before.st_mode):
        raise SourceDiscoveryError(f"source root is not a directory: {root}")
    if (before.st_dev, before.st_ino) != expected_root_identity:
        raise SourceDiscoveryError("source root identity changed")
    arguments = [
        "rev-parse",
        "--show-toplevel",
        "--git-common-dir",
        "--git-dir",
    ]
    if expected_head_commit is not None:
        arguments.append("HEAD")
    resolved = _git(
        root,
        *arguments,
        deadline_ns=deadline_ns,
    ).splitlines()
    expected_fields = 4 if expected_head_commit is not None else 3
    if len(resolved) != expected_fields:
        raise SourceDiscoveryError("Git source identity response is malformed")
    top_level = _resolve_git_path(root, resolved[0], deadline_ns=deadline_ns)
    git_common_dir = _resolve_git_path(root, resolved[1], deadline_ns=deadline_ns)
    git_dir = _resolve_git_path(root, resolved[2], deadline_ns=deadline_ns)
    if top_level != root or git_common_dir != expected_common:
        raise SourceDiscoveryError("source root no longer matches registry Git identity")
    worktree_id = "main" if git_dir == git_common_dir else git_dir.name
    if worktree_id != expected_worktree_id:
        raise SourceDiscoveryError("source worktree no longer matches registry Git identity")
    if expected_head_commit is not None and resolved[3] != expected_head_commit:
        raise SourceDiscoveryError("source HEAD changed during identity verification")
    common_details = git_common_dir.stat()
    if (
        common_details.st_dev != expected_git_common_device
        or common_details.st_ino != expected_git_common_inode
    ):
        raise SourceDiscoveryError("Git common-directory identity changed")
    after = root.stat()
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise SourceDiscoveryError("source root changed during identity verification")


def source_root_identity(
    source_root: Path,
    *,
    deadline_ns: int | None = None,
) -> tuple[int, int]:
    """Capture the directory identity that a later live Git check must retain."""

    _check_deadline(deadline_ns)
    root = source_root.resolve(strict=True)
    details = root.stat()
    _check_deadline(deadline_ns)
    if not stat.S_ISDIR(details.st_mode):
        raise SourceDiscoveryError(f"source root is not a directory: {root}")
    return (details.st_dev, details.st_ino)


def discover_source(
    source_root: Path,
    *,
    deadline_ns: int | None = None,
    max_bytes: int | None = None,
) -> SourceIdentity:
    """Discover source identity without mutating the checkout or Git metadata."""

    if not {os.open, os.stat}.issubset(os.supports_dir_fd):
        raise SourceDiscoveryError(
            "source discovery requires descriptor-relative file access"
        )
    _check_deadline(deadline_ns)
    root = source_root.resolve(strict=True)
    _check_deadline(deadline_ns)
    if not root.is_dir():
        raise SourceDiscoveryError(f"source root is not a directory: {root}")
    root_identity = source_root_identity(root, deadline_ns=deadline_ns)
    # Establish complete object-tree containment before the first Git subprocess.
    # Metadata-only commands then repeat the routing/config/ancestor checks;
    # object traversal and final verification each get a fresh full check.
    _preflight_git_inputs(root, deadline_ns=deadline_ns)
    top_level = Path(
        _git(
            root, "rev-parse", "--show-toplevel", deadline_ns=deadline_ns,
            inspect_object_tree=False,
        )
    ).resolve(strict=True)
    _check_deadline(deadline_ns)
    if top_level != root:
        raise SourceDiscoveryError(f"source root must be the Git top level: {top_level}")

    config, config_bytes = _read_workspace_config(
        root,
        deadline_ns=deadline_ns,
        max_bytes=max_bytes,
    )
    repo_uuid = str(config.to_dict()["repo_uuid"])

    git_common_dir = _resolve_git_path(
        root,
        _git(
            root, "rev-parse", "--git-common-dir", deadline_ns=deadline_ns,
            inspect_object_tree=False,
        ),
        deadline_ns=deadline_ns,
    )
    git_dir = _resolve_git_path(
        root,
        _git(
            root, "rev-parse", "--git-dir", deadline_ns=deadline_ns,
            inspect_object_tree=False,
        ),
        deadline_ns=deadline_ns,
    )
    _check_deadline(deadline_ns)
    details = git_common_dir.stat()
    _check_deadline(deadline_ns)
    worktree_id = "main" if git_dir == git_common_dir else git_dir.name

    # Human-readable remote -v output lets embedded URL newlines forge records.
    # Enumerate complete config keys, then let Git resolve each fetch URL (including
    # insteadOf rules) without discarding whitespace from the URL itself.
    config_keys = _git(
        root, "config", "--null", "--name-only", "--list",
        deadline_ns=deadline_ns, strip_output=False, inspect_object_tree=False,
    )
    remote_names = {
        key[len("remote."):-len(".url")]
        for key in config_keys.split("\0")
        if key.startswith("remote.") and key.endswith(".url")
    }
    remote_pairs: dict[str, str] = {}
    for name in sorted(remote_names):
        if not name or any(character.isspace() for character in name):
            raise SourceDiscoveryError("malformed Git remote name")
        raw_url = _git(
            root, "remote", "get-url", "--", name,
            deadline_ns=deadline_ns, strip_output=False, inspect_object_tree=False,
        ).removesuffix("\n")
        if any(character.isspace() for character in raw_url):
            raise SourceDiscoveryError("fetch remote URL contains whitespace")
        normalized = _normalize_remote(raw_url)
        prior = remote_pairs.get(normalized)
        if prior is None or name < prior:
            remote_pairs[normalized] = name
    if not remote_pairs:
        raise SourceDiscoveryError("at least one fetch remote is required")

    remote_aliases: list[dict[str, str]] = []
    remote_evidence: list[dict[str, str]] = []
    for url in sorted(remote_pairs):
        preimage = {
            "kind": "graphify.workspace.remote_evidence",
            "remote_name": remote_pairs[url],
            "url": url,
        }
        remote_evidence.append(preimage)
        remote_aliases.append({"evidence_sha256": canonical_sha256(preimage), "url": url})

    registry_source: dict[str, Any] = {
        "git_common_dir": str(git_common_dir),
        "path": str(root),
        "remote_aliases": remote_aliases,
        "worktree_id": worktree_id,
    }
    try:
        registry_source = canonical_registry_source(registry_source)
    except ContractError:
        raise SourceDiscoveryError("source identity is not canonical") from None
    if _git(
        root, "rev-parse", "--is-shallow-repository", deadline_ns=deadline_ns,
        inspect_object_tree=False,
    ) == "true":
        raise SourceDiscoveryError("shallow repositories require complete history before enrollment")
    head = _git(
        root, "rev-parse", "HEAD", deadline_ns=deadline_ns,
        inspect_object_tree=False,
    )
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise SourceDiscoveryError("unsupported Git object format: SHA-1 source commit required")
    roots = tuple(
        sorted(
            filter(
                None,
                _git(
                    root,
                    "rev-list",
                    "--max-parents=0",
                    head,
                    deadline_ns=deadline_ns,
                ).splitlines(),
            )
        )
    )
    if not roots:
        raise SourceDiscoveryError("source history has no root commit")
    _config, verified_config_bytes = _read_workspace_config(
        root, deadline_ns=deadline_ns, max_bytes=max_bytes,
    )
    if verified_config_bytes != config_bytes:
        raise SourceDiscoveryError("workspace config changed during source discovery")
    verify_source_checkout(
        root,
        expected_git_common_dir=git_common_dir,
        expected_worktree_id=worktree_id,
        expected_git_common_device=details.st_dev,
        expected_git_common_inode=details.st_ino,
        expected_root_identity=root_identity,
        expected_head_commit=head,
        deadline_ns=deadline_ns,
    )
    return SourceIdentity(
        root=root,
        repo_uuid=repo_uuid,
        registry_source=registry_source,
        source_sha256=canonical_sha256(registry_source),
        head_commit=head,
        history_roots=roots,
        config_sha256=hashlib.sha256(config_bytes).hexdigest(),
        git_common_device=details.st_dev,
        git_common_inode=details.st_ino,
        remote_evidence=tuple(remote_evidence),
    )


def identity_evidence(
    source: SourceIdentity,
    authorization: OperatorAuthorization,
) -> dict[str, Any]:
    """Return the auditable evidence preimage for an authorized identity action."""

    return {
        "action": authorization.action.value,
        "authorization": authorization.to_dict(),
        **source.evidence(),
    }


__all__ = [
    "AuthorizationError",
    "IdentityAction",
    "IdentityError",
    "OperatorAuthorization",
    "SourceAmbiguousError",
    "SourceDiscoveryError",
    "SourceDiscoveryTimeout",
    "SourceIdentity",
    "UUIDCollisionError",
    "discover_source",
    "identity_evidence",
    "read_workspace_config",
    "read_workspace_config_with_digest",
    "source_root_identity",
    "verify_source_checkout",
]
