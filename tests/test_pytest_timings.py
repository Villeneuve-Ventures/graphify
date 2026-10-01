"""Exercise diagnostics in isolated pytest processes, without repository fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "tools.pytest_timings"


@pytest.fixture
def suite(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "tests").mkdir()
    return tmp_path


def run(root, *args, instrument=True, output=None, opt_in=True, env_extra=None, shard=False):
    env = os.environ.copy()
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    env.update(PYTHONPATH=str(ROOT), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.update(env_extra or {})
    # Compare the original console entry point with the instrumented module entry.
    command = [sys.executable, "-m", "pytest"] if instrument else [
        str(Path(sys.executable).with_name("pytest.exe" if sys.platform == "win32" else "pytest"))
    ]
    if instrument:
        command += ["-p", PLUGIN]
        if opt_in:
            command += ["--ci-timing-json", str(output or root / "timings.json")]
    if shard:
        from tools.pytest_partition import allocation, owner
        paths = sorted((root / "tests").rglob("test_*.py"))
        shard_id = owner(paths[0].relative_to(root).as_posix(), allocation()[0])
        command += ["-p", "tools.pytest_partition", f"--ci-shard={shard_id}"]
    return subprocess.run(
        [*command, "tests/", "-q", "--tb=short", "--color=no", *args],
        cwd=root, env=env, text=True, capture_output=True, timeout=30, check=False,
    )


def timing(result, root, *, file=True):
    lines = [line.removeprefix("CI_TIMING_JSON=") for line in result.stdout.splitlines()
             if line.startswith("CI_TIMING_JSON=")]
    assert len(lines) == 1, result.stdout + result.stderr
    payload = json.loads(lines[0], parse_constant=pytest.fail)
    assert lines[0] == json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert payload["exit_code"] == result.returncode
    assert payload["schema_version"] == 1
    if file:
        assert json.loads((root / "timings.json").read_text()) == payload
    return payload


@pytest.mark.parametrize("platform,python_name,launcher", [
    ("linux", "python3.14", "pytest"),
    ("darwin", "python", "pytest"),
    ("win32", "python.exe", "pytest.exe"),
])
def test_original_entry_point_uses_current_interpreters_console_launcher(
    suite, monkeypatch, platform, python_name, launcher,
):
    executable = suite / "environment" / python_name
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "executable", str(executable))
    calls = []

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", capture)
    result = run(suite, instrument=False)
    assert result.returncode == 0
    assert calls[0][0] == [
        str(executable.with_name(launcher)), "tests/", "-q", "--tb=short", "--color=no",
    ]
    assert calls[0][1]["cwd"] == suite


def test_inert_without_explicit_option(suite):
    (suite / "tests/test_simple.py").write_text("def test_ok(): pass\n")
    result = run(suite, opt_in=False)
    assert result.returncode == 0
    assert "1 passed" in result.stdout
    assert "CI_TIMING_JSON=" not in result.stdout
    assert not (suite / "timings.json").exists()


def test_discovery_and_new_modules_match_original_entry_point(suite):
    (suite / "tests/test_z.py").write_text(
        "import pytest\n@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_case(n): pass\n"
    )
    nested = suite / "tests/nested"
    nested.mkdir()
    (nested / "test_a.py").write_text("def test_new(): pass\n")
    baseline = run(suite, "--collect-only", instrument=False)
    observed = run(suite, "--collect-only")
    nodes = lambda result: [line for line in result.stdout.splitlines()
                            if re.match(r"tests/.*::", line)]
    assert baseline.returncode == observed.returncode == 0
    assert nodes(baseline) == nodes(observed)
    assert len(nodes(observed)) == 3
    payload = timing(observed, suite)
    assert payload["selected_items"] == 3
    assert payload["executed_items"] == 0
    assert payload["mode"] == "collect-only"
    assert payload["status"] == "incomplete"
    assert [module["path"] for module in payload["modules"]] == [
        "tests/nested/test_a.py", "tests/test_z.py"
    ]


@pytest.mark.parametrize("source,code,complete", [
    ("def test_case(): pass\n", 0, True),
    ("@pytest.mark.skip(reason='skip')\ndef test_case(): pass\n", 0, True),
    ("def test_case(): pytest.skip('call skip')\n", 0, True),
    ("@pytest.mark.xfail(run=False)\ndef test_case(): pass\n", 0, True),
    ("@pytest.mark.xfail\ndef test_case(): assert False\n", 0, True),
    ("@pytest.fixture\ndef f():\n    yield\n    pytest.skip('cleanup')\n"
     "def test_case(f): pass\n", 0, True),
    ("def test_case(): assert False\n", 1, False),
    ("@pytest.fixture\ndef f(): assert False\ndef test_case(f): pass\n", 1, False),
    ("@pytest.fixture\ndef f():\n    yield\n    assert False\n"
     "def test_case(f): pass\n", 1, False),
    ("def test_case(): pytest.exit('early', returncode=0)\n", 0, False),
    ("def test_case(): raise KeyboardInterrupt\n", 2, False),
])
@pytest.mark.parametrize("shard", [False, True])
def test_outcomes_match_without_observer(suite, source, code, complete, shard):
    (suite / "tests/test_case.py").write_text("import pytest\n" + source)
    baseline = run(suite, instrument=False)
    observed = run(suite, shard=shard)
    assert baseline.returncode == observed.returncode == code, observed.stdout + observed.stderr
    payload = timing(observed, suite)
    assert payload["status"] == ("complete" if complete else "incomplete")
    assert payload["selected_items"] == 1
    # Check the public summary's result counts, excluding changing wall-clock time.
    counts = lambda result: re.findall(
        r"\d+ (?:passed|failed|skipped|xfailed|errors?)", result.stdout
    )
    assert counts(baseline) == counts(observed)


def test_module_phase_attribution_and_execution_once(suite):
    (suite / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    report.duration = {'setup': 1.25, 'call': 2.5, 'teardown': 3.75}[report.when]\n"
        "    return report\n"
    )
    for name in ("z", "a"):
        (suite / f"tests/test_{name}.py").write_text(
            "from pathlib import Path\ndef test_once():\n"
            f"    path = Path('{name}-count')\n"
            "    path.write_text(str(int(path.read_text()) + 1) if path.exists() else '1')\n"
        )
    observed = run(suite)
    payload = timing(observed, suite)
    assert observed.returncode == 0
    assert payload["selected_items"] == payload["executed_items"] == 2
    assert payload["collection_completed"] and not payload["collection_errors"]
    assert 0 < payload["collection_seconds"] <= payload["session_seconds"]
    assert payload["modules"] == [
        {"path": f"tests/test_{name}.py", "selected_items": 1, "executed_items": 1,
         "setup_seconds": 1.25, "call_seconds": 2.5, "teardown_seconds": 3.75,
         "total_seconds": 7.5}
        for name in ("a", "z")
    ]
    assert [(suite / f"{name}-count").read_text() for name in ("a", "z")] == ["1", "1"]


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("kind", ["fixture", "unittest", "setup-fixture"])
@pytest.mark.parametrize("shard", [False, True])
def test_subtests_preserve_outcomes_and_do_not_double_count(suite, fail, kind, shard):
    (suite / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    report.duration = {'setup': 1.0, 'call': 2.0, 'teardown': 3.0}[report.when]\n"
        "    return report\n"
    )
    body = (
        "import time\nimport pytest\n"
        "def test_case(subtests):\n"
        "    for n in [0, 1]:\n"
        "        with subtests.test(n=n):\n"
        "            time.sleep(0.01)\n"
        f"            assert n == 0 or {not fail}\n"
    )
    if kind == "setup-fixture":
        body = body.replace("def test_case(subtests):", "@pytest.fixture\ndef f(subtests):")
        body += "def test_case(f): pass\n"
    elif kind == "unittest":
        body = (
            "import unittest\nclass TestCase(unittest.TestCase):\n"
            "    def test_case(self):\n"
            "        for n in [0, 1]:\n"
            "            with self.subTest(n=n):\n"
            f"                self.assertTrue(n == 0 or {not fail})\n"
        )
    (suite / "tests/test_sub.py").write_text(body)
    baseline = run(suite, instrument=False)
    observed = run(suite, shard=shard)
    assert baseline.returncode == observed.returncode == int(fail), observed.stdout
    payload = timing(observed, suite)
    assert payload["subtests"] == {"passed": 1 if fail else 2, "failed": int(fail), "skipped": 0}
    assert payload["selected_items"] == payload["executed_items"] == 1
    assert payload["modules"][0]["total_seconds"] == 6.0
    assert payload["status"] == ("incomplete" if fail else "complete")


def test_partial_run_and_deselection(suite):
    (suite / "tests/test_case.py").write_text(
        "def test_first(): assert False\ndef test_second(): pass\n"
    )
    payload = timing(run(suite, "-x"), suite)
    assert payload["selected_items"] == 2 and payload["executed_items"] == 1
    assert payload["completed_items"] == 1
    assert payload["status"] == "incomplete"
    payload = timing(run(suite, "-k", "second"), suite)
    assert payload["selected_items"] == payload["executed_items"] == 1
    assert payload["deselected_items"] == 1
    assert "deselected-tests" in payload["incomplete_reasons"]


@pytest.mark.parametrize("args", [(), ("--continue-on-collection-errors",)])
def test_collection_errors_remain_visible(suite, args):
    (suite / "tests/test_ok.py").write_text("def test_ok(): pass\n")
    (suite / "tests/test_broken.py").write_text("def broken(\n")
    baseline = run(suite, *args, instrument=False)
    observed = run(suite, *args)
    assert observed.returncode == baseline.returncode != 0
    assert "SyntaxError" in observed.stdout
    payload = timing(observed, suite)
    assert payload["collection_errors"] == 1
    assert payload["selected_items"] == 1
    assert payload["executed_items"] == int(bool(args))
    assert payload["status"] == "incomplete"


def test_setup_only_and_empty_inventory(suite):
    payload = timing(run(suite), suite)
    assert payload["exit_code"] == 5
    assert payload["status"] == "incomplete"
    (suite / "tests/test_case.py").write_text("def test_case(): pass\n")
    payload = timing(run(suite, "--setup-only"), suite)
    assert payload["mode"] == "setup-only"
    assert payload["executed_items"] == 1
    assert payload["status"] == "incomplete"


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("shard", [False, True])
def test_unwritable_artifact_preserves_test_result(suite, fail, shard):
    (suite / "tests/test_case.py").write_text(f"def test_case(): assert {not fail}\n")
    blocker = suite / "blocked"
    blocker.write_text("preserve")
    result = run(suite, output=blocker / "timings.json", shard=shard)
    assert result.returncode == int(fail)
    payload = timing(result, suite, file=False)
    assert payload["status"] == "incomplete"
    assert payload["output_errors"]
    assert blocker.read_text() == "preserve"


@pytest.mark.parametrize("error", ["OSError", "RuntimeError"])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("shard", [False, True])
def test_terminal_output_failure_preserves_result_and_artifact(suite, error, fail, shard):
    (suite / "conftest.py").write_text(
        "def pytest_sessionstart(session):\n"
        "    reporter = session.config.pluginmanager.getplugin('terminalreporter')\n"
        "    original = reporter.write_line\n"
        "    def write(line, **kwargs):\n"
        f"        if line.startswith('CI_TIMING_JSON='): raise {error}('output')\n"
        "        return original(line, **kwargs)\n"
        "    reporter.write_line = write\n"
    )
    (suite / "tests/test_case.py").write_text(f"def test_case(): assert {not fail}\n")
    result = run(suite, shard=shard)
    assert result.returncode == int(fail), result.stderr
    payload = json.loads((suite / "timings.json").read_text())
    assert payload["exit_code"] == result.returncode
    assert payload["status"] == ("incomplete" if fail else "complete")


@pytest.mark.parametrize("shard", [False, True])
def test_inner_finalizer_failure_is_preserved_and_diagnostic_is_incomplete(suite, shard):
    (suite / "conftest.py").write_text(
        "def pytest_sessionfinish(session): raise RuntimeError('inner finalizer')\n"
    )
    (suite / "tests/test_case.py").write_text("def test_case(): pass\n")
    result = run(suite, shard=shard)
    assert result.returncode != 0
    assert "RuntimeError: inner finalizer" in result.stderr
    payload = json.loads((suite / "timings.json").read_text())
    assert payload["exit_code"] is None
    assert not payload["sessionfinish_completed"]
    assert payload["status"] == "incomplete"


def test_source_revision_and_environment_binding(suite):
    subprocess.run(["git", "init", "-q", str(suite)], check=True)
    (suite / "tests/test_case.py").write_text("def test_case(): pass\n")
    (suite / "uv.lock").write_text("lock fixture")
    subprocess.run(["git", "add", "."], cwd=suite, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                    "commit", "-qm", "fixture"], cwd=suite, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=suite, text=True).strip()
    result = run(suite, env_extra={"GITHUB_SHA": revision, "GITHUB_RUN_ID": "123",
                                  "GITHUB_RUN_ATTEMPT": "2", "RUNNER_OS": "Linux"})
    env = timing(result, suite)["environment"]
    assert env["source_revision"] == env["ci"]["GITHUB_SHA"] == revision
    assert env["ci_source_matches_checkout"] is True
    assert env["tracked_changes"] == ""
    assert env["ci"]["RUNNER_OS"] == "Linux"
    assert env["ci"]["GITHUB_RUN_ID"] == "123"
    assert env["ci"]["GITHUB_RUN_ATTEMPT"] == "2"
    assert env["pytest"] == pytest.__version__
    assert env["python"] == sys.version
    assert len(env["sha256"]["observer"]) == len(env["sha256"]["uv.lock"]) == 64


def test_collection_hook_exception_keeps_diagnostics(suite):
    (suite / "tests/test_case.py").write_text("def test_case(): pass\n")
    (suite / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items): raise RuntimeError('collection hook')\n"
    )
    result = run(suite)
    assert result.returncode == 3
    assert "RuntimeError: collection hook" in result.stdout
    payload = timing(result, suite)
    assert payload["status"] == "incomplete"
    assert not payload["collection_completed"]
    assert payload["collection_seconds"] > 0
    assert payload["executed_items"] == 0


@pytest.mark.parametrize("mutation", [
    "missing-teardown", "duplicate-call", "nan", "overflow", "none-duration",
])
@pytest.mark.parametrize("shard", [False, True])
def test_incomplete_or_invalid_parent_reports_are_ineligible(suite, mutation, shard):
    (suite / "tests/test_case.py").write_text("def test_case(): pass\n")
    (suite / "conftest.py").write_text(
        "import tools.pytest_timings as timing\n"
        "original = timing._Timing.pytest_runtest_logreport\n"
        "def report(self, report):\n"
        f"    mutation = {mutation!r}\n"
        "    if mutation == 'missing-teardown' and report.when == 'teardown': return\n"
        "    if mutation == 'nan': report.duration = float('nan')\n"
        "    if mutation == 'overflow': report.duration = 1e308\n"
        "    if mutation == 'none-duration': report.duration = None\n"
        "    original(self, report)\n"
        "    if mutation == 'duplicate-call' and report.when == 'call': original(self, report)\n"
        "timing._Timing.pytest_runtest_logreport = report\n"
    )
    result = run(suite, shard=shard)
    assert result.returncode == 0
    payload = timing(result, suite)
    assert payload["status"] == "incomplete"
    reason = "execution-incomplete" if mutation in ("missing-teardown", "duplicate-call") else (
        "invalid-reports"
    )
    assert reason in payload["incomplete_reasons"]


@pytest.mark.parametrize("shard", [False, True])
def test_killed_process_leaves_explicit_running_checkpoint(suite, shard):
    (suite / "tests/test_case.py").write_text("import os\ndef test_case(): os._exit(17)\n")
    result = run(suite, shard=shard)
    assert result.returncode == 17
    payload = json.loads((suite / "timings.json").read_text())
    assert payload["status"] == "running"
    assert payload["exit_code"] is None
    assert not payload["sessionfinish_completed"]


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("shard", [False, True])
def test_serialization_failure_does_not_change_result(suite, fail, shard):
    (suite / "tests/test_case.py").write_text(
        "import tools.pytest_timings as timing\n"
        "from types import SimpleNamespace\n"
        "def test_case():\n"
        "    def broken(*args, **kwargs): raise RuntimeError('serialize')\n"
        "    timing.json = SimpleNamespace(dumps=broken)\n"
        f"    assert {not fail}\n"
    )
    result = run(suite, shard=shard)
    assert result.returncode == int(fail)
    # The final write never succeeded, so the earlier record cannot claim success.
    assert json.loads((suite / "timings.json").read_text())["status"] == "running"


def test_finalizer_exit_status_is_observed(suite):
    (suite / "tests/test_case.py").write_text("def test_case(): pass\n")
    (suite / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(trylast=True)\n"
        "def pytest_sessionfinish(session): session.exitstatus = pytest.ExitCode.TESTS_FAILED\n"
    )
    result = run(suite)
    assert result.returncode == 1
    payload = timing(result, suite)
    assert payload["status"] == "incomplete"
    assert payload["completed_items"] == payload["selected_items"] == 1


def test_workflow_keeps_full_suite_and_uploads_failed_run_diagnostics():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["test"]
    assert job["strategy"]["matrix"] == {"python-version": ["3.14"], "shard": ["1", "2", "3", "4"]}
    steps = {step.get("name"): step for step in job["steps"]}
    assert steps["Install dependencies"]["run"] == "uv sync --all-extras --frozen"
    assert "git version 2.55.0" in steps["Install acceptance-pinned Git 2.55.0"]["run"]
    assert steps["Run tests"]["run"] == (
        "uv run --frozen python -m pytest -p tools.pytest_partition -p tools.pytest_timings "
        "--ci-shard=${{ matrix.shard }} "
        '--ci-timing-json "$RUNNER_TEMP/pytest-timings/timings.json" tests/ -q --tb=short'
    )
    assert "continue-on-error" not in steps["Run tests"]
    upload = steps["Upload pytest timing diagnostics"]
    assert upload["if"] == "always()"
    assert upload["continue-on-error"] == "true"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["path"] == "${{ runner.temp }}/pytest-timings/"
    steps = {step.get("name"): step for step in workflow["jobs"]["quality"]["steps"]}
    assert steps["Run protected verifier conformance with optimized Python"]["run"] == (
        "uv run --frozen python -O -m pytest tests/test_protected_change_verifier.py -q --tb=short"
    )
    assert steps["Verify install works end-to-end"]["run"] == (
        "uv run --frozen python -m tools.ci_pytest_gate smoke"
    )
