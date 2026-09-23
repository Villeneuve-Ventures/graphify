"""Simulated Win32 handle tests for ordinary unlink on non-Windows hosts."""

import ctypes
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import storage_guard as guard


@pytest.fixture
def windows_handles(monkeypatch):
    real_os = guard.os

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(real_os, name)

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    handles = {}
    calls = []
    next_handle = 100
    on_open = None
    pending_deletes = set()

    def create(name, access, share, _security, disposition, flags, _template):
        nonlocal next_handle
        calls.append(("open", Path(name), access, share, disposition, flags))
        if on_open is not None:
            on_open(Path(name), access)
        # Resolve the parent, but preserve the final directory entry itself.
        supplied = Path(name)
        actual = Path(real_os.path.realpath(supplied.parent)) / supplied.name
        actual.lstat()
        next_handle += 1
        handles[next_handle] = actual
        return next_handle

    def information(handle, out):
        entry = handles[handle].lstat()
        info = out._obj
        info.dwFileAttributes = (0x10 if stat.S_ISDIR(entry.st_mode) else 0)
        if stat.S_ISLNK(entry.st_mode):
            info.dwFileAttributes |= 0x400
        info.nNumberOfLinks = entry.st_nlink
        return 1

    def final_path(handle, buffer, size, _flags):
        name = str(handles[handle])
        if len(name) + 1 > size:
            return len(name) + 1
        buffer.value = name
        return len(name)

    def close(handle):
        calls.append(("close", handle))
        if handle in pending_deletes:
            handles[handle].unlink()
            pending_deletes.remove(handle)
        handles.pop(handle)
        return 1

    def disposition(handle, info_class, info, size):
        assert info_class == 21  # FileDispositionInfoEx
        assert info._obj.Flags == 3  # DELETE | POSIX_SEMANTICS
        assert size == 4
        calls.append(("delete", handle))
        pending_deletes.add(handle)
        return 1

    api = SimpleNamespace(
        CreateFileW=Function(create),
        GetFileInformationByHandle=Function(information),
        GetFinalPathNameByHandleW=Function(final_path),
        CloseHandle=Function(close),
        SetFileInformationByHandle=Function(disposition),
    )
    monkeypatch.setattr(guard, "os", WindowsOS())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: api, raising=False)

    def set_open(callback):
        nonlocal on_open
        on_open = callback

    return SimpleNamespace(api=api, calls=calls, handles=handles, set_on_open=set_open)


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_deletes_opened_file_and_holds_ancestors(tmp_path, windows_handles):
    target = tmp_path / "safe" / "note"
    target.parent.mkdir()
    target.write_text("ordinary")

    guard.ordinary_unlink(target)

    assert not target.exists()
    assert windows_handles.calls[-1][0] == "close"
    opens = [call for call in windows_handles.calls if call[0] == "open"]
    assert opens[0][1] == target
    assert opens[0][2] == 0x10080  # DELETE | FILE_READ_ATTRIBUTES
    assert all(call[3] == 0x3 and call[4] == 3 for call in opens)
    assert all(call[5] == 0x02200000 for call in opens)
    assert ("delete", 101) in windows_handles.calls
    closes = [call[1] for call in windows_handles.calls if call[0] == "close"]
    assert closes[0] == 101  # finish deletion before releasing ancestor pins
    assert windows_handles.handles == {}


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_refuses_unsupported_immediate_disposition(tmp_path, monkeypatch, windows_handles):
    target = tmp_path / "note"
    target.write_text("preserved")
    attempts = []

    def unsupported(handle, info_class, _info, _size):
        attempts.append(info_class)
        return 0

    windows_handles.api.SetFileInformationByHandle.callback = unsupported
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "unsupported"), raising=False)
    with pytest.raises(OSError, match="unsupported"):
        guard.ordinary_unlink(target)
    assert attempts == [21]  # never fall back to deferred or path-based deletion
    assert target.read_text() == "preserved"
    assert windows_handles.handles == {}


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_rejects_parent_switch_after_preflight(tmp_path, monkeypatch, windows_handles):
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "note").write_text("ordinary")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / guard.WORKSPACE_ROOT_MARKER).write_text("marker")
    protected = managed / "note"
    protected.write_text("protected")
    alias = tmp_path / "alias"
    alias.symlink_to(safe, target_is_directory=True)

    original = guard.require_ordinary_output
    switched = False

    def switch_after_admission(path):
        nonlocal switched
        original(path)
        if not switched:
            alias.unlink()
            alias.symlink_to(managed, target_is_directory=True)
            switched = True

    monkeypatch.setattr(guard, "require_ordinary_output", switch_after_admission)
    # Reproduce the old check-then-path-unlink behavior against this fixture.
    guard.require_ordinary_output(alias / "note")
    (alias / "note").unlink()
    assert not protected.exists()
    protected.write_text("protected")
    alias.unlink()
    alias.symlink_to(safe, target_is_directory=True)
    switched = False

    with pytest.raises(guard.ManagedWorkspaceOutputError):
        guard.ordinary_unlink(alias / "note")
    assert switched
    assert protected.read_text() == "protected"
    assert (safe / "note").read_text() == "ordinary"
    assert not any(call[0] == "delete" for call in windows_handles.calls)
    assert windows_handles.handles == {}


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_closes_handle_when_metadata_fails(tmp_path, windows_handles):
    target = tmp_path / "note"
    target.write_text("preserved")

    def fail_metadata(_handle, _out):
        raise OSError("metadata unavailable")

    windows_handles.api.GetFileInformationByHandle.callback = fail_metadata
    with pytest.raises(OSError, match="metadata unavailable"):
        guard.ordinary_unlink(target)
    assert target.read_text() == "preserved"
    assert windows_handles.calls == [
        ("open", target, 0x10080, 0x3, 3, 0x02200000),
        ("close", 101),
    ]
    assert windows_handles.handles == {}


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_preserves_missing_file_error(tmp_path, monkeypatch, windows_handles):
    windows_handles.api.CreateFileW.callback = lambda *_args: ctypes.c_void_p(-1).value
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2, raising=False)

    with pytest.raises(FileNotFoundError):
        guard.ordinary_unlink(tmp_path / "missing")
    assert windows_handles.handles == {}


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_refuses_changed_opened_path(tmp_path, windows_handles):
    target = tmp_path / "note"
    target.write_text("preserved")
    final_path = windows_handles.api.GetFinalPathNameByHandleW
    original = final_path.callback
    target_reads = 0

    def changed_path(handle, buffer, size, flags):
        nonlocal target_reads
        if handle == 101:
            target_reads += 1
            if target_reads == 2:
                changed = str(tmp_path / "other" / "note")
                buffer.value = changed
                return len(changed)
        return original(handle, buffer, size, flags)

    final_path.callback = changed_path
    with pytest.raises(guard.ManagedWorkspaceOutputError, match="binding changed"):
        guard.ordinary_unlink(target)
    assert target.read_text() == "preserved"
    assert not any(call[0] == "delete" for call in windows_handles.calls)
    assert windows_handles.handles == {}


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_removes_file_symlink_without_following_it(tmp_path, windows_handles):
    target = tmp_path / "target"
    target.write_text("preserved")
    link = tmp_path / "link"
    link.symlink_to(target)

    guard.ordinary_unlink(link)

    assert not link.is_symlink()
    assert target.read_text() == "preserved"


@pytest.mark.skipif(os.name == "nt", reason="simulated Win32 API on POSIX paths")
def test_windows_unlink_refuses_hardlink_and_directory(tmp_path, windows_handles):
    target = tmp_path / "target"
    target.write_text("preserved")
    hardlink = tmp_path / "link"
    os.link(target, hardlink)
    directory = tmp_path / "directory"
    directory.mkdir()

    for entry in (hardlink, directory):
        with pytest.raises(guard.ManagedWorkspaceOutputError):
            guard.ordinary_unlink(entry)
    assert target.read_text() == "preserved"
    assert hardlink.exists() and directory.is_dir()
    assert not any(call[0] == "delete" for call in windows_handles.calls)
