"""Publication acknowledges canonical bindings and preserves immutable winners."""

import errno
import os
import stat

import pytest

from graphify.workspace.persistence import (
    CommitUnknown, DurableStateRoot, PosixSyscalls, RuntimeCapabilities, StateCorrupt,
)


def _state(root, *, syscalls=None, fault_hook=None):
    return DurableStateRoot(root, capabilities=RuntimeCapabilities.supported_test_fixture(),
                            syscalls=syscalls, fault_hook=fault_hook)


@pytest.mark.parametrize("binding", ["root", "parent"])
@pytest.mark.parametrize("stage", ["callback", "parent_sync"])
def test_replace_does_not_acknowledge_detached_parent(tmp_path, binding, stage):
    root = tmp_path.resolve() / "state"
    state = _state(root)
    parent = state.ensure_directory("objects")
    parent_inode = parent.stat().st_ino
    moved = False

    def detach():
        nonlocal moved
        if not moved:
            moved = True
            source = root if binding == "root" else parent
            source.rename(source.with_name(source.name + "-detached"))

    class DetachOnSync(PosixSyscalls):
        def fsync(self, descriptor):
            super().fsync(descriptor)
            if (stage == "parent_sync" and os.fstat(descriptor).st_ino == parent_inode
                    and (parent / "item").exists()):
                detach()

    def hook(event):
        if stage == "callback" and event == "item:replaced":
            detach()

    state = _state(root, syscalls=DetachOnSync(), fault_hook=hook)
    with pytest.raises(CommitUnknown):
        state.atomic_replace_bytes("objects/item", b"data", label="item")
    assert moved
    assert not (parent / "item").exists()


@pytest.mark.parametrize("method", ["write_once", "install_once_bytes"])
@pytest.mark.parametrize("winner", [b"other creator", b"candidate"])
def test_immutable_publication_preserves_concurrent_winner(tmp_path, method, winner):
    root = tmp_path.resolve() / "state"
    parent = _state(root).ensure_directory("objects")
    winner_inode = None

    class PublishWinner(PosixSyscalls):
        def race(self, destination, descriptor):
            nonlocal winner_inode
            if destination == "item" and winner_inode is None:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=descriptor)
                try:
                    os.write(fd, winner)
                    os.fsync(fd)
                    winner_inode = os.fstat(fd).st_ino
                finally:
                    os.close(fd)
                os.fsync(descriptor)

        def replace_at(self, source, destination, **kwargs):
            self.race(destination, kwargs["destination_dir_fd"])
            return super().replace_at(source, destination, **kwargs)

        def rename_exclusive_at(self, source, destination, **kwargs):
            self.race(destination, kwargs["destination_dir_fd"])
            return super().rename_exclusive_at(source, destination, **kwargs)

    state = _state(root, syscalls=PublishWinner())
    install = getattr(state, method)
    kwargs = {"label": "item"} if method == "install_once_bytes" else {}
    error = None
    try:
        install("objects/item", b"candidate", **kwargs)
    except StateCorrupt as exc:
        error = exc
    assert (parent / "item").read_bytes() == winner
    assert (parent / "item").stat().st_ino == winner_inode
    assert (error is not None) == (winner != b"candidate")
    assert sorted(path.name for path in parent.iterdir()) == ["item"]


@pytest.mark.parametrize("stage", ["write", "file_sync", "parent_sync"])
def test_private_creation_failure_has_recoverable_visibility(tmp_path, stage):
    root = tmp_path.resolve() / "state"
    parent = _state(root).ensure_directory("objects")
    failure = OSError(errno.EIO, "injected creation failure")

    class FailCreation(PosixSyscalls):
        def write(self, descriptor, data):
            if stage == "write":
                os.write(descriptor, data[:2])
                raise failure
            return super().write(descriptor, data)

        def fsync(self, descriptor):
            is_file = stat.S_ISREG(os.fstat(descriptor).st_mode)
            if (stage == "file_sync" and is_file) or (stage == "parent_sync" and not is_file):
                raise failure
            return super().fsync(descriptor)

    expected = CommitUnknown if stage == "parent_sync" else OSError
    with pytest.raises(expected):
        _state(root, syscalls=FailCreation()).create_private_file_bytes(
            "objects/item", b"complete", label="item",
        )
    path = parent / "item"
    if stage == "parent_sync":
        assert path.read_bytes() == b"complete"
        assert _state(root).install_once_bytes("objects/item", b"complete", label="item") == path
    else:
        assert not path.exists(), "definite failure left a poison private file"
        assert _state(root).create_private_file_bytes(
            "objects/item", b"complete", label="item",
        ) == path


