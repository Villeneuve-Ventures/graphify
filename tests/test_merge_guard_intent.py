"""Commit guards distinguish intent-to-add placeholders from staged blobs."""

import os
import subprocess

import pytest

from graphify.merge_guard import MergeGuardError, check_merge_commit
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX merge guard qualification")
GRAPH = "graphify-out/graph.json"


def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_mode,
                                          path.read_bytes())
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def candidate_paths(repo):
    empty = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-t", "tree", "--stdin"],
        input=b"", check=True, capture_output=True,
    ).stdout.decode().strip()
    return git(repo, "--no-lazy-fetch", "--no-optional-locks", "-c", "core.fsmonitor=false",
               "diff", "--cached", "--ita-invisible-in-index", "--name-only", "-z",
               "--no-ext-diff", "--no-textconv", "--no-renames", empty,
               "--", f":(top,literal){GRAPH}").stdout


def prepare_repo(tmp_path, monkeypatch, *, predecessor, alternate, intent, absent):
    repo = init_repo(tmp_path / "repo")
    graph = repo / GRAPH
    graph.parent.mkdir()
    if predecessor:
        graph.write_text('{"graph":{},"nodes":[],"links":[]}')
        git(repo, "add", GRAPH)
    git(repo, "commit", "--allow-empty", "-m", "base")
    if alternate:
        selected = repo / ".git/guard-alternate-index"
        primary = repo / ".git/index"
        if primary.exists():
            selected.write_bytes(primary.read_bytes())
        monkeypatch.setenv("GIT_INDEX_FILE", str(selected))
    if predecessor:
        git(repo, "rm", "--cached", GRAPH)
    graph.write_text("not staged and must not be parsed" if intent else "")
    git(repo, "add", *(["-N"] if intent else []), "--", GRAPH)
    if absent:
        graph.unlink()
    (repo / ".git/MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD").stdout)
    return repo


@pytest.mark.parametrize("predecessor", [False, True])
@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("event", ["pre-commit", "pre-merge-commit"])
def test_intent_to_add_is_omitted_without_reading_or_changing_worktree(
    tmp_path, monkeypatch, predecessor, alternate, absent, event,
):
    repo = prepare_repo(tmp_path, monkeypatch, predecessor=predecessor,
                        alternate=alternate, intent=True, absent=absent)
    assert GRAPH in git(repo, "ls-files", "--stage", "--", GRAPH).stdout
    assert candidate_paths(repo) == ""
    before = snapshot(repo)
    check_merge_commit("graphify-out", event, root=repo)
    assert snapshot(repo) == before


@pytest.mark.parametrize("predecessor", [False, True])
@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("absent", [False, True])
def test_actual_empty_staged_blob_still_refuses(tmp_path, monkeypatch, predecessor, alternate, absent):
    repo = prepare_repo(tmp_path, monkeypatch, predecessor=predecessor,
                        alternate=alternate, intent=False, absent=absent)
    assert candidate_paths(repo) == GRAPH + "\0"
    before = snapshot(repo)
    with pytest.raises(MergeGuardError, match="unsupported graph size"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert snapshot(repo) == before


def test_unresolved_index_entries_still_refuse(tmp_path, monkeypatch):
    repo = prepare_repo(tmp_path, monkeypatch, predecessor=False, alternate=True,
                        intent=False, absent=True)
    oid = git(repo, "ls-files", "--stage", "--", GRAPH).stdout.split()[1]
    stages = f"0 {'0' * len(oid)}\t{GRAPH}\n" + "".join(
        f"100644 {oid} {stage}\t{GRAPH}\n" for stage in (1, 2, 3)
    )
    subprocess.run(["git", "-C", str(repo), "update-index", "--index-info"],
                   input=stages, text=True, capture_output=True, check=True)
    before = snapshot(repo)
    with pytest.raises(MergeGuardError, match="unsafe index entry"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert snapshot(repo) == before


@pytest.mark.parametrize("intent", [False, True])
def test_candidate_inventory_uses_repository_object_format(tmp_path, monkeypatch, intent):
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    repo = prepare_repo(tmp_path, monkeypatch, predecessor=True, alternate=True,
                        intent=intent, absent=True)
    assert git(repo, "rev-parse", "--show-object-format").stdout.strip() == "sha256"
    before = snapshot(repo)
    if intent:
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    else:
        with pytest.raises(MergeGuardError, match="unsupported graph size"):
            check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert snapshot(repo) == before
