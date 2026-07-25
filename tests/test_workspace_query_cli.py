"""Public P5B2c one-shot certified workspace-query CLI contract.

The CLI is a transport boundary around ``FreshnessAuthority.query``.  These
tests deliberately make its canonical request/result and redaction rules
independent of the freshness protocol tests.
"""

from __future__ import annotations

import errno
from io import BytesIO, StringIO, TextIOWrapper
import hashlib
import importlib
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.adapters import QueryRequest, UnsupportedCompatibility
from graphify.workspace.composition import (
    WorkspaceAuthorityInvalid,
    WorkspaceAuthorityUnsupported,
)
from graphify.workspace.contracts import JsonValue, canonical_json_bytes
from graphify.workspace.freshness import FreshnessResult
from graphify.workspace.persistence import StatePathError, UnsupportedRuntime


REPO_UUID = "11111111-1111-4111-8111-111111111111"
_QUERY_USAGE = "graphify workspace query --request-stdin"


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _request_value() -> dict[str, JsonValue]:
    return {
        "contract": "graphify.workspace.query_request",
        "schema_version": 1,
        "cli_contract_version": 1,
        "repo_uuid": REPO_UUID,
        "question": "what calls workspace",
        "mode": "bfs",
        "depth": 2,
        "token_budget": 2000,
        "context_filters": ["call"],
        "timeout_ms": 5000,
    }


def _request_bytes(value: dict[str, JsonValue] | None = None) -> bytes:
    return canonical_json_bytes(_request_value() if value is None else value)


def _multibyte_question(*, overflow_bytes: int = 0) -> str:
    return "é" + "\u2003" * 1_364 + " " * overflow_bytes + "é"


def _result_common() -> dict[str, object]:
    return {
        "contract": "graphify.workspace.query_result",
        "schema_version": 1,
        "cli_contract_version": 1,
    }


def _error_payload(stream: StringIO) -> dict[str, object]:
    payload = json.loads(stream.getvalue())
    Draft202012Validator(
        _cli().load_query_result_schema(), format_checker=FormatChecker()
    ).validate(payload)
    return payload


class _ForbiddenFreshness:
    def query(self, *_args: object, **_kwargs: object) -> FreshnessResult[str]:
        pytest.fail("invalid query input must not reach freshness authority")


def test_query_request_and_result_schemas_freeze_the_public_contract() -> None:
    workspace_cli = _cli()
    request_schema = workspace_cli.load_query_request_schema()
    result_schema = workspace_cli.load_query_result_schema()
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(result_schema)

    request_validator = Draft202012Validator(
        request_schema, format_checker=FormatChecker()
    )
    result_validator = Draft202012Validator(result_schema, format_checker=FormatChecker())
    request = _request_value()
    assert not list(request_validator.iter_errors(request))
    for field in request:
        incomplete = dict(request)
        incomplete.pop(field)
        assert list(request_validator.iter_errors(incomplete)), field
    assert list(request_validator.iter_errors({**request, "unexpected": True}))
    assert list(request_validator.iter_errors({**request, "timeout_ms": 0}))
    assert list(request_validator.iter_errors({**request, "timeout_ms": 60_001}))
    assert list(request_validator.iter_errors({**request, "timeout_ms": True}))

    question_schema = request_schema["properties"]["question"]
    filter_array_schema = request_schema["properties"]["context_filters"]
    filter_item_schema = filter_array_schema["items"]
    assert "maxLength" not in question_schema
    assert question_schema["x-graphify-utf8-max-bytes"] == 4_096
    assert "maxLength" not in filter_item_schema
    assert filter_item_schema["x-graphify-utf8-max-bytes"] == 128
    assert filter_array_schema["x-graphify-utf8-total-max-bytes"] == 1_024

    success = {
        **_result_common(),
        "state": "released",
        "decision": "release",
        "exit_code": 0,
        "reason_code": "observed_current",
        "query_executed": True,
        "observation_boundary": "two_sided",
        "repo_uuid": REPO_UUID,
        "output": {
            "stream": "stdout",
            "encoding": "utf-8",
            "bytes": 19,
            "sha256": "a" * 64,
        },
    }
    withheld = {
        **_result_common(),
        "state": "drifted",
        "decision": "withhold",
        "exit_code": 10,
        "reason_code": "drift",
        "action_code": "sync_workspace",
        "query_executed": True,
        "observation_boundary": "two_sided",
    }
    invalid = {
        **_result_common(),
        "state": "invalid",
        "decision": "withhold",
        "exit_code": 20,
        "reason_code": "query_request_invalid",
        "action_code": "provide_valid_query_request",
        "query_executed": False,
        "observation_boundary": "not_observed",
    }
    for result in (success, withheld, invalid):
        assert not list(result_validator.iter_errors(result))
    for result in (withheld, invalid):
        for leaked in (
            {"output": "leak"},
            {"repo_uuid": REPO_UUID},
        ):
            assert list(result_validator.iter_errors({**result, **leaked}))
    assert list(result_validator.iter_errors({**success, "action_code": "nope"}))
    assert list(result_validator.iter_errors({**success, "output": "not raw"}))


