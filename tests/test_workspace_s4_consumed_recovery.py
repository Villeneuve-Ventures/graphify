"""Negative and durable evidence for certified consumed-input recovery."""

import copy
import hashlib
import os

import pytest

from graphify.source_io import SourceIO, SourceUnavailable, SourceUnsupported
from graphify.workspace.adapters.v8 import _consumed_evidence
from graphify.workspace.composition import compose_workspace_runtime
from graphify.workspace.contracts import InputManifest, canonical_json_bytes
from graphify.workspace.generations import GenerationConflict
from graphify.workspace.lifecycle_contracts import ContractError, StagedBuildState
from graphify.workspace.persistence import InjectedFault
from graphify.workspace.sync import synchronize_structural
from tests.test_workspace_s4_certified_drift import certified
from tests.workspace_s3_helpers import REPO_UUID


def _staged(runtime):
    return runtime.stores.generations.read_only_staged_build_locked(REPO_UUID, deadline_ns=None)


def _no_abandonment(runtime):
    stage = _staged(runtime)
    assert stage.lifecycle_state == "CERTIFIED"
    assert stage.abandonment_intent is None
    assert stage.abandon_evidence is None
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
    assert not runtime.stores.leases.inspect(REPO_UUID).leases


def test_unchanged_certified_replay_promotes_once(tmp_path, monkeypatch):
    runtime, _repo, _support, request, _manifest = certified(tmp_path, monkeypatch)
    result = synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    assert result.pointer_revision == 1
    assert _staged(runtime).lifecycle_state == "PROMOTED"
    assert synchronize_structural(runtime, request, attempt_sha256="c" * 64) == result


def test_sealed_directory_membership_changes_and_budget_refuses(tmp_path):
    root = tmp_path.resolve()
    (root / "first.txt").write_text("first")
    with SourceIO(root) as inputs:
        inputs.listdir(root)
        manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=())
    with SourceIO(root) as inputs:
        previous = _consumed_evidence(inputs, manifest)
    (root / "second.txt").write_text("second")
    with SourceIO(root) as inputs:
        changed = _consumed_evidence(inputs, manifest)
    assert changed != previous
    with SourceIO(root, max_entries=2) as inputs:
        with pytest.raises(SourceUnsupported, match="limit exceeded"):
            _consumed_evidence(inputs, manifest)
    with SourceIO(root) as inputs:
        inputs.read_bytes(root / "first.txt")
        read_manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=())
    with SourceIO(root, max_file_bytes=1) as inputs:
        with pytest.raises(SourceUnsupported, match="byte limit exceeded"):
            _consumed_evidence(inputs, read_manifest)


def test_sealed_list_replaced_by_regular_file_has_changed_evidence(tmp_path):
    root = tmp_path.resolve()
    listed = root / "listed"
    listed.mkdir()
    with SourceIO(root) as inputs:
        inputs.listdir(listed)
        manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=())
    with SourceIO(root) as inputs:
        previous = _consumed_evidence(inputs, manifest)
    listed.rmdir()
    listed.write_text("replacement")
    with SourceIO(root) as inputs:
        changed = _consumed_evidence(inputs, manifest)
    assert changed != previous


def test_sealed_directory_ancestor_replaced_by_file_has_changed_evidence(tmp_path):
    root = tmp_path.resolve()
    parent = root / "parent"
    parent.mkdir()
    (parent / "child.txt").write_text("child")
    with SourceIO(root) as inputs:
        inputs.read_bytes(parent / "child.txt")
        manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=())
    with SourceIO(root) as inputs:
        previous = _consumed_evidence(inputs, manifest)
    (parent / "child.txt").unlink()
    parent.rmdir()
    parent.write_text("replacement")
    with SourceIO(root) as inputs:
        changed = _consumed_evidence(inputs, manifest)
    assert changed != previous


