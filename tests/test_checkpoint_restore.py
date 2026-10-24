"""#147 prospective restore acceptance, with explicitly modeled root custody."""
from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from graphify import checkpoint_restore as recovery
from graphify import recovery_authority as authority
from graphify import transaction as tx
from tests.test_recovery_authority import modeled  # noqa: F401 -- shared modeled custody fixture

SOURCE = "1" * 64


def tree(path):
    return {str(p.relative_to(path)): (p.lstat().st_dev, p.lstat().st_ino, p.read_bytes())
            for p in path.rglob("*") if p.is_file() and not p.is_symlink()}


def publish(root, output, *, value="saved", generation=1):
    owner = tx.begin_transaction("full", root, output=output)
    with recovery._owner(owner, "unused"):
        graph = {"directed": False, "multigraph": False, "graph": {
            tx.GRAPH_WATERMARK_KEY: {"schema": 1, "protocol_epoch": 1, "generation": generation, "state": "active"}},
            "nodes": [{"id": value, "weight": 0.75}], "links": []}
        tx.commit_publication_plan(owner, tx.PublicationPlan({"graph.json": json.dumps(graph).encode(),
                                                             "manifest.json": b"{}", "nested/report.txt": b"saved report"}))
        tx.finish_transaction(owner)


@pytest.fixture
def fixture(modeled, tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "file.py").write_text("x = 1")
    original = tmp_path / "original"
    publish(root, original)
    approved = recovery.capture(SOURCE, root, original, "absent")
    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    request = {"schema": 1, "source_id": SOURCE, "root": str(root), "output": str(parent / "restored")}
    return modeled[0], root, original, request, approved


def test_approved_restore_and_normal_next_generation(fixture):
    store, root, original, request, _ = fixture
    before = tree(original), tree(store)
    preview = recovery.preview(request)
    assert before == (tree(original), tree(store))
    result = recovery.execute(request, preview["preview_sha256"])
    output = Path(request["output"])
    snapshot = tx.open_graph_snapshot(output / "graph.json", purpose="test")
    assert snapshot.generation == 1
    assert snapshot.data["nodes"] == [{"id": "saved", "weight": 0.75}]
    assert (output / "nested/report.txt").read_bytes() == b"saved report"
    assert result["output_identity"] != {"device": original.stat().st_dev, "inode": original.stat().st_ino}
    assert before == (tree(original), tree(store))
    proof = (output / recovery.PROVENANCE).read_bytes()
    publish(root, output, value="later", generation=2)
    assert (output / recovery.PROVENANCE).read_bytes() == proof
    assert tx.open_graph_snapshot(output / "graph.json", purpose="test").generation == 2


@pytest.mark.parametrize("boundary", ["after_restore_directory", "after_restore_claim", "after_restore_seal",
                                     "after_publication_plan", "after_receipt", "after_protocol",
                                     "after_close_pending", "after_token_unlink", "after_live_remove", "after_restore_retirement"])
def test_interruption_and_exact_resume(fixture, boundary):
    store, root, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    before = tree(original), tree(store)

    def stop(name):
        if name == boundary:
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match=boundary):
        recovery.execute(request, fingerprint, failpoint=stop)
    output = Path(request["output"])
    if boundary == "after_restore_directory":
        with pytest.raises(tx.PendingTransactionError, match="absent"):
            recovery.execute(request, fingerprint)
        assert not (output / "graph.json").exists()
    else:
        claim = json.loads((output / recovery.CLAIM).read_bytes())
        if boundary != "after_restore_retirement":
            frozen = tree(output)
            for action in (lambda: tx.begin_transaction("full", root, output=output),
                           lambda: tx.recover_transaction("full", root, output=output),
                           lambda: tx.queue_rebuild("update", root, output=output),
                           lambda: tx.open_graph_snapshot(output / "graph.json", purpose="test")):
                with pytest.raises(tx.PendingTransactionError):
                    action()
                assert tree(output) == frozen
        result = recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
        assert result["status"] in {"complete", "already_complete"}
        assert tx.open_graph_snapshot(output / "graph.json", purpose="test").generation == 1
    assert before == (tree(original), tree(store))


def test_changed_head_refuses_start_and_incomplete_resume(fixture):
    store, root, original, request, approved = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    def stop(name):
        if name == "after_restore_seal":
            raise RuntimeError("cut")
    with pytest.raises(RuntimeError):
        recovery.execute(request, fingerprint, failpoint=stop)
    output = Path(request["output"])
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    recovery.revoke(SOURCE, approved["head_sha256"])
    before = tree(output), tree(store), tree(original)
    with pytest.raises(tx.PendingTransactionError, match="enabled"):
        recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
    assert before == (tree(output), tree(store), tree(original))


