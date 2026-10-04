"""Selected packed-ref freshness with disposable source/state repositories."""
import os
import subprocess
import time

import pytest

from graphify.source_io import SourceError
from graphify.workspace.adapters.v8 import V8Adapter
from graphify.workspace.adapters.base import QueryRequest
from graphify.workspace.identity import SourceDiscoveryError
from graphify.workspace.query import query_structural
from graphify.workspace.sync import synchronize_structural
from tests.test_workspace_adapter_v8 import _build
from tests.test_workspace_structural_s4 import runtime_fixture, request_for
from tests.workspace_s3_helpers import create_repo, git_output, tree_snapshot, REPO_UUID

CHECKPOINT = "refs/codex/turn-diffs/checkpoints/fixture/1"


def packed_fixture(repo, mode="loose"):
    head = repo / ".git/HEAD"
    commit = git_output(repo, "rev-parse", "HEAD")
    ref = head.read_text()[4:].strip()
    first = git_output(repo, "rev-parse", "HEAD^{tree}")
    # Construct a second existing tree before the retained observation.
    second = subprocess.check_output(["git", "-C", str(repo), "mktree"], input=b"").decode().strip()
    git_output(repo, "update-ref", CHECKPOINT, first)
    git_output(repo, "pack-refs", "--all", "--no-prune")
    if mode == "packed":
        (repo / ".git" / ref).unlink()
    elif mode == "detached":
        head.write_text(commit + "\n")
    return ref, commit, second


def change_checkpoint(repo, second, *, pack=True):
    git_output(repo, "update-ref", CHECKPOINT, second)
    if pack:
        git_output(repo, "pack-refs", "--all", "--no-prune")


@pytest.mark.parametrize("mode", ["loose", "packed", "detached"])
@pytest.mark.parametrize("mutation", ["replace", "add", "delete"])
def test_unrelated_packed_change_preserves_detection_replay_and_recovery(tmp_path, mode, mutation):
    repo = create_repo(tmp_path.resolve() / "repo")
    _ref, _commit, second = packed_fixture(repo, mode)
    engine = V8Adapter()
    initial, built = _build(engine, repo, tmp_path / "payload")
    previous = engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest)
    if mutation == "replace":
        change_checkpoint(repo, second)
    elif mutation == "add":
        git_output(repo, "update-ref", CHECKPOINT + "-new", second)
        git_output(repo, "pack-refs", "--all", "--no-prune")
    else:
        git_output(repo, "update-ref", "-d", CHECKPOINT)
    before = tree_snapshot(repo)
    observed = engine.observe(repo, input_manifest=built.input_manifest)
    assert observed.initial_detection == initial.initial_detection
    assert observed.consumed_inputs == built.input_manifest
    assert engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest) == previous
    assert tree_snapshot(repo) == before


@pytest.mark.parametrize("mutation", ["replace", "header", "remove", "appear", "shadow"])
def test_unconsulted_packed_representation_is_not_freshness(tmp_path, mutation):
    repo = create_repo(tmp_path.resolve() / "repo")
    ref, commit, _second = packed_fixture(repo)
    packed = repo / ".git/packed-refs"
    if mutation == "appear":
        packed.unlink()
    engine = V8Adapter()
    initial, built = _build(engine, repo, tmp_path / "payload")
    if mutation == "replace":
        replacement = repo / ".git/replacement"
        replacement.write_bytes(packed.read_bytes())
        os.replace(replacement, packed)
    elif mutation == "header":
        packed.write_bytes(packed.read_bytes().replace(b"# pack-refs with:", b"# pack-refs with: fixture"))
    elif mutation == "remove":
        packed.unlink()
    elif mutation == "appear":
        packed.write_text(commit + " " + ref + "\n")
    else:
        # A valid but shadowed packed value cannot override the loose branch.
        packed.write_bytes(packed.read_bytes().replace((commit + " " + ref).encode(),
                                                      ("0" * 40 + " " + ref).encode()))
    observed = engine.observe(repo, input_manifest=built.input_manifest)
    assert observed.initial_detection == initial.initial_detection
    assert observed.consumed_inputs == built.input_manifest


