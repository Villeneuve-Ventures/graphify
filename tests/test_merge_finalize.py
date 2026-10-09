"""Manual transition stages immutable portable bytes and leaves local authority intact."""
import os
from pathlib import Path

import pytest

from tests.test_merge_commit_lifecycle import graph_repo, git, isolated_authority  # noqa: F401

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX manual finalization")


def pending_repo():
    repo = graph_repo(Path.home(), manual=True)
    result = git(repo, "merge", "--no-edit", "side", check=False)
    assert result.returncode != 0
    (repo / "conflict.txt").write_text("resolved\n")
    git(repo, "add", "conflict.txt")
    from graphify import hooks
    hooks.install(repo, merge_guard=True)
    return repo


@pytest.mark.parametrize("staged", [False, True])
def test_commit_refuses_output_conversion_added_after_finalization(staged):
    from graphify import hooks
    from graphify.merge_finalize import finalize_merge

    repo = pending_repo()
    hooks.install(repo, merge_guard=True)
    finalize_merge(repo, "graphify-out")
    with (repo / ".gitattributes").open("a") as stream:
        stream.write("\ngraphify-out/*.json working-tree-encoding=UTF-16LE-BOM\n")
    if staged:
        git(repo, "add", ".gitattributes")
    before_tree = git(repo, "write-tree").stdout
    before_head = git(repo, "rev-parse", "HEAD").stdout
    result = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
    assert result.returncode != 0
    assert "output filters, text conversion, and encodings" in result.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == before_head
    assert git(repo, "write-tree").stdout == before_tree
    assert (repo / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("operation", ["finalize", "cancel", "guard"])
def test_relocated_objects_refuse_without_changing_index(monkeypatch, operation):
    from graphify.merge_finalize import MergeFinalizeError, cancel_merge, finalize_merge
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo = pending_repo()
    if operation != "finalize":
        finalize_merge(repo, "graphify-out")
    external = Path.home() / "external-objects"
    external.mkdir()
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(external))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(repo / ".git/objects"))
    before = (repo / ".git/index").read_bytes()
    with pytest.raises((MergeFinalizeError, MergeGuardError), match="object storage"):
        if operation == "guard":
            check_merge_commit("graphify-out", "pre-commit", root=repo)
        elif operation == "cancel":
            cancel_merge(repo, "graphify-out")
        else:
            finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("sibling", [".graphify_root", "notes.md"])
def test_staged_deletion_cannot_hide_tracked_output_sibling(sibling):
    from graphify import hooks
    from graphify.merge_finalize import MergeFinalizeError, finalize_merge

    repo = graph_repo(Path.home(), manual=True)
    hooks.install(repo, merge_guard=True)
    target = repo / "graphify-out" / sibling
    if sibling == "notes.md":
        target.write_text("tracked note\n")
    git(repo, "add", f"graphify-out/{sibling}")
    git(repo, "commit", "--amend", "--no-edit")
    if sibling == "notes.md":
        target.unlink()
    assert git(repo, "merge", "--no-edit", "side", check=False).returncode != 0
    (repo / "conflict.txt").write_text("resolved\n")
    git(repo, "add", "conflict.txt")
    git(repo, "update-index", "--force-remove", f"graphify-out/{sibling}")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="tracked output sibling"):
        finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before


def test_manual_finalization_records_readable_clone_and_preserves_worktree():
    from graphify.merge_finalize import finalize_merge
    from graphify.portable import open_portable_graph_snapshot

    repo = pending_repo()
    from graphify import hooks
    hooks.install(repo, merge_guard=True)
    output = repo / "graphify-out"
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    (repo / "main.py").write_text("def dirty():\n    return 99\n")
    (repo / "untracked.py").write_text("def untracked():\n    return 100\n")
    unrelated = git(repo, "ls-files", "--stage", "--", "conflict.txt").stdout
    finalized = finalize_merge(repo, "graphify-out")
    assert finalized
    assert git(repo, "ls-files", "--stage", "--", "conflict.txt").stdout == unrelated
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == before
    git(repo, "commit", "--no-edit", skip_hooks=False)
    clone = Path.home() / "clone"
    git(Path.home(), "clone", "--no-local", str(repo), str(clone))
    snapshot = open_portable_graph_snapshot(clone, "graphify-out")
    names = {node["id"] for node in snapshot.data["nodes"]}
    assert {"base_base", "main_main", "side_side"} <= names
    assert not any("dirty" in name or "untracked" in name for name in names)
    import subprocess
    import sys
    query = subprocess.run([sys.executable, "-E", "-P", "-B", "-m", "graphify", "query", "main",
                            "--portable", "--output", "graphify-out"],
                           cwd=clone, capture_output=True, text=True, check=False)
    assert query.returncode == 0, query.stdout + query.stderr
    assert "main" in query.stdout and "Portable bundle" in query.stdout
    assert git(clone, "status", "--porcelain").stdout == ""
    assert not (Path.home() / ".cache/graphify-rebuild.log").exists()
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == before


