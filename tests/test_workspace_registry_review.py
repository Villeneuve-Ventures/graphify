"""Registry identity retention, bounded reads, and activation failure regressions."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from graphify.workspace.identity import (
    IdentityAction, OperatorAuthorization, UUIDCollisionError, discover_source,
)
from graphify.workspace.persistence import CommitUnknown, RuntimeCapabilities, StateCorrupt
from graphify.workspace.registry import RegistryStore, SourceAlreadyActive
from tests.test_workspace_source_discovery import _git, source_repository  # noqa: F401


def authorization(action):
    return OperatorAuthorization(IdentityAction(action), "fixture", "review regression",
                                 "2026-09-23T00:00:00Z", action.lower())


@pytest.fixture
def registry_sources(source_repository, tmp_path):
    _git(source_repository, "remote", "add", "origin", "https://example.test/repo.git")
    sources = [discover_source(source_repository)]
    for name in ("second", "third"):
        clone = tmp_path.resolve() / name
        _git(tmp_path, "clone", str(source_repository), str(clone))
        _git(clone, "remote", "set-url", "origin", "https://example.test/repo.git")
        (clone / ".graphify").mkdir()
        (clone / ".graphify/workspace.toml").write_bytes(
            (source_repository / ".graphify/workspace.toml").read_bytes()
        )
        sources.append(discover_source(clone))
    store = RegistryStore(tmp_path.resolve() / "state",
                          capabilities=RuntimeCapabilities.supported_test_fixture())
    store.enroll(sources[0], authorization("ENROLL"))
    return store, sources


class RecordingLeases:
    """Only the registry-to-lease interface; the lease implementation is a later layer."""
    def __init__(self, registry):
        self.registry = registry
        self.acquired = []
        self.released = []
        self.grant = SimpleNamespace(operation_epoch=2,
                                    lease=SimpleNamespace(to_dict=lambda: {"fence_token": 2}))

    def _acquire_under_registry_lock(self, *args, **kwargs):
        self.acquired.append((args, kwargs))
        return self.grant

    def _release_under_registry_lock(self, grant, document, *, validate_active):
        assert grant is self.grant
        assert validate_active is False
        self.released.append(document)


def activate(store, source, leases):
    entry = store.load().to_dict()
    return store.activate_source(
        source, authorization("ACTIVATE"), leases=leases, owner=object(),
        expected_registry_revision=entry["revision"],
        expected_active_source_revision=entry["workspaces"][0]["active_source_revision"],
        expected_operation_epoch=1, expected_migration_epoch=0,
        acquired_at=datetime(2026, 9, 23, tzinfo=timezone.utc), monotonic_ns=1, ttl_ns=100,
    )


def test_prior_adopted_inode_cannot_enroll_under_another_uuid(registry_sources):
    store, (first, second, third) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    store.adopt(third, authorization("ADOPT"))
    moved = second.root.with_name("moved-second")
    second.root.rename(moved)
    config = moved / ".graphify/workspace.toml"
    config.write_text(config.read_text().replace(first.repo_uuid,
                                               "550e8400-e29b-41d4-a716-446655440001"))
    changed = discover_source(moved)
    assert changed.git_common_inode == second.git_common_inode
    before = store.load().canonical
    with pytest.raises(UUIDCollisionError, match="identity is already enrolled"):
        store.enroll(changed, authorization("ENROLL"))
    assert store.load().canonical == before


def test_prior_activated_inode_remains_bound_after_source_switch(registry_sources):
    import shutil

    store, (first, second, _) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    original = second.root.with_name("original-second")
    second.root.rename(original)
    shutil.copytree(original, second.root)
    replacement = discover_source(second.root)
    assert replacement.registry_source == second.registry_source
    assert replacement.git_common_inode != second.git_common_inode
    store.rebind(replacement, authorization("REBIND"))
    activate(store, replacement, RecordingLeases(store))
    assert store.resolve_active_source(first.repo_uuid) == replacement
    activate(store, first, RecordingLeases(store))
    moved = second.root.with_name("moved-replacement")
    second.root.rename(moved)
    config = moved / ".graphify/workspace.toml"
    config.write_text(config.read_text().replace(first.repo_uuid,
                                               "550e8400-e29b-41d4-a716-446655440001"))
    changed = discover_source(moved)
    assert changed.git_common_inode == replacement.git_common_inode
    before = store.load().canonical
    with pytest.raises(UUIDCollisionError, match="identity is already enrolled"):
        store.enroll(changed, authorization("ENROLL"))
    assert store.load().canonical == before


def test_default_same_source_activation_refuses_before_lease_mutation(registry_sources):
    store, (first, _, _) = registry_sources
    before = store.load().canonical
    leases = RecordingLeases(store)
    with pytest.raises(SourceAlreadyActive):
        activate(store, first, leases)
    assert leases.acquired == []
    assert store.load().canonical == before


@pytest.mark.parametrize("failure_stage", ["evidence", "uncertain_evidence", "commit", "uncertain_commit"])
def test_activation_releases_only_definitely_precommit_failures(
    registry_sources, monkeypatch, failure_stage,
):
    store, (_, second, _) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    before = store.load().canonical
    leases = RecordingLeases(store)
    failure = CommitUnknown("uncertain fixture") if failure_stage.startswith("uncertain_") else OSError("fixture")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, "_authorized_evidence" if failure_stage in {"evidence", "uncertain_evidence"} else "_commit_locked", fail)
    with pytest.raises(type(failure)):
        activate(store, second, leases)
    assert len(leases.acquired) == 1
    assert len(leases.released) == (0 if failure_stage == "uncertain_commit" else 1)
    if leases.released:
        assert leases.released[0].canonical == before
    assert store.load().canonical == before


@pytest.mark.parametrize("recover", [False, True])
def test_registry_load_passes_fixed_byte_limit(registry_sources, monkeypatch, recover):
    store, _ = registry_sources
    method = "recover_record" if recover else "read_stable_record"
    original = getattr(store.state, method)
    seen = []

    def bounded(**kwargs):
        seen.append(kwargs.get("max_bytes"))
        return original(**kwargs)

    monkeypatch.setattr(store.state, method, bounded)
    with store.exclusive_lock():
        store._load_locked(recover=recover)
    assert len(seen) == 1
    assert isinstance(seen[0], int) and 0 < seen[0] <= 16 * 1024 * 1024


def test_identity_inventory_survives_rotation_and_activation(registry_sources):
    store, (_, second, third) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    store.adopt(third, authorization("ADOPT"))
    store.rotate_enrollment_evidence(second, authorization("ROTATE"))
    leases = RecordingLeases(store)
    result = activate(store, third, leases)
    assert result.registry.to_dict()["workspaces"][0]["active_source"] == third.registry_source
    assert len(leases.released) == 1
    with store.read_only_snapshot() as snapshot:
        assert snapshot.canonical == result.registry.canonical
    proofs = [store.read_evidence(digest) for digest in
              store._identity_evidence_digests(snapshot.to_dict()["workspaces"][0])]
    assert {proof["git_common_inode"] for proof in proofs} == {
        item.git_common_inode for item in registry_sources[1]
    }


@pytest.mark.parametrize("damage", ["missing", "wrong_uuid", "duplicate", "oversized", "nested"])
def test_identity_inventory_damage_fails_closed(registry_sources, damage):
    from graphify.workspace.lifecycle_contracts import Registry, canonical_json_bytes
    from graphify.workspace.registry import IDENTITY_EVIDENCE_MAX_RECORDS

    store, (_, second, third) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    document = store.adopt(third, authorization("ADOPT")).to_dict()
    enrollment = document["workspaces"][0]["uuid_enrollment"]
    current = store.read_evidence(enrollment["current_evidence_sha256"])
    inventory = current["bound_identity_evidence_sha256"]
    if damage == "missing":
        del current["bound_identity_evidence_sha256"]
    elif damage == "duplicate":
        current["bound_identity_evidence_sha256"] = inventory + inventory
    elif damage == "oversized":
        current["bound_identity_evidence_sha256"] = ["0" * 64] * (IDENTITY_EVIDENCE_MAX_RECORDS + 1)
    elif damage == "nested":
        current["bound_identity_evidence_sha256"] = [inventory]
    else:
        proof = store.read_evidence(inventory[0])
        proof["repo_uuid"] = "550e8400-e29b-41d4-a716-446655440001"
        current["bound_identity_evidence_sha256"] = sorted([
            store._persist_evidence(proof), *inventory[1:]
        ])
    enrollment["current_evidence_sha256"] = store._persist_evidence(current)
    (store.state.root / store.CURRENT).write_bytes(canonical_json_bytes(document))
    # Historical v2 records with incomplete proofs must fail, not discover live aliases.
    with pytest.raises(StateCorrupt):
        with store.read_only_snapshot():
            pass
    assert Registry.from_mapping(document).to_dict() == document


@pytest.mark.parametrize("recover,name", [
    (False, "registry.json"), (False, "registry.previous.json"),
    (True, "registry.json"), (True, "registry.previous.json"),
    (True, "registry.pending.json"),
])
def test_oversized_registry_candidates_are_never_read(registry_sources, monkeypatch, recover, name):
    import os
    from graphify.workspace.registry import REGISTRY_MAX_BYTES

    store, _ = registry_sources
    path = store.state.root / name
    with path.open("wb") as handle:
        handle.truncate(REGISTRY_MAX_BYTES + 1)
    path.chmod(0o600)
    target = path.stat()
    original_read = os.read
    target_reads = []

    def read(descriptor, size):
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) == (target.st_dev, target.st_ino):
            target_reads.append(size)
            raise AssertionError("oversized registry reached byte read")
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", read)
    with store.exclusive_lock():
        # Recovery can ignore corrupt previous state when current is authoritative.
        if recover and name == store.PREVIOUS:
            assert store._load_locked(recover=True) is not None
        else:
            with pytest.raises(StateCorrupt):
                store._load_locked(recover=recover)
    assert target_reads == []


def test_resolution_rejects_activation_during_discovery(registry_sources, monkeypatch):
    from graphify.workspace import registry
    from graphify.workspace.identity import SourceAmbiguousError

    store, (first, second, _) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    original = registry.discover_source

    def discover_and_activate(path, **kwargs):
        source = original(path, **kwargs)
        if path == first.root:
            activate(store, second, RecordingLeases(store))
        return source

    monkeypatch.setattr(registry, "discover_source", discover_and_activate)
    with pytest.raises(SourceAmbiguousError, match="changed"):
        store.resolve_active_source(first.repo_uuid)
    assert store.load().to_dict()["workspaces"][0]["active_source"] == second.registry_source


def test_rebound_replacement_at_same_path_can_activate(registry_sources):
    import shutil

    store, (first, _, _) = registry_sources
    moved = first.root.with_name("original-first")
    first.root.rename(moved)
    shutil.copytree(moved, first.root)
    replacement = discover_source(first.root)
    store.rebind(replacement, authorization("REBIND"))
    leases = RecordingLeases(store)
    result = activate(store, replacement, leases)
    assert result.registry.to_dict()["workspaces"][0]["active_source_revision"] == 2
    assert store.resolve_active_source(first.repo_uuid) == replacement
    assert len(leases.acquired) == len(leases.released) == 1
    with pytest.raises(SourceAlreadyActive):
        activate(store, replacement, leases)
    assert len(leases.acquired) == 1


@pytest.mark.parametrize("name", ["workspace.json", "workspace.previous.json", "workspace.pending.json"])
def test_oversized_orphan_workspace_candidates_are_not_read_or_changed(
    registry_sources, monkeypatch, name,
):
    import os

    store, (first, _, _) = registry_sources
    directory = store.state.root / "workspaces" / first.repo_uuid
    (directory / "workspace.json").unlink()
    path = directory / name
    with path.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)
    path.chmod(0o600)
    before = {item.name: item.read_bytes() for item in directory.iterdir()}
    target = path.stat()
    original = os.read
    reads = []

    def read(descriptor, size):
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (target.st_dev, target.st_ino):
            reads.append(size)
            raise AssertionError("oversized workspace state reached byte read")
        return original(descriptor, size)

    monkeypatch.setattr(os, "read", read)
    with store.exclusive_lock():
        with pytest.raises(StateCorrupt):
            store._initialize_workspace_state_locked(first.repo_uuid)
    assert reads == []
    assert {item.name: item.read_bytes() for item in directory.iterdir()} == before


@pytest.mark.parametrize("stale", [False, True])
def test_activation_requires_live_authorized_identity_before_lease(registry_sources, stale):
    import shutil
    from graphify.workspace.identity import SourceAmbiguousError

    store, (first, _, _) = registry_sources
    moved = first.root.with_name("original-first")
    first.root.rename(moved)
    shutil.copytree(moved, first.root)
    replacement = discover_source(first.root)
    if stale:
        store.rebind(replacement, authorization("REBIND"))
        activate(store, replacement, RecordingLeases(store))
    before = store.load().canonical
    leases = RecordingLeases(store)
    with pytest.raises(SourceAmbiguousError):
        activate(store, first if stale else replacement, leases)
    assert leases.acquired == []
    assert store.load().canonical == before


def test_rotation_cannot_authorize_replacement_activation(registry_sources):
    import shutil
    from graphify.workspace.identity import SourceAmbiguousError

    store, (first, _, _) = registry_sources
    moved = first.root.with_name("original-first")
    first.root.rename(moved)
    shutil.copytree(moved, first.root)
    replacement = discover_source(first.root)
    store.rotate_enrollment_evidence(replacement, authorization("ROTATE"))
    leases = RecordingLeases(store)
    before = store.load().canonical
    with pytest.raises(SourceAmbiguousError, match="authorized identity binding"):
        activate(store, replacement, leases)
    assert not leases.acquired
    assert store.load().canonical == before


def test_rotation_retains_authorized_binding_for_later_activation(registry_sources):
    store, (_, second, third) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    store.rotate_enrollment_evidence(second, authorization("ROTATE"))
    store.adopt(third, authorization("ADOPT"))
    activate(store, second, RecordingLeases(store))
    assert store.resolve_active_source(second.repo_uuid) == second


@pytest.mark.parametrize("action", ["ENROLL", "ADOPT", "REBIND", "ROTATE"])
def test_identity_actions_reject_stale_facts_before_evidence_write(registry_sources, action):
    import shutil
    from graphify.workspace.identity import SourceAmbiguousError

    store, (first, second, _) = registry_sources
    source = second if action == "ADOPT" else first
    if action == "ENROLL":
        store = RegistryStore(store.state.root.with_name("new-state"),
                              capabilities=RuntimeCapabilities.supported_test_fixture())
    moved = source.root.with_name("old-source")
    source.root.rename(moved)
    shutil.copytree(moved, source.root)

    def records():
        paths = list(store.state.root.glob("registry*.json"))
        paths += list((store.state.root / "evidence").glob("*.json"))
        return {str(path): path.read_bytes() for path in paths}

    before = records()
    method = {"ENROLL": "enroll", "ADOPT": "adopt", "REBIND": "rebind",
              "ROTATE": "rotate_enrollment_evidence"}[action]
    with pytest.raises(SourceAmbiguousError, match="changed"):
        getattr(store, method)(source, authorization(action))
    assert records() == before


def test_activation_rechecks_identity_after_lease_acquisition(registry_sources, monkeypatch):
    import shutil
    from graphify.workspace.identity import SourceAmbiguousError

    store, (_, second, _) = registry_sources
    store.adopt(second, authorization("ADOPT"))
    before = store.load().canonical
    evidence = {path.name: path.read_bytes() for path in (store.state.root / "evidence").iterdir()}
    leases = RecordingLeases(store)
    original = leases._acquire_under_registry_lock

    def acquire_and_replace(*args, **kwargs):
        grant = original(*args, **kwargs)
        moved = second.root.with_name("old-second")
        second.root.rename(moved)
        shutil.copytree(moved, second.root)
        return grant

    monkeypatch.setattr(leases, "_acquire_under_registry_lock", acquire_and_replace)
    with pytest.raises(SourceAmbiguousError, match="changed"):
        activate(store, second, leases)
    assert len(leases.acquired) == len(leases.released) == 1
    assert store.load().canonical == before
    assert {path.name: path.read_bytes() for path in (store.state.root / "evidence").iterdir()} == evidence
