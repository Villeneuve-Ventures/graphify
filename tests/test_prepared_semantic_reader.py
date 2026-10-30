"""Prepared semantic merges use exact native ownership, then public certification."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import transaction as tx
from graphify.build import build_merge

pytestmark = pytest.mark.skipif(os.name == "nt", reason="native POSIX transaction backend")


def _git(root, *arguments):
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Graphify Fixture",
         "-c", "user.email=fixture@example.invalid", *arguments],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _chunk(version):
    return {
        "nodes": [
            {"id": "changed", "label": f"Mutable policy {version}",
             "source_file": "changed.md", "type": "concept"},
            {"id": "retained", "label": "Unrelated stable rule",
             "source_file": "retained.md", "type": "concept"},
            {"id": "anchor", "label": "Permanent reference",
             "source_file": "retained.md", "type": "concept"},
        ],
        "edges": [{"source": "retained", "target": "anchor",
                   "relation": "requires", "confidence": "EXTRACTED",
                   "source_file": "retained.md"}],
        "hyperedges": [{"id": "retained-group", "nodes": ["retained", "anchor"],
                        "label": "Permanent rule group", "source_file": "retained.md"}],
    }


def _published_bytes(output):
    return {name: (output / name).read_bytes() if (output / name).exists() else None
            for name in ("graph.json", "manifest.json", tx.RECEIPT_FILE)}


def _baseline(tmp_path, monkeypatch, *, separate=False, accepted_chunk=None, directed=False):
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.chdir(root)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (root / ".gitignore").write_text("graphify-out/\n.graphify-*\n")
    (root / "changed.md").write_text("Mutable policy one.\n")
    (root / "retained.md").write_text(
        "Unrelated stable rule requires permanent reference.\n" + (
            "Raw CoinGecko fixture price array.\nRaw CoinGecko fixture volume array.\n"
            if accepted_chunk is not None else ""
        )
    )
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture baseline")
    first_head = _git(root, "rev-parse", "HEAD")
    assert _git(root, "status", "--porcelain") == ""
    output = tmp_path / "separate-output" if separate else root / "graphify-out"
    owner = tx.begin_transaction("full", root, output=output)
    token = tx.stage_transaction_handoff(owner)
    tx.run_prepared_token(token.path, ["-c", f"""
from pathlib import Path
from graphify.build import build, build_from_json
from graphify.detect import save_manifest
from graphify.export import to_json
from graphify.transaction import finalize_prepared_transaction
root = Path({str(root)!r})
graph = (build_from_json({accepted_chunk!r}, root=root, directed={directed!r}) if {accepted_chunk is not None!r}
         else build([{_chunk('one')!r}], root=root, directed={directed!r}))
assert to_json(graph, {{}}, 'graph.json', built_at_commit={first_head!r})
save_manifest({{'document': [str(root / 'changed.md'), str(root / 'retained.md')]}},
              manifest_path='manifest.json', root=root)
finalize_prepared_transaction()
"""])
    baseline = tx.open_graph_snapshot(output / "graph.json", purpose="semantic-regression")
    assert baseline.generation is not None
    assert (output / tx.RECEIPT_FILE).is_file()
    return SimpleNamespace(root=root, output=output, baseline=baseline, first_head=first_head)


def _prepare(fixture):
    (fixture.root / "changed.md").write_text("Mutable policy two, with revised requirements.\n")
    assert _git(fixture.root, "diff", "--name-only") == "changed.md"
    _git(fixture.root, "add", "changed.md")
    _git(fixture.root, "commit", "-qm", "fixture semantic update")
    fixture.second_head = _git(fixture.root, "rev-parse", "HEAD")
    assert fixture.second_head != fixture.first_head
    assert _git(fixture.root, "rev-parse", "HEAD^") == fixture.first_head
    fixture.owner = tx.begin_transaction(
        "update", fixture.root, output=fixture.output, expected_snapshot=fixture.baseline,
    )
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    return fixture


def _run(fixture, code):
    tx.run_prepared_token(fixture.token.path, ["-c", f"""
import json, os
from pathlib import Path
from graphify import transaction as tx
from graphify.build import build_merge
root = Path({str(fixture.root)!r})
output = Path({str(fixture.output)!r})
{code}
"""])


@pytest.mark.parametrize("separate", [False, True], ids=["in-corpus", "separate-output"])
@pytest.mark.parametrize("alias", [False, True], ids=["private-path", "public-alias"])
def test_prepared_semantic_merge_publishes_certified_generation(tmp_path, monkeypatch, separate, alias):
    fixture = _prepare(_baseline(tmp_path, monkeypatch, separate=separate))
    before = _published_bytes(fixture.output)
    changed_chunk = {"nodes": [_chunk("two")["nodes"][0]], "edges": []}
    try:
        _run(fixture, f"""
from graphify.detect import detect_incremental, save_manifest
from graphify.export import to_json
assert tx.current_transaction().id == {fixture.owner.id!r}
assert Path.cwd() != output
assert tx.GRAPH_WATERMARK_KEY in json.loads(Path('graph.json').read_bytes())['graph']
assert not Path(tx.RECEIPT_FILE).exists()
incremental = detect_incremental(root, manifest_path='manifest.json', kind='semantic', google_workspace=False)
assert incremental['new_files']['document'] == [str(root / 'changed.md')]
print('Native prepared owner; seeded watermark; only changed.md needs semantic extraction')
path = (output if {alias!r} else Path.cwd()) / 'graph.json'
graph = build_merge([{changed_chunk!r}], graph_path=path, root=root)
assert to_json(graph, {{}}, 'graph.json', built_at_commit={fixture.second_head!r})
save_manifest(incremental['files'], manifest_path='manifest.json', root=root)
tx.finalize_prepared_transaction()
""")
    except Exception:
        assert _published_bytes(fixture.output) == before
        raise
    published = tx.open_graph_snapshot(fixture.output / "graph.json", purpose="semantic-regression")
    old_nodes = {node["id"]: node for node in fixture.baseline.data["nodes"]}
    nodes = {node["id"]: node for node in published.data["nodes"]}
    assert nodes["changed"]["label"] == "Mutable policy two"
    assert nodes["retained"] == old_nodes["retained"]
    assert nodes["anchor"] == old_nodes["anchor"]
    assert published.data["links"] == fixture.baseline.data["links"]
    assert published.data["hyperedges"] == fixture.baseline.data["hyperedges"]
    assert published.data["built_at_commit"] == fixture.second_head
    assert published.data["graph"][tx.GRAPH_WATERMARK_KEY]["generation"] == fixture.owner.generation
    assert published.generation != fixture.baseline.generation


@pytest.mark.parametrize("conflict", [False, True])
def test_native_semantic_update_preserves_distinct_facts_or_refuses_conflict(tmp_path, monkeypatch, conflict):
    chunk = _chunk("one")
    for identity, label, location in [("price", "price", "L2"), ("volume", "volume", "L3")]:
        chunk["nodes"].append({"id": identity, "label": f"Raw CoinGecko fixture {label} array",
                               "source_file": "retained.md", "file_type": "document"})
        chunk["edges"].append({"source": "anchor", "target": identity, "relation": "references",
                               "confidence": "EXTRACTED", "source_file": "retained.md",
                               "source_location": location})
    fixture = _prepare(_baseline(tmp_path, monkeypatch, accepted_chunk=chunk))
    before = _published_bytes(fixture.output)
    fresh = {"nodes": [_chunk("two")["nodes"][0]], "edges": []}
    if conflict:
        fresh["edges"] = [{"source": "anchor", "target": "price", "relation": "references",
                            "source_file": "changed.md", "confidence": "INFERRED"}]
    code = f"""
from graphify.detect import detect_incremental, save_manifest
from graphify.export import to_json
incremental = detect_incremental(root, manifest_path='manifest.json', kind='semantic', google_workspace=False)
graph = build_merge([{fresh!r}], Path.cwd() / 'graph.json', root=root)
assert 'price' in graph and 'volume' in graph
assert to_json(graph, {{}}, 'graph.json', built_at_commit={fixture.second_head!r})
save_manifest(incremental['files'], manifest_path='manifest.json', root=root)
tx.finalize_prepared_transaction()
"""
    if conflict:
        with pytest.raises(Exception) as refused:
            _run(fixture, code)
        assert "retained edge" in str(refused.value)
        assert _published_bytes(fixture.output) == before
        # Refusal leaves native pending state; it does not claim cancellation.
        with pytest.raises(tx.PendingTransactionError):
            tx.open_graph_snapshot(fixture.output / "graph.json", purpose="refused-semantic")
    else:
        _run(fixture, code)
        after = tx.open_graph_snapshot(fixture.output / "graph.json", purpose="preserved-semantic")
        nodes = {node["id"]: node for node in after.data["nodes"]}
        for record in fixture.baseline.data["nodes"]:
            if record["id"] != "changed":
                assert nodes[record["id"]] == record
        assert after.data["links"] == fixture.baseline.data["links"]
        assert after.generation != fixture.baseline.generation


@pytest.mark.parametrize("authority", ["flag-only", "partial", "wrong-owner"])
@pytest.mark.parametrize("missing", [False, True], ids=["graph-present", "graph-missing"])
def test_prepared_intent_never_grants_authority(tmp_path, monkeypatch, authority, missing):
    fixture = _baseline(tmp_path, monkeypatch)
    tx._AUTHORITY.set(None)
    monkeypatch.setenv("GRAPHIFY_PREPARED_OUTPUT", "1")
    if authority == "partial":
        monkeypatch.setenv("GRAPHIFY_TRANSACTION_ID", "0" * 64)
    elif authority == "wrong-owner":
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        tx.begin_transaction("full", foreign, output=foreign / "graphify-out")
        monkeypatch.setenv("GRAPHIFY_TRANSACTION_OUTPUT", str(fixture.output))
    before = _published_bytes(fixture.output)
    path = fixture.output / ("missing.json" if missing else "graph.json")
    with pytest.raises(tx.PendingTransactionError, match="owner") as refused:
        build_merge([], path)
    assert "delete" not in str(refused.value).lower()
    assert _published_bytes(fixture.output) == before
    assert not (fixture.output / "missing.json").exists()


@pytest.mark.parametrize("target", ["foreign-output", "foreign-private", "missing", "malformed"])
def test_prepared_owner_rejects_invalid_graph(tmp_path, monkeypatch, target):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    code = "path = Path.cwd() / 'graph.json'\n"
    if target.startswith("foreign"):
        foreign = tmp_path / target / "graphify-out"
        foreign.mkdir(parents=True)
        (foreign / "graph.json").write_bytes(fixture.baseline.payload)
        code += f"path = Path({str(foreign / 'graph.json')!r})\n"
    elif target == "missing":
        code += "tx.unlink_prepared(tx.current_transaction(), 'graph.json')\n"
    else:
        code += "tx.commit_prepared_bytes(tx.current_transaction(), 'graph.json', b'{bad')\n"
    with pytest.raises(RuntimeError, match="outside the owned workspace|missing|malformed"):
        _run(fixture, code + "build_merge([], path, root=root)")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize("change", ["remove", "same-id-token-change", "cancel"])
def test_authority_changed_after_real_admission_refuses(tmp_path, monkeypatch, change):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    original = tx.open_prepared_graph
    admitted = []

    def read_then_change(owner, path):
        snapshot = original(owner, path)
        admitted.append(snapshot.digest)
        if change == "remove":
            tx._AUTHORITY.set(None)
        elif change == "same-id-token-change":
            tx.stage_transaction_handoff(owner)
            replacement = tx.current_transaction()
            assert replacement.id == owner.id and replacement != owner
        else:
            tx.cancel_unpublished_transaction(owner)
        return snapshot

    monkeypatch.setattr(tx, "open_prepared_graph", read_then_change)
    before = _published_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match="owner|authority|transaction") as refused:
        _run(fixture, "build_merge([], Path.cwd() / 'graph.json', root=root)")
    assert "delete" not in str(refused.value).lower()
    assert len(admitted) == 1
    assert _published_bytes(fixture.output) == before


def test_superseded_token_cannot_enter_prepared_merge(tmp_path, monkeypatch):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    tx.takeover_drainer(fixture.output, now=10**12)
    before = _published_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError):
        _run(fixture, "raise AssertionError('superseded token ran')")
    assert _published_bytes(fixture.output) == before


def test_prepared_alias_enforces_private_graph_size_before_read(tmp_path, monkeypatch):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    limit = len(fixture.baseline.payload) + 256
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", str(limit))
    with pytest.raises(RuntimeError, match="unsafe managed artifact"):
        _run(fixture, f"""
payload = Path('graph.json').read_bytes() + b' ' * {limit}
tx.commit_prepared_bytes(tx.current_transaction(), 'graph.json', payload)
assert (output / 'graph.json').stat().st_size < {limit}
assert Path('graph.json').stat().st_size > {limit}
build_merge([], output / 'graph.json', root=root)
""")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize("damage", ["graph", "manifest", "receipt", "missing-receipt", "generation"])
def test_public_merge_keeps_certification_requirements(tmp_path, monkeypatch, damage):
    fixture = _baseline(tmp_path, monkeypatch)
    if damage == "missing-receipt":
        (fixture.output / tx.RECEIPT_FILE).unlink()
    elif damage == "generation":
        path = fixture.output / "graph.json"
        data = json.loads(path.read_bytes())
        data["graph"][tx.GRAPH_WATERMARK_KEY]["generation"] += 1
        path.write_text(json.dumps(data))
    else:
        path = fixture.output / {"graph": "graph.json", "manifest": "manifest.json",
                                 "receipt": tx.RECEIPT_FILE}[damage]
        path.write_bytes(path.read_bytes() + b" ")
    before = _published_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError):
        tx.open_graph_snapshot(fixture.output / "graph.json", purpose="tamper-regression")
    with pytest.raises(RuntimeError) as refused:
        build_merge([], fixture.output / "graph.json", root=fixture.root)
    assert type(refused.value) is RuntimeError
    assert "Delete the file and run a full rebuild." in str(refused.value)
    assert _published_bytes(fixture.output) == before


def test_ordinary_published_merge_retains_facts(tmp_path, monkeypatch):
    fixture = _baseline(tmp_path, monkeypatch)
    before = _published_bytes(fixture.output)
    graph = build_merge([], fixture.output / "graph.json", root=fixture.root)
    assert set(graph) == {"changed", "retained", "anchor"}
    assert graph.has_edge("retained", "anchor")
    assert _published_bytes(fixture.output) == before


def test_prepared_reader_retains_hard_limit_when_configured_cap_is_larger(tmp_path, monkeypatch):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    original = tx._read_relative_bytes
    observed = []

    def read_with_limit(capability, name, limit=512 * 1024 * 1024):
        if name == "graph.json" and capability.path != fixture.output:
            observed.append(limit)
        return original(capability, name, limit)

    # Seed first so the observation covers admission, not workspace preparation.
    _run(fixture, "assert Path('graph.json').is_file()")
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", str(1024 * 1024 * 1024))
    monkeypatch.setattr(tx, "_read_relative_bytes", read_with_limit)
    before = _published_bytes(fixture.output)
    _run(fixture, "assert build_merge([], output / 'graph.json', root=root).number_of_nodes() == 3")
    assert observed == [512 * 1024 * 1024]
    assert _published_bytes(fixture.output) == before


def test_native_prepared_ast_refresh_preserves_semantic_baseline(tmp_path, monkeypatch):
    fixture = _baseline(tmp_path, monkeypatch)
    app = fixture.root / "app.py"
    app.write_text("def fresh_ast_beacon():\n    return 42\n")
    _git(fixture.root, "add", "app.py")
    _git(fixture.root, "commit", "-qm", "fixture code update")
    fixture.owner = tx.begin_transaction(
        "update", fixture.root, output=fixture.output, expected_snapshot=fixture.baseline,
    )
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    _run(fixture, """
from graphify.detect import save_manifest
from graphify.export import to_json
from graphify.extract import extract
app = root / 'app.py'
chunk = extract([app], cache_root=Path.cwd().parent, parallel=False, strict=True)
graph = build_merge([chunk], Path.cwd() / 'graph.json', root=root,
                    ast_refresh_sources=[str(app)])
