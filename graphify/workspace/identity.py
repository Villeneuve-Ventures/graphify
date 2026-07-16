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


def _git(root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
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


def _resolve_git_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=True)


def _read_source_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
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


def discover_source(source_root: Path) -> SourceIdentity:
    """Discover source identity without mutating the checkout or Git metadata."""

    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise SourceDiscoveryError(f"source root is not a directory: {root}")
    top_level = Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != root:
        raise SourceDiscoveryError(f"source root must be the Git top level: {top_level}")

    config_path = root / ".graphify" / "workspace.toml"
    config_bytes = _read_source_regular(config_path)
    try:
        config = WorkspaceConfig.from_toml(config_bytes)
    except ContractError as exc:
        raise SourceDiscoveryError(f"invalid workspace config: {exc}") from exc
    repo_uuid = str(config.to_dict()["repo_uuid"])

    git_common_dir = _resolve_git_path(root, _git(root, "rev-parse", "--git-common-dir"))
    git_dir = _resolve_git_path(root, _git(root, "rev-parse", "--git-dir"))
    details = git_common_dir.stat()
    worktree_id = "main" if git_dir == git_common_dir else git_dir.name

    remote_output = _git(root, "remote", "-v")
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
    head = _git(root, "rev-parse", "HEAD")
    roots = tuple(
        sorted(filter(None, _git(root, "rev-list", "--max-parents=0", "HEAD").splitlines()))
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
    "SourceIdentity",
    "UUIDCollisionError",
    "discover_source",
    "identity_evidence",
]
