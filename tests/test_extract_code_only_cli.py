"""`graphify extract --code-only` indexes code without an LLM key (#1734).

A mixed repo (code + docs) with no API key configured used to hard-fail on the
doc/paper/image files. `--code-only` skips the semantic pass so the code graph
still builds, and the no-key error now points users at the flag.
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

PYTHON = sys.executable
_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
             "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY")


def _mixed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def hello():\n    return 1\n")
    (repo / "README.md").write_text("# Design\n\nHow it works.\n")
    (repo / "NOTES.txt").write_text("Architecture notes and rationale.\n")
    return repo


def _run(repo: Path, *extra: str, output=None):
    env = {k: v for k, v in os.environ.items() if k not in _KEY_VARS}
    env["GRAPHIFY_OUT"] = str((output or repo) / "graphify-out")
    return subprocess.run(
        [PYTHON, "-m", "graphify", "extract", ".", *extra],
        cwd=repo, capture_output=True, text=True, env=env,
    )


def test_code_only_succeeds_without_key(tmp_path):
    repo = _mixed_repo(tmp_path)
    r = _run(repo, "--code-only")
    assert r.returncode == 0, f"--code-only should succeed with no key: {r.stderr}"
    out = r.stdout + r.stderr
    assert "--code-only: skipping" in out
    graph = repo / "graphify-out" / "graph.json"
    assert graph.exists(), "code graph must still be written"
    import json
    g = json.loads(graph.read_text())
    labels = [n.get("label") for n in g["nodes"]]
    assert any(str(l).startswith("hello") for l in labels), "code was indexed"


def test_mixed_repo_without_key_errors_and_points_at_code_only(tmp_path):
    repo = _mixed_repo(tmp_path)
    r = _run(repo)  # no --code-only, no key
    assert r.returncode != 0, "mixed repo with no key should still error without the flag"
    assert "--code-only" in r.stderr, "the no-key error must point users at --code-only"


def _refresh_repo(tmp_path, output=None):
    from graphify.build import build
    from graphify.detect import save_manifest
    from graphify.export import to_json
    from graphify.extract import extract

    repo = tmp_path / "corpus"
    repo.mkdir()
    imports = "from typing import Any\nfrom decimal import Decimal\nfrom model import MarketObservation\n"
    for name in ("first", "second"):
        (repo / f"{name}.py").write_text(
            imports + f"def {name}(value: Any) -> Decimal:\n    return MarketObservation(value)\n"
        )
    (repo / "model.py").write_text("class MarketObservation:\n    pass\n")
    (repo / "fixture.json").write_text('{"prices": [[1, 2]], "total_volumes": [[1, 3]]}\n')
    (repo / ".gitignore").write_text("graphify-out/\n")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "baseline")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    paths = sorted(repo.glob("*.py"))
    graph_path = (output or repo) / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    to_json(build([extract(paths, cache_root=output or repo, parallel=False)], root=repo), {}, str(graph_path))
    save_manifest({"code": [str(p) for p in paths + [repo / "fixture.json"]]},
                  str(graph_path.parent / "manifest.json"), root=repo)
    return repo, graph_path


def _append_refresh_function(repo):
    with (repo / "first.py").open("a") as stream:
        stream.write("\ndef refresh_beacon(value: Any) -> Decimal:\n    return MarketObservation(value)\n")


def test_public_code_refresh_preserves_accepted_price_volume_evidence(tmp_path):
    repo, graph_path = _refresh_repo(tmp_path)
    data = json.loads(graph_path.read_text())
    facts = [
        {"id": name, "label": f"Raw CoinGecko fixture {name} array",
         "file_type": "concept", "source_file": "fixture.json",
         "source_location": location, "confidence": "EXTRACTED",
         "quotation": quotation}
        for name, location, quotation in (
            ("price", "L1", '"prices": [[1, 2]]'),
            ("volume", "L1", '"total_volumes": [[1, 3]]'))
    ]
    evidence = {"id": "evidence", "label": "Raw fixture evidence",
                "file_type": "document", "source_file": "fixture.json"}
    edges = [{"source": "evidence", "target": fact["id"], "relation": "supports",
              "confidence": "EXTRACTED", "source_file": "fixture.json",
              "source_location": fact["source_location"], "quotation": fact["quotation"]}
             for fact in facts]
    data["nodes"].extend([*facts, evidence])
    data.setdefault("links", []).extend(edges)
    data["hyperedges"] = [{"id": "paired", "nodes": ["price", "volume"],
                           "source_file": "fixture.json", "confidence": "EXTRACTED"}]
    graph_path.write_text(json.dumps(data))
    _append_refresh_function(repo)
    result = _run(repo, "--code-only", "--no-viz")
    assert result.returncode == 0, result.stdout + result.stderr
    refreshed = json.loads(graph_path.read_text())
    nodes = {n["id"]: n for n in refreshed["nodes"]}
    for fact in facts:
        assert fact["id"] in nodes, f"accepted fact lost: {fact['label']}"
        assert {k: nodes[fact["id"]][k] for k in fact} == fact
    for edge in edges:
        assert any(all(actual.get(k) == v for k, v in edge.items())
                   for actual in refreshed["links"]), edge
    assert refreshed["hyperedges"] == data["hyperedges"]


def test_public_code_refresh_matches_full_reference_identities(tmp_path):
    from graphify.build import build
    from graphify.extract import extract

    repo, graph_path = _refresh_repo(tmp_path)
    _append_refresh_function(repo)
    full = build([extract(sorted(repo.glob("*.py")), cache_root=repo, parallel=False)], root=repo)
    result = _run(repo, "--code-only", "--no-viz")
    assert result.returncode == 0, result.stdout + result.stderr
    refreshed = json.loads(graph_path.read_text())
    labels = {"Any", "Decimal", "MarketObservation"}
    expected = {node for node, attrs in full.nodes(data=True) if attrs.get("label") in labels}
    actual = {n["id"] for n in refreshed["nodes"] if n.get("label") in labels}
    assert actual == expected
    provenance = ("relation", "confidence", "source_file", "source_location", "_origin")
    expected_edges = {(attrs["_src"], attrs["_tgt"], *(attrs.get(key) for key in provenance))
                      for a, b, attrs in full.edges(data=True) if a in expected or b in expected}
    actual_edges = {(edge["source"], edge["target"], *(edge.get(key) for key in provenance))
                    for edge in refreshed["links"] if edge["source"] in actual or edge["target"] in actual}
    assert actual_edges == expected_edges
    # Repeating the prepared refresh must preserve logical AST records.
    repeated = _run(repo, "--code-only", "--no-viz")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    again = json.loads(graph_path.read_text())
    def logical(payload):
        return {key: [{k: v for k, v in item.items() if k not in {"community", "community_name"}}
                      for item in payload[key]] for key in ("nodes", "links")}
    assert logical(again) == logical(refreshed)


@pytest.mark.parametrize("baseline", ["unchanged", "changed_same_mtime", "unknown", "conflicting"])
def test_public_code_refresh_uses_byte_baseline_for_code_semantics(tmp_path, baseline):
    repo, graph_path = _refresh_repo(tmp_path)
    data = json.loads(graph_path.read_text())
    fact = {"id": "code_fact", "label": "Accepted second module claim", "file_type": "concept",
            "source_file": "second.py", "source_location": "L4", "confidence": "EXTRACTED"}
    data["nodes"].append(fact)
    graph_path.write_text(json.dumps(data))
    manifest_path = graph_path.parent / "manifest.json"
    if baseline in {"unknown", "conflicting"}:
        manifest = json.loads(manifest_path.read_text())
        if baseline == "unknown":
            manifest["second.py"]["ast_hash"] = ""
            manifest["second.py"]["semantic_hash"] = ""
        else:
            manifest["second.py"]["semantic_hash"] = "0" * 32
        manifest_path.write_text(json.dumps(manifest))
    elif baseline == "changed_same_mtime":
        path = repo / "second.py"
        previous = path.stat()
        path.write_text(path.read_text().replace("value", "other"))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    _append_refresh_function(repo)
    before = (graph_path.read_bytes(), manifest_path.read_bytes())
    result = _run(repo, "--code-only", "--no-viz")
    if baseline in {"unknown", "conflicting"}:
        assert result.returncode != 0, "unverifiable accepted semantic baseline must refuse publication"
        assert (graph_path.read_bytes(), manifest_path.read_bytes()) == before
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        nodes = {n["id"]: n for n in json.loads(graph_path.read_text())["nodes"]}
        assert ("code_fact" in nodes) == (baseline == "unchanged")
        if baseline == "unchanged":
            assert {k: nodes["code_fact"][k] for k in fact} == fact


@pytest.mark.parametrize("failure", ["missing", "error", "raised", "hash_read", "registered_resolver", "inner_import_read"])
def test_public_code_refresh_withholds_failed_ast_publication(tmp_path, monkeypatch, failure):
    import graphify.__main__ as mainmod
    import graphify.extract as extraction
    from graphify.transaction import RECEIPT_FILE

    repo, graph_path = _refresh_repo(tmp_path)
    if failure == "inner_import_read":
        from graphify.build import build
        from graphify.export import to_json
        from graphify.detect import save_manifest
        (repo / "first.py").write_text("from second import Response\nclass Auth: pass\n")
        (repo / "second.py").write_text("class Response: pass\n")
        paths = sorted(repo.glob("*.py"))
        baseline = extraction.extract(paths, cache_root=repo, parallel=False, strict=True)
        assert any(edge.get("relation") == "uses" for edge in baseline["edges"])
        to_json(build([baseline], root=repo), {}, str(graph_path))
        save_manifest({"code": [str(path) for path in paths]},
                      str(graph_path.parent / "manifest.json"), root=repo)
    if failure == "hash_read":
        data = json.loads(graph_path.read_text())
        data["nodes"].append({"id": "accepted", "label": "Accepted second module claim",
                              "source_file": "second.py", "file_type": "concept"})
        graph_path.write_text(json.dumps(data))
    _append_refresh_function(repo)
    protected = [graph_path, graph_path.parent / "manifest.json", graph_path.parent / RECEIPT_FILE]
    before = {path: path.read_bytes() if path.exists() else None for path in protected}

    def failed_worker(work, results, *args):
        if failure == "raised":
            raise RuntimeError("injected extraction failure")
        if failure == "error":
            for index, _ in work:
                results[index] = {"nodes": [], "edges": [], "error": "injected extraction failure"}

    reads = []
    if failure == "inner_import_read":
        original_read = Path.read_bytes
        def inner_read(path):
            if path == repo / "first.py" and sys._getframe(1).f_code.co_name == "_resolve_cross_file_imports":
                reads.append(path)
                raise OSError("injected inner import read")
            return original_read(path)
        monkeypatch.setattr(Path, "read_bytes", inner_read)
    elif failure == "hash_read":
        import graphify.detect as detection
        original_hash = detection._md5_file
        monkeypatch.setattr(detection, "_md5_file", lambda path: "" if path.name == "second.py" else original_hash(path))
    elif failure == "registered_resolver":
        import graphify.resolver_registry as registry
        def failed_resolver(*args):
            raise RuntimeError("injected registered resolver failure")
        monkeypatch.setattr(registry, "_REGISTRY", [registry.LanguageResolver("injected", frozenset({".py"}), failed_resolver)])
    else:
        monkeypatch.setattr(extraction, "load_cached", lambda *a, **k: None)
        monkeypatch.setattr(extraction, "_extract_sequential", failed_worker)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setenv("GRAPHIFY_OUT", str(graph_path.parent))
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "extract", str(repo), "--code-only", "--no-viz"])
    with pytest.raises(SystemExit) as failed:
        mainmod.main()
    assert failed.value.code not in (None, 0)
    if failure == "inner_import_read":
        assert reads
    assert {path: path.read_bytes() if path.exists() else None for path in protected} == before


def test_public_code_refresh_ignores_same_stat_stale_ast_cache(tmp_path, monkeypatch):
    import graphify.cache as cache
    from graphify.extract import extract

    monkeypatch.setattr(cache, "_stat_index", {})
    monkeypatch.setattr(cache, "_stat_index_root", None)
    repo, graph_path = _refresh_repo(tmp_path)
    cache._flush_stat_index()
    path = repo / "second.py"
    old_stat = path.stat()
    path.write_text(path.read_text().replace("def second(", "def modern("))
    assert path.stat().st_size == old_stat.st_size
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    _append_refresh_function(repo)
    result = _run(repo, "--code-only", "--no-viz")
    assert result.returncode == 0, result.stdout + result.stderr
    cold = tmp_path / "cold"
    cold.mkdir()
    for source in repo.glob("*.py"):
        (cold / source.name).write_bytes(source.read_bytes())
    oracle = extract(sorted(cold.glob("*.py")), cache_root=cold, parallel=False)
    expected = {n["label"] for n in oracle["nodes"] if n.get("label") in {"second()", "modern()"}}
    actual = {n["label"] for n in json.loads(graph_path.read_text())["nodes"]
              if n.get("label") in {"second()", "modern()"}}
    assert expected == {"modern()"}
    assert actual == expected


@pytest.mark.parametrize("suffix", ["vue", "svelte", "astro"])
@pytest.mark.parametrize("parallel", [False, True])
def test_public_code_refresh_withholds_vue_read_failure(tmp_path, monkeypatch, parallel, suffix):
    import graphify.__main__ as mainmod
    import graphify.extract as extraction
    from graphify.detect import save_manifest
    from graphify.transaction import RECEIPT_FILE

    repo, graph_path = _refresh_repo(tmp_path)
    vue = repo / ("component." + suffix)
    vue.write_text('<script>function renderCard() { return 1; }</script>{import("./child.js")}')
    child = repo / "child.js"
    child.write_text("export function child() {}")
    fragment = extraction.extract([vue, child], cache_root=repo, parallel=False)
    assert any(edge.get("relation") == "dynamic_import" for edge in fragment["edges"])
    data = json.loads(graph_path.read_text())
    data["nodes"].extend(fragment["nodes"])
    data["links"].extend(fragment["edges"])
    graph_path.write_text(json.dumps(data))
    save_manifest({"code": [str(vue)]}, str(graph_path.parent / "manifest.json"), root=repo)
    _append_refresh_function(repo)
    # Keep the real shrink guard out of the failure assertion: publication would grow.
    with (repo / "first.py").open("a") as stream:
        stream.write("\ndef extra_vue_control():\n    return 1\n")
    protected = [graph_path, graph_path.parent / "manifest.json", graph_path.parent / RECEIPT_FILE]
    before = {path: path.read_bytes() if path.exists() else None for path in protected}
    original_read = Path.read_text
    reads = []
    def failed_read(path, *args, **kwargs):
        if path == vue and sys._getframe(1).f_code.co_name == "extract_" + suffix:
            reads.append(path)
            raise OSError("injected component read failure")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", failed_read)
    monkeypatch.setattr(extraction, "load_cached", lambda *a, **k: None)
    assert "error" not in extraction.extract([vue, child], cache_root=repo, parallel=False)
    assert reads
    reads.clear()
    monkeypatch.setattr(extraction, "_PARALLEL_THRESHOLD", 1 if parallel else 1000)
    if parallel:
        import concurrent.futures
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "extract", str(repo), "--code-only", "--no-viz"])
    with pytest.raises(SystemExit) as failed:
        mainmod.main()
    assert failed.value.code not in (None, 0)
    assert reads
    assert {path: path.read_bytes() if path.exists() else None for path in protected} == before


@pytest.mark.parametrize("state", ["changed", "unchanged", "unknown", "prepared_parent_output"])
def test_public_code_refresh_external_output_source_ownership(tmp_path, state):
    from graphify.build import build
    from graphify.extract import extract
    from graphify.transaction import RECEIPT_FILE

    output = tmp_path if state == "prepared_parent_output" else tmp_path / "output"
    repo, graph_path = _refresh_repo(tmp_path, output=output)
    fact = {"id": "accepted", "label": "Accepted second module claim", "file_type": "concept",
            "source_file": os.path.relpath(repo / "second.py", output), "quotation": "unchanged evidence"}
    data = json.loads(graph_path.read_text())
    data["nodes"].append(fact)
    graph_path.write_text(json.dumps(data))
    if state in {"changed", "prepared_parent_output"}:
        path = repo / "second.py"
        path.write_text(path.read_text().replace("value", "other"))
    elif state == "unknown":
        manifest_path = graph_path.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["second.py"] = {"mtime": (repo / "second.py").stat().st_mtime}
        manifest_path.write_text(json.dumps(manifest))
    _append_refresh_function(repo)
    protected = [graph_path, graph_path.parent / "manifest.json", graph_path.parent / RECEIPT_FILE]
    before = {path: path.read_bytes() if path.exists() else None for path in protected}
    result = _run(repo, "--code-only", "--no-viz", "--out", str(output), output=output)
    if state == "unknown":
        assert result.returncode != 0, "external-output semantic baseline must refuse when unknown"
        assert {path: path.read_bytes() if path.exists() else None for path in protected} == before
        return
    assert result.returncode == 0, result.stdout + result.stderr
    refreshed = json.loads(graph_path.read_text())
    nodes = {node["id"]: node for node in refreshed["nodes"]}
    assert ("accepted" in nodes) == (state == "unchanged")
    if state == "unchanged":
        assert {key: nodes["accepted"][key] for key in fact} == fact
    oracle = build([extract(sorted(repo.glob("*.py")), cache_root=output, strict=True)], root=repo)
    assert {node["id"] for node in refreshed["nodes"] if node.get("_origin") == "ast"} == set(oracle)
    fields = ("relation", "confidence", "source_file", "source_location", "_origin")
    assert {(edge["source"], edge["target"], *(edge.get(key) for key in fields))
            for edge in refreshed["links"] if edge.get("_origin") == "ast"} == {
                (attrs["_src"], attrs["_tgt"], *(attrs.get(key) for key in fields))
                for _, _, attrs in oracle.edges(data=True)}


def test_public_code_refresh_refuses_used_ambiguous_source_alias(tmp_path):
    from graphify.detect import save_manifest

    output = tmp_path / "corpus" / "nested"
    repo, graph_path = _refresh_repo(tmp_path, output=output)
    nested = output / "second.py"
    nested.write_text("def nested_symbol():\n    return 1\n")
    save_manifest({"code": [str(nested)]}, str(graph_path.parent / "manifest.json"), root=repo)
    data = json.loads(graph_path.read_text())
    data["nodes"].append({"id": "ambiguous", "label": "Ambiguous accepted source",
                          "source_file": "second.py", "file_type": "concept"})
    graph_path.write_text(json.dumps(data))
    _append_refresh_function(repo)
    before = graph_path.read_bytes()
    result = _run(repo, "--code-only", "--no-viz", "--out", str(output), output=output)
    assert result.returncode != 0, "used scan/output alias must have one source identity"
    assert graph_path.read_bytes() == before


@pytest.mark.parametrize("spelling", ["absolute", "scan_relative"])
def test_code_refresh_cross_drive_output_alias(tmp_path, monkeypatch, spelling):
    import ntpath
    from graphify.cli import _code_refresh_sources

    output = tmp_path / "output"
    repo, graph_path = _refresh_repo(tmp_path, output=output)
    source = repo / "second.py"
    fact = {"id": "accepted", "label": "Accepted cross-drive claim", "file_type": "concept",
            "source_file": str(source) if spelling == "absolute" else "second.py"}
    data = json.loads(graph_path.read_text())
    data["nodes"].append(fact)
    graph_path.write_text(json.dumps(data))
    manifest_path = graph_path.parent / "manifest.json"
    before = (graph_path.read_bytes(), manifest_path.read_bytes())
    relpath = os.path.relpath
    attempted = []

    def cross_drive(path, start):
        if Path(start) == output:
            attempted.append(str(path))
            return ntpath.relpath(r"C:\corpus\second.py", r"D:\output")
        return relpath(path, start)

    monkeypatch.setattr(os.path, "relpath", cross_drive)
    paths = sorted(repo.glob("*.py"))
    changed, aliases = _code_refresh_sources(
        graph_path, manifest_path, paths, repo, output, [str(p) for p in paths])
    assert attempted
    assert changed == []
    assert aliases[os.path.normpath(fact["source_file"])] == "second.py"
    assert aliases[str(source)] == aliases["second.py"] == "second.py"
    assert (graph_path.read_bytes(), manifest_path.read_bytes()) == before


def test_code_refresh_output_alias_oserror_propagates(tmp_path, monkeypatch):
    from graphify.cli import _code_refresh_sources

    output = tmp_path / "output"
    repo, graph_path = _refresh_repo(tmp_path, output=output)
    relpath = os.path.relpath

    def denied_output(path, start):
        if Path(start) == output:
            raise OSError("output alias denied")
        return relpath(path, start)

    monkeypatch.setattr(os.path, "relpath", denied_output)
    paths = sorted(repo.glob("*.py"))
    with pytest.raises(OSError, match="output alias denied"):
        _code_refresh_sources(graph_path, graph_path.parent / "manifest.json",
                              paths, repo, output, [str(p) for p in paths])



def test_public_update_invalidates_retained_code_semantic_baseline(tmp_path):
    from graphify.transaction import RECEIPT_FILE

    repo, graph_path = _refresh_repo(tmp_path)
    data = json.loads(graph_path.read_text())
    fact = {"id": "accepted", "label": "Accepted second module evidence",
            "file_type": "concept", "source_file": "second.py", "quotation": "old bytes"}
    data["nodes"].append(fact)
    graph_path.write_text(json.dumps(data))
    source = repo / "second.py"
    source.write_text(source.read_text() + "\ndef newly_observed():\n    return 7\n")
    env = {k: v for k, v in os.environ.items() if k not in _KEY_VARS}
    env["GRAPHIFY_OUT"] = str(graph_path.parent)
    updated = subprocess.run([PYTHON, "-m", "graphify", "update", "."], cwd=repo,
                             env=env, capture_output=True, text=True)
    assert updated.returncode == 0, updated.stdout + updated.stderr
    manifest_path = graph_path.parent / "manifest.json"
    entry = json.loads(manifest_path.read_text())["second.py"]
    assert entry["ast_hash"] and entry["semantic_hash"] == ""
    retained = next(n for n in json.loads(graph_path.read_text())["nodes"] if n["id"] == "accepted")
    assert {k: retained[k] for k in fact} == fact
    protected = [graph_path, manifest_path, graph_path.parent / RECEIPT_FILE]
    before = {p: p.read_bytes() if p.exists() else None for p in protected}
    result = _run(repo, "--code-only", "--no-viz")
    assert result.returncode != 0, "AST update cannot certify retained semantic freshness"
    assert {p: p.read_bytes() if p.exists() else None for p in protected} == before


@pytest.mark.parametrize("shape,retained", [("blank", True), ("legacy", True),
                                           ("blank", False), ("legacy", False)])
def test_code_refresh_requires_semantic_stage_proof_only_for_retained_sources(tmp_path, shape, retained):
    from graphify.cli import _code_refresh_sources

    repo, graph_path = _refresh_repo(tmp_path)
    data = json.loads(graph_path.read_text())
    if retained:
        data["nodes"].append({"id": "accepted", "label": "Accepted code evidence",
                              "file_type": "concept", "source_file": "second.py"})
        graph_path.write_text(json.dumps(data))
    manifest_path = graph_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["second.py"]
    manifest["second.py"] = ({"mtime": entry["mtime"], "hash": entry["ast_hash"]}
                             if shape == "legacy" else {**entry, "semantic_hash": ""})
    manifest_path.write_text(json.dumps(manifest))
    before = (graph_path.read_bytes(), manifest_path.read_bytes())
    paths = sorted(repo.glob("*.py"))
    args = (graph_path, manifest_path, paths, repo, repo, [str(p) for p in paths])
    if retained:
        with pytest.raises(ValueError, match="semantic.*baseline"):
            _code_refresh_sources(*args)
    else:
        changed, aliases = _code_refresh_sources(*args)
        assert changed == [] and aliases["second.py"] == "second.py"
    assert (graph_path.read_bytes(), manifest_path.read_bytes()) == before


@pytest.mark.parametrize("retained_semantic", [False, True])
def test_public_code_refresh_historical_alias_ownership(tmp_path, retained_semantic):
    from graphify.build import build
    from graphify.detect import save_manifest
    from graphify.extract import extract
    from graphify.transaction import RECEIPT_FILE

    output = tmp_path / "corpus" / "nested"
    repo, graph_path = _refresh_repo(tmp_path, output=output)
    historical = output / "second.py"
    historical.write_text("def historical_symbol():\n    return 1\n")
    save_manifest({"code": [str(historical)]}, str(graph_path.parent / "manifest.json"), root=repo)
    data = json.loads(graph_path.read_text())
    data["nodes"].append({"id": "historical", "label": "Historical nested symbol",
                          "file_type": "code", "source_file": "second.py", "_origin": "ast"})
    if retained_semantic:
        data["nodes"].append({"id": "ambiguous", "label": "Historical accepted claim",
                              "file_type": "concept", "source_file": "second.py"})
    graph_path.write_text(json.dumps(data))
    historical.unlink()
    _append_refresh_function(repo)
    protected = [graph_path, graph_path.parent / "manifest.json", graph_path.parent / RECEIPT_FILE]
    before = {p: p.read_bytes() if p.exists() else None for p in protected}
    result = _run(repo, "--code-only", "--no-viz", "--out", str(output), output=output)
    if retained_semantic:
        assert result.returncode != 0, "historical aliases must still constrain retained semantics"
        assert {p: p.read_bytes() if p.exists() else None for p in protected} == before
        return
    assert result.returncode == 0, result.stdout + result.stderr
    oracle = build([extract(sorted(repo.glob("*.py")), cache_root=output, strict=True)], root=repo)
    refreshed = json.loads(graph_path.read_text())
    assert {n["id"] for n in refreshed["nodes"] if n.get("_origin") == "ast"} == set(oracle)
    fields = ("relation", "confidence", "source_file", "source_location", "_origin")
    assert {(e["source"], e["target"], *(e.get(k) for k in fields))
            for e in refreshed["links"] if e.get("_origin") == "ast"} == {
                (a["_src"], a["_tgt"], *(a.get(k) for k in fields)) for _, _, a in oracle.edges(data=True)}
