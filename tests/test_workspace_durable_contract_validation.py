"""Durable JSON preserves exact bytes and lease epochs cannot exceed allocation state."""

import hashlib
import json

import pytest

from graphify.workspace import lifecycle_contracts as contracts
from tests.test_workspace_lifecycle_contracts import REPO_UUID, _prior_pointer, _receipt

NOW = "2026-09-23T00:00:00Z"


def _lease(operation="BUILD"):
    return {
        "contract": "graphify.workspace.fenced_lease", "schema_version": 2,
        "repo_uuid": REPO_UUID, "operation": operation, "fence_token": 1,
        "owner": {"boot_id": "boot", "pid": 1, "process_start_id": "start"},
        "acquired_at": NOW, "heartbeat_at": NOW, "liveness_deadline_monotonic_ns": 1,
    }


def _documents(root):
    source = {
        "path": "/source", "git_common_dir": "/source/.git", "worktree_id": "main",
        "remote_aliases": [{"url": "https://example.com/repo", "evidence_sha256": "a" * 64}],
    }
    prior = _prior_pointer()
    return [
        {"contract": "graphify.workspace.config", "schema_version": 1,
         "repo_uuid": REPO_UUID,
         "policy": {"freshness": "current_only", "semantic_mode": "host_agent_only",
                    "network_egress": False, "headless_backends": []}},
        {"contract": "graphify.workspace.registry", "schema_version": 2, "revision": 1,
         "workspaces": [{
             "repo_uuid": REPO_UUID,
             "uuid_enrollment": {"repo_uuid": REPO_UUID,
                                 "immutable_evidence_sha256": "a" * 64,
                                 "current_evidence_sha256": "a" * 64},
             "active_source_revision": 1, "active_source": source, "aliases": [],
             "active_source_evidence": {
                 "active_source_revision": 1, "source_sha256": contracts.canonical_sha256(source),
                 "rebind_evidence_sha256": "a" * 64, "operation_epoch": 1, "fence_token": 1,
             },
         }]},
        _receipt(root),
        {"contract": "graphify.workspace.journal_event", "schema_version": 2,
         "event_id": REPO_UUID, "sequence": 1, "transition": "ALLOCATED",
         "generation_id": "gen-test", "prior_event_sha256": None, "receipt_sha256": None,
         "pointer_revision": None, "operation_epoch": 1, "fence_token": 1, "occurred_at": NOW},
        _lease(), prior["pointer_set"], prior,
        {"contract": "graphify.workspace.generation_coordination_lock", "schema_version": 2,
         "lock_id": "generation:test", "generation_id": "gen-test",
         "relative_path": "locks/generations/gen-test.lock", "installed_before_state": "CERTIFIED",
         "query_lock": "read_only_shared_advisory", "gc_lock": "exclusive_then_reachability_recheck",
         "retention": "retain_v1"},
    ]


@pytest.fixture(params=list(contracts._MODEL_BY_CONTRACT), ids=lambda name: name.rsplit(".", 1)[-1])
def document(request, tmp_path):
    values = {value["contract"]: value for value in _documents(tmp_path)}
    assert set(values) == set(contracts._MODEL_BY_CONTRACT)
    return contracts.parse_contract(values[request.param])


@pytest.mark.parametrize("as_text", [False, True], ids=["bytes", "str"])
def test_public_durable_contracts_roundtrip_exact_bytes(document, as_text):
    raw = document.canonical.decode("utf-8") if as_text else document.canonical
    for restored in (type(document).from_json(raw), contracts.ContractDocument.from_json(raw),
                     contracts.parse_contract(raw), contracts.parse_contract(raw, expected=type(document))):
        assert restored.canonical == document.canonical
        assert restored.sha256 == hashlib.sha256(document.canonical).hexdigest()


@pytest.mark.parametrize("encoding", ["whitespace", "key_order", "missing_newline"])
@pytest.mark.parametrize("as_text", [False, True], ids=["bytes", "str"])
def test_public_durable_contracts_reject_noncanonical_json(document, encoding, as_text):
    if encoding == "whitespace":
        raw = b" " + document.canonical
    elif encoding == "key_order":
        value = dict(reversed(list(document.to_dict().items())))
        raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    else:
        raw = document.canonical[:-1]
    assert raw != document.canonical
    payload = raw.decode("utf-8") if as_text else raw
    for reader in (type(document).from_json, contracts.ContractDocument.from_json,
                   contracts.parse_contract):
        with pytest.raises(contracts.ContractError, match="canonical"):
            reader(payload)
    assert contracts.parse_contract(json.loads(raw)).canonical == document.canonical


def test_mapping_normalization_still_supports_unicode():
    value = _lease()
    value["owner"]["boot_id"] = "cafe\u0301"
    document = contracts.FencedLease.from_mapping(value)
    assert document.to_dict()["owner"]["boot_id"] == "café"
    assert contracts.parse_contract(value).canonical == document.canonical
    with pytest.raises(contracts.ContractError, match="canonical"):
        contracts.FencedLease.from_json(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )


def _lease_state(epochs, operation_epoch=1):
    return {
        "contract": "graphify.workspace.lease_state.internal", "format_version": 1,
        "repo_uuid": REPO_UUID, "revision": 1, "fence_high_watermark": 1,
        "operation_epoch": operation_epoch, "migration_epoch": 0,
        "leases": {domain: _lease("SEMANTIC_CLAIM" if domain == "semantic" else "BUILD")
                   for domain in epochs},
        "lease_epochs": epochs,
    }


@pytest.mark.parametrize("domain", ["workspace", "semantic"])
def test_lease_epoch_cannot_exceed_operation_epoch(domain):
    value = _lease_state({domain: 10})
    with pytest.raises(contracts.ContractError, match=rf"lease_epochs\.{domain}.*operation_epoch"):
        contracts.WorkspaceLeaseState.from_mapping(value)
    with pytest.raises(contracts.ContractError, match="operation_epoch"):
        contracts.WorkspaceLeaseState.from_json(contracts.canonical_json_bytes(value))


@pytest.mark.parametrize("epochs", [{}, {"workspace": 10}, {"semantic": 10},
                                    {"workspace": 1, "semantic": 10},
                                    {"workspace": 9, "semantic": 1}])
def test_current_and_older_lease_epochs_remain_valid(epochs):
    value = _lease_state(epochs, operation_epoch=10)
    state = contracts.WorkspaceLeaseState.from_mapping(value)
    assert state.lease_epochs == epochs
    assert contracts.WorkspaceLeaseState.from_json(state.canonical).to_dict() == value
