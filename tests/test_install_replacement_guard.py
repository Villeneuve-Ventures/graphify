"""Installer archive admission checks use only disposable scopes and journals."""
import os
from pathlib import Path
import sys

import pytest

import graphify.install as installer
from graphify import canonical_replacement as replacement
from graphify import transaction as tx

pytestmark = pytest.mark.skipif(os.name != "posix", reason="modeled guard registration is POSIX-only")


@pytest.fixture
def register(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({"request_digest": "prior", "state": "COMMITTED"}))

    def register_archive(path):
        replacement._register(path, journal, "prior", "archive")

    return register_archive


def test_global_agents_install_refuses_absent_archive_slot(tmp_path, monkeypatch, register):
    monkeypatch.setenv("HOME", str(tmp_path))
    archive = tmp_path / ".agents"
    register(archive)
    monkeypatch.setattr(sys, "argv", ["graphify", "install", "--platform", "agents"])

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        installer.dispatch_install_cli("install")
    assert not archive.exists()


def test_global_agents_uninstall_refuses_retained_skill(tmp_path, monkeypatch, register):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / ".agents"
    skill = archive / "skills" / "graphify" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("retained skill")
    register(archive)
    before = replacement._tree(archive)
    monkeypatch.setattr(sys, "argv", ["graphify", "agents", "uninstall"])

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        installer.dispatch_install_cli("agents")
    assert replacement._tree(archive) == before


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_project_agents_refuses_archive_alias(tmp_path, monkeypatch, register, command):
    project, archive = tmp_path / "project", tmp_path / "archive"
    project.mkdir()
    skill = archive / "skills" / "graphify" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("retained skill")
    register(archive)
    alias = project / ".agents"
    alias.symlink_to(archive, target_is_directory=True)
    before = replacement._tree(archive)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["graphify", command, "--project", "--platform", "agents"])

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        installer.dispatch_install_cli(command)
    assert alias.is_symlink()
    assert replacement._tree(archive) == before


def test_project_codex_hook_refuses_file_alias(tmp_path, monkeypatch, register):
    project, archive = tmp_path / "project", tmp_path / "archive"
    project.mkdir()
    archive.mkdir()
    retained = archive / "hooks.json"
    retained.write_text("{}")
    register(archive)
    hooks = project / ".codex" / "hooks.json"
    hooks.parent.mkdir()
    hooks.symlink_to(retained)
    before = replacement._tree(archive)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["graphify", "install", "--project", "--platform", "codex"])

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        installer.dispatch_install_cli("install")
    assert hooks.is_symlink()
    assert replacement._tree(archive) == before


def test_recursive_sidecar_removal_preserves_nested_archive(tmp_path, register):
    project = tmp_path / "project"
    sidecar = project / ".agents" / "skills" / "graphify" / "references"
    retained = sidecar / "preserved"
    retained.mkdir(parents=True)
    (retained / "graph.json").write_text("retained graph")
    register(retained)
    before = replacement._tree(retained)

    with pytest.raises(tx.PendingTransactionError, match="registered replacement"):
        installer._project_uninstall("agents", project)
    assert replacement._tree(retained) == before


def test_skill_rename_rechecks_destination_after_staging(tmp_path, monkeypatch, register):
    project, archive = tmp_path / "project", tmp_path / "archive"
    project.mkdir()
    archive.mkdir()
    retained = archive / "skill.md"
    retained.write_text("retained skill")
    register(archive)
    destination = project / ".agents" / "skills" / "graphify" / "SKILL.md"
    staged = destination.with_suffix(".md.tmp")
    original_copy = installer.shutil.copy

    def alias_after_staging(source, target):
        result = original_copy(source, target)
        if target == staged:
            destination.symlink_to(retained)
        return result

    monkeypatch.setattr(installer.shutil, "copy", alias_after_staging)
    before = replacement._tree(archive)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        installer._project_install("agents", project)
    assert destination.is_symlink()
    assert not staged.exists()
    assert replacement._tree(archive) == before


def test_hook_directory_does_not_create_registered_archive(tmp_path, register):
    import subprocess

    from graphify import hooks

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    archive = tmp_path / "archive"
    register(archive)
    subprocess.run(["git", "-C", str(repo), "config", "--local", "core.hooksPath", str(archive)], check=True)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        hooks._hooks_dir(repo)
    assert not archive.exists()


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_merge_driver_preserves_gitattributes_archive_alias(tmp_path, register, operation):
    import subprocess

    from graphify import hooks

    repo, archive = tmp_path / "repo", tmp_path / "archive"
    repo.mkdir()
    archive.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    retained = archive / "attributes"
    content = "*.txt text\n"
    if operation == "uninstall":
        content += "graphify-out/graph.json merge=graphify\n"
    retained.write_text(content, encoding="utf-8")
    register(archive)
    alias = repo / ".gitattributes"
    alias.symlink_to(retained)
    action = hooks._register_merge_driver if operation == "install" else hooks._unregister_merge_driver

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        action(repo)
    assert alias.is_symlink()
    assert retained.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_merge_driver_preserves_retained_git_config(tmp_path, register, operation):
    import subprocess

    from graphify import hooks

    repo, archive = tmp_path / "repo", tmp_path / "archive"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "--separate-git-dir", str(archive)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "--local", "merge.graphify.name", "retained"], check=True)
    config = archive / "config"
    before = config.read_bytes()
    register(archive)
    action = hooks._register_merge_driver if operation == "install" else hooks._unregister_merge_driver

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        action(repo)
    assert config.read_bytes() == before
    assert not (archive / "config.lock").exists()
