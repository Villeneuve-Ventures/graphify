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
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
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
    path: Path,
    *,
    deadline_ns: int | None = None,
) -> bytes:
    _check_deadline(deadline_ns)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceDiscoveryError(f"cannot open source identity file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SourceDiscoveryError(
                f"source identity file is not a singular regular file: {path}"
            )
        chunks: list[bytes] = []
        while True:
            _check_deadline(deadline_ns)
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        _check_deadline(deadline_ns)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise SourceDiscoveryError(f"source identity file changed while it was read: {path}")
    try:
        installed = path.lstat()
    except OSError as exc:
        raise SourceDiscoveryError(f"source identity file disappeared after read: {path}") from exc
    if installed.st_dev != after.st_dev or installed.st_ino != after.st_ino:
        raise SourceDiscoveryError(f"source identity file was replaced while it was read: {path}")
    return b"".join(chunks)


def _read_workspace_config(
    root: Path,
    *,
    deadline_ns: int | None = None,
) -> tuple[WorkspaceConfig, bytes]:
    config_bytes = _read_source_regular(
        root / ".graphify" / "workspace.toml",
        deadline_ns=deadline_ns,
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
) -> WorkspaceConfig:
    """Safely read validated policy from an already selected source root."""

    config, _digest = read_workspace_config_with_digest(
        source_root,
        deadline_ns=deadline_ns,
    )
    return config


def read_workspace_config_with_digest(
    source_root: Path,
    *,
    deadline_ns: int | None = None,
) -> tuple[WorkspaceConfig, str]:
    """Safely read policy plus the raw-byte digest used by source identity."""

    _check_deadline(deadline_ns)
    root = source_root.resolve(strict=True)
    _check_deadline(deadline_ns)
    if not root.is_dir():
        raise SourceDiscoveryError(f"source root is not a directory: {root}")
    config, config_bytes = _read_workspace_config(root, deadline_ns=deadline_ns)
    return config, hashlib.sha256(config_bytes).hexdigest()


def verify_source_checkout(
    source_root: Path,
    *,
    expected_git_common_dir: Path,
    expected_worktree_id: str,
    expected_git_common_device: int,
    expected_git_common_inode: int,
    expected_root_identity: tuple[int, int],
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
    resolved = _git(
        root,
        "rev-parse",
        "--show-toplevel",
        "--git-common-dir",
        "--git-dir",
        deadline_ns=deadline_ns,
    ).splitlines()
    if len(resolved) != 3:
        raise SourceDiscoveryError("Git source identity response is malformed")
    top_level = _resolve_git_path(root, resolved[0], deadline_ns=deadline_ns)
    git_common_dir = _resolve_git_path(root, resolved[1], deadline_ns=deadline_ns)
    git_dir = _resolve_git_path(root, resolved[2], deadline_ns=deadline_ns)
    if top_level != root or git_common_dir != expected_common:
        raise SourceDiscoveryError("source root no longer matches registry Git identity")
    worktree_id = "main" if git_dir == git_common_dir else git_dir.name
    if worktree_id != expected_worktree_id:
        raise SourceDiscoveryError("source worktree no longer matches registry Git identity")
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
) -> SourceIdentity:
    """Discover source identity without mutating the checkout or Git metadata."""

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

    config, config_bytes = _read_workspace_config(root, deadline_ns=deadline_ns)
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
                    "HEAD",
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
