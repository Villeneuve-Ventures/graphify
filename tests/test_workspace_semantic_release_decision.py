"""Private P5B2 semantic-release decision-store and capacity/GC boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import graphify.workspace as workspace
import graphify.workspace.generations as generations_module
import graphify.workspace.persistence as persistence_module
from graphify.workspace.composition import WorkspaceRuntimeInputs, compose_workspace_runtime
from graphify.workspace.contracts import (
    SEMANTIC_RELEASE_DECISION_STAGING_OVERHEAD_BYTES,
    CapacityPolicy,
    canonical_json_bytes,
)
from graphify.workspace.gc import GcError, GcProtection, GcStore
from graphify.workspace.generations import CapacityExceeded, GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    InjectedFault,
    LockTimeout,
    PosixSyscalls,
    StateCorrupt,
    StatePathError,
)
from graphify.workspace.pointers import PointerStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from graphify.workspace.semantic_release_decision import (
    DECISION_BINDING_MAX_BYTES,
    DECISION_BINDINGS_PER_GENERATION,
    DECISION_BINDINGS_PER_WORKSPACE,
    SemanticReleaseDecisionBinding,
    SemanticReleaseDecisionCapture,
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


class DestinationRaceSyscalls(PosixSyscalls):
    def __init__(self, create_destination) -> None:
        self.create_destination = create_destination
        self.fired = False

    def rename_exclusive_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        if source == "payload" and not self.fired:
            self.fired = True
            self.create_destination()
        super().rename_exclusive_at(
            source,
            destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )


class PostFaultFsyncSyscalls(PosixSyscalls):
    def __init__(self, fault: CrashAt, *, fail_after_fault: bool = False) -> None:
        self.fault = fault
        self.fail_after_fault = fail_after_fault
        self.post_fault_fsync_count = 0

    def fsync(self, descriptor: int) -> None:
        super().fsync(descriptor)
        if self.fault.fired:
            self.post_fault_fsync_count += 1
            if self.fail_after_fault:
                raise InjectedFault("publication:post-fault-fsync")


class CleanupFaultSyscalls(PosixSyscalls):
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.fired = False
        self._after_unlink = False
        self._after_rmdir = False

    def unlink_at(self, path: str, *, dir_fd: int) -> None:
        super().unlink_at(path, dir_fd=dir_fd)
        self._after_unlink = True
        if self.operation == "unlink" and not self.fired:
            self.fired = True
            raise InjectedFault("cleanup:unlink")

    def rmdir_at(self, path: str, *, dir_fd: int) -> None:
        super().rmdir_at(path, dir_fd=dir_fd)
        self._after_rmdir = True
        if self.operation == "rmdir" and not self.fired:
            self.fired = True
            raise InjectedFault("cleanup:rmdir")

    def fsync(self, descriptor: int) -> None:
        super().fsync(descriptor)
        if self.fired:
            return
        if self.operation == "unlink_parent_fsync" and self._after_unlink:
            self.fired = True
            raise InjectedFault("cleanup:unlink-parent-fsync")
        if self.operation == "rmdir_parent_fsync" and self._after_rmdir:
            self.fired = True
            raise InjectedFault("cleanup:rmdir-parent-fsync")


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


def _refresh_full_result_sha256(value: dict[str, object]) -> None:
    value["full_result_sha256"] = hashlib.sha256(
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


def _binding_for(
    generation_id: str,
    request_sha256: str,
) -> SemanticReleaseDecisionBinding:
    value = _binding_value()
    value["target_generation_id"] = generation_id
    value["decision_request_sha256"] = request_sha256
    return SemanticReleaseDecisionBinding.from_mapping(value)


def _binding_path(root: Path, request_sha256: str = REQUEST_SHA256) -> Path:
    return (
        root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decisions"
        / GENERATION_ID
        / f"{request_sha256}.json"
    )


def _arm_decision_directory_rebind_race(
    monkeypatch: pytest.MonkeyPatch,
    decision_directory: Path,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Replace each enumerated canonical directory while its descriptor is held."""

    race_root = decision_directory.parent.with_name("decision-directory-rebind-race")
    race_root.mkdir(mode=0o700)
    observations: list[tuple[tuple[int, int], tuple[int, int]]] = []
    original_names = DurableStateRoot._tree_entry_names_descriptor
    detached_directory: Path | None = None
    hostile_installed = False
    target_scan_armed = False

    def install_hostile_directory(descriptor: int) -> None:
        nonlocal detached_directory, hostile_installed, target_scan_armed
        index = len(observations)
        detached_directory = race_root / f"detached-{index}"
        hostile = race_root / f"hostile-staged-{index}"
        hostile.mkdir(mode=0o700)
        decision_directory.rename(detached_directory)
        hostile.rename(decision_directory)
        held = os.fstat(descriptor)
        canonical = decision_directory.stat(follow_symlinks=False)
        observations.append(
            (
                (held.st_dev, held.st_ino),
                (canonical.st_dev, canonical.st_ino),
            )
        )
        hostile_installed = True
        target_scan_armed = False

    class RebindingNames(list[str]):
        def __init__(self, names: list[str], descriptor: int) -> None:
            super().__init__(names)
            self.descriptor = descriptor
            self.compared = False

        def __ne__(self, other: object) -> bool:
            if not self.compared:
                self.compared = True
                install_hostile_directory(self.descriptor)
            return super().__ne__(other)

    def racing_tree_entry_names_descriptor(
        state: DurableStateRoot,
        descriptor: int,
        path: Path,
        *,
        deadline_ns: int | None = None,
        maximum_entries: int | None = None,
    ) -> list[str]:
        nonlocal detached_directory, hostile_installed, target_scan_armed
        if path == decision_directory.parent and hostile_installed:
            assert detached_directory is not None
            index = len(observations) - 1
            replacement = race_root / f"replacement-{index}"
            archived_hostile = race_root / f"hostile-{index}"
            shutil.copytree(detached_directory, replacement)
            decision_directory.rename(archived_hostile)
            replacement.rename(decision_directory)
            hostile_installed = False
        names = original_names(
            state,
            descriptor,
            path,
            deadline_ns=deadline_ns,
            maximum_entries=maximum_entries,
        )
        if (
            path == decision_directory
            and not hostile_installed
            and not target_scan_armed
            and len(observations) < 2
        ):
            target_scan_armed = True
            return RebindingNames(names, descriptor)
        return names

    monkeypatch.setattr(
        DurableStateRoot,
        "_tree_entry_names_descriptor",
        racing_tree_entry_names_descriptor,
    )
    return observations


