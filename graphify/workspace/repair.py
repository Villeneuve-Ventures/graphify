"""Approved public pointer repair over existing read-only admission and fencing."""
from datetime import datetime, timezone
import time

from .cli_contracts import RepairApproval, repair_parameters
from .contracts import canonical_sha256
from .journal import JournalError
from .leases import LeaseBusy
from .persistence import CommitUnknown, require_before_deadline
from .pointers import PointerConflict, PointerCorrupt
from .semantic_queue import SemanticQueueError
from .sync import _entry, _semantic_barriers


def _preview(runtime, repo_uuid, *, deadline_ns):
    stores = runtime.stores
    with stores.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        entry = _entry(registry, repo_uuid)
        with stores.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            _semantic_barriers(stores, repo_uuid)
            stores.queue.read_only_snapshot_locked(repo_uuid, deadline_ns=deadline_ns)
            lease = stores.leases.read_only_snapshot_locked(registry, repo_uuid, deadline_ns=deadline_ns)
            stores.leases._assert_recovery_barriers_locked(
                repo_uuid, "REPAIR", recover=False, deadline_ns=deadline_ns)
            if lease.leases:
                raise LeaseBusy("repair requires an unoccupied workspace")
            stores.registry.resolve_active_source_locked(registry, repo_uuid, deadline_ns=deadline_ns)
            if lease.operation_epoch == 2**63 - 1 or lease.fence_high_watermark == 2**63 - 1:
                raise PointerConflict("repair authority has no increment headroom")
            # Analyze the exact proposed grant, including pending-pointer ownership.
            plan = stores.pointers.analyze_repair(repo_uuid,
                active_source_revision=entry["active_source_revision"],
                operation_epoch=lease.operation_epoch + 1,
                fence_token=lease.fence_high_watermark + 1, deadline_ns=deadline_ns)
            result_epoch, result_fence = lease.operation_epoch + 1, lease.fence_high_watermark + 1
            if plan.pointer_action in {"none", "finalize_pending"}:
                current, _ = stores.pointers._read_repair_pointer(stores.pointers._current(repo_uuid),
                    repo_uuid=repo_uuid, allow_missing=False, allow_invalid=False, deadline_ns=deadline_ns)
                if current is None:
                    raise PointerConflict("repair requires its analyzed visible pointer")
                value = current.to_dict()
                result_epoch, result_fence = value["operation_epoch"], value["fence_token"]
            target = plan.candidate
            lock = stores.generations._lock(repo_uuid, target["generation_id"])
            with stores.pointers.state.existing_generation_locks(
                    [(target["generation_id"], lock)], exclusive=False, deadline_ns=deadline_ns):
                receipt = stores.pointers._verify_ref(repo_uuid, target, deadline_ns=deadline_ns)
            journal = stores.journal.project_recovery(repo_uuid, deadline_ns=deadline_ns).snapshot
            if any(event.to_dict()["operation_epoch"] > lease.operation_epoch
                    or event.to_dict()["fence_token"] > lease.fence_high_watermark
                    for event in journal.events):
                raise PointerCorrupt("lease authority precedes durable pointer history")
            approval = RepairApproval.from_mapping({
                "repo_uuid": repo_uuid,
                "state_root_sha256": canonical_sha256(str(runtime.inputs.state_root)),
                "compatibility_sha256": runtime.inputs.expected.sha256,
                "active_source_sha256": canonical_sha256(entry["active_source"]),
                "coordinates": {"registry_revision": registry.to_dict()["revision"],
                    "active_source_revision": entry["active_source_revision"],
                    "operation_epoch": lease.operation_epoch, "migration_epoch": lease.migration_epoch,
                    "fence_high_watermark": lease.fence_high_watermark,
                    "source_epoch": receipt.to_dict()["source_epoch"],
                    "result_operation_epoch": result_epoch, "result_fence_token": result_fence},
                "plan": plan.to_dict(),
            })
            runtime.validate_authority(deadline_ns=deadline_ns)
            require_before_deadline(deadline_ns, "repair preview expired")
            return approval, plan


def repair_structural(runtime, parameters, *, deadline_ns):
    repair_parameters(parameters)
    require_before_deadline(deadline_ns, "repair deadline expired")
    runtime.validate_authority(deadline_ns=deadline_ns)
    try:
        approval, plan = _preview(runtime, parameters["repo_uuid"], deadline_ns=deadline_ns)
    except (JournalError, SemanticQueueError) as exc:
        raise PointerConflict("repair requires readable recovery authority") from exc
    if parameters["operation"] == "preview":
        return {"approval": approval.to_dict()}
    if approval.to_dict() != parameters["approval"]:
        raise PointerConflict("approved repair evidence changed")
    coordinates = approval.to_dict()["coordinates"]
    stores = runtime.stores
    now = time.monotonic_ns()
    require_before_deadline(deadline_ns, "repair lease admission expired")
    grant = stores.leases.acquire(parameters["repo_uuid"], "REPAIR", stores.leases.current_owner(),
        **{"expected_" + key: coordinates[key] for key in ("registry_revision",
            "active_source_revision", "operation_epoch", "migration_epoch")},
        acquired_at=datetime.now(timezone.utc), monotonic_ns=now,
        ttl_ns=deadline_ns - now, deadline_ns=deadline_ns)
    try:
        if grant.lease.to_dict()["fence_token"] != coordinates["fence_high_watermark"] + 1:
            raise PointerConflict("repair fence changed during admission")
        runtime.validate_authority(deadline_ns=deadline_ns)
        # recover re-analyzes with registry/workspace/exclusive generation locks,
        # compares the complete plan and private decision binding, then mutates.
        result = stores.pointers.recover(grant, expected_plan=plan,
            occurred_at=datetime.now(timezone.utc), monotonic_ns=time.monotonic_ns(),
            deadline_ns=deadline_ns)
        stores.leases.release(grant, deadline_ns=deadline_ns)
        runtime.validate_authority(deadline_ns=deadline_ns)
        require_before_deadline(deadline_ns, "repair completion expired")
        return {"pointer": result.to_dict()}
    except Exception as exc:
        raise CommitUnknown("repair completion could not be acknowledged") from exc
