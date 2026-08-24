from __future__ import annotations

import ctypes
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from typing import Any

import pytest

from tools import pytest_parallel_gate as gate


_EXPECTED_HAZARD_PATHS = [
    "tests/test_install.py",
    "tests/test_skillgen.py",
    "tests/test_cache.py",
    "tests/test_extract_cache_location.py",
    "tests/test_word_count_cache.py",
    "tests/test_zero_node_no_cache.py",
    "tests/test_cli_broken_pipe.py",
    "tests/test_claude_cli_backend.py",
    "tests/test_cpp_preprocess.py",
    "tests/test_ollama_retry_cap.py",
    "tests/test_watch.py",
    "tests/test_workspace_runtime.py",
    "tests/test_workspace_generations.py",
    "tests/test_workspace_journal.py",
    "tests/test_workspace_pointers.py",
    "tests/test_workspace_semantic_queue.py",
    "tests/test_workspace_semantic_worker.py",
    "tests/test_workspace_staged_build_integration.py",
    "tests/test_workspace_staged_build_recovery.py",
    "tests/test_workspace_status.py",
    "tests/test_workspace_adapter.py",
]


@pytest.fixture(autouse=True)
def _isolate_nested_pytest_from_outer_xdist(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "PYTHONPATH",
        "PYTEST_PLUGINS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(variable, raising=False)


def _report(
    when: str,
    outcome: str,
    *,
    wasxfail_present: bool = False,
    strict_xpass: bool = False,
):
    return {
        "when": when,
        "outcome": outcome,
        "wasxfail_present": wasxfail_present,
        "strict_xpass": strict_xpass,
    }


@pytest.mark.parametrize(
    ("reports", "expected"),
    [
        ([_report("setup", "passed"), _report("call", "passed"), _report("teardown", "passed")], "passed"),
        ([_report("setup", "passed"), _report("call", "failed"), _report("teardown", "passed")], "failed"),
        ([_report("setup", "skipped"), _report("teardown", "passed")], "skipped"),
        ([_report("setup", "skipped", wasxfail_present=True), _report("teardown", "passed")], "xfailed"),
        ([_report("setup", "passed"), _report("call", "skipped", wasxfail_present=True), _report("teardown", "passed")], "xfailed"),
        ([_report("setup", "passed"), _report("call", "passed", wasxfail_present=True), _report("teardown", "passed")], "xpassed"),
        ([_report("setup", "passed"), _report("call", "failed", strict_xpass=True), _report("teardown", "passed")], "xpassed_strict_failed"),
        ([_report("setup", "failed"), _report("teardown", "passed")], "error"),
        ([_report("setup", "passed"), _report("call", "passed"), _report("teardown", "failed")], "error"),
        ([_report("setup", "passed"), _report("call", "passed"), _report("teardown", "skipped")], "incomplete"),
        ([_report("setup", "passed"), _report("call", "passed")], "incomplete"),
        ([_report("setup", "passed"), _report("call", "passed"), _report("call", "passed"), _report("teardown", "passed")], "incomplete"),
    ],
)
def test_normalize_node_reports(reports, expected):
    assert gate.normalize_node_reports(reports) == expected


def test_atomic_json_round_trip(tmp_path: Path):
    target = tmp_path / "nested" / "value.json"
    gate._atomic_write_json(target, {"z": 1, "a": "é"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": "é", "z": 1}
    assert not list(target.parent.glob("*.tmp"))


def test_canonical_json_rejects_nonfinite_numbers():
    with pytest.raises(ValueError):
        gate._canonical_json_bytes({"wall": math.nan})


def test_uv_version_normalizes_distribution_suffix():
    assert (
        gate._normalize_uv_version("uv 0.11.30 (Homebrew 2026-07-20 aarch64-apple-darwin)\n")
        == "uv 0.11.30"
    )


def test_xdist_is_exactly_pinned_in_dev_scope_only():
    repo_root = Path(__file__).parents[1]
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pytest-xdist==3.8.0" in project["dependency-groups"]["dev"]
    assert all(
        not requirement.startswith("pytest-xdist")
        for requirement in project["project"]["dependencies"]
    )
    assert all(
        not requirement.startswith("pytest-xdist")
        for requirements in project["project"]["optional-dependencies"].values()
        for requirement in requirements
    )


def test_hazard_cohort_is_exact_unique_and_present():
    repo_root = Path(__file__).parents[1]
    cohort_path = repo_root / "tools" / "pytest_parallel_hazard_cohort.txt"
    paths, digest = gate._cohort_paths(cohort_path)
    assert paths == _EXPECTED_HAZARD_PATHS
    assert len(paths) == len(set(paths))
    assert all((repo_root / path).is_file() for path in paths)
    assert digest == gate._sha256_file(cohort_path)


def test_ci_full_suite_command_and_serial_fallback_are_exact():
    repo_root = Path(__file__).parents[1]
    workflow = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    instructions = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    serial = "uv run --frozen pytest tests/ -q --tb=short"
    selected = f"{serial} -n 2 --dist=loadfile --max-worker-restart=0"
    test_job = workflow.split("\n  test:\n", 1)[1].split("\n  security-scan:\n", 1)[0]
    test_job_header, test_job_steps = test_job.split("\n    steps:\n", 1)
    assert test_job_header.count("\n    timeout-minutes: 20") == 1
    run_tests_step = test_job_steps.split("      - name: Run tests\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    assert run_tests_step == f"        run: {selected}\n"
    assert "continue-on-error" not in test_job
    assert f"`{selected}`" in instructions
    assert f"`{serial}`" in instructions
    assert f"{selected}  # full CI gate" in readme
    assert f"{serial}  # serial diagnostic and compatibility fallback" in readme
    assert f"Before opening a PR, run `{selected}`" in readme


def test_repository_manifest_detects_file_mode_and_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repo / "link").symlink_to("tracked.txt")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt", "link"], cwd=repo, check=True)
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "value.txt").write_text("ignored\n", encoding="utf-8")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "value").write_text("excluded\n", encoding="utf-8")
    (repo / ".hypothesis").mkdir()
    (repo / ".hypothesis" / "constants").write_text("excluded\n", encoding="utf-8")

    manifest = gate.repository_manifest(repo)
    by_path = {entry["path"]: entry for entry in manifest["entries"]}
    assert set(by_path) == {
        ".gitignore",
        "ignored",
        "ignored/value.txt",
        "link",
        "tracked.txt",
        "untracked.txt",
    }
    assert by_path["link"]["entry_type"] == "symlink"
    assert by_path["link"]["target"] == "tracked.txt"
    assert by_path["tracked.txt"]["tracked"] is True
    assert by_path["untracked.txt"]["tracked"] is False


def test_inject_plugin_preserves_requested_command():
    requested = ["uv", "run", "--frozen", "pytest", "tests/", "-q"]
    assert gate._inject_plugin(requested, "pytest_parallel_gate") == [
        "uv", "run", "--frozen", "pytest", "-p", "pytest_parallel_gate", "tests/", "-q"
    ]


def test_run_rejects_repo_local_artifacts(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="outside"):
        gate.run_pytest(repo, repo / "artifacts", ["pytest", "--collect-only", "-p", "no:cacheprovider"])


def test_serial_integration_artifact_is_complete(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/repo.git"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "test_sample.py").write_text(
        "import pytest\n\n"
        "def test_pass():\n    assert True\n\n"
        "@pytest.mark.skip(reason='kept')\n"
        "def test_skip():\n    raise AssertionError\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Graphify Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm", "baseline"], cwd=repo, check=True)
    artifact = tmp_path / "artifact"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "test_sample.py",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(artifact / "basetemp"),
    ]
    result = gate.run_pytest(repo, artifact, command, kind="integration")
    assert result["passed"] is True
    assert result["counts"] == {"passed": 1, "skipped": 1}
    assert result["collection_order"] == ["test_sample.py::test_pass", "test_sample.py::test_skip"]
    assert result["manifest_stable"] is True
    assert result["pytest_plugin_entrypoints"] == gate._CANDIDATE_PYTEST_PLUGIN_ENTRYPOINTS
    assert result["plugin_names"] == sorted(
        [
            "pytest_parallel_gate",
            *(item["name"] for item in gate._CANDIDATE_PYTEST_PLUGIN_ENTRYPOINTS),
        ]
    )


