"""Packaging guard (#1121 follow-up): the 5 skillgen guards check the *repo tree*,
not the *built wheel*. A host whose references bundle or always-on block fails to
match the `package-data` globs would pass `--check`/`--audit-coverage` yet make
`graphify install` hard-exit with "not found in package" for real users.

This builds the wheel once and asserts every committed skill artifact ships in it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from graphify.workspace import WORKSPACE_SCHEMA_FILES
from graphify.workspace.contracts import canonical_json_bytes

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "graphify"


def _has_build() -> bool:
    return importlib.util.find_spec("build") is not None


def _skill_bodies() -> list[Path]:
    """Every distinct skill body a platform installs (the SKILL.md is copied from
    one of these). A body missing from the wheel makes `graphify install
    --platform <host>` hard-exit "not found in package" — the exact failure that
    motivated adding the agents platform's skill-agents.md to package-data."""
    from graphify.__main__ import _PLATFORM_CONFIG

    names = {cfg["skill_file"] for cfg in _PLATFORM_CONFIG.values()}
    return sorted({PKG / name for name in names})


def _expected_artifacts() -> list[Path]:
    """Every committed skill body + references/*.md (per host) + always_on/*.md block."""
    bodies = _skill_bodies()
    refs = sorted((PKG / "skills").glob("*/references/*.md"))
    always = sorted((PKG / "always_on").glob("*.md"))
    # Sanity: if these are empty the test wiring is broken, not the wheel.
    assert bodies, "no platform skill bodies found — packaging test mis-wired"
    assert refs, "no skills/*/references/*.md found in repo — packaging test mis-wired"
    assert always, "no always_on/*.md found in repo — packaging test mis-wired"
    return bodies + refs + always


@pytest.fixture(scope="module")
def wheel_build(tmp_path_factory) -> tuple[set[str], str, str, Path]:
    if not _has_build():
        pytest.skip("`python -m build` unavailable (dev extra not installed)")
    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(out), str(REPO)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"wheel build failed:\n{proc.stderr[-800:]}"
    wheels = list(out.glob("graphifyy-*.whl"))
    assert wheels, "no wheel produced"
    wheel = max(wheels, key=lambda p: p.stat().st_mtime)
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1
        metadata = z.read(metadata_names[0]).decode("utf-8")
    return names, proc.stdout + proc.stderr, metadata, wheel


@pytest.fixture(scope="module")
def wheel_namelist(wheel_build: tuple[set[str], str, str, Path]) -> set[str]:
    return wheel_build[0]


def test_wheel_build_uses_current_packaging_metadata_without_tool_warnings(
    wheel_build: tuple[set[str], str, str, Path],
) -> None:
    _, output, metadata, _ = wheel_build
    assert "`project.license` as a TOML table is deprecated" not in output
    assert "The 'wheel' package is no longer the canonical location" not in output
    assert "License-Expression: MIT\n" in metadata
    assert "License-File: LICENSE\n" in metadata
    assert "Requires-Python: >=3.14\n" in metadata


@pytest.mark.parametrize(
    "artifact",
    _expected_artifacts(),
    ids=lambda p: str(p.relative_to(PKG)),
)
def test_skill_artifact_ships_in_wheel(artifact: Path, wheel_namelist: set[str]) -> None:
    rel = "graphify/" + artifact.relative_to(PKG).as_posix()
    assert rel in wheel_namelist, (
        f"{rel} is committed in the repo but NOT in the built wheel — "
        f"`graphify install` would hard-exit for this host. Check the "
        f"[tool.setuptools.package-data] globs in pyproject.toml."
    )


def test_workspace_contract_surface_ships_in_wheel(wheel_namelist: set[str]) -> None:
    expected = {
        "graphify/workspace/__init__.py",
        "graphify/workspace/contracts.py",
        "graphify/workspace/gc_command.py",
        "graphify/workspace/gc.py",
        "graphify/workspace/generations.py",
        "graphify/workspace/identity.py",
        "graphify/workspace/journal.py",
        "graphify/workspace/leases.py",
        "graphify/workspace/persistence.py",
        "graphify/workspace/pointers.py",
        "graphify/workspace/registry.py",
        "graphify/workspace/rollback.py",
        "graphify/workspace/schemas/cli/v1/rollback-request.schema.json",
        "graphify/workspace/schemas/cli/v1/rollback-receipt.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-preview-request.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-preview-result.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-execute-request.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-execute-result.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-reconcile-request.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-reconcile-result.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-purge-request.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-purge-result.schema.json",
        *{
            f"graphify/workspace/schemas/v1/{name}" for name in WORKSPACE_SCHEMA_FILES
        },
    }
    assert expected <= wheel_namelist
    actual_schemas = {
        name
        for name in wheel_namelist
        if name.startswith("graphify/workspace/schemas/v1/")
    }
    assert actual_schemas == {
        f"graphify/workspace/schemas/v1/{name}" for name in WORKSPACE_SCHEMA_FILES
    }


def test_semantic_release_manifest_inventories_exact_real_wheel_bytes(
    wheel_build: tuple[set[str], str, str, Path],
) -> None:
    names, _, _, wheel = wheel_build
    manifest_name = "graphify/workspace/semantic_release_manifest.json"
    with zipfile.ZipFile(wheel) as archive:
        manifest_bytes = archive.read(manifest_name)
        manifest = json.loads(manifest_bytes)
        assert manifest_bytes == canonical_json_bytes(manifest)
        entries = manifest["artifacts"]
        expected = {f"graphify/{entry['path']}" for entry in entries}
        assert manifest_name in names
        assert expected <= names
        actual_data = {
            name
            for name in names
            if name.startswith("graphify/workspace/semantic_release_data/")
            and not name.endswith("/")
        }
        assert actual_data == {
            name for name in expected if "/semantic_release_data/" in name
        }
        for entry in entries:
            name = f"graphify/{entry['path']}"
            raw = archive.read(name)
            mode = stat.S_IMODE(archive.getinfo(name).external_attr >> 16)
            assert len(raw) == entry["byte_count"]
            assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
            assert f"{mode:04o}" == entry["mode"] == "0644"


def test_semantic_release_loader_and_classifier_run_from_isolated_wheel_install(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site_packages)
    script = "\n".join(
        [
            "import json, pathlib, sys",
            f"sys.path.insert(0, {str(site_packages)!r})",
            "import graphify.workspace.semantic_release as semantic_release",
            "bundle = semantic_release.load_installed_semantic_release_bundle()",
            "result = semantic_release.classify_canonical_bytes(",
            "    b'ghp_abcdefghijklmnopqrstuvwxyz0123456789',",
            "    (semantic_release.CORE_SECRETS_PROFILE,),",
            ")",
            "assert pathlib.Path(semantic_release.__file__).is_relative_to(pathlib.Path(sys.path[0]))",
            "print(json.dumps({",
            "    'artifact_count': len(bundle.artifacts),",
            "    'manifest_sha256': bundle.manifest_sha256,",
            "    'result': result.to_dict(),",
            "}, sort_keys=True))",
        ]
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-P", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    evidence = json.loads(proc.stdout)
    assert evidence["artifact_count"] == 7
    assert evidence["result"] == {
        "category_ids": ["secret.provider_credential"],
        "outcome": "MATCH",
        "rule_ids": ["core.provider.github_token.v1"],
    }
    assert len(evidence["manifest_sha256"]) == 64
