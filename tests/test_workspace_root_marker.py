"""Initial ownership publication never exposes partial marker bytes."""

from concurrent.futures import ThreadPoolExecutor
import errno
import os
import stat
import subprocess
import sys
from threading import Event

import pytest

from graphify.storage_guard import WORKSPACE_ROOT_MARKER
from graphify.workspace.contracts import StateRootMarker
from graphify.workspace.persistence import (
    DurableStateRoot, PosixSyscalls, RuntimeCapabilities, StatePathError,
)


def _state(root, **kwargs):
    return DurableStateRoot(
        root, capabilities=RuntimeCapabilities.supported_test_fixture(), **kwargs,
    )


@pytest.mark.parametrize("stage", ["write", "rename"])
def test_interrupted_marker_publication_can_be_retried(tmp_path, stage):
    root = tmp_path.resolve() / "state"
    script = '''
import os
import sys
from pathlib import Path
from graphify.workspace.persistence import DurableStateRoot, PosixSyscalls, RuntimeCapabilities
class Die(PosixSyscalls):
    def write(self, descriptor, data):
        if sys.argv[2] == "write":
            os.write(descriptor, data[:1])
            os._exit(29)
        return super().write(descriptor, data)
    def rename_exclusive_at(self, *args, **kwargs):
        os._exit(29)
DurableStateRoot(Path(sys.argv[1]), capabilities=RuntimeCapabilities.supported_test_fixture(),
                 syscalls=Die()).ensure_directory("workspaces")
'''
    result = subprocess.run([sys.executable, "-c", script, str(root), stage], check=False)
    assert result.returncode == 29
    assert not (root / WORKSPACE_ROOT_MARKER).exists()
    temporary, = root.iterdir()
    retained = temporary.read_bytes()
    if stage == "write":
        assert retained == b"{"
    else:
        StateRootMarker.from_json(retained)
    _state(root).ensure_directory("workspaces")
    StateRootMarker.from_json((root / WORKSPACE_ROOT_MARKER).read_bytes())
    assert (root / "workspaces").is_dir()
    # A retry cannot assume a temp belongs to a dead initializer.
    assert temporary.read_bytes() == retained


def test_concurrent_first_use_validates_winner_without_removing_active_temp(tmp_path):
    root = tmp_path.resolve() / "state"
    partial = Event()
    resume = Event()

    class Pause(PosixSyscalls):
        def write(self, descriptor, data):
            if not partial.is_set():
                written = os.write(descriptor, data[:1])
                partial.set()
                assert resume.wait(10)
                return written
            return super().write(descriptor, data)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(_state(root, syscalls=Pause()).ensure_directory, "first")
        try:
            assert partial.wait(10)
            active, = root.iterdir()
            assert active.read_bytes() == b"{"
            assert not (root / WORKSPACE_ROOT_MARKER).exists()
            _state(root).ensure_directory("second")
            winner_inode = (root / WORKSPACE_ROOT_MARKER).stat().st_ino
            assert active.read_bytes() == b"{"
        finally:
            resume.set()
        first.result(timeout=10)
    assert (root / WORKSPACE_ROOT_MARKER).stat().st_ino == winner_inode
    assert {p.name for p in root.iterdir()} == {WORKSPACE_ROOT_MARKER, "first", "second"}


def test_marker_is_fsynced_before_exclusive_publication(tmp_path):
    root = tmp_path.resolve() / "state"
    synced = set()
    publication = []

    class Observe(PosixSyscalls):
        def fsync(self, descriptor):
            super().fsync(descriptor)
            synced.add(os.fstat(descriptor).st_ino)
            if publication and os.fstat(descriptor).st_ino == root.stat().st_ino:
                publication.append("root_fsync")

        def rename_exclusive_at(self, source, destination, **kwargs):
            details = os.stat(source, dir_fd=kwargs["source_dir_fd"])
            assert details.st_ino in synced
            super().rename_exclusive_at(source, destination, **kwargs)
            publication.append("rename")

        def mkdir_at(self, path, mode, *, dir_fd):
            if path == "workspaces":
                assert publication == ["rename", "root_fsync"]
                publication.append("descendant_mkdir")
            super().mkdir_at(path, mode, dir_fd=dir_fd)

    _state(root, syscalls=Observe()).ensure_directory("workspaces")
    assert publication[:3] == ["rename", "root_fsync", "descendant_mkdir"]


@pytest.mark.parametrize("stage", ["write", "fsync", "rename"])
def test_marker_install_io_failure_uses_state_path_error_and_allows_retry(tmp_path, stage):
    root = tmp_path.resolve() / "state"
    failure = OSError(errno.ENOSPC, "injected marker installation failure")

    class FailInstall(PosixSyscalls):
        def write(self, descriptor, data):
            if stage == "write":
                os.write(descriptor, data[:1])
                raise failure
            return super().write(descriptor, data)

        def fsync(self, descriptor):
            if stage == "fsync" and stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise failure
            return super().fsync(descriptor)

        def rename_exclusive_at(self, *args, **kwargs):
            if stage == "rename":
                raise failure
            return super().rename_exclusive_at(*args, **kwargs)

    with pytest.raises(StatePathError, match="ownership marker") as raised:
        _state(root, syscalls=FailInstall()).ensure_directory("workspaces")
    assert raised.value.__cause__ is failure
    assert not (root / WORKSPACE_ROOT_MARKER).exists()
    assert not (root / "workspaces").exists()
    assert list(root.iterdir()) == []
    _state(root).ensure_directory("workspaces")
    StateRootMarker.from_json((root / WORKSPACE_ROOT_MARKER).read_bytes())
    assert (root / "workspaces").is_dir()


def test_exclusive_publication_loss_preserves_and_rejects_invalid_winner(tmp_path):
    root = tmp_path.resolve() / "state"

    class InvalidWinner(PosixSyscalls):
        def rename_exclusive_at(self, source, destination, **kwargs):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600, dir_fd=kwargs["destination_dir_fd"])
            try:
                os.write(descriptor, b"{")
            finally:
                os.close(descriptor)
            raise FileExistsError(destination)

    with pytest.raises(StatePathError, match="ownership marker is unsafe"):
        _state(root, syscalls=InvalidWinner()).ensure_directory("workspaces")
    assert (root / WORKSPACE_ROOT_MARKER).read_bytes() == b"{"
    assert {p.name for p in root.iterdir()} == {WORKSPACE_ROOT_MARKER}


@pytest.mark.parametrize("name", ["unrelated", WORKSPACE_ROOT_MARKER + ".init-1-invalid"])
def test_initialization_preserves_unrelated_occupied_root(tmp_path, name):
    root = tmp_path.resolve() / "state"
    root.mkdir(mode=0o700)
    occupied = root / name
    occupied.write_bytes(b"preserve")
    occupied.chmod(0o600)
    before = occupied.stat()
    with pytest.raises(StatePathError, match="unmarked occupied"):
        _state(root).ensure_directory("workspaces")
    assert list(root.iterdir()) == [occupied]
    assert occupied.stat() == before
    assert occupied.read_bytes() == b"preserve"


def test_initialization_rejects_symlink_in_temporary_namespace(tmp_path):
    root = tmp_path.resolve() / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_bytes(b"preserve")
    temporary = root / (WORKSPACE_ROOT_MARKER + ".init-1-" + "a" * 32)
    temporary.symlink_to(target)
    with pytest.raises(StatePathError, match="singular regular file"):
        _state(root).ensure_directory("workspaces")
    assert temporary.is_symlink()
    assert target.read_bytes() == b"preserve"
