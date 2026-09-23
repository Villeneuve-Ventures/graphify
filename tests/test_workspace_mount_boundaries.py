"""Private tree traversal must stay on the held state root's filesystem."""

import os
from types import SimpleNamespace

import pytest

from graphify.workspace.persistence import (
    DurableStateRoot, RuntimeCapabilities, StatePathError,
)


DIRECTORY_MODES = frozenset({0o700})
FILE_MODES = frozenset({0o600})


def _private_tree(tmp_path):
    state = DurableStateRoot(
        tmp_path.resolve() / "state",
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    state.ensure_directory("quarantine/nested/volume")
    protected = state.create_private_file_bytes(
        "quarantine/nested/volume/keep", b"protected volume data", label="fixture",
    )
    return state, protected


def _simulate_device(monkeypatch, directory, enabled=lambda: True):
    """Present a mount's consistent stat/fstat identity without mounting a volume."""
    identities = {
        (details.st_dev, details.st_ino)
        for path in (directory, *directory.rglob("*"))
        for details in (path.lstat(),)
    }
    real_stat, real_fstat, real_lstat = os.stat, os.fstat, os.lstat

    def changed(details):
        if enabled() and (details.st_dev, details.st_ino) in identities:
            fields = {name: getattr(details, name) for name in dir(details)
                      if name.startswith("st_")}
            fields["st_dev"] += 1
            return SimpleNamespace(**fields)
        return details

    monkeypatch.setattr(os, "stat", lambda *args, **kwargs: changed(real_stat(*args, **kwargs)))
    monkeypatch.setattr(os, "fstat", lambda fd: changed(real_fstat(fd)))
    monkeypatch.setattr(os, "lstat", lambda *args, **kwargs: changed(real_lstat(*args, **kwargs)))


def _snapshot(path):
    details = path.stat()
    return (path.read_bytes(), details.st_ino, details.st_mode,
            details.st_nlink, details.st_size, details.st_mtime_ns, details.st_ctime_ns)


def test_simulated_mount_keeps_descendants_on_one_device(tmp_path, monkeypatch):
    state, protected = _private_tree(tmp_path)
    mounted = state.root / "quarantine"
    original_device = mounted.stat().st_dev
    _simulate_device(monkeypatch, mounted)
    for path in (mounted, *mounted.rglob("*")):
        assert path.stat().st_dev == original_device + 1
        assert path.lstat().st_dev == original_device + 1
        descriptor = os.open(path, os.O_RDONLY)
        try:
            assert os.fstat(descriptor).st_dev == original_device + 1
        finally:
            os.close(descriptor)
    assert state.root.stat().st_dev == original_device
    assert protected.read_bytes() == b"protected volume data"


@pytest.mark.parametrize("relative", ["quarantine", "quarantine/nested/volume"])
def test_tree_validation_rejects_foreign_device(tmp_path, monkeypatch, relative):
    state, protected = _private_tree(tmp_path)
    before = _snapshot(protected)
    _simulate_device(monkeypatch, protected.parent)
    with pytest.raises(StatePathError, match="filesystem boundary"):
        state.tree_bytes(
            relative, allowed_directory_modes=DIRECTORY_MODES, allowed_file_modes=FILE_MODES,
        )
    assert _snapshot(protected) == before


@pytest.mark.parametrize("mount_at", ["quarantine", "nested", "after_validation"])
def test_tree_removal_preserves_foreign_volume(tmp_path, monkeypatch, mount_at):
    state, protected = _private_tree(tmp_path)
    before = _snapshot(protected)
    active = mount_at != "after_validation"
    directory = state.root / "quarantine" if mount_at == "quarantine" else protected.parent
    _simulate_device(monkeypatch, directory, enabled=lambda: active)
    real_remove = state._remove_tree_contents_descriptor
    validation_completed = False

    def mount_then_remove(*args, **kwargs):
        nonlocal active, validation_completed
        validation_completed = True
        active = True
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(state, "_remove_tree_contents_descriptor", mount_then_remove)
    error = None
    try:
        state.remove_private_tree(
            "quarantine", allowed_directory_modes=DIRECTORY_MODES, allowed_file_modes=FILE_MODES,
        )
    except StatePathError as exc:
        error = exc
    # Assert preservation first so the pre-fix failure demonstrates destructive traversal.
    assert protected.exists(), "cleanup deleted a file on the foreign volume"
    assert _snapshot(protected) == before
    assert error is not None and "filesystem boundary" in str(error)
    assert validation_completed == (mount_at == "after_validation")


def test_state_root_may_be_a_mount_point(tmp_path, monkeypatch):
    state, protected = _private_tree(tmp_path)
    # The parent's device may differ from the state root. Descendant authority
    # starts at the root, not its containing filesystem (e.g. an APFS volume).
    _simulate_device(monkeypatch, state.root)
    assert state.tree_bytes(
        "quarantine", allowed_directory_modes=DIRECTORY_MODES, allowed_file_modes=FILE_MODES,
    ) == len(protected.read_bytes())
    assert state.remove_private_tree(
        "quarantine", allowed_directory_modes=DIRECTORY_MODES, allowed_file_modes=FILE_MODES,
    )
    assert not (state.root / "quarantine").exists()
