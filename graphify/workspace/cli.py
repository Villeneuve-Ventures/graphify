"""Narrow workspace activation, identity, sync, query, status, and doctor commands."""

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
    ContractError,
    WorkspaceLeaseState,
    canonical_json_bytes,
    canonical_registry_source,
    canonical_sha256,
)
from graphify.workspace.generations import (
    CapacityExceeded,
    GenerationConflict,
    GenerationError,
)
from graphify.workspace.freshness import FreshnessResult
from graphify.workspace.journal import JournalError
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
_REVISION_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_REGISTER_USAGE = (
    "graphify workspace register <enroll|adopt|rebind|rotate> --repo-uuid UUID "
    "--expected-registry-revision N --authorization-stdin"
)
_SYNC_USAGE = "graphify workspace sync --code-only --request-stdin"
_QUERY_USAGE = "graphify workspace query --request-stdin"
_ROLLBACK_USAGE = "graphify workspace rollback --request-stdin"
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


class _QueryRequestInvalid(ValueError):
    """The query CLI request cannot be accepted safely."""


class _QueryRequestUnsupported(_QueryRequestInvalid):
    """The query CLI request names an unsupported public contract."""


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
            "run_workspace_repair",
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


__all__ = [
    "load_activation_schema",
    "load_identity_maintenance_schema",
    "load_query_request_schema",
    "load_query_result_schema",
    "load_registration_schema",
    "load_rollback_receipt_schema",
    "load_rollback_request_schema",
    "load_sync_receipt_schema",
    "load_sync_request_schema",
    "run_workspace_command",
]
