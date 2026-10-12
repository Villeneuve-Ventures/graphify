"""Bounded public repair, with mutations confined to disposable fixtures."""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from graphify.workspace import cli
from graphify.workspace.cli_contracts import response, read_only_request
from graphify.workspace.contracts import canonical_json_bytes
from graphify.workspace.lifecycle_contracts import PointerSet
from tests.test_workspace_contracts import compatibility
from tests.test_workspace_s6_rollback import schema_validator
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


def approval_fixture():
    """Return a bounded approval record for wire-contract tests."""
    return {"repo_uuid": REPO_UUID, "state_root_sha256": "a" * 64,
        "compatibility_sha256": "b" * 64, "active_source_sha256": "c" * 64,
        "coordinates": {"registry_revision": 1, "active_source_revision": 1,
            "operation_epoch": 2, "migration_epoch": 0, "fence_high_watermark": 2, "source_epoch": 1,
            "result_operation_epoch": 3, "result_fence_token": 3},
        "plan": {"classification": "repairable", "candidate": {"generation_id": "gen-repair",
            "receipt_sha256": "d" * 64}, "last_good": None, "next_pointer_revision": 2,
            "selected_from": "journal", "pointer_action": "replace", "journal_actions": ["append_repair"],
            "quarantine": [], "decision_sha256": "e" * 64}}


def execute_parameters(approval):
    """Copy approval into an explicit disposable repair execution request."""
    return {"operation": "execute", "repo_uuid": REPO_UUID, "approval": copy.deepcopy(approval),
        "authorization": {"action": "REPAIR_EXECUTE", "operator_id": "fixture",
            "reason": "disposable repair", "issued_at": "2026-10-12T00:00:00Z", "nonce": "repair-fixture"}}


def request(state, parameters):
    """Wrap repair parameters in the canonical public command envelope."""
    return {"contract": "graphify.workspace.cli.request", "format_version": 1,
        "command": "repair", "request_id": "s6-repair", "state_root": str(state),
        "compatibility_manifest": compatibility().to_dict(), "timeout_ms": 60000,
        "parameters": parameters}


@pytest.mark.parametrize("change", ["authorization", "digest", "extra", "counter", "candidate", "actions",
    "missing_approval", "workspace", "preview_approval"])
def test_malformed_request_refuses_at_public_entry(tmp_path, monkeypatch, capsys, change):
    """Reject malformed approval before dispatch and redact private input."""
    p = execute_parameters(approval_fixture())
    if change == "authorization": p["authorization"]["action"] = "ROLLBACK"
    elif change == "digest": p["approval"]["plan"]["decision_sha256"] = "A" * 64
    elif change == "extra": p["approval"]["plan"]["private_path"] = "/private/secret"
    elif change == "counter": p["approval"]["coordinates"]["operation_epoch"] = True
    elif change == "candidate": p["approval"]["plan"]["candidate"]["generation_id"] = "../secret"
    elif change == "actions": p["approval"]["plan"]["journal_actions"] = ["delete_user_files"]
    elif change == "missing_approval": del p["approval"]
    elif change == "workspace": p["repo_uuid"] = "22222222-2222-4222-8222-222222222222"
    else: p["operation"] = "preview"
    value = request(tmp_path.resolve(), p)
    # UUID equality is a runtime relation, beyond the schema's field shapes.
    if change != "workspace": assert list(schema_validator("cli-request.schema.json").iter_errors(value))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(cli, "_read_request", lambda _: canonical_json_bytes(value))
    monkeypatch.setattr(cli, "_run_bounded", lambda *a: pytest.fail("malformed repair executed"))
    assert cli.run_workspace_cli(["repair", "--request", "-"]) == 2
    output = capsys.readouterr()
    assert not output.out and "secret" not in output.err
    assert json.loads(output.err)["error_code"] == "bad_request"


