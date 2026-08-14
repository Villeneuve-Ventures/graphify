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
PUBLIC_ENTRY_POINTS = {
    "graphify": "graphify.__main__:main",
    "graphify-mcp": "graphify.serve:_main",
}
AUTHORITY_SCRIPTS = {
    "_graphify-semantic-authority": 'importlib.import_module("graphify.__main__")',
    "_graphify-mcp-semantic-authority": 'importlib.import_module("graphify.serve")',
}


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


def _create_venv(root: Path, name: str, *, with_pip: bool = True) -> tuple[Path, Path]:
    venv_dir = root / name
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
        venv.EnvBuilder(with_pip=with_pip).create(venv_dir)
    return _venv_paths(venv_dir)


def _install_wheel_without_dependencies(
    wheel: Path,
    root: Path,
    *,
    link_mode: str = "copy",
    install_umask: int = 0o022,
) -> tuple[Path, Path, Path]:
    python, scripts = _create_venv(root, "venv")
    uv = shutil.which("uv")
    if uv is None and link_mode != "copy":
        pytest.skip("uv is required to exercise non-copy wheel installation")
    command = (
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--link-mode",
            link_mode,
            str(wheel),
        ]
        if uv
        else [str(python), "-m", "pip", "install", "--no-deps", str(wheel)]
    )
    environment = _clean_environment()
    if uv:
        environment["UV_CACHE_DIR"] = str(root / "uv-cache")
    proc = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        preexec_fn=(lambda: os.umask(install_umask)) if os.name != "nt" else None,
    )
    assert proc.returncode == 0, proc.stderr
    authority_script = scripts / "_graphify-semantic-authority"
    assert authority_script.exists()
    return python, scripts, authority_script


def _installed_bundle_paths(python: Path, root: Path) -> list[Path]:
    module_source, _ = _module_paths(python, "graphify.workspace.semantic_release", root)
    package_root = module_source.parents[1]
    manifest_path = package_root / "workspace" / "semantic_release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        manifest_path,
        *(package_root / entry["path"] for entry in manifest["artifacts"]),
    ]


def _installed_classification_outcome(python: Path, root: Path) -> str:
    script = (
        "from graphify.workspace.semantic_release import ("
        "CORE_SECRETS_PROFILE, classify_canonical_bytes)\n"
        "result = classify_canonical_bytes(b'password: hunter2', (CORE_SECRETS_PROFILE,))\n"
        "print(result.outcome)\n"
    )
    proc = subprocess.run(
        [str(python), "-I", "-c", script],
        cwd=root,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _uv_tool_install(
    wheel: Path,
    root: Path,
    *,
    link_mode: str,
    reinstall: bool = False,
    install_umask: int = 0o022,
) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to exercise the authority tool-install route")
    tool_dir = root / "tools"
    bin_dir = root / "bin"
    cache_dir = root / "cache"
    environment = _clean_environment()
    environment.update(
        {
            "UV_CACHE_DIR": str(cache_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "UV_TOOL_DIR": str(tool_dir),
        }
    )
    command = [
        uv,
        "tool",
        "install",
        "--from",
        str(wheel),
        "--link-mode",
        link_mode,
    ]
    if reinstall:
        command.extend(["--force", "--reinstall"])
    command.append("graphifyy")
    proc = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        preexec_fn=(lambda: os.umask(install_umask)) if os.name != "nt" else None,
    )
    assert proc.returncode == 0, proc.stderr
    python = tool_dir / "graphifyy" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert python.exists()
    return python


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
    python, scripts = _create_venv(root, "python-env", with_pip=False)
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
    source = out / "source"
    shutil.copytree(
        REPO,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".hypothesis",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "*.pyc",
            "build",
            "dist",
            "graphify-out",
            "graphifyy.egg-info",
            "node_modules",
        ),
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out),
            str(source),
        ],
        capture_output=True,
        text=True,
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


def test_wheel_separates_public_entry_points_from_private_authority_scripts(
    wheel_build: tuple[set[str], str, str, Path],
) -> None:
    names, _, _, wheel = wheel_build
    entry_point_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
    assert len(entry_point_names) == 1
    with zipfile.ZipFile(wheel) as archive:
        entry_points = archive.read(entry_point_names[0]).decode("utf-8")
        assert entry_points == (
            "[console_scripts]\n"
            "graphify = graphify.__main__:main\n"
            "graphify-mcp = graphify.serve:_main\n"
        )
        for public_name in PUBLIC_ENTRY_POINTS:
            assert not any(name.endswith(f".data/scripts/{public_name}") for name in names)
        for script_name, target_import in AUTHORITY_SCRIPTS.items():
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


