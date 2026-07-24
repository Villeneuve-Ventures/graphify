from __future__ import annotations

from pathlib import Path
import re
import tomllib


REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
CI = REPO / ".github/workflows/ci.yml"
RELEASE_GRAPH = REPO / ".github/workflows/release-graph.yml"


def _project() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _dependency_names(requirements: list[str]) -> set[str]:
    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match is not None, requirement
        names.add(match.group(0).lower().replace("_", "-"))
    return names


def _minimum_version(requirements: list[str], name: str) -> tuple[int, ...]:
    pattern = re.compile(rf"^{re.escape(name)}>=(\d+(?:\.\d+)*)$")
    for requirement in requirements:
        match = pattern.fullmatch(requirement)
        if match is not None:
            return tuple(int(part) for part in match.group(1).split("."))
    raise AssertionError(f"missing unmarked {name} lower bound")


def _configured_python_versions(workflow: str) -> set[str]:
    return {
        version
        for line in workflow.splitlines()
        if line.lstrip().startswith("python-version:")
        for version in re.findall(r"\d+\.\d+", line)
    }


def test_package_contract_is_python_314_only() -> None:
    document = _project()
    project = document["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.14"
    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    assert "tomli" not in _dependency_names(dependencies)

    tool = document["tool"]
    assert isinstance(tool, dict)
    ruff = tool["ruff"]
    pyright = tool["pyright"]
    assert isinstance(ruff, dict)
    assert isinstance(pyright, dict)
    assert ruff["target-version"] == "py314"
    assert pyright["pythonVersion"] == "3.14"


def test_supported_extras_have_no_dead_python_markers_or_graspologic() -> None:
    project = _project()["project"]
    assert isinstance(project, dict)
    extras = project["optional-dependencies"]
    assert isinstance(extras, dict)
    assert "leiden" not in extras

    svg = extras["svg"]
    video = extras["video"]
    all_extra = extras["all"]
    pdf = extras["pdf"]
    assert isinstance(svg, list)
    assert isinstance(video, list)
    assert isinstance(all_extra, list)
    assert isinstance(pdf, list)
    assert "numpy>=2.0" in svg
    assert "faster-whisper" in video
    assert "numpy>=2.0" in all_extra
    assert "faster-whisper" in all_extra
    all_requirements: list[str] = []
    for requirements in extras.values():
        assert isinstance(requirements, list)
        assert all(isinstance(requirement, str) for requirement in requirements)
        all_requirements.extend(requirements)
    assert all("python_version" not in requirement for requirement in all_requirements)
    assert "graspologic" not in _dependency_names(all_requirements)
    assert _minimum_version(pdf, "pypdf") >= (6, 14, 2)
    assert _minimum_version(all_extra, "pypdf") >= (6, 14, 2)


def test_runtime_and_repository_tools_use_stdlib_tomllib() -> None:
    for relative in (
        "graphify/cargo_introspect.py",
        "graphify/manifest_ingest.py",
        "graphify/workspace/contracts.py",
        "tools/skillgen/gen.py",
        "tools/workspace_artifacts/candidate.py",
    ):
        source = (REPO / relative).read_text(encoding="utf-8")
        assert re.search(r"\bimport\s+tomli\b", source) is None, relative


def test_ci_uses_only_python_314_and_exact_uv() -> None:
    workflow = CI.read_text(encoding="utf-8")
    assert _configured_python_versions(workflow) == {"3.14"}
    assert workflow.count("uses: astral-sh/setup-uv@v8.1.0") == 3
    assert workflow.count('version: "0.11.30"') == 3
    assert workflow.count('python-version: "3.14"') == 2
    assert workflow.count("python-version: ${{ matrix.python-version }}") == 1


def test_release_graph_uses_python_314_and_exact_uv() -> None:
    workflow = RELEASE_GRAPH.read_text(encoding="utf-8")
    assert 'version: "0.11.30"' in workflow
    assert _configured_python_versions(workflow) == {"3.14"}
