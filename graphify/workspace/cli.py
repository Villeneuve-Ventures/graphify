"""Narrow workspace activation, identity, sync, query, GC, status, and doctor commands."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
from json import loads as _load_json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, cast, Mapping, Sequence, TextIO

import graphify.workspace.rollback as rollback_runtime
import graphify.workspace.gc_command as gc_command_runtime
import graphify.workspace.repair as repair_runtime

from graphify.workspace.adapters import (
    QueryRejected,
    QueryRequest,
    UnsupportedCompatibility,
)
from graphify.workspace.composition import (
    WorkspaceAuthorityError,
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
    load_workspace_runtime_inputs,
)
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    CapacityPolicy,
    ContractError,
    WorkspaceLeaseState,
    canonical_json_bytes,
    canonical_registry_source,
    canonical_sha256,
)
from graphify.workspace.gc import (
    GC_PREVIEW_MAX_GENERATIONS,
    GcCoordinationUnavailable,
    GcPreview,
    GcPreviewAuthorityConflict,
    GcPreviewUnstable,
    GcProtection,
    GcRecoveryRequired,
)
from graphify.workspace.generations import (
    CapacityExceeded,
    GenerationConflict,
    GenerationError,
)
from graphify.workspace.freshness import FreshnessResult
from graphify.workspace.journal import JournalError, JournalRecoveryRequired
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    IdentityError,
    OperatorAuthorization,
    SourceAmbiguousError,
    SourceDiscoveryError,
    SourceDiscoveryTimeout,
    SourceIdentity,
    UUIDCollisionError,
    discover_source,
    source_root_identity,
    verify_source_checkout,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    StateCorrupt,
    StatePathError,
    StateRecoveryRequired,
    UnsupportedRuntime,
)
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseError,
    LeaseExpired,
    LeaseRecoveryRequired,
    StaleLease,
)
from graphify.workspace.pointers import (
    PointerConflict,
    PointerCorrupt,
    PointerRecoveryRequired,
)
from graphify.workspace.registry import (
    ActivationResult,
    RegistryStore,
    RevisionConflict,
    SourceAlreadyActive,
)
from graphify.workspace.repair import (
    RepairExecuteRequest,
    RepairPreviewRequest,
    repair_execute,
    repair_preview,
)
from graphify.workspace.semantic_queue import SemanticQueueError
from graphify.workspace.status import (
    EXIT_DEGRADED,
    EXIT_INVALID,
    EXIT_READY,
    EXIT_USAGE,
    WorkspaceStatusReport,
    inspect_workspace_status,
    invalid_workspace_authority_report,
    missing_workspace_authority_report,
)
from graphify.workspace.sync import (
    SYNC_MODE,
    SYNC_RECEIPT_CONTRACT,
    SYNC_REQUEST_MAX_BYTES,
    SYNC_SCHEMA_VERSION,
    SyncReceipt,
    SyncRequest,
    SyncRequestInvalid,
    WorkspaceSyncError,
    synchronize_code_only,
)


_REGISTRATION_CONTRACT = "graphify.workspace.registration"
_REGISTRATION_SCHEMA_VERSION = 1
_IDENTITY_MAINTENANCE_CONTRACT = "graphify.workspace.identity_maintenance"
_IDENTITY_MAINTENANCE_SCHEMA_VERSION = 1
_ACTIVATION_CONTRACT = "graphify.workspace.activation"
_ACTIVATION_SCHEMA_VERSION = 1
_AUTHORIZATION_MAX_BYTES = 16 * 1024


class _DiscardingEngineStream:
    """Text sink that keeps engine diagnostics out of canonical CLI receipts."""

    encoding = "utf-8"
    errors = "replace"

    @staticmethod
    def write(value: str) -> int:
        return len(value)

    @staticmethod
    def flush() -> None:
        return None

    @staticmethod
    def isatty() -> bool:
        return False


_DISCARDED_ENGINE_OUTPUT = _DiscardingEngineStream()
_REGISTRATION_CONFIG_MAX_BYTES = 64 * 1024
_REGISTRATION_SOURCE_TIMEOUT_NS = 5_000_000_000
_REGISTRATION_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "registration.schema.json"
)
_IDENTITY_MAINTENANCE_SCHEMA_PATH = (
    Path(__file__).parent
    / "schemas"
    / "cli"
    / "v1"
    / "identity-maintenance.schema.json"
)
_ACTIVATION_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "activation.schema.json"
)
_SYNC_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "sync-request.schema.json"
)
_SYNC_RECEIPT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "sync-receipt.schema.json"
)
_QUERY_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "query-request.schema.json"
)
_QUERY_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "query-result.schema.json"
)
_ROLLBACK_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "rollback-request.schema.json"
)
_ROLLBACK_RECEIPT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "rollback-receipt.schema.json"
)
_GC_PREVIEW_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-preview-request.schema.json"
)
_GC_PREVIEW_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-preview-result.schema.json"
)
_GC_EXECUTE_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-execute-request.schema.json"
)
_GC_EXECUTE_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-execute-result.schema.json"
)
_GC_RECONCILE_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-reconcile-request.schema.json"
)
_GC_RECONCILE_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-reconcile-result.schema.json"
)
_GC_PURGE_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-purge-request.schema.json"
)
_GC_PURGE_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "gc-purge-result.schema.json"
)
_REPAIR_PREVIEW_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "repair-preview-request.schema.json"
)
_REPAIR_PREVIEW_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "repair-preview-result.schema.json"
)
_REPAIR_EXECUTE_REQUEST_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "repair-execute-request.schema.json"
)
_REPAIR_EXECUTE_RESULT_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "cli" / "v1" / "repair-execute-result.schema.json"
)
_REVISION_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_REGISTER_USAGE = (
    "graphify workspace register <enroll|adopt|rebind|rotate> --repo-uuid UUID "
    "--expected-registry-revision N --authorization-stdin"
)
_SYNC_USAGE = "graphify workspace sync --code-only --request-stdin"
_QUERY_USAGE = "graphify workspace query --request-stdin"
_ROLLBACK_USAGE = "graphify workspace rollback --request-stdin"
_GC_PREVIEW_USAGE = "graphify workspace gc --dry-run --request-stdin"
_GC_EXECUTE_USAGE = "graphify workspace gc --execute --request-stdin"
_GC_RECONCILE_USAGE = "graphify workspace gc --reconcile --request-stdin"
_GC_PURGE_USAGE = "graphify workspace gc --purge --request-stdin"
_REPAIR_PREVIEW_USAGE = "graphify workspace repair --dry-run --request-stdin"
_REPAIR_EXECUTE_USAGE = "graphify workspace repair --execute --request-stdin"
_ACTIVATION_USAGE = (
    "graphify workspace activate --repo-uuid UUID "
    "--expected-registry-revision N --expected-active-source-revision N "
    "--expected-operation-epoch N --expected-migration-epoch N "
    "--authorization-stdin"
)
_USAGE = (
    "Usage: graphify workspace status --json\n"
    "       graphify workspace doctor\n"
    f"       {_REGISTER_USAGE}\n"
    f"       {_SYNC_USAGE}\n"
    f"       {_QUERY_USAGE}\n"
    f"       {_ROLLBACK_USAGE}\n"
    f"       {_GC_PREVIEW_USAGE}\n"
    f"       {_GC_EXECUTE_USAGE}\n"
    f"       {_GC_RECONCILE_USAGE}\n"
    f"       {_GC_PURGE_USAGE}\n"
    f"       {_REPAIR_PREVIEW_USAGE}\n"
    f"       {_REPAIR_EXECUTE_USAGE}\n"
    f"       {_ACTIVATION_USAGE}"
)

_ACTIVATION_LEASE_TTL_NS = 30_000_000_000

_QUERY_REQUEST_CONTRACT = "graphify.workspace.query_request"
_QUERY_RESULT_CONTRACT = "graphify.workspace.query_result"
_QUERY_SCHEMA_VERSION = 1
_QUERY_REQUEST_MAX_BYTES = 32 * 1024
_QUERY_TIMEOUT_MAX_MS = 60_000
_QUERY_REQUEST_FIELDS = frozenset(
    {
        "contract",
        "schema_version",
        "cli_contract_version",
        "repo_uuid",
        "question",
        "mode",
        "depth",
        "token_budget",
        "context_filters",
        "timeout_ms",
    }
)

_GC_PREVIEW_REQUEST_CONTRACT = "graphify.workspace.gc_preview_request"
_GC_PREVIEW_RESULT_CONTRACT = "graphify.workspace.gc_preview_result"
_GC_PREVIEW_SCHEMA_VERSION = 1
_GC_PREVIEW_REQUEST_MAX_BYTES = 128 * 1024
_GC_PREVIEW_TIMEOUT_MAX_MS = 60_000
_GC_PREVIEW_REQUEST_FIELDS = frozenset(
    {
        "capacity_policy",
        "cli_contract_version",
        "contract",
        "expected_active_source_revision",
        "expected_migration_epoch",
        "expected_operation_epoch",
        "expected_pointer_revision",
        "expected_registry_revision",
        "protections",
        "repo_uuid",
        "schema_version",
        "timeout_ms",
    }
)
_GC_PROTECTION_FIELDS = (
    "active_lease_generations",
    "fixture_generations",
    "migration_sources",
    "proof_generations",
    "rollback_artifact_generations",
    "rollback_sources",
)
_GC_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SIGNED_REVISION = 9_223_372_036_854_775_807
_GC_PROTECTION_REASONS = frozenset(
    {
        "active_lease",
        "fixture",
        "migration_source",
        "prior_current",
        "prior_last_good",
        "proof",
        "rollback_artifact",
        "rollback_source",
        "shared_lock",
        "visible_current",
        "visible_last_good",
    }
)


@dataclass(frozen=True)
class _RegisterRequest:
    action: IdentityAction
    repo_uuid: str
    expected_registry_revision: int


@dataclass(frozen=True)
class _ActivationRequest:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int

    @property
    def action(self) -> IdentityAction:
        return IdentityAction.ACTIVATE


@dataclass(frozen=True)
class _RegistrationFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str
    registry_revision: int | None = None


@dataclass(frozen=True)
class _IdentityMaintenanceFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str
    registry_revision: int | None = None


@dataclass(frozen=True)
class _ActivationFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str
    registry_revision: int | None = None


@dataclass(frozen=True)
class _SyncFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str


@dataclass(frozen=True)
class _QueryCliRequest:
    repo_uuid: str
    query: QueryRequest
    timeout_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "context_filters": list(self.query.context_filters),
            "contract": _QUERY_REQUEST_CONTRACT,
            "depth": self.query.depth,
            "mode": self.query.mode,
            "question": self.query.question,
            "repo_uuid": self.repo_uuid,
            "schema_version": _QUERY_SCHEMA_VERSION,
            "timeout_ms": self.timeout_ms,
            "token_budget": self.query.token_budget,
        }


@dataclass(frozen=True)
class _QueryFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str
    query_executed: bool = False
    observation_boundary: str = "not_observed"


@dataclass(frozen=True)
class _GcPreviewRequest:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    expected_pointer_revision: int
    timeout_ms: int
    capacity_policy: CapacityPolicy
    protections: GcProtection

    def to_dict(self) -> dict[str, object]:
        return {
            "capacity_policy": self.capacity_policy.to_dict(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": _GC_PREVIEW_REQUEST_CONTRACT,
            "expected_active_source_revision": self.expected_active_source_revision,
            "expected_migration_epoch": self.expected_migration_epoch,
            "expected_operation_epoch": self.expected_operation_epoch,
            "expected_pointer_revision": self.expected_pointer_revision,
            "expected_registry_revision": self.expected_registry_revision,
            "protections": {
                "active_lease_generations": sorted(
                    self.protections.active_lease_generations
                ),
                "fixture_generations": sorted(self.protections.fixture_generations),
                "migration_sources": sorted(self.protections.migration_sources),
                "proof_generations": sorted(self.protections.proof_generations),
                "rollback_artifact_generations": sorted(
                    self.protections.rollback_artifact_generations
                ),
                "rollback_sources": sorted(self.protections.rollback_sources),
            },
            "repo_uuid": self.repo_uuid,
            "schema_version": _GC_PREVIEW_SCHEMA_VERSION,
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True)
class _GcPreviewFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str
    observation_boundary: str = "not_observed"


class _QueryRequestInvalid(ValueError):
    """The query CLI request cannot be accepted safely."""


class _QueryRequestUnsupported(_QueryRequestInvalid):
    """The query CLI request names an unsupported public contract."""


class _GcPreviewRequestInvalid(ValueError):
    """The GC preview request cannot be accepted safely."""


class _GcPreviewRequestUnsupported(_GcPreviewRequestInvalid):
    """The GC preview request names an unsupported public contract."""


def _parse_register_request(command: tuple[str, ...]) -> _RegisterRequest | None:
    if len(command) < 2 or command[0] != "register":
        return None
    try:
        action = {
            "enroll": IdentityAction.ENROLL,
            "adopt": IdentityAction.ADOPT,
            "rebind": IdentityAction.REBIND,
            "rotate": IdentityAction.ROTATE,
        }[command[1]]
    except KeyError:
        return None

    values: dict[str, str] = {}
    authorization_stdin = False
    index = 2
    while index < len(command):
        argument = command[index]
        if argument == "--authorization-stdin":
            if authorization_stdin:
                return None
            authorization_stdin = True
            index += 1
            continue
        if argument not in {"--repo-uuid", "--expected-registry-revision"}:
            return None
        if argument in values or index + 1 >= len(command):
            return None
        value = command[index + 1]
        if value.startswith("--"):
            return None
        values[argument] = value
        index += 2

    if not authorization_stdin or set(values) != {
        "--repo-uuid",
        "--expected-registry-revision",
    }:
        return None
    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(values["--repo-uuid"])
    except ContractError:
        return None
    revision_value = values["--expected-registry-revision"]
    if _REVISION_RE.fullmatch(revision_value) is None:
        return None
    return _RegisterRequest(
        action=action,
        repo_uuid=repo_uuid,
        expected_registry_revision=int(revision_value),
    )


def _parse_activation_request(command: tuple[str, ...]) -> _ActivationRequest | None:
    if not command or command[0] != "activate":
        return None
    values: dict[str, str] = {}
    authorization_stdin = False
    index = 1
    value_options = {
        "--repo-uuid",
        "--expected-registry-revision",
        "--expected-active-source-revision",
        "--expected-operation-epoch",
        "--expected-migration-epoch",
    }
    while index < len(command):
        argument = command[index]
        if argument == "--authorization-stdin":
            if authorization_stdin:
                return None
            authorization_stdin = True
            index += 1
            continue
        if argument not in value_options or argument in values or index + 1 >= len(command):
            return None
        value = command[index + 1]
        if value.startswith("--"):
            return None
        values[argument] = value
        index += 2

    if not authorization_stdin or set(values) != value_options:
        return None
    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(values["--repo-uuid"])
    except ContractError:
        return None
    revision_options = (
        "--expected-registry-revision",
        "--expected-active-source-revision",
        "--expected-operation-epoch",
        "--expected-migration-epoch",
    )
    if any(_REVISION_RE.fullmatch(values[option]) is None for option in revision_options):
        return None
    return _ActivationRequest(
        repo_uuid=repo_uuid,
        expected_registry_revision=int(values["--expected-registry-revision"]),
        expected_active_source_revision=int(values["--expected-active-source-revision"]),
        expected_operation_epoch=int(values["--expected-operation-epoch"]),
        expected_migration_epoch=int(values["--expected-migration-epoch"]),
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate authorization field")
        result[key] = value
    return result


def _read_authorization_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    if binary_input is not None:
        raw = binary_input.read(_AUTHORIZATION_MAX_BYTES + 1)
        if not isinstance(raw, bytes):
            raise TypeError("authorization input did not return bytes")
        return raw

    raw = bytearray()
    while len(raw) <= _AUTHORIZATION_MAX_BYTES:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise TypeError("authorization input did not return text")
        if character == "":
            break
        raw.extend(character.encode("utf-8"))
    return bytes(raw)


def _read_authorization(
    request: _RegisterRequest | _ActivationRequest,
    *,
    require_canonical: bool,
) -> OperatorAuthorization:
    try:
        raw_bytes = _read_authorization_bytes()
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
        raise AuthorizationError("authorization input cannot be read") from exc
    if len(raw_bytes) > _AUTHORIZATION_MAX_BYTES:
        raise AuthorizationError("authorization input exceeds the byte limit")
    try:
        raw = raw_bytes.decode("utf-8")
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise AuthorizationError("authorization input is not valid JSON") from exc
    required = {"action", "issued_at", "nonce", "operator_id", "reason"}
    if not isinstance(value, dict) or set(value) != required:
        raise AuthorizationError("authorization input has an invalid field set")
    if not all(isinstance(value[field], str) for field in required):
        raise AuthorizationError("authorization fields must be strings")
    authorization = cast(dict[str, str], value)
    if authorization["action"] != request.action.value:
        raise AuthorizationError("authorization action does not match requested intent")
    result = OperatorAuthorization(
        action=request.action,
        operator_id=authorization["operator_id"],
        reason=authorization["reason"],
        issued_at=authorization["issued_at"],
        nonce=authorization["nonce"],
    )
    try:
        canonical = canonical_json_bytes(result.to_dict())
    except ContractError as exc:
        raise AuthorizationError(
            "authorization fields are not canonically encodable"
        ) from exc
    if require_canonical and raw_bytes != canonical:
        raise AuthorizationError("authorization input is not canonical")
    return result


def _read_operator_authorization(request: _RegisterRequest) -> OperatorAuthorization:
    return _read_authorization(request, require_canonical=False)


def _read_activation_authorization(request: _ActivationRequest) -> OperatorAuthorization:
    return _read_authorization(request, require_canonical=True)


def _validate_registration_source(source: SourceIdentity) -> None:
    try:
        canonical = canonical_registry_source(source.registry_source)
    except ContractError as exc:
        raise SourceDiscoveryError(
            "source identity is not registry-compatible"
        ) from exc
    if canonical != source.registry_source:
        raise SourceDiscoveryError(
            "source filesystem identity is not canonically normalized"
        )
    aliases = cast(
        list[dict[str, str]],
        source.registry_source["remote_aliases"],
    )
    alias_evidence = {
        alias["evidence_sha256"]: alias["url"]
        for alias in aliases
    }
    discovered_evidence: dict[str, str] = {}
    try:
        for item in source.remote_evidence:
            digest = canonical_sha256(item)
            if digest in discovered_evidence:
                raise ContractError("duplicate remote evidence")
            discovered_evidence[digest] = item["url"]
    except (ContractError, KeyError, TypeError) as exc:
        raise SourceDiscoveryError(
            "source remote evidence is not registry-compatible"
        ) from exc
    if (
        source.source_sha256 != canonical_sha256(source.registry_source)
        or len(alias_evidence) != len(aliases)
        or discovered_evidence != alias_evidence
    ):
        raise SourceDiscoveryError("source evidence does not match its registry record")


def _discover_revalidated_source(
    repo_uuid: str,
    registry: RegistryStore,
) -> SourceIdentity:
    source_deadline_ns = time.monotonic_ns() + _REGISTRATION_SOURCE_TIMEOUT_NS
    source_root = Path.cwd()
    root_identity = source_root_identity(
        source_root,
        deadline_ns=source_deadline_ns,
    )
    source = discover_source(
        source_root,
        deadline_ns=source_deadline_ns,
        max_bytes=_REGISTRATION_CONFIG_MAX_BYTES,
    )
    _validate_registration_source(source)
    if source.repo_uuid != repo_uuid:
        raise UUIDCollisionError(
            "explicit workspace UUID does not match the source configuration"
        )
    refreshed_source = discover_source(
        source_root,
        deadline_ns=source_deadline_ns,
        max_bytes=_REGISTRATION_CONFIG_MAX_BYTES,
    )
    _validate_registration_source(refreshed_source)
    if refreshed_source != source:
        raise SourceDiscoveryError("source identity changed during workspace operation")
    git_common_dir = source.root / cast(
        str,
        source.registry_source["git_common_dir"],
    )
    registry.state.assert_external_to(git_common_dir)
    verify_source_checkout(
        source.root,
        expected_git_common_dir=git_common_dir,
        expected_worktree_id=cast(str, source.registry_source["worktree_id"]),
        expected_git_common_device=source.git_common_device,
        expected_git_common_inode=source.git_common_inode,
        expected_root_identity=root_identity,
        expected_head_commit=source.head_commit,
        deadline_ns=source_deadline_ns,
    )
    return source


def _registration_bytes(value: dict[str, object]) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _registration_success(
    request: _RegisterRequest,
    *,
    registry_revision: int,
) -> str:
    return _registration_bytes(
        {
            "action": request.action.value.lower(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": _REGISTRATION_CONTRACT,
            "exit_code": EXIT_READY,
            "registry_revision": registry_revision,
            "repo_uuid": request.repo_uuid,
            "schema_version": _REGISTRATION_SCHEMA_VERSION,
            "state": "registered",
        }
    )


def _registration_failure(
    request: _RegisterRequest,
    failure: _RegistrationFailure,
) -> str:
    receipt: dict[str, object] = {
        "action": request.action.value.lower(),
        "action_code": failure.action_code,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "contract": _REGISTRATION_CONTRACT,
        "exit_code": failure.exit_code,
        "reason_code": failure.reason_code,
        "schema_version": _REGISTRATION_SCHEMA_VERSION,
        "state": failure.state,
    }
    if failure.registry_revision is not None:
        receipt["registry_revision"] = failure.registry_revision
    return _registration_bytes(receipt)


def _identity_maintenance_success(
    request: _RegisterRequest,
    *,
    registry_revision: int,
) -> str:
    return canonical_json_bytes(
        {
            "action": request.action.value.lower(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": _IDENTITY_MAINTENANCE_CONTRACT,
            "exit_code": EXIT_READY,
            "registry_revision": registry_revision,
            "repo_uuid": request.repo_uuid,
            "schema_version": _IDENTITY_MAINTENANCE_SCHEMA_VERSION,
            "state": "maintained",
        }
    ).decode("utf-8")


def _identity_maintenance_failure(
    request: _RegisterRequest,
    failure: _IdentityMaintenanceFailure,
) -> str:
    receipt: dict[str, object] = {
        "action": request.action.value.lower(),
        "action_code": failure.action_code,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "contract": _IDENTITY_MAINTENANCE_CONTRACT,
        "exit_code": failure.exit_code,
        "reason_code": failure.reason_code,
        "schema_version": _IDENTITY_MAINTENANCE_SCHEMA_VERSION,
        "state": failure.state,
    }
    if failure.registry_revision is not None:
        receipt["registry_revision"] = failure.registry_revision
    return canonical_json_bytes(receipt).decode("utf-8")


def _activation_success(
    request: _ActivationRequest,
    result: ActivationResult,
) -> str:
    try:
        document = result.registry.to_dict()
        registry_revision = document["revision"]
        matches = [
            entry
            for entry in document["workspaces"]
            if entry["repo_uuid"] == request.repo_uuid
        ]
        if len(matches) != 1:
            raise ValueError("activation result has no singular workspace entry")
        active_source_revision = matches[0]["active_source_revision"]
        operation_epoch = result.grant.operation_epoch
        migration_epoch = result.grant.migration_epoch
        values = (
            registry_revision,
            active_source_revision,
            operation_epoch,
            migration_epoch,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("activation result revisions are invalid")
        if (
            registry_revision != request.expected_registry_revision + 1
            or active_source_revision != request.expected_active_source_revision + 1
            or operation_epoch != request.expected_operation_epoch + 1
            or migration_epoch != request.expected_migration_epoch
        ):
            raise ValueError("activation result does not match requested CAS transition")
        return canonical_json_bytes(
            {
                "action": "activate",
                "active_source_revision": active_source_revision,
                "cli_contract_version": CLI_CONTRACT_VERSION,
                "contract": _ACTIVATION_CONTRACT,
                "exit_code": EXIT_READY,
                "migration_epoch": migration_epoch,
                "operation_epoch": operation_epoch,
                "registry_revision": registry_revision,
                "repo_uuid": request.repo_uuid,
                "schema_version": _ACTIVATION_SCHEMA_VERSION,
                "state": "activated",
            }
        ).decode("utf-8")
    except InjectedFault:
        raise
    except Exception as exc:
        raise CommitUnknown(
            "activation completed without a valid canonical receipt"
        ) from exc


def _activation_failure(
    failure: _ActivationFailure,
) -> str:
    receipt: dict[str, object] = {
        "action": "activate",
        "action_code": failure.action_code,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "contract": _ACTIVATION_CONTRACT,
        "exit_code": failure.exit_code,
        "reason_code": failure.reason_code,
        "schema_version": _ACTIVATION_SCHEMA_VERSION,
        "state": failure.state,
    }
    if failure.registry_revision is not None:
        receipt["registry_revision"] = failure.registry_revision
    return canonical_json_bytes(receipt).decode("utf-8")


def _silence_standard_streams_after_broken_pipe(
    stream: TextIO,
    error: OSError,
) -> bool:
    if (
        (stream is not sys.stdout and stream is not sys.stderr)
        or error.errno not in {errno.EPIPE, errno.EINVAL}
    ):
        return False
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        for standard_stream in (sys.stdout, sys.stderr):
            os.dup2(devnull, standard_stream.fileno())
    finally:
        os.close(devnull)
    return True


def _emit_text_payload(stream: TextIO, payload: str, *, exit_code: int) -> int:
    try:
        stream.write(payload)
    except (BrokenPipeError, OSError) as exc:
        if not _silence_standard_streams_after_broken_pipe(stream, exc):
            raise
    return exit_code


def _emit_query_output(
    stream: TextIO,
    text: str,
    payload: bytes,
    *,
    exit_code: int,
) -> int:
    """Write certified UTF-8 bytes when the output stream exposes its buffer."""

    binary_stream = getattr(stream, "buffer", None)
    if binary_stream is None:
        return _emit_text_payload(stream, text, exit_code=exit_code)
    try:
        written = binary_stream.write(payload)
        if written != len(payload):
            raise OSError(errno.EIO, "incomplete workspace query output")
    except (BrokenPipeError, OSError) as exc:
        if not _silence_standard_streams_after_broken_pipe(stream, exc):
            raise
    return exit_code


def _emit_rollback_output(
    stream: TextIO,
    payload: bytes,
    *,
    exit_code: int,
) -> int:
    """Write one canonical rollback receipt as exact UTF-8 bytes."""

    binary_stream = getattr(stream, "buffer", None)
    if binary_stream is None:
        return _emit_text_payload(
            stream,
            payload.decode("utf-8"),
            exit_code=exit_code,
        )
    try:
        written = binary_stream.write(payload)
        if written != len(payload):
            raise OSError(errno.EIO, "incomplete workspace rollback receipt")
        binary_stream.flush()
    except (BrokenPipeError, OSError) as exc:
        if not _silence_standard_streams_after_broken_pipe(stream, exc):
            raise
    return exit_code


def _emit_canonical_result(
    stream: TextIO,
    payload: bytes,
    *,
    exit_code: int,
    result_label: str,
) -> int:
    """Write one canonical workspace result as exact UTF-8 bytes."""

    binary_stream = getattr(stream, "buffer", None)
    if binary_stream is None:
        return _emit_text_payload(
            stream,
            payload.decode("utf-8"),
            exit_code=exit_code,
        )
    try:
        written = binary_stream.write(payload)
        if written != len(payload):
            raise OSError(errno.EIO, f"incomplete workspace {result_label}")
        binary_stream.flush()
    except (BrokenPipeError, OSError) as exc:
        if not _silence_standard_streams_after_broken_pipe(stream, exc):
            raise
    return exit_code


def _classify_registration_error(error: Exception) -> _RegistrationFailure:
    if isinstance(error, RevisionConflict):
        action_code = (
            "refresh_registry_revision"
            if error.actual_registry_revision is not None
            else "run_workspace_doctor"
        )
        return _RegistrationFailure(
            "conflict",
            EXIT_DEGRADED,
            "revision_conflict",
            action_code,
            error.actual_registry_revision,
        )
    if isinstance(error, UUIDCollisionError):
        return _RegistrationFailure(
            "conflict",
            EXIT_DEGRADED,
            "uuid_collision",
            "verify_registration_identity",
        )
    if isinstance(error, WorkspaceAuthorityError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, AuthorizationError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "authorization_invalid",
            "provide_valid_authorization",
        )
    if isinstance(error, SourceDiscoveryTimeout):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            error.code,
            "retry_registration",
        )
    if isinstance(error, IdentityError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            error.code,
            "fix_workspace_source",
        )
    if isinstance(error, StatePathError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, StateCorrupt):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "inspect_workspace_state",
        )
    if isinstance(error, CommitUnknown):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            "run_workspace_doctor",
        )
    return _RegistrationFailure(
        "invalid",
        EXIT_INVALID,
        "registration_failed",
        "run_workspace_doctor",
    )


def _classify_identity_maintenance_error(
    error: Exception,
) -> _IdentityMaintenanceFailure:
    if isinstance(error, UUIDCollisionError):
        return _IdentityMaintenanceFailure(
            "conflict",
            EXIT_DEGRADED,
            "identity_mismatch",
            "verify_identity_maintenance_target",
        )
    if isinstance(error, SourceAmbiguousError):
        return _IdentityMaintenanceFailure(
            "conflict",
            EXIT_DEGRADED,
            "source_not_bound",
            "enroll_or_adopt_source",
        )
    registration_failure = _classify_registration_error(error)
    reason_code = registration_failure.reason_code
    action_code = registration_failure.action_code
    if reason_code == "registration_failed":
        reason_code = "identity_maintenance_failed"
    if action_code == "retry_registration":
        action_code = "retry_identity_maintenance"
    return _IdentityMaintenanceFailure(
        registration_failure.state,
        registration_failure.exit_code,
        reason_code,
        action_code,
        registration_failure.registry_revision,
    )


def _classify_activation_error(error: Exception) -> _ActivationFailure:
    if isinstance(error, RevisionConflict):
        if error.actual_registry_revision is not None:
            return _ActivationFailure(
                "conflict",
                EXIT_DEGRADED,
                "registry_revision_conflict",
                "refresh_activation_cas",
                error.actual_registry_revision,
            )
        return _ActivationFailure(
            "conflict",
            EXIT_DEGRADED,
            "activation_cas_conflict",
            "refresh_activation_cas",
        )
    if isinstance(error, SourceAlreadyActive):
        return _ActivationFailure(
            "conflict",
            EXIT_DEGRADED,
            "source_already_active",
            "select_inactive_source",
        )
    if isinstance(error, UUIDCollisionError):
        return _ActivationFailure(
            "conflict",
            EXIT_DEGRADED,
            "identity_mismatch",
            "verify_activation_target",
        )
    if isinstance(error, SourceAmbiguousError):
        return _ActivationFailure(
            "conflict",
            EXIT_DEGRADED,
            "source_not_bound",
            "bind_activation_source",
        )
    if isinstance(error, LeaseBusy):
        return _ActivationFailure(
            "conflict",
            EXIT_DEGRADED,
            "lease_busy",
            "retry_activation",
        )
    if isinstance(error, LeaseRecoveryRequired):
        return _ActivationFailure(
            "conflict",
            EXIT_DEGRADED,
            "workspace_recovery_required",
            "run_workspace_doctor",
        )
    if isinstance(error, WorkspaceAuthorityError):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, AuthorizationError):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            "authorization_invalid",
            "provide_valid_authorization",
        )
    if isinstance(error, SourceDiscoveryTimeout):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            error.code,
            "retry_activation",
        )
    if isinstance(error, IdentityError):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            error.code,
            "fix_workspace_source",
        )
    if isinstance(error, StatePathError):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, StateCorrupt):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_doctor",
        )
    if isinstance(error, CommitUnknown):
        return _ActivationFailure(
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            "run_workspace_doctor",
        )
    return _ActivationFailure(
        "invalid",
        EXIT_INVALID,
        "activation_failed",
        "run_workspace_doctor",
    )


def _activation_timestamp() -> datetime:
    return datetime.now(timezone.utc)


def _activation_monotonic_ns() -> int:
    return time.monotonic_ns()


def _run_registration(
    request: _RegisterRequest,
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    if request.action not in {
        IdentityAction.ENROLL,
        IdentityAction.ADOPT,
        IdentityAction.REBIND,
        IdentityAction.ROTATE,
    }:
        raise AssertionError(f"unsupported register action: {request.action.value}")
    identity_maintenance = request.action in {
        IdentityAction.REBIND,
        IdentityAction.ROTATE,
    }
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            if identity_maintenance:
                maintenance_failure = _IdentityMaintenanceFailure(
                    "invalid",
                    EXIT_INVALID,
                    "runtime_authority_missing",
                    "install_candidate_authority",
                )
                receipt = _identity_maintenance_failure(request, maintenance_failure)
                exit_code = maintenance_failure.exit_code
            else:
                registration_failure = _RegistrationFailure(
                    "invalid",
                    EXIT_INVALID,
                    "runtime_authority_missing",
                    "install_candidate_authority",
                )
                receipt = _registration_failure(request, registration_failure)
                exit_code = registration_failure.exit_code
        else:
            runtime = compose_workspace_runtime(resolved_inputs)
            authorization = _read_operator_authorization(request)
            source = _discover_revalidated_source(request.repo_uuid, runtime.registry)
            if request.action is IdentityAction.ENROLL:
                document = runtime.registry.enroll(
                    source,
                    authorization,
                    expected_revision=request.expected_registry_revision,
                )
            elif request.action is IdentityAction.ADOPT:
                document = runtime.registry.adopt(
                    source,
                    authorization,
                    expected_revision=request.expected_registry_revision,
                )
            elif request.action is IdentityAction.REBIND:
                document = runtime.registry.rebind(
                    source,
                    authorization,
                    expected_revision=request.expected_registry_revision,
                )
            elif request.action is IdentityAction.ROTATE:
                document = runtime.registry.rotate_enrollment_evidence(
                    source,
                    authorization,
                    expected_revision=request.expected_registry_revision,
                )
            else:
                raise AssertionError(
                    f"unsupported register action: {request.action.value}"
                )
            try:
                revision = int(document.to_dict()["revision"])
            except InjectedFault:
                raise
            except Exception as exc:
                raise CommitUnknown(
                    (
                        "identity maintenance completed without a valid revision receipt"
                        if identity_maintenance
                        else "registration completed without a valid revision receipt"
                    )
                ) from exc
            receipt = (
                _identity_maintenance_success(request, registry_revision=revision)
                if identity_maintenance
                else _registration_success(request, registry_revision=revision)
            )
            exit_code = EXIT_READY
    except InjectedFault:
        raise
    except Exception as exc:
        if identity_maintenance:
            maintenance_failure = _classify_identity_maintenance_error(exc)
            receipt = _identity_maintenance_failure(request, maintenance_failure)
            exit_code = maintenance_failure.exit_code
        else:
            registration_failure = _classify_registration_error(exc)
            receipt = _registration_failure(request, registration_failure)
            exit_code = registration_failure.exit_code
    stream = output if exit_code == EXIT_READY else errors
    return _emit_text_payload(stream, receipt, exit_code=exit_code)


def _run_activation(
    request: _ActivationRequest,
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = _ActivationFailure(
                "invalid",
                EXIT_INVALID,
                "runtime_authority_missing",
                "install_candidate_authority",
            )
            receipt = _activation_failure(failure)
            exit_code = failure.exit_code
        else:
            runtime = compose_workspace_runtime(resolved_inputs)
            authorization = _read_activation_authorization(request)
            source = _discover_revalidated_source(request.repo_uuid, runtime.registry)
            result = runtime.registry.activate_source(
                source,
                authorization,
                leases=runtime.leases,
                owner=runtime.leases.current_owner(),
                expected_registry_revision=request.expected_registry_revision,
                expected_active_source_revision=request.expected_active_source_revision,
                expected_operation_epoch=request.expected_operation_epoch,
                expected_migration_epoch=request.expected_migration_epoch,
                acquired_at=_activation_timestamp(),
                monotonic_ns=_activation_monotonic_ns(),
                ttl_ns=_ACTIVATION_LEASE_TTL_NS,
                require_source_change=True,
            )
            receipt = _activation_success(request, result)
            exit_code = EXIT_READY
    except InjectedFault:
        raise
    except Exception as exc:
        failure = _classify_activation_error(exc)
        receipt = _activation_failure(failure)
        exit_code = failure.exit_code
    stream = output if exit_code == EXIT_READY else errors
    return _emit_text_payload(stream, receipt, exit_code=exit_code)


def _read_sync_request_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    if binary_input is not None:
        raw = binary_input.read(SYNC_REQUEST_MAX_BYTES + 1)
        if not isinstance(raw, bytes):
            raise SyncRequestInvalid("sync request input did not return bytes")
        return raw

    raw = bytearray()
    while len(raw) <= SYNC_REQUEST_MAX_BYTES:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise SyncRequestInvalid("sync request input did not return text")
        if character == "":
            break
        try:
            raw.extend(character.encode("utf-8"))
        except UnicodeError as exc:
            raise SyncRequestInvalid("sync request input is not UTF-8") from exc
    return bytes(raw)


def _sync_failure_receipt(failure: _SyncFailure) -> str:
    return canonical_json_bytes(
        {
            "action_code": failure.action_code,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": SYNC_RECEIPT_CONTRACT,
            "exit_code": failure.exit_code,
            "mode": SYNC_MODE,
            "reason_code": failure.reason_code,
            "schema_version": SYNC_SCHEMA_VERSION,
            "state": failure.state,
        }
    ).decode("utf-8")


def _classify_sync_error(error: Exception) -> _SyncFailure:
    if isinstance(error, WorkspaceSyncError):
        return _SyncFailure(
            error.state,
            error.exit_code,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, WorkspaceAuthorityError):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, LeaseBusy):
        return _SyncFailure(
            "conflict",
            EXIT_DEGRADED,
            "lease_busy",
            "retry_workspace_sync",
        )
    if isinstance(error, (LeaseExpired, StaleLease)):
        return _SyncFailure(
            "conflict",
            EXIT_DEGRADED,
            "staged_build_recovery_required",
            "resume_exact_workspace_sync",
        )
    if isinstance(error, CapacityExceeded):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "capacity_exceeded",
            "adjust_capacity_policy",
        )
    if isinstance(error, CommitUnknown):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            "resume_exact_workspace_sync",
        )
    if isinstance(
        error,
        (
            RevisionConflict,
            GenerationConflict,
            PointerConflict,
            LeaseRecoveryRequired,
            IdentityError,
        ),
    ):
        return _SyncFailure(
            "conflict",
            EXIT_DEGRADED,
            "sync_authority_conflict",
            "refresh_sync_request",
        )
    if isinstance(error, StatePathError):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, StateCorrupt):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_repair",
        )
    if isinstance(error, SemanticQueueError):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "sync_failed",
            "run_workspace_doctor",
        )
    if isinstance(error, GenerationError):
        return _SyncFailure(
            "invalid",
            EXIT_INVALID,
            "sync_failed",
            "run_workspace_doctor",
        )
    return _SyncFailure(
        "invalid",
        EXIT_INVALID,
        "sync_failed",
        "run_workspace_doctor",
    )


def _run_sync(
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = _SyncFailure(
                "invalid",
                EXIT_INVALID,
                "runtime_authority_missing",
                "install_candidate_authority",
            )
            payload = _sync_failure_receipt(failure)
            exit_code = failure.exit_code
        else:
            try:
                raw_request = _read_sync_request_bytes()
            except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
                raise SyncRequestInvalid("sync request input cannot be read") from exc
            request = SyncRequest.from_json(raw_request)
            runtime = compose_workspace_runtime(resolved_inputs)
            with (
                redirect_stdout(_DISCARDED_ENGINE_OUTPUT),
                redirect_stderr(_DISCARDED_ENGINE_OUTPUT),
            ):
                receipt: SyncReceipt = synchronize_code_only(runtime, request)
            payload = receipt.canonical.decode("utf-8")
            exit_code = EXIT_READY
    except InjectedFault:
        raise
    except Exception as exc:
        failure = _classify_sync_error(exc)
        payload = _sync_failure_receipt(failure)
        exit_code = failure.exit_code
    stream = output if exit_code == EXIT_READY else errors
    return _emit_text_payload(stream, payload, exit_code=exit_code)


def _read_rollback_request_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    if binary_input is not None:
        limit = rollback_runtime.ROLLBACK_REQUEST_MAX_BYTES + 1
        raw = bytearray()
        while len(raw) < limit:
            remaining = limit - len(raw)
            chunk = binary_input.read(remaining)
            if not isinstance(chunk, bytes):
                raise ValueError("rollback request input did not return bytes")
            if len(chunk) > remaining:
                raise ValueError("rollback request input exceeded its bounded read")
            if chunk == b"":
                break
            raw.extend(chunk)
        return bytes(raw)

    raw = bytearray()
    while len(raw) <= rollback_runtime.ROLLBACK_REQUEST_MAX_BYTES:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise ValueError("rollback request input did not return text")
        if character == "":
            break
        try:
            raw.extend(character.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("rollback request input is not UTF-8") from exc
    return bytes(raw)


def _run_rollback(
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = rollback_runtime.RollbackFailure.missing_authority()
            return _emit_rollback_output(
                errors,
                failure.canonical,
                exit_code=failure.exit_code,
            )
        runtime = compose_workspace_runtime(resolved_inputs)
    except InjectedFault:
        raise
    except Exception as exc:
        failure = rollback_runtime.classify_failure(exc)
        return _emit_rollback_output(
            errors,
            failure.canonical,
            exit_code=failure.exit_code,
        )

    try:
        try:
            raw_request = _read_rollback_request_bytes()
        except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError("rollback request input cannot be read") from exc
        request = rollback_runtime.RollbackRequest.from_bytes(raw_request)
        occurred_at = datetime.now(timezone.utc)
        with (
            redirect_stdout(_DISCARDED_ENGINE_OUTPUT),
            redirect_stderr(_DISCARDED_ENGINE_OUTPUT),
        ):
            receipt = rollback_runtime.rollback(
                runtime,
                request,
                occurred_at=occurred_at,
                monotonic_clock=time.monotonic_ns,
            )
    except InjectedFault:
        raise
    except Exception as exc:
        failure = rollback_runtime.classify_failure(exc)
        return _emit_rollback_output(
            errors,
            failure.canonical,
            exit_code=failure.exit_code,
        )
    return _emit_rollback_output(
        output,
        receipt.canonical,
        exit_code=EXIT_READY,
    )


def _read_query_request_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    if binary_input is not None:
        raw = binary_input.read(_QUERY_REQUEST_MAX_BYTES + 1)
        if not isinstance(raw, bytes):
            raise _QueryRequestInvalid("query request input did not return bytes")
        return raw

    raw = bytearray()
    while len(raw) <= _QUERY_REQUEST_MAX_BYTES:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise _QueryRequestInvalid("query request input did not return text")
        if character == "":
            break
        try:
            raw.extend(character.encode("utf-8"))
        except UnicodeError as exc:
            raise _QueryRequestInvalid("query request input is not UTF-8") from exc
    return bytes(raw)


def _query_request_from_mapping(value: Mapping[str, object]) -> _QueryCliRequest:
    if set(value) != _QUERY_REQUEST_FIELDS:
        raise _QueryRequestInvalid("query request fields are invalid")

    contract = value.get("contract")
    if not isinstance(contract, str):
        raise _QueryRequestInvalid("query request contract is invalid")
    if contract != _QUERY_REQUEST_CONTRACT:
        raise _QueryRequestUnsupported("query request contract is unsupported")

    for field, expected in (
        ("schema_version", _QUERY_SCHEMA_VERSION),
        ("cli_contract_version", CLI_CONTRACT_VERSION),
    ):
        version = value.get(field)
        if type(version) is not int:
            raise _QueryRequestInvalid(f"query request {field} is invalid")
        if version != expected:
            raise _QueryRequestUnsupported(f"query request {field} is unsupported")

    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
    except ContractError as exc:
        raise _QueryRequestInvalid("query request repo UUID is invalid") from exc

    timeout_ms = value.get("timeout_ms")
    if (
        type(timeout_ms) is not int
        or timeout_ms < 1
        or timeout_ms > _QUERY_TIMEOUT_MAX_MS
    ):
        raise _QueryRequestInvalid("query request timeout is invalid")

    try:
        query = QueryRequest(
            question=cast(Any, value.get("question")),
            mode=cast(Any, value.get("mode")),
            depth=cast(Any, value.get("depth")),
            token_budget=cast(Any, value.get("token_budget")),
            context_filters=cast(Any, value.get("context_filters")),
        )
    except QueryRejected as exc:
        raise _QueryRequestInvalid("query request payload is invalid") from exc
    return _QueryCliRequest(
        repo_uuid=repo_uuid,
        query=query,
        timeout_ms=timeout_ms,
    )


def _parse_query_request(raw: bytes) -> _QueryCliRequest:
    if len(raw) > _QUERY_REQUEST_MAX_BYTES:
        raise _QueryRequestInvalid("query request exceeds the byte limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise _QueryRequestInvalid("query request is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise _QueryRequestInvalid("query request must be an object")
    request = _query_request_from_mapping(value)
    try:
        canonical = canonical_json_bytes(request.to_dict())
    except ContractError as exc:
        raise _QueryRequestInvalid("query request is not canonically encodable") from exc
    if canonical != raw:
        raise _QueryRequestInvalid("query request is not canonical")
    return request


def _query_result_payload(
    *,
    state: str,
    decision: str,
    exit_code: int,
    reason_code: str,
    query_executed: bool,
    observation_boundary: str,
    action_code: str | None = None,
    repo_uuid: str | None = None,
    output_bytes: bytes | None = None,
) -> str:
    value: dict[str, object] = {
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "contract": _QUERY_RESULT_CONTRACT,
        "decision": decision,
        "exit_code": exit_code,
        "observation_boundary": observation_boundary,
        "query_executed": query_executed,
        "reason_code": reason_code,
        "schema_version": _QUERY_SCHEMA_VERSION,
        "state": state,
    }
    if action_code is not None:
        value["action_code"] = action_code
    if repo_uuid is not None and output_bytes is not None:
        value["repo_uuid"] = repo_uuid
        value["output"] = {
            "bytes": len(output_bytes),
            "encoding": "utf-8",
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
            "stream": "stdout",
        }
    return canonical_json_bytes(value).decode("utf-8")


def _classify_query_error(error: Exception) -> _QueryFailure:
    if isinstance(error, _QueryRequestUnsupported):
        return _QueryFailure(
            "unsupported",
            EXIT_INVALID,
            "query_request_unsupported",
            "use_supported_query_contract",
        )
    if isinstance(error, (_QueryRequestInvalid, QueryRejected)):
        return _QueryFailure(
            "invalid",
            EXIT_INVALID,
            "query_request_invalid",
            "provide_valid_query_request",
        )
    if isinstance(error, WorkspaceAuthorityError):
        state = (
            "unsupported"
            if error.reason_code == "runtime_authority_unsupported"
            else "invalid"
        )
        return _QueryFailure(
            state,
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, StatePathError):
        return _QueryFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return _QueryFailure(
            "unsupported",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return _QueryFailure(
            "unsupported",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, StateCorrupt):
        return _QueryFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_repair",
        )
    return _QueryFailure(
        "invalid",
        EXIT_INVALID,
        "query_failed",
        "run_workspace_doctor",
    )


def _query_failure_payload(failure: _QueryFailure) -> str:
    return _query_result_payload(
        state=failure.state,
        decision="withhold",
        exit_code=failure.exit_code,
        reason_code=failure.reason_code,
        action_code=failure.action_code,
        query_executed=failure.query_executed,
        observation_boundary=failure.observation_boundary,
    )


def _emit_query_failure(errors: TextIO, failure: _QueryFailure) -> int:
    return _emit_text_payload(
        errors,
        _query_failure_payload(failure),
        exit_code=failure.exit_code,
    )


def _run_query(
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = _QueryFailure(
                "invalid",
                EXIT_INVALID,
                "runtime_authority_missing",
                "install_candidate_authority",
            )
            return _emit_query_failure(errors, failure)
        runtime = compose_workspace_runtime(resolved_inputs)
    except InjectedFault:
        raise
    except Exception as exc:
        failure = _classify_query_error(exc)
        return _emit_query_failure(errors, failure)

    try:
        raw_request = _read_query_request_bytes()
        request = _parse_query_request(raw_request)
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
        failure = _classify_query_error(
            exc
            if isinstance(exc, _QueryRequestInvalid)
            else _QueryRequestInvalid("query request input cannot be read")
        )
        return _emit_query_failure(errors, failure)

    try:
        with (
            redirect_stdout(_DISCARDED_ENGINE_OUTPUT),
            redirect_stderr(_DISCARDED_ENGINE_OUTPUT),
        ):
            result = runtime.freshness.query(
                request.repo_uuid,
                request.query,
                timeout_ns=request.timeout_ms * 1_000_000,
            )
    except InjectedFault:
        raise
    except Exception as exc:
        failure = _classify_query_error(exc)
        return _emit_query_failure(errors, failure)

    if not isinstance(result, FreshnessResult):
        failure = _QueryFailure(
            "invalid",
            EXIT_INVALID,
            "query_result_invalid",
            "run_workspace_doctor",
        )
        return _emit_query_failure(errors, failure)

    observation_boundary = result.observation_boundary
    if (
        type(result.query_executed) is not bool
        or not isinstance(result.decision, str)
        or not isinstance(result.reason, str)
        or not isinstance(observation_boundary, str)
        or observation_boundary not in {"not_observed", "pre_observed", "two_sided"}
    ):
        failure = _QueryFailure(
            "invalid",
            EXIT_INVALID,
            "query_result_invalid",
            "run_workspace_doctor",
        )
    elif result.decision == "release" and result.reason == "observed_current":
        if (
            not result.query_executed
            or observation_boundary != "two_sided"
            or not isinstance(result.output, str)
        ):
            failure = _QueryFailure(
                "invalid",
                EXIT_INVALID,
                "query_result_invalid",
                "run_workspace_doctor",
                query_executed=result.query_executed,
                observation_boundary=observation_boundary,
            )
        else:
            try:
                native_bytes = result.output.encode("utf-8")
            except UnicodeError:
                failure = _QueryFailure(
                    "invalid",
                    EXIT_INVALID,
                    "query_result_invalid",
                    "run_workspace_doctor",
                    query_executed=result.query_executed,
                    observation_boundary=observation_boundary,
                )
            else:
                control = _query_result_payload(
                    state="released",
                    decision="release",
                    exit_code=EXIT_READY,
                    reason_code="observed_current",
                    query_executed=True,
                    observation_boundary="two_sided",
                    repo_uuid=request.repo_uuid,
                    output_bytes=native_bytes,
                )
                _emit_query_output(
                    output,
                    result.output,
                    native_bytes,
                    exit_code=EXIT_READY,
                )
                return _emit_text_payload(
                    errors,
                    control,
                    exit_code=EXIT_READY,
                )
    elif result.output is not None or result.decision != "withhold":
        failure = _QueryFailure(
            "invalid",
            EXIT_INVALID,
            "query_result_invalid",
            "run_workspace_doctor",
            query_executed=result.query_executed,
            observation_boundary=observation_boundary,
        )
    else:
        mapped = {
            "drift": ("drifted", EXIT_DEGRADED, "sync_workspace"),
            "source_unavailable": (
                "withheld",
                EXIT_DEGRADED,
                "restore_workspace_source",
            ),
            "timeout": ("timed_out", EXIT_DEGRADED, "retry_workspace_query"),
            "unstable": ("withheld", EXIT_DEGRADED, "retry_workspace_query"),
            "unsupported": (
                "unsupported",
                EXIT_INVALID,
                "run_workspace_doctor",
            ),
        }.get(result.reason)
        if mapped is None:
            failure = _QueryFailure(
                "invalid",
                EXIT_INVALID,
                "query_result_invalid",
                "run_workspace_doctor",
                query_executed=result.query_executed,
                observation_boundary=observation_boundary,
            )
        else:
            state, exit_code, action_code = mapped
            failure = _QueryFailure(
                state,
                exit_code,
                "query_unsupported" if result.reason == "unsupported" else result.reason,
                action_code,
                query_executed=result.query_executed,
                observation_boundary=observation_boundary,
            )

    return _emit_query_failure(errors, failure)


def _read_gc_preview_request_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    if binary_input is not None:
        limit = _GC_PREVIEW_REQUEST_MAX_BYTES + 1
        raw = bytearray()
        while len(raw) < limit:
            remaining = limit - len(raw)
            chunk = binary_input.read(remaining)
            if not isinstance(chunk, bytes):
                raise _GcPreviewRequestInvalid(
                    "GC preview request input did not return bytes"
                )
            if len(chunk) > remaining:
                raise _GcPreviewRequestInvalid(
                    "GC preview request input exceeded its bounded read"
                )
            if chunk == b"":
                break
            raw.extend(chunk)
        return bytes(raw)

    raw = bytearray()
    while len(raw) <= _GC_PREVIEW_REQUEST_MAX_BYTES:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise _GcPreviewRequestInvalid(
                "GC preview request input did not return text"
            )
        if character == "":
            break
        try:
            raw.extend(character.encode("utf-8"))
        except UnicodeError as exc:
            raise _GcPreviewRequestInvalid(
                "GC preview request input is not UTF-8"
            ) from exc
    return bytes(raw)


def _gc_preview_integer(
    value: object,
    field: str,
    *,
    minimum: int,
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or value > _MAX_SIGNED_REVISION
    ):
        raise _GcPreviewRequestInvalid(f"GC preview request {field} is invalid")
    return value


def _gc_protection_ids(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > GC_PREVIEW_MAX_GENERATIONS:
        raise _GcPreviewRequestInvalid(
            f"GC preview request protection {field} is invalid"
        )
    result: list[str] = []
    for generation_id in value:
        if not isinstance(generation_id, str) or _GC_GENERATION_RE.fullmatch(
            generation_id
        ) is None:
            raise _GcPreviewRequestInvalid(
                f"GC preview request protection {field} is invalid"
            )
        result.append(generation_id)
    if result != sorted(result) or len(result) != len(set(result)):
        raise _GcPreviewRequestInvalid(
            f"GC preview request protection {field} must be unique and sorted"
        )
    return frozenset(result)


def _gc_preview_request_from_mapping(
    value: Mapping[str, object],
) -> _GcPreviewRequest:
    if set(value) != _GC_PREVIEW_REQUEST_FIELDS:
        raise _GcPreviewRequestInvalid("GC preview request fields are invalid")

    contract = value.get("contract")
    if not isinstance(contract, str):
        raise _GcPreviewRequestInvalid("GC preview request contract is invalid")
    if contract != _GC_PREVIEW_REQUEST_CONTRACT:
        raise _GcPreviewRequestUnsupported(
            "GC preview request contract is unsupported"
        )
    for field, expected in (
        ("schema_version", _GC_PREVIEW_SCHEMA_VERSION),
        ("cli_contract_version", CLI_CONTRACT_VERSION),
    ):
        version = value.get(field)
        if type(version) is not int:
            raise _GcPreviewRequestInvalid(
                f"GC preview request {field} is invalid"
            )
        if version != expected:
            raise _GcPreviewRequestUnsupported(
                f"GC preview request {field} is unsupported"
            )

    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(value.get("repo_uuid"))
    except ContractError as exc:
        raise _GcPreviewRequestInvalid(
            "GC preview request repo UUID is invalid"
        ) from exc
    expected_registry_revision = _gc_preview_integer(
        value.get("expected_registry_revision"),
        "expected_registry_revision",
        minimum=1,
    )
    expected_active_source_revision = _gc_preview_integer(
        value.get("expected_active_source_revision"),
        "expected_active_source_revision",
        minimum=1,
    )
    expected_operation_epoch = _gc_preview_integer(
        value.get("expected_operation_epoch"),
        "expected_operation_epoch",
        minimum=0,
    )
    expected_migration_epoch = _gc_preview_integer(
        value.get("expected_migration_epoch"),
        "expected_migration_epoch",
        minimum=0,
    )
    expected_pointer_revision = _gc_preview_integer(
        value.get("expected_pointer_revision"),
        "expected_pointer_revision",
        minimum=0,
    )
    timeout_ms = _gc_preview_integer(
        value.get("timeout_ms"),
        "timeout_ms",
        minimum=1,
    )
    if timeout_ms > _GC_PREVIEW_TIMEOUT_MAX_MS:
        raise _GcPreviewRequestInvalid("GC preview request timeout_ms is invalid")

    capacity_value = value.get("capacity_policy")
    if not isinstance(capacity_value, Mapping):
        raise _GcPreviewRequestInvalid(
            "GC preview request capacity policy is invalid"
        )
    try:
        capacity_policy = CapacityPolicy.from_mapping(capacity_value)
    except (ContractError, TypeError, ValueError) as exc:
        raise _GcPreviewRequestInvalid(
            "GC preview request capacity policy is invalid"
        ) from exc
    for field in (
        "global_max_bytes",
        "global_max_generations",
        "reserve_bytes",
        "workspace_max_bytes",
        "workspace_max_generations",
    ):
        _gc_preview_integer(capacity_policy.to_dict()[field], field, minimum=1)

    protection_value = value.get("protections")
    if not isinstance(protection_value, Mapping) or set(protection_value) != set(
        _GC_PROTECTION_FIELDS
    ):
        raise _GcPreviewRequestInvalid(
            "GC preview request protection fields are invalid"
        )
    protections = {
        field: _gc_protection_ids(protection_value[field], field)
        for field in _GC_PROTECTION_FIELDS
    }
    protection_ids = {
        generation_id
        for generation_ids in protections.values()
        for generation_id in generation_ids
    }
    if len(protection_ids) > GC_PREVIEW_MAX_GENERATIONS:
        raise _GcPreviewRequestInvalid(
            "GC preview request protections exceed the public bound"
        )
    return _GcPreviewRequest(
        repo_uuid=repo_uuid,
        expected_registry_revision=expected_registry_revision,
        expected_active_source_revision=expected_active_source_revision,
        expected_operation_epoch=expected_operation_epoch,
        expected_migration_epoch=expected_migration_epoch,
        expected_pointer_revision=expected_pointer_revision,
        timeout_ms=timeout_ms,
        capacity_policy=capacity_policy,
        protections=GcProtection(
            migration_sources=protections["migration_sources"],
            rollback_sources=protections["rollback_sources"],
            active_lease_generations=protections["active_lease_generations"],
            fixture_generations=protections["fixture_generations"],
            proof_generations=protections["proof_generations"],
            rollback_artifact_generations=protections[
                "rollback_artifact_generations"
            ],
        ),
    )


def _parse_gc_preview_request(raw: bytes) -> _GcPreviewRequest:
    if len(raw) > _GC_PREVIEW_REQUEST_MAX_BYTES:
        raise _GcPreviewRequestInvalid(
            "GC preview request exceeds the byte limit"
        )
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise _GcPreviewRequestInvalid(
            "GC preview request is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise _GcPreviewRequestInvalid("GC preview request must be an object")
    request = _gc_preview_request_from_mapping(value)
    try:
        canonical = canonical_json_bytes(request.to_dict())
    except ContractError as exc:
        raise _GcPreviewRequestInvalid(
            "GC preview request is not canonically encodable"
        ) from exc
    if canonical != raw:
        raise _GcPreviewRequestInvalid("GC preview request is not canonical")
    return request


def _gc_preview_failure_payload(failure: _GcPreviewFailure) -> bytes:
    return canonical_json_bytes(
        {
            "action_code": failure.action_code,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": _GC_PREVIEW_RESULT_CONTRACT,
            "decision": "withhold",
            "exit_code": failure.exit_code,
            "observation_boundary": failure.observation_boundary,
            "reason_code": failure.reason_code,
            "schema_version": _GC_PREVIEW_SCHEMA_VERSION,
            "state": failure.state,
        }
    )


def _gc_preview_is_valid(
    result: GcPreview,
    request: _GcPreviewRequest,
) -> bool:
    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(result.repo_uuid)
    except ContractError:
        return False
    revisions = (
        (result.registry_revision, 1),
        (result.active_source_revision, 1),
        (result.operation_epoch, 0),
        (result.migration_epoch, 0),
        (result.pointer_revision, 0),
    )
    if repo_uuid != result.repo_uuid or any(
        type(value) is not int
        or value < minimum
        or value > _MAX_SIGNED_REVISION
        for value, minimum in revisions
    ):
        return False
    if (
        result.repo_uuid != request.repo_uuid
        or result.registry_revision != request.expected_registry_revision
        or result.active_source_revision != request.expected_active_source_revision
        or result.operation_epoch != request.expected_operation_epoch
        or result.migration_epoch != request.expected_migration_epoch
        or result.pointer_revision != request.expected_pointer_revision
        or result.capacity_policy_sha256 != request.capacity_policy.sha256
    ):
        return False
    if not isinstance(
        result.capacity_policy_sha256,
        str,
    ) or _DIGEST_RE.fullmatch(result.capacity_policy_sha256) is None:
        return False
    candidates = result.candidates
    if (
        not isinstance(candidates, tuple)
        or len(candidates) > GC_PREVIEW_MAX_GENERATIONS
        or list(candidates) != sorted(candidates)
        or len(candidates) != len(set(candidates))
        or any(
            not isinstance(generation_id, str)
            or _GC_GENERATION_RE.fullmatch(generation_id) is None
            for generation_id in candidates
        )
    ):
        return False
    protected = result.protected
    if not isinstance(protected, tuple) or len(protected) > GC_PREVIEW_MAX_GENERATIONS:
        return False
    protected_ids: list[str] = []
    for item in protected:
        if not isinstance(item, tuple) or len(item) != 2:
            return False
        generation_id, reasons = item
        if (
            not isinstance(generation_id, str)
            or _GC_GENERATION_RE.fullmatch(generation_id) is None
            or not isinstance(reasons, tuple)
            or not reasons
            or list(reasons) != sorted(reasons)
            or len(reasons) != len(set(reasons))
            or any(reason not in _GC_PROTECTION_REASONS for reason in reasons)
        ):
            return False
        protected_ids.append(generation_id)
    return (
        protected_ids == sorted(protected_ids)
        and len(protected_ids) == len(set(protected_ids))
        and not set(candidates).intersection(protected_ids)
        and len(set(candidates) | set(protected_ids)) <= GC_PREVIEW_MAX_GENERATIONS
    )


def _gc_preview_success_payload(result: GcPreview) -> bytes:
    return gc_command_runtime.gc_preview_result_bytes(result)


def _classify_gc_preview_error(error: Exception) -> _GcPreviewFailure:
    if isinstance(error, _GcPreviewRequestUnsupported):
        return _GcPreviewFailure(
            "unsupported",
            EXIT_INVALID,
            "gc_request_unsupported",
            "use_supported_gc_contract",
        )
    if isinstance(error, _GcPreviewRequestInvalid):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "gc_request_invalid",
            "provide_valid_gc_request",
        )
    if isinstance(error, WorkspaceAuthorityError):
        state = (
            "unsupported"
            if error.reason_code == "runtime_authority_unsupported"
            else "invalid"
        )
        return _GcPreviewFailure(
            state,
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, GcPreviewAuthorityConflict):
        return _GcPreviewFailure(
            "conflict",
            EXIT_DEGRADED,
            "gc_authority_conflict",
            "refresh_gc_request",
        )
    if isinstance(error, GcPreviewUnstable):
        return _GcPreviewFailure(
            "withheld",
            EXIT_DEGRADED,
            "gc_observation_unstable",
            "retry_gc_preview",
            observation_boundary="unstable",
        )
    if isinstance(error, LockTimeout):
        return _GcPreviewFailure(
            "withheld",
            EXIT_DEGRADED,
            "gc_coordination_contended",
            "retry_gc_preview",
        )
    if isinstance(error, GcCoordinationUnavailable):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "gc_coordination_unavailable",
            "run_workspace_repair",
        )
    if isinstance(
        error,
        (
            GcRecoveryRequired,
            LeaseRecoveryRequired,
            PointerRecoveryRequired,
            StateRecoveryRequired,
        ),
    ):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "gc_recovery_required",
            "run_workspace_repair",
        )
    if isinstance(error, JournalRecoveryRequired):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "gc_recovery_required",
            "run_workspace_repair",
        )
    if isinstance(error, StatePathError):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, JournalError):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "journal_invalid",
            "inspect_workspace_state",
        )
    if isinstance(error, UnsupportedRuntime):
        return _GcPreviewFailure(
            "unsupported",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return _GcPreviewFailure(
            "unsupported",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(
        error,
        (
            ContractError,
            GenerationError,
            LeaseError,
            PointerCorrupt,
            StateCorrupt,
        ),
    ):
        return _GcPreviewFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_repair",
        )
    return _GcPreviewFailure(
        "invalid",
        EXIT_INVALID,
        "gc_preview_failed",
        "run_workspace_doctor",
    )


def _emit_gc_preview_failure(
    errors: TextIO,
    failure: _GcPreviewFailure,
) -> int:
    return _emit_canonical_result(
        errors,
        _gc_preview_failure_payload(failure),
        exit_code=failure.exit_code,
        result_label="GC preview result",
    )


def _run_gc_preview(
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            return _emit_gc_preview_failure(
                errors,
                _GcPreviewFailure(
                    "invalid",
                    EXIT_INVALID,
                    "runtime_authority_missing",
                    "install_candidate_authority",
                ),
            )
        runtime = compose_workspace_runtime(resolved_inputs)
    except InjectedFault:
        raise
    except Exception as exc:
        return _emit_gc_preview_failure(
            errors,
            _classify_gc_preview_error(exc),
        )

    try:
        try:
            raw_request = _read_gc_preview_request_bytes()
        except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
            if isinstance(exc, _GcPreviewRequestInvalid):
                raise
            raise _GcPreviewRequestInvalid(
                "GC preview request input cannot be read"
            ) from exc
        request = _parse_gc_preview_request(raw_request)
    except Exception as exc:
        return _emit_gc_preview_failure(
            errors,
            _classify_gc_preview_error(exc),
        )

    try:
        deadline_ns = time.monotonic_ns() + request.timeout_ms * 1_000_000
        with (
            redirect_stdout(_DISCARDED_ENGINE_OUTPUT),
            redirect_stderr(_DISCARDED_ENGINE_OUTPUT),
        ):
            result = runtime.gc.preview(
                request.repo_uuid,
                expected_registry_revision=request.expected_registry_revision,
                expected_active_source_revision=request.expected_active_source_revision,
                expected_operation_epoch=request.expected_operation_epoch,
                expected_migration_epoch=request.expected_migration_epoch,
                expected_pointer_revision=request.expected_pointer_revision,
                capacity_policy=request.capacity_policy,
                protections=request.protections,
                deadline_ns=deadline_ns,
            )
    except InjectedFault:
        raise
    except Exception as exc:
        return _emit_gc_preview_failure(
            errors,
            _classify_gc_preview_error(exc),
        )
    if not isinstance(result, GcPreview) or not _gc_preview_is_valid(result, request):
        return _emit_gc_preview_failure(
            errors,
            _GcPreviewFailure(
                "invalid",
                EXIT_INVALID,
                "gc_result_invalid",
                "run_workspace_doctor",
            ),
        )
    return _emit_canonical_result(
        output,
        _gc_preview_success_payload(result),
        exit_code=EXIT_READY,
        result_label="GC preview result",
    )


def _read_gc_lifecycle_request_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    if binary_input is not None:
        limit = gc_command_runtime.GC_LIFECYCLE_REQUEST_MAX_BYTES + 1
        raw = bytearray()
        while len(raw) < limit:
            remaining = limit - len(raw)
            chunk = binary_input.read(remaining)
            if not isinstance(chunk, bytes):
                raise gc_command_runtime.GcLifecycleRequestInvalid(
                    "GC lifecycle request input did not return bytes"
                )
            if len(chunk) > remaining:
                raise gc_command_runtime.GcLifecycleRequestInvalid(
                    "GC lifecycle request input exceeded its bounded read"
                )
            if chunk == b"":
                break
            raw.extend(chunk)
        return bytes(raw)

    raw = bytearray()
    while len(raw) <= gc_command_runtime.GC_LIFECYCLE_REQUEST_MAX_BYTES:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise gc_command_runtime.GcLifecycleRequestInvalid(
                "GC lifecycle request input did not return text"
            )
        if character == "":
            break
        try:
            raw.extend(character.encode("utf-8"))
        except UnicodeError as exc:
            raise gc_command_runtime.GcLifecycleRequestInvalid(
                "GC lifecycle request input is not UTF-8"
            ) from exc
    return bytes(raw)


def _run_gc_lifecycle(
    operation: str,
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = gc_command_runtime.GcLifecycleFailure(
                operation,
                "invalid",
                EXIT_INVALID,
                "runtime_authority_missing",
                "install_candidate_authority",
            )
            return _emit_canonical_result(
                errors,
                failure.canonical,
                exit_code=failure.exit_code,
                result_label=f"GC {operation} result",
            )
        runtime = compose_workspace_runtime(resolved_inputs)
    except InjectedFault:
        raise
    except Exception as exc:
        failure = gc_command_runtime.classify_failure(exc, operation)
        return _emit_canonical_result(
            errors,
            failure.canonical,
            exit_code=failure.exit_code,
            result_label=f"GC {operation} result",
        )

    try:
        try:
            raw_request = _read_gc_lifecycle_request_bytes()
        except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
            if isinstance(exc, gc_command_runtime.GcLifecycleRequestInvalid):
                raise
            raise gc_command_runtime.GcLifecycleRequestInvalid(
                "GC lifecycle request input cannot be read"
            ) from exc
        occurred_at = datetime.now(timezone.utc)
        with (
            redirect_stdout(_DISCARDED_ENGINE_OUTPUT),
            redirect_stderr(_DISCARDED_ENGINE_OUTPUT),
        ):
            if operation == "execute":
                request = gc_command_runtime.GcExecuteRequest.from_bytes(raw_request)
                result = gc_command_runtime.execute_gc(
                    runtime,
                    request,
                    occurred_at=occurred_at,
                    monotonic_clock=time.monotonic_ns,
                )
            elif operation == "reconcile":
                request = gc_command_runtime.GcReconcileRequest.from_bytes(raw_request)
                result = gc_command_runtime.reconcile_gc(
                    runtime,
                    request,
                    occurred_at=occurred_at,
                    monotonic_clock=time.monotonic_ns,
                )
            elif operation == "purge":
                request = gc_command_runtime.GcPurgeRequest.from_bytes(raw_request)
                result = gc_command_runtime.purge_gc(
                    runtime,
                    request,
                    occurred_at=occurred_at,
                    monotonic_clock=time.monotonic_ns,
                )
            else:  # pragma: no cover - exact dispatcher invariant
                raise ValueError("unsupported GC lifecycle operation")
    except InjectedFault:
        raise
    except Exception as exc:
        failure = gc_command_runtime.classify_failure(exc, operation)
        return _emit_canonical_result(
            errors,
            failure.canonical,
            exit_code=failure.exit_code,
            result_label=f"GC {operation} result",
        )
    return _emit_canonical_result(
        output,
        result.canonical,
        exit_code=EXIT_READY,
        result_label=f"GC {operation} result",
    )


def _read_repair_request_bytes() -> bytes:
    binary_input = getattr(sys.stdin, "buffer", None)
    limit = repair_runtime.REPAIR_REQUEST_MAX_BYTES + 1
    if binary_input is not None:
        raw = bytearray()
        while len(raw) < limit:
            remaining = limit - len(raw)
            chunk = binary_input.read(remaining)
            if not isinstance(chunk, bytes):
                raise ValueError("repair request input did not return bytes")
            if len(chunk) > remaining:
                raise ValueError("repair request input exceeded its bounded read")
            if chunk == b"":
                break
            raw.extend(chunk)
        return bytes(raw)

    raw = bytearray()
    while len(raw) < limit:
        character = sys.stdin.read(1)
        if not isinstance(character, str) or len(character) > 1:
            raise ValueError("repair request input did not return text")
        if character == "":
            break
        try:
            raw.extend(character.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("repair request input is not UTF-8") from exc
    return bytes(raw)


def _run_repair(
    operation: str,
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = repair_runtime.RepairFailure(
                operation,
                "invalid",
                EXIT_INVALID,
                "runtime_authority_missing",
                "install_candidate_authority",
            )
            return _emit_canonical_result(
                errors,
                failure.canonical,
                exit_code=failure.exit_code,
                result_label=f"repair {operation} result",
            )
        runtime = compose_workspace_runtime(resolved_inputs)
    except InjectedFault:
        raise
    except Exception as exc:
        failure = repair_runtime.classify_failure(exc, operation)
        return _emit_canonical_result(
            errors,
            failure.canonical,
            exit_code=failure.exit_code,
            result_label=f"repair {operation} result",
        )

    try:
        try:
            raw_request = _read_repair_request_bytes()
        except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
            if operation == "preview":
                raise repair_runtime.RepairPreviewRequestInvalid(
                    "repair preview request input cannot be read"
                ) from exc
            raise repair_runtime.RepairExecuteRequestInvalid(
                "repair execute request input cannot be read"
            ) from exc
        with (
            redirect_stdout(_DISCARDED_ENGINE_OUTPUT),
            redirect_stderr(_DISCARDED_ENGINE_OUTPUT),
        ):
            if operation == "preview":
                request = RepairPreviewRequest.from_bytes(raw_request)
                result = repair_preview(runtime, request)
            elif operation == "execute":
                request = RepairExecuteRequest.from_bytes(raw_request)
                result = repair_execute(
                    runtime,
                    request,
                    occurred_at=datetime.now(timezone.utc),
                    monotonic_clock=time.monotonic_ns,
                )
            else:  # pragma: no cover - exact dispatcher invariant
                raise ValueError("unsupported repair operation")
    except InjectedFault:
        raise
    except Exception as exc:
        failure = repair_runtime.classify_failure(exc, operation)
        return _emit_canonical_result(
            errors,
            failure.canonical,
            exit_code=failure.exit_code,
            result_label=f"repair {operation} result",
        )
    exit_code = (
        EXIT_INVALID
        if operation == "preview" and result.to_dict()["classification"] == "irreparable"
        else EXIT_READY
    )
    return _emit_canonical_result(
        output,
        result.canonical,
        exit_code=exit_code,
        result_label=f"repair {operation} result",
    )


def _doctor_text(report: WorkspaceStatusReport) -> str:
    value = report.to_dict()
    lines = [
        f"workspace doctor: {value['state']} (exit {value['exit_code']})",
        f"safe_to_query: {str(value['safe_to_query']).lower()}",
        f"reason: {value['reason_code']}",
        f"action: {value['action_code']}",
    ]
    for check in value["checks"]:
        lines.append(
            "check "
            f"{check['component']}: {check['state']} "
            f"reason={check['reason_code']} action={check['action_code']}"
        )
    return "\n".join(lines) + "\n"


def run_workspace_command(
    arguments: Sequence[str],
    *,
    inputs: WorkspaceRuntimeInputs | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one narrow workspace command and return its stable exit code."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    command = tuple(arguments)
    if command and command[0] == "activate":
        request = _parse_activation_request(command)
        if request is None:
            return _emit_text_payload(
                errors,
                _USAGE + "\n",
                exit_code=EXIT_USAGE,
            )
        return _run_activation(
            request,
            inputs=inputs,
            output=output,
            errors=errors,
        )
    if command and command[0] == "register":
        request = _parse_register_request(command)
        if request is None:
            return _emit_text_payload(
                errors,
                _USAGE + "\n",
                exit_code=EXIT_USAGE,
            )
        return _run_registration(
            request,
            inputs=inputs,
            output=output,
            errors=errors,
        )
    if command and command[0] == "sync":
        if command != ("sync", "--code-only", "--request-stdin"):
            return _emit_text_payload(
                errors,
                _USAGE + "\n",
                exit_code=EXIT_USAGE,
            )
        return _run_sync(
            inputs=inputs,
            output=output,
            errors=errors,
        )
    if command and command[0] == "repair":
        repair_commands: dict[tuple[str, ...], str] = {
            ("repair", "--dry-run", "--request-stdin"): "preview",
            ("repair", "--execute", "--request-stdin"): "execute",
        }
        operation = repair_commands.get(command)
        if operation is not None:
            return _run_repair(
                operation,
                inputs=inputs,
                output=output,
                errors=errors,
            )
        usage = (
            _REPAIR_EXECUTE_USAGE
            if "--execute" in command
            else _REPAIR_PREVIEW_USAGE
        )
        return _emit_text_payload(
            errors,
            usage + "\n",
            exit_code=EXIT_USAGE,
        )
    if command and command[0] == "gc":
        if command == ("gc", "--dry-run", "--request-stdin"):
            return _run_gc_preview(
                inputs=inputs,
                output=output,
                errors=errors,
            )
        lifecycle_commands: dict[tuple[str, ...], str] = {
            ("gc", "--execute", "--request-stdin"): "execute",
            ("gc", "--reconcile", "--request-stdin"): "reconcile",
            ("gc", "--purge", "--request-stdin"): "purge",
        }
        operation = lifecycle_commands.get(command)
        if operation is not None:
            return _run_gc_lifecycle(
                operation,
                inputs=inputs,
                output=output,
                errors=errors,
            )
        usage = _GC_PREVIEW_USAGE
        for flag, lifecycle_usage in (
            ("--execute", _GC_EXECUTE_USAGE),
            ("--reconcile", _GC_RECONCILE_USAGE),
            ("--purge", _GC_PURGE_USAGE),
        ):
            if flag in command:
                usage = lifecycle_usage
                break
        return _emit_text_payload(
            errors,
            usage + "\n",
            exit_code=EXIT_USAGE,
        )
    if command and command[0] == "rollback":
        if command != ("rollback", "--request-stdin"):
            return _emit_text_payload(
                errors,
                _ROLLBACK_USAGE + "\n",
                exit_code=EXIT_USAGE,
            )
        return _run_rollback(
            inputs=inputs,
            output=output,
            errors=errors,
        )
    if command and command[0] == "query":
        if command != ("query", "--request-stdin"):
            return _emit_text_payload(
                errors,
                _QUERY_USAGE + "\n",
                exit_code=EXIT_USAGE,
            )
        return _run_query(
            inputs=inputs,
            output=output,
            errors=errors,
        )
    if command not in {("status", "--json"), ("doctor",)}:
        errors.write(_USAGE + "\n")
        return EXIT_USAGE

    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        report = (
            missing_workspace_authority_report()
            if resolved_inputs is None
            else inspect_workspace_status(resolved_inputs)
        )
    except WorkspaceAuthorityError as exc:
        report = invalid_workspace_authority_report(
            reason_code=exc.reason_code,
            action_code=exc.action_code,
        )
    except StatePathError:
        report = invalid_workspace_authority_report(
            reason_code="unsafe_state_path",
            action_code="configure_safe_state_root",
        )
    except UnsupportedRuntime:
        report = invalid_workspace_authority_report(
            reason_code="unsupported_runtime",
            action_code="use_supported_runtime",
        )
    if command == ("status", "--json"):
        output.write(report.canonical.decode("utf-8"))
    else:
        output.write(_doctor_text(report))
    return report.exit_code


def load_registration_schema() -> dict[str, Any]:
    """Load the public registration receipt schema."""

    value = _load_json(_REGISTRATION_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace registration schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_identity_maintenance_schema() -> dict[str, Any]:
    """Load the public identity-maintenance receipt schema."""

    value = _load_json(_IDENTITY_MAINTENANCE_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace identity-maintenance schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_activation_schema() -> dict[str, Any]:
    """Load the public active-source activation receipt schema."""

    value = _load_json(_ACTIVATION_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace activation schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_sync_request_schema() -> dict[str, Any]:
    """Load the public canonical code-only sync request schema."""

    value = _load_json(_SYNC_REQUEST_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace sync request schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_sync_receipt_schema() -> dict[str, Any]:
    """Load the public redacted code-only sync receipt schema."""

    value = _load_json(_SYNC_RECEIPT_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace sync receipt schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_query_request_schema() -> dict[str, Any]:
    """Load the public canonical one-shot query request schema."""

    value = _load_json(_QUERY_REQUEST_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace query request schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_query_result_schema() -> dict[str, Any]:
    """Load the public one-shot query result control-record schema."""

    value = _load_json(_QUERY_RESULT_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace query result schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_rollback_request_schema() -> dict[str, Any]:
    """Load the public canonical exact-last-good rollback request schema."""

    value = _load_json(_ROLLBACK_REQUEST_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace rollback request schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_rollback_receipt_schema() -> dict[str, Any]:
    """Load the public redacted exact-last-good rollback receipt schema."""

    value = _load_json(_ROLLBACK_RECEIPT_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace rollback receipt schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_gc_preview_request_schema() -> dict[str, Any]:
    """Load the public canonical offline-GC preview request schema."""

    value = _load_json(_GC_PREVIEW_REQUEST_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace GC preview request schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_gc_preview_result_schema() -> dict[str, Any]:
    """Load the public deterministic offline-GC preview result schema."""

    value = _load_json(_GC_PREVIEW_RESULT_SCHEMA_PATH.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError("workspace GC preview result schema must be a JSON object")
    return cast(dict[str, Any], value)


def _load_gc_lifecycle_schema(path: Path, label: str) -> dict[str, Any]:
    value = _load_json(path.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError(f"workspace GC {label} schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_gc_execute_request_schema() -> dict[str, Any]:
    """Load the public canonical fenced-GC execute request schema."""

    return _load_gc_lifecycle_schema(_GC_EXECUTE_REQUEST_SCHEMA_PATH, "execute request")


def load_gc_execute_result_schema() -> dict[str, Any]:
    """Load the public redacted fenced-GC execute result schema."""

    return _load_gc_lifecycle_schema(_GC_EXECUTE_RESULT_SCHEMA_PATH, "execute result")


def load_gc_reconcile_request_schema() -> dict[str, Any]:
    """Load the public canonical fenced-GC reconcile request schema."""

    return _load_gc_lifecycle_schema(
        _GC_RECONCILE_REQUEST_SCHEMA_PATH,
        "reconcile request",
    )


def load_gc_reconcile_result_schema() -> dict[str, Any]:
    """Load the public redacted fenced-GC reconcile result schema."""

    return _load_gc_lifecycle_schema(
        _GC_RECONCILE_RESULT_SCHEMA_PATH,
        "reconcile result",
    )


def load_gc_purge_request_schema() -> dict[str, Any]:
    """Load the public canonical fenced-GC purge request schema."""

    return _load_gc_lifecycle_schema(_GC_PURGE_REQUEST_SCHEMA_PATH, "purge request")


def load_gc_purge_result_schema() -> dict[str, Any]:
    """Load the public redacted fenced-GC purge result schema."""

    return _load_gc_lifecycle_schema(_GC_PURGE_RESULT_SCHEMA_PATH, "purge result")


def _load_repair_schema(path: Path, label: str) -> dict[str, Any]:
    value = _load_json(path.read_bytes())
    if not isinstance(value, dict):  # pragma: no cover - packaged artifact invariant
        raise ValueError(f"workspace repair {label} schema must be a JSON object")
    return cast(dict[str, Any], value)


def load_repair_preview_request_schema() -> dict[str, Any]:
    """Load the public canonical pointer-repair preview request schema."""

    return _load_repair_schema(_REPAIR_PREVIEW_REQUEST_SCHEMA_PATH, "preview request")


def load_repair_preview_result_schema() -> dict[str, Any]:
    """Load the public redacted pointer-repair preview result schema."""

    return _load_repair_schema(_REPAIR_PREVIEW_RESULT_SCHEMA_PATH, "preview result")


def load_repair_execute_request_schema() -> dict[str, Any]:
    """Load the public canonical fenced pointer-repair execute request schema."""

    return _load_repair_schema(_REPAIR_EXECUTE_REQUEST_SCHEMA_PATH, "execute request")


def load_repair_execute_result_schema() -> dict[str, Any]:
    """Load the public redacted fenced pointer-repair execute result schema."""

    return _load_repair_schema(_REPAIR_EXECUTE_RESULT_SCHEMA_PATH, "execute result")


__all__ = [
    "load_activation_schema",
    "load_gc_execute_request_schema",
    "load_gc_execute_result_schema",
    "load_gc_preview_request_schema",
    "load_gc_preview_result_schema",
    "load_gc_purge_request_schema",
    "load_gc_purge_result_schema",
    "load_gc_reconcile_request_schema",
    "load_gc_reconcile_result_schema",
    "load_identity_maintenance_schema",
    "load_query_request_schema",
    "load_query_result_schema",
    "load_repair_execute_request_schema",
    "load_repair_execute_result_schema",
    "load_repair_preview_request_schema",
    "load_repair_preview_result_schema",
    "load_registration_schema",
    "load_rollback_receipt_schema",
    "load_rollback_request_schema",
    "load_sync_receipt_schema",
    "load_sync_request_schema",
    "run_workspace_command",
]