def _manifest(tag: str) -> dict[str, object]:
    source_tag = "candidate-worktree" if tag in {"matrix", "candidate"} else tag
    contents = {
        ".github/workflows/ci.yml": f"ci-{tag}",
        "AGENTS.md": f"agents-{tag}",
        "README.md": f"readme-{tag}",
        "source.txt": source_tag,
    }
    if tag in {"matrix", "candidate"}:
        cohort_path = Path(gate.__file__).resolve().parents[1] / gate._HAZARD_COHORT_PATH
        contents[gate._HAZARD_COHORT_PATH] = cohort_path.read_text(encoding="utf-8")
    payload: dict[str, object] = {
        "repo_root": str(Path(gate.__file__).resolve().parents[1]),
        "enumeration": "filesystem walk plus tracked-missing reconciliation",
        "excluded_directory_names": sorted(gate._EXCLUDED_DIRECTORY_NAMES),
        "excluded_file_suffixes": list(gate._EXCLUDED_FILE_SUFFIXES),
        "entries": [
            {
                "path": path,
                "tracked": True,
                "index_mode": "100644",
                "entry_type": "file",
                "mode": "0644",
                "size": len(content),
                "sha256": gate._sha256_bytes(content.encode()),
            }
            for path, content in sorted(contents.items())
        ],
    }
    payload["manifest_sha256"] = gate._sha256_bytes(gate._canonical_json_bytes(payload))
    return payload


def _identity(status: list[str], *, head: str = "a" * 40) -> dict[str, object]:
    return {
        "head": head,
        "branch": "codex/pytest-parallel-gate",
        "status": status,
        "repository_fingerprint": "repository",
        "host_fingerprint": "host",
    }


def _command(
    artifact_root: Path,
    selections: list[str],
    *,
    collect: bool = False,
    workers: int | None = None,
    distribution: str = "loadfile",
) -> list[str]:
    command = [
        "uv",
        "run",
        "--frozen",
        "pytest",
        *selections,
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(artifact_root / "basetemp"),
    ]
    if collect:
        command.append("--collect-only")
    if workers is not None:
        command.extend(
            ["-n", str(workers), f"--dist={distribution}", "--max-worker-restart=0"]
        )
    return command


def _run_artifact(
    *,
    name: str,
    root: Path,
    evaluator_digest: str,
    kind: str,
    wall: float,
    start_offset: float,
    nodes: list[str],
    outcomes: dict[str, str],
    manifest: dict[str, object],
    identity: dict[str, object],
    command: list[str],
    preflight_sha256: str | None = None,
    cohort_paths: list[str] | None = None,
    cohort_sha256: str | None = None,
    candidate_versions: bool = True,
) -> dict[str, object]:
    started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=start_offset)
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    artifact_root = Path(command[command.index("--basetemp") + 1]).parent
    parallel = any(token == "-n" or token.startswith("-n") for token in command)
    child_versions = {
        "python": "3.14.3",
        "pytest": "9.0.3",
        "pytest_xdist": "3.8.0" if candidate_versions else None,
        "execnet": "2.1.2" if candidate_versions else None,
        "anyio": "4.13.0",
        "hypothesis": "6.153.0",
        "openai": "2.36.0",
        "pytest_cov": "7.1.0",
        "coverage": "7.14.0",
    }
    plugin_entrypoints = (
        gate._CANDIDATE_PYTEST_PLUGIN_ENTRYPOINTS
        if candidate_versions
        else gate._BASELINE_PYTEST_PLUGIN_ENTRYPOINTS
    )
    workers = gate._parallel_config(command)[0] if parallel else 0
    return {
        "schema": gate.RUN_SCHEMA,
        "evaluator_sha256": evaluator_digest,
        "kind": kind,
        "requested_argv": command,
        "executed_argv": gate._inject_plugin(command, "pytest_parallel_gate"),
        "repo_root": str(Path(gate.__file__).resolve().parents[1]),
        "artifact_root": str(artifact_root),
        "git_before": identity,
        "git_after": identity,
        "manifest_before": manifest,
        "manifest_after": manifest,
        "manifest_stable": True,
        "identity_stable": True,
        "environment": {
            key: (
                "1"
                if key == "PYTHONDONTWRITEBYTECODE"
                else ""
                if key == "PYTEST_PLUGINS"
                else None
            )
            for key in (*gate._SAFE_ENV_KEYS, "TMPDIR_sha256")
        }
        | {"GRAPHIFY_OUT_present": "no", "PYTHONPATH_policy": "evaluator-only"},
        "wrapper_versions": {},
        "child_versions": child_versions,
        "plugin_names": sorted(
            [
                "pytest_parallel_gate",
                *(entrypoint["name"] for entrypoint in plugin_entrypoints),
            ]
        ),
        "pytest_plugin_entrypoints": plugin_entrypoints,
        "uv_version": "uv 0.11.30",
        "started_at": started.isoformat(),
        "completed_at": (started + timedelta(seconds=wall)).isoformat(),
        "wall_seconds": wall,
        "user_seconds": wall / 2,
        "system_seconds": wall / 4,
        "timing_capability": "synthetic",
        "child_exit_code": 0,
        "timed_out": False,
        "timeout_seconds": 3000,
        "preflight_artifact_sha256": preflight_sha256,
        "cohort_sha256": cohort_sha256,
        "cohort_paths": cohort_paths or [],
        "collection_order": nodes,
        "collection_consistent": True,
        "worker_collections": {
            f"gw{index}": nodes for index in range(workers)
        },
        "duplicate_collection": False,
        "outcomes": outcomes,
        "counts": counts,
        "extra_reports": [],
        "worker_errors": [],
        "pytest_exitstatus": 0,
        "collect_only": kind.startswith("collect-"),
        "complete": True,
        "passed": True,
    }


def _write_ref(root: Path, name: str, payload: dict[str, object]) -> dict[str, str]:
    path = root / f"{name}.json"
    gate._atomic_write_json(path, payload)
    return {"path": path.name, "sha256": gate._sha256_file(path)}


def _local_gate_evidence(
    gate_name: str,
    candidate_manifest_sha256: str,
    *,
    candidate_head: str = "b" * 40,
) -> dict[str, object]:
    started = datetime(2026, 1, 2, tzinfo=timezone.utc)
    results: list[dict[str, object]] = []
    for index, argv in enumerate(gate._LOCAL_GATE_COMMANDS[gate_name]):
        output = f"{gate_name}-{index}-passed\n"
        results.append(
            {
                "argv": argv,
                "exit_code": 0,
                "conclusion": "success",
                "started_at": (started + timedelta(seconds=index * 2)).isoformat(),
                "completed_at": (started + timedelta(seconds=index * 2 + 1)).isoformat(),
                "output": output,
                "output_sha256": gate._sha256_bytes(output.encode()),
            }
        )
    return {
        "schema": gate.LOCAL_GATE_EVIDENCE_SCHEMA,
        "gate": gate_name,
        "status": "pass",
        "candidate_head": candidate_head,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "results": results,
    }


