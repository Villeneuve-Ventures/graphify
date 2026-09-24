"""Result consumption reports incomplete retained-marker cleanup."""

import errno
import hashlib
import os

import pytest

from graphify.workspace.persistence import (
    CommitUnknown, DurableStateRoot, PosixSyscalls, RuntimeCapabilities,
)


@pytest.mark.parametrize("stage", ["truncate", "fsync"])
def test_retained_cleanup_failure_after_live_consume_is_retryable(tmp_path, stage):
    root = tmp_path.resolve() / "state"
    state = DurableStateRoot(
        root, capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    state.ensure_directory("results")
    expected = b'{"result":"private payload"}'
    relative = "results/result.json"
    live = state.create_private_file_bytes(relative, expected, label="result")
    old = state.create_private_file_bytes("results/old.json", expected, label="result")
    details = old.stat()
    retained = old.with_name(
        f".result.json.consumed-{hashlib.sha256(expected).hexdigest()}-"
        f"{details.st_dev:x}-{details.st_ino:x}"
    )
    old.rename(retained)
    failure = OSError(errno.EIO, "injected retained cleanup failure")

    class FailRetained(PosixSyscalls):
        def truncate(self, descriptor, length):
            if stage == "truncate" and os.fstat(descriptor).st_ino == details.st_ino:
                raise failure
            return super().truncate(descriptor, length)

        def fsync(self, descriptor):
            if stage == "fsync" and os.fstat(descriptor).st_ino == details.st_ino:
                raise failure
            return super().fsync(descriptor)

    failing = DurableStateRoot(
        root, capabilities=RuntimeCapabilities.supported_test_fixture(),
        syscalls=FailRetained(),
    )
    with pytest.raises(CommitUnknown, match="retained cleanup") as caught:
        failing.consume_matching_bytes_and_sync(relative, expected, label="result")
    assert caught.value.__cause__ is failure
    assert not live.exists()
    assert retained.read_bytes() == (expected if stage == "truncate" else b"")

    assert state.consume_matching_bytes_and_sync(relative, expected, label="result")
    assert not retained.exists()
    archives = list((root / "results" / ".semantic-result-consumed").iterdir())
    assert len(archives) == 2
    assert all(path.read_bytes() == b"" for path in archives)
    assert not state.consume_matching_bytes_and_sync(relative, expected, label="result")
