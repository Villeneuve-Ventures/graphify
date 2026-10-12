"""Cold installed repair admission, preview audit, and native disposable repair."""
import copy
import hashlib
import json
import sys

import pytest

from graphify.workspace.lifecycle_contracts import PointerSet
from tests.test_workspace_s5_installed import installed_candidate, PublicClient, authorization  # noqa: F401
from tests.test_workspace_s6_repair import identities, install_semantic_work
from tests.workspace_s3_helpers import create_repo, REPO_UUID, tree_snapshot


@pytest.mark.skipif(sys.platform != "darwin", reason="native repair requires non-elevated macOS/APFS")
def test_installed_native_preview_approved_repair_and_identity(installed_candidate):  # noqa: F811
    """Prove native installed repair admission, no-write preview, and exact execution."""
    root, _, _, _, bundle, _ = installed_candidate
    source = create_repo(root / "repair-source")
    (source / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    state = root / "repair-state"
    state.mkdir(mode=0o700)
    authority = state / "runtime-manifest.json"
    authority.write_bytes((bundle / "runtime-manifest.json").read_bytes())
    authority.chmod(0o600)
    client = PublicClient(installed_candidate, state)
    client.run("register", {"operation": "enroll", "source_root": str(source),
        "authorization": authorization("ENROLL"), "expected_registry_revision": 0})
    prepared = client.run("sync", {"operation": "prepare", "repo_uuid": REPO_UUID,
        "generation_id": "gen-public-repair", "source_epoch": 1, "desired_watermark": 1,
        "expected_payload_bytes": 1024 * 1024})
    synced = client.run("sync", {"operation": "execute", "sync_request": prepared["sync_request"],
        "attempt_sha256": "a" * 64})
    workspace = state / "workspaces" / REPO_UUID
    pointer = workspace / "pointers.json"
    original = PointerSet.from_json(pointer.read_bytes()).to_dict()
    pointer.write_bytes(PointerSet.from_mapping(dict(original, source_epoch=99)).canonical)
    preview = {"operation": "preview", "repo_uuid": REPO_UUID}
    before = client.snapshots(source), identities(state), identities(source)
    approved = client.run("repair", preview, audit=True)["approval"]
    assert client.run("repair", preview, module=True)["approval"] == approved
    assert (client.snapshots(source), identities(state), identities(source)) == before
    assert len(approved["plan"]["decision_sha256"]) == 64
    assert str(source) not in json.dumps(approved) and str(state) not in json.dumps(approved)
    execute = {"operation": "execute", "repo_uuid": REPO_UUID,
        "authorization": authorization("REPAIR_EXECUTE"), "approval": approved}
    queue = workspace / "queue/semantic.jsonl"
    original_queue = queue.read_bytes()
    for queue_state in ("queued", "required_empty"):
        install_semantic_work(queue, queue_state)
        blocked = client.snapshots(source), identities(state), identities(source)
        assert client.run("repair", preview, audit=True, exit_code=4)["error_code"] == "workspace_refused"
        assert (client.snapshots(source), identities(state), identities(source)) == blocked
        assert client.run("repair", execute, exit_code=4)["error_code"] == "workspace_refused"
        assert (client.snapshots(source), identities(state), identities(source)) == blocked
        queue.write_bytes(original_queue)
    before = client.snapshots(source), identities(state), identities(source)
    for key in ("state_root_sha256", "compatibility_sha256", "active_source_sha256"):
        changed = copy.deepcopy(execute); changed["approval"][key] = "f" * 64
        assert client.run("repair", changed, exit_code=4)["error_code"] == "workspace_refused"
        assert client.snapshots(source) == before[0]
    wrong = client.request("repair", preview)
    wrong["compatibility_manifest"] = dict(client.expected, wheel_sha256="f" * 64)
    assert client.run("repair", {}, request=wrong, exit_code=3)["error_code"] == "authority_invalid"
    assert client.snapshots(source) == before[0]
    # No preview/retry may recover an unresolved lease intent or missing lock.
    for path, damage in ((workspace / "workspace.pending.json", b"unknown lease outcome"),
                         (workspace / "gc/intent.json", b"unknown GC outcome"),
                         (workspace / "queue/semantic.pending.jsonl", b"unknown semantic outcome"),
                         (workspace / "journal/head.pending.json", b"unknown journal outcome")):
        if not path.parent.exists(): path.parent.mkdir(mode=0o700)
        path.write_bytes(damage); path.chmod(0o600)
        damaged = client.snapshots(source)
        assert client.run("repair", preview, audit=True, exit_code=4)["error_code"] == "workspace_refused"
        assert client.run("repair", execute, exit_code=4)["error_code"] == "workspace_refused"
        assert client.snapshots(source) == damaged
        path.unlink()
    lock = workspace / "locks/generations/gen-public-repair.lock"
    assert lock.is_file()
    saved_lock = lock.with_name(lock.name + ".held")
    lock.rename(saved_lock)
    damaged = client.snapshots(source)
    assert client.run("repair", preview, audit=True, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == damaged
    saved_lock.rename(lock)
    # Every newly consumed member must be required and its installed bytes verified.
    package = next(client.python.parent.parent.glob("lib/python*/site-packages"))
    for member in ("graphify/workspace/repair.py", "graphify/workspace/schemas/repair-approval.schema.json"):
        assert member in client.expected["package_members"]
        helper = package / member; raw = helper.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == client.expected["package_members"][member]
        helper.write_bytes(raw + b"\n ")
        damaged = client.snapshots(source)
        assert client.run("repair", preview, audit=True, exit_code=3)["error_code"] == "authority_invalid"
        assert client.snapshots(source) == damaged
        helper.write_bytes(raw)
    generations, source_before = tree_snapshot(workspace / "generations"), tree_snapshot(source)
    locks_before = {p: info for p, info in identities(state).items() if p.endswith(".lock")}
    moved = client.run("repair", execute, file=True)["pointer"]
    assert moved["pointer_revision"] == synced["pointer_revision"] + 1
    assert moved["current"] == original["current"] and moved["source_epoch"] == 1
    assert tree_snapshot(workspace / "generations") == generations and tree_snapshot(source) == source_before
    assert {p: info for p, info in identities(state).items() if p.endswith(".lock")} == locks_before
    after = client.snapshots(source)
    assert client.run("repair", execute, exit_code=4)["error_code"] == "workspace_refused"
    noop = client.run("repair", preview, audit=True)["approval"]
    assert noop["plan"]["classification"] == "no_op"
    assert client.snapshots(source) == after
    lease_before = json.loads((workspace / "workspace.json").read_bytes())
    execute_noop = {"operation": "execute", "repo_uuid": REPO_UUID,
        "authorization": authorization("REPAIR_EXECUTE"), "approval": noop}
    assert client.run("repair", execute_noop, file=True)["pointer"] == moved
    lease_after = json.loads((workspace / "workspace.json").read_bytes())
    assert lease_after["operation_epoch"] == lease_before["operation_epoch"] + 1
    assert lease_after["fence_high_watermark"] == lease_before["fence_high_watermark"] + 1
    assert tree_snapshot(workspace / "generations") == generations
    assert tree_snapshot(source) == source_before
    after = client.snapshots(source)
    assert client.run("repair", execute_noop, exit_code=4)["error_code"] == "workspace_refused"
    assert client.snapshots(source) == after

    # Journal-only and completed-pending repairs retain the existing pointer's
    # movement coordinates even though their new REPAIR lease advances authority.
    for pending in (workspace / "journal/head.pending.json", workspace / "pointers.pending.json"):
        current = pending.with_name(pending.name.replace(".pending", ""))
        pending.write_bytes(current.read_bytes()); pending.chmod(0o600)
        damaged = client.snapshots(source)
        approval = client.run("repair", preview, audit=True)["approval"]
        assert client.snapshots(source) == damaged
        assert approval["plan"]["pointer_action"] == ("none" if "head" in pending.name else "finalize_pending")
        assert approval["coordinates"]["result_operation_epoch"] == moved["operation_epoch"]
        assert approval["coordinates"]["result_fence_token"] == moved["fence_token"]
        execute = {"operation": "execute", "repo_uuid": REPO_UUID,
            "authorization": authorization("REPAIR_EXECUTE"), "approval": approval}
        assert client.run("repair", execute, module=True)["pointer"] == moved
        assert not pending.exists()
        assert client.run("repair", preview, audit=True)["approval"]["plan"]["classification"] == "no_op"
        assert tree_snapshot(source) == source_before
