"""Fail-closed matrix aggregation, using separate receipts for every child."""
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
import yaml

from tools.ci_pytest_gate import artifact_name, check, record

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40}


@pytest.fixture
def receipts(tmp_path):
    env = {**IDENTITY, "PYTEST_OUTCOME": "success", "JOB_STATUS": "success",
           "MATRIX_RESULT": "success"}
    root = tmp_path / "pytest-results"
    for shard in (1, 2, 3, 4):
        record(root / artifact_name(shard, env) / "result.json", shard, env)
    return root, env


def workflow():
    return yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)


def test_matrix_and_gate_preserve_execution_and_environment():
    ci = workflow()
    assert set(ci["jobs"]) == {"skillgen-check", "test", "quality", "security-scan", "leiden-binary-smoke"}
    matrix = ci["jobs"]["test"]
    assert matrix["strategy"] == {"fail-fast": "false", "matrix": {
        "python-version": ["3.14"], "shard": ["1", "2", "3", "4"]}}
    assert matrix["runs-on"] == "ubuntu-latest"
    assert "continue-on-error" not in matrix
    steps = {step.get("name"): step for step in matrix["steps"]}
    assert matrix["steps"][0]["with"]["fetch-depth"] == "0"
    assert steps["Install dependencies"]["run"] == "uv sync --all-extras --frozen"
    assert steps["Run tests"]["id"] == "pytest"
    assert "continue-on-error" not in steps["Run tests"]
    result = steps["Record shard execution outcome"]
    assert result["if"] == "always()"
    assert result["env"] == {"PYTEST_OUTCOME": "${{ steps.pytest.outcome }}",
                             "JOB_STATUS": "${{ job.status }}"}
    upload = steps["Upload shard execution outcome"]
    assert upload["if"] == "always()" and "continue-on-error" not in upload
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"] == (
        "pytest-result-${{ github.run_id }}-${{ github.run_attempt }}-shard-${{ matrix.shard }}"
    )
    timing = steps["Upload pytest timing diagnostics"]
    assert timing["if"] == "always()" and timing["continue-on-error"] == "true"
    assert timing["with"]["name"] == (
        "pytest-timings-${{ github.run_id }}-${{ github.run_attempt }}-shard-${{ matrix.shard }}"
    )
    gate = ci["jobs"]["quality"]
    assert gate["name"] == "Python quality gate" and gate["needs"] == ["test"]
    assert gate["if"] == (
        "${{ always() && (github.event_name != 'pull_request' || "
        "github.event.pull_request.draft == false) }}"
    )
    assert "continue-on-error" not in gate
    gate_steps = {step.get("name"): step for step in gate["steps"]}
    download = gate_steps["Download individual shard execution outcomes"]
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {
        "pattern": "pytest-result-${{ github.run_id }}-${{ github.run_attempt }}-shard-*",
        "path": "${{ runner.temp }}/pytest-results", "merge-multiple": "false"}
    assert gate_steps["Require all four successful shard executions"]["env"] == {
        "MATRIX_RESULT": "${{ needs.test.result }}"}
    assert sum("-m pytest" in step.get("run", "") for step in gate["steps"]) == 1
    assert "tests/test_protected_change_verifier.py" in gate_steps[
        "Run protected verifier conformance with optimized Python"]["run"]
    assert gate_steps["Verify install works end-to-end"]["run"] == (
        "uv run --frozen python -m tools.ci_pytest_gate smoke")
    assert gate_steps["Reject whole-run cancellation"] == {
        "name": "Reject whole-run cancellation", "if": "cancelled()", "run": "exit 1"}
    assert not any("continue-on-error" in step for step in gate["steps"])


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX hook authority")
@pytest.mark.parametrize("configured_parent", [False, True])
def test_hook_fixtures_can_use_trusted_parent_with_unsafe_home(tmp_path, configured_parent):
    parent = os.environ.get("GRAPHIFY_TEST_AUTHORITY_PARENT") or Path.home()
    with tempfile.TemporaryDirectory(prefix=".graphify-ci-fixture-", dir=parent) as directory:
        root = Path(directory)
        home, trusted = root / "home", root / "trusted"
        home.mkdir()
        home.chmod(0o777)
        trusted.mkdir(mode=0o700)
        env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("GRAPHIFY_TEST_AUTHORITY_PARENT", None)
        if configured_parent:
            env["GRAPHIFY_TEST_AUTHORITY_PARENT"] = str(trusted)
        result = subprocess.run([
            sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider",
            "tests/test_hook_installation.py::test_partial_batch_is_complete_per_path_and_resumes_exactly",
            "tests/test_hooks.py::test_install_creates_hook", "-q", "--tb=short",
            "--basetemp", str(tmp_path / "pytest"),
        ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == (0 if configured_parent else 1), result.stdout + result.stderr
        if not configured_parent:
            assert "Unsafe installer authority directory owner, mode or ACL" in result.stdout
        assert home.stat().st_mode & 0o777 == 0o777
        assert not list(home.iterdir()) and not list(trusted.iterdir())


def test_gate_accepts_only_four_successful_receipts_even_with_successful_rollup(receipts):
    root, env = receipts
    # Do not assume matrix outputs/rollup preserve individual child outcomes.
    statuses = ("success", "failure", "cancelled", "skipped", "")
    for outcomes in product(statuses, repeat=4):
        for shard, status in enumerate(outcomes, 1):
            record(root / artifact_name(shard, env) / "result.json", shard,
                   {**env, "PYTEST_OUTCOME": status})
        if all(status == "success" for status in outcomes):
            check(root, env)
        else:
            with pytest.raises(ValueError, match="Unsuccessful"):
                check(root, env)


@pytest.mark.parametrize("matrix", ["failure", "cancelled", "skipped", "", None])
def test_gate_rejects_non_success_matrix_with_success_receipts(receipts, matrix):
    root, env = receipts
    if matrix is None:
        del env["MATRIX_RESULT"]
    else:
        env["MATRIX_RESULT"] = matrix
    with pytest.raises(ValueError, match="Matrix execution"):
        check(root, env)


@pytest.mark.parametrize("shard", [1, 2, 3, 4])
def test_missing_execution_fails_closed(receipts, shard):
    root, env = receipts
    (root / artifact_name(shard, env) / "result.json").unlink()
    with pytest.raises(ValueError, match="Missing"):
        check(root, env)


@pytest.mark.parametrize("field,value", [
    ("GITHUB_RUN_ID", "124"), ("GITHUB_RUN_ATTEMPT", "1"), ("GITHUB_SHA", "b" * 40),
    ("shard", 2), ("schema_version", 2), ("job_status", "failure"),
    ("job_status", "cancelled"), ("job_status", "skipped"), ("pytest_outcome", "neutral"),
])
def test_identity_and_status_mismatch_fail_closed(receipts, field, value):
    root, env = receipts
    path = root / artifact_name(1, env) / "result.json"
    content = json.loads(path.read_text())
    content[field] = value
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="mismatched"):
        check(root, env)


@pytest.mark.parametrize("mutation", ["unexpected", "duplicate", "malformed", "empty"])
def test_invalid_receipt_inventory_fails_closed(receipts, mutation):
    root, env = receipts
    path = root / artifact_name(1, env) / "result.json"
    if mutation == "unexpected":
        (root / "unexpected.json").write_text("{}")
    elif mutation == "duplicate":
        directory = root / "duplicate"
        directory.mkdir()
        (directory / "result.json").write_bytes(path.read_bytes())
    else:
        path.write_text("{" if mutation == "malformed" else "")
    with pytest.raises(ValueError):
        check(root, env)


@pytest.mark.parametrize("success", [True, False])
def test_workflow_gate_command_executes_fail_closed(receipts, success):
    root, env = receipts
    if not success:
        env["MATRIX_RESULT"] = "cancelled"
    step = next(step for step in workflow()["jobs"]["quality"]["steps"]
                if step.get("name") == "Require all four successful shard executions")
    environment = {**os.environ, **env, "RUNNER_TEMP": str(root.parent), "PYTHONPATH": str(ROOT)}
    command = step["run"].replace("python -m", f'"{sys.executable}" -m', 1)
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", command], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    assert ("All four pytest shard executions succeeded" in result.stdout) == success


def test_unrelated_job_policies_match_baseline():
    baseline = subprocess.check_output([
        "git", "show", "6dcd6941ec47a5e5d83044a2ac702dc2d3a6c984:.github/workflows/ci.yml"
    ], cwd=ROOT, text=True)
    old = yaml.load(baseline, Loader=yaml.BaseLoader)
    current = workflow()
    for name in ("skillgen-check", "security-scan", "leiden-binary-smoke"):
        assert current["jobs"][name] == old["jobs"][name]
    for field in ("name", "on", "concurrency"):
        assert current[field] == old[field]


def test_quality_installs_the_acceptance_git_before_conformance():
    ci = workflow()
    name = "Install acceptance-pinned Git 2.55.0"
    matrix_step = next(step for step in ci["jobs"]["test"]["steps"]
                       if step.get("name") == name)
    gate_steps = ci["jobs"]["quality"]["steps"]
    git_index = next(index for index, step in enumerate(gate_steps)
                     if step.get("name") == name)
    verifier_index = next(index for index, step in enumerate(gate_steps)
                          if step.get("name") ==
                          "Run protected verifier conformance with optimized Python")
    assert git_index < verifier_index
    assert gate_steps[git_index] == matrix_step
    assert "if" not in matrix_step and "continue-on-error" not in matrix_step
    assert "457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357" in matrix_step["run"]
    assert '"$git_build/install/bin" >> "$GITHUB_PATH"' in matrix_step["run"]


def test_smoke_propagates_a_broken_installed_launcher(tmp_path, monkeypatch):
    import sysconfig
    from tools.ci_pytest_gate import smoke

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / ("graphify.exe" if sys.platform == "win32" else "graphify")
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(scripts))
    original_run = subprocess.run

    def broken_launcher(command, **kwargs):
        if command[0] == str(launcher):
            raise subprocess.CalledProcessError(17, command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", broken_launcher)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        smoke()
    assert failure.value.returncode == 17


def test_smoke_checks_console_commands_in_a_disposable_home(tmp_path, monkeypatch):
    import sysconfig
    from tools.ci_pytest_gate import smoke

    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    protected = operator_home / "existing-skill"
    protected.write_text("preserve")
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("USERPROFILE", str(operator_home))
    scripts = tmp_path / "scripts"
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(scripts))
    launcher = scripts / ("graphify.exe" if sys.platform == "win32" else "graphify")
    calls = []

    def installed_launcher(command, **kwargs):
        assert command[0] == str(launcher)
        assert kwargs["check"] is True
        env = kwargs["env"]
        home = Path(env["HOME"])
        assert home != operator_home and home.is_dir()
        assert env["USERPROFILE"] == str(home)
        assert Path(env["CODEX_HOME"]).is_relative_to(home)
        assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(home)
        calls.append((command, home))
        if command[1:] == ["install"]:
            skill = home / ".claude/skills/graphify/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("installed skill")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", installed_launcher)
    smoke()
    assert [command for command, home in calls] == [
        [str(launcher), "--help"], [str(launcher), "install"]]
    assert calls[0][1] == calls[1][1] and not calls[0][1].exists()
    assert protected.read_text() == "preserve"
    assert set(operator_home.iterdir()) == {protected}
