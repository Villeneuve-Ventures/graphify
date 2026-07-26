from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from graphify.workspace.adapters import UnsupportedCompatibility
from graphify.workspace.composition import WorkspaceAuthorityError, WorkspaceRuntimeInputs
from graphify.workspace.contracts import canonical_json_bytes, canonical_sha256
from graphify.workspace.identity import (
    AuthorizationError,
    IdentityError,
    IdentityAction,
    OperatorAuthorization,
    SourceAmbiguousError,
    SourceDiscoveryTimeout,
    UUIDCollisionError,
    discover_source,
)
from graphify.workspace.leases import LeaseBusy, LeaseRecoveryRequired
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    UnsupportedRuntime,
)
from graphify.workspace.registry import (
    RegistryStore,
    RevisionConflict,
    SourceAlreadyActive,
)
from graphify.workspace.semantic_queue import SemanticQueuePolicy
from tests.workspace_p3_helpers import (
    COMPATIBILITY_MANIFEST,
    clone_repo,
    create_repo,
    git_output as _git,
    tree_snapshot,
)


REPO_UUID = "11111111-1111-4111-8111-111111111111"
SECOND_UUID = "22222222-2222-4222-8222-222222222222"
SUPPORTED = RuntimeCapabilities.supported_test_fixture()
POLICY = SemanticQueuePolicy(max_items=8, max_bytes=16 * 1024, retry_budget=1)
ACTIVATION_REMOTE = "https://github.com/example/workspace.git"
ACTIVATION_USAGE = (
    "graphify workspace activate --repo-uuid UUID "
    "--expected-registry-revision N --expected-active-source-revision N "
    "--expected-operation-epoch N --expected-migration-epoch N "
    "--authorization-stdin"
)


def _cli() -> Any:
    return importlib.import_module("graphify.workspace.cli")


def _inputs(state_root: Path, *, fault_hook: Any = None) -> WorkspaceRuntimeInputs:
    return WorkspaceRuntimeInputs(
        state_root=state_root,
        compatibility_manifest=COMPATIBILITY_MANIFEST,
        semantic_queue_policy=POLICY,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
    )


def _authorization(action: IdentityAction, nonce: str) -> OperatorAuthorization:
    return OperatorAuthorization(
        action=action,
        operator_id="operator:activation-test",
        reason="private activation authorization",
        issued_at="2026-07-16T15:00:00Z",
        nonce=nonce,
    )


def _authorization_payload(action: str = "ACTIVATE") -> str:
    return canonical_json_bytes(
        {
            "action": action,
            "issued_at": "2026-07-16T15:00:00Z",
            "nonce": "activation-cli-test",
            "operator_id": "operator:activation-test",
            "reason": "private activation authorization",
        }
    ).decode("utf-8")


def _activation_arguments(
    *,
    repo_uuid: str = REPO_UUID,
    registry_revision: int = 2,
    active_source_revision: int = 1,
    operation_epoch: int = 1,
    migration_epoch: int = 0,
) -> list[str]:
    return [
        "activate",
        "--repo-uuid",
        repo_uuid,
        "--expected-registry-revision",
        str(registry_revision),
        "--expected-active-source-revision",
        str(active_source_revision),
        "--expected-operation-epoch",
        str(operation_epoch),
        "--expected-migration-epoch",
        str(migration_epoch),
        "--authorization-stdin",
    ]


def _run_activate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cwd: Path,
    inputs: WorkspaceRuntimeInputs | None,
    authorization: str | None = None,
    repo_uuid: str = REPO_UUID,
    registry_revision: int = 2,
    active_source_revision: int = 1,
    operation_epoch: int = 1,
    migration_epoch: int = 0,
) -> tuple[int, StringIO, StringIO]:
    workspace_cli = _cli()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(authorization if authorization is not None else _authorization_payload()),
    )
    stdout = StringIO()
    stderr = StringIO()
    exit_code = workspace_cli.run_workspace_command(
        _activation_arguments(
            repo_uuid=repo_uuid,
            registry_revision=registry_revision,
            active_source_revision=active_source_revision,
            operation_epoch=operation_epoch,
            migration_epoch=migration_epoch,
        ),
        inputs=inputs,
        stdout=stdout,
        stderr=stderr,
    )
    return exit_code, stdout, stderr


