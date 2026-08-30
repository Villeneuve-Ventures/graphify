from __future__ import annotations

import json
import ast
import asyncio
import contextvars
import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from graphify.transaction import (
    CancellationRecovery,
    GRAPH_WATERMARK_KEY,
    MANAGED_PUBLICATION_PATHS,
    OutputIdentity,
    PendingTransactionError,
    RecoverableTransactionError,
    active_transaction_token_path,
    begin_transaction,
    cancel_unpublished_transaction,
    claim_rebuild_queue,
    close_if_queue_empty,
    commit_bytes,
    commit_generation,
    commit_prepared_bytes,
    commit_relative_bytes,
    commit_unlink,
    complete_rebuild_claim,
    finish_transaction,
    gc_retired_workspaces,
    load_detached_merge_snapshot,
    merge_detached_snapshots,
    open_graph_snapshot,
    owned_step,
    pin_output,
    queue_rebuild,
    recover_close,
    recover_transaction,
    recover_selected_transaction,
    resume_transaction,
    run_prepared_token,
    run_token,
    stage_transaction_handoff,
    takeover_drainer,
    transaction_status,
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
    tx = resume_transaction(tx.id, root, output=output)
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


def _file_bytes(output: Path) -> dict[str, bytes]:
    return {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }


def _corrupt_generation_receipt(output: Path, corruption: str) -> None:
    receipt_path = output / ".graphify_generation.json"
    if corruption == "missing":
        receipt_path.unlink()
    elif corruption == "malformed":
        receipt_path.write_bytes(b"{")
    elif corruption == "generation":
        receipt = json.loads(receipt_path.read_text())
        receipt["generation"] += 1
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        )
    else:
        raise AssertionError(f"unsupported receipt corruption: {corruption}")


def _close_pending_after_failpoint(tmp_path: Path) -> tuple[Path, Path]:
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

        def failpoint(name: str) -> None:
            if name == "after_close_pending":
                raise RuntimeError(name)

        with pytest.raises(RuntimeError, match="after_close_pending"):
            close_if_queue_empty(
                tx,
                receipt_digest=generation.digest,
                failpoint=failpoint,
            )
    return root, output


def test_bootstrap_create_interruption_never_exposes_partial_protocol(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    real_write = transaction_module.os.write
    writes = 0

    def interrupted_write(fd, payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, payload[: max(1, len(payload) // 2)])
        raise OSError("simulated bootstrap interruption")

    monkeypatch.setattr(transaction_module.os, "write", interrupted_write)
    with pytest.raises(OSError, match="interruption"):
        begin_transaction("runtime", root, output=output)
    assert not (output / ".graphify_protocol.json").exists()
    assert not list(output.glob("..graphify_protocol.json.*.tmp"))


def test_publication_rejects_missing_or_nonclaimed_durable_drainer(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    tx = resume_transaction(tx.id, root, output=output)
    drainer_path = output / ".graphify_drainer.json"
    drainer = json.loads(drainer_path.read_text())
    drainer["state"] = "launching"
    drainer.pop("acked_ids")
    drainer_path.write_text(json.dumps(drainer))
    with pytest.raises(PendingTransactionError, match="claimed"):
        commit_bytes(tx, "GRAPH_REPORT.md", b"blocked")
    assert not (output / "GRAPH_REPORT.md").exists()


def test_bootstrap_recovery_bound_is_checked_before_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def stop_after_bootstrap(_capability, _protocol):
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        begin_transaction("runtime", root, output=output, now=0.0, failpoint=stop_after_bootstrap)
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["bootstrap_claim_epoch"] = 3
    protocol_path.write_text(json.dumps(protocol, sort_keys=True, separators=(",", ":")))
    before = protocol_path.read_bytes()
    with pytest.raises(RecoverableTransactionError, match="attempt"):
        recover_transaction("runtime", root, output=output, now=100.0, max_attempts=3)
    assert protocol_path.read_bytes() == before


def test_receipt_validation_enforces_aggregate_budget_without_retaining_all(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    _root, output, tx, _token = _owner(tmp_path)
    graph_payload = _graph(tx.generation)
    with owned_step(tx):
        commit_bytes(tx, "graph.json", graph_payload)
        commit_bytes(tx, "manifest.json", b"{}")
        commit_bytes(tx, "large.bin", b"x" * 32)
        commit_generation(
            tx,
            graph_payload=graph_payload,
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "large.bin", "manifest.json"),
        )
    monkeypatch.setattr(transaction_module, "_MAX_RECEIPT_AGGREGATE_BYTES", 16)
    with pytest.raises(PendingTransactionError, match="aggregate budget"):
        open_graph_snapshot(output / "graph.json", purpose="aggregate-bound")


def test_transaction_status_is_read_only_and_omits_capability_material(tmp_path):
    _root, output, tx, _token = _owner(tmp_path)
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    status = transaction_status(output)
    after = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    assert status["transaction"]["id"] == tx.id
    assert "token_digest" not in json.dumps(status)
    assert "launch_nonce" not in json.dumps(status)
    assert before == after


def test_windows_read_adapter_admits_safe_legacy_snapshot_but_mutation_stays_blocked(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module

    output = tmp_path / "graphify-out"
    output.mkdir()
    graph = output / "graph.json"
    graph.write_text(
        '{"directed":false,"multigraph":false,"graph":{},"nodes":[],"links":[]}'
    )
    monkeypatch.setattr(transaction_module, "_PLATFORM", "windows")
    monkeypatch.setattr(
        transaction_module,
        "_open_windows_read_directory",
        lambda path: os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
    )
    monkeypatch.setattr(
        transaction_module,
        "_open_windows_relative_fd",
        lambda capability, name, **_kwargs: os.open(capability.path / name, os.O_RDONLY),
    )
    assert open_graph_snapshot(graph, purpose="windows-read").data["nodes"] == []
    with pytest.raises(PendingTransactionError, match="final mutation"):
        pin_output(output)


def test_windows_handle_relative_model_preserves_top_level_and_nested_identity(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module

    output = tmp_path / "graphify-out"
    nested = output / "wiki"
    nested.mkdir(parents=True)
    top = output / "graph.json"
    leaf = nested / "index.md"
    top.write_bytes(b"original-top")
    leaf.write_bytes(b"original-nested")
    with pin_output(output, mutation=False) as capability:
        replaced: set[str] = set()

        def stable_relative_open(_capability, name, **_kwargs):
            target = output / name
            fd = os.open(target, os.O_RDONLY)
            if name not in replaced:
                replaced.add(name)
                replacement = target.with_name(target.name + ".replacement")
                replacement.write_bytes(b"replacement")
                os.replace(replacement, target)
            return fd

        monkeypatch.setattr(transaction_module, "_PLATFORM", "windows")
        monkeypatch.setattr(
            transaction_module, "_open_windows_relative_fd", stable_relative_open
        )
        assert transaction_module._read_bytes(capability, "graph.json") == b"original-top"
        digest, size, payload = transaction_module._hash_windows_relative(
            capability,
            "wiki/index.md",
            retain=True,
            aggregate_remaining=1024,
        )
    assert payload == b"original-nested"
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert top.read_bytes() == b"replacement"
    assert leaf.read_bytes() == b"replacement"


def test_queue_rejects_oversized_serialized_intent_before_sidecar_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    paths = [f"{index:04d}-" + "x" * 4088 for index in range(300)]
    with pytest.raises(PendingTransactionError, match="serialized budget"):
        queue_rebuild("update", root, output=output, changed_paths=paths)
    assert _file_bytes(output) == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("id", "not-hex"),
        ("changed_paths", ["ok.py", 1]),
        ("semantic", 1),
        ("time", True),
    ],
)
def test_queue_rejects_exact_type_corruption_without_new_mutation(
    tmp_path, field, value
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    queue_rebuild("update", root, output=output, changed_paths=["a.py"])
    queue_path = output / ".graphify_rebuild_queue.jsonl"
    item = json.loads(queue_path.read_text())
    item[field] = value
    queue_path.write_text(json.dumps(item, separators=(",", ":")) + "\n")
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="malformed rebuild queue"):
        transaction_status(output)
    assert _file_bytes(output) == before


def test_invalid_runner_target_clears_process_authority(tmp_path):
    import graphify.transaction as transaction_module

    _root, _output, _tx, token = _owner(tmp_path)
    with pytest.raises(PendingTransactionError, match="ambiguous"):
        run_token(token.path, ["-m", "unsafe/module"])
    assert transaction_module._AUTHORITY.get() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("protocol_epoch", True),
        ("generation", 0),
        ("kind", "unknown"),
        ("phase", "closing"),
        ("root", "relative-root"),
        ("output", "relative-output"),
    ],
)
def test_live_transaction_corruption_is_zero_new_mutation(tmp_path, field, value):
    _root, output, tx, _token = _owner(tmp_path)
    path = output / ".graphify_transaction.json"
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="live transaction"):
        commit_bytes(tx, "proof", b"forbidden")
    assert _file_bytes(output) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("protocol_epoch", True),
        ("generation", 0),
        ("kind", "unknown"),
        ("state", "closing"),
        ("root", "relative-root"),
        ("owner_capability_digest", "not-a-digest"),
        ("token_identity", {"device": True, "inode": 1}),
    ],
)
def test_live_protocol_corruption_is_zero_new_mutation(tmp_path, field, value):
    _root, output, tx, _token = _owner(tmp_path)
    path = output / ".graphify_protocol.json"
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="protocol"):
        commit_bytes(tx, "proof", b"forbidden")
    assert _file_bytes(output) == before


def test_recovery_rejects_protocol_extra_field_before_prepared_or_queue_mutation(
    tmp_path
):
    root, output, tx, _token = _owner(tmp_path)
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["unexpected"] = "laundering"
    protocol_path.write_text(json.dumps(protocol, separators=(",", ":")))
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="protocol"):
        recover_transaction("runtime", root, output=output, now=time.time() + 100)
    assert _file_bytes(output) == before
    assert not list(output.parent.glob(f".graphify-prepare-{tx.id}"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", "field"),
        ("lease_deadline", True),
        ("lease_deadline", float("inf")),
        ("acked_ids", ["A" * 64]),
        ("acked_ids", ["a" * 64, "a" * 64]),
    ],
)
def test_claimed_drainer_strict_shape_is_zero_new_mutation(
    tmp_path, field, value
):
    _root, output, _tx, _token = _owner(tmp_path)
    drainer_path = output / ".graphify_drainer.json"
    raw = json.loads(drainer_path.read_text())
    raw[field] = value
    drainer_path.write_text(json.dumps(raw, separators=(",", ":")))
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="drainer"):
        transaction_status(output)
    assert _file_bytes(output) == before


