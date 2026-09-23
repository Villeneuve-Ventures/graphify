"""S3 denial ownership and ordinary write refusal on disposable roots."""

from __future__ import annotations

import os
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


@pytest.mark.parametrize("operation", ["replace", "unlink", "atomic"])
def test_ordinary_mutations_accept_symlink_parent(tmp_path, operation):
    from graphify.storage_guard import ordinary_atomic_bytes, ordinary_replace, ordinary_unlink

    directory = tmp_path / "real"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    target = alias / "note"
    target.write_bytes(b"old")
    if operation == "replace":
        source = alias / "source"
        source.write_bytes(b"new")
        ordinary_replace(source, target)
        assert target.read_bytes() == b"new"
    elif operation == "atomic":
        ordinary_atomic_bytes(target, b"new")
        assert target.read_bytes() == b"new"
    else:
        ordinary_unlink(target)
        assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor fault injection")
@pytest.mark.parametrize("error", [13, 28, 30])
@pytest.mark.parametrize("boundary", ["root", "directory", "file", "mkdir"])
def test_ordinary_io_errors_preserve_errno(tmp_path, monkeypatch, error, boundary):
    import os
    import graphify.storage_guard as guard

    real_open, real_mkdir = os.open, os.mkdir
    target = tmp_path / "parent" / "note"
    if boundary != "mkdir":
        target.parent.mkdir()

    def failing_open(path, flags, *args, **kwargs):
        if ((boundary == "root" and str(path) == "/")
                or (boundary == "directory" and path == "parent")
                or (boundary == "file" and path == "note")):
            raise OSError(error, "injected")
        return real_open(path, flags, *args, **kwargs)

    def failing_mkdir(path, *args, **kwargs):
        if path == "parent":
            raise OSError(error, "injected")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    monkeypatch.setattr(os, "mkdir", failing_mkdir)
    with pytest.raises(OSError) as raised:
        if boundary == "mkdir":
            guard.ordinary_mkdir(target.parent)
        else:
            with guard.ordinary_open(target, "w"):
                pass
    assert raised.value.errno == error
    assert not target.exists()


def test_obsidian_pruning_preserves_nested_managed_state(tmp_path):
    import json

    vault = tmp_path / "vault"
    managed = vault / "retained"
    managed.mkdir(parents=True)
    (managed / WORKSPACE_ROOT_MARKER).write_text("denied")
    (managed / "stale.md").write_bytes(b"protected")
    (vault / ".graphify_obsidian_manifest.json").write_text(
        json.dumps({"files": ["retained/stale.md"]})
    )
    before = _tree(managed)
    to_obsidian(nx.Graph(), {}, str(vault))
    assert _tree(managed) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal")
