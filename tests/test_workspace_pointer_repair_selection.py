"""Repair selects committed lineage while retaining intact receipt evidence."""

from types import SimpleNamespace
from typing import cast

import pytest

from graphify.workspace.journal import JournalRecoveryProjection, JournalSnapshot
from graphify.workspace.lifecycle_contracts import GenerationReceipt, PointerSet, PriorPointerRecord
from graphify.workspace.pointers import PointerCorrupt
from tests.test_workspace_lifecycle_s3 import _runtime
from tests.workspace_s3_helpers import REPO_UUID


def _receipt(generation_id, digest):
    return cast(GenerationReceipt, SimpleNamespace(sha256=digest * 64, to_dict=lambda: {
        "generation_id": generation_id, "active_source_revision": 1, "source_epoch": 1,
    }))


def _pointer(receipt, revision, **changes):
    return cast(PointerSet, PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": revision,
        "active_source_revision": 1, "source_epoch": 1,
        "operation_epoch": revision, "fence_token": revision, "state_schema_version": 2,
        "current": {"generation_id": receipt.to_dict()["generation_id"],
                    "receipt_sha256": receipt.sha256}, "last_good": None,
        **changes,
    }))


def _event(receipt, transition, revision):
    return SimpleNamespace(to_dict=lambda: {
        "transition": transition, "generation_id": receipt.to_dict()["generation_id"],
        "receipt_sha256": receipt.sha256, "pointer_revision": revision,
        "operation_epoch": revision, "fence_token": revision,
    }, sha256=f"{transition}-{revision}")


def _configure(monkeypatch, pointers, receipts, events):
    projection = JournalRecoveryProjection(JournalSnapshot(None, tuple(events)), (), "d" * 64)
    monkeypatch.setattr(pointers.journal, "project_recovery", lambda *a, **kw: projection)
    monkeypatch.setattr(pointers.state, "private_directory_exists", lambda path: True)
    monkeypatch.setattr(pointers.state, "private_file_exists", lambda path: path != pointers._gc_intent(REPO_UUID))
    monkeypatch.setattr(pointers, "_verify_generation", lambda repo, gid, **kw: receipts[gid])
    return projection


def _analyze(pointers, current, **changes):
    return pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=None, fence_token=None,
        current=current, pending=None, prior=None,
        raw_evidence={"current_sha256": current.sha256},
        allow_atomic_temps=False, deadline_ns=None, **changes,
    )


def test_certified_orphan_visible_pointer_is_neither_candidate_nor_last_good(tmp_path, monkeypatch):
    _, _, pointers, _ = _runtime(tmp_path)
    committed, orphan = _receipt("gen-committed", "a"), _receipt("gen-orphan", "b")
    _configure(monkeypatch, pointers, {"gen-committed": committed, "gen-orphan": orphan}, [
        _event(committed, "CERTIFIED", None), _event(committed, "PROMOTED", 1),
        _event(orphan, "CERTIFIED", None),
    ])
    analysis = _analyze(pointers, _pointer(orphan, 2))
    assert analysis.plan.candidate == pointers._ref(committed)
    assert analysis.plan.last_good is None
    assert analysis.plan.next_pointer_revision == 3


@pytest.mark.parametrize("field", ["active_source_revision", "source_epoch"])
def test_source_envelope_corruption_keeps_receipt_recovery_evidence(tmp_path, monkeypatch, field):
    _, _, pointers, _ = _runtime(tmp_path)
    receipt = _receipt("gen-committed", "a")
    _configure(monkeypatch, pointers, {"gen-committed": receipt}, [
        _event(receipt, "CERTIFIED", None), _event(receipt, "PROMOTED", 1),
    ])
    visible = _pointer(receipt, 1, **{field: 99})
    receipts, corrupt = pointers._verify_repair_refs(REPO_UUID, visible)
    assert receipts == {"current": receipt}
    assert not corrupt
    assert not pointers._fully_verified(visible, receipts)
    analysis = _analyze(pointers, visible)
    assert analysis.plan.candidate == pointers._ref(receipt)
    assert analysis.plan.pointer_action == "replace"
    assert analysis.plan.next_pointer_revision == 2


def test_restored_old_pointer_recovers_latest_committed_target(tmp_path, monkeypatch):
    _, _, pointers, _ = _runtime(tmp_path)
    older, latest = _receipt("gen-older", "a"), _receipt("gen-latest", "b")
    _configure(monkeypatch, pointers, {"gen-older": older, "gen-latest": latest}, [
        _event(older, "CERTIFIED", None), _event(older, "PROMOTED", 1),
        _event(latest, "CERTIFIED", None), _event(latest, "PROMOTED", 2),
    ])
    analysis = _analyze(pointers, _pointer(older, 1))
    assert analysis.plan.candidate == pointers._ref(latest)
    assert analysis.plan.next_pointer_revision == 3


