"""Monotonic fenced leases for P2 workspace lifecycle operations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast, Iterator

from graphify.workspace.contracts import FencedLease, Registry, WorkspaceLeaseState
from graphify.workspace.identity import SourceAmbiguousError, discover_source
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    Syscalls,
    WORKSPACE_LOCK_RANK,
)
from graphify.workspace.registry import RegistryStore, RevisionConflict


class LeaseError(RuntimeError):
    """Base class for stable fenced-lease failures."""

    code = "lease_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class LeaseBusy(LeaseError):
    code = "lease_busy"


class LeaseExpired(LeaseError):
    code = "lease_expired"


class StaleLease(LeaseError):
    code = "stale_fence"


@dataclass(frozen=True)
class LeaseOwner:
    boot_id: str
    pid: int
    process_start_id: str

    def __post_init__(self) -> None:
        if not self.boot_id or not self.process_start_id or self.pid < 1:
            raise LeaseError("lease owner requires boot_id, positive pid, and process_start_id")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "boot_id": self.boot_id,
            "pid": self.pid,
            "process_start_id": self.process_start_id,
        }


@dataclass(frozen=True)
class LeaseGrant:
    lease: FencedLease
    registry_revision: int
    active_source_revision: int
    operation_epoch: int
    migration_epoch: int


def _lease_domain(operation: str) -> str:
    return WorkspaceLeaseState.lease_domain(operation)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise LeaseError("lease timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _registry_entry(document: Registry, repo_uuid: str) -> dict[str, Any]:
    entries = [item for item in document.to_dict()["workspaces"] if item["repo_uuid"] == repo_uuid]
    if len(entries) != 1:
        raise SourceAmbiguousError(f"registry has no singular entry for {repo_uuid}")
    return entries[0]


class LeaseStore:
    """Allocate and validate non-resetting fence epochs under registry-first locks."""

    def __init__(
        self,
        state_root: Path,
        registry: RegistryStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.registry = registry
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        if self.state.root != self.registry.state.root:
            raise LeaseError("lease and registry stores must share one external state root")

    @staticmethod
    def _directory(repo_uuid: str) -> Path:
        return Path("workspaces") / WorkspaceLeaseState.canonical_repo_uuid(repo_uuid)

    @contextmanager
    def workspace_lock(self, repo_uuid: str) -> Iterator[None]:
        directory = self._directory(repo_uuid)
        with self.state.lock(
            directory / "workspace.lock",
            rank=WORKSPACE_LOCK_RANK,
            name="workspace",
        ):
            yield

    def _paths(self, repo_uuid: str) -> tuple[Path, Path, Path]:
        directory = self._directory(repo_uuid)
        return (
            directory / "workspace.json",
            directory / "workspace.previous.json",
            directory / "workspace.pending.json",
        )

    def _load_state_locked(
        self,
        document: Registry,
        repo_uuid: str,
    ) -> WorkspaceLeaseState:
        entry = _registry_entry(document, repo_uuid)
        current, previous, pending = self._paths(repo_uuid)
        recovered = self.state.recover_record(
            label="workspace",
            current=current,
            previous=previous,
            pending=pending,
            decoder=WorkspaceLeaseState.from_json,
            revision=lambda state: state.revision,
            allow_missing=True,
        )
        active_evidence = entry["active_source_evidence"]
        fence_floor = int(active_evidence["fence_token"])
        operation_floor = int(active_evidence["operation_epoch"])
        if recovered is None:
            return WorkspaceLeaseState(
                repo_uuid=repo_uuid,
                revision=0,
                fence_high_watermark=fence_floor,
                operation_epoch=operation_floor,
                migration_epoch=0,
                leases={},
            )
        if recovered.repo_uuid != repo_uuid:
            raise StateCorrupt("workspace lease state is installed under the wrong UUID")
        return WorkspaceLeaseState(
            repo_uuid=recovered.repo_uuid,
            revision=recovered.revision,
            fence_high_watermark=max(recovered.fence_high_watermark, fence_floor),
            operation_epoch=max(recovered.operation_epoch, operation_floor),
            migration_epoch=recovered.migration_epoch,
            leases=dict(recovered.leases),
        )

    def _commit_state_locked(self, state: WorkspaceLeaseState) -> WorkspaceLeaseState:
        current, previous, pending = self._paths(state.repo_uuid)
        return self.state.commit_record(
            label="workspace",
            current=current,
            previous=previous,
            pending=pending,
            payload=state.canonical,
            decoder=WorkspaceLeaseState.from_json,
        )

    @staticmethod
    def _check_expected(
        document: Registry,
        entry: dict[str, Any],
        state: WorkspaceLeaseState,
        *,
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
    ) -> None:
        actual_registry_revision = int(document.to_dict()["revision"])
        if expected_registry_revision != actual_registry_revision:
            raise RevisionConflict(
                "registry_revision expected "
                f"{expected_registry_revision}, found {actual_registry_revision}"
            )
        actual_active_revision = int(entry["active_source_revision"])
        if expected_active_source_revision != actual_active_revision:
            raise RevisionConflict(
                "active_source_revision expected "
                f"{expected_active_source_revision}, found {actual_active_revision}"
            )
        if expected_operation_epoch != state.operation_epoch:
            raise RevisionConflict(
                "operation_epoch expected "
                f"{expected_operation_epoch}, found {state.operation_epoch}"
            )
        if expected_migration_epoch != state.migration_epoch:
            raise RevisionConflict(
                "migration_epoch expected "
                f"{expected_migration_epoch}, found {state.migration_epoch}"
            )

    def acquire(
        self,
        repo_uuid: str,
        operation: str,
        owner: LeaseOwner,
        *,
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> LeaseGrant:
        document = self.registry.load()
        return self._acquire_under_registry_lock(
            document,
            repo_uuid,
            operation,
            owner,
            expected_registry_revision=expected_registry_revision,
            expected_active_source_revision=expected_active_source_revision,
            expected_operation_epoch=expected_operation_epoch,
            expected_migration_epoch=expected_migration_epoch,
            acquired_at=acquired_at,
            monotonic_ns=monotonic_ns,
            ttl_ns=ttl_ns,
            verify_active=True,
            recheck_registry=True,
        )

    def _acquire_under_registry_lock(
        self,
        document: Registry,
        repo_uuid: str,
        operation: str,
        owner: LeaseOwner,
        *,
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
        verify_active: bool = False,
        recheck_registry: bool = False,
    ) -> LeaseGrant:
        if ttl_ns <= 0 or monotonic_ns < 0:
            raise LeaseError("ttl_ns must be positive and monotonic_ns must be non-negative")
        entry = _registry_entry(document, repo_uuid)
        if verify_active:
            recorded_source = entry["active_source"]
            try:
                discovered = discover_source(Path(recorded_source["path"]))
            except (OSError, RuntimeError) as exc:
                raise SourceAmbiguousError(f"selected active source is unavailable: {exc}") from exc
            if discovered.repo_uuid != repo_uuid or discovered.registry_source != recorded_source:
                raise SourceAmbiguousError(
                    "selected active source no longer matches registry evidence"
                )
            self.state.assert_external_to(discovered.root)
        with self.workspace_lock(repo_uuid):
            if recheck_registry:
                document = self.registry._read_current_unlocked()
                entry = _registry_entry(document, repo_uuid)
            state = self._load_state_locked(document, repo_uuid)
            self._check_expected(
                document,
                entry,
                state,
                expected_registry_revision=expected_registry_revision,
                expected_active_source_revision=expected_active_source_revision,
                expected_operation_epoch=expected_operation_epoch,
                expected_migration_epoch=expected_migration_epoch,
            )
            domain = _lease_domain(operation)
            existing = state.leases.get(domain)
            if existing is not None:
                existing_value = existing.to_dict()
                existing_owner = existing_value["owner"]
                rebooted = existing_owner["boot_id"] != owner.boot_id
                expired = monotonic_ns >= int(existing_value["liveness_deadline_monotonic_ns"])
                if not rebooted and not expired:
                    raise LeaseBusy(
                        f"{domain} lease is held by pid {existing_owner['pid']} "
                        f"with fence {existing_value['fence_token']}"
                    )
            fence_token = state.fence_high_watermark + 1
            operation_epoch = state.operation_epoch + 1
            migration_epoch = state.migration_epoch + (operation == "MIGRATE")
            timestamp = _timestamp(acquired_at)
            lease = cast(
                FencedLease,
                FencedLease.from_mapping(
                    {
                        "contract": "graphify.workspace.fenced_lease",
                        "schema_version": 1,
                        "repo_uuid": repo_uuid,
                        "operation": operation,
                        "fence_token": fence_token,
                        "owner": owner.to_dict(),
                        "acquired_at": timestamp,
                        "heartbeat_at": timestamp,
                        "liveness_deadline_monotonic_ns": monotonic_ns + ttl_ns,
                    },
                ),
            )
            leases = dict(state.leases)
            leases[domain] = lease
            committed = self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=fence_token,
                    operation_epoch=operation_epoch,
                    migration_epoch=migration_epoch,
                    leases=leases,
                )
            )
            return LeaseGrant(
                lease=committed.leases[domain],
                registry_revision=int(document.to_dict()["revision"]),
                active_source_revision=int(entry["active_source_revision"]),
                operation_epoch=committed.operation_epoch,
                migration_epoch=committed.migration_epoch,
            )

    @staticmethod
    def _matching_lease(
        state: WorkspaceLeaseState,
        grant: LeaseGrant,
    ) -> tuple[str, FencedLease]:
        grant_value = grant.lease.to_dict()
        domain = _lease_domain(str(grant_value["operation"]))
        current = state.leases.get(domain)
        if current is None:
            raise StaleLease("lease domain is no longer owned")
        current_value = current.to_dict()
        if current_value["fence_token"] != grant_value["fence_token"]:
            raise StaleLease("fence token is no longer current")
        if current_value["owner"] != grant_value["owner"]:
            raise StaleLease("stale_owner: owner identity no longer matches")
        if (
            state.operation_epoch != grant.operation_epoch
            or state.migration_epoch != grant.migration_epoch
        ):
            raise StaleLease("stale_epoch: operation or migration epoch advanced")
        return domain, current

    @staticmethod
    def _check_active(document: Registry, grant: LeaseGrant) -> None:
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        entry = _registry_entry(document, repo_uuid)
        if int(entry["active_source_revision"]) != grant.active_source_revision:
            raise StaleLease("stale_source: active source revision advanced")

    def assert_current(self, grant: LeaseGrant, *, monotonic_ns: int) -> FencedLease:
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        document = self.registry.load()
        with self.workspace_lock(repo_uuid):
            document = self.registry._read_current_unlocked()
            self._check_active(document, grant)
            state = self._load_state_locked(document, repo_uuid)
            _domain, current = self._matching_lease(state, grant)
            if monotonic_ns >= int(current.to_dict()["liveness_deadline_monotonic_ns"]):
                raise LeaseExpired("liveness deadline has passed")
            return current

    def heartbeat(
        self,
        grant: LeaseGrant,
        *,
        heartbeat_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> LeaseGrant:
        if ttl_ns <= 0:
            raise LeaseError("ttl_ns must be positive")
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        document = self.registry.load()
        with self.workspace_lock(repo_uuid):
            document = self.registry._read_current_unlocked()
            self._check_active(document, grant)
            state = self._load_state_locked(document, repo_uuid)
            domain, current = self._matching_lease(state, grant)
            if monotonic_ns >= int(current.to_dict()["liveness_deadline_monotonic_ns"]):
                raise LeaseExpired("liveness deadline has passed")
            value = current.to_dict()
            value["heartbeat_at"] = _timestamp(heartbeat_at)
            value["liveness_deadline_monotonic_ns"] = monotonic_ns + ttl_ns
            leases = dict(state.leases)
            leases[domain] = cast(FencedLease, FencedLease.from_mapping(value))
            committed = self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=state.repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=state.fence_high_watermark,
                    operation_epoch=state.operation_epoch,
                    migration_epoch=state.migration_epoch,
                    leases=leases,
                )
            )
            return LeaseGrant(
                lease=committed.leases[domain],
                registry_revision=int(document.to_dict()["revision"]),
                active_source_revision=grant.active_source_revision,
                operation_epoch=committed.operation_epoch,
                migration_epoch=committed.migration_epoch,
            )

    def release(self, grant: LeaseGrant) -> WorkspaceLeaseState:
        document = self.registry.load()
        return self._release_under_registry_lock(
            grant,
            document,
            validate_active=True,
            recheck_registry=True,
        )

    def _release_under_registry_lock(
        self,
        grant: LeaseGrant,
        document: Registry,
        *,
        validate_active: bool,
        recheck_registry: bool = False,
    ) -> WorkspaceLeaseState:
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        with self.workspace_lock(repo_uuid):
            if recheck_registry:
                document = self.registry._read_current_unlocked()
            if validate_active:
                self._check_active(document, grant)
            state = self._load_state_locked(document, repo_uuid)
            domain, _current = self._matching_lease(state, grant)
            leases = dict(state.leases)
            del leases[domain]
            return self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=state.repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=state.fence_high_watermark,
                    operation_epoch=state.operation_epoch,
                    migration_epoch=state.migration_epoch,
                    leases=leases,
                )
            )

    def inspect(self, repo_uuid: str) -> WorkspaceLeaseState:
        document = self.registry.load()
        with self.workspace_lock(repo_uuid):
            document = self.registry._read_current_unlocked()
            return self._load_state_locked(document, repo_uuid)


__all__ = [
    "LeaseBusy",
    "LeaseError",
    "LeaseExpired",
    "LeaseGrant",
    "LeaseOwner",
    "LeaseStore",
    "StaleLease",
    "WorkspaceLeaseState",
]