@pytest.mark.parametrize("transition", ["loose-to-packed", "packed-to-loose", "symbolic"])
def test_selected_storage_or_symbolic_route_remains_material(tmp_path, transition):
    repo = create_repo(tmp_path.resolve() / "repo")
    ref, commit, _second = packed_fixture(repo, "packed" if transition == "packed-to-loose" else "loose")
    engine = V8Adapter()
    _initial, built = _build(engine, repo, tmp_path / "payload")
    if transition == "loose-to-packed":
        (repo / ".git" / ref).unlink()
    elif transition == "packed-to-loose":
        (repo / ".git" / ref).write_text(commit + "\n")
    else:
        (repo / ".git/refs/heads/alias").write_text(commit + "\n")
        (repo / ".git" / ref).write_text("ref: refs/heads/alias\n")
    with pytest.raises((SourceError, SourceDiscoveryError, ValueError)):
        engine.observe(repo, input_manifest=built.input_manifest)


@pytest.mark.parametrize("moment", ["before", "during"])
def test_certified_query_tolerates_unrelated_packed_changes_without_writes(tmp_path, monkeypatch, moment):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    _ref, _commit, second = packed_fixture(repo)
    request = request_for(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert result.pointer_revision == 1
    state = tree_snapshot(runtime.inputs.state_root)
    original = runtime.adapter.query_structural
    expected_source = None

    def traverse(*args, **kwargs):
        nonlocal expected_source
        text = original(*args, **kwargs)
        change_checkpoint(repo, second)
        expected_source = tree_snapshot(repo)
        return text

    if moment == "during":
        monkeypatch.setattr(runtime.adapter, "query_structural", traverse)
    else:
        change_checkpoint(repo, second)
        expected_source = tree_snapshot(repo)
    text = query_structural(runtime, REPO_UUID, QueryRequest("caller"),
                            deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert "caller" in text and "leaf" in text
    assert tree_snapshot(runtime.inputs.state_root) == state
    assert tree_snapshot(repo) == expected_source


@pytest.mark.parametrize("boundary", ["request", "adapter_built", "generation_certified"])
def test_structural_sync_tolerates_unrelated_packed_changes(tmp_path, monkeypatch, boundary):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    _ref, _commit, second = packed_fixture(repo)
    request = request_for(runtime)
    reached = []
    if boundary == "request":
        change_checkpoint(repo, second)
    else:
        def fault(label):
            if label == boundary or label.endswith(":" + boundary):
                reached.append(label)
                change_checkpoint(repo, second)
        runtime.stores.generations.fault_hook = fault
    result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert result.pointer_revision == 1
    assert boundary == "request" or reached

@pytest.mark.parametrize("mutation", ["null", "shape", "many", "path", "ref", "lookup",
                                     "oid", "negative", "physical", "old-format"])
def test_packed_projection_manifest_refuses_malformed_or_legacy_evidence(tmp_path, mutation):
    from graphify.workspace.contracts import InputManifest, ContractError
    repo = create_repo(tmp_path.resolve() / "repo")
    packed_fixture(repo, "packed")
    value = V8Adapter().observe(repo).initial_detection.to_dict()
    record = next(e for e in value["evidence"] if e["operation"] == "packed_refs")
    if mutation == "null":
        record["value"] = None
    elif mutation == "shape":
        record["value"] = ["not an entry"]
    elif mutation == "many":
        record["value"] *= 2
    elif mutation == "path":
        record["path"] = "git:config"
    elif mutation == "ref":
        record["value"][0][0] = "refs/heads/e\u0301"
    elif mutation == "lookup":
        record["value"][0][1] = "git:refs/heads/other"
    elif mutation == "oid":
        record["value"][0][2] = "f" * 64
    elif mutation == "negative":
        lookup = record["value"][0][1]
        value["evidence"] = [e for e in value["evidence"] if (e["operation"], e["path"]) != ("probe", lookup)]
    elif mutation == "physical":
        value["evidence"].append({"operation": "probe", "path": "git:packed-refs", "value": None})
        value["evidence"].sort(key=lambda e: (e["operation"], e["path"]))
    else:
        value["format_version"] = 1
    with pytest.raises(ContractError):
        InputManifest.from_mapping(value)


@pytest.mark.parametrize("field,old", [
    ("detector_id", "graphify-v8/workspace-observer-v2"),
    ("adapter_contract_version", 2), ("input_manifest_version", 1),
    ("extractor_cache_abi", "graphify-v8-structural-1"),
])
def test_prior_compatibility_tuple_is_not_reinterpreted(field, old):
    from graphify.workspace.contracts import CompatibilityManifest, ContractError
    from tests.test_workspace_contracts import compatibility
    value = compatibility().to_dict()
    value[field] = old
    with pytest.raises(ContractError):
        CompatibilityManifest.from_mapping(value)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
@pytest.mark.parametrize("mode", ["loose", "packed", "detached"])
def test_unsafe_packed_container_refuses_before_git(tmp_path, monkeypatch, kind, mode):
    repo = create_repo(tmp_path.resolve() / "repo")
    packed_fixture(repo, mode)
    packed = repo / ".git/packed-refs"
    outside = tmp_path / "outside"
    outside.write_bytes(packed.read_bytes())
    packed.unlink()
    if kind == "symlink":
        packed.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(packed)
    else:
        packed.mkdir()
    before = tree_snapshot(repo)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Git started"))
    with pytest.raises((SourceError, SourceDiscoveryError, ValueError)):
        V8Adapter().observe(repo)
    assert tree_snapshot(repo) == before


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "malformed", "extra", "trailing", "no-oid"])
def test_selected_packed_failure_and_recovery_absence(tmp_path, mutation):
    from graphify.source_io import SourceUnsupported
    repo = create_repo(tmp_path.resolve() / "repo")
    ref, commit, _second = packed_fixture(repo, "packed")
    engine = V8Adapter()
    _initial, built = _build(engine, repo, tmp_path / "payload")
    previous = engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest)
    packed = repo / ".git/packed-refs"
    # Keep the current source discoverable independently of the sealed old route.
    (repo / ".git/HEAD").write_text(commit + "\n")
    rows = packed.read_bytes().splitlines(keepends=True)
    selected = next(row for row in rows if row.rstrip().endswith(b" " + ref.encode()))
    if mutation == "missing":
        packed.write_bytes(b"".join(row for row in rows if row != selected))
    elif mutation == "duplicate":
        packed.write_bytes(b"".join(rows) + selected)
    elif mutation == "malformed":
        packed.write_bytes(b"".join(rows).replace(selected, b"invalid " + ref.encode() + b"\n"))
    elif mutation == "extra":
        packed.write_bytes(b"".join(rows).replace(selected, commit.encode() + b" extra " + ref.encode() + b"\n"))
    elif mutation == "trailing":
        packed.write_bytes(b"".join(rows).replace(selected, selected.rstrip() + b" \n"))
    else:
        packed.write_bytes(b"".join(rows).replace(selected, ref.encode() + b"\n"))
    with pytest.raises((SourceError, SourceDiscoveryError, ValueError)):
        engine.observe(repo, input_manifest=built.input_manifest)
    if mutation == "missing":
        assert engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest) != previous
    else:
        with pytest.raises((SourceUnsupported, SourceDiscoveryError, ValueError)):
            engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest)