def test_snapshot_uses_effective_graph_byte_cap(monkeypatch, tmp_path):
    output = tmp_path / "graphify-out"
    output.mkdir()
    graph = output / "graph.json"
    payload = b'{"directed":false,"multigraph":false,"graph":{},"nodes":[],"links":[]}'
    graph.write_bytes(payload)
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", str(len(payload) - 1))
    with pytest.raises(PendingTransactionError, match="size limit"):
        open_graph_snapshot(graph, purpose="lowered-cap")
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", str(len(payload) + 1))
    assert open_graph_snapshot(graph, purpose="raised-cap").payload == payload


def test_receipt_revokes_all_publication_but_finish_remains_authorized(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    with owned_step(tx):
        commit_bytes(tx, "graph.json", _graph(tx.generation))
        commit_bytes(tx, "manifest.json", b"{}")
        commit_bytes(tx, "stale.html", b"old")
        receipt = commit_generation(
            tx,
            graph_payload=_graph(tx.generation),
            manifest_payload=b"{}",
            required_artifacts=("graph.json", "manifest.json", "stale.html"),
        )
    before = _file_bytes(output)
    for operation in (
        lambda: commit_bytes(tx, "late.txt", b"forbidden"),
        lambda: commit_relative_bytes(tx, "wiki/late.md", b"forbidden"),
        lambda: commit_unlink(tx, "stale.html"),
    ):
        with pytest.raises(PendingTransactionError):
            operation()
        assert _file_bytes(output) == before
    finish_transaction(tx)
    assert transaction_status(output)["receipt_digest"] == receipt.digest


def test_completed_generation_cannot_claim_queued_successor(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    _commit_owner_generation(output, tx)
    queued = queue_rebuild(
        "update", root, output=output, changed_paths=["late.py"]
    )
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError):
        claim_rebuild_queue(tx, queued.drainer)
    assert _file_bytes(output) == before
    assert not list(output.glob(".graphify_rebuild_inflight.*.jsonl"))
    assert b"late.py" in (output / ".graphify_rebuild_queue.jsonl").read_bytes()


def test_unpublished_cancellation_restores_exact_predecessor_protocol_and_close(
    tmp_path,
):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root_a, output=output)
    _commit_owner_generation(output, predecessor)
    finish_transaction(predecessor)
    protocol_before = (output / ".graphify_protocol.json").read_bytes()
    drainer_before = (output / ".graphify_drainer.json").read_bytes()

    successor = begin_transaction("update", root_b, output=output)
    cancel_unpublished_transaction(successor)

    assert (output / ".graphify_protocol.json").read_bytes() == protocol_before
    assert (output / ".graphify_drainer.json").read_bytes() == drainer_before
    restored = json.loads(protocol_before)
    assert restored["root"] == str(root_a.resolve())
    assert restored["kind"] == "full"
    assert not (output / ".graphify_transaction.json").exists()
    assert not (output / ".graphify_predecessor.json").exists()


@pytest.mark.parametrize("artifact", ["manifest.json", "wiki/proof.md"])
def test_unpublished_cancellation_validates_every_predecessor_artifact_zero_mutation(
    tmp_path, artifact
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root, output=output)
    graph_payload = _graph(predecessor.generation)
    with owned_step(predecessor):
        commit_bytes(predecessor, "graph.json", graph_payload)
        commit_bytes(predecessor, "manifest.json", b"{}")
        commit_relative_bytes(predecessor, "wiki/proof.md", b"proof")
        commit_generation(
            predecessor,
            graph_payload=graph_payload,
            manifest_payload=b"{}",
            required_artifacts=(
                "graph.json",
                "manifest.json",
                "wiki/proof.md",
            ),
        )
    finish_transaction(predecessor)
    successor = begin_transaction("runtime", root, output=output)
    (output / artifact).write_bytes(b"corrupt")
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="digest"):
        cancel_unpublished_transaction(successor)
    assert _file_bytes(output) == before


@pytest.mark.parametrize(
    ("failpoint_name", "durable_state"),
    [
        ("after_cancel_protocol", "protocol-restored"),
        ("after_cancel_drainer", "drainer-restored"),
        ("after_cancel_prepared", "prepared-retired"),
        ("after_cancel_token", "token-removed"),
        ("after_cancel_live", "live-removed"),
        ("after_cancel_record", None),
    ],
)
def test_unpublished_cancellation_resumes_every_durable_step(
    tmp_path, failpoint_name, durable_state
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, predecessor)
    finish_transaction(predecessor)
    protocol_before = (output / ".graphify_protocol.json").read_bytes()
    drainer_before = (output / ".graphify_drainer.json").read_bytes()

    successor = begin_transaction("update", root, output=output)
    stage_transaction_handoff(successor)
    successor = resume_transaction(successor.id, root, output=output)
    commit_prepared_bytes(successor, "draft.txt", b"prepared")

    def interrupt(name: str) -> None:
        if name == failpoint_name:
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match=failpoint_name):
        cancel_unpublished_transaction(successor, failpoint=interrupt)

    status = transaction_status(output)
    transition = status["cancellation_transition"]
    if durable_state is None:
        assert transition is None
    else:
        assert transition == {
            "state": durable_state,
            "transaction_id": successor.id,
            "generation": successor.generation,
            "kind": successor.kind,
            "root": successor.root,
            "output_identity": status["output_identity"],
        }
    retry_transaction = (
        successor
        if durable_state is None
        else resume_transaction(successor.id, root, output=output)
    )
    cancel_unpublished_transaction(retry_transaction)

    assert (output / ".graphify_protocol.json").read_bytes() == protocol_before
    assert (output / ".graphify_drainer.json").read_bytes() == drainer_before
    assert not (output / ".graphify_predecessor.json").exists()
    assert not (output / ".graphify_prepared.json").exists()
    assert not (output / ".graphify_transaction.json").exists()
    assert not list(output.glob(".graphify_transaction_token.*"))
    retained = transaction_status(output)["retained_workspaces"]
    assert len(retained) == 1
    assert retained[0]["state"] == "retired"


@pytest.mark.parametrize("recovery_surface", ["selected", "direct"])
def test_operational_recovery_completes_resumable_cancellation(
    tmp_path, recovery_surface
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, predecessor)
    finish_transaction(predecessor)
    successor = begin_transaction("update", root, output=output)

    def interrupt(name: str) -> None:
        if name == "after_cancel_protocol":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match="after_cancel_protocol"):
        cancel_unpublished_transaction(successor, failpoint=interrupt)
    status = transaction_status(output)
    before = _file_bytes(output)
    identity = OutputIdentity(**status["output_identity"])
    with pytest.raises(PendingTransactionError, match="stale cancellation"):
        recover_selected_transaction(
            "update",
            root,
            output=output,
            expected_generation=successor.generation + 1,
            expected_output_identity=identity,
            expected_transaction_id=successor.id,
        )
    assert _file_bytes(output) == before
    recovered = (
        recover_selected_transaction(
            "update",
            root,
            output=output,
            expected_generation=successor.generation,
            expected_output_identity=identity,
            expected_transaction_id=successor.id,
        )
        if recovery_surface == "selected"
        else recover_transaction(
            "update",
            root,
            output=output,
            expected_generation=successor.generation,
            expected_output_identity=identity,
            expected_transaction_id=successor.id,
        )
    )
    assert isinstance(recovered, CancellationRecovery)
    assert recovered.transaction_id == successor.id
    assert recovered.predecessor_generation == successor.generation - 1
    assert transaction_status(output)["protocol_state"] == "COMPLETE"
    assert not (output / ".graphify_predecessor.json").exists()


@pytest.mark.parametrize("corruption", ["unknown-state", "extra-field"])
def test_cancellation_transition_parser_rejects_noncanonical_state_zero_mutation(
    tmp_path, corruption
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, predecessor)
    finish_transaction(predecessor)
    successor = begin_transaction("update", root, output=output)

    def interrupt(name: str) -> None:
        if name == "after_cancel_protocol":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match="after_cancel_protocol"):
        cancel_unpublished_transaction(successor, failpoint=interrupt)
    record_path = output / ".graphify_predecessor.json"
    record = json.loads(record_path.read_text())
    if corruption == "unknown-state":
        record["state"] = "protocol-restoring"
    else:
        record["unexpected"] = True
    record_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
    before = _file_bytes(output)
    for operation in (
        lambda: transaction_status(output),
        lambda: cancel_unpublished_transaction(successor),
    ):
        with pytest.raises(PendingTransactionError, match="predecessor authority"):
            operation()
        assert _file_bytes(output) == before


@pytest.mark.parametrize(
    "failpoint_name",
    [
        "after_cancel_protocol",
        "after_cancel_drainer",
        "after_cancel_prepared",
        "after_cancel_token",
        "after_cancel_live",
    ],
)
def test_cli_recover_completes_every_resumable_cancellation_phase(
    tmp_path, monkeypatch, capsys, failpoint_name
):
    from graphify.cli import dispatch_command

    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, predecessor)
    finish_transaction(predecessor)
    successor = begin_transaction("update", root, output=output)
    stage_transaction_handoff(successor)
    successor = resume_transaction(successor.id, root, output=output)
    commit_prepared_bytes(successor, "draft.txt", b"prepared")

    def interrupt(name: str) -> None:
        if name == failpoint_name:
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match=failpoint_name):
        cancel_unpublished_transaction(successor, failpoint=interrupt)
    identity = transaction_status(output)["output_identity"]
    base_arguments = [
        "graphify",
        "transaction",
        "recover",
        "--output",
        str(output),
        "--device",
        str(identity["device"]),
        "--inode",
        str(identity["inode"]),
        "--root",
        str(root),
        "--transaction-id",
        successor.id,
    ]
    before = _file_bytes(output)
    monkeypatch.setattr(
        sys, "argv", [*base_arguments, "--generation", str(successor.generation + 1)]
    )
    with pytest.raises(PendingTransactionError, match="stale cancellation"):
        dispatch_command("transaction")
    assert _file_bytes(output) == before

    monkeypatch.setattr(
        sys, "argv", [*base_arguments, "--generation", str(successor.generation)]
    )
    dispatch_command("transaction")
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "state": "cancelled",
        "transaction_id": successor.id,
        "generation": successor.generation,
        "predecessor_generation": successor.generation - 1,
        "protocol_state": "COMPLETE",
        "output_identity": identity,
    }
    assert transaction_status(output)["cancellation_transition"] is None


