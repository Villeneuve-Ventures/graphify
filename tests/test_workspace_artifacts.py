from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

from graphify.workspace import WORKSPACE_SCHEMA_FILES, canonical_json_bytes, canonical_sha256
import tools.workspace_artifacts as workspace_artifacts
import tools.workspace_artifacts.candidate as candidate_artifacts
from tools.workspace_artifacts import (
    ArtifactError,
    build_static_bundles,
    prove_independent_tamper_rejection,
    run_disposable_compensation_proof,
    sha256_file,
    snapshot_disposable_home,
    strict_tree_manifest,
    strict_tree_sha256,
    verify_trusted_manifest,
    write_trusted_manifest,
)
from tools.workspace_artifacts.candidate import (
    CANDIDATE_UV_VERSION,
    CONTROLLED_UPSTREAM_INDEX,
    UPSTREAM_WHEEL_NAME,
    UPSTREAM_WHEEL_SHA256,
    _build_offline_rollback,
    _controlled_upstream_environment,
    _download_verified_upstream_wheel,
    _extract_git_archive,
    _fetch_url,
    _isolated_environment,
    _normalize_cyclonedx,
    _run,
    _select_upstream_wheel,
    build_candidate,
    compare_candidate_roots,
    prove_candidate,
    skill_bundle_tree_sha256,
)


WHEEL_NAME = "graphifyy-0.9.16+workspace.1-py3-none-any.whl"


def _real_skill_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".codex/skills/graphify",
        home / ".copilot/skills/graphify",
        home / ".gemini/skills/graphify",
    )


def _skill_root_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(entry["path"]): str(entry["sha256"])
        for entry in strict_tree_manifest(root)
    }


def _write(path: Path, data: bytes | str, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)
    return path


def _synthetic_repo(root: Path) -> tuple[Path, Path]:
    _write(root / "graphify/skill-codex.md", "# candidate skill\n")
    _write(root / "graphify/skills/codex/references/query.md", "# query\n")
    _write(root / "graphify/workspace/__init__.py", "# reference package\n")
    _write(root / "graphify/workspace/contracts.py", "# reference model\n")
    _write(root / "graphify/workspace/freshness.py", "# freshness runtime\n")
    _write(root / "graphify/workspace/adapters/__init__.py", "# adapter package\n")
    _write(root / "graphify/workspace/adapters/base.py", "# adapter contract\n")
    _write(root / "graphify/workspace/adapters/v0_9_16.py", "# 0.9.16 adapter\n")
    for module_name in (
        "gc.py",
        "generations.py",
        "identity.py",
        "journal.py",
        "leases.py",
        "persistence.py",
        "pointers.py",
        "registry.py",
    ):
        _write(root / "graphify/workspace" / module_name, f"# {module_name} runtime\n")
    for schema_name in WORKSPACE_SCHEMA_FILES:
        _write(root / "graphify/workspace/schemas/v1" / schema_name, "{}\n")
    _write(
        root / "graphify/workspace/schemas/cli/v1/identity-maintenance.schema.json",
        "{}\n",
    )
    _write(root / "graphify/workspace/schemas/cli/v1/registration.schema.json", "{}\n")
    _write(root / "graphify/workspace/schemas/cli/v1/status.schema.json", "{}\n")
    _write(root / "graphify/workspace/schemas/cli/v1/sync-request.schema.json", "{}\n")
    _write(root / "graphify/workspace/schemas/cli/v1/sync-receipt.schema.json", "{}\n")
    _write(root / "graphify/workspace/schemas/cli/v2/status.schema.json", "{}\n")
    _write(root / "docs/workspace/v1/README.md", "# contracts\n")
    _write(root / "tests/fixtures/workspace/v1/positive/config.json", "{}\n")
    _write(root / "uv.lock", "version = 1\n")
    wheel = _write(root / "dist" / WHEEL_NAME, b"fixture-wheel")
    runtime = _write(root / "work" / "runtime-manifest.json", "{\"locked\":true}\n")
    return wheel, runtime


def _build_candidate(root: Path, output: Path) -> tuple[dict[str, Path], bytes]:
    wheel, runtime = _synthetic_repo(root)
    artifacts = build_static_bundles(
        repo_root=root,
        output_root=output,
        wheel=wheel,
        runtime_manifest=runtime,
    )
    trusted = write_trusted_manifest(
        artifact_root=output,
        artifact_names=sorted(artifacts),
    )
    verify_trusted_manifest(artifact_root=output, trusted_manifest=trusted)
    return artifacts, trusted


def _disposable_compensation_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write(home / ".local/bin/graphify", "prior-binary\n", executable=True)
    _write(home / ".local/state/graphify/runtime-manifest.json", "prior-runtime\n")
    _write(codex_home / "skills/graphify/SKILL.md", "prior-skill\n")
    _write(home / "Library/LaunchAgents/com.graphify.fixture.plist", "prior-service\n")
    _write(
        home / ".local/state/graphify/workspaces/fixture/generations/gen-canary/receipt.json",
        "generation-canary\n",
    )
    bundle = tmp_path / "rollback.zip"
    snapshot_disposable_home(home=home, codex_home=codex_home, rollback_bundle=bundle)
    return home, codex_home, bundle


def test_static_contract_fixture_skill_and_runtime_bundles_are_deterministic(
    tmp_path: Path,
) -> None:
    artifacts_one, trusted_one = _build_candidate(tmp_path / "repo-one", tmp_path / "out-one")
    artifacts_two, trusted_two = _build_candidate(tmp_path / "repo-two", tmp_path / "out-two")

    assert sorted(artifacts_one) == sorted(artifacts_two) == [
        "contract-bundle.zip",
        "fixture-bundle.zip",
        "fixture-manifest.json",
        WHEEL_NAME,
        "runtime-bundle.zip",
        "skill-bundle.zip",
    ]
    for name in artifacts_one:
        assert artifacts_one[name].read_bytes() == artifacts_two[name].read_bytes(), name
    assert trusted_one == trusted_two
    with zipfile.ZipFile(artifacts_one["contract-bundle.zip"]) as archive:
        assert "schemas/cli/v1/identity-maintenance.schema.json" in archive.namelist()
        assert "schemas/cli/v1/registration.schema.json" in archive.namelist()
        assert "schemas/cli/v1/status.schema.json" in archive.namelist()
        assert "schemas/cli/v1/sync-request.schema.json" in archive.namelist()
        assert "schemas/cli/v1/sync-receipt.schema.json" in archive.namelist()
        assert "schemas/cli/v2/status.schema.json" in archive.namelist()


def test_contract_bundle_ignores_generated_adapter_bytecode(tmp_path: Path) -> None:
    clean_root = tmp_path / "clean-repo"
    dirty_root = tmp_path / "dirty-repo"
    clean_wheel, clean_runtime = _synthetic_repo(clean_root)
    dirty_wheel, dirty_runtime = _synthetic_repo(dirty_root)
    _write(
        dirty_root / "graphify/workspace/adapters/__pycache__/base.cpython-314.pyc",
        b"ambient bytecode",
    )
    _write(
        dirty_root / "graphify/workspace/adapters/__pycache__/ambient.py",
        "AMBIENT = True\n",
    )

    clean = build_static_bundles(
        repo_root=clean_root,
        output_root=tmp_path / "clean-out",
        wheel=clean_wheel,
        runtime_manifest=clean_runtime,
    )
    dirty = build_static_bundles(
        repo_root=dirty_root,
        output_root=tmp_path / "dirty-out",
        wheel=dirty_wheel,
        runtime_manifest=dirty_runtime,
    )

    assert clean["contract-bundle.zip"].read_bytes() == dirty["contract-bundle.zip"].read_bytes()
    with zipfile.ZipFile(dirty["contract-bundle.zip"]) as archive:
        assert not any(
            "__pycache__" in name or name.endswith(".pyc") for name in archive.namelist()
        )


