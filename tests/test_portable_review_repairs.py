"""Review regressions: refuse unreadable publication before changing the index."""
import os
import subprocess
import sys

import pytest

from graphify import merge_finalize as finalizer, portable
from graphify.merge_guard import check_merge_commit
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401
from tests.test_merge_finalize import pending_repo
from tests.test_portable import repository  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX portable publication")


def index_blob(repo, path, payload):
    oid = subprocess.check_output(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=payload,
    ).decode().strip()
    # Preserve raw Git names even when macOS normalizes command-line arguments.
    subprocess.run(["git", "-C", str(repo), "update-index", "-z", "--index-info"],
                   input=f"100644 {oid}\t{path}\0".encode(), check=True)
    return oid


def protected(repo):
    output = repo / "graphify-out"
    return ((repo / ".git/index").read_bytes(),
            (repo / ".git/MERGE_HEAD").read_bytes(),
            git(repo, "rev-parse", "HEAD").stdout,
            {str(p.relative_to(output)): p.read_bytes()
             for p in output.rglob("*") if p.is_file()})


def test_non_python_output_parent_alias_refuses_before_publication():
    repo = pending_repo()
    index_blob(repo, "Graphify-Out/notes.txt", b"notes\n")
    before = protected(repo)
    with pytest.raises(RuntimeError, match="colli|alias"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert protected(repo) == before
    assert not finalizer._record_path(repo, "graphify-out").exists()


@pytest.mark.parametrize("output,path", [
    ("graphify-out", "Graphify-Out/notes.txt"),
    ("nested/café/out", "nested/cafe\u0301/notes.txt"),
    ("nested/ſnapshot/out", "nested/snapshot/notes.txt"),
])
def test_reader_rejects_non_python_output_prefix_alias(tmp_path, output, path):
    repo = init_repo(tmp_path / "aliases")
    index_blob(repo, path, b"notes\n")
    git(repo, "commit", "-m", "alias")
    assert path.encode() in subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "HEAD"])
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(portable.PortableGraphError, match="colli|alias"):
        portable.source_records_from_tree(repo, output)
    assert (repo / ".git/index").read_bytes() == before


def test_reader_allows_exact_shared_parent_and_distinct_prefix(tmp_path):
    repo = init_repo(tmp_path / "distinct")
    for path in ("nested/notes.txt", "nested/portable-outside/notes.txt"):
        index_blob(repo, path, b"notes\n")
    git(repo, "commit", "-m", "distinct")
    assert portable.source_records_from_tree(repo, "nested/portable-out")[1] == ()


def test_inventory_above_real_reader_limit_refuses_before_publication():
    repo = pending_repo()
    oid = index_blob(repo, "notes.txt", b"notes\n")
    prefix = "/".join(["inventory", "a" * 200, "b" * 200, "c" * 200])
    records = b"".join(
        f"100644 {oid}\t{prefix}/{i:05d}-{'x' * 90}.txt\0".encode()
        for i in range(12000))
    subprocess.run(["git", "-C", str(repo), "update-index", "-z", "--index-info"],
                   input=records, check=True)
    before = protected(repo)
    with pytest.raises(RuntimeError, match="limit|bounds"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert protected(repo) == before
    assert not finalizer._record_path(repo, "graphify-out").exists()


def test_inventory_budget_includes_new_portable_closure(monkeypatch):
    repo = pending_repo()
    tree = git(repo, "write-tree").stdout.strip()
    current = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "-z", tree])
    additions = sum(len(f"100644 blob {'0' * 40}\tgraphify-out/{name}\0".encode())
                    for name in ("manifest.json", portable.PORTABLE_FILE))
    monkeypatch.setattr(portable, "_MAX_METADATA", len(current) + additions - 1)
    before = protected(repo)
    with pytest.raises(RuntimeError, match="inventory.*bounds|inspection exceeds bounds"):
        finalizer.finalize_merge(repo, "graphify-out")
    assert protected(repo) == before
    assert not finalizer._record_path(repo, "graphify-out").exists()


@pytest.mark.parametrize("name", [portable.PORTABLE_FILE, "manifest.json"])
def test_metadata_refuses_before_oversized_blob_is_read(repository, monkeypatch, name):
    oid = index_blob(repository, "graphify-out/" + name,
                     b" " * (portable._MAX_METADATA + 1))
    original = finalizer._git
    reads = []

    def observed(root, *args, **kwargs):
        if args[:2] == ("cat-file", "blob"):
            reads.append(args[-1])
        return original(root, *args, **kwargs)

    monkeypatch.setattr(finalizer, "_git", observed)
    before = (repository / ".git/index").read_bytes()
    with pytest.raises(RuntimeError, match="limit|bounds"):
        check_merge_commit("graphify-out", "pre-commit", root=repository)
    assert oid not in reads
    assert (repository / ".git/index").read_bytes() == before


