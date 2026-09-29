"""Repository content must not choose the Git executable used by workspace operations."""

import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from graphify.workspace.adapters.base import QueryRequest
from graphify.workspace.adapters.v8 import V8Adapter
from graphify.workspace._readonly import ReadOnlyFailure, run_readonly
from graphify.workspace.identity import SourceDiscoveryError, discover_source
from graphify.workspace.query import query_structural
from graphify.workspace.sync import synchronize_structural
from tests.test_workspace_structural_s4 import request_for, runtime_fixture
from tests.workspace_s3_helpers import REPO_UUID, create_repo, tree_snapshot


def _git_wrapper(path: Path, marker: Path, real_git: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"printf 'invoked\\n' >> '{marker}'\n"
        f"exec '{real_git}' \"$@\"\n"
    )
    path.chmod(0o755)


@pytest.mark.parametrize("candidate", ["empty", "dot", "relative_bin", "absolute_bin", "symlink"])
def test_discovery_ignores_checkout_owned_git_executable(tmp_path, monkeypatch, candidate):
    real_git = shutil.which("git")
    assert real_git is not None
    repo = create_repo(tmp_path.resolve() / "repo")
    marker = tmp_path / "wrapper-invoked"
    if candidate in {"empty", "dot"}:
        _git_wrapper(repo / "git", marker, real_git)
        entry = "" if candidate == "empty" else "."
    else:
        checkout_bin = repo / "bin"
        _git_wrapper(checkout_bin / "git", marker, real_git)
        if candidate == "relative_bin":
            entry = "bin"
        elif candidate == "absolute_bin":
            entry = str(checkout_bin)
        else:
            alias = tmp_path / "external-bin"
            alias.mkdir()
            (alias / "git").symlink_to(checkout_bin / "git")
            entry = str(alias)
    before = tree_snapshot(repo)
    monkeypatch.setenv("PATH", os.pathsep.join((entry, os.path.dirname(real_git), os.defpath)))

    assert discover_source(repo).head_commit
    assert not marker.exists(), "checkout-owned git executable ran during discovery"
    assert tree_snapshot(repo) == before


def test_preparation_ignores_checkout_owned_git_executable(tmp_path, monkeypatch):
    real_git = shutil.which("git")
    assert real_git is not None
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    marker = tmp_path / "wrapper-invoked"
    _git_wrapper(repo / "git", marker, real_git)
    before_source = tree_snapshot(repo)
    before_state = tree_snapshot(runtime.inputs.state_root)
    monkeypatch.setenv("PATH", os.pathsep.join((".", os.path.dirname(real_git), os.defpath)))

    request = request_for(runtime)

    assert request.build.observation_manifest_sha256
    assert not marker.exists(), "checkout-owned git executable ran during preparation"
    assert tree_snapshot(repo) == before_source
    assert tree_snapshot(runtime.inputs.state_root) == before_state


def test_certified_query_ignores_checkout_owned_git_executable(tmp_path, monkeypatch):
    real_git = shutil.which("git")
    assert real_git is not None
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    _git_wrapper(repo / "git", tmp_path / "wrapper-invoked", real_git)
    request = request_for(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256="a" * 64)
    assert result.pointer_revision == 1
    marker = tmp_path / "wrapper-invoked"
    marker.unlink(missing_ok=True)
    before_source = tree_snapshot(repo)
    before_state = tree_snapshot(runtime.inputs.state_root)
    monkeypatch.setenv("PATH", os.pathsep.join((".", os.path.dirname(real_git), os.defpath)))

    answer = query_structural(
        runtime, REPO_UUID, QueryRequest("caller"),
        deadline_ns=time.monotonic_ns() + 60_000_000_000,
    )

    assert "caller" in answer and "leaf" in answer
    assert not marker.exists(), "checkout-owned git executable ran during certified query"
    assert tree_snapshot(repo) == before_source
    assert tree_snapshot(runtime.inputs.state_root) == before_state


def test_discovery_accepts_external_absolute_custom_git(tmp_path, monkeypatch):
    real_git = shutil.which("git")
    assert real_git is not None
    repo = create_repo(tmp_path.resolve() / "repo")
    external_bin = tmp_path / "external-bin"
    marker = tmp_path / "wrapper-invoked"
    _git_wrapper(external_bin / "git", marker, real_git)
    before = tree_snapshot(repo)
    monkeypatch.setenv("PATH", os.pathsep.join((str(external_bin), os.path.dirname(real_git), os.defpath)))

    assert discover_source(repo).head_commit
    assert marker.exists(), "external custom Git should remain selectable"
    assert tree_snapshot(repo) == before