def test_cyclonedx_normalization_removes_uv_time_and_uuid_drift() -> None:
    first = {
        "bomFormat": "CycloneDX",
        "metadata": {"timestamp": "2026-07-15T01:02:03Z"},
        "serialNumber": "urn:uuid:11111111-1111-1111-1111-111111111111",
        "specVersion": "1.5",
        "version": 1,
    }
    second = {
        **first,
        "metadata": {"timestamp": "2026-07-15T04:05:06Z"},
        "serialNumber": "urn:uuid:22222222-2222-2222-2222-222222222222",
    }

    first_bytes = _normalize_cyclonedx(first, "a" * 64)
    second_bytes = _normalize_cyclonedx(second, "a" * 64)

    assert first_bytes == second_bytes
    normalized = json.loads(first_bytes)
    assert normalized["metadata"]["timestamp"] == "2026-07-14T22:57:12Z"
    assert normalized["serialNumber"].startswith("urn:uuid:")


def test_upstream_environment_scrubs_untrusted_package_sources() -> None:
    env = _controlled_upstream_environment(
        {
            "PATH": "/usr/bin",
            "UV_INDEX": "private=https://attacker.invalid/simple",
            "UV_DEFAULT_INDEX": "https://attacker.invalid/simple",
            "UV_INDEX_URL": "https://attacker.invalid/simple",
            "UV_EXTRA_INDEX_URL": "https://attacker.invalid/simple",
            "UV_FIND_LINKS": "/tmp/wheels",
            "PIP_INDEX_URL": "https://attacker.invalid/simple",
            "PIP_EXTRA_INDEX_URL": "https://attacker.invalid/simple",
            "PIP_FIND_LINKS": "/tmp/wheels",
            "PIP_NO_INDEX": "1",
        }
    )

    assert env["UV_DEFAULT_INDEX"] == CONTROLLED_UPSTREAM_INDEX
    assert env["PIP_INDEX_URL"] == CONTROLLED_UPSTREAM_INDEX
    assert env["UV_NO_CONFIG"] == "1"
    for name in (
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_NO_INDEX",
    ):
        assert name not in env


def test_isolated_environment_scrubs_untrusted_package_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient = {
        "PIP_CONFIG_FILE": "/tmp/pip.conf",
        "PIP_EXTRA_INDEX_URL": "https://attacker.invalid/simple",
        "PIP_FIND_LINKS": "/tmp/wheels",
        "PIP_INDEX_URL": "https://attacker.invalid/simple",
        "PIP_NO_INDEX": "1",
        "PIP_TRUSTED_HOST": "attacker.invalid",
        "UV_CONFIG_FILE": "/tmp/uv.toml",
        "UV_INDEX": "private=https://attacker.invalid/simple",
        "UV_DEFAULT_INDEX": "https://attacker.invalid/simple",
        "UV_EXTRA_INDEX_URL": "https://attacker.invalid/simple",
        "UV_FIND_LINKS": "/tmp/wheels",
        "UV_INDEX_STRATEGY": "unsafe-best-match",
        "UV_INDEX_URL": "https://attacker.invalid/simple",
        "UV_NO_INDEX": "1",
        "UV_OFFLINE": "1",
    }
    assert set(ambient) == candidate_artifacts._PACKAGE_SOURCE_ENVIRONMENT
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)

    env = _isolated_environment(tmp_path / "home", tmp_path / "home/.codex")

    assert env["UV_DEFAULT_INDEX"] == CONTROLLED_UPSTREAM_INDEX
    assert env["PIP_INDEX_URL"] == CONTROLLED_UPSTREAM_INDEX
    assert env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON"] == sys.executable
    for name in ambient.keys() - {"UV_DEFAULT_INDEX", "PIP_INDEX_URL"}:
        assert name not in env


def test_candidate_uv_version_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    assert CANDIDATE_UV_VERSION == "0.11.30"
    monkeypatch.setattr(candidate_artifacts.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/fixture/uv", "--version"],
            0,
            stdout="uv 0.11.30 (fixture)\n",
            stderr="",
        ),
    )

    assert candidate_artifacts._uv() == "/fixture/uv"


@pytest.mark.parametrize(
    "reported",
    [
        "uv 0.11.29\n",
        "uv\n",
        "uvx 0.11.30\n",
        "uv 0.11.30-beta.1\n",
    ],
)
def test_candidate_uv_version_rejects_unpinned_or_malformed_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    reported: str,
) -> None:
    monkeypatch.setattr(candidate_artifacts.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/fixture/uv", "--version"],
            0,
            stdout=reported,
            stderr="",
        ),
    )

    with pytest.raises(ArtifactError, match=r"requires uv 0\.11\.30"):
        candidate_artifacts._uv()


def test_runtime_export_scrubs_untrusted_package_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in candidate_artifacts._PACKAGE_SOURCE_ENVIRONMENT:
        monkeypatch.setenv(name, f"ambient-{name.lower()}")
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")
    calls: list[dict[str, str]] = []
    commands: list[list[str]] = []

    def fake_run(command, *, cwd, env=None, umask=None):
        del cwd, umask
        commands.append(list(command))
        calls.append(dict(env or {}))
        if "requirements.txt" in command:
            stdout = "networkx==3.6.1 --hash=sha256:" + "0" * 64 + "\n"
        else:
            stdout = json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "metadata": {"timestamp": "2026-07-15T01:02:03Z"},
                    "serialNumber": "urn:uuid:11111111-1111-1111-1111-111111111111",
                    "specVersion": "1.5",
                    "version": 1,
                }
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(candidate_artifacts, "_run", fake_run)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    candidate_artifacts._export_runtime(
        tmp_path,
        tmp_path / "runtime-requirements.txt",
        tmp_path / "sbom.cdx.json",
    )

    assert len(calls) == 2
    assert all("--all-extras" not in command for command in commands)
    for env in calls:
        assert env["UV_DEFAULT_INDEX"] == CONTROLLED_UPSTREAM_INDEX
        assert env["PIP_INDEX_URL"] == CONTROLLED_UPSTREAM_INDEX
        assert env["UV_NO_CONFIG"] == "1"
        for name in candidate_artifacts._PACKAGE_SOURCE_ENVIRONMENT - {
            "UV_DEFAULT_INDEX",
            "PIP_INDEX_URL",
        }:
            assert name not in env


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("networkx==3.6.1\n", "lack valid SHA-256 hashes"),
        ("networkx==3.6.1 --hash=sha256:bad\n", "lack valid SHA-256 hashes"),
        ("networkx==3.6.1 --hash=sha512:" + "0" * 128 + "\n", "lack valid SHA-256 hashes"),
        ("--hash=sha256:" + "0" * 64 + "\n", "lack a requirement specifier"),
        ("networkx==3.6.1 \\\n", "unterminated continuation"),
        ("# empty export\n", "contain no locked entries"),
    ],
)
def test_hashed_requirements_validation_fails_closed(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    requirements = _write(tmp_path / "requirements.txt", contents)

    with pytest.raises(ArtifactError, match=message):
        candidate_artifacts._assert_hashed_requirements(requirements, label="fixture")


def test_hashed_requirements_validation_accepts_uv_multiline_entries(tmp_path: Path) -> None:
    requirements = _write(
        tmp_path / "requirements.txt",
        "networkx==3.6.1 ; python_full_version >= '3.11' \\\n"
        "    --hash=sha256:" + "0" * 64 + " \\\n"
        "    --hash=sha256:" + "1" * 64 + "\n"
        "numpy==2.4.6 --hash=sha256:" + "2" * 64 + "\n",
    )

    candidate_artifacts._assert_hashed_requirements(requirements, label="fixture")


def test_runtime_export_rejects_unhashed_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="networkx==3.6.1\n",
            stderr="",
        ),
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="candidate runtime requirements lack valid"):
        candidate_artifacts._export_runtime(
            tmp_path,
            tmp_path / "runtime-requirements.txt",
            tmp_path / "sbom.cdx.json",
        )


