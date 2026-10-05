"""Public installer interruption, authority and preservation regressions."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest
from graphify import hooks

if os.name != "nt":
    from graphify import hook_installation as publication

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX atomic installer; Windows legacy dispatch is unchanged")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".graphify-hook-proof-", dir=Path.home()) as home:
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("XDG_STATE_HOME", str(Path(home) / "state"))
        root = tmp_path / "repo"
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        for name in publication.NAMES:
            target = root / ".git/hooks" / name
            target.write_bytes(b"#!/bin/sh\nprintf 'user hook\\n'\n")
            target.chmod(0o751)
        yield root


def _hooks(root):
    return root / ".git/hooks"


def _snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_ino,
            hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def _live(root):
    return {name: (_hooks(root) / name).read_bytes() if (_hooks(root) / name).exists() else None
            for name in publication.NAMES}


def _interrupt(root, monkeypatch, operation="install"):
    real = publication._rename
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("injected second publication failure")
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename", fail_second)
        with pytest.raises(RuntimeError, match="incomplete"):
            getattr(hooks, operation)(root)
    assert len(calls) == 2


def test_partial_batch_is_complete_per_path_and_resumes_exactly(repo, monkeypatch):
    old = _live(repo)
    _interrupt(repo, monkeypatch)
    partial = _live(repo)
    assert hooks._HOOK_MARKER.encode() in partial["post-commit"]
    assert partial["post-checkout"] == old["post-checkout"]
    assert partial["post-merge"] == old["post-merge"]
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending install" in hooks.status(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    assert "appended to existing" in hooks.install(repo)
    assert "pending" not in hooks.status(repo)
    for name, marker in zip(publication.NAMES, (hooks._HOOK_MARKER, hooks._CHECKOUT_MARKER, hooks._POST_MERGE_MARKER), strict=True):
        target = _hooks(repo) / name
        assert marker.encode() in target.read_bytes()
        assert b"printf 'user hook\\n'" in target.read_bytes()
        assert stat.S_IMODE(target.stat().st_mode) == 0o751


@pytest.mark.parametrize("failure", ["write", "mode", "seal"])
def test_prepublication_failure_preserves_all_live_hooks(repo, monkeypatch, failure):
    before = _live(repo)
    write = publication._write
    save = publication._Store.save

    def fail_write(fd, name, data, mode):
        if name == "1":
            raise OSError("stage write failed")
        return write(fd, name, data, mode)

    def fail_mode(fd, mode):
        if mode == 0o751:
            raise OSError("stage chmod failed")
        return original_chmod(fd, mode)

    def fail_seal(self):
        if self.body["active"]:
            raise OSError("seal failed")
        return save(self)

    original_chmod = os.fchmod
    if failure == "write":
        monkeypatch.setattr(publication, "_write", fail_write)
    elif failure == "mode":
        monkeypatch.setattr(publication.os, "fchmod", fail_mode)
    else:
        monkeypatch.setattr(publication._Store, "save", fail_seal)
    with pytest.raises(RuntimeError, match="incomplete"):
        hooks.install(repo)
    assert _live(repo) == before
    assert not (repo / ".gitattributes").exists()
    assert list(_hooks(repo).glob(publication._PREFIX + "*"))


def test_never_writes_or_chmods_a_live_hook_and_preserves_noop_inodes(repo, monkeypatch):
    live_paths = {_hooks(repo) / name for name in publication.NAMES}
    write_bytes, write_text, chmod = Path.write_bytes, Path.write_text, Path.chmod

    def guarded(function):
        def invoke(path, *args, **kwargs):
            assert path not in live_paths, f"in-place hook mutation: {path}"
            return function(path, *args, **kwargs)
        return invoke

    monkeypatch.setattr(Path, "write_bytes", guarded(write_bytes))
    monkeypatch.setattr(Path, "write_text", guarded(write_text))
    monkeypatch.setattr(Path, "chmod", guarded(chmod))
    hooks.install(repo)
    before = _snapshot(_hooks(repo))
    hooks.install(repo)
    assert _snapshot(_hooks(repo)) == before
    hooks.uninstall(repo)
    assert all(b"graphify-hook-start" not in data for data in _live(repo).values())


def test_mode_only_repair_is_atomic_and_preserves_bytes(repo):
    hooks.install(repo)
    target = _hooks(repo) / "post-commit"
    expected = target.read_bytes()
    target.chmod(0o640)
    inode = target.stat().st_ino
    hooks.install(repo)
    assert target.read_bytes() == expected
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert target.stat().st_ino != inode


def test_actual_foreign_displacement_and_independent_preimage_survive(repo, monkeypatch):
    old = (_hooks(repo) / "post-commit").read_bytes()
    real = publication._rename

    def substitute(source_fd, source, target_fd, target, **kwargs):
        if target == "post-commit":
            incoming = _hooks(repo) / "foreign-hook"
            incoming.write_bytes(b"foreign hook from another editor\n")
            os.replace(incoming, _hooks(repo) / target)
        return real(source_fd, source, target_fd, target, **kwargs)

    monkeypatch.setattr(publication, "_rename", substitute)
    with pytest.raises(RuntimeError, match="displaced object and preimage retained"):
        hooks.install(repo)
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    assert (stage / "0").read_bytes() == b"foreign hook from another editor\n"
    assert (stage / "pre-0").read_bytes() == old
    assert hooks._HOOK_MARKER.encode() in (_hooks(repo) / "post-commit").read_bytes()
    before = _snapshot(repo)
    with pytest.raises(RuntimeError, match="Foreign or uncertain"):
        hooks.install(repo)
    assert _snapshot(repo) == before


@pytest.mark.parametrize("change", ["output", "interpreter", "operation", "registration", "shared-root"])
def test_changed_pending_request_refuses_before_hook_or_registration_mutation(repo, monkeypatch, tmp_path, change):
    _interrupt(repo, monkeypatch)
    target, operation = repo, hooks.install
    if change == "output":
        monkeypatch.setattr("graphify.paths.GRAPHIFY_OUT", "changed-output")
    elif change == "interpreter":
        monkeypatch.setattr(hooks, "_pinned_python", lambda: "/different/python")
    elif change == "operation":
        operation = hooks.uninstall
    elif change == "registration":
        subprocess.run(["git", "-C", str(repo), "config", "test.changed", "true"], check=True)
    else:
        target = tmp_path / "other-root"
        subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "config", "core.hooksPath", str(_hooks(repo))], check=True)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("registration must not run"))
    monkeypatch.setattr(hooks, "_unregister_merge_driver", lambda *a, **k: pytest.fail("registration must not run"))
    with pytest.raises(RuntimeError, match="different request"):
        operation(target)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))


@pytest.mark.parametrize("damage", ["missing-key", "corrupt-journal", "substituted-stage", "changed-stage-file",
                                    "missing-journal", "substituted-key", "substituted-authority"])
def test_unknown_authority_or_stage_refuses_without_cleanup(repo, monkeypatch, damage):
    _interrupt(repo, monkeypatch)
    slot, = (p for p in publication._state_root().iterdir() if p.is_dir())
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    if damage == "missing-key":
        (slot / "capability").rename(slot / "retained-capability")
    elif damage == "missing-journal":
        (slot / "journal").rename(slot / "retained-journal")
    elif damage == "substituted-key":
        (slot / "capability").rename(slot / "retained-capability")
        (slot / "capability").write_bytes(b"x" * 32)
        (slot / "capability").chmod(0o600)
    elif damage == "substituted-authority":
        slot.rename(slot.with_name("retained-authority"))
        slot.mkdir(mode=0o700)
    elif damage == "corrupt-journal":
        (slot / "journal").write_bytes(b"not a sealed journal")
    elif damage == "substituted-stage":
        stage.rename(stage.with_name("retained-stage"))
        stage.mkdir(mode=0o700)
        (stage / "foreign").write_bytes(b"foreign")
    else:
        (stage / "1").write_bytes(b"foreign staged content")
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending" in hooks.status(repo)
    with pytest.raises(RuntimeError, match="incomplete"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))


@pytest.mark.parametrize("retained_user_hook", [False, True])
def test_interrupted_uninstall_resumes_deletion_or_user_remainder(repo, monkeypatch, retained_user_hook):
    if not retained_user_hook:
        for name in publication.NAMES:
            (_hooks(repo) / name).unlink()
    original = _live(repo)
    hooks.install(repo)
    _interrupt(repo, monkeypatch, "uninstall")
    assert "pending uninstall" in hooks.status(repo)
    hooks.uninstall(repo)
    expected = {name: None if data is None else data.rstrip() + b"\n\n" for name, data in original.items()}
    assert _live(repo) == expected
    assert "pending" not in hooks.status(repo)


@pytest.mark.parametrize("point", ["before-first", "after-first", "after-last", "after-terminal"])
def test_abrupt_process_death_has_sealed_retryable_state(repo, point):
    code = """
