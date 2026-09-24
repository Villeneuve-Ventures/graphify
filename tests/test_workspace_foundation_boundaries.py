"""Filesystem identity, initialization retry, and directory publication boundaries."""

import errno
import os

import pytest

from graphify.workspace.identity import IdentityAction, OperatorAuthorization, discover_source
from graphify.workspace.persistence import (
    CommitUnknown, DurableStateRoot, PosixSyscalls, RuntimeCapabilities, StatePathError,
)
from graphify.workspace.registry import RegistryStore
from tests.test_workspace_source_discovery import _git, source_repository  # noqa: F401


def _state(root, **kwargs):
    return DurableStateRoot(root, capabilities=RuntimeCapabilities.supported_test_fixture(),
                            **kwargs)


def _authorization():
    return OperatorAuthorization(IdentityAction.ENROLL, "fixture", "boundary regression",
                                 "2026-09-23T00:00:00Z", "enroll")


def _case_alias(path):
    alias = path.with_name(path.name.upper())
    if not alias.exists() or not alias.samefile(path):
        pytest.skip("fixture filesystem does not alias differently cased directory names")
    return alias


@pytest.mark.parametrize("relationship", ["same", "state_inside_source", "source_inside_state"])
def test_case_alias_overlap_is_rejected(tmp_path, relationship):
    source = tmp_path.resolve() / "source"
    source.mkdir()
    alias = _case_alias(source)
    root = alias
    if relationship == "state_inside_source":
        root = alias / "missing" / "state"
    elif relationship == "source_inside_state":
        source = source / "checkout"
        source.mkdir()
    with pytest.raises(StatePathError, match="overlaps"):
        _state(root).assert_external_to(source)
    assert not (alias / "missing").exists()


def test_distinct_sibling_directories_are_external(tmp_path):
    source = tmp_path.resolve() / "source"
    source.mkdir()
    root = tmp_path.resolve() / "state"
    root.mkdir()
    _state(root).assert_external_to(source)
    # Differently cased names can denote genuinely distinct directories.
    other = source.with_name("SOURCE")
    if not other.exists():
        other.mkdir()
        _state(other).assert_external_to(source)


def test_case_alias_enrollment_rejects_before_source_mutation(source_repository):
    _git(source_repository, "remote", "add", "origin", "https://example.test/repo.git")
    source = discover_source(source_repository)
    alias = _case_alias(source.root)

    def snapshot():
        return sorted((str(path.relative_to(source.root)), path.stat().st_mode,
                       path.stat().st_ino, path.stat().st_mtime_ns,
                       path.read_bytes() if path.is_file() else None)
                      for path in [source.root, *source.root.rglob("*")])

    before = snapshot()
    store = RegistryStore(alias / "state",
                          capabilities=RuntimeCapabilities.supported_test_fixture())
    with pytest.raises(StatePathError, match="overlaps"):
        store.enroll(source, _authorization())
    assert snapshot() == before
    assert not (source.root / "state").exists()


@pytest.mark.parametrize("boundary", ["root", "descendant"])
def test_directory_retry_repeats_failed_parent_sync(tmp_path, source_repository, boundary):
    _git(source_repository, "remote", "add", "origin", "https://example.test/repo.git")
    source = discover_source(source_repository)
    root = tmp_path.resolve() / "state"
    parent = tmp_path.resolve() if boundary == "root" else _state(root).ensure_directory("objects")
    identity = (parent.stat().st_dev, parent.stat().st_ino)

    class ParentSyncFailure(PosixSyscalls):
        enabled = True
        attempts = 0

        def fsync(self, descriptor):
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) == identity:
                self.attempts += 1
                if self.enabled:
                    raise OSError(errno.EIO, "injected parent sync failure")
            super().fsync(descriptor)

    syscalls = ParentSyncFailure()
    store = RegistryStore(root, capabilities=RuntimeCapabilities.supported_test_fixture(),
                          syscalls=syscalls)

    def initialize():
        if boundary == "root":
            return store.enroll(source, _authorization())
        return _state(root, syscalls=syscalls).ensure_directory("objects/item")

    retained = root if boundary == "root" else parent / "item"
    for _ in range(2):
        attempts = syscalls.attempts
        with pytest.raises(OSError, match="injected parent sync failure"):
            initialize()
        assert syscalls.attempts > attempts
        assert retained.is_dir()
        assert not (root / "registry.json").exists()
    retained_inode = retained.stat().st_ino
    attempts = syscalls.attempts
    syscalls.enabled = False
    result = initialize()
    assert syscalls.attempts > attempts
    assert retained.stat().st_ino == retained_inode
    if boundary == "root":
        assert result.to_dict()["revision"] == 1
        assert store.load().to_dict()["revision"] == 1
    else:
        assert result == retained


@pytest.mark.parametrize("race", ["destination_winner", "detached_parent"])
def test_directory_rename_preserves_publication_boundary(tmp_path, race):
    root = tmp_path.resolve() / "state"
    state = _state(root)
    source = state.ensure_directory("source/item")
    parent = state.ensure_directory("destination")
    state.create_private_file_bytes("source/item/payload", b"candidate", label="payload")
    winner_inode = None

    def hook(event):
        nonlocal winner_inode
        if race == "destination_winner" and event == "move:before_rename":
            winner = parent / "item"
            winner.mkdir(mode=0o700)
            winner_inode = winner.stat().st_ino
        elif race == "detached_parent" and event == "move:renamed":
            parent.rename(root / "detached")

    error = FileExistsError if race == "destination_winner" else CommitUnknown
    with pytest.raises(error):
        _state(root, fault_hook=hook).rename_contained(
            "source/item", "destination/item", label="move",
        )
    if race == "destination_winner":
        assert (parent / "item").stat().st_ino == winner_inode
        assert list((parent / "item").iterdir()) == []
        assert (source / "payload").read_bytes() == b"candidate"
    else:
        assert not (parent / "item").exists()
        assert (root / "detached/item/payload").read_bytes() == b"candidate"


def test_directory_rename_creates_missing_destination_parent(tmp_path):
    state = _state(tmp_path.resolve() / "state")
    source = state.ensure_directory("source/item")
    source_inode = source.stat().st_ino
    state.create_private_file_bytes("source/item/payload", b"candidate", label="payload")
    destination = state.rename_contained("source/item", "missing/nested/item", label="move")
    assert destination == state.root / "missing/nested/item"
    assert destination.stat().st_ino == source_inode
    assert (destination / "payload").read_bytes() == b"candidate"
    assert not source.exists()
