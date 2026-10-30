"""Administrator-approved checkpoint capture and exact new-output restoration.

No path to a failed output is accepted by restore. OS-protected current heads,
not the checkpoint's old coordination records, authorize new publication.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

try:
    import fcntl
except ImportError:  # Unsupported hosts still get a structured refusal.
    fcntl = None

from . import recovery_authority as authority
from . import transaction as tx

CHECKPOINT_SCHEMA = "graphify.checkpoint.v1"
HEAD_SCHEMA = "graphify.checkpoint-head.v1"
TRANSFORM = "watermark-v1"
CLAIM = ".graphify_restore_claim.json"
SEAL = ".graphify_restore_seal.json"
MAX_METADATA = 8 * tx._MAX_STATE_BYTES
PROVENANCE = ".graphify_restore_provenance.json"
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, message):
    if not condition:
        raise tx.PendingTransactionError(message)


def _present[T](value: T | None, message: str) -> T:
    if value is None:
        raise tx.PendingTransactionError(message)
    return value


def _fcntl_module():
    module = fcntl
    if module is None:
        raise tx.PendingTransactionError("checkpoint locking requires a supported POSIX host")
    return module


def _hex(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _json(payload, limit=MAX_METADATA):
    _require(type(payload) is bytes and len(payload) <= limit, "checkpoint metadata exceeds bound")

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate checkpoint field")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=pairs,
                          parse_constant=lambda _: _require(False, "nonfinite checkpoint number"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise tx.PendingTransactionError("malformed checkpoint JSON") from exc


def _encode(value, limit=MAX_METADATA):
    payload = authority.canonical_bytes(value)
    _require(len(payload) <= limit, "checkpoint metadata exceeds bound")
    return payload


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _inventory(payloads):
    return {name: {"sha256": _sha(body), "size": len(body)}
            for name, body in sorted(payloads.items())}


def _canonical_directory(value):
    _require(type(value) is str and len(value.encode()) <= 4096, "invalid checkpoint directory")
    path = Path(value)
    _require(path.is_absolute() and str(path) == value and path.resolve(strict=True) == path,
             "checkpoint directory must be canonical and absolute")
    with tx.pin_output(path, mutation=False) as cap:
        return path, cap.identity.json()


def _request_fields(raw):
    _require(isinstance(raw, dict) and set(raw) == {"schema", "source_id", "root", "output"}
             and type(raw["schema"]) is int and raw["schema"] == 1 and _hex(raw["source_id"]),
             "invalid restore request")
    root_value = raw["root"]
    _require(type(root_value) is str and len(root_value.encode()) <= 4096,
             "invalid checkpoint directory")
    root = Path(root_value)
    _require(root.is_absolute() and str(root) == root_value
             and os.path.abspath(root) == root_value, "invalid checkpoint directory")
    value = raw["output"]
    _require(type(value) is str and len(value.encode()) <= 4096, "invalid restore destination")
    output = Path(value)
    _require(output.is_absolute() and str(output) == value and output.name not in {"", ".", ".."},
             "invalid restore destination")
    _require(output != root and output not in root.parents, "restore destination contains source root")
    return dict(raw)


def _request(raw):
    request = _request_fields(raw)
    _root, root_id = _canonical_directory(request["root"])
    _parent, parent_id = _canonical_directory(str(Path(request["output"]).parent))
    return request, root_id, parent_id


@contextlib.contextmanager
def _pinned_root(plan):
    with tx.pin_output(plan["request"]["root"], mutation=False) as root:
        _require(root.identity.json() == plan["root_identity"], "restore source root identity changed")
        expected_root = Path(plan["request"]["root"])
        tx._validate_root_capability(root, expected_root)
        yield root
        tx._validate_root_capability(root, expected_root)


@contextlib.contextmanager
def _locked(cap, deadline):
    """Bound both thread and process lock acquisition; nested tx calls reenter."""
    locking = _fcntl_module()
    local = tx._lock_for(cap)
    _require(local.acquire(timeout=max(0, deadline - time.monotonic())), "restore lock deadline")
    fd = os.dup(cap.fd)
    try:
        while True:
            try:
                locking.flock(fd, locking.LOCK_EX | locking.LOCK_NB)
                break
            except BlockingIOError:
                _require(time.monotonic() < deadline, "restore lock deadline")
                time.sleep(0.01)
        with tx._locked(cap):
            yield
    finally:
        locking.flock(fd, locking.LOCK_UN)
        os.close(fd)
        local.release()


def _tick(deadline):
    _require(time.monotonic() < deadline, "checkpoint operation deadline")


def _source_signature(cap, name):
    """Inspect every source component with descriptors and reject linked data."""
    parts = Path(name).parts
    fd = os.dup(cap.fd)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                 "checkpoint source contains a nonregular or hard-linked entry")
        return tx._stat_signature(info)
    finally:
        os.close(fd)


def _head(store, source_id):
    _require(_hex(source_id), "invalid checkpoint source ID")
    try:
        payload = store.read(f"heads/{source_id}.json", tx._MAX_STATE_BYTES)
    except FileNotFoundError:
        return None, "absent"
    raw = _json(payload)
    fields = {"schema", "source_id", "sequence", "enabled", "checkpoint", "previous", "generation", "output_identity"}
    _require(isinstance(raw, dict) and set(raw) == fields and raw["schema"] == HEAD_SCHEMA
             and raw["source_id"] == source_id and type(raw["sequence"]) is int
             and 0 < raw["sequence"] < 2**63 and type(raw["enabled"]) is bool
             and _hex(raw["checkpoint"]) and (raw["previous"] == "absent" or _hex(raw["previous"]))
             and type(raw["generation"]) is int and 0 < raw["generation"] < 2**63,
             "malformed protected checkpoint head")
    tx._identity_from_json(raw["output_identity"])
    return raw, authority.digest(HEAD_SCHEMA, payload)


@contextlib.contextmanager
def _capture_source(output):
    # Qualify installed code/config before source access. Keep the source pinned,
    # but release the store lock before taking its output lock.
    with contextlib.ExitStack() as stack:
        with authority.open_store(write=True) as store:
            cap = stack.enter_context(tx.pin_output(output, mutation=False))
            _require(not store.overlaps(cap.fd), "capture source overlaps authority store")
        yield cap


def capture(source_id, root, output, expected_head, *, failpoint=None):
    """Approve a healthy generation. Only the installed administrator tool may write."""
    _require(_hex(source_id) and (expected_head == "absent" or _hex(expected_head)), "invalid capture selector")
    deadline = time.monotonic() + 120
    with _capture_source(output) as cap, _locked(cap, deadline):
        root, _root_id = _canonical_directory(str(root))
        protocol = tx._read_protocol(cap)
        _require(protocol is not None and protocol["root"] == str(root)
                 and "restore_binding" not in protocol and "bootstrap_binding" not in protocol
                 and "replacement_binding" not in protocol,
                 "capture requires a closed ordinary output at the selected root")
        entries = set(tx._list_entries(cap))
        _require(not entries.intersection(tx._COORDINATION_FILES - {
            tx.PROTOCOL_FILE, tx.RECEIPT_FILE, tx.DRAINER_FILE, tx.PREDECESSOR_FILE}), "capture refuses pending coordination")
        _require(not any(name.startswith(".graphify_transaction_token.") for name in entries),
                 "capture refuses live tokens")
        receipt, receipt_digest, artifacts = tx._validate_receipt_locked(cap, require_closed=True,
                                                                       retain_artifacts=("*",))
        predecessor = tx._read_predecessor_authority(cap)
        if predecessor is not None:
            _require(predecessor[3]["state"] == "preserved-complete" and predecessor[0] == protocol
                     and predecessor[1] == tx._read_drainer(cap) and predecessor[2] == receipt_digest,
                     "capture refuses pending or mismatched predecessor")
        _require(receipt.get("graph_name", "graph.json") == "graph.json" and PROVENANCE not in artifacts,
                 "unsupported checkpoint inventory")
        record_names = [tx.PROTOCOL_FILE, tx.RECEIPT_FILE, tx.DRAINER_FILE]
        if predecessor is not None:
            record_names.append(tx.PREDECESSOR_FILE)
        signatures = {name: _source_signature(cap, name) for name in [*record_names, *artifacts]}
        records = {name: tx._read_bytes(cap, name) for name in record_names}
        checkpoint = {"schema": CHECKPOINT_SCHEMA, "source_id": source_id, "root": str(root),
                      "captured_ns": time.time_ns(), "output_identity": cap.identity.json(),
                      "generation": receipt["generation"], "transaction_id": receipt["transaction_id"],
                      "receipt_sha256": receipt_digest,
                      "records": {name: body.hex() for name, body in records.items()},
                      "inventory": _inventory(artifacts)}
        payload = _encode(checkpoint)
        _checkpoint(checkpoint, artifacts)
        _tick(deadline)
        _again, again_digest, _ = tx._validate_receipt_locked(cap, require_closed=True)
        _require(again_digest == receipt_digest and all(tx._read_bytes(cap, n) == b for n, b in records.items()),
                 "checkpoint source changed during capture")
        _require(all(_source_signature(cap, name) == signature for name, signature in signatures.items()),
                 "checkpoint source entry changed during capture")
        cap.validate()
    # No source lock is held while acquiring the authority writer lock.
    with authority.open_store(write=True) as store:
        previous, current = _head(store, source_id)
        _require(current == expected_head, "checkpoint head changed before approval")
        if previous is not None:
            _require(previous["output_identity"] == checkpoint["output_identity"]
                     and checkpoint["generation"] > previous["generation"],
                     "checkpoint lineage changed or generation is not newer")
        digest = authority.digest(CHECKPOINT_SCHEMA, payload)
        slot = f"checkpoints/{digest}"
        store.create_directory(slot)
        store.write_new(slot + "/checkpoint.json", payload)
        for name, body in artifacts.items():
            _tick(deadline)
            parent = str(Path(slot + "/artifacts/" + name).parent)
            if not store.exists(parent):
                store.create_directory(parent)
            store.write_new(slot + "/artifacts/" + name, body)
        store.fsync()
        if failpoint:
            failpoint("before_head")
        head = {"schema": HEAD_SCHEMA, "source_id": source_id,
                "sequence": 1 if previous is None else previous["sequence"] + 1,
                "enabled": True, "checkpoint": digest, "previous": current,
                "generation": checkpoint["generation"], "output_identity": checkpoint["output_identity"]}
        if not store.exists("heads"):
            store.create_directory("heads")
        _tick(deadline)
        head_payload = _encode(head)
        store.replace(f"heads/{source_id}.json", head_payload)
        return {"status": "approved", "checkpoint_sha256": digest,
                "head_sha256": authority.digest(HEAD_SCHEMA, head_payload)}


def revoke(source_id, expected_head):
    with authority.open_store(write=True) as store:
        head, digest = _head(store, source_id)
        _require(head is not None and digest == expected_head, "stale checkpoint revocation")
        head = _present(head, "stale checkpoint revocation")
        head = {**head, "enabled": False, "sequence": head["sequence"] + 1, "previous": digest}
        payload = _encode(head)
        store.replace(f"heads/{source_id}.json", payload)
        return {"status": "revoked", "head_sha256": authority.digest(HEAD_SCHEMA, payload)}


def _checkpoint(raw, artifacts):
    fields = {"schema", "source_id", "root", "captured_ns", "output_identity", "generation",
              "transaction_id", "receipt_sha256", "records", "inventory"}
    _require(isinstance(raw, dict) and set(raw) == fields and raw["schema"] == CHECKPOINT_SCHEMA
             and _hex(raw["source_id"]) and _hex(raw["transaction_id"]) and _hex(raw["receipt_sha256"])
             and type(raw["root"]) is str and type(raw["captured_ns"]) is int and raw["captured_ns"] > 0
             and type(raw["generation"]) is int and 0 < raw["generation"] < 2**63,
             "malformed approved checkpoint")
    tx._identity_from_json(raw["output_identity"])
    core_records = {tx.PROTOCOL_FILE, tx.RECEIPT_FILE, tx.DRAINER_FILE}
    _require(isinstance(raw["records"], dict) and set(raw["records"]) in (
        core_records, core_records | {tx.PREDECESSOR_FILE}), "checkpoint records incomplete")
    try:
        records = {name: bytes.fromhex(value) for name, value in raw["records"].items()}
    except (ValueError, TypeError) as exc:
        raise tx.PendingTransactionError("malformed checkpoint record bytes") from exc
    _require(_sha(records[tx.RECEIPT_FILE]) == raw["receipt_sha256"], "checkpoint receipt digest changed")
    parsed = {name: _json(body, tx._MAX_STATE_BYTES) for name, body in records.items()}
    for record in parsed.values():
        _require(isinstance(record, dict) and record.get("output_identity") == raw["output_identity"],
                 "checkpoint inert identity changed")
    receipt = parsed[tx.RECEIPT_FILE]
    _require(receipt.get("generation") == raw["generation"] and receipt.get("transaction_id") == raw["transaction_id"]
             and receipt.get("artifact_digests") == {n: _sha(b) for n, b in artifacts.items()},
             "checkpoint receipt inventory changed")
    _require(raw["inventory"] == _inventory(artifacts), "checkpoint artifact bytes changed")
    _require(0 < len(artifacts) < tx._MAX_RECEIPT_ARTIFACTS and PROVENANCE not in artifacts,
             "checkpoint artifact count or reserved name")
    for name in artifacts:
        tx._validated_relative_name(name)
        # Storage adds three path components and stricter Unicode/name bounds.
        authority._parts("checkpoints/" + "0" * 64 + "/artifacts/" + name)
    tx._reject_casefold_collisions(tuple(artifacts) + (PROVENANCE,))
    _require(sum(map(len, artifacts.values())) + len(_encode(raw)) < tx._MAX_RECEIPT_AGGREGATE_BYTES,
             "checkpoint aggregate exceeds bound")
    _transformed(artifacts)


def _transformed(artifacts):
    _require("graph.json" in artifacts and "manifest.json" in artifacts, "checkpoint graph/manifest missing")
    graph = _json(artifacts["graph.json"], tx._MAX_RECEIPT_AGGREGATE_BYTES)
    _require(isinstance(graph, dict) and isinstance(graph.get("graph"), dict)
             and isinstance(graph.get("nodes"), list)
             and (isinstance(graph.get("links"), list) or isinstance(graph.get("edges"), list)),
             "unsupported checkpoint graph")
    tx._watermark(artifacts["graph.json"])
    _require(isinstance(_json(artifacts["manifest.json"], tx._MAX_RECEIPT_AGGREGATE_BYTES), dict),
             "unsupported checkpoint manifest")
    graph["graph"][tx.GRAPH_WATERMARK_KEY] = {"schema": 1, "protocol_epoch": 1, "generation": 1, "state": "active"}
    return {**artifacts, "graph.json": json.dumps(graph, sort_keys=True, ensure_ascii=True,
                                                 separators=(",", ":"), allow_nan=False).encode("ascii")}


def _load(store, source_id, deadline):
    head, head_digest = _head(store, source_id)
    _require(head is not None and head["enabled"], "no enabled approved checkpoint")
    head = _present(head, "no enabled approved checkpoint")
    slot = "checkpoints/" + head["checkpoint"]
    payload = store.read(slot + "/checkpoint.json", MAX_METADATA)
    _require(authority.digest(CHECKPOINT_SCHEMA, payload) == head["checkpoint"], "checkpoint digest changed")
    checkpoint = _json(payload)
    _require(isinstance(checkpoint, dict) and isinstance(checkpoint.get("inventory"), dict)
             and 0 < len(checkpoint["inventory"]) < tx._MAX_RECEIPT_ARTIFACTS,
             "malformed checkpoint inventory")
    artifacts = {}
    remaining = tx._MAX_RECEIPT_AGGREGATE_BYTES - len(payload)
    for name, info in checkpoint["inventory"].items():
        _tick(deadline)
        tx._validated_relative_name(name)
        _require(isinstance(info, dict) and set(info) == {"sha256", "size"} and _hex(info["sha256"])
                 and type(info["size"]) is int and 0 <= info["size"] <= remaining,
                 "invalid checkpoint artifact bound")
        body = store.read(slot + "/artifacts/" + name, min(remaining, tx._artifact_read_limit(Path(name).name)))
        _require(len(body) == info["size"] and _sha(body) == info["sha256"], "checkpoint artifact changed")
        artifacts[name] = body
        remaining -= len(body)
    _checkpoint(checkpoint, artifacts)
    _require(checkpoint["source_id"] == source_id and checkpoint["generation"] == head["generation"]
             and checkpoint["output_identity"] == head["output_identity"], "checkpoint head binding changed")
    return head, head_digest, checkpoint, artifacts


def _plan(store, request, deadline, *, require_absent):
    request, root_id, parent_id = _request(request)
    with tx.pin_output(Path(request["output"]).parent, mutation=False) as parent:
        authority.qualify_destination(parent.fd)
        _require(not store.overlaps(parent.fd), "restore destination overlaps authority store")
        if require_absent:
            _require(tx._entry_nofollow_stat(parent, Path(request["output"]).name) is None,
                     "restore requires an absent destination")
    head, head_digest, checkpoint, artifacts = _load(store, request["source_id"], deadline)
    _require(checkpoint["root"] == request["root"], "checkpoint source relocation is unsupported")
    payloads = _transformed(artifacts)
    plan = {"schema": 1, "request": request, "uid": os.getuid(), "root_identity": root_id,
            "parent_identity": parent_id, "config_sha256": store.config_digest,
            "store_identity": store.identity, "head_sha256": head_digest,
            "checkpoint_sha256": head["checkpoint"], "approval_sequence": head["sequence"],
            "captured_ns": checkpoint["captured_ns"], "original_generation": checkpoint["generation"],
            "transform": TRANSFORM, "inventory": _inventory(payloads),
            "provenance_recipe": "checkpoint-origin-v1-with-new-output-identity"}
    fingerprint = authority.digest("graphify.restore-plan.v1", _encode(plan))
    _preflight(plan, fingerprint, checkpoint, payloads)
    return plan, fingerprint, checkpoint, payloads


def preview(request):
    with authority.open_store() as store:
        plan, fingerprint, _checkpoint_data, _payloads = _plan(store, request, time.monotonic() + 120,
                                                             require_absent=True)
        return {"status": "preview", "preview_sha256": fingerprint, "plan": plan,
                "data_loss": "Restores the latest approved checkpoint; later source changes are not recovered.",
                "derived_artifact": PROVENANCE}


def _proof(plan, fingerprint, checkpoint, payloads, identity):
    return {"schema": 1, "mode": "checkpoint-restore", "request": plan["request"],
            "preview_sha256": fingerprint, "checkpoint_sha256": plan["checkpoint_sha256"],
            "head_sha256": plan["head_sha256"], "approval_sequence": plan["approval_sequence"],
            "source_id": checkpoint["source_id"], "original_identity": checkpoint["output_identity"],
            "original_generation": checkpoint["generation"], "original_receipt_sha256": checkpoint["receipt_sha256"],
            "output_identity": identity, "generation": 1, "transform": TRANSFORM,
            "before": checkpoint["inventory"], "after": _inventory(payloads)}


def _preflight(plan, fingerprint, checkpoint, payloads):
    """Charge the largest identity/owner representation before creating output.

    These upper-bound values are encoding measurements, never capabilities or
    inputs to an admission validator.
    """
    identity = {"device": 2**64 - 1, "inode": 2**64 - 1}
    proof = _encode(_proof(plan, fingerprint, checkpoint, payloads, identity), tx._MAX_STATE_BYTES)
    publication = {**payloads, PROVENANCE: proof}
    tx._validated_publication_plan(tx.PublicationPlan(publication))
    binding = tx.RESTORE_BINDING_PREFIX + _sha(proof)
    owner = {"schema": 1, "protocol_epoch": 1, "id": "f" * 64, "kind": "full",
             "root": plan["request"]["root"], "output": plan["request"]["output"],
             "phase": "bootstrap-recovered", "pid": 2**63 - 1, "generation": 1,
             "output_identity": identity, "token_digest": "f" * 64,
             "token_identity": {"device": 2**64 - 1, "inode": "9" * 39, "birthtime_ns": "9" * 39},
             "drainer": {"generation": 1, "claim_epoch": 2**63 - 1, "launch_nonce": "f" * 32},
             "restore_binding": binding}
    _encode({"schema": 1, "request": plan["request"], "plan": plan,
             "preview_sha256": fingerprint, "output_identity": identity,
             "binding": binding, "owner": owner}, tx._MAX_STATE_BYTES)
    _encode({"schema": 1, "binding": binding, "inventory": _inventory(publication),
             "workspace_identity": identity, "child_identity": identity}, tx._MAX_STATE_BYTES)


@contextlib.contextmanager
def _owner(owner, binding):
    previous = tx._AUTHORITY.set(tx._authority_for(owner))
    context = tx._RESTORE_AUTHORITY.set(binding)
    try:
        yield
    finally:
        tx._AUTHORITY.reset(previous)
        tx._RESTORE_AUTHORITY.reset(context)


def _publish(cap, claim, payloads, deadline, failpoint, root_capability):
    root_path = Path(claim["request"]["root"])
    tx._validate_root_capability(root_capability, root_path)
    binding = claim["binding"]
    with contextlib.ExitStack() as stack:
        context = tx._RESTORE_AUTHORITY.set(binding)
        stack.callback(tx._RESTORE_AUTHORITY.reset, context)
        protocol = tx._read_protocol(cap)
        _require(protocol is not None and protocol.get("restore_binding") == binding
                 and protocol.get("transaction_id") == claim["owner"]["id"]
                 and protocol.get("owner_capability_digest") == claim["owner"]["token_digest"],
                 "restore owner changed")
        protocol = _present(protocol, "restore owner changed")
        if protocol["state"] == "COMPLETE":
            tx._validate_receipt_locked(cap)
            drainer = _present(tx._read_drainer(cap), "restore closure drainer missing")
            if drainer[1] == "CLOSE_PENDING":
                tx.recover_close(cap.path)
            elif drainer[1] != "complete":
                owner = tx._read_transaction(cap)
                owner = _present(owner, "restore closure owner missing")
                with _owner(owner, binding):
                    tx._finish_transaction(owner, failpoint=failpoint)
        else:
            owner = tx._read_transaction(cap)
            _require(owner is not None and tx._transaction_from_json(cap, claim["owner"]) == owner,
                     "restore exact owner changed")
            owner = _present(owner, "restore exact owner changed")
            with _owner(owner, binding):
                seal = tx._load_json(cap, SEAL)
                if seal is None:
                    prepared = tx._pin_prepared_workspace(owner, cap)
                    try:
                        for name, body in payloads.items():
                            _tick(deadline)
                            tx._replace_relative_bytes(prepared.output, name, body)
                        seal = {"schema": 1, "binding": binding, "inventory": _inventory(payloads),
                                "workspace_identity": prepared.workspace.identity.json(),
                                "child_identity": prepared.output.identity.json()}
                        tx._create_bytes(cap, SEAL, _encode(seal, tx._MAX_STATE_BYTES))
                    finally:
                        prepared.close()
                    if failpoint:
                        failpoint("after_restore_seal")
                _require(isinstance(seal, dict) and set(seal) == {
                    "schema", "binding", "inventory", "workspace_identity", "child_identity"}
                    and seal["schema"] == 1 and seal["binding"] == binding
                    and seal["inventory"] == _inventory(payloads), "restore seal changed")
                prepared = tx._pin_prepared_workspace(owner, cap)
                try:
                    _require(prepared.workspace.identity.json() == seal["workspace_identity"]
                             and prepared.output.identity.json() == seal["child_identity"], "restore stage replaced")
                    for name, body in payloads.items():
                        _require(tx._read_relative_bytes(prepared.output, name) == body, "restore staged bytes changed")
                finally:
                    prepared.close()
                tx._validate_root_capability(root_capability, root_path)
                tx._commit_publication_plan(owner, tx.PublicationPlan(payloads), failpoint=failpoint)
                tx._validate_root_capability(root_capability, root_path)
                tx._finish_transaction(owner, failpoint=failpoint)
        tx._validate_root_capability(root_capability, root_path)
        _tick(deadline)
        tx._validate_receipt_locked(cap, require_closed=True)
        tx._retire_prepared_locked(cap)
        if failpoint:
            failpoint("after_restore_retirement")
        receipt, digest, _ = tx._validate_receipt_locked(cap, require_closed=True)
        return {"status": "complete", "receipt_sha256": digest, "generation": receipt["generation"],
                "output_identity": cap.identity.json(), "preview_sha256": claim["preview_sha256"]}


def execute(request, preview_sha256, *, failpoint=None):
    _require(_hex(preview_sha256), "invalid restore preview digest")
    deadline = time.monotonic() + 120
    with authority.open_store() as store:
        request, _, _ = _request(request)
        output = Path(request["output"])
        with tx.pin_output(output.parent, mutation=False) as parent, _locked(parent, deadline):
            plan, fingerprint, checkpoint, payloads = _plan(store, request, deadline, require_absent=True)
            _require(fingerprint == preview_sha256, "restore preview changed")
            with _pinned_root(plan) as root_capability:
                parent.validate()
                os.mkdir(output.name, mode=0o700, dir_fd=parent.fd)
                os.fsync(parent.fd)
                created = os.stat(output.name, dir_fd=parent.fd, follow_symlinks=False)
                parent.validate()
                if failpoint:
                    failpoint("after_restore_directory")
                with tx.pin_output(output) as cap, _locked(cap, deadline):
                    parent.validate()
                    _require(cap.identity == tx.OutputIdentity(created.st_dev, created.st_ino),
                             "new restore directory replaced before claim")
                    proof = _encode(_proof(plan, fingerprint, checkpoint, payloads, cap.identity.json()), tx._MAX_STATE_BYTES)
                    payloads = {**payloads, PROVENANCE: proof}
                    tx._validated_publication_plan(tx.PublicationPlan(payloads))
                    binding = tx.RESTORE_BINDING_PREFIX + _sha(proof)
                    context = tx._RESTORE_AUTHORITY.set(binding)
                    try:
                        owner = tx.begin_transaction(
                            "full", request["root"], output=output, restore_binding=binding,
                            root_capability=root_capability,
                        )
                        with _owner(owner, binding):
                            tx.stage_transaction_handoff(owner)
                        tx._validate_root_capability(root_capability, Path(request["root"]))
                        owner = tx._read_transaction(cap)
                        owner = _present(owner, "restore handoff owner missing")
                        claim = {"schema": 1, "request": request, "plan": plan,
                                 "preview_sha256": fingerprint, "output_identity": cap.identity.json(),
                                 "binding": binding, "owner": tx._transaction_json(owner, phase=owner.phase)}
                        tx._create_bytes(cap, CLAIM, _encode(claim, tx._MAX_STATE_BYTES))
                        if failpoint:
                            failpoint("after_restore_claim")
                        return _publish(cap, claim, payloads, deadline, failpoint, root_capability)
                    finally:
                        tx._RESTORE_AUTHORITY.reset(context)


def _claim(cap, request, preview_sha256, expected_identity, expected_owner):
    _require(cap.identity.json() == expected_identity, "stale restore output selector")
    claim = tx._load_json(cap, CLAIM)
    _require(isinstance(claim, dict) and set(claim) == {
        "schema", "request", "plan", "preview_sha256", "output_identity", "binding", "owner"}
        and type(claim["schema"]) is int and claim["schema"] == 1 and claim["request"] == request
        and claim["preview_sha256"] == preview_sha256 and claim["output_identity"] == expected_identity
        and isinstance(claim["owner"], dict) and claim["owner"].get("id") == expected_owner,
        "restore original claim/selector changed")
    claim = _present(claim, "restore original claim/selector changed")
    _require(authority.digest("graphify.restore-plan.v1", _encode(claim["plan"])) == preview_sha256,
             "restore claim plan changed")
    return claim


def resume(request, preview_sha256, output_identity, owner_id, *, failpoint=None):
    _require(_hex(preview_sha256) and _hex(owner_id), "invalid restore resume selector")
    tx._identity_from_json(output_identity)
    request = _request_fields(request)
    output = Path(request["output"])
    deadline = time.monotonic() + 120
    # Observe completed exact retries without relying on a still-enabled head.
    with tx.pin_output(output, mutation=False) as cap, _locked(cap, deadline):
        claim = _claim(cap, request, preview_sha256, output_identity, owner_id)
        raw = tx._load_json(cap, tx.PROTOCOL_FILE)
        drainer = tx._read_drainer(cap)
        if (raw and raw.get("state") == "COMPLETE" and drainer and drainer[1] == "complete"
                and tx._entry_stat(cap, tx.TRANSACTION_FILE) is None and tx._entry_stat(cap, tx.PREPARED_FILE) is None):
            protocol = tx._read_protocol(cap)
            protocol = _present(protocol, "completed restore owner changed")
            _require(protocol.get("restore_binding") == claim["binding"] and protocol["transaction_id"] == owner_id,
                     "completed restore owner changed")
            receipt, digest, _ = tx._validate_receipt_locked(cap, require_closed=True)
            return {"status": "already_complete", "receipt_sha256": digest, "generation": receipt["generation"]}
    with authority.open_store() as store:
        with tx.pin_output(output.parent, mutation=False) as parent, _locked(parent, deadline):
            plan, fingerprint, checkpoint, payloads = _plan(store, request, deadline, require_absent=False)
            _require(fingerprint == preview_sha256, "restore approved head or preview changed")
            with (_pinned_root(plan) as root_capability,
                  tx.pin_output(output) as cap, _locked(cap, deadline)):
                claim = _claim(cap, request, preview_sha256, output_identity, owner_id)
                _require(claim["plan"] == plan, "restore claim plan changed")
                proof = _encode(_proof(plan, fingerprint, checkpoint, payloads, cap.identity.json()), tx._MAX_STATE_BYTES)
                _require(claim["binding"] == tx.RESTORE_BINDING_PREFIX + _sha(proof), "restore provenance changed")
                return _publish(cap, claim, {**payloads, PROVENANCE: proof}, deadline, failpoint, root_capability)


def _read_request(path):
    """Read a bounded single-link regular request without following its final name."""
    _require(os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
             "checkpoint request input requires supported no-follow file semantics")
    limit = 1024 * 1024
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 <= before.st_size <= limit,
                 "checkpoint request must be a bounded regular file with one link")
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        _require(remaining > 0 and limit + 1 - remaining == after.st_size
                 and tx._stat_signature(before) == tx._stat_signature(after)
                 and tx._stat_signature(after) == tx._stat_signature(named),
                 "checkpoint request changed or exceeded its read bound")
        return _json(b"".join(chunks), limit)
    finally:
        os.close(fd)


def run_cli(argv=None):
    parser = argparse.ArgumentParser(prog="graphify checkpoint")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("capture", "revoke"):
        p = sub.add_parser(action)
        p.add_argument("--source-id", required=True)
        p.add_argument("--expected-head", required=True)
        if action == "capture":
            p.add_argument("--root", required=True)
            p.add_argument("--output", required=True)
    for action in ("preview", "restore", "resume"):
        p = sub.add_parser(action)
        p.add_argument("--request", required=True)
        if action != "preview":
            p.add_argument("--preview-sha256", required=True)
        if action == "resume":
            for option in ("device", "inode", "owner-id"):
                p.add_argument("--" + option, required=True, type=int if option != "owner-id" else str)
    args = parser.parse_args(argv)
    try:
        if args.action == "capture":
            result = capture(args.source_id, args.root, args.output, args.expected_head)
        elif args.action == "revoke":
            result = revoke(args.source_id, args.expected_head)
        else:
            request = _read_request(args.request)
            if args.action == "preview":
                result = preview(request)
            elif args.action == "restore":
                result = execute(request, args.preview_sha256)
            else:
                result = resume(request, args.preview_sha256, {"device": args.device, "inode": args.inode}, args.owner_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (tx.PendingTransactionError, authority.RecoveryAuthorityError, OSError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)[:1024]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run_cli())
