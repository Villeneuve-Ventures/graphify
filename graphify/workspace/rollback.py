"""Exact-last-good rollback admission over the existing lifecycle machinery."""
from datetime import datetime, timezone
import time

from .cli_contracts import rollback_parameters
from .leases import LeaseBusy
from .persistence import require_before_deadline
from .pointers import PointerCAS, PointerConflict
from .sync import _entry, _semantic_barriers


def _matches_prior(pointer, parameters, target):
    value = pointer.to_dict()
    return (value["pointer_revision"] == parameters["expected_pointer_revision"]
        and value["active_source_revision"] == parameters["expected_active_source_revision"]
        and value["current"]["receipt_sha256"] == parameters["expected_current_receipt_sha256"]
        and value["last_good"] == target)


def _inspect(runtime, p, *, deadline_ns):
    """Refuse stale inputs before lease allocation; admit only a complete replay."""
    stores = runtime.stores
    repo_uuid = p["repo_uuid"]
    pointers = stores.pointers
    target = {"generation_id": p["target_generation_id"], "receipt_sha256": p["target_receipt_sha256"]}
    with stores.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        entry = _entry(registry, repo_uuid)
        with stores.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            _semantic_barriers(stores, repo_uuid)
            lease = stores.leases.read_only_snapshot_locked(registry, repo_uuid, deadline_ns=deadline_ns)
            stores.leases._assert_recovery_barriers_locked(
                repo_uuid, "ROLLBACK", recover=False, deadline_ns=deadline_ns)
            if lease.leases:
                raise LeaseBusy("rollback requires an unoccupied workspace")
            if (registry.to_dict()["revision"] != p["expected_registry_revision"]
                    or entry["active_source_revision"] != p["expected_active_source_revision"]
                    or lease.migration_epoch != p["expected_migration_epoch"]):
                raise PointerConflict("rollback registry/source/migration authority is stale")
            stores.registry.resolve_active_source_locked(registry, repo_uuid, deadline_ns=deadline_ns)
            current = pointers._preliminary_pointer(repo_uuid, deadline_ns=deadline_ns)
            if current is None:
                raise PointerConflict("rollback requires an existing last_good")
            with pointers.state.existing_generation_locks(
                    pointers._lock_set(repo_uuid, p["target_generation_id"], current),
                    exclusive=False, deadline_ns=deadline_ns):
                pointers._verify_visible_pointer_journal(repo_uuid, current, deadline_ns=deadline_ns)
                candidate = pointers._verify_ref(repo_uuid, target, deadline_ns=deadline_ns)
                if (candidate.to_dict()["active_source_revision"] != p["expected_active_source_revision"]
                        or candidate.to_dict()["source_epoch"] != p["expected_source_epoch"]):
                    raise PointerConflict("rollback target source authority differs")
                journal = stores.journal.read_stable(repo_uuid, deadline_ns=deadline_ns)
                if not pointers._journal_certifies(journal,
                        generation_id=p["target_generation_id"], receipt_sha256=candidate.sha256,
                        allow_superseded=True):
                    raise PointerConflict("rollback target has no certified journal authority")
                value = current.to_dict()
                fresh = (lease.operation_epoch == p["expected_operation_epoch"]
                    and lease.fence_high_watermark == p["expected_fence_high_watermark"]
                    and _matches_prior(current, p, target))
                replay = False
                if (lease.operation_epoch == p["expected_operation_epoch"] + 1
                        and lease.fence_high_watermark == p["expected_fence_high_watermark"] + 1
                        and value["pointer_revision"] == p["expected_pointer_revision"] + 1
                        and value["operation_epoch"] == lease.operation_epoch
                        and value["fence_token"] == lease.fence_high_watermark
                        and value["active_source_revision"] == p["expected_active_source_revision"]
                        and value["source_epoch"] == p["expected_source_epoch"]
                        and value["current"] == target):
                    prior = pointers.retained_prior(repo_uuid, deadline_ns=deadline_ns)
                    if prior is not None and prior.to_dict()["replaced_by_revision"] == value["pointer_revision"]:
                        from .lifecycle_contracts import PointerSet
                        previous = PointerSet.from_mapping(prior.to_dict()["pointer_set"])
                        replay = (_matches_prior(previous, p, target)
                            and pointers._journal_records_pointer(journal, current,
                                transition="ROLLED_BACK", deadline_ns=deadline_ns)
                            and not any(event.to_dict()["pointer_revision"] is not None
                                and event.to_dict()["pointer_revision"] > value["pointer_revision"]
                                for event in journal.events))
                if not fresh and not replay:
                    raise PointerConflict("rollback pointer/epoch/fence/target coordinates are stale")
                runtime.validate_authority(deadline_ns=deadline_ns)
                require_before_deadline(deadline_ns, "rollback inspection expired")
                return current if replay else None


def rollback_structural(runtime, parameters, *, deadline_ns):
    rollback_parameters(parameters)
    require_before_deadline(deadline_ns, "rollback deadline expired")
    runtime.validate_authority(deadline_ns=deadline_ns)
    prior_result = _inspect(runtime, parameters, deadline_ns=deadline_ns)
    if prior_result is not None:
        return prior_result
    p = parameters
    stores = runtime.stores
    now = time.monotonic_ns()
    require_before_deadline(deadline_ns, "rollback lease admission expired")
    grant = stores.leases.acquire(p["repo_uuid"], "ROLLBACK", stores.leases.current_owner(),
        **{key: p[key] for key in ("expected_registry_revision", "expected_active_source_revision",
            "expected_operation_epoch", "expected_migration_epoch")},
        acquired_at=datetime.now(timezone.utc), monotonic_ns=now,
        ttl_ns=deadline_ns - now, deadline_ns=deadline_ns)
    cas = PointerCAS(p["expected_pointer_revision"], p["expected_active_source_revision"],
        p["expected_source_epoch"], grant.operation_epoch, grant.migration_epoch,
        2, grant.lease.to_dict()["fence_token"], p["target_generation_id"],
        p["target_receipt_sha256"], p["expected_current_receipt_sha256"])
    runtime.validate_authority(deadline_ns=deadline_ns)
    result = stores.pointers.rollback(grant, cas, occurred_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(), deadline_ns=deadline_ns)
    # Exceptions leave their durable lease/intent for the existing recovery path.
    # Killing a worker, or failing to acknowledge release, cannot prove no effects.
    stores.leases.release(grant, deadline_ns=deadline_ns)
    runtime.validate_authority(deadline_ns=deadline_ns)
    require_before_deadline(deadline_ns, "rollback completion expired")
    return result
