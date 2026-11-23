"""Synthetic namespace-policy refusals; no OS custody or native authority is tested."""
from dataclasses import replace
import json

import pytest

from graphify import replacement_maintenance as maintenance
from tests.test_replacement_maintenance import (
    admission_for, current_for, grant, policies_for, prepare_release, registry_for, retire,
)
from tests.test_replacement_maintenance import identity, operation  # noqa: F401


def test_missing_issuer_enrollment_refuses(operation, identity):
    registry = maintenance.ModelRegistry(
        operation.target, identity.enrollment, identity.boot, identity.runtime_sha256,
        identity.target_binding, lambda _: None, controller=identity.controller,
        policy_sha256=identity.policy_sha256)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        grant(registry, operation, identity)
    assert registry.snapshot.state == "HELD"


@pytest.mark.parametrize("change", ["cross-namespace", "copied-parent"])
def test_enrollment_cannot_move_to_another_parent(operation, identity, change):
    registry = registry_for(operation, identity, lambda _: None)
    request = json.loads(operation.request_json)
    if change == "cross-namespace":
        request["archive"] = "/another/archive/old"
    else:
        request["parent_identities"]["archive"]["inode"] = 99
    changed = replace(operation, request_json=maintenance._canonical(request))
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        grant(registry, changed, identity)


def test_release_without_current_policy_evidence_holds(operation, identity):
    saved = []
    registry = registry_for(operation, identity, saved.append)
    handle = grant(registry, operation, identity)
    prepare_release(registry, handle)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        registry.release(handle)
    assert saved[-1].state == "RECOVERY_HELD"
    assert not saved[-1].writers_admitted


def test_startup_without_current_policy_evidence_holds(operation, identity):
    saved = []
    registry = registry_for(operation, identity, saved.append)
    handle = grant(registry, operation, identity)
    terminal = prepare_release(registry, handle)
    registry.release(handle, current_issuer_policy_sha256=identity.issuer_policy_sha256)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        registry.admit_client(identity.runtime_sha256, current_for(terminal))
    assert saved[-1].state == "RECOVERY_HELD"
    assert not saved[-1].writers_admitted


@pytest.mark.parametrize("fingerprint", [None, "8" * 64, "unknown"])
def test_admission_requires_current_complete_policy_fingerprint(operation, identity, fingerprint):
    saved = []
    registry = registry_for(operation, identity, saved.append)
    witness = replace(admission_for(operation, identity), current_issuer_policy_sha256=fingerprint)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        registry.grant(operation, identity, witness)
    assert not saved
    assert registry.snapshot.identity is None


@pytest.mark.parametrize("boundary", ["release", "startup"])
@pytest.mark.parametrize("fingerprint", ["8" * 64, "unknown"])
def test_stale_or_unknown_policy_persists_recovery_hold(operation, identity, boundary, fingerprint):
    saved = []
    registry = registry_for(operation, identity, saved.append)
    handle = grant(registry, operation, identity)
    terminal = prepare_release(registry, handle)
    if boundary == "release":
        call = lambda: registry.release(handle, current_issuer_policy_sha256=fingerprint)
    else:
        registry.release(handle, current_issuer_policy_sha256=identity.issuer_policy_sha256)
        call = lambda: registry.admit_client(identity.runtime_sha256, current_for(terminal),
                                            current_issuer_policy_sha256=fingerprint)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        call()
    assert saved[-1].state == "RECOVERY_HELD"
    assert saved[-1].issuer_policies == registry.issuer_policies
    assert not saved[-1].writers_admitted
    with pytest.raises(maintenance.MaintenanceContractError, match="not been acknowledged"):
        registry.admit_client(identity.runtime_sha256, current_for(terminal),
                              current_issuer_policy_sha256=identity.issuer_policy_sha256)


@pytest.mark.parametrize("scope", ["canonical", "archive", "control", "stage"])
def test_every_selected_parent_requires_its_own_enrollment(operation, identity, scope):
    path = dict(zip(("canonical", "archive", "control", "stage"), operation.namespace_parents))[scope][0]
    policies = tuple(policy for policy in policies_for(operation) if policy.namespace_path != path)
    changed = replace(identity, issuer_policy_sha256=maintenance.issuer_policy_set_sha256(policies))
    registry = maintenance.ModelRegistry(
        operation.target, changed.enrollment, changed.boot, changed.runtime_sha256,
        changed.target_binding, lambda _: None, controller=changed.controller,
        policy_sha256=changed.policy_sha256, issuer_policies=policies, mutator_uid=changed.mutator_uid)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer namespace"):
        grant(registry, operation, changed)


def test_ancestor_scope_does_not_enroll_nested_parent(operation, identity):
    request = json.loads(operation.request_json)
    request["stage"] = "/enrolled/stage/nested/data.json"
    changed = replace(operation, request_json=maintenance._canonical(request))
    registry = registry_for(operation, identity, lambda _: None)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer namespace"):
        grant(registry, changed, identity)


