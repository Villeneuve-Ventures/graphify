"""Portable authority preserves composed posthooks and refuses stale source commits."""

import os
import subprocess
from pathlib import Path

import pytest

from graphify import hooks
from graphify.merge_guard import MergeGuardError, check_merge_commit
from graphify.merge_finalize import finalize_merge
from graphify.portable import open_portable_graph_snapshot, PortableGraphError
from tests.test_merge_commit_lifecycle import git, isolated_authority  # noqa: F401
from tests.test_merge_finalize import pending_repo
from tests.test_merge_finalize_observer import repository, PORTABLE

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX portable commit hooks")


@pytest.mark.parametrize("event", ["post-commit", "post-checkout", "post-merge"])
@pytest.mark.parametrize("errexit", [False, True])
def test_portable_suffix(event, tmp_path, errexit):
    repo = repository(tmp_path, graph=PORTABLE, envelope=b"{}")
    hooks.install(repo)
    hook = repo / ".git/hooks" / event
    before_output = {p.name: p.read_bytes() for p in (repo / "graphify-out").iterdir()}
    original_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    prefix = ("set -e\n" if errexit else "") + "FOREIGN_VALUE=retained\n"
    suffix = '\nprintf "%s\\n" "$FOREIGN_VALUE" "$@" >> .git/foreign-suffix\n'
    hook.write_text(hook.read_text().replace("#!/bin/sh\n", "#!/bin/sh\n" + prefix, 1) + suffix)
    hooks.install(repo)
    assert hook.read_text().endswith(suffix)
    if event == "post-commit":
        (repo / "note.txt").write_text("note\n")
        git(repo, "add", "note.txt")
        result = git(repo, "commit", "-m", "note", skip_hooks=False)
    elif event == "post-checkout":
        result = git(repo, "checkout", "-b", "probe", skip_hooks=False)
    else:
        git(repo, "checkout", "-b", "probe")
        (repo / "note.txt").write_text("note\n")
        git(repo, "add", "note.txt")
        git(repo, "commit", "-m", "note")
        git(repo, "checkout", "main")
        result = git(repo, "merge", "--no-edit", "probe", skip_hooks=False)
    assert result.returncode == 0
    expected = ["retained"]
    if event == "post-checkout":
        expected += [original_head, original_head, "1"]
    elif event == "post-merge":
        expected += ["0"]
    assert (repo / ".git/foreign-suffix").read_text().splitlines() == expected
    assert before_output == {p.name: p.read_bytes() for p in (repo / "graphify-out").iterdir()}
    assert not (Path.home() / ".cache/graphify-rebuild.log").exists()


@pytest.mark.parametrize("operation", ["add", "modify", "delete"])
def test_ordinary_portable_source_commit(operation, monkeypatch):
    repo = pending_repo()
    finalize_merge(repo, "graphify-out")
    git(repo, "commit", "--no-edit", skip_hooks=False)
    clone = Path.home() / "clone"
    git(Path.home(), "clone", "--no-local", str(repo), str(clone))
    # Fresh clones do not inherit the source repository's local identity.
    # Do not let ambient identity or OS-derived defaults mask this on CI.
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    git(clone, "config", "user.useConfigOnly", "true")
    git(clone, "config", "user.email", "merge-lifecycle@example.invalid")
    git(clone, "config", "user.name", "Merge Lifecycle Tests")
    hooks.install(clone, merge_guard=True)
    open_portable_graph_snapshot(clone, "graphify-out")
    before = git(clone, "rev-parse", "HEAD").stdout
    before_bytes = {p.name: p.read_bytes() for p in (clone / "graphify-out").iterdir()}
    if operation == "add":
        (clone / "new.py").write_text("def new():\n    return 4\n")
        git(clone, "add", "new.py")
    elif operation == "modify":
        (clone / "base.py").write_text("def replacement():\n    return 4\n")
        git(clone, "add", "base.py")
    else:
        git(clone, "rm", "base.py")
    index_before = (clone / ".git/index").read_bytes()
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-commit", root=clone)
    assert (clone / ".git/index").read_bytes() == index_before
    staged_before = git(clone, "ls-files", "--stage", "-z").stdout
    result = git(clone, "commit", "-m", operation, skip_hooks=False, check=False)
    after = git(clone, "rev-parse", "HEAD").stdout
    try:
        open_portable_graph_snapshot(clone, "graphify-out")
        refusal = None
    except PortableGraphError as exc:
        refusal = str(exc)
    assert result.returncode != 0, "source-changing commit advanced past stale portable bundle"
    assert after == before
    # Git refreshes index stat/cache metadata before its hooks; staged objects
    # and flags must remain unchanged across the refused actual commit.
    assert git(clone, "ls-files", "--stage", "-z").stdout == staged_before
    assert before_bytes == {p.name: p.read_bytes() for p in (clone / "graphify-out").iterdir()}
    assert refusal is None
    assert "portable manifest does not match" in result.stderr


