"""Replacement maintenance contracts and an explicitly non-authoritative model.

Nothing here inspects OS custody, controls processes, implements IPC, or grants
filesystem authority. Model witnesses are caller-supplied simulation data. A
future native backend must independently establish every fact they represent;
constructing these records cannot enable ``canonical_replacement._maintenance``.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import PurePosixPath
import threading
from typing import Callable, Literal, Mapping, Protocol

Action = Literal["execute", "resume", "rollback"]
State = Literal["HELD", "GRANTING", "ACTIVE", "RECOVERY_HELD", "RELEASE_READY", "RELEASED"]
Identity = tuple[int, int]


class MaintenanceContractError(ValueError):
    """An operation or modeled transition does not satisfy the contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MaintenanceContractError(message)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value: str | None) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")) + "\n"


def _identity(value: Identity) -> bool:
    return (type(value) is tuple and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value))


@dataclass(frozen=True)
class OperationDescriptor:
    """Immutable action selectors, distinct from a maintenance grant.

    ``request_json`` is a canonical copy of an already-admitted graph request;
    its digest also binds the frozen parents and all source-selection fields.
    The forward preview binds the observed old output and preservation inventory.
    This type performs no filesystem reads or admission. Invalid or incomplete
    selectors raise ``MaintenanceContractError``; JSON encoding errors propagate.
    """

    action: Action
    request_json: str
    preview_sha256: str | None = None
    seal_sha256: str | None = None
    journal_sha256: str | None = None
    quarantine: str | None = None
    quarantine_parent: Identity | None = None
    new_tree_sha256: str | None = None

    def __post_init__(self) -> None:
        request = json.loads(self.request_json)
        _require(type(request) is dict and _canonical(request) == self.request_json,
                 "canonical replacement request snapshot required")
        output = request.get("output")
        if not isinstance(output, str):
            raise MaintenanceContractError("configured graphify-out target required")
        _require(PurePosixPath(output).is_absolute()
                 and PurePosixPath(output).name == "graphify-out",
                 "configured graphify-out target required")
        _require(isinstance(request.get("parent_identities"), dict), "frozen parents required")
        _require(self.action in {"execute", "resume", "rollback"}, "unknown maintenance action")
        if self.action == "rollback":
            _require(self.preview_sha256 is None and self.seal_sha256 is None
                     and _sha(self.journal_sha256) and isinstance(self.quarantine, str)
                     and PurePosixPath(self.quarantine).is_absolute()
                     and self.quarantine_parent is not None and _identity(self.quarantine_parent)
                     and (self.new_tree_sha256 is None or _sha(self.new_tree_sha256)),
                     "exact rollback selectors required")
        else:
            _require(_sha(self.preview_sha256) and _sha(self.seal_sha256)
                     and ((self.action == "execute" and self.journal_sha256 is None)
                          or (self.action == "resume" and _sha(self.journal_sha256)))
                     and self.quarantine is None
                     and self.quarantine_parent is None and self.new_tree_sha256 is None,
                     "exact forward selectors required")

    @classmethod
    def from_request(cls, action: Action, request: Mapping[str, object], **selectors) -> OperationDescriptor:
        """Copy an admitted request and action selectors without retaining caller containers."""
        return cls(action=action, request_json=_canonical(dict(request)), **selectors)

    @property
    def request_sha256(self) -> str:
        """Digest the complete immutable request, using the coordinator's JSON encoding."""
        return _digest(self.request_json)

    @property
    def target(self) -> str:
        """Return the configured canonical output path, without resolving it on disk."""
        return json.loads(self.request_json)["output"]

    @property
    def sha256(self) -> str:
        """Bind every action selector; this integrity value is not an authority token."""
        return _digest(_canonical(asdict(self)))


