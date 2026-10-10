"""Public rollback transport, with disposable lifecycle roots only."""
import copy
import json
import sys
import time

import pytest

from graphify.workspace import cli
from graphify.workspace.cli_contracts import WorkspaceCommandRequest
from graphify.workspace.contracts import canonical_json_bytes
from tests.test_workspace_contracts import compatibility
from tests.workspace_s3_helpers import REPO_UUID


def schema_validator(name):
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from graphify.workspace.contracts import SCHEMA_FILES
    root = Path(__file__).resolve().parents[1] / "graphify/workspace/schemas"
    schemas = [json.loads((root / item).read_text()) for item in SCHEMA_FILES]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    return Draft202012Validator(next(s for s in schemas if s["$id"].endswith(name)), registry=registry)


def rollback_parameters():
    return {"repo_uuid": REPO_UUID, "authorization": {
        "action": "ROLLBACK", "operator_id": "fixture", "reason": "disposable rollback",
        "issued_at": "2026-10-09T00:00:00Z", "nonce": "rollback-fixture"},
        "expected_registry_revision": 1, "expected_active_source_revision": 1,
        "expected_operation_epoch": 4, "expected_migration_epoch": 0,
        "expected_fence_high_watermark": 4, "expected_pointer_revision": 2,
        "expected_current_receipt_sha256": "b" * 64, "expected_source_epoch": 1,
        "target_generation_id": "gen-first", "target_receipt_sha256": "a" * 64}


def command(state_root, parameters=None):
    return {"contract": "graphify.workspace.cli.request", "format_version": 1,
        "command": "rollback", "request_id": "s6-rollback", "state_root": str(state_root),
        "compatibility_manifest": compatibility().to_dict(), "timeout_ms": 60000,
        "parameters": rollback_parameters() if parameters is None else parameters}


def test_public_rollback_accepts_canonical_request(tmp_path, monkeypatch, capsys):
    value = command(tmp_path.resolve())
    request = WorkspaceCommandRequest.from_json(canonical_json_bytes(value))
    schema_validator("cli-request.schema.json").validate(value)
    assert request.to_dict() == value
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(cli, "_read_request", lambda name: request.canonical)
    from graphify.workspace.cli_contracts import response
    pointer = {"contract": "graphify.workspace.pointer_set", "schema_version": 2,
        "repo_uuid": REPO_UUID, "pointer_revision": 3, "active_source_revision": 1,
        "source_epoch": 1, "operation_epoch": 5, "fence_token": 5,
        "state_schema_version": 2, "current": {"generation_id": "gen-first",
            "receipt_sha256": "a" * 64}, "last_good": {"generation_id": "gen-second",
            "receipt_sha256": "b" * 64}}
    monkeypatch.setattr(cli, "_run_bounded", lambda *args: (
        response("rollback", "s6-rollback", result={"pointer": pointer}), 0))
    assert cli.run_workspace_cli(["rollback", "--request", "-"]) == 0
    output = capsys.readouterr()
    assert output.err == "" and json.loads(output.out)["result"] == {"pointer": pointer}
    schema_validator("cli-response.schema.json").validate(json.loads(output.out))


