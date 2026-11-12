"""Disposable coordinator-lock admission checks with synthetic ACL results."""
import errno
import os
import stat

import pytest

from graphify import canonical_replacement as replacement
from graphify import hook_installation
from graphify import transaction as tx
from tests.test_canonical_replacement import case  # noqa: F401
from tests.test_replacement_lock import _lock_path


@pytest.fixture
def acl_state(monkeypatch):
    state = {"safe": True, "checked": []}

    def safe(fd):
        state["checked"].append(fd)
        return state["safe"]

    monkeypatch.setattr(hook_installation, "_acl_safe", safe)
    return state


def _payload(request):
    return ("replacement-lock-v1:" + replacement._sha(os.fsencode(os.path.basename(request["journal"])))).encode()


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
@pytest.mark.parametrize("initialized", [False, True])
def test_existing_writable_lock_refuses_without_writing(case, acl_state, mode, initialized):
    request, _ = case
    path = _lock_path(request)
    before = _payload(request) if initialized else b""
    path.write_bytes(before)
    path.chmod(mode)
    with pytest.raises(tx.PendingTransactionError, match="operation lock"):
        with replacement._coordinator_lock(request):
            pytest.fail("group/other writable lock must not admit coordinator work")
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("initialized", [False, True])
def test_existing_unsafe_acl_refuses_without_writing(case, acl_state, initialized):
    request, _ = case
    path = _lock_path(request)
    before = _payload(request) if initialized else b""
    path.write_bytes(before)
    path.chmod(0o600)
    acl_state["safe"] = False
    with pytest.raises(tx.PendingTransactionError, match="operation lock"):
        with replacement._coordinator_lock(request):
            pytest.fail("unsafe ACL must not admit coordinator work")
    assert path.read_bytes() == before
    assert acl_state["checked"]


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644])
def test_existing_safe_lock_preserves_mode_and_payload(case, acl_state, mode):
    request, _ = case
    path = _lock_path(request)
    path.write_bytes(_payload(request))
    path.chmod(mode)
    with replacement._coordinator_lock(request):
        assert path.read_bytes() == _payload(request)
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert acl_state["checked"]


@pytest.mark.parametrize("when", ["binding_read", "yielded"])
@pytest.mark.parametrize("damage", ["mode", "acl"])
def test_lock_safety_revalidated_before_and_after_work(case, acl_state, monkeypatch, when, damage):
    request, _ = case
    path = _lock_path(request)
    path.write_bytes(_payload(request))
    path.chmod(0o600)
    real_read = replacement._read
    changed = []

    def change():
        changed.append(True)
        if damage == "mode":
            path.chmod(0o666)
        else:
            acl_state["safe"] = False

    def read(*args, **kwargs):
        data = real_read(*args, **kwargs)
        if os.fspath(args[0]) == path.name and when == "binding_read":
            change()
        return data

    monkeypatch.setattr(replacement, "_read", read)
    with pytest.raises(tx.PendingTransactionError, match="operation lock|lock retargeted"):
        with replacement._coordinator_lock(request):
            if when == "binding_read":
                pytest.fail("unsafe lock must refuse before coordinator work")
            change()
    assert changed == [True]


@pytest.mark.parametrize("damage", ["mode", "acl"])
def test_enoent_peer_reopen_rejects_unsafe_lock(case, acl_state, monkeypatch, damage):
    request, _ = case
    path = _lock_path(request)
    real_open = os.open
    calls, opened = [], []

    def raced_open(name, flags, mode=0o777, *, dir_fd=None):
        if str(name) == path.name and dir_fd is not None:
            calls.append(flags)
            if flags & os.O_CREAT:
                path.write_bytes(_payload(request))
                path.chmod(0o666 if damage == "mode" else 0o600)
                acl_state["safe"] = damage != "acl"
                raise FileNotFoundError(errno.ENOENT, "peer creation", name)
            fd = real_open(name, flags, mode, dir_fd=dir_fd)
            opened.append(fd)
            return fd
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)
    with pytest.raises(tx.PendingTransactionError, match="operation lock"):
        with replacement._coordinator_lock(request):
            pytest.fail("unsafe peer lock must not admit work")
    admissions = [flags for flags in calls if flags & os.O_RDWR]
    assert [bool(flags & os.O_CREAT) for flags in admissions] == [True, False]
    assert path.read_bytes() == _payload(request)
    for fd in opened:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


def test_acl_inspection_error_retains_lock_and_closes_descriptor(case, acl_state, monkeypatch):
    request, _ = case
    path = _lock_path(request)
    path.write_bytes(b"")
    path.chmod(0o600)
    checked = []

    def unavailable(fd):
        checked.append(fd)
        raise OSError(errno.EIO, "synthetic ACL inspection failure")

    monkeypatch.setattr(hook_installation, "_acl_safe", unavailable)
    with pytest.raises(OSError, match="synthetic ACL inspection failure"):
        with replacement._coordinator_lock(request):
            pytest.fail("uncertain ACL evidence must not admit coordinator work")
    assert path.read_bytes() == b""
    assert checked
    for fd in checked:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF
