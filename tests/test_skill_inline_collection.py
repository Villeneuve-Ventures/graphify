"""Inline-host collection uses the admitted prepared output, not disk callbacks."""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

from graphify import transaction as tx
from graphify import cache
from tools.skillgen import gen

pytestmark = pytest.mark.skipif(os.name == "nt", reason="native POSIX transaction backend")
HOSTS = ["codex", "opencode"]


def _block(core: str, marker: str) -> str:
    blocks = [block for block in re.findall(r"```bash\n(.*?)\n```", core, re.S)
              if marker in block]
    assert len(blocks) == 1
    return blocks[0]


def _body(core: str, marker: str) -> str:
    raw = _block(core, marker).rsplit('-c "\n', 1)[1].split('\n"', 1)[0]
    return shlex.split('python -c "' + raw + '"')[-1]


@pytest.fixture(params=HOSTS)
def inline_run(tmp_path, monkeypatch, request):
    monkeypatch.setattr(cache, "_stat_index", {})
    monkeypatch.setattr(cache, "_stat_index_root", None)
    monkeypatch.setattr(cache, "_stat_index_dirty", False)
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "note.md"
    source.write_text("A complete source.\n", encoding="utf-8")
    out = root / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text('{"nodes":[{"id":"baseline","label":"Baseline"}],"links":[]}', encoding="utf-8")
    (out / "manifest.json").write_text('{"baseline":true}', encoding="utf-8")
    baseline = {name: (out / name).read_bytes() for name in ("graph.json", "manifest.json")}
    owner = tx.begin_transaction("update", root, output=out,
                                 expected_snapshot=tx.open_graph_snapshot(graph, purpose="inline-collection-test"))
    token = tx.stage_transaction_handoff(owner)
    core = gen.render(gen.load_platforms()[request.param])[0].content

    def run(code):
        tx.run_prepared_token(token.path, ["-c", code.replace("'INPUT_PATH'", repr(str(root)))])

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        run("from pathlib import Path; print(Path.cwd())")
    prepared = Path(captured.getvalue().strip())
    monkeypatch.chdir(root)
    run("from pathlib import Path; Path('.graphify_detect.json').write_text(" + repr(json.dumps(
        {"files": {"document": [str(source)]}})) + ")")
    run(_body(core, "cached_nodes, cached_edges, cached_hyperedges, uncached ="))
    return root, source, out, baseline, prepared, run, core, request.param, token


def _payload(source: Path) -> dict:
    return {"nodes": [{"id": "fact", "label": "Fact", "source_file": str(source)}],
            "edges": [], "hyperedges": [], "completed_sources": [str(source)],
            "input_tokens": 7, "output_tokens": 3}


@pytest.mark.parametrize("kind", ["nonempty", "empty", "partial", "ownership-only", "incomplete", "empty-unattested"])
def test_inline_cache_replay_preserves_completion_and_public_graph(inline_run, kind):
    root, source, out, baseline, prepared, run, core, _, _ = inline_run
    payload = _payload(source)
    if kind in ("empty", "empty-unattested"):
        payload["nodes"] = []
        if kind == "empty-unattested":
            payload.pop("completed_sources")
    elif kind == "partial":
        payload["nodes"][0]["_partial"] = True
    elif kind == "ownership-only":
        payload.pop("completed_sources")
    elif kind == "incomplete":
        payload["incomplete_sources"] = [str(source)]
    run("from pathlib import Path; Path('.graphify_semantic_new.json').write_text(" + repr(json.dumps(payload)) + ")")
    run(_body(core, "Merged {len(chunks)} chunks:"))
    new = json.loads((prepared / ".graphify_semantic_new.json").read_text())
    expected_complete = [str(source)] if kind in ("nonempty", "empty") else []
    assert new["completed_sources"] == expected_complete
    assert not list(out.glob("cache/semantic/*.json")), "collection is distinct from cache saving"
    run(_body(core, "saved = save_semantic_cache("))
    run(_body(core, "all_nodes = cached['nodes']"))
    merged = json.loads((prepared / ".graphify_semantic.json").read_text())
    assert merged["completed_sources"] == expected_complete
    assert merged["nodes"] == payload["nodes"]
    run(_body(core, "cached_nodes, cached_edges, cached_hyperedges, uncached ="))
    assert (prepared / ".graphify_uncached.txt").read_text().splitlines() == (
        [] if expected_complete else [str(source)])
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert not (out / ".graphify_semantic.json").exists()
    assert not list(out.glob(".graphify_chunk*.json"))
    assert root == Path.cwd(), "the native prepared runner must restore the parent cwd"