def _activation_payload(stream: StringIO) -> dict[str, Any]:
    payload = json.loads(stream.getvalue())
    Draft202012Validator(
        _cli().load_activation_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    return payload


class _AllowingState:
    def assert_external_to(self, _source_root: Path) -> None:
        pass


def _source_stub(root: Path, *, repo_uuid: str = REPO_UUID) -> SimpleNamespace:
    remote_evidence = {
        "kind": "graphify.workspace.remote_evidence",
        "remote_name": "origin",
        "url": ACTIVATION_REMOTE,
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
        repo_uuid=repo_uuid,
        root=root,
        registry_source=registry_source,
        remote_evidence=(remote_evidence,),
        source_sha256=canonical_sha256(registry_source),
    )


def _bound_clone(tmp_path: Path) -> tuple[Path, Path, Path, RegistryStore]:
    original = create_repo(tmp_path / "original", REPO_UUID)
    clone = clone_repo(
        original,
        tmp_path / "clone",
        remote_url=ACTIVATION_REMOTE,
    )
    state_root = tmp_path / "external-state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    enrolled = store.enroll(
        discover_source(original),
        _authorization(IdentityAction.ENROLL, "activation-enroll"),
        expected_revision=0,
    )
    adopted = store.adopt(
        discover_source(clone),
        _authorization(IdentityAction.ADOPT, "activation-adopt"),
        expected_revision=int(enrolled.to_dict()["revision"]),
    )
    assert adopted.to_dict()["revision"] == 2
    return original, clone, state_root, store