assert to_json(graph, {}, 'graph.json')
save_manifest({'code': [str(app)]}, manifest_path='manifest.json', kind='ast', root=root)
tx.finalize_prepared_transaction()
""")
    published = tx.open_graph_snapshot(fixture.output / "graph.json", purpose="prepared-ast")
    nodes = {node["id"]: node for node in published.data["nodes"]}
    assert any(node.get("label") == "fresh_ast_beacon()" for node in nodes.values())
    for node in fixture.baseline.data["nodes"]:
        assert nodes[node["id"]] == node
    assert all(edge in published.data["links"] for edge in fixture.baseline.data["links"])
    assert published.data["hyperedges"] == fixture.baseline.data["hyperedges"]


def test_malformed_member_conflict_preserves_all_published_bytes(tmp_path, monkeypatch):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = {
        path.relative_to(fixture.output): path.read_bytes()
        for path in fixture.output.rglob("*")
        if path.is_file()
    }
    assert {Path("graph.json"), Path("manifest.json"), Path(tx.RECEIPT_FILE)} <= set(before)
    group = {**_chunk("one")["hyperedges"][0], "nodes": ["retained", []]}

    with pytest.raises(ValueError, match="retained hyperedge identity"):
        _run(
            fixture,
            f"build_merge([{{'hyperedges': [{group!r}]}}], Path.cwd() / 'graph.json', root=root)",
        )

    after = {name: (fixture.output / name).read_bytes() for name in before}
    assert after == before
    with pytest.raises(tx.PendingTransactionError):
        tx.open_graph_snapshot(fixture.output / "graph.json", purpose="refused-malformed-member")


@pytest.mark.parametrize("kind", ["edge", "hyperedge", "empty", "conflict"])
def test_explicit_relationship_replacement_under_native_prepared_owner(tmp_path, monkeypatch, kind):
    chunk = _chunk("one")
    chunk["hyperedges"].append({"id": "old", "source_file": "changed.md", "nodes": ["retained"]})
    fixture = _prepare(_baseline(tmp_path, monkeypatch, accepted_chunk=chunk))
    before = _published_bytes(fixture.output)
    fresh = {"nodes": [], "edges": [], "hyperedges": []}
    if kind == "edge":
        fresh["edges"] = [{"source": "changed", "target": "anchor", "source_file": "changed.md",
                            "relation": "references", "confidence": "EXTRACTED"}]
        # The replaced node itself is gone; fresh unknown references retain
        # established discard behavior. Foreign-owned facts remain unchanged.
    elif kind == "hyperedge":
        fresh["hyperedges"] = [{"id": "new", "source_file": "changed.md", "nodes": ["retained", "anchor"]}]
    elif kind == "conflict":
        fresh["hyperedges"] = [{"id": "retained-group", "source_file": "changed.md", "nodes": ["anchor"]}]
    code = f"""
from graphify.export import to_json
from graphify.detect import save_manifest
graph = build_merge([{fresh!r}], output / 'graph.json', root=root,
                    replacement_sources=[str(root / 'changed.md')])
assert 'changed' not in graph
assert to_json(graph, {{}}, 'graph.json', force=True, built_at_commit={fixture.second_head!r})
save_manifest({{'document': [str(root / 'changed.md'), str(root / 'retained.md')]}},
              manifest_path='manifest.json', root=root)
tx.finalize_prepared_transaction()
"""
    if kind == "conflict":
        with pytest.raises(Exception, match="retained hyperedge"):
            _run(fixture, code)
        assert _published_bytes(fixture.output) == before
        return
    _run(fixture, code)
    published = tx.open_graph_snapshot(fixture.output / "graph.json", purpose="explicit-replacement")
    assert published.data['links'] == fixture.baseline.data['links']
    assert {group['id'] for group in published.data['hyperedges']} == (
        {'retained-group', 'new'} if kind == 'hyperedge' else {'retained-group'})
    old = {node['id']: node for node in fixture.baseline.data['nodes']}
    assert {node['id']: node for node in published.data['nodes']} == {key: old[key] for key in ('retained', 'anchor')}


def _rendered_semantic_stage(platform_key, needle):
    import re
    import shlex
    from tools.skillgen import gen
    body = gen.render(gen.load_platforms()[platform_key])[0].content
    block = next(block for block in re.findall(r'```(?:bash|sh)\n(.*?)\n```', body, re.S)
                 if needle in block)
    assert 'run-prepared-token' in next(line for line in block.splitlines() if line.endswith('-c "'))
    raw = block.rsplit('-c "\n', 1)[1].rsplit('\n"', 1)[0]
    return shlex.split('python -c "' + raw + '"')[-1]


@pytest.mark.parametrize('platform_key', ['claude', 'devin', 'aider', 'opencode', 'codex'])
def test_rendered_cache_check_resets_completion_manifest_in_prepared_output(tmp_path, monkeypatch, platform_key):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    code = _rendered_semantic_stage(platform_key, 'cached_nodes, cached_edges, cached_hyperedges, uncached = check_semantic_cache(')
    code = code.replace('INPUT_PATH', str(fixture.root))
    merge_code = _rendered_semantic_stage(platform_key, 'chunks = json.loads(')
    merge_code = merge_code.replace('INPUT_PATH', str(fixture.root))
    _run(fixture, f"""
Path('.graphify_detect.json').write_text(json.dumps({{'files': {{'document': [str(root / 'changed.md')]}}}}))
Path('.graphify_completed_chunks.json').write_text('["stale-chunk.json"]')
Path('.graphify_semantic_new.json').write_text('{{"completed_sources": ["stale.md"]}}')
exec({code!r})
assert json.loads(Path('.graphify_completed_chunks.json').read_text()) == []
assert not Path('.graphify_semantic_new.json').exists()
assert Path.cwd() != output
chunk = Path.cwd() / '.graphify_chunk_01.json'
chunk.write_text(json.dumps({{'nodes': [{{'id': 'fresh', 'source_file': 'changed.md'}}],
    'edges': [], 'hyperedges': [], 'completed_sources': [str(root / 'changed.md')]}}))
if {platform_key == 'codex'!r}:
    # Codex collects inline; only OpenCode supports the serial file fallback.
    Path('.graphify_semantic_new.json').write_bytes(chunk.read_bytes())
else:
    Path('.graphify_completed_chunks.json').write_text(json.dumps([str(chunk)]))
exec({merge_code!r})
merged = json.loads(Path('.graphify_semantic_new.json').read_text())
assert [node['id'] for node in merged['nodes']] == ['fresh']
assert merged['completed_sources'] == [str(root / 'changed.md')]
""")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform_key', ['aider', 'opencode'])
def test_rendered_chunk_merge_preserves_current_direct_aggregate(tmp_path, monkeypatch, platform_key):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    code = _rendered_semantic_stage(platform_key, 'chunks = json.loads(')
    code = code.replace('INPUT_PATH', str(fixture.root))
    direct = {'nodes': [{'id': 'fact', 'source_file': 'changed.md'}], 'edges': [], 'hyperedges': [],
              'completed_sources': [str(fixture.root / 'changed.md')], 'input_tokens': 1, 'output_tokens': 2}
    _run(fixture, f"""
Path('.graphify_completed_chunks.json').write_text('[]')
Path('.graphify_uncached.txt').write_text(str(root / 'changed.md'))
Path('.graphify_semantic_new.json').write_text(json.dumps({direct!r}))
exec({code!r})
assert json.loads(Path('.graphify_semantic_new.json').read_text()) == {direct!r}
""")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('file_type', ['concept', 'rationale'])
@pytest.mark.parametrize('completed', [False, True])
def test_devin_part_c_keeps_completion_through_final_sanitize(tmp_path, monkeypatch, file_type, completed):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    code = _rendered_semantic_stage('devin', "merged_edges = ast['edges'] + sem['edges']")
    semantic = {'nodes': [{'id': 'fresh', 'label': 'Named entity', 'file_type': file_type,
                          'source_file': 'changed.md'}], 'edges': [],
                'hyperedges': [{'id': 'group', 'nodes': ['fresh', 'retained'], 'source_file': 'changed.md'}],
                'completed_sources': ['changed.md'] if completed else []}
    _run(fixture, f"""
Path('.graphify_ast.json').write_text(json.dumps({{'nodes': [], 'edges': []}}))
Path('.graphify_semantic.json').write_text(json.dumps({semantic!r}))
exec({code!r})
merged = json.loads(Path('.graphify_extract.json').read_text())
assert [node['id'] for node in merged['nodes']] == {['fresh'] if completed else []!r}
assert [group['id'] for group in merged['hyperedges']] == {['group'] if completed else []!r}
assert merged['replacement_sources'] == {['changed.md'] if completed else []!r}
assert 'completed_sources' not in merged
""")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform_key', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('directed', [False, True])
def test_rendered_later_graph_consumers_preserve_accepted_facts(
        tmp_path, monkeypatch, platform_key, accepted, directed):
    import ast
    import re
    import shlex
    from tools.skillgen import gen
    output = tmp_path / 'graphify-out'
    output.mkdir()
    extraction = {'accepted_merge': accepted,
                  'nodes': [{'id': 'policy_rule', 'label': 'Rule', 'source_file': 'docs/policy.md',
                             'file_type': 'document', 'retained_attribute': 'exact'},
                            {'id': 'anchor', 'label': 'Anchor', 'source_file': 'stable.md', 'file_type': 'document'}],
                  'edges': [{'source': 'policy_rule', 'target': 'anchor', 'relation': 'references',
                             'confidence': 'EXTRACTED', 'source_file': 'docs/policy.md'}],
                  'hyperedges': [{'id': 'group', 'members': ['policy_rule', 'anchor', 'policy_rule']}]}
    graph_path = output / 'graph.json'
    graph_path.write_text(json.dumps({'nodes': extraction['nodes'], 'links': extraction['edges']}))
    before = graph_path.read_bytes()
    for directory in (tmp_path, output):
        (directory / '.graphify_extract.json').write_text(json.dumps(extraction))
        (directory / '.graphify_detect.json').write_text('{}')
        (directory / '.graphify_analysis.json').write_text('{}')
    count = 0
    for artifact in gen.render(gen.load_platforms()[platform_key]):
        for block in re.findall(r'```(?:bash|sh)\n(.*?)\n```', artifact.content, re.S):
            if 'build_from_json(' not in block:
                continue
            raw = block.rsplit('-c "\n', 1)[1].rsplit('\n"', 1)[0]
            code = shlex.split('python -c "' + raw + '"')[-1]
            tree = ast.parse(code.replace('IS_DIRECTED', str(directed)).replace('INPUT_PATH', str(tmp_path)))
            index = next(i for i, statement in enumerate(tree.body) if isinstance(statement, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id in {'G', 'G_new'}
                                 for target in statement.targets))
            target = tree.body[index].targets[0].id
            prefix = ast.Module(body=tree.body[:index + 1], type_ignores=[])
            runner = next(line for line in block.splitlines() if line.endswith('-c "'))
            # Prepared bodies start in output; direct consumers start in the corpus.
            monkeypatch.chdir(output if 'run-prepared-token' in runner else tmp_path)
            namespace = {}
            exec(compile(prefix, artifact.path, 'exec'), namespace)
            graph = namespace[target]
            expected_id = 'policy_rule' if accepted else 'docs_policy_rule'
            assert set(graph) == {expected_id, 'anchor'}, (artifact.path, target, list(graph))
            assert graph.has_edge(expected_id, 'anchor')
            assert graph.is_directed() == directed
            if accepted:
                assert graph.nodes['policy_rule'] == {key: value for key, value in extraction['nodes'][0].items() if key != 'id'}
                assert graph.graph['hyperedges'] == extraction['hyperedges']
            assert graph_path.read_bytes() == before
            count += 1
    assert count >= (3 if platform_key == 'claude' else 8)


def _deletion_stage(platform_key, needle, *, reference=False):
    import re
    import shlex
    from tools.skillgen import gen
    artifacts = gen.render(gen.load_platforms()[platform_key])
    artifact = next(a for a in artifacts if a.path.endswith('/update.md')) if reference else artifacts[0]
    block = next(b for b in re.findall(r'```(?:bash|sh)\n(.*?)\n```', artifact.content, re.S)
                 if needle in b)
    raw = block.rsplit('-c "\n', 1)[1].split('\n"', 1)[0]
    code = shlex.split('python -c "' + raw + '"')[-1]
    return block, code


def _deletion_fixture(tmp_path, monkeypatch, *, platform_key, empty=False, directed=False):
    chunk = _chunk('one')
    chunk['hyperedges'].append({'id': 'deleted-group', 'nodes': ['changed', 'retained'],
                               'source_file': 'changed.md'})
    fixture = _baseline(tmp_path, monkeypatch, accepted_chunk=chunk, directed=directed)
    fixture.deleted = ['changed.md', 'retained.md'] if empty else ['changed.md']
    for name in fixture.deleted:
        (fixture.root / name).unlink()
    _git(fixture.root, 'add', '-u')
    _git(fixture.root, 'commit', '-qm', 'delete sources')
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    detect = _deletion_code(platform_key, 'result = detect_incremental(', fixture,
                            reference=platform_key == 'claude')
    _run(fixture, f'exec({detect!r})')
    if platform_key == 'claude':
        population = _deletion_code(platform_key, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, """
assert json.loads(Path('.graphify_incremental.json').read_text())['deleted_files']
assert json.loads(Path('.graphify_detect.json').read_text())['total_files'] == 0
""")
    return fixture


def _deletion_code(platform_key, needle, fixture, *, directed=False, reference=False):
    block, code = _deletion_stage(platform_key, needle, reference=reference)
    assert 'run-prepared-token' in block
    return code.replace('INPUT_PATH', str(fixture.root)).replace('IS_DIRECTED', str(directed))


@pytest.mark.parametrize('platform_key', ['claude', 'aider', 'devin'])
def test_deletion_only_reset_overwrites_stale_aggregate(tmp_path, monkeypatch, platform_key):
    from tools.skillgen import gen
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform_key)
    before = _published_bytes(fixture.output)
    stale = dict(_chunk('old'), replacement_sources=['changed.md'])
    _run(fixture, f"""
Path('.graphify_extract.json').write_text(json.dumps({stale!r}))
Path('.graphify_chunk_old.json').write_text(json.dumps({stale!r}))
Path('.graphify_completed_chunks.json').write_text('[".graphify_chunk_old.json"]')
""")
    block, _ = _deletion_stage(platform_key, 'Only deletions', reference=platform_key == 'claude')
    # Bootstrap has its own tests. Keep the actual prepared-token CLI and shell
    # control flow, including the existence guard that originally skipped reset.
    block = block.replace(gen._POSIX_OPERATION_GUARD, '')
    block = block.replace('INPUT_PATH', str(fixture.root))
    result = subprocess.run(['bash', '-e', '-c', block], cwd=fixture.root, text=True,
                            capture_output=True, env={**os.environ,
                                'GRAPHIFY_PYTHON': str(Path(os.sys.executable).absolute())})
    assert result.returncode == 0, result.stdout + result.stderr
    _run(fixture, """
