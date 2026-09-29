"""S4 real engine evidence; fixture repositories never use providers."""
import os

import pytest

from graphify.workspace.adapters import AdapterIntent, CompatibilityTuple, select_adapter
from tests.test_workspace_contracts import compatibility
from tests.workspace_s3_helpers import create_repo, tree_snapshot


def adapter():
    candidate = CompatibilityTuple(compatibility())
    return select_adapter(candidate, expected=candidate, intent=AdapterIntent.EXECUTE).require_adapter()


def test_v8_operational_adapter_builds_and_queries_known_relation(tmp_path):
    from graphify.workspace.adapters.base import QueryRequest

    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "main.py").write_text("def leaf():\n    return 42\n\ndef caller():\n    return leaf()\n")
    before = tree_snapshot(repo)
    engine = adapter()
    initial = engine.observe(repo)
    payload = tmp_path / "payload"
    payload.mkdir(mode=0o700)
    fd = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
    try:
        built = engine.build_structural(repo, payload_fd=fd, scratch_fd=fd,
                                        initial_detection=initial.initial_detection)
        final = engine.observe(repo, input_manifest=built.input_manifest)
        assert final.initial_detection == initial.initial_detection
        assert final.consumed_inputs == built.input_manifest
        assert final.consumed_inputs.complete
        assert built.node_count > 0 and built.edge_count > 0
        text = engine.query_structural(fd, QueryRequest("caller"))
        assert "caller" in text and "leaf" in text
    finally:
        os.close(fd)
    assert tree_snapshot(repo) == before


def _build(engine, repo, output):
    initial = engine.observe(repo)
    output.mkdir(mode=0o700)
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = engine.build_structural(repo, payload_fd=fd, scratch_fd=fd,
                                        initial_detection=initial.initial_detection)
    finally:
        os.close(fd)
    return initial, result


@pytest.mark.parametrize("change", ["metadata", "negative", "membership", "noncode", "policy", "git", "code"])
def test_consumed_observation_rejects_material_drift(tmp_path, change):
    from graphify.source_io import SourceError
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "app.ts").write_text("import {value} from '@lib'; console.log(value);")
    (repo / "lib.ts").write_text("export const value = 1;")
    (repo / "tsconfig.json").write_text('{"compilerOptions":{"paths":{"@lib":["lib.ts"]}}}')
    (repo / ".graphifyignore").write_text("tsconfig.json\n")
    engine = adapter()
    initial, built = _build(engine, repo, tmp_path / "payload")
    detection = {(e["operation"], e["path"]) for e in initial.initial_detection.to_dict()["evidence"]}
    consumed = {(e["operation"], e["path"]) for e in built.input_manifest.to_dict()["evidence"]}
    assert ("read", "tsconfig.json") in consumed - detection
    if change == "metadata":
        (repo / "tsconfig.json").write_text('{"compilerOptions":{}}')
    elif change == "negative":
        negatives = [e["path"] for e in built.input_manifest.to_dict()["evidence"]
                     if e["operation"] == "probe" and e["value"] is None
                     and ":" not in e["path"] and not e["path"].startswith(".git/")]
        assert negatives
        path = repo / negatives[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    elif change == "membership":
        (repo / "new.py").write_text("pass\n")
    elif change == "noncode":
        (repo / "README.md").write_text("original document changed")
    elif change == "policy":
        (repo / ".graphifyignore").write_text("tsconfig.json\nlib.ts\n")
    elif change == "git":
        (repo / ".git/info/exclude").write_text("lib.ts\n")
    else:
        (repo / "lib.ts").write_text("export const value = 2;")
    with pytest.raises((SourceError, ValueError)):
        engine.observe(repo, input_manifest=built.input_manifest)


@pytest.mark.parametrize("name,text,failure_status", [
    ("script.r", "x <- 1", None),
    ("script.F90", "program a\nend program", "unsupported_input"),
])
def test_incomplete_extraction_never_writes_a_payload(tmp_path, name, text, failure_status):
    from graphify.workspace.adapters.base import StructuralBuildIncomplete
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / name).write_text(text)
    output = tmp_path / "payload"
    with pytest.raises(StructuralBuildIncomplete) as caught:
        _build(adapter(), repo, output)
    assert not caught.value.input_manifest.complete
    assert caught.value.input_manifest.to_dict()["failure"] == failure_status
    assert list(output.iterdir()) == []


