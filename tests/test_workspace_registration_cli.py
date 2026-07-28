from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO, StringIO
import importlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import (
    RUNTIME_AUTHORITY_FILENAME,
    WorkspaceRuntimeInputs,
    WorkspaceRuntimeAuthority,
)
from graphify.workspace.contracts import canonical_sha256
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityAction,
    OperatorAuthorization,
    SourceIdentity,
    SourceAmbiguousError,
    SourceDiscoveryTimeout,
    UUIDCollisionError,
    discover_source,
)
from graphify.workspace.persistence import InjectedFault, RuntimeCapabilities, UnsupportedRuntime
from graphify.workspace.registry import RegistryStore, RevisionConflict
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    WORKSPACE_USAGE,
    clone_repo,
    create_repo,
    git_output as _git,
    metadata_snapshot,
    tree_snapshot,
)


REPO_UUID = "11111111-1111-4111-8111-111111111111"
SECOND_UUID = "22222222-2222-4222-8222-222222222222"
SUPPORTED = RuntimeCapabilities.supported_test_fixture()
POLICY = SemanticQueuePolicy(max_items=8, max_bytes=16 * 1024, retry_budget=1)
REGISTRATION_REMOTE = "https://github.com/example/registration.git"
_SUBPROCESS_LAUNCHER = """
import runpy
from graphify.workspace.persistence import RuntimeCapabilities
RuntimeCapabilities.detect = classmethod(
    lambda cls, path: cls.supported_test_fixture()
)
runpy.run_module("graphify", run_name="__main__")
"""


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _authorization_payload(action: str) -> str:
    return json.dumps(
        {
            "action": action,
            "issued_at": "2026-07-16T15:00:00Z",
            "nonce": f"nonce-{action.lower()}",
            "operator_id": "operator:registration-test",
            "reason": "operator authorization that must remain private",
        }
    )


def _inputs(state_root: Path, *, fault_hook: Any = None) -> WorkspaceRuntimeInputs:
    return WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=POLICY,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
    )


def _run_register(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cwd: Path,
    action: str,
    expected_revision: int,
    inputs: WorkspaceRuntimeInputs | None,
    repo_uuid: str = REPO_UUID,
    authorization: str | None = None,
    stdin: Any | None = None,
) -> tuple[int, StringIO, StringIO]:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "Path", SimpleNamespace(cwd=lambda: cwd), raising=False)
    authorization_input = (
        stdin
        if stdin is not None
        else StringIO(authorization or _authorization_payload(action.upper()))
    )
    monkeypatch.setattr(sys, "stdin", authorization_input)
    stdout = StringIO()
    stderr = StringIO()
    exit_code = workspace_cli.run_workspace_command(
        [
            "register",
            action,
            "--repo-uuid",
            repo_uuid,
            "--expected-registry-revision",
            str(expected_revision),
            "--authorization-stdin",
        ],
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    )
    return exit_code, stdout, stderr