class MaintenanceBroker(Protocol):
    """Future whole-operation transport; no server or registration ships here.

    Workers receive no filesystem capability. Implementations must validate the
    exact operation and data at the trusted mutator, rather than yield authority
    for caller-side mutation. This protocol defines no serialization or IPC.
    """

    def submit(self, operation: OperationDescriptor, sealed_data: bytes) -> None:
        """Submit bounded data for the selected action; acknowledgment cannot release service.

        A forward action must bind the sealed bytes to its descriptor. Rollback
        supplies no forward payload. A native implementation must authenticate
        each request, reject resource attachments and independently admit every
        selector. There is no generic open, rename or handle-returning operation.
        """
        ...


@dataclass(frozen=True)
class TargetBinding:
    """Modeled physical source/old-output and inventory bindings omitted from the request.

    A native issuer must observe and verify these under exclusion. They are
    separate from the request's frozen parent identities and confer no continuity
    authority over a historical output.
    """

    root_identity: Identity
    old_output_identity: Identity
    old_inventory_sha256: str

    def __post_init__(self) -> None:
        _require(_identity(self.root_identity) and _identity(self.old_output_identity)
                 and _sha(self.old_inventory_sha256), "complete modeled target binding required")


@dataclass(frozen=True)
class GrantIdentity:
    """Modeled broker-only bindings, never OS attestation or a worker grant.

    ``runtime_sha256`` binds the complete trusted mutator runtime and dependencies;
    ``policy_sha256`` binds its launch, identity, namespace and transport policy.
    Controller and one-shot mutator incarnations are distinct from reusable PIDs.
    """

    enrollment: str
    boot: str
    epoch: int
    mutator: str
    runtime_sha256: str
    target_binding: TargetBinding
    controller: str
    policy_sha256: str

    def __post_init__(self) -> None:
        _require(all(type(item) is str and bool(item)
                     for item in (self.enrollment, self.boot, self.mutator, self.controller))
                 and type(self.epoch) is int and self.epoch > 0 and _sha(self.runtime_sha256)
                 and _sha(self.policy_sha256) and isinstance(self.target_binding, TargetBinding),
                 "complete grant identity required")


@dataclass(frozen=True)
class BrokerAdmission:
    """Caller-supplied simulation evidence for one exact broker-only operation.

    These flags represent independently required native facts; setting them does
    not establish any of those facts. Protected capabilities include metadata and
    namespace mutation, even through descriptors opened read-only. The initial
    profile allows one trusted mutator without child processes.
    """

    operation_sha256: str
    identity: GrantIdentity
    exclusive_mutator: bool
    trusted_runtime: bool
    protected_namespace: bool
    authenticated_peer: bool
    data_only_transport: bool
    workers_without_capabilities: bool
    no_children: bool


@dataclass(frozen=True)
class BrokerRetirement:
    """Simulation evidence for mutator exit and complete operation quiescence.

    A reap alone is insufficient: queued requests and in-flight effects must be
    reconciled, protected handles/mappings closed, and worker capability absence
    established. Permanent registry and launch authority remains with the trusted
    controller; this witness does not claim that authority has been revoked.
    """

    operation_sha256: str
    identity: GrantIdentity
    mutator_exited: bool
    requests_quiesced: bool
    capabilities_closed: bool
    workers_without_capabilities: bool


@dataclass(frozen=True)
class TerminalValidation:
    """Simulation result of independent, post-exit native and archive validation.

    A committed result means a closed generation-one receipt, replacement origin,
    exact canonical identity and unchanged archive were checked. A rolled-back
    result records preservation only and cannot authorize service restart.
    """

    operation_sha256: str
    outcome: Literal["committed", "rolled-back-unrecovered"]
    output_identity: Identity
    archive_sha256: str
    replacement_binding: str | None = None
    receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        _require(_sha(self.operation_sha256) and _identity(self.output_identity)
                 and _sha(self.archive_sha256), "complete terminal validation required")
        _require(self.outcome in {"committed", "rolled-back-unrecovered"}, "unknown terminal outcome")
        if self.outcome == "committed":
            _require(isinstance(self.replacement_binding, str)
                     and self.replacement_binding.startswith("canonical-replacement-v1:")
                     and _sha(self.replacement_binding.removeprefix("canonical-replacement-v1:"))
                     and _sha(self.receipt_sha256), "closed replacement receipt required")
        else:
            _require(self.replacement_binding is None and self.receipt_sha256 is None,
                     "rollback does not establish healthy publication")