@pytest.mark.parametrize(
    ("failpoint_name", "component"),
    [
        ("after_cancel_protocol", "protocol"),
        ("after_cancel_drainer", "drainer"),
        ("after_cancel_prepared", "token"),
        ("after_cancel_drainer", "prepared"),
        ("after_cancel_prepared", "retirement"),
        ("after_cancel_token", "live"),
    ],
)
def test_cancellation_phase_validator_rejects_substituted_filesystem_state(
    tmp_path, failpoint_name, component
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    predecessor = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, predecessor)
    finish_transaction(predecessor)
    successor = begin_transaction("update", root, output=output)
    stage_transaction_handoff(successor)
    successor = resume_transaction(successor.id, root, output=output)
    commit_prepared_bytes(successor, "draft.txt", b"prepared")

    def interrupt(name: str) -> None:
        if name == failpoint_name:
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match=failpoint_name):
        cancel_unpublished_transaction(successor, failpoint=interrupt)
    target = {
        "protocol": output / ".graphify_protocol.json",
        "drainer": output / ".graphify_drainer.json",
        "prepared": output / ".graphify_prepared.json",
        "live": output / ".graphify_transaction.json",
    }.get(component)
    if component == "token":
        token = next(output.glob(".graphify_transaction_token.*"))
        token.unlink()
        token.write_bytes(b"replacement")
    elif component == "retirement":
        target = next(output.parent.glob(".graphify-retired-*")).joinpath(
            ".graphify_retired.json"
        )
        raw = json.loads(target.read_text())
        raw["managed_output_identity"]["inode"] += 1
        target.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    else:
        assert target is not None
        raw = json.loads(target.read_text())
        if component == "protocol":
            raw["bootstrap_nonce"] = "0" * 64
        elif component == "drainer":
            raw["launch_nonce"] = "0" * 32
        elif component == "prepared":
            raw["transaction_id"] = "0" * 64
        else:
            raw["kind"] = "runtime"
        target.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    before = _file_bytes(output)
    output_info = output.stat()
    for operation in (
        lambda: transaction_status(output),
        lambda: recover_selected_transaction(
            "update",
            root,
            output=output,
            expected_generation=successor.generation,
            expected_output_identity=OutputIdentity(output_info.st_dev, output_info.st_ino),
            expected_transaction_id=successor.id,
        ),
    ):
        with pytest.raises(PendingTransactionError):
            operation()
        assert _file_bytes(output) == before


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("now", True),
        ("source", 1),
        ("intent", 1),
        ("root", b"corpus"),
        ("changed_paths", [b"bytes.py"]),
    ],
)
def test_queue_public_arguments_reject_exact_type_errors_before_mutation(
    tmp_path, argument, value
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    kwargs = {"root": root, "output": output, argument: value}
    with pytest.raises((PendingTransactionError, TypeError)):
        queue_rebuild("update", **kwargs)
    assert not output.exists() or _file_bytes(output) == {}


def test_retired_workspace_gc_is_identity_selected_and_dry_run_by_default(tmp_path):
    _root, output, tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            (
                "from graphify.transaction import current_transaction,commit_prepared_bytes,"
                "finalize_prepared_transaction; tx=current_transaction(); "
                f"commit_prepared_bytes(tx,'graph.json',{_graph(tx.generation)!r}); "
                "commit_prepared_bytes(tx,'manifest.json',b'{}'); "
                "finalize_prepared_transaction()"
            ),
        ],
    )
    status = transaction_status(output)
    identity = OutputIdentity(**status["output_identity"])
    workspace = next(output.parent.glob(".graphify-retired-*"))
    workspace_info = workspace.stat(follow_symlinks=False)
    workspace_identity = OutputIdentity(workspace_info.st_dev, workspace_info.st_ino)
    candidates = gc_retired_workspaces(
        output,
        expected_output_identity=identity,
        workspace=workspace,
        expected_workspace_identity=workspace_identity,
        dry_run=True,
    )
    assert candidates
    assert all((output.parent / name).exists() for name in candidates)
    assert gc_retired_workspaces(
        output,
        expected_output_identity=identity,
        workspace=workspace,
        expected_workspace_identity=workspace_identity,
        dry_run=False,
    ) == candidates
    assert all(not (output.parent / name).exists() for name in candidates)
    with pytest.raises(PendingTransactionError, match="stale output identity"):
        gc_retired_workspaces(
            output,
            expected_output_identity=OutputIdentity(identity.device, identity.inode + 1),
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
        )


def test_retired_gc_cannot_cross_two_outputs_with_one_parent(tmp_path):
    outputs: list[tuple[Path, OutputIdentity, Path, OutputIdentity]] = []
    for name in ("left", "right"):
        root = tmp_path / f"{name}-root"
        root.mkdir()
        output = tmp_path / name
        tx = begin_transaction("full", root, output=output)
        token = stage_transaction_handoff(tx)
        run_prepared_token(
            token.path,
            [
                "-c",
                (
                    "from graphify.transaction import current_transaction,commit_prepared_bytes,"
                    "finalize_prepared_transaction; tx=current_transaction(); "
                    f"commit_prepared_bytes(tx,'graph.json',{_graph(tx.generation)!r}); "
                    "commit_prepared_bytes(tx,'manifest.json',b'{}'); "
                    "finalize_prepared_transaction()"
                ),
            ],
        )
        workspace = max(
            output.parent.glob(".graphify-retired-*"), key=lambda path: path.stat().st_mtime_ns
        )
        workspace_info = workspace.stat(follow_symlinks=False)
        status = transaction_status(output)
        outputs.append(
            (
                output,
                OutputIdentity(**status["output_identity"]),
                workspace,
                OutputIdentity(workspace_info.st_dev, workspace_info.st_ino),
            )
        )
    left_output, left_identity, _left_workspace, _left_workspace_identity = outputs[0]
    right_output, _right_identity, right_workspace, right_workspace_identity = outputs[1]
    assert {item["name"] for item in transaction_status(left_output)["retained_workspaces"]} == {
        outputs[0][2].name
    }
    assert {item["name"] for item in transaction_status(right_output)["retained_workspaces"]} == {
        right_workspace.name
    }
    before = right_workspace.joinpath(".graphify_retired.json").read_bytes()
    with pytest.raises(PendingTransactionError, match="binding"):
        gc_retired_workspaces(
            left_output,
            expected_output_identity=left_identity,
            workspace=right_workspace,
            expected_workspace_identity=right_workspace_identity,
            dry_run=False,
        )
    assert right_workspace.joinpath(".graphify_retired.json").read_bytes() == before


def test_retired_gc_quarantine_is_visible_and_resumable(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import current_transaction,commit_prepared_bytes,"
            "finalize_prepared_transaction; tx=current_transaction(); "
            f"commit_prepared_bytes(tx,'graph.json',{_graph(1)!r}); "
            "commit_prepared_bytes(tx,'manifest.json',b'{}'); finalize_prepared_transaction()",
        ],
    )
    workspace = next(output.parent.glob(".graphify-retired-*"))
    workspace_info = workspace.stat(follow_symlinks=False)
    output_identity = OutputIdentity(**transaction_status(output)["output_identity"])
    workspace_identity = OutputIdentity(workspace_info.st_dev, workspace_info.st_ino)

    def interrupt(state: str) -> None:
        if state == "after_gc_quarantine":
            raise RuntimeError("gc interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        gc_retired_workspaces(
            output,
            expected_output_identity=output_identity,
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
            dry_run=False,
            failpoint=interrupt,
        )
    assert not workspace.exists()
    status = transaction_status(output)
    assert status["retained_workspaces"] == [
        {
            "name": workspace.name,
            "current_name": next(output.parent.glob(".graphify-gc-root-*")).name,
            "state": "gc_quarantined",
            "identity": workspace_identity.json(),
        }
    ]
    assert gc_retired_workspaces(
        output,
        expected_output_identity=output_identity,
        workspace=workspace,
        expected_workspace_identity=workspace_identity,
        dry_run=False,
    ) == (workspace.name,)
    assert not list(output.parent.glob(".graphify-gc-root-*"))


def test_retired_gc_rejects_identity_moved_to_unrecorded_controlled_name(
    tmp_path,
):
    _root, output, _tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import current_transaction,commit_prepared_bytes,"
            "finalize_prepared_transaction; tx=current_transaction(); "
            f"commit_prepared_bytes(tx,'graph.json',{_graph(1)!r}); "
            "commit_prepared_bytes(tx,'manifest.json',b'{}'); finalize_prepared_transaction()",
        ],
    )
    workspace = next(output.parent.glob(".graphify-retired-*"))
    workspace_info = workspace.stat(follow_symlinks=False)
    output_identity = OutputIdentity(**transaction_status(output)["output_identity"])
    workspace_identity = OutputIdentity(workspace_info.st_dev, workspace_info.st_ino)

    def interrupt(state: str) -> None:
        if state == "after_gc_quarantine":
            raise RuntimeError(state)

    with pytest.raises(RuntimeError, match="after_gc_quarantine"):
        gc_retired_workspaces(
            output,
            expected_output_identity=output_identity,
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
            dry_run=False,
            failpoint=interrupt,
        )
    quarantine = next(output.parent.glob(".graphify-gc-root-*"))
    moved = output.parent / (".graphify-gc-root-" + "f" * 32)
    quarantine.rename(moved)
    before = _file_bytes(output.parent)
    with pytest.raises(PendingTransactionError, match="location"):
        transaction_status(output)
    assert _file_bytes(output.parent) == before
    with pytest.raises(PendingTransactionError, match="location"):
        gc_retired_workspaces(
            output,
            expected_output_identity=output_identity,
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
            dry_run=False,
        )
    assert _file_bytes(output.parent) == before


@pytest.mark.parametrize(
    "boundary",
    [
        "after_gc_child_quarantine",
        "after_gc_child_unlink",
        "after_gc_marker_retirement",
        "after_gc_root_removal",
        "after_gc_parent_fsync",
    ],
)
def test_retired_gc_parent_journal_survives_and_resumes_each_boundary(
    tmp_path, boundary
):
    _root, output, tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            (
                "from graphify.transaction import current_transaction,commit_prepared_bytes,"
                "finalize_prepared_transaction; tx=current_transaction(); "
                f"commit_prepared_bytes(tx,'graph.json',{_graph(tx.generation)!r}); "
                "commit_prepared_bytes(tx,'manifest.json',b'{}'); "
                "commit_prepared_bytes(tx,'wiki/index.md',b'proof'); "
                "finalize_prepared_transaction()"
            ),
        ],
    )
    status = transaction_status(output)
    output_identity = OutputIdentity(**status["output_identity"])
    workspace = next(output.parent.glob(".graphify-retired-*"))
    info = workspace.stat(follow_symlinks=False)
    workspace_identity = OutputIdentity(info.st_dev, info.st_ino)

    def stop(name: str) -> None:
        if name == boundary:
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match=boundary):
        gc_retired_workspaces(
            output,
            expected_output_identity=output_identity,
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
            dry_run=False,
            failpoint=stop,
        )
    assert list(output.parent.glob(".graphify-gc-journal-*.json"))
    retained = transaction_status(output)["retained_workspaces"]
    assert any(item["name"] == workspace.name for item in retained)
    if boundary == "after_gc_root_removal":
        assert retained == [
            {
                "name": workspace.name,
                "current_name": None,
                "state": "gc_quarantined",
                "identity": workspace_identity.json(),
            }
        ]
    assert gc_retired_workspaces(
        output,
        expected_output_identity=output_identity,
        workspace=workspace,
        expected_workspace_identity=workspace_identity,
        dry_run=False,
    ) == (workspace.name,)
    assert not list(output.parent.glob(".graphify-gc-journal-*.json"))
    assert not list(output.parent.glob(".graphify-gc-root-*"))


