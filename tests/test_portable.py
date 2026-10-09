from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from graphify import portable
from graphify.transaction import GRAPH_WATERMARK_KEY, PendingTransactionError, open_graph_snapshot


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def graph() -> dict:
    return {"directed": False, "multigraph": False, "graph": {},
            "nodes": [{"id": "hello", "label": "Hello"}], "links": []}


def commit(root: Path) -> None:
    git(root, "add", "--all")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "main.py").write_text("def hello():\n    return 1\n")
    (root / "readme.txt").write_text("not a selected source")
    commit(root)
    _, records = portable.source_records_from_tree(root, "graphify-out")
    output = root / "graphify-out"
    output.mkdir()
    for name, payload in portable.make_bundle(graph(), records, "graphify-out").items():
        (output / name).write_bytes(payload)
    commit(root)
    return root


def payloads(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (root / "graphify-out").iterdir()}


def refresh_envelope(bundle: dict[str, bytes]) -> None:
    envelope = portable.parse_json(bundle[portable.PORTABLE_FILE])
    for record in envelope["artifacts"]:
        record["size"] = len(bundle[record["path"]])
        record["sha256"] = hashlib.sha256(bundle[record["path"]]).hexdigest()
    envelope["source_projection_digest"] = hashlib.sha256(bundle["manifest.json"]).hexdigest()
    del envelope["content_id"]
    envelope["content_id"] = hashlib.sha256(
        b"graphify.portable.v1\0" + portable.canonical_json(envelope)).hexdigest()
    bundle[portable.PORTABLE_FILE] = portable.canonical_json(envelope)


def state(root: Path) -> dict[str, tuple[int, bytes]]:
    return {p.relative_to(root).as_posix(): (p.lstat().st_mode, p.read_bytes())
            for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}


def test_fresh_clone_portable_read_preserves_all_output_and_index(repository, tmp_path):
    clone = tmp_path / "relocated-clone"
    git(repository, "clone", "-q", "--no-local", str(repository), str(clone))
    before, index = state(clone), (clone / ".git/index").read_bytes()
    snapshot = portable.open_portable_graph_snapshot(clone, "graphify-out")
    original = portable.open_portable_graph_snapshot(repository, "graphify-out")
    assert snapshot.content_id == original.content_id
    assert snapshot.revision == git(clone, "rev-parse", "HEAD").decode().strip()
    assert snapshot.data["nodes"][0]["id"] == "hello"
    data = snapshot.data
    data["nodes"].clear()
    assert snapshot.data["nodes"]
    with pytest.raises(TypeError):
        snapshot.artifacts["graph.json"] = b"changed"
    assert state(clone) == before
    assert (clone / ".git/index").read_bytes() == index
    assert git(clone, "status", "--porcelain") == b""


def test_working_sources_never_establish_projection(repository):
    (repository / "main.py").write_text("dirty bytes")
    (repository / "untracked.py").write_text("untracked bytes")
    snapshot = portable.open_portable_graph_snapshot(repository, "graphify-out")
    assert snapshot.data["nodes"]