@dataclass(frozen=True)
class ReleaseRecord:
    """Modeled durable release approval, independent of execute's exact gen-one retry."""

    operation_sha256: str
    identity: GrantIdentity
    terminal: TerminalValidation


@dataclass(frozen=True)
class CurrentPublication:
    """Simulation result of normal closed-receipt and retained-origin validation."""

    output_identity: Identity
    replacement_binding: str
    generation: int
    receipt_sha256: str


@dataclass(frozen=True)
class ModelState:
    """Immutable simulation state passed to an injected persistence acknowledgment.

    It is not a protected store, a resumable native journal or an enrollment
    record. In particular, deserializing or copying it grants no live authority.
    ``writers_admitted`` retains attempted admission history even when a durable
    write's acknowledgment fails; ``RECOVERY_HELD`` still denies client admission.
    """

    state: State = "HELD"
    controller: str = ""
    controller_incarnations: tuple[str, ...] = ()
    mutator_incarnations: tuple[str, ...] = ()
    epoch: int = 0
    operation: OperationDescriptor | None = None
    identity: GrantIdentity | None = None
    admission: BrokerAdmission | None = None
    retirement: BrokerRetirement | None = None
    access_withdrawn: bool = False
    writers_admitted: bool = False
    terminal: TerminalValidation | None = None
    release: ReleaseRecord | None = None


