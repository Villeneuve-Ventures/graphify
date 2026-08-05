"""Provider-neutral orchestration for one exact structural workspace sync."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from threading import Event, Thread
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any, Mapping, Sequence, cast

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.composition import WorkspaceRuntime
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    STATE_SCHEMA_VERSION,
    CapacityPolicy,
    ContractError,
    GenerationReceipt,
    StagedBuildState,
    WorkspaceLeaseState,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.generations import (
    CertificationRequest,
    GenerationAllocation,
    GenerationConflict,
    StagedBuildReadRecoveryRequired,
    StagedBuildStillCurrent,
    StructuralBuildRequest,
)
from graphify.workspace.identity import SourceIdentity
from graphify.workspace.leases import LeaseGrant, LeaseOperation
from graphify.workspace.persistence import CommitUnknown, StateRecoveryRequired
from graphify.workspace.pointers import PointerCAS, PointerRecoveryRequired

if TYPE_CHECKING:
    from graphify.workspace.semantic_handoff import (
        SemanticResultHandoff,
        SemanticResultEvidence,
        SemanticResultFinalization,
    )
    from graphify.workspace.semantic_queue import (
        SemanticCertificationView,
        SemanticQueueSnapshot,
    )


SYNC_REQUEST_CONTRACT = "graphify.workspace.sync_request"
SYNC_RECEIPT_CONTRACT = "graphify.workspace.sync"
SYNC_SCHEMA_VERSION = 1
SYNC_MODE = "code_only"
SYNC_REQUEST_MAX_BYTES = 16 * 1024

_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_INTEGER = 9_223_372_036_854_775_807
_SYNC_LEASE_TTL_NS = 3_600_000_000_000
_SYNC_READ_TIMEOUT_NS = 5_000_000_000
_SEMANTIC_CERTIFICATION_VALIDATIONS = (
    "coordination_lock_precreated",
    "payload_manifest",
    "stable_semantic_queue",
)
_REQUEST_FIELDS = frozenset(
    {
        "contract",
        "schema_version",
        "cli_contract_version",
        "mode",
        "repo_uuid",
        "generation_id",
        "expected_registry_revision",
        "expected_active_source_revision",
        "expected_operation_epoch",
        "expected_migration_epoch",
        "expected_pointer_revision",
        "expected_current_receipt_sha256",
        "source_epoch",
        "semantic_desired_watermark",
        "expected_payload_bytes",
        "capacity_policy",
    }
)
_CAPACITY_FIELDS = frozenset(
    {
        "global_max_bytes",
        "global_max_generations",
        "workspace_max_bytes",
        "workspace_max_generations",
        "reserve_bytes",
    }
)


class WorkspaceSyncError(RuntimeError):
    """Base class for stable, redacted public sync failures."""

    state = "invalid"
    exit_code = 20
    reason_code = "sync_failed"
    action_code = "run_workspace_doctor"


class SyncRequestInvalid(WorkspaceSyncError):
    """The bounded public sync request is malformed or noncanonical."""

    reason_code = "sync_request_invalid"
    action_code = "provide_valid_sync_request"


class SyncAuthorityConflict(WorkspaceSyncError, GenerationConflict):
    """The caller's explicit CAS authority is no longer current."""

    state = "conflict"
    exit_code = 10
    reason_code = "sync_authority_conflict"
    action_code = "refresh_sync_request"


class StagedBuildRecoveryRequired(SyncAuthorityConflict):
    """Another exact staged request must be resumed or closed first."""

    reason_code = "staged_build_recovery_required"
    action_code = "resume_exact_workspace_sync"


