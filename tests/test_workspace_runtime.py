from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from dataclasses import replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Any, cast, Iterator

import pytest

import graphify.workspace.identity as identity_module
import graphify.workspace.leases as lease_module

from graphify.workspace.contracts import FencedLease, canonical_json_bytes
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    OperatorAuthorization,
    SourceAmbiguousError,
    SourceDiscoveryError,
    SourceIdentity,
    UUIDCollisionError,
    discover_source,
)
from graphify.workspace.leases import (
    LeaseBusy,
    LeaseError,
    LeaseExpired,
    LeaseGrant,
    LeaseOwner,
    LeaseStore,
    StaleLease,
    SystemLeaseIdentityProvider,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    InjectedFault,
    LockTimeout,
    LockOrderError,
    PosixSyscalls,
    REGISTRY_LOCK_RANK,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    UnsupportedRuntime,
)
from graphify.workspace.registry import RegistryStore, RevisionConflict


REPO_UUID = "11111111-1111-4111-8111-111111111111"
SECOND_UUID = "22222222-2222-4222-8222-222222222222"
REMOTE = "https://github.com/example/graphify-fixture.git"
FORK_REMOTE = "https://github.com/example/graphify-fixture-fork.git"
SUPPORTED = RuntimeCapabilities.supported_test_fixture()


def _run(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _workspace_toml(repo_uuid: str) -> str:
    return (
        'contract = "graphify.workspace.config"\n'
        "schema_version = 1\n"
        f'repo_uuid = "{repo_uuid}"\n'
        "\n"
        "[policy]\n"
        'freshness = "current_only"\n'
        'semantic_mode = "host_agent_only"\n'
        "network_egress = false\n"
        "headless_backends = []\n"
    )


def _create_repo(root: Path, repo_uuid: str, *, marker: str = "main") -> Path:
    root.mkdir(parents=True)
    _run(root, "init", "--quiet")
    _run(root, "config", "user.email", "workspace-test@example.com")
    _run(root, "config", "user.name", "Workspace Test")
    config = root / ".graphify/workspace.toml"
    config.parent.mkdir()
    config.write_text(_workspace_toml(repo_uuid), encoding="utf-8")
    (root / "README.md").write_text(f"{marker}\n", encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "--quiet", "-m", f"fixture {marker}")
    _run(root, "remote", "add", "origin", REMOTE)
    return root


def _clone_repo(source: Path, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "--quiet", str(source), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run(destination, "remote", "set-url", "origin", REMOTE)
    return destination


def _linked_worktree(source: Path, destination: Path, name: str) -> Path:
    _run(source, "worktree", "add", "--quiet", "-b", name, str(destination))
    return destination


def _commit_change(repo: Path, name: str) -> str:
    _run(repo, "config", "user.email", "workspace-test@example.com")
    _run(repo, "config", "user.name", "Workspace Test")
    (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _run(repo, "add", f"{name}.txt")
    _run(repo, "commit", "--quiet", "-m", name)
    return _run(repo, "rev-parse", "HEAD")


def _replace_bound_locator_with_unrelated_history(
    enrolled: Path,
    enrolled_source: SourceIdentity,
    tmp_path: Path,
) -> tuple[Path, SourceIdentity]:
    replacement = _create_repo(tmp_path / "replacement", REPO_UUID, marker="replacement")
    workspace_config = (replacement / ".graphify/workspace.toml").read_text(
        encoding="utf-8"
    )
    _run(replacement, "checkout", "--quiet", "--orphan", "unrelated-root")
    _run(replacement, "rm", "--quiet", "-rf", ".")
    (replacement / ".graphify").mkdir()
    (replacement / ".graphify/workspace.toml").write_text(
        workspace_config,
        encoding="utf-8",
    )
    (replacement / "README.md").write_text("unrelated replacement\n", encoding="utf-8")
    _run(replacement, "add", ".")
    _run(replacement, "commit", "--quiet", "-m", "unrelated root")

    retired = tmp_path / "retired"
    enrolled.rename(retired)
    replacement.rename(enrolled)
    replacement_source = discover_source(enrolled)
    assert replacement_source.registry_source == enrolled_source.registry_source
    assert not set(replacement_source.history_roots).intersection(
        enrolled_source.history_roots
    )
    assert (
        replacement_source.git_common_device,
        replacement_source.git_common_inode,
    ) != (
        enrolled_source.git_common_device,
        enrolled_source.git_common_inode,
    )
    return retired, replacement_source


def _authorization(action: IdentityAction, nonce: str) -> OperatorAuthorization:
    return OperatorAuthorization(
        action=action,
        operator_id="operator:test",
        reason=f"test {action.value.lower()}",
        issued_at="2026-07-16T15:00:00Z",
        nonce=nonce,
    )


def _workspace_entry(document: Any, repo_uuid: str = REPO_UUID) -> dict[str, Any]:
    entries = [item for item in document.to_dict()["workspaces"] if item["repo_uuid"] == repo_uuid]
    assert len(entries) == 1
    return entries[0]


def _tree_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        details = path.lstat()
        mode = stat.S_IMODE(details.st_mode)
        item: dict[str, Any] = {
            "mode": mode,
            "mtime_ns": details.st_mtime_ns,
            "size": details.st_size,
            "type": stat.S_IFMT(details.st_mode),
        }
        if path.is_symlink():
            item["target"] = os.readlink(path)
        elif path.is_file():
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if hasattr(os, "listxattr"):
            listxattr = getattr(os, "listxattr")
            getxattr = getattr(os, "getxattr")
            item["xattrs"] = {
                name: hashlib.sha256(getxattr(path, name, follow_symlinks=False)).hexdigest()
                for name in sorted(listxattr(path, follow_symlinks=False))
            }
        snapshot[relative] = item
    return snapshot


class CrashAt:
    def __init__(self, event: str) -> None:
        self.event = event
        self.fired = False

    def __call__(self, event: str) -> None:
        if event == self.event and not self.fired:
            self.fired = True
            raise InjectedFault(event)


class MutableLeaseIdentity:
    def __init__(self, owner: LeaseOwner) -> None:
        self.owner = owner

    def current_owner(self) -> LeaseOwner:
        return self.owner


class ShortWriteAndEintrSyscalls(PosixSyscalls):
    def __init__(self) -> None:
        self.interrupted = False

    def write(self, descriptor: int, data: memoryview) -> int:
        if not self.interrupted:
            self.interrupted = True
            raise InterruptedError(errno.EINTR, "injected EINTR")
        limit = max(1, len(data) // 3)
        return super().write(descriptor, data[:limit])


class FailOnceSyscalls(PosixSyscalls):
    def __init__(self, operation: str, error_number: int) -> None:
        self.operation = operation
        self.error_number = error_number
        self.failed = False

    def _fail(self, operation: str) -> None:
        if self.operation == operation and not self.failed:
            self.failed = True
            raise OSError(self.error_number, f"injected {operation}")

    def write(self, descriptor: int, data: memoryview) -> int:
        self._fail("write")
        return super().write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        self._fail("fsync")
        super().fsync(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        self._fail("replace")
        super().replace(source, destination)

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._fail("replace")
        super().replace_at(
            source,
            destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )


class FailFsyncCallSyscalls(PosixSyscalls):
    def __init__(self, call_number: int) -> None:
        self.call_number = call_number
        self.calls = 0

    def fsync(self, descriptor: int) -> None:
        self.calls += 1
        if self.calls == self.call_number:
            raise OSError(errno.EIO, "injected fsync after replace")
        super().fsync(descriptor)


class ExpireAfterFirstFsyncSyscalls(PosixSyscalls):
    def __init__(self) -> None:
        self.calls = 0
        self.expired = False

    def fsync(self, descriptor: int) -> None:
        super().fsync(descriptor)
        self.calls += 1
        if self.calls == 1:
            self.expired = True


def _enroll_process(
    state_root: str,
    repo_root: str,
    nonce: str,
    barrier: Any,
    results: Any,
) -> None:
    try:
        source = discover_source(Path(repo_root))
        store = RegistryStore(Path(state_root), capabilities=SUPPORTED)
        barrier.wait(timeout=15)
        document = store.enroll(source, _authorization(IdentityAction.ENROLL, nonce))
        results.put(("ok", document.to_dict()["revision"]))
    except BaseException as exc:  # pragma: no cover - asserted in the parent process
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _hold_registry_lock(
    state_root: str,
    *,
    shared: bool,
    started: Any,
    acquired: Any,
    release: Any,
) -> None:
    store = RegistryStore(Path(state_root), capabilities=SUPPORTED)
    lock = store.read_only_snapshot() if shared else store.exclusive_lock()
    started.set()
    with lock:
        acquired.set()
        release.wait(timeout=15)


def _hold_mutating_runtime_lock(
    state_root: str,
    repo_uuid: str,
    *,
    lock_name: str,
    acquired: Any,
    release: Any,
) -> None:
    registry = RegistryStore(Path(state_root), capabilities=SUPPORTED)
    leases = LeaseStore(Path(state_root), registry, capabilities=SUPPORTED)
    lock = (
        registry.exclusive_lock()
        if lock_name == "registry"
        else leases.workspace_lock(repo_uuid)
    )
    with lock:
        acquired.set()
        release.wait(timeout=15)


def test_runtime_rejects_unsupported_platform_without_test_capability(tmp_path: Path) -> None:
    unsupported = RuntimeCapabilities(
        system="Linux",
        filesystem="ext4",
        elevated=False,
        local=True,
    )

    with pytest.raises(UnsupportedRuntime, match="macOS.*APFS"):
        RegistryStore(tmp_path / "state", capabilities=unsupported)


def test_source_discovery_rejects_missing_descriptor_relative_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity_module.os, "supports_dir_fd", {identity_module.os.open})
    monkeypatch.setattr(
        identity_module,
        "_git",
        lambda *_args, **_kwargs: pytest.fail("unsupported runtime must fail before Git"),
    )

    with pytest.raises(SourceDiscoveryError, match="descriptor-relative file access"):
        discover_source(tmp_path)


def test_darwin_identity_probe_preserves_subsecond_process_start() -> None:
    def probe(microseconds: int) -> str:
        def fake_proc_pidinfo(
            pid: int,
            flavor: int,
            argument: int,
            buffer: Any,
            size: int,
        ) -> int:
            assert flavor == 3
            assert argument == 0
            assert size == ctypes.sizeof(lease_module._DarwinProcBSDInfo)
            info = ctypes.cast(
                buffer,
                ctypes.POINTER(lease_module._DarwinProcBSDInfo),
            ).contents
            info.pbi_pid = pid
            info.pbi_start_tvsec = 1_721_149_200
            info.pbi_start_tvusec = microseconds
            return size

        return SystemLeaseIdentityProvider._darwin_process_start(700, fake_proc_pidinfo)

    first = probe(100_001)
    second = probe(100_002)
    assert first == "1721149200:100001"
    assert second == "1721149200:100002"
    assert SystemLeaseIdentityProvider._digest("darwin-process-start", first) != (
        SystemLeaseIdentityProvider._digest("darwin-process-start", second)
    )


def test_darwin_identity_uses_timezone_stable_boot_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot_sessions = iter(
        [
            "AFC8B9F3-9D67-4745-8327-078C1E140CC0\n",
            "afc8b9f3-9d67-4745-8327-078c1e140cc0\n",
        ]
    )

    def fake_run(
        arguments: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert arguments == ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]
        assert check and capture_output and text
        assert env == {"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        return subprocess.CompletedProcess(arguments, 0, stdout=next(boot_sessions), stderr="")

    def fake_proc_pidinfo(
        pid: int,
        flavor: int,
        argument: int,
        buffer: Any,
        size: int,
    ) -> int:
        info = ctypes.cast(
            buffer,
            ctypes.POINTER(lease_module._DarwinProcBSDInfo),
        ).contents
        info.pbi_pid = pid
        info.pbi_start_tvsec = 1_721_149_200
        info.pbi_start_tvusec = 100_001
        return size

    monkeypatch.setenv("TZ", "UTC")
    first = SystemLeaseIdentityProvider._darwin_owner(
        700,
        runner=fake_run,
        proc_pidinfo=fake_proc_pidinfo,
    )
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    second = SystemLeaseIdentityProvider._darwin_owner(
        700,
        runner=fake_run,
        proc_pidinfo=fake_proc_pidinfo,
    )

    assert first == second


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is the supported macOS probe")
def test_darwin_identity_provider_reads_a_stable_live_owner() -> None:
    provider = SystemLeaseIdentityProvider()

    first = provider.current_owner()
    second = provider.current_owner()

    assert first == second
    assert first.pid == os.getpid()
    assert len(first.boot_id) == 64
    assert len(first.process_start_id) == 64


def test_enrollment_is_operator_authorized_audited_and_source_pure(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "source", REPO_UUID)
    before = _tree_snapshot(repo)
    source = discover_source(repo)
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)

    with pytest.raises(AuthorizationError, match="ENROLL"):
        store.enroll(source, _authorization(IdentityAction.REBIND, "wrong-action"))

    document = store.enroll(
        source,
        _authorization(IdentityAction.ENROLL, "first-enrollment"),
        expected_revision=0,
    )
    entry = _workspace_entry(document)

    assert document.to_dict()["revision"] == 1
    assert entry["repo_uuid"] == REPO_UUID
    assert entry["active_source"] == source.registry_source
    assert entry["aliases"] == []
    evidence_digest = entry["uuid_enrollment"]["immutable_evidence_sha256"]
    evidence = store.read_evidence(evidence_digest)
    assert evidence["action"] == "ENROLL"
    assert evidence["authorization"]["operator_id"] == "operator:test"
    assert evidence["source_sha256"] == source.source_sha256
    assert evidence["registry_revision"] == 1
    assert evidence["active_source_revision"] == 1
    assert evidence["operation_epoch"] == 1
    assert evidence["fence_token"] == 1
    assert _tree_snapshot(repo) == before


def test_state_root_rejects_links_overlap_and_split_registry_roots(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "source", REPO_UUID)
    before = _tree_snapshot(repo)
    overlapping = RegistryStore(repo / ".workspace-state", capabilities=SUPPORTED)

    with pytest.raises(StatePathError, match="overlaps source checkout"):
        overlapping.enroll(
            discover_source(repo),
            _authorization(IdentityAction.ENROLL, "overlap"),
            expected_revision=0,
        )
    assert _tree_snapshot(repo) == before

    target = tmp_path / "real-state"
    target.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(target, target_is_directory=True)
    with pytest.raises(StatePathError, match="symbolic link"):
        RegistryStore(linked_state, capabilities=SUPPORTED)

    registry = RegistryStore(tmp_path / "registry-state", capabilities=SUPPORTED)
    with pytest.raises(LeaseError, match="share one external state root"):
        LeaseStore(tmp_path / "other-state", registry, capabilities=SUPPORTED)


def test_state_root_inspection_probe_distinguishes_missing_existing_and_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = DurableStateRoot(tmp_path / "missing-state", capabilities=SUPPORTED)
    existing_root = tmp_path / "existing-state"
    existing_root.mkdir(mode=0o700)
    existing = DurableStateRoot(existing_root, capabilities=SUPPORTED)
    before = _tree_snapshot(tmp_path)

    assert missing.root_exists_for_inspection() is False
    assert existing.root_exists_for_inspection() is True

    original_open = os.open

    def reject_parent(path: os.PathLike[str] | str, *args: Any, **kwargs: Any) -> int:
        if path == existing.root.parent.name and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EACCES, "operator-secret-state-parent")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", reject_parent)

    with pytest.raises(StatePathError):
        existing.root_exists_for_inspection()
    assert _tree_snapshot(tmp_path) == before


def test_state_root_inspection_probe_normalizes_binding_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    state = DurableStateRoot(state_root, capabilities=SUPPORTED)
    before = _tree_snapshot(tmp_path)
    original_stat = os.stat

    def reject_root_binding(
        path: os.PathLike[str] | str,
        *args: Any,
        **kwargs: Any,
    ) -> os.stat_result:
        if path == state.root.name and kwargs.get("dir_fd") is not None:
            raise FileNotFoundError(errno.ENOENT, "operator-secret-binding")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", reject_root_binding)

    with pytest.raises(StatePathError):
        state.root_exists_for_inspection()
    assert _tree_snapshot(tmp_path) == before


def test_ensure_directory_holds_descriptor_across_binding_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)
    target = state.ensure_directory("workspaces")
    target.chmod(0o755)
    parked = tmp_path / "parked-workspaces"
    external = tmp_path / "external-workspaces"
    external.mkdir(mode=0o755)
    before_external = _tree_snapshot(external)
    original_fchmod = os.fchmod
    target_identity = target.stat()
    swapped = False

    def swap_binding(descriptor: int, mode: int) -> None:
        nonlocal swapped
        opened = os.fstat(descriptor)
        if not swapped and (opened.st_dev, opened.st_ino) == (
            target_identity.st_dev,
            target_identity.st_ino,
        ):
            target.rename(parked)
            target.symlink_to(external, target_is_directory=True)
            swapped = True
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", swap_binding)

    try:
        with pytest.raises(StatePathError, match="changed while opening"):
            state.ensure_directory("workspaces")
        assert swapped
        assert stat.S_IMODE(external.stat().st_mode) == 0o755
        assert _tree_snapshot(external) == before_external
    finally:
        if target.is_symlink():
            target.unlink()
            parked.rename(target)


def test_state_root_repair_holds_descriptor_across_binding_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)
    state.ensure_directory(".")
    parked = tmp_path / "parked-state"
    external = tmp_path / "external-state"
    external.mkdir(mode=0o755)
    before_external = _tree_snapshot(external)
    original_fchmod = os.fchmod
    root_identity = state.root.stat()
    swapped = False

    def swap_binding(descriptor: int, mode: int) -> None:
        nonlocal swapped
        opened = os.fstat(descriptor)
        if not swapped and (opened.st_dev, opened.st_ino) == (
            root_identity.st_dev,
            root_identity.st_ino,
        ):
            state.root.rename(parked)
            state.root.symlink_to(external, target_is_directory=True)
            swapped = True
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", swap_binding)

    try:
        with pytest.raises(StatePathError, match="state root changed while opening"):
            state.ensure_directory(".")
        assert swapped
        assert stat.S_IMODE(external.stat().st_mode) == 0o755
        assert _tree_snapshot(external) == before_external
    finally:
        if state.root.is_symlink():
            state.root.unlink()
            parked.rename(state.root)


def test_root_descriptor_handoff_survives_parent_binding_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "root-parent"
    parent.mkdir()
    state = DurableStateRoot(parent / "state", capabilities=SUPPORTED)
    state.ensure_directory(".")
    parked_parent = tmp_path / "parked-root-parent"
    external_parent = tmp_path / "external-root-parent"
    external_state = external_parent / "state"
    external_state.mkdir(parents=True, mode=0o700)
    external_state.chmod(0o700)
    before_external = _tree_snapshot(external_parent)
    root_identity = state.root.stat()
    original_dup = os.dup
    swapped = False

    def swap_after_root_is_held(descriptor: int) -> int:
        nonlocal swapped
        duplicate = original_dup(descriptor)
        opened = os.fstat(descriptor)
        if not swapped and (opened.st_dev, opened.st_ino) == (
            root_identity.st_dev,
            root_identity.st_ino,
        ):
            parent.rename(parked_parent)
            parent.symlink_to(external_parent, target_is_directory=True)
            swapped = True
        return duplicate

    monkeypatch.setattr(os, "dup", swap_after_root_is_held)

    try:
        state.ensure_directory("workspaces")
        assert swapped
        assert (parked_parent / "state" / "workspaces").is_dir()
        assert not (external_state / "workspaces").exists()
        assert _tree_snapshot(external_parent) == before_external
    finally:
        if parent.is_symlink():
            parent.unlink()
            parked_parent.rename(parent)


def test_writer_lock_rejects_parent_swap_without_external_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)
    lock_relative = Path("workspaces") / REPO_UUID / "locks" / "workspace.lock"
    parent = state.ensure_directory(lock_relative.parent)
    parked = tmp_path / "parked-locks"
    external = tmp_path / "external-locks"
    external.mkdir(mode=0o700)
    external_lock = external / "workspace.lock"
    external_lock.write_bytes(b"external lock\n")
    external_lock.chmod(0o644)
    before_external = _tree_snapshot(external)
    original_ensure_parent = state._ensure_parent
    swapped = False

    def swap_after_ensure(path: Path) -> None:
        nonlocal swapped
        original_ensure_parent(path)
        parent.rename(parked)
        parent.symlink_to(external, target_is_directory=True)
        swapped = True

    monkeypatch.setattr(state, "_ensure_parent", swap_after_ensure)

    try:
        with pytest.raises(StatePathError):
            with state.lock(lock_relative, rank=REGISTRY_LOCK_RANK, name="registry"):
                pytest.fail("unsafe writer lock unexpectedly acquired")
        assert swapped
        assert stat.S_IMODE(external_lock.stat().st_mode) == 0o644
        assert _tree_snapshot(external) == before_external
    finally:
        if parent.is_symlink():
            parent.unlink()
            parked.rename(parent)