def _review_evidence(candidate_manifest_sha256: str) -> dict[str, object]:
    code_report = "CODE REVIEW REPORT\nRECOMMENDATION: APPROVE\n"
    architecture_report = "ARCHITECTURE REVIEW\nArchitectural Status: CLEAR\n"
    return {
        "schema": gate.REVIEW_EVIDENCE_SCHEMA,
        "gate": "independent_review",
        "status": "pass",
        "candidate_head": "b" * 40,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "reviewed_paths": gate._REVIEWED_PATHS,
        "code_reviewer": {
            "lane_id": "code-review-lane",
            "agent_type": "code-reviewer",
            "verdict": "APPROVE",
            "findings": [],
            "report": code_report,
            "report_sha256": gate._sha256_bytes(code_report.encode()),
        },
        "architect": {
            "lane_id": "architecture-lane",
            "agent_type": "architect",
            "verdict": "CLEAR",
            "findings": [],
            "report": architecture_report,
            "report_sha256": gate._sha256_bytes(architecture_report.encode()),
        },
    }


def _hosted_ci_evidence(candidate_manifest_sha256: str) -> dict[str, object]:
    run_started = datetime(2026, 1, 2, tzinfo=timezone.utc)
    run_completed = run_started + timedelta(seconds=600)
    repository_url = f"https://github.com/{gate._HOSTED_CI_REPOSITORY}"
    run_id = 42
    jobs = []
    for index, name in enumerate(gate._HOSTED_CI_JOB_NAMES, start=1):
        job_started = run_started + timedelta(seconds=5)
        duration = 590 if name == "test (3.14)" else 120
        steps = (
            [
                {
                    "number": 1,
                    "name": "Install dependencies",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": (job_started + timedelta(seconds=5)).isoformat(),
                    "completed_at": (job_started + timedelta(seconds=60)).isoformat(),
                },
                {
                    "number": 2,
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": (job_started + timedelta(seconds=70)).isoformat(),
                    "completed_at": (job_started + timedelta(seconds=570)).isoformat(),
                },
            ]
            if name == "test (3.14)"
            else []
        )
        jobs.append(
            {
                "database_id": index,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "started_at": job_started.isoformat(),
                "completed_at": (job_started + timedelta(seconds=duration)).isoformat(),
                "url": f"{repository_url}/actions/runs/{run_id}/job/{index}",
                "runner_os": "Linux",
                "runner_arch": "X64",
                "runner_image": "ubuntu-24.04",
                "steps": steps,
            }
        )
    return {
        "schema": gate.HOSTED_CI_EVIDENCE_SCHEMA,
        "gate": "hosted_ci",
        "status": "pass",
        "candidate_head": "b" * 40,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "repository": gate._HOSTED_CI_REPOSITORY,
        "workflow_name": gate._HOSTED_CI_WORKFLOW,
        "workflow_path": gate._HOSTED_CI_WORKFLOW_PATH,
        "event": "pull_request",
        "head_branch": "codex/pytest-parallel-gate",
        "pull_request_number": 84,
        "pull_request_url": f"{repository_url}/pull/84",
        "pull_request_state": "OPEN",
        "pull_request_is_draft": True,
        "pull_request_base_ref": "workspace/v1",
        "pull_request_head_ref": "codex/pytest-parallel-gate",
        "pull_request_head_sha": "b" * 40,
        "run_pull_request_number": 84,
        "python_version": "3.14",
        "run_id": run_id,
        "run_attempt": 1,
        "run_url": f"{repository_url}/actions/runs/{run_id}",
        "run_inventory_query": [
            "gh",
            "run",
            "list",
            "--repo",
            gate._HOSTED_CI_REPOSITORY,
            "--workflow",
            gate._HOSTED_CI_WORKFLOW,
            "--commit",
            "b" * 40,
        ],
        "workflow_runs_for_head": [
            {
                "run_id": run_id,
                "run_attempt": 1,
                "event": "pull_request",
                "workflow_name": gate._HOSTED_CI_WORKFLOW,
                "head_sha": "b" * 40,
                "status": "completed",
                "conclusion": "success",
                "url": f"{repository_url}/actions/runs/{run_id}",
                "created_at": run_started.isoformat(),
                "updated_at": run_completed.isoformat(),
            }
        ],
        "started_at": run_started.isoformat(),
        "completed_at": run_completed.isoformat(),
        "conclusion": "success",
        "jobs": jobs,
    }


def _hosted_variance_evidence() -> dict[str, object]:
    repository_url = f"https://github.com/{gate._HOSTED_CI_REPOSITORY}"
    run_id = gate._PR81_RUN_ID
    job_id = gate._PR81_JOB_ID
    return {
        "schema": gate.HOSTED_VARIANCE_EVIDENCE_SCHEMA,
        "repository": gate._HOSTED_CI_REPOSITORY,
        "workflow_path": gate._HOSTED_CI_WORKFLOW_PATH,
        "job_name": "test (3.14)",
        "python_version": "3.14",
        "runner_os": "Linux",
        "runner_arch": "X64",
        "runner_image": gate._PR81_RUNNER_IMAGE,
        "source_pull_request_number": 81,
        "source_pull_request_url": f"{repository_url}/pull/81",
        "source_pull_request_state": "MERGED",
        "source_pull_request_base_ref": "workspace/v1",
        "source_pull_request_head_ref": gate._PR81_HEAD_REF,
        "source_pull_request_head_sha": gate._PR81_HEAD_SHA,
        "source_run_id": run_id,
        "source_run_url": f"{repository_url}/actions/runs/{run_id}",
        "source_run_attempt": 1,
        "source_run_event": "pull_request",
        "source_run_workflow_name": gate._HOSTED_CI_WORKFLOW,
        "source_run_status": "completed",
        "source_run_conclusion": "success",
        "source_run_pull_request_number": 81,
        "source_job_id": job_id,
        "source_job_url": f"{repository_url}/actions/runs/{run_id}/job/{job_id}",
        "source_job_status": "completed",
        "source_job_conclusion": "success",
        "source_head_sha": gate._PR81_HEAD_SHA,
        "source_job_started_at": gate._PR81_JOB_STARTED_AT,
        "source_job_completed_at": gate._PR81_JOB_COMPLETED_AT,
        "source_setup_started_at": gate._PR81_SETUP_STARTED_AT,
        "source_setup_completed_at": gate._PR81_SETUP_COMPLETED_AT,
        "source_test_step_started_at": gate._PR81_TEST_STARTED_AT,
        "source_test_step_completed_at": gate._PR81_TEST_COMPLETED_AT,
        "variance_explanation": (
            "Candidate and PR #81 both used ubuntu-24.04 on Linux X64; "
            "whole-job timing includes setup and Run tests step variance."
        ),
    }


def _paired_run(
    *,
    root: Path,
    name: str,
    evaluator_digest: str,
    kind: str,
    wall: float,
    start_offset: float,
    nodes: list[str],
    outcomes: dict[str, str],
    manifest: dict[str, object],
    identity: dict[str, object],
    command: list[str],
    cohort_paths: list[str] | None = None,
    cohort_sha256: str | None = None,
    candidate_versions: bool = True,
) -> tuple[dict[str, str], dict[str, str]]:
    preflight_kind = "collect-hazard" if kind == "hazard" else "collect-full"
    artifact_root = root / f"{name}-preflight-root"
    preflight_command = _command(
        artifact_root,
        cohort_paths or ["tests/"],
        collect=True,
    )
    preflight = _run_artifact(
        name=f"{name}-preflight",
        root=root,
        evaluator_digest=evaluator_digest,
        kind=preflight_kind,
        wall=1,
        start_offset=start_offset - 2,
        nodes=nodes,
        outcomes={},
        manifest=manifest,
        identity=identity,
        command=preflight_command,
        cohort_paths=cohort_paths,
        cohort_sha256=cohort_sha256,
        candidate_versions=candidate_versions,
    )
    preflight_ref = _write_ref(root, f"{name}-preflight", preflight)
    run = _run_artifact(
        name=name,
        root=root,
        evaluator_digest=evaluator_digest,
        kind=kind,
        wall=wall,
        start_offset=start_offset,
        nodes=nodes,
        outcomes=outcomes,
        manifest=manifest,
        identity=identity,
        command=command,
        preflight_sha256=preflight_ref["sha256"],
        cohort_paths=cohort_paths,
        cohort_sha256=cohort_sha256,
        candidate_versions=candidate_versions,
    )
    return preflight_ref, _write_ref(root, name, run)


