"""Packaging guard (#1121 follow-up): the 5 skillgen guards check the *repo tree*,
not the *built wheel*. A host whose references bundle or always-on block fails to
match the `package-data` globs would pass `--check`/`--audit-coverage` yet make
`graphify install` hard-exit with "not found in package" for real users.

This builds the wheel once and asserts every committed skill artifact ships in it.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import stat
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

from graphify.workspace import WORKSPACE_SCHEMA_FILES
from graphify.workspace.contracts import canonical_json_bytes

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "graphify"


def _has_build() -> bool:
    return importlib.util.find_spec("build") is not None


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return environment


def _venv_paths(root: Path) -> tuple[Path, Path]:
    scripts = root / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    return python, scripts


def _install_wheel_without_dependencies(
    wheel: Path,
    root: Path,
) -> tuple[Path, Path, Path]:
    venv_dir = root / "venv"
    uv = shutil.which("uv")
    if uv:
        proc = subprocess.run(
            [uv, "venv", "--python", sys.executable, str(venv_dir)],
            cwd=root,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
    else:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    python, scripts = _venv_paths(venv_dir)
    command = (
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--link-mode",
            "copy",
            str(wheel),
        ]
        if uv
        else [str(python), "-m", "pip", "install", "--no-deps", str(wheel)]
    )
    proc = subprocess.run(
        command,
        cwd=root,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    graphify_script = scripts / ("graphify.exe" if os.name == "nt" else "graphify")
    if not graphify_script.exists():
        graphify_script = scripts / "graphify"
    assert graphify_script.exists()
    return python, scripts, graphify_script


def _module_paths(
    python: Path,
    module_name: str,
    cwd: Path,
) -> tuple[Path, Path]:
    script = (
        "import importlib, json\n"
        f"module = importlib.import_module({module_name!r})\n"
        "print(json.dumps({'file': module.__file__, 'cached': module.__cached__}, sort_keys=True))\n"
    )
    proc = subprocess.run(
        [str(python), "-B", "-E", "-P", "-c", script],
        cwd=cwd,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    evidence = json.loads(proc.stdout)
    return Path(evidence["file"]), Path(evidence["cached"])


def _hostile_source(length: int, sentinel: Path, *, function_name: str = "main") -> bytes:
    payload = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        f"def {function_name}():\n"
        "    print('hostile bytecode executed')\n"
    ).encode("utf-8")
    assert len(payload) < length
    return payload + b"#" * (length - len(payload))


def _compile_hostile_timestamp_pyc(
    source: Path,
    cache: Path,
    hostile: bytes,
) -> tuple[bytes, os.stat_result]:
    genuine = source.read_bytes()
    assert len(hostile) == len(genuine)
    source_stat = source.stat()
    try:
        source.write_bytes(hostile)
        source.chmod(stat.S_IMODE(source_stat.st_mode))
        os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        py_compile.compile(
            str(source),
            cfile=str(cache),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
        )
    finally:
        source.write_bytes(genuine)
        source.chmod(stat.S_IMODE(source_stat.st_mode))
        os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    return genuine, source_stat


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


def test_wheel_ships_pre_import_bootstrap_scripts(
    wheel_build: tuple[set[str], str, str, Path],
) -> None:
    names, _, _, wheel = wheel_build
    assert not any(name.endswith(".dist-info/entry_points.txt") for name in names)
    scripts = {
        "graphify": 'importlib.import_module("graphify.__main__")',
        "graphify-mcp": 'importlib.import_module("graphify.serve")',
    }
    with zipfile.ZipFile(wheel) as archive:
        for script_name, target_import in scripts.items():
            matches = [
                name
                for name in names
                if name.endswith(f".data/scripts/{script_name}")
            ]
            assert len(matches) == 1
            raw = archive.read(matches[0]).decode("utf-8")
            assert raw.startswith("#!python\n")
            assert "sys.flags.dont_write_bytecode" in raw
            assert "sys.flags.ignore_environment" in raw
            assert "sys.flags.no_user_site" in raw
            assert "sys.flags.safe_path" in raw
            assert "os.execv(" in raw
            assert '"-B",\n            "-E",\n            "-P",\n            "-s"' in raw
            assert "getusersitepackages" not in raw
            assert "_GRAPHIFY_BOOTSTRAP_ISOLATED" not in raw
            assert "sys.pycache_prefix = prefix" in raw
            assert raw.index("sys.pycache_prefix = prefix") < raw.index(target_import)


def test_wheel_record_binds_bootstrap_and_classifier_source(
    wheel_build: tuple[set[str], str, str, Path],
) -> None:
    names, _, _, wheel = wheel_build
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    assert len(record_names) == 1
    with zipfile.ZipFile(wheel) as archive:
        rows = {
            path: (digest, byte_count)
            for path, digest, byte_count in csv.reader(
                archive.read(record_names[0]).decode("utf-8").splitlines()
            )
        }
        protected = {
            "graphify/workspace/semantic_release.py",
            *{
                name
                for name in names
                if name.endswith(".data/scripts/graphify")
                or name.endswith(".data/scripts/graphify-mcp")
            },
        }
        assert len(protected) == 3
        for name in protected:
            raw = archive.read(name)
            digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
            assert rows[name] == (f"sha256={digest.decode('ascii')}", str(len(raw)))


def test_installed_graphify_script_ignores_package_local_bytecode_cache(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, _, graphify_script = _install_wheel_without_dependencies(wheel, tmp_path)
    module_source, module_cache = _module_paths(python, "graphify.__main__", tmp_path)
    sentinel = tmp_path / "hostile-main-executed"
    genuine, source_stat = _compile_hostile_timestamp_pyc(
        module_source,
        module_cache,
        _hostile_source(module_source.stat().st_size, sentinel),
    )
    direct = subprocess.run(
        [str(python), "-B", "-E", "-P", "-c", "import graphify.__main__"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr
    assert sentinel.read_text(encoding="utf-8") == "executed"
    sentinel.unlink()

    proc = subprocess.run(
        [str(graphify_script), "--version"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "graphify 0.9.16+workspace.1\n"
    assert not sentinel.exists()
    assert module_source.read_bytes() == genuine
    restored = module_source.stat()
    assert restored.st_size == source_stat.st_size
    assert restored.st_mtime_ns == source_stat.st_mtime_ns

    hostile_path = tmp_path / "hostile-pythonpath"
    hostile_package = hostile_path / "graphify"
    hostile_package.mkdir(parents=True)
    hostile_pythonpath_sentinel = tmp_path / "hostile-pythonpath-executed"
    hostile_stdlib_sentinel = tmp_path / "hostile-stdlib-executed"
    (hostile_package / "__init__.py").write_text("", encoding="utf-8")
    (hostile_package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(hostile_pythonpath_sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        "def main():\n"
        "    print('hostile pythonpath executed')\n",
        encoding="utf-8",
    )
    (hostile_path / "shutil.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(hostile_stdlib_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    hostile_env = _clean_environment()
    hostile_env["PYTHONPATH"] = str(hostile_path)
    hostile_env["_GRAPHIFY_BOOTSTRAP_ISOLATED"] = "1"
    proc = subprocess.run(
        [str(graphify_script), "--version"],
        cwd=tmp_path,
        env=hostile_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "graphify 0.9.16+workspace.1\n"
    assert not hostile_pythonpath_sentinel.exists()
    assert not hostile_stdlib_sentinel.exists()


def test_semantic_release_classifier_ignores_package_local_cache_with_bootstrap_prefix(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, _, _ = _install_wheel_without_dependencies(wheel, tmp_path)
    module_source, module_cache = _module_paths(
        python,
        "graphify.workspace.semantic_release",
        tmp_path,
    )
    sentinel = tmp_path / "hostile-semantic-release-executed"
    genuine, source_stat = _compile_hostile_timestamp_pyc(
        module_source,
        module_cache,
        _hostile_source(module_source.stat().st_size, sentinel),
    )
    direct = subprocess.run(
        [
            str(python),
            "-B",
            "-E",
            "-P",
            "-c",
            "import graphify.workspace.semantic_release",
        ],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr
    assert sentinel.read_text(encoding="utf-8") == "executed"
    sentinel.unlink()

    script = "\n".join(
        [
            "import json, shutil, sys, tempfile",
            "prefix = tempfile.mkdtemp(prefix='graphify-pycache-')",
            "sys.pycache_prefix = prefix",
            "sys.dont_write_bytecode = True",
            "try:",
            "    import graphify.workspace.semantic_release as semantic_release",
            "    result = semantic_release.classify_canonical_bytes(",
            "        b'ghp_abcdefghijklmnopqrstuvwxyz0123456789',",
            "        (semantic_release.CORE_SECRETS_PROFILE,),",
            "    )",
            "    print(json.dumps({",
            "        'cached': semantic_release.__cached__,",
            "        'file': semantic_release.__file__,",
            "        'prefix': prefix,",
            "        'result': result.to_dict(),",
            "    }, sort_keys=True))",
            "finally:",
            "    shutil.rmtree(prefix)",
        ]
    )
    proc = subprocess.run(
        [str(python), "-B", "-E", "-P", "-c", script],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    evidence = json.loads(proc.stdout)
    assert evidence["result"] == {
        "category_ids": ["secret.provider_credential"],
        "outcome": "MATCH",
        "rule_ids": ["core.provider.github_token.v1"],
    }
    assert Path(evidence["cached"]).is_relative_to(Path(evidence["prefix"]))
    assert Path(evidence["file"]) == module_source
    assert not sentinel.exists()
    assert module_source.read_bytes() == genuine
    restored = module_source.stat()
    assert restored.st_size == source_stat.st_size
    assert restored.st_mtime_ns == source_stat.st_mtime_ns


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
