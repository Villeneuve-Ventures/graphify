"""Regressions for the recorded PR #208 authority-lifecycle feedback."""
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest
from graphify import hooks
from tests.test_hook_installation import repo, _hooks, _snapshot  # noqa: F401
from tests.test_hook_authority_lifecycle import body, slot

if os.name != "nt":
    from graphify import hook_installation as publication

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX authority lifecycle")


def _evidence(root):
    """Include directory identities as well as protected file contents."""
    directories = {str(p.relative_to(root)): (p.stat().st_dev, p.stat().st_ino,
                   p.stat().st_mode, p.stat().st_uid, p.stat().st_gid)
                   for p in [root, *root.rglob("*")] if p.is_dir()}
    return directories, _snapshot(root)


def _interrupt(root, monkeypatch, phase):
    """Stop preparation, exposure, or first publication at a real boundary."""
    write, rename_directory, rename = publication._write, publication._rename_directory, publication._rename

    def written(fd, name, *args, **kwargs):
        """Stop before a preparation payload is written."""
        if phase == "preparing" and name == "0":
            raise OSError("injected preparation interruption")
        return write(fd, name, *args, **kwargs)

    def exposed(*args, **kwargs):
        """Stop before or after exposure while preserving the actual stage location."""
        if phase in ("exposing-private", "exposing-hooks") and args[1].startswith(publication._PREFIX):
            if phase == "exposing-hooks":
                rename_directory(*args, **kwargs)
            raise OSError("injected exposure interruption")
        return rename_directory(*args, **kwargs)

    def published(*args, **kwargs):
        """Stop after the first hook publication with its sealed batch still active."""
        result = rename(*args, **kwargs)
        if phase == "applying" and args[1] == "0":
            raise OSError("injected publication interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", written)
        patch.setattr(publication, "_rename_directory", exposed)
        patch.setattr(publication, "_rename", published)
        with pytest.raises(RuntimeError, match="injected"):
            hooks.install(root)


@pytest.mark.parametrize("kind", ["failed", "invalid", "oserror"])
@pytest.mark.parametrize("entry", ["api", "cli"])
def test_status_lookup_failure_is_read_only_diagnostic(repo, monkeypatch, kind, entry):
    """Hooks-path failures return unverified diagnostics through API and CLI without writes."""
    protected = _evidence(repo)
    run = subprocess.run

    def intercepted(command, *args, **kwargs):
        """Fail only Git hooks-path lookup while retaining real repository discovery."""
        if command[0] == "git" and command[-2:] == ["--git-path", "hooks"]:
            if kind == "oserror":
                raise OSError(errno.EACCES, "injected hooks lookup failure")
            return subprocess.CompletedProcess(command, 1 if kind == "failed" else 0,
                                                "" if kind == "failed" else "bad\npath",
                                                "injected hooks lookup failure")
        return run(command, *args, **kwargs)

    if entry == "api":
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "run", intercepted)
            patch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("status attempted mkdir"))
            result = hooks.status(repo)
    else:
        script = f"""
import errno, runpy, subprocess, sys
run = subprocess.run
def intercepted(command, *args, **kwargs):
    if command[0] == 'git' and command[-2:] == ['--git-path', 'hooks']:
        if {kind!r} == 'oserror':
            raise OSError(errno.EACCES, 'injected hooks lookup failure')
        return subprocess.CompletedProcess(command, 1 if {kind!r} == 'failed' else 0,
                                           '' if {kind!r} == 'failed' else 'bad\\npath',
                                           'injected hooks lookup failure')
    return run(command, *args, **kwargs)
subprocess.run = intercepted
sys.argv = ['graphify', 'hook', 'status']
runpy.run_module('graphify', run_name='__main__')
"""
        command = run([sys.executable, "-B", "-c", script], cwd=repo,
                      env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
                      capture_output=True, text=True)
        assert command.returncode == 0, command.stderr
        assert "Traceback" not in command.stderr
        result = command.stdout
    assert "unverified authority" in result
    assert _evidence(repo) == protected
    assert not publication._state_root().exists()


