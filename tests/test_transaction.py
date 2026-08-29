from __future__ import annotations

import json
import ast
import asyncio
import contextvars
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from graphify.transaction import (
    GRAPH_WATERMARK_KEY,
    MANAGED_PUBLICATION_PATHS,
    PendingTransactionError,
    RecoverableTransactionError,
    active_transaction_token_path,
    begin_transaction,
    claim_rebuild_queue,
    close_if_queue_empty,
    commit_bytes,
    commit_generation,
    commit_relative_bytes,
    complete_rebuild_claim,
    finish_transaction,
    load_detached_merge_snapshot,
    merge_detached_snapshots,
    open_graph_snapshot,
    owned_step,
    pin_output,
    queue_rebuild,
    recover_close,
    recover_transaction,
    resume_transaction,
    run_prepared_token,
    run_token,
    stage_transaction_handoff,
    takeover_drainer,
)


def _graph(generation: int, *, state: str = "active") -> bytes:
    return json.dumps(
        {
            "directed": False,
            "multigraph": False,
            "graph": {
                GRAPH_WATERMARK_KEY: {
                    "schema": 1,
                    "protocol_epoch": 1,
                    "generation": generation,
                    "state": state,
                }
            },
            "nodes": [],
            "links": [],
        },
        sort_keys=True,
    ).encode()


def _owner(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    tx = begin_transaction("runtime", root, output=output)
    token = stage_transaction_handoff(tx)
    return root, output, tx, token


def _commit_owner_generation(output: Path, tx) -> str:
    payload = _graph(tx.generation)
    with owned_step(tx):
        commit_bytes(tx, "graph.json", payload)
        commit_bytes(tx, "manifest.json", b"{}")
        return commit_generation(
            tx,
            graph_payload=payload,
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "manifest.json"),
        ).digest


def test_finish_rejects_substituted_receipt_with_zero_mutation(tmp_path):
    _root, output, tx, _token = _owner(tmp_path)
    _commit_owner_generation(output, tx)
    tx = resume_transaction(tx.id, _root, output=output)
    receipt_path = output / ".graphify_generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["transaction_id"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="receipt"):
        finish_transaction(tx)
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