def test_verify_evidence_enforces_full_bound_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evaluator_digest = gate._sha256_file(Path(gate.__file__))
    cohort_file = Path(gate.__file__).resolve().parents[1] / gate._HAZARD_COHORT_PATH
    cohort_paths, cohort_sha256 = gate._cohort_paths(cohort_file)
    hazard_nodes = [f"{path}::test_synthetic_hazard" for path in cohort_paths]
    baseline_nodes = [
        *hazard_nodes,
        *[
            f"tests/test_base.py::test_{index}"
            for index in range(5749 - len(hazard_nodes))
        ],
    ]
    added = "tests/test_pytest_parallel_gate.py::test_added"
    final_nodes = [*baseline_nodes, added]
    baseline_outcomes = {node: "passed" for node in baseline_nodes}
    final_outcomes = {**baseline_outcomes, added: "passed"}
    baseline_manifest = _manifest("baseline")
    matrix_manifest = _manifest("matrix")
    candidate_manifest = _manifest("candidate")
    clean_identity = _identity([])
    matrix_identity = _identity(["?? tests/test_pytest_parallel_gate.py"])
    candidate_identity = _identity([], head="b" * 40)

    preflight_refs: list[dict[str, str]] = []
    preflight, baseline_ref = _paired_run(
        root=tmp_path,
        name="baseline",
        evaluator_digest=evaluator_digest,
        kind="full",
        wall=100,
        start_offset=1000,
        nodes=baseline_nodes,
        outcomes=baseline_outcomes,
        manifest=baseline_manifest,
        identity=clean_identity,
        command=_command(tmp_path / "baseline-root", ["tests/"]),
        candidate_versions=False,
    )
    preflight_refs.append(preflight)

    matrix_refs: list[dict[str, str]] = []
    for name, workers, wall, offset in (
        ("matrix2", 2, 60, 2000),
        ("matrix4", 4, 50, 3000),
    ):
        preflight, reference = _paired_run(
            root=tmp_path,
            name=name,
            evaluator_digest=evaluator_digest,
            kind="full",
            wall=wall,
            start_offset=offset,
            nodes=final_nodes,
            outcomes=final_outcomes,
            manifest=matrix_manifest,
            identity=matrix_identity,
            command=_command(tmp_path / f"{name}-root", ["tests/"], workers=workers),
        )
        preflight_refs.append(preflight)
        matrix_refs.append(reference)

    preflight, final_serial_ref = _paired_run(
        root=tmp_path,
        name="final-serial",
        evaluator_digest=evaluator_digest,
        kind="full",
        wall=110,
        start_offset=4000,
        nodes=final_nodes,
        outcomes=final_outcomes,
        manifest=candidate_manifest,
        identity=candidate_identity,
        command=_command(tmp_path / "final-serial-root", ["tests/"]),
    )
    preflight_refs.append(preflight)

    winner_refs: list[dict[str, str]] = []
    for name, wall, offset in (("winner1", 60, 5000), ("winner2", 62, 6000)):
        preflight, reference = _paired_run(
            root=tmp_path,
            name=name,
            evaluator_digest=evaluator_digest,
            kind="full",
            wall=wall,
            start_offset=offset,
            nodes=final_nodes,
            outcomes=final_outcomes,
            manifest=candidate_manifest,
            identity=candidate_identity,
            command=_command(tmp_path / f"{name}-root", ["tests/"], workers=2),
        )
        preflight_refs.append(preflight)
        winner_refs.append(reference)

    hazard_refs: list[dict[str, str]] = []
    for index, offset in enumerate((7000, 8000, 9000), start=1):
        name = f"hazard{index}"
        preflight, reference = _paired_run(
            root=tmp_path,
            name=name,
            evaluator_digest=evaluator_digest,
            kind="hazard",
            wall=10,
            start_offset=offset,
            nodes=hazard_nodes,
            outcomes={node: "passed" for node in hazard_nodes},
            manifest=candidate_manifest,
            identity=candidate_identity,
            command=_command(
                tmp_path / f"{name}-root", cohort_paths, workers=2
            ),
            cohort_paths=cohort_paths,
            cohort_sha256=cohort_sha256,
        )
        preflight_refs.append(preflight)
        hazard_refs.append(reference)

    gate_refs: dict[str, dict[str, str]] = {}
    gate_evidence_refs: dict[str, dict[str, str]] = {}
    for gate_name in (
        "lock",
        "focused",
        "static",
        "graph_refresh",
        "independent_review",
        "hosted_ci",
    ):
        if gate_name in gate._LOCAL_GATE_COMMANDS:
            gate_evidence = _local_gate_evidence(
                gate_name, str(candidate_manifest["manifest_sha256"])
            )
        elif gate_name == "independent_review":
            gate_evidence = _review_evidence(str(candidate_manifest["manifest_sha256"]))
        else:
            gate_evidence = _hosted_ci_evidence(
                str(candidate_manifest["manifest_sha256"])
            )
        evidence_ref = _write_ref(tmp_path, f"{gate_name}-evidence", gate_evidence)
        gate_evidence_refs[gate_name] = evidence_ref
        receipt = {
            "schema": gate.GATE_RECEIPT_SCHEMA,
            "gate": gate_name,
            "status": "pass",
            "candidate_head": "b" * 40,
            "candidate_manifest_sha256": candidate_manifest["manifest_sha256"],
            "evidence_artifacts": [evidence_ref],
        }
        gate_refs[gate_name] = _write_ref(tmp_path, f"{gate_name}-receipt", receipt)

    evidence = {
        "schema": gate.EVIDENCE_SCHEMA,
        "evaluator_sha256": evaluator_digest,
        "baseline": baseline_ref,
        "final_serial": final_serial_ref,
        "matrix_runs": matrix_refs,
        "winner_runs": winner_refs,
        "hazard_runs": hazard_refs,
        "preflight_runs": preflight_refs,
        "trial_sequence_sha256": [
            digest
            for preflight_ref, run_ref in zip(
                preflight_refs,
                [baseline_ref, *matrix_refs, final_serial_ref, *winner_refs, *hazard_refs],
                strict=True,
            )
            for digest in (preflight_ref["sha256"], run_ref["sha256"])
        ],
        "allowed_added_node_ids": [added],
        "allowed_added_test_files": ["tests/test_pytest_parallel_gate.py"],
        "expected_base_head": "a" * 40,
        "expected_matrix_head": "a" * 40,
        "expected_candidate_head": "b" * 40,
        "expected_repository_fingerprint": "repository",
        "expected_host_fingerprint": "host",
        "expected_matrix_status": ["?? tests/test_pytest_parallel_gate.py"],
        "expected_candidate_status": [],
        "expected_environment": _run_artifact(
            name="environment",
            root=tmp_path,
            evaluator_digest=evaluator_digest,
            kind="full",
            wall=1,
            start_offset=1,
            nodes=["tests/test_base.py::test_0"],
            outcomes={"tests/test_base.py::test_0": "passed"},
            manifest=candidate_manifest,
            identity=candidate_identity,
            command=_command(tmp_path / "environment-root", ["tests/"]),
        )["environment"],
        "expected_baseline_versions": {
            "python": "3.14.3",
            "pytest": "9.0.3",
            "pytest_xdist": None,
            "execnet": None,
            "anyio": "4.13.0",
            "hypothesis": "6.153.0",
            "openai": "2.36.0",
            "pytest_cov": "7.1.0",
            "coverage": "7.14.0",
        },
        "expected_candidate_versions": {
            "python": "3.14.3",
            "pytest": "9.0.3",
            "pytest_xdist": "3.8.0",
            "execnet": "2.1.2",
            "anyio": "4.13.0",
            "hypothesis": "6.153.0",
            "openai": "2.36.0",
            "pytest_cov": "7.1.0",
            "coverage": "7.14.0",
        },
        "expected_baseline_pytest_plugin_entrypoints": gate._BASELINE_PYTEST_PLUGIN_ENTRYPOINTS,
        "expected_candidate_pytest_plugin_entrypoints": gate._CANDIDATE_PYTEST_PLUGIN_ENTRYPOINTS,
        "expected_uv_version": "uv 0.11.30",
        "baseline_manifest_sha256": baseline_manifest["manifest_sha256"],
        "matrix_manifest_sha256": matrix_manifest["manifest_sha256"],
        "candidate_manifest_sha256": candidate_manifest["manifest_sha256"],
        "allowed_snapshot_transition_files": gate._ALLOWED_SNAPSHOT_TRANSITION_FILES,
        "gates": gate_refs,
        "hosted": {
            "jobs_artifact": gate_evidence_refs["hosted_ci"],
        },
    }
    evidence_path = tmp_path / "evidence.json"
    gate._atomic_write_json(evidence_path, evidence)
    monkeypatch.setattr(gate, "_verify_live_github_hosted", lambda artifact: None)
    result = gate.verify_evidence(evidence_path)
    assert result["status"] == "pass"
    assert result["added_nodes"] == 1
    assert result["improvement_vs_baseline"] == pytest.approx(0.39)


