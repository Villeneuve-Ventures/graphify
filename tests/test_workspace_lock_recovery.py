"""Lock instrumentation failures release OS locks and restore ordering state."""

import contextvars
import os

import pytest

from graphify.workspace.persistence import (
    DurableStateRoot, InjectedFault, LockOrderError, RuntimeCapabilities,
)


@pytest.mark.parametrize("method", ["lock", "initialization_lock", "existing_lock"])
@pytest.mark.parametrize("stage", ["acquired", "released"])
def test_lock_hook_failure_preserves_outer_lock_and_allows_retry(tmp_path, method, stage):
    import fcntl

    root = tmp_path.resolve() / "state"
    capabilities = RuntimeCapabilities.supported_test_fixture()
    state = DurableStateRoot(root, capabilities=capabilities)
    state.ensure_directory(".")
    state.create_private_file_bytes("inner.lock", b"", label="fixture")
    failure = InjectedFault("injected lock hook failure")
    events = []

    def hook(event):
        events.append(event)
        if event == f"lock:inner:{stage}":
            raise failure

    failing = DurableStateRoot(root, capabilities=capabilities, fault_hook=hook)

    def acquire(owner):
        kwargs = {"rank": 20, "name": "inner"}
        if method == "initialization_lock":
            return owner.initialization_lock(**kwargs)
        return getattr(owner, method)("inner.lock", **kwargs)

    def exercise():
        with state.lock("outer.lock", rank=10, name="outer"):
            entered = False
            with pytest.raises(InjectedFault) as caught:
                with acquire(failing):
                    entered = True
            assert caught.value is failure
            assert entered == (stage == "released")

            # A separate descriptor can acquire the actual flock immediately.
            locked_path = root if method == "initialization_lock" else root / "inner.lock"
            descriptor = os.open(locked_path, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

            with acquire(state):
                pass
            assert events == ["lock:inner:acquired", "lock:inner:released"]
            # Restoring the failed inner lock must not erase the outer lock.
            with pytest.raises(LockOrderError):
                with state.lock("lower.lock", rank=5, name="lower"):
                    pytest.fail("outer lock ordering was lost")
        with state.lock("lower.lock", rank=5, name="lower"):
            pass

    # Keep an unfixed implementation's leaked ContextVar out of other tests.
    contextvars.copy_context().run(exercise)
