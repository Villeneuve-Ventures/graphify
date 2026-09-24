"""S3 evidence bridge for stores; source reads remain an S4 adapter concern."""

from __future__ import annotations

from dataclasses import dataclass

from graphify.workspace.adapters.base import SourceObservation as StructuralObservation
from graphify.workspace.contracts import DETECTOR_ID, InputManifest, digest


class ObservationError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceEntry:
    value: dict

    def to_dict(self):
        return self.value


@dataclass(frozen=True)
class SourceObservation:
    """Frozen source coordinates plus S2's validated detector evidence."""

    source_commit: str
    policy_sha256: str
    structural: StructuralObservation

    def __post_init__(self):
        if type(self.structural) is not StructuralObservation:
            raise ObservationError("validated S2 source observation required")
        if self.structural.stable_inventory_passes != 2:
            raise ObservationError("S3 observation requires exactly two agreeing passes")
        digest(self.policy_sha256)
        if len(self.source_commit) != 40 or any(ch not in "0123456789abcdef" for ch in self.source_commit):
            raise ObservationError("canonical source commit required")

    @property
    def initial_detection(self) -> InputManifest:
        return self.structural.initial_detection

    @property
    def consumed_inputs(self) -> InputManifest | None:
        return self.structural.consumed_inputs

    @property
    def inventory_sha256(self) -> str:
        return self.initial_detection.sha256

    @property
    def detector_id(self) -> str:
        return DETECTOR_ID

    @property
    def stable_inventory_passes(self) -> int:
        return self.structural.stable_inventory_passes

    @property
    def entries(self) -> tuple[EvidenceEntry, ...]:
        return tuple(EvidenceEntry(entry) for entry in self.initial_detection.to_dict()["evidence"])
