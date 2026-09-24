"""Inventory rejects unsealed shape and bounds work by the sealed receipt."""

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest

from graphify.workspace.generations import GenerationError
from tests.test_workspace_generation_capacity_review import _certification
from tests.test_workspace_generation_io_limits import _inode
from tests.test_workspace_lifecycle_s3 import (
    GENERATION_ID, REPO_UUID, START, _complete, _prepare, _runtime,
)


def _certify(generations, attempt, completion, observations):
    request = _certification(generations, attempt, completion, observations)
    return generations.certify(
        attempt.grant, completion.allocation, request,
        source_observations=observations, declared_entries=completion.entries,
        staged_completion=completion, occurred_at=START, monotonic_ns=10_006,
    )


@pytest.mark.parametrize("at_root", [False, True])
def test_inventory_bounds_directory_enumeration(tmp_path, monkeypatch, at_root):
    harness, generations, _, observations = _runtime(tmp_path)
    _, _, preparation = _prepare(harness, generations, observations)
    target = preparation.staging_path
    if not at_root:
        target /= "graphify-out"
    target_inode = _inode(target.stat())
    real_scandir = os.scandir
    visited = []

    def exploding_scandir(descriptor):
        if not isinstance(descriptor, int) or _inode(os.fstat(descriptor)) != target_inode:
            return real_scandir(descriptor)

        def entries():
            for index in range(100):
                visited.append(index)
                yield SimpleNamespace(name=f"unexpected-{index}")

        @contextmanager
        def scan():
            yield entries()

        return scan()

    monkeypatch.setattr(os, "scandir", exploding_scandir)
    with pytest.raises(GenerationError):
        generations.complete_staged_build(
            preparation, source_observations=observations, monotonic_ns=10_003,
        )
    assert 1 <= len(visited) <= 3


@pytest.mark.parametrize("stage", ["publishing", "complete", "certified"])
@pytest.mark.parametrize("depth", [1, 3])
def test_empty_directories_cannot_enter_sealed_payload(tmp_path, stage, depth):
    harness, generations, _, observations = _runtime(tmp_path)
    if stage == "publishing":
        _, _, preparation = _prepare(harness, generations, observations)
        container = preparation.staging_path
    else:
        _, attempt, completion = _complete(harness, generations, observations)
        container = completion.allocation.staging_path
        if stage == "certified":
            _certify(generations, attempt, completion, observations)
            container = generations.state.path(generations._generation(REPO_UUID, GENERATION_ID))
    unexpected = container / "graphify-out"
    for index in range(depth):
        unexpected /= f"empty-{index}"
        unexpected.mkdir()

    with pytest.raises(GenerationError):
        if stage == "publishing":
            generations.complete_staged_build(
                preparation, source_observations=observations, monotonic_ns=10_003,
            )
        elif stage == "complete":
            _certify(generations, attempt, completion, observations)
        else:
            generations.verify_generation(REPO_UUID, GENERATION_ID)


def test_inventory_never_descends_into_directory_named_as_structural_file(tmp_path, monkeypatch):
    harness, generations, _, observations = _runtime(tmp_path)
    _, _, preparation = _prepare(harness, generations, observations)
    graph = preparation.staging_path / "graphify-out" / "graph.json"
    graph.unlink()
    graph.mkdir()
    nested = graph
    for index in range(4):
        nested /= f"nested-{index}"
        nested.mkdir()
    target_inode = _inode(graph.stat())
    real_scandir = os.scandir
    descended = []

    def tracked_scandir(descriptor):
        if isinstance(descriptor, int) and _inode(os.fstat(descriptor)) == target_inode:
            descended.append(descriptor)
        return real_scandir(descriptor)

    monkeypatch.setattr(os, "scandir", tracked_scandir)
    with pytest.raises(GenerationError):
        generations.complete_staged_build(
            preparation, source_observations=observations, monotonic_ns=10_003,
        )
    assert descended == []


def test_verification_rejects_payload_larger_than_receipt_before_reading(tmp_path, monkeypatch):
    harness, generations, _, observations = _runtime(tmp_path)
    _, attempt, completion = _complete(harness, generations, observations)
    _certify(generations, attempt, completion, observations)
    generation = generations.state.path(generations._generation(REPO_UUID, GENERATION_ID))
    graph = generation / "graphify-out" / "graph.json"
    graph.write_bytes(b"x" * (sum(entry["size"] for entry in completion.entries) + 1))
    target_inode = _inode(graph.stat())
    real_read = os.read
    target_reads = []

    def tracked_read(descriptor, size):
        if _inode(os.fstat(descriptor)) == target_inode:
            target_reads.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", tracked_read)
    with pytest.raises(GenerationError):
        generations.verify_generation(REPO_UUID, GENERATION_ID)
    assert target_reads == []


def test_verification_rechecks_receipt_after_payload_inventory(tmp_path, monkeypatch):
    harness, generations, _, observations = _runtime(tmp_path)
    _, attempt, completion = _complete(harness, generations, observations)
    _certify(generations, attempt, completion, observations)
    generation = generations.state.path(generations._generation(REPO_UUID, GENERATION_ID))
    receipt = generation / "receipt.json"
    real_inventory = generations._inventory

    def inventory_then_change_receipt(*args, **kwargs):
        inventory = real_inventory(*args, **kwargs)
        # Still-valid JSON makes failure depend on byte stability, not parsing.
        with receipt.open("ab") as stream:
            stream.write(b" ")
        return inventory

    monkeypatch.setattr(generations, "_inventory", inventory_then_change_receipt)
    with pytest.raises(GenerationError):
        generations.verify_generation(REPO_UUID, GENERATION_ID)
