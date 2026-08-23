"""Immutable generation allocation, sealing, certification, and verification."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast

from graphify.workspace.adapters import (
    AdapterError,
    AdapterIntent,
    CompatibilityTuple,
    UnsupportedCompatibility,
    select_adapter,
)
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    SEMANTIC_RELEASE_DECISION_BINDING_MAX_BYTES,
    SEMANTIC_RELEASE_DECISION_BINDINGS_PER_GENERATION,
    SEMANTIC_RELEASE_DECISION_BINDINGS_PER_WORKSPACE,
    SEMANTIC_RELEASE_DECISION_STAGING_OVERHEAD_BYTES,
    CapacityPolicy,
    CapacityReservation,
    CapacityReservationState,
    CompatibilityManifest,
    ContractError,
    GenerationCoordinationLock,
    GenerationReceipt,
    PointerSet,
    Registry,
    StagedBuildAbandonmentEvidence,
    StagedBuildAbandonmentIntent,
    StagedBuildAuthorityCurrent,
    StagedBuildState,
    StructuralBuildRequest,
    WorkspaceLeaseState,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.identity import IdentityError
from graphify.workspace.journal import JournalCorrupt, JournalStore
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
    StateRecoveryRequired,
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
_GENERATION_ID_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$", re.ASCII)
_HANDOFF_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$", re.ASCII)
_DECISION_BINDING_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$", re.ASCII)
_DECISION_BINDING_MAX_BYTES = SEMANTIC_RELEASE_DECISION_BINDING_MAX_BYTES
_DECISION_BINDINGS_PER_GENERATION = SEMANTIC_RELEASE_DECISION_BINDINGS_PER_GENERATION
_DECISION_BINDINGS_PER_WORKSPACE = SEMANTIC_RELEASE_DECISION_BINDINGS_PER_WORKSPACE
_MAX_CAPACITY_BYTES = 9_223_372_036_854_775_807


def _bounded_capacity_sum(values: Sequence[int]) -> int:
    total = 0
    for value in values:
        if value < 0 or value > _MAX_CAPACITY_BYTES - total:
            raise CapacityExceeded("capacity usage overflows the supported range")
        total += value
    return total


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


class StagedBuildStillCurrent(GenerationConflict):
    """A stale-abandonment probe found the exact staged authority current."""


class StagedBuildReadRecoveryRequired(GenerationError):
    """Existing-only inspection found a pending staged commit."""


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
            raise GenerationConflict("completed staged build is missing a durable payload manifest")
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
    primary_bytes_by_generation: Mapping[tuple[str, str], int]
    handoff_bytes_by_generation: Mapping[tuple[str, str], int]
    decision_bytes_by_generation: Mapping[tuple[str, str], int]
    decision_bindings_by_generation: Mapping[tuple[str, str], int]
    decision_binding_members: Mapping[tuple[str, str, str], tuple[int, str]]
    reserved_bytes_by_generation: Mapping[tuple[str, str], int]
    unconsumed_reserved_bytes: int

    @property
    def bytes_by_generation(self) -> Mapping[tuple[str, str], int]:
        keys = (
            set(self.primary_bytes_by_generation)
            | set(self.handoff_bytes_by_generation)
            | set(self.decision_bytes_by_generation)
            | set(self.reserved_bytes_by_generation)
        )
        return {
            key: _bounded_capacity_sum(
                (
                    self.handoff_bytes_by_generation.get(key, 0),
                    self.decision_bytes_by_generation.get(key, 0),
                    max(
                        self.primary_bytes_by_generation.get(key, 0),
                        self.reserved_bytes_by_generation.get(key, 0),
                    ),
                )
            )
            for key in keys
        }

    @property
    def global_bytes(self) -> int:
        return _bounded_capacity_sum(tuple(self.bytes_by_generation.values()))

    @property
    def global_generations(self) -> int:
        return len(self.bytes_by_generation)

    def workspace_bytes(self, repo_uuid: str) -> int:
        return _bounded_capacity_sum(
            tuple(
                size
                for (candidate_uuid, _generation_id), size in self.bytes_by_generation.items()
                if candidate_uuid == repo_uuid
            )
        )

    def workspace_generations(self, repo_uuid: str) -> int:
        return sum(
            1
            for candidate_uuid, _generation_id in self.bytes_by_generation
            if candidate_uuid == repo_uuid
        )


def _capacity_rows(values: Mapping[tuple[str, str], int]) -> list[dict[str, object]]:
    return [
        {"repo_uuid": repo_uuid, "generation_id": generation_id, "value": value}
        for (repo_uuid, generation_id), value in sorted(values.items())
    ]


def _decision_binding_rows(
    values: Mapping[tuple[str, str, str], tuple[int, str]],
) -> list[dict[str, object]]:
    return [
        {
            "repo_uuid": repo_uuid,
            "generation_id": generation_id,
            "decision_request_sha256": request_sha256,
            "size": size,
            "binding_sha256": binding_sha256,
        }
        for (repo_uuid, generation_id, request_sha256), (
            size,
            binding_sha256,
        ) in sorted(values.items())
    ]


@dataclass(frozen=True)
class DecisionCapacityUsage:
    """Stable authoritative usage projection including private decision bytes."""

    repo_uuid: str
    primary_bytes_by_generation: Mapping[tuple[str, str], int]
    handoff_bytes_by_generation: Mapping[tuple[str, str], int]
    decision_bytes_by_generation: Mapping[tuple[str, str], int]
    decision_bindings_by_generation: Mapping[tuple[str, str], int]
    decision_binding_members: Mapping[tuple[str, str, str], tuple[int, str]]
    reserved_bytes_by_generation: Mapping[tuple[str, str], int]
    unconsumed_reserved_bytes: int

    def _state_value(
        self,
        *,
        remove_binding: tuple[str, str, str, int, str] | None = None,
    ) -> dict[str, object]:
        decision_bytes = dict(self.decision_bytes_by_generation)
        decision_counts = dict(self.decision_bindings_by_generation)
        decision_members = dict(self.decision_binding_members)
        if remove_binding is not None:
            repo_uuid, generation_id, request_sha256, size, binding_sha256 = (
                remove_binding
            )
            key = (repo_uuid, generation_id)
            member_key = (repo_uuid, generation_id, request_sha256)
            current_bytes = decision_bytes.get(key, 0)
            current_count = decision_counts.get(key, 0)
            if (
                size < 1
                or current_bytes < size
                or current_count < 1
                or decision_members.get(member_key) != (size, binding_sha256)
            ):
                raise CapacityExceeded("decision binding removal projection is invalid")
            decision_members.pop(member_key)
            remaining_bytes = current_bytes - size
            remaining_count = current_count - 1
            if remaining_count == 0:
                if remaining_bytes != 0:
                    raise CapacityExceeded("decision binding byte/count projection disagrees")
                decision_bytes.pop(key, None)
                decision_counts.pop(key, None)
            else:
                if remaining_bytes < remaining_count:
                    raise CapacityExceeded("decision binding byte projection is invalid")
                decision_bytes[key] = remaining_bytes
                decision_counts[key] = remaining_count
        return {
            "primary": _capacity_rows(self.primary_bytes_by_generation),
            "handoffs": _capacity_rows(self.handoff_bytes_by_generation),
            "decisions": _capacity_rows(decision_bytes),
            "decision_bindings": _capacity_rows(decision_counts),
            "decision_binding_members": _decision_binding_rows(decision_members),
            "reservations": _capacity_rows(self.reserved_bytes_by_generation),
            "unconsumed_reserved_bytes": self.unconsumed_reserved_bytes,
        }

    @property
    def state_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._state_value())).hexdigest()

    def state_sha256_without_binding(
        self,
        repo_uuid: str,
        generation_id: str,
        request_sha256: str,
        size: int,
        binding_sha256: str,
    ) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                self._state_value(
                    remove_binding=(
                        repo_uuid,
                        generation_id,
                        request_sha256,
                        size,
                        binding_sha256,
                    )
                )
            )
        ).hexdigest()

    @property
    def global_bytes(self) -> int:
        return self._usage.global_bytes

    @property
    def workspace_bytes(self) -> int:
        return self._usage.workspace_bytes(self.repo_uuid)

    @property
    def workspace_binding_count(self) -> int:
        return sum(
            count
            for (candidate_uuid, _generation_id), count in self.decision_bindings_by_generation.items()
            if candidate_uuid == self.repo_uuid
        )

    def generation_binding_count(self, generation_id: str) -> int:
        return self.decision_bindings_by_generation.get((self.repo_uuid, generation_id), 0)

    @property
    def _usage(self) -> _Usage:
        return _Usage(
            primary_bytes_by_generation=self.primary_bytes_by_generation,
            handoff_bytes_by_generation=self.handoff_bytes_by_generation,
            decision_bytes_by_generation=self.decision_bytes_by_generation,
            decision_bindings_by_generation=self.decision_bindings_by_generation,
            decision_binding_members=self.decision_binding_members,
            reserved_bytes_by_generation=self.reserved_bytes_by_generation,
            unconsumed_reserved_bytes=self.unconsumed_reserved_bytes,
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

    def _load_staged_build_locked(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> StagedBuildState | None:
        try:
            return self.leases._load_staged_build_locked(
                repo_uuid,
                deadline_ns=deadline_ns,
            )
        except LeaseRecoveryRequired as exc:
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc

    def read_only_staged_build_locked(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None,
    ) -> StagedBuildState | None:
        """Read existing staged-build authority without recovery or writes.

        The caller must already hold the read-only workspace lock.  This path
        deliberately rejects pending or divergent durable records rather than
        attempting the normal mutation-capable recovery path.
        """

        try:
            return self.leases._load_staged_build_locked(
                repo_uuid,
                recover=False,
                deadline_ns=deadline_ns,
            )
        except LeaseRecoveryRequired as exc:
            if isinstance(exc.__cause__, StateRecoveryRequired):
                raise StagedBuildReadRecoveryRequired(
                    "staged build has a pending durable commit"
                ) from exc
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc

    def _project_staged_build_recovery_locked(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> tuple[StagedBuildState | None, bool]:
        """Project one staged-state recovery without mutating durable state."""

        current, previous, pending = self._staged_build_paths(repo_uuid)
        try:
            projection = self.state.project_record_recovery(
                label=f"staged-build:{repo_uuid}",
                current=current,
                previous=previous,
                pending=pending,
                decoder=StagedBuildState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
                max_bytes=_MAX_STAGED_BUILD_STATE_BYTES,
                deadline_ns=deadline_ns,
            )
        except StateCorrupt as exc:
            raise GenerationError(f"staged build state is corrupt: {exc}") from exc
        return projection.record, projection.requires_recovery

    def recover_staged_build(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> StagedBuildState | None:
        """Recover one pending staged record under canonical lock ordering."""

        try:
            with self.leases.registry.recovered_snapshot(deadline_ns=deadline_ns):
                with self.leases.workspace_lock(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                ):
                    return self._load_staged_build_locked(
                        repo_uuid,
                        deadline_ns=deadline_ns,
                    )
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

    @staticmethod
    def structural_observation_document(observation: SourceObservation) -> dict[str, object]:
        """Return the sealed structural-observation document for public orchestration."""

        return GenerationStore._source_observation_document(observation)

    @classmethod
    def structural_observation_evidence_sha256(
        cls,
        source_observations: Sequence[SourceObservation],
    ) -> str:
        """Return the canonical digest shared with semantic observation evidence."""

        if len(source_observations) != 2:
            raise GenerationConflict("exactly two source observations are required")
        documents = tuple(
            cls._source_observation_document(observation) for observation in source_observations
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
            raise GenerationError(f"trusted source observations are unavailable: {exc}") from exc
        if confirmed_source != source:
            raise GenerationConflict("trusted source identity changed during observation")
        if trusted[0] != trusted[1]:
            raise GenerationConflict("trusted source observations are not stable")
        if tuple(expected) != trusted:
            raise GenerationConflict("caller evidence differs from trusted source observations")
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
            raise GenerationConflict("staged build request differs from trusted source evidence")
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
            raise GenerationConflict("durable staged abandonment requires exact recovery")

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
            raise GenerationConflict("pointer CAS differs from staged build request authority")

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
                if prior is not None and prior.lifecycle_state not in _STAGED_BUILD_TERMINAL_STATES:
                    raise GenerationConflict("another staged build request requires exact recovery")
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
                        raise GenerationConflict(f"generation already has a {label} directory")
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

    def _previous_certified_for_promoted_locked(
        self,
        state: StagedBuildState,
    ) -> StagedBuildState:
        current, previous, pending = self._staged_build_paths(state.repo_uuid)
        if self.state.private_file_exists(pending):
            raise GenerationConflict("promoted staged state has an unresolved pending commit")
        if not self.state.private_file_exists(current):
            raise GenerationConflict("promoted staged current record is missing")
        try:
            predecessor = StagedBuildState.from_json(
                self.state.read_existing_bytes(
                    previous,
                    max_bytes=_MAX_STAGED_BUILD_STATE_BYTES,
                )
            )
        except Exception as exc:
            raise GenerationConflict(f"promoted staged predecessor is invalid: {exc}") from exc
        if (
            predecessor.lifecycle_state != "CERTIFIED"
            or predecessor.revision + 1 != state.revision
            or predecessor.repo_uuid != state.repo_uuid
            or predecessor.generation_id != state.generation_id
            or predecessor.request.canonical != state.request.canonical
            or predecessor.payload_manifest_sha256 != state.payload_manifest_sha256
            or predecessor.receipt_sha256 != state.receipt_sha256
            or predecessor.operation_epoch is None
            or predecessor.fence_token is None
            or predecessor.pointer_revision is not None
            or predecessor.abandonment_intent is not None
            or predecessor.abandoned_from is not None
            or predecessor.abandon_reason is not None
            or predecessor.abandon_evidence is not None
            or predecessor.abandon_evidence_sha256 is not None
        ):
            raise GenerationConflict(
                "PROMOTED state does not advance the exact prior CERTIFIED record"
            )
        return predecessor

    def _promoted_handoff_evidence_locked(
        self,
        state: StagedBuildState,
    ) -> tuple[bytes, str, Mapping[str, object]]:
        from graphify.workspace.semantic_handoff import parse_semantic_result_handoff

        request = state.request
        handoff_name = f"{request.sha256}.json"
        handoff_parent = (
            self._workspace(state.repo_uuid) / "semantic-staging" / "handoffs" / state.generation_id
        )
        handoff_relative = handoff_parent / handoff_name
        try:
            with self.state.existing_private_directory(handoff_parent) as descriptor:
                with os.scandir(descriptor) as entries:
                    names = sorted(entry.name for entry in entries)
                if names != [handoff_name]:
                    raise GenerationConflict("retained promotion handoff directory is ambiguous")
                details = os.stat(
                    handoff_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise GenerationConflict("retained promotion handoff is not one regular file")
            retained = self.state.read_existing_bytes(
                handoff_relative,
                max_bytes=request.expected_payload_bytes,
            )
            handoff = parse_semantic_result_handoff(
                retained,
                max_bytes=request.expected_payload_bytes,
            )
        except GenerationConflict:
            raise
        except Exception as exc:
            raise GenerationConflict(
                f"promoted semantic handoff evidence is invalid: {exc}"
            ) from exc
        if (
            handoff.repo_uuid != state.repo_uuid
            or handoff.target_generation_id != state.generation_id
            or handoff.structural_request.canonical != request.canonical
        ):
            raise GenerationConflict("retained promotion handoff evidence differs")
        return handoff.canonical, handoff.sha256, handoff.queue

    def _require_promoted_certification_binding_locked(
        self,
        state: StagedBuildState,
        receipt: GenerationReceipt,
        handoff_queue: Mapping[str, object],
    ) -> None:
        if self.semantic_queue is None or state.payload_manifest_sha256 is None:
            raise GenerationConflict("promoted semantic certification authority is unavailable")
        receipt_value = receipt.to_dict()
        validations = tuple(str(item) for item in cast(list[object], receipt_value["validations"]))
        certification_request = CertificationRequest(
            source_commit=str(receipt_value["source_commit"]),
            source_epoch=int(receipt_value["source_epoch"]),
            policy_sha256=str(receipt_value["policy_sha256"]),
            observation_manifest_sha256=str(receipt_value["observation_manifest_sha256"]),
            queue_watermark=int(receipt_value["queue_watermark"]),
            semantic_completeness=str(receipt_value["semantic_completeness"]),
            compatibility_sha256=str(receipt_value["compatibility_sha256"]),
            validations=validations,
        )
        try:
            view = SemanticQueueStore.verify_certification_binding_at(
                self.state,
                state.repo_uuid,
                generation_id=state.generation_id,
                request_sha256=self._semantic_request_sha256(certification_request),
                sealed_input_manifest_sha256=state.payload_manifest_sha256,
            )
            reconciliation = cast(Mapping[str, object], handoff_queue["reconciliation"])
            observation_evidence = cast(
                Mapping[str, object],
                reconciliation["source_observations"],
            )
            observations = cast(
                list[Mapping[str, object]],
                observation_evidence["observations"],
            )
        except (KeyError, SemanticCertificationBlocked) as exc:
            raise GenerationConflict(
                f"promoted semantic certification binding is invalid: {exc}"
            ) from exc
        if len(observations) != 2 or observations[0] != observations[1]:
            raise GenerationConflict("retained semantic observations are not exact")
        observation = observations[0]
        if cast(Mapping[str, object], handoff_queue["queue_policy"]) != (
            self.semantic_queue.policy.to_dict()
        ):
            raise GenerationConflict("retained semantic queue policy is not current")
        expected_view = (
            state.repo_uuid,
            cast(int, handoff_queue["revision"]) + 1,
            cast(int, handoff_queue["desired_watermark"]),
            cast(int, handoff_queue["completed_watermark"]),
            cast(int, handoff_queue["compaction_epoch"]),
            cast(int, reconciliation["source_epoch"]),
            str(observation["source_commit"]),
            str(reconciliation["policy_sha256"]),
            str(observation["inventory_sha256"]),
            str(observation_evidence["evidence_sha256"]),
            state.payload_manifest_sha256,
            "complete",
        )
        actual_view = (
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
        structural_view = (
            view.repo_uuid,
            view.source_commit,
            view.source_epoch,
            view.policy_sha256,
            view.observation_manifest_sha256,
            view.observation_evidence_sha256,
            view.sealed_input_manifest_sha256,
        )
        expected_structural_view = (
            state.repo_uuid,
            state.request.source_commit,
            state.request.source_epoch,
            state.request.policy_sha256,
            state.request.observation_manifest_sha256,
            state.request.observation_evidence_sha256,
            state.payload_manifest_sha256,
        )
        if actual_view != expected_view or structural_view != expected_structural_view:
            raise GenerationConflict(
                "promoted semantic certification binding differs from retained authority"
            )

    def _require_promoted_terminal_cleanup_locked(
        self,
        document: Registry,
        lease_state: WorkspaceLeaseState,
        state: StagedBuildState,
        *,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        attempt_sha256: str,
    ) -> str:
        self._require_staged_binding(
            state,
            repo_uuid=repo_uuid,
            generation_id=generation_id,
            request=request,
        )
        operation = self.leases._require_promoted_staged_cleanup_attempt_locked(
            lease_state,
            state,
            repo_uuid=repo_uuid,
            generation_id=generation_id,
            request=request,
            expected_attempt_sha256=attempt_sha256,
        )
        predecessor = self._previous_certified_for_promoted_locked(state)
        if request.compatibility_sha256 != self.compatibility_sha256:
            raise GenerationConflict("promoted compatibility authority changed")

        registry_value = document.to_dict()
        entries = [
            item
            for item in cast(list[Mapping[str, object]], registry_value["workspaces"])
            if item["repo_uuid"] == repo_uuid
        ]
        if (
            len(entries) != 1
            or int(cast(int, registry_value["revision"])) < request.expected_registry_revision
            or int(cast(int, entries[0]["active_source_revision"]))
            != request.expected_active_source_revision
            or lease_state.migration_epoch != request.expected_migration_epoch
            or state.operation_epoch is None
            or state.fence_token is None
            or lease_state.operation_epoch < state.operation_epoch
            or lease_state.fence_high_watermark < state.fence_token
        ):
            raise GenerationConflict(
                "promoted repository authority differs from the staged request"
            )

        workspace = self._workspace(repo_uuid)
        if self.state.private_file_exists(workspace / "pointers.pending.json"):
            raise GenerationConflict("promoted cleanup requires durable pointer-intent absence")
        pointer = self._visible_pointer_locked(repo_uuid)
        if pointer is None:
            raise GenerationConflict("promoted visible pointer is missing")
        pointer_value = pointer.to_dict()
        current = cast(Mapping[str, object], pointer_value["current"])
        if (
            pointer_value["repo_uuid"] != repo_uuid
            or current["generation_id"] != generation_id
            or current["receipt_sha256"] != state.receipt_sha256
            or int(cast(int, pointer_value["pointer_revision"])) != state.pointer_revision
            or int(cast(int, pointer_value["pointer_revision"]))
            <= request.expected_pointer_revision
            or int(cast(int, pointer_value["active_source_revision"]))
            != request.expected_active_source_revision
            or int(cast(int, pointer_value["source_epoch"])) != request.source_epoch
            or int(cast(int, pointer_value["operation_epoch"])) != state.operation_epoch
            or int(cast(int, pointer_value["fence_token"])) != state.fence_token
        ):
            raise GenerationConflict("visible pointer differs from promoted staged authority")

        try:
            journal = self.journal.project_recovery(repo_uuid)
        except (JournalCorrupt, StateCorrupt) as exc:
            raise GenerationConflict(f"promoted lifecycle journal is invalid: {exc}") from exc
        if journal.actions:
            raise GenerationConflict("promoted lifecycle journal requires durable recovery")
        target_events = journal.snapshot.for_generation(generation_id)
        matching_events = tuple(
            event
            for event in target_events
            if event.to_dict()["transition"] in {"PROMOTED", "REPAIRED"}
            and event.to_dict()["receipt_sha256"] == state.receipt_sha256
            and event.to_dict()["pointer_revision"] == state.pointer_revision
            and event.to_dict()["operation_epoch"] == state.operation_epoch
            and event.to_dict()["fence_token"] == state.fence_token
        )
        if (
            len(matching_events) != 1
            or not target_events
            or target_events[-1].canonical != matching_events[0].canonical
        ):
            raise GenerationConflict(
                "promoted staged state has no exact authoritative journal event"
            )

        reservation, capacity_requires_recovery = self._project_capacity_reservation_locked(
            repo_uuid, generation_id
        )
        if reservation is not None or capacity_requires_recovery:
            raise GenerationConflict("promoted target reservation is not durably absent")

        lock = self._lock(repo_uuid, generation_id)
        with self.state.existing_generation_lock(
            lock,
            generation_id=generation_id,
            exclusive=True,
        ):
            receipt = self.verify_generation(
                repo_uuid,
                generation_id,
                _expected_compatibility_sha256=request.compatibility_sha256,
            )
            handoff_bytes, handoff_sha256, handoff_queue = self._promoted_handoff_evidence_locked(
                state
            )
            self._require_promoted_certification_binding_locked(
                state,
                receipt,
                handoff_queue,
            )
        receipt_value = receipt.to_dict()
        payload = cast(Mapping[str, object], receipt_value["sealed_query_payload"])
        payload_entries = cast(list[Mapping[str, object]], payload["entries"])
        semantic_entries = tuple(
            entry
            for entry in payload_entries
            if entry["path"] == "graphify-out/semantic-inputs.json"
        )
        if (
            receipt.sha256 != state.receipt_sha256
            or payload["manifest_sha256"] != state.payload_manifest_sha256
            or receipt_value["repo_uuid"] != repo_uuid
            or receipt_value["generation_id"] != generation_id
            or receipt_value["source_commit"] != request.source_commit
            or receipt_value["source_epoch"] != request.source_epoch
            or receipt_value["active_source_revision"] != request.expected_active_source_revision
            or receipt_value["policy_sha256"] != request.policy_sha256
            or receipt_value["observation_manifest_sha256"] != request.observation_manifest_sha256
            or receipt_value["compatibility_sha256"] != request.compatibility_sha256
            or receipt_value["operation_epoch"] != predecessor.operation_epoch
            or receipt_value["fence_token"] != predecessor.fence_token
            or receipt_value["semantic_completeness"] != "complete"
            or receipt_value["validations"]
            != [
                "coordination_lock_precreated",
                "payload_manifest",
                "stable_semantic_queue",
            ]
            or len(semantic_entries) != 1
            or semantic_entries[0]["file_type"] != "regular_file"
            or semantic_entries[0]["mode"] != "0600"
            or semantic_entries[0]["size"] != len(handoff_bytes)
            or semantic_entries[0]["sha256"] != handoff_sha256
        ):
            raise GenerationConflict("installed promoted receipt or semantic evidence changed")
        return operation

    def _promoted_terminal_cleanup_preflight(
        self,
        repo_uuid: str,
        generation_id: str,
        request: StructuralBuildRequest,
        *,
        attempt_sha256: str,
    ) -> StagedBuildState | None:
        try:
            with self.leases.registry.read_only_snapshot() as document:
                with self.leases.read_only_workspace_lock(repo_uuid):
                    try:
                        lease_state = self.leases.read_only_snapshot_locked(
                            document,
                            repo_uuid,
                        )
                    except StateRecoveryRequired as exc:
                        self.leases.read_uncertain_snapshot_locked(
                            document,
                            repo_uuid,
                        )
                        projected, _ = self._project_staged_build_recovery_locked(repo_uuid)
                        if projected is not None and projected.lifecycle_state in {
                            "PROMOTED",
                            "ABANDONED",
                        }:
                            raise GenerationConflict(
                                "terminal cleanup lease state is not durably stable"
                            ) from exc
                        return None
                    try:
                        state = self.read_only_staged_build_locked(
                            repo_uuid,
                            deadline_ns=None,
                        )
                    except StagedBuildReadRecoveryRequired as exc:
                        projected, _ = self._project_staged_build_recovery_locked(repo_uuid)
                        if projected is not None and projected.lifecycle_state in {
                            "PROMOTED",
                            "ABANDONED",
                        }:
                            raise GenerationConflict(
                                "terminal staged state is not durably stable"
                            ) from exc
                        return None
                    if state is None or state.lifecycle_state not in {
                        "PROMOTED",
                        "ABANDONED",
                    }:
                        return None
                    self._require_staged_binding(
                        state,
                        repo_uuid=repo_uuid,
                        generation_id=generation_id,
                        request=request,
                        allow_abandonment_intent=True,
                    )
                    if state.lifecycle_state == "ABANDONED":
                        raise GenerationConflict("staged build is already ABANDONED")
                    self._require_promoted_terminal_cleanup_locked(
                        document,
                        lease_state,
                        state,
                        repo_uuid=repo_uuid,
                        generation_id=generation_id,
                        request=request,
                        attempt_sha256=attempt_sha256,
                    )
                    return state
        except StateRecoveryRequired:
            return None
        except StateCorrupt as exc:
            raise GenerationError(f"promoted cleanup lease state is corrupt: {exc}") from exc

    def _acquire_promoted_terminal_cleanup(
        self,
        state: StagedBuildState,
        request: StructuralBuildRequest,
        *,
        attempt_sha256: str,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> StagedBuildOperation:
        grant: LeaseGrant | None = None
        try:
            grant = self.leases.acquire_promoted_staged_cleanup(
                state.repo_uuid,
                state.generation_id,
                request,
                attempt_sha256=attempt_sha256,
                acquired_at=acquired_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=ttl_ns,
            )
            with self.leases.current_promoted_staged_cleanup(
                grant,
                state.generation_id,
                request,
                attempt_sha256=attempt_sha256,
                monotonic_ns=monotonic_ns,
            ) as operation:
                current = self.read_only_staged_build_locked(
                    state.repo_uuid,
                    deadline_ns=None,
                )
                if current is None:
                    raise GenerationConflict(
                        "promoted staged state disappeared after cleanup acquisition"
                    )
                retained_operation = self._require_promoted_terminal_cleanup_locked(
                    operation.registry,
                    operation.state,
                    current,
                    repo_uuid=state.repo_uuid,
                    generation_id=state.generation_id,
                    request=request,
                    attempt_sha256=attempt_sha256,
                )
                if retained_operation != grant.lease.to_dict()["operation"]:
                    raise GenerationConflict("promoted cleanup operation changed after acquisition")
                return StagedBuildOperation(state=current, grant=grant)
        except BaseException as exc:
            if grant is not None:
                try:
                    released = self.leases.release(grant)
                    if (
                        released.leases.get("workspace") is not None
                        or released.staged_attempt_sha256 is not None
                    ):
                        raise GenerationConflict(
                            "promoted cleanup grant release returned ambiguous authority"
                        )
                except BaseException as release_error:
                    raise release_error from exc
            raise

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
        promoted = self._promoted_terminal_cleanup_preflight(
            repo_uuid,
            generation_id,
            request,
            attempt_sha256=attempt_sha256,
        )
        if promoted is not None:
            return self._acquire_promoted_terminal_cleanup(
                promoted,
                request,
                attempt_sha256=attempt_sha256,
                acquired_at=acquired_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=ttl_ns,
            )
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
                        raise GenerationConflict(f"staged build is already {state.lifecycle_state}")
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
            "observation_evidence_sha256": cls.structural_observation_evidence_sha256(trusted),
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
        *,
        capacity_failure_payload_bytes: int | None = None,
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
            queue_snapshot = self.semantic_queue.read_only_snapshot_locked(operation.repo_uuid)
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
        evidence_value: dict[str, object] = {
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
        if capacity_failure_payload_bytes is not None:
            evidence_value["capacity_failure"] = {
                "payload_bytes": capacity_failure_payload_bytes,
            }
        evidence = StagedBuildAbandonmentEvidence.from_mapping(evidence_value)
        try:
            reason = evidence.reason_for(request)
        except StagedBuildAuthorityCurrent:
            return None
        except ContractError as exc:
            raise GenerationConflict(f"staged abandonment evidence is invalid: {exc}") from exc
        return reason, evidence, pointer

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
            raise GenerationConflict(f"stale staging cannot be removed safely: {exc}") from exc
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
            cast(dict[str, str | int], item) for item in cast(list[object], payload["entries"])
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
            raise GenerationConflict("durable receipt lacks stable semantic queue validation")
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
            raise GenerationConflict("durable receipt has no semantic certification binding")
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
                    raise GenerationConflict(f"durable staged receipt is invalid: {exc}") from exc
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
                raise GenerationConflict("recovered certification changed before staged completion")
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
                raise GenerationConflict("staged certification recovery requires COMPLETE state")
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
            raise GenerationConflict("pointer intent must be recovered before staged abandonment")
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
        staged_receipt = (
            self._staging(
                operation.repo_uuid,
                state.generation_id,
            )
            / "receipt.json"
        )
        if state.lifecycle_state == "COMPLETE" and (
            self.state.private_directory_exists(final)
            or self.state.private_file_exists(staged_receipt)
        ):
            raise GenerationConflict("certification recovery is required before staged abandonment")

    def _finish_staged_abandonment_locked(
        self,
        operation: LeaseOperation,
        state: StagedBuildState,
        intent: StagedBuildAbandonmentIntent,
        *,
        occurred_at: datetime,
    ) -> StagedBuildState:
        if state.abandonment_intent != intent:
            raise GenerationConflict("durable staged abandonment intent changed before recovery")
        if (
            intent.operation_epoch > operation.grant.operation_epoch
            or intent.fence_token > operation.fence_token
        ):
            raise GenerationConflict("durable staged abandonment intent belongs to a newer fence")
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
            if attempt.state.repo_uuid != operation.repo_uuid:
                raise GenerationConflict("staged recovery attempt repo_uuid mismatch")
            repo_uuid = operation.repo_uuid
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

        observation_error: GenerationError | None = None
        try:
            trusted = self._trusted_structural_observations(
                repo_uuid,
                source_observations,
            )
            source_document = self._abandonment_source_document(trusted)
        except GenerationError as exc:
            observation_error = exc
            source_document = self._frozen_abandonment_source_document(request)
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
                proof = self._staged_abandonment_proof_if_stale_locked(
                    operation,
                    state,
                    source_document,
                )
                if proof is None:
                    if observation_error is not None:
                        raise observation_error
                    raise StagedBuildStillCurrent(
                        "staged build authority is still current and cannot be abandoned"
                    )
                reason, evidence, _pointer = proof
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

    def _project_capacity_reservation_locked(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        deadline_ns: int | None = None,
    ) -> tuple[CapacityReservation | None, bool]:
        """Project the exact target reservation without repairing capacity state."""

        try:
            projection = self.state.project_record_recovery(
                label="capacity",
                current=_CAPACITY_CURRENT,
                previous=_CAPACITY_PREVIOUS,
                pending=_CAPACITY_PENDING,
                decoder=CapacityReservationState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
                deadline_ns=deadline_ns,
            )
        except StateCorrupt as exc:
            raise CapacityExceeded(f"capacity reservation state is corrupt: {exc}") from exc
        state = projection.record
        matches = (
            ()
            if state is None
            else tuple(
                item
                for item in state.reservations
                if (item.repo_uuid, item.generation_id) == (repo_uuid, generation_id)
            )
        )
        if len(matches) > 1:  # pragma: no cover - canonical state rejects duplicates
            raise CapacityExceeded("capacity reservation state contains duplicate targets")
        return (None if not matches else matches[0]), projection.requires_recovery

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

    def _handoff_directory_bytes(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        deadline_ns: int | None = None,
    ) -> int:
        _require_inventory_deadline(deadline_ns)
        relative = self._workspace(repo_uuid) / "semantic-staging" / "handoffs" / generation_id
        path = self.state.path(relative)
        with self.state._existing_private_directory(
            relative,
            allow_missing=True,
        ) as descriptor:
            if descriptor is None:
                raise FileNotFoundError(path)
            before = os.fstat(descriptor)
            names = self.state._tree_entry_names_descriptor(
                descriptor,
                path,
                deadline_ns=deadline_ns,
            )
            total = 0
            for name in names:
                _require_inventory_deadline(deadline_ns)
                if _HANDOFF_NAME_RE.fullmatch(name) is None:
                    raise StatePathError("retained semantic handoff name is noncanonical")
                candidate = path / name
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                self.state._require_regular_details(
                    details,
                    candidate,
                    allowed_modes=frozenset({0o600}),
                )
                try:
                    file_descriptor = os.open(
                        name,
                        self.state._regular_open_flags(),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise StatePathError(
                        "retained semantic handoff cannot be opened safely"
                    ) from exc
                try:
                    opened = self.state._require_regular_descriptor(
                        file_descriptor,
                        candidate,
                        allowed_modes=frozenset({0o600}),
                    )
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    identity = self.state._stat_identity(details)
                    if (
                        self.state._stat_identity(opened) != identity
                        or self.state._stat_identity(current) != identity
                    ):
                        raise StatePathError("retained semantic handoff changed while scanning")
                    total += opened.st_size
                    if total > _MAX_CAPACITY_BYTES:
                        raise StatePathError("retained semantic handoff usage overflows")
                finally:
                    os.close(file_descriptor)
                _require_inventory_deadline(deadline_ns)
            after_names = self.state._tree_entry_names_descriptor(
                descriptor,
                path,
                deadline_ns=deadline_ns,
            )
            _require_inventory_deadline(deadline_ns)
            after = os.fstat(descriptor)
            if names != after_names or self.state._stat_identity(
                before
            ) != self.state._stat_identity(after):
                raise StatePathError("retained semantic handoff usage changed while scanning")
        return total

    def _decision_generation_ids(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> tuple[str, ...]:
        _require_inventory_deadline(deadline_ns)
        decision_root = self._workspace(repo_uuid) / "semantic-release-decisions"
        path = self.state.path(decision_root)
        with self.state._existing_private_directory(
            decision_root,
            allow_missing=True,
        ) as descriptor:
            if descriptor is None:
                return ()
            before = os.fstat(descriptor)
            names = self.state._tree_entry_names_descriptor(
                descriptor,
                path,
                deadline_ns=deadline_ns,
                maximum_entries=_DECISION_BINDINGS_PER_WORKSPACE,
            )
            _require_inventory_deadline(deadline_ns)
            after = os.fstat(descriptor)
            if self.state._stat_identity(before) != self.state._stat_identity(after):
                raise StatePathError(
                    "decision generation namespace changed while scanning"
                )
            if not names:
                raise StatePathError("decision generation namespace is present but empty")
            self.state._require_held_private_directory_binding(
                decision_root,
                descriptor,
                path,
            )
            return tuple(names)

    def _decision_directory_usage(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        maximum_bindings: int = _DECISION_BINDINGS_PER_GENERATION,
        deadline_ns: int | None = None,
    ) -> tuple[int, int, dict[str, tuple[int, str]]]:
        _require_inventory_deadline(deadline_ns)
        if not 0 <= maximum_bindings <= _DECISION_BINDINGS_PER_GENERATION:
            raise ValueError("maximum_bindings is outside the decision-store bound")
        relative = (
            self._workspace(repo_uuid)
            / "semantic-release-decisions"
            / generation_id
        )
        path = self.state.path(relative)
        with self.state._existing_private_directory(relative) as descriptor:
            if descriptor is None:  # pragma: no cover - allow_missing is false
                raise StatePathError("decision binding directory is missing")
            before = os.fstat(descriptor)
            names = self.state._tree_entry_names_descriptor(
                descriptor,
                path,
                deadline_ns=deadline_ns,
                maximum_entries=maximum_bindings,
            )
            if not names:
                raise StatePathError("decision binding directory is empty")
            total = 0
            members: dict[str, tuple[int, str]] = {}
            for name in names:
                _require_inventory_deadline(deadline_ns)
                if _DECISION_BINDING_NAME_RE.fullmatch(name) is None:
                    raise StatePathError("decision binding name is noncanonical")
                candidate = path / name
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                self.state._require_regular_details(
                    details,
                    candidate,
                    allowed_modes=frozenset({0o600}),
                )
                if details.st_size < 1 or details.st_size > _DECISION_BINDING_MAX_BYTES:
                    raise StatePathError("decision binding exceeds its byte bound")
                try:
                    file_descriptor = os.open(
                        name,
                        self.state._regular_open_flags(),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise StatePathError(
                        "decision binding cannot be opened safely"
                    ) from exc
                try:
                    opened = self.state._require_regular_descriptor(
                        file_descriptor,
                        candidate,
                        allowed_modes=frozenset({0o600}),
                    )
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    identity = self.state._stat_identity(details)
                    if (
                        self.state._stat_identity(opened) != identity
                        or self.state._stat_identity(current) != identity
                    ):
                        raise StatePathError("decision binding changed while scanning")
                    chunks: list[bytes] = []
                    remaining = opened.st_size
                    while remaining:
                        _require_inventory_deadline(deadline_ns)
                        try:
                            chunk = os.read(file_descriptor, min(remaining, 1024 * 1024))
                        except InterruptedError:
                            continue
                        _require_inventory_deadline(deadline_ns)
                        if not chunk:
                            raise StatePathError("decision binding was truncated while reading")
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    payload = b"".join(chunks)
                    from graphify.workspace.semantic_release_decision import (
                        SemanticReleaseDecisionBinding,
                        SemanticReleaseDecisionInvalid,
                    )

                    try:
                        binding = SemanticReleaseDecisionBinding.from_json(payload)
                    except SemanticReleaseDecisionInvalid as exc:
                        raise StatePathError("decision binding is not canonical") from exc
                    reopened = os.fstat(file_descriptor)
                    rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        self.state._stat_identity(reopened) != identity
                        or self.state._stat_identity(rebound) != identity
                    ):
                        raise StatePathError("decision binding changed while scanning")
                    if (
                        binding.repo_uuid != repo_uuid
                        or binding.target_generation_id != generation_id
                        or f"{binding.decision_request_sha256}.json" != name
                    ):
                        raise StatePathError(
                            "decision binding identity differs from its path"
                        )
                    total = _bounded_capacity_sum((total, opened.st_size))
                    members[binding.decision_request_sha256] = (
                        opened.st_size,
                        binding.binding_sha256,
                    )
                finally:
                    os.close(file_descriptor)
            after_names = self.state._tree_entry_names_descriptor(
                descriptor,
                path,
                deadline_ns=deadline_ns,
                maximum_entries=maximum_bindings,
            )
            _require_inventory_deadline(deadline_ns)
            after = os.fstat(descriptor)
            if names != after_names or self.state._stat_identity(
                before
            ) != self.state._stat_identity(after):
                raise StatePathError("decision binding usage changed while scanning")
            self.state._require_held_private_directory_binding(
                relative,
                descriptor,
                path,
            )
            return total, len(names), members

    def _scan_usage_once(
        self,
        *,
        deadline_ns: int | None = None,
    ) -> tuple[
        dict[tuple[str, str], int],
        dict[tuple[str, str], int],
        dict[tuple[str, str], int],
        dict[tuple[str, str], int],
        dict[tuple[str, str, str], tuple[int, str]],
    ]:
        primary_usage: dict[tuple[str, str], int] = {}
        handoff_usage: dict[tuple[str, str], int] = {}
        decision_usage: dict[tuple[str, str], int] = {}
        decision_bindings: dict[tuple[str, str], int] = {}
        decision_binding_members: dict[tuple[str, str, str], tuple[int, str]] = {}
        active_generations: set[tuple[str, str]] = set()
        try:
            _require_inventory_deadline(deadline_ns)
            workspaces = Path("workspaces")
            for repo_uuid in self.state.list_existing_private_directories(
                workspaces,
                allow_missing=True,
                deadline_ns=deadline_ns,
            ):
                _require_inventory_deadline(deadline_ns)
                workspace = workspaces / repo_uuid
                containers: list[tuple[Path, bool, bool]] = [
                    (workspace / "generations", False, True),
                    (workspace / "staging", False, False),
                ]
                quarantine_root = workspace / "quarantine"
                quarantine_kinds = self.state.list_existing_private_directories(
                    quarantine_root,
                    allow_missing=True,
                    deadline_ns=deadline_ns,
                )
                containers.extend(
                    (quarantine_root / quarantine_kind, True, False)
                    for quarantine_kind in ("gc", "corrupt")
                    if quarantine_kind in quarantine_kinds
                )
                for container, strips_epoch, is_active in containers:
                    for name in self.state.list_existing_private_directories(
                        container,
                        allow_missing=True,
                        deadline_ns=deadline_ns,
                    ):
                        _require_inventory_deadline(deadline_ns)
                        generation_id = name.rsplit(".", 1)[0] if strips_epoch else name
                        key = (repo_uuid, generation_id)
                        if key in primary_usage:
                            raise _CapacityScanChanged(
                                "generation occupies multiple active/staging/quarantine locations: "
                                f"{repo_uuid}/{generation_id}"
                            )
                        primary_usage[key] = self.state.tree_bytes(
                            container / name,
                            allowed_directory_modes=_ALLOWED_DIRECTORY_MODES,
                            allowed_file_modes=_ALLOWED_FILE_MODES,
                            deadline_ns=deadline_ns,
                        )
                        if is_active:
                            active_generations.add(key)
                handoff_root = workspace / "semantic-staging" / "handoffs"
                for generation_id in self.state.list_existing_private_directories(
                    handoff_root,
                    allow_missing=True,
                    deadline_ns=deadline_ns,
                ):
                    _require_inventory_deadline(deadline_ns)
                    if _GENERATION_ID_RE.fullmatch(generation_id) is None:
                        raise StatePathError("retained semantic handoff target is noncanonical")
                    size = self._handoff_directory_bytes(
                        repo_uuid,
                        generation_id,
                        deadline_ns=deadline_ns,
                    )
                    _require_inventory_deadline(deadline_ns)
                    if size:
                        handoff_usage[(repo_uuid, generation_id)] = size
                total_bindings = 0
                for generation_id in self._decision_generation_ids(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                ):
                    _require_inventory_deadline(deadline_ns)
                    if _GENERATION_ID_RE.fullmatch(generation_id) is None:
                        raise StatePathError("decision target generation is noncanonical")
                    key = (repo_uuid, generation_id)
                    if key not in active_generations:
                        raise StatePathError(
                            "decision state does not name one retained active generation"
                        )
                    size, count, members = self._decision_directory_usage(
                        repo_uuid,
                        generation_id,
                        maximum_bindings=min(
                            _DECISION_BINDINGS_PER_GENERATION,
                            _DECISION_BINDINGS_PER_WORKSPACE - total_bindings,
                        ),
                        deadline_ns=deadline_ns,
                    )
                    total_bindings += count
                    if total_bindings > _DECISION_BINDINGS_PER_WORKSPACE:
                        raise StatePathError(
                            "decision workspace exceeds 4096 bindings"
                        )
                    decision_usage[key] = size
                    decision_bindings[key] = count
                    decision_binding_members.update(
                        {
                            (repo_uuid, generation_id, request_sha256): member
                            for request_sha256, member in members.items()
                        }
                    )
            _require_inventory_deadline(deadline_ns)
        except StatePathError as exc:
            raise CapacityExceeded(f"unsafe state path in capacity scan: {exc}") from exc
        return (
            primary_usage,
            handoff_usage,
            decision_usage,
            decision_bindings,
            decision_binding_members,
        )

    def _usage(
        self,
        reservations: Sequence[CapacityReservation],
        *,
        deadline_ns: int | None = None,
    ) -> _Usage:
        previous: (
            tuple[
                dict[tuple[str, str], int],
                dict[tuple[str, str], int],
                dict[tuple[str, str], int],
                dict[tuple[str, str], int],
                dict[tuple[str, str, str], tuple[int, str]],
            ]
            | None
        ) = None
        repeated_change: str | None = None
        observed_usage: (
            tuple[
                dict[tuple[str, str], int],
                dict[tuple[str, str], int],
                dict[tuple[str, str], int],
                dict[tuple[str, str], int],
                dict[tuple[str, str, str], tuple[int, str]],
            ]
            | None
        ) = None
        for _attempt in range(5):
            _require_inventory_deadline(deadline_ns)
            try:
                observed = self._scan_usage_once(deadline_ns=deadline_ns)
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
                observed_usage = observed
                break
            previous = observed
        _require_inventory_deadline(deadline_ns)
        if observed_usage is None:
            raise CapacityExceeded("capacity filesystem snapshot did not stabilize")
        (
            primary_usage,
            handoff_usage,
            decision_usage,
            decision_bindings,
            decision_binding_members,
        ) = observed_usage
        reserved_usage = {
            (reservation.repo_uuid, reservation.generation_id): reservation.reserved_bytes
            for reservation in reservations
        }
        unconsumed_reserved_bytes = _bounded_capacity_sum(
            tuple(
                max(
                    reservation.reserved_bytes
                    - primary_usage.get((reservation.repo_uuid, reservation.generation_id), 0),
                    0,
                )
                for reservation in reservations
            )
        )
        return _Usage(
            primary_bytes_by_generation=primary_usage,
            handoff_bytes_by_generation=handoff_usage,
            decision_bytes_by_generation=decision_usage,
            decision_bindings_by_generation=decision_bindings,
            decision_binding_members=decision_binding_members,
            reserved_bytes_by_generation=reserved_usage,
            unconsumed_reserved_bytes=unconsumed_reserved_bytes,
        )

    def _read_capacity_reservations_locked(
        self,
        *,
        deadline_ns: int | None = None,
    ) -> tuple[CapacityReservation, ...]:
        """Project stable reservation arithmetic without recovery or mutation."""

        try:
            projection = self.state.project_record_recovery(
                label="capacity",
                current=_CAPACITY_CURRENT,
                previous=_CAPACITY_PREVIOUS,
                pending=_CAPACITY_PENDING,
                decoder=CapacityReservationState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
                deadline_ns=deadline_ns,
            )
        except StateCorrupt as exc:
            raise CapacityExceeded(f"capacity reservation state is corrupt: {exc}") from exc
        if projection.requires_recovery:
            raise CapacityExceeded("capacity reservation state requires durable recovery")
        if projection.record is None:
            return ()
        return projection.record.reservations

    def decision_capacity_usage_locked(
        self,
        repo_uuid: str,
        capacity_policy: CapacityPolicy,
        *,
        deadline_ns: int | None = None,
    ) -> DecisionCapacityUsage:
        """Return one stable capacity proof; caller owns registry/workspace locks."""

        policy = self._validated_capacity_policy(capacity_policy)
        usage = self._usage(
            self._read_capacity_reservations_locked(deadline_ns=deadline_ns),
            deadline_ns=deadline_ns,
        )
        workspace_bytes = usage.workspace_bytes(repo_uuid)
        if workspace_bytes > policy.workspace_max_bytes:
            raise CapacityExceeded("workspace byte limit is already exceeded")
        if usage.global_bytes > policy.global_max_bytes:
            raise CapacityExceeded("global byte limit is already exceeded")
        if usage.workspace_generations(repo_uuid) > policy.workspace_max_generations:
            raise CapacityExceeded("workspace generation limit is already exceeded")
        if usage.global_generations > policy.global_max_generations:
            raise CapacityExceeded("global generation limit is already exceeded")
        available = shutil.disk_usage(self.state.root.parent).free
        _require_inventory_deadline(deadline_ns)
        if available - usage.unconsumed_reserved_bytes < policy.reserve_bytes:
            raise CapacityExceeded("filesystem reserve threshold is already violated")
        return DecisionCapacityUsage(
            repo_uuid=repo_uuid,
            primary_bytes_by_generation=dict(usage.primary_bytes_by_generation),
            handoff_bytes_by_generation=dict(usage.handoff_bytes_by_generation),
            decision_bytes_by_generation=dict(usage.decision_bytes_by_generation),
            decision_bindings_by_generation=dict(usage.decision_bindings_by_generation),
            decision_binding_members=dict(usage.decision_binding_members),
            reserved_bytes_by_generation=dict(usage.reserved_bytes_by_generation),
            unconsumed_reserved_bytes=usage.unconsumed_reserved_bytes,
        )

    def preflight_decision_install_locked(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        candidate_bytes: int,
        additional_bytes: int,
        capacity_policy: CapacityPolicy,
        usage: DecisionCapacityUsage,
    ) -> None:
        """Apply binding caps, byte ceilings, reservations, and reserve exactly once."""

        policy = self._validated_capacity_policy(capacity_policy)
        if usage.repo_uuid != repo_uuid:
            raise CapacityExceeded("decision usage belongs to another workspace")
        if candidate_bytes < 1 or candidate_bytes > _DECISION_BINDING_MAX_BYTES:
            raise CapacityExceeded("decision binding exceeds 25 MiB")
        if additional_bytes not in {0, candidate_bytes}:
            raise CapacityExceeded("decision binding additional-byte projection is invalid")
        if additional_bytes:
            if (
                usage.generation_binding_count(generation_id)
                >= _DECISION_BINDINGS_PER_GENERATION
            ):
                raise CapacityExceeded("decision generation exceeds 64 bindings")
            if usage.workspace_binding_count >= _DECISION_BINDINGS_PER_WORKSPACE:
                raise CapacityExceeded("decision workspace exceeds 4096 bindings")
        projected_workspace = _bounded_capacity_sum(
            (usage.workspace_bytes, additional_bytes)
        )
        projected_global = _bounded_capacity_sum((usage.global_bytes, additional_bytes))
        if projected_workspace > policy.workspace_max_bytes:
            raise CapacityExceeded("workspace byte limit would be exceeded")
        if projected_global > policy.global_max_bytes:
            raise CapacityExceeded("global byte limit would be exceeded")
        available = shutil.disk_usage(self.state.root.parent).free
        if (
            available
            - usage.unconsumed_reserved_bytes
            - additional_bytes
            - (SEMANTIC_RELEASE_DECISION_STAGING_OVERHEAD_BYTES if additional_bytes else 0)
            < policy.reserve_bytes
        ):
            raise CapacityExceeded("filesystem reserve threshold would be violated")

    def decision_state_generations_locked(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> frozenset[str]:
        """Return exact nonempty decision targets without creating or repairing state."""

        previous: tuple[tuple[str, int, int], ...] | None = None
        for _attempt in range(5):
            _require_inventory_deadline(deadline_ns)
            try:
                observed: list[tuple[str, int, int]] = []
                total_bindings = 0
                for generation_id in self._decision_generation_ids(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                ):
                    if _GENERATION_ID_RE.fullmatch(generation_id) is None:
                        raise StatePathError("decision target generation is noncanonical")
                    size, count, _members = self._decision_directory_usage(
                        repo_uuid,
                        generation_id,
                        maximum_bindings=min(
                            _DECISION_BINDINGS_PER_GENERATION,
                            _DECISION_BINDINGS_PER_WORKSPACE - total_bindings,
                        ),
                        deadline_ns=deadline_ns,
                    )
                    total_bindings += count
                    if total_bindings > _DECISION_BINDINGS_PER_WORKSPACE:
                        raise StatePathError("decision workspace exceeds 4096 bindings")
                    observed.append((generation_id, size, count))
            except (FileNotFoundError, StatePathError) as exc:
                if isinstance(exc, StatePathError):
                    raise CapacityExceeded(
                        f"unsafe semantic-release decision state: {exc}"
                    ) from exc
                previous = None
                continue
            snapshot = tuple(observed)
            if previous is not None and snapshot == previous:
                _require_inventory_deadline(deadline_ns)
                return frozenset(generation_id for generation_id, _size, count in snapshot if count)
            previous = snapshot
        raise CapacityExceeded("semantic-release decision snapshot did not stabilize")

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
        projected_size = _bounded_capacity_sum(
            (
                usage.handoff_bytes_by_generation.get(key, 0),
                usage.decision_bytes_by_generation.get(key, 0),
                max(
                    usage.primary_bytes_by_generation.get(key, 0),
                    expected_payload_bytes,
                ),
            )
        )
        projected_global_bytes = _bounded_capacity_sum((usage.global_bytes - prior, projected_size))
        projected_workspace_bytes = _bounded_capacity_sum(
            (usage.workspace_bytes(repo_uuid) - prior, projected_size)
        )
        additional_generation = 0 if key in usage.bytes_by_generation else 1
        if projected_workspace_bytes > policy.workspace_max_bytes:
            raise CapacityExceeded("workspace byte limit would be exceeded")
        if projected_global_bytes > policy.global_max_bytes:
            raise CapacityExceeded("global byte limit would be exceeded")
        if (
            usage.workspace_generations(repo_uuid) + additional_generation
            > policy.workspace_max_generations
        ):
            raise CapacityExceeded("workspace generation limit would be exceeded")
        if usage.global_generations + additional_generation > policy.global_max_generations:
            raise CapacityExceeded("global generation limit would be exceeded")
        available = shutil.disk_usage(self.state.root.parent).free
        additional_bytes = 0 if existing is not None else expected_payload_bytes
        if available - usage.unconsumed_reserved_bytes - additional_bytes < policy.reserve_bytes:
            raise CapacityExceeded("filesystem reserve threshold would be violated")

    def preflight_retained_handoff_locked(
        self,
        *,
        repo_uuid: str,
        generation_id: str,
        expected_payload_bytes: int,
        handoff_bytes: int,
        existing_handoff_bytes: int,
        policy: CapacityPolicy,
    ) -> None:
        """Charge immutable handoff bytes plus one full target reservation.

        The caller owns the canonical registry-before-workspace lock pair. This
        method performs no durable mutation; it supplies the shared-capacity
        precondition for the install that immediately follows under those locks.
        """

        policy = self._validated_capacity_policy(policy)
        if _GENERATION_ID_RE.fullmatch(generation_id) is None:
            raise CapacityExceeded("handoff target generation is invalid")
        if expected_payload_bytes < 1 or handoff_bytes < 1:
            raise CapacityExceeded("handoff capacity inputs must be positive")
        if existing_handoff_bytes not in {0, handoff_bytes}:
            raise CapacityExceeded("existing handoff bytes differ from exact replay")
        capacity = self._load_capacity_locked()
        reservations = () if capacity is None else capacity.reservations
        existing_reservation = next(
            (
                item
                for item in reservations
                if (item.repo_uuid, item.generation_id) == (repo_uuid, generation_id)
            ),
            None,
        )
        if existing_reservation is not None and (
            existing_reservation.reserved_bytes != expected_payload_bytes
            or existing_reservation.policy_sha256 != policy.sha256
            or existing_reservation.compatibility_sha256 != self.compatibility_sha256
        ):
            raise CapacityExceeded("target reservation differs from handoff authority")
        usage = self._usage(reservations)
        key = (repo_uuid, generation_id)
        current_total = usage.bytes_by_generation.get(key, 0)
        retained = usage.handoff_bytes_by_generation.get(key, 0)
        additional_handoff = 0 if existing_handoff_bytes else handoff_bytes
        projected_retained = _bounded_capacity_sum((retained, additional_handoff))
        projected_total = _bounded_capacity_sum(
            (
                projected_retained,
                usage.decision_bytes_by_generation.get(key, 0),
                max(
                    usage.primary_bytes_by_generation.get(key, 0),
                    usage.reserved_bytes_by_generation.get(key, 0),
                    expected_payload_bytes,
                ),
            )
        )
        projected_global = _bounded_capacity_sum(
            (usage.global_bytes - current_total, projected_total)
        )
        projected_workspace = _bounded_capacity_sum(
            (usage.workspace_bytes(repo_uuid) - current_total, projected_total)
        )
        additional_generation = 0 if key in usage.bytes_by_generation else 1
        if projected_workspace > policy.workspace_max_bytes:
            raise CapacityExceeded("workspace byte limit would be exceeded")
        if projected_global > policy.global_max_bytes:
            raise CapacityExceeded("global byte limit would be exceeded")
        if (
            usage.workspace_generations(repo_uuid) + additional_generation
            > policy.workspace_max_generations
        ):
            raise CapacityExceeded("workspace generation limit would be exceeded")
        if usage.global_generations + additional_generation > policy.global_max_generations:
            raise CapacityExceeded("global generation limit would be exceeded")
        available = shutil.disk_usage(self.state.root.parent).free
        additional_reservation = 0 if existing_reservation is not None else expected_payload_bytes
        if (
            available
            - usage.unconsumed_reserved_bytes
            - additional_reservation
            - additional_handoff
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
                if (item.repo_uuid, item.generation_id) == (operation.repo_uuid, generation_id)
            ),
            None,
        )
        if existing is not None:
            if (
                existing.reserved_bytes != expected_payload_bytes
                or existing.policy_sha256 != policy.sha256
                or existing.compatibility_sha256 != self.compatibility_sha256
                or existing.active_source_revision != operation.grant.active_source_revision
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
                self.fault_hook(f"generation:{generation_id}:certification_recovery_allocation")
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
                raise GenerationConflict(f"generation lifecycle cannot be adopted from {latest}")
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
            raise GenerationConflict("generation allocation differs from its staged build request")

    def _require_staged_allocation_request_locked(
        self,
        operation: LeaseOperation,
        *,
        generation_id: str,
        expected_payload_bytes: int,
        capacity_policy: CapacityPolicy,
    ) -> StagedBuildState | None:
        state = self._load_staged_build_locked(operation.repo_uuid)
        if state is None or state.lifecycle_state in _STAGED_BUILD_TERMINAL_STATES:
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
            raise GenerationConflict("allocation request differs from staged build authority")
        return state

    def _staging_names(self, repo_uuid: str, generation_id: str) -> list[str]:
        relative = self._staging(repo_uuid, generation_id)
        with self.state.existing_private_directory(relative) as descriptor:
            return _directory_names(descriptor, deadline_ns=None)

    def _reuse_staged_completion_locked(
        self,
        state: StagedBuildState,
        allocation: GenerationAllocation,
        *,
        deadline_ns: int | None = None,
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
        container = allocation.staging_path if staging_exists else self.state.path(final_relative)
        staged_receipt = (
            self.state.read_optional_existing_bytes(
                staging_relative / "receipt.json",
                deadline_ns=deadline_ns,
            )
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
                raise GenerationConflict(f"durable staged receipt is invalid: {exc}") from exc
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
            deadline_ns=deadline_ns,
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
                    self.fault_hook(f"generation:{state.generation_id}:successor_staging_empty")
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
            if preparation.state.repo_uuid != recovery_operation.repo_uuid:
                raise GenerationConflict("staged build preparation repo_uuid mismatch")
            repo_uuid = recovery_operation.repo_uuid
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
                    f"staged build cannot complete from {recovery_state.lifecycle_state}"
                )

        self._require_structural_evidence(
            repo_uuid,
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
                    raise GenerationConflict("staged build publication belongs to another fence")
                inventory = self._inventory(
                    preparation.allocation.staging_path,
                    allowed_root_entries=frozenset({"graphify-out"}),
                )
                payload_bytes = inventory.total_bytes
                if payload_bytes <= preparation.allocation.expected_payload_bytes:
                    self._sync_inventory(
                        operation.repo_uuid,
                        state.generation_id,
                        inventory,
                    )
                    self.fault_hook(
                        f"generation:{state.generation_id}:before_completion_reinventory"
                    )
                    reinventory = self._inventory(
                        preparation.allocation.staging_path,
                        allowed_root_entries=frozenset({"graphify-out"}),
                    )
                    if canonical_json_bytes(list(reinventory.entries)) != canonical_json_bytes(
                        list(inventory.entries)
                    ):
                        raise PayloadChanged("payload changed during staged completion")
                    manifest = payload_manifest_sha256(
                        "graphify-out",
                        reinventory.entries,
                    )
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
            proof = self._staged_abandonment_proof_if_stale_locked(
                operation,
                state,
                self._abandonment_source_document(source_observations),
                capacity_failure_payload_bytes=payload_bytes,
            )
            if proof is None:  # pragma: no cover - payload bytes prove closure
                raise GenerationConflict("payload capacity failure produced no terminal evidence")
            reason, evidence, _pointer = proof
            lease_value = preparation.grant.lease.to_dict()
            occurred_at = datetime.fromisoformat(
                str(lease_value["acquired_at"]).replace("Z", "+00:00")
            )
            self._commit_new_staged_abandonment_locked(
                operation,
                state,
                reason=reason,
                evidence=evidence,
                occurred_at=occurred_at,
            )
            raise CapacityExceeded("staged payload exceeds its durable reservation")

    def complete_staged_promotion(
        self,
        attempt: StagedBuildOperation,
        pointer: PointerSet,
        *,
        monotonic_ns: int,
        validate_current: Callable[[LeaseOperation, StagedBuildState], None]
        | None = None,
    ) -> StagedBuildState:
        """Record a separately-authoritative pointer move as terminal.

        This method never moves or repairs pointers. It only verifies the
        visible pointer plus its authoritative journal event, then releases the
        staged-build recovery barrier. An optional current-state validator runs
        under the same workspace lock before the staged transition.
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
                raise GenerationConflict("visible pointer did not advance the staged request CAS")
            if (
                int(pointer_value["operation_epoch"]) > operation.grant.operation_epoch
                or int(pointer_value["fence_token"]) > operation.fence_token
            ):
                raise GenerationConflict("visible pointer belongs to a newer fence")
            if validate_current is not None:
                validate_current(operation, state)
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
                    raise GenerationConflict("certified generation differs from staged build state")
                snapshot = self.journal.recover_locked(operation)
                journal_match = any(
                    event.to_dict()["transition"] in {"PROMOTED", "REPAIRED"}
                    and event.to_dict()["generation_id"] == state.generation_id
                    and event.to_dict()["receipt_sha256"] == state.receipt_sha256
                    and event.to_dict()["pointer_revision"] == pointer_revision
                    and event.to_dict()["operation_epoch"] == pointer_value["operation_epoch"]
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
                self.fault_hook(f"generation:{state.generation_id}:staged_promoted_durable")
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
                if _identity(opened_before) != _identity(opened_after) or _identity(
                    opened_after
                ) != _identity(current):
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
                raise GenerationError(f"semantic certification binding is invalid: {exc}") from exc
            if (
                queue_view.repo_uuid != repo_uuid
                or queue_view.source_commit != request.source_commit
                or queue_view.source_epoch != request.source_epoch
                or queue_view.policy_sha256 != request.policy_sha256
                or queue_view.observation_manifest_sha256 != request.observation_manifest_sha256
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
        if receipt_epoch > operation.grant.operation_epoch or receipt_fence > operation.fence_token:
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
                    event.to_dict()["receipt_sha256"] == receipt.sha256 for event in certified
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
                int(latest_event.to_dict()["operation_epoch"]) != operation.grant.operation_epoch
                or int(latest_event.to_dict()["fence_token"]) != operation.fence_token
            )
        )
        if latest == "BUILT" or (validating_requires_successor_recheck and not receipt_present):
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
            raise SemanticCertificationBlocked("trusted source identity changed during observation")
        if trusted[0] != trusted[1]:
            raise SemanticCertificationBlocked("trusted source observations are not stable")
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
        if state is None or state.lifecycle_state in _STAGED_BUILD_TERMINAL_STATES:
            if completion is not None:
                raise GenerationConflict(
                    "staged completion was supplied without active staged-build authority"
                )
            return None
        if completion is None:
            raise GenerationConflict("active staged build requires durable completion proof")
        self._require_staged_binding(
            state,
            repo_uuid=operation.repo_uuid,
            generation_id=allocation.generation_id,
            request=completion.state.request,
        )
        if state.lifecycle_state != "COMPLETE":
            raise GenerationConflict(f"staged build cannot certify from {state.lifecycle_state}")
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
            raise GenerationConflict("certification request differs from staged build authority")
        if canonical_json_bytes(list(completion.entries)) != canonical_json_bytes(
            list(declared_entries)
        ):
            raise PayloadChanged("certification manifest differs from staged completion proof")
        if (
            payload_manifest_sha256("graphify-out", declared_entries)
            != state.payload_manifest_sha256
        ):
            raise PayloadChanged("certification manifest differs from durable staged completion")
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
    "StagedBuildReadRecoveryRequired",
    "StagedBuildState",
    "StagedBuildStillCurrent",
    "StructuralBuildRequest",
]
