"""Bounded public preview and execution for fenced pointer repair.

The public request approves canonical, redacted preview bytes.  Execution first
reproduces those bytes from the caller's pre-acquisition authority, then passes
the same deterministic pointer decision into :class:`PointerStore` for an exact
comparison under the repair lease and mutation locks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from types import TracebackType
from typing import Any, ClassVar, cast

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import WorkspaceAuthorityError, WorkspaceRuntime
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    ContractError,
    PointerSet,
    WorkspaceLeaseState,
    canonical_json_bytes,
)
from graphify.workspace.generations import GenerationError, GenerationStore
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    OperatorAuthorization,
)
from graphify.workspace.journal import JournalError, JournalStore
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseError,
    LeaseExpired,
    LeaseGrant,
    LeaseRecoveryRequired,
    LeaseStore,
    StaleLease,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    StateRecoveryRequired,
    UnsupportedRuntime,
)
from graphify.workspace.pointers import (
    PointerConflict,
    PointerCorrupt,
    PointerRepairPlan,
    PointerRecoveryRequired,
    PointerStore,
)
from graphify.workspace.registry import RegistryStore, RevisionConflict
from graphify.workspace.semantic_queue import SemanticQueueError
from graphify.workspace.status import EXIT_DEGRADED, EXIT_INVALID, EXIT_READY


REPAIR_PREVIEW_REQUEST_CONTRACT = "graphify.workspace.repair_preview_request"
REPAIR_PREVIEW_RESULT_CONTRACT = "graphify.workspace.repair_preview_result"
REPAIR_EXECUTE_REQUEST_CONTRACT = "graphify.workspace.repair_execute_request"
REPAIR_EXECUTE_RESULT_CONTRACT = "graphify.workspace.repair_execute_result"
REPAIR_SCHEMA_VERSION = 1
REPAIR_REQUEST_MAX_BYTES = 16 * 1024
REPAIR_RESULT_MAX_BYTES = 16 * 1024
REPAIR_TIMEOUT_MAX_MS = 60_000

_REPAIR_LEASE_TTL_NS = 30_000_000_000
_MAX_SIGNED_INTEGER = 9_223_372_036_854_775_807
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$")
_PLAN_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AUTHORIZATION_FIELDS = frozenset({"action", "issued_at", "nonce", "operator_id", "reason"})
_PREVIEW_REQUEST_FIELDS = frozenset(
    {
        "cli_contract_version",
        "contract",
        "expected_active_source_revision",
        "expected_migration_epoch",
        "expected_operation_epoch",
        "expected_registry_revision",
        "repo_uuid",
        "schema_version",
        "timeout_ms",
    }
)
_EXECUTE_REQUEST_FIELDS = _PREVIEW_REQUEST_FIELDS | frozenset(
    {"approved_preview_sha256", "authorization"}
)


class RepairError(RuntimeError):
    """Base class for stable, transport-independent repair failures."""

    code = "repair_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class RepairPreviewRequestInvalid(ValueError):
    """A preview request is malformed or not canonical JSON."""


class RepairPreviewRequestUnsupported(RepairPreviewRequestInvalid):
    """A preview request names an unsupported public contract version."""


class RepairExecuteRequestInvalid(ValueError):
    """An execute request is malformed or not canonical JSON."""


class RepairExecuteRequestUnsupported(RepairExecuteRequestInvalid):
    """An execute request names an unsupported public contract version."""


class RepairConflict(RepairError):
    code = "repair_conflict"


class RepairAuthorityConflict(RepairConflict):
    code = "repair_authority_conflict"


class RepairPlanChanged(RepairConflict):
    code = "repair_plan_changed"


class RepairIrreparable(RepairError):
    code = "repair_irreparable"


class RepairCommitUnknown(CommitUnknown):
    """Repair may have crossed a durable boundary; a fresh preview is required."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("repair request has a duplicate key")
        result[key] = value
    return result