@pytest.fixture
def repair_runtime(tmp_path, monkeypatch, capsys):
    """Create a disposable corrupt pointer and a public-entry repair client."""
    from graphify.workspace import composition
    from graphify.workspace.sync import synchronize_structural
    from tests.test_workspace_structural_s4 import runtime_fixture, request_for
    runtime, repo = runtime_fixture(tmp_path, monkeypatch)
    synchronize_structural(runtime, request_for(runtime, "gen-repair"), attempt_sha256="a" * 64)
    pointer_path = runtime.stores.pointers.state.path(runtime.stores.pointers._current(REPO_UUID))
    original = PointerSet.from_json(pointer_path.read_bytes()).to_dict()
    pointer_path.write_bytes(PointerSet.from_mapping(dict(original, source_epoch=99)).canonical)
    monkeypatch.setattr(composition, "compose_workspace_runtime",
        lambda inputs: SimpleNamespace(require_runtime=lambda: runtime))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def run(p=None, expected=0):
        """Invoke the CLI and check its exit, output channel, and response schema."""
        p = {"operation": "preview", "repo_uuid": REPO_UUID} if p is None else p
        value = request(runtime.inputs.state_root, p)
        monkeypatch.setattr(cli, "_read_request", lambda _: canonical_json_bytes(value))
        def dispatch(req, deadline):
            """Use the fixture runtime while preserving command error classification."""
            try:
                result = cli._execute(req.to_dict(), deadline)
                reply = response("repair", "s6-repair", result=result)
            except Exception as exc:
                reply = response("repair", "s6-repair", error_code=cli._error(exc,
                    mutation=not read_only_request(req.to_dict())))
            return reply, cli._exit_code(reply.to_dict()["error_code"])
        monkeypatch.setattr(cli, "_run_bounded", dispatch)
        capsys.readouterr()
        assert cli.run_workspace_cli(["repair", "--request", "-"]) == expected
        out = capsys.readouterr()
        assert not (out.err if expected == 0 else out.out)
        result = json.loads(out.out if expected == 0 else out.err)
        schema_validator("cli-response.schema.json").validate(result)
        return result["result"] if expected == 0 else result
    return runtime, repo, pointer_path, original, run


def identities(root):
    """Capture retained filesystem identities and metadata without following symlinks."""
    return {str(p.relative_to(root)): (p.lstat().st_dev, p.lstat().st_ino, p.lstat().st_mode,
        p.lstat().st_uid, p.lstat().st_gid, p.lstat().st_nlink, p.lstat().st_mtime_ns,
        p.lstat().st_ctime_ns) for p in (root, *root.rglob("*"))}


def test_preview_exact_approval_monotonic_repair_and_preservation(repair_runtime, monkeypatch):
    """Prove write-free preview, exact monotonic repair, and stale-approval refusal."""
    runtime, repo, pointer, original, run = repair_runtime
    state = runtime.inputs.state_root
    before = tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)
    # Intercept writes as well as checking final bytes; temporary writes cannot hide.
    import os
    with monkeypatch.context() as guard:
        real_open = os.open
        attempted = []
        def readonly_open(path, flags, *args, **kwargs):
            """Reject write-capable opens during preview, including temporary files."""
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                attempted.append((path, flags))
                raise AssertionError(attempted)
            return real_open(path, flags, *args, **kwargs)
        guard.setattr(os, "open", readonly_open)
        guard.setattr(os, "supports_dir_fd", os.supports_dir_fd | {readonly_open})
        def forbidden(*args, **kwargs):
            """Fail on any intercepted durable mutation during preview."""
            pytest.fail("preview attempted a durable write")
        for method in ("write", "replace", "replace_at", "rename_exclusive_at", "truncate",
                       "unlink", "unlink_at", "rmdir", "rmdir_at", "mkdir", "mkdir_at"):
            guard.setattr(runtime.stores.pointers.state.syscalls, method, forbidden)
        try:
            preview = run()["approval"]
            assert run()["approval"] == preview
        finally:
            assert not attempted, attempted
    assert (tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)) == before
    plan = preview["plan"]
    assert plan["decision_sha256"] == runtime.stores.pointers.analyze_repair(REPO_UUID,
        active_source_revision=1, operation_epoch=preview["coordinates"]["operation_epoch"] + 1,
        fence_token=preview["coordinates"]["fence_high_watermark"] + 1).decision_sha256
    assert str(repo) not in json.dumps(preview)
    generations = tree_snapshot(state / "workspaces" / REPO_UUID / "generations")
    locks = {p: data for p, data in identities(state).items() if p.endswith(".lock")}
    moved = run(execute_parameters(preview))["pointer"]
    assert moved["current"] == plan["candidate"] and moved["pointer_revision"] == original["pointer_revision"] + 1
    assert moved["source_epoch"] == 1 and json.loads(pointer.read_bytes()) == moved
    assert tree_snapshot(repo) == before[1]
    assert tree_snapshot(state / "workspaces" / REPO_UUID / "generations") == generations
    assert {p: data for p, data in identities(state).items() if p.endswith(".lock")} == locks
    after = tree_snapshot(state)
    assert run(execute_parameters(preview), expected=4)["error_code"] == "workspace_refused"
    assert tree_snapshot(state) == after
    noop = run()["approval"]
    assert noop["plan"]["classification"] == "no_op"
    before_lease = runtime.stores.leases.inspect(REPO_UUID)
    assert run(execute_parameters(noop))["pointer"] == moved
    after_lease = runtime.stores.leases.inspect(REPO_UUID)
    assert after_lease.operation_epoch == before_lease.operation_epoch + 1
    assert after_lease.fence_high_watermark == before_lease.fence_high_watermark + 1
    assert tree_snapshot(repo) == before[1]
    assert tree_snapshot(state / "workspaces" / REPO_UUID / "generations") == generations
    assert {p: data for p, data in identities(state).items() if p.endswith(".lock")} == locks
    after = tree_snapshot(state)
    assert run(execute_parameters(noop), expected=4)["error_code"] == "workspace_refused"
    assert tree_snapshot(state) == after