@pytest.mark.parametrize("change", ["omitted", "added", "modified", "mode"])
def test_complete_committed_source_projection_is_required(repository, change):
    if change == "omitted":
        (repository / "main.py").unlink()
    elif change == "added":
        (repository / "extra.py").write_text("print(1)")
    elif change == "modified":
        (repository / "main.py").write_text("print(2)")
    else:
        os.chmod(repository / "main.py", 0o755)
        git(repository, "update-index", "--chmod=+x", "main.py")
    commit(repository)
    before = state(repository)
    with pytest.raises(PendingTransactionError, match="source projection"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")
    assert state(repository) == before


@pytest.mark.parametrize("name", [".graphify_protocol.json", ".graphify_generation.json",
                                  ".graphify_prepared.json", ".graphify_rebuild_queue.jsonl",
                                  ".graphify_transaction_token.token", ".graphify-prepare-x"])
def test_any_local_coordination_refuses_without_mutation(repository, name):
    path = repository / "graphify-out" / name
    if name.startswith(".graphify-prepare"):
        path.mkdir()
    else:
        path.write_bytes(b"malformed or foreign")
    before = state(repository)
    with pytest.raises(PendingTransactionError, match="mixed local authority"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")
    assert state(repository) == before


@pytest.mark.parametrize("kind", ["missing", "extra", "tampered", "executable", "symlink"])
def test_working_artifact_closure_refuses_unsafe_inventory(repository, kind):
    output = repository / "graphify-out"
    if kind == "missing":
        (output / "manifest.json").unlink()
    elif kind == "extra":
        (output / "GRAPH_REPORT.md").write_text("unsupported profile")
    elif kind == "tampered":
        (output / "graph.json").write_bytes(b"{}")
    elif kind == "executable":
        os.chmod(output / "graph.json", 0o755)
    else:
        (output / "graph.json").unlink()
        (output / "graph.json").symlink_to(repository / "main.py")
    with pytest.raises(PendingTransactionError):
        portable.open_portable_graph_snapshot(repository, "graphify-out")


def test_committed_extra_artifacts_refuse(repository):
    (repository / "graphify-out/report.txt").write_text("unsupported")
    commit(repository)
    with pytest.raises(PendingTransactionError, match="exact closure"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")


def test_output_alias_refuses(repository):
    (repository / "alias").symlink_to(repository / "graphify-out", target_is_directory=True)
    with pytest.raises(PendingTransactionError):
        portable.open_portable_graph_snapshot(repository, "alias")
    with pytest.raises(PendingTransactionError):
        portable.open_portable_graph_snapshot(repository, "graphify-out/../graphify-out")


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
                                 b'{"a":"\\ud800"}', b'\xef\xbb\xbf{}', b'\xff',
                                 b'{"a":9223372036854775808}', b'[' * 70 + b']' * 70])
def test_strict_json_refuses_invalid_values(raw):
    with pytest.raises(PendingTransactionError):
        portable.parse_json(raw)


@pytest.mark.parametrize("mutation", ["version", "field", "minimum", "output", "pretty"])
def test_strict_envelope_schema(repository, mutation):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    envelope = portable.parse_json(bundle[portable.PORTABLE_FILE])
    if mutation == "version":
        envelope["version"] = True
    elif mutation == "field":
        envelope["surprise"] = 1
    elif mutation == "minimum":
        envelope["minimum_reader"] = 2
    elif mutation == "output":
        envelope["output"] = "other-output"
    if mutation == "pretty":
        bundle[portable.PORTABLE_FILE] += b"\n"
    else:
        bundle[portable.PORTABLE_FILE] = portable.canonical_json(envelope)
    with pytest.raises(PendingTransactionError):
        portable.validate_bundle(bundle, sources, "graphify-out")


def test_manifest_cannot_omit_selected_sources_even_with_valid_content_id(repository):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    manifest = portable.parse_json(bundle["manifest.json"])
    manifest["sources"] = []
    bundle["manifest.json"] = portable.canonical_json(manifest)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="complete committed source projection"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("marker", [None, {}, {"schema": 1}, {**portable.PORTABLE_MARKER, "generation": 1},
                                    {**portable.PORTABLE_MARKER, "schema": True}])
def test_invalid_graph_marker_refuses(repository, marker):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["graph"][GRAPH_WATERMARK_KEY] = marker
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="watermark"):
        portable.validate_bundle(bundle, sources, "graphify-out")


def test_main_reader_refuses_portable_graph(repository):
    with pytest.raises(PendingTransactionError):
        open_graph_snapshot(repository / "graphify-out/graph.json", purpose="portable-test")


@pytest.mark.parametrize("path,mode", [("target.py", "120000"), ("vendor", "160000"),
                                       ("../outside.py", "100644"), ("CON.py", "100644")])
def test_source_selector_refuses_unsupported_inputs(path, mode):
    with pytest.raises(PendingTransactionError):
        portable.source_records_from_blobs({path: (mode, b"text")}, "graphify-out")