def test_valid_portable_publication_preserves_suffix_on_checkout():
    repo = pending_repo()
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text(hook.read_text() + '\nprintf "suffix-ran\\n" >> .git/foreign-suffix\n')
    finalize_merge(repo, "graphify-out")
    git(repo, "commit", "--no-edit", skip_hooks=False)
    result = git(repo, "checkout", "-b", "valid-probe", skip_hooks=False)
    assert result.returncode == 0
    assert (repo / ".git/foreign-suffix").exists()


def test_non_source_portable_commit_keeps_bundle_readable(tmp_path):
    from tests.test_portable_cli import bundle_repo

    bundle_repo(tmp_path)
    hooks.install(tmp_path, merge_guard=True)
    output = tmp_path / "graphify-out"
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    (tmp_path / "hello.py").write_text("def dirty():\n    return 9\n")
    (tmp_path / "untracked.py").write_text("def untracked():\n    return 10\n")
    (tmp_path / "note.txt").write_text("note\n")
    git(tmp_path, "add", "note.txt")
    result = git(tmp_path, "commit", "-m", "note", skip_hooks=False)
    assert result.returncode == 0
    open_portable_graph_snapshot(tmp_path, "graphify-out")
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}
    assert not (Path.home() / ".cache/graphify-rebuild.log").exists()


@pytest.mark.parametrize(
    "defect", ["manifest-missing", "bad-envelope", "envelope-alias", "conversion"]
)
def test_ordinary_portable_closure_refused_without_changes(tmp_path, defect):
    from tests.test_portable_cli import bundle_repo

    bundle_repo(tmp_path)
    hooks.install(tmp_path, merge_guard=True)
    output = tmp_path / "graphify-out"
    if defect == "manifest-missing":
        git(tmp_path, "rm", "graphify-out/manifest.json")
    elif defect == "bad-envelope":
        (output / ".graphify_portable.json").write_text("{}")
        git(tmp_path, "add", "graphify-out/.graphify_portable.json")
    elif defect == "envelope-alias":
        git(
            tmp_path,
            "mv",
            "graphify-out/.graphify_portable.json",
            "graphify-out/.GRAPHIFY_PORTABLE.json",
        )
    else:
        with (tmp_path / ".gitattributes").open("a") as stream:
            stream.write("\ngraphify-out/*.json text\n")
        git(tmp_path, "add", ".gitattributes")
    before_head = git(tmp_path, "rev-parse", "HEAD").stdout
    before_index = (tmp_path / ".git/index").read_bytes()
    before_bytes = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-commit", root=tmp_path)
    assert (tmp_path / ".git/index").read_bytes() == before_index
    staged_before = git(tmp_path, "ls-files", "--stage", "-z").stdout
    result = git(tmp_path, "commit", "-m", defect, skip_hooks=False, check=False)
    assert result.returncode != 0
    assert "portable" in result.stderr or "output filters" in result.stderr
    assert git(tmp_path, "rev-parse", "HEAD").stdout == before_head
    assert git(tmp_path, "ls-files", "--stage", "-z").stdout == staged_before
    assert before_bytes == {p.name: p.read_bytes() for p in output.iterdir()}


