"""Canonical P1 reference models for the Graphify workspace contracts.

The JSON Schemas shipped beside this module are normative.  These frozen
reference models provide a dependency-free implementation of the invariants
that matter for hashing, version rejection, round trips, and journal framing.
They do not read or mutate a real registry, generation, lease, pointer, source
checkout, service, or global installation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import calendar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import unicodedata
from urllib.parse import urlsplit, urlunsplit
import uuid
from typing import Any, ClassVar, TypeVar, cast, overload

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # pyright: ignore[reportMissingImports]


WORKSPACE_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
ADAPTER_CONTRACT_VERSION = 1
CLI_CONTRACT_VERSION = 1
ENGINE_BASELINE = "0.9.16"
CANDIDATE_DISTRIBUTION_VERSION = "0.9.16+workspace.1"
EXTRACTOR_CACHE_ABI = "graphify-0.9.16"
UPSTREAM_BASELINE_COMMIT = "a0e4a1c6bd3a99edfdd84ad30927003f51face6a"
REQUIRED_COMPATIBILITY_ARTIFACTS = (
    "contract-bundle.zip",
    "fixture-bundle.zip",
    "fixture-manifest.json",
    f"graphifyy-{CANDIDATE_DISTRIBUTION_VERSION}-py3-none-any.whl",
    "offline-rollback.zip",
    "provenance.json",
    "runtime-bundle.zip",
    "runtime-requirements.txt",
    "sbom.cdx.json",
    "skill-bundle.zip",
)
WORKSPACE_SCHEMA_FILES = (
    "common.schema.json",
    "artifact-manifest.schema.json",
    "compatibility-manifest.schema.json",
    "compensation-plan.schema.json",
    "config.schema.json",
    "fenced-lease.schema.json",
    "freshness-release.schema.json",
    "generation-coordination-lock.schema.json",
    "generation-receipt.schema.json",
    "installer-transaction.schema.json",
    "journal-event.schema.json",
    "offline-rollback.schema.json",
    "pointer-set.schema.json",
    "prior-pointer.schema.json",
    "registry.schema.json",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$")
_LOCK_RE = re.compile(r"^generation:[a-z0-9][a-z0-9._-]{0,62}$")
_RFC3339_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])-(?P<day>\d{2})"
    r"[Tt](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$",
    re.ASCII,
)
_REMOTE_HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
    re.ASCII,
)
_REMOTE_USERNAME_RE = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=-]+", re.ASCII)
_REMOTE_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@-]+", re.ASCII)
_JOURNAL_MAGIC = b"GWF1"
_JOURNAL_FRAME_VERSION = 1
_JOURNAL_HEADER = struct.Struct(">4sBQ32s")


class ContractError(ValueError):
    """A document violates the frozen P1 contract."""


class UnsupportedContractVersion(ContractError):
    """A known contract uses a schema version this build cannot interpret."""


JsonValue = None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]


def _normalise_string(value: str, path: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContractError(f"{path}: Unicode surrogate code points are forbidden")
    return unicodedata.normalize("NFC", value)


def _normalise_json(value: object, path: str = "$") -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _normalise_string(value, path)
    if isinstance(value, float):
        raise ContractError(f"{path}: floating-point values are forbidden in hashed contracts")
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise ContractError(f"{path}: object keys must be strings")
            key = _normalise_string(raw_key, path)
            if key in result:
                raise ContractError(f"{path}: duplicate key after Unicode normalization: {key!r}")
            result[key] = _normalise_json(raw_value, f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [_normalise_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ContractError(f"{path}: unsupported canonical JSON type {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Return the frozen UTF-8 canonical JSON encoding used for SHA-256 inputs."""
    normalised = _normalise_json(value)
    text = json.dumps(
        normalised,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_duplicate_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _parse_json(value: str | bytes) -> object:
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_json_pairs)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path}: expected object")
    return {str(key): val for key, val in value.items()}


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{path}: expected array")
    return value


