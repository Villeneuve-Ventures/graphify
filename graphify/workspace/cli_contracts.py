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
COMMANDS = frozenset({"register", "activate", "sync", "rollback", "repair", "query", "status", "doctor"})
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


_REPAIR_COORDINATES = frozenset({"registry_revision", "active_source_revision", "operation_epoch",
    "migration_epoch", "fence_high_watermark", "source_epoch", "result_operation_epoch", "result_fence_token"})


def repair_plan(value):
    """Validate bounded repair selection, actions, and exact decision identity."""
    exact(value, {"classification", "candidate", "last_good", "next_pointer_revision",
        "selected_from", "pointer_action", "journal_actions", "quarantine", "decision_sha256"})
    if value["classification"] not in ("no_op", "repairable"):
        raise ContractError("unsupported repair classification")
    if value["selected_from"] not in ("current", "pending", "prior", "journal", "last_good"):
        raise ContractError("unsupported repair source")
    if value["pointer_action"] not in ("none", "replace", "resume_pending", "finalize_pending"):
        raise ContractError("unsupported repair action")
    integer(value["next_pointer_revision"], minimum=1)
    digest(value["decision_sha256"])
    for index, ref in enumerate((value["candidate"], value["last_good"])):
        if ref is None:
            if index == 1:
                continue
            raise ContractError("repair candidate required")
        exact(ref, {"generation_id", "receipt_sha256"})
        generation_id(ref["generation_id"])
        digest(ref["receipt_sha256"])
    for field, allowed in (("journal_actions", {"recover_head", "discard_torn_tail", "adopt_tail", "append_repair"}),
                           ("quarantine", None)):
        items = value[field]
        if (not isinstance(items, list) or len(items) > 10000
                or any(not isinstance(item, str) for item in items) or len(set(items)) != len(items)):
            raise ContractError("bounded unique repair actions required")
        for item in items:
            if allowed is None:
                generation_id(item)
            elif item not in allowed:
                raise ContractError("unsupported repair journal action")


def generation_id(value):
    """Require a bounded generation identifier without path syntax."""
    if not isinstance(value, str) or re.fullmatch(r"gen-[a-z0-9][a-z0-9._-]{0,62}", value) is None:
        raise ContractError("bounded generation identifier required")


def repair_approval(value):
    """Validate workspace bindings, lifecycle coordinates, and repair evidence."""
    exact(value, {"repo_uuid", "state_root_sha256", "compatibility_sha256", "active_source_sha256",
                  "coordinates", "plan"})
    repo_uuid(value["repo_uuid"])
    for key in ("state_root_sha256", "compatibility_sha256", "active_source_sha256"):
        digest(value[key])
    exact(value["coordinates"], _REPAIR_COORDINATES)
    for key, item in value["coordinates"].items():
        integer(item, minimum=1 if key in {"registry_revision", "active_source_revision", "source_epoch", "result_operation_epoch", "result_fence_token"} else 0)
    repair_plan(value["plan"])


class RepairApproval(Document):
    @staticmethod
    def validate(value):
        """Require a complete approval record with no extra fields."""
        repair_approval(value)


def repair_parameters(value):
    """Validate preview parameters or an explicitly authorized exact approval."""
    if not isinstance(value, dict):
        raise ContractError("repair parameters required")
    if value.get("operation") == "preview":
        exact(value, {"operation", "repo_uuid"})
        repo_uuid(value["repo_uuid"])
    elif value.get("operation") == "execute":
        exact(value, {"operation", "repo_uuid", "authorization", "approval"})
        repo_uuid(value["repo_uuid"])
        authorization(value["authorization"], "REPAIR_EXECUTE")
        repair_approval(value["approval"])
        if value["repo_uuid"] != value["approval"]["repo_uuid"]:
            raise ContractError("repair workspace differs from approval")
    else:
        raise ContractError("unsupported repair operation")


def read_only_request(value):
    """Identify requests that must run under the supervisor's no-write audit."""
    return value["command"] in READ_COMMANDS or (
        value["command"] == "sync" and value["parameters"]["operation"] == "prepare") or (
        value["command"] == "repair" and value["parameters"]["operation"] == "preview")


class WorkspaceCommandRequest(Document):
    @staticmethod
    def validate(value):
        """Validate the bounded canonical envelope and command-specific parameters."""
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
        elif command == "repair":
            repair_parameters(p)
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
        """Validate a canonical success result or redacted failure outcome."""
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
            if value["command"] == "repair":
                exact(value["result"], {"approval"} if "approval" in value["result"] else {"pointer"})
                if "approval" in value["result"]:
                    repair_approval(value["result"]["approval"])
                else:
                    from .lifecycle_contracts import PointerSet
                    PointerSet.from_mapping(value["result"]["pointer"])
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
