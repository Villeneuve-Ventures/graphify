"""Capacity recovery, projections, and writes bound reads before allocation."""

import os

import pytest

from graphify.workspace.generations import CapacityExceeded
from graphify.workspace.lifecycle_contracts import LIFECYCLE_JSON_MAX_BYTES
from graphify.workspace.persistence import CommitUnknown
from tests.test_workspace_lifecycle_s3 import _runtime, GENERATION_ID
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


@pytest.mark.parametrize("record", ["capacity.json", "capacity.pending.json", "capacity.previous.json"])
@pytest.mark.parametrize("operation", ["recover", "project_target", "project_all"])
def test_oversized_capacity_records_are_not_read(tmp_path, monkeypatch, record, operation):
    harness, generations, _, _ = _runtime(tmp_path)
    target = harness.state_root / record
    target.write_bytes(b"{}")
    target.chmod(0o600)
    reads = _guard_oversized_read(monkeypatch, target)
    before = tree_snapshot(harness.state_root)
    with harness.registry.exclusive_lock(), pytest.raises(CapacityExceeded, match="corrupt"):
        if operation == "recover":
            generations._load_capacity_locked()
        elif operation == "project_target":
            generations._project_capacity_reservation_locked(REPO_UUID, GENERATION_ID)
        else:
            generations._read_capacity_reservations_locked()
    assert tree_snapshot(harness.state_root) == before
    assert reads == []


def _guard_oversized_read(monkeypatch, target):
    identity = (target.stat().st_dev, target.stat().st_ino)
    real_fstat, real_read = os.fstat, os.read
    reads = []

    def is_target(fd):
        info = real_fstat(fd)
        return (info.st_dev, info.st_ino) == identity

    def oversized_fstat(fd):
        info = real_fstat(fd)
        if is_target(fd):
            fields = list(info)
            fields[6] = LIFECYCLE_JSON_MAX_BYTES + 1
            return os.stat_result(fields)
        return info

    def guarded_read(fd, size):
        if is_target(fd):
            reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(os, "fstat", oversized_fstat)
    monkeypatch.setattr(os, "read", guarded_read)
    return reads


def test_capacity_commit_bounds_reread_of_current_record(tmp_path, monkeypatch):
    harness, generations, _, _ = _runtime(tmp_path)
    target = harness.state_root / "capacity.json"
    target.write_bytes(b"{}")
    target.chmod(0o600)
    _guard_oversized_read(monkeypatch, target)
    with harness.registry.exclusive_lock(), pytest.raises(CommitUnknown) as failure:
        generations._commit_capacity_locked((), prior_revision=0)
    assert f"read limit of {LIFECYCLE_JSON_MAX_BYTES}" in str(failure.value.__cause__)
