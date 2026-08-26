import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_REQUIREMENT = ">=3.14.2,==3.14.*"
UV_PYTHON_SELECTOR = ">=3.14.2,<3.15"


def _readme_executable_uv_examples(markdown: str) -> list[str]:
    """Collect runnable Graphify uv examples from Markdown code spans and fences."""
    snippets = re.findall(r"(?<!`)`([^`\n]+)`(?!`)", markdown)
    for fence in re.findall(r"```[^\n]*\n(.*?)```", markdown, flags=re.DOTALL):
        snippets.extend(line.strip() for line in fence.splitlines())

    examples = []
    for snippet in snippets:
        try:
            tokens = shlex.split(snippet, comments=True)
        except ValueError:
            continue
        installs_graphify = tokens[:3] == ["uv", "tool", "install"] and any(
            token == "graphifyy" or token.startswith("graphifyy[") for token in tokens
        )
        runs_graphify = tokens[:1] == ["uvx"] and any(
            tokens[index : index + 2] == ["--from", "graphifyy"]
            for index in range(len(tokens) - 1)
        )
        if installs_graphify or runs_graphify:
            examples.append(snippet)
    return examples


def _assert_exact_uv_python_selectors(markdown: str) -> None:
    examples = _readme_executable_uv_examples(markdown)
    assert examples, "expected at least one executable Graphify uv example"
    for example in examples:
        tokens = shlex.split(example, comments=True)
        selectors = [
            tokens[index + 1]
            for index, token in enumerate(tokens[:-1])
            if token == "--python"
        ]
        assert selectors == [UV_PYTHON_SELECTOR], example


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
    ci_versions = re.findall(r'python-version: "([0-9.]+)"', ci)
    assert len(ci_versions) == 4
    assert set(ci_versions) == {"3.14"}
    assert re.findall(r'python-version: "([0-9.]+)"', release) == ["3.14"]
    assert 'uv pip install --only-binary=graspologic-native ".[leiden]"' in ci
    assert "ubuntu-latest, macos-latest, windows-latest" in ci
    assert "communities == {frozenset({'0', '1'}), frozenset({'2', '3'})}" in ci
    assert "docker build --tag graphify-python314 ." in ci


def test_windows_ci_selects_powershell_and_user_site_runtime_regressions():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    powershell_job = ci.split("  powershell-test:\n", 1)[1].split("\n  security-scan:", 1)[0]

    assert '-k "powershell or user_site"' in powershell_job
    assert "tests/test_skillgen.py" in powershell_job


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


def test_primary_readme_quickstart_binds_exact_python314_window():
    readme = (ROOT / "README.md").read_text()
    quickstart = readme.split("**Get started** (30 seconds):", 1)[1].split(
        "Then, in your AI assistant:", 1
    )[0]

    assert "uv tool install --python '>=3.14.2,<3.15' graphifyy" in quickstart
    assert "uv tool install graphifyy" not in quickstart


def test_all_executable_readme_uv_examples_bind_exact_python314_window():
    readme = (ROOT / "README.md").read_text()
    examples = _readme_executable_uv_examples(readme)

    _assert_exact_uv_python_selectors(readme)
    assert len(examples) >= 30
    assert any("graphifyy[all]" in example for example in examples)
    assert any(example.startswith("uvx ") for example in examples)


@pytest.mark.parametrize(
    "example",
    [
        "uv tool install graphifyy",
        "uv tool install --python 3.14 graphifyy",
        "uv tool install --python '>=3.14.2,<3.15' --python 3.14 graphifyy",
        "uvx --from graphifyy graphify install",
    ],
)
def test_readme_uv_example_validator_rejects_missing_broad_or_excess_selector(example):
    with pytest.raises(AssertionError):
        _assert_exact_uv_python_selectors(f"`{example}`")


def test_readme_uv_example_parser_excludes_negative_and_descriptive_prose():
    prose = (
        "Plain `uvx graphify …` intentionally fails. The `uv tool install` command and "
        "the package name `graphifyy` are descriptive here."
    )

    assert _readme_executable_uv_examples(prose) == []


def test_documented_direct_pip_module_commands_block_local_shadow_package(tmp_path):
    readme = (ROOT / "README.md").read_text()
    isolated_module = "PYTHONPATH= python3.14 -P -m graphify"
    isolated_server = f"{isolated_module}.serve"
    assert readme.count(isolated_module) == 8
    assert readme.count(isolated_server) == 5
    assert "python3.14 -m graphify" not in readme
    assert (
        "kimi mcp add --transport stdio graphify -- env PYTHONPATH= "
        "python3.14 -P -m graphify.serve"
    ) in readme

    marker = tmp_path / "shadow-executed"
    shadow_package = tmp_path / "graphify"
    shadow_package.mkdir()
    (shadow_package / "__init__.py").write_text("")
    (shadow_package / "serve.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-P", "-m", "graphify.serve", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Serve a graphify knowledge graph over MCP" in result.stdout
    assert not marker.exists()


def test_documented_direct_pip_install_blocks_local_pip_shadow(tmp_path):
    readme = (ROOT / "README.md").read_text()
    command = "PYTHONPATH= python3.14 -P -m pip install graphifyy"
    assert readme.count(command) == 1
    assert "python3.14 -m pip install graphifyy" not in readme

    marker = tmp_path / "shadow-executed"
    (tmp_path / "pip.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-P", "-m", "pip", "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "pip " in result.stdout
    assert not marker.exists()


def test_public_readme_is_english_only():
    readme = (ROOT / "README.md").read_text()
    assert "Read this in other languages" not in readme
    assert "docs/translations/README." not in readme
    assert not list((ROOT / "docs/translations").glob("README.*.md"))


def test_public_pipx_install_validates_python314_patch_window():
    readme = (ROOT / "README.md").read_text()
    install = "pipx install --python python3.14 graphifyy"
    guard = (
        "PYTHONPATH= python3.14 -P -c 'import sys; ok = sys.implementation.name == \"cpython\" and "
        "sys.version_info.releaselevel == \"final\" and "
        "(3, 14, 2) <= sys.version_info[:3] < (3, 15, 0); "
        "raise SystemExit(0 if ok else \"Graphify requires final CPython 3.14.2 through "
        "3.14.x.\")' \\\n  && "
    )
    assert readme.count(install) == 1
    assert f"{guard}{install}" in readme
    assert "python3.14 -c 'import sys; ok =" not in readme
    assert "pipx install graphifyy" not in readme