def test_audit_scope_export_rejects_unhashed_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="networkx==3.6.1\n",
            stderr="",
        ),
    )

    with pytest.raises(ArtifactError, match="all-extras-requirements requirements lack valid"):
        candidate_artifacts._export_audit_scope(
            tmp_path,
            tmp_path / "all-extras-requirements.txt",
            arguments=("--all-extras", "--no-dev"),
        )


def test_hashed_requirements_audit_never_scans_the_editable_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirements = _write(
        tmp_path / "runtime-requirements.txt",
        "networkx==3.6.1 --hash=sha256:" + "0" * 64 + "\n",
    )
    commands: list[list[str]] = []

    def fake_run(command, *, cwd, env=None, umask=None):
        del cwd, env, umask
        commands.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {"dependencies": [{"name": "networkx", "version": "3.6.1", "vulns": []}]}
            ),
            stderr="No known vulnerabilities found\n",
        )

    monkeypatch.setattr(candidate_artifacts, "_run", fake_run)

    result = candidate_artifacts._audit_requirements(
        requirements,
        cwd=tmp_path,
        label="candidate runtime",
        expected_dependency_count=1,
    )

    assert result == {"dependency_count": 1, "vulnerability_count": 0}
    assert len(commands) == 1
    command = commands[0]
    assert command[:3] == [sys.executable, "-m", "pip_audit"]
    assert "--strict" in command
    assert "--require-hashes" in command
    assert "--no-deps" in command
    assert "--disable-pip" in command
    assert command[command.index("--requirement") + 1] == str(requirements)
    assert "--skip-editable" not in command


def test_hashed_requirements_audit_rejects_unresolved_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirements = _write(
        tmp_path / "runtime-requirements.txt",
        "fixture==1.0 --hash=sha256:" + "0" * 64 + "\n",
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {"dependencies": [{"name": "fixture", "skip_reason": "unresolved"}]}
            ),
            stderr="",
        ),
    )

    with pytest.raises(ArtifactError, match="unresolved dependency"):
        candidate_artifacts._audit_requirements(
            requirements,
            cwd=tmp_path,
            label="candidate runtime",
            expected_dependency_count=1,
        )


def test_hashed_requirements_audit_rejects_incomplete_record_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirements = _write(
        tmp_path / "runtime-requirements.txt",
        "fixture==1.0 --hash=sha256:" + "0" * 64 + "\n",
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {"dependencies": [{"name": "fixture", "version": "1.0", "vulns": []}]}
            ),
            stderr="",
        ),
    )

    with pytest.raises(ArtifactError, match="audited 1 of 2 locked records"):
        candidate_artifacts._audit_requirements(
            requirements,
            cwd=tmp_path,
            label="complete lock",
            expected_dependency_count=2,
        )


def test_complete_lock_audit_inputs_include_marker_inapplicable_versions(
    tmp_path: Path,
) -> None:
    lock = f"""
version = 1

[[package]]
name = "graphifyy"
version = "0.9.16+workspace.1"
source = {{ editable = "." }}

[[package]]
name = "networkx"
version = "3.4.2"
source = {{ registry = "{CONTROLLED_UPSTREAM_INDEX}" }}
resolution-markers = ["python_full_version < '3.11'"]
sdist = {{ hash = "sha256:{'0' * 64}" }}

[[package]]
name = "networkx"
version = "3.6.1"
source = {{ registry = "{CONTROLLED_UPSTREAM_INDEX}" }}
resolution-markers = ["python_full_version >= '3.11'"]
wheels = [{{ hash = "sha256:{'1' * 64}" }}]

[[package]]
name = "colorama"
version = "0.4.6"
source = {{ registry = "{CONTROLLED_UPSTREAM_INDEX}" }}
resolution-markers = ["sys_platform == 'win32'"]
wheels = [{{ hash = "sha256:{'2' * 64}" }}]
"""
    _write(tmp_path / "uv.lock", lock)

    cohorts = candidate_artifacts._locked_registry_requirement_files(
        tmp_path,
        tmp_path / "audit-inputs",
    )

    assert [count for _, count in cohorts] == [2, 1]
    contents = [path.read_text(encoding="utf-8") for path, _ in cohorts]
    assert "colorama==0.4.6" in contents[0]
    assert "networkx==3.4.2" in contents[0]
    assert "networkx==3.6.1" in contents[1]
    assert all(";" not in content for content in contents)


@pytest.mark.parametrize(
    ("source", "artifacts", "message"),
    [
        ('{ git = "https://example.invalid/fixture.git" }', 'sdist = { hash = "sha256:' + "0" * 64 + '" }', "not registry-auditable"),
        ('{ registry = "https://example.invalid/simple" }', 'sdist = { hash = "sha256:' + "0" * 64 + '" }', "untrusted registry"),
        ('{ registry = "https://pypi.org/simple" }', "", "lacks valid SHA-256"),
    ],
)
def test_complete_lock_audit_inputs_reject_unauditable_records(
    tmp_path: Path,
    source: str,
    artifacts: str,
    message: str,
) -> None:
    lock = f"""
version = 1

[[package]]
name = "graphifyy"
version = "0.9.16+workspace.1"
source = {{ editable = "." }}

[[package]]
name = "fixture"
version = "1.0"
source = {source}
{artifacts}
"""
    _write(tmp_path / "uv.lock", lock)

    with pytest.raises(ArtifactError, match=message):
        candidate_artifacts._locked_registry_requirement_files(
            tmp_path,
            tmp_path / "audit-inputs",
        )


