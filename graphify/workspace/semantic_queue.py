"""Durable semantic work reconciliation, fenced claims, and certification views."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterator, Mapping, Sequence, cast
import unicodedata

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    ContractError,
    WorkspaceConfig,
    canonical_json_bytes,
)
from graphify.workspace.identity import (
    IdentityError,
    read_workspace_config_with_digest,
    source_root_identity,
    verify_source_checkout,
)
from graphify.workspace.leases import (
    LeaseExpired,
    LeaseGrant,
    LeaseOperation,
    LeaseOwner,
    LeaseStore,
    StaleLease,
)
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
)


_QUEUE_OPERATIONS = frozenset({"DELETE", "UPSERT"})
_QUEUE_STATUSES = frozenset({"claimed", "completed", "dead_letter", "pending"})
_MUTATING_OPERATIONS = frozenset({"BUILD", "MIGRATE", "REPAIR"})
_COMPACTION_OPERATIONS = _MUTATING_OPERATIONS | frozenset({"SEMANTIC_CLAIM"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$", re.ASCII)
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$", re.ASCII)


class SemanticQueueError(RuntimeError):
    """Base class for stable semantic queue failures."""

    code = "semantic_queue_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class SemanticQueueConflict(SemanticQueueError):
    code = "semantic_queue_conflict"


class SemanticQueueCapacityExceeded(SemanticQueueError):
    code = "semantic_queue_capacity_exceeded"


class SemanticQueueCorrupt(SemanticQueueError):
    code = "semantic_queue_corrupt"


class StaleSemanticClaim(SemanticQueueError):
    code = "stale_semantic_claim"


class SemanticCapabilityUnavailable(SemanticQueueError):
    code = "semantic_capability_unavailable"


class SemanticCertificationBlocked(SemanticQueueError):
    code = "semantic_certification_blocked"


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path}: expected object")
    return cast(Mapping[str, object], value)


def _exact_keys(value: Mapping[str, object], path: str, expected: set[str]) -> None:
    keys = set(value)
    missing = expected - keys
    extra = keys - expected
    if missing:
        raise ContractError(f"{path}: missing field {sorted(missing)[0]!r}")
    if extra:
        raise ContractError(f"{path}: unexpected field {sorted(extra)[0]!r}")


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{path}: expected integer >= {minimum}")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path}: expected string")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{path}: expected boolean")
    return value


def _digest(value: object, path: str) -> str:
    text = _string(value, path)
    if _DIGEST_RE.fullmatch(text) is None:
        raise ContractError(f"{path}: expected lowercase SHA-256 digest")
    return text


def _operation(value: object, path: str) -> str:
    text = _string(value, path)
    if text not in _QUEUE_OPERATIONS:
        raise ContractError(f"{path}: unsupported semantic queue operation")
    return text


def _relative_path(value: object, path: str) -> str:
    text = _string(value, path)
    normalized = unicodedata.normalize("NFC", text)
    pure = PurePosixPath(normalized)
    if (
        text != normalized
        or not text
        or "\x00" in text
        or "\\" in text
        or pure.is_absolute()
        or not pure.parts
        or "." in pure.parts
        or ".." in pure.parts
        or pure.as_posix() != text
    ):
        raise ContractError(f"{path}: expected canonical contained relative path")
    return text


def _optional_text(value: object, path: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = _string(value, path)
    if not text or len(text.encode("utf-8")) > maximum:
        raise ContractError(f"{path}: invalid bounded text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise ContractError(f"{path}: control characters are forbidden")
    return text


@dataclass(frozen=True)
class SemanticQueuePolicy:
    """Required queue bounds with no implicit provider or capacity defaults."""

    max_items: int
    max_bytes: int
    retry_budget: int

    def __post_init__(self) -> None:
        _integer(self.max_items, "$.max_items", minimum=1)
        _integer(self.max_bytes, "$.max_bytes", minimum=1)
        _integer(self.retry_budget, "$.retry_budget")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": "graphify.workspace.semantic_queue_policy.internal",
            "format_version": 1,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "retry_budget": self.retry_budget,
        }

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticQueuePolicy":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "contract",
                "format_version",
                "max_items",
                "max_bytes",
                "retry_budget",
            },
        )
        if data["contract"] != "graphify.workspace.semantic_queue_policy.internal":
            raise ContractError("$.contract: unsupported semantic queue policy")
        if _integer(data["format_version"], "$.format_version", minimum=1) != 1:
            raise ContractError("$.format_version: unsupported semantic queue policy version")
        return cls(
            max_items=_integer(data["max_items"], "$.max_items", minimum=1),
            max_bytes=_integer(data["max_bytes"], "$.max_bytes", minimum=1),
            retry_budget=_integer(data["retry_budget"], "$.retry_budget"),
        )


@dataclass(frozen=True)
class SemanticDesiredWork:
    """One immutable desired semantic result identity."""

    source_epoch: int
    policy_sha256: str
    operation: str
    path: str
    content_sha256: str
    desired_revision: int

    def validated(self) -> "SemanticDesiredWork":
        return SemanticDesiredWork.from_mapping(self.to_dict())

    @property
    def coalescing_key(self) -> tuple[int, str, str, str]:
        return (self.source_epoch, self.policy_sha256, self.operation, self.path)

    @property
    def identity(self) -> tuple[int, str, str, str, str, int]:
        return (
            self.source_epoch,
            self.policy_sha256,
            self.operation,
            self.path,
            self.content_sha256,
            self.desired_revision,
        )

    @property
    def sort_key(self) -> tuple[int, str, str, str, int, str]:
        return (
            self.source_epoch,
            self.policy_sha256,
            self.operation,
            self.path,
            self.desired_revision,
            self.content_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_epoch": self.source_epoch,
            "policy_sha256": self.policy_sha256,
            "operation": self.operation,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "desired_revision": self.desired_revision,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticDesiredWork":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "source_epoch",
                "policy_sha256",
                "operation",
                "path",
                "content_sha256",
                "desired_revision",
            },
        )
        return cls(
            source_epoch=_integer(data["source_epoch"], "$.source_epoch", minimum=1),
            policy_sha256=_digest(data["policy_sha256"], "$.policy_sha256"),
            operation=_operation(data["operation"], "$.operation"),
            path=_relative_path(data["path"], "$.path"),
            content_sha256=_digest(data["content_sha256"], "$.content_sha256"),
            desired_revision=_integer(
                data["desired_revision"],
                "$.desired_revision",
                minimum=1,
            ),
        )


@dataclass(frozen=True)
class SemanticClaim:
    """Exact desired revision and semantic lease fence accepted by one worker."""

    work: SemanticDesiredWork
    claim_id: str
    fence_token: int
    operation_epoch: int
    migration_epoch: int
    active_source_revision: int
    attempt: int
    owner: Mapping[str, object]
    checkpoint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "work": self.work.to_dict(),
            "claim_id": self.claim_id,
            "fence_token": self.fence_token,
            "operation_epoch": self.operation_epoch,
            "migration_epoch": self.migration_epoch,
            "active_source_revision": self.active_source_revision,
            "attempt": self.attempt,
            "owner": dict(self.owner),
            "checkpoint": self.checkpoint,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticClaim":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "work",
                "claim_id",
                "fence_token",
                "operation_epoch",
                "migration_epoch",
                "active_source_revision",
                "attempt",
                "owner",
                "checkpoint",
            },
        )
        claim_id = _digest(data["claim_id"], "$.claim_id")
        owner = LeaseOwner.from_mapping(
            cast(dict[str, Any], dict(_mapping(data["owner"], "$.owner")))
        ).to_dict()
        return cls(
            work=SemanticDesiredWork.from_mapping(
                cast(Mapping[str, object], _mapping(data["work"], "$.work"))
            ),
            claim_id=claim_id,
            fence_token=_integer(data["fence_token"], "$.fence_token", minimum=1),
            operation_epoch=_integer(
                data["operation_epoch"],
                "$.operation_epoch",
                minimum=1,
            ),
            migration_epoch=_integer(data["migration_epoch"], "$.migration_epoch"),
            active_source_revision=_integer(
                data["active_source_revision"],
                "$.active_source_revision",
                minimum=1,
            ),
            attempt=_integer(data["attempt"], "$.attempt", minimum=1),
            owner=owner,
            checkpoint=_optional_text(data["checkpoint"], "$.checkpoint", maximum=256),
        )


def _semantic_claim_id(repo_uuid: str, body: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                **body,
                "repo_uuid": repo_uuid,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class SemanticQueueItem:
    work: SemanticDesiredWork
    status: str
    failure_count: int
    last_error: str | None
    claim: SemanticClaim | None

    @property
    def source_epoch(self) -> int:
        return self.work.source_epoch

    @property
    def policy_sha256(self) -> str:
        return self.work.policy_sha256

    @property
    def operation(self) -> str:
        return self.work.operation

    @property
    def path(self) -> str:
        return self.work.path

    @property
    def content_sha256(self) -> str:
        return self.work.content_sha256

    @property
    def desired_revision(self) -> int:
        return self.work.desired_revision

    def to_dict(self) -> dict[str, object]:
        return {
            "work": self.work.to_dict(),
            "status": self.status,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "claim": None if self.claim is None else self.claim.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticQueueItem":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {"work", "status", "failure_count", "last_error", "claim"},
        )
        work = SemanticDesiredWork.from_mapping(
            cast(Mapping[str, object], _mapping(data["work"], "$.work"))
        )
        status = _string(data["status"], "$.status")
        if status not in _QUEUE_STATUSES:
            raise ContractError("$.status: unsupported semantic queue status")
        failure_count = _integer(data["failure_count"], "$.failure_count")
        last_error = _optional_text(data["last_error"], "$.last_error", maximum=128)
        if last_error is not None and _ERROR_RE.fullmatch(last_error) is None:
            raise ContractError("$.last_error: invalid stable error classification")
        claim_value = data["claim"]
        claim = (
            None
            if claim_value is None
            else SemanticClaim.from_mapping(
                cast(Mapping[str, object], _mapping(claim_value, "$.claim"))
            )
        )
        if (status == "claimed") != (claim is not None):
            raise ContractError("$.claim: only claimed items may carry claim state")
        if claim is not None and claim.work != work:
            raise ContractError("$.claim.work: must bind the exact desired work")
        if status == "dead_letter" and last_error is None:
            raise ContractError("$.last_error: dead-letter work requires an error")
        return cls(
            work=work,
            status=status,
            failure_count=failure_count,
            last_error=last_error,
            claim=claim,
        )


@dataclass(frozen=True)
class _SemanticSourceObservation:
    source_commit: str
    inventory_sha256: str
    policy_sha256: str
    detector_id: str
    stable_inventory_passes: int
    entries_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source_commit": self.source_commit,
            "inventory_sha256": self.inventory_sha256,
            "policy_sha256": self.policy_sha256,
            "detector_id": self.detector_id,
            "stable_inventory_passes": self.stable_inventory_passes,
            "entries_sha256": self.entries_sha256,
        }

    @classmethod
    def from_observation(cls, value: SourceObservation) -> "_SemanticSourceObservation":
        return cls.from_mapping(
            {
                "source_commit": value.source_commit,
                "inventory_sha256": value.inventory_sha256,
                "policy_sha256": value.policy_sha256,
                "detector_id": value.detector_id,
                "stable_inventory_passes": value.stable_inventory_passes,
                "entries_sha256": hashlib.sha256(
                    canonical_json_bytes([entry.to_dict() for entry in value.entries])
                ).hexdigest(),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "_SemanticSourceObservation":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "source_commit",
                "inventory_sha256",
                "policy_sha256",
                "detector_id",
                "stable_inventory_passes",
                "entries_sha256",
            },
        )
        source_commit = _string(data["source_commit"], "$.source_commit")
        if _COMMIT_RE.fullmatch(source_commit) is None:
            raise ContractError("$.source_commit: expected lowercase Git commit")
        detector_id = _optional_text(data["detector_id"], "$.detector_id", maximum=128)
        if detector_id is None:
            raise ContractError("$.detector_id: expected non-empty string")
        stable_inventory_passes = _integer(
            data["stable_inventory_passes"],
            "$.stable_inventory_passes",
            minimum=1,
        )
        if stable_inventory_passes != 2:
            raise ContractError("$.stable_inventory_passes: exactly two are required")
        return cls(
            source_commit=source_commit,
            inventory_sha256=_digest(data["inventory_sha256"], "$.inventory_sha256"),
            policy_sha256=_digest(data["policy_sha256"], "$.policy_sha256"),
            detector_id=detector_id,
            stable_inventory_passes=stable_inventory_passes,
            entries_sha256=_digest(data["entries_sha256"], "$.entries_sha256"),
        )


@dataclass(frozen=True)
class _SemanticObservationEvidence:
    observations: tuple[_SemanticSourceObservation, _SemanticSourceObservation]
    evidence_sha256: str

    @property
    def source_commit(self) -> str:
        return self.observations[0].source_commit

    @property
    def inventory_sha256(self) -> str:
        return self.observations[0].inventory_sha256

    @property
    def policy_sha256(self) -> str:
        return self.observations[0].policy_sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def create(
        cls,
        source_observations: Sequence[SourceObservation],
    ) -> "_SemanticObservationEvidence":
        if len(source_observations) != 2:
            raise ContractError("$.source_observations: exactly two observations are required")
        observations = tuple(
            _SemanticSourceObservation.from_observation(item) for item in source_observations
        )
        return cls.from_mapping(
            {
                "observations": [item.to_dict() for item in observations],
                "evidence_sha256": hashlib.sha256(
                    canonical_json_bytes([item.to_dict() for item in observations])
                ).hexdigest(),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "_SemanticObservationEvidence":
        data = _mapping(value, "$")
        _exact_keys(data, "$", {"observations", "evidence_sha256"})
        raw = data["observations"]
        if not isinstance(raw, list) or len(raw) != 2:
            raise ContractError("$.observations: exactly two observations are required")
        parsed = tuple(
            _SemanticSourceObservation.from_mapping(
                cast(Mapping[str, object], _mapping(item, f"$.observations[{index}]"))
            )
            for index, item in enumerate(raw)
        )
        first, second = parsed
        if first != second:
            raise ContractError("$.observations: repeated source observations differ")
        expected = hashlib.sha256(
            canonical_json_bytes([item.to_dict() for item in parsed])
        ).hexdigest()
        digest = _digest(data["evidence_sha256"], "$.evidence_sha256")
        if digest != expected:
            raise ContractError("$.evidence_sha256: does not bind source observations")
        return cls(
            observations=cast(
                tuple[_SemanticSourceObservation, _SemanticSourceObservation],
                parsed,
            ),
            evidence_sha256=digest,
        )


@dataclass(frozen=True)
class _SemanticReconciliation:
    source_epoch: int
    policy_sha256: str
    source_observations: _SemanticObservationEvidence
    desired_watermark: int
    semantic_required: bool
    desired: tuple[SemanticDesiredWork, ...]
    desired_set_sha256: str
    sealed_input_manifest_sha256: str | None

    @property
    def observation_manifest_sha256(self) -> str:
        return self.source_observations.inventory_sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "source_epoch": self.source_epoch,
            "policy_sha256": self.policy_sha256,
            "source_observations": self.source_observations.to_dict(),
            "desired_watermark": self.desired_watermark,
            "semantic_required": self.semantic_required,
            "desired": [work.to_dict() for work in self.desired],
            "desired_set_sha256": self.desired_set_sha256,
            "sealed_input_manifest_sha256": self.sealed_input_manifest_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        source_epoch: int,
        policy_sha256: str,
        source_observations: Sequence[SourceObservation],
        desired_watermark: int,
        semantic_required: bool,
        desired: Sequence[SemanticDesiredWork],
    ) -> "_SemanticReconciliation":
        ordered = tuple(
            sorted((work.validated() for work in desired), key=lambda work: work.sort_key)
        )
        desired_set_sha256 = hashlib.sha256(
            canonical_json_bytes([work.to_dict() for work in ordered])
        ).hexdigest()
        return cls.from_mapping(
            {
                "source_epoch": source_epoch,
                "policy_sha256": policy_sha256,
                "source_observations": _SemanticObservationEvidence.create(
                    source_observations
                ).to_dict(),
                "desired_watermark": desired_watermark,
                "semantic_required": semantic_required,
                "desired": [work.to_dict() for work in ordered],
                "desired_set_sha256": desired_set_sha256,
                "sealed_input_manifest_sha256": None,
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "_SemanticReconciliation":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "source_epoch",
                "policy_sha256",
                "source_observations",
                "desired_watermark",
                "semantic_required",
                "desired",
                "desired_set_sha256",
                "sealed_input_manifest_sha256",
            },
        )
        source_epoch = _integer(data["source_epoch"], "$.source_epoch", minimum=1)
        policy_sha256 = _digest(data["policy_sha256"], "$.policy_sha256")
        source_observations = _SemanticObservationEvidence.from_mapping(
            cast(
                Mapping[str, object],
                _mapping(data["source_observations"], "$.source_observations"),
            )
        )
        if source_observations.policy_sha256 != policy_sha256:
            raise ContractError("$.source_observations: policy hash differs")
        desired_watermark = _integer(
            data["desired_watermark"],
            "$.desired_watermark",
            minimum=1,
        )
        semantic_required = _boolean(data["semantic_required"], "$.semantic_required")
        raw_desired = data["desired"]
        if not isinstance(raw_desired, list):
            raise ContractError("$.desired: expected array")
        desired = tuple(
            SemanticDesiredWork.from_mapping(
                cast(Mapping[str, object], _mapping(item, f"$.desired[{index}]"))
            )
            for index, item in enumerate(raw_desired)
        )
        if desired != tuple(sorted(desired, key=lambda work: work.sort_key)):
            raise ContractError("$.desired: work must be deterministically sorted")
        keys = [work.coalescing_key for work in desired]
        if len(set(keys)) != len(keys):
            raise ContractError("$.desired: coalescing keys must be unique")
        for index, work in enumerate(desired):
            if work.source_epoch != source_epoch or work.policy_sha256 != policy_sha256:
                raise ContractError(
                    f"$.desired[{index}]: source epoch and policy must match reconciliation"
                )
            if work.desired_revision > desired_watermark:
                raise ContractError(
                    f"$.desired[{index}].desired_revision: exceeds desired watermark"
                )
        if semantic_required and not desired:
            raise ContractError(
                "$.desired: semantic-required reconciliation must contain desired work"
            )
        if not semantic_required and desired:
            raise ContractError("$.desired: semantic-not-required reconciliation must be empty")
        expected_digest = hashlib.sha256(
            canonical_json_bytes([work.to_dict() for work in desired])
        ).hexdigest()
        digest = _digest(data["desired_set_sha256"], "$.desired_set_sha256")
        if digest != expected_digest:
            raise ContractError("$.desired_set_sha256: does not bind desired work")
        sealed_value = data["sealed_input_manifest_sha256"]
        sealed_input_manifest_sha256 = (
            None
            if sealed_value is None
            else _digest(sealed_value, "$.sealed_input_manifest_sha256")
        )
        return cls(
            source_epoch=source_epoch,
            policy_sha256=policy_sha256,
            source_observations=source_observations,
            desired_watermark=desired_watermark,
            semantic_required=semantic_required,
            desired=desired,
            desired_set_sha256=digest,
            sealed_input_manifest_sha256=sealed_input_manifest_sha256,
        )


@dataclass(frozen=True)
class SemanticQueueSnapshot:
    """Canonical internal durable state for one workspace semantic queue."""

    repo_uuid: str
    active_source_revision: int | None
    revision: int
    desired_watermark: int
    completed_watermark: int
    compaction_epoch: int
    last_served_operation: str | None
    queue_policy: SemanticQueuePolicy
    reconciliation: _SemanticReconciliation | None
    items: tuple[SemanticQueueItem, ...]

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    @property
    def queue_bytes(self) -> int:
        return len(self.canonical)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": "graphify.workspace.semantic_queue.internal",
            "format_version": 1,
            "repo_uuid": self.repo_uuid,
            "active_source_revision": self.active_source_revision,
            "revision": self.revision,
            "desired_watermark": self.desired_watermark,
            "completed_watermark": self.completed_watermark,
            "compaction_epoch": self.compaction_epoch,
            "last_served_operation": self.last_served_operation,
            "queue_policy": self.queue_policy.to_dict(),
            "reconciliation": (
                None if self.reconciliation is None else self.reconciliation.to_dict()
            ),
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def initial(
        cls,
        repo_uuid: str,
        policy: SemanticQueuePolicy,
    ) -> "SemanticQueueSnapshot":
        return cls(
            repo_uuid=repo_uuid,
            active_source_revision=None,
            revision=0,
            desired_watermark=0,
            completed_watermark=0,
            compaction_epoch=0,
            last_served_operation=None,
            queue_policy=policy,
            reconciliation=None,
            items=(),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticQueueSnapshot":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "contract",
                "format_version",
                "repo_uuid",
                "active_source_revision",
                "revision",
                "desired_watermark",
                "completed_watermark",
                "compaction_epoch",
                "last_served_operation",
                "queue_policy",
                "reconciliation",
                "items",
            },
        )
        if data["contract"] != "graphify.workspace.semantic_queue.internal":
            raise ContractError("$.contract: unsupported semantic queue state")
        if _integer(data["format_version"], "$.format_version", minimum=1) != 1:
            raise ContractError("$.format_version: unsupported semantic queue state version")
        repo_uuid = str(LeaseStore._directory(_string(data["repo_uuid"], "$.repo_uuid")).parts[-1])
        active_source_revision_value = data["active_source_revision"]
        active_source_revision = (
            None
            if active_source_revision_value is None
            else _integer(
                active_source_revision_value,
                "$.active_source_revision",
                minimum=1,
            )
        )
        revision = _integer(data["revision"], "$.revision")
        desired_watermark = _integer(data["desired_watermark"], "$.desired_watermark")
        completed_watermark = _integer(
            data["completed_watermark"],
            "$.completed_watermark",
        )
        if completed_watermark > desired_watermark:
            raise ContractError("$.completed_watermark: exceeds desired watermark")
        compaction_epoch = _integer(data["compaction_epoch"], "$.compaction_epoch")
        last = data["last_served_operation"]
        if last is not None:
            last = _operation(last, "$.last_served_operation")
        queue_policy = SemanticQueuePolicy.from_mapping(
            cast(Mapping[str, object], _mapping(data["queue_policy"], "$.queue_policy"))
        )
        reconciliation_value = data["reconciliation"]
        reconciliation = (
            None
            if reconciliation_value is None
            else _SemanticReconciliation.from_mapping(
                cast(
                    Mapping[str, object],
                    _mapping(reconciliation_value, "$.reconciliation"),
                )
            )
        )
        if reconciliation is not None and reconciliation.desired_watermark != desired_watermark:
            raise ContractError("$.reconciliation.desired_watermark: must match queue watermark")
        raw_items = data["items"]
        if not isinstance(raw_items, list):
            raise ContractError("$.items: expected array")
        items = tuple(
            SemanticQueueItem.from_mapping(
                cast(Mapping[str, object], _mapping(item, f"$.items[{index}]"))
            )
            for index, item in enumerate(raw_items)
        )
        for index, item in enumerate(items):
            if item.claim is None:
                continue
            claim_body = item.claim.to_dict()
            del claim_body["claim_id"]
            del claim_body["checkpoint"]
            if item.claim.claim_id != _semantic_claim_id(repo_uuid, claim_body):
                raise ContractError(
                    f"$.items[{index}].claim.claim_id: does not bind workspace claim"
                )
        if items != tuple(sorted(items, key=lambda item: item.work.sort_key)):
            raise ContractError("$.items: work must be deterministically sorted")
        keys = [item.work.coalescing_key for item in items]
        if len(set(keys)) != len(keys):
            raise ContractError("$.items: coalescing keys must be unique")
        if any(item.desired_revision > desired_watermark for item in items):
            raise ContractError("$.items: desired revision exceeds queue watermark")
        if active_source_revision is None and (
            desired_watermark != 0 or reconciliation is not None or items
        ):
            raise ContractError(
                "$.active_source_revision: durable desired work must bind an active source"
            )
        if reconciliation is not None:
            desired_by_identity = {work.identity: work for work in reconciliation.desired}
            for item in items:
                if item.work.identity not in desired_by_identity:
                    raise ContractError("$.items: item is outside exact reconciliation")
            if completed_watermark < desired_watermark:
                item_identities = {item.work.identity for item in items}
                if item_identities != set(desired_by_identity):
                    raise ContractError(
                        "$.items: incomplete reconciliation must retain every desired item"
                    )
            elif any(item.status != "completed" for item in items):
                raise ContractError(
                    "$.items: completed watermark permits only completed retained items"
                )
            if (
                reconciliation.sealed_input_manifest_sha256 is not None
                and completed_watermark != desired_watermark
            ):
                raise ContractError(
                    "$.reconciliation.sealed_input_manifest_sha256: "
                    "incomplete queue cannot bind staged inputs"
                )
        return cls(
            repo_uuid=repo_uuid,
            active_source_revision=active_source_revision,
            revision=revision,
            desired_watermark=desired_watermark,
            completed_watermark=completed_watermark,
            compaction_epoch=compaction_epoch,
            last_served_operation=cast(str | None, last),
            queue_policy=queue_policy,
            reconciliation=reconciliation,
            items=items,
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "SemanticQueueSnapshot":
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"$: invalid JSON: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ContractError("$: expected object")
        document = cls.from_mapping(cast(Mapping[str, object], parsed))
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if document.canonical != raw:
            raise ContractError("$: internal semantic queue is not canonical JSON")
        return document


@dataclass(frozen=True)
class SemanticCapabilityDecision:
    """Advisory capability report; queue mutation re-derives policy itself."""

    available: bool
    executor: str | None
    backend: str | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "executor": self.executor,
            "backend": self.backend,
            "reason": self.reason,
        }


def decide_semantic_capability(
    config: WorkspaceConfig,
    *,
    host_agent_active: bool,
    explicit_backend: str | None,
) -> SemanticCapabilityDecision:
    """Choose only a live host agent or a caller-named allowlisted backend."""

    if type(host_agent_active) is not bool:
        return SemanticCapabilityDecision(
            available=False,
            executor=None,
            backend=None,
            reason="host_agent_active_invalid",
        )
    policy = cast(Mapping[str, object], config.to_dict()["policy"])
    mode = str(policy["semantic_mode"])
    allowlist = tuple(str(value) for value in cast(list[object], policy["headless_backends"]))
    network_egress = bool(policy["network_egress"])
    if explicit_backend is not None and (
        not isinstance(explicit_backend, str) or not explicit_backend
    ):
        return SemanticCapabilityDecision(
            available=False,
            executor=None,
            backend=None,
            reason="explicit_backend_invalid",
        )
    if host_agent_active and explicit_backend is None:
        return SemanticCapabilityDecision(
            available=True,
            executor="host_agent",
            backend=None,
            reason="active_host_agent",
        )
    if mode == "host_agent_only":
        return SemanticCapabilityDecision(
            available=False,
            executor=None,
            backend=None,
            reason=(
                "explicit_backend_forbidden"
                if explicit_backend is not None
                else "host_agent_inactive"
            ),
        )
    if explicit_backend is None:
        return SemanticCapabilityDecision(
            available=False,
            executor=None,
            backend=None,
            reason="explicit_backend_required",
        )
    if explicit_backend not in allowlist:
        return SemanticCapabilityDecision(
            available=False,
            executor=None,
            backend=None,
            reason="explicit_backend_not_allowlisted",
        )
    if not network_egress:
        return SemanticCapabilityDecision(
            available=False,
            executor=None,
            backend=None,
            reason="network_egress_forbidden",
        )
    return SemanticCapabilityDecision(
        available=True,
        executor="explicit_backend",
        backend=explicit_backend,
        reason="explicit_backend_selected",
    )


@dataclass(frozen=True)
class SemanticCertificationView:
    repo_uuid: str
    queue_revision: int
    queue_state_sha256: str
    queue_watermark: int
    completed_watermark: int
    compaction_epoch: int
    source_epoch: int
    source_commit: str
    policy_sha256: str
    observation_manifest_sha256: str
    observation_evidence_sha256: str
    sealed_input_manifest_sha256: str
    semantic_completeness: str

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_uuid": self.repo_uuid,
            "queue_revision": self.queue_revision,
            "queue_state_sha256": self.queue_state_sha256,
            "queue_watermark": self.queue_watermark,
            "completed_watermark": self.completed_watermark,
            "compaction_epoch": self.compaction_epoch,
            "source_epoch": self.source_epoch,
            "source_commit": self.source_commit,
            "policy_sha256": self.policy_sha256,
            "observation_manifest_sha256": self.observation_manifest_sha256,
            "observation_evidence_sha256": self.observation_evidence_sha256,
            "sealed_input_manifest_sha256": self.sealed_input_manifest_sha256,
            "semantic_completeness": self.semantic_completeness,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SemanticCertificationView":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "repo_uuid",
                "queue_revision",
                "queue_state_sha256",
                "queue_watermark",
                "completed_watermark",
                "compaction_epoch",
                "source_epoch",
                "source_commit",
                "policy_sha256",
                "observation_manifest_sha256",
                "observation_evidence_sha256",
                "sealed_input_manifest_sha256",
                "semantic_completeness",
            },
        )
        repo_uuid = str(LeaseStore._directory(_string(data["repo_uuid"], "$.repo_uuid")).parts[-1])
        source_commit = _string(data["source_commit"], "$.source_commit")
        if _COMMIT_RE.fullmatch(source_commit) is None:
            raise ContractError("$.source_commit: expected lowercase Git commit")
        queue_watermark = _integer(
            data["queue_watermark"],
            "$.queue_watermark",
            minimum=1,
        )
        completed_watermark = _integer(
            data["completed_watermark"],
            "$.completed_watermark",
            minimum=1,
        )
        if completed_watermark != queue_watermark:
            raise ContractError("$.completed_watermark: certification requires exact watermark")
        completeness = _string(data["semantic_completeness"], "$.semantic_completeness")
        if completeness not in {"complete", "not_required"}:
            raise ContractError("$.semantic_completeness: unsupported certification value")
        return cls(
            repo_uuid=repo_uuid,
            queue_revision=_integer(data["queue_revision"], "$.queue_revision", minimum=1),
            queue_state_sha256=_digest(data["queue_state_sha256"], "$.queue_state_sha256"),
            queue_watermark=queue_watermark,
            completed_watermark=completed_watermark,
            compaction_epoch=_integer(data["compaction_epoch"], "$.compaction_epoch"),
            source_epoch=_integer(data["source_epoch"], "$.source_epoch", minimum=1),
            source_commit=source_commit,
            policy_sha256=_digest(data["policy_sha256"], "$.policy_sha256"),
            observation_manifest_sha256=_digest(
                data["observation_manifest_sha256"],
                "$.observation_manifest_sha256",
            ),
            observation_evidence_sha256=_digest(
                data["observation_evidence_sha256"],
                "$.observation_evidence_sha256",
            ),
            sealed_input_manifest_sha256=_digest(
                data["sealed_input_manifest_sha256"],
                "$.sealed_input_manifest_sha256",
            ),
            semantic_completeness=completeness,
        )


@dataclass(frozen=True)
class _SemanticCertificationBinding:
    repo_uuid: str
    generation_id: str
    request_sha256: str
    view: SemanticCertificationView

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": "graphify.workspace.semantic_certification_binding.internal",
            "format_version": 1,
            "repo_uuid": self.repo_uuid,
            "generation_id": self.generation_id,
            "request_sha256": self.request_sha256,
            "view": self.view.to_dict(),
        }

    @classmethod
    def create(
        cls,
        *,
        repo_uuid: str,
        generation_id: str,
        request_sha256: str,
        view: SemanticCertificationView,
    ) -> "_SemanticCertificationBinding":
        return cls.from_mapping(
            {
                "contract": "graphify.workspace.semantic_certification_binding.internal",
                "format_version": 1,
                "repo_uuid": repo_uuid,
                "generation_id": generation_id,
                "request_sha256": request_sha256,
                "view": view.to_dict(),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "_SemanticCertificationBinding":
        data = _mapping(value, "$")
        _exact_keys(
            data,
            "$",
            {
                "contract",
                "format_version",
                "repo_uuid",
                "generation_id",
                "request_sha256",
                "view",
            },
        )
        if data["contract"] != "graphify.workspace.semantic_certification_binding.internal":
            raise ContractError("$.contract: unsupported semantic certification binding")
        if _integer(data["format_version"], "$.format_version", minimum=1) != 1:
            raise ContractError("$.format_version: unsupported certification binding version")
        repo_uuid = str(LeaseStore._directory(_string(data["repo_uuid"], "$.repo_uuid")).parts[-1])
        generation_id = _string(data["generation_id"], "$.generation_id")
        if _GENERATION_RE.fullmatch(generation_id) is None:
            raise ContractError("$.generation_id: invalid generation identity")
        view = SemanticCertificationView.from_mapping(
            cast(Mapping[str, object], _mapping(data["view"], "$.view"))
        )
        if view.repo_uuid != repo_uuid:
            raise ContractError("$.view.repo_uuid: differs from certification binding")
        return cls(
            repo_uuid=repo_uuid,
            generation_id=generation_id,
            request_sha256=_digest(data["request_sha256"], "$.request_sha256"),
            view=view,
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "_SemanticCertificationBinding":
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"$: invalid JSON: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ContractError("$: expected object")
        document = cls.from_mapping(cast(Mapping[str, object], parsed))
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if document.canonical != raw:
            raise ContractError("$: semantic certification binding is not canonical JSON")
        return document


class SemanticQueueStore:
    """Durable queue state under the existing registry-before-workspace lock order."""

    def __init__(
        self,
        state_root: Path,
        leases: LeaseStore,
        *,
        policy: SemanticQueuePolicy,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.leases = leases
        self.policy = SemanticQueuePolicy.from_mapping(policy.to_dict())
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        if self.state.root != leases.state.root:
            raise SemanticQueueError("semantic queue and lease stores must share one root")

    @staticmethod
    def _paths(repo_uuid: str) -> tuple[Path, Path, Path]:
        directory = LeaseStore._directory(repo_uuid) / "queue"
        return (
            directory / "semantic.jsonl",
            directory / "semantic.previous.jsonl",
            directory / "semantic.pending.jsonl",
        )

    def _load_locked(
        self,
        repo_uuid: str,
        *,
        recover: bool = True,
    ) -> SemanticQueueSnapshot:
        current, previous, pending = self._paths(repo_uuid)
        if not recover and not self.state.private_directory_exists(current.parent):
            return SemanticQueueSnapshot.initial(repo_uuid, self.policy)
        loader = self.state.recover_record if recover else self.state.read_stable_record
        try:
            snapshot = loader(
                label="semantic_queue",
                current=current,
                previous=previous,
                pending=pending,
                decoder=SemanticQueueSnapshot.from_json,
                revision=lambda value: value.revision,
                allow_missing=True,
            )
        except (ContractError, StateCorrupt, StatePathError) as exc:
            raise SemanticQueueCorrupt(str(exc)) from exc
        if snapshot is None:
            return SemanticQueueSnapshot.initial(repo_uuid, self.policy)
        if snapshot.repo_uuid != repo_uuid:
            raise SemanticQueueCorrupt("queue is installed under the wrong workspace")
        if snapshot.queue_policy != self.policy:
            raise SemanticQueueConflict("durable queue policy differs from the active policy")
        return snapshot

    def _bounded(self, snapshot: SemanticQueueSnapshot) -> None:
        if len(snapshot.items) > self.policy.max_items:
            raise SemanticQueueCapacityExceeded(
                f"item capacity {self.policy.max_items} would be exceeded"
            )
        if snapshot.queue_bytes > self.policy.max_bytes:
            raise SemanticQueueCapacityExceeded(
                f"byte capacity {self.policy.max_bytes} would be exceeded"
            )

    def _commit_locked(
        self,
        current: SemanticQueueSnapshot,
        candidate: SemanticQueueSnapshot,
    ) -> SemanticQueueSnapshot:
        document = replace(candidate, revision=current.revision + 1)
        validated = SemanticQueueSnapshot.from_mapping(document.to_dict())
        self._bounded(validated)
        current_path, previous_path, pending_path = self._paths(validated.repo_uuid)
        try:
            return self.state.commit_record(
                label="semantic_queue",
                current=current_path,
                previous=previous_path,
                pending=pending_path,
                payload=validated.canonical,
                decoder=SemanticQueueSnapshot.from_json,
            )
        except StateCorrupt as exc:
            raise SemanticQueueCorrupt(str(exc)) from exc

    @contextmanager
    def _semantic_operation(
        self,
        grant: LeaseGrant,
        *,
        monotonic_ns: int,
    ) -> Iterator[LeaseOperation]:
        try:
            with self.leases.current_operation(
                grant,
                monotonic_ns=monotonic_ns,
                allowed_operations=frozenset({"SEMANTIC_CLAIM"}),
                registry_required=True,
            ) as operation:
                yield operation
        except (LeaseExpired, StaleLease) as exc:
            raise StaleSemanticClaim(str(exc)) from exc

    def _active_workspace_config(
        self,
        operation: LeaseOperation,
    ) -> tuple[WorkspaceConfig, str]:
        workspaces = cast(
            list[dict[str, Any]],
            operation.registry.to_dict()["workspaces"],
        )
        entries = [
            entry for entry in workspaces if entry["repo_uuid"] == operation.repo_uuid
        ]
        if len(entries) != 1:
            raise SemanticCapabilityUnavailable("workspace_config_unavailable")
        recorded = cast(dict[str, Any], entries[0]["active_source"])
        active_evidence = cast(dict[str, Any], entries[0]["active_source_evidence"])
        try:
            evidence = self.leases.registry.read_evidence(
                cast(str, active_evidence["rebind_evidence_sha256"])
            )
        except (OSError, StateCorrupt, StatePathError) as exc:
            raise SemanticCapabilityUnavailable("workspace_config_unavailable") from exc
        if evidence.get("repo_uuid") != operation.repo_uuid or evidence.get("source") != recorded:
            raise SemanticCapabilityUnavailable("workspace_config_mismatch")
        try:
            expected_config_sha256 = _digest(
                evidence.get("config_sha256"),
                "$.config_sha256",
            )
            common_device = evidence.get("git_common_device")
            common_inode = evidence.get("git_common_inode")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (common_device, common_inode)
            ):
                raise ContractError("$.git_common_identity: invalid device or inode")
            source_root = Path(cast(str, recorded["path"]))
            root_identity = source_root_identity(source_root)
            config, config_sha256 = read_workspace_config_with_digest(source_root)
            verify_source_checkout(
                source_root,
                expected_git_common_dir=Path(cast(str, recorded["git_common_dir"])),
                expected_worktree_id=cast(str, recorded["worktree_id"]),
                expected_git_common_device=cast(int, common_device),
                expected_git_common_inode=cast(int, common_inode),
                expected_root_identity=root_identity,
            )
            confirmed_config, confirmed_config_sha256 = read_workspace_config_with_digest(
                source_root
            )
            verify_source_checkout(
                source_root,
                expected_git_common_dir=Path(cast(str, recorded["git_common_dir"])),
                expected_worktree_id=cast(str, recorded["worktree_id"]),
                expected_git_common_device=cast(int, common_device),
                expected_git_common_inode=cast(int, common_inode),
                expected_root_identity=root_identity,
            )
            if (
                config_sha256 != expected_config_sha256
                or confirmed_config_sha256 != expected_config_sha256
                or confirmed_config != config
            ):
                raise SemanticCapabilityUnavailable("workspace_config_mismatch")
        except (ContractError, OSError, IdentityError) as exc:
            raise SemanticCapabilityUnavailable("workspace_config_unavailable") from exc
        return confirmed_config, confirmed_config_sha256

    @staticmethod
    def _sorted_items(items: Sequence[SemanticQueueItem]) -> tuple[SemanticQueueItem, ...]:
        return tuple(sorted(items, key=lambda item: item.work.sort_key))

    @staticmethod
    def _require_current_source(
        snapshot: SemanticQueueSnapshot,
        operation: LeaseOperation,
    ) -> None:
        if (
            snapshot.active_source_revision is None
            and snapshot.desired_watermark == 0
            and snapshot.reconciliation is None
            and not snapshot.items
        ):
            return
        if snapshot.active_source_revision != operation.grant.active_source_revision:
            raise SemanticQueueConflict("active source changed; exact reconciliation is required")

    def enqueue(
        self,
        grant: LeaseGrant,
        work: SemanticDesiredWork,
        *,
        monotonic_ns: int,
    ) -> SemanticQueueSnapshot:
        desired = work.validated()
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=_MUTATING_OPERATIONS,
        ) as operation:
            current = self._load_locked(operation.repo_uuid)
            if current.active_source_revision not in {
                None,
                operation.grant.active_source_revision,
            }:
                raise SemanticQueueConflict(
                    "active source changed; exact reconciliation is required"
                )
            by_key = {item.work.coalescing_key: item for item in current.items}
            existing = by_key.get(desired.coalescing_key)
            if existing is not None and existing.work == desired:
                return current
            if (
                existing is None
                and current.reconciliation is not None
                and desired in current.reconciliation.desired
                and current.completed_watermark == current.desired_watermark
            ):
                return current
            if desired.desired_revision <= current.desired_watermark:
                raise SemanticQueueConflict(
                    "desired revision must advance the durable queue watermark"
                )
            if existing is not None and desired.desired_revision <= existing.desired_revision:
                raise SemanticQueueConflict("desired revision does not supersede queued work")
            by_key[desired.coalescing_key] = SemanticQueueItem(
                work=desired,
                status="pending",
                failure_count=0,
                last_error=None,
                claim=None,
            )
            candidate = replace(
                current,
                active_source_revision=operation.grant.active_source_revision,
                desired_watermark=desired.desired_revision,
                reconciliation=None,
                items=self._sorted_items(tuple(by_key.values())),
            )
            return self._commit_locked(current, candidate)

    def reconcile(
        self,
        grant: LeaseGrant,
        desired: Sequence[SemanticDesiredWork],
        *,
        source_epoch: int,
        policy_sha256: str,
        source_observations: Sequence[SourceObservation],
        desired_watermark: int,
        semantic_required: bool,
        monotonic_ns: int,
    ) -> SemanticQueueSnapshot:
        reconciliation = _SemanticReconciliation.create(
            source_epoch=source_epoch,
            policy_sha256=policy_sha256,
            source_observations=source_observations,
            desired_watermark=desired_watermark,
            semantic_required=semantic_required,
            desired=desired,
        )
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=_MUTATING_OPERATIONS,
        ) as operation:
            current = self._load_locked(operation.repo_uuid)
            if desired_watermark < current.desired_watermark:
                raise SemanticQueueConflict("reconciliation desired revision moved backward")
            source_changed = current.active_source_revision not in {
                None,
                operation.grant.active_source_revision,
            }
            if source_changed and desired_watermark == current.desired_watermark:
                raise SemanticQueueConflict(
                    "active source changed; reconciliation desired revision must advance"
                )
            existing_by_identity = (
                {} if source_changed else {item.work.identity: item for item in current.items}
            )
            items = tuple(
                existing_by_identity.get(
                    work.identity,
                    SemanticQueueItem(
                        work=work,
                        status="pending",
                        failure_count=0,
                        last_error=None,
                        claim=None,
                    ),
                )
                for work in reconciliation.desired
            )
            causal_items: list[SemanticQueueItem] = []
            for item in items:
                has_unfinished_predecessor = any(
                    predecessor.path == item.path
                    and predecessor.desired_revision < item.desired_revision
                    and predecessor.status != "completed"
                    for predecessor in items
                )
                if item.status not in {"claimed", "completed"} or not (
                    has_unfinished_predecessor
                ):
                    causal_items.append(item)
                    continue
                failures = item.failure_count + 1
                causal_items.append(
                    replace(
                        item,
                        status=(
                            "pending" if failures <= self.policy.retry_budget else "dead_letter"
                        ),
                        failure_count=failures,
                        last_error="reconciliation_predecessor",
                        claim=None,
                    )
                )
            items = tuple(causal_items)
            if desired_watermark == current.desired_watermark:
                existing_reconciliation = current.reconciliation
                if existing_reconciliation is not None and (
                    replace(
                        existing_reconciliation,
                        sealed_input_manifest_sha256=None,
                    )
                    == reconciliation
                    and (
                        current.items == items
                        or (
                            current.completed_watermark == current.desired_watermark
                            and not current.items
                        )
                    )
                ):
                    return current
                raise SemanticQueueConflict(
                    "desired revision is already bound to different reconciliation evidence"
                )
            completed = (
                desired_watermark
                if not items or all(item.status == "completed" for item in items)
                else current.completed_watermark
            )
            candidate = replace(
                current,
                active_source_revision=operation.grant.active_source_revision,
                desired_watermark=desired_watermark,
                completed_watermark=completed,
                reconciliation=reconciliation,
                items=self._sorted_items(items),
            )
            return self._commit_locked(current, candidate)

    @staticmethod
    def _claim_matches_operation(
        claim: SemanticClaim,
        operation: LeaseOperation,
    ) -> bool:
        lease = operation.lease.to_dict()
        return (
            claim.fence_token == int(lease["fence_token"])
            and claim.operation_epoch == operation.grant.operation_epoch
            and claim.migration_epoch == operation.grant.migration_epoch
            and claim.active_source_revision == operation.grant.active_source_revision
            and dict(claim.owner) == lease["owner"]
        )

    @staticmethod
    def _next_operation(last: str | None, candidates: Sequence[SemanticQueueItem]) -> str:
        operations = sorted({item.operation for item in candidates})
        if last not in operations:
            return operations[0]
        index = operations.index(cast(str, last))
        return operations[(index + 1) % len(operations)]

    @staticmethod
    def _claim_for(
        operation: LeaseOperation,
        work: SemanticDesiredWork,
        *,
        attempt: int,
    ) -> SemanticClaim:
        lease = operation.lease.to_dict()
        body = {
            "work": work.to_dict(),
            "fence_token": lease["fence_token"],
            "operation_epoch": operation.grant.operation_epoch,
            "migration_epoch": operation.grant.migration_epoch,
            "active_source_revision": operation.grant.active_source_revision,
            "attempt": attempt,
            "owner": lease["owner"],
        }
        return SemanticClaim.from_mapping(
            {
                **body,
                "claim_id": _semantic_claim_id(operation.repo_uuid, body),
                "checkpoint": None,
            }
        )

    def claim(
        self,
        grant: LeaseGrant,
        *,
        config: WorkspaceConfig,
        host_agent_active: bool,
        explicit_backend: str | None,
        monotonic_ns: int,
    ) -> SemanticClaim | None:
        """Claim work only after deriving authority at this mutation boundary."""

        repo_uuid = cast(str, grant.lease.to_dict()["repo_uuid"])
        # The pre-lock and locked reads are the two policy observations required
        # to reject replacement/ABA without repeating full source discovery.
        try:
            observed_registry = self.leases.registry.load()
            observed_entries = [
                entry
                for entry in cast(
                    list[dict[str, Any]],
                    observed_registry.to_dict()["workspaces"],
                )
                if entry["repo_uuid"] == repo_uuid
            ]
            if len(observed_entries) != 1:
                raise SemanticCapabilityUnavailable("workspace_config_unavailable")
            observed_source = cast(dict[str, Any], observed_entries[0]["active_source"])
            _observed_config, observed_config_sha256 = read_workspace_config_with_digest(
                Path(cast(str, observed_source["path"]))
            )
        except SemanticCapabilityUnavailable:
            raise
        except (OSError, IdentityError, StateCorrupt, StatePathError) as exc:
            raise SemanticCapabilityUnavailable("workspace_config_unavailable") from exc
        with self._semantic_operation(grant, monotonic_ns=monotonic_ns) as operation:
            try:
                validated_config = cast(
                    WorkspaceConfig,
                    WorkspaceConfig.from_mapping(config.to_dict()),
                )
            except ContractError as exc:
                raise SemanticCapabilityUnavailable("workspace_config_invalid") from exc
            if validated_config.to_dict()["repo_uuid"] != operation.repo_uuid:
                raise SemanticCapabilityUnavailable("workspace_config_mismatch")
            active_config, active_config_sha256 = self._active_workspace_config(operation)
            if (
                validated_config.to_dict() != active_config.to_dict()
                or observed_config_sha256 != active_config_sha256
            ):
                raise SemanticCapabilityUnavailable("workspace_config_mismatch")
            capability = decide_semantic_capability(
                active_config,
                host_agent_active=host_agent_active,
                explicit_backend=explicit_backend,
            )
            if not capability.available:
                raise SemanticCapabilityUnavailable(capability.reason)
            current = self._load_locked(operation.repo_uuid)
            self._require_current_source(current, operation)
            changed = False
            recovered: list[SemanticQueueItem] = []
            active: SemanticClaim | None = None
            for item in current.items:
                if item.status != "claimed" or item.claim is None:
                    recovered.append(item)
                    continue
                if self._claim_matches_operation(item.claim, operation):
                    if active is not None:
                        raise SemanticQueueCorrupt(
                            "one semantic lease owns multiple active queue claims"
                        )
                    active = item.claim
                    recovered.append(item)
                    continue
                failures = item.failure_count + 1
                status = "pending" if failures <= self.policy.retry_budget else "dead_letter"
                recovered.append(
                    replace(
                        item,
                        status=status,
                        failure_count=failures,
                        last_error="claim_expired",
                        claim=None,
                    )
                )
                changed = True
            eligible: list[SemanticQueueItem] = []
            if active is None:
                pending = [item for item in recovered if item.status == "pending"]
                eligible = [
                    item
                    for item in pending
                    if not any(
                        other.path == item.path
                        and other.desired_revision < item.desired_revision
                        and other.status != "completed"
                        for other in recovered
                    )
                ]
            if active is not None or not eligible:
                if changed:
                    self._commit_locked(
                        current,
                        replace(current, items=self._sorted_items(recovered)),
                    )
                return active
            selected_operation = self._next_operation(
                current.last_served_operation,
                eligible,
            )
            selected = min(
                (item for item in eligible if item.operation == selected_operation),
                key=lambda item: (item.desired_revision, item.path, item.content_sha256),
            )
            claim = self._claim_for(
                operation,
                selected.work,
                # The queue revision is a durable monotonic claim generation.
                # Failure counts can disappear with compacted tombstones, so
                # they cannot safely fence a reconstructed item's old token.
                attempt=current.revision + 1,
            )
            claimed_items = [
                replace(item, status="claimed", claim=claim)
                if item.work.coalescing_key == selected.work.coalescing_key
                else item
                for item in recovered
            ]
            committed = self._commit_locked(
                current,
                replace(
                    current,
                    last_served_operation=selected_operation,
                    items=self._sorted_items(claimed_items),
                ),
            )
            match = next(
                item.claim
                for item in committed.items
                if item.work.coalescing_key == selected.work.coalescing_key
            )
            assert match is not None
            return match

    @staticmethod
    def _claimed_item(
        snapshot: SemanticQueueSnapshot,
        claim: SemanticClaim,
        operation: LeaseOperation,
    ) -> tuple[int, SemanticQueueItem]:
        for index, item in enumerate(snapshot.items):
            if item.work.coalescing_key != claim.work.coalescing_key:
                continue
            if item.work != claim.work:
                raise StaleSemanticClaim("newer desired work replaced the claimed revision")
            if (
                item.status != "claimed"
                or item.claim is None
                or item.claim.claim_id != claim.claim_id
            ):
                raise StaleSemanticClaim("claim is no longer current")
            if not SemanticQueueStore._claim_matches_operation(item.claim, operation):
                raise StaleSemanticClaim("claim fence or owner is no longer current")
            return index, item
        raise StaleSemanticClaim("claimed work no longer exists")

    def checkpoint(
        self,
        grant: LeaseGrant,
        claim: SemanticClaim,
        *,
        checkpoint: str,
        monotonic_ns: int,
    ) -> SemanticClaim:
        checkpoint_value = _optional_text(checkpoint, "$.checkpoint", maximum=256)
        if checkpoint_value is None:
            raise ContractError("$.checkpoint: expected non-empty string")
        with self._semantic_operation(grant, monotonic_ns=monotonic_ns) as operation:
            current = self._load_locked(operation.repo_uuid)
            self._require_current_source(current, operation)
            index, item = self._claimed_item(current, claim, operation)
            updated_claim = replace(cast(SemanticClaim, item.claim), checkpoint=checkpoint_value)
            items = list(current.items)
            items[index] = replace(item, claim=updated_claim)
            committed = self._commit_locked(
                current,
                replace(current, items=self._sorted_items(items)),
            )
            committed_item = next(
                candidate
                for candidate in committed.items
                if candidate.work.coalescing_key == claim.work.coalescing_key
            )
            assert committed_item.claim is not None
            return committed_item.claim

    @staticmethod
    def _all_reconciled_complete(
        reconciliation: _SemanticReconciliation | None,
        items: Sequence[SemanticQueueItem],
    ) -> bool:
        if reconciliation is None:
            return False
        by_identity = {item.work.identity: item for item in items}
        return all(
            work.identity in by_identity and by_identity[work.identity].status == "completed"
            for work in reconciliation.desired
        )

    def complete(
        self,
        grant: LeaseGrant,
        claim: SemanticClaim,
        *,
        monotonic_ns: int,
    ) -> SemanticQueueSnapshot:
        with self._semantic_operation(grant, monotonic_ns=monotonic_ns) as operation:
            current = self._load_locked(operation.repo_uuid)
            self._require_current_source(current, operation)
            index, item = self._claimed_item(current, claim, operation)
            items = list(current.items)
            items[index] = replace(
                item,
                status="completed",
                last_error=None,
                claim=None,
            )
            completed_watermark = current.completed_watermark
            if self._all_reconciled_complete(current.reconciliation, items):
                completed_watermark = current.desired_watermark
            return self._commit_locked(
                current,
                replace(
                    current,
                    completed_watermark=completed_watermark,
                    items=self._sorted_items(items),
                ),
            )

    def fail(
        self,
        grant: LeaseGrant,
        claim: SemanticClaim,
        *,
        error_code: str,
        retryable: bool,
        monotonic_ns: int,
    ) -> SemanticQueueSnapshot:
        if _ERROR_RE.fullmatch(error_code) is None:
            raise SemanticQueueError("error_code must be a stable lowercase classification")
        with self._semantic_operation(grant, monotonic_ns=monotonic_ns) as operation:
            current = self._load_locked(operation.repo_uuid)
            self._require_current_source(current, operation)
            index, item = self._claimed_item(current, claim, operation)
            failures = item.failure_count + 1
            status = (
                "pending" if retryable and failures <= self.policy.retry_budget else "dead_letter"
            )
            items = list(current.items)
            items[index] = replace(
                item,
                status=status,
                failure_count=failures,
                last_error=error_code,
                claim=None,
            )
            return self._commit_locked(
                current,
                replace(current, items=self._sorted_items(items)),
            )

    def bind_sealed_inputs(
        self,
        grant: LeaseGrant,
        *,
        sealed_input_manifest_sha256: str,
        monotonic_ns: int,
    ) -> SemanticQueueSnapshot:
        """Durably bind one completed reconciliation to exact staged payload bytes."""

        digest = _digest(
            sealed_input_manifest_sha256,
            "$.sealed_input_manifest_sha256",
        )
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=_MUTATING_OPERATIONS,
        ) as operation:
            current = self._load_locked(operation.repo_uuid)
            if current.active_source_revision != operation.grant.active_source_revision:
                raise SemanticCertificationBlocked(
                    "active source changed; exact reconciliation is required"
                )
            reconciliation = current.reconciliation
            if reconciliation is None:
                raise SemanticCertificationBlocked(
                    "exact reconciliation is required before sealing staged inputs"
                )
            if current.completed_watermark != current.desired_watermark or any(
                item.status != "completed" for item in current.items
            ):
                raise SemanticCertificationBlocked(
                    "semantic work must be complete before sealing staged inputs"
                )
            existing = reconciliation.sealed_input_manifest_sha256
            if existing == digest:
                return current
            if existing is not None:
                raise SemanticQueueConflict(
                    "queue watermark is already bound to different staged inputs"
                )
            candidate = replace(
                current,
                reconciliation=replace(
                    reconciliation,
                    sealed_input_manifest_sha256=digest,
                ),
            )
            return self._commit_locked(current, candidate)

    def compact(
        self,
        grant: LeaseGrant,
        *,
        monotonic_ns: int,
    ) -> SemanticQueueSnapshot:
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=_COMPACTION_OPERATIONS,
            registry_required=True,
        ) as operation:
            current = self._load_locked(operation.repo_uuid)
            self._require_current_source(current, operation)
            items = current.items
            if current.completed_watermark == current.desired_watermark:
                items = tuple(item for item in items if item.status != "completed")
            candidate = replace(
                current,
                compaction_epoch=current.compaction_epoch + 1,
                items=self._sorted_items(items),
            )
            return self._commit_locked(current, candidate)

    def inspect(self, repo_uuid: str) -> SemanticQueueSnapshot:
        canonical = str(LeaseStore._directory(repo_uuid).parts[-1])
        with self.leases.registry.read_only_snapshot():
            with self.leases.read_only_workspace_lock(canonical):
                return self._load_locked(canonical, recover=False)

    @staticmethod
    def _certification_binding_path(repo_uuid: str, generation_id: str) -> Path:
        if _GENERATION_RE.fullmatch(generation_id) is None:
            raise SemanticCertificationBlocked("invalid generation identity")
        return (
            LeaseStore._directory(repo_uuid) / "queue" / "certifications" / f"{generation_id}.json"
        )

    @classmethod
    def _certification_binding_from_state(
        cls,
        state: DurableStateRoot,
        repo_uuid: str,
        *,
        generation_id: str,
        request_sha256: str,
        sealed_input_manifest_sha256: str,
    ) -> SemanticCertificationView | None:
        request_digest = _digest(request_sha256, "$.request_sha256")
        sealed_digest = _digest(
            sealed_input_manifest_sha256,
            "$.sealed_input_manifest_sha256",
        )
        path = cls._certification_binding_path(repo_uuid, generation_id)
        try:
            raw = state.read_optional_existing_bytes(path)
            if raw is None:
                return None
            binding = _SemanticCertificationBinding.from_json(raw)
        except (ContractError, StateCorrupt, StatePathError) as exc:
            raise SemanticCertificationBlocked(
                f"durable semantic certification binding is invalid: {exc}"
            ) from exc
        if (
            binding.repo_uuid != repo_uuid
            or binding.generation_id != generation_id
            or binding.request_sha256 != request_digest
            or binding.view.sealed_input_manifest_sha256 != sealed_digest
        ):
            raise SemanticCertificationBlocked(
                "durable semantic certification binding differs from the request"
            )
        return binding.view

    def certification_binding_locked(
        self,
        operation: LeaseOperation,
        *,
        generation_id: str,
        request_sha256: str,
        sealed_input_manifest_sha256: str,
    ) -> SemanticCertificationView | None:
        """Read and validate immutable prior queue authority under the workspace lock."""

        return self._certification_binding_from_state(
            self.state,
            operation.repo_uuid,
            generation_id=generation_id,
            request_sha256=request_sha256,
            sealed_input_manifest_sha256=sealed_input_manifest_sha256,
        )

    @classmethod
    def verify_certification_binding_at(
        cls,
        state: DurableStateRoot,
        repo_uuid: str,
        *,
        generation_id: str,
        request_sha256: str,
        sealed_input_manifest_sha256: str,
    ) -> SemanticCertificationView:
        """Require immutable queue authority through a reopened durable state root."""

        view = cls._certification_binding_from_state(
            state,
            repo_uuid,
            generation_id=generation_id,
            request_sha256=request_sha256,
            sealed_input_manifest_sha256=sealed_input_manifest_sha256,
        )
        if view is None:
            raise SemanticCertificationBlocked(
                "durable semantic certification binding is missing"
            )
        return view

    def ensure_certification_binding_locked(
        self,
        operation: LeaseOperation,
        *,
        generation_id: str,
        request_sha256: str,
        view: SemanticCertificationView,
    ) -> SemanticCertificationView:
        """Install queue-view provenance before any staged receipt becomes authority."""

        binding = _SemanticCertificationBinding.create(
            repo_uuid=operation.repo_uuid,
            generation_id=generation_id,
            request_sha256=request_sha256,
            view=view,
        )
        path = self._certification_binding_path(operation.repo_uuid, generation_id)
        try:
            self.state.install_once_bytes(
                path,
                binding.canonical,
                label=f"semantic_certification:{generation_id}",
            )
            installed = _SemanticCertificationBinding.from_json(
                self.state.read_existing_bytes(path)
            )
        except (ContractError, StateCorrupt, StatePathError) as exc:
            raise SemanticCertificationBlocked(
                f"durable semantic certification binding failed: {exc}"
            ) from exc
        if installed != binding:
            raise SemanticCertificationBlocked(
                "durable semantic certification binding differs from the stable queue view"
            )
        return installed.view

    @staticmethod
    def _view_from_snapshot(
        snapshot: SemanticQueueSnapshot,
        *,
        source_epoch: int,
        source_observations: _SemanticObservationEvidence,
        sealed_input_manifest_sha256: str,
    ) -> SemanticCertificationView:
        source_epoch = _integer(source_epoch, "$.source_epoch", minimum=1)
        sealed_input_manifest_sha256 = _digest(
            sealed_input_manifest_sha256,
            "$.sealed_input_manifest_sha256",
        )
        reconciliation = snapshot.reconciliation
        if reconciliation is None:
            raise SemanticCertificationBlocked(
                "exact reconciliation is required; queue emptiness is insufficient"
            )
        if (
            reconciliation.source_epoch != source_epoch
            or reconciliation.source_observations != source_observations
        ):
            raise SemanticCertificationBlocked(
                "reconciliation does not match source epoch, policy, and observation"
            )
        if reconciliation.sealed_input_manifest_sha256 is None:
            raise SemanticCertificationBlocked(
                "completed queue watermark is not bound to staged inputs"
            )
        if reconciliation.sealed_input_manifest_sha256 != sealed_input_manifest_sha256:
            raise SemanticCertificationBlocked(
                "staged inputs differ from the queue watermark binding"
            )
        if reconciliation.desired_watermark != snapshot.desired_watermark:
            raise SemanticCertificationBlocked("reconciliation queue watermark is stale")
        if snapshot.completed_watermark != snapshot.desired_watermark:
            raise SemanticCertificationBlocked("semantic work is not complete at the watermark")
        if any(item.status != "completed" for item in snapshot.items):
            raise SemanticCertificationBlocked("queue retains non-complete desired work")
        completeness = "complete" if reconciliation.semantic_required else "not_required"
        return SemanticCertificationView(
            repo_uuid=snapshot.repo_uuid,
            queue_revision=snapshot.revision,
            queue_state_sha256=snapshot.sha256,
            queue_watermark=snapshot.desired_watermark,
            completed_watermark=snapshot.completed_watermark,
            compaction_epoch=snapshot.compaction_epoch,
            source_epoch=source_epoch,
            source_commit=source_observations.source_commit,
            policy_sha256=source_observations.policy_sha256,
            observation_manifest_sha256=source_observations.inventory_sha256,
            observation_evidence_sha256=source_observations.evidence_sha256,
            sealed_input_manifest_sha256=sealed_input_manifest_sha256,
            semantic_completeness=completeness,
        )

    def certification_view(
        self,
        grant: LeaseGrant,
        *,
        source_epoch: int,
        source_observations: Sequence[SourceObservation],
        sealed_input_manifest_sha256: str,
        monotonic_ns: int,
    ) -> SemanticCertificationView:
        try:
            evidence = _SemanticObservationEvidence.create(source_observations)
        except ContractError as exc:
            raise SemanticCertificationBlocked(str(exc)) from exc
        with self.leases.current_operation(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD", "MIGRATE"}),
        ) as operation:
            snapshot = self._load_locked(operation.repo_uuid)
            if snapshot.active_source_revision != operation.grant.active_source_revision:
                raise SemanticCertificationBlocked(
                    "active source changed; exact reconciliation is required"
                )
            return self._view_from_snapshot(
                snapshot,
                source_epoch=source_epoch,
                source_observations=evidence,
                sealed_input_manifest_sha256=sealed_input_manifest_sha256,
            )

    def assert_certification_view_locked(
        self,
        operation: LeaseOperation,
        view: SemanticCertificationView,
    ) -> None:
        """Revalidate a captured view while GenerationStore holds the workspace lock."""

        snapshot = self._load_locked(operation.repo_uuid)
        if snapshot.active_source_revision != operation.grant.active_source_revision:
            raise SemanticCertificationBlocked(
                "active source changed; exact reconciliation is required"
            )
        if snapshot.revision != view.queue_revision or snapshot.sha256 != view.queue_state_sha256:
            raise SemanticCertificationBlocked("queue view changed before certification")
        reconciliation = snapshot.reconciliation
        if reconciliation is None:
            raise SemanticCertificationBlocked("queue view changed before certification")
        current = self._view_from_snapshot(
            snapshot,
            source_epoch=view.source_epoch,
            source_observations=reconciliation.source_observations,
            sealed_input_manifest_sha256=view.sealed_input_manifest_sha256,
        )
        if current != view:
            raise SemanticCertificationBlocked("queue view changed before certification")


__all__ = [
    "SemanticCapabilityDecision",
    "SemanticCapabilityUnavailable",
    "SemanticCertificationBlocked",
    "SemanticCertificationView",
    "SemanticClaim",
    "SemanticDesiredWork",
    "SemanticQueueCapacityExceeded",
    "SemanticQueueConflict",
    "SemanticQueueCorrupt",
    "SemanticQueueError",
    "SemanticQueueItem",
    "SemanticQueuePolicy",
    "SemanticQueueSnapshot",
    "SemanticQueueStore",
    "StaleSemanticClaim",
    "decide_semantic_capability",
]
