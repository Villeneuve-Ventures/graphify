from __future__ import annotations

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
    assert CANDIDATE_UV_VERSION == "0.11.29"
    monkeypatch.setattr(candidate_artifacts.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(
        candidate_artifacts,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/fixture/uv", "--version"],
            0,
            stdout="uv 0.11.29 (fixture)\n",
            stderr="",
        ),
    )

    assert candidate_artifacts._uv() == "/fixture/uv"


@pytest.mark.parametrize(
    "reported",
    [
        "uv 0.11.30\n",
        "uv\n",
        "uvx 0.11.29\n",
        "uv 0.11.29-beta.1\n",
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

    with pytest.raises(ArtifactError, match="requires uv 0.11.29"):
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
            stdout = "networkx==3.6.1\n"
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
        raise ArtifactError("candidate artifact generation requires uv 0.11.29")

    monkeypatch.setattr(candidate_artifacts, "_uv", reject_uv)
    candidate_root = tmp_path / "candidate"
    proof_root = tmp_path / "proof"

    with pytest.raises(ArtifactError, match="requires uv 0.11.29"):
        build_candidate(repo_root=tmp_path / "repo", output_root=candidate_root)
    with pytest.raises(ArtifactError, match="requires uv 0.11.29"):
        prove_candidate(artifact_root=tmp_path / "artifacts", proof_root=proof_root)

    assert not candidate_root.exists()
    assert not proof_root.exists()


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


def test_candidate_module_exposes_explicit_two_root_build_flags() -> None:
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
    _, trusted = _build_candidate(tmp_path / "repo", tmp_path / "candidate")

    results = prove_independent_tamper_rejection(
        artifact_root=tmp_path / "candidate",
        trusted_manifest=trusted,
    )

    assert set(results) == {
        WHEEL_NAME,
        "skill-bundle.zip",
        "contract-bundle.zip",
        "fixture-manifest.json",
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
    assert proof["restored"] == before
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
