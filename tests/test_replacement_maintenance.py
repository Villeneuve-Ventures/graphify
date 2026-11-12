"""Contract simulations only: none of these witnesses qualifies native maintenance."""
from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
from typing import Literal

import pytest

from graphify import canonical_replacement as replacement
from graphify import replacement_maintenance as maintenance
from graphify import transaction as tx
from tests.test_canonical_replacement import case, modeled  # noqa: F401


@pytest.fixture
def operation():
    request = {"root": "/enrolled/source", "output": "/enrolled/source/graphify-out",
               "journal": "/enrolled/control/journal.json",
               "parent_identities": {name: {"device": 1, "inode": number}
                                     for number, name in enumerate(("canonical", "archive", "control", "stage"), 1)}}
    return maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256="a" * 64, seal_sha256="b" * 64)


@pytest.fixture
def identity():
    binding = maintenance.TargetBinding((1, 10), (1, 20), "c" * 64)
    return maintenance.GrantIdentity("enrollment-one", "boot-one", 1,
                                     "coordinator-incarnation-one", "d" * 64, binding)


def registry_for(operation, identity, persist):
    return maintenance.ModelRegistry(operation.target, identity.enrollment, identity.boot,
                                     identity.runtime_sha256, identity.target_binding, persist)


def retire(registry, handle):
    registry.confirm_exit(handle, maintenance.CohortExit(handle.operation.sha256, handle.identity))
    registry.withdraw_access(handle)


def terminal_for(handle, *, outcome: Literal["committed", "rolled-back-unrecovered"] = "committed"):
    return maintenance.TerminalValidation(
        handle.operation.sha256, outcome,
        (1, 30) if outcome == "committed" else handle.identity.target_binding.old_output_identity,
        handle.identity.target_binding.old_inventory_sha256,
        "canonical-replacement-v1:" + "e" * 64 if outcome == "committed" else None,
        "f" * 64 if outcome == "committed" else None)


def prepare_release(registry, handle):
    retire(registry, handle)
    terminal = terminal_for(handle)
    registry.validate_terminal(handle, terminal)
    return terminal


def current_for(terminal, generation=1):
    return maintenance.CurrentPublication(terminal.output_identity, terminal.replacement_binding,
                                           generation, terminal.receipt_sha256 if generation == 1 else "9" * 64)


@pytest.mark.parametrize("catch_rejection", [True, False])
def test_persistence_callback_cannot_grant_a_competing_journal(operation, identity, catch_rejection):
    request = json.loads(operation.request_json)
    request["journal"] += ".competing"
    competing = maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256=operation.preview_sha256, seal_sha256=operation.seal_sha256)
    persisted, observed, returned, rejections = [], [], [], []
    attempted = False

    def persist(candidate):
        nonlocal attempted
        observed.append(registry.snapshot)
        if not attempted:
            attempted = True
            try:
                returned.append(registry.grant(competing, identity))
            except maintenance.MaintenanceContractError as error:
                if not catch_rejection:
                    raise
                rejections.append(str(error))
        persisted.append(candidate)

    registry = registry_for(operation, identity, persist)
    if catch_rejection:
        handle = registry.grant(operation, identity)
        assert handle.operation == operation
        assert registry.snapshot.state == "ACTIVE"
        assert [state.state for state in persisted] == ["GRANTING", "ACTIVE"]
        assert all(state.operation == operation and state.epoch == identity.epoch for state in persisted)
        assert len(rejections) == 1 and "reentrant" in rejections[0]
        assert [state.state for state in observed] == ["HELD", "GRANTING"]
    else:
        with pytest.raises(maintenance.MaintenanceContractError, match="reentrant"):
            registry.grant(operation, identity)
        assert registry.snapshot.state == "RECOVERY_HELD"
        assert not persisted
        assert [state.state for state in observed] == ["HELD"]
    assert not returned
    assert registry.snapshot.operation == operation
    assert registry.snapshot.identity == identity
    assert registry.snapshot.epoch == identity.epoch
    assert registry.snapshot.release is None
    assert not registry.snapshot.writers_admitted


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_failed_persistence_allows_a_later_exact_hold(operation, identity, failure):
    persisted = []
    fail_hold = True

    def persist(candidate):
        nonlocal fail_hold
        if candidate.state == "RECOVERY_HELD" and fail_hold:
            fail_hold = False
            raise failure("uncertain hold acknowledgment")
        persisted.append(candidate)

    registry = registry_for(operation, identity, persist)
    handle = registry.grant(operation, identity)
    with pytest.raises(failure, match="uncertain hold"):
        handle.close()
    held = registry.snapshot
    assert held.state == "RECOVERY_HELD"
    assert held.operation == operation and held.identity == identity
    assert held.epoch == identity.epoch and held.release is None
    assert not held.writers_admitted

    handle.close()

    assert registry.snapshot == held
    assert persisted[-1] == held
    assert [state.state for state in persisted] == ["GRANTING", "ACTIVE", "RECOVERY_HELD"]


