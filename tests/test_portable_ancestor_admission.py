"""Tracked ancestor authority must refuse before portable publication."""
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from graphify import hooks, merge_finalize, paths, portable, transaction as tx
from graphify.merge_guard import check_merge_commit
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401
from tests.test_merge_finalize import pending_repo

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX portable publication")
OUTPUT = "nested/graphify-out"
LEGACY = b'{"graph":{},"nodes":[],"links":[]}'
WATERMARK = b'{"graph":{"_graphify_protocol":null}}'


@pytest.fixture
def enrolled_repo(tmp_path, monkeypatch):
    root = init_repo(tmp_path / "repo")
    monkeypatch.setattr(paths, "GRAPHIFY_OUT", OUTPUT)
    (root / "main.py").write_text("def main():\n    return 1\n")
    git(root, "add", "main.py")
    git(root, "commit", "-m", "source")
    hooks.install(root, merge_guard=True)
    _, sources = portable.source_records_from_tree(root, OUTPUT)
    graph = {"graph": {}, "directed": False, "multigraph": False,
             "nodes": [{"id": "main", "label": "main"}], "links": []}
    output = root / OUTPUT
    output.mkdir(parents=True)
    for name, payload in portable.make_bundle(graph, sources, OUTPUT).items():
        (output / name).write_bytes(payload)
    git(root, "add", ".gitattributes", OUTPUT)
    git(root, "commit", "-m", "complete bundle")
    portable.open_portable_graph_snapshot(root, OUTPUT)
    return root


def stage_blob(root, path, payload, mode="100644"):
    oid = subprocess.check_output(
        ["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=payload,
    ).decode().strip()
    subprocess.run(["git", "-C", str(root), "update-index", "-z", "--index-info"],
                   input=f"{mode} {oid}\t{path}\0".encode(), check=True)
    return oid


def protected(root):
    return ((root / ".git/index").read_bytes(), git(root, "rev-parse", "HEAD").stdout,
            {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode)
             for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts})


@pytest.mark.parametrize("parent", ["", "nested/"])
@pytest.mark.parametrize("name,payload,mode", [
    (tx.PROTOCOL_FILE, b"malformed", "100644"),
    (tx.PROTOCOL_FILE.upper(), b"malformed", "100644"),
    (".graphify-prepare-incomplete/held.txt", b"state", "100644"),
    (".GRAPHIFY-GC-ROOT-held/held.txt", b"state", "100644"),
    (portable.PORTABLE_FILE, b"malformed", "100644"),
    (portable.PORTABLE_FILE.upper(), b"malformed", "100644"),
    ("graph.json", WATERMARK, "100644"),
    ("graph.json", b"malformed", "100644"),
    ("graph.json", b'{"graph":[]}', "100644"),
    ("graph.json", b"[]", "100644"),
    ("graph.json", b'\xff', "100644"),
    ("graph.json", LEGACY, "120000"),
    ("graph.json/held.txt", b"state", "100644"),
])
def test_indexed_ancestor_authority_refuses_and_preserves(enrolled_repo, parent, name, payload, mode):
    root = enrolled_repo
    stage_blob(root, parent + name, payload, mode)
    before = protected(root)
    with pytest.raises(RuntimeError, match="ancestor|managed|authority"):
        merge_finalize.validate_index_bundle(root, OUTPUT)
    assert protected(root) == before
    with pytest.raises(RuntimeError, match="ancestor|managed|authority"):
        check_merge_commit(OUTPUT, "pre-commit", root=root)
    assert protected(root) == before


def test_actual_guarded_commit_refuses_tracked_ancestor(enrolled_repo):
    root = enrolled_repo
    marker = root / tx.PROTOCOL_FILE
    marker.write_bytes(b"malformed")
    git(root, "add", tx.PROTOCOL_FILE)
    hook = root / ".git/hooks/pre-commit"
    installed = root / ".git/pre-commit-under-test"
    installed.write_bytes(hook.read_bytes())
    installed.chmod(0o755)
    hook.write_text(
        "#!/bin/sh\n"
        'index=${GIT_INDEX_FILE:-$(git rev-parse --git-path index)}\n'
        'cp "$index" .git/guard-entry-index\n'
        + shlex.quote(str(installed)) + ' "$@"\nresult=$?\n'
        'cp "$index" .git/guard-exit-index\nexit "$result"\n'
    )
    before = protected(root)
    staged = git(root, "ls-files", "--stage", "-z").stdout
    result = git(root, "commit", "-m", "ancestor authority", skip_hooks=False, check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "managed" in result.stderr or "ancestor" in result.stderr
    assert git(root, "rev-parse", "HEAD").stdout == before[1]
    assert git(root, "ls-files", "--stage", "-z").stdout == staged
    assert (root / ".git/guard-entry-index").read_bytes() == (root / ".git/guard-exit-index").read_bytes()
    assert protected(root)[2] == before[2]
    assert not (Path.home() / ".cache/graphify-rebuild.log").exists()


def test_prospective_finalization_refuses_index_only_ancestor_without_publication():
    root = pending_repo()
    stage_blob(root, tx.PROTOCOL_FILE, b"malformed indexed authority")
    assert not (root / tx.PROTOCOL_FILE).exists()
    before = protected(root)
    merge_head = (root / ".git/MERGE_HEAD").read_bytes()
    record = merge_finalize._record_path(root, "graphify-out")
    assert not record.exists()
    with pytest.raises(RuntimeError, match="nested beneath managed"):
        merge_finalize.finalize_merge(root, "graphify-out")
    assert protected(root) == before
    assert (root / ".git/MERGE_HEAD").read_bytes() == merge_head
    assert not record.exists()
    assert not (root / "graphify-out" / portable.PORTABLE_FILE).exists()
    assert not (root / tx.PROTOCOL_FILE).exists()


@pytest.mark.parametrize("path,payload,mode", [
    ("graph.json", LEGACY, "100644"),
    ("nested/GRAPH.JSON", LEGACY, "100755"),
    ("graph.json", b'{"graph":{"note":NaN},"graph":{}}', "100644"),
    ("unrelated/.graphify_protocol.json", b"malformed", "100644"),
    (".graphify-protocol.json", b"malformed", "100644"),
    ("nested/.graphify-prepare", b"not the matching prefix", "100644"),
])
def test_negative_controls_commit_and_remain_readable(enrolled_repo, tmp_path, path, payload, mode):
    root = enrolled_repo
    oid = stage_blob(root, path, payload, mode)
    snapshot = merge_finalize.validate_index_bundle(root, OUTPUT)
    check_merge_commit(OUTPUT, "pre-commit", root=root)
    git(root, "commit", "-m", "unrelated control", skip_hooks=False)
    assert git(root, "rev-parse", "HEAD:" + path).stdout.strip() == oid
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--no-local", str(root), str(clone))
    before = protected(clone)
    assert portable.open_portable_graph_snapshot(clone, OUTPUT).content_id == snapshot.content_id
    assert protected(clone) == before


@pytest.mark.parametrize("staged_deletion", [False, True])
def test_untracked_ancestor_authority_does_not_enter_git_admission(enrolled_repo, tmp_path, staged_deletion):
    root = enrolled_repo
    if staged_deletion:
        stage_blob(root, tx.PROTOCOL_FILE, b"malformed")
        git(root, "commit", "-m", "bypassed ancestor authority")
        git(root, "update-index", "--force-remove", tx.PROTOCOL_FILE)
    (root / tx.PROTOCOL_FILE).write_bytes(b"local authority remains")
    before = protected(root)
    snapshot = merge_finalize.validate_index_bundle(root, OUTPUT)
    check_merge_commit(OUTPUT, "pre-commit", root=root)
    assert protected(root) == before
    git(root, "commit", "--allow-empty", "-m", "clean prospective clone", skip_hooks=False)
    assert (root / tx.PROTOCOL_FILE).read_bytes() == b"local authority remains"
    with pytest.raises(tx.PendingTransactionError, match="nested beneath managed"):
        portable.open_portable_graph_snapshot(root, OUTPUT)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--no-local", str(root), str(clone))
    assert portable.open_portable_graph_snapshot(clone, OUTPUT).content_id == snapshot.content_id


@pytest.mark.parametrize("name", [*tx._COORDINATION_FILES, *tx._COORDINATION_PREFIXES,
                                  ".graphify-prepare-", ".graphify-retired-", ".graphify-gc-root-",
                                  ".graphify-gc-journal-", ".graphify-gc-quarantine-",
                                  portable.PORTABLE_FILE])
@pytest.mark.parametrize("directory", [False, True])
def test_shared_name_predicates_match_pinned_reader(tmp_path, name, directory):
    root = tmp_path / "ancestor"
    root.mkdir()
    child = root / name.upper()
    if directory:
        child.mkdir()
        (child / "held.txt").write_bytes(b"state")
        path = child.name + "/held.txt"
    else:
        child.write_bytes(b"state")
        path = child.name
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with tx.pin_output(root, mutation=False) as capability:
        assert tx._managed_authority_present(capability) is True
    reads = []

    def read_blob(oid, limit):
        reads.append((oid, limit))
        pytest.fail("name-only authority must not read its blob")

    with pytest.raises(portable.PortableGraphError, match="nested beneath managed"):
        portable._validate_ancestor_authority({path: ("100644", "oid")}, "child/output", read_blob)
    assert reads == []
    assert before == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("payload,expected", [
    (b"{}", False), (LEGACY, False), (b'{"graph":{"note":NaN}}', False),
    (b'{"graph":{"_graphify_protocol":null}}', True),
    (b'{"graph":{"_graphify_protocol":0}}', True),
    (b'{"graph":{"_graphify_protocol":{}},"graph":{}}', False),
    (b'{"graph":{},"graph":{"_graphify_protocol":false}}', True),
    (b'{"gr\\u0061ph":{"_graphify_protocol":{}}}', True),
])
def test_shared_parsed_graph_predicate_matches_reader(tmp_path, payload, expected):
    root = tmp_path / "ancestor"
    root.mkdir()
    (root / "GRAPH.JSON").write_bytes(payload)
    with tx.pin_output(root, mutation=False) as capability:
        assert tx._managed_authority_present(capability) is expected
    reads = []

    def read_blob(oid, limit):
        reads.append((oid, limit))
        return payload

    if expected:
        with pytest.raises(portable.PortableGraphError, match="nested beneath managed"):
            portable._validate_ancestor_authority({"GRAPH.JSON": ("100755", "oid")}, "child/output", read_blob)
    else:
        portable._validate_ancestor_authority({"GRAPH.JSON": ("100755", "oid")}, "child/output", read_blob)
    from graphify.security import _max_graph_file_bytes
    assert reads == [("oid", _max_graph_file_bytes())]


@pytest.mark.parametrize("entries", [
    {"graph.json": ("100644", "one"), "GRAPH.JSON": ("100644", "two")},
    {"graph.json/first": ("100644", "one"), "GRAPH.JSON/second": ("100644", "two")},
    {"graph.json/held.txt": ("100644", "one")},
    {"graph.json": ("120000", "one")},
    {"graph.json": ("160000", "one")},
])
def test_ambiguous_or_nonregular_ancestor_graph_refuses_before_read(entries):
    with pytest.raises(portable.PortableGraphError, match="ambiguous|unsafe"):
        portable._validate_ancestor_authority(entries, "nested/out", lambda *_: pytest.fail("unsafe graph read"))


def test_ancestor_graph_uses_reader_limit_and_preserves_decoder_semantics(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", "16")
    reads = []

    def read_blob(oid, limit):
        reads.append((oid, limit))
        return b" " * 17

    with pytest.raises(portable.PortableGraphError, match="limit"):
        portable._validate_ancestor_authority({"graph.json": ("100644", "oid")}, "nested/out", read_blob)
    assert reads == [("oid", 16)]


def test_each_intermediate_ancestor_is_checked_and_unrelated_siblings_are_ignored():
    for parent in ("", "a/", "a/b/", "a/b/c/"):
        with pytest.raises(portable.PortableGraphError, match="nested beneath managed"):
            portable._validate_ancestor_authority(
                {parent + tx.PROTOCOL_FILE: ("100644", "oid")}, "a/b/c/output",
                lambda *_: pytest.fail("name-only authority read"),
            )
    portable._validate_ancestor_authority(
        {"a/b/sibling/" + tx.PROTOCOL_FILE: ("100644", "oid")}, "a/b/c/output",
        lambda *_: pytest.fail("unrelated sibling read"),
    )


def test_coordination_ignored_names_remain_exact(tmp_path):
    root = tmp_path / "ancestor"
    root.mkdir()
    name = tx.PROTOCOL_FILE.upper()
    (root / name).write_bytes(b"malformed")
    with tx.pin_output(root, mutation=False) as capability:
        assert tx._coordination_present(capability, ignored_names=frozenset({name})) is False
        assert tx._coordination_present(capability, ignored_names=frozenset({tx.PROTOCOL_FILE})) is True


@pytest.mark.parametrize("payload", [b"invalid", b'\xff', b"[]", b'{"graph":null}'])
def test_shared_malformed_ancestor_graph_refusal(tmp_path, payload):
    root = tmp_path / "ancestor"
    root.mkdir()
    (root / "graph.json").write_bytes(payload)
    with tx.pin_output(root, mutation=False) as capability:
        with pytest.raises(tx.PendingTransactionError, match="authority is malformed"):
            tx._managed_authority_present(capability)
    with pytest.raises(portable.PortableGraphError, match="authority is malformed"):
        portable._validate_ancestor_authority({"graph.json": ("100644", "oid")}, "nested/out",
                                            lambda *_: payload)