class ModelHandle(AbstractContextManager["ModelHandle"]):
    """Controller-internal simulation handle, never returned to a worker by IPC.

    Context exit holds service rather than releasing it. It authorizes no
    caller-side filesystem operation, even when the modeled state is ACTIVE.
    """

    def __init__(self, registry: ModelRegistry, operation: OperationDescriptor, identity: GrantIdentity):
        self._registry = registry
        self.operation = operation
        self.identity = identity

    def __enter__(self) -> ModelHandle:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Model timeout, channel closure or process failure without restarting service."""
        self._registry.hold(self)


class ModelRegistry:
    """One-target lifecycle simulation, never a production maintenance backend.

    Construction assumes a cold-start-enrolled target with no admitted writers;
    it does not prove that assumption. ``persist`` must return only after the
    simulated state is durable. Its exceptions retain the attempted selection
    and epoch in an in-memory recovery hold and propagate; they do not prove
    that no durable write occurred. The caller may not treat returned records
    as filesystem grants. Creating another registry does not restore or replace
    native enrollment/history; only the existing model's restart transition
    simulates controller replacement without erasing its history.
    A lock serializes transitions within this one model, including different
    journal slots; it does not supply cross-process or OS exclusion.
    """

    def __init__(self, target: str, enrollment: str, boot: str, runtime_sha256: str,
                 target_binding: TargetBinding,
                 persist: Callable[[ModelState], None], *, controller: str, policy_sha256: str):
        _require(PurePosixPath(target).is_absolute() and PurePosixPath(target).name == "graphify-out"
                 and bool(enrollment) and bool(boot) and _sha(runtime_sha256)
                 and isinstance(target_binding, TargetBinding) and _sha(policy_sha256)
                 and type(controller) is str and bool(controller), "model enrollment required")
        self.target, self.enrollment, self.boot, self.runtime_sha256 = target, enrollment, boot, runtime_sha256
        self.target_binding = target_binding
        self.policy_sha256 = policy_sha256
        self._persist = persist
        self._lock = threading.RLock()
        self._state = ModelState(controller=controller, controller_incarnations=(controller,))
        self._handle: ModelHandle | None = None

    @property
    def snapshot(self) -> ModelState:
        """Return immutable modeled state, with no authority or serialization side effects."""
        return self._state

    def _commit(self, candidate: ModelState) -> None:
        try:
            self._persist(candidate)
        except BaseException:
            # A failed acknowledgment can follow a durable write. Retain the
            # attempted epoch, incarnation and writer-admission latch as uncertain.
            self._state = replace(candidate, state="RECOVERY_HELD")
            raise
        self._state = candidate

    def _current(self, handle: ModelHandle) -> ModelState:
        _require(handle is self._handle and self._state.operation == handle.operation
                 and self._state.identity == handle.identity, "stale or copied maintenance handle")
        return self._state

    def grant(self, operation: OperationDescriptor, identity: GrantIdentity,
              admission: BrokerAdmission) -> ModelHandle:
        """Model internal mutator admission after persistence, never a worker capability.

        Require exact broker policy and explicit boundary witnesses. Unknown or
        still-live prior mutators refuse; each attempted incarnation is retained.
        Contract errors raise MaintenanceContractError; persistence errors hold
        the attempted state and propagate without returning a handle.
        """
        with self._lock:
            old = self._state
            _require(operation.target == self.target and identity.enrollment == self.enrollment
                     and identity.boot == self.boot and identity.runtime_sha256 == self.runtime_sha256
                     and identity.controller == old.controller and identity.policy_sha256 == self.policy_sha256
                     and identity.target_binding == self.target_binding
                     and identity.epoch == old.epoch + 1, "grant binding or epoch changed")
            _require(isinstance(admission, BrokerAdmission)
                     and admission.operation_sha256 == operation.sha256 and admission.identity == identity
                     and all(value is True for value in (
                         admission.exclusive_mutator, admission.trusted_runtime, admission.protected_namespace,
                         admission.authenticated_peer, admission.data_only_transport,
                         admission.workers_without_capabilities, admission.no_children)),
                     "exact broker-only admission evidence required")
            _require(old.state in {"HELD", "RECOVERY_HELD"} and not old.writers_admitted
                     and old.terminal is None, "target is not eligible for cold-start maintenance")
            if old.operation is not None:
                _require(old.retirement is not None and old.access_withdrawn,
                         "prior mutator cohort is not retired")
                _require(operation.request_sha256 == old.operation.request_sha256,
                         "incomplete operation request changed")
                if old.operation.action == "rollback":
                    _require(operation == old.operation, "incomplete rollback selectors changed")
                elif operation.action != "rollback":
                    _require(operation.preview_sha256 == old.operation.preview_sha256
                             and operation.seal_sha256 == old.operation.seal_sha256,
                             "incomplete operation data selectors changed")
            _require(identity.mutator not in old.mutator_incarnations, "mutator incarnation was already used")
            candidate = ModelState(state="GRANTING", controller=old.controller,
                                   controller_incarnations=old.controller_incarnations,
                                   mutator_incarnations=(*old.mutator_incarnations, identity.mutator),
                                   epoch=identity.epoch, operation=operation, identity=identity, admission=admission)
            self._commit(candidate)
            self._commit(replace(candidate, state="ACTIVE"))
            self._handle = ModelHandle(self, operation, identity)
            return self._handle

    def hold(self, handle: ModelHandle) -> None:
        """Retain exclusion after handle closure, timeout or controller/mutator failure."""
        with self._lock:
            old = self._current(handle)
            if old.state != "RELEASED":
                self._commit(replace(old, state="RECOVERY_HELD"))

    def restart(self, controller: str) -> None:
        """Simulate a proven controller replacement while retaining denial and history.

        This is an in-memory contract transition, not native state restoration or
        process retirement. The caller must independently establish exclusive
        controller custody. Old operation, retirement and release evidence stays
        bound to its original incarnation; no boot identity is refreshed. This
        transition alone offers no post-restart service-release reconciliation.
        """
        with self._lock:
            old = self._state
            _require(type(controller) is str and bool(controller)
                     and controller not in old.controller_incarnations,
                     "fresh controller incarnation required")
            self._commit(replace(old, state="RECOVERY_HELD", controller=controller,
                                 controller_incarnations=(*old.controller_incarnations, controller)))

    def confirm_retirement(self, handle: ModelHandle, witness: BrokerRetirement) -> None:
        """Require exact exit, quiescence and capability evidence; uncertainty keeps a hold.

        A mismatched witness refuses without adopting it. An exact but incomplete
        witness persists RECOVERY_HELD and raises MaintenanceContractError. No
        simulated witness establishes an OS fact.
        """
        with self._lock:
            old = self._current(handle)
            _require(old.state in {"ACTIVE", "RECOVERY_HELD"}
                     and witness.operation_sha256 == handle.operation.sha256
                     and witness.identity == handle.identity, "exact cohort retirement witness required")
            if not all(value is True for value in (witness.mutator_exited, witness.requests_quiesced,
                                                  witness.capabilities_closed, witness.workers_without_capabilities)):
                self._commit(replace(old, state="RECOVERY_HELD", retirement=None))
                raise MaintenanceContractError("complete broker retirement evidence required")
            self._commit(replace(old, state="RECOVERY_HELD", retirement=witness))

    def withdraw_access(self, handle: ModelHandle) -> None:
        """Model removal of temporary archive access only after all old writable handles are gone."""
        with self._lock:
            old = self._current(handle)
            _require(old.state == "RECOVERY_HELD" and old.retirement is not None,
                     "cohort exit must precede access withdrawal")
            self._commit(replace(old, access_withdrawn=True))

    def validate_terminal(self, handle: ModelHandle, terminal: TerminalValidation) -> None:
        """Model independent verification after exit/access withdrawal; rollback remains held."""
        with self._lock:
            old = self._current(handle)
            _require(old.state == "RECOVERY_HELD" and old.retirement is not None and old.access_withdrawn
                     and terminal.operation_sha256 == handle.operation.sha256
                     and terminal.archive_sha256 == self.target_binding.old_inventory_sha256,
                     "post-exit independent terminal validation required")
            if handle.operation.action == "rollback":
                _require(terminal.outcome == "rolled-back-unrecovered",
                         "rollback cannot release healthy service")
                _require(terminal.output_identity == self.target_binding.old_output_identity,
                         "rollback restored output identity changed")
            else:
                _require(terminal.outcome == "committed", "forward terminal outcome changed")
                _require(terminal.output_identity != self.target_binding.old_output_identity,
                         "replacement output identity reuses preserved directory")
            state: State = "RELEASE_READY" if terminal.outcome == "committed" else "HELD"
            self._commit(replace(old, state=state, terminal=terminal))

    def release(self, handle: ModelHandle) -> ReleaseRecord:
        """Persist explicit release before admitting clients; exact retries return its own record."""
        with self._lock:
            old = self._current(handle)
            _require(handle.identity.controller == old.controller, "retired controller cannot release service")
            if old.state == "RELEASED" and old.release is not None:
                return old.release
            _require(old.state == "RELEASE_READY" and old.terminal is not None
                     and old.terminal.outcome == "committed", "validated healthy release required")
            assert old.terminal is not None
            record = ReleaseRecord(handle.operation.sha256, handle.identity, old.terminal)
            self._commit(replace(old, state="RELEASED", release=record))
            return record

    def admit_client(self, runtime_sha256: str, publication: CurrentPublication) -> ReleaseRecord:
        """Model compatible startup after durable release and normal receipt/origin validation.

        Later generations retain the released identity/origin but may have a new
        receipt. The first writer-admission bit is persisted before returning;
        a failed acknowledgment holds the target and grants no modeled launch.
        """
        with self._lock:
            old = self._state
            _require(old.state == "RELEASED" and old.release is not None,
                     "service release has not been acknowledged")
            assert old.release is not None
            terminal = old.release.terminal
            _require(runtime_sha256 == self.runtime_sha256
                     and publication.output_identity == terminal.output_identity
                     and publication.replacement_binding == terminal.replacement_binding
                     and type(publication.generation) is int and publication.generation >= 1
                     and _sha(publication.receipt_sha256)
                     and (publication.generation != 1 or publication.receipt_sha256 == terminal.receipt_sha256),
                     "current publication or compatible runtime changed")
            if not old.writers_admitted:
                self._commit(replace(old, writers_admitted=True))
            return old.release