def test_discovery_refuses_when_only_checkout_git_is_available(tmp_path, monkeypatch):
    real_git = shutil.which("git")
    assert real_git is not None
    repo = create_repo(tmp_path.resolve() / "repo")
    marker = tmp_path / "wrapper-invoked"
    _git_wrapper(repo / "git", marker, real_git)
    before = tree_snapshot(repo)
    monkeypatch.setenv("PATH", str(repo))
    with pytest.raises(SourceDiscoveryError, match="no eligible Git executable"):
        discover_source(repo)
    assert not marker.exists()
    assert tree_snapshot(repo) == before


@pytest.mark.parametrize("route", ["common", "symlink"])
@pytest.mark.parametrize("operation", ["discovery", "timed_observation"])
def test_linked_worktree_excludes_common_directory_git(tmp_path, monkeypatch, route, operation):
    real_git = shutil.which("git")
    assert real_git is not None
    main = create_repo(tmp_path.resolve() / "main")
    linked = tmp_path.resolve() / "linked"
    subprocess.run([real_git, "-C", str(main), "worktree", "add", "--detach", str(linked)],
                   check=True, capture_output=True)
    expected = discover_source(linked).head_commit
    common = main / ".git"
    marker = tmp_path / "common-git-invoked"
    _git_wrapper(common / "bin/git", marker, real_git)
    candidate = common / "bin"
    if route == "symlink":
        candidate = tmp_path / "external-bin"
        candidate.mkdir()
        (candidate / "git").symlink_to(common / "bin/git")
    before = tree_snapshot(linked), tree_snapshot(common)
    monkeypatch.setenv("PATH", os.pathsep.join((str(candidate), os.path.dirname(real_git), os.defpath)))
    if operation == "discovery":
        assert discover_source(linked).head_commit == expected
    else:
        observed = V8Adapter().observe_lifecycle(
            linked, deadline_ns=time.monotonic_ns() + 10_000_000_000,
        )
        assert observed.source_commit == expected
    assert not marker.exists()
    assert (tree_snapshot(linked), tree_snapshot(common)) == before


def test_unpinned_readonly_worker_refuses_git(tmp_path):
    repo = create_repo(tmp_path.resolve() / "repo")
    with pytest.raises(ReadOnlyFailure, match="read-only computation failed"):
        run_readonly(
            "import subprocess; subprocess.run(['git', 'rev-parse', 'HEAD'], check=True)",
            b"", arguments=(repo,), deadline_ns=time.monotonic_ns() + 10_000_000_000,
            max_input_bytes=1024, max_output_bytes=65536,
        )


def test_pinned_readonly_git_survives_worker_path_change(tmp_path, monkeypatch):
    real_git = shutil.which("git")
    assert real_git is not None
    repo = create_repo(tmp_path.resolve() / "repo")
    expected = discover_source(repo).head_commit.encode()
    source_marker = tmp_path / "source-git-invoked"
    external_marker = tmp_path / "external-git-invoked"
    _git_wrapper(repo / "git", source_marker, real_git)
    external_bin = tmp_path / "external-bin"
    _git_wrapper(external_bin / "git", external_marker, real_git)
    monkeypatch.setenv("PATH", os.pathsep.join((str(external_bin), os.path.dirname(real_git), os.defpath)))
    before = tree_snapshot(repo)
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from graphify.workspace.identity import _git\n"
        "os.environ['PATH'] = '.:' + os.environ['PATH']\n"
        "print(_git(Path(sys.argv[1]), 'rev-parse', 'HEAD'))\n"
    )

    output = run_readonly(
        code, b"", arguments=(repo,), git_source_root=repo,
        deadline_ns=time.monotonic_ns() + 10_000_000_000,
        max_input_bytes=1024, max_output_bytes=1024,
    )

    assert output.strip() == expected
    assert external_marker.exists(), "worker did not use the selected external Git"
    assert not source_marker.exists(), "worker selected checkout Git after PATH changed"
    assert tree_snapshot(repo) == before


def test_pinned_readonly_worker_refuses_executable_override(tmp_path):
    real_git = shutil.which("git")
    assert real_git is not None
    repo = create_repo(tmp_path.resolve() / "repo")
    marker = tmp_path / "source-git-invoked"
    _git_wrapper(repo / "git", marker, real_git)
    before = tree_snapshot(repo)
    code = (
        "import subprocess, sys\n"
        "subprocess.run([sys.argv[2], 'rev-parse', 'HEAD'], "
        "executable=sys.argv[1], check=True)\n"
    )

    with pytest.raises(ReadOnlyFailure, match="read-only computation failed"):
        run_readonly(
            code, b"", arguments=(repo / "git", real_git), git_source_root=repo,
            deadline_ns=time.monotonic_ns() + 10_000_000_000,
            max_input_bytes=1024, max_output_bytes=65536,
        )
    assert not marker.exists()
    assert tree_snapshot(repo) == before