def test_completed_exact_retry_is_readonly_after_revocation(fixture):
    store, _, original, request, approved = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    recovery.execute(request, fingerprint)
    output = Path(request["output"])
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    recovery.revoke(SOURCE, approved["head_sha256"])
    before = tree(output), tree(store), tree(original)
    assert recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])["status"] == "already_complete"
    assert before == (tree(output), tree(store), tree(original))


def test_capture_cas_duplicate_and_unselected_slot(fixture):
    store, root, original, _, approved = fixture
    before = tree(store), tree(original)
    with pytest.raises(tx.PendingTransactionError, match="head changed"):
        recovery.capture(SOURCE, root, original, "absent")
    with pytest.raises(tx.PendingTransactionError, match="not newer"):
        recovery.capture(SOURCE, root, original, approved["head_sha256"])
    assert before == (tree(store), tree(original))


@pytest.mark.parametrize("mutation", ["occupied", "symlink", "preview", "parent_mode", "missing_checkpoint", "tampered_checkpoint"])
def test_start_refusals_preserve_inputs(fixture, mutation):
    store, _, original, request, approved = fixture
    output = Path(request["output"])
    fingerprint = recovery.preview(request)["preview_sha256"]
    if mutation == "occupied": output.mkdir()
    elif mutation == "symlink": output.symlink_to(original, target_is_directory=True)
    elif mutation == "preview": fingerprint = "0" * 64
    elif mutation == "parent_mode": output.parent.chmod(0o755)
    elif mutation == "missing_checkpoint": request = {**request, "source_id": "2" * 64}
    else:
        target = store / "checkpoints" / approved["checkpoint_sha256"] / "artifacts/graph.json"
        target.write_bytes(b"tampered")
    before = tree(original), tree(store)
    with pytest.raises((tx.PendingTransactionError, authority.RecoveryAuthorityError)):
        recovery.execute(request, fingerprint)
    assert before == (tree(original), tree(store))
    assert not output.exists() or mutation in {"occupied", "symlink"}


@pytest.mark.parametrize("record", [tx.PROTOCOL_FILE, tx.DRAINER_FILE, tx.RECEIPT_FILE])
def test_synthetic_identity_mismatch_never_enrolls(fixture, record):
    store, root, original, _, _ = fixture
    path = original / record
    raw = json.loads(path.read_bytes())
    raw["output_identity"]["device"] += 1
    path.write_text(json.dumps(raw))
    before = tree(original), tree(store)
    with pytest.raises(tx.PendingTransactionError):
        recovery.capture("3" * 64, root, original, "absent")
    assert before == (tree(original), tree(store))


def test_metadata_duplicate_and_nonfinite_refusal():
    for payload in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'):
        with pytest.raises(tx.PendingTransactionError):
            recovery._json(payload)


@pytest.mark.parametrize("boundary", ["after_restore_claim", "after_restore_seal", "after_publication_plan",
                                     "after_receipt", "after_protocol", "after_restore_retirement"])
def test_native_process_exit_then_different_process_resume(fixture, boundary):
    """Actual process death on disposable APFS; root custody remains modeled."""
    store, _, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    frozen = tree(store), tree(original)
    code = '''import json, os, sys
from pathlib import Path
from graphify import recovery_authority as a, checkpoint_restore as r
# Model only administrator custody. Publication uses native descriptors/locks.
a.CONFIG_PATH = Path(sys.argv[1])
a._require_platform = lambda: None
a._filesystem = lambda fd: None
a._qualify_runtime = lambda: None
a._sync_file = os.fsync
a._safe = lambda fd, **kwargs: os.fstat(fd)
a.os.fchown = lambda *args: None
def die(name):
    if name == sys.argv[4]: os._exit(87)
r.execute(json.loads(sys.argv[2]), sys.argv[3], failpoint=die)
'''
    child = subprocess.run([sys.executable, "-B", "-c", code, str(store.parent / "config.json"),
                            json.dumps(request), fingerprint, boundary], capture_output=True, text=True)
    assert child.returncode == 87, child.stderr
    output = Path(request["output"])
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    assert claim["owner"]["pid"] != os.getpid()
    result = recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
    assert result["status"] in {"complete", "already_complete"}
    assert tx.open_graph_snapshot(output / "graph.json", purpose="test").generation == 1
    assert frozen == (tree(store), tree(original))


