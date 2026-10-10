"""Cold installed rollback through the unchanged bounded worker on native APFS."""
import hashlib
import json
import sys

import pytest

from graphify.workspace.lifecycle_contracts import decode_journal_frame
from tests.test_workspace_s5_installed import installed_candidate, PublicClient, authorization  # noqa: F401
from tests.workspace_s3_helpers import REPO_UUID, create_repo, tree_snapshot


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
    for generation, attempt in (("gen-s6-first", "a"), ("gen-s6-second", "b")):
        prepared = client.run("sync", {"operation": "prepare", "repo_uuid": REPO_UUID,
            "generation_id": generation, "source_epoch": 1, "desired_watermark": 1,
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
    # Content snapshots of the immutable generations and source cover success.
    generations = tree_snapshot(workspace / "generations")
    source_before = tree_snapshot(source)
    result = client.run("rollback", params, file=True)["pointer"]
    assert result["pointer_revision"] == synced[1]["pointer_revision"] + 1
    assert result == json.loads((workspace / "pointers.json").read_bytes())
    assert result["current"] == {"generation_id": "gen-s6-first",
        "receipt_sha256": synced[0]["receipt_sha256"]}
    assert tree_snapshot(workspace / "generations") == generations
    assert tree_snapshot(source) == source_before
    events = [decode_journal_frame(path.read_bytes()).to_dict()
        for path in sorted((workspace / "journal/segments").glob("*.gwf"))]
    assert events[-1]["transition"] == "ROLLED_BACK"
    assert events[-1]["receipt_sha256"] == synced[0]["receipt_sha256"]
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