def test_missing_parser_refuses_and_valid_empty_is_explicit(tmp_path, monkeypatch):
    import sys
    from graphify.workspace.adapters.base import StructuralBuildIncomplete
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "empty.sql").write_text("")
    engine = adapter()
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "tree_sitter_sql", None)
        with pytest.raises(StructuralBuildIncomplete):
            _build(engine, repo, tmp_path / "failed")
    _initial, result = _build(engine, repo, tmp_path / "complete")
    assert {"path": "empty.sql", "status": "success"} in result.dispositions
    # SQL deliberately emits a file node even for empty input; JSON can be
    # genuinely node-free and must carry the explicit empty disposition.
    (repo / "empty.json").write_text("{}")
    _initial, result = _build(engine, repo, tmp_path / "empty-complete")
    assert {"path": "empty.json", "status": "empty"} in result.dispositions


def test_original_office_bytes_and_google_refusal(tmp_path, monkeypatch):
    from graphify.source_io import SourceError
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "notes.docx").write_bytes(b"original office input")
    monkeypatch.setattr("graphify.detect.convert_office_file", lambda *a, **k: pytest.fail("conversion"))
    monkeypatch.setattr("graphify.detect.convert_google_workspace_file", lambda *a, **k: pytest.fail("provider"))
    before = tree_snapshot(repo)
    observation = adapter().observe(repo)
    assert any(e["path"] == "notes.docx" and e["operation"] == "read"
               for e in observation.initial_detection.to_dict()["evidence"])
    assert tree_snapshot(repo) == before
    (repo / "remote.gdoc").write_text('{"url":"https://docs.google.com/document/d/test"}')
    with pytest.raises((SourceError, ValueError)):
        adapter().observe(repo)


def test_adapter_facts_ids_and_query_ranking_match_v8(tmp_path):
    import io
    import json
    from graphify.detect import detect
    from graphify.extract import extract
    from graphify.build import build_from_json
    from graphify.export import write_json
    from graphify.serve import _query_graph_text, memory_query_segmenter
    from graphify.workspace.adapters.base import QueryRequest
    from networkx.readwrite import json_graph
    from graphify.workspace.identity import discover_source
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    (repo / "module.cjs").write_text("function helper() { return 1; }\nmodule.exports = helper;")
    engine = adapter()
    _build(engine, repo, tmp_path / "payload")
    ordinary = extract(detect(repo, read_only=True, quiet=True, ambient_output=False)["files"]["code"],
                       source_root=repo, strict=True, parallel=False, quiet=True, ambient_output=False)
    graph = build_from_json(ordinary, directed=True, root=repo)
    stream = io.StringIO()
    write_json(graph, {}, stream, built_at_commit=discover_source(repo).head_commit)
    expected = json.loads(stream.getvalue())
    actual = json.loads((tmp_path / "payload/graph.json").read_text())
    assert actual == expected
    assert all(not n.get("source_file", "").startswith("/") for n in actual["nodes"])
    graph = json_graph.node_link_graph(expected, edges="links")
    fd = os.open(tmp_path / "payload", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for mode in ("bfs", "dfs"):
            request = QueryRequest("caller", mode=mode)
            assert engine.query_structural(fd, request) == _query_graph_text(
                graph, request.question, mode=mode, depth=request.depth,
                token_budget=request.token_budget, context_filters=[], segmenter=memory_query_segmenter())
    finally:
        os.close(fd)


def test_external_resolver_metadata_and_partial_enumeration_refuse(tmp_path, monkeypatch):
    from graphify.workspace.adapters.base import StructuralBuildIncomplete
    from graphify.source_io import SourceIO, SourceEnumerationFailed
    repo = create_repo(tmp_path.resolve() / "repo")
    (tmp_path.resolve() / "outside.json").write_text('{"compilerOptions":{}}')
    (repo / "app.ts").write_text("import {value} from '@lib'; console.log(value);")
    (repo / "tsconfig.json").write_text('{"extends":"../outside.json"}')
    (repo / ".graphifyignore").write_text("tsconfig.json\n")
    with pytest.raises(StructuralBuildIncomplete) as caught:
        _build(adapter(), repo, tmp_path / "payload")
    assert caught.value.input_manifest.to_dict()["failure"] == "unsupported_input"
    assert list((tmp_path / "payload").iterdir()) == []
    original_listdir = SourceIO.listdir
    def unavailable(self, path):
        # Exercise source detection, after the independent Git object preflight.
        if self.root == repo:
            self.refuse("injected partial enumeration", SourceEnumerationFailed)
        return original_listdir(self, path)
    monkeypatch.setattr(SourceIO, "listdir", unavailable)
    with pytest.raises(SourceEnumerationFailed):
        adapter().observe(repo)