def _assert_detached_decision_directories(
    observations: list[tuple[tuple[int, int], tuple[int, int]]],
    *,
    minimum: int,
) -> None:
    assert len(observations) >= minimum, observations
    assert all(held != canonical for held, canonical in observations), observations


class _TopLevelDecisionNamespaceRebindObservations:
    def __init__(self) -> None:
        self.inode_pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.hook_count = 0
        self.snapshots_equal: list[bool] = []


def _arm_top_level_decision_namespace_rebind_race(
    monkeypatch: pytest.MonkeyPatch,
    decision_namespace: Path,
) -> _TopLevelDecisionNamespaceRebindObservations:
    """Replace the top-level namespace after its final descriptor metadata read."""

    observations = _TopLevelDecisionNamespaceRebindObservations()
    original_names = DurableStateRoot._tree_entry_names_descriptor

    class RebindingNames(list[str]):
        def __init__(self, names: list[str], descriptor: int) -> None:
            super().__init__(names)
            self.descriptor = descriptor
            self.fired = False

        def __bool__(self) -> bool:
            if not self.fired:
                self.fired = True
                observations.hook_count += 1
                detached = decision_namespace.with_name(
                    "semantic-release-decisions-detached"
                )
                replacement = decision_namespace.with_name(
                    "semantic-release-decisions-replacement"
                )
                shutil.copytree(decision_namespace, replacement)
                decision_namespace.rename(detached)
                replacement.rename(decision_namespace)
                held = os.fstat(self.descriptor)
                canonical = decision_namespace.stat(follow_symlinks=False)
                observations.inode_pairs.append(
                    (
                        (held.st_dev, held.st_ino),
                        (canonical.st_dev, canonical.st_ino),
                    )
                )
                observations.snapshots_equal.append(
                    tree_snapshot(detached) == tree_snapshot(decision_namespace)
                )
            return list.__len__(self) != 0

    def racing_tree_entry_names_descriptor(
        state: DurableStateRoot,
        descriptor: int,
        path: Path,
        *,
        deadline_ns: int | None = None,
        maximum_entries: int | None = None,
    ) -> list[str]:
        names = original_names(
            state,
            descriptor,
            path,
            deadline_ns=deadline_ns,
            maximum_entries=maximum_entries,
        )
        if path == decision_namespace and observations.hook_count == 0:
            return RebindingNames(names, descriptor)
        return names

    monkeypatch.setattr(
        DurableStateRoot,
        "_tree_entry_names_descriptor",
        racing_tree_entry_names_descriptor,
    )
    return observations


def _assert_top_level_decision_namespace_rebound(
    observations: _TopLevelDecisionNamespaceRebindObservations,
) -> None:
    assert observations.hook_count == 1
    assert len(observations.inode_pairs) == 1
    assert observations.inode_pairs[0][0] != observations.inode_pairs[0][1]
    assert observations.snapshots_equal == [True]


def _prepare_publication_kind(
    generations: GenerationStore,
    store: SemanticReleaseDecisionStore,
    publication_kind: str,
) -> tuple[SemanticReleaseDecisionBinding, SemanticReleaseDecisionCapture]:
    if publication_kind == "generation":
        other_generation = "gen-other"
        other_request = "2" * 64
        active = generations._generation(REPO_UUID, other_generation)
        generations.state.ensure_directory(active)
        generations.state.install_once_bytes(
            active / "payload.bin",
            b"other retained generation",
            label="test:other-generation",
        )
        generations.state.install_once_bytes(
            generations._lock(REPO_UUID, other_generation),
            b"generation lock\n",
            label="test:other-lock",
        )
        other_binding = _binding_for(other_generation, other_request)
        generations.state.install_once_bytes(
            store._binding_path(REPO_UUID, other_generation, other_request),
            other_binding.canonical,
            label="test:other-decision",
        )
    elif publication_kind == "file":
        prior_request = "2" * 64
        prior = _binding_for(GENERATION_ID, prior_request)
        prior_capture = store.capture(
            REPO_UUID,
            GENERATION_ID,
            prior_request,
            capacity_policy=POLICY,
        )
        store.install(prior_capture, prior, capacity_policy=POLICY)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    return binding, capture


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


def test_no_match_cannot_omit_rationale() -> None:
    invalid = _binding_value(terminal_outcome="ALLOW_WITH_OMISSIONS")
    results = cast(list[dict[str, object]], invalid["field_results"])
    rationale = results[1]
    rationale["classifier_outcome"] = "NO_MATCH"
    rationale["category_ids"] = []
    rationale["rule_ids"] = []
    results[2]["disposition"] = "ALLOW_FIELD"
    counts = cast(dict[str, int], invalid["counts"])
    counts["matched_field_count"] = 1
    invalid["full_result_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {
                "eligible_field_inventory_sha256": invalid[
                    "eligible_field_inventory_sha256"
                ],
                "counts": counts,
                "field_results": results,
                "terminal_outcome": invalid["terminal_outcome"],
            }
        )
    ).hexdigest()

    with pytest.raises(
        SemanticReleaseDecisionInvalid,
        match="NO_MATCH must produce ALLOW_FIELD or REJECT_RELEASE",
    ):
        SemanticReleaseDecisionBinding.from_mapping(invalid)


def test_binding_rejects_double_dot_semantic_entity_id() -> None:
    invalid = _binding_value()
    results = cast(list[dict[str, object]], invalid["field_results"])
    results[0]["entity_id"] = "node..secret"
    results[1]["entity_id"] = "node..secret"
    _refresh_full_result_sha256(invalid)

    with pytest.raises(
        SemanticReleaseDecisionInvalid,
        match="entity_id violates semantic ID grammar",
    ):
        SemanticReleaseDecisionBinding.from_mapping(invalid)


