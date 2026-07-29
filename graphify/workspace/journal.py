"""Crash-durable framed lifecycle journal for immutable workspace generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, cast
import uuid

from graphify.workspace.contracts import (
    JournalEvent,
    JournalFrameTruncated,
    JournalHeadState,
    PointerSet,
    canonical_json_bytes,
    decode_journal_frame,
    encode_journal_frame,
)
from graphify.workspace.leases import LeaseGrant, LeaseOperation, LeaseStore
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


_SEGMENT_RE = re.compile(r"^(?P<sequence>[0-9]{20})\.gwf$", re.ASCII)
_ATOMIC_SEGMENT_TEMP_RE = re.compile(
    r"^\..+\.tmp-[1-9][0-9]*-[0-9a-f]{32}$",
    re.ASCII,
)
_MAX_JOURNAL_FRAME_BYTES = 1024 * 1024
_MAX_POINTER_RECORD_BYTES = 64 * 1024
_EVENT_NAMESPACE = uuid.UUID("3924cb61-439f-49d0-8a8c-b3753d140d5e")
_PRECERTIFICATION = frozenset({"ALLOCATED", "STAGING", "BUILT", "VALIDATING", "FAILED"})
_PRECERTIFICATION_NEXT = {
    None: frozenset({"ALLOCATED"}),
    "ALLOCATED": frozenset({"STAGING", "FAILED"}),
    "STAGING": frozenset({"BUILT", "FAILED"}),
    "BUILT": frozenset({"VALIDATING", "FAILED"}),
    "VALIDATING": frozenset({"VALIDATING", "CERTIFIED", "FAILED"}),
}
_TRANSITION_OPERATIONS = {
    "ALLOCATED": frozenset({"BUILD", "MIGRATE"}),
    "STAGING": frozenset({"BUILD", "MIGRATE"}),
    "BUILT": frozenset({"BUILD", "MIGRATE"}),
    "VALIDATING": frozenset({"BUILD", "MIGRATE"}),
    "CERTIFIED": frozenset({"BUILD", "MIGRATE"}),
    "FAILED": frozenset({"BUILD", "MIGRATE"}),
    "PROMOTED": frozenset({"PROMOTE"}),
    "ROLLED_BACK": frozenset({"ROLLBACK"}),
    "REPAIRED": frozenset({"POINTER_RECOVERY", "REPAIR"}),
    "SUPERSEDED": frozenset({"PROMOTE", "ROLLBACK"}),
}
_VISIBLE_POINTER_TRANSITIONS = frozenset({"PROMOTED", "ROLLED_BACK", "REPAIRED"})


def _require_stable_read_deadline(deadline_ns: int | None) -> None:
    require_before_deadline(deadline_ns, "journal stable read exceeded its deadline")


class JournalError(RuntimeError):
    """Base class for stable journal failures."""

    code = "journal_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class JournalCorrupt(JournalError):
    code = "journal_corrupt"


class JournalConflict(JournalError):
    code = "journal_conflict"


class JournalRecoveryRequired(JournalConflict):
    """A stable read found one recoverable uncommitted journal suffix."""


@dataclass(frozen=True)
class JournalSnapshot:
    head: JournalHeadState | None
    events: tuple[JournalEvent, ...]

    def for_generation(self, generation_id: str) -> tuple[JournalEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.to_dict()["generation_id"] == generation_id
        )


@dataclass(frozen=True)
class JournalRecoveryProjection:
    """The exact journal state and bounded writes a recovery would produce."""

    snapshot: JournalSnapshot
    actions: tuple[str, ...]
    evidence_sha256: str


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise JournalError("journal timestamps must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


class JournalStore:
    """Append sealed one-frame segments and recover one crash-durable head."""

    def __init__(
        self,
        state_root: Path,
        leases: LeaseStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.leases = leases
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        if self.state.root != leases.state.root:
            raise JournalError("journal and lease stores must share one external state root")
        self.fault_hook = fault_hook or (lambda _event: None)
        self._generation_authority = object()
        self._pointer_authority = object()

    @staticmethod
    def _directory(repo_uuid: str) -> Path:
        return LeaseStore._directory(repo_uuid) / "journal"

    @classmethod
    def _head_paths(cls, repo_uuid: str) -> tuple[Path, Path, Path]:
        directory = cls._directory(repo_uuid)
        return (
            directory / "head.json",
            directory / "head.previous.json",
            directory / "head.pending.json",
        )

    @classmethod
    def _segments_directory(cls, repo_uuid: str) -> Path:
        return cls._directory(repo_uuid) / "segments"

    @classmethod
    def _segment_path(cls, repo_uuid: str, sequence: int) -> Path:
        return cls._segments_directory(repo_uuid) / f"{sequence:020d}.gwf"

    def _segment_names(
        self,
        repo_uuid: str,
        *,
        ignore_atomic_temps: bool = False,
        deadline_ns: int | None = None,
    ) -> list[tuple[int, Path]]:
        relative = self._segments_directory(repo_uuid)
        directory = self.state.path(relative)
        result: list[tuple[int, Path]] = []
        try:
            _require_stable_read_deadline(deadline_ns)
            if not self.state.private_directory_exists(relative):
                return []
            with self.state.existing_private_directory(relative) as descriptor:
                with os.scandir(descriptor) as entries:
                    names = []
                    for entry in entries:
                        _require_stable_read_deadline(deadline_ns)
                        names.append(entry.name)
                    names.sort()
                for name in names:
                    _require_stable_read_deadline(deadline_ns)
                    match = _SEGMENT_RE.fullmatch(name)
                    if (
                        match is None
                        and ignore_atomic_temps
                        and _ATOMIC_SEGMENT_TEMP_RE.fullmatch(name) is not None
                    ):
                        continue
                    entry_details = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        match is None
                        or not stat.S_ISREG(entry_details.st_mode)
                        or entry_details.st_nlink != 1
                    ):
                        raise JournalCorrupt(
                            f"unexpected journal segment entry: {name}"
                        )
                    result.append(
                        (int(match.group("sequence")), directory / name)
                    )
        except JournalCorrupt:
            raise
        except (OSError, StatePathError) as exc:
            raise JournalCorrupt(
                f"journal segments cannot be enumerated safely: {directory}: {exc}"
            ) from exc
        result.sort(key=lambda item: item[0])
        if [sequence for sequence, _path in result] != list(range(1, len(result) + 1)):
            raise JournalCorrupt("journal segment sequence is not contiguous from one")
        return result

    @staticmethod
    def _validate_lifecycle(
        events: list[JournalEvent],
        *,
        deadline_ns: int | None = None,
    ) -> None:
        latest: dict[str, str] = {}
        latest_event: dict[str, JournalEvent] = {}
        certified: set[str] = set()
        logical: dict[tuple[str, str, int, int], bytes] = {}
        event_ids: dict[str, bytes] = {}
        for event in events:
            _require_stable_read_deadline(deadline_ns)
            value = event.to_dict()
            generation_id = str(value["generation_id"])
            transition = str(value["transition"])
            key = (
                generation_id,
                transition,
                int(value["operation_epoch"]),
                int(value["fence_token"]),
            )
            prior_logical = logical.setdefault(key, event.canonical)
            if prior_logical != event.canonical:
                raise JournalCorrupt("journal contains divergent duplicate logical events")
            event_id = str(value["event_id"])
            prior_event_id = event_ids.setdefault(event_id, event.canonical)
            if prior_event_id != event.canonical:
                raise JournalCorrupt("journal event identity is reused for different bytes")
            prior = latest.get(generation_id)
            if transition in _PRECERTIFICATION or transition == "CERTIFIED":
                allowed = _PRECERTIFICATION_NEXT.get(prior, frozenset())
                if transition not in allowed:
                    raise JournalCorrupt(
                        f"invalid lifecycle transition for {generation_id}: {prior} -> {transition}"
                    )
                if transition == prior == "VALIDATING":
                    prior_value = latest_event[generation_id].to_dict()
                    if (
                        int(value["operation_epoch"])
                        <= int(prior_value["operation_epoch"])
                        or int(value["fence_token"])
                        <= int(prior_value["fence_token"])
                    ):
                        raise JournalCorrupt(
                            "revalidation requires a strictly newer operation and fence"
                        )
                if transition == "CERTIFIED":
                    certified.add(generation_id)
            elif generation_id not in certified:
                raise JournalCorrupt(
                    f"post-certification transition precedes CERTIFIED for {generation_id}"
                )
            latest[generation_id] = transition
            latest_event[generation_id] = event

    def _decode_segment(
        self,
        relative: Path,
        *,
        existing_only: bool = False,
        deadline_ns: int | None = None,
    ) -> tuple[bytes, JournalEvent]:
        _require_stable_read_deadline(deadline_ns)
        try:
            frame = (
                self.state.read_existing_bytes(
                    relative,
                    max_bytes=_MAX_JOURNAL_FRAME_BYTES,
                    deadline_ns=deadline_ns,
                )
                if existing_only
                else self.state.read_bytes(
                    relative,
                    max_bytes=_MAX_JOURNAL_FRAME_BYTES,
                    deadline_ns=deadline_ns,
                )
            )
        except StateCorrupt as exc:
            raise JournalCorrupt(
                f"journal segment cannot be read safely: {relative}: {exc}"
            ) from exc
        try:
            event = decode_journal_frame(frame)
        except JournalFrameTruncated:
            raise
        except Exception as exc:
            raise JournalCorrupt(f"journal segment is corrupt: {relative}: {exc}") from exc
        _require_stable_read_deadline(deadline_ns)
        return frame, event

    @staticmethod
    def _head_for(repo_uuid: str, event: JournalEvent, frame: bytes) -> JournalHeadState:
        value = event.to_dict()
        sequence = int(value["sequence"])
        return JournalHeadState.from_mapping(
            {
                "contract": "graphify.workspace.journal_head.internal",
                "format_version": 1,
                "repo_uuid": repo_uuid,
                "revision": sequence,
                "sequence": sequence,
                "event_id": value["event_id"],
                "event_sha256": event.sha256,
                "segment_name": f"{sequence:020d}.gwf",
                "segment_sha256": hashlib.sha256(frame).hexdigest(),
            }
        )

    def _commit_head(
        self,
        repo_uuid: str,
        head: JournalHeadState,
        *,
        label: str,
        deadline_ns: int | None = None,
    ) -> None:
        current, previous, pending = self._head_paths(repo_uuid)
        self.state.commit_record(
            label=label,
            current=current,
            previous=previous,
            pending=pending,
            payload=head.canonical,
            decoder=JournalHeadState.from_json,
            deadline_ns=deadline_ns,
        )

    def project_recovery(
        self,
        repo_uuid: str,
        *,
        allow_atomic_temps: bool = False,
        deadline_ns: int | None = None,
    ) -> JournalRecoveryProjection:
        """Project the exact bounded journal recovery without mutating state."""

        current, previous, pending = self._head_paths(repo_uuid)
        head_temps = self.state.inspect_atomic_temps(
            current.parent,
            deadline_ns=deadline_ns,
        )
        segment_temps = self.state.inspect_atomic_temps(
            self._segments_directory(repo_uuid),
            deadline_ns=deadline_ns,
        )
        if not allow_atomic_temps and (head_temps or segment_temps):
            raise JournalCorrupt("journal atomic temporary files require legacy fenced recovery")
        head_record_sha256: dict[str, str | None] = {}
        for name, relative in (
            ("current", current),
            ("previous", previous),
            ("pending", pending),
        ):
            data = self.state.read_optional_existing_bytes(
                relative,
                max_bytes=64 * 1024,
                deadline_ns=deadline_ns,
            )
            head_record_sha256[name] = None if data is None else hashlib.sha256(data).hexdigest()
        try:
            head_projection = self.state.project_record_recovery(
                label="journal_head",
                current=current,
                previous=previous,
                pending=pending,
                decoder=JournalHeadState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
                deadline_ns=deadline_ns,
            )
        except StateCorrupt as exc:
            raise JournalCorrupt(str(exc)) from exc
        head = head_projection.record
        if head is not None and head.repo_uuid != repo_uuid:
            raise JournalCorrupt("journal head is installed under the wrong workspace")

        actions: list[str] = []
        if head_projection.requires_recovery:
            actions.append("recover_head")
        segments = self._segment_names(
            repo_uuid,
            ignore_atomic_temps=allow_atomic_temps,
            deadline_ns=deadline_ns,
        )
        committed = 0 if head is None else head.sequence
        if len(segments) < committed:
            raise JournalCorrupt("journal head names a missing committed segment")
        if len(segments) > committed + 1:
            raise JournalCorrupt("journal has ambiguous uncommitted segment suffix")
        segment_sha256 = {
            str(sequence): hashlib.sha256(
                self.state.read_existing_bytes(
                    self._segment_path(repo_uuid, sequence),
                    max_bytes=_MAX_JOURNAL_FRAME_BYTES,
                    deadline_ns=deadline_ns,
                )
            ).hexdigest()
            for sequence, _path in segments
        }

        events: list[JournalEvent] = []
        frames: list[bytes] = []
        prior_sha256: str | None = None
        for sequence, _path in segments[:committed]:
            relative = self._segment_path(repo_uuid, sequence)
            try:
                frame, event = self._decode_segment(
                    relative,
                    existing_only=True,
                    deadline_ns=deadline_ns,
                )
            except JournalFrameTruncated as exc:
                raise JournalCorrupt("committed journal segment is truncated") from exc
            value = event.to_dict()
            self._require_repo_event_id(repo_uuid, event)
            if int(value["sequence"]) != sequence:
                raise JournalCorrupt("journal frame sequence does not match its segment")
            if value["prior_event_sha256"] != prior_sha256:
                raise JournalCorrupt("journal prior-event hash chain is broken")
            prior_sha256 = event.sha256
            frames.append(frame)
            events.append(event)

        if head is not None:
            if not events:
                raise JournalCorrupt("journal head names a missing committed segment")
            expected = self._head_for(repo_uuid, events[-1], frames[-1])
            if expected.canonical != head.canonical:
                raise JournalCorrupt("journal head does not bind the committed segment")

        if len(segments) == committed + 1:
            sequence = committed + 1
            relative = self._segment_path(repo_uuid, sequence)
            try:
                frame, event = self._decode_segment(
                    relative,
                    existing_only=True,
                    deadline_ns=deadline_ns,
                )
            except JournalFrameTruncated:
                actions.append("discard_torn_tail")
            else:
                value = event.to_dict()
                self._require_repo_event_id(repo_uuid, event)
                if (
                    int(value["sequence"]) != sequence
                    or value["prior_event_sha256"] != prior_sha256
                ):
                    raise JournalCorrupt("uncommitted journal segment does not extend the head")
                events.append(event)
                head = self._head_for(repo_uuid, event, frame)
                actions.append("adopt_tail")

        self._validate_lifecycle(events, deadline_ns=deadline_ns)
        return JournalRecoveryProjection(
            snapshot=JournalSnapshot(head=head, events=tuple(events)),
            actions=tuple(actions),
            evidence_sha256=hashlib.sha256(
                canonical_json_bytes(
                    {
                        "head_record_sha256": head_record_sha256,
                        "segment_sha256": segment_sha256,
                    }
                )
            ).hexdigest(),
        )

    def recover_locked(
        self,
        operation: LeaseOperation,
        *,
        deadline_ns: int | None = None,
    ) -> JournalSnapshot:
        """Reconcile sealed segments while the caller holds the operation guard."""

        repo_uuid = operation.repo_uuid
        current, previous, pending = self._head_paths(repo_uuid)
        try:
            head = self.state.recover_record(
                label="journal_head",
                current=current,
                previous=previous,
                pending=pending,
                decoder=JournalHeadState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
                deadline_ns=deadline_ns,
            )
        except StateCorrupt as exc:
            raise JournalCorrupt(str(exc)) from exc
        if head is not None and head.repo_uuid != repo_uuid:
            raise JournalCorrupt("journal head is installed under the wrong workspace")

        self.state.cleanup_atomic_temps(
            self._segments_directory(repo_uuid),
            deadline_ns=deadline_ns,
        )
        segments = self._segment_names(repo_uuid, deadline_ns=deadline_ns)
        committed = 0 if head is None else head.sequence
        if len(segments) < committed:
            raise JournalCorrupt("journal head names a missing committed segment")
        if len(segments) > committed + 1:
            raise JournalCorrupt("journal has ambiguous uncommitted segment suffix")

        events: list[JournalEvent] = []
        frames: list[bytes] = []
        prior_sha256: str | None = None
        for sequence, _path in segments[:committed]:
            relative = self._segment_path(repo_uuid, sequence)
            try:
                frame, event = self._decode_segment(
                    relative,
                    deadline_ns=deadline_ns,
                )
            except JournalFrameTruncated as exc:
                raise JournalCorrupt("committed journal segment is truncated") from exc
            value = event.to_dict()
            self._require_repo_event_id(repo_uuid, event)
            if int(value["sequence"]) != sequence:
                raise JournalCorrupt("journal frame sequence does not match its segment")
            if value["prior_event_sha256"] != prior_sha256:
                raise JournalCorrupt("journal prior-event hash chain is broken")
            prior_sha256 = event.sha256
            frames.append(frame)
            events.append(event)

        if head is not None:
            final_event = events[-1]
            final_frame = frames[-1]
            expected = self._head_for(repo_uuid, final_event, final_frame)
            if expected.canonical != head.canonical:
                raise JournalCorrupt("journal head does not bind the committed segment")

        if len(segments) == committed + 1:
            sequence = committed + 1
            relative = self._segment_path(repo_uuid, sequence)
            try:
                frame, event = self._decode_segment(
                    relative,
                    deadline_ns=deadline_ns,
                )
            except JournalFrameTruncated:
                require_before_deadline(
                    deadline_ns,
                    "journal recovery exceeded its deadline",
                )
                self.state.unlink_and_sync(
                    relative,
                    label="journal:torn_tail",
                    deadline_ns=deadline_ns,
                )
                self.fault_hook("journal:torn_tail:discarded")
            else:
                value = event.to_dict()
                self._require_repo_event_id(repo_uuid, event)
                if (
                    int(value["sequence"]) != sequence
                    or value["prior_event_sha256"] != prior_sha256
                ):
                    raise JournalCorrupt("uncommitted journal segment does not extend the head")
                events.append(event)
                frames.append(frame)
                head = self._head_for(repo_uuid, event, frame)
                self._commit_head(
                    repo_uuid,
                    head,
                    label="journal_head_recovery",
                    deadline_ns=deadline_ns,
                )
                self.fault_hook("journal:head_recovered")

        self._validate_lifecycle(events, deadline_ns=deadline_ns)
        return JournalSnapshot(head=head, events=tuple(events))

    def read_stable(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None = None,
    ) -> JournalSnapshot:
        """Read only fully committed journal authority without recovery writes."""

        current, previous, pending = self._head_paths(repo_uuid)
        try:
            _require_stable_read_deadline(deadline_ns)
            head = self.state.read_stable_record(
                label="journal_head",
                current=current,
                previous=previous,
                pending=pending,
                decoder=JournalHeadState.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
                deadline_ns=deadline_ns,
            )
            _require_stable_read_deadline(deadline_ns)
        except LockTimeout as exc:
            raise LockTimeout(
                "journal stable read exceeded its deadline",
                phase=exc.phase,
                kind=exc.kind,
            ) from exc
        except StateRecoveryRequired as exc:
            projection = self.project_recovery(
                repo_uuid,
                allow_atomic_temps=False,
                deadline_ns=deadline_ns,
            )
            if "recover_head" not in projection.actions:  # pragma: no cover - pending invariant
                raise JournalCorrupt("journal head requires unsupported recovery") from exc
            raise JournalRecoveryRequired(
                "journal head requires recovery before stable read"
            ) from exc
        except (StateCorrupt, StatePathError) as exc:
            raise JournalCorrupt(str(exc)) from exc
        if head is not None and head.repo_uuid != repo_uuid:
            raise JournalCorrupt("journal head is installed under the wrong workspace")

        segments = self._segment_names(repo_uuid, deadline_ns=deadline_ns)
        committed = 0 if head is None else head.sequence
        if len(segments) != committed:
            projection = self.project_recovery(
                repo_uuid,
                allow_atomic_temps=False,
                deadline_ns=deadline_ns,
            )
            if not projection.actions:  # pragma: no cover - segment-count invariant
                raise JournalCorrupt("journal segment count changed without a recovery action")
            raise JournalRecoveryRequired("journal requires recovery before stable read")

        events: list[JournalEvent] = []
        frames: list[bytes] = []
        prior_sha256: str | None = None
        for sequence, _path in segments:
            relative = self._segment_path(repo_uuid, sequence)
            try:
                frame, event = self._decode_segment(
                    relative,
                    existing_only=True,
                    deadline_ns=deadline_ns,
                )
            except JournalFrameTruncated as exc:
                raise JournalCorrupt("committed journal segment is truncated") from exc
            value = event.to_dict()
            self._require_repo_event_id(repo_uuid, event)
            if int(value["sequence"]) != sequence:
                raise JournalCorrupt("journal frame sequence does not match its segment")
            if value["prior_event_sha256"] != prior_sha256:
                raise JournalCorrupt("journal prior-event hash chain is broken")
            prior_sha256 = event.sha256
            frames.append(frame)
            events.append(event)

        if head is not None:
            _require_stable_read_deadline(deadline_ns)
            expected = self._head_for(repo_uuid, events[-1], frames[-1])
            if expected.canonical != head.canonical:
                raise JournalCorrupt("journal head does not bind the committed segment")
        self._validate_lifecycle(events, deadline_ns=deadline_ns)
        _require_stable_read_deadline(deadline_ns)
        return JournalSnapshot(head=head, events=tuple(events))

    def recover(self, grant: LeaseGrant, *, monotonic_ns: int) -> JournalSnapshot:
        with self.leases.current_operation(grant, monotonic_ns=monotonic_ns) as operation:
            return self.recover_locked(operation)

    @staticmethod
    def _event_id(repo_uuid: str, logical: dict[str, Any]) -> str:
        material = hashlib.sha256(canonical_json_bytes(logical)).hexdigest()
        return str(uuid.uuid5(_EVENT_NAMESPACE, f"{repo_uuid}:{material}"))

    def _require_repo_event_id(self, repo_uuid: str, event: JournalEvent) -> None:
        value = event.to_dict()
        logical = {
            key: value[key]
            for key in (
                "transition",
                "generation_id",
                "receipt_sha256",
                "pointer_revision",
                "operation_epoch",
                "fence_token",
                "occurred_at",
            )
        }
        if value["event_id"] != self._event_id(repo_uuid, logical):
            raise JournalCorrupt("journal event id is not bound to its workspace")

    def _require_transition_authority(
        self,
        operation: LeaseOperation,
        *,
        transition: str,
        generation_id: str,
        receipt_sha256: str | None,
        pointer_revision: int | None,
        deadline_ns: int | None = None,
    ) -> None:
        allowed = _TRANSITION_OPERATIONS.get(transition)
        if allowed is None or operation.operation not in allowed:
            raise JournalConflict(
                f"operation {operation.operation} cannot append transition {transition}"
            )
        if transition not in _VISIBLE_POINTER_TRANSITIONS:
            return
        relative = LeaseStore._directory(operation.repo_uuid) / "pointers.json"
        try:
            pointer = PointerSet.from_json(
                self.state.read_existing_bytes(
                    relative,
                    max_bytes=_MAX_POINTER_RECORD_BYTES,
                    deadline_ns=deadline_ns,
                )
            )
        except LockTimeout:
            raise
        except Exception as exc:
            raise JournalConflict(
                f"{transition} requires a valid visible pointer: {exc}"
            ) from exc
        value = pointer.to_dict()
        current = cast(dict[str, Any], value["current"])
        if (
            value["repo_uuid"] != operation.repo_uuid
            or value["pointer_revision"] != pointer_revision
            or value["operation_epoch"] != operation.grant.operation_epoch
            or value["fence_token"] != operation.fence_token
            or current["generation_id"] != generation_id
            or current["receipt_sha256"] != receipt_sha256
        ):
            raise JournalConflict(
                f"{transition} does not match the visible pointer replacement"
            )

    def append_locked(
        self,
        operation: LeaseOperation,
        *,
        transition: str,
        generation_id: str,
        receipt_sha256: str | None,
        pointer_revision: int | None,
        occurred_at: datetime,
        _authority: object | None = None,
        deadline_ns: int | None = None,
    ) -> JournalEvent:
        """Append one canonical event under its owning operation.

        ``occurred_at`` is part of the event's canonical logical identity. A
        retry under the same operation epoch and fence must therefore reuse the
        exact timestamp; a successor fence records a distinct event identity.
        """

        allowed = _TRANSITION_OPERATIONS.get(transition)
        if allowed is None or operation.operation not in allowed:
            raise JournalConflict(
                f"operation {operation.operation} cannot append transition {transition}"
            )
        if transition == "CERTIFIED" and _authority is not self._generation_authority:
            raise JournalConflict("CERTIFIED must be appended by GenerationStore")
        if (
            transition in {"PROMOTED", "ROLLED_BACK", "REPAIRED", "SUPERSEDED"}
            and _authority is not self._pointer_authority
        ):
            raise JournalConflict(f"{transition} must be appended by PointerStore")
        self._require_transition_authority(
            operation,
            transition=transition,
            generation_id=generation_id,
            receipt_sha256=receipt_sha256,
            pointer_revision=pointer_revision,
            deadline_ns=deadline_ns,
        )
        snapshot = self.recover_locked(operation, deadline_ns=deadline_ns)
        logical = {
            "transition": transition,
            "generation_id": generation_id,
            "receipt_sha256": receipt_sha256,
            "pointer_revision": pointer_revision,
            "operation_epoch": operation.grant.operation_epoch,
            "fence_token": operation.fence_token,
            "occurred_at": _timestamp(occurred_at),
        }
        event_id = self._event_id(operation.repo_uuid, logical)
        for existing in snapshot.events:
            value = existing.to_dict()
            if (
                value["generation_id"] == generation_id
                and value["transition"] == transition
                and value["operation_epoch"] == operation.grant.operation_epoch
                and value["fence_token"] == operation.fence_token
            ):
                desired = {
                    **logical,
                    "event_id": value["event_id"],
                }
                actual = {
                    key: value[key]
                    for key in (
                        "transition",
                        "generation_id",
                        "receipt_sha256",
                        "pointer_revision",
                        "operation_epoch",
                        "fence_token",
                        "occurred_at",
                        "event_id",
                    )
                }
                if desired != actual or event_id != value["event_id"]:
                    raise JournalConflict(
                        f"logical event {generation_id}/{transition} already has different bytes"
                    )
                return existing

        sequence = len(snapshot.events) + 1
        prior_sha256 = snapshot.events[-1].sha256 if snapshot.events else None
        event = cast(
            JournalEvent,
            JournalEvent.from_mapping(
                {
                    "contract": "graphify.workspace.journal_event",
                    "schema_version": 1,
                    "event_id": event_id,
                    "sequence": sequence,
                    "transition": transition,
                    "generation_id": generation_id,
                    "prior_event_sha256": prior_sha256,
                    "receipt_sha256": receipt_sha256,
                    "pointer_revision": pointer_revision,
                    "operation_epoch": operation.grant.operation_epoch,
                    "fence_token": operation.fence_token,
                    "occurred_at": logical["occurred_at"],
                }
            ),
        )
        proposed = [*snapshot.events, event]
        self._validate_lifecycle(proposed, deadline_ns=deadline_ns)
        frame = encode_journal_frame(event)
        segment = self._segment_path(operation.repo_uuid, sequence)
        label = f"journal:{transition}:segment"
        self.state.install_once_bytes(
            segment,
            frame,
            label=label,
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"journal:{transition}:segment_durable")
        head = self._head_for(operation.repo_uuid, event, frame)
        self._commit_head(
            operation.repo_uuid,
            head,
            label=f"journal:{transition}:head",
            deadline_ns=deadline_ns,
        )
        self.fault_hook(f"journal:{transition}:head_durable")
        return event

    def append_generation_locked(
        self,
        operation: LeaseOperation,
        *,
        transition: str,
        generation_id: str,
        receipt_sha256: str | None,
        pointer_revision: int | None,
        occurred_at: datetime,
        deadline_ns: int | None = None,
    ) -> JournalEvent:
        if transition not in _PRECERTIFICATION | {"CERTIFIED"}:
            raise JournalConflict(f"{transition} is not a generation transition")
        return self.append_locked(
            operation,
            transition=transition,
            generation_id=generation_id,
            receipt_sha256=receipt_sha256,
            pointer_revision=pointer_revision,
            occurred_at=occurred_at,
            _authority=self._generation_authority,
            deadline_ns=deadline_ns,
        )

    def append_pointer_locked(
        self,
        operation: LeaseOperation,
        *,
        transition: str,
        generation_id: str,
        receipt_sha256: str,
        pointer_revision: int,
        occurred_at: datetime,
        deadline_ns: int | None = None,
    ) -> JournalEvent:
        if transition not in {"PROMOTED", "ROLLED_BACK", "REPAIRED", "SUPERSEDED"}:
            raise JournalConflict(f"{transition} is not a pointer transition")
        return self.append_locked(
            operation,
            transition=transition,
            generation_id=generation_id,
            receipt_sha256=receipt_sha256,
            pointer_revision=pointer_revision,
            occurred_at=occurred_at,
            _authority=self._pointer_authority,
            deadline_ns=deadline_ns,
        )

    def append(
        self,
        grant: LeaseGrant,
        *,
        transition: str,
        generation_id: str,
        receipt_sha256: str | None,
        pointer_revision: int | None,
        occurred_at: datetime,
        monotonic_ns: int,
    ) -> JournalEvent:
        with self.leases.current_operation(grant, monotonic_ns=monotonic_ns) as operation:
            return self.append_locked(
                operation,
                transition=transition,
                generation_id=generation_id,
                receipt_sha256=receipt_sha256,
                pointer_revision=pointer_revision,
                occurred_at=occurred_at,
            )


__all__ = [
    "JournalConflict",
    "JournalCorrupt",
    "JournalError",
    "JournalRecoveryProjection",
    "JournalRecoveryRequired",
    "JournalSnapshot",
    "JournalStore",
]
