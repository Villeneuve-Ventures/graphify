"""Reflection destinations use disposable archive guards and no external actors."""
from datetime import datetime, timezone
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from graphify import canonical_replacement as replacement
from graphify import cli
from graphify import reflect as reflection
from graphify import transaction as tx
from tests.test_global_replacement_guard import registration as registration


@pytest.fixture(autouse=True)
def isolated_reflection(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("reflection guard tests must not use network or subprocesses")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(cli, "_GRAPHIFY_OUT", str(tmp_path / "unused-output"))


def _invoke(entry, tmp_path, output, monkeypatch):
    memory = tmp_path / "memory"
    if entry == "cli":
        monkeypatch.setattr(sys, "argv", ["graphify", "reflect", "--memory-dir", str(memory),
                                         "--out", str(output)])
        return cli.dispatch_command("reflect")
    return reflection.reflect(memory, output, now=datetime(2026, 11, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize("entry", ["library", "cli"])
@pytest.mark.parametrize("slot", ["archive", "descendant", "directory-alias", "absent", "file-alias"])
def test_reflect_refuses_retained_destination_before_writing(
    tmp_path, monkeypatch, registration, entry, slot,
):
    archive = tmp_path / "archive"
    if slot != "absent":
        archive.mkdir()
        (archive / "old.txt").write_bytes(b"preserved graph output")
    registration(archive)
    before = replacement._tree(archive) if archive.exists() else None
    if slot == "directory-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(archive, target_is_directory=True)
        output = alias / "reflections" / "LESSONS.md"
    elif slot == "file-alias":
        ordinary = tmp_path / "ordinary"
        ordinary.mkdir()
        output = ordinary / "LESSONS.md"
        output.symlink_to(archive / "old.txt")
    else:
        output = archive / ("LESSONS.md" if slot == "archive" else "reflections/nested/LESSONS.md")

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _invoke(entry, tmp_path, output, monkeypatch)

    if before is None:
        assert not archive.exists()
    else:
        assert replacement._tree(archive) == before
    if slot == "file-alias":
        assert output.is_symlink()
    else:
        assert not output.exists()


@pytest.mark.parametrize("selected", ["directory", "file"])
def test_reflect_rechecks_after_parent_creation_before_truncation(
    tmp_path, monkeypatch, registration, selected,
):
    output = tmp_path / "ordinary" / "LESSONS.md"
    output.parent.mkdir()
    output.write_bytes(b"previous lessons")
    original = Path.mkdir
    observed = []

    def register_after_mkdir(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == output.parent and not observed:
            registration(path if selected == "directory" else output)
            observed.append(replacement._tree(output.parent))
        return result

    monkeypatch.setattr(Path, "mkdir", register_after_mkdir)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _invoke("library", tmp_path, output, monkeypatch)
    assert len(observed) == 1
    assert replacement._tree(output.parent) == observed[0]
    assert output.read_bytes() == b"previous lessons"


@pytest.mark.parametrize("entry", ["library", "cli"])
def test_ordinary_reflection_still_writes_deterministic_lessons(tmp_path, monkeypatch, entry):
    output = tmp_path / "ordinary" / "reflections" / "LESSONS.md"
    _invoke(entry, tmp_path, output, monkeypatch)
    before = output.read_bytes()
    _invoke(entry, tmp_path, output, monkeypatch)
    assert output.read_bytes() == before
    assert b"# Lessons\n" in before
    assert b"0 session memories" in before
    assert not (tmp_path / "memory").exists()