@pytest.mark.parametrize("phase", ["preparing", "exposing-private", "exposing-hooks", "applying"])
@pytest.mark.parametrize("damage", ["missing", "substituted"])
def test_status_damaged_active_stage_is_unverified_and_preserved(repo, monkeypatch, phase, damage):
    """Missing or substituted active stages report unverified evidence without repair."""
    _interrupt(repo, monkeypatch, phase)
    active = body(repo)["active"]
    parent = slot(repo) if phase in ("preparing", "exposing-private") else _hooks(repo)
    stage = parent / active["stage"]
    stage.rename(parent / "preserved-original")
    if damage == "substituted":
        stage.mkdir(mode=0o700)
        (stage / "foreign").write_bytes(b"preserve foreign content")
    protected = (_evidence(repo), _evidence(publication._state_root()))
    result = hooks.status(repo)
    assert "unverified" in result and "pending install (" not in result
    assert (_evidence(repo), _evidence(publication._state_root())) == protected


@pytest.mark.parametrize("phase", ["exposing-private", "exposing-hooks"])
def test_status_reports_actual_exposing_location_without_writes(repo, monkeypatch, phase):
    """EXPOSING status names the observed directory and preserves both locations."""
    _interrupt(repo, monkeypatch, phase)
    active = body(repo)["active"]
    parent = slot(repo) if phase == "exposing-private" else _hooks(repo)
    protected = (_evidence(repo), _evidence(publication._state_root()))
    result = hooks.status(repo)
    assert "pending install (EXPOSING)" in result
    assert f"recovery files: {parent / active['stage']}" in result
    assert (_evidence(repo), _evidence(publication._state_root())) == protected


def test_initializing_candidate_diagnostic_does_not_poison_other_repository(repo, monkeypatch):
    """Unpublished initialization hints do not taint an unrelated repository status."""
    write = publication._write

    def interrupted(fd, name, *args, **kwargs):
        """Stop capability creation and retain the unpublished candidate."""
        if name == "capability":
            raise OSError("injected initialization interruption")
        return write(fd, name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", interrupted)
        with pytest.raises(RuntimeError, match="initialization interruption"):
            hooks.install(repo)
    other = repo.parent / "other-repository"
    subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
    protected = (_evidence(repo.parent), _evidence(publication._state_root()))
    assert "unverified authority" in hooks.status(repo)
    result = hooks.status(other)
    assert "unverified authority" not in result and "post-commit: not installed" in result
    assert (_evidence(repo.parent), _evidence(publication._state_root())) == protected


def test_capacity_retries_create_no_journal_temporaries(repo, monkeypatch):
    """Repeated capacity refusal preserves the existing authority tree exactly."""
    hooks.install(repo)
    protected = _evidence(publication._state_root())
    child = os.open(slot(repo), os.O_RDONLY | os.O_DIRECTORY)
    try:
        store = publication._Store(child, publication._identity(_hooks(repo).stat()), False)
        store.body["capacity_probe"] = "x" * 1024
        monkeypatch.setattr(publication, "_RECORD_LIMIT", len(store._sealed()) - 1)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="capacity exceeded"):
                store.save()
        assert _evidence(publication._state_root()) == protected
    finally:
        os.close(child)


