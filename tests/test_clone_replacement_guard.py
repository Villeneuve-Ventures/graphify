"""Clone destinations require mutating admission before any Git subprocess."""
import subprocess
import sys

import pytest

from graphify import canonical_replacement as replacement
from graphify import cli
from graphify import transaction as tx


@pytest.mark.parametrize("slot", ["archive", "alias", "absent-archive"])
def test_clone_dispatch_refuses_archive_destination(tmp_path, monkeypatch, slot):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.txt").write_bytes(b"retained old graph")
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({
        "request_digest": "prior-operation", "state": "COMMITTED",
    }))
    replacement._register(archive, journal, "prior-operation", "archive")
    replacement._register(tmp_path / "absent-archive", journal,
                          "prior-operation", "archive")
    (tmp_path / "alias").symlink_to(archive, target_is_directory=True)
    before = replacement._tree(archive)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "graphify", "clone", "https://github.com/example/repo",
        "--out", str(tmp_path / slot),
    ])
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        cli.dispatch_command("clone")

    assert calls == []
    assert replacement._tree(archive) == before
    assert not (tmp_path / "absent-archive").exists()


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("branch", [None, "topic"])
def test_ordinary_clone_destination_reaches_git(tmp_path, monkeypatch, exists, branch):
    dest = tmp_path / "cache" / "repo"
    if exists:
        dest.mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._clone_repo("https://github.com/example/repo", branch, dest) == dest
    if exists:
        expected = ["git", "-C", str(dest), "pull"]
        if branch:
            expected += ["origin", "--", branch]
    else:
        expected = ["git", "clone", "--depth", "1"]
        if branch:
            expected += ["--branch", branch]
        expected += ["--", "https://github.com/example/repo.git", str(dest)]
    assert calls == [(expected, {"capture_output": True, "text": True})]
