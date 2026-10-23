"""Explicit, fresh structural bootstrap of a receiptless legacy graph.

The output lock spans admission, preparation and publication. Source writers are
not locked. A seal freezes exact owner and artifact bytes; an incomplete public
write is replayed with the original token and never by adopting a new baseline.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
from pathlib import Path

from graphify import legacy_source as source
from graphify import transaction as tx
from graphify.source_io import SourceError


PROVENANCE = ".graphify_bootstrap.json"
INPUTS = ".graphify_bootstrap_inputs.json"


def _json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _bounded_json(value):
    payload = _json(value)
    if len(payload) > tx._MAX_STATE_BYTES:
        raise tx.PendingTransactionError("bootstrap evidence exceeds state bound")
    return payload


def _fingerprint(request, evidence_digest, legacy_digest, identity):
    return _digest({
        "mode": tx._BOOTSTRAP_BINDING_PREFIX, "request": request,
        "source_evidence": evidence_digest, "legacy_inventory": legacy_digest,
        "output_identity": identity.json(), "generation": 1,
    })


def _binding(fingerprint):
    return tx._validate_bootstrap_binding(tx._BOOTSTRAP_BINDING_PREFIX + fingerprint)


def _collision_check(capability, payloads, legacy):
    entries = tx._list_entries(capability)
    tx._reject_casefold_collisions((*entries, *payloads))
    for name in payloads:
        if name not in legacy and tx._entry_nofollow_stat(capability, name) is not None:
            raise tx.PendingTransactionError(f"bootstrap would replace unmanaged artifact: {name}")


def _provenance(request, built, binding, identity, legacy_digest):
    return _bounded_json({
        "schema": 1, "mode": "trusted-legacy-bootstrap", "generation": 1,
        "bootstrap_binding": binding, "request": request,
        "output_identity": identity.json(),
        "source_evidence_sha256": built.evidence_digest,
        "legacy_inventory_sha256": legacy_digest,
        "selected": list(built.selected), "outcomes": built.outcomes,
        "uncovered": built.uncovered,
    })


def _preflight_state(capability, request, built, legacy, binding, legacy_digest):
    """Refuse unpersistable bootstrap state before acquiring generation authority."""
    _bounded_json({"request": request, "evidence": built.evidence})
    # The prepared marker repeats legacy paths in prior_inventory and seal
    # deletions. Charge transaction JSON's ASCII escaping (including Unicode),
    # reserving half the state budget for fixed owner fields and five core entries.
    if len(tx._json_bytes(sorted(legacy))) > tx._MAX_STATE_BYTES // 4:
        raise tx.PendingTransactionError("bootstrap legacy paths exceed state bound")
    _provenance(request, built, binding, capability.identity, legacy_digest)


def preview(request):
    """Read-only eligibility, complete extraction and reproducible request digest."""
    request = source.validate_request(request)
    with tx.pin_output(request["output"], mutation=False) as cap, tx._locked(cap):
        legacy = tx.legacy_bootstrap_inventory(cap)
        built = source.build(request)
        source.reobserve(request, built.evidence)
        digest = tx._legacy_update_inventory_digest(legacy)
        tx._validate_legacy_update_inventory_locked(cap, digest)
        _collision_check(cap, {**built.payloads, PROVENANCE: b""}, legacy)
        fingerprint = _fingerprint(request, built.evidence_digest, digest, cap.identity)
        _preflight_state(cap, request, built, legacy, _binding(fingerprint), digest)
        publication_names = set(built.payloads) | {PROVENANCE}
        return {
            "status": "preview", "preview_sha256": fingerprint, "generation": 1,
            "request": request, "selected": list(built.selected),
            "outcomes": built.outcomes, "uncovered": built.uncovered,
            "preserve_legacy": sorted(legacy),
            "replace": sorted(publication_names),
            "delete_active": sorted(set(legacy) - publication_names),
            "source_observation": "reobserved current; not an atomic filesystem snapshot",
        }


@contextlib.contextmanager
def _owner_context(owner):
    previous = tx._AUTHORITY.set(tx._authority_for(owner))
    try:
        yield
    finally:
        tx._AUTHORITY.reset(previous)


def _operational(prepared):
    prepared.validate()
    identity = prepared.workspace.identity
    return {prepared.workspace.path: (identity.device, identity.inode)}


def _open_owner(capability, request, binding, token_path=None):
    owner = tx._read_transaction(capability)
    if owner is None or owner.bootstrap_binding != binding or owner.root != request["root"]:
        raise tx.PendingTransactionError("bootstrap owner or original request changed")
    path = capability.path / f".graphify_transaction_token.{owner.id}"
    if token_path is not None and Path(token_path).absolute() != path:
        raise tx.PendingTransactionError("bootstrap token selector changed")
    tx._open_token(path)
    tx._validate_durable_live_binding(capability, owner,
        allowed_protocol_states=frozenset({"INCOMPLETE", "COMPLETE"}))
    return owner


def _completed(capability, request, binding, *, token_path=None):
    protocol = tx._read_protocol(capability)
    if protocol is None or protocol["state"] != "COMPLETE":
        return None
    if protocol.get("bootstrap_binding") != binding or protocol["generation"] != 1:
        raise tx.PendingTransactionError("completed bootstrap selector is stale")
    receipt, receipt_digest, inventory = tx._validate_receipt_locked(
        capability, retain_artifacts=(PROVENANCE,))
    try:
        proof = json.loads(inventory[PROVENANCE])
    except (KeyError, ValueError, TypeError) as exc:
        raise tx.PendingTransactionError("receipt-bound bootstrap provenance is missing") from exc
    if (proof.get("request") != request or proof.get("bootstrap_binding") != binding
            or proof.get("output_identity") != capability.identity.json()
            or proof.get("generation") != 1):
        raise tx.PendingTransactionError("completed bootstrap request changed")
    if token_path is not None and Path(token_path).absolute() != (
            capability.path / f".graphify_transaction_token.{receipt['transaction_id']}"):
        raise tx.PendingTransactionError("completed bootstrap token selector changed")
    drainer = tx._read_drainer(capability)
    if drainer is None:
        raise tx.PendingTransactionError("completed bootstrap drainer is missing")
    return {"status": "already_complete", "generation": 1,
            "receipt_sha256": receipt_digest, "preview_sha256": binding.rsplit(":", 1)[1],
            "closure_pending": (tx._read_transaction(capability) is not None
                or tx._load_json(capability, tx.PREPARED_FILE) is not None
                or drainer[1] == "CLOSE_PENDING")}


def _prepare(capability, owner, request, built, legacy, *, failpoint=None):
    """Preserve old bytes outside the empty publication child, then seal once."""
    prepared = tx._pin_prepared_workspace(owner, capability)
    try:
        marker = tx._load_json(capability, tx.PREPARED_FILE)
        if marker is None or "bootstrap_seal" in marker:
            raise tx.PendingTransactionError("bootstrap preparation is already sealed")
        if tuple(sorted(legacy)) != tuple(marker["prior_inventory"]):
            raise tx.PendingTransactionError("bootstrap managed inventory changed")
        for name, payload in legacy.items():
            tx._replace_relative_bytes(prepared.workspace, "legacy/" + name, payload)
        inputs = {"request": request, "evidence": built.evidence}
        tx._replace_bytes(prepared.workspace, INPUTS, _bounded_json(inputs))
        payloads = dict(built.payloads)
        graph = json.loads(payloads["graph.json"])
        graph.setdefault("graph", {})[tx.GRAPH_WATERMARK_KEY] = {
            "schema": 1, "protocol_epoch": 1, "generation": 1, "state": "active",
        }
        payloads["graph.json"] = _json(graph)
        payloads[PROVENANCE] = _provenance(request, built, owner.bootstrap_binding,
                                         capability.identity, owner.legacy_inventory_digest)
        # No ambient or predecessor file can enter this complete publication.
        existing = tx.publication_plan_from_directory(prepared.output.path)
        if set(existing.payloads) - set(payloads):
            raise tx.PendingTransactionError("unexpected bootstrap prepared artifacts")
        for name, payload in payloads.items():
            tx._replace_relative_bytes(prepared.output, name, payload)
        source.reobserve(request, built.evidence, operational_dirs=_operational(prepared))
        tx._validate_legacy_update_inventory_locked(capability, owner.legacy_inventory_digest)
        plan = tx.publication_plan_from_directory(prepared.output.path,
                                                 prior_inventory=tuple(sorted(legacy)))
        inventory = tx._publication_inventory(plan)
        seal = {
            "schema": 1, "bootstrap_binding": owner.bootstrap_binding,
            "inputs_sha256": hashlib.sha256(_bounded_json(inputs)).hexdigest(),
            "legacy_inventory_sha256": owner.legacy_inventory_digest,
            "inventory": inventory, "deletions": list(plan.deletions),
            "plan_sha256": tx._publication_plan_digest(generation=1, graph_name="graph.json",
                                inventory=inventory, deletions=plan.deletions),
        }
        tx._replace_bytes(capability, tx.PREPARED_FILE,
                          _bounded_json({**marker, "bootstrap_seal": seal}))
        prepared.validate()
    finally:
        prepared.close()
    if failpoint:
        failpoint("after_bootstrap_seal")


def _sealed_workspace_path(capability, marker, *, allow_retired):
    expected = tx._identity_from_json(marker["identity"])
    with tx.pin_output(capability.path.parent, mutation=False) as parent:
        current = tx._entry_nofollow_stat(parent, marker["workspace_name"])
        if current is not None or not allow_retired:
            return parent.path / marker["workspace_name"]
        prefix = f".graphify-retired-{marker['transaction_id']}-"
        found = None
        # Retirement writes its location before rename. Locate only the original
        # directory identity, then validate that durable record; absence is not proof.
        with os.scandir(parent.fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > tx._MAX_RECEIPT_ARTIFACTS:
                    raise tx.PendingTransactionError("bootstrap retirement scan exceeds bound")
                if not entry.name.startswith(prefix):
                    continue
                info = entry.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (expected.device, expected.inode):
                    continue
                if not stat.S_ISDIR(info.st_mode) or found is not None:
                    raise tx.PendingTransactionError("bootstrap retired identity changed")
                with tx.pin_output(parent.path / entry.name, mutation=False) as retired:
                    record = tx._validated_retired_marker(
                        tx._load_json(retired, ".graphify_retired.json"),
                        candidate_name=entry.name, candidate_identity=retired.identity,
                        managed_output_identity=capability.identity)
                    if (record is None or record["state"] != "retired"
                            or record["transaction_id"] != marker["transaction_id"]
                            or record["output_identity"] != marker["output_identity"]):
                        raise tx.PendingTransactionError("bootstrap retirement evidence changed")
                found = parent.path / entry.name
        if found is None:
            raise tx.PendingTransactionError("bootstrap retained legacy workspace is missing")
        return found


def _sealed_plan(capability, request, *, binding, legacy_digest, owner_id, token_digest,
                 allow_retired=False):
    marker = tx._load_json(capability, tx.PREPARED_FILE)
    if marker is None or set(marker) != tx._PREPARED_BOOTSTRAP_FIELDS:
        raise tx.PendingTransactionError("bootstrap seal is missing")
    seal = marker["bootstrap_seal"]
    if (not isinstance(seal, dict) or set(seal) != {
            "schema", "bootstrap_binding", "inputs_sha256", "legacy_inventory_sha256",
            "inventory", "deletions", "plan_sha256"}
            or type(seal["schema"]) is not int or seal["schema"] != 1
            or seal["bootstrap_binding"] != binding
            or seal["legacy_inventory_sha256"] != legacy_digest):
        raise tx.PendingTransactionError("bootstrap seal binding changed")
    if (marker["schema"] != 1 or marker["protocol_epoch"] != 1
            or marker["state"] != "ready" or marker["generation"] != 1
            or marker["transaction_id"] != owner_id or marker["token_digest"] != token_digest
            or marker["workspace_name"] != f".graphify-prepare-{owner_id}"):
        raise tx.PendingTransactionError("bootstrap prepared owner changed")
    workspace = tx.pin_output(_sealed_workspace_path(capability, marker,
                              allow_retired=allow_retired), mutation=False)
    try:
        if workspace.identity != tx._identity_from_json(marker["identity"]):
            raise tx.PendingTransactionError("bootstrap prepared workspace identity changed")
        state, child_identity = tx._validate_prepared_owner_binding(workspace, marker)
        output = tx.pin_output(workspace.path / "graphify-out", mutation=False)
        if (state != "child-created" or output.identity != child_identity
                or output.identity != tx._identity_from_json(marker["output_identity"])):
            output.close()
            raise tx.PendingTransactionError("bootstrap prepared child identity changed")
    except Exception:
        workspace.close()
        raise
    prepared = tx.PreparedWorkspaceCapability(workspace, output)
    try:
        inputs_payload = tx._read_bytes(prepared.workspace, INPUTS)
        if hashlib.sha256(inputs_payload).hexdigest() != seal["inputs_sha256"]:
            raise tx.PendingTransactionError("bootstrap source evidence changed")
        inputs = json.loads(inputs_payload)
        if inputs["request"] != request:
            raise tx.PendingTransactionError("bootstrap original request changed")
        evidence_digest = source.InputManifest.from_mapping(inputs["evidence"]).sha256
        expected = _binding(_fingerprint(request, evidence_digest,
                            legacy_digest, capability.identity))
        if expected != binding:
            raise tx.PendingTransactionError("bootstrap consumed evidence binding changed")
        with tx.pin_output(prepared.workspace.path / "legacy", mutation=False) as preserved:
            legacy = tx._legacy_update_inventory_locked(preserved)
        if (sorted(legacy) != marker["prior_inventory"]
                or tx._legacy_update_inventory_digest(legacy) != legacy_digest):
            raise tx.PendingTransactionError("preserved legacy evidence changed")
        plan = tx.publication_plan_from_directory(prepared.output.path,
                                    prior_inventory=tuple(marker["prior_inventory"]))
        inventory = tx._publication_inventory(plan)
        if (inventory != seal["inventory"] or list(plan.deletions) != seal["deletions"]
                or tx._publication_plan_digest(generation=1, graph_name="graph.json",
                    inventory=inventory, deletions=plan.deletions) != seal["plan_sha256"]):
            raise tx.PendingTransactionError("sealed bootstrap publication changed")
        prepared.validate()
        return plan
    finally:
        prepared.close()


def _publish(capability, owner, request, *, failpoint=None):
    plan = _sealed_plan(capability, request, binding=owner.bootstrap_binding,
                        legacy_digest=owner.legacy_inventory_digest,
                        owner_id=owner.id, token_digest=owner.token_digest)
    publication_context = tx._BOOTSTRAP_AUTHORITY.set(owner.bootstrap_binding)
    try:
        receipt = tx._commit_publication_plan(owner, plan, failpoint=failpoint)
    finally:
        tx._BOOTSTRAP_AUTHORITY.reset(publication_context)
    tx._finish_transaction(owner, failpoint=failpoint)
    preserved = tx._retire_prepared_locked(capability)
    return {"status": "complete", "generation": 1, "receipt_sha256": receipt.digest,
            "preserved_workspace": str(preserved),
            "preview_sha256": owner.bootstrap_binding.rsplit(":", 1)[1]}


def _close_completed(capability, request, binding, token_path, *, failpoint=None):
    """Close only a receipt-bound original owner or its durable retirement journal."""
    protocol = tx._read_protocol(capability)
    if protocol is None or protocol["state"] != "COMPLETE":
        raise tx.PendingTransactionError("completed bootstrap protocol changed")
    receipt, receipt_digest, retained = tx._validate_receipt_locked(
        capability, retain_artifacts=(PROVENANCE,))
    proof = json.loads(retained[PROVENANCE])
    drainer = tx._read_drainer(capability)
    if drainer is None:
        raise tx.PendingTransactionError("completed bootstrap drainer is missing")
    plan = _sealed_plan(capability, request, binding=binding,
        legacy_digest=proof["legacy_inventory_sha256"],
        owner_id=receipt["transaction_id"], token_digest=receipt["token_digest"],
        allow_retired=(drainer[1] in {"complete", "reserved"}
                       and tx._read_transaction(capability) is None))
    if {name: hashlib.sha256(payload).hexdigest() for name, payload in plan.payloads.items()} != receipt["artifact_digests"]:
        raise tx.PendingTransactionError("completed bootstrap seal differs from receipt")
    if drainer[1] == "claimed":
        owner = _open_owner(capability, request, binding, token_path)
        with _owner_context(owner):
            tx._finish_transaction(owner, failpoint=failpoint)
    elif drainer[1] == "CLOSE_PENDING":
        tx._validate_close_pending_protocol_locked(capability, protocol, drainer)
        tx._validate_close_pending_locked(capability, drainer[2])
        # Token absence is accepted only by the original durable retirement journal.
        tx._finish_close_locked(capability, drainer[2], failpoint=failpoint)
    elif drainer[1] == "complete":
        if tx._read_transaction(capability) is not None:
            raise tx.PendingTransactionError("completed bootstrap owner unexpectedly remains")
        tx._validate_receipt_locked(capability, require_closed=True)
    elif drainer[1] == "reserved":
        preserved = tx._read_predecessor_authority(capability)
        if (tx._read_transaction(capability) is not None
                or drainer[0].generation != int(receipt["generation"]) + 1
                or drainer[2].get("predecessor_receipt") != receipt_digest
                or preserved is None or preserved[3]["state"] != "preserved-complete"
                or preserved[0] != protocol or preserved[2] != receipt_digest):
            raise tx.PendingTransactionError("bootstrap retirement successor binding changed")
        tx._validate_receipt_locked(capability, require_closed=True,
                                   drainer_override=preserved[1])
    else:
        raise tx.PendingTransactionError("completed bootstrap closure selector changed")
    tx._retire_prepared_locked(capability)


def execute(request, preview_sha256=None, *, token_path=None, now=None, failpoint=None):
    """Keep command ownership inside this call, including interrupted claims."""
    context = tx._AUTHORITY.set(tx._AUTHORITY.get())
    try:
        return _execute(request, preview_sha256, token_path=token_path, now=now,
                        failpoint=failpoint)
    finally:
        tx._AUTHORITY.reset(context)


def _execute(request, preview_sha256=None, *, token_path=None, now=None, failpoint=None):
    """Execute a preview or resume the exact token; never select a replacement corpus."""
    request = source.validate_request(request, require_sources=False)
    if preview_sha256 is None and token_path is None:
        raise ValueError("execute requires a preview digest or an exact resume token")
    with tx.pin_output(request["output"]) as cap, tx._locked(cap):
        protocol = tx._read_protocol(cap)
        binding = (_binding(preview_sha256) if preview_sha256 is not None else
                   None if protocol is None else protocol.get("bootstrap_binding"))
        if binding is None:
            raise tx.PendingTransactionError("no trusted bootstrap token owner")
        completed = _completed(cap, request, binding, token_path=token_path)
        if completed is not None:
            # Repeated execute is deliberately read-only, including after token retirement.
            if token_path is not None and completed["closure_pending"]:
                _close_completed(cap, request, binding, token_path, failpoint=failpoint)
                completed["closure_pending"] = False
            return completed
        if protocol is not None and protocol.get("bootstrap_binding") != binding:
            raise tx.PendingTransactionError("bootstrap original preview binding changed")
        if token_path is not None:
            # Resume cannot spend tokenless recovery attempts before authenticating.
            # Only repeat execute with the original digest may reconstruct that claim.
            _open_owner(cap, request, binding, token_path)
        marker = tx._load_json(cap, tx.PREPARED_FILE)
        if marker is not None and "bootstrap_seal" in marker:
            owner = _open_owner(cap, request, binding, token_path)
            with _owner_context(owner):
                return _publish(cap, owner, request, failpoint=failpoint)
        # Before the seal, repeat the original source request and original legacy guard.
        prepared = None
        if protocol is None:
            legacy = tx.legacy_bootstrap_inventory(cap)
        else:
            pending = tx._read_pending_transition(cap)
            live = pending[3] if pending is not None else tx._read_transaction(cap)
            legacy_digest = (protocol.get("legacy_inventory_digest") if live is None
                             else live.legacy_inventory_digest)
            legacy = tx._validate_legacy_update_inventory_locked(cap, legacy_digest)
            if marker is not None:
                owner = _open_owner(cap, request, binding, token_path)
                with _owner_context(owner):
                    prepared = tx._pin_prepared_workspace(owner, cap)
        try:
            operational = None if prepared is None else _operational(prepared)
            built = source.build(request, operational_dirs=operational)
            source.reobserve(request, built.evidence, operational_dirs=operational)
        finally:
            if prepared is not None:
                prepared.close()
        legacy_digest = tx._legacy_update_inventory_digest(legacy)
        if _binding(_fingerprint(request, built.evidence_digest, legacy_digest, cap.identity)) != binding:
            raise tx.PendingTransactionError("bootstrap preview is stale; source or legacy inventory changed")
        _preflight_state(cap, request, built, legacy, binding, legacy_digest)
        _collision_check(cap, {**built.payloads, PROVENANCE: b""}, legacy)
        tx._validate_legacy_update_inventory_locked(cap, legacy_digest)
        if protocol is None:
            def claim_cut(_cap, _protocol):
                if failpoint:
                    failpoint("after_bootstrap_claim")
            owner = tx.begin_transaction("update", request["root"], output=cap.path,
                       bootstrap_binding=binding, now=now, failpoint=claim_cut,
                       transition_failpoint=failpoint)
        else:
            live = tx._read_transaction(cap)
            if (live is not None and live.token_identity is not None
                    and tx._read_token_transition(cap) is None
                    and tx._read_pending_transition(cap) is None):
                owner = _open_owner(cap, request, binding, token_path)
            else:
                owner = tx.recover_transaction("update", request["root"], output=cap.path,
                         bootstrap_binding=binding, now=now, transition_failpoint=failpoint)
        with _owner_context(owner):
            if owner.token_identity is None:
                if token_path is not None:
                    raise tx.PendingTransactionError("bootstrap original token is missing")
                tx.stage_transaction_handoff(owner, failpoint=failpoint)
                owner = tx._read_transaction(cap)
            with _owner_context(owner):
                _prepare(cap, owner, request, built, legacy, failpoint=failpoint)
                return _publish(cap, owner, request, failpoint=failpoint)


def run_cli(argv=None):
    parser = argparse.ArgumentParser(prog="graphify bootstrap-legacy")
    parser.add_argument("--request", required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preview", action="store_true")
    action.add_argument("--execute", metavar="PREVIEW_SHA256")
    action.add_argument("--resume", type=Path, metavar="TOKEN")
    args = parser.parse_args(argv)
    from graphify.extract import ExtractionIncomplete
    try:
        with args.request.open("rb") as stream:
            payload = stream.read(tx._MAX_STATE_BYTES + 1)
        if len(payload) > tx._MAX_STATE_BYTES:
            raise ValueError("bootstrap request exceeds size bound")
        try:
            request = json.loads(payload)
        except RecursionError as exc:
            raise ValueError("bootstrap request exceeds JSON nesting limit") from exc
        result = preview(request) if args.preview else execute(
            request, args.execute, token_path=args.resume)
        print(_json(result).decode(), end="")
        return 0
    except (OSError, ValueError, tx.PendingTransactionError, SourceError, ExtractionIncomplete) as exc:
        print(_json({"status": "refused", "error": str(exc)}).decode(), end="")
        return 2