def _registration_payload(stream: StringIO) -> dict[str, Any]:
    payload = json.loads(stream.getvalue())
    Draft202012Validator(
        _cli().load_registration_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    return payload


def _identity_maintenance_payload(stream: StringIO) -> dict[str, Any]:
    payload = json.loads(stream.getvalue())
    Draft202012Validator(
        _cli().load_identity_maintenance_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    return payload


class _AllowingState:
    def assert_external_to(self, _source_root: Path) -> None:
        pass


def _source_stub(root: Path) -> SimpleNamespace:
    remote_evidence = {
        "kind": "graphify.workspace.remote_evidence",
        "remote_name": "origin",
        "url": REGISTRATION_REMOTE,
    }
    registry_source = {
        "git_common_dir": str(root / ".git"),
        "path": str(root),
        "remote_aliases": [
            {
                "evidence_sha256": canonical_sha256(remote_evidence),
                "url": remote_evidence["url"],
            }
        ],
        "worktree_id": "main",
    }
    return SimpleNamespace(
        git_common_device=0,
        git_common_inode=0,
        head_commit="a" * 40,
        repo_uuid=REPO_UUID,
        root=root,
        registry_source=registry_source,
        remote_evidence=(remote_evidence,),
        source_sha256=canonical_sha256(registry_source),
    )


def test_registration_docs_freeze_exact_authorization_stdin_contract() -> None:
    readme = (
        Path(__file__).parents[1] / "docs/workspace/v1/README.md"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"```json\n(?P<payload>\{\"action\":\"ENROLL\"[^\n]+\})\n```",
        readme,
    )

    assert match is not None
    payload = json.loads(match.group("payload"))
    assert set(payload) == {"action", "issued_at", "nonce", "operator_id", "reason"}
    assert all(isinstance(value, str) for value in payload.values())
    assert payload["action"] == "ENROLL"
    assert "replace `ENROLL` with `ADOPT`" in readme
    assert "`REBIND`, or `ROTATE` respectively" in readme
    assert "graphify.workspace.identity_maintenance" in readme
    assert "identity-maintenance.schema.json" in readme


def test_registration_schema_freezes_success_conflict_and_invalid_receipts() -> None:
    schema = _cli().load_registration_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["action"]["enum"] == ["enroll", "adopt"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    common = {
        "action": "enroll",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.registration",
        "schema_version": 1,
    }
    valid = [
        {
            **common,
            "exit_code": 0,
            "registry_revision": 1,
            "repo_uuid": REPO_UUID,
            "state": "registered",
        },
        {
            **common,
            "action_code": "refresh_registry_revision",
            "exit_code": 10,
            "reason_code": "revision_conflict",
            "registry_revision": 2,
            "state": "conflict",
        },
        {
            **common,
            "action_code": "verify_registration_identity",
            "exit_code": 10,
            "reason_code": "uuid_collision",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "run_workspace_doctor",
            "exit_code": 10,
            "reason_code": "revision_conflict",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "provide_valid_authorization",
            "exit_code": 20,
            "reason_code": "authorization_invalid",
            "state": "invalid",
        },
    ]

    for receipt in valid:
        assert not list(validator.iter_errors(receipt))

    missing_retry_revision = dict(valid[1])
    missing_retry_revision.pop("registry_revision")
    contradictory_invalid = {**valid[3], "registry_revision": 2}
    success_with_failure_code = {**valid[0], "reason_code": "registration_failed"}
    for receipt in (
        missing_retry_revision,
        contradictory_invalid,
        success_with_failure_code,
    ):
        assert list(validator.iter_errors(receipt))


def test_identity_maintenance_schema_freezes_success_conflict_and_invalid_receipts() -> None:
    schema = _cli().load_identity_maintenance_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    common = {
        "action": "rebind",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.identity_maintenance",
        "schema_version": 1,
    }
    valid = [
        {
            **common,
            "exit_code": 0,
            "registry_revision": 2,
            "repo_uuid": REPO_UUID,
            "state": "maintained",
        },
        {
            **common,
            "action_code": "refresh_registry_revision",
            "exit_code": 10,
            "reason_code": "revision_conflict",
            "registry_revision": 3,
            "state": "conflict",
        },
        {
            **common,
            "action_code": "verify_identity_maintenance_target",
            "exit_code": 10,
            "reason_code": "identity_mismatch",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "enroll_or_adopt_source",
            "exit_code": 10,
            "reason_code": "source_not_bound",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "run_workspace_doctor",
            "exit_code": 10,
            "reason_code": "revision_conflict",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "provide_valid_authorization",
            "exit_code": 20,
            "reason_code": "authorization_invalid",
            "state": "invalid",
        },
    ]

    for receipt in valid:
        assert not list(validator.iter_errors(receipt))

    missing_observed_revision = dict(valid[1])
    missing_observed_revision.pop("registry_revision")
    invalid_with_revision = {**valid[-1], "registry_revision": 3}
    success_with_failure_code = {
        **valid[0],
        "reason_code": "identity_maintenance_failed",
    }
    registration_action = {**valid[0], "action": "enroll"}
    for receipt in (
        missing_observed_revision,
        invalid_with_revision,
        success_with_failure_code,
        registration_action,
    ):
        assert list(validator.iter_errors(receipt))


def _assert_external_state_allowlist(state_root: Path, *, includes_authority: bool) -> None:
    allowed = re.compile(
        r"(?:registry(?:\.(?:previous|pending))?\.json|registry\.lock|"
        r"evidence/[0-9a-f]{64}\.json|workspaces/"
        + REPO_UUID
        + r"/workspace(?:\.(?:previous|pending))?\.json|"
        r"workspaces/" + REPO_UUID + r"/workspace\.lock)"
    )
    files = [path for path in state_root.rglob("*") if path.is_file()]
    unexpected = {
        path.relative_to(state_root).as_posix()
        for path in files
        if not allowed.fullmatch(path.relative_to(state_root).as_posix())
    }
    expected_unmatched = {RUNTIME_AUTHORITY_FILENAME} if includes_authority else set()
    assert unexpected == expected_unmatched
    directories = {
        path.relative_to(state_root).as_posix() for path in state_root.rglob("*") if path.is_dir()
    }
    assert directories == {
        "evidence",
        "workspaces",
        f"workspaces/{REPO_UUID}",
    }
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in state_root.rglob("*")
        if path.is_dir()
    )
    assert all(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 for path in files)


@pytest.mark.parametrize(("action", "method"), [("enroll", "enroll"), ("adopt", "adopt")])
def test_register_emits_the_versioned_canonical_success_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    method: str,
) -> None:
    workspace_cli = _cli()
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    source = _source_stub(cwd)
    calls: list[tuple[str, object, object, int]] = []
    discoveries: list[tuple[Path, int, int | None]] = []

    class Registry:
        state = _AllowingState()

        def enroll(
            self, discovered: object, authorization: object, *, expected_revision: int
        ) -> Any:
            calls.append(("enroll", discovered, authorization, expected_revision))
            return SimpleNamespace(to_dict=lambda: {"revision": 4})

        def adopt(
            self, discovered: object, authorization: object, *, expected_revision: int
        ) -> Any:
            calls.append(("adopt", discovered, authorization, expected_revision))
            return SimpleNamespace(to_dict=lambda: {"revision": 4})

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )
    def discover(
        root: Path,
        *,
        deadline_ns: int,
        max_bytes: int | None,
    ) -> object:
        discoveries.append((root, deadline_ns, max_bytes))
        return source

    monkeypatch.setattr(workspace_cli, "discover_source", discover, raising=False)
    monkeypatch.setattr(workspace_cli.time, "monotonic_ns", lambda: 123)
    monkeypatch.setattr(workspace_cli, "Path", SimpleNamespace(cwd=lambda: cwd), raising=False)
    monkeypatch.setattr(
        workspace_cli,
        "verify_source_checkout",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO(_authorization_payload(action.upper())))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            action,
            "--expected-registry-revision",
            "3",
            "--authorization-stdin",
            "--repo-uuid",
            REPO_UUID,
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert calls[0][0] == method
    assert calls[0][1] is source
    assert calls[0][3] == 3
    assert discoveries == [
        (cwd, 5_000_000_123, 64 * 1024),
        (cwd, 5_000_000_123, 64 * 1024),
    ]
    assert isinstance(calls[0][2], OperatorAuthorization)
    assert calls[0][2].to_dict() == json.loads(_authorization_payload(action.upper()))
    expected = {
        "contract": "graphify.workspace.registration",
        "schema_version": 1,
        "cli_contract_version": 1,
        "state": "registered",
        "action": action,
        "exit_code": 0,
        "repo_uuid": REPO_UUID,
        "registry_revision": 4,
    }
    assert stdout.getvalue().encode("utf-8") == (
        json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    assert stderr.getvalue() == ""
    assert "operator authorization" not in stdout.getvalue()


@pytest.mark.parametrize(
    "arguments",
    [
        ["register"],
        ["register", "enroll", "--repo-uuid", REPO_UUID],
        ["register", "rebind", "--repo-uuid", REPO_UUID],
        [
            "register",
            "rotate",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "1",
            "--authorization-stdin",
            "--authorization-stdin",
        ],
        [
            "register",
            "delete",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ],
        [
            "register",
            "activate",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ],
        [
            "register",
            "enroll",
            "--repo-uuid",
            "not-a-uuid",
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ],
        [
            "register",
            "enroll",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "-1",
            "--authorization-stdin",
        ],
    ],
)
def test_register_usage_errors_do_not_read_stdin_or_discover_state(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("must not load authority"),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root: pytest.fail("must not discover source"),
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO("must-not-be-read"))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(arguments, stdout=stdout, stderr=stderr)

    assert result == 64
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == WORKSPACE_USAGE


def test_register_internal_dispatch_rejects_unsupported_action_before_authority_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("unsupported register action must not load authority"),
    )
    stdout = StringIO()
    stderr = StringIO()

    with pytest.raises(AssertionError, match="unsupported register action: ACTIVATE"):
        workspace_cli._run_registration(
            workspace_cli._RegisterRequest(
                action=IdentityAction.ACTIVATE,
                repo_uuid=REPO_UUID,
                expected_registry_revision=0,
            ),
            inputs=None,
            output=stdout,
            errors=stderr,
        )

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    "error, state, exit_code",
    [
        (
            RevisionConflict(
                "private current revision",
                actual_registry_revision=7,
            ),
            "conflict",
            10,
        ),
        (RevisionConflict("private unavailable revision"), "conflict", 10),
        (UUIDCollisionError("private source path"), "conflict", 10),
        (AuthorizationError("private authorization reason"), "invalid", 20),
        (TypeError("private runtime failure"), "invalid", 20),
    ],
)
def test_register_failures_are_canonical_opaque_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    state: str,
    exit_code: int,
) -> None:
    workspace_cli = _cli()
    cwd = tmp_path / "private-checkout"
    cwd.mkdir()
    source = _source_stub(cwd)

    class Registry:
        state = _AllowingState()

        def enroll(self, *_args: object, **_kwargs: object) -> None:
            raise error

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: source,
        raising=False,
    )
    monkeypatch.setattr(workspace_cli, "Path", SimpleNamespace(cwd=lambda: cwd), raising=False)
    monkeypatch.setattr(
        workspace_cli,
        "verify_source_checkout",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO(_authorization_payload("ENROLL")))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            "enroll",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stderr.getvalue())
    assert result == exit_code
    assert stdout.getvalue() == ""
    assert payload["contract"] == "graphify.workspace.registration"
    assert payload["schema_version"] == payload["cli_contract_version"] == 1
    assert payload["state"] == state
    assert payload["exit_code"] == exit_code
    assert payload["action"] == "enroll"
    assert payload["reason_code"] and payload["action_code"]
    if isinstance(error, RevisionConflict):
        if error.actual_registry_revision is None:
            assert payload["action_code"] == "run_workspace_doctor"
            assert "registry_revision" not in payload
        else:
            assert payload["action_code"] == "refresh_registry_revision"
            assert payload["registry_revision"] == 7
    else:
        assert "registry_revision" not in payload
    assert "private" not in stderr.getvalue()
    assert str(cwd) not in stderr.getvalue()


@pytest.mark.parametrize(
    "document_error",
    [KeyError("revision"), TypeError("private malformed registry document")],
)
def test_register_normalizes_unexpected_registry_result_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    document_error: Exception,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "private-checkout", REPO_UUID)
    state_root = tmp_path / "external-state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)

    class Document:
        def to_dict(self) -> dict[str, object]:
            raise document_error

    class Registry:
        state = store.state

        def enroll(
            self,
            source: SourceIdentity,
            authorization: OperatorAuthorization,
            *,
            expected_revision: int,
        ) -> Document:
            store.enroll(
                source,
                authorization,
                expected_revision=expected_revision,
            )
            return Document()

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )
    result, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=_inputs(state_root),
    )

    assert result == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "commit_unknown"
    assert "private" not in stderr.getvalue()
    assert str(checkout) not in stderr.getvalue()
    assert store.load().to_dict()["revision"] == 1