def test_unlink_and_sync_holds_parent_descriptor_across_binding_swap(
    tmp_path: Path,
) -> None:
    parent: Path | None = None
    parked = tmp_path / "parked-cleanup"
    external = tmp_path / "external-cleanup"
    events: list[str] = []

    def swap_before_unlink(event: str) -> None:
        events.append(event)
        if event == "test:cleanup:before_unlink":
            assert parent is not None
            parent.rename(parked)
            parent.symlink_to(external, target_is_directory=True)

    state = DurableStateRoot(
        tmp_path / "state",
        capabilities=SUPPORTED,
        fault_hook=swap_before_unlink,
    )
    relative = Path("workspaces") / REPO_UUID / "gc" / "intent.json"
    parent = state.ensure_directory(relative.parent)
    internal = state.path(relative)
    internal.write_bytes(b"internal intent\n")
    internal.chmod(0o600)
    external.mkdir(mode=0o700)
    external_intent = external / "intent.json"
    external_intent.write_bytes(b"external intent\n")
    external_intent.chmod(0o600)
    before_external = _tree_snapshot(external)

    try:
        state.unlink_and_sync(relative, label="test:cleanup")
        assert events[-3:] == [
            "test:cleanup:before_unlink",
            "test:cleanup:unlinked",
            "test:cleanup:parent_durable",
        ]
        assert not (parked / "intent.json").exists()
        assert _tree_snapshot(external) == before_external
    finally:
        if parent.is_symlink():
            parent.unlink()
            parked.rename(parent)


def test_source_identity_rejects_linked_or_hardlinked_workspace_config(tmp_path: Path) -> None:
    symlink_repo = _create_repo(tmp_path / "symlink-repo", REPO_UUID)
    symlink_config = symlink_repo / ".graphify/workspace.toml"
    external_config = tmp_path / "external-workspace.toml"
    external_config.write_bytes(symlink_config.read_bytes())
    symlink_config.unlink()
    symlink_config.symlink_to(external_config)
    with pytest.raises(SourceDiscoveryError, match="cannot open source identity file"):
        discover_source(symlink_repo)

    hardlink_repo = _create_repo(tmp_path / "hardlink-repo", SECOND_UUID)
    hardlink_config = hardlink_repo / ".graphify/workspace.toml"
    os.link(hardlink_config, tmp_path / "workspace-hardlink.toml")
    with pytest.raises(SourceDiscoveryError, match="singular regular file"):
        discover_source(hardlink_repo)


def test_source_discovery_scrubs_ambient_git_directory_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _create_repo(tmp_path / "source", REPO_UUID)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "private-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "private-work-tree"))

    source = discover_source(repo)

    assert source.root == repo.resolve()
    assert source.repo_uuid == REPO_UUID


