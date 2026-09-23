"""Cache cleanup revalidates ownership at destructive boundaries."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import cache
from graphify.storage_guard import WORKSPACE_ROOT_MARKER


@pytest.mark.parametrize("refusal_at", ["preflight", "write", "io"])
def test_exit_flush_preserves_refused_output_without_traceback(tmp_path, refusal_at):
    output = tmp_path / "out"
    index = output / "cache" / "stat-index.json"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"protected index")
    result = subprocess.run(
        [sys.executable, "-c", """
import atexit
import sys
from pathlib import Path
from graphify import cache
from graphify.storage_guard import WORKSPACE_ROOT_MARKER

output = Path(sys.argv[1])
cache._GRAPHIFY_OUT = str(output)
cache._stat_index_root = output
cache._stat_index = {"pending": {"hash": "new"}}
cache._stat_index_dirty = True
if sys.argv[2] == "preflight":
    (output / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
elif sys.argv[2] == "write":
    original = cache.ordinary_atomic_bytes
    def mark_then_write(path, payload):
        (output / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
        original(path, payload)
    cache.ordinary_atomic_bytes = mark_then_write
else:
    def inaccessible(path):
        raise PermissionError("ancestry cannot be inspected")
    cache.require_ordinary_output = inaccessible
atexit.register(lambda: print(f"dirty={cache._stat_index_dirty}"))
atexit.register(cache._flush_stat_index)
""", str(output), refusal_at],
        cwd=Path(cache.__file__).resolve().parent.parent,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.strip() == "dirty=False"
    assert index.read_bytes() == b"protected index"
    if refusal_at != "io":
        assert (output / WORKSPACE_ROOT_MARKER).read_bytes() == b"deny"


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative deletion is POSIX-only")
@pytest.mark.parametrize("change", ["marker", "nested-marker", "replacement", "moved"])
def test_stale_tree_changed_after_preflight_is_preserved(tmp_path, monkeypatch, change):
    base = tmp_path / "ast"
    stale = base / "v-old"
    nested = stale / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.json").write_bytes(b"protected")
    original = cache._require_ordinary_cache_tree
    moved = tmp_path / "moved"

    def inspect_then_change(root):
        original(root)
        if change == "marker":
            (root / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
        elif change == "nested-marker":
            (nested / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
        else:
            root.rename(moved)
            (moved / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
            if change == "replacement":
                root.mkdir()
                (root / "replacement.json").write_bytes(b"preserve replacement")

    monkeypatch.setattr(cache, "_require_ordinary_cache_tree", inspect_then_change)
    monkeypatch.setattr(cache, "_cleaned_ast_dirs", set())
    with pytest.warns(UserWarning, match="Skipping stale cache cleanup"):
        cache._cleanup_stale_ast_entries(base, base / "v-current")
    protected = moved if change in {"replacement", "moved"} else stale
    assert (protected / "nested" / "payload.json").read_bytes() == b"protected"
    if change == "replacement":
        assert (stale / "replacement.json").read_bytes() == b"preserve replacement"


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative deletion is POSIX-only")
def test_stale_cleanup_removes_ordinary_nested_tree_and_not_symlink_target(tmp_path):
    root = tmp_path / "v-old"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "entry.json").write_bytes(b"ordinary")
    external = tmp_path / "external"
    external.mkdir()
    (external / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    (external / "payload").write_bytes(b"protected")
    (root / "link").symlink_to(external, target_is_directory=True)
    cache._remove_ordinary_cache_tree(root)
    assert not root.exists()
    assert (external / "payload").read_bytes() == b"protected"


def test_windows_recursive_cleanup_skips_but_removes_ordinary_json(tmp_path, monkeypatch):
    base = tmp_path / "ast"
    stale = base / "v-old"
    stale.mkdir(parents=True)
    (stale / "payload.json").write_bytes(b"retained")
    (base / "legacy.json").write_bytes(b"stale")
    monkeypatch.setattr(cache, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(cache, "_cleaned_ast_dirs", set())
    with pytest.warns(UserWarning, match="safe recursive cache cleanup is unavailable"):
        cache._cleanup_stale_ast_entries(base, base / "v-current")
    assert (stale / "payload.json").read_bytes() == b"retained"
    assert not (base / "legacy.json").exists()


@pytest.mark.parametrize("protected_kind", ["semantic", "semantic-deep"])
@pytest.mark.parametrize("late_marker", [False, True])
def test_semantic_prune_skips_protected_namespace_and_continues(
    tmp_path, monkeypatch, protected_kind, late_marker,
):
    monkeypatch.setattr(cache, "_GRAPHIFY_OUT", "out")
    base = tmp_path / "out" / "cache"
    for kind in ("semantic", "semantic-deep"):
        directory = base / kind
        directory.mkdir(parents=True)
        (directory / "orphan.json").write_bytes(b"payload")
    protected = base / protected_kind
    if late_marker:
        original = cache.ordinary_unlink

        def mark_then_unlink(path):
            if Path(path).parent == protected:
                (protected / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
            return original(path)

        monkeypatch.setattr(cache, "ordinary_unlink", mark_then_unlink)
    else:
        (protected / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
    with pytest.warns(UserWarning, match="Skipping semantic cache namespace"):
        assert cache.prune_semantic_cache(tmp_path, set()) == 1
    assert (protected / "orphan.json").read_bytes() == b"payload"
    other = "semantic-deep" if protected_kind == "semantic" else "semantic"
    assert not (base / other / "orphan.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative deletion is POSIX-only")
def test_marker_inserted_during_delete_walk_preserves_directory(tmp_path, monkeypatch):
    stale = tmp_path / "v-old"
    stale.mkdir()
    (stale / "payload.json").write_bytes(b"protected")
    original_preflight = cache._require_ordinary_cache_tree
    original_listdir = os.listdir
    preflight_done = False

    def preflight(root):
        nonlocal preflight_done
        original_preflight(root)
        preflight_done = True

    def list_then_mark(descriptor):
        names = original_listdir(descriptor)
        if preflight_done:
            (stale / WORKSPACE_ROOT_MARKER).write_bytes(b"deny")
        return names

    monkeypatch.setattr(cache, "_require_ordinary_cache_tree", preflight)
    monkeypatch.setattr(cache.os, "listdir", list_then_mark)
    monkeypatch.setattr(cache, "_cleaned_ast_dirs", set())
    with pytest.warns(UserWarning, match="Skipping stale cache cleanup"):
        cache._cleanup_stale_ast_entries(tmp_path, tmp_path / "v-current")
    assert (stale / "payload.json").read_bytes() == b"protected"


@pytest.mark.skipif(os.name == "nt", reason="symlink test requires POSIX")
def test_stale_version_symlink_does_not_delete_target(tmp_path, monkeypatch):
    base = tmp_path / "ast"
    base.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "payload.json").write_bytes(b"preserved")
    (base / "v-link").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(cache, "_cleaned_ast_dirs", set())
    cache._cleanup_stale_ast_entries(base, base / "v-current")
    assert (target / "payload.json").read_bytes() == b"preserved"
