from __future__ import annotations

from dataclasses import FrozenInstanceError
import errno
import os
from pathlib import Path
from typing import cast

import pytest

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import (
    RUNTIME_AUTHORITY_CONTRACT,
    RUNTIME_AUTHORITY_FILENAME,
    RUNTIME_AUTHORITY_FORMAT_VERSION,
    WorkspaceAuthorityInvalid,
    WorkspaceAuthorityUnsupported,
    WorkspaceRuntime,
    WorkspaceRuntimeAuthority,
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
    load_workspace_runtime_inputs,
)
from graphify.workspace.contracts import CompatibilityManifest, canonical_json_bytes
from graphify.workspace.persistence import RuntimeCapabilities, StatePathError, UnsupportedRuntime
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


def _authority_bytes(*, format_version: int = RUNTIME_AUTHORITY_FORMAT_VERSION) -> bytes:
    if format_version == RUNTIME_AUTHORITY_FORMAT_VERSION:
        return WorkspaceRuntimeAuthority(
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=SEMANTIC_QUEUE_POLICY,
        ).canonical
    return canonical_json_bytes(
        {
            "contract": RUNTIME_AUTHORITY_CONTRACT,
            "format_version": format_version,
            "compatibility_manifest": COMPATIBILITY_MANIFEST.to_dict(),
            "semantic_queue_policy": SEMANTIC_QUEUE_POLICY.to_dict(),
        }
    )


def _write_authority(state_root: Path, payload: bytes | None = None) -> Path:
    state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    authority = state_root / RUNTIME_AUTHORITY_FILENAME
    authority.write_bytes(_authority_bytes() if payload is None else payload)
    authority.chmod(0o600)
    return authority


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


def test_load_workspace_runtime_inputs_reads_versioned_external_authority_without_writes(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state-home"
    state_root = state_home / "graphify"
    _write_authority(state_root)
    before_tree = tree_snapshot(state_home)
    before_metadata = metadata_snapshot(state_home)

    inputs = load_workspace_runtime_inputs(
        environ={"XDG_STATE_HOME": str(state_home)},
        capabilities=SUPPORTED,
    )

    assert inputs is not None
    assert inputs.state_root == state_root.resolve()
    assert inputs.compatibility_manifest == COMPATIBILITY_MANIFEST
    assert inputs.semantic_queue_policy == SEMANTIC_QUEUE_POLICY
    assert inputs.capabilities == SUPPORTED
    assert tree_snapshot(state_home) == before_tree
    assert metadata_snapshot(state_home) == before_metadata


def test_load_workspace_runtime_inputs_uses_home_fallback_without_creation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    inputs = load_workspace_runtime_inputs(
        environ={"HOME": str(home)},
        capabilities=SUPPORTED,
    )

    assert inputs is None
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ELOOP])
def test_load_workspace_runtime_inputs_rejects_uninspectable_state_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    state_home = tmp_path / "operator-secret-state-home"
    state_home.mkdir()
    original_lstat = Path.lstat

    def fail_state_home_lstat(path: Path) -> os.stat_result:
        if path == state_home:
            raise OSError(error_number, "operator-secret-state-home")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", fail_state_home_lstat)
        with pytest.raises(StatePathError):
            load_workspace_runtime_inputs(
                environ={"XDG_STATE_HOME": str(state_home)},
                capabilities=SUPPORTED,
            )

    assert list(state_home.iterdir()) == []


def test_load_workspace_runtime_inputs_rejects_host_without_dir_fd_before_authority_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "state-home"
    state_root = state_home / "graphify"
    _write_authority(state_root)
    unsupported = RuntimeCapabilities(
        system="Windows",
        filesystem="ntfs",
        elevated=False,
        local=True,
    )
    before_tree = tree_snapshot(state_home)
    before_metadata = metadata_snapshot(state_home)

    def unexpected_authority_read(*_args: object, **_kwargs: object) -> bytes | None:
        raise AssertionError("unsupported hosts must not enter POSIX authority inspection")

    monkeypatch.setattr(
        "graphify.workspace.composition.DurableStateRoot.read_optional_bytes_for_inspection",
        unexpected_authority_read,
    )
    monkeypatch.setattr(os, "supports_dir_fd", set())

    with pytest.raises(UnsupportedRuntime):
        load_workspace_runtime_inputs(
            environ={"XDG_STATE_HOME": str(state_home)},
            capabilities=unsupported,
        )

    assert tree_snapshot(state_home) == before_tree
    assert metadata_snapshot(state_home) == before_metadata


