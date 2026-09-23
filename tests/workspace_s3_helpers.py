from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Sequence

from graphify.workspace.lifecycle_observation import SourceObservation
from graphify.workspace.identity import IdentityAction, OperatorAuthorization, discover_source
from graphify.workspace.leases import LeaseStore
from graphify.workspace.persistence import RuntimeCapabilities
from graphify.workspace.registry import RegistryStore


REPO_UUID = "11111111-1111-4111-8111-111111111111"
REMOTE = "https://github.com/example/graphify-p3-fixture.git"
SUPPORTED = RuntimeCapabilities.supported_test_fixture()
START = datetime(2026, 7, 16, 19, 0, tzinfo=timezone.utc)
from tests.test_workspace_contracts import compatibility

COMPATIBILITY_MANIFEST = compatibility()


class StaticObservationAdapter:
    """Test adapter that makes synthetic observation authority explicit."""

    adapter_id = "test-static-observation"
    engine_baseline = "test"
    detector_id = "test-static-observation"

    def __init__(self, observations: Sequence[SourceObservation]) -> None:
        self.observations = tuple(observations)
        if not self.observations:
            raise ValueError("at least one source observation is required")
        self.calls = 0

    def observe(self, _source_root: Path, **_kwargs: Any) -> SourceObservation:
        observation = self.observations[min(self.calls, len(self.observations) - 1)]
        self.calls += 1
        return observation


def trust_source_observations(
    store: Any,
    observations: Sequence[SourceObservation],
) -> StaticObservationAdapter:
    adapter = StaticObservationAdapter(observations)
    store.observer = adapter.observe
    return adapter


def git_output(
    repo: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
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


def create_repo(root: Path, repo_uuid: str = REPO_UUID) -> Path:
    root.mkdir(parents=True)
    git_output(root, "init", "--quiet")
    git_output(root, "config", "user.email", "workspace-p3@example.com")
    git_output(root, "config", "user.name", "Workspace P3")
    config = root / ".graphify/workspace.toml"
    config.parent.mkdir()
    config.write_text(_workspace_toml(repo_uuid), encoding="utf-8")
    (root / "README.md").write_text("p3 fixture\n", encoding="utf-8")
    (root / "main.py").write_text("answer = 42\n", encoding="utf-8")
    git_output(root, "add", ".")
    seed_commit_env = os.environ.copy()
    seed_commit_env.update(
        {
            "GIT_AUTHOR_DATE": START.isoformat(),
            "GIT_AUTHOR_EMAIL": "workspace-p3@example.com",
            "GIT_AUTHOR_NAME": "Workspace P3",
            "GIT_COMMITTER_DATE": START.isoformat(),
            "GIT_COMMITTER_EMAIL": "workspace-p3@example.com",
            "GIT_COMMITTER_NAME": "Workspace P3",
        }
    )
    disabled_hooks = root / ".git" / "disabled-hooks"
    disabled_hooks.mkdir()
    git_output(
        root,
        "-c",
        f"core.hooksPath={disabled_hooks}",
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "-m",
        "p3 fixture",
        env=seed_commit_env,
    )
    git_output(root, "remote", "add", "origin", REMOTE)
    return root


def authorization(nonce: str) -> OperatorAuthorization:
    return OperatorAuthorization(
        action=IdentityAction.ENROLL,
        operator_id="operator:p3-test",
        reason="P3 test enrollment",
        issued_at="2026-07-16T19:00:00Z",
        nonce=nonce,
    )


@dataclass(frozen=True)
class RuntimeHarness:
    repo: Path
    state_root: Path
    registry: RegistryStore
    leases: LeaseStore


def create_harness(tmp_path: Path, *, fault_hook: Any = None, syscalls: Any = None) -> RuntimeHarness:
    tmp_path = tmp_path.resolve()
    repo = create_repo(tmp_path / "repo")
    state_root = tmp_path / "state"
    registry = RegistryStore(
        state_root,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
        syscalls=syscalls,
    )
    registry.enroll(
        discover_source(repo),
        authorization("enroll"),
        expected_revision=0,
    )
    leases = LeaseStore(
        state_root,
        registry,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
        syscalls=syscalls,
    )
    return RuntimeHarness(repo=repo, state_root=state_root, registry=registry, leases=leases)


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, int, str | None]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[int, int, int, int, int, str | None]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        details = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        digest = None
        if stat.S_ISREG(details.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(details.st_mode):
            digest = os.readlink(path)
        result[relative] = (
            stat.S_IFMT(details.st_mode),
            stat.S_IMODE(details.st_mode),
            details.st_ino,
            details.st_size,
            details.st_mtime_ns,
            digest,
        )
    return result