def test_failed_private_creation_does_not_remove_replacement_inode(tmp_path):
    root = tmp_path.resolve() / "state"
    parent = _state(root).ensure_directory("objects")
    path = parent / "item"
    replacement = parent / "replacement"
    replacement.write_bytes(b"preserve replacement")
    replacement.chmod(0o600)
    identity = replacement.stat().st_ino

    class RebindThenFail(PosixSyscalls):
        def write(self, descriptor, data):
            replacement.replace(path)
            raise OSError(errno.EIO, "write failed after rebinding")

    with pytest.raises(CommitUnknown, match="rollback"):
        _state(root, syscalls=RebindThenFail()).create_private_file_bytes(
            "objects/item", b"candidate", label="item",
        )
    assert path.stat().st_ino == identity
    assert path.read_bytes() == b"preserve replacement"


@pytest.mark.parametrize("method", ["write_once", "install_once_bytes"])
def test_overlapping_immutable_creators_keep_each_others_staging(tmp_path, method):
    root = tmp_path.resolve() / "state"
    parent = _state(root).ensure_directory("objects")
    winner_inode = None
    kwargs = {"label": "item"} if method == "install_once_bytes" else {}

    class PublishDuringStageSync(PosixSyscalls):
        def fsync(self, descriptor):
            nonlocal winner_inode
            super().fsync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode) and winner_inode is None:
                getattr(_state(root), method)("objects/item", b"candidate", **kwargs)
                winner_inode = (parent / "item").stat().st_ino

    getattr(_state(root, syscalls=PublishDuringStageSync()), method)(
        "objects/item", b"candidate", **kwargs,
    )
    assert (parent / "item").stat().st_ino == winner_inode
    assert (parent / "item").read_bytes() == b"candidate"
    assert sorted(path.name for path in parent.iterdir()) == ["item"]


@pytest.mark.parametrize("change", ["parent", "destination"])
def test_exclusive_rename_revalidates_after_durability_hook(tmp_path, change):
    root = tmp_path.resolve() / "state"
    state = _state(root)
    state.ensure_directory("source")
    parent = state.ensure_directory("destination")
    state.create_private_file_bytes("source/item", b"original", label="item")
    moved = False

    def hook(event):
        nonlocal moved
        if event == "move:destination_parent_durable":
            moved = True
            if change == "parent":
                parent.rename(root / "detached")
            else:
                replacement = parent / "replacement"
                replacement.write_bytes(b"replacement")
                replacement.chmod(0o600)
                replacement.replace(parent / "item")

    with pytest.raises(CommitUnknown):
        _state(root, fault_hook=hook).rename_exclusive_contained(
            "source/item", "destination/item", source_kind="regular", label="move",
        )
    assert moved


def test_private_creation_rollback_reports_inode_moved_elsewhere(tmp_path):
    root = tmp_path.resolve() / "state"
    parent = _state(root).ensure_directory("objects")

    def move_then_fail(event):
        if event == "item:written":
            (parent / "item").rename(parent / "moved")
            raise OSError(errno.EIO, "failure after moving created inode")

    with pytest.raises(CommitUnknown, match="rollback"):
        _state(root, fault_hook=move_then_fail).create_private_file_bytes(
            "objects/item", b"visible bytes", label="item",
        )
    assert (parent / "moved").read_bytes() == b"visible bytes"