def test_source_selector_excludes_output_and_checks_case_collisions():
    assert portable.source_records_from_blobs({"graphify-out/a.py": ("100644", b"ignored")},
                                              "graphify-out") == ()
    with pytest.raises(PendingTransactionError, match="collide"):
        portable.source_records_from_blobs({"a.py": ("100644", b"a"), "A.py": ("100644", b"b")},
                                           "graphify-out")


@pytest.mark.parametrize("paths", [
    ("\u00e9.py", "e\u0301.py"),
    ("\u00c9.py", "e\u0301.py"),
    ("A/first.py", "a/second.py"),
    ("\u00e9/first.py", "e\u0301/second.py"),
])
def test_source_selector_refuses_normalization_and_directory_aliases(paths):
    entries = {path: ("100644", b"def selected(): pass\n") for path in paths}
    with pytest.raises(PendingTransactionError, match="collide"):
        portable.source_records_from_blobs(entries, "graphify-out")


def test_source_selector_preserves_original_unicode_path_bytes():
    path = "e\u0301.py"
    records = portable.source_records_from_blobs({path: ("100644", b"pass\n")}, "graphify-out")
    assert records[0]["path"] == path
    assert records[0]["path"].encode("utf-8") == b"e\xcc\x81.py"


@pytest.mark.parametrize("entry_point", ["selector", "bundle"])
@pytest.mark.parametrize("output,path", [
    ("graphify-out", "Graphify-Out/a.py"),
    ("\u00e9-out", "e\u0301-out/a.py"),
    ("docs/graphify-out", "Docs/Graphify-Out/a.py"),
    ("docs/graphify-out", "docs/Graphify-Out/a.py"),
    ("docs/graphify-out", "Docs/other.py"),
    ("package.py/output", "Package.py"),
    ("bundle.py", "Bundle.py"),
    ("package.py/output", "package.py"),
])
def test_source_paths_cannot_collide_with_output_directories(output, path, entry_point):
    payload = b"pass\n"
    records = [{"path": path, "mode": "100644", "sha256": hashlib.sha256(payload).hexdigest()}]
    with pytest.raises(PendingTransactionError, match="colli"):
        if entry_point == "selector":
            portable.source_records_from_blobs({path: ("100644", payload)}, output)
        else:
            portable.make_bundle(graph(), records, output)


@pytest.mark.parametrize("path", ["docs/other.py", "docs/graphify-outside/a.py"])
def test_output_collision_check_preserves_exact_shared_parents_and_component_boundaries(path):
    output = "docs/graphify-out"
    records = portable.source_records_from_blobs({path: ("100644", b"pass\n")}, output)
    assert records[0]["path"] == path
    portable.make_bundle(graph(), records, output)
    assert portable.source_records_from_blobs(
        {f"{output}/ignored.py": ("100644", b"ignored")}, output) == ()


