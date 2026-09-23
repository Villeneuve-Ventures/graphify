"""Regressions for coordination admission and recovery review findings."""

import fcntl

import pytest

from graphify.workspace.adapters.base import SourceObservation as StructuralObservation
from graphify.workspace.journal import JournalRecoveryRequired, JournalStore
from graphify.workspace.lifecycle_observation import ObservationError, SourceObservation
from graphify.workspace.persistence import StatePathError
from tests.test_workspace_contracts import manifests
from tests.workspace_s3_helpers import REPO_UUID, START, SUPPORTED, create_harness, tree_snapshot


def acquire(harness, operation):
    state = harness.leases.inspect(REPO_UUID)
    return harness.leases.acquire(
        REPO_UUID, operation, harness.leases.current_owner(),
        expected_registry_revision=1, expected_active_source_revision=1,
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        acquired_at=START, monotonic_ns=10_000, ttl_ns=1_000_000,
    )


def test_activate_journal_recovery_excludes_global_writer(tmp_path, monkeypatch):
    harness = create_harness(tmp_path)
    grant = acquire(harness, "ACTIVATE")
    journal = JournalStore(harness.state_root, harness.leases, capabilities=SUPPORTED)
    original = harness.leases.state.recover_record
    checked = []

    def recover_record(**kwargs):
        if kwargs["label"] == "capacity":
            # A competing global writer must remain excluded while recovery can
            # remove root-level atomic temps belonging to registry/capacity.
            with (harness.state_root / "registry.lock").open("rb") as competitor:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            checked.append(True)
        return original(**kwargs)

    monkeypatch.setattr(harness.leases.state, "recover_record", recover_record)
    assert journal.recover(grant, monotonic_ns=10_001).events == ()
    assert checked == [True]


def test_journal_atomic_segment_temp_requires_recovery_without_mutation(tmp_path):
    harness = create_harness(tmp_path)
    grant = acquire(harness, "BUILD")
    journal = JournalStore(harness.state_root, harness.leases, capabilities=SUPPORTED)
    directory = journal.state.ensure_directory(journal._segments_directory(REPO_UUID))
    orphan = directory / (".00000000000000000001.gwf.tmp-123-" + "a" * 32)
    orphan.write_bytes(b"interrupted frame")
    orphan.chmod(0o600)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(JournalRecoveryRequired):
        journal.read_stable(REPO_UUID)
    assert tree_snapshot(harness.state_root) == before
    assert journal.recover(grant, monotonic_ns=10_001).events == ()
    assert not orphan.exists()
    assert journal.read_stable(REPO_UUID).events == ()


def test_journal_unsafe_atomic_segment_temp_still_refuses(tmp_path):
    harness = create_harness(tmp_path)
    journal = JournalStore(harness.state_root, harness.leases, capabilities=SUPPORTED)
    directory = journal.state.ensure_directory(journal._segments_directory(REPO_UUID))
    orphan = directory / (".00000000000000000001.gwf.tmp-123-" + "a" * 32)
    orphan.write_bytes(b"unsafe permissions")
    orphan.chmod(0o666)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(StatePathError):
        journal.read_stable(REPO_UUID)
    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("passes", [2, 3, 4, 5, 6])
def test_s3_observation_requires_exactly_two_passes(tmp_path, passes):
    initial, _ = manifests(tmp_path)
    structural = StructuralObservation(initial, None, passes)
    if passes == 2:
        assert SourceObservation("a" * 40, "b" * 64, structural).stable_inventory_passes == 2
    else:
        with pytest.raises(ObservationError, match="exactly two"):
            SourceObservation("a" * 40, "b" * 64, structural)