@pytest.mark.parametrize("field,value", [
    ("expected_pointer_revision", True), ("expected_source_epoch", "1"),
    ("expected_operation_epoch", -1), ("expected_fence_high_watermark", 2**63),
    ("target_generation_id", "../secret"), ("target_receipt_sha256", "A" * 64),
    ("authorization", {"action": "REPAIR_EXECUTE"}), ("arbitrary_history", True),
])
def test_bad_rollback_is_redacted_before_execution(tmp_path, monkeypatch, capsys, field, value):
    request = command(tmp_path.resolve())
    request["parameters"][field] = value
    assert list(schema_validator("cli-request.schema.json").iter_errors(request))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(request))
    monkeypatch.setattr(cli, "_run_bounded", lambda *args: pytest.fail("invalid request executed"))
    assert cli.run_workspace_cli(["rollback", "--request", "-"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "secret" not in output.err
    assert json.loads(output.err)["error_code"] == "bad_request"


def parameters_for(runtime):
    stores = runtime.stores
    pointer = json.loads(stores.pointers.state.path(stores.pointers._current(REPO_UUID)).read_bytes())
    lease = stores.leases.inspect(REPO_UUID)
    registry = stores.registry.load().to_dict()
    target = pointer["last_good"]
    p = rollback_parameters()
    p.update(expected_registry_revision=registry["revision"],
        expected_active_source_revision=registry["workspaces"][0]["active_source_revision"],
        expected_operation_epoch=lease.operation_epoch, expected_migration_epoch=lease.migration_epoch,
        expected_fence_high_watermark=lease.fence_high_watermark,
        expected_pointer_revision=pointer["pointer_revision"],
        expected_current_receipt_sha256=pointer["current"]["receipt_sha256"],
        target_generation_id=target["generation_id"], target_receipt_sha256=target["receipt_sha256"])
    return p


@pytest.fixture
def public_runtime(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from graphify.workspace import composition
    from tests.test_workspace_structural_s4 import runtime_fixture, request_for
    from graphify.workspace.sync import prepare_structural_sync, synchronize_structural
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    first = synchronize_structural(runtime, request_for(runtime, "gen-first"), attempt_sha256="a" * 64)
    second_request = prepare_structural_sync(runtime, repo_uuid=REPO_UUID,
        generation_id="gen-second", source_epoch=2, desired_watermark=2,
        expected_payload_bytes=1024 * 1024)
    second = synchronize_structural(runtime, second_request, attempt_sha256="b" * 64)
    assert (first.pointer_revision, second.pointer_revision) == (1, 2)
    p = parameters_for(runtime)
    monkeypatch.setattr(composition, "compose_workspace_runtime",
        lambda inputs: SimpleNamespace(require_runtime=lambda: runtime))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    def run(parameters=p, *, expected=0):
        value = command(runtime.inputs.state_root, parameters)
        monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(value))
        # The focused transport tests exercise real stores in process. Separate
        # installed tests keep the supervisor and package admission unchanged.
        def dispatch(request, deadline):
            from graphify.workspace.cli_contracts import response
            try:
                result = cli._execute(request.to_dict(), deadline)
                reply = response("rollback", "s6-rollback", result=result)
            except Exception as exc:
                reply = response("rollback", "s6-rollback", error_code=cli._error(exc, mutation=True))
            return reply, cli._exit_code(reply.to_dict()["error_code"])
        monkeypatch.setattr(cli, "_run_bounded", dispatch)
        assert cli.run_workspace_cli(["rollback", "--request", "-"]) == expected
    return runtime, repo, p, run


def test_public_rollback_and_exact_retry_preserve_payloads(public_runtime, capsys):
    from tests.workspace_s3_helpers import tree_snapshot
    from graphify.workspace.lifecycle_contracts import decode_journal_frame
    runtime, repo, p, run = public_runtime
    workspace = runtime.inputs.state_root / "workspaces" / REPO_UUID
    source_before = tree_snapshot(repo)
    payload_before = tree_snapshot(workspace / "generations")
    unrelated = runtime.inputs.state_root / "unrelated"
    unrelated.write_bytes(b"preserve this content")
    run()
    result = json.loads(capsys.readouterr().out)["result"]["pointer"]
    assert result == json.loads((workspace / "pointers.json").read_bytes())
    assert result["pointer_revision"] == 3
    assert result["current"] == {"generation_id": p["target_generation_id"],
        "receipt_sha256": p["target_receipt_sha256"]}
    assert result["source_epoch"] == p["expected_source_epoch"] == 1
    events = [decode_journal_frame(path.read_bytes()).to_dict()
              for path in sorted((workspace / "journal/segments").glob("*.gwf"))]
    assert events[-1]["transition"] == "ROLLED_BACK" and events[-1]["pointer_revision"] == 3
    assert events[-1]["fence_token"] == p["expected_fence_high_watermark"] + 1
    assert tree_snapshot(repo) == source_before
    assert tree_snapshot(workspace / "generations") == payload_before
    assert unrelated.read_bytes() == b"preserve this content"
    before = tree_snapshot(runtime.inputs.state_root)
    run()
    assert json.loads(capsys.readouterr().out)["result"]["pointer"] == result
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_corrupt_current_pointer_epoch_refuses_before_mutation(public_runtime, capsys):
    from graphify.workspace.lifecycle_contracts import PointerSet
    from tests.workspace_s3_helpers import tree_snapshot
    runtime, repo, p, run = public_runtime
    pointer = runtime.inputs.state_root / "workspaces" / REPO_UUID / "pointers.json"
    original = json.loads(pointer.read_bytes())
    assert original["source_epoch"] == 2 and p["expected_source_epoch"] == 1
    pointer.write_bytes(PointerSet.from_mapping(dict(original, source_epoch=3)).canonical)
    before = (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo))
    run(expected=4)
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error_code"] == "workspace_refused"
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


def test_corrupt_current_generation_keeps_core_rollback_behavior(public_runtime, capsys):
    from tests.workspace_s3_helpers import tree_snapshot
    runtime, repo, p, run = public_runtime
    workspace = runtime.inputs.state_root / "workspaces" / REPO_UUID
    current = workspace / "generations" / "gen-second"
    receipt = current / "receipt.json"
    receipt.chmod(0o600)
    receipt.write_bytes(b"corrupt previous current receipt")
    damaged = tree_snapshot(current)
    target = tree_snapshot(workspace / "generations" / p["target_generation_id"])
    source = tree_snapshot(repo)
    run()
    result = json.loads(capsys.readouterr().out)["result"]["pointer"]
    assert result["current"] == {"generation_id": p["target_generation_id"],
        "receipt_sha256": p["target_receipt_sha256"]}
    assert result["source_epoch"] == p["expected_source_epoch"] == 1
    assert not current.exists()
    assert tree_snapshot(workspace / "quarantine/corrupt/gen-second.3") == damaged
    assert tree_snapshot(workspace / "generations" / p["target_generation_id"]) == target
    assert tree_snapshot(repo) == source
    before = tree_snapshot(runtime.inputs.state_root)
    run()
    assert json.loads(capsys.readouterr().out)["result"]["pointer"] == result
    assert tree_snapshot(runtime.inputs.state_root) == before


@pytest.mark.parametrize("boundary", ["after_acquire", "move", "release", "after_release"])
def test_post_acquisition_failures_are_unknown(public_runtime, monkeypatch, capsys, boundary):
    from graphify.workspace.leases import LeaseError
    from graphify.workspace.pointers import PointerConflict
    from tests.workspace_s3_helpers import tree_snapshot
    runtime, repo, p, run = public_runtime
    workspace = runtime.inputs.state_root / "workspaces" / REPO_UUID
    authority = runtime.inputs.state_root / "runtime-manifest.json"
    original_authority = authority.read_bytes()
    payload = tree_snapshot(workspace / "generations")
    source = tree_snapshot(repo)
    original_acquire = runtime.stores.leases.acquire
    original_release = runtime.stores.leases.release
    def acquire(*args, **kwargs):
        grant = original_acquire(*args, **kwargs)
        if boundary == "after_acquire":
            authority.write_bytes(b"private invalid authority")
        return grant
    def move(*args, **kwargs):
        raise PointerConflict("private pointer failure")
    def release(*args, **kwargs):
        if boundary == "release":
            raise LeaseError("private release failure")
        result = original_release(*args, **kwargs)
        if boundary == "after_release":
            authority.write_bytes(b"private invalid authority")
        return result
    monkeypatch.setattr(runtime.stores.leases, "acquire", acquire)
    monkeypatch.setattr(runtime.stores.leases, "release", release)
    if boundary == "move":
        monkeypatch.setattr(runtime.stores.pointers, "rollback", move)
    run(expected=5)
    output = capsys.readouterr()
    assert output.out == "" and "private" not in output.err
    assert json.loads(output.err)["error_code"] == "execution_unknown"
    lease = json.loads((workspace / "workspace.json").read_bytes())
    pointer = json.loads((workspace / "pointers.json").read_bytes())
    moved = boundary in {"release", "after_release"}
    assert lease["operation_epoch"] == p["expected_operation_epoch"] + 1
    assert lease["fence_high_watermark"] == p["expected_fence_high_watermark"] + 1
    assert bool(lease["leases"]) == (boundary != "after_release")
    assert pointer["pointer_revision"] == p["expected_pointer_revision"] + moved
    if moved:
        assert pointer["current"] == {"generation_id": p["target_generation_id"],
            "receipt_sha256": p["target_receipt_sha256"]}
    assert tree_snapshot(workspace / "generations") == payload
    assert tree_snapshot(repo) == source
    authority.write_bytes(original_authority)
    before = tree_snapshot(runtime.inputs.state_root)
    run(expected=0 if boundary == "after_release" else 4)
    capsys.readouterr()
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_stale_public_rollback_refuses_before_mutation(public_runtime, capsys):
    from tests.workspace_s3_helpers import tree_snapshot
    runtime, repo, p, run = public_runtime
    for field in ("expected_registry_revision", "expected_active_source_revision",
                  "expected_operation_epoch", "expected_migration_epoch", "expected_source_epoch",
                  "expected_fence_high_watermark", "expected_pointer_revision"):
        changed = copy.deepcopy(p)
        changed[field] += 1
        before = (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo))
        run(changed, expected=4)
        output = capsys.readouterr()
        assert output.out == "" and json.loads(output.err)["error_code"] == "workspace_refused"
        assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before
    for field in ("target_receipt_sha256", "expected_current_receipt_sha256", "target_generation_id"):
        changed = dict(p, **{field: "gen-second" if field.endswith("id") else "f" * 64})
        before = tree_snapshot(runtime.inputs.state_root)
        run(changed, expected=4)
        capsys.readouterr()
        assert tree_snapshot(runtime.inputs.state_root) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "incompatible", "no_last_good", "pending", "gc", "staged"])
