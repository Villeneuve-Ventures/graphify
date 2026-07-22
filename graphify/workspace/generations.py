"""Immutable generation allocation, sealing, certification, and verification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Mapping, Sequence, cast

from graphify.workspace.adapters import (
    AdapterError,
    AdapterIntent,
    CompatibilityTuple,
    UnsupportedCompatibility,
    select_adapter,
)
from graphify.workspace.identity import IdentityError
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    CapacityPolicy,
    CapacityReservation,
    CapacityReservationState,
    CompatibilityManifest,
    ContractError,
    GenerationCoordinationLock,
    GenerationReceipt,
    PointerSet,
    StagedBuildAbandonmentEvidence,
    StagedBuildAbandonmentIntent,
    StagedBuildAuthorityCurrent,
    StagedBuildState,
    StructuralBuildRequest,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import (
    LeaseGrant,
    LeaseOperation,
    LeaseRecoveryRequired,
    LeaseStore,
)
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    LockTimeout,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
    require_before_deadline,
)
from graphify.workspace.semantic_queue import (
    SemanticCertificationBlocked,
    SemanticCertificationView,
    SemanticQueueStore,
)


_ALLOWED_FILE_MODES = frozenset({0o600, 0o644, 0o755})
_ALLOWED_DIRECTORY_MODES = frozenset({0o700, 0o755})
_CAPACITY_CURRENT = Path("capacity.json")
_CAPACITY_PREVIOUS = Path("capacity.previous.json")
_CAPACITY_PENDING = Path("capacity.pending.json")
_MAX_GENERATION_COORDINATION_LOCK_BYTES = 64 * 1024
_MAX_POINTER_RECORD_BYTES = 64 * 1024
_MAX_STAGED_BUILD_STATE_BYTES = 64 * 1024
_STAGED_BUILD_TERMINAL_STATES = frozenset({"PROMOTED", "ABANDONED"})


class GenerationError(RuntimeError):
    """Base class for stable generation failures."""

    code = "generation_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class CapacityExceeded(GenerationError):
    code = "capacity_exceeded"


class _CapacityScanChanged(RuntimeError):
    pass


def _require_inventory_deadline(deadline_ns: int | None) -> None:
    require_before_deadline(
        deadline_ns,
        "generation inventory exceeded its deadline",
    )


def _require_verification_deadline(deadline_ns: int | None) -> None:
    require_before_deadline(
        deadline_ns,
        "generation verification exceeded its deadline",
    )


def _directory_names(descriptor: int, *, deadline_ns: int | None) -> list[str]:
    names: list[str] = []
    _require_inventory_deadline(deadline_ns)
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            _require_inventory_deadline(deadline_ns)
            names.append(entry.name)
    _require_inventory_deadline(deadline_ns)
    names.sort()
    _require_inventory_deadline(deadline_ns)
    return names


class PayloadChanged(GenerationError):
    code = "payload_changed"


class GenerationConflict(GenerationError):
    code = "generation_conflict"


@dataclass(frozen=True)
class CertificationRequest:
    source_commit: str
    source_epoch: int
    policy_sha256: str
    observation_manifest_sha256: str
    queue_watermark: int
    semantic_completeness: str
    compatibility_sha256: str
    validations: tuple[str, ...]


@dataclass(frozen=True)
class GenerationAllocation:
    repo_uuid: str
    generation_id: str
    staging_path: Path
    expected_payload_bytes: int
    capacity_policy_sha256: str
    compatibility_sha256: str
    active_source_revision: int
    operation_epoch: int
    fence_token: int


@dataclass(frozen=True)
class StagedBuildOperation:
    """One exact request-bound BUILD/PROMOTE lease attempt."""

    state: StagedBuildState
    grant: LeaseGrant


@dataclass(frozen=True)
class StagedBuildPreparation:
    """Canonical empty staging authority for one fenced publisher."""

    state: StagedBuildState
    grant: LeaseGrant
    allocation: GenerationAllocation
    staging_path: Path


@dataclass(frozen=True)
class StagedBuildCompletion:
    """Durable proof that staged payload bytes are complete and source-bound."""

    state: StagedBuildState
    allocation: GenerationAllocation
    entries: tuple[dict[str, str | int], ...]

    @property
    def manifest_sha256(self) -> str:
        manifest_sha256 = self.state.payload_manifest_sha256
        if manifest_sha256 is None:
            raise GenerationConflict(
                "completed staged build is missing a durable payload manifest"
            )
        return manifest_sha256


@dataclass(frozen=True)
class _PayloadInventory:
    entries: tuple[dict[str, str | int], ...]
    directories: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(int(entry["size"]) for entry in self.entries)


@dataclass(frozen=True)
class _Usage:
    bytes_by_generation: Mapping[tuple[str, str], int]
    unconsumed_reserved_bytes: int

    @property
    def global_bytes(self) -> int:
        return sum(self.bytes_by_generation.values())

    @property
    def global_generations(self) -> int:
        return len(self.bytes_by_generation)

    def workspace_bytes(self, repo_uuid: str) -> int:
        return sum(
            size
            for (candidate_uuid, _generation_id), size in self.bytes_by_generation.items()
            if candidate_uuid == repo_uuid
        )

    def workspace_generations(self, repo_uuid: str) -> int:
        return sum(
            1
            for candidate_uuid, _generation_id in self.bytes_by_generation
            if candidate_uuid == repo_uuid
        )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise GenerationError("generation timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


class GenerationStore:
    """Own staged payload validation and the one-way transition to certification."""

    def __init__(
        self,
        state_root: Path,
        leases: LeaseStore,
        journal: JournalStore,
        *,
        compatibility_manifest: CompatibilityManifest,
        semantic_queue: SemanticQueueStore | None = None,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        compatibility = CompatibilityTuple.from_manifest(compatibility_manifest)
        self.adapter = select_adapter(
            compatibility,
            intent=AdapterIntent.STAGE,
        ).require_adapter()
        self.compatibility_sha256 = compatibility_manifest.sha256
        self.leases = leases
        self.journal = journal
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        if self.state.root != leases.state.root or self.state.root != journal.state.root:
            raise GenerationError("generation, journal, and lease stores must share one root")
        if semantic_queue is not None and (
            semantic_queue.state.root != self.state.root or semantic_queue.leases is not leases
        ):
            raise GenerationError(
                "generation and semantic queue stores must share one lease authority"
            )
        self.semantic_queue = semantic_queue
        self.fault_hook = fault_hook or (lambda _event: None)

    @staticmethod
    def _workspace(repo_uuid: str) -> Path:
        return LeaseStore._directory(repo_uuid)

    @classmethod
    def _staging(cls, repo_uuid: str, generation_id: str) -> Path:
        return cls._workspace(repo_uuid) / "staging" / generation_id

    @classmethod
    def _generation(cls, repo_uuid: str, generation_id: str) -> Path:
        return cls._workspace(repo_uuid) / "generations" / generation_id

    @classmethod
    def _lock(cls, repo_uuid: str, generation_id: str) -> Path:
        return cls._workspace(repo_uuid) / "locks" / "generations" / f"{generation_id}.lock"

    @classmethod
    def _staged_build_paths(cls, repo_uuid: str) -> tuple[Path, Path, Path]:
        workspace = cls._workspace(repo_uuid)
        return (
            workspace / "staged-build.json",
            workspace / "staged-build.previous.json",
            workspace / "staged-build.pending.json",
        )

    @staticmethod
    def _validated_structural_request(
        request: StructuralBuildRequest,
    ) -> StructuralBuildRequest:
        try:
            return StructuralBuildRequest.from_mapping(request.to_dict())
        except (AttributeError, ContractError, TypeError, ValueError) as exc:
            raise GenerationConflict(f"staged build request is invalid: {exc}") from exc

    def _load_staged_build_locked(self, repo_uuid: str) -> StagedBuildState | None:
        try:
            return self.leases._load_staged_build_locked(repo_uuid)
        except LeaseRecoveryRequired as exc:
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc

    def _commit_staged_build_locked(self, state: StagedBuildState) -> StagedBuildState:
        try:
            state = StagedBuildState.from_mapping(state.to_dict())
        except ContractError as exc:
            raise GenerationConflict(f"staged build state is invalid: {exc}") from exc
        if len(state.canonical) > _MAX_STAGED_BUILD_STATE_BYTES:
            raise GenerationConflict("staged build state exceeds its fixed byte budget")
        current, previous, pending = self._staged_build_paths(state.repo_uuid)
        return self.state.commit_record(
            label=f"staged-build:{state.repo_uuid}",
            current=current,
            previous=previous,
            pending=pending,
            payload=state.canonical,
            decoder=StagedBuildState.from_json,
        )

    @staticmethod
    def _source_observation_document(observation: SourceObservation) -> dict[str, object]:
        if observation.stable_inventory_passes != 2:
            raise GenerationConflict("source observation requires exactly two stable passes")
        return {
            "source_commit": observation.source_commit,
            "inventory_sha256": observation.inventory_sha256,
            "policy_sha256": observation.policy_sha256,
            "detector_id": observation.detector_id,
            "stable_inventory_passes": observation.stable_inventory_passes,
            "entries_sha256": hashlib.sha256(
                canonical_json_bytes([entry.to_dict() for entry in observation.entries])
            ).hexdigest(),
        }

    @classmethod
    def structural_observation_evidence_sha256(
        cls,
        source_observations: Sequence[SourceObservation],
    ) -> str:
        """Return the canonical digest shared with semantic observation evidence."""

        if len(source_observations) != 2:
            raise GenerationConflict("exactly two source observations are required")
        documents = tuple(
            cls._source_observation_document(observation)
            for observation in source_observations
        )
        if documents[0] != documents[1]:
            raise GenerationConflict("repeated source observations differ")
        return hashlib.sha256(canonical_json_bytes(list(documents))).hexdigest()

    def _trusted_structural_observations(
        self,
        repo_uuid: str,
        expected: Sequence[SourceObservation],
    ) -> tuple[SourceObservation, SourceObservation]:
        try:
            source = self.leases.registry.resolve_active_source(repo_uuid)
            trusted = (
                self.adapter.observe(source.root),
                self.adapter.observe(source.root),
            )
            confirmed_source = self.leases.registry.resolve_active_source(repo_uuid)
        except (AdapterError, IdentityError, OSError, StateCorrupt, StatePathError) as exc:
            raise GenerationError(
                f"trusted source observations are unavailable: {exc}"
            ) from exc
        if confirmed_source != source:
            raise GenerationConflict("trusted source identity changed during observation")
        if trusted[0] != trusted[1]:
            raise GenerationConflict("trusted source observations are not stable")
        if tuple(expected) != trusted:
            raise GenerationConflict(
                "caller evidence differs from trusted source observations"
            )
        return trusted

    def _require_structural_evidence(
        self,
        repo_uuid: str,
        request: StructuralBuildRequest,
        source_observations: Sequence[SourceObservation],
    ) -> tuple[SourceObservation, SourceObservation]:
        trusted = self._trusted_structural_observations(
            repo_uuid,
            source_observations,
        )
        evidence_sha256 = self.structural_observation_evidence_sha256(trusted)
        observation = trusted[0]
        expected = (
            request.source_commit,
            request.policy_sha256,
            request.observation_manifest_sha256,
            request.observation_evidence_sha256,
        )
        actual = (
            observation.source_commit,
            observation.policy_sha256,
            observation.inventory_sha256,
            evidence_sha256,
        )
        if expected != actual:
            raise GenerationConflict(
                "staged build request differs from trusted source evidence"
            )
        return trusted

    @staticmethod
    def _staged_state(
        *,
        revision: int,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        lifecycle_state: str,
        operation_epoch: int | None = None,
        fence_token: int | None = None,
        payload_manifest_sha256: str | None = None,
        receipt_sha256: str | None = None,
        pointer_revision: int | None = None,
        abandonment_intent: StagedBuildAbandonmentIntent | None = None,
        abandoned_from: str | None = None,
        abandon_reason: str | None = None,
        abandon_evidence: StagedBuildAbandonmentEvidence | None = None,
    ) -> StagedBuildState:
        try:
            return StagedBuildState.from_mapping(
                StagedBuildState(
                    revision=revision,
                    repo_uuid=repo_uuid,
                    generation_id=generation_id,
                    request=request,
                    lifecycle_state=lifecycle_state,
                    operation_epoch=operation_epoch,
                    fence_token=fence_token,
                    payload_manifest_sha256=payload_manifest_sha256,
                    receipt_sha256=receipt_sha256,
                    pointer_revision=pointer_revision,
                    abandonment_intent=abandonment_intent,
                    abandoned_from=abandoned_from,
                    abandon_reason=abandon_reason,
                    abandon_evidence=abandon_evidence,
                    abandon_evidence_sha256=(
                        None if abandon_evidence is None else abandon_evidence.sha256
                    ),
                ).to_dict()
            )
        except ContractError as exc:
            raise GenerationConflict(f"staged build state is invalid: {exc}") from exc

    @staticmethod
    def _require_staged_binding(
        state: StagedBuildState,
        *,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        allow_abandonment_intent: bool = False,
    ) -> None:
        if (
            state.repo_uuid != repo_uuid
            or state.generation_id != generation_id
            or state.request.sha256 != request.sha256
        ):
            raise GenerationConflict("staged build request binding does not match")
        if state.abandonment_intent is not None and not allow_abandonment_intent:
            raise GenerationConflict(
                "durable staged abandonment requires exact recovery"
            )

    def _visible_pointer_locked(self, repo_uuid: str) -> PointerSet | None:
        current_relative = self._workspace(repo_uuid) / "pointers.json"
        try:
            raw = self.state.read_optional_existing_bytes(
                current_relative,
                max_bytes=_MAX_POINTER_RECORD_BYTES,
            )
            pointer = None if raw is None else cast(PointerSet, PointerSet.from_json(raw))
        except Exception as exc:
            raise GenerationConflict(f"visible pointer CAS authority is invalid: {exc}") from exc
        if pointer is not None and pointer.to_dict()["repo_uuid"] != repo_uuid:
            raise GenerationConflict("visible pointer belongs to another workspace")
        return pointer

    @staticmethod
    def _pointer_cas(pointer: PointerSet | None) -> tuple[int, str | None]:
        if pointer is None:
            return (0, None)
        value = pointer.to_dict()
        current = cast(dict[str, Any], value["current"])
        return (int(value["pointer_revision"]), str(current["receipt_sha256"]))

    def _pointer_cas_locked(self, repo_uuid: str) -> tuple[int, str | None]:
        return self._pointer_cas(self._visible_pointer_locked(repo_uuid))

    def _require_pointer_cas_locked(
        self,
        repo_uuid: str,
        request: StructuralBuildRequest,
    ) -> None:
        actual = self._pointer_cas_locked(repo_uuid)
        expected = (
            request.expected_pointer_revision,
            request.expected_current_receipt_sha256,
        )
        if expected != actual:
            raise GenerationConflict(
                "pointer CAS differs from staged build request authority"
            )

    def request_staged_build(
        self,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        *,
        source_observations: Sequence[SourceObservation],
    ) -> StagedBuildState:
        """Durably install exact request authority before BUILD acquisition."""

        request = self._validated_structural_request(request)
        self._lock_document(generation_id)
        try:
            with self.leases.registry.recovered_snapshot():
                with self.leases.workspace_lock(repo_uuid):
                    prior = self._load_staged_build_locked(repo_uuid)
                    if prior is not None and prior.request.sha256 == request.sha256:
                        self._require_staged_binding(
                            prior,
                            repo_uuid=repo_uuid,
                            generation_id=generation_id,
                            request=request,
                            allow_abandonment_intent=True,
                        )
                        return prior
        except LeaseRecoveryRequired as exc:
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc
        if request.compatibility_sha256 != self.compatibility_sha256:
            raise UnsupportedCompatibility(
                "staged build request does not match the selected compatibility manifest"
            )
        self._require_structural_evidence(repo_uuid, request, source_observations)
        try:
            with self.leases._bound_request_state(repo_uuid) as (document, entry, lease_state):
                prior = self._load_staged_build_locked(repo_uuid)
                if prior is not None and prior.request.sha256 == request.sha256:
                    self._require_staged_binding(
                        prior,
                        repo_uuid=repo_uuid,
                        generation_id=generation_id,
                        request=request,
                        allow_abandonment_intent=True,
                    )
                    return prior
                if (
                    prior is not None
                    and prior.lifecycle_state not in _STAGED_BUILD_TERMINAL_STATES
                ):
                    raise GenerationConflict(
                        "another staged build request requires exact recovery"
                    )
                LeaseStore._check_expected(
                    document,
                    entry,
                    lease_state,
                    expected_registry_revision=request.expected_registry_revision,
                    expected_active_source_revision=request.expected_active_source_revision,
                    expected_operation_epoch=request.expected_operation_epoch,
                    expected_migration_epoch=request.expected_migration_epoch,
                )
                self.leases._assert_recovery_barriers_locked(repo_uuid, "BUILD")
                self._require_pointer_cas_locked(repo_uuid, request)
                if lease_state.leases:
                    raise GenerationConflict(
                        "staged build request cannot start while a lease is active"
                    )
                for relative, label in (
                    (self._staging(repo_uuid, generation_id), "staging"),
                    (self._generation(repo_uuid, generation_id), "certified"),
                ):
                    if self.state.private_directory_exists(relative):
                        raise GenerationConflict(
                            f"generation already has a {label} directory"
                        )
                if self.state.private_file_exists(self._lock(repo_uuid, generation_id)):
                    raise GenerationConflict("generation coordination lock already exists")
                requested = self._staged_state(
                    revision=1 if prior is None else prior.revision + 1,
                    repo_uuid=repo_uuid,
                    generation_id=generation_id,
                    request=request,
                    lifecycle_state="REQUESTED",
                )
                committed = self._commit_staged_build_locked(requested)
                self.fault_hook(f"generation:{generation_id}:request_durable")
                return committed
        except LeaseRecoveryRequired as exc:
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc

    def acquire_staged_operation(
        self,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        *,
        attempt_sha256: str,
        operation: str,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> StagedBuildOperation:
        request = self._validated_structural_request(request)
        grant = self.leases.acquire_staged_request(
            repo_uuid,
            generation_id,
            operation,
            self.leases.current_owner(),
            request,
            attempt_sha256=attempt_sha256,
            acquired_at=acquired_at,
            monotonic_ns=monotonic_ns,
            ttl_ns=ttl_ns,
        )
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({operation}),
        ) as current:
            state = self._load_staged_build_locked(repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request disappeared after acquisition")
            self._require_staged_binding(
                state,
                repo_uuid=current.repo_uuid,
                generation_id=generation_id,
                request=request,
            )
        if operation in {"PROMOTE", "POINTER_RECOVERY"}:
            with self.leases.current_operation(
                grant,
                monotonic_ns=monotonic_ns,
                allowed_operations=frozenset({operation}),
                registry_required=True,
            ) as capacity_operation:
                self._clear_reservation_locked(
                    capacity_operation.repo_uuid,
                    generation_id,
                )
        return StagedBuildOperation(state=state, grant=grant)

    def acquire_staged_recovery(
        self,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        *,
        attempt_sha256: str,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> StagedBuildOperation:
        """Acquire the exact fenced lane that may recover or close stale authority."""

        request = self._validated_structural_request(request)
        try:
            with self.leases.registry.recovered_snapshot():
                with self.leases.workspace_lock(repo_uuid):
                    state = self._load_staged_build_locked(repo_uuid)
                    if state is None:
                        raise GenerationConflict("staged build request is missing")
                    self._require_staged_binding(
                        state,
                        repo_uuid=repo_uuid,
                        generation_id=generation_id,
                        request=request,
                        allow_abandonment_intent=True,
                    )
                    if state.lifecycle_state in _STAGED_BUILD_TERMINAL_STATES:
                        raise GenerationConflict(
                            f"staged build is already {state.lifecycle_state}"
                        )
                    if state.lifecycle_state == "CERTIFIED":
                        pending = self._workspace(repo_uuid) / "pointers.pending.json"
                        operation = (
                            "POINTER_RECOVERY"
                            if self.state.private_file_exists(pending)
                            else "PROMOTE"
                        )
                    else:
                        operation = "BUILD"
        except LeaseRecoveryRequired as exc:
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc

        grant = self.leases.acquire_staged_recovery(
            repo_uuid,
            generation_id,
            operation,
            self.leases.current_owner(),
            request,
            attempt_sha256=attempt_sha256,
            acquired_at=acquired_at,
            monotonic_ns=monotonic_ns,
            ttl_ns=ttl_ns,
        )
        with self.leases.current_staged_recovery(
            grant,
            generation_id,
            request,
            monotonic_ns=monotonic_ns,
        ):
            state = self._load_staged_build_locked(repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request disappeared after acquisition")
            self._require_staged_binding(
                state,
                repo_uuid=repo_uuid,
                generation_id=generation_id,
                request=request,
                allow_abandonment_intent=True,
            )
        return StagedBuildOperation(state=state, grant=grant)

    @classmethod
    def _abandonment_source_document(
        cls,
        source_observations: Sequence[SourceObservation],
    ) -> dict[str, object]:
        trusted = tuple(source_observations)
        return {
            "observation": cls._source_observation_document(trusted[0]),
            "observation_evidence_sha256": cls.structural_observation_evidence_sha256(
                trusted
            ),
        }

    @staticmethod
    def _frozen_abandonment_source_document(
        request: StructuralBuildRequest,
    ) -> dict[str, object]:
        return {
            "observation": request.source_observation_document(),
            "observation_evidence_sha256": request.observation_evidence_sha256,
        }

    @staticmethod
    def _registry_workspace_entry(
        operation: LeaseOperation,
    ) -> tuple[int, dict[str, Any]]:
        registry_value = operation.registry.to_dict()
        entries = [
            cast(dict[str, Any], item)
            for item in cast(list[object], registry_value["workspaces"])
            if cast(dict[str, Any], item)["repo_uuid"] == operation.repo_uuid
        ]
        if len(entries) != 1:
            raise GenerationConflict("registry has no singular staged workspace entry")
        return int(registry_value["revision"]), entries[0]

    def _staged_abandonment_proof_if_stale_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        source_document: Mapping[str, object],
    ) -> tuple[str, StagedBuildAbandonmentEvidence, PointerSet | None] | None:
        registry_revision, entry = self._registry_workspace_entry(operation)
        active_source_revision = int(entry["active_source_revision"])
        pointer = self._visible_pointer_locked(operation.repo_uuid)
        pointer_cas = self._pointer_cas(pointer)
        semantic_queue: dict[str, object] | None = None
        if (
            self.semantic_queue is not None
            and active_source_revision == state.request.expected_active_source_revision
            and operation.state.migration_epoch == state.request.expected_migration_epoch
            and pointer_cas
            == (
                state.request.expected_pointer_revision,
                state.request.expected_current_receipt_sha256,
            )
            and self.compatibility_sha256 == state.request.compatibility_sha256
        ):
            queue_snapshot = self.semantic_queue.read_only_snapshot_locked(
                operation.repo_uuid
            )
            reconciliation = queue_snapshot.reconciliation
            if reconciliation is not None:
                if queue_snapshot.active_source_revision != active_source_revision:
                    raise GenerationConflict(
                        "semantic queue authority differs from the active source"
                    )
                semantic_queue = {
                    "source_epoch": reconciliation.source_epoch,
                    "queue_watermark": queue_snapshot.desired_watermark,
                    "queue_state_sha256": queue_snapshot.sha256,
                }
        request = state.request
        evidence = StagedBuildAbandonmentEvidence.from_mapping(
            {
                "request_sha256": request.sha256,
                "registry_revision": registry_revision,
                "active_source_revision": active_source_revision,
                "operation_epoch": operation.state.operation_epoch,
                "migration_epoch": operation.state.migration_epoch,
                "pointer_revision": pointer_cas[0],
                "current_receipt_sha256": pointer_cas[1],
                "selected_compatibility_sha256": self.compatibility_sha256,
                "semantic_queue": semantic_queue,
                "source": source_document,
            }
        )
        try:
            reason = evidence.reason_for(request)
        except StagedBuildAuthorityCurrent:
            return None
        except ContractError as exc:
            raise GenerationConflict(
                f"staged abandonment evidence is invalid: {exc}"
            ) from exc
        return reason, evidence, pointer

    def _staged_abandonment_proof_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        trusted_observations: Sequence[SourceObservation],
    ) -> tuple[str, StagedBuildAbandonmentEvidence, PointerSet | None]:
        proof = self._staged_abandonment_proof_if_stale_locked(
            operation,
            state,
            self._abandonment_source_document(trusted_observations),
        )
        if proof is None:
            raise GenerationConflict(
                "staged build authority is still current and cannot be abandoned"
            )
        return proof

    @staticmethod
    def _pointer_references_staged_generation(
        pointer: PointerSet,
        state: StagedBuildState,
    ) -> bool:
        value = pointer.to_dict()
        references = [cast(dict[str, Any], value["current"])]
        if value["last_good"] is not None:
            references.append(cast(dict[str, Any], value["last_good"]))
        return any(
            reference["generation_id"] == state.generation_id
            and reference["receipt_sha256"] == state.receipt_sha256
            for reference in references
        )

    def _fail_pre_certification_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        *,
        occurred_at: datetime,
    ) -> None:
        snapshot = self.journal.recover_locked(operation)
        events = snapshot.for_generation(state.generation_id)
        latest = None if not events else str(events[-1].to_dict()["transition"])
        if latest == "CERTIFIED" or latest in {
            "PROMOTED",
            "REPAIRED",
            "ROLLED_BACK",
            "SUPERSEDED",
        }:
            raise GenerationConflict(
                "generation has certification authority and cannot be abandoned"
            )
        if latest is not None and latest != "FAILED":
            self.journal.append_generation_locked(
                operation,
                transition="FAILED",
                generation_id=state.generation_id,
                receipt_sha256=None,
                pointer_revision=None,
                occurred_at=occurred_at,
            )
        try:
            self.state.remove_private_tree(
                self._staging(operation.repo_uuid, state.generation_id),
                allowed_directory_modes=_ALLOWED_DIRECTORY_MODES,
                allowed_file_modes=_ALLOWED_FILE_MODES,
            )
        except (OSError, StatePathError) as exc:
            raise GenerationConflict(
                f"stale staging cannot be removed safely: {exc}"
            ) from exc
        self.fault_hook(f"generation:{state.generation_id}:abandon_staging_removed")

    @staticmethod
    def _stale_certification_operation(
        operation: LeaseOperation,
        state: StagedBuildState,
    ) -> LeaseOperation:
        """Retain current fencing while validating the receipt's frozen source revision."""

        recovery_grant = LeaseGrant(
            lease=operation.grant.lease,
            registry_revision=operation.grant.registry_revision,
            active_source_revision=state.request.expected_active_source_revision,
            operation_epoch=operation.grant.operation_epoch,
            migration_epoch=operation.grant.migration_epoch,
        )
        return LeaseOperation(
            registry=operation.registry,
            state=operation.state,
            lease=operation.lease,
            grant=recovery_grant,
        )

    def _stale_certification_inputs_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        receipt: GenerationReceipt,
    ) -> tuple[CertificationRequest, tuple[dict[str, str | int], ...]]:
        if self.semantic_queue is None:
            raise GenerationConflict(
                "staged certification recovery requires semantic queue authority"
            )
        value = receipt.to_dict()
        payload = cast(dict[str, Any], value["sealed_query_payload"])
        entries = tuple(
            cast(dict[str, str | int], item)
            for item in cast(list[object], payload["entries"])
        )
        validations = tuple(str(item) for item in cast(list[object], value["validations"]))
        certification_request = CertificationRequest(
            source_commit=str(value["source_commit"]),
            source_epoch=int(value["source_epoch"]),
            policy_sha256=str(value["policy_sha256"]),
            observation_manifest_sha256=str(value["observation_manifest_sha256"]),
            queue_watermark=int(value["queue_watermark"]),
            semantic_completeness=str(value["semantic_completeness"]),
            compatibility_sha256=str(value["compatibility_sha256"]),
            validations=validations,
        )
        expected = (
            state.repo_uuid,
            state.generation_id,
            state.request.source_commit,
            state.request.source_epoch,
            state.request.expected_active_source_revision,
            state.request.policy_sha256,
            state.request.observation_manifest_sha256,
            state.request.compatibility_sha256,
            state.payload_manifest_sha256,
        )
        actual = (
            value["repo_uuid"],
            value["generation_id"],
            certification_request.source_commit,
            certification_request.source_epoch,
            value["active_source_revision"],
            certification_request.policy_sha256,
            certification_request.observation_manifest_sha256,
            certification_request.compatibility_sha256,
            payload["manifest_sha256"],
        )
        if expected != actual:
            raise GenerationConflict(
                "durable receipt differs from completed staged build authority"
            )
        if "stable_semantic_queue" not in certification_request.validations:
            raise GenerationConflict(
                "durable receipt lacks stable semantic queue validation"
            )
        if payload_manifest_sha256("graphify-out", entries) != state.payload_manifest_sha256:
            raise GenerationConflict(
                "durable receipt payload differs from completed staged manifest"
            )
        request_sha256 = self._semantic_request_sha256(certification_request)
        sealed_input_manifest_sha256 = str(payload["manifest_sha256"])
        queue_view = self.semantic_queue.certification_binding_locked(
            operation,
            generation_id=state.generation_id,
            request_sha256=request_sha256,
            sealed_input_manifest_sha256=sealed_input_manifest_sha256,
        )
        if queue_view is None:
            raise GenerationConflict(
                "durable receipt has no semantic certification binding"
            )
        queue_expected = (
            state.repo_uuid,
            certification_request.source_commit,
            certification_request.source_epoch,
            certification_request.policy_sha256,
            certification_request.observation_manifest_sha256,
            state.request.observation_evidence_sha256,
            certification_request.queue_watermark,
            certification_request.semantic_completeness,
            sealed_input_manifest_sha256,
        )
        queue_actual = (
            queue_view.repo_uuid,
            queue_view.source_commit,
            queue_view.source_epoch,
            queue_view.policy_sha256,
            queue_view.observation_manifest_sha256,
            queue_view.observation_evidence_sha256,
            queue_view.queue_watermark,
            queue_view.semantic_completeness,
            queue_view.sealed_input_manifest_sha256,
        )
        if queue_expected != queue_actual:
            raise GenerationConflict(
                "semantic certification binding differs from completed staged authority"
            )
        return certification_request, entries

    def _recover_stale_certification_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        *,
        occurred_at: datetime,
    ) -> StagedBuildState:
        """Adopt durable semantic/receipt authority before any stale build cleanup."""

        staging = self._staging(operation.repo_uuid, state.generation_id)
        final = self._generation(operation.repo_uuid, state.generation_id)
        staging_exists = self.state.private_directory_exists(staging)
        final_exists = self.state.private_directory_exists(final)
        if staging_exists == final_exists:
            raise GenerationConflict(
                "certification recovery requires exactly one generation location"
            )
        lock = self._lock(operation.repo_uuid, state.generation_id)
        recovery_operation = self._stale_certification_operation(operation, state)
        with self.state.existing_generation_lock(
            lock,
            generation_id=state.generation_id,
            exclusive=True,
        ):
            if final_exists:
                receipt = self.verify_generation(
                    operation.repo_uuid,
                    state.generation_id,
                    _expected_compatibility_sha256=state.request.compatibility_sha256,
                )
            else:
                try:
                    receipt = cast(
                        GenerationReceipt,
                        GenerationReceipt.from_json(
                            self.state.read_existing_bytes(staging / "receipt.json")
                        ),
                    )
                except Exception as exc:
                    raise GenerationConflict(
                        f"durable staged receipt is invalid: {exc}"
                    ) from exc
            certification_request, entries = self._stale_certification_inputs_locked(
                recovery_operation,
                state,
                receipt,
            )
            allocation = GenerationAllocation(
                repo_uuid=state.repo_uuid,
                generation_id=state.generation_id,
                staging_path=self.state.path(staging),
                expected_payload_bytes=state.request.expected_payload_bytes,
                capacity_policy_sha256=state.request.capacity_policy_sha256,
                compatibility_sha256=state.request.compatibility_sha256,
                active_source_revision=state.request.expected_active_source_revision,
                operation_epoch=operation.grant.operation_epoch,
                fence_token=operation.fence_token,
            )
            recovered_receipt = self._certify_locked(
                recovery_operation,
                allocation,
                certification_request,
                declared_entries=entries,
                occurred_at=occurred_at,
                expected_compatibility_sha256=state.request.compatibility_sha256,
            )
        self._clear_reservation_locked(operation.repo_uuid, state.generation_id)
        with self.state.existing_generation_lock(
            lock,
            generation_id=state.generation_id,
            exclusive=True,
        ):
            verified = self.verify_generation(
                operation.repo_uuid,
                state.generation_id,
                _expected_compatibility_sha256=state.request.compatibility_sha256,
            )
            if verified.canonical != recovered_receipt.canonical:
                raise GenerationConflict(
                    "recovered certification changed before staged completion"
                )
            return self._mark_staged_certified_locked(state, recovered_receipt)

    def recover_staged_certification(
        self,
        attempt: StagedBuildOperation,
        *,
        monotonic_ns: int,
    ) -> StagedBuildState:
        """Finish durable certification for one exact stale COMPLETE request."""

        request = self._validated_structural_request(attempt.state.request)
        with self.leases.current_staged_recovery(
            attempt.grant,
            attempt.state.generation_id,
            request,
            monotonic_ns=monotonic_ns,
        ) as operation:
            state = self._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                state,
                repo_uuid=operation.repo_uuid,
                generation_id=attempt.state.generation_id,
                request=request,
            )
            if state.lifecycle_state != "COMPLETE":
                raise GenerationConflict(
                    "staged certification recovery requires COMPLETE state"
                )
            lease_value = attempt.grant.lease.to_dict()
            occurred_at = datetime.fromisoformat(
                str(lease_value["acquired_at"]).replace("Z", "+00:00")
            )
            return self._recover_stale_certification_locked(
                operation,
                state,
                occurred_at=occurred_at,
            )

    @staticmethod
    def _staged_abandonment_intent(
        operation: LeaseOperation,
        state: StagedBuildState,
        *,
        reason: str,
        evidence: StagedBuildAbandonmentEvidence,
    ) -> StagedBuildAbandonmentIntent:
        return StagedBuildAbandonmentIntent.from_mapping(
            {
                "repo_uuid": state.repo_uuid,
                "generation_id": state.generation_id,
                "request_sha256": state.request.sha256,
                "staged_revision": state.revision,
                "abandoned_from": state.lifecycle_state,
                "operation_epoch": operation.grant.operation_epoch,
                "fence_token": operation.fence_token,
                "reason": reason,
                "evidence": evidence.to_dict(),
                "evidence_sha256": evidence.sha256,
            }
        )

    def _commit_staged_abandonment_intent_locked(
        self,
        state: StagedBuildState,
        intent: StagedBuildAbandonmentIntent,
    ) -> StagedBuildState:
        pending = self._staged_state(
            revision=state.revision + 1,
            repo_uuid=state.repo_uuid,
            generation_id=state.generation_id,
            request=state.request,
            lifecycle_state=state.lifecycle_state,
            operation_epoch=state.operation_epoch,
            fence_token=state.fence_token,
            payload_manifest_sha256=state.payload_manifest_sha256,
            receipt_sha256=state.receipt_sha256,
            pointer_revision=state.pointer_revision,
            abandonment_intent=intent,
        )
        committed = self._commit_staged_build_locked(pending)
        self.fault_hook(f"generation:{state.generation_id}:abandon_intent_durable")
        return committed

    def _require_staged_abandonment_safe_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
    ) -> None:
        workspace = self._workspace(operation.repo_uuid)
        if self.state.private_file_exists(workspace / "pointers.pending.json"):
            raise GenerationConflict(
                "pointer intent must be recovered before staged abandonment"
            )
        pointer = self._visible_pointer_locked(operation.repo_uuid)
        if state.lifecycle_state == "CERTIFIED":
            if pointer is not None and self._pointer_references_staged_generation(
                pointer,
                state,
            ):
                raise GenerationConflict(
                    "authoritative promotion is already visible; complete promotion instead"
                )
            lock = self._lock(operation.repo_uuid, state.generation_id)
            with self.state.existing_generation_lock(
                lock,
                generation_id=state.generation_id,
                exclusive=True,
            ):
                receipt = self.verify_generation(
                    operation.repo_uuid,
                    state.generation_id,
                    _expected_compatibility_sha256=state.request.compatibility_sha256,
                )
                if receipt.sha256 != state.receipt_sha256:
                    raise GenerationConflict(
                        "certified staged generation differs from its durable receipt"
                    )
            return
        final = self._generation(operation.repo_uuid, state.generation_id)
        staged_receipt = self._staging(
            operation.repo_uuid,
            state.generation_id,
        ) / "receipt.json"
        if state.lifecycle_state == "COMPLETE" and (
            self.state.private_directory_exists(final)
            or self.state.private_file_exists(staged_receipt)
        ):
            raise GenerationConflict(
                "certification recovery is required before staged abandonment"
            )

    def _finish_staged_abandonment_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        intent: StagedBuildAbandonmentIntent,
        *,
        occurred_at: datetime,
    ) -> StagedBuildState:
        if state.abandonment_intent != intent:
            raise GenerationConflict(
                "durable staged abandonment intent changed before recovery"
            )
        if (
            intent.operation_epoch > operation.grant.operation_epoch
            or intent.fence_token > operation.fence_token
        ):
            raise GenerationConflict(
                "durable staged abandonment intent belongs to a newer fence"
            )
        self._require_staged_abandonment_safe_locked(operation, state)
        if state.lifecycle_state != "CERTIFIED":
            lock = self._lock(operation.repo_uuid, state.generation_id)
            if self.state.private_file_exists(lock):
                with self.state.existing_generation_lock(
                    lock,
                    generation_id=state.generation_id,
                    exclusive=True,
                ):
                    self._fail_pre_certification_locked(
                        operation,
                        state,
                        occurred_at=occurred_at,
                    )
            else:
                self._fail_pre_certification_locked(
                    operation,
                    state,
                    occurred_at=occurred_at,
                )
        self._clear_reservation_locked(operation.repo_uuid, state.generation_id)
        self.fault_hook(f"generation:{state.generation_id}:abandon_capacity_cleared")
        abandoned = self._staged_state(
            revision=state.revision + 1,
            repo_uuid=state.repo_uuid,
            generation_id=state.generation_id,
            request=state.request,
            lifecycle_state="ABANDONED",
            operation_epoch=operation.grant.operation_epoch,
            fence_token=operation.fence_token,
            payload_manifest_sha256=state.payload_manifest_sha256,
            receipt_sha256=state.receipt_sha256,
            abandoned_from=intent.abandoned_from,
            abandon_reason=intent.reason,
            abandon_evidence=intent.evidence,
        )
        committed = self._commit_staged_build_locked(abandoned)
        self.fault_hook(f"generation:{state.generation_id}:staged_abandoned_durable")
        return committed

    def _commit_new_staged_abandonment_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        *,
        reason: str,
        evidence: StagedBuildAbandonmentEvidence,
        occurred_at: datetime,
    ) -> StagedBuildState:
        self._require_staged_abandonment_safe_locked(operation, state)
        intent = self._staged_abandonment_intent(
            operation,
            state,
            reason=reason,
            evidence=evidence,
        )
        state = self._commit_staged_abandonment_intent_locked(state, intent)
        return self._finish_staged_abandonment_locked(
            operation,
            state,
            intent,
            occurred_at=occurred_at,
        )

    def abandon_staged_build(
        self,
        attempt: StagedBuildOperation,
        *,
        source_observations: Sequence[SourceObservation],
        monotonic_ns: int,
    ) -> StagedBuildState:
        """Close one provably stale staged request without publishing its bytes."""

        request = self._validated_structural_request(attempt.state.request)
        with self.leases.current_staged_recovery(
            attempt.grant,
            attempt.state.generation_id,
            request,
            monotonic_ns=monotonic_ns,
        ) as operation:
            state = self._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                state,
                repo_uuid=operation.repo_uuid,
                generation_id=attempt.state.generation_id,
                request=request,
                allow_abandonment_intent=True,
            )
            lease_value = attempt.grant.lease.to_dict()
            occurred_at = datetime.fromisoformat(
                str(lease_value["acquired_at"]).replace("Z", "+00:00")
            )
            if state.abandonment_intent is not None:
                return self._finish_staged_abandonment_locked(
                    operation,
                    state,
                    state.abandonment_intent,
                    occurred_at=occurred_at,
                )
            proof = self._staged_abandonment_proof_if_stale_locked(
                operation,
                state,
                self._frozen_abandonment_source_document(request),
            )
            if proof is not None:
                reason, evidence, _pointer = proof
                return self._commit_new_staged_abandonment_locked(
                    operation,
                    state,
                    reason=reason,
                    evidence=evidence,
                    occurred_at=occurred_at,
                )

        trusted = self._trusted_structural_observations(
            attempt.state.repo_uuid,
            source_observations,
        )
        with self.leases.current_staged_recovery(
            attempt.grant,
            attempt.state.generation_id,
            request,
            monotonic_ns=monotonic_ns,
        ) as operation:
            state = self._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                state,
                repo_uuid=operation.repo_uuid,
                generation_id=attempt.state.generation_id,
                request=request,
                allow_abandonment_intent=True,
            )
            lease_value = attempt.grant.lease.to_dict()
            occurred_at = datetime.fromisoformat(
                str(lease_value["acquired_at"]).replace("Z", "+00:00")
            )
            intent = state.abandonment_intent
            if intent is None:
                reason, evidence, _pointer = self._staged_abandonment_proof_locked(
                    operation,
                    state,
                    trusted,
                )
                return self._commit_new_staged_abandonment_locked(
                    operation,
                    state,
                    reason=reason,
                    evidence=evidence,
                    occurred_at=occurred_at,
                )
            return self._finish_staged_abandonment_locked(
                operation,
                state,
                intent,
                occurred_at=occurred_at,
            )

    def _load_capacity_locked(self) -> CapacityReservationState | None:
        try:
            return self.state.recover_record(
                label="capacity",
                current=_CAPACITY_CURRENT,
                previous=_CAPACITY_PREVIOUS,
                pending=_CAPACITY_PENDING,
                decoder=CapacityReservationState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
            )
        except StateCorrupt as exc:
            raise CapacityExceeded(f"capacity reservation state is corrupt: {exc}") from exc

    @staticmethod
    def _validated_capacity_policy(policy: CapacityPolicy) -> CapacityPolicy:
        try:
            return CapacityPolicy.from_mapping(policy.to_dict())
        except ContractError as exc:
            raise CapacityExceeded(f"capacity policy is invalid: {exc}") from exc

    def _commit_capacity_locked(
        self,
        reservations: Sequence[CapacityReservation],
        *,
        prior_revision: int,
    ) -> CapacityReservationState:
        document = CapacityReservationState(
            revision=prior_revision + 1,
            reservations=tuple(
                sorted(reservations, key=lambda item: (item.repo_uuid, item.generation_id))
            ),
        )
        return self.state.commit_record(
            label="capacity",
            current=_CAPACITY_CURRENT,
            previous=_CAPACITY_PREVIOUS,
            pending=_CAPACITY_PENDING,
            payload=document.canonical,
            decoder=CapacityReservationState.from_json,
        )

    def _scan_usage_once(self) -> dict[tuple[str, str], int]:
        usage: dict[tuple[str, str], int] = {}
        try:
            workspaces = Path("workspaces")
            for repo_uuid in self.state.list_existing_private_directories(
                workspaces,
                allow_missing=True,
            ):
                workspace = workspaces / repo_uuid
                containers: list[tuple[Path, bool]] = [
                    (workspace / "generations", False),
                    (workspace / "staging", False),
                ]
                quarantine_root = workspace / "quarantine"
                quarantine_kinds = self.state.list_existing_private_directories(
                    quarantine_root,
                    allow_missing=True,
                )
                containers.extend(
                    (quarantine_root / quarantine_kind, True)
                    for quarantine_kind in ("gc", "corrupt")
                    if quarantine_kind in quarantine_kinds
                )
                for container, strips_epoch in containers:
                    for name in self.state.list_existing_private_directories(
                        container,
                        allow_missing=True,
                    ):
                        generation_id = name.rsplit(".", 1)[0] if strips_epoch else name
                        key = (repo_uuid, generation_id)
                        if key in usage:
                            raise _CapacityScanChanged(
                                "generation occupies multiple active/staging/quarantine locations: "
                                f"{repo_uuid}/{generation_id}"
                            )
                        usage[key] = self.state.tree_bytes(
                            container / name,
                            allowed_directory_modes=_ALLOWED_DIRECTORY_MODES,
                            allowed_file_modes=_ALLOWED_FILE_MODES,
                        )
        except StatePathError as exc:
            raise CapacityExceeded(f"unsafe state path in capacity scan: {exc}") from exc
        return usage

    def _usage(self, reservations: Sequence[CapacityReservation]) -> _Usage:
        previous: dict[tuple[str, str], int] | None = None
        repeated_change: str | None = None
        usage: dict[tuple[str, str], int] | None = None
        for _attempt in range(5):
            try:
                observed = self._scan_usage_once()
            except FileNotFoundError:
                previous = None
                repeated_change = None
                continue
            except _CapacityScanChanged as exc:
                detail = str(exc)
                if detail == repeated_change:
                    raise CapacityExceeded(detail) from exc
                previous = None
                repeated_change = detail
                continue
            repeated_change = None
            if previous is not None and observed == previous:
                usage = observed
                break
            previous = observed
        if usage is None:
            raise CapacityExceeded("capacity filesystem snapshot did not stabilize")
        physical_usage = dict(usage)
        unconsumed_reserved_bytes = sum(
            max(
                reservation.reserved_bytes
                - physical_usage.get((reservation.repo_uuid, reservation.generation_id), 0),
                0,
            )
            for reservation in reservations
        )
        for reservation in reservations:
            key = (reservation.repo_uuid, reservation.generation_id)
            usage[key] = max(usage.get(key, 0), reservation.reserved_bytes)
        return _Usage(usage, unconsumed_reserved_bytes)

    def _preflight(
        self,
        *,
        repo_uuid: str,
        generation_id: str,
        expected_payload_bytes: int,
        policy: CapacityPolicy,
        reservations: Sequence[CapacityReservation],
    ) -> None:
        if expected_payload_bytes < 1:
            raise CapacityExceeded("expected_payload_bytes must be positive")
        existing = next(
            (
                item
                for item in reservations
                if item.repo_uuid == repo_uuid and item.generation_id == generation_id
            ),
            None,
        )
        usage = self._usage(reservations)
        key = (repo_uuid, generation_id)
        prior = usage.bytes_by_generation.get(key, 0)
        projected_size = max(prior, expected_payload_bytes)
        projected_global_bytes = usage.global_bytes - prior + projected_size
        projected_workspace_bytes = usage.workspace_bytes(repo_uuid) - prior + projected_size
        additional_generation = 0 if key in usage.bytes_by_generation else 1
        if projected_workspace_bytes > policy.workspace_max_bytes:
            raise CapacityExceeded("workspace byte limit would be exceeded")
        if projected_global_bytes > policy.global_max_bytes:
            raise CapacityExceeded("global byte limit would be exceeded")
        if usage.workspace_generations(repo_uuid) + additional_generation > policy.workspace_max_generations:
            raise CapacityExceeded("workspace generation limit would be exceeded")
        if usage.global_generations + additional_generation > policy.global_max_generations:
            raise CapacityExceeded("global generation limit would be exceeded")
        available = shutil.disk_usage(self.state.root.parent).free
        additional_bytes = 0 if existing is not None else expected_payload_bytes
        if (
            available - usage.unconsumed_reserved_bytes - additional_bytes
            < policy.reserve_bytes
        ):
            raise CapacityExceeded("filesystem reserve threshold would be violated")

    def _reserve_locked(
        self,
        operation: LeaseOperation,
        *,
        generation_id: str,
        expected_payload_bytes: int,
        policy: CapacityPolicy,
        occurred_at: datetime,
    ) -> CapacityReservation:
        state = self._load_capacity_locked()
        reservations = [] if state is None else list(state.reservations)
        existing = next(
            (
                item
                for item in reservations
                if (item.repo_uuid, item.generation_id)
                == (operation.repo_uuid, generation_id)
            ),
            None,
        )
        if existing is not None:
            if (
                existing.reserved_bytes != expected_payload_bytes
                or existing.policy_sha256 != policy.sha256
                or existing.compatibility_sha256 != self.compatibility_sha256
                or existing.active_source_revision
                != operation.grant.active_source_revision
            ):
                raise GenerationConflict("generation has a different durable capacity reservation")
            if (
                existing.operation_epoch == operation.grant.operation_epoch
                and existing.fence_token == operation.fence_token
            ):
                return existing
            if (
                existing.operation_epoch >= operation.grant.operation_epoch
                or existing.fence_token >= operation.fence_token
            ):
                raise GenerationConflict("generation reservation is owned by a newer fence")
            adopted = CapacityReservation(
                repo_uuid=operation.repo_uuid,
                generation_id=generation_id,
                reserved_bytes=existing.reserved_bytes,
                policy_sha256=existing.policy_sha256,
                compatibility_sha256=existing.compatibility_sha256,
                active_source_revision=existing.active_source_revision,
                operation_epoch=operation.grant.operation_epoch,
                fence_token=operation.fence_token,
                created_at=_timestamp(occurred_at),
            )
            reservations[reservations.index(existing)] = adopted
            self._commit_capacity_locked(
                reservations,
                prior_revision=0 if state is None else state.revision,
            )
            self.fault_hook(f"generation:{generation_id}:capacity_adopted")
            return adopted
        self._preflight(
            repo_uuid=operation.repo_uuid,
            generation_id=generation_id,
            expected_payload_bytes=expected_payload_bytes,
            policy=policy,
            reservations=reservations,
        )
        reservation = CapacityReservation(
            repo_uuid=operation.repo_uuid,
            generation_id=generation_id,
            reserved_bytes=expected_payload_bytes,
            policy_sha256=policy.sha256,
            compatibility_sha256=self.compatibility_sha256,
            active_source_revision=operation.grant.active_source_revision,
            operation_epoch=operation.grant.operation_epoch,
            fence_token=operation.fence_token,
            created_at=_timestamp(occurred_at),
        )
        reservations.append(reservation)
        self._commit_capacity_locked(
            reservations,
            prior_revision=0 if state is None else state.revision,
        )
        self.fault_hook(f"generation:{generation_id}:capacity_reserved")
        return reservation

    def _clear_reservation_locked(self, repo_uuid: str, generation_id: str) -> None:
        state = self._load_capacity_locked()
        if state is None:
            return
        retained = [
            item
            for item in state.reservations
            if (item.repo_uuid, item.generation_id) != (repo_uuid, generation_id)
        ]
        if len(retained) == len(state.reservations):
            return
        self._commit_capacity_locked(retained, prior_revision=state.revision)
        self.fault_hook(f"generation:{generation_id}:capacity_released")

    @staticmethod
    def _lock_document(generation_id: str) -> GenerationCoordinationLock:
        return cast(
            GenerationCoordinationLock,
            GenerationCoordinationLock.from_mapping(
                {
                    "contract": "graphify.workspace.generation_coordination_lock",
                    "schema_version": 1,
                    "lock_id": f"generation:{generation_id[4:]}",
                    "generation_id": generation_id,
                    "relative_path": f"locks/generations/{generation_id}.lock",
                    "installed_before_state": "CERTIFIED",
                    "query_lock": "read_only_shared_advisory",
                    "gc_lock": "exclusive_then_reachability_recheck",
                    "retention": "retain_v1",
                }
            ),
        )

    def allocate(
        self,
        grant: LeaseGrant,
        *,
        expected_payload_bytes: int,
        capacity_policy: CapacityPolicy,
        generation_id: str,
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> GenerationAllocation:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
            registry_required=True,
        ) as capacity_operation:
            self._require_staged_allocation_request_locked(
                capacity_operation,
                generation_id=generation_id,
                expected_payload_bytes=expected_payload_bytes,
                capacity_policy=capacity_policy,
            )
            final_relative = self._generation(
                capacity_operation.repo_uuid,
                generation_id,
            )
            capacity_state = self._load_capacity_locked()
            existing = (
                None
                if capacity_state is None
                else next(
                    (
                        item
                        for item in capacity_state.reservations
                        if (item.repo_uuid, item.generation_id)
                        == (capacity_operation.repo_uuid, generation_id)
                    ),
                    None,
                )
            )
            try:
                final_exists = self.state.private_directory_exists(final_relative)
            except StatePathError as exc:
                raise CapacityExceeded(f"unsafe state path in capacity scan: {exc}") from exc
            if final_exists and existing is None:
                staged = self._load_staged_build_locked(capacity_operation.repo_uuid)
                if staged is None or (
                    staged.lifecycle_state != "COMPLETE"
                    or staged.generation_id != generation_id
                    or staged.request.expected_payload_bytes != expected_payload_bytes
                    or staged.request.capacity_policy_sha256 != capacity_policy.sha256
                    or staged.request.compatibility_sha256 != self.compatibility_sha256
                    or staged.request.expected_active_source_revision
                    != capacity_operation.grant.active_source_revision
                ):
                    raise GenerationConflict("generation is already certified")
                reservation = CapacityReservation(
                    repo_uuid=capacity_operation.repo_uuid,
                    generation_id=generation_id,
                    reserved_bytes=expected_payload_bytes,
                    policy_sha256=capacity_policy.sha256,
                    compatibility_sha256=self.compatibility_sha256,
                    active_source_revision=capacity_operation.grant.active_source_revision,
                    operation_epoch=capacity_operation.grant.operation_epoch,
                    fence_token=capacity_operation.fence_token,
                    created_at=_timestamp(occurred_at),
                )
                self.fault_hook(
                    f"generation:{generation_id}:certification_recovery_allocation"
                )
            else:
                reservation = self._reserve_locked(
                    capacity_operation,
                    generation_id=generation_id,
                    expected_payload_bytes=expected_payload_bytes,
                    policy=capacity_policy,
                    occurred_at=occurred_at,
                )

        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
        ) as operation:
            final_relative = self._generation(operation.repo_uuid, generation_id)
            staging_relative = self._staging(operation.repo_uuid, generation_id)
            staging_path = self.state.path(staging_relative)
            final_exists = self.state.private_directory_exists(final_relative)
            staging_exists = self.state.private_directory_exists(staging_relative)
            if final_exists and staging_exists:
                raise GenerationConflict("generation exists in both staging and final locations")
            if final_exists:
                self.verify_generation(operation.repo_uuid, generation_id)
            else:
                staging_path = self.state.ensure_directory(staging_relative)
                lock_document = self._lock_document(generation_id)
                lock_relative = self._lock(operation.repo_uuid, generation_id)
                self.state.install_once_bytes(
                    lock_relative,
                    lock_document.canonical,
                    label=f"generation:{generation_id}:lock",
                )
                self.fault_hook(f"generation:{generation_id}:lock_durable")

            snapshot = self.journal.recover_locked(operation)
            events = snapshot.for_generation(generation_id)
            latest = None if not events else str(events[-1].to_dict()["transition"])
            if latest is None:
                if self.state.private_directory_exists(final_relative):
                    raise GenerationConflict("installed generation has no lifecycle journal")
                self.journal.append_generation_locked(
                    operation,
                    transition="ALLOCATED",
                    generation_id=generation_id,
                    receipt_sha256=None,
                    pointer_revision=None,
                    occurred_at=occurred_at,
                )
                latest = "ALLOCATED"
            if latest == "ALLOCATED":
                self.journal.append_generation_locked(
                    operation,
                    transition="STAGING",
                    generation_id=generation_id,
                    receipt_sha256=None,
                    pointer_revision=None,
                    occurred_at=occurred_at,
                )
                latest = "STAGING"
            if latest not in {"STAGING", "BUILT", "VALIDATING", "CERTIFIED"}:
                raise GenerationConflict(
                    f"generation lifecycle cannot be adopted from {latest}"
                )
            return GenerationAllocation(
                repo_uuid=operation.repo_uuid,
                generation_id=generation_id,
                staging_path=staging_path,
                expected_payload_bytes=reservation.reserved_bytes,
                capacity_policy_sha256=reservation.policy_sha256,
                compatibility_sha256=reservation.compatibility_sha256,
                active_source_revision=reservation.active_source_revision,
                operation_epoch=reservation.operation_epoch,
                fence_token=reservation.fence_token,
            )

    @staticmethod
    def _require_structural_allocation(
        state: StagedBuildState,
        allocation: GenerationAllocation,
    ) -> None:
        request = state.request
        expected = (
            state.repo_uuid,
            state.generation_id,
            request.expected_payload_bytes,
            request.capacity_policy_sha256,
            request.compatibility_sha256,
            request.expected_active_source_revision,
        )
        actual = (
            allocation.repo_uuid,
            allocation.generation_id,
            allocation.expected_payload_bytes,
            allocation.capacity_policy_sha256,
            allocation.compatibility_sha256,
            allocation.active_source_revision,
        )
        if expected != actual:
            raise GenerationConflict(
                "generation allocation differs from its staged build request"
            )

    def _require_staged_allocation_request_locked(
        self,
        operation: LeaseOperation,
        *,
        generation_id: str,
        expected_payload_bytes: int,
        capacity_policy: CapacityPolicy,
    ) -> StagedBuildState | None:
        state = self._load_staged_build_locked(operation.repo_uuid)
        if (
            state is None
            or state.lifecycle_state in _STAGED_BUILD_TERMINAL_STATES
        ):
            return None
        expected = (
            state.repo_uuid,
            state.generation_id,
            state.request.expected_payload_bytes,
            state.request.capacity_policy_sha256,
            state.request.compatibility_sha256,
            state.request.expected_active_source_revision,
        )
        actual = (
            operation.repo_uuid,
            generation_id,
            expected_payload_bytes,
            capacity_policy.sha256,
            self.compatibility_sha256,
            operation.grant.active_source_revision,
        )
        if expected != actual:
            raise GenerationConflict(
                "allocation request differs from staged build authority"
            )
        return state

    def _staging_names(self, repo_uuid: str, generation_id: str) -> list[str]:
        relative = self._staging(repo_uuid, generation_id)
        with self.state.existing_private_directory(relative) as descriptor:
            return _directory_names(descriptor, deadline_ns=None)

    def _reuse_staged_completion_locked(
        self,
        state: StagedBuildState,
        allocation: GenerationAllocation,
    ) -> StagedBuildCompletion:
        if state.lifecycle_state != "COMPLETE":
            raise GenerationConflict("staged build is not complete")
        staging_relative = self._staging(state.repo_uuid, state.generation_id)
        final_relative = self._generation(state.repo_uuid, state.generation_id)
        staging_exists = self.state.private_directory_exists(staging_relative)
        final_exists = self.state.private_directory_exists(final_relative)
        if staging_exists == final_exists:
            raise GenerationConflict(
                "completed staged build must occupy exactly one generation location"
            )
        container = (
            allocation.staging_path
            if staging_exists
            else self.state.path(final_relative)
        )
        staged_receipt = (
            self.state.read_optional_existing_bytes(staging_relative / "receipt.json")
            if staging_exists
            else None
        )
        if staged_receipt is not None:
            try:
                receipt = cast(
                    GenerationReceipt,
                    GenerationReceipt.from_json(staged_receipt),
                )
            except Exception as exc:
                raise GenerationConflict(
                    f"durable staged receipt is invalid: {exc}"
                ) from exc
            receipt_value = receipt.to_dict()
            payload = cast(dict[str, Any], receipt_value["sealed_query_payload"])
            if (
                receipt_value["repo_uuid"] != state.repo_uuid
                or receipt_value["generation_id"] != state.generation_id
                or payload["manifest_sha256"] != state.payload_manifest_sha256
            ):
                raise GenerationConflict(
                    "durable staged receipt differs from completed staged authority"
                )
        inventory = self._inventory(
            container,
            allowed_root_entries=(
                frozenset({"graphify-out", "receipt.json"})
                if staged_receipt is not None or final_exists
                else frozenset({"graphify-out"})
            ),
        )
        if inventory.total_bytes > allocation.expected_payload_bytes:
            raise CapacityExceeded("staged payload exceeds its durable reservation")
        manifest = payload_manifest_sha256("graphify-out", inventory.entries)
        if manifest != state.payload_manifest_sha256:
            raise PayloadChanged("completed staged payload differs from durable manifest")
        return StagedBuildCompletion(
            state=state,
            allocation=allocation,
            entries=inventory.entries,
        )

    def prepare_staged_build(
        self,
        attempt: StagedBuildOperation,
        allocation: GenerationAllocation,
        *,
        monotonic_ns: int,
    ) -> StagedBuildPreparation:
        """Return a durably empty canonical staging directory for publication."""

        request = self._validated_structural_request(attempt.state.request)
        with self.leases.current_operation(
            attempt.grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD"}),
        ) as operation:
            state = self._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                state,
                repo_uuid=operation.repo_uuid,
                generation_id=attempt.state.generation_id,
                request=request,
            )
            self._require_allocation(operation, allocation)
            self._require_structural_allocation(state, allocation)
            lock = self._lock(operation.repo_uuid, state.generation_id)
            with self.state.existing_generation_lock(
                lock,
                generation_id=state.generation_id,
                exclusive=True,
            ):
                if state.lifecycle_state == "COMPLETE":
                    self._reuse_staged_completion_locked(state, allocation)
                    return StagedBuildPreparation(
                        state=state,
                        grant=attempt.grant,
                        allocation=allocation,
                        staging_path=allocation.staging_path,
                    )
                if state.lifecycle_state not in {"REQUESTED", "PUBLISHING"}:
                    raise GenerationConflict(
                        f"staged build cannot publish from {state.lifecycle_state}"
                    )
                attempt_tuple = (operation.grant.operation_epoch, operation.fence_token)
                state_tuple = (state.operation_epoch, state.fence_token)
                names = self._staging_names(operation.repo_uuid, state.generation_id)
                if state.lifecycle_state == "REQUESTED":
                    if names:
                        raise GenerationConflict(
                            "requested staged build has unowned nonempty staging bytes"
                        )
                elif state_tuple == attempt_tuple:
                    if names:
                        raise GenerationConflict(
                            "nonempty staging under the same fence cannot be reused"
                        )
                    return StagedBuildPreparation(
                        state=state,
                        grant=attempt.grant,
                        allocation=allocation,
                        staging_path=allocation.staging_path,
                    )
                else:
                    prior_epoch, prior_fence = state_tuple
                    if prior_epoch is None or prior_fence is None:
                        raise GenerationConflict(
                            "publishing staged build is missing durable fence authority"
                        )
                    if (
                        prior_epoch >= operation.grant.operation_epoch
                        or prior_fence >= operation.fence_token
                    ):
                        raise GenerationConflict(
                            "staged build publication belongs to a newer fence"
                        )
                    try:
                        self.state.remove_private_tree(
                            self._staging(operation.repo_uuid, state.generation_id),
                            allowed_directory_modes=_ALLOWED_DIRECTORY_MODES,
                            allowed_file_modes=_ALLOWED_FILE_MODES,
                        )
                        self.state.ensure_directory(
                            self._staging(operation.repo_uuid, state.generation_id)
                        )
                        self.state.fsync_directory(
                            self._staging(operation.repo_uuid, state.generation_id)
                        )
                    except (OSError, StatePathError) as exc:
                        raise GenerationConflict(
                            f"interrupted staging cannot be reset safely: {exc}"
                        ) from exc
                    self.fault_hook(
                        f"generation:{state.generation_id}:successor_staging_empty"
                    )
                publishing = self._staged_state(
                    revision=state.revision + 1,
                    repo_uuid=state.repo_uuid,
                    generation_id=state.generation_id,
                    request=state.request,
                    lifecycle_state="PUBLISHING",
                    operation_epoch=operation.grant.operation_epoch,
                    fence_token=operation.fence_token,
                )
                committed = self._commit_staged_build_locked(publishing)
                self.fault_hook(f"generation:{state.generation_id}:publishing_durable")
                return StagedBuildPreparation(
                    state=committed,
                    grant=attempt.grant,
                    allocation=allocation,
                    staging_path=allocation.staging_path,
                )

    def complete_staged_build(
        self,
        preparation: StagedBuildPreparation,
        *,
        source_observations: Sequence[SourceObservation],
        monotonic_ns: int,
    ) -> StagedBuildCompletion:
        """Seal complete staged bytes only after trusted source re-observation."""

        request = self._validated_structural_request(preparation.state.request)
        with self.leases.current_operation(
            preparation.grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD"}),
        ) as recovery_operation:
            recovery_state = self._load_staged_build_locked(recovery_operation.repo_uuid)
            if recovery_state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                recovery_state,
                repo_uuid=recovery_operation.repo_uuid,
                generation_id=preparation.state.generation_id,
                request=request,
            )
            self._require_allocation(recovery_operation, preparation.allocation)
            self._require_structural_allocation(recovery_state, preparation.allocation)
            if recovery_state.lifecycle_state == "COMPLETE":
                lock = self._lock(
                    recovery_operation.repo_uuid,
                    recovery_state.generation_id,
                )
                with self.state.existing_generation_lock(
                    lock,
                    generation_id=recovery_state.generation_id,
                    exclusive=True,
                ):
                    return self._reuse_staged_completion_locked(
                        recovery_state,
                        preparation.allocation,
                    )
            if recovery_state.lifecycle_state != "PUBLISHING":
                raise GenerationConflict(
                    "staged build cannot complete from "
                    f"{recovery_state.lifecycle_state}"
                )

        self._require_structural_evidence(
            preparation.state.repo_uuid,
            request,
            source_observations,
        )
        with self.leases.current_operation(
            preparation.grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD"}),
        ) as operation:
            state = self._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                state,
                repo_uuid=operation.repo_uuid,
                generation_id=preparation.state.generation_id,
                request=request,
            )
            self._require_allocation(operation, preparation.allocation)
            self._require_structural_allocation(state, preparation.allocation)
            lock = self._lock(operation.repo_uuid, state.generation_id)
            with self.state.existing_generation_lock(
                lock,
                generation_id=state.generation_id,
                exclusive=True,
            ):
                if state.lifecycle_state == "COMPLETE":
                    return self._reuse_staged_completion_locked(
                        state,
                        preparation.allocation,
                    )
                if state.lifecycle_state != "PUBLISHING":
                    raise GenerationConflict(
                        f"staged build cannot complete from {state.lifecycle_state}"
                    )
                if (state.operation_epoch, state.fence_token) != (
                    operation.grant.operation_epoch,
                    operation.fence_token,
                ):
                    raise GenerationConflict(
                        "staged build publication belongs to another fence"
                    )
                inventory = self._inventory(
                    preparation.allocation.staging_path,
                    allowed_root_entries=frozenset({"graphify-out"}),
                )
                if inventory.total_bytes > preparation.allocation.expected_payload_bytes:
                    raise CapacityExceeded(
                        "staged payload exceeds its durable reservation"
                    )
                self._sync_inventory(
                    operation.repo_uuid,
                    state.generation_id,
                    inventory,
                )
                self.fault_hook(f"generation:{state.generation_id}:before_completion_reinventory")
                reinventory = self._inventory(
                    preparation.allocation.staging_path,
                    allowed_root_entries=frozenset({"graphify-out"}),
                )
                if canonical_json_bytes(list(reinventory.entries)) != canonical_json_bytes(
                    list(inventory.entries)
                ):
                    raise PayloadChanged("payload changed during staged completion")
                manifest = payload_manifest_sha256("graphify-out", reinventory.entries)
                complete = self._staged_state(
                    revision=state.revision + 1,
                    repo_uuid=state.repo_uuid,
                    generation_id=state.generation_id,
                    request=state.request,
                    lifecycle_state="COMPLETE",
                    operation_epoch=operation.grant.operation_epoch,
                    fence_token=operation.fence_token,
                    payload_manifest_sha256=manifest,
                )
                committed = self._commit_staged_build_locked(complete)
                self.fault_hook(f"generation:{state.generation_id}:completion_durable")
                return StagedBuildCompletion(
                    state=committed,
                    allocation=preparation.allocation,
                    entries=reinventory.entries,
                )

    def complete_staged_promotion(
        self,
        attempt: StagedBuildOperation,
        pointer: PointerSet,
        *,
        monotonic_ns: int,
    ) -> StagedBuildState:
        """Record a separately-authoritative pointer move as terminal.

        This method never moves or repairs pointers. It only verifies the
        visible pointer plus its authoritative journal event, then releases the
        staged-build recovery barrier.
        """

        try:
            pointer = cast(PointerSet, PointerSet.from_mapping(pointer.to_dict()))
        except (AttributeError, ContractError, TypeError, ValueError) as exc:
            raise GenerationConflict(f"promoted pointer is invalid: {exc}") from exc
        request = self._validated_structural_request(attempt.state.request)
        operation_name = str(attempt.grant.lease.to_dict()["operation"])
        if operation_name not in {"PROMOTE", "POINTER_RECOVERY"}:
            raise GenerationConflict(
                "staged promotion completion requires PROMOTE or POINTER_RECOVERY"
            )
        with self.leases.current_operation(
            attempt.grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({operation_name}),
        ) as operation:
            state = self._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise GenerationConflict("staged build request is missing")
            self._require_staged_binding(
                state,
                repo_uuid=operation.repo_uuid,
                generation_id=attempt.state.generation_id,
                request=request,
            )
            if state.lifecycle_state not in {"CERTIFIED", "PROMOTED"}:
                raise GenerationConflict(
                    f"staged build cannot complete promotion from {state.lifecycle_state}"
                )
            workspace = self._workspace(operation.repo_uuid)
            pending = workspace / "pointers.pending.json"
            if self.state.private_file_exists(pending):
                raise GenerationConflict(
                    "pointer intent must be recovered before promotion can complete"
                )
            current_relative = workspace / "pointers.json"
            try:
                current = cast(
                    PointerSet,
                    PointerSet.from_json(
                        self.state.read_existing_bytes(
                            current_relative,
                            max_bytes=_MAX_POINTER_RECORD_BYTES,
                        )
                    ),
                )
            except Exception as exc:
                raise GenerationConflict(f"visible pointer is invalid: {exc}") from exc
            if current.canonical != pointer.canonical:
                raise GenerationConflict("visible pointer changed before completion")
            pointer_value = current.to_dict()
            current_ref = cast(dict[str, Any], pointer_value["current"])
            expected = (
                operation.repo_uuid,
                state.generation_id,
                state.receipt_sha256,
                request.expected_active_source_revision,
                request.source_epoch,
            )
            actual = (
                pointer_value["repo_uuid"],
                current_ref["generation_id"],
                current_ref["receipt_sha256"],
                pointer_value["active_source_revision"],
                pointer_value["source_epoch"],
            )
            if expected != actual:
                raise GenerationConflict(
                    "visible pointer does not bind the staged certified generation"
                )
            pointer_revision = int(pointer_value["pointer_revision"])
            if pointer_revision <= request.expected_pointer_revision:
                raise GenerationConflict(
                    "visible pointer did not advance the staged request CAS"
                )
            if (
                int(pointer_value["operation_epoch"]) > operation.grant.operation_epoch
                or int(pointer_value["fence_token"]) > operation.fence_token
            ):
                raise GenerationConflict("visible pointer belongs to a newer fence")
            lock = self._lock(operation.repo_uuid, state.generation_id)
            with self.state.existing_generation_lock(
                lock,
                generation_id=state.generation_id,
                exclusive=True,
            ):
                receipt = self.verify_generation(
                    operation.repo_uuid,
                    state.generation_id,
                    _expected_compatibility_sha256=state.request.compatibility_sha256,
                )
                if receipt.sha256 != state.receipt_sha256:
                    raise GenerationConflict(
                        "certified generation differs from staged build state"
                    )
                snapshot = self.journal.recover_locked(operation)
                journal_match = any(
                    event.to_dict()["transition"] in {"PROMOTED", "REPAIRED"}
                    and event.to_dict()["generation_id"] == state.generation_id
                    and event.to_dict()["receipt_sha256"] == state.receipt_sha256
                    and event.to_dict()["pointer_revision"] == pointer_revision
                    and event.to_dict()["operation_epoch"]
                    == pointer_value["operation_epoch"]
                    and event.to_dict()["fence_token"] == pointer_value["fence_token"]
                    for event in snapshot.events
                )
                if not journal_match:
                    raise GenerationConflict(
                        "visible pointer has no authoritative promotion journal event"
                    )
                if state.lifecycle_state == "PROMOTED":
                    if state.pointer_revision != pointer_revision:
                        raise GenerationConflict(
                            "terminal staged build records another pointer revision"
                        )
                    return state
                promoted = self._staged_state(
                    revision=state.revision + 1,
                    repo_uuid=state.repo_uuid,
                    generation_id=state.generation_id,
                    request=state.request,
                    lifecycle_state="PROMOTED",
                    operation_epoch=int(pointer_value["operation_epoch"]),
                    fence_token=int(pointer_value["fence_token"]),
                    payload_manifest_sha256=state.payload_manifest_sha256,
                    receipt_sha256=state.receipt_sha256,
                    pointer_revision=pointer_revision,
                )
                committed = self._commit_staged_build_locked(promoted)
                self.fault_hook(
                    f"generation:{state.generation_id}:staged_promoted_durable"
                )
                return committed

    def _scan_directory(
        self,
        descriptor: int,
        *,
        prefix: str,
        entries: list[dict[str, str | int]],
        directories: list[str],
        deadline_ns: int | None = None,
    ) -> None:
        _require_inventory_deadline(deadline_ns)
        before = os.fstat(descriptor)
        names = _directory_names(descriptor, deadline_ns=deadline_ns)
        if any(not name or "/" in name or name in {".", ".."} for name in names):
            raise GenerationError("payload contains a noncanonical directory entry")
        for name in names:
            _require_inventory_deadline(deadline_ns)
            relative = f"{prefix}/{name}"
            details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(details.st_mode):
                raise GenerationError(f"payload symbolic link is forbidden: {relative}")
            if stat.S_ISDIR(details.st_mode):
                if stat.S_IMODE(details.st_mode) not in _ALLOWED_DIRECTORY_MODES:
                    raise GenerationError(f"payload directory mode is not allowed: {relative}")
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                child = os.open(name, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if _identity(opened) != _identity(details):
                        raise PayloadChanged(f"payload directory changed while opened: {relative}")
                    directories.append(relative)
                    self._scan_directory(
                        child,
                        prefix=relative,
                        entries=entries,
                        directories=directories,
                        deadline_ns=deadline_ns,
                    )
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(details.st_mode):
                raise GenerationError(f"payload special file is forbidden: {relative}")
            if details.st_nlink != 1:
                raise GenerationError(f"payload hardlink is forbidden: {relative}")
            mode = stat.S_IMODE(details.st_mode)
            if mode not in _ALLOWED_FILE_MODES:
                raise GenerationError(f"payload file mode is not allowed: {relative}")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            file_descriptor = os.open(name, flags, dir_fd=descriptor)
            try:
                opened_before = os.fstat(file_descriptor)
                if _identity(opened_before) != _identity(details):
                    raise PayloadChanged(f"payload file changed while opened: {relative}")
                digest = hashlib.sha256()
                while True:
                    _require_inventory_deadline(deadline_ns)
                    try:
                        chunk = os.read(file_descriptor, 1024 * 1024)
                    except InterruptedError:
                        continue
                    _require_inventory_deadline(deadline_ns)
                    if not chunk:
                        break
                    digest.update(chunk)
                opened_after = os.fstat(file_descriptor)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    _identity(opened_before) != _identity(opened_after)
                    or _identity(opened_after) != _identity(current)
                ):
                    raise PayloadChanged(f"payload file changed while hashing: {relative}")
            finally:
                os.close(file_descriptor)
            entries.append(
                {
                    "path": relative,
                    "file_type": "regular_file",
                    "size": details.st_size,
                    "sha256": digest.hexdigest(),
                    "mode": f"{mode:04o}",
                }
            )
        _require_inventory_deadline(deadline_ns)
        after_names = _directory_names(descriptor, deadline_ns=deadline_ns)
        after = os.fstat(descriptor)
        if names != after_names or _identity(before) != _identity(after):
            raise PayloadChanged(f"payload directory changed during inventory: {prefix}")

    def _inventory(
        self,
        container: Path,
        *,
        allowed_root_entries: frozenset[str],
        deadline_ns: int | None = None,
    ) -> _PayloadInventory:
        _require_inventory_deadline(deadline_ns)
        try:
            relative = container.relative_to(self.state.root)
        except ValueError as exc:
            raise StatePathError("generation path escapes state root") from exc
        with self.state.existing_private_directory(relative) as root_descriptor:
            root_before = os.fstat(root_descriptor)
            root_names = _directory_names(root_descriptor, deadline_ns=deadline_ns)
            if set(root_names) != set(allowed_root_entries):
                raise GenerationError(
                    "generation root must contain exactly "
                    + ", ".join(sorted(allowed_root_entries))
                )
            payload_details = os.stat(
                "graphify-out",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(payload_details.st_mode):
                raise GenerationError("graphify-out must be a real directory")
            if stat.S_IMODE(payload_details.st_mode) not in _ALLOWED_DIRECTORY_MODES:
                raise GenerationError("payload directory mode is not allowed: graphify-out")
            payload_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            payload_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            payload_descriptor = os.open("graphify-out", payload_flags, dir_fd=root_descriptor)
            try:
                if _identity(os.fstat(payload_descriptor)) != _identity(payload_details):
                    raise PayloadChanged("graphify-out changed while opened")
                entries: list[dict[str, str | int]] = []
                directories = ["graphify-out"]
                self._scan_directory(
                    payload_descriptor,
                    prefix="graphify-out",
                    entries=entries,
                    directories=directories,
                    deadline_ns=deadline_ns,
                )
            finally:
                os.close(payload_descriptor)
            after_root_names = _directory_names(
                root_descriptor,
                deadline_ns=deadline_ns,
            )
            current_payload = os.stat(
                "graphify-out",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                root_names != after_root_names
                or _identity(root_before) != _identity(os.fstat(root_descriptor))
                or _identity(current_payload) != _identity(payload_details)
            ):
                raise PayloadChanged("generation root changed during inventory")
        _require_inventory_deadline(deadline_ns)
        entries.sort(key=lambda item: str(item["path"]))
        _require_inventory_deadline(deadline_ns)
        return _PayloadInventory(entries=tuple(entries), directories=tuple(directories))

    def inspect_staged_payload(
        self,
        allocation: GenerationAllocation,
    ) -> tuple[dict[str, str | int], ...]:
        expected = self.state.path(self._staging(allocation.repo_uuid, allocation.generation_id))
        if allocation.staging_path != expected:
            raise GenerationConflict("allocation staging path is not canonical")
        return self._inventory(
            expected,
            allowed_root_entries=frozenset({"graphify-out"}),
        ).entries

    def _require_allocation(
        self,
        operation: LeaseOperation,
        allocation: GenerationAllocation,
    ) -> None:
        canonical_staging = self.state.path(
            self._staging(operation.repo_uuid, allocation.generation_id)
        )
        if (allocation.repo_uuid, allocation.staging_path) != (
            operation.repo_uuid,
            canonical_staging,
        ):
            raise GenerationConflict("allocation path is not canonical for the current workspace")
        state = self._load_capacity_locked()
        reservation = (
            None
            if state is None
            else next(
                (
                    item
                    for item in state.reservations
                    if (item.repo_uuid, item.generation_id)
                    == (allocation.repo_uuid, allocation.generation_id)
                ),
                None,
            )
        )
        if reservation is None:
            final_relative = self._generation(
                operation.repo_uuid,
                allocation.generation_id,
            )
            if self.state.private_directory_exists(final_relative):
                return
            raise GenerationConflict("allocation has no durable capacity reservation")
        expected_fence = (
            operation.grant.active_source_revision,
            operation.grant.operation_epoch,
            operation.fence_token,
        )
        supplied_fence = (
            allocation.active_source_revision,
            allocation.operation_epoch,
            allocation.fence_token,
        )
        if supplied_fence != expected_fence:
            raise GenerationConflict("allocation is stale for the current fenced operation")
        durable = (
            reservation.reserved_bytes,
            reservation.policy_sha256,
            reservation.compatibility_sha256,
            reservation.active_source_revision,
            reservation.operation_epoch,
            reservation.fence_token,
        )
        supplied = (
            allocation.expected_payload_bytes,
            allocation.capacity_policy_sha256,
            allocation.compatibility_sha256,
            allocation.active_source_revision,
            allocation.operation_epoch,
            allocation.fence_token,
        )
        if supplied != durable:
            raise GenerationConflict("allocation differs from its durable capacity reservation")

    def _receipt(
        self,
        operation: LeaseOperation,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        entries: Sequence[Mapping[str, object]],
    ) -> GenerationReceipt:
        return cast(
            GenerationReceipt,
            GenerationReceipt.from_mapping(
                {
                    "contract": "graphify.workspace.generation_receipt",
                    "schema_version": 1,
                    "repo_uuid": operation.repo_uuid,
                    "generation_id": allocation.generation_id,
                    "lifecycle_state": "CERTIFIED",
                    "source_commit": request.source_commit,
                    "source_epoch": request.source_epoch,
                    "active_source_revision": operation.grant.active_source_revision,
                    "operation_epoch": operation.grant.operation_epoch,
                    "fence_token": operation.fence_token,
                    "policy_sha256": request.policy_sha256,
                    "observation_manifest_sha256": request.observation_manifest_sha256,
                    "queue_watermark": request.queue_watermark,
                    "semantic_completeness": request.semantic_completeness,
                    "compatibility_sha256": request.compatibility_sha256,
                    "coordination_lock_id": f"generation:{allocation.generation_id[4:]}",
                    "sealed_query_payload": {
                        "root": "graphify-out",
                        "manifest_sha256": payload_manifest_sha256("graphify-out", entries),
                        "entries": list(entries),
                    },
                    "validations": sorted(request.validations),
                }
            ),
        )

    def _prevalidate_certification_binding_inputs(
        self,
        operation: LeaseOperation,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        declared_entries: Sequence[Mapping[str, object]],
    ) -> None:
        # Constructing the frozen receipt validates every request and manifest
        # field before an immutable semantic binding can make the request
        # unrecoverable. The receipt itself is not installed at this boundary.
        self._receipt(operation, allocation, request, declared_entries)
        staging_relative = self._staging(operation.repo_uuid, allocation.generation_id)
        if self.state.read_optional_existing_bytes(staging_relative / "receipt.json") is not None:
            raise SemanticCertificationBlocked(
                "staged receipt has no durable semantic certification binding"
            )
        inventory = self._inventory(
            allocation.staging_path,
            allowed_root_entries=frozenset({"graphify-out"}),
        )
        if canonical_json_bytes(list(inventory.entries)) != canonical_json_bytes(
            list(declared_entries)
        ):
            raise PayloadChanged("declared payload manifest differs from staged payload")
        if inventory.total_bytes > allocation.expected_payload_bytes:
            raise CapacityExceeded("staged payload exceeds its durable reservation")

    def _sync_inventory(
        self,
        repo_uuid: str,
        generation_id: str,
        inventory: _PayloadInventory,
    ) -> None:
        staging = self._staging(repo_uuid, generation_id)
        for entry in inventory.entries:
            self.state.fsync_contained_regular_file(
                staging,
                str(entry["path"]),
                allowed_directory_modes=_ALLOWED_DIRECTORY_MODES,
                allowed_file_modes=_ALLOWED_FILE_MODES,
            )
            self.fault_hook(f"generation:{generation_id}:payload_file_durable:{entry['path']}")
        for directory in sorted(
            inventory.directories,
            key=lambda value: (-len(Path(value).parts), value),
        ):
            self.state.fsync_contained_directory(
                staging,
                directory,
                allowed_directory_modes=_ALLOWED_DIRECTORY_MODES,
            )
        self.state.fsync_directory(staging)
        self.fault_hook(f"generation:{generation_id}:payload_durable")

    def verify_generation(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        deadline_ns: int | None = None,
        _expected_compatibility_sha256: str | None = None,
    ) -> GenerationReceipt:
        _require_verification_deadline(deadline_ns)
        relative = self._generation(repo_uuid, generation_id)
        generation = self.state.path(relative)
        inventory = self._inventory(
            generation,
            allowed_root_entries=frozenset({"graphify-out", "receipt.json"}),
            deadline_ns=deadline_ns,
        )
        inventory_entries_bytes = canonical_json_bytes(list(inventory.entries))
        _require_verification_deadline(deadline_ns)
        try:
            receipt_bytes = self.state.read_existing_bytes(
                relative / "receipt.json",
                deadline_ns=deadline_ns,
            )
        except StateCorrupt as exc:
            raise GenerationError(f"generation receipt is invalid: {exc}") from exc
        try:
            receipt = cast(GenerationReceipt, GenerationReceipt.from_json(receipt_bytes))
        except Exception as exc:
            raise GenerationError(f"generation receipt is invalid: {exc}") from exc
        _require_verification_deadline(deadline_ns)
        value = receipt.to_dict()
        if value["repo_uuid"] != repo_uuid or value["generation_id"] != generation_id:
            raise GenerationError("generation receipt identity does not match its path")
        expected_compatibility_sha256 = (
            self.compatibility_sha256
            if _expected_compatibility_sha256 is None
            else _expected_compatibility_sha256
        )
        if value["compatibility_sha256"] != expected_compatibility_sha256:
            raise UnsupportedCompatibility(
                "generation receipt does not match the expected compatibility manifest"
            )
        payload = cast(dict[str, Any], value["sealed_query_payload"])
        declared = cast(list[dict[str, Any]], payload["entries"])
        if canonical_json_bytes(declared) != inventory_entries_bytes:
            raise PayloadChanged("certified generation payload does not match its receipt")
        if payload["manifest_sha256"] != payload_manifest_sha256("graphify-out", declared):
            raise GenerationError("generation receipt manifest digest does not match")
        _require_verification_deadline(deadline_ns)
        queue_watermark = int(value["queue_watermark"])
        validations = tuple(
            str(validation) for validation in cast(list[object], value["validations"])
        )
        if "stable_semantic_queue" in validations:
            request = CertificationRequest(
                source_commit=str(value["source_commit"]),
                source_epoch=int(value["source_epoch"]),
                policy_sha256=str(value["policy_sha256"]),
                observation_manifest_sha256=str(value["observation_manifest_sha256"]),
                queue_watermark=queue_watermark,
                semantic_completeness=str(value["semantic_completeness"]),
                compatibility_sha256=str(value["compatibility_sha256"]),
                validations=validations,
            )
            try:
                queue_view = SemanticQueueStore.verify_certification_binding_at(
                    self.state,
                    repo_uuid,
                    generation_id=generation_id,
                    request_sha256=self._semantic_request_sha256(request),
                    sealed_input_manifest_sha256=str(payload["manifest_sha256"]),
                    deadline_ns=deadline_ns,
                )
            except SemanticCertificationBlocked as exc:
                raise GenerationError(
                    f"semantic certification binding is invalid: {exc}"
                ) from exc
            if (
                queue_view.repo_uuid != repo_uuid
                or queue_view.source_commit != request.source_commit
                or queue_view.source_epoch != request.source_epoch
                or queue_view.policy_sha256 != request.policy_sha256
                or queue_view.observation_manifest_sha256
                != request.observation_manifest_sha256
                or queue_view.queue_watermark != request.queue_watermark
                or queue_view.semantic_completeness != request.semantic_completeness
            ):
                raise GenerationError(
                    "semantic certification binding differs from generation receipt"
                )
            _require_verification_deadline(deadline_ns)
        lock_relative = self._lock(repo_uuid, generation_id)
        try:
            lock_bytes = self.state.read_existing_bytes(
                lock_relative,
                max_bytes=_MAX_GENERATION_COORDINATION_LOCK_BYTES,
                deadline_ns=deadline_ns,
            )
            lock_document = cast(
                GenerationCoordinationLock,
                GenerationCoordinationLock.from_json(lock_bytes),
            )
        except LockTimeout:
            raise
        except Exception as exc:
            raise GenerationError(f"generation coordination lock is invalid: {exc}") from exc
        if lock_document.canonical != self._lock_document(generation_id).canonical:
            raise GenerationError("generation coordination lock identity does not match")
        _require_verification_deadline(deadline_ns)
        return receipt

    def _validate_recovery_receipt(
        self,
        operation: LeaseOperation,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        declared_entries: Sequence[Mapping[str, object]],
        receipt: GenerationReceipt,
        *,
        validating_events: Sequence[Any],
    ) -> None:
        expected = self._receipt(operation, allocation, request, declared_entries).to_dict()
        actual = receipt.to_dict()
        expected_without_fence = {
            key: value
            for key, value in expected.items()
            if key not in {"operation_epoch", "fence_token"}
        }
        actual_without_fence = {
            key: value
            for key, value in actual.items()
            if key not in {"operation_epoch", "fence_token"}
        }
        if actual_without_fence != expected_without_fence:
            raise GenerationConflict("durable receipt differs from the requested certification")
        receipt_epoch = int(actual["operation_epoch"])
        receipt_fence = int(actual["fence_token"])
        if (
            receipt_epoch > operation.grant.operation_epoch
            or receipt_fence > operation.fence_token
        ):
            raise GenerationConflict("durable receipt belongs to a newer fenced operation")
        if not any(
            int(event.to_dict()["operation_epoch"]) == receipt_epoch
            and int(event.to_dict()["fence_token"]) == receipt_fence
            for event in validating_events
        ):
            raise GenerationConflict("durable receipt has no matching VALIDATING event")

    def _certify_locked(
        self,
        operation: LeaseOperation,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        *,
        declared_entries: Sequence[Mapping[str, object]],
        occurred_at: datetime,
        expected_compatibility_sha256: str | None = None,
    ) -> GenerationReceipt:
        final_relative = self._generation(operation.repo_uuid, allocation.generation_id)
        snapshot = self.journal.recover_locked(operation)
        events = snapshot.for_generation(allocation.generation_id)
        latest = None if not events else str(events[-1].to_dict()["transition"])
        validating_events = tuple(
            event for event in events if event.to_dict()["transition"] == "VALIDATING"
        )
        if self.state.private_directory_exists(final_relative):
            receipt = self.verify_generation(
                operation.repo_uuid,
                allocation.generation_id,
                _expected_compatibility_sha256=expected_compatibility_sha256,
            )
            self._validate_recovery_receipt(
                operation,
                allocation,
                request,
                declared_entries,
                receipt,
                validating_events=validating_events,
            )
            certified = tuple(
                event for event in events if event.to_dict()["transition"] == "CERTIFIED"
            )
            if certified:
                if not any(
                    event.to_dict()["receipt_sha256"] == receipt.sha256
                    for event in certified
                ):
                    raise GenerationConflict(
                        "certified journal event does not bind the installed receipt"
                    )
                return receipt
            if latest != "VALIDATING":
                raise GenerationConflict(
                    f"installed generation cannot certify from lifecycle {latest}"
                )
            self.journal.append_generation_locked(
                operation,
                transition="CERTIFIED",
                generation_id=allocation.generation_id,
                receipt_sha256=receipt.sha256,
                pointer_revision=0,
                occurred_at=occurred_at,
            )
            return receipt

        if latest == "STAGING":
            self.journal.append_generation_locked(
                operation,
                transition="BUILT",
                generation_id=allocation.generation_id,
                receipt_sha256=None,
                pointer_revision=None,
                occurred_at=occurred_at,
            )
            latest = "BUILT"
        staging_relative = self._staging(operation.repo_uuid, allocation.generation_id)
        self.state.cleanup_atomic_temps(staging_relative)
        receipt_relative = staging_relative / "receipt.json"
        receipt_bytes = self.state.read_optional_existing_bytes(receipt_relative)
        receipt_present = receipt_bytes is not None
        latest_event = None if not events else events[-1]
        validating_requires_successor_recheck = (
            latest == "VALIDATING"
            and latest_event is not None
            and (
                int(latest_event.to_dict()["operation_epoch"])
                != operation.grant.operation_epoch
                or int(latest_event.to_dict()["fence_token"]) != operation.fence_token
            )
        )
        if latest == "BUILT" or (
            validating_requires_successor_recheck and not receipt_present
        ):
            self.journal.append_generation_locked(
                operation,
                transition="VALIDATING",
                generation_id=allocation.generation_id,
                receipt_sha256=None,
                pointer_revision=None,
                occurred_at=occurred_at,
            )
            latest = "VALIDATING"
            snapshot = self.journal.recover_locked(operation)
            events = snapshot.for_generation(allocation.generation_id)
            validating_events = tuple(
                event for event in events if event.to_dict()["transition"] == "VALIDATING"
            )
        if latest != "VALIDATING":
            raise GenerationConflict(f"generation cannot certify from lifecycle {latest}")
        inventory = self._inventory(
            allocation.staging_path,
            allowed_root_entries=(
                frozenset({"graphify-out", "receipt.json"})
                if receipt_present
                else frozenset({"graphify-out"})
            ),
        )
        if receipt_bytes is not None:
            try:
                receipt = cast(
                    GenerationReceipt,
                    GenerationReceipt.from_json(receipt_bytes),
                )
            except Exception as exc:
                raise GenerationConflict(f"durable staged receipt is invalid: {exc}") from exc
            self._validate_recovery_receipt(
                operation,
                allocation,
                request,
                declared_entries,
                receipt,
                validating_events=validating_events,
            )
        else:
            receipt = self._receipt(operation, allocation, request, declared_entries)
        if canonical_json_bytes(list(inventory.entries)) != canonical_json_bytes(
            list(declared_entries)
        ):
            raise PayloadChanged("declared payload manifest differs from staged payload")
        if inventory.total_bytes > allocation.expected_payload_bytes:
            raise CapacityExceeded("staged payload exceeds its durable reservation")
        self._sync_inventory(operation.repo_uuid, allocation.generation_id, inventory)
        self.fault_hook(f"generation:{allocation.generation_id}:before_reinventory")
        reinventory = self._inventory(
            allocation.staging_path,
            allowed_root_entries=(
                frozenset({"graphify-out", "receipt.json"})
                if receipt_present
                else frozenset({"graphify-out"})
            ),
        )
        if canonical_json_bytes(list(reinventory.entries)) != canonical_json_bytes(
            list(inventory.entries)
        ):
            raise PayloadChanged("payload changed during sealing")
        if not receipt_present:
            self.state.install_once_bytes(
                receipt_relative,
                receipt.canonical,
                label=f"generation:{allocation.generation_id}:receipt",
            )
        self.state.fsync_regular_file(receipt_relative)
        self.state.fsync_directory(receipt_relative.parent)
        self.fault_hook(f"generation:{allocation.generation_id}:receipt_durable")
        try:
            self.state.rename_contained(
                self._staging(operation.repo_uuid, allocation.generation_id),
                final_relative,
                label=f"generation:{allocation.generation_id}:install",
            )
        except StatePathError:
            if not self.state.private_directory_exists(final_relative):
                raise
        self.fault_hook(f"generation:{allocation.generation_id}:installed")
        verified = self.verify_generation(
            operation.repo_uuid,
            allocation.generation_id,
            _expected_compatibility_sha256=expected_compatibility_sha256,
        )
        if verified.canonical != receipt.canonical:
            raise GenerationError("installed receipt differs from sealed receipt")
        self.journal.append_generation_locked(
            operation,
            transition="CERTIFIED",
            generation_id=allocation.generation_id,
            receipt_sha256=receipt.sha256,
            pointer_revision=0,
            occurred_at=occurred_at,
        )
        return receipt

    @staticmethod
    def _semantic_request_sha256(request: CertificationRequest) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "source_commit": request.source_commit,
                    "source_epoch": request.source_epoch,
                    "policy_sha256": request.policy_sha256,
                    "observation_manifest_sha256": request.observation_manifest_sha256,
                    "queue_watermark": request.queue_watermark,
                    "semantic_completeness": request.semantic_completeness,
                    "compatibility_sha256": request.compatibility_sha256,
                    "validations": sorted(request.validations),
                }
            )
        ).hexdigest()

    def _trusted_source_observations(
        self,
        repo_uuid: str,
        expected: Sequence[SourceObservation],
    ) -> tuple[SourceObservation, SourceObservation]:
        try:
            source = self.leases.registry.resolve_active_source(repo_uuid)
            trusted = (
                self.adapter.observe(source.root),
                self.adapter.observe(source.root),
            )
            confirmed_source = self.leases.registry.resolve_active_source(repo_uuid)
        except (AdapterError, IdentityError, OSError, StateCorrupt, StatePathError) as exc:
            raise SemanticCertificationBlocked(
                f"trusted source observations are unavailable: {exc}"
            ) from exc
        if confirmed_source != source:
            raise SemanticCertificationBlocked(
                "trusted source identity changed during observation"
            )
        if trusted[0] != trusted[1]:
            raise SemanticCertificationBlocked(
                "trusted source observations are not stable"
            )
        if tuple(expected) != trusted:
            raise SemanticCertificationBlocked(
                "caller evidence differs from trusted source observations"
            )
        return trusted

    def _require_staged_certification_locked(
        self,
        operation: LeaseOperation,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        declared_entries: Sequence[Mapping[str, object]],
        completion: StagedBuildCompletion | None,
    ) -> StagedBuildState | None:
        state = self._load_staged_build_locked(operation.repo_uuid)
        if (
            state is None
            or state.lifecycle_state in _STAGED_BUILD_TERMINAL_STATES
        ):
            if completion is not None:
                raise GenerationConflict(
                    "staged completion was supplied without active staged-build authority"
                )
            return None
        if completion is None:
            raise GenerationConflict(
                "active staged build requires durable completion proof"
            )
        self._require_staged_binding(
            state,
            repo_uuid=operation.repo_uuid,
            generation_id=allocation.generation_id,
            request=completion.state.request,
        )
        if state.lifecycle_state != "COMPLETE":
            raise GenerationConflict(
                f"staged build cannot certify from {state.lifecycle_state}"
            )
        if completion.state.canonical != state.canonical:
            raise GenerationConflict("staged completion proof is stale")
        if completion.allocation != allocation:
            raise GenerationConflict("staged completion allocation differs")
        structural_request = state.request
        expected = (
            structural_request.source_commit,
            structural_request.source_epoch,
            structural_request.policy_sha256,
            structural_request.observation_manifest_sha256,
            structural_request.compatibility_sha256,
            structural_request.expected_payload_bytes,
            structural_request.capacity_policy_sha256,
            structural_request.expected_active_source_revision,
        )
        actual = (
            request.source_commit,
            request.source_epoch,
            request.policy_sha256,
            request.observation_manifest_sha256,
            request.compatibility_sha256,
            allocation.expected_payload_bytes,
            allocation.capacity_policy_sha256,
            allocation.active_source_revision,
        )
        if expected != actual:
            raise GenerationConflict(
                "certification request differs from staged build authority"
            )
        if canonical_json_bytes(list(completion.entries)) != canonical_json_bytes(
            list(declared_entries)
        ):
            raise PayloadChanged(
                "certification manifest differs from staged completion proof"
            )
        if (
            payload_manifest_sha256("graphify-out", declared_entries)
            != state.payload_manifest_sha256
        ):
            raise PayloadChanged(
                "certification manifest differs from durable staged completion"
            )
        return state

    def _mark_staged_certified_locked(
        self,
        state: StagedBuildState,
        receipt: GenerationReceipt,
    ) -> StagedBuildState:
        receipt_value = receipt.to_dict()
        certified = self._staged_state(
            revision=state.revision + 1,
            repo_uuid=state.repo_uuid,
            generation_id=state.generation_id,
            request=state.request,
            lifecycle_state="CERTIFIED",
            operation_epoch=int(receipt_value["operation_epoch"]),
            fence_token=int(receipt_value["fence_token"]),
            payload_manifest_sha256=state.payload_manifest_sha256,
            receipt_sha256=receipt.sha256,
        )
        committed = self._commit_staged_build_locked(certified)
        self.fault_hook(f"generation:{state.generation_id}:staged_certified_durable")
        return committed

    def certify(
        self,
        grant: LeaseGrant,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        *,
        source_observations: Sequence[SourceObservation],
        declared_entries: Sequence[Mapping[str, object]],
        staged_completion: StagedBuildCompletion | None = None,
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> GenerationReceipt:
        if (
            allocation.compatibility_sha256 != self.compatibility_sha256
            or request.compatibility_sha256 != self.compatibility_sha256
        ):
            raise UnsupportedCompatibility(
                "generation certification is not bound to the selected compatibility manifest"
            )
        if self.semantic_queue is None:
            raise SemanticCertificationBlocked(
                "generation certification requires durable semantic queue authority"
            )
        sealed_input_manifest_sha256 = payload_manifest_sha256(
            "graphify-out",
            declared_entries,
        )
        request_sha256 = self._semantic_request_sha256(request)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
            registry_required=True,
        ) as capacity_operation:
            self._require_allocation(capacity_operation, allocation)
            self._require_staged_certification_locked(
                capacity_operation,
                allocation,
                request,
                declared_entries,
                staged_completion,
            )
            self._receipt(
                capacity_operation,
                allocation,
                request,
                declared_entries,
            )
            if "stable_semantic_queue" not in request.validations:
                raise SemanticCertificationBlocked(
                    "certification request lacks stable semantic queue validation"
                )
            queue_view = self.semantic_queue.certification_binding_locked(
                capacity_operation,
                generation_id=allocation.generation_id,
                request_sha256=request_sha256,
                sealed_input_manifest_sha256=sealed_input_manifest_sha256,
            )

        binding_exists = queue_view is not None
        if queue_view is None:
            trusted_observations = self._trusted_source_observations(
                allocation.repo_uuid,
                source_observations,
            )
            queue_view = self.semantic_queue.certification_view(
                grant,
                source_epoch=request.source_epoch,
                source_observations=trusted_observations,
                sealed_input_manifest_sha256=sealed_input_manifest_sha256,
                monotonic_ns=monotonic_ns,
            )
            if queue_view is None:
                raise SemanticCertificationBlocked(
                    "certification requires a stable semantic queue view"
                )
            if (
                request.queue_watermark != queue_view.queue_watermark
                or request.semantic_completeness != queue_view.semantic_completeness
                or request.source_commit != queue_view.source_commit
                or request.policy_sha256 != queue_view.policy_sha256
                or request.observation_manifest_sha256 != queue_view.observation_manifest_sha256
            ):
                raise SemanticCertificationBlocked(
                    "certification request does not match stable queue evidence"
                )
            self.fault_hook(f"generation:{allocation.generation_id}:queue_view_captured")
        if (
            staged_completion is not None
            and queue_view.observation_evidence_sha256
            != staged_completion.state.request.observation_evidence_sha256
        ):
            raise SemanticCertificationBlocked(
                "semantic queue evidence differs from staged build authority"
            )
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
        ) as operation:
            staged_state = self._require_staged_certification_locked(
                operation,
                allocation,
                request,
                declared_entries,
                staged_completion,
            )
            if binding_exists:
                durable_view = self.semantic_queue.certification_binding_locked(
                    operation,
                    generation_id=allocation.generation_id,
                    request_sha256=request_sha256,
                    sealed_input_manifest_sha256=sealed_input_manifest_sha256,
                )
                if durable_view != queue_view:
                    raise SemanticCertificationBlocked(
                        "durable semantic certification binding changed"
                    )
            else:
                self._prevalidate_certification_binding_inputs(
                    operation,
                    allocation,
                    request,
                    declared_entries,
                )
                self.semantic_queue.assert_certification_view_locked(operation, queue_view)
                queue_view = self.semantic_queue.ensure_certification_binding_locked(
                    operation,
                    generation_id=allocation.generation_id,
                    request_sha256=request_sha256,
                    view=queue_view,
                )
            lock = self._lock(operation.repo_uuid, allocation.generation_id)
            with self.state.existing_generation_lock(
                lock,
                generation_id=allocation.generation_id,
                exclusive=True,
            ):
                receipt = self._certify_locked(
                    operation,
                    allocation,
                    request,
                    declared_entries=declared_entries,
                    occurred_at=occurred_at,
                )
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
            registry_required=True,
        ) as capacity_operation:
            self._clear_reservation_locked(
                capacity_operation.repo_uuid,
                allocation.generation_id,
            )
            if staged_state is not None:
                current_staged = self._require_staged_certification_locked(
                    capacity_operation,
                    allocation,
                    request,
                    declared_entries,
                    staged_completion,
                )
                if current_staged is None:
                    raise GenerationConflict(
                        "staged build authority disappeared during certification"
                    )
                lock = self._lock(
                    capacity_operation.repo_uuid,
                    allocation.generation_id,
                )
                with self.state.existing_generation_lock(
                    lock,
                    generation_id=allocation.generation_id,
                    exclusive=True,
                ):
                    verified = self.verify_generation(
                        capacity_operation.repo_uuid,
                        allocation.generation_id,
                    )
                    if verified.canonical != receipt.canonical:
                        raise GenerationConflict(
                            "certified generation changed before staged completion"
                        )
                    self._mark_staged_certified_locked(current_staged, receipt)
        return receipt


__all__ = [
    "CapacityExceeded",
    "CertificationRequest",
    "GenerationAllocation",
    "GenerationConflict",
    "GenerationError",
    "GenerationStore",
    "PayloadChanged",
    "StagedBuildCompletion",
    "StagedBuildOperation",
    "StagedBuildPreparation",
    "StagedBuildState",
    "StructuralBuildRequest",
]
