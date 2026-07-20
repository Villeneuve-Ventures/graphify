"""Focused production composition for the workspace lifecycle runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graphify.workspace.adapters import AdapterIntent, CompatibilityTuple, select_adapter
from graphify.workspace.contracts import CompatibilityManifest
from graphify.workspace.freshness import FreshnessAuthority
from graphify.workspace.gc import GcStore
from graphify.workspace.generations import GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseStore
from graphify.workspace.persistence import FaultHook, RuntimeCapabilities, Syscalls
from graphify.workspace.pointers import PointerStore
from graphify.workspace.registry import RegistryStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy, SemanticQueueStore


@dataclass(frozen=True)
class WorkspaceRuntimeInputs:
    """Explicit authorities required to compose one workspace runtime."""

    state_root: Path
    compatibility_manifest: CompatibilityManifest
    semantic_queue_policy: SemanticQueuePolicy
    capabilities: RuntimeCapabilities | None = None
    fault_hook: FaultHook | None = None
    syscalls: Syscalls | None = None


@dataclass(frozen=True)
class WorkspaceRuntime:
    """One dependency-consistent workspace runtime rooted outside a checkout."""

    registry: RegistryStore
    leases: LeaseStore
    journal: JournalStore
    semantic_queue: SemanticQueueStore
    generations: GenerationStore
    pointers: PointerStore
    freshness: FreshnessAuthority
    gc: GcStore


def compose_workspace_runtime(inputs: WorkspaceRuntimeInputs) -> WorkspaceRuntime:
    """Validate authorities, then wire the existing stores without state access."""

    compatibility = CompatibilityTuple.from_manifest(inputs.compatibility_manifest)
    select_adapter(compatibility, intent=AdapterIntent.EXECUTE).require_adapter()
    queue_policy = SemanticQueuePolicy.from_mapping(inputs.semantic_queue_policy.to_dict())

    shared = {
        "capabilities": inputs.capabilities,
        "fault_hook": inputs.fault_hook,
        "syscalls": inputs.syscalls,
    }
    registry = RegistryStore(inputs.state_root, **shared)
    leases = LeaseStore(inputs.state_root, registry, **shared)
    journal = JournalStore(inputs.state_root, leases, **shared)
    semantic_queue = SemanticQueueStore(
        inputs.state_root,
        leases,
        policy=queue_policy,
        **shared,
    )
    generations = GenerationStore(
        inputs.state_root,
        leases,
        journal,
        compatibility_manifest=inputs.compatibility_manifest,
        semantic_queue=semantic_queue,
        **shared,
    )
    pointers = PointerStore(
        inputs.state_root,
        leases,
        generations,
        journal,
        compatibility_manifest=inputs.compatibility_manifest,
        **shared,
    )
    freshness = FreshnessAuthority(
        registry,
        pointers,
        compatibility_manifest=inputs.compatibility_manifest,
    )
    gc = GcStore(
        inputs.state_root,
        leases,
        generations,
        pointers,
        **shared,
    )
    return WorkspaceRuntime(
        registry=registry,
        leases=leases,
        journal=journal,
        semantic_queue=semantic_queue,
        generations=generations,
        pointers=pointers,
        freshness=freshness,
        gc=gc,
    )


__all__ = [
    "WorkspaceRuntime",
    "WorkspaceRuntimeInputs",
    "compose_workspace_runtime",
]
