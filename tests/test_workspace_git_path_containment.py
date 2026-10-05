"""Git containment keeps pathlib semantics and physical observations."""

import itertools
import os
from pathlib import PosixPath, PurePosixPath, PureWindowsPath

import pytest

from graphify.workspace import identity


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX path fast path")
def test_git_containment_matches_pathlib_components():
    parts = ("", "a", "b", "A", "a-b", "a_b", "..", ".", "é", "\u0301", " ")
    paths = {
        PosixPath(prefix + "/".join(values))
        for prefix in ("", "/", "//", "///")
        for count in range(3)
        for values in itertools.product(parts, repeat=count)
    }
    for path, parent in itertools.product(paths, repeat=2):
        assert identity._git_path_is_relative_to(path, parent) == path.is_relative_to(parent), (
            path, parent,
        )


@pytest.mark.parametrize("path,parent", [
    (PurePosixPath("/a/b"), PurePosixPath("/a")),
    (PureWindowsPath("C:/a/b"), PureWindowsPath("c:/A")),
    (PureWindowsPath("//server/share/a"), PureWindowsPath("//SERVER/SHARE")),
    (PureWindowsPath("C:/a/b"), PureWindowsPath("D:/a")),
])
def test_git_containment_other_path_flavours_keep_pathlib(path, parent):
    assert identity._git_path_is_relative_to(path, parent) == path.is_relative_to(parent)


@pytest.mark.skipif(os.name != "posix", reason="POSIX path subclass observations")
@pytest.mark.parametrize("subclass_operand", ["path", "parent", "both"])
@pytest.mark.parametrize("refuse", [False, True])
def test_git_containment_preserves_subclass_observations(subclass_operand, refuse):
    observations = []

    class ObservedPath(PosixPath):
        def is_relative_to(self, other):
            observations.append(("is_relative_to", str(self), str(other)))
            return super().is_relative_to(other)

        def __eq__(self, other):
            observations.append(("eq", str(self), str(other)))
            if refuse:
                raise RuntimeError("comparison refused")
            return super().__eq__(other)

        __hash__ = PosixPath.__hash__

    path = (ObservedPath if subclass_operand in {"path", "both"} else PosixPath)("/a/b")
    parent = (ObservedPath if subclass_operand in {"parent", "both"} else PosixPath)("/a")

    def outcome(operation):
        observations.clear()
        try:
            result = ("ok", operation())
        except RuntimeError as exc:
            result = (type(exc), str(exc))
        return result, list(observations)

    expected = outcome(lambda: path.is_relative_to(parent))
    assert outcome(lambda: identity._git_path_is_relative_to(path, parent)) == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and executable observations")
@pytest.mark.parametrize("pin", [None, "external", "source", "common"])
@pytest.mark.parametrize("move_alias", [False, True])
def test_git_containment_keeps_search_and_executable_observations(
    tmp_path, monkeypatch, pin, move_alias,
):
    repo = tmp_path.resolve() / "repo"
    common = repo / ".git"
    external = tmp_path / "external"
    sibling = tmp_path / "repo-suffix"
    for directory in (repo / "bin", common / "bin", external, sibling):
        directory.mkdir(parents=True)
    for directory in (repo / "bin", common / "bin", external):
        executable = directory / "git"
        executable.write_text("fixture executable")
        executable.chmod(0o755)
    ordinary_file = tmp_path / "file"
    ordinary_file.write_text("not a directory")
    alias = tmp_path / "alias"
    alias.symlink_to(repo / "bin", target_is_directory=True)
    cycle = tmp_path / "cycle"
    cycle.symlink_to(cycle)
    entries = ["", ".", "relative", str(repo / "bin"), str(common / "bin"),
               str(external), str(external), str(sibling), str(ordinary_file),
               str(tmp_path / "missing"), str(alias), str(cycle)]
    pinned = {None: None, "external": external / "git", "source": repo / "bin/git",
              "common": common / "bin/git"}[pin]
    resolve, is_dir = PosixPath.resolve, PosixPath.is_dir
    outcomes = []
    for reference in (True, False):
        alias.unlink()
        alias.symlink_to(repo / "bin", target_is_directory=True)
        trace = []

        def observed_resolve(path, *args, **kwargs):
            trace.append(("resolve", str(path), args, kwargs))
            if move_alias and sum(item[0] == "resolve" for item in trace) == 3:
                alias.unlink()
                alias.symlink_to(external, target_is_directory=True)
            return resolve(path, *args, **kwargs)

        def observed_is_dir(path, *args, **kwargs):
            trace.append(("is_dir", str(path), args, kwargs))
            return is_dir(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "get_exec_path", lambda: entries)
            patch.setattr(PosixPath, "resolve", observed_resolve)
            patch.setattr(PosixPath, "is_dir", observed_is_dir)
            patch.setattr(identity, "_PINNED_GIT_EXECUTABLE", str(pinned) if pinned else None)
            if reference:
                patch.setattr(identity, "_git_path_is_relative_to",
                              lambda path, parent: path.is_relative_to(parent))
            try:
                value = (identity._git_search_path(repo, common),
                         identity._git_executable(repo, git_common_dir=common))
                result = ("ok", value)
            except identity.SourceDiscoveryError as exc:
                result = (type(exc), str(exc))
        outcomes.append((result, trace))
    assert outcomes[0] == outcomes[1]