@pytest.mark.parametrize("damage", ["error", "failed", "truncated", "bad-records", "bad-completion", "invalid-incomplete"])
def test_inline_failed_result_stops_before_cache_or_merge(inline_run, damage):
    _, source, out, baseline, prepared, run, core, _, _ = inline_run
    payload = _payload(source)
    if damage == "error":
        payload["error"] = "read failed"
    elif damage == "failed":
        payload["failed_chunks"] = 1
    elif damage == "truncated":
        payload["finish_reason"] = "length"
    elif damage == "bad-records":
        payload["nodes"] = [None]
    elif damage == "bad-completion":
        payload["completed_sources"] = "note.md"
    else:
        payload["incomplete_sources"] = ["", str(source)]
    raw = json.dumps(payload)
    run("from pathlib import Path; Path('.graphify_semantic_new.json').write_text(" + repr(raw) + ")")
    with pytest.raises(ValueError):
        run(_body(core, "Merged {len(chunks)} chunks:"))
    assert (prepared / ".graphify_semantic_new.json").read_text() == raw
    assert not (prepared / ".graphify_semantic.json").exists()
    assert not list(out.glob("cache/semantic/*.json"))
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


def test_missing_inline_result_is_not_successful_empty(inline_run):
    _, _, out, baseline, prepared, run, core, _, _ = inline_run
    with pytest.raises(ValueError):
        run(_body(core, "Merged {len(chunks)} chunks:"))
    assert not (prepared / ".graphify_semantic_new.json").exists()
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


def test_only_opencode_can_collect_its_registered_serial_fallback(inline_run):
    _, source, out, baseline, prepared, run, core, host, _ = inline_run
    payload = _payload(source)
    run("from pathlib import Path; Path('.graphify_chunk_01.json').write_text(" + repr(json.dumps(payload)) + "); "
        "Path('.graphify_completed_chunks.json').write_text(" + repr(json.dumps(
            [str(prepared / ".graphify_chunk_01.json")])) + ")")
    if host == "codex":
        with pytest.raises(ValueError, match="aggregate"):
            run(_body(core, "Merged {len(chunks)} chunks:"))
    else:
        run(_body(core, "Merged {len(chunks)} chunks:"))
        assert json.loads((prepared / ".graphify_semantic_new.json").read_text())["nodes"] == payload["nodes"]
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


def test_inline_shipped_cache_command_runs_from_parent_shell(inline_run):
    root, source, out, baseline, prepared, run, core, _, _ = inline_run
    run("from pathlib import Path; Path('.graphify_semantic_new.json').write_text(" + repr(json.dumps(_payload(source))) + ")")
    for marker in ("Merged {len(chunks)} chunks:", "saved = save_semantic_cache(",
                   "all_nodes = cached['nodes']"):
        block = re.sub(r"(?<![A-Z_])INPUT_PATH(?![A-Z_])", lambda _: str(root), _block(core, marker))
        result = subprocess.run(["/bin/bash", "-c", block], cwd=root, capture_output=True, text=True,
                                env={**os.environ, "VIRTUAL_ENV": sys.prefix}, check=False)
        assert result.returncode == 0, result.stderr
        if marker == "saved = save_semantic_cache(":
            assert "Cached 1 files" in result.stdout
    assert (prepared / ".graphify_semantic_new.json").exists()
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


def test_all_cached_inline_batch_merges_without_dispatch(inline_run):
    _, source, out, baseline, prepared, run, core, _, _ = inline_run
    run("from pathlib import Path; Path('.graphify_semantic_new.json').write_text(" + repr(json.dumps(_payload(source))) + ")")
    run(_body(core, "Merged {len(chunks)} chunks:"))
    run(_body(core, "saved = save_semantic_cache("))
    run(_body(core, "cached_nodes, cached_edges, cached_hyperedges, uncached ="))
    assert (prepared / ".graphify_uncached.txt").read_text() == ""
    assert not (prepared / ".graphify_semantic_new.json").exists()
    run(_body(core, "Merged {len(chunks)} chunks:"))
    run(_body(core, "all_nodes = cached['nodes']"))
    semantic = json.loads((prepared / ".graphify_semantic.json").read_text())
    assert semantic["completed_sources"] == [str(source)]
    assert semantic["nodes"] == _payload(source)["nodes"]
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


