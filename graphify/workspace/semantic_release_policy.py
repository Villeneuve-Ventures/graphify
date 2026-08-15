"""Private P5B2 semantic-release policy-authority persistence.

The store in this module owns only the fixed per-workspace policy-authority
current/previous/pending namespace.  It deliberately exposes no CLI, public
schema, live policy selection, release decision, projection, provider, or
publication behavior.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator, Mapping, cast
import unicodedata
from uuid import UUID

from graphify.workspace.contracts import ContractError, Registry, canonical_json_bytes
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    StateRecoveryRequired,
    Syscalls,
    WORKSPACE_LOCK_RANK,
    WorkspaceRuntimeError,
)
from graphify.workspace.registry import RegistryStore
from graphify.workspace.semantic_release import (
    CORE_SECRETS_PROFILE,
    BundleArtifact,
    SemanticReleaseBundle,
    load_installed_semantic_release_bundle,
)


POLICY_AUTHORITY_CONTRACT = "graphify.workspace.semantic_release_policy_authority.internal"
POLICY_SELECTION_AUTHORIZATION_CONTRACT = (
    "graphify.workspace.semantic_release_policy_selection_authorization.internal"
)
COVERAGE_SUFFICIENCY_CONTRACT = "graphify.workspace.semantic_release_coverage_sufficiency.internal"
SEMANTIC_RELEASE_POLICY_CONTRACT = "graphify.workspace.semantic_release_policy.internal"
POLICY_AUTHORITY_FORMAT_VERSION = 1
SELECT_SEMANTIC_RELEASE_POLICY = "SELECT_SEMANTIC_RELEASE_POLICY"

POLICY_AUTHORITY_RECORD_MAX_BYTES = 64 * 1024
POLICY_SELECTION_AUTHORIZATION_MAX_BYTES = 16 * 1024
POLICY_AUTHORITY_TRANSACTION_PEAK_BYTES = 256 * 1024
POLICY_AUTHORITY_MAX_IDENTIFIER_BYTES = 256
POLICY_AUTHORITY_NAMESPACE_MAX_ENTRIES = 4_096

POLICY_AUTHORITY_CURRENT = "semantic-release-policy-authority.json"
POLICY_AUTHORITY_PREVIOUS = "semantic-release-policy-authority.previous.json"
POLICY_AUTHORITY_PENDING = "semantic-release-policy-authority.pending.json"
_POLICY_AUTHORITY_NAMES = (
    POLICY_AUTHORITY_CURRENT,
    POLICY_AUTHORITY_PREVIOUS,
    POLICY_AUTHORITY_PENDING,
)
_FIELD_TYPES = ("node_label", "node_rationale", "hyperedge_label")
_FIELD_TYPE_ORDER = {name: index for index, name in enumerate(_FIELD_TYPES)}
_DISPOSITIONS = frozenset({"ALLOW_FIELD", "OMIT_RATIONALE", "REJECT_RELEASE"})
_REDUCTION_PRECEDENCE = ("REJECT_RELEASE", "OMIT_RATIONALE", "ALLOW_FIELD")
_COVERAGE_STATES = frozenset({"SUFFICIENT", "INSUFFICIENT"})
_AUTHORITY_STATES = frozenset({"ACTIVE", "REVOKED"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RFC3339_UTC_RE = re.compile(
    r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?Z",
    re.ASCII,
)
_ATOMIC_TEMP_RE = re.compile(
    r"^\.(?P<destination>.+)\.tmp-(?P<pid>[1-9][0-9]*)-(?P<nonce>[0-9a-f]{32})$",
    re.ASCII,
)


class SemanticReleasePolicyAuthorityError(WorkspaceRuntimeError):
    """Base failure for the private policy-authority store."""

    code = "semantic_release_policy_authority_error"


class SemanticReleasePolicyAuthorityInvalid(SemanticReleasePolicyAuthorityError):
    """Structured selection input or canonical authority bytes are invalid."""

    code = "semantic_release_policy_authority_invalid"


class SemanticReleasePolicyAuthorityConflict(SemanticReleasePolicyAuthorityError):
    """The exact revision-and-digest CAS or monotonic chain does not match."""

    code = "semantic_release_policy_authority_conflict"


class SemanticReleasePolicyAuthorityRecoveryRequired(StateRecoveryRequired):
    """A valid pending selection transaction must be recovered exactly."""


def _plain_string(value: object, label: str, *, trimmed: bool = True) -> str:
    if type(value) is not str or not value:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must be a non-empty string")
    text = cast(str, value)
    if trimmed and text.strip() != text:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must be trimmed")
    if unicodedata.normalize("NFC", text) != text:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must already be NFC-normalized")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} contains a Unicode surrogate")
    return text


def _identifier(value: object, label: str) -> str:
    text = _plain_string(value, label)
    if len(text.encode("utf-8")) > POLICY_AUTHORITY_MAX_IDENTIFIER_BYTES:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} exceeds 256 UTF-8 bytes")
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in text):
        raise SemanticReleasePolicyAuthorityInvalid(
            f"{label} contains whitespace or control characters"
        )
    return text


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or cast(int, value) < 1:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must be a positive integer")
    return cast(int, value)


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or cast(int, value) < 0:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must be a nonnegative integer")
    return cast(int, value)


def _sha256(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or _SHA256_RE.fullmatch(cast(str, value)) is None:
        raise SemanticReleasePolicyAuthorityInvalid(
            f"{label} must be 64 lowercase hexadecimal digits"
        )
    return cast(str, value)


def _repo_uuid(value: object) -> str:
    text = _plain_string(value, "repo_uuid")
    try:
        canonical = str(UUID(text))
    except ValueError as exc:
        raise SemanticReleasePolicyAuthorityInvalid("repo_uuid is not a UUID") from exc
    if canonical != text:
        raise SemanticReleasePolicyAuthorityInvalid("repo_uuid is not canonical")
    return text


def _exact_members(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SemanticReleasePolicyAuthorityInvalid(
            f"{label} members must be exactly {', '.join(sorted(expected))}"
        )


def _mapping(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in cast(dict[Any, Any], value)):
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must be a plain object")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} must be an array")
    return cast(list[object], value)


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticReleasePolicyAuthorityInvalid(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SemanticReleasePolicyAuthorityInvalid(f"non-finite JSON number is forbidden: {value}")


def _canonical_object(payload: bytes, *, label: str, max_bytes: int) -> dict[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > max_bytes:
        raise SemanticReleasePolicyAuthorityInvalid(
            f"{label} must be between 1 and {max_bytes} canonical bytes"
        )
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except SemanticReleasePolicyAuthorityInvalid:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} is invalid JSON: {exc}") from exc
    document = _mapping(value, label)
    try:
        canonical = canonical_json_bytes(document)
    except ContractError as exc:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} is not canonical: {exc}") from exc
    if canonical != payload:
        raise SemanticReleasePolicyAuthorityInvalid(f"{label} bytes are not canonical")
    return document


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest_object(value: object) -> str:
    return _digest_bytes(canonical_json_bytes(value))


@dataclass(frozen=True)
class SemanticReleasePolicyProfile:
    profile_id: str
    profile_version: int
    profile_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "profile_id")
        _positive_integer(self.profile_version, "profile_version")
        _sha256(self.profile_sha256, "profile_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_sha256": self.profile_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SemanticReleasePolicyProfile:
        data = _mapping(value, "selected profile")
        _exact_members(data, {"profile_id", "profile_version", "profile_sha256"}, "profile")
        return cls(
            profile_id=_identifier(data["profile_id"], "profile_id"),
            profile_version=_positive_integer(data["profile_version"], "profile_version"),
            profile_sha256=cast(str, _sha256(data["profile_sha256"], "profile_sha256")),
        )


def _validate_profile_order(profiles: tuple[SemanticReleasePolicyProfile, ...]) -> None:
    if type(profiles) is not tuple:
        raise SemanticReleasePolicyAuthorityInvalid("selected_profiles must be a tuple")
    if any(type(profile) is not SemanticReleasePolicyProfile for profile in profiles):
        raise SemanticReleasePolicyAuthorityInvalid(
            "selected_profiles must contain only closed profile coordinates"
        )
    identifiers = tuple(profile.profile_id for profile in profiles)
    if identifiers != tuple(sorted(identifiers, key=lambda item: item.encode("utf-8"))):
        raise SemanticReleasePolicyAuthorityInvalid("selected_profiles are not utf8_lex_v1 ordered")
    if len(identifiers) != len(set(identifiers)):
        raise SemanticReleasePolicyAuthorityInvalid("selected_profiles are duplicated")


@dataclass(frozen=True)
class SemanticReleaseCoverageSufficiency:
    release_context: str
    selected_profiles: tuple[SemanticReleasePolicyProfile, ...]
    coverage_state: str

    def __post_init__(self) -> None:
        _identifier(self.release_context, "coverage.release_context")
        _validate_profile_order(self.selected_profiles)
        if type(self.coverage_state) is not str or self.coverage_state not in _COVERAGE_STATES:
            raise SemanticReleasePolicyAuthorityInvalid(
                "coverage_state must be SUFFICIENT or INSUFFICIENT"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": COVERAGE_SUFFICIENCY_CONTRACT,
            "format_version": POLICY_AUTHORITY_FORMAT_VERSION,
            "release_context": self.release_context,
            "selected_profiles": [profile.to_dict() for profile in self.selected_profiles],
            "coverage_state": self.coverage_state,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return _digest_bytes(self.canonical)

    @classmethod
    def from_mapping(cls, value: object) -> SemanticReleaseCoverageSufficiency:
        data = _mapping(value, "coverage_sufficiency")
        _exact_members(
            data,
            {
                "contract",
                "format_version",
                "release_context",
                "selected_profiles",
                "coverage_state",
            },
            "coverage_sufficiency",
        )
        if (
            data["contract"] != COVERAGE_SUFFICIENCY_CONTRACT
            or _positive_integer(data["format_version"], "coverage.format_version")
            != POLICY_AUTHORITY_FORMAT_VERSION
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "coverage_sufficiency contract or format version is unsupported"
            )
        profiles = tuple(
            SemanticReleasePolicyProfile.from_mapping(item)
            for item in _array(data["selected_profiles"], "coverage.selected_profiles")
        )
        return cls(
            release_context=_identifier(data["release_context"], "coverage.release_context"),
            selected_profiles=profiles,
            coverage_state=_plain_string(data["coverage_state"], "coverage_state"),
        )


@dataclass(frozen=True)
class SemanticReleasePairDisposition:
    field_type: str
    category_id: str
    disposition: str

    def __post_init__(self) -> None:
        if type(self.field_type) is not str or self.field_type not in _FIELD_TYPE_ORDER:
            raise SemanticReleasePolicyAuthorityInvalid("field_type is unsupported")
        _identifier(self.category_id, "category_id")
        if type(self.disposition) is not str or self.disposition not in _DISPOSITIONS:
            raise SemanticReleasePolicyAuthorityInvalid("disposition is unsupported")
        if self.disposition == "OMIT_RATIONALE" and self.field_type != "node_rationale":
            raise SemanticReleasePolicyAuthorityInvalid(
                "OMIT_RATIONALE is valid only for node_rationale"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "field_type": self.field_type,
            "category_id": self.category_id,
            "disposition": self.disposition,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SemanticReleasePairDisposition:
        data = _mapping(value, "pair_disposition")
        _exact_members(data, {"field_type", "category_id", "disposition"}, "pair_disposition")
        return cls(
            field_type=_plain_string(data["field_type"], "field_type"),
            category_id=_identifier(data["category_id"], "category_id"),
            disposition=_plain_string(data["disposition"], "disposition"),
        )


def _validate_pair_order(pairs: tuple[SemanticReleasePairDisposition, ...]) -> None:
    if type(pairs) is not tuple:
        raise SemanticReleasePolicyAuthorityInvalid("pair_dispositions must be a tuple")
    if any(type(pair) is not SemanticReleasePairDisposition for pair in pairs):
        raise SemanticReleasePolicyAuthorityInvalid(
            "pair_dispositions must contain only closed pair records"
        )
    keys = tuple(
        (_FIELD_TYPE_ORDER[pair.field_type], pair.category_id.encode("utf-8")) for pair in pairs
    )
    if keys != tuple(sorted(keys)):
        raise SemanticReleasePolicyAuthorityInvalid(
            "pair_dispositions are not in canonical field/category order"
        )
    if len(keys) != len(set(keys)):
        raise SemanticReleasePolicyAuthorityInvalid("pair_dispositions are duplicated")


@dataclass(frozen=True)
class SemanticReleasePolicy:
    policy_id: str
    policy_version: int
    release_context: str
    coverage_sufficiency_sha256: str
    pair_dispositions: tuple[SemanticReleasePairDisposition, ...]
    reduction_precedence: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _positive_integer(self.policy_version, "policy_version")
        _identifier(self.release_context, "policy.release_context")
        _sha256(self.coverage_sufficiency_sha256, "coverage_sufficiency_sha256")
        _validate_pair_order(self.pair_dispositions)
        if (
            type(self.reduction_precedence) is not tuple
            or any(type(item) is not str for item in self.reduction_precedence)
            or self.reduction_precedence != _REDUCTION_PRECEDENCE
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "reduction_precedence must be REJECT_RELEASE, OMIT_RATIONALE, ALLOW_FIELD"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": SEMANTIC_RELEASE_POLICY_CONTRACT,
            "format_version": POLICY_AUTHORITY_FORMAT_VERSION,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "release_context": self.release_context,
            "coverage_sufficiency_sha256": self.coverage_sufficiency_sha256,
            "pair_dispositions": [pair.to_dict() for pair in self.pair_dispositions],
            "reduction_precedence": list(self.reduction_precedence),
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return _digest_bytes(self.canonical)

    @classmethod
    def from_mapping(cls, value: object) -> SemanticReleasePolicy:
        data = _mapping(value, "policy")
        _exact_members(
            data,
            {
                "contract",
                "format_version",
                "policy_id",
                "policy_version",
                "release_context",
                "coverage_sufficiency_sha256",
                "pair_dispositions",
                "reduction_precedence",
            },
            "policy",
        )
        if (
            data["contract"] != SEMANTIC_RELEASE_POLICY_CONTRACT
            or _positive_integer(data["format_version"], "policy.format_version")
            != POLICY_AUTHORITY_FORMAT_VERSION
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "policy contract or format version is unsupported"
            )
        pairs = tuple(
            SemanticReleasePairDisposition.from_mapping(item)
            for item in _array(data["pair_dispositions"], "pair_dispositions")
        )
        precedence = tuple(
            _plain_string(item, "reduction_precedence")
            for item in _array(data["reduction_precedence"], "reduction_precedence")
        )
        return cls(
            policy_id=_identifier(data["policy_id"], "policy_id"),
            policy_version=_positive_integer(data["policy_version"], "policy_version"),
            release_context=_identifier(data["release_context"], "policy.release_context"),
            coverage_sufficiency_sha256=cast(
                str,
                _sha256(
                    data["coverage_sufficiency_sha256"],
                    "coverage_sufficiency_sha256",
                ),
            ),
            pair_dispositions=pairs,
            reduction_precedence=precedence,
        )


@dataclass(frozen=True)
class SemanticReleasePolicySelectionAuthorization:
    action: str
    issued_at: str
    nonce: str
    operator_id: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.action) is not str or self.action != SELECT_SEMANTIC_RELEASE_POLICY:
            raise SemanticReleasePolicyAuthorityInvalid(
                f"action must be {SELECT_SEMANTIC_RELEASE_POLICY}"
            )
        for name in ("nonce", "operator_id", "reason"):
            _plain_string(getattr(self, name), name)
        issued_at = _plain_string(self.issued_at, "issued_at")
        if _RFC3339_UTC_RE.fullmatch(issued_at) is None:
            raise SemanticReleasePolicyAuthorityInvalid(
                "issued_at must be an RFC 3339 UTC timestamp"
            )
        try:
            datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SemanticReleasePolicyAuthorityInvalid(
                "issued_at must name a real calendar timestamp"
            ) from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "issued_at": self.issued_at,
            "nonce": self.nonce,
            "operator_id": self.operator_id,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SemanticReleasePolicySelectionAuthorization:
        data = _mapping(value, "authorization")
        _exact_members(
            data,
            {"action", "issued_at", "nonce", "operator_id", "reason"},
            "authorization",
        )
        return cls(
            action=_plain_string(data["action"], "action"),
            issued_at=_plain_string(data["issued_at"], "issued_at"),
            nonce=_plain_string(data["nonce"], "nonce"),
            operator_id=_plain_string(data["operator_id"], "operator_id"),
            reason=_plain_string(data["reason"], "reason"),
        )


@dataclass(frozen=True)
class SemanticReleasePolicySelection:
    repo_uuid: str
    expected_authority_revision: int
    expected_authority_sha256: str | None
    release_context: str
    bundle_manifest_sha256: str
    selected_profiles: tuple[SemanticReleasePolicyProfile, ...]
    coverage_sufficiency: SemanticReleaseCoverageSufficiency
    policy_id: str
    policy_version: int
    policy: SemanticReleasePolicy
    authorization: SemanticReleasePolicySelectionAuthorization

    def __post_init__(self) -> None:
        _repo_uuid(self.repo_uuid)
        revision = _nonnegative_integer(
            self.expected_authority_revision,
            "expected_authority_revision",
        )
        digest = _sha256(
            self.expected_authority_sha256,
            "expected_authority_sha256",
            allow_none=True,
        )
        if (revision == 0) != (digest is None):
            raise SemanticReleasePolicyAuthorityInvalid(
                "genesis requires revision 0 and digest null; advancement requires both CAS values"
            )
        _identifier(self.release_context, "release_context")
        _sha256(self.bundle_manifest_sha256, "bundle_manifest_sha256")
        _validate_profile_order(self.selected_profiles)
        if type(self.coverage_sufficiency) is not SemanticReleaseCoverageSufficiency:
            raise SemanticReleasePolicyAuthorityInvalid(
                "coverage_sufficiency must be one closed declaration"
            )
        if self.coverage_sufficiency.release_context != self.release_context:
            raise SemanticReleasePolicyAuthorityInvalid(
                "coverage release_context differs from selection"
            )
        if self.coverage_sufficiency.selected_profiles != self.selected_profiles:
            raise SemanticReleasePolicyAuthorityInvalid(
                "coverage selected_profiles differ from selection"
            )
        _identifier(self.policy_id, "policy_id")
        _positive_integer(self.policy_version, "policy_version")
        if type(self.policy) is not SemanticReleasePolicy:
            raise SemanticReleasePolicyAuthorityInvalid("policy must be one closed policy object")
        if type(self.authorization) is not SemanticReleasePolicySelectionAuthorization:
            raise SemanticReleasePolicyAuthorityInvalid(
                "authorization must be one closed selection authorization"
            )
        if (
            self.policy.policy_id != self.policy_id
            or self.policy.policy_version != self.policy_version
            or self.policy.release_context != self.release_context
            or self.policy.coverage_sufficiency_sha256 != self.coverage_sufficiency.sha256
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "policy coordinates do not bind the selected context and coverage declaration"
            )


@dataclass(frozen=True)
class SemanticReleasePolicySelectionEnvelope:
    authority_body_sha256: str
    authorization: SemanticReleasePolicySelectionAuthorization

    def __post_init__(self) -> None:
        _sha256(self.authority_body_sha256, "authority_body_sha256")
        if type(self.authorization) is not SemanticReleasePolicySelectionAuthorization:
            raise SemanticReleasePolicyAuthorityInvalid(
                "selection envelope authorization is not closed"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": POLICY_SELECTION_AUTHORIZATION_CONTRACT,
            "format_version": POLICY_AUTHORITY_FORMAT_VERSION,
            "authority_body_sha256": self.authority_body_sha256,
            "authorization": self.authorization.to_dict(),
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return _digest_bytes(self.canonical)

    @classmethod
    def from_mapping(cls, value: object) -> SemanticReleasePolicySelectionEnvelope:
        data = _mapping(value, "selection_authorization")
        _exact_members(
            data,
            {"contract", "format_version", "authority_body_sha256", "authorization"},
            "selection_authorization",
        )
        if (
            data["contract"] != POLICY_SELECTION_AUTHORIZATION_CONTRACT
            or _positive_integer(data["format_version"], "authorization.format_version")
            != POLICY_AUTHORITY_FORMAT_VERSION
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "selection authorization contract or format version is unsupported"
            )
        return cls(
            authority_body_sha256=cast(
                str,
                _sha256(data["authority_body_sha256"], "authority_body_sha256"),
            ),
            authorization=SemanticReleasePolicySelectionAuthorization.from_mapping(
                data["authorization"]
            ),
        )


@dataclass(frozen=True)
class SemanticReleasePolicyAuthorityRecord:
    repo_uuid: str
    release_context: str
    authority_revision: int
    previous_authority_sha256: str | None
    state: str
    bundle_manifest_sha256: str
    selected_profiles: tuple[SemanticReleasePolicyProfile, ...]
    coverage_sufficiency: SemanticReleaseCoverageSufficiency
    coverage_sufficiency_sha256: str
    policy_id: str
    policy_version: int
    policy: SemanticReleasePolicy
    policy_sha256: str
    selection_authorization: SemanticReleasePolicySelectionEnvelope
    selection_authorization_sha256: str

    def __post_init__(self) -> None:
        _repo_uuid(self.repo_uuid)
        _identifier(self.release_context, "release_context")
        revision = _positive_integer(self.authority_revision, "authority_revision")
        predecessor = _sha256(
            self.previous_authority_sha256,
            "previous_authority_sha256",
            allow_none=True,
        )
        if (revision == 1) != (predecessor is None):
            raise SemanticReleasePolicyAuthorityInvalid(
                "revision 1 requires a null predecessor and higher revisions require a digest"
            )
        if type(self.state) is not str or self.state not in _AUTHORITY_STATES:
            raise SemanticReleasePolicyAuthorityInvalid("authority state is unsupported")
        _sha256(self.bundle_manifest_sha256, "bundle_manifest_sha256")
        _validate_profile_order(self.selected_profiles)
        if type(self.coverage_sufficiency) is not SemanticReleaseCoverageSufficiency:
            raise SemanticReleasePolicyAuthorityInvalid(
                "authority coverage_sufficiency is not closed"
            )
        if (
            self.coverage_sufficiency.release_context != self.release_context
            or self.coverage_sufficiency.selected_profiles != self.selected_profiles
            or self.coverage_sufficiency.sha256 != self.coverage_sufficiency_sha256
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "coverage declaration or digest differs from the authority body"
            )
        _sha256(self.coverage_sufficiency_sha256, "coverage_sufficiency_sha256")
        _identifier(self.policy_id, "policy_id")
        _positive_integer(self.policy_version, "policy_version")
        if type(self.policy) is not SemanticReleasePolicy:
            raise SemanticReleasePolicyAuthorityInvalid("authority policy is not closed")
        if (
            self.policy.policy_id != self.policy_id
            or self.policy.policy_version != self.policy_version
            or self.policy.release_context != self.release_context
            or self.policy.coverage_sufficiency_sha256 != self.coverage_sufficiency_sha256
            or self.policy.sha256 != self.policy_sha256
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "policy object or digest differs from the authority body"
            )
        _sha256(self.policy_sha256, "policy_sha256")
        if type(self.selection_authorization) is not SemanticReleasePolicySelectionEnvelope:
            raise SemanticReleasePolicyAuthorityInvalid(
                "authority selection_authorization is not closed"
            )
        if self.selection_authorization.authority_body_sha256 != _digest_object(
            self.authority_body()
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "selection authorization does not bind the exact authority body"
            )
        if self.selection_authorization.sha256 != self.selection_authorization_sha256:
            raise SemanticReleasePolicyAuthorityInvalid(
                "selection_authorization_sha256 does not hash the completed envelope"
            )
        _sha256(self.selection_authorization_sha256, "selection_authorization_sha256")
        if len(self.selection_authorization.canonical) > (POLICY_SELECTION_AUTHORIZATION_MAX_BYTES):
            raise SemanticReleasePolicyAuthorityInvalid("selection authorization exceeds 16 KiB")
        if len(self.canonical) > POLICY_AUTHORITY_RECORD_MAX_BYTES:
            raise SemanticReleasePolicyAuthorityInvalid("authority record exceeds 64 KiB")

    def authority_body(self) -> dict[str, object]:
        return {
            "contract": POLICY_AUTHORITY_CONTRACT,
            "format_version": POLICY_AUTHORITY_FORMAT_VERSION,
            "repo_uuid": self.repo_uuid,
            "release_context": self.release_context,
            "authority_revision": self.authority_revision,
            "previous_authority_sha256": self.previous_authority_sha256,
            "state": self.state,
            "bundle_manifest_sha256": self.bundle_manifest_sha256,
            "selected_profiles": [profile.to_dict() for profile in self.selected_profiles],
            "coverage_sufficiency": self.coverage_sufficiency.to_dict(),
            "coverage_sufficiency_sha256": self.coverage_sufficiency_sha256,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy": self.policy.to_dict(),
            "policy_sha256": self.policy_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.authority_body(),
            "selection_authorization": self.selection_authorization.to_dict(),
            "selection_authorization_sha256": self.selection_authorization_sha256,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return _digest_bytes(self.canonical)

    @classmethod
    def from_json(cls, payload: bytes) -> SemanticReleasePolicyAuthorityRecord:
        data = _canonical_object(
            payload,
            label="semantic-release policy authority record",
            max_bytes=POLICY_AUTHORITY_RECORD_MAX_BYTES,
        )
        _exact_members(
            data,
            {
                "contract",
                "format_version",
                "repo_uuid",
                "release_context",
                "authority_revision",
                "previous_authority_sha256",
                "state",
                "bundle_manifest_sha256",
                "selected_profiles",
                "coverage_sufficiency",
                "coverage_sufficiency_sha256",
                "policy_id",
                "policy_version",
                "policy",
                "policy_sha256",
                "selection_authorization",
                "selection_authorization_sha256",
            },
            "policy authority record",
        )
        if (
            data["contract"] != POLICY_AUTHORITY_CONTRACT
            or _positive_integer(data["format_version"], "record.format_version")
            != POLICY_AUTHORITY_FORMAT_VERSION
        ):
            raise SemanticReleasePolicyAuthorityInvalid(
                "policy authority contract or format version is unsupported"
            )
        profiles = tuple(
            SemanticReleasePolicyProfile.from_mapping(item)
            for item in _array(data["selected_profiles"], "selected_profiles")
        )
        record = cls(
            repo_uuid=_repo_uuid(data["repo_uuid"]),
            release_context=_identifier(data["release_context"], "release_context"),
            authority_revision=_positive_integer(data["authority_revision"], "authority_revision"),
            previous_authority_sha256=_sha256(
                data["previous_authority_sha256"],
                "previous_authority_sha256",
                allow_none=True,
            ),
            state=_plain_string(data["state"], "state"),
            bundle_manifest_sha256=cast(
                str,
                _sha256(data["bundle_manifest_sha256"], "bundle_manifest_sha256"),
            ),
            selected_profiles=profiles,
            coverage_sufficiency=SemanticReleaseCoverageSufficiency.from_mapping(
                data["coverage_sufficiency"]
            ),
            coverage_sufficiency_sha256=cast(
                str,
                _sha256(
                    data["coverage_sufficiency_sha256"],
                    "coverage_sufficiency_sha256",
                ),
            ),
            policy_id=_identifier(data["policy_id"], "policy_id"),
            policy_version=_positive_integer(data["policy_version"], "policy_version"),
            policy=SemanticReleasePolicy.from_mapping(data["policy"]),
            policy_sha256=cast(str, _sha256(data["policy_sha256"], "policy_sha256")),
            selection_authorization=SemanticReleasePolicySelectionEnvelope.from_mapping(
                data["selection_authorization"]
            ),
            selection_authorization_sha256=cast(
                str,
                _sha256(
                    data["selection_authorization_sha256"],
                    "selection_authorization_sha256",
                ),
            ),
        )
        if record.canonical != payload:
            raise SemanticReleasePolicyAuthorityInvalid(
                "record did not round-trip byte-identically"
            )
        return record


def _build_record(
    request: SemanticReleasePolicySelection,
) -> SemanticReleasePolicyAuthorityRecord:
    revision = request.expected_authority_revision + 1
    body = {
        "contract": POLICY_AUTHORITY_CONTRACT,
        "format_version": POLICY_AUTHORITY_FORMAT_VERSION,
        "repo_uuid": request.repo_uuid,
        "release_context": request.release_context,
        "authority_revision": revision,
        "previous_authority_sha256": request.expected_authority_sha256,
        "state": "ACTIVE",
        "bundle_manifest_sha256": request.bundle_manifest_sha256,
        "selected_profiles": [profile.to_dict() for profile in request.selected_profiles],
        "coverage_sufficiency": request.coverage_sufficiency.to_dict(),
        "coverage_sufficiency_sha256": request.coverage_sufficiency.sha256,
        "policy_id": request.policy_id,
        "policy_version": request.policy_version,
        "policy": request.policy.to_dict(),
        "policy_sha256": request.policy.sha256,
    }
    envelope = SemanticReleasePolicySelectionEnvelope(
        authority_body_sha256=_digest_object(body),
        authorization=request.authorization,
    )
    return SemanticReleasePolicyAuthorityRecord(
        repo_uuid=request.repo_uuid,
        release_context=request.release_context,
        authority_revision=revision,
        previous_authority_sha256=request.expected_authority_sha256,
        state="ACTIVE",
        bundle_manifest_sha256=request.bundle_manifest_sha256,
        selected_profiles=request.selected_profiles,
        coverage_sufficiency=request.coverage_sufficiency,
        coverage_sufficiency_sha256=request.coverage_sufficiency.sha256,
        policy_id=request.policy_id,
        policy_version=request.policy_version,
        policy=request.policy,
        policy_sha256=request.policy.sha256,
        selection_authorization=envelope,
        selection_authorization_sha256=envelope.sha256,
    )


@dataclass(frozen=True)
class _AuthoritySnapshot:
    current_bytes: bytes | None
    previous_bytes: bytes | None
    pending_bytes: bytes | None
    current: SemanticReleasePolicyAuthorityRecord | None
    previous: SemanticReleasePolicyAuthorityRecord | None
    pending: SemanticReleasePolicyAuthorityRecord | None

    @property
    def byte_tuple(self) -> tuple[bytes | None, bytes | None, bytes | None]:
        return (self.current_bytes, self.previous_bytes, self.pending_bytes)


@dataclass(frozen=True)
class _RecoveryPlan:
    phase: str
    candidate: SemanticReleasePolicyAuthorityRecord | None
    predecessor_bytes: bytes | None = None
    retain_predecessor: bool = False
    install_current: bool = False
    clear_pending: bool = False

    @property
    def requires_recovery(self) -> bool:
        return self.retain_predecessor or self.install_current or self.clear_pending


@dataclass
class _CommitVisibility:
    pending_may_be_visible: bool = False


@dataclass(frozen=True)
class SemanticReleasePolicyAuthorityRecovery:
    """Read-only exact-prefix projection for one policy-authority transaction."""

    phase: str
    current: SemanticReleasePolicyAuthorityRecord | None
    previous: SemanticReleasePolicyAuthorityRecord | None
    pending: SemanticReleasePolicyAuthorityRecord | None
    orphan_temporary: bool
    requires_recovery: bool


class SemanticReleasePolicyAuthorityStore:
    """Own the fixed private current/previous/pending policy-authority records."""

    def __init__(
        self,
        state_root: Path,
        registry: RegistryStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.registry = registry
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        if self.state.root != registry.state.root:
            raise SemanticReleasePolicyAuthorityInvalid(
                "policy-authority and registry stores must share one external state root"
            )

    @staticmethod
    def _directory(repo_uuid: str) -> Path:
        return Path("workspaces") / _repo_uuid(repo_uuid)

    @classmethod
    def _record_paths(cls, repo_uuid: str) -> tuple[Path, Path, Path]:
        directory = cls._directory(repo_uuid)
        return (
            directory / POLICY_AUTHORITY_CURRENT,
            directory / POLICY_AUTHORITY_PREVIOUS,
            directory / POLICY_AUTHORITY_PENDING,
        )

    @contextmanager
    def _workspace_lock(
        self,
        repo_uuid: str,
        *,
        exclusive: bool,
        deadline_ns: int | None,
    ) -> Iterator[None]:
        lock_path = self._directory(repo_uuid) / "workspace.lock"
        if exclusive:
            with self.state.lock(
                lock_path,
                rank=WORKSPACE_LOCK_RANK,
                name="workspace",
                deadline_ns=deadline_ns,
            ):
                yield
            return
        with self.state.existing_lock(
            lock_path,
            rank=WORKSPACE_LOCK_RANK,
            name="workspace",
            exclusive=False,
            deadline_ns=deadline_ns,
            kind="workspace",
        ):
            yield

    @staticmethod
    def _require_registered(document: Registry, repo_uuid: str) -> None:
        matches = [
            entry for entry in document.to_dict()["workspaces"] if entry["repo_uuid"] == repo_uuid
        ]
        if len(matches) != 1:
            raise SemanticReleasePolicyAuthorityConflict(
                f"registry has no singular entry for {repo_uuid}"
            )

    def _registry_locked(
        self,
        *,
        deadline_ns: int | None,
    ) -> Registry:
        document = self.registry._load_locked(
            recover=False,
            deadline_ns=deadline_ns,
        )
        if document is None:  # pragma: no cover - registry does not allow missing here
            raise StateCorrupt("registry current record is missing")
        return document

    @staticmethod
    def _decode_record(payload: bytes, label: str) -> SemanticReleasePolicyAuthorityRecord:
        try:
            return SemanticReleasePolicyAuthorityRecord.from_json(payload)
        except SemanticReleasePolicyAuthorityInvalid as exc:
            raise StateCorrupt(f"{label} is invalid: {exc}") from exc

    def _read_snapshot(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None,
    ) -> _AuthoritySnapshot:
        current_path, previous_path, pending_path = self._record_paths(repo_uuid)

        def read(path: Path) -> bytes | None:
            return self.state.read_optional_existing_bytes(
                path,
                max_bytes=POLICY_AUTHORITY_RECORD_MAX_BYTES,
                deadline_ns=deadline_ns,
            )

        current_bytes = read(current_path)
        previous_bytes = read(previous_path)
        pending_bytes = read(pending_path)
        return _AuthoritySnapshot(
            current_bytes=current_bytes,
            previous_bytes=previous_bytes,
            pending_bytes=pending_bytes,
            current=(
                None
                if current_bytes is None
                else self._decode_record(current_bytes, "current policy authority")
            ),
            previous=(
                None
                if previous_bytes is None
                else self._decode_record(previous_bytes, "previous policy authority")
            ),
            pending=(
                None
                if pending_bytes is None
                else self._decode_record(pending_bytes, "pending policy authority")
            ),
        )

    def _namespace_preflight(
        self,
        repo_uuid: str,
        *,
        require_no_temporary: bool,
        require_reserve: bool,
        deadline_ns: int | None,
    ) -> tuple[Path, ...]:
        directory = self._directory(repo_uuid)
        store_names: list[str] = []
        with self.state.existing_private_directory(directory) as descriptor:
            try:
                for index, entry in enumerate(os.scandir(descriptor), start=1):
                    if index > POLICY_AUTHORITY_NAMESPACE_MAX_ENTRIES:
                        raise StatePathError(
                            "workspace directory exceeds the bounded authority namespace scan"
                        )
                    name = entry.name
                    if name in _POLICY_AUTHORITY_NAMES:
                        store_names.append(name)
                        continue
                    match = _ATOMIC_TEMP_RE.fullmatch(name)
                    if match is not None and match.group("destination") in (
                        _POLICY_AUTHORITY_NAMES
                    ):
                        store_names.append(name)
                        continue
                    if name.startswith("semantic-release-policy-authority") or name.startswith(
                        ".semantic-release-policy-authority"
                    ):
                        raise StatePathError(f"unexpected policy-authority namespace entry: {name}")
                if require_reserve:
                    try:
                        filesystem = os.fstatvfs(descriptor)
                    except OSError as exc:
                        raise StatePathError(
                            "policy-authority filesystem reserve cannot be inspected"
                        ) from exc
                    available = filesystem.f_bavail * filesystem.f_frsize
                    if available < POLICY_AUTHORITY_TRANSACTION_PEAK_BYTES:
                        raise SemanticReleasePolicyAuthorityInvalid(
                            "policy-authority filesystem reserve cannot preserve the 256 KiB peak"
                        )
            except OSError as exc:
                raise StatePathError(
                    "policy-authority namespace cannot be enumerated safely"
                ) from exc

        temporaries: list[Path] = []
        for destination in _POLICY_AUTHORITY_NAMES:
            temporaries.extend(
                self.state.inspect_atomic_temps(
                    directory,
                    destination_name=destination,
                    deadline_ns=deadline_ns,
                )
            )
        if len(temporaries) > 1:
            raise StatePathError("policy-authority namespace contains multiple atomic temporaries")
        expected_store_names = set(_POLICY_AUTHORITY_NAMES) | {path.name for path in temporaries}
        if any(name not in expected_store_names for name in store_names):
            raise StatePathError("policy-authority namespace contains an unrecognized entry")
        if require_no_temporary and temporaries:
            raise SemanticReleasePolicyAuthorityRecoveryRequired(
                "policy-authority namespace contains an orphan atomic temporary"
            )
        if POLICY_AUTHORITY_RECORD_MAX_BYTES * 4 != POLICY_AUTHORITY_TRANSACTION_PEAK_BYTES:
            raise RuntimeError("policy-authority transaction peak invariant is broken")
        return tuple(temporaries)

    @staticmethod
    def _profile_artifacts(bundle: SemanticReleaseBundle) -> dict[str, BundleArtifact]:
        return {
            artifact.artifact_id: artifact
            for artifact in bundle.artifacts
            if artifact.artifact_kind == "profile"
        }

    @classmethod
    def _validate_record_against_bundle(
        cls,
        record: SemanticReleasePolicyAuthorityRecord,
        bundle: SemanticReleaseBundle,
    ) -> None:
        if record.bundle_manifest_sha256 != bundle.manifest_sha256:
            raise StateCorrupt("policy authority does not bind the installed bundle manifest")
        artifacts = cls._profile_artifacts(bundle)
        expected_profiles: list[SemanticReleasePolicyProfile] = []
        selected_categories: set[str] = set()
        profiles_by_id = {profile.coordinate.profile_id: profile for profile in bundle.profiles}
        for selected in record.selected_profiles:
            artifact = artifacts.get(selected.profile_id)
            profile = profiles_by_id.get(selected.profile_id)
            if (
                artifact is None
                or profile is None
                or artifact.artifact_version != selected.profile_version
                or artifact.sha256 != selected.profile_sha256
                or profile.coordinate.profile_version != selected.profile_version
            ):
                raise StateCorrupt(
                    f"selected profile does not equal installed manifest bytes: {selected.profile_id}"
                )
            expected_profiles.append(
                SemanticReleasePolicyProfile(
                    selected.profile_id,
                    selected.profile_version,
                    selected.profile_sha256,
                )
            )
            selected_categories.update(profile.category_ids)
        if tuple(expected_profiles) != record.selected_profiles:
            raise StateCorrupt("selected profile coordinates are not exact")
        if record.coverage_sufficiency.coverage_state == "SUFFICIENT" and not any(
            profile.profile_id == CORE_SECRETS_PROFILE.profile_id
            and profile.profile_version == CORE_SECRETS_PROFILE.profile_version
            for profile in record.selected_profiles
        ):
            raise StateCorrupt("SUFFICIENT coverage requires the exact core_secrets.v1 profile")
        for pair in record.policy.pair_dispositions:
            if pair.category_id not in selected_categories:
                raise StateCorrupt(
                    f"policy disposition is not covered by a selected profile: {pair.category_id}"
                )

    @classmethod
    def _validate_snapshot_records(
        cls,
        snapshot: _AuthoritySnapshot,
        bundle: SemanticReleaseBundle,
        repo_uuid: str,
    ) -> None:
        for record in (snapshot.current, snapshot.previous, snapshot.pending):
            if record is None:
                continue
            if record.repo_uuid != repo_uuid:
                raise StateCorrupt("policy-authority record belongs to a different repository")
            cls._validate_record_against_bundle(record, bundle)

    @staticmethod
    def _validate_stable_snapshot(
        snapshot: _AuthoritySnapshot,
    ) -> SemanticReleasePolicyAuthorityRecord | None:
        if snapshot.pending is not None:
            raise SemanticReleasePolicyAuthorityRecoveryRequired(
                "policy authority has an unresolved pending transaction"
            )
        current = snapshot.current
        previous = snapshot.previous
        if current is None:
            if previous is not None:
                raise StateCorrupt("previous policy authority exists without current")
            return None
        if current.authority_revision == 1:
            if previous is not None:
                raise StateCorrupt("revision 1 policy authority must not retain previous")
            return current
        if previous is None or snapshot.previous_bytes is None:
            raise StateCorrupt("policy authority predecessor is missing")
        if (
            previous.authority_revision != current.authority_revision - 1
            or previous.sha256 != current.previous_authority_sha256
            or _digest_bytes(snapshot.previous_bytes) != current.previous_authority_sha256
        ):
            raise StateCorrupt("policy authority predecessor chain is divergent")
        if current.state == "ACTIVE" and previous.state == "REVOKED":
            raise StateCorrupt("ACTIVE policy authority cannot reactivate a REVOKED predecessor")
        return current

    @classmethod
    def _recovery_plan(
        cls,
        snapshot: _AuthoritySnapshot,
    ) -> _RecoveryPlan:
        pending = snapshot.pending
        if pending is None:
            current = cls._validate_stable_snapshot(snapshot)
            return _RecoveryPlan("ABSENT" if current is None else "STABLE", current)
        if pending.state != "ACTIVE" or snapshot.pending_bytes is None:
            raise StateCorrupt("pending policy authority is not an ACTIVE selection")
        if pending.authority_revision == 1:
            if snapshot.previous is not None:
                raise StateCorrupt("pending genesis has an unexpected previous record")
            if snapshot.current is None:
                return _RecoveryPlan(
                    "PENDING_GENESIS",
                    pending,
                    install_current=True,
                    clear_pending=True,
                )
            if snapshot.current_bytes == snapshot.pending_bytes:
                return _RecoveryPlan(
                    "PENDING_GENESIS_CURRENT",
                    pending,
                    clear_pending=True,
                )
            raise StateCorrupt("pending genesis conflicts with current policy authority")

        predecessor_digest = pending.previous_authority_sha256
        if predecessor_digest is None:
            raise StateCorrupt("pending advancement has no predecessor digest")
        if snapshot.current_bytes == snapshot.pending_bytes:
            predecessor = snapshot.previous
            if (
                predecessor is None
                or snapshot.previous_bytes is None
                or predecessor.state != "ACTIVE"
                or predecessor.authority_revision != pending.authority_revision - 1
                or predecessor.sha256 != predecessor_digest
                or _digest_bytes(snapshot.previous_bytes) != predecessor_digest
            ):
                raise StateCorrupt(
                    "already-current pending policy authority lacks its exact predecessor"
                )
            return _RecoveryPlan(
                "PENDING_CURRENT",
                pending,
                predecessor_bytes=snapshot.previous_bytes,
                clear_pending=True,
            )

        predecessor = snapshot.current
        if (
            predecessor is None
            or snapshot.current_bytes is None
            or predecessor.state != "ACTIVE"
            or predecessor.authority_revision != pending.authority_revision - 1
            or predecessor.sha256 != predecessor_digest
            or _digest_bytes(snapshot.current_bytes) != predecessor_digest
        ):
            raise StateCorrupt("pending policy authority does not advance the exact current record")
        if snapshot.previous_bytes == snapshot.current_bytes:
            return _RecoveryPlan(
                "PENDING_AFTER_RETENTION",
                pending,
                predecessor_bytes=snapshot.current_bytes,
                install_current=True,
                clear_pending=True,
            )
        stable_predecessor = _AuthoritySnapshot(
            current_bytes=snapshot.current_bytes,
            previous_bytes=snapshot.previous_bytes,
            pending_bytes=None,
            current=snapshot.current,
            previous=snapshot.previous,
            pending=None,
        )
        cls._validate_stable_snapshot(stable_predecessor)
        return _RecoveryPlan(
            "PENDING_BEFORE_RETENTION",
            pending,
            predecessor_bytes=snapshot.current_bytes,
            retain_predecessor=True,
            install_current=True,
            clear_pending=True,
        )

    @classmethod
    def _selection_action(
        cls,
        snapshot: _AuthoritySnapshot,
        request: SemanticReleasePolicySelection,
        candidate: SemanticReleasePolicyAuthorityRecord,
    ) -> str:
        current = cls._validate_stable_snapshot(snapshot)
        if snapshot.current_bytes == candidate.canonical:
            return "REPLAY"
        if request.expected_authority_revision == 0:
            if current is not None or snapshot.previous is not None:
                raise SemanticReleasePolicyAuthorityConflict(
                    "genesis requires current, previous, and pending to be absent"
                )
            return "COMMIT"
        if (
            current is None
            or snapshot.current_bytes is None
            or current.state != "ACTIVE"
            or current.authority_revision != request.expected_authority_revision
            or current.sha256 != request.expected_authority_sha256
            or _digest_bytes(snapshot.current_bytes) != request.expected_authority_sha256
        ):
            raise SemanticReleasePolicyAuthorityConflict(
                "expected policy-authority revision and complete-record digest do not match"
            )
        return "COMMIT"

    @staticmethod
    def _bundle() -> SemanticReleaseBundle:
        return load_installed_semantic_release_bundle()

    @classmethod
    def _validate_request_bundle(
        cls,
        request: SemanticReleasePolicySelection,
        candidate: SemanticReleasePolicyAuthorityRecord,
        bundle: SemanticReleaseBundle,
    ) -> None:
        if request.bundle_manifest_sha256 != bundle.manifest_sha256:
            raise SemanticReleasePolicyAuthorityInvalid(
                "selection bundle_manifest_sha256 does not name the installed bundle"
            )
        try:
            cls._validate_record_against_bundle(candidate, bundle)
        except StateCorrupt as exc:
            raise SemanticReleasePolicyAuthorityInvalid(str(exc)) from exc

    def read(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> SemanticReleasePolicyAuthorityRecord | None:
        canonical_uuid = _repo_uuid(repo_uuid)
        with self.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
            self._require_registered(registry, canonical_uuid)
            with self._workspace_lock(
                canonical_uuid,
                exclusive=False,
                deadline_ns=deadline_ns,
            ):
                bundle = self._bundle()
                self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=True,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                first = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                self._validate_snapshot_records(first, bundle, canonical_uuid)
                record = self._validate_stable_snapshot(first)
                final_bundle = self._bundle()
                final = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                self._require_registered(registry, canonical_uuid)
                self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=True,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                if (
                    final.byte_tuple != first.byte_tuple
                    or final_bundle.manifest_bytes != bundle.manifest_bytes
                ):
                    raise StateCorrupt("policy-authority stable read did not revalidate")
                self._validate_snapshot_records(final, final_bundle, canonical_uuid)
                self._validate_stable_snapshot(final)
                return record

    def project_recovery(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> SemanticReleasePolicyAuthorityRecovery:
        canonical_uuid = _repo_uuid(repo_uuid)
        with self.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
            self._require_registered(registry, canonical_uuid)
            with self._workspace_lock(
                canonical_uuid,
                exclusive=False,
                deadline_ns=deadline_ns,
            ):
                bundle = self._bundle()
                temporaries = self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=False,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                first = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                self._validate_snapshot_records(first, bundle, canonical_uuid)
                plan = self._recovery_plan(first)
                final_bundle = self._bundle()
                final = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                final_temporaries = self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=False,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                self._require_registered(registry, canonical_uuid)
                if (
                    final.byte_tuple != first.byte_tuple
                    or final_temporaries != temporaries
                    or final_bundle.manifest_bytes != bundle.manifest_bytes
                ):
                    raise StateCorrupt("policy-authority recovery projection did not revalidate")
                self._validate_snapshot_records(final, final_bundle, canonical_uuid)
                self._recovery_plan(final)
                return SemanticReleasePolicyAuthorityRecovery(
                    phase=plan.phase,
                    current=first.current,
                    previous=first.previous,
                    pending=first.pending,
                    orphan_temporary=bool(temporaries),
                    requires_recovery=plan.requires_recovery or bool(temporaries),
                )

    def select(
        self,
        request: SemanticReleasePolicySelection,
        *,
        deadline_ns: int | None = None,
    ) -> SemanticReleasePolicyAuthorityRecord:
        if type(request) is not SemanticReleasePolicySelection:
            raise SemanticReleasePolicyAuthorityInvalid(
                "selection must be a closed SemanticReleasePolicySelection"
            )
        initial_bundle = self._bundle()
        initial_candidate = _build_record(request)
        self._validate_request_bundle(request, initial_candidate, initial_bundle)

        visibility = _CommitVisibility()
        try:
            return self._select_locked(
                request,
                initial_bundle=initial_bundle,
                initial_candidate=initial_candidate,
                visibility=visibility,
                deadline_ns=deadline_ns,
            )
        except CommitUnknown:
            raise
        except BaseException as exc:
            if visibility.pending_may_be_visible:
                raise CommitUnknown(
                    "policy-authority selection outcome is uncertain after pending visibility"
                ) from exc
            raise

    def _select_locked(
        self,
        request: SemanticReleasePolicySelection,
        *,
        initial_bundle: SemanticReleaseBundle,
        initial_candidate: SemanticReleasePolicyAuthorityRecord,
        visibility: _CommitVisibility,
        deadline_ns: int | None,
    ) -> SemanticReleasePolicyAuthorityRecord:

        with self.registry.exclusive_lock(deadline_ns=deadline_ns):
            registry = self._registry_locked(deadline_ns=deadline_ns)
            self._require_registered(registry, request.repo_uuid)
            with self._workspace_lock(
                request.repo_uuid,
                exclusive=True,
                deadline_ns=deadline_ns,
            ):
                bundle = self._bundle()
                candidate = _build_record(request)
                self._validate_request_bundle(request, candidate, bundle)
                if (
                    candidate.canonical != initial_candidate.canonical
                    or bundle.manifest_bytes != initial_bundle.manifest_bytes
                ):
                    raise SemanticReleasePolicyAuthorityConflict(
                        "selection candidate or installed bundle changed before locking"
                    )
                self._namespace_preflight(
                    request.repo_uuid,
                    require_no_temporary=True,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                first = self._read_snapshot(request.repo_uuid, deadline_ns=deadline_ns)
                self._validate_snapshot_records(first, bundle, request.repo_uuid)
                action = self._selection_action(first, request, candidate)

                final_registry = self._registry_locked(deadline_ns=deadline_ns)
                self._require_registered(final_registry, request.repo_uuid)
                final_bundle = self._bundle()
                final_candidate = _build_record(request)
                final_snapshot = self._read_snapshot(
                    request.repo_uuid,
                    deadline_ns=deadline_ns,
                )
                self._namespace_preflight(
                    request.repo_uuid,
                    require_no_temporary=True,
                    require_reserve=action == "COMMIT",
                    deadline_ns=deadline_ns,
                )
                if (
                    final_registry.canonical != registry.canonical
                    or final_bundle.manifest_bytes != bundle.manifest_bytes
                    or final_candidate.canonical != candidate.canonical
                    or final_snapshot.byte_tuple != first.byte_tuple
                ):
                    raise SemanticReleasePolicyAuthorityConflict(
                        "policy-authority selection inputs changed before commit"
                    )
                self._validate_request_bundle(request, final_candidate, final_bundle)
                self._validate_snapshot_records(
                    final_snapshot,
                    final_bundle,
                    request.repo_uuid,
                )
                final_action = self._selection_action(final_snapshot, request, final_candidate)
                if final_action != action:
                    raise SemanticReleasePolicyAuthorityConflict(
                        "policy-authority selection outcome changed before commit"
                    )
                if action == "REPLAY":
                    if final_snapshot.current is None:
                        raise StateCorrupt("policy-authority replay has no current record")
                    return final_snapshot.current

                current, previous, pending = self._record_paths(request.repo_uuid)
                try:
                    committed = self.state.commit_record(
                        label="semantic-release-policy-authority",
                        current=current,
                        previous=previous,
                        pending=pending,
                        payload=final_candidate.canonical,
                        decoder=SemanticReleasePolicyAuthorityRecord.from_json,
                        cleanup_parent_atomic_temps=False,
                        deadline_ns=deadline_ns,
                    )
                except CommitUnknown:
                    visibility.pending_may_be_visible = True
                    raise
                visibility.pending_may_be_visible = True
                reopened_bundle = self._bundle()
                reopened = self._read_snapshot(request.repo_uuid, deadline_ns=deadline_ns)
                self._namespace_preflight(
                    request.repo_uuid,
                    require_no_temporary=True,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                self._validate_snapshot_records(reopened, reopened_bundle, request.repo_uuid)
                stable = self._validate_stable_snapshot(reopened)
                if (
                    stable is None
                    or reopened.current_bytes != final_candidate.canonical
                    or stable.sha256 != final_candidate.sha256
                    or committed.canonical != final_candidate.canonical
                ):
                    raise CommitUnknown(
                        "policy-authority commit could not prove the exact completed outcome"
                    )
                return stable

    def recover(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> SemanticReleasePolicyAuthorityRecord | None:
        canonical_uuid = _repo_uuid(repo_uuid)
        visibility = _CommitVisibility()
        try:
            return self._recover_locked(
                canonical_uuid,
                visibility=visibility,
                deadline_ns=deadline_ns,
            )
        except CommitUnknown:
            raise
        except BaseException as exc:
            if visibility.pending_may_be_visible:
                raise CommitUnknown(
                    "policy-authority recovery outcome is uncertain after pending visibility"
                ) from exc
            raise

    def _recover_locked(
        self,
        canonical_uuid: str,
        *,
        visibility: _CommitVisibility,
        deadline_ns: int | None,
    ) -> SemanticReleasePolicyAuthorityRecord | None:
        with self.registry.exclusive_lock(deadline_ns=deadline_ns):
            registry = self._registry_locked(deadline_ns=deadline_ns)
            self._require_registered(registry, canonical_uuid)
            with self._workspace_lock(
                canonical_uuid,
                exclusive=True,
                deadline_ns=deadline_ns,
            ):
                bundle = self._bundle()
                temporaries = self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=False,
                    require_reserve=True,
                    deadline_ns=deadline_ns,
                )
                initial = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                self._validate_snapshot_records(initial, bundle, canonical_uuid)
                self._recovery_plan(initial)
                if temporaries:
                    destination_match = _ATOMIC_TEMP_RE.fullmatch(temporaries[0].name)
                    if destination_match is None:
                        raise StatePathError("policy-authority temporary name is not canonical")
                    try:
                        self.state.cleanup_atomic_temps(
                            self._directory(canonical_uuid),
                            destination_name=destination_match.group("destination"),
                            deadline_ns=deadline_ns,
                        )
                    except BaseException as exc:
                        if initial.pending is not None:
                            visibility.pending_may_be_visible = True
                            raise CommitUnknown(
                                "policy-authority recovery temporary cleanup is uncertain"
                            ) from exc
                        raise

                self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=True,
                    require_reserve=True,
                    deadline_ns=deadline_ns,
                )
                registry_before = self._registry_locked(deadline_ns=deadline_ns)
                self._require_registered(registry_before, canonical_uuid)
                bundle_before = self._bundle()
                snapshot = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                self._validate_snapshot_records(snapshot, bundle_before, canonical_uuid)
                plan = self._recovery_plan(snapshot)
                if (
                    registry_before.canonical != registry.canonical
                    or bundle_before.manifest_bytes != bundle.manifest_bytes
                    or snapshot.byte_tuple != initial.byte_tuple
                ):
                    raise SemanticReleasePolicyAuthorityConflict(
                        "policy-authority recovery inputs changed before mutation"
                    )
                if not plan.requires_recovery:
                    return plan.candidate
                if plan.candidate is None:
                    raise StateCorrupt("policy-authority recovery plan has no candidate")
                candidate = plan.candidate
                current, previous, pending = self._record_paths(canonical_uuid)

                def mutate() -> None:
                    if plan.retain_predecessor:
                        if plan.predecessor_bytes is None:
                            raise StateCorrupt("policy-authority recovery has no predecessor bytes")
                        self.state.atomic_replace_bytes(
                            previous,
                            plan.predecessor_bytes,
                            label="semantic-release-policy-authority:recovery_previous",
                            deadline_ns=deadline_ns,
                        )
                    if plan.install_current:
                        self.state.atomic_replace_bytes(
                            current,
                            candidate.canonical,
                            label="semantic-release-policy-authority:recovery_current",
                            deadline_ns=deadline_ns,
                        )
                    before_clear = self._read_snapshot(
                        canonical_uuid,
                        deadline_ns=deadline_ns,
                    )
                    self._validate_snapshot_records(
                        before_clear,
                        self._bundle(),
                        canonical_uuid,
                    )
                    before_clear_plan = self._recovery_plan(before_clear)
                    if (
                        before_clear_plan.phase
                        not in {"PENDING_GENESIS_CURRENT", "PENDING_CURRENT"}
                        or before_clear.current_bytes != candidate.canonical
                        or before_clear.pending_bytes != candidate.canonical
                    ):
                        raise StateCorrupt(
                            "policy-authority recovery did not reach the exact clearable prefix"
                        )
                    self.state.unlink_and_sync(
                        pending,
                        label="semantic-release-policy-authority:recovery_pending",
                        deadline_ns=deadline_ns,
                    )

                visibility.pending_may_be_visible = True
                try:
                    mutate()
                except CommitUnknown:
                    raise
                except BaseException as exc:
                    raise CommitUnknown(
                        "policy-authority recovery remains uncertain after pending visibility"
                    ) from exc

                final_registry = self._registry_locked(deadline_ns=deadline_ns)
                self._require_registered(final_registry, canonical_uuid)
                final_bundle = self._bundle()
                final = self._read_snapshot(canonical_uuid, deadline_ns=deadline_ns)
                self._namespace_preflight(
                    canonical_uuid,
                    require_no_temporary=True,
                    require_reserve=False,
                    deadline_ns=deadline_ns,
                )
                self._validate_snapshot_records(final, final_bundle, canonical_uuid)
                stable = self._validate_stable_snapshot(final)
                if (
                    final_registry.canonical != registry.canonical
                    or final_bundle.manifest_bytes != bundle.manifest_bytes
                    or stable is None
                    or final.current_bytes != candidate.canonical
                ):
                    raise CommitUnknown(
                        "policy-authority recovery could not prove the exact completed outcome"
                    )
                return stable


__all__ = [
    "COVERAGE_SUFFICIENCY_CONTRACT",
    "POLICY_AUTHORITY_CONTRACT",
    "POLICY_AUTHORITY_CURRENT",
    "POLICY_AUTHORITY_FORMAT_VERSION",
    "POLICY_AUTHORITY_PENDING",
    "POLICY_AUTHORITY_PREVIOUS",
    "POLICY_AUTHORITY_RECORD_MAX_BYTES",
    "POLICY_AUTHORITY_TRANSACTION_PEAK_BYTES",
    "POLICY_SELECTION_AUTHORIZATION_CONTRACT",
    "POLICY_SELECTION_AUTHORIZATION_MAX_BYTES",
    "SELECT_SEMANTIC_RELEASE_POLICY",
    "SEMANTIC_RELEASE_POLICY_CONTRACT",
    "SemanticReleaseCoverageSufficiency",
    "SemanticReleasePairDisposition",
    "SemanticReleasePolicy",
    "SemanticReleasePolicyAuthorityConflict",
    "SemanticReleasePolicyAuthorityError",
    "SemanticReleasePolicyAuthorityInvalid",
    "SemanticReleasePolicyAuthorityRecord",
    "SemanticReleasePolicyAuthorityRecovery",
    "SemanticReleasePolicyAuthorityRecoveryRequired",
    "SemanticReleasePolicyAuthorityStore",
    "SemanticReleasePolicyProfile",
    "SemanticReleasePolicySelection",
    "SemanticReleasePolicySelectionAuthorization",
    "SemanticReleasePolicySelectionEnvelope",
]