def test_activation_schema_freezes_success_conflict_and_invalid_receipts() -> None:
    schema = _cli().load_activation_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    common = {
        "action": "activate",
        "cli_contract_version": 1,
        "contract": "graphify.workspace.activation",
        "schema_version": 1,
    }
    valid = [
        {
            **common,
            "active_source_revision": 2,
            "exit_code": 0,
            "migration_epoch": 0,
            "operation_epoch": 2,
            "registry_revision": 3,
            "repo_uuid": REPO_UUID,
            "state": "activated",
        },
        {
            **common,
            "action_code": "refresh_activation_cas",
            "exit_code": 10,
            "reason_code": "registry_revision_conflict",
            "registry_revision": 3,
            "state": "conflict",
        },
        {
            **common,
            "action_code": "refresh_activation_cas",
            "exit_code": 10,
            "reason_code": "activation_cas_conflict",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "verify_activation_target",
            "exit_code": 10,
            "reason_code": "identity_mismatch",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "bind_activation_source",
            "exit_code": 10,
            "reason_code": "source_not_bound",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "select_inactive_source",
            "exit_code": 10,
            "reason_code": "source_already_active",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "retry_activation",
            "exit_code": 10,
            "reason_code": "lease_busy",
            "state": "conflict",
        },
        {
            **common,
            "action_code": "run_workspace_doctor",
            "exit_code": 10,
            "reason_code": "workspace_recovery_required",
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

    invalid = [
        {**valid[0], "owner": {"pid": 1}},
        {**valid[0], "path": "/private/source"},
        {**valid[0], "authorization": {"action": "ACTIVATE"}},
        {**valid[0], "active_source_revision": -1},
        {**valid[1], "repo_uuid": REPO_UUID},
        {key: value for key, value in valid[1].items() if key != "registry_revision"},
        {**valid[-1], "operation_epoch": 2},
    ]
    for receipt in invalid:
        assert list(validator.iter_errors(receipt))


def test_activation_docs_freeze_the_standalone_cli_and_redacted_schema() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "docs/workspace/v1/README.md").read_text(encoding="utf-8")
    architecture = (root / "docs/workspace/v1/architecture.md").read_text(
        encoding="utf-8"
    )
    verification = (root / "docs/workspace/v1/verification.md").read_text(
        encoding="utf-8"
    )

    assert ACTIVATION_USAGE in readme
    assert "activation.schema.json" in readme
    assert "`register activate` remains invalid" in readme
    assert "enrollment identity by sharing" in readme
    assert "RegistryStore.activate_source()" in architecture
    assert "continuity with immutable enrollment identity" in architecture
    package_scope = (root / "graphify/workspace/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "active-source slice adds only standalone" in package_scope
    assert "## Active-source activation CLI gates" in verification
    assert "enrollment history root" in verification
    assert (
        "shared usage emitted for malformed register, sync, status, and doctor argv"
        in verification
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["activate"],
        ["activate", "--repo-uuid", "not-a-uuid"],
        _activation_arguments()[:-1],
        [*_activation_arguments(), "--authorization-stdin"],
        [*_activation_arguments(), "--unknown"],
        [
            *_activation_arguments(),
            "--expected-registry-revision",
            "2",
        ],
        _activation_arguments(active_source_revision=-1),
        _activation_arguments(registry_revision=-1),
        _activation_arguments(operation_epoch=-1),
        _activation_arguments(migration_epoch=-1),
        [
            "register",
            "activate",
            "--repo-uuid",
            REPO_UUID,
            "--expected-registry-revision",
            "2",
            "--authorization-stdin",
        ],
    ],
)
def test_activation_usage_errors_precede_authority_stdin_source_and_state(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    workspace_cli = _cli()
    monkeypatch.setattr(
        workspace_cli,
        "load_workspace_runtime_inputs",
        lambda: pytest.fail("malformed activation argv must not load authority"),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda *_args, **_kwargs: pytest.fail("malformed activation argv must not discover"),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            read=lambda *_args: pytest.fail("malformed activation argv must not read stdin")
        ),
    )
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        arguments,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 64
    assert stdout.getvalue() == ""
    assert ACTIVATION_USAGE in stderr.getvalue()
    assert "register <enroll|adopt|rebind|rotate>" in stderr.getvalue()


def test_top_level_activation_skips_ambient_install_version_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("bounded activation must not inspect ambient installs"),
    )
    monkeypatch.setattr(mainmod, "dispatch_install_cli", lambda _command: False)
    observed: list[str] = []
    monkeypatch.setattr(mainmod, "dispatch_command", observed.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["graphify", "workspace", "activate", *_activation_arguments()[1:]],
    )

    mainmod._run_cli()

    assert observed == ["workspace"]


@pytest.mark.parametrize("help_flag", ["-h", "--help", "-?"])
def test_top_level_activation_help_and_version_check_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    help_flag: str,
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(
        mainmod,
        "_check_skill_version",
        lambda _path: pytest.fail("bounded activation must not inspect ambient installs"),
    )
    monkeypatch.setattr(sys, "argv", ["graphify", "workspace", "activate", help_flag])

    with pytest.raises(SystemExit) as raised:
        mainmod._run_cli()

    captured = capsys.readouterr()
    assert raised.value.code == 64
    assert captured.out == ""
    assert captured.err == _cli()._USAGE + "\n"


def test_top_level_help_lists_the_workspace_activation_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mainmod = importlib.import_module("graphify.__main__")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])

    mainmod._run_cli()

    assert "workspace activate --repo-uuid UUID ..." in capsys.readouterr().out