def _string(value: object, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ContractError(f"{path}: expected {'non-empty ' if nonempty else ''}string")
    return value


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{path}: expected integer >= {minimum}")
    return value


def _exact_version(value: object, path: str, expected: int = 1) -> int:
    actual = _integer(value, path, minimum=1)
    if actual != expected:
        raise UnsupportedContractVersion(f"{path}: expected {expected}, got {actual}")
    return actual


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{path}: expected boolean")
    return value


def _exact_keys(
    value: Mapping[str, object],
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise ContractError(f"{path}: missing required field(s): {', '.join(sorted(missing))}")
    if unexpected:
        raise ContractError(f"{path}: unexpected field(s): {', '.join(sorted(unexpected))}")


def _digest(value: object, path: str) -> str:
    text = _string(value, path)
    if not _SHA256_RE.fullmatch(text):
        raise ContractError(f"{path}: expected lowercase SHA-256 hex")
    return text


def _commit(value: object, path: str) -> str:
    text = _string(value, path)
    if not _COMMIT_RE.fullmatch(text):
        raise ContractError(f"{path}: expected lowercase 40-character Git commit")
    return text


def _uuid(value: object, path: str) -> str:
    text = _string(value, path)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ContractError(f"{path}: expected UUID") from exc
    if str(parsed) != text:
        raise ContractError(f"{path}: UUID must use canonical lowercase hyphenated form")
    if parsed.version not in range(1, 9) or parsed.variant != uuid.RFC_4122:
        raise ContractError(f"{path}: UUID must use an RFC variant and version 1 through 8")
    return text


def _date_time(value: object, path: str) -> str:
    text = _string(value, path)
    match = _RFC3339_RE.fullmatch(text)
    if match is None:
        raise ContractError(f"{path}: expected RFC 3339 date-time")
    year = int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day"))
    if year == 0 or not 1 <= day <= calendar.monthrange(year, month)[1]:
        raise ContractError(f"{path}: expected RFC 3339 date-time")
    return text


def _absolute_path(value: object, path: str) -> str:
    text = _string(value, path)
    if "\x00" in text or "\r" in text or "\n" in text or text.startswith("//"):
        raise ContractError(f"{path}: expected canonical absolute POSIX path")
    pure = PurePosixPath(text)
    if (
        not pure.is_absolute()
        or ".." in pure.parts
        or str(pure) != text
        or "\\" in text
    ):
        raise ContractError(f"{path}: expected canonical absolute POSIX path")
    return text


def _relative_path(value: object, path: str) -> str:
    text = _string(value, path)
    if text == "." or "\x00" in text or "\r" in text or "\n" in text:
        raise ContractError(f"{path}: expected normalized non-escaping POSIX relative path")
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or text.startswith("./") or "\\" in text:
        raise ContractError(f"{path}: expected normalized non-escaping POSIX relative path")
    if str(pure) != text:
        raise ContractError(f"{path}: relative path is not canonical")
    return text


def _normalized_remote_url(value: object, path: str) -> str:
    text = _string(value, path)
    if "\x00" in text or any(character.isspace() for character in text):
        raise ContractError(f"{path}: expected normalized https:// or ssh:// remote URL")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ContractError(f"{path}: invalid remote URL") from exc
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        raise ContractError(f"{path}: expected normalized https:// or ssh:// remote URL")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise ContractError(f"{path}: remote URL credentials, query, and fragment are forbidden")
    if parsed.username == "":
        raise ContractError(f"{path}: SSH remote URL username must not be empty")
    if parsed.username is not None and parsed.scheme != "ssh":
        raise ContractError(f"{path}: HTTPS remote URL must not contain userinfo")
    if parsed.username is not None and _REMOTE_USERNAME_RE.fullmatch(parsed.username) is None:
        raise ContractError(f"{path}: SSH remote URL username is not canonical")
    if port is not None:
        raise ContractError(f"{path}: remote URL must not contain a port")
    remote_path = parsed.path
    pure = PurePosixPath(remote_path)
    path_segments = remote_path.removeprefix("/").split("/")
    if (
        not remote_path.startswith("/")
        or remote_path == "/"
        or remote_path.endswith("/")
        or "//" in remote_path
        or "%" in remote_path
        or "\\" in remote_path
        or "." in pure.parts
        or ".." in pure.parts
        or str(pure) != remote_path
        or any(_REMOTE_PATH_SEGMENT_RE.fullmatch(segment) is None for segment in path_segments)
    ):
        raise ContractError(f"{path}: remote URL path is not canonical")
    if _REMOTE_HOST_RE.fullmatch(parsed.hostname) is None:
        raise ContractError(f"{path}: remote URL hostname is not canonical")
    hostname = parsed.hostname.lower()
    netloc = f"{parsed.username}@{hostname}" if parsed.username is not None else hostname
    normalized = urlunsplit((parsed.scheme.lower(), netloc, remote_path, "", ""))
    if text != normalized:
        raise ContractError(f"{path}: remote URL is not normalized; expected {normalized!r}")
    return text


def _enum(value: object, path: str, allowed: set[str]) -> str:
    text = _string(value, path)
    if text not in allowed:
        raise ContractError(f"{path}: expected one of {', '.join(sorted(allowed))}")
    return text


def _validate_workspace_config(data: Mapping[str, object]) -> None:
    _exact_keys(data, "$", {"contract", "schema_version", "repo_uuid", "policy"})
    _uuid(data["repo_uuid"], "$.repo_uuid")
    policy = _mapping(data["policy"], "$.policy")
    _exact_keys(
        policy,
        "$.policy",
        {"freshness", "semantic_mode", "network_egress", "headless_backends"},
    )
    _enum(policy["freshness"], "$.policy.freshness", {"current_only"})
    _enum(
        policy["semantic_mode"],
        "$.policy.semantic_mode",
        {"host_agent_only", "explicit_backend"},
    )
    _boolean(policy["network_egress"], "$.policy.network_egress")
    backends = _list(policy["headless_backends"], "$.policy.headless_backends")
    seen_backends: set[str] = set()
    for index, backend in enumerate(backends):
        name = _string(backend, f"$.policy.headless_backends[{index}]")
        if name in seen_backends:
            raise ContractError("$.policy.headless_backends: values must be unique")
        seen_backends.add(name)
    if policy["semantic_mode"] == "host_agent_only" and backends:
        raise ContractError("$.policy.headless_backends: host_agent_only requires an empty list")


def _validate_source(source: object, path: str) -> None:
    value = _mapping(source, path)
    _exact_keys(value, path, {"path", "git_common_dir", "worktree_id", "remote_aliases"})
    _absolute_path(value["path"], f"{path}.path")
    _absolute_path(value["git_common_dir"], f"{path}.git_common_dir")
    _string(value["worktree_id"], f"{path}.worktree_id")
    aliases = _list(value["remote_aliases"], f"{path}.remote_aliases")
    if not aliases:
        raise ContractError(f"{path}.remote_aliases: at least one remote alias is required")
    previous = ""
    seen: set[str] = set()
    for index, raw in enumerate(aliases):
        alias_path = f"{path}.remote_aliases[{index}]"
        alias = _mapping(raw, alias_path)
        _exact_keys(alias, alias_path, {"url", "evidence_sha256"})
        url = _normalized_remote_url(alias["url"], f"{alias_path}.url")
        if url in seen or (previous and url <= previous):
            raise ContractError(f"{path}.remote_aliases: URLs must be unique and sorted")
        _digest(alias["evidence_sha256"], f"{alias_path}.evidence_sha256")
        seen.add(url)
        previous = url


def _validate_registry(data: Mapping[str, object]) -> None:
    _exact_keys(data, "$", {"contract", "schema_version", "revision", "workspaces"})
    _integer(data["revision"], "$.revision", minimum=1)
    workspaces = _list(data["workspaces"], "$.workspaces")
    if not workspaces:
        raise ContractError("$.workspaces: at least one workspace is required")
    seen: set[str] = set()
    for index, raw in enumerate(workspaces):
        path = f"$.workspaces[{index}]"
        workspace = _mapping(raw, path)
        _exact_keys(
            workspace,
            path,
            {
                "repo_uuid",
                "uuid_enrollment",
                "active_source_revision",
                "active_source",
                "active_source_evidence",
                "aliases",
            },
        )
        repo_uuid = _uuid(workspace["repo_uuid"], f"{path}.repo_uuid")
        if repo_uuid in seen:
            raise ContractError(f"{path}.repo_uuid: duplicate workspace identity")
        seen.add(repo_uuid)
        enrollment = _mapping(workspace["uuid_enrollment"], f"{path}.uuid_enrollment")
        _exact_keys(
            enrollment,
            f"{path}.uuid_enrollment",
            {"repo_uuid", "immutable_evidence_sha256", "current_evidence_sha256"},
        )
        if _uuid(enrollment["repo_uuid"], f"{path}.uuid_enrollment.repo_uuid") != repo_uuid:
            raise ContractError(f"{path}.uuid_enrollment.repo_uuid: must match workspace repo_uuid")
        _digest(
            enrollment["immutable_evidence_sha256"],
            f"{path}.uuid_enrollment.immutable_evidence_sha256",
        )
        _digest(
            enrollment["current_evidence_sha256"],
            f"{path}.uuid_enrollment.current_evidence_sha256",
        )
        active_source_revision = _integer(
            workspace["active_source_revision"],
            f"{path}.active_source_revision",
            minimum=1,
        )
        active_source = _mapping(workspace["active_source"], f"{path}.active_source")
        _validate_source(active_source, f"{path}.active_source")
        evidence = _mapping(
            workspace["active_source_evidence"],
            f"{path}.active_source_evidence",
        )
        _exact_keys(
            evidence,
            f"{path}.active_source_evidence",
            {
                "active_source_revision",
                "source_sha256",
                "rebind_evidence_sha256",
                "operation_epoch",
                "fence_token",
            },
        )
        if _integer(
            evidence["active_source_revision"],
            f"{path}.active_source_evidence.active_source_revision",
            minimum=1,
        ) != active_source_revision:
            raise ContractError(
                f"{path}.active_source_evidence.active_source_revision: "
                "must match workspace active_source_revision"
            )
        if _digest(
            evidence["source_sha256"],
            f"{path}.active_source_evidence.source_sha256",
        ) != canonical_sha256(active_source):
            raise ContractError(
                f"{path}.active_source_evidence.source_sha256: must match active_source"
            )
        _digest(
            evidence["rebind_evidence_sha256"],
            f"{path}.active_source_evidence.rebind_evidence_sha256",
        )
        _integer(
            evidence["operation_epoch"],
            f"{path}.active_source_evidence.operation_epoch",
            minimum=1,
        )
        _integer(
            evidence["fence_token"],
            f"{path}.active_source_evidence.fence_token",
            minimum=1,
        )
        aliases = _list(workspace["aliases"], f"{path}.aliases")
        for alias_index, alias in enumerate(aliases):
            _validate_source(alias, f"{path}.aliases[{alias_index}]")


def _validate_payload_entries(
    entries: object,
    path: str,
    *,
    required_root: str | None = None,
) -> list[str]:
    values = _list(entries, path)
    seen: set[str] = set()
    names: list[str] = []
    previous = ""
    for index, raw in enumerate(values):
        item_path = f"{path}[{index}]"
        entry = _mapping(raw, item_path)
        _exact_keys(entry, item_path, {"path", "file_type", "size", "sha256", "mode"})
        item_name = _relative_path(entry["path"], f"{item_path}.path")
        if required_root is not None and PurePosixPath(required_root) not in PurePosixPath(
            item_name
        ).parents:
            raise ContractError(
                f"{item_path}.path: must be a strict descendant of {required_root!r}"
            )
        if item_name in seen or (previous and item_name <= previous):
            raise ContractError(f"{item_path}.path: entries must be unique and sorted")
        seen.add(item_name)
        names.append(item_name)
        previous = item_name
        _enum(entry["file_type"], f"{item_path}.file_type", {"regular_file"})
        _integer(entry["size"], f"{item_path}.size")
        _digest(entry["sha256"], f"{item_path}.sha256")
        _enum(entry["mode"], f"{item_path}.mode", {"0600", "0644", "0755"})
    return names


def _validate_generation_receipt(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "repo_uuid",
            "generation_id",
            "lifecycle_state",
            "source_commit",
            "source_epoch",
            "active_source_revision",
            "operation_epoch",
            "fence_token",
            "policy_sha256",
            "observation_manifest_sha256",
            "queue_watermark",
            "semantic_completeness",
            "compatibility_sha256",
            "coordination_lock_id",
            "sealed_query_payload",
            "validations",
        },
    )
    _uuid(data["repo_uuid"], "$.repo_uuid")
    generation_id = _string(data["generation_id"], "$.generation_id")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ContractError("$.generation_id: invalid generation identity")
    _enum(data["lifecycle_state"], "$.lifecycle_state", {"CERTIFIED"})
    _commit(data["source_commit"], "$.source_commit")
    for field in ("source_epoch", "active_source_revision", "operation_epoch", "fence_token"):
        _integer(data[field], f"$.{field}", minimum=1)
    for field in ("policy_sha256", "observation_manifest_sha256", "compatibility_sha256"):
        _digest(data[field], f"$.{field}")
    _integer(data["queue_watermark"], "$.queue_watermark")
    _enum(
        data["semantic_completeness"],
        "$.semantic_completeness",
        {"complete", "not_required", "pending_rejected"},
    )
    lock_id = _string(data["coordination_lock_id"], "$.coordination_lock_id")
    if lock_id != f"generation:{generation_id[4:]}":
        raise ContractError("$.coordination_lock_id: must bind the generation identity")
    payload = _mapping(data["sealed_query_payload"], "$.sealed_query_payload")
    _exact_keys(payload, "$.sealed_query_payload", {"root", "manifest_sha256", "entries"})
    root = _relative_path(payload["root"], "$.sealed_query_payload.root")
    if root != "graphify-out":
        raise ContractError("$.sealed_query_payload.root: v1 requires 'graphify-out'")
    _digest(payload["manifest_sha256"], "$.sealed_query_payload.manifest_sha256")
    _validate_payload_entries(
        payload["entries"],
        "$.sealed_query_payload.entries",
        required_root=root,
    )
    validations = _list(data["validations"], "$.validations")
    if not validations:
        raise ContractError("$.validations: at least one validation is required")
    seen_validations: set[str] = set()
    for index, validation in enumerate(validations):
        name = _string(validation, f"$.validations[{index}]")
        if name in seen_validations:
            raise ContractError("$.validations: values must be unique")
        seen_validations.add(name)


def _validate_journal_event(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "event_id",
            "sequence",
            "transition",
            "generation_id",
            "prior_event_sha256",
            "receipt_sha256",
            "pointer_revision",
            "operation_epoch",
            "fence_token",
            "occurred_at",
        },
    )
    _uuid(data["event_id"], "$.event_id")
    sequence = _integer(data["sequence"], "$.sequence", minimum=1)
    transition = _enum(
        data["transition"],
        "$.transition",
        {
            "ALLOCATED",
            "STAGING",
            "BUILT",
            "VALIDATING",
            "CERTIFIED",
            "PROMOTED",
            "FAILED",
            "SUPERSEDED",
            "REPAIRED",
            "ROLLED_BACK",
        },
    )
    generation_id = _string(data["generation_id"], "$.generation_id")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ContractError("$.generation_id: invalid generation identity")
    _integer(data["operation_epoch"], "$.operation_epoch", minimum=1)
    _integer(data["fence_token"], "$.fence_token", minimum=1)
    prior = data["prior_event_sha256"]
    if prior is not None:
        _digest(prior, "$.prior_event_sha256")
    if sequence == 1 and prior is not None:
        raise ContractError("$.prior_event_sha256: initial event must not name a prior event")
    if sequence > 1 and prior is None:
        raise ContractError("$.prior_event_sha256: noninitial event requires a prior event hash")
    precertification = {"ALLOCATED", "STAGING", "BUILT", "VALIDATING", "FAILED"}
    receipt = data["receipt_sha256"]
    pointer_revision = data["pointer_revision"]
    if transition in precertification:
        if receipt is not None or pointer_revision is not None:
            raise ContractError(
                "precertification journal event must not reference a sealed receipt or pointer"
            )
    else:
        if receipt is None or pointer_revision is None:
            raise ContractError("certified journal event requires receipt and pointer references")
        _digest(receipt, "$.receipt_sha256")
        minimum_pointer_revision = 0 if transition == "CERTIFIED" else 1
        _integer(
            pointer_revision,
            "$.pointer_revision",
            minimum=minimum_pointer_revision,
        )
    _date_time(data["occurred_at"], "$.occurred_at")