def test_descriptor_freezes_nested_request_and_binds_every_selector(operation):
    request = json.loads(operation.request_json)
    copied = maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256=operation.preview_sha256, seal_sha256=operation.seal_sha256)
    request["parent_identities"]["control"]["inode"] = 999
    assert copied == operation
    assert copied.request_sha256 == operation.request_sha256
    assert copied.sha256 != replace(copied, action="resume", journal_sha256="7" * 64).sha256
    assert copied.sha256 != replace(copied, preview_sha256="8" * 64).sha256
    assert copied.sha256 != replace(copied, seal_sha256="8" * 64).sha256
    changed = maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256=operation.preview_sha256, seal_sha256=operation.seal_sha256)
    assert changed.request_sha256 != copied.request_sha256
    with pytest.raises(FrozenInstanceError):
        setattr(copied, "action", "rollback")


@pytest.mark.parametrize("change", [
    {"action": "unknown"}, {"preview_sha256": None}, {"seal_sha256": "bad"},
    {"journal_sha256": "1" * 64}, {"quarantine": "/enrolled/quarantine"},
    {"request_json": '{"output":"/enrolled/alternate"}'},
])
def test_incomplete_or_mixed_forward_selectors_refuse(operation, change):
    with pytest.raises(maintenance.MaintenanceContractError):
        replace(operation, **change)


def test_rollback_selectors_are_distinct_and_complete(operation):
    rollback = maintenance.OperationDescriptor.from_request(
        "rollback", json.loads(operation.request_json), journal_sha256="1" * 64,
        quarantine="/enrolled/quarantine/new", quarantine_parent=(1, 40), new_tree_sha256="2" * 64)
    for change in ({"journal_sha256": None}, {"quarantine_parent": (True, 40)},
                   {"preview_sha256": "a" * 64}, {"quarantine": "relative"}):
        with pytest.raises(maintenance.MaintenanceContractError):
            replace(rollback, **change)
    assert replace(rollback, new_tree_sha256="3" * 64).sha256 != rollback.sha256


def test_different_journals_compete_for_one_target(operation, identity):
    saved = []
    registry = registry_for(operation, identity, saved.append)
    handle = registry.grant(operation, identity)
    request = json.loads(operation.request_json)
    request["journal"] = "/enrolled/control/other.json"
    other = maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256=operation.preview_sha256, seal_sha256=operation.seal_sha256)
    with pytest.raises(maintenance.MaintenanceContractError, match="not eligible"):
        registry.grant(other, replace(identity, epoch=2, coordinator="competitor"))
    assert registry.snapshot.operation == handle.operation
    assert [item.state for item in saved] == ["GRANTING", "ACTIVE"]


@pytest.mark.parametrize("change", [
    {"boot": "another-boot"}, {"enrollment": "another-enrollment"}, {"epoch": 2},
    {"runtime_sha256": "8" * 64},
    {"target_binding": maintenance.TargetBinding((1, 11), (1, 20), "c" * 64)},
    {"target_binding": maintenance.TargetBinding((1, 10), (1, 21), "c" * 64)},
    {"target_binding": maintenance.TargetBinding((1, 10), (1, 20), "8" * 64)},
])
def test_changed_grant_context_cannot_start(operation, identity, change):
    registry = registry_for(operation, identity, lambda _: None)
    with pytest.raises(maintenance.MaintenanceContractError, match="binding or epoch"):
        registry.grant(operation, replace(identity, **change))
    assert registry.snapshot.state == "HELD"