@pytest.mark.parametrize("entry", ["api", "cli"])
def test_unclaimed_private_stage_takes_precedence_over_retained_evidence(repo, monkeypatch, entry):
    """An interrupted stage birth stays visible even beside authenticated retained evidence."""
    _interrupt(repo, monkeypatch, "preparing")
    save = publication._Store.save

    def interrupted(store):
        """Stop the next stage before its PREPARING ownership is persisted."""
        active = store.body["active"]
        if store.body.get("retained") and active is not None and active["phase"] == "PREPARING":
            raise OSError("injected unrecorded stage birth")
        return save(store)

    with monkeypatch.context() as patch:
        patch.setattr(publication._Store, "save", interrupted)
        with pytest.raises(RuntimeError, match="unrecorded stage birth"):
            hooks.install(repo)
    record = body(repo)
    assert record["active"] is None and len(record["retained"]) == 1
    known = {item["name"] for item in record["retained"]}
    unclaimed = [path for path in slot(repo).iterdir()
                 if path.name.startswith(publication._PREFIX) and path.name not in known]
    assert len(unclaimed) == 1
    (unclaimed[0] / "uncertain-content").write_bytes(b"preserve without adoption")
    protected = (_evidence(repo), _evidence(publication._state_root()))
    if entry == "api":
        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("status attempted mkdir"))
            result = hooks.status(repo)
    else:
        command = subprocess.run([sys.executable, "-B", "-m", "graphify", "hook", "status"], cwd=repo,
                                 env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
                                 capture_output=True, text=True)
        assert command.returncode == 0 and "Traceback" not in command.stderr, command.stderr
        result = command.stdout
    assert "unverified authority" in result and "unclaimed private preparation" in result
    assert (_evidence(repo), _evidence(publication._state_root())) == protected


