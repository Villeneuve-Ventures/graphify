from __future__ import annotations

from datetime import timedelta
import errno
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

import graphify.workspace.journal as journal_module

from graphify.workspace.contracts import (
    JournalEvent,
    JournalFrameTruncated,
    encode_journal_frame,
)
from graphify.workspace.journal import JournalConflict, JournalCorrupt, JournalStore
from graphify.workspace.leases import LeaseGrant
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    PosixSyscalls,
)

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

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._fail("replace")
        super().replace_at(
            source,
            destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )


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


def test_journal_rejects_transition_owned_by_another_operation(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(store, grant)

    with pytest.raises(JournalConflict, match="operation BUILD cannot append transition PROMOTED"):
        store.append(
            grant,
            transition="PROMOTED",
            generation_id="gen-journal",
            receipt_sha256="a" * 64,
            pointer_revision=1,
            occurred_at=START,
            monotonic_ns=10_002,
        )


def test_read_stable_honors_deadline_during_segment_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    for sequence in range(1, 21):
        store.append(
            grant,
            transition="ALLOCATED",
            generation_id=f"gen-deadline-{sequence}",
            receipt_sha256=None,
            pointer_revision=None,
            occurred_at=START,
            monotonic_ns=10_000 + sequence,
        )

    decoded = 0
    original_decode = store._decode_segment

    def count_decode(
        relative: Path,
        *,
        existing_only: bool = False,
        deadline_ns: int | None = None,
    ) -> tuple[bytes, JournalEvent]:
        nonlocal decoded
        decoded += 1
        return original_decode(
            relative,
            existing_only=existing_only,
            deadline_ns=deadline_ns,
        )

    monotonic_tick = 0

    def monotonic_ns() -> int:
        nonlocal monotonic_tick
        monotonic_tick += 1
        return monotonic_tick

    monkeypatch.setattr(store, "_decode_segment", count_decode)
    monkeypatch.setattr("graphify.workspace.persistence.time.monotonic_ns", monotonic_ns)

    with pytest.raises(LockTimeout, match="journal stable read exceeded its deadline"):
        store.read_stable(REPO_UUID, deadline_ns=8)

    assert decoded < 20


def test_read_stable_preserves_lock_timeout_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )

    def acquisition_timeout(*_args: object, **_kwargs: object) -> object:
        raise LockTimeout(
            "message intentionally omits the journal context",
            phase="acquire",
            kind="journal",
        )

    monkeypatch.setattr(type(store.state), "read_stable_record", acquisition_timeout)

    with pytest.raises(
        LockTimeout,
        match="journal stable read exceeded its deadline",
    ) as captured:
        store.read_stable(REPO_UUID)

    assert captured.value.phase == "acquire"
    assert captured.value.kind == "journal"


def test_decode_segment_rejects_oversized_frame_before_reading_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(store, grant)
    segment = _segment(harness.state_root, 1)
    with segment.open("r+b") as stream:
        stream.truncate((1024 * 1024) + 1)

    def unexpected_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized journal payload was read")

    monkeypatch.setattr("graphify.workspace.persistence.os.read", unexpected_read)

    with pytest.raises(JournalCorrupt, match="exceeds its read limit"):
        store._decode_segment(store._segment_path(REPO_UUID, 1), existing_only=True)


def test_decode_segment_checks_deadline_while_reading_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(store, grant)
    segment = _segment(harness.state_root, 1)
    with segment.open("ab") as stream:
        stream.write(b"x" * (128 * 1024))

    monotonic_tick = 0

    def monotonic_ns() -> int:
        nonlocal monotonic_tick
        monotonic_tick += 1
        return monotonic_tick

    monkeypatch.setattr("graphify.workspace.persistence.time.monotonic_ns", monotonic_ns)

    with pytest.raises(LockTimeout, match="state record read exceeded its deadline"):
        store._decode_segment(
            store._segment_path(REPO_UUID, 1),
            existing_only=True,
            deadline_ns=4,
        )


