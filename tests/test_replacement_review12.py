"""Malformed fragments and synthetic portability evidence use disposable inputs."""
import os
from types import SimpleNamespace

import pytest

from graphify import replacement_guard_issuers as issuers
from tests.test_canonical_replacement import case as case
from tests.test_replacement_guard_issuers import protected_policy as protected_policy


def test_lock_module_skips_if_fcntl_is_unavailable(monkeypatch):
    # Both __import__ and importlib.import_module refuse this sentinel, even if
    # an earlier test loaded fcntl. Monkeypatch restores the prior module entry.
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(pytest.skip.Exception) as error:
        runpy.run_path(str(Path(__file__).with_name("test_replacement_lock.py")))
    assert "fcntl" in str(error.value)


@pytest.fixture
def shared_ancestor_modes(tmp_path, monkeypatch):
    """Model unsafe host ancestor metadata without chmod or another host launch."""
    info = os.stat(tmp_path.parent)
    identity = info.st_dev, info.st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def metadata(info):
        if (info.st_dev, info.st_ino) != identity:
            return info
        return SimpleNamespace(**{name: (info.st_mode | 0o7022 if name == "st_mode" else getattr(info, name))
                                  for name in dir(info) if name.startswith("st_")})

    monkeypatch.setattr(os, "stat", lambda *a, **kw: metadata(real_stat(*a, **kw)))
    monkeypatch.setattr(os, "fstat", lambda fd: metadata(real_fstat(fd)))


@pytest.fixture
def policy_below_shared_ancestor(shared_ancestor_modes, request):
    # Resolve after raw host metadata is intercepted, before the policy model binds it.
    return request.getfixturevalue("protected_policy")


def test_synthetic_policy_admits_safe_modeled_shared_ancestors(policy_below_shared_ancestor, tmp_path):
    parent = tmp_path / "namespace"
    parent.mkdir()
    policy, _, _ = policy_below_shared_ancestor.install(parent)
    info = parent.stat()
    with issuers.issuer_snapshot() as snapshot:
        assert policy.mutator_uid in snapshot.trusted_issuers(parent, (info.st_dev, info.st_ino))


@pytest.mark.parametrize("permissions", [0o777, 0o1777, 0o2755, 0o4755])
def test_modeled_unsafe_policy_directories_still_refuse(protected_policy, permissions):
    protected_policy.mode(protected_policy.directory, permissions)
    with pytest.raises(issuers.IssuerPolicyError, match="unsafe issuer policy"):
        with issuers.issuer_snapshot():
            pytest.fail("an explicit unsafe directory model must not be normalized away")
