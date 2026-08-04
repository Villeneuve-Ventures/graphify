"""Internal semantic-result handoff ownership and sealed-input evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING, Mapping, Sequence, cast
import uuid

from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    ContractError,
    StagedBuildState,
    StructuralBuildRequest,
    WorkspaceLeaseState,
    canonical_json_bytes,
    payload_manifest_sha256,
)
from graphify.workspace.generations import (
    CapacityExceeded,
    GenerationStore,
    StagedBuildCompletion,
    StagedBuildPreparation,
)
from graphify.workspace.leases import LeaseGrant, LeaseOperation, LeaseRecoveryRequired, LeaseStore
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    FaultHook,
    InjectedFault,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
    require_before_deadline,
)
from graphify.workspace.semantic_queue import (
    SemanticDesiredWork,
    SemanticQueuePolicy,
    SemanticQueueSnapshot,
    SemanticQueueStore,
    _SemanticReconciliation,
)
from graphify.workspace.semantic_worker import (
    COMPLETE_MAX_BYTES,
    RESULT_MAX_BYTES,
    SemanticResultInvalid,
    canonical_protocol_bytes,
    canonical_result_bytes,
    parse_request_frame,
    parse_result_binding,
    parse_result_frame,
    sha256,
)

if TYPE_CHECKING:
    from graphify.workspace.sync import SyncRequest


HANDOFF_CONTRACT = "graphify.workspace.semantic_result_handoff.internal"
HANDOFF_FORMAT_VERSION = 1
SEMANTIC_INPUT_PATH = "graphify-out/semantic-inputs.json"
MAX_SESSION_FRAMES = 10
MAX_CHECKPOINT_FRAMES = 8

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_GENERATION_RE = re.compile(r"^gen-[a-z0-9][a-z0-9._-]{0,62}$", re.ASCII)
_ORIGINS = frozenset({"fresh_worker_session", "carried_current_generation"})
_RESULT_FIELDS = frozenset(
    {
        "origin",
        "begin_request",
        "begin_request_sha256",
        "session",
        "result_binding",
        "result_binding_bytes",
        "result_binding_sha256",
    }
)
_SESSION_FIELDS = frozenset({"frames", "stdout_bytes", "stdout_sha256", "process_exit_code"})
_MATERIALIZED_FIELDS = frozenset(
    {
        "work",
        "work_sha256",
        "payload",
        "payload_bytes",
        "payload_sha256",
        "result_binding_sha256",
    }
)
_QUEUE_FIELDS = frozenset(
    {
        "active_source_revision",
        "revision",
        "canonical_state_sha256",
        "completed_watermark",
        "desired_watermark",
        "compaction_epoch",
        "queue_policy",
        "reconciliation",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "contract",
        "format_version",
        "repo_uuid",
        "target_generation_id",
        "carried_source_generation_id",
        "structural_request",
        "structural_request_sha256",
        "queue",
        "results",
        "materialized",
    }
)


class SemanticHandoffError(RuntimeError):
    """Base class for stable, redacted internal handoff failures."""

    code = "semantic_handoff_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class SemanticHandoffInvalid(SemanticHandoffError):
    code = "semantic_handoff_invalid"


class SemanticHandoffConflict(SemanticHandoffError):
    code = "semantic_handoff_conflict"


class SemanticHandoffCommitUnknown(CommitUnknown):
    code = "semantic_handoff_commit_unknown"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class FreshWorkerSessionEvidence:
    """Captured canonical worker input/output and the observed process exit."""

    begin_request_bytes: bytes
    stdout_bytes: bytes
    process_exit_code: int


@dataclass(frozen=True)
class CarriedSemanticResultEvidence:
    """Select one desired identity from the verified current generation."""

    work: SemanticDesiredWork


SemanticResultEvidence = FreshWorkerSessionEvidence | CarriedSemanticResultEvidence


@dataclass(frozen=True)
class SemanticResultHandoff:
    """One parsed immutable format-version-1 semantic handoff."""

    value: Mapping[str, object]
    canonical: bytes

    @property
    def sha256(self) -> str:
        return sha256(self.canonical)

    @property
    def repo_uuid(self) -> str:
        return cast(str, self.value["repo_uuid"])

    @property
    def target_generation_id(self) -> str:
        return cast(str, self.value["target_generation_id"])

    @property
    def carried_source_generation_id(self) -> str | None:
        return cast(str | None, self.value["carried_source_generation_id"])

    @property
    def structural_request(self) -> StructuralBuildRequest:
        return StructuralBuildRequest.from_mapping(
            cast(Mapping[str, object], self.value["structural_request"])
        )

    @property
    def queue(self) -> Mapping[str, object]:
        return cast(Mapping[str, object], self.value["queue"])

    @property
    def results(self) -> tuple[Mapping[str, object], ...]:
        return tuple(cast(list[Mapping[str, object]], self.value["results"]))


@dataclass(frozen=True)
class SemanticHandoffCapture:
    """Installed handoff plus the exact queue/staged recovery snapshot."""

    handoff: SemanticResultHandoff
    pre_bind_queue: SemanticQueueSnapshot
    staged_state: StagedBuildState | None


@dataclass(frozen=True)
class SemanticResultFinalization:
    """Redacted internal terminal proof; this is not a public receipt."""

    repo_uuid: str
    target_generation_id: str
    carried_source_generation_id: str | None
    handoff_sha256: str
    payload_manifest_sha256: str
    staged_revision: int
    queue_revision: int
    queue_sha256: str


@dataclass(frozen=True)
class _ParsedSession:
    begin: Mapping[str, object]
    begin_canonical: bytes
    frames: tuple[Mapping[str, object], ...]
    stdout_canonical: bytes
    process_exit_code: int

    @property
    def begin_sha256(self) -> str:
        return sha256(self.begin_canonical)


@dataclass(frozen=True)
class _PreparedHandoffEvidence:
    existing: bytes | None
    retained: SemanticResultHandoff | None
    fresh_entries: tuple[Mapping[str, object], ...]
    carried_works: tuple[SemanticDesiredWork, ...]
    carried_source_generation_id: str | None
    carried_source_receipt_sha256: str | None
    carried_source_handoff: SemanticResultHandoff | None
    pointer: Mapping[str, object] | None


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticHandoffInvalid("canonical JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise SemanticHandoffInvalid("canonical JSON contains a non-finite number")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SemanticHandoffInvalid(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise SemanticHandoffInvalid(f"{label} field set is invalid")


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or cast(int, value) < minimum:
        raise SemanticHandoffInvalid(f"{label} is invalid")
    return cast(int, value)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SemanticHandoffInvalid(f"{label} is invalid")
    return value


def _generation_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _GENERATION_RE.fullmatch(value) is None:
        raise SemanticHandoffInvalid(f"{label} is invalid")
    return value


def _repo_uuid(value: object) -> str:
    try:
        return WorkspaceLeaseState.canonical_repo_uuid(value)
    except ContractError as exc:
        raise SemanticHandoffInvalid("handoff repository identity is invalid") from exc


def _protocol_object(value: Mapping[str, object], label: str) -> bytes:
    try:
        return canonical_protocol_bytes(value)
    except (RecursionError, TypeError, ValueError) as exc:
        raise SemanticHandoffInvalid(f"{label} is not canonically encodable") from exc


def _parse_protocol_json(raw: bytes, *, maximum: int, label: str) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
        raise SemanticHandoffInvalid(f"{label} exceeds its byte limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except SemanticHandoffInvalid:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise SemanticHandoffInvalid(f"{label} is not valid canonical JSON") from exc
    parsed = _mapping(value, label)
    if _protocol_object(parsed, label) != raw:
        raise SemanticHandoffInvalid(f"{label} is not canonical")
    return parsed


def _parse_session(evidence: FreshWorkerSessionEvidence) -> _ParsedSession:
    if type(evidence.process_exit_code) is not int or evidence.process_exit_code != 0:
        raise SemanticHandoffInvalid("worker session did not exit exactly zero")
    try:
        begin = parse_request_frame(evidence.begin_request_bytes)
    except Exception as exc:
        raise SemanticHandoffInvalid("worker begin request is invalid") from exc
    if begin.action != "begin":
        raise SemanticHandoffInvalid("worker session does not begin with a begin request")
    maximum_stdout = MAX_SESSION_FRAMES * RESULT_MAX_BYTES
    if (
        not evidence.stdout_bytes
        or len(evidence.stdout_bytes) > maximum_stdout
        or not evidence.stdout_bytes.endswith(b"\n")
    ):
        raise SemanticHandoffInvalid("worker stdout transcript is incomplete or oversized")
    lines = evidence.stdout_bytes.splitlines(keepends=True)
    if not 2 <= len(lines) <= MAX_SESSION_FRAMES or b"".join(lines) != evidence.stdout_bytes:
        raise SemanticHandoffInvalid("worker stdout frame count is invalid")
    try:
        frames = tuple(parse_result_frame(line) for line in lines)
    except Exception as exc:
        raise SemanticHandoffInvalid("worker stdout contains an invalid result frame") from exc
    if frames[0].get("kind") != "work":
        raise SemanticHandoffInvalid("worker stdout must begin with one work frame")
    checkpoints = frames[1:-1]
    if len(checkpoints) > MAX_CHECKPOINT_FRAMES or any(
        frame.get("kind") != "checkpointed" for frame in checkpoints
    ):
        raise SemanticHandoffInvalid("worker stdout checkpoint sequence is invalid")
    terminal = frames[-1]
    terminals = [frame for frame in frames if frame.get("kind") == "terminal"]
    if (
        terminals != [terminal]
        or terminal.get("outcome") != "completed"
        or terminal.get("exit_code") != 0
    ):
        raise SemanticHandoffInvalid("worker stdout lacks one final completed terminal")
    return _ParsedSession(
        begin=begin.to_dict(),
        begin_canonical=begin.canonical,
        frames=frames,
        stdout_canonical=evidence.stdout_bytes,
        process_exit_code=evidence.process_exit_code,
    )


def _work_from_result_entry(entry: Mapping[str, object]) -> SemanticDesiredWork:
    binding = _mapping(entry["result_binding"], "result binding")
    try:
        return SemanticDesiredWork.from_mapping(cast(Mapping[str, object], binding["work"]))
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise SemanticHandoffInvalid("result entry desired work is invalid") from exc


def _result_sort_key(entry: Mapping[str, object]) -> tuple[bytes, int, str, str, str]:
    work = _work_from_result_entry(entry)
    return (
        work.path.encode("utf-8"),
        work.desired_revision,
        work.operation,
        work.content_sha256,
        cast(str, entry["result_binding_sha256"]),
    )


def _validate_result_entry(
    raw: object,
    *,
    repo_uuid: str,
    active_source_revision: int,
    migration_epoch: int,
    expected_operation_epoch: int,
    expected_registry_revision: int,
    queue_revision: int,
    desired_watermark: int,
    completed_watermark: int,
    reconciliation: _SemanticReconciliation,
) -> dict[str, object]:
    entry = _mapping(raw, "result entry")
    _exact_fields(entry, _RESULT_FIELDS, "result entry")
    origin = entry.get("origin")
    if origin not in _ORIGINS:
        raise SemanticHandoffInvalid("result entry origin is invalid")
    begin_value = _mapping(entry.get("begin_request"), "result begin request")
    begin_canonical = _protocol_object(begin_value, "result begin request")
    try:
        begin = parse_request_frame(begin_canonical)
    except Exception as exc:
        raise SemanticHandoffInvalid("result begin request is invalid") from exc
    if begin.action != "begin":
        raise SemanticHandoffInvalid("result begin request action is invalid")
    begin_sha256 = _digest(entry.get("begin_request_sha256"), "begin request digest")
    if begin_sha256 != sha256(begin_canonical):
        raise SemanticHandoffInvalid("result begin request digest differs")

    session = _mapping(entry.get("session"), "result session")
    _exact_fields(session, _SESSION_FIELDS, "result session")
    raw_frames = session.get("frames")
    if not isinstance(raw_frames, list) or not 2 <= len(raw_frames) <= MAX_SESSION_FRAMES:
        raise SemanticHandoffInvalid("result session frame count is invalid")
    frames: list[Mapping[str, object]] = []
    encoded_frames: list[bytes] = []
    for raw_frame in raw_frames:
        frame = _mapping(raw_frame, "result session frame")
        try:
            encoded = canonical_result_bytes(frame)
            parsed = parse_result_frame(encoded)
        except Exception as exc:
            raise SemanticHandoffInvalid("result session frame is invalid") from exc
        if parsed != frame:
            raise SemanticHandoffInvalid("result session frame changed during validation")
        frames.append(frame)
        encoded_frames.append(encoded)
    transcript = b"".join(encoded_frames)
    if _integer(session.get("stdout_bytes"), "result stdout byte count", minimum=1) != len(
        transcript
    ):
        raise SemanticHandoffInvalid("result stdout byte count differs")
    if _digest(session.get("stdout_sha256"), "result stdout digest") != sha256(transcript):
        raise SemanticHandoffInvalid("result stdout digest differs")
    if _integer(session.get("process_exit_code"), "result process exit") != 0:
        raise SemanticHandoffInvalid("result process exit is not zero")
    if frames[0].get("kind") != "work":
        raise SemanticHandoffInvalid("result session must begin with work")
    checkpoints = frames[1:-1]
    if len(checkpoints) > MAX_CHECKPOINT_FRAMES or any(
        frame.get("kind") != "checkpointed" for frame in checkpoints
    ):
        raise SemanticHandoffInvalid("result checkpoint sequence is invalid")
    terminal = frames[-1]
    terminal_frames = [frame for frame in frames if frame.get("kind") == "terminal"]
    if (
        terminal_frames != [terminal]
        or terminal.get("outcome") != "completed"
        or terminal.get("exit_code") != 0
    ):
        raise SemanticHandoffInvalid("result session terminal is invalid")

    binding_value = _mapping(entry.get("result_binding"), "result binding")
    binding_canonical = _protocol_object(binding_value, "result binding")
    try:
        binding = parse_result_binding(binding_canonical)
    except SemanticResultInvalid as exc:
        raise SemanticHandoffInvalid("result binding is invalid") from exc
    binding_bytes = _integer(
        entry.get("result_binding_bytes"),
        "result binding byte count",
        minimum=1,
    )
    binding_sha256 = _digest(
        entry.get("result_binding_sha256"),
        "result binding digest",
    )
    if binding_bytes != len(binding_canonical) or binding_sha256 != sha256(binding_canonical):
        raise SemanticHandoffInvalid("result binding byte metadata differs")
    binding_data = binding.value
    work = SemanticDesiredWork.from_mapping(cast(Mapping[str, object], binding_data["work"]))
    work_sha256 = sha256(canonical_protocol_bytes(work.to_dict()))
    work_frame = frames[0]
    begin_data = begin.value
    common = (
        begin_data.get("repo_uuid"),
        work_frame.get("repo_uuid"),
        terminal.get("repo_uuid"),
        binding_data.get("repo_uuid"),
    )
    if common != (repo_uuid, repo_uuid, repo_uuid, repo_uuid):
        raise SemanticHandoffInvalid("result repository bindings differ")
    if (
        any(frame.get("begin_request_sha256") != begin_sha256 for frame in frames)
        or binding_data.get("begin_request_sha256") != begin_sha256
    ):
        raise SemanticHandoffInvalid("result begin bindings differ")
    claim_id = work_frame.get("claim_id")
    if (
        any(frame.get("claim_id") != claim_id for frame in frames)
        or binding_data.get("claim_id") != claim_id
    ):
        raise SemanticHandoffInvalid("result claim bindings differ")
    if (
        work_frame.get("work") != work.to_dict()
        or work_frame.get("work_sha256") != work_sha256
        or terminal.get("work_sha256") != work_sha256
        or binding_data.get("work_sha256") != work_sha256
    ):
        raise SemanticHandoffInvalid("result desired-work bindings differ")
    if work_frame.get("attempt") != binding_data.get("attempt") or terminal.get(
        "attempt"
    ) != binding_data.get("attempt"):
        raise SemanticHandoffInvalid("result attempt bindings differ")
    payload = _mapping(binding_data.get("payload"), "result payload")
    if (
        terminal.get("payload_kind") != payload.get("kind")
        or terminal.get("payload_bytes") != binding_data.get("payload_bytes")
        or terminal.get("payload_sha256") != binding_data.get("payload_sha256")
        or terminal.get("result_binding_bytes") != binding_bytes
        or terminal.get("result_binding_sha256") != binding_sha256
    ):
        raise SemanticHandoffInvalid("result payload or envelope bindings differ")
    begin_operation_epoch = _integer(
        begin_data.get("expected_operation_epoch"),
        "begin operation epoch",
        minimum=1,
    )
    if (
        binding_data.get("active_source_revision") != active_source_revision
        or binding_data.get("migration_epoch") != migration_epoch
        or begin_data.get("expected_active_source_revision") != active_source_revision
        or begin_data.get("expected_migration_epoch") != migration_epoch
        or begin_operation_epoch + 1 != binding_data.get("operation_epoch")
        or work.source_epoch != reconciliation.source_epoch
        or work.policy_sha256 != reconciliation.policy_sha256
    ):
        raise SemanticHandoffInvalid("result current source authority differs")
    if _integer(terminal.get("queue_revision"), "terminal queue revision") > queue_revision:
        raise SemanticHandoffInvalid("terminal queue revision exceeds captured queue")
    if (
        _integer(terminal.get("completed_watermark"), "terminal completed watermark")
        > completed_watermark
    ):
        raise SemanticHandoffInvalid("terminal watermark exceeds captured queue")
    if _integer(begin_data.get("expected_queue_revision"), "begin queue revision") > cast(
        int, terminal["queue_revision"]
    ):
        raise SemanticHandoffInvalid("begin queue revision exceeds its terminal")
    if _integer(begin_data.get("expected_registry_revision"), "begin registry revision") > (
        expected_registry_revision
    ):
        raise SemanticHandoffInvalid("begin registry revision exceeds captured authority")
    if origin == "fresh_worker_session" and (
        begin_data.get("expected_desired_watermark") != desired_watermark
        or _integer(
            binding_data.get("operation_epoch"),
            "result binding operation epoch",
            minimum=1,
        )
        > expected_operation_epoch
    ):
        raise SemanticHandoffInvalid("fresh result coordinates differ from captured authority")
    return {
        "origin": origin,
        "begin_request": deepcopy(dict(begin.value)),
        "begin_request_sha256": begin_sha256,
        "session": {
            "frames": [deepcopy(dict(frame)) for frame in frames],
            "stdout_bytes": len(transcript),
            "stdout_sha256": sha256(transcript),
            "process_exit_code": 0,
        },
        "result_binding": deepcopy(dict(binding.value)),
        "result_binding_bytes": binding_bytes,
        "result_binding_sha256": binding_sha256,
    }


def _materialize(results: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    slots: dict[str, dict[str, object]] = {}
    prior_revision: dict[str, int] = {}
    for entry in results:
        work = _work_from_result_entry(entry)
        previous = prior_revision.get(work.path)
        if previous is not None and work.desired_revision <= previous:
            raise SemanticHandoffInvalid("same-path desired revisions are not ascending")
        prior_revision[work.path] = work.desired_revision
        binding = _mapping(entry["result_binding"], "result binding")
        payload = _mapping(binding["payload"], "result payload")
        kind = payload.get("kind")
        if work.operation == "UPSERT":
            if kind != "semantic_fragment":
                raise SemanticHandoffInvalid("UPSERT result payload kind differs")
            slots[work.path] = {
                "work": work.to_dict(),
                "work_sha256": cast(str, binding["work_sha256"]),
                "payload": deepcopy(dict(payload)),
                "payload_bytes": cast(int, binding["payload_bytes"]),
                "payload_sha256": cast(str, binding["payload_sha256"]),
                "result_binding_sha256": cast(str, entry["result_binding_sha256"]),
            }
            continue
        if work.operation != "DELETE" or dict(payload) != {"kind": "delete_tombstone"}:
            raise SemanticHandoffInvalid("DELETE result payload kind differs")
        slots.pop(work.path, None)
    return [slots[path] for path in sorted(slots, key=lambda item: item.encode("utf-8"))]


def _validate_queue_binding(value: object) -> tuple[dict[str, object], _SemanticReconciliation]:
    queue = _mapping(value, "handoff queue binding")
    _exact_fields(queue, _QUEUE_FIELDS, "handoff queue binding")
    active_source_revision = _integer(
        queue.get("active_source_revision"),
        "queue active-source revision",
        minimum=1,
    )
    revision = _integer(queue.get("revision"), "queue revision", minimum=1)
    canonical_state_sha256 = _digest(
        queue.get("canonical_state_sha256"),
        "queue canonical-state digest",
    )
    completed = _integer(queue.get("completed_watermark"), "queue completed watermark")
    desired = _integer(queue.get("desired_watermark"), "queue desired watermark", minimum=1)
    if completed != desired:
        raise SemanticHandoffInvalid("handoff queue watermarks are incomplete")
    compaction = _integer(queue.get("compaction_epoch"), "queue compaction epoch")
    try:
        policy = SemanticQueuePolicy.from_mapping(
            cast(Mapping[str, object], _mapping(queue.get("queue_policy"), "queue policy"))
        )
        reconciliation = _SemanticReconciliation.from_mapping(
            cast(
                Mapping[str, object],
                _mapping(queue.get("reconciliation"), "queue reconciliation"),
            )
        )
    except (ContractError, TypeError, ValueError) as exc:
        raise SemanticHandoffInvalid("handoff queue authority is invalid") from exc
    if (
        not reconciliation.semantic_required
        or reconciliation.desired_watermark != desired
        or reconciliation.sealed_input_manifest_sha256 is not None
    ):
        raise SemanticHandoffInvalid("handoff reconciliation is not unsealed semantic work")
    return (
        {
            "active_source_revision": active_source_revision,
            "revision": revision,
            "canonical_state_sha256": canonical_state_sha256,
            "completed_watermark": completed,
            "desired_watermark": desired,
            "compaction_epoch": compaction,
            "queue_policy": policy.to_dict(),
            "reconciliation": reconciliation.to_dict(),
        },
        reconciliation,
    )


def parse_semantic_result_handoff(
    raw: bytes,
    *,
    max_bytes: int,
) -> SemanticResultHandoff:
    """Parse and fully revalidate one closed format-version-1 handoff."""

    if max_bytes < 1:
        raise SemanticHandoffInvalid("handoff read bound must be positive")
    value = _parse_protocol_json(raw, maximum=max_bytes, label="semantic handoff")
    _exact_fields(value, _TOP_LEVEL_FIELDS, "semantic handoff")
    if value.get("contract") != HANDOFF_CONTRACT:
        raise SemanticHandoffInvalid("semantic handoff contract is unsupported")
    if (
        value.get("format_version") != HANDOFF_FORMAT_VERSION
        or type(value.get("format_version")) is not int
    ):
        raise SemanticHandoffInvalid("semantic handoff version is unsupported")
    repo_uuid = _repo_uuid(value.get("repo_uuid"))
    target = _generation_id(value.get("target_generation_id"), "target generation")
    carried_raw = value.get("carried_source_generation_id")
    carried = None if carried_raw is None else _generation_id(carried_raw, "carried source")
    if carried == target:
        raise SemanticHandoffInvalid("carried source and target generations are equal")
    try:
        structural = StructuralBuildRequest.from_mapping(
            cast(
                Mapping[str, object],
                _mapping(value.get("structural_request"), "structural request"),
            )
        )
    except (ContractError, TypeError, ValueError) as exc:
        raise SemanticHandoffInvalid("handoff structural request is invalid") from exc
    structural_sha256 = _digest(
        value.get("structural_request_sha256"),
        "structural request digest",
    )
    if structural.sha256 != structural_sha256:
        raise SemanticHandoffInvalid("handoff structural request digest differs")
    queue, reconciliation = _validate_queue_binding(value.get("queue"))
    results_raw = value.get("results")
    if not isinstance(results_raw, list) or not results_raw:
        raise SemanticHandoffInvalid("handoff results must be a nonempty array")
    results = [
        _validate_result_entry(
            item,
            repo_uuid=repo_uuid,
            active_source_revision=cast(int, queue["active_source_revision"]),
            migration_epoch=structural.expected_migration_epoch,
            expected_operation_epoch=structural.expected_operation_epoch,
            expected_registry_revision=structural.expected_registry_revision,
            queue_revision=cast(int, queue["revision"]),
            desired_watermark=cast(int, queue["desired_watermark"]),
            completed_watermark=cast(int, queue["completed_watermark"]),
            reconciliation=reconciliation,
        )
        for item in results_raw
    ]
    if results != sorted(results, key=_result_sort_key):
        raise SemanticHandoffInvalid("handoff results are not deterministically ordered")
    works = [_work_from_result_entry(entry) for entry in results]
    desired_identities = {work.identity for work in reconciliation.desired}
    result_identities = [work.identity for work in works]
    if (
        len(result_identities) != len(set(result_identities))
        or set(result_identities) != desired_identities
        or len(result_identities) != len(reconciliation.desired)
    ):
        raise SemanticHandoffInvalid("handoff results are not a bijection with desired work")
    carried_origins = [
        entry for entry in results if entry["origin"] == "carried_current_generation"
    ]
    if bool(carried_origins) != (carried is not None):
        raise SemanticHandoffInvalid("carried-source identity and result origins differ")
    materialized = _materialize(results)
    raw_materialized = value.get("materialized")
    if not isinstance(raw_materialized, list):
        raise SemanticHandoffInvalid("handoff materialized value must be an array")
    for item in raw_materialized:
        _exact_fields(
            _mapping(item, "materialized entry"), _MATERIALIZED_FIELDS, "materialized entry"
        )
    if canonical_protocol_bytes(raw_materialized) != canonical_protocol_bytes(materialized):
        raise SemanticHandoffInvalid("handoff materialized value differs from recomputation")
    normalized: dict[str, object] = {
        "contract": HANDOFF_CONTRACT,
        "format_version": HANDOFF_FORMAT_VERSION,
        "repo_uuid": repo_uuid,
        "target_generation_id": target,
        "carried_source_generation_id": carried,
        "structural_request": structural.to_dict(),
        "structural_request_sha256": structural_sha256,
        "queue": queue,
        "results": results,
        "materialized": materialized,
    }
    canonical = canonical_protocol_bytes(normalized)
    if canonical != raw:
        raise SemanticHandoffInvalid("semantic handoff is not canonical")
    if len(canonical) > structural.expected_payload_bytes:
        raise SemanticHandoffInvalid("semantic handoff exceeds the structural reservation")
    return SemanticResultHandoff(value=normalized, canonical=canonical)


def _queue_binding(snapshot: SemanticQueueSnapshot) -> dict[str, object]:
    reconciliation = snapshot.reconciliation
    if reconciliation is None:
        raise SemanticHandoffConflict("exact semantic reconciliation is missing")
    if reconciliation.sealed_input_manifest_sha256 is not None:
        raise SemanticHandoffConflict("new semantic handoff authority is already sealed")
    return {
        "active_source_revision": snapshot.active_source_revision,
        "revision": snapshot.revision,
        "canonical_state_sha256": snapshot.sha256,
        "completed_watermark": snapshot.completed_watermark,
        "desired_watermark": snapshot.desired_watermark,
        "compaction_epoch": snapshot.compaction_epoch,
        "queue_policy": snapshot.queue_policy.to_dict(),
        "reconciliation": reconciliation.to_dict(),
    }


def _queue_from_existing_binding(
    current: SemanticQueueSnapshot,
    binding: Mapping[str, object],
) -> SemanticQueueSnapshot:
    if (
        current.revision == binding["revision"]
        and current.sha256 == binding["canonical_state_sha256"]
        and _queue_binding(current) == dict(binding)
    ):
        return current
    reconciliation = current.reconciliation
    if reconciliation is None or reconciliation.sealed_input_manifest_sha256 is None:
        raise SemanticHandoffConflict("current queue differs from retained handoff authority")
    pre_bind = replace(
        current,
        revision=cast(int, binding["revision"]),
        reconciliation=replace(reconciliation, sealed_input_manifest_sha256=None),
    )
    if current.revision != cast(int, binding["revision"]) + 1:
        raise SemanticHandoffConflict("current queue advanced beyond retained handoff authority")
    if pre_bind.sha256 != binding["canonical_state_sha256"]:
        raise SemanticHandoffConflict("current queue is not the deterministic post-bind state")
    if _queue_binding(pre_bind) != dict(binding):
        raise SemanticHandoffConflict("retained handoff queue binding is incomplete")
    return pre_bind


def _source_observation_evidence(
    structural_request: StructuralBuildRequest,
) -> dict[str, object]:
    document = structural_request.source_observation_document()
    observations = [document, document]
    return {
        "observations": observations,
        "evidence_sha256": hashlib.sha256(canonical_json_bytes(observations)).hexdigest(),
    }


class SemanticResultHandoffStore:
    """Own immutable handoff records and their exact generation-owned copies."""

    def __init__(
        self,
        state_root: Path,
        leases: LeaseStore,
        generations: GenerationStore,
        semantic_queue: SemanticQueueStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.leases = leases
        self.generations = generations
        self.semantic_queue = semantic_queue
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        roots = {
            self.state.root,
            leases.state.root,
            generations.state.root,
            semantic_queue.state.root,
        }
        if (
            len(roots) != 1
            or semantic_queue.leases is not leases
            or generations.leases is not leases
        ):
            raise SemanticHandoffInvalid("handoff stores do not share one authority root")
        self.fault_hook = fault_hook or (lambda _event: None)

    @staticmethod
    def _relative_path(
        repo_uuid: str,
        target_generation_id: str,
        structural_request_sha256: str,
    ) -> Path:
        return (
            LeaseStore._directory(repo_uuid)
            / "semantic-staging"
            / "handoffs"
            / target_generation_id
            / f"{structural_request_sha256}.json"
        )

    @classmethod
    def relative_path_for(cls, handoff: SemanticResultHandoff) -> Path:
        return cls._relative_path(
            handoff.repo_uuid,
            handoff.target_generation_id,
            handoff.structural_request.sha256,
        )

    @staticmethod
    def _generation_copy_relative(handoff: SemanticResultHandoff) -> Path:
        return (
            LeaseStore._directory(handoff.repo_uuid)
            / "staging"
            / handoff.target_generation_id
            / SEMANTIC_INPUT_PATH
        )

    def _read_optional_handoff(
        self,
        relative: Path,
        *,
        maximum: int,
        deadline_ns: int | None = None,
    ) -> bytes | None:
        try:
            return self.state.read_optional_existing_bytes(
                relative,
                max_bytes=maximum,
                deadline_ns=deadline_ns,
            )
        except (StateCorrupt, StatePathError) as exc:
            raise SemanticHandoffConflict("semantic handoff path is unsafe or unreadable") from exc
        except Exception as exc:
            raise SemanticHandoffCommitUnknown("semantic handoff read is ambiguous") from exc

    def _read_exact_handoff_directory(
        self,
        relative: Path,
        *,
        maximum: int,
        deadline_ns: int | None = None,
    ) -> bytes | None:
        parent = relative.parent
        parent_path = self.state.path(parent)
        try:
            if not self.state.private_directory_exists(parent):
                observed = self._read_optional_handoff(
                    relative,
                    maximum=maximum,
                    deadline_ns=deadline_ns,
                )
                if observed is not None:
                    raise SemanticHandoffCommitUnknown(
                        "semantic handoff directory appeared during inspection"
                    )
                return None
            with self.state.existing_private_directory(parent) as descriptor:
                before = tuple(
                    self.state._tree_entry_names_descriptor(
                        descriptor,
                        parent_path,
                        deadline_ns=deadline_ns,
                        maximum_entries=2,
                    )
                )
            if before not in {(), (relative.name,)}:
                raise SemanticHandoffConflict("semantic handoff directory contains an extra entry")
            observed = self._read_optional_handoff(
                relative,
                maximum=maximum,
                deadline_ns=deadline_ns,
            )
            with self.state.existing_private_directory(parent) as descriptor:
                after = tuple(
                    self.state._tree_entry_names_descriptor(
                        descriptor,
                        parent_path,
                        deadline_ns=deadline_ns,
                        maximum_entries=2,
                    )
                )
            if after != before:
                raise SemanticHandoffCommitUnknown(
                    "semantic handoff directory changed during inspection"
                )
            if (observed is None) != (before == ()):
                raise SemanticHandoffCommitUnknown("semantic handoff directory and record disagree")
            return observed
        except SemanticHandoffError, SemanticHandoffCommitUnknown:
            raise
        except (StateCorrupt, StatePathError) as exc:
            raise SemanticHandoffConflict(
                "semantic handoff directory is unsafe or unreadable"
            ) from exc
        except Exception as exc:
            raise SemanticHandoffCommitUnknown(
                "semantic handoff directory inspection is ambiguous"
            ) from exc

    def _fresh_entry(
        self,
        session: _ParsedSession,
        *,
        repo_uuid: str,
        maximum: int,
    ) -> dict[str, object]:
        if session.begin.get("repo_uuid") != repo_uuid:
            raise SemanticHandoffInvalid("fresh session names another repository")
        relative = (
            LeaseStore._directory(repo_uuid)
            / "semantic-staging"
            / session.begin_sha256
            / "result.json"
        )
        try:
            raw_binding = self.state.read_existing_bytes(
                relative,
                max_bytes=min(maximum, COMPLETE_MAX_BYTES),
            )
            binding = parse_result_binding(raw_binding)
        except Exception as exc:
            raise SemanticHandoffInvalid("fresh result binding is missing or invalid") from exc
        return {
            "origin": "fresh_worker_session",
            "begin_request": deepcopy(dict(session.begin)),
            "begin_request_sha256": session.begin_sha256,
            "session": {
                "frames": [deepcopy(dict(frame)) for frame in session.frames],
                "stdout_bytes": len(session.stdout_canonical),
                "stdout_sha256": sha256(session.stdout_canonical),
                "process_exit_code": session.process_exit_code,
            },
            "result_binding": deepcopy(dict(binding.value)),
            "result_binding_bytes": binding.bytes_count,
            "result_binding_sha256": binding.sha256,
        }

    def _current_source_handoff(
        self,
        *,
        repo_uuid: str,
        target_generation_id: str,
        structural_request: StructuralBuildRequest,
        pointer: Mapping[str, object] | None,
    ) -> tuple[str, SemanticResultHandoff]:
        if pointer is None or structural_request.expected_current_receipt_sha256 is None:
            raise SemanticHandoffConflict("carried completion requires a current generation")
        current = _mapping(pointer.get("current"), "current pointer")
        source_generation = _generation_id(
            current.get("generation_id"),
            "carried source generation",
        )
        current_receipt = _digest(current.get("receipt_sha256"), "current receipt")
        if (
            source_generation == target_generation_id
            or current_receipt != structural_request.expected_current_receipt_sha256
        ):
            raise SemanticHandoffConflict("carried source and target authority differ")
        try:
            receipt = self.generations.verify_generation(repo_uuid, source_generation)
        except Exception as exc:
            raise SemanticHandoffConflict("current carried source generation is invalid") from exc
        if receipt.sha256 != current_receipt:
            raise SemanticHandoffConflict("current generation receipt binding differs")
        receipt_value = receipt.to_dict()
        payload = _mapping(receipt_value.get("sealed_query_payload"), "sealed payload")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise SemanticHandoffConflict("current generation payload inventory is invalid")
        matches = [
            _mapping(entry, "semantic input inventory entry")
            for entry in entries
            if isinstance(entry, Mapping) and entry.get("path") == SEMANTIC_INPUT_PATH
        ]
        if len(matches) != 1:
            raise SemanticHandoffConflict("current generation has no singular semantic input")
        entry = matches[0]
        if entry.get("file_type") != "regular_file" or entry.get("mode") != "0600":
            raise SemanticHandoffConflict("current semantic input metadata is invalid")
        source_size = entry.get("size")
        if type(source_size) is not int or cast(int, source_size) < 1:
            raise SemanticHandoffConflict("current semantic input size is invalid")
        source_size = cast(int, source_size)
        relative = (
            LeaseStore._directory(repo_uuid)
            / "generations"
            / source_generation
            / SEMANTIC_INPUT_PATH
        )
        try:
            generation_relative = (
                LeaseStore._directory(repo_uuid) / "generations" / source_generation
            )
            payload_path = self.state.path(generation_relative / "graphify-out")
            with self.state.existing_private_directory(generation_relative) as generation:
                payload_descriptor = self.state._open_directory_at(
                    generation,
                    "graphify-out",
                    payload_path,
                    allowed_modes=frozenset({0o700, 0o755}),
                )
                if payload_descriptor is None:  # pragma: no cover - allow_missing is false
                    raise SemanticHandoffConflict("current generation payload root is missing")
                try:
                    raw = self._read_semantic_input_descriptor(
                        payload_descriptor,
                        self.state.path(relative),
                        maximum=source_size,
                    )
                finally:
                    os.close(payload_descriptor)
            if raw is None:
                raise SemanticHandoffConflict("current semantic input is missing")
        except Exception as exc:
            raise SemanticHandoffConflict("current semantic input cannot be reopened") from exc
        if entry.get("size") != len(raw) or entry.get("sha256") != sha256(raw):
            raise SemanticHandoffConflict("current semantic input inventory binding differs")
        if payload.get("manifest_sha256") != payload_manifest_sha256(
            "graphify-out",
            cast(Sequence[Mapping[str, object]], entries),
        ):
            raise SemanticHandoffConflict("current payload manifest binding differs")
        source_handoff = parse_semantic_result_handoff(
            raw,
            max_bytes=source_size,
        )
        if (
            source_handoff.repo_uuid != repo_uuid
            or source_handoff.target_generation_id != source_generation
        ):
            raise SemanticHandoffConflict("current semantic input source identity differs")
        return source_generation, source_handoff

    @staticmethod
    def _carried_entry(
        source_handoff: SemanticResultHandoff,
        work: SemanticDesiredWork,
    ) -> dict[str, object]:
        matches = [
            entry
            for entry in source_handoff.results
            if _work_from_result_entry(entry).identity == work.identity
        ]
        if len(matches) != 1:
            raise SemanticHandoffConflict("carried desired work lacks exact source evidence")
        copied = deepcopy(dict(matches[0]))
        copied["origin"] = "carried_current_generation"
        return copied

    @staticmethod
    def _validate_authority_locked(
        document: Mapping[str, object],
        entry: Mapping[str, object],
        lease_state: WorkspaceLeaseState,
        request: SyncRequest,
        structural_request: StructuralBuildRequest,
        queue: SemanticQueueSnapshot,
        pointer: Mapping[str, object] | None,
        observations: Sequence[SourceObservation],
        compatibility_sha256: str,
        *,
        require_original_operation_epoch: bool,
    ) -> None:
        request_value = request.to_dict()
        try:
            validated_request = type(request).from_mapping(request_value)
        except Exception as exc:
            raise SemanticHandoffInvalid("semantic finalization sync request is invalid") from exc
        if validated_request != request:
            raise SemanticHandoffInvalid("semantic finalization sync request changed")
        try:
            validated_structural = StructuralBuildRequest.from_mapping(structural_request.to_dict())
        except Exception as exc:
            raise SemanticHandoffInvalid(
                "semantic finalization structural request is invalid"
            ) from exc
        expected_structural = (
            request.sha256,
            request.expected_registry_revision,
            request.expected_active_source_revision,
            request.expected_operation_epoch,
            request.expected_migration_epoch,
            request.expected_pointer_revision,
            request.expected_current_receipt_sha256,
            request.source_epoch,
            request.expected_payload_bytes,
            request.capacity_policy.sha256,
            compatibility_sha256,
        )
        actual_structural = (
            validated_structural.logical_request_sha256,
            validated_structural.expected_registry_revision,
            validated_structural.expected_active_source_revision,
            validated_structural.expected_operation_epoch,
            validated_structural.expected_migration_epoch,
            validated_structural.expected_pointer_revision,
            validated_structural.expected_current_receipt_sha256,
            validated_structural.source_epoch,
            validated_structural.expected_payload_bytes,
            validated_structural.capacity_policy_sha256,
            validated_structural.compatibility_sha256,
        )
        if actual_structural != expected_structural:
            raise SemanticHandoffConflict("structural and sync request authority differ")
        if validated_structural.compatibility_sha256 != compatibility_sha256:
            raise SemanticHandoffConflict("selected compatibility authority differs")
        registry_revision = _integer(document.get("revision"), "registry revision", minimum=1)
        active_source_revision = _integer(
            entry.get("active_source_revision"),
            "active-source revision",
            minimum=1,
        )
        if (
            registry_revision != request.expected_registry_revision
            or active_source_revision != request.expected_active_source_revision
            or lease_state.migration_epoch != request.expected_migration_epoch
        ):
            raise SemanticHandoffConflict("repository authority differs from structural request")
        if require_original_operation_epoch:
            if lease_state.operation_epoch != request.expected_operation_epoch:
                raise SemanticHandoffConflict(
                    "repository operation authority differs from structural request"
                )
        elif lease_state.operation_epoch < request.expected_operation_epoch:
            raise SemanticHandoffConflict("repository operation authority regressed")
        pointer_revision = 0
        current_receipt: str | None = None
        current_generation: str | None = None
        if pointer is not None:
            pointer_revision = _integer(
                pointer.get("pointer_revision"),
                "pointer revision",
            )
            current = _mapping(pointer.get("current"), "current pointer")
            current_receipt = _digest(current.get("receipt_sha256"), "current receipt")
            current_generation = _generation_id(
                current.get("generation_id"),
                "current generation",
            )
        if (pointer_revision, current_receipt) != (
            request.expected_pointer_revision,
            request.expected_current_receipt_sha256,
        ):
            raise SemanticHandoffConflict("pointer authority differs from structural request")
        if current_generation == request.generation_id:
            raise SemanticHandoffConflict("target generation is already current")
        reconciliation = queue.reconciliation
        if (
            queue.repo_uuid != request.repo_uuid
            or queue.active_source_revision != request.expected_active_source_revision
            or queue.desired_watermark != request.semantic_desired_watermark
            or queue.completed_watermark != queue.desired_watermark
            or reconciliation is None
            or not reconciliation.semantic_required
            or reconciliation.source_epoch != request.source_epoch
            or reconciliation.policy_sha256 != structural_request.policy_sha256
            or reconciliation.desired_watermark != request.semantic_desired_watermark
            or any(item.status != "completed" for item in queue.items)
        ):
            raise SemanticHandoffConflict("semantic reconciliation is not exactly complete")
        if reconciliation.source_observations.to_dict() != _source_observation_evidence(
            structural_request
        ):
            raise SemanticHandoffConflict("semantic and structural observations differ")
        try:
            documents = [
                {
                    "source_commit": item.source_commit,
                    "inventory_sha256": item.inventory_sha256,
                    "policy_sha256": item.policy_sha256,
                    "detector_id": item.detector_id,
                    "stable_inventory_passes": item.stable_inventory_passes,
                    "entries_sha256": hashlib.sha256(
                        canonical_json_bytes([entry.to_dict() for entry in item.entries])
                    ).hexdigest(),
                }
                for item in observations
            ]
        except Exception as exc:
            raise SemanticHandoffConflict("trusted source observations are invalid") from exc
        if len(documents) != 2 or documents[0] != documents[1]:
            raise SemanticHandoffConflict("trusted source observations are not stable")
        if documents[0] != structural_request.source_observation_document():
            raise SemanticHandoffConflict("trusted source observations differ from request")

    def _target_state_locked(
        self,
        *,
        repo_uuid: str,
        target_generation_id: str,
        structural_request: StructuralBuildRequest,
        handoff_exists: bool,
    ) -> StagedBuildState | None:
        staged = self.generations._load_staged_build_locked(repo_uuid)
        target_staging = self.generations._staging(repo_uuid, target_generation_id)
        target_generation = self.generations._generation(repo_uuid, target_generation_id)
        target_lock = self.generations._lock(repo_uuid, target_generation_id)
        staging_exists = self.state.private_directory_exists(target_staging)
        generation_exists = self.state.private_directory_exists(target_generation)
        lock_exists = self.state.private_file_exists(target_lock)
        if generation_exists:
            raise SemanticHandoffConflict("target generation is already certified")
        if not handoff_exists:
            if (
                staging_exists
                or lock_exists
                or (staged is not None and staged.generation_id == target_generation_id)
            ):
                raise SemanticHandoffConflict("target generation already has durable state")
            return staged
        if staged is None:
            if staging_exists or lock_exists:
                raise SemanticHandoffConflict("retained handoff has unbound target state")
            return None
        if staged.generation_id != target_generation_id:
            if staged.lifecycle_state not in {"PROMOTED", "ABANDONED"}:
                raise SemanticHandoffConflict("another staged request blocks exact replay")
            if staging_exists or lock_exists:
                raise SemanticHandoffConflict("retained handoff target state is unbound")
            return staged
        if staged.request.sha256 != structural_request.sha256 or staged.lifecycle_state not in {
            "REQUESTED",
            "PUBLISHING",
            "COMPLETE",
        }:
            raise SemanticHandoffConflict("retained handoff staged state differs")
        return staged

    @staticmethod
    def _validate_retained_carried(
        retained: SemanticResultHandoff,
        source_generation: str,
        source_handoff: SemanticResultHandoff,
    ) -> None:
        if source_generation != retained.carried_source_generation_id:
            raise SemanticHandoffConflict("retained carried source differs")
        source_entries = {
            _work_from_result_entry(item).identity: item for item in source_handoff.results
        }
        for item in retained.results:
            if item["origin"] != "carried_current_generation":
                continue
            source_item = source_entries.get(_work_from_result_entry(item).identity)
            if source_item is None:
                raise SemanticHandoffConflict("retained carried result disappeared")
            retained_evidence = {key: value for key, value in item.items() if key != "origin"}
            source_evidence = {key: value for key, value in source_item.items() if key != "origin"}
            if canonical_protocol_bytes(retained_evidence) != canonical_protocol_bytes(
                source_evidence
            ):
                raise SemanticHandoffConflict("retained carried result differs")

    def _prepare_evidence(
        self,
        request: SyncRequest,
        structural_request: StructuralBuildRequest,
        evidence: Sequence[SemanticResultEvidence],
        *,
        pointer: Mapping[str, object] | None,
        relative: Path,
    ) -> _PreparedHandoffEvidence:
        fresh_evidence = tuple(
            item for item in evidence if isinstance(item, FreshWorkerSessionEvidence)
        )
        carried_evidence = tuple(
            item for item in evidence if isinstance(item, CarriedSemanticResultEvidence)
        )
        if len(fresh_evidence) + len(carried_evidence) != len(evidence):
            raise SemanticHandoffInvalid("semantic result evidence type is unsupported")
        fresh_sessions = tuple(_parse_session(item) for item in fresh_evidence)
        fresh_entries = tuple(
            self._fresh_entry(
                session,
                repo_uuid=request.repo_uuid,
                maximum=structural_request.expected_payload_bytes,
            )
            for session in fresh_sessions
        )
        carried_works: list[SemanticDesiredWork] = []
        for item in carried_evidence:
            try:
                carried_works.append(item.work.validated())
            except Exception as exc:
                raise SemanticHandoffInvalid("carried desired work is invalid") from exc
        try:
            pointer_snapshot = None if pointer is None else deepcopy(dict(pointer))
            canonical_json_bytes(pointer_snapshot)
        except Exception as exc:
            raise SemanticHandoffInvalid("current pointer evidence is invalid") from exc
        existing = self._read_exact_handoff_directory(
            relative,
            maximum=structural_request.expected_payload_bytes,
        )
        retained: SemanticResultHandoff | None = None
        if existing is not None:
            retained = parse_semantic_result_handoff(
                existing,
                max_bytes=structural_request.expected_payload_bytes,
            )
            if (
                retained.repo_uuid != request.repo_uuid
                or retained.target_generation_id != request.generation_id
                or retained.structural_request.canonical != structural_request.canonical
            ):
                raise SemanticHandoffConflict("retained handoff identities differ")
        needs_carried_source = bool(carried_works) or (
            retained is not None
            and not evidence
            and retained.carried_source_generation_id is not None
        )
        source_generation: str | None = None
        source_receipt_sha256: str | None = None
        source_handoff: SemanticResultHandoff | None = None
        if needs_carried_source:
            source_generation, source_handoff = self._current_source_handoff(
                repo_uuid=request.repo_uuid,
                target_generation_id=request.generation_id,
                structural_request=structural_request,
                pointer=pointer_snapshot,
            )
            current = _mapping(
                None if pointer_snapshot is None else pointer_snapshot.get("current"),
                "prepared current pointer",
            )
            source_receipt_sha256 = _digest(
                current.get("receipt_sha256"),
                "prepared current receipt",
            )
        if (
            retained is not None
            and not evidence
            and retained.carried_source_generation_id is not None
        ):
            if source_generation is None or source_handoff is None:
                raise SemanticHandoffConflict("retained carried source is missing")
            self._validate_retained_carried(
                retained,
                source_generation,
                source_handoff,
            )
        return _PreparedHandoffEvidence(
            existing=existing,
            retained=retained,
            fresh_entries=fresh_entries,
            carried_works=tuple(carried_works),
            carried_source_generation_id=source_generation,
            carried_source_receipt_sha256=source_receipt_sha256,
            carried_source_handoff=source_handoff,
            pointer=pointer_snapshot,
        )

    def _revalidate_prepared_carried_locked(
        self,
        prepared: _PreparedHandoffEvidence,
        structural_request: StructuralBuildRequest,
    ) -> None:
        source_generation = prepared.carried_source_generation_id
        source_receipt_sha256 = prepared.carried_source_receipt_sha256
        source_handoff = prepared.carried_source_handoff
        if source_generation is None and source_receipt_sha256 is None and source_handoff is None:
            return
        if source_generation is None or source_receipt_sha256 is None or source_handoff is None:
            raise SemanticHandoffConflict("prepared carried source evidence is incomplete")
        generation_relative = (
            LeaseStore._directory(source_handoff.repo_uuid) / "generations" / source_generation
        )
        payload_path = self.state.path(generation_relative / "graphify-out")
        semantic_path = payload_path / "semantic-inputs.json"
        generation_lock = self.generations._lock(
            source_handoff.repo_uuid,
            source_generation,
        )
        try:
            with self.state.existing_generation_lock(
                generation_lock,
                generation_id=source_generation,
                exclusive=False,
            ):
                try:
                    receipt = self.generations.verify_generation(
                        source_handoff.repo_uuid,
                        source_generation,
                    )
                except Exception as exc:
                    raise SemanticHandoffConflict(
                        "prepared carried source generation is invalid"
                    ) from exc
                if receipt.sha256 != source_receipt_sha256:
                    raise SemanticHandoffConflict("prepared carried source receipt changed")
                with self.state.existing_private_directory(generation_relative) as generation:
                    payload = self.state._open_directory_at(
                        generation,
                        "graphify-out",
                        payload_path,
                        allowed_modes=frozenset({0o700, 0o755}),
                    )
                    if payload is None:  # pragma: no cover - allow_missing is false
                        raise SemanticHandoffConflict("prepared carried source payload is missing")
                    try:
                        reopened = self._read_semantic_input_descriptor(
                            payload,
                            semantic_path,
                            maximum=len(source_handoff.canonical),
                        )
                    finally:
                        os.close(payload)
        except SemanticHandoffError, SemanticHandoffCommitUnknown:
            raise
        except Exception as exc:
            raise SemanticHandoffConflict("prepared carried source cannot be revalidated") from exc
        if reopened != source_handoff.canonical:
            raise SemanticHandoffConflict(
                "prepared carried source changed during authority capture"
            )

    def _build_candidate(
        self,
        *,
        request: SyncRequest,
        structural_request: StructuralBuildRequest,
        queue_binding: Mapping[str, object],
        fresh_entries: Sequence[Mapping[str, object]],
        carried_works: Sequence[SemanticDesiredWork],
        carried_source_generation_id: str | None,
        carried_source_handoff: SemanticResultHandoff | None,
    ) -> SemanticResultHandoff:
        carried_entries: list[dict[str, object]] = []
        if carried_works:
            if carried_source_generation_id is None or carried_source_handoff is None:
                raise SemanticHandoffConflict("carried source evidence is missing")
            carried_entries = [
                self._carried_entry(carried_source_handoff, work) for work in carried_works
            ]
        results = sorted([*fresh_entries, *carried_entries], key=_result_sort_key)
        candidate = {
            "contract": HANDOFF_CONTRACT,
            "format_version": HANDOFF_FORMAT_VERSION,
            "repo_uuid": request.repo_uuid,
            "target_generation_id": request.generation_id,
            "carried_source_generation_id": (
                carried_source_generation_id if carried_works else None
            ),
            "structural_request": structural_request.to_dict(),
            "structural_request_sha256": structural_request.sha256,
            "queue": deepcopy(dict(queue_binding)),
            "results": results,
            "materialized": _materialize(results),
        }
        return parse_semantic_result_handoff(
            canonical_protocol_bytes(candidate),
            max_bytes=structural_request.expected_payload_bytes,
        )

    def _install_locked(
        self,
        handoff: SemanticResultHandoff,
        *,
        existing: bytes | None,
    ) -> SemanticResultHandoff:
        relative = self.relative_path_for(handoff)
        if existing is not None:
            if existing != handoff.canonical:
                raise SemanticHandoffConflict("retained handoff bytes conflict")
            reopened = self._read_exact_handoff_directory(
                relative,
                maximum=handoff.structural_request.expected_payload_bytes,
            )
            if reopened != existing:
                raise SemanticHandoffCommitUnknown("retained handoff reopen differs")
            return parse_semantic_result_handoff(
                existing,
                max_bytes=handoff.structural_request.expected_payload_bytes,
            )
        for retry in range(2):
            try:
                self.state.install_once_bytes(
                    relative,
                    handoff.canonical,
                    label=f"semantic_result_handoff:{handoff.target_generation_id}",
                )
            except CommitUnknown, InjectedFault:
                try:
                    observed = self._read_exact_handoff_directory(
                        relative,
                        maximum=handoff.structural_request.expected_payload_bytes,
                    )
                except (SemanticHandoffError, SemanticHandoffCommitUnknown) as exc:
                    raise SemanticHandoffCommitUnknown(
                        "semantic handoff installation is ambiguous"
                    ) from exc
                if observed == handoff.canonical:
                    break
                if observed is None and retry == 0:
                    continue
                if observed is not None:
                    raise SemanticHandoffConflict("retained handoff bytes conflict")
                raise SemanticHandoffCommitUnknown(
                    "semantic handoff installation outcome is uncertain"
                )
            except (StateCorrupt, StatePathError) as exc:
                observed = self._read_exact_handoff_directory(
                    relative,
                    maximum=handoff.structural_request.expected_payload_bytes,
                )
                if observed == handoff.canonical:
                    break
                if observed is not None:
                    raise SemanticHandoffConflict("retained handoff bytes conflict") from exc
                raise SemanticHandoffCommitUnknown(
                    "semantic handoff installation outcome is uncertain"
                ) from exc
            else:
                break
        reopened = self._read_exact_handoff_directory(
            relative,
            maximum=handoff.structural_request.expected_payload_bytes,
        )
        if reopened != handoff.canonical:
            raise SemanticHandoffCommitUnknown("semantic handoff reopen differs")
        self.fault_hook(f"semantic_handoff:{handoff.target_generation_id}:reopened")
        return parse_semantic_result_handoff(
            cast(bytes, reopened),
            max_bytes=handoff.structural_request.expected_payload_bytes,
        )

    def capture_and_install(
        self,
        request: SyncRequest,
        structural_request: StructuralBuildRequest,
        evidence: Sequence[SemanticResultEvidence],
        *,
        current_pointer: Mapping[str, object] | None,
        source_observations: Sequence[SourceObservation],
    ) -> SemanticHandoffCapture:
        """Capture exact authority, capacity-preflight, install, and reopen."""

        try:
            request = type(request).from_mapping(request.to_dict())
            structural_request = StructuralBuildRequest.from_mapping(structural_request.to_dict())
        except Exception as exc:
            raise SemanticHandoffInvalid("semantic handoff request authority is invalid") from exc
        if structural_request.logical_request_sha256 != request.sha256:
            raise SemanticHandoffInvalid("semantic handoff request identities differ")
        evidence = tuple(evidence)
        relative = self._relative_path(
            request.repo_uuid,
            request.generation_id,
            structural_request.sha256,
        )
        prepared = self._prepare_evidence(
            request,
            structural_request,
            evidence,
            pointer=current_pointer,
            relative=relative,
        )
        try:
            with self.leases._bound_request_state(request.repo_uuid) as (
                document,
                entry,
                lease_state,
            ):
                queue = self.semantic_queue._load_locked(request.repo_uuid)
                pointer_document = self.generations._visible_pointer_locked(request.repo_uuid)
                pointer = None if pointer_document is None else pointer_document.to_dict()
                if canonical_json_bytes(pointer) != canonical_json_bytes(prepared.pointer):
                    raise SemanticHandoffConflict("current pointer changed during evidence capture")
                self._revalidate_prepared_carried_locked(
                    prepared,
                    structural_request,
                )
                existing = self._read_exact_handoff_directory(
                    relative,
                    maximum=structural_request.expected_payload_bytes,
                )
                if existing != prepared.existing:
                    if existing is not None and prepared.existing is not None:
                        raise SemanticHandoffConflict(
                            "semantic handoff changed during evidence capture"
                        )
                    raise SemanticHandoffCommitUnknown(
                        "semantic handoff visibility changed during evidence capture"
                    )
                retained = prepared.retained
                if retained is not None:
                    pre_bind = _queue_from_existing_binding(queue, retained.queue)
                else:
                    if (
                        queue.reconciliation is None
                        or queue.reconciliation.sealed_input_manifest_sha256 is not None
                    ):
                        raise SemanticHandoffConflict("first handoff requires an unsealed queue")
                    pre_bind = queue
                staged = self._target_state_locked(
                    repo_uuid=request.repo_uuid,
                    target_generation_id=request.generation_id,
                    structural_request=structural_request,
                    handoff_exists=retained is not None,
                )
                exact_staged_recovery = staged is not None and (
                    staged.generation_id == request.generation_id
                    and staged.request.sha256 == structural_request.sha256
                    and retained is not None
                )
                self._validate_authority_locked(
                    document.to_dict(),
                    entry,
                    lease_state,
                    request,
                    structural_request,
                    pre_bind,
                    pointer,
                    source_observations,
                    self.generations.compatibility_sha256,
                    require_original_operation_epoch=not exact_staged_recovery,
                )
                if retained is not None and not evidence:
                    candidate = retained
                else:
                    candidate = self._build_candidate(
                        request=request,
                        structural_request=structural_request,
                        queue_binding=_queue_binding(pre_bind),
                        fresh_entries=prepared.fresh_entries,
                        carried_works=prepared.carried_works,
                        carried_source_generation_id=(prepared.carried_source_generation_id),
                        carried_source_handoff=prepared.carried_source_handoff,
                    )
                if retained is not None and candidate.canonical != retained.canonical:
                    raise SemanticHandoffConflict("exact handoff replay bytes differ")
                self.generations.preflight_retained_handoff_locked(
                    repo_uuid=request.repo_uuid,
                    generation_id=request.generation_id,
                    expected_payload_bytes=request.expected_payload_bytes,
                    handoff_bytes=len(candidate.canonical),
                    existing_handoff_bytes=0 if existing is None else len(existing),
                    policy=request.capacity_policy,
                )
                installed = self._install_locked(candidate, existing=existing)
                return SemanticHandoffCapture(
                    handoff=installed,
                    pre_bind_queue=pre_bind,
                    staged_state=staged,
                )
        except LeaseRecoveryRequired as exc:
            raise SemanticHandoffConflict("workspace authority requires recovery") from exc
        except CapacityExceeded as exc:
            raise SemanticHandoffConflict("shared generation capacity is unavailable") from exc

    def cleanup_consumed_fresh_results(
        self,
        handoff: SemanticResultHandoff,
    ) -> None:
        """Best-effort delete only exact fresh envelopes retained by the handoff."""

        for entry in handoff.results:
            if entry.get("origin") != "fresh_worker_session":
                continue
            try:
                begin_sha256 = _digest(
                    entry.get("begin_request_sha256"),
                    "fresh result begin request",
                )
                binding = _mapping(entry.get("result_binding"), "fresh result binding")
                expected = canonical_protocol_bytes(binding)
                if len(expected) != _integer(
                    entry.get("result_binding_bytes"), "fresh result bytes"
                ) or sha256(expected) != _digest(
                    entry.get("result_binding_sha256"),
                    "fresh result digest",
                ):
                    continue
                relative = (
                    LeaseStore._directory(handoff.repo_uuid)
                    / "semantic-staging"
                    / begin_sha256
                    / "result.json"
                )
                self.state.consume_matching_bytes_and_sync(
                    relative,
                    expected,
                    label=f"semantic_result_cleanup:{begin_sha256}",
                )
            except Exception:
                continue

    def install_generation_copy(
        self,
        handoff: SemanticResultHandoff,
        preparation: StagedBuildPreparation,
        *,
        monotonic_ns: int,
    ) -> Path:
        """Install or reopen the exact handoff under the current BUILD fence."""

        allocation = preparation.allocation
        if (
            allocation.repo_uuid != handoff.repo_uuid
            or allocation.generation_id != handoff.target_generation_id
            or allocation.staging_path
            != self.state.path(
                self.generations._staging(handoff.repo_uuid, handoff.target_generation_id)
            )
        ):
            raise SemanticHandoffConflict("semantic input allocation identity differs")
        with self.leases.current_operation(
            preparation.grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD"}),
        ) as operation:
            state = self.generations._load_staged_build_locked(operation.repo_uuid)
            if state is None:
                raise SemanticHandoffConflict("semantic input staged request is missing")
            self.generations._require_staged_binding(
                state,
                repo_uuid=handoff.repo_uuid,
                generation_id=handoff.target_generation_id,
                request=handoff.structural_request,
            )
            self.generations._require_allocation(operation, allocation)
            self.generations._require_structural_allocation(state, allocation)
            if state.lifecycle_state not in {"PUBLISHING", "COMPLETE"}:
                raise SemanticHandoffConflict("semantic input staging is not publishable")
            if state.lifecycle_state == "PUBLISHING" and (
                state.operation_epoch,
                state.fence_token,
            ) != (operation.grant.operation_epoch, operation.fence_token):
                raise SemanticHandoffConflict("semantic input staging belongs to another fence")
            lock = self.generations._lock(
                handoff.repo_uuid,
                handoff.target_generation_id,
            )
            with self.state.existing_generation_lock(
                lock,
                generation_id=handoff.target_generation_id,
                exclusive=True,
            ):
                reopened = self._install_or_reopen_generation_copy_locked(
                    handoff,
                    require_existing=state.lifecycle_state == "COMPLETE",
                )
        if reopened != handoff.canonical:
            raise SemanticHandoffCommitUnknown("generation semantic input reopen differs")
        parse_semantic_result_handoff(
            reopened,
            max_bytes=handoff.structural_request.expected_payload_bytes,
        )
        self.fault_hook(f"semantic_input_copy:{handoff.target_generation_id}:reopened")
        return self.state.path(self._generation_copy_relative(handoff))

    def _read_generation_copy_descriptor(
        self,
        descriptor: int,
        handoff: SemanticResultHandoff,
    ) -> bytes | None:
        path = self.state.path(self._generation_copy_relative(handoff))
        return self._read_semantic_input_descriptor(
            descriptor,
            path,
            maximum=handoff.structural_request.expected_payload_bytes,
        )

    def _read_semantic_input_descriptor(
        self,
        descriptor: int,
        path: Path,
        *,
        maximum: int,
    ) -> bytes | None:
        try:
            details = os.stat(
                path.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SemanticHandoffCommitUnknown(
                "generation semantic input cannot be inspected"
            ) from exc
        try:
            self.state._require_regular_details(
                details,
                path,
                allowed_modes=frozenset({0o600}),
            )
            file_descriptor = os.open(
                path.name,
                self.state._regular_open_flags(),
                dir_fd=descriptor,
            )
            transferred = False
            try:
                opened = os.fstat(file_descriptor)
                current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                identity = self.state._stat_identity(details)
                if (
                    self.state._stat_identity(opened) != identity
                    or self.state._stat_identity(current) != identity
                ):
                    raise SemanticHandoffCommitUnknown(
                        "generation semantic input changed while opening"
                    )
                transferred = True
                return self.state._read_regular_descriptor(
                    file_descriptor,
                    path,
                    max_bytes=maximum,
                )
            finally:
                if not transferred:
                    os.close(file_descriptor)
        except SemanticHandoffCommitUnknown:
            raise
        except Exception as exc:
            raise SemanticHandoffConflict(
                "generation semantic input is unsafe or unreadable"
            ) from exc

    def _install_generation_copy_descriptor(
        self,
        descriptor: int,
        handoff: SemanticResultHandoff,
    ) -> None:
        path = self.state.path(self._generation_copy_relative(handoff))
        temporary = f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(temporary, flags, 0o600, dir_fd=descriptor)
        visible = False
        replaced = False
        try:
            try:
                os.fchmod(file_descriptor, 0o600)
                self.state._write_all(file_descriptor, handoff.canonical)
                self.state.syscalls.fsync(file_descriptor)
            finally:
                os.close(file_descriptor)
            self.state.syscalls.rename_exclusive_at(
                temporary,
                path.name,
                source_dir_fd=descriptor,
                destination_dir_fd=descriptor,
            )
            replaced = True
            visible = True
            self.fault_hook(f"semantic_input_copy:{handoff.target_generation_id}:installed")
            self.state.syscalls.fsync(descriptor)
        except BaseException as exc:
            if visible:
                raise SemanticHandoffCommitUnknown(
                    "generation semantic input became visible before durability acknowledgement"
                ) from exc
            raise
        finally:
            if not replaced:
                try:
                    self.state.syscalls.unlink_at(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass

    def _install_or_reopen_generation_copy_locked(
        self,
        handoff: SemanticResultHandoff,
        *,
        require_existing: bool,
    ) -> bytes:
        staging_relative = self.generations._staging(
            handoff.repo_uuid,
            handoff.target_generation_id,
        )
        payload_path = self.state.path(staging_relative / "graphify-out")
        try:
            with self.state.existing_private_directory(staging_relative) as staging:
                details = os.stat(
                    "graphify-out",
                    dir_fd=staging,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(details.st_mode):
                    raise SemanticHandoffConflict("structural payload root is not a directory")
                payload = self.state._open_directory_at(
                    staging,
                    "graphify-out",
                    payload_path,
                    allowed_modes=frozenset({0o700, 0o755}),
                )
                if payload is None:  # pragma: no cover - allow_missing is false
                    raise SemanticHandoffConflict("structural payload root is missing")
                try:
                    if self.state._stat_identity(os.fstat(payload)) != self.state._stat_identity(
                        details
                    ):
                        raise SemanticHandoffCommitUnknown(
                            "structural payload root changed while opening"
                        )
                    observed = self._read_generation_copy_descriptor(payload, handoff)
                    if observed is not None:
                        if observed != handoff.canonical:
                            raise SemanticHandoffConflict(
                                "generation semantic input bytes conflict"
                            )
                        return observed
                    if require_existing:
                        raise SemanticHandoffConflict("completed staging lacks its semantic input")
                    for retry in range(2):
                        try:
                            self._install_generation_copy_descriptor(payload, handoff)
                        except FileExistsError:
                            observed = self._read_generation_copy_descriptor(payload, handoff)
                            if observed == handoff.canonical:
                                return cast(bytes, observed)
                            if observed is not None:
                                raise SemanticHandoffConflict(
                                    "generation semantic input bytes conflict"
                                )
                            raise SemanticHandoffCommitUnknown(
                                "generation semantic input installation is uncertain"
                            )
                        except CommitUnknown, InjectedFault:
                            observed = self._read_generation_copy_descriptor(payload, handoff)
                            if observed == handoff.canonical:
                                return cast(bytes, observed)
                            if observed is None and retry == 0:
                                continue
                            if observed is not None:
                                raise SemanticHandoffConflict(
                                    "generation semantic input bytes conflict"
                                )
                            raise SemanticHandoffCommitUnknown(
                                "generation semantic input installation is uncertain"
                            )
                        observed = self._read_generation_copy_descriptor(payload, handoff)
                        if observed == handoff.canonical:
                            return cast(bytes, observed)
                        raise SemanticHandoffCommitUnknown(
                            "generation semantic input reopen differs"
                        )
                finally:
                    os.close(payload)
        except SemanticHandoffError:
            raise
        except SemanticHandoffCommitUnknown:
            raise
        except (StateCorrupt, StatePathError, OSError) as exc:
            raise SemanticHandoffConflict(
                "generation semantic input path is unsafe or unreadable"
            ) from exc
        raise SemanticHandoffCommitUnknown("generation semantic input installation is uncertain")

    def _reopen_exact_files(
        self,
        handoff: SemanticResultHandoff,
        *,
        deadline_ns: int | None = None,
    ) -> None:
        require_before_deadline(deadline_ns, "semantic handoff verification exceeded deadline")
        external = self.state.read_existing_bytes(
            self.relative_path_for(handoff),
            max_bytes=handoff.structural_request.expected_payload_bytes,
            deadline_ns=deadline_ns,
        )
        staging_relative = self.generations._staging(
            handoff.repo_uuid,
            handoff.target_generation_id,
        )
        payload_path = self.state.path(staging_relative / "graphify-out")
        with self.state.existing_private_directory(staging_relative) as staging:
            payload = self.state._open_directory_at(
                staging,
                "graphify-out",
                payload_path,
                allowed_modes=frozenset({0o700, 0o755}),
            )
            if payload is None:  # pragma: no cover - allow_missing is false
                raise SemanticHandoffConflict("terminal structural payload root is missing")
            try:
                copied = self._read_generation_copy_descriptor(payload, handoff)
            finally:
                os.close(payload)
        if external != handoff.canonical or copied != handoff.canonical:
            raise SemanticHandoffConflict("semantic handoff copies differ")

    def validate_terminal_locked(
        self,
        operation: LeaseOperation,
        current_queue: SemanticQueueSnapshot,
        *,
        request: SyncRequest,
        capture: SemanticHandoffCapture,
        completion: StagedBuildCompletion,
        source_observations: Sequence[SourceObservation],
        manifest_sha256: str,
        deadline_ns: int | None = None,
    ) -> None:
        handoff = capture.handoff
        if operation.operation != "BUILD" or operation.repo_uuid != request.repo_uuid:
            raise SemanticHandoffConflict("terminal operation is not the exact BUILD grant")
        document = operation.registry.to_dict()
        entries = [
            item
            for item in cast(list[Mapping[str, object]], document["workspaces"])
            if item["repo_uuid"] == request.repo_uuid
        ]
        if len(entries) != 1:
            raise SemanticHandoffConflict("terminal repository authority is ambiguous")
        pointer_document = self.generations._visible_pointer_locked(request.repo_uuid)
        pointer = None if pointer_document is None else pointer_document.to_dict()
        pre_or_post = _queue_from_existing_binding(current_queue, handoff.queue)
        if pre_or_post != capture.pre_bind_queue:
            raise SemanticHandoffConflict("terminal queue differs from captured authority")
        self._validate_authority_locked(
            document,
            entries[0],
            operation.state,
            request,
            handoff.structural_request,
            pre_or_post,
            pointer,
            source_observations,
            self.generations.compatibility_sha256,
            require_original_operation_epoch=False,
        )
        if (
            operation.grant.active_source_revision != request.expected_active_source_revision
            or operation.grant.migration_epoch != request.expected_migration_epoch
            or operation.grant.registry_revision != request.expected_registry_revision
        ):
            raise SemanticHandoffConflict("terminal BUILD grant authority differs")
        staged = self.generations._load_staged_build_locked(request.repo_uuid)
        exact_complete_replay = (
            capture.staged_state is not None
            and capture.staged_state.lifecycle_state == "COMPLETE"
            and staged is not None
            and capture.staged_state.canonical == staged.canonical
        )
        publisher_authority_matches = staged is not None and (
            (
                staged.operation_epoch == completion.allocation.operation_epoch
                and staged.fence_token == completion.allocation.fence_token
            )
            or (
                exact_complete_replay
                and staged.operation_epoch is not None
                and staged.fence_token is not None
                and staged.operation_epoch <= completion.allocation.operation_epoch
                and staged.fence_token <= completion.allocation.fence_token
            )
        )
        if (
            staged is None
            or staged.lifecycle_state != "COMPLETE"
            or staged.generation_id != request.generation_id
            or staged.request.sha256 != handoff.structural_request.sha256
            or staged.payload_manifest_sha256 != manifest_sha256
            or completion.state.canonical != staged.canonical
            or not publisher_authority_matches
            or completion.allocation.operation_epoch != operation.grant.operation_epoch
            or completion.allocation.fence_token != operation.fence_token
        ):
            raise SemanticHandoffConflict("terminal staged manifest authority differs")
        inventory = self.generations.inspect_staged_payload(completion.allocation)
        if (
            canonical_json_bytes(list(inventory)) != canonical_json_bytes(list(completion.entries))
            or payload_manifest_sha256("graphify-out", inventory) != manifest_sha256
        ):
            raise SemanticHandoffConflict("terminal payload inventory differs")
        semantic_entries = [entry for entry in inventory if entry["path"] == SEMANTIC_INPUT_PATH]
        if len(semantic_entries) != 1:
            raise SemanticHandoffConflict("terminal payload lacks one semantic input")
        semantic_entry = semantic_entries[0]
        if (
            semantic_entry["file_type"] != "regular_file"
            or semantic_entry["mode"] != "0600"
            or semantic_entry["size"] != len(handoff.canonical)
            or semantic_entry["sha256"] != handoff.sha256
        ):
            raise SemanticHandoffConflict("terminal semantic input inventory differs")
        self._reopen_exact_files(handoff, deadline_ns=deadline_ns)
        if handoff.carried_source_generation_id is not None:
            source_generation, _source_handoff = self._current_source_handoff(
                repo_uuid=request.repo_uuid,
                target_generation_id=request.generation_id,
                structural_request=handoff.structural_request,
                pointer=pointer,
            )
            if source_generation != handoff.carried_source_generation_id:
                raise SemanticHandoffConflict("terminal carried-source authority differs")

    def reopen_terminal(
        self,
        grant: LeaseGrant,
        *,
        request: SyncRequest,
        capture: SemanticHandoffCapture,
        completion: StagedBuildCompletion,
        source_observations: Sequence[SourceObservation],
        manifest_sha256: str,
        monotonic_ns: int,
        deadline_ns: int,
    ) -> tuple[StagedBuildState, SemanticQueueSnapshot]:
        with self.leases.current_operation_read_only(
            grant,
            monotonic_ns=monotonic_ns,
            allowed_operations=frozenset({"BUILD"}),
            deadline_ns=deadline_ns,
        ) as operation:
            staged = self.generations.read_only_staged_build_locked(
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            queue = self.semantic_queue.read_only_snapshot_locked(
                request.repo_uuid,
                deadline_ns=deadline_ns,
            )
            self.validate_terminal_locked(
                operation,
                queue,
                request=request,
                capture=capture,
                completion=completion,
                source_observations=source_observations,
                manifest_sha256=manifest_sha256,
                deadline_ns=deadline_ns,
            )
            reconciliation = queue.reconciliation
            if (
                staged is None
                or reconciliation is None
                or reconciliation.sealed_input_manifest_sha256 != manifest_sha256
            ):
                raise SemanticHandoffCommitUnknown("terminal sealed-input proof is absent")
            return staged, queue


__all__ = [
    "CarriedSemanticResultEvidence",
    "FreshWorkerSessionEvidence",
    "HANDOFF_CONTRACT",
    "HANDOFF_FORMAT_VERSION",
    "SEMANTIC_INPUT_PATH",
    "SemanticHandoffCapture",
    "SemanticHandoffCommitUnknown",
    "SemanticHandoffConflict",
    "SemanticHandoffError",
    "SemanticHandoffInvalid",
    "SemanticResultEvidence",
    "SemanticResultFinalization",
    "SemanticResultHandoff",
    "SemanticResultHandoffStore",
    "parse_semantic_result_handoff",
]
