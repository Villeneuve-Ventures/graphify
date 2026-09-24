"""Held locks cannot authorize operations in a replacement state tree."""

import contextvars
import errno
import os
import shutil

import pytest

from graphify.workspace.persistence import DurableStateRoot, RuntimeCapabilities, StatePathError


@pytest.mark.parametrize("method,replacement", [
    ("lock", "root"), ("existing_lock", "root"), ("initialization_lock", "root"),
    ("lock", "lockfile"), ("existing_lock", "lockfile"),
])
@pytest.mark.parametrize("stage", ["acquired", "body"])
def test_replaced_binding_cannot_authorize_state_operations(tmp_path, method, replacement, stage):
    root = tmp_path.resolve() / "state"
    capabilities = RuntimeCapabilities.supported_test_fixture()
    state = DurableStateRoot(root, capabilities=capabilities)
    state.ensure_directory(".")
    state.create_private_file_bytes("registry.lock", b"", label="lock")
    state.create_private_file_bytes("record", b"original", label="record")
    replaced = False
    entered = False

    def replace_binding():
        nonlocal replaced
        if replacement == "root":
            detached = root.with_name("detached")
            root.rename(detached)
            shutil.copytree(detached, root)
        else:
            lock = root / "registry.lock"
            lock.rename(root / "detached.lock")
            lock.write_bytes(b"")
            lock.chmod(0o600)
        replaced = True

        # This independent logical caller can lock the replacement inode while
        # the original caller is still holding its detached flock.
        def second_caller():
            other = DurableStateRoot(root, capabilities=capabilities)
            with other.existing_lock("registry.lock", rank=10, name="registry", blocking=False):
                pass

        contextvars.Context().run(second_caller)

    def hook(event):
        if event == "lock:registry:acquired" and stage == "acquired":
            replace_binding()

    guarded = DurableStateRoot(root, capabilities=capabilities, fault_hook=hook)
    kwargs = {"rank": 10, "name": "registry"}
    lock = (guarded.initialization_lock(**kwargs) if method == "initialization_lock"
            else getattr(guarded, method)("registry.lock", **kwargs))
    with pytest.raises(StatePathError, match="binding changed"):
        with lock:
            entered = True
            if stage == "body":
                replace_binding()
            # A new facade for the same root must not escape the held binding.
            DurableStateRoot(root, capabilities=capabilities).atomic_replace_bytes(
                "record", b"unauthorized replacement", label="record",
            )
    assert replaced
    assert entered == (stage == "body")
    assert (root / "record").read_bytes() == b"original"
    assert not list(root.glob(".record.tmp-*"))

    # Guard state must be removed on failure so a fresh operation can proceed.
    with state.existing_lock("registry.lock", rank=10, name="registry", blocking=False):
        state.atomic_replace_bytes("record", b"fresh owner", label="record")
    assert (root / "record").read_bytes() == b"fresh owner"


def test_binding_failure_after_file_open_closes_unreturned_descriptor(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "state"
    state = DurableStateRoot(root, capabilities=RuntimeCapabilities.supported_test_fixture())
    state.ensure_directory(".")
    state.create_private_file_bytes("record", b"original", label="record")
    real_open = os.open
    captured = []

    def open_then_replace(path, *args, **kwargs):
        descriptor = real_open(path, *args, **kwargs)
        if path == "record":
            captured.append(descriptor)
            lock = root / "registry.lock"
            lock.rename(root / "detached.lock")
            lock.write_bytes(b"")
            lock.chmod(0o600)
        return descriptor

    with pytest.raises(StatePathError, match="binding changed"):
        with state.lock("registry.lock", rank=10, name="registry"):
            monkeypatch.setattr(os, "open", open_then_replace)
            state.read_existing_bytes("record")
    assert len(captured) == 1
    try:
        with pytest.raises(OSError) as caught:
            os.fstat(captured[0])
        assert caught.value.errno == errno.EBADF
    finally:
        # Keep a failing pre-fix run from leaking into other tests.
        try:
            os.close(captured[0])
        except OSError:
            pass
