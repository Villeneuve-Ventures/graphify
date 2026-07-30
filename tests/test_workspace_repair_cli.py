"""Public CLI-v1 pointer-repair contracts.

These tests intentionally describe the narrow public boundary only.  The
repair engine remains responsible for deriving candidate/last-good selections
and for performing its fenced pointer and journal mutations.
"""

from __future__ import annotations

from io import BytesIO, StringIO
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.contracts import canonical_json_bytes
from tests.workspace_p3_helpers import REPO_UUID


_PREVIEW_USAGE = "graphify workspace repair --dry-run --request-stdin"
_EXECUTE_USAGE = "graphify workspace repair --execute --request-stdin"
_SCHEMA_ROOT = Path(__file__).parents[1] / "graphify/workspace/schemas/cli/v1"


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _repair() -> Any:
    return importlib.import_module("graphify.workspace.repair")


def _preview_request_value() -> dict[str, Any]:
    return {
        "cli_contract_version": 1,
        "contract": "graphify.workspace.repair_preview_request",
        "expected_active_source_revision": 1,
        "expected_migration_epoch": 0,
        "expected_operation_epoch": 7,
        "expected_registry_revision": 2,
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "timeout_ms": 5_000,
    }


def _authorization() -> dict[str, str]:
    return {
        "action": "REPAIR_EXECUTE",
        "issued_at": "2026-07-28T12:00:00Z",
        "nonce": "repair-cli-test",
        "operator_id": "operator:repair-cli-test",
        "reason": "private repair authorization",
    }


def _execute_request_value() -> dict[str, Any]:
    return {
        **_preview_request_value(),
        "approved_preview_sha256": "a" * 64,
        "authorization": _authorization(),
        "contract": "graphify.workspace.repair_execute_request",
    }


def _request_bytes(value: dict[str, Any]) -> bytes:
    return canonical_json_bytes(value)


def test_canonical_result_short_write_names_the_repair_boundary() -> None:
    class ShortBinaryStream:
        def write(self, payload: bytes) -> int:
            return len(payload) - 1

        def flush(self) -> None:
            raise AssertionError("short writes must fail before flush")

    stream = SimpleNamespace(buffer=ShortBinaryStream())

    with pytest.raises(OSError, match="incomplete workspace repair preview result"):
        _cli()._emit_canonical_result(
            stream,
            b"{}",
            exit_code=20,
            result_label="repair preview result",
        )


def _preview_result_value(*, classification: str = "repairable") -> dict[str, Any]:
    if classification == "irreparable":
        plan: dict[str, Any] = {
            "candidate": None,
            "decision_sha256": "d" * 64,
            "journal_actions": [],
            "last_good": None,
            "next_pointer_revision": 0,
            "pointer_action": "none",
            "quarantine": [],
            "selected_from": "none",
        }
    elif classification == "no_op":
        plan = {
            "candidate": {"generation_id": "gen-candidate", "receipt_sha256": "b" * 64},
            "decision_sha256": "d" * 64,
            "journal_actions": [],
            "last_good": {"generation_id": "gen-last-good", "receipt_sha256": "c" * 64},
            "next_pointer_revision": 3,
            "pointer_action": "none",
            "quarantine": [],
            "selected_from": "current",
        }
    else:
        plan = {
            "candidate": {"generation_id": "gen-candidate", "receipt_sha256": "b" * 64},
            "decision_sha256": "d" * 64,
            "journal_actions": ["append_repair"],
            "last_good": {"generation_id": "gen-last-good", "receipt_sha256": "c" * 64},
            "next_pointer_revision": 3,
            "pointer_action": "replace",
            "quarantine": ["gen-invalid"],
            "selected_from": "last_good",
        }
    return {
        "classification": classification,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.repair_preview_result",
        "observed_authority": {
            "active_source_revision": 1,
            "migration_epoch": 0,
            "operation_epoch": 7,
            "registry_revision": 2,
        },
        "plan": plan,
        "repo_uuid": REPO_UUID,
        "request_sha256": hashlib.sha256(_request_bytes(_preview_request_value())).hexdigest(),
        "schema_version": 1,
        "state": "previewed",
    }