def _validate_fenced_lease(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "repo_uuid",
            "operation",
            "fence_token",
            "owner",
            "acquired_at",
            "heartbeat_at",
            "liveness_deadline_monotonic_ns",
        },
    )
    _uuid(data["repo_uuid"], "$.repo_uuid")
    _enum(
        data["operation"],
        "$.operation",
        {
            "ACTIVATE",
            "BUILD",
            "GC",
            "MIGRATE",
            "POINTER_RECOVERY",
            "PROMOTE",
            "REPAIR",
            "ROLLBACK",
            "SEMANTIC_CLAIM",
        },
    )
    _integer(data["fence_token"], "$.fence_token", minimum=1)
    owner = _mapping(data["owner"], "$.owner")
    _exact_keys(owner, "$.owner", {"boot_id", "pid", "process_start_id"})
    _string(owner["boot_id"], "$.owner.boot_id")
    _integer(owner["pid"], "$.owner.pid", minimum=1)
    _string(owner["process_start_id"], "$.owner.process_start_id")
    _date_time(data["acquired_at"], "$.acquired_at")
    _date_time(data["heartbeat_at"], "$.heartbeat_at")
    _integer(data["liveness_deadline_monotonic_ns"], "$.liveness_deadline_monotonic_ns", minimum=1)