fresh = json.loads(Path('.graphify_extract.json').read_text())
assert fresh == {'nodes': [], 'edges': [], 'hyperedges': [],
                 'replacement_sources': [], 'input_tokens': 0, 'output_tokens': 0}
""")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform_key', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('empty', [False, True], ids=['foreign-retained', 'prune-to-empty'])
@pytest.mark.parametrize('directed', [False, True])
@pytest.mark.parametrize('relative_prune', [False, True], ids=['absolute-owner', 'relative-owner'])
def test_deletion_only_consumers_publish_graph_and_manifest_together(
        tmp_path, monkeypatch, platform_key, empty, directed, relative_prune):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform_key, empty=empty, directed=directed)
    before = _published_bytes(fixture.output)
    merge = _deletion_code(platform_key, '# Load new extraction and incremental state', fixture,
                           directed=directed, reference=platform_key == 'claude')
    build = _deletion_code(platform_key, 'wrote = to_json(', fixture, directed=directed)
    finish = _deletion_code(platform_key, '# Update cumulative cost tracker', fixture)
    diff_code = _deletion_code(platform_key, 'from graphify.analyze import graph_diff', fixture,
                               directed=directed, reference=platform_key == 'claude')
    _run(fixture, """
Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [],
    'replacement_sources': []}))
manifest_before = Path('manifest.json').read_bytes()
""" + ("""
incremental = json.loads(Path('.graphify_incremental.json').read_text())
incremental['deleted_files'] = ['./' + str(Path(p).relative_to(root)) for p in incremental['deleted_files']]
Path('.graphify_incremental.json').write_text(json.dumps(incremental))
""" if relative_prune else '') + f"\nexec({merge!r})\n" + """
assert Path('manifest.json').read_bytes() == manifest_before
""")
    assert _published_bytes(fixture.output) == before
    _run(fixture, f'exec({diff_code!r})\nassert old_data is not None\nassert diff["removed_nodes"]')
    _run(fixture, f'exec({build!r})')
    assert _published_bytes(fixture.output) == before
    _run(fixture, f'exec({finish!r})\ntx.finalize_prepared_transaction()')
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='deletion-update')
    old_nodes = {node['id']: node for node in fixture.baseline.data['nodes']}
    expected = {} if empty else {key: old_nodes[key] for key in ['retained', 'anchor']}
    # Clustering recalculates community labels; retained semantic attributes
    # must otherwise survive publication exactly.
    def facts(nodes):
        return {key: {k: v for k, v in node.items() if k != 'community'}
                for key, node in nodes.items()}
    assert facts({node['id']: node for node in current.data['nodes']}) == facts(expected)
    assert current.data['links'] == ([] if empty else fixture.baseline.data['links'])
    assert current.data['hyperedges'] == ([] if empty else [fixture.baseline.data['hyperedges'][0]])
    assert current.data['directed'] == directed
    manifest = json.loads((fixture.output / 'manifest.json').read_text())
    assert set(manifest) == (set() if empty else {'retained.md'})
    from graphify.detect import detect_incremental
    again = detect_incremental(fixture.root, manifest_path=fixture.output / 'manifest.json',
                               google_workspace=False)
    assert again['new_total'] == 0 and again['deleted_files'] == []


@pytest.mark.parametrize('platform_key', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('failure', ['empty-input', 'export-refused', 'report-failed'])
def test_deletion_update_failure_keeps_manifest_and_deletion_pending(
        tmp_path, monkeypatch, platform_key, failure):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform_key)
    before = _published_bytes(fixture.output)
    merge = _deletion_code(platform_key, '# Load new extraction and incremental state', fixture,
                           reference=platform_key == 'claude')
    build = _deletion_code(platform_key, 'wrote = to_json(', fixture)
    if failure == 'empty-input':
        setup = "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'complete_empty_prune': True}))"
    else:
        setup = """Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [],
            'hyperedges': [], 'replacement_sources': []}))""" + f'\nexec({merge!r})'
        if failure == 'export-refused':
            setup += '\nfrom unittest.mock import patch'
            build = f"with patch('graphify.export.to_json', return_value=False):\n    exec({build!r})"
        else:
            setup += "\nfrom unittest.mock import patch"
            build = f"with patch('graphify.report.generate', side_effect=RuntimeError('injected report failure')):\n    exec({build!r})"
    with pytest.raises((SystemExit, RuntimeError)):
        _run(fixture, setup + f'\nexec({build!r})')
    assert _published_bytes(fixture.output) == before
    _run(fixture, """
from graphify.detect import detect_incremental
incremental = detect_incremental(root, manifest_path='manifest.json', google_workspace=False)
assert incremental['deleted_files'] and incremental['new_total'] == 0
""")


@pytest.mark.parametrize('platform_key', ['claude', 'aider', 'devin'])
def test_deletion_step9_failure_does_not_finalize_and_can_retry(tmp_path, monkeypatch, platform_key):
    from tools.skillgen import gen
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform_key, empty=True)
    before = _published_bytes(fixture.output)
    merge = _deletion_code(platform_key, '# Load new extraction and incremental state', fixture,
                           reference=platform_key == 'claude')
    build = _deletion_code(platform_key, 'wrote = to_json(', fixture)
    _run(fixture, """
Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [],
    'replacement_sources': []}))
""" + f'\nexec({merge!r})\nexec({build!r})' + """
Path('cost.json').write_text('invalid cost JSON')
""")
    block, _ = _deletion_stage(platform_key, '# Update cumulative cost tracker')
    block = block.replace(gen._POSIX_OPERATION_GUARD, '').replace('INPUT_PATH', str(fixture.root))
    result = subprocess.run(['bash', '-c', block], cwd=fixture.root, capture_output=True, text=True,
                            env={**os.environ, 'GRAPHIFY_PYTHON': os.sys.executable})
    assert result.returncode != 0 and 'JSONDecodeError' in result.stderr
    assert _published_bytes(fixture.output) == before
    from graphify.detect import detect_incremental
    pending = detect_incremental(fixture.root, manifest_path=fixture.output / 'manifest.json',
                                 google_workspace=False)
    assert pending['deleted_files']
    _run(fixture, "Path('cost.json').unlink()")
    result = subprocess.run(['bash', '-c', block], cwd=fixture.root, capture_output=True, text=True,
                            env={**os.environ, 'GRAPHIFY_PYTHON': os.sys.executable})
    assert result.returncode == 0, result.stdout + result.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='deletion-retry')
    assert current.data['nodes'] == [] and current.data['links'] == []
    assert json.loads((fixture.output / 'manifest.json').read_text()) == {}


def _update_shell(fixture, platform, needle, *, token=None, resume_assignment=None, fail_detection=False):
    import re
    from tools.skillgen import gen
    block, _ = _deletion_stage(platform, needle,
                              reference=platform == 'claude' and needle == 'result = detect_incremental(')
    block = re.sub(r'\bINPUT_PATH\b', str(fixture.root), block.replace(gen._POSIX_OPERATION_GUARD, ''))
    if fail_detection:
        block = block.replace('result = detect_incremental(',
                              "raise RuntimeError('injected detection failure')\nresult = detect_incremental(", 1)
    if resume_assignment is not None:
        block = resume_assignment + '\n' + block
    environment = {**os.environ, 'GRAPHIFY_PYTHON': os.sys.executable,
                   'GRAPHIFY_OUT': str(fixture.output)}
    environment.pop('GRAPHIFY_TRANSACTION_TOKEN', None)
    if token is not None:
        environment['GRAPHIFY_TRANSACTION_TOKEN'] = str(token)
    return subprocess.run(['bash', '-c', block],
                          cwd=fixture.root, capture_output=True, text=True, env=environment)


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('input_spelling', ['.', 'corpus', 'corpus-alias'])
@pytest.mark.parametrize('separate', [False, True])
@pytest.mark.parametrize('directed', [False, True])
def test_relative_update_input_uses_owner_root(tmp_path, monkeypatch, platform, input_spelling, separate, directed):
    import re
    from tools.skillgen import gen
    fixture = _baseline(tmp_path, monkeypatch, separate=separate, directed=directed)
    (fixture.root / 'changed.md').unlink()
    if input_spelling == 'corpus-alias':
        (tmp_path / input_spelling).symlink_to(fixture.root, target_is_directory=True)
    cwd = fixture.root if input_spelling == '.' else tmp_path
    before = _published_bytes(fixture.output)
    def shell(needle, token=None):
        block, _ = _deletion_stage(platform, needle,
                                  reference=platform == 'claude' and needle != '# Update cumulative cost tracker')
        block = block.replace(gen._POSIX_OPERATION_GUARD, '')
        block = re.sub(r'\bINPUT_PATH\b', input_spelling, block).replace('IS_DIRECTED', str(directed))
        environment = {**os.environ, 'GRAPHIFY_PYTHON': os.sys.executable,
                       'GRAPHIFY_OUT': str(fixture.output)}
        environment.pop('GRAPHIFY_TRANSACTION_TOKEN', None)
        if token:
            environment['GRAPHIFY_TRANSACTION_TOKEN'] = str(token)
        return subprocess.run(['bash', '-c', block], cwd=cwd, env=environment,
                              capture_output=True, text=True)
    detected = shell('result = detect_incremental(')
    assert detected.returncode == 0, detected.stderr
    _, token = _update_resume_assignment(detected)
    fixture.token = SimpleNamespace(path=token)
    _run(fixture, """
state = json.loads(Path('.graphify_incremental.json').read_text())
assert state['deleted_files'] == [str(root / 'changed.md')], state
assert state['new_total'] == 0, state
assert state['files']['document'] == [str(root / 'retained.md')], state
""")
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    reset = shell("Only deletions -- creating empty extraction", token)
    assert reset.returncode == 0, reset.stderr
    merged = shell('# Load new extraction and incremental state', token)
    assert merged.returncode == 0, merged.stderr
    _run(fixture, """
merged = json.loads(Path('.graphify_extract.json').read_text())
assert {n['id'] for n in merged['nodes']} == {'retained', 'anchor'}, merged
""")
    build = _deletion_code(platform, 'wrote = to_json(', fixture, directed=directed)
    _run(fixture, f'exec({build!r})')
    assert _published_bytes(fixture.output) == before
    finished = shell('# Update cumulative cost tracker', token)
    assert finished.returncode == 0, finished.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='relative-update')
    assert {n['id'] for n in current.data['nodes']} == {'retained', 'anchor'}
    assert current.data['directed'] == directed
    assert set(json.loads((fixture.output / 'manifest.json').read_text())) == {'retained.md'}


def _update_resume_assignment(result):
    import shlex
    prefix = '[graphify update] Resume with: '
    assignment = next(line.removeprefix(prefix) for line in result.stderr.splitlines()
                      if line.startswith(prefix))
    command, value = shlex.split(assignment)
    key, separator, token = value.partition('=')
    assert command == 'export' and key == 'GRAPHIFY_TRANSACTION_TOKEN' and separator and token
    return assignment, Path(token)


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('deleted', [False, True], ids=['no-op', 'deletion'])
@pytest.mark.parametrize('separate', [False, True], ids=['in-corpus', 'separate-output'])
def test_semantic_update_entry_begins_or_cancels_exact_owner(tmp_path, monkeypatch, platform, deleted, separate):
    fixture = _baseline(tmp_path, monkeypatch, separate=separate)
    if deleted:
        (fixture.root / 'changed.md').unlink()
        _git(fixture.root, 'add', '-u')
        _git(fixture.root, 'commit', '-qm', 'delete source')
    before = _published_bytes(fixture.output)
    result = _update_shell(fixture, platform, 'result = detect_incremental(')
    assert result.returncode == 0, result.stdout + result.stderr
    assert _published_bytes(fixture.output) == before
    if not deleted:
        assert 'Nothing to update' in result.stdout
        assert tx.transaction_status(fixture.output)['transaction'] is None
        tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='no-op-update')
        return
    _assignment, token = _update_resume_assignment(result)
    fixture.token = SimpleNamespace(path=token)
    _run(fixture, """
assert tx.current_transaction().kind == 'update'
assert tx.current_transaction().root == str(root)
assert tx.current_transaction().output == output
assert json.loads(Path('.graphify_incremental.json').read_text())['deleted_files']
""")


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('change', ['no-op', 'modified', 'deleted'])
@pytest.mark.parametrize('separate', [False, True])
def test_receiptless_update_entry_preserves_noop_and_starts_changed_work(tmp_path, monkeypatch, platform, change, separate):
    from graphify.build import build_from_json
    from graphify.detect import save_manifest
    from networkx.readwrite import json_graph
    root = tmp_path / 'legacy-corpus'
    root.mkdir()
    monkeypatch.chdir(root)
    (root / 'changed.md').write_text('Mutable policy one.\n')
    (root / 'retained.md').write_text('Permanent reference.\n')
    output = tmp_path / 'separate-output' if separate else root / 'graphify-out'
    output.mkdir()
    graph = build_from_json(_chunk('one'), root=root)
    (output / 'graph.json').write_text(json.dumps(json_graph.node_link_data(graph, edges='links')))
    save_manifest({'document': [str(root / 'changed.md'), str(root / 'retained.md')]},
                  manifest_path=output / 'manifest.json', root=root)
    if change == 'modified':
        (root / 'changed.md').write_text('Mutable policy two with different requirements.\n')
    elif change == 'deleted':
        (root / 'changed.md').unlink()
    fixture = SimpleNamespace(root=root, output=output)
    before = {str(path.relative_to(output)): path.read_bytes()
              for path in output.rglob('*') if path.is_file()}
    baseline = tx.open_graph_snapshot(output / 'graph.json', purpose='legacy-entry')
    assert baseline.generation is None
    result = _update_shell(fixture, platform, 'result = detect_incremental(')
    assert result.returncode == 0, result.stdout + result.stderr
    assert _published_bytes(output) == {'graph.json': before['graph.json'],
                                        'manifest.json': before['manifest.json'], tx.RECEIPT_FILE: None}
    if change == 'no-op':
        assert 'Nothing to update' in result.stdout + result.stderr
        assert 'Resume with:' not in result.stderr
        after = {str(path.relative_to(output)): path.read_bytes()
                 for path in output.rglob('*') if path.is_file()}
        assert {name: after[name] for name in before} == before
        # Normal detection may warm its existing stat cache without publishing.
        assert set(after) - set(before) <= {'cache/stat-index.json'}
        assert tx.transaction_status(output)['transaction'] is None
        assert tx.open_graph_snapshot(output / 'graph.json', purpose='legacy-noop').generation is None
    else:
        _, token = _update_resume_assignment(result)
        fixture.token = SimpleNamespace(path=token)
        _run(fixture, f"""