def test_register_preserves_injected_fault_from_revision_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    state_root = tmp_path / "external-state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)

    class Document:
        def to_dict(self) -> dict[str, object]:
            raise InjectedFault("revision-receipt")

    class Registry:
        state = store.state

        def enroll(
            self,
            source: SourceIdentity,
            authorization: OperatorAuthorization,
            *,
            expected_revision: int,
        ) -> Document:
            store.enroll(
                source,
                authorization,
                expected_revision=expected_revision,
            )
            return Document()

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )

    with pytest.raises(InjectedFault, match="revision-receipt"):
        _run_register(
            monkeypatch,
            cwd=checkout,
            action="enroll",
            expected_revision=0,
            inputs=_inputs(state_root),
        )

    assert store.load().to_dict()["revision"] == 1


def test_register_applies_a_deadline_to_source_discovery_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state_root = tmp_path / "external-state"
    observed_calls: list[tuple[int, int | None]] = []

    def time_out(
        _root: Path,
        *,
        deadline_ns: int,
        max_bytes: int | None,
    ) -> None:
        observed_calls.append((deadline_ns, max_bytes))
        raise SourceDiscoveryTimeout("private source command timed out")

    monkeypatch.setattr(workspace_cli, "discover_source", time_out, raising=False)
    monkeypatch.setattr(workspace_cli.time, "monotonic_ns", lambda: 456)

    result, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=_inputs(state_root),
    )

    assert result == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "source_discovery_timeout"
    assert observed_calls == [(5_000_000_456, 64 * 1024)]
    assert not state_root.exists()


def test_register_revalidates_source_head_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    raced = create_repo(tmp_path / "raced", REPO_UUID)
    (raced / "README.md").write_text("raced registration source\n", encoding="utf-8")
    _git(raced, "add", "README.md")
    _git(raced, "commit", "--quiet", "--amend", "--no-edit")
    raced_head = _git(raced, "rev-parse", "HEAD")
    assert raced_head != _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "fetch", "--quiet", str(raced), raced_head)
    inputs = _inputs(tmp_path / "external-state")
    discover = workspace_cli.discover_source

    def switch_head_after_discovery(
        root: Path,
        *,
        deadline_ns: int,
        max_bytes: int | None,
    ) -> Any:
        source = discover(root, deadline_ns=deadline_ns, max_bytes=max_bytes)
        _git(checkout, "update-ref", "HEAD", raced_head)
        return source

    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        switch_head_after_discovery,
        raising=False,
    )

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "source_discovery_error"
    assert not inputs.state_root.exists()


@pytest.mark.parametrize("changed_fact", ["config", "remote"])
def test_register_resnapshots_all_source_evidence_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed_fact: str,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    discover = workspace_cli.discover_source
    discovery_count = 0

    def mutate_after_first_discovery(
        root: Path,
        *,
        deadline_ns: int,
        max_bytes: int | None,
    ) -> Any:
        nonlocal discovery_count
        source = discover(root, deadline_ns=deadline_ns, max_bytes=max_bytes)
        discovery_count += 1
        if discovery_count == 1:
            if changed_fact == "config":
                config = checkout / ".graphify/workspace.toml"
                config.write_bytes(config.read_bytes() + b"\n# raced registration\n")
            else:
                _git(
                    checkout,
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/example/raced-registration.git",
                )
        return source

    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        mutate_after_first_discovery,
        raising=False,
    )

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "source_discovery_error"
    assert discovery_count == 2
    assert not inputs.state_root.exists()


def test_register_rejects_noncanonical_authorization_before_external_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    authorization = json.dumps(
        {
            "action": "ENROLL",
            "issued_at": "2026-07-16T15:00:00Z",
            "nonce": "\ud800",
            "operator_id": "operator:registration-test",
            "reason": "operator authorization that must remain private",
        }
    )
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "source_root_identity",
        lambda *_args, **_kwargs: pytest.fail(
            "noncanonical authorization must not inspect the source"
        ),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda *_args, **_kwargs: pytest.fail(
            "noncanonical authorization must not discover the source"
        ),
        raising=False,
    )

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
        authorization=authorization,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    payload = _registration_payload(stderr)
    assert payload["reason_code"] == "authorization_invalid"
    assert payload["action_code"] == "provide_valid_authorization"
    assert not inputs.state_root.exists()


def test_register_rejects_registry_incompatible_remote_before_external_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    _git(
        checkout,
        "remote",
        "set-url",
        "origin",
        "https://github.com/example/repo%2Fname.git",
    )
    inputs = _inputs(tmp_path / "external-state")

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "source_discovery_error"
    assert not inputs.state_root.exists()


def test_register_rejects_noncanonical_filesystem_identity_before_external_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "Caf\u00e9", REPO_UUID)
    discovered = workspace_cli.discover_source(checkout)
    registry_source = dict(discovered.registry_source)
    registry_source["path"] = str(tmp_path / "Cafe\u0301")
    assert registry_source["path"] != str(checkout)
    noncanonical = replace(
        discovered,
        registry_source=registry_source,
        source_sha256=canonical_sha256(registry_source),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: noncanonical,
        raising=False,
    )
    inputs = _inputs(tmp_path / "external-state")

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "source_discovery_error"
    assert not inputs.state_root.exists()


@pytest.mark.parametrize("inconsistency", ["source_hash", "remote_evidence"])
def test_register_rejects_inconsistent_source_evidence_before_external_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inconsistency: str,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    discovered = workspace_cli.discover_source(checkout)
    if inconsistency == "source_hash":
        inconsistent = replace(discovered, source_sha256="f" * 64)
    else:
        inconsistent = replace(
            discovered,
            remote_evidence=(
                *discovered.remote_evidence,
                {
                    "kind": "graphify.workspace.remote_evidence",
                    "remote_name": "private-origin",
                    "url": "https://github.com/private/inconsistent.git",
                },
            ),
        )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: inconsistent,
        raising=False,
    )
    inputs = _inputs(tmp_path / "external-state")

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "source_discovery_error"
    assert not inputs.state_root.exists()


@pytest.mark.parametrize(
    "action",
    [IdentityAction.ENROLL, IdentityAction.REBIND, IdentityAction.ROTATE],
)
def test_register_authorization_input_is_bounded_in_bytes_before_decode(
    monkeypatch: pytest.MonkeyPatch,
    action: IdentityAction,
) -> None:
    workspace_cli = _cli()
    read_sizes: list[int] = []

    class RecordingBuffer(BytesIO):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(size)

    binary_input = RecordingBuffer("é".encode("utf-8") * (8 * 1024 + 1))
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            buffer=binary_input,
            read=lambda _size=-1: pytest.fail("authorization must be byte-bounded"),
        ),
    )
    request = workspace_cli._RegisterRequest(
        action=action,
        repo_uuid=REPO_UUID,
        expected_registry_revision=0,
    )

    with pytest.raises(AuthorizationError, match="exceeds the byte limit"):
        workspace_cli._read_operator_authorization(request)

    assert read_sizes == [16 * 1024 + 1]


def test_register_classifies_json_parser_depth_failure_as_invalid_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    authorization = "[" * 1_100 + "0" + "]" * 1_100
    assert len(authorization.encode("utf-8")) < 16 * 1024

    def reject_parser_depth(
        raw: str,
        *,
        object_pairs_hook: Any,
    ) -> Any:
        del object_pairs_hook
        assert raw == authorization
        raise RecursionError("private parser depth")

    monkeypatch.setattr(
        workspace_cli,
        "json",
        SimpleNamespace(loads=reject_parser_depth),
        raising=False,
    )

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
        authorization=authorization,
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    payload = _registration_payload(stderr)
    assert payload["reason_code"] == "authorization_invalid"
    assert payload["action_code"] == "provide_valid_authorization"
    assert not inputs.state_root.exists()