@pytest.mark.parametrize(
    ("field", "value", "expected_bytes"),
    [
        ("question", _multibyte_question(), 4_096),
        ("context_filters", ["é" * 64], 128),
        ("context_filters", ["é" * 64] * 8, 1_024),
    ],
)
def test_query_accepts_exact_multibyte_utf8_byte_bounds(
    field: str,
    value: JsonValue,
    expected_bytes: int,
) -> None:
    request = {**_request_value(), field: value}
    if field == "question":
        assert isinstance(value, str)
        actual_bytes = len(value.encode("utf-8"))
    else:
        assert isinstance(value, list)
        filter_values = cast(list[str], value)
        actual_bytes = sum(len(item.encode("utf-8")) for item in filter_values)
    assert actual_bytes == expected_bytes
    assert _cli()._parse_query_request(_request_bytes(request)).to_dict() == request


def test_query_rejects_multibyte_utf8_byte_bounds_one_byte_over() -> None:
    question = _multibyte_question(overflow_bytes=1)
    item = "é" * 63 + "€"
    filters = ["é" * 64] * 8 + ["x"]
    assert len(question.encode("utf-8")) == 4_097
    assert len(question) == 1_367
    assert sum(len(part) for part in question.split()) == 2
    assert len(item.encode("utf-8")) == 129
    assert len(item) == 64
    assert sum(len(value.encode("utf-8")) for value in filters) == 1_025
    assert all(len(value.encode("utf-8")) <= 128 for value in filters)

    workspace_cli = _cli()
    validator = Draft202012Validator(
        workspace_cli.load_query_request_schema(), format_checker=FormatChecker()
    )
    for field, value in (
        ("question", cast(JsonValue, question)),
        ("context_filters", cast(JsonValue, [item])),
        ("context_filters", cast(JsonValue, filters)),
    ):
        request = _request_value()
        request[field] = value
        assert not list(validator.iter_errors(request))
        with pytest.raises(ValueError, match="query request payload is invalid"):
            workspace_cli._parse_query_request(_request_bytes(request))


