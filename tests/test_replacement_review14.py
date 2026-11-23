"""Profile cardinality and guard modes use disposable, synthetic custody fixtures."""
import base64
import json
import os

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case
from tests.test_replacement_guard_issuers import guarded_parent as guarded_parent
from tests.test_replacement_guard_issuers import protected_policy as protected_policy


def _group(fragment, members):
    node = fragment["nodes"][0]
    fragment["nodes"] = [{**node, "id": identity, "label": identity}
                         for identity in ("guide_one", "guide_two", "guide_three")]
    fragment["hyperedges"] = [{"id": "guide_flow", "nodes": members,
        "source_file": node["source_file"], "relation": "participate_in",
        "confidence": "EXTRACTED", "confidence_score": 1.0}]


def test_profile_keeps_three_distinct_group_members(case):
    request, fragments = case
    _group(fragments[0], ["guide_one", "guide_two", "guide_three"])
    seal = replacement.build_data(request, fragments)
    graph = json.loads(base64.b64decode(seal["payloads"]["graph.json"]))
    assert graph["hyperedges"][0]["nodes"] == ["guide_one", "guide_two", "guide_three"]


@pytest.mark.parametrize("mode", [0o620, 0o602])
@pytest.mark.parametrize("owner", ["caller", "legacy"])
@pytest.mark.parametrize("operation", ["read", "mutation", "registration"])
@pytest.mark.skipif(os.name != "posix", reason="POSIX issuer and marker-mode admission")
def test_trusted_writable_marker_refuses_before_payload_admission(
    protected_policy, guarded_parent, monkeypatch, mode, owner, operation,
):
    parent, marker, journal = guarded_parent
    if owner == "legacy":
        protected_policy.install(parent, legacy=(987653,))
        protected_policy.owner(marker, 987653)
    else:
        protected_policy.owner(marker, os.getuid())
    marker.chmod(mode)
    before = marker.read_bytes()
    real_read, real_load = replacement._read, replacement._load

    def no_payload_read(path, *args, **kwargs):
        if path == replacement.GUARD:
            pytest.fail("unsafe trusted marker payload must not be read")
        return real_read(path, *args, **kwargs)

    def no_payload_load(path, *args, **kwargs):
        if path == replacement.GUARD:
            pytest.fail("unsafe trusted marker payload must not be loaded")
        return real_load(path, *args, **kwargs)

    monkeypatch.setattr(replacement, "_read", no_payload_read)
    monkeypatch.setattr(replacement, "_load", no_payload_load)
    with pytest.raises(tx.PendingTransactionError, match="writable replacement guard"):
        if operation == "registration":
            replacement._register(parent / "next", journal, "exact", "archive")
        else:
            replacement.guard_output(parent / "ordinary", mutation=operation == "mutation")
    assert marker.read_bytes() == before


@pytest.mark.parametrize("mode", [0o600, 0o644])
@pytest.mark.skipif(os.name != "posix", reason="POSIX marker registration")
def test_trusted_safe_mode_marker_retains_archive_fence(protected_policy, guarded_parent, mode):
    parent, marker, journal = guarded_parent
    marker.chmod(mode)
    replacement.guard_output(parent / "archive", mutation=False)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement.guard_output(parent / "archive")
    replacement._register(parent / "next", journal, "exact", "archive")
    assert "next" in json.loads(marker.read_bytes())["entries"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX foreign-issuer admission")
def test_foreign_writable_marker_is_ignored_without_payload_read(
    protected_policy, guarded_parent, monkeypatch,
):
    parent, marker, _ = guarded_parent
    protected_policy.owner(marker, 987654)
    marker.chmod(0o666)
    marker.write_bytes(b"not-json")
    real_load = replacement._load

    def no_foreign_load(path, *args, **kwargs):
        if path == replacement.GUARD:
            pytest.fail("foreign marker payload must not be loaded")
        return real_load(path, *args, **kwargs)

    monkeypatch.setattr(replacement, "_load", no_foreign_load)
    replacement.guard_output(parent / "ordinary")
