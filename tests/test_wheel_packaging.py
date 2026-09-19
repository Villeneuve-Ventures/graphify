"""Packaging guard (#1121 follow-up): the 5 skillgen guards check the *repo tree*,
not the *built wheel*. A host whose references bundle or always-on block fails to
match the `package-data` globs would pass `--check`/`--audit-coverage` yet make
`graphify install` hard-exit with "not found in package" for real users.

This builds the wheel once and asserts every committed skill artifact ships in it.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "graphify"


def _has_build() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-m", "build", "--version"],
            check=True, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


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
def built_wheel(tmp_path_factory) -> Path:
    if not _has_build():
        pytest.skip("`python -m build` unavailable (dev extra not installed)")
    out = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(out), str(REPO)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"wheel build failed in this env:\n{proc.stderr[-800:]}")
    wheels = list(out.glob("graphifyy-*.whl"))
    assert wheels, "no wheel produced"
    return max(wheels, key=lambda p: p.stat().st_mtime)


@pytest.fixture(scope="module")
def wheel_namelist(built_wheel) -> set[str]:
    with zipfile.ZipFile(built_wheel) as z:
        return set(z.namelist())


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


def test_complete_structural_package_members(built_wheel):
    from tools.workspace_artifacts.candidate import package_members
    import hashlib
    with zipfile.ZipFile(built_wheel) as archive:
        actual = {name: hashlib.sha256(archive.read(name)).hexdigest()
                  for name in archive.namelist() if name.startswith("graphify/")}
    assert actual == package_members(REPO)


def test_local_fixture_identity_reproducibility_and_tamper(built_wheel, tmp_path):
    from graphify.workspace.composition import StructuralPolicy, WorkspaceRuntimeAuthority
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts.candidate import build_fixture
    policy = StructuralPolicy(8, 16384, 1, 4, 1048576)
    a, b = tmp_path / "first", tmp_path / "second"
    first = build_fixture(repo_root=REPO, wheel=built_wheel, output_root=a, policy=policy)
    second = build_fixture(repo_root=REPO, wheel=built_wheel, output_root=b, policy=policy)
    assert first == second
    assert {p.name:p.read_bytes() for p in a.iterdir()} == {p.name:p.read_bytes() for p in b.iterdir()}
    assert first.to_dict()["distribution_build"].startswith("fixture:sha256:")
    assert first.to_dict()["certified"] is False
    assert WorkspaceRuntimeAuthority.from_json((a / "runtime-manifest.json").read_bytes()).compatibility == first
    with zipfile.ZipFile(a / "fixture-bundle.zip") as archive:
        assert "schemas/input-manifest.schema.json" in archive.namelist()
        assert "tests/test_workspace_contracts.py" in archive.namelist()
    # A changed engine file in a same-version wheel must refuse before output.
    corrupt = tmp_path / built_wheel.name
    with zipfile.ZipFile(built_wheel) as source, zipfile.ZipFile(corrupt, "w") as destination:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "graphify/source_io.py": data += b"\n# changed\n"
            destination.writestr(info, data)
    target = tmp_path / "refused"
    with pytest.raises(ContractError, match="complete candidate package"):
        build_fixture(repo_root=REPO, wheel=corrupt, output_root=target, policy=policy)
    assert not target.exists()
    with pytest.raises(ContractError, match="new and outside"):
        build_fixture(repo_root=REPO, wheel=built_wheel, output_root=a, policy=policy)


def test_disposable_noneditable_candidate_install_and_authority(built_wheel, tmp_path):
    import os
    import shutil
    from graphify.workspace.composition import StructuralPolicy
    from tools.workspace_artifacts.candidate import build_fixture
    uv = shutil.which("uv")
    assert uv, "required disposable installation smoke needs uv"
    tmp_path = tmp_path.resolve()
    home, state_home, codex = (tmp_path / n for n in ("home", "state", "codex"))
    for p in (home, state_home, codex): p.mkdir(mode=0o700)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_STATE_HOME": str(state_home),
           "CODEX_HOME": str(codex), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], env=env, check=True, capture_output=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run([uv, "pip", "install", "--no-deps", "--python", str(python), str(built_wheel)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    bundle = tmp_path / "fixture"
    build_fixture(repo_root=REPO, wheel=built_wheel, output_root=bundle,
                  policy=StructuralPolicy(8, 16384, 1, 4, 1048576))
    state = state_home / "graphify"; state.mkdir(mode=0o700)
    target = state / "runtime-manifest.json"
    target.write_bytes((bundle / "runtime-manifest.json").read_bytes()); target.chmod(0o600)
    probe = '''
import sys
from pathlib import Path
from graphify.workspace.contracts import CompatibilityManifest
from graphify.workspace.composition import load_workspace_runtime_inputs, compose_workspace_runtime
expected = CompatibilityManifest.from_json(Path(sys.argv[1]).read_bytes())
def audit(event, args):
    if event == "open" and args[2] & (64 | 512 | 1024 | 1 | 2):
        raise RuntimeError("authority loader attempted write")
    if event in {"os.mkdir", "os.rename", "os.remove", "socket.connect"}:
        raise RuntimeError(event)
sys.addaudithook(audit)
inputs = load_workspace_runtime_inputs(state_root=Path(sys.argv[2]), expected=expected)
plan = compose_workspace_runtime(inputs)
assert plan.inputs.authority.compatibility == expected
print("S2-AUTHORITY-OK")
'''
    args = [str(python), "-I", "-B", "-c", probe, str(bundle / "compatibility.json"), str(state)]
    result = subprocess.run(args, cwd=tmp_path, env=env, text=True, capture_output=True)
    if os.name == "nt":
        assert result.returncode != 0 and "inspection is unavailable" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "S2-AUTHORITY-OK"
    for arguments in (["--help"], ["install", "--platform", "codex"]):
        result = subprocess.run([str(python), "-B", "-m", "graphify", *arguments], cwd=home, env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    # The loaded noneditable bytes, not only distribution metadata, are checked.
    location = subprocess.check_output([str(python), "-I", "-B", "-c", "import graphify; print(graphify.__file__)"], env=env, text=True).strip()
    original = Path(location).read_bytes()
    Path(location).write_bytes(original + b"\n# fixture tamper\n")
    result = subprocess.run(args, cwd=tmp_path, env=env, text=True, capture_output=True)
    if os.name != "nt":
        assert result.returncode != 0 and "member mismatch" in result.stderr
    Path(location).write_bytes(original)
    installed_metadata = Path(location).parent.parent / "graphifyy-0.10.0.dist-info/entry_points.txt"
    installed_metadata.write_text("[console_scripts]\ngraphify = unexpected:main\n")
    result = subprocess.run(args, cwd=tmp_path, env=env, text=True, capture_output=True)
    if os.name != "nt":
        assert result.returncode != 0 and "member mismatch" in result.stderr


@pytest.mark.parametrize("damage", ["pth", "entry-point", "dependency"])
def test_whole_wheel_rejects_unexpected_executable_identity(built_wheel, tmp_path, damage):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts.candidate import build_fixture
    changed = tmp_path / built_wheel.name
    with zipfile.ZipFile(built_wheel) as source, zipfile.ZipFile(changed, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if damage == "entry-point" and info.filename.endswith("entry_points.txt"):
                data = b"[console_scripts]\ngraphify = unexpected:main\n"
            if damage == "dependency" and info.filename.endswith("/METADATA"):
                data = data.replace(b"Requires-Dist: networkx>=3.4", b"Requires-Dist: unexpected>=1")
            target.writestr(info, data)
        if damage == "pth": target.writestr("unexpected.pth", "import unexpected\n")
    output = tmp_path / "refused"
    with pytest.raises(ContractError):
        build_fixture(repo_root=REPO, wheel=changed, output_root=output,
                      policy=StructuralPolicy(8, 16384, 1, 4, 1048576))
    assert not output.exists()


def test_wheel_hash_and_validation_use_same_captured_bytes(built_wheel, tmp_path, monkeypatch):
    import hashlib
    from graphify.workspace.composition import StructuralPolicy
    from tools.workspace_artifacts import candidate
    copy = tmp_path / built_wheel.name
    original = built_wheel.read_bytes(); copy.write_bytes(original)
    read = candidate._read
    def replace_after_read(path):
        data = read(path)
        if path == copy: copy.write_bytes(b"replacement after capture")
        return data
    monkeypatch.setattr(candidate, "_read", replace_after_read)
    fixture = candidate.build_fixture(repo_root=REPO, wheel=copy, output_root=tmp_path / "captured",
                                      policy=StructuralPolicy(8, 16384, 1, 4, 1048576))
    assert fixture.to_dict()["wheel_sha256"] == hashlib.sha256(original).hexdigest()
