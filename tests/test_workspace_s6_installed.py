"""Cold installed rollback through the unchanged bounded worker on native APFS."""
import hashlib
import json
import sys

import pytest

from graphify.workspace.lifecycle_contracts import (
    PointerSet, PriorPointerRecord, WorkspaceLeaseState, decode_journal_frame,
)
from tests.test_workspace_s5_installed import installed_candidate, PublicClient, authorization  # noqa: F401
from tests.workspace_s3_helpers import REPO_UUID, create_repo, tree_snapshot


@pytest.mark.skipif(sys.platform != "darwin", reason="native rollback requires non-elevated macOS/APFS")
def test_installed_repaired_rollback_requires_exact_prior(installed_candidate):  # noqa: F811
    import subprocess
    from graphify.workspace.contracts import canonical_json_bytes
    root, _, _, _, bundle, _ = installed_candidate
    source = create_repo(root / "s6-repaired-source")
    (source / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    state = root / "s6-repaired-state"
    state.mkdir(mode=0o700)
    authority = state / "runtime-manifest.json"
    authority.write_bytes((bundle / "runtime-manifest.json").read_bytes())
    authority.chmod(0o600)
    client = PublicClient(installed_candidate, state)
    enrolled = client.run("register", {"operation": "enroll", "source_root": str(source),
        "authorization": authorization("ENROLL"), "expected_registry_revision": 0})
    workspace = state / "workspaces" / REPO_UUID
    pointer, prior = workspace / "pointers.json", workspace / "pointers.previous.json"
    saved_prior = None
    for epoch in range(1, 4):
        prepared = client.run("sync", {"operation": "prepare", "repo_uuid": REPO_UUID,
            "generation_id": f"gen-repaired-{epoch}", "source_epoch": epoch,
            "desired_watermark": epoch, "expected_payload_bytes": 1024 * 1024})
        client.run("sync", {"operation": "execute", "sync_request": prepared["sync_request"],
            "attempt_sha256": str(epoch) * 64}, module=True)
        if epoch == 2:
            saved_prior = prior.read_bytes()
    diagnostic = client.run("status", {"repo_uuid": REPO_UUID})
    value = json.loads(pointer.read_bytes())
    pointer.write_bytes(PointerSet.from_mapping(dict(value, source_epoch=99)).canonical)
    # Recover through the installed core in this disposable fixture. Public repair
    # transport is outside S6; native runtime/candidate admission stays enabled.
    script = r'''
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from graphify.workspace.contracts import CompatibilityManifest
from graphify.workspace.composition import load_workspace_runtime_inputs, compose_workspace_runtime
from graphify.workspace.contracts import canonical_json_bytes
value = json.load(sys.stdin)
runtime = compose_workspace_runtime(load_workspace_runtime_inputs(
    state_root=Path(value["state_root"]),
    expected=CompatibilityManifest.from_mapping(value["expected"]))).require_runtime()
leases = runtime.stores.leases
grant = leases.acquire(value["repo_uuid"], "POINTER_RECOVERY", leases.current_owner(),
    **value["coordinates"], acquired_at=datetime.now(timezone.utc),
    monotonic_ns=time.monotonic_ns(), ttl_ns=10**12)
repaired = runtime.stores.pointers.recover(grant, occurred_at=datetime.now(timezone.utc),
    monotonic_ns=time.monotonic_ns())
leases.release(grant)
sys.stdout.buffer.write(canonical_json_bytes(repaired.to_dict()))
'''
    result = subprocess.run([str(client.python), "-E", "-P", "-B", "-c", script],
        input=canonical_json_bytes({"state_root": str(state), "expected": client.expected,
            "repo_uuid": REPO_UUID, "coordinates": {
                "expected_registry_revision": enrolled["registry_revision"],
                "expected_active_source_revision": diagnostic["active_source_revision"],
                "expected_operation_epoch": diagnostic["operation_epoch"],
                "expected_migration_epoch": diagnostic["migration_epoch"]}}),
        env=client.env, cwd=client.root, capture_output=True, timeout=180)
    assert result.returncode == 0 and result.stderr == b"", (result.stdout, result.stderr)
    repaired = PointerSet.from_json(result.stdout)
    repaired_prior = prior.read_bytes()
    assert json.loads(repaired_prior)["replaced_by_revision"] == repaired.to_dict()["pointer_revision"]
    assert repaired.to_dict()["last_good"]["generation_id"] == "gen-repaired-2"
    lease = json.loads((workspace / "workspace.json").read_bytes())
    params = {"repo_uuid": REPO_UUID, "authorization": authorization("ROLLBACK"),
        "expected_registry_revision": enrolled["registry_revision"],
        "expected_active_source_revision": diagnostic["active_source_revision"],
        "expected_operation_epoch": lease["operation_epoch"],
        "expected_migration_epoch": lease["migration_epoch"],
        "expected_fence_high_watermark": lease["fence_high_watermark"],
        "expected_pointer_revision": repaired.to_dict()["pointer_revision"],
        "expected_current_receipt_sha256": repaired.to_dict()["current"]["receipt_sha256"],
        "expected_source_epoch": 2, "target_generation_id": "gen-repaired-2",
        "target_receipt_sha256": repaired.to_dict()["last_good"]["receipt_sha256"]}
    assert saved_prior is not None
    for damage in ("stale", "stale_replaced", "operation_epoch", "fence_token"):
        if damage in {"stale", "stale_replaced"}:
            value = json.loads(saved_prior)
            if damage == "stale_replaced":
                value["replaced_by_revision"] = repaired.to_dict()["pointer_revision"]
            prior.write_bytes(PriorPointerRecord.from_mapping(value).canonical)
            target = json.loads(saved_prior)["pointer_set"]["current"]
            pointer.write_bytes(PointerSet.from_mapping(dict(repaired.to_dict(), last_good=target)).canonical)
            stale = dict(params, expected_source_epoch=1, target_generation_id=target["generation_id"],
                target_receipt_sha256=target["receipt_sha256"])
        else:
            value = json.loads(repaired_prior)
            value["pointer_set"][damage] += 1
            prior.write_bytes(PriorPointerRecord.from_mapping(value).canonical)
            stale = params
        before = client.snapshots(source)
        assert client.run("rollback", stale, exit_code=4)["error_code"] == "workspace_refused"
        assert client.snapshots(source) == before
        prior.write_bytes(repaired_prior)
        pointer.write_bytes(repaired.canonical)
    source_before, payloads = tree_snapshot(source), tree_snapshot(workspace / "generations")
    moved = client.run("rollback", params, module=True)["pointer"]
    assert moved["current"] == repaired.to_dict()["last_good"]
    before = client.snapshots(source)
    assert client.run("rollback", params, file=True)["pointer"] == moved
    assert client.snapshots(source) == before
    assert tree_snapshot(source) == source_before
    assert tree_snapshot(workspace / "generations") == payloads


@pytest.mark.skipif(sys.platform != "darwin", reason="native rollback requires non-elevated macOS/APFS")
def test_installed_native_public_rollback_and_authority(installed_candidate):  # noqa: F811
    root, _, _, _, bundle, _ = installed_candidate
    source = create_repo(root / "s6-source")
    (source / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    state = root / "s6-state"
    state.mkdir(mode=0o700)
    authority = state / "runtime-manifest.json"
    authority.write_bytes((bundle / "runtime-manifest.json").read_bytes())
    authority.chmod(0o600)
    client = PublicClient(installed_candidate, state)
    enrolled = client.run("register", {"operation": "enroll", "source_root": str(source),
        "authorization": authorization("ENROLL"), "expected_registry_revision": 0})
    synced = []
    for epoch, (generation, attempt) in enumerate((("gen-s6-first", "a"), ("gen-s6-second", "b")), 1):
        prepared = client.run("sync", {"operation": "prepare", "repo_uuid": REPO_UUID,
            "generation_id": generation, "source_epoch": epoch, "desired_watermark": epoch,
            "expected_payload_bytes": 1024 * 1024})
        synced.append(client.run("sync", {"operation": "execute", "sync_request": prepared["sync_request"],
            "attempt_sha256": attempt * 64}, module=True))
    diagnostic = client.run("status", {"repo_uuid": REPO_UUID}, audit=True)
    workspace = state / "workspaces" / REPO_UUID
    lease = json.loads((workspace / "workspace.json").read_bytes())
    params = {"repo_uuid": REPO_UUID, "authorization": authorization("ROLLBACK"),
        "expected_registry_revision": enrolled["registry_revision"],
        "expected_active_source_revision": diagnostic["active_source_revision"],
        "expected_operation_epoch": diagnostic["operation_epoch"],
        "expected_migration_epoch": diagnostic["migration_epoch"],
        "expected_fence_high_watermark": lease["fence_high_watermark"],
        "expected_pointer_revision": synced[1]["pointer_revision"],
        "expected_current_receipt_sha256": synced[1]["receipt_sha256"],
        "expected_source_epoch": 1, "target_generation_id": "gen-s6-first",
        "target_receipt_sha256": synced[0]["receipt_sha256"]}
    # Wrong candidate and authority fail before any lifecycle or source write.
    wrong = client.request("rollback", params)
    wrong["compatibility_manifest"] = dict(client.expected, wheel_sha256="f" * 64)
    assert client.run("rollback", {}, request=wrong, exit_code=3)["error_code"] == "authority_invalid"
    original_authority = authority.read_bytes()
    authority.write_bytes(b"invalid authority")
    damaged = client.snapshots(source)
    assert client.run("rollback", params, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == damaged
    authority.write_bytes(original_authority)
    pointer = workspace / "pointers.json"
    original_pointer = pointer.read_bytes()
    value = json.loads(original_pointer)
    assert value["source_epoch"] == 2
    pointer.write_bytes(PointerSet.from_mapping(dict(value, source_epoch=3)).canonical)
    damaged = client.snapshots(source)
    assert client.run("rollback", params, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == damaged
    pointer.write_bytes(original_pointer)
    queue_pending = workspace / "queue/semantic.pending.jsonl"
    assert not queue_pending.exists()
    queue_pending.write_bytes(b"unresolved semantic intent")
    queue_pending.chmod(0o600)
    damaged = client.snapshots(source)
    assert client.run("rollback", params, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == damaged
    queue_pending.unlink()
    lease_path = workspace / "workspace.json"
    original_lease = lease_path.read_bytes()
    for field in ("operation_epoch", "fence_high_watermark"):
        value = json.loads(original_lease)
        value[field] = 2
        lease_path.write_bytes(WorkspaceLeaseState.from_mapping(value).canonical)
        stale = dict(params)
        stale["expected_" + field] = 2
        damaged = client.snapshots(source)
        assert client.run("rollback", stale, exit_code=4)["error_code"] == "workspace_refused"
        assert client.snapshots(source) == damaged
        lease_path.write_bytes(original_lease)
    prior = workspace / "pointers.previous.json"
    original_prior = prior.read_bytes()
    prior.unlink()
    damaged = client.snapshots(source)
    assert client.run("rollback", params, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == damaged
    prior.write_bytes(original_prior)
    prior.chmod(0o600)
    # Content snapshots of the immutable generations and source cover success.
    generations = tree_snapshot(workspace / "generations")
    source_before = tree_snapshot(source)
    result = client.run("rollback", params, file=True)["pointer"]
    assert result["pointer_revision"] == synced[1]["pointer_revision"] + 1
    assert result["source_epoch"] == 1
    assert result == json.loads((workspace / "pointers.json").read_bytes())
    assert result["current"] == {"generation_id": "gen-s6-first",
        "receipt_sha256": synced[0]["receipt_sha256"]}
    assert tree_snapshot(workspace / "generations") == generations
    assert tree_snapshot(source) == source_before
    events = [decode_journal_frame(path.read_bytes()).to_dict()
        for path in sorted((workspace / "journal/segments").glob("*.gwf"))]
    assert events[-1]["transition"] == "ROLLED_BACK"
    assert events[-1]["receipt_sha256"] == synced[0]["receipt_sha256"]
    original_pointer = pointer.read_bytes()
    for last_good in (None, result["current"]):
        pointer.write_bytes(PointerSet.from_mapping(dict(result, last_good=last_good)).canonical)
        damaged = client.snapshots(source)
        assert client.run("rollback", params, exit_code=4)["error_code"] == "workspace_refused"
        assert client.snapshots(source) == damaged
        pointer.write_bytes(original_pointer)
    prior = workspace / "pointers.previous.json"
    original_prior = prior.read_bytes()
    value = json.loads(original_prior)
    value["pointer_set"]["fence_token"] += 1
    prior.write_bytes(PriorPointerRecord.from_mapping(value).canonical)
    damaged = client.snapshots(source)
    assert client.run("rollback", params, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == damaged
    prior.write_bytes(original_prior)
    before = client.snapshots(source)
    assert client.run("rollback", params, module=True)["pointer"] == result
    assert client.snapshots(source) == before
    query = {"repo_uuid": REPO_UUID, "question": "caller", "mode": "bfs", "depth": 2,
        "token_budget": 4096, "context_filters": []}
    assert "leaf" in client.run("query", query, audit=True)["text"]
    for name in ("status", "doctor"):
        assert client.run(name, {"repo_uuid": REPO_UUID}, audit=True)["state"] == "current"
    assert client.snapshots(source) == before
    # New helper membership is mandatory and its installed bytes are authoritative.
    member = "graphify/workspace/rollback.py"
    assert member in client.expected["package_members"]
    package = next(client.python.parent.parent.glob("lib/python*/site-packages"))
    helper = package / member
    original = helper.read_bytes()
    assert hashlib.sha256(original).hexdigest() == client.expected["package_members"][member]
    try:
        helper.write_bytes(original + b"\n# unauthorized content\n")
        before = client.snapshots(source)
        assert client.run("rollback", params, exit_code=3)["error_code"] == "authority_invalid"
        assert client.snapshots(source) == before
    finally:
        helper.write_bytes(original)