def test_identical_retry_preserves_index_and_changed_source_refuses():
    from graphify.merge_finalize import MergeFinalizeError, finalize_merge

    repo = pending_repo()
    content = finalize_merge(repo, "graphify-out")
    index = repo / ".git/index"
    before = index.read_bytes()
    assert finalize_merge(repo, "graphify-out") == content
    assert index.read_bytes() == before
    (repo / "main.py").write_text("def replacement():\n    return 9\n")
    git(repo, "add", "main.py")
    changed = index.read_bytes()
    with pytest.raises(MergeFinalizeError):
        finalize_merge(repo, "graphify-out")
    assert index.read_bytes() == changed


@pytest.mark.parametrize("kind", ["alternate", "sparse", "split", "unmerged", "user-hook", "sibling", "locked"])
def test_unsupported_states_refuse_without_publication(monkeypatch, kind):
    from graphify.merge_finalize import MergeFinalizeError, finalize_merge

    repo = pending_repo()
    if kind == "alternate":
        monkeypatch.setenv("GIT_INDEX_FILE", str(repo / ".git/index"))
    elif kind == "sparse":
        git(repo, "config", "core.sparseCheckout", "true")
    elif kind == "split":
        git(repo, "update-index", "--split-index")
    elif kind == "unmerged":
        oid = git(repo, "rev-parse", "HEAD:conflict.txt").stdout.strip()
        import subprocess
        subprocess.run(["git", "-C", str(repo), "update-index", "--index-info"],
                       input=f"0 {'0'*40}\tconflict.txt\n100644 {oid} 1\tconflict.txt\n".encode(), check=True)
    elif kind == "user-hook":
        hook = repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
    elif kind == "sibling":
        sibling = repo / "graphify-out/analysis.md"
        sibling.write_text("unsupported\n")
        git(repo, "add", "graphify-out/analysis.md")
    elif kind == "locked":
        (repo / ".git/index.lock").write_bytes(b"another owner")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError):
        finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before
    if kind == "locked":
        assert (repo / ".git/index.lock").read_bytes() == b"another owner"


def test_input_change_during_preparation_preserves_concurrent_index(monkeypatch):
    import graphify.merge_finalize as finalizer

    repo = pending_repo()
    original = finalizer._extract
    captured = []
    def mutate(root, blobs):
        graph = original(root, blobs)
        (repo / "main.py").write_text("def concurrent():\n    return 3\n")
        git(repo, "add", "main.py")
        captured.append((repo / ".git/index").read_bytes())
        return graph
    monkeypatch.setattr(finalizer, "_extract", mutate)
    with pytest.raises(finalizer.MergeFinalizeError, match="inputs changed"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == captured[0]
    assert not (repo / ".git/index.lock").exists()


def test_premerge_portable_refused_manual_closure_validated():
    from graphify.merge_finalize import finalize_merge
    from graphify.merge_guard import MergeGuardError, check_merge_commit

    repo = pending_repo()
    finalize_merge(repo, "graphify-out")
    check_merge_commit("graphify-out", "pre-commit", root=repo)
    with pytest.raises(MergeGuardError, match="automatic"):
        check_merge_commit("graphify-out", "pre-merge-commit", root=repo)
    (repo / "omitted.py").write_text("def omitted():\n    return 1\n")
    git(repo, "add", "omitted.py")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert (repo / ".git/index").read_bytes() == before


def test_publication_does_not_remove_next_writer_lock(monkeypatch):
    import graphify.merge_finalize as finalizer

    repo = pending_repo()
    original = finalizer.os.replace
    index = repo / ".git/index"
    lock = repo / ".git/index.lock"
    def publish_then_claim(source, destination):
        original(source, destination)
        if Path(destination) == index:
            lock.write_bytes(b"next writer")
    monkeypatch.setattr(finalizer.os, "replace", publish_then_claim)
    finalizer.finalize_merge(repo, "graphify-out")
    assert lock.read_bytes() == b"next writer"


def test_intent_to_add_is_refused():
    from graphify.merge_finalize import MergeFinalizeError, finalize_merge

    repo = pending_repo()
    (repo / "intent.py").write_text("def intent():\n    pass\n")
    git(repo, "add", "--intent-to-add", "intent.py")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="intent-to-add"):
        finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before


