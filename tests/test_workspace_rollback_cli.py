"""Public P5B2 rollback CLI contract.

The rollback command is deliberately a one-shot fence around the existing
pointer rollback primitive.  These tests freeze its request boundary and the
values it must carry into the lease and ``PointerCAS`` layers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from io import BytesIO, StringIO
import errno
import hashlib
import importlib
import json
import sys
import time
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.composition import (
    WorkspaceAuthorityInvalid,
    WorkspaceAuthorityUnsupported,
)
from graphify.workspace.contracts import STATE_SCHEMA_VERSION, canonical_json_bytes
from graphify.workspace.generations import GenerationError
from graphify.workspace.journal import (
    JournalConflict,
    JournalCorrupt,
    JournalRecoveryRequired,
)
from graphify.workspace.leases import LeaseBusy, LeaseExpired, LeaseRecoveryRequired
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    LockTimeout,
    StateCorrupt,
)
from graphify.workspace.pointers import PointerCAS, PointerConflict, PointerCorrupt
from graphify.workspace.registry import RevisionConflict
from graphify.workspace.status import EXIT_READY
from tests.workspace_p3_helpers import REPO_UUID, START, tree_snapshot


_ROLLBACK_USAGE = "graphify workspace rollback --request-stdin"
_RECEIPT_SHA256 = "a" * 64
_TARGET_GENERATION = "gen-p5b2-last-good"


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _rollback() -> Any:
    """Import lazily so dispatch-only assertions retain their own diagnosis."""

    return importlib.import_module("graphify.workspace.rollback")


def _request_value() -> dict[str, Any]:
    return {
        "contract": "graphify.workspace.rollback_request",
        "schema_version": 1,
        "cli_contract_version": 1,
        "repo_uuid": REPO_UUID,
        "expected_registry_revision": 2,
        "expected_active_source_revision": 1,
        "expected_operation_epoch": 7,
        "expected_migration_epoch": 0,
        "expected_pointer_revision": 2,
        "expected_current_receipt_sha256": "b" * 64,
        "target_generation_id": _TARGET_GENERATION,
        "target_receipt_sha256": _RECEIPT_SHA256,
        "target_source_epoch": 3,
        "authorization": {
            "action": "ROLLBACK",
            "issued_at": "2026-07-26T15:00:00Z",
            "nonce": "rollback-cli-test",
            "operator_id": "operator:rollback-test",
            "reason": "private rollback authorization",
        },
    }


def _request_bytes(value: dict[str, Any] | None = None) -> bytes:
    return canonical_json_bytes(_request_value() if value is None else value)


def _parse_request(value: dict[str, Any] | None = None) -> Any:
    return _rollback().RollbackRequest.from_bytes(_request_bytes(value))


def _pointer(*, current: str = "b" * 64, last_good: str = _RECEIPT_SHA256) -> dict[str, object]:
    return {
        "pointer_revision": 2,
        "current": {"generation_id": "gen-p5b2-current", "receipt_sha256": current},
        "last_good": {"generation_id": _TARGET_GENERATION, "receipt_sha256": last_good},
    }


class _LeaseGrant:
    active_source_revision = 1
    operation_epoch = 8
    migration_epoch = 0

    def __init__(self, deadline_ns: object, *, fence_token: object = 41) -> None:
        self.lease = SimpleNamespace(
            to_dict=lambda: {
                "fence_token": fence_token,
                "liveness_deadline_monotonic_ns": deadline_ns,
            }
        )


class _Leases:
    def __init__(self) -> None:
        self.acquire_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.release_calls: list[object] = []

    def current_owner(self) -> str:
        return "operator:trusted-owner"

    def acquire(self, *args: object, **kwargs: object) -> _LeaseGrant:
        self.acquire_calls.append((args, kwargs))
        expected = {
            "expected_registry_revision": 2,
            "expected_active_source_revision": 1,
            "expected_operation_epoch": 7,
            "expected_migration_epoch": 0,
        }
        if any(kwargs.get(name) != value for name, value in expected.items()):
            raise RevisionConflict("stale rollback lease authority")
        monotonic_ns = kwargs["monotonic_ns"]
        assert isinstance(monotonic_ns, int)
        return _LeaseGrant(monotonic_ns + 30_000_000_000)

    def release(self, grant: object, **_kwargs: object) -> None:
        self.release_calls.append(grant)


class _PointerDocument:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = value

    def to_dict(self) -> dict[str, object]:
        return self._value


class _ReceiptDocument:
    def __init__(self, *, active_source_revision: int = 1) -> None:
        self.active_source_revision = active_source_revision

    def to_dict(self) -> dict[str, object]:
        return {
            "source_epoch": 3,
            "active_source_revision": self.active_source_revision,
        }


class _Pointers:
    def __init__(
        self,
        pointer: dict[str, object] | None = None,
        *,
        last_good_active_source_revision: int = 1,
    ) -> None:
        self.pointer = _pointer() if pointer is None else pointer
        self.last_good_active_source_revision = last_good_active_source_revision
        self.load_calls: list[dict[str, object]] = []
        self.verify_calls: list[dict[str, object]] = []
        self.rollback_calls: list[tuple[object, PointerCAS, dict[str, object]]] = []

    def load(
        self,
        _repo_uuid: str,
        *,
        allow_missing: bool = False,
        deadline_ns: int | None = None,
    ) -> _PointerDocument:
        self.load_calls.append(
            {
                "allow_missing": allow_missing,
                "deadline_ns": deadline_ns,
            }
        )
        return _PointerDocument(self.pointer)

    def verify_pointer(
        self,
        _pointer: _PointerDocument,
        *,
        expected_repo_uuid: str,
        deadline_ns: int | None = None,
    ) -> dict[str, _ReceiptDocument]:
        assert expected_repo_uuid == REPO_UUID
        self.verify_calls.append(
            {
                "expected_repo_uuid": expected_repo_uuid,
                "deadline_ns": deadline_ns,
            }
        )
        return {
            "current": _ReceiptDocument(),
            "last_good": _ReceiptDocument(
                active_source_revision=self.last_good_active_source_revision
            ),
        }

    def verify_visible_pointer(
        self,
        pointer: _PointerDocument,
        *,
        expected_repo_uuid: str,
        deadline_ns: int | None = None,
    ) -> dict[str, _ReceiptDocument]:
        return self.verify_pointer(
            pointer,
            expected_repo_uuid=expected_repo_uuid,
            deadline_ns=deadline_ns,
        )

    def rollback(
        self, grant: object, cas: PointerCAS, **kwargs: object
    ) -> _PointerDocument:
        self.rollback_calls.append((grant, cas, kwargs))
        if cas.expected_source_epoch != 3:
            raise PointerConflict("stale rollback target source epoch")
        return _PointerDocument(
            {
                "pointer_revision": 3,
                "current": self.pointer["last_good"],
                "last_good": self.pointer["current"],
            }
        )


def _runtime(
    pointer: dict[str, object] | None = None,
    *,
    last_good_active_source_revision: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        leases=_Leases(),
        pointers=_Pointers(
            pointer,
            last_good_active_source_revision=last_good_active_source_revision,
        ),
    )


def _clock(*samples: int) -> Callable[[], int]:
    remaining = iter(samples)

    def sample() -> int:
        try:
            return next(remaining)
        except StopIteration as exc:
            raise AssertionError("rollback sampled the monotonic clock unexpectedly") from exc

    return sample


def _real_rollback_fixture(tmp_path: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
    from tests.test_workspace_pointers import _cas, _promotion_runtime

    harness, journal, _generations, pointers, promote, receipts = _promotion_runtime(
        tmp_path
    )
    old = receipts["gen-old"]
    new = receipts["gen-new"]
    pointers.promote(
        promote,
        _cas(promote, old, revision=0, current_sha256=None),
        occurred_at=START + timedelta(seconds=2),
        monotonic_ns=20_001,
    )
    pointers.promote(
        promote,
        _cas(promote, new, revision=1, current_sha256=old.sha256),
        occurred_at=START + timedelta(seconds=3),
        monotonic_ns=20_002,
    )
    harness.leases.release(promote)
    registry = harness.registry.load().to_dict()
    lease_state = harness.leases.inspect(REPO_UUID)
    request = _request_value()
    request.update(
        {
            "expected_registry_revision": registry["revision"],
            "expected_active_source_revision": registry["workspaces"][0][
                "active_source_revision"
            ],
            "expected_operation_epoch": lease_state.operation_epoch,
            "expected_migration_epoch": lease_state.migration_epoch,
            "expected_pointer_revision": 2,
            "expected_current_receipt_sha256": new.sha256,
            "target_generation_id": "gen-old",
            "target_receipt_sha256": old.sha256,
            "target_source_epoch": old.to_dict()["source_epoch"],
        }
    )
    runtime = SimpleNamespace(leases=harness.leases, pointers=pointers)
    return runtime, harness, journal, request


def test_rollback_schema_freezes_bounded_canonical_request_and_receipt() -> None:
    workspace_cli = _cli()
    request_schema = workspace_cli.load_rollback_request_schema()
    receipt_schema = workspace_cli.load_rollback_receipt_schema()
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(receipt_schema)
    request_validator = Draft202012Validator(request_schema, format_checker=FormatChecker())
    receipt_validator = Draft202012Validator(receipt_schema, format_checker=FormatChecker())

    request = _request_value()
    assert not list(request_validator.iter_errors(request))
    assert list(
        request_validator.iter_errors(
            {
                **request,
                "expected_pointer_revision": 9_223_372_036_854_775_807,
            }
        )
    )
    for field in request:
        incomplete = dict(request)
        incomplete.pop(field)
        assert list(request_validator.iter_errors(incomplete)), field
    assert list(request_validator.iter_errors({**request, "unexpected": True}))
    assert list(request_validator.iter_errors({**request, "target_generation_id": "x" * 513}))
    authorization = request["authorization"]
    assert isinstance(authorization, dict)
    invalid_authorization = {**authorization, "action": "PROMOTE"}
    assert list(
        request_validator.iter_errors(
            {**request, "authorization": invalid_authorization}
        )
    )
    for field in ("nonce", "operator_id", "reason"):
        for value in (" ", " leading", "trailing ", "leading\n", "\ntrailing"):
            invalid_authorization = {**authorization, field: value}
            assert list(
                request_validator.iter_errors(
                    {**request, "authorization": invalid_authorization}
                )
            ), (field, value)
    lowercase_separator = {
        **authorization,
        "issued_at": str(authorization["issued_at"]).replace("T", "t"),
    }
    assert list(
        request_validator.iter_errors(
            {**request, "authorization": lowercase_separator}
        )
    )

    success = {
        "contract": "graphify.workspace.rollback",
        "schema_version": 1,
        "cli_contract_version": 1,
        "state": "rolled_back",
        "exit_code": EXIT_READY,
        "repo_uuid": REPO_UUID,
        "request_sha256": "c" * 64,
        "target_generation_id": _TARGET_GENERATION,
        "target_receipt_sha256": _RECEIPT_SHA256,
        "pointer_revision": 3,
    }
    assert not list(receipt_validator.iter_errors(success))
    assert not list(
        receipt_validator.iter_errors(
            {**success, "pointer_revision": 9_223_372_036_854_775_807}
        )
    )
    assert list(receipt_validator.iter_errors({**success, "owner": "private"}))


@pytest.mark.parametrize(
    "arguments",
    [
        ("rollback",),
        ("rollback", "--request-stdin", "extra"),
        ("rollback", "--request-stdin", "--request-stdin"),
        ("rollback", "--unknown"),
        ("rollback", "--help"),
    ],
)
def test_rollback_dispatch_and_help_are_exact_before_authority_or_stdin(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: pytest.fail("usage must not load authority"))

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("usage must not read stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == _ROLLBACK_USAGE + "\n"


@pytest.mark.parametrize("help_flag", ["-h", "--help", "-?"])
def test_top_level_rollback_help_is_bounded_to_exact_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    help_flag: str,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("bounded rollback must not inspect ambient installs"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", "workspace", "rollback", help_flag],
    )
    with pytest.raises(SystemExit) as raised:
        mainmod._run_cli()
    captured = capsys.readouterr()
    assert raised.value.code == 64
    assert captured.out == ""
    assert captured.err == _ROLLBACK_USAGE + "\n"


def test_top_level_help_lists_workspace_rollback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])
    mainmod._run_cli()
    assert "workspace rollback --request-stdin" in capsys.readouterr().out


def test_rollback_loads_authority_before_reading_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: None)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("stdin must follow authority")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(("rollback", "--request-stdin"), stdout=stdout, stderr=stderr) == 20
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["reason_code"] == "runtime_authority_missing"


@pytest.mark.parametrize(
    "failure, reason_code",
    [
        (
            WorkspaceAuthorityInvalid("private invalid authority"),
            "runtime_authority_invalid",
        ),
        (
            WorkspaceAuthorityUnsupported("private unsupported authority"),
            "runtime_authority_unsupported",
        ),
    ],
)
def test_rollback_rejects_unusable_authority_before_reading_stdin(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    reason_code: str,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: (_ for _ in ()).throw(failure),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("stdin must follow usable authority")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("rollback", "--request-stdin"),
            stdout=stdout,
            stderr=stderr,
        )
        == 20
    )
    assert stdout.getvalue() == ""
    payload = json.loads(stderr.getvalue())
    assert payload["reason_code"] == reason_code
    assert "private" not in stderr.getvalue()


def test_rollback_composes_authority_before_reading_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: (_ for _ in ()).throw(
            WorkspaceAuthorityInvalid("private composition failure")
        ),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("stdin must follow composition")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("rollback", "--request-stdin"),
            stdout=stdout,
            stderr=stderr,
        )
        == 20
    )
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["reason_code"] == "runtime_authority_invalid"


@pytest.mark.parametrize(
    "field, value",
    [
        ("contract", "graphify.workspace.rollback_request.v2"),
        ("schema_version", 2),
        ("cli_contract_version", 2),
    ],
)
def test_rollback_rejects_unsupported_request_before_lease(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    workspace_cli = _cli()
    runtime = _runtime()
    request = _request_value()
    request[field] = value
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes(request))))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: runtime)
    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("rollback", "--request-stdin"),
            inputs=object(),
            stdout=stdout,
            stderr=stderr,
        )
        == 20
    )
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "action_code": "use_supported_rollback_contract",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.rollback",
        "exit_code": 20,
        "reason_code": "rollback_request_unsupported",
        "schema_version": 1,
        "state": "invalid",
    }
    assert runtime.leases.acquire_calls == []


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda: b"{}\n",
        lambda: b'{"contract":"graphify.workspace.rollback_request"}\n',
        lambda: _request_bytes()[:-1],
        lambda: _request_bytes() + b" ",
        lambda: b"{" + (b" " * 1_000_000) + b"}\n",
    ],
)
def test_rollback_rejects_noncanonical_missing_or_oversized_input_before_writes(
    payload_factory: Any,
) -> None:
    with pytest.raises(ValueError, match="rollback request"):
        _rollback().RollbackRequest.from_bytes(payload_factory())


def test_rollback_request_hash_and_authorization_are_canonical_and_duplicate_free() -> None:
    request = _parse_request()
    assert request.to_dict() == _request_value()
    assert request.request_sha256 == hashlib.sha256(_request_bytes()).hexdigest()
    duplicate = _request_bytes().replace(
        b'"schema_version":1,', b'"schema_version":1,"repo_uuid":"' + REPO_UUID.encode() + b'",', 1
    )
    with pytest.raises(ValueError, match="rollback request"):
        _rollback().RollbackRequest.from_bytes(duplicate)


@pytest.mark.parametrize(
    "field, value",
    [
        ("expected_registry_revision", 0),
        ("expected_operation_epoch", True),
        ("expected_pointer_revision", 9_223_372_036_854_775_807),
        ("expected_current_receipt_sha256", "A" * 64),
        ("target_receipt_sha256", "short"),
    ],
)
def test_rollback_request_field_validation_uses_public_error(
    field: str,
    value: object,
) -> None:
    request = _request_value()
    request[field] = value

    with pytest.raises(_rollback().RollbackRequestInvalid) as raised:
        _rollback().RollbackRequest.from_bytes(_request_bytes(request))

    assert isinstance(raised.value.__cause__, ValueError)


def test_rollback_stdin_binary_reader_collects_short_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    payload = _request_bytes()
    chunks = iter((payload[:17], payload[17:113], payload[113:], b""))

    class ShortReader:
        @staticmethod
        def read(_size: int) -> bytes:
            return next(chunks)

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=ShortReader()))

    assert workspace_cli._read_rollback_request_bytes() == payload


def test_rollback_requires_the_exact_live_last_good_before_lease_or_delegate() -> None:
    runtime = _runtime(_pointer(last_good="d" * 64))
    with pytest.raises(RevisionConflict, match="last_good"):
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(),
        )
    assert runtime.leases.acquire_calls == []
    assert runtime.pointers.rollback_calls == []


def test_rollback_rejects_current_as_last_good_before_lease_or_delegate() -> None:
    same_ref = {
        "generation_id": _TARGET_GENERATION,
        "receipt_sha256": _RECEIPT_SHA256,
    }
    runtime = _runtime(
        {
            "pointer_revision": 2,
            "current": same_ref,
            "last_good": dict(same_ref),
        }
    )

    with pytest.raises(StateCorrupt, match="distinct"):
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(),
        )

    assert runtime.leases.acquire_calls == []
    assert runtime.pointers.rollback_calls == []


def test_rollback_rejects_cross_source_last_good_before_lease_or_delegate() -> None:
    runtime = _runtime(last_good_active_source_revision=2)
    with pytest.raises(RevisionConflict, match="active source revision"):
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(),
        )
    assert runtime.leases.acquire_calls == []
    assert runtime.pointers.rollback_calls == []


def test_rollback_acquires_trusted_ttl_bounded_lease_and_derives_pointer_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    pointer_cas_type = _rollback().PointerCAS
    monkeypatch.setattr(
        _rollback(),
        "PointerCAS",
        lambda **kwargs: pointer_cas_type(**kwargs),
    )
    receipt = _rollback().rollback(
        runtime,
        _parse_request(),
        occurred_at=START,
        monotonic_clock=_clock(100, 101),
    )
    assert receipt.to_dict()["state"] == "rolled_back"
    assert runtime.leases.acquire_calls
    args, kwargs = runtime.leases.acquire_calls[0]
    assert args[:3] == (REPO_UUID, "ROLLBACK", "operator:trusted-owner")
    assert kwargs == {
        "expected_registry_revision": 2,
        "expected_active_source_revision": 1,
        "expected_operation_epoch": 7,
        "expected_migration_epoch": 0,
        "acquired_at": START,
        "monotonic_ns": 100,
        "ttl_ns": 30_000_000_000,
        "deadline_ns": 30_000_000_100,
    }
    _grant, cas, call_kwargs = runtime.pointers.rollback_calls[0]
    assert cas == PointerCAS(
        expected_pointer_revision=2,
        expected_active_source_revision=1,
        expected_source_epoch=3,
        expected_operation_epoch=8,
        expected_migration_epoch=0,
        expected_state_schema_version=STATE_SCHEMA_VERSION,
        expected_fence_token=41,
        candidate_generation_id=_TARGET_GENERATION,
        candidate_receipt_sha256=_RECEIPT_SHA256,
        expected_current_receipt_sha256="b" * 64,
    )
    assert runtime.pointers.verify_calls == [
        {"expected_repo_uuid": REPO_UUID, "deadline_ns": None},
        {
            "expected_repo_uuid": REPO_UUID,
            "deadline_ns": 30_000_000_100,
        },
    ]
    assert runtime.pointers.load_calls == [
        {"allow_missing": False, "deadline_ns": None},
        {"allow_missing": False, "deadline_ns": 30_000_000_100},
    ]
    assert call_kwargs == {
        "occurred_at": START,
        "monotonic_ns": 101,
        "deadline_ns": 30_000_000_100,
    }


def test_rollback_success_is_two_generation_transition_and_releases_lease() -> None:
    runtime = _runtime()
    receipt = _rollback().rollback(
        runtime,
        _parse_request(),
        occurred_at=START,
        monotonic_clock=_clock(100, 101),
    )
    payload = receipt.to_dict()
    assert payload["state"] == "rolled_back"
    assert payload["target_generation_id"] == _TARGET_GENERATION
    assert payload["pointer_revision"] == 3
    assert runtime.leases.release_calls == [runtime.pointers.rollback_calls[0][0]]


def test_rollback_persists_exact_two_generation_transition_and_journal(
    tmp_path: Any,
) -> None:
    runtime, _harness, journal, request_value = _real_rollback_fixture(tmp_path)
    request = _parse_request(request_value)
    base_monotonic_ns = time.monotonic_ns()
    receipt = _rollback().rollback(
        runtime,
        request,
        occurred_at=START + timedelta(seconds=4),
        monotonic_clock=_clock(base_monotonic_ns, base_monotonic_ns + 1),
    )
    pointer = runtime.pointers.load(REPO_UUID)
    assert pointer is not None
    value = pointer.to_dict()
    assert receipt.to_dict()["pointer_revision"] == 3
    assert value["pointer_revision"] == 3
    assert value["current"] == {
        "generation_id": "gen-old",
        "receipt_sha256": request.target_receipt_sha256,
    }
    assert value["last_good"]["generation_id"] == "gen-new"
    transitions = [
        event.to_dict()["transition"]
        for event in journal.read_stable(REPO_UUID).for_generation("gen-old")
    ]
    assert transitions[-1] == "ROLLED_BACK"


def test_rollback_recovers_valid_uncommitted_journal_tail_under_lease(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.workspace_p3_helpers import acquire

    runtime, harness, journal, request_value = _real_rollback_fixture(tmp_path)
    build = acquire(harness, "BUILD", tick=3)
    armed = True

    def interrupt_after_segment(event: str) -> None:
        nonlocal armed
        if armed and event == "journal:ALLOCATED:segment_durable":
            armed = False
            raise InjectedFault(event)

    monkeypatch.setattr(journal, "fault_hook", interrupt_after_segment)
    with pytest.raises(InjectedFault):
        journal.append(
            build,
            transition="ALLOCATED",
            generation_id="gen-uncommitted-tail",
            receipt_sha256=None,
            pointer_revision=None,
            occurred_at=START + timedelta(seconds=4),
            monotonic_ns=30_001,
        )
    harness.leases.release(build)
    monkeypatch.setattr(journal, "fault_hook", lambda _event: None)

    with pytest.raises(JournalConflict, match="requires recovery"):
        journal.read_stable(REPO_UUID)

    request_value["expected_operation_epoch"] = harness.leases.inspect(
        REPO_UUID
    ).operation_epoch
    base_monotonic_ns = time.monotonic_ns()
    receipt = _rollback().rollback(
        runtime,
        _parse_request(request_value),
        occurred_at=START + timedelta(seconds=5),
        monotonic_clock=_clock(base_monotonic_ns, base_monotonic_ns + 1),
    )

    assert receipt.to_dict()["state"] == "rolled_back"
    transitions = [event.to_dict()["transition"] for event in journal.read_stable(REPO_UUID).events]
    assert transitions[-2:] == ["ALLOCATED", "ROLLED_BACK"]


def test_rollback_rejects_stale_visible_pointer_before_acquiring_lease(
    tmp_path: Any,
) -> None:
    runtime, harness, journal, request_value = _real_rollback_fixture(tmp_path)
    pointer_path = harness.state_root / "workspaces" / REPO_UUID / "pointers.json"
    stale_pointer = pointer_path.read_bytes()
    base_monotonic_ns = time.monotonic_ns()
    _rollback().rollback(
        runtime,
        _parse_request(request_value),
        occurred_at=START + timedelta(seconds=4),
        monotonic_clock=_clock(base_monotonic_ns, base_monotonic_ns + 1),
    )
    pointer_path.write_bytes(stale_pointer)
    before = tree_snapshot(harness.state_root)
    journal_before = tuple(
        event.to_dict() for event in journal.read_stable(REPO_UUID).events
    )

    with pytest.raises(PointerCorrupt, match="stale relative"):
        _rollback().rollback(
            runtime,
            _parse_request(request_value),
            occurred_at=START + timedelta(seconds=5),
            monotonic_clock=_clock(),
        )

    assert tree_snapshot(harness.state_root) == before
    assert tuple(
        event.to_dict() for event in journal.read_stable(REPO_UUID).events
    ) == journal_before


def test_rollback_expired_lease_never_writes_pointer_or_journal(tmp_path: Any) -> None:
    runtime, harness, journal, request_value = _real_rollback_fixture(tmp_path)
    request = _parse_request(request_value)
    pointer_path = (
        harness.state_root / "workspaces" / REPO_UUID / "pointers.json"
    )
    pointer_before = pointer_path.read_bytes()
    journal_before = tuple(
        event.to_dict() for event in journal.read_stable(REPO_UUID).events
    )
    base_monotonic_ns = time.monotonic_ns()

    with pytest.raises(LeaseExpired):
        _rollback().rollback(
            runtime,
            request,
            occurred_at=START + timedelta(seconds=4),
            monotonic_clock=_clock(
                base_monotonic_ns,
                base_monotonic_ns + 30_000_000_000,
            ),
        )

    assert pointer_path.read_bytes() == pointer_before
    assert tuple(
        event.to_dict() for event in journal.read_stable(REPO_UUID).events
    ) == journal_before


def test_rollback_expired_reload_stops_before_generation_reverification(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, harness, journal, request_value = _real_rollback_fixture(tmp_path)
    request = _parse_request(request_value)
    pointer_path = harness.state_root / "workspaces" / REPO_UUID / "pointers.json"
    pointer_before = pointer_path.read_bytes()
    journal_before = tuple(
        event.to_dict() for event in journal.read_stable(REPO_UUID).events
    )
    verify_pointer = runtime.pointers.verify_pointer
    verify_calls = 0

    def verify_once(*args: object, **kwargs: object) -> object:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls > 1:
            raise AssertionError(
                "expired post-acquisition reload reached generation verification"
            )
        return verify_pointer(*args, **kwargs)

    runtime.pointers.verify_pointer = verify_once
    deadline_ns = 40_000_000_000
    expired = False
    acquire = runtime.leases.acquire

    def acquire_then_expire(*args: Any, **kwargs: Any) -> Any:
        nonlocal expired
        grant = acquire(*args, **kwargs)
        expired = True
        return grant

    monkeypatch.setattr(runtime.leases, "acquire", acquire_then_expire)
    monkeypatch.setattr(
        "graphify.workspace.persistence.time.monotonic_ns",
        lambda: deadline_ns + 1 if expired else deadline_ns - 1,
    )

    with pytest.raises(RevisionConflict, match="lease advanced") as raised:
        _rollback().rollback(
            runtime,
            request,
            occurred_at=START + timedelta(seconds=4),
            monotonic_clock=_clock(deadline_ns - 30_000_000_000),
        )

    assert isinstance(raised.value.__cause__, LockTimeout)
    assert _rollback().classify_failure(raised.value).reason_code == (
        "rollback_authority_conflict"
    )
    assert verify_calls == 1
    assert pointer_path.read_bytes() == pointer_before
    assert tuple(
        event.to_dict() for event in journal.read_stable(REPO_UUID).events
    ) == journal_before


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("expected_registry_revision", lambda value: int(value) + 1),
        ("expected_active_source_revision", lambda value: int(value) + 1),
        ("expected_operation_epoch", lambda value: int(value) + 1),
        ("expected_migration_epoch", lambda value: int(value) + 1),
        ("expected_pointer_revision", lambda value: int(value) + 1),
        ("expected_current_receipt_sha256", lambda _value: "d" * 64),
        ("target_generation_id", lambda _value: "gen-racer"),
        ("target_receipt_sha256", lambda _value: "e" * 64),
        ("target_source_epoch", lambda value: int(value) + 1),
    ],
)
def test_rollback_real_stale_cas_is_conflict_without_durable_mutation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: Any,
) -> None:
    workspace_cli = _cli()
    runtime, harness, journal, request = _real_rollback_fixture(tmp_path)
    request[field] = replacement(request[field])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes(request))))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: runtime)
    before = tree_snapshot(harness.state_root)
    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("rollback", "--request-stdin"),
            inputs=object(),
            stdout=stdout,
            stderr=stderr,
        )
        == 10
    )
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {
        "action_code": "refresh_rollback_request",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.rollback",
        "exit_code": 10,
        "reason_code": "rollback_authority_conflict",
        "schema_version": 1,
        "state": "conflict",
    }
    assert tree_snapshot(harness.state_root) == before
    assert all(
        event.to_dict()["transition"] != "ROLLED_BACK"
        for event in journal.read_stable(REPO_UUID).events
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("expected_registry_revision", 3),
        ("expected_active_source_revision", 2),
        ("expected_operation_epoch", 8),
        ("expected_migration_epoch", 1),
        ("expected_pointer_revision", 3),
        ("expected_current_receipt_sha256", "e" * 64),
        ("target_source_epoch", 4),
    ],
)
def test_rollback_fails_closed_for_every_stale_cas_dimension(field: str, value: object) -> None:
    runtime = _runtime()
    request = _request_value()
    request[field] = value
    with pytest.raises((RevisionConflict, PointerConflict)):
        _rollback().rollback(
            runtime,
            _parse_request(request),
            occurred_at=START,
            monotonic_clock=_clock(100, 101),
        )
    assert runtime.pointers.rollback_calls == []


@pytest.mark.parametrize(
    "error, expected_exit, reason_code, action_code",
    [
        (LeaseBusy("private owner"), 10, "lease_busy", "retry_workspace_rollback"),
        (
            LeaseRecoveryRequired("private recovery"),
            10,
            "workspace_recovery_required",
            "inspect_workspace_state",
        ),
        (
            PointerCorrupt("private pointer"),
            20,
            "state_corrupt",
            "run_workspace_repair",
        ),
        (
            JournalRecoveryRequired("private suffix"),
            20,
            "state_corrupt",
            "run_workspace_repair",
        ),
        (
            GenerationError("private generation"),
            20,
            "state_corrupt",
            "inspect_workspace_state",
        ),
        (
            JournalCorrupt("private journal"),
            20,
            "state_corrupt",
            "inspect_workspace_state",
        ),
        (
            StateCorrupt("private state"),
            20,
            "state_corrupt",
            "inspect_workspace_state",
        ),
        (
            CommitUnknown("private uncertain commit"),
            20,
            "commit_unknown",
            "run_workspace_doctor",
        ),
    ],
)
def test_rollback_contention_recovery_corruption_and_commit_unknown_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_exit: int,
    reason_code: str,
    action_code: str,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: _runtime())
    monkeypatch.setattr(_rollback(), "rollback", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("rollback", "--request-stdin"),
            inputs=object(),
            stdout=stdout,
            stderr=stderr,
        )
        == expected_exit
    )
    assert stdout.getvalue() == ""
    payload = json.loads(stderr.getvalue())
    assert payload["reason_code"] == reason_code
    assert payload["action_code"] == action_code
    Draft202012Validator(
        workspace_cli.load_rollback_receipt_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    assert "private" not in stderr.getvalue()
    assert "/" not in stderr.getvalue()


def test_rollback_cli_discards_engine_output_and_emits_one_canonical_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    request = _parse_request()
    runtime = _runtime()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(request.canonical)))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: runtime)
    expected_receipt = _rollback().RollbackReceipt(
        repo_uuid=request.repo_uuid,
        request_sha256=request.request_sha256,
        target_generation_id=request.target_generation_id,
        target_receipt_sha256=request.target_receipt_sha256,
        pointer_revision=3,
    )

    def noisy_rollback(*_args: object, **_kwargs: object) -> Any:
        print("private engine stdout credential=secret")
        print("private engine stderr /private/path", file=sys.stderr)
        return expected_receipt

    monkeypatch.setattr(_rollback(), "rollback", noisy_rollback)
    stdout, stderr = StringIO(), StringIO()
    exit_code = workspace_cli.run_workspace_command(
        ("rollback", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == EXIT_READY
    assert stderr.getvalue() == ""
    assert stdout.getvalue() == expected_receipt.canonical.decode("utf-8")
    assert json.loads(stdout.getvalue())["exit_code"] == exit_code
    assert "private" not in stdout.getvalue()


def test_rollback_non_last_good_target_is_rejected_before_lease_or_delegate() -> None:
    runtime = _runtime(_pointer())
    request = _request_value()
    request["target_generation_id"] = "gen-missing"
    with pytest.raises((RevisionConflict, StateCorrupt)):
        _rollback().rollback(
            runtime,
            _parse_request(request),
            occurred_at=START,
            monotonic_clock=_clock(),
        )
    assert runtime.leases.acquire_calls == []
    assert runtime.pointers.rollback_calls == []


@pytest.mark.parametrize(
    "damage, reason_code",
    [("missing", "unsafe_state_path"), ("corrupt", "state_corrupt")],
)
def test_rollback_real_missing_or_corrupt_target_is_invalid_without_new_write(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    reason_code: str,
) -> None:
    workspace_cli = _cli()
    runtime, harness, journal, request = _real_rollback_fixture(tmp_path)
    target = (
        harness.state_root
        / "workspaces"
        / REPO_UUID
        / "generations"
        / "gen-old"
    )
    if damage == "missing":
        target.rename(harness.state_root / "parked-gen-old")
    else:
        (target / "receipt.json").write_bytes(b"corrupt rollback target\n")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes(request))))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: runtime)
    before = tree_snapshot(harness.state_root)
    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("rollback", "--request-stdin"),
            inputs=object(),
            stdout=stdout,
            stderr=stderr,
        )
        == 20
    )
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["reason_code"] == reason_code
    assert tree_snapshot(harness.state_root) == before
    assert all(
        event.to_dict()["transition"] != "ROLLED_BACK"
        for event in journal.read_stable(REPO_UUID).events
    )


def test_rollback_without_last_good_reports_no_target_before_lease() -> None:
    pointer = {"pointer_revision": 2, "current": _pointer()["current"], "last_good": None}
    runtime = _runtime(pointer)
    with pytest.raises(RevisionConflict, match="live last_good"):
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(),
        )
    assert runtime.leases.acquire_calls == []
    assert runtime.pointers.rollback_calls == []


def test_rollback_malformed_pointer_remains_state_corrupt_before_lease() -> None:
    pointer = {"pointer_revision": 2, "last_good": _pointer()["last_good"]}
    runtime = _runtime(pointer)
    with pytest.raises(StateCorrupt):
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(),
        )
    assert runtime.leases.acquire_calls == []
    assert runtime.pointers.rollback_calls == []


@pytest.mark.parametrize(
    "grant",
    [
        _LeaseGrant("invalid-deadline"),
        _LeaseGrant(30_000_000_100, fence_token="invalid-fence"),
    ],
)
def test_rollback_malformed_internal_grant_is_state_corrupt(grant: _LeaseGrant) -> None:
    runtime = _runtime()
    runtime.leases.acquire = lambda *_args, **_kwargs: grant

    with pytest.raises(StateCorrupt, match="lease grant"):
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(100),
        )

    assert runtime.leases.release_calls == [grant]
    assert runtime.pointers.rollback_calls == []


def test_rollback_reraises_injected_fault_and_release_cannot_mask_primary_error() -> None:
    runtime = _runtime()
    injected = InjectedFault("rollback-fault")
    runtime.pointers.rollback = lambda *_args, **_kwargs: (_ for _ in ()).throw(injected)
    runtime.leases.release = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private release"))
    with pytest.raises(InjectedFault) as raised:
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(100, 101),
        )
    assert raised.value is injected


def test_rollback_release_injected_fault_never_masks_primary_error() -> None:
    runtime = _runtime()
    primary = PointerConflict("private stale pointer")
    release = InjectedFault("private release fault")
    runtime.pointers.rollback = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        primary
    )
    runtime.leases.release = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        release
    )

    with pytest.raises(PointerConflict) as raised:
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(100, 101),
        )

    assert raised.value is primary


def test_rollback_reraises_release_injected_fault_after_visible_success() -> None:
    runtime = _runtime()
    injected = InjectedFault("rollback-release-fault")
    runtime.leases.release = lambda *_args, **_kwargs: (_ for _ in ()).throw(injected)
    with pytest.raises(InjectedFault) as raised:
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(100, 101),
        )
    assert raised.value is injected


@pytest.mark.parametrize(
    "release_error",
    [CommitUnknown("private release uncertainty"), RuntimeError("private release failure")],
)
def test_rollback_release_failure_after_success_is_commit_unknown(
    release_error: Exception,
) -> None:
    runtime = _runtime()
    runtime.leases.release = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        release_error
    )
    with pytest.raises(CommitUnknown) as raised:
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(100, 101),
        )
    if isinstance(release_error, CommitUnknown):
        assert raised.value is release_error
    else:
        assert raised.value.__cause__ is release_error


@pytest.mark.parametrize(
    "primary",
    [PointerConflict("private stale pointer"), CommitUnknown("private pointer uncertainty")],
)
def test_rollback_release_failure_never_masks_primary(primary: Exception) -> None:
    runtime = _runtime()
    runtime.pointers.rollback = lambda *_args, **_kwargs: (_ for _ in ()).throw(primary)
    runtime.leases.release = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("private release failure")
    )
    with pytest.raises(type(primary)) as raised:
        _rollback().rollback(
            runtime,
            _parse_request(),
            occurred_at=START,
            monotonic_clock=_clock(100, 101),
        )
    assert raised.value is primary


def test_rollback_binary_output_flushes_inside_standard_broken_pipe_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()

    class BufferedPipe:
        @staticmethod
        def write(payload: bytes) -> int:
            return len(payload)

        @staticmethod
        def flush() -> None:
            raise BrokenPipeError(errno.EPIPE, "closed reader")

    standard_out = SimpleNamespace(buffer=BufferedPipe(), fileno=lambda: 1)
    standard_err = SimpleNamespace(fileno=lambda: 2)
    duplicated: list[tuple[int, int]] = []
    closed: list[int] = []
    with monkeypatch.context() as patch:
        patch.setattr(workspace_cli.sys, "stdout", standard_out)
        patch.setattr(workspace_cli.sys, "stderr", standard_err)
        patch.setattr(workspace_cli.os, "open", lambda _path, _flags: 99)
        patch.setattr(
            workspace_cli.os,
            "dup2",
            lambda source, target: duplicated.append((source, target)),
        )
        patch.setattr(workspace_cli.os, "close", closed.append)
        result = workspace_cli._emit_rollback_output(
            standard_out, b"{}\n", exit_code=EXIT_READY
        )

    assert result == EXIT_READY
    assert duplicated == [(99, 1), (99, 2)]
    assert closed == [99]
