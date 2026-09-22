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
import io
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


@pytest.mark.parametrize("wheel_metadata", [
    b"not metadata\n",
    b"Wheel-Version: 999.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n",
    b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp314-cp314-macosx_14_0_arm64\n",
])
def test_fixture_rejects_invalid_wheel_metadata(built_wheel, tmp_path, wheel_metadata):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    changed = tmp_path / built_wheel.name
    with zipfile.ZipFile(built_wheel) as source, zipfile.ZipFile(changed, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename.endswith("/WHEEL"):
                payload = wheel_metadata
            target.writestr(info, payload)
    output = tmp_path / "refused-wheel-metadata"
    with pytest.raises(ContractError, match="WHEEL|wheel"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=changed,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize("damage", ["duplicate-name", "missing-version", "malformed-header"])
def test_fixture_rejects_invalid_core_metadata(built_wheel, tmp_path, damage):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    changed = tmp_path / built_wheel.name
    with zipfile.ZipFile(built_wheel) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    metadata_name = next(name for name in payloads if name.endswith("/METADATA"))
    payload = payloads[metadata_name]
    if damage == "duplicate-name":
        payload = payload.replace(b"Name: graphifyy\n", b"Name: graphifyy\nName: unexpected\n", 1)
    elif damage == "missing-version":
        payload = b"\n".join(
            line for line in payload.split(b"\n")
            if not line.startswith(b"Metadata-Version:")
        )
    else:
        payload = b"not a metadata header\n" + payload
    payloads[metadata_name] = payload
    record_name = next(name for name in payloads if name.endswith("/RECORD"))
    rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"), newline="")))
    row = next(row for row in rows if row[0] == metadata_name)
    row[1] = "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(payload).digest()
    ).rstrip(b"=").decode("ascii")
    row[2] = str(len(payload))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    payloads[record_name] = stream.getvalue().encode("utf-8")
    with zipfile.ZipFile(changed, "w") as target:
        for info in infos:
            target.writestr(info, payloads[info.filename])
    output = tmp_path / "refused-core-metadata"
    with pytest.raises(ContractError, match="metadata"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=changed,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize(("field", "value"), [
    ("name", "unexpected"),
    ("version", "0.10.1"),
    ("requires-python", ">=3.15"),
])
def test_fixture_binds_wheel_identity_to_project(
        built_wheel, tmp_path, monkeypatch, field, value):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    loads = candidate.tomllib.loads

    def changed_project(payload):
        parsed = loads(payload)
        if "project" in parsed:
            parsed["project"][field] = value
        return parsed

    monkeypatch.setattr(candidate.tomllib, "loads", changed_project)
    output = tmp_path / "refused-project-identity"
    with pytest.raises(ContractError, match="project|identity"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=built_wheel,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize("filename", [
    "graphifyy-0.10.0-py2-none-any.whl",
    "unexpected-0.10.0-py3-none-any.whl",
    "graphifyy-0.10.1-py3-none-any.whl",
    "graphifyy-0.10.0-1-py3-none-any.whl",
])
def test_fixture_rejects_inconsistent_wheel_filename(built_wheel, tmp_path, filename):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    changed = tmp_path / filename
    changed.write_bytes(built_wheel.read_bytes())
    output = tmp_path / "refused-wheel-filename"
    with pytest.raises(ContractError, match="wheel filename"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=changed,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize("damage", ["missing-row", "bad-hash", "bad-size", "unsafe-row"])
def test_fixture_rejects_invalid_wheel_record(built_wheel, tmp_path, damage):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    changed = tmp_path / built_wheel.name
    with zipfile.ZipFile(built_wheel) as source, zipfile.ZipFile(changed, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename.endswith("/RECORD"):
                rows = list(csv.reader(io.StringIO(payload.decode("utf-8"), newline="")))
                ordinary = next(row for row in rows if not row[0].endswith("/RECORD"))
                if damage == "missing-row":
                    rows.remove(ordinary)
                elif damage == "bad-hash":
                    ordinary[1] = "sha256=invalid"
                elif damage == "bad-size":
                    ordinary[2] = str(int(ordinary[2]) + 1)
                else:
                    rows.append(["../outside", "", ""])
                stream = io.StringIO(newline="")
                csv.writer(stream, lineterminator="\n").writerows(rows)
                payload = stream.getvalue().encode("utf-8")
            target.writestr(info, payload)
    output = tmp_path / "refused-wheel-record"
    with pytest.raises(ContractError, match="RECORD"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=changed,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize(("field", "value"), [
    ("dependencies", ["not a valid requirement ???"]),
    ("dependencies", [7]),
    ("optional-dependencies", {"broken": ["not a valid requirement ???"]}),
    ("optional-dependencies", {"broken": "not-a-list"}),
])
def test_fixture_translates_malformed_project_dependencies(
        built_wheel, tmp_path, monkeypatch, field, value):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    loads = candidate.tomllib.loads

    def changed_project(payload):
        parsed = loads(payload)
        if "project" in parsed:
            parsed["project"][field] = value
        return parsed

    monkeypatch.setattr(candidate.tomllib, "loads", changed_project)
    output = tmp_path / "refused-project-dependencies"
    with pytest.raises(ContractError, match="dependenc"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=built_wheel,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize("setuptools", [
    None,
    {},
    {"packages": "graphify", "package-data": {}},
    {"packages": [7], "package-data": {}},
    {"packages": ["graphify"], "package-data": []},
    {"packages": ["graphify"], "package-data": {"graphify": "*.py"}},
    {"packages": ["../outside"], "package-data": {}},
    {"packages": ["graphify"], "package-data": {"graphify": ["../outside"]}},
    {"packages": ["graphify"], "package-data": {"outside": ["*.py"]}},
])
def test_fixture_translates_malformed_setuptools_selection(
        built_wheel, tmp_path, monkeypatch, setuptools):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    loads = candidate.tomllib.loads

    def changed_project(payload):
        parsed = loads(payload)
        parsed["tool"]["setuptools"] = setuptools
        return parsed

    monkeypatch.setattr(candidate.tomllib, "loads", changed_project)
    output = tmp_path / "refused-setuptools-selection"
    with pytest.raises(ContractError, match="setuptools package selection"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=built_wheel,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


def test_fixture_binds_checked_project_bytes_to_source_inventory(
        built_wheel, tmp_path, monkeypatch):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    capture = candidate._capture
    project_path = REPO / "pyproject.toml"
    project_reads = 0

    def racing_read(path, **kwargs):
        nonlocal project_reads
        payload, info = capture(path, **kwargs)
        if path == project_path:
            project_reads += 1
            if project_reads == 2:
                return payload + b"\n# alternate project snapshot\n", info
        return payload, info

    monkeypatch.setattr(candidate, "_capture", racing_read)
    output = tmp_path / "refused-project-snapshot"
    with pytest.raises(ContractError, match="project configuration differs"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=built_wheel,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize("failure", [zipfile.BadZipFile, NotImplementedError])
def test_fixture_translates_corrupt_archive_member_reads(
        built_wheel, tmp_path, monkeypatch, failure):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate

    def corrupt_member(*args, **kwargs):
        raise failure("unsupported or corrupt member")

    monkeypatch.setattr(zipfile.ZipFile, "open", corrupt_member)
    output = tmp_path / "refused-corrupt-member"
    with pytest.raises(ContractError, match="wheel archive") as caught:
        candidate.build_fixture(
            repo_root=REPO,
            wheel=built_wheel,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert isinstance(caught.value.__cause__, failure)
    assert not output.exists()


def test_fixture_rechecks_bundled_tests_outside_source_inventory(
        built_wheel, tmp_path, monkeypatch):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    manifest = candidate.source_manifest(REPO)
    target = REPO / "tests/test_workspace_contracts.py"
    manifest["files"].pop(target.relative_to(REPO).as_posix())
    capture = candidate._capture
    reads = 0

    def changed_test(path, **kwargs):
        nonlocal reads
        payload, info = capture(path, **kwargs)
        if path == target:
            reads += 1
            if reads == 2:
                return payload + b"\n# changed after fixture capture\n", info
        return payload, info

    monkeypatch.setattr(candidate, "source_manifest", lambda repo: manifest)
    monkeypatch.setattr(candidate, "_capture", changed_test)
    output = tmp_path / "refused-bundled-test-race"
    with pytest.raises(ContractError, match="candidate changed"):
        candidate.build_fixture(
            repo_root=REPO,
            wheel=built_wheel,
            output_root=output,
            policy=StructuralPolicy(8, 16384, 1, 4, 1048576),
        )
    assert not output.exists()


@pytest.mark.parametrize("damage", ["package", "metadata", "license", "total", "namespace"])
def test_wheel_expansion_rejected_before_read(built_wheel, tmp_path, monkeypatch, damage):
    from graphify.workspace.composition import StructuralPolicy
    from graphify.workspace.contracts import ContractError
    from tools.workspace_artifacts import candidate
    original_infos = zipfile.ZipFile.infolist

    def damaged_infos(archive):
        infos = original_infos(archive)
        if damage == "namespace":
            infos.append(zipfile.ZipInfo("unexpected.pth"))
        elif damage != "total":
            suffix = {"package": "graphify/source_io.py", "metadata": "/METADATA",
                      "license": "/licenses/LICENSE"}[damage]
            next(info for info in infos if info.filename.endswith(suffix)).file_size = (
                candidate.MAX_WHEEL_MEMBER_BYTES + 1)
        return infos

    def forbidden_open(*args, **kwargs):
        raise AssertionError("invalid wheel was decompressed before preflight")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", damaged_infos)
    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden_open)
    if damage == "total":
        monkeypatch.setattr(candidate, "MAX_WHEEL_EXPANDED_BYTES", 1)
    output = tmp_path / "refused"
    with pytest.raises(ContractError, match="expanded byte limit|whole-wheel members"):
        candidate.build_fixture(repo_root=REPO, wheel=built_wheel, output_root=output,
                                policy=StructuralPolicy(8, 16384, 1, 4, 1048576))
    assert not output.exists()


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_fixture_write_failure_allows_retry(tmp_path, monkeypatch, failure):
    from tools.workspace_artifacts import candidate
    output = tmp_path / "fixture"
    bundle = {name: b"{}" for name in
              ("source-manifest.json", "compatibility.json", "runtime-manifest.json")}
    original = zipfile.ZipFile.writestr

    def fail_write(*args, **kwargs):
        raise failure("injected write failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_write)
    with pytest.raises(failure):
        candidate._write_fixture(output, bundle, b"{}")
    assert not output.exists()
    monkeypatch.setattr(zipfile.ZipFile, "writestr", original)
    candidate._write_fixture(output, bundle, b"{}")
    assert (output / "fixture-bundle.zip").is_file()


@pytest.mark.parametrize("occupied", [False, True])
def test_fixture_publication_preserves_racing_destination(tmp_path, monkeypatch, occupied):
    from tools.workspace_artifacts import candidate
    output = tmp_path / "fixture"
    bundle = {name: b"{}" for name in
              ("source-manifest.json", "compatibility.json", "runtime-manifest.json")}
    publish = candidate._publish_fixture

    def racing_publish(payload_root, destination, **kwargs):
        output.mkdir()
        if occupied:
            (output / "other-owner").write_bytes(b"keep")
        publish(payload_root, destination, **kwargs)

    monkeypatch.setattr(candidate, "_publish_fixture", racing_publish)
    with pytest.raises(FileExistsError):
        candidate._write_fixture(output, bundle, b"{}")
    assert output.is_dir()
    assert sorted(p.name for p in output.iterdir()) == (["other-owner"] if occupied else [])
    if occupied:
        assert (output / "other-owner").read_bytes() == b"keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fixture"]


def test_fixture_failure_never_cleans_public_destination(tmp_path, monkeypatch):
    from tools.workspace_artifacts import candidate
    output = tmp_path / "fixture"
    bundle = {name: b"{}" for name in
              ("source-manifest.json", "compatibility.json", "runtime-manifest.json")}

    def create_destination_and_fail(*args, **kwargs):
        output.mkdir()
        (output / "other-owner").write_bytes(b"keep")
        raise OSError("injected failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", create_destination_and_fail)
    with pytest.raises(OSError, match="injected failure"):
        candidate._write_fixture(output, bundle, b"{}")
    assert (output / "other-owner").read_bytes() == b"keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fixture"]
