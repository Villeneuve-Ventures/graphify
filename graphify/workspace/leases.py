"""Monotonic fenced leases for P2 workspace lifecycle operations."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, cast, Iterator, Protocol
from uuid import UUID

from graphify.workspace.contracts import (
    CapacityReservationState,
    FencedLease,
    Registry,
    StagedBuildState,
    StructuralBuildRequest,
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


_MAX_STAGED_BUILD_STATE_BYTES = 64 * 1024
_STAGED_BUILD_TERMINAL_STATES = frozenset({"PROMOTED", "ABANDONED"})
_STAGED_ATTEMPT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
            kind="workspace",
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
        state = self._load_state_locked(
            document,
            repo_uuid,
            recover=False,
            deadline_ns=deadline_ns,
        )
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

    def _staged_build_paths(self, repo_uuid: str) -> tuple[Path, Path, Path]:
        directory = self._directory(repo_uuid)
        return (
            directory / "staged-build.json",
            directory / "staged-build.previous.json",
            directory / "staged-build.pending.json",
        )

    def _load_staged_build_locked(
        self,
        repo_uuid: str,
        *,
        recover: bool = True,
        deadline_ns: int | None = None,
    ) -> StagedBuildState | None:
        current, previous, pending = self._staged_build_paths(repo_uuid)
        try:
            if recover:
                return self.state.recover_record(
                    label=f"staged-build:{repo_uuid}",
                    current=current,
                    previous=previous,
                    pending=pending,
                    decoder=StagedBuildState.from_json,
                    revision=lambda value: value.revision,
                    allow_missing=True,
                    max_bytes=_MAX_STAGED_BUILD_STATE_BYTES,
                )
            return self.state.read_stable_record(
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
        except (StateCorrupt, StatePathError) as exc:
            raise LeaseRecoveryRequired(f"staged build state requires recovery: {exc}") from exc

    @staticmethod
    def _verify_selected_source(repo_uuid: str, entry: dict[str, Any]) -> Path:
        recorded_source = entry["active_source"]
        try:
            discovered = discover_source(Path(recorded_source["path"]))
        except (OSError, RuntimeError) as exc:
            raise SourceAmbiguousError(f"selected active source is unavailable: {exc}") from exc
        if discovered.repo_uuid != repo_uuid or discovered.registry_source != recorded_source:
            raise SourceAmbiguousError(
                "selected active source no longer matches registry evidence"
            )
        return discovered.root

    def _assert_staged_recovery_source_boundary(
        self,
        repo_uuid: str,
        entry: dict[str, Any],
    ) -> None:
        """Keep recovery state external without requiring a live selected source."""

        try:
            source_root = self._verify_selected_source(repo_uuid, entry)
        except SourceAmbiguousError:
            recorded_root = Path(str(entry["active_source"]["path"]))
            try:
                source_root = recorded_root.resolve(strict=True)
            except (OSError, RuntimeError):
                source_root = Path(os.path.abspath(recorded_root))
            if (
                self.state.root == source_root
                or self.state.root in source_root.parents
                or source_root in self.state.root.parents
            ):
                raise StatePathError(
                    f"external state root {self.state.root} overlaps "
                    f"recorded source checkout {source_root}"
                )
            return
        self.state.assert_external_to(source_root)

    @contextmanager
    def _bound_request_state(
        self,
        repo_uuid: str,
    ) -> Iterator[tuple[Registry, dict[str, Any], WorkspaceLeaseState]]:
        """Hold registry then workspace authority for a request-bound mutation."""

        with self.registry.recovered_snapshot() as document:
            entry = _registry_entry(document, repo_uuid)
            source_root = self._verify_selected_source(repo_uuid, entry)
            self.state.assert_external_to(source_root)
            with self.workspace_lock(repo_uuid):
                yield document, entry, self._load_state_locked(document, repo_uuid)

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
        allow_staged_abandonment: bool = False,
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
        staged_build = self._load_staged_build_locked(repo_uuid, recover=recover)
        if (
            staged_build is not None
            and staged_build.abandonment_intent is not None
            and not allow_staged_abandonment
        ):
            raise LeaseRecoveryRequired(
                "durable staged abandonment requires exact recovery"
            )
        if (
            staged_build is not None
            and staged_build.lifecycle_state not in _STAGED_BUILD_TERMINAL_STATES
        ):
            if staged_build.lifecycle_state == "CERTIFIED":
                allowed = {"PROMOTE", "POINTER_RECOVERY"}
            else:
                allowed = {"BUILD"}
            if operation not in allowed:
                raise LeaseRecoveryRequired(
                    "unresolved staged build requires request-bound recovery"
                )
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
        deadline_ns: int | None = None,
    ) -> WorkspaceLeaseState:
        entry = _registry_entry(document, repo_uuid)
        current, previous, pending = self._paths(repo_uuid)
        kwargs = {
            "label": "workspace",
            "current": current,
            "previous": previous,
            "pending": pending,
            "decoder": WorkspaceLeaseState.from_json,
            "revision": lambda state: state.revision,
            "allow_missing": False,
        }
        if recover:
            recovered = self.state.recover_record(**kwargs)
        else:
            recovered = self.state.read_stable_record(
                **kwargs,
                deadline_ns=deadline_ns,
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
            staged_attempt_sha256=recovered.staged_attempt_sha256,
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

    def acquire_staged_request(
        self,
        repo_uuid: str,
        generation_id: str,
        operation: str,
        owner: LeaseOwner,
        request: StructuralBuildRequest,
        *,
        attempt_sha256: str,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> LeaseGrant:
        """Acquire or recover one exact staged-build operation.

        The durable REQUESTED record proves the caller's original CAS before
        an operation epoch advances. Only this narrow path may recover that
        request across expired BUILD/PROMOTE attempts; ordinary ``acquire``
        remains strict and cannot adopt staged-build authority. The caller
        retains one unique attempt digest across a commit-unknown retry.
        """

        try:
            request = StructuralBuildRequest.from_mapping(request.to_dict())
        except Exception as exc:
            raise LeaseRecoveryRequired(f"staged build request is invalid: {exc}") from exc
        if operation not in {"BUILD", "PROMOTE", "POINTER_RECOVERY"}:
            raise LeaseRecoveryRequired(
                f"operation {operation} is not a staged-build recovery operation"
            )
        with self.registry.recovered_snapshot() as document:
            return self._acquire_under_registry_lock(
                document,
                repo_uuid,
                operation,
                owner,
                expected_registry_revision=request.expected_registry_revision,
                expected_active_source_revision=request.expected_active_source_revision,
                expected_operation_epoch=request.expected_operation_epoch,
                expected_migration_epoch=request.expected_migration_epoch,
                acquired_at=acquired_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=ttl_ns,
                verify_active=True,
                staged_request=(generation_id, request),
                staged_attempt_sha256=attempt_sha256,
            )

    def acquire_staged_recovery(
        self,
        repo_uuid: str,
        generation_id: str,
        operation: str,
        owner: LeaseOwner,
        request: StructuralBuildRequest,
        *,
        attempt_sha256: str,
        acquired_at: datetime,
        monotonic_ns: int,
        ttl_ns: int,
    ) -> LeaseGrant:
        """Acquire stale recovery under one caller-retained attempt digest."""

        try:
            request = StructuralBuildRequest.from_mapping(request.to_dict())
        except Exception as exc:
            raise LeaseRecoveryRequired(f"staged build request is invalid: {exc}") from exc
        if operation not in {"BUILD", "PROMOTE", "POINTER_RECOVERY"}:
            raise LeaseRecoveryRequired(
                f"operation {operation} cannot recover a staged build"
            )
        with self.registry.recovered_snapshot() as document:
            return self._acquire_under_registry_lock(
                document,
                repo_uuid,
                operation,
                owner,
                expected_registry_revision=request.expected_registry_revision,
                expected_active_source_revision=request.expected_active_source_revision,
                expected_operation_epoch=request.expected_operation_epoch,
                expected_migration_epoch=request.expected_migration_epoch,
                acquired_at=acquired_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=ttl_ns,
                verify_active=True,
                staged_request=(generation_id, request),
                staged_attempt_sha256=attempt_sha256,
                allow_stale_staged_authority=True,
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
        staged_request: tuple[str, StructuralBuildRequest] | None = None,
        staged_attempt_sha256: str | None = None,
        allow_stale_staged_authority: bool = False,
    ) -> LeaseGrant:
        owner = self._require_current_owner(owner)
        if staged_request is None:
            if staged_attempt_sha256 is not None:
                raise LeaseError("staged attempt requires a staged request")
        elif (
            staged_attempt_sha256 is None
            or _STAGED_ATTEMPT_SHA256_RE.fullmatch(staged_attempt_sha256) is None
        ):
            raise LeaseError("staged attempt must be lowercase SHA-256 hex")
        if ttl_ns <= 0 or monotonic_ns < 0:
            raise LeaseError("ttl_ns must be positive and monotonic_ns must be non-negative")
        entry = _registry_entry(document, repo_uuid)
        if verify_active:
            if allow_stale_staged_authority:
                self._assert_staged_recovery_source_boundary(repo_uuid, entry)
            else:
                source_root = self._verify_selected_source(repo_uuid, entry)
                self.state.assert_external_to(source_root)
        with self.workspace_lock(repo_uuid):
            state = self._load_state_locked(document, repo_uuid)
            staged_build = self._load_staged_build_locked(repo_uuid)
            if staged_request is None:
                self._check_expected(
                    document,
                    entry,
                    state,
                    expected_registry_revision=expected_registry_revision,
                    expected_active_source_revision=expected_active_source_revision,
                    expected_operation_epoch=expected_operation_epoch,
                    expected_migration_epoch=expected_migration_epoch,
                )
                if (
                    staged_build is not None
                    and staged_build.lifecycle_state not in _STAGED_BUILD_TERMINAL_STATES
                ):
                    raise LeaseRecoveryRequired(
                        "unresolved staged build requires request-bound recovery"
                    )
            else:
                generation_id, request = staged_request
                self._check_staged_request_locked(
                    document,
                    entry,
                    state,
                    staged_build,
                    repo_uuid=repo_uuid,
                    generation_id=generation_id,
                    operation=operation,
                    request=request,
                    allow_stale_authority=allow_stale_staged_authority,
                )
            self._assert_recovery_barriers_locked(
                repo_uuid,
                operation,
                allow_staged_abandonment=allow_stale_staged_authority,
            )
            domain = _lease_domain(operation)
            existing = state.leases.get(domain)
            if existing is not None:
                existing_value = existing.to_dict()
                existing_owner = existing_value["owner"]
                rebooted = existing_owner["boot_id"] != owner.boot_id
                expired = monotonic_ns >= int(existing_value["liveness_deadline_monotonic_ns"])
                if not rebooted and not expired:
                    if (
                        staged_request is not None
                        and state.staged_attempt_sha256 == staged_attempt_sha256
                        and existing_value["owner"] == owner.to_dict()
                        and existing_value["operation"] == operation
                    ):
                        return LeaseGrant(
                            lease=existing,
                            registry_revision=int(document.to_dict()["revision"]),
                            active_source_revision=int(entry["active_source_revision"]),
                            operation_epoch=state.lease_epochs[domain],
                            migration_epoch=state.migration_epoch,
                        )
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
            committed_staged_attempt = state.staged_attempt_sha256
            if domain == "workspace":
                committed_staged_attempt = staged_attempt_sha256
            committed = self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=fence_token,
                    operation_epoch=operation_epoch,
                    migration_epoch=migration_epoch,
                    leases=leases,
                    lease_epochs=lease_epochs,
                    staged_attempt_sha256=committed_staged_attempt,
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
    def _check_staged_request_locked(
        document: Registry,
        entry: dict[str, Any],
        state: WorkspaceLeaseState,
        staged_build: StagedBuildState | None,
        *,
        repo_uuid: str,
        generation_id: str,
        operation: str,
        request: StructuralBuildRequest,
        allow_stale_authority: bool = False,
    ) -> None:
        if staged_build is None:
            raise LeaseRecoveryRequired("staged build request is not durable")
        if (
            staged_build.repo_uuid != repo_uuid
            or staged_build.generation_id != generation_id
            or staged_build.request.sha256 != request.sha256
        ):
            raise LeaseRecoveryRequired("staged build request binding does not match")
        if (
            staged_build.abandonment_intent is not None
            and not allow_stale_authority
        ):
            raise LeaseRecoveryRequired(
                "durable staged abandonment requires exact recovery"
            )
        allowed = {
            "BUILD": {"REQUESTED", "PUBLISHING", "COMPLETE"},
            "PROMOTE": {"CERTIFIED"},
            "POINTER_RECOVERY": {"CERTIFIED"},
        }
        if staged_build.lifecycle_state not in allowed[operation]:
            raise LeaseRecoveryRequired(
                f"staged build in {staged_build.lifecycle_state} cannot acquire {operation}"
            )
        actual_registry_revision = int(document.to_dict()["revision"])
        if actual_registry_revision < request.expected_registry_revision:
            raise RevisionConflict(
                "registry_revision expected at least "
                f"{request.expected_registry_revision}, found {actual_registry_revision}"
            )
        actual_active_revision = int(entry["active_source_revision"])
        if (
            not allow_stale_authority
            and request.expected_active_source_revision != actual_active_revision
        ):
            raise RevisionConflict(
                "active_source_revision expected "
                f"{request.expected_active_source_revision}, found {actual_active_revision}"
            )
        if state.operation_epoch < request.expected_operation_epoch:
            raise RevisionConflict(
                "operation_epoch expected at least "
                f"{request.expected_operation_epoch}, found {state.operation_epoch}"
            )
        if (
            not allow_stale_authority
            and state.migration_epoch != request.expected_migration_epoch
        ):
            raise RevisionConflict(
                "migration_epoch expected "
                f"{request.expected_migration_epoch}, found {state.migration_epoch}"
            )

    @contextmanager
    def current_staged_recovery(
        self,
        grant: LeaseGrant,
        generation_id: str,
        request: StructuralBuildRequest,
        *,
        monotonic_ns: int,
    ) -> Iterator[LeaseOperation]:
        """Validate a stale request-bound grant without adopting its old source CAS."""

        self._require_grant_owner(grant)
        repo_uuid = str(grant.lease.to_dict()["repo_uuid"])
        with self.registry.recovered_snapshot() as document:
            entry = _registry_entry(document, repo_uuid)
            self._assert_staged_recovery_source_boundary(repo_uuid, entry)
            with self.workspace_lock(repo_uuid):
                state = self._load_state_locked(document, repo_uuid)
                _domain, current = self._matching_lease(state, grant)
                operation = str(current.to_dict()["operation"])
                if monotonic_ns >= int(
                    current.to_dict()["liveness_deadline_monotonic_ns"]
                ):
                    raise LeaseExpired("liveness deadline has passed")
                staged_build = self._load_staged_build_locked(repo_uuid)
                self._check_staged_request_locked(
                    document,
                    entry,
                    state,
                    staged_build,
                    repo_uuid=repo_uuid,
                    generation_id=generation_id,
                    operation=operation,
                    request=request,
                    allow_stale_authority=True,
                )
                self._assert_recovery_barriers_locked(
                    repo_uuid,
                    operation,
                    allow_staged_abandonment=True,
                )
                yield LeaseOperation(
                    registry=document,
                    state=state,
                    lease=current,
                    grant=grant,
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
                        staged_attempt_sha256=state.staged_attempt_sha256,
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
            staged_attempt_sha256 = state.staged_attempt_sha256
            if domain == "workspace":
                staged_attempt_sha256 = None
            return self._commit_state_locked(
                WorkspaceLeaseState(
                    repo_uuid=state.repo_uuid,
                    revision=state.revision + 1,
                    fence_high_watermark=state.fence_high_watermark,
                    operation_epoch=state.operation_epoch,
                    migration_epoch=state.migration_epoch,
                    leases=leases,
                    lease_epochs=lease_epochs,
                    staged_attempt_sha256=staged_attempt_sha256,
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