@pytest.mark.parametrize("output,path", [
    ("graphify-out", "Graphify-Out/a.py"),
    ("\u00e9-out", "e\u0301-out/a.py"),
    ("docs/graphify-out", "Docs/Graphify-Out/a.py"),
])
def test_output_alias_in_real_git_objects_refuses_before_clone_admission(tmp_path, output, path):
    from graphify.merge_finalize import validate_index_bundle

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "core.ignorecase", "false")
    git(root, "config", "core.precomposeunicode", "false")
    payload = b"def selected(): pass\n"
    records = portable.source_records_from_blobs({"main.py": ("100644", payload)}, output)
    bundle = portable.make_bundle(graph(), records, output)
    manifest = portable.parse_json(bundle["manifest.json"])
    manifest["sources"][0]["path"] = path
    bundle["manifest.json"] = portable.canonical_json(manifest)
    refresh_envelope(bundle)
    # Immutable objects retain distinct Git spellings even on a host whose
    # filesystem combines case or Unicode aliases during checkout.
    entries = {path: payload, **{f"{output}/{name}": data for name, data in bundle.items()}}
    for entry, data in entries.items():
        oid = subprocess.check_output(
            ["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=data).decode().strip()
        git(root, "update-index", "--add", "--cacheinfo", "100644", oid, entry)
    with pytest.raises(PendingTransactionError, match="colli"):
        validate_index_bundle(root, output)
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture")
    with pytest.raises(PendingTransactionError, match="colli"):
        portable.source_records_from_tree(root, output)
    clone = tmp_path / "clone"
    git(root, "clone", "-q", "--no-local", str(root), str(clone))
    before, index = state(clone), (clone / ".git/index").read_bytes()
    with pytest.raises(PendingTransactionError, match="colli"):
        portable.open_portable_graph_snapshot(clone, output)
    assert state(clone) == before
    assert (clone / ".git/index").read_bytes() == index


def test_new_coordination_during_admission_refuses(repository, monkeypatch):
    original = portable.validate_bundle
    def add_authority(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        (repository / "graphify-out/.graphify_transaction.json").write_text("foreign")
        return snapshot
    monkeypatch.setattr(portable, "validate_bundle", add_authority)
    with pytest.raises(PendingTransactionError, match="mixed local authority"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")


def test_artifact_replacement_during_admission_refuses(repository, monkeypatch):
    original = portable.validate_bundle
    def replace_graph(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        graph_path = repository / "graphify-out/graph.json"
        graph_path.rename(repository / "old-graph.json")
        graph_path.write_bytes(snapshot.payload)
        return snapshot
    monkeypatch.setattr(portable, "validate_bundle", replace_graph)
    with pytest.raises(PendingTransactionError, match="identity changed"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")


def test_selected_git_revision_is_pinned(repository):
    prior = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "main.py").write_text("new committed source")
    commit(repository)
    snapshot = portable.open_portable_graph_snapshot(repository, "graphify-out", revision=prior)
    assert snapshot.revision == prior


@pytest.mark.parametrize("route", ["ordinary", "external", "detached"])
def test_baseline_reader_rejects_portable_format(repository, monkeypatch, route):
    checkout = Path(__file__).resolve().parents[1]
    source = git(checkout, "show", "3cecae9bed3f9cb02caa70788fae8faa583f8828:graphify/transaction.py")
    baseline = types.ModuleType("graphify_baseline_transaction")
    baseline.__file__ = str(checkout / "graphify/transaction.py")
    monkeypatch.setitem(sys.modules, baseline.__name__, baseline)
    exec(compile(source, baseline.__file__, "exec"), baseline.__dict__)
    before, index = state(repository), (repository / ".git/index").read_bytes()
    path = repository / "graphify-out/graph.json"
    with pytest.raises(baseline.PendingTransactionError):
        if route == "ordinary":
            baseline.open_graph_snapshot(path, purpose="compatibility")
        elif route == "external":
            baseline.open_external_graph_snapshot(path)
        else:
            baseline.load_detached_merge_snapshot(path, role="ours")
    assert state(repository) == before
    assert (repository / ".git/index").read_bytes() == index


def test_ancestor_local_authority_refuses(repository):
    (repository / ".graphify_protocol.json").write_text("malformed")
    with pytest.raises(PendingTransactionError, match="nested beneath managed"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")


def test_committed_executable_artifact_mode_refuses(repository):
    os.chmod(repository / "graphify-out/graph.json", 0o755)
    git(repository, "update-index", "--chmod=+x", "graphify-out/graph.json")
    commit(repository)
    os.chmod(repository / "graphify-out/graph.json", 0o644)
    with pytest.raises(PendingTransactionError, match="Git modes"):
        portable.open_portable_graph_snapshot(repository, "graphify-out")


def test_manifest_boolean_version_refuses(repository):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    manifest = portable.parse_json(bundle["manifest.json"])
    manifest["version"] = True
    bundle["manifest.json"] = portable.canonical_json(manifest)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="source projection"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("problem", ["duplicate", "dangling", "flags"])
def test_node_link_contract_refuses_malformed_graph(problem):
    data = graph()
    if problem == "duplicate":
        data["nodes"].append(dict(data["nodes"][0]))
    elif problem == "dangling":
        data["links"].append({"source": "hello", "target": "missing"})
    else:
        data["directed"] = 0
    with pytest.raises(PendingTransactionError):
        portable.make_bundle(data, [], "graphify-out")


@pytest.mark.parametrize("problem", ["integer-identifiers", "mixed-identifiers"])
def test_query_profile_requires_string_identifiers_with_valid_bundle_hashes(repository, problem):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["nodes"] = [{"id": 1}, {"id": 2}]
    data["links"] = [{"source": 1, "target": 2}]
    if problem == "mixed-identifiers":
        data["nodes"][1]["id"] = "hello"
        data["links"][0]["target"] = "hello"
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="identifiers"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_query_profile_refuses_integer_endpoints_with_valid_bundle_hashes(repository, endpoint):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["links"] = [{"source": "hello", "target": "hello"}]
    data["links"][0][endpoint] = 1
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="link"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("field", ["label", "norm_label", "source_file"])
@pytest.mark.parametrize("value", [1, True, None, [], {}])
def test_query_profile_refuses_non_string_node_metadata_with_valid_bundle_hashes(
    repository, field, value,
):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["nodes"][0][field] = value
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="node metadata"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("metadata", [{}, {"label": "", "norm_label": "", "source_file": ""},
                                      {"label": "Hello", "norm_label": "hello", "source_file": "main.py"}])
def test_query_profile_preserves_absent_and_string_node_metadata(metadata):
    from networkx.readwrite import json_graph
    from graphify.serve import _query_graph_text
    data = graph()
    data["nodes"] = [{"id": "hello", **metadata}]
    bundle = portable.make_bundle(data, [], "graphify-out")
    snapshot = portable.validate_bundle(bundle, [], "graphify-out")
    loaded = json_graph.node_link_graph(snapshot.data, edges="links")
    assert "1 nodes found" in _query_graph_text(loaded, "hello")


@pytest.mark.parametrize("field", ["_learning_overlay", "_idf_cache", "_trigram_index"])
@pytest.mark.parametrize("value", [1, {}])
def test_query_profile_refuses_persisted_runtime_metadata_with_valid_bundle_hashes(
    repository, field, value,
):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["graph"][field] = value
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="runtime metadata"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("value", [1, True, None, [], {}])
def test_query_profile_refuses_non_string_edge_context_with_valid_bundle_hashes(repository, value):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["links"] = [{"source": "hello", "target": "hello", "context": value}]
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="edge context"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("value", [True, None, [], {}, 1.5])
def test_query_profile_refuses_invalid_multigraph_key_with_valid_bundle_hashes(repository, value):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    data = portable.parse_json(bundle["graph.json"])
    data["multigraph"] = True
    data["links"] = [{"source": "hello", "target": "hello", "key": value}]
    bundle["graph.json"] = portable.canonical_json(data)
    refresh_envelope(bundle)
    with pytest.raises(PendingTransactionError, match="multigraph key"):
        portable.validate_bundle(bundle, sources, "graphify-out")


@pytest.mark.parametrize("key", ["edge", 0])
def test_query_profile_preserves_supported_multigraph_keys_and_context(key):
    from networkx.readwrite import json_graph
    from graphify.serve import _query_graph_text
    data = graph()
    data["multigraph"] = True
    data["links"] = [{"source": "hello", "target": "hello", "key": key, "context": "call"}]
    bundle = portable.make_bundle(data, [], "graphify-out")
    snapshot = portable.validate_bundle(bundle, [], "graphify-out")
    loaded = json_graph.node_link_graph(snapshot.data, edges="links")
    assert "1 nodes found" in _query_graph_text(loaded, "hello", context_filters=["call"])


def test_aggregate_byte_limit_refuses(repository, monkeypatch):
    bundle = payloads(repository)
    _, sources = portable.source_records_from_tree(repository, "graphify-out")
    monkeypatch.setattr(portable, "_MAX_TOTAL", sum(map(len, bundle.values())) - 1)
    with pytest.raises(PendingTransactionError, match="byte bounds"):
        portable.validate_bundle(bundle, sources, "graphify-out")