def test_load_workspace_runtime_inputs_reads_authority_on_unsupported_posix_runtime(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state-home"
    state_root = state_home / "graphify"
    _write_authority(state_root)
    unsupported = RuntimeCapabilities(
        system="Linux",
        filesystem="ext4",
        elevated=False,
        local=True,
    )
    before_tree = tree_snapshot(state_home)
    before_metadata = metadata_snapshot(state_home)

    inputs = load_workspace_runtime_inputs(
        environ={"XDG_STATE_HOME": str(state_home)},
        capabilities=unsupported,
    )

    assert inputs is not None
    assert inputs.compatibility_manifest == COMPATIBILITY_MANIFEST
    assert inputs.capabilities == unsupported
    assert tree_snapshot(state_home) == before_tree
    assert metadata_snapshot(state_home) == before_metadata


@pytest.mark.parametrize(
    "payload",
    [
        b"{not-json}\n",
        b"[" * 2_000 + b"]" * 2_000,
        canonical_json_bytes(
            {
                "contract": RUNTIME_AUTHORITY_CONTRACT,
                "format_version": RUNTIME_AUTHORITY_FORMAT_VERSION,
                "compatibility_manifest": COMPATIBILITY_MANIFEST.to_dict(),
            }
        ),
    ],
)
def test_load_workspace_runtime_inputs_rejects_malformed_authority_without_writes(
    tmp_path: Path,
    payload: bytes,
) -> None:
    state_home = tmp_path / "state-home"
    _write_authority(state_home / "graphify", payload)
    before_tree = tree_snapshot(state_home)
    before_metadata = metadata_snapshot(state_home)

    with pytest.raises(WorkspaceAuthorityInvalid):
        load_workspace_runtime_inputs(
            environ={"XDG_STATE_HOME": str(state_home)},
            capabilities=SUPPORTED,
        )

    assert tree_snapshot(state_home) == before_tree
    assert metadata_snapshot(state_home) == before_metadata


def test_load_workspace_runtime_inputs_rejects_unsupported_authority_version(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state-home"
    _write_authority(state_home / "graphify", _authority_bytes(format_version=2))

    with pytest.raises(WorkspaceAuthorityUnsupported):
        load_workspace_runtime_inputs(
            environ={"XDG_STATE_HOME": str(state_home)},
            capabilities=SUPPORTED,
        )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode", "oversized"])
def test_load_workspace_runtime_inputs_rejects_unsafe_or_unbounded_authority(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    state_home = tmp_path / "state-home"
    state_root = state_home / "graphify"
    state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    authority = state_root / RUNTIME_AUTHORITY_FILENAME
    if unsafe_kind == "symlink":
        target = tmp_path / "outside-secret"
        target.write_bytes(_authority_bytes())
        authority.symlink_to(target)
    elif unsafe_kind == "mode":
        authority.write_bytes(_authority_bytes())
        authority.chmod(0o644)
    else:
        authority.write_bytes(b"x" * (64 * 1024 + 1))
        authority.chmod(0o600)
    before_tree = tree_snapshot(tmp_path)
    before_metadata = metadata_snapshot(tmp_path)

    with pytest.raises(WorkspaceAuthorityInvalid):
        load_workspace_runtime_inputs(
            environ={"XDG_STATE_HOME": str(state_home)},
            capabilities=SUPPORTED,
        )

    assert tree_snapshot(tmp_path) == before_tree
    assert metadata_snapshot(tmp_path) == before_metadata


@pytest.mark.parametrize(
    "environ",
    [
        {"XDG_STATE_HOME": "relative-state"},
        {"HOME": "relative-home"},
        {},
    ],
)
def test_load_workspace_runtime_inputs_rejects_unsafe_environment_roots(
    environ: dict[str, str],
) -> None:
    with pytest.raises(StatePathError):
        load_workspace_runtime_inputs(environ=environ, capabilities=SUPPORTED)
