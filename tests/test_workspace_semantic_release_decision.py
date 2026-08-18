"""Private P5B2 semantic-release decision-store and capacity/GC boundary."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import time
from typing import cast

import pytest

import graphify.workspace as workspace
import graphify.workspace.generations as generations_module
import graphify.workspace.persistence as persistence_module
from graphify.workspace.composition import WorkspaceRuntimeInputs, compose_workspace_runtime
from graphify.workspace.contracts import CapacityPolicy, canonical_json_bytes
from graphify.workspace.gc import GcError, GcProtection, GcStore
from graphify.workspace.generations import CapacityExceeded, GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    StatePathError,
)
from graphify.workspace.pointers import PointerStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from graphify.workspace.semantic_release_decision import (
    DECISION_BINDING_MAX_BYTES,
    DECISION_BINDINGS_PER_GENERATION,
    DECISION_BINDINGS_PER_WORKSPACE,
    SemanticReleaseDecisionBinding,
    SemanticReleaseDecisionConflict,
    SemanticReleaseDecisionInvalid,
    SemanticReleaseDecisionStore,
)
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    SUPPORTED,
    acquire,
    create_harness,
    metadata_snapshot,
    tree_snapshot,
)


POLICY = CapacityPolicy.from_mapping(
    {
        "contract": "graphify.workspace.capacity_policy.internal",
        "format_version": 1,
        "global_max_bytes": 256 * 1024 * 1024,
        "global_max_generations": 16,
        "workspace_max_bytes": 128 * 1024 * 1024,
        "workspace_max_generations": 8,
        "reserve_bytes": 1,
    }
)
EMPTY_PROTECTION = GcProtection(
    migration_sources=frozenset(),
    rollback_sources=frozenset(),
    active_lease_generations=frozenset(),
    fixture_generations=frozenset(),
    proof_generations=frozenset(),
    rollback_artifact_generations=frozenset(),
)
GENERATION_ID = "gen-decision"
REQUEST_SHA256 = "1" * 64


class CrashAt:
    def __init__(self, event: str) -> None:
        self.event = event
        self.fired = False

    def __call__(self, event: str) -> None:
        if event == self.event and not self.fired:
            self.fired = True
            raise InjectedFault(event)


def _runtime(tmp_path: Path, *, fault_hook=None):
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=SUPPORTED,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=SUPPORTED,
    )
    workspace_directory = Path("workspaces") / REPO_UUID
    generation = workspace_directory / "generations" / GENERATION_ID
    generations.state.ensure_directory(generation)
    generations.state.install_once_bytes(
        generation / "payload.bin",
        b"certified private fixture",
        label="test:generation",
    )
    lock = workspace_directory / "locks" / "generations" / f"{GENERATION_ID}.lock"
    generations.state.install_once_bytes(lock, b"generation lock\n", label="test:lock")
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=SUPPORTED,
    )
    gc = GcStore(
        harness.state_root,
        harness.leases,
        generations,
        pointers,
        capabilities=SUPPORTED,
    )
    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
    )
    return harness, generations, store, gc


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding_value(*, terminal_outcome: str = "REJECTED") -> dict[str, object]:
    field_results = [
        {
            "entity_kind": "node",
            "entity_id": "node-a",
            "field_name": "label",
            "field_value_sha256": _digest("public label"),
            "classifier_outcome": "NO_MATCH",
            "category_ids": [],
            "rule_ids": [],
            "disposition": "ALLOW_FIELD",
        },
        {
            "entity_kind": "node",
            "entity_id": "node-a",
            "field_name": "rationale",
            "field_value_sha256": _digest("private rationale"),
            "classifier_outcome": "MATCH",
            "category_ids": ["secret.api_key"],
            "rule_ids": ["core_secrets.api_key.v1"],
            "disposition": "OMIT_RATIONALE",
        },
        {
            "entity_kind": "hyperedge",
            "entity_id": "edge-a",
            "field_name": "label",
            "field_value_sha256": _digest("private edge"),
            "classifier_outcome": "MATCH",
            "category_ids": ["secret.api_key"],
            "rule_ids": ["core_secrets.api_key.v1"],
            "disposition": "REJECT_RELEASE",
        },
    ]
    counts = {
        "node_label_count": 1,
        "node_rationale_count": 1,
        "hyperedge_label_count": 1,
        "field_result_count": 3,
        "matched_field_count": 2,
    }
    inventory_sha256 = "8" * 64
    full_result = {
        "eligible_field_inventory_sha256": inventory_sha256,
        "counts": counts,
        "field_results": field_results,
        "terminal_outcome": terminal_outcome,
    }
    return {
        "contract": "graphify.workspace.semantic_release_decision.internal",
        "format_version": 1,
        "repo_uuid": REPO_UUID,
        "target_generation_id": GENERATION_ID,
        "decision_request_sha256": REQUEST_SHA256,
        "promoted_entry_sha256": "2" * 64,
        "bundle_manifest_sha256": "3" * 64,
        "policy_authority_revision": 1,
        "policy_authority_sha256": "4" * 64,
        "semantic_input_byte_count": 1024,
        "semantic_input_sha256": "5" * 64,
        "eligible_field_inventory_sha256": inventory_sha256,
        "taxonomy_sha256": "9" * 64,
        "normalization_sha256": "a" * 64,
        "classifier_implementation_sha256": "b" * 64,
        "classifier_abi_sha256": "c" * 64,
        "ruleset_sha256": "d" * 64,
        "selected_profile_sha256s": ["e" * 64],
        "coverage_sufficiency_sha256": "f" * 64,
        "policy_sha256": "0" * 64,
        "counts": counts,
        "field_results": field_results,
        "terminal_outcome": terminal_outcome,
        "full_result_sha256": hashlib.sha256(canonical_json_bytes(full_result)).hexdigest(),
    }


def _binding(*, terminal_outcome: str = "REJECTED") -> SemanticReleaseDecisionBinding:
    return SemanticReleaseDecisionBinding.from_mapping(
        _binding_value(terminal_outcome=terminal_outcome)
    )


def _binding_path(root: Path, request_sha256: str = REQUEST_SHA256) -> Path:
    return (
        root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decisions"
        / GENERATION_ID
        / f"{request_sha256}.json"
    )


def test_binding_closes_members_orders_results_and_uses_exact_digest_preimages() -> None:
    binding = _binding()
    value = binding.to_dict()

    assert binding.canonical == canonical_json_bytes(value)
    assert binding.binding_sha256 == hashlib.sha256(binding.canonical).hexdigest()
    assert value["full_result_sha256"] == hashlib.sha256(
        canonical_json_bytes(
            {
                "eligible_field_inventory_sha256": value[
                    "eligible_field_inventory_sha256"
                ],
                "counts": value["counts"],
                "field_results": value["field_results"],
                "terminal_outcome": value["terminal_outcome"],
            }
        )
    ).hexdigest()
    assert cast(list[dict[str, object]], value["field_results"])[0][
        "field_value_sha256"
    ] == hashlib.sha256(b"public label").hexdigest()
    assert b"public label" not in binding.canonical
    assert b"private rationale" not in binding.canonical
    assert len(binding.canonical) <= DECISION_BINDING_MAX_BYTES
    assert DECISION_BINDINGS_PER_GENERATION == 64
    assert DECISION_BINDINGS_PER_WORKSPACE == 4_096

    extra = _binding_value()
    extra["binding_sha256"] = "a" * 64
    with pytest.raises(SemanticReleaseDecisionInvalid, match="members"):
        SemanticReleaseDecisionBinding.from_mapping(extra)

    reordered = _binding_value()
    cast(list[object], reordered["field_results"]).reverse()
    with pytest.raises(SemanticReleaseDecisionInvalid, match="order"):
        SemanticReleaseDecisionBinding.from_mapping(reordered)

    inconsistent = _binding_value(terminal_outcome="ALLOW_WITH_OMISSIONS")
    with pytest.raises(SemanticReleaseDecisionInvalid, match="terminal_outcome"):
        SemanticReleaseDecisionBinding.from_mapping(inconsistent)

    unsupported_rejection = _binding_value()
    unsupported_results = cast(
        list[dict[str, object]], unsupported_rejection["field_results"]
    )
    unsupported_results[-1]["disposition"] = "ALLOW_FIELD"
    unsupported_rejection["full_result_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {
                "eligible_field_inventory_sha256": unsupported_rejection[
                    "eligible_field_inventory_sha256"
                ],
                "counts": unsupported_rejection["counts"],
                "field_results": unsupported_results,
                "terminal_outcome": unsupported_rejection["terminal_outcome"],
            }
        )
    ).hexdigest()
    with pytest.raises(SemanticReleaseDecisionInvalid, match="terminal_outcome"):
        SemanticReleaseDecisionBinding.from_mapping(unsupported_rejection)


def test_capture_is_bounded_read_only_and_constructor_adds_no_public_surface(
    tmp_path: Path,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    before = tree_snapshot(harness.state_root)

    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    assert capture.generation_binding_count == 0
    assert capture.workspace_binding_count == 0
    assert capture.existing_binding_sha256 is None
    assert tree_snapshot(harness.state_root) == before
    assert not _binding_path(harness.state_root).parent.parent.exists()
    assert not hasattr(workspace, "SemanticReleaseDecisionStore")
    assert not hasattr(store, "delete")
    assert not hasattr(store, "repair")
    assert not hasattr(store, "gc")


def test_present_empty_decision_namespace_is_not_canonical_absence(
    tmp_path: Path,
) -> None:
    harness, generations, store, _gc = _runtime(tmp_path)
    decision_root = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decisions"
    )
    decision_root.mkdir(mode=0o700)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(CapacityExceeded, match="empty"):
        store.capture(
            REPO_UUID,
            GENERATION_ID,
            REQUEST_SHA256,
            capacity_policy=POLICY,
        )
    with pytest.raises(CapacityExceeded, match="empty"):
        generations.decision_state_generations_locked(REPO_UUID)

    assert tree_snapshot(harness.state_root) == before


def test_install_once_reopens_exact_private_file_and_replay_is_no_write(tmp_path: Path) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    installed = store.install(capture, binding, capacity_policy=POLICY)
    path = _binding_path(harness.state_root)
    before = metadata_snapshot(path.parent.parent)
    details = path.stat()

    assert installed.canonical == binding.canonical
    assert path.read_bytes() == binding.canonical
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(details.st_mode) == 0o600
    assert details.st_nlink == 1

    replay_capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    replay = store.install(replay_capture, binding, capacity_policy=POLICY)
    assert replay.canonical == binding.canonical
    assert metadata_snapshot(path.parent.parent) == before

    conflict_value = _binding_value()
    conflict_value["policy_sha256"] = "7" * 64
    conflict = SemanticReleaseDecisionBinding.from_mapping(conflict_value)
    with pytest.raises(SemanticReleaseDecisionConflict, match="different bytes"):
        store.install(replay_capture, conflict, capacity_policy=POLICY)
    assert path.read_bytes() == binding.canonical


def test_possible_visibility_adopts_exact_bytes_and_unsafe_namespace_fails_closed(
    tmp_path: Path,
) -> None:
    crash = CrashAt("semantic-release-decision:installed")
    harness, _generations, store, _gc = _runtime(tmp_path, fault_hook=crash)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    assert store.install(capture, binding, capacity_policy=POLICY) == binding
    assert crash.fired
    assert _binding_path(harness.state_root).read_bytes() == binding.canonical

    other = tmp_path / "other"
    other.mkdir()
    unsafe = (
        harness.state_root / "workspaces" / REPO_UUID / "semantic-release-decisions-unsafe"
    )
    unsafe.symlink_to(other, target_is_directory=True)
    decision_root = unsafe.with_name("semantic-release-decisions")
    decision_root.rename(decision_root.with_name("semantic-release-decisions-valid"))
    unsafe.rename(decision_root)
    with pytest.raises((SemanticReleaseDecisionInvalid, CapacityExceeded, StatePathError)):
        store.capture(
            REPO_UUID,
            GENERATION_ID,
            REQUEST_SHA256,
            capacity_policy=POLICY,
        )


def test_possible_visibility_adoption_revalidates_capacity_namespace(
    tmp_path: Path,
) -> None:
    state_root: Path | None = None
    other_request = "2" * 64
    other_value = _binding_value()
    other_value["decision_request_sha256"] = other_request
    other_binding = SemanticReleaseDecisionBinding.from_mapping(other_value)

    def install_unrelated_binding_then_fail(event: str) -> None:
        if event != "semantic-release-decision:installed":
            return
        assert state_root is not None
        other_path = _binding_path(state_root, other_request)
        other_path.write_bytes(other_binding.canonical)
        other_path.chmod(0o600)
        raise InjectedFault(event)

    harness, _generations, store, _gc = _runtime(
        tmp_path,
        fault_hook=install_unrelated_binding_then_fail,
    )
    state_root = harness.state_root
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    with pytest.raises(CommitUnknown, match="decision namespace"):
        store.install(capture, binding, capacity_policy=POLICY)

    assert _binding_path(harness.state_root).read_bytes() == binding.canonical
    assert _binding_path(harness.state_root, other_request).read_bytes() == (
        other_binding.canonical
    )


@pytest.mark.parametrize(
    "hostile_kind",
    ["hardlink", "special_file", "unexpected_entry", "unsafe_mode"],
)
def test_authoritative_scanner_rejects_hostile_binding_entries_without_writes(
    tmp_path: Path,
    hostile_kind: str,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    store.install(capture, binding, capacity_policy=POLICY)
    path = _binding_path(harness.state_root)
    hostile = path.with_name(f"{'2' * 64}.json")
    if hostile_kind == "hardlink":
        os.link(path, hostile)
    elif hostile_kind == "special_file":
        os.mkfifo(hostile)
    elif hostile_kind == "unexpected_entry":
        hostile = path.with_name("foreign.txt")
        hostile.write_bytes(b"foreign")
        hostile.chmod(0o600)
    else:
        path.chmod(0o644)
    before = tree_snapshot(harness.state_root)

    with pytest.raises((CapacityExceeded, SemanticReleaseDecisionInvalid, StatePathError)):
        store.capture(
            REPO_UUID,
            GENERATION_ID,
            REQUEST_SHA256,
            capacity_policy=POLICY,
        )

    assert tree_snapshot(harness.state_root) == before


@pytest.mark.parametrize("failure", ["reopen", "capacity"])
def test_post_visibility_final_proof_failure_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    harness, generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    if failure == "reopen":
        original_read_binding = store._read_binding
        reads = 0

        def fail_final_reopen(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise SemanticReleaseDecisionInvalid("injected final reopen failure")
            return original_read_binding(*args, **kwargs)

        monkeypatch.setattr(store, "_read_binding", fail_final_reopen)
    else:
        original_usage = generations.decision_capacity_usage_locked
        scans = 0

        def fail_final_capacity(*args, **kwargs):
            nonlocal scans
            scans += 1
            if scans == 2:
                raise CapacityExceeded("injected final capacity failure")
            return original_usage(*args, **kwargs)

        monkeypatch.setattr(
            generations,
            "decision_capacity_usage_locked",
            fail_final_capacity,
        )

    with pytest.raises(CommitUnknown, match="decision install"):
        store.install(capture, binding, capacity_policy=POLICY)

    assert _binding_path(harness.state_root).read_bytes() == binding.canonical


def test_post_install_exact_reopen_rejects_path_inode_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    path = _binding_path(harness.state_root)
    replacement = path.with_name("replacement.tmp")
    original_read = persistence_module.os.read
    matching_reads = 0

    def substitute_path_after_exact_reopen(descriptor: int, maximum: int) -> bytes:
        nonlocal matching_reads
        payload = original_read(descriptor, maximum)
        if payload == binding.canonical:
            matching_reads += 1
            if matching_reads == 3:
                replacement.write_bytes(binding.canonical)
                replacement.chmod(0o600)
                replacement.replace(path)
        return payload

    monkeypatch.setattr(persistence_module.os, "read", substitute_path_after_exact_reopen)

    with pytest.raises(CommitUnknown, match="decision install"):
        store.install(capture, binding, capacity_policy=POLICY)

    assert matching_reads >= 3
    assert path.read_bytes() == binding.canonical
    assert not replacement.exists()


def test_generation_binding_bound_and_capacity_failure_are_no_write(tmp_path: Path) -> None:
    harness, generations, store, _gc = _runtime(tmp_path)
    decision_generation = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decisions"
        / GENERATION_ID
    )
    decision_generation.mkdir(parents=True, mode=0o700)
    decision_generation.parent.chmod(0o700)
    for index in range(65):
        path = decision_generation / f"{index:064x}.json"
        path.write_bytes(b"{}")
        path.chmod(0o600)
    before = tree_snapshot(harness.state_root)
    with pytest.raises(CapacityExceeded, match="maximum|64"):
        store.capture(
            REPO_UUID,
            GENERATION_ID,
            REQUEST_SHA256,
            capacity_policy=POLICY,
        )
    assert tree_snapshot(harness.state_root) == before

    for path in decision_generation.iterdir():
        path.unlink()
    decision_generation.rmdir()
    decision_generation.parent.rmdir()
    binding = _binding()
    current = generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    too_small = replace(
        POLICY,
        workspace_max_bytes=current.workspace_bytes + len(binding.canonical) - 1,
    )
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=too_small,
    )
    before = tree_snapshot(harness.state_root)
    with pytest.raises(CapacityExceeded, match="workspace byte"):
        store.install(capture, binding, capacity_policy=too_small)
    assert tree_snapshot(harness.state_root) == before


def test_preflight_enforces_exact_byte_count_workspace_count_and_reserve_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, generations, _store, _gc = _runtime(tmp_path)
    usage = generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    generous = replace(
        POLICY,
        global_max_bytes=512 * 1024 * 1024,
        workspace_max_bytes=256 * 1024 * 1024,
    )

    generations.preflight_decision_install_locked(
        REPO_UUID,
        GENERATION_ID,
        candidate_bytes=DECISION_BINDING_MAX_BYTES,
        additional_bytes=DECISION_BINDING_MAX_BYTES,
        capacity_policy=generous,
        usage=usage,
    )
    with pytest.raises(CapacityExceeded, match="25 MiB"):
        generations.preflight_decision_install_locked(
            REPO_UUID,
            GENERATION_ID,
            candidate_bytes=DECISION_BINDING_MAX_BYTES + 1,
            additional_bytes=DECISION_BINDING_MAX_BYTES + 1,
            capacity_policy=generous,
            usage=usage,
        )

    near_generation_limit = replace(
        usage,
        decision_bindings_by_generation={(REPO_UUID, GENERATION_ID): 63},
    )
    generations.preflight_decision_install_locked(
        REPO_UUID,
        GENERATION_ID,
        candidate_bytes=1,
        additional_bytes=1,
        capacity_policy=generous,
        usage=near_generation_limit,
    )
    at_generation_limit = replace(
        usage,
        decision_bindings_by_generation={(REPO_UUID, GENERATION_ID): 64},
    )
    with pytest.raises(CapacityExceeded, match="64"):
        generations.preflight_decision_install_locked(
            REPO_UUID,
            GENERATION_ID,
            candidate_bytes=1,
            additional_bytes=1,
            capacity_policy=generous,
            usage=at_generation_limit,
        )

    near_limit_counts = {
        (REPO_UUID, f"other-generation-{index}"): 64 for index in range(63)
    }
    near_limit_counts[(REPO_UUID, "other-generation-final")] = 63
    near_workspace_limit = replace(
        usage,
        decision_bindings_by_generation=near_limit_counts,
    )
    generations.preflight_decision_install_locked(
        REPO_UUID,
        GENERATION_ID,
        candidate_bytes=1,
        additional_bytes=1,
        capacity_policy=generous,
        usage=near_workspace_limit,
    )
    at_limit_counts = dict(near_limit_counts)
    at_limit_counts[(REPO_UUID, "other-generation-final")] = 64
    at_workspace_limit = replace(
        usage,
        decision_bindings_by_generation=at_limit_counts,
    )
    with pytest.raises(CapacityExceeded, match="4096"):
        generations.preflight_decision_install_locked(
            REPO_UUID,
            GENERATION_ID,
            candidate_bytes=1,
            additional_bytes=1,
            capacity_policy=generous,
            usage=at_workspace_limit,
        )

    global_usage = replace(
        usage,
        primary_bytes_by_generation={
            **usage.primary_bytes_by_generation,
            ("22222222-2222-4222-8222-222222222222", "gen-other"): 100,
        },
    )
    with pytest.raises(CapacityExceeded, match="global byte"):
        generations.preflight_decision_install_locked(
            REPO_UUID,
            GENERATION_ID,
            candidate_bytes=30,
            additional_bytes=30,
            capacity_policy=replace(
                generous,
                global_max_bytes=global_usage.global_bytes + 29,
                workspace_max_bytes=usage.workspace_bytes + 30,
            ),
            usage=global_usage,
        )

    reserved_usage = replace(usage, unconsumed_reserved_bytes=20)
    disk_usage = generations_module.shutil.disk_usage(harness.state_root)
    monkeypatch.setattr(
        generations_module.shutil,
        "disk_usage",
        lambda _path: type(disk_usage)(disk_usage.total, disk_usage.used, 149),
    )
    with pytest.raises(CapacityExceeded, match="reserve threshold"):
        generations.preflight_decision_install_locked(
            REPO_UUID,
            GENERATION_ID,
            candidate_bytes=30,
            additional_bytes=30,
            capacity_policy=replace(generous, reserve_bytes=100),
            usage=reserved_usage,
        )


def test_authoritative_capacity_scanner_charges_decision_bytes_and_gc_blocks_preplan(
    tmp_path: Path,
) -> None:
    harness, generations, store, gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    before = generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    store.install(capture, binding, capacity_policy=POLICY)
    after = generations.decision_capacity_usage_locked(REPO_UUID, POLICY)

    assert after.workspace_bytes == before.workspace_bytes + len(binding.canonical)
    assert after.global_bytes == before.global_bytes + len(binding.canonical)
    assert after.workspace_binding_count == 1
    assert after.generation_binding_count(GENERATION_ID) == 1

    grant = acquire(harness, "GC", tick=2)
    with pytest.raises(GcError, match="decision state"):
        gc.plan(
            grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=20_001,
        )
    with pytest.raises(GcError, match="decision state"):
        gc.plan(
            grant,
            capacity_policy=POLICY,
            protections=replace(
                EMPTY_PROTECTION,
                proof_generations=frozenset({GENERATION_ID}),
            ),
            monotonic_ns=20_002,
        )

    before = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    with pytest.raises(GcError, match="decision state"):
        gc.preview(
            REPO_UUID,
            expected_registry_revision=grant.registry_revision,
            expected_active_source_revision=grant.active_source_revision,
            expected_operation_epoch=grant.operation_epoch,
            expected_migration_epoch=grant.migration_epoch,
            expected_pointer_revision=0,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            deadline_ns=time.monotonic_ns() + 5_000_000_000,
        )
    assert tree_snapshot(harness.state_root) == before
    assert metadata_snapshot(harness.state_root) == before_metadata


def test_gc_preview_deadline_is_enforced_during_decision_binding_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, store, gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    store.install(capture, binding, capacity_policy=POLICY)
    grant = acquire(harness, "GC", tick=2)
    now = [1]
    original_read = generations_module.os.read

    def expire_after_binding_read(descriptor: int, maximum: int) -> bytes:
        payload = original_read(descriptor, maximum)
        if payload == binding.canonical:
            now[0] = 3
        return payload

    monkeypatch.setattr(generations_module.os, "read", expire_after_binding_read)
    monkeypatch.setattr(persistence_module.time, "monotonic_ns", lambda: now[0])
    before = tree_snapshot(harness.state_root)

    with pytest.raises(LockTimeout):
        gc.preview(
            REPO_UUID,
            expected_registry_revision=grant.registry_revision,
            expected_active_source_revision=grant.active_source_revision,
            expected_operation_epoch=grant.operation_epoch,
            expected_migration_epoch=grant.migration_epoch,
            expected_pointer_revision=0,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            deadline_ns=2,
        )

    assert tree_snapshot(harness.state_root) == before


def test_authoritative_capacity_scanner_rejects_same_inode_binding_read_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    store.install(capture, binding, capacity_policy=POLICY)
    path = _binding_path(harness.state_root)
    replacement_value = _binding_value()
    replacement_value["policy_sha256"] = "7" * 64
    replacement = SemanticReleaseDecisionBinding.from_mapping(replacement_value)
    assert len(replacement.canonical) == len(binding.canonical)

    original_read = generations_module.os.read
    raced = False

    def racing_read(descriptor: int, maximum: int) -> bytes:
        nonlocal raced
        payload = original_read(descriptor, maximum)
        if not raced and payload == binding.canonical:
            raced = True
            path.write_bytes(replacement.canonical)
            path.chmod(0o600)
        return payload

    monkeypatch.setattr(generations_module.os, "read", racing_read)

    with pytest.raises(CapacityExceeded, match="changed while scanning"):
        generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    assert raced


def test_final_install_rejects_same_size_binding_member_substitution(
    tmp_path: Path,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    prior_request = "2" * 64
    replacement_request = "3" * 64
    prior_value = _binding_value()
    prior_value["decision_request_sha256"] = prior_request
    prior = SemanticReleaseDecisionBinding.from_mapping(prior_value)
    prior_capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        prior_request,
        capacity_policy=POLICY,
    )
    store.install(prior_capture, prior, capacity_policy=POLICY)

    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    replacement_value = _binding_value()
    replacement_value["decision_request_sha256"] = replacement_request
    replacement = SemanticReleaseDecisionBinding.from_mapping(replacement_value)
    assert len(replacement.canonical) == len(prior.canonical)
    prior_path = _binding_path(harness.state_root, prior_request)
    replacement_path = _binding_path(harness.state_root, replacement_request)
    prior_path.rename(replacement_path)
    replacement_path.write_bytes(replacement.canonical)
    replacement_path.chmod(0o600)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionConflict, match="capacity usage changed"):
        store.install(capture, _binding(), capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before
    assert not _binding_path(harness.state_root).exists()


def test_replay_revalidates_namespace_after_reading_existing_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    store.install(capture, binding, capacity_policy=POLICY)
    replay_capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    path = _binding_path(harness.state_root)
    replacement_value = _binding_value()
    replacement_value["policy_sha256"] = "7" * 64
    replacement = SemanticReleaseDecisionBinding.from_mapping(replacement_value)
    assert len(replacement.canonical) == len(binding.canonical)

    original_read = generations_module.os.read
    matching_reads = 0

    def substitute_after_replay_read(descriptor: int, maximum: int) -> bytes:
        nonlocal matching_reads
        payload = original_read(descriptor, maximum)
        if payload == binding.canonical:
            matching_reads += 1
            if matching_reads == 3:
                path.write_bytes(replacement.canonical)
                path.chmod(0o600)
        return payload

    monkeypatch.setattr(generations_module.os, "read", substitute_after_replay_read)

    with pytest.raises(SemanticReleaseDecisionConflict, match="namespace changed"):
        store.install(replay_capture, binding, capacity_policy=POLICY)

    assert matching_reads >= 3
    assert path.read_bytes() == replacement.canonical


def test_same_path_different_bytes_after_preflight_is_a_definite_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    conflict_value = _binding_value()
    conflict_value["policy_sha256"] = "7" * 64
    conflict = SemanticReleaseDecisionBinding.from_mapping(conflict_value)
    path = _binding_path(harness.state_root)
    original_preflight = generations.preflight_decision_install_locked

    def install_conflict_after_preflight(*args, **kwargs) -> None:
        original_preflight(*args, **kwargs)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.parent.chmod(0o700)
        path.parent.chmod(0o700)
        path.write_bytes(conflict.canonical)
        path.chmod(0o600)

    monkeypatch.setattr(
        generations,
        "preflight_decision_install_locked",
        install_conflict_after_preflight,
    )

    with pytest.raises(SemanticReleaseDecisionConflict, match="different bytes"):
        store.install(capture, binding, capacity_policy=POLICY)

    assert path.read_bytes() == conflict.canonical


def test_final_install_revalidation_rejects_unresolved_gc_state_without_binding_write(
    tmp_path: Path,
) -> None:
    harness, _generations, store, gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    gc.state.install_once_bytes(
        gc._intent_path(REPO_UUID),
        b"{}",
        label="test:gc-intent",
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionConflict, match="GC eligibility"):
        store.install(capture, binding, capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before
    assert not _binding_path(harness.state_root).exists()


def test_runtime_composes_private_decision_store_without_public_schema_or_command(
    tmp_path: Path,
) -> None:
    runtime = compose_workspace_runtime(
        WorkspaceRuntimeInputs(
            state_root=tmp_path / "state",
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=SemanticQueuePolicy(
                max_items=16,
                max_bytes=64 * 1024,
                retry_budget=1,
            ),
            capabilities=SUPPORTED,
        )
    )

    assert isinstance(runtime.semantic_release_decisions, SemanticReleaseDecisionStore)
    assert runtime.semantic_release_decisions.generations is runtime.generations
    assert runtime.semantic_release_decisions.registry is runtime.registry
    assert not hasattr(workspace, "SemanticReleaseDecisionStore")
