"""Versioned, deterministic, read-only workspace status inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, cast
import uuid

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import (
    WorkspaceRuntime,
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
)
from graphify.workspace.contracts import (
    ADAPTER_CONTRACT_VERSION,
    CANDIDATE_DISTRIBUTION_VERSION,
    CLI_CONTRACT_VERSION,
    ENGINE_BASELINE,
    STATE_SCHEMA_VERSION,
    ContractError,
    Registry,
    canonical_json_bytes,
    canonical_sha256,
)
from graphify.workspace.generations import (
    GenerationError,
    StagedBuildReadRecoveryRequired,
)
from graphify.workspace.gc import GcError
from graphify.workspace.journal import JournalError, JournalSnapshot
from graphify.workspace.leases import LeaseError
from graphify.workspace.persistence import (
    LockTimeout,
    StateCorrupt,
    StatePathError,
    StateRecordMissing,
    UnsupportedRuntime,
    WorkspaceRuntimeError,
    require_before_deadline,
)
from graphify.workspace.pointers import PointerError, PointerRecoveryRequired
from graphify.workspace.semantic_queue import SemanticQueueError, SemanticQueueSnapshot


STATUS_CONTRACT = "graphify.workspace.status"
STATUS_SCHEMA_VERSION = 2
EXIT_READY = 0
EXIT_DEGRADED = 10
EXIT_INVALID = 20
EXIT_USAGE = 64
_DEFAULT_INSPECTION_TIMEOUT_NS = 5_000_000_000
_CHECK_STATES = frozenset({"ready", "degraded", "invalid", "not_evaluated"})
_REPORT_STATES = frozenset({"ready", "degraded", "invalid"})
REASON_CODES = frozenset(
    {
        "compatibility_manifest_missing",
        "freshness_not_observed",
        "freshness_timeout",
        "freshness_unsupported",
        "generation_lock_contended",
        "generation_or_pointer_invalid",
        "generation_pending",
        "inspection_deadline_exceeded",
        "journal_invalid",
        "no_current_generation",
        "no_registered_workspaces",
        "not_evaluated_p5b1",
        "not_recorded_v1",
        "pointer_invalid",
        "pointer_recovery_required",
        "ready",
        "registry_invalid",
        "registry_lock_contended",
        "registry_lock_invalid",
        "registry_lock_missing",
        "resource_accounting_deferred_p5c",
        "runtime_authority_invalid",
        "runtime_authority_unsupported",
        "semantic_queue_dead_letter",
        "semantic_queue_invalid",
        "semantic_queue_pending",
        "service_deferred_p5c",
        "source_drift",
        "source_unavailable",
        "source_unstable",
        "state_root_missing",
        "staged_build_invalid",
        "staged_build_recovery_required",
        "status_snapshot_changed",
        "unsafe_state_path",
        "unsupported_compatibility",
        "unsupported_runtime",
        "workspace_lock_contended",
        "workspace_lock_invalid",
        "workspace_not_inspected",
        "workspace_record_invalid",
        "workspace_record_missing",
        "workspace_state_invalid",
    }
)
ACTION_CODES = frozenset(
    {
        "configure_safe_state_root",
        "drain_semantic_queue",
        "inspect_semantic_queue",
        "inspect_source",
        "inspect_workspace_state",
        "install_candidate_authority",
        "install_supported_candidate",
        "none",
        "register_workspace",
        "restore_source",
        "retry_status",
        "resume_exact_workspace_sync",
        "run_workspace_gc_reconcile",
        "run_workspace_repair",
        "run_workspace_sync",
        "use_supported_runtime",
        "verify_freshness",
    }
)


def _validate_codes(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            is_reason_code = isinstance(key, str) and (
                key == "reason_code" or key.endswith("_reason_code")
            )
            is_action_code = isinstance(key, str) and (
                key == "action_code" or key.endswith("_action_code")
            )
            if is_reason_code and (not isinstance(item, str) or item not in REASON_CODES):
                raise ValueError(f"unsupported workspace status reason code: {item!r}")
            if is_action_code and (not isinstance(item, str) or item not in ACTION_CODES):
                raise ValueError(f"unsupported workspace status action code: {item!r}")
            if key == "age_reason_code" and item != "not_recorded_v1":
                raise ValueError(f"unsupported workspace status age reason code: {item!r}")
            _validate_codes(item)
    elif isinstance(value, list):
        for item in value:
            _validate_codes(item)


def _check(
    component: str,
    state: str,
    reason_code: str,
    action_code: str,
) -> dict[str, str]:
    if state not in _CHECK_STATES:
        raise ValueError(f"unsupported workspace check state: {state}")
    if reason_code not in REASON_CODES:
        raise ValueError(f"unsupported workspace status reason code: {reason_code!r}")
    if action_code not in ACTION_CODES:
        raise ValueError(f"unsupported workspace status action code: {action_code!r}")
    return {
        "component": component,
        "state": state,
        "reason_code": reason_code,
        "action_code": action_code,
    }


def _runtime_summary(compatibility_sha256: str | None) -> dict[str, object]:
    return {
        "distribution_version": CANDIDATE_DISTRIBUTION_VERSION,
        "engine_baseline": ENGINE_BASELINE,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "compatibility_sha256": compatibility_sha256,
    }


def _schema_equal(left: object, right: object) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _schema_accepts(
    value: object,
    schema: Mapping[str, object],
    root: Mapping[str, object],
    path: str,
) -> bool:
    try:
        _validate_schema_node(value, schema, root, path)
    except ValueError:
        return False
    return True


def _schema_type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _schema_error(path: str, detail: str) -> ValueError:
    return ValueError(f"workspace status schema violation at {path}: {detail}")


_SUPPORTED_STATUS_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "const",
        "contains",
        "description",
        "else",
        "enum",
        "format",
        "if",
        "items",
        "minContains",
        "minItems",
        "minLength",
        "minimum",
        "not",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "then",
        "title",
        "type",
    }
)


def _validate_schema_keywords(schema: Mapping[str, object], path: str) -> None:
    unsupported = sorted(set(schema) - _SUPPORTED_STATUS_SCHEMA_KEYWORDS)
    if unsupported:
        raise _schema_error(path, f"unsupported schema keyword(s): {', '.join(unsupported)}")

    for keyword in ("$defs", "properties"):
        children = schema.get(keyword)
        if isinstance(children, Mapping):
            for name, child in children.items():
                if isinstance(child, Mapping):
                    _validate_schema_keywords(child, f"{path}.{keyword}.{name}")

    for keyword in ("contains", "else", "if", "items", "not", "then"):
        child = schema.get(keyword)
        if isinstance(child, Mapping):
            _validate_schema_keywords(child, f"{path}.{keyword}")

    for keyword in ("allOf", "oneOf"):
        children = schema.get(keyword)
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    _validate_schema_keywords(child, f"{path}.{keyword}[{index}]")


def _resolve_schema_reference(
    reference: str,
    root: Mapping[str, object],
    path: str,
) -> Mapping[str, object]:
    if not reference.startswith("#/"):
        raise _schema_error(path, f"unsupported schema reference {reference!r}")
    resolved: object = root
    for part in reference[2:].split("/"):
        if not isinstance(resolved, Mapping) or part not in resolved:
            raise _schema_error(path, f"unresolved schema reference {reference!r}")
        resolved = resolved[part]
    if not isinstance(resolved, Mapping):
        raise _schema_error(path, f"invalid schema reference {reference!r}")
    return cast(Mapping[str, object], resolved)


def _validate_schema_node(
    value: object,
    schema: Mapping[str, object],
    root: Mapping[str, object],
    path: str,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        _validate_schema_node(value, _resolve_schema_reference(reference, root, path), root, path)

    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list):
        matches = sum(
            _schema_accepts(value, cast(Mapping[str, object], option), root, path)
            for option in alternatives
            if isinstance(option, Mapping)
        )
        if matches != 1:
            raise _schema_error(path, "value must match exactly one schema alternative")

    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        allowed_types = (expected_types,)
    elif isinstance(expected_types, list) and all(isinstance(item, str) for item in expected_types):
        allowed_types = tuple(cast(list[str], expected_types))
    else:
        allowed_types = ()
    if allowed_types and not any(
        _schema_type_matches(value, expected) for expected in allowed_types
    ):
        raise _schema_error(path, f"expected type {' or '.join(allowed_types)}")

    if "const" in schema and not _schema_equal(value, schema["const"]):
        raise _schema_error(path, "value does not match the required constant")
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_schema_equal(value, item) for item in enum):
        raise _schema_error(path, "value is outside the versioned enum")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise _schema_error(path, f"string must contain at least {minimum_length} characters")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise _schema_error(path, "string does not match the required pattern")
        if schema.get("format") == "uuid":
            try:
                parsed_uuid = uuid.UUID(value)
            except (AttributeError, ValueError) as exc:
                raise _schema_error(path, "string is not a UUID") from exc
            if str(parsed_uuid) != value.lower():
                raise _schema_error(path, "string is not a canonical UUID")

    minimum = schema.get("minimum")
    if (
        isinstance(minimum, int)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value < minimum
    ):
        raise _schema_error(path, f"integer must be at least {minimum}")

    if isinstance(value, Mapping):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                raise _schema_error(path, f"missing required fields: {', '.join(missing)}")
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                unexpected = sorted(str(key) for key in value if key not in properties)
                if unexpected:
                    raise _schema_error(path, f"unexpected fields: {', '.join(unexpected)}")
            for key, child_schema in properties.items():
                if key in value and isinstance(key, str) and isinstance(child_schema, Mapping):
                    _validate_schema_node(
                        value[key],
                        cast(Mapping[str, object], child_schema),
                        root,
                        f"{path}.{key}",
                    )

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise _schema_error(path, f"array must contain at least {minimum_items} items")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_schema_node(
                    item,
                    cast(Mapping[str, object], items),
                    root,
                    f"{path}[{index}]",
                )
        contains = schema.get("contains")
        if isinstance(contains, Mapping):
            matches = sum(
                _schema_accepts(
                    item,
                    cast(Mapping[str, object], contains),
                    root,
                    f"{path}[{index}]",
                )
                for index, item in enumerate(value)
            )
            minimum_contains = schema.get("minContains", 1)
            if isinstance(minimum_contains, int) and matches < minimum_contains:
                raise _schema_error(path, "array does not contain enough matching items")

    clauses = schema.get("allOf")
    if isinstance(clauses, list):
        for clause in clauses:
            if isinstance(clause, Mapping):
                _validate_schema_node(value, cast(Mapping[str, object], clause), root, path)

    condition = schema.get("if")
    if isinstance(condition, Mapping):
        branch_name = "then" if _schema_accepts(value, condition, root, path) else "else"
        branch = schema.get(branch_name)
        if isinstance(branch, Mapping):
            _validate_schema_node(value, cast(Mapping[str, object], branch), root, path)

    excluded = schema.get("not")
    if isinstance(excluded, Mapping) and _schema_accepts(value, excluded, root, path):
        raise _schema_error(path, "value matches an excluded schema")


def _validate_status_schema_document(value: Mapping[str, object]) -> None:
    schema = load_status_schema()
    _validate_schema_keywords(schema, "$schema")
    _validate_schema_node(value, schema, schema, "$")


@dataclass(frozen=True)
class WorkspaceStatusReport:
    """Immutable canonical representation of one CLI status document."""

    _value: Mapping[str, object]
    _canonical: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        canonical = canonical_json_bytes(dict(self._value))
        value = json.loads(canonical)
        if not isinstance(value, dict):  # pragma: no cover - construction invariant
            raise ValueError("workspace status must be a JSON object")
        if value.get("contract") != STATUS_CONTRACT:
            raise ValueError("workspace status contract is invalid")
        if value.get("schema_version") != STATUS_SCHEMA_VERSION:
            raise ValueError("workspace status schema version is invalid")
        if value.get("cli_contract_version") != CLI_CONTRACT_VERSION:
            raise ValueError("workspace status CLI contract version is invalid")
        if value.get("state") not in _REPORT_STATES:
            raise ValueError("workspace status state is invalid")
        state = cast(str, value["state"])
        expected_exit = {
            "ready": EXIT_READY,
            "degraded": EXIT_DEGRADED,
            "invalid": EXIT_INVALID,
        }[state]
        if value.get("exit_code") != expected_exit:
            raise ValueError("workspace status state and exit code are inconsistent")
        expected_safe = state == "ready"
        if value.get("safe_to_query") is not expected_safe:
            raise ValueError("workspace status state and query safety are inconsistent")
        checks = value.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("workspace status checks are invalid")
        check_states: list[str] = []
        for item in checks:
            if not isinstance(item, Mapping) or item.get("state") not in _CHECK_STATES:
                raise ValueError("workspace status check state is invalid")
            check_states.append(cast(str, item["state"]))
        workspaces = value.get("workspaces")
        if not isinstance(workspaces, list):
            raise ValueError("workspace status workspaces are invalid")
        workspace_states: list[str] = []
        for item in workspaces:
            if not isinstance(item, Mapping) or item.get("state") not in _REPORT_STATES:
                raise ValueError("workspace status workspace state is invalid")
            workspace_state = cast(str, item["state"])
            workspace_states.append(workspace_state)
            workspace_safe = item.get("safe_to_query")
            if workspace_safe is not (workspace_state == "ready"):
                raise ValueError("workspace state and query safety are inconsistent")
            if workspace_state != "ready":
                continue
            generations = item.get("generations")
            freshness = item.get("freshness")
            if not isinstance(generations, Mapping) or not isinstance(freshness, Mapping):
                raise ValueError("ready workspace evidence is incomplete")
            current = generations.get("current")
            binding = freshness.get("binding")
            pointer_revision = generations.get("pointer_revision")
            if (
                not isinstance(current, Mapping)
                or not isinstance(binding, Mapping)
                or isinstance(pointer_revision, bool)
                or not isinstance(pointer_revision, int)
                or freshness.get("state") != "observed_current"
                or freshness.get("observation_boundary") != "two_sided"
                or binding.get("pointer_revision") != pointer_revision
                or binding.get("active_source_revision") != item.get("active_source_revision")
                or binding.get("receipt_sha256") != current.get("receipt_sha256")
            ):
                raise ValueError("ready workspace lacks bound observed-current evidence")
        derived_state = (
            "invalid"
            if "invalid" in (*check_states, *workspace_states)
            else "degraded"
            if "degraded" in (*check_states, *workspace_states)
            else "ready"
        )
        if state != derived_state:
            raise ValueError("workspace status state contradicts its evidence")
        if expected_safe and not workspaces:
            raise ValueError("workspace status query safety contradicts its workspaces")
        _validate_codes(value)
        _validate_status_schema_document(value)
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_canonical", canonical)

    @property
    def canonical(self) -> bytes:
        return self._canonical

    @property
    def exit_code(self) -> int:
        return cast(int, self._value["exit_code"])

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._canonical))


def load_status_schema() -> dict[str, Any]:
    """Load the public CLI schema without changing the frozen state catalog."""

    path = (
        Path(__file__).parent
        / "schemas"
        / "cli"
        / f"v{STATUS_SCHEMA_VERSION}"
        / "status.schema.json"
    )
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace status schema must be a JSON object")
    return cast(dict[str, Any], value)


def _finalize(
    *,
    runtime: Mapping[str, object],
    workspaces: list[dict[str, object]],
    checks: list[dict[str, str]],
) -> WorkspaceStatusReport:
    ordered_checks = sorted(
        checks,
        key=lambda item: (
            item["component"],
            item["state"],
            item["reason_code"],
            item["action_code"],
        ),
    )
    invalid = [item for item in ordered_checks if item["state"] == "invalid"]
    degraded = [item for item in ordered_checks if item["state"] == "degraded"]
    if invalid:
        state = "invalid"
        exit_code = EXIT_INVALID
        primary = invalid[0]
    elif degraded:
        state = "degraded"
        exit_code = EXIT_DEGRADED
        primary = min(
            degraded,
            key=lambda item: (
                item["reason_code"] != "staged_build_recovery_required",
                item["component"],
                item["reason_code"],
                item["action_code"],
            ),
        )
    else:
        state = "ready"
        exit_code = EXIT_READY
        primary = _check("status", "ready", "ready", "none")
    safe_to_query = bool(workspaces) and all(
        bool(workspace["safe_to_query"]) for workspace in workspaces
    )
    if exit_code != EXIT_READY:
        safe_to_query = False
    body: dict[str, object] = {
        "contract": STATUS_CONTRACT,
        "schema_version": STATUS_SCHEMA_VERSION,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "state": state,
        "exit_code": exit_code,
        "safe_to_query": safe_to_query,
        "reason_code": primary["reason_code"],
        "action_code": primary["action_code"],
        "correlation_id": "",
        "runtime": dict(runtime),
        "workspaces": sorted(workspaces, key=lambda item: str(item["repo_uuid"])),
        "checks": ordered_checks,
    }
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()[:24]
    body["correlation_id"] = f"status-{digest}"
    return WorkspaceStatusReport(body)


def missing_workspace_authority_report() -> WorkspaceStatusReport:
    """Return the fail-closed result used before P5C installs candidate authority."""

    return _finalize(
        runtime=_runtime_summary(None),
        workspaces=[],
        checks=[
            _check(
                "compatibility",
                "invalid",
                "compatibility_manifest_missing",
                "install_candidate_authority",
            )
        ],
    )


def invalid_workspace_authority_report(
    *,
    reason_code: str,
    action_code: str,
) -> WorkspaceStatusReport:
    """Return a redacted fail-closed result for an unusable installed authority."""

    return _finalize(
        runtime=_runtime_summary(None),
        workspaces=[],
        checks=[
            _check(
                "runtime_authority",
                "invalid",
                reason_code,
                action_code,
            )
        ],
    )


def _generation_reference(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    reference = cast(dict[str, object], value)
    return {
        "generation_id": str(reference["generation_id"]),
        "receipt_sha256": str(reference["receipt_sha256"]),
    }


def _lease_summary(value: object) -> dict[str, object]:
    if value is None:
        return {
            "present": False,
            "operation": None,
            "fence_token": None,
            "liveness": "not_evaluated",
        }
    document = cast(Any, value).to_dict()
    return {
        "present": True,
        "operation": str(document["operation"]),
        "fence_token": int(document["fence_token"]),
        "liveness": "not_evaluated",
    }


def _queue_summary(snapshot: SemanticQueueSnapshot) -> dict[str, object]:
    pending = sum(item.status == "pending" for item in snapshot.items)
    claimed = sum(item.status == "claimed" for item in snapshot.items)
    retrying = sum(item.status == "pending" and item.failure_count > 0 for item in snapshot.items)
    dead_letter = sum(item.status == "dead_letter" for item in snapshot.items)
    return {
        "revision": snapshot.revision,
        "desired_watermark": snapshot.desired_watermark,
        "completed_watermark": snapshot.completed_watermark,
        "depth": pending + claimed,
        "pending": pending,
        "claimed": claimed,
        "retrying": retrying,
        "dead_letter": dead_letter,
        "oldest_age_seconds": None,
        "age_reason_code": "not_recorded_v1",
    }


def _queue_issue(summary: Mapping[str, object]) -> tuple[str, str] | None:
    if int(cast(int, summary["dead_letter"])) > 0:
        return "semantic_queue_dead_letter", "inspect_semantic_queue"
    if (
        int(cast(int, summary["depth"])) > 0
        or int(cast(int, summary["retrying"])) > 0
        or int(cast(int, summary["completed_watermark"]))
        < int(cast(int, summary["desired_watermark"]))
    ):
        return "semantic_queue_pending", "drain_semantic_queue"
    return None


def _journal_summary(
    snapshot: JournalSnapshot,
    *,
    deadline_ns: int | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], int]:
    latest: dict[str, Mapping[str, object]] = {}
    repair_count = 0
    last_successful_transition: str | None = None
    last_failure_classification: str | None = None
    for event in snapshot.events:
        require_before_deadline(
            deadline_ns,
            "journal summary exceeded its deadline",
        )
        value = event.to_dict()
        latest[str(value["generation_id"])] = value
        if value["transition"] == "REPAIRED":
            repair_count += 1
        if value["transition"] == "FAILED":
            last_failure_classification = "generation_failed"
        else:
            last_successful_transition = str(value["transition"])
    pending_states = frozenset({"ALLOCATED", "STAGING", "BUILT", "VALIDATING", "CERTIFIED"})
    pending: list[dict[str, object]] = []
    for generation_id, value in sorted(latest.items()):
        require_before_deadline(
            deadline_ns,
            "journal summary exceeded its deadline",
        )
        if value["transition"] in pending_states:
            pending.append(
                {
                    "generation_id": generation_id,
                    "lifecycle_state": str(value["transition"]),
                    "receipt_sha256": value["receipt_sha256"],
                }
            )
    require_before_deadline(
        deadline_ns,
        "journal summary exceeded its deadline",
    )
    return (
        {
            "sequence": 0 if snapshot.head is None else snapshot.head.sequence,
            "last_successful_transition": last_successful_transition,
            "last_failure_classification": last_failure_classification,
        },
        pending,
        repair_count,
    )


def _workspace_shell(entry: Mapping[str, object]) -> dict[str, object]:
    active_source = cast(Mapping[str, object], entry["active_source"])
    return {
        "repo_uuid": str(entry["repo_uuid"]),
        "state": "invalid",
        "safe_to_query": False,
        "reason_code": "workspace_not_inspected",
        "action_code": "inspect_workspace_state",
        "source_identity_sha256": canonical_sha256(active_source),
        "active_source_revision": cast(int, entry["active_source_revision"]),
        "source_epoch": None,
        "policy_sha256": None,
        "generations": {
            "pointer_revision": None,
            "current": None,
            "last_good": None,
            "pending": [],
            "pending_reason_code": "not_evaluated_p5b1",
        },
        "staged_build": {
            "present": False,
            "blocking": False,
            "revision": None,
            "generation_id": None,
            "lifecycle_state": None,
            "logical_request_sha256": None,
            "request_sha256": None,
        },
        "queue": {
            "revision": 0,
            "desired_watermark": 0,
            "completed_watermark": 0,
            "depth": 0,
            "pending": 0,
            "claimed": 0,
            "retrying": 0,
            "dead_letter": 0,
            "oldest_age_seconds": None,
            "age_reason_code": "not_recorded_v1",
        },
        "leases": {
            "migration_epoch": None,
            "workspace": _lease_summary(None),
            "semantic": _lease_summary(None),
        },
        "journal": {
            "sequence": 0,
            "last_successful_transition": None,
            "last_failure_classification": None,
        },
        "freshness": {
            "state": "not_observed",
            "duration_ms": None,
            "observation_boundary": "not_observed",
            "binding": None,
        },
        "watcher": {
            "state": "not_evaluated",
            "heartbeat": None,
            "boot_id": None,
            "process_id": None,
            "reason_code": "service_deferred_p5c",
        },
        "resources": {
            "state": "not_evaluated",
            "pressure": None,
            "reason_code": "resource_accounting_deferred_p5c",
        },
        "repair": {
            "required": False,
            "count": None,
        },
    }


def _workspace_failure(
    workspace: dict[str, object],
    checks: list[dict[str, str]],
    *,
    component: str,
    state: str,
    reason_code: str,
    action_code: str,
    repair_required: bool,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    workspace["state"] = state
    workspace["safe_to_query"] = False
    workspace["reason_code"] = reason_code
    workspace["action_code"] = action_code
    cast(dict[str, object], workspace["repair"])["required"] = repair_required
    checks.append(_check(component, state, reason_code, action_code))
    return workspace, checks


def _deadline_failure(
    workspace: dict[str, object],
    checks: list[dict[str, str]],
    *,
    component: str,
    reason_code: str = "inspection_deadline_exceeded",
) -> tuple[dict[str, object], list[dict[str, str]]]:
    return _workspace_failure(
        workspace,
        checks,
        component=component,
        state="degraded",
        reason_code=reason_code,
        action_code="retry_status",
        repair_required=False,
    )


def _lock_timeout_reason(exc: LockTimeout, kind: str) -> str:
    if exc.phase == "acquire" and exc.kind == kind:
        return f"{kind}_lock_contended"
    return "inspection_deadline_exceeded"


def _inspect_workspace(
    runtime: WorkspaceRuntime,
    registry: Registry,
    entry: Mapping[str, object],
    *,
    deadline_ns: int,
    journal_tokens: dict[str, tuple[int, str]],
    queue_tokens: dict[str, tuple[int, str]],
    staged_build_tokens: dict[str, tuple[int, str] | None],
) -> tuple[dict[str, object], list[dict[str, str]]]:
    repo_uuid = str(entry["repo_uuid"])
    prefix = f"workspace:{repo_uuid}"
    workspace = _workspace_shell(entry)
    checks: list[dict[str, str]] = []
    workspace_lock_acquired = False
    state_path_component = f"{prefix}:lock"
    try:
        with runtime.leases.read_only_workspace_lock(
            repo_uuid,
            deadline_ns=deadline_ns,
        ):
            workspace_lock_acquired = True
            checks.append(_check(f"{prefix}:lock", "ready", "ready", "none"))
            state_path_component = f"{prefix}:leases"
            try:
                lease_state = runtime.leases.read_only_snapshot_locked(
                    registry,
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
            except LockTimeout:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:leases",
                )
            except StateRecordMissing:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:leases",
                    state="invalid",
                    reason_code="workspace_record_missing",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            except StateCorrupt:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:leases",
                    state="invalid",
                    reason_code="workspace_record_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            workspace["leases"] = {
                "migration_epoch": lease_state.migration_epoch,
                "workspace": _lease_summary(lease_state.leases.get("workspace")),
                "semantic": _lease_summary(lease_state.leases.get("semantic")),
            }
            checks.append(_check(f"{prefix}:leases", "ready", "ready", "none"))

            state_path_component = f"{prefix}:gc"
            try:
                gc_intent = runtime.gc.read_only_intent_locked(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
            except LockTimeout:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:gc",
                )
            except (GcError, StateCorrupt):
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:gc",
                    state="invalid",
                    reason_code="workspace_state_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            if gc_intent is not None:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:gc",
                    state="invalid",
                    reason_code="workspace_state_invalid",
                    action_code="run_workspace_gc_reconcile",
                    repair_required=True,
                )
            checks.append(_check(f"{prefix}:gc", "ready", "ready", "none"))

            state_path_component = f"{prefix}:staged_build"
            try:
                staged_build = runtime.generations.read_only_staged_build_locked(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
            except LockTimeout:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:staged_build",
                )
            except StagedBuildReadRecoveryRequired:
                staged_summary = cast(dict[str, object], workspace["staged_build"])
                staged_summary.update(present=True, blocking=True)
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:staged_build",
                    state="degraded",
                    reason_code="staged_build_recovery_required",
                    action_code="resume_exact_workspace_sync",
                    repair_required=False,
                )
            except (GenerationError, StateCorrupt, ContractError, StatePathError):
                staged_summary = cast(dict[str, object], workspace["staged_build"])
                staged_summary.update(present=True, blocking=True)
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:staged_build",
                    state="invalid",
                    reason_code="staged_build_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            if staged_build is None:
                staged_build_tokens[repo_uuid] = None
            else:
                staged_value = staged_build.to_dict()
                lifecycle_state = str(staged_value["lifecycle_state"])
                blocking = lifecycle_state not in {"PROMOTED", "ABANDONED"}
                workspace["staged_build"] = {
                    "present": True,
                    "blocking": blocking,
                    "revision": int(staged_value["revision"]),
                    "generation_id": str(staged_value["generation_id"]),
                    "lifecycle_state": lifecycle_state,
                    "logical_request_sha256": str(
                        cast(Mapping[str, object], staged_value["request"])[
                            "logical_request_sha256"
                        ]
                    ),
                    "request_sha256": str(staged_value["request_sha256"]),
                }
                staged_build_tokens[repo_uuid] = (
                    int(staged_value["revision"]),
                    canonical_sha256(staged_value),
                )
                if blocking:
                    workspace["state"] = "degraded"
                    workspace["reason_code"] = "staged_build_recovery_required"
                    workspace["action_code"] = "resume_exact_workspace_sync"
                    checks.append(
                        _check(
                            f"{prefix}:staged_build",
                            "degraded",
                            "staged_build_recovery_required",
                            "resume_exact_workspace_sync",
                        )
                    )
                else:
                    checks.append(_check(f"{prefix}:staged_build", "ready", "ready", "none"))

            state_path_component = f"{prefix}:semantic_queue"
            try:
                queue = runtime.semantic_queue.read_only_snapshot_locked(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
            except LockTimeout:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:semantic_queue",
                )
            except SemanticQueueError:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:semantic_queue",
                    state="invalid",
                    reason_code="semantic_queue_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            queue_summary = _queue_summary(queue)
            queue_tokens[repo_uuid] = (queue.revision, queue.sha256)
            workspace["queue"] = queue_summary
            queue_issue = _queue_issue(queue_summary)
            if queue_issue is None:
                checks.append(_check(f"{prefix}:semantic_queue", "ready", "ready", "none"))
            else:
                queue_reason, queue_action = queue_issue
                workspace["state"] = "degraded"
                if not bool(
                    cast(Mapping[str, object], workspace["staged_build"])["blocking"]
                ):
                    workspace["reason_code"] = queue_reason
                    workspace["action_code"] = queue_action
                checks.append(
                    _check(
                        f"{prefix}:semantic_queue",
                        "degraded",
                        queue_reason,
                        queue_action,
                    )
                )

            state_path_component = f"{prefix}:pointer"
            try:
                require_before_deadline(
                    deadline_ns,
                    "pointer inspection exceeded its deadline",
                )
                pointer = runtime.pointers.load(
                    repo_uuid,
                    allow_missing=True,
                    deadline_ns=deadline_ns,
                )
                require_before_deadline(
                    deadline_ns,
                    "pointer inspection exceeded its deadline",
                )
            except LockTimeout:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:pointer",
                )
            except PointerRecoveryRequired:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:pointer",
                    state="invalid",
                    reason_code="pointer_recovery_required",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            except PointerError:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:pointer",
                    state="invalid",
                    reason_code="pointer_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            state_path_component = f"{prefix}:journal"
            try:
                require_before_deadline(
                    deadline_ns,
                    "journal inspection exceeded its deadline",
                )
                journal_directory_exists = runtime.journal.state.private_directory_exists(
                    runtime.journal._directory(repo_uuid)
                )
                journal = (
                    JournalSnapshot(head=None, events=())
                    if pointer is None and not journal_directory_exists
                    else runtime.journal.read_stable(
                        repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                )
                require_before_deadline(
                    deadline_ns,
                    "journal inspection exceeded its deadline",
                )
                journal_summary, pending_generations, repair_count = _journal_summary(
                    journal,
                    deadline_ns=deadline_ns,
                )
            except LockTimeout:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:journal",
                )
            except JournalError:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:journal",
                    state="invalid",
                    reason_code="journal_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            workspace["journal"] = journal_summary
            journal_tokens[repo_uuid] = _journal_snapshot_token(journal)
            generations = cast(dict[str, object], workspace["generations"])
            generations["pending"] = pending_generations
            generations["pending_reason_code"] = (
                "generation_pending" if pending_generations else "ready"
            )
            cast(dict[str, object], workspace["repair"])["count"] = repair_count
            checks.append(_check(f"{prefix}:journal", "ready", "ready", "none"))
            if pointer is None:
                if bool(
                    cast(Mapping[str, object], workspace["staged_build"])["blocking"]
                ):
                    checks.append(
                        _check(
                            f"{prefix}:pointer",
                            "degraded",
                            "no_current_generation",
                            "run_workspace_sync",
                        )
                    )
                    return workspace, checks
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:pointer",
                    state="degraded",
                    reason_code="no_current_generation",
                    action_code="run_workspace_sync",
                    repair_required=False,
                )

            state_path_component = f"{prefix}:generation"
            try:
                with runtime.pointers.read_current(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                ) as reading:
                    if reading.pointer.canonical != pointer.canonical:
                        raise PointerError("pointer changed during locked status read")
                    receipts = runtime.pointers.verify_pointer(
                        pointer,
                        expected_repo_uuid=repo_uuid,
                        deadline_ns=deadline_ns,
                    )
            except LockTimeout as exc:
                return _deadline_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:generation",
                    reason_code=_lock_timeout_reason(exc, "generation"),
                )
            except UnsupportedCompatibility:
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:compatibility",
                    state="invalid",
                    reason_code="unsupported_compatibility",
                    action_code="install_supported_candidate",
                    repair_required=False,
                )
            except (GenerationError, PointerError, StatePathError):
                return _workspace_failure(
                    workspace,
                    checks,
                    component=f"{prefix}:generation",
                    state="invalid",
                    reason_code="generation_or_pointer_invalid",
                    action_code="run_workspace_repair",
                    repair_required=True,
                )
            pointer_value = pointer.to_dict()
            current_receipt = receipts["current"].to_dict()
            workspace["source_epoch"] = int(pointer_value["source_epoch"])
            workspace["policy_sha256"] = str(current_receipt["policy_sha256"])
            workspace["generations"] = {
                "pointer_revision": int(pointer_value["pointer_revision"]),
                "current": _generation_reference(pointer_value["current"]),
                "last_good": _generation_reference(pointer_value["last_good"]),
                "pending": pending_generations,
                "pending_reason_code": ("generation_pending" if pending_generations else "ready"),
            }
            if queue_issue is None and not bool(
                cast(Mapping[str, object], workspace["staged_build"])["blocking"]
            ):
                workspace["state"] = "ready"
                workspace["reason_code"] = "freshness_not_observed"
                workspace["action_code"] = "verify_freshness"
            checks.append(_check(f"{prefix}:pointer", "ready", "ready", "none"))
            return workspace, checks
    except LockTimeout:
        return _deadline_failure(
            workspace,
            checks,
            component=f"{prefix}:lock",
            reason_code=(
                "inspection_deadline_exceeded"
                if workspace_lock_acquired
                else "workspace_lock_contended"
            ),
        )
    except StatePathError:
        return _workspace_failure(
            workspace,
            checks,
            component=state_path_component,
            state="invalid",
            reason_code=(
                "workspace_state_invalid" if workspace_lock_acquired else "workspace_lock_invalid"
            ),
            action_code="run_workspace_repair",
            repair_required=True,
        )
    except (WorkspaceRuntimeError, LeaseError, ContractError):
        return _workspace_failure(
            workspace,
            checks,
            component=f"{prefix}:state",
            state="invalid",
            reason_code="workspace_state_invalid",
            action_code="run_workspace_repair",
            repair_required=True,
        )


def _mark_snapshot_changed(
    workspace: dict[str, object],
    checks: list[dict[str, str]],
    *,
    component: str,
) -> None:
    cast(dict[str, object], workspace["freshness"])["state"] = "drift"
    workspace["state"] = "degraded"
    workspace["safe_to_query"] = False
    workspace["reason_code"] = "status_snapshot_changed"
    workspace["action_code"] = "retry_status"
    checks.append(
        _check(
            component,
            "degraded",
            "status_snapshot_changed",
            "retry_status",
        )
    )


def _queue_snapshot_is_current_locked(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    expected: tuple[int, str] | None,
    *,
    deadline_ns: int,
) -> bool:
    if expected is None:
        return False
    try:
        observed = runtime.semantic_queue.read_only_snapshot_locked(
            repo_uuid,
            deadline_ns=deadline_ns,
        )
    except LockTimeout:
        raise
    except (StateCorrupt, StatePathError, ContractError, LeaseError, SemanticQueueError):
        return False
    return (observed.revision, observed.sha256) == expected


def _gc_intent_is_absent_locked(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    *,
    deadline_ns: int,
) -> bool:
    try:
        return (
            runtime.gc.read_only_intent_locked(
                repo_uuid,
                deadline_ns=deadline_ns,
            )
            is None
        )
    except LockTimeout:
        raise
    except (WorkspaceRuntimeError, ContractError, GcError, LeaseError):
        return False


def _journal_snapshot_token(snapshot: JournalSnapshot) -> tuple[int, str]:
    if snapshot.head is None:
        return (0, canonical_sha256(None))
    return (
        snapshot.head.revision,
        canonical_sha256(snapshot.head.to_dict()),
    )


def _journal_snapshot_is_current_locked(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    expected: tuple[int, str] | None,
    *,
    deadline_ns: int,
) -> bool:
    if expected is None:
        return False
    try:
        observed = runtime.journal.read_stable(
            repo_uuid,
            deadline_ns=deadline_ns,
        )
    except LockTimeout:
        raise
    except (WorkspaceRuntimeError, ContractError, JournalError, LeaseError):
        return False
    return _journal_snapshot_token(observed) == expected


def _staged_build_snapshot_is_current_locked(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
    expected: tuple[int, str] | None,
    *,
    deadline_ns: int,
) -> bool:
    try:
        observed = runtime.generations.read_only_staged_build_locked(
            repo_uuid,
            deadline_ns=deadline_ns,
        )
    except LockTimeout:
        raise
    except (GenerationError, WorkspaceRuntimeError, ContractError, StatePathError):
        return False
    token = (
        None
        if observed is None
        else (observed.revision, canonical_sha256(observed.to_dict()))
    )
    return token == expected


def _pointer_snapshot_is_current_locked(
    runtime: WorkspaceRuntime,
    workspace: Mapping[str, object],
    *,
    deadline_ns: int,
) -> bool:
    generations = cast(Mapping[str, object], workspace["generations"])
    current = generations["current"]
    pointer_revision = generations["pointer_revision"]
    if not isinstance(current, Mapping) or not isinstance(pointer_revision, int):
        return False
    expected = (
        pointer_revision,
        str(current["generation_id"]),
        str(current["receipt_sha256"]),
    )
    try:
        with runtime.pointers.read_current(
            str(workspace["repo_uuid"]),
            deadline_ns=deadline_ns,
        ) as reading:
            observed = reading.pointer.to_dict()
            observed_current = cast(Mapping[str, object], observed["current"])
            token = (
                int(observed["pointer_revision"]),
                str(observed_current["generation_id"]),
                str(observed_current["receipt_sha256"]),
            )
    except LockTimeout:
        raise
    except (ContractError, GenerationError, PointerError, UnsupportedCompatibility):
        return False
    return token == expected


def _apply_freshness(
    runtime: WorkspaceRuntime,
    workspace: dict[str, object],
    checks: list[dict[str, str]],
    *,
    deadline_ns: int,
) -> None:
    generations = cast(Mapping[str, object], workspace["generations"])
    staged_build = cast(Mapping[str, object], workspace["staged_build"])
    if (
        generations["current"] is None
        or workspace["state"] == "invalid"
        or bool(staged_build["blocking"])
    ):
        return
    repo_uuid = str(workspace["repo_uuid"])
    component = f"workspace:{repo_uuid}:freshness"
    started_ns = time.monotonic_ns()
    result = runtime.freshness.probe(
        repo_uuid,
        timeout_ns=max(0, deadline_ns - started_ns),
    )
    duration_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
    freshness = cast(dict[str, object], workspace["freshness"])
    freshness["duration_ms"] = duration_ms
    freshness["observation_boundary"] = result.observation_boundary
    binding: dict[str, object] | None = None
    if result.release is not None:
        release = result.release.to_dict()
        pre = cast(Mapping[str, object], release["pre_observation"])
        post = cast(Mapping[str, object], release["post_observation"])
        binding_keys = (
            "active_source_revision",
            "pointer_revision",
            "receipt_sha256",
        )
        if all(pre[key] == post[key] for key in binding_keys):
            binding = {key: pre[key] for key in binding_keys}
    freshness["binding"] = binding
    if result.decision == "release" and result.reason == "observed_current":
        current = cast(Mapping[str, object], generations["current"])
        expected_binding = {
            "active_source_revision": workspace["active_source_revision"],
            "pointer_revision": generations["pointer_revision"],
            "receipt_sha256": current["receipt_sha256"],
        }
        if binding != expected_binding:
            _mark_snapshot_changed(
                workspace,
                checks,
                component=component,
            )
            return
        freshness["state"] = "observed_current"
        checks.append(_check(component, "ready", "ready", "none"))
        if workspace["state"] == "ready":
            workspace["safe_to_query"] = True
            workspace["reason_code"] = "ready"
            workspace["action_code"] = "none"
        return

    classification = {
        "drift": ("drift", "degraded", "source_drift", "run_workspace_sync"),
        "source_unavailable": (
            "unavailable",
            "degraded",
            "source_unavailable",
            "restore_source",
        ),
        "unstable": ("unstable", "degraded", "source_unstable", "retry_status"),
        "timeout": ("timeout", "degraded", "freshness_timeout", "retry_status"),
        "unsupported": (
            "unsupported",
            "invalid",
            "freshness_unsupported",
            "inspect_source",
        ),
    }
    freshness_state, state, reason_code, action_code = classification.get(
        result.reason,
        ("unsupported", "invalid", "freshness_unsupported", "inspect_source"),
    )
    freshness["state"] = freshness_state
    workspace["state"] = state
    workspace["safe_to_query"] = False
    workspace["reason_code"] = reason_code
    workspace["action_code"] = action_code
    checks.append(_check(component, state, reason_code, action_code))


def inspect_workspace_status(
    inputs: WorkspaceRuntimeInputs,
    *,
    deadline_ns: int | None = None,
) -> WorkspaceStatusReport:
    """Inspect all registered workspaces without recovery or filesystem writes."""

    checks: list[dict[str, str]] = []
    compatibility_sha256: str | None = None
    try:
        compatibility_sha256 = inputs.compatibility_manifest.sha256
        runtime = compose_workspace_runtime(inputs)
    except UnsupportedCompatibility:
        checks.append(
            _check(
                "compatibility",
                "invalid",
                "unsupported_compatibility",
                "install_supported_candidate",
            )
        )
        return _finalize(
            runtime=_runtime_summary(compatibility_sha256),
            workspaces=[],
            checks=checks,
        )
    except UnsupportedRuntime:
        checks.append(
            _check(
                "runtime",
                "invalid",
                "unsupported_runtime",
                "use_supported_runtime",
            )
        )
        return _finalize(
            runtime=_runtime_summary(compatibility_sha256),
            workspaces=[],
            checks=checks,
        )
    except (StatePathError, ContractError, ValueError, TypeError):
        checks.append(
            _check(
                "state_root",
                "invalid",
                "unsafe_state_path",
                "configure_safe_state_root",
            )
        )
        return _finalize(
            runtime=_runtime_summary(compatibility_sha256),
            workspaces=[],
            checks=checks,
        )

    checks.extend(
        (
            _check("compatibility", "ready", "ready", "none"),
            _check("runtime", "ready", "ready", "none"),
        )
    )
    try:
        state_root_exists = runtime.registry.state.root_exists_for_inspection()
    except StatePathError:
        checks.append(
            _check(
                "state_root",
                "invalid",
                "unsafe_state_path",
                "configure_safe_state_root",
            )
        )
        return _finalize(
            runtime=_runtime_summary(compatibility_sha256),
            workspaces=[],
            checks=checks,
        )
    if not state_root_exists:
        checks.append(
            _check(
                "state_root",
                "invalid",
                "state_root_missing",
                "register_workspace",
            )
        )
        return _finalize(
            runtime=_runtime_summary(compatibility_sha256),
            workspaces=[],
            checks=checks,
        )
    checks.append(_check("state_root", "ready", "ready", "none"))

    try:
        registry_lock_exists = runtime.registry.state.private_file_exists(runtime.registry.LOCK)
    except StatePathError:
        registry_lock_exists = True
    if not registry_lock_exists:
        checks.append(
            _check(
                "registry",
                "invalid",
                "registry_lock_missing",
                "register_workspace",
            )
        )
        return _finalize(
            runtime=_runtime_summary(compatibility_sha256),
            workspaces=[],
            checks=checks,
        )

    absolute_deadline = (
        time.monotonic_ns() + _DEFAULT_INSPECTION_TIMEOUT_NS if deadline_ns is None else deadline_ns
    )
    workspaces: list[dict[str, object]] = []
    journal_tokens: dict[str, tuple[int, str]] = {}
    queue_tokens: dict[str, tuple[int, str]] = {}
    staged_build_tokens: dict[str, tuple[int, str] | None] = {}
    registry_token: tuple[int, str] | None = None
    registry_lock_acquired = False
    try:
        with runtime.registry.read_only_snapshot(deadline_ns=absolute_deadline) as registry:
            registry_lock_acquired = True
            checks.append(_check("registry", "ready", "ready", "none"))
            registry_value = registry.to_dict()
            registry_token = (int(registry_value["revision"]), registry.sha256)
            entries = cast(
                list[dict[str, object]],
                registry_value["workspaces"],
            )
            for entry in entries:
                require_before_deadline(
                    absolute_deadline,
                    "workspace registry traversal exceeded its deadline",
                )
                workspace, workspace_checks = _inspect_workspace(
                    runtime,
                    registry,
                    entry,
                    deadline_ns=absolute_deadline,
                    journal_tokens=journal_tokens,
                    queue_tokens=queue_tokens,
                    staged_build_tokens=staged_build_tokens,
                )
                workspaces.append(workspace)
                checks.extend(workspace_checks)
    except LockTimeout:
        for workspace in workspaces:
            if workspace["state"] == "ready":
                _deadline_failure(
                    workspace,
                    checks,
                    component=f"workspace:{workspace['repo_uuid']}:inspection",
                )
        checks.append(
            _check(
                "registry",
                "degraded",
                (
                    "inspection_deadline_exceeded"
                    if registry_lock_acquired
                    else "registry_lock_contended"
                ),
                "retry_status",
            )
        )
    except StatePathError:
        checks.append(
            _check(
                "registry",
                "invalid",
                "registry_lock_invalid",
                "register_workspace",
            )
        )
    except (StateCorrupt, ContractError):
        checks.append(
            _check(
                "registry",
                "invalid",
                "registry_invalid",
                "run_workspace_repair",
            )
        )
    else:
        if not workspaces:
            checks.append(
                _check(
                    "workspaces",
                    "degraded",
                    "no_registered_workspaces",
                    "register_workspace",
                )
            )
        else:
            try:
                for workspace in workspaces:
                    require_before_deadline(
                        absolute_deadline,
                        "workspace status finalization exceeded its deadline",
                    )
                    _apply_freshness(
                        runtime,
                        workspace,
                        checks,
                        deadline_ns=absolute_deadline,
                    )
                if any(bool(workspace["safe_to_query"]) for workspace in workspaces):
                    with runtime.registry.read_only_snapshot(
                        deadline_ns=absolute_deadline,
                    ) as observed_registry:
                        observed_registry_token = (
                            int(observed_registry.to_dict()["revision"]),
                            observed_registry.sha256,
                        )
                        if registry_token is None or observed_registry_token != registry_token:
                            for workspace in workspaces:
                                if bool(workspace["safe_to_query"]):
                                    _mark_snapshot_changed(
                                        workspace,
                                        checks,
                                        component=(
                                            f"workspace:{workspace['repo_uuid']}:registry"
                                        ),
                                    )
                        else:
                            for workspace in workspaces:
                                if not bool(workspace["safe_to_query"]):
                                    continue
                                require_before_deadline(
                                    absolute_deadline,
                                    "workspace final snapshot exceeded its deadline",
                                )
                                repo_uuid = str(workspace["repo_uuid"])
                                try:
                                    with runtime.leases.read_only_workspace_lock(
                                        repo_uuid,
                                        deadline_ns=absolute_deadline,
                                    ):
                                        if not _gc_intent_is_absent_locked(
                                            runtime,
                                            repo_uuid,
                                            deadline_ns=absolute_deadline,
                                        ):
                                            _workspace_failure(
                                                workspace,
                                                checks,
                                                component=f"workspace:{repo_uuid}:gc",
                                                state="invalid",
                                                reason_code="workspace_state_invalid",
                                                action_code="run_workspace_gc_reconcile",
                                                repair_required=True,
                                            )
                                        elif not _queue_snapshot_is_current_locked(
                                            runtime,
                                            repo_uuid,
                                            queue_tokens.get(repo_uuid),
                                            deadline_ns=absolute_deadline,
                                        ):
                                            _mark_snapshot_changed(
                                                workspace,
                                                checks,
                                                component=f"workspace:{repo_uuid}:snapshot",
                                            )
                                        elif not _journal_snapshot_is_current_locked(
                                            runtime,
                                            repo_uuid,
                                            journal_tokens.get(repo_uuid),
                                            deadline_ns=absolute_deadline,
                                        ):
                                            _mark_snapshot_changed(
                                                workspace,
                                                checks,
                                                component=(
                                                    f"workspace:{repo_uuid}:journal_snapshot"
                                                ),
                                            )
                                        elif not _pointer_snapshot_is_current_locked(
                                            runtime,
                                            workspace,
                                            deadline_ns=absolute_deadline,
                                        ):
                                            _mark_snapshot_changed(
                                                workspace,
                                                checks,
                                                component=(
                                                    f"workspace:{repo_uuid}:pointer_snapshot"
                                                ),
                                            )
                                        elif not _staged_build_snapshot_is_current_locked(
                                            runtime,
                                            repo_uuid,
                                            staged_build_tokens.get(repo_uuid),
                                            deadline_ns=absolute_deadline,
                                        ):
                                            _mark_snapshot_changed(
                                                workspace,
                                                checks,
                                                component=(
                                                    f"workspace:{repo_uuid}:staged_build_snapshot"
                                                ),
                                            )
                                except LockTimeout:
                                    raise
                                except (WorkspaceRuntimeError, ContractError):
                                    _workspace_failure(
                                        workspace,
                                        checks,
                                        component=f"workspace:{repo_uuid}:state",
                                        state="invalid",
                                        reason_code="workspace_state_invalid",
                                        action_code="run_workspace_repair",
                                        repair_required=True,
                                    )
            except LockTimeout:
                for workspace in workspaces:
                    if workspace["state"] == "ready":
                        _deadline_failure(
                            workspace,
                            checks,
                            component=f"workspace:{workspace['repo_uuid']}:inspection",
                        )
                checks.append(
                    _check(
                        "status",
                        "degraded",
                        "inspection_deadline_exceeded",
                        "retry_status",
                    )
                )
            except (WorkspaceRuntimeError, ContractError):
                for workspace in workspaces:
                    if bool(workspace["safe_to_query"]):
                        _mark_snapshot_changed(
                            workspace,
                            checks,
                            component=f"workspace:{workspace['repo_uuid']}:registry",
                        )
    return _finalize(
        runtime=_runtime_summary(compatibility_sha256),
        workspaces=workspaces,
        checks=checks,
    )


__all__ = [
    "ACTION_CODES",
    "EXIT_DEGRADED",
    "EXIT_INVALID",
    "EXIT_READY",
    "EXIT_USAGE",
    "REASON_CODES",
    "STATUS_CONTRACT",
    "STATUS_SCHEMA_VERSION",
    "WorkspaceStatusReport",
    "inspect_workspace_status",
    "invalid_workspace_authority_report",
    "load_status_schema",
    "missing_workspace_authority_report",
]