def test_damaged_last_good_and_recovery_refuse(public_runtime, capsys, damage):
    from tests.workspace_s3_helpers import tree_snapshot
    from graphify.workspace.lifecycle_contracts import GenerationReceipt, PointerSet
    runtime, repo, p, run = public_runtime
    workspace = runtime.inputs.state_root / "workspaces" / REPO_UUID
    receipt = workspace / "generations" / p["target_generation_id"] / "receipt.json"
    if damage == "missing":
        receipt.unlink()
    elif damage == "corrupt":
        receipt.chmod(0o600)
        receipt.write_bytes(b"not a receipt")
    elif damage == "incompatible":
        value = json.loads(receipt.read_bytes())
        value["compatibility_sha256"] = "f" * 64
        value["completion_binding"]["compatibility_sha256"] = "f" * 64
        receipt.chmod(0o600)
        receipt.write_bytes(GenerationReceipt.from_mapping(value).canonical)
    elif damage == "no_last_good":
        pointer = workspace / "pointers.json"
        value = json.loads(pointer.read_bytes())
        value["last_good"] = None
        pointer.write_bytes(PointerSet.from_mapping(value).canonical)
    else:
        path = workspace / {"pending": "pointers.pending.json", "gc": "gc/intent.json",
                            "staged": "staged-build.pending.json"}[damage]
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(b"pending intent")
        path.chmod(0o600)
    before = (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo))
    run(expected=4)
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error_code"] == "workspace_refused"
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