@pytest.mark.parametrize(
    "authorization",
    [
        "{not-json",
        json.dumps({"action": "ENROLL"}),
        json.dumps(
            {
                "action": "ADOPT",
                "issued_at": "2026-07-16T15:00:00Z",
                "nonce": "n",
                "operator_id": "op",
                "reason": "r",
                "extra": True,
            }
        ),
        "{"
        + '"action":"ENROLL","issued_at":"2026-07-16T15:00:00Z","nonce":"n","operator_id":"op","reason":"'
        + ("x" * (16 * 1024))
        + '"}',
    ],
)
def test_register_rejects_malformed_or_mismatched_stdin_authority_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    authorization: str,
) -> None:
    workspace_cli = _cli()
    runtime_calls: list[str] = []
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root: pytest.fail("invalid authority must not discover source"),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: runtime_calls.append("load") or object(),
    )
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: runtime_calls.append("compose")
        or SimpleNamespace(registry=object()),
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO(authorization))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            "enroll",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stderr.getvalue())
    assert result == 20
    assert stdout.getvalue() == ""
    assert payload["state"] == "invalid"
    assert payload["exit_code"] == 20
    assert runtime_calls == ["load", "compose"]


def test_register_real_composition_rejects_malformed_authorization_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    before_checkout = tree_snapshot(checkout)
    before_checkout_metadata = metadata_snapshot(checkout)

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
        authorization="{not-json",
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "authorization_invalid"
    assert not inputs.state_root.exists()
    assert tree_snapshot(checkout) == before_checkout
    assert metadata_snapshot(checkout) == before_checkout_metadata


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (UnsupportedRuntime("private unsupported runtime"), "unsupported_runtime"),
        (
            UnsupportedCompatibility("private unsupported compatibility"),
            "unsupported_compatibility",
        ),
    ],
)
def test_register_composition_failures_do_not_read_authorization_stdin(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason_code: str,
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: (_ for _ in ()).throw(error),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root: pytest.fail("invalid runtime must not discover source"),
        raising=False,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(read=lambda _size=-1: pytest.fail("invalid runtime must not read stdin")),
    )
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            "enroll",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "0",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == reason_code


def test_register_real_enroll_then_clone_adopt_preserves_active_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = create_repo(tmp_path / "source", REPO_UUID)
    clone = clone_repo(
        source,
        tmp_path / "clone",
        remote_url=REGISTRATION_REMOTE,
    )
    inputs = _inputs(tmp_path / "external-state")

    first_exit, first_stdout, first_stderr = _run_register(
        monkeypatch, cwd=source, action="enroll", expected_revision=0, inputs=inputs
    )
    second_exit, second_stdout, second_stderr = _run_register(
        monkeypatch, cwd=clone, action="adopt", expected_revision=1, inputs=inputs
    )

    assert first_exit == second_exit == 0
    assert first_stderr.getvalue() == second_stderr.getvalue() == ""
    assert _registration_payload(first_stdout)["registry_revision"] == 1
    assert _registration_payload(second_stdout)["registry_revision"] == 2
    entry = (
        RegistryStore(inputs.state_root, capabilities=SUPPORTED).load().to_dict()["workspaces"][0]
    )
    assert entry["active_source"]["path"] == str(source.resolve())
    assert {alias["path"] for alias in entry["aliases"]} == {str(clone.resolve())}


def test_register_duplicate_and_stale_requests_do_not_change_external_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = create_repo(tmp_path / "source", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    assert (
        _run_register(monkeypatch, cwd=source, action="enroll", expected_revision=0, inputs=inputs)[
            0
        ]
        == 0
    )
    before_tree = tree_snapshot(inputs.state_root)
    before_metadata = metadata_snapshot(inputs.state_root)

    duplicate_exit, _duplicate_stdout, duplicate_stderr = _run_register(
        monkeypatch, cwd=source, action="enroll", expected_revision=1, inputs=inputs
    )
    repeated_exit, _repeated_stdout, repeated_stderr = _run_register(
        monkeypatch, cwd=source, action="enroll", expected_revision=1, inputs=inputs
    )
    stale_exit, _stale_stdout, stale_stderr = _run_register(
        monkeypatch, cwd=source, action="enroll", expected_revision=0, inputs=inputs
    )

    assert duplicate_exit == repeated_exit == stale_exit == 10
    assert repeated_stderr.getvalue() == duplicate_stderr.getvalue()
    assert _registration_payload(duplicate_stderr)["reason_code"] == "uuid_collision"
    stale_payload = _registration_payload(stale_stderr)
    assert stale_payload["reason_code"] == "revision_conflict"
    assert stale_payload["action_code"] == "refresh_registry_revision"
    assert stale_payload["registry_revision"] == 1
    assert tree_snapshot(inputs.state_root) == before_tree
    assert set(metadata_snapshot(inputs.state_root)) == set(before_metadata)
    assert (
        RegistryStore(inputs.state_root, capabilities=SUPPORTED).load().to_dict()["revision"] == 1
    )


def test_register_unrelated_same_uuid_adoption_is_rejected_without_external_diff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    enrolled = create_repo(tmp_path / "enrolled", REPO_UUID)
    unrelated = create_repo(tmp_path / "unrelated", REPO_UUID)
    (unrelated / "README.md").write_text("unrelated registration history\n", encoding="utf-8")
    _git(unrelated, "add", "README.md")
    _git(unrelated, "commit", "--quiet", "--amend", "--no-edit")
    inputs = _inputs(tmp_path / "external-state")
    assert (
        _run_register(
            monkeypatch, cwd=enrolled, action="enroll", expected_revision=0, inputs=inputs
        )[0]
        == 0
    )
    before_tree = tree_snapshot(inputs.state_root)
    before_metadata = metadata_snapshot(inputs.state_root)

    exit_code, stdout, stderr = _run_register(
        monkeypatch, cwd=unrelated, action="adopt", expected_revision=1, inputs=inputs
    )

    assert exit_code == 10
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "uuid_collision"
    assert tree_snapshot(inputs.state_root) == before_tree
    assert set(metadata_snapshot(inputs.state_root)) == set(before_metadata)


@pytest.mark.parametrize(
    ("authority", "reason_code", "action_code"),
    [
        (None, "runtime_authority_missing", "install_candidate_authority"),
        (b"{not-json", "runtime_authority_invalid", "install_candidate_authority"),
        (
            b'{"compatibility_manifest":{},"contract":"graphify.workspace.runtime_authority.internal","format_version":2,"semantic_queue_policy":{}}\n',
            "runtime_authority_unsupported",
            "install_supported_candidate",
        ),
    ],
)
def test_register_loader_authority_failures_are_redacted_and_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authority: bytes | None,
    reason_code: str,
    action_code: str,
) -> None:
    checkout = create_repo(tmp_path / "private-checkout", REPO_UUID)
    state_home = tmp_path / "private-state-home"
    state_root = state_home / "graphify"
    if authority is not None:
        state_root.mkdir(parents=True, mode=0o700)
        authority_path = state_root / RUNTIME_AUTHORITY_FILENAME
        authority_path.write_bytes(authority)
        authority_path.chmod(0o600)
    before_checkout = tree_snapshot(checkout)
    before_home = tree_snapshot(state_home)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HOME", str(tmp_path / "private-home"))

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=None,
        stdin=SimpleNamespace(
            read=lambda _size=-1: pytest.fail("invalid authority must not read stdin")
        ),
    )

    payload = _registration_payload(stderr)
    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert (payload["reason_code"], payload["action_code"]) == (reason_code, action_code)
    assert str(checkout) not in stderr.getvalue()
    assert str(state_home) not in stderr.getvalue()
    assert tree_snapshot(checkout) == before_checkout
    assert tree_snapshot(state_home) == before_home


def test_register_rejects_oversized_workspace_policy_before_external_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "private-checkout", REPO_UUID)
    config = checkout / ".graphify/workspace.toml"
    config.write_bytes(config.read_bytes() + b"\n#" + b"x" * (64 * 1024))
    before_checkout = tree_snapshot(checkout)
    inputs = _inputs(tmp_path / "external-state")

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    payload = _registration_payload(stderr)
    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "source_discovery_error"
    assert str(checkout) not in stderr.getvalue()
    assert not inputs.state_root.exists()
    assert tree_snapshot(checkout) == before_checkout