def _execute_result_value(*, state: str = "repaired") -> dict[str, Any]:
    return {
        "approved_preview_sha256": "a" * 64,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.repair_execute_result",
        "current": {"generation_id": "gen-last-good", "receipt_sha256": "c" * 64},
        "last_good": {"generation_id": "gen-last-good", "receipt_sha256": "c" * 64},
        "pointer_revision": 3,
        "repo_uuid": REPO_UUID,
        "request_sha256": hashlib.sha256(_request_bytes(_execute_request_value())).hexdigest(),
        "schema_version": 1,
        "state": state,
    }


def test_repair_schema_loaders_and_packaged_files_cover_preview_and_execute() -> None:
    workspace_cli = _cli()
    loaders = {
        "repair-preview-request.schema.json": "load_repair_preview_request_schema",
        "repair-preview-result.schema.json": "load_repair_preview_result_schema",
        "repair-execute-request.schema.json": "load_repair_execute_request_schema",
        "repair-execute-result.schema.json": "load_repair_execute_result_schema",
    }

    for filename, loader_name in loaders.items():
        assert (_SCHEMA_ROOT / filename).is_file(), filename
        schema = getattr(workspace_cli, loader_name)()
        Draft202012Validator.check_schema(schema)


def test_top_level_help_lists_the_repair_lifecycle_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])
    mainmod._run_cli()
    help_text = capsys.readouterr().out
    assert "workspace repair --dry-run --request-stdin" in help_text
    assert "workspace repair --execute --request-stdin" in help_text


def test_repair_preview_request_is_canonical_duplicate_free_and_16kib_bounded() -> None:
    repair = _repair()
    raw = _request_bytes(_preview_request_value())
    request = repair.RepairPreviewRequest.from_bytes(raw)

    assert request.to_dict() == _preview_request_value()
    assert request.request_sha256 == hashlib.sha256(raw).hexdigest()
    for invalid in (
        raw + b" ",
        json.dumps(_preview_request_value(), indent=2).encode(),
        raw.replace(
            b'"schema_version":1,',
            b'"schema_version":1,"repo_uuid":"duplicate",',
            1,
        ),
        b"{" + b" " * (16 * 1024) + b"}",
    ):
        with pytest.raises(ValueError, match="repair preview request"):
            repair.RepairPreviewRequest.from_bytes(invalid)


@pytest.mark.parametrize("timeout_ms", [0, 60_001])
def test_repair_preview_request_rejects_timeout_outside_public_range(timeout_ms: int) -> None:
    repair = _repair()
    request = {**_preview_request_value(), "timeout_ms": timeout_ms}

    with pytest.raises(repair.RepairPreviewRequestInvalid) as raised:
        repair.RepairPreviewRequest.from_bytes(_request_bytes(request))

    assert isinstance(raised.value.__cause__, ValueError)


def test_repair_execute_request_requires_approved_preview_and_exact_authorization() -> None:
    repair = _repair()
    raw = _request_bytes(_execute_request_value())
    request = repair.RepairExecuteRequest.from_bytes(raw)

    assert request.to_dict() == _execute_request_value()
    assert request.request_sha256 == hashlib.sha256(raw).hexdigest()
    for invalid in (
        {
            key: value
            for key, value in _execute_request_value().items()
            if key != "approved_preview_sha256"
        },
        {**_execute_request_value(), "authorization": {**_authorization(), "action": "ROLLBACK"}},
        {**_execute_request_value(), "authorization": {**_authorization(), "unexpected": "value"}},
    ):
        with pytest.raises(ValueError, match="repair execute request"):
            repair.RepairExecuteRequest.from_bytes(_request_bytes(invalid))


def test_repair_execute_schema_matches_transport_bounded_authorization() -> None:
    workspace_cli = _cli()
    repair = _repair()
    value = _execute_request_value()
    value["authorization"] = {
        **_authorization(),
        "reason": "r" * 4_097,
    }
    raw = _request_bytes(value)

    assert len(raw) < repair.REPAIR_REQUEST_MAX_BYTES
    assert repair.RepairExecuteRequest.from_bytes(raw).to_dict() == value
    Draft202012Validator(
        workspace_cli.load_repair_execute_request_schema(),
        format_checker=FormatChecker(),
    ).validate(value)


