"""Inline hosts must not publish disk-only B0 dispatch metadata."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify import transaction as tx
from tests.skill_runbook_helpers import prepared_path, python_bodies, run_body
from tools.skillgen import gen

pytestmark = pytest.mark.skipif(os.name == "nt", reason="native POSIX transaction backend")


def _body(core: str, marker: str) -> str:
    selected = [body for body in python_bodies(core) if marker in body]
    assert len(selected) == 1
    return selected[0]


def _fixture(tmp_path: Path, host: str):
    root = tmp_path / "inline 'quoted' corpus"
    root.mkdir()
    source = root / "note.md"
    source.write_text("A complete source.\n")
    out = root / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text(json.dumps({"nodes": [{"id": "baseline"}], "links": []}))
    (out / "manifest.json").write_text('{"baseline": true}')
    baseline = {p.name: p.read_bytes() for p in out.iterdir()}
    owner = tx.begin_transaction(
        "update", root, output=out,
        expected_snapshot=tx.open_graph_snapshot(graph, purpose="inline-batch-test"),
    )
    token = tx.stage_transaction_handoff(owner)
    prepared = prepared_path(token.path)
    core = gen.render(gen.load_platforms()[host])[0].content
    run = lambda body: run_body(body, token=token.path, root=root)
    detect = json.dumps({"files": {"document": [str(source)]}})
    run("from pathlib import Path; Path('.graphify_detect.json').write_text(" + repr(detect) + ")")
    return source, out, baseline, owner, prepared, run, core


@pytest.mark.parametrize("host", ["codex", "opencode"])
@pytest.mark.parametrize("kind", ["nonempty", "empty", "partial", "ownership-only"])
def test_inline_b0_collection_cache_and_publication_have_no_disk_batch_marker(tmp_path, host, kind):
    source, out, baseline, _, prepared, run, core = _fixture(tmp_path, host)
    b0 = _body(core, "cached_nodes, cached_edges, cached_hyperedges, uncached =")
    run(b0)
    assert (prepared / ".graphify_uncached.txt").read_text().splitlines() == [str(source)]
    payload: dict = {
        "nodes": [{"id": "fact", "label": "A fact", "source_file": str(source)}],
        "edges": [], "hyperedges": [], "completed_sources": [str(source)],
    }
    if kind == "empty":
        payload["nodes"] = []
    elif kind == "partial":
        payload["nodes"][0]["_partial"] = True
        # B2 requires the parent to remove partial sources from completion.
        payload["completed_sources"] = []
    elif kind == "ownership-only":
        payload.pop("completed_sources")
    # B2 specifies parent in-memory collection and exact-token aggregate staging.
    # Generated inline B3 cache routing is still #237. Exercise native cache
    # admission directly here, without treating that separate path as repaired.
    run("import json; from pathlib import Path; result = json.loads(" + repr(json.dumps(payload))
        + "); Path('.graphify_semantic_new.json').write_text(json.dumps(result))")
    assert not list(out.glob("cache/semantic/*.json")), "collection is not cache admission"
    run("import json; from pathlib import Path; from graphify.cache import save_semantic_cache\n"
        "new = json.loads(Path('.graphify_semantic_new.json').read_text())\n"
        "uncached = Path('.graphify_uncached.txt').read_text().splitlines()\n"
        "save_semantic_cache(new['nodes'], new['edges'], new['hyperedges'], root='INPUT_PATH', "
        "allowed_source_files=uncached, completed_source_files=new.get('completed_sources', []))")
    run(_body(core, "print(f'Extraction complete -"))
    semantic = json.loads((prepared / ".graphify_semantic.json").read_text())
    assert semantic["nodes"] == payload["nodes"]
    assert semantic["completed_sources"] == payload.get("completed_sources", [])
    run(b0)
    cacheable = kind in {"nonempty", "empty"}
    assert (prepared / ".graphify_uncached.txt").read_text().splitlines() == (
        [] if cacheable else [str(source)])
    cached = prepared / ".graphify_cached.json"
    assert cached.exists() is cacheable
    if cacheable:
        assert json.loads(cached.read_text())["completed_sources"] == [str(source)]
    run("from pathlib import Path; Path('intended-export.txt').write_text('retain this export')")
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    run("from graphify.transaction import finalize_prepared_transaction; finalize_prepared_transaction()")
    assert not (out / ".graphify_dispatch_batch.txt").exists()
    assert (out / "intended-export.txt").read_text() == "retain this export"
    assert json.loads((out / ".graphify_semantic.json").read_text()) == semantic
    graph = json.loads((out / "graph.json").read_text())
    assert graph["nodes"] == [{"id": "baseline"}]
    assert graph["links"] == []
    assert graph["graph"]["_graphify_protocol"]["state"] == "active"
    assert (out / "manifest.json").read_bytes() == baseline["manifest.json"]


@pytest.mark.parametrize("host", ["codex", "opencode"])
def test_inline_error_and_wrong_owner_cannot_stage_or_publish_success(tmp_path, host):
    _, out, baseline, owner, prepared, run, core = _fixture(tmp_path, host)
    run(_body(core, "cached_nodes, cached_edges, cached_hyperedges, uncached ="))
    with pytest.raises(tx.PendingTransactionError):
        tx.commit_prepared_bytes(owner, ".graphify_semantic_new.json", b"{}")
    with pytest.raises(json.JSONDecodeError):
        run("import json; from pathlib import Path; result = json.loads('invalid response'); "
            "Path('.graphify_semantic_new.json').write_text(json.dumps(result))")
    assert not (prepared / ".graphify_semantic_new.json").exists()
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
    assert not list(out.glob("cache/semantic/*.json"))
    tx.abort_transaction(owner)
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert not (out / ".graphify_dispatch_batch.txt").exists()