@pytest.mark.parametrize(
    "arguments",
    [
        ("query",),
        ("query", "--request-stdin", "extra"),
        ("query", "--unknown"),
        ("query", "--request-stdin", "--request-stdin"),
        ("query", "--help"),
    ],
)
def test_query_usage_is_exact_and_precedes_authority_and_stdin(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("usage must not load query authority"),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("usage must not read query stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == _QUERY_USAGE + "\n"


def test_query_missing_authority_is_reported_before_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: None)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("authority failure must precede query stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    result = workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), stdout=stdout, stderr=stderr
    )
    assert result == 20
    assert stdout.getvalue() == ""
    assert _error_payload(stderr) == {
        **_result_common(),
        "state": "invalid",
        "decision": "withhold",
        "exit_code": 20,
        "reason_code": "runtime_authority_missing",
        "action_code": "install_candidate_authority",
        "query_executed": False,
        "observation_boundary": "not_observed",
    }


@pytest.mark.parametrize(
    ("error", "expected_state", "reason_code", "action_code"),
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
def test_query_invalid_runtime_authority_precedes_stdin(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_state: str,
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
            pytest.fail("invalid runtime authority must precede query stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), stdout=stdout, stderr=stderr
    ) == 20
    assert stdout.getvalue() == ""
    assert _error_payload(stderr) == {
        **_result_common(),
        "state": expected_state,
        "decision": "withhold",
        "exit_code": 20,
        "reason_code": reason_code,
        "action_code": action_code,
        "query_executed": False,
        "observation_boundary": "not_observed",
    }
    assert "/private" not in stderr.getvalue()
    assert "provider-secret" not in stderr.getvalue()


def test_query_uncomposable_runtime_authority_precedes_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()

    def fail_compose(_inputs: object) -> None:
        raise UnsupportedCompatibility("/private/authority provider-secret")

    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", fail_compose)

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("uncomposable runtime authority must precede query stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    payload = _error_payload(stderr)
    assert payload["reason_code"] == "unsupported_compatibility"
    assert payload["action_code"] == "install_supported_candidate"
    assert "/private" not in stderr.getvalue()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"contract":"graphify.workspace.query_request","contract":"duplicate"}',
        b'{"schema_version":1}',
        b'{"contract":"graphify.workspace.query_request","unexpected":true}',
        json.dumps(_request_value(), indent=2).encode(),
        _request_bytes()[:-1] + b" ",
        b"\xff",
        _request_bytes({**_request_value(), "question": " untrimmed"}),
        _request_bytes({**_request_value(), "question": "x" * 4_097}),
        _request_bytes({**_request_value(), "mode": "best_first"}),
        _request_bytes({**_request_value(), "depth": 9}),
        _request_bytes({**_request_value(), "token_budget": 32_769}),
        _request_bytes({**_request_value(), "context_filters": ["x" * 129]}),
        _request_bytes({**_request_value(), "timeout_ms": 0}),
        _request_bytes({**_request_value(), "timeout_ms": 60_001}),
        _request_bytes({**_request_value(), "timeout_ms": True}),
        _request_bytes({key: value for key, value in _request_value().items() if key != "timeout_ms"}),
    ],
)
def test_query_rejects_invalid_stdin_before_freshness_query(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(freshness=_ForbiddenFreshness()),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(payload)))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 20
    assert stdout.getvalue() == ""
    payload_value = _error_payload(stderr)
    assert payload_value["reason_code"] == "query_request_invalid"
    assert payload_value["action_code"] == "provide_valid_query_request"
    assert "graphify.workspace.query_request" not in stderr.getvalue()


def test_query_stdin_is_bounded_before_decode_or_runtime_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(freshness=_ForbiddenFreshness()),
    )
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(buffer=BytesIO(b"{" + b"x" * (32 * 1024 + 1)))
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 20
    assert stdout.getvalue() == ""
    assert _error_payload(stderr)["reason_code"] == "query_request_invalid"


def test_query_rejects_unsupported_request_version_before_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    unsupported = _request_bytes({**_request_value(), "schema_version": 2})
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(unsupported)))
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(freshness=_ForbiddenFreshness()),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"),
        inputs=object(),
        stdout=stdout,
        stderr=stderr,
    ) == 20
    assert stdout.getvalue() == ""
    payload = _error_payload(stderr)
    assert payload["state"] == "unsupported"
    assert payload["reason_code"] == "query_request_unsupported"
    assert payload["action_code"] == "use_supported_query_contract"


def test_query_passes_only_query_request_and_deadline_to_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    request_value = _request_value()
    calls: list[tuple[str, object, int]] = []
    native_output = "exact e\u0301 output"

    class Freshness:
        def query(self, repo_uuid: str, request: QueryRequest, *, timeout_ns: int) -> FreshnessResult[str]:
            calls.append((repo_uuid, request, timeout_ns))
            return FreshnessResult(
                "release", "observed_current", native_output, None, True, "two_sided"
            )

        def probe(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("query CLI must not use freshness status probes")

    runtime = SimpleNamespace(freshness=Freshness())
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: runtime)
    stdout, stderr = StringIO(), StringIO()
    exit_code = workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    )
    assert exit_code == 0
    timeout_ms = request_value["timeout_ms"]
    assert isinstance(timeout_ms, int) and not isinstance(timeout_ms, bool)
    assert calls == [
        (
            REPO_UUID,
            QueryRequest(
                question="what calls workspace",
                mode="bfs",
                depth=2,
                token_budget=2000,
                context_filters=("call",),
            ),
            timeout_ms * 1_000_000,
        )
    ]
    assert stdout.getvalue() == native_output
    output_bytes = native_output.encode("utf-8")
    control = json.loads(stderr.getvalue())
    Draft202012Validator(
        workspace_cli.load_query_result_schema(), format_checker=FormatChecker()
    ).validate(control)
    assert control == {
        **_result_common(),
        "state": "released",
        "decision": "release",
        "exit_code": 0,
        "reason_code": "observed_current",
        "query_executed": True,
        "observation_boundary": "two_sided",
        "repo_uuid": REPO_UUID,
        "output": {
            "stream": "stdout",
            "encoding": "utf-8",
            "bytes": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
        },
    }
    assert stderr.getvalue().encode() == canonical_json_bytes(control)
    assert stdout.getvalue().encode("utf-8") == output_bytes


