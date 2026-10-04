"""Bounded, existing-only structural diagnostics with public-safe projections."""
from __future__ import annotations

import hashlib

from graphify.source_io import SourceError

from .adapters.base import QueryRequest
from .contracts import InputManifest, MAX_DOCUMENT_BYTES
from .leases import LeaseRecoveryRequired
from .lifecycle_contracts import WorkspaceLeaseState
from .persistence import LockTimeout, StateRecoveryRequired, require_before_deadline
from .sync import _entry, _semantic_barriers


class _Stale(Exception):
    """The certified source no longer matches current observation authority."""


def _result(state, reason_code, **fields):
    return {"state": state, "safe_to_query": state == "current",
            "reason_code": reason_code, **fields}


def inspect_structural(runtime, repo_uuid, *, deadline_ns, doctor=False):
    """Observe one workspace without creating locks, recovering, or repairing.

    Both modes verify the certified generation and before/after source evidence.
    Doctor additionally consumes its current graph through the qualified engine.
    A failed read discards all provisional fields; exception text is never public.
    """
    try:
        if type(deadline_ns) is not int or deadline_ns <= 0 or type(doctor) is not bool:
            return _result("unavailable", "invalid_request")
        WorkspaceLeaseState.canonical_repo_uuid(repo_uuid)
        require_before_deadline(deadline_ns, "diagnostic deadline expired")
        return _inspect(runtime, repo_uuid, deadline_ns=deadline_ns, doctor=doctor)
    except _Stale:
        return _result("stale", "source_stale")
    except (LeaseRecoveryRequired, StateRecoveryRequired):
        return _result("pending", "recovery_required")
    except LockTimeout:
        return _result("unavailable", "deadline_exceeded")
    except Exception:
        return _result("unavailable", "diagnostic_unavailable")


def _inspect(runtime, repo_uuid, *, deadline_ns, doctor):
    runtime.validate_authority(deadline_ns=deadline_ns)
    stores = runtime.stores
    with stores.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        entry = _entry(registry, repo_uuid)
        with stores.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            _semantic_barriers(stores, repo_uuid)
            lease = stores.leases.read_only_snapshot_locked(
                registry, repo_uuid, deadline_ns=deadline_ns)
            staged = stores.generations.read_only_staged_build_locked(
                repo_uuid, deadline_ns=deadline_ns)
            queue = stores.queue.read_only_snapshot_locked(repo_uuid, deadline_ns=deadline_ns)
            fields = {
                "registry_revision": registry.to_dict()["revision"],
                "active_source_revision": entry["active_source_revision"],
                "operation_epoch": lease.operation_epoch,
                "migration_epoch": lease.migration_epoch,
                "desired_watermark": queue.desired_watermark,
                "staged_lifecycle_state": None if staged is None else staged.lifecycle_state,
                "lease_states": {domain: value.to_dict()["operation"]
                                 for domain, value in sorted(lease.leases.items())},
            }
            try:
                stores.leases._assert_recovery_barriers_locked(
                    repo_uuid, "QUERY", recover=False, deadline_ns=deadline_ns)
            except LeaseRecoveryRequired:
                return _finish(runtime, "pending", "recovery_required", fields, deadline_ns)
            if (lease.leases or queue.items
                    or (queue.reconciliation is not None and queue.reconciliation.semantic_required)
                    or (staged is not None and staged.lifecycle_state not in {"PROMOTED", "ABANDONED"})):
                return _finish(runtime, "pending", "lifecycle_pending", fields, deadline_ns)
            if not stores.pointers.state.private_file_exists(stores.pointers._current(repo_uuid)):
                if (staged is not None and staged.lifecycle_state == "PROMOTED"
                        or stores.pointers.state.private_file_exists(stores.pointers._prior(repo_uuid))):
                    raise ValueError("promoted pointer is missing")
                return _finish(runtime, "unbuilt", "generation_absent", fields, deadline_ns)
            with stores.pointers.read_current(repo_uuid, deadline_ns=deadline_ns) as reading:
                value = reading.receipt.to_dict()
                pointer = reading.pointer.to_dict()
                if (entry["active_source_revision"] != pointer["active_source_revision"]
                        or (staged is not None and staged.generation_id == value["generation_id"]
                            and lease.migration_epoch != staged.request.expected_migration_epoch)
                        or (lease.migration_epoch != 0 and (staged is None
                            or staged.generation_id != value["generation_id"]))):
                    raise _Stale
                relative = reading.generation_path.relative_to(stores.generations.state.root)
                raw = stores.generations.state.read_contained_regular_file(
                    relative, "graphify-out/input-manifest.json", max_bytes=MAX_DOCUMENT_BYTES,
                    allowed_directory_modes=frozenset({0o700, 0o500}),
                    allowed_file_modes=frozenset({0o600, 0o400}), deadline_ns=deadline_ns)
                manifest = InputManifest.from_json(raw)
                binding = value["completion_binding"]
                if hashlib.sha256(raw).hexdigest() != binding["consumed_inputs_sha256"]:
                    raise ValueError("certified consumed-input binding differs")
                source = stores.registry.resolve_active_source_locked(
                    registry, repo_uuid, deadline_ns=deadline_ns)

                def observe():
                    try:
                        result = runtime.adapter.observe_lifecycle(
                            source.root, input_manifest=manifest, deadline_ns=deadline_ns)
                    except SourceError:
                        # Replay can refuse changed consumed bytes before yielding
                        # an observation. A separate complete detection distinguishes
                        # known source drift from unavailable source evidence.
                        require_before_deadline(deadline_ns, "diagnostic deadline expired")
                        detected = runtime.adapter.observe_lifecycle(source.root, deadline_ns=deadline_ns)
                        if (detected.source_commit != value["source_commit"]
                                or detected.policy_sha256 != value["policy_sha256"]
                                or detected.inventory_sha256 != binding["initial_detection_sha256"]):
                            raise _Stale from None
                        raise
                    if (result.source_commit != value["source_commit"]
                            or result.policy_sha256 != value["policy_sha256"]
                            or result.inventory_sha256 != binding["initial_detection_sha256"]
                            or result.consumed_inputs != manifest):
                        raise _Stale
                    return result

                before = observe()
                if doctor:
                    with stores.generations.state.existing_private_directory(relative / "graphify-out") as payload:
                        runtime.adapter.query_structural(
                            payload, QueryRequest("graphify"), deadline_ns=deadline_ns)
                if (observe() != before or stores.registry.resolve_active_source_locked(
                        registry, repo_uuid, deadline_ns=deadline_ns) != source):
                    raise _Stale
                stores.pointers.revalidate_read(repo_uuid, reading, deadline_ns=deadline_ns)
                fields.update(pointer_revision=pointer["pointer_revision"],
                              generation_id=value["generation_id"],
                              receipt_sha256=reading.receipt.sha256)
                return _finish(runtime, "current", "current", fields, deadline_ns)


def _finish(runtime, state, reason_code, fields, deadline_ns):
    runtime.validate_authority(deadline_ns=deadline_ns)
    require_before_deadline(deadline_ns, "diagnostic deadline expired")
    return _result(state, reason_code, **fields)
