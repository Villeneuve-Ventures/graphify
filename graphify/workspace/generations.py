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

from graphify.workspace.contracts import (
    CapacityPolicy,
    CapacityReservation,
    CapacityReservationState,
    ContractError,
    GenerationCoordinationLock,
    GenerationReceipt,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseGrant, LeaseOperation, LeaseStore
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
)


_ALLOWED_FILE_MODES = frozenset({0o600, 0o644, 0o755})
_ALLOWED_DIRECTORY_MODES = frozenset({0o700, 0o755})
_CAPACITY_CURRENT = Path("capacity.json")
_CAPACITY_PREVIOUS = Path("capacity.previous.json")
_CAPACITY_PENDING = Path("capacity.pending.json")


class GenerationError(RuntimeError):
    """Base class for stable generation failures."""

    code = "generation_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class CapacityExceeded(GenerationError):
    code = "capacity_exceeded"


class _CapacityScanChanged(RuntimeError):
    pass


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
    active_source_revision: int
    operation_epoch: int
    fence_token: int


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
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
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

    @staticmethod
    def _tree_bytes(path: Path) -> int:
        total = 0
        for root, directories, files in os.walk(path, topdown=True, followlinks=False):
            root_path = Path(root)
            safe_directories: list[str] = []
            for name in directories:
                candidate = root_path / name
                details = candidate.lstat()
                if not stat.S_ISDIR(details.st_mode) or candidate.is_symlink():
                    raise CapacityExceeded(f"unsafe directory in capacity scan: {candidate}")
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                candidate = root_path / name
                details = candidate.lstat()
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise CapacityExceeded(f"unsafe file in capacity scan: {candidate}")
                total += details.st_size
        return total

    def _scan_usage_once(self) -> dict[tuple[str, str], int]:
        usage: dict[tuple[str, str], int] = {}
        workspaces = self.state.root / "workspaces"
        if workspaces.exists():
            for workspace in workspaces.iterdir():
                details = workspace.lstat()
                if not stat.S_ISDIR(details.st_mode) or workspace.is_symlink():
                    raise CapacityExceeded(f"unsafe workspace in capacity scan: {workspace}")
                repo_uuid = workspace.name
                candidates: list[tuple[Path, str]] = []
                for relative in (Path("generations"), Path("staging")):
                    parent = workspace / relative
                    if parent.exists():
                        candidates.extend((candidate, candidate.name) for candidate in parent.iterdir())
                for quarantine_kind in ("gc", "corrupt"):
                    quarantine = workspace / "quarantine" / quarantine_kind
                    if quarantine.exists():
                        candidates.extend(
                            (candidate, candidate.name.rsplit(".", 1)[0])
                            for candidate in quarantine.iterdir()
                        )
                for candidate, generation_id in candidates:
                    candidate_details = candidate.lstat()
                    if not stat.S_ISDIR(candidate_details.st_mode) or candidate.is_symlink():
                        raise CapacityExceeded(
                            f"unsafe generation in capacity scan: {candidate}"
                        )
                    key = (repo_uuid, generation_id)
                    if key in usage:
                        raise _CapacityScanChanged(
                            "generation occupies multiple active/staging/quarantine locations: "
                            f"{repo_uuid}/{generation_id}"
                        )
                    usage[key] = self._tree_bytes(candidate)
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
            final_path = self.state.path(
                self._generation(capacity_operation.repo_uuid, generation_id)
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
            if final_path.exists() and existing is None:
                raise GenerationConflict("generation is already certified")
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
            final_path = self.state.path(self._generation(operation.repo_uuid, generation_id))
            staging_relative = self._staging(operation.repo_uuid, generation_id)
            staging_path = self.state.path(staging_relative)
            if final_path.exists() and staging_path.exists():
                raise GenerationConflict("generation exists in both staging and final locations")
            if final_path.exists():
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
                if final_path.exists():
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
                active_source_revision=reservation.active_source_revision,
                operation_epoch=reservation.operation_epoch,
                fence_token=reservation.fence_token,
            )

    def _require_real_chain(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.state.root)
        except ValueError as exc:
            raise StatePathError("generation path escapes state root") from exc
        self.state.require_existing_directory_chain(relative)

    def _scan_directory(
        self,
        descriptor: int,
        *,
        prefix: str,
        entries: list[dict[str, str | int]],
        directories: list[str],
    ) -> None:
        before = os.fstat(descriptor)
        names = sorted(entry.name for entry in os.scandir(descriptor))
        if any(not name or "/" in name or name in {".", ".."} for name in names):
            raise GenerationError("payload contains a noncanonical directory entry")
        for name in names:
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
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open(name, flags, dir_fd=descriptor)
            try:
                opened_before = os.fstat(file_descriptor)
                if _identity(opened_before) != _identity(details):
                    raise PayloadChanged(f"payload file changed while opened: {relative}")
                digest = hashlib.sha256()
                while True:
                    try:
                        chunk = os.read(file_descriptor, 1024 * 1024)
                    except InterruptedError:
                        continue
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
        after_names = sorted(entry.name for entry in os.scandir(descriptor))
        after = os.fstat(descriptor)
        if names != after_names or _identity(before) != _identity(after):
            raise PayloadChanged(f"payload directory changed during inventory: {prefix}")

    def _inventory(self, container: Path, *, allowed_root_entries: frozenset[str]) -> _PayloadInventory:
        self._require_real_chain(container)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_descriptor = os.open(container, flags)
        try:
            root_before = os.fstat(root_descriptor)
            root_names = sorted(entry.name for entry in os.scandir(root_descriptor))
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
                )
            finally:
                os.close(payload_descriptor)
            after_root_names = sorted(entry.name for entry in os.scandir(root_descriptor))
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
        finally:
            os.close(root_descriptor)
        entries.sort(key=lambda item: str(item["path"]))
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
            final_path = self.state.path(
                self._generation(operation.repo_uuid, allocation.generation_id)
            )
            if final_path.exists():
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
            reservation.active_source_revision,
            reservation.operation_epoch,
            reservation.fence_token,
        )
        supplied = (
            allocation.expected_payload_bytes,
            allocation.capacity_policy_sha256,
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
                    "validations": list(request.validations),
                }
            ),
        )

    def _sync_inventory(
        self,
        repo_uuid: str,
        generation_id: str,
        inventory: _PayloadInventory,
    ) -> None:
        staging = self._staging(repo_uuid, generation_id)
        for entry in inventory.entries:
            relative = staging / str(entry["path"])
            self.state.fsync_regular_file(relative, allowed_modes=_ALLOWED_FILE_MODES)
            self.fault_hook(f"generation:{generation_id}:payload_file_durable:{entry['path']}")
        for directory in sorted(
            inventory.directories,
            key=lambda value: (-len(Path(value).parts), value),
        ):
            self.state.fsync_directory(staging / directory)
        self.state.fsync_directory(staging)
        self.fault_hook(f"generation:{generation_id}:payload_durable")

    def verify_generation(self, repo_uuid: str, generation_id: str) -> GenerationReceipt:
        relative = self._generation(repo_uuid, generation_id)
        generation = self.state.path(relative)
        inventory = self._inventory(
            generation,
            allowed_root_entries=frozenset({"graphify-out", "receipt.json"}),
        )
        receipt_bytes = self.state.read_existing_bytes(relative / "receipt.json")
        try:
            receipt = cast(GenerationReceipt, GenerationReceipt.from_json(receipt_bytes))
        except Exception as exc:
            raise GenerationError(f"generation receipt is invalid: {exc}") from exc
        value = receipt.to_dict()
        if value["repo_uuid"] != repo_uuid or value["generation_id"] != generation_id:
            raise GenerationError("generation receipt identity does not match its path")
        payload = cast(dict[str, Any], value["sealed_query_payload"])
        declared = cast(list[dict[str, Any]], payload["entries"])
        if canonical_json_bytes(declared) != canonical_json_bytes(list(inventory.entries)):
            raise PayloadChanged("certified generation payload does not match its receipt")
        if payload["manifest_sha256"] != payload_manifest_sha256("graphify-out", declared):
            raise GenerationError("generation receipt manifest digest does not match")
        lock_relative = self._lock(repo_uuid, generation_id)
        try:
            lock_bytes = self.state.read_existing_bytes(lock_relative)
            lock_document = cast(
                GenerationCoordinationLock,
                GenerationCoordinationLock.from_json(lock_bytes),
            )
        except Exception as exc:
            raise GenerationError(f"generation coordination lock is invalid: {exc}") from exc
        if lock_document.canonical != self._lock_document(generation_id).canonical:
            raise GenerationError("generation coordination lock identity does not match")
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
    ) -> GenerationReceipt:
        final_relative = self._generation(operation.repo_uuid, allocation.generation_id)
        final_path = self.state.path(final_relative)
        snapshot = self.journal.recover_locked(operation)
        events = snapshot.for_generation(allocation.generation_id)
        latest = None if not events else str(events[-1].to_dict()["transition"])
        validating_events = tuple(
            event for event in events if event.to_dict()["transition"] == "VALIDATING"
        )
        if final_path.exists():
            receipt = self.verify_generation(operation.repo_uuid, allocation.generation_id)
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
        receipt_present = self.state.path(receipt_relative).exists()
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
        if receipt_present:
            try:
                receipt = cast(
                    GenerationReceipt,
                    GenerationReceipt.from_json(self.state.read_existing_bytes(receipt_relative)),
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
            if not final_path.exists():
                raise
        self.fault_hook(f"generation:{allocation.generation_id}:installed")
        verified = self.verify_generation(operation.repo_uuid, allocation.generation_id)
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

    def certify(
        self,
        grant: LeaseGrant,
        allocation: GenerationAllocation,
        request: CertificationRequest,
        *,
        declared_entries: Sequence[Mapping[str, object]],
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> GenerationReceipt:
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
            registry_required=True,
        ) as capacity_operation:
            self._require_allocation(capacity_operation, allocation)
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
        ) as operation:
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
        return receipt


__all__ = [
    "CapacityExceeded",
    "CertificationRequest",
    "GenerationAllocation",
    "GenerationConflict",
    "GenerationError",
    "GenerationStore",
    "PayloadChanged",
]