def test_orphan_envelope_guard_refuses_without_mutation():
    from graphify import hooks
    from graphify.merge_finalize import finalize_merge

    repo = pending_repo()
    hooks.install(repo, merge_guard=True)
    finalize_merge(repo, "graphify-out")
    git(repo, "update-index", "--force-remove", "graphify-out/graph.json")
    from graphify.merge_guard import MergeGuardError, check_merge_commit
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeGuardError):
        check_merge_commit("graphify-out", "pre-commit", root=repo)
    assert (repo / ".git/index").read_bytes() == before
    stage_before = git(repo, "ls-files", "--stage").stdout
    refused = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
    assert refused.returncode != 0
    assert "three-file closure" in refused.stderr
    assert git(repo, "ls-files", "--stage").stdout == stage_before


def test_abort_keeps_complete_bundle_until_git_cancels():
    from graphify.merge_finalize import MergeFinalizeError, cancel_merge, finalize_merge, validate_index_bundle

    repo = pending_repo()
    content = finalize_merge(repo, "graphify-out")
    assert validate_index_bundle(repo, "graphify-out").content_id == content
    cancel_merge(repo, "graphify-out")
    git(repo, "merge", "--abort")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="uncommitted"):
        finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before


def test_frozen_python_relations_keep_declared_endpoints():
    from graphify.merge_finalize import finalize_merge, validate_index_bundle

    repo = pending_repo()
    (repo / "relations.py").write_text(
        "import os\nclass Base:\n    def method(self):\n        return os.getcwd()\n"
        "class Child(Base):\n    def call(self):\n        return self.method()\n")
    git(repo, "add", "relations.py")
    finalize_merge(repo, "graphify-out")
    snapshot = validate_index_bundle(repo, "graphify-out")
    identities = {node["id"] for node in snapshot.data["nodes"]}
    links = snapshot.data["links"]
    assert links
    assert all(link["source"] in identities and link["target"] in identities for link in links)
    assert {"imports", "inherits", "calls"} <= {link["relation"] for link in links}


def test_cancel_preserves_unrelated_current_staging_and_flags():
    from graphify.merge_finalize import cancel_merge, finalize_merge

    repo = pending_repo()
    finalize_merge(repo, "graphify-out")
    (repo / "main.py").write_text("def after_preparation():\n    return 12\n")
    git(repo, "add", "main.py")
    git(repo, "update-index", "--assume-unchanged", "conflict.txt")
    before = git(repo, "ls-files", "--debug", "--", "main.py", "conflict.txt").stdout
    cancel_merge(repo, "graphify-out")
    assert git(repo, "ls-files", "--debug", "--", "main.py", "conflict.txt").stdout == before
    assert git(repo, "show", ":graphify-out/graph.json").stdout == (repo / "graphify-out/graph.json").read_text()


@pytest.mark.parametrize("kind", ["malformed-record", "symlink-record", "changed-bundle"])
def test_cancel_refuses_changed_or_foreign_state_without_index_changes(kind):
    from graphify.merge_finalize import MergeFinalizeError, _record_path, cancel_merge, finalize_merge

    repo = pending_repo()
    finalize_merge(repo, "graphify-out")
    record = _record_path(repo, "graphify-out")
    if kind == "malformed-record":
        record.write_bytes(b"{}")
    elif kind == "symlink-record":
        retained = record.with_suffix(".retained")
        record.rename(retained)
        record.symlink_to(retained)
    else:
        oid = git(repo, "rev-parse", "HEAD:base.py").stdout.strip()
        git(repo, "update-index", "--cacheinfo", f"100644,{oid},graphify-out/manifest.json")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError):
        cancel_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before


