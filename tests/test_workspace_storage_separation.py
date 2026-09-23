"""S3 denial ownership and ordinary write refusal on disposable roots."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import networkx as nx
import pytest

from graphify import cache, querylog
from graphify.export import to_canvas, to_cypher, to_graphml, to_html, to_json, to_obsidian
from graphify.storage_guard import (
    WORKSPACE_ROOT_MARKER, ManagedWorkspaceOutputError, ordinary_open,
    require_ordinary_output,
)
from graphify.transaction import ManagedAuthorityError, begin_transaction, pin_output, recover_transaction


def _tree(root: Path):
    return sorted((p.relative_to(root).as_posix(), p.lstat().st_mode,
                   p.lstat().st_ino, p.lstat().st_mtime_ns,
                   p.read_bytes() if p.is_file() else None)
                  for p in root.rglob("*"))


def test_all_direct_output_sinks_refuse_marked_state_without_changes(tmp_path, monkeypatch):
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    (state / WORKSPACE_ROOT_MARKER).write_text("corrupt marker still denies")
    staging = state / "workspaces" / str(uuid4()) / "staging"
    staging.mkdir(parents=True)
    (staging / "keep").write_bytes(b"protected")
    before = _tree(state)
    graph = nx.Graph()
    graph.add_node("a", label="a")
    outputs = (
        lambda: to_json(graph, {}, str(staging / "graph.json"), force=True),
        lambda: to_cypher(graph, str(staging / "graph.cypher")),
        lambda: to_graphml(graph, {}, str(staging / "graph.graphml")),
        lambda: to_html(graph, {}, str(staging / "graph.html")),
        lambda: to_canvas(graph, {}, str(staging / "graph.canvas")),
        lambda: to_obsidian(graph, {}, str(staging / "obsidian")),
        lambda: cache.cache_dir(state, "ast"),
    )
    for output in outputs:
        with pytest.raises(ManagedWorkspaceOutputError):
            output()
        assert _tree(state) == before
    source = tmp_path / "source.py"
    source.write_text("x = 1\n")
    monkeypatch.setattr(cache, "_GRAPHIFY_OUT", state)
    with pytest.raises(ManagedWorkspaceOutputError):
        cache.save_cached(source, {"nodes": [], "edges": []}, root=tmp_path)
    monkeypatch.setenv("GRAPHIFY_QUERY_LOG", str(staging / "queries.jsonl"))
    querylog.log_query(kind="query", question="x", corpus="test")
    assert _tree(state) == before
    with pytest.raises(ManagedAuthorityError):
        pin_output(staging, create=True)
    with pytest.raises(ManagedAuthorityError):
        begin_transaction("runtime", tmp_path, output=staging)
    with pytest.raises(ManagedAuthorityError):
        recover_transaction("runtime", tmp_path, output=staging)
    assert _tree(state) == before


def test_historical_layout_and_symlinked_ancestry_deny_writes(tmp_path):
    old = tmp_path.resolve() / "old"
    workspace = old / "workspaces" / str(uuid4())
    (workspace / "generations").mkdir(parents=True)
    (old / "registry.json").write_text("damaged")
    before = _tree(old)
    alias = tmp_path.resolve() / "alias"
    alias.symlink_to(workspace / "generations", target_is_directory=True)
    for target in (workspace / "generations" / "graph.json", alias / "graph.json"):
        with pytest.raises(ManagedWorkspaceOutputError):
            require_ordinary_output(target)
    assert _tree(old) == before


def test_ast_cache_cleanup_preserves_nested_managed_state(tmp_path):
    managed = tmp_path / "graphify-out" / "cache" / "ast" / "v-retained"
    managed.mkdir(parents=True)
    (managed / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    nested = managed / "workspaces" / str(uuid4()) / "staging"
    nested.mkdir(parents=True)
    (nested / "graph.json").write_bytes(b"protected")
    before = _tree(managed)

    cache.cache_dir(tmp_path, "ast")

    assert _tree(managed) == before


def test_ordinary_open_refuses_directory_moved_under_managed_root(tmp_path, monkeypatch):
    import graphify.storage_guard as storage_guard

    safe = tmp_path / "safe"
    safe.mkdir()
    protected = safe / "graph.json"
    protected.write_bytes(b"protected")
    original = protected.stat()
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    real_open = storage_guard.os.open
    moved = False

    def move_after_open(path, flags, *args, **kwargs):
        nonlocal moved
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "safe" and not moved:
            moved = True
            safe.rename(managed / "staging")
        return descriptor

    monkeypatch.setattr(storage_guard.os, "open", move_after_open)
    with pytest.raises(ManagedWorkspaceOutputError):
        with ordinary_open(protected, "w") as stream:
            stream.write("overwritten")
    after = managed / "staging" / "graph.json"
    assert moved
    assert after.read_bytes() == b"protected"
    assert (after.stat().st_ino, after.stat().st_mtime_ns) == (
        original.st_ino, original.st_mtime_ns,
    )


def test_export_staging_rejects_retargeted_parent_before_output(tmp_path, monkeypatch):
    import graphify.export as export

    safe = tmp_path / "safe"
    safe.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    before = _tree(state)
    alias = tmp_path / "alias"
    alias.symlink_to(safe, target_is_directory=True)
    stage = export.ordinary_temporary_file

    def retarget_then_stage(directory):
        alias.unlink()
        alias.symlink_to(state, target_is_directory=True)
        return stage(directory)

    monkeypatch.setattr(export, "ordinary_temporary_file", retarget_then_stage)
    with pytest.raises(ManagedWorkspaceOutputError):
        export.to_json(nx.Graph(), {}, str(alias / "graph.json"), force=True)
    assert _tree(state) == before


def test_failed_temporary_admission_removes_its_owned_name(tmp_path, monkeypatch):
    import graphify.storage_guard as storage_guard

    real_open = storage_guard.os.open
    real_binding = storage_guard._require_directory_binding
    created = False

    def create_then_fail(path, flags, *args, **kwargs):
        nonlocal created
        descriptor = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".graphify-stage-"):
            created = True
        return descriptor

    def deny_after_creation(path, descriptor):
        if created:
            raise ManagedWorkspaceOutputError("admission changed after creation")
        real_binding(path, descriptor)

    monkeypatch.setattr(storage_guard.os, "open", create_then_fail)
    monkeypatch.setattr(storage_guard, "_require_directory_binding", deny_after_creation)
    with pytest.raises(ManagedWorkspaceOutputError, match="admission changed"):
        storage_guard.ordinary_temporary_file(tmp_path)
    assert created
    assert list(tmp_path.glob(".graphify-stage-*")) == []


@pytest.mark.parametrize("retarget_at", [1, 2])
def test_export_rechecks_retargeted_symlink_before_open(tmp_path, monkeypatch, retarget_at):
    import graphify.export as export

    safe = tmp_path / "safe"
    safe.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    (state / "graph.json").write_bytes(b"protected")
    alias = tmp_path / "alias"
    alias.symlink_to(safe, target_is_directory=True)
    before = _tree(state)
    original = export.require_ordinary_output
    calls = 0

    def retarget_after_initial_check(path):
        nonlocal calls
        calls += 1
        original(path)
        if calls == retarget_at:
            alias.unlink()
            alias.symlink_to(state, target_is_directory=True)

    monkeypatch.setattr(export, "require_ordinary_output", retarget_after_initial_check)
    graph = nx.Graph()
    with pytest.raises(ManagedWorkspaceOutputError):
        export.to_json(graph, {}, str(alias / "graph.json"), force=True)
    assert calls >= retarget_at
    assert _tree(state) == before


