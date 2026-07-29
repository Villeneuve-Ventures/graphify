"""Atomic pointer promotion, rollback, monotonic repair, and shared readers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Iterator, cast

from graphify.workspace.adapters import (
    AdapterIntent,
    CompatibilityTuple,
    UnsupportedCompatibility,
    select_adapter,
)
from graphify.workspace.contracts import (
    STATE_SCHEMA_VERSION,
    CompatibilityManifest,
    GenerationReceipt,
    PointerSet,
    PriorPointerRecord,
    canonical_json_bytes,
)
from graphify.workspace.generations import GenerationError, GenerationStore
from graphify.workspace.journal import (
    JournalError,
    JournalRecoveryProjection,
    JournalSnapshot,
    JournalStore,
)
from graphify.workspace.leases import LeaseGrant, LeaseOperation, LeaseStore
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    LockTimeout,
    RuntimeCapabilities,
    StatePathError,
    Syscalls,
    require_before_deadline,
)


_MAX_POINTER_RECORD_BYTES = 64 * 1024


class PointerError(RuntimeError):
    """Base class for stable pointer failures."""

    code = "pointer_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class PointerConflict(PointerError):
    code = "pointer_conflict"


class PointerSuperseded(PointerConflict):
    code = "pointer_superseded"


class PointerCorrupt(PointerError):
    code = "pointer_corrupt"


class PointerRecoveryRequired(PointerError):
    code = "pointer_recovery_required"


@dataclass(frozen=True)
class PointerCAS:
    expected_pointer_revision: int
    expected_active_source_revision: int
    expected_source_epoch: int
    expected_operation_epoch: int
    expected_migration_epoch: int
    expected_state_schema_version: int
    expected_fence_token: int
    candidate_generation_id: str
    candidate_receipt_sha256: str
    expected_current_receipt_sha256: str | None


@dataclass(frozen=True)
class GenerationRead:
    pointer: PointerSet
    receipt: GenerationReceipt
    generation_path: Path


@dataclass(frozen=True)
class PointerRepairPlan:
    """Bounded public repair decision plus a private exact-evidence binding."""

    classification: str
    candidate: dict[str, str]
    last_good: dict[str, str] | None
    next_pointer_revision: int
    selected_from: str
    pointer_action: str
    journal_actions: tuple[str, ...]
    quarantine: tuple[str, ...]
    _decision_sha256: str = field(repr=False)

    @property
    def decision_sha256(self) -> str:
        return self._decision_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "candidate": dict(self.candidate),
            "last_good": None if self.last_good is None else dict(self.last_good),
            "next_pointer_revision": self.next_pointer_revision,
            "selected_from": self.selected_from,
            "pointer_action": self.pointer_action,
            "journal_actions": list(self.journal_actions),
            "quarantine": list(self.quarantine),
        }


@dataclass(frozen=True)
class _PointerRepairAnalysis:
    plan: PointerRepairPlan
    current: PointerSet | None
    pending: PointerSet | None
    prior: PriorPointerRecord | None
    prior_pointer: PointerSet | None
    candidate: GenerationReceipt
    visible_current: PointerSet | None
    journal: JournalRecoveryProjection


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise PointerError("pointer timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


class PointerStore:
    """Own the only visible pointer replacement and its recovery protocol."""

    def __init__(
        self,
        state_root: Path,
        leases: LeaseStore,
        generations: GenerationStore,
        journal: JournalStore,
        *,
        compatibility_manifest: CompatibilityManifest,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        compatibility = CompatibilityTuple.from_manifest(compatibility_manifest)
        select_adapter(compatibility, intent=AdapterIntent.PROMOTE).require_adapter()
        self.compatibility_sha256 = compatibility_manifest.sha256
        if generations.compatibility_sha256 != self.compatibility_sha256:
            raise UnsupportedCompatibility(
                "pointer and generation stores require the same compatibility manifest"
            )
        self.leases = leases
        self.generations = generations
        self.journal = journal
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        roots = {self.state.root, leases.state.root, generations.state.root, journal.state.root}
        if len(roots) != 1:
            raise PointerError("pointer dependencies must share one external state root")
        self.fault_hook = fault_hook or (lambda _event: None)

    @staticmethod
    def _workspace(repo_uuid: str) -> Path:
        return LeaseStore._directory(repo_uuid)

    @classmethod
    def _current(cls, repo_uuid: str) -> Path:
        return cls._workspace(repo_uuid) / "pointers.json"

    @classmethod
    def _prior(cls, repo_uuid: str) -> Path:
        return cls._workspace(repo_uuid) / "pointers.previous.json"

    @classmethod
    def _pending(cls, repo_uuid: str) -> Path:
        return cls._workspace(repo_uuid) / "pointers.pending.json"

    @classmethod
    def _gc_intent(cls, repo_uuid: str) -> Path:
        return cls._workspace(repo_uuid) / "gc" / "intent.json"

    def _exists(
        self,
        relative: Path,
        *,
        deadline_ns: int | None = None,
    ) -> bool:
        try:
            require_before_deadline(
                deadline_ns,
                "pointer state inspection exceeded its deadline",
            )
            result = self.state.private_file_exists(relative)
            require_before_deadline(
                deadline_ns,
                "pointer state inspection exceeded its deadline",
            )
            return result
        except StatePathError as exc:
            raise PointerCorrupt(f"pointer state path is unsafe: {relative}") from exc

    def _read_pointer(
        self,
        relative: Path,
        *,
        allow_missing: bool,
        expected_repo_uuid: str | None = None,
        deadline_ns: int | None = None,
    ) -> PointerSet | None:
        if not self._exists(relative, deadline_ns=deadline_ns):
            if allow_missing:
                return None
            raise PointerCorrupt(f"pointer record is missing: {relative}")
        try:
            pointer = cast(
                PointerSet,
                PointerSet.from_json(
                    self.state.read_existing_bytes(
                        relative,
                        max_bytes=_MAX_POINTER_RECORD_BYTES,
                        deadline_ns=deadline_ns,
                    )
                ),
            )
        except LockTimeout:
            raise
        except Exception as exc:
            raise PointerCorrupt(f"pointer record is invalid: {relative}: {exc}") from exc
        if (
            expected_repo_uuid is not None
            and pointer.to_dict()["repo_uuid"] != expected_repo_uuid
        ):
            raise PointerCorrupt(
                f"pointer record belongs to another workspace: {relative}"
            )
        return pointer

    def retained_prior(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> PriorPointerRecord | None:
        relative = self._prior(repo_uuid)
        if not self._exists(relative, deadline_ns=deadline_ns):
            return None
        try:
            prior = cast(
                PriorPointerRecord,
                PriorPointerRecord.from_json(
                    self.state.read_existing_bytes(
                        relative,
                        max_bytes=_MAX_POINTER_RECORD_BYTES,
                        deadline_ns=deadline_ns,
                    )
                ),
            )
        except LockTimeout:
            raise
        except Exception as exc:
            raise PointerCorrupt(f"prior pointer record is invalid: {exc}") from exc
        if prior.to_dict()["pointer_set"]["repo_uuid"] != repo_uuid:
            raise PointerCorrupt("prior pointer record belongs to another workspace")
        return prior

    def _assert_no_gc_intent(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> None:
        if self._exists(self._gc_intent(repo_uuid), deadline_ns=deadline_ns):
            raise PointerRecoveryRequired("unresolved GC intent blocks pointer mutation")

    @staticmethod
    def _validate_pending_relationship(
        current: PointerSet | None,
        pending: PointerSet | None,
        prior: PriorPointerRecord | None,
    ) -> None:
        if pending is None:
            return
        pending_revision = int(pending.to_dict()["pointer_revision"])
        if current is not None and current.canonical != pending.canonical:
            current_revision = int(current.to_dict()["pointer_revision"])
            if pending_revision <= current_revision:
                raise PointerCorrupt("pending pointer is stale relative to visible current")
            if prior is None:
                raise PointerCorrupt("pending pointer has no retained prior binding")
        if prior is None:
            if pending_revision != 1:
                raise PointerCorrupt("noninitial pending pointer has no retained prior binding")
            return
        prior_value = prior.to_dict()
        replaced_by_revision = int(prior_value["replaced_by_revision"])
        pending_is_visible = current is not None and current.canonical == pending.canonical
        prior_pointer = cast(
            PointerSet,
            PointerSet.from_mapping(prior_value["pointer_set"]),
        )
        retained_pending_before_replacement = (
            current is None
            and prior_pointer.canonical == pending.canonical
            and replaced_by_revision > pending_revision
        )
        if (
            not pending_is_visible
            and replaced_by_revision != pending_revision
            and not retained_pending_before_replacement
        ) or (pending_is_visible and replaced_by_revision < pending_revision):
            raise PointerCorrupt("pending pointer revision does not match retained prior")
        if int(prior_pointer.to_dict()["pointer_revision"]) >= replaced_by_revision:
            raise PointerCorrupt("retained prior is not older than pending pointer")
        if (
            current is not None
            and current.canonical != pending.canonical
            and prior_pointer.canonical != current.canonical
        ):
            current_value = current.to_dict()
            pending_value = pending.to_dict()
            if (
                current_value["current"] != pending_value["current"]
                or current_value["last_good"] != pending_value["last_good"]
            ):
                raise PointerCorrupt("pending pointer is not based on visible current")

    def load(
        self,
        repo_uuid: str,
        *,
        allow_missing: bool = False,
        deadline_ns: int | None = None,
    ) -> PointerSet | None:
        """Read one visible pointer without recovery or any mutating syscall."""

        if self._exists(self._pending(repo_uuid)):
            raise PointerRecoveryRequired("a durable pointer intent requires fenced recovery")
        return self._read_pointer(
            self._current(repo_uuid),
            allow_missing=allow_missing,
            expected_repo_uuid=repo_uuid,
            deadline_ns=deadline_ns,
        )

    @staticmethod
    def _ref(receipt: GenerationReceipt) -> dict[str, str]:
        value = receipt.to_dict()
        return {
            "generation_id": str(value["generation_id"]),
            "receipt_sha256": receipt.sha256,
        }

    def _require_compatible(self, receipt: GenerationReceipt) -> GenerationReceipt:
        if receipt.to_dict()["compatibility_sha256"] != self.compatibility_sha256:
            raise UnsupportedCompatibility(
                "pointer receipt does not match the selected compatibility manifest"
            )
        return receipt

    def _verify_generation(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        deadline_ns: int | None = None,
    ) -> GenerationReceipt:
        try:
            receipt = self.generations.verify_generation(
                repo_uuid,
                generation_id,
                deadline_ns=deadline_ns,
            )
        except UnsupportedCompatibility as exc:
            raise UnsupportedCompatibility(
                "pointer receipt does not match the selected compatibility manifest"
            ) from exc
        return self._require_compatible(receipt)

    def _verify_ref(
        self,
        repo_uuid: str,
        reference: dict[str, Any],
        *,
        deadline_ns: int | None = None,
    ) -> GenerationReceipt:
        generation_id = str(reference["generation_id"])
        receipt = self._verify_generation(
            repo_uuid,
            generation_id,
            deadline_ns=deadline_ns,
        )
        if receipt.sha256 != reference["receipt_sha256"]:
            raise PointerCorrupt(f"pointer receipt hash is stale for {generation_id}")
        return receipt

    def verify_pointer(
        self,
        pointer: PointerSet,
        *,
        expected_repo_uuid: str | None = None,
        deadline_ns: int | None = None,
    ) -> dict[str, GenerationReceipt]:
        value = pointer.to_dict()
        repo_uuid = str(value["repo_uuid"])
        if expected_repo_uuid is not None and repo_uuid != expected_repo_uuid:
            raise PointerCorrupt("pointer belongs to another workspace")
        result = {
            "current": self._verify_ref(
                repo_uuid,
                cast(dict[str, Any], value["current"]),
                deadline_ns=deadline_ns,
            )
        }
        if value["last_good"] is not None:
            result["last_good"] = self._verify_ref(
                repo_uuid,
                cast(dict[str, Any], value["last_good"]),
                deadline_ns=deadline_ns,
            )
        current_value = result["current"].to_dict()
        if (
            int(value["active_source_revision"])
            != int(current_value["active_source_revision"])
            or int(value["source_epoch"]) != int(current_value["source_epoch"])
        ):
            raise PointerCorrupt("pointer source authority does not match its current receipt")
        return result

    def verify_visible_pointer(
        self,
        pointer: PointerSet,
        *,
        expected_repo_uuid: str | None = None,
        deadline_ns: int | None = None,
    ) -> dict[str, GenerationReceipt]:
        """Verify generation receipts and the journal authority for a visible pointer."""

        value = pointer.to_dict()
        repo_uuid = str(value["repo_uuid"])
        if expected_repo_uuid is not None and repo_uuid != expected_repo_uuid:
            raise PointerCorrupt("pointer belongs to another workspace")
        self._verify_visible_pointer_journal(
            repo_uuid,
            pointer,
            deadline_ns=deadline_ns,
        )
        return self.verify_pointer(
            pointer,
            expected_repo_uuid=expected_repo_uuid,
            deadline_ns=deadline_ns,
        )

    def _verify_repair_refs(
        self,
        repo_uuid: str,
        pointer: PointerSet,
        *,
        deadline_ns: int | None = None,
    ) -> tuple[dict[str, GenerationReceipt], set[str]]:
        value = pointer.to_dict()
        receipts: dict[str, GenerationReceipt] = {}
        corrupt_generations: set[str] = set()
        for name in ("current", "last_good"):
            reference = value[name]
            if reference is None:
                continue
            ref = cast(dict[str, Any], reference)
            generation_id = str(ref["generation_id"])
            generation_path = self.state.path(
                self.generations._generation(repo_uuid, generation_id)
            )
            if not generation_path.exists():
                continue
            try:
                receipt = self._verify_generation(
                    repo_uuid,
                    generation_id,
                    deadline_ns=deadline_ns,
                )
            except GenerationError:
                corrupt_generations.add(generation_id)
                continue
            if receipt.sha256 == ref["receipt_sha256"]:
                receipts[name] = receipt
        if "current" in receipts:
            current_value = receipts["current"].to_dict()
            if (
                int(value["active_source_revision"])
                != int(current_value["active_source_revision"])
                or int(value["source_epoch"]) != int(current_value["source_epoch"])
            ):
                del receipts["current"]
        return receipts, corrupt_generations

    @staticmethod
    def _journal_certifies(
        snapshot: JournalSnapshot,
        *,
        generation_id: str,
        receipt_sha256: str,
        allow_superseded: bool = False,
    ) -> bool:
        certified = False
        forbidden = False
        for event in snapshot.for_generation(generation_id):
            value = event.to_dict()
            if value["transition"] == "CERTIFIED" and value["receipt_sha256"] == receipt_sha256:
                certified = True
            if value["transition"] == "FAILED" or (
                value["transition"] == "SUPERSEDED" and not allow_superseded
            ):
                forbidden = True
        return certified and not forbidden

    @staticmethod
    def _journal_records_pointer(
        snapshot: JournalSnapshot,
        pointer: PointerSet,
        *,
        transition: str,
        deadline_ns: int | None = None,
    ) -> bool:
        value = pointer.to_dict()
        current = cast(dict[str, Any], value["current"])
        for event in snapshot.events:
            require_before_deadline(
                deadline_ns,
                "visible pointer journal verification exceeded its deadline",
            )
            event_value = event.to_dict()
            if (
                event_value["transition"] == transition
                and event_value["generation_id"] == current["generation_id"]
                and event_value["receipt_sha256"] == current["receipt_sha256"]
                and event_value["pointer_revision"] == value["pointer_revision"]
                and event_value["operation_epoch"] == value["operation_epoch"]
                and event_value["fence_token"] == value["fence_token"]
            ):
                return True
        return False

    def _verify_visible_pointer_journal(
        self,
        repo_uuid: str,
        pointer: PointerSet,
        *,
        deadline_ns: int | None = None,
    ) -> None:
        try:
            snapshot = self.journal.read_stable(
                repo_uuid,
                deadline_ns=deadline_ns,
            )
        except JournalError as exc:
            raise PointerCorrupt(
                f"visible pointer journal authority is unavailable: {exc}"
            ) from exc
        pointer_revision = int(pointer.to_dict()["pointer_revision"])
        durable_pointer_revisions: list[int] = []
        for event in snapshot.events:
            require_before_deadline(
                deadline_ns,
                "visible pointer journal verification exceeded its deadline",
            )
            value = event.to_dict()
            if value["pointer_revision"] is not None:
                durable_pointer_revisions.append(int(value["pointer_revision"]))
        if durable_pointer_revisions and max(durable_pointer_revisions) > pointer_revision:
            raise PointerCorrupt("visible pointer is stale relative to durable journal history")
        if not any(
            self._journal_records_pointer(
                snapshot,
                pointer,
                transition=transition,
                deadline_ns=deadline_ns,
            )
            for transition in ("PROMOTED", "ROLLED_BACK", "REPAIRED")
        ):
            raise PointerCorrupt("visible pointer has no matching durable journal event")

    def _preliminary_pointer(self, repo_uuid: str) -> PointerSet | None:
        if self._exists(self._pending(repo_uuid)):
            raise PointerRecoveryRequired("a durable pointer intent requires fenced recovery")
        return self._read_pointer(
            self._current(repo_uuid),
            allow_missing=True,
            expected_repo_uuid=repo_uuid,
        )

    def _lock_set(
        self,
        repo_uuid: str,
        candidate_generation_id: str,
        preliminary: PointerSet | None,
    ) -> list[tuple[str, Path]]:
        generation_ids = {candidate_generation_id}
        if preliminary is not None:
            preliminary_value = preliminary.to_dict()
            current = cast(dict[str, Any], preliminary_value["current"])
            generation_ids.add(str(current["generation_id"]))
            if preliminary_value["last_good"] is not None:
                last_good = cast(dict[str, Any], preliminary_value["last_good"])
                generation_ids.add(str(last_good["generation_id"]))
        return [
            (generation_id, self.generations._lock(repo_uuid, generation_id))
            for generation_id in sorted(generation_ids)
        ]

    def _validate_cas(
        self,
        operation: LeaseOperation,
        cas: PointerCAS,
        current: PointerSet | None,
        candidate: GenerationReceipt,
    ) -> None:
        current_value = None if current is None else current.to_dict()
        current_revision = 0 if current_value is None else int(current_value["pointer_revision"])
        current_receipt = (
            None
            if current_value is None
            else cast(dict[str, Any], current_value["current"])["receipt_sha256"]
        )
        if cas.expected_pointer_revision != current_revision:
            raise PointerSuperseded(
                f"pointer revision expected {cas.expected_pointer_revision}, found {current_revision}"
            )
        expected = (
            cas.expected_active_source_revision,
            cas.expected_operation_epoch,
            cas.expected_migration_epoch,
            cas.expected_state_schema_version,
            cas.expected_fence_token,
            cas.expected_source_epoch,
            cas.candidate_generation_id,
            cas.candidate_receipt_sha256,
            cas.expected_current_receipt_sha256,
        )
        candidate_value = candidate.to_dict()
        actual = (
            operation.grant.active_source_revision,
            operation.grant.operation_epoch,
            operation.grant.migration_epoch,
            STATE_SCHEMA_VERSION,
            operation.fence_token,
            int(candidate_value["source_epoch"]),
            str(candidate_value["generation_id"]),
            candidate.sha256,
            current_receipt,
        )
        if expected != actual:
            raise PointerConflict(
                "pointer CAS source/operation/schema/fence/receipt tuple is stale"
            )
        if int(candidate_value["active_source_revision"]) != operation.grant.active_source_revision:
            raise PointerConflict("candidate receipt was certified for another active source")

    def _pointer_document(
        self,
        operation: LeaseOperation,
        *,
        revision: int,
        candidate: GenerationReceipt,
        last_good: dict[str, str] | None,
    ) -> PointerSet:
        candidate_value = candidate.to_dict()
        return cast(
            PointerSet,
            PointerSet.from_mapping(
                {
                    "contract": "graphify.workspace.pointer_set",
                    "schema_version": 1,
                    "repo_uuid": operation.repo_uuid,
                    "pointer_revision": revision,
                    "active_source_revision": operation.grant.active_source_revision,
                    "source_epoch": candidate_value["source_epoch"],
                    "operation_epoch": operation.grant.operation_epoch,
                    "fence_token": operation.fence_token,
                    "state_schema_version": STATE_SCHEMA_VERSION,
                    "current": self._ref(candidate),
                    "last_good": last_good,
                }
            ),
        )

    def _retain_prior(
        self,
        repo_uuid: str,
        current: PointerSet,
        *,
        replaced_by_revision: int,
        retained_at: datetime,
        label: str,
        deadline_ns: int | None = None,
    ) -> None:
        prior = cast(
            PriorPointerRecord,
            PriorPointerRecord.from_mapping(
                {
                    "contract": "graphify.workspace.prior_pointer",
                    "schema_version": 1,
                    "retained_at": _timestamp(retained_at),
                    "replaced_by_revision": replaced_by_revision,
                    "pointer_set": current.to_dict(),
                }
            ),
        )
        self.state.atomic_replace_bytes(
            self._prior(repo_uuid),
            prior.canonical,
            label=label,
            deadline_ns=deadline_ns,
        )

    def _quarantine_corrupt(
        self,
        repo_uuid: str,
        generation_id: str,
        *,
        revision: int,
        deadline_ns: int | None = None,
    ) -> None:
        source = self.generations._generation(repo_uuid, generation_id)
        source_path = self.state.path(source)
        if not source_path.exists():
            return
        destination = (
            self._workspace(repo_uuid) / "quarantine" / "corrupt" / f"{generation_id}.{revision}"
        )
        self.state.rename_contained(
            source,
            destination,
            label=f"pointer:quarantine:{generation_id}",
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"pointer:{generation_id}:quarantined")

    def _persist_move(
        self,
        operation: LeaseOperation,
        *,
        current: PointerSet | None,
        candidate: GenerationReceipt,
        pointer: PointerSet,
        transition: str,
        occurred_at: datetime,
        corrupt_generations: tuple[str, ...],
        deadline_ns: int | None = None,
    ) -> PointerSet:
        label = transition.lower()
        if current is not None:
            self._retain_prior(
                operation.repo_uuid,
                current,
                replaced_by_revision=int(pointer.to_dict()["pointer_revision"]),
                retained_at=occurred_at,
                label=f"pointer:{label}:prior",
                deadline_ns=deadline_ns,
            )
            self.fault_hook(f"pointer:{label}:prior_durable")
        self.state.atomic_replace_bytes(
            self._pending(operation.repo_uuid),
            pointer.canonical,
            label=f"pointer:{label}:pending",
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"pointer:{label}:pending_durable")
        self.state.atomic_replace_bytes(
            self._current(operation.repo_uuid),
            pointer.canonical,
            label=f"pointer:{label}:visible",
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"pointer:{label}:visible")
        pointer_revision = int(pointer.to_dict()["pointer_revision"])
        self.journal.append_pointer_locked(
            operation,
            transition=transition,
            generation_id=str(candidate.to_dict()["generation_id"]),
            receipt_sha256=candidate.sha256,
            pointer_revision=pointer_revision,
            occurred_at=occurred_at,
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"pointer:{label}:journal_durable")
        for generation_id in corrupt_generations:
            self._quarantine_corrupt(
                operation.repo_uuid,
                generation_id,
                revision=pointer_revision,
                deadline_ns=deadline_ns,
            )
        self.state.unlink_and_sync(
            self._pending(operation.repo_uuid),
            label=f"pointer:{label}:complete",
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"pointer:{label}:complete")
        return pointer

    def _move(
        self,
        grant: LeaseGrant,
        cas: PointerCAS,
        *,
        transition: str,
        allowed_operation: str,
        occurred_at: datetime,
        monotonic_ns: int,
        deadline_ns: int | None = None,
    ) -> PointerSet:
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({allowed_operation}),
            deadline_ns=deadline_ns,
        ) as operation:
            self._assert_no_gc_intent(operation.repo_uuid)
            preliminary = self._preliminary_pointer(operation.repo_uuid)
            locks = self._lock_set(
                operation.repo_uuid,
                cas.candidate_generation_id,
                preliminary,
            )
            with self.state.existing_generation_locks(
                locks,
                exclusive=True,
                deadline_ns=deadline_ns,
            ):
                require_before_deadline(
                    deadline_ns,
                    "pointer movement exceeded its lease deadline",
                )
                current = self._preliminary_pointer(operation.repo_uuid)
                candidate = self._verify_generation(
                    operation.repo_uuid,
                    cas.candidate_generation_id,
                    deadline_ns=deadline_ns,
                )
                require_before_deadline(
                    deadline_ns,
                    "pointer movement exceeded its lease deadline",
                )
                self.state.cleanup_atomic_temps(
                    self._workspace(operation.repo_uuid),
                    deadline_ns=deadline_ns,
                )
                candidate_is_current = False
                candidate_is_last_good = False
                if current is not None:
                    current_value = current.to_dict()
                    current_ref = cast(dict[str, Any], current_value["current"])
                    candidate_is_current = (
                        current_ref["generation_id"] == cas.candidate_generation_id
                        and current_ref["receipt_sha256"] == candidate.sha256
                    )
                    last_good = current_value["last_good"]
                    if last_good is not None:
                        last_good_ref = cast(dict[str, Any], last_good)
                        candidate_is_last_good = (
                            transition == "ROLLED_BACK"
                            and last_good_ref["generation_id"]
                            == cas.candidate_generation_id
                            and last_good_ref["receipt_sha256"] == candidate.sha256
                        )
                snapshot = self.journal.recover_locked(
                    operation,
                    deadline_ns=deadline_ns,
                )
                if transition == "ROLLED_BACK" and current is not None:
                    self._verify_visible_pointer_journal(
                        operation.repo_uuid,
                        current,
                        deadline_ns=deadline_ns,
                    )
                if not self._journal_certifies(
                    snapshot,
                    generation_id=cas.candidate_generation_id,
                    receipt_sha256=candidate.sha256,
                    allow_superseded=candidate_is_last_good,
                ):
                    raise PointerConflict("candidate is not eligible for pointer movement")
                try:
                    self._validate_cas(operation, cas, current, candidate)
                except PointerSuperseded:
                    if current is None or candidate_is_current:
                        raise
                    require_before_deadline(
                        deadline_ns,
                        "pointer movement exceeded its lease deadline",
                    )
                    pointer_revision = int(current.to_dict()["pointer_revision"])
                    self.journal.append_pointer_locked(
                        operation,
                        transition="SUPERSEDED",
                        generation_id=cas.candidate_generation_id,
                        receipt_sha256=candidate.sha256,
                        pointer_revision=pointer_revision,
                        occurred_at=occurred_at,
                        deadline_ns=deadline_ns,
                    )
                    raise
                if candidate_is_current:
                    return cast(PointerSet, current)
                corrupt_generations: set[str] = set()
                last_good = None
                if current is not None:
                    current_value = current.to_dict()
                    current_ref = cast(dict[str, Any], current_value["current"])
                    try:
                        old_receipt = self._verify_ref(
                            operation.repo_uuid,
                            current_ref,
                            deadline_ns=deadline_ns,
                        )
                    except (GenerationError, PointerError):
                        corrupt_generations.add(str(current_ref["generation_id"]))
                        if current_value["last_good"] is not None:
                            last_good_ref = cast(dict[str, Any], current_value["last_good"])
                            try:
                                verified_last_good = self._verify_ref(
                                    operation.repo_uuid,
                                    last_good_ref,
                                    deadline_ns=deadline_ns,
                                )
                            except (GenerationError, PointerError):
                                corrupt_generations.add(str(last_good_ref["generation_id"]))
                            else:
                                last_good = self._ref(verified_last_good)
                    else:
                        last_good = self._ref(old_receipt)
                revision = 1 if current is None else int(current.to_dict()["pointer_revision"]) + 1
                pointer = self._pointer_document(
                    operation,
                    revision=revision,
                    candidate=candidate,
                    last_good=last_good,
                )
                require_before_deadline(
                    deadline_ns,
                    "pointer movement exceeded its lease deadline",
                )
                return self._persist_move(
                    operation,
                    current=current,
                    candidate=candidate,
                    pointer=pointer,
                    transition=transition,
                    occurred_at=occurred_at,
                    corrupt_generations=tuple(sorted(corrupt_generations)),
                    deadline_ns=deadline_ns,
                )

    def promote(
        self,
        grant: LeaseGrant,
        cas: PointerCAS,
        *,
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> PointerSet:
        return self._move(
            grant,
            cas,
            transition="PROMOTED",
            allowed_operation="PROMOTE",
            occurred_at=occurred_at,
            monotonic_ns=monotonic_ns,
        )

    def rollback(
        self,
        grant: LeaseGrant,
        cas: PointerCAS,
        *,
        occurred_at: datetime,
        monotonic_ns: int,
        deadline_ns: int | None = None,
    ) -> PointerSet:
        return self._move(
            grant,
            cas,
            transition="ROLLED_BACK",
            allowed_operation="ROLLBACK",
            occurred_at=occurred_at,
            monotonic_ns=monotonic_ns,
            deadline_ns=deadline_ns,
        )

    @staticmethod
    def _pointer_refs(pointer: PointerSet | None) -> set[str]:
        if pointer is None:
            return set()
        value = pointer.to_dict()
        result = {str(cast(dict[str, Any], value["current"])["generation_id"])}
        if value["last_good"] is not None:
            result.add(str(cast(dict[str, Any], value["last_good"])["generation_id"]))
        return result

    def _read_repair_pointer(
        self,
        relative: Path,
        *,
        repo_uuid: str,
        allow_missing: bool,
        allow_invalid: bool,
        deadline_ns: int | None,
    ) -> tuple[PointerSet | None, str | None]:
        try:
            require_before_deadline(
                deadline_ns,
                "pointer repair record inspection exceeded its deadline",
            )
            exists = self.state.private_file_exists(relative)
            require_before_deadline(
                deadline_ns,
                "pointer repair record inspection exceeded its deadline",
            )
        except (LockTimeout, StatePathError):
            raise
        except Exception as exc:
            raise PointerCorrupt(f"pointer record cannot be read safely: {relative}") from exc
        if not exists:
            if allow_missing:
                return None, None
            raise PointerCorrupt(f"pointer record is missing: {relative}")
        try:
            data = self.state.read_existing_bytes(
                relative,
                max_bytes=_MAX_POINTER_RECORD_BYTES,
                deadline_ns=deadline_ns,
            )
        except (LockTimeout, StatePathError):
            raise
        except Exception as exc:
            raise PointerCorrupt(f"pointer record cannot be read safely: {relative}") from exc
        digest = hashlib.sha256(data).hexdigest()
        try:
            pointer = cast(PointerSet, PointerSet.from_json(data))
            if pointer.to_dict()["repo_uuid"] != repo_uuid:
                raise PointerCorrupt(f"pointer record belongs to another workspace: {relative}")
        except Exception as exc:
            if allow_invalid:
                return None, digest
            if isinstance(exc, PointerCorrupt):
                raise
            raise PointerCorrupt(f"pointer record is invalid: {relative}") from exc
        return pointer, digest

    def _read_repair_prior(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None,
    ) -> tuple[PriorPointerRecord | None, str | None]:
        relative = self._prior(repo_uuid)
        try:
            require_before_deadline(
                deadline_ns,
                "prior pointer repair record inspection exceeded its deadline",
            )
            exists = self.state.private_file_exists(relative)
            require_before_deadline(
                deadline_ns,
                "prior pointer repair record inspection exceeded its deadline",
            )
        except (LockTimeout, StatePathError):
            raise
        except Exception as exc:
            raise PointerCorrupt("prior pointer record cannot be read safely") from exc
        if not exists:
            return None, None
        try:
            data = self.state.read_existing_bytes(
                relative,
                max_bytes=_MAX_POINTER_RECORD_BYTES,
                deadline_ns=deadline_ns,
            )
        except (LockTimeout, StatePathError):
            raise
        except Exception as exc:
            raise PointerCorrupt("prior pointer record cannot be read safely") from exc
        digest = hashlib.sha256(data).hexdigest()
        try:
            prior = cast(PriorPointerRecord, PriorPointerRecord.from_json(data))
        except Exception as exc:
            raise PointerCorrupt("prior pointer record is invalid") from exc
        if prior.to_dict()["pointer_set"]["repo_uuid"] != repo_uuid:
            raise PointerCorrupt("prior pointer record belongs to another workspace")
        return prior, digest

    @staticmethod
    def _fully_verified(
        pointer: PointerSet,
        receipts: dict[str, GenerationReceipt],
    ) -> bool:
        required = {"current"}
        if pointer.to_dict()["last_good"] is not None:
            required.add("last_good")
        return required == set(receipts)

    @staticmethod
    def _journal_projection_evidence(
        projection: JournalRecoveryProjection,
    ) -> dict[str, Any]:
        head = projection.snapshot.head
        return {
            "actions": list(projection.actions),
            "evidence_sha256": projection.evidence_sha256,
            "head_sha256": (None if head is None else hashlib.sha256(head.canonical).hexdigest()),
            "event_sha256": [event.sha256 for event in projection.snapshot.events],
        }

    @staticmethod
    def _plan_document(
        *,
        classification: str,
        candidate: dict[str, str],
        last_good: dict[str, str] | None,
        next_pointer_revision: int,
        selected_from: str,
        pointer_action: str,
        journal_actions: tuple[str, ...],
        quarantine: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "classification": classification,
            "candidate": dict(candidate),
            "last_good": None if last_good is None else dict(last_good),
            "next_pointer_revision": next_pointer_revision,
            "selected_from": selected_from,
            "pointer_action": pointer_action,
            "journal_actions": list(journal_actions),
            "quarantine": list(quarantine),
        }

    def _repair_plan(
        self,
        *,
        classification: str,
        candidate: dict[str, str],
        last_good: dict[str, str] | None,
        next_pointer_revision: int,
        selected_from: str,
        pointer_action: str,
        journal_actions: tuple[str, ...],
        quarantine: tuple[str, ...],
        evidence: dict[str, Any],
    ) -> PointerRepairPlan:
        public = self._plan_document(
            classification=classification,
            candidate=candidate,
            last_good=last_good,
            next_pointer_revision=next_pointer_revision,
            selected_from=selected_from,
            pointer_action=pointer_action,
            journal_actions=journal_actions,
            quarantine=quarantine,
        )
        decision_sha256 = hashlib.sha256(
            canonical_json_bytes({"evidence": evidence, "plan": public})
        ).hexdigest()
        return PointerRepairPlan(
            classification=classification,
            candidate=dict(candidate),
            last_good=None if last_good is None else dict(last_good),
            next_pointer_revision=next_pointer_revision,
            selected_from=selected_from,
            pointer_action=pointer_action,
            journal_actions=journal_actions,
            quarantine=quarantine,
            _decision_sha256=decision_sha256,
        )

    def _derive_repair_analysis(
        self,
        repo_uuid: str,
        *,
        active_source_revision: int,
        operation_epoch: int | None,
        fence_token: int | None,
        current: PointerSet | None,
        pending: PointerSet | None,
        prior: PriorPointerRecord | None,
        raw_evidence: dict[str, str | None],
        allow_atomic_temps: bool,
        deadline_ns: int | None,
    ) -> _PointerRepairAnalysis:
        prior_pointer = (
            None
            if prior is None
            else cast(PointerSet, PointerSet.from_mapping(prior.to_dict()["pointer_set"]))
        )
        self._validate_pending_relationship(current, pending, prior)
        journal = self.journal.project_recovery(
            repo_uuid,
            allow_atomic_temps=allow_atomic_temps,
            deadline_ns=deadline_ns,
        )
        snapshot = journal.snapshot
        valid: list[tuple[str, PointerSet, dict[str, GenerationReceipt]]] = []
        verified_by_name: dict[str, dict[str, GenerationReceipt]] = {}
        corrupt_generations: set[str] = set()
        for name, pointer in (
            ("current", current),
            ("pending", pending),
            ("prior", prior_pointer),
        ):
            require_before_deadline(
                deadline_ns,
                "pointer repair analysis exceeded its deadline",
            )
            if pointer is None:
                continue
            receipts, corrupt = self._verify_repair_refs(
                repo_uuid,
                pointer,
                deadline_ns=deadline_ns,
            )
            verified_by_name[name] = receipts
            corrupt_generations.update(corrupt)
            current_receipt = receipts.get("current")
            if (
                current_receipt is not None
                and int(current_receipt.to_dict()["active_source_revision"])
                == active_source_revision
            ):
                valid.append((name, pointer, receipts))

        by_name = {name: (pointer, receipts) for name, pointer, receipts in valid}
        journal_revisions = [
            int(event.to_dict()["pointer_revision"])
            for event in snapshot.events
            if event.to_dict()["pointer_revision"] is not None
        ]
        if pending is None and current is not None and "current" in by_name:
            current_receipts = by_name["current"][1]
            current_receipt = current_receipts["current"]
            current_revision = int(current.to_dict()["pointer_revision"])
            prior_revision = 0 if prior is None else int(prior.to_dict()["replaced_by_revision"])
            visible_event_matches = any(
                self._journal_records_pointer(
                    snapshot,
                    current,
                    transition=transition,
                    deadline_ns=deadline_ns,
                )
                for transition in ("PROMOTED", "ROLLED_BACK", "REPAIRED")
            )
            if journal_revisions and max(journal_revisions) > current_revision:
                raise PointerCorrupt("visible pointer is stale relative to durable journal history")
            if (
                self._fully_verified(current, current_receipts)
                and visible_event_matches
                and prior_revision <= current_revision
                and self._journal_certifies(
                    snapshot,
                    generation_id=str(current_receipt.to_dict()["generation_id"]),
                    receipt_sha256=current_receipt.sha256,
                )
            ):
                last_good_receipt = current_receipts.get("last_good")
                last_good = None if last_good_receipt is None else self._ref(last_good_receipt)
                public_candidate = self._ref(current_receipt)
                actions = journal.actions
                classification = "no_op" if not actions else "repairable"
                evidence = {
                    "active_source_revision": active_source_revision,
                    "journal": self._journal_projection_evidence(journal),
                    "pointers": raw_evidence,
                    "verified_receipts": {
                        name: {key: receipt.sha256 for key, receipt in sorted(receipts.items())}
                        for name, receipts in sorted(verified_by_name.items())
                    },
                }
                plan = self._repair_plan(
                    classification=classification,
                    candidate=public_candidate,
                    last_good=last_good,
                    next_pointer_revision=current_revision,
                    selected_from="current",
                    pointer_action="none",
                    journal_actions=actions,
                    quarantine=(),
                    evidence=evidence,
                )
                return _PointerRepairAnalysis(
                    plan=plan,
                    current=current,
                    pending=pending,
                    prior=prior,
                    prior_pointer=prior_pointer,
                    candidate=current_receipt,
                    visible_current=current,
                    journal=journal,
                )

        if not valid:
            raise PointerCorrupt("no fully verified pointer source can be repaired")
        if "pending" in by_name:
            chosen_name = "pending"
        elif "current" in by_name:
            chosen_name = "current"
        else:
            chosen_name = "prior"
        chosen_pointer, chosen_receipts = by_name[chosen_name]
        candidate = chosen_receipts["current"]
        if not self._journal_certifies(
            snapshot,
            generation_id=str(candidate.to_dict()["generation_id"]),
            receipt_sha256=candidate.sha256,
        ):
            raise PointerCorrupt("repair candidate is failed, superseded, or uncertified")

        pending_value = None if pending is None else pending.to_dict()
        pending_belongs_to_operation = (
            operation_epoch is not None
            and fence_token is not None
            and pending_value is not None
            and int(pending_value["operation_epoch"]) == operation_epoch
            and int(pending_value["fence_token"]) == fence_token
        )
        if pending_belongs_to_operation and pending is not None:
            pending_receipts = self.verify_pointer(
                pending,
                expected_repo_uuid=repo_uuid,
                deadline_ns=deadline_ns,
            )
            pending_candidate = pending_receipts["current"]
            if int(pending_candidate.to_dict()["active_source_revision"]) != active_source_revision:
                raise PointerCorrupt(
                    "pending repair does not match current active-source authority"
                )
        pointer_action = "replace"
        next_revision: int
        last_good: dict[str, str] | None
        visible_current = (
            current if "current" in by_name else (pending if chosen_name == "pending" else None)
        )
        repaired_pointer: PointerSet | None = None
        journal_actions = journal.actions

        finalized_pending = (
            current is not None
            and pending is not None
            and current.canonical == pending.canonical
            and "current" in by_name
            and "pending" in by_name
            and self._fully_verified(current, by_name["current"][1])
            and self._journal_records_pointer(
                snapshot,
                current,
                transition="REPAIRED",
                deadline_ns=deadline_ns,
            )
        )
        if finalized_pending:
            assert current is not None
            pointer_action = "finalize_pending"
            candidate = by_name["current"][1]["current"]
            next_revision = int(current.to_dict()["pointer_revision"])
            last_good_receipt = by_name["current"][1].get("last_good")
            last_good = None if last_good_receipt is None else self._ref(last_good_receipt)
            repaired_pointer = current
        elif (
            pending_belongs_to_operation
            and pending is not None
            and "pending" in by_name
            and self._fully_verified(pending, by_name["pending"][1])
        ):
            pointer_action = "resume_pending"
            candidate = by_name["pending"][1]["current"]
            next_revision = int(pending.to_dict()["pointer_revision"])
            last_good_receipt = by_name["pending"][1].get("last_good")
            last_good = None if last_good_receipt is None else self._ref(last_good_receipt)
            repaired_pointer = pending
            if not self._journal_records_pointer(
                snapshot,
                pending,
                transition="REPAIRED",
                deadline_ns=deadline_ns,
            ):
                journal_actions = (*journal_actions, "append_repair")
        else:
            revisions = [
                int(pointer.to_dict()["pointer_revision"])
                for pointer in (current, pending, prior_pointer)
                if pointer is not None
            ]
            if prior is not None:
                revisions.append(int(prior.to_dict()["replaced_by_revision"]))
            revisions.extend(journal_revisions)
            next_revision = max(revisions, default=0) + 1
            last_good = None
            if "current" in by_name:
                former, former_receipts = by_name["current"]
                former_ref = cast(dict[str, Any], former.to_dict()["current"])
                if former_ref["generation_id"] != candidate.to_dict()["generation_id"]:
                    last_good = self._ref(former_receipts["current"])
            if last_good is None:
                for source_name in dict.fromkeys((chosen_name, "current", "pending", "prior")):
                    receipt = verified_by_name.get(source_name, {}).get("last_good")
                    if (
                        receipt is not None
                        and receipt.to_dict()["generation_id"]
                        != candidate.to_dict()["generation_id"]
                    ):
                        last_good = self._ref(receipt)
                        break
            journal_actions = (*journal_actions, "append_repair")
            if (
                visible_current is not None
                and pending is not None
                and visible_current.canonical == pending.canonical
                and prior is not None
            ):
                visible_current = prior_pointer

        repaired_refs = (
            self._pointer_refs(repaired_pointer)
            if repaired_pointer is not None
            else {
                str(candidate.to_dict()["generation_id"]),
                *(() if last_good is None else (str(last_good["generation_id"]),)),
            }
        )
        quarantine = tuple(
            sorted(
                generation_id
                for generation_id in corrupt_generations
                if generation_id not in repaired_refs
            )
        )
        evidence = {
            "active_source_revision": active_source_revision,
            "journal": self._journal_projection_evidence(journal),
            "pointers": raw_evidence,
            "verified_receipts": {
                name: {key: receipt.sha256 for key, receipt in sorted(receipts.items())}
                for name, receipts in sorted(verified_by_name.items())
            },
        }
        plan = self._repair_plan(
            classification="repairable",
            candidate=self._ref(candidate),
            last_good=last_good,
            next_pointer_revision=next_revision,
            selected_from=chosen_name,
            pointer_action=pointer_action,
            journal_actions=journal_actions,
            quarantine=quarantine,
            evidence=evidence,
        )
        return _PointerRepairAnalysis(
            plan=plan,
            current=current,
            pending=pending,
            prior=prior,
            prior_pointer=prior_pointer,
            candidate=candidate,
            visible_current=visible_current,
            journal=journal,
        )

    @contextmanager
    def _repair_analysis_locked(
        self,
        repo_uuid: str,
        *,
        active_source_revision: int,
        operation_epoch: int | None,
        fence_token: int | None,
        exclusive: bool,
        allow_atomic_temps: bool,
        deadline_ns: int | None,
    ) -> Iterator[_PointerRepairAnalysis]:
        workspace_temps = self.state.inspect_atomic_temps(
            self._workspace(repo_uuid),
            deadline_ns=deadline_ns,
        )
        if workspace_temps and not allow_atomic_temps:
            raise PointerCorrupt("pointer atomic temporary files require legacy fenced recovery")
        current, current_sha256 = self._read_repair_pointer(
            self._current(repo_uuid),
            repo_uuid=repo_uuid,
            allow_missing=True,
            allow_invalid=True,
            deadline_ns=deadline_ns,
        )
        pending, pending_sha256 = self._read_repair_pointer(
            self._pending(repo_uuid),
            repo_uuid=repo_uuid,
            allow_missing=True,
            allow_invalid=False,
            deadline_ns=deadline_ns,
        )
        prior, prior_sha256 = self._read_repair_prior(
            repo_uuid,
            deadline_ns=deadline_ns,
        )
        prior_pointer = (
            None
            if prior is None
            else cast(PointerSet, PointerSet.from_mapping(prior.to_dict()["pointer_set"]))
        )
        generation_ids = (
            self._pointer_refs(current)
            | self._pointer_refs(pending)
            | self._pointer_refs(prior_pointer)
        )
        locks: list[tuple[str, Path]] = []
        for generation_id in sorted(generation_ids):
            lock = self.generations._lock(repo_uuid, generation_id)
            require_before_deadline(
                deadline_ns,
                "pointer repair generation-lock inspection exceeded its deadline",
            )
            exists = self.state.private_file_exists(lock)
            require_before_deadline(
                deadline_ns,
                "pointer repair generation-lock inspection exceeded its deadline",
            )
            if not exists:
                raise PointerCorrupt(f"referenced generation lock is missing: {generation_id}")
            locks.append((generation_id, lock))
        with self.state.existing_generation_locks(
            locks,
            exclusive=exclusive,
            deadline_ns=deadline_ns,
        ):
            analysis = self._derive_repair_analysis(
                repo_uuid,
                active_source_revision=active_source_revision,
                operation_epoch=operation_epoch,
                fence_token=fence_token,
                current=current,
                pending=pending,
                prior=prior,
                raw_evidence={
                    "current_sha256": current_sha256,
                    "pending_sha256": pending_sha256,
                    "prior_sha256": prior_sha256,
                },
                allow_atomic_temps=allow_atomic_temps,
                deadline_ns=deadline_ns,
            )
            yield analysis

    def analyze_repair(
        self,
        repo_uuid: str,
        *,
        active_source_revision: int,
        operation_epoch: int | None = None,
        fence_token: int | None = None,
        deadline_ns: int | None = None,
    ) -> PointerRepairPlan:
        """Return a deterministic repair plan without any state mutation."""

        self._assert_no_gc_intent(repo_uuid, deadline_ns=deadline_ns)
        with self._repair_analysis_locked(
            repo_uuid,
            active_source_revision=active_source_revision,
            operation_epoch=operation_epoch,
            fence_token=fence_token,
            exclusive=False,
            allow_atomic_temps=False,
            deadline_ns=deadline_ns,
        ) as analysis:
            return analysis.plan

    def recover(
        self,
        grant: LeaseGrant,
        *,
        occurred_at: datetime,
        monotonic_ns: int,
        expected_plan: PointerRepairPlan | None = None,
        deadline_ns: int | None = None,
    ) -> PointerSet:
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"POINTER_RECOVERY", "REPAIR"}),
            deadline_ns=deadline_ns,
        ) as operation:
            self._assert_no_gc_intent(
                operation.repo_uuid,
                deadline_ns=deadline_ns,
            )
            with self._repair_analysis_locked(
                operation.repo_uuid,
                active_source_revision=operation.grant.active_source_revision,
                operation_epoch=operation.grant.operation_epoch,
                fence_token=operation.fence_token,
                exclusive=True,
                allow_atomic_temps=operation.operation == "POINTER_RECOVERY",
                deadline_ns=deadline_ns,
            ) as analysis:
                if expected_plan is not None and analysis.plan != expected_plan:
                    raise PointerSuperseded("repair plan changed before execution")
                require_before_deadline(
                    deadline_ns,
                    "pointer repair exceeded its lease deadline",
                )
                self.state.cleanup_atomic_temps(
                    self._workspace(operation.repo_uuid),
                    deadline_ns=deadline_ns,
                )
                snapshot = self.journal.recover_locked(
                    operation,
                    deadline_ns=deadline_ns,
                )
                if snapshot != analysis.journal.snapshot:
                    raise PointerConflict("journal changed during repair execution")
                plan = analysis.plan
                current = analysis.current
                pending = analysis.pending
                candidate = analysis.candidate
                if plan.pointer_action == "none":
                    if current is None:  # pragma: no cover - analysis invariant
                        raise PointerCorrupt("repair no-op has no visible pointer")
                    return current
                if plan.pointer_action == "finalize_pending":
                    if current is None or pending is None:  # pragma: no cover
                        raise PointerCorrupt("pending repair finalization is incomplete")
                    revision = plan.next_pointer_revision
                    for generation_id in plan.quarantine:
                        self._quarantine_corrupt(
                            operation.repo_uuid,
                            generation_id,
                            revision=revision,
                            deadline_ns=deadline_ns,
                        )
                    self.state.unlink_and_sync(
                        self._pending(operation.repo_uuid),
                        label="pointer:repaired:complete",
                        deadline_ns=deadline_ns,
                    )
                    self.fault_hook("pointer:repaired:complete")
                    self.verify_pointer(
                        current,
                        expected_repo_uuid=operation.repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                    return current
                if plan.pointer_action == "resume_pending":
                    if pending is None:  # pragma: no cover - analysis invariant
                        raise PointerCorrupt("pending repair resumption is incomplete")
                    if current is None or current.canonical != pending.canonical:
                        self.state.atomic_replace_bytes(
                            self._current(operation.repo_uuid),
                            pending.canonical,
                            label="pointer:repaired:visible",
                            deadline_ns=deadline_ns,
                        )
                        self.fault_hook("pointer:repaired:visible")
                    if not self._journal_records_pointer(
                        snapshot,
                        pending,
                        transition="REPAIRED",
                        deadline_ns=deadline_ns,
                    ):
                        self.journal.append_pointer_locked(
                            operation,
                            transition="REPAIRED",
                            generation_id=str(candidate.to_dict()["generation_id"]),
                            receipt_sha256=candidate.sha256,
                            pointer_revision=plan.next_pointer_revision,
                            occurred_at=occurred_at,
                            deadline_ns=deadline_ns,
                        )
                    self.fault_hook("pointer:repaired:journal_durable")
                    for generation_id in plan.quarantine:
                        self._quarantine_corrupt(
                            operation.repo_uuid,
                            generation_id,
                            revision=plan.next_pointer_revision,
                            deadline_ns=deadline_ns,
                        )
                    self.state.unlink_and_sync(
                        self._pending(operation.repo_uuid),
                        label="pointer:repaired:complete",
                        deadline_ns=deadline_ns,
                    )
                    self.fault_hook("pointer:repaired:complete")
                    self.verify_pointer(
                        pending,
                        expected_repo_uuid=operation.repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                    return pending
                if plan.pointer_action != "replace":  # pragma: no cover
                    raise PointerCorrupt("repair plan contains an unknown pointer action")
                repaired = self._pointer_document(
                    operation,
                    revision=plan.next_pointer_revision,
                    candidate=candidate,
                    last_good=plan.last_good,
                )
                result = self._persist_move(
                    operation,
                    current=analysis.visible_current,
                    candidate=candidate,
                    pointer=repaired,
                    transition="REPAIRED",
                    occurred_at=occurred_at,
                    corrupt_generations=plan.quarantine,
                    deadline_ns=deadline_ns,
                )
                self.verify_pointer(
                    result,
                    expected_repo_uuid=operation.repo_uuid,
                    deadline_ns=deadline_ns,
                )
                return result

    @contextmanager
    def read_current(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Iterator[GenerationRead]:
        while True:
            require_before_deadline(
                deadline_ns,
                "current pointer read exceeded its deadline",
            )
            pointer = self._read_pointer(
                self._current(repo_uuid),
                allow_missing=False,
                expected_repo_uuid=repo_uuid,
                deadline_ns=deadline_ns,
            )
            require_before_deadline(
                deadline_ns,
                "current pointer read exceeded its deadline",
            )
            assert pointer is not None
            value = pointer.to_dict()
            current = cast(dict[str, Any], value["current"])
            generation_id = str(current["generation_id"])
            lock = self.generations._lock(repo_uuid, generation_id)
            with self.state.existing_generation_lock(
                lock,
                generation_id=generation_id,
                exclusive=False,
                deadline_ns=deadline_ns,
            ):
                require_before_deadline(
                    deadline_ns,
                    "current pointer read exceeded its deadline",
                )
                reloaded = self._read_pointer(
                    self._current(repo_uuid),
                    allow_missing=False,
                    expected_repo_uuid=repo_uuid,
                    deadline_ns=deadline_ns,
                )
                require_before_deadline(
                    deadline_ns,
                    "current pointer read exceeded its deadline",
                )
                if reloaded is None or reloaded.canonical != pointer.canonical:
                    continue
                receipt = self._verify_generation(
                    repo_uuid,
                    generation_id,
                    deadline_ns=deadline_ns,
                )
                require_before_deadline(
                    deadline_ns,
                    "current pointer read exceeded its deadline",
                )
                if receipt.sha256 != current["receipt_sha256"]:
                    raise PointerCorrupt("current pointer receipt hash does not match generation")
                self._verify_visible_pointer_journal(
                    repo_uuid,
                    pointer,
                    deadline_ns=deadline_ns,
                )
                require_before_deadline(
                    deadline_ns,
                    "current pointer read exceeded its deadline",
                )
                yield GenerationRead(
                    pointer=pointer,
                    receipt=receipt,
                    generation_path=self.state.path(
                        self.generations._generation(repo_uuid, generation_id)
                    ),
                )
                return

    def revalidate_read(
        self,
        repo_uuid: str,
        reading: GenerationRead,
        *,
        deadline_ns: int | None = None,
    ) -> None:
        """Revalidate a protected read without releasing its shared lock.

        Callers use this immediately before an output-release boundary. The
        method performs no recovery or mutation; any pointer or receipt change
        is a conflict and the caller must discard its output.
        """

        require_before_deadline(
            deadline_ns,
            "current pointer revalidation exceeded its deadline",
        )
        pointer = self._read_pointer(
            self._current(repo_uuid),
            allow_missing=False,
            expected_repo_uuid=repo_uuid,
            deadline_ns=deadline_ns,
        )
        require_before_deadline(
            deadline_ns,
            "current pointer revalidation exceeded its deadline",
        )
        if pointer is None or pointer.canonical != reading.pointer.canonical:
            raise PointerConflict("current pointer changed during protected read")
        receipts = self.verify_pointer(
            pointer,
            expected_repo_uuid=repo_uuid,
            deadline_ns=deadline_ns,
        )
        require_before_deadline(
            deadline_ns,
            "current pointer revalidation exceeded its deadline",
        )
        self._verify_visible_pointer_journal(
            repo_uuid,
            pointer,
            deadline_ns=deadline_ns,
        )
        require_before_deadline(
            deadline_ns,
            "current pointer revalidation exceeded its deadline",
        )
        if receipts["current"].canonical != reading.receipt.canonical:
            raise PointerConflict("current receipt changed during protected read")
        current = cast(dict[str, Any], pointer.to_dict()["current"])
        expected_path = self.state.path(
            self.generations._generation(repo_uuid, str(current["generation_id"]))
        )
        if expected_path != reading.generation_path:
            raise PointerConflict("current generation path changed during protected read")


__all__ = [
    "GenerationRead",
    "PointerCAS",
    "PointerConflict",
    "PointerCorrupt",
    "PointerError",
    "PointerRepairPlan",
    "PointerRecoveryRequired",
    "PointerStore",
    "PointerSuperseded",
]