def test_snapshot_rejects_deleted_required_artifact(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    _commit_owner_generation(output, tx)
    finish_transaction(resume_transaction(tx.id, root, output=output))
    (output / "manifest.json").unlink()
    with pytest.raises(PendingTransactionError, match="artifact|manifest"):
        open_graph_snapshot(output / "graph.json", purpose="deleted-artifact")


def test_claim_retry_is_idempotent_at_inflight_boundary(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    receipt = queue_rebuild("update", root, output=output, changed_paths=["a.py"])
    with pytest.raises(RuntimeError, match="crash"):
        claim_rebuild_queue(
            tx,
            receipt.drainer,
            failpoint=lambda point: (_ for _ in ()).throw(RuntimeError("crash"))
            if point == "before_quarantine_durable"
            else None,
        )
    claim = claim_rebuild_queue(tx, receipt.drainer)
    assert [item["id"] for item in claim.items] == [receipt.id]
    inflight = claim.inflight_path.read_text().splitlines() if claim.inflight_path else []
    assert len(inflight) == 1


def test_recovered_generation_and_drainer_converge_into_successor(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    recovered = recover_transaction("runtime", root, output=output, now=time.time() + 100)
    assert recovered.generation == recovered.drainer.generation
    _commit_owner_generation(output, recovered)
    finish_transaction(recovered)
    queued = queue_rebuild("update", root, output=output, changed_paths=["next.py"])
    successor = begin_transaction("runtime", root, output=output)
    assert successor.generation == queued.drainer.generation


def test_legacy_pending_bridge_retains_late_and_open_fd_appends(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module

    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    output.mkdir()
    legacy = output / ".pending_changes"
    legacy.write_text("first.py\n", encoding="utf-8")
    open_writer = legacy.open("a", encoding="utf-8")
    original = transaction_module._write_queue
    appended = False

    def append_during_bridge(capability, name, items):
        nonlocal appended
        original(capability, name, items)
        if name == transaction_module.QUEUE_FILE and not appended:
            appended = True
            open_writer.write("late-open-fd.py\n")
            open_writer.flush()
            os.fsync(open_writer.fileno())

    monkeypatch.setattr(transaction_module, "_write_queue", append_during_bridge)
    queue_rebuild(
        "update", root, output=output, legacy_pending_name=".pending_changes"
    )
    open_writer.close()
    with legacy.open("a", encoding="utf-8") as stream:
        stream.write("late-reopen.py\n")
    queue_rebuild(
        "update", root, output=output, legacy_pending_name=".pending_changes"
    )
    queued = [
        json.loads(line)
        for line in (output / ".graphify_rebuild_queue.jsonl").read_text().splitlines()
    ]
    durable_paths = {
        path for item in queued for path in (item.get("changed_paths") or [])
    }
    assert durable_paths >= {"first.py", "late-open-fd.py", "late-reopen.py"}
    assert legacy.exists(), "append-compatible bridge must retain the legacy inode"


def test_finish_refreshes_lease_before_close_releases_lock(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    _root, output, tx, _token = _owner(tmp_path)
    _commit_owner_generation(output, tx)
    tx = resume_transaction(tx.id, _root, output=output)
    original = transaction_module.close_if_queue_empty
    takeover_errors: list[Exception] = []

    def racing_close(transaction, *, receipt_digest, failpoint=None):
        try:
            takeover_drainer(output, now=time.time() + 1.0)
        except Exception as exc:  # noqa: BLE001 - exact race result asserted below
            takeover_errors.append(exc)
        return original(
            transaction, receipt_digest=receipt_digest, failpoint=failpoint
        )

    monkeypatch.setattr(transaction_module, "close_if_queue_empty", racing_close)
    finish_transaction(tx)
    assert len(takeover_errors) == 1
    assert "lease has not expired" in str(takeover_errors[0])


def test_takeover_rotates_token_and_installs_usable_successor_authority(tmp_path):
    root, output, tx, old_token = _owner(tmp_path)
    queued = queue_rebuild(
        "update", root, output=output, changed_paths=["successor.py"], now=0.0
    )
    claim_rebuild_queue(tx, queued.drainer, now=0.0)
    successor = takeover_drainer(output, now=100.0)
    successor_token = active_transaction_token_path(output)
    with pytest.raises(PendingTransactionError):
        run_token(old_token.path, ["-c", "pass"])
    queue_rebuild(
        "update", root, output=output, changed_paths=["after-takeover.py"], now=100.0
    )
    proof = tmp_path / "successor-claim.json"
    run_token(
        successor_token,
        [
            "-c",
            "import json; from pathlib import Path; "
            "from graphify.transaction import current_transaction, claim_rebuild_queue; "
            "tx=current_transaction(); claim=claim_rebuild_queue(tx, tx.drainer, now=100.0); "
            f"Path({str(proof)!r}).write_text(json.dumps(list(claim.items)))",
        ],
    )
    successor_items = json.loads(proof.read_text())
    assert any(
        "after-takeover.py" in (item.get("changed_paths") or [])
        for item in successor_items
    )


def test_finalize_requires_prepared_manifest(tmp_path):
    from graphify.transaction import finalize_prepared_transaction

    _root, output, _tx, token = _owner(tmp_path)
    (output / "graph.json").write_text('{"graph":{},"nodes":[],"links":[]}')
    with pytest.raises(PendingTransactionError, match="manifest"):
        run_token(
            token.path,
            [
                "-c",
                "from graphify.transaction import finalize_prepared_transaction; "
                "finalize_prepared_transaction()",
            ],
        )
    assert not (output / ".graphify_generation.json").exists()
    assert (output / ".graphify_transaction.json").exists()


def test_bootstrap_pending_is_the_first_protocol_byte_and_fences_reads(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    seen: list[set[str]] = []

    def after_first(capability, _state):
        seen.append({entry.name for entry in capability.path.iterdir()})
        with pytest.raises(PendingTransactionError, match="bootstrap"):
            open_graph_snapshot(output / "graph.json", purpose="test")
        raise RuntimeError("crash after first durable mutation")

    with pytest.raises(RuntimeError, match="first durable"):
        begin_transaction(
            "runtime", root, output=output, failpoint=after_first
        )
    assert seen == [{".graphify_protocol.json"}]
    protocol = json.loads((output / ".graphify_protocol.json").read_text())
    assert protocol["state"] == "BOOTSTRAP_PENDING"
    assert protocol["protocol_epoch"] == 1
    assert protocol["generation"] == 1
    assert protocol["owner_capability_digest"]
    assert protocol["output_identity"]


def test_concurrent_bootstrap_has_one_winner_and_bounded_takeover(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    barrier = threading.Barrier(2)
    results: list[object] = []

    def start():
        barrier.wait()
        try:
            results.append(begin_transaction("runtime", root, output=output))
        except Exception as exc:  # noqa: BLE001 - asserting exact race outcome
            results.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, PendingTransactionError) for value in results) == 1


def test_crashed_bootstrap_has_bounded_cas_takeover(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def crash_after_first(_capability, _protocol):
        raise RuntimeError("bootstrap crash")

    with pytest.raises(RuntimeError, match="bootstrap crash"):
        begin_transaction(
            "runtime", root, output=output, now=10.0, failpoint=crash_after_first
        )
    with pytest.raises(PendingTransactionError, match="lease"):
        recover_transaction("runtime", root, output=output, now=20.0)
    recovered = recover_transaction("runtime", root, output=output, now=41.0)
    protocol = json.loads((output / ".graphify_protocol.json").read_text())
    assert recovered.generation == 1
    assert recovered.drainer.claim_epoch == 1
    assert protocol["bootstrap_claim_epoch"] == 1
    assert protocol["state"] == "INCOMPLETE"


def test_token_content_and_stable_object_identity_are_both_required(tmp_path):
    root, output, tx, token = _owner(tmp_path)
    payload = token.path.read_bytes()
    token.path.unlink()
    token.path.write_bytes(payload)
    with pytest.raises(PendingTransactionError, match="owner|identity"):
        run_token(token.path, ["-c", "pass"])
    live = json.loads((output / ".graphify_transaction.json").read_text())
    assert live["token_digest"]
    assert live["token_identity"]
    assert live["generation"] == tx.generation


def test_stale_drainer_cannot_stage_handoff_token(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    tx = begin_transaction("runtime", root, output=output)
    transaction_before = (output / ".graphify_transaction.json").read_bytes()
    takeover_drainer(output, now=time.time() + 100.0)
    successor_before = (output / ".graphify_transaction.json").read_bytes()
    successor_tokens = list(output.glob(".graphify_transaction_token.*"))

    with pytest.raises(PendingTransactionError, match="drainer"):
        stage_transaction_handoff(tx)

    assert list(output.glob(".graphify_transaction_token.*")) == successor_tokens
    assert (output / ".graphify_transaction.json").read_bytes() != transaction_before
    assert (output / ".graphify_transaction.json").read_bytes() == successor_before
    live = json.loads((output / ".graphify_transaction.json").read_text())
    assert live["drainer"]["claim_epoch"] == tx.drainer.claim_epoch + 1


def test_token_cannot_install_successor_authority_during_takeover(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    errors: list[BaseException] = []
    code = (
        "from pathlib import Path; import time; "
        "from graphify.transaction import current_transaction, commit_bytes; "
        f"Path({str(entered)!r}).write_text('ready'); "
        f"gate=Path({str(release)!r}); "
        "\nwhile not gate.exists(): time.sleep(0.01)\n"
        "commit_bytes(current_transaction(), 'stale-token-publish', b'no')"
    )

    def runner():
        try:
            run_token(token.path, ["-c", code])
        except BaseException as exc:  # noqa: BLE001 - asserting interleaving result
            errors.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    for _ in range(200):
        if entered.exists():
            break
        time.sleep(0.01)
    assert entered.exists()
    takeover_drainer(output, now=time.time() + 100)
    release.write_text("go")
    thread.join(timeout=3)
    assert errors and isinstance(errors[0], PendingTransactionError)
    assert not (output / "stale-token-publish").exists()


def test_old_token_context_cannot_select_successor_drainer(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    takeover_drainer(output, now=time.time() + 100)
    with pytest.raises(PendingTransactionError, match="transaction id"):
        resume_transaction(tx.id, root, output=output)
    successor_token = active_transaction_token_path(output)
    run_token(
        successor_token,
        [
            "-c",
            "from graphify.transaction import current_transaction, owned_step; "
            "tx=current_transaction(); "
            "owned_step(tx, drainer=tx.drainer).__enter__()",
        ],
    )


def test_fresh_claim_lease_and_terminal_drainer_cannot_be_taken_over(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    queued = queue_rebuild(
        "update", root, output=output, changed_paths=["fresh.py"], now=10.0
    )
    claim_rebuild_queue(tx, queued.drainer, now=10.0)
    with pytest.raises(PendingTransactionError, match="lease"):
        takeover_drainer(output, now=20.0)

    live = resume_transaction(tx.id, root, output=output)
    _commit_owner_generation(output, live)
    finish_transaction(live)
    with pytest.raises(PendingTransactionError, match="state"):
        takeover_drainer(output, now=time.time() + 1000)


def test_prepared_workspace_retarget_preserves_replacement_sentinel(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    run_token(
        token.path,
        ["-c", "from graphify.transaction import prepared_workspace_path; prepared_workspace_path()"],
    )
    workspace = output.parent / f".graphify-prepare-{token.id}"
    workspace.rename(output.parent / "retired-original")
    workspace.mkdir()
    (workspace / "sentinel").write_text("replacement", encoding="utf-8")
    with pytest.raises(PendingTransactionError, match="identity|missing"):
        run_token(
            token.path,
            ["-c", "from graphify.transaction import finalize_prepared_transaction; finalize_prepared_transaction()"],
        )
    assert (workspace / "sentinel").read_text(encoding="utf-8") == "replacement"


def test_prepared_runner_executes_from_retained_workspace_after_rename(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    moved = tmp_path / "moved-prepared"
    replacement = output.parent / f".graphify-prepare-{token.id}"
    proof = tmp_path / "retained-proof"
    code = (
        "import os; from pathlib import Path; "
        f"workspace=Path({str(replacement)!r}); moved=Path({str(moved)!r}); "
        "workspace.rename(moved); workspace.mkdir(); "
        "(workspace/'sentinel').write_text('replacement'); "
        "Path('graphify-out/relative-proof').write_text('retained'); "
        f"Path({str(proof)!r}).write_text(str(os.stat('.').st_ino))"
    )
    run_prepared_token(token.path, ["-c", code])
    assert (moved / "graphify-out" / "relative-proof").read_text() == "retained"
    assert (replacement / "sentinel").read_text() == "replacement"
    assert int(proof.read_text()) == moved.stat().st_ino


def test_prepared_runner_rejects_swap_before_fchdir(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    _root, output, _tx, token = _owner(tmp_path)
    original = transaction_module._pin_prepared_workspace
    moved = tmp_path / "moved-before-fchdir"

    def swap_before_fchdir(transaction, capability):
        prepared = original(transaction, capability)
        prepared.path.rename(moved)
        prepared.path.mkdir()
        (prepared.path / "sentinel").write_text("replacement")
        return prepared

    monkeypatch.setattr(
        transaction_module, "_pin_prepared_workspace", swap_before_fchdir
    )
    with pytest.raises(PendingTransactionError):
        run_prepared_token(token.path, ["-c", "raise AssertionError('must not run')"])
    workspace = output.parent / f".graphify-prepare-{token.id}"
    assert (workspace / "sentinel").read_text() == "replacement"
    assert not (workspace / "graphify-out").exists()


def test_prepared_cost_is_receipt_bound_and_accumulates_across_generations(tmp_path):
    from graphify.transaction import finalize_prepared_transaction

    root, output, tx, token = _owner(tmp_path)
    first_cost = {
        "runs": [{"input_tokens": 3, "output_tokens": 2}],
        "total_input_tokens": 3,
        "total_output_tokens": 2,
    }
    run_prepared_token(
        token.path,
        [
            "-c",
            "import json; from pathlib import Path; "
            f"Path('graphify-out/graph.json').write_bytes({ _graph(tx.generation)!r}); "
            "Path('graphify-out/manifest.json').write_text('{}'); "
            f"Path('graphify-out/cost.json').write_text(json.dumps({first_cost!r}))",
        ],
    )
    run_token(
        token.path,
        ["-c", "from graphify.transaction import finalize_prepared_transaction; finalize_prepared_transaction()"],
    )
    first_receipt = json.loads((output / ".graphify_generation.json").read_text())
    assert "cost.json" in first_receipt["required_artifacts"]

    second = begin_transaction("full", root, output=output)
    second_token = stage_transaction_handoff(second)
    run_prepared_token(
        second_token.path,
        [
            "-c",
            "import json; from pathlib import Path; "
            "cost= json.loads(Path('graphify-out/cost.json').read_text()); "
            "cost['runs'].append({'input_tokens': 5, 'output_tokens': 7}); "
            "cost['total_input_tokens'] += 5; cost['total_output_tokens'] += 7; "
            "Path('graphify-out/cost.json').write_text(json.dumps(cost)); "
            f"Path('graphify-out/graph.json').write_bytes({_graph(second.generation)!r}); "
            "Path('graphify-out/manifest.json').write_text('{}')",
        ],
    )
    run_token(
        second_token.path,
        ["-c", "from graphify.transaction import finalize_prepared_transaction; finalize_prepared_transaction()"],
    )
    accumulated = json.loads((output / "cost.json").read_text())
    assert accumulated["total_input_tokens"] == 8
    assert accumulated["total_output_tokens"] == 9


def test_recovery_identity_retires_expired_prepared_workspace(tmp_path):
    root, output, _tx, token = _owner(tmp_path)
    run_prepared_token(token.path, ["-c", "from pathlib import Path; Path('proof').write_text('old')"])
    old_workspace = output.parent / f".graphify-prepare-{token.id}"
    recovered = recover_transaction(
        "full", root, output=output, now=time.time() + 100.0
    )
    assert not old_workspace.exists()
    assert list(output.parent.glob(f".graphify-retired-{token.id}-*"))
    recovered_token = stage_transaction_handoff(recovered)
    run_prepared_token(
        recovered_token.path,
        ["-c", "from pathlib import Path; Path('proof').write_text('new')"],
    )


def test_next_generation_retires_prepared_workspace_left_after_close(tmp_path):
    root, output, tx, token = _owner(tmp_path)
    run_prepared_token(token.path, ["-c", "from pathlib import Path; Path('proof').write_text('old')"])
    old_workspace = output.parent / f".graphify-prepare-{token.id}"
    _commit_owner_generation(output, tx)
    finish_transaction(resume_transaction(tx.id, root, output=output))
    assert old_workspace.exists(), "simulated crash occurs before prepared cleanup"
    begin_transaction("full", root, output=output)
    assert not old_workspace.exists()
    assert list(output.parent.glob(f".graphify-retired-{token.id}-*"))


def test_unexpired_unauthenticated_recovery_has_zero_mutation(tmp_path):
    root, output, tx, token = _owner(tmp_path)
    paths = {
        path.name: path.read_bytes()
        for path in output.iterdir()
        if path.is_file()
    }
    code = (
        "from pathlib import Path; from graphify.transaction import recover_transaction; "
        f"recover_transaction('runtime', {str(root)!r}, output=Path({str(output)!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "lease" in result.stderr
    assert token.path.exists()
    assert {
        path.name: path.read_bytes()
        for path in output.iterdir()
        if path.is_file()
    } == paths
    live = json.loads((output / ".graphify_transaction.json").read_text())
    assert live["id"] == tx.id
    assert live["generation"] == tx.generation


def test_transport_environment_never_grants_owner_context(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    code = (
        "from pathlib import Path; import os; "
        "from graphify.transaction import resume_transaction, owned_step, commit_bytes; "
        f"tx=resume_transaction(os.environ['GRAPHIFY_TRANSACTION_ID'], "
        f"os.environ['GRAPHIFY_TRANSACTION_ROOT'], output=Path({str(output)!r})); "
        "\nwith owned_step(tx): commit_bytes(tx, 'ambient', b'no')"
    )
    env = os.environ.copy()
    env.update(
        GRAPHIFY_TRANSACTION_ID=tx.id,
        GRAPHIFY_TRANSACTION_ROOT=str(root.resolve()),
        GRAPHIFY_TRANSACTION_TOKEN=str(_token.path),
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "owner context" in result.stderr
    assert not (output / "ambient").exists()


def test_resumed_transaction_without_token_cannot_claim_queue(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    queue_rebuild("update", root, output=output, changed_paths=["queued.py"])
    queue_before = (output / ".graphify_rebuild_queue.jsonl").read_bytes()
    code = (
        "from pathlib import Path; "
        "from graphify.transaction import resume_transaction, claim_rebuild_queue; "
        f"tx=resume_transaction({tx.id!r}, {str(root)!r}, output=Path({str(output)!r})); "
        "claim_rebuild_queue(tx, tx.drainer)"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "owner context" in result.stderr
    assert (output / ".graphify_rebuild_queue.jsonl").read_bytes() == queue_before
    assert not list(output.glob(".graphify_rebuild_inflight.*.jsonl"))


def test_runner_owner_context_is_thread_local(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    observed = tmp_path / "observed"
    code = (
        "import threading; from pathlib import Path; "
        "from graphify.transaction import current_transaction, commit_bytes; "
        "tx=current_transaction(); errors=[]; "
        f"\ndef child():\n try: commit_bytes(tx, 'thread-leak', b'no')\n except Exception as e: errors.append(type(e).__name__)\n"
        "\nt=threading.Thread(target=child); t.start(); t.join(); "
        f"Path({str(observed)!r}).write_text(errors[0])"
    )
    run_token(token.path, ["-c", code])
    assert observed.read_text() == "PendingTransactionError"
    assert not (output / "thread-leak").exists()


def test_exact_token_runner_can_publish_without_finishing_parent(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    run_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import current_transaction, commit_bytes; "
            "tx=current_transaction(); commit_bytes(tx, 'child-proof', b'ok')",
        ],
    )
    assert (output / "child-proof").read_bytes() == b"ok"
    assert (output / ".graphify_transaction.json").exists()


def test_recovery_revokes_old_token_and_rotates_generation(tmp_path):
    root, output, tx, old_token = _owner(tmp_path)
    recovered = recover_transaction(
        "runtime", root, output=output, now=time.time() + 100.0
    )
    new_token = stage_transaction_handoff(recovered)
    assert recovered.generation == tx.generation + 1
    assert new_token.path != old_token.path
    assert not old_token.path.exists()
    with pytest.raises(PendingTransactionError):
        run_token(old_token.path, ["-c", "pass"])
    run_token(new_token.path, ["-c", "pass"])


def test_pinned_posix_directory_replacement_is_rejected_and_untouched(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX dir-fd behavior")
    root, output, tx, _token = _owner(tmp_path)
    capability = pin_output(output)
    original = tmp_path / "original-output"
    output.rename(original)
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"replacement")
    with pytest.raises(PendingTransactionError, match="identity"):
        commit_bytes(tx, "graph.json", b"unsafe", capability=capability)
    assert sentinel.read_bytes() == b"replacement"
    assert not (output / "graph.json").exists()
    capability.close()


def test_finish_transaction_rejects_replacement_without_touching_it(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX directory replacement behavior")
    root, output, tx, _token = _owner(tmp_path)
    payload = _graph(tx.generation)
    with owned_step(tx):
        commit_bytes(tx, "graph.json", payload)
        commit_bytes(tx, "manifest.json", b"{}")
        commit_generation(
            tx,
            graph_payload=payload,
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "manifest.json"),
        )
    original = tmp_path / "original-output"
    output.rename(original)
    output.mkdir()
    (output / ".graphify_generation.json").write_bytes(
        (original / ".graphify_generation.json").read_bytes()
    )
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"replacement")

    with pytest.raises(PendingTransactionError, match="owner|identity"):
        finish_transaction(tx)

    assert sentinel.read_bytes() == b"replacement"
    assert {entry.name for entry in output.iterdir()} == {
        ".graphify_generation.json",
        "sentinel",
    }


def test_recovery_waits_for_leaf_commit_then_revokes_next_publication(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    recovered: list[object] = []

    def pause(name: str):
        if name == "after_validate":
            entered.set()
            assert release.wait(timeout=3)

    def publish():
        commit_relative_bytes(
            tx, "wiki/index.md", b"first", failpoint=pause
        )

    owner_context = contextvars.copy_context()
    publisher = threading.Thread(target=lambda: owner_context.run(publish))
    publisher.start()
    assert entered.wait(timeout=3)
    recovery = threading.Thread(
        target=lambda: recovered.append(
            recover_transaction(
                "runtime", root, output=output, now=time.time() + 100.0
            )
        )
    )
    recovery.start()
    recovery.join(timeout=0.05)
    assert recovery.is_alive(), "recovery must block behind the leaf commit"
    release.set()
    publisher.join(timeout=3)
    recovery.join(timeout=3)
    assert (output / "wiki" / "index.md").read_bytes() == b"first"
    assert recovered
    with pytest.raises(PendingTransactionError):
        commit_relative_bytes(tx, "wiki/next.md", b"stale")
    assert not (output / "wiki" / "next.md").exists()


def test_executable_managed_publication_inventory_uses_owner_primitives(tmp_path):
    _root, output, tx, _token = _owner(tmp_path)
    with owned_step(tx):
        for relative in MANAGED_PUBLICATION_PATHS:
            if "/" in relative:
                commit_relative_bytes(tx, relative, relative.encode())
            else:
                commit_bytes(tx, relative, relative.encode())
    for relative in MANAGED_PUBLICATION_PATHS:
        assert (output / relative).read_bytes() == relative.encode()


def test_stale_drainer_cannot_publish_acknowledge_or_close(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    receipt = queue_rebuild(
        "update", root, output=output, changed_paths=["a.py"], now=0.0
    )
    claim = claim_rebuild_queue(tx, receipt.drainer, now=0.0)
    with owned_step(tx, drainer=claim.drainer):
        takeover = takeover_drainer(output, now=100.0, lease_seconds=1.0)
        assert takeover.claim_epoch == claim.drainer.claim_epoch + 1
        for operation in (
            lambda: commit_bytes(tx, "stale", b"no"),
            lambda: complete_rebuild_claim(tx, claim, receipt_digest="0" * 64),
            lambda: close_if_queue_empty(tx, receipt_digest="0" * 64),
        ):
            with pytest.raises(PendingTransactionError, match="drainer"):
                operation()
    assert not (output / "stale").exists()


@pytest.mark.parametrize("boundary", ["inflight", "quarantine", "queue"])
def test_claim_source_survives_until_replacement_is_durable(tmp_path, boundary):
    root, output, tx, _token = _owner(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    receipt = queue_rebuild(
        "update", root, output=output, changed_paths=["a.py"], now=0.0
    )
    queue_rebuild("full", other, output=output, now=0.0)

    def failpoint(name: str):
        if name == f"before_{boundary}_durable":
            raise OSError(boundary)

    with pytest.raises(OSError, match=boundary):
        claim_rebuild_queue(tx, receipt.drainer, now=0.0, failpoint=failpoint)
    queued = (output / ".graphify_rebuild_queue.jsonl").read_text()
    assert "a.py" in queued
    assert str(other.resolve()) in queued


def test_duplicate_queue_intent_ids_fail_closed(tmp_path):
    root, output, _tx, _token = _owner(tmp_path)
    intent = {
        "schema": 1,
        "id": "a" * 64,
        "kind": "update",
        "intent": "update",
        "root": str(root.resolve()),
        "changed_paths": ["a.py"],
        "semantic": False,
        "source": "test",
        "time": 0.0,
    }
    queue = output / ".graphify_rebuild_queue.jsonl"
    line = json.dumps(intent, sort_keys=True) + "\n"
    queue.write_text(line + line, encoding="utf-8")
    with pytest.raises(PendingTransactionError, match="duplicate"):
        queue_rebuild("update", root, output=output, changed_paths=["b.py"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX capability lock behavior")
def test_queue_rebuild_subprocess_race_preserves_every_intent(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    output.mkdir()
    gate = tmp_path / "start"
    script = r'''
import os
import time
from pathlib import Path
from graphify import transaction as tx

root = Path(os.environ["RACE_ROOT"])
output = Path(os.environ["RACE_OUTPUT"])
gate = Path(os.environ["RACE_GATE"])
name = os.environ["RACE_NAME"]
original = tx._replace_bytes

def synchronized_replace(capability, entry, payload):
    if entry == tx.QUEUE_FILE:
        (gate.parent / ("ready-" + name)).write_text("ready")
        deadline = time.time() + 0.75
        while len(list(gate.parent.glob("ready-*"))) < 2 and time.time() < deadline:
            time.sleep(0.005)
    return original(capability, entry, payload)

tx._replace_bytes = synchronized_replace
while not gate.exists():
    time.sleep(0.005)
tx.queue_rebuild("update", root, output=output, changed_paths=[name])
'''
    processes = []
    for name in ("one.py", "two.py"):
        env = os.environ.copy()
        env.update(
            RACE_ROOT=str(root),
            RACE_OUTPUT=str(output),
            RACE_GATE=str(gate),
            RACE_NAME=name,
        )
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parent.parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    gate.write_text("go")
    results = [process.communicate(timeout=5) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    queued = [
        json.loads(line)
        for line in (output / ".graphify_rebuild_queue.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {tuple(item["changed_paths"]) for item in queued} == {
        ("one.py",),
        ("two.py",),
    }


def test_manifest_failure_prevents_receipt_ack_and_close(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    receipt = queue_rebuild("update", root, output=output, changed_paths=["a.py"])
    claim = claim_rebuild_queue(tx, receipt.drainer)
    with owned_step(tx, drainer=claim.drainer):
        graph_payload = _graph(tx.generation)
        commit_bytes(tx, "graph.json", graph_payload)
        with pytest.raises(OSError, match="manifest"):
            raise OSError("manifest persistence failed")
        with pytest.raises(PendingTransactionError, match="receipt"):
            complete_rebuild_claim(tx, claim, receipt_digest="")
    assert claim.inflight_path is not None and claim.inflight_path.exists()
    assert (output / ".graphify_transaction.json").exists()


def test_generation_receipt_is_last_and_watermark_prevents_legacy_downgrade(tmp_path):
    _root, output, tx, _token = _owner(tmp_path)
    payload = _graph(tx.generation)
    with owned_step(tx):
        commit_bytes(tx, "graph.json", payload)
        commit_bytes(tx, "manifest.json", b"{}")
        with pytest.raises(PendingTransactionError, match="receipt"):
            open_graph_snapshot(output / "graph.json", purpose="test")
        receipt = commit_generation(
            tx,
            graph_payload=payload,
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "manifest.json"),
        )
    snapshot = open_graph_snapshot(output / "graph.json", purpose="test")
    assert snapshot.generation == tx.generation
    assert receipt.digest
    for sidecar in output.iterdir():
        if sidecar.name != "graph.json" and sidecar.is_file():
            sidecar.unlink()
    with pytest.raises(PendingTransactionError, match="receipt"):
        open_graph_snapshot(output / "graph.json", purpose="test")


def test_legacy_graph_without_any_protocol_marker_remains_readable(tmp_path):
    output = tmp_path / "graphify-out"
    output.mkdir()
    graph = output / "graph.json"
    graph.write_text('{"directed":false,"multigraph":false,"graph":{},"nodes":[],"links":[]}')
    snapshot = open_graph_snapshot(graph, purpose="legacy")
    assert snapshot.generation is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory retarget behavior")
def test_legacy_marker_fence_is_discovered_through_pinned_directory(
    tmp_path, monkeypatch
):
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_bytes(
        b'{"directed":false,"multigraph":false,"graph":{},"nodes":[],"links":[]}'
    )
    (output / ".graphify_protocol.json").write_text(
        '{"state":"INCOMPLETE"}', encoding="utf-8"
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "graph.json").write_bytes((output / "graph.json").read_bytes())
    original = tmp_path / "original-output"
    real_lexists = os.path.lexists
    calls = 0

    def retargeting_lexists(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            output.rename(original)
            replacement.rename(output)
        result = real_lexists(path)
        if calls == 5:
            output.rename(replacement)
            original.rename(output)
        return result

    monkeypatch.setattr(os.path, "lexists", retargeting_lexists)
    with pytest.raises(PendingTransactionError, match="protocol"):
        open_graph_snapshot(output / "graph.json", purpose="retarget-test")
    assert calls == 0


def test_close_pending_recovery_and_racing_enqueue_create_successor(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    intent = queue_rebuild("update", root, output=output, changed_paths=["a.py"])
    claim = claim_rebuild_queue(tx, intent.drainer)
    with owned_step(tx, drainer=claim.drainer):
        payload = _graph(tx.generation)
        commit_bytes(tx, "graph.json", payload)
        commit_bytes(tx, "manifest.json", b"{}")
        generation = commit_generation(
            tx,
            graph_payload=payload,
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "manifest.json"),
        )
        complete_rebuild_claim(tx, claim, receipt_digest=generation.digest)

        def failpoint(name: str):
            if name == "after_close_pending":
                raise RuntimeError(name)

        with pytest.raises(RuntimeError, match="close_pending"):
            close_if_queue_empty(tx, receipt_digest=generation.digest, failpoint=failpoint)
    late = queue_rebuild("update", root, output=output, changed_paths=["late.py"])
    recover_close(output)
    assert late.drainer.generation == claim.drainer.generation + 1
    drainer = json.loads((output / ".graphify_drainer.json").read_text())
    assert drainer["state"] == "reserved"
    assert drainer["generation"] == late.drainer.generation


@pytest.mark.parametrize(
    "boundary",
    [
        "after_close_pending",
        "after_inflight_remove",
        "after_token_unlink",
        "after_live_remove",
        "after_complete",
    ],
)
def test_close_recovery_converges_after_each_durable_boundary(tmp_path, boundary):
    root, output, tx, _token = _owner(tmp_path)
    intent = queue_rebuild("update", root, output=output, changed_paths=["a.py"])
    claim = claim_rebuild_queue(tx, intent.drainer)
    with owned_step(tx, drainer=claim.drainer):
        payload = _graph(tx.generation)
        commit_bytes(tx, "graph.json", payload)
        commit_bytes(tx, "manifest.json", b"{}")
        generation = commit_generation(
            tx,
            graph_payload=payload,
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "manifest.json"),
        )
        complete_rebuild_claim(tx, claim, receipt_digest=generation.digest)

        def failpoint(name: str):
            if name == boundary:
                raise RuntimeError(name)

        with pytest.raises(RuntimeError, match=boundary):
            close_if_queue_empty(
                tx, receipt_digest=generation.digest, failpoint=failpoint
            )
    recover_close(output)
    drainer = json.loads((output / ".graphify_drainer.json").read_text())
    assert drainer["state"] == "complete"
    assert not (output / ".graphify_transaction.json").exists()


def test_recovery_is_bounded_and_preserves_intent(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    queue_rebuild("update", root, output=output, changed_paths=["a.py"], now=0.0)
    with pytest.raises(RecoverableTransactionError, match="attempt"):
        recover_transaction(
            "runtime", root, output=output, now=0.0, max_attempts=0
        )
    assert (output / ".graphify_rebuild_queue.jsonl").exists()
    assert (output / ".graphify_transaction.json").exists()


def test_detached_merge_parser_is_private_data_only_and_marks_pending(tmp_path):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_bytes(_graph(7))
    detached = load_detached_merge_snapshot(current, role="current")
    assert isinstance(detached, dict)
    assert not hasattr(detached, "capability")
    merge_detached_snapshots(base, current, other)
    merged = json.loads(current.read_text())
    watermark = merged["graph"][GRAPH_WATERMARK_KEY]
    assert watermark["state"] == "merge_pending"
    assert watermark["input_digests"]
    managed = tmp_path / "graphify-out"
    managed.mkdir()
    (managed / "graph.json").write_bytes(current.read_bytes())
    with pytest.raises(PendingTransactionError, match="merge_pending"):
        open_graph_snapshot(managed / "graph.json", purpose="post-merge")
    from graphify.affected import load_graph as load_affected
    from graphify.build import build_merge
    from graphify.prs import _load_graph_json
    from graphify.serve import _load_graph as load_server
    from graphify.tree_html import write_tree_html

    for operation in (
        lambda: load_affected(managed / "graph.json"),
        lambda: load_server(str(managed / "graph.json")),
        lambda: build_merge([], graph_path=managed / "graph.json"),
        lambda: _load_graph_json(managed / "graph.json"),
        lambda: write_tree_html(
            managed / "graph.json", tmp_path / "post-merge-tree.html"
        ),
    ):
        with pytest.raises((PendingTransactionError, RuntimeError)):
            operation()
    assert not (tmp_path / "post-merge-tree.html").exists()

    with pytest.raises(PendingTransactionError, match="role"):
        load_detached_merge_snapshot(current, role="working-tree")


def test_detached_merge_rejects_oversize_and_unsupported_watermark(tmp_path):
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    with pytest.raises(PendingTransactionError, match="unsafe"):
        load_detached_merge_snapshot(oversized, role="current")

    unsupported = tmp_path / "unsupported.json"
    payload = json.loads(_graph(1))
    payload["graph"][GRAPH_WATERMARK_KEY]["state"] = "future"
    unsupported.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PendingTransactionError, match="watermark"):
        load_detached_merge_snapshot(unsupported, role="current")


def test_detached_merge_rejects_more_than_one_hundred_thousand_nodes(tmp_path):
    oversized = tmp_path / "too-many-nodes.json"
    payload = json.loads(_graph(1))
    payload["nodes"] = [{"id": str(index)} for index in range(100_001)]
    oversized.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PendingTransactionError, match="node count"):
        load_detached_merge_snapshot(oversized, role="current")


def test_detached_merge_preserves_multigraph_parallel_edges(tmp_path):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    template = json.loads(_graph(1))
    template.update(multigraph=True)
    template["nodes"] = [{"id": "a"}, {"id": "b"}]
    base.write_text(json.dumps(template), encoding="utf-8")
    current_payload = dict(template)
    current_payload["links"] = [
        {"source": "a", "target": "b", "key": "current", "kind": "one"}
    ]
    current.write_text(json.dumps(current_payload), encoding="utf-8")
    other_payload = dict(template)
    other_payload["links"] = [
        {"source": "a", "target": "b", "key": "other", "kind": "two"}
    ]
    other.write_text(json.dumps(other_payload), encoding="utf-8")
    merge_detached_snapshots(base, current, other)
    merged = json.loads(current.read_text())
    assert {(edge["key"], edge["kind"]) for edge in merged["links"]} == {
        ("current", "one"),
        ("other", "two"),
    }


def test_detached_merge_checks_identity_inside_final_replace(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_bytes(_graph(3))
    original_replace = transaction_module._replace_bytes
    swapped = False

    def swap_at_replace(capability, name, payload, **kwargs):
        nonlocal swapped
        if name == current.name and kwargs.get("expected_identity") and not swapped:
            swapped = True
            current.rename(tmp_path / "original-at-final-replace.json")
            current.write_bytes(b"replacement")
        return original_replace(capability, name, payload, **kwargs)

    monkeypatch.setattr(transaction_module, "_replace_bytes", swap_at_replace)
    with pytest.raises(PendingTransactionError, match="identity"):
        merge_detached_snapshots(base, current, other)
    assert current.read_bytes() == b"replacement"


def test_detached_merge_refuses_retargeted_current_snapshot(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module

    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_bytes(_graph(3))
    original = transaction_module._load_detached_merge_snapshot_with_identity
    replaced = False

    def retarget(path, *, role):
        nonlocal replaced
        result = original(path, role=role)
        if role == "current" and not replaced:
            replaced = True
            current.rename(tmp_path / "original-current.json")
            current.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(
        transaction_module, "_load_detached_merge_snapshot_with_identity", retarget
    )
    with pytest.raises(PendingTransactionError, match="identity"):
        merge_detached_snapshots(base, current, other)
    assert current.read_bytes() == b"replacement"


def test_merge_driver_cli_accepts_exact_three_snapshots_only(tmp_path):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_bytes(_graph(3))
    command = [
        sys.executable,
        "-m",
        "graphify",
        "merge-driver",
        str(base),
        str(current),
        str(other),
    ]
    accepted = subprocess.run(command, capture_output=True, text=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(current.read_text())["graph"][GRAPH_WATERMARK_KEY][
        "state"
    ] == "merge_pending"
    rejected = subprocess.run(
        [*command, str(tmp_path / "fourth.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0


def test_unknown_watermark_schema_fails_closed(tmp_path):
    output = tmp_path / "graphify-out"
    output.mkdir()
    payload = json.loads(_graph(1))
    payload["graph"][GRAPH_WATERMARK_KEY]["schema"] = 99
    graph = output / "graph.json"
    graph.write_text(json.dumps(payload))
    with pytest.raises(PendingTransactionError, match="watermark"):
        open_graph_snapshot(graph, purpose="future")


def test_representative_library_and_server_readers_reject_pending_state(tmp_path):
    from graphify.affected import load_graph as load_affected
    from graphify.build import build_merge
    from graphify.prs import _load_graph_json
    from graphify.serve import _load_graph as load_server
    from graphify.tree_html import write_tree_html

    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    output.mkdir()
    graph = output / "graph.json"
    graph.write_text(
        '{"directed":false,"multigraph":false,"graph":{},"nodes":[],"links":[]}',
        encoding="utf-8",
    )
    begin_transaction("runtime", root, output=output)
    for name, operation in (
        ("affected", lambda: load_affected(graph)),
        ("server-loader", lambda: load_server(str(graph))),
        ("build-merge", lambda: build_merge([], graph_path=graph)),
        ("prs", lambda: _load_graph_json(graph)),
        ("tree-html", lambda: write_tree_html(graph, tmp_path / "tree.html")),
    ):
        try:
            operation()
        except (PendingTransactionError, RuntimeError):
            continue
        pytest.fail(f"{name} consumed a pending managed graph")
    assert not (tmp_path / "tree.html").exists()


def test_stdio_mcp_tool_rejects_pending_managed_graph(tmp_path):
    types = pytest.importorskip("mcp.types")
    from graphify.serve import _build_server

    output = tmp_path / "graphify-out"
    output.mkdir()
    graph = output / "graph.json"
    graph.write_text('{"nodes":[],"edges":[]}', encoding="utf-8")
    begin_transaction("runtime", tmp_path, output=output)
    server = _build_server(str(graph))
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(name="graph_stats", arguments={})
    )
    response = asyncio.run(handler(request))
    assert "without a graph receipt" in str(response)


def test_windows_adapter_is_guarantee_or_explicit_blocker(monkeypatch, tmp_path):
    import graphify.transaction as transaction_module

    monkeypatch.setattr(transaction_module, "_PLATFORM", "windows")
    output = tmp_path / "graphify-out"
    output.mkdir()
    with pytest.raises(PendingTransactionError, match="Windows.*non-retargetable"):
        pin_output(output)


@pytest.mark.parametrize(
    "module_name",
    [
        "affected.py",
        "benchmark.py",
        "build.py",
        "callflow_html.py",
        "cli.py",
        "global_graph.py",
        "prs.py",
        "reflect.py",
        "serve.py",
        "tree_html.py",
        "watch.py",
    ],
)
def test_managed_reader_inventory_uses_canonical_snapshot_boundary(module_name):
    source = (Path(__file__).parents[1] / "graphify" / module_name).read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open_graph_snapshot" in calls, module_name
