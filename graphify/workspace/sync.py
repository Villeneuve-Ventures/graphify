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
from typing import Any, Mapping, cast

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
    GenerationConflict,
    StagedBuildReadRecoveryRequired,
    StagedBuildStillCurrent,
    StructuralBuildRequest,
)
from graphify.workspace.identity import SourceIdentity
from graphify.workspace.leases import LeaseGrant
from graphify.workspace.persistence import CommitUnknown, StateRecoveryRequired
from graphify.workspace.pointers import PointerCAS


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
    primary: tuple[Exception, TracebackType | None] | None,
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
    primary: tuple[Exception, TracebackType | None] | None = None
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
    except Exception as exc:
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
    observations: tuple[SourceObservation, SourceObservation],
    *,
    attempt_sha256: str,
) -> StagedBuildState:
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
    primary: tuple[Exception, TracebackType | None] | None = None
    terminal: StagedBuildState | None = None
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
            if pointer is None:  # pragma: no cover - promotion must return authority
                raise GenerationConflict("promotion returned no visible pointer")
        _sync_fault(runtime, request, "pointer_moved")
        terminal = runtime.generations.complete_staged_promotion(
            attempt,
            pointer,
            monotonic_ns=time.monotonic_ns(),
        )
        _sync_fault(runtime, request, "promotion_completed")
    except Exception as exc:
        primary = (exc, exc.__traceback__)
    _release_grant(runtime, attempt.grant, primary)
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
