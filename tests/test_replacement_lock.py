"""Coordinator initialization recovery keeps the original native safety boundary."""
import errno
import fcntl
import os
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case  # noqa: F401


def _lock_path(request):
    journal = Path(request["journal"])
    return journal.parent / (".replacement-lock-" + replacement._sha(os.fsencode(journal.name)))


def _assert_closed(fd):
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


def test_coordinator_reopens_peer_after_create_enoent(case, monkeypatch):
    request, _ = case
    path = _lock_path(request)
    original_open = os.open
    calls, opened = [], []

    def raced_open(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            calls.append(flags)
            fd = original_open(name, flags, mode, dir_fd=dir_fd)
            if flags & os.O_CREAT:
                os.close(fd)  # A peer has created the name before this caller sees ENOENT.
                raise FileNotFoundError(errno.ENOENT, "concurrent first creation", name)
            opened.append(fd)
            return fd
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)
    with replacement._coordinator_lock(request):
        assert path.read_bytes() == ("replacement-lock-v1:" + replacement._sha(os.fsencode(Path(request["journal"]).name))).encode()
    assert [bool(flags & os.O_CREAT) for flags in calls] == [True, False]
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in calls)
    assert len(opened) == 1
    _assert_closed(opened[0])
    assert not Path(request["journal"]).exists()


