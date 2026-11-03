"""Direct wiki export observes retained-archive admission at each mutation."""
from pathlib import Path
import socket
import subprocess

import networkx as nx
import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from graphify import wiki


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("wiki guard tests must not launch a process or use network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


@pytest.fixture
def sample():
    graph = nx.Graph()
    graph.add_node("alpha", label="Alpha", file_type="code", source_file="a.py")
    return graph, {0: ["alpha"]}


@pytest.fixture
def registration(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({"request_digest": "prior", "state": "COMMITTED"}))
    def register(path):
        replacement._register(path, journal, "prior", "archive")
    return register


@pytest.mark.parametrize("selected", ["archive", "descendant", "alias", "absent"])
def test_direct_wiki_refuses_before_mkdir_or_delete(tmp_path, registration, sample, selected):
    archive = tmp_path / "archive"
    archive.mkdir()
    retained = archive / "old.md"
    retained.write_text("retained")
    registration(archive)
    absent = tmp_path / "absent"
    registration(absent)
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    destination = {"archive": archive, "descendant": archive / "new", "alias": alias, "absent": absent}[selected]
    before = replacement._tree(archive)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        wiki.to_wiki(sample[0], sample[1], destination)
    assert replacement._tree(archive) == before
    assert not (archive / "new").exists()
    assert not absent.exists()


def test_registration_before_stale_delete_preserves_article(tmp_path, monkeypatch, registration, sample):
    out = tmp_path / "wiki"
    out.mkdir()
    stale = out / "old.md"
    stale.write_text("retained")
    original = Path.glob
    def register_before_glob(path, *args, **kwargs):
        entries = list(original(path, *args, **kwargs))
        if path == out:
            registration(out)
        return iter(entries)
    monkeypatch.setattr(Path, "glob", register_before_glob)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        wiki.to_wiki(sample[0], sample[1], out)
    assert stale.read_text() == "retained"
    assert sorted(p.name for p in out.iterdir()) == ["old.md"]


@pytest.mark.parametrize("boundary", ["community", "god", "index"])
@pytest.mark.parametrize("alias", [False, True])
def test_registration_or_alias_during_render_blocks_final_write(
    tmp_path, monkeypatch, registration, sample, boundary, alias,
):
    out = tmp_path / "wiki"
    retained = tmp_path / "archive"
    retained.mkdir()
    original = retained / "retained.txt"
    original.write_text("retained")
    registration(retained)
    target = {"community": "Community_0.md", "god": "Alpha.md", "index": "index.md"}[boundary]
    renderer = {"community": "_community_article", "god": "_god_node_article", "index": "_index_md"}[boundary]
    captured = []
    def render(*args, **kwargs):
        if alias:
            (out / target).symlink_to(original)
        else:
            registration(out)
            captured.append(replacement._tree(out))
        return "replacement article"
    monkeypatch.setattr(wiki, renderer, render)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        wiki.to_wiki(sample[0], sample[1], out, god_nodes_data=[{"id": "alpha", "label": "Alpha", "degree": 0}])
    assert original.read_text() == "retained"
    if alias:
        assert (out / target).is_symlink()
    else:
        assert replacement._tree(out) == captured[0]


def test_ordinary_direct_wiki_still_exports(tmp_path, sample):
    out = tmp_path / "wiki"
    out.mkdir()
    (out / "stale.md").write_text("old")
    assert wiki.to_wiki(sample[0], sample[1], out, god_nodes_data=[{"id": "alpha", "label": "Alpha", "degree": 0}]) == 2
    assert sorted(path.name for path in out.iterdir()) == ["Alpha.md", "Community_0.md", "index.md"]
