"""Recovery intent cannot promote an impossible backup over valid authority."""

import json

import pytest

from graphify.workspace.persistence import DurableStateRoot, RuntimeCapabilities, StateCorrupt


def _fixture(tmp_path, current, previous, pending):
    state = DurableStateRoot(tmp_path.resolve() / "state",
                             capabilities=RuntimeCapabilities.supported_test_fixture())
    state.ensure_directory("records")
    for name, value in (("current", current), ("previous", previous), ("pending", pending)):
        if value is not None:
            payload = value if isinstance(value, bytes) else json.dumps(value).encode()
            state.write_once(f"records/{name}", payload)
    return state


def _recover(state, method):
    return getattr(state, method)(
        label="test", current="records/current", previous="records/previous",
        pending="records/pending", decoder=json.loads, revision=lambda value: value["revision"],
    )


def _snapshot(state):
    return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
            for p in (state.root / "records").iterdir()}


@pytest.mark.parametrize("method", ["project_record_recovery", "recover_record"])
@pytest.mark.parametrize("previous,pending", [
    ({"revision": 2}, None),
    ({"revision": 2}, {"revision": 2}),
    ({"revision": 1, "different": True}, {"revision": 2}),
    ({"revision": 0}, {"revision": 3}),
])
def test_recovery_rejects_impossible_order_without_mutation(tmp_path, method, previous, pending):
    state = _fixture(tmp_path, {"revision": 1}, previous, pending)
    before = _snapshot(state)
    with pytest.raises(StateCorrupt):
        _recover(state, method)
    assert _snapshot(state) == before


@pytest.mark.parametrize("current,pending,expected", [
    ({"revision": 1}, {"revision": 2}, 2),
    ({"revision": 1}, {"revision": 1}, 1),
    ({"revision": 1}, None, 1),
    (None, {"revision": 2}, 2),
    (b"corrupt", {"revision": 2}, 2),
])
def test_recovery_retains_valid_current_or_durable_pending(tmp_path, current, pending, expected):
    state = _fixture(tmp_path, current, {"revision": 0}, pending)
    assert _recover(state, "recover_record") == {"revision": expected}
    assert json.loads((state.root / "records/current").read_bytes()) == {"revision": expected}
    assert not (state.root / "records/pending").exists()


@pytest.mark.parametrize("method", ["project_record_recovery", "recover_record"])
@pytest.mark.parametrize("current", [None, b"corrupt"])
@pytest.mark.parametrize("previous", [{"revision": 99}, {"revision": 2, "different": True}])
def test_pending_recovery_rejects_contradictory_backup(tmp_path, method, current, previous):
    state = _fixture(tmp_path, current, previous, {"revision": 2})
    before = _snapshot(state)
    with pytest.raises(StateCorrupt):
        _recover(state, method)
    assert _snapshot(state) == before