state = json.loads(Path('.graphify_incremental.json').read_text())
assert state['new_total'] == {1 if change == 'modified' else 0}
assert state['deleted_files'] == {[str(root / 'changed.md')] if change == 'deleted' else []!r}
assert tx.open_update_baseline(tx.current_transaction()).generation is None
""")


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('damage', ['nodes', 'links', 'metadata', 'null-watermark', 'foreign-queue'])
def test_receiptless_noop_keeps_legacy_admission_refusals(tmp_path, monkeypatch, platform, damage):
    from graphify.build import build_from_json
    from graphify.detect import save_manifest
    from networkx.readwrite import json_graph
    root = tmp_path / 'legacy-corpus'
    root.mkdir()
    monkeypatch.chdir(root)
    for name in ('changed.md', 'retained.md'):
        (root / name).write_text('Stable policy.\n')
    output = root / 'graphify-out'
    output.mkdir()
    data = json_graph.node_link_data(build_from_json(_chunk('one'), root=root), edges='links')
    if damage in {'nodes', 'links'}:
        data[damage] = {}
    elif damage == 'metadata':
        data['graph'] = []
    elif damage == 'null-watermark':
        data['graph'][tx.GRAPH_WATERMARK_KEY] = None
    (output / 'graph.json').write_text(json.dumps(data))
    save_manifest({'document': [str(root / 'changed.md'), str(root / 'retained.md')]},
                  manifest_path=output / 'manifest.json', root=root)
    if damage == 'foreign-queue':
        other = tmp_path / 'other-root'
        other.mkdir()
        tx.queue_rebuild('update', other, output=output, changed_paths=['policy.md'],
                         source='legacy-admission-fixture')
    before = {str(path.relative_to(output)): path.read_bytes()
              for path in output.rglob('*') if path.is_file()}
    result = _update_shell(SimpleNamespace(root=root, output=output), platform, 'result = detect_incremental(')
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'Nothing to update' not in result.stdout + result.stderr
    assert {str(path.relative_to(output)): path.read_bytes()
            for path in output.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
def test_receiptless_noop_keeps_legacy_edges_key_compatibility(tmp_path, monkeypatch, platform):
    from graphify.detect import save_manifest
    root = tmp_path / 'legacy-corpus'
    root.mkdir()
    monkeypatch.chdir(root)
    document = root / 'policy.md'
    document.write_text('Stable policy.\n')
    output = root / 'graphify-out'
    output.mkdir()
    (output / 'graph.json').write_text(json.dumps({'nodes': [], 'edges': []}))
    save_manifest({'document': [str(document)]}, manifest_path=output / 'manifest.json', root=root)
    before = _published_bytes(output)
    result = _update_shell(SimpleNamespace(root=root, output=output), platform, 'result = detect_incremental(')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Nothing to update' in result.stdout + result.stderr
    assert _published_bytes(output) == before
    assert tx.transaction_status(output)['transaction'] is None


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('damage', ['absent-token', 'foreign-output', 'wrong-kind', 'wrong-root'])
def test_update_entry_refuses_mismatched_handoff_without_mutation(tmp_path, monkeypatch, platform, damage):
    fixture = _baseline(tmp_path, monkeypatch)
    wrong_root = tmp_path / 'other-root'
    wrong_root.mkdir()
    owner = tx.begin_transaction('full' if damage == 'wrong-kind' else 'update',
                                 wrong_root if damage == 'wrong-root' else fixture.root,
                                 output=fixture.output)
    token = tx.stage_transaction_handoff(owner).path
    if damage == 'foreign-output':
        foreign = tmp_path / 'foreign'
        foreign.mkdir()
        foreign_owner = tx.begin_transaction('update', wrong_root, output=foreign / 'graphify-out')
        token = tx.stage_transaction_handoff(foreign_owner).path
    def files():
        return {str(p.relative_to(tmp_path)): p.read_bytes()
                for p in tmp_path.rglob('*') if p.is_file()}
    before = files()
    result = _update_shell(fixture, platform, 'result = detect_incremental(',
                           token=None if damage == 'absent-token' else token)
    assert result.returncode != 0
    assert 'retained exact token' in result.stderr or 'handoff root, output or kind mismatch' in result.stderr
    assert files() == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
def test_diff_baseline_is_removed_before_publication(tmp_path, monkeypatch, platform):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform, empty=True)
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    diff = _deletion_code(platform, 'from graphify.analyze import graph_diff', fixture, reference=platform == 'claude')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})\nexec({diff!r})\nexec({build!r})')
    result = _update_shell(fixture, platform, '# Update cumulative cost tracker', token=fixture.token.path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (fixture.output / '.graphify_old.json').exists()
    receipt = json.loads((fixture.output / tx.RECEIPT_FILE).read_text())
    assert '.graphify_old.json' not in json.dumps(receipt)
    assert tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='pruned-update').data['nodes'] == []


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('completed', [False, True], ids=['failed-extraction', 'successful-empty'])
def test_mixed_empty_extraction_requires_completion(tmp_path, monkeypatch, platform, completed):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform, empty=True)
    (fixture.root / 'new.md').write_text('New document requires semantic extraction.\n')
    before = _published_bytes(fixture.output)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture, reference=platform == 'claude')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    replacement = [str(fixture.root / 'new.md')] if completed else []
    _run(fixture, f'exec({detect!r})' + "\nassert result['new_total'] == 1")
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': REPLACEMENTS}))".replace('REPLACEMENTS', repr(replacement)) + f'\nexec({merge!r})')
    if completed:
        _run(fixture, f'exec({build!r})')
    else:
        with pytest.raises(SystemExit):
            _run(fixture, f'exec({build!r})')
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
def test_full_update_retry_detects_unpublished_deletion_and_finalizes(tmp_path, monkeypatch, platform):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform, empty=True)
    before = _published_bytes(fixture.output)
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
    reset = "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
    # Stop after the successful Step 9 body, before its separate finalizer.
    _run(fixture, reset + f'\nexec({merge!r})\nexec({build!r})\nexec({finish!r})')
    assert _published_bytes(fixture.output) == before
    result = _update_shell(fixture, platform, 'result = detect_incremental(', token=fixture.token.path)
    assert result.returncode == 0 and 'Nothing to update' not in result.stdout, result.stdout + result.stderr
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "assert json.loads(Path('.graphify_incremental.json').read_text())['deleted_files']\n"
         + reset + f'\nexec({merge!r})\nexec({build!r})')
    result = _update_shell(fixture, platform, '# Update cumulative cost tracker', token=fixture.token.path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='retried-update').data['nodes'] == []
    assert json.loads((fixture.output / 'manifest.json').read_text()) == {}
    assert not (fixture.output / '.graphify_old.json').exists()


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('failpoint', ['detect', 'report'])
@pytest.mark.parametrize('separate', [False, True], ids=['in-corpus', 'separate-output'])
def test_printed_update_handoff_resumes_in_a_fresh_shell(tmp_path, monkeypatch, platform, failpoint, separate):
    fixture = _baseline(tmp_path, monkeypatch, separate=separate)
    for name in ('changed.md', 'retained.md'):
        (fixture.root / name).unlink()
    _git(fixture.root, 'add', '-u')
    _git(fixture.root, 'commit', '-qm', 'delete final sources')
    before = _published_bytes(fixture.output)
    started = _update_shell(fixture, platform, 'result = detect_incremental(',
                             fail_detection=failpoint == 'detect')
    assignment, token = _update_resume_assignment(started)
    assert token == tx.active_transaction_token_path(fixture.output)
    fixture.token = SimpleNamespace(path=token)
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    reset = "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
    if failpoint == 'detect':
        assert started.returncode != 0 and 'injected detection failure' in started.stderr
    else:
        assert started.returncode == 0, started.stderr
        if platform == 'claude':
            population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
            _run(fixture, f'exec({population!r})')
        with pytest.raises(RuntimeError, match='injected report failure'):
            _run(fixture, reset + f'\nexec({merge!r})\n' +
                 "from unittest.mock import patch\nwith patch('graphify.report.generate', side_effect=RuntimeError('injected report failure')):\n" +
                 f'    exec({build!r})')
    assert _published_bytes(fixture.output) == before
    refused = _update_shell(fixture, platform, 'result = detect_incremental(')
    assert refused.returncode != 0 and 'retained exact token' in refused.stderr
    assert _published_bytes(fixture.output) == before
    # Reuse only the actual shipped resume assignment, without a parent-shell
    # environment or test-added token print command.
    resumed = _update_shell(fixture, platform, 'result = detect_incremental(', resume_assignment=assignment)
    assert resumed.returncode == 0 and 'Nothing to update' not in resumed.stdout, resumed.stderr
    assert _update_resume_assignment(resumed)[1] == token
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, reset + f'\nexec({merge!r})\nexec({build!r})')
    finished = _update_shell(fixture, platform, '# Update cumulative cost tracker', resume_assignment=assignment)
    assert finished.returncode == 0, finished.stdout + finished.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='fresh-shell-retry')
    assert current.data['nodes'] == [] and current.generation != fixture.baseline.generation
    assert json.loads((fixture.output / 'manifest.json').read_text()) == {}


def _baseline_with_exports(tmp_path, monkeypatch, *, directed=False):
    original = tx.finalize_prepared_transaction
    def finalize_with_exports():
        from graphify.export import to_graphml, to_html, to_obsidian, to_canvas
        from graphify.wiki import to_wiki
        from networkx.readwrite import json_graph
        graph = json_graph.node_link_graph(json.loads(Path('graph.json').read_text()), edges='links')
        communities = {0: list(graph.nodes)}
        to_graphml(graph, communities, 'graph.graphml')
        to_html(graph, communities, 'graph.html', graph_path='graph.json')
        to_wiki(graph, communities, 'wiki')
        to_obsidian(graph, communities, 'obsidian')
        to_canvas(graph, communities, 'obsidian/graph.canvas')
        for name in ('graph.svg', 'cypher.txt', 'graphify-callflow.html', 'GRAPH_TREE.html'):
            tx.commit_prepared_bytes(tx.current_transaction(), name, b'old derived node: changed')
        tx.commit_prepared_bytes(tx.current_transaction(), 'evidence.txt', b'retained unrelated artifact')
        original()
    with monkeypatch.context() as patch:
        patch.setattr(tx, 'finalize_prepared_transaction', finalize_with_exports)
        fixture = _baseline(tmp_path, monkeypatch, directed=directed)
    fixture.baseline = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='prior-exports',
                                              retain_artifacts=('*',))
    return fixture


def _run_rendered_wiki(fixture, platform, *, directed=False):
    import re
    from tools.skillgen import gen
    artifacts = gen.render(gen.load_platforms()[platform])
    artifact = next((a for a in artifacts if a.path.endswith('/references/exports.md')), artifacts[0])
    section = artifact.content.split('### Step 6b - Wiki', 1)[1].split('### Step 7 -', 1)[0]
    block = re.findall(r'```(?:bash|sh)\n(.*?)\n```', section, re.S)[0]
    assert 'run-prepared-token' in block
    block = block.replace(gen._POSIX_OPERATION_GUARD, '').replace('INPUT_PATH', str(fixture.root))
    block = block.replace('IS_DIRECTED', str(directed))
    return subprocess.run(['bash', '-c', block], cwd=fixture.root, capture_output=True, text=True,
                          env={**os.environ, 'GRAPHIFY_PYTHON': os.sys.executable,
                               'GRAPHIFY_OUT': str(fixture.output),
                               'GRAPHIFY_TRANSACTION_TOKEN': str(fixture.token.path)})


@pytest.mark.parametrize('platform', ['claude', 'codex', 'opencode', 'devin'])
@pytest.mark.parametrize('empty', [False, True], ids=['foreign-retained', 'prune-to-empty'])
@pytest.mark.parametrize('directed', [False, True])
def test_rendered_wiki_flag_allows_deletion_publication(tmp_path, monkeypatch, platform, empty, directed):
    fixture = _baseline_with_exports(tmp_path, monkeypatch, directed=directed)
    wiki_before = {n: b for n, b in fixture.baseline.artifacts.items() if n.startswith('wiki/')}
    user_note = fixture.output / 'wiki/user-note.md'
    user_note.write_text('User-owned wiki note.\n')
    for name in (('changed.md', 'retained.md') if empty else ('changed.md',)):
        (fixture.root / name).unlink()
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    common = 'devin' if platform == 'devin' else 'claude'
    detect = _deletion_code(common, 'result = detect_incremental(', fixture, reference=common == 'claude')
    merge = _deletion_code(common, '# Load new extraction and incremental state', fixture,
                           reference=common == 'claude', directed=directed)
    build = _deletion_code(common, 'wrote = to_json(', fixture, directed=directed)
    finish = _deletion_code(common, '# Update cumulative cost tracker', fixture)
    before = _published_bytes(fixture.output)
    _run(fixture, f'exec({detect!r})')
    if common == 'claude':
        population = _deletion_code(common, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})\nexec({build!r})')
    exported = _run_rendered_wiki(fixture, platform, directed=directed)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    if empty:
        assert 'Skipping wiki export for accepted empty prune' in exported.stdout
    else:
        assert 'Skipping wiki export' not in exported.stdout
        assert 'articles written' in exported.stdout
    assert _published_bytes(fixture.output) == before
    assert {n: (fixture.output / n).read_bytes() for n in wiki_before} == wiki_before
    # Stop before finalization: optional export success must not advance public
    # graph/manifest or remove public wiki files early.
    _run(fixture, f'exec({finish!r})\ntx.finalize_prepared_transaction()')
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='wiki-update', retain_artifacts=('*',))
    assert {n['id'] for n in current.data['nodes']} == (set() if empty else {'retained', 'anchor'})
    assert current.data['directed'] == directed
    if empty:
        assert not any(n.startswith('wiki/') for n in current.artifacts)
        assert not any((fixture.output / n).exists() for n in wiki_before)
    else:
        assert 'wiki/index.md' in current.artifacts
        assert b'2 nodes' in current.artifacts['wiki/index.md']
    assert user_note.read_text() == 'User-owned wiki note.\n'


@pytest.mark.parametrize('platform', ['claude', 'devin'])
@pytest.mark.parametrize('state', ['ordinary-empty', 'replacement-empty', 'flag-only',
                                  'nonempty-flag', 'full-owner', 'nonempty-no-communities'])
def test_wiki_skip_keeps_other_empty_community_refusals(tmp_path, monkeypatch, platform, state):
    fixture = _baseline_with_exports(tmp_path, monkeypatch)
    fixture.owner = tx.begin_transaction('full' if state == 'full-owner' else 'update',
                                         fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    extraction = {'accepted_merge': state != 'flag-only',
                  'complete_empty_prune': state not in {'ordinary-empty', 'replacement-empty', 'nonempty-no-communities'},
                  'complete_empty_replacement': state == 'replacement-empty',
                  'nodes': (_chunk('one')['nodes'] if state == 'nonempty-no-communities' else
                            [_chunk('one')['nodes'][0]] if state == 'nonempty-flag' else []),
                  'edges': [], 'hyperedges': []}
    _run(fixture, f"""
from graphify.export import to_json
from graphify.build import build_from_json
graph = build_from_json({_chunk('one')!r} if {state == 'nonempty-no-communities'!r} else {{'nodes': [], 'edges': []}})
# Deliberately construct the isolated empty wiki-refusal fixture, without
# invoking the separate shrink guard under test in the export regressions.
assert to_json(graph, {{}}, 'graph.json', force=True)
tx.commit_prepared_bytes(tx.current_transaction(), 'manifest.json', {fixture.baseline.manifest_payload!r})
Path('.graphify_extract.json').write_text(json.dumps({extraction!r}))
Path('.graphify_analysis.json').write_text(json.dumps({{'communities': {{}}, 'cohesion': {{}}}}))
Path('.graphify_labels.json').write_text('{{}}')
tx.commit_prepared_bytes(tx.current_transaction(), 'wiki/keep.md', b'private wiki stays intact')
""")
    before = _published_bytes(fixture.output)
    result = _run_rendered_wiki(fixture, platform)
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'Skipping wiki export' not in result.stdout
    assert 'empty' in result.stderr or 'stale' in result.stderr
    assert _published_bytes(fixture.output) == before
    _run(fixture, "assert Path('wiki/keep.md').read_bytes() == b'private wiki stays intact'")


@pytest.mark.parametrize('platform', ['claude', 'devin'])
def test_rendered_wiki_keeps_nonempty_full_build_export(tmp_path, monkeypatch, platform):
    fixture = _baseline_with_exports(tmp_path, monkeypatch)
    fixture.owner = tx.begin_transaction('full', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    _run(fixture, f"""
