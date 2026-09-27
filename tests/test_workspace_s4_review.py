"""Regressions for the S4 execution-boundary review findings."""
import os
import stat
from types import SimpleNamespace

import pytest

from graphify.workspace.adapters.base import QueryRejected, QueryRequest
from graphify.workspace.query import query_structural
from graphify.workspace.sync import synchronize_structural
from tests.test_workspace_adapter_v8 import adapter, _build
from tests.test_workspace_structural_s4 import runtime_fixture, request_for
from tests.workspace_s3_helpers import create_repo, git_output, tree_snapshot, REPO_UUID


@pytest.mark.parametrize("boundary", ["library", "adapter"])
@pytest.mark.parametrize("field,value", [
    ("depth", 10**12), ("depth", True), ("token_budget", 32769),
    ("question", "q" * 257), ("mode", "invalid"),
    ("context_filters", ["context"] * 17),
])
def test_revalidate_query_before_authority_or_payload_access(monkeypatch, boundary, field, value):
    request = QueryRequest("caller")
    object.__setattr__(request, field, value)
    def unexpected(*args, **kwargs):
        pytest.fail("invalid query reached authority or payload access")
    monkeypatch.setattr("graphify.workspace.adapters.v8.read_payload_file", unexpected)
    with pytest.raises(QueryRejected):
        if boundary == "library":
            query_structural(SimpleNamespace(validate_authority=unexpected), REPO_UUID, request)
        else:
            adapter().query_structural(-1, request)


def test_payload_permissions_ignore_restrictive_umask(tmp_path):
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    engine = adapter()
    initial = engine.observe(repo)
    payload = tmp_path / "payload"
    payload.mkdir(mode=0o700)
    fd = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
    previous = os.umask(0o277)
    try:
        engine.build_structural(repo, payload_fd=fd, scratch_fd=fd,
                                initial_detection=initial.initial_detection)
    finally:
        os.umask(previous)
        os.close(fd)
    assert {p.name: stat.S_IMODE(p.stat().st_mode) for p in payload.iterdir()} == {
        "graph.json": 0o600, "input-manifest.json": 0o600,
    }


