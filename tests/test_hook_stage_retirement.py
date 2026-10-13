"""Explicit archival retires references, never physical stages or foreign objects."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from graphify import hooks
from tests.test_hook_installation import repo, _hooks, _live, _snapshot  # noqa: F401
from tests.test_hook_authority_lifecycle import body, slot

if os.name != "nt":
    from graphify import hook_installation as publication

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX authority lifecycle")


def completed(root):
    hooks.install(root)
    return body(root)["history"][-1]["name"]


def protected(root):
    return _snapshot(root), _snapshot(publication._state_root())


def test_preview_and_archive_keep_every_original_and_bind_copies(repo, monkeypatch):
    batch = completed(repo)
    before = protected(repo)
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", lambda *args, **kwargs: pytest.fail("preview wrote a file"))
        patch.setattr(publication, "_mkdir_private", lambda *args, **kwargs: pytest.fail("preview created a directory"))
        preview = hooks.retire(repo, batch=batch)
    assert preview["state"] == "COMPLETED_RETAINED"
    assert protected(repo) == before
    old_stage = _hooks(repo) / batch
    original = _snapshot(old_stage)
    live = _live(repo)
    result = hooks.retire(repo, batch=batch, expected=preview["token"])
    assert result["state"] == "RETIRED_ARCHIVED" and result["physical_stage"] == "retained"
    assert _snapshot(old_stage) == original and _live(repo) == live
    archive = Path(result["archive"])
    manifest = json.loads((archive / "manifest").read_bytes())["body"]
    for member, entry in manifest["payloads"].items():
        assert (archive / entry["file"]).read_bytes() == (old_stage / member).read_bytes()
        assert entry["stored"]["identity"] != entry["original"]["identity"]
        assert entry["original"]["ownership"] == [os.getuid(), (old_stage / member).stat().st_gid]
    assert manifest["issuer_uid"] == os.getuid()
    retained = protected(repo)
    assert hooks.retire(repo, batch=batch, expected=preview["token"]) == result
    assert protected(repo) == retained
    status = hooks.status(repo)
    assert "archived references: 1" in status and f"completed batch: {batch}" in status
    assert protected(repo) == retained


@pytest.mark.parametrize("damage", ["missing-ignore", "changed-file", "unknown", "substituted", "missing"])
def test_unverified_source_refuses_before_any_archive_mutation(repo, damage):
    batch = completed(repo)
    preview = hooks.retire(repo, batch=batch)
    stage = _hooks(repo) / batch
    if damage == "missing-ignore":
        (stage / ".gitignore").rename(stage / "retained-ignore")
    elif damage == "changed-file":
        (stage / "pre-0").write_bytes(b"foreign modification")
    elif damage == "unknown":
        (stage / "foreign").write_bytes(b"keep this")
    else:
        stage.rename(_hooks(repo) / "retained-original")
        if damage == "substituted":
            stage.mkdir(mode=0o700)
            (stage / "foreign").write_bytes(b"keep foreign directory")
    before = protected(repo)
    with pytest.raises(RuntimeError):
        hooks.retire(repo, batch=batch, expected=preview["token"])
    assert protected(repo) == before


def test_missing_original_is_accepted_only_after_archive_and_substitution_still_refuses(repo):
    batch = completed(repo)
    token = hooks.retire(repo, batch=batch)["token"]
    hooks.retire(repo, batch=batch, expected=token)
    stage = _hooks(repo) / batch
    stage.rename(repo / "original-preserved")
    before = protected(repo)
    assert hooks.retire(repo, batch=batch)["state"] == "RETIRED_ARCHIVED"
    assert protected(repo) == before
    assert "pending" not in hooks.status(repo)
    hooks.install(repo)
    stage.mkdir(mode=0o700)
    (stage / "foreign").write_bytes(b"not the recorded directory")
    before = protected(repo)
    with pytest.raises(RuntimeError):
        hooks.install(repo)
    assert protected(repo) == before


def test_archive_preserves_complete_private_reference_closure(repo):
    first = completed(repo)
    hooks.uninstall(repo)
    last = completed(repo)
    original_records = {record["retired"]: (slot(repo) / record["retired"]).read_bytes() for record in body(repo)["history"]}
    token = hooks.retire(repo, batch=last)["token"]
    result = hooks.retire(repo, batch=last, expected=token)
    archive = Path(result["archive"])
    manifest = json.loads((archive / "manifest").read_bytes())["body"]
    assert set(manifest["completions"]) == set(original_records)
    for name, entry in manifest["completions"].items():
        assert (archive / entry["file"]).read_bytes() == original_records[name]
        assert (slot(repo) / name).read_bytes() == original_records[name]
    assert (_hooks(repo) / first).exists()


@pytest.mark.parametrize("point", ["payloads-0", "completions-0", "manifest", "terminal"])
def test_interrupted_archive_keeps_partial_copy_and_exact_retry(repo, monkeypatch, point):
    batch = completed(repo)
    token = hooks.retire(repo, batch=batch)["token"]
    original = _snapshot(_hooks(repo))
    write, save = publication._write, publication._Store.save

    def fail_write(fd, name, *args, **kwargs):
        if name == point:
            raise OSError("injected archive write failure")
        return write(fd, name, *args, **kwargs)

    def fail_terminal(self):
        if point == "terminal" and self.body.get("maintenance") is None and any("archive" in r for r in self.body["history"]):
            raise OSError("injected terminal failure")
        return save(self)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", fail_write)
        patch.setattr(publication._Store, "save", fail_terminal)
        with pytest.raises(RuntimeError, match="injected"):
            hooks.retire(repo, batch=batch, expected=token)
    archives = {str(path): _snapshot(path) for path in slot(repo).glob(publication._ARCHIVE_PREFIX + "*")}
    assert archives and _snapshot(_hooks(repo)) == original
    before = protected(repo)
    assert "pending stage archival" in hooks.status(repo)
    with pytest.raises(RuntimeError, match="Pending stage archival"):
        hooks.uninstall(repo)
    assert protected(repo) == before
    assert hooks.retire(repo, batch=batch, expected=token)["state"] == "RETIRED_ARCHIVED"
    assert all(_snapshot(Path(path)) == snapshot for path, snapshot in archives.items())
    assert _snapshot(_hooks(repo)) == original


@pytest.mark.parametrize("damage", ["manifest", "payload", "ignore", "extra", "directory"])
def test_changed_archive_never_falls_back_to_original_stage(repo, damage):
    batch = completed(repo)
    token = hooks.retire(repo, batch=batch)["token"]
    archive = Path(hooks.retire(repo, batch=batch, expected=token)["archive"])
    if damage == "directory":
        archive.rename(archive.with_name("retained-archive"))
        archive.mkdir(mode=0o700)
    elif damage == "extra":
        (archive / "foreign").write_bytes(b"foreign archive content")
    else:
        name = {"manifest": "manifest", "payload": "payloads-0", "ignore": ".gitignore"}[damage]
        (archive / name).write_bytes(b"foreign replacement bytes")
    before = protected(repo)
    assert "pending/unverified" in hooks.status(repo)
    with pytest.raises(RuntimeError):
        hooks.install(repo)
    assert protected(repo) == before


def test_stale_preview_and_changed_account_refuse_without_mutation(repo):
    batch = completed(repo)
    token = hooks.retire(repo, batch=batch)["token"]
    hooks.uninstall(repo)
    before = protected(repo)
    with pytest.raises(RuntimeError, match="Stale retirement preview"):
        hooks.retire(repo, batch=batch, expected=token)
    assert protected(repo) == before
    with publication._authority(publication._state_root(), False) as (fd, _):
        child = os.open(publication._slot(publication._identity(_hooks(repo).stat())), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            store = publication._Store(child, publication._identity(_hooks(repo).stat()), False)
            store.body["issuer_uid"] += 1
            store.save()
        finally:
            os.close(child)
    before = protected(repo)
    with pytest.raises(RuntimeError, match="Unauthenticated"):
        hooks.retire(repo, batch=batch)
    assert protected(repo) == before


def test_cli_preview_and_execute_are_explicit_and_preserve_originals(repo):
    batch = completed(repo)
    command = [sys.executable, "-B", "-m", "graphify", "hook", "retire", "--batch", batch]
    env = {**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)}
    before = protected(repo)
    preview = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert protected(repo) == before
    token = json.loads(preview.stdout)["token"]
    original = _snapshot(_hooks(repo))
    result = subprocess.run([*command, "--execute", token], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "RETIRED_ARCHIVED"
    assert _snapshot(_hooks(repo)) == original


def test_active_work_and_legacy_history_block_retirement(repo, monkeypatch):
    from tests.test_hook_installation import _interrupt
    batch = completed(repo)
    _interrupt(repo, monkeypatch, "uninstall")
    before = protected(repo)
    with pytest.raises(RuntimeError, match="Active hook work"):
        hooks.retire(repo, batch=batch)
    assert protected(repo) == before


def test_pending_archive_source_change_and_wrong_token_preserve_all_state(repo, monkeypatch):
    batch = completed(repo)
    token = hooks.retire(repo, batch=batch)["token"]
    write = publication._write
    def fail(fd, name, *args, **kwargs):
        if name == "payloads-0":
            raise OSError("stop archive")
        return write(fd, name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", fail)
        with pytest.raises(RuntimeError):
            hooks.retire(repo, batch=batch, expected=token)
    before = protected(repo)
    with pytest.raises(RuntimeError, match="exact original request"):
        hooks.retire(repo, batch=batch, expected="wrong")
    assert protected(repo) == before
    (_hooks(repo) / batch / "pre-0").write_bytes(b"foreign content after interruption")
    before = protected(repo)
    with pytest.raises(RuntimeError, match="member changed"):
        hooks.retire(repo, batch=batch, expected=token)
    assert protected(repo) == before


def test_legacy_retirement_does_not_upgrade_or_create_archive(repo):
    identity = publication._identity(_hooks(repo).stat())
    with publication._authority(publication._state_root(), True) as (fd, _):
        publication._mkdir_private(fd, publication._slot(identity))
        child = os.open(publication._slot(identity), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            publication._Store(child, identity, True)
        finally:
            os.close(child)
    batch = completed(repo)
    before = protected(repo)
    with pytest.raises(RuntimeError, match="Legacy history"):
        hooks.retire(repo, batch=batch)
    assert protected(repo) == before
    assert body(repo)["schema"] == publication._SCHEMA


@pytest.mark.parametrize("hooks_path", [".githooks", ".husky/_", "shared"])
def test_retirement_custom_and_shared_hooks_preserves_registration(repo, tmp_path, hooks_path):
    selected = tmp_path / "shared-hooks" if hooks_path == "shared" else repo / hooks_path
    selected.mkdir(parents=True)
    directory = selected.parent if selected.name == "_" else selected
    (directory / "post-commit").write_bytes(b"#!/bin/sh\n# private user preimage\n")
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(selected)], check=True)
    hooks.install(repo)
    identity = publication._identity(directory.stat())
    state = publication._state_root() / publication._slot(identity)
    record = json.loads((state / "journal").read_bytes())["body"]["history"][0]
    target = repo
    if hooks_path == "shared":
        target = tmp_path / "other"
        subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "config", "core.hooksPath", str(directory)], check=True)
    before = _snapshot(repo), _snapshot(target), _snapshot(directory)
    token = hooks.retire(target, batch=record["name"])["token"]
    assert hooks.retire(target, batch=record["name"], expected=token)["state"] == "RETIRED_ARCHIVED"
    assert before == (_snapshot(repo), _snapshot(target), _snapshot(directory))


@pytest.mark.parametrize("point", ["allocated", "copied", "committed"])
def test_process_kill_at_archive_persistence_boundary(repo, point):
    batch = completed(repo)
    token = hooks.retire(repo, batch=batch)["token"]
    code = """
