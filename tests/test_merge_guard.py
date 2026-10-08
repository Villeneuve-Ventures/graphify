"""Containment applies to staged merge output, without migrating legacy graphs."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from graphify import hooks, paths, transaction
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX merge guard qualification")


def payload(state="merge_pending"):
    return {"nodes": [], "links": [], "graph": {transaction.GRAPH_WATERMARK_KEY: {
        "schema": 1, "protocol_epoch": 1, "generation": 1, "state": state,
    }}}


def staged_repo(tmp_path, data=None, *, output="graphify-out"):
    repo = init_repo(tmp_path / "repo")
    git(repo, "commit", "--allow-empty", "-m", "base")
    graph = repo / output / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(json.dumps(payload() if data is None else data))
    git(repo, "add", "--", graph.relative_to(repo).as_posix())
    (repo / ".git/MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD").stdout)
    return repo, graph


@pytest.mark.parametrize("event", ["pre-commit", "pre-merge-commit"])
def test_pending_staged_graph_refused_without_changes(tmp_path, event):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, graph = staged_repo(tmp_path)
    before_index = (repo / ".git/index").read_bytes()
    before_graph = graph.read_bytes()
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", event, root=repo)
    assert (repo / ".git/index").read_bytes() == before_index
    assert graph.read_bytes() == before_graph


@pytest.mark.parametrize("data", [payload("active"), {"nodes": [], "links": [], "graph": {}}],
                         ids=["active", "legacy"])
def test_active_and_legacy_graphs_do_not_require_portable_receipts(tmp_path, data):
    from graphify.merge_guard import check_merge_commit

    repo, _ = staged_repo(tmp_path, data)
    check_merge_commit("graphify-out", "pre-commit", root=repo)
    check_merge_commit("graphify-out", "pre-merge-commit", root=repo)


@pytest.mark.parametrize("generation", [None, "1", 1.0, True, "missing"])
def test_active_watermark_requires_integer_generation_without_mutation(tmp_path, generation):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    data = payload("active")
    watermark = data["graph"][transaction.GRAPH_WATERMARK_KEY]
    if generation == "missing":
        del watermark["generation"]
    else:
        watermark["generation"] = generation
    repo, graph = staged_repo(tmp_path, data)
    before = ((repo / ".git/index").read_bytes(), graph.read_bytes())
    with pytest.raises(MergeGuardError, match="generation"):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert before == ((repo / ".git/index").read_bytes(), graph.read_bytes())


def test_ordinary_commit_and_untracked_graph_are_outside_guard(tmp_path):
    from graphify.merge_guard import check_merge_commit

    repo, _ = staged_repo(tmp_path)
    (repo / ".git/MERGE_HEAD").unlink()
    check_merge_commit("graphify-out", "pre-commit", root=repo)
    git(repo, "rm", "--cached", "graphify-out/graph.json")
    check_merge_commit("graphify-out", "pre-merge-commit", root=repo)
    check_merge_commit("absent-out", "pre-merge-commit", root=repo)


@pytest.mark.parametrize("signal", ["rebase-merge", "rebase-apply", "sequencer", "CHERRY_PICK_HEAD"])
def test_sequencer_boundaries_are_not_claimed(tmp_path, signal):
    from graphify.merge_guard import check_merge_commit

    repo, _ = staged_repo(tmp_path)
    marker = repo / ".git" / signal
    if signal.endswith("HEAD"):
        marker.write_text(git(repo, "rev-parse", "HEAD").stdout)
    else:
        marker.mkdir()
    check_merge_commit("graphify-out", "pre-commit", root=repo)
    check_merge_commit("graphify-out", "pre-merge-commit", root=repo)


@pytest.mark.parametrize("body", ["{", "[]", '{"graph":null}',
    '{"graph":{"_graphify_protocol":null}}',
    '{"graph":{"_graphify_protocol":{"state":"active"}}}',
    json.dumps(payload("unknown"))], ids=["json", "top-level", "metadata", "watermark", "schema", "state"])
def test_malformed_staged_graph_refused(tmp_path, body):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, graph = staged_repo(tmp_path)
    graph.write_text(body)
    git(repo, "add", "graphify-out/graph.json")
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-merge-commit", root=repo)


def test_staged_symlink_refused_without_following_target(tmp_path):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, graph = staged_repo(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload("active")))
    graph.unlink()
    graph.symlink_to(outside)
    git(repo, "add", "graphify-out/graph.json")
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-merge-commit", root=repo)
    assert json.loads(outside.read_text()) == payload("active")


def test_missing_staged_object_is_a_read_failure(tmp_path):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, _ = staged_repo(tmp_path)
    git(repo, "update-index", "--info-only", "--cacheinfo",
        "100644," + "d" * 40 + ",graphify-out/graph.json")
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-merge-commit", root=repo)


@pytest.mark.parametrize("index_pending", [False, True])
def test_effective_alternate_index_is_authority(tmp_path, monkeypatch, index_pending):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, graph = staged_repo(tmp_path, payload("active" if index_pending else "merge_pending"))
    alternate = repo / ".git/alternate-index"
    alternate.write_bytes((repo / ".git/index").read_bytes())
    graph.write_text(json.dumps(payload("merge_pending" if index_pending else "active")))
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate))
    git(repo, "add", "graphify-out/graph.json")
    # Neither the primary index nor later working-tree bytes are the commit input.
    graph.write_text("working tree is intentionally unrelated")
    primary_before = (repo / ".git/index").read_bytes()
    alternate_before = alternate.read_bytes()
    if index_pending:
        with pytest.raises(MergeGuardError):
            check_merge_commit("graphify-out", "pre-commit", root=repo)
    else:
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert (repo / ".git/index").read_bytes() == primary_before
    assert alternate.read_bytes() == alternate_before


def test_literal_custom_output_selection(tmp_path):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    output = "custom output[1]"
    repo, _ = staged_repo(tmp_path, output=output)
    with pytest.raises(MergeGuardError, match="merge_pending"):
        check_merge_commit(output, "pre-merge-commit", root=repo)


def test_in_repository_absolute_output_refuses_enrollment_without_mutation(tmp_path, monkeypatch):
    repo, _ = staged_repo(tmp_path, output="custom-out")
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", str(repo / "custom-out"))
    before_config = (repo / ".git/config").read_bytes()
    before_index = (repo / ".git/index").read_bytes()
    with pytest.raises(RuntimeError, match="absolute outputs are unsupported"):
        hooks.install(repo, merge_guard=True)
    assert (repo / ".git/config").read_bytes() == before_config
    assert (repo / ".git/index").read_bytes() == before_index
    assert installed_hook_names(repo) == set()
    assert not (repo / ".gitattributes").exists()


def test_cli_refusal_and_explicit_environment_bypass(tmp_path):
    repo, _ = staged_repo(tmp_path)
    command = [sys.executable, "-E", "-P", "-B", "-m", "graphify.merge_guard",
               "--event", "pre-commit", "--output", "graphify-out"]
    refused = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "merge_pending" in refused.stderr
    bypassed = subprocess.run(command, cwd=repo,
                              env={**os.environ, "GRAPHIFY_SKIP_HOOK": "1"},
                              capture_output=True, text=True)
    assert bypassed.returncode == 0, bypassed.stderr


@pytest.mark.parametrize("name", ["pre-commit", "pre-merge-commit"])
@pytest.mark.parametrize("body", ["#!/bin/sh\nexit 0\n", "#!/usr/bin/env python3\nraise SystemExit(0)\n"])
def test_existing_user_prehook_refuses_install_without_overwrite(tmp_path, name, body):
    repo = init_repo(tmp_path / "repo")
    hook = repo / ".git/hooks" / name
    hook.write_text(body)
    hook.chmod(0o751)
    before_config = (repo / ".git/config").read_bytes()
    before_hooks = {p.name: (p.read_bytes(), p.stat().st_mode)
                    for p in hook.parent.iterdir() if p.is_file()}
    with pytest.raises(RuntimeError):
        hooks.install(repo, merge_guard=True)
    assert (repo / ".git/config").read_bytes() == before_config
    assert {p.name: (p.read_bytes(), p.stat().st_mode)
            for p in hook.parent.iterdir() if p.is_file()} == before_hooks
    assert not (repo / ".gitattributes").exists()


def test_opt_in_guard_install_status_uninstall_roundtrip(tmp_path):
    repo = init_repo(tmp_path / "repo")
    hooks.install(repo, merge_guard=True)
    for name in ("pre-commit", "pre-merge-commit", "post-commit", "post-checkout", "post-merge"):
        assert os.access(repo / ".git/hooks" / name, os.X_OK)
    installed = hooks.status(repo, merge_guard=True)
    assert "pre-commit" in str(installed)
    hooks.install(repo, merge_guard=True)
    hooks.uninstall(repo, merge_guard=True)
    for name in ("pre-commit", "pre-merge-commit", "post-commit", "post-checkout", "post-merge"):
        assert not (repo / ".git/hooks" / name).exists()


def test_external_absolute_output_opt_in_refused(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", str(tmp_path / "shared-output"))
    before_config = (repo / ".git/config").read_bytes()
    with pytest.raises(RuntimeError, match="absolute outputs are unsupported"):
        hooks.install(repo, merge_guard=True)
    assert (repo / ".git/config").read_bytes() == before_config
    assert not (repo / ".git/hooks/pre-commit").exists()
    assert not (repo / ".gitattributes").exists()


def test_custom_hooks_path_linked_worktree_commit_is_guarded(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    git(repo, "commit", "--allow-empty", "-m", "base")
    hooks_path = repo / "shared hooks"
    git(repo, "config", "core.hooksPath", str(hooks_path))
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "linked", str(linked))
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", "custom-out")
    hooks.install(linked, merge_guard=True)
    output = linked / "custom-out"
    output.mkdir()
    (output / "graph.json").write_text(json.dumps(payload()))
    git(linked, "add", "custom-out/graph.json")
    merge_head = Path(git(linked, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").stdout.strip())
    merge_head.write_text(git(linked, "rev-parse", "HEAD").stdout)
    before = git(linked, "rev-parse", "HEAD").stdout
    result = git(linked, "commit", "-m", "guarded", skip_hooks=False, check=False)
    assert result.returncode != 0
    assert "merge_pending" in result.stdout + result.stderr
    assert git(linked, "rev-parse", "HEAD").stdout == before


def hook_cli(repo, *arguments):
    return subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-m", "graphify", "hook", *arguments],
        cwd=repo, capture_output=True, text=True,
    )


def installed_hook_names(repo):
    return {path.name for path in (repo / ".git/hooks").iterdir()
            if path.is_file() and not path.name.endswith(".sample")}


@pytest.mark.parametrize("output", [
    "custom output", "custom[1]", "custom*",
    "./graphify-out", "out//nested", "out/./nested",
])
def test_unsupported_attribute_selector_refuses_enrollment_without_mutation(tmp_path, monkeypatch, output):
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", output)
    before_config = (repo / ".git/config").read_bytes()
    with pytest.raises(RuntimeError):
        hooks.install(repo, merge_guard=True)
    assert (repo / ".git/config").read_bytes() == before_config
    assert installed_hook_names(repo) == set()
    assert not (repo / ".gitattributes").exists()


def test_module_cli_merge_guard_roundtrip_and_default_scope(tmp_path):
    repo = init_repo(tmp_path / "repo")
    installed = hook_cli(repo, "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert installed_hook_names(repo) == {"post-commit", "post-checkout", "post-merge"}
    enrolled = hook_cli(repo, "install", "--merge-guard")
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
    assert installed_hook_names(repo) == {
        "pre-commit", "pre-merge-commit", "post-commit", "post-checkout", "post-merge",
    }
    status = hook_cli(repo, "status", "--merge-guard")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "pre-commit: installed" in status.stdout
    assert "pre-merge-commit: installed" in status.stdout
    removed = hook_cli(repo, "uninstall", "--merge-guard")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert installed_hook_names(repo) == set()


@pytest.mark.parametrize("name", ["pre-commit", "pre-merge-commit"])
@pytest.mark.parametrize("change", ["prefix", "suffix", "interpreter"])
def test_guard_status_refuses_unsupported_shape_without_mutation(tmp_path, name, change):
    repo = init_repo(tmp_path / "repo")
    hooks.install(repo, merge_guard=True)
    hook = repo / ".git/hooks" / name
    original = hook.read_bytes()
    if change == "prefix":
        changed = original.replace(b"#!/bin/sh\n", b"#!/bin/sh\nexit 0\n", 1)
    elif change == "suffix":
        changed = original + b"echo foreign-hook-content\n"
    else:
        changed = original.replace(b"#!/bin/sh\n", b"#!/usr/bin/env python3\n", 1)
    hook.write_bytes(changed)
    before = (hook.read_bytes(), hook.stat().st_ino, hook.stat().st_mode)
    result = hook_cli(repo, "status", "--merge-guard")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{name}: not installed (unsupported standalone guard)" in result.stdout
    assert before == (hook.read_bytes(), hook.stat().st_ino, hook.stat().st_mode)


@pytest.mark.parametrize("malformed", [False, True])
def test_global_uninstall_removes_opted_in_guards(tmp_path, malformed):
    repo = init_repo(tmp_path / "repo")
    hooks.install(repo, merge_guard=True)
    assert {"pre-commit", "pre-merge-commit"} <= installed_hook_names(repo)
    if malformed:
        guard = repo / ".git/hooks/pre-commit"
        start, end = hooks._merge_guard_markers("pre-commit")
        guard.write_text(f"#!/bin/sh\n{start}\nexit 0\n")
        before = {name: (repo / ".git/hooks" / name).read_bytes()
                  for name in installed_hook_names(repo)}
    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-m", "graphify", "uninstall"],
        cwd=repo, capture_output=True, text=True,
    )
    if malformed:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "pip uninstall graphifyy" not in result.stdout
        assert before == {name: (repo / ".git/hooks" / name).read_bytes()
                          for name in installed_hook_names(repo)}
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed_hook_names(repo) == set()
    assert git(repo, "config", "--get", "merge.graphify.driver", check=False).returncode != 0


@pytest.mark.parametrize("arguments", [
    ("install", "--unknown"), ("status", "--unknown"), ("uninstall", "--unknown"),
    ("install", "--merge-guard", "--merge-guard"), ("install", "--merge-guard=true"),
])
def test_module_cli_unknown_or_duplicate_option_has_no_repository_effect(tmp_path, arguments):
    repo = init_repo(tmp_path / "repo")
    before_config = (repo / ".git/config").read_bytes()
    before_hooks = {path.name: (path.read_bytes(), path.stat().st_mode)
                    for path in (repo / ".git/hooks").iterdir()}
    result = hook_cli(repo, *arguments)
    assert result.returncode != 0
    assert "Usage:" in result.stderr
    assert (repo / ".git/config").read_bytes() == before_config
    assert {path.name: (path.read_bytes(), path.stat().st_mode)
            for path in (repo / ".git/hooks").iterdir()} == before_hooks
    assert not (repo / ".gitattributes").exists()


@pytest.mark.parametrize("case", ["selected", "ordinary", "untracked", "absent"])
def test_missing_runtime_is_required_only_for_selected_merge_graph(tmp_path, monkeypatch, case):
    repo, graph = staged_repo(tmp_path)
    # A failed pinned-runtime probe is observable. PATH offers Git but no Python
    # fallback, so an affected merge must refuse instead of silently passing.
    runtime_dir = Path.home() / "runtime"
    runtime_dir.mkdir()
    probe_log = runtime_dir / "probe.log"
    failed_runtime = runtime_dir / "python-unavailable"
    failed_runtime.write_text(
        "#!/bin/sh\nprintf 'probe\\n' >> " + shlex.quote(str(probe_log)) + "\nexit 1\n"
    )
    failed_runtime.chmod(0o755)
    (runtime_dir / "git").symlink_to(shutil.which("git"))
    monkeypatch.setattr(hooks, "_pinned_python", lambda: str(failed_runtime))
    hooks.install(repo, merge_guard=True)
    if case == "ordinary":
        (repo / ".git/MERGE_HEAD").unlink()
    elif case in {"untracked", "absent"}:
        git(repo, "rm", "--cached", "graphify-out/graph.json")
        if case == "absent":
            graph.unlink()
    before_index = (repo / ".git/index").read_bytes()
    before_graph = graph.read_bytes() if graph.exists() else None
    event = "pre-commit" if case == "ordinary" else "pre-merge-commit"
    result = subprocess.run(
        ["/bin/sh", str(repo / ".git/hooks" / event)], cwd=repo,
        env={**os.environ, "PATH": str(runtime_dir)}, capture_output=True, text=True,
    )
    if case == "selected":
        assert result.returncode != 0
        assert "could not locate a trusted" in result.stderr
        assert probe_log.read_text() == "probe\n"
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert not probe_log.exists()
    assert (repo / ".git/index").read_bytes() == before_index
    assert (graph.read_bytes() if graph.exists() else None) == before_graph


def test_replace_ref_cannot_disguise_pending_index_blob(tmp_path):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, graph = staged_repo(tmp_path)
    pending_oid = git(repo, "rev-parse", ":graphify-out/graph.json").stdout.strip()
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(payload("active")))
    active_oid = git(repo, "hash-object", "-w", str(replacement)).stdout.strip()
    git(repo, "replace", pending_oid, active_oid)
    # Establish that ordinary cat-file is fooled, while the index still names
    # the original pending bytes; the guard must read that exact stored object.
    assert json.loads(git(repo, "cat-file", "blob", pending_oid).stdout) == payload("active")
    before_index = (repo / ".git/index").read_bytes()
    before_graph = graph.read_bytes()
    with pytest.raises(MergeGuardError, match="merge_pending"):
        check_merge_commit("graphify-out", "pre-merge-commit", root=repo)
    assert (repo / ".git/index").read_bytes() == before_index
    assert graph.read_bytes() == before_graph


@pytest.mark.parametrize("installed", [False, True], ids=["python-check", "installed-shell"])
def test_inherited_literal_pathspec_mode_cannot_hide_staged_graph(tmp_path, monkeypatch, installed):
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo, graph = staged_repo(tmp_path)
    if installed:
        hooks.install(repo, merge_guard=True)
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    before_index = (repo / ".git/index").read_bytes()
    before_graph = graph.read_bytes()
    if installed:
        result = subprocess.run(["/bin/sh", str(repo / ".git/hooks/pre-merge-commit")],
                                cwd=repo, capture_output=True, text=True)
        assert result.returncode != 0
        assert "merge_pending" in result.stderr
    else:
        with pytest.raises(MergeGuardError, match="merge_pending"):
            check_merge_commit("graphify-out", "pre-merge-commit", root=repo)
    assert (repo / ".git/index").read_bytes() == before_index
    assert graph.read_bytes() == before_graph