def test_candidate_audit_covers_runtime_all_extras_and_dev_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "candidate"
    _write(repo / "uv.lock", "version = 1\n")
    _write(artifact_root / "trusted-manifest.json", "{}\n")
    _write(artifact_root / "compatibility.json", "{}\n")
    _write(
        artifact_root / "provenance.json",
        json.dumps({"fork_commit": "head", "fork_tree": "tree"}),
    )
    for name in (WHEEL_NAME, "runtime-requirements.txt", "sbom.cdx.json"):
        _write(artifact_root / name, b"fixture")

    class FakeCompatibility:
        @classmethod
        def from_mapping(cls, _data):
            return cls()

        def to_dict(self):
            return {"fork_commit": "head", "runtime_lock_sha256": "lock-sha256"}

    monkeypatch.setattr(candidate_artifacts, "CompatibilityManifest", FakeCompatibility)
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_assert_candidate_source",
        lambda _repo: ("head", "tree"),
    )
    monkeypatch.setattr(candidate_artifacts, "verify_trusted_manifest", lambda **_kwargs: None)
    monkeypatch.setattr(
        candidate_artifacts,
        "_validate_candidate_runtime_authority",
        lambda **_kwargs: (None, b"fixture", "runtime-authority-sha256"),
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "sha256_file",
        lambda path: "lock-sha256" if path.name == "uv.lock" else f"sha256:{path.name}",
    )
    exports: list[tuple[str, ...]] = []

    def fake_export(_repo, destination, *, arguments):
        exports.append(arguments)
        _write(destination, "networkx==3.6.1 --hash=sha256:" + "0" * 64 + "\n")

    def fake_locked_registry(_repo, destination):
        first = _write(destination / "locked-registry-1.txt", "attrs==26.1.0\n")
        second = _write(destination / "locked-registry-2.txt", "networkx==3.4.2\n")
        return [(first, 1), (second, 1)]

    install_inputs: list[tuple[Path, Path]] = []

    def fake_install(*, wheel, requirements, work_root):
        del work_root
        install_inputs.append((wheel, requirements))
        return {"distribution": "graphifyy", "version": "0.9.16+workspace.1"}

    audited: list[tuple[str, str, int | None]] = []

    def fake_audit(requirements, *, cwd, label, expected_dependency_count=None):
        del cwd
        audited.append((requirements.name, label, expected_dependency_count))
        return {"dependency_count": 1, "vulnerability_count": 0}

    monkeypatch.setattr(candidate_artifacts, "_export_audit_scope", fake_export)
    monkeypatch.setattr(
        candidate_artifacts,
        "_locked_registry_requirement_files",
        fake_locked_registry,
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "_verify_noneditable_candidate_install",
        fake_install,
    )
    monkeypatch.setattr(candidate_artifacts, "_audit_requirements", fake_audit)
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="pip-audit 2.10.0\n" if "pip_audit" in command else "uv 0.11.30\n",
            stderr="",
        ),
    )

    result = candidate_artifacts.audit_candidate(repo_root=repo, artifact_root=artifact_root)

    assert exports == [("--no-dev", "--all-extras"), ("--only-dev",)]
    assert install_inputs == [
        (artifact_root / WHEEL_NAME, artifact_root / "runtime-requirements.txt")
    ]
    assert audited == [
        ("runtime-requirements.txt", "candidate runtime", None),
        ("all-extras-requirements.txt", "locked runtime plus all extras", None),
        ("dev-requirements.txt", "locked development dependencies", None),
        ("locked-registry-1.txt", "complete locked registry cohort 1", 1),
        ("locked-registry-2.txt", "complete locked registry cohort 2", 1),
    ]
    audits = result["audits"]
    assert isinstance(audits, dict)
    assert set(audits) == {"runtime", "all_extras", "dev", "all_locked_registry_records"}
    assert audits["all_locked_registry_records"] == {
        "cohort_count": 2,
        "dependency_count": 2,
        "vulnerability_count": 0,
    }


@pytest.mark.parametrize(
    ("compatibility", "provenance", "message"),
    [
        (
            {"fork_commit": "different-head", "runtime_lock_sha256": "lock-sha256"},
            {"fork_commit": "head", "fork_tree": "tree"},
            "compatibility does not match",
        ),
        (
            {"fork_commit": "head", "runtime_lock_sha256": "different-lock"},
            {"fork_commit": "head", "fork_tree": "tree"},
            "compatibility does not match",
        ),
        (
            {"fork_commit": "head", "runtime_lock_sha256": "lock-sha256"},
            {"fork_commit": "different-head", "fork_tree": "tree"},
            "provenance does not match",
        ),
        (
            {"fork_commit": "head", "runtime_lock_sha256": "lock-sha256"},
            {"fork_commit": "head", "fork_tree": "different-tree"},
            "provenance does not match",
        ),
    ],
)
def test_candidate_audit_rejects_resigned_checkout_or_lock_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compatibility: dict[str, str],
    provenance: dict[str, str],
    message: str,
) -> None:
    repo = tmp_path / "repo"
    artifact_root = tmp_path / "candidate"
    _write(repo / "uv.lock", "version = 1\n")
    _write(artifact_root / "trusted-manifest.json", "{}\n")
    _write(artifact_root / "compatibility.json", "{}\n")
    _write(artifact_root / "provenance.json", json.dumps(provenance))

    class FakeCompatibility:
        @classmethod
        def from_mapping(cls, _data):
            return cls()

        def to_dict(self):
            return compatibility

    monkeypatch.setattr(candidate_artifacts, "CompatibilityManifest", FakeCompatibility)
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_assert_candidate_source",
        lambda _repo: ("head", "tree"),
    )
    monkeypatch.setattr(candidate_artifacts, "verify_trusted_manifest", lambda **_kwargs: None)
    monkeypatch.setattr(
        candidate_artifacts,
        "_validate_candidate_runtime_authority",
        lambda **_kwargs: (None, b"fixture", "runtime-authority-sha256"),
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "sha256_file",
        lambda path: "lock-sha256" if path.name == "uv.lock" else f"sha256:{path.name}",
    )

    with pytest.raises(ArtifactError, match=message):
        candidate_artifacts.audit_candidate(repo_root=repo, artifact_root=artifact_root)


