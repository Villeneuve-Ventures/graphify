from __future__ import annotations

from io import StringIO
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import graphify.__main__ as mainmod
from graphify.workspace.composition import (
    RUNTIME_AUTHORITY_FILENAME,
    WorkspaceAuthorityInvalid,
    WorkspaceAuthorityUnsupported,
    WorkspaceRuntimeAuthority,
)
from graphify.workspace.persistence import StatePathError, UnsupportedRuntime
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from graphify.workspace.status import WorkspaceStatusReport, load_status_schema
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    COMPATIBILITY_SHA256,
    metadata_snapshot,
    tree_snapshot,
)


SEMANTIC_QUEUE_POLICY = SemanticQueuePolicy(
    max_items=8,
    max_bytes=16 * 1024,
    retry_budget=1,
)


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _ready_workspace() -> dict[str, Any]:
    receipt_sha256 = "3" * 64
    absent_lease = {
        "present": False,
        "operation": None,
        "fence_token": None,
        "liveness": "not_evaluated",
    }
    return {
        "repo_uuid": "11111111-1111-4111-8111-111111111111",
        "state": "ready",
        "safe_to_query": True,
        "reason_code": "ready",
        "action_code": "none",
        "source_identity_sha256": "1" * 64,
        "active_source_revision": 1,
        "source_epoch": 1,
        "policy_sha256": "2" * 64,
        "generations": {
            "pointer_revision": 1,
            "current": {
                "generation_id": "gen-current",
                "receipt_sha256": receipt_sha256,
            },
            "last_good": None,
            "pending": [],
            "pending_reason_code": "ready",
        },
        "staged_build": {
            "present": False,
            "blocking": False,
            "revision": None,
            "generation_id": None,
            "lifecycle_state": None,
            "logical_request_sha256": None,
            "request_sha256": None,
        },
        "queue": {
            "revision": 0,
            "desired_watermark": 0,
            "completed_watermark": 0,
            "depth": 0,
            "pending": 0,
            "claimed": 0,
            "retrying": 0,
            "dead_letter": 0,
            "oldest_age_seconds": None,
            "age_reason_code": "not_recorded_v1",
        },
        "leases": {
            "migration_epoch": 0,
            "workspace": absent_lease,
            "semantic": absent_lease,
        },
        "journal": {
            "sequence": 0,
            "last_successful_transition": None,
            "last_failure_classification": None,
        },
        "freshness": {
            "state": "observed_current",
            "duration_ms": 0,
            "observation_boundary": "two_sided",
            "binding": {
                "active_source_revision": 1,
                "pointer_revision": 1,
                "receipt_sha256": receipt_sha256,
            },
        },
        "watcher": {
            "state": "not_evaluated",
            "heartbeat": None,
            "boot_id": None,
            "process_id": None,
            "reason_code": "service_deferred_p5c",
        },
        "resources": {
            "state": "not_evaluated",
            "pressure": None,
            "reason_code": "resource_accounting_deferred_p5c",
        },
        "repair": {"required": False, "count": 0},
    }


def _report(exit_code: int) -> WorkspaceStatusReport:
    state, reason_code, action_code = {
        0: ("ready", "ready", "none"),
        10: ("degraded", "no_registered_workspaces", "register_workspace"),
        20: ("invalid", "registry_invalid", "run_workspace_repair"),
    }[exit_code]
    return WorkspaceStatusReport(
        {
            "contract": "graphify.workspace.status",
            "schema_version": 2,
            "cli_contract_version": 1,
            "state": state,
            "exit_code": exit_code,
            "reason_code": reason_code,
            "action_code": action_code,
            "safe_to_query": exit_code == 0,
            "correlation_id": "status-000000000000000000000000",
            "runtime": {
                "distribution_version": "test",
                "engine_baseline": "test",
                "adapter_contract_version": 1,
                "state_schema_version": 1,
                "compatibility_sha256": None,
            },
            "workspaces": [_ready_workspace()] if exit_code == 0 else [],
            "checks": [
                {
                    "component": "runtime_authority",
                    "state": state,
                    "reason_code": reason_code,
                    "action_code": action_code,
                }
            ],
        }
    )


@pytest.mark.parametrize("exit_code", [0, 10, 20])
def test_cli_report_fixtures_follow_the_versioned_status_contract(exit_code: int) -> None:
    value = _report(exit_code).to_dict()

    Draft202012Validator(
        load_status_schema(),
        format_checker=FormatChecker(),
    ).validate(value)
    WorkspaceStatusReport(value)