def test_repair_result_schemas_admit_only_redacted_bounded_public_shapes() -> None:
    workspace_cli = _cli()
    preview = _preview_result_value()
    execute = _execute_result_value()
    preview_validator = Draft202012Validator(
        workspace_cli.load_repair_preview_result_schema(), format_checker=FormatChecker()
    )
    execute_validator = Draft202012Validator(
        workspace_cli.load_repair_execute_result_schema(), format_checker=FormatChecker()
    )

    assert not list(preview_validator.iter_errors(preview))
    assert not list(execute_validator.iter_errors(execute))
    for classification in ("no_op", "repairable", "irreparable"):
        assert not list(
            preview_validator.iter_errors(_preview_result_value(classification=classification))
        )
    assert not list(execute_validator.iter_errors(_execute_result_value(state="no_op")))
    assert list(preview_validator.iter_errors({**preview, "absolute_path": "/private/state"}))
    assert list(execute_validator.iter_errors({**execute, "authorization": _authorization()}))
    assert list(execute_validator.iter_errors({**execute, "pointer_revision": 0}))
    contradictory_irreparable = _preview_result_value(classification="irreparable")
    contradictory_irreparable["plan"] = _preview_result_value()["plan"]
    contradictory_no_op = _preview_result_value(classification="no_op")
    contradictory_no_op["plan"]["candidate"] = None
    contradictory_repairable = _preview_result_value()
    contradictory_repairable["plan"]["pointer_action"] = "unsupported"
    unproducible_journal_action = _preview_result_value()
    unproducible_journal_action["plan"]["journal_actions"] = ["clear_head_pending"]
    assert list(preview_validator.iter_errors(contradictory_irreparable))
    assert list(preview_validator.iter_errors(contradictory_no_op))
    assert list(preview_validator.iter_errors(contradictory_repairable))
    assert list(preview_validator.iter_errors(unproducible_journal_action))
    bounded_quarantine = _preview_result_value()
    bounded_quarantine["plan"]["quarantine"] = [f"gen-quarantine-{index}" for index in range(8)]
    oversized_quarantine = _preview_result_value()
    oversized_quarantine["plan"]["quarantine"] = [f"gen-quarantine-{index}" for index in range(9)]
    assert not list(preview_validator.iter_errors(bounded_quarantine))
    assert list(preview_validator.iter_errors(oversized_quarantine))


def test_repair_failure_results_are_schema_valid_and_redacted() -> None:
    workspace_cli = _cli()
    repair = _repair()
    preview = repair.classify_failure(
        repair.RepairPlanChanged("/private/preview provider-secret"),
        "preview",
    )
    execute = repair.classify_failure(
        repair.RepairExecuteRequestInvalid("/private/request provider-secret"),
        "execute",
    )

    Draft202012Validator(
        workspace_cli.load_repair_preview_result_schema(),
        format_checker=FormatChecker(),
    ).validate(preview.to_dict())
    Draft202012Validator(
        workspace_cli.load_repair_execute_result_schema(),
        format_checker=FormatChecker(),
    ).validate(execute.to_dict())
    for result in (preview, execute):
        assert "/private" not in result.canonical.decode()
        assert "provider-secret" not in result.canonical.decode()


@pytest.mark.parametrize(
    ("arguments", "usage"),
    [
        (("repair",), _PREVIEW_USAGE),
        (("repair", "--dry-run", "--request-stdin", "extra"), _PREVIEW_USAGE),
        (("repair", "--request-stdin", "--dry-run"), _PREVIEW_USAGE),
        (("repair", "--execute"), _EXECUTE_USAGE),
        (("repair", "--execute", "--request-stdin", "extra"), _EXECUTE_USAGE),
        (("repair", "--request-stdin", "--execute"), _EXECUTE_USAGE),
    ],
)
def test_repair_cli_rejects_malformed_argv_before_authority_or_stdin(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    usage: str,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("repair usage must not load authority"),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("repair usage must not read stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == usage + "\n"


@pytest.mark.parametrize(
    "arguments",
    [("repair", "--dry-run", "--request-stdin"), ("repair", "--execute", "--request-stdin")],
)
def test_repair_cli_loads_authority_before_stdin(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: None)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("repair authority must precede stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 20
    assert stdout.getvalue() == ""
    failure = json.loads(stderr.getvalue())
    assert failure["state"] == "invalid"
    assert failure["exit_code"] == 20
    assert "private" not in stderr.getvalue()


def test_repair_preview_cli_emits_one_canonical_redacted_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    result = _preview_result_value()
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes(_preview_request_value())))
    )
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: object())
    monkeypatch.setattr(
        workspace_cli,
        "repair_preview",
        lambda _runtime, _request, **_kwargs: SimpleNamespace(
            canonical=canonical_json_bytes(result),
            to_dict=lambda: result,
        ),
    )
    stdout, stderr = StringIO(), StringIO()

    assert (
        workspace_cli.run_workspace_command(
            ("repair", "--dry-run", "--request-stdin"), stdout=stdout, stderr=stderr
        )
        == 0
    )
    assert stderr.getvalue() == ""
    assert stdout.getvalue().encode() == canonical_json_bytes(result)
    assert "/private" not in stdout.getvalue()
    assert "secret" not in stdout.getvalue()