def test_installed_wheel_provides_public_console_commands(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    _, scripts, _ = _install_wheel_without_dependencies(wheel, tmp_path)
    suffix = ".exe" if os.name == "nt" else ""
    public_scripts = {name: scripts / f"{name}{suffix}" for name in PUBLIC_ENTRY_POINTS}
    assert all(path.exists() for path in public_scripts.values())

    proc = subprocess.run(
        [str(public_scripts["graphify"]), "--version"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "graphify 0.9.16+workspace.1\n"


def test_authority_copy_install_satisfies_frozen_file_invariants(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, _, _ = _install_wheel_without_dependencies(wheel, tmp_path)

    for path in _installed_bundle_paths(python, tmp_path):
        details = path.stat(follow_symlinks=False)
        assert stat.S_ISREG(details.st_mode)
        assert details.st_nlink == 1
        assert stat.S_IMODE(details.st_mode) == 0o644


def test_documented_authority_reinstall_requalifies_hardlink_tool_install(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    root = tmp_path.resolve()
    python = _uv_tool_install(wheel, root, link_mode="hardlink")
    hardlinked_paths = _installed_bundle_paths(python, root)
    assert any(path.stat(follow_symlinks=False).st_nlink > 1 for path in hardlinked_paths)
    assert _installed_classification_outcome(python, root) == "INDETERMINATE"

    python = _uv_tool_install(
        wheel,
        root,
        link_mode="copy",
        reinstall=True,
        install_umask=0o077,
    )
    for path in _installed_bundle_paths(python, root):
        details = path.stat(follow_symlinks=False)
        assert stat.S_ISREG(details.st_mode)
        assert details.st_nlink == 1
        assert stat.S_IMODE(details.st_mode) == 0o644
    assert _installed_classification_outcome(python, root) == "MATCH"


@pytest.mark.parametrize(
    ("link_mode", "install_umask", "expected_failure"),
    [
        ("hardlink", 0o022, "single-link"),
        ("copy", 0o077, "file mode"),
    ],
)
def test_non_authority_installs_fail_closed(
    link_mode: str,
    install_umask: int,
    expected_failure: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, _, _ = _install_wheel_without_dependencies(
        wheel,
        tmp_path,
        link_mode=link_mode,
        install_umask=install_umask,
    )
    paths = _installed_bundle_paths(python, tmp_path)
    if link_mode == "hardlink":
        assert any(path.stat(follow_symlinks=False).st_nlink > 1 for path in paths)
    else:
        assert any(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)

    assert _installed_classification_outcome(python, tmp_path) == "INDETERMINATE"

    module_source, _ = _module_paths(python, "graphify.workspace.semantic_release", tmp_path)
    loader_script = (
        "from graphify.workspace.semantic_release import load_installed_semantic_release_bundle\n"
        "load_installed_semantic_release_bundle()\n"
    )
    failed = subprocess.run(
        [str(python), "-I", "-c", loader_script],
        cwd=module_source.parent,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert expected_failure in failed.stderr


@pytest.mark.parametrize(
    ("script_name", "args", "must_succeed"),
    [
        ("_graphify-semantic-authority", ["--version"], True),
        ("_graphify-mcp-semantic-authority", ["--help"], False),
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
        ("_graphify-semantic-authority", ["--version"], True),
        ("_graphify-mcp-semantic-authority", ["--help"], False),
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


@pytest.mark.parametrize(
    "script_name", ["_graphify-semantic-authority", "_graphify-mcp-semantic-authority"]
)
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


@pytest.mark.parametrize(
    "script_name", ["_graphify-semantic-authority", "_graphify-mcp-semantic-authority"]
)
def test_installed_bootstrap_rejects_unsupported_sibling_python(
    script_name: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, _ = _install_wheel_as_user_script_layout(wheel, tmp_path, script_name)
    sibling_python = script_path.parent / "python"
    sibling_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    sibling_python.chmod(0o755)

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=_clean_environment(),
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
    script_path, _ = _install_wheel_as_user_script_layout(
        wheel, tmp_path, "_graphify-semantic-authority"
    )
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
        "_graphify-semantic-authority",
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


def test_installed_bootstrap_rejects_relative_editable_direct_url_roots(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, site_packages = _install_wheel_as_user_script_layout(
        wheel,
        tmp_path,
        "_graphify-semantic-authority",
    )
    relative_source = tmp_path / "relative-source"
    hostile_package = relative_source / "graphify"
    hostile_package.mkdir(parents=True)
    sentinel = tmp_path / "relative-editable-source-executed"
    (hostile_package / "__init__.py").write_text("", encoding="utf-8")
    (hostile_package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        "def main():\n"
        "    print('relative editable executed')\n",
        encoding="utf-8",
    )
    dist_info = site_packages / "graphifyy-0.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(
        json.dumps({"dir_info": {"editable": True}, "url": "file:relative-source"}),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("graphify ")
    assert "relative editable executed" not in proc.stdout
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("script_name", "hostile_module"),
    [
        ("_graphify-semantic-authority", "__main__.py"),
        ("_graphify-mcp-semantic-authority", "serve.py"),
    ],
)
def test_installed_bootstrap_rejects_ambiguous_script_prefix_package_owners(
    script_name: str,
    hostile_module: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, site_packages = _install_wheel_as_user_script_layout(
        wheel,
        tmp_path,
        script_name,
    )
    stale_source = tmp_path / "stale-editable-source"
    hostile_package = stale_source / "graphify"
    hostile_package.mkdir(parents=True)
    sentinel = tmp_path / f"{script_name}-stale-editable-source-executed"
    (hostile_package / "__init__.py").write_text("", encoding="utf-8")
    entrypoint = "main" if hostile_module == "__main__.py" else "_main"
    (hostile_package / hostile_module).write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        f"def {entrypoint}():\n"
        "    print('stale editable executed')\n",
        encoding="utf-8",
    )
    dist_info = site_packages / "graphifyy-0.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(
        json.dumps({"dir_info": {"editable": True}, "url": stale_source.as_uri()}),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "ambiguous script-prefix package roots" in proc.stderr
    assert "stale editable executed" not in proc.stdout
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("script_name", "hostile_module"),
    [
        ("_graphify-semantic-authority", "__main__.py"),
        ("_graphify-mcp-semantic-authority", "serve.py"),
    ],
)
def test_installed_bootstrap_rejects_multiple_editable_direct_url_owners(
    script_name: str,
    hostile_module: str,
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    script_path, site_packages = _install_wheel_as_user_script_layout(
        wheel,
        tmp_path,
        script_name,
        include_package=False,
    )
    entrypoint = "main" if hostile_module == "__main__.py" else "_main"
    sentinels: list[Path] = []
    for index in range(2):
        source = tmp_path / f"editable-source-{index}"
        hostile_package = source / "graphify"
        hostile_package.mkdir(parents=True)
        sentinel = tmp_path / f"{script_name}-editable-source-{index}-executed"
        sentinels.append(sentinel)
        (hostile_package / "__init__.py").write_text("", encoding="utf-8")
        (hostile_package / hostile_module).write_text(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
            f"def {entrypoint}():\n"
            "    print('editable owner executed')\n",
            encoding="utf-8",
        )
        dist_info = site_packages / f"graphifyy-{index}.dist-info"
        dist_info.mkdir()
        (dist_info / "direct_url.json").write_text(
            json.dumps({"dir_info": {"editable": True}, "url": source.as_uri()}),
            encoding="utf-8",
        )

    proc = subprocess.run(
        [str(script_path), "--version"],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "ambiguous script-prefix package roots" in proc.stderr
    assert "editable owner executed" not in proc.stdout
    assert all(not sentinel.exists() for sentinel in sentinels)


@pytest.mark.parametrize(
    ("script_name", "target_module", "target_entrypoint"),
    [
        ("_graphify-semantic-authority", "graphify.__main__", "main"),
        ("_graphify-mcp-semantic-authority", "graphify.serve", "_main"),
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
        ("_graphify-semantic-authority", "graphify.__main__", "main"),
        ("_graphify-mcp-semantic-authority", "graphify.serve", "_main"),
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
                if any(name.endswith(f".data/scripts/{script}") for script in AUTHORITY_SCRIPTS)
            },
        }
        assert len(protected) == 3
        for name in protected:
            raw = archive.read(name)
            digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
            assert rows[name] == (f"sha256={digest.decode('ascii')}", str(len(raw)))


def test_installed_authority_script_ignores_package_local_bytecode_cache(
    wheel_build: tuple[set[str], str, str, Path],
    tmp_path: Path,
) -> None:
    _, _, _, wheel = wheel_build
    python, _, authority_script = _install_wheel_without_dependencies(wheel, tmp_path)
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
        [str(authority_script), "--version"],
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
        [str(authority_script), "--version"],
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