@pytest.mark.parametrize("state", ["legacy", "active"])
@pytest.mark.parametrize("unborn", [False, True])
def test_ordinary_legacy_commit_skips_trusted_runtime_discovery(tmp_path, monkeypatch, state, unborn):
    from tests.test_merge_guard import staged_repo, payload

    data = payload("active") if state == "active" else {"nodes": [], "links": [], "graph": {}}
    repo, graph = staged_repo(tmp_path, data)
    (repo / ".git/MERGE_HEAD").unlink()
    if unborn:
        git(repo, "update-ref", "-d", "HEAD")
    check_merge_commit("graphify-out", "pre-commit", root=repo)
    monkeypatch.setattr(hooks, "_pinned_python", lambda: "/missing/python")
    hooks.install(repo, merge_guard=True)
    script = (repo / ".git/hooks/pre-commit").read_text()
    script = script[: script.index("# Detect a trusted Python interpreter")]
    script += 'printf "unexpected-runtime-discovery\\n" >&2\nexit 1\n'
    before = ((repo / ".git/index").read_bytes(), graph.read_bytes())
    result = subprocess.run(["/bin/sh"], input=script, cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert before == ((repo / ".git/index").read_bytes(), graph.read_bytes())


@pytest.mark.parametrize("also_remove", [None, "graph.json", "manifest.json"])
def test_ordinary_portable_envelope_deletion_refused(tmp_path, also_remove):
    from tests.test_portable_cli import bundle_repo

    output = bundle_repo(tmp_path)
    hooks.install(tmp_path, merge_guard=True)
    git(tmp_path, "rm", "graphify-out/.graphify_portable.json")
    if also_remove:
        git(tmp_path, "rm", "graphify-out/" + also_remove)
    before_head = git(tmp_path, "rev-parse", "HEAD").stdout
    before_index = (tmp_path / ".git/index").read_bytes()
    before_bytes = {p.name: p.read_bytes() for p in output.iterdir()}
    try:
        check_merge_commit("graphify-out", "pre-commit", root=tmp_path)
        refusal = None
    except MergeGuardError as exc:
        refusal = str(exc)
    assert (tmp_path / ".git/index").read_bytes() == before_index
    staged_before = git(tmp_path, "ls-files", "--stage", "-z").stdout
    result = git(tmp_path, "commit", "-m", "remove envelope", skip_hooks=False, check=False)
    assert result.returncode != 0, "partial portable deletion advanced HEAD"
    assert refusal is not None and "portable" in refusal
    assert "portable" in result.stderr
    assert git(tmp_path, "rev-parse", "HEAD").stdout == before_head
    assert git(tmp_path, "ls-files", "--stage", "-z").stdout == staged_before
    assert before_bytes == {p.name: p.read_bytes() for p in output.iterdir()}


@pytest.mark.parametrize("missing_runtime", [False, True])
def test_ordinary_complete_portable_removal_is_not_partial_bundle(tmp_path, monkeypatch, missing_runtime):
    from tests.test_portable_cli import bundle_repo

    bundle_repo(tmp_path)
    if missing_runtime:
        monkeypatch.setattr(hooks, "_pinned_python", lambda: "/missing/python")
    hooks.install(tmp_path, merge_guard=True)
    git(tmp_path, "rm", "-r", "graphify-out")
    check_merge_commit("graphify-out", "pre-commit", root=tmp_path)
    script = (tmp_path / ".git/hooks/pre-commit").read_text()
    if missing_runtime:
        script = script[: script.index("# Detect a trusted Python interpreter")]
        script += 'printf "unexpected-runtime-discovery\\n" >&2\nexit 1\n'
    result = subprocess.run(["/bin/sh"], input=script, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
