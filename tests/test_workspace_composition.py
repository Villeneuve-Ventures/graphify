from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import (
    WorkspaceRuntime,
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
)
from graphify.workspace.contracts import CompatibilityManifest, canonical_json_bytes
from graphify.workspace.persistence import StatePathError
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    SUPPORTED,
    metadata_snapshot,
    tree_snapshot,
)


SEMANTIC_QUEUE_POLICY = SemanticQueuePolicy(
    max_items=8,
    max_bytes=16 * 1024,
    retry_budget=1,
)


def _inputs(state_root: Path) -> WorkspaceRuntimeInputs:
    return WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=SEMANTIC_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )


def _unsupported_manifest() -> CompatibilityManifest:
    value = {
        **COMPATIBILITY_MANIFEST.to_dict(),
        "engine_baseline": "0.9.15",
    }
    return CompatibilityManifest(
        contract=cast(str, CompatibilityManifest.CONTRACT),
        schema_version=1,
        canonical=canonical_json_bytes(value),
    )


def test_workspace_runtime_inputs_are_frozen(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path / "state")

    with pytest.raises(FrozenInstanceError):
        setattr(inputs, "state_root", tmp_path / "replacement")


def test_workspace_runtime_is_frozen(tmp_path: Path) -> None:
    runtime = compose_workspace_runtime(_inputs(tmp_path / "state"))

    with pytest.raises(FrozenInstanceError):
        setattr(runtime, "registry", runtime.registry)


def test_compose_workspace_runtime_wires_one_supported_dependency_graph(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"

    runtime = compose_workspace_runtime(_inputs(state_root))

    assert isinstance(runtime, WorkspaceRuntime)
    assert runtime.leases.registry is runtime.registry
    assert runtime.journal.leases is runtime.leases
    assert runtime.semantic_queue.leases is runtime.leases
    assert runtime.generations.leases is runtime.leases
    assert runtime.generations.journal is runtime.journal
    assert runtime.generations.semantic_queue is runtime.semantic_queue
    assert runtime.pointers.leases is runtime.leases
    assert runtime.pointers.generations is runtime.generations
    assert runtime.pointers.journal is runtime.journal
    assert runtime.gc.leases is runtime.leases
    assert runtime.gc.generations is runtime.generations
    assert runtime.gc.pointers is runtime.pointers
    assert runtime.freshness.registry is runtime.registry
    assert runtime.freshness.pointers is runtime.pointers

    states = (
        runtime.registry.state,
        runtime.leases.state,
        runtime.journal.state,
        runtime.semantic_queue.state,
        runtime.generations.state,
        runtime.pointers.state,
        runtime.gc.state,
    )
    assert {state.root for state in states} == {state_root.resolve()}
    assert {state.capabilities for state in states} == {SUPPORTED}
    assert runtime.generations.compatibility_sha256 == COMPATIBILITY_SHA256
    assert runtime.pointers.compatibility_sha256 == COMPATIBILITY_SHA256
    assert runtime.freshness.compatibility_sha256 == COMPATIBILITY_SHA256


def test_compose_rejects_unsupported_compatibility_before_state_creation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    inputs = WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=_unsupported_manifest(),
        semantic_queue_policy=SEMANTIC_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )

    with pytest.raises(UnsupportedCompatibility, match="unsupported compatibility tuple"):
        compose_workspace_runtime(inputs)

    assert not state_root.exists()


def test_compose_rejects_relative_state_root_without_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative_root = Path("relative-state")

    with pytest.raises(StatePathError, match="absolute"):
        compose_workspace_runtime(_inputs(relative_root))

    assert not relative_root.exists()


def test_compose_performs_no_writes_to_existing_state_tree(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    sentinel = state_root / "sentinel"
    sentinel.write_bytes(b"composition must remain read-only\n")
    sentinel.chmod(0o600)
    before_tree = tree_snapshot(state_root)
    before_metadata = metadata_snapshot(state_root)

    compose_workspace_runtime(_inputs(state_root))

    assert tree_snapshot(state_root) == before_tree
    assert metadata_snapshot(state_root) == before_metadata
