"""Public installer interruption, authority and preservation regressions."""
import errno
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
    parent = os.environ.get("GRAPHIFY_TEST_AUTHORITY_PARENT") or Path.home()
    with tempfile.TemporaryDirectory(prefix=".graphify-hook-proof-", dir=parent) as home:
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

    def fail_write(fd, name, data, mode, metadata=None):
        if name == "1":
            raise OSError("stage write failed")
        return write(fd, name, data, mode, metadata)

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


def _request_window_state(*roots):
    return [(_snapshot(root), {str(p.relative_to(root)): (
        p.lstat().st_mode, p.lstat().st_dev, p.lstat().st_ino,
        p.lstat().st_uid, p.lstat().st_gid)
        for p in (root, *root.rglob("*"))}) for root in roots]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("change", ["config", "attributes", "root", "gitdir"])
@pytest.mark.parametrize("timing", ["before-capture", "after-capture"])
def test_pending_request_window_refuses_and_preserves_external_edit(repo, monkeypatch, operation, change, timing):
    shared = repo.parent / "shared-hooks"
    _hooks(repo).rename(shared)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(shared)], check=True)
    attrs = repo / ".gitattributes"
    attrs.write_bytes(b"*.dat binary\n")
    if operation == "uninstall":
        hooks.install(repo)
    _interrupt(repo, monkeypatch, operation)
    partial = _snapshot(shared)
    hook_identity = publication._identity(shared.stat())
    request = hooks._hook_request
    original_request = request(repo)
    observed = {}

    def edit():
        if change == "config":
            subprocess.run(["git", "-C", str(repo), "config", "merge.graphify.driver", "foreign-editor-driver --preserve-me"], check=True)
        elif change == "attributes":
            attrs.write_bytes(attrs.read_bytes() + b"*.foreign text\n")
        else:
            config, attributes = (repo / ".git/config").read_bytes(), attrs.read_bytes()
            selected = repo if change == "root" else repo / ".git"
            identity = publication._identity(selected.stat())
            selected.rename(repo.parent / "original-retained")
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            (repo / ".git/config").write_bytes(config)
            attrs.write_bytes(attributes)
            assert publication._identity(selected.stat()) != identity
        assert hooks._hooks_dir(repo) == shared
        assert publication._identity(shared.stat()) == hook_identity
        assert _snapshot(shared) == partial
        assert request(repo) != original_request
        observed["state"] = _request_window_state(repo.parent, Path.home())

    calls = 0

    def capture_then_edit(root):
        nonlocal calls
        calls += 1
        captured = request(root)
        if calls == 1 and timing == "after-capture":
            assert captured == original_request
            edit()
        return captured

    if timing == "before-capture":
        edit()
    with monkeypatch.context() as patch:
        patch.setattr(hooks, "_hook_request", capture_then_edit)
        with pytest.raises(RuntimeError, match="different request|request context changed"):
            getattr(hooks, operation)(repo)
    assert calls >= 1
    assert observed["state"] == _request_window_state(repo.parent, Path.home())
    assert f"pending {operation}" in hooks.status(repo)
    assert observed["state"] == _request_window_state(repo.parent, Path.home())


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_request_revalidation_allows_unchanged_exact_retry(repo, monkeypatch, operation):
    original = _live(repo)
    (repo / ".gitattributes").write_bytes(b"*.dat binary\n")
    subprocess.run(["git", "-C", str(repo), "config", "merge.other.driver", "keep this driver"], check=True)
    if operation == "uninstall":
        hooks.install(repo)
    _interrupt(repo, monkeypatch, operation)
    before = _request_window_state(repo.parent, Path.home())
    assert f"pending {operation}" in hooks.status(repo)
    assert before == _request_window_state(repo.parent, Path.home())
    assert getattr(hooks, operation)(repo)
    assert "pending" not in hooks.status(repo)
    assert b"*.dat binary\n" in (repo / ".gitattributes").read_bytes()
    assert subprocess.check_output(["git", "-C", str(repo), "config", "merge.other.driver"], text=True).strip() == "keep this driver"
    for (name, data), marker in zip(_live(repo).items(), (hooks._HOOK_MARKER, hooks._CHECKOUT_MARKER, hooks._POST_MERGE_MARKER), strict=True):
        assert original[name].rstrip() in data
        assert stat.S_IMODE((_hooks(repo) / name).stat().st_mode) == 0o751
        assert (marker.encode() in data) == (operation == "install")
    before = _request_window_state(_hooks(repo), Path.home())
    assert getattr(hooks, operation)(repo)
    assert before == _request_window_state(_hooks(repo), Path.home())


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_request_change_after_first_publication_stops_remaining_hooks(repo, monkeypatch, operation):
    if operation == "uninstall":
        hooks.install(repo)
    before = _live(repo)
    rename = publication._rename
    observed = {}

    def publish_then_edit(*args, **kwargs):
        result = rename(*args, **kwargs)
        if args[3] == "post-commit":
            subprocess.run(["git", "-C", str(repo), "config", "merge.graphify.driver", "foreign-editor-driver --preserve-me"], check=True)
            observed["state"] = _request_window_state(repo.parent, Path.home())
            observed["published"] = (_hooks(repo) / "post-commit").read_bytes()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename", publish_then_edit)
        with pytest.raises(RuntimeError, match="request context changed"):
            getattr(hooks, operation)(repo)
    assert observed["state"] == _request_window_state(repo.parent, Path.home())
    assert (_hooks(repo) / "post-commit").read_bytes() == observed["published"] != before["post-commit"]
    assert (hooks._HOOK_MARKER.encode() in observed["published"]) == (operation == "install")
    if operation == "install":
        assert hooks._HOOK_MARKER_END.encode() in observed["published"]
    assert stat.S_IMODE((_hooks(repo) / "post-commit").stat().st_mode) == 0o751
    assert all(_live(repo)[name] == before[name] for name in publication.NAMES[1:])
    assert f"pending {operation}" in hooks.status(repo)
    assert observed["state"] == _request_window_state(repo.parent, Path.home())


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_initial_locked_request_change_precedes_authority_creation(repo, monkeypatch, operation):
    request = hooks._hook_request
    observed = {}
    calls = 0

    def capture_then_edit(root):
        nonlocal calls
        calls += 1
        captured = request(root)
        if calls == 1:
            (repo / ".gitattributes").write_bytes(b"*.foreign text\n")
            observed["state"] = _request_window_state(repo.parent, Path.home())
        return captured

    monkeypatch.setattr(hooks, "_hook_request", capture_then_edit)
    with pytest.raises(RuntimeError, match="request context changed"):
        getattr(hooks, operation)(repo)
    assert calls == 2
    assert not publication._state_root().exists()
    assert observed["state"] == _request_window_state(repo.parent, Path.home())


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("failure", ["io", "git"])
def test_unreadable_locked_request_refuses_without_authority_mutation(repo, monkeypatch, operation, failure):
    request = hooks._hook_request
    calls = 0
    before = _request_window_state(repo.parent, Path.home())

    def unreadable(root):
        nonlocal calls
        calls += 1
        if calls > 1:
            if failure == "io":
                raise OSError("injected request read failure")
            raise subprocess.CalledProcessError(128, ["git", "config"])
        return request(root)

    monkeypatch.setattr(hooks, "_hook_request", unreadable)
    with pytest.raises(RuntimeError, match="Cannot revalidate hook request"):
        getattr(hooks, operation)(repo)
    assert calls == 2
    assert not publication._state_root().exists()
    assert before == _request_window_state(repo.parent, Path.home())