def test_retired_marker_traversal_name_is_zero_new_mutation(tmp_path):
    _root, output, tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            (
                "from graphify.transaction import current_transaction,commit_prepared_bytes,"
                "finalize_prepared_transaction; tx=current_transaction(); "
                f"commit_prepared_bytes(tx,'graph.json',{_graph(tx.generation)!r}); "
                "commit_prepared_bytes(tx,'manifest.json',b'{}'); "
                "finalize_prepared_transaction()"
            ),
        ],
    )
    workspace = next(output.parent.glob(".graphify-retired-*"))
    marker_path = workspace / ".graphify_retired.json"
    marker = json.loads(marker_path.read_text())
    marker["current_name"] = "../escape"
    marker_path.write_text(json.dumps(marker, separators=(",", ":")))
    before = _file_bytes(output)
    workspace_before = _file_bytes(workspace)
    with pytest.raises(PendingTransactionError, match="retired workspace"):
        transaction_status(output)
    assert _file_bytes(output) == before
    assert _file_bytes(workspace) == workspace_before


def test_gc_journal_traversal_and_forged_filename_are_zero_new_mutation(tmp_path):
    _root, output, tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            (
                "from graphify.transaction import current_transaction,commit_prepared_bytes,"
                "finalize_prepared_transaction; tx=current_transaction(); "
                f"commit_prepared_bytes(tx,'graph.json',{_graph(tx.generation)!r}); "
                "commit_prepared_bytes(tx,'manifest.json',b'{}'); "
                "finalize_prepared_transaction()"
            ),
        ],
    )
    status = transaction_status(output)
    output_identity = OutputIdentity(**status["output_identity"])
    workspace = next(output.parent.glob(".graphify-retired-*"))
    info = workspace.stat(follow_symlinks=False)
    workspace_identity = OutputIdentity(info.st_dev, info.st_ino)

    def stop(name: str) -> None:
        if name == "after_gc_quarantine":
            raise RuntimeError(name)

    with pytest.raises(RuntimeError, match="after_gc_quarantine"):
        gc_retired_workspaces(
            output,
            expected_output_identity=output_identity,
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
            dry_run=False,
            failpoint=stop,
        )
    journal_path = next(output.parent.glob(".graphify-gc-journal-*.json"))
    journal = json.loads(journal_path.read_text())
    forged = output.parent / (".graphify-gc-journal-" + "f" * 64 + ".json")
    forged.write_bytes(journal_path.read_bytes())
    before = {
        path.name: path.read_bytes()
        for path in output.parent.iterdir()
        if path.is_file()
    }
    with pytest.raises(PendingTransactionError, match="journal selector"):
        transaction_status(output)
    assert {
        path.name: path.read_bytes()
        for path in output.parent.iterdir()
        if path.is_file()
    } == before
    forged.unlink()
    journal["quarantine_name"] = "../escape"
    journal_path.write_text(json.dumps(journal, separators=(",", ":")))
    before = journal_path.read_bytes()
    with pytest.raises(PendingTransactionError, match="journal"):
        transaction_status(output)
    assert journal_path.read_bytes() == before


def test_retired_gc_final_identity_check_preserves_replacement(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    _root, output, _tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import current_transaction,commit_prepared_bytes,"
            "finalize_prepared_transaction; tx=current_transaction(); "
            f"commit_prepared_bytes(tx,'graph.json',{_graph(1)!r}); "
            "commit_prepared_bytes(tx,'manifest.json',b'{}'); finalize_prepared_transaction()",
        ],
    )
    workspace = next(output.parent.glob(".graphify-retired-*"))
    workspace_info = workspace.stat(follow_symlinks=False)
    output_identity = OutputIdentity(**transaction_status(output)["output_identity"])
    workspace_identity = OutputIdentity(workspace_info.st_dev, workspace_info.st_ino)
    real_stat = transaction_module.os.stat
    root_stats = 0

    def replace_before_terminal_stat(path, *args, **kwargs):
        nonlocal root_stats
        if (
            isinstance(path, str)
            and path.startswith(".graphify-gc-root-")
            and kwargs.get("dir_fd") is not None
        ):
            root_stats += 1
            if root_stats == 2:
                parent_fd = kwargs["dir_fd"]
                preserved = f"{path}.preserved"
                os.rename(path, preserved, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(path, dir_fd=parent_fd)
                replacement_fd = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    dir_fd=parent_fd,
                )
                try:
                    fd = os.open("sentinel", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=replacement_fd)
                    os.close(fd)
                finally:
                    os.close(replacement_fd)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(transaction_module.os, "stat", replace_before_terminal_stat)
    with pytest.raises(PendingTransactionError, match="identity changed"):
        gc_retired_workspaces(
            output,
            expected_output_identity=output_identity,
            workspace=workspace,
            expected_workspace_identity=workspace_identity,
            dry_run=False,
        )
    replacement = next(
        path
        for path in output.parent.glob(".graphify-gc-root-*")
        if not path.name.endswith(".preserved")
    )
    assert replacement.joinpath("sentinel").exists()


def test_status_surfaces_bound_retired_corruption_but_ignores_proven_sibling(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import current_transaction,commit_prepared_bytes,"
            "finalize_prepared_transaction; tx=current_transaction(); "
            f"commit_prepared_bytes(tx,'graph.json',{_graph(1)!r}); "
            "commit_prepared_bytes(tx,'manifest.json',b'{}'); finalize_prepared_transaction()",
        ],
    )
    workspace = next(output.parent.glob(".graphify-retired-*"))
    sibling = output.parent / ".graphify-retired-unrelated"
    sibling.mkdir()
    sibling.joinpath(".graphify_retired.json").write_text(
        json.dumps({"managed_output_identity": {"device": -1, "inode": -1}}),
        encoding="utf-8",
    )
    assert len(transaction_status(output)["retained_workspaces"]) == 1
    marker_path = workspace / ".graphify_retired.json"
    marker = json.loads(marker_path.read_text())
    marker["transaction_id"] = "bad"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(PendingTransactionError, match="binding"):
        transaction_status(output)


@pytest.mark.parametrize("stop_state", ["reserved", "launching"])
def test_drainer_transition_failpoint_recovers_exact_state(tmp_path, stop_state):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def stop(state: str) -> None:
        if state == f"after_drainer_{stop_state}":
            raise RuntimeError(state)

    with pytest.raises(RuntimeError, match=stop_state):
        begin_transaction(
            "full", root, output=output, now=0.0, transition_failpoint=stop
        )
    live = json.loads((output / ".graphify_transaction.json").read_text())
    recovered = recover_transaction(
        "full",
        root,
        output=output,
        now=31.0,
        expected_transaction_id=live["id"],
        expected_generation=live["generation"],
        expected_output_identity=OutputIdentity(**live["output_identity"]),
    )
    assert recovered.id == live["id"]
    assert json.loads((output / ".graphify_drainer.json").read_text())["state"] == "claimed"


@pytest.mark.parametrize(
    "boundary",
    [
        "after_owner_protocol",
        "after_transaction",
        "after_drainer_reserved",
        "after_drainer_launching",
        "after_drainer_claimed",
    ],
)
def test_repeated_build_complete_predecessor_recovers_exact_pending_transition(
    tmp_path, boundary
):
    root, output, first, _token = _owner(tmp_path)
    first = resume_transaction(first.id, root, output=output)
    _commit_owner_generation(output, first)
    finish_transaction(first)

    def stop(state: str) -> None:
        if state == boundary:
            raise RuntimeError("repeated build crash")

    with pytest.raises(RuntimeError, match="repeated build"):
        begin_transaction(
            "full", root, output=output, now=0.0, transition_failpoint=stop
        )
    pending = json.loads((output / ".graphify_transition.json").read_text())
    successor = pending["successor_transaction"]
    assert pending["predecessor_drainer"]["state"] == "complete"
    recovered = recover_transaction(
        "full",
        root,
        output=output,
        now=31.0,
        expected_transaction_id=successor["id"],
        expected_generation=successor["generation"],
        expected_output_identity=OutputIdentity(**successor["output_identity"]),
    )
    assert recovered.id == successor["id"]
    assert not (output / ".graphify_transition.json").exists()


@pytest.mark.parametrize(
    "boundary",
    [
        "after_successor_token",
        "after_owner_protocol",
        "after_transaction",
        "after_drainer_reserved",
        "after_drainer_launching",
        "after_drainer_claimed",
    ],
)
def test_takeover_recovery_reuses_identity_proven_successor_token(tmp_path, boundary):
    root, output, _tx, _token = _owner(tmp_path)

    def stop(state: str) -> None:
        if state == boundary:
            raise RuntimeError("takeover token crash")

    with pytest.raises(RuntimeError, match="takeover token"):
        takeover_drainer(
            output, now=time.time() + 100.0, transition_failpoint=stop
        )
    pending = json.loads((output / ".graphify_transition.json").read_text())
    successor = pending["successor_transaction"]
    token_path = output / f".graphify_transaction_token.{successor['id']}"
    token_identity = token_path.stat(follow_symlinks=False)
    recovered = recover_transaction(
        "runtime",
        root,
        output=output,
        expected_transaction_id=successor["id"],
        expected_generation=successor["generation"],
        expected_output_identity=OutputIdentity(**successor["output_identity"]),
    )
    after = token_path.stat(follow_symlinks=False)
    assert recovered.token_identity == (token_identity.st_dev, token_identity.st_ino)
    assert (after.st_dev, after.st_ino) == recovered.token_identity


