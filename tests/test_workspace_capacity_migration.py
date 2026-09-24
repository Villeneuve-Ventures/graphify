"""Capacity updates preserve legacy evidence until unbound reservations leave."""

from dataclasses import replace
from pathlib import Path

from graphify.workspace.lifecycle_contracts import CapacityReservation, CapacityReservationState
from tests.test_workspace_lifecycle_s3 import POLICY, _runtime
from tests.workspace_s3_helpers import REPO_UUID, START


def test_capacity_updates_retain_legacy_format_until_last_unbound_reservation_leaves(tmp_path):
    harness, generations, _, _ = _runtime(tmp_path)
    legacy_a = CapacityReservation(
        repo_uuid=REPO_UUID, generation_id="gen-legacy-a", reserved_bytes=1024,
        policy_sha256=POLICY.sha256, compatibility_sha256="legacy-unbound",
        active_source_revision=1, operation_epoch=1, fence_token=1,
        created_at=START.isoformat(),
    )
    legacy_b = replace(legacy_a, generation_id="gen-legacy-b")
    bound = replace(legacy_a, generation_id="gen-new",
                    compatibility_sha256=generations.compatibility_sha256)
    initial = CapacityReservationState(revision=1, reservations=(legacy_a, legacy_b), format_version=1)
    with harness.registry.exclusive_lock():
        generations.state.commit_record(
            label="capacity", current=Path("capacity.json"),
            previous=Path("capacity.previous.json"), pending=Path("capacity.pending.json"),
            payload=initial.canonical, decoder=CapacityReservationState.from_json,
        )
        mixed = generations._commit_capacity_locked((legacy_a, legacy_b, bound), prior_revision=1)
        assert mixed.format_version == 1
        assert mixed.reservations == (legacy_a, legacy_b, bound)
        assert "compatibility_sha256" not in mixed.to_dict()["reservations"][0]
        assert mixed.to_dict()["reservations"][2]["compatibility_sha256"] == bound.compatibility_sha256
        assert generations._load_capacity_locked() == mixed

        generations._clear_reservation_locked(REPO_UUID, legacy_a.generation_id)
        remaining = generations._load_capacity_locked()
        assert remaining.format_version == 1
        assert remaining.reservations == (legacy_b, bound)

        generations._clear_reservation_locked(REPO_UUID, legacy_b.generation_id)
        upgraded = generations._load_capacity_locked()
        assert upgraded.format_version == 2
        assert upgraded.reservations == (bound,)
        assert upgraded.revision == 4