@pytest.mark.parametrize("failure", ["read_eio", "permission", "symlink_ancestor", "symlink_leaf", "fifo_leaf", "pass_disagreement"])
def test_unproven_consumed_change_keeps_certified_state(tmp_path, monkeypatch, failure):
    runtime, repo, support, request, _manifest = certified(tmp_path, monkeypatch)
    if failure == "read_eio":
        original = SourceIO.read_bytes

        def fail_support_read(self, path, **kwargs):
            if os.fspath(path) == os.fspath(support):
                self.refuse("injected EIO at sealed support read", SourceUnavailable)
            return original(self, path, **kwargs)

        monkeypatch.setattr(SourceIO, "read_bytes", fail_support_read)
    elif failure == "permission":
        original = os.open

        def deny_support_open(path, flags, *args, **kwargs):
            if os.fspath(path) == "base.json":
                raise PermissionError("injected permission denial at sealed support open")
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", deny_support_open)
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {deny_support_open})
    elif failure == "symlink_ancestor":
        support.unlink()
        (repo / ".config").rmdir()
        (repo / ".config").symlink_to(repo, target_is_directory=True)
    elif failure == "symlink_leaf":
        support.unlink()
        support.symlink_to(repo / "lib.ts")
    elif failure == "fifo_leaf":
        support.unlink()
        os.mkfifo(support)
    else:
        observer = runtime.stores.generations.consumed_observer
        counter = iter(("a" * 64, "b" * 64))
        runtime.stores.generations.consumed_observer = lambda *_args, **_kwargs: next(counter)

    if failure in {"read_eio", "permission", "symlink_leaf", "fifo_leaf", "pass_disagreement"}:
        assert runtime.adapter.observe(repo).initial_detection.sha256 == request.build.observation_manifest_sha256

    try:
        if failure == "pass_disagreement":
            with pytest.raises(GenerationConflict, match="not stable"):
                synchronize_structural(runtime, request, attempt_sha256="b" * 64)
        else:
            with pytest.raises((SourceUnavailable, SourceUnsupported)):
                synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    finally:
        if failure == "pass_disagreement":
            runtime.stores.generations.consumed_observer = observer
    _no_abandonment(runtime)


def test_consumed_change_intent_restarts_with_bound_proof(tmp_path, monkeypatch):
    runtime, _repo, support, request, manifest = certified(tmp_path, monkeypatch)
    support.write_text('{"compilerOptions":{}}')

    def fault(label):
        if label.endswith(":abandon_intent_durable"):
            raise InjectedFault(label)

    runtime.stores.generations.fault_hook = fault
    with pytest.raises(InjectedFault):
        synchronize_structural(runtime, request, attempt_sha256="b" * 64)
    pending = _staged(runtime)
    assert pending.lifecycle_state == "CERTIFIED"
    assert pending.abandonment_intent is not None
    proof = pending.abandonment_intent.evidence.consumed_input_change
    assert proof is not None
    assert proof[0] == manifest.sha256
    assert proof[1] != proof[2]
    assert StagedBuildState.from_json(pending.canonical) == pending

    raw = pending.to_dict()
    for mutation in ("wrong_seal", "equal_evidence", "missing_binding", "malformed_proof"):
        invalid = copy.deepcopy(raw)
        if mutation == "missing_binding":
            invalid["completion_binding"] = None
        else:
            change = invalid["abandonment_intent"]["evidence"]["consumed_input_change"]
            if mutation == "wrong_seal":
                change["sealed_manifest_sha256"] = "0" * 64
            elif mutation == "equal_evidence":
                change["observed_evidence_sha256"] = change["previous_evidence_sha256"]
            else:
                change["observed_evidence_sha256"] = "bad"
            invalid["abandonment_intent"]["evidence_sha256"] = hashlib.sha256(
                canonical_json_bytes(invalid["abandonment_intent"]["evidence"])
            ).hexdigest()
        with pytest.raises(ContractError):
            StagedBuildState.from_mapping(invalid)

    runtime = compose_workspace_runtime(runtime.inputs).require_runtime()
    with pytest.raises(GenerationConflict, match="abandoned"):
        synchronize_structural(runtime, request, attempt_sha256="c" * 64)
    final = _staged(runtime)
    assert final.lifecycle_state == "ABANDONED"
    assert final.abandon_reason == "SOURCE_CHANGED"
    assert final.abandon_evidence.consumed_input_change == proof
    assert StagedBuildState.from_json(final.canonical) == final
    assert runtime.stores.pointers.load(REPO_UUID, allow_missing=True) is None
    assert not runtime.stores.leases.inspect(REPO_UUID).leases