import os, signal, sys
from pathlib import Path
from graphify import hooks, hook_installation as p
real = p._rename
count = 0
def killed(*args, **kwargs):
    global count
    count += 1
    if sys.argv[2] == 'before-first' and count == 1:
        os.kill(os.getpid(), signal.SIGKILL)
    result = real(*args, **kwargs)
    if (sys.argv[2] == 'after-first' and count == 1) or (sys.argv[2] == 'after-last' and count == 3):
        os.kill(os.getpid(), signal.SIGKILL)
    return result
p._rename = killed
save = p._Store.save
def saved(self):
    save(self)
    if sys.argv[2] == 'after-terminal' and self.body['history'] and self.body['active'] is None:
        os.kill(os.getpid(), signal.SIGKILL)
p._Store.save = saved
hooks.install(Path(sys.argv[1]))
"""
    killed = subprocess.run([sys.executable, "-B", "-c", code, str(repo), point], capture_output=True)
    assert killed.returncode == -9, killed.stderr.decode()
    assert "pending install" in hooks.status(repo)
    hooks.install(repo)
    assert "pending" not in hooks.status(repo)


def test_fsync_failure_after_exchange_retains_retryable_objects(repo, monkeypatch):
    real_rename, real_fsync = publication._rename, os.fsync
    exchanged = False

    def exchange(*args, **kwargs):
        nonlocal exchanged
        result = real_rename(*args, **kwargs)
        exchanged = True
        return result

    def fail_flush(fd):
        if exchanged:
            raise OSError("injected persistence uncertainty")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename", exchange)
        patch.setattr(publication.os, "fsync", fail_flush)
        with pytest.raises(RuntimeError, match="persistence uncertainty"):
            hooks.install(repo)
    assert "pending install" in hooks.status(repo)
    hooks.install(repo)
    assert "pending" not in hooks.status(repo)


def test_concurrent_installer_is_refused_by_physical_directory_lock(repo):
    code = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
fcntl.flock(fd, fcntl.LOCK_EX)
print('locked', flush=True)
sys.stdin.read()
"""
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", code, str(_hooks(repo))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        before = _snapshot(repo)
        with pytest.raises(RuntimeError, match="incomplete"):
            hooks.install(repo)
        assert _snapshot(repo) == before
    finally:
        child.communicate("", timeout=10)
    assert child.returncode == 0
    hooks.install(repo)


