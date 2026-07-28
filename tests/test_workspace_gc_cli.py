"""Public P5B2 offline-GC preview and explicit fenced lifecycle contracts."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast, Never

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.composition import (
    WorkspaceRuntimeAuthority,
    WorkspaceRuntimeInputs,
    WorkspaceAuthorityInvalid,
    WorkspaceAuthorityUnsupported,
    compose_workspace_runtime,
    load_workspace_runtime_inputs,
)
from graphify.workspace.contracts import GcIntentState, JsonValue, canonical_json_bytes
from graphify.workspace.gc import (
    GcCoordinationUnavailable,
    GcError,
    GcPlanStale,
    GcPreviewAuthorityConflict,
    GcProtection,
    GcRecoveryRequired,
    GcStore,
)
from graphify.workspace.leases import LeaseRecoveryRequired
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    PosixSyscalls,
    StateCorrupt,
    StatePathError,
    UnsupportedRuntime,
)
from graphify.workspace.pointers import PointerCorrupt
from graphify.workspace.semantic_queue import SemanticQueuePolicy

from tests.test_workspace_gc import (
    EMPTY_PROTECTION,
    GC_RECOVERY_PHASES,
    POLICY,
    _runtime,
)
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    START,
    SUPPORTED,
    acquire,
    metadata_snapshot,
    tree_snapshot,
)


_GC_USAGE = "graphify workspace gc --dry-run --request-stdin"
_GC_EXECUTE_USAGE = "graphify workspace gc --execute --request-stdin"
_GC_RECONCILE_USAGE = "graphify workspace gc --reconcile --request-stdin"
_GC_PURGE_USAGE = "graphify workspace gc --purge --request-stdin"
_SEMANTIC_QUEUE_POLICY = SemanticQueuePolicy(
    max_items=16,
    max_bytes=64 * 1024,
    retry_budget=1,
)


class _RejectingWriteSyscalls(PosixSyscalls):
    """Fail if any durable-state mutation primitive is attempted."""

    @staticmethod
    def _reject(operation: str) -> Never:
        pytest.fail(f"GC preview attempted mutating syscall: {operation}")

    def write(self, descriptor: int, data: memoryview) -> int:
        self._reject("write")

    def fsync(self, descriptor: int) -> None:
        self._reject("fsync")

    def replace(self, source: Path, destination: Path) -> None:
        self._reject("replace")

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._reject("replace_at")

    def unlink(self, path: Path) -> None:
        self._reject("unlink")

    def unlink_at(self, path: str, *, dir_fd: int) -> None:
        self._reject("unlink_at")

    def rmdir(self, path: Path) -> None:
        self._reject("rmdir")

    def rmdir_at(self, path: str, *, dir_fd: int) -> None:
        self._reject("rmdir_at")

    def mkdir(self, path: Path, mode: int) -> None:
        self._reject("mkdir")

    def mkdir_at(self, path: str, mode: int, *, dir_fd: int) -> None:
        self._reject("mkdir_at")


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _gc_module() -> Any:
    return importlib.import_module("graphify.workspace.gc")


def _protection_value(
    protection: GcProtection = EMPTY_PROTECTION,
) -> dict[str, JsonValue]:
    return {
        "active_lease_generations": cast(
            JsonValue, sorted(protection.active_lease_generations)
        ),
        "fixture_generations": cast(
            JsonValue, sorted(protection.fixture_generations)
        ),
        "migration_sources": cast(JsonValue, sorted(protection.migration_sources)),
        "proof_generations": cast(JsonValue, sorted(protection.proof_generations)),
        "rollback_artifact_generations": cast(
            JsonValue, sorted(protection.rollback_artifact_generations)
        ),
        "rollback_sources": cast(JsonValue, sorted(protection.rollback_sources)),
    }


def _request_value(
    *,
    expected_registry_revision: int = 1,
    expected_active_source_revision: int = 1,
    expected_operation_epoch: int = 2,
    expected_migration_epoch: int = 0,
    expected_pointer_revision: int = 1,
    protection: GcProtection = EMPTY_PROTECTION,
) -> dict[str, JsonValue]:
    return {
        "capacity_policy": POLICY.to_dict(),
        "cli_contract_version": 1,
        "contract": "graphify.workspace.gc_preview_request",
        "expected_active_source_revision": expected_active_source_revision,
        "expected_migration_epoch": expected_migration_epoch,
        "expected_operation_epoch": expected_operation_epoch,
        "expected_pointer_revision": expected_pointer_revision,
        "expected_registry_revision": expected_registry_revision,
        "protections": _protection_value(protection),
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "timeout_ms": 5_000,
    }


def _request_for_runtime(
    harness: Any,
    pointers: Any,
    *,
    protection: GcProtection = EMPTY_PROTECTION,
) -> dict[str, JsonValue]:
    registry = harness.registry.load()
    registry_value = registry.to_dict()
    entry = registry_value["workspaces"][0]
    state = harness.leases.inspect(REPO_UUID)
    pointer = pointers.load(REPO_UUID)
    assert pointer is not None
    return _request_value(
        expected_registry_revision=int(registry_value["revision"]),
        expected_active_source_revision=int(entry["active_source_revision"]),
        expected_operation_epoch=state.operation_epoch,
        expected_migration_epoch=state.migration_epoch,
        expected_pointer_revision=int(pointer.to_dict()["pointer_revision"]),
        protection=protection,
    )


def _request_bytes(value: dict[str, JsonValue] | None = None) -> bytes:
    return canonical_json_bytes(_request_value() if value is None else value)


def _result_common() -> dict[str, object]:
    return {
        "cli_contract_version": 1,
        "contract": "graphify.workspace.gc_preview_result",
        "schema_version": 1,
    }


def _result_payload(stream: StringIO) -> dict[str, object]:
    payload = json.loads(stream.getvalue())
    Draft202012Validator(
        _cli().load_gc_preview_result_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    return payload


def _runtime_namespace(harness: Any, generations: Any, pointers: Any) -> Any:
    return SimpleNamespace(
        registry=harness.registry,
        leases=harness.leases,
        gc=GcStore(
            harness.state_root,
            harness.leases,
            generations,
            pointers,
            capabilities=harness.leases.state.capabilities,
        ),
    )


def _guarded_runtime(harness: Any) -> Any:
    return compose_workspace_runtime(
        WorkspaceRuntimeInputs(
            state_root=harness.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=_SEMANTIC_QUEUE_POLICY,
            capabilities=SUPPORTED,
            syscalls=_RejectingWriteSyscalls(),
        )
    )


def test_gc_preview_request_and_result_schemas_freeze_the_public_contract() -> None:
    workspace_cli = _cli()
    request_schema = workspace_cli.load_gc_preview_request_schema()
    result_schema = workspace_cli.load_gc_preview_result_schema()
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(result_schema)

    request_validator = Draft202012Validator(
        request_schema,
        format_checker=FormatChecker(),
    )
    result_validator = Draft202012Validator(
        result_schema,
        format_checker=FormatChecker(),
    )
    request = _request_value()
    assert not list(request_validator.iter_errors(request))
    for field in request:
        incomplete = dict(request)
        incomplete.pop(field)
        assert list(request_validator.iter_errors(incomplete)), field
    assert list(request_validator.iter_errors({**request, "unexpected": True}))
    assert list(request_validator.iter_errors({**request, "timeout_ms": 0}))
    assert list(request_validator.iter_errors({**request, "timeout_ms": 60_001}))
    assert list(
        request_validator.iter_errors(
            {
                **request,
                "protections": {
                    **_protection_value(),
                    "proof_generations": ["gen-a", "gen-a"],
                },
            }
        )
    )

    success = {
        **_result_common(),
        "capacity_policy_sha256": POLICY.sha256,
        "candidates": ["gen-unused"],
        "decision": "preview",
        "exit_code": 0,
        "observation_boundary": "locked_double_snapshot",
        "observed": {
            "active_source_revision": 1,
            "migration_epoch": 0,
            "operation_epoch": 2,
            "pointer_revision": 1,
            "registry_revision": 1,
        },
        "protected": [
            {"generation_id": "gen-current", "reasons": ["visible_current"]}
        ],
        "reason_code": "preview_ready",
        "repo_uuid": REPO_UUID,
        "state": "previewed",
    }
    failure = {
        **_result_common(),
        "action_code": "refresh_gc_request",
        "decision": "withhold",
        "exit_code": 10,
        "observation_boundary": "not_observed",
        "reason_code": "gc_authority_conflict",
        "state": "conflict",
    }
    assert not list(result_validator.iter_errors(success))
    assert not list(result_validator.iter_errors(failure))
    assert list(result_validator.iter_errors({**success, "fence_token": 7}))
    assert list(result_validator.iter_errors({**failure, "repo_uuid": REPO_UUID}))
    assert list(result_validator.iter_errors({**failure, "candidates": []}))


@pytest.mark.parametrize(
    "arguments",
    [
        ("gc",),
        ("gc", "--dry-run"),
        ("gc", "--request-stdin"),
        ("gc", "--dry-run", "--request-stdin", "extra"),
        ("gc", "--request-stdin", "--dry-run"),
        ("gc", "--dry-run", "--dry-run", "--request-stdin"),
        ("gc", "--help"),
    ],
)
def test_gc_preview_usage_is_exact_and_precedes_authority_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("usage must not load GC authority"),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("usage must not read GC stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        arguments,
        stdout=stdout,
        stderr=stderr,
    ) == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == _GC_USAGE + "\n"


def test_general_workspace_usage_lists_the_gc_lifecycle_commands() -> None:
    workspace_cli = _cli()
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("unknown",),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 64
    usage = stderr.getvalue()
    assert _GC_USAGE in usage
    assert _GC_EXECUTE_USAGE in usage
    assert _GC_RECONCILE_USAGE in usage
    assert _GC_PURGE_USAGE in usage


def test_gc_preview_missing_authority_is_reported_before_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: None)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("authority failure must precede GC stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    assert _result_payload(stderr) == {
        **_result_common(),
        "action_code": "install_candidate_authority",
        "decision": "withhold",
        "exit_code": 20,
        "observation_boundary": "not_observed",
        "reason_code": "runtime_authority_missing",
        "state": "invalid",
    }


@pytest.mark.parametrize(
    ("error", "state", "reason_code", "action_code"),
    [
        (
            WorkspaceAuthorityInvalid("/private/authority provider-secret"),
            "invalid",
            "runtime_authority_invalid",
            "install_candidate_authority",
        ),
        (
            WorkspaceAuthorityUnsupported("/private/authority provider-secret"),
            "unsupported",
            "runtime_authority_unsupported",
            "install_supported_candidate",
        ),
        (
            StatePathError("/private/state provider-secret"),
            "invalid",
            "unsafe_state_path",
            "configure_safe_state_root",
        ),
        (
            UnsupportedRuntime("/private/runtime provider-secret"),
            "unsupported",
            "unsupported_runtime",
            "use_supported_runtime",
        ),
    ],
)
def test_gc_preview_invalid_runtime_authority_precedes_stdin_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    state: str,
    reason_code: str,
    action_code: str,
) -> None:
    workspace_cli = _cli()

    def fail_load() -> None:
        raise error

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", fail_load)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("invalid authority must precede GC stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    result = _result_payload(stderr)
    assert result["state"] == state
    assert result["reason_code"] == reason_code
    assert result["action_code"] == action_code
    assert "/private" not in stderr.getvalue()
    assert "provider-secret" not in stderr.getvalue()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"contract":"graphify.workspace.gc_preview_request","contract":"duplicate"}',
        b'{"schema_version":1}',
        json.dumps(_request_value(), indent=2).encode(),
        _request_bytes()[:-1] + b" ",
        b"\xff",
        _request_bytes({**_request_value(), "timeout_ms": 0}),
        _request_bytes({**_request_value(), "timeout_ms": 60_001}),
        _request_bytes({**_request_value(), "expected_pointer_revision": True}),
        _request_bytes(
            {
                **_request_value(),
                "capacity_policy": {
                    **POLICY.to_dict(),
                    "reserve_bytes": 0,
                },
            }
        ),
        _request_bytes(
            {
                **_request_value(),
                "protections": {
                    **_protection_value(),
                    "proof_generations": ["gen-z", "gen-a"],
                },
            }
        ),
        _request_bytes(
            {
                **_request_value(),
                "protections": {
                    **_protection_value(),
                    "proof_generations": ["gen-a", "gen-a"],
                },
            }
        ),
        _request_bytes(
            {
                **_request_value(),
                "protections": {
                    **_protection_value(),
                    "proof_generations": ["../secret"],
                },
            }
        ),
    ],
)
def test_gc_preview_rejects_invalid_stdin_before_runtime_preview(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    workspace_cli = _cli()

    class ForbiddenGc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("invalid GC request must not reach the runtime")

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=ForbiddenGc()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(payload)))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    result = _result_payload(stderr)
    assert result["reason_code"] == "gc_request_invalid"
    assert result["action_code"] == "provide_valid_gc_request"
    assert "graphify.workspace.gc_preview_request" not in stderr.getvalue()


def test_gc_preview_rejects_oversized_protection_union_before_runtime_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    maximum = _gc_module().GC_PREVIEW_MAX_GENERATIONS
    first_count = maximum // 2
    protections = _protection_value()
    protections["fixture_generations"] = cast(
        JsonValue,
        [f"gen-fixture-{index:04x}" for index in range(first_count)],
    )
    protections["proof_generations"] = cast(
        JsonValue,
        [f"gen-proof-{index:04x}" for index in range(maximum - first_count + 1)],
    )
    payload = _request_bytes(
        {
            **_request_value(),
            "protections": protections,
        }
    )
    assert len(payload) <= workspace_cli._GC_PREVIEW_REQUEST_MAX_BYTES

    class ForbiddenGc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("oversized protection union must not reach the runtime")

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=ForbiddenGc()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(payload)))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    result = _result_payload(stderr)
    assert result["reason_code"] == "gc_request_invalid"
    assert result["action_code"] == "provide_valid_gc_request"


def test_gc_preview_stdin_is_bounded_before_decode_or_runtime_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()

    class ForbiddenGc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("oversized request must not reach the runtime")

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=ForbiddenGc()),
    )
    oversized = b"{" + b"x" * workspace_cli._GC_PREVIEW_REQUEST_MAX_BYTES
    assert len(oversized) == workspace_cli._GC_PREVIEW_REQUEST_MAX_BYTES + 1
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(oversized)))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    assert _result_payload(stderr)["reason_code"] == "gc_request_invalid"


def test_gc_preview_rejects_unsupported_request_version_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    request = _request_bytes({**_request_value(), "schema_version": 2})

    class ForbiddenGc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("unsupported request must not reach the runtime")

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=ForbiddenGc()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(request)))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    result = _result_payload(stderr)
    assert result["state"] == "unsupported"
    assert result["reason_code"] == "gc_request_unsupported"
    assert result["action_code"] == "use_supported_gc_contract"


def test_gc_preview_passes_only_explicit_authority_and_emits_canonical_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    gc_module = _gc_module()
    request = _request_value(
        protection=replace(
            EMPTY_PROTECTION,
            proof_generations=frozenset({"gen-proof"}),
        )
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Gc:
        def preview(self, *args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return gc_module.GcPreview(
                repo_uuid=REPO_UUID,
                registry_revision=1,
                active_source_revision=1,
                operation_epoch=2,
                migration_epoch=0,
                pointer_revision=1,
                capacity_policy_sha256=POLICY.sha256,
                candidates=("gen-unused",),
                protected=(("gen-current", ("visible_current",)),),
            )

    monkeypatch.setattr(workspace_cli.time, "monotonic_ns", lambda: 1_000)
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=Gc()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes(request))))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert stderr.getvalue() == ""
    result = _result_payload(stdout)
    assert stdout.getvalue().encode("utf-8") == canonical_json_bytes(result)
    assert result == {
        **_result_common(),
        "capacity_policy_sha256": POLICY.sha256,
        "candidates": ["gen-unused"],
        "decision": "preview",
        "exit_code": 0,
        "observation_boundary": "locked_double_snapshot",
        "observed": {
            "active_source_revision": 1,
            "migration_epoch": 0,
            "operation_epoch": 2,
            "pointer_revision": 1,
            "registry_revision": 1,
        },
        "protected": [
            {"generation_id": "gen-current", "reasons": ["visible_current"]}
        ],
        "reason_code": "preview_ready",
        "repo_uuid": REPO_UUID,
        "state": "previewed",
    }
    assert calls == [
        (
            (REPO_UUID,),
            {
                "capacity_policy": POLICY,
                "deadline_ns": 5_000_001_000,
                "expected_active_source_revision": 1,
                "expected_migration_epoch": 0,
                "expected_operation_epoch": 2,
                "expected_pointer_revision": 1,
                "expected_registry_revision": 1,
                "protections": replace(
                    EMPTY_PROTECTION,
                    proof_generations=frozenset({"gen-proof"}),
                ),
            },
        )
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: replace(
            result,
            repo_uuid="22222222-2222-4222-8222-222222222222",
        ),
        lambda result: replace(result, registry_revision=2),
        lambda result: replace(result, active_source_revision=2),
        lambda result: replace(result, operation_epoch=3),
        lambda result: replace(result, migration_epoch=1),
        lambda result: replace(result, pointer_revision=2),
        lambda result: replace(result, capacity_policy_sha256="0" * 64),
    ],
)
def test_gc_preview_rejects_results_not_bound_to_the_request(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    workspace_cli = _cli()
    base = _gc_module().GcPreview(
        repo_uuid=REPO_UUID,
        registry_revision=1,
        active_source_revision=1,
        operation_epoch=2,
        migration_epoch=0,
        pointer_revision=1,
        capacity_policy_sha256=POLICY.sha256,
        candidates=("gen-unused",),
        protected=(("gen-current", ("visible_current",)),),
    )

    class Gc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            return mutate(base)

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=Gc()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    assert _result_payload(stderr) == {
        **_result_common(),
        "action_code": "run_workspace_doctor",
        "decision": "withhold",
        "exit_code": 20,
        "observation_boundary": "not_observed",
        "reason_code": "gc_result_invalid",
        "state": "invalid",
    }


@pytest.mark.parametrize(
    ("error_factory", "expected"),
    [
        (
            lambda module: module.GcPreviewAuthorityConflict(
                "/private/state provider-secret"
            ),
            (10, "conflict", "gc_authority_conflict", "refresh_gc_request", "not_observed"),
        ),
        (
            lambda module: module.GcPreviewUnstable(
                "/private/state provider-secret"
            ),
            (10, "withheld", "gc_observation_unstable", "retry_gc_preview", "unstable"),
        ),
        (
            lambda _module: LockTimeout(
                "/private/lock provider-secret",
                phase="acquire",
                kind="workspace",
            ),
            (10, "withheld", "gc_coordination_contended", "retry_gc_preview", "not_observed"),
        ),
        (
            lambda module: module.GcCoordinationUnavailable(
                "/private/lock provider-secret"
            ),
            (20, "invalid", "gc_coordination_unavailable", "run_workspace_repair", "not_observed"),
        ),
        (
            lambda _module: GcRecoveryRequired(
                "/private/intent provider-secret"
            ),
            (20, "invalid", "gc_recovery_required", "run_workspace_repair", "not_observed"),
        ),
        (
            lambda _module: StateCorrupt("/private/state provider-secret"),
            (20, "invalid", "state_corrupt", "run_workspace_repair", "not_observed"),
        ),
        (
            lambda _module: PointerCorrupt("/private/pointer provider-secret"),
            (20, "invalid", "state_corrupt", "run_workspace_repair", "not_observed"),
        ),
    ],
)
def test_gc_preview_failures_are_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Any,
    expected: tuple[int, str, str, str, str],
) -> None:
    workspace_cli = _cli()
    error = error_factory(_gc_module())

    class Gc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            raise error

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(gc=Gc()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == expected[0]
    assert stdout.getvalue() == ""
    result = _result_payload(stderr)
    assert stderr.getvalue().encode("utf-8") == canonical_json_bytes(result)
    assert result["state"] == expected[1]
    assert result["reason_code"] == expected[2]
    assert result["action_code"] == expected[3]
    assert result["observation_boundary"] == expected[4]
    assert "/private" not in stderr.getvalue()
    assert "provider-secret" not in stderr.getvalue()
    assert "repo_uuid" not in result
    assert "candidates" not in result
    assert "protected" not in result


@pytest.mark.parametrize("phase", ["compose", "preview"])
def test_gc_preview_reraises_injected_fault(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    workspace_cli = _cli()
    injected = InjectedFault(f"gc-{phase}")

    class Gc:
        def preview(self, *_args: object, **_kwargs: object) -> object:
            raise injected

    if phase == "compose":
        monkeypatch.setattr(
            workspace_cli,
            "compose_workspace_runtime",
            lambda _inputs: (_ for _ in ()).throw(injected),
        )
    else:
        monkeypatch.setattr(
            workspace_cli,
            "compose_workspace_runtime",
            lambda _inputs: SimpleNamespace(gc=Gc()),
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            SimpleNamespace(buffer=BytesIO(_request_bytes())),
        )
    with pytest.raises(InjectedFault) as raised:
        workspace_cli.run_workspace_command(
            ("gc", "--dry-run", "--request-stdin"),
            inputs=object(),
            stdout=StringIO(),
            stderr=StringIO(),
        )
    assert raised.value is injected


def test_gc_preview_with_real_runtime_is_unfenced_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    harness, _generations, pointers, _receipts = _runtime(tmp_path / "fixture")
    request = _request_for_runtime(harness, pointers)
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    checkout = tmp_path / "checkout"
    state_home = tmp_path / "xdg-state"
    home.mkdir()
    codex_home.mkdir()
    checkout.mkdir()
    state_home.mkdir()
    installed_state = state_home / "graphify"
    harness.state_root.rename(installed_state)
    authority = installed_state / "runtime-manifest.json"
    authority.write_bytes(
        WorkspaceRuntimeAuthority(
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=_SEMANTIC_QUEUE_POLICY,
        ).canonical
    )
    authority.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.chdir(checkout)
    gc_directory = installed_state / "workspaces" / REPO_UUID / "gc"
    gc_directory.mkdir(mode=0o700)
    orphan = gc_directory / f".intent.json.tmp-123-{'a' * 32}"
    orphan.write_bytes(b"do-not-clean")
    orphan.chmod(0o600)
    before = {
        "source_tree": tree_snapshot(harness.repo),
        "source_metadata": metadata_snapshot(harness.repo),
        "state_tree": tree_snapshot(state_home),
        "state_metadata": metadata_snapshot(state_home),
        "home_tree": tree_snapshot(home),
        "home_metadata": metadata_snapshot(home),
        "codex_tree": tree_snapshot(codex_home),
        "codex_metadata": metadata_snapshot(codex_home),
        "checkout_tree": tree_snapshot(checkout),
        "checkout_metadata": metadata_snapshot(checkout),
    }
    monkeypatch.setattr(
        type(harness.leases),
        "current_owner",
        lambda _self: pytest.fail("preview must not derive or validate a lease owner"),
    )
    rejecting_syscalls = _RejectingWriteSyscalls()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: load_workspace_runtime_inputs(
            environ=os.environ,
            capabilities=SUPPORTED,
            syscalls=rejecting_syscalls,
        ),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(_request_bytes(request))),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert stderr.getvalue() == ""
    result = _result_payload(stdout)
    assert result["candidates"] == ["gen-unused"]
    assert result["protected"] == [
        {"generation_id": "gen-current", "reasons": ["visible_current"]}
    ]
    assert "fence_token" not in stdout.getvalue()
    assert "lease" not in stdout.getvalue()
    assert orphan.read_bytes() == b"do-not-clean"
    assert {
        "source_tree": tree_snapshot(harness.repo),
        "source_metadata": metadata_snapshot(harness.repo),
        "state_tree": tree_snapshot(state_home),
        "state_metadata": metadata_snapshot(state_home),
        "home_tree": tree_snapshot(home),
        "home_metadata": metadata_snapshot(home),
        "codex_tree": tree_snapshot(codex_home),
        "codex_metadata": metadata_snapshot(codex_home),
        "checkout_tree": tree_snapshot(checkout),
        "checkout_metadata": metadata_snapshot(checkout),
    } == before


@pytest.mark.parametrize(
    "field",
    [
        "expected_registry_revision",
        "expected_active_source_revision",
        "expected_operation_epoch",
        "expected_migration_epoch",
        "expected_pointer_revision",
    ],
)
def test_gc_preview_stale_cas_fails_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    workspace_cli = _cli()
    harness, _generations, pointers, _receipts = _runtime(tmp_path)
    runtime = _guarded_runtime(harness)
    request = _request_for_runtime(harness, pointers)
    expected_revision = request[field]
    assert isinstance(expected_revision, int)
    request[field] = expected_revision + 1
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: runtime,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(_request_bytes(request))),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 10
    assert stdout.getvalue() == ""
    assert _result_payload(stderr)["reason_code"] == "gc_authority_conflict"
    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata


def test_gc_preview_rejects_missing_protected_generation_lock_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    harness, generations, pointers, _receipts = _runtime(tmp_path)
    runtime = _guarded_runtime(harness)
    protection = replace(
        EMPTY_PROTECTION,
        fixture_generations=frozenset({"gen-unused"}),
    )
    request = _request_for_runtime(harness, pointers, protection=protection)
    generation_lock = generations.state.path(
        generations._lock(REPO_UUID, "gen-unused")
    )
    generation_lock.unlink()
    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: runtime,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(_request_bytes(request))),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    assert _result_payload(stderr)["reason_code"] == "gc_coordination_unavailable"
    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata


@pytest.mark.parametrize(
    ("scenario", "expected_exit", "expected_reason"),
    [
        ("workspace_pending", 20, "gc_recovery_required"),
        ("pointer_pending", 20, "gc_recovery_required"),
        ("pointer_corrupt", 20, "state_corrupt"),
        ("gc_intent", 20, "gc_recovery_required"),
        ("unstable", 10, "gc_observation_unstable"),
    ],
)
def test_gc_preview_real_failure_paths_write_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_exit: int,
    expected_reason: str,
) -> None:
    workspace_cli = _cli()
    harness, _generations, pointers, _receipts = _runtime(tmp_path)
    request = _request_for_runtime(harness, pointers)
    runtime = _guarded_runtime(harness)

    workspace = harness.state_root / "workspaces" / REPO_UUID
    if scenario == "workspace_pending":
        pending = workspace / "workspace.pending.json"
        pending.write_bytes((workspace / "workspace.json").read_bytes())
        pending.chmod(0o600)
    elif scenario == "pointer_pending":
        pending = workspace / "pointers.pending.json"
        pending.write_bytes((workspace / "pointers.json").read_bytes())
        pending.chmod(0o600)
    elif scenario == "pointer_corrupt":
        pointer = workspace / "pointers.json"
        pointer.write_bytes(b"not-json")
        pointer.chmod(0o600)
    elif scenario == "gc_intent":
        gc_directory = workspace / "gc"
        gc_directory.mkdir(mode=0o700)
        intent = gc_directory / "intent.json"
        intent.write_bytes(
            GcIntentState(
                repo_uuid=REPO_UUID,
                operation_epoch=2,
                fence_token=1,
                active_source_revision=1,
                migration_epoch=0,
                pointer_revision=1,
                capacity_policy_sha256=POLICY.sha256,
                plan_sha256="0" * 64,
                candidates=("gen-unused",),
                occurred_at="2026-07-16T19:00:00Z",
            ).canonical
        )
        intent.chmod(0o600)
    elif scenario == "unstable":
        original = runtime.gc._reachability_locked
        observations = 0

        def unstable(*args: object, **kwargs: object) -> object:
            nonlocal observations
            observations += 1
            reachability = original(*args, **kwargs)
            if observations == 2:
                return replace(reachability, candidates=())
            return reachability

        monkeypatch.setattr(runtime.gc, "_reachability_locked", unstable)
    else:  # pragma: no cover - constrained by parametrization
        raise AssertionError(f"unknown GC preview scenario: {scenario}")

    before_tree = tree_snapshot(harness.state_root)
    before_metadata = metadata_snapshot(harness.state_root)
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: runtime,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(_request_bytes(request))),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--dry-run", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == expected_exit
    assert stdout.getvalue() == ""
    result = _result_payload(stderr)
    assert result["reason_code"] == expected_reason
    assert "repo_uuid" not in result
    assert tree_snapshot(harness.state_root) == before_tree
    assert metadata_snapshot(harness.state_root) == before_metadata


def test_top_level_help_lists_the_gc_lifecycle_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])
    mainmod._run_cli()
    help_text = capsys.readouterr().out
    assert "workspace gc --dry-run --request-stdin" in help_text
    assert "workspace gc --execute --request-stdin" in help_text
    assert "workspace gc --reconcile --request-stdin" in help_text
    assert "workspace gc --purge --request-stdin" in help_text


def test_real_module_gc_missing_authority_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    checkout = tmp_path / "checkout"
    state_home = tmp_path / "xdg-state"
    home.mkdir()
    codex_home.mkdir()
    checkout.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "HOME": str(home),
            "PYTHONPATH": str(Path(__file__).parents[1]),
            "XDG_STATE_HOME": str(state_home),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphify",
            "workspace",
            "gc",
            "--dry-run",
            "--request-stdin",
        ],
        cwd=checkout,
        env=environment,
        input=_request_bytes(),
        check=False,
        capture_output=True,
    )

    assert result.returncode == 20
    assert result.stdout == b""
    assert json.loads(result.stderr) == {
        **_result_common(),
        "action_code": "install_candidate_authority",
        "decision": "withhold",
        "exit_code": 20,
        "observation_boundary": "not_observed",
        "reason_code": "runtime_authority_missing",
        "state": "invalid",
    }
    assert not state_home.exists()
    assert list(home.iterdir()) == []
    assert list(codex_home.iterdir()) == []
    assert list(checkout.iterdir()) == []


def test_top_level_gc_skips_ambient_install_version_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("bounded GC preview must not inspect ambient installs"),
    )
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", "workspace", "gc", "--dry-run", "--request-stdin"],
    )
    mainmod._run_cli()
    assert observed == ["workspace"]


@pytest.mark.parametrize("help_flag", ["-h", "--help", "-?"])
def test_top_level_gc_forwards_help_flags_to_exact_argv_validation(
    monkeypatch: pytest.MonkeyPatch,
    help_flag: str,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(sys, "argv", ["graphify", "workspace", "gc", help_flag])
    mainmod._run_cli()
    assert observed == ["workspace"]


def _gc_command() -> Any:
    """Import the fenced lifecycle boundary lazily for tests-first delivery."""

    return importlib.import_module("graphify.workspace.gc_command")


def _gc_lifecycle_request_value(
    operation: str,
    *,
    authorization_action: str | None = None,
) -> dict[str, JsonValue]:
    request = _request_value()
    request.update(
        {
            "contract": f"graphify.workspace.gc_{operation}_request",
            "authorization": {
                "action": authorization_action or f"GC_{operation.upper()}",
                "issued_at": "2026-07-28T12:00:00Z",
                "nonce": f"gc-{operation}-cli-test",
                "operator_id": "operator:gc-cli-test",
                "reason": "private gc lifecycle authorization",
            },
        }
    )
    if operation == "execute":
        request["approved_preview_sha256"] = "a" * 64
    elif operation == "purge":
        request["expected_plan_sha256"] = "b" * 64
    return request


@pytest.mark.parametrize(
    ("arguments", "usage"),
    [
        (("gc", "--execute"), _GC_EXECUTE_USAGE),
        (("gc", "--execute", "--request-stdin", "extra"), _GC_EXECUTE_USAGE),
        (("gc", "--request-stdin", "--execute"), _GC_EXECUTE_USAGE),
        (("gc", "--execute", "--execute", "--request-stdin"), _GC_EXECUTE_USAGE),
        (("gc", "--reconcile"), _GC_RECONCILE_USAGE),
        (("gc", "--reconcile", "--request-stdin", "extra"), _GC_RECONCILE_USAGE),
        (("gc", "--request-stdin", "--reconcile"), _GC_RECONCILE_USAGE),
        (("gc", "--purge"), _GC_PURGE_USAGE),
        (("gc", "--purge", "--request-stdin", "extra"), _GC_PURGE_USAGE),
        (("gc", "--request-stdin", "--purge"), _GC_PURGE_USAGE),
    ],
)
def test_gc_lifecycle_usage_is_exact_and_precedes_authority_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    usage: str,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("malformed GC lifecycle argv must not load authority"),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("malformed GC lifecycle argv must not read stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == usage + "\n"


@pytest.mark.parametrize(
    ("arguments", "operation"),
    [
        (("gc", "--execute", "--request-stdin"), "execute"),
        (("gc", "--reconcile", "--request-stdin"), "reconcile"),
        (("gc", "--purge", "--request-stdin"), "purge"),
    ],
)
def test_gc_lifecycle_authority_composes_before_stdin(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    operation: str,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: None)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail(f"{operation} authority failure must precede stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 20
    assert stdout.getvalue() == ""


def test_gc_lifecycle_stdin_read_stops_at_the_public_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    inputs = WorkspaceRuntimeInputs(
        state_root=harness.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=_SEMANTIC_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )
    command = _gc_command()

    class EndlessBinaryInput:
        def __init__(self) -> None:
            self.read_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return b"x" * size

    binary_input = EndlessBinaryInput()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=binary_input))
    before = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )
    stdout, stderr = StringIO(), StringIO()
    assert _cli().run_workspace_command(
        ("gc", "--execute", "--request-stdin"),
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    ) == 20

    assert binary_input.read_sizes == [command.GC_LIFECYCLE_REQUEST_MAX_BYTES + 1]
    assert stdout.getvalue() == ""
    failure = json.loads(stderr.getvalue())
    assert failure["reason_code"] == "gc_execute_request_invalid"
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before


def test_gc_lifecycle_cli_conflict_receipt_is_canonical_redacted_and_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    inputs = WorkspaceRuntimeInputs(
        state_root=harness.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=_SEMANTIC_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )
    runtime = compose_workspace_runtime(inputs)
    stale_request = _approved_execute_request(runtime)
    advancing_grant = acquire(harness, "GC", tick=101)
    harness.leases.release(advancing_grant)
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(stale_request.canonical)),
    )
    before = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )
    stdout, stderr = StringIO(), StringIO()
    assert _cli().run_workspace_command(
        ("gc", "--execute", "--request-stdin"),
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    ) == 10

    assert stdout.getvalue() == ""
    raw = stderr.getvalue().encode()
    failure = json.loads(raw)
    Draft202012Validator(
        _cli().load_gc_execute_result_schema(),
        format_checker=FormatChecker(),
    ).validate(failure)
    assert raw == canonical_json_bytes(failure)
    assert failure == {
        "action_code": "refresh_gc_execute_request",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.gc_execute_result",
        "exit_code": 10,
        "reason_code": "gc_authority_conflict",
        "schema_version": 1,
        "state": "conflict",
    }
    assert not {
        "authorization",
        "completion",
        "fence_token",
        "intent",
        "repo_uuid",
        "request_sha256",
    }.intersection(failure)
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before


@pytest.mark.parametrize(
    ("operation", "request_class", "request_loader", "result_loader"),
    [
        ("execute", "GcExecuteRequest", "load_gc_execute_request_schema", "load_gc_execute_result_schema"),
        ("reconcile", "GcReconcileRequest", "load_gc_reconcile_request_schema", "load_gc_reconcile_result_schema"),
        ("purge", "GcPurgeRequest", "load_gc_purge_request_schema", "load_gc_purge_result_schema"),
    ],
)
def test_gc_lifecycle_contracts_are_bounded_canonical_and_schema_backed(
    operation: str,
    request_class: str,
    request_loader: str,
    result_loader: str,
) -> None:
    workspace_cli = _cli()
    command = _gc_command()
    request_value = _gc_lifecycle_request_value(operation)
    raw = canonical_json_bytes(request_value)
    request_schema = getattr(workspace_cli, request_loader)()
    result_schema = getattr(workspace_cli, result_loader)()
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(result_schema)
    assert not list(Draft202012Validator(request_schema).iter_errors(request_value))
    if operation == "reconcile":
        assert list(
            Draft202012Validator(request_schema).iter_errors(
                {**request_value, "expected_plan_sha256": "b" * 64}
            )
        )

    request_type = getattr(command, request_class)
    request = request_type.from_bytes(raw)
    assert request.to_dict() == request_value
    assert request.request_sha256 == hashlib.sha256(raw).hexdigest()

    for invalid in (
        raw + b" ",
        json.dumps(request_value, indent=2).encode(),
        raw.replace(b'"schema_version":1,', b'"schema_version":1,"repo_uuid":"duplicate",', 1),
        canonical_json_bytes({**request_value, "unexpected": True}),
    ):
        with pytest.raises(ValueError):
            request_type.from_bytes(invalid)
    with pytest.raises(command.GcLifecycleRequestUnsupported):
        request_type.from_bytes(
            canonical_json_bytes({**request_value, "schema_version": 2})
        )
    with pytest.raises(ValueError):
        request_type.from_bytes(
            b" " * (command.GC_LIFECYCLE_REQUEST_MAX_BYTES + 1)
        )


@pytest.mark.parametrize(
    ("operation", "request_class", "other_actions"),
    [
        ("execute", "GcExecuteRequest", ("GC_RECONCILE", "GC_PURGE")),
        ("reconcile", "GcReconcileRequest", ("GC_EXECUTE", "GC_PURGE")),
        ("purge", "GcPurgeRequest", ("GC_EXECUTE", "GC_RECONCILE")),
    ],
)
def test_gc_lifecycle_authorizations_are_operation_specific(
    operation: str,
    request_class: str,
    other_actions: tuple[str, str],
) -> None:
    command = _gc_command()
    request_type = getattr(command, request_class)
    assert request_type.from_bytes(
        canonical_json_bytes(_gc_lifecycle_request_value(operation))
    ).authorization.action.value == f"GC_{operation.upper()}"
    for action in other_actions:
        with pytest.raises(ValueError):
            request_type.from_bytes(
                canonical_json_bytes(
                    _gc_lifecycle_request_value(
                        operation,
                        authorization_action=action,
                    )
                )
            )


@pytest.mark.parametrize(
    ("error", "state", "exit_code", "reason_code", "action_code"),
    [
        (
            WorkspaceAuthorityUnsupported("/private/authority provider-secret"),
            "unsupported",
            20,
            "runtime_authority_unsupported",
            "install_supported_candidate",
        ),
        (
            GcRecoveryRequired("/private/intent provider-secret"),
            "conflict",
            10,
            "gc_recovery_required",
            "run_workspace_gc_reconcile",
        ),
        (
            LeaseRecoveryRequired("/private/lease provider-secret"),
            "conflict",
            10,
            "workspace_recovery_required",
            "run_workspace_doctor",
        ),
        (
            CommitUnknown("/private/commit provider-secret"),
            "invalid",
            20,
            "commit_unknown",
            "run_workspace_gc_reconcile",
        ),
    ],
)
def test_gc_lifecycle_failures_are_stable_redacted_and_schema_valid(
    error: Exception,
    state: str,
    exit_code: int,
    reason_code: str,
    action_code: str,
) -> None:
    command = _gc_command()
    failure = command.classify_failure(error, "execute")
    value = failure.to_dict()
    assert value == {
        "action_code": action_code,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.gc_execute_result",
        "exit_code": exit_code,
        "reason_code": reason_code,
        "schema_version": 1,
        "state": state,
    }
    Draft202012Validator(
        _cli().load_gc_execute_result_schema(),
        format_checker=FormatChecker(),
    ).validate(value)
    assert failure.canonical == canonical_json_bytes(value)
    assert "/private" not in failure.canonical.decode()
    assert "provider-secret" not in failure.canonical.decode()


@pytest.mark.parametrize(
    ("operation", "error", "expected"),
    [
        (
            "execute",
            GcCoordinationUnavailable("/private/coordination provider-secret"),
            ("invalid", 20, "gc_coordination_unavailable", "run_workspace_repair"),
        ),
        (
            "execute",
            GcRecoveryRequired("/private/intent provider-secret"),
            ("conflict", 10, "gc_recovery_required", "run_workspace_gc_reconcile"),
        ),
        (
            "reconcile",
            GcRecoveryRequired("/private/intent provider-secret"),
            ("invalid", 20, "gc_recovery_required", "run_workspace_repair"),
        ),
        (
            "purge",
            CommitUnknown("/private/purge provider-secret"),
            ("invalid", 20, "commit_unknown", "retry_workspace_gc_purge"),
        ),
    ],
)
def test_gc_lifecycle_recovery_actions_match_the_failure_boundary(
    operation: str,
    error: Exception,
    expected: tuple[str, int, str, str],
) -> None:
    value = _gc_command().classify_failure(error, operation).to_dict()

    assert (
        value["state"],
        value["exit_code"],
        value["reason_code"],
        value["action_code"],
    ) == expected


@pytest.mark.parametrize(
    ("operation", "result_loader", "state", "operation_field"),
    [
        ("execute", "load_gc_execute_result_schema", "quarantined", "quarantined"),
        ("reconcile", "load_gc_reconcile_result_schema", "reconciled", "quarantined"),
        ("purge", "load_gc_purge_result_schema", "purged", "purged"),
    ],
)
def test_gc_lifecycle_results_are_canonical_redacted_public_receipts(
    operation: str,
    result_loader: str,
    state: str,
    operation_field: str,
) -> None:
    workspace_cli = _cli()
    result = {
        "cli_contract_version": 1,
        "contract": f"graphify.workspace.gc_{operation}_result",
        "exit_code": 0,
        "plan_sha256": "b" * 64,
        "repo_uuid": REPO_UUID,
        "request_sha256": "c" * 64,
        "schema_version": 1,
        "state": state,
        operation_field: ["gen-unused"],
    }
    if operation == "execute":
        result["approved_preview_sha256"] = "a" * 64
    schema = getattr(workspace_cli, result_loader)()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert not list(validator.iter_errors(result))
    for private_field in ("authorization", "completion", "fence_token", "intent"):
        assert list(validator.iter_errors({**result, private_field: {}})), private_field


def test_gc_execute_request_binds_the_exact_canonical_preview_result_bytes() -> None:
    command = _gc_command()
    preview_payload = canonical_json_bytes(
        {
            **_result_common(),
            "capacity_policy_sha256": POLICY.sha256,
            "candidates": ["gen-unused"],
            "decision": "preview",
            "exit_code": 0,
            "observation_boundary": "locked_double_snapshot",
            "observed": {
                "active_source_revision": 1,
                "migration_epoch": 0,
                "operation_epoch": 2,
                "pointer_revision": 1,
                "registry_revision": 1,
            },
            "protected": [],
            "reason_code": "preview_ready",
            "repo_uuid": REPO_UUID,
            "state": "previewed",
        }
    )
    request = _gc_lifecycle_request_value("execute")
    request["approved_preview_sha256"] = hashlib.sha256(preview_payload).hexdigest()
    parsed = command.GcExecuteRequest.from_bytes(canonical_json_bytes(request))
    assert parsed.approved_preview_sha256 == request["approved_preview_sha256"]
    stale = dict(request)
    stale["approved_preview_sha256"] = "0" * 64
    assert command.GcExecuteRequest.from_bytes(canonical_json_bytes(stale)).approved_preview_sha256 != parsed.approved_preview_sha256


def _gc_mutation_runtime(harness: Any, *, fault_hook: Any = None) -> Any:
    return compose_workspace_runtime(
        WorkspaceRuntimeInputs(
            state_root=harness.state_root,
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=_SEMANTIC_QUEUE_POLICY,
            capabilities=SUPPORTED,
            fault_hook=fault_hook,
        )
    )


def _gc_live_lifecycle_request(
    runtime: Any,
    operation: str,
    *,
    approved_preview_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
    protection: GcProtection = EMPTY_PROTECTION,
) -> dict[str, JsonValue]:
    registry_value = runtime.registry.load().to_dict()
    entry = registry_value["workspaces"][0]
    lease_state = runtime.leases.inspect(REPO_UUID)
    pointer = runtime.pointers.load(REPO_UUID)
    assert pointer is not None
    request = _request_value(
        expected_registry_revision=int(registry_value["revision"]),
        expected_active_source_revision=int(entry["active_source_revision"]),
        expected_operation_epoch=lease_state.operation_epoch,
        expected_migration_epoch=lease_state.migration_epoch,
        expected_pointer_revision=int(pointer.to_dict()["pointer_revision"]),
        protection=protection,
    )
    request.update(
        {
            "authorization": {
                "action": f"GC_{operation.upper()}",
                "issued_at": "2026-07-28T12:00:00Z",
                "nonce": f"gc-{operation}-e2e",
                "operator_id": "operator:gc-e2e",
                "reason": "explicit disposable GC lifecycle proof",
            },
            "contract": f"graphify.workspace.gc_{operation}_request",
        }
    )
    if approved_preview_sha256 is not None:
        request["approved_preview_sha256"] = approved_preview_sha256
    if expected_plan_sha256 is not None:
        request["expected_plan_sha256"] = expected_plan_sha256
    return request


def _approved_execute_request(runtime: Any) -> Any:
    command = _gc_command()
    request_value = _gc_live_lifecycle_request(runtime, "execute")
    preview = runtime.gc.preview(
        REPO_UUID,
        expected_registry_revision=cast(int, request_value["expected_registry_revision"]),
        expected_active_source_revision=cast(
            int, request_value["expected_active_source_revision"]
        ),
        expected_operation_epoch=cast(int, request_value["expected_operation_epoch"]),
        expected_migration_epoch=cast(int, request_value["expected_migration_epoch"]),
        expected_pointer_revision=cast(int, request_value["expected_pointer_revision"]),
        capacity_policy=POLICY,
        protections=EMPTY_PROTECTION,
        deadline_ns=time.monotonic_ns() + 5_000_000_000,
    )
    request_value["approved_preview_sha256"] = hashlib.sha256(
        command.gc_preview_result_bytes(preview)
    ).hexdigest()
    return command.GcExecuteRequest.from_bytes(canonical_json_bytes(request_value))


def test_gc_execute_requires_exact_approved_preview_before_any_lease_or_mutation(
    tmp_path: Path,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    request_value = _gc_live_lifecycle_request(
        runtime,
        "execute",
        approved_preview_sha256="0" * 64,
    )
    request = _gc_command().GcExecuteRequest.from_bytes(
        canonical_json_bytes(request_value)
    )
    before = tree_snapshot(harness.state_root)

    with pytest.raises(ValueError, match="approved preview"):
        _gc_command().execute_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    assert tree_snapshot(harness.state_root) == before


def test_gc_execute_rejects_stale_request_authority_before_any_new_mutation(
    tmp_path: Path,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    stale_execute_request = _approved_execute_request(runtime)
    advancing_grant = acquire(harness, "GC", tick=100)
    harness.leases.release(advancing_grant)
    before = tree_snapshot(harness.state_root)

    with pytest.raises(GcPreviewAuthorityConflict):
        _gc_command().execute_gc(
            runtime,
            stale_execute_request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    assert tree_snapshot(harness.state_root) == before


def test_gc_execute_rejects_fresh_plan_non_fence_projection_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    request = _approved_execute_request(runtime)
    original_plan = runtime.gc.plan

    def drift_plan(*args: object, **kwargs: object) -> object:
        plan = original_plan(*args, **kwargs)
        return replace(plan, candidates=())

    monkeypatch.setattr(runtime.gc, "plan", drift_plan)
    monkeypatch.setattr(
        runtime.gc,
        "execute",
        lambda *_args, **_kwargs: pytest.fail(
            "non-fence projection drift must not reach GC execute"
        ),
    )
    generation_root = (
        harness.state_root / "workspaces" / REPO_UUID / "generations"
    )
    quarantine_root = harness.state_root / "workspaces" / REPO_UUID / "quarantine"
    before = (tree_snapshot(generation_root), tree_snapshot(quarantine_root))

    with pytest.raises(command.GcPreviewPlanMismatch):
        command.execute_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    assert (tree_snapshot(generation_root), tree_snapshot(quarantine_root)) == before


def test_gc_reconcile_and_purge_reject_stale_pointer_before_lease_mutation(
    tmp_path: Path,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    reconcile_value = _gc_live_lifecycle_request(runtime, "reconcile")
    reconcile_value["expected_pointer_revision"] = cast(
        int,
        reconcile_value["expected_pointer_revision"],
    ) + 1
    stale_reconcile = command.GcReconcileRequest.from_bytes(
        canonical_json_bytes(reconcile_value)
    )
    before_reconcile = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )

    with pytest.raises(GcPreviewAuthorityConflict, match="pointer revision"):
        command.reconcile_gc(
            runtime,
            stale_reconcile,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before_reconcile

    executed = command.execute_gc(
        runtime,
        _approved_execute_request(runtime),
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    plan_sha256 = cast(str, executed.to_dict()["plan_sha256"])
    purge_value = _gc_live_lifecycle_request(
        runtime,
        "purge",
        expected_plan_sha256=plan_sha256,
    )
    purge_value["expected_pointer_revision"] = cast(
        int,
        purge_value["expected_pointer_revision"],
    ) + 1
    stale_purge = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(purge_value)
    )
    quarantine_root = harness.state_root / "workspaces" / REPO_UUID / "quarantine"
    before_purge = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
        tree_snapshot(quarantine_root),
    )

    with pytest.raises(GcPreviewAuthorityConflict, match="pointer revision"):
        command.purge_gc(
            runtime,
            stale_purge,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
        tree_snapshot(quarantine_root),
    ) == before_purge


def test_gc_public_purge_unknown_plan_requires_reselection_without_mutation(
    tmp_path: Path,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256="b" * 64,
            )
        )
    )
    before = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )

    with pytest.raises(GcPlanStale, match="completion is unavailable") as raised:
        command.purge_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    failure = command.classify_failure(raised.value, "purge").to_dict()
    assert failure["exit_code"] == 10
    assert failure["reason_code"] == "gc_authority_conflict"
    assert failure["action_code"] == "refresh_gc_purge_request"
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before


def test_gc_public_purge_malformed_terminal_record_remains_state_corrupt(
    tmp_path: Path,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    executed = command.execute_gc(
        runtime,
        _approved_execute_request(runtime),
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    plan_sha256 = cast(str, executed.to_dict()["plan_sha256"])
    request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
            )
        )
    )
    purge_relative = runtime.gc._purge_path(REPO_UUID, plan_sha256)
    runtime.gc.state.ensure_directory(purge_relative.parent)
    purge_path = runtime.gc.state.path(purge_relative)
    purge_path.write_bytes(b"{}\n")
    purge_path.chmod(0o600)
    before = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )

    with pytest.raises(GcError, match="purge record is invalid") as raised:
        command.purge_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    failure = command.classify_failure(raised.value, "purge").to_dict()
    assert failure["exit_code"] == 20
    assert failure["reason_code"] == "state_corrupt"
    assert failure["action_code"] == "run_workspace_repair"
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before


def test_gc_public_terminal_purge_read_timeout_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    executed = command.execute_gc(
        runtime,
        _approved_execute_request(runtime),
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    plan_sha256 = cast(str, executed.to_dict()["plan_sha256"])
    request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
            )
        )
    )
    command.purge_gc(
        runtime,
        request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    purge_relative = runtime.gc._purge_path(REPO_UUID, plan_sha256)
    original_read = runtime.gc.state.read_optional_existing_bytes

    def timeout_selected_purge(
        relative: str | Path,
        *,
        max_bytes: int | None = None,
        deadline_ns: int | None = None,
    ) -> bytes | None:
        if Path(relative) == purge_relative:
            raise LockTimeout(
                "terminal purge receipt read exceeded its deadline",
                phase="acquire",
                kind="workspace",
            )
        return original_read(
            relative,
            max_bytes=max_bytes,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(
        runtime.gc.state,
        "read_optional_existing_bytes",
        timeout_selected_purge,
    )

    with pytest.raises(LockTimeout) as raised:
        command.purge_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    failure = command.classify_failure(raised.value, "purge").to_dict()
    assert failure["exit_code"] == 10
    assert failure["reason_code"] == "gc_lease_busy"
    assert failure["action_code"] == "retry_workspace_gc_purge"


def test_gc_reconcile_lease_ttl_starts_after_preflight(tmp_path: Path) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    interrupted = False

    def leave_durable_intent(event: str) -> None:
        nonlocal interrupted
        if event == "gc:intent_durable" and not interrupted:
            interrupted = True
            raise InjectedFault(event)

    runtime = _gc_mutation_runtime(harness, fault_hook=leave_durable_intent)
    with pytest.raises((CommitUnknown, InjectedFault)):
        _gc_command().execute_gc(
            runtime,
            _approved_execute_request(runtime),
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )
    assert interrupted is True

    recovery_runtime = _gc_mutation_runtime(harness)
    request_value = _gc_live_lifecycle_request(recovery_runtime, "reconcile")
    request_value["timeout_ms"] = 60_000
    request = _gc_command().GcReconcileRequest.from_bytes(
        canonical_json_bytes(request_value)
    )
    started_ns = time.monotonic_ns()
    ticks = iter(
        (
            started_ns,
            started_ns + 31_000_000_000,
            started_ns + 31_000_000_001,
        )
    )

    result = _gc_command().reconcile_gc(
        recovery_runtime,
        request,
        occurred_at=START,
        monotonic_clock=lambda: next(ticks),
    )

    assert result.to_dict()["state"] == "reconciled"


def test_gc_purge_lease_ttl_starts_after_preflight(tmp_path: Path) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    executed = _gc_command().execute_gc(
        runtime,
        _approved_execute_request(runtime),
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    request_value = _gc_live_lifecycle_request(
        runtime,
        "purge",
        expected_plan_sha256=cast(str, executed.to_dict()["plan_sha256"]),
    )
    request_value["timeout_ms"] = 60_000
    request = _gc_command().GcPurgeRequest.from_bytes(
        canonical_json_bytes(request_value)
    )
    started_ns = time.monotonic_ns()
    ticks = iter(
        (
            started_ns,
            started_ns + 31_000_000_000,
            started_ns + 31_000_000_001,
        )
    )

    result = _gc_command().purge_gc(
        runtime,
        request,
        occurred_at=START,
        monotonic_clock=lambda: next(ticks),
    )

    assert result.to_dict()["purged"] == ["gen-unused"]


def test_gc_public_lifecycle_executes_reconciles_idempotently_and_purges_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    execute_request = _approved_execute_request(runtime)

    executed = command.execute_gc(
        runtime,
        execute_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    execute_value = executed.to_dict()
    assert execute_value["state"] == "quarantined"
    assert execute_value["quarantined"] == ["gen-unused"]
    assert "fence_token" not in execute_value

    reconcile_request = command.GcReconcileRequest.from_bytes(
        canonical_json_bytes(_gc_live_lifecycle_request(runtime, "reconcile"))
    )
    before_reconcile = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )
    reconciled = command.reconcile_gc(
        runtime,
        reconcile_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    assert reconciled.to_dict()["state"] == "nothing_to_reconcile"
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before_reconcile

    purge_request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=cast(str, execute_value["plan_sha256"]),
            )
        )
    )
    purged = command.purge_gc(
        runtime,
        purge_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    assert purged.to_dict()["purged"] == ["gen-unused"]

    before_retry = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )
    monkeypatch.setattr(
        command,
        "_acquire_gc",
        lambda *_args, **_kwargs: pytest.fail(
            "exact terminal purge replay must not acquire a lease"
        ),
    )
    retried = command.purge_gc(
        runtime,
        purge_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    assert retried.to_dict()["purged"] == ["gen-unused"]
    assert retried.request_sha256 == purge_request.request_sha256
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before_retry


def test_gc_lifecycle_cli_dispatches_canonical_success_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    inputs = WorkspaceRuntimeInputs(
        state_root=harness.state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=_SEMANTIC_QUEUE_POLICY,
        capabilities=SUPPORTED,
    )
    runtime = compose_workspace_runtime(inputs)
    workspace_cli = _cli()
    execute_request = _approved_execute_request(runtime)
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(execute_request.canonical)),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--execute", "--request-stdin"),
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert stderr.getvalue() == ""
    execute_value = json.loads(stdout.getvalue())
    Draft202012Validator(
        workspace_cli.load_gc_execute_result_schema(),
        format_checker=FormatChecker(),
    ).validate(execute_value)

    reconcile_request = _gc_command().GcReconcileRequest.from_bytes(
        canonical_json_bytes(_gc_live_lifecycle_request(runtime, "reconcile"))
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(reconcile_request.canonical)),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--reconcile", "--request-stdin"),
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert stderr.getvalue() == ""
    reconcile_value = json.loads(stdout.getvalue())
    Draft202012Validator(
        workspace_cli.load_gc_reconcile_result_schema(),
        format_checker=FormatChecker(),
    ).validate(reconcile_value)
    assert reconcile_value["state"] == "nothing_to_reconcile"

    purge_request = _gc_command().GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=cast(str, execute_value["plan_sha256"]),
            )
        )
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(purge_request.canonical)),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("gc", "--purge", "--request-stdin"),
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert stderr.getvalue() == ""
    purge_value = json.loads(stdout.getvalue())
    Draft202012Validator(
        workspace_cli.load_gc_purge_result_schema(),
        format_checker=FormatChecker(),
    ).validate(purge_value)
    assert purge_value["purged"] == ["gen-unused"]


def test_gc_public_purge_rejects_reprotected_quarantine_then_allows_fresh_retry(
    tmp_path: Path,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    executed = _gc_command().execute_gc(
        runtime,
        _approved_execute_request(runtime),
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    plan_sha256 = cast(str, executed.to_dict()["plan_sha256"])
    quarantine_root = harness.state_root / "workspaces" / REPO_UUID / "quarantine"
    quarantine_before = tree_snapshot(quarantine_root)
    protected = replace(
        EMPTY_PROTECTION,
        proof_generations=frozenset({"gen-unused"}),
    )
    protected_request = _gc_command().GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
                protection=protected,
            )
        )
    )

    with pytest.raises(GcPlanStale, match="became protected"):
        _gc_command().purge_gc(
            runtime,
            protected_request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    assert tree_snapshot(quarantine_root) == quarantine_before
    assert not (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "gc"
        / "purges"
        / f"{plan_sha256}.json"
    ).exists()
    retry = _gc_command().GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
            )
        )
    )
    assert _gc_command().purge_gc(
        runtime,
        retry,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    ).to_dict()["purged"] == ["gen-unused"]


def test_gc_public_lifecycle_single_disposable_proof_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, generations, _pointers, _receipts = _runtime(tmp_path)
    outside_roots = {
        "source": harness.repo,
        "home": tmp_path / "home",
        "xdg": tmp_path / "xdg",
        "codex": tmp_path / "codex",
    }
    for root in outside_roots.values():
        root.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(outside_roots["home"]))
    monkeypatch.setenv("XDG_STATE_HOME", str(outside_roots["xdg"]))
    monkeypatch.setenv("CODEX_HOME", str(outside_roots["codex"]))
    outside_before = {
        label: (tree_snapshot(root), metadata_snapshot(root))
        for label, root in outside_roots.items()
    }
    fired = False

    def interrupt_after_quarantine_durability(event: str) -> None:
        nonlocal fired
        if event == "gc:gen-unused:quarantine:destination_parent_durable" and not fired:
            fired = True
            raise InjectedFault(event)

    command = _gc_command()
    runtime = _gc_mutation_runtime(
        harness,
        fault_hook=interrupt_after_quarantine_durability,
    )
    execute_request = _approved_execute_request(runtime)
    approved_bytes = command.gc_preview_result_bytes(
        runtime.gc.preview(
            REPO_UUID,
            expected_registry_revision=execute_request.expected_registry_revision,
            expected_active_source_revision=(
                execute_request.expected_active_source_revision
            ),
            expected_operation_epoch=execute_request.expected_operation_epoch,
            expected_migration_epoch=execute_request.expected_migration_epoch,
            expected_pointer_revision=execute_request.expected_pointer_revision,
            capacity_policy=execute_request.capacity_policy,
            protections=execute_request.protections,
            deadline_ns=time.monotonic_ns() + 5_000_000_000,
        )
    )
    assert hashlib.sha256(approved_bytes).hexdigest() == (
        execute_request.approved_preview_sha256
    )

    lock = generations.state.path(generations._lock(REPO_UUID, "gen-unused"))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDONLY); "
                "fcntl.flock(fd, fcntl.LOCK_SH); print('READY', flush=True); input()"
            ),
            str(lock),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "READY"
    before_locked_attempt = (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    )
    try:
        with pytest.raises(command.GcApprovedPreviewMismatch):
            command.execute_gc(
                runtime,
                execute_request,
                occurred_at=START,
                monotonic_clock=time.monotonic_ns,
            )
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=5)
    assert (
        tree_snapshot(harness.state_root),
        metadata_snapshot(harness.state_root),
    ) == before_locked_attempt

    with pytest.raises((CommitUnknown, InjectedFault)):
        command.execute_gc(
            runtime,
            execute_request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )
    assert fired is True
    assert harness.leases.inspect(REPO_UUID).operation_epoch == (
        execute_request.expected_operation_epoch + 1
    )

    recovery_runtime = _gc_mutation_runtime(harness)
    reconcile_request = command.GcReconcileRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(recovery_runtime, "reconcile")
        )
    )
    reconciled = command.reconcile_gc(
        recovery_runtime,
        reconcile_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    reconcile_value = reconciled.to_dict()
    assert reconcile_value["state"] == "reconciled"
    assert reconcile_value["quarantined"] == ["gen-unused"]
    plan_sha256 = cast(str, reconcile_value["plan_sha256"])

    purge_request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                recovery_runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
            )
        )
    )
    purge_value = command.purge_gc(
        recovery_runtime,
        purge_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    ).to_dict()
    assert purge_value["purged"] == ["gen-unused"]
    retry_request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                recovery_runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
            )
        )
    )
    assert command.purge_gc(
        recovery_runtime,
        retry_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    ).to_dict()["purged"] == ["gen-unused"]

    for value in (reconcile_value, purge_value):
        assert not {
            "authorization",
            "completion",
            "fence_token",
            "intent",
            "operation_epoch",
        }.intersection(value)
    assert {
        label: (tree_snapshot(root), metadata_snapshot(root))
        for label, root in outside_roots.items()
    } == outside_before


def test_gc_execute_preserves_receipt_across_release_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    request = _approved_execute_request(runtime)
    original_release = runtime.leases.release

    def release_then_unknown(grant: Any) -> Never:
        original_release(grant)
        raise CommitUnknown("GC execute lease release acknowledgement was lost")

    monkeypatch.setattr(runtime.leases, "release", release_then_unknown)

    result = command.execute_gc(
        runtime,
        request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )

    value = result.to_dict()
    assert value["state"] == "quarantined"
    assert value["quarantined"] == ["gen-unused"]
    assert not runtime.gc.state.path(runtime.gc._intent_path(REPO_UUID)).exists()
    plan_sha256 = cast(str, value["plan_sha256"])
    purge_runtime = _gc_mutation_runtime(harness)
    purge_request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                purge_runtime,
                "purge",
                expected_plan_sha256=plan_sha256,
            )
        )
    )
    assert command.purge_gc(
        purge_runtime,
        purge_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    ).to_dict()["purged"] == ["gen-unused"]


def test_gc_reconcile_preserves_receipt_across_release_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    interrupted = False

    def leave_durable_intent(event: str) -> None:
        nonlocal interrupted
        if event == "gc:intent_durable" and not interrupted:
            interrupted = True
            raise InjectedFault(event)

    interrupted_runtime = _gc_mutation_runtime(
        harness,
        fault_hook=leave_durable_intent,
    )
    with pytest.raises((CommitUnknown, InjectedFault)):
        _gc_command().execute_gc(
            interrupted_runtime,
            _approved_execute_request(interrupted_runtime),
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )
    assert interrupted is True

    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    request = command.GcReconcileRequest.from_bytes(
        canonical_json_bytes(_gc_live_lifecycle_request(runtime, "reconcile"))
    )
    original_release = runtime.leases.release

    def release_then_unknown(grant: Any) -> Never:
        original_release(grant)
        raise CommitUnknown("GC reconcile lease release acknowledgement was lost")

    monkeypatch.setattr(runtime.leases, "release", release_then_unknown)

    result = command.reconcile_gc(
        runtime,
        request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )

    value = result.to_dict()
    assert value["state"] == "reconciled"
    assert value["quarantined"] == ["gen-unused"]
    assert not runtime.gc.state.path(runtime.gc._intent_path(REPO_UUID)).exists()


def test_gc_purge_release_commit_unknown_requests_exact_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    runtime = _gc_mutation_runtime(harness)
    command = _gc_command()
    executed = command.execute_gc(
        runtime,
        _approved_execute_request(runtime),
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    request = command.GcPurgeRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(
                runtime,
                "purge",
                expected_plan_sha256=cast(str, executed.to_dict()["plan_sha256"]),
            )
        )
    )
    original_release = runtime.leases.release

    def release_then_unknown(grant: Any) -> Never:
        original_release(grant)
        raise CommitUnknown("GC purge lease release acknowledgement was lost")

    monkeypatch.setattr(runtime.leases, "release", release_then_unknown)

    with pytest.raises(CommitUnknown) as raised:
        command.purge_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )

    failure = command.classify_failure(raised.value, "purge").to_dict()
    assert failure["exit_code"] == 20
    assert failure["reason_code"] == "commit_unknown"
    assert failure["action_code"] == "retry_workspace_gc_purge"
    assert command.purge_gc(
        runtime,
        request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    ).to_dict()["purged"] == ["gen-unused"]


@pytest.mark.parametrize("phase", GC_RECOVERY_PHASES)
def test_every_durable_execute_interruption_requires_explicit_public_reconcile(
    tmp_path: Path,
    phase: str,
) -> None:
    harness, _generations, _pointers, _receipts = _runtime(tmp_path)
    fired = False

    def fail_once(event: str) -> None:
        nonlocal fired
        if event == phase and not fired:
            fired = True
            raise InjectedFault(event)

    runtime = _gc_mutation_runtime(harness, fault_hook=fail_once)
    request = _approved_execute_request(runtime)
    with pytest.raises((CommitUnknown, InjectedFault)):
        _gc_command().execute_gc(
            runtime,
            request,
            occurred_at=START,
            monotonic_clock=time.monotonic_ns,
        )
    assert fired is True

    recovery_runtime = _gc_mutation_runtime(harness)
    reconcile_request = _gc_command().GcReconcileRequest.from_bytes(
        canonical_json_bytes(
            _gc_live_lifecycle_request(recovery_runtime, "reconcile")
        )
    )
    result = _gc_command().reconcile_gc(
        recovery_runtime,
        reconcile_request,
        occurred_at=START,
        monotonic_clock=time.monotonic_ns,
    )
    assert result.to_dict()["state"] in {"reconciled", "nothing_to_reconcile"}
