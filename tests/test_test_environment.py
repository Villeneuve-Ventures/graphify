"""Keep test-only dependencies and collection policy out of runtime behavior."""
from pathlib import Path
import subprocess
import sys
import tomllib

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def test_httpx2_is_development_only():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_names = {Requirement(dep).name for dep in config["dependency-groups"]["dev"]}
    assert "httpx2" in dev_names
    runtime = list(config["project"]["dependencies"])
    for dependencies in config["project"]["optional-dependencies"].values():
        runtime.extend(dependencies)
    assert "httpx2" not in {Requirement(dep).name for dep in runtime}


def test_collection_preserves_exclusions_without_hypothesis_warning(tmp_path):
    # Use the real configuration and poison excluded directories so a lost
    # exclusion fails collection, even if the Hypothesis plugin skips its cache.
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_visible.py").write_text("def test_visible():\n    pass\n")
    exclusions = (
        "graphify-benchmark", "graphify_eval", "graphify_test", "worked",
        "llm-stack-corpus", "llm-stack-demo", "product-site", "scripts",
        "ebook", ".github", "dist", "build", ".hypothesis",
    )
    for name in exclusions:
        directory = tests / name
        directory.mkdir()
        (directory / "test_excluded.py").write_text("raise RuntimeError('collected excluded directory')\n")
    result = subprocess.run(
        [sys.executable, *(["-O"] if sys.flags.optimize else []), "-m", "pytest", "tests/",
         "--collect-only", "-q", "-W", "error:Skipping collection"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 test collected" in output
    assert "Skipping collection" not in output