def test_register_requires_cwd_to_be_the_git_top_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "private-checkout", REPO_UUID)
    nested = checkout / "nested"
    nested.mkdir()
    before_checkout = tree_snapshot(checkout)
    inputs = _inputs(tmp_path / "external-state")

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=nested,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )

    payload = _registration_payload(stderr)
    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "source_discovery_error"
    assert str(nested) not in stderr.getvalue()
    assert not inputs.state_root.exists()
    assert tree_snapshot(checkout) == before_checkout


def test_register_corrupt_registry_and_unsafe_state_path_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    corrupt_inputs = _inputs(tmp_path / "corrupt-state")
    corrupt_inputs.state_root.mkdir(mode=0o700)
    corrupt_registry = corrupt_inputs.state_root / "registry.json"
    corrupt_registry.write_text("{corrupt", encoding="utf-8")
    corrupt_registry.chmod(0o600)
    corrupt_lock = corrupt_inputs.state_root / "registry.lock"
    corrupt_lock.touch(mode=0o600)
    before_corrupt = tree_snapshot(corrupt_inputs.state_root)
    before_corrupt_metadata = metadata_snapshot(corrupt_inputs.state_root)
    corrupt_exit, corrupt_stdout, corrupt_stderr = _run_register(
        monkeypatch, cwd=checkout, action="enroll", expected_revision=0, inputs=corrupt_inputs
    )
    assert corrupt_exit == 20
    assert corrupt_stdout.getvalue() == ""
    assert _registration_payload(corrupt_stderr)["reason_code"] == "state_corrupt"
    assert tree_snapshot(corrupt_inputs.state_root) == before_corrupt
    after_corrupt_metadata = metadata_snapshot(corrupt_inputs.state_root)
    assert set(after_corrupt_metadata) == set(before_corrupt_metadata)
    assert after_corrupt_metadata["registry.json"] == before_corrupt_metadata["registry.json"]

    external = tmp_path / "private-external-state"
    external.mkdir(mode=0o700)
    linked = tmp_path / "linked-state"
    linked.symlink_to(external, target_is_directory=True)
    before_external = tree_snapshot(external)
    unsafe_exit, unsafe_stdout, unsafe_stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=_inputs(linked),
    )
    assert unsafe_exit == 20
    assert unsafe_stdout.getvalue() == ""
    assert _registration_payload(unsafe_stderr)["reason_code"] == "unsafe_state_path"
    assert tree_snapshot(external) == before_external


def test_register_rejects_state_root_inside_linked_worktree_git_common_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = create_repo(tmp_path / "primary", REPO_UUID)
    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    common_dir = Path(_git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    state_root = common_dir / "graphify-state"
    before_common = tree_snapshot(common_dir)
    before_common_metadata = metadata_snapshot(common_dir)
    before_linked = tree_snapshot(linked)
    before_linked_metadata = metadata_snapshot(linked)

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=linked,
        action="enroll",
        expected_revision=0,
        inputs=_inputs(state_root),
    )

    assert exit_code == 20
    assert stdout.getvalue() == ""
    assert _registration_payload(stderr)["reason_code"] == "unsafe_state_path"
    assert not state_root.exists()
    assert tree_snapshot(common_dir) == before_common
    assert metadata_snapshot(common_dir) == before_common_metadata
    assert tree_snapshot(linked) == before_linked
    assert metadata_snapshot(linked) == before_linked_metadata


def test_registration_subprocess_uses_cwd_stdin_and_production_authority(
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    clone = clone_repo(
        checkout,
        tmp_path / "clone",
        remote_url=REGISTRATION_REMOTE,
    )
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    state_home = tmp_path / "state-home"
    state_root = state_home / "graphify"
    home.mkdir()
    codex_home.mkdir()
    stale_skill = home / ".codex/skills/graphify/SKILL.md"
    stale_skill.parent.mkdir(parents=True)
    stale_skill.write_text("stale test skill\n", encoding="utf-8")
    (stale_skill.parent / ".graphify_version").write_text("0.0.0\n", encoding="utf-8")
    state_root.mkdir(parents=True, mode=0o700)
    authority_path = state_root / RUNTIME_AUTHORITY_FILENAME
    authority_path.write_bytes(
        WorkspaceRuntimeAuthority(
            compatibility_manifest=COMPATIBILITY_MANIFEST,
            semantic_queue_policy=POLICY,
        ).canonical
    )
    authority_path.chmod(0o600)
    protected_roots = (checkout, checkout / ".git", clone, clone / ".git", home, codex_home)
    snapshots = {root: tree_snapshot(root) for root in protected_roots}
    metadata_snapshots = {root: metadata_snapshot(root) for root in protected_roots}
    environment = dict(os.environ)
    for name in (
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MOONSHOT_API_KEY",
        "OPENAI_API_KEY",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "HOME": str(home),
            "PYTHONPATH": str(Path(__file__).parents[1]),
            "XDG_STATE_HOME": str(state_home),
        }
    )

    def invoke(cwd: Path, action: str, expected_revision: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _SUBPROCESS_LAUNCHER,
                "workspace",
                "register",
                action,
                "--repo-uuid",
                REPO_UUID,
                "--expected-registry-revision",
                str(expected_revision),
                "--authorization-stdin",
            ],
            cwd=cwd,
            env=environment,
            input=_authorization_payload(action.upper()),
            check=False,
            capture_output=True,
            text=True,
        )

    enrolled = invoke(checkout, "enroll", 0)
    adopted = invoke(clone, "adopt", 1)
    usage = subprocess.run(
        [
            sys.executable,
            "-c",
            _SUBPROCESS_LAUNCHER,
            "workspace",
            "register",
            "enroll",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "2",
            "--authorization-stdin",
            "--unexpected",
        ],
        cwd=checkout,
        env=environment,
        input="must-not-be-read",
        check=False,
        capture_output=True,
        text=True,
    )

    assert enrolled.returncode == adopted.returncode == 0
    assert enrolled.stderr == adopted.stderr == ""
    assert usage.returncode == 64
    assert usage.stdout == ""
    assert usage.stderr == WORKSPACE_USAGE
    assert json.loads(enrolled.stdout)["registry_revision"] == 1
    assert json.loads(adopted.stdout)["registry_revision"] == 2
    document = RegistryStore(state_root, capabilities=SUPPORTED).load().to_dict()
    assert document["revision"] == 2
    assert document["workspaces"][0]["active_source"]["path"] == str(checkout.resolve())
    assert {alias["path"] for alias in document["workspaces"][0]["aliases"]} == {
        str(clone.resolve())
    }
    _assert_external_state_allowlist(state_root, includes_authority=True)
    for root, snapshot in snapshots.items():
        assert tree_snapshot(root) == snapshot
        assert metadata_snapshot(root) == metadata_snapshots[root]


def test_register_limits_writes_to_private_external_state_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    clone = clone_repo(
        checkout,
        tmp_path / "clone",
        remote_url=REGISTRATION_REMOTE,
    )
    state_root = tmp_path / "external-state"
    home = tmp_path / "private-home"
    codex_home = tmp_path / "private-codex-home"
    home.mkdir()
    codex_home.mkdir()
    inputs = _inputs(state_root)
    protected_roots = (
        checkout,
        checkout / ".git",
        clone,
        clone / ".git",
        home,
        codex_home,
    )
    snapshots = {root: tree_snapshot(root) for root in protected_roots}
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert (
        _run_register(
            monkeypatch, cwd=checkout, action="enroll", expected_revision=0, inputs=inputs
        )[0]
        == 0
    )
    assert (
        _run_register(monkeypatch, cwd=clone, action="adopt", expected_revision=1, inputs=inputs)[0]
        == 0
    )

    _assert_external_state_allowlist(state_root, includes_authority=False)
    for root, snapshot in snapshots.items():
        assert tree_snapshot(root) == snapshot


