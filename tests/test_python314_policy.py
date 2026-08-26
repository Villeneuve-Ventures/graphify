from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PYTHON_REQUIREMENT = ">=3.14.2,==3.14.*"


def test_package_metadata_enforces_exact_python314_window():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["project"]["version"] == "0.10.0"
    assert config["project"]["requires-python"] == PYTHON_REQUIREMENT
    assert config["project"]["optional-dependencies"]["leiden"] == [
        "graspologic-native>=1.3.1,<2"
    ]
    assert "graspologic-native>=1.3.1,<2" in config["project"]["optional-dependencies"]["all"]
    assert not any(
        dependency.split(";", 1)[0].strip() == "graspologic"
        for dependencies in config["project"]["optional-dependencies"].values()
        for dependency in dependencies
    )
    assert config["tool"]["ruff"]["target-version"] == "py314"
    assert config["tool"]["pyright"]["pythonVersion"] == "3.14"


def test_workflows_run_executable_python_on_314_only():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    release = (ROOT / ".github/workflows/release-graph.yml").read_text()
    assert 'python-version: ["3.14"]' in ci
    assert re.findall(r'python-version: "([0-9.]+)"', ci) == ["3.14", "3.14", "3.14"]
    assert re.findall(r'python-version: "([0-9.]+)"', release) == ["3.14"]
    assert 'uv pip install --only-binary=graspologic-native ".[leiden]"' in ci
    assert "ubuntu-latest, macos-latest, windows-latest" in ci
    assert "communities == {frozenset({'0', '1'}), frozenset({'2', '3'})}" in ci
    assert "docker build --tag graphify-python314 ." in ci


def test_docker_and_public_docs_match_python314_policy():
    assert "FROM python:3.14-slim" in (ROOT / "Dockerfile").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "| Python | 3.14.2 through final 3.14.x releases |" in readme
    assert "uv tool install --python '>=3.14.2,<3.15' graphifyy" in readme
    assert "uv venv --python '>=3.14.2,<3.15' .venv" in readme
    assert readme.count("uv python install '>=3.14.2,<3.15'") == 2
    assert "uvx --python '>=3.14.2,<3.15' --from graphifyy" in readme
    broad_runtime_requests = (
        "uv python install 3.14",
        "uv tool install --python 3.14",
        "uvx --python 3.14",
        "uv venv --python 3.14",
    )
    assert not any(request in readme for request in broad_runtime_requests)
    assert 'uv pip install --python .venv/bin/python "graphifyy[mcp]"' in readme
    assert 'python3 -m venv .venv && .venv/bin/pip install "graphifyy[mcp]"' not in readme
    assert "| `leiden` | Native Leiden community detection |" in readme


def test_public_readme_is_english_only():
    readme = (ROOT / "README.md").read_text()
    assert "Read this in other languages" not in readme
    assert "docs/translations/README." not in readme
    assert not list((ROOT / "docs/translations").glob("README.*.md"))


def test_public_pipx_install_validates_python314_patch_window():
    readme = (ROOT / "README.md").read_text()
    install = "pipx install --python python3.14 graphifyy"
    guard = (
        "python3.14 -c 'import sys; ok = sys.implementation.name == \"cpython\" and "
        "sys.version_info.releaselevel == \"final\" and "
        "(3, 14, 2) <= sys.version_info[:3] < (3, 15, 0); "
        "raise SystemExit(0 if ok else \"Graphify requires final CPython 3.14.2 through "
        "3.14.x.\")' \\\n  && "
    )
    assert readme.count(install) == 1
    assert f"{guard}{install}" in readme
    assert "pipx install graphifyy" not in readme
