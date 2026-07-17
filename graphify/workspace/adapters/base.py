"""Stable workspace-facing types for versioned Graphify engine adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from graphify.workspace.contracts import CompatibilityManifest, canonical_json_bytes


class AdapterError(RuntimeError):
    """Base class for stable adapter failures."""

    code = "adapter_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class UnsupportedCompatibility(AdapterError):
    code = "unsupported_compatibility"


class ObservationUnstable(AdapterError):
    code = "observation_unstable"


class ObservationUnsupported(AdapterError):
    code = "observation_unsupported"


class ObservationUnavailable(AdapterError):
    code = "observation_unavailable"


class ObservationTimeout(AdapterError):
    code = "observation_timeout"


class RetainedStateInvalid(AdapterError):
    code = "retained_state_invalid"


class QueryRejected(AdapterError):
    code = "query_rejected"


class AdapterIntent(str, Enum):
    EXECUTE = "execute"
    IMPORT = "import"
    QUERY = "query"
    STAGE = "stage"
    PROMOTE = "promote"
    PROBE = "probe"


class CompatibilityLane(str, Enum):
    SUPPORTED = "supported"
    NON_PROMOTING = "non_promoting"


@dataclass(frozen=True)
class CompatibilityTuple:
    distribution: str
    distribution_version: str
    engine_baseline: str
    extractor_cache_abi: str
    adapter_contract_version: int
    state_schema_version: int

    @classmethod
    def from_manifest(cls, manifest: CompatibilityManifest) -> CompatibilityTuple:
        value = manifest.to_dict()
        return cls(
            distribution=str(value["distribution"]),
            distribution_version=str(value["distribution_version"]),
            engine_baseline=str(value["engine_baseline"]),
            extractor_cache_abi=str(value["extractor_cache_abi"]),
            adapter_contract_version=int(value["adapter_contract_version"]),
            state_schema_version=int(value["state_schema_version"]),
        )

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(
            {
                "adapter_contract_version": self.adapter_contract_version,
                "distribution": self.distribution,
                "distribution_version": self.distribution_version,
                "engine_baseline": self.engine_baseline,
                "extractor_cache_abi": self.extractor_cache_abi,
                "state_schema_version": self.state_schema_version,
            }
        )


@dataclass(frozen=True)
class SourceEntry:
    path: str
    file_type: str
    size: int
    sha256: str
    mode: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "file_type": self.file_type,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class SourceObservation:
    source_commit: str
    inventory_sha256: str
    policy_sha256: str
    detector_id: str
    stable_inventory_passes: int
    entries: tuple[SourceEntry, ...]


@dataclass(frozen=True)
class LegacyManifestEntry:
    path: str
    mtime: int | float
    ast_hash: str
    semantic_hash: str


@dataclass(frozen=True)
class RetainedFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class LegacyStateSnapshot:
    source_version: str
    manifest_entries: tuple[LegacyManifestEntry, ...]
    cache_entries: tuple[str, ...]
    artifact_entries: tuple[str, ...]
    files: tuple[RetainedFile, ...]


@dataclass(frozen=True)
class StructuralBuild:
    engine_baseline: str
    node_count: int
    edge_count: int
    detected_code_files: tuple[str, ...]
    omitted_dispatched_files: tuple[str, ...]


@dataclass(frozen=True)
class QueryRequest:
    question: str
    mode: str = "bfs"
    depth: int = 2
    token_budget: int = 2000
    context_filters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.question or self.question.strip() != self.question:
            raise QueryRejected("question must be non-empty and trimmed")
        if self.mode not in {"bfs", "dfs"}:
            raise QueryRejected("mode must be bfs or dfs")
        if self.depth < 0:
            raise QueryRejected("depth must be non-negative")
        if self.token_budget <= 0:
            raise QueryRejected("token_budget must be positive")
        if any(not item or item.strip() != item for item in self.context_filters):
            raise QueryRejected("context filters must be non-empty and trimmed")


ObservationHook = Callable[[str, Mapping[str, object]], None]


class EngineAdapter(Protocol):
    adapter_id: str
    engine_baseline: str
    detector_id: str

    def build_structural(self, source_root: Path, *, output_root: Path) -> StructuralBuild: ...

    def query_structural(self, payload_root: Path, request: QueryRequest) -> str: ...

    def observe(
        self,
        source_root: Path,
        *,
        max_inventory_passes: int = 6,
        deadline_ns: int | None = None,
        hook: ObservationHook | None = None,
    ) -> SourceObservation: ...

    def read_retained_state(
        self,
        retained_root: Path,
        *,
        source_version: str,
    ) -> LegacyStateSnapshot: ...


@dataclass(frozen=True)
class AdapterSelection:
    compatibility: CompatibilityTuple
    intent: AdapterIntent
    lane: CompatibilityLane
    promotable: bool
    adapter: EngineAdapter | None

    def require_adapter(self) -> EngineAdapter:
        if self.adapter is None:
            raise UnsupportedCompatibility(
                f"{self.compatibility.engine_baseline} is confined to the non-promoting lane"
            )
        return self.adapter


JsonMapping = Mapping[str, Any]


__all__ = [
    "AdapterError",
    "AdapterIntent",
    "AdapterSelection",
    "CompatibilityLane",
    "CompatibilityTuple",
    "EngineAdapter",
    "JsonMapping",
    "LegacyManifestEntry",
    "LegacyStateSnapshot",
    "ObservationHook",
    "ObservationTimeout",
    "ObservationUnavailable",
    "ObservationUnstable",
    "ObservationUnsupported",
    "QueryRejected",
    "QueryRequest",
    "RetainedFile",
    "RetainedStateInvalid",
    "SourceEntry",
    "SourceObservation",
    "StructuralBuild",
    "UnsupportedCompatibility",
]