def test_candidate_install_rejects_editable_directory_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write(tmp_path / WHEEL_NAME, b"fixture")
    requirements = _write(
        tmp_path / "runtime-requirements.txt",
        "networkx==3.6.1 --hash=sha256:" + "0" * 64 + "\n",
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "_wheel_metadata",
        lambda _wheel: {"distribution": "graphifyy", "version": "0.9.16+workspace.1"},
    )
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")

    def fake_run(command, **_kwargs):
        stdout = ""
        if Path(command[0]).name in {"graphify", "graphify.exe"} and "--version" in command:
            stdout = "graphify 0.9.16+workspace.1\n"
        if "-c" in command:
            stdout = json.dumps(
                {
                    "version": "0.9.16+workspace.1",
                    "module_file": str(
                        tmp_path / "candidate-venv/lib/python/site-packages/graphify/__init__.py"
                    ),
                    "direct_url": {
                        "url": tmp_path.as_uri(),
                        "dir_info": {"editable": True},
                    },
                }
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(candidate_artifacts, "_run", fake_run)

    with pytest.raises(ArtifactError, match="not bound to the non-editable wheel archive"):
        candidate_artifacts._verify_noneditable_candidate_install(
            wheel=wheel,
            requirements=requirements,
            work_root=tmp_path,
        )


def test_candidate_install_rejects_broken_wheel_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write(tmp_path / WHEEL_NAME, b"fixture")
    requirements = _write(
        tmp_path / "runtime-requirements.txt",
        "networkx==3.6.1 --hash=sha256:" + "0" * 64 + "\n",
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "_wheel_metadata",
        lambda _wheel: {"distribution": "graphifyy", "version": "0.9.16+workspace.1"},
    )
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")

    def fake_run(command, **_kwargs):
        if Path(command[0]).name in {"graphify", "graphify.exe"}:
            raise ArtifactError("broken wheel console")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(candidate_artifacts, "_run", fake_run)

    with pytest.raises(ArtifactError, match="broken wheel console"):
        candidate_artifacts._verify_noneditable_candidate_install(
            wheel=wheel,
            requirements=requirements,
            work_root=tmp_path,
        )


def test_candidate_install_accepts_module_under_resolved_symlinked_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write(tmp_path / WHEEL_NAME, b"fixture")
    requirements = _write(
        tmp_path / "runtime-requirements.txt",
        "networkx==3.6.1 --hash=sha256:" + "0" * 64 + "\n",
    )
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    module_file = real_root / "candidate-venv/lib/python/site-packages/graphify/__init__.py"
    monkeypatch.setattr(
        candidate_artifacts,
        "_wheel_metadata",
        lambda _wheel: {"distribution": "graphifyy", "version": "0.9.16+workspace.1"},
    )
    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")

    def fake_run(command, **_kwargs):
        stdout = ""
        if Path(command[0]).name in {"graphify", "graphify.exe"} and "--version" in command:
            stdout = "graphify 0.9.16+workspace.1\n"
        if "-c" in command:
            stdout = json.dumps(
                {
                    "version": "0.9.16+workspace.1",
                    "module_file": str(module_file),
                    "direct_url": {
                        "url": wheel.as_uri().replace("%2B", "+"),
                        "archive_info": {},
                    },
                }
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(candidate_artifacts, "_run", fake_run)

    result = candidate_artifacts._verify_noneditable_candidate_install(
        wheel=wheel,
        requirements=requirements,
        work_root=linked_root,
    )

    assert result["editable"] is False
    assert result["module_file"] == str(module_file)


def test_trusted_manifest_rejects_empty_artifact_set(tmp_path: Path) -> None:
    trusted = canonical_json_bytes(
        {
            "contract": "graphify.workspace.artifact_manifest",
            "schema_version": 1,
            "manifest_version": 1,
            "artifacts": [],
        }
    )

    with pytest.raises(ArtifactError, match="at least one artifact"):
        verify_trusted_manifest(artifact_root=tmp_path, trusted_manifest=trusted)

    with pytest.raises(ArtifactError, match="at least one artifact"):
        write_trusted_manifest(artifact_root=tmp_path, artifact_names=[])
    assert not (tmp_path / "trusted-manifest.json").exists()


def test_candidate_entrypoints_reject_wrong_uv_before_writing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_uv() -> str:
        raise ArtifactError("candidate artifact generation requires uv 0.11.30")

    monkeypatch.setattr(candidate_artifacts, "_uv", reject_uv)
    candidate_root = tmp_path / "candidate"
    proof_root = tmp_path / "proof"

    with pytest.raises(ArtifactError, match=r"requires uv 0\.11\.30"):
        build_candidate(repo_root=tmp_path / "repo", output_root=candidate_root)
    with pytest.raises(ArtifactError, match=r"requires uv 0\.11\.30"):
        prove_candidate(artifact_root=tmp_path / "artifacts", proof_root=proof_root)

    assert not candidate_root.exists()
    assert not proof_root.exists()


def test_candidate_assembly_freezes_runtime_authority_across_identical_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _write(repo / "uv.lock", "version = 1\n")

    def build_wheel(_repo: Path, destination: Path) -> Path:
        return _write(destination, b"fixture-wheel")

    def export_runtime(_source: Path, requirements: Path, sbom: Path) -> None:
        _write(requirements, "networkx==3.6.1\n")
        _write(sbom, "{}\n")

    def static_bundles(*, output_root: Path, wheel: Path, **_kwargs: object) -> dict[str, Path]:
        names = (
            "skill-bundle.zip",
            "contract-bundle.zip",
            "fixture-bundle.zip",
            "fixture-manifest.json",
            "runtime-bundle.zip",
        )
        artifacts = {name: _write(output_root / name, name) for name in names}
        artifacts[wheel.name] = _write(output_root / wheel.name, wheel.read_bytes())
        return artifacts

    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_assert_candidate_source",
        lambda _repo: ("a" * 40, "b" * 40),
    )
    monkeypatch.setattr(candidate_artifacts, "_assert_safe_output_root", lambda *_args: None)
    monkeypatch.setattr(candidate_artifacts, "_extract_head", lambda *_args: None)
    monkeypatch.setattr(candidate_artifacts, "_render_codex_skill", lambda _source: None)
    monkeypatch.setattr(candidate_artifacts, "_build_reproducible_wheel", build_wheel)
    monkeypatch.setattr(candidate_artifacts, "_export_runtime", export_runtime)
    monkeypatch.setattr(candidate_artifacts, "build_static_bundles", static_bundles)
    monkeypatch.setattr(
        candidate_artifacts,
        "_build_offline_rollback",
        lambda destination: _write(destination, b"fixture-rollback"),
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="uv 0.11.30\n", stderr=""
        ),
    )

    first = tmp_path / "candidate-one"
    second = tmp_path / "candidate-two"
    first_result = build_candidate(repo_root=repo, output_root=first)
    build_candidate(repo_root=repo, output_root=second)

    runtime = first / "runtime-manifest.json"
    authority = candidate_artifacts.WorkspaceRuntimeAuthority.from_json(runtime.read_bytes())
    compatibility = (first / "compatibility.json").read_bytes()
    trusted = (first / "trusted-manifest.json").read_bytes()
    manifest = candidate_artifacts.ArtifactManifest.from_json(trusted).to_dict()
    runtime_entry = next(entry for entry in manifest["artifacts"] if entry["path"] == runtime.name)

    assert authority.canonical == runtime.read_bytes()
    assert authority.compatibility_manifest.canonical == compatibility
    assert authority.semantic_queue_policy.to_dict() == {
        "contract": "graphify.workspace.semantic_queue_policy.internal",
        "format_version": 1,
        "max_items": 8,
        "max_bytes": 16_384,
        "retry_budget": 1,
    }
    assert runtime_entry == {
        "path": runtime.name,
        "file_type": "regular_file",
        "size": runtime.stat().st_size,
        "sha256": sha256_file(runtime),
        "mode": "0644",
    }
    assert first_result["runtime_manifest_sha256"] == sha256_file(runtime)
    assert compare_candidate_roots(first=first, second=second)["byte_identical"] is True


def test_candidate_proof_wires_runtime_authority_into_installation_and_compensation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "candidate"
    artifacts, _ = _build_candidate(tmp_path / "repo", artifact_root)
    runtime = _write(artifact_root / "runtime-manifest.json", b"runtime-authority\n")
    rollback = _write(artifact_root / "offline-rollback.zip", b"rollback\n")
    artifacts.update({runtime.name: runtime, rollback.name: rollback})
    trusted = write_trusted_manifest(
        artifact_root=artifact_root,
        artifact_names=sorted(artifacts),
    )
    proof_root = tmp_path / "proof"
    expected_skill_tree = skill_bundle_tree_sha256(artifact_root / "skill-bundle.zip")
    installation_calls: list[dict[str, object]] = []
    compensation_calls: list[dict[str, object]] = []

    monkeypatch.setattr(candidate_artifacts, "_uv", lambda: "/fixture/uv")

    def prove_installation(**kwargs: object) -> dict[str, object]:
        installation_calls.append(kwargs)
        return {"absent_target_compensation": True}

    def install_clean_home(*, home: Path, **_kwargs: object) -> dict[str, str]:
        _write(home / ".local/bin/graphify", "#!/bin/sh\n", executable=True)
        _write(home / ".codex/skills/graphify/SKILL.md", "candidate skill\n")
        return {
            "dependency_manifest_sha256": "dependencies",
            "skill_tree_sha256": expected_skill_tree,
        }

    def compensate(**kwargs: object) -> dict[str, object]:
        compensation_calls.append(kwargs)
        return {
            "restored_modes": {"runtime": "0600"},
            "restored": {
                "runtime": hashlib.sha256(
                    candidate_artifacts._PRIOR_FILES["runtime"]
                ).hexdigest()
            },
            "runtime_target_restored": True,
            "generations_unchanged": True,
        }

    monkeypatch.setattr(
        candidate_artifacts, "_prove_runtime_authority_installation", prove_installation
    )
    monkeypatch.setattr(
        candidate_artifacts,
        "prove_independent_tamper_rejection",
        lambda **_kwargs: {"runtime-manifest.json": "rejected"},
    )
    monkeypatch.setattr(candidate_artifacts, "_install_clean_home", install_clean_home)
    monkeypatch.setattr(
        candidate_artifacts,
        "_download_verified_upstream_wheel",
        lambda _destination: {
            "path": tmp_path / "upstream.whl",
            "logical_requirement": "graphifyy==0.9.16",
            "source_url": "https://example.invalid",
            "filename": "upstream.whl",
            "sha256": "upstream",
        },
    )
    monkeypatch.setattr(candidate_artifacts, "_isolated_environment", lambda *_args: {})
    monkeypatch.setattr(candidate_artifacts, "_write_prior_home", lambda *_args: None)
    monkeypatch.setattr(candidate_artifacts, "run_disposable_compensation_proof", compensate)
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="graphify 0.9.16\n", stderr=""
        ),
    )

    summary = prove_candidate(artifact_root=artifact_root, proof_root=proof_root)

    assert installation_calls == [
        {
            "artifact_root": artifact_root,
            "trusted_manifest": trusted,
            "expected_sha256": sha256_file(runtime),
            "proof_root": proof_root / "runtime-authority-installation",
        }
    ]
    assert len(compensation_calls) == 1
    assert compensation_calls[0]["candidate_files"] == {
        "binary": b"#!/bin/sh\n",
        "runtime": runtime.read_bytes(),
        "skill": b"candidate skill\n",
        "service": b"candidate-login-service-fixture\n",
    }
    assert json.loads((proof_root / "runtime-authority-installation-proof.json").read_text()) == {
        "absent_target_compensation": True
    }
    assert summary["runtime_authority_installation"] is True
    assert summary["absent_target_compensation"] is True
    assert summary["preexisting_target_compensation"] is True


