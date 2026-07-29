"""Explicit dry-run-first offline garbage collection for certified generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from graphify.workspace.contracts import (
    CapacityPolicy,
    ContractError,
    GcCompletionIndexState,
    GcCompletionState,
    GcIntentState,
    GcPurgeState,
    PointerSet,
    Registry,
    WorkspaceLeaseState,
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
    require_before_deadline,
)
from graphify.workspace.pointers import PointerStore
from graphify.workspace.semantic_queue import SemanticQueueStore


_PURGE_ALLOWED_DIRECTORY_MODES = frozenset({0o700, 0o755})
_PURGE_ALLOWED_FILE_MODES = frozenset({0o600, 0o644, 0o755})
_MAX_GC_INTENT_BYTES = 1024 * 1024
GC_PREVIEW_MAX_GENERATIONS = 4096


class GcError(RuntimeError):
    """Base class for stable offline-GC failures."""

    code = "gc_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class GcPlanStale(GcError):
    code = "gc_plan_stale"


class GcRecoveryRequired(GcError):
    code = "gc_recovery_required"


class GcPreviewAuthorityConflict(GcError):
    """The caller's read-only preview CAS no longer matches durable authority."""

    code = "gc_preview_authority_conflict"


class GcPreviewUnstable(GcError):
    """Two read-only reachability observations did not match."""

    code = "gc_preview_unstable"


class GcCoordinationUnavailable(GcError):
    """A retained coordination object cannot be inspected safely."""

    code = "gc_coordination_unavailable"


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


@dataclass(frozen=True)
class GcPreview:
    """Unfenced read-only GC reachability; never executable as a ``GcPlan``."""

    repo_uuid: str
    registry_revision: int
    active_source_revision: int
    operation_epoch: int
    migration_epoch: int
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