def test_source_discovery_resolves_roots_from_the_captured_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _create_repo(tmp_path / "source", REPO_UUID, marker="source")
    raced = _create_repo(tmp_path / "raced", REPO_UUID, marker="raced")
    captured_head = _run(repo, "rev-parse", "HEAD")
    captured_root = _run(repo, "rev-list", "--max-parents=0", captured_head)
    raced_head = _run(raced, "rev-parse", "HEAD")
    _run(repo, "fetch", "--quiet", str(raced), raced_head)
    original_git = identity_module._git
    switched = False

    def switch_head_after_capture(
        root: Path,
        *arguments: str,
        deadline_ns: int | None = None,
    ) -> str:
        nonlocal switched
        result = original_git(root, *arguments, deadline_ns=deadline_ns)
        if arguments == ("rev-parse", "HEAD") and not switched:
            _run(repo, "update-ref", "HEAD", raced_head)
            switched = True
        return result

    monkeypatch.setattr(identity_module, "_git", switch_head_after_capture)

    source = discover_source(repo)

    assert switched
    assert source.head_commit == captured_head
    assert source.history_roots == (captured_root,)
    assert _run(repo, "rev-parse", "HEAD") == raced_head


def test_source_discovery_ignores_replacement_refs_for_adoption_history(
    tmp_path: Path,
) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID, marker="original")
    unrelated = _create_repo(tmp_path / "unrelated", REPO_UUID, marker="unrelated")
    enrolled_root = _run(original, "rev-list", "--max-parents=0", "HEAD")
    _run(unrelated, "fetch", "--quiet", str(original), enrolled_root)
    _run(unrelated, "replace", "--graft", "HEAD", enrolled_root)
    assert _run(unrelated, "rev-list", "--max-parents=0", "HEAD") == enrolled_root

    state_root = tmp_path / "state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "enroll-original"),
        expected_revision=0,
    )
    before_state = _tree_snapshot(state_root)

    with pytest.raises(UUIDCollisionError, match="shared history"):
        store.adopt(
            discover_source(unrelated),
            _authorization(IdentityAction.ADOPT, "reject-replacement-ref"),
            expected_revision=1,
        )

    assert store.load().to_dict()["revision"] == 1
    assert _tree_snapshot(state_root) == before_state


def test_source_discovery_ignores_graft_files_for_adoption_history(
    tmp_path: Path,
) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID, marker="original")
    unrelated = _create_repo(tmp_path / "unrelated", REPO_UUID, marker="unrelated")
    enrolled_root = _run(original, "rev-list", "--max-parents=0", "HEAD")
    unrelated_head = _run(unrelated, "rev-parse", "HEAD")
    _run(unrelated, "fetch", "--quiet", str(original), enrolled_root)
    grafts = unrelated / ".git/info/grafts"
    grafts.write_text(f"{unrelated_head} {enrolled_root}\n", encoding="utf-8")
    assert _run(unrelated, "rev-list", "--max-parents=0", "HEAD") == enrolled_root

    state_root = tmp_path / "state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "enroll-original"),
        expected_revision=0,
    )
    before_state = _tree_snapshot(state_root)

    with pytest.raises(UUIDCollisionError, match="shared history"):
        store.adopt(
            discover_source(unrelated),
            _authorization(IdentityAction.ADOPT, "reject-graft-file"),
            expected_revision=1,
        )

    assert store.load().to_dict()["revision"] == 1
    assert _tree_snapshot(state_root) == before_state


def test_shallow_history_without_enrollment_root_cannot_satisfy_adoption(
    tmp_path: Path,
) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID, marker="original")
    _commit_change(original, "second-commit")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", original.resolve().as_uri(), str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run(shallow, "remote", "set-url", "origin", REMOTE)
    assert _run(shallow, "rev-parse", "--is-shallow-repository") == "true"

    state_root = tmp_path / "state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "enroll-complete-history"),
        expected_revision=0,
    )

    with pytest.raises(UUIDCollisionError, match="shared history"):
        store.adopt(
            discover_source(shallow),
            _authorization(IdentityAction.ADOPT, "reject-shallow-history"),
            expected_revision=1,
        )

    assert store.load().to_dict()["revision"] == 1


def test_source_identity_rejects_a_symlinked_graphify_directory(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "source", REPO_UUID)
    graphify_directory = repo / ".graphify"
    external = tmp_path / "private-graphify"
    graphify_directory.rename(external)
    graphify_directory.symlink_to(external, target_is_directory=True)

    with pytest.raises(SourceDiscoveryError):
        discover_source(repo)


def test_source_discovery_config_byte_limit_is_explicit_per_caller(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "source", REPO_UUID)
    config = repo / ".graphify/workspace.toml"
    config.write_bytes(config.read_bytes() + b"\n#" + b"x" * (64 * 1024))

    assert discover_source(repo).repo_uuid == REPO_UUID
    with pytest.raises(SourceDiscoveryError, match="exceeds byte limit"):
        discover_source(repo, max_bytes=64 * 1024)


def test_uuid_collision_adoption_and_enrollment_evidence_rotation(tmp_path: Path) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID, marker="original")
    clone = _clone_repo(original, tmp_path / "clone")
    fork = _clone_repo(original, tmp_path / "fork")
    _run(fork, "remote", "set-url", "origin", FORK_REMOTE)
    unrelated = _create_repo(tmp_path / "unrelated", REPO_UUID, marker="unrelated")
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    first = store.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "enroll-original"),
        expected_revision=0,
    )

    with pytest.raises(UUIDCollisionError, match="uuid_collision"):
        store.enroll(
            discover_source(clone),
            _authorization(IdentityAction.ENROLL, "clone-collision"),
            expected_revision=1,
        )
    with pytest.raises(UUIDCollisionError, match="uuid_collision"):
        store.enroll(
            discover_source(fork),
            _authorization(IdentityAction.ENROLL, "fork-collision"),
            expected_revision=1,
        )
    with pytest.raises(UUIDCollisionError, match="shared history"):
        store.adopt(
            discover_source(unrelated),
            _authorization(IdentityAction.ADOPT, "bad-adoption"),
            expected_revision=1,
        )

    adopted = store.adopt(
        discover_source(clone),
        _authorization(IdentityAction.ADOPT, "adopt-clone"),
        expected_revision=1,
    )
    adopted_fork = store.adopt(
        discover_source(fork),
        _authorization(IdentityAction.ADOPT, "adopt-fork"),
        expected_revision=2,
    )
    adopted_entry = _workspace_entry(adopted_fork)
    immutable_digest = adopted_entry["uuid_enrollment"]["immutable_evidence_sha256"]
    current_digest = adopted_entry["uuid_enrollment"]["current_evidence_sha256"]

    assert adopted.to_dict()["revision"] == 2
    assert adopted_fork.to_dict()["revision"] == 3
    assert adopted_entry["repo_uuid"] == REPO_UUID
    assert adopted_entry["active_source"]["path"] == str(original.resolve())
    assert {alias["path"] for alias in adopted_entry["aliases"]} == {
        str(clone.resolve()),
        str(fork.resolve()),
    }

    rotated = store.rotate_enrollment_evidence(
        discover_source(clone),
        _authorization(IdentityAction.ROTATE, "rotate-current-evidence"),
        expected_revision=3,
    )
    rotated_entry = _workspace_entry(rotated)

    assert rotated.to_dict()["revision"] == 4
    assert rotated_entry["repo_uuid"] == REPO_UUID
    assert rotated_entry["uuid_enrollment"]["immutable_evidence_sha256"] == immutable_digest
    assert rotated_entry["uuid_enrollment"]["current_evidence_sha256"] not in {
        immutable_digest,
        current_digest,
    }
    rotation_evidence = store.read_evidence(
        rotated_entry["uuid_enrollment"]["current_evidence_sha256"]
    )
    assert rotation_evidence["action"] == "ROTATE"
    assert rotation_evidence["source"]["path"] == str(clone.resolve())


def test_rotate_rejects_bound_locator_replaced_by_unrelated_history_without_writes(
    tmp_path: Path,
) -> None:
    enrolled = _create_repo(tmp_path / "enrolled", REPO_UUID, marker="enrolled")
    enrolled_source = discover_source(enrolled)
    state_root = tmp_path / "state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        enrolled_source,
        _authorization(IdentityAction.ENROLL, "replacement-enroll"),
        expected_revision=0,
    )
    retired, replacement_source = _replace_bound_locator_with_unrelated_history(
        enrolled,
        enrolled_source,
        tmp_path,
    )
    registry_path = state_root / "registry.json"
    workspace_path = state_root / "workspaces" / REPO_UUID / "workspace.json"
    evidence_dir = state_root / "evidence"
    before_registry = registry_path.read_bytes()
    before_evidence = {
        path.name: path.read_bytes() for path in sorted(evidence_dir.glob("*.json"))
    }
    before_workspace = workspace_path.read_bytes()
    before_state = _tree_snapshot(state_root)
    before_sources = {path: _tree_snapshot(path) for path in (retired, enrolled)}

    with pytest.raises(SourceAmbiguousError, match="source_ambiguous"):
        store.rotate_enrollment_evidence(
            replacement_source,
            _authorization(IdentityAction.ROTATE, "reject-replacement"),
            expected_revision=1,
        )

    assert registry_path.read_bytes() == before_registry
    assert {
        path.name: path.read_bytes() for path in sorted(evidence_dir.glob("*.json"))
    } == before_evidence
    assert workspace_path.read_bytes() == before_workspace
    assert _tree_snapshot(state_root) == before_state
    assert {path: _tree_snapshot(path) for path in (retired, enrolled)} == before_sources


def test_resolve_active_source_rejects_bound_locator_replaced_by_unrelated_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrolled = _create_repo(tmp_path / "enrolled", REPO_UUID, marker="enrolled")
    enrolled_source = discover_source(enrolled)
    state_root = tmp_path / "state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        enrolled_source,
        _authorization(IdentityAction.ENROLL, "replacement-enroll"),
        expected_revision=0,
    )
    retired, _replacement_source = _replace_bound_locator_with_unrelated_history(
        enrolled,
        enrolled_source,
        tmp_path,
    )
    before_state = _tree_snapshot(state_root)
    before_sources = {path: _tree_snapshot(path) for path in (retired, enrolled)}
    read_evidence = store.read_evidence
    evidence_reads = 0

    def counted_read_evidence(digest: str) -> dict[str, Any]:
        nonlocal evidence_reads
        evidence_reads += 1
        return read_evidence(digest)

    monkeypatch.setattr(store, "read_evidence", counted_read_evidence)
    store.load()
    registry_load_reads = evidence_reads
    evidence_reads = 0

    with pytest.raises(SourceAmbiguousError, match="source_ambiguous"):
        store.resolve_active_source(REPO_UUID)

    assert evidence_reads == registry_load_reads + 1
    assert _tree_snapshot(state_root) == before_state
    assert {path: _tree_snapshot(path) for path in (retired, enrolled)} == before_sources