def test_activation_composes_authority_before_authorization_or_source(
    monkeypatch: pytest.MonkeyPatch,
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
        or (_ for _ in ()).throw(UnsupportedCompatibility("private runtime")),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda *_args, **_kwargs: pytest.fail("invalid authority must not discover source"),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            read=lambda *_args: pytest.fail("invalid authority must not read stdin")
        ),
    )
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        _activation_arguments(),
        stdout=stdout,
        stderr=stderr,
    )

    payload = _activation_payload(stderr)
    assert result == 20
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "unsupported_compatibility"
    assert calls == ["load", "compose"]


@pytest.mark.parametrize(
    "authorization",
    [
        _authorization_payload("REBIND"),
        (
            '{"action":"ACTIVATE","action":"ACTIVATE",'
            '"issued_at":"2026-07-16T15:00:00Z","nonce":"n",'
            '"operator_id":"op","reason":"r"}'
        ),
        json.dumps(
            {
                "action": "ACTIVATE",
                "issued_at": "2026-07-16T15:00:00Z",
                "nonce": "activation-cli-test",
                "operator_id": "operator:activation-test",
                "reason": "private activation authorization",
            },
            indent=2,
        ),
        json.dumps(
            {
                "action": "ACTIVATE",
                "issued_at": "2026-07-16T15:00:00Z",
                "nonce": "\ud800",
                "operator_id": "op",
                "reason": "r",
            }
        ),
        "x" * (16 * 1024 + 1),
    ],
)
def test_activation_authorization_is_bounded_duplicate_safe_canonical_and_exact(
    monkeypatch: pytest.MonkeyPatch,
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
        lambda _inputs: calls.append("compose")
        or SimpleNamespace(registry=object(), leases=object()),
    )
    monkeypatch.setattr(
        workspace_cli,
        "source_root_identity",
        lambda *_args, **_kwargs: pytest.fail("invalid authorization must not inspect source"),
    )
    monkeypatch.setattr(sys, "stdin", StringIO(authorization))
    stdout = StringIO()
    stderr = StringIO()

    result = workspace_cli.run_workspace_command(
        _activation_arguments(),
        stdout=stdout,
        stderr=stderr,
    )

    payload = _activation_payload(stderr)
    assert result == 20
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "authorization_invalid"
    assert payload["action_code"] == "provide_valid_authorization"
    assert calls == ["load", "compose"]
    assert "private activation authorization" not in stderr.getvalue()


def test_activation_delegates_once_with_all_cas_and_internal_lease_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    source = _source_stub(cwd)
    owner = object()
    fixed_time = datetime(2026, 7, 16, 15, 1, tzinfo=timezone.utc)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Registry:
        state = _AllowingState()

        def activate_source(self, *args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return SimpleNamespace(
                registry=SimpleNamespace(
                    to_dict=lambda: {
                        "revision": 8,
                        "workspaces": [
                            {
                                "repo_uuid": REPO_UUID,
                                "active_source_revision": 4,
                            }
                        ],
                    }
                ),
                grant=SimpleNamespace(operation_epoch=12, migration_epoch=2),
            )

    leases = SimpleNamespace(current_owner=lambda: owner)
    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(registry=Registry(), leases=leases),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: source,
    )
    monkeypatch.setattr(workspace_cli, "source_root_identity", lambda *_a, **_k: object())
    monkeypatch.setattr(workspace_cli, "verify_source_checkout", lambda *_a, **_k: None)
    monkeypatch.setattr(workspace_cli, "_activation_timestamp", lambda: fixed_time)
    monkeypatch.setattr(workspace_cli, "_activation_monotonic_ns", lambda: 123_456)

    exit_code, stdout, stderr = _run_activate(
        monkeypatch,
        cwd=cwd,
        inputs=None,
        registry_revision=7,
        active_source_revision=3,
        operation_epoch=11,
        migration_epoch=2,
    )

    expected = {
        "action": "activate",
        "active_source_revision": 4,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.activation",
        "exit_code": 0,
        "migration_epoch": 2,
        "operation_epoch": 12,
        "registry_revision": 8,
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "state": "activated",
    }
    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert _activation_payload(stdout) == expected
    assert stdout.getvalue().encode("utf-8") == canonical_json_bytes(expected)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] is source
    assert isinstance(args[1], OperatorAuthorization)
    assert args[1].action is IdentityAction.ACTIVATE
    assert kwargs == {
        "leases": leases,
        "owner": owner,
        "expected_registry_revision": 7,
        "expected_active_source_revision": 3,
        "expected_operation_epoch": 11,
        "expected_migration_epoch": 2,
        "acquired_at": fixed_time,
        "monotonic_ns": 123_456,
        "ttl_ns": workspace_cli._ACTIVATION_LEASE_TTL_NS,
        "require_source_change": True,
    }
    assert 0 < workspace_cli._ACTIVATION_LEASE_TTL_NS <= 60_000_000_000
    assert "operator:activation-test" not in stdout.getvalue()
    assert str(cwd) not in stdout.getvalue()