def test_register_partial_write_recovery_has_no_duplicate_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    state_root = tmp_path / "external-state"
    fired = False

    def crash_once(event: str) -> None:
        nonlocal fired
        if event == "registry:current_replaced" and not fired:
            fired = True
            raise InjectedFault(event)

    first_exit, _first_stdout, first_stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="enroll",
        expected_revision=0,
        inputs=_inputs(state_root, fault_hook=crash_once),
    )
    second_exit, second_stdout, second_stderr = _run_register(
        monkeypatch, cwd=checkout, action="enroll", expected_revision=0, inputs=_inputs(state_root)
    )

    assert fired
    assert first_exit == 20
    assert _registration_payload(first_stderr)["reason_code"] in {
        "commit_unknown",
        "registration_failed",
    }
    assert second_exit in {0, 10}
    if second_exit == 0:
        assert second_stderr.getvalue() == ""
        assert _registration_payload(second_stdout)["registry_revision"] == 1
    else:
        assert second_stdout.getvalue() == ""
        assert _registration_payload(second_stderr)["reason_code"] in {
            "revision_conflict",
            "uuid_collision",
        }
    assert RegistryStore(state_root, capabilities=SUPPORTED).load().to_dict()["revision"] == 1


def test_registration_cli_cas_contention_emits_one_success_and_one_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    workspace_cli = _cli()
    authorization = OperatorAuthorization(
        action=IdentityAction.ENROLL,
        operator_id="operator:contention-test",
        reason="deterministic registration contention",
        issued_at="2026-07-16T15:00:00Z",
        nonce="contention",
    )
    monkeypatch.setattr(
        workspace_cli,
        "_read_operator_authorization",
        lambda _request: authorization,
    )
    monkeypatch.chdir(checkout)
    barrier = Barrier(2)

    def enroll() -> tuple[int, StringIO, StringIO]:
        stdout = StringIO()
        stderr = StringIO()
        barrier.wait(timeout=5)
        exit_code = workspace_cli.run_workspace_command(
            [
                "register",
                "enroll",
                "--repo-uuid",
                REPO_UUID,
                "--expected-registry-revision",
                "0",
                "--authorization-stdin",
            ],
            inputs=inputs,
            stdout=stdout,
            stderr=stderr,
        )
        return exit_code, stdout, stderr

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result() for future in (executor.submit(enroll), executor.submit(enroll))
        ]

    assert sorted(outcome[0] for outcome in outcomes) == [0, 10]
    success = next(outcome for outcome in outcomes if outcome[0] == 0)
    conflict = next(outcome for outcome in outcomes if outcome[0] == 10)
    assert success[2].getvalue() == ""
    assert _registration_payload(success[1])["registry_revision"] == 1
    assert conflict[1].getvalue() == ""
    conflict_payload = _registration_payload(conflict[2])
    assert conflict_payload["reason_code"] == "revision_conflict"
    assert conflict_payload["registry_revision"] == 1
    document = RegistryStore(inputs.state_root, capabilities=SUPPORTED).load().to_dict()
    assert document["revision"] == 1
    assert len(document["workspaces"]) == 1


@pytest.mark.parametrize(
    ("action", "method"),
    [
        ("rebind", "rebind"),
        ("rotate", "rotate_enrollment_evidence"),
    ],
)
def test_identity_maintenance_emits_a_dedicated_canonical_success_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    method: str,
) -> None:
    workspace_cli = _cli()
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    source = _source_stub(cwd)
    calls: list[tuple[str, object, OperatorAuthorization, int]] = []

    class Registry:
        state = _AllowingState()

        def rebind(
            self,
            discovered: object,
            authorization: OperatorAuthorization,
            *,
            expected_revision: int,
        ) -> Any:
            calls.append(("rebind", discovered, authorization, expected_revision))
            return SimpleNamespace(to_dict=lambda: {"revision": 4})

        def rotate_enrollment_evidence(
            self,
            discovered: object,
            authorization: OperatorAuthorization,
            *,
            expected_revision: int,
        ) -> Any:
            calls.append(("rotate_enrollment_evidence", discovered, authorization, expected_revision))
            return SimpleNamespace(to_dict=lambda: {"revision": 4})

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: source,
        raising=False,
    )
    monkeypatch.setattr(workspace_cli, "Path", SimpleNamespace(cwd=lambda: cwd), raising=False)
    monkeypatch.setattr(
        workspace_cli,
        "verify_source_checkout",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO(_authorization_payload(action.upper())))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            action,
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "3",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    expected = {
        "action": action,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.identity_maintenance",
        "exit_code": 0,
        "registry_revision": 4,
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "state": "maintained",
    }
    assert result == 0
    assert calls == [(method, source, calls[0][2], 3)]
    assert calls[0][2].to_dict() == json.loads(_authorization_payload(action.upper()))
    assert stdout.getvalue().encode("utf-8") == (
        json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    assert stderr.getvalue() == ""
    payload = _identity_maintenance_payload(stdout)
    assert payload == expected
    assert list(
        Draft202012Validator(
            workspace_cli.load_registration_schema(),
            format_checker=FormatChecker(),
        ).iter_errors(payload)
    )
    assert "operator authorization" not in stdout.getvalue()


@pytest.mark.parametrize(
    ("error", "state", "exit_code", "reason_code", "action_code", "revision"),
    [
        (
            RevisionConflict("private current revision", actual_registry_revision=7),
            "conflict",
            10,
            "revision_conflict",
            "refresh_registry_revision",
            7,
        ),
        (
            RevisionConflict("private unavailable revision"),
            "conflict",
            10,
            "revision_conflict",
            "run_workspace_doctor",
            None,
        ),
        (
            UUIDCollisionError("private unrelated identity"),
            "conflict",
            10,
            "identity_mismatch",
            "verify_identity_maintenance_target",
            None,
        ),
        (
            SourceAmbiguousError("private unbound source"),
            "conflict",
            10,
            "source_not_bound",
            "enroll_or_adopt_source",
            None,
        ),
        (
            AuthorizationError("private authorization"),
            "invalid",
            20,
            "authorization_invalid",
            "provide_valid_authorization",
            None,
        ),
        (
            TypeError("private runtime failure"),
            "invalid",
            20,
            "identity_maintenance_failed",
            "run_workspace_doctor",
            None,
        ),
    ],
)
def test_identity_maintenance_failures_are_canonical_opaque_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    state: str,
    exit_code: int,
    reason_code: str,
    action_code: str,
    revision: int | None,
) -> None:
    workspace_cli = _cli()
    cwd = tmp_path / "private-checkout"
    cwd.mkdir()
    source = _source_stub(cwd)

    class Registry:
        state = _AllowingState()

        def rebind(self, *_args: object, **_kwargs: object) -> None:
            raise error

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: source,
        raising=False,
    )
    monkeypatch.setattr(workspace_cli, "Path", SimpleNamespace(cwd=lambda: cwd), raising=False)
    monkeypatch.setattr(
        workspace_cli,
        "verify_source_checkout",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO(_authorization_payload("REBIND")))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            "rebind",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "6",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    payload = _identity_maintenance_payload(stderr)
    assert result == exit_code
    assert stdout.getvalue() == ""
    assert payload["state"] == state
    assert payload["exit_code"] == exit_code
    assert payload["reason_code"] == reason_code
    assert payload["action_code"] == action_code
    if revision is None:
        assert "registry_revision" not in payload
    else:
        assert payload["registry_revision"] == revision
    assert "repo_uuid" not in payload
    assert "private" not in stderr.getvalue()
    assert str(cwd) not in stderr.getvalue()


@pytest.mark.parametrize("action", ["rebind", "rotate"])
def test_identity_maintenance_composes_authority_before_reading_authorization_stdin(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    workspace_cli = _cli()
    calls: list[str] = []
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: calls.append("load") or object(),
    )
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: calls.append("compose")
        or (_ for _ in ()).throw(UnsupportedRuntime("private runtime")),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda *_args, **_kwargs: pytest.fail("invalid runtime must not discover source"),
        raising=False,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(read=lambda _size=-1: pytest.fail("invalid runtime must not read stdin")),
    )
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            action,
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "1",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 20
    assert stdout.getvalue() == ""
    assert _identity_maintenance_payload(stderr)["reason_code"] == "unsupported_runtime"
    assert calls == ["load", "compose"]


