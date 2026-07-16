"""Explicit dry-run-first offline garbage collection for certified generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Any, cast

from graphify.workspace.contracts import (
    CapacityPolicy,
    ContractError,
    GcCompletionState,
    GcIntentState,
    GcPurgeState,
    PointerSet,
    canonical_json_bytes,
    canonical_sha256,
)
from graphify.workspace.generations import GenerationStore
from graphify.workspace.leases import LeaseGrant, LeaseOperation, LeaseStore
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StatePathError,
    Syscalls,
)
from graphify.workspace.pointers import PointerStore


class GcError(RuntimeError):
    """Base class for stable offline-GC failures."""

    code = "gc_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class GcPlanStale(GcError):
    code = "gc_plan_stale"


class GcRecoveryRequired(GcError):
    code = "gc_recovery_required"


@dataclass(frozen=True)
class GcProtection:
    """Caller-owned non-pointer reachability that P3 cannot infer safely."""

    migration_sources: frozenset[str]
    rollback_sources: frozenset[str]
    active_lease_generations: frozenset[str]
    fixture_generations: frozenset[str]
    proof_generations: frozenset[str]
    rollback_artifact_generations: frozenset[str]

    def reasons(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for reason, generation_ids in (
            ("migration_source", self.migration_sources),
            ("rollback_source", self.rollback_sources),
            ("active_lease", self.active_lease_generations),
            ("fixture", self.fixture_generations),
            ("proof", self.proof_generations),
            ("rollback_artifact", self.rollback_artifact_generations),
        ):
            for generation_id in generation_ids:
                result.setdefault(generation_id, set()).add(reason)
        return result


@dataclass(frozen=True)
class GcPlan:
    repo_uuid: str
    registry_revision: int
    active_source_revision: int
    operation_epoch: int
    migration_epoch: int
    fence_token: int
    pointer_revision: int
    capacity_policy_sha256: str
    candidates: tuple[str, ...]
    protected: tuple[tuple[str, tuple[str, ...]], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_uuid": self.repo_uuid,
            "registry_revision": self.registry_revision,
            "active_source_revision": self.active_source_revision,
            "operation_epoch": self.operation_epoch,
            "migration_epoch": self.migration_epoch,
            "fence_token": self.fence_token,
            "pointer_revision": self.pointer_revision,
            "capacity_policy_sha256": self.capacity_policy_sha256,
            "candidates": list(self.candidates),
            "protected": [
                {"generation_id": generation_id, "reasons": list(reasons)}
                for generation_id, reasons in self.protected
            ],
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise GcError("GC timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


class GcStore:
    """Plan without writes, quarantine under intent, reconcile, then purge explicitly."""

    def __init__(
        self,
        state_root: Path,
        leases: LeaseStore,
        generations: GenerationStore,
        pointers: PointerStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.leases = leases
        self.generations = generations
        self.pointers = pointers
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        roots = {self.state.root, leases.state.root, generations.state.root, pointers.state.root}
        if len(roots) != 1:
            raise GcError("GC dependencies must share one external state root")
        self.fault_hook = fault_hook or (lambda _event: None)

    @staticmethod
    def _validated_capacity_policy(policy: CapacityPolicy) -> CapacityPolicy:
        try:
            return CapacityPolicy.from_mapping(policy.to_dict())
        except ContractError as exc:
            raise GcError(f"capacity policy is invalid: {exc}") from exc

    @staticmethod
    def _workspace(repo_uuid: str) -> Path:
        return LeaseStore._directory(repo_uuid)

    @classmethod
    def _intent_path(cls, repo_uuid: str) -> Path:
        return cls._workspace(repo_uuid) / "gc" / "intent.json"

    @classmethod
    def _completion_path(cls, repo_uuid: str, plan_sha256: str) -> Path:
        return cls._workspace(repo_uuid) / "gc" / "completions" / f"{plan_sha256}.json"

    @classmethod
    def _purge_path(cls, repo_uuid: str, plan_sha256: str) -> Path:
        return cls._workspace(repo_uuid) / "gc" / "purges" / f"{plan_sha256}.json"

    @classmethod
    def _quarantine(cls, repo_uuid: str, generation_id: str, operation_epoch: int) -> Path:
        return (
            cls._workspace(repo_uuid) / "quarantine" / "gc" / f"{generation_id}.{operation_epoch}"
        )

    def _read_intent(self, repo_uuid: str) -> GcIntentState | None:
        relative = self._intent_path(repo_uuid)
        payload = self.state.read_optional_existing_bytes(relative)
        if payload is None:
            return None
        try:
            intent = GcIntentState.from_json(payload)
        except Exception as exc:
            raise GcRecoveryRequired(f"GC intent is invalid: {exc}") from exc
        if intent.repo_uuid != repo_uuid:
            raise GcRecoveryRequired("GC intent belongs to another workspace")
        return intent

    def _generation_ids(self, repo_uuid: str) -> tuple[str, ...]:
        relative = self.generations._workspace(repo_uuid) / "generations"
        try:
            return self.state.list_existing_private_directories(
                relative,
                allow_missing=True,
            )
        except StatePathError as exc:
            raise GcError(f"generations path is unsafe: {exc}") from exc

    @staticmethod
    def _add_pointer_reasons(
        reasons: dict[str, set[str]],
        pointer: PointerSet,
        *,
        prefix: str,
    ) -> None:
        value = pointer.to_dict()
        current = cast(dict[str, Any], value["current"])
        reasons.setdefault(str(current["generation_id"]), set()).add(f"{prefix}_current")
        if value["last_good"] is not None:
            last_good = cast(dict[str, Any], value["last_good"])
            reasons.setdefault(str(last_good["generation_id"]), set()).add(f"{prefix}_last_good")

    def _plan_locked(
        self,
        operation: LeaseOperation,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        probe_locks: bool,
    ) -> GcPlan:
        pointer = self.pointers.load(operation.repo_uuid, allow_missing=True)
        reasons = protections.reasons()
        pointer_revision = 0
        if pointer is not None:
            self.pointers.verify_pointer(pointer, expected_repo_uuid=operation.repo_uuid)
            pointer_revision = int(pointer.to_dict()["pointer_revision"])
            self._add_pointer_reasons(reasons, pointer, prefix="visible")
        prior = self.pointers.retained_prior(operation.repo_uuid)
        if prior is not None:
            prior_pointer = cast(
                PointerSet,
                PointerSet.from_mapping(prior.to_dict()["pointer_set"]),
            )
            self._add_pointer_reasons(reasons, prior_pointer, prefix="prior")
        generations = self._generation_ids(operation.repo_uuid)
        if probe_locks:
            for generation_id in generations:
                if generation_id in reasons:
                    continue
                lock = self.generations._lock(operation.repo_uuid, generation_id)
                try:
                    with self.state.existing_generation_lock(
                        lock,
                        generation_id=generation_id,
                        exclusive=True,
                        blocking=False,
                    ):
                        pass
                except BlockingIOError:
                    reasons.setdefault(generation_id, set()).add("shared_lock")
                except (OSError, StatePathError) as exc:
                    raise GcError(f"generation coordination lock is unavailable: {exc}") from exc
        candidates = tuple(
            generation_id for generation_id in generations if generation_id not in reasons
        )
        protected = tuple(
            (generation_id, tuple(sorted(names)))
            for generation_id, names in sorted(reasons.items())
        )
        plan = GcPlan(
            repo_uuid=operation.repo_uuid,
            registry_revision=int(operation.registry.to_dict()["revision"]),
            active_source_revision=operation.grant.active_source_revision,
            operation_epoch=operation.grant.operation_epoch,
            migration_epoch=operation.grant.migration_epoch,
            fence_token=operation.fence_token,
            pointer_revision=pointer_revision,
            capacity_policy_sha256=capacity_policy.sha256,
            candidates=candidates,
            protected=protected,
        )
        self.fault_hook("gc:reachability_enumerated")
        return plan

    def plan(
        self,
        grant: LeaseGrant,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        monotonic_ns: int,
    ) -> GcPlan:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation_read_only(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC"}),
        ) as operation:
            if self._read_intent(operation.repo_uuid) is not None:
                raise GcRecoveryRequired("an unresolved GC intent must be reconciled")
            return self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=True,
            )

    def _intent(
        self,
        operation: LeaseOperation,
        plan: GcPlan,
        *,
        occurred_at: datetime,
    ) -> GcIntentState:
        return GcIntentState.from_mapping(
            {
                "contract": "graphify.workspace.gc_intent.internal",
                "format_version": 1,
                "repo_uuid": operation.repo_uuid,
                "operation_epoch": operation.grant.operation_epoch,
                "fence_token": operation.fence_token,
                "active_source_revision": operation.grant.active_source_revision,
                "migration_epoch": operation.grant.migration_epoch,
                "pointer_revision": plan.pointer_revision,
                "capacity_policy_sha256": plan.capacity_policy_sha256,
                "plan_sha256": plan.sha256,
                "candidates": list(plan.candidates),
                "occurred_at": _timestamp(occurred_at),
            }
        )

    def _completion(
        self,
        intent: GcIntentState,
        *,
        completed_at: datetime,
    ) -> GcCompletionState:
        return GcCompletionState.from_mapping(
            {
                "contract": "graphify.workspace.gc_completion.internal",
                "format_version": 1,
                "repo_uuid": intent.repo_uuid,
                "operation_epoch": intent.operation_epoch,
                "intent_sha256": intent.sha256,
                "plan_sha256": intent.plan_sha256,
                "quarantined": list(intent.candidates),
                "completed_at": _timestamp(completed_at),
            }
        )

    def _rename_candidates(self, intent: GcIntentState) -> None:
        for generation_id in intent.candidates:
            source = self.generations._generation(intent.repo_uuid, generation_id)
            destination = self._quarantine(
                intent.repo_uuid,
                generation_id,
                intent.operation_epoch,
            )
            source_exists = self.state.path(source).exists()
            destination_exists = self.state.path(destination).exists()
            if source_exists == destination_exists:
                raise GcRecoveryRequired(
                    f"GC location is ambiguous for {generation_id}: "
                    f"source={source_exists} quarantine={destination_exists}"
                )
            if source_exists:
                self.state.rename_contained(
                    source,
                    destination,
                    label=f"gc:{generation_id}:quarantine",
                )
                self.fault_hook(f"gc:{generation_id}:quarantined")

    def _write_completion(
        self,
        intent: GcIntentState,
        completion: GcCompletionState,
    ) -> None:
        self.state.install_once_bytes(
            self._completion_path(intent.repo_uuid, intent.plan_sha256),
            completion.canonical,
            label="gc:completion",
        )
        self.fault_hook("gc:completion_durable")

    def _read_completion(self, intent: GcIntentState) -> GcCompletionState | None:
        relative = self._completion_path(intent.repo_uuid, intent.plan_sha256)
        self.state.cleanup_atomic_temps(relative.parent)
        path = self.state.path(relative)
        if not path.exists():
            return None
        try:
            completion = GcCompletionState.from_json(self.state.read_existing_bytes(relative))
        except Exception as exc:
            raise GcRecoveryRequired(f"GC completion is invalid: {exc}") from exc
        if (
            completion.repo_uuid != intent.repo_uuid
            or completion.operation_epoch != intent.operation_epoch
            or completion.intent_sha256 != intent.sha256
            or completion.plan_sha256 != intent.plan_sha256
            or completion.quarantined != intent.candidates
        ):
            raise GcRecoveryRequired("GC completion does not bind the durable intent")
        return completion

    def execute(
        self,
        grant: LeaseGrant,
        plan: GcPlan,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> GcCompletionState:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC"}),
        ) as operation:
            self.state.cleanup_atomic_temps(self._workspace(operation.repo_uuid) / "gc")
            if self._read_intent(operation.repo_uuid) is not None:
                raise GcRecoveryRequired("an unresolved GC intent must be reconciled")
            refreshed = self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=True,
            )
            if refreshed.canonical != plan.canonical:
                raise GcPlanStale("GC dry-run plan no longer matches reachability")
            intent = self._intent(operation, plan, occurred_at=occurred_at)
            self.state.install_once_bytes(
                self._intent_path(operation.repo_uuid),
                intent.canonical,
                label="gc:intent",
            )
            self.fault_hook("gc:intent_durable")
            locks = [
                (
                    generation_id,
                    self.generations._lock(operation.repo_uuid, generation_id),
                )
                for generation_id in plan.candidates
            ]
            with self.state.existing_generation_locks(locks, exclusive=True):
                self.fault_hook("gc:generation_locks_acquired")
                locked_plan = self._plan_locked(
                    operation,
                    capacity_policy=capacity_policy,
                    protections=protections,
                    probe_locks=False,
                )
                if locked_plan.canonical != plan.canonical:
                    raise GcRecoveryRequired("GC reachability changed after durable intent")
                self.fault_hook("gc:reachability_rechecked")
                self._rename_candidates(intent)
            completion = self._read_completion(intent)
            if completion is None:
                completion = self._completion(intent, completed_at=occurred_at)
                self._write_completion(intent, completion)
            self.state.unlink_and_sync(
                self._intent_path(operation.repo_uuid),
                label="gc:intent_clear",
            )
            self.fault_hook("gc:complete")
            return completion

    def reconcile(
        self,
        grant: LeaseGrant,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        completed_at: datetime,
        monotonic_ns: int,
    ) -> GcCompletionState | None:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC", "POINTER_RECOVERY"}),
        ) as operation:
            self.state.cleanup_atomic_temps(self._workspace(operation.repo_uuid) / "gc")
            intent = self._read_intent(operation.repo_uuid)
            if intent is None:
                return None
            if intent.capacity_policy_sha256 != capacity_policy.sha256:
                raise GcRecoveryRequired("capacity policy differs from durable GC intent")
            refreshed = self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=False,
            )
            protected = {generation_id for generation_id, _reasons in refreshed.protected}
            if any(generation_id in protected for generation_id in intent.candidates):
                raise GcRecoveryRequired("a durable GC candidate became reachable")
            locks = [
                (
                    generation_id,
                    self.generations._lock(operation.repo_uuid, generation_id),
                )
                for generation_id in intent.candidates
            ]
            with self.state.existing_generation_locks(locks, exclusive=True):
                self._rename_candidates(intent)
            completion = self._read_completion(intent)
            if completion is None:
                completion = self._completion(intent, completed_at=completed_at)
                self._write_completion(intent, completion)
            self.state.unlink_and_sync(
                self._intent_path(operation.repo_uuid),
                label="gc:reconcile_clear",
            )
            self.fault_hook("gc:reconciled")
            return completion

    def _remove_quarantine(self, path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise GcError(f"quarantine path is unsafe: {path}")
        self.state._require_owner(path.lstat(), path)
        for root, directories, files in os.walk(path, topdown=False, followlinks=False):
            root_path = Path(root)
            for name in files:
                candidate = root_path / name
                details = candidate.lstat()
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise GcError(f"unsafe file in quarantine: {candidate}")
                self.state._require_owner(details, candidate)
                self.state.syscalls.unlink(candidate)
            for name in directories:
                candidate = root_path / name
                details = candidate.lstat()
                if not stat.S_ISDIR(details.st_mode) or candidate.is_symlink():
                    raise GcError(f"unsafe directory in quarantine: {candidate}")
                self.state._require_owner(details, candidate)
                self.state.syscalls.rmdir(candidate)
        self.state.syscalls.rmdir(path)
        self.state.fsync_directory(path.parent.relative_to(self.state.root))

    def purge(
        self,
        grant: LeaseGrant,
        *,
        plan_sha256: str,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        completed_at: datetime,
        monotonic_ns: int,
    ) -> GcPurgeState:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC"}),
        ) as operation:
            self.state.cleanup_atomic_temps(self._workspace(operation.repo_uuid) / "gc")
            if self._read_intent(operation.repo_uuid) is not None:
                raise GcRecoveryRequired("GC intent must be reconciled before purge")
            purge_relative = self._purge_path(operation.repo_uuid, plan_sha256)
            if self.state.path(purge_relative).exists():
                try:
                    purge = GcPurgeState.from_json(
                        self.state.read_existing_bytes(purge_relative)
                    )
                except Exception as exc:
                    raise GcError(f"GC purge record is invalid: {exc}") from exc
                if purge.repo_uuid != operation.repo_uuid or purge.plan_sha256 != plan_sha256:
                    raise GcError("GC purge record belongs to another workspace or plan")
                return purge
            completion_relative = self._completion_path(operation.repo_uuid, plan_sha256)
            self.state.cleanup_atomic_temps(completion_relative.parent)
            try:
                completion = GcCompletionState.from_json(
                    self.state.read_existing_bytes(completion_relative)
                )
            except Exception as exc:
                raise GcError(f"GC completion is unavailable: {exc}") from exc
            if (
                completion.repo_uuid != operation.repo_uuid
                or completion.plan_sha256 != plan_sha256
            ):
                raise GcError("GC completion belongs to another workspace or plan")
            refreshed = self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=False,
            )
            protected = {generation_id for generation_id, _reasons in refreshed.protected}
            if any(generation_id in protected for generation_id in completion.quarantined):
                raise GcPlanStale("quarantined generation became protected before purge")
            locks = [
                (
                    generation_id,
                    self.generations._lock(operation.repo_uuid, generation_id),
                )
                for generation_id in completion.quarantined
            ]
            with self.state.existing_generation_locks(locks, exclusive=True):
                for generation_id in completion.quarantined:
                    quarantine = self.state.path(
                        self._quarantine(
                            operation.repo_uuid,
                            generation_id,
                            completion.operation_epoch,
                        )
                    )
                    if quarantine.exists():
                        self._remove_quarantine(quarantine)
                        self.fault_hook(f"gc:{generation_id}:purged")
                    else:
                        self.state.fsync_directory(
                            quarantine.parent.relative_to(self.state.root)
                        )
            purge = GcPurgeState.from_mapping(
                {
                    "contract": "graphify.workspace.gc_purge.internal",
                    "format_version": 1,
                    "repo_uuid": operation.repo_uuid,
                    "operation_epoch": operation.grant.operation_epoch,
                    "plan_sha256": plan_sha256,
                    "purged": list(completion.quarantined),
                    "completed_at": _timestamp(completed_at),
                }
            )
            self.state.install_once_bytes(
                purge_relative,
                purge.canonical,
                label="gc:purge",
            )
            self.fault_hook("gc:purge_complete")
            return purge


__all__ = [
    "GcError",
    "GcPlan",
    "GcPlanStale",
    "GcProtection",
    "GcRecoveryRequired",
    "GcStore",
]