def test_copying_handle_cannot_transfer_a_grant(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = registry.grant(operation, identity)
    with pytest.raises(maintenance.MaintenanceContractError, match="copied"):
        registry.confirm_exit(copy(handle), maintenance.CohortExit(operation.sha256, identity))
    assert registry.snapshot.state == "ACTIVE"


@pytest.mark.parametrize("failure", [None, RuntimeError("coordinator death")])
def test_context_exit_never_releases_or_regrants(operation, identity, failure):
    registry = registry_for(operation, identity, lambda _: None)
    try:
        with registry.grant(operation, identity):
            if failure:
                raise failure
    except RuntimeError:
        assert failure is not None
    assert registry.snapshot.state == "RECOVERY_HELD"
    assert registry.snapshot.release is None
    with pytest.raises(maintenance.MaintenanceContractError, match="cohort"):
        registry.grant(operation, replace(identity, epoch=2, coordinator="successor"))


def test_retirement_requires_exact_exit_and_access_withdrawal(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = registry.grant(operation, identity)
    handle.close()  # A timeout/closed connection is not process-exit evidence.
    with pytest.raises(maintenance.MaintenanceContractError, match="exit must precede"):
        registry.withdraw_access(handle)
    with pytest.raises(maintenance.MaintenanceContractError, match="exact cohort"):
        registry.confirm_exit(handle, maintenance.CohortExit(operation.sha256, replace(identity, coordinator="other")))
    registry.confirm_exit(handle, maintenance.CohortExit(operation.sha256, identity))
    with pytest.raises(maintenance.MaintenanceContractError, match="cohort"):
        registry.grant(operation, replace(identity, epoch=2, coordinator="successor"))
    registry.withdraw_access(handle)
    successor = registry.grant(replace(operation, action="resume", journal_sha256="7" * 64),
                               replace(identity, epoch=2, coordinator="successor"))
    with pytest.raises(maintenance.MaintenanceContractError, match="stale"):
        handle.close()
    assert registry.snapshot.identity == successor.identity


def test_retired_operation_cannot_adopt_a_different_request(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = registry.grant(operation, identity)
    retire(registry, handle)
    request = json.loads(operation.request_json)
    request["journal"] += ".other"
    other = maintenance.OperationDescriptor.from_request(
        "resume", request, preview_sha256=operation.preview_sha256, seal_sha256=operation.seal_sha256,
        journal_sha256="7" * 64)
    with pytest.raises(maintenance.MaintenanceContractError, match="request changed"):
        registry.grant(other, replace(identity, epoch=2, coordinator="successor"))


def test_retired_operation_cannot_change_its_seal(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = registry.grant(operation, identity)
    retire(registry, handle)
    changed = replace(operation, action="resume", journal_sha256="7" * 64, seal_sha256="8" * 64)
    with pytest.raises(maintenance.MaintenanceContractError, match="data selectors changed"):
        registry.grant(changed, replace(identity, epoch=2, coordinator="successor"))


def test_release_requires_exit_withdrawal_and_independent_validation(operation, identity):
    saved = []
    registry = registry_for(operation, identity, saved.append)
    handle = registry.grant(operation, identity)
    terminal = terminal_for(handle)
    with pytest.raises(maintenance.MaintenanceContractError, match="post-exit"):
        registry.validate_terminal(handle, terminal)
    retire(registry, handle)
    for bad in (replace(terminal, operation_sha256="8" * 64), replace(terminal, archive_sha256="8" * 64)):
        with pytest.raises(maintenance.MaintenanceContractError, match="validation"):
            registry.validate_terminal(handle, bad)
    with pytest.raises(maintenance.MaintenanceContractError, match="healthy release"):
        registry.release(handle)
    registry.validate_terminal(handle, terminal)
    with pytest.raises(maintenance.MaintenanceContractError, match="not been acknowledged"):
        registry.admit_client(identity.runtime_sha256, current_for(terminal))
    record = registry.release(handle)
    assert saved[-1].release == record
    assert not saved[-1].writers_admitted
    assert registry.admit_client(identity.runtime_sha256, current_for(terminal)) == record
    assert saved[-1].writers_admitted


def test_release_retry_uses_separate_record_after_generation_two(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = registry.grant(operation, identity)
    terminal = prepare_release(registry, handle)
    record = registry.release(handle)
    assert registry.admit_client(identity.runtime_sha256, current_for(terminal, 2)) == record
    assert registry.release(handle) is record
    for bad in (replace(current_for(terminal, 2), output_identity=(1, 31)),
                replace(current_for(terminal, 2), replacement_binding="canonical-replacement-v1:" + "8" * 64),
                replace(current_for(terminal), receipt_sha256="8" * 64)):
        with pytest.raises(maintenance.MaintenanceContractError, match="publication"):
            registry.admit_client(identity.runtime_sha256, bad)
    with pytest.raises(maintenance.MaintenanceContractError, match="runtime"):
        registry.admit_client("8" * 64, current_for(terminal, 2))
    with pytest.raises(maintenance.MaintenanceContractError, match="not eligible"):
        registry.grant(operation, replace(identity, epoch=2, coordinator="later"))


def test_rollback_is_terminal_held_service(operation, identity):
    rollback = maintenance.OperationDescriptor.from_request(
        "rollback", json.loads(operation.request_json), journal_sha256="1" * 64,
        quarantine="/enrolled/quarantine/new", quarantine_parent=(1, 40))
    registry = registry_for(rollback, identity, lambda _: None)
    handle = registry.grant(rollback, identity)
    retire(registry, handle)
    with pytest.raises(maintenance.MaintenanceContractError, match="rollback cannot release"):
        registry.validate_terminal(handle, terminal_for(handle))
    registry.validate_terminal(handle, terminal_for(handle, outcome="rolled-back-unrecovered"))
    assert registry.snapshot.state == "HELD"
    with pytest.raises(maintenance.MaintenanceContractError, match="healthy release"):
        registry.release(handle)
    with pytest.raises(maintenance.MaintenanceContractError, match="not eligible"):
        registry.grant(operation, replace(identity, epoch=2, coordinator="later"))


@pytest.mark.parametrize("action", ["execute", "resume"])
def test_forward_operation_cannot_certify_an_unselected_rollback(operation, identity, action):
    selected = operation if action == "execute" else replace(
        operation, action="resume", journal_sha256="7" * 64)
    registry = registry_for(selected, identity, lambda _: None)
    handle = registry.grant(selected, identity)
    retire(registry, handle)
    with pytest.raises(maintenance.MaintenanceContractError, match="forward terminal outcome"):
        registry.validate_terminal(handle, terminal_for(handle, outcome="rolled-back-unrecovered"))
    assert registry.snapshot.terminal is None


def test_committed_replacement_cannot_reuse_preserved_directory_identity(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = registry.grant(operation, identity)
    retire(registry, handle)
    wrong = replace(terminal_for(handle), output_identity=identity.target_binding.old_output_identity)
    with pytest.raises(maintenance.MaintenanceContractError, match="replacement output identity"):
        registry.validate_terminal(handle, wrong)
    assert registry.snapshot.release is None


def test_rollback_requires_the_original_directory_identity(operation, identity):
    rollback = maintenance.OperationDescriptor.from_request(
        "rollback", json.loads(operation.request_json), journal_sha256="1" * 64,
        quarantine="/enrolled/quarantine/new", quarantine_parent=(1, 40))
    registry = registry_for(rollback, identity, lambda _: None)
    handle = registry.grant(rollback, identity)
    retire(registry, handle)
    wrong = replace(terminal_for(handle, outcome="rolled-back-unrecovered"), output_identity=(1, 99))
    with pytest.raises(maintenance.MaintenanceContractError, match="rollback restored output identity"):
        registry.validate_terminal(handle, wrong)
    assert registry.snapshot.terminal is None


@pytest.mark.parametrize("change", [
    {"journal_sha256": "8" * 64}, {"quarantine": "/enrolled/quarantine/other"},
    {"quarantine_parent": (1, 41)}, {"new_tree_sha256": "8" * 64}, "forward",
])
def test_incomplete_rollback_cannot_change_action_or_retention_selection(operation, identity, change):
    rollback = maintenance.OperationDescriptor.from_request(
        "rollback", json.loads(operation.request_json), journal_sha256="1" * 64,
        quarantine="/enrolled/quarantine/new", quarantine_parent=(1, 40), new_tree_sha256="2" * 64)
    registry = registry_for(rollback, identity, lambda _: None)
    handle = registry.grant(rollback, identity)
    retire(registry, handle)
    changed = operation if change == "forward" else replace(rollback, **change)
    successor = replace(identity, epoch=2, coordinator="successor")
    with pytest.raises(maintenance.MaintenanceContractError, match="incomplete rollback selectors"):
        registry.grant(changed, successor)
    assert registry.grant(rollback, successor).operation == rollback


@pytest.mark.parametrize("failed_state", ["GRANTING", "ACTIVE", "RELEASED", "writer-admission"])
def test_persistence_failure_never_admits_a_writer(operation, identity, failed_state):
    def persist(state):
        if state.state == failed_state or (failed_state == "writer-admission" and state.writers_admitted):
            raise OSError("injected durable acknowledgment failure")

    registry = registry_for(operation, identity, persist)
    with pytest.raises(OSError, match="acknowledgment"):
        handle = registry.grant(operation, identity)
        terminal = prepare_release(registry, handle)
        registry.release(handle)
        registry.admit_client(identity.runtime_sha256, current_for(terminal))
    assert registry.snapshot.state == "RECOVERY_HELD"
    # A failed acknowledgment cannot erase an attempted writer-admission latch.
    assert registry.snapshot.writers_admitted is (failed_state == "writer-admission")
    with pytest.raises(maintenance.MaintenanceContractError, match="not been acknowledged"):
        registry.admit_client(identity.runtime_sha256, maintenance.CurrentPublication((1, 30), "binding", 2, "9" * 64))


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_uncertain_first_grant_retains_selection_and_epoch(operation, identity, failure):
    persisted = []

    def persist(state):
        persisted.append(state)
        if len(persisted) == 1:
            raise failure("durable write without acknowledgment")

    registry = registry_for(operation, identity, persist)
    with pytest.raises(failure, match="without acknowledgment"):
        registry.grant(operation, identity)
    assert registry.snapshot.state == "RECOVERY_HELD"
    assert registry.snapshot.epoch == identity.epoch
    assert registry.snapshot.operation == operation
    assert not registry.snapshot.cohort_exited
    assert not registry.snapshot.access_withdrawn
    request = json.loads(operation.request_json)
    request["journal"] = "/enrolled/control/other-journal.json"
    changed = maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256=operation.preview_sha256,
        seal_sha256=operation.seal_sha256)
    for candidate in (operation, changed):
        for epoch in (identity.epoch, identity.epoch + 1):
            with pytest.raises(maintenance.MaintenanceContractError):
                registry.grant(candidate, replace(identity, epoch=epoch))
    assert len(persisted) == 1


@pytest.mark.parametrize("action", ["execute", "resume", "rollback"])
def test_constructed_model_and_environment_cannot_enable_live_entrypoints(case, identity, monkeypatch, action):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    descriptor = maintenance.OperationDescriptor.from_request(
        "execute", request, preview_sha256=plan["preview_sha256"], seal_sha256=replacement._sha(replacement._bytes(seal)))
    registry = registry_for(descriptor, identity, lambda _: None)
    registry.grant(descriptor, identity)
    monkeypatch.setenv("GRAPHIFY_MAINTENANCE_BACKEND", "modeled")
    monkeypatch.setenv("GRAPHIFY_MAINTENANCE_GRANT", json.dumps(asdict(registry.snapshot)))
    before = replacement._tree(request["output"])
    if action == "rollback":
        call = lambda: replacement.rollback(request, "1" * 64, Path(request["journal"]).parent / "quarantine")
    elif action == "resume":
        call = lambda: replacement.resume(request, seal, plan["preview_sha256"], expected_journal_sha256="1" * 64)
    else:
        call = lambda: getattr(replacement, action)(request, seal, plan["preview_sha256"])
    with pytest.raises(tx.PendingTransactionError, match="not qualified"):
        call()
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert not list(Path(request["journal"]).parent.glob(".replacement-lock-*"))
    Path(request["stage"]).write_bytes(replacement._bytes(seal))
    request_path = Path(request["journal"]).parent / "request.json"
    request_path.write_bytes(replacement._bytes(request))
    args = [action, "--request", str(request_path)]
    if action == "rollback":
        args += ["--journal-sha256", "1" * 64, "--quarantine", str(Path(request["journal"]).parent / "quarantine")]
    else:
        args += ["--preview-sha256", plan["preview_sha256"]]
        if action == "resume":
            args += ["--journal-sha256", "1" * 64]
    assert replacement.main(args) == 1
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()


@pytest.mark.parametrize("action", ["execute", "resume", "rollback"])
def test_cli_passes_exact_action_and_selectors_without_mutation(case, monkeypatch, capsys, action):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    Path(request["stage"]).write_bytes(replacement._bytes(seal))
    request_path = Path(request["journal"]).parent / "request.json"
    request_path.write_bytes(replacement._bytes(request))
    quarantine = Path(request["journal"]).parent / "quarantine"
    args = [action, "--request", str(request_path)]
    if action == "rollback":
        args += ["--journal-sha256", "1" * 64, "--quarantine", str(quarantine), "--new-tree-sha256", "2" * 64]
    else:
        args += ["--preview-sha256", plan["preview_sha256"]]
        if action == "resume":
            args += ["--journal-sha256", "1" * 64]
    captured = []

    def refuse(descriptor):
        captured.append(descriptor)
        raise tx.PendingTransactionError("native replacement maintenance backend is not qualified")

    monkeypatch.setattr(replacement, "_maintenance", refuse)
    before = deepcopy(replacement._tree(request["output"]))
    assert replacement.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    descriptor = captured[0]
    assert descriptor.action == action
    assert descriptor.request_json == replacement._bytes(request).decode()
    if action == "rollback":
        assert descriptor.journal_sha256 == "1" * 64
        assert descriptor.quarantine == str(quarantine)
        assert descriptor.new_tree_sha256 == "2" * 64
        parent = replacement._directory(quarantine.parent)
        assert descriptor.quarantine_parent == (parent["device"], parent["inode"])
    else:
        assert descriptor.preview_sha256 == plan["preview_sha256"]
        assert descriptor.seal_sha256 == replacement._sha(replacement._bytes(seal))
        assert descriptor.journal_sha256 == ("1" * 64 if action == "resume" else None)
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()
    assert not quarantine.exists()


def test_explicit_resume_rechecks_selected_journal_before_mutation(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)

    def stop(point):
        if point == "OLD_ARCHIVED":
            raise RuntimeError("modeled interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=stop)
    journal = Path(request["journal"]).read_bytes()
    archive = replacement._tree(request["archive"])
    with pytest.raises(tx.PendingTransactionError, match="selected resume journal changed"):
        replacement.resume(request, seal, plan["preview_sha256"], expected_journal_sha256="0" * 64)
    assert Path(request["journal"]).read_bytes() == journal
    assert replacement._tree(request["archive"]) == archive
    assert not Path(request["output"]).exists()
    result = replacement.resume(request, seal, plan["preview_sha256"],
                                expected_journal_sha256=replacement._sha(journal))
    assert result["status"] == "complete"


def test_rollback_rechecks_granted_quarantine_parent_after_maintenance(case, modeled, monkeypatch):
    import contextlib

    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    journal_path = Path(request["journal"])
    journal = journal_path.read_bytes()
    old_tree = replacement._tree(request["archive"])
    new_tree = replacement._tree(request["output"])
    parent = journal_path.parent / "retention"
    parent.mkdir()
    quarantine = parent / "new"

    @contextlib.contextmanager
    def swapped_parent(operation):
        selected = replacement._directory(parent)
        assert operation.quarantine_parent == (selected["device"], selected["inode"])
        parent.rename(parent.with_name("retained-parent"))
        parent.mkdir()
        yield

    monkeypatch.setattr(replacement, "_maintenance", swapped_parent)
    with pytest.raises(tx.PendingTransactionError, match="rollback retention parent changed"):
        replacement.rollback(request, replacement._sha(journal), quarantine,
                             expected_new_tree=replacement._sha(replacement._bytes(new_tree)))
    assert journal_path.read_bytes() == journal
    assert replacement._tree(request["archive"]) == old_tree
    assert replacement._tree(request["output"]) == new_tree
    assert not list(parent.iterdir())