from graphify.export import to_json
from graphify.build import build_from_json
extraction = {_chunk('one')!r}
graph = build_from_json(extraction)
assert to_json(graph, {{0: list(graph.nodes)}}, 'graph.json')
tx.commit_prepared_bytes(tx.current_transaction(), 'manifest.json', {fixture.baseline.manifest_payload!r})
Path('.graphify_extract.json').write_text(json.dumps(extraction))
Path('.graphify_analysis.json').write_text(json.dumps({{'communities': {{'0': list(graph.nodes)}}, 'cohesion': {{}}}}))
Path('.graphify_labels.json').write_text('{{}}')
""")
    before = _published_bytes(fixture.output)
    exported = _run_rendered_wiki(fixture, platform)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert 'articles written' in exported.stdout and 'Skipping wiki export' not in exported.stdout
    assert _published_bytes(fixture.output) == before
    _run(fixture, "assert b'3 nodes' in Path('wiki/index.md').read_bytes()")


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('empty', [False, True], ids=['foreign-retained', 'prune-to-empty'])
@pytest.mark.parametrize('outcome', ['publish', 'regenerate', 'report-failed'])
def test_prune_to_empty_invalidates_only_managed_prior_exports(tmp_path, monkeypatch, platform, empty, outcome):
    fixture = _baseline_with_exports(tmp_path, monkeypatch)
    stale = {name: payload for name, payload in fixture.baseline.artifacts.items()
             if name in {'graph.html', 'graph.svg', 'graph.graphml', 'cypher.txt',
                         'graphify-callflow.html', 'GRAPH_TREE.html'}
             or name.startswith(('wiki/', 'obsidian/'))}
    assert {'graph.html', 'graph.graphml', 'wiki/index.md', 'obsidian/graph.canvas'} <= set(stale)
    assert b'changed' in stale['graph.graphml']
    unmanaged = {'wiki/user-note.md': b'user wiki note', 'obsidian/user-note.md': b'user vault note'}
    for name, payload in unmanaged.items():
        (fixture.output / name).write_bytes(payload)
    for name in (('changed.md', 'retained.md') if empty else ('changed.md',)):
        (fixture.root / name).unlink()
    _git(fixture.root, 'add', '-u')
    _git(fixture.root, 'commit', '-qm', 'delete sources with prior exports')
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    before = _published_bytes(fixture.output)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture, reference=platform == 'claude')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    _run(fixture, f'exec({detect!r})')
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})')
    assert _published_bytes(fixture.output) == before
    assert {name: (fixture.output / name).read_bytes() for name in stale} == stale
    if outcome == 'report-failed':
        with pytest.raises(RuntimeError, match='injected report failure'):
            _run(fixture, "from unittest.mock import patch\nwith patch('graphify.report.generate', side_effect=RuntimeError('injected report failure')):\n"
                 + f'    exec({build!r})')
        assert _published_bytes(fixture.output) == before
        assert {name: (fixture.output / name).read_bytes() for name in stale} == stale
    _run(fixture, f'exec({build!r})')
    if outcome == 'regenerate':
        _run(fixture, "import sys\nfrom graphify.cli import dispatch_command\nsys.argv=['graphify', 'export', 'graphml']\ndispatch_command('export')")
    finished = _update_shell(fixture, platform, '# Update cumulative cost tracker', token=fixture.token.path)
    assert finished.returncode == 0, finished.stdout + finished.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='updated-exports', retain_artifacts=('*',))
    assert {node['id'] for node in current.data['nodes']} == (set() if empty else {'retained', 'anchor'})
    for name, payload in stale.items():
        if name == 'graph.graphml' and outcome == 'regenerate':
            assert name in current.artifacts and b'changed' not in current.artifacts[name]
        elif empty:
            assert name not in current.artifacts and not (fixture.output / name).exists()
        else:
            # Nonempty optional-export retention predates this deletion-only fix.
            assert current.artifacts[name] == payload
    assert current.artifacts['evidence.txt'] == b'retained unrelated artifact'
    assert {name: (fixture.output / name).read_bytes() for name in unmanaged} == unmanaged


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('destination', [None, 'custom.html', 'nested/architecture.txt'])
@pytest.mark.parametrize('empty', [False, True])
def test_empty_prune_invalidates_owned_callflow_destinations(tmp_path, monkeypatch, platform, destination, empty):
    fixture = _baseline(tmp_path, monkeypatch)
    sections = tmp_path / 'sections.json'
    sections.write_text(json.dumps([{'id': 'rules', 'name': 'Rules', 'communities': [0]}]))
    args = [os.sys.executable, '-m', 'graphify', 'export', 'callflow-html',
            '--graph', str(fixture.output / 'graph.json'), '--sections', str(sections)]
    if destination:
        args += ['--output', str(fixture.output / destination)]
    exported = subprocess.run(args, cwd=fixture.root, capture_output=True, text=True)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    fixture.baseline = tx.open_graph_snapshot(fixture.output / 'graph.json',
                                              purpose='callflow-baseline', retain_artifacts=('*',))
    flows = {name: payload for name, payload in fixture.baseline.artifacts.items()
             if b'graphify callflow-html' in payload}
    assert len(flows) == 1 and b'Permanent rule group' in next(iter(flows.values()))
    if destination:
        assert set(flows) == {destination}
    else:
        assert next(iter(flows)).endswith('-callflow.html')
        assert 'graphify-callflow.html' not in flows
    user = fixture.output / 'user-callflow.html'
    user.write_bytes(next(iter(flows.values())))
    external = tmp_path / 'external.html'
    external.write_bytes(user.read_bytes())
    for name in (('changed.md', 'retained.md') if empty else ('changed.md',)):
        (fixture.root / name).unlink()
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    before = _published_bytes(fixture.output)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture, reference=platform == 'claude')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
    _run(fixture, f'exec({detect!r})')
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})')
    assert _published_bytes(fixture.output) == before
    assert {name: (fixture.output / name).read_bytes() for name in flows} == flows
    _run(fixture, f'exec({build!r})\nexec({finish!r})\ntx.finalize_prepared_transaction()')
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='callflow-pruned', retain_artifacts=('*',))
    for name, payload in flows.items():
        if empty:
            assert name not in current.artifacts, 'stale managed callflow was published'
            assert not (fixture.output / name).exists()
        else:
            assert current.artifacts[name] == payload
    assert user.read_bytes() == external.read_bytes() == next(iter(flows.values()))


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('destination', ['views/tree.html', 'nested/structure.txt'])
@pytest.mark.parametrize('empty', [False, True])
def test_empty_prune_invalidates_only_owned_custom_tree_destinations(tmp_path, monkeypatch, platform, destination, empty):
    fixture = _baseline(tmp_path, monkeypatch)
    exported = subprocess.run([os.sys.executable, '-m', 'graphify', 'tree',
        '--graph', str(fixture.output / 'graph.json'), '--output', str(fixture.output / destination)],
        cwd=fixture.root, capture_output=True, text=True)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    fixture.baseline = tx.open_graph_snapshot(fixture.output / 'graph.json',
                                              purpose='tree-baseline', retain_artifacts=('*',))
    payload = fixture.baseline.artifacts[destination]
    assert b'changed.md' in payload and b'graphify tree viewer</title>' in payload
    user = fixture.output / 'user-tree.html'
    external = tmp_path / 'external-tree.html'
    user.write_bytes(payload)
    external.write_bytes(payload)
    for name in (('changed.md', 'retained.md') if empty else ('changed.md',)):
        (fixture.root / name).unlink()
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    before = _published_bytes(fixture.output)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture, reference=platform == 'claude')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
    _run(fixture, f'exec({detect!r})')
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})')
    assert _published_bytes(fixture.output) == before
    assert (fixture.output / destination).read_bytes() == payload
    _run(fixture, f'exec({build!r})\nexec({finish!r})\ntx.finalize_prepared_transaction()')
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='tree-pruned', retain_artifacts=('*',))
    if empty:
        assert destination not in current.artifacts, 'stale managed tree was published'
        assert not (fixture.output / destination).exists()
    else:
        assert current.artifacts[destination] == payload
    assert user.read_bytes() == external.read_bytes() == payload


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('failed_attempt', ['empty-prune', 'new-exports'])
def test_retry_restores_complete_published_inventory(tmp_path, monkeypatch, platform, failed_attempt):
    fixture = _baseline_with_exports(tmp_path, monkeypatch)
    for name in ('changed.md', 'retained.md'):
        (fixture.root / name).unlink()
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture, reference=platform == 'claude')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    before = _published_bytes(fixture.output)
    _run(fixture, f'exec({detect!r})')
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})\nexec({build!r})')
    if failed_attempt == 'new-exports':
        _run(fixture, """
tx.commit_prepared_bytes(tx.current_transaction(), 'new/staged.html', b'failed attempt export')
tx.commit_prepared_bytes(tx.current_transaction(), 'evidence.txt', b'failed attempt mutation')
""")
    assert _published_bytes(fixture.output) == before
    (fixture.root / 'retained.md').write_text('Unrelated stable rule requires permanent reference.\n')
    resumed = _update_shell(fixture, platform, 'result = detect_incremental(', token=fixture.token.path)
    assert resumed.returncode == 0, resumed.stderr
    # All published payloads, including optional exports and cost/evidence, are
    # restored before the new attempt. Its detection files alone are fresh.
    _run(fixture, """
baseline = tx.open_update_baseline(tx.current_transaction())
for name, payload in baseline.artifacts.items():
    assert Path(name).read_bytes() == payload, 'retry lost published artifact: ' + name
assert not Path('new/staged.html').exists(), 'retry retained failed export'
assert not Path('.graphify_extract.json').exists(), 'retry retained failed extraction'
assert not Path('.graphify_old.json').exists(), 'retry retained failed diff'
""")
    if platform == 'claude':
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})\nexec({build!r})')
    finished = _update_shell(fixture, platform, '# Update cumulative cost tracker', token=fixture.token.path)
    assert finished.returncode == 0, finished.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='retry-exports', retain_artifacts=('*',))
    assert {n['id'] for n in current.data['nodes']} == {'retained', 'anchor'}
    for name, payload in fixture.baseline.artifacts.items():
        if name not in {'graph.json', 'manifest.json', 'cost.json', 'GRAPH_REPORT.md'}:
            assert current.artifacts[name] == payload, name
    assert 'new/staged.html' not in current.artifacts


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
def test_retry_diff_keeps_published_predecessor_after_step9(tmp_path, monkeypatch, platform):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform, empty=True)
    before = _published_bytes(fixture.output)
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture,
                           reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
    reset = "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
    _run(fixture, reset + f'\nexec({merge!r})\nexec({build!r})\nexec({finish!r})')
    assert _published_bytes(fixture.output) == before
    resumed = _update_shell(fixture, platform, 'result = detect_incremental(', token=fixture.token.path)
    assert resumed.returncode == 0, resumed.stderr
    _run(fixture, reset + f'\nexec({merge!r})' + "\nassert json.loads(Path('.graphify_old.json').read_text())['nodes'], 'retry diff lost published predecessor'")
    diff = _deletion_code(platform, 'from graphify.analyze import graph_diff', fixture,
                          reference=platform == 'claude')
    _run(fixture, f'exec({diff!r})\nassert len(diff["removed_nodes"]) == 3')
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('directed', [False, True])
@pytest.mark.parametrize('failpoint', ['report', 'step9'])
def test_retry_merge_discards_failed_attempt_semantics(tmp_path, monkeypatch, platform, directed, failpoint):
    fixture = _prepare(_baseline(tmp_path, monkeypatch, directed=directed))
    before = _published_bytes(fixture.output)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture,
                            reference=platform == 'claude', directed=directed)
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture,
                           reference=platform == 'claude', directed=directed)
    build = _deletion_code(platform, 'wrote = to_json(', fixture, directed=directed)
    fresh = {'nodes': [_chunk('two')['nodes'][0], {'id': 'transient', 'source_file': 'changed.md'}],
             'edges': [{'source': 'changed', 'target': 'transient', 'source_file': 'changed.md', 'relation': 'staged'}],
             'hyperedges': [{'id': 'staged-group', 'nodes': ['changed', 'transient'], 'source_file': 'changed.md'}],
             'replacement_sources': ['changed.md']}
    _run(fixture, f'exec({detect!r})')
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, f'Path(".graphify_extract.json").write_text(json.dumps({fresh!r}))\nexec({merge!r})')
    if failpoint == 'report':
        with pytest.raises(RuntimeError, match='injected report failure'):
            _run(fixture, "from unittest.mock import patch\nwith patch('graphify.report.generate', side_effect=RuntimeError('injected report failure')):\n"
                 + f'    exec({build!r})')
    else:
        finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
        _run(fixture, f'exec({build!r})\nexec({finish!r})')
    assert _published_bytes(fixture.output) == before
    (fixture.root / 'changed.md').write_text('Mutable policy three. Extraction fails.\n')
    resumed = _update_shell(fixture, platform, 'result = detect_incremental(', token=fixture.token.path)
    assert resumed.returncode == 0, resumed.stderr
    if platform == 'claude':
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})' + "\nassert next(n for n in merged_out['nodes'] if n['id'] == 'changed')['label'] == 'Mutable policy one', 'retry retained failed-attempt semantics'")
    _run(fixture, f'exec({build!r})')
    result = _update_shell(fixture, platform, '# Update cumulative cost tracker', token=fixture.token.path)
    assert result.returncode == 0, result.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='retry-published-baseline')
    semantic_nodes = lambda data: {n['id']: {k: v for k, v in n.items() if k != 'community'}
                                   for n in data['nodes']}
    assert semantic_nodes(current.data) == semantic_nodes(fixture.baseline.data)
    assert current.data['links'] == fixture.baseline.data['links']
    assert current.data['hyperedges'] == fixture.baseline.data['hyperedges']

    manifest = json.loads((fixture.output / 'manifest.json').read_text())
    assert manifest == json.loads(fixture.baseline.manifest_payload)
    from graphify.detect import detect_incremental
    pending = detect_incremental(fixture.root, manifest_path=fixture.output / 'manifest.json',
                                 google_workspace=False)
    assert str(fixture.root / 'changed.md') in pending['new_files']['document']


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('vault', ['vault', 'nested/vault', '.'])
@pytest.mark.parametrize('outcome', ['publish', 'report-failed', 'regenerate'])
def test_prune_to_empty_invalidates_custom_managed_vault(tmp_path, monkeypatch, platform, vault, outcome):
    fixture = _baseline(tmp_path, monkeypatch)
    exported = subprocess.run([os.sys.executable, '-m', 'graphify', 'export', 'obsidian',
                               '--graph', str(fixture.output / 'graph.json'),
                               '--dir', str(fixture.output / vault)],
                              cwd=fixture.root, capture_output=True, text=True)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    fixture.baseline = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='custom-vault',
                                              retain_artifacts=('*',))
    prefix = '' if vault == '.' else vault + '/'
    evidence = prefix + 'evidence.txt'
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    _run(fixture, f"tx.commit_prepared_bytes(tx.current_transaction(), {evidence!r}, b'unrelated managed evidence')\ntx.finalize_prepared_transaction()")
    fixture.baseline = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='vault-with-evidence',
                                              retain_artifacts=('*',))
    stale = {n: b for n, b in fixture.baseline.artifacts.items()
             if n.startswith(prefix) and n not in {'graph.json', 'manifest.json', 'cost.json', evidence}}
    assert prefix + '.graphify_obsidian_manifest.json' in stale
    user_note = fixture.output / vault / 'user-note.md'
    user_note.write_text('User-owned note.\n')
    external = tmp_path / 'external-vault'
    external.mkdir()
    (external / 'keep.md').write_text('External vault note.\n')
    for name in ('changed.md', 'retained.md'):
        (fixture.root / name).unlink()
    fixture.owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    before = _published_bytes(fixture.output)
    detect = _deletion_code(platform, 'result = detect_incremental(', fixture, reference=platform == 'claude')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture, reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    _run(fixture, f'exec({detect!r})')
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
         + f'\nexec({merge!r})')
    if outcome == 'report-failed':
        with pytest.raises(RuntimeError, match='injected report failure'):
            _run(fixture, "from unittest.mock import patch\nwith patch('graphify.report.generate', side_effect=RuntimeError('injected report failure')):\n"
                 + f'    exec({build!r})')
    _run(fixture, f'exec({build!r})')
    if outcome == 'regenerate':
        _run(fixture, "import sys\nfrom graphify.cli import dispatch_command\nsys.argv=['graphify', 'export', 'obsidian', '--dir', VAULT]\ndispatch_command('export')".replace('VAULT', repr(vault)))
    assert _published_bytes(fixture.output) == before
    assert {n: (fixture.output / n).read_bytes() for n in stale} == stale
    finished = _update_shell(fixture, platform, '# Update cumulative cost tracker', token=fixture.token.path)
    assert finished.returncode == 0, finished.stderr
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='empty-custom-vault', retain_artifacts=('*',))
    assert current.data['nodes'] == []
    for name, payload in stale.items():
        if outcome == 'regenerate' and name in current.artifacts:
            assert b'Mutable policy one' not in current.artifacts[name]
            assert b'Unrelated stable rule' not in current.artifacts[name]
        else:
            assert name not in current.artifacts, 'stale custom managed vault was published'
            assert not (fixture.output / name).exists()
    assert current.artifacts[evidence] == b'unrelated managed evidence'
    assert user_note.read_text() == 'User-owned note.\n'
    assert (external / 'keep.md').read_text() == 'External vault note.\n'


@pytest.mark.parametrize('separate', [False, True])
def test_update_baseline_reads_published_bytes_after_staged_write(tmp_path, monkeypatch, separate):
    fixture = _prepare(_baseline(tmp_path, monkeypatch, separate=separate))
    before = _published_bytes(fixture.output)
    _run(fixture, """
