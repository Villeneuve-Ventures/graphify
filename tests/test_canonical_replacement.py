"""Candidate replacement tests: maintenance is explicitly modeled, never qualified."""
import hashlib
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("def alpha(): return 1\n")
    (root / "guide.md").write_text("Alpha is the entry point.\n")
    output = root / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text('{"old":true}')
    external = tmp_path / "external"
    external.mkdir()
    request = {"schema": 1, "root": str(root), "output": str(output),
               "archive": str(external / "archive"), "stage": str(external / "stage.json"),
               "journal": str(external / "journal.json"), "include": ["**/*"], "exclude": [],
               "extra_roots": [], "directed": True,
               "extraction_profile": replacement.PROFILE, "artifact_profile": "core-v1"}
    doc = str(root / "guide.md")
    fragment = {"backend": "codex_host_agent", "network_egress": False,
                "source_sha256": {doc: hashlib.sha256((root / "guide.md").read_bytes()).hexdigest()},
                "nodes": [{"id": "guide_alpha", "label": "Alpha", "file_type": "concept", "source_file": doc}],
                "edges": [], "hyperedges": []}
    return replacement.bind_request(request), [fragment]


def test_default_backend_never_grants_live_authority(case):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    old = (Path(request["output"]) / "graph.json").read_bytes()
    with pytest.raises(tx.PendingTransactionError, match="not qualified"):
        replacement.execute(request, seal, plan["preview_sha256"])
    assert not Path(request["journal"]).exists()
    assert (Path(request["output"]) / "graph.json").read_bytes() == old


def test_new_selected_input_invalidates_seal(case):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    (Path(request["root"]) / "new.py").write_text("def added(): return 2\n")
    with pytest.raises(tx.PendingTransactionError, match="changed"):
        replacement.preview(request, seal)


def test_arbitrary_graph_payload_cannot_be_resigned(case):
    import base64
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    seal["payloads"]["graph.json"] = base64.b64encode(b'{"nodes":[],"links":[]}').decode()
    with pytest.raises(tx.PendingTransactionError, match="changed"):
        replacement.preview(request, seal)


def test_missing_semantic_coverage_refuses(case):
    with pytest.raises(tx.PendingTransactionError, match="semantic inputs"):
        replacement.build_data(case[0], [])


@pytest.fixture
def modeled(monkeypatch):
    import contextlib
    @contextlib.contextmanager
    def fixture_maintenance(request):
        yield
    monkeypatch.setattr(replacement, "_maintenance", fixture_maintenance)


def test_modeled_full_replacement(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    before = replacement._tree(request["output"])
    result = replacement.execute(request, seal, plan["preview_sha256"])
    assert result["status"] == "complete"
    assert replacement._tree(request["archive"]) == before
    snapshot = tx.open_graph_snapshot(Path(request["output"]) / "graph.json", purpose="test")
    assert snapshot.generation == 1
    assert any(n["id"] == "guide_alpha" for n in snapshot.data["nodes"])
    assert replacement.execute(request, seal, plan["preview_sha256"]) == result
    with pytest.raises(tx.PendingTransactionError, match="archive"):
        tx.pin_output(request["archive"])


@pytest.mark.parametrize("boundary", ["ALLOCATED", "ARCHIVE_INTENT", "OLD_ARCHIVED", "INSTALL_INTENT", "NEW_INSTALLED", "PUBLISHING", "after_receipt", "after_protocol", "COMMITTED"])
def test_interrupted_exact_resume(case, modeled, boundary):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    def stop(point):
        if point == boundary:
            raise RuntimeError("injected interruption")
    with pytest.raises(RuntimeError, match="injected"):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=stop)
    assert replacement.execute(request, seal, plan["preview_sha256"])["status"] == "complete"


