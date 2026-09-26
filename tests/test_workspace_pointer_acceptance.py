"""Focused acceptance regressions for S3 pointer recovery and publication."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from graphify.workspace.lifecycle_contracts import PointerSet
from graphify.workspace.journal import JournalError
from graphify.workspace.persistence import InjectedFault
from graphify.workspace.pointers import PointerError, PointerStore, PointerSuperseded
from tests import test_workspace_lifecycle_s3 as lifecycle
from tests.workspace_s3_helpers import REPO_UUID, START, tree_snapshot


def _rolled_back_runtime(tmp_path, monkeypatch):
    captured = []
    runtime = lifecycle._runtime
    rollback = PointerStore.rollback

    def capture(*args, **kwargs):
        result = runtime(*args, **kwargs)
        captured.append(result)
        return result

    def supersede_then_rollback(self, grant, cas, **kwargs):
        with pytest.raises(PointerSuperseded):
            rollback(self, grant, replace(
                cas, expected_pointer_revision=cas.expected_pointer_revision - 1,
            ), **kwargs)
        return rollback(self, grant, cas, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_runtime", capture)
        patch.setattr(PointerStore, "rollback", supersede_then_rollback)
        lifecycle.test_successor_pointer_recovery_retains_only_active_source_last_good(
            tmp_path, patch, False, None, False,
        )
    return captured[0]


@pytest.mark.parametrize("damage", ["source_epoch", "corrupt", "missing"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_repair_preserves_exact_rollback_lineage(
    tmp_path, monkeypatch, damage, interrupted, *, repeat_fault="pending_durable",
):
    harness, _generations, pointers, _observations = _rolled_back_runtime(tmp_path, monkeypatch)
    assert pointers.analyze_repair(REPO_UUID, active_source_revision=1).classification == "no_op"
    path = pointers.state.path(pointers._current(REPO_UUID))
    original = PointerSet.from_json(path.read_bytes())
    if damage == "source_epoch":
        path.write_bytes(PointerSet.from_mapping({**original.to_dict(), "source_epoch": 99}).canonical)
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        path.unlink()
    state = harness.leases.inspect(REPO_UUID)
    recovery = harness.leases.acquire(
        REPO_UUID, "POINTER_RECOVERY", harness.leases.current_owner(),
        expected_registry_revision=1, expected_active_source_revision=1,
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        acquired_at=START, monotonic_ns=20_000_000, ttl_ns=1_000_000,
    )
    if interrupted:
        def fault(label):
            if label == "pointer:repaired:pending_durable":
                raise InjectedFault(label)
        pointers.fault_hook = fault
        with pytest.raises(InjectedFault, match="pending_durable"):
            pointers.recover(recovery, occurred_at=START, monotonic_ns=20_000_001)
        pointers.fault_hook = lambda _label: None
        interrupted_state = tree_snapshot(harness.state_root)
        with pytest.raises((PointerError, JournalError), match="timezone-aware"):
            pointers.recover(
                recovery, occurred_at=START.replace(tzinfo=None), monotonic_ns=20_000_001,
            )
        assert tree_snapshot(harness.state_root) == interrupted_state
    repaired = pointers.recover(recovery, occurred_at=START, monotonic_ns=20_000_001)
    assert repaired.to_dict()["current"] == original.to_dict()["current"]
    assert pointers.analyze_repair(REPO_UUID, active_source_revision=1).classification == "no_op"
    before = tree_snapshot(harness.state_root)
    assert pointers.recover(recovery, occurred_at=START, monotonic_ns=20_000_001) == repaired
    assert tree_snapshot(harness.state_root) == before
    assert not pointers.state.path(pointers._pending(REPO_UUID)).exists()

    if damage == "source_epoch" and not interrupted:
        # A further repair must preserve the root across a REPAIRED journal
        # event as well as across the original ROLLED_BACK event.
        harness.leases.release(recovery)
        state = harness.leases.inspect(REPO_UUID)
        recovery = harness.leases.acquire(
            REPO_UUID, "POINTER_RECOVERY", harness.leases.current_owner(),
            expected_registry_revision=1, expected_active_source_revision=1,
            expected_operation_epoch=state.operation_epoch,
            expected_migration_epoch=state.migration_epoch,
            acquired_at=START, monotonic_ns=22_000_000, ttl_ns=1_000_000,
        )
        path.write_bytes(PointerSet.from_mapping({**repaired.to_dict(), "source_epoch": 99}).canonical)
        def second_fault(label):
            if label == f"pointer:repaired:{repeat_fault}":
                raise InjectedFault(label)
        pointers.fault_hook = second_fault
        with pytest.raises(InjectedFault, match=repeat_fault):
            pointers.recover(recovery, occurred_at=START, monotonic_ns=22_000_001)
        pointers.fault_hook = lambda _label: None
        repeated = pointers.recover(recovery, occurred_at=START, monotonic_ns=22_000_001)
        assert repeated.to_dict()["pointer_revision"] > repaired.to_dict()["pointer_revision"]
        assert pointers.analyze_repair(REPO_UUID, active_source_revision=1).classification == "no_op"
        assert pointers.recover(recovery, occurred_at=START, monotonic_ns=22_000_001) == repeated


@pytest.mark.parametrize("fault", ["prior_durable", "visible", "journal_durable"])
def test_repeated_repair_preserves_lineage_at_durable_boundaries(tmp_path, monkeypatch, fault):
    test_repair_preserves_exact_rollback_lineage(
        tmp_path, monkeypatch, "source_epoch", False, repeat_fault=fault,
    )


class _NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


def test_first_promotion_rejects_invalid_timestamp_without_mutation(tmp_path, monkeypatch):
    promote = PointerStore.promote
    checked = []

    def reject_then_promote(self, grant, cas, **kwargs):
        if cas.expected_pointer_revision == 0:
            before = tree_snapshot(self.state.root)
            for timestamp, error in (
                (START.replace(tzinfo=None), (PointerError, JournalError)),
                (START.replace(tzinfo=_NoOffset()), (PointerError, JournalError)),
                (datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))), OverflowError),
            ):
                with pytest.raises(error):
                    promote(self, grant, cas, **{**kwargs, "occurred_at": timestamp})
                assert tree_snapshot(self.state.root) == before
            checked.append(True)
        return promote(self, grant, cas, **kwargs)

    monkeypatch.setattr(PointerStore, "promote", reject_then_promote)
    lifecycle.test_successor_pointer_recovery_retains_only_active_source_last_good(
        tmp_path, monkeypatch, False, None, False,
    )
    assert checked == [True]


@pytest.mark.parametrize("later", [None, "SUPERSEDED", "PROMOTED"])
def test_advanced_prior_requires_latest_unsuperseded_exact_repair(tmp_path, later):
    from types import SimpleNamespace

    from graphify.workspace.journal import JournalSnapshot
    from graphify.workspace.lifecycle_contracts import PriorPointerRecord

    _harness, _generations, pointers, _observations = lifecycle._runtime(tmp_path)
    target = {"generation_id": "gen-target", "receipt_sha256": "a" * 64}
    other = {"generation_id": "gen-other", "receipt_sha256": "b" * 64}
    pointer = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 3,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": 3, "fence_token": 3, "state_schema_version": 2,
        "current": target, "last_good": other,
    })
    prior = PriorPointerRecord.from_mapping({
        "contract": "graphify.workspace.prior_pointer", "schema_version": 2,
        "retained_at": "2026-07-16T19:00:00Z", "replaced_by_revision": 4,
        "pointer_set": {
            **pointer.to_dict(), "pointer_revision": 2,
            "operation_epoch": 2, "fence_token": 2,
            "current": other, "last_good": target,
        },
    })

    def event(transition, revision, ref):
        return SimpleNamespace(to_dict=lambda: {
            **ref, "transition": transition, "pointer_revision": revision,
            "operation_epoch": revision, "fence_token": revision,
        })

    events = [event("SUPERSEDED", 2, target), event("REPAIRED", 3, target)]
    if later is not None:
        events.append(event(later, 4, target if later == "SUPERSEDED" else other))
    assert pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, tuple(events)), pointer, None, prior, deadline_ns=None,
    ) is (later is None)
    unrelated_prior = PriorPointerRecord.from_mapping({
        **prior.to_dict(),
        "pointer_set": {**prior.to_dict()["pointer_set"], "last_good": other},
    })
    assert not pointers._rollback_authorizes_superseded(
        JournalSnapshot(None, tuple(events)), pointer, None, unrelated_prior, deadline_ns=None,
    )
