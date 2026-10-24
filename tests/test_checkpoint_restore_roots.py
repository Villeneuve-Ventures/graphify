"""Root identity during publication and independent completed restore reads.

Administrator custody is modeled by the shared fixture; directory replacement,
transaction admission, handoff, publication and public resume are real calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify import checkpoint_restore as recovery
from graphify import transaction as tx
from tests.test_checkpoint_restore import SOURCE, fixture, publish, tree  # noqa: F401 -- shared fixture
from tests.test_recovery_authority import modeled  # noqa: F401 -- fixture dependency


def replace_root(root):
    saved = root.with_name("preserved-corpus")
    root.rename(saved)
    root.mkdir(mode=0o700)
    (root / "foreign.py").write_bytes(b"unrelated replacement root\n")
    return saved


@pytest.mark.parametrize("boundary", ["before_begin", "during_begin_canonicalization"])
def test_root_replacement_at_real_transaction_admission_refuses(fixture, monkeypatch, boundary):
    store, root, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    before = tree(original), tree(store), tree(root)
    real_begin = tx.begin_transaction
    real_canonical = tx._canonical_directory
    admitted = False
    replaced = False
    saved = None

    def canonical(path):
        nonlocal replaced, saved
        result = real_canonical(path)
        if boundary == "during_begin_canonicalization" and admitted and result == root and not replaced:
            saved = replace_root(root)
            replaced = True
        return result

    def begin(*args, **kwargs):
        nonlocal admitted, replaced, saved
        admitted = True
        if boundary == "before_begin":
            saved = replace_root(root)
            replaced = True
        # Forward every actual admission argument, including any pinned root
        # capability; the real admission code must enforce the identity.
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(tx, "_canonical_directory", canonical)
    monkeypatch.setattr(tx, "begin_transaction", begin)
    with pytest.raises((tx.PendingTransactionError, OSError)):
        recovery.execute(request, fingerprint)
    assert admitted and replaced and saved is not None
    output = Path(request["output"])
    assert not (output / "graph.json").exists()
    assert not (output / recovery.CLAIM).exists()
    assert before == (tree(original), tree(store), tree(saved))
    assert (root / "foreign.py").read_bytes() == b"unrelated replacement root\n"


@pytest.mark.parametrize("boundary", ["after_real_handoff", "after_restore_claim"])
def test_root_replacement_after_owner_admission_keeps_partial_restore_fenced(fixture, monkeypatch, boundary):
    store, root, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    before = tree(original), tree(store), tree(root)
    saved = None
    real_handoff = tx.stage_transaction_handoff

    def handoff(*args, **kwargs):
        nonlocal saved
        result = real_handoff(*args, **kwargs)
        if boundary == "after_real_handoff":
            saved = replace_root(root)
        return result

    def at_boundary(name):
        nonlocal saved
        if boundary == "after_restore_claim" and name == boundary:
            saved = replace_root(root)

    monkeypatch.setattr(tx, "stage_transaction_handoff", handoff)
    with pytest.raises((tx.PendingTransactionError, OSError)):
        recovery.execute(request, fingerprint, failpoint=at_boundary)
    assert saved is not None
    output = Path(request["output"])
    assert output.is_dir() and (output / tx.PROTOCOL_FILE).is_file()
    assert not (output / "graph.json").exists()
    if boundary == "after_restore_claim":
        assert (output / recovery.CLAIM).is_file()
    partial = tree(output)
    for operation in (
        lambda: tx.open_graph_snapshot(output / "graph.json", purpose="root replacement test"),
        lambda: tx.begin_transaction("full", root, output=output),
        lambda: tx.queue_rebuild("update", root, output=output),
    ):
        with pytest.raises((tx.PendingTransactionError, OSError)):
            operation()
        assert tree(output) == partial
    assert before == (tree(original), tree(store), tree(saved))
    assert (root / "foreign.py").read_bytes() == b"unrelated replacement root\n"


def test_completed_public_exact_resume_and_reads_survive_missing_source_root(fixture, tmp_path, capsys):
    store, root, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    recovery.execute(request, fingerprint)
    output = Path(request["output"])
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    saved = root.with_name("preserved-completed-corpus")
    root.rename(saved)
    request_file = tmp_path / "exact-resume-request.json"
    request_file.write_text(json.dumps(request))
    before = tree(output), tree(store), tree(original), tree(saved)
    result = recovery.run_cli([
        "resume", "--request", str(request_file), "--preview-sha256", fingerprint,
        "--device", str(claim["output_identity"]["device"]),
        "--inode", str(claim["output_identity"]["inode"]), "--owner-id", claim["owner"]["id"],
    ])
    streams = capsys.readouterr()
    assert result == 0, streams.err
    assert not streams.err and json.loads(streams.out)["status"] == "already_complete"
    snapshot = tx.open_graph_snapshot(output / "graph.json", purpose="completed independent restore")
    assert snapshot.generation == 1 and snapshot.data["nodes"] == [{"id": "saved", "weight": 0.75}]
    status = tx.transaction_status(output)
    assert status["protocol_state"] == "COMPLETE" and status["generation"] == 1
    assert before == (tree(output), tree(store), tree(original), tree(saved))
    # Completion permits independent reads, but does not authorize new mutation
    # at a missing corpus path.
    for operation in (
        lambda: tx.begin_transaction("full", root, output=output),
        lambda: tx.queue_rebuild("update", root, output=output),
        lambda: tx.recover_transaction("full", root, output=output),
    ):
        with pytest.raises((tx.PendingTransactionError, OSError)):
            operation()
        assert before == (tree(output), tree(store), tree(original), tree(saved))


@pytest.mark.parametrize("replacement", [False, True], ids=["absent-root", "replacement-root"])
@pytest.mark.parametrize("boundary", ["after_restore_claim", "after_protocol", "after_close_pending"])
def test_incomplete_resume_still_requires_original_root(fixture, replacement, boundary):
    store, root, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    def stop(name):
        if name == boundary:
            raise RuntimeError("retain incomplete restore")
    with pytest.raises(RuntimeError, match="retain incomplete"):
        recovery.execute(request, fingerprint, failpoint=stop)
    output = Path(request["output"])
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    if replacement:
        saved = replace_root(root)
    else:
        saved = root.with_name("preserved-incomplete-corpus")
        root.rename(saved)
    before = tree(output), tree(store), tree(original), tree(saved)
    with pytest.raises((tx.PendingTransactionError, OSError)):
        recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
    if boundary == "after_restore_claim":
        assert not (output / "graph.json").exists()
    else:
        assert (output / "graph.json").is_file()
        assert json.loads((output / tx.PROTOCOL_FILE).read_bytes())["state"] == "COMPLETE"
    assert before == (tree(output), tree(store), tree(original), tree(saved))
    for operation in (
        lambda: tx.open_graph_snapshot(output / "graph.json", purpose="partial completed protocol"),
        lambda: tx.begin_transaction("full", root, output=output),
        lambda: tx.queue_rebuild("update", root, output=output),
        lambda: tx.recover_transaction("full", root, output=output),
    ):
        with pytest.raises((tx.PendingTransactionError, OSError)):
            operation()
        assert before == (tree(output), tree(store), tree(original), tree(saved))


def test_ordinary_closed_output_still_rejects_missing_corpus_root(fixture):
    store, root, original, _, _ = fixture
    saved = root.with_name("preserved-ordinary-corpus")
    root.rename(saved)
    before = tree(original), tree(store), tree(saved)
    for operation in (
        lambda: tx.open_graph_snapshot(original / "graph.json", purpose="ordinary root absence"),
        lambda: tx.transaction_status(original),
    ):
        with pytest.raises((tx.PendingTransactionError, OSError)):
            operation()
        assert before == (tree(original), tree(store), tree(saved))


@pytest.mark.parametrize("mutation", ["provenance", "artifact", "exact_request"])
def test_completed_retry_without_root_still_validates_exact_claim_and_artifacts(fixture, mutation):
    store, root, original, request, _ = fixture
    fingerprint = recovery.preview(request)["preview_sha256"]
    recovery.execute(request, fingerprint)
    output = Path(request["output"])
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    saved = root.with_name("preserved-tamper-control-corpus")
    root.rename(saved)
    if mutation == "provenance":
        (output / recovery.PROVENANCE).write_bytes(b"{}")
    elif mutation == "artifact":
        (output / "nested/report.txt").write_bytes(b"tampered closed artifact")
    else:
        request = {**request, "source_id": "2" * 64}
    before = tree(output), tree(store), tree(original), tree(saved)
    with pytest.raises((tx.PendingTransactionError, OSError)):
        recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
    assert before == (tree(output), tree(store), tree(original), tree(saved))


@pytest.mark.parametrize("timing", ["after_real_plan", "before_real_publish"])
def test_incomplete_resume_rejects_same_inode_noncanonical_root(modeled, tmp_path, monkeypatch, timing):
    store = modeled[0]
    ancestor = tmp_path / "root-A"
    ancestor.mkdir(mode=0o700)
    root = ancestor / "corpus"
    root.mkdir(mode=0o700)
    (root / "file.py").write_bytes(b"x = 1\n")
    original = tmp_path / "ordinary-source"
    publish(root, original)
    recovery.capture(SOURCE, root, original, "absent")
    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    output = parent / "restored"
    request = {"schema": 1, "source_id": SOURCE, "root": str(root), "output": str(output)}
    fingerprint = recovery.preview(request)["preview_sha256"]

    def interrupt(name):
        if name == "after_live_remove":
            raise RuntimeError("retain no-live incomplete close")

    with pytest.raises(RuntimeError, match="retain no-live incomplete close"):
        recovery.execute(request, fingerprint, failpoint=interrupt)
    claim = json.loads((output / recovery.CLAIM).read_bytes())
    assert json.loads((output / tx.PROTOCOL_FILE).read_bytes())["state"] == "COMPLETE"
    assert json.loads((output / tx.DRAINER_FILE).read_bytes())["state"] == "CLOSE_PENDING"
    assert not (output / tx.TRANSACTION_FILE).exists()
    assert not (output / (".graphify_transaction_token." + claim["owner"]["id"])).exists()
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    before = tree(output), tree(store), tree(original), tree(root)
    relocated = tmp_path / "root-B"
    moved = False
    real_plan = recovery._plan
    real_publish = recovery._publish

    def move_ancestor():
        nonlocal moved
        assert not moved
        ancestor.rename(relocated)
        ancestor.symlink_to(relocated, target_is_directory=True)
        moved = True
        assert (root.stat().st_dev, root.stat().st_ino) == root_identity
        assert root.resolve(strict=True) == relocated / "corpus"
        assert root.resolve(strict=True) != root

    def plan(*args, **kwargs):
        result = real_plan(*args, **kwargs)
        if timing == "after_real_plan":
            move_ancestor()
        return result

    def publish_restore(*args, **kwargs):
        if timing == "before_real_publish":
            move_ancestor()
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(recovery, "_plan", plan)
    monkeypatch.setattr(recovery, "_publish", publish_restore)
    with pytest.raises((tx.PendingTransactionError, OSError)):
        recovery.resume(request, fingerprint, claim["output_identity"], claim["owner"]["id"])
    assert moved
    assert before == (tree(output), tree(store), tree(original), tree(root))
    with pytest.raises((tx.PendingTransactionError, OSError)):
        tx.open_graph_snapshot(output / "graph.json", purpose="noncanonical partial restore")
    assert before == (tree(output), tree(store), tree(original), tree(root))
