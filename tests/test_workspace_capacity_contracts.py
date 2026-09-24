"""Current capacity reservations require an exact compatibility binding."""

from dataclasses import replace

import pytest

from graphify.workspace.lifecycle_contracts import (
    CapacityReservationState,
    ContractError,
    canonical_json_bytes,
)


def _capacity_state(format_version=2):
    return {
        "contract": "graphify.workspace.capacity_reservations.internal",
        "format_version": format_version,
        "revision": 1,
        "reservations": [{
            "repo_uuid": "550e8400-e29b-41d4-a716-446655440000",
            "generation_id": "gen-test",
            "reserved_bytes": 1024,
            "policy_sha256": "a" * 64,
            "compatibility_sha256": "b" * 64,
            "active_source_revision": 1,
            "operation_epoch": 1,
            "fence_token": 1,
            "created_at": "2026-09-23T00:00:00Z",
        }],
    }


@pytest.mark.parametrize("format_version", [1, 2])
def test_capacity_digest_roundtrips(format_version):
    value = _capacity_state(format_version)
    state = CapacityReservationState.from_mapping(value)
    assert state.to_dict() == value
    assert CapacityReservationState.from_json(state.canonical) == state


def test_legacy_capacity_without_binding_preserves_v1_bytes():
    value = _capacity_state(1)
    del value["reservations"][0]["compatibility_sha256"]
    raw = canonical_json_bytes(value)
    state = CapacityReservationState.from_json(raw)
    assert state.reservations[0].compatibility_sha256 == "legacy-unbound"
    assert state.canonical == raw


@pytest.mark.parametrize("format_version", [1, 2])
@pytest.mark.parametrize("reader", [CapacityReservationState.from_mapping,
                                    CapacityReservationState.from_json])
def test_capacity_rejects_explicit_legacy_sentinel(format_version, reader):
    value = _capacity_state(format_version)
    value["reservations"][0]["compatibility_sha256"] = "legacy-unbound"
    payload = canonical_json_bytes(value) if reader.__name__ == "from_json" else value
    with pytest.raises(ContractError, match="compatibility_sha256"):
        reader(payload)


def test_current_capacity_cannot_serialize_unbound_legacy_reservation():
    value = _capacity_state(1)
    del value["reservations"][0]["compatibility_sha256"]
    legacy = CapacityReservationState.from_mapping(value)
    with pytest.raises(ContractError, match="compatibility_sha256"):
        replace(legacy, format_version=2).canonical


@pytest.mark.parametrize("reader", [CapacityReservationState.from_mapping,
                                    CapacityReservationState.from_json])
def test_current_capacity_requires_compatibility_field(reader):
    value = _capacity_state()
    del value["reservations"][0]["compatibility_sha256"]
    payload = canonical_json_bytes(value) if reader.__name__ == "from_json" else value
    with pytest.raises(ContractError, match="compatibility_sha256"):
        reader(payload)
