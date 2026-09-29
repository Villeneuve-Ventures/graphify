"""Git routing is admitted before discovery consumes repository metadata."""

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from graphify.workspace import identity
from graphify.workspace.adapters.v8 import V8Adapter, _git_inputs
from graphify.source_io import SourceIO, SourceChanged
from tests.workspace_s3_helpers import create_repo


def _guard_metadata_reads(monkeypatch):
    reads = []
    original = identity._read_source_regular

    def guarded(root, relative, **kwargs):
        reads.append((Path(root), str(relative)))
        if str(relative) not in {".git", "commondir", "gitdir"}:
            pytest.fail(f"Git metadata read before routing admission: {relative}")
        return original(root, relative, **kwargs)

    monkeypatch.setattr(identity, "_read_source_regular", guarded)
    for method in ("probe", "read_bytes", "listdir"):
        def guarded_source(self, path):
            pytest.fail(f"Git ref/object metadata touched before routing admission: {path}")

        monkeypatch.setattr(SourceIO, method, guarded_source)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("Git started"))
    return reads


@pytest.mark.parametrize("route", [
    "separate", "forged_commondir", "forged_backlink", "symlink_gitdir",
])
def test_unsupported_linked_route_refuses_before_metadata_or_git(tmp_path, monkeypatch, route):
    repo = create_repo(tmp_path.resolve() / "repo")
    linked = tmp_path.resolve() / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(linked)],
                   check=True, capture_output=True)
    git_dir = repo / ".git" / "worktrees" / "linked"
    if route == "separate":
        (linked / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    elif route == "forged_commondir":
        (git_dir / "commondir").write_text(str(tmp_path.resolve() / "other") + "\n")
    elif route == "forged_backlink":
        (git_dir / "gitdir").write_text(str(repo / ".git") + "\n")
    else:
        alias = repo / ".git" / "worktrees" / "alias"
        alias.symlink_to(git_dir, target_is_directory=True)
        (linked / ".git").write_text(f"gitdir: {alias}\n")
    reads = _guard_metadata_reads(monkeypatch)
    with pytest.raises(identity.SourceDiscoveryError):
        identity.discover_source(linked)
    assert all(name in {".git", "commondir", "gitdir"} for _, name in reads)


def test_ordinary_git_directory_refuses_commondir_route_before_metadata(tmp_path, monkeypatch):
    repo = create_repo(tmp_path.resolve() / "repo")
    (repo / ".git" / "commondir").write_text(str(tmp_path.resolve() / "other") + "\n")
    _guard_metadata_reads(monkeypatch)
    with pytest.raises(identity.SourceDiscoveryError):
        identity.discover_source(repo)


@pytest.mark.parametrize("route", ["relative", "absolute"])
def test_standard_linked_route_discovery(tmp_path, route):
    repo = create_repo(tmp_path.resolve() / "repo")
    linked = tmp_path.resolve() / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(linked)],
                   check=True, capture_output=True)
    git_dir = repo / ".git" / "worktrees" / "linked"
    if route == "absolute":
        (linked / ".git").write_text(f"gitdir: {git_dir}\n")
        (git_dir / "commondir").write_text(str(repo / ".git") + "\n")
    else:
        (linked / ".git").write_text(f"gitdir: {os.path.relpath(git_dir, linked)}\n")
    assert identity.discover_source(linked).head_commit
    assert V8Adapter().observe(linked).stable_inventory_passes == 2


def _rewrite_symbolic_ref(repo, location, separator):
    expected = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).strip()
    head = repo / '.git/HEAD'
    target = head.read_bytes()[4:].strip()
    if location == 'loose':
        selected = repo / '.git' / os.fsdecode(target)
        target = b'refs/heads/whitespace-target'
        (repo / '.git' / os.fsdecode(target)).write_bytes(expected + b'\n')
    else:
        selected = head
    selected.write_bytes(b'ref:' + separator + target + b' \n')
    assert subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).strip() == expected
    return expected.decode('ascii')


