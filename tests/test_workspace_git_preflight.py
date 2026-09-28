"""Git discovery preflights object reads without walking for metadata commands."""

import shutil
import subprocess

import pytest

from graphify.source_io import SourceIO
from graphify.workspace import identity
from tests.workspace_s3_helpers import create_repo


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