@pytest.mark.parametrize("unsafe", ["symlink", "public-mode", "foreign-stage"])
def test_unsafe_authority_or_unrecognized_staging_has_no_live_effect(repo, monkeypatch, tmp_path, unsafe):
    home = Path.home()
    if unsafe == "foreign-stage":
        (_hooks(repo) / (publication._PREFIX + "foreign")).mkdir()
    else:
        authority = home / "unsafe-authority"
        authority.mkdir(mode=0o755 if unsafe == "public-mode" else 0o700)
        target = authority
        if unsafe == "symlink":
            target = home / "authority-alias"
            target.symlink_to(authority, target_is_directory=True)
        monkeypatch.setattr(publication, "_state_root", lambda: target)
    before = _live(repo)
    with pytest.raises(RuntimeError, match="incomplete"):
        hooks.install(repo)
    assert _live(repo) == before
    assert not (repo / ".gitattributes").exists()


def test_driver_failure_is_nonzero_with_complete_hooks(repo):
    code = """
from graphify import hooks
from graphify.__main__ import main
hooks._register_merge_driver = lambda root: 'not registered (injected config failure)'
main()
"""
    result = subprocess.run([sys.executable, "-B", "-c", code, "hook", "install"], cwd=repo,
                            env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "injected config failure" in result.stdout + result.stderr
    assert all(b"graphify" in data for data in _live(repo).values())
    assert "pending" not in hooks.status(repo)


def test_legacy_dispatch_does_not_enter_posix_authority(repo, monkeypatch):
    monkeypatch.setattr(hooks, "_atomic_hooks_supported", lambda: False)
    monkeypatch.setattr(publication, "run", lambda *a, **k: pytest.fail("legacy dispatcher entered POSIX recovery"))
    before = _live(repo)
    hooks.install(repo)
    assert "installed" in hooks.status(repo)
    hooks.uninstall(repo)
    assert _live(repo) == {name: data.rstrip() + b"\n\n" for name, data in before.items()}
    assert not publication._state_root().exists()


def test_stage_flush_failure_precedes_live_publication(repo, monkeypatch):
    before = _snapshot(_hooks(repo))
    real = os.fsync

    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and 'pre-0' in os.listdir(fd):
            raise OSError('injected staging directory flush failure')
        return real(fd)

    monkeypatch.setattr(publication.os, 'fsync', fail)
    with pytest.raises(RuntimeError, match='staging directory flush failure'):
        hooks.install(repo)
    assert {n: _snapshot(_hooks(repo))[n] for n in before} == before
    assert 'pending' in hooks.status(repo)


def test_foreign_retirement_retains_displaced_file_and_preimage(repo, monkeypatch):
    for name in publication.NAMES:
        (_hooks(repo) / name).unlink()
    hooks.install(repo)
    old = (_hooks(repo) / 'post-commit').read_bytes()
    real = publication._rename

    def replace(source_fd, source, target_fd, target, **kwargs):
        if source == 'post-commit':
            foreign = _hooks(repo) / 'foreign'
            foreign.write_bytes(b'foreign retirement content\n')
            os.replace(foreign, _hooks(repo) / source)
        return real(source_fd, source, target_fd, target, **kwargs)

    monkeypatch.setattr(publication, '_rename', replace)
    with pytest.raises(RuntimeError, match='displaced object and preimage retained'):
        hooks.uninstall(repo)
    slot, = publication._state_root().iterdir()
    batch = json.loads((slot / 'journal').read_bytes())['body']['active']
    stage = _hooks(repo) / batch['stage']
    assert (stage / '0').read_bytes() == b'foreign retirement content\n'
    assert (stage / 'pre-0').read_bytes() == old
    assert not (_hooks(repo) / 'post-commit').exists()
    assert 'pending uninstall' in hooks.status(repo)


def test_hooks_directory_substitution_preserves_both_directories(repo, monkeypatch):
    real = publication._prepare
    retained = repo / '.git/retained-hooks'
    original = _live(repo)

    def substitute(*args, **kwargs):
        result = real(*args, **kwargs)
        _hooks(repo).rename(retained)
        _hooks(repo).mkdir()
        (_hooks(repo) / 'post-commit').write_bytes(b'foreign directory hook\n')
        return result

    monkeypatch.setattr(publication, '_prepare', substitute)
    with pytest.raises(RuntimeError, match='directory changed'):
        hooks.install(repo)
    assert (_hooks(repo) / 'post-commit').read_bytes() == b'foreign directory hook\n'
    assert {name: (retained / name).read_bytes() for name in publication.NAMES} == original
    assert not (repo / '.gitattributes').exists()


def test_foreign_live_edit_preflight_preserves_batch_and_registration(repo, monkeypatch):
    _interrupt(repo, monkeypatch)
    (_hooks(repo) / 'post-checkout').write_bytes(b'#!/bin/sh\necho foreign\n')
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))