@pytest.mark.parametrize(
    "boundary", ["after_successor_token", "after_owner_protocol", "after_transaction"]
)
def test_takeover_refuses_existing_pending_transition_zero_mutation(
    tmp_path, boundary
):
    _root, output, _tx, _token = _owner(tmp_path)

    def stop(state: str) -> None:
        if state == boundary:
            raise RuntimeError(boundary)

    with pytest.raises(RuntimeError, match=boundary):
        takeover_drainer(output, now=10**12, transition_failpoint=stop)
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="requires transaction recovery"):
        takeover_drainer(output, now=10**12)
    assert _file_bytes(output) == before


def test_tokenless_takeover_rotates_only_valid_pristine_reservation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    queued = queue_rebuild("full", root, output=output, now=0.0)
    successor = takeover_drainer(output, now=31.0)
    assert successor.generation == queued.drainer.generation == 1
    assert successor.claim_epoch == queued.drainer.claim_epoch + 1


def test_tokenless_takeover_rotates_receipt_bound_complete_successor(tmp_path):
    root, output, tx, _token = _owner(tmp_path)
    _commit_owner_generation(output, tx)
    finish_transaction(tx)
    queued = queue_rebuild("update", root, output=output, now=0.0)
    successor = takeover_drainer(output, now=31.0)
    assert successor.generation == queued.drainer.generation == tx.generation + 1
    assert successor.claim_epoch == 1


@pytest.mark.parametrize("protocol_state", ["BOOTSTRAP_PENDING", "INCOMPLETE"])
def test_tokenless_takeover_rejects_orphan_protocol_zero_mutation(
    tmp_path, protocol_state
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    if protocol_state == "BOOTSTRAP_PENDING":
        def stop(_capability, _protocol):
            raise RuntimeError("bootstrap")

        with pytest.raises(RuntimeError, match="bootstrap"):
            begin_transaction("runtime", root, output=output, now=0.0, failpoint=stop)
        protocol = json.loads((output / ".graphify_protocol.json").read_text())
    else:
        tx = begin_transaction("runtime", root, output=output, now=0.0)
        protocol = json.loads((output / ".graphify_protocol.json").read_text())
        (output / ".graphify_transaction.json").unlink()
    drainer = {
        "schema": 1,
        "protocol_epoch": 1,
        "generation": 1,
        "claim_epoch": 0,
        "launch_nonce": "a" * 32,
        "state": "reserved",
        "lease_deadline": 30.0,
    }
    (output / ".graphify_drainer.json").write_text(
        json.dumps(drainer, sort_keys=True, separators=(",", ":"))
    )
    assert protocol["state"] == protocol_state
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError, match="incomplete protocol"):
        takeover_drainer(output, now=31.0)
    assert _file_bytes(output) == before


def test_pending_transition_rejects_substituted_predecessor_without_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def stop(state: str) -> None:
        if state == "after_transition_record":
            raise RuntimeError("transition crash")

    with pytest.raises(RuntimeError, match="transition crash"):
        begin_transaction(
            "runtime", root, output=output, now=0.0, transition_failpoint=stop
        )
    transition_path = output / ".graphify_transition.json"
    transition = json.loads(transition_path.read_text())
    transition["predecessor_protocol"]["bootstrap_nonce"] = "substituted"
    transition_path.write_text(json.dumps(transition, sort_keys=True, separators=(",", ":")))
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="protocol predecessor"):
        recover_transaction("runtime", root, output=output, now=31.0)
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("boundary", ["after_transition_record", "after_owner_protocol"])
def test_selected_recovery_consumes_pending_successor(tmp_path, boundary):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def stop(state: str) -> None:
        if state == boundary:
            raise RuntimeError("selected recovery crash")

    with pytest.raises(RuntimeError, match="selected recovery"):
        begin_transaction(
            "runtime", root, output=output, now=0.0, transition_failpoint=stop
        )
    pending = json.loads((output / ".graphify_transition.json").read_text())
    successor = pending["successor_transaction"]
    status = transaction_status(output)
    assert status["pending_transition"] == {
        "state": "pending",
        "transaction_id": successor["id"],
        "generation": successor["generation"],
        "output_identity": successor["output_identity"],
    }
    assert "token_digest" not in json.dumps(status)
    recovered = recover_selected_transaction(
        "runtime",
        root,
        output=output,
        now=31.0,
        expected_transaction_id=successor["id"],
        expected_generation=successor["generation"],
        expected_output_identity=OutputIdentity(**successor["output_identity"]),
    )
    assert recovered.id == successor["id"]
    assert transaction_status(output)["pending_transition"] is None


@pytest.mark.parametrize("boundary", ["after_transition_record", "after_owner_protocol"])
def test_selected_pending_recovery_selector_mismatch_is_zero_mutation(tmp_path, boundary):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def stop(state: str) -> None:
        if state == boundary:
            raise RuntimeError("selector crash")

    with pytest.raises(RuntimeError, match="selector crash"):
        begin_transaction(
            "runtime", root, output=output, now=0.0, transition_failpoint=stop
        )
    pending = json.loads((output / ".graphify_transition.json").read_text())
    successor = pending["successor_transaction"]
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="stale transaction id"):
        recover_selected_transaction(
            "runtime",
            root,
            output=output,
            now=31.0,
            expected_transaction_id="f" * 64,
            expected_generation=successor["generation"],
            expected_output_identity=OutputIdentity(**successor["output_identity"]),
        )
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("boundary", ["after_transition_record", "after_owner_protocol"])
@pytest.mark.parametrize("kind", ["full", "update"])
def test_cli_selected_recovery_stages_tokenless_pending_successor(
    tmp_path, monkeypatch, capsys, boundary, kind
):
    from graphify.cli import dispatch_command

    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"

    def stop(state: str) -> None:
        if state == boundary:
            raise RuntimeError("cli selected crash")

    with pytest.raises(RuntimeError, match="cli selected"):
        begin_transaction(
            kind, root, output=output, now=0.0, transition_failpoint=stop
        )
    pending = transaction_status(output)["pending_transition"]
    assert pending is not None
    identity = pending["output_identity"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "graphify",
            "transaction",
            "recover",
            "--output",
            str(output),
            "--generation",
            str(pending["generation"]),
            "--device",
            str(identity["device"]),
            "--inode",
            str(identity["inode"]),
            "--root",
            str(root),
            "--transaction-id",
            str(pending["transaction_id"]),
        ],
    )
    dispatch_command("transaction")
    result = json.loads(capsys.readouterr().out)
    token_path = Path(result["token_path"])
    assert token_path == output / f".graphify_transaction_token.{pending['transaction_id']}"
    assert token_path.exists()
    assert resume_transaction(str(pending["transaction_id"]), root, output=output).kind == kind


@pytest.mark.parametrize("mutation", ["deleted", "substituted"])
def test_pending_takeover_requires_exact_predecessor_transaction_zero_mutation(
    tmp_path, mutation
):
    root, output, _tx, _token = _owner(tmp_path)

    def stop(state: str) -> None:
        if state == "after_successor_token":
            raise RuntimeError("takeover predecessor crash")

    with pytest.raises(RuntimeError, match="takeover predecessor"):
        takeover_drainer(
            output, now=time.time() + 100.0, transition_failpoint=stop
        )
    transaction_path = output / ".graphify_transaction.json"
    if mutation == "deleted":
        transaction_path.unlink()
    else:
        transaction = json.loads(transaction_path.read_text())
        transaction["id"] = "e" * 64
        transaction_path.write_text(
            json.dumps(transaction, sort_keys=True, separators=(",", ":"))
        )
    pending = json.loads((output / ".graphify_transition.json").read_text())
    successor = pending["successor_transaction"]
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="transaction predecessor"):
        recover_selected_transaction(
            None,
            root,
            output=output,
            expected_transaction_id=successor["id"],
            expected_generation=successor["generation"],
            expected_output_identity=OutputIdentity(**successor["output_identity"]),
        )
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


def test_cli_pending_takeover_reuses_successor_token(tmp_path, monkeypatch, capsys):
    from graphify.cli import dispatch_command

    root, output, _tx, _token = _owner(tmp_path)

    def stop(state: str) -> None:
        if state == "after_successor_token":
            raise RuntimeError("cli takeover crash")

    with pytest.raises(RuntimeError, match="cli takeover"):
        takeover_drainer(
            output, now=time.time() + 100.0, transition_failpoint=stop
        )
    pending = transaction_status(output)["pending_transition"]
    assert pending is not None
    token_path = output / f".graphify_transaction_token.{pending['transaction_id']}"
    token_info = token_path.stat(follow_symlinks=False)
    identity = pending["output_identity"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "graphify",
            "transaction",
            "recover",
            "--output",
            str(output),
            "--generation",
            str(pending["generation"]),
            "--device",
            str(identity["device"]),
            "--inode",
            str(identity["inode"]),
            "--root",
            str(root),
            "--transaction-id",
            str(pending["transaction_id"]),
        ],
    )
    dispatch_command("transaction")
    result = json.loads(capsys.readouterr().out)
    after = token_path.stat(follow_symlinks=False)
    assert result["token_path"] == str(token_path)
    assert (after.st_dev, after.st_ino) == (token_info.st_dev, token_info.st_ino)
    assert len(list(output.glob(f".graphify_transaction_token.{pending['transaction_id']}"))) == 1


def test_selected_recovery_stale_generation_is_zero_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    tx = begin_transaction("full", root, output=output, now=0.0)
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="stale transaction generation"):
        recover_selected_transaction(
            "runtime",
            root,
            output=output,
            now=31.0,
            expected_transaction_id=tx.id,
            expected_generation=tx.generation + 1,
            expected_output_identity=tx.output_identity,
        )
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("operation", ["takeover", "selected", "direct"])
@pytest.mark.parametrize(
    "field",
    [
        "generation",
        "transaction_id",
        "root",
        "kind",
        "owner_capability_digest",
        "token_identity",
        "output_identity",
        "drainer",
    ],
)
def test_durable_live_owner_mismatch_is_zero_mutation(
    tmp_path, operation, field
):
    root, output, tx, _token = _owner(tmp_path)
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    other = output / "other-identity"
    other.write_bytes(b"identity")
    other_info = other.stat(follow_symlinks=False)
    if field == "generation":
        protocol[field] = tx.generation + 1
    elif field == "transaction_id":
        protocol[field] = "f" * 64
    elif field == "root":
        protocol[field] = str(other_root)
    elif field == "kind":
        protocol[field] = "update"
    elif field == "owner_capability_digest":
        protocol[field] = "f" * 64
    elif field == "token_identity":
        protocol[field] = {"device": other_info.st_dev, "inode": other_info.st_ino}
    elif field == "output_identity":
        protocol[field] = {
            "device": tx.output_identity.device,
            "inode": tx.output_identity.inode + 1,
        }
    else:
        drainer_path = output / ".graphify_drainer.json"
        drainer = json.loads(drainer_path.read_text())
        drainer["launch_nonce"] = "f" * 32
        drainer_path.write_text(json.dumps(drainer, sort_keys=True, separators=(",", ":")))
    if field != "drainer":
        protocol_path.write_text(
            json.dumps(protocol, sort_keys=True, separators=(",", ":"))
        )
    before = _file_bytes(output)
    with pytest.raises(PendingTransactionError):
        if operation == "takeover":
            takeover_drainer(output, now=10**12)
        elif operation == "selected":
            recover_selected_transaction(
                "runtime",
                root,
                output=output,
                expected_generation=tx.generation,
                expected_transaction_id=tx.id,
                expected_output_identity=tx.output_identity,
                now=10**12,
            )
        else:
            recover_transaction(
                "runtime",
                root,
                output=output,
                expected_generation=tx.generation,
                expected_transaction_id=tx.id,
                expected_output_identity=tx.output_identity,
                now=10**12,
            )
    assert _file_bytes(output) == before