def test_directory_admission_opens_scale_linearly(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    shallow = tmp_path / "first"
    deep = shallow.joinpath(*("level" for _ in range(10)))
    deep.mkdir(parents=True)
    real_open = guard.os.open
    calls = 0

    def counted_open(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(guard.os, "open", counted_open)
    with guard.ordinary_directory(shallow):
        pass
    shallow_calls = calls
    calls = 0
    with guard.ordinary_directory(deep):
        pass
    assert calls - shallow_calls <= 2 * 10


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor fault injection")
def test_directory_creation_rechecks_ancestors_before_mkdir(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    parent = tmp_path / "parent"
    existing = parent / "existing"
    existing.mkdir(parents=True)
    real_open = guard.os.open

    def mark_after_descent(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "existing":
            (parent / WORKSPACE_ROOT_MARKER).write_text("new ownership")
        return descriptor

    monkeypatch.setattr(guard.os, "open", mark_after_descent)
    with pytest.raises(ManagedWorkspaceOutputError):
        guard.ordinary_mkdir(existing / "new")
    assert not (existing / "new").exists()


def test_windows_locked_replace_never_falls_back_to_path_copy(tmp_path, monkeypatch):
    import shutil
    import graphify.storage_guard as guard

    real_os = guard.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

        def replace(self, *_args, **_kwargs):
            raise PermissionError("locked destination")

    monkeypatch.setattr(guard, "os", WindowsOS())
    monkeypatch.setattr(
        shutil, "copy2",
        lambda *_args, **_kwargs: pytest.fail("unsafe path-based copy fallback"),
    )
    target = tmp_path / "cache.json"
    target.write_bytes(b"old")

    with pytest.raises(PermissionError, match="locked destination"):
        guard.ordinary_atomic_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.json"]


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows branch uses POSIX symlinks")
def test_windows_open_checks_the_file_handle_before_truncation(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    real_os = guard.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(guard, "os", WindowsOS())
    monkeypatch.setattr(
        guard, "_create_windows_output",
        lambda path, flags: real_os.open(path, flags | real_os.O_CREAT | real_os.O_EXCL),
    )
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "graph.json").write_bytes(b"ordinary")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    protected = managed / "graph.json"
    protected.write_bytes(b"protected")
    original_admission = guard.require_ordinary_output
    switched = False

    def replace_parent_after_admission(path):
        nonlocal switched
        original_admission(path)
        if not switched:
            safe.rename(tmp_path / "retained")
            safe.symlink_to(managed, target_is_directory=True)
            switched = True

    monkeypatch.setattr(guard, "require_ordinary_output", replace_parent_after_admission)
    with pytest.raises(ManagedWorkspaceOutputError):
        with guard.ordinary_open(safe / "graph.json", "w") as stream:
            stream.write("overwritten")
    assert switched
    assert protected.read_bytes() == b"protected"
    assert (tmp_path / "retained" / "graph.json").read_bytes() == b"ordinary"


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows branch uses POSIX symlinks")
def test_windows_denied_creation_rolls_back_the_opened_file(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    real_os = guard.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(guard, "os", WindowsOS())
    monkeypatch.setattr(
        guard, "_create_windows_output",
        lambda path, flags: real_os.open(path, flags | real_os.O_CREAT | real_os.O_EXCL),
    )
    safe = tmp_path / "safe"
    safe.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    original_admission = guard.require_ordinary_output
    switched = False

    def replace_parent_after_admission(path):
        nonlocal switched
        original_admission(path)
        if not switched:
            safe.rename(tmp_path / "retained")
            safe.symlink_to(managed, target_is_directory=True)
            switched = True

    def delete_opened_file(descriptor):
        created = managed / "new.json"
        opened = real_os.fstat(descriptor)
        named = created.lstat()
        assert (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        created.unlink()

    monkeypatch.setattr(guard, "require_ordinary_output", replace_parent_after_admission)
    monkeypatch.setattr(guard, "_delete_opened_windows_output", delete_opened_file)
    with pytest.raises(ManagedWorkspaceOutputError):
        with guard.ordinary_open(safe / "new.json", "w") as stream:
            stream.write("unexpected")
    assert switched
    assert not (managed / "new.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows branch uses POSIX descriptors")
def test_windows_ordinary_creation_still_writes(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    real_os = guard.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(guard, "os", WindowsOS())
    monkeypatch.setattr(
        guard, "_create_windows_output",
        lambda path, flags: real_os.open(path, flags | real_os.O_CREAT | real_os.O_EXCL),
    )
    output = tmp_path / "ordinary.txt"
    with guard.ordinary_open(output, "w", encoding="utf-8") as stream:
        stream.write("ordinary")
    assert output.read_text() == "ordinary"


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows branch uses POSIX symlinks")
def test_windows_admission_denies_dangling_marker(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    real_os = guard.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(guard, "os", WindowsOS())
    output = tmp_path / "output"
    output.mkdir()
    marker = output / WORKSPACE_ROOT_MARKER
    marker.symlink_to(output / "missing")
    with pytest.raises(ManagedWorkspaceOutputError):
        guard.require_ordinary_output(output / "graph.json")
    marker.unlink()
    guard.require_ordinary_output(output / "graph.json")


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor fault injection")
@pytest.mark.parametrize("replacement", [False, True])
def test_ordinary_open_cleans_only_its_created_inode_after_admission_failure(
    tmp_path, monkeypatch, replacement,
):
    import graphify.storage_guard as guard

    safe = tmp_path / "safe"
    safe.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    real_open = guard.os.open
    moved = False

    def move_after_creation(path, flags, *args, **kwargs):
        nonlocal moved
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "new" and flags & os.O_EXCL and not moved:
            moved = True
            if replacement:
                (safe / "new").rename(safe / "original")
                (safe / "new").write_bytes(b"replacement")
            safe.rename(managed / "staging")
        return descriptor

    monkeypatch.setattr(guard.os, "open", move_after_creation)
    with pytest.raises(ManagedWorkspaceOutputError):
        with guard.ordinary_open(safe / "new", "w"):
            pass
    assert moved
    if replacement:
        assert (managed / "staging" / "new").read_bytes() == b"replacement"
        assert (managed / "staging" / "original").read_bytes() == b""
    else:
        assert not (managed / "staging" / "new").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor fault injection")
@pytest.mark.parametrize("winner", ["ordinary", "marked", "symlink"])
def test_directory_creation_validates_competing_winner(tmp_path, monkeypatch, winner):
    import graphify.storage_guard as guard

    parent = tmp_path / "parent"
    parent.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    real_mkdir = guard.os.mkdir

    def competing_mkdir(path, *args, **kwargs):
        if path == "new":
            if winner == "symlink":
                (parent / "new").symlink_to(managed, target_is_directory=True)
            else:
                real_mkdir(path, *args, **kwargs)
                if winner == "marked":
                    (parent / "new" / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
            raise FileExistsError("competing creator won")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(guard.os, "mkdir", competing_mkdir)
    if winner == "ordinary":
        assert guard.ordinary_mkdir(parent / "new") == parent / "new"
    else:
        with pytest.raises(ManagedWorkspaceOutputError):
            guard.ordinary_mkdir(parent / "new")
    assert not (managed / "output").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor enumeration")
def test_historical_workspace_probe_stops_at_first_matching_entry(tmp_path, monkeypatch):
    import graphify.storage_guard as guard

    root = tmp_path / "old"
    workspaces = root / "workspaces"
    historical = workspaces / str(uuid4()) / "generations"
    historical.mkdir(parents=True)
    for index in range(20):
        (workspaces / f"unrelated-{index}").mkdir()
    real_scandir = guard.os.scandir
    entries_seen = 0

    class CountedScan:
        def __init__(self, scan):
            self.scan = scan
            self.entries = iter(sorted(
                scan, key=lambda item: item.name != historical.parent.name,
            ))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.scan.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal entries_seen
            entries_seen += 1
            return next(self.entries)

    monkeypatch.setattr(guard.os, "scandir", lambda path: CountedScan(real_scandir(path)))
    with pytest.raises(ManagedWorkspaceOutputError):
        guard.require_ordinary_output(root / "output")
    assert entries_seen == 1