def test_upstream_wheel_selector_binds_exact_pypi_file_and_digest() -> None:
    expected_url = f"https://files.pythonhosted.org/packages/fixture/{UPSTREAM_WHEEL_NAME}"
    metadata = {
        "info": {"name": "graphifyy", "version": "0.9.16"},
        "urls": [
            {
                "filename": UPSTREAM_WHEEL_NAME,
                "packagetype": "bdist_wheel",
                "url": expected_url,
                "digests": {"sha256": UPSTREAM_WHEEL_SHA256},
            }
        ],
    }

    assert _select_upstream_wheel(metadata) == expected_url
    metadata["urls"][0]["digests"]["sha256"] = "0" * 64
    with pytest.raises(ArtifactError, match="digest"):
        _select_upstream_wheel(metadata)


def test_upstream_wheel_download_rejects_substituted_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    expected_url = f"https://files.pythonhosted.org/packages/fixture/{UPSTREAM_WHEEL_NAME}"
    metadata = {
        "info": {"name": "graphifyy", "version": "0.9.16"},
        "urls": [
            {
                "filename": UPSTREAM_WHEEL_NAME,
                "packagetype": "bdist_wheel",
                "url": expected_url,
                "digests": {"sha256": UPSTREAM_WHEEL_SHA256},
            }
        ],
    }

    def fake_fetch(url: str) -> tuple[bytes, str]:
        if url.endswith("/json"):
            return json.dumps(metadata).encode(), url
        return b"substituted-wheel", url

    monkeypatch.setattr("tools.workspace_artifacts.candidate._fetch_url", fake_fetch)

    with pytest.raises(ArtifactError, match="downloaded upstream wheel digest mismatch"):
        _download_verified_upstream_wheel(tmp_path)


def test_upstream_fetch_rejects_non_https_and_unapproved_hosts() -> None:
    with pytest.raises(ArtifactError, match="untrusted upstream provenance URL"):
        _fetch_url("file:///etc/hosts")
    with pytest.raises(ArtifactError, match="untrusted upstream provenance URL"):
        _fetch_url("https://example.invalid/graphifyy.whl")


def test_upstream_fetch_requires_explicit_http_200(monkeypatch) -> None:
    class StatuslessResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b"{}"

        def geturl(self) -> str:
            return "https://pypi.org/pypi/graphifyy/0.9.16/json"

    monkeypatch.setattr(
        "tools.workspace_artifacts.candidate.urlopen",
        lambda *_args, **_kwargs: StatuslessResponse(),
    )

    with pytest.raises(ArtifactError, match="missing an explicit HTTP status"):
        _fetch_url("https://pypi.org/pypi/graphifyy/0.9.16/json")


@pytest.mark.parametrize("ambient_path", [None, ""])
def test_isolated_environment_never_adds_current_directory_to_path(
    tmp_path: Path,
    monkeypatch,
    ambient_path: str | None,
) -> None:
    if ambient_path is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", ambient_path)

    env = _isolated_environment(tmp_path / "home", tmp_path / "home/.codex")

    assert env["PATH"] == str(tmp_path / "home/.local/bin")
    assert "" not in env["PATH"].split(os.pathsep)


def test_git_archive_extraction_rejects_links(tmp_path: Path) -> None:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        link = tarfile.TarInfo("unsafe-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "outside"
        archive.addfile(link)

    with pytest.raises(ArtifactError, match="unsupported Git archive member"):
        _extract_git_archive(stream.getvalue(), tmp_path / "source")


def test_complete_candidate_roots_compare_every_output_file(tmp_path: Path) -> None:
    _build_candidate(tmp_path / "repo-one", tmp_path / "out-one")
    _build_candidate(tmp_path / "repo-two", tmp_path / "out-two")

    comparison = compare_candidate_roots(
        first=tmp_path / "out-one",
        second=tmp_path / "out-two",
    )

    assert comparison["byte_identical"] is True
    assert comparison["file_count"] == 7
    _write(tmp_path / "out-two/untrusted-extra.txt", "drift\n")
    with pytest.raises(ArtifactError, match="artifact set mismatch"):
        compare_candidate_roots(
            first=tmp_path / "out-one",
            second=tmp_path / "out-two",
        )


def test_candidate_artifact_modes_and_hashes_are_umask_independent(tmp_path: Path) -> None:
    outputs = []
    for name, mask in (("normal", 0o022), ("restrictive", 0o077)):
        previous = os.umask(mask)
        try:
            output = tmp_path / name
            _build_candidate(tmp_path / f"repo-{name}", output)
        finally:
            os.umask(previous)
        outputs.append(output)

    comparison = compare_candidate_roots(first=outputs[0], second=outputs[1])

    assert comparison["byte_identical"] is True
    for output in outputs:
        assert {path.stat().st_mode & 0o777 for path in output.iterdir()} == {0o644}


def test_candidate_subprocess_applies_explicit_umask(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        _run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('created').write_bytes(b'x')",
            ],
            cwd=tmp_path,
            umask=0o022,
        )
    finally:
        os.umask(previous)

    assert (tmp_path / "created").stat().st_mode & 0o777 == 0o644


def test_candidate_offline_rollback_is_umask_independent(tmp_path: Path) -> None:
    bundles = []
    for name, mask in (("normal", 0o022), ("restrictive", 0o077)):
        previous = os.umask(mask)
        try:
            bundle = tmp_path / f"{name}.zip"
            _build_offline_rollback(bundle)
        finally:
            os.umask(previous)
        bundles.append(bundle)

    assert bundles[0].read_bytes() == bundles[1].read_bytes()


def test_installed_skill_tree_digest_is_bound_to_skill_bundle(tmp_path: Path) -> None:
    artifacts, _ = _build_candidate(tmp_path / "repo", tmp_path / "candidate")
    skill_bundle = artifacts["skill-bundle.zip"]
    with zipfile.ZipFile(skill_bundle) as archive:
        archive.extractall(tmp_path / "extracted")
    installed = tmp_path / "extracted/skill"

    expected = skill_bundle_tree_sha256(skill_bundle)
    assert expected == skill_bundle_tree_sha256(skill_bundle, installed_root=installed)
    _write(installed / "references/query.md", "tampered\n")
    with pytest.raises(ArtifactError, match="installed skill tree does not match"):
        skill_bundle_tree_sha256(skill_bundle, installed_root=installed)


