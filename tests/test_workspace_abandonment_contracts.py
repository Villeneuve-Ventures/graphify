"""Abandonment authority must fit the frozen request and recorded evidence."""

from dataclasses import replace

import pytest

from graphify.workspace.contracts import CompletionBinding
from graphify.workspace.lifecycle_contracts import (
    ContractError,
    StagedBuildAbandonmentEvidence,
    StagedBuildAbandonmentIntent,
    StagedBuildState,
    StructuralBuildRequest,
    canonical_json_bytes,
    canonical_sha256,
)


def _staged_with_intent(lifecycle_state="REQUESTED"):
    request = StructuralBuildRequest(
        logical_request_sha256="a" * 64,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=3,
        expected_migration_epoch=0,
        expected_pointer_revision=0,
        expected_current_receipt_sha256=None,
        source_commit="a" * 40,
        source_epoch=1,
        policy_sha256="b" * 64,
        observation_manifest_sha256="c" * 64,
        observation_evidence_sha256="",
        observation_detector_id="test-detector",
        observation_entries_sha256="d" * 64,
        expected_payload_bytes=1024,
        capacity_policy_sha256="e" * 64,
        compatibility_sha256="f" * 64,
    )
    observation = request.source_observation_document()
    request = replace(request, observation_evidence_sha256=canonical_sha256(
        [observation, observation]
    ))
    request = StructuralBuildRequest.from_mapping(request.to_dict())
    evidence = StagedBuildAbandonmentEvidence(
        request_sha256=request.sha256,
        registry_revision=1,
        active_source_revision=1,
        operation_epoch=9,
        migration_epoch=0,
        pointer_revision=0,
        current_receipt_sha256=None,
        selected_compatibility_sha256="0" * 64,
        semantic_source_epoch=None,
        semantic_queue_watermark=None,
        semantic_queue_state_sha256=None,
        source_commit=request.source_commit,
        source_inventory_sha256=request.observation_manifest_sha256,
        source_policy_sha256=request.policy_sha256,
        source_detector_id=request.observation_detector_id,
        source_stable_inventory_passes=2,
        source_entries_sha256=request.observation_entries_sha256,
        source_observation_evidence_sha256=request.observation_evidence_sha256,
    )
    intent = StagedBuildAbandonmentIntent(
        repo_uuid="550e8400-e29b-41d4-a716-446655440000",
        generation_id="gen-test",
        request_sha256=request.sha256,
        staged_revision=1,
        abandoned_from=lifecycle_state,
        operation_epoch=5,
        fence_token=4,
        reason="COMPATIBILITY_CHANGED",
        evidence=evidence,
    )
    completed = lifecycle_state in {"COMPLETE", "CERTIFIED"}
    completion = CompletionBinding.from_mapping({
        "contract": "graphify.workspace.structural-completion", "state_schema_version": 2,
        "initial_detection_sha256": request.observation_manifest_sha256,
        "consumed_inputs_sha256": "1" * 64,
        "compatibility_sha256": request.compatibility_sha256, "graph_sha256": "2" * 64,
    }) if completed else None
    return StagedBuildState(
        revision=2,
        repo_uuid=intent.repo_uuid,
        generation_id=intent.generation_id,
        request=request,
        lifecycle_state=lifecycle_state,
        operation_epoch=None if lifecycle_state == "REQUESTED" else 4,
        fence_token=None if lifecycle_state == "REQUESTED" else 3,
        payload_manifest_sha256="3" * 64 if completed else None,
        receipt_sha256="4" * 64 if lifecycle_state == "CERTIFIED" else None,
        pointer_revision=None,
        completion_binding=completion,
        abandonment_intent=intent,
    ).to_dict()


@pytest.mark.parametrize("lifecycle_state", ["REQUESTED", "PUBLISHING", "COMPLETE", "CERTIFIED"])
def test_abandonment_allows_new_grant_and_later_global_epoch(lifecycle_state):
    value = _staged_with_intent(lifecycle_state)
    state = StagedBuildState.from_mapping(value)
    assert state.abandonment_intent.operation_epoch < state.abandonment_intent.evidence.operation_epoch
    assert state.to_dict() == value
    assert StagedBuildState.from_json(state.canonical) == state


@pytest.mark.parametrize("lifecycle_state", ["PUBLISHING", "COMPLETE", "CERTIFIED"])
def test_abandonment_allows_same_attempt_grant(lifecycle_state):
    value = _staged_with_intent(lifecycle_state)
    value["abandonment_intent"].update(operation_epoch=4, fence_token=3)
    assert StagedBuildState.from_json(canonical_json_bytes(value)).to_dict() == value


def test_intent_cannot_claim_an_epoch_newer_than_its_evidence():
    value = _staged_with_intent()["abandonment_intent"]
    value["operation_epoch"] = 10
    with pytest.raises(ContractError, match="operation_epoch"):
        StagedBuildAbandonmentIntent.from_mapping(value)


@pytest.mark.parametrize("lifecycle_state,field,value", [
    ("REQUESTED", "operation_epoch", 2),
    ("REQUESTED", "operation_epoch", 3),
    ("REQUESTED", "operation_epoch", 10),
    ("PUBLISHING", "operation_epoch", 3),
    ("PUBLISHING", "fence_token", 2),
    ("PUBLISHING", "operation_epoch", 4),
    ("PUBLISHING", "fence_token", 3),
    ("COMPLETE", "operation_epoch", 3),
    ("COMPLETE", "fence_token", 2),
    ("CERTIFIED", "operation_epoch", 3),
    ("CERTIFIED", "fence_token", 2),
])
@pytest.mark.parametrize("reader", [StagedBuildState.from_mapping, StagedBuildState.from_json])
def test_abandonment_rejects_authority_outside_recorded_bounds(lifecycle_state, field, value, reader):
    document = _staged_with_intent(lifecycle_state)
    document["abandonment_intent"][field] = value
    payload = canonical_json_bytes(document) if reader.__name__ == "from_json" else document
    with pytest.raises(ContractError, match=field):
        reader(payload)
