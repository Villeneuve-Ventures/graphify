"""Global storage mutations refuse retained namespaces; all storage is disposable."""
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import networkx as nx
import pytest

from graphify import canonical_replacement as replacement
from graphify import global_graph as global_store
from graphify import transaction as tx


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("global guard tests must not use network or subprocesses")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    target = tmp_path / "global"
    monkeypatch.setattr(global_store, "_GLOBAL_DIR", target)
    monkeypatch.setattr(global_store, "_GLOBAL_GRAPH", target / "global-graph.json")
    monkeypatch.setattr(global_store, "_GLOBAL_MANIFEST", target / "global-manifest.json")
    monkeypatch.setattr(global_store, "datetime", SimpleNamespace(
        now=lambda tz: datetime(2026, 11, 2, 12, 0, 0, tzinfo=tz),
    ))
    return target


@pytest.fixture
def registration(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({
        "request_digest": "prior-operation", "state": "COMMITTED",
    }))

    def register(path):
        replacement._register(path, journal, "prior-operation", "archive")

    return register


def select_storage(monkeypatch, target):
    monkeypatch.setattr(global_store, "_GLOBAL_DIR", target)
    monkeypatch.setattr(global_store, "_GLOBAL_GRAPH", target / "global-graph.json")
    monkeypatch.setattr(global_store, "_GLOBAL_MANIFEST", target / "global-manifest.json")


def save(kind):
    if kind == "graph":
        global_store._save_global_graph(nx.Graph([("one", "two")]))
    else:
        global_store._save_manifest({"version": 1, "repos": {"one": {}}})


@pytest.mark.parametrize("kind", ["graph", "manifest"])
@pytest.mark.parametrize("namespace", ["archive", "quarantine"])
@pytest.mark.parametrize("slot", ["directory", "descendant", "alias", "absent"])
def test_global_save_refuses_retained_directory_before_creation(
    tmp_path, monkeypatch, registration, kind, namespace, slot,
):
    retained = tmp_path / namespace
    if slot != "absent":
        retained.mkdir()
        (retained / "old.txt").write_bytes(b"preserved old graph")
    registration(retained)
    alias = tmp_path / "alias"
    alias.symlink_to(retained, target_is_directory=True)
    target = {"directory": retained, "descendant": retained / "new" / "global",
              "alias": alias, "absent": retained / "global"}[slot]
    select_storage(monkeypatch, target)
    before = replacement._tree(retained) if retained.exists() else None

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        save(kind)

    if before is None:
        assert not retained.exists()
    else:
        assert replacement._tree(retained) == before
    assert not (retained / "new").exists()


@pytest.mark.parametrize("kind", ["graph", "manifest"])
def test_global_save_selected_file_alias_is_guarded(
    tmp_path, isolated_storage, registration, kind,
):
    retained = tmp_path / "archive"
    retained.mkdir()
    (retained / "old.txt").write_bytes(b"preserved content")
    registration(retained)
    isolated_storage.mkdir()
    selected = global_store._GLOBAL_GRAPH if kind == "graph" else global_store._GLOBAL_MANIFEST
    selected.symlink_to(retained / "old.txt")
    before = replacement._tree(retained)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        save(kind)

    assert selected.is_symlink()
    assert replacement._tree(retained) == before


@pytest.mark.parametrize("kind", ["graph", "manifest"])
def test_global_save_rechecks_after_serialization(
    monkeypatch, isolated_storage, registration, kind,
):
    selected = global_store._GLOBAL_GRAPH if kind == "graph" else global_store._GLOBAL_MANIFEST
    original = json.dumps
    armed = True

    def serialize_then_register(value, *args, **kwargs):
        nonlocal armed
        content = original(value, *args, **kwargs)
        if armed:
            armed = False
            registration(selected)
        return content

    monkeypatch.setattr(global_store.json, "dumps", serialize_then_register)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        save(kind)

    assert not selected.exists()
    assert [path.name for path in isolated_storage.iterdir()] == [replacement.GUARD]


def backup_path():
    manifest = global_store._GLOBAL_MANIFEST
    stamp = int(global_store.datetime.now(timezone.utc).timestamp())
    return manifest.with_suffix(manifest.suffix + f".corrupt.{stamp}")


@pytest.mark.parametrize("namespace", ["archive", "quarantine"])
def test_corrupt_manifest_in_retained_namespace_is_not_renamed(
    tmp_path, monkeypatch, registration, namespace,
):
    retained = tmp_path / namespace
    retained.mkdir()
    select_storage(monkeypatch, retained)
    global_store._GLOBAL_MANIFEST.write_text("not json")
    registration(retained)
    before = replacement._tree(retained)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        global_store._load_manifest()

    assert replacement._tree(retained) == before