@pytest.mark.parametrize("exit_code", [0, 10, 20])
def test_status_json_emits_one_canonical_document_and_returns_report_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    workspace_cli = _cli()
    report = _report(exit_code)
    monkeypatch.setattr(workspace_cli, "inspect_workspace_status", lambda _inputs: report)
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        ["status", "--json"],
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == exit_code
    assert stdout.getvalue().encode("utf-8") == report.canonical
    assert json.loads(stdout.getvalue()) == report.to_dict()
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("exit_code", [0, 10, 20])
def test_doctor_renders_the_same_checks_and_returns_report_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    workspace_cli = _cli()
    report = _report(exit_code)
    monkeypatch.setattr(workspace_cli, "inspect_workspace_status", lambda _inputs: report)
    stdout = StringIO()

    result = workspace_cli.run_workspace_command(
        ["doctor"],
        inputs=object(),
        stdout=stdout,
        stderr=StringIO(),
    )

    rendered = stdout.getvalue()
    value = report.to_dict()
    assert result == exit_code
    assert value["state"] in rendered
    assert value["checks"][0]["component"] in rendered
    assert value["checks"][0]["reason_code"] in rendered
    assert value["checks"][0]["action_code"] in rendered


def test_doctor_renders_the_staged_build_recovery_reason_and_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    report = SimpleNamespace(
        exit_code=10,
        to_dict=lambda: {
            "state": "degraded",
            "exit_code": 10,
            "safe_to_query": False,
            "reason_code": "staged_build_recovery_required",
            "action_code": "resume_exact_workspace_sync",
            "checks": [
                {
                    "component": "workspace:11111111-1111-4111-8111-111111111111:staged_build",
                    "state": "degraded",
                    "reason_code": "staged_build_recovery_required",
                    "action_code": "resume_exact_workspace_sync",
                }
            ],
        },
    )
    monkeypatch.setattr(workspace_cli, "inspect_workspace_status", lambda _inputs: report)
    stdout = StringIO()

    result = workspace_cli.run_workspace_command(
        ["doctor"],
        inputs=object(),
        stdout=stdout,
        stderr=StringIO(),
    )

    assert result == 10
    assert "staged_build_recovery_required" in stdout.getvalue()
    assert "resume_exact_workspace_sync" in stdout.getvalue()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["unknown"],
        ["status"],
        ["status", "--json", "extra"],
        ["doctor", "extra"],
        ["doctor", "--json"],
    ],
)
def test_invalid_workspace_arguments_return_usage_without_inspecting_state(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    workspace_cli = _cli()

    def unexpected_inspection(_inputs: object) -> WorkspaceStatusReport:
        raise AssertionError("invalid arguments must not inspect workspace state")

    monkeypatch.setattr(workspace_cli, "inspect_workspace_status", unexpected_inspection)
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        arguments,
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 64
    assert stdout.getvalue() == ""
    assert "usage" in stderr.getvalue().lower()


def test_status_without_explicit_inputs_loads_production_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    loaded_inputs = object()
    report = _report(10)
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: loaded_inputs,
    )
    monkeypatch.setattr(
        workspace_cli,
        "inspect_workspace_status",
        lambda inputs: report if inputs is loaded_inputs else None,
    )
    stdout = StringIO()

    result = workspace_cli.run_workspace_command(
        ["status", "--json"],
        inputs=None,
        stdout=stdout,
        stderr=StringIO(),
    )

    assert result == 10
    assert json.loads(stdout.getvalue()) == report.to_dict()


@pytest.mark.parametrize(
    ("error", "reason_code", "action_code"),
    [
        (
            WorkspaceAuthorityInvalid("operator-secret-invalid-authority"),
            "runtime_authority_invalid",
            "install_candidate_authority",
        ),
        (
            WorkspaceAuthorityUnsupported("operator-secret-unsupported-authority"),
            "runtime_authority_unsupported",
            "install_supported_candidate",
        ),
        (
            StatePathError("operator-secret-unsafe-state-home"),
            "unsafe_state_path",
            "configure_safe_state_root",
        ),
        (
            UnsupportedRuntime("operator-secret-unsupported-runtime"),
            "unsupported_runtime",
            "use_supported_runtime",
        ),
    ],
)
def test_production_input_load_failures_are_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason_code: str,
    action_code: str,
) -> None:
    workspace_cli = _cli()

    def fail_load() -> None:
        raise error

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", fail_load)
    stdout = StringIO()

    result = workspace_cli.run_workspace_command(
        ["status", "--json"],
        inputs=None,
        stdout=stdout,
        stderr=StringIO(),
    )

    payload = json.loads(stdout.getvalue())
    assert result == 20
    assert payload["reason_code"] == reason_code
    assert payload["action_code"] == action_code
    assert "operator-secret" not in stdout.getvalue()


