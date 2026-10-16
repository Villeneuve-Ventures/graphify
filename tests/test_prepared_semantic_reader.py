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
    direct = {'nodes': [{'id': 'fact', 'source_file': 'changed.md'}], 'edges': [], 'hyperedges': [],
              'completed_sources': [str(fixture.root / 'changed.md')], 'input_tokens': 1, 'output_tokens': 2}
    _run(fixture, f"""
Path('.graphify_completed_chunks.json').write_text('[]')
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
