"""Monotonic fenced leases for P2 workspace lifecycle operations."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, cast, Iterator, Protocol
from uuid import UUID

from graphify.workspace.contracts import (
    CapacityReservationState,
    FencedLease,
    Registry,
    WorkspaceLeaseState,
)
from graphify.workspace.identity import SourceAmbiguousError, discover_source
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
    WORKSPACE_LOCK_RANK,
    require_before_deadline,
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


class LeaseRecoveryRequired(LeaseError):
    code = "lease_recovery_required"


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

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "LeaseOwner":
        return cls(
            boot_id=str(value["boot_id"]),
            pid=int(value["pid"]),
            process_start_id=str(value["process_start_id"]),
        )


class LeaseIdentityProvider(Protocol):
    """Trusted runtime identity used to bind lease liveness to this process."""

    def current_owner(self) -> LeaseOwner: ...


class _DarwinProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class SystemLeaseIdentityProvider:
    """Read boot and process-start identity from OS-owned runtime state."""

    @staticmethod
    def _digest(label: str, value: str) -> str:
        return hashlib.sha256(f"{label}\0{value}".encode()).hexdigest()

    @classmethod
    def _linux_owner(cls, pid: int) -> LeaseOwner:
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            stat_value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            remainder = stat_value[stat_value.rindex(")") + 2 :].split()
            process_start = remainder[19]
        except (OSError, IndexError, ValueError) as exc:
            raise LeaseError(f"cannot read trusted Linux process identity: {exc}") from exc
        return LeaseOwner(
            boot_id=cls._digest("linux-boot", boot),
            pid=pid,
            process_start_id=cls._digest("linux-process-start", process_start),
        )

    @classmethod
    def _darwin_owner(
        cls,
        pid: int,
        *,
        runner: Any | None = None,
        proc_pidinfo: Any | None = None,
    ) -> LeaseOwner:
        environment = {"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        run_process = subprocess.run if runner is None else runner
        try:
            boot = run_process(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout.strip()
            process_start = cls._darwin_process_start(pid, proc_pidinfo)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise LeaseError(f"cannot read trusted macOS process identity: {exc}") from exc
        if not boot or not process_start:
            raise LeaseError("trusted macOS process identity is empty")
        try:
            boot = str(UUID(boot))
        except ValueError as exc:
            raise LeaseError("trusted macOS boot-session identity is invalid") from exc
        return LeaseOwner(
            boot_id=cls._digest("darwin-boot", boot),
            pid=pid,
            process_start_id=cls._digest("darwin-process-start", process_start),
        )

    @staticmethod
    def _darwin_process_start(pid: int, proc_pidinfo: Any | None = None) -> str:
        if proc_pidinfo is None:
            try:
                library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                probe = cast(Any, library.proc_pidinfo)
            except OSError as exc:
                raise LeaseError(f"cannot load trusted macOS process identity: {exc}") from exc
            probe.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            probe.restype = ctypes.c_int
        else:
            probe = proc_pidinfo
        info = _DarwinProcBSDInfo()
        size = ctypes.sizeof(info)
        returned = probe(pid, 3, 0, ctypes.byref(info), size)
        if returned != size or info.pbi_pid != pid:
            error_number = ctypes.get_errno()
            detail = os.strerror(error_number) if error_number else f"returned {returned} bytes"
            raise LeaseError(f"cannot read trusted macOS process start: {detail}")
        if info.pbi_start_tvsec < 1 or info.pbi_start_tvusec >= 1_000_000:
            raise LeaseError("trusted macOS process start is invalid")
        return f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec:06d}"

    def current_owner(self) -> LeaseOwner:
        pid = os.getpid()
        if sys.platform == "linux":
            return self._linux_owner(pid)
        if sys.platform == "darwin":
            return self._darwin_owner(pid)
        raise LeaseError(f"trusted lease identity is unsupported on {sys.platform}")


@dataclass(frozen=True)
class LeaseGrant:
    lease: FencedLease
    registry_revision: int
    active_source_revision: int
    operation_epoch: int
    migration_epoch: int


@dataclass(frozen=True)
class LeaseOperation:
    """A current fenced operation held under its serialized workspace lock."""

    registry: Registry
    state: WorkspaceLeaseState
    lease: FencedLease
    grant: LeaseGrant

    @property
    def repo_uuid(self) -> str:
        return str(self.lease.to_dict()["repo_uuid"])

    @property
    def operation(self) -> str:
        return str(self.lease.to_dict()["operation"])

    @property
    def fence_token(self) -> int:
        return int(self.lease.to_dict()["fence_token"])


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
        identity_provider: LeaseIdentityProvider | None = None,
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
        self.identity_provider = identity_provider or SystemLeaseIdentityProvider()

    def current_owner(self) -> LeaseOwner:
        """Return the OS-backed owner identity accepted by this store."""

        return self.identity_provider.current_owner()

    def _require_current_owner(self, owner: LeaseOwner) -> LeaseOwner:
        trusted = self.current_owner()
        if owner != trusted:
            raise StaleLease("stale_owner: owner identity does not match the current runtime")
        return trusted

    def _require_grant_owner(self, grant: LeaseGrant) -> LeaseOwner:
        owner = LeaseOwner.from_mapping(cast(dict[str, Any], grant.lease.to_dict()["owner"]))
        return self._require_current_owner(owner)

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

    @contextmanager
    def read_only_workspace_lock(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> Iterator[None]:
        directory = self._directory(repo_uuid)
        with self.state.existing_lock(
            directory / "workspace.lock",
            rank=WORKSPACE_LOCK_RANK,
            name="workspace",
            exclusive=True,
            deadline_ns=deadline_ns,
        ):
            yield

    def read_only_snapshot_locked(
        self,
        document: Registry,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> WorkspaceLeaseState:
        """Read stable lease/fence state while the caller owns both read locks."""

        require_before_deadline(
            deadline_ns,
            "workspace lease snapshot read exceeded its deadline",
        )
        state = self._load_state_locked(document, repo_uuid, recover=False)
        require_before_deadline(
            deadline_ns,
            "workspace lease snapshot read exceeded its deadline",
        )
        return state

    def _paths(self, repo_uuid: str) -> tuple[Path, Path, Path]:
        directory = self._directory(repo_uuid)
        return (
            directory / "workspace.json",
            directory / "workspace.previous.json",
            directory / "workspace.pending.json",
        )

    def _durable_record_exists(self, relative: Path) -> bool:
        try:
            return self.state.private_file_exists(relative)
        except StatePathError as exc:
            raise LeaseRecoveryRequired(f"recovery barrier is unsafe: {relative}") from exc

    def _assert_recovery_barriers_locked(
        self,
        repo_uuid: str,
        operation: str,
        *,
        recover: bool = True,
    ) -> None:
        workspace = self._directory(repo_uuid)
        gc_intent = workspace / "gc" / "intent.json"
        if self._durable_record_exists(gc_intent) and operation not in {
            "GC",
            "POINTER_RECOVERY",
        }:
            raise LeaseRecoveryRequired("unresolved GC intent requires fenced reconciliation")
        pointer_intent = workspace / "pointers.pending.json"
        if self._durable_record_exists(pointer_intent) and operation != "POINTER_RECOVERY":
            raise LeaseRecoveryRequired("unresolved pointer intent requires fenced recovery")
        if operation != "ACTIVATE":
            return
        try:
            loader = (
                self.state.recover_record
                if recover
                else self.state.read_stable_record
            )
            capacity = loader(
                label="capacity",
                current=Path("capacity.json"),
                previous=Path("capacity.previous.json"),
                pending=Path("capacity.pending.json"),
                decoder=CapacityReservationState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
            )
        except StateCorrupt as exc:
            raise LeaseRecoveryRequired(
                f"capacity reservation state requires recovery: {exc}"
            ) from exc
        if capacity is not None and any(
            reservation.repo_uuid == repo_uuid for reservation in capacity.reservations
        ):
            raise LeaseRecoveryRequired(
                "outstanding generation reservation must complete before activation"
            )

    def _load_state_locked(
        self,
        document: Registry,
        repo_uuid: str,
        *,
        recover: bool = True,
    ) -> WorkspaceLeaseState:
        entry = _registry_entry(document, repo_uuid)
        current, previous, pending = self._paths(repo_uuid)
        loader = self.state.recover_record if recover else self.state.read_stable_record
        recovered = loader(
            label="workspace",
            current=current,
            previous=previous,
            pending=pending,
            decoder=WorkspaceLeaseState.from_json,
            revision=lambda state: state.revision,
            allow_missing=False,
        )
        active_evidence = entry["active_source_evidence"]
        fence_floor = int(active_evidence["fence_token"])
        operation_floor = int(active_evidence["operation_epoch"])
        assert recovered is not None
        if recovered.repo_uuid != repo_uuid:
            raise StateCorrupt("workspace lease state is installed under the wrong UUID")
        return WorkspaceLeaseState(
            repo_uuid=recovered.repo_uuid,
            revision=recovered.revision,
            fence_high_watermark=max(recovered.fence_high_watermark, fence_floor),
            operation_epoch=max(recovered.operation_epoch, operation_floor),
            migration_epoch=recovered.migration_epoch,
            leases=dict(recovered.leases),
            lease_epochs=dict(recovered.lease_epochs),
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
        with self.registry.recovered_snapshot() as document:
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
    ) -> LeaseGrant:
        owner = self._require_current_owner(owner)
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
            self._assert_recovery_barriers_locked(repo_uuid, operation)
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
            lease_epochs = dict(state.lease_epochs)
            lease_epochs[domain] = operation_epoch
            committed = self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=fence_token,
                    operation_epoch=operation_epoch,
                    migration_epoch=migration_epoch,
                    leases=leases,
                    lease_epochs=lease_epochs,
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
        *,
        require_epochs: bool = True,
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
        if require_epochs and state.lease_epochs.get(domain) != grant.operation_epoch:
            raise StaleLease("stale_epoch: lease-domain operation epoch advanced")
        if require_epochs and state.migration_epoch != grant.migration_epoch:
            raise StaleLease("stale_epoch: operation or migration epoch advanced")
        return domain, current

    @staticmethod
    def _check_active(document: Registry, grant: LeaseGrant) -> None:
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        entry = _registry_entry(document, repo_uuid)
        if int(entry["active_source_revision"]) != grant.active_source_revision:
            raise StaleLease("stale_source: active source revision advanced")

    def assert_current(self, grant: LeaseGrant, *, monotonic_ns: int) -> FencedLease:
        self._require_grant_owner(grant)
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        with self.registry.recovered_snapshot() as document:
            with self.workspace_lock(repo_uuid):
                self._check_active(document, grant)
                state = self._load_state_locked(document, repo_uuid)
                _domain, current = self._matching_lease(state, grant)
                if monotonic_ns >= int(current.to_dict()["liveness_deadline_monotonic_ns"]):
                    raise LeaseExpired("liveness deadline has passed")
                return current

    @contextmanager
    def current_operation(
        self,
        grant: LeaseGrant,
        *,
        monotonic_ns: int,
        allowed_operations: frozenset[str] | None = None,
        registry_required: bool = False,
    ) -> Iterator[LeaseOperation]:
        """Keep a grant current for one serialized P3 mutation.

        Normal mutations retain only the workspace lock after taking a stable
        registry snapshot. The live workspace lease prevents activation or a
        successor fence from committing. Callers that mutate global state such
        as capacity reservations opt into the short ``registry_required`` path.
        """

        self._require_grant_owner(grant)
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])

        def checked_operation(document: Registry) -> LeaseOperation:
            self._check_active(document, grant)
            state = self._load_state_locked(document, repo_uuid)
            _domain, current = self._matching_lease(state, grant)
            operation = str(current.to_dict()["operation"])
            if allowed_operations is not None and operation not in allowed_operations:
                raise StaleLease(f"operation {operation} is not authorized for this mutation")
            if monotonic_ns >= int(current.to_dict()["liveness_deadline_monotonic_ns"]):
                raise LeaseExpired("liveness deadline has passed")
            self._assert_recovery_barriers_locked(repo_uuid, operation)
            return LeaseOperation(
                registry=document,
                state=state,
                lease=current,
                grant=grant,
            )

        if registry_required:
            with self.registry.recovered_snapshot() as document:
                with self.workspace_lock(repo_uuid):
                    yield checked_operation(document)
            return

        with self.registry.recovered_snapshot() as document:
            self._check_active(document, grant)
            snapshot = document
        with self.workspace_lock(repo_uuid):
            yield checked_operation(snapshot)

    @contextmanager
    def current_operation_read_only(
        self,
        grant: LeaseGrant,
        *,
        monotonic_ns: int,
        allowed_operations: frozenset[str] | None = None,
    ) -> Iterator[LeaseOperation]:
        """Validate a grant under existing locks without repairing durable state."""

        self._require_grant_owner(grant)
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        with self.registry.read_only_snapshot() as document:
            with self.read_only_workspace_lock(repo_uuid):
                self._check_active(document, grant)
                state = self._load_state_locked(document, repo_uuid, recover=False)
                _domain, current = self._matching_lease(state, grant)
                operation = str(current.to_dict()["operation"])
                if allowed_operations is not None and operation not in allowed_operations:
                    raise StaleLease(
                        f"operation {operation} is not authorized for this mutation"
                    )
                if monotonic_ns >= int(
                    current.to_dict()["liveness_deadline_monotonic_ns"]
                ):
                    raise LeaseExpired("liveness deadline has passed")
                self._assert_recovery_barriers_locked(
                    repo_uuid,
                    operation,
                    recover=False,
                )
                yield LeaseOperation(
                    registry=document,
                    state=state,
                    lease=current,
                    grant=grant,
                )

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
        self._require_grant_owner(grant)
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        with self.registry.recovered_snapshot() as document:
            with self.workspace_lock(repo_uuid):
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
                        lease_epochs=dict(state.lease_epochs),
                    )
                )
                return LeaseGrant(
                    lease=committed.leases[domain],
                    registry_revision=int(document.to_dict()["revision"]),
                    active_source_revision=grant.active_source_revision,
                    operation_epoch=grant.operation_epoch,
                    migration_epoch=committed.migration_epoch,
                )

    def release(self, grant: LeaseGrant) -> WorkspaceLeaseState:
        with self.registry.recovered_snapshot() as document:
            return self._release_under_registry_lock(
                grant,
                document,
                validate_active=False,
            )

    def _release_under_registry_lock(
        self,
        grant: LeaseGrant,
        document: Registry,
        *,
        validate_active: bool,
    ) -> WorkspaceLeaseState:
        self._require_grant_owner(grant)
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        with self.workspace_lock(repo_uuid):
            if validate_active:
                self._check_active(document, grant)
            state = self._load_state_locked(document, repo_uuid)
            domain, _current = self._matching_lease(state, grant, require_epochs=False)
            leases = dict(state.leases)
            del leases[domain]
            lease_epochs = dict(state.lease_epochs)
            del lease_epochs[domain]
            return self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=state.repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=state.fence_high_watermark,
                    operation_epoch=state.operation_epoch,
                    migration_epoch=state.migration_epoch,
                    leases=leases,
                    lease_epochs=lease_epochs,
                )
            )

    def inspect(self, repo_uuid: str) -> WorkspaceLeaseState:
        with self.registry.recovered_snapshot() as document:
            with self.workspace_lock(repo_uuid):
                return self._load_state_locked(document, repo_uuid)


__all__ = [
    "LeaseBusy",
    "LeaseError",
    "LeaseExpired",
    "LeaseGrant",
    "LeaseIdentityProvider",
    "LeaseOperation",
    "LeaseOwner",
    "LeaseRecoveryRequired",
    "LeaseStore",
    "StaleLease",
    "SystemLeaseIdentityProvider",
]
