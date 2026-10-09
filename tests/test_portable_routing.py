"""Portable signals must never become local or unmanaged write authority."""
import json

import pytest

from graphify import transaction as tx


def inventory(root):
    return {
        str(path.relative_to(root)): (path.lstat().st_mode, path.read_bytes())
        for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("marker", ["absent", None])
def test_orphan_envelope_refuses_ordinary_and_external_read(tmp_path, marker):
    output = tmp_path / "graphify-out"
    output.mkdir()
    data = {"nodes": [], "links": [], "graph": {}}
    if marker != "absent":
        data["graph"][tx.GRAPH_WATERMARK_KEY] = marker
    graph = output / "graph.json"
    graph.write_text(json.dumps(data))
    (output / ".graphify_portable.json").write_bytes(b"malformed")
    before = inventory(tmp_path)
    with pytest.raises(tx.PendingTransactionError, match="portable"):
        tx.open_graph_snapshot(graph, purpose="query")
    with pytest.raises(tx.PendingTransactionError, match="portable"):
        tx.open_external_graph_snapshot(graph)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("nested", [False, True])
def test_orphan_envelope_refuses_bootstrap_before_creating_output(tmp_path, nested):
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / ".graphify_portable.json").write_bytes(b"{}")
    target = output / "nested" if nested else output
    before = inventory(tmp_path)
    with pytest.raises(tx.PendingTransactionError, match="portable"):
        tx.begin_transaction("full", tmp_path, output=target)
    assert inventory(tmp_path) == before
    if nested:
        assert not target.exists()


def test_orphan_envelope_refuses_unmanaged_write(tmp_path):
    (tmp_path / ".graphify_portable.json").write_bytes(b"{}")
    before = inventory(tmp_path)
    with pytest.raises(tx.PendingTransactionError):
        tx.commit_unmanaged_bytes(tmp_path / "report.txt", b"no")
    assert inventory(tmp_path) == before


def test_portable_snapshot_cannot_be_a_local_baseline(tmp_path):
    class ReadOnlySnapshot:
        graph_present = False
        output_identity = None

    before = inventory(tmp_path)
    with pytest.raises(TypeError, match="GraphSnapshot"):
        tx.begin_transaction(
            "full", tmp_path, output=tmp_path / "out", expected_snapshot=ReadOnlySnapshot()
        )
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("role", ["ancestor", "current", "other"])
def test_detached_merge_operand_cannot_bypass_orphan_envelope(tmp_path, role):
    output = tmp_path / "portable"
    output.mkdir()
    graph = output / "operand.json"
    graph.write_text(json.dumps({"nodes": [], "links": [], "graph": {}}))
    (output / ".graphify_portable.json").write_bytes(b"malformed")
    before = inventory(tmp_path)
    with pytest.raises(tx.PendingTransactionError, match="portable"):
        tx.load_detached_merge_snapshot(graph, role=role)
    assert inventory(tmp_path) == before