def test_terminal_sync_failure_preserves_pending_binding_and_exact_retry(repo, monkeypatch):
    real = os.fsync

    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and "completion" in os.listdir(fd):
            journal = publication._read(fd, "journal")[1]
            if json.loads(journal)["body"]["active"] is None:
                raise OSError("terminal directory sync failed")
        return real(fd)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "fsync", fail)
        with pytest.raises(RuntimeError, match="terminal directory sync failed"):
            hooks.install(repo)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending install" in hooks.status(repo)
    with pytest.raises(RuntimeError, match="different request"):
        hooks.uninstall(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    hooks.install(repo)
    assert "pending" not in hooks.status(repo)
    slot, = publication._state_root().iterdir()
    assert len(json.loads((slot / "journal").read_bytes())["body"]["history"]) == 1


def test_noop_directory_substitution_refuses_before_registration(repo, monkeypatch):
    hooks.install(repo)
    real = publication._prepare
    retained = repo / ".git/retained-noop-hooks"
    before = _live(repo)

    def substitute(*args, **kwargs):
        assert real(*args, **kwargs) is None
        _hooks(repo).rename(retained)
        _hooks(repo).mkdir()
        (_hooks(repo) / "post-commit").write_bytes(b"foreign noop directory\n")
        return None

    monkeypatch.setattr(publication, "_prepare", substitute)
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("registration must not run"))
    with pytest.raises(RuntimeError, match="directory changed"):
        hooks.install(repo)
    assert (_hooks(repo) / "post-commit").read_bytes() == b"foreign noop directory\n"
    assert {name: (retained / name).read_bytes() for name in publication.NAMES} == before


