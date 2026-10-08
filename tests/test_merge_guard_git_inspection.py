"""Git inspection must not hydrate missing promisor objects or invoke a fetch."""

import os

import pytest

from graphify.merge_guard import MergeGuardError, check_merge_commit
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX local transport fixture")


def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_mode,
                                          path.read_bytes())
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def test_missing_promisor_graph_refuses_without_fetch_or_repo_changes(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    git(repo, "commit", "--allow-empty", "-m", "base")
    git(repo, "config", "core.repositoryformatversion", "1")
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    git(repo, "config", "remote.origin.url", "guard-proof::local-test-only")

    # An unavailable promisor object is a valid partial-clone condition. The
    # synthetic transport records invocation and immediately fails, so this
    # regression can never reach a network or hydrate repository objects.
    missing = "d" * 40
    git(repo, "update-index", "--add", "--info-only", "--cacheinfo",
        f"100644,{missing},graphify-out/graph.json")
    (repo / ".git/MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD").stdout)
    tools = tmp_path / "transport"
    tools.mkdir()
    helper = tools / "git-remote-guard-proof"
    helper.write_text('#!/bin/sh\nprintf "fetch attempted\\n" > "$GRAPHIFY_PROMISOR_ATTEMPT"\nexit 1\n')
    helper.chmod(0o755)
    attempted = tmp_path / "fetch-attempted"
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GRAPHIFY_PROMISOR_ATTEMPT", str(attempted))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "guard-proof")
    monkeypatch.delenv("GIT_NO_LAZY_FETCH", raising=False)
    before = snapshot(repo)

    with pytest.raises(MergeGuardError, match="cannot inspect"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)

    assert not attempted.exists(), "read-only guard invoked the promisor fetch transport"
    assert snapshot(repo) == before
    assert not (repo / ".git/objects" / missing[:2] / missing[2:]).exists()