@pytest.mark.parametrize("mode", ["loose", "packed"])
@pytest.mark.parametrize("moment", ["before", "during"])
def test_certified_query_withholds_selected_oid_drift(tmp_path, monkeypatch, mode, moment):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    ref, commit, _second = packed_fixture(repo, mode)
    tree = git_output(repo, "rev-parse", "HEAD^{tree}")
    next_commit = subprocess.check_output(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", commit], input=b"second commit\n",
    ).decode().strip()
    synchronize_structural(runtime, request_for(runtime), attempt_sha256="a" * 64)
    before_state = tree_snapshot(runtime.inputs.state_root)
    expected_source = None
    called = []
    original = runtime.adapter.query_structural

    def change():
        nonlocal expected_source
        if mode == "loose":
            (repo / ".git" / ref).write_text(next_commit + "\n")
        else:
            packed = repo / ".git/packed-refs"
            packed.write_bytes(packed.read_bytes().replace((commit + " " + ref).encode(),
                                                          (next_commit + " " + ref).encode()))
        expected_source = tree_snapshot(repo)

    def traverse(*args, **kwargs):
        called.append(True)
        text = original(*args, **kwargs)
        change()
        return text

    monkeypatch.setattr(runtime.adapter, "query_structural", traverse)
    if moment == "before":
        change()
    with pytest.raises((SourceError, ValueError)):
        query_structural(runtime, REPO_UUID, QueryRequest("caller"),
                         deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert bool(called) == (moment == "during")
    assert tree_snapshot(runtime.inputs.state_root) == before_state
    assert tree_snapshot(repo) == expected_source


def test_projection_cannot_bypass_acquisition_or_byte_budget(tmp_path):
    from graphify.source_io import SourceIO, SourceUnsupported
    from graphify.workspace.adapters.v8 import _replay_packed_refs
    git = tmp_path.resolve() / "git"
    git.mkdir()
    packed = git / "packed-refs"
    packed.write_bytes(b"#" * 11)
    with SourceIO(tmp_path.resolve(), extra_roots={"git": git}) as inputs:
        inputs.probe(packed)
        with pytest.raises(SourceUnsupported, match="complete read"):
            inputs.record_packed_refs(())
    with SourceIO(tmp_path.resolve(), extra_roots={"git": git}, max_file_bytes=10) as inputs:
        with pytest.raises(SourceUnsupported, match="byte limit"):
            _replay_packed_refs(inputs, {"value": []})
        assert inputs.failure is not None


def test_projected_container_still_detects_in_scope_replacement(tmp_path):
    from graphify.source_io import SourceIO, SourceChanged
    from graphify.workspace.adapters.v8 import _replay_packed_refs
    git = tmp_path.resolve() / "git"
    git.mkdir()
    packed = git / "packed-refs"
    packed.write_bytes(b"# fixture\n")
    with SourceIO(tmp_path.resolve(), extra_roots={"git": git}) as inputs:
        _replay_packed_refs(inputs, {"value": []})
        packed.write_bytes(b"# changed\n")
        with pytest.raises(SourceChanged):
            _replay_packed_refs(inputs, {"value": []})


def test_projected_container_cannot_hide_engine_consumption(tmp_path):
    from graphify.source_io import SourceIO, SourceUnsupported, engine_inputs
    from graphify.workspace.adapters.v8 import _replay_packed_refs
    git = tmp_path.resolve() / "git"
    git.mkdir()
    packed = git / "packed-refs"
    packed.write_bytes(b"# fixture\n")
    with SourceIO(tmp_path.resolve(), extra_roots={"git": git}) as inputs:
        _replay_packed_refs(inputs, {"value": []})
        with pytest.raises(SourceUnsupported, match="engine input"), engine_inputs(inputs):
            inputs.read_bytes(packed)


def test_selected_packed_tab_separator_preserves_replay_and_recovery(tmp_path):
    repo = create_repo(tmp_path.resolve() / "repo")
    ref, commit, _second = packed_fixture(repo, "packed")
    engine = V8Adapter()
    initial, built = _build(engine, repo, tmp_path / "payload")
    previous = engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest)
    packed = repo / ".git/packed-refs"
    packed.write_bytes(packed.read_bytes().replace((commit + " " + ref).encode(),
                                                  (commit + "\t" + ref).encode()))
    assert git_output(repo, "rev-parse", "HEAD") == commit
    observed = engine.observe(repo, input_manifest=built.input_manifest)
    assert observed.initial_detection == initial.initial_detection
    assert observed.consumed_inputs == built.input_manifest
    assert engine.observe_consumed_inputs(repo, input_manifest=built.input_manifest) == previous