def test_match_requires_private_rule_provenance() -> None:
    invalid = _binding_value()
    results = cast(list[dict[str, object]], invalid["field_results"])
    results[1]["rule_ids"] = []
    _refresh_full_result_sha256(invalid)

    with pytest.raises(
        SemanticReleaseDecisionInvalid,
        match="MATCH must carry at least one rule",
    ):
        SemanticReleaseDecisionBinding.from_mapping(invalid)


def test_indeterminate_cannot_retain_match_provenance() -> None:
    invalid = _binding_value()
    results = cast(list[dict[str, object]], invalid["field_results"])
    results[2]["classifier_outcome"] = "INDETERMINATE"
    counts = cast(dict[str, int], invalid["counts"])
    counts["matched_field_count"] = 1
    _refresh_full_result_sha256(invalid)

    with pytest.raises(
        SemanticReleaseDecisionInvalid,
        match="INDETERMINATE cannot carry category or rule matches",
    ):
        SemanticReleaseDecisionBinding.from_mapping(invalid)


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
            if matching_reads >= 3 and path.parent.exists():
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
    with pytest.raises(CapacityExceeded, match=r"maximum|64"):
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


def test_capacity_scanner_rejects_detached_decision_directory_binding(
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
    observations = _arm_decision_directory_rebind_race(
        monkeypatch,
        _binding_path(harness.state_root).parent,
    )

    failure: CapacityExceeded | None = None
    try:
        generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    except CapacityExceeded as exc:
        failure = exc

    if failure is None:
        _assert_detached_decision_directories(observations, minimum=2)
        pytest.fail(
            "detached decision-directory capacity evidence was accepted; "
            f"observations={observations!r}"
        )
    _assert_detached_decision_directories(observations, minimum=1)
    assert "unsafe state path in capacity scan" in str(failure)
    assert isinstance(failure.__cause__, StatePathError)
    assert "state directory binding changed while held" in str(failure.__cause__)


def test_gc_plan_rejects_detached_decision_directory_binding_as_unsafe(
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
    observations = _arm_decision_directory_rebind_race(
        monkeypatch,
        _binding_path(harness.state_root).parent,
    )

    with pytest.raises(GcError) as failure:
        gc.plan(
            grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=20_001,
        )

    detail = str(failure.value)
    if "blocks GC eligibility before planning" in detail:
        _assert_detached_decision_directories(observations, minimum=2)
    _assert_detached_decision_directories(observations, minimum=1)
    assert "semantic-release decision state is unsafe" in detail, (
        f"{detail}; observations={observations!r}"
    )
    capacity_failure = failure.value.__cause__
    assert isinstance(capacity_failure, CapacityExceeded)
    assert "unsafe semantic-release decision state" in str(capacity_failure)
    path_failure = capacity_failure.__cause__
    assert isinstance(path_failure, StatePathError)
    assert "state directory binding changed while held" in str(path_failure)


def test_capacity_scanner_rejects_rebound_top_level_decision_namespace(
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
    observations = _arm_top_level_decision_namespace_rebind_race(
        monkeypatch,
        _binding_path(harness.state_root).parent.parent,
    )

    failure: CapacityExceeded | None = None
    try:
        generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    except CapacityExceeded as exc:
        failure = exc

    _assert_top_level_decision_namespace_rebound(observations)
    if failure is None:
        pytest.fail(
            "rebound top-level decision namespace was accepted by the capacity scan; "
            f"inode_pairs={observations.inode_pairs!r}"
        )
    assert "unsafe state path in capacity scan" in str(failure)
    path_failure = failure.__cause__
    assert isinstance(path_failure, StatePathError)
    assert "state directory binding changed while held" in str(path_failure)


def test_gc_plan_rejects_rebound_top_level_decision_namespace_as_unsafe(
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
    observations = _arm_top_level_decision_namespace_rebind_race(
        monkeypatch,
        _binding_path(harness.state_root).parent.parent,
    )

    with pytest.raises(GcError) as failure:
        gc.plan(
            grant,
            capacity_policy=POLICY,
            protections=EMPTY_PROTECTION,
            monotonic_ns=20_001,
        )

    _assert_top_level_decision_namespace_rebound(observations)
    detail = str(failure.value)
    assert "blocks GC eligibility before planning" not in detail, (
        f"ordinary GC blockade accepted rebound namespace; "
        f"inode_pairs={observations.inode_pairs!r}"
    )
    assert "semantic-release decision state is unsafe" in detail
    capacity_failure = failure.value.__cause__
    assert isinstance(capacity_failure, CapacityExceeded)
    assert "unsafe semantic-release decision state" in str(capacity_failure)
    path_failure = capacity_failure.__cause__
    assert isinstance(path_failure, StatePathError)
    assert "state directory binding changed while held" in str(path_failure)


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


def test_previsibility_publish_failure_leaves_no_canonical_empty_namespace(
    tmp_path: Path,
) -> None:
    crash = CrashAt("semantic-release-decision:publish:before_rename")
    harness, _generations, store, _gc = _runtime(tmp_path, fault_hook=crash)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    with pytest.raises(InjectedFault):
        store.install(capture, binding, capacity_policy=POLICY)

    assert crash.fired
    assert not _binding_path(harness.state_root).parent.parent.exists()

    installed = store.install(capture, binding, capacity_policy=POLICY)

    assert installed == binding
    assert _binding_path(harness.state_root).read_bytes() == binding.canonical


@pytest.mark.parametrize("publication_kind", ["generation", "file"])
def test_previsibility_failure_retries_without_empty_canonical_boundary(
    tmp_path: Path,
    publication_kind: str,
) -> None:
    harness, generations, initial_store, gc = _runtime(tmp_path)
    if publication_kind == "generation":
        other_generation = "gen-other"
        other_request = "2" * 64
        active = generations._generation(REPO_UUID, other_generation)
        generations.state.ensure_directory(active)
        generations.state.install_once_bytes(
            active / "payload.bin",
            b"other retained generation",
            label="test:other-generation",
        )
        generations.state.install_once_bytes(
            generations._lock(REPO_UUID, other_generation),
            b"generation lock\n",
            label="test:other-lock",
        )
        other_binding = _binding_for(other_generation, other_request)
        other_path = initial_store._binding_path(
            REPO_UUID,
            other_generation,
            other_request,
        )
        generations.state.install_once_bytes(
            other_path,
            other_binding.canonical,
            label="test:other-decision",
        )
    else:
        prior_request = "2" * 64
        prior = _binding_for(GENERATION_ID, prior_request)
        prior_capture = initial_store.capture(
            REPO_UUID,
            GENERATION_ID,
            prior_request,
            capacity_policy=POLICY,
        )
        initial_store.install(prior_capture, prior, capacity_policy=POLICY)

    crash = CrashAt("semantic-release-decision:publish:before_rename")
    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=crash,
    )
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    with pytest.raises(InjectedFault):
        store.install(capture, binding, capacity_policy=POLICY)

    destination = _binding_path(harness.state_root)
    if publication_kind == "generation":
        assert not destination.parent.exists()
    else:
        assert destination.parent.is_dir()
        assert not destination.exists()
        assert not any(path.name.startswith(".") for path in destination.parent.iterdir())

    assert store.install(capture, binding, capacity_policy=POLICY) == binding
    assert destination.read_bytes() == binding.canonical


@pytest.mark.parametrize(
    "event",
    [
        "semantic-release-decision:stage-manifest:created",
        "semantic-release-decision:stage-manifest:written",
        "semantic-release-decision:stage-manifest:durable",
        "semantic-release-decision:stage-manifest:parent_durable",
        "semantic-release-decision:stage-manifest:installed",
        "semantic-release-decision:stage-binding:created",
        "semantic-release-decision:stage-binding:written",
        "semantic-release-decision:stage-binding:durable",
        "semantic-release-decision:stage-binding:parent_durable",
        "semantic-release-decision:stage-binding:installed",
        "semantic-release-decision:stage:binding_contained_durable",
        "semantic-release-decision:stage:generation_directory_durable",
        "semantic-release-decision:stage:payload_directory_durable",
        "semantic-release-decision:stage:build_directory_durable",
        "semantic-release-decision:stage:slot_directory_durable",
        "semantic-release-decision:stage:before_rename",
        "semantic-release-decision:stage:renamed",
        "semantic-release-decision:stage:source_parent_durable",
        "semantic-release-decision:stage:destination_parent_durable",
    ],
)
def test_interrupted_staging_is_noncanonical_and_retryable(
    tmp_path: Path,
    event: str,
) -> None:
    crash = CrashAt(event)
    harness, _generations, store, _gc = _runtime(tmp_path, fault_hook=crash)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )

    with pytest.raises((InjectedFault, SemanticReleaseDecisionConflict)):
        store.install(capture, binding, capacity_policy=POLICY)

    assert crash.fired
    assert not _binding_path(harness.state_root).parent.parent.exists()
    assert store.install(capture, binding, capacity_policy=POLICY) == binding


@pytest.mark.parametrize("hostile_kind", ["extra", "hardlink", "symlink", "mode"])
def test_unsafe_publication_staging_fails_closed(
    tmp_path: Path,
    hostile_kind: str,
) -> None:
    crash = CrashAt("semantic-release-decision:publish:before_rename")
    harness, _generations, store, _gc = _runtime(tmp_path, fault_hook=crash)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    with pytest.raises(InjectedFault):
        store.install(capture, binding, capacity_policy=POLICY)
    ready = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
        / "ready"
    )
    manifest = ready / "manifest.json"
    if hostile_kind == "extra":
        hostile = ready / "extra"
        hostile.write_bytes(b"unexpected")
        hostile.chmod(0o600)
    elif hostile_kind == "hardlink":
        os.link(manifest, ready / "manifest-copy.json")
    elif hostile_kind == "symlink":
        (ready / "payload-link").symlink_to(ready / "payload")
    else:
        manifest.chmod(0o644)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(
        (
            SemanticReleaseDecisionInvalid,
            SemanticReleaseDecisionConflict,
            StateCorrupt,
            StatePathError,
        )
    ):
        store.install(capture, binding, capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before
    assert not _binding_path(harness.state_root).parent.parent.exists()


@pytest.mark.parametrize("missing_kind", ["parent", "file"])
def test_optional_stable_missing_read_checks_deadline_after_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_kind: str,
) -> None:
    _harness, _generations, store, _gc = _runtime(tmp_path)
    relative = Path("workspaces") / REPO_UUID / "missing" / "record.json"
    if missing_kind == "file":
        store.state.ensure_directory(relative.parent)
    observations = iter((1, 3))
    monkeypatch.setattr(
        persistence_module.time,
        "monotonic_ns",
        lambda: next(observations, 3),
    )

    with pytest.raises(LockTimeout):
        store.state._read_optional_existing_stable_bytes(
            relative,
            deadline_ns=2,
        )


def test_decision_capacity_scan_retries_interrupted_binding_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness, generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    store.install(capture, binding, capacity_policy=POLICY)
    original_read = generations_module.os.read
    interrupted = False

    def interrupt_once(descriptor: int, maximum: int) -> bytes:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return original_read(descriptor, maximum)

    monkeypatch.setattr(generations_module.os, "read", interrupt_once)

    usage = generations.decision_capacity_usage_locked(REPO_UUID, POLICY)

    assert interrupted
    assert usage.generation_binding_count(GENERATION_ID) == 1
    assert usage.decision_bytes_by_generation[(REPO_UUID, GENERATION_ID)] == len(
        binding.canonical
    )


def test_reserve_preflight_charges_staging_overhead_without_logical_double_charge(
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
    usage = generations.decision_capacity_usage_locked(REPO_UUID, POLICY)
    reserved_usage = replace(usage, unconsumed_reserved_bytes=20)
    reserve = POLICY.reserve_bytes
    candidate_bytes = len(binding.canonical)
    exact_free = (
        reserve
        + reserved_usage.unconsumed_reserved_bytes
        + candidate_bytes
        + SEMANTIC_RELEASE_DECISION_STAGING_OVERHEAD_BYTES
    )
    disk_usage = generations_module.shutil.disk_usage(harness.state_root)
    monkeypatch.setattr(
        generations_module.shutil,
        "disk_usage",
        lambda _path: type(disk_usage)(disk_usage.total, disk_usage.used, exact_free),
    )
    logical_once = replace(
        POLICY,
        reserve_bytes=reserve,
        workspace_max_bytes=usage.workspace_bytes + candidate_bytes,
        global_max_bytes=usage.global_bytes + candidate_bytes,
    )

    generations.preflight_decision_install_locked(
        REPO_UUID,
        GENERATION_ID,
        candidate_bytes=candidate_bytes,
        additional_bytes=candidate_bytes,
        capacity_policy=logical_once,
        usage=reserved_usage,
    )
    monkeypatch.setattr(
        generations_module.shutil,
        "disk_usage",
        lambda _path: type(disk_usage)(
            disk_usage.total,
            disk_usage.used,
            reserve
            + usage.unconsumed_reserved_bytes
            + candidate_bytes
            + SEMANTIC_RELEASE_DECISION_STAGING_OVERHEAD_BYTES
            - 1,
        ),
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(CapacityExceeded, match="reserve threshold"):
        store.install(
            capture,
            binding,
            capacity_policy=POLICY,
        )

    assert tree_snapshot(harness.state_root) == before
    assert not (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
    ).exists()


@pytest.mark.parametrize("publication_kind", ["root", "generation", "file"])
@pytest.mark.parametrize("race_bytes", ["exact", "different"])
def test_exclusive_publication_never_overwrites_destination_race(
    tmp_path: Path,
    publication_kind: str,
    race_bytes: str,
) -> None:
    harness, generations, initial_store, gc = _runtime(tmp_path)
    binding, capture = _prepare_publication_kind(
        generations,
        initial_store,
        publication_kind,
    )
    conflict_value = _binding_value()
    conflict_value["policy_sha256"] = "7" * 64
    conflict = SemanticReleaseDecisionBinding.from_mapping(conflict_value)
    raced = binding if race_bytes == "exact" else conflict
    destination = _binding_path(harness.state_root)

    def create_destination() -> None:
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.parent.parent.chmod(0o700)
        destination.parent.chmod(0o700)
        destination.write_bytes(raced.canonical)
        destination.chmod(0o600)

    syscalls = DestinationRaceSyscalls(create_destination)
    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        syscalls=syscalls,
    )

    if race_bytes == "exact":
        assert store.install(capture, binding, capacity_policy=POLICY) == binding
    else:
        with pytest.raises(SemanticReleaseDecisionConflict, match="different bytes"):
            store.install(capture, binding, capacity_policy=POLICY)

    assert syscalls.fired
    assert destination.read_bytes() == raced.canonical
    decision_names = {path.name for path in destination.parent.parent.rglob("*")}
    assert not decision_names & {"build", "cleanup", "manifest.json", "payload", "ready"}


@pytest.mark.parametrize("publication_kind", ["root", "generation", "file"])
@pytest.mark.parametrize(
    "event",
    [
        "semantic-release-decision:publish:renamed",
        "semantic-release-decision:publish:source_parent_durable",
        "semantic-release-decision:publish:destination_parent_durable",
    ],
)
def test_post_publication_durability_fault_finishes_parent_sync_before_adoption(
    tmp_path: Path,
    publication_kind: str,
    event: str,
) -> None:
    harness, generations, initial_store, gc = _runtime(tmp_path)
    binding, capture = _prepare_publication_kind(
        generations,
        initial_store,
        publication_kind,
    )
    crash = CrashAt(event)
    syscalls = PostFaultFsyncSyscalls(crash)
    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=crash,
        syscalls=syscalls,
    )

    result = store.install(capture, binding, capacity_policy=POLICY)

    assert crash.fired
    assert result == binding
    assert syscalls.post_fault_fsync_count >= 2
    destination = _binding_path(harness.state_root)
    assert destination.read_bytes() == binding.canonical
    decision_names = {path.name for path in destination.parent.parent.rglob("*")}
    assert not decision_names & {"build", "cleanup", "manifest.json", "payload", "ready"}


def test_post_publication_parent_sync_failure_remains_commit_unknown(
    tmp_path: Path,
) -> None:
    harness, generations, initial_store, gc = _runtime(tmp_path)
    binding, capture = _prepare_publication_kind(
        generations,
        initial_store,
        "root",
    )
    crash = CrashAt("semantic-release-decision:publish:renamed")
    syscalls = PostFaultFsyncSyscalls(crash, fail_after_fault=True)
    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=crash,
        syscalls=syscalls,
    )

    with pytest.raises(
        CommitUnknown,
        match="rename became visible before both directories were durable",
    ):
        store.install(capture, binding, capacity_policy=POLICY)

    assert crash.fired
    assert syscalls.post_fault_fsync_count == 1
    assert _binding_path(harness.state_root).read_bytes() == binding.canonical