def test_coordinator_missing_peer_is_bounded_without_recreating(case, monkeypatch):
    request, _ = case
    path = _lock_path(request)
    original_open = os.open
    clock = [0.0]
    calls = []

    def missing(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            calls.append(flags)
            raise FileNotFoundError(errno.ENOENT, "no peer lock", name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", missing)
    monkeypatch.setattr(replacement.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(replacement.time, "sleep", lambda _seconds: clock.__setitem__(0, clock[0] + 10))
    with pytest.raises(tx.PendingTransactionError, match="operation lock deadline"):
        with replacement._coordinator_lock(request):
            pytest.fail("missing lock must not admit an owner")
    assert clock[0] == 30
    assert len(calls) <= 4
    assert calls[0] & os.O_CREAT
    assert all(not flags & os.O_CREAT for flags in calls[1:])
    assert not path.exists()
    assert not Path(request["journal"]).exists()


def test_coordinator_reopen_and_flock_share_deadline_and_close_fd(case, monkeypatch):
    request, _ = case
    path = _lock_path(request)
    original_open, original_flock = os.open, fcntl.flock
    clock = [0.0]
    opened, attempts = [], []

    def raced_open(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            fd = original_open(name, flags, mode, dir_fd=dir_fd)
            if flags & os.O_CREAT:
                os.close(fd)
                raise FileNotFoundError(errno.ENOENT, "peer", name)
            clock[0] = 29.0
            opened.append(fd)
            return fd
        return original_open(name, flags, mode, dir_fd=dir_fd)

    def busy(fd, operation):
        if fd in opened and operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            attempts.append(fd)
            raise BlockingIOError(errno.EWOULDBLOCK, "busy")
        return original_flock(fd, operation)

    monkeypatch.setattr(os, "open", raced_open)
    monkeypatch.setattr(fcntl, "flock", busy)
    monkeypatch.setattr(replacement.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(replacement.time, "sleep", lambda _seconds: clock.__setitem__(0, clock[0] + 1))
    with pytest.raises(tx.PendingTransactionError, match="operation lock deadline"):
        with replacement._coordinator_lock(request):
            pytest.fail("the recovery must not restart the acquisition deadline")
    assert clock[0] == 30
    assert len(attempts) == 1
    assert len(opened) == 1
    _assert_closed(opened[0])


@pytest.mark.parametrize("retarget_at", ["initial", "reopen"])
def test_coordinator_parent_retarget_refuses_and_closes_fd(case, monkeypatch, retarget_at):
    request, _ = case
    path = _lock_path(request)
    original_open, original_flock = os.open, fcntl.flock
    opened, calls = [], []

    def retarget():
        path.parent.rename(path.parent.with_name("retained-control"))
        path.parent.mkdir()

    def raced_open(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            calls.append(flags)
            fd = original_open(name, flags, mode, dir_fd=dir_fd)
            if flags & os.O_CREAT:
                os.close(fd)
                if retarget_at == "initial":
                    retarget()
                raise FileNotFoundError(errno.ENOENT, "peer", name)
            opened.append(fd)
            retarget()
            return fd
        return original_open(name, flags, mode, dir_fd=dir_fd)

    def unexpected_flock(fd, operation):
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            pytest.fail("changed control parent must be rejected before lock acquisition")
        return original_flock(fd, operation)

    monkeypatch.setattr(os, "open", raced_open)
    monkeypatch.setattr(fcntl, "flock", unexpected_flock)
    with pytest.raises(tx.PendingTransactionError, match="parent identity changed|directory identity changed"):
        with replacement._coordinator_lock(request):
            pytest.fail("retargeted parent must refuse")
    assert len(calls) == (1 if retarget_at == "initial" else 2)
    for fd in opened:
        _assert_closed(fd)
    assert not Path(request["journal"]).exists()


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "payload"])
def test_coordinator_reopen_preserves_unsafe_peer_checks(case, monkeypatch, damage):
    request, _ = case
    path = _lock_path(request)
    original_open = os.open
    opened, calls = [], []
    peer = path.parent / "peer"
    peer.write_bytes(b"foreign-lock" if damage == "payload" else b"")
    original_graph = (Path(request["output"]) / "graph.json").read_bytes()

    def raced_open(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            calls.append(flags)
            if flags & os.O_CREAT:
                if damage == "symlink":
                    path.symlink_to(peer)
                elif damage == "hardlink":
                    os.link(peer, path)
                else:
                    path.write_bytes(peer.read_bytes())
                raise FileNotFoundError(errno.ENOENT, "peer", name)
            fd = original_open(name, flags, mode, dir_fd=dir_fd)
            opened.append(fd)
            return fd
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)
    if damage == "symlink":
        with pytest.raises(OSError) as error:
            with replacement._coordinator_lock(request):
                pytest.fail("symlink peer must refuse")
        assert error.value.errno == errno.ELOOP
    else:
        reason = "operation lock changed" if damage == "hardlink" else "operation lock binding changed"
        with pytest.raises(tx.PendingTransactionError, match=reason):
            with replacement._coordinator_lock(request):
                pytest.fail("unsafe peer must refuse")
    assert [bool(flags & os.O_CREAT) for flags in calls] == [True, False]
    for fd in opened:
        _assert_closed(fd)
    assert peer.read_bytes() == (b"foreign-lock" if damage == "payload" else b"")
    assert (Path(request["output"]) / "graph.json").read_bytes() == original_graph
    assert not Path(request["journal"]).exists()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
@pytest.mark.parametrize("stage", ["create", "reopen"])
def test_coordinator_other_open_errors_are_not_retried(case, monkeypatch, error_number, stage):
    request, _ = case
    path = _lock_path(request)
    original_open = os.open
    calls = []

    def denied(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            calls.append(flags)
            if stage == "reopen" and flags & os.O_CREAT:
                raise FileNotFoundError(errno.ENOENT, "peer", name)
            raise OSError(error_number, "other native failure", name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(OSError) as error:
        with replacement._coordinator_lock(request):
            pytest.fail("non-ENOENT failures must propagate")
    assert error.value.errno == error_number
    assert len(calls) == (1 if stage == "create" else 2)
    assert not path.exists()


def test_coordinator_initial_open_uses_overall_deadline_and_closes_fd(case, monkeypatch):
    request, _ = case
    path = _lock_path(request)
    original_open = os.open
    clock = [0.0]
    opened = []

    def slow_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        if str(name) == path.name and dir_fd is not None:
            clock[0] = 30.0
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", slow_open)
    monkeypatch.setattr(replacement.time, "monotonic", lambda: clock[0])
    with pytest.raises(tx.PendingTransactionError, match="operation lock deadline"):
        with replacement._coordinator_lock(request):
            pytest.fail("first open must not restart the acquisition deadline")
    assert len(opened) == 1
    _assert_closed(opened[0])


def test_coordinator_late_successful_flock_closes_fd(case, monkeypatch):
    request, _ = case
    original_flock = fcntl.flock
    clock = [0.0]
    opened = []

    def late_flock(fd, operation):
        result = original_flock(fd, operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            opened.append(fd)
            clock[0] = 30.0
        return result

    monkeypatch.setattr(fcntl, "flock", late_flock)
    monkeypatch.setattr(replacement.time, "monotonic", lambda: clock[0])
    with pytest.raises(tx.PendingTransactionError, match="operation lock deadline"):
        with replacement._coordinator_lock(request):
            pytest.fail("acquisition must finish within the original deadline")
    assert len(opened) == 1
    _assert_closed(opened[0])
    assert _lock_path(request).read_bytes() == b""