@pytest.mark.parametrize(
    (
        "error",
        "state",
        "exit_code",
        "reason_code",
        "action_code",
        "revision",
    ),
    [
        (
            RevisionConflict("private registry revision", actual_registry_revision=7),
            "conflict",
            10,
            "registry_revision_conflict",
            "refresh_activation_cas",
            7,
        ),
        (
            RevisionConflict("private active-source or epoch revision"),
            "conflict",
            10,
            "activation_cas_conflict",
            "refresh_activation_cas",
            None,
        ),
        (
            UUIDCollisionError("private source path"),
            "conflict",
            10,
            "identity_mismatch",
            "verify_activation_target",
            None,
        ),
        (
            SourceAmbiguousError("private unbound source"),
            "conflict",
            10,
            "source_not_bound",
            "bind_activation_source",
            None,
        ),
        (
            SourceAlreadyActive("private selected source"),
            "conflict",
            10,
            "source_already_active",
            "select_inactive_source",
            None,
        ),
        (
            LeaseBusy("private owner identity"),
            "conflict",
            10,
            "lease_busy",
            "retry_activation",
            None,
        ),
        (
            LeaseRecoveryRequired("private reservation"),
            "conflict",
            10,
            "workspace_recovery_required",
            "run_workspace_doctor",
            None,
        ),
        (
            WorkspaceAuthorityError("private authority"),
            "invalid",
            20,
            "runtime_authority_invalid",
            "install_candidate_authority",
            None,
        ),
        (
            SourceDiscoveryTimeout("private source timeout"),
            "invalid",
            20,
            "source_discovery_timeout",
            "retry_activation",
            None,
        ),
        (
            IdentityError("private source identity"),
            "invalid",
            20,
            "identity_error",
            "fix_workspace_source",
            None,
        ),
        (
            StatePathError("private state path"),
            "invalid",
            20,
            "unsafe_state_path",
            "configure_safe_state_root",
            None,
        ),
        (
            UnsupportedRuntime("private runtime"),
            "invalid",
            20,
            "unsupported_runtime",
            "use_supported_runtime",
            None,
        ),
        (
            StateCorrupt("private state"),
            "invalid",
            20,
            "state_corrupt",
            "run_workspace_doctor",
            None,
        ),
        (
            CommitUnknown("private uncertain mutation"),
            "invalid",
            20,
            "commit_unknown",
            "run_workspace_doctor",
            None,
        ),
        (
            TypeError("private raw failure"),
            "invalid",
            20,
            "activation_failed",
            "run_workspace_doctor",
            None,
        ),
    ],
)
def test_activation_failures_are_canonical_opaque_and_redacted(
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

        def activate_source(self, *_args: object, **_kwargs: object) -> None:
            raise error

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(
            registry=Registry(),
            leases=SimpleNamespace(current_owner=lambda: object()),
        ),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: source,
    )
    monkeypatch.setattr(workspace_cli, "source_root_identity", lambda *_a, **_k: object())
    monkeypatch.setattr(workspace_cli, "verify_source_checkout", lambda *_a, **_k: None)

    result, stdout, stderr = _run_activate(
        monkeypatch,
        cwd=cwd,
        inputs=None,
        registry_revision=6,
    )

    payload = _activation_payload(stderr)
    assert result == exit_code
    assert stdout.getvalue() == ""
    assert payload["state"] == state
    assert payload["reason_code"] == reason_code
    assert payload["action_code"] == action_code
    if revision is None:
        assert "registry_revision" not in payload
    else:
        assert payload["registry_revision"] == revision
    assert "repo_uuid" not in payload
    assert "private" not in stderr.getvalue()
    assert str(cwd) not in stderr.getvalue()
    assert "owner" not in stderr.getvalue()


