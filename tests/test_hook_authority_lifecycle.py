"""V2 issuance/preparation proof through isolated public installer entry points."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify import hooks
from tests.test_hook_installation import repo, _hooks, _live, _snapshot, _different_permitted_group  # noqa: F401

if os.name != "nt":
    from graphify import hook_installation as publication

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX authority lifecycle")


def slot(root):
    """Locate the fixture authority slot from its actual hooks-directory identity."""
    return publication._state_root() / publication._slot(publication._identity(_hooks(root).stat()))


def body(root):
    """Read the fixture journal body for lifecycle-state assertions."""
    return json.loads((slot(root) / "journal").read_bytes())["body"]


@pytest.mark.parametrize("point", ["capability", "journal", "publish"])
def test_incomplete_slot_is_not_canonical_and_retry_preserves_candidate(repo, monkeypatch, point):
    """Retry issues fresh authority while preserving an incomplete unpublished candidate."""
    write, rename = publication._write, publication._rename_directory
    before = _live(repo)

    def fail_write(fd, name, *args, **kwargs):
        """Inject a selected write failure before completing staged evidence."""
        if (point == "capability" and name == "capability"
                or point == "journal" and name.startswith("journal-")):
            raise OSError("injected initialization failure")
        return write(fd, name, *args, **kwargs)

    def fail_publish(*args, **kwargs):
        """Interrupt canonical publication without adopting the retained candidate."""
        raise OSError("injected initialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", fail_write)
        if point == "publish":
            patch.setattr(publication, "_rename_directory", fail_publish)
        with pytest.raises(RuntimeError, match="initialization failure"):
            hooks.install(repo)
    assert _live(repo) == before and not slot(repo).exists()
    candidates = list(publication._state_root().glob(publication._INITIALIZING + "*"))
    assert len(candidates) == 1
    retained = _snapshot(candidates[0])
    status_before = _snapshot(publication._state_root())
    assert "unverified authority" in hooks.status(repo)
    assert _snapshot(publication._state_root()) == status_before
    hooks.install(repo)
    assert body(repo)["schema"] == publication._LIFECYCLE_SCHEMA
    assert _snapshot(candidates[0]) == retained
    assert rename is publication._rename_directory


@pytest.mark.parametrize("name", [".gitignore", "pre-0", "0", "pre-1", "1", "pre-2", "2"])
def test_incomplete_private_preparation_retries_without_adopting_or_removing_files(repo, monkeypatch, name):
    """Exact retry retains incomplete preparation and every uncertain child unchanged."""
    write = publication._write
    before = _live(repo)

    def fail(fd, selected, *args, **kwargs):
        """Inject the selected preparation failure before live publication."""
        if selected == name:
            raise OSError("injected preparation failure")
        return write(fd, selected, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write", fail)
        with pytest.raises(RuntimeError, match="preparation failure"):
            hooks.install(repo)
    active = body(repo)["active"]
    assert active["phase"] == "PREPARING"
    original = slot(repo) / active["stage"]
    foreign = original / "unrecorded-foreign-content"
    foreign.write_bytes(b"preserve this uncertain child")
    retained = _snapshot(original)
    assert not list(_hooks(repo).glob(publication._PREFIX + "*"))
    assert _live(repo) == before and not (repo / ".gitattributes").exists()
    state = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "pending install (PREPARING)" in hooks.status(repo)
    assert state == (_snapshot(repo), _snapshot(publication._state_root()))
    hooks.install(repo)
    assert _snapshot(original) == retained
    assert body(repo)["retained"][0]["identity"] == publication._identity(original.stat())
    assert "retained incomplete" in hooks.status(repo)


@pytest.mark.parametrize("after", [False, True])
def test_exposure_interruption_reconciles_exact_stage(repo, monkeypatch, after):
    """Exposure retry accepts the recorded directory at exactly one legal location."""
    rename = publication._rename_directory
    before = _live(repo)

    def fail(source_fd, source, target_fd, target):
        """Interrupt exposure at the selected location without changing its ownership."""
        if source.startswith(publication._PREFIX):
            if after:
                rename(source_fd, source, target_fd, target)
            raise OSError("injected exposure uncertainty")
        return rename(source_fd, source, target_fd, target)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename_directory", fail)
        with pytest.raises(RuntimeError, match="exposure uncertainty"):
            hooks.install(repo)
    active = body(repo)["active"]
    assert active["phase"] == "EXPOSING" and _live(repo) == before
    location = _hooks(repo) if after else slot(repo)
    assert publication._identity((location / active["stage"]).stat()) == active["identity"]
    hooks.install(repo)
    assert body(repo)["active"] is None
    assert len(body(repo)["history"]) == 1


def test_copied_journal_refuses_even_with_unchanged_bytes(repo):
    """Identical journal bytes cannot authenticate a replacement filesystem object."""
    hooks.install(repo)
    journal = slot(repo) / "journal"
    original = journal.read_bytes()
    journal.rename(slot(repo) / "retained-journal")
    journal.write_bytes(original)
    journal.chmod(0o600)
    protected = (_snapshot(repo), _snapshot(publication._state_root()))
    assert "Copied or replaced" in hooks.status(repo)
    with pytest.raises(RuntimeError, match="Copied or replaced"):
        hooks.install(repo)
    assert protected == (_snapshot(repo), _snapshot(publication._state_root()))


@pytest.mark.parametrize("entry", ["api", "cli"])
def test_status_does_not_create_missing_hooks_or_authority(repo, monkeypatch, entry):
    """API and CLI status preserve missing hooks and authority paths."""
    path = repo / "missing" / "hooks"
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(path)], check=True)
    protected = _snapshot(repo)
    if entry == "api":
        mkdir = Path.mkdir
        def forbid(*args, **kwargs):
            """Fail immediately if read-only status attempts directory creation."""
            pytest.fail("status attempted directory creation")
        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", forbid)
            result = hooks.status(repo)
        assert mkdir is Path.mkdir
    else:
        command = subprocess.run([sys.executable, "-B", "-m", "graphify", "hook", "status"], cwd=repo,
                                 env={**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)},
                                 capture_output=True, text=True)
        assert command.returncode == 0, command.stderr
        result = command.stdout
    assert "hooks directory absent" in result
    assert not path.parent.exists() and not publication._state_root().exists()
    assert _snapshot(repo) == protected


def test_complete_history_has_closed_inventory_and_legacy_store_stays_legacy(repo):
    """Existing v1 authority remains v1 without gaining v2 retention permissions."""
    with publication._authority(publication._state_root(), True) as (fd, _):
        identity = publication._identity(_hooks(repo).stat())
        name = publication._slot(identity)
        publication._mkdir_private(fd, name)
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            publication._Store(child, identity, True)
        finally:
            os.close(child)
    hooks.install(repo)
    assert body(repo)["schema"] == publication._SCHEMA
    assert "inventory" not in body(repo)["history"][0]


@pytest.mark.parametrize("point", ["capability", "preparing", "exposure", "prepared"])
def test_process_kill_at_issuance_and_preparation_boundary(repo, point):
    """Abrupt process death retains evidence and permits only authenticated exact retry."""
    code = """