@pytest.mark.parametrize("boundary", ["pending_durable", "visible", "journal_durable"])
def test_interrupted_public_rollback_is_unknown_and_retry_preserves_intent(public_runtime, capsys, boundary):
    from graphify.workspace.persistence import InjectedFault
    from tests.workspace_s3_helpers import tree_snapshot
    runtime, repo, p, run = public_runtime
    def fail(label):
        if label == "pointer:rolled_back:" + boundary:
            raise InjectedFault(label)
    runtime.stores.pointers.fault_hook = fail
    payload = tree_snapshot(runtime.inputs.state_root / "workspaces" / REPO_UUID / "generations")
    source = tree_snapshot(repo)
    run(expected=5)
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error_code"] == "execution_unknown"
    assert tree_snapshot(repo) == source
    assert tree_snapshot(runtime.inputs.state_root / "workspaces" / REPO_UUID / "generations") == payload
    before = tree_snapshot(runtime.inputs.state_root)
    run(expected=4)
    capsys.readouterr()
    assert tree_snapshot(runtime.inputs.state_root) == before


def test_public_rollback_contention_preserves_occupied_lease(public_runtime, capsys):
    from datetime import datetime, timezone
    from tests.workspace_s3_helpers import tree_snapshot
    runtime, repo, p, run = public_runtime
    runtime.stores.leases.acquire(REPO_UUID, "ROLLBACK", runtime.stores.leases.current_owner(),
        **{key: p[key] for key in ("expected_registry_revision", "expected_active_source_revision",
            "expected_operation_epoch", "expected_migration_epoch")},
        acquired_at=datetime.now(timezone.utc), monotonic_ns=time.monotonic_ns(), ttl_ns=10**12)
    before = (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo))
    run(expected=4)
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error_code"] == "workspace_refused"
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


@pytest.mark.parametrize("outcome", ["timeout", "malformed", "wrong_exit", "oversized"])
def test_rollback_worker_ambiguity_withholds_success(tmp_path, monkeypatch, capsys, outcome):
    from graphify.workspace.cli_contracts import response
    request = command(tmp_path.resolve())
    request["timeout_ms"] = 1000
    marker = tmp_path / "durable-effect"
    if outcome == "timeout":
        bootstrap = f"import time; from pathlib import Path; Path({str(marker)!r}).write_text('prior effect'); time.sleep(60)"
    elif outcome == "malformed":
        bootstrap = "print('private source and secret partial result')"
    elif outcome == "wrong_exit":
        raw = response("rollback", "s6-rollback", error_code="workspace_refused").canonical
        bootstrap = f"import sys; sys.stdout.buffer.write({raw!r})"
    else:
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 1024)
        bootstrap = "print('secret partial result' * 1024)"
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(request))
    monkeypatch.setattr(cli, "_BOOTSTRAP", bootstrap)
    assert cli.run_workspace_cli(["rollback", "--request", "-"]) == 5
    output = capsys.readouterr()
    assert output.out == "" and "secret" not in output.err and "partial" not in output.err
    assert json.loads(output.err)["error_code"] == "execution_unknown"
    if outcome == "timeout":
        assert marker.read_text() == "prior effect"


def test_candidate_identity_requires_rollback_helper():
    from graphify.workspace.contracts import CompatibilityManifest, ContractError
    candidate = compatibility().to_dict()
    assert "graphify/workspace/rollback.py" in candidate["package_members"]
    del candidate["package_members"]["graphify/workspace/rollback.py"]
    with pytest.raises(ContractError):
        CompatibilityManifest.from_mapping(candidate)
