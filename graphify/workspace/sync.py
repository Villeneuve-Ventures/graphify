"""Provider-neutral S4 library orchestration over the S3 durable stores.

Requests freeze detection independently of completion. Retrying requires the
same request; every execution attempt gets its own caller-supplied digest.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from threading import Event, Thread
import time

from .contracts import InputManifest, MAX_DOCUMENT_BYTES, canonical_json_bytes, decode_canonical, digest, exact, integer
from .generations import (
    CertificationRequest, GenerationConflict, GenerationStore, StagedBuildStillCurrent,
    StagedBuildReadRecoveryRequired,
)
from .lifecycle_contracts import StructuralBuildRequest, payload_manifest_sha256
from .persistence import CommitUnknown, StateRecoveryRequired
from .pointers import PointerCAS
from .semantic_queue import SemanticQueueCorrupt

_LEASE_TTL_NS = 3_600_000_000_000


def _now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StructuralSyncRequest:
    repo_uuid: str
    generation_id: str
    desired_watermark: int
    build: StructuralBuildRequest

    def __post_init__(self):
        from .leases import LeaseStore
        LeaseStore._directory(self.repo_uuid)
        GenerationStore._lock_document(self.generation_id)
        integer(self.desired_watermark, minimum=1)
        if type(self.build) is not StructuralBuildRequest:
            raise GenerationConflict("validated structural request required")
        StructuralBuildRequest.from_mapping(self.build.to_dict())
        if self.build.logical_request_sha256 != self.identity(
            self.repo_uuid, self.generation_id, self.desired_watermark, self.build.to_dict(),
        ):
            raise GenerationConflict("logical request does not bind sync coordinates")

    def to_dict(self):
        return {"contract": "graphify.workspace.structural-sync.internal", "format_version": 1,
                "repo_uuid": self.repo_uuid, "generation_id": self.generation_id,
                "desired_watermark": self.desired_watermark, "build": self.build.to_dict()}

    @property
    def canonical(self):
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_json(cls, raw):
        value = decode_canonical(raw)
        exact(value, {"contract", "format_version", "repo_uuid", "generation_id", "desired_watermark", "build"})
        if (value["contract"] != "graphify.workspace.structural-sync.internal"
                or type(value["format_version"]) is not int or value["format_version"] != 1):
            raise GenerationConflict("unsupported structural sync request")
        return cls(value["repo_uuid"], value["generation_id"], value["desired_watermark"],
                   StructuralBuildRequest.from_mapping(value["build"]))

    @staticmethod
    def identity(repo_uuid, generation_id, watermark, build):
        fields = {k: v for k, v in build.items() if k != "logical_request_sha256"}
        return hashlib.sha256(canonical_json_bytes({
            "repo_uuid": repo_uuid, "generation_id": generation_id,
            "desired_watermark": watermark, "build": fields,
        })).hexdigest()


@dataclass(frozen=True)
class StructuralSyncResult:
    generation_id: str
    request_sha256: str
    receipt_sha256: str
    pointer_revision: int


def _observe(runtime, repo_uuid, manifest=None):
    stores = runtime.stores
    source = stores.registry.resolve_active_source(repo_uuid, read_only=True)
    first = runtime.adapter.observe_lifecycle(source.root, input_manifest=manifest)
    second = runtime.adapter.observe_lifecycle(source.root, input_manifest=manifest)
    if first != second or stores.registry.resolve_active_source(repo_uuid, read_only=True) != source:
        raise GenerationConflict("source authority changed during observation")
    return source, (first, second)


def _entry(registry, repo_uuid):
    found = [w for w in registry.to_dict()["workspaces"] if w["repo_uuid"] == repo_uuid]
    if len(found) != 1:
        raise GenerationConflict("one explicitly registered workspace required")
    return found[0]


def _queue_barrier(queue):
    if queue.items or (queue.reconciliation is not None and queue.reconciliation.semantic_required):
        raise GenerationConflict("existing semantic work blocks structural sync")


def _queue_request(queue, request):
    """Check reconciliation admission under the registry/workspace read locks."""
    if request.desired_watermark < queue.desired_watermark:
        raise GenerationConflict("desired watermark moved backward")
    if request.desired_watermark == queue.desired_watermark:
        bound = queue.reconciliation
        build = request.build
        if (queue.active_source_revision not in {None, build.expected_active_source_revision}
                or bound is None or bound.source_epoch != build.source_epoch
                or bound.policy_sha256 != build.policy_sha256
                or bound.source_observations.evidence_sha256 != build.observation_evidence_sha256):
            raise GenerationConflict("desired watermark is already bound to different source evidence")


def prepare_structural_sync(runtime, *, repo_uuid, generation_id, source_epoch,
                            desired_watermark, expected_payload_bytes):
    """Freeze a read-only request. Enrollment and activation are separate calls."""
    runtime.validate_authority()
    stores = runtime.stores
    source, observations = _observe(runtime, repo_uuid)
    observation = observations[0]
    document = stores.generations.structural_observation_document(observation)
    with stores.registry.read_only_snapshot() as registry:
        entry = _entry(registry, repo_uuid)
        if entry["active_source"] != source.registry_source:
            raise GenerationConflict("active source changed")
        with stores.leases.read_only_workspace_lock(repo_uuid):
            _semantic_barriers(stores, repo_uuid)
            lease = stores.leases.read_only_snapshot_locked(registry, repo_uuid)
            if lease.leases:
                raise GenerationConflict("active lease blocks structural preparation")
            queue = stores.queue.read_only_snapshot_locked(repo_uuid)
            _queue_barrier(queue)
            pointer = stores.pointers.load(repo_uuid, allow_missing=True)
            build = {
                "expected_registry_revision": registry.to_dict()["revision"],
                "expected_active_source_revision": entry["active_source_revision"],
                "expected_operation_epoch": lease.operation_epoch,
                "expected_migration_epoch": lease.migration_epoch,
                "expected_pointer_revision": 0 if pointer is None else pointer.to_dict()["pointer_revision"],
                "expected_current_receipt_sha256": None if pointer is None else pointer.to_dict()["current"]["receipt_sha256"],
                "source_epoch": source_epoch, "source_commit": observation.source_commit,
                "policy_sha256": observation.policy_sha256,
                "observation_manifest_sha256": observation.inventory_sha256,
                "observation_evidence_sha256": stores.generations.structural_observation_evidence_sha256(observations),
                "observation_detector_id": document["detector_id"],
                "observation_entries_sha256": document["entries_sha256"],
                "expected_payload_bytes": expected_payload_bytes,
                "capacity_policy_sha256": stores.capacity_policy.sha256,
                "compatibility_sha256": runtime.inputs.expected.sha256,
            }
            build["logical_request_sha256"] = StructuralSyncRequest.identity(repo_uuid, generation_id, desired_watermark, build)
            request = StructuralSyncRequest(repo_uuid, generation_id, desired_watermark,
                                            StructuralBuildRequest.from_mapping(build))
            _queue_request(queue, request)
            return request


def _semantic_barriers(stores, repo_uuid):
    root = stores.leases._directory(repo_uuid)
    for name in ("semantic-staging", "semantic-release-decisions"):
        if stores.generations.state.private_directory_exists(root / name):
            raise GenerationConflict("retained semantic lifecycle requires separate recovery")


def _recover_records(runtime, request, attempt_sha256):
    """Project and prove the exact request before acknowledging pending records."""
    from .semantic_queue import SemanticQueueSnapshot
    stores = runtime.stores
    with stores.registry.read_only_snapshot() as registry:
        with stores.leases.workspace_lock(request.repo_uuid):
            _semantic_barriers(stores, request.repo_uuid)
            staged, staged_pending = stores.generations._project_staged_build_recovery_locked(request.repo_uuid)
            if staged is None or staged.generation_id != request.generation_id or staged.request != request.build:
                raise GenerationConflict("pending state does not belong to this exact request")
            _lease, lease_pending = stores.leases.project_uncertain_snapshot_locked(registry, request.repo_uuid)
            if lease_pending and _lease.leases and _lease.staged_attempt_sha256 != attempt_sha256:
                raise GenerationConflict("pending lease belongs to a different caller attempt")
            queue = stores.queue
            current, previous, pending = queue._paths(request.repo_uuid)
            projection = queue.state.project_record_recovery(
                label="semantic_queue", current=current, previous=previous, pending=pending,
                decoder=SemanticQueueSnapshot.from_json, revision=lambda state: state.revision,
                allow_missing=True, max_bytes=queue.policy.max_bytes,
            )
            projected = projection.record
            if projected is not None:
                _queue_barrier(projected)
                if projected.repo_uuid != request.repo_uuid or projected.queue_policy != queue.policy:
                    raise GenerationConflict("pending queue authority differs")
                queue._bounded(projected)
                if projection.requires_recovery:
                    rec = projected.reconciliation
                    if (rec is None or projected.desired_watermark != request.desired_watermark
                            or rec.source_epoch != request.build.source_epoch
                            or rec.source_observations.evidence_sha256 != request.build.observation_evidence_sha256):
                        raise GenerationConflict("pending queue is not bound to the exact request")
            # All projections are fixed under this registry/workspace lock.
            if staged_pending:
                stores.generations._load_staged_build_locked(request.repo_uuid)
            if lease_pending:
                stores.leases._load_state_locked(registry, request.repo_uuid)
            if projection.requires_recovery:
                queue._load_locked(request.repo_uuid)


def _staged(runtime, request):
    stores = runtime.stores
    with stores.registry.read_only_snapshot() as registry:
        _entry(registry, request.repo_uuid)
        with stores.leases.read_only_workspace_lock(request.repo_uuid):
            _semantic_barriers(stores, request.repo_uuid)
            lease = stores.leases.read_only_snapshot_locked(registry, request.repo_uuid)
            queue = stores.queue.read_only_snapshot_locked(request.repo_uuid)
            _queue_barrier(queue)
            staged = stores.generations.read_only_staged_build_locked(request.repo_uuid, deadline_ns=None)
            if staged is not None and (staged.generation_id != request.generation_id
                                      or staged.request != request.build):
                if staged.lifecycle_state not in {"PROMOTED", "ABANDONED"}:
                    raise GenerationConflict("another exact request requires recovery")
                staged = None
            if staged is None:
                _queue_request(queue, request)
                if lease.leases:
                    raise GenerationConflict("active lease blocks a new structural request")
                # A later queue writer must acquire a lease and advance the epoch;
                # request_staged_build checks that epoch atomically before persisting.
            return staged


def _fault(runtime, request, boundary):
    runtime.stores.generations.fault_hook(f"sync:{request.generation_id}:{boundary}")


@contextmanager
def _released(runtime, grant):
    primary = None
    try:
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            runtime.stores.leases.release(grant)
        except CommitUnknown:
            raise
        except Exception:
            if primary is None:
                raise


def _acquire(runtime, request, attempt_sha256, *, recovering):
    method = (runtime.stores.generations.acquire_staged_recovery if recovering
              else runtime.stores.generations.acquire_staged_operation)
    options = {} if recovering else {"operation": "BUILD"}
    return method(request.repo_uuid, request.generation_id, request.build,
                  attempt_sha256=attempt_sha256, acquired_at=_now(),
                  monotonic_ns=time.monotonic_ns(), ttl_ns=_LEASE_TTL_NS, **options)


@contextmanager
def _heartbeat(runtime, grant):
    stop = Event()
    errors = []
    def beat():
        while not stop.wait(_LEASE_TTL_NS / 3e9):
            try:
                runtime.stores.leases.heartbeat(grant, heartbeat_at=_now(),
                    monotonic_ns=time.monotonic_ns(), ttl_ns=_LEASE_TTL_NS)
            except BaseException as exc:
                errors.append(exc)
                return
    thread = Thread(target=beat, name="graphify-structural-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
    if errors:
        raise errors[0]
    runtime.stores.leases.assert_current(grant, monotonic_ns=time.monotonic_ns())


def _build(runtime, request, preparation, source, initial):
    stores = runtime.stores
    state = stores.generations.state
    relative = preparation.staging_path.relative_to(state.root)
    with stores.leases.current_operation(preparation.grant, monotonic_ns=time.monotonic_ns(),
                                        allowed_operations=frozenset({"BUILD"})):
        with state.existing_generation_lock(stores.generations._lock(request.repo_uuid, request.generation_id),
                                            generation_id=request.generation_id, exclusive=True):
            state.ensure_directory(relative / "graphify-out")
    with state.existing_private_directory(relative) as scratch:
        with state.existing_private_directory(relative / "graphify-out") as payload:
            @contextmanager
            def guard():
                runtime.validate_authority()
                with stores.leases.current_operation(preparation.grant, monotonic_ns=time.monotonic_ns(),
                                                    allowed_operations=frozenset({"BUILD"})):
                    with state.existing_generation_lock(stores.generations._lock(request.repo_uuid, request.generation_id),
                                                        generation_id=request.generation_id, exclusive=True):
                        with state.existing_private_directory(relative / "graphify-out") as live:
                            if (os.fstat(live).st_dev, os.fstat(live).st_ino) != (os.fstat(payload).st_dev, os.fstat(payload).st_ino):
                                raise GenerationConflict("staging descriptor was replaced")
                            yield
            with _heartbeat(runtime, preparation.grant):
                return runtime.adapter.build_structural(source.root, payload_fd=payload,
                    scratch_fd=scratch, initial_detection=initial, write_guard=guard)


def _manifest(stores, request, *, certified=False):
    directory = (stores.generations._generation if certified else stores.generations._staging)(
        request.repo_uuid, request.generation_id)
    raw = stores.generations.state.read_contained_regular_file(
        directory, "graphify-out/input-manifest.json", max_bytes=MAX_DOCUMENT_BYTES,
        allowed_directory_modes=frozenset({0o700, 0o500}), allowed_file_modes=frozenset({0o600, 0o400}),
    )
    return InputManifest.from_json(raw)


def _success(runtime, request, staged):
    runtime.validate_authority()
    stores = runtime.stores
    with stores.registry.read_only_snapshot() as registry:
        with stores.leases.read_only_workspace_lock(request.repo_uuid):
            lease = stores.leases.read_only_snapshot_locked(registry, request.repo_uuid)
            if lease.leases:
                raise GenerationConflict("terminal lease cleanup remains pending")
            if (_entry(registry, request.repo_uuid)["active_source_revision"]
                    != request.build.expected_active_source_revision
                    or lease.migration_epoch != request.build.expected_migration_epoch):
                raise GenerationConflict("terminal source authority changed")
        with stores.pointers.read_current(request.repo_uuid) as reading:
            value = reading.pointer.to_dict()
            if (staged.lifecycle_state != "PROMOTED" or reading.receipt.sha256 != staged.receipt_sha256
                    or value["current"]["generation_id"] != request.generation_id
                    or value["pointer_revision"] != staged.pointer_revision):
                raise GenerationConflict("request is not the visible promoted result")
            return StructuralSyncResult(request.generation_id, request.build.sha256,
                                        reading.receipt.sha256, staged.pointer_revision)


def synchronize_structural(runtime, request, *, attempt_sha256):
    """Execute/recover exactly one library request; never infer a replacement."""
    if type(request) is not StructuralSyncRequest:
        raise GenerationConflict("validated sync request required")
    request.__post_init__()
    digest(attempt_sha256)
    runtime.validate_authority()
    stores = runtime.stores
    if (request.build.compatibility_sha256 != runtime.inputs.expected.sha256
            or request.build.capacity_policy_sha256 != stores.capacity_policy.sha256):
        raise GenerationConflict("sync candidate or capacity authority differs")
    try:
        staged = _staged(runtime, request)
    except (StagedBuildReadRecoveryRequired, StateRecoveryRequired, SemanticQueueCorrupt) as exc:
        if isinstance(exc, SemanticQueueCorrupt) and not isinstance(exc.__cause__, StateRecoveryRequired):
            raise
        _recover_records(runtime, request, attempt_sha256)
        staged = _staged(runtime, request)
    if staged is not None and staged.lifecycle_state == "ABANDONED":
        raise GenerationConflict("exact request was abandoned")
    if staged is not None and staged.lifecycle_state == "PROMOTED":
        # S3 validates terminal identity before admitting exact cleanup.
        with stores.registry.read_only_snapshot() as registry:
            with stores.leases.read_only_workspace_lock(request.repo_uuid):
                pending = stores.leases.read_only_snapshot_locked(registry, request.repo_uuid).leases
        if pending:
            attempt = _acquire(runtime, request, attempt_sha256, recovering=True)
            with _released(runtime, attempt.grant):
                pass
        return _success(runtime, request, staged)
    recovering = staged is not None
    if staged is None:
        _source, observations = _observe(runtime, request.repo_uuid)
        staged = stores.generations.request_staged_build(request.repo_uuid, request.generation_id,
                                                        request.build, source_observations=observations)
        _fault(runtime, request, "request_staged")
    if staged.lifecycle_state != "CERTIFIED":
        attempt = _acquire(runtime, request, attempt_sha256, recovering=recovering)
        with _released(runtime, attempt.grant):
            _fault(runtime, request, "build_acquired")
            # Certification binding is an already-authorized durable boundary.
            # Finish it before consulting potentially newer source contents.
            certification = stores.queue._certification_binding_path(request.repo_uuid, request.generation_id)
            if staged.lifecycle_state == "COMPLETE" and stores.queue.state.private_file_exists(certification):
                staged = stores.generations.recover_staged_certification(attempt, monotonic_ns=time.monotonic_ns())
            else:
                source, observations = _observe(runtime, request.repo_uuid)
                if recovering:
                    try:
                        stores.generations.abandon_staged_build(attempt, source_observations=observations,
                                                               monotonic_ns=time.monotonic_ns())
                    except StagedBuildStillCurrent:
                        pass
                    else:
                        raise GenerationConflict("stale exact request was abandoned")
                allocation = stores.generations.allocate(attempt.grant,
                    expected_payload_bytes=request.build.expected_payload_bytes,
                    capacity_policy=stores.capacity_policy, generation_id=request.generation_id,
                    occurred_at=_now(), monotonic_ns=time.monotonic_ns())
                _fault(runtime, request, "generation_allocated")
                preparation = stores.generations.prepare_staged_build(attempt, allocation, monotonic_ns=time.monotonic_ns())
                _fault(runtime, request, "staging_prepared")
                if preparation.state.lifecycle_state != "COMPLETE":
                    built = _build(runtime, request, preparation, source, observations[0].initial_detection)
                    manifest = built.input_manifest
                    _fault(runtime, request, "adapter_built")
                else:
                    manifest = _manifest(stores, request)
                _source, final = _observe(runtime, request.repo_uuid, manifest)
                runtime.validate_authority()
                completion = stores.generations.complete_staged_build(preparation,
                    source_observations=final, monotonic_ns=time.monotonic_ns())
                _fault(runtime, request, "staging_completed")
                _queue_barrier(stores.queue.inspect(request.repo_uuid))
                queue = stores.queue.reconcile(attempt.grant, (), source_epoch=request.build.source_epoch,
                    policy_sha256=request.build.policy_sha256, source_observations=final,
                    desired_watermark=request.desired_watermark, semantic_required=False,
                    monotonic_ns=time.monotonic_ns())
                _fault(runtime, request, "queue_reconciled")
                stores.queue.bind_sealed_inputs(attempt.grant,
                    sealed_input_manifest_sha256=payload_manifest_sha256("graphify-out", completion.entries),
                    monotonic_ns=time.monotonic_ns())
                _fault(runtime, request, "sealed_inputs_bound")
                stores.generations.certify(attempt.grant, completion.allocation,
                    CertificationRequest(source_commit=request.build.source_commit,
                        source_epoch=request.build.source_epoch, policy_sha256=request.build.policy_sha256,
                        observation_manifest_sha256=request.build.observation_manifest_sha256,
                        queue_watermark=queue.desired_watermark, semantic_completeness="not_required",
                        compatibility_sha256=request.build.compatibility_sha256,
                        validations=("payload_manifest", "coordination_lock_precreated", "stable_semantic_queue")),
                    source_observations=final, declared_entries=completion.entries, staged_completion=completion,
                    occurred_at=_now(), monotonic_ns=time.monotonic_ns())
                _fault(runtime, request, "generation_certified")
        _fault(runtime, request, "build_released")
    runtime.validate_authority()
    receipt = stores.generations.verify_generation(request.repo_uuid, request.generation_id)
    attempt = _acquire(runtime, request, attempt_sha256, recovering=True)
    with _released(runtime, attempt.grant):
        _fault(runtime, request, "promotion_acquired")
        if attempt.grant.lease.to_dict()["operation"] == "POINTER_RECOVERY":
            pointer = stores.pointers.recover(attempt.grant, occurred_at=_now(), monotonic_ns=time.monotonic_ns())
        else:
            pointer = stores.pointers.load(request.repo_uuid, allow_missing=True)
            current = None if pointer is None else pointer.to_dict()["current"]
            if current != {"generation_id": request.generation_id, "receipt_sha256": receipt.sha256}:
                manifest = _manifest(stores, request, certified=True)
                _source, observations = _observe(runtime, request.repo_uuid, manifest)
                try:
                    stores.generations.abandon_staged_build(attempt, source_observations=observations,
                                                           monotonic_ns=time.monotonic_ns())
                except StagedBuildStillCurrent:
                    pass
                else:
                    raise GenerationConflict("stale certified request was abandoned")
                pointer = stores.pointers.promote(attempt.grant, PointerCAS(
                    expected_pointer_revision=request.build.expected_pointer_revision,
                    expected_active_source_revision=attempt.grant.active_source_revision,
                    expected_source_epoch=request.build.source_epoch,
                    expected_operation_epoch=attempt.grant.operation_epoch,
                    expected_migration_epoch=attempt.grant.migration_epoch,
                    expected_state_schema_version=2,
                    expected_fence_token=attempt.grant.lease.to_dict()["fence_token"],
                    candidate_generation_id=request.generation_id, candidate_receipt_sha256=receipt.sha256,
                    expected_current_receipt_sha256=request.build.expected_current_receipt_sha256),
                    occurred_at=_now(), monotonic_ns=time.monotonic_ns())
        _fault(runtime, request, "pointer_moved")
        staged = stores.generations.complete_staged_promotion(attempt, pointer, monotonic_ns=time.monotonic_ns())
        _fault(runtime, request, "promotion_completed")
    _fault(runtime, request, "promotion_released")
    return _success(runtime, request, staged)
