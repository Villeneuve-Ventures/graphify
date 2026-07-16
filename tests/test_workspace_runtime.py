from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
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
from typing import Any, cast

import pytest

import graphify.workspace.leases as lease_module

from graphify.workspace.contracts import FencedLease, canonical_json_bytes
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    OperatorAuthorization,
    SourceAmbiguousError,
    SourceDiscoveryError,
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
    LockOrderError,
    PosixSyscalls,
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


class FailFsyncCallSyscalls(PosixSyscalls):
    def __init__(self, call_number: int) -> None:
        self.call_number = call_number
        self.calls = 0

    def fsync(self, descriptor: int) -> None:
        self.calls += 1
        if self.calls == self.call_number:
            raise OSError(errno.EIO, "injected fsync after replace")
        super().fsync(descriptor)


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


def test_runtime_rejects_unsupported_platform_without_test_capability(tmp_path: Path) -> None:
    unsupported = RuntimeCapabilities(
        system="Linux",
        filesystem="ext4",
        elevated=False,
        local=True,
    )

    with pytest.raises(UnsupportedRuntime, match="macOS.*APFS"):
        RegistryStore(tmp_path / "state", capabilities=unsupported)


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
        discover_source(original),
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
    assert (
        store.read_evidence(rotated_entry["uuid_enrollment"]["current_evidence_sha256"])["action"]
        == "ROTATE"
    )


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


def test_rebind_aliases_and_active_source_cas_fail_closed(tmp_path: Path) -> None:
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
    assert events.index("lock:registry:released") < events.index("lock:workspace:acquired")
    leases.release(grant)


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
