"""Crash-durable framed lifecycle journal for immutable workspace generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import stat
from typing import Any, cast
import uuid

from graphify.workspace.contracts import (
    JournalEvent,
    JournalFrameTruncated,
    JournalHeadState,
    canonical_json_bytes,
    decode_journal_frame,
    encode_journal_frame,
)
from graphify.workspace.leases import LeaseGrant, LeaseOperation, LeaseStore
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    Syscalls,
)


_SEGMENT_RE = re.compile(r"^(?P<sequence>[0-9]{20})\.gwf$", re.ASCII)
_EVENT_NAMESPACE = uuid.UUID("3924cb61-439f-49d0-8a8c-b3753d140d5e")
_PRECERTIFICATION = frozenset({"ALLOCATED", "STAGING", "BUILT", "VALIDATING", "FAILED"})
_PRECERTIFICATION_NEXT = {
    None: frozenset({"ALLOCATED"}),
    "ALLOCATED": frozenset({"STAGING", "FAILED"}),
    "STAGING": frozenset({"BUILT", "FAILED"}),
    "BUILT": frozenset({"VALIDATING", "FAILED"}),
    "VALIDATING": frozenset({"CERTIFIED", "FAILED"}),
}


class JournalError(RuntimeError):
    """Base class for stable journal failures."""

    code = "journal_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class JournalCorrupt(JournalError):
    code = "journal_corrupt"


class JournalConflict(JournalError):
    code = "journal_conflict"


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

    def _segment_names(self, repo_uuid: str) -> list[tuple[int, Path]]:
        directory = self.state.path(self._segments_directory(repo_uuid))
        if not directory.exists():
            return []
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or directory.is_symlink():
            raise JournalCorrupt("journal segments path is not a real directory")
        result: list[tuple[int, Path]] = []
        for entry in directory.iterdir():
            match = _SEGMENT_RE.fullmatch(entry.name)
            entry_details = entry.lstat()
            if (
                match is None
                or not stat.S_ISREG(entry_details.st_mode)
                or entry_details.st_nlink != 1
                or entry.is_symlink()
            ):
                raise JournalCorrupt(f"unexpected journal segment entry: {entry.name}")
            result.append((int(match.group("sequence")), entry))
        result.sort(key=lambda item: item[0])
        if [sequence for sequence, _path in result] != list(range(1, len(result) + 1)):
            raise JournalCorrupt("journal segment sequence is not contiguous from one")
        return result

    @staticmethod
    def _validate_lifecycle(events: list[JournalEvent]) -> None:
        latest: dict[str, str] = {}
        certified: set[str] = set()
        logical: dict[tuple[str, str, int, int], bytes] = {}
        event_ids: dict[str, bytes] = {}
        for event in events:
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
                if transition == "CERTIFIED":
                    certified.add(generation_id)
            elif generation_id not in certified:
                raise JournalCorrupt(
                    f"post-certification transition precedes CERTIFIED for {generation_id}"
                )
            latest[generation_id] = transition

    def _decode_segment(self, relative: Path) -> tuple[bytes, JournalEvent]:
        frame = self.state.read_bytes(relative)
        try:
            event = decode_journal_frame(frame)
        except JournalFrameTruncated:
            raise
        except Exception as exc:
            raise JournalCorrupt(f"journal segment is corrupt: {relative}: {exc}") from exc
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

    def _commit_head(self, repo_uuid: str, head: JournalHeadState, *, label: str) -> None:
        current, previous, pending = self._head_paths(repo_uuid)
        self.state.commit_record(
            label=label,
            current=current,
            previous=previous,
            pending=pending,
            payload=head.canonical,
            decoder=JournalHeadState.from_json,
        )

    def recover_locked(self, operation: LeaseOperation) -> JournalSnapshot:
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
            )
        except StateCorrupt as exc:
            raise JournalCorrupt(str(exc)) from exc
        if head is not None and head.repo_uuid != repo_uuid:
            raise JournalCorrupt("journal head is installed under the wrong workspace")

        segments = self._segment_names(repo_uuid)
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
                frame, event = self._decode_segment(relative)
            except JournalFrameTruncated as exc:
                raise JournalCorrupt("committed journal segment is truncated") from exc
            value = event.to_dict()
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
                frame, event = self._decode_segment(relative)
            except JournalFrameTruncated:
                self.state.unlink_and_sync(relative, label="journal:torn_tail")
                self.fault_hook("journal:torn_tail:discarded")
            else:
                value = event.to_dict()
                if (
                    int(value["sequence"]) != sequence
                    or value["prior_event_sha256"] != prior_sha256
                ):
                    raise JournalCorrupt("uncommitted journal segment does not extend the head")
                events.append(event)
                frames.append(frame)
                head = self._head_for(repo_uuid, event, frame)
                self._commit_head(repo_uuid, head, label="journal_head_recovery")
                self.fault_hook("journal:head_recovered")

        self._validate_lifecycle(events)
        return JournalSnapshot(head=head, events=tuple(events))

    def recover(self, grant: LeaseGrant, *, monotonic_ns: int) -> JournalSnapshot:
        with self.leases.current_operation(grant, monotonic_ns=monotonic_ns) as operation:
            return self.recover_locked(operation)

    @staticmethod
    def _event_id(repo_uuid: str, logical: dict[str, Any]) -> str:
        material = hashlib.sha256(canonical_json_bytes(logical)).hexdigest()
        return str(uuid.uuid5(_EVENT_NAMESPACE, f"{repo_uuid}:{material}"))

    def append_locked(
        self,
        operation: LeaseOperation,
        *,
        transition: str,
        generation_id: str,
        receipt_sha256: str | None,
        pointer_revision: int | None,
        occurred_at: datetime,
    ) -> JournalEvent:
        snapshot = self.recover_locked(operation)
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
        self._validate_lifecycle(proposed)
        frame = encode_journal_frame(event)
        segment = self._segment_path(operation.repo_uuid, sequence)
        label = f"journal:{transition}:segment"
        self.state.install_once_bytes(segment, frame, label=label)
        self.fault_hook(f"journal:{transition}:segment_durable")
        head = self._head_for(operation.repo_uuid, event, frame)
        self._commit_head(
            operation.repo_uuid,
            head,
            label=f"journal:{transition}:head",
        )
        self.fault_hook(f"journal:{transition}:head_durable")
        return event

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
    "JournalSnapshot",
    "JournalStore",
]
