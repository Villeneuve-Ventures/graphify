"""Focused production composition for the workspace lifecycle runtime."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, cast

from graphify.workspace.adapters import AdapterIntent, CompatibilityTuple, select_adapter
from graphify.workspace.contracts import (
    CompatibilityManifest,
    ContractError,
    UnsupportedContractVersion,
    canonical_json_bytes,
)
from graphify.workspace.freshness import FreshnessAuthority
from graphify.workspace.gc import GcStore
from graphify.workspace.generations import GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.leases import LeaseStore
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
    UnsupportedRuntime,
)
from graphify.workspace.pointers import PointerStore
from graphify.workspace.registry import RegistryStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy, SemanticQueueStore

if TYPE_CHECKING:
    from graphify.workspace.semantic_handoff import SemanticResultHandoffStore


RUNTIME_AUTHORITY_CONTRACT = "graphify.workspace.runtime_authority.internal"
RUNTIME_AUTHORITY_FORMAT_VERSION = 1
RUNTIME_AUTHORITY_FILENAME = "runtime-manifest.json"
_RUNTIME_AUTHORITY_MAX_BYTES = 64 * 1024


class WorkspaceAuthorityError(RuntimeError):
    """A production runtime authority cannot be used safely."""

    reason_code = "runtime_authority_invalid"
    action_code = "install_candidate_authority"


class WorkspaceAuthorityInvalid(WorkspaceAuthorityError):
    """The installed runtime authority is malformed or unsafe."""


class WorkspaceAuthorityUnsupported(WorkspaceAuthorityError):
    """The installed runtime authority uses an unsupported version."""

    reason_code = "runtime_authority_unsupported"
    action_code = "install_supported_candidate"


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceAuthorityInvalid("runtime authority contains duplicate keys")
        result[key] = value
    return result


@dataclass(frozen=True)
class WorkspaceRuntimeAuthority:
    """Versioned installed authority for composing the read-only runtime."""

    compatibility_manifest: CompatibilityManifest
    semantic_queue_policy: SemanticQueuePolicy

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": RUNTIME_AUTHORITY_CONTRACT,
            "format_version": RUNTIME_AUTHORITY_FORMAT_VERSION,
            "compatibility_manifest": self.compatibility_manifest.to_dict(),
            "semantic_queue_policy": self.semantic_queue_policy.to_dict(),
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WorkspaceRuntimeAuthority":
        expected = {
            "contract",
            "format_version",
            "compatibility_manifest",
            "semantic_queue_policy",
        }
        if set(value) != expected:
            raise WorkspaceAuthorityInvalid("runtime authority fields are invalid")
        if value.get("contract") != RUNTIME_AUTHORITY_CONTRACT:
            raise WorkspaceAuthorityInvalid("runtime authority contract is invalid")
        version = value.get("format_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise WorkspaceAuthorityInvalid("runtime authority version is invalid")
        if version != RUNTIME_AUTHORITY_FORMAT_VERSION:
            raise WorkspaceAuthorityUnsupported("runtime authority version is unsupported")
        compatibility = value.get("compatibility_manifest")
        queue_policy = value.get("semantic_queue_policy")
        if not isinstance(compatibility, Mapping) or not isinstance(queue_policy, Mapping):
            raise WorkspaceAuthorityInvalid("runtime authority payload is invalid")
        try:
            compatibility_manifest = cast(
                CompatibilityManifest,
                CompatibilityManifest.from_mapping(compatibility),
            )
            semantic_queue_policy = SemanticQueuePolicy.from_mapping(queue_policy)
        except UnsupportedContractVersion as exc:
            raise WorkspaceAuthorityUnsupported(
                "runtime authority contains an unsupported contract version"
            ) from exc
        except (ContractError, TypeError, ValueError) as exc:
            raise WorkspaceAuthorityInvalid("runtime authority payload is invalid") from exc
        return cls(
            compatibility_manifest=compatibility_manifest,
            semantic_queue_policy=semantic_queue_policy,
        )

    @classmethod
    def from_json(cls, value: bytes) -> "WorkspaceRuntimeAuthority":
        try:
            parsed = json.loads(value, object_pairs_hook=_reject_duplicate_pairs)
        except WorkspaceAuthorityError:
            raise
        except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
            raise WorkspaceAuthorityInvalid("runtime authority is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise WorkspaceAuthorityInvalid("runtime authority must be an object")
        authority = cls.from_mapping(parsed)
        if authority.canonical != value:
            raise WorkspaceAuthorityInvalid("runtime authority is not canonical")
        return authority


def _workspace_state_root(environ: Mapping[str, str]) -> Path:
    state_home_value = environ.get("XDG_STATE_HOME")
    if state_home_value:
        state_home = Path(state_home_value)
    else:
        home_value = environ.get("HOME")
        if not home_value:
            raise StatePathError("HOME is required when XDG_STATE_HOME is not set")
        state_home = Path(home_value) / ".local" / "state"
    if not state_home.is_absolute():
        raise StatePathError("workspace state home must be an absolute path")
    return Path(os.path.abspath(state_home / "graphify"))


def load_workspace_runtime_inputs(
    *,
    environ: Mapping[str, str] | None = None,
    capabilities: RuntimeCapabilities | None = None,
    fault_hook: FaultHook | None = None,
    syscalls: Syscalls | None = None,
) -> WorkspaceRuntimeInputs | None:
    """Load installed composition authorities without creating or repairing state."""

    state_root = _workspace_state_root(os.environ if environ is None else environ)
    if not {os.open, os.stat}.issubset(os.supports_dir_fd):
        raise UnsupportedRuntime(
            "workspace inspection requires descriptor-relative file access"
        )
    try:
        payload = DurableStateRoot.read_optional_bytes_for_inspection(
            state_root,
            RUNTIME_AUTHORITY_FILENAME,
            max_bytes=_RUNTIME_AUTHORITY_MAX_BYTES,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
    except StateCorrupt as exc:
        raise WorkspaceAuthorityInvalid("runtime authority cannot be read safely") from exc
    if payload is None:
        return None
    resolved_capabilities = capabilities or RuntimeCapabilities.detect(state_root)
    authority = WorkspaceRuntimeAuthority.from_json(payload)
    return WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=authority.compatibility_manifest,
        semantic_queue_policy=authority.semantic_queue_policy,
        capabilities=resolved_capabilities,
        fault_hook=fault_hook,
        syscalls=syscalls,
    )


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
    semantic_handoffs: SemanticResultHandoffStore | None = None


def compose_workspace_runtime(inputs: WorkspaceRuntimeInputs) -> WorkspaceRuntime:
    """Validate authorities, then wire the existing stores without state access."""

    from graphify.workspace.semantic_handoff import SemanticResultHandoffStore

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
    semantic_handoffs = SemanticResultHandoffStore(
        inputs.state_root,
        leases,
        generations,
        semantic_queue,
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
        semantic_handoffs=semantic_handoffs,
    )


__all__ = [
    "RUNTIME_AUTHORITY_CONTRACT",
    "RUNTIME_AUTHORITY_FILENAME",
    "RUNTIME_AUTHORITY_FORMAT_VERSION",
    "WorkspaceAuthorityError",
    "WorkspaceAuthorityInvalid",
    "WorkspaceAuthorityUnsupported",
    "WorkspaceRuntime",
    "WorkspaceRuntimeAuthority",
    "WorkspaceRuntimeInputs",
    "compose_workspace_runtime",
    "load_workspace_runtime_inputs",
]
