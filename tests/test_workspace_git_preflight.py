"""Git discovery preflights object reads without walking for metadata commands."""

import os
import shutil
import subprocess

import pytest

from graphify.source_io import SourceIO
from graphify.workspace import identity
from tests.workspace_s3_helpers import create_repo


@pytest.mark.parametrize("route", [
    "loose", "ancestor", "packed", "symbolic", "fifo", "spaced_head", "spaced_loose",
])
def test_unsafe_selected_reference_refuses_before_any_git(tmp_path, monkeypatch, route):
    repo = create_repo(tmp_path.resolve() / "repo")
    git_dir = repo / ".git"
    branch = subprocess.check_output(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], text=True,
    ).strip()
    ref = git_dir / "refs" / "heads" / branch
    if route in {"symbolic", "spaced_loose"}:
        alias = git_dir / "refs" / "heads" / "alias"
        prefix = "ref:\t" if route == "spaced_loose" else "ref: "
        suffix = " \n" if route == "spaced_loose" else "\n"
        alias.write_text(f"{prefix}refs/heads/{branch}{suffix}")
        (git_dir / "HEAD").write_text("ref: refs/heads/alias\n")
        target = ref
    elif route == "spaced_head":
        (git_dir / "HEAD").write_text(f"ref:\trefs/heads/{branch} \n")
        target = ref
    elif route == "packed":
        subprocess.run(["git", "-C", str(repo), "pack-refs", "--all"], check=True)
        target = git_dir / "packed-refs"
    elif route == "ancestor":
        target = ref.parent
    else:
        target = ref
    outside = tmp_path / "outside-ref"
    if target.is_dir():
        shutil.copytree(target, outside)
        shutil.rmtree(target)
    else:
        if route != "fifo":
            shutil.copy2(target, outside)
        target.unlink()
    if route == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(outside)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Git started"),
    )
    with pytest.raises(identity.SourceDiscoveryError):
        identity.discover_source(repo)


def test_valid_symbolic_ref_chain_discovers_source(tmp_path):
    repo = create_repo(tmp_path.resolve() / "repo")
    git_dir = repo / ".git"
    branch = subprocess.check_output(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], text=True,
    ).strip()
    (git_dir / "refs" / "heads" / "alias").write_text(f"ref: refs/heads/{branch}\n")
    (git_dir / "HEAD").write_text("ref: refs/heads/alias\n")
    assert identity.discover_source(repo).head_commit


def test_linked_worktree_active_branch_uses_shared_ref(tmp_path, monkeypatch):
    repo = create_repo(tmp_path.resolve() / "repo")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "linked", str(linked)],
        check=True, capture_output=True,
    )
    assert identity.discover_source(linked).head_commit
    ref = repo / ".git" / "refs" / "heads" / "linked"
    outside = tmp_path / "outside-ref"
    shutil.copy2(ref, outside)
    ref.unlink()
    ref.symlink_to(outside)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Git started"),
    )
    with pytest.raises(identity.SourceDiscoveryError):
        identity.discover_source(linked)


def test_discovery_walks_object_tree_at_initial_read_and_final_checks(tmp_path, monkeypatch):
    repo = create_repo(tmp_path.resolve() / "repo")
    original_listdir = SourceIO.listdir
    object_walks = []

    def listdir(self, path):
        if path == repo / ".git" / "objects":
            object_walks.append(path)
        return original_listdir(self, path)

    monkeypatch.setattr(SourceIO, "listdir", listdir)
    identity.discover_source(repo)
    assert len(object_walks) == 3


def test_initial_unsafe_object_refuses_before_any_git(tmp_path, monkeypatch):
    repo = create_repo(tmp_path.resolve() / "repo")
    object_path = next((repo / ".git" / "objects").glob("??/*"))
    outside = tmp_path / "external-object"
    shutil.move(object_path, outside)
    object_path.symlink_to(outside)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Git started"),
    )
    with pytest.raises(identity.SourceDiscoveryError, match="unsafe Git object tree entry"):
        identity.discover_source(repo)


@pytest.mark.parametrize("change_after", ["--git-common-dir", "rev-list"])
def test_new_unsafe_object_route_refused_before_next_object_read(
    tmp_path, monkeypatch, change_after,
):
    repo = create_repo(tmp_path.resolve() / "repo")
    object_path = next((repo / ".git" / "objects").glob("??/*"))
    outside = tmp_path / "external-object"
    original_git = identity._git
    original_popen = subprocess.Popen
    changed = False
    commands = []

    def changing_git(root, *arguments, **kwargs):
        nonlocal changed
        result = original_git(root, *arguments, **kwargs)
        trigger = (
            arguments[:2] == ("rev-parse", "--git-common-dir")
            if change_after == "--git-common-dir"
            else arguments[0] == "rev-list"
        )
        if not changed and trigger:
            shutil.move(object_path, outside)
            object_path.symlink_to(outside)
            changed = True
        return result

    def popen(arguments, **kwargs):
        commands.append(arguments)
        return original_popen(arguments, **kwargs)

    monkeypatch.setattr(identity, "_git", changing_git)
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(identity.SourceDiscoveryError, match="unsafe Git object tree entry"):
        identity.discover_source(repo)
    assert changed
    assert sum(command[1] == "rev-list" for command in commands) == (
        0 if change_after == "--git-common-dir" else 1
    )
    assert not any(
        command[1:] == [
            "rev-parse", "--show-toplevel", "--git-common-dir", "--git-dir", "HEAD",
        ]
        for command in commands
    )