def test_rotate_allows_enrolled_common_directory_after_history_rewrite(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    enrolled_source = discover_source(repo)
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    enrolled = store.enroll(
        enrolled_source,
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    enrolled_entry = _workspace_entry(enrolled)
    rewritten_head = _run(
        repo,
        "commit-tree",
        _run(repo, "write-tree"),
        "-m",
        "rewritten-root",
    )
    _run(repo, "update-ref", "HEAD", rewritten_head)
    rewritten_source = discover_source(repo)
    assert (
        rewritten_source.git_common_device,
        rewritten_source.git_common_inode,
    ) == (
        enrolled_source.git_common_device,
        enrolled_source.git_common_inode,
    )
    assert not set(enrolled_source.history_roots).intersection(
        rewritten_source.history_roots
    )

    rotated = store.rotate_enrollment_evidence(
        rewritten_source,
        _authorization(IdentityAction.ROTATE, "rotate-rewritten-history"),
        expected_revision=1,
    )

    rotated_entry = _workspace_entry(rotated)
    assert rotated.to_dict()["revision"] == 2
    assert rotated_entry["uuid_enrollment"]["immutable_evidence_sha256"] == (
        enrolled_entry["uuid_enrollment"]["immutable_evidence_sha256"]
    )
    evidence = store.read_evidence(
        rotated_entry["uuid_enrollment"]["current_evidence_sha256"]
    )
    assert evidence["action"] == "ROTATE"
    assert evidence["history_roots"] == list(rewritten_source.history_roots)


def test_same_git_common_directory_cannot_be_reenrolled_under_a_new_uuid(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    store.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "first-uuid"),
        expected_revision=0,
    )
    (repo / ".graphify/workspace.toml").write_text(
        _workspace_toml(SECOND_UUID),
        encoding="utf-8",
    )

    with pytest.raises(UUIDCollisionError, match="common directory|common-directory"):
        store.enroll(
            discover_source(repo),
            _authorization(IdentityAction.ENROLL, "replacement-uuid"),
            expected_revision=1,
        )

    document = store.load()
    assert document.to_dict()["revision"] == 1
    assert [entry["repo_uuid"] for entry in document.to_dict()["workspaces"]] == [REPO_UUID]


def test_same_git_common_directory_cannot_be_adopted_after_remote_change(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    store.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    _run(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://github.com/example/changed-remote.git",
    )
    before_state = _tree_snapshot(store.state.root)

    with pytest.raises(UUIDCollisionError, match="already enrolled|already bound"):
        store.adopt(
            discover_source(repo),
            _authorization(IdentityAction.ADOPT, "reject-already-bound"),
            expected_revision=1,
        )

    document = store.load().to_dict()
    assert document["revision"] == 1
    assert document["workspaces"][0]["aliases"] == []
    assert _tree_snapshot(store.state.root) == before_state


def test_adopt_rejects_cross_uuid_persisted_common_directory_identity_before_writes(
    tmp_path: Path,
) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID)
    second = _clone_repo(original, tmp_path / "second")
    original_source = discover_source(original)
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    store.enroll(
        original_source,
        _authorization(IdentityAction.ENROLL, "enroll-original"),
        expected_revision=0,
    )

    (second / ".graphify/workspace.toml").write_text(
        _workspace_toml(SECOND_UUID),
        encoding="utf-8",
    )
    _run(second, "config", "user.email", "workspace-test@example.com")
    _run(second, "config", "user.name", "Workspace Test")
    _run(second, "add", ".graphify/workspace.toml")
    _run(second, "commit", "--quiet", "-m", "retarget second workspace UUID")
    second_source = discover_source(second)
    store.enroll(
        second_source,
        _authorization(IdentityAction.ENROLL, "enroll-second"),
        expected_revision=1,
    )

    moved = tmp_path / "moved-original"
    original.rename(moved)
    (moved / ".graphify/workspace.toml").write_text(
        _workspace_toml(SECOND_UUID),
        encoding="utf-8",
    )
    moved_source = discover_source(moved)
    assert moved_source.root != original_source.root
    assert (
        moved_source.registry_source["git_common_dir"]
        != original_source.registry_source["git_common_dir"]
    )
    assert (
        moved_source.git_common_device,
        moved_source.git_common_inode,
    ) == (
        original_source.git_common_device,
        original_source.git_common_inode,
    )
    assert set(moved_source.history_roots).intersection(second_source.history_roots)

    before_document = store.load().to_dict()
    before_external = _tree_snapshot(store.state.root)
    before_evidence = {path.name for path in (store.state.root / "evidence").iterdir()}
    before_checkouts = {path: _tree_snapshot(path) for path in (moved, second)}
    rejection: Exception | None = None
    try:
        store.adopt(
            moved_source,
            _authorization(IdentityAction.ADOPT, "reject-cross-uuid-identity"),
            expected_revision=2,
        )
    except (UUIDCollisionError, StateCorrupt) as exc:
        rejection = exc
    else:
        pytest.fail("cross-UUID persisted common-directory identity was adopted")

    after_document = store.load().to_dict()
    after_evidence = {path.name for path in (store.state.root / "evidence").iterdir()}
    assert {
        "rejection_type": type(rejection).__name__,
        "registry_revision": after_document["revision"],
        "registry_entries_unchanged": (
            after_document["workspaces"] == before_document["workspaces"]
        ),
        "new_evidence_files": sorted(after_evidence - before_evidence),
        "external_state_unchanged": _tree_snapshot(store.state.root) == before_external,
        "source_checkouts_unchanged": (
            {path: _tree_snapshot(path) for path in (moved, second)} == before_checkouts
        ),
    } == {
        "rejection_type": "UUIDCollisionError",
        "registry_revision": 2,
        "registry_entries_unchanged": True,
        "new_evidence_files": [],
        "external_state_unchanged": True,
        "source_checkouts_unchanged": True,
    }


def test_adopt_allows_a_distinct_clone_when_retained_inode_is_reused(
    tmp_path: Path,
) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID)
    clone = _clone_repo(original, tmp_path / "clone")
    original_source = discover_source(original)
    clone_source = discover_source(clone)
    assert clone_source.root != original_source.root
    assert (
        clone_source.registry_source["git_common_dir"]
        != original_source.registry_source["git_common_dir"]
    )
    simulated_reuse = replace(
        clone_source,
        git_common_device=original_source.git_common_device,
        git_common_inode=original_source.git_common_inode,
    )
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    store.enroll(
        original_source,
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )

    adopted = store.adopt(
        simulated_reuse,
        _authorization(IdentityAction.ADOPT, "adopt-reused-inode"),
        expected_revision=1,
    )

    entry = _workspace_entry(adopted)
    assert adopted.to_dict()["revision"] == 2
    assert [alias["path"] for alias in entry["aliases"]] == [str(clone.resolve())]


def test_adopt_rejects_unrelated_history_when_retained_inode_is_reused(
    tmp_path: Path,
) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID, marker="original")
    unrelated = _create_repo(tmp_path / "unrelated", REPO_UUID, marker="unrelated")
    original_source = discover_source(original)
    unrelated_source = discover_source(unrelated)
    assert not set(original_source.history_roots).intersection(unrelated_source.history_roots)
    simulated_reuse = replace(
        unrelated_source,
        git_common_device=original_source.git_common_device,
        git_common_inode=original_source.git_common_inode,
    )
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    store.enroll(
        original_source,
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    before_state = _tree_snapshot(store.state.root)

    with pytest.raises(UUIDCollisionError, match="shared history"):
        store.adopt(
            simulated_reuse,
            _authorization(IdentityAction.ADOPT, "reject-reused-inode"),
            expected_revision=1,
        )

    assert store.load().to_dict()["revision"] == 1
    assert _tree_snapshot(store.state.root) == before_state


def test_rebind_allows_enrolled_common_directory_after_history_rewrite(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    enrolled_source = discover_source(repo)
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    enrolled = store.enroll(
        enrolled_source,
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    enrolled_entry = _workspace_entry(enrolled)

    rewritten_head = _run(
        repo,
        "commit-tree",
        _run(repo, "write-tree"),
        "-m",
        "rewritten-root",
    )
    _run(repo, "update-ref", "HEAD", rewritten_head)
    rewritten_source = discover_source(repo)
    assert (
        rewritten_source.git_common_device,
        rewritten_source.git_common_inode,
    ) == (
        enrolled_source.git_common_device,
        enrolled_source.git_common_inode,
    )
    assert not set(enrolled_source.history_roots).intersection(
        rewritten_source.history_roots
    )

    rebound = store.rebind(
        rewritten_source,
        _authorization(IdentityAction.REBIND, "rebind-rewritten-history"),
        expected_revision=1,
    )

    rebound_entry = _workspace_entry(rebound)
    assert rebound.to_dict()["revision"] == 2
    assert rebound_entry["uuid_enrollment"]["current_evidence_sha256"] != (
        enrolled_entry["uuid_enrollment"]["current_evidence_sha256"]
    )
    evidence = store.read_evidence(
        rebound_entry["uuid_enrollment"]["current_evidence_sha256"]
    )
    assert evidence["action"] == "REBIND"
    assert evidence["history_roots"] == list(rewritten_source.history_roots)

    leases = LeaseStore(tmp_path / "state", store, capabilities=SUPPORTED)
    activation = store.activate_source(
        rewritten_source,
        _authorization(IdentityAction.ACTIVATE, "activate-rewritten-history"),
        leases=leases,
        owner=leases.current_owner(),
        expected_registry_revision=2,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 1, tzinfo=timezone.utc),
        monotonic_ns=100,
        ttl_ns=50,
    )

    activated_entry = _workspace_entry(activation.registry)
    assert activation.registry.to_dict()["revision"] == 3
    assert activated_entry["active_source_revision"] == 2
    assert activated_entry["active_source"] == rewritten_source.registry_source
    assert store.resolve_active_source(REPO_UUID).registry_source == (
        rewritten_source.registry_source
    )


def test_rebind_aliases_and_active_source_activation_cas_fail_closed(tmp_path: Path) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID)
    linked = _linked_worktree(original, tmp_path / "linked", "linked-source")
    clone = _clone_repo(original, tmp_path / "clone")
    store = RegistryStore(tmp_path / "state", capabilities=SUPPORTED)
    leases = LeaseStore(tmp_path / "state", store, capabilities=SUPPORTED)
    enrolled = store.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    rebound = store.rebind(
        discover_source(linked),
        _authorization(IdentityAction.REBIND, "bind-linked"),
        expected_revision=enrolled.to_dict()["revision"],
    )
    enrolled_entry = _workspace_entry(enrolled)
    rebound_entry = _workspace_entry(rebound)
    assert (
        rebound_entry["active_source_evidence"]
        == enrolled_entry["active_source_evidence"]
    )
    rebind_evidence = store.read_evidence(
        rebound_entry["uuid_enrollment"]["current_evidence_sha256"]
    )
    assert rebind_evidence["action"] == "REBIND"
    assert rebind_evidence["source"] == discover_source(linked).registry_source
    corrupt_root = tmp_path / "corrupt-rebind-state"
    shutil.copytree(tmp_path / "state", corrupt_root)
    corrupt_document = rebound.to_dict()
    corrupt_entry = corrupt_document["workspaces"][0]
    corrupt_entry["active_source_evidence"]["rebind_evidence_sha256"] = (
        rebound_entry["uuid_enrollment"]["current_evidence_sha256"]
    )
    corrupt_entry["active_source_evidence"]["source_sha256"] = rebind_evidence[
        "source_sha256"
    ]
    (corrupt_root / "registry.json").write_bytes(canonical_json_bytes(corrupt_document))
    with pytest.raises(StateCorrupt):
        RegistryStore(corrupt_root, capabilities=SUPPORTED).load()
    adopted = store.adopt(
        discover_source(clone),
        _authorization(IdentityAction.ADOPT, "adopt-clone"),
        expected_revision=rebound.to_dict()["revision"],
    )
    divergent_heads = {
        _commit_change(original, "original-diverged"),
        _commit_change(linked, "linked-diverged"),
        _commit_change(clone, "clone-diverged"),
    }
    assert len(divergent_heads) == 3
    owner = leases.current_owner()

    with pytest.raises(RevisionConflict, match="active_source_revision"):
        store.activate_source(
            discover_source(linked),
            _authorization(IdentityAction.ACTIVATE, "stale-cas"),
            leases=leases,
            owner=owner,
            expected_registry_revision=adopted.to_dict()["revision"],
            expected_active_source_revision=99,
            expected_operation_epoch=1,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 1, tzinfo=timezone.utc),
            monotonic_ns=100,
            ttl_ns=50,
        )

    activation = store.activate_source(
        discover_source(linked),
        _authorization(IdentityAction.ACTIVATE, "activate-linked"),
        leases=leases,
        owner=owner,
        expected_registry_revision=adopted.to_dict()["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 1, tzinfo=timezone.utc),
        monotonic_ns=100,
        ttl_ns=50,
    )
    entry = _workspace_entry(activation.registry)

    assert entry["active_source_revision"] == 2
    assert entry["active_source"]["path"] == str(linked.resolve())
    assert entry["active_source_evidence"]["operation_epoch"] == 2
    assert entry["active_source_evidence"]["fence_token"] == 2
    activation_evidence = store.read_evidence(
        entry["active_source_evidence"]["rebind_evidence_sha256"]
    )
    assert activation_evidence["action"] == "ACTIVATE"
    assert activation_evidence["registry_revision"] == activation.registry.to_dict()["revision"]
    assert activation_evidence["active_source_revision"] == 2
    assert activation_evidence["operation_epoch"] == 2
    assert activation_evidence["fence_token"] == 2
    assert len({alias["path"] for alias in entry["aliases"]}) == len(entry["aliases"])
    assert entry["active_source"] not in entry["aliases"]
    assert store.resolve_active_source(REPO_UUID).registry_source == entry["active_source"]

    moved = tmp_path / "linked-moved"
    linked.rename(moved)
    with pytest.raises(SourceAmbiguousError, match="source_ambiguous"):
        store.resolve_active_source(REPO_UUID)


def test_registry_recovery_is_monotonic_across_named_crash_schedules(tmp_path: Path) -> None:
    first_repo = _create_repo(tmp_path / "first", REPO_UUID)
    second_repo = _create_repo(tmp_path / "second", SECOND_UUID)
    base_state = tmp_path / "base-state"
    RegistryStore(base_state, capabilities=SUPPORTED).enroll(
        discover_source(first_repo),
        _authorization(IdentityAction.ENROLL, "base"),
        expected_revision=0,
    )

    for event in (
        "registry:pending_durable",
        "registry:previous_durable",
        "registry:current_replaced",
        "registry:current_durable",
        "registry:pending_cleared",
    ):
        case_state = tmp_path / event.replace(":", "-")
        shutil.copytree(base_state, case_state)
        crash = CrashAt(event)
        store = RegistryStore(case_state, capabilities=SUPPORTED, fault_hook=crash)
        with pytest.raises((InjectedFault, CommitUnknown)):
            store.enroll(
                discover_source(second_repo),
                _authorization(IdentityAction.ENROLL, event),
                expected_revision=1,
            )
        assert crash.fired

        recovered_store = RegistryStore(case_state, capabilities=SUPPORTED)
        recovered = recovered_store.load()
        assert recovered.to_dict()["revision"] in {1, 2}
        if recovered.to_dict()["revision"] == 1:
            recovered = recovered_store.enroll(
                discover_source(second_repo),
                _authorization(IdentityAction.ENROLL, f"retry-{event}"),
                expected_revision=1,
            )
        assert recovered.to_dict()["revision"] == 2
        assert {item["repo_uuid"] for item in recovered.to_dict()["workspaces"]} == {
            REPO_UUID,
            SECOND_UUID,
        }


def test_lease_acquire_recovers_durable_pending_registry_before_cas(tmp_path: Path) -> None:
    first_repo = _create_repo(tmp_path / "first", REPO_UUID)
    second_repo = _create_repo(tmp_path / "second", SECOND_UUID)
    state_root = tmp_path / "state"
    RegistryStore(state_root, capabilities=SUPPORTED).enroll(
        discover_source(first_repo),
        _authorization(IdentityAction.ENROLL, "first"),
        expected_revision=0,
    )
    crash = CrashAt("registry:pending_durable")
    crashing = RegistryStore(state_root, capabilities=SUPPORTED, fault_hook=crash)
    with pytest.raises(CommitUnknown):
        crashing.enroll(
            discover_source(second_repo),
            _authorization(IdentityAction.ENROLL, "second"),
            expected_revision=1,
        )
    assert crash.fired
    assert (state_root / "registry.pending.json").exists()

    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    workspace_root = state_root / "workspaces" / REPO_UUID
    before_workspace = _tree_snapshot(workspace_root)

    with pytest.raises(RevisionConflict, match="expected 1, found 2"):
        leases.acquire(
            REPO_UUID,
            "BUILD",
            leases.current_owner(),
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=1,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 2, tzinfo=timezone.utc),
            monotonic_ns=100,
            ttl_ns=50,
        )

    assert registry.load().to_dict()["revision"] == 2
    assert not (state_root / "registry.pending.json").exists()
    assert _tree_snapshot(workspace_root) == before_workspace