def _validate_pointer_ref(value: object, path: str) -> None:
    ref = _mapping(value, path)
    _exact_keys(ref, path, {"generation_id", "receipt_sha256"})
    generation_id = _string(ref["generation_id"], f"{path}.generation_id")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ContractError(f"{path}.generation_id: invalid generation identity")
    _digest(ref["receipt_sha256"], f"{path}.receipt_sha256")


def _validate_pointer_set(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "repo_uuid",
            "pointer_revision",
            "active_source_revision",
            "source_epoch",
            "operation_epoch",
            "fence_token",
            "state_schema_version",
            "current",
            "last_good",
        },
    )
    _uuid(data["repo_uuid"], "$.repo_uuid")
    for field in (
        "pointer_revision",
        "active_source_revision",
        "source_epoch",
        "operation_epoch",
        "fence_token",
    ):
        _integer(data[field], f"$.{field}", minimum=1)
    _exact_version(data["state_schema_version"], "$.state_schema_version", STATE_SCHEMA_VERSION)
    _validate_pointer_ref(data["current"], "$.current")
    if data["last_good"] is not None:
        _validate_pointer_ref(data["last_good"], "$.last_good")


def _validate_prior_pointer(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {"contract", "schema_version", "retained_at", "replaced_by_revision", "pointer_set"},
    )
    _date_time(data["retained_at"], "$.retained_at")
    replaced = _integer(data["replaced_by_revision"], "$.replaced_by_revision", minimum=2)
    pointer = _mapping(data["pointer_set"], "$.pointer_set")
    _validate_pointer_set(pointer)
    pointer_revision = _integer(pointer["pointer_revision"], "$.pointer_set.pointer_revision", minimum=1)
    if replaced <= pointer_revision:
        raise ContractError("$.replaced_by_revision: must exceed retained pointer revision")


def _validate_generation_lock(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "lock_id",
            "generation_id",
            "relative_path",
            "installed_before_state",
            "query_lock",
            "gc_lock",
            "retention",
        },
    )
    generation_id = _string(data["generation_id"], "$.generation_id")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ContractError("$.generation_id: invalid generation identity")
    lock_id = _string(data["lock_id"], "$.lock_id")
    if not _LOCK_RE.fullmatch(lock_id) or lock_id != f"generation:{generation_id[4:]}":
        raise ContractError("$.lock_id: must be the generation coordination identity")
    expected_path = f"locks/generations/{generation_id}.lock"
    if _relative_path(data["relative_path"], "$.relative_path") != expected_path:
        raise ContractError("$.relative_path: must use the retained v1 generation-lock path")
    _enum(data["installed_before_state"], "$.installed_before_state", {"CERTIFIED"})
    _enum(data["query_lock"], "$.query_lock", {"read_only_shared_advisory"})
    _enum(data["gc_lock"], "$.gc_lock", {"exclusive_then_reachability_recheck"})
    _enum(data["retention"], "$.retention", {"retain_v1"})


def _validate_observation(value: object, path: str) -> None:
    observation = _mapping(value, path)
    _exact_keys(
        observation,
        path,
        {
            "pointer_revision",
            "active_source_revision",
            "operation_epoch",
            "fence_token",
            "state_schema_version",
            "source_commit",
            "inventory_sha256",
            "policy_sha256",
            "detector_id",
            "receipt_sha256",
            "payload_manifest_sha256",
            "stable_inventory_passes",
        },
    )
    for field in (
        "pointer_revision",
        "active_source_revision",
        "operation_epoch",
        "fence_token",
    ):
        _integer(observation[field], f"{path}.{field}", minimum=1)
    _exact_version(
        observation["state_schema_version"],
        f"{path}.state_schema_version",
        STATE_SCHEMA_VERSION,
    )
    _commit(observation["source_commit"], f"{path}.source_commit")
    for field in (
        "inventory_sha256",
        "policy_sha256",
        "receipt_sha256",
        "payload_manifest_sha256",
    ):
        _digest(observation[field], f"{path}.{field}")
    _string(observation["detector_id"], f"{path}.detector_id")
    passes = _integer(observation["stable_inventory_passes"], f"{path}.stable_inventory_passes")
    if passes != 2:
        raise ContractError(f"{path}.stable_inventory_passes: v1 requires exactly two")