@pytest.mark.parametrize("parent_kind", ["source", "destination"])
def test_post_publication_parent_substitution_remains_commit_unknown(
    tmp_path: Path,
    parent_kind: str,
) -> None:
    harness, generations, initial_store, gc = _runtime(tmp_path)
    binding, capture = _prepare_publication_kind(
        generations,
        initial_store,
        "file",
    )
    destination = _binding_path(harness.state_root)
    ready = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
        / "ready"
    )
    selected_parent = ready if parent_kind == "source" else destination.parent
    aside = selected_parent.with_name(f"{selected_parent.name}-original")
    fired = False

    def substitute_parent(event: str) -> None:
        nonlocal fired
        if event != "semantic-release-decision:publish:renamed" or fired:
            return
        fired = True
        selected_parent.rename(aside)
        shutil.copytree(aside, selected_parent)

    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=substitute_parent,
    )

    with pytest.raises(CommitUnknown):
        store.install(capture, binding, capacity_policy=POLICY)

    assert fired
    assert aside.is_dir()
    if parent_kind == "source":
        assert destination.read_bytes() == binding.canonical
    else:
        assert (aside / destination.name).read_bytes() == binding.canonical


@pytest.mark.parametrize(
    "cleanup_fault",
    [
        "transition_before_rename",
        "transition_renamed",
        "transition_source_parent_durable",
        "transition_destination_parent_durable",
        "unlink",
        "rmdir",
        "unlink_parent_fsync",
        "rmdir_parent_fsync",
    ],
)
@pytest.mark.parametrize("publication_kind", ["root", "generation", "file"])
def test_interrupted_cleanup_leaves_bounded_residue_and_retry_succeeds(
    tmp_path: Path,
    cleanup_fault: str,
    publication_kind: str,
) -> None:
    initial_crash = CrashAt("semantic-release-decision:publish:before_rename")
    harness, generations, seed_store, gc = _runtime(tmp_path)
    binding, capture = _prepare_publication_kind(
        generations,
        seed_store,
        publication_kind,
    )
    initial_store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=initial_crash,
    )
    with pytest.raises(InjectedFault):
        initial_store.install(capture, binding, capacity_policy=POLICY)

    if cleanup_fault.startswith("transition_"):
        transition = cleanup_fault.removeprefix("transition_")
        crash = CrashAt(
            f"semantic-release-decision:cleanup-transition:{transition}"
        )
        syscalls = None
        fault_hook = crash
    else:
        crash = None
        syscalls = CleanupFaultSyscalls(cleanup_fault)
        fault_hook = None
    interrupted_store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
        syscalls=syscalls,
    )

    with pytest.raises((InjectedFault, SemanticReleaseDecisionConflict)):
        interrupted_store.install(capture, binding, capacity_policy=POLICY)

    if crash is not None:
        assert crash.fired
    if syscalls is not None:
        assert syscalls.fired
    slot = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
    )
    assert len(list(slot.rglob("*"))) <= 5
    destination = _binding_path(harness.state_root)
    assert not destination.exists()
    if publication_kind == "root":
        assert not destination.parent.parent.exists()
    elif publication_kind == "generation":
        assert not destination.parent.exists()
    else:
        assert any(destination.parent.iterdir())

    retry_store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
    )
    assert retry_store.install(capture, binding, capacity_policy=POLICY) == binding


