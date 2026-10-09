"""Publication requires current hooks, prepared objects, and unambiguous paths."""
import json
import os
import sys

import pytest

from graphify import hooks
from graphify.merge_finalize import (
    MergeFinalizeError, _entries, _record_path, _source_blobs, cancel_merge,
    finalize_merge, validate_index_bundle,
)
from graphify.merge_guard import MergeGuardError, _git, check_merge_commit
from graphify.portable import PORTABLE_FILE, canonical_json, make_bundle, source_records_from_blobs
from tests.test_merge_commit_lifecycle import git, isolated_authority  # noqa: F401
from tests.test_merge_finalize import pending_repo
from tests.test_merge_guard import staged_repo

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX manual publication")


def guarded_repo():
    repo = pending_repo()
    hooks.install(repo, merge_guard=True)
    return repo


def preserved(repo):
    output = repo / "graphify-out"
    return ((repo / ".git/index").read_bytes(),
            {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()})


@pytest.mark.parametrize("name", ["pre-commit", "pre-merge-commit"])
@pytest.mark.parametrize("state", ["absent", "nonexecutable"])
def test_finalizer_requires_executable_current_guards(name, state):
    repo = guarded_repo()
    hook = repo / ".git/hooks" / name
    if state == "absent":
        hook.unlink()
    else:
        hook.chmod(0o644)
    before = preserved(repo)
    with pytest.raises(MergeFinalizeError, match="merge-guard"):
        finalize_merge(repo, "graphify-out")
    assert preserved(repo) == before
    assert not _record_path(repo, "graphify-out").exists()


@pytest.mark.parametrize("name", ["post-commit", "post-checkout", "post-merge"])
def test_finalizer_refuses_stale_managed_post_hooks(name):
    repo = guarded_repo()
    hook = repo / ".git/hooks" / name
    current = hook.read_text()
    assert hooks._PORTABLE_POST_EVENT_GUARD in current
    hook.write_text(current.replace(hooks._PORTABLE_POST_EVENT_GUARD, ""))
    before = preserved(repo)
    with pytest.raises(MergeFinalizeError, match="post.*hook"):
        finalize_merge(repo, "graphify-out")
    assert preserved(repo) == before
    assert not _record_path(repo, "graphify-out").exists()


@pytest.mark.parametrize("state", ["absent", "nonexecutable", "foreign-comment"])
def test_optional_post_hook_states_preserve_supported_manual_route(state):
    repo = guarded_repo()
    hook = repo / ".git/hooks/post-commit"
    if state == "absent":
        hook.unlink()
    elif state == "nonexecutable":
        hook.chmod(0o644)
    else:
        hook.write_text(hook.read_text() + "\n# user-maintained comment\n")
    output_before = preserved(repo)[1]
    finalize_merge(repo, "graphify-out")
    check_merge_commit("graphify-out", "pre-commit", root=repo)
    cancel_merge(repo, "graphify-out")
    assert preserved(repo)[1] == output_before


def test_post_hook_change_during_preparation_refuses_without_publication(monkeypatch):
    import graphify.merge_finalize as finalizer

    repo = guarded_repo()
    before = preserved(repo)
    extract = finalizer._extract
    def changed_hook(root, blobs):
        graph = extract(root, blobs)
        hook = repo / ".git/hooks/post-commit"
        hook.write_text(hook.read_text() + "\n# changed during preparation\n")
        return graph
    monkeypatch.setattr(finalizer, "_extract", changed_hook)
    with pytest.raises(MergeFinalizeError, match="hook inputs changed"):
        finalize_merge(repo, "graphify-out")
    assert preserved(repo) == before
    assert not _record_path(repo, "graphify-out").exists()