def _validate_freshness(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "policy",
            "pre_observation",
            "post_observation",
            "release_decision",
            "reason",
            "limitations",
        },
    )
    _enum(data["policy"], "$.policy", {"current_only"})
    _validate_observation(data["pre_observation"], "$.pre_observation")
    _validate_observation(data["post_observation"], "$.post_observation")
    decision = _enum(data["release_decision"], "$.release_decision", {"release", "withhold"})
    reason = _enum(
        data["reason"],
        "$.reason",
        {"observed_current", "drift", "unstable", "unsupported", "timeout", "source_unavailable"},
    )
    if decision == "release" and reason != "observed_current":
        raise ContractError("$.reason: release requires observed_current")
    if decision == "release" and data["pre_observation"] != data["post_observation"]:
        raise ContractError("$.pre_observation and $.post_observation: release requires equality")
    limitations = _mapping(data["limitations"], "$.limitations")
    _exact_keys(
        limitations,
        "$.limitations",
        {"strict_source_linearizability", "inter_observation_aba_detection", "post_boundary_changes"},
    )
    if _boolean(limitations["strict_source_linearizability"], "$.limitations.strict_source_linearizability"):
        raise ContractError("$.limitations.strict_source_linearizability: v1 must be false")
    if _boolean(
        limitations["inter_observation_aba_detection"],
        "$.limitations.inter_observation_aba_detection",
    ):
        raise ContractError("$.limitations.inter_observation_aba_detection: v1 must be false")
    _enum(limitations["post_boundary_changes"], "$.limitations.post_boundary_changes", {"out_of_scope"})


def _validate_artifact_manifest(data: Mapping[str, object]) -> None:
    _exact_keys(data, "$", {"contract", "schema_version", "manifest_version", "artifacts"})
    _exact_version(data["manifest_version"], "$.manifest_version")
    artifacts = _validate_payload_entries(data["artifacts"], "$.artifacts")
    if not artifacts:
        raise ContractError("$.artifacts: manifest must cover at least one artifact")


def _validate_compatibility(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "distribution",
            "distribution_version",
            "distribution_build",
            "engine_baseline",
            "upstream_commit",
            "fork_commit",
            "extractor_cache_abi",
            "python",
            "platform",
            "state_schema_version",
            "adapter_contract_version",
            "cli_contract_version",
            "runtime_lock_sha256",
            "skill_bundle_sha256",
            "contract_bundle_sha256",
            "fixture_manifest_sha256",
            "provenance_sha256",
            "sbom_sha256",
            "artifacts",
        },
    )
    if data["distribution"] != "graphifyy":
        raise ContractError("$.distribution: P1 freezes the single graphifyy distribution")
    if data["distribution_version"] != CANDIDATE_DISTRIBUTION_VERSION:
        raise ContractError("$.distribution_version: unsupported workspace candidate")
    _string(data["distribution_build"], "$.distribution_build")
    if data["engine_baseline"] != ENGINE_BASELINE:
        raise ContractError("$.engine_baseline: P1 is pinned to Graphify 0.9.16")
    upstream_commit = _commit(data["upstream_commit"], "$.upstream_commit")
    if upstream_commit != UPSTREAM_BASELINE_COMMIT:
        raise ContractError("$.upstream_commit: expected exact upstream baseline")
    _commit(data["fork_commit"], "$.fork_commit")
    if data["extractor_cache_abi"] != EXTRACTOR_CACHE_ABI:
        raise ContractError("$.extractor_cache_abi: unsupported ABI")
    _string(data["python"], "$.python")
    _string(data["platform"], "$.platform")
    for field, expected in (
        ("state_schema_version", STATE_SCHEMA_VERSION),
        ("adapter_contract_version", ADAPTER_CONTRACT_VERSION),
        ("cli_contract_version", CLI_CONTRACT_VERSION),
    ):
        _exact_version(data[field], f"$.{field}", expected)
    for field in (
        "runtime_lock_sha256",
        "skill_bundle_sha256",
        "contract_bundle_sha256",
        "fixture_manifest_sha256",
        "provenance_sha256",
        "sbom_sha256",
    ):
        _digest(data[field], f"$.{field}")
    artifacts = _mapping(data["artifacts"], "$.artifacts")
    if not artifacts:
        raise ContractError("$.artifacts: at least one artifact digest is required")
    for name, digest in artifacts.items():
        _relative_path(name, f"$.artifacts.{name}")
        _digest(digest, f"$.artifacts.{name}")
    expected_artifacts = set(REQUIRED_COMPATIBILITY_ARTIFACTS)
    actual_artifacts = set(artifacts)
    if actual_artifacts != expected_artifacts:
        missing = sorted(expected_artifacts - actual_artifacts)
        extra = sorted(actual_artifacts - expected_artifacts)
        raise ContractError(f"$.artifacts: incomplete artifact tuple: missing={missing}, extra={extra}")
    for field, artifact_name in (
        ("skill_bundle_sha256", "skill-bundle.zip"),
        ("contract_bundle_sha256", "contract-bundle.zip"),
        ("fixture_manifest_sha256", "fixture-manifest.json"),
        ("provenance_sha256", "provenance.json"),
        ("sbom_sha256", "sbom.cdx.json"),
    ):
        if data[field] != artifacts[artifact_name]:
            raise ContractError(f"$.{field}: must match $.artifacts.{artifact_name}")


def _validate_install_item(value: object, path: str) -> str:
    item = _mapping(value, path)
    _exact_keys(item, path, {"path", "before_sha256", "after_sha256"})
    item_path = _absolute_path(item["path"], f"{path}.path")
    before = item["before_sha256"]
    if before is not None:
        _digest(before, f"{path}.before_sha256")
    _digest(item["after_sha256"], f"{path}.after_sha256")
    return item_path


