"""Fail-closed adapter selection for the frozen workspace compatibility tuple."""

from __future__ import annotations

import re

from graphify.workspace.contracts import (
    ADAPTER_CONTRACT_VERSION,
    CANDIDATE_DISTRIBUTION_VERSION,
    ENGINE_BASELINE,
    EXTRACTOR_CACHE_ABI,
    STATE_SCHEMA_VERSION,
)

from .base import (
    AdapterError,
    AdapterIntent,
    AdapterSelection,
    CompatibilityLane,
    CompatibilityTuple,
    EngineAdapter,
    ObservationHook,
    ObservationTimeout,
    ObservationUnavailable,
    ObservationUnstable,
    ObservationUnsupported,
    QueryRejected,
    QueryRequest,
    SourceEntry,
    SourceObservation,
    StructuralBuild,
    UnsupportedCompatibility,
)


SUPPORTED_COMPATIBILITY = CompatibilityTuple(
    distribution="graphifyy",
    distribution_version=CANDIDATE_DISTRIBUTION_VERSION,
    engine_baseline=ENGINE_BASELINE,
    extractor_cache_abi=EXTRACTOR_CACHE_ABI,
    adapter_contract_version=ADAPTER_CONTRACT_VERSION,
    state_schema_version=STATE_SCHEMA_VERSION,
)

_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _future_whole_artifact(value: CompatibilityTuple) -> bool:
    candidate = _version(value.engine_baseline)
    current = _version(ENGINE_BASELINE)
    return bool(
        candidate is not None
        and current is not None
        and candidate > current
        and value.distribution == "graphifyy"
        and value.distribution_version == f"{value.engine_baseline}+workspace.1"
        and value.extractor_cache_abi == f"graphify-{value.engine_baseline}"
        and value.adapter_contract_version == ADAPTER_CONTRACT_VERSION
        and value.state_schema_version == STATE_SCHEMA_VERSION
    )


def select_adapter(
    compatibility: CompatibilityTuple,
    *,
    intent: AdapterIntent,
) -> AdapterSelection:
    """Select only a certified tuple; future whole artifacts remain probe-only."""

    if compatibility == SUPPORTED_COMPATIBILITY:
        from .v0_9_16 import Graphify0916Adapter

        return AdapterSelection(
            compatibility=compatibility,
            intent=intent,
            lane=CompatibilityLane.SUPPORTED,
            promotable=True,
            adapter=Graphify0916Adapter(),
        )
    if _future_whole_artifact(compatibility):
        if intent is not AdapterIntent.PROBE:
            raise UnsupportedCompatibility(
                f"{compatibility.engine_baseline} is a non-promoting compatibility candidate"
            )
        return AdapterSelection(
            compatibility=compatibility,
            intent=intent,
            lane=CompatibilityLane.NON_PROMOTING,
            promotable=False,
            adapter=None,
        )
    raise UnsupportedCompatibility(
        f"unsupported compatibility tuple for {intent.value}: "
        f"{compatibility.canonical.decode('utf-8').strip()}"
    )


__all__ = [
    "AdapterError",
    "AdapterIntent",
    "AdapterSelection",
    "CompatibilityLane",
    "CompatibilityTuple",
    "EngineAdapter",
    "ObservationHook",
    "ObservationTimeout",
    "ObservationUnavailable",
    "ObservationUnstable",
    "ObservationUnsupported",
    "QueryRejected",
    "QueryRequest",
    "SUPPORTED_COMPATIBILITY",
    "SourceEntry",
    "SourceObservation",
    "StructuralBuild",
    "UnsupportedCompatibility",
    "select_adapter",
]
