"""Buffered, existing-only certified structural queries under GC protection."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import time

from .adapters.base import QueryRequest, QueryRejected
from .contracts import InputManifest, MAX_DOCUMENT_BYTES
from .persistence import require_before_deadline
from .sync import _entry, _queue_barrier, _semantic_barriers


def query_structural(runtime, repo_uuid, request, *, deadline_ns=None):
    """Return text only after before/after source and pointer validation.

    The embedding process must start with -B/PYTHONDONTWRITEBYTECODE=1 before
    importing candidate modules. This library never creates a query launcher.
    """
    if type(request) is not QueryRequest:
        raise QueryRejected("validated query request required")
    request = replace(request)
    if deadline_ns is None:
        deadline_ns = time.monotonic_ns() + 30_000_000_000
    require_before_deadline(deadline_ns, "query deadline expired")
    runtime.validate_authority()
    stores = runtime.stores
    with stores.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
        entry = _entry(registry, repo_uuid)
        # Retain workspace protection as well as generation protection: no
        # queue/staged/epoch transition may slip across the output boundary.
        with stores.leases.read_only_workspace_lock(repo_uuid, deadline_ns=deadline_ns):
            _semantic_barriers(stores, repo_uuid)
            lease = stores.leases.read_only_snapshot_locked(registry, repo_uuid, deadline_ns=deadline_ns)
            stores.leases._assert_recovery_barriers_locked(
                repo_uuid, "QUERY", recover=False, deadline_ns=deadline_ns,
            )
            staged = stores.generations.read_only_staged_build_locked(repo_uuid, deadline_ns=deadline_ns)
            if lease.leases or (staged is not None and staged.lifecycle_state not in {"PROMOTED", "ABANDONED"}):
                raise QueryRejected("workspace has pending lifecycle authority")
            queue = stores.queue.read_only_snapshot_locked(repo_uuid, deadline_ns=deadline_ns)
            _queue_barrier(queue)
            with stores.pointers.read_current(repo_uuid, deadline_ns=deadline_ns) as reading:
                value = reading.receipt.to_dict()
                pointer = reading.pointer.to_dict()
                if (entry["active_source_revision"] != pointer["active_source_revision"]
                        or (staged is not None and staged.generation_id == value["generation_id"]
                            and lease.migration_epoch != staged.request.expected_migration_epoch)
                        or (lease.migration_epoch != 0 and (staged is None
                            or staged.generation_id != value["generation_id"]))):
                    raise QueryRejected("certified source authority is no longer current")
                relative = reading.generation_path.relative_to(stores.generations.state.root)
                raw = stores.generations.state.read_contained_regular_file(
                    relative, "graphify-out/input-manifest.json", max_bytes=MAX_DOCUMENT_BYTES,
                    allowed_directory_modes=frozenset({0o700, 0o500}),
                    allowed_file_modes=frozenset({0o600, 0o400}), deadline_ns=deadline_ns,
                )
                manifest = InputManifest.from_json(raw)
                binding = value["completion_binding"]
                if hashlib.sha256(raw).hexdigest() != binding["consumed_inputs_sha256"]:
                    raise QueryRejected("certified consumed-input binding differs")
                source = stores.registry.resolve_active_source_locked(registry, repo_uuid, deadline_ns=deadline_ns)
                def observe():
                    result = runtime.adapter.observe_lifecycle(source.root, input_manifest=manifest, deadline_ns=deadline_ns)
                    if (result.source_commit != value["source_commit"]
                            or result.policy_sha256 != value["policy_sha256"]
                            or result.inventory_sha256 != binding["initial_detection_sha256"]
                            or result.consumed_inputs != manifest):
                        raise QueryRejected("certified source is stale")
                    return result
                before = observe()
                with stores.generations.state.existing_private_directory(relative / "graphify-out") as payload:
                    text = runtime.adapter.query_structural(payload, request)
                if observe() != before or stores.registry.resolve_active_source_locked(
                    registry, repo_uuid, deadline_ns=deadline_ns,
                ) != source:
                    raise QueryRejected("source changed during traversal")
                stores.pointers.revalidate_read(repo_uuid, reading, deadline_ns=deadline_ns)
                runtime.validate_authority()
                require_before_deadline(deadline_ns, "query deadline expired")
                return text