@pytest.mark.parametrize(
    ("action", "authorization"),
    [
        ("rebind", _authorization_payload("ROTATE")),
        ("rotate", _authorization_payload("REBIND")),
        (
            "rebind",
            '{"action":"REBIND","action":"REBIND","issued_at":"2026-07-16T15:00:00Z",'
            '"nonce":"n","operator_id":"op","reason":"r"}',
        ),
        (
            "rotate",
            json.dumps(
                {
                    "action": "ROTATE",
                    "issued_at": "2026-07-16T15:00:00Z",
                    "nonce": "\ud800",
                    "operator_id": "op",
                    "reason": "r",
                }
            ),
        ),
    ],
)
def test_identity_maintenance_authorization_is_duplicate_safe_canonical_and_action_exact(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    authorization: str,
) -> None:
    workspace_cli = _cli()
    calls: list[str] = []
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: calls.append("load") or object(),
    )
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: calls.append("compose") or SimpleNamespace(registry=object()),
        raising=False,
    )
    monkeypatch.setattr(
        workspace_cli,
        "source_root_identity",
        lambda *_args, **_kwargs: pytest.fail("invalid authorization must not inspect source"),
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", StringIO(authorization))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        [
            "register",
            action,
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "1",
            "--authorization-stdin",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    payload = _identity_maintenance_payload(stderr)
    assert result == 20
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "authorization_invalid"
    assert payload["action_code"] == "provide_valid_authorization"
    assert calls == ["load", "compose"]
    assert "operator authorization" not in stderr.getvalue()


def _active_source_state(entry: dict[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            {
                "active_source": entry["active_source"],
                "active_source_evidence": entry["active_source_evidence"],
                "active_source_revision": entry["active_source_revision"],
            }
        )
    )


def test_identity_maintenance_rebind_and_rotate_obey_registry_policy_without_source_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = create_repo(tmp_path / "original", REPO_UUID)
    clone = clone_repo(
        original,
        tmp_path / "clone",
        remote_url=REGISTRATION_REMOTE,
    )
    unrelated = create_repo(tmp_path / "unrelated", REPO_UUID)
    (unrelated / "README.md").write_text("unrelated identity history\n", encoding="utf-8")
    _git(unrelated, "add", "README.md")
    _git(unrelated, "commit", "--quiet", "--amend", "--no-edit")
    inputs = _inputs(tmp_path / "external-state")
    protected = (original, clone, unrelated)
    snapshots = {root: tree_snapshot(root) for root in protected}

    enrolled_exit, enrolled_stdout, enrolled_stderr = _run_register(
        monkeypatch,
        cwd=original,
        action="enroll",
        expected_revision=0,
        inputs=inputs,
    )
    assert enrolled_exit == 0
    assert enrolled_stderr.getvalue() == ""
    assert _registration_payload(enrolled_stdout)["registry_revision"] == 1
    store = RegistryStore(inputs.state_root, capabilities=SUPPORTED)
    enrolled_entry = store.load().to_dict()["workspaces"][0]
    active_before = _active_source_state(enrolled_entry)

    rebound_exit, rebound_stdout, rebound_stderr = _run_register(
        monkeypatch,
        cwd=clone,
        action="rebind",
        expected_revision=1,
        inputs=inputs,
    )
    assert rebound_exit == 0
    assert rebound_stderr.getvalue() == ""
    assert _identity_maintenance_payload(rebound_stdout)["registry_revision"] == 2
    rebound_entry = store.load().to_dict()["workspaces"][0]
    assert {alias["path"] for alias in rebound_entry["aliases"]} == {str(clone.resolve())}
    assert _active_source_state(rebound_entry) == active_before

    rotated_exit, rotated_stdout, rotated_stderr = _run_register(
        monkeypatch,
        cwd=clone,
        action="rotate",
        expected_revision=2,
        inputs=inputs,
    )
    assert rotated_exit == 0
    assert rotated_stderr.getvalue() == ""
    assert _identity_maintenance_payload(rotated_stdout)["registry_revision"] == 3
    rotated_entry = store.load().to_dict()["workspaces"][0]
    assert _active_source_state(rotated_entry) == active_before

    before_failed_state = tree_snapshot(inputs.state_root)
    rotate_exit, rotate_stdout, rotate_stderr = _run_register(
        monkeypatch,
        cwd=unrelated,
        action="rotate",
        expected_revision=3,
        inputs=inputs,
    )
    rebind_exit, rebind_stdout, rebind_stderr = _run_register(
        monkeypatch,
        cwd=unrelated,
        action="rebind",
        expected_revision=3,
        inputs=inputs,
    )

    assert rotate_exit == rebind_exit == 10
    assert rotate_stdout.getvalue() == rebind_stdout.getvalue() == ""
    assert _identity_maintenance_payload(rotate_stderr)["reason_code"] == "source_not_bound"
    assert _identity_maintenance_payload(rebind_stderr)["reason_code"] == "identity_mismatch"
    assert store.load().to_dict()["revision"] == 3
    assert tree_snapshot(inputs.state_root) == before_failed_state
    assert _active_source_state(store.load().to_dict()["workspaces"][0]) == active_before
    assert {root: tree_snapshot(root) for root in protected} == snapshots
    _assert_external_state_allowlist(inputs.state_root, includes_authority=False)


def test_identity_maintenance_rotate_rejects_bound_locator_replaced_by_unrelated_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    enrolled = create_repo(tmp_path / "enrolled", REPO_UUID)
    enrolled_source = discover_source(enrolled)
    inputs = _inputs(tmp_path / "external-state")
    assert (
        _run_register(
            monkeypatch,
            cwd=enrolled,
            action="enroll",
            expected_revision=0,
            inputs=inputs,
        )[0]
        == 0
    )

    replacement = create_repo(tmp_path / "replacement", REPO_UUID)
    workspace_config = (replacement / ".graphify/workspace.toml").read_text(
        encoding="utf-8"
    )
    _git(replacement, "checkout", "--quiet", "--orphan", "unrelated-root")
    _git(replacement, "rm", "--quiet", "-rf", ".")
    (replacement / ".graphify").mkdir()
    (replacement / ".graphify/workspace.toml").write_text(
        workspace_config,
        encoding="utf-8",
    )
    (replacement / "README.md").write_text("unrelated replacement\n", encoding="utf-8")
    _git(replacement, "add", ".")
    _git(replacement, "commit", "--quiet", "-m", "unrelated root")
    retired = tmp_path / "retired"
    enrolled.rename(retired)
    replacement.rename(enrolled)
    replacement_source = discover_source(enrolled)
    assert replacement_source.registry_source == enrolled_source.registry_source
    assert not set(replacement_source.history_roots).intersection(
        enrolled_source.history_roots
    )
    assert (
        replacement_source.git_common_device,
        replacement_source.git_common_inode,
    ) != (
        enrolled_source.git_common_device,
        enrolled_source.git_common_inode,
    )

    state_root = inputs.state_root
    registry_path = state_root / "registry.json"
    workspace_path = state_root / "workspaces" / REPO_UUID / "workspace.json"
    evidence_dir = state_root / "evidence"
    before_registry = registry_path.read_bytes()
    before_evidence = {
        path.name: path.read_bytes() for path in sorted(evidence_dir.glob("*.json"))
    }
    before_workspace = workspace_path.read_bytes()
    before_state = tree_snapshot(state_root)
    before_sources = {path: tree_snapshot(path) for path in (retired, enrolled)}

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=enrolled,
        action="rotate",
        expected_revision=1,
        inputs=inputs,
    )

    assert exit_code == 10
    assert stdout.getvalue() == ""
    payload = _identity_maintenance_payload(stderr)
    assert payload["reason_code"] == "source_not_bound"
    assert payload["action_code"] == "enroll_or_adopt_source"
    assert registry_path.read_bytes() == before_registry
    assert {
        path.name: path.read_bytes() for path in sorted(evidence_dir.glob("*.json"))
    } == before_evidence
    assert workspace_path.read_bytes() == before_workspace
    assert tree_snapshot(state_root) == before_state
    assert {path: tree_snapshot(path) for path in (retired, enrolled)} == before_sources