def test_rollback_retains_new_graph_and_is_terminal(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    journal = replacement._load(request["journal"])
    new = replacement._tree(request["output"])
    quarantine = Path(request["journal"]).parent / "retained-new"
    result = replacement.rollback(request, replacement._sha(replacement._bytes(journal)), quarantine,
                                  expected_new_tree=replacement._sha(replacement._bytes(new)))
    assert result["status"] == "rolled-back-unrecovered"
    assert replacement._tree(quarantine) == new
    assert (Path(request["output"]) / "graph.json").read_text() == '{"old":true}'
    with pytest.raises(tx.PendingTransactionError, match="terminal"):
        replacement.execute(request, seal, plan["preview_sha256"])


@pytest.mark.parametrize("boundary", ["OLD_ARCHIVED", "after_receipt"])
def test_resume_in_fresh_python_process(case, modeled, boundary, tmp_path):
    import json
    import os
    import subprocess
    import sys
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    def stop(point):
        if point == boundary:
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=stop)
    packet = tmp_path / "resume.json"
    packet.write_text(json.dumps([request, seal, plan["preview_sha256"]]))
    code = '''import contextlib,json,sys
from graphify import canonical_replacement as r
@contextlib.contextmanager
def modeled(request): yield
r._maintenance=modeled
request,seal,preview=json.load(open(sys.argv[1]))
assert r.execute(request,seal,preview)['status']=='complete'
'''
    env = {k: v for k, v in os.environ.items() if not any(s in k for s in ("API_KEY", "GRAPHIFY_", "PYTHONPATH"))}
    result = subprocess.run([sys.executable, "-B", "-c", code, str(packet)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_allocation_orphan_does_not_move_old_graph(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    def stop(point):
        if point == "after_container_mkdir":
            raise RuntimeError("orphan")
    with pytest.raises(RuntimeError):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=stop)
    orphans = list(Path(request["journal"]).parent.glob(".replacement-container-*"))
    assert len(orphans) == 1 and not list(orphans[0].iterdir())
    assert (Path(request["output"]) / "graph.json").exists()
    assert not Path(request["archive"]).exists()
    assert replacement.execute(request, seal, plan["preview_sha256"])["status"] == "complete"
    assert orphans[0].is_dir()


def test_occupied_archive_is_never_overwritten(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    Path(request["archive"]).mkdir()
    with pytest.raises(tx.PendingTransactionError, match="occupied"):
        replacement.execute(request, seal, plan["preview_sha256"])
    assert (Path(request["output"]) / "graph.json").read_text() == '{"old":true}'


def test_malformed_guard_is_fail_closed(tmp_path):
    (tmp_path / replacement.GUARD).write_text('{"schema":1,"entries":{"graphify-out":{}}}')
    with pytest.raises(tx.PendingTransactionError, match="malformed"):
        replacement.guard_output(tmp_path / "graphify-out")


def test_archive_alias_is_fenced(case, modeled, tmp_path):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    alias = tmp_path / "alias"
    alias.symlink_to(request["archive"], target_is_directory=True)
    with pytest.raises(tx.PendingTransactionError, match="archive"):
        replacement.guard_output(alias / "nested/new.json")


def test_external_evidence_cannot_overlap_policy_root(case):
    request, fragments = case
    request["extra_roots"] = [{"label": "policy", "path": str(Path(request["archive"]).parent)}]
    with pytest.raises(tx.PendingTransactionError, match="overlaps"):
        replacement.build_data(request, fragments)


def test_completed_result_rechecks_graph_bytes(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    (Path(request["output"]) / "graph.json").write_text('{"tampered":true}')
    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, seal, plan["preview_sha256"])


@pytest.mark.parametrize("boundary", ["after_rollback_quarantine", "after_rollback_restore"])
def test_interrupted_rollback_resumes_exactly(case, modeled, boundary):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    journal = replacement._load(request["journal"])
    expected = replacement._sha(replacement._bytes(journal))
    quarantine = Path(request["journal"]).parent / "retained-new"
    def stop(point):
        if point == boundary:
            raise RuntimeError("rollback interruption")
    with pytest.raises(RuntimeError):
        replacement.rollback(request, expected, quarantine, failpoint=stop,
                             expected_new_tree=replacement._sha(replacement._bytes(replacement._tree(request["output"]))))
    assert replacement.rollback(request, expected, quarantine)["status"] == "rolled-back-unrecovered"


def test_source_change_after_wait_refuses_before_archival(case, modeled):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    def change(point):
        if point == "ARCHIVE_INTENT":
            (Path(request["root"]) / "a.py").write_text("def changed(): return 3\n")
    with pytest.raises(tx.PendingTransactionError, match="changed"):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=change)
    assert not Path(request["archive"]).exists()


def test_unrecorded_native_owner_is_blocked_not_adopted(case, modeled, monkeypatch):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    original = tx.begin_transaction
    def lose_owner(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("owner created before journal binding")
    monkeypatch.setattr(tx, "begin_transaction", lose_owner)
    with pytest.raises(RuntimeError, match="owner created"):
        replacement.execute(request, seal, plan["preview_sha256"])
    monkeypatch.setattr(tx, "begin_transaction", original)
    journal = replacement._load(request["journal"])
    assert journal["state"] == "NEW_INSTALLED" and journal["owner"] is None
    with replacement._operation(request["journal"], journal["request_digest"]):
        before = replacement._tree(request["output"])
    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, seal, plan["preview_sha256"])
    with replacement._operation(request["journal"], journal["request_digest"]):
        assert replacement._tree(request["output"]) == before
    quarantine = Path(request["journal"]).parent / "owner-orphan"
    assert replacement.rollback(request, replacement._sha(replacement._bytes(journal)), quarantine)["status"] == "rolled-back-unrecovered"
    assert replacement._tree(quarantine) == before


def test_hosted_backend_fragment_refused_without_network(case, monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    request, fragments = case
    assert replacement.build_data(request, fragments)["producer"] == replacement.PROFILE
    fragments[0]["backend"] = "gemini"
    with pytest.raises(tx.PendingTransactionError, match="host-Codex"):
        replacement.build_data(request, fragments)


def test_competing_exact_operations_publish_once(case, modeled, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    original = tx.begin_transaction
    owners = []
    barrier = threading.Barrier(2)
    def count(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner.id)
        return owner
    monkeypatch.setattr(tx, "begin_transaction", count)
    def run():
        barrier.wait(timeout=10)
        return replacement.execute(request, seal, plan["preview_sha256"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert len(owners) == 1
    assert results[0] == results[1]


def test_journal_slot_lock_is_independent_of_request(case):
    request, _ = case
    with replacement._coordinator_lock(request):
        pass
    other = {**request, "directed": False}
    with replacement._coordinator_lock(other):
        pass
    assert len(list(Path(request["journal"]).parent.glob(".replacement-lock-*"))) == 1


def test_control_parent_copy_refuses_without_mutation(case, modeled, tmp_path):
    import shutil
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    def stop(point):
        if point == "ALLOCATED":
            raise RuntimeError("interruption")
    with pytest.raises(RuntimeError):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=stop)
    control = Path(request["journal"]).parent
    held = tmp_path / "original-control"
    control.rename(held)
    shutil.copytree(held, control)
    before = replacement._tree(control)
    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, seal, plan["preview_sha256"])
    assert replacement._tree(control) == before
    assert Path(request["output"]).is_dir()


@pytest.mark.parametrize("label", ["git", "policy"])
def test_rollback_quarantine_cannot_enter_extra_root(case, modeled, tmp_path, label):
    request, fragments = case
    policy = tmp_path / "policy"
    policy.mkdir()
    request["extra_roots"] = [{"label": label, "path": str(policy)}]
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    journal = replacement._load(request["journal"])
    before = replacement._tree(Path(request["journal"]).parent)
    with pytest.raises(tx.PendingTransactionError, match="overlap"):
        replacement.rollback(request, replacement._sha(replacement._bytes(journal)), policy / "new",
                             expected_new_tree=replacement.status(request)["new_tree_sha256"])
    assert replacement._tree(Path(request["journal"]).parent) == before


def test_cross_source_semantic_id_collision_refused(case):
    request, fragments = case
    doc = Path(request["root"]) / "another.md"
    doc.write_text("Another concept.\n")
    import copy
    fragment = copy.deepcopy(fragments[0])
    fragment["source_sha256"] = {str(doc): hashlib.sha256(doc.read_bytes()).hexdigest()}
    fragment["nodes"][0]["source_file"] = str(doc)
    with pytest.raises(tx.PendingTransactionError, match="identity"):
        replacement.build_data(request, [*fragments, fragment])


def test_semantic_id_cannot_shadow_ast_source(case):
    request, fragments = case
    fragments[0]["nodes"][0]["id"] = "a_alpha"
    with pytest.raises(tx.PendingTransactionError, match="identity"):
        replacement.build_data(request, fragments)


def test_control_filesystem_preflight_precedes_archival(case, monkeypatch, tmp_path):
    request, fragments = case
    control = tmp_path / "control"
    control.mkdir()
    request["journal"] = str(control / "journal.json")
    original = replacement._directory
    def foreign(path):
        value = original(path)
        return {**value, "device": value["device"] + 1} if Path(path) == control else value
    monkeypatch.setattr(replacement, "_directory", foreign)
    request["parent_identities"]["control"] = foreign(control)
    seal = replacement.build_data(request, fragments)
    with pytest.raises(tx.PendingTransactionError, match="filesystem"):
        replacement.preview(request, seal)
    assert not Path(request["archive"]).exists()
    assert not Path(request["journal"]).exists()


def test_rollback_filesystem_preflight_has_no_control_effects(case, modeled, monkeypatch, tmp_path):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    original = replacement._directory
    def different(path):
        value = original(path)
        return {**value, "device": value["device"] + 1} if Path(path) == foreign else value
    journal = replacement._load(request["journal"])
    before = replacement._tree(Path(request["journal"]).parent)
    monkeypatch.setattr(replacement, "_directory", different)
    with pytest.raises(tx.PendingTransactionError, match="filesystem"):
        replacement.rollback(request, replacement._sha(replacement._bytes(journal)), foreign / "new",
                             expected_new_tree=replacement.status(request)["new_tree_sha256"])
    assert replacement._tree(Path(request["journal"]).parent) == before
    assert list(foreign.iterdir()) == []


@pytest.mark.parametrize("confidence,score", [("EXTRACTED", 0.2), (None, 1.0), ("INFERRED", float("inf"))])
def test_invalid_semantic_confidence_refused(case, confidence, score):
    request, fragments = case
    fragment = fragments[0]
    first = fragment["nodes"][0]
    fragment["nodes"].append({**first, "id": "guide_other", "label": "Other"})
    fragment["edges"] = [{"source": first["id"], "target": "guide_other", "source_file": first["source_file"],
                          "relation": "references", "confidence": confidence, "confidence_score": score}]
    with pytest.raises(tx.PendingTransactionError, match="confidence"):
        replacement.build_data(request, fragments)


def test_different_requests_same_journal_have_one_winner(case, modeled, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    request, fragments = case
    other = {**request, "directed": False}
    candidates = [(r, replacement.build_data(r, fragments)) for r in (request, other)]
    candidates = [(r, s, replacement.preview(r, s)["preview_sha256"]) for r, s in candidates]
    original = tx.begin_transaction
    owners = []
    def count(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner.id)
        return owner
    monkeypatch.setattr(tx, "begin_transaction", count)
    barrier = threading.Barrier(2)
    def run(candidate):
        barrier.wait(timeout=10)
        try:
            return replacement.execute(*candidate)["status"]
        except tx.PendingTransactionError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, candidates))
    assert sorted(results) == ["blocked", "complete"] and len(owners) == 1
    journal = replacement._load(request["journal"])
    winner = candidates[results.index("complete")]
    assert journal["request"] == winner[0] and journal["state"] == "COMMITTED"
    before = replacement._tree(Path(request["journal"]).parent)
    loser = candidates[results.index("blocked")]
    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(*loser)
    assert replacement._tree(Path(request["journal"]).parent) == before


@pytest.mark.parametrize("bad", [True, 1.0, None])
def test_parent_identity_fields_are_strict(case, bad):
    request, fragments = case
    request["parent_identities"]["control"]["inode"] = bad
    with pytest.raises(tx.PendingTransactionError):
        replacement.build_data(request, fragments)


def test_bind_existing_request_does_not_refresh_parent(case, tmp_path):
    import shutil
    request, _ = case
    control = Path(request["journal"]).parent
    held = tmp_path / "old-control"
    control.rename(held)
    shutil.copytree(held, control)
    with pytest.raises(tx.PendingTransactionError, match="parent identity"):
        replacement.bind_request(request)