@pytest.mark.parametrize("point", ["before-prepare", "before-stage", "noop", "completion"])
def test_request_change_at_preparation_noop_and_completion_checks(repo, monkeypatch, point):
    if point == "noop":
        hooks.install(repo)
    before = _live(repo)
    read, prepare_hook = publication._read, hooks._prepare_hook_install
    prepare, rename = publication._prepare, publication._rename
    observed, prepared = {}, []
    hook_identity = publication._identity(_hooks(repo).stat())

    def edit():
        assert not observed
        subprocess.run(["git", "-C", str(repo), "config", "merge.graphify.driver", "foreign-editor-driver --preserve-me"], check=True)
        observed["state"] = _request_window_state(repo.parent, Path.home())

    def read_then_edit(fd, name, private=False):
        result = read(fd, name, private=private)
        if not observed and name == "post-merge" and publication._identity(os.fstat(fd)) == hook_identity:
            edit()
        return result

    def prepare_then_edit(*args, **kwargs):
        result = prepare_hook(*args, **kwargs)
        prepared.append(result)
        if len(prepared) == 3:
            edit()
        return result

    def noop_then_edit(*args, **kwargs):
        result = prepare(*args, **kwargs)
        assert result is None
        edit()
        return result

    def publish_then_edit(*args, **kwargs):
        result = rename(*args, **kwargs)
        if args[3] == "post-merge":
            edit()
        return result

    with monkeypatch.context() as patch:
        if point == "before-prepare":
            patch.setattr(publication, "_read", read_then_edit)
        elif point == "before-stage":
            patch.setattr(hooks, "_prepare_hook_install", prepare_then_edit)
        elif point == "noop":
            patch.setattr(publication, "_prepare", noop_then_edit)
        else:
            patch.setattr(publication, "_rename", publish_then_edit)
        with pytest.raises(RuntimeError, match="request context changed"):
            hooks.install(repo)
    assert observed["state"] == _request_window_state(repo.parent, Path.home())
    if point != "completion":
        assert _live(repo) == before
    else:
        for name, marker in zip(publication.NAMES, (hooks._HOOK_MARKER_END, hooks._CHECKOUT_MARKER_END, hooks._POST_MERGE_MARKER_END), strict=True):
            assert marker.encode() in _live(repo)[name]
            assert stat.S_IMODE((_hooks(repo) / name).stat().st_mode) == 0o751
        assert "pending install" in hooks.status(repo)
        assert observed["state"] == _request_window_state(repo.parent, Path.home())


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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux XDG authority selection")
def test_empty_xdg_state_home_uses_default_for_public_operations(repo, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert publication._state_root() == Path.home() / ".local/state/graphify/hook-installations"
    hooks.install(repo)
    assert "pending" not in hooks.status(repo)
    hooks.uninstall(repo)
    assert all(b"graphify-hook-start" not in data for data in _live(repo).values())


def test_status_without_attempt_distinguishes_unsafe_authority_and_preserves_state(repo):
    Path.home().chmod(0o770)
    authority = publication._state_root()
    assert not authority.exists()
    before = (_snapshot(repo), _snapshot(Path.home()))
    result = hooks.status(repo)
    assert "unverified authority" in result and "pending" not in result
    assert "Unsafe installer authority" in result
    assert all(f"{name}: not installed" in result for name in publication.NAMES)
    assert "merge driver:" in result
    assert before == (_snapshot(repo), _snapshot(Path.home()))
    assert not authority.exists()


@pytest.mark.parametrize("hooks_path", [".githooks", ".husky/_"])
def test_worktree_stage_content_is_excluded_from_ordinary_git_add(repo, hooks_path):
    resolved = repo / hooks_path
    resolved.mkdir(parents=True)
    selected = resolved.parent if resolved.name == "_" else resolved
    target = selected / "post-commit"
    target.write_bytes(b"#!/bin/sh\n# retained user content\n")
    target.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", hooks_path], check=True)
    hooks.install(repo)
    stage, = selected.glob(publication._PREFIX + "*")
    assert (stage / "pre-0").read_bytes() == b"#!/bin/sh\n# retained user content\n"
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"]).split(b"\0")
    assert os.fsencode(target.relative_to(repo)) in tracked
    assert not any(publication._PREFIX.encode() in path for path in tracked)
    assert stat.S_IMODE((stage / ".gitignore").stat().st_mode) == 0o600


def test_existing_unrecognized_stage_is_not_adopted_or_given_exclusion(repo):
    stage = _hooks(repo) / (publication._PREFIX + "foreign")
    stage.mkdir()
    (stage / "pre-0").write_bytes(b"foreign preserved content\n")
    before = _snapshot(repo)
    with pytest.raises(RuntimeError, match="Unrecognized hook staging"):
        hooks.install(repo)
    assert before == _snapshot(repo)
    assert not (stage / ".gitignore").exists()


def test_stage_exclusion_write_failure_precedes_payload_and_live_publication(repo, monkeypatch):
    real_write, real_fsync = publication._write, os.fsync
    before = _live(repo)
    payloads = []

    def record_write(fd, name, data, mode, metadata=None):
        if name.isdigit() or name.startswith("pre-"):
            payloads.append(name)
        return real_write(fd, name, data, mode, metadata)

    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and os.listdir(fd) == [".gitignore"]:
            raise OSError("injected exclusion persistence failure")
        return real_fsync(fd)

    monkeypatch.setattr(publication, "_write", record_write)
    monkeypatch.setattr(publication.os, "fsync", fail)
    with pytest.raises(RuntimeError, match="exclusion persistence failure"):
        hooks.install(repo)
    assert not payloads and _live(repo) == before
    assert not (repo / ".gitattributes").exists()
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    assert list(stage.iterdir()) == [stage / ".gitignore"]
    assert (stage / ".gitignore").read_bytes() == b"*\n"


@pytest.mark.parametrize("mask", [0o177, 0o777])
@pytest.mark.parametrize("boundary", ["authority", "slot", "stage"])
def test_private_directory_birth_ignores_parent_umask(repo, mask, boundary):
    # Keep the separate merge-driver file-creation behavior outside this probe.
    (repo / ".gitattributes").write_bytes(b"# existing repository attributes\n")
    if boundary in ("slot", "stage"):
        with publication._authority(publication._state_root(), True):
            pass
    if boundary == "stage":
        with publication._authority(publication._state_root(), True) as (fd, _):
            identity = publication._identity(_hooks(repo).stat())
            name = publication._slot(identity)
            os.mkdir(name, 0o700, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            try:
                publication._Store(child, identity, True)
            finally:
                os.close(child)
    old_mask = os.umask(mask)
    try:
        hooks.install(repo)
        assert os.umask(mask) == mask
        hooks.uninstall(repo)
        assert os.umask(mask) == mask
    finally:
        os.umask(old_mask)
    assert "pending" not in hooks.status(repo)
    for parent in (publication._state_root(), _hooks(repo)):
        for child in parent.iterdir():
            if child.is_dir():
                assert stat.S_IMODE(child.stat().st_mode) == 0o700


def _forbid_directory_permission_changes(monkeypatch):
    def guarded(function, descriptor):
        def invoke(target, *args, **kwargs):
            info = (os.fstat(target) if descriptor or isinstance(target, int)
                    else os.stat(target, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False))
            assert not stat.S_ISDIR(info.st_mode), "installer changed directory permissions"
            return function(target, *args, **kwargs)
        return invoke

    for name in ("chmod", "fchmod", "chown", "fchown", "lchown"):
        monkeypatch.setattr(os, name, guarded(getattr(os, name), name.startswith("f")))


def _private_boundary(root, boundary):
    if boundary == "authority":
        return publication._state_root()
    if boundary == "slot":
        slot, = (p for p in publication._state_root().iterdir() if p.is_dir())
        return slot
    stage, = _hooks(root).glob(publication._PREFIX + "*")
    return stage


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="setgid inheritance requires native Linux")
@pytest.mark.parametrize("parent", ["hooks", "xdg"])
@pytest.mark.parametrize("entry_point", ["api", "cli"])
def test_native_setgid_public_lifecycle(repo, monkeypatch, parent, entry_point):
    directory = _hooks(repo) if parent == "hooks" else Path(os.environ["XDG_STATE_HOME"])
    directory.mkdir(exist_ok=True)
    directory.chmod(0o2755)
    parent_info = directory.stat()
    original = _live(repo)
    attrs = repo / ".gitattributes"
    attrs.write_bytes(b"*.dat binary\n")
    unrelated = _hooks(repo) / "pre-push"
    unrelated.write_bytes(b"#!/bin/sh\n# unrelated user hook\n")
    subprocess.run(["git", "-C", str(repo), "config", "merge.other.driver", "keep this driver"], check=True)
    protected = _snapshot(_hooks(repo))["pre-push"]

    def invoke(operation):
        if entry_point == "api":
            return getattr(hooks, operation)(repo)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "graphify", "hook", operation], cwd=repo,
            env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    with monkeypatch.context() as patch:
        _forbid_directory_permission_changes(patch)
        assert "appended to existing" in invoke("install")
        result = invoke("status")
        assert "pending" not in result and "unverified" not in result
        assert all(f"{name}: installed" in result for name in publication.NAMES)
        for name in publication.NAMES:
            assert original[name].rstrip() in (_hooks(repo) / name).read_bytes()
            assert stat.S_IMODE((_hooks(repo) / name).stat().st_mode) == 0o751
        before = (_snapshot(_hooks(repo)), _snapshot(publication._state_root()))
        assert "already installed" in invoke("install")
        assert before == (_snapshot(_hooks(repo)), _snapshot(publication._state_root()))
        for operation in ("uninstall", "install"):
            # The CLI retries the same authenticated request and interpreter.
            _interrupt(repo, patch, operation)
            before = (_snapshot(repo), _snapshot(publication._state_root()))
            assert f"pending {operation}" in invoke("status")
            assert before == (_snapshot(repo), _snapshot(publication._state_root()))
            assert invoke(operation)
            assert "pending" not in invoke("status")
        assert "removed" in invoke("uninstall")
        result = invoke("status")
        assert "pending" not in result and "unverified" not in result
        assert all(f"{name}: not installed" in result for name in publication.NAMES)

    assert _live(repo) == {name: data.rstrip() + b"\n\n" for name, data in original.items()}
    assert attrs.read_bytes() == b"*.dat binary\n"
    assert protected == _snapshot(_hooks(repo))["pre-push"]
    assert subprocess.check_output(["git", "-C", str(repo), "config", "merge.other.driver"], text=True).strip() == "keep this driver"
    assert (directory.stat().st_ino, directory.stat().st_mode) == (parent_info.st_ino, parent_info.st_mode)
    authority = publication._state_root()
    assert stat.S_IMODE(authority.stat().st_mode) == (0o2700 if parent == "xdg" else 0o700)
    slot, = (p for p in authority.iterdir() if p.is_dir())
    assert stat.S_IMODE(slot.stat().st_mode) == (0o2700 if parent == "xdg" else 0o700)
    stages = list(_hooks(repo).glob(publication._PREFIX + "*"))
    assert len(stages) == 4
    for stage in stages:
        assert stat.S_IMODE(stage.stat().st_mode) == (0o2700 if parent == "hooks" else 0o700)
        assert (stage / ".gitignore").read_bytes() == b"*\n"
        assert stat.S_IMODE((stage / ".gitignore").stat().st_mode) == 0o600
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o751 for p in stage.iterdir() if p.name != ".gitignore")
        assert all(p.stat().st_uid == os.getuid() for p in stage.iterdir())
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in slot.iterdir())