def test_lease_allocation_fails_before_workspace_write_when_active_source_is_missing(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    workspace_root = state_root / "workspaces" / REPO_UUID
    before_workspace = _tree_snapshot(workspace_root)
    repo.rename(tmp_path / "moved")
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)

    with pytest.raises(SourceAmbiguousError, match="source_ambiguous"):
        leases.acquire(
            REPO_UUID,
            "BUILD",
            leases.current_owner(),
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=1,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 2, tzinfo=timezone.utc),
            monotonic_ns=100,
            ttl_ns=50,
        )
    assert _tree_snapshot(workspace_root) == before_workspace


@pytest.mark.parametrize(
    ("operation", "error_number"),
    [
        ("write", errno.ENOSPC),
        ("write", errno.EDQUOT),
        ("write", errno.EIO),
        ("fsync", errno.EIO),
        ("replace", errno.EIO),
    ],
)
def test_registry_syscall_faults_preserve_last_durable_revision(
    tmp_path: Path,
    operation: str,
    error_number: int,
) -> None:
    first_repo = _create_repo(tmp_path / "first", REPO_UUID)
    second_repo = _create_repo(tmp_path / "second", SECOND_UUID)
    state_root = tmp_path / "state"
    RegistryStore(state_root, capabilities=SUPPORTED).enroll(
        discover_source(first_repo),
        _authorization(IdentityAction.ENROLL, "first"),
        expected_revision=0,
    )
    failing = RegistryStore(
        state_root,
        capabilities=SUPPORTED,
        syscalls=FailOnceSyscalls(operation, error_number),
    )

    with pytest.raises(OSError) as failure:
        failing.enroll(
            discover_source(second_repo),
            _authorization(IdentityAction.ENROLL, "second"),
            expected_revision=1,
        )
    assert failure.value.errno == error_number

    recovered = RegistryStore(state_root, capabilities=SUPPORTED).load()
    assert recovered.to_dict()["revision"] in {1, 2}
    assert recovered.to_dict()["revision"] >= 1


def test_registry_retries_eintr_and_short_writes(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    store = RegistryStore(
        tmp_path / "state",
        capabilities=SUPPORTED,
        syscalls=ShortWriteAndEintrSyscalls(),
    )

    document = store.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "short-write"),
        expected_revision=0,
    )

    assert document.to_dict()["revision"] == 1
    assert store.load().canonical == document.canonical


@pytest.mark.parametrize(
    ("operation", "error_number"),
    [
        ("write", errno.ENOSPC),
        ("write", errno.EDQUOT),
        ("write", errno.EIO),
        ("fsync", errno.EIO),
        ("replace", errno.EIO),
    ],
)
def test_lease_allocator_syscall_faults_preserve_the_initialized_fence_floor(
    tmp_path: Path,
    operation: str,
    error_number: int,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    failing = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        syscalls=FailOnceSyscalls(operation, error_number),
    )

    with pytest.raises(OSError) as failure:
        failing.acquire(
            REPO_UUID,
            "BUILD",
            failing.current_owner(),
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=1,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 2, tzinfo=timezone.utc),
            monotonic_ns=100,
            ttl_ns=50,
        )
    assert failure.value.errno == error_number

    recovered = LeaseStore(state_root, registry, capabilities=SUPPORTED).inspect(REPO_UUID)
    assert recovered.revision == 1
    assert recovered.fence_high_watermark == 1
    assert recovered.operation_epoch == 1
    assert recovered.leases == {}
    assert recovered.lease_epochs == {}


def test_lease_allocator_retries_eintr_and_short_writes(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        syscalls=ShortWriteAndEintrSyscalls(),
    )

    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 2, tzinfo=timezone.utc),
        monotonic_ns=100,
        ttl_ns=50,
    )

    assert grant.lease.to_dict()["fence_token"] == 2
    assert leases.assert_current(grant, monotonic_ns=120)


def test_failed_directory_sync_reports_commit_unknown_and_recovers_monotonically(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "state"
    stable = DurableStateRoot(root_path, capabilities=SUPPORTED)
    first_payload = canonical_json_bytes({"revision": 1})
    stable.commit_record(
        label="counter",
        current="counter.json",
        previous="counter.previous.json",
        pending="counter.pending.json",
        payload=first_payload,
        decoder=json.loads,
    )

    failing = DurableStateRoot(
        root_path,
        capabilities=SUPPORTED,
        syscalls=FailFsyncCallSyscalls(2),
    )
    with pytest.raises(CommitUnknown, match="recovery intent became visible"):
        failing.commit_record(
            label="counter",
            current="counter.json",
            previous="counter.previous.json",
            pending="counter.pending.json",
            payload=canonical_json_bytes({"revision": 2}),
            decoder=json.loads,
        )

    recovered = DurableStateRoot(root_path, capabilities=SUPPORTED).recover_record(
        label="counter",
        current="counter.json",
        previous="counter.previous.json",
        pending="counter.pending.json",
        decoder=json.loads,
        revision=lambda value: int(value["revision"]),
    )
    assert recovered == {"revision": 2}


def test_record_recovery_rejects_dangling_pending_authority(tmp_path: Path) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)
    current = canonical_json_bytes({"revision": 1})
    state.atomic_replace_bytes("counter.json", current, label="counter:current")
    pending = state.path("counter.pending.json")
    pending.symlink_to(tmp_path / "missing-pending-record")

    with pytest.raises(StateCorrupt, match="pending commit is corrupt"):
        state.recover_record(
            label="counter",
            current="counter.json",
            previous="counter.previous.json",
            pending="counter.pending.json",
            decoder=json.loads,
            revision=lambda value: int(value["revision"]),
        )

    assert pending.is_symlink()
    assert state.read_existing_bytes("counter.json") == current


def test_registry_mutations_serialize_across_processes(tmp_path: Path) -> None:
    first_repo = _create_repo(tmp_path / "first", REPO_UUID)
    second_repo = _create_repo(tmp_path / "second", SECOND_UUID)
    state_root = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_enroll_process,
            args=(str(state_root), str(repo), f"process-{index}", barrier, results),
        )
        for index, repo in enumerate((first_repo, second_repo), start=1)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 0

    outcomes = sorted(results.get(timeout=5) for _ in processes)
    assert [kind for kind, _ in outcomes] == ["ok", "ok"]
    document = RegistryStore(state_root, capabilities=SUPPORTED).load()
    assert document.to_dict()["revision"] == 2
    assert {item["repo_uuid"] for item in document.to_dict()["workspaces"]} == {
        REPO_UUID,
        SECOND_UUID,
    }


