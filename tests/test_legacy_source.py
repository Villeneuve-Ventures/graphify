"""Fresh structural payloads consume rooted evidence and never publish it."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify.legacy_source import BOUNDS, build, reobserve, validate_request
from graphify.source_io import SourceChanged, SourceError, SourceUnsupported
from graphify.workspace.contracts import InputManifest


@pytest.fixture
def source_request(tmp_path):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    output = tmp_path.resolve() / "output"
    output.mkdir()
    (root / "main.py").write_text(
        "def leaf():\n    return 42\n\ndef caller():\n    return leaf()\n"
    )
    return {
        "schema": 1, "root": str(root), "output": str(output), "include": ["**"],
        "exclude": [], "extra_roots": [], "extraction_profile": "structural-v1",
        "artifact_profile": "core-v1",
    }


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mode, path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


def test_build_is_fresh_deterministic_and_read_only(source_request, monkeypatch):
    root, output = Path(source_request["root"]), Path(source_request["output"])
    (root / "README.md").write_text("A document which needs semantic extraction.")
    (root / "image.png").write_bytes(b"unparsed media")
    (output / "manifest.json").write_text('{"stale.py":{"semantic_hash":"stale"}}')
    (output / "graph.json").write_text('{"untrusted":"old"}')
    before = snapshot(root.parent)
    monkeypatch.setitem(sys.modules, "graphify.llm", None)
    first = build(source_request)
    second = build(source_request)
    assert first.payloads == second.payloads
    assert first.evidence == second.evidence
    assert snapshot(root.parent) == before
    assert set(first.payloads) == {
        "graph.json", "manifest.json", ".graphify_root", ".graphify_build.json",
    }
    graph = json.loads(first.payloads["graph.json"])
    labels = {node["id"]: node["label"] for node in graph["nodes"]}
    assert graph["directed"] is True
    assert any(labels[edge["source"]] == "caller()" and labels[edge["target"]] == "leaf()"
               and edge["relation"] == "calls" for edge in graph["links"])
    manifest = json.loads(first.payloads["manifest.json"])
    assert set(manifest) == {"main.py"}
    assert manifest["main.py"] == {
        "mtime": (root / "main.py").stat().st_mtime_ns / 1_000_000_000,
        "ast_hash": hashlib.md5((root / "main.py").read_bytes(), usedforsecurity=False).hexdigest(),
        "semantic_hash": "",
    }
    assert first.selected == ("main.py",)
    assert first.outcomes == [{"path": "main.py", "status": "success"}]
    assert {item["path"] for item in first.uncovered["selected_noncode"]} == {
        "README.md", "image.png",
    }
    assert InputManifest.from_mapping(first.evidence).sha256 == first.evidence_digest
    assert first.payloads[".graphify_root"] == (str(root) + "\n").encode()
    reobserve(source_request, first.evidence)


def test_explicit_selection_after_full_detection(source_request):
    root = Path(source_request["root"])
    (root / "other.py").write_text("def unselected(): pass\n")
    (root / "unknown.zzz").write_text("unrecognized")
    (root / "script.r").write_text("x <- 1")
    nested = root / "nested"
    nested.mkdir()
    (nested / "deep.py").write_text("def deep(): pass\n")
    source_request["include"] = ["*.py"]
    source_request["exclude"] = ["other.py"]
    result = build(source_request)
    assert result.selected == ("main.py",)
    reads = {record["path"] for record in result.evidence["evidence"]
             if record["operation"] == "read"}
    assert {"other.py", "unknown.zzz", "script.r", "nested/deep.py"} <= reads
    assert "unknown.zzz" in result.uncovered["unclassified_not_selected"]
    assert "nested/deep.py" in result.uncovered["not_selected"]
    assert json.loads(result.payloads[".graphify_build.json"])["excludes"] == ["other.py"]


def test_selected_unclassified_fails_unless_explicitly_excluded(source_request):
    root = Path(source_request["root"])
    (root / "unknown.zzz").write_text("unknown")
    with pytest.raises(SourceUnsupported, match="selected unclassified"):
        build(source_request)
    source_request["exclude"] = ["unknown.zzz"]
    assert build(source_request).selected == ("main.py",)


def test_unsupported_extractor_and_missing_parser_fail_closed(source_request, monkeypatch):
    from graphify.extract import ExtractionIncomplete

    root = Path(source_request["root"])
    (root / "script.r").write_text("x <- 1")
    with pytest.raises(ExtractionIncomplete) as caught:
        build(source_request)
    assert any(item["status"] == "unsupported_extractor" for item in caught.value.outcomes)
    source_request["exclude"] = ["script.r"]
    (root / "main.sql").write_text("SELECT 1;")
    monkeypatch.setitem(sys.modules, "tree_sitter_sql", None)
    with pytest.raises(ExtractionIncomplete) as caught:
        build(source_request)
    assert any(item["status"] == "missing_parser" for item in caught.value.outcomes)
    assert list(Path(source_request["output"]).iterdir()) == []


@pytest.mark.parametrize("change", ["source", "noncode", "membership", "negative", "policy"])
def test_reobserve_rejects_consumed_drift(source_request, change):
    root = Path(source_request["root"])
    (root / "README.md").write_text("original document")
    result = build(source_request)
    if change == "source":
        (root / "main.py").write_text("def replacement(): pass\n")
    elif change == "noncode":
        (root / "README.md").write_text("changed document")
    elif change == "membership":
        (root / "new.py").write_text("pass\n")
    elif change == "policy":
        (root / ".graphifyignore").write_text("main.py\n")
    else:
        negatives = [record["path"] for record in result.evidence["evidence"]
                     if record["operation"] == "probe" and record["value"] is None
                     and ":" not in record["path"]]
        assert negatives
        target = root / negatives[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    with pytest.raises(SourceError):
        reobserve(source_request, result.evidence)


def test_dependency_metadata_consumed_even_when_not_selected(source_request):
    root = Path(source_request["root"])
    (root / "app.ts").write_text("import {value} from '@lib'; console.log(value);")
    (root / "lib.ts").write_text("export const value = 1;")
    (root / "tsconfig.json").write_text('{"compilerOptions":{"paths":{"@lib":["lib.ts"]}}}')
    (root / ".graphifyignore").write_text("tsconfig.json\n")
    source_request["include"] = ["**/*.ts"]
    result = build(source_request)
    assert ("read", "tsconfig.json") in {
        (record["operation"], record["path"]) for record in result.evidence["evidence"]
    }
    (root / "tsconfig.json").write_text('{"compilerOptions":{}}')
    with pytest.raises(SourceChanged):
        reobserve(source_request, result.evidence)


def test_external_git_routing_requires_explicit_extra_root(source_request):
    root = Path(source_request["root"])
    common = root.parent / "common"
    gitdir = common / "worktrees" / "example"
    gitdir.mkdir(parents=True)
    (common / "info").mkdir()
    (common / "info" / "exclude").write_text("excluded.py\n")
    (gitdir / "commondir").write_text("../..\n")
    (root / ".git").write_text(f"gitdir: {gitdir}\n")
    (root / "excluded.py").write_text("pass\n")
    source_request["include"] = ["**/*.py"]
    with pytest.raises(SourceUnsupported, match="outside declared roots"):
        build(source_request)
    source_request["extra_roots"] = [{"label": "git", "path": str(common)}]
    result = build(source_request)
    assert result.selected == ("main.py",)
    assert result.evidence["roots"] == ["git", "source"]
    reobserve(source_request, result.evidence)
    (common / "info" / "exclude").write_text("main.py\n")
    with pytest.raises(SourceChanged):
        reobserve(source_request, result.evidence)


def test_request_is_copied_and_fixed_bounds_bound_the_normalized_form(source_request):
    normalized = validate_request(source_request)
    assert normalized["bounds"] == BOUNDS
    assert validate_request(normalized) == normalized
    source_request["include"].append("other.py")
    assert normalized["include"] == ["**"]
    changed = copy.deepcopy(normalized)
    changed["bounds"]["max_file_bytes"] *= 2
    with pytest.raises(ValueError, match="bounds"):
        validate_request(changed)


@pytest.mark.parametrize("field,value", [
    ("schema", True), ("schema", 2), ("root", "."), ("output", "."),
    ("include", []), ("include", ["../outside.py"]), ("exclude", ["/absolute"]),
    ("include", ["dir//*.py"]), ("include", ["./main.py"]),
    ("include", ["C:/*.py"]), ("extra_roots", {}),
    ("extra_roots", [{"label": "arbitrary", "path": "/"}]),
    ("extraction_profile", "semantic-v1"), ("artifact_profile", "everything"),
])
def test_malformed_requests_fail_before_source_reads(source_request, field, value, monkeypatch):
    from graphify.source_io import SourceIO

    source_request[field] = value
    monkeypatch.setattr(SourceIO, "__enter__", lambda self: pytest.fail("source opened"))
    with pytest.raises(ValueError):
        build(source_request)


def test_symlink_and_noncanonical_root_rejected(source_request):
    root = Path(source_request["root"])
    alias = root.parent / "alias"
    alias.symlink_to(root, target_is_directory=True)
    source_request["root"] = str(alias)
    with pytest.raises(ValueError, match="canonical"):
        validate_request(source_request)
    source_request["root"] = str(root) + "/../source"
    with pytest.raises(ValueError, match="canonical"):
        validate_request(source_request)


def test_source_mutation_during_extraction_cannot_return_stamped_payload(source_request, monkeypatch):
    import graphify.extract as engine

    original = engine.extract

    def changed(paths, **kwargs):
        (Path(source_request["root"]) / "main.py").write_text("def changed(): pass\n")
        return original(paths, **kwargs)

    monkeypatch.setattr(engine, "extract", changed)
    with pytest.raises(engine.ExtractionIncomplete):
        build(source_request)
    assert list(Path(source_request["output"]).iterdir()) == []


def test_empty_selected_input_has_explicit_empty_outcome(source_request):
    root = Path(source_request["root"])
    (root / "empty.json").write_text("{}")
    source_request["include"] = ["empty.json"]
    result = build(source_request)
    assert result.outcomes == [{"path": "empty.json", "status": "empty"}]
    assert set(json.loads(result.payloads["manifest.json"])) == {"empty.json"}


def test_source_read_bound_refuses_instead_of_returning_prefix(source_request, monkeypatch):
    monkeypatch.setitem(BOUNDS, "max_file_bytes", 8)
    with pytest.raises(SourceUnsupported, match="byte limit"):
        build(source_request)


def test_payload_bytes_agree_across_process_hash_seeds(source_request):
    root = Path(source_request["root"])
    (root / "other.py").write_text("from main import leaf\ndef invoke(): return leaf()\n")
    script = (
        "import hashlib,json,sys; from graphify.legacy_source import build; "
        "result=build(json.loads(sys.stdin.read())); "
        "print(json.dumps({name:hashlib.sha256(raw).hexdigest() "
        "for name,raw in result.payloads.items()},sort_keys=True))"
    )
    outputs = []
    for seed in ("1", "2"):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script], input=json.dumps(source_request),
            text=True, capture_output=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": seed}, timeout=30,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_pinned_operational_directory_preserves_original_source_evidence(source_request):
    root = Path(source_request["root"])
    output = root / "graphify-out"
    output.mkdir()
    source_request["output"] = str(output)
    initial = build(source_request)
    workspace = root / (".graphify-prepare-" + "a" * 64)
    workspace.mkdir(mode=0o700)
    (workspace / "untrusted.py").write_text("def prepared_only(): pass\n")
    identity = workspace.stat()
    operational = {workspace: (identity.st_dev, identity.st_ino)}
    with pytest.raises(SourceChanged):
        reobserve(source_request, initial.evidence)
    reobserve(source_request, initial.evidence, operational_dirs=operational)
    repeated = build(source_request, operational_dirs=operational)
    assert repeated.evidence == initial.evidence
    assert repeated.evidence_digest == initial.evidence_digest
    assert repeated.payloads == initial.payloads
    assert repeated.selected == initial.selected
    assert all(".graphify-prepare-" not in record["path"]
               for record in repeated.evidence["evidence"])
    # Only the exact validated workspace is hidden. A second similarly named
    # directory remains in the source inventory and invalidates the old proof.
    (root / (".graphify-prepare-" + "b" * 64)).mkdir()
    with pytest.raises(SourceChanged):
        reobserve(source_request, initial.evidence, operational_dirs=operational)


@pytest.mark.parametrize("change", ["wrong_inode", "symlink", "missing", "ordinary_file"])
def test_operational_directory_requires_live_exact_ordinary_binding(source_request, change):
    root = Path(source_request["root"])
    initial = build(source_request)
    workspace = root / (".graphify-prepare-" + "a" * 64)
    workspace.mkdir()
    identity = workspace.stat()
    operational = {workspace: (identity.st_dev, identity.st_ino)}
    if change == "wrong_inode":
        operational[workspace] = (identity.st_dev, identity.st_ino + 1)
    else:
        workspace.rmdir()
        if change == "symlink":
            workspace.symlink_to(root, target_is_directory=True)
        elif change == "ordinary_file":
            workspace.write_text("not a directory")
    with pytest.raises(SourceChanged, match="operational directory"):
        reobserve(source_request, initial.evidence, operational_dirs=operational)
    with pytest.raises(SourceChanged, match="operational directory"):
        build(source_request, operational_dirs=operational)


def test_operational_exclusion_keeps_unrelated_new_source_visible(source_request):
    root = Path(source_request["root"])
    initial = build(source_request)
    workspace = root / (".graphify-prepare-" + "a" * 64)
    workspace.mkdir()
    identity = workspace.stat()
    operational = {workspace: (identity.st_dev, identity.st_ino)}
    (root / "new.py").write_text("def new_source(): pass\n")
    with pytest.raises(SourceChanged):
        reobserve(source_request, initial.evidence, operational_dirs=operational)
    repeated = build(source_request, operational_dirs=operational)
    assert repeated.evidence != initial.evidence
    assert "new.py" in repeated.selected


def test_operational_directory_is_revalidated_after_each_list(source_request, monkeypatch):
    from graphify.source_io import SourceIO

    root = Path(source_request["root"])
    initial = build(source_request)
    workspace = root / (".graphify-prepare-" + "a" * 64)
    workspace.mkdir()
    identity = workspace.stat()
    operational = {workspace: (identity.st_dev, identity.st_ino)}
    original = SourceIO.listdir

    def swap_after_list(inputs, path):
        entries = original(inputs, path)
        if Path(path) == root:
            workspace.rmdir()
            workspace.symlink_to(root, target_is_directory=True)
        return entries

    monkeypatch.setattr(SourceIO, "listdir", swap_after_list)
    with pytest.raises(SourceChanged, match="operational directory"):
        reobserve(source_request, initial.evidence, operational_dirs=operational)


def test_sealed_replay_request_normalization_matches_live_validation(source_request):
    policy = Path(source_request["root"]).parent / "policy"
    policy.mkdir()
    source_request["extra_roots"] = [{"label": "policy", "path": str(policy)}]
    normalized = validate_request(source_request)
    assert validate_request(source_request, require_sources=False) == normalized
    assert validate_request(normalized, require_sources=False) == normalized


@pytest.mark.parametrize("missing", ["source", "extra_root"])
def test_only_sealed_replay_validation_allows_missing_source_roots(source_request, missing):
    root = Path(source_request["root"])
    policy = root.parent / "policy"
    policy.mkdir()
    source_request["extra_roots"] = [{"label": "policy", "path": str(policy)}]
    normalized = validate_request(source_request)
    if missing == "source":
        root.rename(root.parent / "moved_source")
    else:
        policy.rmdir()
    assert validate_request(source_request, require_sources=False) == normalized
    with pytest.raises(ValueError, match="canonical directory"):
        validate_request(source_request)
    with pytest.raises(ValueError, match="canonical directory"):
        build(source_request)


@pytest.mark.parametrize("require_sources", [True, False])
@pytest.mark.parametrize("unsafe", [
    "relative/source", "/tmp/../source", "/tmp/./source", "/tmp//source",
    "//tmp/source", "/tmp/source/", "/tmp/source\x00", "/tmp/source\t",
    "/tmp/source\r", "/tmp/source\n", "/tmp/source\x1b", "/tmp/source\x7f",
    "/tmp/source\x85",
])
@pytest.mark.parametrize("field", ["root", "extra_root"])
def test_source_root_syntax_is_strict_in_both_validation_modes(
    source_request, field, unsafe, require_sources,
):
    if field == "root":
        source_request["root"] = unsafe
    else:
        source_request["extra_roots"] = [{"label": "policy", "path": unsafe}]
    with pytest.raises(ValueError, match="canonical directory"):
        validate_request(source_request, require_sources=require_sources)


def test_sealed_replay_validation_still_requires_live_canonical_output(source_request):
    output = Path(source_request["output"])
    output.rmdir()
    with pytest.raises(ValueError, match="output must be an absolute canonical directory"):
        validate_request(source_request, require_sources=False)


def test_sealed_replay_validation_does_not_dereference_source_roots(source_request, monkeypatch):
    root = Path(source_request["root"])
    policy = root.parent / "policy"
    policy.mkdir()
    source_request["extra_roots"] = [{"label": "policy", "path": str(policy)}]
    original = Path.resolve

    def only_output(path, *args, **kwargs):
        assert path not in {root, policy}, "source path dereferenced during sealed replay"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", only_output)
    assert validate_request(source_request, require_sources=False)["root"] == str(root)


@pytest.mark.parametrize("output_name", ["legacy-output", "graphify-out", "nested/legacy-output"])
def test_requested_output_contents_never_enter_source_corpus(source_request, output_name, monkeypatch):
    from graphify.source_io import SourceIO

    root = Path(source_request["root"])
    output = root / output_name
    output.mkdir(parents=True)
    source_request["output"] = str(output)
    (output / "graph.json").write_text(
        '{"nodes":[{"id":"old_only","label":"NEVER_TRUST_OLD_GRAPH"}],"links":[]}'
    )
    (output / "manifest.json").write_text('{"old.py":{"ast_hash":"OLD_MANIFEST"}}')
    (output / "old.py").write_text("def NEVER_TRUST_OUTPUT_SOURCE(): pass\n")
    (output / ".graphify_protocol.json").write_text('{"old":"coordination"}')
    original = SourceIO.read_bytes

    def refuse_output_reads(inputs, path, **kwargs):
        absolute = Path(path) if Path(path).is_absolute() else inputs.root / path
        assert not absolute.is_relative_to(output), "output content reached source reader"
        return original(inputs, path, **kwargs)

    monkeypatch.setattr(SourceIO, "read_bytes", refuse_output_reads)
    initial = build(source_request)
    assert initial.selected == ("main.py",)
    assert b"NEVER_TRUST" not in initial.payloads["graph.json"]
    assert set(json.loads(initial.payloads["manifest.json"])) == {"main.py"}
    assert all(not record["path"].startswith(output_name + "/")
               for record in initial.evidence["evidence"])
    directory = next(record for record in initial.evidence["evidence"]
                     if record["operation"] == "list"
                     and record["path"] == output.parent.relative_to(root).as_posix())
    assert output.name in {member[0] for member in directory["value"][1]}
    (output / "graph.json").write_text('{"changed":"old graph"}')
    (output / "manifest.json").write_text('{"changed":"old manifest"}')
    (output / ".graphify_protocol.json").write_text('{"changed":"coordination"}')
    (output / "new_state.json").write_text('{"new":"coordination"}')
    reobserve(source_request, initial.evidence)
    repeated = build(source_request)
    assert repeated.evidence == initial.evidence
    assert repeated.payloads == initial.payloads


def test_nested_output_replacement_changes_parent_binding(source_request):
    root = Path(source_request["root"])
    output = root / "legacy-output"
    output.mkdir()
    source_request["output"] = str(output)
    initial = build(source_request)
    original_output = root.parent / "original-output"
    output.rename(original_output)
    output.mkdir()
    with pytest.raises(SourceChanged):
        reobserve(source_request, initial.evidence)


@pytest.mark.parametrize("layout", ["equal", "ancestor"])
@pytest.mark.parametrize("require_sources", [True, False])
def test_output_cannot_equal_or_contain_source_before_source_io(
    source_request, layout, require_sources, monkeypatch,
):
    from graphify.source_io import SourceIO

    root = Path(source_request["root"])
    source_request["output"] = str(root if layout == "equal" else root.parent)
    monkeypatch.setattr(SourceIO, "__enter__", lambda self: pytest.fail("source IO opened"))
    with pytest.raises(ValueError, match="must not equal or contain"):
        validate_request(source_request, require_sources=require_sources)
    with pytest.raises(ValueError, match="must not equal or contain"):
        build(source_request)


def test_output_binding_is_revalidated_after_source_listing(source_request, monkeypatch):
    from graphify.source_io import SourceIO

    root = Path(source_request["root"])
    output = root / "legacy-output"
    output.mkdir()
    source_request["output"] = str(output)
    original = SourceIO.listdir

    def substitute_after_list(inputs, path):
        entries = original(inputs, path)
        if Path(path) == root:
            output.rename(root.parent / "original-output")
            output.mkdir()
        return entries

    monkeypatch.setattr(SourceIO, "listdir", substitute_after_list)
    with pytest.raises(SourceChanged, match="output directory identity"):
        build(source_request)
