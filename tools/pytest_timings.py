"""Opt-in, diagnostic-only timings for normal pytest collection and execution.

Load with ``-p tools.pytest_timings --ci-timing-json PATH``. Fixture costs follow
pytest's module attribution; subtest intervals are included in the parent call.
A complete record is a successful, fully reported run of the selected inventory,
not a claim that arbitrary invocation paths represent the repository's full suite.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import pytest

from tools.pytest_partition import PARTITION

PHASES = ("setup", "call", "teardown")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("CI diagnostics").addoption(
        "--ci-timing-json", metavar="PATH", help="Write advisory pytest timing JSON."
    )


def pytest_configure(config: pytest.Config) -> None:
    output = config.getoption("ci_timing_json")
    if output:
        config.pluginmanager.register(_Timing(config, Path(output)), "ci-timing-observer")


def _command(root: Path, *args: str) -> str:
    return subprocess.check_output(
        args, cwd=root, text=True, stderr=subprocess.PIPE, timeout=10
    ).strip()


def _environment(config: pytest.Config) -> dict:
    root = config.rootpath
    result = {
        "python": sys.version, "implementation": platform.python_implementation(),
        "platform": platform.platform(), "machine": platform.machine(),
        "pytest": pytest.__version__, "argv": list(config.invocation_params.args),
        "collection_paths": list(config.args),
        "plugins": sorted({
            f"{dist.project_name}=={dist.version}"
            for _, dist in config.pluginmanager.list_plugin_distinfo()
        }),
        "ci": {name: os.environ.get(name) for name in (
            "GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_REF",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "RUNNER_OS",
            "RUNNER_ARCH", "ImageOS", "ImageVersion", "PYTEST_ADDOPTS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS",
        )},
        "errors": [],
    }
    for key, command in {
        "source_revision": ("git", "rev-parse", "HEAD"),
        "tracked_changes": ("git", "status", "--porcelain", "--untracked-files=no"),
        "git": ("git", "--version"), "uv": ("uv", "--version"),
    }.items():
        try:
            result[key] = _command(root, *command)
        except Exception as error:
            result[key] = None
            result["errors"].append(f"{key}: {type(error).__name__}")
    result["sha256"] = {}
    for name, path in {
        "uv.lock": root / "uv.lock", "pyproject.toml": root / "pyproject.toml",
        ".github/workflows/ci.yml": root / ".github/workflows/ci.yml",
        "observer": Path(__file__),
    }.items():
        try:
            result["sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            result["sha256"][name] = None
    ci_sha = result["ci"]["GITHUB_SHA"]
    result["ci_source_matches_checkout"] = (
        result["source_revision"] == ci_sha if ci_sha else None
    )
    return result


class _Timing:
    def __init__(self, config: pytest.Config, output: Path) -> None:
        self.config = config
        self.output = output
        self.started = None
        self.started_at = None
        self.collection_seconds = 0.0
        self.collection_completed = False
        self.collection_errors = 0
        self.deselected = 0
        self.full_inventory = {}
        self.items = {}
        self.modules = {}
        self.phases = {}
        self.subtests = {"passed": 0, "failed": 0, "skipped": 0}
        self.valid = True
        self.output_errors = []
        self.environment = _environment(config)

    def _seconds(self, value: float) -> float:
        try:
            valid = math.isfinite(value) and value >= 0
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            self.valid = False
            return 0.0
        return value

    def _write(self, payload: dict) -> bool:
        # Atomic replacement prevents a truncated file from looking complete.
        temporary = self.output.with_name(self.output.name + ".tmp")
        try:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            self.output.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(data + "\n", encoding="utf-8")
            temporary.replace(self.output)
            return True
        except Exception as error:
            self.output_errors.append(type(error).__name__)
            return False

    def pytest_sessionstart(self) -> None:
        if self.started is not None:
            self.valid = False
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc).isoformat()
        # A killed process can leave this explicit unfinished diagnostic record.
        self._write(self._payload(None, finished=False, running=True))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection(self, session: pytest.Session):
        started = time.perf_counter()
        try:
            result = yield
            self.collection_completed = True
            return result
        finally:
            self.collection_seconds = self._seconds(time.perf_counter() - started)
            # Keep the discovered inventory even when collection raises.
            for item in session.items:
                path = os.path.relpath(item.path, self.config.rootpath).replace(os.sep, "/")
                if item.nodeid in self.items:
                    self.valid = False
                self.items[item.nodeid] = path
                module = self.modules.setdefault(path, {
                    "path": path, "selected_items": 0, "executed_items": 0,
                    **{f"{phase}_seconds": 0.0 for phase in PHASES},
                })
                module["selected_items"] += 1

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        self.full_inventory = {}
        for item in items:
            if item.nodeid in self.full_inventory:
                self.valid = False
            self.full_inventory[item.nodeid] = os.path.relpath(
                item.path, self.config.rootpath
            ).replace(os.sep, "/")
        return (yield)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self.collection_errors += int(report.failed)

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected += len(items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # pytest 9 reports subtests on the same hook, with the parent's nodeid and
        # when='call'. Adding their duration would count overlapping time twice.
        if isinstance(report, pytest.SubtestReport):
            if report.nodeid not in self.items or report.outcome not in self.subtests:
                self.valid = False
            else:
                self.subtests[report.outcome] += 1
            return
        if report.nodeid not in self.items or report.when not in PHASES:
            self.valid = False
            return
        module = self.modules[self.items[report.nodeid]]
        phases = self.phases.setdefault(report.nodeid, [])
        if not phases:
            module["executed_items"] += 1
        phases.append((report.when, report.outcome))
        key = f"{report.when}_seconds"
        module[key] = self._seconds(module[key] + self._seconds(report.duration))

    def _payload(self, exit_code, *, finished: bool, running: bool = False) -> dict:
        mode = (
            "collect-only" if self.config.getoption("collectonly") else
            "setup-only" if self.config.getoption("setuponly") else "run"
        )
        modules = []
        for path in sorted(self.modules):
            module = self.modules[path].copy()
            total = self._seconds(sum(module[f"{phase}_seconds"] for phase in PHASES))
            for phase in PHASES:
                module[f"{phase}_seconds"] = round(module[f"{phase}_seconds"], 6)
            module["total_seconds"] = round(total, 6)
            modules.append(module)
        reasons = []
        if mode != "run":
            reasons.append(mode)
        if not self.collection_completed:
            reasons.append("collection-incomplete")
        if self.collection_errors:
            reasons.append("collection-errors")
        if not self.items:
            reasons.append("no-selected-tests")
        # Structural completion is separate from success: a full failing run can
        # still have useful diagnostic coverage, while -x can truncate execution.
        outcomes = ("passed", "failed", "skipped")
        allowed = [[("setup", "passed"), ("call", outcome)] for outcome in outcomes]
        allowed += [[("setup", outcome)] for outcome in ("failed", "skipped")]
        if mode == "setup-only":
            allowed.append([("setup", "passed")])
        completed = sum(
            phases[:-1] in allowed and phases[-1:] in (
                [("teardown", outcome)] for outcome in outcomes
            )
            for phases in (self.phases.get(node, []) for node in self.items)
        )
        if completed != len(self.items):
            reasons.append("execution-incomplete")
        partition = self.config.stash.get(PARTITION, None)
        validated_partition = bool(
            partition is not None
            and partition["full_inventory"] == self.full_inventory
            and partition["selected_inventory"] == self.items
            and partition["deselected_items"] == self.deselected
            and len(self.full_inventory) == len(self.items) + self.deselected
        )
        if partition is not None and not validated_partition:
            reasons.append("invalid-shard-selection")
        if self.deselected and not validated_partition:
            reasons.append("deselected-tests")
        if exit_code != 0:
            reasons.append("pytest-unsuccessful")
        if any(outcome == "failed" for phases in self.phases.values() for _, outcome in phases):
            reasons.append("failed-tests")
        if self.subtests["failed"]:
            reasons.append("failed-subtests")
        if not self.valid:
            reasons.append("invalid-reports")
        if not finished:
            reasons.append("sessionfinish-incomplete")
        if self.output_errors:
            reasons.append("output-errors")
        return {
            "schema_version": 1, "status": "running" if running else (
                "incomplete" if reasons else "complete"
            ), "incomplete_reasons": reasons, "mode": mode, "exit_code": exit_code,
            "sessionfinish_completed": finished, "started_at": self.started_at,
            "session_seconds": round(self._seconds(time.perf_counter() - self.started), 6)
            if self.started is not None else 0.0,
            "collection_seconds": round(self.collection_seconds, 6),
            "collection_completed": self.collection_completed,
            "collection_errors": self.collection_errors,
            "full_items": len(self.full_inventory),
            "full_inventory": dict(sorted(self.full_inventory.items())),
            "selected_inventory": dict(sorted(self.items.items())),
            "partition": ({key: partition[key] for key in (
                "shard", "shard_count", "allocation_sha256"
            )} | {"selection_validated": validated_partition}) if partition else None,
            "selected_items": len(self.items), "executed_items": len(self.phases),
            "completed_items": completed,
            "deselected_items": self.deselected, "modules": modules,
            "subtests": self.subtests.copy(), "output_errors": self.output_errors.copy(),
            "environment": self.environment,
        }

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session):
        finished = False
        try:
            result = yield
            finished = True
            return result
        finally:
            # Other session finalizers can change exitstatus.
            # Observe after them, and let their exceptions propagate unchanged.
            try:
                payload = self._payload(int(session.exitstatus) if finished else None,
                                        finished=finished)
                if not self._write(payload):
                    payload = self._payload(payload["exit_code"], finished=finished)
                reporter = self.config.pluginmanager.getplugin("terminalreporter")
                if reporter is not None:
                    reporter.write_line("CI_TIMING_JSON=" + json.dumps(
                        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ))
            except Exception:
                # Diagnostics must never replace a test result or an inner error.
                pass
