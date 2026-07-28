"""Public, request-bound orchestration for the fenced offline-GC lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from types import TracebackType
from typing import ClassVar, cast

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import WorkspaceAuthorityError, WorkspaceRuntime
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    CapacityPolicy,
    ContractError,
    GcCompletionState,
    GcIntentState,
    GcPurgeState,
    WorkspaceLeaseState,
    canonical_json_bytes,
)
from graphify.workspace.gc import (
    GC_PREVIEW_MAX_GENERATIONS,
    GcCoordinationUnavailable,
    GcError,
    GcPlan,
    GcPlanStale,
    GcPreview,
    GcPreviewAuthorityConflict,
    GcPreviewUnstable,
    GcProtection,
    GcRecoveryRequired,
)
from graphify.workspace.generations import GenerationError
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    OperatorAuthorization,
)
from graphify.workspace.journal import JournalError
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseError,
    LeaseExpired,
    LeaseGrant,
    LeaseRecoveryRequired,
    StaleLease,
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
from graphify.workspace.pointers import (
    PointerConflict,
    PointerCorrupt,
    PointerRecoveryRequired,
)
from graphify.workspace.registry import RevisionConflict
from graphify.workspace.status import EXIT_DEGRADED, EXIT_INVALID, EXIT_READY


GC_EXECUTE_REQUEST_CONTRACT = "graphify.workspace.gc_execute_request"
GC_EXECUTE_RESULT_CONTRACT = "graphify.workspace.gc_execute_result"
GC_RECONCILE_REQUEST_CONTRACT = "graphify.workspace.gc_reconcile_request"
GC_RECONCILE_RESULT_CONTRACT = "graphify.workspace.gc_reconcile_result"
GC_PURGE_REQUEST_CONTRACT = "graphify.workspace.gc_purge_request"
GC_PURGE_RESULT_CONTRACT = "graphify.workspace.gc_purge_result"
GC_PREVIEW_RESULT_CONTRACT = "graphify.workspace.gc_preview_result"
GC_LIFECYCLE_SCHEMA_VERSION = 1
GC_LIFECYCLE_REQUEST_MAX_BYTES = 128 * 1024
GC_LIFECYCLE_TIMEOUT_MAX_MS = 60_000

_GC_LEASE_TTL_NS = 30_000_000_000
_MAX_SIGNED_INTEGER = 9_223_372_036_854_775_807
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$")
_AUTHORIZATION_FIELDS = frozenset(
    {"action", "issued_at", "nonce", "operator_id", "reason"}
)
_PROTECTION_FIELDS = (
    "active_lease_generations",
    "fixture_generations",
    "migration_sources",
    "proof_generations",
    "rollback_artifact_generations",
    "rollback_sources",
)
_COMMON_REQUEST_FIELDS = frozenset(
    {
        "authorization",
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


class GcLifecycleRequestInvalid(ValueError):
    """A public lifecycle request is malformed or noncanonical."""


class GcLifecycleRequestUnsupported(GcLifecycleRequestInvalid):
    """A public lifecycle request names an unsupported contract version."""


class GcApprovedPreviewMismatch(GcPlanStale, ValueError):
    """The operator-approved public preview bytes no longer match."""


class GcPreviewPlanMismatch(GcPlanStale):
    """The fresh fenced plan differs from the approved non-fence facts."""


@dataclass(frozen=True)
class GcLifecycleFailure:
    """Canonical redacted failure for one public lifecycle phase."""

    operation: str
    state: str
    exit_code: int
    reason_code: str
    action_code: str

    @property
    def contract(self) -> str:
        return f"graphify.workspace.gc_{self.operation}_result"

    def to_dict(self) -> dict[str, object]:
        return {
            "action_code": self.action_code,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": self.contract,
            "exit_code": self.exit_code,
            "reason_code": self.reason_code,
            "schema_version": GC_LIFECYCLE_SCHEMA_VERSION,
            "state": self.state,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def classify_failure(error: Exception, operation: str) -> GcLifecycleFailure:
    """Map an internal lifecycle failure to its phase-specific public envelope."""

    if operation not in {"execute", "reconcile", "purge"}:
        raise ValueError("unsupported GC lifecycle operation")
    if isinstance(error, WorkspaceAuthorityError):
        state = (
            "unsupported"
            if error.reason_code == "runtime_authority_unsupported"
            else "invalid"
        )
        return GcLifecycleFailure(
            operation,
            state,
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, GcLifecycleRequestUnsupported):
        return GcLifecycleFailure(
            operation,
            "unsupported",
            EXIT_INVALID,
            f"gc_{operation}_request_unsupported",
            "use_supported_gc_contract",
        )
    if isinstance(error, (GcLifecycleRequestInvalid, AuthorizationError)):
        return GcLifecycleFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            f"gc_{operation}_request_invalid",
            f"provide_valid_gc_{operation}_request",
        )
    if isinstance(error, (LeaseBusy, LockTimeout, GcCoordinationUnavailable)):
        return GcLifecycleFailure(
            operation,
            "conflict",
            EXIT_DEGRADED,
            "gc_lease_busy",
            f"retry_workspace_gc_{operation}",
        )
    if isinstance(
        error,
        (
            GcApprovedPreviewMismatch,
            GcPreviewPlanMismatch,
            GcPlanStale,
            GcPreviewAuthorityConflict,
            GcPreviewUnstable,
            RevisionConflict,
            PointerConflict,
            LeaseExpired,
            StaleLease,
        ),
    ):
        return GcLifecycleFailure(
            operation,
            "conflict",
            EXIT_DEGRADED,
            "gc_authority_conflict",
            f"refresh_gc_{operation}_request",
        )
    if isinstance(error, GcRecoveryRequired):
        return GcLifecycleFailure(
            operation,
            "conflict",
            EXIT_DEGRADED,
            "gc_recovery_required",
            "run_workspace_gc_reconcile",
        )
    if isinstance(
        error,
        (LeaseRecoveryRequired, PointerRecoveryRequired, StateRecoveryRequired),
    ):
        return GcLifecycleFailure(
            operation,
            "conflict",
            EXIT_DEGRADED,
            "workspace_recovery_required",
            "run_workspace_doctor",
        )
    if isinstance(error, CommitUnknown):
        action = (
            "run_workspace_gc_reconcile"
            if operation in {"execute", "reconcile"}
            else "run_workspace_doctor"
        )
        return GcLifecycleFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            action,
        )
    if isinstance(error, StatePathError):
        return GcLifecycleFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return GcLifecycleFailure(
            operation,
            "unsupported",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return GcLifecycleFailure(
            operation,
            "unsupported",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(
        error,
        (
            StateCorrupt,
            PointerCorrupt,
            ContractError,
            GenerationError,
            JournalError,
            LeaseError,
            GcError,
        ),
    ):
        return GcLifecycleFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_repair",
        )
    return GcLifecycleFailure(
        operation,
        "invalid",
        EXIT_INVALID,
        f"gc_{operation}_failed",
        "run_workspace_doctor",
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GcLifecycleRequestInvalid("GC lifecycle request has a duplicate key")
        result[key] = value
    return result


def _integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SIGNED_INTEGER:
        raise GcLifecycleRequestInvalid(f"GC lifecycle request {label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise GcLifecycleRequestInvalid(f"GC lifecycle request {label} is invalid")
    return value


def _generation_ids(value: object, label: str) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > GC_PREVIEW_MAX_GENERATIONS:
        raise GcLifecycleRequestInvalid(
            f"GC lifecycle request protection {label} is invalid"
        )
    generation_ids: list[str] = []
    for generation_id in value:
        if not isinstance(generation_id, str) or _GENERATION_RE.fullmatch(
            generation_id
        ) is None:
            raise GcLifecycleRequestInvalid(
                f"GC lifecycle request protection {label} is invalid"
            )
        generation_ids.append(generation_id)
    if generation_ids != sorted(generation_ids) or len(generation_ids) != len(
        set(generation_ids)
    ):
        raise GcLifecycleRequestInvalid(
            f"GC lifecycle request protection {label} must be unique and sorted"
        )
    return frozenset(generation_ids)


def _protection_dict(protection: GcProtection) -> dict[str, object]:
    return {
        field: sorted(cast(frozenset[str], getattr(protection, field)))
        for field in _PROTECTION_FIELDS
    }


@dataclass(frozen=True)
class _ParsedCommon:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    expected_pointer_revision: int
    timeout_ms: int
    capacity_policy: CapacityPolicy
    protections: GcProtection
    authorization: OperatorAuthorization


def _parse_request(
    value: bytes,
    *,
    contract: str,
    action: IdentityAction,
    extra_fields: frozenset[str],
) -> tuple[_ParsedCommon, Mapping[str, object]]:
    if not isinstance(value, bytes) or len(value) > GC_LIFECYCLE_REQUEST_MAX_BYTES:
        raise GcLifecycleRequestInvalid("GC lifecycle request exceeds the byte limit")
    try:
        data = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(data, Mapping) or set(data) != _COMMON_REQUEST_FIELDS | extra_fields:
        raise GcLifecycleRequestInvalid("GC lifecycle request fields are invalid")
    request_contract = data["contract"]
    if not isinstance(request_contract, str):
        raise GcLifecycleRequestInvalid("GC lifecycle request contract is invalid")
    if request_contract != contract:
        raise GcLifecycleRequestUnsupported(
            "GC lifecycle request contract is unsupported"
        )
    for field, expected in (
        ("schema_version", GC_LIFECYCLE_SCHEMA_VERSION),
        ("cli_contract_version", CLI_CONTRACT_VERSION),
    ):
        version = data[field]
        if type(version) is not int:
            raise GcLifecycleRequestInvalid(
                f"GC lifecycle request {field} is invalid"
            )
        if version != expected:
            raise GcLifecycleRequestUnsupported(
                f"GC lifecycle request {field} is unsupported"
            )
    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(data["repo_uuid"])
    except ContractError as exc:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request repo_uuid is invalid"
        ) from exc
    capacity_value = data["capacity_policy"]
    if not isinstance(capacity_value, Mapping):
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request capacity policy is invalid"
        )
    try:
        capacity_policy = CapacityPolicy.from_mapping(capacity_value)
    except (ContractError, TypeError, ValueError) as exc:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request capacity policy is invalid"
        ) from exc
    protection_value = data["protections"]
    if not isinstance(protection_value, Mapping) or set(protection_value) != set(
        _PROTECTION_FIELDS
    ):
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request protection fields are invalid"
        )
    protection_ids = {
        field: _generation_ids(protection_value[field], field)
        for field in _PROTECTION_FIELDS
    }
    if (
        len(
            {
                generation_id
                for generation_ids in protection_ids.values()
                for generation_id in generation_ids
            }
        )
        > GC_PREVIEW_MAX_GENERATIONS
    ):
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request protections exceed the public bound"
        )
    authorization_value = data["authorization"]
    if not isinstance(authorization_value, Mapping) or set(
        authorization_value
    ) != _AUTHORIZATION_FIELDS:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request authorization fields are invalid"
        )
    if not all(
        isinstance(authorization_value[field], str)
        for field in _AUTHORIZATION_FIELDS
    ) or authorization_value["action"] != action.value:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request authorization is invalid"
        )
    try:
        authorization = OperatorAuthorization(
            action=action,
            issued_at=cast(str, authorization_value["issued_at"]),
            nonce=cast(str, authorization_value["nonce"]),
            operator_id=cast(str, authorization_value["operator_id"]),
            reason=cast(str, authorization_value["reason"]),
        )
    except AuthorizationError as exc:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request authorization is invalid"
        ) from exc
    timeout_ms = _integer(data["timeout_ms"], "timeout_ms", minimum=1)
    if timeout_ms > GC_LIFECYCLE_TIMEOUT_MAX_MS:
        raise GcLifecycleRequestInvalid(
            "GC lifecycle request timeout_ms is invalid"
        )
    common = _ParsedCommon(
        repo_uuid=repo_uuid,
        expected_registry_revision=_integer(
            data["expected_registry_revision"],
            "expected_registry_revision",
            minimum=1,
        ),
        expected_active_source_revision=_integer(
            data["expected_active_source_revision"],
            "expected_active_source_revision",
            minimum=1,
        ),
        expected_operation_epoch=_integer(
            data["expected_operation_epoch"],
            "expected_operation_epoch",
            minimum=0,
        ),
        expected_migration_epoch=_integer(
            data["expected_migration_epoch"],
            "expected_migration_epoch",
            minimum=0,
        ),
        expected_pointer_revision=_integer(
            data["expected_pointer_revision"],
            "expected_pointer_revision",
            minimum=0,
        ),
        timeout_ms=timeout_ms,
        capacity_policy=capacity_policy,
        protections=GcProtection(
            migration_sources=protection_ids["migration_sources"],
            rollback_sources=protection_ids["rollback_sources"],
            active_lease_generations=protection_ids["active_lease_generations"],
            fixture_generations=protection_ids["fixture_generations"],
            proof_generations=protection_ids["proof_generations"],
            rollback_artifact_generations=protection_ids[
                "rollback_artifact_generations"
            ],
        ),
        authorization=authorization,
    )
    return common, data


@dataclass(frozen=True)
class _GcLifecycleRequest:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    expected_pointer_revision: int
    timeout_ms: int
    capacity_policy: CapacityPolicy
    protections: GcProtection
    authorization: OperatorAuthorization

    CONTRACT: ClassVar[str]

    def _extra_dict(self) -> dict[str, object]:
        return {}

    def to_dict(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.to_dict(),
            "capacity_policy": self.capacity_policy.to_dict(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": self.CONTRACT,
            "expected_active_source_revision": self.expected_active_source_revision,
            "expected_migration_epoch": self.expected_migration_epoch,
            "expected_operation_epoch": self.expected_operation_epoch,
            "expected_pointer_revision": self.expected_pointer_revision,
            "expected_registry_revision": self.expected_registry_revision,
            "protections": _protection_dict(self.protections),
            "repo_uuid": self.repo_uuid,
            "schema_version": GC_LIFECYCLE_SCHEMA_VERSION,
            "timeout_ms": self.timeout_ms,
            **self._extra_dict(),
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()


@dataclass(frozen=True)
class GcExecuteRequest(_GcLifecycleRequest):
    approved_preview_sha256: str

    CONTRACT: ClassVar[str] = GC_EXECUTE_REQUEST_CONTRACT

    def _extra_dict(self) -> dict[str, object]:
        return {"approved_preview_sha256": self.approved_preview_sha256}

    @classmethod
    def from_bytes(cls, value: bytes) -> "GcExecuteRequest":
        common, data = _parse_request(
            value,
            contract=cls.CONTRACT,
            action=IdentityAction.GC_EXECUTE,
            extra_fields=frozenset({"approved_preview_sha256"}),
        )
        request = cls(
            common.repo_uuid,
            common.expected_registry_revision,
            common.expected_active_source_revision,
            common.expected_operation_epoch,
            common.expected_migration_epoch,
            common.expected_pointer_revision,
            common.timeout_ms,
            common.capacity_policy,
            common.protections,
            common.authorization,
            _digest(data["approved_preview_sha256"], "approved_preview_sha256"),
        )
        if request.canonical != value:
            raise GcLifecycleRequestInvalid("GC lifecycle request is not canonical JSON")
        return request


@dataclass(frozen=True)
class GcReconcileRequest(_GcLifecycleRequest):
    CONTRACT: ClassVar[str] = GC_RECONCILE_REQUEST_CONTRACT

    @classmethod
    def from_bytes(cls, value: bytes) -> "GcReconcileRequest":
        common, _data = _parse_request(
            value,
            contract=cls.CONTRACT,
            action=IdentityAction.GC_RECONCILE,
            extra_fields=frozenset(),
        )
        request = cls(
            common.repo_uuid,
            common.expected_registry_revision,
            common.expected_active_source_revision,
            common.expected_operation_epoch,
            common.expected_migration_epoch,
            common.expected_pointer_revision,
            common.timeout_ms,
            common.capacity_policy,
            common.protections,
            common.authorization,
        )
        if request.canonical != value:
            raise GcLifecycleRequestInvalid("GC lifecycle request is not canonical JSON")
        return request


@dataclass(frozen=True)
class GcPurgeRequest(_GcLifecycleRequest):
    expected_plan_sha256: str

    CONTRACT: ClassVar[str] = GC_PURGE_REQUEST_CONTRACT

    def _extra_dict(self) -> dict[str, object]:
        return {"expected_plan_sha256": self.expected_plan_sha256}

    @classmethod
    def from_bytes(cls, value: bytes) -> "GcPurgeRequest":
        common, data = _parse_request(
            value,
            contract=cls.CONTRACT,
            action=IdentityAction.GC_PURGE,
            extra_fields=frozenset({"expected_plan_sha256"}),
        )
        request = cls(
            common.repo_uuid,
            common.expected_registry_revision,
            common.expected_active_source_revision,
            common.expected_operation_epoch,
            common.expected_migration_epoch,
            common.expected_pointer_revision,
            common.timeout_ms,
            common.capacity_policy,
            common.protections,
            common.authorization,
            _digest(data["expected_plan_sha256"], "expected_plan_sha256"),
        )
        if request.canonical != value:
            raise GcLifecycleRequestInvalid("GC lifecycle request is not canonical JSON")
        return request


def _validate_public_generation_ids(value: tuple[str, ...], label: str) -> None:
    if (
        len(value) > GC_PREVIEW_MAX_GENERATIONS
        or list(value) != sorted(value)
        or len(value) != len(set(value))
        or any(_GENERATION_RE.fullmatch(item) is None for item in value)
    ):
        raise ValueError(f"GC {label} generation IDs are invalid")


@dataclass(frozen=True)
class GcExecuteResult:
    repo_uuid: str
    request_sha256: str
    approved_preview_sha256: str
    plan_sha256: str
    quarantined: tuple[str, ...]

    def __post_init__(self) -> None:
        WorkspaceLeaseState.canonical_repo_uuid(self.repo_uuid)
        for value in (
            self.request_sha256,
            self.approved_preview_sha256,
            self.plan_sha256,
        ):
            if _DIGEST_RE.fullmatch(value) is None:
                raise ValueError("GC execute result digest is invalid")
        _validate_public_generation_ids(self.quarantined, "execute")

    def to_dict(self) -> dict[str, object]:
        return {
            "approved_preview_sha256": self.approved_preview_sha256,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": GC_EXECUTE_RESULT_CONTRACT,
            "exit_code": EXIT_READY,
            "plan_sha256": self.plan_sha256,
            "quarantined": list(self.quarantined),
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": GC_LIFECYCLE_SCHEMA_VERSION,
            "state": "quarantined",
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class GcReconcileResult:
    repo_uuid: str
    request_sha256: str
    plan_sha256: str | None = None
    quarantined: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        WorkspaceLeaseState.canonical_repo_uuid(self.repo_uuid)
        if _DIGEST_RE.fullmatch(self.request_sha256) is None:
            raise ValueError("GC reconcile result request digest is invalid")
        if (self.plan_sha256 is None) != (self.quarantined is None):
            raise ValueError("GC reconcile result is internally inconsistent")
        if self.plan_sha256 is not None:
            if _DIGEST_RE.fullmatch(self.plan_sha256) is None:
                raise ValueError("GC reconcile result plan digest is invalid")
            _validate_public_generation_ids(
                cast(tuple[str, ...], self.quarantined),
                "reconcile",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": GC_RECONCILE_RESULT_CONTRACT,
            "exit_code": EXIT_READY,
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": GC_LIFECYCLE_SCHEMA_VERSION,
            "state": "nothing_to_reconcile",
        }
        if self.plan_sha256 is not None:
            result.update(
                {
                    "plan_sha256": self.plan_sha256,
                    "quarantined": list(cast(tuple[str, ...], self.quarantined)),
                    "state": "reconciled",
                }
            )
        return result

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class GcPurgeResult:
    repo_uuid: str
    request_sha256: str
    plan_sha256: str
    purged: tuple[str, ...]

    def __post_init__(self) -> None:
        WorkspaceLeaseState.canonical_repo_uuid(self.repo_uuid)
        if _DIGEST_RE.fullmatch(self.request_sha256) is None or _DIGEST_RE.fullmatch(
            self.plan_sha256
        ) is None:
            raise ValueError("GC purge result digest is invalid")
        _validate_public_generation_ids(self.purged, "purge")

    def to_dict(self) -> dict[str, object]:
        return {
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": GC_PURGE_RESULT_CONTRACT,
            "exit_code": EXIT_READY,
            "plan_sha256": self.plan_sha256,
            "purged": list(self.purged),
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": GC_LIFECYCLE_SCHEMA_VERSION,
            "state": "purged",
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def gc_preview_result_bytes(result: GcPreview) -> bytes:
    """Return the frozen canonical public bytes approved by execute requests."""

    return canonical_json_bytes(
        {
            "capacity_policy_sha256": result.capacity_policy_sha256,
            "candidates": list(result.candidates),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": GC_PREVIEW_RESULT_CONTRACT,
            "decision": "preview",
            "exit_code": EXIT_READY,
            "observation_boundary": "locked_double_snapshot",
            "observed": {
                "active_source_revision": result.active_source_revision,
                "migration_epoch": result.migration_epoch,
                "operation_epoch": result.operation_epoch,
                "pointer_revision": result.pointer_revision,
                "registry_revision": result.registry_revision,
            },
            "protected": [
                {"generation_id": generation_id, "reasons": list(reasons)}
                for generation_id, reasons in result.protected
            ],
            "reason_code": "preview_ready",
            "repo_uuid": result.repo_uuid,
            "schema_version": GC_LIFECYCLE_SCHEMA_VERSION,
            "state": "previewed",
        }
    )


def _preview_plan_projection(value: GcPreview | GcPlan) -> tuple[object, ...]:
    return (
        value.repo_uuid,
        value.registry_revision,
        value.active_source_revision,
        value.migration_epoch,
        value.pointer_revision,
        value.capacity_policy_sha256,
        value.candidates,
        value.protected,
    )


def _acquire_gc(
    runtime: WorkspaceRuntime,
    request: _GcLifecycleRequest,
    *,
    occurred_at: datetime,
    monotonic_ns: int,
    deadline_ns: int,
) -> LeaseGrant:
    return runtime.leases.acquire(
        request.repo_uuid,
        "GC",
        runtime.leases.current_owner(),
        expected_registry_revision=request.expected_registry_revision,
        expected_active_source_revision=request.expected_active_source_revision,
        expected_operation_epoch=request.expected_operation_epoch,
        expected_migration_epoch=request.expected_migration_epoch,
        acquired_at=occurred_at,
        monotonic_ns=monotonic_ns,
        ttl_ns=_GC_LEASE_TTL_NS,
        deadline_ns=deadline_ns,
    )


def _release_grant(
    runtime: WorkspaceRuntime,
    grant: LeaseGrant,
    operation: str,
    primary: tuple[BaseException, TracebackType | None] | None,
) -> None:
    try:
        runtime.leases.release(grant)
    except (CommitUnknown, InjectedFault):
        if primary is None:
            raise
    except Exception as exc:
        if primary is None:
            raise CommitUnknown(
                f"GC {operation} lease release outcome is uncertain"
            ) from exc
    if primary is not None:
        error, traceback = primary
        raise error.with_traceback(traceback)


def execute_gc(
    runtime: WorkspaceRuntime,
    request: GcExecuteRequest,
    *,
    occurred_at: datetime,
    monotonic_clock: Callable[[], int],
) -> GcExecuteResult:
    """Revalidate approved preview bytes, then quarantine under a fresh fence."""

    request.authorization.require(IdentityAction.GC_EXECUTE)
    started_ns = monotonic_clock()
    deadline_ns = started_ns + request.timeout_ms * 1_000_000
    preview = runtime.gc.preview(
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
    approved_preview_sha256 = hashlib.sha256(
        gc_preview_result_bytes(preview)
    ).hexdigest()
    if approved_preview_sha256 != request.approved_preview_sha256:
        raise GcApprovedPreviewMismatch(
            "approved preview SHA-256 does not match the current public preview bytes"
        )
    grant = _acquire_gc(
        runtime,
        request,
        occurred_at=occurred_at,
        monotonic_ns=monotonic_clock(),
        deadline_ns=deadline_ns,
    )
    primary: tuple[BaseException, TracebackType | None] | None = None
    try:
        plan = runtime.gc.plan(
            grant,
            capacity_policy=request.capacity_policy,
            protections=request.protections,
            monotonic_ns=monotonic_clock(),
        )
        if _preview_plan_projection(preview) != _preview_plan_projection(plan):
            raise GcPreviewPlanMismatch(
                "fresh fenced plan does not match the approved non-fence preview facts"
            )
        completion = runtime.gc.execute(
            grant,
            plan,
            capacity_policy=request.capacity_policy,
            protections=request.protections,
            occurred_at=occurred_at,
            monotonic_ns=monotonic_clock(),
        )
        if (
            not isinstance(completion, GcCompletionState)
            or completion.repo_uuid != request.repo_uuid
            or completion.plan_sha256 != plan.sha256
            or completion.quarantined != plan.candidates
        ):
            raise CommitUnknown("GC execute completed without a valid public receipt")
        return GcExecuteResult(
            repo_uuid=request.repo_uuid,
            request_sha256=request.request_sha256,
            approved_preview_sha256=request.approved_preview_sha256,
            plan_sha256=completion.plan_sha256,
            quarantined=completion.quarantined,
        )
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    finally:
        _release_grant(runtime, grant, "execute", primary)
    raise AssertionError("unreachable")


def reconcile_gc(
    runtime: WorkspaceRuntime,
    request: GcReconcileRequest,
    *,
    occurred_at: datetime,
    monotonic_clock: Callable[[], int],
) -> GcReconcileResult:
    """Explicitly reconcile only an existing durable GC intent."""

    request.authorization.require(IdentityAction.GC_RECONCILE)
    started_ns = monotonic_clock()
    deadline_ns = started_ns + request.timeout_ms * 1_000_000
    preflight = runtime.gc.preflight_lifecycle(
        request.repo_uuid,
        expected_registry_revision=request.expected_registry_revision,
        expected_active_source_revision=request.expected_active_source_revision,
        expected_operation_epoch=request.expected_operation_epoch,
        expected_migration_epoch=request.expected_migration_epoch,
        expected_pointer_revision=request.expected_pointer_revision,
        expected_capacity_policy_sha256=request.capacity_policy.sha256,
        deadline_ns=deadline_ns,
    )
    if preflight is None:
        return GcReconcileResult(
            repo_uuid=request.repo_uuid,
            request_sha256=request.request_sha256,
        )
    if not isinstance(preflight, GcIntentState):
        raise StateCorrupt("GC reconcile preflight returned an invalid record")
    grant = _acquire_gc(
        runtime,
        request,
        occurred_at=occurred_at,
        monotonic_ns=started_ns,
        deadline_ns=deadline_ns,
    )
    primary: tuple[BaseException, TracebackType | None] | None = None
    try:
        completion = runtime.gc.reconcile(
            grant,
            capacity_policy=request.capacity_policy,
            protections=request.protections,
            expected_pointer_revision=request.expected_pointer_revision,
            completed_at=occurred_at,
            monotonic_ns=monotonic_clock(),
        )
        if completion is None:
            return GcReconcileResult(
                repo_uuid=request.repo_uuid,
                request_sha256=request.request_sha256,
            )
        if not isinstance(completion, GcCompletionState) or (
            completion.repo_uuid != request.repo_uuid
        ):
            raise CommitUnknown("GC reconcile completed without a valid public receipt")
        return GcReconcileResult(
            repo_uuid=request.repo_uuid,
            request_sha256=request.request_sha256,
            plan_sha256=completion.plan_sha256,
            quarantined=completion.quarantined,
        )
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    finally:
        _release_grant(runtime, grant, "reconcile", primary)
    raise AssertionError("unreachable")


def purge_gc(
    runtime: WorkspaceRuntime,
    request: GcPurgeRequest,
    *,
    occurred_at: datetime,
    monotonic_clock: Callable[[], int],
) -> GcPurgeResult:
    """Explicitly purge one exact completed plan under fresh fenced authority."""

    request.authorization.require(IdentityAction.GC_PURGE)
    started_ns = monotonic_clock()
    deadline_ns = started_ns + request.timeout_ms * 1_000_000
    preflight = runtime.gc.preflight_lifecycle(
        request.repo_uuid,
        expected_registry_revision=request.expected_registry_revision,
        expected_active_source_revision=request.expected_active_source_revision,
        expected_operation_epoch=request.expected_operation_epoch,
        expected_migration_epoch=request.expected_migration_epoch,
        expected_pointer_revision=request.expected_pointer_revision,
        plan_sha256=request.expected_plan_sha256,
        deadline_ns=deadline_ns,
    )
    if isinstance(preflight, GcPurgeState):
        return GcPurgeResult(
            repo_uuid=request.repo_uuid,
            request_sha256=request.request_sha256,
            plan_sha256=preflight.plan_sha256,
            purged=preflight.purged,
        )
    if preflight is not None:
        raise StateCorrupt("GC purge preflight returned an invalid record")
    grant = _acquire_gc(
        runtime,
        request,
        occurred_at=occurred_at,
        monotonic_ns=started_ns,
        deadline_ns=deadline_ns,
    )
    primary: tuple[BaseException, TracebackType | None] | None = None
    try:
        purge = runtime.gc.purge(
            grant,
            plan_sha256=request.expected_plan_sha256,
            capacity_policy=request.capacity_policy,
            protections=request.protections,
            expected_pointer_revision=request.expected_pointer_revision,
            completed_at=occurred_at,
            monotonic_ns=monotonic_clock(),
        )
        if (
            not isinstance(purge, GcPurgeState)
            or purge.repo_uuid != request.repo_uuid
            or purge.plan_sha256 != request.expected_plan_sha256
        ):
            raise CommitUnknown("GC purge completed without a valid public receipt")
        return GcPurgeResult(
            repo_uuid=request.repo_uuid,
            request_sha256=request.request_sha256,
            plan_sha256=purge.plan_sha256,
            purged=purge.purged,
        )
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    finally:
        _release_grant(runtime, grant, "purge", primary)
    raise AssertionError("unreachable")


__all__ = [
    "GC_EXECUTE_REQUEST_CONTRACT",
    "GC_EXECUTE_RESULT_CONTRACT",
    "GC_LIFECYCLE_REQUEST_MAX_BYTES",
    "GC_LIFECYCLE_SCHEMA_VERSION",
    "GC_PURGE_REQUEST_CONTRACT",
    "GC_PURGE_RESULT_CONTRACT",
    "GC_RECONCILE_REQUEST_CONTRACT",
    "GC_RECONCILE_RESULT_CONTRACT",
    "GcApprovedPreviewMismatch",
    "GcExecuteRequest",
    "GcExecuteResult",
    "GcLifecycleFailure",
    "GcLifecycleRequestInvalid",
    "GcLifecycleRequestUnsupported",
    "GcPurgeRequest",
    "GcPurgeResult",
    "GcReconcileRequest",
    "GcReconcileResult",
    "classify_failure",
    "execute_gc",
    "gc_preview_result_bytes",
    "purge_gc",
    "reconcile_gc",
]
