"""Immutable retries complete durability for the retained visible inode."""

import errno
import os
import stat

import pytest

from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    PosixSyscalls,
    RuntimeCapabilities,
    StateCorrupt,
)


def _state(root, syscalls=None):
    return DurableStateRoot(
        root,
        capabilities=RuntimeCapabilities.supported_test_fixture(),
        syscalls=syscalls,
    )


def _install(state, method):
    if method == "install_once_bytes":
        return state.install_once_bytes("objects/item", b"immutable", label="item")
    return state.write_once("objects/item", b"immutable")


@pytest.mark.parametrize("method", ["install_once_bytes", "write_once"])
@pytest.mark.parametrize("retry_failure", [None, "file", "parent"])
def test_retry_completes_retained_inode_durability(tmp_path, method, retry_failure):
    root = tmp_path.resolve() / "state"
    state = _state(root)
    parent = state.ensure_directory("objects")
    parent_inode = parent.stat().st_ino
    failure = OSError(errno.EIO, "injected sync failure")

    class FailPublication(PosixSyscalls):
        def fsync(self, descriptor):
            if os.fstat(descriptor).st_ino == parent_inode:
                raise failure
            super().fsync(descriptor)

    expected_error = CommitUnknown if method == "install_once_bytes" else OSError
    with pytest.raises(expected_error):
        _install(_state(root, FailPublication()), method)
    path = parent / "item"
    retained = path.stat()
    assert path.read_bytes() == b"immutable"
    events = []

    class RetrySync(PosixSyscalls):
        def fsync(self, descriptor):
            details = os.fstat(descriptor)
            kind = "file" if stat.S_ISREG(details.st_mode) else "parent"
            events.append(kind)
            assert details.st_ino == (retained.st_ino if kind == "file" else parent_inode)
            if kind == retry_failure:
                raise failure
            super().fsync(descriptor)

    retry = _state(root, RetrySync())
    if retry_failure:
        with pytest.raises(expected_error) as caught:
            _install(retry, method)
        if method == "install_once_bytes":
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
        assert events == (["file"] if retry_failure == "file" else ["file", "parent"])
    else:
        assert _install(retry, method) == path
        assert events == ["file", "parent"]
    assert path.stat().st_ino == retained.st_ino
    assert path.read_bytes() == b"immutable"
    assert _install(state, method) == path
    assert path.stat().st_ino == retained.st_ino


@pytest.mark.parametrize("method", ["install_once_bytes", "write_once"])
@pytest.mark.parametrize("stage", ["file", "parent"])
def test_existing_retry_rejects_rebinding_during_sync(tmp_path, method, stage):
    root = tmp_path.resolve() / "state"
    state = _state(root)
    path = _install(state, method)
    replacement = path.with_name("replacement")
    replacement.write_bytes(b"immutable")
    replacement.chmod(0o600)

    class Rebind(PosixSyscalls):
        def fsync(self, descriptor):
            kind = "file" if stat.S_ISREG(os.fstat(descriptor).st_mode) else "parent"
            super().fsync(descriptor)
            if kind == stage:
                replacement.replace(path)

    with pytest.raises(StateCorrupt, match="changed"):
        _install(_state(root, Rebind()), method)


@pytest.mark.parametrize("method", ["install_once_bytes", "write_once"])
def test_conflicting_existing_bytes_are_not_acknowledged(tmp_path, method):
    root = tmp_path.resolve() / "state"
    state = _state(root)
    path = state.write_once("objects/item", b"different")
    retained = path.stat()

    class NoSync(PosixSyscalls):
        def fsync(self, descriptor):
            pytest.fail("conflicting bytes must fail before durability acknowledgement")

    with pytest.raises(StateCorrupt, match="conflicts"):
        _install(_state(root, NoSync()), method)
    assert path.read_bytes() == b"different"
    assert path.stat().st_ino == retained.st_ino