def test_query_writes_certified_utf8_bytes_independent_of_text_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    native_output = "exact é output"

    class Freshness:
        def query(self, *_args: object, **_kwargs: object) -> FreshnessResult[str]:
            return FreshnessResult(
                "release", "observed_current", native_output, None, True, "two_sided"
            )

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(freshness=Freshness()),
    )
    raw_stdout = BytesIO()
    stdout = TextIOWrapper(raw_stdout, encoding="latin-1")
    stderr = StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 0
    expected = native_output.encode("utf-8")
    assert raw_stdout.getvalue() == expected
    control = json.loads(stderr.getvalue())
    assert control["output"] == {
        "stream": "stdout",
        "encoding": "utf-8",
        "bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }


def test_query_binary_output_preserves_standard_broken_pipe_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()

    class BrokenBuffer:
        @staticmethod
        def write(_payload: bytes) -> int:
            raise BrokenPipeError(errno.EPIPE, "closed reader")

    standard_out = SimpleNamespace(buffer=BrokenBuffer(), fileno=lambda: 1)
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
        result = workspace_cli._emit_query_output(
            cast(Any, standard_out),
            "payload",
            b"payload",
            exit_code=0,
        )

    assert result == 0
    assert duplicated == [(99, 1), (99, 2)]
    assert closed == [99]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (FreshnessResult("withhold", "drift", None, None, True, "two_sided"), (10, "drifted", "drift", "sync_workspace")),
        (FreshnessResult("withhold", "source_unavailable", None, None, False), (10, "withheld", "source_unavailable", "restore_workspace_source")),
        (FreshnessResult("withhold", "timeout", None, None, True), (10, "timed_out", "timeout", "retry_workspace_query")),
        (FreshnessResult("withhold", "unstable", None, None, False), (10, "withheld", "unstable", "retry_workspace_query")),
        (FreshnessResult("withhold", "unsupported", None, None, False), (20, "unsupported", "query_unsupported", "run_workspace_doctor")),
    ],
)
def test_query_withholds_and_redacts_all_nonrelease_results(
    monkeypatch: pytest.MonkeyPatch,
    result: FreshnessResult[str],
    expected: tuple[int, str, str, str],
) -> None:
    workspace_cli = _cli()

    class Freshness:
        def query(self, *_args: object, **_kwargs: object) -> FreshnessResult[str]:
            return result

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: SimpleNamespace(freshness=Freshness()))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == expected[0]
    assert stdout.getvalue() == ""
    payload = _error_payload(stderr)
    assert payload["state"] == expected[1]
    assert payload["reason_code"] == expected[2]
    assert payload["action_code"] == expected[3]
    assert "output" not in payload and "repo_uuid" not in payload


def test_query_invalid_freshness_result_never_leaks_output_or_engine_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    malicious = "/private/source provider-secret native-output"

    class Freshness:
        def query(self, *_args: object, **_kwargs: object) -> FreshnessResult[str]:
            print(malicious)
            print(malicious, file=sys.stderr)
            return FreshnessResult("withhold", "drift", malicious, None, True, "two_sided")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: SimpleNamespace(freshness=Freshness()))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 20
    assert stdout.getvalue() == ""
    payload = _error_payload(stderr)
    assert payload["reason_code"] == "query_result_invalid"
    assert payload["action_code"] == "run_workspace_doctor"
    assert malicious not in stderr.getvalue()
    assert "output" not in payload and "repo_uuid" not in payload


def test_query_execution_failure_is_redacted_as_query_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()

    class Freshness:
        def query(self, *_args: object, **_kwargs: object) -> FreshnessResult[str]:
            raise RuntimeError("/private/source provider-secret")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(freshness=Freshness()),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 20
    assert stdout.getvalue() == ""
    payload = _error_payload(stderr)
    assert payload["reason_code"] == "query_failed"
    assert payload["action_code"] == "run_workspace_doctor"
    assert "/private" not in stderr.getvalue()
    assert "provider-secret" not in stderr.getvalue()