def test_candidate_module_exposes_explicit_build_and_audit_flags() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tools.workspace_artifacts", "build", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--repo-root" in result.stdout
    assert "--output-root" in result.stdout
    assert "--comparison-output-root" in result.stdout

    audit = subprocess.run(
        [sys.executable, "-m", "tools.workspace_artifacts", "audit", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--repo-root" in audit.stdout
    assert "--artifact-root" in audit.stdout


def test_verify_cli_reports_missing_manifest_without_traceback(tmp_path: Path) -> None:
    missing = tmp_path / "missing-manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.workspace_artifacts",
            "verify",
            "--artifact-root",
            str(tmp_path / "candidate"),
            "--manifest",
            str(missing),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert f"cannot read trusted manifest {missing}" in result.stderr
    assert "Traceback" not in result.stderr


def test_absolute_candidate_update_does_not_refresh_real_home_skills(tmp_path: Path) -> None:
    candidate = Path(sys.executable).with_name("graphify")
    if not candidate.is_file():
        pytest.skip("repo-local graphify console script is unavailable")
    project = tmp_path / "project"
    _write(project / "sample.py", "VALUE = 1\n")
    roots = _real_skill_roots()
    before = {root: _skill_root_snapshot(root) for root in roots}
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)

    result = subprocess.run(
        [str(candidate.resolve()), "update", str(project)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert {root: _skill_root_snapshot(root) for root in roots} == before


def test_locally_frozen_manifest_rejects_independent_required_artifact_tamper(
    tmp_path: Path,
) -> None:
    artifacts, _ = _build_candidate(tmp_path / "repo", tmp_path / "candidate")
    runtime_authority = _write(
        tmp_path / "candidate/runtime-manifest.json",
        canonical_json_bytes({"fixture": "candidate-runtime-authority"}),
    )
    artifacts[runtime_authority.name] = runtime_authority
    trusted = write_trusted_manifest(
        artifact_root=tmp_path / "candidate",
        artifact_names=sorted(artifacts),
    )

    results = prove_independent_tamper_rejection(
        artifact_root=tmp_path / "candidate",
        trusted_manifest=trusted,
    )

    assert set(results) == {
        WHEEL_NAME,
        "skill-bundle.zip",
        "contract-bundle.zip",
        "fixture-manifest.json",
        "runtime-manifest.json",
    }
    assert all("digest mismatch" in result for result in results.values())
    verify_trusted_manifest(artifact_root=tmp_path / "candidate", trusted_manifest=trusted)


def test_trusted_manifest_rejects_mode_tamper_and_unexpected_broken_symlink(
    tmp_path: Path,
) -> None:
    artifacts, trusted = _build_candidate(tmp_path / "repo", tmp_path / "candidate")
    target = artifacts["contract-bundle.zip"]
    target.chmod(0o755)
    with pytest.raises(ArtifactError, match="mode mismatch"):
        verify_trusted_manifest(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
        )
    target.chmod(0o644)
    (tmp_path / "candidate/broken-link").symlink_to("missing-target")
    with pytest.raises(ArtifactError, match="forbidden filesystem node"):
        verify_trusted_manifest(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
        )


def test_trusted_manifest_rejects_nested_reserved_name_and_fifo(tmp_path: Path) -> None:
    _, trusted = _build_candidate(tmp_path / "repo", tmp_path / "candidate")
    nested = _write(
        tmp_path / "candidate/nested/trusted-manifest.json",
        "not-the-root-anchor\n",
    )
    with pytest.raises(ArtifactError, match="artifact set mismatch"):
        verify_trusted_manifest(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
        )
    nested.unlink()
    nested.parent.rmdir()
    fifo = tmp_path / "candidate/unexpected-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ArtifactError, match="forbidden filesystem node"):
        verify_trusted_manifest(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
        )
    fifo.unlink()
    os.link(
        tmp_path / "candidate/contract-bundle.zip",
        tmp_path / "candidate/unexpected-hardlink",
    )
    with pytest.raises(ArtifactError, match="forbidden filesystem node"):
        verify_trusted_manifest(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
        )


def test_skill_tree_digest_and_real_home_guard_reject_extra_symlink(tmp_path: Path) -> None:
    artifacts, _ = _build_candidate(tmp_path / "repo", tmp_path / "candidate")
    with zipfile.ZipFile(artifacts["skill-bundle.zip"]) as archive:
        archive.extractall(tmp_path / "extracted")
    skill_root = tmp_path / "extracted/skill"
    (skill_root / "dangling").symlink_to("missing")

    with pytest.raises(ArtifactError, match="forbidden filesystem node"):
        skill_bundle_tree_sha256(
            artifacts["skill-bundle.zip"],
            installed_root=skill_root,
        )
    with pytest.raises(ArtifactError, match="forbidden filesystem node"):
        _skill_root_snapshot(skill_root)


def test_tamper_proof_rejects_escaping_or_untrusted_artifact_names(tmp_path: Path) -> None:
    _, trusted = _build_candidate(tmp_path / "repo", tmp_path / "candidate")
    outside = _write(tmp_path / "outside", b"outside")
    outside_before = outside.read_bytes()

    with pytest.raises(ArtifactError, match="invalid tamper artifact name"):
        prove_independent_tamper_rejection(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
            artifact_names=["../outside"],
        )
    assert outside.read_bytes() == outside_before
    with pytest.raises(ArtifactError, match="not covered by trusted manifest"):
        prove_independent_tamper_rejection(
            artifact_root=tmp_path / "candidate",
            trusted_manifest=trusted,
            artifact_names=["untrusted-extra"],
        )


@pytest.mark.parametrize("fail_after", ["binary", "runtime", "skill", "service"])
def test_disposable_home_failpoint_compensates_offline_and_preserves_generations(
    tmp_path: Path,
    monkeypatch,
    fail_after: str,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    binary = _write(home / ".local/bin/graphify", "#!/bin/sh\necho prior\n", executable=True)
    runtime = _write(home / ".local/state/graphify/runtime-manifest.json", "prior-runtime\n")
    skill = _write(codex_home / "skills/graphify/SKILL.md", "prior-skill\n")
    service = _write(
        home / "Library/LaunchAgents/com.graphify.fixture.plist",
        "prior-service\n",
    )
    canary = _write(
        home / ".local/state/graphify/workspaces/fixture/generations/gen-canary/receipt.json",
        "generation-canary\n",
    )
    generation_graph = _write(
        canary.parent / "graphify-out/graph.json",
        '{"nodes": []}\n',
    )
    last_good = _write(
        canary.parents[1] / "gen-last-good/receipt.json",
        "generation-last-good\n",
    )
    before = {
        "binary": sha256_file(binary),
        "runtime": sha256_file(runtime),
        "skill": sha256_file(skill),
        "service": sha256_file(service),
    }
    generation_root = canary.parents[1]
    generations_before = strict_tree_sha256(generation_root)
    bundle_one = tmp_path / "rollback-one.zip"
    bundle_two = tmp_path / "rollback-two.zip"
    snapshot_disposable_home(home=home, codex_home=codex_home, rollback_bundle=bundle_one)
    snapshot_disposable_home(home=home, codex_home=codex_home, rollback_bundle=bundle_two)
    assert bundle_one.read_bytes() == bundle_two.read_bytes()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    proof = run_disposable_compensation_proof(
        home=home,
        codex_home=codex_home,
        rollback_bundle=bundle_one,
        candidate_files={
            "binary": b"#!/bin/sh\necho candidate\n",
            "runtime": b"candidate-runtime\n",
            "skill": b"candidate-skill\n",
            "service": b"candidate-service\n",
        },
        fail_after=fail_after,
    )

    assert proof["failpoint_triggered"] is True
    assert proof["offline"] is True
    pair_validation = proof["contract_pair_validation"]
    assert isinstance(pair_validation, dict)
    assert pair_validation["validated"] is True
    assert pair_validation["installer_item_count"] == 6
    assert pair_validation["restore_action_count"] == 4
    assert pair_validation["remove_action_count"] == 2
    assert pair_validation["restore_mapping_count"] == 4
    assert proof["installer_transaction_sha256"]
    assert proof["compensation_plan_sha256"]
    assert proof["offline_rollback_sha256"]
    transaction = proof["installer_transaction_preimage"]
    plan = proof["compensation_plan_preimage"]
    execution = proof["compensation_execution"]
    assert isinstance(transaction, dict)
    assert isinstance(plan, dict)
    assert isinstance(execution, dict)
    assert canonical_sha256(transaction) == proof["installer_transaction_sha256"]
    assert canonical_sha256(plan) == proof["compensation_plan_sha256"]
    assert execution["restore_order"] == plan["restore_order"]
    assert execution["remove_if_created"] == plan["remove_if_created"]
    assert execution["restore_artifacts"] == plan["restore_artifacts"]
    item_paths = {item["path"] for item in transaction["items"]}
    assert str(skill.parent / ".graphify_version") in item_paths
    assert str(skill.parent / "references/candidate.md") in item_paths
    assert plan["remove_if_created"] == [
        str(skill.parent / ".graphify_version"),
        str(skill.parent / "references/candidate.md"),
    ]
    assert proof["generations_unchanged"] is True
    assert proof["runtime_target_restored"] is True
    assert proof["restored"] == before
    assert proof["restored_modes"] == {
        "binary": "0755",
        "runtime": "0644",
        "skill": "0644",
        "service": "0644",
    }
    assert proof["generations_tree_sha256_before"] == generations_before
    assert proof["generations_tree_sha256_after"] == generations_before
    assert sha256_file(canary)
    assert sha256_file(generation_graph)
    assert sha256_file(last_good)
    assert binary.read_text() == "#!/bin/sh\necho prior\n"
    assert skill.read_text() == "prior-skill\n"
    assert not (skill.parent / "references").exists()
    assert not (skill.parent / ".graphify_version").exists()


def test_compensation_rejects_untracked_created_file(tmp_path: Path, monkeypatch) -> None:
    home, codex_home, bundle = _disposable_compensation_fixture(tmp_path)
    original = workspace_artifacts._stage_transaction_item

    def stage_with_untracked_file(*, target: Path, data: bytes) -> None:
        original(target=target, data=data)
        if target.name == "SKILL.md":
            _write(target.parent / "untracked-created.txt", "untracked\n")

    monkeypatch.setattr(workspace_artifacts, "_stage_transaction_item", stage_with_untracked_file)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(ArtifactError, match="untracked created or mutated path"):
        run_disposable_compensation_proof(
            home=home,
            codex_home=codex_home,
            rollback_bundle=bundle,
            candidate_files={
                "binary": b"candidate-binary\n",
                "runtime": b"candidate-runtime\n",
                "skill": b"candidate-skill\n",
                "service": b"candidate-service\n",
            },
            fail_after="skill",
        )


def test_compensation_rejects_untracked_mode_mutation(tmp_path: Path, monkeypatch) -> None:
    home, codex_home, bundle = _disposable_compensation_fixture(tmp_path)
    undeclared = _write(home / ".local/bin/undeclared-helper", "helper\n")
    original = workspace_artifacts._stage_transaction_item

    def stage_with_untracked_chmod(*, target: Path, data: bytes) -> None:
        original(target=target, data=data)
        if target.name == "SKILL.md":
            undeclared.chmod(0o600)

    monkeypatch.setattr(workspace_artifacts, "_stage_transaction_item", stage_with_untracked_chmod)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(ArtifactError, match="untracked created or mutated path"):
        run_disposable_compensation_proof(
            home=home,
            codex_home=codex_home,
            rollback_bundle=bundle,
            candidate_files={
                "binary": b"candidate-binary\n",
                "runtime": b"candidate-runtime\n",
                "skill": b"candidate-skill\n",
                "service": b"candidate-service\n",
            },
            fail_after="skill",
        )


def test_compensation_rejects_executor_plan_order_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home, codex_home, bundle = _disposable_compensation_fixture(tmp_path)
    original = workspace_artifacts._restore_offline

    def restore_out_of_order(**kwargs) -> dict[str, object]:
        execution = original(**kwargs)
        restore_order = execution["restore_order"]
        assert isinstance(restore_order, list)
        execution["restore_order"] = list(reversed(restore_order))
        return execution

    monkeypatch.setattr(workspace_artifacts, "_restore_offline", restore_out_of_order)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(ArtifactError, match="executor restore order diverged from plan"):
        run_disposable_compensation_proof(
            home=home,
            codex_home=codex_home,
            rollback_bundle=bundle,
            candidate_files={
                "binary": b"candidate-binary\n",
                "runtime": b"candidate-runtime\n",
                "skill": b"candidate-skill\n",
                "service": b"candidate-service\n",
            },
            fail_after="skill",
        )


def test_offline_compensation_restores_preexisting_skill_tree(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write(home / ".local/bin/graphify", "prior-binary\n", executable=True)
    _write(home / ".local/state/graphify/runtime-manifest.json", "prior-runtime\n")
    skill = _write(codex_home / "skills/graphify/SKILL.md", "prior-skill\n")
    prior_reference = _write(skill.parent / "references/prior.md", "prior-reference\n")
    prior_version = _write(skill.parent / ".graphify_version", "0.9.16")
    _write(home / "Library/LaunchAgents/com.graphify.fixture.plist", "prior-service\n")
    _write(
        home / ".local/state/graphify/workspaces/fixture/generations/gen-canary/receipt.json",
        "generation-canary\n",
    )
    bundle = tmp_path / "rollback.zip"
    snapshot_disposable_home(home=home, codex_home=codex_home, rollback_bundle=bundle)
    reference_sha256 = sha256_file(prior_reference)
    version_sha256 = sha256_file(prior_version)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proof = run_disposable_compensation_proof(
        home=home,
        codex_home=codex_home,
        rollback_bundle=bundle,
        candidate_files={
            "binary": b"candidate-binary\n",
            "runtime": b"candidate-runtime\n",
            "skill": b"candidate-skill\n",
            "service": b"candidate-service\n",
        },
        fail_after="skill",
    )

    assert proof["generations_unchanged"] is True
    assert sha256_file(prior_reference) == reference_sha256
    assert sha256_file(prior_version) == version_sha256
    assert not (skill.parent / "references/candidate.md").exists()


def test_offline_rollback_preserves_allowed_private_file_modes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write(home / ".local/bin/graphify", "prior-binary\n", executable=True)
    runtime = _write(home / ".local/state/graphify/runtime-manifest.json", "prior-runtime\n")
    runtime.chmod(0o600)
    _write(codex_home / "skills/graphify/SKILL.md", "prior-skill\n")
    _write(home / "Library/LaunchAgents/com.graphify.fixture.plist", "prior-service\n")
    bundle = tmp_path / "rollback.zip"

    snapshot_disposable_home(home=home, codex_home=codex_home, rollback_bundle=bundle)

    with zipfile.ZipFile(bundle) as archive:
        rollback = json.loads(archive.read("rollback.json"))
    runtime_entry = next(
        entry for entry in rollback["entries"] if entry["path"] == "snapshot/runtime"
    )
    assert runtime_entry["mode"] == "0600"