@pytest.mark.parametrize("branch", [
    "feature+test", "feature@test", "feature/naïve",
    "feature/trailing\u00a0", "feature/line\u2028break",
])
@pytest.mark.parametrize("packed", [False, True])
def test_git_valid_branch_names_build_and_query(tmp_path, branch, packed):
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    git_output(repo, "check-ref-format", "--branch", branch)
    git_output(repo, "checkout", "-b", branch)
    if packed:
        git_output(repo, "pack-refs", "--all", "--prune")
    before = tree_snapshot(repo)
    engine = adapter()
    _build(engine, repo, tmp_path / "payload")
    fd = os.open(tmp_path / "payload", os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert "leaf" in engine.query_structural(fd, QueryRequest("caller"))
    finally:
        os.close(fd)
    assert tree_snapshot(repo) == before


@pytest.mark.parametrize("ref", [
    "refs/heads/../outside", "refs/heads/topic..bad",
    "refs/heads/topic.lock", "refs/heads/topic//bad",
])
def test_invalid_git_refs_refuse_before_target_read(tmp_path, monkeypatch, ref):
    from pathlib import Path
    from graphify.source_io import SourceIO, SourceUnsupported
    from graphify.workspace.adapters.v8 import _git_inputs
    from graphify.workspace.identity import discover_source

    repo = create_repo(tmp_path.resolve() / "repo")
    source = discover_source(repo)
    common = Path(source.registry_source["git_common_dir"])
    (repo / ".git/HEAD").write_text("ref: " + ref + "\n")
    with SourceIO(repo, extra_roots={"git": common}) as inputs:
        original = inputs.probe
        def probe(path):
            assert path != common / ref, "invalid ref reached a target probe"
            return original(path)
        monkeypatch.setattr(inputs, "probe", probe)
        with pytest.raises(SourceUnsupported, match="unsupported Git reference"):
            _git_inputs(inputs, source)


@pytest.mark.parametrize("scenario", ["umask", "branch"])
def test_reviewed_source_conditions_complete_and_promote(tmp_path, monkeypatch, scenario):
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    if scenario == "branch":
        git_output(repo, "checkout", "-b", "feature+test")
    request = request_for(runtime)
    source_before = tree_snapshot(repo)
    previous = os.umask(0o277) if scenario == "umask" else None
    try:
        result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    finally:
        if previous is not None:
            os.umask(previous)
    assert result.pointer_revision == 1
    state_before = tree_snapshot(runtime.inputs.state_root)
    assert "leaf" in query_structural(runtime, REPO_UUID, QueryRequest("caller"))
    assert tree_snapshot(runtime.inputs.state_root) == state_before
    assert tree_snapshot(repo) == source_before


def test_consumed_replay_has_separate_bounded_read_budget(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO, SourceUnsupported

    class SmallBudgetInputs(SourceIO):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("max_total_bytes", 20_000)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("graphify.workspace.adapters.v8.SourceIO", SmallBudgetInputs)
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    # Detection consumes this non-code input once; extraction only needs the small code.
    (repo / "notes.png").write_bytes(b"x" * 12_000)
    before = tree_snapshot(repo)
    request = request_for(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256="c" * 64)
    assert result.pointer_revision == 1
    state = tree_snapshot(runtime.inputs.state_root)
    assert "leaf" in query_structural(runtime, REPO_UUID, QueryRequest("caller"))
    assert tree_snapshot(runtime.inputs.state_root) == state
    assert tree_snapshot(repo) == before
    # Separate replay budgets must not relax the per-observation input ceiling.
    (repo / "notes.png").write_bytes(b"x" * 21_000)
    with pytest.raises(SourceUnsupported, match="input byte limit exceeded"):
        adapter().observe(repo)


def test_unrelated_non_utf8_packed_ref_does_not_break_observation(tmp_path):
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / "main.py").write_text("def leaf(): return 42\ndef caller(): return leaf()\n")
    # Git accepts opaque non-UTF-8 ref bytes on POSIX. Only the unrelated ref's
    # bytes live inside packed-refs; all consumed filesystem labels remain UTF-8.
    opaque = os.fsdecode(b"refs/heads/feature-\xff")
    git_output(repo, "check-ref-format", opaque)
    git_output(repo, "pack-refs", "--all", "--prune")
    packed = repo / ".git/packed-refs"
    rows = [line for line in packed.read_bytes().splitlines(keepends=True)
            if not line.startswith(b"#")]
    rows.append(git_output(repo, "rev-parse", "HEAD").encode("ascii")
                + b" " + os.fsencode(opaque) + b"\n")
    packed.write_bytes(b"".join(sorted(rows)))
    git_output(repo, "rev-parse", "HEAD")
    before = tree_snapshot(repo)
    engine = adapter()
    initial, built = _build(engine, repo, tmp_path / "payload")
    observed = engine.observe(repo, input_manifest=built.input_manifest)
    assert observed.initial_detection == initial.initial_detection
    assert observed.consumed_inputs == built.input_manifest
    assert tree_snapshot(repo) == before


def test_selected_non_utf8_ref_refuses_at_manifest_label_boundary(tmp_path):
    from graphify.source_io import SourceUnsupported

    repo = create_repo(tmp_path.resolve() / "repo")
    opaque = os.fsdecode(b"refs/heads/feature-\xff")
    git_output(repo, "check-ref-format", opaque)
    head = git_output(repo, "rev-parse", "HEAD").encode("ascii")
    (repo / ".git/packed-refs").write_bytes(head + b" " + os.fsencode(opaque) + b"\n")
    (repo / ".git/HEAD").write_bytes(b"ref: " + os.fsencode(opaque) + b"\n")
    assert git_output(repo, "rev-parse", "HEAD").encode("ascii") == head
    before = tree_snapshot(repo)
    with pytest.raises(SourceUnsupported, match="Git reference requires a canonical UTF-8 input label"):
        adapter().observe(repo)
    assert tree_snapshot(repo) == before