@pytest.mark.parametrize("boundary", ["authority", "slot", "stage"])
@pytest.mark.parametrize("mode", [0o700, 0o2700])
def test_existing_private_directory_exact_native_mode_set(repo, monkeypatch, boundary, mode):
    if boundary == "authority":
        directory = publication._state_root()
        directory.mkdir(parents=True, mode=0o700)
    else:
        _interrupt(repo, monkeypatch)
        directory = _private_boundary(repo, boundary)
    directory.chmod(mode)
    info = directory.stat()
    assert stat.S_IMODE(info.st_mode) == mode
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with monkeypatch.context() as patch:
        _forbid_directory_permission_changes(patch)
        if mode == 0o2700 and sys.platform == "darwin":
            with pytest.raises(RuntimeError, match="Unsafe installer authority"):
                hooks.install(repo)
            assert before == (_snapshot(repo), _snapshot(publication._state_root()))
        else:
            assert "appended to existing" in hooks.install(repo)
            assert "pending" not in hooks.status(repo)
            before = (_snapshot(_hooks(repo)), _snapshot(publication._state_root()))
            assert "already installed" in hooks.install(repo)
            assert before == (_snapshot(_hooks(repo)), _snapshot(publication._state_root()))
            hooks.uninstall(repo)
    assert (directory.stat().st_ino, directory.stat().st_mode) == (info.st_ino, info.st_mode)