class SyncLeaseBusy(SyncAuthorityConflict):
    """A live fenced owner currently excludes this sync attempt."""

    reason_code = "lease_busy"
    action_code = "retry_workspace_sync"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SyncRequestInvalid("sync request contains duplicate keys")
        value[key] = item
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SyncRequestInvalid(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncRequestInvalid(f"{label} must be an integer")
    if value < minimum or value > _MAX_INTEGER:
        raise SyncRequestInvalid(f"{label} is outside the supported range")
    return value


def _digest_or_none(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SyncRequestInvalid(f"{label} must be a canonical SHA-256 digest")
    return value


@dataclass(frozen=True)
class SyncRequest:
    """Smallest explicit public authority for one code-only sync."""

    repo_uuid: str
    generation_id: str
    expected_registry_revision: int
    expected_active_source_revision: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    expected_pointer_revision: int
    expected_current_receipt_sha256: str | None
    source_epoch: int
    semantic_desired_watermark: int
    expected_payload_bytes: int
    capacity_policy: CapacityPolicy

    def to_dict(self) -> dict[str, object]:
        capacity = self.capacity_policy.to_dict()
        return {
            "capacity_policy": {
                key: capacity[key]
                for key in sorted(_CAPACITY_FIELDS)
            },
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": SYNC_REQUEST_CONTRACT,
            "expected_active_source_revision": self.expected_active_source_revision,
            "expected_current_receipt_sha256": self.expected_current_receipt_sha256,
            "expected_migration_epoch": self.expected_migration_epoch,
            "expected_operation_epoch": self.expected_operation_epoch,
            "expected_payload_bytes": self.expected_payload_bytes,
            "expected_pointer_revision": self.expected_pointer_revision,
            "expected_registry_revision": self.expected_registry_revision,
            "generation_id": self.generation_id,
            "mode": SYNC_MODE,
            "repo_uuid": self.repo_uuid,
            "schema_version": SYNC_SCHEMA_VERSION,
            "semantic_desired_watermark": self.semantic_desired_watermark,
            "source_epoch": self.source_epoch,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SyncRequest":
        data = _mapping(value, "sync request")
        if set(data) != _REQUEST_FIELDS:
            raise SyncRequestInvalid("sync request fields are invalid")
        if data["contract"] != SYNC_REQUEST_CONTRACT:
            raise SyncRequestInvalid("sync request contract is invalid")
        if data["schema_version"] != SYNC_SCHEMA_VERSION:
            raise SyncRequestInvalid("sync request schema version is invalid")
        if data["cli_contract_version"] != CLI_CONTRACT_VERSION:
            raise SyncRequestInvalid("sync request CLI contract version is invalid")
        if data["mode"] != SYNC_MODE:
            raise SyncRequestInvalid("sync request mode is invalid")
        try:
            repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(data["repo_uuid"])
        except ContractError as exc:
            raise SyncRequestInvalid("sync request repo_uuid is invalid") from exc
        generation_id = data["generation_id"]
        if not isinstance(generation_id, str) or _GENERATION_RE.fullmatch(generation_id) is None:
            raise SyncRequestInvalid("sync request generation_id is invalid")
        pointer_revision = _integer(
            data["expected_pointer_revision"],
            "expected_pointer_revision",
            minimum=0,
        )
        current_receipt = _digest_or_none(
            data["expected_current_receipt_sha256"],
            "expected_current_receipt_sha256",
        )
        if (pointer_revision == 0) != (current_receipt is None):
            raise SyncRequestInvalid(
                "expected_current_receipt_sha256 must be null exactly at pointer revision zero"
            )
        capacity_value = _mapping(data["capacity_policy"], "capacity_policy")
        if set(capacity_value) != _CAPACITY_FIELDS:
            raise SyncRequestInvalid("capacity_policy fields are invalid")
        try:
            capacity_policy = CapacityPolicy.from_mapping(
                {
                    "contract": "graphify.workspace.capacity_policy.internal",
                    "format_version": 1,
                    **capacity_value,
                }
            )
        except (ContractError, TypeError, ValueError) as exc:
            raise SyncRequestInvalid("capacity_policy is invalid") from exc
        expected_payload_bytes = _integer(
            data["expected_payload_bytes"],
            "expected_payload_bytes",
            minimum=1,
        )
        if expected_payload_bytes > capacity_policy.workspace_max_bytes:
            raise SyncRequestInvalid(
                "expected_payload_bytes exceeds the workspace capacity bound"
            )
        request = cls(
            repo_uuid=repo_uuid,
            generation_id=generation_id,
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
                minimum=1,
            ),
            expected_migration_epoch=_integer(
                data["expected_migration_epoch"],
                "expected_migration_epoch",
                minimum=0,
            ),
            expected_pointer_revision=pointer_revision,
            expected_current_receipt_sha256=current_receipt,
            source_epoch=_integer(data["source_epoch"], "source_epoch", minimum=1),
            semantic_desired_watermark=_integer(
                data["semantic_desired_watermark"],
                "semantic_desired_watermark",
                minimum=1,
            ),
            expected_payload_bytes=expected_payload_bytes,
            capacity_policy=capacity_policy,
        )
        return request

    @classmethod
    def from_json(cls, value: bytes) -> "SyncRequest":
        if len(value) > SYNC_REQUEST_MAX_BYTES:
            raise SyncRequestInvalid("sync request exceeds the byte limit")
        try:
            parsed = json.loads(value, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
            raise SyncRequestInvalid("sync request is not valid UTF-8 JSON") from exc
        if not isinstance(parsed, Mapping):
            raise SyncRequestInvalid("sync request must be an object")
        request = cls.from_mapping(parsed)
        if request.canonical != value:
            raise SyncRequestInvalid("sync request is not canonical JSON")
        return request


@dataclass(frozen=True)
class SyncReceipt:
    """Canonical redacted success receipt, identical on exact terminal replay."""

    repo_uuid: str
    generation_id: str
    request_sha256: str
    receipt_sha256: str
    pointer_revision: int

    def __post_init__(self) -> None:
        try:
            canonical_uuid = WorkspaceLeaseState.canonical_repo_uuid(self.repo_uuid)
        except ContractError as exc:
            raise ValueError("sync receipt repo_uuid is invalid") from exc
        if canonical_uuid != self.repo_uuid:
            raise ValueError("sync receipt repo_uuid is noncanonical")
        if _GENERATION_RE.fullmatch(self.generation_id) is None:
            raise ValueError("sync receipt generation_id is invalid")
        if _DIGEST_RE.fullmatch(self.request_sha256) is None:
            raise ValueError("sync receipt request_sha256 is invalid")
        if _DIGEST_RE.fullmatch(self.receipt_sha256) is None:
            raise ValueError("sync receipt receipt_sha256 is invalid")
        if (
            isinstance(self.pointer_revision, bool)
            or not isinstance(self.pointer_revision, int)
            or not 1 <= self.pointer_revision <= _MAX_INTEGER
        ):
            raise ValueError("sync receipt pointer_revision is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": SYNC_RECEIPT_CONTRACT,
            "exit_code": 0,
            "generation_id": self.generation_id,
            "mode": SYNC_MODE,
            "pointer_revision": self.pointer_revision,
            "receipt_sha256": self.receipt_sha256,
            "repo_uuid": self.repo_uuid,
            "request_sha256": self.request_sha256,
            "schema_version": SYNC_SCHEMA_VERSION,
            "state": "synchronized",
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class _SemanticCertificationEntry:
    """Exact reopened COMPLETE authority for the private certification child."""

    staged: StagedBuildState
    handoff: SemanticResultHandoff
    entries: tuple[dict[str, str | int], ...]
    binding_request_sha256: str | None
    binding_view: SemanticCertificationView | None
    queue_revision: int | None
    queue_sha256: str | None
    capacity_requires_recovery: bool


@dataclass(frozen=True)
class _SemanticCertificationFinalization:
    """Redacted internal terminal proof; this is not a public receipt."""

    repo_uuid: str
    target_generation_id: str
    request_sha256: str
    handoff_sha256: str
    payload_manifest_sha256: str
    certification_request_sha256: str
    receipt_sha256: str
    staged_revision: int
    queue_revision: int
    queue_sha256: str


def _observe_structural_source(
    runtime: WorkspaceRuntime,
    repo_uuid: str,
) -> tuple[SourceIdentity, tuple[SourceObservation, SourceObservation]]:
    source = runtime.registry.resolve_active_source(repo_uuid)
    observations = (
        runtime.generations.adapter.observe(source.root),
        runtime.generations.adapter.observe(source.root),
    )
    confirmed = runtime.registry.resolve_active_source(repo_uuid)
    if confirmed != source:
        raise GenerationConflict("trusted source identity changed during sync observation")
    if observations[0] != observations[1]:
        raise GenerationConflict("trusted sync observations are not stable")
    return source, observations


def _structural_request(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    observations: tuple[SourceObservation, SourceObservation],
) -> StructuralBuildRequest:
    observation = observations[0]
    document = runtime.generations.structural_observation_document(observation)
    return StructuralBuildRequest.from_mapping(
        {
            "capacity_policy_sha256": request.capacity_policy.sha256,
            "compatibility_sha256": runtime.generations.compatibility_sha256,
            "expected_active_source_revision": request.expected_active_source_revision,
            "expected_current_receipt_sha256": request.expected_current_receipt_sha256,
            "expected_migration_epoch": request.expected_migration_epoch,
            "expected_operation_epoch": request.expected_operation_epoch,
            "expected_payload_bytes": request.expected_payload_bytes,
            "expected_pointer_revision": request.expected_pointer_revision,
            "expected_registry_revision": request.expected_registry_revision,
            "logical_request_sha256": request.sha256,
            "observation_detector_id": document["detector_id"],
            "observation_entries_sha256": document["entries_sha256"],
            "observation_evidence_sha256": (
                runtime.generations.structural_observation_evidence_sha256(
                    observations
                )
            ),
            "observation_manifest_sha256": observation.inventory_sha256,
            "policy_sha256": observation.policy_sha256,
            "source_commit": observation.source_commit,
            "source_epoch": request.source_epoch,
        }
    )


def _read_staged_build(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> StagedBuildState | None:
    repo_uuid = request.repo_uuid
    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        registry_value = registry.to_dict()
        registered = [
            item
            for item in cast(list[Mapping[str, object]], registry_value["workspaces"])
            if item["repo_uuid"] == repo_uuid
        ]
        if len(registered) != 1:
            raise SyncAuthorityConflict(
                "sync request does not name one registered workspace"
            )
        with runtime.leases.read_only_workspace_lock(
            repo_uuid,
            deadline_ns=deadline_ns,
        ):
            lease_state = runtime.leases.read_only_snapshot_locked(
                registry,
                repo_uuid,
                deadline_ns=deadline_ns,
            )
            staged = runtime.generations.read_only_staged_build_locked(
                repo_uuid,
                deadline_ns=deadline_ns,
            )
            live_workspace_lease = lease_state.leases.get("workspace")
            if live_workspace_lease is not None:
                live_value = live_workspace_lease.to_dict()
                live_owner = cast(Mapping[str, object], live_value["owner"])
                current_boot_id = runtime.leases.current_owner().boot_id
                if (
                    live_owner["boot_id"] == current_boot_id
                    and time.monotonic_ns()
                    < int(live_value["liveness_deadline_monotonic_ns"])
                ):
                    raise SyncLeaseBusy("a live workspace lease excludes this sync")

            exact = staged is not None and (
                staged.generation_id == request.generation_id
                and staged.request.logical_request_sha256 == request.sha256
            )
            if (
                staged is not None
                and not exact
                and staged.lifecycle_state not in {"PROMOTED", "ABANDONED"}
            ):
                raise StagedBuildRecoveryRequired(
                    "another staged build request requires exact recovery"
                )
            if exact:
                if int(registry_value["revision"]) < request.expected_registry_revision:
                    raise SyncAuthorityConflict(
                        "registry authority predates the exact staged request"
                    )
                if lease_state.operation_epoch < request.expected_operation_epoch:
                    raise SyncAuthorityConflict(
                        "operation authority predates the exact staged request"
                    )
                return staged

            pointer = runtime.pointers.load(
                repo_uuid,
                allow_missing=True,
                deadline_ns=deadline_ns,
            )
            if pointer is None:
                pointer_cas: tuple[int, str | None] = (0, None)
            else:
                pointer_value = pointer.to_dict()
                current = cast(Mapping[str, object], pointer_value["current"])
                pointer_cas = (
                    int(pointer_value["pointer_revision"]),
                    str(current["receipt_sha256"]),
                )
            actual = (
                int(registry_value["revision"]),
                cast(int, registered[0]["active_source_revision"]),
                lease_state.operation_epoch,
                lease_state.migration_epoch,
                *pointer_cas,
            )
            expected = (
                request.expected_registry_revision,
                request.expected_active_source_revision,
                request.expected_operation_epoch,
                request.expected_migration_epoch,
                request.expected_pointer_revision,
                request.expected_current_receipt_sha256,
            )
            if actual != expected:
                raise SyncAuthorityConflict(
                    "sync request authority is no longer current"
                )
            return staged


def _sync_fault(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    boundary: str,
) -> None:
    runtime.generations.fault_hook(
        f"sync:{request.generation_id}:{boundary}"
    )


def _success_receipt(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    staged: StagedBuildState,
) -> SyncReceipt:
    if staged.lifecycle_state != "PROMOTED":
        raise GenerationConflict("staged build is not terminally promoted")
    with runtime.pointers.read_current(request.repo_uuid) as reading:
        pointer_value = reading.pointer.to_dict()
        current = cast(Mapping[str, object], pointer_value["current"])
        expected = (
            request.repo_uuid,
            request.generation_id,
            staged.receipt_sha256,
            staged.pointer_revision,
        )
        actual = (
            pointer_value["repo_uuid"],
            current["generation_id"],
            current["receipt_sha256"],
            pointer_value["pointer_revision"],
        )
        if expected != actual or reading.receipt.sha256 != staged.receipt_sha256:
            raise GenerationConflict(
                "terminal staged build is not the visible certified generation"
            )
    return SyncReceipt(
        repo_uuid=request.repo_uuid,
        generation_id=request.generation_id,
        request_sha256=request.sha256,
        receipt_sha256=cast(str, staged.receipt_sha256),
        pointer_revision=cast(int, staged.pointer_revision),
    )


def _release_grant(
    runtime: WorkspaceRuntime,
    grant: LeaseGrant,
    primary: tuple[BaseException, TracebackType | None] | None,
) -> None:
    try:
        runtime.leases.release(grant)
    except CommitUnknown:
        raise
    except Exception:
        if primary is None:
            raise
    if primary is not None:
        error, traceback = primary
        raise error.with_traceback(traceback)


def _build_structural_with_heartbeat(
    runtime: WorkspaceRuntime,
    grant: LeaseGrant,
    source_root: Path,
    staging_path: Path,
) -> None:
    stop = Event()
    heartbeat_failure: list[tuple[Exception, TracebackType | None]] = []
    interval_seconds = (_SYNC_LEASE_TTL_NS / 3) / 1_000_000_000

    def heartbeat() -> None:
        while not stop.wait(interval_seconds):
            try:
                runtime.leases.heartbeat(
                    grant,
                    heartbeat_at=datetime.now(timezone.utc),
                    monotonic_ns=time.monotonic_ns(),
                    ttl_ns=_SYNC_LEASE_TTL_NS,
                )
            except Exception as exc:
                heartbeat_failure.append((exc, exc.__traceback__))
                stop.set()
                return

    worker = Thread(
        target=heartbeat,
        name="graphify-workspace-sync-heartbeat",
    )
    worker.start()
    build_failure: tuple[Exception, TracebackType | None] | None = None
    try:
        runtime.generations.adapter.build_structural(
            source_root,
            output_root=staging_path,
            scratch_root=staging_path,
        )
    except Exception as exc:
        build_failure = (exc, exc.__traceback__)
    finally:
        stop.set()
        worker.join()
    if heartbeat_failure:
        error, traceback = heartbeat_failure[0]
        raise error.with_traceback(traceback)
    if build_failure is not None:
        error, traceback = build_failure
        raise error.with_traceback(traceback)
    runtime.leases.heartbeat(
        grant,
        heartbeat_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=_SYNC_LEASE_TTL_NS,
    )


def _finalize_semantic_result_handoff(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    evidence: Sequence[SemanticResultEvidence],
) -> SemanticResultFinalization:
    """Stop at one reopened COMPLETE manifest and equal sealed queue binding."""

    from graphify.workspace.semantic_handoff import (
        SEMANTIC_INPUT_PATH,
        SemanticHandoffConflict,
        SemanticResultFinalization,
    )

    request = SyncRequest.from_mapping(request.to_dict())
    owner = runtime.semantic_handoffs
    if owner is None:
        raise SemanticHandoffConflict("semantic handoff owner is not composed")

    source, captured_observations = _observe_structural_source(runtime, request.repo_uuid)
    structural_request = _structural_request(runtime, request, captured_observations)
    try:
        pointer_document = runtime.pointers.load(request.repo_uuid, allow_missing=True)
        current_pointer = (
            None if pointer_document is None else pointer_document.to_dict()
        )
    except Exception as exc:
        raise SemanticHandoffConflict(
            "current pointer authority cannot be reopened"
        ) from exc
    capture = owner.capture_and_install(
        request,
        structural_request,
        tuple(evidence),
        current_pointer=current_pointer,
        source_observations=captured_observations,
    )
    staged = runtime.generations.request_staged_build(
        request.repo_uuid,
        request.generation_id,
        structural_request,
        source_observations=captured_observations,
    )
    if staged.lifecycle_state not in {"REQUESTED", "PUBLISHING", "COMPLETE"}:
        raise SemanticHandoffConflict("semantic handoff staged lifecycle is not recoverable")

    exact_recovery = capture.staged_state is not None and (
        capture.staged_state.repo_uuid == request.repo_uuid
        and capture.staged_state.generation_id == request.generation_id
        and capture.staged_state.request.sha256 == structural_request.sha256
    )
    acquired_at = datetime.now(timezone.utc)
    acquired_ns = time.monotonic_ns()
    attempt_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    if exact_recovery:
        attempt = runtime.generations.acquire_staged_recovery(
            request.repo_uuid,
            request.generation_id,
            structural_request,
            attempt_sha256=attempt_sha256,
            acquired_at=acquired_at,
            monotonic_ns=acquired_ns,
            ttl_ns=_SYNC_LEASE_TTL_NS,
        )
    else:
        attempt = runtime.generations.acquire_staged_operation(
            request.repo_uuid,
            request.generation_id,
            structural_request,
            attempt_sha256=attempt_sha256,
            operation="BUILD",
            acquired_at=acquired_at,
            monotonic_ns=acquired_ns,
            ttl_ns=_SYNC_LEASE_TTL_NS,
        )

    primary: tuple[BaseException, TracebackType | None] | None = None
    result: SemanticResultFinalization | None = None
    try:
        allocation = runtime.generations.allocate(
            attempt.grant,
            expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=request.capacity_policy,
            generation_id=request.generation_id,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
        preparation = runtime.generations.prepare_staged_build(
            attempt,
            allocation,
            monotonic_ns=time.monotonic_ns(),
        )
        structural_entries: tuple[dict[str, str | int], ...] | None = None
        if preparation.state.lifecycle_state != "COMPLETE":
            _build_structural_with_heartbeat(
                runtime,
                attempt.grant,
                source.root,
                preparation.staging_path,
            )
            structural_entries = runtime.generations.inspect_staged_payload(allocation)
            if any(entry["path"] == SEMANTIC_INPUT_PATH for entry in structural_entries):
                raise SemanticHandoffConflict(
                    "structural output already contains semantic input authority"
                )
        owner.install_generation_copy(
            capture.handoff,
            preparation,
            monotonic_ns=time.monotonic_ns(),
        )
        copied_entries = runtime.generations.inspect_staged_payload(allocation)
        semantic_entries = [
            entry for entry in copied_entries if entry["path"] == SEMANTIC_INPUT_PATH
        ]
        if len(semantic_entries) != 1:
            raise SemanticHandoffConflict("staging lacks one exact semantic input")
        semantic_entry = semantic_entries[0]
        if (
            semantic_entry["file_type"] != "regular_file"
            or semantic_entry["mode"] != "0600"
            or semantic_entry["size"] != len(capture.handoff.canonical)
            or semantic_entry["sha256"] != capture.handoff.sha256
        ):
            raise SemanticHandoffConflict("staged semantic input inventory differs")
        if structural_entries is not None:
            expected_entries = tuple(
                sorted(
                    (*structural_entries, semantic_entry),
                    key=lambda entry: str(entry["path"]),
                )
            )
            if canonical_json_bytes(list(copied_entries)) != canonical_json_bytes(
                list(expected_entries)
            ):
                raise SemanticHandoffConflict(
                    "semantic copy changed structural staged output"
                )

        confirmed_source, completion_observations = _observe_structural_source(
            runtime,
            request.repo_uuid,
        )
        if confirmed_source != source or completion_observations != captured_observations:
            raise SemanticHandoffConflict("trusted source changed before staged completion")
        completion = runtime.generations.complete_staged_build(
            preparation,
            source_observations=completion_observations,
            monotonic_ns=time.monotonic_ns(),
        )
        inventory = runtime.generations.inspect_staged_payload(completion.allocation)
        if canonical_json_bytes(list(inventory)) != canonical_json_bytes(
            list(completion.entries)
        ):
            raise SemanticHandoffConflict("completed staged inventory changed after commit")
        manifest_sha256 = payload_manifest_sha256("graphify-out", inventory)
        if manifest_sha256 != completion.manifest_sha256:
            raise SemanticHandoffConflict("completed staged manifest differs from inventory")

        bind_deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS

        def validate_bind(
            operation: LeaseOperation,
            queue: SemanticQueueSnapshot,
        ) -> None:
            owner.validate_terminal_locked(
                operation,
                queue,
                request=request,
                capture=capture,
                completion=completion,
                source_observations=completion_observations,
                manifest_sha256=manifest_sha256,
                deadline_ns=bind_deadline_ns,
            )

        runtime.semantic_queue.bind_sealed_inputs(
            attempt.grant,
            sealed_input_manifest_sha256=manifest_sha256,
            monotonic_ns=time.monotonic_ns(),
            expected_snapshot=capture.pre_bind_queue,
            validate_current=validate_bind,
            deadline_ns=bind_deadline_ns,
        )
        terminal_deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
        reopened_staged, reopened_queue = owner.reopen_terminal(
            attempt.grant,
            request=request,
            capture=capture,
            completion=completion,
            source_observations=completion_observations,
            manifest_sha256=manifest_sha256,
            monotonic_ns=time.monotonic_ns(),
            deadline_ns=terminal_deadline_ns,
        )
        result = SemanticResultFinalization(
            repo_uuid=request.repo_uuid,
            target_generation_id=request.generation_id,
            carried_source_generation_id=capture.handoff.carried_source_generation_id,
            handoff_sha256=capture.handoff.sha256,
            payload_manifest_sha256=manifest_sha256,
            staged_revision=reopened_staged.revision,
            queue_revision=reopened_queue.revision,
            queue_sha256=reopened_queue.sha256,
        )
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    _release_grant(runtime, attempt.grant, primary)
    if result is None:  # pragma: no cover - guarded by primary exception replay
        raise SemanticHandoffConflict("semantic result finalization returned no proof")
    owner.cleanup_consumed_fresh_results(capture.handoff)
    return result


def _semantic_certification_request(
    view: SemanticCertificationView,
    *,
    compatibility_sha256: str,
) -> CertificationRequest:
    return CertificationRequest(
        source_commit=view.source_commit,
        source_epoch=view.source_epoch,
        policy_sha256=view.policy_sha256,
        observation_manifest_sha256=view.observation_manifest_sha256,
        queue_watermark=view.queue_watermark,
        semantic_completeness="complete",
        compatibility_sha256=compatibility_sha256,
        validations=_SEMANTIC_CERTIFICATION_VALIDATIONS,
    )


def _receipt_certification_request(receipt: GenerationReceipt) -> CertificationRequest:
    value = receipt.to_dict()
    return CertificationRequest(
        source_commit=str(value["source_commit"]),
        source_epoch=int(value["source_epoch"]),
        policy_sha256=str(value["policy_sha256"]),
        observation_manifest_sha256=str(value["observation_manifest_sha256"]),
        queue_watermark=int(value["queue_watermark"]),
        semantic_completeness=str(value["semantic_completeness"]),
        compatibility_sha256=str(value["compatibility_sha256"]),
        validations=tuple(str(item) for item in cast(list[object], value["validations"])),
    )


def _require_semantic_certification_view(
    handoff: SemanticResultHandoff,
    view: SemanticCertificationView,
    *,
    manifest_sha256: str,
) -> None:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    queue = handoff.queue
    reconciliation = cast(Mapping[str, object], queue["reconciliation"])
    observation_evidence = cast(
        Mapping[str, object],
        reconciliation["source_observations"],
    )
    observations = cast(list[Mapping[str, object]], observation_evidence["observations"])
    if len(observations) != 2 or observations[0] != observations[1]:
        raise SemanticHandoffConflict("retained semantic observations are not exact")
    observation = observations[0]
    expected = (
        handoff.repo_uuid,
        cast(int, queue["revision"]) + 1,
        cast(int, queue["desired_watermark"]),
        cast(int, queue["completed_watermark"]),
        cast(int, queue["compaction_epoch"]),
        cast(int, reconciliation["source_epoch"]),
        str(observation["source_commit"]),
        str(reconciliation["policy_sha256"]),
        str(observation["inventory_sha256"]),
        str(observation_evidence["evidence_sha256"]),
        manifest_sha256,
        "complete",
    )
    actual = (
        view.repo_uuid,
        view.queue_revision,
        view.queue_watermark,
        view.completed_watermark,
        view.compaction_epoch,
        view.source_epoch,
        view.source_commit,
        view.policy_sha256,
        view.observation_manifest_sha256,
        view.observation_evidence_sha256,
        view.sealed_input_manifest_sha256,
        view.semantic_completeness,
    )
    if actual != expected:
        raise SemanticHandoffConflict(
            "semantic certification view differs from retained handoff authority"
        )


def _reopen_semantic_certification_binding(
    runtime: WorkspaceRuntime,
    staged: StagedBuildState,
    handoff: SemanticResultHandoff,
    *,
    deadline_ns: int | None,
) -> tuple[str, SemanticCertificationView] | None:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict
    from graphify.workspace.semantic_queue import SemanticQueueStore

    manifest_sha256 = staged.payload_manifest_sha256
    if manifest_sha256 is None:
        raise SemanticHandoffConflict("completed staged manifest is missing")
    binding = SemanticQueueStore._certification_binding_for_target_from_state(
        runtime.semantic_queue.state,
        staged.repo_uuid,
        generation_id=staged.generation_id,
        sealed_input_manifest_sha256=manifest_sha256,
        deadline_ns=deadline_ns,
    )
    if binding is None:
        return None
    request_sha256, view = binding
    _require_semantic_certification_view(
        handoff,
        view,
        manifest_sha256=manifest_sha256,
    )
    derived = _semantic_certification_request(
        view,
        compatibility_sha256=staged.request.compatibility_sha256,
    )
    if runtime.generations._semantic_request_sha256(derived) != request_sha256:
        raise SemanticHandoffConflict(
            "semantic certification binding request was not target-derived"
        )
    structural_expected = (
        staged.repo_uuid,
        staged.request.source_commit,
        staged.request.source_epoch,
        staged.request.policy_sha256,
        staged.request.observation_manifest_sha256,
        staged.request.observation_evidence_sha256,
        manifest_sha256,
    )
    structural_actual = (
        view.repo_uuid,
        view.source_commit,
        view.source_epoch,
        view.policy_sha256,
        view.observation_manifest_sha256,
        view.observation_evidence_sha256,
        view.sealed_input_manifest_sha256,
    )
    if structural_actual != structural_expected:
        raise SemanticHandoffConflict(
            "semantic certification binding differs from staged authority"
        )
    return request_sha256, view


def _require_certification_structural_authority(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    structural: StructuralBuildRequest,
) -> None:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    expected = (
        request.sha256,
        request.expected_registry_revision,
        request.expected_active_source_revision,
        request.expected_operation_epoch,
        request.expected_migration_epoch,
        request.expected_pointer_revision,
        request.expected_current_receipt_sha256,
        request.source_epoch,
        request.expected_payload_bytes,
        request.capacity_policy.sha256,
        runtime.generations.compatibility_sha256,
    )
    actual = (
        structural.logical_request_sha256,
        structural.expected_registry_revision,
        structural.expected_active_source_revision,
        structural.expected_operation_epoch,
        structural.expected_migration_epoch,
        structural.expected_pointer_revision,
        structural.expected_current_receipt_sha256,
        structural.source_epoch,
        structural.expected_payload_bytes,
        structural.capacity_policy_sha256,
        structural.compatibility_sha256,
    )
    if actual != expected:
        raise SemanticHandoffConflict(
            "certification structural request differs from canonical sync authority"
        )


def _certification_pointer_locked(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    *,
    deadline_ns: int | None,
) -> Mapping[str, object] | None:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    try:
        pointer_document = runtime.pointers.load(
            request.repo_uuid,
            allow_missing=True,
            deadline_ns=deadline_ns,
        )
    except Exception as exc:
        raise SemanticHandoffConflict(
            "certification pointer authority cannot be reopened"
        ) from exc
    pointer = None if pointer_document is None else pointer_document.to_dict()
    pointer_revision = 0
    current_receipt: str | None = None
    current_generation: str | None = None
    if pointer is not None:
        current = cast(Mapping[str, object], pointer["current"])
        pointer_revision = int(pointer["pointer_revision"])
        current_receipt = str(current["receipt_sha256"])
        current_generation = str(current["generation_id"])
    if (pointer_revision, current_receipt) != (
        request.expected_pointer_revision,
        request.expected_current_receipt_sha256,
    ):
        raise SemanticHandoffConflict("certification pointer authority drifted")
    if current_generation == request.generation_id:
        raise SemanticHandoffConflict("certification target is already current")
    return pointer


def _require_semantic_input_entry(
    handoff: SemanticResultHandoff,
    entries: Sequence[Mapping[str, object]],
) -> None:
    from graphify.workspace.semantic_handoff import (
        SEMANTIC_INPUT_PATH,
        SemanticHandoffConflict,
    )

    semantic_entries = [item for item in entries if item["path"] == SEMANTIC_INPUT_PATH]
    if len(semantic_entries) != 1:
        raise SemanticHandoffConflict("certification target lacks one semantic input")
    semantic_entry = semantic_entries[0]
    if (
        semantic_entry["file_type"] != "regular_file"
        or semantic_entry["mode"] != "0600"
        or semantic_entry["size"] != len(handoff.canonical)
        or semantic_entry["sha256"] != handoff.sha256
    ):
        raise SemanticHandoffConflict("certification semantic input inventory differs")


def _complete_entry_locked(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    document: Mapping[str, object],
    lease_state: WorkspaceLeaseState,
    staged: StagedBuildState,
    *,
    source_observations: Sequence[SourceObservation] | None,
    deadline_ns: int,
) -> _SemanticCertificationEntry:
    from graphify.workspace.semantic_handoff import (
        SemanticHandoffConflict,
        _queue_from_existing_binding,
    )

    owner = runtime.semantic_handoffs
    if owner is None:
        raise SemanticHandoffConflict("semantic handoff owner is not composed")
    if (
        staged.lifecycle_state != "COMPLETE"
        or staged.repo_uuid != request.repo_uuid
        or staged.generation_id != request.generation_id
        or staged.request.logical_request_sha256 != request.sha256
        or staged.receipt_sha256 is not None
        or staged.pointer_revision is not None
        or staged.abandonment_intent is not None
        or staged.payload_manifest_sha256 is None
        or staged.operation_epoch is None
        or staged.fence_token is None
    ):
        raise SemanticHandoffConflict("certification entry is not the exact COMPLETE state")
    _require_certification_structural_authority(runtime, request, staged.request)

    registry_entries = [
        item
        for item in cast(list[Mapping[str, object]], document["workspaces"])
        if item["repo_uuid"] == request.repo_uuid
    ]
    if len(registry_entries) != 1:
        raise SemanticHandoffConflict("certification repository authority is ambiguous")
    registry_entry = registry_entries[0]
    if (
        cast(int, document["revision"]) != request.expected_registry_revision
        or cast(int, registry_entry["active_source_revision"])
        != request.expected_active_source_revision
        or lease_state.migration_epoch != request.expected_migration_epoch
        or runtime.generations.compatibility_sha256
        != staged.request.compatibility_sha256
    ):
        raise SemanticHandoffConflict("certification repository authority drifted")

    pointer = _certification_pointer_locked(
        runtime,
        request,
        deadline_ns=deadline_ns,
    )
    handoff = owner._reopen_for_certification(
        request,
        staged.request,
        deadline_ns=deadline_ns,
    )
    binding = _reopen_semantic_certification_binding(
        runtime,
        staged,
        handoff,
        deadline_ns=deadline_ns,
    )
    staging_relative = runtime.generations._staging(
        staged.repo_uuid,
        staged.generation_id,
    )
    final_relative = runtime.generations._generation(
        staged.repo_uuid,
        staged.generation_id,
    )
    staging_exists = runtime.generations.state.private_directory_exists(staging_relative)
    final_exists = runtime.generations.state.private_directory_exists(final_relative)
    staged_receipt_exists = staging_exists and runtime.generations.state.private_file_exists(
        staging_relative / "receipt.json"
    )
    if binding is None and (not staging_exists or staged_receipt_exists):
        raise SemanticHandoffConflict(
            "pre-binding certification target is not the exact receipt-free staging entry"
        )
    allocation = GenerationAllocation(
        repo_uuid=staged.repo_uuid,
        generation_id=staged.generation_id,
        staging_path=runtime.generations.state.path(
            runtime.generations._staging(staged.repo_uuid, staged.generation_id)
        ),
        expected_payload_bytes=staged.request.expected_payload_bytes,
        capacity_policy_sha256=staged.request.capacity_policy_sha256,
        compatibility_sha256=staged.request.compatibility_sha256,
        active_source_revision=staged.request.expected_active_source_revision,
        operation_epoch=staged.operation_epoch,
        fence_token=staged.fence_token,
    )
    completion = runtime.generations._reuse_staged_completion_locked(staged, allocation)
    if completion.state.canonical != staged.canonical:
        raise SemanticHandoffConflict("certification completion wrapper is stale")
    if payload_manifest_sha256("graphify-out", completion.entries) != staged.payload_manifest_sha256:
        raise SemanticHandoffConflict("certification inventory manifest differs")
    _require_semantic_input_entry(handoff, completion.entries)
    if final_exists:
        runtime.generations.verify_generation(
            staged.repo_uuid,
            staged.generation_id,
            deadline_ns=deadline_ns,
            _expected_compatibility_sha256=staged.request.compatibility_sha256,
        )
    reservation, capacity_requires_recovery = (
        runtime.generations._project_capacity_reservation_locked(
            staged.repo_uuid,
            staged.generation_id,
            deadline_ns=deadline_ns,
        )
    )
    if reservation is not None:
        reservation_identity = (
            reservation.repo_uuid,
            reservation.generation_id,
            reservation.reserved_bytes,
            reservation.policy_sha256,
            reservation.compatibility_sha256,
            reservation.active_source_revision,
        )
        expected_reservation_identity = (
            staged.repo_uuid,
            staged.generation_id,
            staged.request.expected_payload_bytes,
            staged.request.capacity_policy_sha256,
            staged.request.compatibility_sha256,
            staged.request.expected_active_source_revision,
        )
        if reservation_identity != expected_reservation_identity:
            raise SemanticHandoffConflict("certification capacity reservation differs")
        if binding is None and (
            reservation.operation_epoch,
            reservation.fence_token,
        ) != (staged.operation_epoch, staged.fence_token):
            raise SemanticHandoffConflict("certification reservation fence differs")
        if binding is not None and (
            reservation.operation_epoch < staged.operation_epoch
            or reservation.fence_token < staged.fence_token
            or reservation.operation_epoch > lease_state.operation_epoch
            or reservation.fence_token > lease_state.fence_high_watermark
        ):
            raise SemanticHandoffConflict("certification reservation fence is ambiguous")
    elif binding is None:
        raise SemanticHandoffConflict("certification capacity reservation is missing")
    else:
        container = final_relative if final_exists else staging_relative
        if not runtime.generations.state.private_file_exists(container / "receipt.json"):
            raise SemanticHandoffConflict(
                "missing reservation has no interrupted certification receipt"
            )
    if binding is None and capacity_requires_recovery:
        raise SemanticHandoffConflict(
            "pre-binding capacity authority requires durable recovery"
        )

    if handoff.carried_source_generation_id is not None:
        source_generation, _source_handoff = owner._current_source_handoff(
            repo_uuid=request.repo_uuid,
            target_generation_id=request.generation_id,
            structural_request=staged.request,
            pointer=pointer,
        )
        if source_generation != handoff.carried_source_generation_id:
            raise SemanticHandoffConflict("certification carried source differs")

    queue_revision: int | None = None
    queue_sha256: str | None = None
    if binding is None:
        queue = runtime.semantic_queue.read_only_snapshot_locked(
            request.repo_uuid,
            deadline_ns=deadline_ns,
        )
        pre_bind = _queue_from_existing_binding(queue, handoff.queue)
        reconciliation = queue.reconciliation
        if (
            reconciliation is None
            or reconciliation.sealed_input_manifest_sha256
            != staged.payload_manifest_sha256
        ):
            raise SemanticHandoffConflict("certification queue sealing differs")
        if source_observations is not None:
            owner._validate_authority_locked(
                document,
                registry_entry,
                lease_state,
                request,
                staged.request,
                pre_bind,
                pointer,
                source_observations,
                runtime.generations.compatibility_sha256,
                require_original_operation_epoch=False,
            )
        queue_revision = queue.revision
        queue_sha256 = queue.sha256
    else:
        _binding_request_sha256, binding_view = binding
        queue_revision = binding_view.queue_revision
        queue_sha256 = binding_view.queue_state_sha256

    return _SemanticCertificationEntry(
        staged=staged,
        handoff=handoff,
        entries=completion.entries,
        binding_request_sha256=None if binding is None else binding[0],
        binding_view=None if binding is None else binding[1],
        queue_revision=queue_revision,
        queue_sha256=queue_sha256,
        capacity_requires_recovery=capacity_requires_recovery,
    )


def _capture_semantic_certification_entry(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    staged: StagedBuildState,
    *,
    source_observations: Sequence[SourceObservation] | None,
) -> _SemanticCertificationEntry:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        with runtime.leases.read_only_workspace_lock(
            request.repo_uuid,
            deadline_ns=deadline_ns,
        ):
            lease_state = runtime.leases.read_only_snapshot_locked(
                registry,
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if (
                lease_state.leases.get("workspace") is not None
                or lease_state.staged_attempt_sha256 is not None
            ):
                raise SemanticHandoffConflict(
                    "prior staged BUILD lease release is not durably proven"
                )
            current = runtime.generations.read_only_staged_build_locked(
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if current is None or current.canonical != staged.canonical:
                raise SemanticHandoffConflict("certification staged entry changed")
            entry = _complete_entry_locked(
                runtime,
                request,
                registry.to_dict(),
                lease_state,
                current,
                source_observations=source_observations,
                deadline_ns=deadline_ns,
            )
            if entry.binding_view is None:
                if (
                    lease_state.operation_epoch != staged.operation_epoch
                    or lease_state.fence_high_watermark != staged.fence_token
                ):
                    raise SemanticHandoffConflict(
                        "pre-binding operation authority advanced beyond COMPLETE"
                    )
            else:
                staged_operation_epoch = staged.operation_epoch
                staged_fence_token = staged.fence_token
                if staged_operation_epoch is None or staged_fence_token is None:
                    raise SemanticHandoffConflict(
                        "bound recovery lacks prior COMPLETE fencing evidence"
                    )
                if (
                    lease_state.operation_epoch < staged_operation_epoch
                    or lease_state.fence_high_watermark < staged_fence_token
                ):
                    raise SemanticHandoffConflict("bound recovery authority regressed")
            return entry


def _validate_semantic_certification_entry_under_grant(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    expected: _SemanticCertificationEntry,
    grant: LeaseGrant,
    *,
    source_observations: Sequence[SourceObservation] | None,
) -> _SemanticCertificationEntry:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    with runtime.leases.current_operation_read_only(
        grant,
        monotonic_ns=time.monotonic_ns(),
        allowed_operations=frozenset({"BUILD"}),
        deadline_ns=deadline_ns,
    ) as operation:
        staged = runtime.generations.read_only_staged_build_locked(
            request.repo_uuid,
            deadline_ns=deadline_ns,
        )
        if staged is None or staged.canonical != expected.staged.canonical:
            raise SemanticHandoffConflict("certification staged entry drifted after binding")
        current = _complete_entry_locked(
            runtime,
            request,
            operation.registry.to_dict(),
            operation.state,
            staged,
            source_observations=source_observations,
            deadline_ns=deadline_ns,
        )
        if (
            current.handoff.canonical != expected.handoff.canonical
            or canonical_json_bytes(list(current.entries))
            != canonical_json_bytes(list(expected.entries))
        ):
            raise SemanticHandoffConflict("certification entry bytes drifted after acquisition")
        if expected.binding_view is not None and (
            current.binding_request_sha256 != expected.binding_request_sha256
            or current.binding_view != expected.binding_view
        ):
            raise SemanticHandoffConflict("certification binding changed after acquisition")
        if expected.binding_view is None and current.binding_view is None and (
            current.queue_revision != expected.queue_revision
            or current.queue_sha256 != expected.queue_sha256
        ):
            raise SemanticHandoffConflict("certification queue drifted after acquisition")
        return current


def _terminal_previous_complete_locked(
    runtime: WorkspaceRuntime,
    staged: StagedBuildState,
    *,
    deadline_ns: int,
) -> StagedBuildState:
    current_path, previous_path, pending_path = runtime.generations._staged_build_paths(
        staged.repo_uuid
    )
    if runtime.generations.state.private_file_exists(pending_path):
        raise GenerationConflict("terminal staged state has an unresolved pending commit")
    if not runtime.generations.state.private_file_exists(current_path):
        raise GenerationConflict("terminal staged current record is missing")
    try:
        previous = StagedBuildState.from_json(
            runtime.generations.state.read_existing_bytes(
                previous_path,
                max_bytes=64 * 1024,
                deadline_ns=deadline_ns,
            )
        )
    except Exception as exc:
        raise GenerationConflict(
            f"terminal previous COMPLETE record is invalid: {exc}"
        ) from exc
    if (
        previous.lifecycle_state != "COMPLETE"
        or previous.revision + 1 != staged.revision
        or previous.repo_uuid != staged.repo_uuid
        or previous.generation_id != staged.generation_id
        or previous.request.canonical != staged.request.canonical
        or previous.payload_manifest_sha256 != staged.payload_manifest_sha256
        or previous.receipt_sha256 is not None
        or previous.pointer_revision is not None
        or previous.abandonment_intent is not None
    ):
        raise GenerationConflict(
            "terminal CERTIFIED state does not advance the exact previous COMPLETE record"
        )
    return previous


def _semantic_certification_terminal_locked(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    document: Mapping[str, object],
    lease_state: WorkspaceLeaseState,
    staged: StagedBuildState,
    *,
    expected_complete: StagedBuildState | None,
    grant: LeaseGrant | None,
    deadline_ns: int,
) -> _SemanticCertificationFinalization:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    owner = runtime.semantic_handoffs
    if owner is None:
        raise SemanticHandoffConflict("semantic handoff owner is not composed")
    if (
        staged.lifecycle_state != "CERTIFIED"
        or staged.repo_uuid != request.repo_uuid
        or staged.generation_id != request.generation_id
        or staged.request.logical_request_sha256 != request.sha256
        or staged.payload_manifest_sha256 is None
        or staged.receipt_sha256 is None
        or staged.operation_epoch is None
        or staged.fence_token is None
        or staged.pointer_revision is not None
        or staged.abandonment_intent is not None
        or staged.abandoned_from is not None
        or staged.abandon_reason is not None
        or staged.abandon_evidence is not None
        or staged.abandon_evidence_sha256 is not None
    ):
        raise GenerationConflict("terminal staged certification proof is incomplete")
    previous = _terminal_previous_complete_locked(
        runtime,
        staged,
        deadline_ns=deadline_ns,
    )
    if expected_complete is not None and previous.canonical != expected_complete.canonical:
        raise GenerationConflict("terminal staged certification advanced another COMPLETE state")
    _require_certification_structural_authority(runtime, request, staged.request)

    registry_entries = [
        item
        for item in cast(list[Mapping[str, object]], document["workspaces"])
        if item["repo_uuid"] == request.repo_uuid
    ]
    if len(registry_entries) != 1 or (
        cast(int, document["revision"]) != request.expected_registry_revision
        or cast(int, registry_entries[0]["active_source_revision"])
        != request.expected_active_source_revision
        or lease_state.migration_epoch != request.expected_migration_epoch
    ):
        raise GenerationConflict("terminal repository authority differs from the request")

    workspace_lease = lease_state.leases.get("workspace")
    if grant is None:
        if workspace_lease is not None or lease_state.staged_attempt_sha256 is not None:
            raise CommitUnknown(
                "terminal recovery owner or staged attempt remains durably visible"
            )
    else:
        if workspace_lease is None or workspace_lease.canonical != grant.lease.canonical:
            raise CommitUnknown("terminal proof is not held by the exact recovery grant")
        if lease_state.lease_epochs.get("workspace") != grant.operation_epoch:
            raise CommitUnknown("terminal recovery lease epoch differs from its grant")

    _certification_pointer_locked(runtime, request, deadline_ns=deadline_ns)
    handoff = owner._reopen_for_certification(
        request,
        staged.request,
        deadline_ns=deadline_ns,
    )
    receipt = runtime.generations.verify_generation(
        request.repo_uuid,
        request.generation_id,
        deadline_ns=deadline_ns,
        _expected_compatibility_sha256=staged.request.compatibility_sha256,
    )
    if receipt.sha256 != staged.receipt_sha256:
        raise GenerationConflict("terminal staged receipt digest differs")
    receipt_value = receipt.to_dict()
    payload = cast(Mapping[str, object], receipt_value["sealed_query_payload"])
    entries = tuple(
        cast(dict[str, str | int], item)
        for item in cast(list[Mapping[str, object]], payload["entries"])
    )
    if (
        payload["manifest_sha256"] != staged.payload_manifest_sha256
        or payload_manifest_sha256("graphify-out", entries)
        != staged.payload_manifest_sha256
    ):
        raise GenerationConflict("terminal receipt payload manifest differs")
    _require_semantic_input_entry(handoff, entries)

    certification_request = _receipt_certification_request(receipt)
    if (
        certification_request.semantic_completeness != "complete"
        or certification_request.validations != _SEMANTIC_CERTIFICATION_VALIDATIONS
    ):
        raise GenerationConflict("terminal receipt certification evidence is incomplete")
    expected_receipt = (
        request.repo_uuid,
        request.generation_id,
        staged.request.source_commit,
        staged.request.source_epoch,
        staged.request.expected_active_source_revision,
        staged.request.policy_sha256,
        staged.request.observation_manifest_sha256,
        staged.request.compatibility_sha256,
        staged.operation_epoch,
        staged.fence_token,
    )
    actual_receipt = (
        receipt_value["repo_uuid"],
        receipt_value["generation_id"],
        certification_request.source_commit,
        certification_request.source_epoch,
        receipt_value["active_source_revision"],
        certification_request.policy_sha256,
        certification_request.observation_manifest_sha256,
        certification_request.compatibility_sha256,
        receipt_value["operation_epoch"],
        receipt_value["fence_token"],
    )
    if actual_receipt != expected_receipt:
        raise GenerationConflict("terminal receipt differs from staged certification authority")

    request_sha256 = runtime.generations._semantic_request_sha256(certification_request)
    binding = _reopen_semantic_certification_binding(
        runtime,
        staged,
        handoff,
        deadline_ns=deadline_ns,
    )
    if binding is None or binding[0] != request_sha256:
        raise GenerationConflict("terminal semantic certification binding is absent")
    view = binding[1]
    if (
        certification_request.source_commit != view.source_commit
        or certification_request.source_epoch != view.source_epoch
        or certification_request.policy_sha256 != view.policy_sha256
        or certification_request.observation_manifest_sha256
        != view.observation_manifest_sha256
        or certification_request.queue_watermark != view.queue_watermark
        or certification_request.semantic_completeness != view.semantic_completeness
    ):
        raise GenerationConflict("terminal receipt differs from its certification binding")

    journal = runtime.journal.project_recovery(
        request.repo_uuid,
        deadline_ns=deadline_ns,
    )
    if journal.actions:
        raise GenerationConflict("terminal lifecycle journal requires durable recovery")
    target_events = journal.snapshot.for_generation(request.generation_id)
    certified_events = tuple(
        event
        for event in target_events
        if event.to_dict()["transition"] == "CERTIFIED"
    )
    if not target_events or target_events[-1].to_dict()["transition"] != "CERTIFIED":
        raise GenerationConflict("terminal lifecycle journal is not CERTIFIED")
    if len(certified_events) != 1:
        raise GenerationConflict("terminal lifecycle journal certification is ambiguous")
    certified_event = certified_events[0].to_dict()
    if (
        certified_event["receipt_sha256"] != receipt.sha256
        or certified_event["pointer_revision"] != 0
        or any(event.to_dict()["transition"] == "PROMOTED" for event in target_events)
    ):
        raise GenerationConflict("terminal lifecycle journal evidence differs")

    reservation, capacity_requires_recovery = (
        runtime.generations._project_capacity_reservation_locked(
            request.repo_uuid,
            request.generation_id,
            deadline_ns=deadline_ns,
        )
    )
    if reservation is not None or capacity_requires_recovery:
        raise GenerationConflict("terminal capacity reservation is not durably absent")

    return _SemanticCertificationFinalization(
        repo_uuid=request.repo_uuid,
        target_generation_id=request.generation_id,
        request_sha256=request.sha256,
        handoff_sha256=handoff.sha256,
        payload_manifest_sha256=staged.payload_manifest_sha256,
        certification_request_sha256=request_sha256,
        receipt_sha256=receipt.sha256,
        staged_revision=staged.revision,
        queue_revision=view.queue_revision,
        queue_sha256=view.queue_state_sha256,
    )


def _semantic_certification_terminal_under_grant(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    expected_complete: StagedBuildState,
    grant: LeaseGrant,
) -> _SemanticCertificationFinalization:
    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    runtime.leases._require_grant_owner(grant)
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        with runtime.leases.read_only_workspace_lock(
            request.repo_uuid,
            deadline_ns=deadline_ns,
        ):
            runtime.leases._check_active(registry, grant)
            lease_state = runtime.leases.read_only_snapshot_locked(
                registry,
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            _domain, current = runtime.leases._matching_lease(lease_state, grant)
            current_value = current.to_dict()
            if current_value["operation"] != "BUILD":
                raise GenerationConflict(
                    "terminal certification is not held by the BUILD recovery lane"
                )
            if time.monotonic_ns() >= int(
                current_value["liveness_deadline_monotonic_ns"]
            ):
                raise CommitUnknown(
                    "terminal certification recovery lease expired before proof"
                )
            staged = runtime.generations.read_only_staged_build_locked(
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if staged is None:
                raise GenerationConflict("terminal staged certification is missing")
            return _semantic_certification_terminal_locked(
                runtime,
                request,
                registry.to_dict(),
                lease_state,
                staged,
                expected_complete=expected_complete,
                grant=grant,
                deadline_ns=deadline_ns,
            )


def _semantic_certification_terminal_after_release(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    *,
    expected_complete: StagedBuildState | None,
) -> _SemanticCertificationFinalization:
    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        with runtime.leases.read_only_workspace_lock(
            request.repo_uuid,
            deadline_ns=deadline_ns,
        ):
            lease_state = runtime.leases.read_only_snapshot_locked(
                registry,
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            staged = runtime.generations.read_only_staged_build_locked(
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if staged is None:
                raise GenerationConflict("terminal staged certification is missing")
            return _semantic_certification_terminal_locked(
                runtime,
                request,
                registry.to_dict(),
                lease_state,
                staged,
                expected_complete=expected_complete,
                grant=None,
                deadline_ns=deadline_ns,
            )


def _project_and_recover_certification_staged_state(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> StagedBuildState:
    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        with runtime.leases.read_only_workspace_lock(
            request.repo_uuid,
            deadline_ns=deadline_ns,
        ):
            lease_state = runtime.leases.read_only_snapshot_locked(
                registry,
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if (
                lease_state.leases.get("workspace") is not None
                or lease_state.staged_attempt_sha256 is not None
            ):
                raise SemanticHandoffConflict(
                    "staged-state recovery is blocked by retained lease authority"
                )
            projected, requires_recovery = (
                runtime.generations._project_staged_build_recovery_locked(
                    request.repo_uuid,
                    deadline_ns=deadline_ns,
                )
            )
            if (
                projected is None
                or not requires_recovery
                or projected.repo_uuid != request.repo_uuid
                or projected.generation_id != request.generation_id
                or projected.request.logical_request_sha256 != request.sha256
                or projected.lifecycle_state not in {"COMPLETE", "CERTIFIED"}
            ):
                raise SemanticHandoffConflict(
                    "staged-state recovery projection is not the exact certification target"
                )
    recovered = runtime.generations.recover_staged_build(request.repo_uuid)
    if recovered is None or recovered.canonical != projected.canonical:
        raise CommitUnknown("staged-state recovery did not reopen the projected record")
    return recovered


def _semantic_certification_staged_state(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> StagedBuildState:
    try:
        staged = _read_staged_build(runtime, request)
    except StagedBuildReadRecoveryRequired:
        staged = _project_and_recover_certification_staged_state(runtime, request)
    if staged is None:
        raise GenerationConflict("semantic certification staged request is missing")
    return staged


def _inspect_certification_grant_release(
    runtime: WorkspaceRuntime,
    grant: LeaseGrant,
    *,
    attempt_sha256: str,
) -> str:
    deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
    repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        with runtime.leases.read_only_workspace_lock(
            repo_uuid,
            deadline_ns=deadline_ns,
        ):
            state = runtime.leases.read_only_snapshot_locked(
                registry,
                repo_uuid,
                deadline_ns=deadline_ns,
            )
            current = state.leases.get("workspace")
            if current is None:
                if state.staged_attempt_sha256 is not None:
                    raise CommitUnknown(
                        "recovery lease is absent but its staged attempt remains"
                    )
                grant_fence = int(grant.lease.to_dict()["fence_token"])
                if (
                    state.operation_epoch != grant.operation_epoch
                    or state.fence_high_watermark != grant_fence
                ):
                    raise CommitUnknown(
                        "recovery lease absence follows replacement authority"
                    )
                return "absent"
            if (
                current.canonical == grant.lease.canonical
                and state.lease_epochs.get("workspace") == grant.operation_epoch
                and state.staged_attempt_sha256 == attempt_sha256
            ):
                return "same"
            raise CommitUnknown("recovery lease was replaced before release proof")


def _release_semantic_certification_grant(
    runtime: WorkspaceRuntime,
    grant: LeaseGrant,
    *,
    attempt_sha256: str,
) -> None:
    for retry in range(2):
        try:
            released = runtime.leases.release(grant)
        except CommitUnknown:
            status = _inspect_certification_grant_release(
                runtime,
                grant,
                attempt_sha256=attempt_sha256,
            )
            if status == "absent":
                return
            lease_value = grant.lease.to_dict()
            if (
                retry == 0
                and status == "same"
                and time.monotonic_ns()
                < int(lease_value["liveness_deadline_monotonic_ns"])
            ):
                continue
            raise
        if (
            released.leases.get("workspace") is not None
            or released.staged_attempt_sha256 is not None
        ):
            raise CommitUnknown("recovery lease release returned ambiguous state")
        return
    raise CommitUnknown("recovery lease release could not be proven")  # pragma: no cover


def _finalize_semantic_generation_certification(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> _SemanticCertificationFinalization:
    """Advance one accepted semantic handoff from COMPLETE to exact CERTIFIED."""

    from graphify.workspace.semantic_handoff import SemanticHandoffConflict

    request = SyncRequest.from_mapping(request.to_dict())
    staged = _semantic_certification_staged_state(runtime, request)
    if staged.lifecycle_state == "CERTIFIED":
        return _semantic_certification_terminal_after_release(
            runtime,
            request,
            expected_complete=None,
        )
    if staged.lifecycle_state != "COMPLETE":
        raise SemanticHandoffConflict(
            f"semantic certification cannot start from {staged.lifecycle_state}"
        )

    entry = _capture_semantic_certification_entry(
        runtime,
        request,
        staged,
        source_observations=None,
    )
    pre_source: SourceIdentity | None = None
    pre_observations: tuple[SourceObservation, SourceObservation] | None = None
    if entry.binding_view is None:
        pre_source, pre_observations = _observe_structural_source(
            runtime,
            request.repo_uuid,
        )
        structural = _structural_request(runtime, request, pre_observations)
        if structural.canonical != entry.staged.request.canonical:
            raise SemanticHandoffConflict(
                "trusted source differs from the completed structural request"
            )
        entry = _capture_semantic_certification_entry(
            runtime,
            request,
            staged,
            source_observations=pre_observations,
        )

    acquired_at = datetime.now(timezone.utc)
    attempt_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    attempt = None
    for retry in range(2):
        try:
            attempt = runtime.generations.acquire_staged_recovery(
                request.repo_uuid,
                request.generation_id,
                entry.staged.request,
                attempt_sha256=attempt_sha256,
                acquired_at=acquired_at,
                monotonic_ns=time.monotonic_ns(),
                ttl_ns=_SYNC_LEASE_TTL_NS,
            )
        except CommitUnknown:
            if retry == 0:
                continue
            raise
        else:
            break
    if attempt is None:  # pragma: no cover - retry loop returns or raises
        raise CommitUnknown("semantic certification recovery acquisition is uncertain")
    primary: tuple[BaseException, TracebackType | None] | None = None
    pre_release: _SemanticCertificationFinalization | None = None
    try:
        if (
            attempt.state.canonical != entry.staged.canonical
            or attempt.grant.lease.to_dict()["operation"] != "BUILD"
        ):
            raise SemanticHandoffConflict(
                "semantic certification did not acquire the exact request-bound BUILD lane"
            )
        if entry.binding_view is None:
            complete_operation_epoch = entry.staged.operation_epoch
            complete_fence_token = entry.staged.fence_token
            recovery_fence_token = cast(
                int,
                attempt.grant.lease.to_dict()["fence_token"],
            )
            if (
                complete_operation_epoch is None
                or complete_fence_token is None
                or attempt.grant.operation_epoch != complete_operation_epoch + 1
                or recovery_fence_token != complete_fence_token + 1
            ):
                raise SemanticHandoffConflict(
                    "semantic certification BUILD authority did not advance COMPLETE exactly once"
                )
        current = _validate_semantic_certification_entry_under_grant(
            runtime,
            request,
            entry,
            attempt.grant,
            source_observations=None,
        )
        certification_observations: tuple[SourceObservation, SourceObservation] | tuple[()] = ()
        view = current.binding_view
        if view is None:
            post_source, post_observations = _observe_structural_source(
                runtime,
                request.repo_uuid,
            )
            if (
                pre_source is None
                or pre_observations is None
                or post_source != pre_source
                or post_observations != pre_observations
            ):
                raise SemanticHandoffConflict(
                    "trusted source drifted before certification binding"
                )
            current = _validate_semantic_certification_entry_under_grant(
                runtime,
                request,
                current,
                attempt.grant,
                source_observations=post_observations,
            )
            view = current.binding_view
            if view is None:
                view = runtime.semantic_queue.certification_view(
                    attempt.grant,
                    source_epoch=current.staged.request.source_epoch,
                    source_observations=post_observations,
                    sealed_input_manifest_sha256=cast(
                        str,
                        current.staged.payload_manifest_sha256,
                    ),
                    monotonic_ns=time.monotonic_ns(),
                )
                _require_semantic_certification_view(
                    current.handoff,
                    view,
                    manifest_sha256=cast(
                        str,
                        current.staged.payload_manifest_sha256,
                    ),
                )
                if (
                    view.queue_revision != current.queue_revision
                    or view.queue_state_sha256 != current.queue_sha256
                ):
                    raise SemanticHandoffConflict(
                        "derived certification view differs from the exact sealed queue"
                    )
                certification_observations = post_observations

        certification_request = _semantic_certification_request(
            view,
            compatibility_sha256=current.staged.request.compatibility_sha256,
        )
        allocation = runtime.generations.allocate(
            attempt.grant,
            expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=request.capacity_policy,
            generation_id=request.generation_id,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
        preparation = runtime.generations.prepare_staged_build(
            attempt,
            allocation,
            monotonic_ns=time.monotonic_ns(),
        )
        if preparation.state.canonical != entry.staged.canonical:
            raise SemanticHandoffConflict(
                "certification reconstruction changed the COMPLETE staged record"
            )
        completion = runtime.generations.complete_staged_build(
            preparation,
            source_observations=certification_observations,
            monotonic_ns=time.monotonic_ns(),
        )
        if (
            completion.state.canonical != entry.staged.canonical
            or canonical_json_bytes(list(completion.entries))
            != canonical_json_bytes(list(entry.entries))
            or completion.manifest_sha256 != entry.staged.payload_manifest_sha256
        ):
            raise SemanticHandoffConflict(
                "certification reconstruction changed completed payload authority"
            )
        owner = runtime.semantic_handoffs
        if owner is None:  # pragma: no cover - captured entry already requires it
            raise SemanticHandoffConflict("semantic handoff owner is not composed")
        reopen_deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
        reopened_handoff = owner._reopen_for_certification(
            request,
            entry.staged.request,
            deadline_ns=reopen_deadline_ns,
        )
        if reopened_handoff.canonical != entry.handoff.canonical:
            raise SemanticHandoffConflict("semantic handoff changed before certification")

        receipt = runtime.generations.certify(
            attempt.grant,
            allocation,
            certification_request,
            source_observations=certification_observations,
            declared_entries=completion.entries,
            staged_completion=completion,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
        verification_deadline_ns = time.monotonic_ns() + _SYNC_READ_TIMEOUT_NS
        if receipt.sha256 != runtime.generations.verify_generation(
            request.repo_uuid,
            request.generation_id,
            deadline_ns=verification_deadline_ns,
            _expected_compatibility_sha256=entry.staged.request.compatibility_sha256,
        ).sha256:
            raise GenerationConflict("certification receipt changed after installation")
        pre_release = _semantic_certification_terminal_under_grant(
            runtime,
            request,
            entry.staged,
            attempt.grant,
        )
    except BaseException as exc:
        primary = (exc, exc.__traceback__)

    try:
        _release_semantic_certification_grant(
            runtime,
            attempt.grant,
            attempt_sha256=attempt_sha256,
        )
    except BaseException as release_error:
        if primary is not None:
            raise release_error from primary[0]
        raise
    if primary is not None:
        error, traceback = primary
        raise error.with_traceback(traceback)
    if pre_release is None:  # pragma: no cover - successful branch assigns proof
        raise GenerationConflict("semantic certification returned no terminal proof")
    terminal = _semantic_certification_terminal_after_release(
        runtime,
        request,
        expected_complete=entry.staged,
    )
    if terminal != pre_release:
        raise CommitUnknown("semantic certification terminal proof changed during release")
    return terminal


def _build_and_certify(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    structural_request: StructuralBuildRequest,
    source: SourceIdentity,
    observations: tuple[SourceObservation, SourceObservation],
    *,
    attempt_sha256: str,
    recovering: bool,
) -> GenerationReceipt:
    acquired_at = datetime.now(timezone.utc)
    acquired_ns = time.monotonic_ns()
    if recovering:
        attempt = runtime.generations.acquire_staged_recovery(
            request.repo_uuid,
            request.generation_id,
            structural_request,
            attempt_sha256=attempt_sha256,
            acquired_at=acquired_at,
            monotonic_ns=acquired_ns,
            ttl_ns=_SYNC_LEASE_TTL_NS,
        )
    else:
        attempt = runtime.generations.acquire_staged_operation(
            request.repo_uuid,
            request.generation_id,
            structural_request,
            attempt_sha256=attempt_sha256,
            operation="BUILD",
            acquired_at=acquired_at,
            monotonic_ns=acquired_ns,
            ttl_ns=_SYNC_LEASE_TTL_NS,
        )
    primary: tuple[BaseException, TracebackType | None] | None = None
    result: GenerationReceipt | None = None
    try:
        _sync_fault(runtime, request, "build_acquired")
        if recovering:
            try:
                runtime.generations.abandon_staged_build(
                    attempt,
                    source_observations=observations,
                    monotonic_ns=time.monotonic_ns(),
                )
            except StagedBuildStillCurrent:
                pass
            else:
                raise SyncAuthorityConflict(
                    "stale staged build was terminally abandoned"
                )
        allocation = runtime.generations.allocate(
            attempt.grant,
            expected_payload_bytes=request.expected_payload_bytes,
            capacity_policy=request.capacity_policy,
            generation_id=request.generation_id,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "generation_allocated")
        preparation = runtime.generations.prepare_staged_build(
            attempt,
            allocation,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "staging_prepared")
        if preparation.state.lifecycle_state != "COMPLETE":
            _build_structural_with_heartbeat(
                runtime,
                attempt.grant,
                source.root,
                preparation.staging_path,
            )
            _sync_fault(runtime, request, "adapter_built")
        completion = runtime.generations.complete_staged_build(
            preparation,
            source_observations=observations,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "staging_completed")
        queue = runtime.semantic_queue.reconcile(
            attempt.grant,
            (),
            source_epoch=request.source_epoch,
            policy_sha256=structural_request.policy_sha256,
            source_observations=observations,
            desired_watermark=request.semantic_desired_watermark,
            semantic_required=False,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "queue_reconciled")
        sealed_manifest = payload_manifest_sha256("graphify-out", completion.entries)
        runtime.semantic_queue.bind_sealed_inputs(
            attempt.grant,
            sealed_input_manifest_sha256=sealed_manifest,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "sealed_inputs_bound")
        result = runtime.generations.certify(
            attempt.grant,
            completion.allocation,
            CertificationRequest(
                source_commit=structural_request.source_commit,
                source_epoch=request.source_epoch,
                policy_sha256=structural_request.policy_sha256,
                observation_manifest_sha256=(
                    structural_request.observation_manifest_sha256
                ),
                queue_watermark=queue.desired_watermark,
                semantic_completeness="not_required",
                compatibility_sha256=runtime.generations.compatibility_sha256,
                validations=(
                    "payload_manifest",
                    "coordination_lock_precreated",
                    "stable_semantic_queue",
                ),
            ),
            source_observations=observations,
            declared_entries=completion.entries,
            staged_completion=completion,
            occurred_at=acquired_at,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "generation_certified")
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    _release_grant(runtime, attempt.grant, primary)
    if result is None:  # pragma: no cover - guarded by primary exception replay
        raise GenerationConflict("structural certification returned no receipt")
    return result


def _promote(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
    structural_request: StructuralBuildRequest,
    receipt: GenerationReceipt,
    observations: tuple[SourceObservation, SourceObservation] | None,
    *,
    attempt_sha256: str,
) -> StagedBuildState | None:
    acquired_at = datetime.now(timezone.utc)
    attempt = runtime.generations.acquire_staged_recovery(
        request.repo_uuid,
        request.generation_id,
        structural_request,
        attempt_sha256=attempt_sha256,
        acquired_at=acquired_at,
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=_SYNC_LEASE_TTL_NS,
    )
    primary: tuple[BaseException, TracebackType | None] | None = None
    terminal: StagedBuildState | None = None
    source_observation_required = False
    try:
        _sync_fault(runtime, request, "promotion_acquired")
        operation = str(attempt.grant.lease.to_dict()["operation"])
        if operation == "POINTER_RECOVERY":
            pointer = runtime.pointers.recover(
                attempt.grant,
                occurred_at=acquired_at,
                monotonic_ns=time.monotonic_ns(),
            )
        else:
            pointer = runtime.pointers.load(request.repo_uuid, allow_missing=True)
            visible_current = (
                None
                if pointer is None
                else cast(Mapping[str, object], pointer.to_dict()["current"])
            )
            receipt_sha256 = receipt.sha256
            candidate_is_current = visible_current is not None and (
                visible_current["generation_id"],
                visible_current["receipt_sha256"],
            ) == (request.generation_id, receipt_sha256)
            if not candidate_is_current:
                if observations is None:
                    source_observation_required = True
                else:
                    try:
                        runtime.generations.abandon_staged_build(
                            attempt,
                            source_observations=observations,
                            monotonic_ns=time.monotonic_ns(),
                        )
                    except StagedBuildStillCurrent:
                        pass
                    else:
                        raise SyncAuthorityConflict(
                            "stale staged build was terminally abandoned"
                        )
                    pointer = runtime.pointers.promote(
                        attempt.grant,
                        PointerCAS(
                            expected_pointer_revision=request.expected_pointer_revision,
                            expected_active_source_revision=(
                                attempt.grant.active_source_revision
                            ),
                            expected_source_epoch=request.source_epoch,
                            expected_operation_epoch=attempt.grant.operation_epoch,
                            expected_migration_epoch=attempt.grant.migration_epoch,
                            expected_state_schema_version=STATE_SCHEMA_VERSION,
                            expected_fence_token=int(
                                attempt.grant.lease.to_dict()["fence_token"]
                            ),
                            candidate_generation_id=request.generation_id,
                            candidate_receipt_sha256=receipt_sha256,
                            expected_current_receipt_sha256=(
                                request.expected_current_receipt_sha256
                            ),
                        ),
                        occurred_at=acquired_at,
                        monotonic_ns=time.monotonic_ns(),
                    )
        if not source_observation_required:
            if pointer is None:  # pragma: no cover - promotion must return authority
                raise GenerationConflict("promotion returned no visible pointer")
            _sync_fault(runtime, request, "pointer_moved")
            terminal = runtime.generations.complete_staged_promotion(
                attempt,
                pointer,
                monotonic_ns=time.monotonic_ns(),
            )
            _sync_fault(runtime, request, "promotion_completed")
    except BaseException as exc:
        primary = (exc, exc.__traceback__)
    _release_grant(runtime, attempt.grant, primary)
    if source_observation_required:
        return None
    if terminal is None:  # pragma: no cover - guarded by primary exception replay
        raise GenerationConflict("promotion returned no terminal staged state")
    return terminal


def synchronize_code_only(
    runtime: WorkspaceRuntime,
    request: SyncRequest,
) -> SyncReceipt:
    """Build, certify, and atomically promote one exact structural generation."""

    request = SyncRequest.from_mapping(request.to_dict())
    staged: StagedBuildState | None
    try:
        staged = _read_staged_build(runtime, request)
    except (StagedBuildReadRecoveryRequired, StateRecoveryRequired):
        # Reconcile pending bytes before source observation so a different exact
        # request remains the primary recovery barrier and cannot be masked by
        # source availability or generic request conflict classification.
        runtime.generations.recover_staged_build(request.repo_uuid)
        staged = _read_staged_build(runtime, request)

    structural_request: StructuralBuildRequest | None = None
    recovering = False
    if staged is not None:
        exact = (
            staged.generation_id == request.generation_id
            and staged.request.logical_request_sha256 == request.sha256
        )
        if not exact and staged.lifecycle_state not in {"PROMOTED", "ABANDONED"}:
            raise StagedBuildRecoveryRequired(
                "another staged build request requires exact recovery"
            )
        if exact:
            if staged.lifecycle_state == "PROMOTED":
                return _success_receipt(runtime, request, staged)
            if staged.lifecycle_state == "ABANDONED":
                raise SyncAuthorityConflict("exact staged build request was abandoned")
            structural_request = staged.request
            recovering = True

    if staged is not None and staged.lifecycle_state == "CERTIFIED":
        if structural_request is None:  # pragma: no cover - exact nonterminal invariant
            raise GenerationConflict("certified staged build is not exact")
        certified_receipt = runtime.generations.verify_generation(
            request.repo_uuid,
            request.generation_id,
        )
        if certified_receipt.sha256 != staged.receipt_sha256:
            raise GenerationConflict(
                "certified staged state differs from its generation receipt"
            )
        recovery_ready = False
        try:
            visible_pointer = runtime.pointers.load(
                request.repo_uuid,
                allow_missing=True,
            )
        except PointerRecoveryRequired:
            recovery_ready = True
        else:
            if visible_pointer is not None:
                current = cast(
                    Mapping[str, object],
                    visible_pointer.to_dict()["current"],
                )
                recovery_ready = (
                    current["generation_id"],
                    current["receipt_sha256"],
                ) == (request.generation_id, certified_receipt.sha256)
        if recovery_ready:
            terminal = _promote(
                runtime,
                request,
                structural_request,
                certified_receipt,
                None,
                attempt_sha256=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            )
            if terminal is not None:
                _sync_fault(runtime, request, "promotion_released")
                return _success_receipt(runtime, request, terminal)

    source, observations = _observe_structural_source(runtime, request.repo_uuid)
    _sync_fault(runtime, request, "source_observed")
    if structural_request is None:
        structural_request = _structural_request(
            runtime,
            request,
            observations,
        )
    staged = runtime.generations.request_staged_build(
        request.repo_uuid,
        request.generation_id,
        structural_request,
        source_observations=observations,
    )
    _sync_fault(runtime, request, "request_staged")
    if staged.lifecycle_state == "PROMOTED":
        return _success_receipt(runtime, request, staged)
    if staged.lifecycle_state == "ABANDONED":
        raise SyncAuthorityConflict("exact staged build request was abandoned")

    attempt_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    receipt: GenerationReceipt
    if staged.lifecycle_state == "CERTIFIED":
        receipt = runtime.generations.verify_generation(
            request.repo_uuid,
            request.generation_id,
        )
        if receipt.sha256 != staged.receipt_sha256:
            raise GenerationConflict(
                "certified staged state differs from its generation receipt"
            )
    else:
        receipt = _build_and_certify(
            runtime,
            request,
            structural_request,
            source,
            observations,
            attempt_sha256=attempt_sha256,
            recovering=recovering,
        )
        _sync_fault(runtime, request, "build_released")
    terminal = _promote(
        runtime,
        request,
        structural_request,
        receipt,
        observations,
        attempt_sha256=attempt_sha256,
    )
    if terminal is None:  # pragma: no cover - observations were supplied
        raise GenerationConflict("promotion unexpectedly requires source observation")
    _sync_fault(runtime, request, "promotion_released")
    return _success_receipt(runtime, request, terminal)


__all__ = [
    "SYNC_MODE",
    "SYNC_RECEIPT_CONTRACT",
    "SYNC_REQUEST_CONTRACT",
    "SYNC_REQUEST_MAX_BYTES",
    "SYNC_SCHEMA_VERSION",
    "StagedBuildRecoveryRequired",
    "SyncAuthorityConflict",
    "SyncLeaseBusy",
    "SyncReceipt",
    "SyncRequest",
    "SyncRequestInvalid",
    "WorkspaceSyncError",
    "synchronize_code_only",
]
