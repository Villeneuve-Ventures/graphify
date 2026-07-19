from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, cast

import pytest

from graphify.workspace.adapters import QueryRequest
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import (
    CapacityPolicy,
    CompatibilityManifest,
    GenerationReceipt,
    PointerSet,
    payload_manifest_sha256,
)
from graphify.workspace.freshness import FreshnessAuthority
from graphify.workspace.generations import CertificationRequest, GenerationStore
from graphify.workspace.journal import JournalStore
from graphify.workspace.pointers import PointerCAS, PointerStore
from graphify.workspace.semantic_queue import SemanticQueuePolicy, SemanticQueueStore
from tests.workspace_p3_helpers import (
    REPO_UUID,
    START,
    acquire,
    create_harness,
    metadata_snapshot,
    tree_snapshot,
)


FIXTURES = Path(__file__).parent / "fixtures" / "workspace" / "v1"
COMPATIBILITY_MANIFEST = cast(
    CompatibilityManifest,
    CompatibilityManifest.from_json(
        (FIXTURES / "positive" / "compatibility-manifest.json").read_bytes()
    ),
)
COMPATIBILITY_SHA256 = COMPATIBILITY_MANIFEST.sha256
QUERY = QueryRequest("workspace")
POLICY = CapacityPolicy.from_mapping(
    {
        "contract": "graphify.workspace.capacity_policy.internal",
        "format_version": 1,
        "global_max_bytes": 32 * 1024 * 1024,
        "global_max_generations": 16,
        "workspace_max_bytes": 8 * 1024 * 1024,
        "workspace_max_generations": 8,
        "reserve_bytes": 1024,
    }
)
QUEUE_POLICY = SemanticQueuePolicy(
    max_items=16,
    max_bytes=64 * 1024,
    retry_budget=1,
)


@dataclass(frozen=True)
class FreshnessRuntime:
    repo: Path
    state_root: Path
    registry: Any
    pointers: PointerStore
    authority: FreshnessAuthority


