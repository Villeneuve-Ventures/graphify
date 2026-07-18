"""Stable workspace-facing types for versioned Graphify engine adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from graphify.workspace.contracts import CompatibilityManifest, canonical_json_bytes


_MAX_QUERY_DEPTH = 8
_MAX_QUERY_TOKEN_BUDGET = 32_768
_MAX_QUERY_QUESTION_BYTES = 4_096
_MAX_QUERY_TERM_UNITS = 256
_MAX_QUERY_CONTEXT_FILTERS = 16
_MAX_QUERY_CONTEXT_FILTER_BYTES = 128
_MAX_QUERY_CONTEXT_FILTER_TOTAL_BYTES = 1_024


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


class QueryRejected(AdapterError):
    code = "query_rejected"


class AdapterIntent(str, Enum):
    EXECUTE = "execute"
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
        raw_filters = self.context_filters
        if isinstance(raw_filters, (str, bytes)):
            raise QueryRejected("context filters must be a sequence of strings")
        try:
            context_filters = tuple(raw_filters)
        except TypeError:
            raise QueryRejected(
                "context filters must be a sequence of strings"
            ) from None
        if any(not isinstance(item, str) for item in context_filters):
            raise QueryRejected("context filters must be a sequence of strings")
        object.__setattr__(self, "context_filters", context_filters)
        if not self.question or self.question.strip() != self.question:
            raise QueryRejected("question must be non-empty and trimmed")
        if len(self.question.encode("utf-8")) > _MAX_QUERY_QUESTION_BYTES:
            raise QueryRejected(
                f"question must not exceed {_MAX_QUERY_QUESTION_BYTES} UTF-8 bytes"
            )
        # Every engine-emitted query term consumes at least one non-space code
        # point, so this bounds segmented and non-segmented term work without
        # importing version-private tokenizer logic into the stable protocol.
        term_units = sum(len(part) for part in self.question.split())
        if term_units > _MAX_QUERY_TERM_UNITS:
            raise QueryRejected(
                f"question must not exceed {_MAX_QUERY_TERM_UNITS} non-space term units"
            )
        if self.mode not in {"bfs", "dfs"}:
            raise QueryRejected("mode must be bfs or dfs")
        if self.depth < 0:
            raise QueryRejected("depth must be non-negative")
        if self.depth > _MAX_QUERY_DEPTH:
            raise QueryRejected(f"depth must not exceed {_MAX_QUERY_DEPTH}")
        if self.token_budget <= 0:
            raise QueryRejected("token_budget must be positive")
        if self.token_budget > _MAX_QUERY_TOKEN_BUDGET:
            raise QueryRejected(f"token_budget must not exceed {_MAX_QUERY_TOKEN_BUDGET}")
        if any(not item or item.strip() != item for item in self.context_filters):
            raise QueryRejected("context filters must be non-empty and trimmed")
        if len(self.context_filters) > _MAX_QUERY_CONTEXT_FILTERS:
            raise QueryRejected(
                f"context filters must not exceed {_MAX_QUERY_CONTEXT_FILTERS} entries"
            )
        filter_sizes = tuple(len(item.encode("utf-8")) for item in self.context_filters)
        if any(size > _MAX_QUERY_CONTEXT_FILTER_BYTES for size in filter_sizes):
            raise QueryRejected(
                f"each context filter must not exceed {_MAX_QUERY_CONTEXT_FILTER_BYTES} UTF-8 bytes"
            )
        if sum(filter_sizes) > _MAX_QUERY_CONTEXT_FILTER_TOTAL_BYTES:
            raise QueryRejected(
                "context filters must not exceed "
                f"{_MAX_QUERY_CONTEXT_FILTER_TOTAL_BYTES} aggregate UTF-8 bytes"
            )


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
    "ObservationHook",
    "ObservationTimeout",
    "ObservationUnavailable",
    "ObservationUnstable",
    "ObservationUnsupported",
    "QueryRejected",
    "QueryRequest",
    "SourceEntry",
    "SourceObservation",
    "StructuralBuild",
    "UnsupportedCompatibility",
]