def test_registry_shared_readers_overlap_while_writer_waits(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    context = multiprocessing.get_context("spawn")
    reader_releases = [context.Event(), context.Event()]
    reader_started = [context.Event(), context.Event()]
    reader_acquired = [context.Event(), context.Event()]
    writer_started = context.Event()
    writer_acquired = context.Event()
    writer_release = context.Event()
    processes = [
        context.Process(
            target=_hold_registry_lock,
            kwargs={
                "state_root": str(state_root),
                "shared": True,
                "started": reader_started[index],
                "acquired": reader_acquired[index],
                "release": reader_releases[index],
            },
        )
        for index in range(2)
    ]
    writer = context.Process(
        target=_hold_registry_lock,
        kwargs={
            "state_root": str(state_root),
            "shared": False,
            "started": writer_started,
            "acquired": writer_acquired,
            "release": writer_release,
        },
    )
    try:
        processes[0].start()
        assert reader_started[0].wait(timeout=5)
        assert reader_acquired[0].wait(timeout=5)
        processes[1].start()
        assert reader_started[1].wait(timeout=5)
        readers_overlapped = reader_acquired[1].wait(timeout=2)
        if not readers_overlapped:
            reader_releases[0].set()
            assert reader_acquired[1].wait(timeout=5)

        writer.start()
        assert writer_started.wait(timeout=5)
        writer_waited = not writer_acquired.wait(timeout=1)
        reader_releases[0].set()
        reader_releases[1].set()
        assert writer_acquired.wait(timeout=5)
        writer_release.set()
    finally:
        for event in (*reader_releases, writer_release):
            event.set()
        for process in (*processes, writer):
            if process.pid is not None:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

    assert readers_overlapped
    assert writer_waited
    assert all(process.exitcode == 0 for process in (*processes, writer))


def test_fenced_lease_allocation_heartbeat_acceptance_and_stale_rejection(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    owner = leases.current_owner()
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 2, tzinfo=timezone.utc),
        monotonic_ns=1_000,
        ttl_ns=100,
    )

    assert grant.lease.to_dict()["fence_token"] == 2
    assert grant.operation_epoch == 2
    assert grant.migration_epoch == 0
    assert grant.active_source_revision == 1
    assert leases.assert_current(grant, monotonic_ns=1_050).sha256 == grant.lease.sha256

    forged_value = grant.lease.to_dict()
    forged_value["owner"]["process_start_id"] = "200:forged"
    forged = LeaseGrant(
        lease=cast(FencedLease, FencedLease.from_mapping(forged_value)),
        registry_revision=grant.registry_revision,
        active_source_revision=grant.active_source_revision,
        operation_epoch=grant.operation_epoch,
        migration_epoch=grant.migration_epoch,
    )
    with pytest.raises(StaleLease, match="stale_owner"):
        leases.assert_current(forged, monotonic_ns=1_050)

    heartbeat = leases.heartbeat(
        grant,
        heartbeat_at=datetime(2026, 7, 16, 14, 59, tzinfo=timezone.utc),
        monotonic_ns=1_060,
        ttl_ns=100,
    )
    assert heartbeat.lease.to_dict()["fence_token"] == 2
    assert heartbeat.lease.to_dict()["heartbeat_at"] == "2026-07-16T14:59:00Z"
    assert leases.assert_current(heartbeat, monotonic_ns=1_159)

    with pytest.raises(LeaseExpired, match="lease_expired"):
        leases.assert_current(heartbeat, monotonic_ns=1_160)
    with pytest.raises(LeaseExpired):
        leases.heartbeat(
            heartbeat,
            heartbeat_at=datetime(2026, 7, 16, 15, 3, tzinfo=timezone.utc),
            monotonic_ns=1_161,
            ttl_ns=100,
        )

    successor_owner = leases.current_owner()
    successor = leases.acquire(
        REPO_UUID,
        "PROMOTE",
        successor_owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=2,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 3, tzinfo=timezone.utc),
        monotonic_ns=1_161,
        ttl_ns=100,
    )
    assert successor.lease.to_dict()["fence_token"] == 3
    assert successor.operation_epoch == 3
    with pytest.raises(StaleLease, match="stale_fence"):
        leases.assert_current(heartbeat, monotonic_ns=1_162)


def test_workspace_and_semantic_domains_keep_independent_operation_epochs(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    owner = leases.current_owner()
    workspace = leases.acquire(
        REPO_UUID,
        "BUILD",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 3, tzinfo=timezone.utc),
        monotonic_ns=1_000,
        ttl_ns=1_000,
    )
    semantic = leases.acquire(
        REPO_UUID,
        "SEMANTIC_CLAIM",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=2,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 3, tzinfo=timezone.utc),
        monotonic_ns=1_010,
        ttl_ns=1_000,
    )

    assert workspace.operation_epoch == 2
    assert semantic.operation_epoch == 3
    assert leases.assert_current(workspace, monotonic_ns=1_020)
    assert leases.assert_current(semantic, monotonic_ns=1_020)
    heartbeat = leases.heartbeat(
        workspace,
        heartbeat_at=datetime(2026, 7, 16, 15, 4, tzinfo=timezone.utc),
        monotonic_ns=1_030,
        ttl_ns=1_000,
    )
    assert heartbeat.operation_epoch == workspace.operation_epoch
    assert leases.assert_current(heartbeat, monotonic_ns=1_040)

    leases.release(semantic)
    leases.release(heartbeat)
    state = leases.inspect(REPO_UUID)
    assert state.leases == {}
    assert state.lease_epochs == {}
    assert state.operation_epoch == 3


def test_migration_invalidates_semantic_commit_but_not_exact_owner_cleanup(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    owner = leases.current_owner()
    semantic = leases.acquire(
        REPO_UUID,
        "SEMANTIC_CLAIM",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 3, tzinfo=timezone.utc),
        monotonic_ns=1_000,
        ttl_ns=1_000,
    )
    migration = leases.acquire(
        REPO_UUID,
        "MIGRATE",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=2,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 4, tzinfo=timezone.utc),
        monotonic_ns=1_010,
        ttl_ns=1_000,
    )

    with pytest.raises(StaleLease, match="stale_epoch"):
        leases.assert_current(semantic, monotonic_ns=1_020)
    after_semantic_release = leases.release(semantic)
    assert set(after_semantic_release.leases) == {"workspace"}
    after_migration_release = leases.release(migration)
    assert after_migration_release.leases == {}
    assert after_migration_release.lease_epochs == {}


def test_trusted_runtime_identity_rejects_forged_reboot_and_pid_reuse(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    before = LeaseOwner(boot_id="boot-before", pid=700, process_start_id="700:1")
    identity = MutableLeaseIdentity(before)
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        identity_provider=identity,
    )
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        before,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 4, tzinfo=timezone.utc),
        monotonic_ns=1_000,
        ttl_ns=1_000,
    )

    forged_reboot = LeaseOwner(boot_id="boot-forged", pid=701, process_start_id="701:1")
    with pytest.raises(StaleLease, match="stale_owner"):
        leases.acquire(
            REPO_UUID,
            "PROMOTE",
            forged_reboot,
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=2,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 5, tzinfo=timezone.utc),
            monotonic_ns=1_010,
            ttl_ns=100,
        )
    assert leases.assert_current(grant, monotonic_ns=1_020)

    identity.owner = LeaseOwner(boot_id="boot-before", pid=700, process_start_id="700:2")
    with pytest.raises(StaleLease, match="stale_owner"):
        leases.assert_current(grant, monotonic_ns=1_030)
    with pytest.raises(LeaseBusy):
        leases.acquire(
            REPO_UUID,
            "PROMOTE",
            identity.owner,
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=2,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 5, tzinfo=timezone.utc),
            monotonic_ns=1_030,
            ttl_ns=100,
        )

    identity.owner = LeaseOwner(boot_id="boot-after", pid=700, process_start_id="700:1")
    with pytest.raises(StaleLease, match="stale_owner"):
        leases.assert_current(grant, monotonic_ns=5)
    with pytest.raises(StaleLease, match="stale_owner"):
        leases.heartbeat(
            grant,
            heartbeat_at=datetime(2026, 7, 16, 15, 6, tzinfo=timezone.utc),
            monotonic_ns=5,
            ttl_ns=100,
        )
    with pytest.raises(StaleLease, match="stale_owner"):
        leases.release(grant)
    assert set(leases.inspect(REPO_UUID).leases) == {"workspace"}

    successor = leases.acquire(
        REPO_UUID,
        "PROMOTE",
        identity.owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=2,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 6, tzinfo=timezone.utc),
        monotonic_ns=5,
        ttl_ns=100,
    )
    assert successor.lease.to_dict()["fence_token"] == 3
    assert leases.assert_current(successor, monotonic_ns=6)


def test_fence_tokens_do_not_reset_after_release_reboot_or_commit_unknown(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    owner = LeaseOwner(boot_id="boot-a", pid=300, process_start_id="300:1")
    identity = MutableLeaseIdentity(owner)
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        identity_provider=identity,
    )
    first = leases.acquire(
        REPO_UUID,
        "BUILD",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 4, tzinfo=timezone.utc),
        monotonic_ns=10_000,
        ttl_ns=1_000,
    )
    leases.release(first)

    identity.owner = LeaseOwner(boot_id="boot-a", pid=301, process_start_id="301:1")
    crash = CrashAt("workspace:current_replaced")
    crashing = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        fault_hook=crash,
        identity_provider=identity,
    )
    with pytest.raises((InjectedFault, CommitUnknown)):
        crashing.acquire(
            REPO_UUID,
            "MIGRATE",
            identity.owner,
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=2,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 5, tzinfo=timezone.utc),
            monotonic_ns=11_000,
            ttl_ns=1_000,
        )
    assert crash.fired

    identity.owner = LeaseOwner(boot_id="boot-b", pid=300, process_start_id="300:1")
    recovered = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        identity_provider=identity,
    )
    state = recovered.inspect(REPO_UUID)
    assert state.fence_high_watermark >= 3
    assert state.operation_epoch >= 3
    assert state.migration_epoch >= 1

    successor = recovered.acquire(
        REPO_UUID,
        "BUILD",
        identity.owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        acquired_at=datetime(2026, 7, 16, 15, 6, tzinfo=timezone.utc),
        monotonic_ns=5,
        ttl_ns=100,
    )
    assert successor.lease.to_dict()["fence_token"] > state.fence_high_watermark
    with pytest.raises(StaleLease):
        recovered.assert_current(first, monotonic_ns=6)


def test_missing_initialized_workspace_records_fail_closed_without_fence_reset(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 6, tzinfo=timezone.utc),
        monotonic_ns=100,
        ttl_ns=50,
    )
    assert grant.lease.to_dict()["fence_token"] == 2
    leases.release(grant)

    workspace_root = state_root / "workspaces" / REPO_UUID
    for name in (
        "workspace.json",
        "workspace.previous.json",
        "workspace.pending.json",
    ):
        (workspace_root / name).unlink(missing_ok=True)

    with pytest.raises(StateCorrupt, match="all records are missing"):
        leases.inspect(REPO_UUID)
    with pytest.raises(StateCorrupt, match="all records are missing"):
        leases.acquire(
            REPO_UUID,
            "PROMOTE",
            leases.current_owner(),
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=2,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
            monotonic_ns=200,
            ttl_ns=50,
        )


@pytest.mark.parametrize(
    "event",
    [
        "workspace:pending_durable",
        "workspace:previous_durable",
        "workspace:current_replaced",
        "workspace:current_durable",
        "workspace:pending_cleared",
    ],
)
def test_workspace_lease_recovery_never_reuses_fences_across_crash_schedules(
    tmp_path: Path,
    event: str,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    base_state = tmp_path / "base-state"
    registry = RegistryStore(base_state, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(base_state, registry, capabilities=SUPPORTED)
    seed = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 5, tzinfo=timezone.utc),
        monotonic_ns=10,
        ttl_ns=10,
    )
    leases.release(seed)

    case_state = tmp_path / event.replace(":", "-")
    shutil.copytree(base_state, case_state)
    case_registry = RegistryStore(case_state, capabilities=SUPPORTED)
    crash = CrashAt(event)
    crashing = LeaseStore(
        case_state,
        case_registry,
        capabilities=SUPPORTED,
        fault_hook=crash,
    )
    with pytest.raises((InjectedFault, CommitUnknown)):
        crashing.acquire(
            REPO_UUID,
            "PROMOTE",
            crashing.current_owner(),
            expected_registry_revision=1,
            expected_active_source_revision=1,
            expected_operation_epoch=2,
            expected_migration_epoch=0,
            acquired_at=datetime(2026, 7, 16, 15, 6, tzinfo=timezone.utc),
            monotonic_ns=100,
            ttl_ns=50,
        )
    assert crash.fired

    recovered = LeaseStore(case_state, case_registry, capabilities=SUPPORTED)
    recovered_state = recovered.inspect(REPO_UUID)
    assert recovered_state.fence_high_watermark == 3
    assert recovered_state.operation_epoch == 3
    successor = recovered.acquire(
        REPO_UUID,
        "BUILD",
        recovered.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=3,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=150,
        ttl_ns=50,
    )
    assert successor.lease.to_dict()["fence_token"] == 4