@pytest.mark.parametrize("mutation", ["trailing", "extra"])
def test_malformed_sealed_packed_ref_cannot_abandon_certified_request(tmp_path, monkeypatch, mutation):
    from graphify.source_io import SourceUnsupported
    from graphify.workspace.persistence import InjectedFault
    from tests.test_workspace_s4_consumed_recovery import _no_abandonment
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    ref, commit, _second = packed_fixture(repo, "packed")
    request = request_for(runtime)

    def fault(label):
        if label.endswith(":generation_certified"):
            raise InjectedFault(label)

    runtime.stores.generations.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    runtime.stores.generations.fault_hook = lambda _label: None
    (repo / ".git/HEAD").write_text(commit + "\n")
    packed = repo / ".git/packed-refs"
    selected = (commit + " " + ref).encode()
    replacement = selected + b" " if mutation == "trailing" else (commit + " extra " + ref).encode()
    packed.write_bytes(packed.read_bytes().replace(selected, replacement))
    source_before = tree_snapshot(repo)
    with pytest.raises(SourceUnsupported, match="packed Git reference"):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    _no_abandonment(runtime)
    assert tree_snapshot(repo) == source_before


def test_selected_projection_matches_published_json_schema(tmp_path):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    repo = create_repo(tmp_path.resolve() / "repo")
    packed_fixture(repo, "packed")
    value = V8Adapter().observe(repo).initial_detection.to_dict()
    schema = json.loads((Path(__file__).parents[1] / "graphify/workspace/schemas/input-manifest.schema.json").read_text())
    Draft202012Validator(schema).validate(value)


