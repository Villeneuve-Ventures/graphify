"""Canonical guard selectors and linked postevents preserve hook boundaries."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify import hooks, paths, portable
from graphify.merge_finalize import finalize_merge, MergeFinalizeError
from tests.test_merge_commit_lifecycle import git, isolated_authority  # noqa: F401
from tests.test_merge_finalize import pending_repo
from tests.test_merge_finalize_observer import repository, LEGACY, PORTABLE

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX portable hook proof")


@pytest.mark.parametrize("output", ["graphify-out/", "custom-output///"])
def test_guard_install_uses_one_canonical_selector(tmp_path, monkeypatch, output):
    repo = repository(tmp_path)
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", output)
    hooks.install(repo, merge_guard=True)
    canonical = output.rstrip("/")
    expected = hooks._post_event_scripts(canonical, canonical, sys.executable)
    for name, script in expected.items():
        assert (repo / ".git/hooks" / name).read_bytes() == ("#!/bin/sh\n" + script).encode()
    for name in hooks._MERGE_GUARD_HOOKS:
        expected_guard = hooks._merge_guard_script(name, canonical, sys.executable)
        assert (repo / ".git/hooks" / name).read_bytes() == ("#!/bin/sh\n" + expected_guard).encode()
    before = {name: (repo / ".git/hooks" / name).read_bytes()
              for name in (*expected, *hooks._MERGE_GUARD_HOOKS)}
    hooks.install(repo, merge_guard=True)
    assert before == {name: (repo / ".git/hooks" / name).read_bytes() for name in before}


@pytest.mark.parametrize("absolute", [False, True])
def test_ordinary_install_preserves_legacy_selector(tmp_path, monkeypatch, absolute):
    repo = repository(tmp_path)
    output = str(tmp_path / "shared-output") + "/" if absolute else "graphify-out/"
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", output)
    hooks.install(repo)
    expected = hooks._post_event_scripts(output, "" if absolute else "graphify-out", sys.executable)
    for name, script in expected.items():
        assert (repo / ".git/hooks" / name).read_bytes() == ("#!/bin/sh\n" + script).encode()


def test_trailing_slash_cli_guard_install_admits_exact_hooks():
    repo = pending_repo()
    output = repo / "graphify-out"
    before_output = {str(p.relative_to(output)): (p.read_bytes(), p.stat().st_mode)
                     for p in output.rglob("*") if p.is_file()}
    before_head = git(repo, "rev-parse", "HEAD").stdout
    before_merge_head = (repo / ".git/MERGE_HEAD").read_bytes()
    (repo / "unrelated.txt").write_text("preserve staged note\n")
    git(repo, "add", "unrelated.txt")
    unrelated = git(repo, "ls-files", "--stage", "--", "unrelated.txt").stdout
    env = dict(os.environ, GRAPHIFY_OUT="graphify-out/")
    for _ in range(2):
        installed = subprocess.run(
            [sys.executable, "-E", "-P", "-B", "-m", "graphify", "hook", "install", "--merge-guard"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=40,
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr
    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-m", "graphify", "merge-finalize", "--output", "graphify-out"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert before_output == {str(p.relative_to(output)): (p.read_bytes(), p.stat().st_mode)
                            for p in output.rglob("*") if p.is_file()}
    assert before_head == git(repo, "rev-parse", "HEAD").stdout
    assert before_merge_head == (repo / ".git/MERGE_HEAD").read_bytes()
    assert unrelated == git(repo, "ls-files", "--stage", "--", "unrelated.txt").stdout
    # Canonicalization must not relax exact managed-byte verification.
    hook = repo / ".git/hooks/post-commit"
    hook.write_text(hook.read_text().replace("GRAPHIFY_OUT=graphify-out\n", "GRAPHIFY_OUT=other-output\n"))
    before_index = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="stale managed post-commit hook"):
        finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before_index


@pytest.mark.parametrize("event", ["post-commit", "post-checkout"])
@pytest.mark.parametrize("state", ["legacy", "valid-portable", "malformed-portable"])
@pytest.mark.parametrize("errexit", [False, True])
def test_linked_postevents_observe_before_legacy_suppression(tmp_path, event, state, errexit):
    repo = repository(tmp_path, graph=PORTABLE if state == "malformed-portable" else LEGACY,
                      envelope=b"{}" if state == "malformed-portable" else None)
    if state == "valid-portable":
        _, sources = portable.source_records_from_tree(repo, "graphify-out")
        graph = {"graph": {}, "directed": False, "multigraph": False, "nodes": [], "links": []}
        for name, payload in portable.make_bundle(graph, sources, "graphify-out").items():
            (repo / "graphify-out" / name).write_bytes(payload)
        git(repo, "add", "graphify-out")
        git(repo, "commit", "-m", "portable bundle")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "linked", str(linked))
    hooks.install(repo)
    hook = repo / ".git/hooks" / event
    # Intercept only the detached launcher; the observer still runs normally.
    body = hooks._REBUILD_BODY_COMMIT if event == "post-commit" else hooks._REBUILD_BODY_CHECKOUT
    script = hook.read_text().replace(hooks._detached_launch(body), 'printf "legacy-launcher-reached\\n" >&2\n')
    observer = 'if "$GRAPHIFY_PYTHON" -E -P -B -m graphify.merge_finalize --observe'
    script = script.replace(observer, 'printf "%s\\n" "$GRAPHIFY_OUT" "$@" > observer-entry\n' + observer)
    prefix = (("set -e\n" if errexit else "") + "FOREIGN_VALUE=retained\n"
              'cp "$(git rev-parse --git-path index)" hook-entry-index\n'
              'git rev-parse HEAD > hook-entry-head\n')
    suffix = ('\ncp "$(git rev-parse --git-path index)" hook-exit-index\n'
              'git rev-parse HEAD > hook-exit-head\n'
              'printf "%s\\n" "$FOREIGN_VALUE" "$@" > foreign-suffix\n')
    hook.write_text(script.replace("#!/bin/sh\n", "#!/bin/sh\n" + prefix, 1) + suffix)
    before = {(str(root), str(p.relative_to(root))): (p.read_bytes(), p.stat().st_mode)
              for root in (repo, linked) for p in (root / "graphify-out").iterdir()}
    original_head = git(linked, "rev-parse", "HEAD").stdout.strip()
    if event == "post-commit":
        (linked / "note.txt").write_text("note\n")
        git(linked, "add", "note.txt")
        result = git(linked, "commit", "-m", "note", skip_hooks=False)
        assert git(linked, "rev-parse", "HEAD^").stdout.strip() == original_head
        arguments = []
    else:
        result = git(linked, "checkout", "-b", "linked-probe", skip_hooks=False)
        assert git(linked, "rev-parse", "HEAD").stdout.strip() == original_head
        arguments = [original_head, original_head, "1"]
    assert (linked / "foreign-suffix").read_text().splitlines() == ["retained", *arguments]
    assert (linked / "hook-entry-index").read_bytes() == (linked / "hook-exit-index").read_bytes()
    assert (linked / "hook-entry-head").read_bytes() == (linked / "hook-exit-head").read_bytes()
    assert (linked / "observer-entry").read_text().splitlines() == ["graphify-out", *arguments]
    diagnostic = result.stdout + result.stderr
    if state == "valid-portable":
        assert "committed portable bundle" in diagnostic and "verified" in diagnostic
    elif state == "malformed-portable":
        assert "committed portable bundle refused" in diagnostic
    assert "legacy-launcher-reached" not in diagnostic
    assert before == {(str(root), str(p.relative_to(root))): (p.read_bytes(), p.stat().st_mode)
                      for root in (repo, linked) for p in (root / "graphify-out").iterdir()}
    assert not (Path.home() / ".cache/graphify-rebuild.log").exists()
