"""Index publication preserves access and the saved objects needed by cancellation."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from graphify import hooks, merge_finalize as finalizer
from tests.test_merge_commit_lifecycle import graph_repo, git, isolated_authority  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX index publication")


def pending_repo(monkeypatch=None, *, linked=False):
    if linked:
        from tests import test_merge_commit_lifecycle as lifecycle

        assert monkeypatch is not None
        primary = lifecycle.init_repo(Path.home() / "primary")
        git(primary, "commit", "--allow-empty", "-m", "initial")
        git(primary, "branch", "-m", "holding")

        def linked_repo(path):
            git(primary, "worktree", "add", "-b", "main", str(path))
            return path

        monkeypatch.setattr(lifecycle, "init_repo", linked_repo)
    repo = graph_repo(Path.home(), manual=False)
    git(repo, "merge", "--no-commit", "side")
    hooks.install(repo, merge_guard=True)
    return repo


def cli(repo, *args):
    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-m", "graphify", "merge-finalize",
         "--output", "graphify-out", *args], cwd=repo, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def output_state(repo):
    output = repo / "graphify-out"
    return (git(repo, "rev-parse", "HEAD").stdout,
            finalizer._git_path(repo, "MERGE_HEAD").read_bytes(),
            {str(p.relative_to(output)): p.read_bytes()
             for p in output.rglob("*") if p.is_file()})


@pytest.mark.parametrize("operation", ["prepare", "cancel"])
def test_index_replacement_preserves_shared_access(operation):
    repo = pending_repo()
    git(repo, "config", "core.sharedRepository", "group")
    if operation == "cancel":
        cli(repo)
    index = repo / ".git/index"
    index.chmod(0o664)
    before = index.stat()
    protected = output_state(repo)
    cli(repo, *(("--cancel",) if operation == "cancel" else ()))
    after = index.stat()
    assert stat.S_IMODE(after.st_mode) == 0o664
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert output_state(repo) == protected


@pytest.mark.parametrize("pruning", ["now", "normal-aged"])
@pytest.mark.parametrize("linked", [False, True], ids=["primary", "linked"])
def test_cancellation_survives_git_gc(pruning, linked, monkeypatch):
    repo = pending_repo(monkeypatch, linked=linked)
    # The saved staging can differ from both the authenticated working copy
    # and all committed operands. Retain every saved blob, not only the graph.
    saved = b"operator-staged manifest\x00bytes"
    manifest_oid = subprocess.check_output(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=saved,
    ).decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo",
        f"100644,{manifest_oid},graphify-out/manifest.json")
    before = git(repo, "ls-files", "--stage", "-z").stdout
    protected = output_state(repo)
    graph_oid = git(repo, "rev-parse", ":graphify-out/graph.json").stdout.strip()
    cli(repo)
    captured = finalizer._read_record(finalizer._record_path(repo, "graphify-out"))
    assert captured is not None
    record = captured[0]
    ref, tree, _payload = finalizer._prior_object_root(repo, record)
    assert git(repo, "rev-parse", ref).stdout.strip() == tree
    collector = Path.home() / "primary" if linked else repo
    if linked:
        assert git(collector, "rev-parse", ref).stdout.strip() == tree
    if pruning == "normal-aged":
        # Model a long-lived pending merge, including young referring trees.
        age = time.time() - 21 * 24 * 3600
        objects = finalizer._git_path(repo, "objects")
        for bucket in objects.iterdir():
            if len(bucket.name) == 2 and bucket.is_dir():
                for path in bucket.iterdir():
                    if path.is_file():
                        os.utime(path, (age, age))
        git(collector, "gc")
    else:
        git(collector, "gc", "--prune=now")
    cli(repo, "--cancel")
    assert git(repo, "ls-files", "--stage", "-z").stdout == before
    assert git(repo, "cat-file", "-e", graph_oid).returncode == 0
    restored = subprocess.check_output(["git", "-C", str(repo), "show", ":graphify-out/manifest.json"])
    assert restored == saved
    assert output_state(repo) == protected
    assert not finalizer._record_path(repo, "graphify-out").exists()
    assert git(repo, "show-ref", "--verify", ref, check=False).returncode != 0


@pytest.mark.parametrize("kind", ["missing", "foreign", "symbolic"])
def test_retention_root_changes_preserve_foreign_state(kind):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo = pending_repo()
    cli(repo)
    path = finalizer._record_path(repo, "graphify-out")
    captured = finalizer._read_record(path)
    assert captured is not None
    record = captured[0]
    ref, oid, _tree = finalizer._prior_object_root(repo, record)
    if kind == "missing":
        git(repo, "update-ref", "-d", ref, oid)
    elif kind == "foreign":
        git(repo, "update-ref", ref, git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(), oid)
    else:
        git(repo, "update-ref", "-d", ref, oid)
        git(repo, "symbolic-ref", ref, "refs/heads/main")
    before = ((repo / ".git/index").read_bytes(), output_state(repo), path.read_bytes(),
              git(repo, "show-ref").stdout)
    with pytest.raises(MergeGuardError, match="retention root"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert ((repo / ".git/index").read_bytes(), output_state(repo), path.read_bytes(),
            git(repo, "show-ref").stdout) == before
    if kind == "missing":
        # Cancellation alone can reestablish the exact root after validating
        # every saved blob and the current private record/admission.
        cli(repo, "--cancel")
        assert not path.exists()
    else:
        with pytest.raises(finalizer.MergeFinalizeError, match="retention root"):
            finalizer.cancel_merge(repo, "graphify-out")
        assert ((repo / ".git/index").read_bytes(), output_state(repo), path.read_bytes(),
                git(repo, "show-ref").stdout) == before


@pytest.mark.parametrize("stage", ["record", "retention", "release"])
def test_interrupted_retention_order_can_resume(monkeypatch, stage):
    repo = pending_repo()
    before = git(repo, "ls-files", "--stage", "-z").stdout
    if stage == "release":
        finalizer.finalize_merge(repo, "graphify-out")
    target = {"record": "_retain_prior_objects", "retention": "os.replace",
              "release": "_remove_record"}[stage]

    def interrupted(*args, **kwargs):
        raise OSError("interrupted retention proof")

    with monkeypatch.context() as patch:
        owner, name = (finalizer.os, "replace") if target == "os.replace" else (finalizer, target)
        patch.setattr(owner, name, interrupted)
        with pytest.raises(finalizer.MergeFinalizeError, match="interrupted retention proof"):
            (finalizer.cancel_merge if stage == "release" else finalizer.finalize_merge)(repo, "graphify-out")
    record_path = finalizer._record_path(repo, "graphify-out")
    captured = finalizer._read_record(record_path)
    assert captured is not None
    record = captured[0]
    assert finalizer._retained_prior_objects(repo, record) == (stage == "retention")
    if stage != "release":
        assert git(repo, "ls-files", "--stage", "-z").stdout == before
        finalizer.finalize_merge(repo, "graphify-out")
    finalizer.cancel_merge(repo, "graphify-out")
    assert git(repo, "ls-files", "--stage", "-z").stdout == before
    assert not record_path.exists()
