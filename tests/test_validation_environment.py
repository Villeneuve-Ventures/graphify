"""Real uv/default Make proofs for the validation environment boundary."""

from pathlib import Path
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from wheel.wheelfile import WheelFile

from tools import validation_route as route
from tools.validation_environment import (
    EnvironmentRefused, GIT_NAMESPACE_VARIABLES, ValidationEnvironment, check_uv_argv, git_environment,
)


ROOT = Path(__file__).resolve().parents[1]


def execute(argv, root, env):
    return subprocess.run(argv, cwd=root, env=env, text=True, capture_output=True)


def inventory(root):
    return {str(p.relative_to(root)): (p.lstat().st_ino, p.lstat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in root.rglob("*") if not p.is_symlink()}


def _write_fixture_lock(root, offline_wheels):
    """Keep locked versions/edges, using local wheels for the fixture's offline inputs."""
    header, *packages = (ROOT / "uv.lock").read_text().split("[[package]]\n")
    format_version = tomllib.loads(header)
    fixture = (
        f'version = {format_version["version"]}\nrevision = {format_version["revision"]}\n'
        'requires-python = ">=3.14,<3.15"\n\n'
        '[[package]]\nname = "graphifyy"\nversion = "0.0.0"\n'
        'source = { editable = "." }\ndependencies = [{ name = "pytest" }]\n\n'
        '[package.metadata]\nrequires-dist = [{ name = "pytest", specifier = "==9.0.3" }]\n\n'
    )
    for package in packages:
        locked = tomllib.loads("[[package]]\n" + package)["package"][0]
        name = locked["name"]
        if name == "graphifyy":
            continue
        if name in offline_wheels:
            wheel = offline_wheels[name]
            package = (
                f'name = {json.dumps(name)}\nversion = {json.dumps(locked["version"])}\n'
                f'source = {{ path = {json.dumps(str(wheel))} }}\n'
            )
            if locked.get("dependencies"):
                package += "dependencies = [\n"
                for dependency in locked["dependencies"]:
                    fields = ", ".join(f"{key} = {json.dumps(value)}"
                                       for key, value in dependency.items())
                    package += "    { " + fields + " },\n"
                package += "]\n"
            digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
            package += (f'wheels = [{{ filename = {json.dumps(wheel.name)}, '
                        f'hash = {json.dumps(digest)} }}]\n\n')
        fixture += "[[package]]\n" + package
    (root / "uv.lock").write_text(fixture)


@pytest.fixture(scope="session")
def offline_wheels(tmp_path_factory):
    """Repack locked pure-Python inputs and their active dependencies for offline setup.

    The installed environment must match the repository lock. Installed console
    wrappers and bytecode are omitted; WheelFile regenerates the wheel RECORD.
    """
    root = tmp_path_factory.mktemp("offline-wheels")
    locked = {(package["name"], package["version"])
              for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    wheels = {}
    pending = ["setuptools", "attrs", "pytest"]
    while pending:
        name = pending.pop()
        if name in wheels:
            continue
        distribution = importlib.metadata.distribution(name)
        assert (name, distribution.version) in locked
        metadata = distribution.read_text("WHEEL")
        assert metadata is not None
        assert "Root-Is-Purelib: true" in metadata
        assert "Tag: py3-none-any" in metadata
        files = distribution.files
        assert files is not None
        wheel = root / f"{name.replace('-', '_')}-{distribution.version}-py3-none-any.whl"
        scripts = {entry.name for entry in distribution.entry_points
                   if entry.group == "console_scripts"}
        with WheelFile(wheel, "w") as archive:
            for item in files:
                if item.is_absolute() or ".." in item.parts:
                    assert item.name in scripts
                    continue
                if item.suffix == ".pyc" or (item.name == "RECORD"
                                            and item.parent.name.endswith(".dist-info")):
                    continue
                archive.write(str(distribution.locate_file(item)), str(item))
        wheels[name] = wheel
        for requirement in distribution.requires or ():
            dependency = Requirement(requirement)
            if dependency.marker is None or dependency.marker.evaluate({"extra": ""}):
                pending.append(canonicalize_name(dependency.name))
    return wheels


@pytest.fixture
def projects(tmp_path, offline_wheels):
    """Create offline projects with caller-anchored cache paths and no foreign routing."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("UV_", "PYTHON", "PYTEST_", "CONDA_"))
           and k not in {"VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "GRAPHIFY_VALIDATION_BASE"}}
    if "UV_CACHE_DIR" in os.environ:
        env["UV_CACHE_DIR"] = os.path.join(os.getcwd(), os.environ["UV_CACHE_DIR"])
    env.update(UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")

    def create(name, *, passing=False):
        root = tmp_path / name
        root.mkdir()
        (root / "tools").mkdir()
        for file in ("__init__.py", "validation_route.py", "validation_environment.py"):
            shutil.copyfile(ROOT / "tools" / file, root / "tools" / file)
        shutil.copyfile(ROOT / "Makefile", root / "Makefile")
        (root / "graphify").mkdir()
        (root / "graphify/__init__.py").write_text("")
        (root / "graphify/validation_plugin.py").write_text(
            'import pytest\n@pytest.fixture\ndef installed_plugin_value():\n    return True\n'
        )
        (root / "tests").mkdir()
        (root / "tests/test_readme_policy.py").write_text(
            'import os\ndef test_required_local_check(pytestconfig, installed_plugin_value):\n'
            '    assert installed_plugin_value\n'
            '    assert pytestconfig.getoption("strict_markers")\n'
            '    assert "PYTEST_PLUGINS" not in os.environ\n'
            '    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in os.environ\n'
            '    assert "PYTHONPATH" not in os.environ\n'
            f'    assert {passing!r}, "required local test executed"\n'
        )
        (root / "docs").mkdir()
        (root / "docs/notes.md").write_text("Initial explanation.\n")
        (root / "README.md").write_text("Project overview.\n")
        (root / ".gitignore").write_text(".venv/\n__pycache__/\n.pytest_cache/\n*.egg-info/\n")
        (root / "pyproject.toml").write_text(
            '[project]\nname="graphifyy"\nversion="0.0.0"\nrequires-python=">=3.14,<3.15"\n'
            'dependencies=["pytest==9.0.3"]\n'
            '[project.entry-points.pytest11]\nvalidation_fixture="graphify.validation_plugin"\n'
            '[build-system]\nrequires=['
            + json.dumps("setuptools @ " + offline_wheels["setuptools"].as_uri()) + ']\n'
            'build-backend="setuptools.build_meta"\n'
            '[tool.setuptools]\npackages=["graphify"]\n'
            '[tool.pytest.ini_options]\naddopts="--strict-markers"\n'
        )
        _write_fixture_lock(root, offline_wheels)
        for argv in (["git", "init", "-q"], ["git", "config", "user.email", "fixture@example.invalid"],
                     ["git", "config", "user.name", "Fixture"], ["git", "add", "."],
                     ["git", "commit", "-qm", "fixture"]):
            result = execute(argv, root, env)
            assert result.returncode == 0, result.stdout + result.stderr
        base = execute(["git", "rev-parse", "HEAD"], root, env).stdout.strip()
        (root / "docs/notes.md").write_text("Revised explanation.\n")
        return root, base

    return create, env


def prepare(root, env):
    result = execute(["uv", "sync", "--all-extras", "--frozen"], root, env)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_build_backend_works_with_an_empty_offline_cache(projects, tmp_path):
    create, env = projects
    primary, _ = create("primary")
    output = tmp_path / "built-wheels"
    result = execute(["uv", "build", "--wheel", "--offline", "--out-dir", str(output)],
                     primary, {**env, "UV_CACHE_DIR": str(tmp_path / "empty-build-cache")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (output / "graphifyy-0.0.0-py3-none-any.whl").is_file()


def test_fixture_sync_works_with_an_empty_offline_cache(projects, tmp_path):
    """Exercise frozen setup and the installed pytest plugin without cached packages."""
    create, env = projects
    primary, base = create("primary", passing=True)
    settings = {**env, "UV_CACHE_DIR": str(tmp_path / "empty-sync-cache")}
    prepare(primary, settings)
    result = execute(["make", "fast-feedback", f"BASE={base}"], primary, settings)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "validation_route_status=complete; selected_lane=editorial-docs" in output
    assert "1 passed" in output


@pytest.mark.parametrize("relative_cache", [False, True])
def test_projects_use_configured_cache_without_foreign_project_routing(
        tmp_path, monkeypatch, request, relative_cache):
    cache = tmp_path / "custom-cache"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UV_CACHE_DIR", cache.name if relative_cache else str(cache))
    monkeypatch.setenv("UV_PROJECT", str(tmp_path / "foreign"))
    monkeypatch.setenv("UV_OFFLINE", "0")
    _, env = request.getfixturevalue("projects")
    assert env["UV_OFFLINE"] == "1"
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert "UV_PROJECT" not in env
    child = tmp_path / "child"
    child.mkdir()
    result = execute(["uv", "cache", "dir"], child, env)
    assert result.returncode == 0, result.stderr
    reported = Path(result.stdout.strip())
    assert (reported if reported.is_absolute() else child / reported) == cache
    assert env["UV_CACHE_DIR"] == str(cache)


def test_admitted_child_uv_uses_configured_offline_cache(tmp_path, monkeypatch):
    cache = tmp_path / "custom-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setenv("UV_OFFLINE", "1")
    admitted = ValidationEnvironment(tmp_path)
    child = admitted.child_environment()
    result = execute(["uv", "cache", "dir"], tmp_path, child)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == cache
    assert child["UV_OFFLINE"] == "1"
    assert child["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / ".venv")


@pytest.mark.parametrize("control", ["UV_WORKING_DIR", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT",
                                      "UV_ENV_FILE", "plugins", "imports"])
def test_actual_uv_ignores_foreign_routing_and_preserves_required_check(projects, control, tmp_path):
    create, env = projects
    primary, base = create("primary")
    foreign, _ = create("foreign", passing=True)
    prepare(foreign, env)
    before = inventory(foreign)
    options = tmp_path / "options.env"
    options.write_text("PYTEST_ADDOPTS=--collect-only\n")
    settings = dict(env)
    if control == "UV_PROJECT_ENVIRONMENT":
        settings[control] = str(foreign / ".venv")
    elif control == "UV_ENV_FILE":
        settings[control] = str(options)
    elif control == "plugins":
        settings.update(PYTEST_PLUGINS="foreign_plugin_must_not_load", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    elif control == "imports":
        settings.update(PYTHONPATH=str(foreign), PYTHONHOME=str(foreign), PYTHONUSERBASE=str(foreign))
    else:
        settings[control] = str(foreign)
    # Use default Make bootstrap: no PYTHON or environment override supplied.
    result = execute(["make", "fast-feedback", f"BASE={base}"], primary, settings)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "required local test executed" in output, output
    assert "validation_route_status=complete" not in output
    assert '"stage": "postsetup-before-test"' in output
    assert inventory(foreign) == before


@pytest.mark.parametrize("target", ["fast-feedback", "review-ready"])
def test_local_symlink_is_refused_before_real_uv_can_mutate_foreign_environment(projects, target):
    create, env = projects
    primary, base = create("primary")
    foreign, _ = create("foreign", passing=True)
    prepare(foreign, env)
    (primary / ".venv").symlink_to(foreign / ".venv", target_is_directory=True)
    before = inventory(foreign)
    result = execute(["make", target, f"BASE={base}"], primary, env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "aliases and symlinks" in output
    assert "$ uv " not in output
    assert inventory(foreign) == before


def test_real_owned_local_with_foreign_editable_binding_is_refused(projects):
    create, env = projects
    primary, base = create("primary")
    foreign, _ = create("foreign")
    prepare(foreign, env)
    (foreign / ".venv").rename(primary / ".venv")
    before = inventory(primary / ".venv")
    result = execute(["make", "review-ready", f"BASE={base}"], primary, env)
    assert result.returncode != 0
    assert "another checkout" in result.stderr
    assert "$ uv " not in result.stdout
    assert inventory(primary / ".venv") == before


def test_explicit_owned_external_environment_retains_optional_packages(projects, tmp_path, offline_wheels):
    create, env = projects
    primary, base = create("primary", passing=True)
    prepare(primary, env)
    external = tmp_path / "external"
    (primary / ".venv").rename(external)
    optional = execute(["uv", "pip", "install", "--offline", "--python", str(external / "bin/python"),
                        str(offline_wheels["attrs"])], primary, env)
    assert optional.returncode == 0, optional.stderr
    result = execute(["make", "fast-feedback", f"BASE={base}",
                      f"PYTHON={external / 'bin/python'}", f"VALIDATION_ENVIRONMENT={external}"], primary, env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "validation_route_status=complete; selected_lane=editorial-docs" in output
    assert '"state": "explicit-external"' in output
    assert '"isolated_prefix": ' + json.dumps(str(external)) in output
    assert '"actual_package_origin": ' + json.dumps(str(primary / "graphify/__init__.py")) in output
    retained = execute([str(external / "bin/python"), "-I", "-c", "import attrs"], primary, env)
    assert retained.returncode == 0, retained.stderr
    assert not (primary / ".venv").exists()
    # A mixed snapshot must forward the same explicit admission through recursive
    # Make and run the complete fixture suite, with the full sync semantics.
    (primary / "runtime.py").write_text("value = 1\n")
    full = execute(["make", "fast-feedback", f"BASE={base}",
                    f"PYTHON={external / 'bin/python'}", f"VALIDATION_ENVIRONMENT={external}"], primary, env)
    assert full.returncode == 0, full.stdout + full.stderr
    assert "$ uv sync --all-extras --frozen" in full.stdout
    assert "validation_route_status=complete; selected_lane=full" in full.stdout
    assert not (primary / ".venv").exists()


@pytest.mark.parametrize("options", ["--collect-only", "--lf"])
def test_default_make_rejects_ambient_pytest_options_before_any_setup(projects, options):
    create, env = projects
    primary, base = create("primary")
    result = execute(["make", "fast-feedback", f"BASE={base}"], primary,
                     {**env, "PYTEST_ADDOPTS": options})
    assert result.returncode != 0
    assert "ambient-pytest-addopts" in result.stderr
    assert "$ uv " not in result.stdout
    assert "validation_route_status=complete" not in result.stdout
    assert not (primary / ".venv").exists()


def test_correct_editable_metadata_cannot_hide_earlier_foreign_pth(projects):
    create, env = projects
    primary, base = create("primary")
    foreign, _ = create("foreign")
    prepare(primary, env)
    site = primary / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    metadata = next(site.glob("graphifyy-*.dist-info/direct_url.json"))
    assert json.loads(metadata.read_text())["url"] == primary.as_uri()
    (site / "00_foreign.pth").write_text(str(foreign) + "\n")
    before = inventory(primary / ".venv")
    foreign_before = inventory(foreign)
    result = execute(["make", "review-ready", f"BASE={base}"], primary, env)
    assert result.returncode != 0
    assert "normal Python package origin belongs to another checkout" in result.stderr
    assert "$ uv " not in result.stdout
    assert inventory(primary / ".venv") == before
    assert inventory(foreign) == foreign_before


@pytest.mark.parametrize("control", ["MAKEFLAGS", "MAKEFILES"])
def test_direct_cli_cannot_qualify_printed_full_make_instead_of_running_tests(projects, tmp_path, control):
    create, env = projects
    primary, base = create("primary")
    prepare(primary, env)
    (primary / "runtime.py").write_text("value = 1\n")
    poison = tmp_path / "poison.mk"
    poison.write_text("override MAKEFLAGS += --just-print\n")
    settings = {**env, control: "--just-print" if control == "MAKEFLAGS" else str(poison)}
    result = execute([
        str(primary / ".venv/bin/python"), "-I", "-S", "-B", "-c",
        'import os,runpy,sys; sys.path.insert(0,os.getcwd()); '
        'runpy.run_module("tools.validation_route",run_name="__main__")',
        "--base", base, "--validation-environment", str(primary / ".venv"),
    ], primary, settings)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "required local test executed" in output, output
    assert "$ uv sync --all-extras --frozen" in output
    assert "validation_route_status=complete" not in output


@pytest.mark.parametrize("control", ["MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKEFILES",
                                      "GNUMAKEFLAGS", "VALIDATION_ENVIRONMENT"])
def test_child_environment_drops_make_action_controls_but_retains_policy_context(tmp_path, monkeypatch, control):
    monkeypatch.setenv(control, "untrusted")
    monkeypatch.setenv("GRAPHIFY_VALIDATION_BASE", "explicit-policy-context")
    admitted = ValidationEnvironment(tmp_path)
    child = admitted.child_environment()
    assert control not in child
    assert child["GRAPHIFY_VALIDATION_BASE"] == "explicit-policy-context"


def test_direct_make_bootstraps_full_environment_and_runs_entire_suite(projects):
    create, env = projects
    primary, base = create("primary")
    result = execute(["make", "review-ready", f"BASE={base}"], primary, env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "required local test executed" in output, output
    assert output.index("$ uv sync --all-extras --frozen") < output.index('"stage": "postsetup-before-test"')
    assert "$ uv run --frozen python -m pytest tests/ -q --tb=short" in output


def test_direct_make_setup_failure_does_not_start_pytest(projects):
    create, env = projects
    primary, base = create("primary")
    (primary / "uv.lock").write_text("not valid toml [\n")
    result = execute(["make", "review-ready", f"BASE={base}"], primary, env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "$ uv sync --all-extras --frozen" in output
    assert "$ uv run " not in output
    assert "postsetup-before-test" not in output


def test_unsupported_inline_uv_routing_arguments_fail_closed():
    with pytest.raises(EnvironmentRefused, match="unsupported inline"):
        check_uv_argv(("uv", "run", "--frozen", "python", "-m", "pytest", "--directory=elsewhere"))


@pytest.mark.parametrize("existing", [False, True])
def test_actual_other_minor_bootstrap_uses_candidate_runtime_paths(projects, existing):
    create, env = projects
    bootstrap = Path("/usr/bin/python3")
    if not bootstrap.exists():
        pytest.skip("a second system Python is unavailable")
    version = execute([str(bootstrap), "-I", "-S", "-c",
                       "import sys; print(sys.version_info.minor)"], ROOT, env)
    if version.returncode or int(version.stdout.strip()) == sys.version_info.minor:
        pytest.skip("system Python does not provide a different bootstrap minor")
    primary, base = create("primary", passing=True)
    if existing:
        prepare(primary, env)
    result = execute(["make", "fast-feedback", f"PYTHON={bootstrap}", f"BASE={base}"], primary, env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "validation_route_status=complete; selected_lane=editorial-docs" in output
    assert '"python_version": [3, 14,' in output
    assert str(primary / ".venv/lib/python3.14/site-packages") in output


def test_candidate_must_satisfy_the_actual_project_python_requirement(projects):
    create, env = projects
    primary, base = create("primary")
    prepare(primary, env)
    project = primary / "pyproject.toml"
    project.write_text(project.read_text().replace('>=3.14,<3.15', '>=3.15,==3.15.*'))
    before = inventory(primary / ".venv")
    result = execute(["make", "review-ready", f"BASE={base}"], primary, env)
    assert result.returncode != 0
    assert "does not satisfy project requires-python" in result.stderr
    assert "$ uv " not in result.stdout
    assert inventory(primary / ".venv") == before


def test_explicit_selection_requires_running_the_selected_python(projects):
    create, env = projects
    primary, base = create("primary")
    prepare(primary, env)
    before = inventory(primary / ".venv")
    result = execute([sys.executable, "-I", "-S", "-B", "-c",
                      'import os,runpy,sys; sys.path.insert(0,os.getcwd()); '
                      'runpy.run_module("tools.validation_route",run_name="__main__")',
                      "--base", base, "--validation-environment", str(primary / ".venv")], primary, env)
    assert result.returncode != 0
    assert "must match the running Python prefix" in result.stderr
    assert "$ uv " not in result.stdout
    assert inventory(primary / ".venv") == before


def test_foreign_git_namespace_cannot_hide_mixed_changes_or_qualify_docs(projects, tmp_path):
    create, env = projects
    primary, _ = create("primary", passing=True)
    (primary / "tests/test_runtime_gate.py").write_text(
        'def test_full_runtime_gate():\n    assert False, "full runtime sentinel executed"\n'
    )
    for argv in (["git", "add", "."], ["git", "commit", "-qm", "common baseline"]):
        assert execute(argv, primary, env).returncode == 0
    base = execute(["git", "rev-parse", "HEAD"], primary, env).stdout.strip()
    prepare(primary, env)
    foreign = tmp_path / "foreign"
    clone = execute(["git", "clone", "--no-hardlinks", str(primary), str(foreign)], tmp_path, env)
    assert clone.returncode == 0, clone.stderr
    (primary / "runtime.py").write_text("value = 1\n")
    (foreign / "docs/notes.md").write_text("Foreign editorial explanation.\n")
    before = inventory(foreign)
    result = execute(["make", "fast-feedback", f"BASE={base}"], primary,
                     {**env, "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign)})
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "selected_lane=full" in output
    assert "full runtime sentinel executed" in output
    assert "validation_route_status=complete" not in output
    assert inventory(foreign) == before


def test_linked_worktree_gitfile_remains_supported(projects, tmp_path, monkeypatch):
    create, env = projects
    primary, base = create("primary")
    linked = tmp_path / "linked"
    added = execute(["git", "worktree", "add", "--detach", str(linked), base], primary, env)
    assert added.returncode == 0, added.stderr
    assert (linked / ".git").is_file()
    (linked / "docs/notes.md").write_text("Linked editorial explanation.\n")
    monkeypatch.setattr(route, "ROOT", linked)
    monkeypatch.setenv("GIT_DIR", str(primary / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(primary))
    assert route.classify_repository(base).lane == route.EDITORIAL_DOCS
    result = route.run_git_bytes(["rev-parse", "--show-toplevel"])
    assert result.returncode == 0
    assert Path(result.stdout.decode().strip()).resolve() == linked.resolve()


def test_physical_git_top_level_must_match_inspected_root(projects, monkeypatch):
    create, env = projects
    primary, _ = create("primary")
    nested = primary / "docs"
    actual = execute(["git", "rev-parse", "--show-toplevel"], nested, env)
    assert actual.returncode == 0
    assert Path(actual.stdout.strip()).resolve() == primary.resolve()
    monkeypatch.setattr(route, "ROOT", nested)
    result = route.run_git(["ls-files"])
    assert result.returncode != 0
    assert "Git top-level does not match inspected root" in result.stderr


def test_git_selectors_are_cleared_without_removing_transport_or_restrictive_controls(tmp_path, monkeypatch):
    for key in GIT_NAMESPACE_VARIABLES:
        monkeypatch.setenv(key, "foreign")
    retained = {"GIT_SSH_COMMAND": "ssh -i /retained/key", "GIT_ASKPASS": "/retained/askpass",
                "GIT_CONFIG_GLOBAL": "/retained/gitconfig", "GIT_CONFIG_COUNT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1"}
    for key, value in retained.items():
        monkeypatch.setenv(key, value)
    for clean in (git_environment(), ValidationEnvironment(tmp_path).child_environment()):
        assert not GIT_NAMESPACE_VARIABLES.intersection(clean)
        assert {key: clean[key] for key in retained} == retained