def test_interrupted_record_retries_exact_preparation(monkeypatch):
    import graphify.merge_finalize as finalizer

    repo = pending_repo()
    original = finalizer.os.replace
    index = repo / ".git/index"
    before = index.read_bytes()
    def interrupted(source, destination):
        if Path(destination) == index:
            raise OSError("injected publication interruption")
        original(source, destination)
    monkeypatch.setattr(finalizer.os, "replace", interrupted)
    with pytest.raises(finalizer.MergeFinalizeError, match="interruption"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert index.read_bytes() == before
    assert not (repo / ".git/index.lock").exists()
    assert finalizer._record_path(repo, "graphify-out").exists()
    monkeypatch.setattr(finalizer.os, "replace", original)
    content = finalizer.finalize_merge(repo, "graphify-out")
    assert finalizer.validate_index_bundle(repo, "graphify-out").content_id == content


def test_output_filter_is_refused_without_executing_it():
    from graphify.merge_finalize import MergeFinalizeError, finalize_merge

    repo = pending_repo()
    marker = repo / "filter-was-invoked"
    script = repo / ".git/filter.sh"
    script.write_text("#!/bin/sh\ntouch " + str(marker) + "\ncat\n")
    script.chmod(0o755)
    with (repo / ".gitattributes").open("a") as stream:
        stream.write("graphify-out/graph.json filter=probe\n")
    from graphify.merge_guard import _git
    oid = _git(repo, "hash-object", "-w", "--stdin", input=(repo / ".gitattributes").read_bytes()).decode().strip()
    git(repo, "update-index", "--cacheinfo", f"100644,{oid},.gitattributes")
    git(repo, "config", "filter.probe.clean", str(script))
    assert not marker.exists()
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="filters"):
        finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before
    assert not marker.exists()


def test_output_symlink_cannot_borrow_local_authority():
    from graphify.merge_finalize import MergeFinalizeError, finalize_merge

    repo = pending_repo()
    (repo / "alias").symlink_to(repo / "graphify-out", target_is_directory=True)
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="aliases"):
        finalize_merge(repo, "alias")
    assert (repo / ".git/index").read_bytes() == before


def test_cancel_does_not_read_unrelated_filter_sources():
    from graphify.merge_finalize import cancel_merge, finalize_merge

    repo = pending_repo()
    finalize_merge(repo, "graphify-out")
    marker = repo / "unrelated-filter-invoked"
    script = repo / ".git/filter.sh"
    script.write_text("#!/bin/sh\ntouch " + str(marker) + "\ncat\n")
    script.chmod(0o755)
    git(repo, "config", "filter.probe.clean", str(script))
    with (repo / ".gitattributes").open("a") as stream:
        stream.write("main.py filter=probe\n")
    git(repo, "add", ".gitattributes")
    (repo / "main.py").write_text("def dirty_unrelated():\n    return 77\n")
    before = git(repo, "ls-files", "--stage", "--", "main.py", ".gitattributes").stdout
    cancel_merge(repo, "graphify-out")
    assert git(repo, "ls-files", "--stage", "--", "main.py", ".gitattributes").stdout == before
    assert not marker.exists()


def test_repository_identity_change_during_preparation_refuses(monkeypatch):
    import shutil
    import graphify.merge_finalize as finalizer

    repo = pending_repo()
    index_bytes = (repo / ".git/index").read_bytes()
    original = finalizer._extract
    def retarget(root, blobs):
        graph = original(root, blobs)
        (repo / ".git").rename(repo / ".git-original")
        shutil.copytree(repo / ".git-original", repo / ".git")
        return graph
    monkeypatch.setattr(finalizer, "_extract", retarget)
    with pytest.raises(finalizer.MergeFinalizeError, match="identity changed"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == index_bytes
    assert (repo / ".git-original/index").read_bytes() == index_bytes
    assert not finalizer._record_path(repo, "graphify-out").exists()


def test_changed_effective_prehook_selector_refuses(monkeypatch):
    import graphify.merge_finalize as finalizer

    repo = pending_repo()
    original = finalizer._extract
    before = (repo / ".git/index").read_bytes()
    def change_hooks(root, blobs):
        graph = original(root, blobs)
        directory = repo / ".git/foreign-hooks"
        directory.mkdir()
        hook = directory / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        git(repo, "config", "core.hooksPath", str(directory))
        return graph
    monkeypatch.setattr(finalizer, "_extract", change_hooks)
    with pytest.raises(finalizer.MergeFinalizeError, match="identity changed"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before


@pytest.mark.parametrize("profile", ["sparse", "split"])
def test_cancel_refuses_new_unsupported_index_profile(profile):
    from graphify.merge_finalize import MergeFinalizeError, cancel_merge, finalize_merge

    repo = pending_repo()
    finalize_merge(repo, "graphify-out")
    if profile == "sparse":
        git(repo, "config", "core.sparseCheckout", "true")
    else:
        git(repo, "update-index", "--split-index")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(MergeFinalizeError, match="unsupported"):
        cancel_merge(repo, "graphify-out")
    assert (repo / ".git/index").read_bytes() == before