@pytest.mark.parametrize(
    "result",
    [
        FreshnessResult(
            "withhold",
            cast(Any, []),
            None,
            None,
            False,
            "not_observed",
        ),
        object(),
    ],
)
def test_query_malformed_freshness_result_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    result: object,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(
            freshness=SimpleNamespace(
                query=lambda *_args, **_kwargs: result,
            )
        ),
    )
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 20
    assert stdout.getvalue() == ""
    payload = _error_payload(stderr)
    assert payload["state"] == "invalid"
    assert payload["reason_code"] == "query_result_invalid"
    assert payload["action_code"] == "run_workspace_doctor"


def test_query_accepts_canonical_text_only_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(sys, "stdin", StringIO(_request_bytes().decode("utf-8")))
    raw = workspace_cli._read_query_request_bytes()
    assert raw == _request_bytes()
    assert workspace_cli._parse_query_request(raw).to_dict() == _request_value()


def test_top_level_query_skips_ambient_install_version_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("bounded query must not inspect ambient installs"),
    )
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys, "argv", ["graphify", "workspace", "query", "--request-stdin"]
    )
    mainmod._run_cli()
    assert observed == ["workspace"]


def test_top_level_help_lists_the_certified_workspace_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])
    mainmod._run_cli()
    assert "workspace query --request-stdin" in capsys.readouterr().out


@pytest.mark.parametrize("help_flag", ["-h", "--help", "-?"])
def test_top_level_query_help_and_version_check_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    help_flag: str,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("bounded query must not inspect ambient installs"),
    )
    monkeypatch.setattr(sys, "argv", ["graphify", "workspace", "query", help_flag])
    with pytest.raises(SystemExit) as raised:
        mainmod._run_cli()
    captured = capsys.readouterr()
    assert raised.value.code == 64
    assert captured.out == ""
    assert captured.err == _QUERY_USAGE + "\n"


def test_query_with_real_freshness_authority_writes_no_source_state_or_query_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_workspace_freshness import _runtime, _xattr_snapshot
    from tests.workspace_p3_helpers import metadata_snapshot, tree_snapshot

    workspace_cli = _cli()
    runtime = _runtime(tmp_path)
    home, codex_home = tmp_path / "home", tmp_path / "codex-home"
    home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    query_log = tmp_path / "forbidden-query-log.jsonl"
    monkeypatch.setenv("GRAPHIFY_QUERY_LOG", str(query_log))
    before = {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
        "home_tree": tree_snapshot(home),
        "home_metadata": metadata_snapshot(home),
        "codex_home_tree": tree_snapshot(codex_home),
        "codex_home_metadata": metadata_snapshot(codex_home),
    }
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(_request_bytes())))
    monkeypatch.setattr(workspace_cli, "compose_workspace_runtime", lambda _inputs: SimpleNamespace(freshness=runtime.authority))
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(
        ("query", "--request-stdin"), inputs=object(), stdout=stdout, stderr=stderr
    ) == 0
    assert stdout.getvalue() == "No matching nodes found."
    control = json.loads(stderr.getvalue())
    assert control["state"] == "released"
    assert control["output"]["bytes"] == len(stdout.getvalue().encode("utf-8"))
    assert control["output"]["sha256"] == hashlib.sha256(
        stdout.getvalue().encode("utf-8")
    ).hexdigest()
    assert not query_log.exists()
    assert "query" not in "\n".join(path.name for path in runtime.state_root.rglob("*"))
    assert {
        "source_tree": tree_snapshot(runtime.repo),
        "source_metadata": metadata_snapshot(runtime.repo),
        "source_xattrs": _xattr_snapshot(runtime.repo),
        "workspace_tree": tree_snapshot(runtime.state_root),
        "workspace_metadata": metadata_snapshot(runtime.state_root),
        "workspace_xattrs": _xattr_snapshot(runtime.state_root),
        "home_tree": tree_snapshot(home),
        "home_metadata": metadata_snapshot(home),
        "codex_home_tree": tree_snapshot(codex_home),
        "codex_home_metadata": metadata_snapshot(codex_home),
    } == before