def test_fsync_failure_after_seal_preserves_replayable_state(fixture, monkeypatch):
    store, _, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    before = tree(store), tree(original)
    original_sync = os.fsync
    def bad_sync(fd):
        raise OSError("injected fsync error")
    def inject(name):
        if name == "after_restore_seal":
            monkeypatch.setattr(os, "fsync", bad_sync)
    try:
        with pytest.raises(OSError, match="fsync"):
            recovery.execute(request, fingerprint, failpoint=inject)
    finally:
        monkeypatch.setattr(os, "fsync", original_sync)
    output = Path(request["output"])
    with pytest.raises(tx.PendingTransactionError):
        tx.open_graph_snapshot(output / "graph.json", purpose="test")
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
    assert before == (tree(store), tree(original))


@pytest.mark.parametrize("mutation", ["active", "queued", "artifact", "predecessor"])
def test_capture_pending_or_changed_source_never_approves(fixture, mutation):
    store, root, original, _, approved = fixture
    if mutation == "active":
        tx.begin_transaction("full", root, output=original)
    elif mutation == "queued":
        tx.queue_rebuild("update", root, output=original)
    elif mutation == "artifact":
        (original / "nested/report.txt").write_bytes(b"changed")
    else:
        raw = json.loads((original / tx.PREDECESSOR_FILE).read_bytes())
        raw["output_identity"]["device"] += 1
        (original / tx.PREDECESSOR_FILE).write_text(json.dumps(raw))
    before = tree(store), tree(original)
    with pytest.raises(tx.PendingTransactionError):
        recovery.capture(SOURCE, root, original, approved["head_sha256"])
    assert before == (tree(store), tree(original))


def test_snapshot_survives_failed_original_without_opening_it(fixture):
    store, _, original, request, _ = fixture
    saved = original.with_name("preserved-original")
    original.rename(saved)
    original.symlink_to("/a/nonexistent/or/hostile/path")
    before = tree(saved), tree(store)
    plan = recovery.preview(request)
    recovery.execute(request, plan["preview_sha256"])
    assert tx.open_graph_snapshot(Path(request["output"]) / "graph.json", purpose="test").generation == 1
    assert before == (tree(saved), tree(store))


