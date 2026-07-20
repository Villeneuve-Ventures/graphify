"""Minimal read-only CLI surface for workspace status and doctor."""

from __future__ import annotations

import sys
from typing import Sequence, TextIO

from graphify.workspace.composition import (
    WorkspaceAuthorityError,
    WorkspaceRuntimeInputs,
    load_workspace_runtime_inputs,
)
from graphify.workspace.persistence import StatePathError
from graphify.workspace.status import (
    EXIT_USAGE,
    WorkspaceStatusReport,
    inspect_workspace_status,
    invalid_workspace_authority_report,
    missing_workspace_authority_report,
)


_USAGE = "Usage: graphify workspace [status --json|doctor]"


def _doctor_text(report: WorkspaceStatusReport) -> str:
    value = report.to_dict()
    lines = [
        f"workspace doctor: {value['state']} (exit {value['exit_code']})",
        f"safe_to_query: {str(value['safe_to_query']).lower()}",
        f"reason: {value['reason_code']}",
        f"action: {value['action_code']}",
    ]
    for check in value["checks"]:
        lines.append(
            "check "
            f"{check['component']}: {check['state']} "
            f"reason={check['reason_code']} action={check['action_code']}"
        )
    return "\n".join(lines) + "\n"


def run_workspace_command(
    arguments: Sequence[str],
    *,
    inputs: WorkspaceRuntimeInputs | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one read-only workspace command and return its stable exit code."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    command = tuple(arguments)
    if command not in {("status", "--json"), ("doctor",)}:
        errors.write(_USAGE + "\n")
        return EXIT_USAGE

    try:
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        report = (
            missing_workspace_authority_report()
            if resolved_inputs is None
            else inspect_workspace_status(resolved_inputs)
        )
    except WorkspaceAuthorityError as exc:
        report = invalid_workspace_authority_report(
            reason_code=exc.reason_code,
            action_code=exc.action_code,
        )
    except StatePathError:
        report = invalid_workspace_authority_report(
            reason_code="unsafe_state_path",
            action_code="configure_safe_state_root",
        )
    if command == ("status", "--json"):
        output.write(report.canonical.decode("utf-8"))
    else:
        output.write(_doctor_text(report))
    return report.exit_code


__all__ = ["run_workspace_command"]
