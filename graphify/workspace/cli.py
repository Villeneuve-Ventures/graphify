"""Narrow workspace registration plus read-only status and doctor commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import cast, Sequence, TextIO

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import (
    WorkspaceAuthorityError,
    WorkspaceRuntimeInputs,
    compose_workspace_runtime,
    load_workspace_runtime_inputs,
)
from graphify.workspace.contracts import (
    CLI_CONTRACT_VERSION,
    ContractError,
    WorkspaceLeaseState,
    canonical_json_bytes,
)
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    IdentityError,
    OperatorAuthorization,
    SourceDiscoveryTimeout,
    UUIDCollisionError,
    discover_source,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    StateCorrupt,
    StatePathError,
    UnsupportedRuntime,
    WorkspaceRuntimeError,
)
from graphify.workspace.registry import RegistryError, RevisionConflict
from graphify.workspace.status import (
    EXIT_DEGRADED,
    EXIT_INVALID,
    EXIT_READY,
    EXIT_USAGE,
    WorkspaceStatusReport,
    inspect_workspace_status,
    invalid_workspace_authority_report,
    missing_workspace_authority_report,
)


_REGISTRATION_CONTRACT = "graphify.workspace.registration"
_REGISTRATION_SCHEMA_VERSION = 1
_AUTHORIZATION_MAX_BYTES = 16 * 1024
_REVISION_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_REGISTER_USAGE = (
    "graphify workspace register <enroll|adopt> --repo-uuid UUID "
    "--expected-registry-revision N --authorization-stdin"
)
_USAGE = (
    "Usage: graphify workspace status --json\n"
    "       graphify workspace doctor\n"
    f"       {_REGISTER_USAGE}"
)


@dataclass(frozen=True)
class _RegisterRequest:
    action: IdentityAction
    repo_uuid: str
    expected_registry_revision: int


@dataclass(frozen=True)
class _RegistrationFailure:
    state: str
    exit_code: int
    reason_code: str
    action_code: str


def _parse_register_request(command: tuple[str, ...]) -> _RegisterRequest | None:
    if len(command) < 2 or command[0] != "register":
        return None
    try:
        action = {
            "enroll": IdentityAction.ENROLL,
            "adopt": IdentityAction.ADOPT,
        }[command[1]]
    except KeyError:
        return None

    values: dict[str, str] = {}
    authorization_stdin = False
    index = 2
    while index < len(command):
        argument = command[index]
        if argument == "--authorization-stdin":
            if authorization_stdin:
                return None
            authorization_stdin = True
            index += 1
            continue
        if argument not in {"--repo-uuid", "--expected-registry-revision"}:
            return None
        if argument in values or index + 1 >= len(command):
            return None
        value = command[index + 1]
        if value.startswith("--"):
            return None
        values[argument] = value
        index += 2

    if not authorization_stdin or set(values) != {
        "--repo-uuid",
        "--expected-registry-revision",
    }:
        return None
    try:
        repo_uuid = WorkspaceLeaseState.canonical_repo_uuid(values["--repo-uuid"])
    except ContractError:
        return None
    revision_value = values["--expected-registry-revision"]
    if _REVISION_RE.fullmatch(revision_value) is None:
        return None
    return _RegisterRequest(
        action=action,
        repo_uuid=repo_uuid,
        expected_registry_revision=int(revision_value),
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate authorization field")
        result[key] = value
    return result


def _read_operator_authorization(request: _RegisterRequest) -> OperatorAuthorization:
    try:
        raw = sys.stdin.read(_AUTHORIZATION_MAX_BYTES + 1)
        payload_size = len(raw.encode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise AuthorizationError("authorization input cannot be read") from exc
    if payload_size > _AUTHORIZATION_MAX_BYTES:
        raise AuthorizationError("authorization input exceeds the byte limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError("authorization input is not valid JSON") from exc
    required = {"action", "issued_at", "nonce", "operator_id", "reason"}
    if not isinstance(value, dict) or set(value) != required:
        raise AuthorizationError("authorization input has an invalid field set")
    if not all(isinstance(value[field], str) for field in required):
        raise AuthorizationError("authorization fields must be strings")
    authorization = cast(dict[str, str], value)
    if authorization["action"] != request.action.value:
        raise AuthorizationError("authorization action does not match registration intent")
    return OperatorAuthorization(
        action=request.action,
        operator_id=authorization["operator_id"],
        reason=authorization["reason"],
        issued_at=authorization["issued_at"],
        nonce=authorization["nonce"],
    )


def _registration_bytes(value: dict[str, object]) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _registration_success(
    request: _RegisterRequest,
    *,
    registry_revision: int,
) -> str:
    return _registration_bytes(
        {
            "action": request.action.value.lower(),
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": _REGISTRATION_CONTRACT,
            "exit_code": EXIT_READY,
            "registry_revision": registry_revision,
            "repo_uuid": request.repo_uuid,
            "schema_version": _REGISTRATION_SCHEMA_VERSION,
            "state": "registered",
        }
    )


def _registration_failure(
    request: _RegisterRequest,
    failure: _RegistrationFailure,
) -> str:
    return _registration_bytes(
        {
            "action": request.action.value.lower(),
            "action_code": failure.action_code,
            "cli_contract_version": CLI_CONTRACT_VERSION,
            "contract": _REGISTRATION_CONTRACT,
            "exit_code": failure.exit_code,
            "reason_code": failure.reason_code,
            "schema_version": _REGISTRATION_SCHEMA_VERSION,
            "state": failure.state,
        }
    )


def _classify_registration_error(error: Exception) -> _RegistrationFailure:
    if isinstance(error, RevisionConflict):
        return _RegistrationFailure(
            "conflict",
            EXIT_DEGRADED,
            "revision_conflict",
            "refresh_registry_revision",
        )
    if isinstance(error, UUIDCollisionError):
        return _RegistrationFailure(
            "conflict",
            EXIT_DEGRADED,
            "uuid_collision",
            "verify_registration_identity",
        )
    if isinstance(error, WorkspaceAuthorityError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            error.reason_code,
            error.action_code,
        )
    if isinstance(error, AuthorizationError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "authorization_invalid",
            "provide_valid_authorization",
        )
    if isinstance(error, SourceDiscoveryTimeout):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            error.code,
            "retry_registration",
        )
    if isinstance(error, IdentityError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            error.code,
            "fix_workspace_source",
        )
    if isinstance(error, StatePathError):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "unsafe_state_path",
            "configure_safe_state_root",
        )
    if isinstance(error, UnsupportedRuntime):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_runtime",
            "use_supported_runtime",
        )
    if isinstance(error, UnsupportedCompatibility):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "unsupported_compatibility",
            "install_supported_candidate",
        )
    if isinstance(error, StateCorrupt):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "state_corrupt",
            "run_workspace_repair",
        )
    if isinstance(error, CommitUnknown):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "commit_unknown",
            "run_workspace_doctor",
        )
    if isinstance(error, (RegistryError, WorkspaceRuntimeError, ContractError)):
        return _RegistrationFailure(
            "invalid",
            EXIT_INVALID,
            "registration_failed",
            "run_workspace_doctor",
        )
    return _RegistrationFailure(
        "invalid",
        EXIT_INVALID,
        "registration_failed",
        "run_workspace_doctor",
    )


def _run_registration(
    request: _RegisterRequest,
    *,
    inputs: WorkspaceRuntimeInputs | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    try:
        authorization = _read_operator_authorization(request)
        resolved_inputs = load_workspace_runtime_inputs() if inputs is None else inputs
        if resolved_inputs is None:
            failure = _RegistrationFailure(
                "invalid",
                EXIT_INVALID,
                "runtime_authority_missing",
                "install_candidate_authority",
            )
            errors.write(_registration_failure(request, failure))
            return failure.exit_code
        runtime = compose_workspace_runtime(resolved_inputs)
        source = discover_source(Path.cwd())
        if source.repo_uuid != request.repo_uuid:
            raise UUIDCollisionError(
                "explicit registration UUID does not match the source configuration"
            )
        if request.action is IdentityAction.ENROLL:
            document = runtime.registry.enroll(
                source,
                authorization,
                expected_revision=request.expected_registry_revision,
            )
        else:
            document = runtime.registry.adopt(
                source,
                authorization,
                expected_revision=request.expected_registry_revision,
            )
    except InjectedFault:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        failure = _classify_registration_error(exc)
        errors.write(_registration_failure(request, failure))
        return failure.exit_code
    revision = int(document.to_dict()["revision"])
    output.write(_registration_success(request, registry_revision=revision))
    return EXIT_READY


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
    """Run one narrow workspace command and return its stable exit code."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    command = tuple(arguments)
    if command and command[0] == "register":
        request = _parse_register_request(command)
        if request is None:
            errors.write(_USAGE + "\n")
            return EXIT_USAGE
        return _run_registration(
            request,
            inputs=inputs,
            output=output,
            errors=errors,
        )
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
    except UnsupportedRuntime:
        report = invalid_workspace_authority_report(
            reason_code="unsupported_runtime",
            action_code="use_supported_runtime",
        )
    if command == ("status", "--json"):
        output.write(report.canonical.decode("utf-8"))
    else:
        output.write(_doctor_text(report))
    return report.exit_code


__all__ = ["run_workspace_command"]
