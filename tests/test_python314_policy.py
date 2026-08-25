from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PYTHON_REQUIREMENT = ">=3.14.2,==3.14.*"
TRANSLATION_NOTICE = (
    "> **Compatibility authority:** This translation may lag behind. The English README "
    "is authoritative for current Python requirements, supported platforms, installation "
    "commands, and optional dependencies."
)


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
    assert "docker build --tag graphify-python314 ." in ci


def test_docker_and_public_docs_match_python314_policy():
    assert "FROM python:3.14-slim" in (ROOT / "Dockerfile").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "| Python | 3.14.2 through final 3.14.x releases |" in readme
    assert "uv tool install --python 3.14 graphifyy" in readme
    assert "| `leiden` | Native Leiden community detection |" in readme


def test_every_linked_translation_defers_compatibility_to_english_readme():
    readme = (ROOT / "README.md").read_text()
    linked = set(re.findall(r'href="(docs/translations/README\.[^"]+\.md)"', readme))
    translation_files = set(
        str(path.relative_to(ROOT)) for path in (ROOT / "docs/translations").glob("README.*.md")
    )
    assert linked == translation_files
    for relative_path in sorted(linked):
        assert (ROOT / relative_path).read_text().startswith(f"{TRANSLATION_NOTICE}\n\n")