import os, signal, sys
from pathlib import Path
from graphify import hooks, hook_installation as p
write, save, rename = p._write, p._Store.save, p._rename_directory
def written(fd, name, *args, **kwargs):
    result = write(fd, name, *args, **kwargs)
    if sys.argv[2] == 'capability' and name == 'capability':
        os.kill(os.getpid(), signal.SIGKILL)
    return result
def saved(self):
    result = save(self)
    phase = (self.body.get('active') or {}).get('phase')
    if (sys.argv[2] == 'preparing' and phase == 'PREPARING'
            or sys.argv[2] == 'prepared' and phase == 'PREPARED'):
        os.kill(os.getpid(), signal.SIGKILL)
    return result
def renamed(*args, **kwargs):
    result = rename(*args, **kwargs)
    if sys.argv[2] == 'exposure' and args[1].startswith(p._PREFIX):
        os.kill(os.getpid(), signal.SIGKILL)
    return result
p._write, p._Store.save, p._rename_directory = written, saved, renamed
hooks.install(Path(sys.argv[1]))
"""
    before = _live(repo)
    killed = subprocess.run([sys.executable, "-B", "-c", code, str(repo), point], capture_output=True)
    assert killed.returncode == -9, killed.stderr.decode()
    assert _live(repo) == before and not (repo / ".gitattributes").exists()
    retained = {str(path): _snapshot(path) for path in publication._state_root().glob(publication._INITIALIZING + "*")}
    hooks.install(repo)
    assert body(repo)["active"] is None
    assert all(_snapshot(Path(path)) == snapshot for path, snapshot in retained.items())


def test_completed_inventory_keeps_preimages_and_displaced_originals(repo):
    """Completed inventory retains both copies of original user-hook content."""
    original = _live(repo)
    hooks.install(repo)
    record = body(repo)["history"][0]
    stage = _hooks(repo) / record["name"]
    assert set(record["inventory"]) == set(os.listdir(stage))
    for index, name in enumerate(publication.NAMES):
        for member in (str(index), "pre-" + str(index)):
            assert (stage / member).read_bytes() == original[name]
            original_data = original[name]
            assert original_data is not None
            assert record["inventory"][member]["sha256"] == hashlib.sha256(original_data).hexdigest()


def test_new_guards_keep_hooks_directory_native_creation_group(repo):
    """Private-born guards receive the selected hooks directory native creation group."""
    directory = _hooks(repo)
    os.chown(directory, -1, _different_permitted_group())
    if sys.platform.startswith("linux"):
        directory.chmod(directory.stat().st_mode | 0o2000)
    probe = directory / "fixture-creation-group"
    probe.write_bytes(b"fixture")
    expected = [probe.stat().st_uid, probe.stat().st_gid]
    hooks.install(repo, merge_guard=True)
    for name in ("pre-merge-commit", "pre-commit"):
        info = (directory / name).stat()
        assert [info.st_uid, info.st_gid] == expected


@pytest.mark.parametrize("damage", ["exclusion", "extra"])
def test_prepared_exclusion_and_unknown_members_refuse_before_live_publication(repo, monkeypatch, damage):
    """Prepared exclusion damage or foreign members blocks every live publication."""
    prepare = publication._prepare
    before = _live(repo)
    observed = {}
    def changed(*args, **kwargs):
        """Tamper with prepared evidence before any live hook may be published."""
        batch = prepare(*args, **kwargs)
        stage = _hooks(repo) / batch["stage"]
        if damage == "exclusion":
            (stage / ".gitignore").write_bytes(b"foreign exclusion")
        else:
            (stage / "foreign").write_bytes(b"preserve foreign content")
        observed["stage"] = _snapshot(stage)
        return batch
    monkeypatch.setattr(publication, "_prepare", changed)
    with pytest.raises(RuntimeError):
        hooks.install(repo)
    assert _live(repo) == before
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    assert _snapshot(stage) == observed["stage"]
    assert not (repo / ".gitattributes").exists()


@pytest.mark.parametrize("after", [False, True])
def test_exposing_retry_preserves_changed_exclusion_at_either_location(repo, monkeypatch, after):
    """Exposure retry preserves altered exclusion evidence at either legal location."""
    rename = publication._rename_directory
    before = _live(repo)
    def interrupted(source_fd, source, target_fd, target):
        """Interrupt exposure at the selected location without changing its ownership."""
        if source.startswith(publication._PREFIX):
            if after:
                rename(source_fd, source, target_fd, target)
            raise OSError("injected exposure failure")
        return rename(source_fd, source, target_fd, target)
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename_directory", interrupted)
        with pytest.raises(RuntimeError, match="exposure failure"):
            hooks.install(repo)
    active = body(repo)["active"]
    stage = (_hooks(repo) if after else slot(repo)) / active["stage"]
    (stage / ".gitignore").write_bytes(b"foreign exclusion")
    evidence = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="evidence changed"):
        hooks.install(repo)
    assert (_snapshot(repo), _snapshot(publication._state_root())) == evidence
    assert _live(repo) == before


def test_applying_retry_preserves_unknown_member_and_remaining_hooks(repo, monkeypatch):
    """Publication retry preserves foreign members and leaves remaining hooks exact."""
    rename = publication._rename
    def interrupted(*args, **kwargs):
        """Interrupt the first publication before recovery may advance."""
        result = rename(*args, **kwargs)
        if args[1] == "0":
            raise OSError("injected publication interruption")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rename", interrupted)
        with pytest.raises(RuntimeError, match="publication interruption"):
            hooks.install(repo)
    active = body(repo)["active"]
    assert active["phase"] == "APPLYING"
    stage = _hooks(repo) / active["stage"]
    (stage / "foreign").write_bytes(b"preserve foreign content")
    evidence = (_snapshot(repo), _snapshot(publication._state_root()))
    with pytest.raises(RuntimeError, match="inventory changed"):
        hooks.install(repo)
    assert (_snapshot(repo), _snapshot(publication._state_root())) == evidence
    assert not (repo / ".gitattributes").exists()


def test_writer_refuses_records_that_exceed_the_reader_limit(repo):
    """The writer rejects envelopes its bounded reader cannot accept."""
    hooks.install(repo)
    evidence = (_snapshot(repo), _snapshot(publication._state_root()))
    child = os.open(slot(repo), os.O_RDONLY | os.O_DIRECTORY)
    try:
        store = publication._Store(child, publication._identity(_hooks(repo).stat()), False)
        store.body["retention_probe"] = "x" * (16 * 1024 * 1024)
        with pytest.raises(RuntimeError, match="capacity exceeded"):
            store._sealed()
    finally:
        os.close(child)
    assert (_snapshot(repo), _snapshot(publication._state_root())) == evidence


def test_completion_capacity_refusal_keeps_authenticated_pending_recovery(repo, monkeypatch):
    """Completion capacity refusal keeps pending authority usable for exact retry."""
    finish = publication._Store.finish
    def limited(self, check):
        """Reduce the record limit at completion and retain authenticated pending work."""
        with monkeypatch.context() as patch:
            patch.setattr(publication, "_RECORD_LIMIT", len(self._sealed()) - 1)
            return finish(self, check)
    with monkeypatch.context() as patch:
        patch.setattr(publication._Store, "finish", limited)
        with pytest.raises(RuntimeError, match="capacity exceeded"):
            hooks.install(repo)
    assert body(repo)["active"]["phase"] == "APPLYING"
    assert not (slot(repo) / "completion").exists()
    assert not (repo / ".gitattributes").exists()
    stage, = _hooks(repo).glob(publication._PREFIX + "*")
    evidence = _snapshot(stage)
    assert "pending install" in hooks.status(repo)
    hooks.install(repo)
    assert body(repo)["active"] is None and _snapshot(stage) == evidence
