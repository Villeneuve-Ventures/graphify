"""Malformed fragments and synthetic portability evidence use disposable inputs."""
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import replacement_guard_issuers as issuers
from tests.test_canonical_replacement import case as case
from tests.test_replacement_guard_issuers import protected_policy as protected_policy


@pytest.mark.parametrize("field", ["nodes", "edges", "hyperedges"])
@pytest.mark.parametrize("value", [None, 5, {}, "records"])
def test_fragment_containers_refuse_before_iteration(case, tmp_path, capsys, field, value):
    request, fragments = case
    fragments[0][field] = value
    _assert_cli_refusal(request, fragments, tmp_path, capsys)


@pytest.mark.parametrize("value", [None, [], {}, 3, ""])
def test_node_identity_refuses_before_hashing(case, tmp_path, capsys, value):
    request, fragments = case
    fragments[0]["nodes"][0]["id"] = value
    _assert_cli_refusal(request, fragments, tmp_path, capsys)


@pytest.mark.parametrize("value", [None, [], 3, "node"])
def test_semantic_node_records_must_be_objects(case, tmp_path, capsys, value):
    request, fragments = case
    fragments[0]["nodes"] = [value]
    _assert_cli_refusal(request, fragments, tmp_path, capsys)


@pytest.mark.parametrize("field", ["source_file", "file_type", "edge_source", "edge_target",
                                    "edge_owner", "edge_relation", "group_owner", "group_relation"])
@pytest.mark.parametrize("value", [[], {}])
def test_fragment_identifier_fields_refuse_before_membership(case, tmp_path, capsys, field, value):
    request, fragments = case
    fragment = fragments[0]
    node = fragment["nodes"][0]
    owner = node["source_file"]
    fragment["nodes"].extend({**node, "id": identity} for identity in ("second", "third"))
    edge = {"source": node["id"], "target": "second", "source_file": owner,
            "relation": "references", "confidence": "EXTRACTED", "confidence_score": 1.0}
    group = {"nodes": [node["id"], "second", "third"], "source_file": owner,
             "relation": "participate_in", "confidence": "EXTRACTED", "confidence_score": 1.0}
    fragment["edges"], fragment["hyperedges"] = [edge], [group]
    if field in {"source_file", "file_type"}:
        node[field] = value
    else:
        record = group if field.startswith("group_") else edge
        record[field.split("_", 1)[1].replace("owner", "source_file")] = value
    _assert_cli_refusal(request, fragments, tmp_path, capsys)


def _assert_cli_refusal(request, fragments, tmp_path, capsys):
    before = replacement._tree(request["output"])
    request_path, fragments_path = tmp_path / "request.json", tmp_path / "fragments.json"
    request_path.write_text(json.dumps(request))
    fragments_path.write_text(json.dumps(fragments))
    result = replacement.main(["build-data", "--request", str(request_path),
                               "--fragments", str(fragments_path)])
    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists()
    assert not Path(request["journal"]).exists()


def test_lock_module_skips_if_fcntl_is_unavailable(monkeypatch):
    # Both __import__ and importlib.import_module refuse this sentinel, even if
    # an earlier test loaded fcntl. Monkeypatch restores the prior module entry.
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(pytest.skip.Exception) as error:
        runpy.run_path(str(Path(__file__).with_name("test_replacement_lock.py")))
    assert "fcntl" in str(error.value)


@pytest.fixture
def shared_ancestor_modes(tmp_path, monkeypatch):
    """Model unsafe host ancestor metadata without chmod or another host launch."""
    info = os.stat(tmp_path.parent)
    identity = info.st_dev, info.st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def metadata(info):
        if (info.st_dev, info.st_ino) != identity:
            return info
        return SimpleNamespace(**{name: (info.st_mode | 0o7022 if name == "st_mode" else getattr(info, name))
                                  for name in dir(info) if name.startswith("st_")})

    monkeypatch.setattr(os, "stat", lambda *a, **kw: metadata(real_stat(*a, **kw)))
    monkeypatch.setattr(os, "fstat", lambda fd: metadata(real_fstat(fd)))


@pytest.fixture
def policy_below_shared_ancestor(shared_ancestor_modes, request):
    # Resolve after raw host metadata is intercepted, before the policy model binds it.
    return request.getfixturevalue("protected_policy")


def test_synthetic_policy_admits_safe_modeled_shared_ancestors(policy_below_shared_ancestor, tmp_path):
    parent = tmp_path / "namespace"
    parent.mkdir()
    policy, _, _ = policy_below_shared_ancestor.install(parent)
    info = parent.stat()
    with issuers.issuer_snapshot() as snapshot:
        assert policy.mutator_uid in snapshot.trusted_issuers(parent, (info.st_dev, info.st_ino))


@pytest.mark.parametrize("permissions", [0o777, 0o1777, 0o2755, 0o4755])
def test_modeled_unsafe_policy_directories_still_refuse(protected_policy, permissions):
    protected_policy.mode(protected_policy.directory, permissions)
    with pytest.raises(issuers.IssuerPolicyError, match="unsafe issuer policy"):
        with issuers.issuer_snapshot():
            pytest.fail("an explicit unsafe directory model must not be normalized away")