def test_successor_cleans_real_process_death_atomic_segment_temp(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    script = (
        "from datetime import datetime, timezone\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "from graphify.workspace.journal import JournalStore\n"
        "from graphify.workspace.leases import LeaseStore\n"
        "from graphify.workspace.persistence import PosixSyscalls, RuntimeCapabilities\n"
        "from graphify.workspace.registry import RegistryStore\n"
        "class KillWrite(PosixSyscalls):\n"
        "    def write(self, descriptor, data):\n"
        "        os._exit(91)\n"
        "root=Path(sys.argv[1]); repo_uuid=sys.argv[2]\n"
        "caps=RuntimeCapabilities.supported_test_fixture()\n"
        "registry=RegistryStore(root, capabilities=caps)\n"
        "leases=LeaseStore(root, registry, capabilities=caps)\n"
        "document=registry.load(); entry=document.to_dict()['workspaces'][0]\n"
        "state=leases.inspect(repo_uuid)\n"
        "grant=leases.acquire(repo_uuid, 'BUILD', leases.current_owner(), "
        "expected_registry_revision=int(document.to_dict()['revision']), "
        "expected_active_source_revision=int(entry['active_source_revision']), "
        "expected_operation_epoch=state.operation_epoch, "
        "expected_migration_epoch=state.migration_epoch, "
        "acquired_at=datetime(2026,7,16,19,0,tzinfo=timezone.utc), "
        "monotonic_ns=100, ttl_ns=1)\n"
        "journal=JournalStore(root, leases, capabilities=caps, syscalls=KillWrite())\n"
        "journal.append(grant, transition='ALLOCATED', generation_id='gen-killed', "
        "receipt_sha256=None, pointer_revision=None, "
        "occurred_at=datetime(2026,7,16,19,0,tzinfo=timezone.utc), monotonic_ns=100)\n"
    )
    killed = subprocess.run(
        [sys.executable, "-c", script, str(harness.state_root), REPO_UUID],
        cwd=Path.cwd(),
        check=False,
    )
    assert killed.returncode == 91
    segments = harness.state_root / "workspaces" / REPO_UUID / "journal" / "segments"
    assert [path.name for path in segments.iterdir()] and any(
        path.name.startswith(".00000000000000000001.gwf.tmp-")
        for path in segments.iterdir()
    )

    successor = acquire(harness, "BUILD", tick=3)
    reopened = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    snapshot = reopened.recover(successor, monotonic_ns=30_001)

    assert snapshot.events == ()
    assert list(segments.iterdir()) == []
    harness.leases.release(successor)


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


def test_journal_recovery_does_not_discard_torn_tail_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    expired = False
    original_decode_segment = store._decode_segment

    def expire_on_torn_tail(
        relative: Path,
        *,
        existing_only: bool = False,
        deadline_ns: int | None = None,
    ) -> tuple[bytes, JournalEvent]:
        nonlocal expired
        try:
            return original_decode_segment(
                relative,
                existing_only=existing_only,
                deadline_ns=deadline_ns,
            )
        except JournalFrameTruncated:
            expired = True
            raise

    def enforce_test_deadline(deadline_ns: int | None, detail: str) -> None:
        if deadline_ns is not None and expired:
            raise LockTimeout(detail)

    monkeypatch.setattr(store, "_decode_segment", expire_on_torn_tail)
    monkeypatch.setattr(
        journal_module,
        "require_before_deadline",
        enforce_test_deadline,
    )

    with harness.leases.current_operation(grant, monotonic_ns=10_002) as operation:
        with pytest.raises(LockTimeout):
            store.recover_locked(operation, deadline_ns=10**30)

    assert tail.exists()


def test_journal_recovery_head_commit_honors_deadline(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    _append_allocated(store, grant)
    snapshot = store.read_stable(REPO_UUID)
    assert snapshot.head is not None
    current, _previous, _pending = store._head_paths(REPO_UUID)
    current_path = store.state.path(current)
    current_before = current_path.read_bytes()

    with pytest.raises(LockTimeout):
        store._commit_head(
            REPO_UUID,
            snapshot.head,
            label="journal_head_recovery",
            deadline_ns=0,
        )

    assert current_path.read_bytes() == current_before


def test_repaired_transition_authority_preserves_deadline_timeout() -> None:
    store = cast(Any, object.__new__(JournalStore))
    timeout = LockTimeout("visible pointer validation exceeded its deadline")
    read_kwargs: list[dict[str, object]] = []

    def expire_visible_pointer(*_args: object, **_kwargs: object) -> bytes:
        read_kwargs.append(_kwargs)
        raise timeout

    store.state = SimpleNamespace(read_existing_bytes=expire_visible_pointer)
    operation = SimpleNamespace(
        operation="REPAIR",
        repo_uuid=REPO_UUID,
        grant=SimpleNamespace(operation_epoch=1),
        fence_token=1,
    )

    with pytest.raises(LockTimeout) as raised:
        store._require_transition_authority(
            operation,
            transition="REPAIRED",
            generation_id="gen-repaired",
            receipt_sha256="a" * 64,
            pointer_revision=1,
            deadline_ns=20_000,
        )

    assert raised.value is timeout
    assert read_kwargs == [{"deadline_ns": 20_000, "max_bytes": 64 * 1024}]


def test_journal_adopts_one_complete_hash_linked_uncommitted_segment(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    first = _append_allocated(store, grant)
    logical = {
        "transition": "STAGING",
        "generation_id": "gen-journal",
        "receipt_sha256": None,
        "pointer_revision": None,
        "operation_epoch": grant.operation_epoch,
        "fence_token": grant.lease.to_dict()["fence_token"],
        "occurred_at": "2026-07-16T19:00:00Z",
    }
    second = cast(JournalEvent, JournalEvent.from_mapping(
        {
            "contract": "graphify.workspace.journal_event",
            "schema_version": 1,
            "event_id": store._event_id(REPO_UUID, logical),
            "sequence": 2,
            "prior_event_sha256": first.sha256,
            **logical,
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
    logical = {
        "transition": "STAGING",
        "generation_id": "gen-journal",
        "receipt_sha256": None,
        "pointer_revision": None,
        "operation_epoch": grant.operation_epoch,
        "fence_token": grant.lease.to_dict()["fence_token"],
        "occurred_at": "2026-07-16T19:00:00Z",
    }
    second = cast(
        JournalEvent,
        JournalEvent.from_mapping(
            {
                "contract": "graphify.workspace.journal_event",
                "schema_version": 1,
                "event_id": store._event_id(REPO_UUID, logical),
                "sequence": 2,
                "prior_event_sha256": first.sha256,
                **logical,
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


def test_journal_rejects_uncommitted_event_id_from_another_workspace(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD", tick=1)
    store = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    logical = {
        "transition": "ALLOCATED",
        "generation_id": "gen-cross-workspace",
        "receipt_sha256": None,
        "pointer_revision": None,
        "operation_epoch": grant.operation_epoch,
        "fence_token": grant.lease.to_dict()["fence_token"],
        "occurred_at": "2026-07-16T19:00:00Z",
    }
    event = cast(
        JournalEvent,
        JournalEvent.from_mapping(
            {
                "contract": "graphify.workspace.journal_event",
                "schema_version": 1,
                "event_id": store._event_id(
                    "22222222-2222-4222-8222-222222222222",
                    logical,
                ),
                "sequence": 1,
                "prior_event_sha256": None,
                **logical,
            }
        ),
    )
    segment = _segment(harness.state_root, 1)
    store.state.ensure_directory(segment.parent.relative_to(harness.state_root))
    segment.write_bytes(encode_journal_frame(event))
    segment.chmod(0o600)

    with pytest.raises(JournalCorrupt, match="event id is not bound"):
        store.recover(grant, monotonic_ns=10_001)

    assert not store.state.path(store._head_paths(REPO_UUID)[0]).exists()


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
