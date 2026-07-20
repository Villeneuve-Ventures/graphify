from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import graphify.__main__ as mainmod
from graphify.workspace.contracts import canonical_json_bytes


@dataclass(frozen=True)
class _Report:
    exit_code: int
    payload: dict[str, Any]

    @property
    def canonical(self) -> bytes:
        return canonical_json_bytes(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return self.payload


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _report(exit_code: int) -> _Report:
    state = {0: "healthy", 10: "degraded", 20: "invalid"}[exit_code]
    return _Report(
        exit_code=exit_code,
        payload={
            "contract": "graphify.workspace.status",
            "schema_version": 1,
            "cli_contract_version": 1,
            "state": state,
            "exit_code": exit_code,
            "reason_code": f"fixture_{state}",
            "action_code": "none" if exit_code == 0 else "inspect_fixture",
            "safe_to_query": exit_code == 0,
            "workspaces": [],
            "checks": [
                {
                    "component": "runtime_authority",
                    "state": state,
                    "reason_code": f"fixture_{state}",
                    "action_code": "none" if exit_code == 0 else "inspect_fixture",
                }
            ],
        },
    )


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
    assert result == exit_code
    assert report.payload["state"] in rendered
    assert report.payload["checks"][0]["component"] in rendered
    assert report.payload["checks"][0]["reason_code"] in rendered
    assert report.payload["checks"][0]["action_code"] in rendered


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

    def unexpected_inspection(_inputs: object) -> _Report:
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


def test_top_level_help_lists_workspace_status_and_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])

    mainmod.main()

    output = capsys.readouterr().out
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