def test_deterministic_concurrent_lease_race_has_one_owner_and_monotonic_fence(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    barrier = multiprocessing.Barrier(2)

    def contender(index: int) -> tuple[str, int | str]:
        leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
        barrier.wait(timeout=10)
        try:
            grant = leases.acquire(
                REPO_UUID,
                "BUILD",
                leases.current_owner(),
                expected_registry_revision=1,
                expected_active_source_revision=1,
                expected_operation_epoch=1,
                expected_migration_epoch=0,
                acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
                monotonic_ns=20_000,
                ttl_ns=1_000,
            )
            return ("acquired", grant.lease.to_dict()["fence_token"])
        except (LeaseBusy, RevisionConflict) as exc:
            return ("rejected", type(exc).__name__)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(contender, (1, 2)))

    assert [kind for kind, _ in outcomes] == ["acquired", "rejected"]
    assert [value for kind, value in outcomes if kind == "acquired"] == [2]
    state = LeaseStore(state_root, registry, capabilities=SUPPORTED).inspect(REPO_UUID)
    assert state.fence_high_watermark == 2
    assert state.operation_epoch == 2


def test_registry_before_workspace_lock_order_is_enforced_and_observable(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    events: list[str] = []
    registry = RegistryStore(state_root, capabilities=SUPPORTED, fault_hook=events.append)
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        fault_hook=events.append,
    )
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )

    with leases.workspace_lock(REPO_UUID):
        with pytest.raises(LockOrderError, match="registry.*workspace"):
            with registry.exclusive_lock():
                pass

    events.clear()
    with registry.exclusive_lock():
        with leases.workspace_lock(REPO_UUID):
            pass
    assert events.index("lock:registry:acquired") < events.index("lock:workspace:acquired")

    def assert_registry_encloses_workspace() -> None:
        assert events.index("lock:registry:acquired") < events.index(
            "lock:workspace:acquired"
        )
        assert events.index("lock:workspace:released") < events.index(
            "lock:registry:released"
        )

    events.clear()
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=20_000,
        ttl_ns=1_000,
    )
    assert_registry_encloses_workspace()

    events.clear()
    leases.assert_current(grant, monotonic_ns=20_100)
    assert_registry_encloses_workspace()

    events.clear()
    grant = leases.heartbeat(
        grant,
        heartbeat_at=datetime(2026, 7, 16, 15, 8, tzinfo=timezone.utc),
        monotonic_ns=20_200,
        ttl_ns=1_000,
    )
    assert_registry_encloses_workspace()

    events.clear()
    leases.inspect(REPO_UUID)
    assert_registry_encloses_workspace()

    events.clear()
    leases.release(grant)
    assert_registry_encloses_workspace()


@pytest.mark.parametrize("lock_name", ["registry", "workspace"])
def test_steady_state_lock_removal_cannot_split_serialization(
    tmp_path: Path,
    lock_name: str,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    lock_path = (
        state_root / "registry.lock"
        if lock_name == "registry"
        else state_root / "workspaces" / REPO_UUID / "workspace.lock"
    )
    holder = (
        registry.existing_exclusive_lock()
        if lock_name == "registry"
        else leases.read_only_workspace_lock(REPO_UUID)
    )
    contender_acquired = threading.Event()
    contender_failures: list[Exception] = []

    def contend() -> None:
        try:
            contender = (
                registry.exclusive_lock()
                if lock_name == "registry"
                else leases.workspace_lock(REPO_UUID)
            )
            with contender:
                contender_acquired.set()
        except Exception as exc:
            contender_failures.append(exc)

    with holder:
        lock_path.unlink()
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert not contender_acquired.is_set()
        assert len(contender_failures) == 1
        assert isinstance(contender_failures[0], StatePathError)
        assert not lock_path.exists()


@pytest.mark.parametrize("case", ["missing", "wrong_mode", "root_wrong_mode"])
def test_subsequent_enrollment_requires_existing_registry_lock_without_mutation(
    tmp_path: Path,
    case: str,
) -> None:
    first_repo = _create_repo(tmp_path / "first", REPO_UUID)
    second_repo = _create_repo(tmp_path / "second", SECOND_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(first_repo),
        _authorization(IdentityAction.ENROLL, "first"),
        expected_revision=0,
    )
    registry_lock = state_root / "registry.lock"
    if case == "missing":
        registry_lock.unlink()
    elif case == "wrong_mode":
        registry_lock.chmod(0o644)
    else:
        state_root.chmod(0o755)
    before = _tree_snapshot(state_root)

    with pytest.raises(StatePathError):
        registry.enroll(
            discover_source(second_repo),
            _authorization(IdentityAction.ENROLL, "second"),
            expected_revision=1,
        )

    assert _tree_snapshot(state_root) == before


def test_concurrent_initialization_cannot_recreate_removed_registry_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_repo = _create_repo(tmp_path / "first", REPO_UUID)
    second_repo = _create_repo(tmp_path / "second", SECOND_UUID)
    state_root = tmp_path / "state"
    first_acquired = threading.Event()
    allow_first_commit = threading.Event()

    def first_fault(event: str) -> None:
        if event == "lock:registry:acquired":
            first_acquired.set()
            assert allow_first_commit.wait(timeout=5)

    first = RegistryStore(
        state_root,
        capabilities=SUPPORTED,
        fault_hook=first_fault,
    )
    first_failures: list[Exception] = []

    def enroll_first() -> None:
        try:
            first.enroll(
                discover_source(first_repo),
                _authorization(IdentityAction.ENROLL, "first"),
                expected_revision=0,
            )
        except Exception as exc:
            first_failures.append(exc)

    first_thread = threading.Thread(target=enroll_first, name="first-enrollment")
    first_thread.start()
    assert first_acquired.wait(timeout=5)
    registry_lock = state_root / "registry.lock"
    registry_lock.unlink()

    second = RegistryStore(state_root, capabilities=SUPPORTED)
    second_attempted_initialization = threading.Event()
    second_inspected = threading.Event()
    allow_second_inspection = threading.Event()
    second_failures: list[Exception] = []
    original_probe = second.state.private_file_exists
    original_initialization_lock = second.state.initialization_lock

    @contextmanager
    def track_initialization_attempt(*, rank: int, name: str) -> Iterator[None]:
        second_attempted_initialization.set()
        with original_initialization_lock(rank=rank, name=name):
            yield

    def pause_at_first_probe(relative: str | Path) -> bool:
        second_inspected.set()
        assert allow_second_inspection.wait(timeout=5)
        return original_probe(relative)

    monkeypatch.setattr(second.state, "private_file_exists", pause_at_first_probe)
    monkeypatch.setattr(
        second.state,
        "initialization_lock",
        track_initialization_attempt,
    )

    def enroll_second() -> None:
        try:
            second.enroll(
                discover_source(second_repo),
                _authorization(IdentityAction.ENROLL, "second"),
                expected_revision=1,
            )
        except Exception as exc:
            second_failures.append(exc)

    second_thread = threading.Thread(target=enroll_second, name="second-enrollment")
    second_thread.start()
    assert second_attempted_initialization.wait(timeout=5)
    assert not second_inspected.is_set()

    allow_first_commit.set()
    first_thread.join(timeout=5)
    assert not first_thread.is_alive()
    assert first_failures == []
    assert second_inspected.wait(timeout=5)
    before = _tree_snapshot(state_root)
    allow_second_inspection.set()
    second_thread.join(timeout=5)

    assert not second_thread.is_alive()
    assert len(second_failures) == 1
    assert isinstance(second_failures[0], StatePathError)
    assert not registry_lock.exists()
    assert _tree_snapshot(state_root) == before


@pytest.mark.parametrize("lock_name", ["registry", "workspace"])
def test_lease_acquisition_deadline_bounds_mutating_lock_wait(
    tmp_path: Path,
    lock_name: str,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    workspace_path = state_root / "workspaces" / REPO_UUID / "workspace.json"
    workspace_before = workspace_path.read_bytes()
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_mutating_runtime_lock,
        kwargs={
            "state_root": str(state_root),
            "repo_uuid": REPO_UUID,
            "lock_name": lock_name,
            "acquired": acquired,
            "release": release,
        },
    )
    release_timer = threading.Timer(1.0, release.set)
    try:
        holder.start()
        assert acquired.wait(timeout=5)
        release_timer.start()
        started = time.monotonic()
        with pytest.raises(LockTimeout) as raised:
            leases.acquire(
                REPO_UUID,
                "ROLLBACK",
                leases.current_owner(),
                expected_registry_revision=1,
                expected_active_source_revision=1,
                expected_operation_epoch=1,
                expected_migration_epoch=0,
                acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
                monotonic_ns=20_000,
                ttl_ns=30_000_000_000,
                deadline_ns=time.monotonic_ns() + 100_000_000,
            )
        assert time.monotonic() - started < 0.5
        assert raised.value.phase == "acquire"
        assert raised.value.kind == lock_name
    finally:
        release.set()
        release_timer.cancel()
        if holder.pid is not None:
            holder.join(timeout=5)
            if holder.is_alive():
                holder.terminate()
                holder.join(timeout=5)

    assert workspace_path.read_bytes() == workspace_before


def test_repair_lease_threads_deadline_through_source_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    owner = LeaseOwner(boot_id="boot-repair", pid=700, process_start_id="700:1")
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        identity_provider=MutableLeaseIdentity(owner),
    )
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    observed: list[int | None] = []
    original_discover_source = lease_module.discover_source

    def track_discover_source(
        source_root: Path,
        *,
        deadline_ns: int | None = None,
        max_bytes: int | None = None,
    ) -> SourceIdentity:
        observed.append(deadline_ns)
        return original_discover_source(
            source_root,
            deadline_ns=deadline_ns,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(lease_module, "discover_source", track_discover_source)
    grant = leases.acquire(
        REPO_UUID,
        "REPAIR",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=20_000,
        ttl_ns=30_000_000_000,
        deadline_ns=deadline_ns,
    )

    assert observed == [deadline_ns]
    leases.release(grant, deadline_ns=deadline_ns)


def test_repair_lease_preserves_unrelated_atomic_temporaries(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    owner = LeaseOwner(boot_id="boot-repair", pid=700, process_start_id="700:1")
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        identity_provider=MutableLeaseIdentity(owner),
    )
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    workspace = state_root / "workspaces" / REPO_UUID
    before_acquire = workspace / (".pointers.json.tmp-999-" + "a" * 32)
    before_acquire.write_bytes(b"pointer temporary")
    before_acquire.chmod(0o600)

    grant = leases.acquire(
        REPO_UUID,
        "REPAIR",
        owner,
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=20_000,
        ttl_ns=30_000_000_000,
    )

    assert before_acquire.read_bytes() == b"pointer temporary"
    before_release = workspace / (".staged-build.json.tmp-999-" + "b" * 32)
    before_release.write_bytes(b"staged temporary")
    before_release.chmod(0o600)
    leases.release(grant)
    assert before_acquire.read_bytes() == b"pointer temporary"
    assert before_release.read_bytes() == b"staged temporary"


@pytest.mark.parametrize("lock_name", ["registry", "workspace"])
def test_current_operation_deadline_bounds_mutating_lock_wait(
    tmp_path: Path,
    lock_name: str,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=20_000,
        ttl_ns=10_000,
    )
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_mutating_runtime_lock,
        kwargs={
            "state_root": str(state_root),
            "repo_uuid": REPO_UUID,
            "lock_name": lock_name,
            "acquired": acquired,
            "release": release,
        },
    )
    release_timer = threading.Timer(1.0, release.set)
    try:
        holder.start()
        assert acquired.wait(timeout=5)
        release_timer.start()
        started = time.monotonic()
        with pytest.raises(LockTimeout) as raised:
            with leases.current_operation(
                grant,
                monotonic_ns=20_001,
                deadline_ns=time.monotonic_ns() + 100_000_000,
            ):
                pytest.fail("expired operation lock unexpectedly acquired")
        assert time.monotonic() - started < 0.5
        assert raised.value.phase == "acquire"
        assert raised.value.kind == lock_name
    finally:
        release.set()
        release_timer.cancel()
        if holder.pid is not None:
            holder.join(timeout=5)
            if holder.is_alive():
                holder.terminate()
                holder.join(timeout=5)


