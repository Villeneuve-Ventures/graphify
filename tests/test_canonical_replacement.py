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


def test_external_evidence_cannot_overlap_policy_root(case):
    request, fragments = case
    request["extra_roots"] = [{"label": "policy", "path": str(Path(request["archive"]).parent)}]
    with pytest.raises(tx.PendingTransactionError, match="overlaps"):
        replacement.build_data(request, fragments)


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
