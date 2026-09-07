"""Tests for graphify query CLI context filtering."""
from __future__ import annotations

import json
import os
import shutil

import networkx as nx
import pytest
from networkx.readwrite import json_graph

import graphify.__main__ as mainmod


def _write_graph(tmp_path):
    G = nx.Graph()
    G.add_node("n1", label="extract", source_file="extract.py", source_location="L10", community=0)
    G.add_node("n2", label="cluster", source_file="cluster.py", source_location="L5", community=0)
    G.add_node("n3", label="build", source_file="build.py", source_location="L1", community=1)
    G.add_edge("n1", "n2", relation="calls", confidence="EXTRACTED", context="call")
    G.add_edge("n2", "n3", relation="imports", confidence="EXTRACTED", context="import")
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")))
    return graph_path


def test_query_cli_explicit_context_filter(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "extract", "--context", "call", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "Context: call (explicit)" in out
    assert "cluster" in out
    assert "build" not in out


def test_query_cli_heuristic_context_filter(monkeypatch, tmp_path, capsys):
    graph_path = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "who calls extract", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "Context: call (heuristic)" in out
    assert "cluster" in out
    assert "build" not in out


def test_query_cli_rejects_oversized_graph(monkeypatch, tmp_path, capsys):
    """#F4: query CLI must refuse to parse a graph.json that exceeds the cap."""
    graph_path = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr("graphify.security._MAX_GRAPH_FILE_BYTES", 16)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "query", "extract", "--graph", str(graph_path)],
    )
    with pytest.raises(SystemExit):
        mainmod.main()
    err = capsys.readouterr().err
    assert "exceeds" in err
    assert "byte cap" in err


@pytest.fixture
def native_query_copy(tmp_path, monkeypatch):
    from graphify import transaction as tx

    if tx._PLATFORM == "windows":
        pytest.skip("native Windows publication remains unsupported")
    root = tmp_path / "corpus"
    root.mkdir()
    output = root / "graphify-out"
    saved_authority = tx._AUTHORITY.set(None)
    try:
        owner = tx.begin_transaction("full", root, output=output)
        tx.stage_transaction_handoff(owner)
        owner = tx.resume_transaction(owner.id, root, output=output)
        legacy_path = _write_graph(tmp_path)
        data = json.loads(legacy_path.read_text())
        data["graph"][tx.GRAPH_WATERMARK_KEY] = {
            "schema": 1, "protocol_epoch": 1,
            "generation": owner.generation, "state": "active",
        }
        payload = json.dumps(data).encode()
        tx.commit_publication_plan(owner, tx.PublicationPlan({
            "graph.json": payload, "manifest.json": b"{}",
        }))
        tx.finish_transaction(owner)
        detached = tmp_path / "detached"
        detached.mkdir()
        graph = detached / "graph.json"
        graph.write_bytes(payload)
        monkeypatch.chdir(root)
        monkeypatch.setenv("GRAPHIFY_OUT", str(output))
        monkeypatch.setenv("GRAPHIFY_QUERY_LOG_DISABLE", "1")
        monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
        monkeypatch.setattr(mainmod.sys, "argv", [
            "graphify", "query", "extract", "--graph", str(graph),
        ])
        yield output, graph
    finally:
        tx._AUTHORITY.reset(saved_authority)


def test_query_cli_admits_exact_detached_native_copy(native_query_copy, capsys):
    output, graph = native_query_copy
    before = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
    mainmod.main()
    assert "extract" in capsys.readouterr().out
    assert graph.read_bytes() == before["graph.json"]
    assert {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()} == before


def test_query_cli_detached_native_requires_explicit_output(native_query_copy, monkeypatch, capsys):
    monkeypatch.delenv("GRAPHIFY_OUT")
    with pytest.raises(SystemExit) as stopped:
        mainmod.main()
    assert stopped.value.code == 1
    assert "generation receipt is missing" in capsys.readouterr().err


@pytest.mark.parametrize("unsafe", ["different-bytes", "symlink", "fifo"])
def test_query_cli_detached_input_rejections(native_query_copy, monkeypatch, capsys, unsafe):
    _output, graph = native_query_copy
    if unsafe == "different-bytes":
        graph.write_bytes(graph.read_bytes() + b" ")
    elif unsafe == "symlink":
        target = graph.with_name("original.json")
        graph.rename(target)
        graph.symlink_to(target)
    elif unsafe == "fifo":
        graph.unlink()
        os.mkfifo(graph)
    with pytest.raises(SystemExit) as stopped:
        mainmod.main()
    assert stopped.value.code == 1
    assert capsys.readouterr().err


@pytest.mark.parametrize("invalid", ["missing-receipt", "invalid-receipt", "pending", "local-coordination"])
def test_query_cli_detached_preserves_native_admission(native_query_copy, capsys, invalid):
    from graphify import transaction as tx

    output, graph = native_query_copy
    if invalid == "missing-receipt":
        (output / tx.RECEIPT_FILE).unlink()
    elif invalid == "invalid-receipt":
        (output / tx.RECEIPT_FILE).write_bytes(b"{}")
    elif invalid == "pending":
        tx.begin_transaction("full", output.parent, output=output)
    else:
        shutil.copy2(output / tx.RECEIPT_FILE, graph.parent / tx.RECEIPT_FILE)
    with pytest.raises(SystemExit) as stopped:
        mainmod.main()
    assert stopped.value.code == 1
    assert capsys.readouterr().err