def test_activation_requires_matching_uuid_and_an_explicitly_bound_source_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    enrolled = create_repo(tmp_path / "enrolled", REPO_UUID)
    unbound = create_repo(tmp_path / "unbound", REPO_UUID)
    state_root = tmp_path / "external-state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        discover_source(enrolled),
        _authorization(IdentityAction.ENROLL, "bound-source-enroll"),
        expected_revision=0,
    )
    before_state = tree_snapshot(state_root)
    before_sources = {root: tree_snapshot(root) for root in (enrolled, unbound)}

    exit_code, stdout, stderr = _run_activate(
        monkeypatch,
        cwd=unbound,
        inputs=_inputs(state_root),
        registry_revision=1,
    )

    payload = _activation_payload(stderr)
    assert exit_code == 10
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "source_not_bound"
    assert tree_snapshot(state_root) == before_state
    assert {root: tree_snapshot(root) for root in (enrolled, unbound)} == before_sources

    mismatch_exit, mismatch_stdout, mismatch_stderr = _run_activate(
        monkeypatch,
        cwd=unbound,
        inputs=_inputs(state_root),
        repo_uuid=SECOND_UUID,
        registry_revision=1,
    )
    assert mismatch_exit == 10
    assert mismatch_stdout.getvalue() == ""
    assert _activation_payload(mismatch_stderr)["reason_code"] == "identity_mismatch"
    assert tree_snapshot(state_root) == before_state