def test_tampered_approval_and_stale_raw_evidence_refuse_without_writes(repair_runtime):
    """Refuse changed approval fields or raw pointer evidence without writes."""
    runtime, repo, pointer, original, run = repair_runtime
    approved = run()["approval"]
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    for field in ("state_root_sha256", "compatibility_sha256", "active_source_sha256"):
        p = execute_parameters(approved); p["approval"][field] = "f" * 64
        assert run(p, expected=4)["error_code"] == "workspace_refused"
        assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before
    for field, value in (("decision_sha256", "f" * 64), ("next_pointer_revision", 55)):
        p = execute_parameters(approved); p["approval"]["plan"][field] = value
        assert run(p, expected=4)["error_code"] == "workspace_refused"
        assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before
    pointer.write_bytes(PointerSet.from_mapping(dict(original, source_epoch=98)).canonical)
    damaged = tree_snapshot(runtime.inputs.state_root)
    assert run(execute_parameters(approved), expected=4)["error_code"] == "workspace_refused"
    assert tree_snapshot(runtime.inputs.state_root) == damaged


def test_changed_evidence_after_acquisition_is_unknown_and_retains_lease(repair_runtime, monkeypatch):
    """Retain uncertain lease evidence when pointer authority changes after admission."""
    runtime, repo, pointer, original, run = repair_runtime
    approved = run()["approval"]
    acquire = runtime.stores.leases.acquire
    def changed(*a, **kw):
        """Change pointer evidence after the real repair lease is allocated."""
        grant = acquire(*a, **kw)
        pointer.write_bytes(PointerSet.from_mapping(dict(original, source_epoch=98)).canonical)
        return grant
    monkeypatch.setattr(runtime.stores.leases, "acquire", changed)
    assert run(execute_parameters(approved), expected=5)["error_code"] == "execution_unknown"
    assert runtime.stores.leases.inspect(REPO_UUID).leases
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    assert run(execute_parameters(approved), expected=4)["error_code"] == "workspace_refused"
    assert run(expected=4)["error_code"] == "workspace_refused"
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


@pytest.mark.parametrize("operation", ["preview", "execute"])
def test_uncertain_worker_protocol_obeys_operation(tmp_path, monkeypatch, capsys, operation):
    """Distinguish preview failure from uncertain execution on malformed worker output."""
    p = {"operation": "preview", "repo_uuid": REPO_UUID} if operation == "preview" else execute_parameters(approval_fixture())
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(cli, "_read_request", lambda _: canonical_json_bytes(request(tmp_path.resolve(), p)))
    def fail(*args):
        """Simulate a worker protocol error containing private diagnostic text."""
        raise cli._ProtocolError("private secret")
    monkeypatch.setattr(cli, "_run_bounded", fail)
    assert cli.run_workspace_cli(["repair", "--request", "-"]) == (4 if operation == "preview" else 5)
    output = capsys.readouterr()
    assert not output.out and "secret" not in output.err
    assert json.loads(output.err)["error_code"] == ("execution_failed" if operation == "preview" else "execution_unknown")


def test_failed_completion_is_unknown_even_after_pointer_move(repair_runtime, monkeypatch):
    """Report uncertainty when release fails after a visible pointer repair."""
    runtime, repo, pointer, original, run = repair_runtime
    approved = run()["approval"]
    def release(*a, **kw):
        """Simulate failure to acknowledge repair lease release."""
        raise OSError("private completion acknowledgment failure")
    monkeypatch.setattr(runtime.stores.leases, "release", release)
    assert run(execute_parameters(approved), expected=5)["error_code"] == "execution_unknown"
    moved = json.loads(pointer.read_bytes())
    assert moved["pointer_revision"] == original["pointer_revision"] + 1
    assert moved["current"] == approved["plan"]["candidate"]
    assert runtime.stores.leases.inspect(REPO_UUID).leases
    before = tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)
    assert run(execute_parameters(approved), expected=4)["error_code"] == "workspace_refused"
    assert (tree_snapshot(runtime.inputs.state_root), tree_snapshot(repo)) == before