def test_orphan_without_committed_history_cannot_be_repaired(tmp_path, monkeypatch):
    _, _, pointers, _ = _runtime(tmp_path)
    orphan = _receipt("gen-orphan", "a")
    _configure(monkeypatch, pointers, {"gen-orphan": orphan}, [
        _event(orphan, "CERTIFIED", None),
    ])
    with pytest.raises(PointerCorrupt):
        _analyze(pointers, _pointer(orphan, 1))


@pytest.mark.parametrize("change_journal", [False, True])
def test_journal_only_target_is_locked_and_projection_revalidated(
    tmp_path, monkeypatch, change_journal,
):
    from contextlib import contextmanager
    from graphify.workspace.pointers import PointerConflict

    _, _, pointers, _ = _runtime(tmp_path)
    older, latest = _receipt("gen-older", "a"), _receipt("gen-latest", "b")
    visible = _pointer(older, 1)
    projection = _configure(
        monkeypatch, pointers, {"gen-older": older, "gen-latest": latest}, [
            _event(older, "CERTIFIED", None), _event(older, "PROMOTED", 1),
            _event(latest, "CERTIFIED", None), _event(latest, "PROMOTED", 2),
        ],
    )
    monkeypatch.setattr(pointers.state, "inspect_atomic_temps", lambda *a, **kw: ())
    monkeypatch.setattr(pointers, "_read_repair_pointer", lambda path, **kw: (
        (visible, visible.sha256) if path == pointers._current(REPO_UUID) else (None, None)
    ))
    monkeypatch.setattr(pointers, "_read_repair_prior", lambda *a, **kw: (None, None))
    locked = set()

    @contextmanager
    def locks(entries, **kwargs):
        locked.update(generation_id for generation_id, _ in entries)
        if change_journal:
            changed = JournalRecoveryProjection(projection.snapshot, (), "e" * 64)
            monkeypatch.setattr(pointers.journal, "project_recovery", lambda *a, **kw: changed)
        yield
        locked.clear()

    monkeypatch.setattr(pointers.state, "existing_generation_locks", locks)

    def verify(repo_uuid, generation_id, **kwargs):
        assert generation_id in locked
        assert not change_journal, "changed journal must fail before target verification"
        return {"gen-older": older, "gen-latest": latest}[generation_id]

    monkeypatch.setattr(pointers, "_verify_generation", verify)
    if change_journal:
        with pytest.raises(PointerConflict, match="journal changed"):
            pointers.analyze_repair(REPO_UUID, active_source_revision=1)
    else:
        plan = pointers.analyze_repair(REPO_UUID, active_source_revision=1)
        assert plan.candidate == pointers._ref(latest)
        assert plan.selected_from == "journal"


def test_missing_latest_receipt_does_not_select_an_older_committed_current(tmp_path, monkeypatch):
    from graphify.workspace.generations import GenerationError

    _, _, pointers, _ = _runtime(tmp_path)
    older, latest = _receipt("gen-older", "a"), _receipt("gen-latest", "b")
    _configure(monkeypatch, pointers, {"gen-older": older}, [
        _event(older, "CERTIFIED", None), _event(older, "PROMOTED", 1),
        _event(latest, "CERTIFIED", None), _event(latest, "PROMOTED", 2),
    ])

    def verify(repo_uuid, generation_id, **kwargs):
        if generation_id == "gen-latest":
            raise GenerationError("receipt missing")
        return older

    monkeypatch.setattr(pointers, "_verify_generation", verify)
    with pytest.raises(PointerCorrupt, match="no fully verified"):
        _analyze(pointers, _pointer(older, 1))


def test_pending_with_corrupt_source_envelope_cannot_authorize_replacement(tmp_path, monkeypatch):
    _, _, pointers, _ = _runtime(tmp_path)
    receipt = _receipt("gen-pending", "a")
    _configure(monkeypatch, pointers, {"gen-pending": receipt}, [
        _event(receipt, "CERTIFIED", None),
    ])
    pending = _pointer(receipt, 1, source_epoch=99)
    with pytest.raises(PointerCorrupt, match="no fully verified"):
        pointers._derive_repair_analysis(
            REPO_UUID, active_source_revision=1, operation_epoch=None, fence_token=None,
            current=None, pending=pending, prior=None, raw_evidence={},
            allow_atomic_temps=False, deadline_ns=None,
        )


