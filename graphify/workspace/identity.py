"""Read-only source discovery and operator authorization for workspace identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from graphify.workspace.contracts import (
    ContractError,
    WorkspaceConfig,
    canonical_sha256,
)


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


def _git(
    root: Path,
    *arguments: str,
    deadline_ns: int | None = None,
) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_remaining_timeout_seconds(deadline_ns),
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceDiscoveryTimeout("source discovery deadline expired") from exc
    _check_deadline(deadline_ns)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise SourceDiscoveryError(detail)
    return result.stdout.strip()


def _normalize_remote(raw: str) -> str:
    value = raw.strip()
    if "://" not in value:
        match = re.fullmatch(r"(?P<user>[^@/:\s]+)@(?P<host>[^:/\s]+):(?P<path>.+)", value)
        if match is None:
            raise SourceDiscoveryError(f"unsupported remote URL: {raw!r}")
        value = (
            f"ssh://{match.group('user')}@{match.group('host')}/{match.group('path').lstrip('/')}"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SourceDiscoveryError(f"invalid remote URL: {raw!r}") from exc
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


def _read_workspace_config(
    root: Path,
    *,
    deadline_ns: int | None = None,
    max_bytes: int | None = None,
) -> tuple[WorkspaceConfig, bytes]:
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
    top_level = Path(
        _git(root, "rev-parse", "--show-toplevel", deadline_ns=deadline_ns)
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
        _git(root, "rev-parse", "--git-common-dir", deadline_ns=deadline_ns),
        deadline_ns=deadline_ns,
    )
    git_dir = _resolve_git_path(
        root,
        _git(root, "rev-parse", "--git-dir", deadline_ns=deadline_ns),
        deadline_ns=deadline_ns,
    )
    _check_deadline(deadline_ns)
    details = git_common_dir.stat()
    _check_deadline(deadline_ns)
    worktree_id = "main" if git_dir == git_common_dir else git_dir.name

    remote_output = _git(root, "remote", "-v", deadline_ns=deadline_ns)
    remote_pairs: dict[str, str] = {}
    for line in remote_output.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[-1] != "(fetch)":
            continue
        name, raw_url = fields[0], fields[1]
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
    head = _git(root, "rev-parse", "HEAD", deadline_ns=deadline_ns)
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
