"""Canonical, bounded workspace requests. No runtime or source inspection here."""
from __future__ import annotations

from pathlib import Path
import re
from uuid import UUID

from .adapters.base import QueryRequest
from .contracts import CompatibilityManifest, ContractError, Document, exact, integer, digest

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TIMEOUT_MS = 300_000
COMMANDS = frozenset({"register", "activate", "sync", "rollback", "query", "status", "doctor"})
READ_COMMANDS = frozenset({"query", "status", "doctor"})


def absolute_root(value):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096
            or "\x00" in value or not Path(value).is_absolute()
            or ".." in Path(value).parts or str(Path(value)) != value):
        raise ContractError("explicit canonical absolute root required")


def repo_uuid(value):
    if not isinstance(value, str):
        raise ContractError("canonical repo UUID required")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ContractError("canonical repo UUID required") from None


def authorization(value, action):
    from .identity import IdentityAction, OperatorAuthorization
    exact(value, {"action", "operator_id", "reason", "issued_at", "nonce"})
    for text in value.values():
        if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 4096:
            raise ContractError("bounded authorization strings required")
    if value["action"] != action.upper():
        raise ContractError("authorization action differs")
    return OperatorAuthorization(**{**value, "action": IdentityAction(value["action"])})


def rollback_parameters(value):
    exact(value, {"repo_uuid", "authorization", "expected_registry_revision",
        "expected_active_source_revision", "expected_operation_epoch", "expected_migration_epoch",
        "expected_fence_high_watermark", "expected_pointer_revision", "expected_source_epoch",
        "expected_current_receipt_sha256", "target_generation_id", "target_receipt_sha256"})
    repo_uuid(value["repo_uuid"])
    authorization(value["authorization"], "rollback")
    for key in ("expected_registry_revision", "expected_active_source_revision",
                "expected_pointer_revision", "expected_source_epoch"):
        integer(value[key], minimum=1)
    for key in ("expected_operation_epoch", "expected_migration_epoch", "expected_fence_high_watermark"):
        integer(value[key])
    if (not isinstance(value["target_generation_id"], str)
            or re.fullmatch(r"gen-[a-z0-9][a-z0-9._-]{0,62}", value["target_generation_id"]) is None):
        raise ContractError("bounded rollback target required")
    digest(value["expected_current_receipt_sha256"])
    digest(value["target_receipt_sha256"])


class WorkspaceCommandRequest(Document):
    @staticmethod
    def validate(value):
        exact(value, {"contract", "format_version", "command", "request_id", "state_root",
                      "compatibility_manifest", "timeout_ms", "parameters"})
        if (value["contract"] != "graphify.workspace.cli.request"
                or type(value["format_version"]) is not int or value["format_version"] != 1):
            raise ContractError("unsupported command request")
        command = value["command"]
        if not isinstance(command, str) or command not in COMMANDS:
            raise ContractError("unsupported workspace command")
        if (not isinstance(value["request_id"], str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value["request_id"]) is None):
            raise ContractError("bounded request identifier required")
        absolute_root(value["state_root"])
        CompatibilityManifest.from_mapping(value["compatibility_manifest"])
        integer(value["timeout_ms"], minimum=1, maximum=MAX_TIMEOUT_MS)
        p = value["parameters"]
        if command == "register":
            exact(p, {"operation", "source_root", "authorization", "expected_registry_revision"})
            if p["operation"] not in ("enroll", "adopt", "rebind", "rotate"):
                raise ContractError("unsupported registration operation")
            absolute_root(p["source_root"])
            authorization(p["authorization"], p["operation"])
            integer(p["expected_registry_revision"])
        elif command == "activate":
            exact(p, {"source_root", "authorization", "expected_registry_revision",
                      "expected_active_source_revision", "expected_operation_epoch",
                      "expected_migration_epoch", "ttl_ns"})
            absolute_root(p["source_root"])
            authorization(p["authorization"], "activate")
            for key in ("expected_registry_revision", "expected_active_source_revision",
                        "expected_operation_epoch", "expected_migration_epoch"):
                integer(p[key])
            integer(p["ttl_ns"], minimum=1, maximum=MAX_TIMEOUT_MS * 1_000_000)
        elif command == "sync":
            if not isinstance(p, dict):
                raise ContractError("sync parameters required")
            if p.get("operation") == "prepare":
                exact(p, {"operation", "repo_uuid", "generation_id", "source_epoch",
                          "desired_watermark", "expected_payload_bytes"})
                repo_uuid(p["repo_uuid"])
                if (not isinstance(p["generation_id"], str)
                        or re.fullmatch(r"gen-[a-z0-9][a-z0-9._-]{0,62}", p["generation_id"]) is None):
                    raise ContractError("bounded generation identifier required")
                for key in ("source_epoch", "desired_watermark", "expected_payload_bytes"):
                    integer(p[key], minimum=1)
            elif p.get("operation") == "execute":
                from .sync import StructuralSyncRequest
                from .contracts import canonical_json_bytes
                exact(p, {"operation", "sync_request", "attempt_sha256"})
                StructuralSyncRequest.from_json(canonical_json_bytes(p["sync_request"]))
                digest(p["attempt_sha256"])
            else:
                raise ContractError("unsupported sync operation")
        elif command == "rollback":
            rollback_parameters(p)
        elif command == "query":
            exact(p, {"repo_uuid", "question", "mode", "depth", "token_budget", "context_filters"})
            repo_uuid(p["repo_uuid"])
            if not isinstance(p["context_filters"], list):
                raise ContractError("context filters must be a JSON array")
            QueryRequest(**{k: v for k, v in p.items() if k != "repo_uuid"})
        else:
            exact(p, {"repo_uuid"})
            repo_uuid(p["repo_uuid"])
        from .contracts import canonical_json_bytes
        if len(canonical_json_bytes(value)) > MAX_REQUEST_BYTES:
            raise ContractError("command request byte limit exceeded")


class WorkspaceCommandResponse(Document):
    @staticmethod
    def validate(value):
        exact(value, {"contract", "format_version", "command", "request_id", "outcome",
                      "result", "error_code"})
        if (value["contract"] != "graphify.workspace.cli.response"
                or type(value["format_version"]) is not int or value["format_version"] != 1):
            raise ContractError("unsupported command response")
        if value["command"] is not None and value["command"] not in COMMANDS:
            raise ContractError("unsupported response command")
        if value["request_id"] is not None and (
                not isinstance(value["request_id"], str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value["request_id"]) is None):
            raise ContractError("invalid response identifier")
        if value["outcome"] == "ok":
            if not isinstance(value["result"], dict) or value["error_code"] is not None:
                raise ContractError("success response required")
            if value["command"] == "rollback":
                from .lifecycle_contracts import PointerSet
                exact(value["result"], {"pointer"})
                PointerSet.from_mapping(value["result"]["pointer"])
        elif value["outcome"] == "refused":
            if value["result"] is not None or value["error_code"] not in ERROR_CODES:
                raise ContractError("redacted refusal response required")
        else:
            raise ContractError("invalid response outcome")


ERROR_CODES = frozenset({"bad_request", "unsupported_runtime", "authority_invalid",
    "workspace_refused", "deadline_exceeded", "execution_unknown", "execution_failed"})


def response(command=None, request_id=None, *, result=None, error_code=None):
    return WorkspaceCommandResponse.from_mapping({
        "contract": "graphify.workspace.cli.response", "format_version": 1,
        "command": command, "request_id": request_id,
        "outcome": "ok" if error_code is None else "refused",
        "result": result, "error_code": error_code,
    })