@pytest.mark.parametrize("phase", ["completed", "active", "retained"])
@pytest.mark.parametrize("entry", ["api", "cli"])
def test_unpublished_candidate_is_visible_beside_canonical_authority(repo, monkeypatch, phase, entry):
    """A fresh canonical slot does not hide an earlier unclaimed authority candidate."""
    write = publication._write

    def interrupted(fd, name, *args, **kwargs):
        """Retain an unpublished candidate before its capability becomes complete."""
        if name == "capability":
            raise OSError("injected unpublished candidate")
        return write(fd, name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", interrupted)
        with pytest.raises(RuntimeError, match="unpublished candidate"):
            hooks.install(repo)
    if phase != "completed":
        _interrupt(repo, monkeypatch, "preparing")
    if phase != "active":
        hooks.install(repo)
    assert slot(repo).exists()
    protected = (_evidence(repo), _evidence(publication._state_root()))
    if entry == "api":
        result = hooks.status(repo)
    else:
        command = subprocess.run([sys.executable, "-B", "-m", "graphify", "hook", "status"], cwd=repo,
                                 env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
                                 capture_output=True, text=True)
        assert command.returncode == 0 and "Traceback" not in command.stderr, command.stderr
        result = command.stdout
    assert "unpublished authority candidates retained" in result
    if phase == "completed":
        assert "post-commit: installed" in result and "merge driver: registered" in result
    elif phase == "active":
        assert "pending install (PREPARING)" in result
        pending = publication._pending_record(_hooks(repo))
        assert isinstance(pending, dict)
        assert pending["binding"] == body(repo)["active"]["binding"]
    else:
        assert "retained incomplete" in result
    assert (_evidence(repo), _evidence(publication._state_root())) == protected


@pytest.mark.parametrize("point", ["guard-write", "guard-flush", "completion-check", "terminal-save"])
@pytest.mark.parametrize("merge_guard", [False, True])
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_completion_guard_interruption_retains_exact_retry(repo, monkeypatch, point, merge_guard, operation):
    """A valid completion obligation agrees with its persisted APPLYING predecessor."""
    if operation == "uninstall":
        hooks.install(repo, merge_guard=merge_guard)
    write, finish, save, fsync = publication._write, publication._Store.finish, publication._Store.save, os.fsync

    def written(fd, name, *args, **kwargs):
        """Fail after the completion file is fully written and flushed."""
        result = write(fd, name, *args, **kwargs)
        if point == "guard-write" and name == "completion":
            raise OSError("injected completion boundary")
        return result

    def saved(store):
        """Fail before saving terminal history while the completion guard remains."""
        if point == "terminal-save" and store.body["active"] is None and getattr(store, "guard", None) is not None:
            raise OSError("injected completion boundary")
        return save(store)

    def finished(store, check):
        """Inject only the selected guard flush or completion validation boundary."""
        def flushed(fd):
            """Stop the guard directory flush without changing its evidence."""
            if point == "guard-flush" and fd == store.fd:
                raise OSError("injected completion boundary")
            return fsync(fd)

        def checked():
            """Stop after verifying the fully published hooks."""
            check()
            if point == "completion-check":
                raise OSError("injected completion boundary")

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", flushed)
            return finish(store, checked)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", written)
        patch.setattr(publication._Store, "save", saved)
        patch.setattr(publication._Store, "finish", finished)
        with pytest.raises(RuntimeError, match="completion boundary"):
            getattr(hooks, operation)(repo, merge_guard=merge_guard)
    assert (slot(repo) / "completion").is_file()
    protected = (_evidence(repo), _evidence(publication._state_root()))
    status = hooks.status(repo, merge_guard=merge_guard)
    assert f"pending {operation} (APPLYING)" in status and "Conflicting completion obligation" not in status
    assert (_evidence(repo), _evidence(publication._state_root())) == protected
    getattr(hooks, operation)(repo, merge_guard=merge_guard)
    assert body(repo)["active"] is None and not (slot(repo) / "completion").exists()
    expected = "merge driver: registered" if operation == "install" else "merge driver: not registered"
    assert expected in hooks.status(repo)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SELinux label observation")
@pytest.mark.parametrize("phase", ["new", "preparing", "exposing-private", "exposing-hooks", "applying"])
def test_selinux_target_label_refuses_new_hook_creation(repo, monkeypatch, phase):
    """Observed target SELinux labeling blocks private-born hooks without relabeling."""
    for name in publication.NAMES:
        (_hooks(repo) / name).unlink()
    if phase != "new":
        _interrupt(repo, monkeypatch, phase)
    listxattr = getattr(os, "listxattr")
    identity = publication._identity(_hooks(repo).stat())

    def labeled(fd, *args, **kwargs):
        """Inject a label observation on only the selected hooks descriptor."""
        result = listxattr(fd, *args, **kwargs)
        if isinstance(fd, int) and publication._identity(os.fstat(fd)) == identity:
            return [*result, "security.selinux"]
        return result

    protected = (_evidence(repo), _evidence(publication._state_root()) if phase != "new" else None)
    with monkeypatch.context() as patch:
        patch.setattr(os, "listxattr", labeled)
        with pytest.raises(RuntimeError, match="Cannot preserve.*SELinux"):
            hooks.install(repo)
    assert _evidence(repo) == protected[0]
    if phase == "new":
        assert body(repo)["active"] is None and not list(slot(repo).glob(publication._PREFIX + "*"))
    else:
        assert _evidence(publication._state_root()) == protected[1]


def _macos_inheritable_deny_acl(directory):
    """Set a native macOS directory ACL whose denial passes to newly created files."""
    subprocess.run(["/bin/chmod", "+a", "user:nobody deny read,execute,file_inherit,directory_inherit,only_inherit",
                    str(directory)], check=True, capture_output=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        acl = publication._darwin_acl(fd)
        assert acl is not None
        return acl
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS inherited ACL")
def test_new_macos_hook_inherited_acl_refuses_without_live_changes(repo):
    """Private preparation cannot silently lose the hooks directory's native ACL denial."""
    (_hooks(repo) / "post-commit").unlink()
    acl = _macos_inheritable_deny_acl(_hooks(repo))
    control = _hooks(repo) / "acl-inheritance-control"
    control.write_bytes(b"native inherited ACL control")
    fd = os.open(control, os.O_RDONLY)
    try:
        assert publication._darwin_acl(fd) is not None
    finally:
        os.close(fd)
    protected = _evidence(repo)
    with pytest.raises(RuntimeError, match="Cannot preserve hooks-directory.*ACL"):
        hooks.install(repo)
    assert _evidence(repo) == protected
    assert body(repo)["active"] is None
    assert not list(slot(repo).glob(publication._PREFIX + "*"))
    assert acl == _macos_acl(_hooks(repo))


def _macos_acl(directory):
    """Read a native directory ACL through the existing descriptor-based helper."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return publication._darwin_acl(fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS inherited ACL")
@pytest.mark.parametrize("phase", ["preparing", "exposing-private", "exposing-hooks", "applying"])
def test_macos_acl_added_during_pending_creation_refuses_retry(repo, monkeypatch, phase):
    """Pending retry rechecks native target ACLs without altering hooks or authority."""
    for name in publication.NAMES:
        (_hooks(repo) / name).unlink()
    _interrupt(repo, monkeypatch, phase)
    acl = _macos_inheritable_deny_acl(_hooks(repo))
    protected = (_evidence(repo), _evidence(publication._state_root()))
    with pytest.raises(RuntimeError, match="Cannot preserve hooks-directory.*ACL"):
        hooks.install(repo)
    assert (_evidence(repo), _evidence(publication._state_root())) == protected
    assert _macos_acl(_hooks(repo)) == acl


def _default_acl(directory):
    """Set a native Linux inherited ACL containing a named account denial."""
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identity)
        for tag, permissions, identity in [(1, 7, 0xFFFFFFFF), (2, 0, 65534),
                                           (4, 5, 0xFFFFFFFF), (16, 5, 0xFFFFFFFF),
                                           (32, 5, 0xFFFFFFFF)])
    getattr(os, "setxattr")(directory, "system.posix_acl_default", acl)
    return getattr(os, "getxattr")(directory, "system.posix_acl_default")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Linux POSIX default ACL")
@pytest.mark.parametrize("legacy", [False, True])
def test_new_hook_default_acl_refuses_without_live_changes(repo, legacy):
    """Native target default ACLs cause refusal before any new hook is published."""
    if legacy:
        with publication._authority(publication._state_root(), True) as (fd, _):
            identity = publication._identity(_hooks(repo).stat())
            publication._mkdir_private(fd, publication._slot(identity))
            child = os.open(publication._slot(identity), os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            try:
                publication._Store(child, identity, True)
            finally:
                os.close(child)
    acl = _default_acl(_hooks(repo))
    protected = _evidence(repo)
    with pytest.raises(RuntimeError, match="Cannot preserve hooks-directory.*ACL|Unsafe installer authority"):
        hooks.install(repo, merge_guard=True)
    observed = _evidence(repo)
    for before, after in zip(protected, observed, strict=True):
        assert {name: after[name] for name in before} == before
    if not legacy:
        assert observed == protected
    assert getattr(os, "getxattr")(_hooks(repo), "system.posix_acl_default") == acl
    assert not (repo / ".gitattributes").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Linux POSIX default ACL")
@pytest.mark.parametrize("phase", ["exposing-private", "exposing-hooks", "applying"])
def test_default_acl_added_during_pending_creation_refuses_and_preserves(repo, monkeypatch, phase):
    # Missing default hooks make each prepared successor a new file.
    """A newly added default ACL blocks pending publication and preserves all evidence."""
    for name in publication.NAMES:
        (_hooks(repo) / name).unlink()
    _interrupt(repo, monkeypatch, phase)
    acl = _default_acl(_hooks(repo))
    protected = (_evidence(repo), _evidence(publication._state_root()))
    with pytest.raises(RuntimeError, match="Cannot preserve hooks-directory.*ACL"):
        hooks.install(repo)
    assert (_evidence(repo), _evidence(publication._state_root())) == protected
    assert getattr(os, "getxattr")(_hooks(repo), "system.posix_acl_default") == acl