def test_identity_maintenance_rebind_rejects_a_source_bound_to_another_uuid_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = create_repo(tmp_path / "first", REPO_UUID)
    second = clone_repo(
        first,
        tmp_path / "second",
        remote_url=REGISTRATION_REMOTE,
    )
    second_config = second / ".graphify/workspace.toml"
    second_config.write_text(
        second_config.read_text(encoding="utf-8").replace(REPO_UUID, SECOND_UUID),
        encoding="utf-8",
    )
    _git(second, "config", "user.email", "workspace-test@example.com")
    _git(second, "config", "user.name", "Workspace Test")
    _git(second, "add", ".graphify/workspace.toml")
    _git(second, "commit", "--quiet", "-m", "use second workspace UUID")
    inputs = _inputs(tmp_path / "external-state")
    assert (
        _run_register(
            monkeypatch,
            cwd=first,
            action="enroll",
            expected_revision=0,
            inputs=inputs,
        )[0]
        == 0
    )
    assert (
        _run_register(
            monkeypatch,
            cwd=second,
            action="enroll",
            expected_revision=1,
            inputs=inputs,
            repo_uuid=SECOND_UUID,
        )[0]
        == 0
    )
    first_config = first / ".graphify/workspace.toml"
    first_config.write_text(
        first_config.read_text(encoding="utf-8").replace(REPO_UUID, SECOND_UUID),
        encoding="utf-8",
    )
    before_external_state = tree_snapshot(inputs.state_root)
    before_checkouts = {root: tree_snapshot(root) for root in (first, second)}

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=first,
        action="rebind",
        expected_revision=2,
        inputs=inputs,
        repo_uuid=SECOND_UUID,
    )

    payload = _identity_maintenance_payload(stderr)
    assert exit_code == 10
    assert stdout.getvalue() == ""
    assert payload["state"] == "conflict"
    assert payload["reason_code"] == "identity_mismatch"
    assert payload["action_code"] == "verify_identity_maintenance_target"
    assert RegistryStore(inputs.state_root, capabilities=SUPPORTED).load().to_dict()[
        "revision"
    ] == 2
    assert tree_snapshot(inputs.state_root) == before_external_state
    assert {root: tree_snapshot(root) for root in (first, second)} == before_checkouts


def test_identity_maintenance_rebind_accepts_the_enrolled_git_common_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    assert (
        _run_register(
            monkeypatch,
            cwd=checkout,
            action="enroll",
            expected_revision=0,
            inputs=inputs,
        )[0]
        == 0
    )
    store = RegistryStore(inputs.state_root, capabilities=SUPPORTED)
    active_before = _active_source_state(store.load().to_dict()["workspaces"][0])
    rewritten_head = _git(
        checkout,
        "commit-tree",
        _git(checkout, "write-tree"),
        "-m",
        "rewritten-root",
    )
    _git(checkout, "update-ref", "HEAD", rewritten_head)
    before_checkout = tree_snapshot(checkout)

    exit_code, stdout, stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="rebind",
        expected_revision=1,
        inputs=inputs,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert _identity_maintenance_payload(stdout)["registry_revision"] == 2
    entry = store.load().to_dict()["workspaces"][0]
    assert _active_source_state(entry) == active_before
    assert tree_snapshot(checkout) == before_checkout


def test_identity_maintenance_partial_write_recovery_has_no_duplicate_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    state_root = tmp_path / "external-state"
    assert (
        _run_register(
            monkeypatch,
            cwd=checkout,
            action="enroll",
            expected_revision=0,
            inputs=_inputs(state_root),
        )[0]
        == 0
    )
    fired = False

    def crash_once(event: str) -> None:
        nonlocal fired
        if event == "registry:current_replaced" and not fired:
            fired = True
            raise InjectedFault(event)

    first_exit, first_stdout, first_stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="rotate",
        expected_revision=1,
        inputs=_inputs(state_root, fault_hook=crash_once),
    )
    second_exit, second_stdout, second_stderr = _run_register(
        monkeypatch,
        cwd=checkout,
        action="rotate",
        expected_revision=1,
        inputs=_inputs(state_root),
    )

    assert fired
    assert first_exit == 20
    assert first_stdout.getvalue() == ""
    assert _identity_maintenance_payload(first_stderr)["reason_code"] in {
        "commit_unknown",
        "identity_maintenance_failed",
    }
    assert second_exit in {0, 10}
    if second_exit == 0:
        assert second_stderr.getvalue() == ""
        assert _identity_maintenance_payload(second_stdout)["registry_revision"] == 2
    else:
        assert second_stdout.getvalue() == ""
        payload = _identity_maintenance_payload(second_stderr)
        assert payload["reason_code"] == "revision_conflict"
        assert payload["registry_revision"] == 2
    assert RegistryStore(state_root, capabilities=SUPPORTED).load().to_dict()["revision"] == 2


def test_identity_maintenance_preserves_injected_fault_from_revision_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    state_root = tmp_path / "external-state"
    inputs = _inputs(state_root)
    assert (
        _run_register(
            monkeypatch,
            cwd=checkout,
            action="enroll",
            expected_revision=0,
            inputs=inputs,
        )[0]
        == 0
    )
    store = RegistryStore(state_root, capabilities=SUPPORTED)

    class Document:
        def to_dict(self) -> dict[str, object]:
            raise InjectedFault("identity-maintenance-revision-receipt")

    class Registry:
        state = store.state

        def rotate_enrollment_evidence(
            self,
            source: SourceIdentity,
            authorization: OperatorAuthorization,
            *,
            expected_revision: int,
        ) -> Document:
            store.rotate_enrollment_evidence(
                source,
                authorization,
                expected_revision=expected_revision,
            )
            return Document()

    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry()),
        raising=False,
    )

    with pytest.raises(InjectedFault, match="identity-maintenance-revision-receipt"):
        _run_register(
            monkeypatch,
            cwd=checkout,
            action="rotate",
            expected_revision=1,
            inputs=inputs,
        )

    assert store.load().to_dict()["revision"] == 2


def test_identity_maintenance_cli_cas_contention_emits_one_success_and_one_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = create_repo(tmp_path / "checkout", REPO_UUID)
    inputs = _inputs(tmp_path / "external-state")
    assert (
        _run_register(
            monkeypatch,
            cwd=checkout,
            action="enroll",
            expected_revision=0,
            inputs=inputs,
        )[0]
        == 0
    )
    workspace_cli = _cli()
    authorization = OperatorAuthorization(
        action=IdentityAction.ROTATE,
        operator_id="operator:contention-test",
        reason="deterministic identity maintenance contention",
        issued_at="2026-07-16T15:00:00Z",
        nonce="identical-rotate-contention",
    )
    monkeypatch.setattr(
        workspace_cli,
        "_read_operator_authorization",
        lambda _request: authorization,
    )
    monkeypatch.chdir(checkout)
    barrier = Barrier(2)

    def rotate() -> tuple[int, StringIO, StringIO]:
        stdout = StringIO()
        stderr = StringIO()
        barrier.wait(timeout=5)
        exit_code = workspace_cli.run_workspace_command(
            [
                "register",
                "rotate",
                "--repo-uuid",
                REPO_UUID,
                "--expected-registry-revision",
                "1",
                "--authorization-stdin",
            ],
            inputs=inputs,
            stdout=stdout,
            stderr=stderr,
        )
        return exit_code, stdout, stderr

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result() for future in (executor.submit(rotate), executor.submit(rotate))
        ]

    assert sorted(outcome[0] for outcome in outcomes) == [0, 10]
    success = next(outcome for outcome in outcomes if outcome[0] == 0)
    conflict = next(outcome for outcome in outcomes if outcome[0] == 10)
    assert success[2].getvalue() == ""
    assert _identity_maintenance_payload(success[1])["registry_revision"] == 2
    assert conflict[1].getvalue() == ""
    conflict_payload = _identity_maintenance_payload(conflict[2])
    assert conflict_payload["reason_code"] == "revision_conflict"
    assert conflict_payload["registry_revision"] == 2
    document = RegistryStore(inputs.state_root, capabilities=SUPPORTED).load().to_dict()
    assert document["revision"] == 2