def test_rollback_quarantine_parent_requires_enrollment(operation, identity):
    rollback = maintenance.OperationDescriptor.from_request(
        "rollback", json.loads(operation.request_json), journal_sha256="1" * 64,
        quarantine="/unenrolled/quarantine/new", quarantine_parent=(1, 40))
    registry = registry_for(operation, identity, lambda _: None)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer namespace"):
        grant(registry, rollback, identity)


@pytest.mark.parametrize("change", [{"issuer_policy_sha256": "8" * 64}, {"mutator_uid": 451}])
def test_grant_binds_complete_policy_digest_and_actual_mutator_uid(operation, identity, change):
    registry = registry_for(operation, identity, lambda _: None)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer policy or mutator UID"):
        grant(registry, operation, replace(identity, **change))


def test_policy_digest_binds_records_and_is_independent_of_operation_epoch(operation, identity):
    policies = policies_for(operation)
    fingerprint = maintenance.issuer_policy_set_sha256(policies)
    assert maintenance.issuer_policy_set_sha256(tuple(reversed(policies))) == fingerprint
    for change in ({"policy_generation": 2}, {"namespace_identity": (1, 99)},
                   {"legacy_issuer_uids": (452,)}, {"mutator_uid": 452},
                   {"namespace_path": "/enrolled/other"}, {"enrollment_id": "other"}):
        changed = (replace(policies[0], **change), *policies[1:])
        assert maintenance.issuer_policy_set_sha256(changed) != fingerprint
    registry = registry_for(operation, identity, lambda _: None)
    handle = grant(registry, operation, identity)
    retire(registry, handle)
    registry.restart("controller-two")
    successor = replace(identity, controller="controller-two", epoch=2, mutator="successor")
    grant(registry, operation, successor)
    assert registry.snapshot.issuer_policies == policies
    assert registry.snapshot.issuer_policy_sha256 == fingerprint
    assert registry.snapshot.mutator_uid == identity.mutator_uid
    assert all(policy.legacy_issuer_uids == (451,) for policy in registry.snapshot.issuer_policies)
    with pytest.raises(AttributeError):
        setattr(registry, "issuer_policies", ())
    with pytest.raises(AttributeError):
        setattr(registry, "issuer_policy_sha256", "8" * 64)


@pytest.mark.parametrize("change", [{"enrollment_id": "other"}, {"mutator_uid": 452}])
def test_registry_rejects_mixed_enrollment_or_dedicated_uid(operation, identity, change):
    policies = policies_for(operation)
    changed = (replace(policies[0], **change), *policies[1:])
    with pytest.raises(maintenance.MaintenanceContractError, match="matching dedicated"):
        maintenance.ModelRegistry(
            operation.target, identity.enrollment, identity.boot, identity.runtime_sha256,
            identity.target_binding, lambda _: None, controller=identity.controller,
            policy_sha256=identity.policy_sha256, issuer_policies=changed, mutator_uid=identity.mutator_uid)


def test_generation_change_cannot_refresh_release_policy(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = grant(registry, operation, identity)
    prepare_release(registry, handle)
    policies = policies_for(operation)
    changed = (replace(policies[0], policy_generation=2), *policies[1:])
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        registry.release(handle, current_issuer_policy_sha256=maintenance.issuer_policy_set_sha256(changed))
    assert registry.snapshot.issuer_policies == policies
    assert registry.snapshot.issuer_policy_sha256 == identity.issuer_policy_sha256


@pytest.mark.parametrize("kind", ["path", "identity", "mutable"])
def test_policy_set_rejects_ambiguous_or_mutable_enrollment(operation, kind):
    policies = policies_for(operation)
    if kind == "mutable":
        changed = list(policies)
    else:
        field = "namespace_path" if kind == "path" else "namespace_identity"
        changed = (replace(policies[0], **{field: getattr(policies[1], field)}), *policies[1:])
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        # Exercise the runtime validator with input that bypasses static typing.
        getattr(maintenance, "issuer_policy_set_sha256")(changed)


@pytest.mark.parametrize("uid", [True, 0, -1, 2**32])
def test_grant_identity_requires_a_valid_dedicated_uid(identity, uid):
    with pytest.raises(maintenance.MaintenanceContractError, match="grant identity"):
        replace(identity, mutator_uid=uid)


def test_release_retry_rechecks_policy_and_preserves_original_record(operation, identity):
    registry = registry_for(operation, identity, lambda _: None)
    handle = grant(registry, operation, identity)
    prepare_release(registry, handle)
    record = registry.release(handle, current_issuer_policy_sha256=identity.issuer_policy_sha256)
    with pytest.raises(maintenance.MaintenanceContractError, match="issuer"):
        registry.release(handle, current_issuer_policy_sha256="8" * 64)
    assert registry.snapshot.state == "RECOVERY_HELD"
    assert registry.snapshot.release == record
    assert not registry.snapshot.writers_admitted