def _validate_installer(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "transaction_id",
            "phase",
            "home",
            "codex_home",
            "candidate_manifest_sha256",
            "items",
            "compensation_plan_sha256",
            "generation_disposition",
        },
    )
    _uuid(data["transaction_id"], "$.transaction_id")
    _enum(
        data["phase"],
        "$.phase",
        {"PREPARED", "STAGED", "SWITCHED", "VERIFIED", "COMPENSATING", "ROLLED_BACK"},
    )
    home = PurePosixPath(_absolute_path(data["home"], "$.home"))
    codex_home = PurePosixPath(_absolute_path(data["codex_home"], "$.codex_home"))
    if home == PurePosixPath("/"):
        raise ContractError("$.home: installer root must not be the filesystem root")
    if codex_home == PurePosixPath("/"):
        raise ContractError("$.codex_home: installer root must not be the filesystem root")
    _digest(data["candidate_manifest_sha256"], "$.candidate_manifest_sha256")
    items = _list(data["items"], "$.items")
    if not items:
        raise ContractError("$.items: transaction must name at least one switched item")
    seen_paths: set[str] = set()
    for index, item in enumerate(items):
        raw_item_path = _validate_install_item(item, f"$.items[{index}]")
        if raw_item_path in seen_paths:
            raise ContractError(f"$.items[{index}].path: installer item paths must be unique")
        seen_paths.add(raw_item_path)
        item_path = PurePosixPath(raw_item_path)
        if home not in item_path.parents and codex_home not in item_path.parents:
            raise ContractError(f"$.items[{index}].path: outside declared HOME/CODEX_HOME")
    _digest(data["compensation_plan_sha256"], "$.compensation_plan_sha256")
    _enum(data["generation_disposition"], "$.generation_disposition", {"preserve_untouched"})


def _validate_compensation(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "transaction_id",
            "restore_order",
            "remove_if_created",
            "restore_artifacts",
            "required_offline_artifacts",
            "generation_disposition",
        },
    )
    _uuid(data["transaction_id"], "$.transaction_id")
    action_count = 0
    normalized_arrays: dict[str, list[str]] = {}
    for field in ("restore_order", "remove_if_created", "required_offline_artifacts"):
        values = _list(data[field], f"$.{field}")
        if field == "required_offline_artifacts" and not values:
            raise ContractError("$.required_offline_artifacts: at least one artifact is required")
        if field != "required_offline_artifacts":
            action_count += len(values)
        seen_values: set[str] = set()
        normalized_values: list[str] = []
        for index, value in enumerate(values):
            if field == "required_offline_artifacts":
                normalized = _relative_path(value, f"$.{field}[{index}]")
            else:
                normalized = _absolute_path(value, f"$.{field}[{index}]")
            if normalized in seen_values:
                raise ContractError(f"$.{field}: values must be unique")
            seen_values.add(normalized)
            normalized_values.append(normalized)
        normalized_arrays[field] = normalized_values
    if action_count == 0:
        raise ContractError("$.restore_order/$.remove_if_created: at least one action is required")
    restore_artifacts = _list(data["restore_artifacts"], "$.restore_artifacts")
    mapped_paths: list[str] = []
    seen_artifacts: set[str] = set()
    for index, raw in enumerate(restore_artifacts):
        path = f"$.restore_artifacts[{index}]"
        mapping = _mapping(raw, path)
        _exact_keys(mapping, path, {"path", "offline_artifact"})
        mapped_paths.append(_absolute_path(mapping["path"], f"{path}.path"))
        artifact = _relative_path(mapping["offline_artifact"], f"{path}.offline_artifact")
        if artifact in seen_artifacts:
            raise ContractError("$.restore_artifacts: offline artifacts must be unique")
        if artifact not in normalized_arrays["required_offline_artifacts"]:
            raise ContractError(
                f"{path}.offline_artifact: must be named by required_offline_artifacts"
            )
        seen_artifacts.add(artifact)
    if mapped_paths != normalized_arrays["restore_order"]:
        raise ContractError(
            "$.restore_artifacts: mapping paths must match restore_order exactly and in order"
        )
    _enum(data["generation_disposition"], "$.generation_disposition", {"preserve_untouched"})


def _validate_offline_rollback(data: Mapping[str, object]) -> None:
    _exact_keys(
        data,
        "$",
        {
            "contract",
            "schema_version",
            "bundle_version",
            "offline",
            "entries",
            "restore_order",
            "generation_disposition",
        },
    )
    _exact_version(data["bundle_version"], "$.bundle_version")
    if not _boolean(data["offline"], "$.offline"):
        raise ContractError("$.offline: rollback bundle must be self-contained")
    entry_paths = _validate_payload_entries(data["entries"], "$.entries")
    if not entry_paths:
        raise ContractError("$.entries: offline rollback requires at least one entry")
    order = _list(data["restore_order"], "$.restore_order")
    restore_order: list[str] = []
    for index, value in enumerate(order):
        restore_order.append(_relative_path(value, f"$.restore_order[{index}]"))
    if len(restore_order) != len(set(restore_order)) or set(restore_order) != set(entry_paths):
        raise ContractError("$.restore_order: must name every entry exactly once")
    _enum(data["generation_disposition"], "$.generation_disposition", {"preserve_untouched"})


_VALIDATORS = {
    "graphify.workspace.config": _validate_workspace_config,
    "graphify.workspace.registry": _validate_registry,
    "graphify.workspace.generation_receipt": _validate_generation_receipt,
    "graphify.workspace.journal_event": _validate_journal_event,
    "graphify.workspace.fenced_lease": _validate_fenced_lease,
    "graphify.workspace.pointer_set": _validate_pointer_set,
    "graphify.workspace.prior_pointer": _validate_prior_pointer,
    "graphify.workspace.generation_coordination_lock": _validate_generation_lock,
    "graphify.workspace.freshness_release": _validate_freshness,
    "graphify.workspace.artifact_manifest": _validate_artifact_manifest,
    "graphify.workspace.compatibility_manifest": _validate_compatibility,
    "graphify.workspace.installer_transaction": _validate_installer,
    "graphify.workspace.compensation_plan": _validate_compensation,
    "graphify.workspace.offline_rollback": _validate_offline_rollback,
}

_SCHEMA_FILES = {
    "graphify.workspace.artifact_manifest": "artifact-manifest.schema.json",
    "graphify.workspace.compatibility_manifest": "compatibility-manifest.schema.json",
    "graphify.workspace.compensation_plan": "compensation-plan.schema.json",
    "graphify.workspace.config": "config.schema.json",
    "graphify.workspace.fenced_lease": "fenced-lease.schema.json",
    "graphify.workspace.freshness_release": "freshness-release.schema.json",
    "graphify.workspace.generation_coordination_lock": "generation-coordination-lock.schema.json",
    "graphify.workspace.generation_receipt": "generation-receipt.schema.json",
    "graphify.workspace.installer_transaction": "installer-transaction.schema.json",
    "graphify.workspace.journal_event": "journal-event.schema.json",
    "graphify.workspace.offline_rollback": "offline-rollback.schema.json",
    "graphify.workspace.pointer_set": "pointer-set.schema.json",
    "graphify.workspace.prior_pointer": "prior-pointer.schema.json",
    "graphify.workspace.registry": "registry.schema.json",
}