@pytest.mark.parametrize("boundary", ["authority", "slot", "stage"])
@pytest.mark.parametrize("mode", [0o1700, 0o4700, 0o6700, 0o2770, 0o2701])
def test_unsafe_private_special_modes_refuse_without_normalization(repo, monkeypatch, boundary, mode):
    _interrupt(repo, monkeypatch)
    directory = _private_boundary(repo, boundary)
    directory.chmod(mode)
    info = directory.stat()
    assert stat.S_IMODE(info.st_mode) == mode
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with monkeypatch.context() as patch:
        _forbid_directory_permission_changes(patch)
        with pytest.raises(RuntimeError, match="Unsafe installer authority"):
            hooks.install(repo)
        assert before == (_snapshot(repo), _snapshot(publication._state_root()))
        hooks.status(repo)
        assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    assert (directory.stat().st_ino, directory.stat().st_mode) == (info.st_ino, info.st_mode)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="02700 admission requires native Linux")
@pytest.mark.parametrize("stage_kind", ["unknown", "substituted"])
def test_safe_setgid_stage_mode_does_not_grant_ownership(repo, monkeypatch, stage_kind):
    if stage_kind == "substituted":
        _interrupt(repo, monkeypatch)
        stage = _private_boundary(repo, "stage")
        stage.rename(stage.with_name("retained-stage"))
    else:
        stage = _hooks(repo) / (publication._PREFIX + "foreign")
    stage.mkdir(mode=0o700)
    stage.chmod(0o2700)
    (stage / "pre-0").write_bytes(b"foreign preserved content\n")
    info = stage.stat()
    before = _snapshot(repo)
    with monkeypatch.context() as patch:
        _forbid_directory_permission_changes(patch)
        with pytest.raises(RuntimeError, match="Unrecognized hook staging|staging directory changed"):
            hooks.install(repo)
    assert before == _snapshot(repo)
    assert (stage.stat().st_ino, stage.stat().st_mode) == (info.st_ino, info.st_mode)
    assert not (stage / ".gitignore").exists()


def _set_extended_metadata(path, kind):
    if kind == "flags":
        if sys.platform != "darwin":
            pytest.skip("BSD flags require native macOS")
        os.chflags(path, stat.UF_HIDDEN | stat.UF_NODUMP)
    elif kind == "xattr":
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/xattr", "-w", "user.graphify-proof", "preserve-me", str(path)], check=True)
        else:
            os.setxattr(path, "user.graphify-proof", b"preserve-me")
    elif sys.platform == "darwin":
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        subprocess.run(["/bin/chmod", "+a", f"user:{user} deny execute", str(path)], check=True)
    else:
        import struct
        entries = [(1, 7, 0xffffffff), (2, 5, 65534), (4, 5, 0xffffffff),
                   (16, 5, 0xffffffff), (32, 1, 0xffffffff)]
        os.setxattr(path, "system.posix_acl_access", struct.pack("<I", 2)
                    + b"".join(struct.pack("<HHI", *entry) for entry in entries))


def _assert_extended_metadata(path, kind):
    if kind == "flags":
        assert path.stat().st_flags == stat.UF_HIDDEN | stat.UF_NODUMP
    elif kind == "xattr":
        value = (subprocess.check_output(["/usr/bin/xattr", "-p", "user.graphify-proof", str(path)]).rstrip(b"\n")
                 if sys.platform == "darwin" else os.getxattr(path, "user.graphify-proof"))
        assert value == b"preserve-me"
    elif sys.platform == "darwin":
        assert not os.access(path, os.X_OK)
        assert "deny execute" in subprocess.check_output(["/bin/ls", "-le", str(path)], text=True)
    else:
        import struct
        raw = os.getxattr(path, "system.posix_acl_access")
        assert (2, 5, 65534) in list(struct.iter_unpack("<HHI", raw[4:]))