def test_installed_entrypoint_fails_closed_without_authority(tmp_path):
    request = tmp_path / "request.json"
    root = tmp_path / "root"
    root.mkdir()
    request.write_text(json.dumps({"schema": 1, "source_id": SOURCE, "root": str(root), "output": str(tmp_path / "never-created")}))
    result = subprocess.run([sys.executable, "-B", "-m", "graphify", "checkpoint", "preview", "--request", str(request)],
                            capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    assert not (tmp_path / "never-created").exists()


@pytest.mark.parametrize("name", [recovery.CLAIM, recovery.SEAL, recovery.CLAIM.upper(),
                                 "nested/" + recovery.SEAL, ".write-report", ".authority.lock"])
def test_reserved_storage_and_coordination_names_refuse_before_approval(fixture, name):
    store, root, original, _, approved = fixture
    before = tree(store)
    if name not in {".write-report", ".authority.lock"}:
        with pytest.raises(tx.PendingTransactionError, match="coordination"):
            tx._validated_relative_name(name)
        return
    owner = tx.begin_transaction("full", root, output=original)
    graph = json.loads((original / "graph.json").read_bytes())
    graph["graph"][tx.GRAPH_WATERMARK_KEY]["generation"] = 2
    with recovery._owner(owner, "unused"):
        tx.commit_publication_plan(owner, tx.PublicationPlan({"graph.json": json.dumps(graph).encode(),
                                                             "manifest.json": b"{}", name: b"data"}))
        tx.finish_transaction(owner)
    frozen = tree(original)
    with pytest.raises(authority.RecoveryAuthorityError):
        recovery.capture(SOURCE, root, original, approved["head_sha256"])
    assert before == tree(store)
    assert frozen == tree(original)


def test_provenance_bound_is_checked_before_destination_creation(fixture, monkeypatch):
    store, root, original, request, approved = fixture
    owner = tx.begin_transaction("full", root, output=original)
    graph = json.loads((original / "graph.json").read_bytes())
    graph["graph"][tx.GRAPH_WATERMARK_KEY]["generation"] = 2
    artifacts = {f"artifact-{i:04d}-" + "x" * 190 + ".txt": b"" for i in range(500)}
    artifacts.update({"graph.json": json.dumps(graph).encode(), "manifest.json": b"{}"})
    with recovery._owner(owner, "unused"):
        tx.commit_publication_plan(owner, tx.PublicationPlan(artifacts))
        tx.finish_transaction(owner)
    recovery.capture(SOURCE, root, original, approved["head_sha256"])
    before = tree(store), tree(original)
    monkeypatch.setattr(tx, "_MAX_STATE_BYTES", 256 * 1024)
    with pytest.raises(tx.PendingTransactionError, match="metadata exceeds"):
        recovery.preview(request)
    assert not Path(request["output"]).exists()
    assert before == (tree(store), tree(original))


@pytest.mark.parametrize("name", ["nested/report.txt", tx.PROTOCOL_FILE, tx.RECEIPT_FILE, tx.DRAINER_FILE])
def test_capture_rejects_hardlinked_artifacts_and_authority_records(fixture, name):
    store, root, original, _, approved = fixture
    os.link(original / name, original.parent / "untrusted-alias")
    before = tree(store), tree(original)
    with pytest.raises(tx.PendingTransactionError, match="hard-linked"):
        recovery.capture(SOURCE, root, original, approved["head_sha256"])
    assert before == (tree(store), tree(original))


def test_two_cooperating_restore_writers_have_one_winner(fixture):
    store, _, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    before = tree(store), tree(original)
    barrier = threading.Barrier(2)
    def run():
        barrier.wait()
        try:
            return recovery.execute(request, fingerprint)["status"]
        except tx.PendingTransactionError:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ["complete", "refused"]
    assert before == (tree(store), tree(original))


def test_parent_retarget_refuses_before_writing_foreign_destination(fixture):
    store, _, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    output = Path(request["output"])
    preserved = output.parent.with_name("preserved-parent")
    def replace(name):
        if name == "after_restore_directory":
            output.parent.rename(preserved)
            output.parent.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            (output / "foreign.txt").write_bytes(b"leave intact")
    before = tree(store), tree(original)
    with pytest.raises(tx.PendingTransactionError):
        recovery.execute(request, fingerprint, failpoint=replace)
    assert list(output.iterdir()) == [output / "foreign.txt"]
    assert (output / "foreign.txt").read_bytes() == b"leave intact"
    assert before == (tree(store), tree(original))


def test_malformed_ordinary_protocol_keeps_structured_refusal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    output = tmp_path / "output"
    publish(root, output)
    (output / tx.PROTOCOL_FILE).write_bytes(b"7")
    before = tree(output)
    with pytest.raises(tx.PendingTransactionError):
        tx.queue_rebuild("update", root, output=output)
    assert before == tree(output)


def test_native_full_administrator_store_qualification_is_not_modeled():
    from tools.checkpoint_qualification import execute_spec, opt_in_spec
    spec = opt_in_spec()
    if spec is None:
        pytest.skip("A14 opt-in requires GRAPHIFY_A14_SPEC and a separately provisioned disposable macOS/APFS VM with root-owned installed Python/candidate and an existing non-admin reader account without passwordless sudo")
    execute_spec(spec)


@pytest.mark.parametrize("kind", ["ordinary", "fifo", "symlink", "hardlink", "oversized"])
def test_cli_request_reader_bounds_and_type(tmp_path, kind):
    request = tmp_path / "request.json"
    data = b'{"schema":1}'
    if kind == "fifo":
        os.mkfifo(request)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(data)
        request.symlink_to(target)
    else:
        request.write_bytes(data)
        if kind == "hardlink":
            os.link(request, tmp_path / "alias")
        elif kind == "oversized":
            with request.open("r+b") as stream:
                stream.truncate(tx._MAX_STATE_BYTES + 1)
    if kind == "ordinary":
        assert recovery._read_request(request) == {"schema": 1}
    else:
        with pytest.raises((tx.PendingTransactionError, OSError)):
            recovery._read_request(request)


def test_base_reader_refuses_restore_binding_without_mutation(fixture, tmp_path):
    _, _, _, request, _ = fixture
    preview = recovery.preview(request)
    recovery.execute(request, preview["preview_sha256"])
    old = tmp_path / "base_transaction.py"
    old.write_bytes(subprocess.check_output(['git', 'show',
        '53becfa9819dda39c660553794d03132b9a0e7c8:graphify/transaction.py']))
    output = Path(request["output"])
    before = tree(output)
    code = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location("base_transaction", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    with module.pin_output(sys.argv[2], mutation=False) as cap:
        module._read_protocol(cap)
except module.PendingTransactionError as exc:
    assert "malformed protocol state" in str(exc), str(exc)
else:
    raise AssertionError("base reader admitted new restore authority")
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(old), str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert before == tree(output)


def test_capture_source_inside_store_refuses_without_authority_write(fixture):
    store, root, _, _, _ = fixture
    inside = store / "mistaken-source"
    publish(root, inside)
    before = tree(store)
    with pytest.raises(tx.PendingTransactionError, match="overlaps"):
        recovery.capture("9" * 64, root, inside, "absent")
    assert before == tree(store)