def test_recovery_substituted_drainer_tuple_is_zero_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    tx = begin_transaction("full", root, output=output, now=0.0)
    drainer_path = output / ".graphify_drainer.json"
    drainer = json.loads(drainer_path.read_text())
    drainer["launch_nonce"] = "f" * 32
    drainer_path.write_text(json.dumps(drainer, sort_keys=True, separators=(",", ":")))
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="exact live drainer"):
        recover_transaction(
            "full",
            root,
            output=output,
            now=31.0,
            expected_transaction_id=tx.id,
            expected_generation=tx.generation,
            expected_output_identity=tx.output_identity,
        )
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


def test_repeated_prepared_builds_do_not_hit_a_fixed_retirement_ceiling(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    for _index in range(17):
        tx = begin_transaction("full", root, output=output)
        token = stage_transaction_handoff(tx)
        run_prepared_token(
            token.path,
            [
                "-c",
                (
                    "from graphify.transaction import current_transaction,commit_prepared_bytes,"
                    "finalize_prepared_transaction; tx=current_transaction(); "
                    f"commit_prepared_bytes(tx,'graph.json',{_graph(tx.generation)!r}); "
                    "commit_prepared_bytes(tx,'manifest.json',b'{}'); "
                    "finalize_prepared_transaction()"
                ),
            ],
        )
    assert len(list(output.parent.glob(".graphify-retired-*"))) == 17


def test_managed_tree_html_publishes_as_a_new_receipt_bound_generation(tmp_path):
    from graphify.tree_html import write_tree_html

    root, output, tx, _token = _owner(tmp_path)
    tx = resume_transaction(tx.id, root, output=output)
    _commit_owner_generation(output, tx)
    finish_transaction(tx)
    destination = output / "GRAPH_TREE.html"
    write_tree_html(output / "graph.json", destination)
    snapshot = open_graph_snapshot(output / "graph.json", purpose="tree-result")
    assert destination.read_bytes().startswith(b"<!DOCTYPE html>")
    receipt = json.loads((output / ".graphify_generation.json").read_text())
    assert receipt["generation"] == snapshot.generation == tx.generation + 1
    assert "GRAPH_TREE.html" in receipt["required_artifacts"]


def test_transaction_cli_status_uses_validated_read_only_surface(tmp_path, monkeypatch, capsys):
    from graphify.cli import dispatch_command

    _root, output, tx, _token = _owner(tmp_path)
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    monkeypatch.setattr(sys, "argv", ["graphify", "transaction", "status", "--output", str(output)])
    dispatch_command("transaction")
    payload = json.loads(capsys.readouterr().out)
    after = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    assert payload["transaction"]["id"] == tx.id
    assert before == after


def test_public_help_discovers_transaction_operations(monkeypatch, capsys):
    import graphify.__main__ as mainmod

    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(sys, "argv", ["graphify", "--help"])
    mainmod.main()
    output = capsys.readouterr().out
    assert "transaction status --output DIR" in output
    assert (
        "transaction recover --output DIR --generation N --device D --inode I --root PATH"
        in output
    )
    assert "[--transaction-id ID] recover one exact generation" in output
    assert "transaction gc --output DIR --device D --inode I --workspace PATH" in output
    assert "--workspace-device D --workspace-inode I [--apply]" in output
    assert "recover one exact generation" in output
    assert "dry-run or remove one proven retired workspace" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["recover", "--output", "{output}", "--unknown", "x"],
        ["recover", "--output", "{output}", "--generation", "1"],
    ],
)
def test_transaction_recover_parser_errors_are_zero_mutation(
    tmp_path, monkeypatch, arguments
):
    from graphify.cli import dispatch_command

    root, output, _tx, _token = _owner(tmp_path)
    expanded = [
        str(output) if value == "{output}" else value for value in arguments
    ]
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    monkeypatch.setattr(sys, "argv", ["graphify", "transaction", *expanded, "--root", str(root)])
    with pytest.raises(SystemExit):
        dispatch_command("transaction")
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


def test_cli_recovery_rejects_healthy_completed_generation_without_mutation(
    tmp_path, monkeypatch
):
    from graphify.cli import dispatch_command

    root, output, tx, _token = _owner(tmp_path)
    tx = resume_transaction(tx.id, root, output=output)
    _commit_owner_generation(output, tx)
    finish_transaction(tx)
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "graphify",
            "transaction",
            "recover",
            "--output",
            str(output),
            "--generation",
            str(tx.generation),
            "--device",
            str(tx.output_identity.device),
            "--inode",
            str(tx.output_identity.inode),
            "--root",
            str(root),
            "--transaction-id",
            tx.id,
        ],
    )
    with pytest.raises(PendingTransactionError, match="completed generation"):
        dispatch_command("transaction")
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


def test_selected_queued_recovery_derives_kind_from_exact_matching_root(tmp_path):
    selected_root, output, tx, _token = _owner(tmp_path)
    tx = resume_transaction(tx.id, selected_root, output=output)
    _commit_owner_generation(output, tx)
    finish_transaction(tx)
    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    queue_rebuild("full", unrelated_root, output=output, changed_paths=["other.py"])
    queue_rebuild("update", selected_root, output=output, changed_paths=["selected.py"])
    recovered = recover_selected_transaction(
        None,
        selected_root,
        output=output,
        expected_generation=tx.generation,
        expected_output_identity=tx.output_identity,
    )
    assert recovered.kind == "update"
    assert recovered.root == str(selected_root.resolve())


def test_selected_queued_recovery_without_matching_root_is_zero_mutation(tmp_path):
    completed_root, output, tx, _token = _owner(tmp_path)
    tx = resume_transaction(tx.id, completed_root, output=output)
    _commit_owner_generation(output, tx)
    finish_transaction(tx)
    queued_root = tmp_path / "queued"
    queued_root.mkdir()
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    queue_rebuild("full", queued_root, output=output, changed_paths=["queued.py"])
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="no intent for the selected root"):
        recover_selected_transaction(
            None,
            selected_root,
            output=output,
            expected_generation=tx.generation,
            expected_output_identity=tx.output_identity,
        )
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("entrypoint", ["begin", "queue"])
def test_orphaned_watermarked_graph_cannot_bootstrap_without_sidecars(
    tmp_path, entrypoint
):
    root, output, completed, _token = _owner(tmp_path)
    completed = resume_transaction(completed.id, root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    for path in output.iterdir():
        if path.is_file() and path.name.startswith(".graphify"):
            path.unlink()
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError, match="watermarked graph"):
        if entrypoint == "begin":
            begin_transaction("full", root, output=output)
        else:
            queue_rebuild("full", root, output=output, changed_paths=["changed.py"])

    assert _file_bytes(output) == before


@pytest.mark.parametrize("entrypoint", ["begin", "queue"])
def test_malformed_graph_cannot_be_treated_as_legacy_bootstrap(tmp_path, entrypoint):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_bytes(b"{")
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError, match="malformed graph"):
        if entrypoint == "begin":
            begin_transaction("full", root, output=output)
        else:
            queue_rebuild("full", root, output=output, changed_paths=["changed.py"])

    assert _file_bytes(output) == before


@pytest.mark.parametrize("entrypoint", ["begin", "queue"])
@pytest.mark.parametrize(
    "orphan",
    [
        "manifest",
        "prepared",
        "token",
        "transaction",
        "nested",
        "dynamic_nested",
    ],
)
def test_graphless_bootstrap_rejects_orphaned_managed_state_without_mutation(
    tmp_path, entrypoint, orphan
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    output.mkdir()
    if orphan == "manifest":
        (output / "manifest.json").write_bytes(b"{}")
    elif orphan == "prepared":
        (output / ".graphify_prepared.json").write_bytes(b"{}")
    elif orphan == "token":
        (output / f".graphify_transaction_token.{'a' * 64}").write_bytes(b"token")
    elif orphan == "transaction":
        (output / ".graphify_transaction.json").write_bytes(b"{}")
    elif orphan == "nested":
        (output / "wiki").mkdir()
        (output / "wiki" / "orphan.md").write_text("orphan", encoding="utf-8")
    elif orphan == "dynamic_nested":
        (output / "custom-output").mkdir()
        (output / "custom-output" / "report.html").write_text(
            "orphan", encoding="utf-8"
        )
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError):
        if entrypoint == "begin":
            begin_transaction("full", root, output=output)
        else:
            queue_rebuild("full", root, output=output, changed_paths=["changed.py"])

    assert _file_bytes(output) == before