@pytest.mark.parametrize("kind", ["xattr", "acl", "flags"])
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_public_replacement_preserves_extended_metadata(repo, kind, operation):
    if operation == "uninstall":
        hooks.install(repo)
    target = _hooks(repo) / "post-commit"
    _set_extended_metadata(target, kind)
    _assert_extended_metadata(target, kind)
    old_inode = target.stat().st_ino
    getattr(hooks, operation)(repo)
    assert target.stat().st_ino != old_inode
    _assert_extended_metadata(target, kind)
    assert b"printf 'user hook\\n'" in target.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o751


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
def test_bsd_flags_survive_mode_repair_and_noop(repo):
    hooks.install(repo)
    target = _hooks(repo) / "post-commit"
    _set_extended_metadata(target, "flags")
    target.chmod(0o640)
    content, inode = target.read_bytes(), target.stat().st_ino
    hooks.install(repo)
    assert target.read_bytes() == content and target.stat().st_ino != inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    _assert_extended_metadata(target, "flags")
    before = _snapshot(_hooks(repo))
    hooks.install(repo)
    assert _snapshot(_hooks(repo)) == before
    _assert_extended_metadata(target, "flags")


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_unsupported_bsd_flags_refuse_before_any_publication(repo, operation):
    if operation == "uninstall":
        hooks.install(repo)
    target = _hooks(repo) / "post-merge"
    os.chflags(target, stat.UF_IMMUTABLE)
    try:
        before = _snapshot(repo)
        with pytest.raises(RuntimeError, match="Unsupported BSD hook flags"):
            getattr(hooks, operation)(repo)
        after = _snapshot(repo)
        assert {name: after[name] for name in before} == before
        assert target.stat().st_flags == stat.UF_IMMUTABLE
    finally:
        os.chflags(target, 0)  # Release only this fixture's immutable flag.


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
def test_bsd_flag_copy_failure_precedes_live_publication(repo, monkeypatch):
    target = _hooks(repo) / "post-merge"
    _set_extended_metadata(target, "flags")
    before = _snapshot(repo)
    real = publication.ctypes.CDLL
    calls = []

    def denied(fd, flags):
        calls.append((fd, flags))
        publication.ctypes.set_errno(errno.EIO)
        return -1

    def library(*args, **kwargs):
        result = real(*args, **kwargs)
        result.fchflags = denied
        return result

    monkeypatch.setattr(publication.ctypes, "CDLL", library)
    with pytest.raises(RuntimeError, match="Cannot preserve BSD hook flags"):
        hooks.install(repo)
    after = _snapshot(repo)
    assert calls and {name: after[name] for name in before} == before
    _assert_extended_metadata(target, "flags")


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_unsupported_bsd_flags_keep_unchanged_noop_hooks(repo, operation):
    if operation == "install":
        hooks.install(repo)
    target = _hooks(repo) / "post-commit"
    os.chflags(target, stat.UF_IMMUTABLE)
    try:
        before = _snapshot(_hooks(repo))
        assert getattr(hooks, operation)(repo)
        assert _snapshot(_hooks(repo)) == before
        assert target.stat().st_flags == stat.UF_IMMUTABLE
    finally:
        os.chflags(target, 0)  # Release only this fixture's immutable flag.


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_changed_bsd_flags_refuse_pending_resume_and_exact_retry(repo, monkeypatch, operation):
    if operation == "uninstall":
        hooks.install(repo)
    _interrupt(repo, monkeypatch, operation)
    target = _hooks(repo) / "post-merge"
    os.chflags(target, stat.UF_NODUMP)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="Foreign or uncertain"):
        getattr(hooks, operation)(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    assert target.stat().st_flags == stat.UF_NODUMP
    os.chflags(target, 0)
    assert getattr(hooks, operation)(repo)
    assert "pending" not in hooks.status(repo)


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
def test_legacy_pending_batch_without_bsd_flag_binding_refuses(repo, monkeypatch):
    _interrupt(repo, monkeypatch)
    with publication._authority(publication._state_root(), False) as (fd, _):
        identity = publication._identity(_hooks(repo).stat())
        slot = os.open(publication._slot(identity), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            store = publication._Store(slot, identity, False)
            for entry in store.body["active"]["entries"]:
                for key in ("old", "new", "preimage"):
                    if entry[key] is not None:
                        entry[key]["metadata"].pop("flags", None)
            store.save()
        finally:
            os.close(slot)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="preimage changed|Foreign or uncertain"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
@pytest.mark.parametrize("name", ["2", "pre-2"])
def test_changed_staged_bsd_flags_refuse_pending_resume(repo, monkeypatch, name):
    _interrupt(repo, monkeypatch)
    target = next(_hooks(repo).glob(".graphify-hook-batch-*")) / name
    os.chflags(target, stat.UF_NODUMP)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="preimage changed|Foreign or uncertain"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    assert target.stat().st_flags == stat.UF_NODUMP
    os.chflags(target, 0)
    assert hooks.install(repo)


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD flags require native macOS")
def test_completed_history_without_bsd_flag_binding_remains_usable(repo, monkeypatch):
    original = publication._signature

    def legacy(snapshot):
        result = original(snapshot)
        if result is not None and "metadata" in result:
            result["metadata"] = {key: value for key, value in result["metadata"].items() if key != "flags"}
        return result

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_signature", legacy)
        hooks.install(repo)
    assert hooks.uninstall(repo)
    assert "pending" not in hooks.status(repo)


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_restrictive_umask_never_normalizes_preexisting_authority(repo, kind):
    authority = publication._state_root()
    authority.parent.mkdir(parents=True)
    foreign = Path.home() / "foreign"
    foreign.mkdir()
    (foreign / "marker").write_bytes(b"keep foreign metadata")
    if kind == "symlink":
        authority.symlink_to(foreign, target_is_directory=True)
    else:
        authority.mkdir()
        (authority / "marker").write_bytes(b"keep authority metadata")
        authority.chmod(0o500)
    before = (_snapshot(Path.home()), authority.lstat(), _live(repo))
    old_mask = os.umask(0o777)
    try:
        with pytest.raises(RuntimeError, match="incomplete"):
            hooks.install(repo)
        assert os.umask(0o777) == 0o777
    finally:
        os.umask(old_mask)
    assert _snapshot(Path.home()) == before[0]
    assert (authority.lstat().st_ino, authority.lstat().st_mode) == (before[1].st_ino, before[1].st_mode)
    assert _live(repo) == before[2] and not (repo / ".gitattributes").exists()


@pytest.mark.parametrize("outcome", ["failure", "created-then-killed"])
def test_private_directory_child_failure_does_not_publish(repo, monkeypatch, outcome):
    import errno
    real = subprocess.run
    before = _live(repo)
    affected = []

    def fail(args, **kwargs):
        if publication._MKDIR_CODE not in args:
            return real(args, **kwargs)
        assert kwargs["pass_fds"] and kwargs["umask"] == 0o077
        if outcome == "created-then-killed":
            result = real(args, **kwargs)
            assert result.returncode == 0
            affected.append(os.stat(args[-2], dir_fd=int(args[-1])).st_ino)
            return subprocess.CompletedProcess(args, -9, "", "")
        return subprocess.CompletedProcess(args, 1, str(errno.EACCES), "")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="incomplete"):
        hooks.install(repo)
    assert _live(repo) == before and not (repo / ".gitattributes").exists()
    if outcome == "created-then-killed":
        assert len(affected) == 1
        assert any(p.stat().st_ino == affected[0] for p in Path.home().rglob("*") if p.is_dir())


def test_mode_only_replacement_keeps_acl_and_legacy_chmod_rules(repo):
    hooks.install(repo)
    target = _hooks(repo) / "post-commit"
    _set_extended_metadata(target, "acl")
    target.chmod(0o640)
    before = target.read_bytes()
    hooks.install(repo)
    assert target.read_bytes() == before and stat.S_IMODE(target.stat().st_mode) == 0o751
    _assert_extended_metadata(target, "acl")


@pytest.mark.parametrize("failure", ["read", "copy"])
def test_metadata_failure_refuses_before_live_publication(repo, monkeypatch, failure):
    target = _hooks(repo) / "post-commit"
    _set_extended_metadata(target, "xattr")
    before = _live(repo)
    if failure == "read":
        def unreadable(_):
            raise OSError("injected metadata inspection failure")
        monkeypatch.setattr(publication, "_extended", unreadable)
    else:
        monkeypatch.setattr(publication, "_copy_metadata", lambda *args: None)
    with pytest.raises(RuntimeError, match="metadata"):
        hooks.install(repo)
    assert _live(repo) == before and not (repo / ".gitattributes").exists()
    _assert_extended_metadata(target, "xattr")


def test_changed_metadata_refuses_pending_resume_without_mutation(repo, monkeypatch):
    _interrupt(repo, monkeypatch)
    target = _hooks(repo) / "post-checkout"
    _set_extended_metadata(target, "xattr")
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="Foreign or uncertain"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    _assert_extended_metadata(target, "xattr")
    assert "pending install" in hooks.status(repo)


def test_legacy_active_batch_without_metadata_binding_refuses(repo, monkeypatch):
    _interrupt(repo, monkeypatch)
    with publication._authority(publication._state_root(), False) as (fd, _):
        identity = publication._identity(_hooks(repo).stat())
        slot = os.open(publication._slot(identity), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            store = publication._Store(slot, identity, False)
            batch = store.body["active"]
            del batch["metadata_version"]
            for entry in batch["entries"]:
                for key in ("old", "new", "preimage"):
                    if entry[key] is not None:
                        entry[key].pop("metadata")
            store.save()
        finally:
            os.close(slot)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("legacy batch cannot register"))
    with pytest.raises(RuntimeError, match="Legacy pending batch lacks hook metadata binding"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("directory_kind", ["ancestor", "slot", "stage"])
@pytest.mark.parametrize("point,kind", [("prepared", "mode"), ("prepared", "acl"), ("published", "mode")])
def test_changed_authority_ancestor_stops_further_publication(repo, monkeypatch, operation, directory_kind, point, kind):
    if operation == "uninstall":
        hooks.install(repo)
    home = Path.home()
    directory = home
    original_prepare, original_rename = publication._prepare, publication._rename
    config = (repo / ".git/config").read_bytes()
    attrs = (repo / ".gitattributes").read_bytes() if (repo / ".gitattributes").exists() else None
    stopped = {}

    def change_ancestor():
        if kind == "mode":
            directory.chmod(0o777)
        elif sys.platform == "darwin":
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
            subprocess.run(["/bin/chmod", "+a", f"user:{user} allow read", str(directory)], check=True)
        else:
            _set_extended_metadata(directory, "acl")
        stopped.update({name: value for name, value in _snapshot(_hooks(repo)).items() if "/" not in name})

    def prepare(*args, **kwargs):
        nonlocal directory
        batch = original_prepare(*args, **kwargs)
        if directory_kind == "slot":
            directory = publication._state_root() / publication._slot(publication._identity(_hooks(repo).stat()))
        elif directory_kind == "stage":
            directory = _hooks(repo) / batch["stage"]
        if point == "prepared":
            change_ancestor()
        return batch

    def rename(source_fd, source, target_fd, target, **kwargs):
        result = original_rename(source_fd, source, target_fd, target, **kwargs)
        if point == "published" and target == "post-commit":
            change_ancestor()
        return result

    monkeypatch.setattr(publication, "_prepare", prepare)
    monkeypatch.setattr(publication, "_rename", rename)
    try:
        with pytest.raises(RuntimeError, match="Unsafe installer authority"):
            getattr(hooks, operation)(repo)
        assert stopped
        assert stopped == {name: value for name, value in _snapshot(_hooks(repo)).items() if "/" not in name}
        assert (repo / ".git/config").read_bytes() == config
        assert ((repo / ".gitattributes").read_bytes() if (repo / ".gitattributes").exists() else None) == attrs
        expected_status = f"pending {operation}" if directory_kind == "stage" else "unverified"
        assert expected_status in hooks.status(repo)
    finally:
        if kind == "acl":
            if sys.platform == "darwin":
                subprocess.run(["/bin/chmod", "-N", str(directory)], check=True)
            else:
                os.removexattr(directory, "system.posix_acl_access")
        directory.chmod(0o700)
    assert f"pending {operation}" in hooks.status(repo)
    monkeypatch.setattr(publication, "_prepare", original_prepare)
    monkeypatch.setattr(publication, "_rename", original_rename)
    getattr(hooks, operation)(repo)
    assert "pending" not in hooks.status(repo)


@pytest.mark.parametrize("installed", [False, True])
def test_uninstall_preserves_inherited_global_driver_config(repo, monkeypatch, tmp_path, installed):
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text('[merge "graphify"]\n name = inherited driver\n driver = true\n recursive = binary\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    before_global = global_config.read_bytes()
    user_hooks = _live(repo)
    attrs = repo / ".gitattributes"
    attrs.write_text("*.dat binary\n")
    if installed:
        hooks.install(repo)
        user_hooks = {name: data + b"\n" for name, data in user_hooks.items()}
    for _ in range(2):
        result = hooks.uninstall(repo)
        assert "merge driver:" in result
        assert global_config.read_bytes() == before_global
        assert _live(repo) == user_hooks
        assert attrs.read_text() == "*.dat binary\n"
        for key in ("name", "driver", "recursive"):
            local = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get-all", f"merge.graphify.{key}"], capture_output=True)
            assert local.returncode == 1


@pytest.mark.parametrize("key", ["name", "driver", "recursive"])
@pytest.mark.parametrize("entry_point", ["api", "cli"])
def test_uninstall_removes_duplicate_local_driver_values(repo, monkeypatch, tmp_path, key, entry_point):
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text('[merge "graphify"]\n name = inherited driver\n driver = true\n recursive = binary\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    before_global = global_config.read_bytes()
    user_hooks = {name: data + b"\n" for name, data in _live(repo).items()}
    attrs = repo / ".gitattributes"
    attrs.write_text("*.dat binary\n")
    git_config = ["git", "-C", str(repo), "config", "--local"]
    subprocess.run([*git_config, "merge.other.driver", "keep this driver"], check=True)
    hooks.install(repo)
    subprocess.run([*git_config, "--add", f"merge.graphify.{key}", "duplicate value"], check=True)
    duplicate = subprocess.run([*git_config, "--get-all", f"merge.graphify.{key}"], capture_output=True, text=True, check=True)
    assert len(duplicate.stdout.splitlines()) == 2

    for _ in range(2):
        if entry_point == "api":
            result = hooks.uninstall(repo)
        else:
            command = subprocess.run(
                [sys.executable, "-B", "-m", "graphify", "hook", "uninstall"], cwd=repo,
                env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent),
                     "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
            )
            assert command.returncode == 0, command.stderr
            result = command.stdout
        assert "merge driver:" in result
        assert global_config.read_bytes() == before_global
        assert _live(repo) == user_hooks
        assert attrs.read_text() == "*.dat binary\n"
        unrelated = subprocess.run([*git_config, "--get", "merge.other.driver"], capture_output=True, text=True, check=True)
        assert unrelated.stdout == "keep this driver\n"
        for removed in ("name", "driver", "recursive"):
            local = subprocess.run([*git_config, "--get-all", f"merge.graphify.{removed}"], capture_output=True)
            assert local.returncode == 1


def test_uninstall_reports_local_config_removal_failure_and_retries(repo):
    hooks.install(repo)
    config = (repo / ".git/config").read_bytes()
    attrs = (repo / ".gitattributes").read_bytes()
    lock = repo / ".git/config.lock"
    lock.write_text("fixture lock")
    try:
        with pytest.raises(RuntimeError, match="merge-driver config removal failed"):
            hooks.uninstall(repo)
        assert (repo / ".git/config").read_bytes() == config
        assert (repo / ".gitattributes").read_bytes() == attrs
        assert all(b"graphify-hook-start" not in data for data in _live(repo).values())
    finally:
        lock.unlink()
    assert "removed" in hooks.uninstall(repo)
    assert not (repo / ".gitattributes").exists()


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("replacement", ["root", "gitdir"])
def test_replacement_repository_cannot_resume_shared_hooks(repo, monkeypatch, tmp_path, operation, replacement):
    shared = tmp_path / "shared-hooks"
    _hooks(repo).rename(shared)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(shared)], check=True)
    if operation == "uninstall":
        hooks.install(repo)
    _interrupt(repo, monkeypatch, operation)
    config = (repo / ".git/config").read_bytes()
    attrs = (repo / ".gitattributes").read_bytes() if (repo / ".gitattributes").exists() else None
    selected = repo if replacement == "root" else repo / ".git"
    old_identity = publication._identity(selected.stat())
    selected.rename(tmp_path / "original-retained")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / ".git/config").write_bytes(config)
    if attrs is not None:
        (repo / ".gitattributes").write_bytes(attrs)
    assert publication._identity(selected.stat()) != old_identity
    before = (_snapshot(repo), _snapshot(shared), _snapshot(publication._state_root()))
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("replacement must not register"))
    monkeypatch.setattr(hooks, "_unregister_merge_driver", lambda *a, **k: pytest.fail("replacement must not unregister"))
    with pytest.raises(RuntimeError, match="different request"):
        getattr(hooks, operation)(repo)
    assert before == (_snapshot(repo), _snapshot(shared), _snapshot(publication._state_root()))
    assert f"pending {operation}" in hooks.status(repo)