@pytest.mark.parametrize(
    "manifest_fault",
    ["identity", "digest", "size", "bool_version"],
)
def test_ready_manifest_disagreement_fails_closed(
    tmp_path: Path,
    manifest_fault: str,
) -> None:
    crash = CrashAt("semantic-release-decision:publish:before_rename")
    harness, _generations, store, _gc = _runtime(tmp_path, fault_hook=crash)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    with pytest.raises(InjectedFault):
        store.install(capture, binding, capacity_policy=POLICY)
    manifest_path = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
        / "ready"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_bytes())
    if manifest_fault == "identity":
        manifest["request_sha256"] = "2" * 64
        manifest["destination"] = (
            Path("workspaces")
            / REPO_UUID
            / "semantic-release-decisions"
        ).as_posix()
    elif manifest_fault == "digest":
        manifest["binding_sha256"] = "2" * 64
    elif manifest_fault == "size":
        manifest["binding_bytes"] += 1
    else:
        manifest["format_version"] = True
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    manifest_path.chmod(0o600)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionInvalid):
        store.install(capture, binding, capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before
    assert not _binding_path(harness.state_root).parent.parent.exists()


@pytest.mark.parametrize("cleanup_fault", ["extra", "malformed_with_payload"])
def test_hostile_cleanup_residue_fails_closed(
    tmp_path: Path,
    cleanup_fault: str,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    cleanup = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
        / "cleanup"
    )
    cleanup.mkdir(parents=True, mode=0o700)
    cleanup.parent.chmod(0o700)
    if cleanup_fault == "extra":
        extra = cleanup / "extra"
        extra.write_bytes(b"hostile")
        extra.chmod(0o600)
    else:
        manifest = cleanup / "manifest.json"
        manifest.write_bytes(b"{")
        manifest.chmod(0o600)
        (cleanup / "payload").mkdir(mode=0o700)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionInvalid):
        store.install(capture, binding, capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before


def test_malformed_manifest_only_ready_tomb_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    ready = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
        / "ready"
    )
    ready.mkdir(parents=True, mode=0o700)
    ready.parent.chmod(0o700)
    manifest = ready / "manifest.json"
    manifest.write_bytes(b"{")
    manifest.chmod(0o600)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionInvalid):
        store.install(capture, binding, capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before
    assert manifest.read_bytes() == b"{"
    assert not _binding_path(harness.state_root).parent.parent.exists()


@pytest.mark.parametrize(
    "residue",
    ["build", "ready_with_payload", "ready_tomb", "cleanup"],
)
def test_foreign_repo_staging_manifest_fails_closed_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residue: str,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    foreign_repo_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    foreign_value = _binding_value()
    foreign_value["repo_uuid"] = foreign_repo_uuid
    foreign_binding = SemanticReleaseDecisionBinding.from_mapping(foreign_value)
    manifest, manifest_bytes = store._publication_manifest(foreign_binding, "root")
    slot = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
    )
    state_name = "ready" if residue.startswith("ready") else residue
    state = slot / state_name
    state.mkdir(parents=True, mode=0o700)
    slot.chmod(0o700)
    manifest_path = state / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o600)
    if residue != "ready_tomb":
        staged = (
            state
            / "payload"
            / cast(str, manifest["generation_id"])
            / f"{manifest['request_sha256']}.json"
        )
        staged.parent.mkdir(parents=True, mode=0o700)
        (state / "payload").chmod(0o700)
        staged.write_bytes(foreign_binding.canonical)
        staged.chmod(0o600)
    foreign_destination_reads: list[str] = []
    original_read_binding = store._read_binding

    def record_read_binding(
        repo_uuid: str,
        generation_id: str,
        request_sha256: str,
        *,
        deadline_ns: int | None,
    ) -> SemanticReleaseDecisionBinding | None:
        if repo_uuid == foreign_repo_uuid:
            foreign_destination_reads.append(repo_uuid)
        return original_read_binding(
            repo_uuid,
            generation_id,
            request_sha256,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(store, "_read_binding", record_read_binding)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionInvalid):
        store.install(capture, binding, capacity_policy=POLICY)

    assert foreign_destination_reads == []
    assert tree_snapshot(harness.state_root) == before
    assert not _binding_path(harness.state_root).exists()