def test_corrupt_manifest_backup_alias_is_guarded(
    tmp_path, isolated_storage, registration,
):
    retained = tmp_path / "archive"
    retained.mkdir()
    (retained / "old.txt").write_bytes(b"preserved content")
    registration(retained)
    isolated_storage.mkdir()
    global_store._GLOBAL_MANIFEST.write_text("not json")
    backup = backup_path()
    backup.symlink_to(retained / "old.txt")
    before = replacement._tree(retained)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        global_store._load_manifest()

    assert global_store._GLOBAL_MANIFEST.read_text() == "not json"
    assert backup.is_symlink()
    assert replacement._tree(retained) == before


@pytest.mark.parametrize("selected", ["source", "backup"])
def test_corrupt_manifest_late_registration_refuses_without_fallback(
    monkeypatch, isolated_storage, registration, selected, capsys,
):
    isolated_storage.mkdir()
    global_store._GLOBAL_MANIFEST.write_text("not json")
    destination = backup_path()
    path = global_store._GLOBAL_MANIFEST if selected == "source" else destination
    original = json.loads

    def parse_after_registration(value, *args, **kwargs):
        if value == "not json":
            registration(path)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(global_store.json, "loads", parse_after_registration)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        global_store._load_manifest()

    assert global_store._GLOBAL_MANIFEST.read_text() == "not json"
    assert not destination.exists()
    assert "starting fresh" not in capsys.readouterr().err.lower()


def test_corrupt_manifest_source_alias_is_guarded(
    tmp_path, isolated_storage, registration,
):
    retained = tmp_path / "archive"
    retained.mkdir()
    (retained / "old.json").write_text("not json")
    registration(retained)
    isolated_storage.mkdir()
    global_store._GLOBAL_MANIFEST.symlink_to(retained / "old.json")
    before = replacement._tree(retained)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        global_store._load_manifest()

    assert global_store._GLOBAL_MANIFEST.is_symlink()
    assert not backup_path().exists()
    assert replacement._tree(retained) == before


def test_ordinary_global_storage_round_trip(isolated_storage):
    graph = nx.DiGraph()
    graph.add_edge("one", "two", relation="imports")
    manifest = {"version": 1, "repos": {"example": {"node_count": 2}}}

    global_store._save_global_graph(graph)
    global_store._save_manifest(manifest)

    loaded = global_store._load_global_graph()
    assert loaded.is_directed()
    assert list(loaded.edges(data=True)) == [("one", "two", {"relation": "imports"})]
    assert global_store._load_manifest() == manifest
    assert global_store.global_list() == manifest["repos"]
    assert sorted(path.name for path in isolated_storage.iterdir()) == [
        "global-graph.json", "global-manifest.json",
    ]


def test_valid_retained_storage_reads_do_not_mutate(isolated_storage, registration):
    global_store._save_global_graph(nx.Graph([("one", "two")]))
    manifest = {"version": 1, "repos": {"example": {}}}
    global_store._save_manifest(manifest)
    registration(isolated_storage)
    before = replacement._tree(isolated_storage)

    assert global_store._load_manifest() == manifest
    assert global_store.global_list() == manifest["repos"]
    assert global_store._load_global_graph().has_edge("one", "two")
    assert replacement._tree(isolated_storage) == before


def test_ordinary_corrupt_manifest_backup_preserves_bytes(isolated_storage, capsys):
    isolated_storage.mkdir()
    global_store._GLOBAL_MANIFEST.write_bytes(b"not json")
    backup = backup_path()

    assert global_store._load_manifest() == {"version": 1, "repos": {}}

    assert backup.read_bytes() == b"not json"
    assert not global_store._GLOBAL_MANIFEST.exists()
    assert "moved to" in capsys.readouterr().err


def test_ordinary_backup_oserror_keeps_existing_fallback(
    monkeypatch, isolated_storage, capsys,
):
    isolated_storage.mkdir()
    global_store._GLOBAL_MANIFEST.write_bytes(b"not json")
    original = Path.rename

    def rename(path, destination):
        if path == global_store._GLOBAL_MANIFEST:
            raise OSError("intercepted rename failure")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", rename)
    assert global_store._load_manifest() == {"version": 1, "repos": {}}

    assert global_store._GLOBAL_MANIFEST.read_bytes() == b"not json"
    assert not backup_path().exists()
    assert "could not be backed up" in capsys.readouterr().err