def _xattr_snapshot(root: Path) -> dict[str, tuple[tuple[str, bytes], ...]]:
    result: dict[str, tuple[tuple[str, bytes], ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        try:
            listxattr = getattr(os, "listxattr")
            getxattr = getattr(os, "getxattr")
            names = sorted(listxattr(path, follow_symlinks=False))
            values = tuple(
                (name, getxattr(path, name, follow_symlinks=False)) for name in names
            )
        except (AttributeError, OSError):
            values = ()
        result[relative] = values
    return result


def _runtime(
    tmp_path: Path,
    *,
    max_inventory_passes: int = 6,
) -> FreshnessRuntime:
    harness = create_harness(tmp_path)
    journal = JournalStore(
        harness.state_root,
        harness.leases,
        capabilities=harness.leases.state.capabilities,
    )
    semantic_queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=QUEUE_POLICY,
        capabilities=harness.leases.state.capabilities,
    )
    generations = GenerationStore(
        harness.state_root,
        harness.leases,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue=semantic_queue,
        capabilities=harness.leases.state.capabilities,
    )
    pointers = PointerStore(
        harness.state_root,
        harness.leases,
        generations,
        journal,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        capabilities=harness.leases.state.capabilities,
    )
    authority = FreshnessAuthority(
        harness.registry,
        pointers,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        max_inventory_passes=max_inventory_passes,
    )
    observation = authority.adapter.observe(harness.repo)
    build = acquire(harness, "BUILD", tick=1)
    allocation = generations.allocate(
        build,
        expected_payload_bytes=4096,
        capacity_policy=POLICY,
        generation_id="gen-current",
        occurred_at=START,
        monotonic_ns=10_001,
    )
    payload = allocation.staging_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text('{"nodes": [], "edges": []}\n', encoding="utf-8")
    entries = generations.inspect_staged_payload(allocation)
    source_observations: tuple[SourceObservation, SourceObservation] = (
        observation,
        observation,
    )
    semantic_queue.reconcile(
        build,
        (),
        source_epoch=1,
        policy_sha256=observation.policy_sha256,
        source_observations=source_observations,
        desired_watermark=1,
        semantic_required=False,
        monotonic_ns=10_002,
    )
    semantic_queue.bind_sealed_inputs(
        build,
        sealed_input_manifest_sha256=payload_manifest_sha256(
            "graphify-out",
            entries,
        ),
        monotonic_ns=10_003,
    )
    receipt = generations.certify(
        build,
        allocation,
        CertificationRequest(
            source_commit=observation.source_commit,
            source_epoch=1,
            policy_sha256=observation.policy_sha256,
            observation_manifest_sha256=observation.inventory_sha256,
            queue_watermark=1,
            semantic_completeness="not_required",
            compatibility_sha256=COMPATIBILITY_SHA256,
            validations=(
                "payload_manifest",
                "coordination_lock_precreated",
                "stable_semantic_queue",
            ),
        ),
        source_observations=source_observations,
        declared_entries=entries,
        occurred_at=START,
        monotonic_ns=10_004,
    )
    harness.leases.release(build)
    promote = acquire(harness, "PROMOTE", tick=2)
    receipt_value = receipt.to_dict()
    pointers.promote(
        promote,
        PointerCAS(
            expected_pointer_revision=0,
            expected_active_source_revision=promote.active_source_revision,
            expected_source_epoch=int(receipt_value["source_epoch"]),
            expected_operation_epoch=promote.operation_epoch,
            expected_migration_epoch=promote.migration_epoch,
            expected_state_schema_version=1,
            expected_fence_token=int(promote.lease.to_dict()["fence_token"]),
            candidate_generation_id="gen-current",
            candidate_receipt_sha256=receipt.sha256,
            expected_current_receipt_sha256=None,
        ),
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )
    harness.leases.release(promote)
    return FreshnessRuntime(
        repo=harness.repo,
        state_root=harness.state_root,
        registry=harness.registry,
        pointers=pointers,
        authority=authority,
    )


def _query(payload: Path) -> str:
    return (payload / "graph.json").read_text(encoding="utf-8")


def test_two_sided_observed_current_releases_only_after_equal_observations(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    events: list[str] = []

    result = runtime.authority.query(
        REPO_UUID,
        QUERY,
        hook=lambda event, _details: events.append(event),
    )

    assert result.decision == "release"
    assert result.reason == "observed_current"
    assert result.query_executed is True
    assert result.output == "No matching nodes found."
    assert result.release is not None
    release = result.release.to_dict()
    assert release["pre_observation"] == release["post_observation"]
    assert release["limitations"] == {
        "strict_source_linearizability": False,
        "inter_observation_aba_detection": False,
        "post_boundary_changes": "out_of_scope",
    }
    assert events[-1] == "freshness:release_boundary"


Mutation = Callable[[Path], None]


def _edit(repo: Path) -> None:
    (repo / "README.md").write_text("edited after pre-observation\n", encoding="utf-8")


def _create(repo: Path) -> None:
    (repo / "created.py").write_text("created = True\n", encoding="utf-8")


def _delete(repo: Path) -> None:
    (repo / "README.md").unlink()


def _rename(repo: Path) -> None:
    (repo / "README.md").rename(repo / "RENAMED.md")


def _replace(repo: Path) -> None:
    replacement = repo / "replacement.tmp"
    replacement.write_text("replacement\n", encoding="utf-8")
    os.replace(replacement, repo / "README.md")


def _policy(repo: Path) -> None:
    (repo / ".graphifyignore").write_text("README.md\n", encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [_edit, _create, _delete, _rename, _replace, _policy],
    ids=["edit", "create", "delete", "rename", "replace", "policy"],
)
def test_mutation_after_pre_observation_withholds_query_output(
    tmp_path: Path,
    mutation: Mutation,
) -> None:
    runtime = _runtime(tmp_path)
    mutated = False

    def hook(event: str, _details: Mapping[str, object]) -> None:
        nonlocal mutated
        if event == "freshness:pre_observed" and not mutated:
            mutation(runtime.repo)
            mutated = True

    result = runtime.authority._run(REPO_UUID, _query, hook=hook)

    assert result.decision == "withhold"
    assert result.reason == "drift"
    assert result.query_executed is True
    assert result.output is None


def test_mutation_after_file_hash_in_post_pass_cannot_escape_second_inventory(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mutated = False

    def hook(event: str, details: Mapping[str, object]) -> None:
        nonlocal mutated
        if (
            event == "freshness:post:inventory_file_hashed"
            and details.get("path") == "README.md"
            and not mutated
        ):
            _edit(runtime.repo)
            mutated = True

    result = runtime.authority._run(REPO_UUID, _query, hook=hook)

    assert result.decision == "withhold"
    assert result.reason == "drift"
    assert result.output is None


def test_persistent_inventory_churn_fails_closed_as_unstable(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, max_inventory_passes=3)
    flip = False

    def hook(event: str, _details: Mapping[str, object]) -> None:
        nonlocal flip
        if event == "freshness:pre:inventory_complete":
            flip = not flip
            (runtime.repo / "README.md").write_text(
                "first\n" if flip else "second\n",
                encoding="utf-8",
            )

    result = runtime.authority._run(REPO_UUID, _query, hook=hook)

    assert result.decision == "withhold"
    assert result.reason == "unstable"
    assert result.query_executed is False
    assert result.output is None


def test_unsupported_remote_shortcut_after_query_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    def hook(event: str, _details: Mapping[str, object]) -> None:
        if event == "freshness:after_query":
            (runtime.repo / "remote.gdoc").write_text("{}\n", encoding="utf-8")

    result = runtime.authority.query(REPO_UUID, QUERY, hook=hook)

    assert result.decision == "withhold"
    assert result.reason == "unsupported"
    assert result.query_executed is True
    assert result.output is None


def test_symlinked_source_is_unsupported_before_query(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = runtime.repo / "alias.md"
    try:
        source.symlink_to(runtime.repo / "README.md")
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not support symlinks")

    result = runtime.authority.query(REPO_UUID, QUERY)

    assert result.decision == "withhold"
    assert result.reason == "unsupported"
    assert result.query_executed is False
    assert result.output is None


def test_source_read_io_failure_withholds_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    runtime = _runtime(tmp_path)
    source = runtime.repo / "README.md"
    source_identity = source.stat()
    original_read = adapter_module.os.read

    def failing_read(descriptor: int, size: int) -> bytes:
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        ):
            raise OSError(errno.EIO, "injected source read failure")
        return original_read(descriptor, size)

    monkeypatch.setattr(adapter_module.os, "read", failing_read)

    result = runtime.authority.query(REPO_UUID, QUERY)

    assert result.decision == "withhold"
    assert result.reason == "source_unavailable"
    assert result.query_executed is False
    assert result.output is None


def test_expired_freshness_deadline_fails_before_query(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    result = runtime.authority.query(REPO_UUID, QUERY, timeout_ns=1)

    assert result.decision == "withhold"
    assert result.reason == "timeout"
    assert result.query_executed is False
    assert result.output is None


def test_freshness_deadline_bounds_source_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.workspace.identity as identity_module

    runtime = _runtime(tmp_path)
    before = {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    }

    def blocked_git(command: list[str], **kwargs: Any) -> Any:
        timeout = kwargs.get("timeout")
        if timeout is None:
            raise AssertionError("freshness deadline did not reach source discovery")
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(identity_module.subprocess, "run", blocked_git)

    result = runtime.authority.query(
        REPO_UUID,
        QUERY,
        timeout_ns=1_000_000_000,
    )

    assert result.decision == "withhold"
    assert result.reason == "timeout"
    assert result.query_executed is False
    assert result.output is None
    assert {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    } == before


def test_freshness_deadline_does_not_block_on_fifo_workspace_config(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable on this platform")

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import os, sys",
                    "from pathlib import Path",
                    "from tests.test_workspace_freshness import QUERY, _runtime",
                    "from tests.workspace_p3_helpers import REPO_UUID",
                    "runtime = _runtime(Path(sys.argv[1]))",
                    "config = runtime.repo / '.graphify' / 'workspace.toml'",
                    "config.unlink()",
                    "os.mkfifo(config)",
                    "result = runtime.authority.query(REPO_UUID, QUERY, timeout_ns=60_000_000_000)",
                    "assert result.decision == 'withhold'",
                    "assert result.reason == 'source_unavailable'",
                    "assert result.query_executed is False",
                )
            ),
            str(tmp_path / "fifo-probe"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("lock_kind", ["registry", "generation"])
def test_freshness_deadline_bounds_shared_lock_acquisition(
    tmp_path: Path,
    lock_kind: str,
) -> None:
    runtime = _runtime(tmp_path)
    before = {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    }
    if lock_kind == "registry":
        lock = runtime.registry.state.path(runtime.registry.LOCK)
    else:
        lock = runtime.pointers.state.path(
            runtime.pointers.generations._lock(REPO_UUID, "gen-current")
        )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time; "
                "fd=os.open(sys.argv[1], os.O_RDONLY); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "print('READY', flush=True); time.sleep(60)"
            ),
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        started = time.monotonic()

        result = runtime.authority.query(
            REPO_UUID,
            QUERY,
            timeout_ns=10_000_000,
        )

        assert time.monotonic() - started < 1.0
        assert result.decision == "withhold"
        assert result.reason == "timeout"
        assert result.query_executed is False
        assert result.output is None
        assert {
            "source_tree": tree_snapshot(runtime.repo),
            "source_metadata": metadata_snapshot(runtime.repo),
            "source_xattrs": _xattr_snapshot(runtime.repo),
            "workspace_tree": tree_snapshot(runtime.state_root),
            "workspace_metadata": metadata_snapshot(runtime.state_root),
            "workspace_xattrs": _xattr_snapshot(runtime.state_root),
        } == before
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_freshness_deadline_expires_during_pointer_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    before = {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    }
    now = 0
    timeout_ns = 60_000_000_000
    original_verify = runtime.pointers.generations.verify_generation

    def monotonic_ns() -> int:
        return now

    def verify_generation(repo_uuid: str, generation_id: str) -> GenerationReceipt:
        nonlocal now
        receipt = original_verify(repo_uuid, generation_id)
        now = timeout_ns
        return receipt

    def forbidden_journal_verification(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("expired pointer read reached journal verification")

    monkeypatch.setattr(time, "monotonic_ns", monotonic_ns)
    monkeypatch.setattr(
        runtime.pointers.generations,
        "verify_generation",
        verify_generation,
    )
    monkeypatch.setattr(
        runtime.pointers,
        "_verify_visible_pointer_journal",
        forbidden_journal_verification,
    )

    result = runtime.authority.query(REPO_UUID, QUERY, timeout_ns=timeout_ns)

    assert result.decision == "withhold"
    assert result.reason == "timeout"
    assert result.query_executed is False
    assert result.output is None
    assert {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    } == before


def test_freshness_deadline_expires_before_release_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    now = 0
    timeout_ns = 60_000_000_000
    journal_checks = 0
    original_journal_verification = runtime.pointers._verify_visible_pointer_journal

    def monotonic_ns() -> int:
        return now

    def hook(event: str, _details: Mapping[str, object]) -> None:
        nonlocal now
        if event == "freshness:post_observed":
            now = timeout_ns

    def count_journal_verification(repo_uuid: str, pointer: PointerSet) -> None:
        nonlocal journal_checks
        journal_checks += 1
        if journal_checks > 1:
            raise AssertionError("expired release reached journal revalidation")
        original_journal_verification(repo_uuid, pointer)

    monkeypatch.setattr(time, "monotonic_ns", monotonic_ns)
    monkeypatch.setattr(
        runtime.pointers,
        "_verify_visible_pointer_journal",
        count_journal_verification,
    )

    result = runtime.authority.query(
        REPO_UUID,
        QUERY,
        timeout_ns=timeout_ns,
        hook=hook,
    )

    assert result.decision == "withhold"
    assert result.reason == "timeout"
    assert result.query_executed is True
    assert result.output is None
    assert journal_checks == 1


def test_deadline_expiring_after_pre_observation_skips_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    now = 0
    timeout_ns = 60_000_000_000
    calls = 0

    def monotonic_ns() -> int:
        return now

    def hook(event: str, _details: Mapping[str, object]) -> None:
        nonlocal now
        if event == "freshness:pre_observed":
            now = timeout_ns

    def query(payload: Path) -> str:
        nonlocal calls
        calls += 1
        return _query(payload)

    monkeypatch.setattr(time, "monotonic_ns", monotonic_ns)

    result = runtime.authority._run(
        REPO_UUID,
        query,
        timeout_ns=timeout_ns,
        hook=hook,
    )

    assert result.decision == "withhold"
    assert result.reason == "timeout"
    assert result.query_executed is False
    assert result.output is None
    assert result.release is not None
    release = result.release.to_dict()
    assert release["release_decision"] == "withhold"
    assert release["reason"] == "timeout"
    assert release["pre_observation"] == release["post_observation"]
    assert calls == 0


def test_inter_observation_aba_is_documented_not_overclaimed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original = (runtime.repo / "README.md").read_bytes()

    def aba_query(payload: Path) -> str:
        (runtime.repo / "README.md").write_text("transient ABA edit\n", encoding="utf-8")
        (runtime.repo / "README.md").write_bytes(original)
        return _query(payload)

    result = runtime.authority._run(REPO_UUID, aba_query)

    assert result.decision == "release"
    assert result.reason == "observed_current"
    assert result.release is not None
    assert result.release.to_dict()["limitations"]["inter_observation_aba_detection"] is False


def test_mutation_after_release_boundary_is_a_subsequent_change(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    def hook(event: str, _details: Mapping[str, object]) -> None:
        if event == "freshness:release_boundary":
            _edit(runtime.repo)

    result = runtime.authority._run(REPO_UUID, _query, hook=hook)

    assert result.decision == "release"
    assert result.reason == "observed_current"
    assert (runtime.repo / "README.md").read_text(encoding="utf-8") == (
        "edited after pre-observation\n"
    )


def test_source_mutation_after_post_observation_is_a_subsequent_change(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    def hook(event: str, _details: Mapping[str, object]) -> None:
        if event == "freshness:post_observed":
            _edit(runtime.repo)

    result = runtime.authority._run(REPO_UUID, _query, hook=hook)

    assert result.decision == "release"
    assert result.reason == "observed_current"
    assert (runtime.repo / "README.md").read_text(encoding="utf-8") == (
        "edited after pre-observation\n"
    )


def test_pointer_change_before_release_discards_output(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    pointer_path = runtime.state_root / "workspaces" / REPO_UUID / "pointers.json"

    def hook(event: str, _details: Mapping[str, object]) -> None:
        if event != "freshness:after_query":
            return
        current = PointerSet.from_json(pointer_path.read_bytes()).to_dict()
        current["pointer_revision"] = int(current["pointer_revision"]) + 1
        pointer_path.write_bytes(PointerSet.from_mapping(current).canonical)
        pointer_path.chmod(0o600)

    result = runtime.authority._run(REPO_UUID, _query, hook=hook)

    assert result.decision == "withhold"
    assert result.reason == "drift"
    assert result.output is None


def test_forged_pointer_lease_authority_is_rejected_before_query(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    pointer_path = runtime.state_root / "workspaces" / REPO_UUID / "pointers.json"
    pointer = cast(PointerSet, PointerSet.from_json(pointer_path.read_bytes()))
    forged = pointer.to_dict()
    forged["operation_epoch"] = int(forged["operation_epoch"]) + 1
    forged["fence_token"] = int(forged["fence_token"]) + 1
    pointer_path.write_bytes(PointerSet.from_mapping(forged).canonical)
    pointer_path.chmod(0o600)
    before = tree_snapshot(runtime.state_root)
    called = False

    def query(_payload: Path) -> str:
        nonlocal called
        called = True
        return "unexpected"

    result = runtime.authority._run(REPO_UUID, query)

    assert result.decision == "withhold"
    assert result.reason == "drift"
    assert result.query_executed is False
    assert called is False
    assert tree_snapshot(runtime.state_root) == before


def test_corrupt_current_generation_is_withheld_before_query(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    graph_path = (
        runtime.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / "gen-current"
        / "graphify-out"
        / "graph.json"
    )
    graph_path.unlink()
    before = tree_snapshot(runtime.state_root)
    called = False

    def query(_payload: Path) -> str:
        nonlocal called
        called = True
        return "unexpected"

    result = runtime.authority._run(REPO_UUID, query)

    assert result.decision == "withhold"
    assert result.reason == "drift"
    assert result.query_executed is False
    assert called is False
    assert tree_snapshot(runtime.state_root) == before


def test_source_identity_change_after_post_observation_discards_output(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    mutated = False

    def hook(event: str, _details: Mapping[str, object]) -> None:
        nonlocal mutated
        if event == "freshness:post_observed" and not mutated:
            subprocess.run(
                [
                    "git",
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/example/changed.git",
                ],
                cwd=runtime.repo,
                check=True,
            )
            mutated = True

    result = runtime.authority.query(REPO_UUID, QUERY, hook=hook)

    assert result.decision == "withhold"
    assert result.reason == "source_unavailable"
    assert result.query_executed is True
    assert result.output is None


def test_receipt_compatibility_mismatch_fails_before_query(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    workspace = runtime.state_root / "workspaces" / REPO_UUID
    pointer_path = workspace / "pointers.json"
    pointer = cast(PointerSet, PointerSet.from_json(pointer_path.read_bytes()))
    pointer_value = pointer.to_dict()
    current = cast(dict[str, Any], pointer_value["current"])
    receipt_path = (
        workspace / "generations" / str(current["generation_id"]) / "receipt.json"
    )
    receipt = cast(GenerationReceipt, GenerationReceipt.from_json(receipt_path.read_bytes()))
    unsupported_receipt = cast(
        GenerationReceipt,
        GenerationReceipt.from_mapping(
            {**receipt.to_dict(), "compatibility_sha256": "d" * 64}
        ),
    )
    receipt_path.write_bytes(unsupported_receipt.canonical)
    current["receipt_sha256"] = unsupported_receipt.sha256
    unsupported_pointer = cast(PointerSet, PointerSet.from_mapping(pointer_value))
    pointer_path.write_bytes(unsupported_pointer.canonical)
    called = False

    def query(_payload: Path) -> str:
        nonlocal called
        called = True
        return "unexpected"

    result = runtime.authority._run(REPO_UUID, query)

    assert result.decision == "withhold"
    assert result.reason == "unsupported"
    assert result.query_executed is False
    assert called is False


def test_unavailable_active_source_fails_closed_without_query(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    moved = runtime.repo.with_name("repo-moved-away")
    runtime.repo.rename(moved)
    called = False

    def query(_payload: Path) -> str:
        nonlocal called
        called = True
        return "unexpected"

    result = runtime.authority._run(REPO_UUID, query)

    assert result.decision == "withhold"
    assert result.reason == "source_unavailable"
    assert result.query_executed is False
    assert result.output is None
    assert called is False


def test_source_disappearing_between_authority_and_observation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    moved = runtime.repo.with_name("repo-moved-during-pre-observation")
    original_observe = runtime.authority.adapter.observe
    disappeared = False
    called = False

    def disappearing_observe(
        source_root: Path,
        **kwargs: Any,
    ) -> Any:
        nonlocal disappeared
        if not disappeared:
            runtime.repo.rename(moved)
            disappeared = True
        return original_observe(source_root, **kwargs)

    def query(_payload: Path) -> str:
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(runtime.authority.adapter, "observe", disappearing_observe)
    result = runtime.authority._run(REPO_UUID, query)

    assert result.decision == "withhold"
    assert result.reason == "source_unavailable"
    assert result.query_executed is False
    assert result.output is None
    assert called is False


def test_freshness_leaves_bytes_write_sensitive_metadata_and_xattrs_unchanged(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    before = {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    }

    result = runtime.authority.query(REPO_UUID, QUERY)

    after = {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
    }
    assert result.decision == "release"
    assert after == before


def test_freshness_emits_no_transient_filesystem_write_events(tmp_path: Path) -> None:
    watchdog = pytest.importorskip("watchdog.observers")
    events_module = pytest.importorskip("watchdog.events")
    runtime = _runtime(tmp_path)
    observed: list[tuple[str, str]] = []

    class Handler(events_module.FileSystemEventHandler):  # type: ignore[misc]
        def on_any_event(self, event: Any) -> None:
            if event.event_type not in {"opened", "closed", "closed_no_write"}:
                observed.append((event.event_type, event.src_path))

    observer = watchdog.Observer()
    handler = Handler()
    observer.schedule(handler, str(runtime.repo), recursive=True)
    observer.schedule(handler, str(runtime.state_root), recursive=True)
    observer.start()
    try:
        # macOS FSEvents may first replay mutations from fixture construction.
        # Settle that backlog before arming this operation-specific assertion.
        time.sleep(0.1)
        observed.clear()
        result = runtime.authority.query(REPO_UUID, QUERY)
        time.sleep(0.1)
    finally:
        observer.stop()
        observer.join(timeout=5)

    assert result.decision == "release"
    assert observed == []


def test_native_query_bypasses_optional_query_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    log_path = tmp_path / "query-log.jsonl"
    monkeypatch.setenv("GRAPHIFY_QUERY_LOG", str(log_path))

    result = runtime.authority.query(REPO_UUID, QUERY)

    assert result.decision == "release"
    assert not log_path.exists()


def test_observer_runs_against_read_only_source_tree(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    original_modes: dict[Path, int] = {}
    for path in sorted((runtime.repo, *runtime.repo.rglob("*")), reverse=True):
        original_modes[path] = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        observation = runtime.authority.adapter.observe(runtime.repo)
    finally:
        for path, mode in sorted(original_modes.items(), key=lambda item: len(item[0].parts)):
            if path.exists() and not path.is_symlink():
                path.chmod(mode)

    assert observation.stable_inventory_passes == 2
    assert observation.inventory_sha256