if set(_SCHEMA_FILES) != set(_VALIDATORS) or set(_SCHEMA_FILES.values()) != set(
    WORKSPACE_SCHEMA_FILES[1:]
):  # pragma: no cover - import-time developer invariant
    raise RuntimeError("workspace schema catalog does not match the frozen v1 member set")


@dataclass(frozen=True)
class ContractDocument:
    """Immutable canonical representation of one known versioned contract."""

    contract: str
    schema_version: int
    canonical: bytes

    CONTRACT: ClassVar[str | None] = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ContractDocument":
        raw_contract = _string(value.get("contract"), "$.contract")
        if cls.CONTRACT is not None and raw_contract != cls.CONTRACT:
            raise ContractError(
                f"$.contract: expected {cls.CONTRACT!r}, got {raw_contract!r}"
            )
        if raw_contract not in _VALIDATORS:
            raise ContractError(f"$.contract: unknown contract {raw_contract!r}")
        raw_version = _integer(value.get("schema_version"), "$.schema_version", minimum=1)
        if raw_version != WORKSPACE_SCHEMA_VERSION:
            raise UnsupportedContractVersion(
                f"$.schema_version: expected {WORKSPACE_SCHEMA_VERSION}, got {raw_version}"
            )

        normalised = _normalise_json(value)
        if not isinstance(normalised, dict):
            raise ContractError("$: expected object")
        _VALIDATORS[raw_contract](value)

        contract = _string(normalised.get("contract"), "$.contract")
        if cls.CONTRACT is not None and contract != cls.CONTRACT:
            raise ContractError(f"$.contract: expected {cls.CONTRACT!r}, got {contract!r}")
        if contract not in _VALIDATORS:
            raise ContractError(f"$.contract: unknown contract {contract!r}")
        version = _integer(normalised.get("schema_version"), "$.schema_version", minimum=1)
        if version != WORKSPACE_SCHEMA_VERSION:
            raise UnsupportedContractVersion(
                f"$.schema_version: expected {WORKSPACE_SCHEMA_VERSION}, got {version}"
            )
        _VALIDATORS[contract](normalised)
        return cls(contract=contract, schema_version=version, canonical=canonical_json_bytes(normalised))

    @classmethod
    def from_json(cls, value: str | bytes) -> "ContractDocument":
        parsed = _parse_json(value)
        if not isinstance(parsed, Mapping):
            raise ContractError("$: expected object")
        return cls.from_mapping(parsed)

    def to_dict(self) -> dict[str, Any]:
        result = json.loads(self.canonical)
        return cast(dict[str, Any], result)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()


class WorkspaceConfig(ContractDocument):
    CONTRACT = "graphify.workspace.config"

    @classmethod
    def from_toml(cls, value: str | bytes) -> "WorkspaceConfig":
        try:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            parsed = tomllib.loads(text)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ContractError(f"invalid workspace TOML: {exc}") from exc
        return cast(WorkspaceConfig, cls.from_mapping(parsed))


class Registry(ContractDocument):
    CONTRACT = "graphify.workspace.registry"


class GenerationReceipt(ContractDocument):
    CONTRACT = "graphify.workspace.generation_receipt"


class JournalEvent(ContractDocument):
    CONTRACT = "graphify.workspace.journal_event"


class FencedLease(ContractDocument):
    CONTRACT = "graphify.workspace.fenced_lease"


class PointerSet(ContractDocument):
    CONTRACT = "graphify.workspace.pointer_set"


class PriorPointerRecord(ContractDocument):
    CONTRACT = "graphify.workspace.prior_pointer"


class GenerationCoordinationLock(ContractDocument):
    CONTRACT = "graphify.workspace.generation_coordination_lock"


class FreshnessRelease(ContractDocument):
    CONTRACT = "graphify.workspace.freshness_release"


class ArtifactManifest(ContractDocument):
    CONTRACT = "graphify.workspace.artifact_manifest"


class CompatibilityManifest(ContractDocument):
    CONTRACT = "graphify.workspace.compatibility_manifest"


class InstallerTransaction(ContractDocument):
    CONTRACT = "graphify.workspace.installer_transaction"


class CompensationPlan(ContractDocument):
    CONTRACT = "graphify.workspace.compensation_plan"


class OfflineRollback(ContractDocument):
    CONTRACT = "graphify.workspace.offline_rollback"