def test_published_tomb_binding_length_disagreement_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    harness, generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    generations.state.install_once_bytes(
        store._binding_path(REPO_UUID, GENERATION_ID, REQUEST_SHA256),
        binding.canonical,
        label="test:installed-decision",
    )
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    manifest, _manifest_bytes = store._publication_manifest(binding, "root")
    manifest["binding_bytes"] = cast(int, manifest["binding_bytes"]) - 1
    ready = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
        / "ready"
    )
    ready.mkdir(parents=True, mode=0o700)
    ready.parent.chmod(0o700)
    manifest_path = ready / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    manifest_path.chmod(0o600)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(SemanticReleaseDecisionInvalid):
        store.install(capture, binding, capacity_policy=POLICY)

    assert tree_snapshot(harness.state_root) == before
    assert _binding_path(harness.state_root).read_bytes() == binding.canonical


def test_post_publication_same_workspace_tomb_substitution_fails_without_mutation(
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
    substituted_binding = _binding_for(GENERATION_ID, "2" * 64)
    _substituted_manifest, substituted_manifest_bytes = store._publication_manifest(
        substituted_binding,
        "file",
    )
    original_read_binding = store._read_binding
    substituted_snapshot: list[dict[str, tuple[int, int, int, str | None]]] = []

    def substitute_tomb_after_publication(
        repo_uuid: str,
        generation_id: str,
        request_sha256: str,
        *,
        deadline_ns: int | None,
    ) -> SemanticReleaseDecisionBinding | None:
        destination = _binding_path(harness.state_root)
        manifest_path = (
            harness.state_root
            / "workspaces"
            / REPO_UUID
            / "semantic-release-decision-publication"
            / "ready"
            / "manifest.json"
        )
        if destination.exists() and manifest_path.exists() and not substituted_snapshot:
            manifest_path.write_bytes(substituted_manifest_bytes)
            manifest_path.chmod(0o600)
            substituted_snapshot.append(tree_snapshot(harness.state_root))
        return original_read_binding(
            repo_uuid,
            generation_id,
            request_sha256,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(store, "_read_binding", substitute_tomb_after_publication)

    with pytest.raises(CommitUnknown):
        store.install(capture, binding, capacity_policy=POLICY)

    assert len(substituted_snapshot) == 1
    assert tree_snapshot(harness.state_root) == substituted_snapshot[0]
    assert _binding_path(harness.state_root).read_bytes() == binding.canonical


def test_post_publication_manifest_kind_substitution_fails_without_mutation(
    tmp_path: Path,
) -> None:
    harness, generations, seed_store, gc = _runtime(tmp_path)
    binding = _binding()
    capture = seed_store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    substituted_snapshot: list[dict[str, tuple[int, int, int, str | None]]] = []

    def substitute_manifest(event: str) -> None:
        if event != "semantic-release-decision:installed" or substituted_snapshot:
            return
        manifest_path = (
            harness.state_root
            / "workspaces"
            / REPO_UUID
            / "semantic-release-decision-publication"
            / "ready"
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_bytes())
        manifest["publication_kind"] = "file"
        manifest["destination"] = (
            Path("workspaces")
            / REPO_UUID
            / "semantic-release-decisions"
            / GENERATION_ID
            / f"{REQUEST_SHA256}.json"
        ).as_posix()
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        manifest_path.chmod(0o600)
        substituted_snapshot.append(tree_snapshot(harness.state_root))

    store = SemanticReleaseDecisionStore(
        harness.state_root,
        harness.registry,
        harness.leases,
        generations,
        gc,
        capabilities=SUPPORTED,
        fault_hook=substitute_manifest,
    )

    with pytest.raises(CommitUnknown):
        store.install(capture, binding, capacity_policy=POLICY)

    assert len(substituted_snapshot) == 1
    assert tree_snapshot(harness.state_root) == substituted_snapshot[0]
    assert _binding_path(harness.state_root).read_bytes() == binding.canonical


@pytest.mark.parametrize("residue", ["empty_slot", "empty_build"])
def test_empty_staging_prefix_retries_without_canonical_empty_namespace(
    tmp_path: Path,
    residue: str,
) -> None:
    harness, _generations, store, _gc = _runtime(tmp_path)
    binding = _binding()
    capture = store.capture(
        REPO_UUID,
        GENERATION_ID,
        REQUEST_SHA256,
        capacity_policy=POLICY,
    )
    slot = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "semantic-release-decision-publication"
    )
    slot.mkdir(mode=0o700)
    if residue == "empty_build":
        (slot / "build").mkdir(mode=0o700)
    destination = _binding_path(harness.state_root)

    assert not destination.parent.parent.exists()
    assert store.install(capture, binding, capacity_policy=POLICY) == binding
    assert destination.read_bytes() == binding.canonical


def test_remove_tree_contents_wraps_entry_stat_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    state = DurableStateRoot(
        harness.state_root,
        capabilities=SUPPORTED,
    )
    cleanup_relative = Path("cleanup-race")
    payload_relative = cleanup_relative / "payload"
    state.ensure_directory(cleanup_relative)
    state.create_private_file_bytes(
        payload_relative,
        b"payload",
        label="test-cleanup-race",
    )
    original_stat = persistence_module.os.stat

    def missing_during_sort(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path == "payload" and dir_fd is not None:
            raise FileNotFoundError(path)
        return original_stat(
            path,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    with state.existing_private_directory(cleanup_relative) as descriptor:
        monkeypatch.setattr(persistence_module.os, "stat", missing_during_sort)
        with pytest.raises(
            StatePathError,
            match="state tree entry cannot be inspected safely",
        ):
            state._remove_tree_contents_descriptor(
                descriptor,
                harness.state_root / cleanup_relative,
                allowed_directory_modes=frozenset({0o700}),
                allowed_file_modes=frozenset({0o600}),
            )

    assert (harness.state_root / payload_relative).read_bytes() == b"payload"


def test_remove_tree_contents_rejects_entry_replacement_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    state = DurableStateRoot(
        harness.state_root,
        capabilities=SUPPORTED,
    )
    cleanup_relative = Path("cleanup-replacement")
    payload_relative = cleanup_relative / "payload"
    payload = harness.state_root / payload_relative
    original_payload = payload.with_name("payload-original")
    state.ensure_directory(cleanup_relative)
    state.create_private_file_bytes(
        payload_relative,
        b"original",
        label="test-cleanup-replacement",
    )
    original_stat = persistence_module.os.stat
    observations = 0

    def replace_before_revalidation(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal observations
        if path == "payload" and dir_fd is not None:
            observations += 1
            if observations == 2:
                payload.rename(original_payload)
                payload.write_bytes(b"replacement")
                payload.chmod(0o600)
        return original_stat(
            path,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    with state.existing_private_directory(cleanup_relative) as descriptor:
        monkeypatch.setattr(
            persistence_module.os,
            "stat",
            replace_before_revalidation,
        )
        with pytest.raises(
            StatePathError,
            match="state tree entry changed before cleanup",
        ):
            state._remove_tree_contents_descriptor(
                descriptor,
                harness.state_root / cleanup_relative,
                allowed_directory_modes=frozenset({0o700}),
                allowed_file_modes=frozenset({0o600}),
            )

    assert observations == 2
    assert original_payload.read_bytes() == b"original"
    assert payload.read_bytes() == b"replacement"


def test_exclusive_rename_rejects_source_path_substitution_before_visibility(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    source_relative = Path("rename-test") / "source" / "payload"
    destination_relative = Path("rename-test") / "destination" / "binding"
    source = harness.state_root / source_relative
    destination = harness.state_root / destination_relative
    aside = source.with_name("payload-original")

    def substitute_source(event: str) -> None:
        if event != "test-exclusive:before_rename":
            return
        source.rename(aside)
        source.write_bytes(b"substitute")
        source.chmod(0o600)

    state = DurableStateRoot(
        harness.state_root,
        capabilities=SUPPORTED,
        fault_hook=substitute_source,
    )
    state.ensure_directory(source_relative.parent)
    state.ensure_directory(destination_relative.parent)
    state.create_private_file_bytes(
        source_relative,
        b"original",
        label="test-source",
    )

    with pytest.raises(StatePathError, match="source changed"):
        state.rename_exclusive_contained(
            source_relative,
            destination_relative,
            source_kind="regular",
            label="test-exclusive",
        )

    assert not destination.exists()
    assert source.read_bytes() == b"substitute"
    assert aside.read_bytes() == b"original"


def test_exclusive_rename_surfaces_destination_substitution_after_visibility(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    source_relative = Path("rename-test") / "source" / "payload"
    destination_relative = Path("rename-test") / "destination" / "binding"
    source = harness.state_root / source_relative
    destination = harness.state_root / destination_relative

    def substitute_destination(event: str) -> None:
        if event != "test-exclusive:renamed":
            return
        destination.unlink()
        destination.write_bytes(b"substitute")
        destination.chmod(0o600)

    state = DurableStateRoot(
        harness.state_root,
        capabilities=SUPPORTED,
        fault_hook=substitute_destination,
    )
    state.ensure_directory(source_relative.parent)
    state.ensure_directory(destination_relative.parent)
    state.create_private_file_bytes(
        source_relative,
        b"original",
        label="test-source",
    )

    with pytest.raises(CommitUnknown):
        state.rename_exclusive_contained(
            source_relative,
            destination_relative,
            source_kind="regular",
            label="test-exclusive",
        )

    assert not source.exists()
    assert destination.read_bytes() == b"substitute"


@pytest.mark.parametrize("parent_kind", ["source", "destination"])
@pytest.mark.parametrize("visibility", ["before", "after"])
def test_exclusive_rename_revalidates_parent_path_bindings(
    tmp_path: Path,
    parent_kind: str,
    visibility: str,
) -> None:
    harness = create_harness(tmp_path)
    source_relative = Path("rename-test") / "source" / "payload"
    destination_relative = Path("rename-test") / "destination" / "binding"
    source = harness.state_root / source_relative
    destination = harness.state_root / destination_relative
    selected_parent = source.parent if parent_kind == "source" else destination.parent
    aside = selected_parent.with_name(f"{selected_parent.name}-original")
    event_name = (
        "test-exclusive:before_rename"
        if visibility == "before"
        else "test-exclusive:renamed"
    )

    def substitute_parent(event: str) -> None:
        if event != event_name:
            return
        selected_parent.rename(aside)
        selected_parent.mkdir(mode=0o700)

    state = DurableStateRoot(
        harness.state_root,
        capabilities=SUPPORTED,
        fault_hook=substitute_parent,
    )
    state.ensure_directory(source_relative.parent)
    state.ensure_directory(destination_relative.parent)
    state.create_private_file_bytes(
        source_relative,
        b"original",
        label="test-source",
    )

    expected = StatePathError if visibility == "before" else CommitUnknown
    with pytest.raises(expected):
        state.rename_exclusive_contained(
            source_relative,
            destination_relative,
            source_kind="regular",
            label="test-exclusive",
        )

    if visibility == "before":
        assert not destination.exists()
    elif parent_kind == "destination":
        assert not destination.exists()
        assert (aside / destination.name).read_bytes() == b"original"
    else:
        assert destination.read_bytes() == b"original"