@pytest.mark.parametrize("entrypoint", ["begin", "queue"])
def test_graphless_bootstrap_preserves_explicit_safe_runtime_directories(
    tmp_path, entrypoint
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    for directory in ("cache", "memory", "reflections"):
        path = output / directory
        path.mkdir(parents=True, exist_ok=True)
        (path / "retained.txt").write_text(directory, encoding="utf-8")
    (output / ".graphify_python").write_text("/validated/python", encoding="utf-8")
    (output / ".rebuild.lock").write_text("123", encoding="utf-8")
    before = _file_bytes(output)

    if entrypoint == "begin":
        begin_transaction("full", root, output=output)
    else:
        queue_rebuild("full", root, output=output, changed_paths=["changed.py"])

    after = _file_bytes(output)
    assert all(after[name] == payload for name, payload in before.items())


@pytest.mark.parametrize("entrypoint", ["begin", "queue"])
@pytest.mark.parametrize("legacy", [False, True])
def test_empty_and_unwatermarked_legacy_outputs_remain_bootstrap_compatible(
    tmp_path, entrypoint, legacy
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    output.mkdir()
    legacy_payload = (
        b'{"directed":false,"multigraph":false,"nodes":[],"edges":[]}'
    )
    if legacy:
        (output / "graph.json").write_bytes(legacy_payload)

    if entrypoint == "begin":
        started = begin_transaction("full", root, output=output)
        assert started.generation == 1
    else:
        queued = queue_rebuild(
            "full", root, output=output, changed_paths=["changed.py"]
        )
        assert queued.drainer.generation == 1
    if legacy:
        assert (output / "graph.json").read_bytes() == legacy_payload


@pytest.mark.parametrize("kind", ["full", "update"])
def test_missing_drainer_reserves_valid_completed_successor_generation(tmp_path, kind):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    completed = None
    for _generation in range(4):
        completed = begin_transaction("full", root, output=output)
        _commit_owner_generation(output, completed)
        finish_transaction(completed)
    assert completed is not None and completed.generation == 4
    protocol = json.loads((output / ".graphify_protocol.json").read_text())
    receipt_digest = protocol["receipt_digest"]
    (output / ".graphify_drainer.json").unlink()

    queued = queue_rebuild(
        kind,
        root,
        output=output,
        changed_paths=[f"{kind}.py"],
    )
    drainer = json.loads((output / ".graphify_drainer.json").read_text())
    assert queued.drainer.generation == drainer["generation"] == 5
    assert drainer["state"] == "reserved"
    assert drainer["predecessor_receipt"] == receipt_digest

    successor = recover_selected_transaction(
        None,
        root,
        output=output,
        expected_generation=4,
        expected_output_identity=completed.output_identity,
    )
    assert successor.generation == 5
    assert successor.kind == kind
    claim = claim_rebuild_queue(successor, queued.drainer)
    assert [item["changed_paths"] for item in claim.items] == [[f"{kind}.py"]]
    digest = _commit_owner_generation(output, successor)
    complete_rebuild_claim(successor, claim, receipt_digest=digest)
    finish_transaction(successor)
    assert json.loads((output / ".graphify_protocol.json").read_text())["generation"] == 5


def test_missing_drainer_with_mismatched_completed_protocol_is_zero_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    completed = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    (output / ".graphify_drainer.json").unlink()
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["generation"] = completed.generation + 1
    protocol_path.write_text(json.dumps(protocol, sort_keys=True, separators=(",", ":")))
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="receipt does not match protocol"):
        queue_rebuild("full", root, output=output, changed_paths=["changed.py"])
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("entrypoint", ["queue", "recover_close"])
@pytest.mark.parametrize("corruption", ["missing", "malformed", "generation"])
def test_complete_drainer_requires_valid_closed_receipt_before_successor_mutation(
    tmp_path, entrypoint, corruption
):
    root, output, completed, _token = _owner(tmp_path)
    completed = resume_transaction(completed.id, root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    if entrypoint == "recover_close":
        complete_drainer = (output / ".graphify_drainer.json").read_bytes()
        queue_rebuild("update", root, output=output, changed_paths=["queued.py"])
        (output / ".graphify_drainer.json").write_bytes(complete_drainer)
    _corrupt_generation_receipt(output, corruption)
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError, match="receipt"):
        if entrypoint == "queue":
            queue_rebuild("update", root, output=output, changed_paths=["queued.py"])
        else:
            recover_close(output)

    assert _file_bytes(output) == before


@pytest.mark.parametrize("entrypoint", ["queue", "recover_close"])
def test_complete_drainer_rejects_retained_successor_generation_mismatch(
    tmp_path, entrypoint
):
    root, output, completed, _token = _owner(tmp_path)
    completed = resume_transaction(completed.id, root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    complete_path = output / ".graphify_drainer.json"
    complete_drainer = complete_path.read_bytes()
    queue_rebuild("update", root, output=output, changed_paths=["queued.py"])
    raw = json.loads(complete_drainer)
    raw["successor_generation"] = completed.generation + 2
    complete_path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError, match="successor generation"):
        if entrypoint == "queue":
            queue_rebuild("update", root, output=output, changed_paths=["later.py"])
        else:
            recover_close(output)

    assert _file_bytes(output) == before


def test_missing_drainer_rejects_coherent_receipt_rewrite_with_stale_graph_watermark(
    tmp_path,
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    completed = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    (output / ".graphify_drainer.json").unlink()
    receipt_path = output / ".graphify_generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["generation"] = completed.generation + 1
    receipt["drainer"]["generation"] = completed.generation + 1
    receipt["watermark"]["generation"] = completed.generation + 1
    receipt_payload = json.dumps(
        receipt, sort_keys=True, separators=(",", ":")
    ).encode()
    receipt_path.write_bytes(receipt_payload)
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["generation"] = completed.generation + 1
    protocol["receipt_digest"] = hashlib.sha256(receipt_payload).hexdigest()
    protocol_path.write_text(
        json.dumps(protocol, sort_keys=True, separators=(",", ":"))
    )
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="graph watermark"):
        queue_rebuild("full", root, output=output, changed_paths=["changed.py"])
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("missing_drainer", [False, True])
def test_begin_rejects_corrupt_completed_protocol_before_mutation(
    tmp_path, missing_drainer
):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    completed = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    if missing_drainer:
        (output / ".graphify_drainer.json").unlink()
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["generation"] = completed.generation + 9
    protocol_path.write_text(
        json.dumps(protocol, sort_keys=True, separators=(",", ":"))
    )
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="receipt does not match protocol"):
        begin_transaction("full", root, output=output)
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


def test_begin_rejects_coherent_negative_generation_rollback_without_mutation(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    output = tmp_path / "graphify-out"
    completed = begin_transaction("full", root, output=output)
    _commit_owner_generation(output, completed)
    finish_transaction(completed)
    (output / ".graphify_drainer.json").unlink()
    graph_path = output / "graph.json"
    graph = json.loads(graph_path.read_text())
    graph["graph"][GRAPH_WATERMARK_KEY]["generation"] = -1
    graph_payload = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
    graph_path.write_bytes(graph_payload)
    receipt_path = output / ".graphify_generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["generation"] = -1
    receipt["drainer"]["generation"] = -1
    receipt["watermark"] = graph["graph"][GRAPH_WATERMARK_KEY]
    receipt["graph_digest"] = hashlib.sha256(graph_payload).hexdigest()
    receipt["artifact_digests"]["graph.json"] = receipt["graph_digest"]
    receipt_payload = json.dumps(
        receipt, sort_keys=True, separators=(",", ":")
    ).encode()
    receipt_path.write_bytes(receipt_payload)
    protocol_path = output / ".graphify_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["generation"] = -1
    protocol["receipt_digest"] = hashlib.sha256(receipt_payload).hexdigest()
    protocol_path.write_text(
        json.dumps(protocol, sort_keys=True, separators=(",", ":"))
    )
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="malformed protocol|drainer authority"):
        begin_transaction("full", root, output=output)
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", "1"),
        ("claim_epoch", True),
        ("launch_nonce", 1234567890123456),
    ],
)
def test_drainer_parser_rejects_coercible_json_types_without_mutation(
    tmp_path, field, value
):
    root, output, _tx, _token = _owner(tmp_path)
    drainer_path = output / ".graphify_drainer.json"
    drainer = json.loads(drainer_path.read_text())
    drainer[field] = value
    drainer_path.write_text(json.dumps(drainer, sort_keys=True, separators=(",", ":")))
    before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    with pytest.raises(PendingTransactionError, match="malformed drainer authority"):
        queue_rebuild("update", root, output=output, changed_paths=["changed.py"])
    assert {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()} == before


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


