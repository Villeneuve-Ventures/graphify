"""One-shot, operator-authorized rollback over the fenced pointer primitive."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from types import TracebackType
from typing import cast

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import WorkspaceAuthorityError, WorkspaceRuntime
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    STATE_SCHEMA_VERSION,
    ContractError,
    PointerSet,
    WorkspaceLeaseState,
    canonical_json_bytes,
)
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    OperatorAuthorization,
)
from graphify.workspace.generations import GenerationError
from graphify.workspace.journal import JournalError
from graphify.workspace.leases import (
    LeaseBusy,
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
    PointerCAS,
    PointerConflict,
    PointerCorrupt,
    PointerRecoveryRequired,
)
from graphify.workspace.registry import RevisionConflict
from graphify.workspace.status import EXIT_DEGRADED, EXIT_INVALID, EXIT_READY


ROLLBACK_REQUEST_CONTRACT = "graphify.workspace.rollback_request"
ROLLBACK_RECEIPT_CONTRACT = "graphify.workspace.rollback"
ROLLBACK_SCHEMA_VERSION = 1
ROLLBACK_REQUEST_MAX_BYTES = 16 * 1024
_ROLLBACK_LEASE_TTL_NS = 30_000_000_000
_MAX_INTEGER = 9_223_372_036_854_775_807
_MAX_EXPECTED_POINTER_REVISION = _MAX_INTEGER - 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$")
_REQUEST_FIELDS = frozenset(
    {
        "contract",
        "schema_version",
        "cli_contract_version",
        "repo_uuid",
        "expected_registry_revision",
        "expected_active_source_revision",
        "expected_operation_epoch",
        "expected_migration_epoch",
        "expected_pointer_revision",
        "expected_current_receipt_sha256",
        "target_generation_id",
        "target_receipt_sha256",
        "target_source_epoch",
        "authorization",
    }
)
_AUTHORIZATION_FIELDS = frozenset({"action", "issued_at", "nonce", "operator_id", "reason"})


class RollbackRequestInvalid(ValueError):
    """The public rollback request is malformed or noncanonical."""


class RollbackRequestUnsupported(RollbackRequestInvalid):
    """The public rollback request names an unsupported contract version."""


@dataclass(frozen=True)
class RollbackFailure:
    """Canonical, redacted public failure for the rollback transport."""

    state: str
    exit_code: int
    reason_code: str
    action_code: str

    @classmethod
    def missing_authority(cls) -> "RollbackFailure":
        return cls(
            "invalid",
            EXIT_INVALID,
            "runtime_authority_missing",
            "install_candidate_authority",
        )

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(
            {
                "action_code": self.action_code,
                "cli_contract_version": CLI_CONTRACT_VERSION,
                "contract": ROLLBACK_RECEIPT_CONTRACT,
                "exit_code": self.exit_code,
                "reason_code": self.reason_code,
                "schema_version": ROLLBACK_SCHEMA_VERSION,
                "state": self.state,
            }
        )


def classify_failure(error: Exception) -> RollbackFailure:
    """Map an internal rollback failure to its redacted public contract."""

    if isinstance(error, WorkspaceAuthorityError):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, RollbackRequestUnsupported):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "rollback_request_unsupported",
            "use_supported_rollback_contract",
        )
    if isinstance(error, (ValueError, AuthorizationError)):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "rollback_request_invalid",
            "provide_valid_rollback_request",
        )
    if isinstance(error, (LeaseBusy, LockTimeout)):
        return RollbackFailure(
            "conflict",
            EXIT_DEGRADED,
            "lease_busy",
            "retry_workspace_rollback",
        )
    if isinstance(error, (RevisionConflict, PointerConflict, LeaseExpired, StaleLease)):
        return RollbackFailure(
            "conflict",
            EXIT_DEGRADED,
            "rollback_authority_conflict",
            "refresh_rollback_request",
        )
    if isinstance(
        error,
        (LeaseRecoveryRequired, PointerRecoveryRequired, StateRecoveryRequired),
    ):
        return RollbackFailure(
            "conflict",
            EXIT_DEGRADED,
            "workspace_recovery_required",
            "run_workspace_doctor",
        )
    if isinstance(error, CommitUnknown):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            "run_workspace_doctor",
        )
    if isinstance(error, StatePathError):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, (StateCorrupt, PointerCorrupt, GenerationError, JournalError)):
        return RollbackFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_repair",
        )
    return RollbackFailure(
        "invalid",
        EXIT_INVALID,
        "rollback_failed",
        "run_workspace_doctor",
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackRequestInvalid("rollback request contains a duplicate key")
        result[key] = value
    return result


def _integer(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int = _MAX_INTEGER,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside the supported range")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical SHA-256 digest")
    return value


@dataclass(frozen=True)
class RollbackRequest:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    expected_pointer_revision: int
    expected_current_receipt_sha256: str
    target_generation_id: str
    target_receipt_sha256: str
    target_source_epoch: int
    authorization: OperatorAuthorization

    def to_dict(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.to_dict(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": ROLLBACK_REQUEST_CONTRACT,
            "expected_active_source_revision": self.expected_active_source_revision,
            "expected_current_receipt_sha256": self.expected_current_receipt_sha256,
            "expected_migration_epoch": self.expected_migration_epoch,
            "expected_operation_epoch": self.expected_operation_epoch,
            "expected_pointer_revision": self.expected_pointer_revision,
            "expected_registry_revision": self.expected_registry_revision,
            "repo_uuid": self.repo_uuid,
            "schema_version": ROLLBACK_SCHEMA_VERSION,
            "target_generation_id": self.target_generation_id,
            "target_receipt_sha256": self.target_receipt_sha256,
            "target_source_epoch": self.target_source_epoch,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    @classmethod
    def from_bytes(cls, value: bytes) -> "RollbackRequest":
        if not isinstance(value, bytes) or len(value) > ROLLBACK_REQUEST_MAX_BYTES:
            raise RollbackRequestInvalid("rollback request exceeds the byte limit")
        try:
            data = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
            raise RollbackRequestInvalid(
                "rollback request is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(data, Mapping) or set(data) != _REQUEST_FIELDS:
            raise RollbackRequestInvalid("rollback request fields are invalid")
        contract = data["contract"]
        if not isinstance(contract, str):
            raise RollbackRequestInvalid("rollback request contract is invalid")
        if contract != ROLLBACK_REQUEST_CONTRACT:
            raise RollbackRequestUnsupported("rollback request contract is unsupported")
        for field, expected in (
            ("schema_version", ROLLBACK_SCHEMA_VERSION),
            ("cli_contract_version", CLI_CONTRACT_VERSION),
        ):
            version = data[field]
            if isinstance(version, bool) or not isinstance(version, int):
                raise RollbackRequestInvalid(f"rollback request {field} is invalid")
            if version != expected:
                raise RollbackRequestUnsupported(
                    f"rollback request {field} is unsupported"
                )
        try:
            repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(data["repo_uuid"])
        except ContractError as exc:
            raise RollbackRequestInvalid("rollback request repo_uuid is invalid") from exc
        generation_id = data["target_generation_id"]
        if not isinstance(generation_id, str) or _GENERATION_RE.fullmatch(generation_id) is None:
            raise RollbackRequestInvalid(
                "rollback request target_generation_id is invalid"
            )
        authorization_value = data["authorization"]
        if not isinstance(authorization_value, Mapping) or set(authorization_value) != _AUTHORIZATION_FIELDS:
            raise RollbackRequestInvalid(
                "rollback request authorization fields are invalid"
            )
        if not all(isinstance(authorization_value[field], str) for field in _AUTHORIZATION_FIELDS):
            raise RollbackRequestInvalid(
                "rollback request authorization fields must be strings"
            )
        if authorization_value["action"] != IdentityAction.ROLLBACK.value:
            raise RollbackRequestInvalid(
                "rollback request authorization action is invalid"
            )
        try:
            authorization = OperatorAuthorization(
                action=IdentityAction.ROLLBACK,
                issued_at=cast(str, authorization_value["issued_at"]),
                nonce=cast(str, authorization_value["nonce"]),
                operator_id=cast(str, authorization_value["operator_id"]),
                reason=cast(str, authorization_value["reason"]),
            )
        except AuthorizationError as exc:
            raise RollbackRequestInvalid(
                "rollback request authorization is invalid"
            ) from exc
        try:
            request = cls(
                repo_uuid=repo_uuid,
                expected_registry_revision=_integer(data["expected_registry_revision"], "expected_registry_revision", minimum=1),
                expected_active_source_revision=_integer(data["expected_active_source_revision"], "expected_active_source_revision", minimum=1),
                expected_operation_epoch=_integer(data["expected_operation_epoch"], "expected_operation_epoch", minimum=1),
                expected_migration_epoch=_integer(data["expected_migration_epoch"], "expected_migration_epoch", minimum=0),
                expected_pointer_revision=_integer(
                    data["expected_pointer_revision"],
                    "expected_pointer_revision",
                    minimum=1,
                    maximum=_MAX_EXPECTED_POINTER_REVISION,
                ),
                expected_current_receipt_sha256=_digest(data["expected_current_receipt_sha256"], "expected_current_receipt_sha256"),
                target_generation_id=generation_id,
                target_receipt_sha256=_digest(data["target_receipt_sha256"], "target_receipt_sha256"),
                target_source_epoch=_integer(data["target_source_epoch"], "target_source_epoch", minimum=1),
                authorization=authorization,
            )
        except ValueError as exc:
            raise RollbackRequestInvalid(
                "rollback request fields are invalid"
            ) from exc
        if request.canonical != value:
            raise RollbackRequestInvalid("rollback request is not canonical JSON")
        return request


@dataclass(frozen=True)
class RollbackReceipt:
    repo_uuid: str
    request_sha256: str
    target_generation_id: str
    target_receipt_sha256: str
    pointer_revision: int

    def __post_init__(self) -> None:
        try:
            if WorkspaceLeaseState.canonical_repo_uuid(self.repo_uuid) != self.repo_uuid:
                raise ValueError("noncanonical")
        except ContractError as exc:
            raise ValueError("rollback receipt repo_uuid is invalid") from exc
        if _DIGEST_RE.fullmatch(self.request_sha256) is None:
            raise ValueError("rollback receipt request_sha256 is invalid")
        if _GENERATION_RE.fullmatch(self.target_generation_id) is None:
            raise ValueError("rollback receipt target_generation_id is invalid")
        if _DIGEST_RE.fullmatch(self.target_receipt_sha256) is None:
            raise ValueError("rollback receipt target_receipt_sha256 is invalid")
        _integer(self.pointer_revision, "pointer_revision", minimum=1)

    def to_dict(self) -> dict[str, object]:
        return {
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": ROLLBACK_RECEIPT_CONTRACT,
            "exit_code": EXIT_READY,
            "pointer_revision": self.pointer_revision,
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": ROLLBACK_SCHEMA_VERSION,
            "state": "rolled_back",
            "target_generation_id": self.target_generation_id,
            "target_receipt_sha256": self.target_receipt_sha256,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def _pointer_value(pointer: PointerSet) -> Mapping[str, object]:
    value = pointer.to_dict()
    if not isinstance(value, Mapping):
        raise StateCorrupt("rollback pointer is invalid")
    return value


def _preflight_pointer(request: RollbackRequest, pointer: PointerSet) -> None:
    value = _pointer_value(pointer)
    if "last_good" in value and value["last_good"] is None:
        raise RevisionConflict("rollback requires a live last_good pointer target")
    try:
        revision = _integer(value["pointer_revision"], "pointer_revision", minimum=1)
        current = cast(Mapping[str, object], value["current"])
        last_good = cast(Mapping[str, object], value["last_good"])
        actual = (
            revision,
            current["receipt_sha256"],
            last_good["generation_id"],
            last_good["receipt_sha256"],
        )
        current_ref = (current["generation_id"], current["receipt_sha256"])
        last_good_ref = (last_good["generation_id"], last_good["receipt_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StateCorrupt("rollback pointer is invalid") from exc
    if current_ref == last_good_ref:
        raise StateCorrupt("rollback current and last_good pointers must be distinct")
    expected = (
        request.expected_pointer_revision,
        request.expected_current_receipt_sha256,
        request.target_generation_id,
        request.target_receipt_sha256,
    )
    if actual != expected:
        raise RevisionConflict("rollback request does not match live pointer current or last_good")


def _verify_target_receipt_authority(
    runtime: WorkspaceRuntime,
    request: RollbackRequest,
    pointer: PointerSet,
    *,
    deadline_ns: int | None = None,
) -> None:
    receipts = runtime.pointers.verify_visible_pointer(
        pointer,
        expected_repo_uuid=request.repo_uuid,
        deadline_ns=deadline_ns,
    )
    try:
        target_receipt = receipts["last_good"]
    except KeyError as exc:
        raise StateCorrupt("rollback last_good receipt is invalid") from exc
    if target_receipt is None:
        raise RevisionConflict("rollback requires a live last_good pointer target")
    try:
        target = target_receipt.to_dict()
        source_epoch = _integer(
            target["source_epoch"],
            "target_source_epoch",
            minimum=1,
        )
        active_source_revision = _integer(
            target["active_source_revision"],
            "target_active_source_revision",
            minimum=1,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StateCorrupt("rollback last_good receipt is invalid") from exc
    if source_epoch != request.target_source_epoch:
        raise RevisionConflict("rollback target source epoch is stale")
    if active_source_revision != request.expected_active_source_revision:
        raise RevisionConflict("rollback target active source revision is stale")


def _release_grant(
    runtime: WorkspaceRuntime,
    grant: LeaseGrant,
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
                "rollback lease release outcome is uncertain"
            ) from exc
    if primary is not None:
        error, traceback = primary
        raise error.with_traceback(traceback)


def rollback(
    runtime: WorkspaceRuntime,
    request: RollbackRequest,
    *,
    occurred_at: datetime,
    monotonic_clock: Callable[[], int],
) -> RollbackReceipt:
    """Atomically move the visible pointer to its exact live ``last_good`` ref."""

    request.authorization.require(IdentityAction.ROLLBACK)
    pointer = runtime.pointers.load(request.repo_uuid)
    if pointer is None:
        raise StateCorrupt("rollback requires a live pointer")
    _preflight_pointer(request, pointer)
    _verify_target_receipt_authority(runtime, request, pointer)
    acquisition_monotonic_ns = monotonic_clock()
    acquisition_deadline_ns = acquisition_monotonic_ns + _ROLLBACK_LEASE_TTL_NS
    grant = runtime.leases.acquire(
        request.repo_uuid,
        "ROLLBACK",
        runtime.leases.current_owner(),
        expected_registry_revision=request.expected_registry_revision,
        expected_active_source_revision=request.expected_active_source_revision,
        expected_operation_epoch=request.expected_operation_epoch,
        expected_migration_epoch=request.expected_migration_epoch,
        acquired_at=occurred_at,
        monotonic_ns=acquisition_monotonic_ns,
        ttl_ns=_ROLLBACK_LEASE_TTL_NS,
        deadline_ns=acquisition_deadline_ns,
    )
    primary: tuple[BaseException, TracebackType | None] | None = None
    try:
        try:
            grant_value = grant.lease.to_dict()
            deadline_ns = _integer(
                grant_value["liveness_deadline_monotonic_ns"],
                "liveness_deadline_monotonic_ns",
                minimum=1,
            )
            fence_token = _integer(
                grant_value["fence_token"],
                "fence_token",
                minimum=1,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise StateCorrupt("rollback lease grant is invalid") from exc
        try:
            current = runtime.pointers.load(
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if current is None:
                raise StateCorrupt("rollback requires a live pointer")
            _preflight_pointer(request, current)
            _verify_target_receipt_authority(
                runtime,
                request,
                current,
                deadline_ns=deadline_ns,
            )
            cas = PointerCAS(
                expected_pointer_revision=request.expected_pointer_revision,
                expected_active_source_revision=grant.active_source_revision,
                expected_source_epoch=request.target_source_epoch,
                expected_operation_epoch=grant.operation_epoch,
                expected_migration_epoch=grant.migration_epoch,
                expected_state_schema_version=STATE_SCHEMA_VERSION,
                expected_fence_token=fence_token,
                candidate_generation_id=request.target_generation_id,
                candidate_receipt_sha256=request.target_receipt_sha256,
                expected_current_receipt_sha256=request.expected_current_receipt_sha256,
            )
            result = runtime.pointers.rollback(
                grant,
                cas,
                occurred_at=occurred_at,
                monotonic_ns=monotonic_clock(),
                deadline_ns=deadline_ns,
            )
        except LockTimeout as exc:
            raise RevisionConflict(
                "rollback lease advanced; refresh the rollback request"
            ) from exc
        try:
            result_value = _pointer_value(result)
            result_current = cast(Mapping[str, object], result_value["current"])
            revision = _integer(
                result_value["pointer_revision"],
                "pointer_revision",
                minimum=1,
            )
            if (
                result_current.get("generation_id") != request.target_generation_id
                or result_current.get("receipt_sha256")
                != request.target_receipt_sha256
                or revision != request.expected_pointer_revision + 1
            ):
                raise PointerConflict(
                    "rollback result does not bind the requested target"
                )
            receipt = RollbackReceipt(
                repo_uuid=request.repo_uuid,
                request_sha256=request.request_sha256,
                target_generation_id=request.target_generation_id,
                target_receipt_sha256=request.target_receipt_sha256,
                pointer_revision=revision,
            )
        except Exception as exc:
            raise CommitUnknown(
                "rollback completed without a valid canonical receipt"
            ) from exc
        return receipt
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    finally:
        _release_grant(runtime, grant, primary)
    raise AssertionError("unreachable")