def _integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SIGNED_INTEGER:
        raise ValueError(f"repair request {label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"repair request {label} is invalid")
    return value


def _repo_uuid(value: object) -> str:
    try:
        return WorkspaceLeaseState.canonical_repo_uuid(value)
    except ContractError as exc:
        raise ValueError("repair request repo_uuid is invalid") from exc


def _parse_json_request(
    value: bytes,
    *,
    label: str,
    fields: frozenset[str],
    contract: str,
    invalid_type: type[ValueError],
    unsupported_type: type[ValueError],
) -> Mapping[str, object]:
    if not isinstance(value, bytes) or len(value) > REPAIR_REQUEST_MAX_BYTES:
        raise invalid_type(f"{label} exceeds the byte limit")
    try:
        data = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise invalid_type(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(data, Mapping) or set(data) != fields:
        raise invalid_type(f"{label} fields are invalid")
    if not isinstance(data["contract"], str):
        raise invalid_type(f"{label} contract is invalid")
    if data["contract"] != contract:
        raise unsupported_type(f"{label} contract is unsupported")
    for field_name, expected in (
        ("schema_version", REPAIR_SCHEMA_VERSION),
        ("cli_contract_version", CLI_CONTRACT_VERSION),
    ):
        version = data[field_name]
        if type(version) is not int:
            raise invalid_type(f"{label} {field_name} is invalid")
        if version != expected:
            raise unsupported_type(f"{label} {field_name} is unsupported")
    return data


@dataclass(frozen=True)
class RepairAuthorization:
    """The exact five-field authorization accepted by public repair execute."""

    action: str | IdentityAction
    operator_id: str
    reason: str
    issued_at: str
    nonce: str
    _authorization: OperatorAuthorization = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        action = self.action.value if isinstance(self.action, IdentityAction) else self.action
        if action != IdentityAction.REPAIR_EXECUTE.value:
            raise AuthorizationError("repair execute requires REPAIR_EXECUTE authorization")
        authorization = OperatorAuthorization(
            action=IdentityAction.REPAIR_EXECUTE,
            operator_id=self.operator_id,
            reason=self.reason,
            issued_at=self.issued_at,
            nonce=self.nonce,
        )
        object.__setattr__(self, "action", IdentityAction.REPAIR_EXECUTE.value)
        object.__setattr__(self, "_authorization", authorization)

    def require(self, expected: IdentityAction = IdentityAction.REPAIR_EXECUTE) -> None:
        cast(OperatorAuthorization, self._authorization).require(expected)

    def to_dict(self) -> dict[str, str]:
        return cast(OperatorAuthorization, self._authorization).to_dict()

    @property
    def sha256(self) -> str:
        return cast(OperatorAuthorization, self._authorization).sha256


@dataclass(frozen=True)
class RepairPreviewRequest:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    timeout_ms: int | None = None
    timeout_ns: int | None = field(default=None, repr=False, compare=False)

    CONTRACT: ClassVar[str] = REPAIR_PREVIEW_REQUEST_CONTRACT

    def __post_init__(self) -> None:
        try:
            repo_uuid = _repo_uuid(self.repo_uuid)
            _integer(
                self.expected_registry_revision,
                "expected_registry_revision",
                minimum=1,
            )
            _integer(
                self.expected_active_source_revision,
                "expected_active_source_revision",
                minimum=1,
            )
            _integer(
                self.expected_operation_epoch,
                "expected_operation_epoch",
                minimum=0,
            )
            _integer(
                self.expected_migration_epoch,
                "expected_migration_epoch",
                minimum=0,
            )
            if self.timeout_ms is None and self.timeout_ns is None:
                raise ValueError("repair request timeout is missing")
            if self.timeout_ms is not None and self.timeout_ns is not None:
                timeout_ms = _integer(self.timeout_ms, "timeout_ms", minimum=1)
                duration_ns = _integer(self.timeout_ns, "timeout_ns", minimum=1)
                if (
                    timeout_ms > REPAIR_TIMEOUT_MAX_MS
                    or duration_ns > REPAIR_TIMEOUT_MAX_MS * 1_000_000
                    or timeout_ms != max(1, (duration_ns + 999_999) // 1_000_000)
                ):
                    raise ValueError("repair request timeout units are inconsistent")
            elif self.timeout_ms is not None:
                timeout_ms = _integer(self.timeout_ms, "timeout_ms", minimum=1)
                if timeout_ms > REPAIR_TIMEOUT_MAX_MS:
                    raise ValueError("repair request timeout_ms is invalid")
                duration_ns = timeout_ms * 1_000_000
            else:
                duration_ns = _integer(self.timeout_ns, "timeout_ns", minimum=1)
                if duration_ns > REPAIR_TIMEOUT_MAX_MS * 1_000_000:
                    raise ValueError("repair request timeout_ns is invalid")
                timeout_ms = max(1, (duration_ns + 999_999) // 1_000_000)
        except ValueError as exc:
            raise RepairPreviewRequestInvalid("repair preview request is invalid") from exc
        object.__setattr__(self, "repo_uuid", repo_uuid)
        object.__setattr__(self, "timeout_ms", timeout_ms)
        object.__setattr__(self, "timeout_ns", duration_ns)

    def to_dict(self) -> dict[str, object]:
        return {
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": self.CONTRACT,
            "expected_active_source_revision": self.expected_active_source_revision,
            "expected_migration_epoch": self.expected_migration_epoch,
            "expected_operation_epoch": self.expected_operation_epoch,
            "expected_registry_revision": self.expected_registry_revision,
            "repo_uuid": self.repo_uuid,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "timeout_ms": self.timeout_ms,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    def deadline_from(self, started_ns: int | None = None) -> int:
        if started_ns is None:
            started_ns = time.monotonic_ns()
        if type(started_ns) is not int or started_ns < 0:
            raise ValueError("repair deadline start must be a non-negative integer")
        return started_ns + cast(int, self.timeout_ns)

    def runtime_deadline(self) -> int | None:
        """Return the public deadline, or ``None`` for synthetic test-clock inputs."""

        if self.timeout_ns != cast(int, self.timeout_ms) * 1_000_000:
            return None
        return self.deadline_from()

    @classmethod
    def from_bytes(cls, value: bytes) -> "RepairPreviewRequest":
        try:
            data = _parse_json_request(
                value,
                label="repair preview request",
                fields=_PREVIEW_REQUEST_FIELDS,
                contract=cls.CONTRACT,
                invalid_type=RepairPreviewRequestInvalid,
                unsupported_type=RepairPreviewRequestUnsupported,
            )
            request = cls(
                repo_uuid=_repo_uuid(data["repo_uuid"]),
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
                timeout_ms=_integer(data["timeout_ms"], "timeout_ms", minimum=1),
            )
        except RepairPreviewRequestUnsupported:
            raise
        except RepairPreviewRequestInvalid:
            raise
        except (ContractError, TypeError, ValueError) as exc:
            raise RepairPreviewRequestInvalid("repair preview request is invalid") from exc
        if request.canonical != value:
            raise RepairPreviewRequestInvalid("repair preview request is not canonical JSON")
        return request


@dataclass(frozen=True)
class RepairExecuteRequest:
    repo_uuid: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    approved_preview_sha256: str
    authorization: RepairAuthorization
    timeout_ms: int | None = None
    timeout_ns: int | None = field(default=None, repr=False, compare=False)

    CONTRACT: ClassVar[str] = REPAIR_EXECUTE_REQUEST_CONTRACT

    def __post_init__(self) -> None:
        try:
            preview = RepairPreviewRequest(
                repo_uuid=self.repo_uuid,
                expected_registry_revision=self.expected_registry_revision,
                expected_active_source_revision=self.expected_active_source_revision,
                expected_operation_epoch=self.expected_operation_epoch,
                expected_migration_epoch=self.expected_migration_epoch,
                timeout_ms=self.timeout_ms,
                timeout_ns=self.timeout_ns,
            )
            digest = _digest(
                self.approved_preview_sha256,
                "approved_preview_sha256",
            )
            if not isinstance(self.authorization, RepairAuthorization):
                raise ValueError("repair execute authorization is invalid")
            self.authorization.require()
        except (AuthorizationError, RepairPreviewRequestInvalid, ValueError) as exc:
            raise RepairExecuteRequestInvalid("repair execute request is invalid") from exc
        object.__setattr__(self, "repo_uuid", preview.repo_uuid)
        object.__setattr__(self, "timeout_ms", preview.timeout_ms)
        object.__setattr__(self, "timeout_ns", preview.timeout_ns)
        object.__setattr__(self, "approved_preview_sha256", digest)

    @property
    def preview_request(self) -> RepairPreviewRequest:
        return RepairPreviewRequest(
            repo_uuid=self.repo_uuid,
            expected_registry_revision=self.expected_registry_revision,
            expected_active_source_revision=self.expected_active_source_revision,
            expected_operation_epoch=self.expected_operation_epoch,
            expected_migration_epoch=self.expected_migration_epoch,
            timeout_ms=self.timeout_ms,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "approved_preview_sha256": self.approved_preview_sha256,
            "authorization": self.authorization.to_dict(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": self.CONTRACT,
            "expected_active_source_revision": self.expected_active_source_revision,
            "expected_migration_epoch": self.expected_migration_epoch,
            "expected_operation_epoch": self.expected_operation_epoch,
            "expected_registry_revision": self.expected_registry_revision,
            "repo_uuid": self.repo_uuid,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "timeout_ms": self.timeout_ms,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    @classmethod
    def from_bytes(cls, value: bytes) -> "RepairExecuteRequest":
        try:
            data = _parse_json_request(
                value,
                label="repair execute request",
                fields=_EXECUTE_REQUEST_FIELDS,
                contract=cls.CONTRACT,
                invalid_type=RepairExecuteRequestInvalid,
                unsupported_type=RepairExecuteRequestUnsupported,
            )
            authorization_value = data["authorization"]
            if (
                not isinstance(authorization_value, Mapping)
                or set(authorization_value) != _AUTHORIZATION_FIELDS
            ):
                raise ValueError("repair execute request authorization is invalid")
            if not all(
                isinstance(authorization_value[field_name], str)
                for field_name in _AUTHORIZATION_FIELDS
            ):
                raise ValueError("repair execute request authorization is invalid")
            authorization = RepairAuthorization(
                action=cast(str, authorization_value["action"]),
                issued_at=cast(str, authorization_value["issued_at"]),
                nonce=cast(str, authorization_value["nonce"]),
                operator_id=cast(str, authorization_value["operator_id"]),
                reason=cast(str, authorization_value["reason"]),
            )
            request = cls(
                repo_uuid=_repo_uuid(data["repo_uuid"]),
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
                timeout_ms=_integer(data["timeout_ms"], "timeout_ms", minimum=1),
                approved_preview_sha256=_digest(
                    data["approved_preview_sha256"],
                    "approved_preview_sha256",
                ),
                authorization=authorization,
            )
        except RepairExecuteRequestUnsupported:
            raise
        except RepairExecuteRequestInvalid:
            raise
        except (AuthorizationError, ContractError, TypeError, ValueError) as exc:
            raise RepairExecuteRequestInvalid("repair execute request is invalid") from exc
        if request.canonical != value:
            raise RepairExecuteRequestInvalid("repair execute request is not canonical JSON")
        return request


def _reference(value: object, label: str, *, allow_none: bool) -> dict[str, str] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "generation_id",
        "receipt_sha256",
    }:
        raise ValueError(f"repair {label} reference is invalid")
    generation_id = value["generation_id"]
    receipt_sha256 = value["receipt_sha256"]
    if not isinstance(generation_id, str) or _GENERATION_RE.fullmatch(generation_id) is None:
        raise ValueError(f"repair {label} generation is invalid")
    if not isinstance(receipt_sha256, str) or _DIGEST_RE.fullmatch(receipt_sha256) is None:
        raise ValueError(f"repair {label} receipt is invalid")
    return {
        "generation_id": generation_id,
        "receipt_sha256": receipt_sha256,
    }


@dataclass(frozen=True)
class RepairPlan:
    """The bounded redacted projection of one internal pointer repair decision."""

    classification: str
    candidate: dict[str, str] | None
    last_good: dict[str, str] | None
    next_pointer_revision: int
    selected_from: str | None
    pointer_action: str
    journal_actions: tuple[str, ...]
    quarantine: tuple[str, ...]
    decision_sha256: str
    decision: PointerRepairPlan | None = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.classification not in {"no_op", "repairable", "irreparable"}:
            raise ValueError("repair classification is invalid")
        candidate = _reference(
            self.candidate,
            "candidate",
            allow_none=self.classification == "irreparable",
        )
        last_good = _reference(self.last_good, "last_good", allow_none=True)
        revision = _integer(
            self.next_pointer_revision,
            "next_pointer_revision",
            minimum=0,
        )
        decision_sha256 = _digest(self.decision_sha256, "decision_sha256")
        if self.selected_from is not None and self.selected_from not in {
            "current",
            "pending",
            "prior",
            "last_good",
            "none",
        }:
            raise ValueError("repair selected source is invalid")
        if _PLAN_ACTION_RE.fullmatch(self.pointer_action) is None:
            raise ValueError("repair pointer action is invalid")
        if (
            len(self.journal_actions) > 8
            or len(self.journal_actions) != len(set(self.journal_actions))
            or any(_PLAN_ACTION_RE.fullmatch(action) is None for action in self.journal_actions)
        ):
            raise ValueError("repair journal actions are invalid")
        if (
            len(self.quarantine) > 8
            or list(self.quarantine) != sorted(self.quarantine)
            or len(self.quarantine) != len(set(self.quarantine))
            or any(_GENERATION_RE.fullmatch(item) is None for item in self.quarantine)
        ):
            raise ValueError("repair quarantine set is invalid")
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "last_good", last_good)
        object.__setattr__(self, "next_pointer_revision", revision)
        object.__setattr__(self, "decision_sha256", decision_sha256)

    @classmethod
    def from_decision(cls, decision: PointerRepairPlan) -> "RepairPlan":
        try:
            value = cast(Any, decision).to_dict()
        except (AttributeError, TypeError, ValueError) as exc:
            raise StateCorrupt("pointer repair analysis returned an invalid plan") from exc
        if not isinstance(value, Mapping):
            raise StateCorrupt("pointer repair analysis returned an invalid plan")
        classification_value = value.get("classification")
        if isinstance(classification_value, str):
            classification = classification_value
        else:
            pointer_action = value.get("pointer_action")
            classification = (
                "no_op"
                if pointer_action in {"none", "noop"}
                and not value.get("journal_actions")
                and not value.get("quarantine")
                else "repairable"
            )
        try:
            journal_actions = tuple(cast(list[str] | tuple[str, ...], value["journal_actions"]))
            quarantine = tuple(cast(list[str] | tuple[str, ...], value["quarantine"]))
            return cls(
                classification=classification,
                candidate=cast(dict[str, str] | None, value["candidate"]),
                last_good=cast(dict[str, str] | None, value["last_good"]),
                next_pointer_revision=cast(int, value["next_pointer_revision"]),
                selected_from=cast(str | None, value["selected_from"]),
                pointer_action=cast(str, value["pointer_action"]),
                journal_actions=journal_actions,
                quarantine=quarantine,
                decision_sha256=decision.decision_sha256,
                decision=decision,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateCorrupt("pointer repair analysis returned an invalid plan") from exc

    @classmethod
    def irreparable(cls) -> "RepairPlan":
        neutral = {
            "candidate": None,
            "classification": "irreparable",
            "journal_actions": [],
            "last_good": None,
            "next_pointer_revision": 0,
            "pointer_action": "none",
            "quarantine": [],
            "selected_from": "none",
        }
        return cls(
            classification="irreparable",
            candidate=None,
            last_good=None,
            next_pointer_revision=0,
            selected_from="none",
            pointer_action="none",
            journal_actions=(),
            quarantine=(),
            decision_sha256=hashlib.sha256(canonical_json_bytes(neutral)).hexdigest(),
            decision=None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "classification": self.classification,
            "decision_sha256": self.decision_sha256,
            "journal_actions": list(self.journal_actions),
            "last_good": self.last_good,
            "next_pointer_revision": self.next_pointer_revision,
            "pointer_action": self.pointer_action,
            "quarantine": list(self.quarantine),
            "selected_from": self.selected_from,
        }

    def public_plan_dict(self) -> dict[str, object]:
        value = self.to_dict()
        del value["classification"]
        return value

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()


@dataclass(frozen=True)
class RepairObservedAuthority:
    registry_revision: int
    active_source_revision: int
    operation_epoch: int
    migration_epoch: int

    @classmethod
    def from_request(cls, request: RepairPreviewRequest) -> "RepairObservedAuthority":
        return cls(
            registry_revision=request.expected_registry_revision,
            active_source_revision=request.expected_active_source_revision,
            operation_epoch=request.expected_operation_epoch,
            migration_epoch=request.expected_migration_epoch,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "active_source_revision": self.active_source_revision,
            "migration_epoch": self.migration_epoch,
            "operation_epoch": self.operation_epoch,
            "registry_revision": self.registry_revision,
        }


@dataclass(frozen=True)
class RepairPreviewResult:
    repo_uuid: str
    request_sha256: str
    observed_authority: RepairObservedAuthority
    plan: RepairPlan

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.plan.classification,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": REPAIR_PREVIEW_RESULT_CONTRACT,
            "observed_authority": self.observed_authority.to_dict(),
            "plan": self.plan.public_plan_dict(),
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "state": "previewed",
        }

    @property
    def canonical(self) -> bytes:
        value = canonical_json_bytes(self.to_dict())
        if len(value) > REPAIR_RESULT_MAX_BYTES:
            raise ValueError("repair preview result exceeds the public byte limit")
        return value

    @property
    def exit_code(self) -> int:
        return EXIT_INVALID if self.plan.classification == "irreparable" else EXIT_READY

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()


def _public_preview_result(
    request: RepairPreviewRequest,
    plan: RepairPlan,
) -> RepairPreviewResult:
    return RepairPreviewResult(
        repo_uuid=request.repo_uuid,
        request_sha256=request.request_sha256,
        observed_authority=RepairObservedAuthority.from_request(request),
        plan=plan,
    )


@dataclass(frozen=True)
class RepairExecution:
    plan: RepairPlan
    pointer: PointerSet | None

    def to_dict(self) -> dict[str, object]:
        return self.plan.to_dict()


@dataclass(frozen=True)
class RepairExecuteResult:
    repo_uuid: str
    request_sha256: str
    approved_preview_sha256: str
    current: dict[str, str]
    last_good: dict[str, str] | None
    pointer_revision: int
    state: str

    def __post_init__(self) -> None:
        _repo_uuid(self.repo_uuid)
        _digest(self.request_sha256, "request_sha256")
        _digest(self.approved_preview_sha256, "approved_preview_sha256")
        _reference(self.current, "current", allow_none=False)
        _reference(self.last_good, "last_good", allow_none=True)
        _integer(self.pointer_revision, "pointer_revision", minimum=1)
        if self.state not in {"no_op", "repaired"}:
            raise ValueError("repair execute result state is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "approved_preview_sha256": self.approved_preview_sha256,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": REPAIR_EXECUTE_RESULT_CONTRACT,
            "current": self.current,
            "last_good": self.last_good,
            "pointer_revision": self.pointer_revision,
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "state": self.state,
        }

    @property
    def canonical(self) -> bytes:
        value = canonical_json_bytes(self.to_dict())
        if len(value) > REPAIR_RESULT_MAX_BYTES:
            raise ValueError("repair execute result exceeds the public byte limit")
        return value

    @property
    def exit_code(self) -> int:
        return EXIT_READY


@dataclass(frozen=True)
class RepairFailure:
    operation: str
    state: str
    exit_code: int
    reason_code: str
    action_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action_code": self.action_code,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": f"graphify.workspace.repair_{self.operation}_result",
            "exit_code": self.exit_code,
            "reason_code": self.reason_code,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "state": self.state,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def classify_failure(error: Exception, operation: str) -> RepairFailure:
    """Map internal failures to a bounded redacted public repair envelope."""

    if operation not in {"preview", "execute"}:
        raise ValueError("unsupported repair operation")
    if isinstance(error, WorkspaceAuthorityError):
        state = "unsupported" if error.reason_code == "runtime_authority_unsupported" else "invalid"
        return RepairFailure(
            operation,
            state,
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(
        error,
        (RepairPreviewRequestUnsupported, RepairExecuteRequestUnsupported),
    ):
        return RepairFailure(
            operation,
            "unsupported",
            EXIT_INVALID,
            f"repair_{operation}_request_unsupported",
            "use_supported_repair_contract",
        )
    if isinstance(
        error,
        (
            RepairPreviewRequestInvalid,
            RepairExecuteRequestInvalid,
            AuthorizationError,
        ),
    ):
        return RepairFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            f"repair_{operation}_request_invalid",
            f"provide_valid_repair_{operation}_request",
        )
    if isinstance(error, RepairCommitUnknown | CommitUnknown):
        return RepairFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            "run_workspace_status_then_repair_dry_run",
        )
    if isinstance(error, RepairIrreparable):
        return RepairFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "repair_irreparable",
            "inspect_workspace_state",
        )
    if isinstance(
        error,
        (
            RepairConflict,
            RevisionConflict,
            PointerConflict,
            LeaseExpired,
            StaleLease,
        ),
    ):
        return RepairFailure(
            operation,
            "conflict",
            EXIT_DEGRADED,
            "repair_authority_conflict",
            "refresh_repair_request",
        )
    if isinstance(error, (LeaseBusy, LockTimeout)):
        return RepairFailure(
            operation,
            "conflict",
            EXIT_DEGRADED,
            "repair_lease_busy",
            "retry_workspace_repair",
        )
    if isinstance(error, StatePathError):
        return RepairFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedCompatibility):
        return RepairFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, UnsupportedRuntime):
        return RepairFailure(
            operation,
            "unsupported",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(
        error,
        (
            LeaseRecoveryRequired,
            PointerRecoveryRequired,
            StateRecoveryRequired,
            StateCorrupt,
            PointerCorrupt,
            GenerationError,
            JournalError,
            LeaseError,
        ),
    ):
        return RepairFailure(
            operation,
            "invalid",
            EXIT_INVALID,
            "repair_state_unsupported",
            "inspect_workspace_state",
        )
    return RepairFailure(
        operation,
        "invalid",
        EXIT_INVALID,
        f"repair_{operation}_failed",
        "run_workspace_doctor",
    )


class WorkspaceRepair:
    """Coordinate pure pointer analysis and exact fenced execution."""

    def __init__(
        self,
        state_root: Path,
        registry: RegistryStore,
        leases: LeaseStore,
        generations: GenerationStore,
        pointers: PointerStore,
        journal: JournalStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve(strict=True)
        self.registry = registry
        self.leases = leases
        self.generations = generations
        self.pointers = pointers
        self.journal = journal
        roots = {
            self.state_root,
            registry.state.root,
            leases.state.root,
            generations.state.root,
            pointers.state.root,
            journal.state.root,
        }
        if len(roots) != 1:
            raise RepairError("repair dependencies must share one external state root")
        if capabilities is not None and capabilities != leases.state.capabilities:
            raise RepairError("repair capabilities must match the composed runtime")

    @staticmethod
    def _entry(registry: object, repo_uuid: str) -> Mapping[str, object]:
        try:
            workspaces = cast(Mapping[str, object], cast(Any, registry).to_dict())["workspaces"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise StateCorrupt("registry workspace entries are invalid") from exc
        if not isinstance(workspaces, list):
            raise StateCorrupt("registry workspace entries are invalid")
        matches = [
            item
            for item in workspaces
            if isinstance(item, Mapping) and item.get("repo_uuid") == repo_uuid
        ]
        if len(matches) != 1:
            raise RepairIrreparable("registry has no singular repair authority")
        return cast(Mapping[str, object], matches[0])

    @staticmethod
    def _check_authority(
        request: RepairPreviewRequest,
        registry: object,
        entry: Mapping[str, object],
        lease_state: WorkspaceLeaseState,
    ) -> None:
        try:
            registry_revision = int(cast(Any, registry).to_dict()["revision"])
            active_source_revision = _integer(
                entry["active_source_revision"],
                "active_source_revision",
                minimum=1,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise StateCorrupt("repair authority is invalid") from exc
        expected_actual = (
            (
                "registry_revision",
                request.expected_registry_revision,
                registry_revision,
            ),
            (
                "active_source_revision",
                request.expected_active_source_revision,
                active_source_revision,
            ),
            (
                "operation_epoch",
                request.expected_operation_epoch,
                lease_state.operation_epoch,
            ),
            (
                "migration_epoch",
                request.expected_migration_epoch,
                lease_state.migration_epoch,
            ),
        )
        for label, expected, actual in expected_actual:
            if expected != actual:
                raise RepairAuthorityConflict(f"{label} does not match the repair request")

    def preview(
        self,
        request: RepairPreviewRequest,
        *,
        monotonic_ns: int | None = None,
        deadline_ns: int | None = None,
    ) -> RepairPlan:
        """Analyze repair using existing-only locks and no durable writes."""

        del monotonic_ns  # operation clocks do not define host lock deadlines
        if deadline_ns is None:
            deadline_ns = request.runtime_deadline()
        try:
            with self.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
                entry = self._entry(registry, request.repo_uuid)
                with self.leases.read_only_workspace_lock(
                    request.repo_uuid,
                    deadline_ns=deadline_ns,
                ):
                    lease_state = self.leases.read_only_snapshot_locked(
                        registry,
                        request.repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                    self._check_authority(request, registry, entry, lease_state)
                    self.leases._assert_recovery_barriers_locked(
                        request.repo_uuid,
                        "REPAIR",
                        recover=False,
                        deadline_ns=deadline_ns,
                    )
                    semantic_queue = self.generations.semantic_queue
                    if semantic_queue is None:
                        return RepairPlan.irreparable()
                    semantic_queue.read_only_snapshot_locked(
                        request.repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                    decision = self.pointers.analyze_repair(
                        request.repo_uuid,
                        active_source_revision=request.expected_active_source_revision,
                        operation_epoch=None,
                        fence_token=None,
                        deadline_ns=deadline_ns,
                    )
                    return RepairPlan.from_decision(decision)
        except RepairError:
            raise
        except PointerCorrupt, GenerationError, JournalError, SemanticQueueError:
            return RepairPlan.irreparable()
        except LeaseRecoveryRequired, StateRecoveryRequired:
            return RepairPlan.irreparable()

    def execute(
        self,
        request: RepairPreviewRequest,
        *,
        approved_preview_sha256: str,
        authorization: RepairAuthorization,
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> RepairExecution:
        """Execute an approved public preview under one fresh repair fence."""

        authorization.require()
        _digest(approved_preview_sha256, "approved_preview_sha256")
        deadline_ns = request.runtime_deadline()
        try:
            plan = self.preview(
                request,
                monotonic_ns=monotonic_ns,
                deadline_ns=deadline_ns,
            )
        except RepairAuthorityConflict as exc:
            raise RepairPlanChanged("canonical preview no longer matches repair authority") from exc
        if plan.classification == "irreparable":
            raise RepairIrreparable("current state is outside pointer repair authority")
        if _public_preview_result(request, plan).sha256 != approved_preview_sha256:
            raise RepairPlanChanged("canonical preview no longer matches")
        return self._execute_plan(
            request,
            plan,
            occurred_at=occurred_at,
            monotonic_ns=monotonic_ns,
            deadline_ns=deadline_ns,
        )

    def _execute_plan(
        self,
        request: RepairPreviewRequest,
        plan: RepairPlan,
        *,
        occurred_at: datetime,
        monotonic_ns: int,
        deadline_ns: int | None,
    ) -> RepairExecution:
        if plan.classification == "irreparable":
            raise RepairIrreparable("the approved repair plan is irreparable")
        ttl_ns = (
            _REPAIR_LEASE_TTL_NS
            if deadline_ns is None
            else max(_REPAIR_LEASE_TTL_NS, deadline_ns - time.monotonic_ns())
        )
        try:
            grant = self.leases.acquire(
                request.repo_uuid,
                "REPAIR",
                self.leases.current_owner(),
                expected_registry_revision=request.expected_registry_revision,
                expected_active_source_revision=request.expected_active_source_revision,
                expected_operation_epoch=request.expected_operation_epoch,
                expected_migration_epoch=request.expected_migration_epoch,
                acquired_at=occurred_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=ttl_ns,
                deadline_ns=deadline_ns,
            )
        except CommitUnknown as exc:
            raise RepairCommitUnknown(
                "repair lease acquisition outcome is uncertain; run status and a fresh dry-run"
            ) from exc
        primary: tuple[BaseException, TracebackType | None] | None = None
        try:
            try:
                if plan.decision is None:  # pragma: no cover - classification invariant
                    raise StateCorrupt("repair plan has no exact pointer decision")
                pointer = self.pointers.recover(
                    grant,
                    occurred_at=occurred_at,
                    monotonic_ns=monotonic_ns,
                    expected_plan=plan.decision,
                    deadline_ns=deadline_ns,
                )
            except PointerConflict as exc:
                raise RepairPlanChanged(
                    "canonical repair plan changed under the mutation boundary"
                ) from exc
            return RepairExecution(
                plan=plan,
                pointer=None if plan.classification == "no_op" else pointer,
            )
        except BaseException as exc:
            primary = (exc, exc.__traceback__)
        finally:
            self._release(grant, primary)
        raise AssertionError("unreachable")

    def _release(
        self,
        grant: LeaseGrant,
        primary: tuple[BaseException, TracebackType | None] | None,
    ) -> None:
        try:
            self.leases.release(grant)
        except (CommitUnknown, InjectedFault) as exc:
            if primary is None:
                raise RepairCommitUnknown(
                    "repair lease release outcome is uncertain; run status and a fresh dry-run"
                ) from exc
        except Exception as exc:
            if primary is None:
                raise RepairCommitUnknown(
                    "repair lease release outcome is uncertain; run status and a fresh dry-run"
                ) from exc
        if primary is not None:
            error, traceback = primary
            raise error.with_traceback(traceback)


def _workspace_repair(runtime: WorkspaceRuntime) -> WorkspaceRepair:
    return WorkspaceRepair(
        runtime.leases.state.root,
        runtime.registry,
        runtime.leases,
        runtime.generations,
        runtime.pointers,
        runtime.journal,
        capabilities=runtime.leases.state.capabilities,
    )


def repair_preview(
    runtime: WorkspaceRuntime,
    request: RepairPreviewRequest,
    *,
    monotonic_ns: int | None = None,
) -> RepairPreviewResult:
    """Return the exact canonical public preview approved by execute."""

    plan = _workspace_repair(runtime).preview(request, monotonic_ns=monotonic_ns)
    return _public_preview_result(request, plan)


def _pointer_public_value(
    execution: RepairExecution,
) -> tuple[dict[str, str], dict[str, str] | None, int, str]:
    if execution.pointer is None:
        if execution.plan.candidate is None:
            raise RepairCommitUnknown("repair no-op did not identify a current generation")
        return (
            execution.plan.candidate,
            execution.plan.last_good,
            execution.plan.next_pointer_revision,
            "no_op",
        )
    try:
        pointer = execution.pointer.to_dict()
        current = _reference(pointer["current"], "current", allow_none=False)
        last_good = _reference(pointer["last_good"], "last_good", allow_none=True)
        revision = _integer(pointer["pointer_revision"], "pointer_revision", minimum=1)
    except (KeyError, TypeError, ValueError) as exc:
        raise RepairCommitUnknown("repair completed without a valid canonical result") from exc
    assert current is not None
    return current, last_good, revision, "repaired"


def repair_execute(
    runtime: WorkspaceRuntime,
    request: RepairExecuteRequest,
    *,
    occurred_at: datetime,
    monotonic_clock: Callable[[], int] = time.monotonic_ns,
) -> RepairExecuteResult:
    """Reproduce approved preview bytes, then execute their exact repair plan."""

    request.authorization.require()
    preview_request = request.preview_request
    repair = _workspace_repair(runtime)
    deadline_ns = preview_request.runtime_deadline()
    plan = repair.preview(
        preview_request,
        monotonic_ns=monotonic_clock(),
        deadline_ns=deadline_ns,
    )
    preview = _public_preview_result(preview_request, plan)
    if preview.sha256 != request.approved_preview_sha256:
        raise RepairPlanChanged(
            "approved preview SHA-256 does not match current canonical preview bytes"
        )
    execution = repair._execute_plan(
        preview_request,
        plan,
        occurred_at=occurred_at,
        monotonic_ns=monotonic_clock(),
        deadline_ns=deadline_ns,
    )
    current, last_good, pointer_revision, state = _pointer_public_value(execution)
    return RepairExecuteResult(
        repo_uuid=request.repo_uuid,
        request_sha256=request.request_sha256,
        approved_preview_sha256=request.approved_preview_sha256,
        current=current,
        last_good=last_good,
        pointer_revision=pointer_revision,
        state=state,
    )


__all__ = [
    "REPAIR_EXECUTE_REQUEST_CONTRACT",
    "REPAIR_EXECUTE_RESULT_CONTRACT",
    "REPAIR_PREVIEW_REQUEST_CONTRACT",
    "REPAIR_PREVIEW_RESULT_CONTRACT",
    "REPAIR_REQUEST_MAX_BYTES",
    "REPAIR_RESULT_MAX_BYTES",
    "REPAIR_SCHEMA_VERSION",
    "RepairAuthorization",
    "RepairAuthorityConflict",
    "RepairCommitUnknown",
    "RepairConflict",
    "RepairError",
    "RepairExecuteRequest",
    "RepairExecuteRequestInvalid",
    "RepairExecuteRequestUnsupported",
    "RepairExecuteResult",
    "RepairExecution",
    "RepairFailure",
    "RepairIrreparable",
    "RepairObservedAuthority",
    "RepairPlan",
    "RepairPlanChanged",
    "RepairPreviewRequest",
    "RepairPreviewRequestInvalid",
    "RepairPreviewRequestUnsupported",
    "RepairPreviewResult",
    "WorkspaceRepair",
    "classify_failure",
    "repair_execute",
    "repair_preview",
]
