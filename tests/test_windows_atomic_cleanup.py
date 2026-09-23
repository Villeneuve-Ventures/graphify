"""Exercise temporary-file failure cleanup without requiring a Windows host."""

import errno
import os
import tempfile
from pathlib import Path

import pytest

from graphify import storage_guard as guard


@pytest.mark.parametrize("failure", ["write", "close"])
def test_windows_atomic_failure_removes_partial_temp(tmp_path, monkeypatch, failure):
    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    target = tmp_path / "cache.json"
    target.write_bytes(b"previous cache")
    original_tempfile = tempfile.NamedTemporaryFile
    created = []

    class FailingStream:
        def __init__(self, **kwargs):
            self.stream = original_tempfile(**kwargs)
            self.name = self.stream.name
            created.append(Path(self.name))

        def __enter__(self):
            return self

        def write(self, data):
            self.stream.write(data[:3])
            if failure == "write":
                raise OSError(errno.ENOSPC, "injected write failure")

        def __exit__(self, *args):
            self.stream.close()
            if failure == "close":
                raise OSError(errno.ENOSPC, "injected close failure")

    monkeypatch.setattr(guard, "os", WindowsOS())
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", FailingStream)
    # Handle-bound Windows deletion has separate tests; isolate cleanup scope.
    monkeypatch.setattr(guard, "ordinary_unlink", lambda path: Path(path).unlink())
    with pytest.raises(OSError, match=f"injected {failure} failure"):
        guard.ordinary_atomic_bytes(target, b"replacement cache")
    assert target.read_bytes() == b"previous cache"
    assert len(created) == 1
    assert not created[0].exists()