@pytest.mark.parametrize("inline_run", ["opencode"], indirect=True)
@pytest.mark.parametrize("damage", ["foreign", "relative", "duplicate", "missing", "symlink", "invalid-json", "failed"])
def test_serial_fallback_refuses_invalid_current_manifest_or_result(inline_run, damage):
    root, source, out, baseline, prepared, run, core, _, _ = inline_run
    candidate = prepared / ".graphify_chunk_01.json"
    payload = _payload(source)
    if damage == "failed":
        payload["error"] = "serial extraction failed"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    paths = [str(candidate)]
    if damage == "foreign":
        foreign = root / candidate.name
        foreign.write_bytes(candidate.read_bytes())
        paths = [str(foreign)]
    elif damage == "relative":
        paths = [candidate.name]
    elif damage == "duplicate":
        paths += paths
    elif damage == "missing":
        paths = [str(prepared / ".graphify_chunk_02.json")]
    elif damage == "symlink":
        foreign = root / "foreign.json"
        foreign.write_bytes(candidate.read_bytes())
        candidate.unlink()
        candidate.symlink_to(foreign)
    elif damage == "invalid-json":
        candidate.write_text("{", encoding="utf-8")
    run("from pathlib import Path; Path('.graphify_completed_chunks.json').write_text(" + repr(json.dumps(paths)) + ")")
    expected_error = OSError if damage in ("missing", "symlink") else ValueError
    with pytest.raises(expected_error) as error:
        run(_body(core, "Merged {len(chunks)} chunks:"))
    if damage == "symlink":
        assert isinstance(error.value, OSError)
        assert error.value.errno == errno.ELOOP
    assert not (prepared / ".graphify_semantic_new.json").exists()
    assert not list(out.glob("cache/semantic/*.json"))
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


def test_inline_completion_cannot_replace_an_undispatched_files_cache(inline_run):
    root, source, out, baseline, prepared, run, core, _, _ = inline_run
    foreign = root / "undispatched.md"
    foreign.write_text("Not dispatched in this batch.\n", encoding="utf-8")
    run("from graphify.cache import save_semantic_cache; save_semantic_cache(" + repr(
        [{"id": "accepted", "source_file": str(foreign)}]) + ", [], root=" + repr(str(root)) + ")")
    old_cache = {path.name: path.read_bytes() for path in out.glob("cache/semantic/*.json")}
    payload = _payload(source)
    payload["nodes"].append({"id": "mention", "source_file": str(foreign)})
    payload["completed_sources"].append(str(foreign))
    run("from pathlib import Path; Path('.graphify_semantic_new.json').write_text(" + repr(json.dumps(payload)) + ")")
    run(_body(core, "Merged {len(chunks)} chunks:"))
    assert json.loads((prepared / ".graphify_semantic_new.json").read_text())["completed_sources"] == [str(source)]
    with pytest.warns(RuntimeWarning, match="out-of-scope"):
        run(_body(core, "saved = save_semantic_cache("))
    assert all((out / "cache/semantic" / name).read_bytes() == raw for name, raw in old_cache.items())
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


def test_inline_guidance_uses_aggregate_success_without_disk_callbacks():
    for host in HOSTS:
        core = gen.render(gen.load_platforms()[host])[0].content
        prose = core.split("**Step B3", 1)[1].split("```bash", 1)[0]
        assert "inline aggregate" in prose
        assert "graphify-out/.graphify_chunk_NN.json" not in prose
        assert "general-purpose" not in prose


def test_inline_repair_leaves_other_hosts_declarations_and_monoliths_unchanged():
    for host, platform in gen.load_platforms().items():
        if host in HOSTS:
            continue
        for artifact in gen.render(platform):
            baseline = subprocess.check_output(["git", "show", "HEAD:" + artifact.path],
                                               cwd=gen.REPO_ROOT, text=True)
            assert artifact.content == baseline, artifact.path