def test_current_operation_deadline_reaches_recovery_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=20_000,
        ttl_ns=10_000,
    )
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    observed: list[tuple[str, object]] = []
    registry_recover = registry.state.recover_record
    workspace_recover = leases.state.recover_record

    def track_registry_recovery(**kwargs: Any) -> object:
        observed.append((str(kwargs["label"]), kwargs.get("deadline_ns")))
        return registry_recover(**kwargs)

    def track_workspace_recovery(**kwargs: Any) -> object:
        observed.append((str(kwargs["label"]), kwargs.get("deadline_ns")))
        return workspace_recover(**kwargs)

    monkeypatch.setattr(registry.state, "recover_record", track_registry_recovery)
    monkeypatch.setattr(leases.state, "recover_record", track_workspace_recovery)

    with leases.current_operation(
        grant,
        monotonic_ns=20_001,
        deadline_ns=deadline_ns,
    ):
        pass

    assert observed == [
        ("registry", deadline_ns),
        ("workspace", deadline_ns),
        (f"staged-build:{REPO_UUID}", deadline_ns),
    ]


def test_release_deadline_reaches_recovery_and_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 7, tzinfo=timezone.utc),
        monotonic_ns=20_000,
        ttl_ns=10_000,
    )
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    observed: list[tuple[str, object]] = []
    registry_recover = registry.state.recover_record
    workspace_recover = leases.state.recover_record
    workspace_commit = leases.state.commit_record

    def track_registry_recovery(**kwargs: Any) -> object:
        observed.append((f"recover:{kwargs['label']}", kwargs.get("deadline_ns")))
        return registry_recover(**kwargs)

    def track_workspace_recovery(**kwargs: Any) -> object:
        observed.append((f"recover:{kwargs['label']}", kwargs.get("deadline_ns")))
        return workspace_recover(**kwargs)

    def track_workspace_commit(**kwargs: Any) -> object:
        observed.append((f"commit:{kwargs['label']}", kwargs.get("deadline_ns")))
        return workspace_commit(**kwargs)

    monkeypatch.setattr(registry.state, "recover_record", track_registry_recovery)
    monkeypatch.setattr(leases.state, "recover_record", track_workspace_recovery)
    monkeypatch.setattr(leases.state, "commit_record", track_workspace_commit)

    leases.release(grant, deadline_ns=deadline_ns)

    assert observed == [
        ("recover:registry", deadline_ns),
        ("recover:workspace", deadline_ns),
        ("commit:workspace", deadline_ns),
    ]


def test_record_recovery_honors_expired_deadline(tmp_path: Path) -> None:
    state = DurableStateRoot(tmp_path / "state", capabilities=SUPPORTED)

    with pytest.raises(LockTimeout):
        state.recover_record(
            label="counter",
            current="counter.json",
            previous="counter.previous.json",
            pending="counter.pending.json",
            decoder=json.loads,
            revision=lambda value: int(value["revision"]),
            deadline_ns=time.monotonic_ns(),
        )


def test_record_commit_does_not_become_visible_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "commit-state"
    root.mkdir(mode=0o700)
    syscalls = ExpireAfterFirstFsyncSyscalls()
    state = DurableStateRoot(root, capabilities=SUPPORTED, syscalls=syscalls)
    monkeypatch.setattr(
        "graphify.workspace.persistence.time.monotonic_ns",
        lambda: 2 if syscalls.expired else 0,
    )

    with pytest.raises(LockTimeout):
        state.commit_record(
            label="counter",
            current="counter.json",
            previous="counter.previous.json",
            pending="counter.pending.json",
            payload=canonical_json_bytes({"revision": 1}),
            decoder=json.loads,
            deadline_ns=1,
        )

    assert list(root.iterdir()) == []


def test_record_recovery_does_not_become_visible_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recovery-state"
    stable = DurableStateRoot(root, capabilities=SUPPORTED)
    current = canonical_json_bytes({"revision": 1})
    pending = canonical_json_bytes({"revision": 2})
    stable.atomic_replace_bytes("counter.json", current, label="counter:current")
    stable.atomic_replace_bytes(
        "counter.pending.json",
        pending,
        label="counter:pending",
    )
    syscalls = ExpireAfterFirstFsyncSyscalls()
    state = DurableStateRoot(root, capabilities=SUPPORTED, syscalls=syscalls)
    monkeypatch.setattr(
        "graphify.workspace.persistence.time.monotonic_ns",
        lambda: 2 if syscalls.expired else 0,
    )

    with pytest.raises(LockTimeout):
        state.recover_record(
            label="counter",
            current="counter.json",
            previous="counter.previous.json",
            pending="counter.pending.json",
            decoder=json.loads,
            revision=lambda value: int(value["revision"]),
            deadline_ns=1,
        )

    assert state.read_existing_bytes("counter.json") == current
    assert state.read_existing_bytes("counter.pending.json") == pending
    assert not state.path("counter.previous.json").exists()


def test_unlink_and_sync_does_not_become_visible_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = False

    def expire_before_unlink(event: str) -> None:
        nonlocal expired
        if event == "test:deadline:before_unlink":
            expired = True

    state = DurableStateRoot(
        tmp_path / "unlink-state",
        capabilities=SUPPORTED,
        fault_hook=expire_before_unlink,
    )
    relative = Path("records") / "pending.json"
    state.ensure_directory(relative.parent)
    target = state.path(relative)
    target.write_bytes(b"pending\n")
    target.chmod(0o600)
    monkeypatch.setattr(
        "graphify.workspace.persistence.time.monotonic_ns",
        lambda: 2 if expired else 0,
    )

    with pytest.raises(LockTimeout):
        state.unlink_and_sync(
            relative,
            label="test:deadline",
            deadline_ns=1,
        )

    assert target.read_bytes() == b"pending\n"


def test_registry_and_lease_operations_never_write_source_checkouts(tmp_path: Path) -> None:
    original = _create_repo(tmp_path / "original", REPO_UUID)
    linked = _linked_worktree(original, tmp_path / "linked", "linked-purity")
    before = {path: _tree_snapshot(path) for path in (original, linked)}
    state_root = tmp_path / "external-state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    enrolled = registry.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    rebound = registry.rebind(
        discover_source(linked),
        _authorization(IdentityAction.REBIND, "rebind"),
        expected_revision=enrolled.to_dict()["revision"],
    )
    activation = registry.activate_source(
        discover_source(linked),
        _authorization(IdentityAction.ACTIVATE, "activate"),
        leases=leases,
        owner=leases.current_owner(),
        expected_registry_revision=rebound.to_dict()["revision"],
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 8, tzinfo=timezone.utc),
        monotonic_ns=30_000,
        ttl_ns=1_000,
    )
    entry = _workspace_entry(activation.registry)
    grant = leases.acquire(
        REPO_UUID,
        "SEMANTIC_CLAIM",
        leases.current_owner(),
        expected_registry_revision=activation.registry.to_dict()["revision"],
        expected_active_source_revision=entry["active_source_revision"],
        expected_operation_epoch=activation.grant.operation_epoch,
        expected_migration_epoch=activation.grant.migration_epoch,
        acquired_at=datetime(2026, 7, 16, 15, 9, tzinfo=timezone.utc),
        monotonic_ns=31_000,
        ttl_ns=1_000,
    )
    assert leases.assert_current(grant, monotonic_ns=31_500)
    leases.release(grant)

    assert {path: _tree_snapshot(path) for path in (original, linked)} == before
    for path in state_root.rglob("*"):
        assert original not in path.parents
        assert linked not in path.parents
        expected_mode = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.lstat().st_mode) == expected_mode


def test_registry_rejects_wrong_mode_and_hardlinked_state_records(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    source = discover_source(repo)

    wrong_mode_root = tmp_path / "wrong-mode-state"
    wrong_mode = RegistryStore(wrong_mode_root, capabilities=SUPPORTED)
    wrong_mode.enroll(
        source,
        _authorization(IdentityAction.ENROLL, "wrong-mode"),
        expected_revision=0,
    )
    (wrong_mode_root / "registry.json").chmod(0o644)
    with pytest.raises(StateCorrupt, match="registry"):
        wrong_mode.load()

    hardlink_root = tmp_path / "hardlink-state"
    hardlinked = RegistryStore(hardlink_root, capabilities=SUPPORTED)
    hardlinked.enroll(
        source,
        _authorization(IdentityAction.ENROLL, "hardlink"),
        expected_revision=0,
    )
    os.link(hardlink_root / "registry.json", tmp_path / "registry-hardlink")
    with pytest.raises(StateCorrupt, match="registry"):
        hardlinked.load()


def test_registry_runtime_rejects_alias_ambiguity_and_missing_evidence(tmp_path: Path) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    source = discover_source(repo)

    ambiguous_root = tmp_path / "ambiguous-state"
    ambiguous = RegistryStore(ambiguous_root, capabilities=SUPPORTED)
    document = ambiguous.enroll(
        source,
        _authorization(IdentityAction.ENROLL, "ambiguous"),
        expected_revision=0,
    ).to_dict()
    document["workspaces"][0]["aliases"].append(document["workspaces"][0]["active_source"])
    (ambiguous_root / "registry.json").write_bytes(canonical_json_bytes(document))
    with pytest.raises(StateCorrupt, match="active source is also an alias"):
        ambiguous.load()

    missing_root = tmp_path / "missing-evidence-state"
    missing = RegistryStore(missing_root, capabilities=SUPPORTED)
    enrolled = missing.enroll(
        source,
        _authorization(IdentityAction.ENROLL, "missing-evidence"),
        expected_revision=0,
    ).to_dict()
    evidence_digest = enrolled["workspaces"][0]["active_source"]["remote_aliases"][0][
        "evidence_sha256"
    ]
    (missing_root / "evidence" / f"{evidence_digest}.json").unlink()
    with pytest.raises(StateCorrupt, match="evidence"):
        missing.load()


@pytest.mark.parametrize("unsafe_kind", ("record_mode", "parent_link"))
def test_registry_evidence_read_preserves_digest_context_for_unsafe_state(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    document = store.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "evidence-read-context"),
        expected_revision=0,
    ).to_dict()
    digest = document["workspaces"][0]["uuid_enrollment"][
        "immutable_evidence_sha256"
    ]
    evidence_directory = state_root / "evidence"
    external: Path | None = None
    if unsafe_kind == "record_mode":
        (evidence_directory / f"{digest}.json").chmod(0o644)
    else:
        external = tmp_path / "external-evidence"
        evidence_directory.rename(external)
        evidence_directory.symlink_to(external, target_is_directory=True)

    before_state = _tree_snapshot(state_root)
    before_external = _tree_snapshot(external) if external is not None else None

    with pytest.raises(StateCorrupt, match=rf"evidence {digest} is unreadable"):
        store.read_evidence(digest)

    assert _tree_snapshot(state_root) == before_state
    if external is not None:
        assert _tree_snapshot(external) == before_external


def test_ambiguous_or_corrupt_runtime_state_fails_closed_without_counter_reset(
    tmp_path: Path,
) -> None:
    repo = _create_repo(tmp_path / "repo", REPO_UUID)
    state_root = tmp_path / "state"
    registry = RegistryStore(state_root, capabilities=SUPPORTED)
    registry.enroll(
        discover_source(repo),
        _authorization(IdentityAction.ENROLL, "enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(state_root, registry, capabilities=SUPPORTED)
    grant = leases.acquire(
        REPO_UUID,
        "BUILD",
        leases.current_owner(),
        expected_registry_revision=1,
        expected_active_source_revision=1,
        expected_operation_epoch=1,
        expected_migration_epoch=0,
        acquired_at=datetime(2026, 7, 16, 15, 10, tzinfo=timezone.utc),
        monotonic_ns=40_000,
        ttl_ns=1_000,
    )
    assert grant.lease.to_dict()["fence_token"] == 2

    workspace_root = state_root / "workspaces" / REPO_UUID
    (workspace_root / "workspace.json").write_text("{corrupt", encoding="utf-8")
    pending = workspace_root / "workspace.pending.json"
    pending.unlink(missing_ok=True)

    with pytest.raises(StateCorrupt, match="workspace"):
        LeaseStore(state_root, registry, capabilities=SUPPORTED).inspect(REPO_UUID)