def test_final_stage_substitution_retains_pending_batch(repo, monkeypatch):
    real = publication._rename
    retained = _hooks(repo) / "retained-final-stage"

    def substitute(source_fd, source, target_fd, target, **kwargs):
        result = real(source_fd, source, target_fd, target, **kwargs)
        if target == "post-merge":
            stage, = _hooks(repo).glob(publication._PREFIX + "*")
            stage.rename(retained)
            stage.mkdir(mode=0o700)
            (stage / "foreign").write_bytes(b"foreign final stage\n")
        return result

    monkeypatch.setattr(publication, "_rename", substitute)
    with pytest.raises(RuntimeError, match="staging path changed before completion"):
        hooks.install(repo)
    assert "pending install" in hooks.status(repo)
    assert (retained / "pre-2").read_bytes() == (retained / "2").read_bytes()
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    assert (stage / "foreign").read_bytes() == b"foreign final stage\n"
    assert not (repo / ".gitattributes").exists()


def test_completion_retirement_failure_before_move_remains_retryable(repo, monkeypatch):
    real = publication._rename

    def fail(source_fd, source, target_fd, target, **kwargs):
        if source == "completion":
            raise OSError("completion retirement refused")
        return real(source_fd, source, target_fd, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename", fail)
        with pytest.raises(RuntimeError, match="completion retirement refused"):
            hooks.install(repo)
    assert "pending install" in hooks.status(repo)
    hooks.install(repo)
    assert "pending" not in hooks.status(repo)
    slot, = publication._state_root().iterdir()
    body = json.loads((slot / "journal").read_bytes())["body"]
    assert len(body["history"]) == 1 and body["active"] is None


def test_optional_retirement_postmove_errors_keep_durable_completion(repo, monkeypatch, capsys):
    real_rename, real_fsync = publication._rename, os.fsync
    retired = False

    def rename(source_fd, source, target_fd, target, **kwargs):
        nonlocal retired
        result = real_rename(source_fd, source, target_fd, target, **kwargs)
        retired = retired or source == "completion"
        if source == "completion":
            raise OSError("retirement moved before outcome error")
        return result

    def flush(fd):
        if retired:
            raise OSError("optional retirement sync failed")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename", rename)
        patch.setattr(publication.os, "fsync", flush)
        assert "registered" in hooks.install(repo)
    assert "retirement persistence is uncertain" in capsys.readouterr().err
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending" not in hooks.status(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))


def test_reappearing_completion_guard_refuses_changed_registration_context(repo):
    hooks.install(repo)
    slot, = publication._state_root().iterdir()
    completed = json.loads((slot / "journal").read_bytes())["body"]["history"][0]
    (slot / completed["retired"]).rename(slot / "completion")
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending install" in hooks.status(repo)
    with pytest.raises(RuntimeError, match="different request"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))


def test_missing_historical_stage_reports_unverified_without_mutation(repo, monkeypatch):
    hooks.install(repo)
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    retained = repo / "retained-historical-stage"
    stage.rename(retained)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("registration must not run"))
    status = hooks.status(repo)
    assert "pending/unverified" in status
    assert "Retained staging directory missing" in status
    with pytest.raises(RuntimeError, match="Retained staging directory missing"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    assert retained.is_dir() and not stage.exists()
