"""Internal P5B2 semantic-release bundle and deterministic byte classifier.

This module deliberately owns no workspace, policy, disposition, persistence,
projection, provider, publication, or public CLI behavior.  The only runtime
authority is the installed ``graphify`` package root containing this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence
import unicodedata

from graphify.workspace.contracts import ContractError, canonical_json_bytes


BUNDLE_MANIFEST_MAX_BYTES = 1 * 1024 * 1024
BUNDLE_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024
FIELD_MAX_BYTES = 16 * 1024
MAX_PROFILES = 64
MAX_CATEGORIES = 4_096
MAX_RULES = 4_096
MAX_IDENTIFIER_BYTES = 256
MAX_MATCH_IDS = 256

_MANIFEST_RELATIVE_PATH = "workspace/semantic_release_manifest.json"
_CLASSIFIER_RELATIVE_PATH = "workspace/semantic_release.py"
_DATA_PREFIX = "workspace/semantic_release_data/"
_MANIFEST_CONTRACT = "graphify.workspace.semantic_release_manifest.internal"
_MANIFEST_ID = "graphify.semantic_release_bundle.v1"
_FORMAT_VERSION = 1
_COMPATIBILITY_VERSION = 1
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_DIRECTORY_ENTRIES = 8_192
_MAX_DIRECTORY_DEPTH = 8
_ALLOWED_FILE_MODES = frozenset({0o444, 0o644})
_ARTIFACT_KINDS = frozenset(
    {"classifier", "classifier_abi", "taxonomy", "normalization", "ruleset", "profile"}
)
_SINGLETON_KINDS = frozenset(
    {"classifier", "classifier_abi", "taxonomy", "normalization", "ruleset"}
)
_COORDINATE_FIELDS = {
    "classifier": ("classifier_id", "classifier_version"),
    "classifier_abi": ("abi_id", "abi_version"),
    "taxonomy": ("taxonomy_id", "taxonomy_version"),
    "normalization": ("normalization_id", "normalization_version"),
    "ruleset": ("ruleset_id", "ruleset_version"),
    "profile": ("profile_id", "profile_version"),
}
_PROFILE_ID_RE = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*)*\.v[1-9][0-9]*",
    re.ASCII,
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PINNED_TRIM_CODE_POINTS = frozenset(
    {
        0x0009,
        0x000A,
        0x000B,
        0x000C,
        0x000D,
        0x001C,
        0x001D,
        0x001E,
        0x001F,
        0x0020,
        0x0085,
        0x00A0,
        0x1680,
        0x2000,
        0x2001,
        0x2002,
        0x2003,
        0x2004,
        0x2005,
        0x2006,
        0x2007,
        0x2008,
        0x2009,
        0x200A,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    }
)
_ASCII_LOWER = bytes.maketrans(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    b"abcdefghijklmnopqrstuvwxyz",
)

_TAXONOMY_ID = "graphify.semantic_release.core_taxonomy.v1"
_TAXONOMY_VERSION = 1
_RULESET_ID = "graphify.semantic_release.core_ruleset.v1"
_RULESET_VERSION = 1
_CLASSIFIER_ID = "graphify.semantic_release.deterministic_classifier.v1"
_CLASSIFIER_VERSION = 1
_ABI_ID = "graphify.semantic_release.byte_abi.v1"
_ABI_VERSION = 1
_NORMALIZATION_ID = "graphify.semantic_release.already_canonical_utf8.v1"
_NORMALIZATION_VERSION = 1

_CATEGORY_DOCUMENTS = (
    {
        "category_id": "secret.authorization_credential",
        "definition": "Complete supported authorization credential syntax.",
    },
    {
        "category_id": "secret.credential_assignment",
        "definition": "Closed credential key assignment with a non-placeholder value.",
    },
    {
        "category_id": "secret.credential_uri",
        "definition": "Credential-bearing URI userinfo with an explicit password component.",
    },
    {
        "category_id": "secret.private_key_material",
        "definition": "Complete deterministically recognized private-key material.",
    },
    {
        "category_id": "secret.provider_credential",
        "definition": "Complete token in the pinned local provider credential registry.",
    },
    {
        "category_id": "secret.seed_or_recovery_material",
        "definition": "Complete explicitly labeled seed or recovery phrase format.",
    },
)

_RULE_DOCUMENTS = (
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.credential_assignment",
        "credential_group": "credential",
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"(?:^|\r?\n)(?ai:api[_-]?key|password|passwd|secret|client[_-]?secret|"
            r"access[_-]?token|auth[_-]?token|token)[ \t]*(?:=|:)[ \t]*"
            r"(?P<credential>[A-Za-z0-9._~+/@-]{8,256})(?=$|\r?\n)"
        ),
        "rule_id": "core.assignment.bare.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.credential_assignment",
        "credential_group": "credential",
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"(?:^|\r?\n)(?ai:api[_-]?key|password|passwd|secret|client[_-]?secret|"
            r"access[_-]?token|auth[_-]?token|token)[ \t]*(?:=|:)[ \t]*"
            r"(?P<quote>[\"'])(?P<credential>[^\"'\r\n]{8,256})(?P=quote)(?=$|\r?\n)"
        ),
        "rule_id": "core.assignment.quoted.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.authorization_credential",
        "credential_group": "credential",
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"(?:^|\r?\n)(?ai:Authorization):[ \t]*(?ai:Basic)[ \t]+"
            r"(?P<credential>[A-Za-z0-9+/]{8,}={0,2})(?=$|\r?\n)"
        ),
        "rule_id": "core.authorization.basic.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.authorization_credential",
        "credential_group": "credential",
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"(?:^|\r?\n)(?ai:Authorization):[ \t]*(?ai:Bearer)[ \t]+"
            r"(?P<credential>[A-Za-z0-9._~+/-]{16,}={0,2})(?=$|\r?\n)"
        ),
        "rule_id": "core.authorization.bearer.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.private_key_material",
        "credential_group": None,
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"-----BEGIN (?P<label>(?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY)-----"
            r"\r?\n(?:(?:[A-Za-z0-9+/]{4}){16}\r?\n)+"
            r"(?:(?:[A-Za-z0-9+/]{4}){1,16}|"
            r"(?:[A-Za-z0-9+/]{4}){0,15}[A-Za-z0-9+/]{3}=|"
            r"(?:[A-Za-z0-9+/]{4}){0,15}[A-Za-z0-9+/]{2}==)"
            r"\r?\n-----END (?P=label)-----"
        ),
        "rule_id": "core.pem.private_key.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.provider_credential",
        "credential_group": None,
        "matcher": "byte_regex_search_v1",
        "pattern": r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])",
        "rule_id": "core.provider.aws_access_key.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.provider_credential",
        "credential_group": None,
        "matcher": "byte_regex_search_v1",
        "pattern": r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,255}(?![A-Za-z0-9])",
        "rule_id": "core.provider.github_token.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.provider_credential",
        "credential_group": None,
        "matcher": "byte_regex_search_v1",
        "pattern": r"(?<![A-Za-z0-9])sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,255}(?![A-Za-z0-9])",
        "rule_id": "core.provider.openai_service_key.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.provider_credential",
        "credential_group": None,
        "matcher": "byte_regex_search_v1",
        "pattern": r"(?<![A-Za-z0-9])(?:sk|rk)_live_[A-Za-z0-9]{16,255}(?![A-Za-z0-9])",
        "rule_id": "core.provider.stripe_live_key.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.seed_or_recovery_material",
        "credential_group": "credential",
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"(?:^|\r?\n)(?ai:seed[ _-]?phrase|recovery[ _-]?phrase|mnemonic)"
            r"[ \t]*(?:=|:)[ \t]*(?P<credential>(?:(?:[a-z]{3,12}[ \t]+){11}"
            r"[a-z]{3,12}|(?:[a-z]{3,12}[ \t]+){14}[a-z]{3,12}|"
            r"(?:[a-z]{3,12}[ \t]+){17}[a-z]{3,12}|"
            r"(?:[a-z]{3,12}[ \t]+){20}[a-z]{3,12}|"
            r"(?:[a-z]{3,12}[ \t]+){23}[a-z]{3,12}))(?=$|\r?\n)"
        ),
        "rule_id": "core.recovery.labeled_phrase.v1",
    },
    {
        "ascii_case_insensitive": False,
        "category_id": "secret.credential_uri",
        "credential_group": "credential",
        "matcher": "byte_regex_search_v1",
        "pattern": (
            r"[A-Za-z][A-Za-z0-9+.-]{1,31}://"
            r"[A-Za-z0-9._~!$&'()*+,;=%-]{1,128}:"
            r"(?P<credential>[^\s/@:]{8,256})@"
            r"[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:[/?#][^\s]*)?"
        ),
        "rule_id": "core.uri.userinfo_password.v1",
    },
)

_EXCLUDED_CREDENTIAL_VALUES = (
    "<redacted>",
    "changeme",
    "dummy_value",
    "example_value",
    "placeholder",
    "redacted",
    "your_api_key",
    "your_password",
    "your_secret",
    "your_token",
)


class SemanticReleaseBundleError(RuntimeError):
    """The installed semantic-release trust-root bundle is not exact and usable."""


@dataclass(frozen=True)
class ProfileCoordinate:
    profile_id: str
    profile_version: int


CORE_SECRETS_PROFILE = ProfileCoordinate("core_secrets.v1", 1)
PROVIDER_CREDENTIALS_PROFILE = ProfileCoordinate("provider_credentials.v1", 1)


@dataclass(frozen=True)
class BundleArtifact:
    artifact_kind: str
    path: str
    mode: str
    byte_count: int
    sha256: str
    artifact_id: str
    artifact_version: int


@dataclass(frozen=True)
class CoverageProfile:
    coordinate: ProfileCoordinate
    taxonomy_id: str
    taxonomy_version: int
    category_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category_id: str
    credential_group: str | None
    pattern: re.Pattern[bytes]


@dataclass(frozen=True)
class SemanticReleaseBundle:
    manifest_id: str
    manifest_bytes: bytes
    manifest_sha256: str
    artifacts: tuple[BundleArtifact, ...]
    profiles: tuple[CoverageProfile, ...]
    rules: tuple[_Rule, ...]
    excluded_credential_values: frozenset[bytes]


@dataclass(frozen=True)
class ClassificationResult:
    outcome: str
    category_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "category_ids": list(self.category_ids),
            "outcome": self.outcome,
            "rule_ids": list(self.rule_ids),
        }


def _indeterminate() -> ClassificationResult:
    return ClassificationResult("INDETERMINATE")


def _installed_package_root() -> Path:
    module_path = Path(__file__)
    if not module_path.is_absolute() or module_path.name != "semantic_release.py":
        raise SemanticReleaseBundleError("installed classifier module path is not canonical")
    if module_path.parent.name != "workspace" or module_path.parent.parent.name != "graphify":
        raise SemanticReleaseBundleError("installed graphify package root is not canonical")
    return module_path.parent.parent


def _require_no_follow_support() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory:
        raise SemanticReleaseBundleError("descriptor-relative no-follow traversal is unavailable")
    return no_follow


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | _require_no_follow_support()
        | getattr(os, "O_DIRECTORY")
    )


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | _require_no_follow_support()
        | getattr(os, "O_NONBLOCK", 0)
    )


def _stat_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _require_exact_directory_name(parent_descriptor: int, name: str, label: str) -> None:
    count = 0
    exact = False
    try:
        with os.scandir(parent_descriptor) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_DIRECTORY_ENTRIES:
                    raise SemanticReleaseBundleError(f"{label}: directory inventory exceeds limit")
                if entry.name == name:
                    exact = True
    except OSError as exc:
        raise SemanticReleaseBundleError(f"{label}: directory inventory could not be read") from exc
    if not exact:
        raise SemanticReleaseBundleError(f"{label}: path uses an alternate spelling")


def _revalidate_directories(
    package_root: Path,
    root_descriptor: int,
    bindings: Sequence[tuple[str, int, int]],
    root_identity: tuple[int, int, int, int, int, int, int],
) -> None:
    try:
        rebound_root = os.stat(package_root, follow_symlinks=False)
        opened_root = os.fstat(root_descriptor)
    except OSError as exc:
        raise SemanticReleaseBundleError("installed package root could not be revalidated") from exc
    if (
        not stat.S_ISDIR(rebound_root.st_mode)
        or _stat_identity(opened_root) != root_identity
        or _stat_identity(rebound_root) != root_identity
    ):
        raise SemanticReleaseBundleError("installed package root changed during read")
    for component, parent_descriptor, child_descriptor in bindings:
        try:
            opened = os.fstat(child_descriptor)
            rebound = os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SemanticReleaseBundleError("bundle directory could not be revalidated") from exc
        if not stat.S_ISDIR(rebound.st_mode) or _stat_identity(opened) != _stat_identity(rebound):
            raise SemanticReleaseBundleError("bundle directory changed during read")


def _read_chunks(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        read_size = min(_READ_CHUNK_BYTES, (max_bytes - total) + 1)
        try:
            chunk = os.read(descriptor, read_size)
        except InterruptedError:
            continue
        except OSError as exc:
            raise SemanticReleaseBundleError("bundle file read failed") from exc
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise SemanticReleaseBundleError("bundle file exceeds its bounded read limit")
        chunks.append(chunk)


def _read_package_file(
    package_root: Path,
    relative_path: str,
    *,
    max_bytes: int,
    expected_mode: str | None = None,
    expected_byte_count: int | None = None,
    expected_sha256: str | None = None,
) -> bytes:
    path = _validated_relative_path(relative_path, "bundle path")
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(package_root, _directory_flags())
        descriptors.append(root_descriptor)
        root_details = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_details.st_mode):
            raise SemanticReleaseBundleError("installed graphify package root is not a directory")
        root_identity = _stat_identity(root_details)
        bindings: list[tuple[str, int, int]] = []
        parent = root_descriptor
        for component in path.parts[:-1]:
            _require_exact_directory_name(parent, component, relative_path)
            try:
                child = os.open(component, _directory_flags(), dir_fd=parent)
            except OSError as exc:
                raise SemanticReleaseBundleError(
                    f"{relative_path}: directory component is linked or unsafe"
                ) from exc
            descriptors.append(child)
            child_details = os.fstat(child)
            if not stat.S_ISDIR(child_details.st_mode):
                raise SemanticReleaseBundleError(
                    f"{relative_path}: directory component is not a directory"
                )
            bindings.append((component, parent, child))
            parent = child

        name = path.parts[-1]
        _require_exact_directory_name(parent, name, relative_path)
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=parent)
        except OSError as exc:
            raise SemanticReleaseBundleError(
                f"{relative_path}: file is missing, linked, or unsafe"
            ) from exc
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SemanticReleaseBundleError(
                f"{relative_path}: artifact is not a single-link regular file"
            )
        if mode not in _ALLOWED_FILE_MODES:
            raise SemanticReleaseBundleError(f"{relative_path}: file mode is not allowed")
        if expected_mode is not None and mode != int(expected_mode, 8):
            raise SemanticReleaseBundleError(f"{relative_path}: file mode differs from manifest")
        if before.st_size > max_bytes:
            if relative_path == _MANIFEST_RELATIVE_PATH:
                raise SemanticReleaseBundleError("semantic-release manifest exceeds 1 MiB")
            raise SemanticReleaseBundleError(f"{relative_path}: artifact exceeds bounded size")
        if expected_byte_count is not None and before.st_size != expected_byte_count:
            raise SemanticReleaseBundleError(f"{relative_path}: artifact size differs from manifest")

        raw = _read_chunks(descriptor, max_bytes)
        after = os.fstat(descriptor)
        try:
            rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise SemanticReleaseBundleError(f"{relative_path}: path binding was lost") from exc
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(rebound)
            or len(raw) != before.st_size
        ):
            raise SemanticReleaseBundleError(f"{relative_path}: artifact changed during read")
        _revalidate_directories(package_root, root_descriptor, bindings, root_identity)
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise SemanticReleaseBundleError(f"{relative_path}: artifact digest differs from manifest")
        return raw
    except SemanticReleaseBundleError:
        raise
    except OSError as exc:
        raise SemanticReleaseBundleError(
            f"{relative_path}: descriptor-relative traversal failed"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validated_relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SemanticReleaseBundleError(f"{label}: expected nonempty path")
    if value != unicodedata.normalize("NFC", value):
        raise SemanticReleaseBundleError(f"{label}: path is not NFC")
    if "\\" in value or value.startswith("/") or value.endswith("/") or "//" in value:
        raise SemanticReleaseBundleError(f"{label}: path is not POSIX relative normal form")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise SemanticReleaseBundleError(f"{label}: path contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SemanticReleaseBundleError(f"{label}: path is not contained")
    if path.as_posix() != value:
        raise SemanticReleaseBundleError(f"{label}: path has an alternate spelling")
    return path


def _duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticReleaseBundleError(f"duplicate JSON member: {key!r}")
        result[key] = value
    return result


def _canonical_json_document(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw, object_pairs_hook=_duplicate_pairs)
    except SemanticReleaseBundleError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SemanticReleaseBundleError(f"{label}: malformed UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SemanticReleaseBundleError(f"{label}: expected JSON object")
    try:
        expected = canonical_json_bytes(value)
    except ContractError as exc:
        raise SemanticReleaseBundleError(f"{label}: JSON is not canonical") from exc
    if raw != expected:
        raise SemanticReleaseBundleError(f"{label}: JSON bytes are not canonical")
    return value


def _exact_members(value: Mapping[str, object], expected: set[str], label: str) -> None:
    missing = expected - set(value)
    unexpected = set(value) - expected
    if missing:
        raise SemanticReleaseBundleError(f"{label}: missing member {sorted(missing)[0]!r}")
    if unexpected:
        raise SemanticReleaseBundleError(f"{label}: unexpected member {sorted(unexpected)[0]!r}")


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticReleaseBundleError(f"{label}: expected positive integer")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SemanticReleaseBundleError(f"{label}: expected nonempty identifier")
    if value != unicodedata.normalize("NFC", value):
        raise SemanticReleaseBundleError(f"{label}: identifier is not NFC")
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise SemanticReleaseBundleError(f"{label}: identifier exceeds 256 UTF-8 bytes")
    return value


def _profile_coordinate(profile_id: object, profile_version: object, label: str) -> ProfileCoordinate:
    identifier = _identifier(profile_id, f"{label}.profile_id")
    version = _positive_integer(profile_version, f"{label}.profile_version")
    if _PROFILE_ID_RE.fullmatch(identifier) is None:
        raise SemanticReleaseBundleError(f"{label}: profile ID is invalid")
    suffix = identifier.rsplit(".v", 1)[1]
    if str(version) != suffix:
        raise SemanticReleaseBundleError(
            f"{label}: profile coordinate ID/version suffix disagreement"
        )
    return ProfileCoordinate(identifier, version)


def _artifact_entry(value: object, index: int) -> BundleArtifact:
    label = f"manifest.artifacts[{index}]"
    if not isinstance(value, dict):
        raise SemanticReleaseBundleError(f"{label}: expected object")
    kind = value.get("artifact_kind")
    if kind not in _ARTIFACT_KINDS:
        raise SemanticReleaseBundleError(f"{label}: unknown artifact kind")
    assert isinstance(kind, str)
    id_field, version_field = _COORDINATE_FIELDS[kind]
    _exact_members(
        value,
        {
            "artifact_kind",
            "path",
            "mode",
            "byte_count",
            "sha256",
            id_field,
            version_field,
        },
        label,
    )
    path = _validated_relative_path(value["path"], f"{label}.path").as_posix()
    if kind == "classifier":
        if path != _CLASSIFIER_RELATIVE_PATH:
            raise SemanticReleaseBundleError("manifest classifier path is not canonical")
    elif not path.startswith(_DATA_PREFIX):
        raise SemanticReleaseBundleError(f"{label}: data artifact is outside package-data root")
    if kind == "profile" and not path.startswith(_DATA_PREFIX + "profiles/"):
        raise SemanticReleaseBundleError(f"{label}: profile is outside profile data root")
    mode = value["mode"]
    if mode not in {"0444", "0644"}:
        raise SemanticReleaseBundleError(f"{label}.mode: unsupported mode")
    byte_count = _positive_integer(value["byte_count"], f"{label}.byte_count")
    digest = value["sha256"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise SemanticReleaseBundleError(f"{label}.sha256: invalid lowercase SHA-256")
    if kind == "profile":
        coordinate = _profile_coordinate(value[id_field], value[version_field], label)
        artifact_id = coordinate.profile_id
        artifact_version = coordinate.profile_version
    else:
        artifact_id = _identifier(value[id_field], f"{label}.{id_field}")
        artifact_version = _positive_integer(value[version_field], f"{label}.{version_field}")
    return BundleArtifact(
        artifact_kind=kind,
        path=path,
        mode=mode,
        byte_count=byte_count,
        sha256=digest,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
    )


def _manifest_document(raw: bytes) -> tuple[dict[str, object], tuple[BundleArtifact, ...]]:
    manifest = _canonical_json_document(raw, "semantic-release manifest")
    _exact_members(
        manifest,
        {"artifacts", "compatibility_version", "contract", "format_version", "manifest_id"},
        "semantic-release manifest",
    )
    if manifest["contract"] != _MANIFEST_CONTRACT:
        raise SemanticReleaseBundleError("semantic-release manifest contract is unsupported")
    if manifest["format_version"] != _FORMAT_VERSION:
        raise SemanticReleaseBundleError("semantic-release manifest format version is unsupported")
    if manifest["compatibility_version"] != _COMPATIBILITY_VERSION:
        raise SemanticReleaseBundleError(
            "semantic-release manifest compatibility version is unsupported"
        )
    if manifest["manifest_id"] != _MANIFEST_ID:
        raise SemanticReleaseBundleError("semantic-release manifest ID is unsupported")
    raw_artifacts = manifest["artifacts"]
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise SemanticReleaseBundleError("semantic-release manifest artifacts must be nonempty")
    artifacts = tuple(_artifact_entry(value, index) for index, value in enumerate(raw_artifacts))
    paths = tuple(artifact.path for artifact in artifacts)
    if paths != tuple(sorted(paths, key=lambda value: value.encode("utf-8"))):
        raise SemanticReleaseBundleError("semantic-release manifest inventory is not ordered")
    if len(set(paths)) != len(paths):
        raise SemanticReleaseBundleError("semantic-release manifest contains duplicate paths")
    coordinates = tuple(
        (artifact.artifact_kind, artifact.artifact_id, artifact.artifact_version)
        for artifact in artifacts
    )
    if len(set(coordinates)) != len(coordinates):
        raise SemanticReleaseBundleError("semantic-release manifest contains duplicate coordinates")
    for kind in _SINGLETON_KINDS:
        if sum(artifact.artifact_kind == kind for artifact in artifacts) != 1:
            raise SemanticReleaseBundleError(f"semantic-release manifest requires one {kind}")
    if not any(
        artifact.artifact_kind == "profile"
        and artifact.artifact_id == CORE_SECRETS_PROFILE.profile_id
        and artifact.artifact_version == CORE_SECRETS_PROFILE.profile_version
        for artifact in artifacts
    ):
        raise SemanticReleaseBundleError("semantic-release manifest requires core_secrets.v1")
    if sum(artifact.byte_count for artifact in artifacts) > BUNDLE_ARTIFACT_MAX_BYTES:
        raise SemanticReleaseBundleError("semantic-release bundle byte limit exceeded")
    return manifest, artifacts


def _scan_data_inventory(package_root: Path) -> set[str]:
    expected_root = PurePosixPath(_DATA_PREFIX.removesuffix("/"))
    descriptors: list[int] = []
    files: set[str] = set()
    total_entries = 0
    try:
        root_descriptor = os.open(package_root, _directory_flags())
        descriptors.append(root_descriptor)
        root_identity = _stat_identity(os.fstat(root_descriptor))
        bindings: list[tuple[str, int, int]] = []
        parent = root_descriptor
        for component in expected_root.parts:
            _require_exact_directory_name(parent, component, _DATA_PREFIX)
            child = os.open(component, _directory_flags(), dir_fd=parent)
            descriptors.append(child)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise SemanticReleaseBundleError("semantic-release data root is not a directory")
            bindings.append((component, parent, child))
            parent = child

        def scan(descriptor: int, relative: PurePosixPath, depth: int = 0) -> None:
            nonlocal total_entries
            if depth > _MAX_DIRECTORY_DEPTH:
                raise SemanticReleaseBundleError(
                    "semantic-release data inventory exceeds depth limit"
                )
            before = os.fstat(descriptor)
            try:
                with os.scandir(descriptor) as entries:
                    names = sorted(
                        (entry.name for entry in entries),
                        key=lambda value: value.encode("utf-8"),
                    )
            except OSError as exc:
                raise SemanticReleaseBundleError("semantic-release data inventory is unreadable") from exc
            for name in names:
                total_entries += 1
                if total_entries > _MAX_DIRECTORY_ENTRIES:
                    raise SemanticReleaseBundleError("semantic-release data inventory exceeds limit")
                _validated_relative_path(name, "semantic-release data entry")
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child_relative = relative / name
                if stat.S_ISDIR(details.st_mode):
                    child = os.open(name, _directory_flags(), dir_fd=descriptor)
                    descriptors.append(child)
                    opened = os.fstat(child)
                    if _stat_identity(opened) != _stat_identity(details):
                        raise SemanticReleaseBundleError(
                            "semantic-release data directory binding changed"
                        )
                    scan(child, child_relative, depth + 1)
                    rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if _stat_identity(rebound) != _stat_identity(opened):
                        raise SemanticReleaseBundleError(
                            "semantic-release data directory changed during inventory"
                        )
                elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                    if stat.S_IMODE(details.st_mode) not in _ALLOWED_FILE_MODES:
                        raise SemanticReleaseBundleError(
                            "semantic-release data file mode is not allowed"
                        )
                    files.add(child_relative.as_posix())
                else:
                    raise SemanticReleaseBundleError(
                        "semantic-release data contains a linked or special entry"
                    )
            after = os.fstat(descriptor)
            if _stat_identity(before) != _stat_identity(after):
                raise SemanticReleaseBundleError(
                    "semantic-release data directory changed during inventory"
                )

        scan(parent, expected_root)
        _revalidate_directories(package_root, root_descriptor, bindings, root_identity)
        return files
    except SemanticReleaseBundleError:
        raise
    except OSError as exc:
        raise SemanticReleaseBundleError(
            "semantic-release data descriptor traversal failed"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _document_coordinate(
    document: Mapping[str, object],
    artifact: BundleArtifact,
    id_field: str,
    version_field: str,
    label: str,
) -> None:
    if (
        document.get(id_field) != artifact.artifact_id
        or document.get(version_field) != artifact.artifact_version
    ):
        raise SemanticReleaseBundleError(f"{label}: artifact coordinate differs from manifest")


def _validate_classifier_artifact(artifact: BundleArtifact) -> None:
    if (
        artifact.artifact_id != _CLASSIFIER_ID
        or artifact.artifact_version != _CLASSIFIER_VERSION
        or artifact.path != _CLASSIFIER_RELATIVE_PATH
    ):
        raise SemanticReleaseBundleError("classifier coordinate is unsupported")


def _validate_abi(document: dict[str, object], artifact: BundleArtifact) -> None:
    expected = {
        "abi_id": _ABI_ID,
        "abi_version": _ABI_VERSION,
        "ascii_case_fold": "syntax_names_a_to_z_v1",
        "byte_comparison": "unsigned_utf8_lex_shorter_prefix_first_v1",
        "contract": "graphify.workspace.semantic_release_classifier_abi.internal",
        "dictionary_encoding": "canonical_utf8_json_arrays_v1",
        "duplicate_reduction": "unique_ids_utf8_lex_v1",
        "error_outcome": "INDETERMINATE",
        "format_version": 1,
        "matcher_grammar": "python_bytes_regex_ascii_v1",
        "match_order": ["ruleset_order", "match_start", "match_end", "rule_id_utf8_lex_v1"],
        "outcomes": ["NO_MATCH", "MATCH", "INDETERMINATE"],
        "value_transform": "none",
    }
    _exact_members(document, set(expected), "classifier ABI")
    _document_coordinate(document, artifact, "abi_id", "abi_version", "classifier ABI")
    if document != expected:
        raise SemanticReleaseBundleError("classifier ABI semantics are unsupported")


def _validate_normalization(document: dict[str, object], artifact: BundleArtifact) -> None:
    expected = {
        "contract": "graphify.workspace.semantic_release_normalization.internal",
        "control_code_points": [
            "U+0000-U+0009",
            "U+000B-U+001F",
            "U+007F-U+009F",
        ],
        "format_version": 1,
        "input_encoding": "UTF-8",
        "max_byte_count": FIELD_MAX_BYTES,
        "normalization_id": _NORMALIZATION_ID,
        "normalization_version": _NORMALIZATION_VERSION,
        "preconditions": [
            "nonempty",
            "trimmed_pinned_whitespace_v1",
            "nfc_prevalidated",
            "forbidden_controls_absent",
        ],
        "protocol_separator": "LF_prevalidated_rationale_only",
        "runtime_transforms": [],
        "trim_code_points": sorted(_PINNED_TRIM_CODE_POINTS),
    }
    _exact_members(document, set(expected), "normalization")
    _document_coordinate(
        document,
        artifact,
        "normalization_id",
        "normalization_version",
        "normalization",
    )
    if document != expected:
        raise SemanticReleaseBundleError("normalization semantics are unsupported")


def _validate_taxonomy(
    document: dict[str, object],
    artifact: BundleArtifact,
) -> frozenset[str]:
    _exact_members(
        document,
        {"categories", "contract", "format_version", "taxonomy_id", "taxonomy_version"},
        "taxonomy",
    )
    _document_coordinate(document, artifact, "taxonomy_id", "taxonomy_version", "taxonomy")
    if (
        document["contract"] != "graphify.workspace.semantic_release_taxonomy.internal"
        or document["format_version"] != 1
        or artifact.artifact_id != _TAXONOMY_ID
        or artifact.artifact_version != _TAXONOMY_VERSION
    ):
        raise SemanticReleaseBundleError("taxonomy coordinate is unsupported")
    categories = document["categories"]
    if not isinstance(categories, list):
        raise SemanticReleaseBundleError("taxonomy categories must be an array")
    if len(categories) > MAX_CATEGORIES:
        raise SemanticReleaseBundleError("taxonomy limit exceeded")
    normalized: list[dict[str, str]] = []
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise SemanticReleaseBundleError(f"taxonomy.categories[{index}]: expected object")
        _exact_members(category, {"category_id", "definition"}, "taxonomy category")
        category_id = _identifier(category["category_id"], "taxonomy category ID")
        definition = category["definition"]
        if not isinstance(definition, str) or not definition:
            raise SemanticReleaseBundleError("taxonomy category definition is invalid")
        normalized.append({"category_id": category_id, "definition": definition})
    category_ids = [category["category_id"] for category in normalized]
    if category_ids != sorted(category_ids, key=lambda value: value.encode("utf-8")):
        raise SemanticReleaseBundleError("taxonomy categories are not utf8_lex_v1 ordered")
    if len(category_ids) != len(set(category_ids)):
        raise SemanticReleaseBundleError("taxonomy categories are duplicated")
    if tuple(normalized) != _CATEGORY_DOCUMENTS:
        raise SemanticReleaseBundleError("taxonomy version contains unsupported categories")
    return frozenset(category_ids)


def _validate_ruleset(
    document: dict[str, object],
    artifact: BundleArtifact,
    category_ids: frozenset[str],
) -> tuple[tuple[_Rule, ...], frozenset[bytes]]:
    _exact_members(
        document,
        {
            "contract",
            "excluded_credential_values",
            "format_version",
            "rules",
            "ruleset_id",
            "ruleset_version",
            "taxonomy_id",
            "taxonomy_version",
        },
        "ruleset",
    )
    _document_coordinate(document, artifact, "ruleset_id", "ruleset_version", "ruleset")
    if (
        document["contract"] != "graphify.workspace.semantic_release_ruleset.internal"
        or document["format_version"] != 1
        or artifact.artifact_id != _RULESET_ID
        or artifact.artifact_version != _RULESET_VERSION
        or document["taxonomy_id"] != _TAXONOMY_ID
        or document["taxonomy_version"] != _TAXONOMY_VERSION
    ):
        raise SemanticReleaseBundleError("ruleset coordinate is unsupported")
    excluded = document["excluded_credential_values"]
    if excluded != list(_EXCLUDED_CREDENTIAL_VALUES):
        raise SemanticReleaseBundleError("ruleset placeholder dictionary is unsupported")
    raw_rules = document["rules"]
    if not isinstance(raw_rules, list):
        raise SemanticReleaseBundleError("ruleset rules must be an array")
    if len(raw_rules) > MAX_RULES:
        raise SemanticReleaseBundleError("ruleset limit exceeded")
    rules: list[_Rule] = []
    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(raw_rules):
        label = f"ruleset.rules[{index}]"
        if not isinstance(value, dict):
            raise SemanticReleaseBundleError(f"{label}: expected object")
        _exact_members(
            value,
            {
                "ascii_case_insensitive",
                "category_id",
                "credential_group",
                "matcher",
                "pattern",
                "rule_id",
            },
            label,
        )
        rule_id = _identifier(value["rule_id"], f"{label}.rule_id")
        category_id = _identifier(value["category_id"], f"{label}.category_id")
        if category_id not in category_ids:
            raise SemanticReleaseBundleError(f"{label}: unknown taxonomy category")
        if rule_id in seen_ids:
            raise SemanticReleaseBundleError("ruleset contains duplicate rule IDs")
        seen_ids.add(rule_id)
        if value["matcher"] != "byte_regex_search_v1":
            raise SemanticReleaseBundleError(f"{label}: unknown matcher grammar")
        if value["ascii_case_insensitive"] is not False:
            raise SemanticReleaseBundleError(f"{label}: global case folding is forbidden")
        credential_group = value["credential_group"]
        if credential_group not in {None, "credential"}:
            raise SemanticReleaseBundleError(f"{label}: credential group is invalid")
        pattern_text = value["pattern"]
        if not isinstance(pattern_text, str):
            raise SemanticReleaseBundleError(f"{label}: pattern must be a string")
        try:
            pattern_bytes = pattern_text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SemanticReleaseBundleError(f"{label}: pattern must be ASCII") from exc
        try:
            pattern = re.compile(pattern_bytes, re.ASCII)
        except re.error as exc:
            raise SemanticReleaseBundleError(f"{label}: pattern is unexecutable") from exc
        if credential_group is not None and credential_group not in pattern.groupindex:
            raise SemanticReleaseBundleError(f"{label}: credential group is missing")
        normalized.append(dict(value))
        rules.append(_Rule(rule_id, category_id, credential_group, pattern))
    if tuple(normalized) != _RULE_DOCUMENTS:
        raise SemanticReleaseBundleError("ruleset version contains unsupported rules")
    excluded_bytes = frozenset(value.encode("ascii") for value in _EXCLUDED_CREDENTIAL_VALUES)
    return tuple(rules), excluded_bytes


def _ordered_unique_identifiers(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SemanticReleaseBundleError(f"{label}: expected array")
    identifiers = tuple(_identifier(item, label) for item in value)
    if identifiers != tuple(sorted(identifiers, key=lambda item: item.encode("utf-8"))):
        raise SemanticReleaseBundleError(f"{label}: identifiers are not utf8_lex_v1 ordered")
    if len(identifiers) != len(set(identifiers)):
        raise SemanticReleaseBundleError(f"{label}: identifiers are duplicated")
    return identifiers


def _validate_profile(
    document: dict[str, object],
    artifact: BundleArtifact,
    category_ids: frozenset[str],
    rule_ids: frozenset[str],
) -> CoverageProfile:
    _exact_members(
        document,
        {
            "category_ids",
            "contract",
            "format_version",
            "profile_id",
            "profile_version",
            "rule_ids",
            "taxonomy_id",
            "taxonomy_version",
        },
        "profile",
    )
    coordinate = _profile_coordinate(document["profile_id"], document["profile_version"], "profile")
    if coordinate != ProfileCoordinate(artifact.artifact_id, artifact.artifact_version):
        raise SemanticReleaseBundleError("profile: artifact coordinate differs from manifest")
    if (
        document["contract"] != "graphify.workspace.semantic_release_profile.internal"
        or document["format_version"] != 1
        or document["taxonomy_id"] != _TAXONOMY_ID
        or document["taxonomy_version"] != _TAXONOMY_VERSION
    ):
        raise SemanticReleaseBundleError("profile coordinate is unsupported")
    selected_categories = _ordered_unique_identifiers(document["category_ids"], "profile categories")
    selected_rules = _ordered_unique_identifiers(document["rule_ids"], "profile rules")
    if not set(selected_categories).issubset(category_ids):
        raise SemanticReleaseBundleError("profile contains an unknown category")
    if not set(selected_rules).issubset(rule_ids):
        raise SemanticReleaseBundleError("profile contains an unknown rule")
    category_by_rule = {rule["rule_id"]: rule["category_id"] for rule in _RULE_DOCUMENTS}
    if {category_by_rule[rule_id] for rule_id in selected_rules} != set(selected_categories):
        raise SemanticReleaseBundleError("profile category/rule coverage disagrees")
    if coordinate == CORE_SECRETS_PROFILE:
        if set(selected_categories) != category_ids or set(selected_rules) != rule_ids:
            raise SemanticReleaseBundleError("core_secrets.v1 is not complete")
    return CoverageProfile(
        coordinate=coordinate,
        taxonomy_id=_TAXONOMY_ID,
        taxonomy_version=_TAXONOMY_VERSION,
        category_ids=selected_categories,
        rule_ids=selected_rules,
    )


def load_installed_semantic_release_bundle() -> SemanticReleaseBundle:
    """Load and validate the bundle rooted only at the installed package authority."""

    package_root = _installed_package_root()
    manifest_bytes = _read_package_file(
        package_root,
        _MANIFEST_RELATIVE_PATH,
        max_bytes=BUNDLE_MANIFEST_MAX_BYTES,
    )
    _, artifacts = _manifest_document(manifest_bytes)
    inventoried_data = {
        artifact.path for artifact in artifacts if artifact.path.startswith(_DATA_PREFIX)
    }
    if _scan_data_inventory(package_root) != inventoried_data:
        raise SemanticReleaseBundleError(
            "semantic-release data inventory contains missing or unlisted artifacts"
        )

    documents: dict[str, dict[str, object]] = {}
    for artifact in artifacts:
        raw = _read_package_file(
            package_root,
            artifact.path,
            max_bytes=artifact.byte_count,
            expected_mode=artifact.mode,
            expected_byte_count=artifact.byte_count,
            expected_sha256=artifact.sha256,
        )
        if artifact.artifact_kind == "classifier":
            _validate_classifier_artifact(artifact)
        else:
            documents[artifact.path] = _canonical_json_document(raw, artifact.path)

    by_kind: dict[str, list[tuple[BundleArtifact, dict[str, object] | None]]] = {
        kind: [] for kind in _ARTIFACT_KINDS
    }
    for artifact in artifacts:
        by_kind[artifact.artifact_kind].append((artifact, documents.get(artifact.path)))

    abi_artifact, abi_document = by_kind["classifier_abi"][0]
    assert abi_document is not None
    _validate_abi(abi_document, abi_artifact)
    normalization_artifact, normalization_document = by_kind["normalization"][0]
    assert normalization_document is not None
    _validate_normalization(normalization_document, normalization_artifact)
    taxonomy_artifact, taxonomy_document = by_kind["taxonomy"][0]
    assert taxonomy_document is not None
    category_ids = _validate_taxonomy(taxonomy_document, taxonomy_artifact)
    ruleset_artifact, ruleset_document = by_kind["ruleset"][0]
    assert ruleset_document is not None
    rules, excluded_values = _validate_ruleset(ruleset_document, ruleset_artifact, category_ids)
    rule_ids = frozenset(rule.rule_id for rule in rules)
    profiles = tuple(
        _validate_profile(document, artifact, category_ids, rule_ids)
        for artifact, document in by_kind["profile"]
        if document is not None
    )
    profile_coordinates = tuple(profile.coordinate.profile_id for profile in profiles)
    if profile_coordinates != tuple(
        sorted(profile_coordinates, key=lambda value: value.encode("utf-8"))
    ):
        raise SemanticReleaseBundleError("profiles are not utf8_lex_v1 ordered")
    return SemanticReleaseBundle(
        manifest_id=_MANIFEST_ID,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifacts=artifacts,
        profiles=profiles,
        rules=rules,
        excluded_credential_values=excluded_values,
    )


def _valid_field_bytes(value: object) -> bytes | None:
    if not isinstance(value, bytes) or not value or len(value) > FIELD_MAX_BYTES:
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    if text != unicodedata.normalize("NFC", text):
        return None
    if ord(text[0]) in _PINNED_TRIM_CODE_POINTS or ord(text[-1]) in _PINNED_TRIM_CODE_POINTS:
        return None
    if any(
        (ord(character) <= 0x001F and character != "\n")
        or 0x007F <= ord(character) <= 0x009F
        for character in text
    ):
        return None
    return value


def _validated_selected_profiles(
    value: object,
    bundle: SemanticReleaseBundle,
) -> tuple[CoverageProfile, ...] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return None
    if len(value) > MAX_PROFILES:
        return None
    coordinates: list[ProfileCoordinate] = []
    for coordinate in value:
        if not isinstance(coordinate, ProfileCoordinate):
            return None
        try:
            validated = _profile_coordinate(
                coordinate.profile_id,
                coordinate.profile_version,
                "selected profile",
            )
        except SemanticReleaseBundleError:
            return None
        coordinates.append(validated)
    profile_ids = tuple(coordinate.profile_id for coordinate in coordinates)
    if profile_ids != tuple(sorted(profile_ids, key=lambda item: item.encode("utf-8"))):
        return None
    if len(coordinates) != len(set(coordinates)):
        return None
    installed = MappingProxyType({profile.coordinate: profile for profile in bundle.profiles})
    if any(coordinate not in installed for coordinate in coordinates):
        return None
    return tuple(installed[coordinate] for coordinate in coordinates)


def classify_canonical_bytes(
    field_bytes: bytes,
    profile_coordinates: Sequence[ProfileCoordinate],
) -> ClassificationResult:
    """Return deterministic facts for explicit bytes and explicit validated profiles.

    ``NO_MATCH`` is only a matcher fact.  This function never interprets it as
    coverage, safety, a policy disposition, or release authority.
    """

    value = _valid_field_bytes(field_bytes)
    if value is None:
        return _indeterminate()
    try:
        bundle = load_installed_semantic_release_bundle()
    except (SemanticReleaseBundleError, OSError, ValueError, re.error):
        return _indeterminate()
    selected_profiles = _validated_selected_profiles(profile_coordinates, bundle)
    if selected_profiles is None:
        return _indeterminate()
    selected_rule_ids = {
        rule_id for profile in selected_profiles for rule_id in profile.rule_ids
    }
    matched_categories: set[str] = set()
    matched_rules: set[str] = set()
    try:
        for rule in bundle.rules:
            if rule.rule_id not in selected_rule_ids:
                continue
            for match in rule.pattern.finditer(value):
                if rule.credential_group is not None:
                    credential = match.group(rule.credential_group)
                    if credential.translate(_ASCII_LOWER) in bundle.excluded_credential_values:
                        continue
                matched_categories.add(rule.category_id)
                matched_rules.add(rule.rule_id)
    except (IndexError, re.error):
        return _indeterminate()
    if len(matched_categories) > MAX_MATCH_IDS or len(matched_rules) > MAX_MATCH_IDS:
        return _indeterminate()
    if not matched_categories:
        return ClassificationResult("NO_MATCH")
    return ClassificationResult(
        "MATCH",
        tuple(sorted(matched_categories, key=lambda item: item.encode("utf-8"))),
        tuple(sorted(matched_rules, key=lambda item: item.encode("utf-8"))),
    )
