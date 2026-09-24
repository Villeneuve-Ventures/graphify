"""Lifecycle input limits fail through the documented ContractError boundary."""

import json

import pytest

from graphify.workspace import lifecycle_contracts as contracts


@pytest.mark.parametrize("depth", [64, 1500])
def test_nested_json_is_rejected_with_contract_error(depth):
    payload = ('{"contract":"graphify.workspace.registry","schema_version":2,'
               '"unexpected":' + '{"nested":' * depth + 'null' + '}' * depth + '}')
    with pytest.raises(contracts.ContractError, match="nesting|invalid JSON"):
        contracts.Registry.from_json(payload)


def test_mapping_and_canonical_normalization_have_the_same_depth_bound():
    value = {}
    current = value
    for _ in range(64):
        nested = {}
        current["nested"] = nested
        current = nested
    value.update(contract="graphify.workspace.registry", schema_version=2)
    for convert in (contracts.Registry.from_mapping, contracts.canonical_json_bytes):
        with pytest.raises(contracts.ContractError, match="nesting"):
            convert(value)


@pytest.mark.parametrize("reader", [
    contracts.Registry.from_json, contracts.WorkspaceLeaseState.from_json,
    contracts.parse_contract,
])
@pytest.mark.parametrize("payload", [b" " * 129, " " * 129, '"' + "é" * 64 + '"'],
                         ids=["bytes", "text", "utf8"])
def test_oversized_json_rejected_before_decoder(monkeypatch, reader, payload):
    monkeypatch.setattr(contracts, "LIFECYCLE_JSON_MAX_BYTES", 128, raising=False)
    reads = []
    original = json.loads

    def decode(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(contracts.json, "loads", decode)
    with pytest.raises(contracts.ContractError):
        reader(payload)
    assert reads == []


def test_valid_json_at_byte_limit_preserves_canonical_bytes(monkeypatch):
    document = contracts.WorkspaceConfig.from_mapping({
        "contract": "graphify.workspace.config", "schema_version": 1,
        "repo_uuid": "550e8400-e29b-41d4-a716-446655440000",
        "policy": {"freshness": "current_only", "semantic_mode": "host_agent_only",
                   "network_egress": False, "headless_backends": []},
    })
    monkeypatch.setattr(contracts, "LIFECYCLE_JSON_MAX_BYTES", len(document.canonical), raising=False)
    for payload in (document.canonical, document.canonical.decode("utf-8")):
        assert contracts.WorkspaceConfig.from_json(payload).canonical == document.canonical
        assert contracts.parse_contract(payload).canonical == document.canonical
    monkeypatch.setattr(contracts, "LIFECYCLE_JSON_MAX_BYTES", len(document.canonical) - 1)
    with pytest.raises(contracts.ContractError, match="byte limit"):
        contracts.WorkspaceConfig.from_json(document.canonical)


def test_json_numeric_decoder_error_is_contract_error():
    with pytest.raises(contracts.ContractError, match="invalid JSON"):
        contracts.Registry.from_json('{"revision":' + '1' * 10000 + '}')


def test_decoder_recursion_failure_is_contract_error(monkeypatch):
    def fail(*args, **kwargs):
        raise RecursionError("decoder nesting limit")

    monkeypatch.setattr(contracts.json, "loads", fail)
    with pytest.raises(contracts.ContractError, match="invalid JSON"):
        contracts.Registry.from_json("{}")