def test_status_without_production_authority_fails_closed_without_creating_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    home = tmp_path / "private-home"
    state_home = tmp_path / "private-state"
    checkout = tmp_path / "checkout"
    home.mkdir()
    checkout.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.chdir(checkout)
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        ["status", "--json"],
        inputs=None,
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert result == 20
    assert payload["checks"]
    assert any(check["state"] == "invalid" for check in payload["checks"])
    assert not state_home.exists()
    assert list(home.iterdir()) == []
    assert list(checkout.iterdir()) == []
    assert stderr.getvalue() == ""


def test_missing_authority_output_redacts_private_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    private_home = tmp_path / "operator-secret-home"
    private_state = tmp_path / "operator-secret-state"
    private_home.mkdir()
    monkeypatch.setenv("HOME", str(private_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(private_state))
    stdout = StringIO()

    result = workspace_cli.run_workspace_command(
        ["status", "--json"],
        inputs=None,
        stdout=stdout,
        stderr=StringIO(),
    )

    rendered = stdout.getvalue()
    assert result == 20
    assert str(private_home) not in rendered
    assert str(private_state) not in rendered


def test_top_level_help_lists_workspace_register_status_and_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])

    mainmod.main()

    output = capsys.readouterr().out
    assert "workspace register" in output
    assert "workspace status --json" in output
    assert "workspace doctor" in output


def test_top_level_workspace_dispatch_propagates_arguments_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> int:
        calls.append(arguments)
        return 10

    monkeypatch.setattr(workspace_cli, "run_workspace_command", fake_run)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "workspace", "status", "--json"])

    with pytest.raises(SystemExit) as raised:
        mainmod.main()

    assert raised.value.code == 10
    assert calls == [["status", "--json"]]


def test_top_level_registration_failure_preserves_exit_with_broken_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()

    class BrokenFlushStream(StringIO):
        def flush(self) -> None:
            raise BrokenPipeError

    def fake_run(arguments: list[str], **_kwargs: object) -> int:
        assert arguments == ["register"]
        return 64

    monkeypatch.setattr(workspace_cli, "run_workspace_command", fake_run)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "workspace", "register"])
    monkeypatch.setattr(sys, "stdout", BrokenFlushStream())

    with pytest.raises(SystemExit) as raised:
        mainmod.main()

    assert raised.value.code == 64


@pytest.mark.parametrize(
    ("arguments", "expected_exit"),
    [
        (("workspace", "status", "--json"), 20),
        (("workspace", "doctor"), 20),
        (("workspace", "status"), 64),
    ],
)
def test_real_module_cli_preserves_exit_and_no_write_contract(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_exit: int,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    state_home = tmp_path / "state-home"
    home.mkdir()
    checkout.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state_home),
            "PYTHONPATH": str(Path(__file__).parents[1]),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "graphify", *arguments],
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_exit
    assert not state_home.exists()
    assert list(home.iterdir()) == []
    assert list(checkout.iterdir()) == []
    if arguments[-2:] == ("status", "--json"):
        payload = json.loads(result.stdout)
        assert payload["contract"] == "graphify.workspace.status"
        assert payload["exit_code"] == 20
        assert result.stdout.endswith("\n")
        assert result.stderr == ""
    elif arguments[-1] == "doctor":
        assert "workspace doctor: invalid (exit 20)" in result.stdout
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert "usage" in result.stderr.lower()


@pytest.mark.parametrize("arguments", [("status", "--json"), ("doctor",)])
def test_real_module_cli_loads_external_authority_without_writing_state(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    state_home = tmp_path / "state-home"
    state_root = state_home / "graphify"
    home.mkdir()
    checkout.mkdir()
    state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    authority = state_root / RUNTIME_AUTHORITY_FILENAME
    authority.write_bytes(
        WorkspaceRuntimeAuthority(
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=SEMANTIC_QUEUE_POLICY,
        ).canonical
    )
    authority.chmod(0o600)
    before_tree = tree_snapshot(state_home)
    before_metadata = metadata_snapshot(state_home)
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state_home),
            "PYTHONPATH": str(Path(__file__).parents[1]),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "graphify", "workspace", *arguments],
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 20
    assert tree_snapshot(state_home) == before_tree
    assert metadata_snapshot(state_home) == before_metadata
    assert list(home.iterdir()) == []
    assert list(checkout.iterdir()) == []
    assert "compatibility_manifest_missing" not in result.stdout
    if arguments == ("status", "--json"):
        payload = json.loads(result.stdout)
        assert payload["runtime"]["compatibility_sha256"] == COMPATIBILITY_SHA256
    else:
        assert "workspace doctor: invalid (exit 20)" in result.stdout
