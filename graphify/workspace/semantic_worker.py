"""Executable P5B2 host-agent semantic-worker transport."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import errno
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import select
import stat
import sys
import time
import unicodedata
from typing import Any, BinaryIO, Callable, Mapping, TextIO, cast

from graphify import semantic_cleanup
from graphify.workspace.composition import WorkspaceRuntime
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    ContractError,
    WorkspaceConfig,
    WorkspaceLeaseState,
)
from graphify.workspace.identity import (
    IdentityError,
    SourceDiscoveryError,
    SourceDiscoveryTimeout,
    discover_source,
    read_workspace_config,
)
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseError,
    LeaseExpired,
    LeaseGrant,
    LeaseRecoveryRequired,
    StagedBuildLeaseRecoveryRequired,
    StaleLease,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    StateCorrupt,
    StatePathError,
    StateRecoveryRequired,
    require_before_deadline,
)
from graphify.workspace.registry import RevisionConflict
from graphify.workspace.semantic_queue import (
    SemanticCapabilityUnavailable,
    SemanticCheckpointCapacityUnavailable,
    SemanticClaim,
    SemanticDesiredWork,
    SemanticQueueCapacityExceeded,
    SemanticQueueConflict,
    SemanticQueueCorrupt,
    SemanticQueueSnapshot,
    StaleSemanticClaim,
)


REQUEST_CONTRACT = "graphify.workspace.semantic_worker_request"
RESULT_CONTRACT = "graphify.workspace.semantic_worker_result"
RESULT_BINDING_CONTRACT = "graphify.workspace.semantic_result_binding.internal"
SCHEMA_VERSION = 1
BEGIN_MAX_BYTES = 16 * 1024
SMALL_FRAME_MAX_BYTES = 4 * 1024
SEMANTIC_PAYLOAD_MAX_BYTES = 25 * 1024 * 1024
COMPLETE_MAX_BYTES = SEMANTIC_PAYLOAD_MAX_BYTES + 64 * 1024
RESULT_MAX_BYTES = 64 * 1024
SEMANTIC_TEXT_MAX_BYTES = 16 * 1024
SOURCE_LOCATION_MAX_BYTES = 32
MAX_SIGNED_COORDINATE = 9_223_372_036_854_775_807

_SCHEMA_ROOT = Path(__file__).parent / "schemas" / "cli" / "v1"
_REQUEST_SCHEMA_PATH = _SCHEMA_ROOT / "semantic-worker-request.schema.json"
_RESULT_SCHEMA_PATH = _SCHEMA_ROOT / "semantic-worker-result.schema.json"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PROGRESS_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$", re.ASCII)
_SOURCE_LOCATION_RE = re.compile(r"^L[1-9][0-9]*$", re.ASCII)

_NODE_FIELDS = frozenset(
    {
        "id",
        "label",
        "file_type",
        "source_file",
        "source_location",
        "source_url",
        "captured_at",
        "author",
        "contributor",
    }
)
_POST_NODE_FIELDS = _NODE_FIELDS | frozenset({"rationale"})
_EDGE_FIELDS = frozenset(
    {
        "source",
        "target",
        "relation",
        "confidence",
        "confidence_score",
        "source_file",
        "source_location",
        "weight",
    }
)
_HYPEREDGE_FIELDS = frozenset(
    {"id", "label", "nodes", "relation", "confidence", "confidence_score", "source_file"}
)
_PRE_FILE_TYPES = frozenset({"code", "document", "paper", "image", "rationale", "concept"})
_POST_FILE_TYPES = frozenset({"code", "document", "paper", "image"})
_EDGE_RELATIONS = frozenset(
    {
        "calls",
        "implements",
        "references",
        "cites",
        "conceptually_related_to",
        "shares_data_with",
        "semantically_similar_to",
        "rationale_for",
    }
)
_HYPEREDGE_RELATIONS = frozenset({"participate_in", "implement", "form"})
_EDGE_CONFIDENCE = frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"})
_HYPEREDGE_CONFIDENCE = frozenset({"EXTRACTED", "INFERRED"})
_CALLER_FAILURES = {
    "host_agent_transient": True,
    "semantic_policy_refused": False,
    "semantic_work_unsupported": False,
}
_TRANSPORT_FAILURES = {
    "host_agent_timeout": True,
    "host_agent_interrupted": True,
    "semantic_result_invalid": True,
    "source_unavailable": True,
    "source_content_changed": False,
    "semantic_result_binding_conflict": False,
}
_ALL_QUEUE_FAILURES = {**_CALLER_FAILURES, **_TRANSPORT_FAILURES}
_FAILURE_ACTIONS: dict[tuple[str, str], str] = {
    ("retry_scheduled", "host_agent_transient"): "drain_semantic_queue",
    ("retry_scheduled", "host_agent_timeout"): "drain_semantic_queue",
    ("retry_scheduled", "host_agent_interrupted"): "drain_semantic_queue",
    ("retry_scheduled", "semantic_result_invalid"): "drain_semantic_queue",
    ("retry_scheduled", "source_unavailable"): "restore_source",
}
_WITHHELD_ACTIONS = {
    "semantic_claim_contended": "retry_status",
    "semantic_authority_stale": "retry_status",
    "semantic_worker_preclaim_timeout": "retry_status",
    "semantic_worker_preclaim_interrupted": "retry_status",
    "semantic_checkpoint_capacity_unavailable": "inspect_semantic_queue",
    "workspace_config_unavailable": "retry_status",
    "semantic_capability_unavailable": "inspect_workspace_state",
    "staged_build_recovery_required": "resume_exact_workspace_sync",
}
_INVALID_ACTIONS = {
    "semantic_worker_request_invalid": "none",
    "runtime_authority_missing": "install_candidate_authority",
    "runtime_authority_invalid": "install_candidate_authority",
    "runtime_authority_unsupported": "install_supported_candidate",
    "unsafe_state_path": "configure_safe_state_root",
    "workspace_config_invalid": "inspect_workspace_state",
    "semantic_queue_invalid": "inspect_semantic_queue",
    "registry_invalid": "inspect_workspace_state",
    "workspace_state_invalid": "inspect_workspace_state",
}
_LEASE_TTL_NS = 30_000_000_000
_HEARTBEAT_INTERVAL_NS = 10_000_000_000
_OUTPUT_WINDOW_NS = 5_000_000_000
_SOURCE_CHUNK_BYTES = 1024 * 1024
_WORKSPACE_CONFIG_MAX_BYTES = 64 * 1024


class SemanticWorkerError(RuntimeError):
    """Base class for stable semantic-worker transport failures."""


class SemanticWorkerRequestInvalid(SemanticWorkerError):
    """A protocol frame cannot be accepted."""


class SemanticResultInvalid(SemanticWorkerError):
    """A complete payload or result binding is invalid."""


class SemanticResultBindingConflict(SemanticWorkerError):
    """A derived immutable staging path already contains different bytes."""


class SemanticOutputDeliveryError(SemanticWorkerError):
    """A complete result frame could not be written and flushed."""


class SemanticSourceUnavailable(SemanticWorkerError):
    """The source observation did not complete safely."""


class SemanticSourceChanged(SemanticWorkerError):
    """A completed source observation proved a content mismatch."""


def _caused_by_keyboard_interrupt(fault: BaseException) -> bool:
    current: BaseException | None = fault
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, KeyboardInterrupt):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class _TerminalRoute(Exception):
    outcome: str
    reason_code: str
    action_code: str
    exit_code: int


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _normalise_string(value: str, path: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{path}: Unicode surrogate code points are forbidden")
    return unicodedata.normalize("NFC", value)


def _fixed_point_token(value: Decimal) -> str:
    if not value.is_finite() or value < 0 or value > 1:
        raise ValueError("protocol decimal must be finite and between 0 and 1")
    normalized = Decimal(0) if value.is_zero() else value.normalize()
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -6:
        raise ValueError("protocol decimal has more than six fractional digits")
    token = format(normalized, "f")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token or "0"


def _report_progress(
    progress: Callable[[], object] | None,
    index: int = 0,
) -> None:
    if progress is not None and index % 256 == 0:
        progress()


def _encode_protocol_value(
    value: object,
    path: str,
    *,
    progress: Callable[[], object] | None,
) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return _fixed_point_token(value)
    if isinstance(value, float):
        raise ValueError(f"{path}: binary floating-point values are forbidden")
    if isinstance(value, str):
        normalized_string = _normalise_string(value, path)
        return json.dumps(normalized_string, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list) or isinstance(value, tuple):
        encoded_items: list[str] = []
        for index, item in enumerate(value):
            _report_progress(progress, index)
            encoded_items.append(
                _encode_protocol_value(
                    item,
                    f"{path}[{index}]",
                    progress=progress,
                )
            )
        return "[" + ",".join(encoded_items) + "]"
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            _report_progress(progress, index)
            if not isinstance(raw_key, str):
                raise ValueError(f"{path}: object keys must be strings")
            key = _normalise_string(raw_key, f"{path}.<key>")
            if key in normalized:
                raise ValueError(f"{path}: duplicate key after Unicode normalization")
            normalized[key] = item
        encoded_items = []
        for index, key in enumerate(sorted(normalized)):
            _report_progress(progress, index)
            encoded_items.append(
                json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                + ":"
                + _encode_protocol_value(
                    normalized[key],
                    f"{path}.{key}",
                    progress=progress,
                )
            )
        return "{" + ",".join(encoded_items) + "}"
    raise ValueError(f"{path}: unsupported protocol value {type(value).__name__}")


def canonical_protocol_bytes(
    value: object,
    *,
    progress: Callable[[], object] | None = None,
) -> bytes:
    """Encode canonical UTF-8 JSON with exact unquoted fixed-point decimals."""

    _report_progress(progress)
    encoded = _encode_protocol_value(value, "$", progress=progress)
    _report_progress(progress)
    return (encoded + "\n").encode("utf-8")


def _work_digest(
    work: SemanticDesiredWork,
    *,
    progress: Callable[[], object] | None = None,
) -> str:
    return sha256(canonical_protocol_bytes(work.to_dict(), progress=progress))


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SemanticWorkerRequestInvalid("request contains duplicate keys")
        value[key] = item
    return value


def _reject_constant(_value: str) -> object:
    raise SemanticWorkerRequestInvalid("non-finite JSON numbers are forbidden")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SemanticWorkerRequestInvalid(f"{label} must be an object")
    return cast(dict[str, object], value)


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise SemanticWorkerRequestInvalid(f"{label} field set is invalid")


def _integer(value: object, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise SemanticWorkerRequestInvalid(f"{label} is invalid")
    return cast(int, value)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SemanticWorkerRequestInvalid(f"{label} is invalid")
    return value


def _common_request(value: Mapping[str, object]) -> str:
    if value.get("contract") != REQUEST_CONTRACT:
        raise SemanticWorkerRequestInvalid("request contract is unsupported")
    for field in ("schema_version", "cli_contract_version"):
        if type(value.get(field)) is not int or value.get(field) != 1:
            raise SemanticWorkerRequestInvalid(f"request {field} is unsupported")
    action = value.get("action")
    if not isinstance(action, str) or action not in {"begin", "checkpoint", "complete", "fail"}:
        raise SemanticWorkerRequestInvalid("request action is unsupported")
    return action


def _validate_begin(value: Mapping[str, object]) -> None:
    _exact_fields(
        value,
        frozenset(
            {
                "contract",
                "schema_version",
                "cli_contract_version",
                "action",
                "repo_uuid",
                "expected_registry_revision",
                "expected_active_source_revision",
                "expected_operation_epoch",
                "expected_migration_epoch",
                "expected_queue_revision",
                "expected_desired_watermark",
                "executor",
                "host_agent_active",
                "timeout_ms",
            }
        ),
        "begin request",
    )
    try:
        WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
    except ContractError as exc:
        raise SemanticWorkerRequestInvalid("begin repo_uuid is invalid") from exc
    for field in (
        "expected_registry_revision",
        "expected_active_source_revision",
        "expected_operation_epoch",
    ):
        _integer(value.get(field), field, minimum=1, maximum=MAX_SIGNED_COORDINATE)
    for field in (
        "expected_migration_epoch",
        "expected_queue_revision",
        "expected_desired_watermark",
    ):
        _integer(value.get(field), field, minimum=0, maximum=MAX_SIGNED_COORDINATE)
    _integer(value.get("timeout_ms"), "timeout_ms", minimum=1, maximum=600_000)
    if value.get("executor") != "host_agent" or value.get("host_agent_active") is not True:
        raise SemanticWorkerRequestInvalid("begin requires an explicitly active host agent")


def _validate_post_claim_common(value: Mapping[str, object]) -> None:
    _digest(value.get("begin_request_sha256"), "begin_request_sha256")
    _digest(value.get("claim_id"), "claim_id")


def _validate_checkpoint(value: Mapping[str, object]) -> None:
    _exact_fields(
        value,
        frozenset(
            {
                "contract",
                "schema_version",
                "cli_contract_version",
                "action",
                "begin_request_sha256",
                "claim_id",
                "progress_code",
            }
        ),
        "checkpoint request",
    )
    _validate_post_claim_common(value)
    code = value.get("progress_code")
    if (
        not isinstance(code, str)
        or _PROGRESS_RE.fullmatch(code) is None
        or code.startswith("result:")
    ):
        raise SemanticWorkerRequestInvalid("checkpoint progress_code is invalid")


def _validate_fail(value: Mapping[str, object]) -> None:
    _exact_fields(
        value,
        frozenset(
            {
                "contract",
                "schema_version",
                "cli_contract_version",
                "action",
                "begin_request_sha256",
                "claim_id",
                "error_code",
                "retryable",
            }
        ),
        "fail request",
    )
    _validate_post_claim_common(value)
    error_code = value.get("error_code")
    if not isinstance(error_code, str) or error_code not in _CALLER_FAILURES:
        raise SemanticWorkerRequestInvalid("fail error_code is invalid")
    if (
        type(value.get("retryable")) is not bool
        or value.get("retryable") is not _CALLER_FAILURES[error_code]
    ):
        raise SemanticWorkerRequestInvalid("fail retryability is invalid")


def _semantic_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SemanticResultInvalid(f"{label} must be nonempty and trimmed")
    if len(value.encode("utf-8")) > SEMANTIC_TEXT_MAX_BYTES:
        raise SemanticResultInvalid(f"{label} exceeds the UTF-8 byte limit")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise SemanticResultInvalid(f"{label} contains a control character")
    return value


def _semantic_rationale(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SemanticResultInvalid(f"{label} must be nonempty and trimmed")
    if len(value.encode("utf-8")) > SEMANTIC_TEXT_MAX_BYTES:
        raise SemanticResultInvalid(f"{label} exceeds the UTF-8 byte limit")
    segments = value.split("\n\n")
    for index, segment in enumerate(segments):
        _semantic_text(segment, f"{label}[{index}]")
    return value


def _source_location(value: object, label: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or _SOURCE_LOCATION_RE.fullmatch(value) is None
        or len(value.encode("utf-8")) > SOURCE_LOCATION_MAX_BYTES
    ):
        raise SemanticResultInvalid(f"{label} is invalid")


def _fixed_point(value: object, label: str) -> Decimal:
    if type(value) is int:
        if value not in {0, 1}:
            raise SemanticResultInvalid(f"{label} is outside the fixed-point range")
        return Decimal(cast(int, value))
    if not isinstance(value, Decimal):
        raise SemanticResultInvalid(f"{label} must be an exact decimal, not a binary float")
    try:
        token = _fixed_point_token(value)
        canonical = Decimal(token)
    except (InvalidOperation, ValueError) as exc:
        raise SemanticResultInvalid(f"{label} is not a canonical fixed-point value") from exc
    if canonical != value:
        raise SemanticResultInvalid(f"{label} is not a canonical fixed-point value")
    return value


def _semantic_id(value: object, label: str) -> str:
    errors: list[str] = []
    semantic_cleanup.validate_semantic_id(errors, label, value)
    if errors:
        raise SemanticResultInvalid(errors[0])
    return cast(str, value)


def _normalize_fragment_numbers(
    fragment: dict[str, object],
    *,
    progress: Callable[[], object] | None = None,
) -> None:
    edges = cast(list[dict[str, object]], fragment["edges"])
    for index, edge in enumerate(edges):
        _report_progress(progress, index)
        edge["confidence_score"] = _fixed_point(
            edge.get("confidence_score"), f"edges[{index}].confidence_score"
        )
        edge["weight"] = _fixed_point(edge.get("weight"), f"edges[{index}].weight")
    hyperedges = cast(list[dict[str, object]], fragment["hyperedges"])
    for index, hyperedge in enumerate(hyperedges):
        _report_progress(progress, index)
        hyperedge["confidence_score"] = _fixed_point(
            hyperedge.get("confidence_score"), f"hyperedges[{index}].confidence_score"
        )


def _validate_fragment(
    value: object,
    *,
    source_file: str | None,
    post_sanitize: bool,
    progress: Callable[[], object] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"nodes", "edges", "hyperedges"}:
        raise SemanticResultInvalid("semantic fragment field set is invalid")
    fragment = cast(dict[str, object], value)
    nodes = fragment.get("nodes")
    edges = fragment.get("edges")
    hyperedges = fragment.get("hyperedges")
    if not isinstance(nodes, list) or len(nodes) > 10_000:
        raise SemanticResultInvalid("semantic fragment nodes are invalid")
    if not isinstance(edges, list) or len(edges) > 100_000:
        raise SemanticResultInvalid("semantic fragment edges are invalid")
    if not isinstance(hyperedges, list) or len(hyperedges) > 10_000:
        raise SemanticResultInvalid("semantic fragment hyperedges are invalid")

    node_ids: set[str] = set()
    for index, raw_node in enumerate(nodes):
        _report_progress(progress, index)
        if not isinstance(raw_node, dict):
            raise SemanticResultInvalid(f"nodes[{index}] must be an object")
        fields = set(raw_node)
        if fields not in ({_NODE_FIELDS, _POST_NODE_FIELDS} if post_sanitize else {_NODE_FIELDS}):
            raise SemanticResultInvalid(f"nodes[{index}] field set is invalid")
        node_id = _semantic_id(raw_node.get("id"), f"nodes[{index}].id")
        if node_id in node_ids:
            raise SemanticResultInvalid("node IDs must be unique")
        node_ids.add(node_id)
        _semantic_text(raw_node.get("label"), f"nodes[{index}].label")
        file_type = raw_node.get("file_type")
        valid_types = _POST_FILE_TYPES if post_sanitize else _PRE_FILE_TYPES
        if file_type not in valid_types:
            raise SemanticResultInvalid(f"nodes[{index}].file_type is invalid")
        if source_file is not None and raw_node.get("source_file") != source_file:
            raise SemanticResultInvalid(f"nodes[{index}].source_file differs from work.path")
        if not isinstance(raw_node.get("source_file"), str):
            raise SemanticResultInvalid(f"nodes[{index}].source_file is invalid")
        _source_location(raw_node.get("source_location"), f"nodes[{index}].source_location")
        for field in ("source_url", "captured_at", "author", "contributor"):
            if raw_node.get(field) is not None:
                raise SemanticResultInvalid(f"nodes[{index}].{field} must be null")
        if "rationale" in raw_node:
            if not post_sanitize:
                raise SemanticResultInvalid(f"nodes[{index}] field set is invalid")
            _semantic_rationale(raw_node["rationale"], f"nodes[{index}].rationale")

    for index, raw_edge in enumerate(edges):
        _report_progress(progress, index)
        if not isinstance(raw_edge, dict) or set(raw_edge) != _EDGE_FIELDS:
            raise SemanticResultInvalid(f"edges[{index}] field set is invalid")
        source = _semantic_id(raw_edge.get("source"), f"edges[{index}].source")
        target = _semantic_id(raw_edge.get("target"), f"edges[{index}].target")
        if source not in node_ids or target not in node_ids:
            raise SemanticResultInvalid(f"edges[{index}] endpoint is not an input node")
        if raw_edge.get("relation") not in _EDGE_RELATIONS:
            raise SemanticResultInvalid(f"edges[{index}].relation is invalid")
        if raw_edge.get("confidence") not in _EDGE_CONFIDENCE:
            raise SemanticResultInvalid(f"edges[{index}].confidence is invalid")
        raw_edge["confidence_score"] = _fixed_point(
            raw_edge.get("confidence_score"), f"edges[{index}].confidence_score"
        )
        raw_edge["weight"] = _fixed_point(raw_edge.get("weight"), f"edges[{index}].weight")
        if source_file is not None and raw_edge.get("source_file") != source_file:
            raise SemanticResultInvalid(f"edges[{index}].source_file differs from work.path")
        if not isinstance(raw_edge.get("source_file"), str):
            raise SemanticResultInvalid(f"edges[{index}].source_file is invalid")
        _source_location(raw_edge.get("source_location"), f"edges[{index}].source_location")

    hyperedge_ids: set[str] = set()
    for index, raw_hyperedge in enumerate(hyperedges):
        _report_progress(progress, index)
        if not isinstance(raw_hyperedge, dict) or set(raw_hyperedge) != _HYPEREDGE_FIELDS:
            raise SemanticResultInvalid(f"hyperedges[{index}] field set is invalid")
        hyperedge_id = _semantic_id(raw_hyperedge.get("id"), f"hyperedges[{index}].id")
        if hyperedge_id in hyperedge_ids:
            raise SemanticResultInvalid("hyperedge IDs must be unique")
        hyperedge_ids.add(hyperedge_id)
        _semantic_text(raw_hyperedge.get("label"), f"hyperedges[{index}].label")
        members = raw_hyperedge.get("nodes")
        if not isinstance(members, list) or not 2 <= len(members) <= 256:
            raise SemanticResultInvalid(f"hyperedges[{index}].nodes is invalid")
        normalized_members = []
        for member_index, member in enumerate(members):
            _report_progress(progress, member_index)
            normalized_members.append(
                _semantic_id(
                    member,
                    f"hyperedges[{index}].nodes[{member_index}]",
                )
            )
        if len(set(normalized_members)) != len(normalized_members):
            raise SemanticResultInvalid("hyperedge members must be pairwise unique")
        if not set(normalized_members).issubset(node_ids):
            raise SemanticResultInvalid(f"hyperedges[{index}] member is not an input node")
        if raw_hyperedge.get("relation") not in _HYPEREDGE_RELATIONS:
            raise SemanticResultInvalid(f"hyperedges[{index}].relation is invalid")
        if raw_hyperedge.get("confidence") not in _HYPEREDGE_CONFIDENCE:
            raise SemanticResultInvalid(f"hyperedges[{index}].confidence is invalid")
        raw_hyperedge["confidence_score"] = _fixed_point(
            raw_hyperedge.get("confidence_score"), f"hyperedges[{index}].confidence_score"
        )
        if source_file is not None and raw_hyperedge.get("source_file") != source_file:
            raise SemanticResultInvalid(f"hyperedges[{index}].source_file differs from work.path")
        if not isinstance(raw_hyperedge.get("source_file"), str):
            raise SemanticResultInvalid(f"hyperedges[{index}].source_file is invalid")
    return fragment


def _bounded_rationale_text(
    labels: list[str],
    *,
    progress: Callable[[], object] | None = None,
) -> str:
    projected_bytes = 0
    for index, label in enumerate(labels):
        _report_progress(progress, index)
        projected_bytes += len(label.encode("utf-8"))
        if index:
            projected_bytes += 2
        if projected_bytes > SEMANTIC_TEXT_MAX_BYTES:
            raise SemanticResultInvalid("projected rationale exceeds the UTF-8 byte limit")
    return "\n\n".join(labels)


def _project_sanitized_fragment(
    fragment: dict[str, object],
    *,
    progress: Callable[[], object] | None = None,
) -> dict[str, object]:
    _report_progress(progress)
    projected = deepcopy(fragment)
    _report_progress(progress)
    nodes = cast(list[dict[str, object]], projected["nodes"])
    edges = cast(list[dict[str, object]], projected["edges"])
    hyperedges = cast(list[dict[str, object]], projected["hyperedges"])
    node_by_id: dict[str, dict[str, object]] = {}
    for index, node in enumerate(nodes):
        _report_progress(progress, index)
        node_by_id[cast(str, node["id"])] = node
    rationale_targets: dict[str, list[str]] = {}
    for index, edge in enumerate(edges):
        _report_progress(progress, index)
        if edge.get("relation") == "rationale_for":
            rationale_targets.setdefault(cast(str, edge["source"]), []).append(
                cast(str, edge["target"])
            )

    remove_ids: set[str] = set()
    rationale_candidates: list[dict[str, object]] = []
    keep_nodes: list[dict[str, object]] = []
    for index, node in enumerate(nodes):
        _report_progress(progress, index)
        node_id = cast(str, node["id"])
        label = cast(str, node["label"])
        if node.get("file_type") in {"rationale", "concept"}:
            if semantic_cleanup.is_sentence_like_rationale_label(label):
                rationale_candidates.append(node)
            remove_ids.add(node_id)
        elif node_id in rationale_targets and semantic_cleanup.is_sentence_like_rationale_label(
            label
        ):
            rationale_candidates.append(node)
            remove_ids.add(node_id)
        else:
            keep_nodes.append(node)

    rationale_by_target: dict[str, list[str]] = {}
    for index, candidate in enumerate(rationale_candidates):
        _report_progress(progress, index)
        candidate_id = cast(str, candidate["id"])
        label = cast(str, candidate["label"]).strip()
        for target in rationale_targets.get(candidate_id, ()):
            if target in node_by_id and target not in remove_ids:
                rationale_by_target.setdefault(target, []).append(label)
    for index, (target, labels) in enumerate(rationale_by_target.items()):
        _report_progress(progress, index)
        node_by_id[target]["rationale"] = _bounded_rationale_text(
            labels,
            progress=progress,
        )

    keep_edges = []
    for index, edge in enumerate(edges):
        _report_progress(progress, index)
        if edge.get("source") not in remove_ids and edge.get("target") not in remove_ids:
            keep_edges.append(edge)
    surviving_ids: set[str] = set()
    for index, node in enumerate(keep_nodes):
        _report_progress(progress, index)
        surviving_ids.add(cast(str, node["id"]))
    keep_hyperedges: list[dict[str, object]] = []
    for index, hyperedge in enumerate(hyperedges):
        _report_progress(progress, index)
        members = [
            member for member in cast(list[str], hyperedge["nodes"]) if member in surviving_ids
        ]
        if len(members) < 2:
            continue
        copied = dict(hyperedge)
        copied["nodes"] = members
        keep_hyperedges.append(copied)
    return {"nodes": keep_nodes, "edges": keep_edges, "hyperedges": keep_hyperedges}


def _numeric_multiset(
    fragment: Mapping[str, object],
    *,
    progress: Callable[[], object] | None = None,
) -> dict[tuple[object, ...], list[tuple[Decimal, ...]]]:
    values: dict[tuple[object, ...], list[tuple[Decimal, ...]]] = {}
    for index, edge in enumerate(cast(list[dict[str, object]], fragment["edges"])):
        _report_progress(progress, index)
        key = (
            "edge",
            edge["source"],
            edge["target"],
            edge["relation"],
            edge["source_file"],
            edge["source_location"],
        )
        values.setdefault(key, []).append(
            (cast(Decimal, edge["confidence_score"]), cast(Decimal, edge["weight"]))
        )
    for index, hyperedge in enumerate(cast(list[dict[str, object]], fragment["hyperedges"])):
        _report_progress(progress, index)
        key = (
            "hyperedge",
            hyperedge["id"],
            tuple(cast(list[str], hyperedge["nodes"])),
            hyperedge["relation"],
            hyperedge["source_file"],
        )
        values.setdefault(key, []).append((cast(Decimal, hyperedge["confidence_score"]),))
    return values


@dataclass(frozen=True)
class SemanticWorkerRequest:
    action: str
    value: Mapping[str, object]
    canonical: bytes

    def to_dict(self) -> dict[str, object]:
        return deepcopy(dict(self.value))


def _validate_complete(
    value: dict[str, object],
    *,
    progress: Callable[[], object] | None = None,
) -> None:
    _exact_fields(
        value,
        frozenset(
            {
                "contract",
                "schema_version",
                "cli_contract_version",
                "action",
                "begin_request_sha256",
                "claim_id",
                "payload",
            }
        ),
        "complete request",
    )
    _validate_post_claim_common(value)
    payload = _mapping(value.get("payload"), "complete payload")
    kind = payload.get("kind")
    if kind == "delete_tombstone":
        _exact_fields(payload, frozenset({"kind"}), "delete payload")
        return
    if kind != "semantic_fragment":
        raise SemanticWorkerRequestInvalid("complete payload kind is invalid")
    _exact_fields(payload, frozenset({"kind", "fragment"}), "semantic payload")
    try:
        fragment = _validate_fragment(
            payload.get("fragment"),
            source_file=None,
            post_sanitize=False,
            progress=progress,
        )
    except SemanticResultInvalid as exc:
        raise SemanticWorkerRequestInvalid(str(exc)) from exc
    _normalize_fragment_numbers(fragment, progress=progress)


def parse_request_frame(
    raw: bytes,
    *,
    progress: Callable[[], object] | None = None,
) -> SemanticWorkerRequest:
    """Parse one bounded canonical request frame without binary-float coercion."""

    if not isinstance(raw, bytes) or not raw or len(raw) > COMPLETE_MAX_BYTES:
        raise SemanticWorkerRequestInvalid("request frame exceeds its byte limit")
    _report_progress(progress)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except SemanticWorkerRequestInvalid:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise SemanticWorkerRequestInvalid("request frame is not valid UTF-8 JSON") from exc
    _report_progress(progress)
    if not isinstance(value, dict):
        raise SemanticWorkerRequestInvalid("request frame must be an object")
    request_value = cast(dict[str, object], value)
    action = _common_request(request_value)
    maximum = (
        BEGIN_MAX_BYTES
        if action == "begin"
        else (COMPLETE_MAX_BYTES if action == "complete" else SMALL_FRAME_MAX_BYTES)
    )
    if len(raw) > maximum:
        raise SemanticWorkerRequestInvalid("request frame exceeds its action byte limit")
    try:
        canonical = canonical_protocol_bytes(request_value, progress=progress)
    except (RecursionError, TypeError, ValueError) as exc:
        raise SemanticWorkerRequestInvalid("request frame is not canonically encodable") from exc
    if canonical != raw:
        raise SemanticWorkerRequestInvalid("request frame is not canonical")
    if action == "begin":
        _validate_begin(request_value)
    elif action == "checkpoint":
        _validate_checkpoint(request_value)
    elif action == "complete":
        _validate_complete(request_value, progress=progress)
    else:
        _validate_fail(request_value)
    return SemanticWorkerRequest(action=action, value=request_value, canonical=raw)


@dataclass(frozen=True)
class ValidatedPayload:
    value: Mapping[str, object]
    canonical: bytes

    @property
    def sha256(self) -> str:
        return sha256(self.canonical)

    @property
    def bytes_count(self) -> int:
        return len(self.canonical)

    @property
    def kind(self) -> str:
        return cast(str, self.value["kind"])


def _canonical_fragment_encoder(
    progress: Callable[[], object] | None,
) -> Callable[[object], bytes]:
    def encode(candidate: object) -> bytes:
        return canonical_protocol_bytes(candidate, progress=progress)

    return encode


def validate_completion_payload(
    payload: object,
    work: SemanticDesiredWork,
    *,
    progress: Callable[[], object] | None = None,
) -> ValidatedPayload:
    """Validate and sanitize one operation-matched complete payload."""

    try:
        validated_work = work.validated()
    except (ContractError, ValueError) as exc:
        raise SemanticResultInvalid("live desired work is invalid") from exc
    if not isinstance(payload, dict):
        raise SemanticResultInvalid("complete payload must be an object")
    _report_progress(progress)
    value = deepcopy(cast(dict[str, object], payload))
    _report_progress(progress)
    if validated_work.operation == "DELETE":
        if value != {"kind": "delete_tombstone"}:
            raise SemanticResultInvalid("DELETE requires the exact delete tombstone payload")
        canonical = canonical_protocol_bytes(value, progress=progress)
        return ValidatedPayload(value=value, canonical=canonical)
    if set(value) != {"kind", "fragment"} or value.get("kind") != "semantic_fragment":
        raise SemanticResultInvalid("UPSERT requires the exact semantic fragment payload")

    fragment = _validate_fragment(
        value.get("fragment"),
        source_file=validated_work.path,
        post_sanitize=False,
        progress=progress,
    )
    canonical_encoder = _canonical_fragment_encoder(progress)
    validation_errors = semantic_cleanup.validate_semantic_fragment(
        fragment,
        canonical_encoder=canonical_encoder,
        progress=progress,
    )
    if validation_errors:
        raise SemanticResultInvalid(validation_errors[0])
    raw_fragment_bytes = canonical_protocol_bytes(fragment, progress=progress)
    if len(raw_fragment_bytes) > SEMANTIC_PAYLOAD_MAX_BYTES:
        raise SemanticResultInvalid("raw semantic fragment exceeds the byte limit")

    projected = _project_sanitized_fragment(fragment, progress=progress)
    projected_bytes = canonical_protocol_bytes(projected, progress=progress)
    if len(projected_bytes) > SEMANTIC_PAYLOAD_MAX_BYTES:
        raise SemanticResultInvalid("projected sanitized fragment exceeds the byte limit")
    before_numbers = _numeric_multiset(fragment, progress=progress)
    _report_progress(progress)
    sanitized = semantic_cleanup.sanitize_semantic_fragment(
        deepcopy(fragment),
        progress=progress,
    )
    _report_progress(progress)
    if sanitized != projected:
        raise SemanticResultInvalid("semantic sanitizer differs from its bounded projection")
    post = _validate_fragment(
        sanitized,
        source_file=validated_work.path,
        post_sanitize=True,
        progress=progress,
    )
    validation_errors = semantic_cleanup.validate_semantic_fragment(
        post,
        canonical_encoder=canonical_encoder,
        progress=progress,
    )
    if validation_errors:
        raise SemanticResultInvalid(validation_errors[0])
    after_numbers = _numeric_multiset(post, progress=progress)
    for index, (key, numbers) in enumerate(after_numbers.items()):
        _report_progress(progress, index)
        available = list(before_numbers.get(key, ()))
        for numeric in numbers:
            if numeric not in available or any(not isinstance(item, Decimal) for item in numeric):
                raise SemanticResultInvalid("sanitizer changed a surviving exact numeric value")
            available.remove(numeric)

    canonical_fragment = canonical_protocol_bytes(post, progress=progress)
    if len(canonical_fragment) > SEMANTIC_PAYLOAD_MAX_BYTES:
        raise SemanticResultInvalid("sanitized semantic fragment exceeds the byte limit")
    wrapped: dict[str, object] = {"kind": "semantic_fragment", "fragment": post}
    canonical = canonical_protocol_bytes(wrapped, progress=progress)
    if len(canonical) > COMPLETE_MAX_BYTES:
        raise SemanticResultInvalid("canonical semantic payload exceeds the framing byte limit")
    return ValidatedPayload(value=wrapped, canonical=canonical)


def validate_bound_payload(
    payload: object,
    work: SemanticDesiredWork,
    *,
    progress: Callable[[], object] | None = None,
) -> ValidatedPayload:
    """Revalidate the already-sanitized payload stored in a result binding."""

    try:
        validated_work = work.validated()
    except (ContractError, ValueError) as exc:
        raise SemanticResultInvalid("bound desired work is invalid") from exc
    if not isinstance(payload, dict):
        raise SemanticResultInvalid("bound payload must be an object")
    _report_progress(progress)
    try:
        value = deepcopy(cast(dict[str, object], payload))
    except RecursionError as exc:
        raise SemanticResultInvalid("bound payload nesting is too deep") from exc
    _report_progress(progress)
    if validated_work.operation == "DELETE":
        if value != {"kind": "delete_tombstone"}:
            raise SemanticResultInvalid("bound DELETE payload is invalid")
        canonical = canonical_protocol_bytes(value, progress=progress)
        return ValidatedPayload(value=value, canonical=canonical)
    if set(value) != {"kind", "fragment"} or value.get("kind") != "semantic_fragment":
        raise SemanticResultInvalid("bound UPSERT payload is invalid")
    fragment = _validate_fragment(
        value.get("fragment"),
        source_file=validated_work.path,
        post_sanitize=True,
        progress=progress,
    )
    errors = semantic_cleanup.validate_semantic_fragment(
        fragment,
        canonical_encoder=_canonical_fragment_encoder(progress),
        progress=progress,
    )
    if errors:
        raise SemanticResultInvalid(errors[0])
    canonical_fragment = canonical_protocol_bytes(fragment, progress=progress)
    if len(canonical_fragment) > SEMANTIC_PAYLOAD_MAX_BYTES:
        raise SemanticResultInvalid("bound semantic fragment exceeds the byte limit")
    wrapped: dict[str, object] = {"kind": "semantic_fragment", "fragment": fragment}
    canonical = canonical_protocol_bytes(wrapped, progress=progress)
    if len(canonical) > COMPLETE_MAX_BYTES:
        raise SemanticResultInvalid("bound semantic payload exceeds the framing byte limit")
    return ValidatedPayload(value=wrapped, canonical=canonical)


@dataclass(frozen=True)
class ResultBinding:
    value: Mapping[str, object]
    canonical: bytes

    @property
    def sha256(self) -> str:
        return sha256(self.canonical)

    @property
    def bytes_count(self) -> int:
        return len(self.canonical)


def build_result_binding(
    *,
    begin_request_sha256: str,
    repo_uuid: str,
    claim: SemanticClaim,
    work_sha256: str,
    payload: ValidatedPayload,
    progress: Callable[[], object] | None = None,
) -> ResultBinding:
    try:
        canonical_repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(repo_uuid)
    except ContractError as exc:
        raise SemanticResultInvalid("result binding repo UUID is invalid") from exc
    for label, digest in (
        ("begin_request_sha256", begin_request_sha256),
        ("work_sha256", work_sha256),
    ):
        if _DIGEST_RE.fullmatch(digest) is None:
            raise SemanticResultInvalid(f"result binding {label} is invalid")
    value: dict[str, object] = {
        "active_source_revision": claim.active_source_revision,
        "attempt": claim.attempt,
        "begin_request_sha256": begin_request_sha256,
        "claim_id": claim.claim_id,
        "contract": RESULT_BINDING_CONTRACT,
        "format_version": 1,
        "migration_epoch": claim.migration_epoch,
        "operation_epoch": claim.operation_epoch,
        "payload": deepcopy(dict(payload.value)),
        "payload_bytes": payload.bytes_count,
        "payload_sha256": payload.sha256,
        "repo_uuid": canonical_repo_uuid,
        "work": claim.work.to_dict(),
        "work_sha256": work_sha256,
    }
    _report_progress(progress)
    return parse_result_binding(
        canonical_protocol_bytes(value, progress=progress),
        progress=progress,
    )


def parse_result_binding(
    raw: bytes,
    *,
    progress: Callable[[], object] | None = None,
) -> ResultBinding:
    if not isinstance(raw, bytes) or len(raw) > COMPLETE_MAX_BYTES:
        raise SemanticResultInvalid("result binding exceeds its byte limit")
    _report_progress(progress)
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise SemanticResultInvalid("result binding is not valid JSON") from exc
    _report_progress(progress)
    if not isinstance(parsed, dict):
        raise SemanticResultInvalid("result binding must be an object")
    value = cast(dict[str, object], parsed)
    expected = {
        "active_source_revision",
        "attempt",
        "begin_request_sha256",
        "claim_id",
        "contract",
        "format_version",
        "migration_epoch",
        "operation_epoch",
        "payload",
        "payload_bytes",
        "payload_sha256",
        "repo_uuid",
        "work",
        "work_sha256",
    }
    if set(value) != expected or value.get("contract") != RESULT_BINDING_CONTRACT:
        raise SemanticResultInvalid("result binding field set or contract is invalid")
    if value.get("format_version") != 1 or type(value.get("format_version")) is not int:
        raise SemanticResultInvalid("result binding version is invalid")
    for field in ("active_source_revision", "attempt", "operation_epoch"):
        if type(value.get(field)) is not int or cast(int, value[field]) < 1:
            raise SemanticResultInvalid(f"result binding {field} is invalid")
    if type(value.get("migration_epoch")) is not int or cast(int, value["migration_epoch"]) < 0:
        raise SemanticResultInvalid("result binding migration_epoch is invalid")
    for field in ("begin_request_sha256", "claim_id", "payload_sha256", "work_sha256"):
        if (
            not isinstance(value.get(field), str)
            or _DIGEST_RE.fullmatch(cast(str, value[field])) is None
        ):
            raise SemanticResultInvalid(f"result binding {field} is invalid")
    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
        work = SemanticDesiredWork.from_mapping(cast(Mapping[str, object], value.get("work")))
    except (ContractError, TypeError, ValueError) as exc:
        raise SemanticResultInvalid("result binding authority is invalid") from exc
    if value.get("work_sha256") != _work_digest(work, progress=progress):
        raise SemanticResultInvalid("result binding work digest is invalid")
    payload = validate_bound_payload(
        value.get("payload"),
        work,
        progress=progress,
    )
    if (
        type(value.get("payload_bytes")) is not int
        or value.get("payload_bytes") != payload.bytes_count
    ):
        raise SemanticResultInvalid("result binding payload byte count is invalid")
    if value.get("payload_sha256") != payload.sha256:
        raise SemanticResultInvalid("result binding payload digest is invalid")
    value["repo_uuid"] = repo_uuid
    value["payload"] = deepcopy(dict(payload.value))
    canonical = canonical_protocol_bytes(value, progress=progress)
    if canonical != raw:
        raise SemanticResultInvalid("result binding is not canonical")
    return ResultBinding(value=value, canonical=raw)


def load_request_schema() -> dict[str, Any]:
    value = json.loads(_REQUEST_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("semantic-worker request schema must be an object")
    return cast(dict[str, Any], value)


def load_result_schema() -> dict[str, Any]:
    value = json.loads(_RESULT_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("semantic-worker result schema must be an object")
    return cast(dict[str, Any], value)


def _result_common(kind: str) -> dict[str, object]:
    return {
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "contract": RESULT_CONTRACT,
        "kind": kind,
        "schema_version": SCHEMA_VERSION,
    }


def _failure_result(
    *,
    outcome: str,
    reason_code: str,
    action_code: str,
    exit_code: int,
    begin_request_sha256: str | None,
) -> dict[str, object]:
    value: dict[str, object] = {
        **_result_common("terminal"),
        "action_code": action_code,
        "exit_code": exit_code,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    if begin_request_sha256 is not None:
        value["begin_request_sha256"] = begin_request_sha256
    return value


def _validate_result_value(value: Mapping[str, object]) -> None:
    common = {"cli_contract_version", "contract", "kind", "schema_version"}
    if value.get("contract") != RESULT_CONTRACT:
        raise SemanticResultInvalid("result contract is unsupported")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise SemanticResultInvalid("result schema version is unsupported")
    if type(value.get("cli_contract_version")) is not int or value.get("cli_contract_version") != 1:
        raise SemanticResultInvalid("result CLI contract version is unsupported")
    kind = value.get("kind")
    if kind == "work":
        expected = common | {
            "attempt",
            "begin_request_sha256",
            "claim_id",
            "repo_uuid",
            "work",
            "work_sha256",
        }
        if set(value) != expected:
            raise SemanticResultInvalid("work result field set is invalid")
        _positive_result_integer(value.get("attempt"), "attempt")
        _result_digest(value.get("begin_request_sha256"), "begin_request_sha256")
        _result_digest(value.get("claim_id"), "claim_id")
        _result_digest(value.get("work_sha256"), "work_sha256")
        try:
            WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
            work = SemanticDesiredWork.from_mapping(cast(Mapping[str, object], value.get("work")))
        except (ContractError, TypeError, ValueError) as exc:
            raise SemanticResultInvalid("work result authority is invalid") from exc
        if value.get("work_sha256") != _work_digest(work):
            raise SemanticResultInvalid("work result work digest is invalid")
        return
    if kind == "checkpointed":
        expected = common | {
            "begin_request_sha256",
            "checkpoint_sha256",
            "claim_id",
        }
        if set(value) != expected:
            raise SemanticResultInvalid("checkpoint result field set is invalid")
        for field in ("begin_request_sha256", "checkpoint_sha256", "claim_id"):
            _result_digest(value.get(field), field)
        return
    if kind != "terminal":
        raise SemanticResultInvalid("result kind is unsupported")
    outcome = value.get("outcome")
    exit_code = value.get("exit_code")
    terminal_common = common | {"exit_code", "outcome"}
    if outcome == "completed":
        expected = terminal_common | {
            "attempt",
            "begin_request_sha256",
            "claim_id",
            "completed_watermark",
            "payload_bytes",
            "payload_kind",
            "payload_sha256",
            "queue_revision",
            "repo_uuid",
            "result_binding_bytes",
            "result_binding_sha256",
            "work_sha256",
        }
        if set(value) != expected or type(exit_code) is not int or exit_code != 0:
            raise SemanticResultInvalid("completed result field set or exit code is invalid")
        for field in ("attempt", "payload_bytes", "result_binding_bytes"):
            _positive_result_integer(value.get(field), field)
        for field in ("queue_revision", "completed_watermark"):
            _nonnegative_result_integer(value.get(field), field)
        for field in (
            "begin_request_sha256",
            "claim_id",
            "payload_sha256",
            "result_binding_sha256",
            "work_sha256",
        ):
            _result_digest(value.get(field), field)
        try:
            WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
        except ContractError as exc:
            raise SemanticResultInvalid("completed result repo UUID is invalid") from exc
        if value.get("payload_kind") not in {"semantic_fragment", "delete_tombstone"}:
            raise SemanticResultInvalid("completed payload kind is invalid")
        return
    if outcome == "idle":
        expected = terminal_common | {
            "begin_request_sha256",
            "completed_watermark",
            "desired_watermark",
            "queue_revision",
            "repo_uuid",
        }
        if set(value) != expected or type(exit_code) is not int or exit_code != 0:
            raise SemanticResultInvalid("idle result field set or exit code is invalid")
        for field in ("queue_revision", "desired_watermark", "completed_watermark"):
            _nonnegative_result_integer(value.get(field), field)
        if cast(int, value["completed_watermark"]) > cast(int, value["desired_watermark"]):
            raise SemanticResultInvalid("idle completed watermark exceeds desired watermark")
        _result_digest(value.get("begin_request_sha256"), "begin_request_sha256")
        try:
            WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
        except ContractError as exc:
            raise SemanticResultInvalid("idle result repo UUID is invalid") from exc
        return
    expected_without_begin = terminal_common | {"action_code", "reason_code"}
    expected_with_begin = expected_without_begin | {"begin_request_sha256"}
    if set(value) not in {frozenset(expected_without_begin), frozenset(expected_with_begin)}:
        raise SemanticResultInvalid("failure terminal field set is invalid")
    has_begin = "begin_request_sha256" in value
    if has_begin:
        _result_digest(value.get("begin_request_sha256"), "begin_request_sha256")
    reason = value.get("reason_code")
    action = value.get("action_code")
    pre_begin_only = {
        "semantic_worker_request_invalid",
        "runtime_authority_missing",
        "runtime_authority_invalid",
        "runtime_authority_unsupported",
    }
    post_begin_only = {
        "workspace_config_invalid",
        "semantic_queue_invalid",
        "registry_invalid",
        "workspace_state_invalid",
        "semantic_worker_commit_unknown",
    }
    if (
        (outcome != "invalid" and not has_begin)
        or (reason in pre_begin_only and has_begin)
        or (reason in post_begin_only and not has_begin)
    ):
        raise SemanticResultInvalid("failure terminal begin binding is invalid")
    if outcome == "retry_scheduled":
        if type(exit_code) is not int or exit_code != 10:
            raise SemanticResultInvalid("retry result exit code is invalid")
        if (
            not isinstance(reason, str)
            or _FAILURE_ACTIONS.get(("retry_scheduled", reason)) != action
        ):
            raise SemanticResultInvalid("retry result route is invalid")
        return
    if outcome == "withheld":
        if type(exit_code) is not int or exit_code != 10:
            raise SemanticResultInvalid("withheld result exit code is invalid")
        if not isinstance(reason, str) or _WITHHELD_ACTIONS.get(reason) != action:
            raise SemanticResultInvalid("withheld result route is invalid")
        return
    if outcome == "dead_lettered":
        if (
            type(exit_code) is not int
            or exit_code != 20
            or reason not in _ALL_QUEUE_FAILURES
            or action != "inspect_semantic_queue"
        ):
            raise SemanticResultInvalid("dead-letter result route is invalid")
        return
    if outcome == "invalid":
        if (
            type(exit_code) is not int
            or exit_code != 20
            or not isinstance(reason, str)
            or _INVALID_ACTIONS.get(reason) != action
        ):
            raise SemanticResultInvalid("invalid result route is invalid")
        return
    if outcome == "commit_unknown":
        if (
            type(exit_code) is not int
            or exit_code != 20
            or reason != "semantic_worker_commit_unknown"
            or action != "none"
        ):
            raise SemanticResultInvalid("commit-unknown result route is invalid")
        return
    raise SemanticResultInvalid("terminal outcome is unsupported")


def _positive_result_integer(value: object, label: str) -> int:
    if type(value) is not int or cast(int, value) < 1:
        raise SemanticResultInvalid(f"result {label} must be a positive integer")
    return cast(int, value)


def _nonnegative_result_integer(value: object, label: str) -> int:
    if type(value) is not int or cast(int, value) < 0:
        raise SemanticResultInvalid(f"result {label} must be a nonnegative integer")
    return cast(int, value)


def _result_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SemanticResultInvalid(f"result {label} is invalid")
    return value


def canonical_result_bytes(value: Mapping[str, object]) -> bytes:
    try:
        _validate_result_value(value)
        encoded = canonical_protocol_bytes(value)
    except RecursionError as exc:
        raise SemanticResultInvalid("public result nesting is too deep") from exc
    if len(encoded) > RESULT_MAX_BYTES:
        raise SemanticResultInvalid("public result exceeds the 64-KiB limit")
    return encoded


def parse_result_frame(raw: bytes) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > RESULT_MAX_BYTES:
        raise SemanticResultInvalid("public result exceeds the 64-KiB limit")
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise SemanticResultInvalid("public result is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise SemanticResultInvalid("public result must be an object")
    value = cast(dict[str, object], parsed)

    def reject_decimal(item: object) -> None:
        if isinstance(item, Decimal):
            raise SemanticResultInvalid("public results admit integers only")
        if isinstance(item, dict):
            for nested in item.values():
                reject_decimal(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_decimal(nested)

    try:
        reject_decimal(value)
        _validate_result_value(value)
        canonical = canonical_protocol_bytes(value)
    except RecursionError as exc:
        raise SemanticResultInvalid("public result nesting is too deep") from exc
    if canonical != raw:
        raise SemanticResultInvalid("public result is not canonical")
    return value


def _write_result_frame(
    stream: BinaryIO | TextIO,
    value: Mapping[str, object],
    *,
    monotonic_clock: Callable[[], int],
    work_deadline_ns: int | None = None,
) -> None:
    payload = canonical_result_bytes(value)
    encoded_at = monotonic_clock()
    delivery_deadline = encoded_at + _OUTPUT_WINDOW_NS
    if work_deadline_ns is not None:
        delivery_deadline = min(delivery_deadline, work_deadline_ns)
    if encoded_at >= delivery_deadline:
        raise SemanticOutputDeliveryError("result delivery deadline expired")
    descriptor: int | None
    try:
        descriptor = stream.fileno()
    except AttributeError, OSError, ValueError:
        descriptor = None
    if descriptor is None:
        _write_stream_without_fd(
            stream,
            payload,
            monotonic_clock=monotonic_clock,
            deadline_ns=delivery_deadline,
        )
        return
    try:
        blocking = os.get_blocking(descriptor)
    except OSError as exc:
        raise SemanticOutputDeliveryError("result output is closed") from exc
    try:
        try:
            os.set_blocking(descriptor, False)
        except OSError as exc:
            raise SemanticOutputDeliveryError(
                "result output could not be made nonblocking"
            ) from exc
        offset = 0
        while offset < len(payload):
            now = monotonic_clock()
            if now >= delivery_deadline:
                raise SemanticOutputDeliveryError("result delivery deadline expired")
            timeout = max(0.0, (delivery_deadline - now) / 1_000_000_000)
            try:
                _readable, writable, _exceptional = select.select(
                    [],
                    [descriptor],
                    [],
                    timeout,
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as exc:
                raise SemanticOutputDeliveryError("result output readiness check failed") from exc
            if not writable:
                raise SemanticOutputDeliveryError("result output did not become writable")
            try:
                written = os.write(descriptor, payload[offset:])
            except InterruptedError:
                continue
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError) as exc:
                raise SemanticOutputDeliveryError("result output write failed") from exc
            if written <= 0:
                raise SemanticOutputDeliveryError("result output made no progress")
            offset += written
        try:
            stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise SemanticOutputDeliveryError("result output flush failed") from exc
        if monotonic_clock() >= delivery_deadline:
            raise SemanticOutputDeliveryError("result output flush exceeded its deadline")
    finally:
        try:
            os.set_blocking(descriptor, blocking)
        except OSError:
            pass


def _write_stream_without_fd(
    stream: BinaryIO | TextIO,
    payload: bytes,
    *,
    monotonic_clock: Callable[[], int],
    deadline_ns: int,
) -> None:
    offset = 0
    while offset < len(payload):
        if monotonic_clock() >= deadline_ns:
            raise SemanticOutputDeliveryError("result delivery deadline expired")
        try:
            remaining = payload[offset:]
            try:
                written = cast(BinaryIO, stream).write(remaining)
            except TypeError:
                remaining_text = remaining.decode("utf-8")
                written_characters = cast(TextIO, stream).write(remaining_text)
                if (
                    type(written_characters) is not int
                    or written_characters <= 0
                    or written_characters > len(remaining_text)
                ):
                    raise SemanticOutputDeliveryError("result output made no progress")
                written = len(remaining_text[:written_characters].encode("utf-8"))
        except InterruptedError:
            continue
        except SemanticOutputDeliveryError:
            raise
        except (BrokenPipeError, OSError, TypeError, ValueError) as exc:
            raise SemanticOutputDeliveryError("result output write failed") from exc
        if type(written) is not int or written <= 0 or written > len(payload) - offset:
            raise SemanticOutputDeliveryError("result output made no progress")
        offset += written
    try:
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise SemanticOutputDeliveryError("result output flush failed") from exc
    if monotonic_clock() >= deadline_ns:
        raise SemanticOutputDeliveryError("result output flush exceeded its deadline")


def emit_pre_begin_failure(
    stdout: BinaryIO | TextIO,
    *,
    reason_code: str,
    action_code: str,
    monotonic_clock: Callable[[], int] | None = None,
) -> int:
    """Emit one authority or pre-begin invalid terminal without touching runtime state."""

    clock = time.monotonic_ns if monotonic_clock is None else monotonic_clock
    value = _failure_result(
        outcome="invalid",
        reason_code=reason_code,
        action_code=action_code,
        exit_code=20,
        begin_request_sha256=None,
    )
    try:
        _write_result_frame(stdout, value, monotonic_clock=clock)
    except (KeyboardInterrupt, SemanticOutputDeliveryError):
        return 20
    return 20


class _FrameEof(SemanticWorkerError):
    pass


class _FrameDeadline(SemanticWorkerError):
    pass


@dataclass(frozen=True)
class _ReleaseInvalid(SemanticWorkerError):
    reason_code: str


class _FrameReader:
    def __init__(
        self,
        stream: BinaryIO | TextIO,
        *,
        monotonic_clock: Callable[[], int],
    ) -> None:
        self.stream = stream
        self.monotonic_clock = monotonic_clock
        self.buffer = bytearray()
        try:
            self.descriptor: int | None = stream.fileno()
        except AttributeError, OSError, ValueError:
            self.descriptor = None

    def read_frame(
        self,
        maximum: int,
        *,
        deadline_ns: int | None,
        action_limits: Mapping[bytes, int] | None = None,
    ) -> bytes:
        while True:
            effective_maximum, action_resolved = self._effective_limit(
                maximum,
                action_limits,
            )
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                frame = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                if len(frame) > effective_maximum:
                    raise SemanticWorkerRequestInvalid("request frame exceeds its byte limit")
                return frame
            if len(self.buffer) > effective_maximum:
                raise SemanticWorkerRequestInvalid("request frame exceeds its byte limit")
            if deadline_ns is not None and self.monotonic_clock() >= deadline_ns:
                raise _FrameDeadline("protocol frame deadline expired")
            read_maximum = effective_maximum + 1 - len(self.buffer)
            if action_limits is not None and not action_resolved:
                read_maximum = min(read_maximum, max(1, 64 - len(self.buffer)))
            chunk = self._read_chunk(read_maximum, deadline_ns)
            if not chunk:
                raise _FrameEof("protocol input closed")
            self.buffer.extend(chunk)

    def _effective_limit(
        self,
        default: int,
        action_limits: Mapping[bytes, int] | None,
    ) -> tuple[int, bool]:
        if action_limits is None:
            return default, True
        current = bytes(self.buffer)
        for prefix, limit in action_limits.items():
            if current.startswith(prefix):
                return limit, True
        if any(prefix.startswith(current) for prefix in action_limits):
            return default, False
        return SMALL_FRAME_MAX_BYTES, True

    def _read_chunk(self, maximum: int, deadline_ns: int | None) -> bytes:
        if self.descriptor is None:
            try:
                try:
                    value = self.stream.readline(maximum)
                except TypeError:
                    value = self.stream.readline()
            except (OSError, TypeError, ValueError) as exc:
                raise _FrameEof("protocol input could not be read") from exc
            if deadline_ns is not None and self.monotonic_clock() >= deadline_ns:
                raise _FrameDeadline("protocol frame deadline expired")
            if isinstance(value, str):
                return value.encode("utf-8")
            try:
                return bytes(value)
            except (TypeError, ValueError) as exc:
                raise _FrameEof("protocol input could not be read") from exc
        while True:
            timeout: float | None = None
            if deadline_ns is not None:
                remaining = deadline_ns - self.monotonic_clock()
                if remaining <= 0:
                    raise _FrameDeadline("protocol frame deadline expired")
                timeout = remaining / 1_000_000_000
            try:
                readable, _writable, _exceptional = select.select(
                    [self.descriptor],
                    [],
                    [],
                    timeout,
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as exc:
                raise _FrameEof("protocol input could not be read") from exc
            if not readable:
                raise _FrameDeadline("protocol frame deadline expired")
            try:
                return os.read(self.descriptor, min(maximum, 64 * 1024))
            except InterruptedError:
                continue
            except OSError as exc:
                raise _FrameEof("protocol input could not be read") from exc


@dataclass(frozen=True)
class _Preflight:
    repo_uuid: str
    registry_revision: int
    active_source_revision: int
    lease_state: WorkspaceLeaseState
    queue_snapshot: SemanticQueueSnapshot
    config: WorkspaceConfig
    source_root: Path


def _workspace_entry(document: Mapping[str, object], repo_uuid: str) -> dict[str, object]:
    entries = [
        entry
        for entry in cast(list[dict[str, object]], document.get("workspaces"))
        if entry.get("repo_uuid") == repo_uuid
    ]
    if len(entries) != 1:
        raise RevisionConflict("workspace registry selection changed")
    return entries[0]


def _preflight(
    runtime: WorkspaceRuntime,
    begin: Mapping[str, object],
    *,
    deadline_ns: int,
    monotonic_clock: Callable[[], int],
) -> _Preflight:
    repo_uuid = cast(str, begin["repo_uuid"])
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        registry_value = registry.to_dict()
        entry = _workspace_entry(registry_value, repo_uuid)
        registry_revision = cast(int, registry_value["revision"])
        active_source_revision = cast(int, entry["active_source_revision"])
        with runtime.leases.read_only_workspace_lock(
            repo_uuid,
            deadline_ns=deadline_ns,
        ):
            try:
                lease_state = runtime.leases.read_only_snapshot_locked(
                    registry,
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
            except (StateCorrupt, StateRecoveryRequired) as exc:
                raise LeaseRecoveryRequired("workspace lease state requires recovery") from exc
            try:
                queue_snapshot = runtime.semantic_queue.read_only_snapshot_locked(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
            except StateRecoveryRequired as exc:
                raise SemanticQueueCorrupt("semantic queue state requires recovery") from exc
        recorded_source = deepcopy(cast(dict[str, object], entry["active_source"]))
    expected = {
        "expected_registry_revision": registry_revision,
        "expected_active_source_revision": active_source_revision,
        "expected_operation_epoch": lease_state.operation_epoch,
        "expected_migration_epoch": lease_state.migration_epoch,
        "expected_queue_revision": queue_snapshot.revision,
        "expected_desired_watermark": queue_snapshot.desired_watermark,
    }
    if any(begin[field] != observed for field, observed in expected.items()):
        raise RevisionConflict("semantic-worker authority coordinates changed")
    semantic_lease = lease_state.leases.get("semantic")
    if semantic_lease is not None:
        live_until = cast(int, semantic_lease.to_dict()["liveness_deadline_monotonic_ns"])
        if monotonic_clock() < live_until:
            raise LeaseBusy("semantic claim lease is already live")
    source_root = Path(cast(str, recorded_source["path"]))
    try:
        current_root = Path.cwd().resolve(strict=True)
        expected_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise SourceDiscoveryError("active source top level is unavailable") from exc
    if current_root != expected_root:
        raise RevisionConflict("current directory is not the exact active Git top level")
    discovered = discover_source(
        source_root,
        deadline_ns=deadline_ns,
        max_bytes=_WORKSPACE_CONFIG_MAX_BYTES,
    )
    if discovered.repo_uuid != repo_uuid or discovered.registry_source != recorded_source:
        raise RevisionConflict("active source no longer matches registry authority")
    config = read_workspace_config(
        source_root,
        deadline_ns=deadline_ns,
        max_bytes=_WORKSPACE_CONFIG_MAX_BYTES,
    )
    return _Preflight(
        repo_uuid=repo_uuid,
        registry_revision=registry_revision,
        active_source_revision=active_source_revision,
        lease_state=lease_state,
        queue_snapshot=queue_snapshot,
        config=config,
        source_root=source_root,
    )


def _read_lease_state(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    *,
    deadline_ns: int,
) -> tuple[int, int, WorkspaceLeaseState]:
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        registry_value = registry.to_dict()
        entry = _workspace_entry(registry_value, repo_uuid)
        with runtime.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            state = runtime.leases.read_only_snapshot_locked(
                registry,
                repo_uuid,
                deadline_ns=deadline_ns,
            )
        return (
            cast(int, registry_value["revision"]),
            cast(int, entry["active_source_revision"]),
            state,
        )


def _read_uncertain_lease_state(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    *,
    deadline_ns: int,
) -> tuple[int, int, WorkspaceLeaseState, bool]:
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        registry_value = registry.to_dict()
        entry = _workspace_entry(registry_value, repo_uuid)
        with runtime.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            state, pending_present = runtime.leases.read_uncertain_snapshot_locked(
                registry,
                repo_uuid,
                deadline_ns=deadline_ns,
            )
        return (
            cast(int, registry_value["revision"]),
            cast(int, entry["active_source_revision"]),
            state,
            pending_present,
        )


def _read_uncertain_queue(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    *,
    deadline_ns: int,
) -> tuple[SemanticQueueSnapshot, bool, int]:
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        registry_revision = cast(int, registry.to_dict()["revision"])
        with runtime.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            snapshot, pending_present = runtime.semantic_queue.read_uncertain_snapshot_locked(
                repo_uuid,
                deadline_ns=deadline_ns,
            )
            return snapshot, pending_present, registry_revision


def _grant_from_state(
    state: WorkspaceLeaseState,
    *,
    registry_revision: int,
    active_source_revision: int,
) -> LeaseGrant | None:
    lease = state.leases.get("semantic")
    if lease is None:
        return None
    epoch = state.lease_epochs.get("semantic")
    if epoch is None:
        return None
    return LeaseGrant(
        lease=lease,
        registry_revision=registry_revision,
        active_source_revision=active_source_revision,
        operation_epoch=epoch,
        migration_epoch=state.migration_epoch,
    )


def _source_observation(
    source_root: Path,
    work: SemanticDesiredWork,
    *,
    deadline_ns: int,
    monotonic_clock: Callable[[], int],
    progress: Callable[[], object] | None = None,
) -> None:
    def check_progress() -> None:
        if progress is not None:
            progress()
        if monotonic_clock() >= deadline_ns:
            raise _FrameDeadline("source observation exceeded the work deadline")

    check_progress()
    path = PurePosixPath(work.path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SemanticSourceUnavailable("claimed source path is not contained")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(source_root, directory_flags)
        descriptors.append(root_descriptor)
        check_progress()
        parent = root_descriptor
        for component in path.parts[:-1]:
            parent = os.open(component, directory_flags, dir_fd=parent)
            descriptors.append(parent)
            check_progress()
        name = path.parts[-1]
        if work.operation == "DELETE":
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                check_progress()
                return
            except OSError as exc:
                raise SemanticSourceUnavailable("source absence could not be proved") from exc
            check_progress()
            raise SemanticSourceChanged("DELETE source is present")
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            raise SemanticSourceUnavailable("UPSERT source could not be opened") from exc
        descriptors.append(descriptor)
        check_progress()
        before = os.fstat(descriptor)
        check_progress()
        if not stat.S_ISREG(before.st_mode):
            raise SemanticSourceUnavailable("UPSERT source is not a regular file")
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            check_progress()
            try:
                chunk = os.read(descriptor, _SOURCE_CHUNK_BYTES)
            except InterruptedError:
                continue
            except OSError as exc:
                raise SemanticSourceUnavailable("UPSERT source read was incomplete") from exc
            check_progress()
            if not chunk:
                break
            bytes_read += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        check_progress()
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if not stable or bytes_read != before.st_size:
            raise SemanticSourceUnavailable("UPSERT source changed during observation")
        if digest.hexdigest() != work.content_sha256:
            raise SemanticSourceChanged("UPSERT source digest differs from desired work")
    except _FrameDeadline:
        raise
    except SemanticSourceUnavailable, SemanticSourceChanged:
        raise
    except OSError as exc:
        raise SemanticSourceUnavailable("source path traversal was incomplete") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _claim_matches_grant(claim: SemanticClaim, grant: LeaseGrant) -> bool:
    lease = grant.lease.to_dict()
    return (
        claim.fence_token == lease["fence_token"]
        and claim.operation_epoch == grant.operation_epoch
        and claim.migration_epoch == grant.migration_epoch
        and claim.active_source_revision == grant.active_source_revision
        and dict(claim.owner) == lease["owner"]
    )


def _find_claims(snapshot: SemanticQueueSnapshot) -> list[SemanticClaim]:
    return [
        item.claim for item in snapshot.items if item.status == "claimed" and item.claim is not None
    ]


def _eligible_items(snapshot: SemanticQueueSnapshot) -> list[object]:
    pending = [item for item in snapshot.items if item.status == "pending"]
    return [
        item
        for item in pending
        if not any(
            other.path == item.path
            and other.desired_revision < item.desired_revision
            and other.status != "completed"
            for other in snapshot.items
        )
    ]


@dataclass
class _WorkerSession:
    runtime: WorkspaceRuntime
    reader: _FrameReader
    stdout: BinaryIO | TextIO
    begin: SemanticWorkerRequest
    monotonic_clock: Callable[[], int]
    wall_clock: Callable[[], datetime]
    deadline_ns: int
    preflight: _Preflight | None = None
    grant: LeaseGrant | None = None
    claim: SemanticClaim | None = None
    work_sha256: str | None = None
    next_heartbeat_ns: int | None = None
    expected_queue_revision: int | None = None
    expected_desired_watermark: int | None = None

    def __post_init__(self) -> None:
        if self.expected_queue_revision is None:
            self.expected_queue_revision = cast(
                int,
                self.begin.value["expected_queue_revision"],
            )
        if self.expected_desired_watermark is None:
            self.expected_desired_watermark = cast(
                int,
                self.begin.value["expected_desired_watermark"],
            )

    @property
    def begin_sha256(self) -> str:
        return sha256(self.begin.canonical)

    @property
    def begin_value(self) -> Mapping[str, object]:
        return self.begin.value

    def _ensure_work_time(self) -> int:
        now = self.monotonic_clock()
        if now >= self.deadline_ns:
            raise _FrameDeadline("semantic-worker work deadline expired")
        if (
            self.grant is not None
            and self.claim is not None
            and self.next_heartbeat_ns is not None
            and now >= self.next_heartbeat_ns
        ):
            self._heartbeat()
            now = self.monotonic_clock()
            if now >= self.deadline_ns:
                raise _FrameDeadline("semantic-worker work deadline expired")
        return now

    def _emit(self, value: Mapping[str, object], *, work_bounded: bool = False) -> None:
        try:
            _write_result_frame(
                self.stdout,
                value,
                monotonic_clock=self.monotonic_clock,
                work_deadline_ns=self.deadline_ns if work_bounded else None,
            )
        except KeyboardInterrupt as exc:
            raise SemanticOutputDeliveryError("result output interrupted") from exc

    def _emit_route(
        self,
        *,
        outcome: str,
        reason_code: str,
        action_code: str,
        exit_code: int,
    ) -> int:
        value = _failure_result(
            outcome=outcome,
            reason_code=reason_code,
            action_code=action_code,
            exit_code=exit_code,
            begin_request_sha256=self.begin_sha256,
        )
        try:
            self._emit(value)
        except SemanticOutputDeliveryError:
            return 20
        return exit_code

    def _commit_unknown(self) -> int:
        return self._emit_route(
            outcome="commit_unknown",
            reason_code="semantic_worker_commit_unknown",
            action_code="none",
            exit_code=20,
        )

    def _withheld(self, reason_code: str) -> int:
        return self._emit_route(
            outcome="withheld",
            reason_code=reason_code,
            action_code=_WITHHELD_ACTIONS[reason_code],
            exit_code=10,
        )

    def _invalid(self, reason_code: str) -> int:
        return self._emit_route(
            outcome="invalid",
            reason_code=reason_code,
            action_code=_INVALID_ACTIONS[reason_code],
            exit_code=20,
        )

    def _acquire(self) -> LeaseGrant:
        preflight = cast(_Preflight, self.preflight)
        owner = self.runtime.leases.current_owner()
        acquired_at = self.wall_clock().astimezone(timezone.utc)
        monotonic_ns = self._ensure_work_time()
        expected_deadline = monotonic_ns + _LEASE_TTL_NS

        def attempt() -> LeaseGrant:
            return self.runtime.leases.acquire(
                preflight.repo_uuid,
                "SEMANTIC_CLAIM",
                owner,
                expected_registry_revision=cast(
                    int, self.begin_value["expected_registry_revision"]
                ),
                expected_active_source_revision=cast(
                    int, self.begin_value["expected_active_source_revision"]
                ),
                expected_operation_epoch=cast(int, self.begin_value["expected_operation_epoch"]),
                expected_migration_epoch=cast(int, self.begin_value["expected_migration_epoch"]),
                acquired_at=acquired_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=_LEASE_TTL_NS,
                deadline_ns=self.deadline_ns,
            )

        for retry in range(2):
            try:
                return attempt()
            except (CommitUnknown, InjectedFault, KeyboardInterrupt) as fault:
                interrupted = _caused_by_keyboard_interrupt(fault)
                try:
                    (
                        registry_revision,
                        active_revision,
                        observed,
                        pending_present,
                    ) = _read_uncertain_lease_state(
                        self.runtime,
                        preflight.repo_uuid,
                        deadline_ns=self.deadline_ns,
                    )
                except (Exception, KeyboardInterrupt) as exc:
                    raise CommitUnknown("semantic lease acquisition cannot be reconciled") from exc
                if (
                    registry_revision != preflight.registry_revision
                    or active_revision != preflight.active_source_revision
                    or observed.migration_epoch != preflight.lease_state.migration_epoch
                    or observed.operation_epoch
                    not in {
                        preflight.lease_state.operation_epoch,
                        preflight.lease_state.operation_epoch + 1,
                    }
                ):
                    raise _TerminalRoute(
                        "withheld",
                        "semantic_authority_stale",
                        "retry_status",
                        10,
                    )
                observed_grant = _grant_from_state(
                    observed,
                    registry_revision=registry_revision,
                    active_source_revision=active_revision,
                )
                if observed_grant is not None:
                    lease = observed_grant.lease.to_dict()
                    expected_timestamp = _canonical_timestamp(acquired_at)
                    observed_other_leases = {
                        key: value for key, value in observed.leases.items() if key != "semantic"
                    }
                    expected_other_leases = {
                        key: value
                        for key, value in preflight.lease_state.leases.items()
                        if key != "semantic"
                    }
                    observed_other_epochs = {
                        key: value
                        for key, value in observed.lease_epochs.items()
                        if key != "semantic"
                    }
                    expected_other_epochs = {
                        key: value
                        for key, value in preflight.lease_state.lease_epochs.items()
                        if key != "semantic"
                    }
                    exact = (
                        registry_revision == preflight.registry_revision
                        and active_revision == preflight.active_source_revision
                        and observed.revision == preflight.lease_state.revision + 1
                        and observed.fence_high_watermark
                        == preflight.lease_state.fence_high_watermark + 1
                        and observed.operation_epoch == preflight.lease_state.operation_epoch + 1
                        and observed.migration_epoch == preflight.lease_state.migration_epoch
                        and observed_grant.operation_epoch
                        == preflight.lease_state.operation_epoch + 1
                        and observed_other_leases == expected_other_leases
                        and observed_other_epochs == expected_other_epochs
                        and observed.staged_attempt_sha256
                        == preflight.lease_state.staged_attempt_sha256
                        and lease["operation"] == "SEMANTIC_CLAIM"
                        and lease["fence_token"] == preflight.lease_state.fence_high_watermark + 1
                        and lease["owner"] == owner.to_dict()
                        and lease["acquired_at"] == expected_timestamp
                        and lease["heartbeat_at"] == expected_timestamp
                        and lease["liveness_deadline_monotonic_ns"] == expected_deadline
                    )
                    if exact:
                        if pending_present:
                            try:
                                recovered = self.runtime.leases.recover_uncertain_snapshot(
                                    preflight.repo_uuid,
                                    deadline_ns=self.deadline_ns,
                                )
                            except (Exception, KeyboardInterrupt) as exc:
                                raise CommitUnknown(
                                    "semantic lease acquisition recovery is ambiguous"
                                ) from exc
                            if recovered != observed:
                                raise CommitUnknown(
                                    "recovered semantic lease differs from adopted bytes"
                                )
                        if interrupted:
                            self.grant = observed_grant
                            raise KeyboardInterrupt
                        return observed_grant
                    if lease["owner"] != owner.to_dict() and self.monotonic_clock() < cast(
                        int, lease["liveness_deadline_monotonic_ns"]
                    ):
                        if interrupted:
                            raise KeyboardInterrupt
                        raise _TerminalRoute(
                            "withheld",
                            "semantic_claim_contended",
                            "retry_status",
                            10,
                        )
                if (
                    retry == 0
                    and registry_revision == preflight.registry_revision
                    and active_revision == preflight.active_source_revision
                    and observed == preflight.lease_state
                    and not pending_present
                    and self.monotonic_clock() < self.deadline_ns
                ):
                    if interrupted:
                        raise KeyboardInterrupt
                    continue
                raise CommitUnknown("semantic lease acquisition outcome is uncertain")
        raise CommitUnknown("semantic lease acquisition outcome is uncertain")

    def _expected_claim_snapshot(
        self,
        observed_claim: SemanticClaim | None,
    ) -> tuple[SemanticQueueSnapshot, SemanticDesiredWork | None]:
        preflight = cast(_Preflight, self.preflight)
        current = preflight.queue_snapshot
        recovered_items = []
        changed = False
        grant = cast(LeaseGrant, self.grant)
        for item in current.items:
            if item.status != "claimed" or item.claim is None:
                recovered_items.append(item)
                continue
            if _claim_matches_grant(item.claim, grant):
                recovered_items.append(item)
                continue
            failures = item.failure_count + 1
            recovered_items.append(
                item.__class__(
                    work=item.work,
                    status=(
                        "pending"
                        if failures <= self.runtime.semantic_queue.policy.retry_budget
                        else "dead_letter"
                    ),
                    failure_count=failures,
                    last_error="claim_expired",
                    claim=None,
                )
            )
            changed = True
        base = replace(
            current,
            items=self.runtime.semantic_queue._sorted_items(recovered_items),
        )
        eligible = cast(list[Any], _eligible_items(base))
        if not eligible:
            return (
                replace(base, revision=current.revision + (1 if changed else 0)),
                None,
            )
        selected_operation = self.runtime.semantic_queue._next_operation(
            current.last_served_operation,
            eligible,
        )
        selected = min(
            (item for item in eligible if item.operation == selected_operation),
            key=lambda item: (item.desired_revision, item.path, item.content_sha256),
        )
        if observed_claim is None:
            return base, selected.work
        claimed_items = [
            item.__class__(
                work=item.work,
                status="claimed",
                failure_count=item.failure_count,
                last_error=item.last_error,
                claim=observed_claim,
            )
            if item.work.coalescing_key == selected.work.coalescing_key
            else item
            for item in base.items
        ]
        expected = replace(
            base,
            revision=current.revision + 1,
            last_served_operation=selected_operation,
            items=self.runtime.semantic_queue._sorted_items(claimed_items),
        )
        return expected, selected.work

    def _claim_work(self) -> SemanticClaim | None:
        preflight = cast(_Preflight, self.preflight)
        grant = cast(LeaseGrant, self.grant)
        expected_revision = cast(int, self.expected_queue_revision)
        expected_watermark = cast(int, self.expected_desired_watermark)

        def attempt() -> SemanticClaim | None:
            return self.runtime.semantic_queue.claim(
                grant,
                config=preflight.config,
                host_agent_active=True,
                explicit_backend=None,
                monotonic_ns=self._ensure_work_time(),
                expected_registry_revision=grant.registry_revision,
                expected_queue_revision=expected_revision,
                expected_desired_watermark=expected_watermark,
                deadline_ns=self.deadline_ns,
            )

        for retry in range(2):
            try:
                claimed = attempt()
                expected_snapshot, selected_work = self._expected_claim_snapshot(claimed)
                if claimed is None:
                    if selected_work is not None:
                        raise SemanticQueueCorrupt(
                            "semantic claim returned no work despite an eligible candidate"
                        )
                elif (
                    selected_work is None
                    or claimed.work != selected_work
                    or claimed.attempt != expected_revision + 1
                ):
                    raise SemanticQueueCorrupt(
                        "semantic claim return differs from the deterministic candidate"
                    )
                if (
                    expected_snapshot.desired_watermark != expected_watermark
                    or expected_snapshot.revision not in {expected_revision, expected_revision + 1}
                ):
                    raise SemanticQueueCorrupt(
                        "semantic claim advanced unexpected queue coordinates"
                    )
                self.expected_queue_revision = expected_snapshot.revision
                return claimed
            except (CommitUnknown, InjectedFault, KeyboardInterrupt) as fault:
                interrupted = _caused_by_keyboard_interrupt(fault)
                try:
                    observed, pending_present, registry_revision = _read_uncertain_queue(
                        self.runtime,
                        preflight.repo_uuid,
                        deadline_ns=self.deadline_ns,
                    )
                except (Exception, KeyboardInterrupt) as exc:
                    raise CommitUnknown("semantic queue claim cannot be reconciled") from exc
                if registry_revision != grant.registry_revision:
                    raise CommitUnknown(
                        "semantic registry authority changed during uncertain claim"
                    )
                matching = [
                    claim for claim in _find_claims(observed) if _claim_matches_grant(claim, grant)
                ]
                if len(matching) == 1:
                    expected, selected_work = self._expected_claim_snapshot(matching[0])
                    claim = matching[0]
                    if (
                        selected_work is not None
                        and claim.work == selected_work
                        and claim.attempt == expected_revision + 1
                        and observed.desired_watermark == expected_watermark
                        and observed == expected
                    ):
                        if pending_present:
                            try:
                                recovered = self.runtime.semantic_queue.recover_uncertain_snapshot(
                                    grant,
                                    monotonic_ns=self._ensure_work_time(),
                                    expected_registry_revision=grant.registry_revision,
                                    expected_queue_revision=observed.revision,
                                    expected_desired_watermark=expected_watermark,
                                    deadline_ns=self.deadline_ns,
                                )
                            except (Exception, KeyboardInterrupt) as exc:
                                raise CommitUnknown(
                                    "semantic queue claim recovery is ambiguous"
                                ) from exc
                            if recovered != expected:
                                raise CommitUnknown(
                                    "recovered semantic claim differs from adopted bytes"
                                )
                        self.expected_queue_revision = observed.revision
                        if interrupted:
                            self.claim = claim
                            raise KeyboardInterrupt
                        return claim
                recovery_only, selected_work = self._expected_claim_snapshot(None)
                if (
                    observed.desired_watermark == expected_watermark
                    and observed == recovery_only
                    and selected_work is None
                ):
                    if pending_present:
                        try:
                            recovered = self.runtime.semantic_queue.recover_uncertain_snapshot(
                                grant,
                                monotonic_ns=self._ensure_work_time(),
                                expected_registry_revision=grant.registry_revision,
                                expected_queue_revision=observed.revision,
                                expected_desired_watermark=expected_watermark,
                                deadline_ns=self.deadline_ns,
                            )
                        except (Exception, KeyboardInterrupt) as exc:
                            raise CommitUnknown(
                                "semantic queue recovery-only state is ambiguous"
                            ) from exc
                        if recovered != recovery_only:
                            raise CommitUnknown(
                                "recovered semantic queue differs from adopted bytes"
                            )
                    self.expected_queue_revision = observed.revision
                    if interrupted:
                        raise KeyboardInterrupt
                    return None
                if (
                    retry == 0
                    and observed == preflight.queue_snapshot
                    and observed.revision == expected_revision
                    and observed.desired_watermark == expected_watermark
                    and not pending_present
                    and self.monotonic_clock() < self.deadline_ns
                ):
                    if interrupted:
                        raise KeyboardInterrupt
                    continue
                raise CommitUnknown("semantic queue claim outcome is uncertain")
        raise CommitUnknown("semantic queue claim outcome is uncertain")

    @staticmethod
    def _require_session_coordinates(
        operation: object,
        snapshot: SemanticQueueSnapshot,
        *,
        grant: LeaseGrant,
        expected_queue_revision: int,
        expected_desired_watermark: int,
    ) -> None:
        operation_value = cast(Any, operation)
        if int(operation_value.registry.to_dict()["revision"]) != grant.registry_revision:
            raise SemanticQueueConflict("workspace registry revision changed")
        if snapshot.revision != expected_queue_revision:
            raise SemanticQueueConflict("semantic queue revision changed")
        if snapshot.desired_watermark != expected_desired_watermark:
            raise SemanticQueueConflict("semantic desired watermark changed")

    def _read_current_queue_snapshot(self, *, deadline_ns: int) -> SemanticQueueSnapshot:
        grant = cast(LeaseGrant, self.grant)
        with self.runtime.leases.current_operation_read_only(
            grant,
            monotonic_ns=self.monotonic_clock(),
            allowed_operations=frozenset({"SEMANTIC_CLAIM"}),
            deadline_ns=deadline_ns,
        ) as operation:
            snapshot = self.runtime.semantic_queue.read_only_snapshot_locked(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            self._require_session_coordinates(
                operation,
                snapshot,
                grant=grant,
                expected_queue_revision=cast(int, self.expected_queue_revision),
                expected_desired_watermark=cast(
                    int,
                    self.expected_desired_watermark,
                ),
            )
            return snapshot

    def _read_current_claim(self, *, deadline_ns: int) -> SemanticClaim:
        grant = cast(LeaseGrant, self.grant)
        expected = cast(SemanticClaim, self.claim)
        with self.runtime.leases.current_operation_read_only(
            grant,
            monotonic_ns=self.monotonic_clock(),
            allowed_operations=frozenset({"SEMANTIC_CLAIM"}),
            deadline_ns=deadline_ns,
        ) as operation:
            snapshot = self.runtime.semantic_queue.read_only_snapshot_locked(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            self._require_session_coordinates(
                operation,
                snapshot,
                grant=grant,
                expected_queue_revision=cast(int, self.expected_queue_revision),
                expected_desired_watermark=cast(
                    int,
                    self.expected_desired_watermark,
                ),
            )
            matching = [
                item.claim
                for item in snapshot.items
                if item.status == "claimed"
                and item.claim is not None
                and item.claim.claim_id == expected.claim_id
            ]
            if len(matching) != 1 or matching[0] != expected:
                raise StaleSemanticClaim("current semantic claim is absent or replaced")
            return matching[0]

    def _read_uncertain_current_claim(
        self,
        *,
        deadline_ns: int,
    ) -> tuple[SemanticClaim, bool, SemanticQueueSnapshot]:
        grant = cast(LeaseGrant, self.grant)
        expected = cast(SemanticClaim, self.claim)
        with self.runtime.leases.current_operation_read_only(
            grant,
            monotonic_ns=self.monotonic_clock(),
            allowed_operations=frozenset({"SEMANTIC_CLAIM"}),
            deadline_ns=deadline_ns,
        ) as operation:
            snapshot, pending_present = self.runtime.semantic_queue.read_uncertain_snapshot_locked(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if int(operation.registry.to_dict()["revision"]) != grant.registry_revision:
                raise CommitUnknown(
                    "workspace registry revision changed during uncertain checkpoint"
                )
            if snapshot.desired_watermark != cast(
                int,
                self.expected_desired_watermark,
            ):
                raise CommitUnknown(
                    "semantic desired watermark changed during uncertain checkpoint"
                )
            matching = [
                item.claim
                for item in snapshot.items
                if item.status == "claimed"
                and item.claim is not None
                and item.claim.claim_id == expected.claim_id
            ]
            if (
                len(matching) != 1
                or replace(matching[0], checkpoint=expected.checkpoint) != expected
            ):
                raise StaleSemanticClaim("current semantic claim is absent or replaced")
            return matching[0], pending_present, snapshot

    def _release(self, *, success_required: bool) -> bool:
        grant = self.grant
        if grant is None:
            return True
        lease_value = grant.lease.to_dict()
        liveness_deadline = cast(int, lease_value["liveness_deadline_monotonic_ns"])
        for retry in range(2):
            try:
                self.runtime.leases.release(grant, deadline_ns=liveness_deadline)
                self.grant = None
                return True
            except (StateCorrupt, StateRecoveryRequired, LeaseRecoveryRequired) as exc:
                if not success_required:
                    return True
                reason_code = self._classify_release_corruption(
                    cast(_Preflight, self.preflight).repo_uuid,
                    deadline_ns=liveness_deadline,
                )
                if reason_code is not None:
                    raise _ReleaseInvalid(reason_code) from exc
                return False
            except CommitUnknown, InjectedFault, KeyboardInterrupt:
                try:
                    (
                        _registry_revision,
                        _active_revision,
                        observed,
                        pending_present,
                    ) = _read_uncertain_lease_state(
                        self.runtime,
                        cast(_Preflight, self.preflight).repo_uuid,
                        deadline_ns=liveness_deadline,
                    )
                except Exception, KeyboardInterrupt:
                    return not success_required
                current = observed.leases.get("semantic")
                if current is None:
                    if pending_present:
                        try:
                            recovered = self.runtime.leases.recover_uncertain_snapshot(
                                cast(_Preflight, self.preflight).repo_uuid,
                                deadline_ns=liveness_deadline,
                            )
                        except Exception, KeyboardInterrupt:
                            return not success_required
                        if recovered != observed:
                            return not success_required
                    self.grant = None
                    return True
                if (
                    retry == 0
                    and current.to_dict() == lease_value
                    and not pending_present
                    and self.monotonic_clock() < liveness_deadline
                ):
                    continue
                return not success_required
            except Exception:
                return not success_required
        return not success_required

    def _classify_release_corruption(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int,
    ) -> str | None:
        try:
            registry_snapshot = self.runtime.registry.read_only_snapshot(deadline_ns=deadline_ns)
            with registry_snapshot as document:
                try:
                    with self.runtime.leases.read_only_workspace_lock(
                        repo_uuid,
                        deadline_ns=deadline_ns,
                    ):
                        self.runtime.leases.read_only_snapshot_locked(
                            document,
                            repo_uuid,
                            deadline_ns=deadline_ns,
                        )
                except StateCorrupt, StateRecoveryRequired, LeaseRecoveryRequired:
                    return "workspace_state_invalid"
                except Exception:
                    return None
        except StateCorrupt, StateRecoveryRequired:
            return "registry_invalid"
        except Exception:
            return None
        return "workspace_state_invalid"

    def _heartbeat(self) -> None:
        grant = cast(LeaseGrant, self.grant)
        heartbeat_at = self.wall_clock().astimezone(timezone.utc)
        monotonic_ns = self.monotonic_clock()
        if monotonic_ns >= self.deadline_ns:
            raise _FrameDeadline("semantic-worker work deadline expired")
        expected_liveness = monotonic_ns + _LEASE_TTL_NS
        before = grant.lease.to_dict()
        for retry in range(2):
            try:
                updated = self.runtime.leases.heartbeat(
                    grant,
                    heartbeat_at=heartbeat_at,
                    monotonic_ns=monotonic_ns,
                    ttl_ns=_LEASE_TTL_NS,
                    expected_registry_revision=grant.registry_revision,
                    deadline_ns=self.deadline_ns,
                )
                expected = {
                    **before,
                    "heartbeat_at": _canonical_timestamp(heartbeat_at),
                    "liveness_deadline_monotonic_ns": expected_liveness,
                }
                if (
                    updated.registry_revision != grant.registry_revision
                    or updated.active_source_revision != grant.active_source_revision
                    or updated.operation_epoch != grant.operation_epoch
                    or updated.migration_epoch != grant.migration_epoch
                    or updated.lease.to_dict() != expected
                ):
                    raise CommitUnknown("semantic heartbeat returned unexpected authority")
                self.grant = updated
                self.next_heartbeat_ns = monotonic_ns + _HEARTBEAT_INTERVAL_NS
                return
            except (CommitUnknown, InjectedFault, KeyboardInterrupt) as fault:
                interrupted = _caused_by_keyboard_interrupt(fault)
                try:
                    (
                        registry_revision,
                        active_revision,
                        observed,
                        pending_present,
                    ) = _read_uncertain_lease_state(
                        self.runtime,
                        cast(_Preflight, self.preflight).repo_uuid,
                        deadline_ns=self.deadline_ns,
                    )
                except (Exception, KeyboardInterrupt) as exc:
                    raise CommitUnknown("semantic lease heartbeat cannot be reconciled") from exc
                if (
                    registry_revision != grant.registry_revision
                    or active_revision != grant.active_source_revision
                    or observed.operation_epoch != grant.operation_epoch
                    or observed.migration_epoch != grant.migration_epoch
                    or observed.lease_epochs.get("semantic") not in {None, grant.operation_epoch}
                ):
                    raise StaleLease("semantic lease authority changed during heartbeat")
                adopted = _grant_from_state(
                    observed,
                    registry_revision=registry_revision,
                    active_source_revision=active_revision,
                )
                if adopted is not None:
                    value = adopted.lease.to_dict()
                    expected = {
                        **before,
                        "heartbeat_at": _canonical_timestamp(heartbeat_at),
                        "liveness_deadline_monotonic_ns": expected_liveness,
                    }
                    if value == expected:
                        if pending_present:
                            try:
                                recovered = self.runtime.leases.recover_uncertain_snapshot(
                                    cast(_Preflight, self.preflight).repo_uuid,
                                    deadline_ns=self.deadline_ns,
                                )
                            except (Exception, KeyboardInterrupt) as exc:
                                raise CommitUnknown(
                                    "semantic heartbeat recovery is ambiguous"
                                ) from exc
                            if recovered != observed:
                                raise CommitUnknown(
                                    "recovered semantic heartbeat differs from adopted bytes"
                                )
                        self.grant = adopted
                        self.next_heartbeat_ns = monotonic_ns + _HEARTBEAT_INTERVAL_NS
                        if interrupted:
                            raise KeyboardInterrupt
                        return
                    if (
                        retry == 0
                        and value == before
                        and not pending_present
                        and self.monotonic_clock()
                        < cast(int, before["liveness_deadline_monotonic_ns"])
                    ):
                        if interrupted:
                            raise KeyboardInterrupt
                        continue
                    if (
                        value["owner"] != before["owner"]
                        or value["fence_token"] != before["fence_token"]
                        or self.monotonic_clock()
                        >= cast(int, value["liveness_deadline_monotonic_ns"])
                    ):
                        raise StaleLease("semantic lease was replaced or expired")
                elif not pending_present:
                    raise StaleLease("semantic lease disappeared during heartbeat")
                raise CommitUnknown("semantic lease heartbeat outcome is uncertain")

    def _checkpoint(self, checkpoint: str) -> SemanticClaim:
        grant = cast(LeaseGrant, self.grant)
        prior = cast(SemanticClaim, self.claim)
        expected_revision = cast(int, self.expected_queue_revision)
        expected_watermark = cast(int, self.expected_desired_watermark)
        liveness_deadline = cast(
            int,
            grant.lease.to_dict()["liveness_deadline_monotonic_ns"],
        )
        boundary = min(self.deadline_ns, liveness_deadline)
        for retry in range(2):
            try:
                updated = self.runtime.semantic_queue.checkpoint(
                    grant,
                    prior,
                    checkpoint=checkpoint,
                    monotonic_ns=self._ensure_work_time(),
                    expected_registry_revision=grant.registry_revision,
                    expected_queue_revision=expected_revision,
                    expected_desired_watermark=expected_watermark,
                    deadline_ns=boundary,
                )
                if (
                    updated.checkpoint != checkpoint
                    or replace(updated, checkpoint=prior.checkpoint) != prior
                ):
                    raise SemanticQueueCorrupt(
                        "semantic checkpoint return differs from the live claim"
                    )
                self.expected_queue_revision = expected_revision + 1
                self.claim = updated
                return updated
            except (CommitUnknown, InjectedFault, KeyboardInterrupt) as fault:
                interrupted = _caused_by_keyboard_interrupt(fault)
                try:
                    observed, pending_present, snapshot = self._read_uncertain_current_claim(
                        deadline_ns=boundary
                    )
                except (Exception, KeyboardInterrupt) as exc:
                    raise CommitUnknown("semantic checkpoint cannot be reconciled") from exc
                if (
                    observed.checkpoint == checkpoint
                    and snapshot.revision == expected_revision + 1
                    and snapshot.desired_watermark == expected_watermark
                ):
                    self.claim = observed
                    if pending_present:
                        try:
                            recovered = self.runtime.semantic_queue.recover_uncertain_snapshot(
                                grant,
                                monotonic_ns=self._ensure_work_time(),
                                expected_registry_revision=grant.registry_revision,
                                expected_queue_revision=snapshot.revision,
                                expected_desired_watermark=expected_watermark,
                                deadline_ns=boundary,
                            )
                        except (Exception, KeyboardInterrupt) as exc:
                            raise CommitUnknown(
                                "semantic checkpoint recovery is ambiguous"
                            ) from exc
                        recovered_claims = [
                            item.claim
                            for item in recovered.items
                            if item.status == "claimed"
                            and item.claim is not None
                            and item.claim.claim_id == observed.claim_id
                        ]
                        if recovered_claims != [observed]:
                            raise CommitUnknown(
                                "recovered semantic checkpoint differs from adopted bytes"
                            )
                    self.expected_queue_revision = snapshot.revision
                    if interrupted:
                        raise KeyboardInterrupt
                    return observed
                if (
                    retry == 0
                    and observed == prior
                    and snapshot.revision == expected_revision
                    and snapshot.desired_watermark == expected_watermark
                    and not pending_present
                    and self.monotonic_clock() < boundary
                ):
                    if interrupted:
                        raise KeyboardInterrupt
                    continue
                raise CommitUnknown("semantic checkpoint outcome is uncertain")
        raise CommitUnknown("semantic checkpoint outcome is uncertain")

    def _stage_binding(self, binding: ResultBinding) -> Path:
        preflight = cast(_Preflight, self.preflight)
        relative = (
            Path("workspaces")
            / preflight.repo_uuid
            / "semantic-staging"
            / self.begin_sha256
            / "result.json"
        )

        def read() -> bytes | None:
            self._ensure_work_time()
            return self.runtime.semantic_queue.state.read_optional_existing_bytes(
                relative,
                max_bytes=COMPLETE_MAX_BYTES,
                deadline_ns=self.deadline_ns,
            )

        try:
            existing = read()
            self._ensure_work_time()
        except _FrameDeadline, LockTimeout:
            raise
        except Exception as exc:
            raise CommitUnknown("semantic result binding cannot be inspected") from exc
        if existing is not None:
            if existing == binding.canonical:
                return self.runtime.semantic_queue.state.path(relative)
            self._read_current_claim(deadline_ns=self.deadline_ns)
            raise SemanticResultBindingConflict("semantic result binding bytes conflict")
        for retry in range(2):
            try:
                self._ensure_work_time()
                installed = self.runtime.semantic_queue.state.install_once_bytes(
                    relative,
                    binding.canonical,
                    label="semantic_result_binding",
                    deadline_ns=self.deadline_ns,
                )
                self._ensure_work_time()
                return installed
            except CommitUnknown, InjectedFault:
                try:
                    observed = read()
                except Exception as exc:
                    raise CommitUnknown("semantic result installation is ambiguous") from exc
                if observed == binding.canonical:
                    return self.runtime.semantic_queue.state.path(relative)
                if observed is None and retry == 0 and self.monotonic_clock() < self.deadline_ns:
                    continue
                if observed is not None:
                    self._read_current_claim(deadline_ns=self.deadline_ns)
                    raise SemanticResultBindingConflict("semantic result binding bytes conflict")
                raise CommitUnknown("semantic result installation outcome is uncertain")
            except StateCorrupt as exc:
                try:
                    observed = read()
                except Exception as read_exc:
                    raise CommitUnknown("semantic result installation is ambiguous") from read_exc
                if observed == binding.canonical:
                    return self.runtime.semantic_queue.state.path(relative)
                if observed is not None:
                    self._read_current_claim(deadline_ns=self.deadline_ns)
                    raise SemanticResultBindingConflict(
                        "semantic result binding bytes conflict"
                    ) from exc
                raise
        raise CommitUnknown("semantic result installation outcome is uncertain")

    def _reopen_binding(self, path: Path, binding: ResultBinding) -> ResultBinding:
        relative = path.relative_to(self.runtime.semantic_queue.state.root)
        try:
            self._ensure_work_time()
            raw = self.runtime.semantic_queue.state.read_existing_bytes(
                relative,
                max_bytes=COMPLETE_MAX_BYTES,
                deadline_ns=self.deadline_ns,
            )
        except _FrameDeadline, LockTimeout:
            raise
        except Exception as exc:
            raise CommitUnknown("semantic result binding reopen is ambiguous") from exc
        if raw != binding.canonical or sha256(raw) != binding.sha256:
            raise CommitUnknown("semantic result binding changed after installation")
        return parse_result_binding(raw, progress=self._ensure_work_time)

    def _fail_current(
        self,
        error_code: str,
        retryable: bool,
        *,
        emit_terminal: bool,
    ) -> int:
        grant = cast(LeaseGrant, self.grant)
        claim = cast(SemanticClaim, self.claim)
        expected_revision = cast(int, self.expected_queue_revision)
        expected_watermark = cast(int, self.expected_desired_watermark)
        liveness_deadline = cast(
            int,
            grant.lease.to_dict()["liveness_deadline_monotonic_ns"],
        )
        monotonic_ns = self.monotonic_clock()
        work_bounded = error_code != "host_agent_timeout"
        if work_bounded and monotonic_ns >= self.deadline_ns:
            error_code = "host_agent_timeout"
            retryable = True
            work_bounded = False
        mutation_deadline = (
            min(self.deadline_ns, liveness_deadline) if work_bounded else liveness_deadline
        )

        try:
            try:
                snapshot = self.runtime.semantic_queue.fail(
                    grant,
                    claim,
                    error_code=error_code,
                    retryable=retryable,
                    monotonic_ns=monotonic_ns,
                    expected_registry_revision=grant.registry_revision,
                    expected_queue_revision=expected_revision,
                    expected_desired_watermark=expected_watermark,
                    deadline_ns=mutation_deadline,
                )
            except StaleSemanticClaim, StaleLease, LeaseExpired, SemanticQueueConflict:
                self._release(success_required=False)
                if not emit_terminal:
                    return 20
                return self._withheld("semantic_authority_stale")
            except BaseException:
                self._release(success_required=False)
                if not emit_terminal:
                    return 20
                return self._commit_unknown()
            self.claim = None
            matching = [
                item
                for item in snapshot.items
                if item.work.coalescing_key == claim.work.coalescing_key
                and item.work == claim.work
            ]
            if (
                snapshot.revision != expected_revision + 1
                or snapshot.desired_watermark != expected_watermark
                or len(matching) != 1
                or matching[0].status not in {"pending", "dead_letter"}
                or matching[0].last_error != error_code
                or matching[0].claim is not None
            ):
                self._release(success_required=False)
                if not emit_terminal:
                    return 20
                return self._commit_unknown()
            self.expected_queue_revision = snapshot.revision
            status = matching[0].status
            observed_late = work_bounded and self.monotonic_clock() >= self.deadline_ns
            self._release(success_required=False)
            if not emit_terminal:
                return 20
            if observed_late:
                return self._commit_unknown()
            outcome = "retry_scheduled" if status == "pending" else "dead_lettered"
            exit_code = 10 if outcome == "retry_scheduled" else 20
            action = (
                _FAILURE_ACTIONS[(outcome, error_code)]
                if outcome == "retry_scheduled"
                else "inspect_semantic_queue"
            )
            return self._emit_route(
                outcome=outcome,
                reason_code=error_code,
                action_code=action,
                exit_code=exit_code,
            )
        except KeyboardInterrupt:
            self._release(success_required=False)
            if not emit_terminal:
                return 20
            return self._commit_unknown()

    def _fail_accepted_request(self, error_code: str, retryable: bool) -> int:
        for _attempt in range(2):
            try:
                return self._fail_current(
                    error_code,
                    retryable,
                    emit_terminal=True,
                )
            except KeyboardInterrupt:
                continue
        self._release(success_required=False)
        return self._commit_unknown()

    def _output_failure(self) -> int:
        code = (
            "host_agent_timeout"
            if self.monotonic_clock() >= self.deadline_ns
            else "host_agent_interrupted"
        )
        return self._fail_current(code, True, emit_terminal=False)

    def _work_result(self) -> dict[str, object]:
        preflight = cast(_Preflight, self.preflight)
        claim = cast(SemanticClaim, self.claim)
        work_bytes = canonical_protocol_bytes(claim.work.to_dict())
        self.work_sha256 = sha256(work_bytes)
        return {
            **_result_common("work"),
            "attempt": claim.attempt,
            "begin_request_sha256": self.begin_sha256,
            "claim_id": claim.claim_id,
            "repo_uuid": preflight.repo_uuid,
            "work": claim.work.to_dict(),
            "work_sha256": self.work_sha256,
        }

    def _idle_result(self, snapshot: SemanticQueueSnapshot) -> dict[str, object]:
        return {
            **_result_common("terminal"),
            "begin_request_sha256": self.begin_sha256,
            "completed_watermark": snapshot.completed_watermark,
            "desired_watermark": snapshot.desired_watermark,
            "exit_code": 0,
            "outcome": "idle",
            "queue_revision": snapshot.revision,
            "repo_uuid": cast(_Preflight, self.preflight).repo_uuid,
        }

    def _completed_result(
        self,
        *,
        claim: SemanticClaim,
        payload: ValidatedPayload,
        binding: ResultBinding,
        snapshot: SemanticQueueSnapshot,
    ) -> dict[str, object]:
        return {
            **_result_common("terminal"),
            "attempt": claim.attempt,
            "begin_request_sha256": self.begin_sha256,
            "claim_id": claim.claim_id,
            "completed_watermark": snapshot.completed_watermark,
            "exit_code": 0,
            "outcome": "completed",
            "payload_bytes": payload.bytes_count,
            "payload_kind": payload.kind,
            "payload_sha256": payload.sha256,
            "queue_revision": snapshot.revision,
            "repo_uuid": cast(_Preflight, self.preflight).repo_uuid,
            "result_binding_bytes": binding.bytes_count,
            "result_binding_sha256": binding.sha256,
            "work_sha256": cast(str, self.work_sha256),
        }

    def _process_complete(self, request: SemanticWorkerRequest) -> int:
        claim = cast(SemanticClaim, self.claim)
        self._ensure_work_time()
        payload = validate_completion_payload(
            request.value["payload"],
            claim.work,
            progress=self._ensure_work_time,
        )
        self._ensure_work_time()
        _source_observation(
            cast(_Preflight, self.preflight).source_root,
            claim.work,
            deadline_ns=self.deadline_ns,
            monotonic_clock=self.monotonic_clock,
            progress=self._ensure_work_time,
        )
        self._read_current_claim(deadline_ns=self.deadline_ns)
        binding = build_result_binding(
            begin_request_sha256=self.begin_sha256,
            repo_uuid=cast(_Preflight, self.preflight).repo_uuid,
            claim=claim,
            work_sha256=cast(str, self.work_sha256),
            payload=payload,
            progress=self._ensure_work_time,
        )
        self._ensure_work_time()
        staged_path = self._stage_binding(binding)
        reopened = self._reopen_binding(staged_path, binding)
        if reopened.canonical != binding.canonical:
            raise CommitUnknown("semantic result binding changed after reopen")
        self._checkpoint("result:" + binding.sha256)
        reopened = self._reopen_binding(staged_path, binding)
        if reopened.canonical != binding.canonical:
            raise CommitUnknown("semantic result binding changed after checkpoint")
        binding_value = reopened.value
        exact = (
            binding_value["begin_request_sha256"] == self.begin_sha256
            and binding_value["repo_uuid"] == cast(_Preflight, self.preflight).repo_uuid
            and binding_value["claim_id"] == claim.claim_id
            and binding_value["attempt"] == claim.attempt
            and binding_value["work"] == claim.work.to_dict()
            and binding_value["work_sha256"] == self.work_sha256
            and binding_value["payload"] == payload.value
            and binding_value["payload_bytes"] == payload.bytes_count
            and binding_value["payload_sha256"] == payload.sha256
            and binding_value["active_source_revision"] == claim.active_source_revision
            and binding_value["operation_epoch"] == claim.operation_epoch
            and binding_value["migration_epoch"] == claim.migration_epoch
        )
        if not exact:
            raise SemanticResultInvalid("semantic result binding differs from the live session")
        current_claim = self._read_current_claim(deadline_ns=self.deadline_ns)
        if (
            current_claim.claim_id != claim.claim_id
            or current_claim.checkpoint != "result:" + binding.sha256
        ):
            raise CommitUnknown("mandatory result checkpoint is not current")
        _source_observation(
            cast(_Preflight, self.preflight).source_root,
            claim.work,
            deadline_ns=self.deadline_ns,
            monotonic_clock=self.monotonic_clock,
            progress=self._ensure_work_time,
        )
        self._ensure_work_time()
        grant = cast(LeaseGrant, self.grant)
        expected_revision = cast(int, self.expected_queue_revision)
        expected_watermark = cast(int, self.expected_desired_watermark)
        completion_deadline = min(
            self.deadline_ns,
            cast(
                int,
                grant.lease.to_dict()["liveness_deadline_monotonic_ns"],
            ),
        )
        try:
            snapshot = self.runtime.semantic_queue.complete(
                grant,
                current_claim,
                monotonic_ns=self.monotonic_clock(),
                expected_registry_revision=grant.registry_revision,
                expected_queue_revision=expected_revision,
                expected_desired_watermark=expected_watermark,
                deadline_ns=completion_deadline,
            )
        except LockTimeout:
            raise
        except StaleSemanticClaim, StaleLease, LeaseExpired, SemanticQueueConflict:
            self._release(success_required=False)
            return self._withheld("semantic_authority_stale")
        except BaseException:
            self._release(success_required=False)
            return self._commit_unknown()
        matching = [
            item
            for item in snapshot.items
            if item.work.coalescing_key == current_claim.work.coalescing_key
            and item.work == current_claim.work
        ]
        if (
            snapshot.revision != expected_revision + 1
            or snapshot.desired_watermark != expected_watermark
            or len(matching) != 1
            or matching[0].status != "completed"
            or matching[0].last_error is not None
            or matching[0].claim is not None
        ):
            self.claim = None
            self._release(success_required=False)
            return self._commit_unknown()
        self.expected_queue_revision = snapshot.revision
        if self.monotonic_clock() >= self.deadline_ns:
            self.claim = None
            self._release(success_required=False)
            return self._commit_unknown()
        self.claim = None
        if not self._release(success_required=True):
            return self._commit_unknown()
        result = self._completed_result(
            claim=claim,
            payload=payload,
            binding=binding,
            snapshot=snapshot,
        )
        try:
            self._emit(result)
        except SemanticOutputDeliveryError:
            return 20
        return 0

    def _read_post_claim_frame(self) -> SemanticWorkerRequest:
        while True:
            heartbeat_deadline = cast(int, self.next_heartbeat_ns)
            boundary = min(self.deadline_ns, heartbeat_deadline)
            try:
                raw = self.reader.read_frame(
                    COMPLETE_MAX_BYTES,
                    deadline_ns=boundary,
                    action_limits={
                        b'{"action":"begin",': BEGIN_MAX_BYTES,
                        b'{"action":"checkpoint",': SMALL_FRAME_MAX_BYTES,
                        b'{"action":"complete",': COMPLETE_MAX_BYTES,
                        b'{"action":"fail",': SMALL_FRAME_MAX_BYTES,
                    },
                )
                return parse_request_frame(raw, progress=self._ensure_work_time)
            except _FrameDeadline:
                if self.monotonic_clock() >= self.deadline_ns:
                    raise
                self._heartbeat()

    def _protocol(self) -> int:
        checkpoint_count = 0
        while True:
            try:
                request = self._read_post_claim_frame()
                if request.action == "begin":
                    raise SemanticResultInvalid("a second begin request is forbidden")
                if (
                    request.value.get("begin_request_sha256") != self.begin_sha256
                    or request.value.get("claim_id") != cast(SemanticClaim, self.claim).claim_id
                ):
                    raise SemanticResultInvalid("post-claim request binding differs")
                if request.action == "checkpoint":
                    if checkpoint_count >= 8:
                        raise SemanticResultInvalid("at most eight checkpoints are accepted")
                    checkpoint_count += 1
                    self._checkpoint(cast(str, request.value["progress_code"]))
                    result = {
                        **_result_common("checkpointed"),
                        "begin_request_sha256": self.begin_sha256,
                        "checkpoint_sha256": sha256(request.canonical),
                        "claim_id": cast(SemanticClaim, self.claim).claim_id,
                    }
                    try:
                        self._emit(result, work_bounded=True)
                    except SemanticOutputDeliveryError:
                        return self._output_failure()
                    continue
                if request.action == "fail":
                    return self._fail_accepted_request(
                        cast(str, request.value["error_code"]),
                        cast(bool, request.value["retryable"]),
                    )
                return self._process_complete(request)
            except LockTimeout, _FrameDeadline:
                return self._fail_current("host_agent_timeout", True, emit_terminal=True)
            except _FrameEof, KeyboardInterrupt:
                return self._fail_current("host_agent_interrupted", True, emit_terminal=True)
            except SemanticResultBindingConflict:
                return self._fail_current(
                    "semantic_result_binding_conflict",
                    False,
                    emit_terminal=True,
                )
            except SemanticSourceUnavailable:
                return self._fail_current("source_unavailable", True, emit_terminal=True)
            except SemanticSourceChanged:
                return self._fail_current(
                    "source_content_changed",
                    False,
                    emit_terminal=True,
                )
            except SemanticWorkerRequestInvalid, SemanticResultInvalid, ContractError, ValueError:
                return self._fail_current("semantic_result_invalid", True, emit_terminal=True)
            except StaleSemanticClaim, StaleLease, LeaseExpired, SemanticQueueConflict:
                self._release(success_required=False)
                return self._withheld("semantic_authority_stale")
            except CommitUnknown, InjectedFault:
                self._release(success_required=False)
                return self._commit_unknown()
            except SemanticQueueCorrupt, StateRecoveryRequired:
                self._release(success_required=False)
                return self._invalid("semantic_queue_invalid")
            except StateCorrupt, LeaseRecoveryRequired:
                self._release(success_required=False)
                return self._invalid("workspace_state_invalid")
            except OSError, IdentityError:
                return self._fail_current("source_unavailable", True, emit_terminal=True)

    def run(self) -> int:
        try:
            self.preflight = _preflight(
                self.runtime,
                self.begin_value,
                deadline_ns=self.deadline_ns,
                monotonic_clock=self.monotonic_clock,
            )
        except LockTimeout, SourceDiscoveryTimeout, _FrameDeadline:
            return self._withheld("semantic_worker_preclaim_timeout")
        except KeyboardInterrupt:
            if self.grant is not None and not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("semantic_worker_preclaim_interrupted")
        except LeaseBusy:
            return self._withheld("semantic_claim_contended")
        except RevisionConflict:
            return self._withheld("semantic_authority_stale")
        except SemanticQueueCapacityExceeded:
            return self._invalid("semantic_queue_invalid")
        except SemanticQueueCorrupt:
            return self._invalid("semantic_queue_invalid")
        except StatePathError:
            return self._invalid("unsafe_state_path")
        except StateCorrupt, StateRecoveryRequired:
            return self._invalid("registry_invalid")
        except LeaseRecoveryRequired:
            return self._invalid("workspace_state_invalid")
        except SourceDiscoveryError as exc:
            reason = (
                "workspace_config_invalid"
                if "invalid workspace config" in str(exc)
                else "workspace_config_unavailable"
            )
            return (
                self._invalid(reason)
                if reason == "workspace_config_invalid"
                else self._withheld(reason)
            )
        except IdentityError, OSError:
            return self._withheld("workspace_config_unavailable")
        try:
            self.grant = self._acquire()
        except _TerminalRoute as route:
            return self._emit_route(
                outcome=route.outcome,
                reason_code=route.reason_code,
                action_code=route.action_code,
                exit_code=route.exit_code,
            )
        except LockTimeout, SourceDiscoveryTimeout, _FrameDeadline:
            return self._withheld("semantic_worker_preclaim_timeout")
        except KeyboardInterrupt:
            if self.grant is not None and not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("semantic_worker_preclaim_interrupted")
        except LeaseBusy:
            return self._withheld("semantic_claim_contended")
        except StagedBuildLeaseRecoveryRequired:
            return self._withheld("staged_build_recovery_required")
        except RevisionConflict, StaleLease, LeaseExpired:
            return self._withheld("semantic_authority_stale")
        except CommitUnknown, InjectedFault:
            return self._commit_unknown()
        except StatePathError:
            return self._invalid("unsafe_state_path")
        except StateCorrupt, StateRecoveryRequired, LeaseRecoveryRequired:
            return self._invalid("workspace_state_invalid")
        except LeaseError:
            return self._invalid("workspace_state_invalid")
        try:
            self.claim = self._claim_work()
        except SemanticCheckpointCapacityUnavailable, SemanticQueueCapacityExceeded:
            if not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("semantic_checkpoint_capacity_unavailable")
        except SemanticCapabilityUnavailable as exc:
            if not self._release(success_required=True):
                return self._commit_unknown()
            reason = str(exc)
            if "workspace_config_invalid" in reason:
                return self._invalid("workspace_config_invalid")
            if "workspace_config_unavailable" in reason:
                return self._withheld("workspace_config_unavailable")
            return self._withheld("semantic_capability_unavailable")
        except RevisionConflict, SemanticQueueConflict, StaleSemanticClaim, StaleLease:
            if not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("semantic_authority_stale")
        except StagedBuildLeaseRecoveryRequired:
            if not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("staged_build_recovery_required")
        except LockTimeout, SourceDiscoveryTimeout, _FrameDeadline:
            if not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("semantic_worker_preclaim_timeout")
        except KeyboardInterrupt:
            if self.claim is not None:
                return self._fail_current(
                    "host_agent_interrupted",
                    True,
                    emit_terminal=True,
                )
            if not self._release(success_required=True):
                return self._commit_unknown()
            return self._withheld("semantic_worker_preclaim_interrupted")
        except CommitUnknown, InjectedFault:
            self._release(success_required=False)
            return self._commit_unknown()
        except SemanticQueueCorrupt:
            self._release(success_required=False)
            return self._invalid("semantic_queue_invalid")
        except StateCorrupt, StateRecoveryRequired, LeaseRecoveryRequired:
            self._release(success_required=False)
            return self._invalid("workspace_state_invalid")
        if self.claim is None:
            try:
                snapshot = self._read_current_queue_snapshot(
                    deadline_ns=self.deadline_ns,
                )
            except KeyboardInterrupt:
                if not self._release(success_required=True):
                    return self._commit_unknown()
                return self._withheld("semantic_worker_preclaim_interrupted")
            except LockTimeout, SourceDiscoveryTimeout, _FrameDeadline:
                if not self._release(success_required=True):
                    return self._commit_unknown()
                return self._withheld("semantic_worker_preclaim_timeout")
            except RevisionConflict, SemanticQueueConflict, StaleLease, LeaseExpired:
                if not self._release(success_required=True):
                    return self._commit_unknown()
                return self._withheld("semantic_authority_stale")
            except StagedBuildLeaseRecoveryRequired:
                if not self._release(success_required=True):
                    return self._commit_unknown()
                return self._withheld("staged_build_recovery_required")
            except Exception:
                self._release(success_required=False)
                return self._commit_unknown()
            if _eligible_items(snapshot):
                if not self._release(success_required=True):
                    return self._commit_unknown()
                return self._withheld("semantic_authority_stale")
            if not self._release(success_required=True):
                return self._commit_unknown()
            try:
                self._emit(self._idle_result(snapshot))
            except SemanticOutputDeliveryError:
                return 20
            return 0
        self.next_heartbeat_ns = self.monotonic_clock() + _HEARTBEAT_INTERVAL_NS
        try:
            _source_observation(
                cast(_Preflight, self.preflight).source_root,
                self.claim.work,
                deadline_ns=self.deadline_ns,
                monotonic_clock=self.monotonic_clock,
                progress=self._ensure_work_time,
            )
            self._read_current_claim(deadline_ns=self.deadline_ns)
        except LockTimeout, _FrameDeadline:
            return self._fail_current("host_agent_timeout", True, emit_terminal=True)
        except KeyboardInterrupt:
            return self._fail_current("host_agent_interrupted", True, emit_terminal=True)
        except SemanticSourceUnavailable:
            return self._fail_current("source_unavailable", True, emit_terminal=True)
        except SemanticSourceChanged:
            return self._fail_current("source_content_changed", False, emit_terminal=True)
        except StaleSemanticClaim, StaleLease, LeaseExpired, SemanticQueueConflict:
            self._release(success_required=False)
            return self._withheld("semantic_authority_stale")
        except CommitUnknown, InjectedFault:
            self._release(success_required=False)
            return self._commit_unknown()
        try:
            work_result = self._work_result()
            canonical_result_bytes(work_result)
        except KeyboardInterrupt:
            return self._fail_current(
                "host_agent_interrupted",
                True,
                emit_terminal=True,
            )
        except SemanticResultInvalid:
            return self._fail_current(
                "semantic_work_unsupported",
                False,
                emit_terminal=True,
            )
        try:
            self._emit(work_result, work_bounded=True)
        except SemanticOutputDeliveryError:
            return self._output_failure()
        return self._protocol()


def run_semantic_worker(
    runtime: object,
    *,
    stdin: BinaryIO | TextIO | None = None,
    stdout: BinaryIO | TextIO | None = None,
    monotonic_clock: Callable[[], int] | None = None,
    wall_clock: Callable[[], datetime] | None = None,
) -> int:
    """Run one same-process semantic-worker session."""

    runtime_value = cast(WorkspaceRuntime, runtime)
    input_stream = cast(BinaryIO, sys.stdin.buffer) if stdin is None else stdin
    output_stream = cast(BinaryIO, sys.stdout.buffer) if stdout is None else stdout
    clock = time.monotonic_ns if monotonic_clock is None else monotonic_clock
    now_utc = (lambda: datetime.now(timezone.utc)) if wall_clock is None else wall_clock
    reader = _FrameReader(input_stream, monotonic_clock=clock)
    try:
        raw = reader.read_frame(BEGIN_MAX_BYTES, deadline_ns=None)
        begin = parse_request_frame(raw)
        if begin.action != "begin":
            raise SemanticWorkerRequestInvalid("the first request must be begin")
    except SemanticWorkerRequestInvalid, _FrameEof, KeyboardInterrupt, UnicodeError:
        return emit_pre_begin_failure(
            output_stream,
            reason_code="semantic_worker_request_invalid",
            action_code="none",
            monotonic_clock=clock,
        )
    try:
        accepted_at = clock()
    except KeyboardInterrupt:
        value = _failure_result(
            outcome="withheld",
            reason_code="semantic_worker_preclaim_interrupted",
            action_code="retry_status",
            exit_code=10,
            begin_request_sha256=sha256(begin.canonical),
        )
        try:
            _write_result_frame(output_stream, value, monotonic_clock=clock)
        except (KeyboardInterrupt, SemanticOutputDeliveryError):
            return 20
        return 10
    timeout_ms = cast(int, begin.value["timeout_ms"])
    deadline_ns = accepted_at + (timeout_ms * 1_000_000)
    session = _WorkerSession(
        runtime=runtime_value,
        reader=reader,
        stdout=output_stream,
        begin=begin,
        monotonic_clock=clock,
        wall_clock=now_utc,
        deadline_ns=deadline_ns,
    )
    try:
        return session.run()
    except _ReleaseInvalid as exc:
        return session._invalid(exc.reason_code)


__all__ = [
    "BEGIN_MAX_BYTES",
    "COMPLETE_MAX_BYTES",
    "RESULT_MAX_BYTES",
    "ResultBinding",
    "SemanticOutputDeliveryError",
    "SemanticResultBindingConflict",
    "SemanticResultInvalid",
    "SemanticWorkerError",
    "SemanticWorkerRequest",
    "SemanticWorkerRequestInvalid",
    "ValidatedPayload",
    "build_result_binding",
    "canonical_result_bytes",
    "canonical_protocol_bytes",
    "emit_pre_begin_failure",
    "load_request_schema",
    "load_result_schema",
    "parse_request_frame",
    "parse_result_frame",
    "parse_result_binding",
    "run_semantic_worker",
    "sha256",
    "validate_bound_payload",
    "validate_completion_payload",
]