import os, signal, sys
from pathlib import Path
from graphify import hooks, hook_installation as p
save, write = p._Store.save, p._write
def saved(self):
    result = save(self)
    pending = self.body.get('maintenance')
    if (sys.argv[4] == 'allocated' and pending and pending.get('archive')
            or sys.argv[4] == 'committed' and not pending and any('archive' in r for r in self.body['history'])):
        os.kill(os.getpid(), signal.SIGKILL)
    return result
def written(fd, name, *args, **kwargs):
    result = write(fd, name, *args, **kwargs)
    if sys.argv[4] == 'copied' and name == 'payloads-0':
        os.kill(os.getpid(), signal.SIGKILL)
    return result
p._Store.save, p._write = saved, written
hooks.retire(Path(sys.argv[1]), batch=sys.argv[2], expected=sys.argv[3])
"""
    original = _snapshot(_hooks(repo))
    killed = subprocess.run([sys.executable, "-B", "-c", code, str(repo), batch, token, point], capture_output=True)
    assert killed.returncode == -signal.SIGKILL, killed.stderr.decode()
    assert _snapshot(_hooks(repo)) == original
    assert hooks.retire(repo, batch=batch, expected=token)["state"] == "RETIRED_ARCHIVED"
    assert _snapshot(_hooks(repo)) == original
