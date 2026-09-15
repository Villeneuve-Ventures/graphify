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


def _baseline(tmp_path, monkeypatch, *, separate=False, accepted_chunk=None):
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
graph = (build_from_json({accepted_chunk!r}, root=root) if {accepted_chunk is not None!r}
         else build([{_chunk('one')!r}], root=root))
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
