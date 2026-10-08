"""Opt-in guard batches preserve installer recovery and existing user hooks."""

import os
import stat

import pytest

from graphify import hooks
from tests.test_merge_commit_lifecycle import init_repo, isolated_authority  # noqa: F401

if os.name != "nt":
    from graphify import hook_installation as publication


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX atomic guard installer")
USER_HOOK = b"#!/bin/sh\nprintf 'existing user hook\\n'\n"


@pytest.fixture
def repo(tmp_path):
    root = init_repo(tmp_path / "repo")
    for name in publication.NAMES:
        target = root / ".git/hooks" / name
        target.write_bytes(USER_HOOK)
        target.chmod(0o751)
    return root


def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_mode,
                                          path.read_bytes())
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def live(root):
    directory = root / ".git/hooks"
    return {name: ((directory / name).read_bytes(),
                   stat.S_IMODE((directory / name).stat().st_mode))
            if (directory / name).exists() else None
            for name in publication.MERGE_GUARD_NAMES}


def interrupt(root, monkeypatch, operation, *, merge_guard, after):
    original = publication._apply
    published = []

    def apply_then_interrupt(fd, stage_fd, entry, publish=True):
        original(fd, stage_fd, entry, publish=publish)
        if publish:
            published.append(entry["name"])
            if len(published) == after:
                raise OSError("injected interruption after hook publication")

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_apply", apply_then_interrupt)
        with pytest.raises(RuntimeError, match="injected interruption"):
            getattr(hooks, operation)(root, merge_guard=merge_guard)
    expected = publication.MERGE_GUARD_NAMES if merge_guard else publication.NAMES
    assert published == list(expected[:after])


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_five_hook_interruption_resumes_exact_request(repo, monkeypatch, operation):
    if operation == "uninstall":
        hooks.install(repo, merge_guard=True)
    original = live(repo)
    original_config = (repo / ".git/config").read_bytes()
    attrs = repo / ".gitattributes"
    original_attrs = attrs.read_bytes() if attrs.exists() else None
    interrupt(repo, monkeypatch, operation, merge_guard=True, after=4)

    partial = live(repo)
    assert partial["pre-merge-commit"] != original["pre-merge-commit"]
    assert partial["pre-commit"] == original["pre-commit"]
    assert (repo / ".git/config").read_bytes() == original_config
    assert (attrs.read_bytes() if attrs.exists() else None) == original_attrs
    before_status = (snapshot(repo), snapshot(publication._state_root()))
    assert f"pending {operation}" in hooks.status(repo, merge_guard=True)
    assert before_status == (snapshot(repo), snapshot(publication._state_root()))

    # A default command cannot silently adopt a partially published opt-in batch.
    with pytest.raises(RuntimeError, match="different request"):
        getattr(hooks, operation)(repo)
    assert before_status == (snapshot(repo), snapshot(publication._state_root()))
    getattr(hooks, operation)(repo, merge_guard=True)
    assert "pending" not in hooks.status(repo, merge_guard=True)
    finished = live(repo)
    for name in publication.NAMES:
        assert finished[name][0].startswith(USER_HOOK)
        assert finished[name][1] == 0o751
    for name in ("pre-merge-commit", "pre-commit"):
        if operation == "install":
            assert finished[name] is not None
            assert finished[name][1] & 0o111
        else:
            assert finished[name] is None
    if operation == "uninstall":
        assert all(b"graphify-" not in finished[name][0] for name in publication.NAMES)
    # Exact hook publication retries preserve live inodes and recovery evidence.
    # Existing driver registration can replace Git's config with identical bytes.
    before_retry = (snapshot(repo / ".git/hooks"), snapshot(publication._state_root()))
    config_before_retry = (repo / ".git/config").read_bytes()
    getattr(hooks, operation)(repo, merge_guard=True)
    assert before_retry == (snapshot(repo / ".git/hooks"), snapshot(publication._state_root()))
    assert (repo / ".git/config").read_bytes() == config_before_retry


def test_legacy_pending_batch_requires_default_retry_before_opt_in(repo, monkeypatch):
    interrupt(repo, monkeypatch, "install", merge_guard=False, after=1)
    before = (snapshot(repo), snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="different request"):
        hooks.install(repo, merge_guard=True)
    assert before == (snapshot(repo), snapshot(publication._state_root()))
    hooks.install(repo)
    assert "pending" not in hooks.status(repo)
    completed_old = live(repo)
    assert completed_old["pre-merge-commit"] is None
    assert completed_old["pre-commit"] is None
    # The completed three-hook history remains valid when adding the guards.
    hooks.install(repo, merge_guard=True)
    completed_new = live(repo)
    for name in publication.NAMES:
        assert completed_new[name] == completed_old[name]
    for name in ("pre-merge-commit", "pre-commit"):
        assert f"{name}: installed" in hooks.status(repo, merge_guard=True)
    hooks.uninstall(repo, merge_guard=True)
    assert "pending" not in hooks.status(repo, merge_guard=True)


@pytest.mark.parametrize("name", ["pre-merge-commit", "pre-commit"])
@pytest.mark.parametrize("kind", ["foreign", "malformed", "outside-owned"])
def test_inadmissible_prehook_refuses_before_any_live_publication(repo, name, kind):
    start, end = hooks._merge_guard_markers(name)
    bodies = {
        "foreign": "#!/usr/bin/env python3\nraise SystemExit(0)\n",
        "malformed": f"#!/bin/sh\n{start}\nexit 0\n",
        "outside-owned": f"#!/bin/sh\necho user\n{start}\nexit 0\n{end}\n",
    }
    target = repo / ".git/hooks" / name
    target.write_text(bodies[kind])
    target.chmod(0o751)
    before = snapshot(repo)
    with pytest.raises(RuntimeError, match="Malformed|Cannot compose"):
        hooks.install(repo, merge_guard=True)
    assert snapshot(repo) == before
    assert not list((repo / ".git/hooks").glob(publication._PREFIX + "*"))
    assert "pending" not in hooks.status(repo)