def test_repair_preview_cli_returns_canonical_irreparable_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    result = _preview_result_value(classification="irreparable")
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(_request_bytes(_preview_request_value()))),
    )
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: object())
    monkeypatch.setattr(
        workspace_cli,
        "repair_preview",
        lambda _runtime, _request, **_kwargs: SimpleNamespace(
            canonical=canonical_json_bytes(result),
            to_dict=lambda: result,
        ),
    )
    stdout, stderr = StringIO(), StringIO()

    assert (
        workspace_cli.run_workspace_command(
            ("repair", "--dry-run", "--request-stdin"),
            stdout=stdout,
            stderr=stderr,
        )
        == 20
    )
    assert stderr.getvalue() == ""
    assert stdout.getvalue().encode() == canonical_json_bytes(result)


@pytest.mark.parametrize(
    "unsafe_surface",
    ("current_pointer", "prior_pointer", "generation_lock"),
)
def test_repair_preview_cli_preserves_unsafe_analysis_paths_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_surface: str,
) -> None:
    from tests.test_workspace_repair import _promote, _request, _runtime
    from tests.workspace_p3_helpers import tree_snapshot

    workspace_cli = _cli()
    harness, journal, generations, pointers, receipts = _runtime(tmp_path)
    _promote(pointers, harness, receipts[0])
    unsafe_path = {
        "current_pointer": generations.state.path(pointers._current(REPO_UUID)),
        "prior_pointer": generations.state.path(pointers._prior(REPO_UUID)),
        "generation_lock": generations.state.path(generations._lock(REPO_UUID, "gen-old")),
    }[unsafe_surface]
    external_record = tmp_path / "outside-provider-secret.record"
    external_record.write_bytes(b"external state must not be read\n")
    external_record.chmod(0o600)
    if unsafe_path.exists():
        unsafe_path.unlink()
    try:
        unsafe_path.symlink_to(external_record)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    before_tree = tree_snapshot(harness.state_root)
    request = _request(harness, timeout_ms=5_000)
    runtime = SimpleNamespace(
        registry=harness.registry,
        leases=harness.leases,
        generations=generations,
        pointers=pointers,
        journal=journal,
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(request.canonical)))
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: runtime)
    stdout, stderr = StringIO(), StringIO()

    assert (
        workspace_cli.run_workspace_command(
            ("repair", "--dry-run", "--request-stdin"),
            stdout=stdout,
            stderr=stderr,
        )
        == 20
    )

    assert stdout.getvalue() == ""
    failure = json.loads(stderr.getvalue())
    assert stderr.getvalue().encode() == canonical_json_bytes(failure)
    assert failure["state"] == "invalid"
    assert failure["reason_code"] == "unsafe_state_path"
    assert failure["action_code"] == "configure_safe_state_root"
    assert "provider-secret" not in stderr.getvalue()
    assert tree_snapshot(harness.state_root) == before_tree


def test_repair_execute_rejects_stale_or_changed_approval_before_pointer_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes(_execute_request_value())))
    )
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: object())

    monkeypatch.setattr(
        workspace_cli,
        "repair_execute",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _repair().RepairPlanChanged("/private/pointer secret")
        ),
    )
    stdout, stderr = StringIO(), StringIO()

    assert (
        workspace_cli.run_workspace_command(
            ("repair", "--execute", "--request-stdin"), stdout=stdout, stderr=stderr
        )
        == 10
    )
    assert stdout.getvalue() == ""
    failure = json.loads(stderr.getvalue())
    assert failure["state"] == "conflict"
    assert failure["exit_code"] == 10
    assert "/private" not in stderr.getvalue()
    assert "secret" not in stderr.getvalue()