def test_aggregate_refuses_before_any_artifact_blob_is_read(repository, monkeypatch):
    output = repository / "graphify-out"
    budget = sum(p.stat().st_size for p in output.iterdir()) - 1
    monkeypatch.setattr(portable, "_MAX_TOTAL", budget)
    original = finalizer._git
    reads = []

    def observed(root, *args, **kwargs):
        if args[:2] == ("cat-file", "blob"):
            reads.append(args[-1])
        return original(root, *args, **kwargs)

    monkeypatch.setattr(finalizer, "_git", observed)
    before = (repository / ".git/index").read_bytes()
    with pytest.raises(RuntimeError, match="limit|bounds"):
        finalizer.validate_index_bundle(repository, "graphify-out")
    assert not reads
    assert (repository / ".git/index").read_bytes() == before


@pytest.mark.parametrize("option", ["core.sparseCheckout", "core.splitIndex", "index.sparse"])
@pytest.mark.parametrize("value", ["2", "-1"])
def test_git_true_index_settings_refuse(option, value, tmp_path):
    repo = init_repo(tmp_path / "config")
    git(repo, "config", option, value)
    with pytest.raises(finalizer.MergeFinalizeError, match="sparse|split"):
        finalizer._require_full_index(repo)


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_git_false_index_settings_remain_supported(tmp_path, value):
    repo = init_repo(tmp_path / "config")
    for option in ("core.sparseCheckout", "core.splitIndex", "index.sparse"):
        git(repo, "config", option, value)
    finalizer._require_full_index(repo)


def test_private_record_fifo_refuses_promptly(tmp_path):
    path = tmp_path / "record.json"
    os.mkfifo(path, 0o600)
    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-c",
         "import sys; from pathlib import Path; "
         "from graphify.merge_finalize import _read_record; _read_record(Path(sys.argv[1]))",
         str(path)], capture_output=True, text=True, timeout=3,
    )
    assert result.returncode != 0
    assert "unsafe private finalization record" in result.stderr
    assert path.is_fifo()


def test_legacy_observer_streams_instead_of_materializing_graph(tmp_path, monkeypatch):
    from tests.test_merge_finalize_observer import repository as observer_repository

    graph = (b'{"nodes":["' + b"x" * (2 * 1024 * 1024)
             + b'"],"graph":{},"links":[]}')
    repo = observer_repository(tmp_path, graph=graph)
    original = finalizer._git

    def bounded_inspection(root, *args, **kwargs):
        assert args[:2] != ("cat-file", "blob"), "observer retained a whole graph blob"
        return original(root, *args, **kwargs)

    monkeypatch.setattr(finalizer, "_git", bounded_inspection)
    assert finalizer.observe_committed_portable(repo, "graphify-out") is False


def test_legacy_observer_closes_stream_after_invalid_json(tmp_path, monkeypatch):
    from tests.test_merge_finalize_observer import repository as observer_repository

    repo = observer_repository(tmp_path)
    closed = []

    def broken_stream(*args, **kwargs):
        try:
            yield b"invalid"
            pytest.fail("parser should stop after invalid syntax")
        finally:
            closed.append(True)

    monkeypatch.setattr(finalizer, "_git_chunks", broken_stream)
    assert finalizer.observe_committed_portable(repo, "graphify-out") is False
    assert closed == [True]


@pytest.mark.parametrize("operation", ["limit", "close", "failure"])
def test_bounded_git_transport_reaps_child(tmp_path, monkeypatch, operation):
    from graphify._git_io import GitReadError, git_stdout

    repo = init_repo(tmp_path / "transport")
    oid = index_blob(repo, "payload", b"x" * (2 * 1024 * 1024))
    original = subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture)
    command = ["git", "-C", str(repo), "cat-file", "blob",
               "0" * 40 if operation == "failure" else oid]
    stream = git_stdout(command, dict(os.environ), 64 if operation == "limit" else 3 * 1024 * 1024)
    if operation == "close":
        assert next(stream)
        stream.close()
    else:
        with pytest.raises(GitReadError, match="bounds" if operation == "limit" else "failed"):
            list(stream)
    assert len(children) == 1 and children[0].poll() is not None


def test_bounded_git_transport_excludes_consumer_time(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import time
    from graphify import _git_io

    repo = init_repo(tmp_path / "consumer-time")
    payload = b"x" * (128 * 1024)
    oid = index_blob(repo, "payload", payload)
    elapsed = [0.0]
    monkeypatch.setattr(_git_io, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + elapsed[0]))
    stream = _git_io.git_stdout(["git", "-C", str(repo), "cat-file", "blob", oid],
                                dict(os.environ), len(payload))
    first = next(stream)
    elapsed[0] += 35  # Parsing runs in the caller, outside active Git I/O.
    assert first + b"".join(stream) == payload


def test_bounded_git_transport_still_limits_active_read_time(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from graphify import _git_io

    repo = init_repo(tmp_path / "read-time")
    payload = b"x" * (128 * 1024)
    oid = index_blob(repo, "payload", payload)
    # Two active reads use 16 seconds each, with no consumer pause between them.
    elapsed = iter((0.0, 16.0, 16.0, 16.0, 32.0))
    monkeypatch.setattr(_git_io, "time", SimpleNamespace(monotonic=lambda: next(elapsed)))
    with pytest.raises(_git_io.GitReadError, match="timed out"):
        list(_git_io.git_stdout(["git", "-C", str(repo), "cat-file", "blob", oid],
                               dict(os.environ), len(payload)))