@dataclass(frozen=True)
class _GcReachability:
    pointer_revision: int
    candidates: tuple[str, ...]
    protected: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(
            {
                "candidates": list(self.candidates),
                "pointer_revision": self.pointer_revision,
                "protected": [
                    {"generation_id": generation_id, "reasons": list(reasons)}
                    for generation_id, reasons in self.protected
                ],
            }
        )


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
    def _operation_completion_path(cls, repo_uuid: str, operation_epoch: int) -> Path:
        return (
            cls._workspace(repo_uuid)
            / "gc"
            / "operation-completions"
            / f"{operation_epoch}.json"
        )

    @classmethod
    def _purge_path(cls, repo_uuid: str, plan_sha256: str) -> Path:
        return cls._workspace(repo_uuid) / "gc" / "purges" / f"{plan_sha256}.json"

    @classmethod
    def _quarantine(cls, repo_uuid: str, generation_id: str, operation_epoch: int) -> Path:
        return (
            cls._workspace(repo_uuid) / "quarantine" / "gc" / f"{generation_id}.{operation_epoch}"
        )

    def _read_intent(
        self,
        repo_uuid: str,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> GcIntentState | None:
        relative = self._intent_path(repo_uuid)
        payload = self.state.read_optional_existing_bytes(
            relative,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )
        if payload is None:
            return None
        try:
            intent = GcIntentState.from_json(payload)
        except Exception as exc:
            raise GcRecoveryRequired(f"GC intent is invalid: {exc}") from exc
        if intent.repo_uuid != repo_uuid:
            raise GcRecoveryRequired("GC intent belongs to another workspace")
        return intent

    def read_only_intent_locked(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> GcIntentState | None:
        """Read and validate the optional GC recovery barrier without writes."""

        require_before_deadline(
            deadline_ns,
            "GC intent inspection exceeded its deadline",
        )
        intent = self._read_intent(
            repo_uuid,
            max_bytes=_MAX_GC_INTENT_BYTES,
            deadline_ns=deadline_ns,
        )
        require_before_deadline(
            deadline_ns,
            "GC intent inspection exceeded its deadline",
        )
        return intent

    def _generation_ids(
        self,
        repo_uuid: str,
        *,
        maximum_entries: int | None = None,
        deadline_ns: int | None = None,
    ) -> tuple[str, ...]:
        require_before_deadline(
            deadline_ns,
            "GC generation enumeration exceeded its deadline",
        )
        relative = self.generations._workspace(repo_uuid) / "generations"
        try:
            generation_ids = self.state.list_existing_private_directories(
                relative,
                allow_missing=True,
                maximum_entries=maximum_entries,
            )
        except StatePathError as exc:
            raise GcError(f"generations path is unsafe: {exc}") from exc
        require_before_deadline(
            deadline_ns,
            "GC generation enumeration exceeded its deadline",
        )
        return generation_ids

    @staticmethod
    def _registry_entry(document: Registry, repo_uuid: str) -> dict[str, Any]:
        entries = [
            cast(dict[str, Any], item)
            for item in document.to_dict()["workspaces"]
            if item["repo_uuid"] == repo_uuid
        ]
        if len(entries) != 1:
            raise GcPreviewAuthorityConflict(
                "GC preview request does not name one registered workspace"
            )
        return entries[0]

    @staticmethod
    def _validate_expected_revision(
        value: int,
        name: str,
        *,
        minimum: int,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise GcPreviewAuthorityConflict(f"{name} is invalid")
        return value

    def _verified_pointer_revision(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> int:
        pointer = self.pointers.load(
            repo_uuid,
            allow_missing=True,
            deadline_ns=deadline_ns,
        )
        if pointer is None:
            return 0
        self.pointers.verify_pointer(
            pointer,
            expected_repo_uuid=repo_uuid,
            deadline_ns=deadline_ns,
        )
        return int(pointer.to_dict()["pointer_revision"])

    def _read_purge_state_locked(
        self,
        repo_uuid: str,
        plan_sha256: str,
        *,
        deadline_ns: int | None = None,
    ) -> GcPurgeState | None:
        purge_data = self.state.read_optional_existing_bytes(
            self._purge_path(repo_uuid, plan_sha256),
            max_bytes=_MAX_GC_INTENT_BYTES,
            deadline_ns=deadline_ns,
        )
        if purge_data is None:
            return None
        try:
            purge = GcPurgeState.from_json(purge_data)
        except Exception as exc:
            raise GcError(f"GC purge record is invalid: {exc}") from exc
        if purge.repo_uuid != repo_uuid or purge.plan_sha256 != plan_sha256:
            raise GcError("GC purge record belongs to another workspace or plan")
        return purge

    def _read_operation_completion_locked(
        self,
        repo_uuid: str,
        operation_epoch: int,
        *,
        deadline_ns: int | None = None,
    ) -> GcCompletionState | None:
        if operation_epoch < 1:
            return None
        index_data = self.state.read_optional_existing_bytes(
            self._operation_completion_path(repo_uuid, operation_epoch),
            max_bytes=_MAX_GC_INTENT_BYTES,
            deadline_ns=deadline_ns,
        )
        if index_data is None:
            return None
        try:
            index = GcCompletionIndexState.from_json(index_data)
        except Exception as exc:
            raise GcRecoveryRequired(
                f"GC operation completion index is invalid: {exc}"
            ) from exc
        if (
            index.repo_uuid != repo_uuid
            or index.operation_epoch != operation_epoch
        ):
            raise GcRecoveryRequired(
                "GC operation completion index belongs to another workspace or epoch"
            )
        completion_data = self.state.read_optional_existing_bytes(
            self._completion_path(repo_uuid, index.plan_sha256),
            max_bytes=_MAX_GC_INTENT_BYTES,
            deadline_ns=deadline_ns,
        )
        if completion_data is None:
            raise GcRecoveryRequired(
                "GC operation completion is unavailable"
            )
        try:
            completion = GcCompletionState.from_json(completion_data)
        except Exception as exc:
            raise GcRecoveryRequired(
                f"GC operation completion is invalid: {exc}"
            ) from exc
        if (
            completion.repo_uuid != repo_uuid
            or completion.plan_sha256 != index.plan_sha256
            or canonical_sha256(completion.to_dict()) != index.completion_sha256
        ):
            raise GcRecoveryRequired(
                "GC operation completion index does not bind its completion"
            )
        return completion

    @classmethod
    def _check_preview_authority(
        cls,
        document: Registry,
        entry: dict[str, Any],
        state: WorkspaceLeaseState,
        *,
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
    ) -> None:
        actual = {
            "registry_revision": int(document.to_dict()["revision"]),
            "active_source_revision": int(entry["active_source_revision"]),
            "operation_epoch": state.operation_epoch,
            "migration_epoch": state.migration_epoch,
        }
        expected = {
            "registry_revision": cls._validate_expected_revision(
                expected_registry_revision,
                "expected_registry_revision",
                minimum=1,
            ),
            "active_source_revision": cls._validate_expected_revision(
                expected_active_source_revision,
                "expected_active_source_revision",
                minimum=1,
            ),
            "operation_epoch": cls._validate_expected_revision(
                expected_operation_epoch,
                "expected_operation_epoch",
                minimum=0,
            ),
            "migration_epoch": cls._validate_expected_revision(
                expected_migration_epoch,
                "expected_migration_epoch",
                minimum=0,
            ),
        }
        mismatches = [name for name in expected if expected[name] != actual[name]]
        if mismatches:
            raise GcPreviewAuthorityConflict(
                "GC preview authority changed: " + ", ".join(sorted(mismatches))
            )

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

    def _reachability_locked(
        self,
        repo_uuid: str,
        *,
        protections: GcProtection,
        probe_locks: bool,
        inspect_protected_locks: bool = False,
        deadline_ns: int | None = None,
        maximum_generations: int | None = None,
    ) -> _GcReachability:
        require_before_deadline(
            deadline_ns,
            "GC reachability inspection exceeded its deadline",
        )
        pointer = self.pointers.load(
            repo_uuid,
            allow_missing=True,
            deadline_ns=deadline_ns,
        )
        reasons = protections.reasons()
        pointer_revision = 0
        if pointer is not None:
            self.pointers.verify_pointer(
                pointer,
                expected_repo_uuid=repo_uuid,
                deadline_ns=deadline_ns,
            )
            pointer_revision = int(pointer.to_dict()["pointer_revision"])
            self._add_pointer_reasons(reasons, pointer, prefix="visible")
        prior = self.pointers.retained_prior(
            repo_uuid,
            deadline_ns=deadline_ns,
        )
        if prior is not None:
            prior_pointer = cast(
                PointerSet,
                PointerSet.from_mapping(prior.to_dict()["pointer_set"]),
            )
            self._add_pointer_reasons(reasons, prior_pointer, prefix="prior")
        generations = self._generation_ids(
            repo_uuid,
            maximum_entries=maximum_generations,
            deadline_ns=deadline_ns,
        )
        if maximum_generations is not None and len(set(generations) | set(reasons)) > (
            maximum_generations
        ):
            raise GcError("GC preview generation set exceeds its public bound")
        if probe_locks:
            for generation_id in generations:
                require_before_deadline(
                    deadline_ns,
                    "GC reachability inspection exceeded its deadline",
                )
                if generation_id in reasons and not inspect_protected_locks:
                    continue
                lock = self.generations._lock(repo_uuid, generation_id)
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
                    detail = f"generation coordination lock is unavailable: {exc}"
                    if inspect_protected_locks:
                        raise GcCoordinationUnavailable(detail) from exc
                    raise GcError(detail) from exc
        candidates = tuple(
            generation_id for generation_id in generations if generation_id not in reasons
        )
        protected = tuple(
            (generation_id, tuple(sorted(names)))
            for generation_id, names in sorted(reasons.items())
        )
        require_before_deadline(
            deadline_ns,
            "GC reachability inspection exceeded its deadline",
        )
        reachability = _GcReachability(
            pointer_revision=pointer_revision,
            candidates=candidates,
            protected=protected,
        )
        self.fault_hook("gc:reachability_enumerated")
        return reachability

    def _plan_locked(
        self,
        operation: LeaseOperation,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        probe_locks: bool,
        deadline_ns: int | None = None,
    ) -> GcPlan:
        reachability = self._reachability_locked(
            operation.repo_uuid,
            protections=protections,
            probe_locks=probe_locks,
            deadline_ns=deadline_ns,
            maximum_generations=GC_PREVIEW_MAX_GENERATIONS,
        )
        plan = GcPlan(
            repo_uuid=operation.repo_uuid,
            registry_revision=int(operation.registry.to_dict()["revision"]),
            active_source_revision=operation.grant.active_source_revision,
            operation_epoch=operation.grant.operation_epoch,
            migration_epoch=operation.grant.migration_epoch,
            fence_token=operation.fence_token,
            pointer_revision=reachability.pointer_revision,
            capacity_policy_sha256=capacity_policy.sha256,
            candidates=reachability.candidates,
            protected=reachability.protected,
        )
        return plan

    def preview(
        self,
        repo_uuid: str,
        *,
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
        expected_pointer_revision: int,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        deadline_ns: int,
    ) -> GcPreview:
        """Return one bounded unfenced preview under existing read-only locks."""

        capacity_policy = self._validated_capacity_policy(capacity_policy)
        expected_pointer_revision = self._validate_expected_revision(
            expected_pointer_revision,
            "expected_pointer_revision",
            minimum=0,
        )
        require_before_deadline(
            deadline_ns,
            "GC preview exceeded its deadline",
        )
        with self.leases.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
            entry = self._registry_entry(registry, repo_uuid)
            with self.leases.read_only_workspace_lock(
                repo_uuid,
                deadline_ns=deadline_ns,
            ):
                lease_state = self.leases.read_only_snapshot_locked(
                    registry,
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
                self._check_preview_authority(
                    registry,
                    entry,
                    lease_state,
                    expected_registry_revision=expected_registry_revision,
                    expected_active_source_revision=expected_active_source_revision,
                    expected_operation_epoch=expected_operation_epoch,
                    expected_migration_epoch=expected_migration_epoch,
                )
                self.leases._assert_recovery_barriers_locked(
                    repo_uuid,
                    "GC",
                    recover=False,
                    deadline_ns=deadline_ns,
                )
                if self.read_only_intent_locked(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                ) is not None:
                    raise GcRecoveryRequired(
                        "an unresolved GC intent must be reconciled"
                    )
                first = self._reachability_locked(
                    repo_uuid,
                    protections=protections,
                    probe_locks=True,
                    inspect_protected_locks=True,
                    deadline_ns=deadline_ns,
                    maximum_generations=GC_PREVIEW_MAX_GENERATIONS,
                )
                if first.pointer_revision != expected_pointer_revision:
                    raise GcPreviewAuthorityConflict(
                        "GC preview pointer revision changed"
                    )
                second = self._reachability_locked(
                    repo_uuid,
                    protections=protections,
                    probe_locks=True,
                    inspect_protected_locks=True,
                    deadline_ns=deadline_ns,
                    maximum_generations=GC_PREVIEW_MAX_GENERATIONS,
                )
                if first.canonical != second.canonical:
                    raise GcPreviewUnstable(
                        "GC reachability changed between read-only observations"
                    )
                require_before_deadline(
                    deadline_ns,
                    "GC preview exceeded its deadline",
                )
                return GcPreview(
                    repo_uuid=repo_uuid,
                    registry_revision=int(registry.to_dict()["revision"]),
                    active_source_revision=int(entry["active_source_revision"]),
                    operation_epoch=lease_state.operation_epoch,
                    migration_epoch=lease_state.migration_epoch,
                    pointer_revision=second.pointer_revision,
                    capacity_policy_sha256=capacity_policy.sha256,
                    candidates=second.candidates,
                    protected=second.protected,
                )

    def preflight_lifecycle(
        self,
        repo_uuid: str,
        *,
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
        expected_pointer_revision: int,
        expected_capacity_policy_sha256: str | None = None,
        plan_sha256: str | None = None,
        deadline_ns: int,
    ) -> GcIntentState | GcCompletionState | GcPurgeState | None:
        """Validate lifecycle CAS and durable selection without acquiring a lease."""

        expected_pointer_revision = self._validate_expected_revision(
            expected_pointer_revision,
            "expected_pointer_revision",
            minimum=0,
        )
        require_before_deadline(
            deadline_ns,
            "GC lifecycle preflight exceeded its deadline",
        )
        with self.leases.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
            entry = self._registry_entry(registry, repo_uuid)
            with self.leases.read_only_workspace_lock(
                repo_uuid,
                deadline_ns=deadline_ns,
            ):
                if plan_sha256 is not None:
                    purge = self._read_purge_state_locked(
                        repo_uuid,
                        plan_sha256,
                        deadline_ns=deadline_ns,
                    )
                    if purge is not None:
                        return purge
                lease_state = self.leases.read_only_snapshot_locked(
                    registry,
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
                self._check_preview_authority(
                    registry,
                    entry,
                    lease_state,
                    expected_registry_revision=expected_registry_revision,
                    expected_active_source_revision=expected_active_source_revision,
                    expected_operation_epoch=expected_operation_epoch,
                    expected_migration_epoch=expected_migration_epoch,
                )
                self.leases._assert_recovery_barriers_locked(
                    repo_uuid,
                    "GC",
                    recover=False,
                    deadline_ns=deadline_ns,
                )
                pointer_revision = self._verified_pointer_revision(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
                if pointer_revision != expected_pointer_revision:
                    raise GcPreviewAuthorityConflict(
                        "GC lifecycle pointer revision changed"
                    )
                intent = self.read_only_intent_locked(
                    repo_uuid,
                    deadline_ns=deadline_ns,
                )
                if (
                    intent is not None
                    and intent.pointer_revision != expected_pointer_revision
                ):
                    raise GcPlanStale(
                        "durable GC intent has another pointer revision"
                    )
                if (
                    intent is not None
                    and expected_capacity_policy_sha256 is not None
                    and intent.capacity_policy_sha256
                    != expected_capacity_policy_sha256
                ):
                    raise GcRecoveryRequired(
                        "capacity policy differs from durable GC intent"
                    )
                if plan_sha256 is None:
                    if intent is not None:
                        return intent
                    return self._read_operation_completion_locked(
                        repo_uuid,
                        expected_operation_epoch,
                        deadline_ns=deadline_ns,
                    )
                if intent is not None:
                    raise GcRecoveryRequired(
                        "GC intent must be reconciled before purge"
                    )
                completion_data = self.state.read_optional_existing_bytes(
                    self._completion_path(repo_uuid, plan_sha256),
                    max_bytes=_MAX_GC_INTENT_BYTES,
                    deadline_ns=deadline_ns,
                )
                if completion_data is None:
                    raise GcPlanStale("GC completion is unavailable for selected plan")
                try:
                    completion = GcCompletionState.from_json(completion_data)
                except Exception as exc:
                    raise GcError(f"GC completion is unavailable: {exc}") from exc
                if (
                    completion.repo_uuid != repo_uuid
                    or completion.plan_sha256 != plan_sha256
                ):
                    raise GcError(
                        "GC completion belongs to another workspace or plan"
                    )
                require_before_deadline(
                    deadline_ns,
                    "GC lifecycle preflight exceeded its deadline",
                )
                return None

    def plan(
        self,
        grant: LeaseGrant,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        monotonic_ns: int,
        deadline_ns: int | None = None,
    ) -> GcPlan:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation_read_only(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC"}),
            deadline_ns=deadline_ns,
        ) as operation:
            if self._read_intent(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            ) is not None:
                raise GcRecoveryRequired("an unresolved GC intent must be reconciled")
            return self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=True,
                deadline_ns=deadline_ns,
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

    def _rename_candidates(
        self,
        intent: GcIntentState,
        *,
        deadline_ns: int | None = None,
    ) -> None:
        for generation_id in intent.candidates:
            require_before_deadline(
                deadline_ns,
                "GC candidate quarantine exceeded its deadline",
            )
            source = self.generations._generation(intent.repo_uuid, generation_id)
            destination = self._quarantine(
                intent.repo_uuid,
                generation_id,
                intent.operation_epoch,
            )
            source_exists = self.state.private_directory_exists(source)
            destination_exists = self.state.private_directory_exists(destination)
            if source_exists == destination_exists:
                raise GcRecoveryRequired(
                    f"GC location is ambiguous for {generation_id}: "
                    f"source={source_exists} quarantine={destination_exists}"
                )
            if source_exists:
                require_before_deadline(
                    deadline_ns,
                    "GC candidate quarantine exceeded its deadline",
                )
                self.state.rename_contained(
                    source,
                    destination,
                    label=f"gc:{generation_id}:quarantine",
                )
                self.fault_hook(f"gc:{generation_id}:quarantined")
            require_before_deadline(
                deadline_ns,
                "GC candidate quarantine exceeded its deadline",
            )

    def _write_completion(
        self,
        intent: GcIntentState,
        completion: GcCompletionState,
        *,
        receipt_operation_epoch: int,
        deadline_ns: int | None = None,
    ) -> None:
        require_before_deadline(
            deadline_ns,
            "GC completion persistence exceeded its deadline",
        )
        self.state.install_once_bytes(
            self._completion_path(intent.repo_uuid, intent.plan_sha256),
            completion.canonical,
            label="gc:completion",
        )
        for operation_epoch in sorted(
            {intent.operation_epoch, receipt_operation_epoch}
        ):
            require_before_deadline(
                deadline_ns,
                "GC completion persistence exceeded its deadline",
            )
            index = GcCompletionIndexState.from_mapping(
                {
                    "completion_sha256": canonical_sha256(completion.to_dict()),
                    "contract": "graphify.workspace.gc_completion_index.internal",
                    "format_version": 1,
                    "operation_epoch": operation_epoch,
                    "plan_sha256": completion.plan_sha256,
                    "repo_uuid": completion.repo_uuid,
                }
            )
            self.state.install_once_bytes(
                self._operation_completion_path(intent.repo_uuid, operation_epoch),
                index.canonical,
                label="gc:completion_epoch",
            )
        require_before_deadline(
            deadline_ns,
            "GC completion persistence exceeded its deadline",
        )
        self.fault_hook("gc:completion_durable")

    def _read_completion(
        self,
        intent: GcIntentState,
        *,
        deadline_ns: int | None = None,
    ) -> GcCompletionState | None:
        relative = self._completion_path(intent.repo_uuid, intent.plan_sha256)
        self.state.cleanup_atomic_temps(
            relative.parent,
            deadline_ns=deadline_ns,
        )
        try:
            data = self.state.read_optional_existing_bytes(
                relative,
                deadline_ns=deadline_ns,
            )
            if data is None:
                return None
            completion = GcCompletionState.from_json(data)
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
        require_before_deadline(
            deadline_ns,
            "GC completion inspection exceeded its deadline",
        )
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
        deadline_ns: int | None = None,
    ) -> GcCompletionState:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC"}),
            deadline_ns=deadline_ns,
        ) as operation:
            self.state.cleanup_atomic_temps(
                self._workspace(operation.repo_uuid) / "gc",
                deadline_ns=deadline_ns,
            )
            if self._read_intent(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            ) is not None:
                raise GcRecoveryRequired("an unresolved GC intent must be reconciled")
            refreshed = self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=True,
                deadline_ns=deadline_ns,
            )
            if refreshed.canonical != plan.canonical:
                raise GcPlanStale("GC dry-run plan no longer matches reachability")
            intent = self._intent(operation, plan, occurred_at=occurred_at)
            completion = self._read_completion(
                intent,
                deadline_ns=deadline_ns,
            )
            require_before_deadline(
                deadline_ns,
                "GC execute exceeded its deadline",
            )
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
            with self.state.existing_generation_locks(
                locks,
                exclusive=True,
                deadline_ns=deadline_ns,
            ):
                self.fault_hook("gc:generation_locks_acquired")
                locked_plan = self._plan_locked(
                    operation,
                    capacity_policy=capacity_policy,
                    protections=protections,
                    probe_locks=False,
                    deadline_ns=deadline_ns,
                )
                if locked_plan.canonical != plan.canonical:
                    raise GcRecoveryRequired("GC reachability changed after durable intent")
                self.fault_hook("gc:reachability_rechecked")
                self._rename_candidates(
                    intent,
                    deadline_ns=deadline_ns,
                )
            if completion is None:
                completion = self._completion(intent, completed_at=occurred_at)
            self._write_completion(
                intent,
                completion,
                receipt_operation_epoch=operation.grant.operation_epoch,
                deadline_ns=deadline_ns,
            )
            self.state.unlink_and_sync(
                self._intent_path(operation.repo_uuid),
                label="gc:intent_clear",
                deadline_ns=deadline_ns,
            )
            self.fault_hook("gc:complete")
            return completion

    def reconcile(
        self,
        grant: LeaseGrant,
        *,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        expected_pointer_revision: int | None = None,
        completed_at: datetime,
        monotonic_ns: int,
        deadline_ns: int | None = None,
    ) -> GcCompletionState | None:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC", "POINTER_RECOVERY"}),
            deadline_ns=deadline_ns,
        ) as operation:
            self.state.cleanup_atomic_temps(
                self._workspace(operation.repo_uuid) / "gc",
                deadline_ns=deadline_ns,
            )
            intent = self._read_intent(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            pointer_revision = self._verified_pointer_revision(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if (
                expected_pointer_revision is not None
                and pointer_revision != expected_pointer_revision
            ):
                raise GcPlanStale("GC reconcile pointer revision is stale")
            if intent is None:
                return None
            if (
                expected_pointer_revision is not None
                and intent.pointer_revision != expected_pointer_revision
            ):
                raise GcPlanStale("durable GC intent has another pointer revision")
            if intent.capacity_policy_sha256 != capacity_policy.sha256:
                raise GcRecoveryRequired("capacity policy differs from durable GC intent")
            refreshed = self._plan_locked(
                operation,
                capacity_policy=capacity_policy,
                protections=protections,
                probe_locks=False,
                deadline_ns=deadline_ns,
            )
            if (
                expected_pointer_revision is not None
                and refreshed.pointer_revision != expected_pointer_revision
            ):
                raise GcPlanStale("GC reconcile reachability has another pointer revision")
            protected = {generation_id for generation_id, _reasons in refreshed.protected}
            if any(generation_id in protected for generation_id in intent.candidates):
                raise GcRecoveryRequired("a durable GC candidate became reachable")
            completion = self._read_completion(
                intent,
                deadline_ns=deadline_ns,
            )
            locks = [
                (
                    generation_id,
                    self.generations._lock(operation.repo_uuid, generation_id),
                )
                for generation_id in intent.candidates
            ]
            with self.state.existing_generation_locks(
                locks,
                exclusive=True,
                deadline_ns=deadline_ns,
            ):
                self._rename_candidates(
                    intent,
                    deadline_ns=deadline_ns,
                )
            if completion is None:
                completion = self._completion(intent, completed_at=completed_at)
            self._write_completion(
                intent,
                completion,
                receipt_operation_epoch=operation.grant.operation_epoch,
                deadline_ns=deadline_ns,
            )
            self.state.unlink_and_sync(
                self._intent_path(operation.repo_uuid),
                label="gc:reconcile_clear",
                deadline_ns=deadline_ns,
            )
            self.fault_hook("gc:reconciled")
            return completion

    def _remove_quarantine(
        self,
        relative: Path,
        *,
        deadline_ns: int | None = None,
    ) -> bool:
        require_before_deadline(
            deadline_ns,
            "GC quarantine purge exceeded its deadline",
        )
        try:
            removed = self.state.remove_private_tree(
                relative,
                allowed_directory_modes=_PURGE_ALLOWED_DIRECTORY_MODES,
                allowed_file_modes=_PURGE_ALLOWED_FILE_MODES,
            )
        except StatePathError as exc:
            raise GcError(f"quarantine path is unsafe: {exc}") from exc
        require_before_deadline(
            deadline_ns,
            "GC quarantine purge exceeded its deadline",
        )
        return removed

    def purge(
        self,
        grant: LeaseGrant,
        *,
        plan_sha256: str,
        capacity_policy: CapacityPolicy,
        protections: GcProtection,
        expected_pointer_revision: int | None = None,
        completed_at: datetime,
        monotonic_ns: int,
        deadline_ns: int | None = None,
    ) -> GcPurgeState:
        capacity_policy = self._validated_capacity_policy(capacity_policy)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"GC"}),
            deadline_ns=deadline_ns,
        ) as operation:
            self.state.cleanup_atomic_temps(
                self._workspace(operation.repo_uuid) / "gc",
                deadline_ns=deadline_ns,
            )
            if self._read_intent(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            ) is not None:
                raise GcRecoveryRequired("GC intent must be reconciled before purge")
            pointer_revision = self._verified_pointer_revision(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            if (
                expected_pointer_revision is not None
                and pointer_revision != expected_pointer_revision
            ):
                raise GcPlanStale("GC purge pointer revision is stale")
            purge_relative = self._purge_path(operation.repo_uuid, plan_sha256)
            try:
                purge = self._read_purge_state_locked(
                    operation.repo_uuid,
                    plan_sha256,
                    deadline_ns=deadline_ns,
                )
            except GcError:
                raise
            except Exception as exc:
                raise GcError(f"GC purge record is invalid: {exc}") from exc
            if purge is not None:
                return purge
            completion_relative = self._completion_path(operation.repo_uuid, plan_sha256)
            self.state.cleanup_atomic_temps(
                completion_relative.parent,
                deadline_ns=deadline_ns,
            )
            try:
                completion = GcCompletionState.from_json(
                    self.state.read_existing_bytes(
                        completion_relative,
                        deadline_ns=deadline_ns,
                    )
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
                deadline_ns=deadline_ns,
            )
            if (
                expected_pointer_revision is not None
                and refreshed.pointer_revision != expected_pointer_revision
            ):
                raise GcPlanStale("GC purge reachability has another pointer revision")
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
            with self.state.existing_generation_locks(
                locks,
                exclusive=True,
                deadline_ns=deadline_ns,
            ):
                for generation_id in completion.quarantined:
                    require_before_deadline(
                        deadline_ns,
                        "GC purge exceeded its deadline",
                    )
                    quarantine = self._quarantine(
                        operation.repo_uuid,
                        generation_id,
                        completion.operation_epoch,
                    )
                    if self._remove_quarantine(
                        quarantine,
                        deadline_ns=deadline_ns,
                    ):
                        self.fault_hook(f"gc:{generation_id}:purged")
                    else:
                        require_before_deadline(
                            deadline_ns,
                            "GC purge exceeded its deadline",
                        )
                        self.state.fsync_directory(quarantine.parent)
                    binding = SemanticQueueStore._certification_binding_path(
                        operation.repo_uuid,
                        generation_id,
                    )
                    self.state.unlink_and_sync(
                        binding,
                        label=f"gc:{generation_id}:semantic_binding",
                        deadline_ns=deadline_ns,
                    )
                    if self.state.private_directory_exists(binding.parent):
                        require_before_deadline(
                            deadline_ns,
                            "GC purge exceeded its deadline",
                        )
                        self.state.fsync_directory(binding.parent)
                        self.fault_hook(
                            f"gc:{generation_id}:semantic_binding_parent_durable"
                        )
                    self.fault_hook(f"gc:{generation_id}:semantic_binding_removed")
            require_before_deadline(
                deadline_ns,
                "GC purge exceeded its deadline",
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
            require_before_deadline(
                deadline_ns,
                "GC purge exceeded its deadline",
            )
            self.state.install_once_bytes(
                purge_relative,
                purge.canonical,
                label="gc:purge",
            )
            require_before_deadline(
                deadline_ns,
                "GC purge exceeded its deadline",
            )
            self.fault_hook("gc:purge_complete")
            return purge


__all__ = [
    "GC_PREVIEW_MAX_GENERATIONS",
    "GcCoordinationUnavailable",
    "GcError",
    "GcPlan",
    "GcPlanStale",
    "GcPreview",
    "GcPreviewAuthorityConflict",
    "GcPreviewUnstable",
    "GcProtection",
    "GcRecoveryRequired",
    "GcStore",
]