@pytest.mark.parametrize("mode", ["loose", "packed", "detached"])
def test_linked_worktree_ignores_unrelated_common_packed_changes(tmp_path, mode):
    repo = create_repo(tmp_path.resolve() / "repo")
    ref, _commit, second = packed_fixture(repo, mode)
    linked = tmp_path.resolve() / "linked"
    git_output(repo, "worktree", "add", "--detach", str(linked))
    if mode != "detached":
        (repo / ".git/worktrees/linked/HEAD").write_text("ref: " + ref + "\n")
    engine = V8Adapter()
    initial, built = _build(engine, linked, tmp_path / "payload")
    change_checkpoint(repo, second)
    before = tree_snapshot(repo), tree_snapshot(linked)
    observed = engine.observe(linked, input_manifest=built.input_manifest)
    assert observed.initial_detection == initial.initial_detection
    assert observed.consumed_inputs == built.input_manifest
    assert (tree_snapshot(repo), tree_snapshot(linked)) == before


@pytest.mark.parametrize("row", [b"invalid refs/heads/main", b"refs/heads/main",
                                b"{oid} extra refs/heads/main", b"{oid} refs/heads/main ",
                                b"^{oid} refs/heads/main"])
def test_malformed_selected_row_is_not_recovery_absence(row):
    from graphify.source_io import SourceUnsupported
    from graphify.workspace.adapters.v8 import _packed_entries
    with pytest.raises(SourceUnsupported, match="packed Git reference"):
        _packed_entries(row.replace(b"{oid}", b"a" * 40),
                        [("refs/heads/main", "git:refs/heads/main")], allow_missing=True)