def test_tokenless_takeover_cannot_install_local_publication_authority(tmp_path):
    root, output, _tx, _token = _owner(tmp_path)
    code = (
        "import json; from pathlib import Path; "
        "from graphify.transaction import takeover_drainer, resume_transaction, commit_bytes; "
        f"output=Path({str(output)!r}); root=Path({str(root)!r}); "
        "takeover_drainer(output, now=10**12); "
        "live=json.loads((output/'.graphify_transaction.json').read_text()); "
        "tx=resume_transaction(live['id'], root, output=output); "
        "commit_bytes(tx, 'tokenless-takeover-publish', b'no')"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "owner context" in result.stderr
    assert not (output / "tokenless-takeover-publish").exists()


def test_old_token_runner_cannot_self_takeover_then_publish(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    code = (
        "import json; from pathlib import Path; "
        "from graphify.transaction import takeover_drainer, resume_transaction, commit_bytes; "
        "old=Path(__import__('os').environ['GRAPHIFY_TRANSACTION_OUTPUT']); "
        "takeover_drainer(old, now=10**12); "
        "live=json.loads((old/'.graphify_transaction.json').read_text()); "
        "tx=resume_transaction(live['id'], live['root'], output=old); "
        "commit_bytes(tx, 'old-token-self-takeover', b'no')"
    )
    with pytest.raises(PendingTransactionError, match="owner context"):
        run_token(token.path, ["-c", code])
    assert not (output / "old-token-self-takeover").exists()


def test_takeover_retires_prepared_binding_and_successor_can_prepare(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    run_prepared_token(
        token.path, ["-c", "from pathlib import Path; Path('old-proof').write_text('old')"]
    )
    old_workspace = output.parent / f".graphify-prepare-{token.id}"
    takeover_drainer(output, now=10**12)
    successor_token = active_transaction_token_path(output)
    run_prepared_token(
        successor_token,
        ["-c", "from pathlib import Path; Path('new-proof').write_text('new')"],
    )
    assert not old_workspace.exists()
    assert list(output.parent.glob(f".graphify-retired-{token.id}-*"))
    successor_workspace = output.parent / f".graphify-prepare-{successor_token.name.rsplit('.', 1)[-1]}"
    assert (successor_workspace / "graphify-out" / "new-proof").read_text() == "new"


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
    claim = claim_rebuild_queue(tx, queued.drainer, now=10.0)
    with pytest.raises(PendingTransactionError, match="lease"):
        takeover_drainer(output, now=20.0)

    live = resume_transaction(tx.id, root, output=output)
    receipt_digest = _commit_owner_generation(output, live)
    complete_rebuild_claim(live, claim, receipt_digest=receipt_digest)
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
    with pytest.raises(PendingTransactionError, match="identity|missing|cannot pin"):
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
        "Path('relative-proof').write_text('retained'); "
        f"Path({str(proof)!r}).write_text(str(os.stat('.').st_ino))"
    )
    run_prepared_token(token.path, ["-c", code])
    assert (moved / "graphify-out" / "relative-proof").read_text() == "retained"
    assert (replacement / "sentinel").read_text() == "replacement"
    assert int(proof.read_text()) == (moved / "graphify-out").stat().st_ino


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


def test_prepared_runner_writes_through_retained_child_after_child_swap(tmp_path):
    _root, output, _tx, token = _owner(tmp_path)
    workspace = output.parent / f".graphify-prepare-{token.id}"
    moved_child = tmp_path / "moved-prepared-child"
    code = (
        "from pathlib import Path; "
        f"child=Path({str(workspace / 'graphify-out')!r}); "
        f"child.rename(Path({str(moved_child)!r})); child.mkdir(); "
        "(child/'sentinel').write_text('replacement'); "
        "Path('retained-child-proof').write_text('original')"
    )
    run_prepared_token(token.path, ["-c", code])
    assert (moved_child / "retained-child-proof").read_text() == "original"
    assert (workspace / "graphify-out" / "sentinel").read_text() == "replacement"
    assert not (workspace / "graphify-out" / "retained-child-proof").exists()


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
            f"Path('graph.json').write_bytes({ _graph(tx.generation)!r}); "
            "Path('manifest.json').write_text('{}'); "
            f"Path('cost.json').write_text(json.dumps({first_cost!r}))",
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
            "cost= json.loads(Path('cost.json').read_text()); "
            "cost['runs'].append({'input_tokens': 5, 'output_tokens': 7}); "
            "cost['total_input_tokens'] += 5; cost['total_output_tokens'] += 7; "
            "Path('cost.json').write_text(json.dumps(cost)); "
            f"Path('graph.json').write_bytes({_graph(second.generation)!r}); "
            "Path('manifest.json').write_text('{}')",
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


def test_interrupted_nested_publication_leaves_no_temporary_residue(tmp_path, monkeypatch):
    import graphify.transaction as transaction_module

    _root, output, tx, _token = _owner(tmp_path)
    real_replace = transaction_module.os.replace

    def interrupt_nested_replace(source, destination, *args, **kwargs):
        if destination == "index.md":
            raise OSError("nested replace interrupted")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(transaction_module.os, "replace", interrupt_nested_replace)
    with pytest.raises(OSError, match="nested replace interrupted"):
        commit_relative_bytes(tx, "wiki/index.md", b"payload")
    assert not (output / "wiki" / "index.md").exists()
    assert not list((output / "wiki").glob(".index.md.*.tmp"))


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


@pytest.mark.parametrize("entrypoint", ["queue", "recover"])
@pytest.mark.parametrize(
    "corruption", ["missing", "malformed", "generation", "pending_digest"]
)
def test_close_pending_replay_validates_receipt_before_any_new_mutation(
    tmp_path, entrypoint, corruption
):
    root, output = _close_pending_after_failpoint(tmp_path)
    if corruption == "pending_digest":
        drainer_path = output / ".graphify_drainer.json"
        pending = json.loads(drainer_path.read_text())
        pending["receipt_digest"] = "0" * 64
        drainer_path.write_text(
            json.dumps(pending, sort_keys=True, separators=(",", ":"))
        )
    else:
        _corrupt_generation_receipt(output, corruption)
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError, match="receipt"):
        if entrypoint == "queue":
            queue_rebuild("update", root, output=output, changed_paths=["late.py"])
        else:
            recover_close(output)

    assert _file_bytes(output) == before


@pytest.mark.parametrize("entrypoint", ["queue", "recover"])
@pytest.mark.parametrize(
    "corruption",
    [
        "substituted_token_identity",
        "replacement_token",
        "recreated_inflight",
        "unexpected_token_without_live",
    ],
)
def test_close_pending_replay_rejects_substituted_runtime_state_without_mutation(
    tmp_path, entrypoint, corruption
):
    root, output = _close_pending_after_failpoint(tmp_path)
    drainer_path = output / ".graphify_drainer.json"
    pending = json.loads(drainer_path.read_text())
    transaction_id = pending["transaction_id"]
    token_path = output / f".graphify_transaction_token.{transaction_id}"
    if corruption == "substituted_token_identity":
        pending["token_identity"]["inode"] += 1
        drainer_path.write_text(
            json.dumps(pending, sort_keys=True, separators=(",", ":"))
        )
    elif corruption == "replacement_token":
        token_path.unlink()
        token_path.write_bytes(b"replacement")
    elif corruption == "recreated_inflight":
        (output / f".graphify_rebuild_inflight.{transaction_id}.jsonl").write_bytes(
            b"recreated"
        )
    elif corruption == "unexpected_token_without_live":
        token_path.unlink()
        (output / ".graphify_transaction.json").unlink()
        (output / f".graphify_transaction_token.{'f' * 64}").write_bytes(
            b"unexpected"
        )
    before = _file_bytes(output)

    with pytest.raises(PendingTransactionError, match="token|inflight"):
        if entrypoint == "queue":
            queue_rebuild("update", root, output=output, changed_paths=["late.py"])
        else:
            recover_close(output)

    assert _file_bytes(output) == before


def test_recover_close_reconstructs_successor_after_complete_reserve_crash(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module

    root, output, tx, _token = _owner(tmp_path)
    _commit_owner_generation(output, tx)
    tx = resume_transaction(tx.id, root, output=output)
    queue_rebuild(
        "update", root, output=output, changed_paths=["late-after-complete.py"]
    )
    original = transaction_module._write_drainer
    crashed = False

    def crash_before_successor(capability, drainer, state, **extra):
        nonlocal crashed
        if state == "reserved" and not crashed:
            crashed = True
            raise RuntimeError("complete-before-successor")
        return original(capability, drainer, state, **extra)

    monkeypatch.setattr(transaction_module, "_write_drainer", crash_before_successor)
    with pytest.raises(RuntimeError, match="complete-before-successor"):
        finish_transaction(tx)
    monkeypatch.setattr(transaction_module, "_write_drainer", original)
    recover_close(output)
    recovered_drainer = json.loads((output / ".graphify_drainer.json").read_text())
    assert recovered_drainer["state"] == "reserved"
    successor = begin_transaction("runtime", root, output=output)
    claim = claim_rebuild_queue(successor, successor.drainer)
    assert any(
        isinstance(paths := item.get("changed_paths"), list)
        and "late-after-complete.py" in paths
        for item in claim.items
    )


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


def test_detached_merge_does_not_overwrite_replacement_winning_final_link(
    tmp_path, monkeypatch
):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_bytes(_graph(3))
    real_link = os.link
    injected = False

    def replace_before_link(src, dst, *args, **kwargs):
        nonlocal injected
        if dst == current.name and not injected:
            injected = True
            current.write_bytes(b"replacement")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", replace_before_link)
    with pytest.raises(PendingTransactionError, match="replacement won"):
        merge_detached_snapshots(base, current, other)
    assert current.read_bytes() == b"replacement"


def test_detached_merge_non_fileexists_competitor_cleans_exact_quarantine(
    tmp_path, monkeypatch
):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    for path in (base, current, other):
        path.write_bytes(_graph(3))
    real_link = os.link
    injected = False

    def competitor_then_io_error(src, dst, *args, **kwargs):
        nonlocal injected
        if dst == current.name and not injected:
            injected = True
            current.write_bytes(b"competitor")
            raise OSError("injected non-FileExists publication failure")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", competitor_then_io_error)
    with pytest.raises(OSError, match="injected"):
        merge_detached_snapshots(base, current, other)
    assert current.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".*.graphify-merge-backup.*"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_detached_merge_rejects_composed_node_cap_before_mutation(tmp_path):
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    other = tmp_path / "other.json"
    base.write_bytes(_graph(1))
    current_payload = json.loads(_graph(1))
    current_payload["nodes"] = [{"id": f"a-{index}"} for index in range(50_001)]
    current.write_text(json.dumps(current_payload), encoding="utf-8")
    other_payload = json.loads(_graph(1))
    other_payload["nodes"] = [{"id": f"b-{index}"} for index in range(50_000)]
    other.write_text(json.dumps(other_payload), encoding="utf-8")
    before = current.read_bytes()
    with pytest.raises(PendingTransactionError, match="composed.*node count"):
        merge_detached_snapshots(base, current, other)
    assert current.read_bytes() == before


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


def test_graph_reader_inventory_classifies_every_canonical_call_site():
    canonical = {
        ("affected.py", "load_graph", "affected"),
        ("benchmark.py", "run_benchmark", "benchmark"),
        ("build.py", "build_merge", "build-merge"),
        ("callflow_html.py", "load_graph", "callflow-html"),
        ("cli.py", "_stale_graph_sources", "stale-source-scan"),
        ("cli.py", "_prune_graph_json_sources", "source-prune"),
        ("cli.py", "_transactional_extract", "extract-baseline"),
        ("cli.py", "_transactional_cluster_only", "publication-prepare"),
        ("cli.py", "_transactional_export", "export-admission"),
        ("cli.py", "_dispatch_command", "query"),
        ("cli.py", "_dispatch_command", "path"),
        ("cli.py", "_dispatch_command", "explain"),
        ("cli.py", "_dispatch_command", "cluster-only"),
        ("cli.py", "_dispatch_command", "merge-graphs"),
        ("cli.py", "_dispatch_command", "export"),
        ("diagnostics.py", "_read_json_file", "diagnostics"),
        ("global_graph.py", "global_add", "global-add"),
        ("prs.py", "_load_graph_json", "pull-request-impact"),
        ("reflect.py", "_load_node_community", "reflect-community"),
        ("reflect.py", "_load_known_nodes", "reflect-known-nodes"),
        ("reflect.py", "reflect", "reflect"),
        ("reflect.py", "_build_id_label_maps", "reflect-projection"),
        ("serve.py", "_load_graph", "serve"),
        ("serve.py", "_load_ctx", "mcp-context-admission"),
        ("tree_html.py", "write_tree_html", "tree-prepare"),
        ("watch.py", "_rebuild_code", "watch-prepare"),
    }
    discovered: set[tuple[str, str, str]] = set()
    source_root = Path(__file__).parents[1] / "graphify"
    for path in source_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "open_graph_snapshot"
            ):
                continue
            owner: ast.AST | None = node
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            purpose = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "purpose"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            assert purpose is not None, (path.name, node.lineno)
            discovered.add((path.name, owner.name, purpose))
    assert discovered == canonical

    transaction_source = (source_root / "transaction.py").read_text(encoding="utf-8")
    transaction_functions = {
        node.name
        for node in ast.walk(ast.parse(transaction_source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "open_prepared_graph",  # prepared/private identity-bound workspace
        "_load_detached_merge_snapshot_with_identity",  # unmanaged/detached input
    } <= transaction_functions