from graphify.export import to_json
from graphify.transaction import open_update_baseline
fresh = {'nodes': [{'id': 'staged', 'source_file': 'changed.md'}], 'edges': []}
graph = build_merge([fresh], Path.cwd() / 'graph.json', root=root,
                    replacement_sources=['changed.md'])
assert to_json(graph, {}, 'graph.json')
private_before = Path('graph.json').read_bytes()
baseline = open_update_baseline(tx.current_transaction())
assert baseline.payload == (output / 'graph.json').read_bytes()
assert baseline.manifest_payload == (output / 'manifest.json').read_bytes()
assert {n['id'] for n in baseline.data['nodes']} == {'changed', 'retained', 'anchor'}
assert baseline.generation == tx.current_transaction().generation - 1
assert Path('graph.json').read_bytes() == private_before
""")
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('damage', ['graph', 'manifest', 'receipt', 'predecessor',
                                   'missing-predecessor', 'graph-symlink', 'size-limit'])
def test_update_baseline_refuses_changed_published_admission(tmp_path, monkeypatch, damage):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    mutations = {
        'graph': "(output / 'graph.json').write_text('{}')",
        'manifest': "(output / 'manifest.json').write_text('{}')",
        'receipt': "(output / tx.RECEIPT_FILE).write_text('{}')",
        'predecessor': "(output / tx.PREDECESSOR_FILE).write_text('{}')",
        'missing-predecessor': "(output / tx.PREDECESSOR_FILE).unlink()",
        'graph-symlink': "copy = output.parent / 'graph-copy.json'; copy.write_bytes((output / 'graph.json').read_bytes()); (output / 'graph.json').unlink(); (output / 'graph.json').symlink_to(copy)",
        'size-limit': 'pass',
    }
    if damage == 'size-limit':
        monkeypatch.setenv('GRAPHIFY_MAX_GRAPH_BYTES', '10')
    _run(fixture, """
from graphify.transaction import open_update_baseline
""" + mutations[damage] + """
before = {str(p): p.read_bytes() for p in output.parent.rglob('*') if p.is_file()}
try:
    open_update_baseline(tx.current_transaction())
except tx.PendingTransactionError:
    pass
else:
    raise AssertionError('changed published admission was accepted')
assert {str(p): p.read_bytes() for p in output.parent.rglob('*') if p.is_file()} == before
""")


def test_update_baseline_requires_exact_update_owner(tmp_path, monkeypatch):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    _run(fixture, """
from dataclasses import replace
from graphify.transaction import open_update_baseline
owner = tx.current_transaction()
for candidate in (replace(owner, id='0' * 64), replace(owner, kind='full'),
                  replace(owner, generation=owner.generation + 1)):
    try:
        open_update_baseline(candidate)
    except tx.PendingTransactionError:
        pass
    else:
        raise AssertionError('forged update owner accepted')
authority = tx._AUTHORITY.set(None)
try:
    try:
        open_update_baseline(owner)
    except tx.PendingTransactionError:
        pass
    else:
        raise AssertionError('absent update owner accepted')
finally:
    tx._AUTHORITY.reset(authority)
""")
    assert _published_bytes(fixture.output) == before


def test_update_baseline_preserves_receiptless_legacy_admission(tmp_path, monkeypatch):
    from graphify.build import build_from_json
    from networkx.readwrite import json_graph
    root = tmp_path / 'legacy-corpus'
    root.mkdir()
    monkeypatch.chdir(root)
    output = root / 'graphify-out'
    output.mkdir()
    graph = build_from_json(_chunk('one'), root=root)
    (output / 'graph.json').write_text(json.dumps(json_graph.node_link_data(graph, edges='links')))
    (output / 'manifest.json').write_text('{}')
    baseline = tx.open_graph_snapshot(output / 'graph.json', purpose='legacy-update')
    owner = tx.begin_transaction('update', root, output=output, expected_snapshot=baseline)
    fixture = SimpleNamespace(root=root, output=output, token=tx.stage_transaction_handoff(owner))
    before = _published_bytes(output)
    _run(fixture, """
