from __future__ import annotations

from datetime import timedelta
import errno
from pathlib import Path
from typing import cast
import uuid

import pytest

from graphify.workspace.contracts import JournalEvent, encode_journal_frame
from graphify.workspace.journal import JournalConflict, JournalCorrupt, JournalStore
from graphify.workspace.leases import LeaseGrant
from graphify.workspace.persistence import CommitUnknown, InjectedFault, PosixSyscalls

from tests.workspace_p3_helpers import REPO_UUID, START, acquire, create_harness


class _FailOnceSyscalls(PosixSyscalls):
    def __init__(self, operation: str, error_number: int) -> None:
        self.operation = operation
        self.error_number = error_number
        self.failed = False

    def _fail(self, operation: str) -> None:
        if not self.failed and self.operation == operation:
            self.failed = True
            raise OSError(self.error_number, f"injected {operation}")

    def write(self, descriptor: int, data: memoryview) -> int:
        self._fail("write")
        return super().write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        self._fail("fsync")
        super().fsync(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        self._fail("replace")
        super().replace(source, destination)


class _ShortWriteEintrSyscalls(PosixSyscalls):
    def __init__(self) -> None:
        self.interrupted = False

    def write(self, descriptor: int, data: memoryview) -> int:
        if not self.interrupted:
            self.interrupted = True
            raise InterruptedError(errno.EINTR, "injected EINTR")
        return super().write(descriptor, data[: max(1, len(data) // 4)])


class _FailSecondFsyncSyscalls(PosixSyscalls):
    def __init__(self) -> None:
        self.calls = 0

    def fsync(self, descriptor: int) -> None:
        self.calls += 1
        if self.calls == 2:
            raise OSError(errno.EIO, "injected directory fsync")
        super().fsync(descriptor)


def _append_allocated(
    store: JournalStore,
    grant: LeaseGrant,
    *,
    monotonic_ns: int = 10_001,
):
    return store.append(
        grant,
        transition="ALLOCATED",
        generation_id="gen-journal",
        receipt_sha256=None,
        pointer_revision=None,
        occurred_at=START,
        monotonic_ns=monotonic_ns,
    )


def _segment(root: Path, sequence: int) -> Path:
    return (
        root
        / "workspaces"
        / REPO_UUID
        / "journal"
        / "segments"
        / f"{sequence:020d}.gwf"
    )


def test_journal_append_is_idempotent_and_rejects_divergent_logical_retry(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )

    first = _append_allocated(store, grant)
    assert _append_allocated(store, grant).canonical == first.canonical
    with pytest.raises(JournalConflict, match="different bytes"):
        store.append(
            grant,
            transition="ALLOCATED",
            generation_id="gen-journal",
            receipt_sha256=None,
            pointer_revision=None,
            occurred_at=START + timedelta(seconds=1),
            monotonic_ns=10_002,
        )


def test_journal_discards_only_one_uncommitted_torn_tail(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(store, grant)
    tail = _segment(harness.state_root, 2)
    tail.write_bytes(b"GWF1")
    tail.chmod(0o600)

    snapshot = store.recover(grant, monotonic_ns=10_002)

    assert len(snapshot.events) == 1
    assert not tail.exists()


def test_journal_adopts_one_complete_hash_linked_uncommitted_segment(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    first = _append_allocated(store, grant)
    second = cast(JournalEvent, JournalEvent.from_mapping(
        {
            "contract": "graphify.workspace.journal_event",
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "sequence": 2,
            "transition": "STAGING",
            "generation_id": "gen-journal",
            "prior_event_sha256": first.sha256,
            "receipt_sha256": None,
            "pointer_revision": None,
            "operation_epoch": grant.operation_epoch,
            "fence_token": grant.lease.to_dict()["fence_token"],
            "occurred_at": "2026-07-16T19:00:00Z",
        }
    ))
    tail = _segment(harness.state_root, 2)
    tail.write_bytes(encode_journal_frame(second))
    tail.chmod(0o600)

    snapshot = store.recover(grant, monotonic_ns=10_002)

    assert [event.to_dict()["transition"] for event in snapshot.events] == [
        "ALLOCATED",
        "STAGING",
    ]
    assert snapshot.head is not None and snapshot.head.sequence == 2


def test_every_strict_frame_prefix_is_classified_as_one_torn_tail(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    first = _append_allocated(store, grant)
    second = cast(
        JournalEvent,
        JournalEvent.from_mapping(
            {
                "contract": "graphify.workspace.journal_event",
                "schema_version": 1,
                "event_id": str(uuid.uuid4()),
                "sequence": 2,
                "transition": "STAGING",
                "generation_id": "gen-journal",
                "prior_event_sha256": first.sha256,
                "receipt_sha256": None,
                "pointer_revision": None,
                "operation_epoch": grant.operation_epoch,
                "fence_token": grant.lease.to_dict()["fence_token"],
                "occurred_at": "2026-07-16T19:00:00Z",
            }
        ),
    )
    frame = encode_journal_frame(second)
    tail = _segment(harness.state_root, 2)

    for cut in range(len(frame)):
        tail.write_bytes(frame[:cut])
        tail.chmod(0o600)
        snapshot = store.recover(grant, monotonic_ns=10_002 + cut)
        assert len(snapshot.events) == 1
        assert not tail.exists()

    tail.write_bytes(frame)
    tail.chmod(0o600)
    completed = store.recover(grant, monotonic_ns=20_000)
    assert completed.head is not None and completed.head.sequence == 2


def test_journal_rejects_committed_corruption_and_ambiguous_suffix(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(store, grant)
    committed = _segment(harness.state_root, 1)
    original = committed.read_bytes()
    committed.write_bytes(original[:-1])
    committed.chmod(0o600)
    with pytest.raises(JournalCorrupt, match="committed.*truncated"):
        store.recover(grant, monotonic_ns=10_002)

    committed.write_bytes(original)
    committed.chmod(0o600)
    for sequence in (2, 3):
        extra = _segment(harness.state_root, sequence)
        extra.write_bytes(b"GWF1")
        extra.chmod(0o600)
    with pytest.raises(JournalCorrupt, match="ambiguous"):
        store.recover(grant, monotonic_ns=10_003)


def test_segment_visibility_failure_is_commit_unknown_and_recoverable(tmp_path: Path) -> None:
    armed = True

    def fail(event: str) -> None:
        nonlocal armed
        if armed and event == "journal:ALLOCATED:segment:installed":
            armed = False
            raise InjectedFault(event)

    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        fault_hook=fail,
    )
    with pytest.raises(CommitUnknown):
        _append_allocated(store, grant)

    snapshot = store.recover(grant, monotonic_ns=10_002)
    assert [event.to_dict()["transition"] for event in snapshot.events] == ["ALLOCATED"]


@pytest.mark.parametrize(
    ("operation", "error_number"),
    [
        ("write", errno.ENOSPC),
        ("write", errno.EDQUOT),
        ("write", errno.EIO),
        ("fsync", errno.EIO),
        ("replace", errno.EIO),
    ],
)
def test_journal_previsibility_syscall_failures_retry_without_ambiguity(
    tmp_path: Path,
    operation: str,
    error_number: int,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    baseline = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(baseline, grant)
    syscalls = _FailOnceSyscalls(operation, error_number)
    faulty = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        syscalls=syscalls,
    )
    with pytest.raises(OSError) as raised:
        faulty.append(
            grant,
            transition="STAGING",
            generation_id="gen-journal",
            receipt_sha256=None,
            pointer_revision=None,
            occurred_at=START,
            monotonic_ns=10_002,
        )
    assert raised.value.errno == error_number
    event = faulty.append(
        grant,
        transition="STAGING",
        generation_id="gen-journal",
        receipt_sha256=None,
        pointer_revision=None,
        occurred_at=START,
        monotonic_ns=10_003,
    )
    assert event.to_dict()["sequence"] == 2


def test_journal_handles_eintr_short_writes_and_postvisibility_fsync_uncertainty(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    baseline = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(baseline, grant)
    short = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        syscalls=_ShortWriteEintrSyscalls(),
    )
    assert short.append(
        grant,
        transition="STAGING",
        generation_id="gen-journal",
        receipt_sha256=None,
        pointer_revision=None,
        occurred_at=START,
        monotonic_ns=10_002,
    ).to_dict()["sequence"] == 2

    uncertain = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
        syscalls=_FailSecondFsyncSyscalls(),
    )
    with pytest.raises(CommitUnknown):
        uncertain.append(
            grant,
            transition="BUILT",
            generation_id="gen-journal",
            receipt_sha256=None,
            pointer_revision=None,
            occurred_at=START,
            monotonic_ns=10_003,
        )
    recovered = baseline.recover(grant, monotonic_ns=10_004)
    assert [event.to_dict()["transition"] for event in recovered.events][-1] == "BUILT"