@pytest.mark.parametrize("operation", ["preview", "execute"])
def test_preview_and_execute_keep_platform_refusal(tmp_path, monkeypatch, capsys, operation):
    """Preserve unsupported-runtime refusal and redaction for both operations."""
    from graphify.workspace.persistence import UnsupportedRuntime
    p = {"operation": "preview", "repo_uuid": REPO_UUID} if operation == "preview" else execute_parameters(approval_fixture())
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(cli, "_read_request", lambda _: canonical_json_bytes(request(tmp_path.resolve(), p)))
    def refused(*a):
        """Simulate an unsupported host with a private diagnostic."""
        raise UnsupportedRuntime("private host restriction")
    monkeypatch.setattr(cli, "_run_bounded", refused)
    assert cli.run_workspace_cli(["repair", "--request", "-"]) == 3
    output = capsys.readouterr()
    assert not output.out and "private" not in output.err
    assert json.loads(output.err)["error_code"] == "unsupported_runtime"


def test_repair_helper_and_schema_are_required_candidate_members():
    """Reject candidate manifests that omit either new repair runtime member."""
    from graphify.workspace.contracts import CompatibilityManifest, ContractError
    for member in ("graphify/workspace/repair.py", "graphify/workspace/schemas/repair-approval.schema.json"):
        candidate = compatibility().to_dict()
        assert member in candidate["package_members"]
        del candidate["package_members"][member]
        with pytest.raises(ContractError, match="missing structural package members"):
            CompatibilityManifest.from_mapping(candidate)