@pytest.mark.parametrize("changed", ["output", "receipt", "detached"])
def test_query_cli_detached_rechecks_binding(native_query_copy, monkeypatch, capsys, changed):
    from graphify import transaction as tx

    output, graph = native_query_copy
    original = tx._validate_receipt_locked
    injected = False
    closed_checks = 0

    def validate(*args, **kwargs):
        nonlocal injected, closed_checks
        result = original(*args, **kwargs)
        if kwargs.get("require_closed"):
            closed_checks += 1
        if closed_checks == 2 and not injected:
            injected = True
            if changed == "output":
                previous = output.with_name("previous-output")
                output.rename(previous)
                shutil.copytree(previous, output)
            elif changed == "receipt":
                replacement = output / "replacement.json"
                replacement.write_bytes((output / tx.RECEIPT_FILE).read_bytes())
                replacement.replace(output / tx.RECEIPT_FILE)
            else:
                graph.write_bytes(graph.read_bytes() + b" ")
        return result

    monkeypatch.setattr(tx, "_validate_receipt_locked", validate)
    with pytest.raises(SystemExit) as stopped:
        mainmod.main()
    assert injected and stopped.value.code == 1
    assert capsys.readouterr().err


def test_query_cli_detached_stat_open_fifo_swap(native_query_copy, monkeypatch, capsys):
    _output, graph = native_query_copy
    original = os.open
    injected = False

    def open_file(name, flags, *args, **kwargs):
        nonlocal injected
        if name == "graph.json" and kwargs.get("dir_fd") is not None and not injected:
            injected = True
            graph.unlink()
            os.mkfifo(graph)
            assert flags & os.O_NONBLOCK
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(SystemExit) as stopped:
        mainmod.main()
    assert injected and stopped.value.code == 1
    assert capsys.readouterr().err


def test_query_cli_preserves_legacy_with_explicit_output(native_query_copy, capsys):
    from graphify import transaction as tx

    _output, graph = native_query_copy
    data = json.loads(graph.read_text())
    del data["graph"][tx.GRAPH_WATERMARK_KEY]
    graph.write_text(json.dumps(data))
    mainmod.main()
    assert "extract" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["legacy", "managed"])
@pytest.mark.parametrize("override", [False, True])
def test_query_cli_preserves_ordinary_graph_symlink(
    native_query_copy, monkeypatch, capsys, kind, override,
):
    output, graph = native_query_copy
    target = _write_graph(graph.parent) if kind == "legacy" else output / "graph.json"
    alias = graph.parent / "alias.json"
    alias.symlink_to(target)
    if not override:
        monkeypatch.delenv("GRAPHIFY_OUT")
    monkeypatch.setattr(mainmod.sys, "argv", [
        "graphify", "query", "extract", "--graph", str(alias),
    ])
    mainmod.main()
    assert "extract" in capsys.readouterr().out


@pytest.mark.parametrize("fits", [False, True])
def test_detached_reader_enforces_own_size_cap(native_query_copy, monkeypatch, fits):
    from graphify import transaction as tx

    output, graph = native_query_copy
    limit = graph.stat().st_size - (not fits)
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", str(limit))
    original = tx._read_open_regular
    calls = []

    def read(fd, *, limit, label):
        calls.append((limit, label))
        return original(fd, limit=limit, label=label)

    monkeypatch.setattr(tx, "_read_open_regular", read)
    if fits:
        assert tx._query_graph_data(graph, managed_output=output)["nodes"]
    else:
        with pytest.raises(tx.PendingTransactionError, match="unsafe legacy artifact: graph.json"):
            tx._query_graph_data(graph, managed_output=output)
    assert (limit, "graph.json") in calls


@pytest.mark.parametrize("escape", [False, True])
def test_query_alias_inside_selected_output(native_query_copy, escape):
    from graphify import transaction as tx

    output, graph = native_query_copy
    target = _write_graph(graph.parent) if escape else output / "graph.json"
    alias = output / "alias.json"
    alias.symlink_to(target)
    if escape:
        with pytest.raises(tx.PendingTransactionError):
            tx._query_graph_data(alias, managed_output=output)
    else:
        assert tx._query_graph_data(alias, managed_output=output)["nodes"]


@pytest.mark.parametrize("invalid", ["alias-replaced", "target-replaced", "local-coordination", "receipt", "pending"])
def test_query_alias_preserves_admission(native_query_copy, monkeypatch, invalid):
    from graphify import transaction as tx

    output, graph = native_query_copy
    target = output / "graph.json"
    alias = graph.parent / "alias.json"
    alias.symlink_to(target)
    original = tx.open_graph_snapshot
    injected = False

    def snapshot(*args, **kwargs):
        nonlocal injected
        result = original(*args, **kwargs)
        injected = True
        if invalid == "alias-replaced":
            alias.unlink()
            alias.symlink_to(_write_graph(graph.parent))
        elif invalid == "target-replaced":
            payload = target.read_bytes()
            target.unlink()
            target.write_bytes(payload)
        elif invalid == "local-coordination":
            shutil.copyfile(output / tx.RECEIPT_FILE, graph.parent / tx.RECEIPT_FILE)
        elif invalid == "receipt":
            (output / tx.RECEIPT_FILE).write_bytes(b"{}")
        else:
            tx.begin_transaction("full", output.parent, output=output)
        return result

    monkeypatch.setattr(tx, "open_graph_snapshot", snapshot)
    with pytest.raises(tx.PendingTransactionError):
        tx._query_graph_data(alias, managed_output=output)
    assert injected
