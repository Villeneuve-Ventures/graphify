"""Public P5B2 offline-GC preview CLI contract.

The command exposes only a bounded, read-only reachability preview.  It must
never manufacture the fenced ``GcPlan`` authority required by execution.
"""

from __future__ import annotations

from dataclasses import replace
import importlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
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
from graphify.workspace.gc import GcProtection, GcRecoveryRequired, GcStore
from graphify.workspace.persistence import (
    InjectedFault,
    LockTimeout,
    PosixSyscalls,
    StateCorrupt,
    StatePathError,
    UnsupportedRuntime,
)
from graphify.workspace.pointers import PointerCorrupt
from graphify.workspace.semantic_queue import SemanticQueuePolicy

from tests.test_workspace_gc import EMPTY_PROTECTION, POLICY, _runtime
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    REPO_UUID,
    SUPPORTED,
    metadata_snapshot,
    tree_snapshot,
)


_GC_USAGE = "graphify workspace gc --dry-run --request-stdin"
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


def test_general_workspace_usage_lists_only_the_gc_preview_command() -> None:
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
    assert "workspace gc --execute" not in usage
    assert "workspace gc --purge" not in usage
    assert "workspace gc --reconcile" not in usage


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


def test_top_level_help_lists_only_the_gc_preview_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])
    mainmod._run_cli()
    help_text = capsys.readouterr().out
    assert "workspace gc --dry-run --request-stdin" in help_text
    assert "workspace gc --execute" not in help_text
    assert "workspace gc --purge" not in help_text


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
