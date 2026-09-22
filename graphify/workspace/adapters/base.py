"""Engine-neutral S2 adapter protocol. No operational adapter is shipped yet."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Protocol

from graphify.workspace.contracts import (
    CompatibilityManifest, InputManifest, ContractError, _validate_input_extension,
)

_MAX_QUERY_DEPTH = 8
_MAX_QUERY_TOKEN_BUDGET = 32_768
_MAX_QUERY_QUESTION_BYTES = 4_096
_MAX_QUERY_TERM_UNITS = 256
_MAX_QUERY_CONTEXT_FILTERS = 16
_MAX_QUERY_CONTEXT_FILTER_BYTES = 128
_MAX_QUERY_CONTEXT_FILTER_TOTAL_BYTES = 1_024


class UnsupportedCompatibility(ContractError):
    """Exact candidate mismatch or unavailable operational capability."""


class QueryRejected(ContractError):
    """Query request exceeds the bounded adapter contract."""


class AdapterIntent(str, Enum):
    PROBE = "probe"
    EXECUTE = "execute"
    QUERY = "query"
    STAGE = "stage"
    PROMOTE = "promote"


@dataclass(frozen=True)
class CompatibilityTuple:
    """Whole candidate identity, including build and installed package members."""
    manifest: CompatibilityManifest

    def __post_init__(self):
        if type(self.manifest) is not CompatibilityManifest:
            raise UnsupportedCompatibility("validated compatibility manifest required")

    @classmethod
    def from_manifest(cls, manifest):
        return cls(manifest)

    @property
    def canonical(self):
        return self.manifest.canonical


@dataclass(frozen=True)
class SourceObservation:
    """Detection and consumed evidence remain distinct, never relabelled."""
    initial_detection: InputManifest
    consumed_inputs: InputManifest | None
    stable_inventory_passes: int

    def __post_init__(self):
        if (type(self.initial_detection) is not InputManifest
                or (self.consumed_inputs is not None
                    and type(self.consumed_inputs) is not InputManifest)):
            raise ContractError("observation requires validated input manifests")
        if self.initial_detection.to_dict()["phase"] != "detection":
            raise ContractError("initial observation must be detection")
        if type(self.stable_inventory_passes) is not int or not 2 <= self.stable_inventory_passes <= 6:
            raise ContractError("observation requires two to six agreeing passes")
        if self.consumed_inputs is not None:
            if not self.consumed_inputs.complete:
                raise ContractError("final observation must cover complete consumed inputs")
            _validate_input_extension(self.initial_detection, self.consumed_inputs)


@dataclass(frozen=True)
class StructuralBuild:
    input_manifest: InputManifest
    graph_sha256: str
    node_count: int
    edge_count: int

    def __post_init__(self):
        from graphify.workspace.contracts import digest, integer
        if type(self.input_manifest) is not InputManifest:
            raise ContractError("structural build requires a validated input manifest")
        if not self.input_manifest.complete:
            raise ContractError("structural build requires complete input dispositions")
        digest(self.graph_sha256)
        integer(self.node_count)
        integer(self.edge_count)

    @property
    def input_manifest_sha256(self):
        return self.input_manifest.sha256

    @property
    def dispositions(self):
        return tuple(self.input_manifest.to_dict()["outcomes"])


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
            context_filters = tuple(
                islice(raw_filters, _MAX_QUERY_CONTEXT_FILTERS + 1)
            )
        except TypeError:
            raise QueryRejected(
                "context filters must be a sequence of strings"
            ) from None
        if any(not isinstance(item, str) for item in context_filters):
            raise QueryRejected("context filters must be a sequence of strings")
        object.__setattr__(self, "context_filters", context_filters)
        if not isinstance(self.question, str):
            raise QueryRejected("question must be a string")
        if not self.question or self.question.strip() != self.question:
            raise QueryRejected("question must be non-empty and trimmed")
        try:
            question_size = len(self.question.encode("utf-8"))
        except UnicodeEncodeError:
            raise QueryRejected("question must be valid UTF-8") from None
        if question_size > _MAX_QUERY_QUESTION_BYTES:
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
        if not isinstance(self.mode, str):
            raise QueryRejected("mode must be a string")
        if self.mode not in {"bfs", "dfs"}:
            raise QueryRejected("mode must be bfs or dfs")
        if type(self.depth) is not int:
            raise QueryRejected("depth must be an integer")
        if self.depth < 0:
            raise QueryRejected("depth must be non-negative")
        if self.depth > _MAX_QUERY_DEPTH:
            raise QueryRejected(f"depth must not exceed {_MAX_QUERY_DEPTH}")
        if type(self.token_budget) is not int:
            raise QueryRejected("token_budget must be an integer")
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
        try:
            filter_sizes = tuple(len(item.encode("utf-8")) for item in self.context_filters)
        except UnicodeEncodeError:
            raise QueryRejected("context filters must be valid UTF-8") from None
        if any(size > _MAX_QUERY_CONTEXT_FILTER_BYTES for size in filter_sizes):
            raise QueryRejected(
                f"each context filter must not exceed {_MAX_QUERY_CONTEXT_FILTER_BYTES} UTF-8 bytes"
            )
        if sum(filter_sizes) > _MAX_QUERY_CONTEXT_FILTER_TOTAL_BYTES:
            raise QueryRejected(
                "context filters must not exceed "
                f"{_MAX_QUERY_CONTEXT_FILTER_TOTAL_BYTES} aggregate UTF-8 bytes"
            )


class EngineAdapter(Protocol):
    """S4 supplies implementation; paths are explicit, never cwd-derived authority.

    The lifecycle owns pinned staging descriptors and fences. Implementations must
    validate those capabilities before writing, and never publish ordinary output.
    An input manifest is validated evidence, not permission to reopen its labels.
    """
    adapter_id: str
    detector_id: str

    def build_structural(self, source_root: Path, *, payload_fd: int,
                         scratch_fd: int, initial_detection: InputManifest) -> StructuralBuild: ...

    def query_structural(self, payload_fd: int, request: QueryRequest) -> str: ...

    def observe(self, source_root: Path, *, input_manifest: InputManifest | None = None,
                max_inventory_passes: int = 6, deadline_ns: int | None = None) -> SourceObservation: ...


@dataclass(frozen=True)
class AdapterSelection:
    compatibility: CompatibilityTuple
    intent: AdapterIntent
    executable: bool = False
    promotable: bool = False

    def require_adapter(self) -> EngineAdapter:
        raise UnsupportedCompatibility("S2 has no operational adapter; S4 is required")
