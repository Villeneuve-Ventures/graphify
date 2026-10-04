"""Observation workers preserve manifest admission and public error contracts."""
import io
import json
import stat
import time
from types import SimpleNamespace

import pytest

from graphify.source_io import SourceError
from graphify.workspace.adapters import v8
from graphify.workspace.adapters.base import SourceObservation
from graphify.workspace.contracts import InputManifest, MAX_DOCUMENT_BYTES
from tests.test_workspace_contracts import manifests


def envelope(initial, consumed):
    return {"source_commit": "a" * 40, "policy_sha256": "b" * 64,
            "initial_detection": initial.to_dict(), "consumed_inputs": consumed.to_dict(),
            "stable_inventory_passes": 2}


@pytest.mark.parametrize("malformation", ["json", "utf8", "shape", "manifest", "passes", "commit", "commit_type"])
def test_malformed_observation_output_has_source_error(tmp_path, monkeypatch, malformation):
    initial, consumed = manifests(tmp_path)
    value = envelope(initial, consumed)
    if malformation == "manifest":
        value["initial_detection"] = {}
    elif malformation == "passes":
        value["stable_inventory_passes"] = 3  # Structural-valid, lifecycle-invalid.
    elif malformation == "commit":
        value["source_commit"] = "bad"
    elif malformation == "commit_type":
        value["source_commit"] = None
    raw = {"json": b"not JSON", "utf8": b"\xff", "shape": b"[]"}.get(malformation, json.dumps(value).encode())
    monkeypatch.setattr(v8, "run_readonly", lambda *args, **kwargs: raw)
    with pytest.raises(SourceError):
        v8.V8Adapter().observe_lifecycle(tmp_path, deadline_ns=time.monotonic_ns() + 10_000_000_000)


def test_near_limit_manifests_fit_observation_transport(tmp_path, monkeypatch):
    count = 60_000
    record_bytes = len(json.dumps({"operation": "probe", "path": "000000", "value": None},
                                 separators=(",", ":"))) + 1
    prefix = "x" * ((MAX_DOCUMENT_BYTES - 4096) // count - record_bytes)
    value = {"contract": "graphify.workspace.source-inputs", "format_version": 2,
             "phase": "detection", "roots": ["source"], "code_inputs": [],
             "outcomes": [], "failure": None,
             "evidence": [{"operation": "directory", "path": ".", "value": [1, 1, stat.S_IFDIR | 0o700]}]
                         + [{"operation": "probe", "path": f"{prefix}{i:06d}", "value": None}
                          for i in range(count)]}
    initial = InputManifest.from_mapping(value)
    consumed = InputManifest.from_mapping(dict(value, phase="consumed"))
    assert MAX_DOCUMENT_BYTES - 100_000 < len(consumed.canonical) <= MAX_DOCUMENT_BYTES
    observed = SourceObservation(initial, consumed, 2)
    source = SimpleNamespace(head_commit="a" * 40, config_sha256="b" * 64)
    legacy = json.dumps(envelope(initial, consumed), ensure_ascii=False).encode()
    assert len(legacy) > 2 * MAX_DOCUMENT_BYTES + 65_536

    def transport(code, raw, **limits):
        assert len(raw) <= limits["max_input_bytes"]
        request = json.loads(raw)
        assert InputManifest.from_mapping(request["input_manifest"]) == consumed
        output = io.BytesIO()
        with monkeypatch.context() as patch:
            patch.setattr(v8, "_child_request", lambda: request)
            patch.setattr(v8.V8Adapter, "_observe", lambda *args, **kwargs: (observed, source))
            patch.setattr(v8.sys, "stdout", SimpleNamespace(buffer=output))
            v8._observation_child()
        encoded = output.getvalue()
        assert len(encoded) <= limits["max_output_bytes"]
        return encoded

    monkeypatch.setattr(v8, "run_readonly", transport)
    actual = v8.V8Adapter().observe_lifecycle(tmp_path, input_manifest=consumed,
                                             deadline_ns=time.monotonic_ns() + 60_000_000_000)
    assert actual.structural == observed
    assert actual.source_commit == source.head_commit
