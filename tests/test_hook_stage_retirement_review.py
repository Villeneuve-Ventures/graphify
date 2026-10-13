"""Deferred #199 history admission and integrated #208 diagnostic regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from graphify import hooks
from tests.test_hook_installation import repo, _hooks, _snapshot  # noqa: F401
from tests.test_hook_authority_lifecycle import body, slot
from tests.test_hook_authority_review import _evidence, _interrupt

if os.name != "nt":
    from graphify import hook_installation as publication

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX retained history")


def protected(root):
    """Include symlink identities and targets beside directory and file evidence."""
    links = {str(path.relative_to(root)): (path.lstat().st_ino, path.lstat().st_mode,
              path.lstat().st_uid, path.lstat().st_gid, os.readlink(path))
             for path in root.rglob("*") if path.is_symlink()}
    return _evidence(root), links


def damage(root, kind):
    """Change only one completed fixture artifact, preserving all other evidence."""
    stage = _hooks(root) / body(root)["history"][0]["name"]
    if kind == "unknown":
        (stage / "foreign").write_bytes(b"preserve foreign content")
    elif kind == "missing":
        (stage / "pre-0").unlink()
    elif kind == "symlink":
        target = root / "foreign-preserved"
        target.write_bytes(b"do not follow or replace")
        (stage / "pre-0").unlink()
        (stage / "pre-0").symlink_to(target)
    elif kind == "mode":
        (stage / "pre-0").chmod(0o600)
    else:
        (stage / (".gitignore" if kind == "exclusion" else "pre-0")).write_bytes(b"changed evidence")


def cli(root, *arguments):
    """Use the current source's public CLI with explicit source discovery."""
    return subprocess.run([sys.executable, "-B", "-m", "graphify", "hook", *arguments], cwd=root,
                          env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
                          capture_output=True, text=True)


@pytest.mark.parametrize("kind", ["changed", "missing", "unknown", "exclusion", "symlink", "mode"])
@pytest.mark.parametrize("entry", ["api", "cli"])
@pytest.mark.parametrize("merge_guard", [False, True])
def test_damaged_completed_history_status_is_unverified_and_read_only(repo, kind, entry, merge_guard):
    """Status cannot represent corrupted completed v2 evidence as admitted history."""
    hooks.install(repo, merge_guard=merge_guard)
    damage(repo, kind)
    before = protected(repo), protected(publication._state_root())
    if entry == "api":
        result = hooks.status(repo, merge_guard=merge_guard)
    else:
        command = cli(repo, "status", *(["--merge-guard"] if merge_guard else []))
        assert command.returncode == 0 and "Traceback" not in command.stderr, command.stderr
        result = command.stdout
    assert "unverified" in result and "completed stages retained:" not in result
    assert (protected(repo), protected(publication._state_root())) == before


@pytest.mark.parametrize("kind", ["changed", "missing", "unknown", "exclusion", "symlink", "mode"])
@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("merge_guard", [False, True])
def test_damaged_completed_history_blocks_ordinary_mutation(repo, kind, operation, merge_guard):
    """Damaged historical payloads block ordinary operations before live or authority writes."""
    hooks.install(repo, merge_guard=merge_guard)
    damage(repo, kind)
    before = protected(repo), protected(publication._state_root())
    with pytest.raises(RuntimeError):
        getattr(hooks, operation)(repo, merge_guard=merge_guard)
    assert (protected(repo), protected(publication._state_root())) == before


