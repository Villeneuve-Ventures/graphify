"""P5B2 host-agent semantic-worker executable conformance."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, cast

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import graphify.workspace.semantic_worker as semantic_worker
from graphify.workspace.contracts import WorkspaceConfig, canonical_json_bytes
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.composition import WorkspaceRuntime
from graphify.workspace.leases import LeaseGrant
from graphify.workspace.persistence import InjectedFault
from graphify.workspace.semantic_queue import (
    SemanticDesiredWork,
    SemanticCheckpointCapacityUnavailable,
    SemanticClaim,
    SemanticQueueCapacityExceeded,
    SemanticQueuePolicy,
    SemanticQueueStore,
)
from tests.workspace_p3_helpers import REPO_UUID as HARNESS_REPO_UUID
from tests.workspace_p3_helpers import acquire, create_harness, tree_snapshot


REPO_UUID = "11111111-1111-4111-8111-111111111111"
BEGIN_SHA256 = "a" * 64
CLAIM_ID = "b" * 64
WORK_SHA256 = "c" * 64
_USAGE = "graphify workspace semantic-worker --stdio"


def _begin_value() -> dict[str, object]:
    return {
        "action": "begin",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "executor": "host_agent",
        "expected_active_source_revision": 1,
        "expected_desired_watermark": 1,
        "expected_migration_epoch": 0,
        "expected_operation_epoch": 1,
        "expected_queue_revision": 1,
        "expected_registry_revision": 1,
        "host_agent_active": True,
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "timeout_ms": 5_000,
    }


def _work() -> SemanticDesiredWork:
    return SemanticDesiredWork(
        source_epoch=1,
        policy_sha256="d" * 64,
        operation="UPSERT",
        path="README.md",
        content_sha256="e" * 64,
        desired_revision=1,
    )


def _fragment() -> dict[str, object]:
    return {
        "nodes": [
            {
                "id": "readme",
                "label": "README",
                "file_type": "document",
                "source_file": "README.md",
                "source_location": "L1",
                "source_url": None,
                "captured_at": None,
                "author": None,
                "contributor": None,
            },
            {
                "id": "workspace",
                "label": "Workspace",
                "file_type": "concept",
                "source_file": "README.md",
                "source_location": None,
                "source_url": None,
                "captured_at": None,
                "author": None,
                "contributor": None,
            },
        ],
        "edges": [
            {
                "source": "readme",
                "target": "workspace",
                "relation": "conceptually_related_to",
                "confidence": "INFERRED",
                "confidence_score": Decimal("0.75"),
                "source_file": "README.md",
                "source_location": "L1",
                "weight": Decimal("1"),
            }
        ],
        "hyperedges": [],
    }


def _complete_value() -> dict[str, object]:
    return {
        "action": "complete",
        "begin_request_sha256": BEGIN_SHA256,
        "claim_id": CLAIM_ID,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "payload": {"kind": "semantic_fragment", "fragment": _fragment()},
        "schema_version": 1,
    }


def _result_common() -> dict[str, object]:
    return {
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_result",
        "schema_version": 1,
    }


def test_semantic_worker_request_and_result_schemas_are_closed() -> None:
    request_schema = semantic_worker.load_request_schema()
    result_schema = semantic_worker.load_result_schema()
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(result_schema)
    request_validator = Draft202012Validator(request_schema, format_checker=FormatChecker())
    result_validator = Draft202012Validator(result_schema, format_checker=FormatChecker())

    begin: Any = _begin_value()
    assert not list(request_validator.iter_errors(begin))
    for field in begin:
        missing = dict(begin)
        missing.pop(field)
        assert list(request_validator.iter_errors(missing)), field
    assert list(request_validator.iter_errors({**begin, "backend": "gemini"}))
    assert list(request_validator.iter_errors({**begin, "host_agent_active": 1}))
    assert list(request_validator.iter_errors({**begin, "expected_operation_epoch": 0}))
    assert list(request_validator.iter_errors({**begin, "expected_queue_revision": -1}))
    assert list(request_validator.iter_errors({**begin, "timeout_ms": 600_001}))

    checkpoint: Any = {
        "action": "checkpoint",
        "begin_request_sha256": BEGIN_SHA256,
        "claim_id": CLAIM_ID,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "progress_code": "extract:parsed",
        "schema_version": 1,
    }
    fail: Any = {
        "action": "fail",
        "begin_request_sha256": BEGIN_SHA256,
        "claim_id": CLAIM_ID,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "error_code": "host_agent_transient",
        "retryable": True,
        "schema_version": 1,
    }
    for request in (checkpoint, _complete_value(), fail):
        assert not list(request_validator.iter_errors(cast(Any, request)))
    invalid_semantic_id: Any = _complete_value()
    invalid_semantic_id["payload"]["fragment"]["nodes"][0]["id"] = "node..name"
    assert list(request_validator.iter_errors(invalid_semantic_id))
    assert list(
        request_validator.iter_errors({**checkpoint, "progress_code": "result:" + "0" * 64})
    )
    assert list(request_validator.iter_errors({**fail, "retryable": False}))

    work: Any = {
        **_result_common(),
        "attempt": 2**60 + 1,
        "begin_request_sha256": BEGIN_SHA256,
        "claim_id": CLAIM_ID,
        "kind": "work",
        "repo_uuid": REPO_UUID,
        "work": _work().to_dict(),
        "work_sha256": WORK_SHA256,
    }
    invalid: Any = {
        **_result_common(),
        "action_code": "none",
        "exit_code": 20,
        "kind": "terminal",
        "outcome": "invalid",
        "reason_code": "semantic_worker_request_invalid",
    }
    assert not list(result_validator.iter_errors(work))
    assert not list(result_validator.iter_errors(invalid))
    assert list(result_validator.iter_errors(cast(Any, {**work, "payload": _fragment()})))
    assert list(result_validator.iter_errors({**invalid, "detail": "private error"}))
    assert list(
        result_validator.iter_errors(
            cast(
                Any,
                {
                    **_result_common(),
                    "action_code": "none",
                    "exit_code": 20,
                    "kind": "terminal",
                    "outcome": "commit_unknown",
                    "reason_code": "semantic_worker_commit_unknown",
                },
            )
        )
    )


@pytest.mark.parametrize(
    "token",
    [b"0.750", b"1.0", b"-0", b"1e0", b"0.0000001", b"NaN", b"Infinity"],
)
def test_semantic_worker_rejects_noncanonical_or_nonfinite_fixed_point_tokens(
    token: bytes,
) -> None:
    raw = semantic_worker.canonical_protocol_bytes(_complete_value())
    raw = raw.replace(b"0.75", token, 1)
    with pytest.raises(semantic_worker.SemanticWorkerRequestInvalid):
        semantic_worker.parse_request_frame(raw)


@pytest.mark.parametrize(
    "value", [Decimal("0"), Decimal("0.75"), Decimal("0.123456"), Decimal("1")]
)
def test_semantic_worker_retains_exact_fixed_point_values(value: Decimal) -> None:
    request = _complete_value()
    fragment = request["payload"]
    assert isinstance(fragment, dict)
    nested = fragment["fragment"]
    assert isinstance(nested, dict)
    edges = nested["edges"]
    assert isinstance(edges, list)
    edge = edges[0]
    assert isinstance(edge, dict)
    edge["confidence_score"] = value

    raw = semantic_worker.canonical_protocol_bytes(request)
    parsed = semantic_worker.parse_request_frame(raw)
    parsed_value = cast(Any, parsed.to_dict())["payload"]["fragment"]["edges"][0][
        "confidence_score"
    ]
    assert parsed_value == value
    assert isinstance(parsed_value, Decimal)
    assert semantic_worker.canonical_protocol_bytes(parsed.to_dict()) == raw


def test_semantic_payload_validation_reports_cooperative_progress() -> None:
    payload = cast(dict[str, object], deepcopy(_complete_value()["payload"]))
    fragment = cast(dict[str, object], payload["fragment"])
    edge = cast(list[dict[str, object]], fragment["edges"])[0]
    fragment["edges"] = [deepcopy(edge) for _ in range(1_024)]
    calls = 0

    def progress() -> None:
        nonlocal calls
        calls += 1

    validated = semantic_worker.validate_completion_payload(
        payload,
        _work(),
        progress=progress,
    )

    assert validated.kind == "semantic_fragment"
    assert calls >= 20


def test_semantic_payload_validation_preserves_catchable_interruption() -> None:
    calls = 0

    def interrupt() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        semantic_worker.validate_completion_payload(
            cast(dict[str, object], deepcopy(_complete_value()["payload"])),
            _work(),
            progress=interrupt,
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda payload: payload["fragment"].update({"provider": "secret"}), "field"),
        (lambda payload: payload["fragment"]["nodes"][0].update({"raw_source": "x"}), "field"),
        (lambda payload: payload["fragment"]["nodes"][1].update({"id": "readme"}), "unique"),
        (lambda payload: payload["fragment"]["edges"][0].update({"target": "missing"}), "endpoint"),
        (
            lambda payload: payload["fragment"]["edges"][0].update({"source_file": "/private"}),
            "source_file",
        ),
        (
            lambda payload: payload["fragment"]["nodes"][0].update({"label": "bad\u0000label"}),
            "control",
        ),
    ],
)
def test_semantic_worker_closed_payload_validator_rejects_smuggling_and_bad_graphs(
    mutate: Any,
    match: str,
) -> None:
    payload = deepcopy(_complete_value()["payload"])
    mutate(payload)
    with pytest.raises(semantic_worker.SemanticResultInvalid, match=match):
        semantic_worker.validate_completion_payload(payload, _work())


def test_semantic_worker_payload_validation_sanitizes_without_numeric_coercion() -> None:
    validated = semantic_worker.validate_completion_payload(
        deepcopy(_complete_value()["payload"]),
        _work(),
    )
    fragment = cast(Any, validated.value["fragment"])
    assert {node["id"] for node in fragment["nodes"]} == {"readme"}
    assert fragment["edges"] == []
    assert b'"confidence_score":0.75' not in validated.canonical
    assert validated.canonical.endswith(b"\n")
    assert validated.sha256 == semantic_worker.sha256(validated.canonical)


def test_result_binding_revalidates_the_closed_post_sanitize_rationale_shape() -> None:
    payload = cast(Any, deepcopy(_complete_value()["payload"]))
    fragment = payload["fragment"]
    fragment["nodes"][1]["file_type"] = "rationale"
    fragment["nodes"][1]["label"] = (
        "This rationale sentence is intentionally long enough to be propagated "
        "onto the surviving README node by the bounded sanitizer."
    )
    fragment["edges"][0]["source"] = "workspace"
    fragment["edges"][0]["target"] = "readme"
    fragment["edges"][0]["relation"] = "rationale_for"
    validated = semantic_worker.validate_completion_payload(payload, _work())
    assert "rationale" in cast(Any, validated.value["fragment"])["nodes"][0]
    claim = SemanticClaim(
        work=_work(),
        claim_id=CLAIM_ID,
        fence_token=1,
        operation_epoch=1,
        migration_epoch=0,
        active_source_revision=1,
        attempt=1,
        owner={"boot_id": "boot", "pid": 1, "process_start_id": "start"},
    )
    binding = semantic_worker.build_result_binding(
        begin_request_sha256=BEGIN_SHA256,
        repo_uuid=REPO_UUID,
        claim=claim,
        work_sha256=semantic_worker.sha256(
            semantic_worker.canonical_protocol_bytes(claim.work.to_dict())
        ),
        payload=validated,
    )

    assert semantic_worker.parse_result_binding(binding.canonical) == binding


def test_result_binding_parser_rejects_an_under_limit_payload_depth_bomb() -> None:
    work = _work()
    validated = semantic_worker.validate_completion_payload(
        deepcopy(_complete_value()["payload"]),
        work,
    )
    claim = SemanticClaim(
        work=work,
        claim_id=CLAIM_ID,
        fence_token=1,
        operation_epoch=1,
        migration_epoch=0,
        active_source_revision=1,
        attempt=1,
        owner={"boot_id": "boot", "pid": 1, "process_start_id": "start"},
    )
    binding = semantic_worker.build_result_binding(
        begin_request_sha256=BEGIN_SHA256,
        repo_uuid=REPO_UUID,
        claim=claim,
        work_sha256=semantic_worker.sha256(
            semantic_worker.canonical_protocol_bytes(work.to_dict())
        ),
        payload=validated,
    )
    depth_bomb = b"[" * 1_000 + b"0" + b"]" * 1_000
    fragment_end = b'},"kind":"semantic_fragment"}'
    poisoned_payload = validated.canonical[:-1].replace(
        fragment_end,
        b',"zz":' + depth_bomb + fragment_end,
        1,
    )
    raw = binding.canonical.replace(validated.canonical[:-1], poisoned_payload, 1)
    assert len(raw) < semantic_worker.COMPLETE_MAX_BYTES

    with pytest.raises(
        semantic_worker.SemanticResultInvalid,
        match="bound payload nesting is too deep",
    ):
        semantic_worker.parse_result_binding(raw)


def test_post_sanitize_rationale_accepts_only_the_frozen_multi_label_separator() -> None:
    payload = cast(Any, deepcopy(_complete_value()["payload"]))
    fragment = payload["fragment"]
    first = "First rationale explains why the README node remains the correct semantic target."
    second = "Second rationale independently explains why the same README node remains useful."
    rationale = fragment["nodes"][1]
    rationale["file_type"] = "rationale"
    rationale["label"] = first
    fragment["edges"][0].update(
        {
            "source": rationale["id"],
            "target": "readme",
            "relation": "rationale_for",
        }
    )
    second_node = deepcopy(rationale)
    second_node.update({"id": "workspace-second-rationale", "label": second})
    fragment["nodes"].append(second_node)
    second_edge = deepcopy(fragment["edges"][0])
    second_edge["source"] = second_node["id"]
    fragment["edges"].append(second_edge)

    validated = semantic_worker.validate_completion_payload(payload, _work())

    nodes = cast(Any, validated.value["fragment"])["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["rationale"] == first + "\n\n" + second


def test_rationale_projection_rejects_oversize_before_joining() -> None:
    assert semantic_worker._bounded_rationale_text(["one", "two"]) == "one\n\ntwo"
    with pytest.raises(semantic_worker.SemanticResultInvalid, match="rationale"):
        semantic_worker._bounded_rationale_text(["a" * 8192, "b" * 8192])


def test_public_and_internal_work_digests_bind_the_exact_work_object() -> None:
    work = _work()
    work_sha256 = semantic_worker.sha256(semantic_worker.canonical_protocol_bytes(work.to_dict()))
    work_result: dict[str, object] = {
        **_result_common(),
        "attempt": 1,
        "begin_request_sha256": BEGIN_SHA256,
        "claim_id": CLAIM_ID,
        "kind": "work",
        "repo_uuid": REPO_UUID,
        "work": work.to_dict(),
        "work_sha256": work_sha256,
    }
    assert (
        semantic_worker.parse_result_frame(semantic_worker.canonical_result_bytes(work_result))
        == work_result
    )

    substituted = {**work_result, "work_sha256": "0" * 64}
    with pytest.raises(semantic_worker.SemanticResultInvalid, match="work digest"):
        semantic_worker.canonical_result_bytes(substituted)
    with pytest.raises(semantic_worker.SemanticResultInvalid, match="work digest"):
        semantic_worker.parse_result_frame(semantic_worker.canonical_protocol_bytes(substituted))

    validated = semantic_worker.validate_completion_payload(
        deepcopy(_complete_value()["payload"]),
        work,
    )
    claim = SemanticClaim(
        work=work,
        claim_id=CLAIM_ID,
        fence_token=1,
        operation_epoch=1,
        migration_epoch=0,
        active_source_revision=1,
        attempt=1,
        owner={"boot_id": "boot", "pid": 1, "process_start_id": "start"},
    )
    binding = semantic_worker.build_result_binding(
        begin_request_sha256=BEGIN_SHA256,
        repo_uuid=REPO_UUID,
        claim=claim,
        work_sha256=work_sha256,
        payload=validated,
    )
    substituted_binding = {**binding.value, "work_sha256": "0" * 64}
    with pytest.raises(semantic_worker.SemanticResultInvalid, match="work digest"):
        semantic_worker.parse_result_binding(
            semantic_worker.canonical_protocol_bytes(substituted_binding)
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ("semantic-worker",),
        ("--stdio", "semantic-worker"),
        ("semantic-worker", "--stdio", "extra"),
        ("semantic-worker", "--stdio", "--stdio"),
        ("semantic-worker", "--help"),
    ],
)
def test_semantic_worker_usage_is_exact_before_authority_or_stdin(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    workspace_cli = pytest.importorskip("graphify.workspace.cli")
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("invalid argv must not load authority"),
    )

    class UnreadableStdin:
        @property
        def buffer(self) -> object:
            pytest.fail("invalid argv must not read stdin")

    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    stdout, stderr = StringIO(), StringIO()
    assert workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr) == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == _USAGE + "\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ("workspace", "semantic-worker"),
        ("workspace", "--stdio", "semantic-worker"),
        ("workspace", "semantic-worker", "--stdio", "extra"),
        ("workspace", "semantic-worker", "--stdio", "--stdio"),
        ("workspace", "semantic-worker", "--help"),
        ("workspace", "semantic-worker", "-h"),
        ("workspace", "semantic-worker", "-?"),
        ("semantic-worker", "--stdio"),
        ("--stdio", "workspace", "semantic-worker"),
        ("--version", "workspace", "semantic-worker", "--stdio"),
        ("--help", "workspace", "semantic-worker", "--stdio"),
        ("--version", "semantic-worker", "--stdio"),
        ("--help", "semantic-worker"),
        ("-h", "semantic-worker"),
        ("-?", "semantic-worker"),
        ("--version", "semantic-worker"),
        ("version", "semantic-worker"),
        ("install", "workspace", "semantic-worker", "--stdio"),
        ("uninstall", "workspace", "semantic-worker", "--stdio"),
        ("install", "semantic-worker"),
        ("uninstall", "semantic-worker"),
        ("extra", "workspace", "semantic-worker", "--stdio"),
    ],
)
def test_public_semantic_worker_negative_vectors_use_exact_usage_before_ambient_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("semantic-worker must not inspect ambient installs"),
    )
    monkeypatch.setattr(
        mainmod,
        "Path",
        lambda *_args, **_kwargs: pytest.fail("semantic-worker must not inspect paths"),
    )
    monkeypatch.setattr(sys, "argv", ["graphify", *arguments])

    with pytest.raises(SystemExit) as raised:
        mainmod._run_cli()

    captured = capsys.readouterr()
    assert raised.value.code == 64
    assert captured.out == ""
    assert captured.err == _USAGE + "\n"


def test_public_semantic_worker_exact_vector_skips_ambient_install_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("semantic-worker must not inspect ambient installs"),
    )
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", "workspace", "semantic-worker", "--stdio"],
    )

    mainmod._run_cli()

    assert observed == ["workspace"]


def test_semantic_worker_tokens_remain_free_text_for_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", "query", "workspace", "semantic-worker", "--stdio"],
    )

    mainmod._run_cli()

    assert observed == ["query"]


def test_semantic_worker_tokens_in_extract_values_do_not_change_command_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", "extract", "workspace", "--out", "semantic-worker"],
    )

    mainmod._run_cli()

    assert observed == ["extract"]


def test_semantic_worker_token_in_implicit_path_output_does_not_change_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", ".", "--out", "semantic-worker"],
    )

    mainmod._run_cli()

    assert observed == ["."]


def test_top_level_help_lists_the_semantic_worker_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])

    mainmod._run_cli()

    assert "workspace semantic-worker --stdio" in capsys.readouterr().out


def test_general_workspace_usage_lists_semantic_worker() -> None:
    from graphify.workspace import cli as workspace_cli

    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("unknown",), inputs=cast(Any, object()), stdout=stdout, stderr=stderr
        )
        == 64
    )
    assert _USAGE in stderr.getvalue()


def test_workspace_cli_import_does_not_eagerly_load_semantic_worker() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import graphify.workspace.cli; "
                "raise SystemExit(int('graphify.workspace.semantic_worker' in sys.modules))"
            ),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_valid_semantic_worker_dispatch_loads_and_composes_before_protocol_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace import cli as workspace_cli

    events: list[str] = []
    inputs = object()
    runtime = object()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: events.append("load") or inputs,
    )
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda value: events.append("compose") or (runtime if value is inputs else None),
    )
    monkeypatch.setattr(
        semantic_worker,
        "run_semantic_worker",
        lambda value, **_kwargs: events.append("protocol") or (0 if value is runtime else 20),
    )

    stdout, stderr = StringIO(), StringIO()
    assert (
        workspace_cli.run_workspace_command(
            ("semantic-worker", "--stdio"), stdout=stdout, stderr=stderr
        )
        == 0
    )
    assert events == ["load", "compose", "protocol"]
    assert stderr.getvalue() == ""


class _ProtocolOutput:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, value: bytes) -> int:
        self.frames.append(bytes(value))
        return len(value)

    def flush(self) -> None:
        return None


class _ShortProtocolOutput:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.value = bytearray()

    def write(self, value: bytes) -> int:
        count = min(self.maximum, len(value))
        self.value.extend(value[:count])
        return count

    def flush(self) -> None:
        return None


class _ShortTextOutput:
    def __init__(self) -> None:
        self.value = ""

    def write(self, value: object) -> int:
        if not isinstance(value, str):
            raise TypeError("text output accepts strings only")
        self.value += value[:1]
        return min(1, len(value))

    def flush(self) -> None:
        return None


class _ArmedFault:
    def __init__(self, event: str, *, skip: int = 0) -> None:
        self.event = event
        self.skip = skip
        self.armed = False
        self.fired = False

    def __call__(self, event: str) -> None:
        if not self.armed or self.fired or event != self.event:
            return
        if self.skip:
            self.skip -= 1
            return
        self.fired = True
        raise InjectedFault(event)


class _ArmedInterruption(_ArmedFault):
    def __call__(self, event: str) -> None:
        if not self.armed or self.fired or event != self.event:
            return
        if self.skip:
            self.skip -= 1
            return
        self.fired = True
        raise KeyboardInterrupt


class _CompleteAfterWorkInput:
    def __init__(self, begin: bytes, output: _ProtocolOutput, payload: object) -> None:
        self.begin = begin
        self.output = output
        self.payload = payload
        self.calls = 0

    def readline(self, _maximum: int = -1) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return self.begin
        if self.calls == 2:
            work = semantic_worker.parse_result_frame(self.output.frames[0])
            return semantic_worker.canonical_protocol_bytes(
                {
                    "action": "complete",
                    "begin_request_sha256": work["begin_request_sha256"],
                    "claim_id": work["claim_id"],
                    "cli_contract_version": 1,
                    "contract": "graphify.workspace.semantic_worker_request",
                    "payload": self.payload,
                    "schema_version": 1,
                }
            )
        return b""


class _RequestAfterWorkInput:
    def __init__(
        self,
        begin: bytes,
        output: _ProtocolOutput,
        request: Callable[[Mapping[str, object]], bytes],
    ) -> None:
        self.begin = begin
        self.output = output
        self.request = request
        self.calls = 0

    def readline(self, _maximum: int = -1) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return self.begin
        if self.calls == 2:
            return self.request(semantic_worker.parse_result_frame(self.output.frames[0]))
        return b""


class _OneFrameInput:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.used = False

    def readline(self, _maximum: int = -1) -> bytes:
        if self.used:
            return b""
        self.used = True
        return self.frame


def _runtime_with_one_readme_work(
    tmp_path: Path,
    *,
    max_bytes: int = 64 * 1024,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[WorkspaceRuntime, dict[str, object]]:
    harness = create_harness(tmp_path, fault_hook=fault_hook)
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=SemanticQueuePolicy(max_items=16, max_bytes=max_bytes, retry_budget=1),
        capabilities=harness.leases.state.capabilities,
        fault_hook=fault_hook,
    )
    build = acquire(harness, "BUILD", tick=1)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=harness.repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    observation = SourceObservation(
        source_commit=source_commit,
        inventory_sha256="2" * 64,
        policy_sha256="1" * 64,
        detector_id="semantic-worker-test",
        stable_inventory_passes=2,
        entries=(),
    )
    readme = (harness.repo / "README.md").read_bytes()
    queue.reconcile(
        (build),
        (
            SemanticDesiredWork(
                source_epoch=1,
                policy_sha256="1" * 64,
                operation="UPSERT",
                path="README.md",
                content_sha256=hashlib.sha256(readme).hexdigest(),
                desired_revision=1,
            ),
        ),
        source_epoch=1,
        policy_sha256="1" * 64,
        source_observations=(observation, observation),
        desired_watermark=1,
        semantic_required=True,
        monotonic_ns=10_001,
    )
    harness.leases.release(build)
    registry = harness.registry.load()
    entry = registry.to_dict()["workspaces"][0]
    lease_state = harness.leases.inspect(HARNESS_REPO_UUID)
    queue_state = queue.inspect(HARNESS_REPO_UUID)
    begin = {
        "action": "begin",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "executor": "host_agent",
        "expected_active_source_revision": entry["active_source_revision"],
        "expected_desired_watermark": queue_state.desired_watermark,
        "expected_migration_epoch": lease_state.migration_epoch,
        "expected_operation_epoch": lease_state.operation_epoch,
        "expected_queue_revision": queue_state.revision,
        "expected_registry_revision": registry.to_dict()["revision"],
        "host_agent_active": True,
        "repo_uuid": HARNESS_REPO_UUID,
        "schema_version": 1,
        "timeout_ms": 5_000,
    }
    runtime = WorkspaceRuntime(
        registry=harness.registry,
        leases=harness.leases,
        journal=None,  # type: ignore[arg-type]
        semantic_queue=queue,
        generations=None,  # type: ignore[arg-type]
        pointers=None,  # type: ignore[arg-type]
        freshness=None,  # type: ignore[arg-type]
        gc=None,  # type: ignore[arg-type]
    )
    return runtime, begin


def _idle_runtime(tmp_path: Path) -> tuple[WorkspaceRuntime, dict[str, object], Path]:
    harness = create_harness(tmp_path)
    queue = SemanticQueueStore(
        harness.state_root,
        harness.leases,
        policy=SemanticQueuePolicy(max_items=16, max_bytes=64 * 1024, retry_budget=1),
        capabilities=harness.leases.state.capabilities,
    )
    registry = harness.registry.load()
    entry = registry.to_dict()["workspaces"][0]
    lease_state = harness.leases.inspect(HARNESS_REPO_UUID)
    queue_state = queue.inspect(HARNESS_REPO_UUID)
    begin = {
        "action": "begin",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.semantic_worker_request",
        "executor": "host_agent",
        "expected_active_source_revision": entry["active_source_revision"],
        "expected_desired_watermark": 0,
        "expected_migration_epoch": lease_state.migration_epoch,
        "expected_operation_epoch": lease_state.operation_epoch,
        "expected_queue_revision": queue_state.revision,
        "expected_registry_revision": registry.to_dict()["revision"],
        "host_agent_active": True,
        "repo_uuid": HARNESS_REPO_UUID,
        "schema_version": 1,
        "timeout_ms": 5_000,
    }
    runtime = WorkspaceRuntime(
        registry=harness.registry,
        leases=harness.leases,
        journal=None,  # type: ignore[arg-type]
        semantic_queue=queue,
        generations=None,  # type: ignore[arg-type]
        pointers=None,  # type: ignore[arg-type]
        freshness=None,  # type: ignore[arg-type]
        gc=None,  # type: ignore[arg-type]
    )
    return runtime, begin, harness.repo


def _preflight_session(
    runtime: WorkspaceRuntime,
    begin: Mapping[str, object],
    output: _ProtocolOutput,
) -> semantic_worker._WorkerSession:
    clock = time.monotonic_ns
    deadline_ns = clock() + 5_000_000_000
    request = semantic_worker.parse_request_frame(semantic_worker.canonical_protocol_bytes(begin))
    session = semantic_worker._WorkerSession(
        runtime=runtime,
        reader=semantic_worker._FrameReader(
            cast(Any, _OneFrameInput(b"")),
            monotonic_clock=clock,
        ),
        stdout=output,  # type: ignore[arg-type]
        begin=request,
        monotonic_clock=clock,
        wall_clock=lambda: datetime.now(timezone.utc),
        deadline_ns=deadline_ns,
    )
    session.preflight = semantic_worker._preflight(
        runtime,
        begin,
        deadline_ns=deadline_ns,
        monotonic_clock=clock,
    )
    return session


def _active_session(
    runtime: WorkspaceRuntime,
    begin: Mapping[str, object],
    output: _ProtocolOutput,
) -> semantic_worker._WorkerSession:
    session = _preflight_session(runtime, begin, output)
    session.grant = session._acquire()
    session.claim = session._claim_work()
    assert session.claim is not None
    session.next_heartbeat_ns = time.monotonic_ns() + 10_000_000_000
    return session


def _assert_live_claim_timeout(
    runtime: WorkspaceRuntime,
    output: _ProtocolOutput,
    exit_code: int,
) -> None:
    assert exit_code == 10
    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "retry_scheduled"
    assert terminal["reason_code"] == "host_agent_timeout"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.status == "pending"
    assert item.failure_count == 1
    assert item.last_error == "host_agent_timeout"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_semantic_worker_same_process_stages_checkpoints_completes_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    source_root = runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root
    monkeypatch.chdir(source_root)
    payload = deepcopy(_complete_value()["payload"])
    output = _ProtocolOutput()
    protocol_input = _CompleteAfterWorkInput(
        semantic_worker.canonical_protocol_bytes(begin),
        output,
        payload,
    )

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=protocol_input,  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 0
    )

    assert len(output.frames) == 2
    work = semantic_worker.parse_result_frame(output.frames[0])
    completed = semantic_worker.parse_result_frame(output.frames[1])
    assert work["kind"] == "work"
    assert completed["outcome"] == "completed"
    snapshot = runtime.semantic_queue.inspect(HARNESS_REPO_UUID)
    assert snapshot.completed_watermark == snapshot.desired_watermark == 1
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases
    staged = (
        runtime.semantic_queue.state.root
        / "workspaces"
        / HARNESS_REPO_UUID
        / "semantic-staging"
        / cast(str, work["begin_request_sha256"])
        / "result.json"
    )
    assert staged.is_file()
    assert staged.stat().st_mode & 0o777 == 0o600
    assert staged.parent.stat().st_mode & 0o777 == 0o700


def test_post_claim_read_timeout_fails_once_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()

    def timeout_read(
        _session: semantic_worker._WorkerSession,
        *,
        deadline_ns: int,
    ) -> SemanticClaim:
        assert deadline_ns > 0
        raise semantic_worker.LockTimeout("post-claim read deadline")

    monkeypatch.setattr(semantic_worker._WorkerSession, "_read_current_claim", timeout_read)

    result = semantic_worker.run_semantic_worker(
        runtime,
        stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )

    _assert_live_claim_timeout(runtime, output, result)


def test_optional_checkpoint_lock_timeout_fails_once_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()

    def timeout_checkpoint(*_args: object, **_kwargs: object) -> SemanticClaim:
        raise semantic_worker.LockTimeout("checkpoint before visibility")

    def checkpoint_request(work: Mapping[str, object]) -> bytes:
        return semantic_worker.canonical_protocol_bytes(
            {
                "action": "checkpoint",
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
                "cli_contract_version": 1,
                "contract": "graphify.workspace.semantic_worker_request",
                "progress_code": "extract:parsed",
                "schema_version": 1,
            }
        )

    monkeypatch.setattr(runtime.semantic_queue, "checkpoint", timeout_checkpoint)
    result = semantic_worker.run_semantic_worker(
        runtime,
        stdin=_RequestAfterWorkInput(
            semantic_worker.canonical_protocol_bytes(begin),
            output,
            checkpoint_request,
        ),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )

    _assert_live_claim_timeout(runtime, output, result)


@pytest.mark.parametrize("boundary", ["inspect", "install", "reopen", "checkpoint"])
def test_completion_previsibility_deadlines_fail_once_and_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    state = runtime.semantic_queue.state

    def is_staging(relative: str | Path) -> bool:
        return "semantic-staging" in Path(relative).parts

    if boundary == "inspect":
        original = state.read_optional_existing_bytes

        def timeout_inspect(relative: str | Path, **kwargs: Any) -> bytes | None:
            if is_staging(relative):
                raise semantic_worker.LockTimeout("staging inspect deadline")
            return original(relative, **kwargs)

        monkeypatch.setattr(state, "read_optional_existing_bytes", timeout_inspect)
    elif boundary == "install":
        original_install = state.install_once_bytes

        def timeout_install(relative: str | Path, data: bytes, **kwargs: Any) -> Path:
            if is_staging(relative):
                raise semantic_worker.LockTimeout("staging install before visibility")
            return original_install(relative, data, **kwargs)

        monkeypatch.setattr(state, "install_once_bytes", timeout_install)
    elif boundary == "reopen":
        original_read = state.read_existing_bytes

        def timeout_reopen(relative: str | Path, **kwargs: Any) -> bytes:
            if is_staging(relative):
                raise semantic_worker.LockTimeout("staging reopen deadline")
            return original_read(relative, **kwargs)

        monkeypatch.setattr(state, "read_existing_bytes", timeout_reopen)
    else:
        def timeout_result_checkpoint(*_args: object, **_kwargs: object) -> SemanticClaim:
            raise semantic_worker.LockTimeout("result checkpoint before visibility")

        monkeypatch.setattr(runtime.semantic_queue, "checkpoint", timeout_result_checkpoint)

    result = semantic_worker.run_semantic_worker(
        runtime,
        stdin=_CompleteAfterWorkInput(
            semantic_worker.canonical_protocol_bytes(begin),
            output,
            deepcopy(_complete_value()["payload"]),
        ),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )

    _assert_live_claim_timeout(runtime, output, result)


def test_completion_transition_previsibility_timeout_fails_once_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()

    def timeout_complete(*_args: object, **_kwargs: object) -> object:
        raise semantic_worker.LockTimeout("completion before visibility")

    monkeypatch.setattr(runtime.semantic_queue, "complete", timeout_complete)
    result = semantic_worker.run_semantic_worker(
        runtime,
        stdin=_CompleteAfterWorkInput(
            semantic_worker.canonical_protocol_bytes(begin),
            output,
            deepcopy(_complete_value()["payload"]),
        ),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )

    _assert_live_claim_timeout(runtime, output, result)


@pytest.mark.parametrize(
    ("event", "skip"),
    [
        ("workspace:current_replaced", 0),
        ("semantic_queue:current_replaced", 0),
        ("semantic_result_binding:installed", 0),
        ("semantic_queue:current_replaced", 1),
        ("workspace:current_replaced", 1),
    ],
)
def test_semantic_worker_adopts_only_exact_uncertain_durable_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    skip: int,
) -> None:
    fault = _ArmedFault(event, skip=skip)
    runtime, begin = _runtime_with_one_readme_work(tmp_path, fault_hook=fault)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    fault.armed = True

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_CompleteAfterWorkInput(
                semantic_worker.canonical_protocol_bytes(begin),
                output,
                deepcopy(_complete_value()["payload"]),
            ),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 0
    )

    assert fault.fired
    assert semantic_worker.parse_result_frame(output.frames[-1])["outcome"] == "completed"


def test_uncertain_acquisition_distinguishes_authority_drift_from_ambiguity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _preflight_session(runtime, begin, _ProtocolOutput())
    preflight = session.preflight
    assert preflight is not None

    def uncertain_acquire(*_args: object, **_kwargs: object) -> object:
        raise InjectedFault("acquire")

    monkeypatch.setattr(runtime.leases, "acquire", uncertain_acquire)
    monkeypatch.setattr(
        semantic_worker,
        "_read_uncertain_lease_state",
        lambda *_args, **_kwargs: (
            preflight.registry_revision + 1,
            preflight.active_source_revision,
            preflight.lease_state,
            False,
        ),
    )

    with pytest.raises(semantic_worker._TerminalRoute) as captured:
        session._acquire()
    assert captured.value.reason_code == "semantic_authority_stale"

    def unreadable(*_args: object, **_kwargs: object):
        raise semantic_worker.StateCorrupt("unreadable")

    monkeypatch.setattr(semantic_worker, "_read_uncertain_lease_state", unreadable)
    with pytest.raises(semantic_worker.CommitUnknown):
        session._acquire()


def test_uncertain_claim_unreadable_reread_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _preflight_session(runtime, begin, _ProtocolOutput())
    session.grant = session._acquire()

    def uncertain_claim(*_args: object, **_kwargs: object) -> object:
        raise InjectedFault("claim")

    def unreadable(*_args: object, **_kwargs: object):
        raise semantic_worker.SemanticQueueCorrupt("unreadable")

    monkeypatch.setattr(runtime.semantic_queue, "claim", uncertain_claim)
    monkeypatch.setattr(semantic_worker, "_read_uncertain_queue", unreadable)

    with pytest.raises(semantic_worker.CommitUnknown):
        session._claim_work()


@pytest.mark.parametrize("post_commit", [False, True])
def test_acquisition_interruption_reconciles_then_ends_preclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_commit: bool,
) -> None:
    fault = _ArmedInterruption("workspace:current_replaced")
    runtime, begin = _runtime_with_one_readme_work(tmp_path, fault_hook=fault)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    if post_commit:
        fault.armed = True
    else:
        monkeypatch.setattr(
            runtime.leases,
            "acquire",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 10
    )

    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "withheld"
    assert terminal["reason_code"] == "semantic_worker_preclaim_interrupted"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.status == "pending"
    assert item.failure_count == 0


@pytest.mark.parametrize("post_commit", [False, True])
def test_claim_interruption_reconciles_then_ends_at_the_claim_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_commit: bool,
) -> None:
    fault = _ArmedInterruption("semantic_queue:current_replaced")
    runtime, begin = _runtime_with_one_readme_work(tmp_path, fault_hook=fault)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    if post_commit:
        fault.armed = True
    else:
        monkeypatch.setattr(
            runtime.semantic_queue,
            "claim",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    exit_code = semantic_worker.run_semantic_worker(
        runtime,
        stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )
    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    if post_commit:
        assert exit_code == 10
        assert terminal["outcome"] == "retry_scheduled"
        assert terminal["reason_code"] == "host_agent_interrupted"
        assert item.failure_count == 1
        assert item.last_error == "host_agent_interrupted"
    else:
        assert exit_code == 10
        assert terminal["outcome"] == "withheld"
        assert terminal["reason_code"] == "semantic_worker_preclaim_interrupted"
        assert item.failure_count == 0
        assert item.last_error is None
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


@pytest.mark.parametrize("phase", ["build", "validate", "emit"])
def test_work_frame_interruption_fails_and_releases_the_live_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    interrupted = False

    if phase == "build":
        original_work_result = semantic_worker._WorkerSession._work_result

        def interrupt_work_result(
            session: semantic_worker._WorkerSession,
        ) -> dict[str, object]:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_work_result(session)

        monkeypatch.setattr(
            semantic_worker._WorkerSession,
            "_work_result",
            interrupt_work_result,
        )
    elif phase == "validate":
        original_canonical_result_bytes = semantic_worker.canonical_result_bytes

        def interrupt_result_validation(
            value: Mapping[str, object],
        ) -> bytes:
            nonlocal interrupted
            if value.get("kind") == "work" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_canonical_result_bytes(value)

        monkeypatch.setattr(
            semantic_worker,
            "canonical_result_bytes",
            interrupt_result_validation,
        )
    else:
        original_write = output.write

        def interrupt_work_write(value: bytes) -> int:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_write(value)

        monkeypatch.setattr(output, "write", interrupt_work_write)

    try:
        exit_code = semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
    except KeyboardInterrupt:
        pytest.fail(f"{phase} interruption escaped the live-claim cleanup route")

    assert interrupted
    if phase == "emit":
        assert exit_code == 20
        assert output.frames == []
    else:
        assert exit_code == 10
        terminal = semantic_worker.parse_result_frame(output.frames[-1])
        assert terminal["outcome"] == "retry_scheduled"
        assert terminal["reason_code"] == "host_agent_interrupted"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.status == "pending"
    assert item.failure_count == 1
    assert item.last_error == "host_agent_interrupted"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


@pytest.mark.parametrize("action", ["complete", "fail"])
def test_terminal_write_interruption_does_not_repeat_the_queue_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    original_write = output.write
    interrupted = False

    def interrupt_terminal_write(value: bytes) -> int:
        nonlocal interrupted
        if output.frames and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_write(value)

    def terminal_request(work: Mapping[str, object]) -> bytes:
        if action == "complete":
            value = {
                **_complete_value(),
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
            }
        else:
            value = {
                "action": "fail",
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
                "cli_contract_version": 1,
                "contract": "graphify.workspace.semantic_worker_request",
                "error_code": "host_agent_transient",
                "retryable": True,
                "schema_version": 1,
            }
        return semantic_worker.canonical_protocol_bytes(value)

    monkeypatch.setattr(output, "write", interrupt_terminal_write)
    try:
        exit_code = semantic_worker.run_semantic_worker(
            runtime,
            stdin=_RequestAfterWorkInput(
                semantic_worker.canonical_protocol_bytes(begin),
                output,
                terminal_request,
            ),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
    except KeyboardInterrupt:
        pytest.fail(f"{action} terminal interruption escaped delivery cleanup")

    assert interrupted
    assert exit_code == 20
    assert len(output.frames) == 1
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    if action == "complete":
        assert item.status == "completed"
        assert item.failure_count == 0
        assert item.last_error is None
    else:
        assert item.status == "pending"
        assert item.failure_count == 1
        assert item.last_error == "host_agent_transient"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_checkpoint_write_interruption_fails_without_a_replacement_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    original_write = output.write
    interrupted = False

    def interrupt_checkpoint_write(value: bytes) -> int:
        nonlocal interrupted
        if output.frames and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_write(value)

    def checkpoint_request(work: Mapping[str, object]) -> bytes:
        return semantic_worker.canonical_protocol_bytes(
            {
                "action": "checkpoint",
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
                "cli_contract_version": 1,
                "contract": "graphify.workspace.semantic_worker_request",
                "progress_code": "extract:parsed",
                "schema_version": 1,
            }
        )

    monkeypatch.setattr(output, "write", interrupt_checkpoint_write)
    exit_code = semantic_worker.run_semantic_worker(
        runtime,
        stdin=_RequestAfterWorkInput(
            semantic_worker.canonical_protocol_bytes(begin),
            output,
            checkpoint_request,
        ),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    )

    assert interrupted
    assert exit_code == 20
    assert len(output.frames) == 1
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.status == "pending"
    assert item.failure_count == 1
    assert item.last_error == "host_agent_interrupted"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_current_claim_read_rejects_an_unexpected_checkpoint_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _active_session(runtime, begin, _ProtocolOutput())
    grant = cast(LeaseGrant, session.grant)
    claim = cast(SemanticClaim, session.claim)
    runtime.semantic_queue.checkpoint(
        grant,
        claim,
        checkpoint="unexpected",
        monotonic_ns=time.monotonic_ns(),
    )

    with pytest.raises(semantic_worker.SemanticQueueConflict):
        session._read_current_claim(deadline_ns=session.deadline_ns)


def test_checkpoint_rejects_concurrent_queue_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _active_session(runtime, begin, _ProtocolOutput())
    with runtime.registry.recovered_snapshot():
        with runtime.leases.workspace_lock(HARNESS_REPO_UUID):
            current = runtime.semantic_queue._load_locked(HARNESS_REPO_UUID)
            drifted = runtime.semantic_queue._commit_locked(current, current)
    assert drifted.revision == current.revision + 1

    with pytest.raises(semantic_worker.SemanticQueueConflict):
        session._checkpoint("extract:parsed")

    assert runtime.semantic_queue.inspect(HARNESS_REPO_UUID) == drifted


def _advance_registry_revision(runtime: WorkspaceRuntime) -> None:
    with runtime.registry.exclusive_lock():
        current = runtime.registry._load_locked()
        assert current is not None
        value = current.to_dict()
        runtime.registry._commit_locked(
            runtime.registry._document_value(
                current,
                cast(int, value["revision"]) + 1,
                cast(list[dict[str, object]], value["workspaces"]),
            )
        )


def test_checkpoint_rejects_registry_revision_drift_before_queue_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _active_session(runtime, begin, _ProtocolOutput())
    before = runtime.semantic_queue.inspect(HARNESS_REPO_UUID)
    _advance_registry_revision(runtime)

    with pytest.raises(semantic_worker.SemanticQueueConflict):
        session._checkpoint("extract:parsed")

    assert runtime.semantic_queue.inspect(HARNESS_REPO_UUID) == before


def test_heartbeat_rejects_registry_revision_drift_before_lease_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _active_session(runtime, begin, _ProtocolOutput())
    before = runtime.leases.inspect(HARNESS_REPO_UUID)
    _advance_registry_revision(runtime)

    with pytest.raises(semantic_worker.StaleLease):
        session._heartbeat()

    assert runtime.leases.inspect(HARNESS_REPO_UUID) == before


def test_uncertain_checkpoint_stale_reread_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _active_session(runtime, begin, _ProtocolOutput())

    def uncertain_checkpoint(*_args: object, **_kwargs: object) -> object:
        raise InjectedFault("checkpoint")

    def stale_reread(*, deadline_ns: int):
        del deadline_ns
        raise semantic_worker.StaleSemanticClaim("replaced")

    monkeypatch.setattr(runtime.semantic_queue, "checkpoint", uncertain_checkpoint)
    monkeypatch.setattr(session, "_read_uncertain_current_claim", stale_reread)

    with pytest.raises(semantic_worker.CommitUnknown):
        session._checkpoint("extract:parsed")


def test_uncertain_heartbeat_proven_absence_is_stale_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    session = _active_session(runtime, begin, _ProtocolOutput())
    registry = runtime.registry.load()
    entry = registry.to_dict()["workspaces"][0]
    observed = runtime.leases.inspect(HARNESS_REPO_UUID)
    absent = replace(
        observed,
        leases={key: value for key, value in observed.leases.items() if key != "semantic"},
        lease_epochs={
            key: value for key, value in observed.lease_epochs.items() if key != "semantic"
        },
    )

    def uncertain_heartbeat(*_args: object, **_kwargs: object) -> object:
        raise InjectedFault("heartbeat")

    monkeypatch.setattr(runtime.leases, "heartbeat", uncertain_heartbeat)
    monkeypatch.setattr(
        semantic_worker,
        "_read_uncertain_lease_state",
        lambda *_args, **_kwargs: (
            registry.to_dict()["revision"],
            entry["active_source_revision"],
            absent,
            False,
        ),
    )

    with pytest.raises(semantic_worker.StaleLease):
        session._heartbeat()


def test_heartbeat_interruption_adopts_then_fails_the_live_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    session = _active_session(runtime, begin, output)
    original = runtime.leases.heartbeat

    def interrupted_heartbeat(*args: object, **kwargs: object) -> object:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime.leases, "heartbeat", interrupted_heartbeat)
    with pytest.raises(KeyboardInterrupt):
        session._heartbeat()

    assert (
        session._fail_current(
            "host_agent_interrupted",
            True,
            emit_terminal=True,
        )
        == 10
    )
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.failure_count == 1
    assert item.last_error == "host_agent_interrupted"


def test_checkpoint_interruption_adopts_then_fails_the_live_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    session = _active_session(runtime, begin, output)
    original = runtime.semantic_queue.checkpoint

    def interrupted_checkpoint(*args: object, **kwargs: object) -> object:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime.semantic_queue, "checkpoint", interrupted_checkpoint)
    with pytest.raises(KeyboardInterrupt):
        session._checkpoint("extract:parsed")

    assert cast(SemanticClaim, session.claim).checkpoint == "extract:parsed"
    assert (
        session._fail_current(
            "host_agent_interrupted",
            True,
            emit_terminal=True,
        )
        == 10
    )
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.failure_count == 1
    assert item.last_error == "host_agent_interrupted"


def test_semantic_worker_never_replays_uncertain_queue_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = _ArmedFault("semantic_queue:current_replaced", skip=2)
    runtime, begin = _runtime_with_one_readme_work(tmp_path, fault_hook=fault)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    fault.armed = True

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_CompleteAfterWorkInput(
                semantic_worker.canonical_protocol_bytes(begin),
                output,
                deepcopy(_complete_value()["payload"]),
            ),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )

    assert fault.fired
    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "commit_unknown"
    assert terminal["reason_code"] == "semantic_worker_commit_unknown"
    with runtime.registry.read_only_snapshot():
        with runtime.leases.read_only_workspace_lock(HARNESS_REPO_UUID):
            snapshot, pending = runtime.semantic_queue.read_uncertain_snapshot_locked(
                HARNESS_REPO_UUID
            )
    assert pending is True
    assert snapshot.items[0].status == "completed"


def test_uncertain_state_reads_apply_deadlines_and_byte_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _begin = _runtime_with_one_readme_work(tmp_path)
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    observed: dict[str, tuple[int | None, int | None]] = {}

    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns) as document:
        with runtime.leases.read_only_workspace_lock(
            HARNESS_REPO_UUID,
            deadline_ns=deadline_ns,
        ):
            original_lease_read = runtime.leases.state.read_optional_existing_bytes
            original_queue_read = runtime.semantic_queue.state.read_optional_existing_bytes

            def bounded_lease_read(relative: str | Path, **kwargs: Any) -> bytes | None:
                observed[Path(relative).name] = (
                    kwargs.get("max_bytes"),
                    kwargs.get("deadline_ns"),
                )
                return original_lease_read(relative, **kwargs)

            def bounded_queue_read(relative: str | Path, **kwargs: Any) -> bytes | None:
                observed[Path(relative).name] = (
                    kwargs.get("max_bytes"),
                    kwargs.get("deadline_ns"),
                )
                return original_queue_read(relative, **kwargs)

            monkeypatch.setattr(
                runtime.leases.state,
                "read_optional_existing_bytes",
                bounded_lease_read,
            )
            monkeypatch.setattr(
                runtime.semantic_queue.state,
                "read_optional_existing_bytes",
                bounded_queue_read,
            )
            runtime.leases.read_uncertain_snapshot_locked(
                document,
                HARNESS_REPO_UUID,
                deadline_ns=deadline_ns,
            )
            runtime.semantic_queue.read_uncertain_snapshot_locked(
                HARNESS_REPO_UUID,
                deadline_ns=deadline_ns,
            )

    lease_bound, lease_deadline = observed["workspace.json"]
    queue_bound, queue_deadline = observed["semantic.jsonl"]
    assert lease_bound is not None and lease_bound > 0
    assert queue_bound == runtime.semantic_queue.policy.max_bytes
    assert lease_deadline == queue_deadline == deadline_ns


def test_semantic_queue_recovery_propagates_worker_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _begin = _runtime_with_one_readme_work(tmp_path)
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    observed_deadlines: list[int | None] = []

    with runtime.registry.read_only_snapshot(deadline_ns=deadline_ns):
        with runtime.leases.read_only_workspace_lock(
            HARNESS_REPO_UUID,
            deadline_ns=deadline_ns,
        ):
            original_recover = runtime.semantic_queue.state.recover_record

            def recover_record(**kwargs: Any) -> object:
                observed_deadlines.append(kwargs.get("deadline_ns"))
                return original_recover(**kwargs)

            monkeypatch.setattr(
                runtime.semantic_queue.state,
                "recover_record",
                recover_record,
            )
            runtime.semantic_queue._load_locked(
                HARNESS_REPO_UUID,
                deadline_ns=deadline_ns,
            )

    assert observed_deadlines == [deadline_ns]


def test_semantic_worker_truthful_idle_releases_before_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin, source_root = _idle_runtime(tmp_path)
    monkeypatch.chdir(source_root)
    output = _ProtocolOutput()

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 0
    )

    assert len(output.frames) == 1
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "idle"
    assert terminal["desired_watermark"] == terminal["completed_watermark"] == 0
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_semantic_worker_preflight_lease_recovery_is_workspace_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin, source_root = _idle_runtime(tmp_path)
    monkeypatch.chdir(source_root)
    output = _ProtocolOutput()

    def lease_recovery(*_args: object, **_kwargs: object) -> object:
        raise semantic_worker.StateRecoveryRequired("lease pending")

    monkeypatch.setattr(runtime.leases, "read_only_snapshot_locked", lease_recovery)
    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "invalid"
    assert terminal["reason_code"] == "workspace_state_invalid"


def test_semantic_worker_idle_inspect_interruption_is_preclaim_withheld(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin, source_root = _idle_runtime(tmp_path)
    monkeypatch.chdir(source_root)
    output = _ProtocolOutput()

    def interrupted_inspect(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        semantic_worker._WorkerSession,
        "_read_current_queue_snapshot",
        interrupted_inspect,
    )
    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 10
    )
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "withheld"
    assert terminal["reason_code"] == "semantic_worker_preclaim_interrupted"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_semantic_worker_idle_coordinate_drift_is_authority_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin, source_root = _idle_runtime(tmp_path)
    monkeypatch.chdir(source_root)
    output = _ProtocolOutput()

    def drifted_snapshot(*_args: object, **_kwargs: object) -> object:
        raise semantic_worker.SemanticQueueConflict("queue revision changed")

    monkeypatch.setattr(
        semantic_worker._WorkerSession,
        "_read_current_queue_snapshot",
        drifted_snapshot,
    )
    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 10
    )
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "withheld"
    assert terminal["reason_code"] == "semantic_authority_stale"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_semantic_worker_idle_release_interruption_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin, source_root = _idle_runtime(tmp_path)
    monkeypatch.chdir(source_root)
    output = _ProtocolOutput()

    def interrupted_release(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime.leases, "release", interrupted_release)
    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "commit_unknown"


def test_semantic_worker_output_writer_retries_short_writes_and_rejects_zero_progress() -> None:
    short = _ShortProtocolOutput(3)
    assert (
        semantic_worker.emit_pre_begin_failure(
            short,  # type: ignore[arg-type]
            reason_code="runtime_authority_missing",
            action_code="install_candidate_authority",
        )
        == 20
    )
    parsed = semantic_worker.parse_result_frame(bytes(short.value))
    assert parsed["reason_code"] == "runtime_authority_missing"

    stopped = _ShortProtocolOutput(0)
    assert (
        semantic_worker.emit_pre_begin_failure(
            stopped,  # type: ignore[arg-type]
            reason_code="runtime_authority_missing",
            action_code="install_candidate_authority",
        )
        == 20
    )
    assert stopped.value == b""

    text = _ShortTextOutput()
    assert (
        semantic_worker.emit_pre_begin_failure(
            text,  # type: ignore[arg-type]
            reason_code="runtime_authority_missing",
            action_code="install_candidate_authority",
        )
        == 20
    )
    assert semantic_worker.parse_result_frame(text.value.encode("utf-8"))["reason_code"] == (
        "runtime_authority_missing"
    )


def test_semantic_worker_unreadable_pre_begin_input_is_request_invalid() -> None:
    class UnreadableInput:
        def readline(self, _maximum: int = -1) -> bytes:
            raise OSError("closed")

    output = _ProtocolOutput()
    assert (
        semantic_worker.run_semantic_worker(
            object(),
            stdin=UnreadableInput(),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "invalid"
    assert terminal["reason_code"] == "semantic_worker_request_invalid"


def test_semantic_worker_interruption_after_accepted_begin_is_preclaim_withheld() -> None:
    class InterruptOnce:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return 0

    begin = semantic_worker.canonical_protocol_bytes(_begin_value())
    output = _ProtocolOutput()
    assert (
        semantic_worker.run_semantic_worker(
            object(),
            stdin=_OneFrameInput(begin),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
            monotonic_clock=InterruptOnce(),
        )
        == 10
    )
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "withheld"
    assert terminal["reason_code"] == "semantic_worker_preclaim_interrupted"
    assert terminal["begin_request_sha256"] == semantic_worker.sha256(begin)


def test_source_observation_rechecks_deadline_after_final_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "README.md"
    source.write_bytes(b"source")
    work = replace(
        _work(),
        content_sha256=hashlib.sha256(b"source").hexdigest(),
    )
    now = [0]
    original_read = semantic_worker.os.read

    def late_eof(descriptor: int, maximum: int) -> bytes:
        value = original_read(descriptor, maximum)
        if value == b"":
            now[0] = 2
        return value

    monkeypatch.setattr(semantic_worker.os, "read", late_eof)
    with pytest.raises(semantic_worker._FrameDeadline):
        semantic_worker._source_observation(
            tmp_path,
            work,
            deadline_ns=1,
            monotonic_clock=lambda: now[0],
        )


def test_semantic_worker_rejects_bad_begin_without_runtime_mutation(tmp_path: Path) -> None:
    runtime, _begin = _runtime_with_one_readme_work(tmp_path)
    before = tree_snapshot(runtime.semantic_queue.state.root)
    output = _ProtocolOutput()

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(b"{}\n"),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )

    assert tree_snapshot(runtime.semantic_queue.state.root) == before
    assert len(output.frames) == 1
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "invalid"
    assert terminal["reason_code"] == "semantic_worker_request_invalid"
    assert "begin_request_sha256" not in terminal


def test_semantic_worker_rejects_an_under_limit_request_depth_bomb() -> None:
    begin = semantic_worker.canonical_protocol_bytes(_begin_value())
    depth_bomb = b"[" * 1_000 + b"0" + b"]" * 1_000
    request = begin[:-2] + b',"zz":' + depth_bomb + b"}\n"
    assert len(request) < semantic_worker.BEGIN_MAX_BYTES
    output = _ProtocolOutput()

    assert (
        semantic_worker.run_semantic_worker(
            object(),
            stdin=_OneFrameInput(request),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )

    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "invalid"
    assert terminal["reason_code"] == "semantic_worker_request_invalid"


def test_result_parser_rejects_an_under_limit_depth_bomb() -> None:
    depth_bomb = b"[" * 1_000 + b"0" + b"]" * 1_000
    result = b'{"zz":' + depth_bomb + b"}\n"
    assert len(result) < semantic_worker.RESULT_MAX_BYTES

    with pytest.raises(
        semantic_worker.SemanticResultInvalid,
        match="public result nesting is too deep",
    ):
        semantic_worker.parse_result_frame(result)


def test_semantic_worker_preclaim_deadline_never_attributes_queue_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    begin["timeout_ms"] = 1
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    before = runtime.semantic_queue.inspect(HARNESS_REPO_UUID)
    output = _ProtocolOutput()

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
            monotonic_clock=lambda: 0,
        )
        == 10
    )

    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "withheld"
    assert terminal["reason_code"] == "semantic_worker_preclaim_timeout"
    assert runtime.semantic_queue.inspect(HARNESS_REPO_UUID) == before


def test_semantic_worker_caller_failure_retries_once_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()

    def failure(work: Mapping[str, object]) -> bytes:
        return semantic_worker.canonical_protocol_bytes(
            {
                "action": "fail",
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
                "cli_contract_version": 1,
                "contract": "graphify.workspace.semantic_worker_request",
                "error_code": "host_agent_transient",
                "retryable": True,
                "schema_version": 1,
            }
        )

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_RequestAfterWorkInput(
                semantic_worker.canonical_protocol_bytes(begin),
                output,
                failure,
            ),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 10
    )

    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "retry_scheduled"
    assert terminal["reason_code"] == "host_agent_transient"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.status == "pending"
    assert item.failure_count == 1
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_caller_failure_interruption_before_mutation_preserves_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    interrupted = False
    original_fail_current = semantic_worker._WorkerSession._fail_current

    def interrupt_before_failure_mutation(
        session: semantic_worker._WorkerSession,
        error_code: str,
        retryable: bool,
        *,
        emit_terminal: bool,
    ) -> int:
        nonlocal interrupted
        if error_code == "host_agent_transient" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_fail_current(
            session,
            error_code,
            retryable,
            emit_terminal=emit_terminal,
        )

    def failure(work: Mapping[str, object]) -> bytes:
        return semantic_worker.canonical_protocol_bytes(
            {
                "action": "fail",
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
                "cli_contract_version": 1,
                "contract": "graphify.workspace.semantic_worker_request",
                "error_code": "host_agent_transient",
                "retryable": True,
                "schema_version": 1,
            }
        )

    monkeypatch.setattr(
        semantic_worker._WorkerSession,
        "_fail_current",
        interrupt_before_failure_mutation,
    )
    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_RequestAfterWorkInput(
                semantic_worker.canonical_protocol_bytes(begin),
                output,
                failure,
            ),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 10
    )

    assert interrupted
    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "retry_scheduled"
    assert terminal["reason_code"] == "host_agent_transient"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.failure_count == 1
    assert item.last_error == "host_agent_transient"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_caller_failure_after_deadline_becomes_transport_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    session = _active_session(runtime, begin, output)
    session.deadline_ns = session.monotonic_clock()

    assert session._fail_accepted_request("host_agent_transient", True) == 10

    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "retry_scheduled"
    assert terminal["reason_code"] == "host_agent_timeout"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.failure_count == 1
    assert item.last_error == "host_agent_timeout"
    assert "semantic" not in runtime.leases.inspect(HARNESS_REPO_UUID).leases


def test_semantic_worker_late_caller_failure_return_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    monkeypatch.chdir(runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root)
    output = _ProtocolOutput()
    now = [time.monotonic_ns()]
    original_fail = runtime.semantic_queue.fail

    def late_fail(*args: object, **kwargs: object):
        snapshot = original_fail(*args, **kwargs)  # type: ignore[arg-type]
        now[0] += 6_000_000_000
        return snapshot

    monkeypatch.setattr(runtime.semantic_queue, "fail", late_fail)

    def failure(work: Mapping[str, object]) -> bytes:
        return semantic_worker.canonical_protocol_bytes(
            {
                "action": "fail",
                "begin_request_sha256": work["begin_request_sha256"],
                "claim_id": work["claim_id"],
                "cli_contract_version": 1,
                "contract": "graphify.workspace.semantic_worker_request",
                "error_code": "host_agent_transient",
                "retryable": True,
                "schema_version": 1,
            }
        )

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_RequestAfterWorkInput(
                semantic_worker.canonical_protocol_bytes(begin),
                output,
                failure,
            ),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
            monotonic_clock=lambda: now[0],
        )
        == 20
    )

    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "commit_unknown"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.last_error == "host_agent_transient"
    assert item.failure_count == 1


def test_semantic_worker_proven_source_change_dead_letters_without_work_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    source_root = runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root
    (source_root / "README.md").write_text("changed after reconciliation\n", encoding="utf-8")
    monkeypatch.chdir(source_root)
    output = _ProtocolOutput()

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=_OneFrameInput(semantic_worker.canonical_protocol_bytes(begin)),  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )

    assert len(output.frames) == 1
    terminal = semantic_worker.parse_result_frame(output.frames[0])
    assert terminal["outcome"] == "dead_lettered"
    assert terminal["reason_code"] == "source_content_changed"
    item = runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0]
    assert item.status == "dead_letter"
    assert item.failure_count == 1


def test_semantic_worker_different_staged_binding_dead_letters_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, begin = _runtime_with_one_readme_work(tmp_path)
    source_root = runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root
    monkeypatch.chdir(source_root)
    begin_bytes = semantic_worker.canonical_protocol_bytes(begin)
    begin_sha256 = semantic_worker.sha256(begin_bytes)
    relative = (
        Path("workspaces") / HARNESS_REPO_UUID / "semantic-staging" / begin_sha256 / "result.json"
    )
    conflict = b'{"conflict":true}\n'
    runtime.semantic_queue.state.install_once_bytes(
        relative,
        conflict,
        label="semantic_worker_test_conflict",
    )
    output = _ProtocolOutput()
    protocol_input = _CompleteAfterWorkInput(
        begin_bytes,
        output,
        deepcopy(_complete_value()["payload"]),
    )

    assert (
        semantic_worker.run_semantic_worker(
            runtime,
            stdin=protocol_input,  # type: ignore[arg-type]
            stdout=output,  # type: ignore[arg-type]
        )
        == 20
    )

    terminal = semantic_worker.parse_result_frame(output.frames[-1])
    assert terminal["outcome"] == "dead_lettered"
    assert terminal["reason_code"] == "semantic_result_binding_conflict"
    assert runtime.semantic_queue.state.read_existing_bytes(relative) == conflict
    assert runtime.semantic_queue.inspect(HARNESS_REPO_UUID).items[0].status == "dead_letter"


def _direct_semantic_claim(
    runtime: WorkspaceRuntime,
    begin: Mapping[str, object],
) -> tuple[LeaseGrant, SemanticClaim]:
    monotonic_ns = time.monotonic_ns()
    grant = runtime.leases.acquire(
        HARNESS_REPO_UUID,
        "SEMANTIC_CLAIM",
        runtime.leases.current_owner(),
        expected_registry_revision=cast(int, begin["expected_registry_revision"]),
        expected_active_source_revision=cast(int, begin["expected_active_source_revision"]),
        expected_operation_epoch=cast(int, begin["expected_operation_epoch"]),
        expected_migration_epoch=cast(int, begin["expected_migration_epoch"]),
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=monotonic_ns,
        ttl_ns=30_000_000_000,
    )
    source_root = runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root
    config = WorkspaceConfig.from_toml((source_root / ".graphify/workspace.toml").read_bytes())
    claim = runtime.semantic_queue.claim(
        grant,
        config=config,
        host_agent_active=True,
        explicit_backend=None,
        monotonic_ns=monotonic_ns + 1,
        expected_queue_revision=cast(int, begin["expected_queue_revision"]),
        expected_desired_watermark=cast(int, begin["expected_desired_watermark"]),
    )
    return grant, cast(SemanticClaim, claim)


def test_semantic_claim_reserves_exact_mandatory_result_checkpoint_capacity(
    tmp_path: Path,
) -> None:
    template_runtime, template_begin = _runtime_with_one_readme_work(tmp_path / "template")
    _grant, claim = _direct_semantic_claim(template_runtime, template_begin)
    assert claim is not None
    claimed = template_runtime.semantic_queue.inspect(HARNESS_REPO_UUID)

    boundary = claimed.queue_bytes
    for _ in range(8):
        policy = SemanticQueuePolicy(max_items=16, max_bytes=boundary, retry_budget=1)
        boundary = len(replace(claimed, queue_policy=policy).canonical)
    policy = SemanticQueuePolicy(max_items=16, max_bytes=boundary, retry_budget=1)
    actual = replace(claimed, queue_policy=policy)
    projected_items = tuple(
        replace(
            item,
            claim=replace(item.claim, checkpoint="result:" + "0" * 64),
        )
        if item.claim is not None
        else item
        for item in actual.items
    )
    assert len(actual.canonical) <= boundary
    assert len(replace(actual, items=projected_items).canonical) > boundary

    runtime, begin = _runtime_with_one_readme_work(
        tmp_path / "boundary",
        max_bytes=boundary,
    )
    monotonic_ns = time.monotonic_ns()
    grant = runtime.leases.acquire(
        HARNESS_REPO_UUID,
        "SEMANTIC_CLAIM",
        runtime.leases.current_owner(),
        expected_registry_revision=cast(int, begin["expected_registry_revision"]),
        expected_active_source_revision=cast(int, begin["expected_active_source_revision"]),
        expected_operation_epoch=cast(int, begin["expected_operation_epoch"]),
        expected_migration_epoch=cast(int, begin["expected_migration_epoch"]),
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=monotonic_ns,
        ttl_ns=30_000_000_000,
    )
    source_root = runtime.registry.resolve_active_source(HARNESS_REPO_UUID).root
    config = WorkspaceConfig.from_toml((source_root / ".graphify/workspace.toml").read_bytes())
    before = runtime.semantic_queue.inspect(HARNESS_REPO_UUID)

    with pytest.raises(SemanticCheckpointCapacityUnavailable):
        runtime.semantic_queue.claim(
            grant,
            config=config,
            host_agent_active=True,
            explicit_backend=None,
            monotonic_ns=monotonic_ns + 1,
            expected_queue_revision=cast(int, begin["expected_queue_revision"]),
            expected_desired_watermark=cast(int, begin["expected_desired_watermark"]),
        )

    assert runtime.semantic_queue.inspect(HARNESS_REPO_UUID) == before


def test_semantic_checkpoint_capacity_checks_actual_and_reserved_bytes(
    tmp_path: Path,
) -> None:
    template_runtime, template_begin = _runtime_with_one_readme_work(tmp_path / "template")
    _template_grant, template_claim = _direct_semantic_claim(
        template_runtime,
        template_begin,
    )
    template_snapshot = template_runtime.semantic_queue.inspect(HARNESS_REPO_UUID)
    projected_items = tuple(
        replace(
            item,
            claim=replace(item.claim, checkpoint="result:" + "0" * 64),
        )
        if item.claim is not None
        else item
        for item in template_snapshot.items
    )
    reserved_bytes = len(replace(template_snapshot, items=projected_items).canonical)

    runtime, begin = _runtime_with_one_readme_work(
        tmp_path / "boundary",
        max_bytes=reserved_bytes,
    )
    grant, claim = _direct_semantic_claim(runtime, begin)
    before = runtime.semantic_queue.inspect(HARNESS_REPO_UUID)

    with pytest.raises(SemanticQueueCapacityExceeded):
        runtime.semantic_queue.checkpoint(
            grant,
            claim,
            checkpoint="x" * 256,
            monotonic_ns=time.monotonic_ns(),
        )

    assert runtime.semantic_queue.inspect(HARNESS_REPO_UUID) == before