from graphify.transaction import open_update_baseline
baseline = open_update_baseline(tx.current_transaction())
assert baseline.generation is None
assert baseline.payload == (output / 'graph.json').read_bytes()
assert {n['id'] for n in baseline.data['nodes']} == {'changed', 'retained', 'anchor'}
""")
    assert _published_bytes(output) == before


def _receiptless_retry_fixture(tmp_path):
    from graphify.build import build_from_json
    from graphify.detect import save_manifest
    from networkx.readwrite import json_graph
    root = tmp_path / 'legacy-corpus'
    root.mkdir()
    output = root / 'graphify-out'
    output.mkdir()
    for name in ('changed.md', 'retained.md'):
        (root / name).write_text(name + '\n')
    graph = build_from_json(_chunk('one'), root=root)
    (output / 'graph.json').write_text(json.dumps(json_graph.node_link_data(graph, edges='links')))
    save_manifest({'document': [str(root / 'changed.md'), str(root / 'retained.md')]},
                  manifest_path=output / 'manifest.json', root=root)
    (output / 'graph.svg').write_bytes(b'original export')
    (root / 'changed.md').unlink()
    return SimpleNamespace(root=root, output=output)


def _change_legacy_inventory(output, damage):
    if damage == 'graph':
        data = json.loads((output / 'graph.json').read_bytes())
        data['nodes'].append({'id': 'unadmitted', 'source_file': 'retained.md'})
        (output / 'graph.json').write_text(json.dumps(data))
    elif damage == 'manifest':
        (output / 'manifest.json').write_text('{}')
    elif damage == 'missing-graph':
        (output / 'graph.json').unlink()
    elif damage == 'export':
        (output / 'graph.svg').write_bytes(b'changed export')
    elif damage == 'missing-export':
        (output / 'graph.svg').unlink()
    elif damage == 'added-export':
        (output / 'graph.graphml').write_bytes(b'new export')
    elif damage == 'added-callflow':
        (output / 'new-callflow.html').write_bytes(b'<footer>graphify callflow-html</footer>')
    elif damage == 'receipt':
        (output / tx.RECEIPT_FILE).write_text('{}')
    else:
        raise AssertionError(damage)


def _output_bytes(output):
    return {str(p.relative_to(output)): p.read_bytes()
            for p in output.rglob('*') if p.is_file()}


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('retry', [False, True], ids=['before-first-body', 'interrupted-retry'])
@pytest.mark.parametrize('damage', ['graph', 'manifest', 'missing-graph', 'export', 'missing-export',
                                   'added-export', 'added-callflow'])
def test_receiptless_retry_refuses_changed_admitted_inventory(tmp_path, platform, retry, damage):
    fixture = _receiptless_retry_fixture(tmp_path)
    if retry:
        first = _update_shell(fixture, platform, 'result = detect_incremental(', fail_detection=True)
        assert first.returncode != 0 and 'injected detection failure' in first.stderr
        _, token = _update_resume_assignment(first)
    else:
        snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='semantic-update')
        owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                     expected_snapshot=snapshot)
        token = tx.stage_transaction_handoff(owner).path
    _change_legacy_inventory(fixture.output, damage)
    before = _output_bytes(fixture.output)
    result = _update_shell(fixture, platform, 'result = detect_incremental(', token=token)
    assert result.returncode != 0, 'unadmitted public bytes became the retry baseline'
    assert ('graph.json' if damage == 'missing-graph' else 'legacy update inventory changed') in result.stderr
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('damage', ['graph', 'manifest', 'missing-graph', 'export', 'missing-export',
                                   'added-export', 'added-callflow'])
def test_receiptless_retry_binding_precedes_token_publication(tmp_path, damage):
    fixture = _receiptless_retry_fixture(tmp_path)
    snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='semantic-update')
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output,
                                 expected_snapshot=snapshot)
    _change_legacy_inventory(fixture.output, damage)
    before = _output_bytes(fixture.output)
    message = 'graph.json' if damage == 'missing-graph' else 'legacy update inventory changed'
    with pytest.raises(tx.PendingTransactionError, match=message):
        tx.stage_transaction_handoff(owner)
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
def test_receiptless_retry_unchanged_inventory_publishes_deletion(tmp_path, platform):
    fixture = _receiptless_retry_fixture(tmp_path)
    first = _update_shell(fixture, platform, 'result = detect_incremental(', fail_detection=True)
    assert first.returncode != 0 and 'injected detection failure' in first.stderr
    _, token = _update_resume_assignment(first)
    fixture.token = SimpleNamespace(path=token)
    before = _output_bytes(fixture.output)
    retry = _update_shell(fixture, platform, 'result = detect_incremental(', token=token)
    assert retry.returncode == 0, retry.stderr
    assert _output_bytes(fixture.output) == before
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture,
                           reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], "
         "'hyperedges': [], 'replacement_sources': []}))" + f'\nexec({merge!r})\nexec({build!r})')
    assert _output_bytes(fixture.output) == before
    _run(fixture, f'exec({finish!r})\ntx.finalize_prepared_transaction()')
    snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='completed-retry')
    assert {n['id'] for n in snapshot.data['nodes']} == {'retained', 'anchor'}
    assert set(json.loads((fixture.output / 'manifest.json').read_bytes())) == {'retained.md'}


@pytest.mark.parametrize('damage', ['missing', 'null', 'malformed', 'changed'])
def test_receiptless_retry_token_rejects_changed_live_binding(tmp_path, damage):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output)
    token = tx.stage_transaction_handoff(owner)
    path = fixture.output / tx.TRANSACTION_FILE
    record = json.loads(path.read_bytes())
    assert len(record['legacy_inventory_digest']) == 64
    if damage == 'missing':
        del record['legacy_inventory_digest']
    else:
        record['legacy_inventory_digest'] = {'null': None, 'malformed': [], 'changed': '0' * 64}[damage]
    path.write_text(json.dumps(record))
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError):
        tx.run_prepared_token(token.path, ['-c', "raise AssertionError('unbound owner entered')"])
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('field,value', [('kind', 'full'), ('generation', 2)])
def test_receiptless_retry_binding_requires_first_update_owner(tmp_path, field, value):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output)
    record = tx._transaction_json(owner, phase='building')
    record[field] = value
    before = _output_bytes(fixture.output)
    with tx.pin_output(fixture.output) as capability:
        with pytest.raises(tx.PendingTransactionError, match='legacy update inventory binding'):
            tx._transaction_from_json(capability, record)
    assert _output_bytes(fixture.output) == before


def test_receiptless_retry_without_snapshot_keeps_baseline_binding(tmp_path):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output)
    fixture.token = tx.stage_transaction_handoff(owner)
    before = _output_bytes(fixture.output)
    _run(fixture, "baseline = tx.open_update_baseline(tx.current_transaction())\n"
         "assert baseline.payload == (output / 'graph.json').read_bytes()")
    assert _published_bytes(fixture.output) == {
        'graph.json': before['graph.json'], 'manifest.json': before['manifest.json'], tx.RECEIPT_FILE: None,
    }


def test_receiptless_retry_binding_does_not_change_graphless_update(tmp_path):
    root = tmp_path / 'graphless-corpus'
    root.mkdir()
    output = root / 'graphify-out'
    owner = tx.begin_transaction('update', root, output=output)
    assert owner.legacy_inventory_digest is None
    assert 'legacy_inventory_digest' not in tx._transaction_json(owner, phase='building')
    fixture = SimpleNamespace(root=root, output=output, token=tx.stage_transaction_handoff(owner))
    _run(fixture, "assert not Path('graph.json').exists()")


@pytest.mark.parametrize('damage', ['graph', 'manifest', 'missing-graph', 'export', 'missing-export',
                                   'added-export', 'added-callflow'])
def test_receiptless_retry_binding_revalidates_snapshot_before_begin(tmp_path, damage):
    fixture = _receiptless_retry_fixture(tmp_path)
    snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='semantic-update')
    _change_legacy_inventory(fixture.output, damage)
    before = _output_bytes(fixture.output)
    # The existing snapshot guard reports a removed graph as FileNotFoundError.
    refusal = (tx.PendingTransactionError, FileNotFoundError) if damage == 'missing-graph' else tx.PendingTransactionError
    with pytest.raises(refusal):
        tx.begin_transaction('update', fixture.root, output=fixture.output,
                             expected_snapshot=snapshot)
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('changed', [False, True])
def test_receiptless_retry_takeover_keeps_immutable_baseline_binding(tmp_path, changed):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    old_token = tx.stage_transaction_handoff(owner)
    path = fixture.output / tx.TRANSACTION_FILE
    if changed:
        record = json.loads(path.read_bytes())
        record['legacy_inventory_digest'] = '0' * 64
        path.write_text(json.dumps(record))
        before = _output_bytes(fixture.output)
        with pytest.raises(tx.PendingTransactionError, match='token changed'):
            tx.takeover_drainer(fixture.output, now=100)
        assert _output_bytes(fixture.output) == before
        return
    tx.takeover_drainer(fixture.output, now=100)
    with pytest.raises(tx.PendingTransactionError):
        tx.run_token(old_token.path, ['-c', 'pass'])
    fixture.token = SimpleNamespace(path=tx.active_transaction_token_path(fixture.output))
    _run(fixture, "baseline = tx.open_update_baseline(tx.current_transaction())\n"
         "assert baseline.payload == (output / 'graph.json').read_bytes()")
    _change_legacy_inventory(fixture.output, 'graph')
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='legacy update inventory changed'):
        _run(fixture, 'tx.open_update_baseline(tx.current_transaction())')
    assert _output_bytes(fixture.output) == before


def test_receiptless_retry_old_pending_binding_is_not_inferred(tmp_path):
    from dataclasses import replace
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output)
    # Reproduce the prior live/token shape, which carried no baseline digest.
    old_owner = replace(owner, legacy_inventory_digest=None)
    with tx.pin_output(fixture.output) as capability, tx._locked(capability):
        tx._write_transaction(capability, old_owner)
    tx._AUTHORITY.set(tx._authority_for(old_owner))
    token = tx.stage_transaction_handoff(old_owner)
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='inventory binding is missing'):
        tx.run_prepared_token(token.path, ['-c', "raise AssertionError('unbound baseline entered')"])
    assert _output_bytes(fixture.output) == before



def _recover_update_fixture(fixture, entrypoint='direct', *, failpoint=None):
    with tx.pin_output(fixture.output) as capability:
        pending = tx._read_pending_transition(capability)
        owner = pending[3] if pending is not None else tx._read_transaction(capability)
        protocol = tx._read_protocol(capability)
        identity = capability.identity
    selectors = dict(expected_output_identity=identity,
                     expected_generation=protocol['generation'] if owner is None else owner.generation,
                     expected_transaction_id=None if owner is None else owner.id)
    if entrypoint == 'cli':
        command = [os.sys.executable, '-B', '-m', 'graphify', 'transaction', 'recover',
                   '--root', str(fixture.root), '--output', str(fixture.output),
                   '--generation', str(selectors['expected_generation']),
                   '--device', str(identity.device), '--inode', str(identity.inode)]
        if owner is not None:
            command += ['--transaction-id', owner.id]
        result = subprocess.run(command, cwd=fixture.root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        handoff = json.loads(result.stdout)
        fixture.token = SimpleNamespace(path=Path(handoff['token_path']))
        return tx.resume_transaction(handoff['transaction_id'], fixture.root, output=fixture.output)
    if entrypoint == 'direct':
        return tx.recover_transaction('update', fixture.root, output=fixture.output, now=10**12,
                                      transition_failpoint=failpoint, **selectors)
    return tx.recover_selected_transaction('update', fixture.root, output=fixture.output,
                                           now=10**12, **selectors)


@pytest.mark.parametrize('entrypoint', ['direct', 'selected', 'cli'])
@pytest.mark.parametrize('legacy', [False, True], ids=['certified', 'receiptless'])
def test_update_recovery_preserves_admitted_baseline(tmp_path, monkeypatch, entrypoint, legacy):
    fixture = _receiptless_retry_fixture(tmp_path) if legacy else _baseline(tmp_path, monkeypatch)
    if not legacy:
        (fixture.root / 'changed.md').unlink()
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    old_token = tx.stage_transaction_handoff(owner)
    fixture.token = old_token
    live = tx.resume_transaction(owner.id, fixture.root, output=fixture.output)
    expected = dict(tx.open_update_baseline(live).artifacts)
    _run(fixture, "tx.commit_prepared_bytes(tx.current_transaction(), 'failed-attempt.txt', b'discard me')")
    recovered = _recover_update_fixture(fixture, entrypoint)
    assert recovered.generation == owner.generation
    assert recovered.legacy_inventory_digest == owner.legacy_inventory_digest
    assert recovered.drainer.claim_epoch == owner.drainer.claim_epoch + 1
    assert recovered.id != owner.id
    assert all((fixture.output / name).read_bytes() == payload for name, payload in expected.items())
    with pytest.raises(tx.PendingTransactionError):
        tx.run_token(old_token.path, ['-c', 'pass'])
    if entrypoint != 'cli':
        fixture.token = tx.stage_transaction_handoff(recovered)
    _run(fixture, f"assert dict(tx.open_update_baseline(tx.current_transaction()).artifacts) == {expected!r}\n"
         "assert not Path('failed-attempt.txt').exists()")


@pytest.mark.parametrize('platform,entrypoint', [('claude', 'direct'), ('aider', 'selected'), ('devin', 'cli')])
def test_receiptless_recovery_consumers_publish_same_baseline(tmp_path, platform, entrypoint):
    from graphify.detect import detect_incremental
    fixture = _receiptless_retry_fixture(tmp_path)
    graph_path = fixture.output / 'graph.json'
    legacy_data = json.loads(graph_path.read_bytes())
    # NetworkX's node-link fixture nests graph attributes. Public Graphify JSON
    # carries hyperedges at the top level, where build_merge consumes them.
    legacy_data['hyperedges'] = legacy_data['graph'].pop('hyperedges')
    legacy_data['links'] = [{k: v for k, v in edge.items() if k not in ('_src', '_tgt')}
                            | {'confidence_score': 1.0} for edge in legacy_data['links']]
    graph_path.write_text(json.dumps(legacy_data))
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    tx.stage_transaction_handoff(owner)
    recovered = _recover_update_fixture(fixture, entrypoint)
    if entrypoint != 'cli':
        fixture.token = tx.stage_transaction_handoff(recovered)
    retry = _update_shell(fixture, platform, 'result = detect_incremental(', token=fixture.token.path)
    assert retry.returncode == 0, retry.stderr
    if platform == 'claude':
        population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
        _run(fixture, f'exec({population!r})')
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture,
                           reference=platform == 'claude')
    build = _deletion_code(platform, 'wrote = to_json(', fixture)
    finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
    _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], "
         "'hyperedges': [], 'replacement_sources': []}))" +
         f'\nexec({merge!r})\nexec({build!r})\nexec({finish!r})\ntx.finalize_prepared_transaction()')
    snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='recovered-update')
    assert snapshot.generation == 1
    assert {n['id'] for n in snapshot.data['nodes']} == {'retained', 'anchor'}
    assert snapshot.data['links'] == legacy_data['links']
    assert {h['id'] for h in snapshot.data['hyperedges']} == {'retained-group'}
    assert set(json.loads((fixture.output / 'manifest.json').read_bytes())) == {'retained.md'}
    again = detect_incremental(fixture.root, manifest_path=fixture.output / 'manifest.json',
                               google_workspace=False)
    assert again['new_total'] == 0 and again['deleted_files'] == []


@pytest.mark.parametrize('boundary', ['bootstrap', 'after_transition_record', 'after_owner_protocol',
                                     'after_transaction', 'after_drainer_reserved',
                                     'after_drainer_launching', 'after_drainer_claimed'])
def test_receiptless_begin_recovery_keeps_baseline(tmp_path, boundary):
    fixture = _receiptless_retry_fixture(tmp_path)
    expected = {name: (fixture.output / name).read_bytes()
                for name in ('graph.json', 'manifest.json', 'graph.svg')}

    def stop(*_args):
        raise RuntimeError('interrupted begin')

    def transition(phase):
        if phase == boundary:
            stop()

    with pytest.raises(RuntimeError, match='interrupted begin'):
        tx.begin_transaction('update', fixture.root, output=fixture.output, now=0,
                             failpoint=stop if boundary == 'bootstrap' else None,
                             transition_failpoint=transition)
    recovered = _recover_update_fixture(fixture)
    assert recovered.generation == 1 and recovered.legacy_inventory_digest is not None
    fixture.token = tx.stage_transaction_handoff(recovered)
    _run(fixture, f"assert dict(tx.open_update_baseline(tx.current_transaction()).artifacts) == {expected!r}")
    protocol = json.loads((fixture.output / tx.PROTOCOL_FILE).read_bytes())
    assert 'legacy_inventory_digest' not in protocol


@pytest.mark.parametrize('boundary', ['after_transition_record', 'after_recovery_protocol',
                                     'after_transaction', 'after_drainer_reserved',
                                     'after_drainer_launching', 'after_drainer_claimed'])
def test_receiptless_claimed_recovery_replays_bound_transition(tmp_path, boundary):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    tx.stage_transaction_handoff(owner)

    def stop(phase):
        if phase == boundary:
            raise RuntimeError('interrupted recovery')

    with pytest.raises(RuntimeError, match='interrupted recovery'):
        _recover_update_fixture(fixture, failpoint=stop)
    recovered = _recover_update_fixture(fixture, 'selected')
    assert recovered.generation == owner.generation
    assert recovered.legacy_inventory_digest == owner.legacy_inventory_digest
    fixture.token = tx.stage_transaction_handoff(recovered)
    _run(fixture, "assert tx.open_update_baseline(tx.current_transaction()).payload == (output / 'graph.json').read_bytes()")


@pytest.mark.parametrize('phase', ['bootstrap', 'pending', 'claimed'])
@pytest.mark.parametrize('damage', ['graph', 'manifest', 'missing-graph', 'export', 'missing-export',
                                   'added-export', 'added-callflow', 'receipt'])
def test_receiptless_recovery_rejects_changed_inventory_before_mutation(tmp_path, phase, damage):
    fixture = _receiptless_retry_fixture(tmp_path)

    def stop(*_args):
        raise RuntimeError('interrupt')

    if phase == 'claimed':
        owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
        fixture.token = tx.stage_transaction_handoff(owner)
        _run(fixture, "tx.commit_prepared_bytes(tx.current_transaction(), 'failed-attempt.txt', b'keep on refusal')")
    else:
        with pytest.raises(RuntimeError, match='interrupt'):
            tx.begin_transaction('update', fixture.root, output=fixture.output, now=0,
                                 failpoint=stop if phase == 'bootstrap' else None,
                                 transition_failpoint=stop if phase == 'pending' else None)
    _change_legacy_inventory(fixture.output, damage)
    before = _output_bytes(fixture.output)
    workspaces = {p.name: _output_bytes(p) for p in fixture.output.parent.glob('.graphify-prepare-*')}
    with pytest.raises(tx.PendingTransactionError, match='inventory|artifact'):
        _recover_update_fixture(fixture)
    assert _output_bytes(fixture.output) == before
    assert {p.name: _output_bytes(p) for p in fixture.output.parent.glob('.graphify-prepare-*')} == workspaces


@pytest.mark.parametrize('field,value', [('legacy_inventory_digest', None),
                                       ('legacy_inventory_digest', 'bad'),
                                       ('kind', 'full'), ('generation', 2)])
def test_receiptless_bootstrap_recovery_rejects_malformed_binding(tmp_path, field, value):
    fixture = _receiptless_retry_fixture(tmp_path)

    def stop(*_args):
        raise RuntimeError('bootstrap')

    with pytest.raises(RuntimeError, match='bootstrap'):
        tx.begin_transaction('update', fixture.root, output=fixture.output, now=0, failpoint=stop)
    path = fixture.output / tx.PROTOCOL_FILE
    raw = json.loads(path.read_bytes())
    raw[field] = value
    path.write_text(json.dumps(raw))
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='legacy bootstrap inventory binding'):
        tx.recover_transaction('update', fixture.root, output=fixture.output, now=100)
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('state', ['INCOMPLETE', 'COMPLETE'])
def test_legacy_bootstrap_binding_cannot_extend_later_protocol_shapes(tmp_path, state):
    fixture = _receiptless_retry_fixture(tmp_path)
    tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    raw = json.loads((fixture.output / tx.PROTOCOL_FILE).read_bytes())
    raw.update(state=state, legacy_inventory_digest='0' * 64)
    if state == 'COMPLETE':
        raw['receipt_digest'] = '0' * 64
    before = _output_bytes(fixture.output)
    with tx.pin_output(fixture.output) as capability:
        with pytest.raises(tx.PendingTransactionError, match='malformed protocol state'):
            tx._protocol_from_json(capability, raw)
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('phase', ['bootstrap', 'claimed'])
def test_receiptless_recovery_does_not_infer_missing_binding(tmp_path, phase):
    from dataclasses import replace
    fixture = _receiptless_retry_fixture(tmp_path)
    if phase == 'claimed':
        owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
        old_owner = replace(owner, legacy_inventory_digest=None)
        with tx.pin_output(fixture.output) as capability, tx._locked(capability):
            tx._write_transaction(capability, old_owner)
        tx._AUTHORITY.set(tx._authority_for(old_owner))
        tx.stage_transaction_handoff(old_owner)
    else:
        def stop(*_args):
            raise RuntimeError('bootstrap')
        with pytest.raises(RuntimeError, match='bootstrap'):
            tx.begin_transaction('update', fixture.root, output=fixture.output, now=0, failpoint=stop)
        path = fixture.output / tx.PROTOCOL_FILE
        raw = json.loads(path.read_bytes())
        raw.pop('legacy_inventory_digest')
        path.write_text(json.dumps(raw))
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='inventory binding is missing'):
        _recover_update_fixture(fixture)
    assert _output_bytes(fixture.output) == before


@pytest.mark.parametrize('damage', ['missing', 'changed'])
@pytest.mark.parametrize('phase', ['bootstrap', 'claimed'])
def test_receiptless_recovery_refuses_transition_binding_substitution(tmp_path, phase, damage):
    fixture = _receiptless_retry_fixture(tmp_path)

    def stop(*_args):
        raise RuntimeError('transition')

    if phase == 'claimed':
        owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
        tx.stage_transaction_handoff(owner)
        with pytest.raises(RuntimeError, match='transition'):
            _recover_update_fixture(fixture, failpoint=stop)
    else:
        with pytest.raises(RuntimeError, match='transition'):
            tx.begin_transaction('update', fixture.root, output=fixture.output, now=0,
                                 transition_failpoint=stop)
    path = fixture.output / tx.TRANSITION_FILE
    raw = json.loads(path.read_bytes())
    successor = raw['successor_transaction']
    if damage == 'missing':
        successor.pop('legacy_inventory_digest')
    else:
        successor['legacy_inventory_digest'] = '0' * 64
    path.write_text(json.dumps(raw))
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='transition legacy inventory binding changed'):
        _recover_update_fixture(fixture)
    assert _output_bytes(fixture.output) == before


def test_receiptless_recovery_accepts_older_bound_pending_transition(tmp_path):
    fixture = _receiptless_retry_fixture(tmp_path)

    def stop(*_args):
        raise RuntimeError('transition')

    with pytest.raises(RuntimeError, match='transition'):
        tx.begin_transaction('update', fixture.root, output=fixture.output, now=0,
                             transition_failpoint=stop)
    protocol_path = fixture.output / tx.PROTOCOL_FILE
    transition_path = fixture.output / tx.TRANSITION_FILE
    protocol = json.loads(protocol_path.read_bytes())
    transition = json.loads(transition_path.read_bytes())
    # The previous format already bound its pending successor, but omitted the
    # digest on the pre-owner bootstrap protocol. Preserve that exact replay.
    protocol.pop('legacy_inventory_digest')
    transition['predecessor_protocol'].pop('legacy_inventory_digest')
    protocol_path.write_text(json.dumps(protocol))
    transition_path.write_text(json.dumps(transition))
    recovered = _recover_update_fixture(fixture)
    assert recovered.generation == 1 and recovered.legacy_inventory_digest is not None
    fixture.token = tx.stage_transaction_handoff(recovered)
    _run(fixture, "assert tx.open_update_baseline(tx.current_transaction()).payload == (output / 'graph.json').read_bytes()")


@pytest.mark.parametrize('kind', ['full', 'update'])
@pytest.mark.parametrize('bootstrap', [False, True], ids=['claimed', 'bootstrap'])
def test_graphless_recovery_keeps_existing_generation_rules(tmp_path, kind, bootstrap):
    root = tmp_path / 'empty-corpus'
    root.mkdir()
    output = root / 'graphify-out'

    def stop(*_args):
        raise RuntimeError('bootstrap')

    if bootstrap:
        with pytest.raises(RuntimeError, match='bootstrap'):
            tx.begin_transaction(kind, root, output=output, now=0, failpoint=stop)
    else:
        tx.begin_transaction(kind, root, output=output, now=0)
    recovered = tx.recover_transaction(kind, root, output=output, now=100)
    assert recovered.generation == (1 if bootstrap else 2)
    assert recovered.legacy_inventory_digest is None
    assert 'legacy_inventory_digest' not in tx._transaction_json(recovered, phase='building')
    assert 'legacy_inventory_digest' not in json.loads((output / tx.PROTOCOL_FILE).read_bytes())


def test_receiptless_recovery_rotates_claims_without_advancing_publication(tmp_path):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    tx.stage_transaction_handoff(owner)
    for epoch in range(1, 4):
        recovered = tx.recover_transaction('update', fixture.root, output=fixture.output,
                                            now=epoch * 100)
        assert recovered.generation == 1 and recovered.drainer.claim_epoch == epoch
        assert recovered.legacy_inventory_digest == owner.legacy_inventory_digest
        fixture.token = tx.stage_transaction_handoff(recovered)
        _run(fixture, "assert tx.open_update_baseline(tx.current_transaction()).payload == (output / 'graph.json').read_bytes()")
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.RecoverableTransactionError, match='attempt bound'):
        tx.recover_transaction('update', fixture.root, output=fixture.output, now=400)
    assert _output_bytes(fixture.output) == before


def test_receiptless_recovery_cannot_drop_binding_by_changing_kind(tmp_path):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    tx.stage_transaction_handoff(owner)
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='recovery kind changed'):
        tx.recover_transaction('full', fixture.root, output=fixture.output, now=100)
    assert _output_bytes(fixture.output) == before


def test_receiptless_recovery_without_handoff_keeps_native_baseline(tmp_path):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    expected = dict(tx.open_update_baseline(owner).artifacts)
    recovered = tx.recover_transaction('update', fixture.root, output=fixture.output, now=100)
    assert recovered.generation == 1
    assert recovered.legacy_inventory_digest == owner.legacy_inventory_digest
    assert dict(tx.open_update_baseline(recovered).artifacts) == expected


@pytest.mark.parametrize('entrypoint', ['selected', 'takeover', 'cli'])
def test_receiptless_recovery_entrypoints_preserve_rejected_workspace(tmp_path, entrypoint):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    fixture.token = tx.stage_transaction_handoff(owner)
    _run(fixture, "tx.commit_prepared_bytes(tx.current_transaction(), 'failed-attempt.txt', b'preserve me')")
    _change_legacy_inventory(fixture.output, 'graph')
    before = _output_bytes(fixture.output)
    workspaces = {p.name: _output_bytes(p) for p in fixture.output.parent.glob('.graphify-prepare-*')}
    if entrypoint == 'cli':
        identity = owner.output_identity
        result = subprocess.run(
            [os.sys.executable, '-B', '-m', 'graphify', 'transaction', 'recover',
             '--root', str(fixture.root), '--output', str(fixture.output),
             '--generation', str(owner.generation), '--transaction-id', owner.id,
             '--device', str(identity.device), '--inode', str(identity.inode)],
            cwd=fixture.root, capture_output=True, text=True,
        )
        assert result.returncode != 0 and 'legacy update inventory changed' in result.stderr
    else:
        with pytest.raises(tx.PendingTransactionError, match='legacy update inventory changed'):
            if entrypoint == 'takeover':
                tx.takeover_drainer(fixture.output, now=100)
            else:
                _recover_update_fixture(fixture, entrypoint)
    assert _output_bytes(fixture.output) == before
    assert {p.name: _output_bytes(p) for p in fixture.output.parent.glob('.graphify-prepare-*')} == workspaces


def test_receiptless_partial_publication_keeps_existing_finalization_path(tmp_path):
    fixture = _receiptless_retry_fixture(tmp_path)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output, now=0)
    fixture.token = tx.stage_transaction_handoff(owner)
    with pytest.raises(RuntimeError, match='receipt committed'):
        _run(fixture, """
