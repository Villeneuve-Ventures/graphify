"""Trusted receiptless bootstrap authority and crash boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify import transaction as tx
from graphify import legacy_bootstrap as bootstrap


BINDING = "legacy-bootstrap-v1:structural-v1:core-v1:" + "a" * 64


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("def fresh():\n    return 1\n")
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text(json.dumps({
        "graph": {}, "nodes": [{"id": "stale"}], "links": [],
    }))
    (output / "manifest.json").write_text("{\"stale\":true}")
    (output / "GRAPH_REPORT.md").write_text("old report")
    return root, output


def test_binding_is_durable_before_live_owner(legacy):
    root, output = legacy

    def stop(capability, protocol):
        assert protocol["bootstrap_binding"] == BINDING
        assert not (output / tx.TRANSACTION_FILE).exists()
        raise RuntimeError("claim cut")

    with pytest.raises(RuntimeError, match="claim cut"):
        tx.begin_transaction("update", root, output=output,
                             bootstrap_binding=BINDING, failpoint=stop, now=0)
    raw = json.loads((output / tx.PROTOCOL_FILE).read_text())
    assert raw["bootstrap_binding"] == BINDING
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(tx.PendingTransactionError, match="bootstrap"):
        tx.recover_transaction("update", root, output=output, now=100)
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    recovered = tx.recover_transaction("update", root, output=output, now=100,
                                       bootstrap_binding=BINDING)
    assert recovered.generation == 1
    assert recovered.bootstrap_binding == BINDING


def test_bootstrap_preparation_starts_empty(legacy):
    root, output = legacy
    owner = tx.begin_transaction("update", root, output=output,
                                 bootstrap_binding=BINDING)
    token = tx.stage_transaction_handoff(owner)
    with tx.pin_output(output) as cap, tx._locked(cap):
        owner = tx._read_transaction(cap)
        assert owner.bootstrap_binding == BINDING
        assert json.loads(token.path.read_bytes())["bootstrap_binding"] == BINDING
        prepared = tx._pin_prepared_workspace(owner, cap)
        try:
            assert list(prepared.output.path.iterdir()) == []
        finally:
            prepared.close()


def request_for(root, output):
    return {"schema": 1, "root": str(root), "output": str(output),
            "include": ["**/*.py"], "exclude": [], "extra_roots": [],
            "extraction_profile": "structural-v1", "artifact_profile": "core-v1"}


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_preview_execute_and_readonly_repeat(legacy):
    root, output = legacy
    request = request_for(root, output)
    before = tree_bytes(output.parent)
    preview = bootstrap.preview(request)
    assert tree_bytes(output.parent) == before
    result = bootstrap.execute(request, preview["preview_sha256"])
    assert result["status"] == "complete"
    snapshot = tx.open_graph_snapshot(output / "graph.json", purpose="test")
    assert snapshot.generation == 1
    assert "stale" not in {node["id"] for node in snapshot.data["nodes"]}
    assert any("fresh" in node["label"] for node in snapshot.data["nodes"])
    assert not (output / "GRAPH_REPORT.md").exists()
    assert (output / bootstrap.PROVENANCE).exists()
    preserved = Path(result["preserved_workspace"])
    assert (preserved / "legacy" / "graph.json").read_bytes() == before["graphify-out/graph.json"]
    complete = tree_bytes(output.parent)
    assert bootstrap.execute(request, preview["preview_sha256"])["status"] == "already_complete"
    assert tree_bytes(output.parent) == complete


@pytest.mark.parametrize("boundary", ["after_bootstrap_claim", "after_bootstrap_seal",
                                     "after_publication_plan", "after_receipt", "after_protocol"])
def test_cut_replay_uses_original_request_and_sealed_bytes(legacy, boundary):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def failpoint(name):
        if name == boundary:
            raise RuntimeError(boundary)

    with pytest.raises(RuntimeError, match=boundary):
        bootstrap.execute(request, fingerprint, now=0, failpoint=failpoint)
    if boundary in {"after_bootstrap_seal", "after_publication_plan", "after_receipt"}:
        # A sealed retry has no engine input dependency.
        (root / "main.py").write_text("def later():\n    return 2\n")
        before = tree_bytes(output.parent)
        with pytest.raises(tx.PendingTransactionError, match="bootstrap"):
            tx.recover_transaction("update", root, output=output, now=100)
        assert tree_bytes(output.parent) == before
    result = bootstrap.execute(request, fingerprint, now=100)
    assert result["status"] in {"complete", "already_complete"}
    assert tx.open_graph_snapshot(output / "graph.json", purpose="test").generation == 1


def test_nested_output_reobservation_ignores_only_owned_preparation(tmp_path):
    root = tmp_path
    (root / "main.py").write_text("def fresh():\n    pass\n")
    output = root / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text('{"graph":{},"nodes":[],"links":[]}')
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    assert bootstrap.execute(request, fingerprint)["status"] == "complete"


@pytest.mark.parametrize("boundary", [
    "after_transition_record", "after_owner_protocol", "after_transaction",
    "after_drainer_reserved", "after_drainer_launching", "after_drainer_claimed",
    "after_token_journal", "after_token_created", "after_token_live", "after_token_protocol",
])
def test_preseal_transition_cuts_keep_binding_and_generation(legacy, boundary):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def cut(name):
        if name == boundary:
            raise RuntimeError(boundary)

    with pytest.raises(RuntimeError, match=boundary):
        bootstrap.execute(request, fingerprint, now=0, failpoint=cut)
    result = bootstrap.execute(request, fingerprint, now=100)
    assert result["generation"] == 1
    assert result["status"] == "complete"


@pytest.mark.parametrize("boundary", ["after_protocol", "after_close_pending",
    "after_inflight_remove", "after_token_unlink", "after_live_remove", "after_complete"])
def test_completed_repeat_readonly_and_explicit_resume_finishes_closure(legacy, boundary):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def cut(name):
        if name == boundary:
            raise RuntimeError(boundary)

    with pytest.raises(RuntimeError, match=boundary):
        bootstrap.execute(request, fingerprint, failpoint=cut)
    protocol = json.loads((output / tx.PROTOCOL_FILE).read_bytes())
    token = output / f".graphify_transaction_token.{protocol['transaction_id']}"
    before = tree_bytes(output.parent)
    assert bootstrap.execute(request, fingerprint)["status"] == "already_complete"
    assert tree_bytes(output.parent) == before
    result = bootstrap.execute(request, token_path=token)
    assert result["status"] == "already_complete"
    assert not (output / tx.TRANSACTION_FILE).exists()
    assert not (output / tx.PREPARED_FILE).exists()
    assert json.loads((output / tx.DRAINER_FILE).read_bytes())["state"] == "complete"


@pytest.mark.parametrize("state", ["missing", "null-watermark", "pending-watermark",
    "active-watermark", "malformed", "protocol", "receipt", "queue", "portable"])
def test_exact_legacy_predicate_refuses_other_states_without_mutation(legacy, state):
    root, output = legacy
    graph_path = output / "graph.json"
    if state == "missing":
        graph_path.unlink()
    elif state.endswith("watermark"):
        graph = json.loads(graph_path.read_bytes())
        graph["graph"][tx.GRAPH_WATERMARK_KEY] = (
            None if state == "null-watermark" else
            {"schema": 1, "protocol_epoch": 1, "generation": 1,
             "state": "merge-pending" if state == "pending-watermark" else "active"})
        graph_path.write_text(json.dumps(graph))
    elif state == "malformed":
        graph_path.write_text('{"graph": {}, "nodes": "bad", "links": []}')
    else:
        name = {"protocol": tx.PROTOCOL_FILE, "receipt": tx.RECEIPT_FILE,
                "queue": tx.QUEUE_FILE, "portable": tx.PORTABLE_ENVELOPE_FILE}[state]
        (output / name).write_text("{}")
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError):
        bootstrap.preview(request_for(root, output))
    assert tree_bytes(output.parent) == before


@pytest.mark.parametrize("changed", ["source", "legacy", "request"])
def test_stale_preview_never_claims_output(legacy, changed):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    if changed == "source":
        (root / "main.py").write_text("def changed(): pass\n")
    elif changed == "legacy":
        (output / "GRAPH_REPORT.md").write_text("changed report")
    else:
        request["include"] = ["main.py"]
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError, match="stale"):
        bootstrap.execute(request, fingerprint)
    assert tree_bytes(output.parent) == before


@pytest.mark.parametrize("damage", ["missing-token", "tampered-token", "changed-payload"])
def test_sealed_damage_refuses_and_preserves_all_remaining_state(legacy, damage):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def cut(name):
        if name == "after_bootstrap_seal":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError):
        bootstrap.execute(request, fingerprint, failpoint=cut)
    token = next(output.glob(".graphify_transaction_token.*"))
    if damage == "missing-token":
        token.unlink()
    elif damage == "tampered-token":
        token.write_bytes(b"{}")
    else:
        marker = json.loads((output / tx.PREPARED_FILE).read_bytes())
        prepared = output.parent / marker["workspace_name"] / "graphify-out"
        (prepared / "graph.json").write_bytes(b"{}")
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError):
        bootstrap.execute(request, fingerprint)
    assert tree_bytes(output.parent) == before


def test_partial_publication_blocks_readers_and_replays(legacy, monkeypatch):
    root, output = legacy
    (output / "user-notes.txt").write_text("keep")
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    original = tx.commit_bytes

    def cut(owner, name, payload, **kwargs):
        original(owner, name, payload, **kwargs)
        raise RuntimeError("one public artifact")

    with monkeypatch.context() as patch:
        patch.setattr(tx, "commit_bytes", cut)
        with pytest.raises(RuntimeError, match="one public artifact"):
            bootstrap.execute(request, fingerprint)
    with pytest.raises(tx.PendingTransactionError):
        tx.open_graph_snapshot(output / "graph.json", purpose="test")
    assert bootstrap.execute(request, fingerprint)["status"] == "complete"
    assert (output / "user-notes.txt").read_text() == "keep"


def test_sealed_replay_does_not_require_live_source_directory(legacy):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def cut(name):
        if name == "after_bootstrap_seal":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError):
        bootstrap.execute(request, fingerprint, failpoint=cut)
    root.rename(root.with_name("source-moved"))
    assert bootstrap.execute(request, fingerprint)["status"] == "complete"
    assert bootstrap.execute(request, fingerprint)["status"] == "already_complete"


def test_completed_bootstrap_does_not_block_next_generation_takeover(legacy):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    bootstrap.execute(request, fingerprint)
    queued = tx.queue_rebuild("update", root, output=output, now=0)
    successor = tx.takeover_drainer(output, now=100)
    assert successor.generation == 2
    assert successor.claim_epoch == queued.drainer.claim_epoch + 1


def test_cold_cli_preview_and_complete_repeat_preserve_state(legacy):
    root, output = legacy
    request_path = root.parent / "request.json"
    request_path.write_text(json.dumps(request_for(root, output)))
    home = root.parent / "home"
    home.mkdir()
    cache = root.parent / "cold-bytecode"
    env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPYCACHEPREFIX": str(cache)}

    def command(*args):
        result = subprocess.run([sys.executable, "-B", "-m", "graphify", "bootstrap-legacy",
            "--request", str(request_path), *args], cwd=home, env=env,
            capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        return json.loads(result.stdout)

    before = tree_bytes(root.parent)
    preview = command("--preview")
    assert tree_bytes(root.parent) == before
    assert not cache.exists()
    assert command("--execute", preview["preview_sha256"])["status"] == "complete"
    completed = tree_bytes(root.parent)
    assert command("--execute", preview["preview_sha256"])["status"] == "already_complete"
    assert tree_bytes(root.parent) == completed


def test_command_restores_caller_context_even_after_claim_cut(legacy):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    before = tx._AUTHORITY.get()

    def cut(name):
        if name == "after_bootstrap_claim":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError):
        bootstrap.execute(request, fingerprint, now=0, failpoint=cut)
    assert tx._AUTHORITY.get() == before
    bootstrap.execute(request, fingerprint, now=100)
    assert tx._AUTHORITY.get() == before


@pytest.mark.parametrize("damage_retirement", [False, True])
def test_resume_after_retirement_rename_requires_exact_retained_evidence(legacy, monkeypatch,
                                                                       damage_retirement):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    move = tx._move_identity_no_replace

    def cut(*args, **kwargs):
        move(*args, **kwargs)
        if kwargs.get("label") == "prepared workspace retirement":
            raise RuntimeError("retirement rename")

    with monkeypatch.context() as patch:
        patch.setattr(tx, "_move_identity_no_replace", cut)
        with pytest.raises(RuntimeError, match="retirement rename"):
            bootstrap.execute(request, fingerprint)
    protocol = json.loads((output / tx.PROTOCOL_FILE).read_bytes())
    token = output / f".graphify_transaction_token.{protocol['transaction_id']}"
    retired = next(output.parent.glob(".graphify-retired-*"))
    old_graph = (retired / "legacy/graph.json").read_bytes()
    if damage_retirement:
        (retired / ".graphify_retired.json").write_text("{}")
        before = tree_bytes(output.parent)
        with pytest.raises(tx.PendingTransactionError):
            bootstrap.execute(request, token_path=token)
        assert tree_bytes(output.parent) == before
    else:
        assert bootstrap.execute(request, token_path=token)["closure_pending"] is False
        assert not (output / tx.PREPARED_FILE).exists()
        assert (retired / "legacy/graph.json").read_bytes() == old_graph


def test_custom_nested_output_is_never_source_input(tmp_path):
    (tmp_path / "source.py").write_text("def source_only(): pass\n")
    output = tmp_path / "legacy-output"
    output.mkdir()
    (output / "graph.json").write_text('{"graph":{},"nodes":[],"links":[]}')
    (output / "manifest.json").write_text('{"legacy_only_marker": {"ast_hash":"old"}}')
    request = request_for(tmp_path, output)
    request["include"] = ["**"]
    preview = bootstrap.preview(request)
    assert preview["selected"] == ["source.py"]
    result = bootstrap.execute(request, preview["preview_sha256"])
    assert result["status"] == "complete"
    graph = json.loads((output / "graph.json").read_bytes())
    assert all("legacy_only_marker" not in node.get("label", "") for node in graph["nodes"])


@pytest.mark.parametrize("renamed", [False, True])
@pytest.mark.parametrize("damaged", [False, True])
def test_reserved_successor_preserves_queue_during_bootstrap_retirement(
        legacy, monkeypatch, renamed, damaged):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    move = tx._move_identity_no_replace

    def rename_cut(*args, **kwargs):
        move(*args, **kwargs)
        if kwargs.get("label") == "prepared workspace retirement":
            raise RuntimeError("retirement cut")

    def complete_cut(name):
        if not renamed and name == "after_complete":
            raise RuntimeError("retirement cut")

    with monkeypatch.context() as patch:
        if renamed:
            patch.setattr(tx, "_move_identity_no_replace", rename_cut)
        with pytest.raises(RuntimeError, match="retirement cut"):
            bootstrap.execute(request, fingerprint, failpoint=complete_cut)
    protocol = json.loads((output / tx.PROTOCOL_FILE).read_bytes())
    token = output / f".graphify_transaction_token.{protocol['transaction_id']}"
    queued = tx.queue_rebuild("update", root, output=output, now=0)
    if damaged:
        with tx.pin_output(output) as cap:
            preserved = tx._read_predecessor_authority(cap)
            assert preserved is not None
            record = preserved[3]
        record["state"] = "cancelling"
        (output / tx.PREDECESSOR_FILE).write_text(json.dumps(record))
        before = tree_bytes(output.parent)
        with pytest.raises(tx.PendingTransactionError):
            bootstrap.execute(request, token_path=token)
        assert tree_bytes(output.parent) == before
        return
    before = tree_bytes(output)
    result = bootstrap.execute(request, token_path=token)
    assert result["closure_pending"] is False
    assert tree_bytes(output) == {name: data for name, data in before.items()
                                 if name != tx.PREPARED_FILE}
    successor = tx.takeover_drainer(output, now=100)
    assert successor.generation == 2
    assert successor.claim_epoch == queued.drainer.claim_epoch + 1


@pytest.mark.parametrize("damage", ["none", "legacy", "missing"])
def test_ordinary_claim_preserves_pending_bootstrap_retirement(legacy, damage):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def cut(name):
        if name == "after_complete":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError):
        bootstrap.execute(request, fingerprint, failpoint=cut)
    workspace = next(output.parent.glob(".graphify-prepare-*"))
    if damage == "legacy":
        (workspace / "legacy/graph.json").write_text("corrupt retained evidence")
    elif damage == "missing":
        workspace.rename(output.parent / "unavailable-workspace")
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError, match="bootstrap"):
        tx.begin_transaction("update", root, output=output)
    assert tree_bytes(output.parent) == before


def test_invalid_tokenless_resume_preserves_original_claim(legacy):
    root, output = legacy
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]

    def cut(name):
        if name == "after_bootstrap_claim":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError):
        bootstrap.execute(request, fingerprint, now=0, failpoint=cut)
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError):
        bootstrap.execute(request, token_path=output / "nonexistent", now=100)
    assert tree_bytes(output.parent) == before
    assert bootstrap.execute(request, fingerprint, now=100)["status"] == "complete"


def test_completed_bootstrap_accepts_admitted_merge_successor(legacy):
    root, output = legacy
    request = request_for(root, output)
    bootstrap.execute(request, bootstrap.preview(request)["preview_sha256"])
    ancestor = output.parent / "ancestor.json"
    ancestor.write_bytes((output / "graph.json").read_bytes())
    other = output.parent / "other.json"
    other_graph = json.loads(ancestor.read_bytes())
    other_graph["nodes"].append({"id": "other", "label": "other"})
    other.write_text(json.dumps(other_graph))
    tx.merge_detached_snapshots(ancestor, output / "graph.json", other)
    _, _, admission = tx._open_merge_pending_rebuild_snapshot(output / "graph.json")
    queued = tx.queue_rebuild("update", root, output=output, merge_admission=admission)
    successor = tx.begin_transaction("update", root, output=output)
    assert successor.generation == 2
    assert successor.drainer == queued.drainer


def test_preview_replacements_match_complete_receipt(legacy):
    root, output = legacy
    request = request_for(root, output)
    before = tree_bytes(output.parent)
    plan = bootstrap.preview(request)
    assert tree_bytes(output.parent) == before
    assert bootstrap.PROVENANCE in plan["replace"]
    bootstrap.execute(request, plan["preview_sha256"])
    receipt = json.loads((output / tx.RECEIPT_FILE).read_bytes())
    assert plan["replace"] == receipt["required_artifacts"]


@pytest.mark.parametrize("action", ["preview", "execute"])
@pytest.mark.parametrize("name", ["x" * 190, "é" * 60, "😀" * 40],
                         ids=["ascii", "escaped", "astral"])
def test_legacy_path_state_budget_refuses_before_claim(legacy, monkeypatch, action, name):
    root, output = legacy
    wiki = output / "wiki"
    wiki.mkdir()
    for index in range(30):
        (wiki / f"{index:02d}-{name}.md").write_text("legacy")
    request = request_for(root, output)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    # Scale the byte budget, not the artifact/path limits or admission behavior.
    monkeypatch.setattr(tx, "_MAX_STATE_BYTES", 16 * 1024)
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError, match="state bound"):
        if action == "preview":
            bootstrap.preview(request)
        else:
            bootstrap.execute(request, fingerprint)
    assert tree_bytes(output.parent) == before


def test_admitted_path_budget_seals_and_resumes(legacy, monkeypatch):
    root, output = legacy
    wiki = output / "wiki"
    wiki.mkdir()
    for index in range(8):
        (wiki / f"{index:02d}-{'é' * 60}.md").write_text("legacy")
    monkeypatch.setattr(tx, "_MAX_STATE_BYTES", 16 * 1024)
    request = request_for(root, output)
    plan = bootstrap.preview(request)

    def cut(boundary):
        if boundary == "after_bootstrap_seal":
            raise RuntimeError("sealed")

    with pytest.raises(RuntimeError, match="sealed"):
        bootstrap.execute(request, plan["preview_sha256"], failpoint=cut)
    assert (output / tx.PREPARED_FILE).stat().st_size <= tx._MAX_STATE_BYTES
    with tx.pin_output(output, mutation=False) as cap:
        assert "bootstrap_seal" in tx._load_json(cap, tx.PREPARED_FILE)
    token = next(output.glob(".graphify_transaction_token.*"))
    assert bootstrap.execute(request, token_path=token)["status"] == "complete"


@pytest.mark.parametrize("action", ["preview", "execute"])
def test_provenance_size_refuses_before_claim(legacy, monkeypatch, action):
    from dataclasses import replace

    root, output = legacy
    request = request_for(root, output)
    built = bootstrap.source.build(request)
    built = replace(built, uncovered={"ignored": ["x" * (20 * 1024)]})
    monkeypatch.setattr(bootstrap.source, "build", lambda *args, **kwargs: built)
    fingerprint = bootstrap.preview(request)["preview_sha256"]
    monkeypatch.setattr(tx, "_MAX_STATE_BYTES", 16 * 1024)
    before = tree_bytes(output.parent)
    with pytest.raises(tx.PendingTransactionError, match="state bound"):
        if action == "preview":
            bootstrap.preview(request)
        else:
            bootstrap.execute(request, fingerprint)
    assert tree_bytes(output.parent) == before


@pytest.mark.parametrize("kind", ["array", "object"])
def test_cli_over_nested_bounded_json_is_structured_refusal(tmp_path, kind):
    depth = (tx._MAX_STATE_BYTES - 1) // (2 if kind == "array" else 6)
    payload = ("[" * depth + "0" + "]" * depth if kind == "array" else
               '{"a":' * depth + "0" + "}" * depth)
    request = tmp_path / "request.json"
    request.write_text(payload)
    assert request.stat().st_size <= tx._MAX_STATE_BYTES
    before = tree_bytes(tmp_path)
    result = subprocess.run(
        [sys.executable, "-B", "-m", "graphify", "bootstrap-legacy",
         "--request", str(request), "--preview"], capture_output=True, text=True,
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["status"] == "refused"
    assert "Traceback" not in result.stderr
    assert tree_bytes(tmp_path) == before