def test_activation_rejects_bound_locator_replaced_by_unrelated_history_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    enrolled = create_repo(tmp_path / "enrolled", REPO_UUID)
    enrolled_source = discover_source(enrolled)
    state_root = tmp_path / "external-state"
    store = RegistryStore(state_root, capabilities=SUPPORTED)
    store.enroll(
        enrolled_source,
        _authorization(IdentityAction.ENROLL, "replacement-enroll"),
        expected_revision=0,
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
    (replacement / "README.md").write_text(
        "unrelated replacement\n",
        encoding="utf-8",
    )
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
    before_state = tree_snapshot(state_root)
    before_source = tree_snapshot(enrolled)

    exit_code, stdout, stderr = _run_activate(
        monkeypatch,
        cwd=enrolled,
        inputs=_inputs(state_root),
        registry_revision=1,
    )

    assert exit_code == 10
    assert stdout.getvalue() == ""
    payload = _activation_payload(stderr)
    assert payload["reason_code"] == "source_not_bound"
    assert payload["action_code"] == "bind_activation_source"
    assert tree_snapshot(state_root) == before_state
    assert tree_snapshot(enrolled) == before_source


def test_activation_cli_switches_the_bound_active_source_without_checkout_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original, clone, state_root, store = _bound_clone(tmp_path)
    before_sources = {root: tree_snapshot(root) for root in (original, clone)}

    exit_code, stdout, stderr = _run_activate(
        monkeypatch,
        cwd=clone,
        inputs=_inputs(state_root),
    )

    payload = _activation_payload(stdout)
    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert payload == {
        "action": "activate",
        "active_source_revision": 2,
        "cli_contract_version": 1,
        "contract": "graphify.workspace.activation",
        "exit_code": 0,
        "migration_epoch": 0,
        "operation_epoch": 2,
        "registry_revision": 3,
        "repo_uuid": REPO_UUID,
        "schema_version": 1,
        "state": "activated",
    }
    entry = store.load().to_dict()["workspaces"][0]
    assert entry["active_source"] == discover_source(clone).registry_source
    assert entry["active_source_revision"] == 2
    assert entry["active_source_evidence"]["operation_epoch"] == 2
    assert {alias["path"] for alias in entry["aliases"]} == {str(original.resolve())}
    lease_state = _cli().compose_workspace_runtime(_inputs(state_root)).leases.inspect(REPO_UUID)
    assert lease_state.leases == {}
    assert lease_state.operation_epoch == 2
    assert {root: tree_snapshot(root) for root in (original, clone)} == before_sources


def test_activation_cli_rejects_reselecting_the_active_source_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original, clone, state_root, store = _bound_clone(tmp_path)
    before_state = tree_snapshot(state_root)
    before_sources = {root: tree_snapshot(root) for root in (original, clone)}
    before_registry = store.load().to_dict()

    exit_code, stdout, stderr = _run_activate(
        monkeypatch,
        cwd=original,
        inputs=_inputs(state_root),
    )

    payload = _activation_payload(stderr)
    assert exit_code == 10
    assert stdout.getvalue() == ""
    assert payload["reason_code"] == "source_already_active"
    assert payload["action_code"] == "select_inactive_source"
    assert tree_snapshot(state_root) == before_state
    assert {root: tree_snapshot(root) for root in (original, clone)} == before_sources
    assert store.load().to_dict() == before_registry


def test_activation_partial_write_recovery_never_duplicates_the_registry_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _original, clone, state_root, _store = _bound_clone(tmp_path)
    fired = False

    def crash_once(event: str) -> None:
        nonlocal fired
        if event == "registry:current_replaced" and not fired:
            fired = True
            raise InjectedFault(event)

    first_exit, first_stdout, first_stderr = _run_activate(
        monkeypatch,
        cwd=clone,
        inputs=_inputs(state_root, fault_hook=crash_once),
    )
    second_exit, second_stdout, second_stderr = _run_activate(
        monkeypatch,
        cwd=clone,
        inputs=_inputs(state_root),
    )

    assert fired
    assert first_exit == 20
    assert first_stdout.getvalue() == ""
    assert _activation_payload(first_stderr)["reason_code"] == "commit_unknown"
    assert second_exit == 10
    assert second_stdout.getvalue() == ""
    assert _activation_payload(second_stderr)["reason_code"] == "registry_revision_conflict"
    document = RegistryStore(state_root, capabilities=SUPPORTED).load().to_dict()
    assert document["revision"] == 3
    assert document["workspaces"][0]["active_source_revision"] == 2


def test_activation_re_raises_injected_faults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_cli = _cli()
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    source = _source_stub(cwd)

    class Registry:
        state = _AllowingState()

        def activate_source(self, *_args: object, **_kwargs: object) -> None:
            raise InjectedFault("activation-cli-injected")

    monkeypatch.setattr(workspace_cli, "load_workspace_runtime_inputs", lambda: object())
    monkeypatch.setattr(
        workspace_cli,
        "compose_workspace_runtime",
        lambda _inputs: SimpleNamespace(
            registry=Registry(),
            leases=SimpleNamespace(current_owner=lambda: object()),
        ),
    )
    monkeypatch.setattr(
        workspace_cli,
        "discover_source",
        lambda _root, *, deadline_ns, max_bytes: source,
    )
    monkeypatch.setattr(workspace_cli, "source_root_identity", lambda *_a, **_k: object())
    monkeypatch.setattr(workspace_cli, "verify_source_checkout", lambda *_a, **_k: None)

    with pytest.raises(InjectedFault, match="activation-cli-injected"):
        _run_activate(monkeypatch, cwd=cwd, inputs=None)