@pytest.mark.parametrize("missing", ["inventory", "ownership", "metadata"])
def test_older_v2_history_without_required_evidence_blocks_latest_selection(repo, missing):
    """All history needs its own evidence, even when selecting a newer intact batch."""
    hooks.install(repo)
    hooks.uninstall(repo)
    hooks.install(repo)
    records = body(repo)["history"]
    latest = records[-1]["name"]
    token = hooks.retire(repo, batch=latest)["token"]
    child = os.open(slot(repo), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        store = publication._Store(child, publication._identity(_hooks(repo).stat()), False)
        if missing == "inventory":
            del store.body["history"][0][missing]
        else:
            del store.body["history"][0]["inventory"]["pre-0"][missing]
        store.save()
    finally:
        os.close(child)
    before = protected(repo), protected(publication._state_root())
    assert "unverified" in hooks.status(repo)
    for operation in (lambda: hooks.install(repo), lambda: hooks.uninstall(repo),
                      lambda: hooks.retire(repo, batch=latest),
                      lambda: hooks.retire(repo, batch=latest, expected=token)):
        with pytest.raises(RuntimeError):
            operation()
        assert (protected(repo), protected(publication._state_root())) == before


@pytest.mark.parametrize("phase", ["completed", "active", "retained"])
def test_candidate_warning_coexists_with_pending_and_history_summary(repo, monkeypatch, phase):
    """An unowned candidate remains visible without destroying typed canonical observations."""
    write = publication._write

    def interrupted(fd, name, *args, **kwargs):
        """Leave an unpublished candidate without authenticating it."""
        if name == "capability":
            raise OSError("injected candidate interruption")
        return write(fd, name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", interrupted)
        with pytest.raises(RuntimeError, match="candidate interruption"):
            hooks.install(repo)
    if phase != "completed":
        _interrupt(repo, monkeypatch, "preparing")
    if phase != "active":
        hooks.install(repo)
    before = protected(repo), protected(publication._state_root())
    result = hooks.status(repo)
    assert "unpublished authority candidates retained" in result
    pending = publication.pending(_hooks(repo))
    assert isinstance(pending, str) and "unpublished authority candidates retained" in pending
    if phase == "active":
        assert "pending install (PREPARING)" in result
        record = publication.recovery_record(_hooks(repo))
        assert isinstance(record, dict) and record["binding"] == body(repo)["active"]["binding"]
    else:
        assert "post-commit: installed" in result and "merge driver: registered" in result
        assert "completed stages retained:" in result
        if phase == "retained":
            assert "retained incomplete evidence: 1" in result
    assert (protected(repo), protected(publication._state_root())) == before


@pytest.mark.parametrize("phase", ["completed", "active", "retained", "archive"])
def test_unclaimed_warning_preserves_known_pending_objects(repo, monkeypatch, phase):
    """Show unknown private residue beside pending work without misclassifying its stage."""
    if phase in ("active", "retained"):
        _interrupt(repo, monkeypatch, "preparing")
    if phase != "active":
        hooks.install(repo)
    if phase == "archive":
        batch = body(repo)["history"][0]["name"]
        token = hooks.retire(repo, batch=batch)["token"]
        write = publication._write

        def interrupted(fd, name, *args, **kwargs):
            """Leave a journal-bound incomplete archive for its exact retry."""
            if name == "payloads-0":
                raise OSError("injected archive interruption")
            return write(fd, name, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(publication, "_write", interrupted)
            with pytest.raises(RuntimeError, match="archive interruption"):
                hooks.retire(repo, batch=batch, expected=token)
    assert "unclaimed private" not in hooks.status(repo)
    unknown = slot(repo) / (publication._PREFIX + "foreign-preserved")
    unknown.mkdir(mode=0o700)
    (unknown / "foreign").write_bytes(b"no ownership from this name")
    before = protected(repo), protected(publication._state_root())
    result = hooks.status(repo)
    assert "unclaimed private preparation or archive artifacts retained" in result
    pending = publication.pending(_hooks(repo))
    assert isinstance(pending, str) and "unclaimed private" in pending
    if phase == "active":
        assert "pending install (PREPARING)" in result
        record = publication.recovery_record(_hooks(repo))
        assert isinstance(record, dict) and record["binding"] == body(repo)["active"]["binding"]
    elif phase == "archive":
        assert "pending stage archival" in result
        record = publication.recovery_record(_hooks(repo))
        assert isinstance(record, dict) and record["binding"] == body(repo)["maintenance"]["binding"]
    else:
        assert "completed stages retained:" in result and "unclaimed artifacts: 1" in result
    assert (protected(repo), protected(publication._state_root())) == before


@pytest.mark.parametrize("copy_authority", [False, True])
@pytest.mark.parametrize("archived", [False, True])
def test_changed_authority_root_cannot_adopt_history(repo, monkeypatch, copy_authority, archived):
    """Empty or copied authority at another safe root cannot authenticate old objects."""
    hooks.install(repo)
    batch = body(repo)["history"][0]["name"]
    token = hooks.retire(repo, batch=batch)["token"]
    if archived:
        hooks.retire(repo, batch=batch, expected=token)
    old_root = publication._state_root()
    new_home = Path.home() / "other-authority-home"
    new_home.mkdir(mode=0o700)
    with monkeypatch.context() as patch:
        patch.setenv("HOME", str(new_home))
        patch.setenv("XDG_STATE_HOME", str(new_home / "state"))
        new_root = publication._state_root()
        with publication._authority(new_root, True):
            pass
        if copy_authority:
            for source in old_root.iterdir():
                shutil.copytree(source, new_root / source.name)
        before = protected(repo), protected(old_root), protected(new_root)
        assert "unverified" in hooks.status(repo)
        for operation in (lambda: hooks.retire(repo, batch=batch),
                          lambda: hooks.retire(repo, batch=batch, expected=token),
                          lambda: hooks.install(repo), lambda: hooks.uninstall(repo)):
            with pytest.raises(RuntimeError):
                operation()
            assert (protected(repo), protected(old_root), protected(new_root)) == before


def test_shared_hooks_damaged_history_refuses_from_another_repository(repo, tmp_path):
    """A different registration context cannot bypass completed evidence admission."""
    directory = tmp_path / "shared-hooks"
    directory.mkdir(mode=0o700)
    (directory / "post-commit").write_bytes(b"#!/bin/sh\n# shared user hook\n")
    other = tmp_path / "other"
    subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
    for root in (repo, other):
        subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", str(directory)], check=True)
    hooks.install(repo)
    identity = publication._identity(directory.stat())
    state = publication._state_root() / publication._slot(identity)
    record = json.loads((state / "journal").read_bytes())["body"]["history"][0]
    token = hooks.retire(other, batch=record["name"])["token"]
    hooks.retire(other, batch=record["name"], expected=token)
    (directory / record["name"] / "pre-0").write_bytes(b"changed shared history")
    before = tuple(protected(path) for path in (repo, other, directory, publication._state_root()))
    assert "unverified" in hooks.status(other)
    for operation in (lambda: hooks.install(other), lambda: hooks.uninstall(other),
                      lambda: hooks.retire(other, batch=record["name"])):
        with pytest.raises(RuntimeError):
            operation()
        assert tuple(protected(path) for path in (repo, other, directory, publication._state_root())) == before


@pytest.mark.skipif(sys.platform != "linux" or os.getuid() == 0
                   or os.environ.get("GRAPHIFY_TEST_NATIVE_OTHER_ACCOUNT") != "1",
                   reason="Explicit native Linux guest-to-root qualification")
@pytest.mark.parametrize("archived", [False, True])
def test_native_other_account_refuses_without_adoption(repo, archived):
    """An actual different UID must refuse private authority, including archived history."""
    hooks.install(repo)
    batch = body(repo)["history"][0]["name"]
    token = hooks.retire(repo, batch=batch)["token"]
    if archived:
        hooks.retire(repo, batch=batch, expected=token)
    before = protected(repo), protected(publication._state_root())
    code = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from graphify import hooks
root, batch, token = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
result = {'uid': os.getuid(), 'status': hooks.status(root), 'refusals': []}
for operation in (lambda: hooks.retire(root, batch=batch),
                  lambda: hooks.retire(root, batch=batch, expected=token),
                  lambda: hooks.install(root), lambda: hooks.uninstall(root)):
    try:
        operation()
    except RuntimeError as exc:
        result['refusals'].append(str(exc))
print(json.dumps(result))
"""
    command = ["sudo", "-n", "--", "env", "PATH=" + os.environ["PATH"],
               "XDG_STATE_HOME=" + os.environ["XDG_STATE_HOME"], "PYTHONDONTWRITEBYTECODE=1",
               "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=safe.directory", "GIT_CONFIG_VALUE_0=" + str(repo),
               sys.executable, "-B", "-c", code, str(Path(hooks.__file__).parent.parent), str(repo), batch, token]
    probe = subprocess.run(command, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    result = json.loads(probe.stdout)
    assert result["uid"] == 0 and result["uid"] != os.getuid()
    assert "unverified" in result["status"]
    assert len(result["refusals"]) == 4
    assert all("Unsafe installer authority" in error for error in result["refusals"])
    assert (protected(repo), protected(publication._state_root())) == before


def test_legacy_history_keeps_original_admission_and_refuses_retirement(repo):
    """V1 lacks the new manifest guarantee and is neither migrated nor adopted."""
    identity = publication._identity(_hooks(repo).stat())
    with publication._authority(publication._state_root(), True) as (fd, _):
        publication._mkdir_private(fd, publication._slot(identity))
        child = os.open(publication._slot(identity), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            publication._Store(child, identity, True)
        finally:
            os.close(child)
    hooks.install(repo)
    batch = body(repo)["history"][0]["name"]
    (_hooks(repo) / batch / "pre-0").write_bytes(b"legacy evidence remains unclaimed")
    before = protected(repo), protected(publication._state_root())
    assert "post-commit: installed" in hooks.status(repo)
    with pytest.raises(RuntimeError, match="Legacy history"):
        hooks.retire(repo, batch=batch)
    assert body(repo)["schema"] == publication._SCHEMA
    assert (protected(repo), protected(publication._state_root())) == before