@pytest.mark.parametrize("operation", ["preview", "execute"])
def test_supervisor_refuses_wrong_success_binding(tmp_path, monkeypatch, operation):
    """Reject structurally valid worker results bound to different repair evidence."""
    import subprocess
    from graphify.workspace.cli_contracts import WorkspaceCommandRequest
    p = {"operation": "preview", "repo_uuid": REPO_UUID} if operation == "preview" else execute_parameters(approval_fixture())
    req = WorkspaceCommandRequest.from_mapping(request(tmp_path.resolve(), p))
    if operation == "preview":
        result = {"approval": approval_fixture()}
    else:
        result = {"pointer": {"contract": "graphify.workspace.pointer_set", "schema_version": 2,
            "repo_uuid": REPO_UUID, "pointer_revision": 99, "active_source_revision": 1,
            "source_epoch": 1, "operation_epoch": 3, "fence_token": 3, "state_schema_version": 2,
            "current": p["approval"]["plan"]["candidate"], "last_good": None}}
    raw = response("repair", "s6-repair", result=result).canonical
    real_popen = subprocess.Popen
    def worker(*args, **kwargs):
        # A real bounded worker writes canonical, structurally valid wrong evidence.
        """Launch a real bounded child that returns deliberately mismatched evidence."""
        return real_popen([sys.executable, "-I", "-S", "-B", "-c",
            "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(" + repr(raw) + ")"], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", worker)
    with pytest.raises(cli._ProtocolError, match="binding differs|acknowledgment differs"):
        cli._run_bounded(req, __import__("time").monotonic_ns() + 10_000_000_000)


def test_public_repair_retains_last_good_fallback_and_corrupt_content(repair_runtime):
    """Preserve source, lock identities, and corrupt bytes during last-good fallback."""
    from graphify.workspace.sync import prepare_structural_sync, synchronize_structural
    runtime, repo, pointer, original, run = repair_runtime
    pointer.write_bytes(PointerSet.from_mapping(original).canonical)
    prepared = prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id="gen-damaged",
        source_epoch=2, desired_watermark=2, expected_payload_bytes=1024 * 1024)
    synchronize_structural(runtime, prepared, attempt_sha256="b" * 64)
    state = runtime.inputs.state_root
    workspace = state / "workspaces" / REPO_UUID
    payload = workspace / "generations/gen-damaged/graphify-out/graph.json"
    payload.write_bytes(b"damaged graph content retained for inspection\n")
    lock = workspace / "locks/generations/gen-damaged.lock"
    lock_identity = lock.stat().st_dev, lock.stat().st_ino
    before = tree_snapshot(state), tree_snapshot(repo), identities(state)
    approval = run()["approval"]
    assert (tree_snapshot(state), tree_snapshot(repo), identities(state)) == before
    assert approval["plan"]["selected_from"] == "last_good"
    assert approval["plan"]["candidate"] == original["current"]
    assert approval["plan"]["quarantine"] == ["gen-damaged"]
    preserved = tree_snapshot(workspace / "generations/gen-repair")
    moved = run(execute_parameters(approval))["pointer"]
    assert moved["pointer_revision"] == 3 and moved["current"] == original["current"]
    assert tree_snapshot(workspace / "generations/gen-repair") == preserved
    assert tree_snapshot(repo) == before[1]
    assert (lock.stat().st_dev, lock.stat().st_ino) == lock_identity
    quarantined = list((workspace / "quarantine").rglob("graph.json"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == b"damaged graph content retained for inspection\n"


def install_semantic_work(queue_path, queue_state):
    """Install valid queued work or an empty semantic-required reconciliation."""
    import hashlib
    from graphify.workspace.semantic_queue import SemanticDesiredWork, SemanticQueueItem, SemanticQueueSnapshot
    value = json.loads(queue_path.read_bytes())
    reconciliation = value["reconciliation"]
    work = SemanticDesiredWork(reconciliation["source_epoch"], reconciliation["policy_sha256"],
        "UPSERT", "main.py", "c" * 64, value["desired_watermark"] + 1)
    value["revision"] += 1
    value["desired_watermark"] = work.desired_revision
    if queue_state == "queued":
        value["reconciliation"] = None
        value["items"] = [SemanticQueueItem(work, "pending", 0, None, None).to_dict()]
    else:
        reconciliation.update(desired_watermark=work.desired_revision, semantic_required=True,
            desired=[work.to_dict()],
            desired_set_sha256=hashlib.sha256(canonical_json_bytes([work.to_dict()])).hexdigest())
        value["completed_watermark"] = work.desired_revision
        value["items"] = []
    queue_path.write_bytes(SemanticQueueSnapshot.from_mapping(value).canonical)


@pytest.mark.parametrize("queue_state", ["queued", "required_empty"])
@pytest.mark.parametrize("timing", ["before_preview", "before_execute", "after_acquire"])
def test_semantic_work_blocks_public_repair(repair_runtime, monkeypatch, queue_state, timing):
    """Block fallback across semantic work at preview, execution, and locked recheck."""
    from graphify.workspace.sync import prepare_structural_sync, synchronize_structural
    runtime, repo, pointer, original, run = repair_runtime
    pointer.write_bytes(PointerSet.from_mapping(original).canonical)
    prepared = prepare_structural_sync(runtime, repo_uuid=REPO_UUID, generation_id="gen-damaged",
        source_epoch=2, desired_watermark=2, expected_payload_bytes=1024 * 1024)
    synchronize_structural(runtime, prepared, attempt_sha256="b" * 64)
    payload = runtime.inputs.state_root / "workspaces" / REPO_UUID / "generations/gen-damaged/graphify-out/graph.json"
    payload.write_bytes(b"damaged newer generation retained for inspection\n")
    approval = run()["approval"]
    assert approval["plan"]["selected_from"] == "last_good"
    assert approval["coordinates"]["source_epoch"] == 1
    state = runtime.inputs.state_root
    queue = runtime.stores.queue
    current, _, _ = queue._paths(REPO_UUID)
    queue_path = queue.state.path(current)
    if timing == "after_acquire":
        acquire = runtime.stores.leases.acquire
        held = []
        def changed(*args, **kwargs):
            """Install semantic work after acquisition and record the held recovery evidence."""
            grant = acquire(*args, **kwargs)
            install_semantic_work(queue_path, queue_state)
            held.append((tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)))
            return grant
        monkeypatch.setattr(runtime.stores.leases, "acquire", changed)
        assert run(execute_parameters(approval), expected=5)["error_code"] == "execution_unknown"
        assert (tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)) == held[0]
        assert runtime.stores.leases.inspect(REPO_UUID).leases
    else:
        install_semantic_work(queue_path, queue_state)
        before = tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)
        if timing == "before_preview":
            assert run(expected=4)["error_code"] == "workspace_refused"
            assert (tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)) == before
        assert run(execute_parameters(approval), expected=4)["error_code"] == "workspace_refused"
        assert (tree_snapshot(state), tree_snapshot(repo), identities(state), identities(repo)) == before
