"""Generation limits must bound I/O before oversized contents are hashed."""

import os

import pytest

from graphify.workspace.generations import CapacityExceeded, GenerationError
from graphify.workspace.lifecycle_contracts import LIFECYCLE_JSON_MAX_BYTES
from tests.test_workspace_lifecycle_s3 import (
    GENERATION_ID, REPO_UUID, _complete, _prepare, _runtime,
)


def _inode(details):
    return details.st_dev, details.st_ino


def test_verification_rejects_oversized_receipt_before_reading(tmp_path, monkeypatch):
    harness, generations, _, observations = _runtime(tmp_path)
    _, _, completion = _complete(harness, generations, observations)
    generation = generations.state.path(generations._generation(REPO_UUID, GENERATION_ID))
    generation.parent.mkdir(mode=0o700, exist_ok=True)
    completion.allocation.staging_path.rename(generation)
    receipt = generation / "receipt.json"
    receipt.write_bytes(b"{}")
    receipt.chmod(0o600)
    receipt_inode = _inode(receipt.stat())
    real_fstat, real_read = os.fstat, os.read
    receipt_reads = []

    def oversized_fstat(descriptor):
        details = real_fstat(descriptor)
        if _inode(details) == receipt_inode:
            fields = list(details)
            fields[6] = LIFECYCLE_JSON_MAX_BYTES + 1
            return os.stat_result(fields)
        return details

    def tracked_read(descriptor, size):
        if _inode(real_fstat(descriptor)) == receipt_inode:
            receipt_reads.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "fstat", oversized_fstat)
    monkeypatch.setattr(os, "read", tracked_read)
    with pytest.raises(GenerationError, match=f"read limit of {LIFECYCLE_JSON_MAX_BYTES} bytes"):
        generations.verify_generation(REPO_UUID, GENERATION_ID)
    assert receipt_reads == []


@pytest.mark.parametrize("cumulative", [False, True])
def test_completion_stops_before_reading_payload_over_reservation(
    tmp_path, monkeypatch, cumulative,
):
    harness, generations, _, observations = _runtime(tmp_path)
    _, _, preparation = _prepare(harness, generations, observations)
    payload = preparation.staging_path / "graphify-out"
    limit = preparation.allocation.expected_payload_bytes
    if cumulative:
        (payload / "graph.json").write_bytes(b"a" * (limit // 2 + 1))
        oversized = payload / "input-manifest.json"
        oversized.write_bytes(b"b" * (limit // 2 + 1))
    else:
        oversized = payload / "graph.json"
        oversized.write_bytes(b"a" * (limit + 1))
    target_inode = _inode(oversized.stat())
    real_read = os.read
    target_reads = []

    def tracked_read(descriptor, size):
        if _inode(os.fstat(descriptor)) == target_inode:
            target_reads.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", tracked_read)
    # Abandonment unlinks this file, then reads newly written state records.
    # Keep its inode alive so Linux cannot recycle it into a tracked record.
    with oversized.open("rb"), pytest.raises(CapacityExceeded):
        generations.complete_staged_build(
            preparation, source_observations=observations, monotonic_ns=10_003,
        )
    assert target_reads == []
    assert generations.recover_staged_build(REPO_UUID).lifecycle_state == "ABANDONED"


def test_completion_bounds_reads_when_payload_grows_after_stat(tmp_path, monkeypatch):
    harness, generations, _, observations = _runtime(tmp_path)
    _, _, preparation = _prepare(harness, generations, observations)
    growing = preparation.staging_path / "graphify-out" / "graph.json"
    growing.write_bytes(b"a")
    target_inode = _inode(growing.stat())
    limit = preparation.allocation.expected_payload_bytes
    real_read = os.read
    bytes_read = 0
    grew = False

    def grow_then_read(descriptor, size):
        nonlocal bytes_read, grew
        if _inode(os.fstat(descriptor)) != target_inode:
            return real_read(descriptor, size)
        if not grew:
            with growing.open("ab") as stream:
                stream.write(b"b" * (limit * 2))
            grew = True
        chunk = real_read(descriptor, size)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", grow_then_read)
    # Pin the tracked inode across abandonment and its subsequent state writes.
    with growing.open("rb"), pytest.raises(CapacityExceeded):
        generations.complete_staged_build(
            preparation, source_observations=observations, monotonic_ns=10_003,
        )
    assert grew
    # One excess byte may be read to prove that the reservation was exceeded.
    assert bytes_read <= limit + 1
    assert generations.recover_staged_build(REPO_UUID).lifecycle_state == "ABANDONED"
