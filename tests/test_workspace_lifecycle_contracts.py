"""Lifecycle authority binds payload digests and nested document versions."""

import hashlib

import pytest

from graphify.source_io import SourceIO
from graphify.workspace import lifecycle_contracts
from graphify.workspace.contracts import CompletionBinding, InputManifest
from graphify.workspace.lifecycle_contracts import (
    ContractError, GenerationReceipt, PointerSet, PriorPointerRecord,
    payload_manifest_sha256,
)

REPO_UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize("model,contract", [
    ("FreshnessRelease", "freshness_release"),
    ("ArtifactManifest", "artifact_manifest"),
    ("CompatibilityManifest", "compatibility_manifest"),
    ("InstallerTransaction", "installer_transaction"),
    ("CompensationPlan", "compensation_plan"),
    ("OfflineRollback", "offline_rollback"),
])
def test_lifecycle_does_not_expose_deferred_donor_contracts(model, contract):
    with pytest.raises(ContractError, match="unknown contract"):
        lifecycle_contracts.parse_contract({
            "contract": f"graphify.workspace.{contract}", "schema_version": 2,
        })
    assert not hasattr(lifecycle_contracts, model)


def test_every_exposed_lifecycle_model_has_a_validator():
    assert set(lifecycle_contracts._MODEL_BY_CONTRACT) == set(lifecycle_contracts._VALIDATORS)


def _receipt(root):
    with SourceIO(root.resolve()) as inputs:
        inputs.listdir(root.resolve())
        consumed = InputManifest.from_engine(inputs, phase="consumed", code_inputs=[])
    graph_bytes = b"{}\n"
    graph_digest = hashlib.sha256(graph_bytes).hexdigest()
    entries = [
        {"path": "graphify-out/graph.json", "file_type": "regular_file",
         "size": len(graph_bytes), "sha256": graph_digest, "mode": "0600"},
        {"path": "graphify-out/input-manifest.json", "file_type": "regular_file",
         "size": len(consumed.canonical), "sha256": consumed.sha256, "mode": "0600"},
    ]
    completion = CompletionBinding.from_mapping({
        "contract": "graphify.workspace.structural-completion", "state_schema_version": 2,
        "initial_detection_sha256": "c" * 64, "consumed_inputs_sha256": consumed.sha256,
        "compatibility_sha256": "e" * 64, "graph_sha256": graph_digest,
    })
    return {
        "contract": "graphify.workspace.generation_receipt", "schema_version": 2,
        "repo_uuid": REPO_UUID, "generation_id": "gen-test", "lifecycle_state": "CERTIFIED",
        "source_commit": "f" * 40, "source_epoch": 1, "active_source_revision": 1,
        "operation_epoch": 1, "fence_token": 1, "policy_sha256": "f" * 64,
        "observation_manifest_sha256": "c" * 64, "queue_watermark": 1,
        "semantic_completeness": "not_required", "compatibility_sha256": "e" * 64,
        "completion_binding": completion.to_dict(), "coordination_lock_id": "generation:test",
        "sealed_query_payload": {
            "root": "graphify-out",
            "manifest_sha256": payload_manifest_sha256("graphify-out", entries),
            "entries": entries,
        },
        "validations": ["coordination_lock_precreated", "payload_manifest", "stable_semantic_queue"],
    }


def test_receipt_rejects_consumed_manifest_digest_mismatch(tmp_path):
    receipt = _receipt(tmp_path)
    assert GenerationReceipt.from_mapping(receipt).to_dict() == receipt
    receipt["completion_binding"]["consumed_inputs_sha256"] = "d" * 64
    with pytest.raises(ContractError, match="consumed inputs"):
        GenerationReceipt.from_mapping(receipt)


def _prior_pointer():
    pointer = PointerSet.from_mapping({
        "contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 1, "active_source_revision": 1,
        "source_epoch": 1, "operation_epoch": 1, "fence_token": 1, "state_schema_version": 2,
        "current": {"generation_id": "gen-test", "receipt_sha256": "a" * 64},
        "last_good": None,
    })
    return {
        "contract": "graphify.workspace.prior_pointer", "schema_version": 2,
        "retained_at": "2026-09-23T00:00:00Z", "replaced_by_revision": 2,
        "pointer_set": pointer.to_dict(),
    }


@pytest.mark.parametrize("field,value", [
    ("contract", "graphify.workspace.unknown"),
    ("schema_version", 1),
    ("schema_version", 999),
])
def test_prior_pointer_validates_nested_contract_envelope(field, value):
    prior = _prior_pointer()
    assert PriorPointerRecord.from_mapping(prior).to_dict() == prior
    prior["pointer_set"][field] = value
    with pytest.raises(ContractError):
        PointerSet.from_mapping(prior["pointer_set"])
    with pytest.raises(ContractError):
        PriorPointerRecord.from_mapping(prior)