def test_verify_evidence_rejects_per_node_swap():
    baseline = {"collection_order": ["test_a", "test_b"], "outcomes": {"test_a": "passed", "test_b": "skipped"}}
    candidate = {"collection_order": ["test_a", "test_b"], "outcomes": {"test_a": "skipped", "test_b": "passed"}}
    with pytest.raises(ValueError, match="test_a"):
        gate._assert_parity(baseline, candidate, set())


def test_verify_evidence_rejects_baseline_collection_reordering():
    baseline = {
        "collection_order": ["test_a", "test_b"],
        "outcomes": {"test_a": "passed", "test_b": "passed"},
    }
    candidate = {
        "collection_order": ["test_b", "test_added", "test_a"],
        "outcomes": {
            "test_a": "passed",
            "test_b": "passed",
            "test_added": "passed",
        },
    }
    with pytest.raises(ValueError, match="collection order"):
        gate._assert_parity(baseline, candidate, {"test_added"})


def test_committed_hazard_cohort_is_bound_to_candidate_manifest():
    repo_root = Path(gate.__file__).resolve().parents[1]
    manifest = _manifest("candidate")
    paths, digest = gate._committed_hazard_cohort(repo_root, manifest)
    assert paths == _EXPECTED_HAZARD_PATHS
    assert digest == gate._sha256_file(repo_root / gate._HAZARD_COHORT_PATH)

    entries = manifest["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        if entry["path"] == gate._HAZARD_COHORT_PATH:
            entry["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="committed candidate"):
        gate._committed_hazard_cohort(repo_root, manifest)


def test_hazard_outcomes_must_repeat_and_match_full_suite():
    nodeid = "tests/test_hazard.py::test_one"
    winner = {"outcomes": {nodeid: "passed"}}
    hazards = [
        {"collection_order": [nodeid], "outcomes": {nodeid: "passed"}},
        {"collection_order": [nodeid], "outcomes": {nodeid: "passed"}},
        {"collection_order": [nodeid], "outcomes": {nodeid: "skipped"}},
    ]
    with pytest.raises(ValueError, match="hazard outcomes"):
        gate._assert_hazard_outcome_parity(hazards, winner)


def test_gate_evidence_rejects_dummy_artifacts_and_wrong_commands():
    manifest_sha = "c" * 64
    with pytest.raises(ValueError, match="schema"):
        gate._validate_local_gate_evidence(
            {"exit_code": 0}, "lock", "b" * 40, manifest_sha
        )

    evidence = _local_gate_evidence("lock", manifest_sha)
    results = evidence["results"]
    assert isinstance(results, list)
    assert isinstance(results[0], dict)
    results[0]["argv"] = ["true"]
    with pytest.raises(ValueError, match="did not pass exactly"):
        gate._validate_local_gate_evidence(
            evidence, "lock", "b" * 40, manifest_sha
        )


def test_review_evidence_requires_two_distinct_clean_lanes():
    manifest_sha = "c" * 64
    evidence = _review_evidence(manifest_sha)
    architect = evidence["architect"]
    assert isinstance(architect, dict)
    architect["lane_id"] = "code-review-lane"
    with pytest.raises(ValueError, match="not distinct"):
        gate._validate_review_evidence(evidence, "b" * 40, manifest_sha)


def test_hosted_ci_derives_duration_and_rejects_reruns():
    manifest_sha = "c" * 64
    evidence = _hosted_ci_evidence(manifest_sha)
    assert gate._validate_hosted_ci_evidence(
        evidence, "b" * 40, manifest_sha
    ) == pytest.approx(590)
    evidence["run_attempt"] = 2
    with pytest.raises(ValueError, match="run_attempt"):
        gate._validate_hosted_ci_evidence(evidence, "b" * 40, manifest_sha)


def test_hosted_ci_rejects_wrong_repository_urls():
    manifest_sha = "c" * 64
    evidence = _hosted_ci_evidence(manifest_sha)
    assert gate._validate_hosted_ci_evidence(
        evidence, "b" * 40, manifest_sha
    ) == pytest.approx(590)
    evidence["run_url"] = "https://github.com/attacker/dummy/actions/runs/42"
    with pytest.raises(ValueError, match="run_url"):
        gate._validate_hosted_ci_evidence(evidence, "b" * 40, manifest_sha)


def test_hosted_ci_rejects_wrong_pr_delivery_metadata():
    manifest_sha = "c" * 64
    evidence = _hosted_ci_evidence(manifest_sha)
    assert gate._validate_hosted_ci_evidence(
        evidence, "b" * 40, manifest_sha
    ) == pytest.approx(590)
    evidence["pull_request_base_ref"] = "main"
    with pytest.raises(ValueError, match="pull_request_base_ref"):
        gate._validate_hosted_ci_evidence(evidence, "b" * 40, manifest_sha)


def test_live_github_rejects_coordinated_candidate_identity_substitution():
    evidence = _hosted_ci_evidence("c" * 64)
    run_id = evidence["run_id"]
    pull_number = evidence["pull_request_number"]
    assert isinstance(run_id, int)
    assert isinstance(pull_number, int)
    jobs = evidence["jobs"]
    assert isinstance(jobs, list)

    def api_loader(endpoint: str) -> dict[str, object]:
        if "/pulls/" in endpoint:
            return {
                "number": pull_number,
                "html_url": evidence["pull_request_url"],
                "state": "open",
                "draft": True,
                "base": {"ref": "workspace/v1"},
                "head": {
                    "ref": "codex/pytest-parallel-gate",
                    "sha": "b" * 40,
                },
            }
        if "actions/runs?" in endpoint:
            inventory = evidence["workflow_runs_for_head"]
            assert isinstance(inventory, list)
            item = inventory[0]
            assert isinstance(item, dict)
            return {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": item["run_id"],
                        "run_attempt": item["run_attempt"],
                        "event": item["event"],
                        "name": item["workflow_name"],
                        "path": gate._HOSTED_CI_WORKFLOW_PATH,
                        "head_sha": item["head_sha"],
                        "status": item["status"],
                        "conclusion": item["conclusion"],
                        "html_url": item["url"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"],
                    }
                ]
            }
        if endpoint.endswith("/jobs?per_page=100"):
            return {
                "total_count": len(jobs),
                "jobs": [
                    {
                        "id": job["database_id"],
                        "name": job["name"],
                        "status": job["status"],
                        "conclusion": job["conclusion"],
                        "started_at": job["started_at"],
                        "completed_at": job["completed_at"],
                        "html_url": job["url"],
                        "labels": ["ubuntu-latest"],
                        "steps": job["steps"],
                    }
                    for job in jobs
                ],
            }
        return {
            "id": run_id,
            "run_attempt": 1,
            "event": "pull_request",
            "name": gate._HOSTED_CI_WORKFLOW,
            "path": gate._HOSTED_CI_WORKFLOW_PATH,
            "status": "completed",
            "conclusion": "success",
            "head_branch": "codex/pytest-parallel-gate",
            "head_sha": "b" * 40,
            "html_url": evidence["run_url"],
            "created_at": evidence["started_at"],
            "updated_at": evidence["completed_at"],
            "pull_requests": [
                {
                    "number": pull_number,
                    "url": (
                        "https://api.github.com/repos/"
                        f"{gate._HOSTED_CI_REPOSITORY}/pulls/{pull_number}"
                    ),
                    "head": {
                        "ref": "codex/pytest-parallel-gate",
                        "sha": "b" * 40,
                        "repo": {
                            "url": (
                                "https://api.github.com/repos/"
                                f"{gate._HOSTED_CI_REPOSITORY}"
                            )
                        },
                    },
                    "base": {
                        "ref": "workspace/v1",
                        "repo": {
                            "url": (
                                "https://api.github.com/repos/"
                                f"{gate._HOSTED_CI_REPOSITORY}"
                            )
                        },
                    },
                }
            ],
        }

    gate._verify_live_github_hosted(
        evidence,
        api_loader=api_loader,
        job_log_loader=lambda run, job: "Image: ubuntu-24.04\n",
    )

    def wrong_pr_loader(endpoint: str) -> dict[str, object]:
        result = api_loader(endpoint)
        if (
            endpoint.endswith(f"/actions/runs/{run_id}")
            and "?" not in endpoint
        ):
            result = json.loads(json.dumps(result))
            result["pull_requests"][0]["number"] = 999
            result["pull_requests"][0]["url"] = (
                "https://api.github.com/repos/"
                f"{gate._HOSTED_CI_REPOSITORY}/pulls/999"
            )
        return result

    with pytest.raises(ValueError, match="another pull request"):
        gate._verify_live_github_hosted(
            evidence,
            api_loader=wrong_pr_loader,
            job_log_loader=lambda run, job: "Image: ubuntu-24.04\n",
        )

    def paginated_loader(endpoint: str) -> dict[str, object]:
        result = api_loader(endpoint)
        if "actions/runs?" in endpoint:
            result = json.loads(json.dumps(result))
            result["total_count"] = 101
        return result

    with pytest.raises(ValueError, match="inventory is missing"):
        gate._verify_live_github_hosted(
            evidence,
            api_loader=paginated_loader,
            job_log_loader=lambda run, job: "Image: ubuntu-24.04\n",
        )

    substituted = json.loads(json.dumps(evidence))
    substituted["pull_request_number"] = 999
    substituted["pull_request_url"] = (
        f"https://github.com/{gate._HOSTED_CI_REPOSITORY}/pull/999"
    )
    substituted["run_pull_request_number"] = 999
    substituted["run_id"] = 999999
    substituted["run_url"] = (
        f"https://github.com/{gate._HOSTED_CI_REPOSITORY}/actions/runs/999999"
    )
    substituted["workflow_runs_for_head"][0]["run_id"] = 999999
    substituted["workflow_runs_for_head"][0]["url"] = substituted["run_url"]
    for job in substituted["jobs"]:
        job["url"] = f"{substituted['run_url']}/job/{job['database_id']}"
    gate._validate_hosted_ci_evidence(substituted, "b" * 40, "c" * 64)
    with pytest.raises(ValueError, match="live GitHub"):
        gate._verify_live_github_hosted(
            substituted,
            api_loader=api_loader,
            job_log_loader=lambda run, job: "Image: ubuntu-24.04\n",
        )


def test_hosted_variance_accepts_exact_644_second_boundary(tmp_path: Path):
    reference = _write_ref(tmp_path, "variance", _hosted_variance_evidence())
    manifest_path = tmp_path / "evidence.json"
    gate._atomic_write_json(manifest_path, {})
    hosted = _hosted_ci_evidence("c" * 64)
    gate._validate_hosted_variance_evidence(manifest_path, reference, hosted, 644)
    with pytest.raises(ValueError, match="hosted_seconds"):
        gate._validate_hosted_variance_evidence(
            manifest_path, reference, hosted, 644.01
        )


def test_hosted_variance_rejects_unsuccessful_source_job(tmp_path: Path):
    variance = _hosted_variance_evidence()
    valid_reference = _write_ref(tmp_path, "variance-valid", variance)
    manifest_path = tmp_path / "evidence.json"
    gate._atomic_write_json(manifest_path, {})
    gate._validate_hosted_variance_evidence(
        manifest_path,
        valid_reference,
        _hosted_ci_evidence("c" * 64),
        640,
    )
    variance["source_job_conclusion"] = "failure"
    reference = _write_ref(tmp_path, "variance-failed", variance)
    with pytest.raises(ValueError, match="source_job_conclusion"):
        gate._validate_hosted_variance_evidence(
            manifest_path,
            reference,
            _hosted_ci_evidence("c" * 64),
            640,
        )


def test_hosted_variance_rejects_coordinated_source_identity_substitution(
    tmp_path: Path,
):
    variance = _hosted_variance_evidence()
    valid_reference = _write_ref(tmp_path, "variance-valid", variance)
    manifest_path = tmp_path / "evidence.json"
    gate._atomic_write_json(manifest_path, {})
    gate._validate_hosted_variance_evidence(
        manifest_path,
        valid_reference,
        _hosted_ci_evidence("c" * 64),
        640,
    )
    variance["source_run_id"] = 999999
    variance["source_run_url"] = (
        f"https://github.com/{gate._HOSTED_CI_REPOSITORY}/actions/runs/999999"
    )
    variance["source_job_id"] = 888888
    variance["source_job_url"] = f"{variance['source_run_url']}/job/888888"
    variance["source_head_sha"] = "9" * 40
    variance["source_pull_request_head_sha"] = "9" * 40
    reference = _write_ref(tmp_path, "variance-substituted", variance)
    with pytest.raises(ValueError, match="source_run_id"):
        gate._validate_hosted_variance_evidence(
            manifest_path,
            reference,
            _hosted_ci_evidence("c" * 64),
            640,
        )


def test_final_candidate_status_must_be_exactly_clean():
    gate._require_clean_candidate_status([])
    with pytest.raises(ValueError, match="exactly clean"):
        gate._require_clean_candidate_status(["M  tools/pytest_parallel_gate.py"])


def test_canonical_command_rejects_deselection(tmp_path: Path):
    command = _command(tmp_path, ["tests/"])
    command.extend(["-k", "identical"])
    with pytest.raises(ValueError, match="forbidden"):
        gate._validate_measurement_command(
            command,
            tmp_path,
            kind="full",
            cohort_paths=[],
        )


def test_serial_control_rejects_xdist_settings(tmp_path: Path):
    with pytest.raises(ValueError, match="serial control"):
        gate._assert_serial_command(_command(tmp_path, ["tests/"], workers=2))


def test_added_nodes_are_bound_to_changed_test_files():
    node = "tests/test_pytest_parallel_gate.py::test_added"
    gate._assert_added_node_files(
        {node},
        ["tests/test_pytest_parallel_gate.py"],
        ["?? tests/test_pytest_parallel_gate.py"],
    )
    with pytest.raises(ValueError, match="in-scope"):
        gate._assert_added_node_files(
            {"tests/test_unrelated.py::test_added"},
            ["tests/test_unrelated.py"],
            ["?? docs/unrelated.md"],
        )


def test_snapshot_transition_rejects_source_byte_drift():
    matrix = _manifest("matrix")
    candidate = _manifest("candidate")
    entries = candidate["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        if entry["path"] == "source.txt":
            entry["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="source/test bytes"):
        gate._assert_snapshot_transition(
            matrix,
            candidate,
            gate._ALLOWED_SNAPSHOT_TRANSITION_FILES,
        )


def test_run_artifact_requires_xdist_lifecycle_fields(tmp_path: Path):
    evaluator_digest = gate._sha256_file(Path(gate.__file__))
    artifact = _run_artifact(
        name="parallel",
        root=tmp_path,
        evaluator_digest=evaluator_digest,
        kind="full",
        wall=10,
        start_offset=10,
        nodes=["tests/test_sample.py::test_pass"],
        outcomes={"tests/test_sample.py::test_pass": "passed"},
        manifest=_manifest("candidate"),
        identity=_identity([]),
        command=_command(tmp_path / "parallel-root", ["tests/"], workers=2),
        preflight_sha256="0" * 64,
    )
    artifact.pop("worker_errors")
    with pytest.raises(ValueError, match="required schema fields"):
        gate._validate_run_artifact(artifact, evaluator_digest)


def test_run_artifact_rejects_unknown_normalized_outcome(tmp_path: Path):
    evaluator_digest = gate._sha256_file(Path(gate.__file__))
    nodeid = "tests/test_sample.py::test_pass"
    artifact = _run_artifact(
        name="serial",
        root=tmp_path,
        evaluator_digest=evaluator_digest,
        kind="full",
        wall=10,
        start_offset=10,
        nodes=[nodeid],
        outcomes={nodeid: "mystery"},
        manifest=_manifest("candidate"),
        identity=_identity([]),
        command=_command(tmp_path / "serial-root", ["tests/"]),
        preflight_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="normalization vocabulary"):
        gate._validate_run_artifact(artifact, evaluator_digest)


def test_run_rejects_ambient_pytest_plugin_controls(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PYTEST_PLUGINS", "ambient_plugin")
    with pytest.raises(ValueError, match="ambient PYTEST_PLUGINS"):
        gate.run_pytest(repo, tmp_path / "artifact", [sys.executable, "-m", "pytest"])


def test_run_rejects_ambient_pythonpath(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PYTHONPATH", "/ambient/import/path")
    with pytest.raises(ValueError, match="ambient PYTHONPATH"):
        gate.run_pytest(repo, tmp_path / "artifact", [sys.executable, "-m", "pytest"])


def _committed_test_repo(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/repo.git"],
        cwd=repo,
        check=True,
    )
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "test_sample.py").write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Graphify Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def test_nested_pytest_drops_outer_xdist_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _committed_test_repo(tmp_path, "def test_pass():\n    assert True\n")
    for variable in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.setenv(variable, "outer-xdist")

    result = gate.run_pytest(
        repo,
        tmp_path / "artifact",
        [sys.executable, "-m", "pytest", "test_sample.py", "-q"],
        kind="integration",
    )

    assert result["passed"] is True


def test_missing_plugin_artifact_is_fail_closed(tmp_path: Path, monkeypatch):
    repo = _committed_test_repo(tmp_path, "def test_pass():\n    assert True\n")
    artifact = tmp_path / "artifact"
    command = [sys.executable, "-m", "pytest", "test_sample.py", "-q"]
    monkeypatch.setattr(gate, "_inject_plugin", lambda requested, plugin: list(requested))
    result = gate.run_pytest(repo, artifact, command, kind="integration")
    assert result["passed"] is False
    assert result["complete"] is False


def test_windows_job_abi_layout() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    expected_basic_size = 64 if pointer_size == 8 else 48
    expected_extended_size = 144 if pointer_size == 8 else 112

    assert ctypes.sizeof(gate._WindowsJobBasicLimitInformation) == expected_basic_size
    assert ctypes.sizeof(gate._WindowsJobExtendedLimitInformation) == expected_extended_size
    assert gate._WindowsJobBasicLimitInformation.LimitFlags.offset == 16


def test_windows_job_configuration_and_assignment_own_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Kernel32:
        def CreateJobObjectW(self, attributes: object, name: object) -> int:
            assert attributes is None
            assert name is None
            events.append("create-job")
            return 100

        def SetInformationJobObject(
            self,
            handle: int,
            information_class: int,
            information: Any,
            size: int,
        ) -> int:
            limits = ctypes.cast(
                information,
                ctypes.POINTER(gate._WindowsJobExtendedLimitInformation),
            ).contents
            assert handle == 100
            assert information_class == 9
            assert size == ctypes.sizeof(gate._WindowsJobExtendedLimitInformation)
            assert limits.BasicLimitInformation.LimitFlags == 0x00002000
            events.append("configure-job")
            return 1

        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            assert access == 0x0101
            assert inherit is False
            assert pid == 4242
            events.append("open-process")
            return 200

        def AssignProcessToJobObject(self, job_handle: int, process_handle: int) -> int:
            assert (job_handle, process_handle) == (100, 200)
            events.append("assign-process")
            return 1

        def CloseHandle(self, handle: int) -> int:
            events.append(("close", handle))
            return 1

    kernel32 = Kernel32()
    monkeypatch.setattr(gate, "_windows_kernel32", lambda: kernel32)

    job = gate._create_windows_job()
    gate._assign_windows_process(job, 4242)
    gate._close_windows_job(job)

    assert events == [
        "create-job",
        "configure-job",
        "open-process",
        "assign-process",
        ("close", 200),
        ("close", 100),
    ]


def test_windows_timeout_terminates_assigned_job_without_launcher_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    requested = ["pytest", "tests/", "-q"]
    job = object()

    class Stdin:
        def write(self, payload: str) -> int:
            events.append(("release", json.loads(payload)))
            return len(payload)

        def flush(self) -> None:
            events.append("flush")

        def close(self) -> None:
            events.append("close-stdin")

    class Process:
        pid = 4242
        stdin = Stdin()
        wait_calls = 0

        def wait(self, timeout: float) -> int:
            self.wait_calls += 1
            events.append(("wait", timeout))
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("helper", timeout)
            return 1

        def poll(self) -> int:
            raise AssertionError("launcher polling must not prove job termination")

    def popen(command: list[str], **kwargs: object) -> Process:
        assert command == [sys.executable, "-c", gate._WINDOWS_JOB_LAUNCHER]
        assert kwargs["stdin"] == subprocess.PIPE
        events.append("start-helper")
        return Process()

    monkeypatch.setattr(gate, "_create_windows_job", lambda: events.append("create-job") or job)
    monkeypatch.setattr(gate.subprocess, "Popen", popen)
    monkeypatch.setattr(
        gate,
        "_assign_windows_process",
        lambda assigned_job, pid: events.append(("assign", assigned_job, pid)),
    )
    monkeypatch.setattr(
        gate,
        "_terminate_windows_job",
        lambda assigned_job: events.append(("terminate-job", assigned_job)),
    )
    monkeypatch.setattr(
        gate,
        "_close_windows_job",
        lambda assigned_job: events.append(("close-job", assigned_job)),
    )

    assert gate._execute_windows(requested, tmp_path, {}, 0.25) == (124, True)
    assert events == [
        "create-job",
        "start-helper",
        ("assign", job, 4242),
        ("release", requested),
        "flush",
        "close-stdin",
        ("wait", 0.25),
        ("terminate-job", job),
        ("wait", 5),
        ("close-job", job),
    ]


def test_windows_assignment_failure_never_releases_requested_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    job = object()

    class Stdin:
        closed = False

        def write(self, payload: str) -> int:
            raise AssertionError(f"requested command was released: {payload}")

        def close(self) -> None:
            self.closed = True
            events.append("close-stdin")

    class Process:
        pid = 4242
        stdin = Stdin()

        def poll(self) -> None:
            events.append("poll-helper")
            return None

        def terminate(self) -> None:
            events.append("terminate-helper")

        def wait(self, timeout: float) -> int:
            events.append(("wait", timeout))
            return 1

    def fail_assignment(assigned_job: object, pid: int) -> None:
        assert assigned_job is job
        assert pid == 4242
        raise OSError("assignment failed")

    monkeypatch.setattr(gate, "_create_windows_job", lambda: events.append("create-job") or job)
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(gate, "_assign_windows_process", fail_assignment)
    monkeypatch.setattr(
        gate,
        "_close_windows_job",
        lambda assigned_job: events.append(("close-job", assigned_job)),
    )

    with pytest.raises(OSError, match="assignment failed"):
        gate._execute_windows(["pytest", "tests/"], tmp_path, {}, 1)

    assert events == [
        "create-job",
        "close-stdin",
        "poll-helper",
        "terminate-helper",
        ("wait", 5),
        ("close-job", job),
    ]


@pytest.mark.parametrize("payload", ["", "{}\n", '[""]\n'])
def test_windows_job_launcher_rejects_invalid_payload(payload: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", gate._WINDOWS_JOB_LAUNCHER],
        input=payload,
        text=True,
        check=False,
    )

    assert result.returncode == 125


def test_windows_job_launcher_returns_requested_exit_code() -> None:
    command = [sys.executable, "-c", "raise SystemExit(7)"]
    result = subprocess.run(
        [sys.executable, "-c", gate._WINDOWS_JOB_LAUNCHER],
        input=json.dumps(command) + "\n",
        text=True,
        check=False,
    )

    assert result.returncode == 7


if os.name == "nt":

    def test_windows_job_timeout_stops_descendant_heartbeat(tmp_path: Path) -> None:
        heartbeat = tmp_path / "heartbeat.txt"
        child = (
            "from pathlib import Path; import sys, time\n"
            "path = Path(sys.argv[1])\n"
            "while True:\n"
            "    path.write_text(str(time.time()), encoding='utf-8')\n"
            "    time.sleep(0.05)\n"
        )
        parent = (
            "from pathlib import Path; import subprocess, sys, time\n"
            "heartbeat = Path(sys.argv[2])\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline:\n"
            "    try:\n"
            "        if heartbeat.read_text(encoding='utf-8'):\n"
            "            break\n"
            "    except (FileNotFoundError, OSError):\n"
            "        pass\n"
            "    time.sleep(0.01)\n"
            "else:\n"
            "    raise SystemExit(91)\n"
            "time.sleep(30)\n"
        )

        assert gate._execute_windows(
            [sys.executable, "-c", parent, child, str(heartbeat)],
            tmp_path,
            os.environ.copy(),
            10,
        ) == (124, True)
        assert heartbeat.exists()
        time.sleep(0.3)
        stopped_value = heartbeat.read_text(encoding="utf-8")
        time.sleep(0.3)
        assert heartbeat.read_text(encoding="utf-8") == stopped_value


    def test_windows_job_close_stops_descendant_after_launcher_exit(tmp_path: Path) -> None:
        heartbeat = tmp_path / "heartbeat.txt"
        child = (
            "from pathlib import Path; import sys, time\n"
            "path = Path(sys.argv[1])\n"
            "while True:\n"
            "    path.write_text(str(time.time()), encoding='utf-8')\n"
            "    time.sleep(0.05)\n"
        )
        parent = (
            "from pathlib import Path; import subprocess, sys, time\n"
            "heartbeat = Path(sys.argv[2])\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline:\n"
            "    try:\n"
            "        if heartbeat.read_text(encoding='utf-8'):\n"
            "            break\n"
            "    except (FileNotFoundError, OSError):\n"
            "        pass\n"
            "    time.sleep(0.01)\n"
            "else:\n"
            "    raise SystemExit(91)\n"
        )

        assert gate._execute_windows(
            [sys.executable, "-c", parent, child, str(heartbeat)],
            tmp_path,
            os.environ.copy(),
            10,
        ) == (0, False)
        stopped_value = heartbeat.read_text(encoding="utf-8")
        time.sleep(0.3)
        assert heartbeat.read_text(encoding="utf-8") == stopped_value


def test_timeout_is_fail_closed(tmp_path: Path):
    repo = _committed_test_repo(
        tmp_path,
        "import time\n\ndef test_hang():\n    time.sleep(10)\n",
    )
    artifact = tmp_path / "artifact"
    command = [sys.executable, "-m", "pytest", "test_sample.py", "-q"]
    result = gate.run_pytest(
        repo,
        artifact,
        command,
        kind="integration",
        timeout_seconds=0.25,
    )
    assert result["timed_out"] is True
    assert result["passed"] is False
    assert result["child_exit_code"] == 124


def test_xdist_happy_path_records_consistent_worker_collections(tmp_path: Path):
    pytest.importorskip("xdist")
    repo = _committed_test_repo(
        tmp_path,
        "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n",
    )
    artifact = tmp_path / "artifact"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "test_sample.py",
        "-q",
        "-n",
        "2",
        "--dist=loadfile",
        "--max-worker-restart=0",
    ]
    result = gate.run_pytest(repo, artifact, command, kind="integration")
    assert result["passed"] is True
    assert result["collection_consistent"] is True
    assert len(result["worker_collections"]) == 2
    assert result["child_versions"]["pytest_xdist"] == "3.8.0"
    assert any("xdist" in name for name in result["plugin_names"])


def test_xdist_collection_divergence_is_fail_closed(tmp_path: Path):
    pytest.importorskip("xdist")
    repo = _committed_test_repo(
        tmp_path,
        "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n",
    )
    (repo / "conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    worker = getattr(config, 'workerinput', {}).get('workerid')\n"
        "    if worker == 'gw0':\n"
        "        items.reverse()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "conftest.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Graphify Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "divergence",
        ],
        cwd=repo,
        check=True,
    )
    artifact = tmp_path / "artifact"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "test_sample.py",
        "-q",
        "-n",
        "2",
        "--dist=loadfile",
        "--max-worker-restart=0",
    ]
    result = gate.run_pytest(repo, artifact, command, kind="integration")
    assert result["passed"] is False


def test_xdist_worker_crash_is_fail_closed(tmp_path: Path):
    pytest.importorskip("xdist")
    repo = _committed_test_repo(
        tmp_path,
        "import os\n\ndef test_crash():\n    os._exit(3)\n",
    )
    artifact = tmp_path / "artifact"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "test_sample.py",
        "-q",
        "-n",
        "2",
        "--dist=loadfile",
        "--max-worker-restart=0",
    ]
    result = gate.run_pytest(repo, artifact, command, kind="integration")
    assert result["passed"] is False
    assert result["child_exit_code"] != 0
