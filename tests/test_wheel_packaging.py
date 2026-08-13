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


def _probe_installed_bootstrap(
    python: Path,
    script_path: Path,
    *,
    target_module: str,
    target_entrypoint: str,
    target_error: bool = False,
    target_result: int | None = None,
    cleanup_error: bool = False,
) -> dict[str, object]:
    target_failure = ["    raise RuntimeError('target failed')"] if target_error else []
    target_return = [f"    return {target_result}"] if target_result is not None else []
    cleanup_failure = ["    raise OSError('cleanup failed')"] if cleanup_error else []
    script = "\n".join(
        [
            "import json, runpy, sys, types",
            f"namespace = runpy.run_path({str(script_path)!r}, run_name='bootstrap_probe')",
            "entrypoint = namespace['_main']",
            "scope = entrypoint.__globals__",
            "events = []",
            "prefix = 'probe-prefix'",
            "scope['_prepare_import_boundary'] = lambda: prefix",
            "sys.pycache_prefix = prefix",
            "def target():",
            f"    events.append(['call', {target_entrypoint!r}])",
            *target_failure,
            *target_return,
            "def import_module(name):",
            "    events.append(['import', name])",
            f"    assert name == {target_module!r}",
            f"    return types.SimpleNamespace(**{{{target_entrypoint!r}: target}})",
            "scope['importlib'] = types.SimpleNamespace(import_module=import_module)",
            "def rmtree(value):",
            "    events.append(['rmtree', value])",
            *cleanup_failure,
            "scope['shutil'] = types.SimpleNamespace(rmtree=rmtree)",
            "caught = None",
            "try:",
            "    entrypoint()",
            "except BaseException as exc:",
            "    caught = {",
            "        'message': str(exc),",
            "        'notes': getattr(exc, '__notes__', []),",
            "        'type': type(exc).__name__,",
            "    }",
            "print(json.dumps({",
            "    'caught': caught,",
            "    'events': events,",
            "    'pycache_prefix': sys.pycache_prefix,",
            "}, sort_keys=True))",
        ]
    )
    proc = subprocess.run(
        [str(python), "-B", "-E", "-P", "-s", "-S", "-c", script],
        cwd=script_path.parent,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    evidence = json.loads(proc.stdout)
    assert isinstance(evidence, dict)
    return evidence


def _install_wheel_as_user_script_layout(
    wheel: Path,
    root: Path,
    script_name: str,
    *,
    include_package: bool = True,
) -> tuple[Path, Path]:
    userbase = root / f"{script_name}-userbase"
    scripts = userbase / "bin"
    site_packages = (
        userbase
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    scripts.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    script_bytes: bytes | None = None
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if include_package and name.startswith("graphify/") and not name.endswith("/"):
                target = site_packages / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
            if name.endswith(f".data/scripts/{script_name}"):
                script_bytes = archive.read(name)
    assert script_bytes is not None
    script_path = scripts / script_name
    script_path.write_bytes(script_bytes)
    script_path.chmod(0o755)
    assert not (scripts / "python").exists()
    return script_path, site_packages


def _copy_wheel_package_to_source_root(wheel: Path, source_root: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("graphify/") and not name.endswith("/"):
                target = source_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))


def _create_python_env(root: Path) -> tuple[Path, Path, Path]:
    venv_dir = root / "python-env"
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
        venv.EnvBuilder(with_pip=False).create(venv_dir)
    python, scripts = _venv_paths(venv_dir)
    site_packages = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        cwd=root,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    python314 = scripts / "python3.14"
    if not python314.exists():
        python314.symlink_to(python.name)
    return python, scripts, Path(site_packages)


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
            assert raw.startswith("#!/bin/sh\n")
            assert 'exec "$_GRAPHIFY_PYTHON" -BEPsS "$_GRAPHIFY_SCRIPT" "$@"' in raw
            assert raw.index('exec "$_GRAPHIFY_PYTHON" -BEPsS') < raw.index(
                "from __future__ import annotations"
            )
            assert "sys.flags.dont_write_bytecode" in raw
            assert "sys.flags.ignore_environment" in raw
            assert "sys.flags.no_site" in raw
            assert "sys.flags.no_user_site" in raw
            assert "sys.flags.safe_path" in raw
            assert "os.execv(" in raw
            assert '"-B",\n            "-E",\n            "-P",\n            "-s",\n            "-S"' in raw
            assert "getusersitepackages" not in raw
            assert "_GRAPHIFY_BOOTSTRAP_ISOLATED" not in raw
            assert "sys.pycache_prefix = prefix" in raw
            assert raw.index("sys.pycache_prefix = prefix") < raw.index(target_import)


@pytest.mark.parametrize(
    ("script_name", "args", "must_succeed"),
    [
        ("graphify", ["--version"], True),
        ("graphify-mcp", ["--help"], False),
    ],
)
def test_installed_bootstrap_blocks_pythonpath_sitecustomize_before_startup(
    script_name: str,
    args: list[str],
    must_succeed: bool,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, scripts, _ = _install_wheel_without_dependencies(wheel, tmp_path)
    script_path = scripts / script_name
    raw = script_path.read_text(encoding="utf-8")
    assert raw.startswith("#!/bin/sh\n")
    assert 'exec "$_GRAPHIFY_PYTHON" -BEPsS "$_GRAPHIFY_SCRIPT" "$@"' in raw

    hostile_path = tmp_path / "hostile-pythonpath"
    hostile_path.mkdir()
    sentinel = tmp_path / f"{script_name}-sitecustomize-executed"
    (hostile_path / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    site_packages = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    site_sentinel = tmp_path / f"{script_name}-site-startup-executed"
    Path(site_packages, "graphify_hostile.pth").write_text(
        "import pathlib; "
        f"pathlib.Path({str(site_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    Path(site_packages, "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(site_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    hostile_env = _clean_environment()
    hostile_env["PYTHONPATH"] = str(hostile_path)
    proc = subprocess.run(
        [str(script_path), *args],
        cwd=tmp_path,
        env=hostile_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert not sentinel.exists(), proc.stderr
    assert not site_sentinel.exists(), proc.stderr
    assert proc.returncode != 126, proc.stderr
    if must_succeed:
        assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    ("script_name", "args", "must_succeed"),
    [
        ("graphify", ["--version"], True),
        ("graphify-mcp", ["--help"], False),
    ],
)
def test_installed_bootstrap_runs_from_plain_user_script_layout_without_startup_hooks(
    script_name: str,
    args: list[str],
    must_succeed: bool,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, site_packages = _install_wheel_as_user_script_layout(
        wheel,
        tmp_path,
        script_name,
    )
    raw = script_path.read_text(encoding="utf-8")
    assert raw.startswith("#!/bin/sh\n")
    assert 'exec "$_GRAPHIFY_PYTHON" -BEPsS "$_GRAPHIFY_SCRIPT" "$@"' in raw

    sentinel = tmp_path / f"{script_name}-user-site-startup-executed"
    (site_packages / "graphify_hostile.pth").write_text(
        "import pathlib; "
        f"pathlib.Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (site_packages / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [str(script_path), *args],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert not sentinel.exists(), proc.stderr
    assert proc.returncode != 126, proc.stderr
    if must_succeed:
        assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script_name", ["graphify", "graphify-mcp"])
def test_installed_bootstrap_rejects_unsupported_generic_python3_fallback(
    script_name: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, _ = _install_wheel_as_user_script_layout(wheel, tmp_path, script_name)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_python3.chmod(0o755)
    environment = _clean_environment()
    environment["PATH"] = str(fake_bin)

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 126
    assert "Python >= 3.14" in proc.stderr


def test_installed_bootstrap_prefers_script_prefix_package_before_interpreter_sites(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, _ = _install_wheel_as_user_script_layout(wheel, tmp_path, "graphify")
    _, foreign_scripts, foreign_site = _create_python_env(tmp_path)
    sentinel = tmp_path / "foreign-graphify-executed"
    foreign_package = foreign_site / "graphify"
    foreign_package.mkdir()
    (foreign_package / "__init__.py").write_text("", encoding="utf-8")
    (foreign_package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        "def main():\n"
        "    print('foreign graphify executed')\n",
        encoding="utf-8",
    )
    environment = _clean_environment()
    environment["PATH"] = str(foreign_scripts)

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("graphify ")
    assert "foreign graphify" not in proc.stdout
    assert not sentinel.exists()


def test_installed_bootstrap_preserves_editable_direct_url_roots_without_startup_hooks(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, site_packages = _install_wheel_as_user_script_layout(
        wheel,
        tmp_path,
        "graphify",
        include_package=False,
    )
    source_root = tmp_path / "editable-source"
    _copy_wheel_package_to_source_root(wheel, source_root)
    dist_info = site_packages / "graphifyy-0.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(
        json.dumps({"dir_info": {"editable": True}, "url": source_root.as_uri()}),
        encoding="utf-8",
    )
    startup_sentinel = tmp_path / "editable-site-startup-executed"
    (site_packages / "graphify_hostile.pth").write_text(
        "import pathlib; "
        f"pathlib.Path({str(startup_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (site_packages / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(startup_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _, scripts, _ = _create_python_env(tmp_path)
    environment = _clean_environment()
    environment["PATH"] = str(scripts)

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("graphify ")
    assert not startup_sentinel.exists()


@pytest.mark.parametrize(
    ("script_name", "target_module", "target_entrypoint"),
    [
        ("graphify", "graphify.__main__", "main"),
        ("graphify-mcp", "graphify.serve", "_main"),
    ],
)
def test_installed_bootstrap_dispatches_the_expected_target(
    script_name: str,
    target_module: str,
    target_entrypoint: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, scripts, _ = _install_wheel_without_dependencies(wheel, tmp_path)
    evidence = _probe_installed_bootstrap(
        python,
        scripts / script_name,
        target_module=target_module,
        target_entrypoint=target_entrypoint,
    )

    assert evidence == {
        "caught": None,
        "events": [
            ["import", target_module],
            ["call", target_entrypoint],
            ["rmtree", "probe-prefix"],
        ],
        "pycache_prefix": None,
    }


@pytest.mark.parametrize(
    ("script_name", "target_module", "target_entrypoint"),
    [
        ("graphify", "graphify.__main__", "main"),
        ("graphify-mcp", "graphify.serve", "_main"),
    ],
)
@pytest.mark.parametrize("target_outcome", ["success", "exception", "exit"])
def test_installed_bootstrap_cleanup_preserves_the_primary_outcome(
    script_name: str,
    target_module: str,
    target_entrypoint: str,
    target_outcome: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, scripts, _ = _install_wheel_without_dependencies(wheel, tmp_path)
    evidence = _probe_installed_bootstrap(
        python,
        scripts / script_name,
        target_module=target_module,
        target_entrypoint=target_entrypoint,
        target_error=target_outcome == "exception",
        target_result=7 if target_outcome == "exit" else None,
        cleanup_error=True,
    )

    assert evidence["pycache_prefix"] is None
    caught = evidence["caught"]
    assert isinstance(caught, dict)
    if target_outcome == "exception":
        assert caught == {
            "message": "target failed",
            "notes": ["private bytecode cache cleanup failed: cleanup failed"],
            "type": "RuntimeError",
        }
    elif target_outcome == "exit":
        assert caught == {
            "message": "7",
            "notes": ["private bytecode cache cleanup failed: cleanup failed"],
            "type": "SystemExit",
        }
    else:
        assert caught == {
            "message": "cleanup failed",
            "notes": [],
            "type": "OSError",
        }


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