@pytest.mark.parametrize('location', ['HEAD', 'loose'])
@pytest.mark.parametrize('separator', [b'\t', b'  '])
def test_adapter_accepts_git_symbolic_ref_whitespace(tmp_path, location, separator):
    repo = create_repo(tmp_path.resolve() / 'repo')
    expected = _rewrite_symbolic_ref(repo, location, separator)
    assert identity.discover_source(repo).head_commit == expected
    assert V8Adapter().observe_lifecycle(repo).source_commit == expected


@pytest.mark.parametrize('location', ['loose', 'packed', 'detached'])
def test_adapter_accepts_uppercase_git_oid_with_raw_evidence(tmp_path, location):
    repo = create_repo(tmp_path.resolve() / 'repo')
    expected = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).strip()
    upper = expected.upper()
    head = repo / '.git/HEAD'
    if location == 'loose':
        ref = head.read_bytes()[4:].strip().decode('ascii')
        selected = repo / '.git' / ref
        selected.write_bytes(upper + b'\n')
    elif location == 'packed':
        ref = head.read_bytes()[4:].strip()
        selected = repo / '.git/packed-refs'
        (repo / '.git' / ref.decode('ascii')).unlink()
        selected.write_bytes(b'# pack-refs with: peeled fully-peeled sorted\n' + upper + b' ' + ref + b'\n')
    else:
        selected = head
        selected.write_bytes(upper + b'\n')
    assert subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).strip() == expected
    observed = V8Adapter().observe_lifecycle(repo)
    assert observed.source_commit == expected.decode('ascii')
    label = 'git:' + selected.relative_to(repo / '.git').as_posix()
    reads = [e for e in observed.structural.initial_detection.to_dict()['evidence']
             if e['operation'] == 'read' and e['path'] == label]
    assert len(reads) == 1
    assert reads[0]['value'][1] == hashlib.sha256(selected.read_bytes()).hexdigest()


def test_adapter_rejects_different_git_oid(tmp_path):
    repo = create_repo(tmp_path.resolve() / 'repo')
    source = identity.discover_source(repo)
    (repo / 'main.py').write_text('answer = 43\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'main.py'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=.git/disabled-hooks',
                    'commit', '--quiet', '--no-gpg-sign', '-m', 'next'],
                   check=True, capture_output=True)
    assert identity.discover_source(repo).head_commit != source.head_commit
    with SourceIO(repo, extra_roots={'git': repo / '.git'}) as inputs:
        with pytest.raises(SourceChanged, match='Git HEAD changed'):
            _git_inputs(inputs, source)


def test_linked_worktree_local_reference_is_observed(tmp_path):
    repo = create_repo(tmp_path.resolve() / 'repo')
    linked = tmp_path.resolve() / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(linked)],
                   check=True, capture_output=True)
    git_dir = repo / '.git/worktrees/linked'
    commit = (git_dir / 'HEAD').read_bytes().strip()
    local_ref = git_dir / 'refs/worktree/active'
    local_ref.parent.mkdir(parents=True)
    local_ref.write_bytes(commit + b'\n')
    (git_dir / 'HEAD').write_bytes(b'ref: refs/worktree/active\n')
    assert subprocess.check_output(['git', '-C', str(linked), 'rev-parse', 'HEAD']).strip() == commit
    assert identity.discover_source(linked).head_commit == commit.decode('ascii')
    assert V8Adapter().observe_lifecycle(linked).source_commit == commit.decode('ascii')


def test_symbolic_ref_whitespace_structural_round_trip(tmp_path, monkeypatch):
    import time
    from graphify.workspace.adapters.base import QueryRequest
    from graphify.workspace.query import query_structural
    from graphify.workspace.sync import synchronize_structural
    from tests.test_workspace_structural_s4 import runtime_fixture, request_for
    from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot

    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    _rewrite_symbolic_ref(repo, 'loose', b'\t')
    before_source = tree_snapshot(repo)
    request = request_for(runtime)
    result = synchronize_structural(runtime, request, attempt_sha256='a' * 64)
    assert result.pointer_revision == 1
    before_state = tree_snapshot(runtime.inputs.state_root)
    answer = query_structural(runtime, REPO_UUID, QueryRequest('caller'),
                              deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert 'caller' in answer and 'leaf' in answer
    assert tree_snapshot(repo) == before_source
    assert tree_snapshot(runtime.inputs.state_root) == before_state
