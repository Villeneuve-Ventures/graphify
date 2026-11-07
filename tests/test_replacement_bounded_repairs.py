"""Replacement regressions on disposable roots with inert maintenance only."""
import importlib
import json
import os
from pathlib import Path
import stat

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


@pytest.fixture
def uninstall(monkeypatch):
    installer = importlib.import_module("graphify.install")
    for name in (
        "claude_uninstall", "codebuddy_uninstall", "gemini_uninstall", "vscode_uninstall",
        "_cursor_uninstall", "_kiro_uninstall", "_antigravity_uninstall", "_agents_uninstall",
        "_remove_skill_file", "_uninstall_opencode_plugin", "_uninstall_codex_hook",
    ):
        monkeypatch.setattr(installer, name, lambda *args, **kwargs: None)
    monkeypatch.setattr("graphify.hooks._git_root", lambda path: None)
    return installer.uninstall_all


@pytest.mark.parametrize("alias", [False, True])
def test_uninstall_purge_preserves_registered_archive(case, modeled, tmp_path, uninstall, alias):
    request, fragments = case
    request["archive"] = str(Path(request["archive"]).parent / "graphify-out")
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan["preview_sha256"])
    archive = Path(request["archive"])
    project = archive.parent
    if alias:
        project = tmp_path / "alias-project"
        project.mkdir()
        (project / "graphify-out").symlink_to(archive, target_is_directory=True)
    before = replacement._tree(archive)
    identity = archive.stat().st_ino

    with pytest.raises(tx.PendingTransactionError, match="archive"):
        uninstall(project, purge=True)

    assert archive.stat().st_ino == identity
    assert replacement._tree(archive) == before
    if alias:
        assert (project / "graphify-out").is_symlink()


def test_uninstall_purge_removes_ordinary_output(tmp_path, uninstall):
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_bytes(b"ordinary graph")
    uninstall(tmp_path, purge=True)
    assert not output.exists()


def test_uninstall_without_purge_keeps_output(tmp_path, uninstall):
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_bytes(b"keep")
    uninstall(tmp_path)
    assert (output / "graph.json").read_bytes() == b"keep"


@pytest.mark.parametrize("mask", [0o022, 0o777])
def test_sealed_stage_remains_private_and_readable_under_umask(case, mask):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    previous = os.umask(mask)
    try:
        replacement._seal_data(request, sealed)
    finally:
        os.umask(previous)
    stage = Path(request["stage"])
    assert stat.S_IMODE(stage.stat().st_mode) == 0o600
    assert replacement._load(stage) == json.loads(replacement._bytes(sealed))