@pytest.mark.parametrize("operation_epoch", [None, 1])
@pytest.mark.parametrize("pending_revision", [1, 2])
def test_obsolete_pending_cannot_override_newer_committed_target(
    tmp_path, monkeypatch, operation_epoch, pending_revision,
):
    _, _, pointers, _ = _runtime(tmp_path)
    older, latest = _receipt("gen-older", "a"), _receipt("gen-latest", "b")
    _configure(monkeypatch, pointers, {"gen-older": older, "gen-latest": latest}, [
        _event(older, "CERTIFIED", None), _event(older, "PROMOTED", 1),
        _event(latest, "CERTIFIED", None), _event(latest, "PROMOTED", 2),
    ])
    restored = _pointer(older, pending_revision)
    prior = None if pending_revision == 1 else cast(PriorPointerRecord, PriorPointerRecord.from_mapping({
        "contract": "graphify.workspace.prior_pointer", "schema_version": 2,
        "retained_at": "2026-07-16T19:00:00Z", "replaced_by_revision": pending_revision,
        "pointer_set": _pointer(older, 1).to_dict(),
    }))
    with pytest.raises(PointerCorrupt, match="pending.*committed"):
        pointers._derive_repair_analysis(
            REPO_UUID, active_source_revision=1, operation_epoch=operation_epoch,
            fence_token=operation_epoch, current=restored, pending=restored, prior=prior,
            raw_evidence={}, allow_atomic_temps=False, deadline_ns=None,
        )


def test_current_with_orphan_last_good_requires_replacement(tmp_path, monkeypatch):
    _, _, pointers, _ = _runtime(tmp_path)
    committed, orphan = _receipt("gen-committed", "a"), _receipt("gen-orphan", "b")
    _configure(monkeypatch, pointers, {"gen-committed": committed, "gen-orphan": orphan}, [
        _event(committed, "CERTIFIED", None), _event(committed, "PROMOTED", 1),
        _event(orphan, "CERTIFIED", None),
    ])
    visible = _pointer(committed, 1, last_good=pointers._ref(orphan))
    analysis = _analyze(pointers, visible)
    assert analysis.plan.classification == "repairable"
    assert analysis.plan.pointer_action == "replace"
    assert analysis.plan.candidate == pointers._ref(committed)
    assert analysis.plan.last_good is None
    assert analysis.plan.next_pointer_revision == 2


@pytest.mark.parametrize("journal_completed", [False, True])
@pytest.mark.parametrize("committed_last_good", [False, True])
def test_pending_reuse_requires_authorized_last_good(
    tmp_path, monkeypatch, journal_completed, committed_last_good,
):
    _, _, pointers, _ = _runtime(tmp_path)
    candidate, last_good = _receipt("gen-current", "a"), _receipt("gen-last-good", "b")
    events = [_event(last_good, "CERTIFIED", None)]
    if committed_last_good:
        events.append(_event(last_good, "PROMOTED", 1))
    events.extend([_event(candidate, "CERTIFIED", None), _event(candidate, "PROMOTED", 2)])
    if journal_completed:
        events.append(_event(candidate, "REPAIRED", 3))
    _configure(monkeypatch, pointers, {"gen-current": candidate, "gen-last-good": last_good}, events)
    pending = _pointer(candidate, 3, last_good=pointers._ref(last_good))
    prior = cast(PriorPointerRecord, PriorPointerRecord.from_mapping({
        "contract": "graphify.workspace.prior_pointer", "schema_version": 2,
        "retained_at": "2026-07-16T19:00:00Z", "replaced_by_revision": 3,
        "pointer_set": _pointer(candidate, 2).to_dict(),
    }))
    analysis = pointers._derive_repair_analysis(
        REPO_UUID, active_source_revision=1, operation_epoch=3, fence_token=3,
        current=pending, pending=pending, prior=prior, raw_evidence={},
        allow_atomic_temps=False, deadline_ns=None,
    )
    assert analysis.plan.candidate == pointers._ref(candidate)
    if committed_last_good:
        assert analysis.plan.pointer_action == (
            "finalize_pending" if journal_completed else "resume_pending"
        )
        assert analysis.plan.last_good == pointers._ref(last_good)
        assert analysis.plan.next_pointer_revision == 3
    else:
        assert analysis.plan.pointer_action == "replace"
        assert analysis.plan.last_good is None
        assert analysis.plan.next_pointer_revision == 4