def test_legacy_pending_request_without_physical_origin_refuses(repo, monkeypatch):
    _interrupt(repo, monkeypatch)
    with publication._authority(publication._state_root(), False) as (fd, _):
        identity = publication._identity(_hooks(repo).stat())
        slot = os.open(publication._slot(identity), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            store = publication._Store(slot, identity, False)
            request = store.body["active"]["binding"]["request"]
            request.pop("root_identity", None)
            request.pop("gitdir_identity", None)
            store.save()
        finally:
            os.close(slot)
    before = (_snapshot(repo), _snapshot(publication._state_root()))
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("legacy request must not register"))
    with pytest.raises(RuntimeError, match="different request"):
        hooks.install(repo)
    assert before == (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending install" in hooks.status(repo)


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("noop", [False, True])
def test_hardlinked_hook_refuses_before_staging(repo, monkeypatch, tmp_path, operation, noop):
    if (operation == "uninstall") != noop:
        hooks.install(repo)
    other = tmp_path / "other-repo"
    subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
    target, alias = _hooks(repo) / "post-commit", _hooks(other) / "post-commit"
    _set_extended_metadata(target, "xattr")
    os.link(target, alias)
    assert target.stat().st_nlink == alias.stat().st_nlink == 2
    before = (_snapshot(repo), _snapshot(other))
    monkeypatch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("linked hook must not register"))
    monkeypatch.setattr(hooks, "_unregister_merge_driver", lambda *a, **k: pytest.fail("linked hook must not unregister"))
    with pytest.raises(RuntimeError, match="hard-linked"):
        getattr(hooks, operation)(repo)
    assert before == (_snapshot(repo), _snapshot(other))
    assert target.stat().st_ino == alias.stat().st_ino
    assert target.stat().st_nlink == alias.stat().st_nlink == 2
    _assert_extended_metadata(target, "xattr")
    _assert_extended_metadata(alias, "xattr")


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("point", ["prepared", "published"])
def test_new_hardlink_stops_further_publication(repo, monkeypatch, tmp_path, operation, point):
    if operation == "uninstall":
        hooks.install(repo)
    other = tmp_path / "other-repo"
    subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
    target, alias = _hooks(repo) / "post-checkout", _hooks(other) / "post-checkout"
    _set_extended_metadata(target, "xattr")
    original_prepare, original_rename = publication._prepare, publication._rename
    stopped = []

    def add_link():
        os.link(target, alias)
        stopped.append((_snapshot(repo), _snapshot(other)))

    def prepare(*args, **kwargs):
        batch = original_prepare(*args, **kwargs)
        if point == "prepared":
            add_link()
        return batch

    def rename(source_fd, source, target_fd, destination, **kwargs):
        result = original_rename(source_fd, source, target_fd, destination, **kwargs)
        if point == "published" and destination == "post-commit":
            add_link()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_prepare", prepare)
        patch.setattr(publication, "_rename", rename)
        patch.setattr(hooks, "_register_merge_driver", lambda _: pytest.fail("linked hook must not register"))
        patch.setattr(hooks, "_unregister_merge_driver", lambda *a, **k: pytest.fail("linked hook must not unregister"))
        with pytest.raises(RuntimeError, match="hard-linked"):
            getattr(hooks, operation)(repo)
    assert len(stopped) == 1
    assert stopped[0] == (_snapshot(repo), _snapshot(other))
    assert target.stat().st_ino == alias.stat().st_ino
    assert target.stat().st_nlink == alias.stat().st_nlink == 2
    _assert_extended_metadata(target, "xattr")
    _assert_extended_metadata(alias, "xattr")
    alias.unlink()  # Remove only the alias created by this fixture.
    assert f"pending {operation}" in hooks.status(repo)
    assert getattr(hooks, operation)(repo)
    assert "pending" not in hooks.status(repo)


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("entry_point", ["api", "cli"])
def test_readonly_hooks_directory_refuses_with_actionable_error_and_retries(repo, operation, entry_point):
    if os.geteuid() == 0:
        pytest.skip("real directory permission denial requires a non-root process")
    if operation == "uninstall":
        hooks.install(repo)
    directory = _hooks(repo)
    target = directory / "post-commit"
    _set_extended_metadata(target, "xattr")
    mode = stat.S_IMODE(directory.stat().st_mode)

    def invoke():
        if entry_point == "api":
            return getattr(hooks, operation)(repo)
        return subprocess.run(
            [sys.executable, "-B", "-m", "graphify", "hook", operation], cwd=repo,
            env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent),
                 "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
        )

    directory.chmod(0o555)
    try:
        assert not os.access(directory, os.W_OK)
        assert all(os.access(directory / name, os.W_OK) for name in publication.NAMES)
        before = _snapshot(repo)
        entries = sorted(directory.iterdir())
        if entry_point == "api":
            with pytest.raises(RuntimeError) as error:
                invoke()
            message = str(error.value)
        else:
            result = invoke()
            assert result.returncode == 1
            message = result.stderr
        assert "atomic hook updates require permission to create and rename entries" in message
        assert "Ask the directory administrator" in message
        assert "retry the exact original command" in message
        assert str(directory) in message
        assert before == _snapshot(repo)
        assert entries == sorted(directory.iterdir())
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
        _assert_extended_metadata(target, "xattr")
    finally:
        directory.chmod(mode)  # Restore only this fixture's directory access.
    result = invoke()
    if entry_point == "api":
        assert "post-commit:" in result
    else:
        assert result.returncode == 0
        assert "post-commit:" in result.stdout
    assert (hooks._HOOK_MARKER.encode() in target.read_bytes()) == (operation == "install")
    assert "pending" not in hooks.status(repo)
    _assert_extended_metadata(target, "xattr")


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_readonly_hooks_directory_keeps_noop_behavior(repo, operation):
    if os.geteuid() == 0:
        pytest.skip("real directory permission denial requires a non-root process")
    if operation == "install":
        hooks.install(repo)
    directory = _hooks(repo)
    mode = stat.S_IMODE(directory.stat().st_mode)
    directory.chmod(0o555)
    try:
        assert not os.access(directory, os.W_OK)
        before = _snapshot(directory)
        config = (repo / ".git/config").read_bytes()
        attributes = repo / ".gitattributes"
        attrs = attributes.read_bytes() if attributes.exists() else None
        assert getattr(hooks, operation)(repo)
        assert before == _snapshot(directory)
        assert config == (repo / ".git/config").read_bytes()
        assert attrs == (attributes.read_bytes() if attributes.exists() else None)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
    finally:
        directory.chmod(mode)


@pytest.mark.parametrize("error_source", ["stage", "interpreter"])
def test_stage_permission_diagnostic_does_not_relabel_other_errors(repo, monkeypatch, error_source):
    mkdir = publication._mkdir_private
    error_number = errno.EPERM if error_source == "stage" else errno.EACCES

    def fail_stage(fd, name):
        if name.startswith(publication._PREFIX):
            filename = name if error_source == "stage" else sys.executable
            raise OSError(error_number, os.strerror(error_number), filename)
        return mkdir(fd, name)

    before = _snapshot(repo)
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_mkdir_private", fail_stage)
        with pytest.raises(RuntimeError) as error:
            hooks.install(repo)
    message = str(error.value)
    assert ("Ask the directory administrator" in message) == (error_source == "stage")
    if error_source == "interpreter":
        assert sys.executable in message
        assert os.strerror(errno.EACCES) in message
    assert before == _snapshot(repo)
    assert hooks.install(repo)