def test_guard_rechecks_post_hooks_after_preparation():
    repo = guarded_repo()
    finalize_merge(repo, "graphify-out")
    hook = repo / ".git/hooks/post-commit"
    hook.write_text(hook.read_text().replace(hooks._PORTABLE_POST_EVENT_GUARD, ""))
    before = preserved(repo)
    with pytest.raises(MergeGuardError, match="post-commit hook"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert preserved(repo) == before


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
@pytest.mark.parametrize("operation", ["finalize", "cancel"])
def test_output_index_flags_refuse_without_loss(flag, operation):
    repo = guarded_repo()
    if operation == "cancel":
        finalize_merge(repo, "graphify-out")
    git(repo, "update-index", "--" + flag, "graphify-out/graph.json")
    before = preserved(repo)
    before_flags = git(repo, "ls-files", "-v", "--", "graphify-out").stdout
    with pytest.raises(MergeFinalizeError, match="index flags"):
        (cancel_merge if operation == "cancel" else finalize_merge)(repo, "graphify-out")
    assert preserved(repo) == before
    assert git(repo, "ls-files", "-v", "--", "graphify-out").stdout == before_flags
    assert _record_path(repo, "graphify-out").exists() is (operation == "cancel")


def stage_unprepared_bundle(repo):
    sources = source_records_from_blobs(_source_blobs(repo, _entries(repo), "graphify-out"), "graphify-out")
    graph = {"directed": True, "multigraph": False, "graph": {},
             "nodes": [{"id": "invented", "label": "invented"}], "links": []}
    for name, payload in make_bundle(graph, sources, "graphify-out").items():
        oid = _git(repo, "hash-object", "-w", "--stdin", input=payload).decode().strip()
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},graphify-out/{name}")


@pytest.mark.parametrize("kind", ["unprepared", "replacement", "foreign-record"])
def test_guard_requires_current_prepared_bundle(kind):
    repo = guarded_repo()
    if kind != "unprepared":
        finalize_merge(repo, "graphify-out")
    if kind == "foreign-record":
        record = _record_path(repo, "graphify-out")
        data = json.loads(record.read_bytes())
        data["receipt_digest"] = "0" * 64
        record.write_bytes(canonical_json(data))
    else:
        stage_unprepared_bundle(repo)
    validate_index_bundle(repo, "graphify-out")  # Integrity alone is insufficient.
    before = preserved(repo)
    with pytest.raises(MergeGuardError, match="finalization|prepared"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert preserved(repo) == before
    head = git(repo, "rev-parse", "HEAD").stdout
    tree = git(repo, "write-tree").stdout
    result = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
    assert result.returncode != 0
    assert "finalization" in result.stderr or "prepared" in result.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == head
    assert git(repo, "write-tree").stdout == tree
    assert (repo / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("graph_present", [False, True])
@pytest.mark.parametrize("event", ["pre-commit", "pre-merge-commit"])
@pytest.mark.parametrize("path", ["graphify-out/.Graphify_Portable.json",
                                  "graphify-out/.graphify_portable.jſon",
                                  "Graphify-Out/.graphify_portable.json"])
def test_guard_rejects_case_alias_envelope(tmp_path, graph_present, event, path):
    repo, graph = staged_repo(tmp_path, {"nodes": [], "links": []})
    oid = _git(repo, "hash-object", "-w", "--stdin", input=b"{}").decode().strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}")
    if not graph_present:
        git(repo, "update-index", "--force-remove", "graphify-out/graph.json")
    hooks.install(repo, merge_guard=True)
    before = preserved(repo)
    with pytest.raises(MergeGuardError, match="alias"):
        check_merge_commit("graphify-out", event, root=repo)
    assert preserved(repo) == before
    # Exercise the generated shell hook too, including its graph-absent fast path.
    import subprocess
    result = subprocess.run([str(repo / ".git/hooks" / event)], cwd=repo,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "alias" in result.stderr
    assert preserved(repo) == before


@pytest.mark.parametrize("event", ["pre-commit", "pre-merge-commit"])
def test_graph_absent_ordinary_output_sibling_keeps_legacy_route(tmp_path, event):
    import subprocess

    repo, graph = staged_repo(tmp_path, {"nodes": [], "links": []})
    git(repo, "update-index", "--force-remove", "graphify-out/graph.json")
    (graph.parent / "notes.md").write_text("ordinary sibling\n")
    git(repo, "add", "graphify-out/notes.md")
    hooks.install(repo, merge_guard=True)
    before = preserved(repo)
    result = subprocess.run([str(repo / ".git/hooks" / event)], cwd=repo,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert preserved(repo) == before


def test_shell_marker_candidates_cover_nonascii_casefold_aliases():
    script = hooks._merge_guard_script("pre-merge-commit", "graphify-out", sys.executable)
    for codepoint in range(128, sys.maxunicode + 1):
        character = chr(codepoint)
        folded = character.casefold()
        if folded and folded != character and folded in PORTABLE_FILE:
            alias = PORTABLE_FILE.replace(folded, character)
            assert alias in script