live = tx.current_transaction()
data = json.loads(Path('graph.json').read_bytes())
data['graph'][tx.GRAPH_WATERMARK_KEY] = {
    'schema': 1, 'protocol_epoch': 1, 'generation': live.generation, 'state': 'active'}
def interrupt(phase):
    if phase == 'after_receipt':
        raise RuntimeError('receipt committed')
tx._commit_publication_plan(live, tx.PublicationPlan({
    'graph.json': json.dumps(data).encode(), 'manifest.json': Path('manifest.json').read_bytes()}),
    failpoint=interrupt)
""")
    before = _output_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='inventory acquired receipt authority'):
        _recover_update_fixture(fixture)
    assert _output_bytes(fixture.output) == before
    # The pending receipt does not authorize a new baseline or replacement owner.
    # Its original exact token can still finish the already prepared publication.
    _run(fixture, 'tx.finalize_prepared_transaction()')
    snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='completed-first-publication')
    assert snapshot.generation == 1


def test_update_baseline_refuses_actual_full_owner(tmp_path, monkeypatch):
    fixture = _baseline(tmp_path, monkeypatch)
    fixture.owner = tx.begin_transaction('full', fixture.root, output=fixture.output,
                                         expected_snapshot=fixture.baseline)
    fixture.token = tx.stage_transaction_handoff(fixture.owner)
    before = _published_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='requires an update owner'):
        _run(fixture, 'tx.open_update_baseline(tx.current_transaction())')
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('manifest', [{'files': ['../evidence.txt']}, {'files': [42]}, {'bad': []}])
def test_empty_prune_refuses_malformed_managed_vault_manifest(tmp_path, monkeypatch, platform, manifest):
    fixture = _deletion_fixture(tmp_path, monkeypatch, platform_key=platform, empty=True)
    before = _published_bytes(fixture.output)
    merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture,
                           reference=platform == 'claude')
    _run(fixture, f"tx.commit_prepared_bytes(tx.current_transaction(), 'vault/.graphify_obsidian_manifest.json', {json.dumps(manifest).encode()!r})")
    with pytest.raises(ValueError, match='managed Obsidian manifest'):
        _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
             + f'\nexec({merge!r})')
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('cap', [10, 1024 * 1024 * 1024])
def test_update_baseline_applies_graph_limit_before_payload_read(tmp_path, monkeypatch, cap):
    fixture = _prepare(_baseline(tmp_path, monkeypatch))
    before = _published_bytes(fixture.output)
    original = tx._hash_relative_bytes
    seen = []
    def observed(capability, name, **options):
        if name == 'graph.json' and capability.path == fixture.output:
            seen.append(options['retain_limit'])
        return original(capability, name, **options)
    monkeypatch.setattr(tx, '_hash_relative_bytes', observed)
    monkeypatch.setenv('GRAPHIFY_MAX_GRAPH_BYTES', str(cap))
    if cap == 10:
        with pytest.raises(tx.PendingTransactionError, match='read limit'):
            _run(fixture, 'tx.open_update_baseline(tx.current_transaction())')
    else:
        _run(fixture, 'tx.open_update_baseline(tx.current_transaction())')
    assert seen == [min(cap, 512 * 1024 * 1024)]
    assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('preserved', [False, True])
@pytest.mark.parametrize('deleted', [False, True])
@pytest.mark.parametrize('separate', [False, True])
def test_missing_completed_drainer_update_consumers(tmp_path, monkeypatch, platform, preserved, deleted, separate):
    fixture = _baseline(tmp_path, monkeypatch, separate=separate)
    (fixture.output / tx.DRAINER_FILE).unlink()
    if not preserved:
        (fixture.output / tx.PREDECESSOR_FILE).unlink(missing_ok=True)
    if deleted:
        (fixture.root / 'changed.md').unlink()
    before = _published_bytes(fixture.output)
    with pytest.raises(tx.PendingTransactionError, match='drainer'):
        tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='ordinary-reader')
    result = _update_shell(fixture, platform, 'result = detect_incremental(')
    assert result.returncode == 0, result.stdout + result.stderr
    assert _published_bytes(fixture.output) == before
    if deleted:
        _, token = _update_resume_assignment(result)
        fixture.token = SimpleNamespace(path=token)
        if platform == 'claude':
            population = _deletion_code(platform, 'r = json.loads(', fixture, reference=True)
            _run(fixture, f'exec({population!r})')
        merge = _deletion_code(platform, '# Load new extraction and incremental state', fixture,
                               reference=platform == 'claude')
        build = _deletion_code(platform, 'wrote = to_json(', fixture)
        finish = _deletion_code(platform, '# Update cumulative cost tracker', fixture)
        _run(fixture, "Path('.graphify_extract.json').write_text(json.dumps({'nodes': [], 'edges': [], 'hyperedges': [], 'replacement_sources': []}))"
             + f'\nexec({merge!r})\nexec({build!r})')
        assert _published_bytes(fixture.output) == before
        _run(fixture, f'exec({finish!r})\ntx.finalize_prepared_transaction()')
    else:
        assert 'Nothing to update' in result.stdout + result.stderr
        assert tx.transaction_status(fixture.output)['transaction'] is None
    current = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='recovered-update')
    assert current.generation == fixture.baseline.generation + int(deleted)
    assert {n['id'] for n in current.data['nodes']} == ({'retained', 'anchor'} if deleted else {'changed', 'retained', 'anchor'})
    if deleted:
        assert set(json.loads((fixture.output / 'manifest.json').read_bytes())) == {'retained.md'}
    else:
        assert _published_bytes(fixture.output) == before


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('damage', ['graph', 'manifest', 'receipt', 'protocol', 'watermark', 'predecessor'])
def test_missing_completed_drainer_refuses_corrupt_admission(tmp_path, monkeypatch, platform, damage):
    fixture = _baseline(tmp_path, monkeypatch)
    (fixture.output / tx.DRAINER_FILE).unlink()
    (fixture.output / tx.PREDECESSOR_FILE).unlink(missing_ok=True)
    name = {'graph': 'graph.json', 'manifest': 'manifest.json', 'receipt': tx.RECEIPT_FILE,
            'protocol': tx.PROTOCOL_FILE, 'watermark': 'graph.json', 'predecessor': tx.PREDECESSOR_FILE}[damage]
    path = fixture.output / name
    if damage == 'predecessor':
        path.write_text('{}')
    elif damage == 'watermark':
        data = json.loads(path.read_bytes())
        data['graph'][tx.GRAPH_WATERMARK_KEY]['generation'] += 1
        path.write_text(json.dumps(data))
    else:
        path.write_bytes(path.read_bytes() + b' ')
        if damage == 'protocol':
            data = json.loads(path.read_bytes())
            data['state'] = 'INCOMPLETE'
            path.write_text(json.dumps(data))
    before = {str(p.relative_to(fixture.output)): p.read_bytes()
              for p in fixture.output.rglob('*') if p.is_file()}
    result = _update_shell(fixture, platform, 'result = detect_incremental(')
    assert result.returncode != 0, result.stdout + result.stderr
    assert {str(p.relative_to(fixture.output)): p.read_bytes()
            for p in fixture.output.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('damage', ['graph', 'receipt-identity', 'drainer', 'live'])
def test_missing_completed_drainer_snapshot_is_revalidated_before_begin(tmp_path, monkeypatch, damage):
    fixture = _baseline(tmp_path, monkeypatch)
    drainer = (fixture.output / tx.DRAINER_FILE).read_bytes()
    (fixture.output / tx.DRAINER_FILE).unlink()
    snapshot = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='semantic-update')
    if damage == 'graph':
        (fixture.output / 'graph.json').write_text('{}')
    elif damage == 'receipt-identity':
        receipt = fixture.output / tx.RECEIPT_FILE
        temporary = fixture.output / 'replacement-receipt'
        temporary.write_bytes(receipt.read_bytes())
        temporary.replace(receipt)
    elif damage == 'drainer':
        data = json.loads(drainer)
        data['launch_nonce'] = '0' * 32
        (fixture.output / tx.DRAINER_FILE).write_text(json.dumps(data))
    else:
        (fixture.output / tx.TRANSACTION_FILE).write_text('{}')
    before = {str(p.relative_to(fixture.output)): p.read_bytes()
              for p in fixture.output.rglob('*') if p.is_file()}
    with pytest.raises(tx.PendingTransactionError):
        tx.begin_transaction('update', fixture.root, output=fixture.output, expected_snapshot=snapshot)
    assert {str(p.relative_to(fixture.output)): p.read_bytes()
            for p in fixture.output.rglob('*') if p.is_file()} == before


def test_missing_completed_drainer_native_update_preserves_baseline(tmp_path, monkeypatch):
    fixture = _baseline(tmp_path, monkeypatch)
    (fixture.output / tx.DRAINER_FILE).unlink()
    (fixture.output / tx.PREDECESSOR_FILE).unlink(missing_ok=True)
    before = _published_bytes(fixture.output)
    owner = tx.begin_transaction('update', fixture.root, output=fixture.output)
    fixture.token = tx.stage_transaction_handoff(owner)
    _run(fixture, """
baseline = tx.open_update_baseline(tx.current_transaction())
assert baseline.generation == 1
assert baseline.payload == (output / 'graph.json').read_bytes()
assert baseline.manifest_payload == (output / 'manifest.json').read_bytes()
tx.cancel_unpublished_transaction(tx.current_transaction())
""")
    assert _published_bytes(fixture.output) == before
    assert tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='ordinary-reader').generation == 1


@pytest.mark.parametrize('platform', ['claude', 'aider', 'devin'])
@pytest.mark.parametrize('legacy', [False, True])
def test_oversized_semantic_update_snapshot_refuses_before_handoff(tmp_path, monkeypatch, platform, legacy):
    import shlex
    import sys
    from dataclasses import replace
    fixture = _baseline(tmp_path, monkeypatch)
    if legacy:
        data = json.loads((fixture.output / 'graph.json').read_bytes())
        data['graph'].pop(tx.GRAPH_WATERMARK_KEY)
        (fixture.output / 'graph.json').write_text(json.dumps(data))
        for name in (tx.PROTOCOL_FILE, tx.RECEIPT_FILE, tx.DRAINER_FILE, tx.PREDECESSOR_FILE):
            (fixture.output / name).unlink(missing_ok=True)
        fixture.baseline = tx.open_graph_snapshot(fixture.output / 'graph.json', purpose='legacy-size')
    class Oversize(bytes):
        def __len__(self):
            return 512 * 1024 * 1024 + 1
    snapshot = replace(fixture.baseline, payload=Oversize(fixture.baseline.payload))
    block, _ = _deletion_stage(platform, 'result = detect_incremental(', reference=platform == 'claude')
    command = block[block.index('GRAPHIFY_TRANSACTION_TOKEN=$(') + len('GRAPHIFY_TRANSACTION_TOKEN=$('):]
    handoff = shlex.split(command[:command.index(' INPUT_PATH)')])[5]
    assert handoff.startswith('import os, shlex, sys')
    # A size-only double avoids allocating >512MiB; execute actual generated
    # handoff code while keeping native output/ownership checks in place.
    monkeypatch.setattr(tx, 'open_graph_snapshot', lambda *a, **k: snapshot)
    monkeypatch.setattr(sys, 'argv', ['-c', str(fixture.root)])
    before = {str(p.relative_to(fixture.output)): p.read_bytes()
              for p in fixture.output.rglob('*') if p.is_file()}
    with pytest.raises(tx.PendingTransactionError, match='prepared read limit before handoff'):
        exec(handoff, {})
    assert {str(p.relative_to(fixture.output)): p.read_bytes()
            for p in fixture.output.rglob('*') if p.is_file()} == before
