"""Live source verification cannot retain registry locks on a stalled Git child."""

import subprocess
import sys
import time

import pytest

from graphify.workspace import identity, registry
from graphify.workspace.identity import SourceAmbiguousError, SourceDiscoveryTimeout
from graphify.workspace.persistence import RuntimeCapabilities
from graphify.workspace.registry import RegistryStore
from tests.test_workspace_registry_review import (
    RecordingLeases, activate, authorization, registry_sources,  # noqa: F401
)
from tests.test_workspace_source_discovery import source_repository  # noqa: F401


@pytest.mark.parametrize("operation,fail_on", [
    ("enroll", 1), ("adopt", 1), ("rebind", 1), ("rotate", 1),
    ("activate", 1), ("activate", 2),
])
def test_stalled_live_verification_releases_authority(
    registry_sources, tmp_path, monkeypatch, operation, fail_on,
):
    store, (first, second, _) = registry_sources
    if operation == "enroll":
        store = RegistryStore(tmp_path.resolve() / "new-state",
                              capabilities=RuntimeCapabilities.supported_test_fixture())
    elif operation == "activate":
        store.adopt(second, authorization("ADOPT"))
    before = None if operation == "enroll" else store.load().canonical
    leases = RecordingLeases(store)
    children = []
    deadlines = []
    original_discover = registry.discover_source
    original_popen = subprocess.Popen

    def stalled_git(arguments, **kwargs):
        # Use a real pipe-holding child; the production timeout must kill/reap it.
        child = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    def verify(path, *, deadline_ns=None):
        # Fail promptly on the pre-fix implementation rather than hang the test.
        assert deadline_ns is not None, "live discovery has no deadline"
        remaining = deadline_ns - time.monotonic_ns()
        assert 0 < remaining <= registry.LIVE_SOURCE_VERIFY_TIMEOUT_NS
        deadlines.append(deadline_ns)
        if len(deadlines) != fail_on:
            return second
        with monkeypatch.context() as child_patch:
            child_patch.setattr(identity.subprocess, "Popen", stalled_git)
            return original_discover(path, deadline_ns=deadline_ns)

    monkeypatch.setattr(registry, "LIVE_SOURCE_VERIFY_TIMEOUT_NS", 150_000_000, raising=False)
    monkeypatch.setattr(registry, "discover_source", verify)
    try:
        with pytest.raises(SourceAmbiguousError, match="unavailable") as caught:
            if operation == "enroll":
                store.enroll(first, authorization("ENROLL"))
            elif operation == "adopt":
                store.adopt(second, authorization("ADOPT"))
            elif operation == "rebind":
                store.rebind(first, authorization("REBIND"))
            elif operation == "rotate":
                store.rotate_enrollment_evidence(first, authorization("ROTATE"))
            else:
                activate(store, second, leases)
        assert isinstance(caught.value.__cause__, SourceDiscoveryTimeout)
        assert len(deadlines) == fail_on
        assert children and all(child.poll() is not None for child in children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    if before is None:
        assert not (store.state.root / store.CURRENT).exists()
    else:
        assert store.load().canonical == before
    assert not (store.state.root / store.PENDING).exists()
    expected_grants = int(operation == "activate" and fail_on == 2)
    assert len(leases.acquired) == len(leases.released) == expected_grants
    with store.state.existing_lock(store.LOCK, rank=10, name="probe", blocking=False):
        pass
