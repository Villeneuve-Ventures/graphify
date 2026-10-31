"""The normal dependency checks use the PR lock and propagate tool failures."""

import os
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("workflow", ["ci.yml", "dependabot-ci.yml"])
@pytest.mark.parametrize("name,command", [
    ("Lint locked dependencies", "uv run --frozen ruff check ."),
    ("Typecheck locked dependencies", "uv run --frozen pyright"),
])
def test_quality_checks_use_locked_environment_and_fail_closed(tmp_path, workflow, name, command):
    job = load(f".github/workflows/{workflow}")["jobs"]["quality"]
    steps = job["steps"]
    step = next(step for step in steps if step.get("name") == name)
    install = next(step for step in steps if step.get("name") == "Install dependencies")
    assert install["run"] == "uv sync --all-extras --frozen"
    assert steps.index(install) < steps.index(step)
    assert step == {"name": name, "run": command}
    assert "continue-on-error" not in job
    # Run the workflow shell with a failing tool, without another environment sync.
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\nexit 23\n')
    uv.chmod(0o700)
    capture = tmp_path / "arguments"
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            cwd=ROOT, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                           "CAPTURE": str(capture)}, check=False)
    assert result.returncode == 23
    assert capture.read_text().splitlines() == shlex.split(command)[1:]


def test_precommit_ruff_uses_the_same_lock_and_rejects_bad_python(tmp_path):
    config = load(".pre-commit-config.yaml")
    repo, hook = next((repo, hook) for repo in config["repos"] for hook in repo["hooks"]
                      if hook["id"] == "ruff")
    assert repo["repo"] == "local" and "rev" not in repo
    assert hook["entry"] == "uv run --frozen ruff check"
    assert hook["language"] == "system"
    assert hook["types_or"] == ["python", "pyi"]
    source = tmp_path / "broken.py"
    source.write_text("def broken(:\n")
    before = source.read_bytes()
    command = shlex.split(hook["entry"]) + hook["args"] + [str(source)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "invalid-syntax" in result.stdout
    assert source.read_bytes() == before
    source.write_text("value = 1\n")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