def validate_installer_compensation(
    transaction: InstallerTransaction | Mapping[str, object],
    plan: CompensationPlan | Mapping[str, object],
    rollback: OfflineRollback | Mapping[str, object],
) -> dict[str, JsonValue]:
    """Validate the relational installer, compensation, and rollback invariants."""
    transaction_document = (
        transaction
        if isinstance(transaction, InstallerTransaction)
        else cast(InstallerTransaction, InstallerTransaction.from_mapping(transaction))
    )
    plan_document = (
        plan
        if isinstance(plan, CompensationPlan)
        else cast(CompensationPlan, CompensationPlan.from_mapping(plan))
    )
    rollback_document = (
        rollback
        if isinstance(rollback, OfflineRollback)
        else cast(OfflineRollback, OfflineRollback.from_mapping(rollback))
    )
    transaction_data = transaction_document.to_dict()
    plan_data = plan_document.to_dict()
    rollback_data = rollback_document.to_dict()

    transaction_id = str(transaction_data["transaction_id"])
    if plan_data["transaction_id"] != transaction_id:
        raise ContractError("installer and compensation transaction IDs must match")
    if transaction_data["compensation_plan_sha256"] != plan_document.sha256:
        raise ContractError("installer compensation_plan_sha256 must match canonical plan bytes")

    home = PurePosixPath(str(transaction_data["home"]))
    codex_home = PurePosixPath(str(transaction_data["codex_home"]))
    expected_restore: set[str] = set()
    expected_remove: set[str] = set()
    item_paths: set[str] = set()
    for index, raw in enumerate(transaction_data["items"]):
        item = cast(dict[str, Any], raw)
        item_path = str(item["path"])
        if item_path in item_paths:
            raise ContractError(f"installer item path is duplicated: {item_path}")
        item_paths.add(item_path)
        pure = PurePosixPath(item_path)
        if home not in pure.parents and codex_home not in pure.parents:
            raise ContractError(f"installer item path is outside declared HOME/CODEX_HOME: {item_path}")
        if item["before_sha256"] is None:
            expected_remove.add(item_path)
        else:
            expected_restore.add(item_path)

    restore_order = [str(path) for path in plan_data["restore_order"]]
    remove_if_created = [str(path) for path in plan_data["remove_if_created"]]
    restore_artifacts = [cast(dict[str, Any], value) for value in plan_data["restore_artifacts"]]
    if len(restore_order) != len(set(restore_order)):
        raise ContractError("compensation restore_order must be unique")
    if len(remove_if_created) != len(set(remove_if_created)):
        raise ContractError("compensation remove_if_created must be unique")
    if set(restore_order) & set(remove_if_created):
        raise ContractError("compensation restore and remove actions must not overlap")
    if set(restore_order) != expected_restore:
        raise ContractError("compensation restore_order must cover every preexisting item exactly once")
    if set(remove_if_created) != expected_remove:
        raise ContractError(
            "compensation remove_if_created must cover every newly created item exactly once"
        )
    for action_path in (*restore_order, *remove_if_created):
        pure = PurePosixPath(action_path)
        if home not in pure.parents and codex_home not in pure.parents:
            raise ContractError(
                f"compensation action is outside declared HOME/CODEX_HOME: {action_path}"
            )

    required_artifacts = [str(path) for path in plan_data["required_offline_artifacts"]]
    rollback_entries = {
        str(entry["path"]): cast(dict[str, Any], entry) for entry in rollback_data["entries"]
    }
    missing_artifacts = sorted(set(required_artifacts) - set(rollback_entries))
    if missing_artifacts:
        raise ContractError(
            "compensation required_offline_artifacts are absent from OfflineRollback entries: "
            f"{missing_artifacts}"
        )
    mapping_by_path = {
        str(mapping["path"]): str(mapping["offline_artifact"]) for mapping in restore_artifacts
    }
    if [str(mapping["path"]) for mapping in restore_artifacts] != restore_order:
        raise ContractError("compensation restore mapping must follow restore_order")
    item_by_path = {
        str(cast(dict[str, Any], item)["path"]): cast(dict[str, Any], item)
        for item in transaction_data["items"]
    }
    for target_path in restore_order:
        artifact = mapping_by_path.get(target_path)
        if artifact is None:
            raise ContractError(f"compensation restore mapping is missing target: {target_path}")
        rollback_entry = rollback_entries.get(artifact)
        if rollback_entry is None:
            raise ContractError(f"compensation restore mapping names missing artifact: {artifact}")
        if rollback_entry["sha256"] != item_by_path[target_path]["before_sha256"]:
            raise ContractError(
                f"compensation restore mapping digest does not match installer preimage: {target_path}"
            )

    return {
        "compensation_plan_sha256": plan_document.sha256,
        "installer_item_count": len(item_paths),
        "offline_rollback_sha256": rollback_document.sha256,
        "remove_action_count": len(remove_if_created),
        "required_offline_artifact_count": len(required_artifacts),
        "restore_mapping_count": len(restore_artifacts),
        "restore_action_count": len(restore_order),
        "transaction_id": transaction_id,
        "validated": True,
    }


_MODEL_BY_CONTRACT: dict[str, type[ContractDocument]] = {
    model.CONTRACT: model
    for model in (
        WorkspaceConfig,
        Registry,
        GenerationReceipt,
        JournalEvent,
        FencedLease,
        PointerSet,
        PriorPointerRecord,
        GenerationCoordinationLock,
        FreshnessRelease,
        ArtifactManifest,
        CompatibilityManifest,
        InstallerTransaction,
        CompensationPlan,
        OfflineRollback,
    )
    if model.CONTRACT is not None
}

DocumentT = TypeVar("DocumentT", bound=ContractDocument)


@overload
def parse_contract(
    value: Mapping[str, object] | str | bytes,
    *,
    expected: type[DocumentT],
) -> DocumentT: ...


@overload
def parse_contract(
    value: Mapping[str, object] | str | bytes,
    *,
    expected: None = None,
) -> ContractDocument: ...


def parse_contract(
    value: Mapping[str, object] | str | bytes,
    *,
    expected: type[DocumentT] | None = None,
) -> ContractDocument | DocumentT:
    if isinstance(value, (str, bytes)):
        parsed = _parse_json(value)
    else:
        parsed = value
    if not isinstance(parsed, Mapping):
        raise ContractError("$: expected object")
    contract = _string(parsed.get("contract"), "$.contract")
    model = _MODEL_BY_CONTRACT.get(contract)
    if model is None:
        raise ContractError(f"$.contract: unknown contract {contract!r}")
    if expected is not None and model is not expected:
        raise ContractError(f"$.contract: expected {expected.CONTRACT!r}, got {contract!r}")
    document = model.from_mapping(parsed)
    return cast(ContractDocument | DocumentT, document)


def encode_journal_frame(event: JournalEvent | Mapping[str, object]) -> bytes:
    """Encode one corruption-evident journal frame without writing it anywhere."""
    document = event if isinstance(event, JournalEvent) else JournalEvent.from_mapping(event)
    payload = document.canonical
    checksum = hashlib.sha256(payload).digest()
    return _JOURNAL_HEADER.pack(
        _JOURNAL_MAGIC,
        _JOURNAL_FRAME_VERSION,
        len(payload),
        checksum,
    ) + payload


def decode_journal_frame(frame: bytes) -> JournalEvent:
    """Decode exactly one complete v1 frame and reject truncation or tampering."""
    if len(frame) < _JOURNAL_HEADER.size:
        raise ContractError("journal frame is truncated before its header")
    magic, version, length, checksum = _JOURNAL_HEADER.unpack(frame[: _JOURNAL_HEADER.size])
    if magic != _JOURNAL_MAGIC:
        raise ContractError("journal frame magic does not match GWF1")
    if version != _JOURNAL_FRAME_VERSION:
        raise UnsupportedContractVersion(
            f"journal frame version: expected {_JOURNAL_FRAME_VERSION}, got {version}"
        )
    payload = frame[_JOURNAL_HEADER.size :]
    if len(payload) != length:
        raise ContractError(f"journal frame length mismatch: expected {length}, got {len(payload)}")
    if hashlib.sha256(payload).digest() != checksum:
        raise ContractError("journal frame checksum mismatch")
    return cast(JournalEvent, JournalEvent.from_json(payload))


def schema_path(contract: str) -> Path:
    try:
        filename = _SCHEMA_FILES[contract]
    except KeyError as exc:
        raise ContractError(f"unknown contract {contract!r}") from exc
    return Path(__file__).with_name("schemas") / "v1" / filename


def load_schema(contract: str) -> dict[str, Any]:
    path = schema_path(contract)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load bundled schema {path.name}: {exc}") from exc
    if not isinstance(result, dict):
        raise ContractError(f"bundled schema {path.name} is not a JSON object")
    return cast(dict[str, Any], result)
