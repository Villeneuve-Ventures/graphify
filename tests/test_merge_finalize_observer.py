"""Post-event portable classification must preserve ordinary legacy hook routes."""
import hashlib
import os
import shlex
import sys

import pytest

from graphify import hooks
from graphify.merge_finalize import MergeFinalizeError, observe_committed_portable
from graphify.merge_guard import _git
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX post-event hook qualification")
LEGACY = b'{"directed":true,"multigraph":false,"graph":{},"nodes":[],"links":[]}'
PORTABLE = b'{"directed":true,"multigraph":false,"graph":{"_graphify_protocol":{"schema":2,"protocol_epoch":2,"state":"portable_complete"}},"nodes":[],"links":[]}'


def record(root, path, payload):
    oid = _git(root, "hash-object", "-w", "--stdin", input=payload).strip()
    _git(root, "update-index", "-z", "--index-info", input=b"100644 " + oid + b"\t" + path + b"\0")


def repository(tmp_path, *, graph=LEGACY, extra=(), envelope=None, unborn=False):
    repo = init_repo(tmp_path / "repo")
    git(repo, "config", "core.ignorecase", "false")
    output = repo / "graphify-out"
    output.mkdir()
    (repo / "base.py").write_text("def base():\n    return 1\n")
    record(repo, b"base.py", (repo / "base.py").read_bytes())
    if graph is not None:
        (output / "graph.json").write_bytes(graph)
        record(repo, b"graphify-out/graph.json", graph)
    if envelope is not None:
        (output / ".graphify_portable.json").write_bytes(envelope)
        record(repo, b"graphify-out/.graphify_portable.json", envelope)
    for path, payload in extra:
        record(repo, path, payload)
    if not unborn:
        git(repo, "commit", "-m", "observer fixture")
    return repo


def protected(repo):
    return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in repo.rglob("*") if p.is_file() and ".git/objects/" not in str(p)}


@pytest.mark.parametrize("extra", [(), ((b"legal\\unix.txt", b"extra"),),
                                  ((b"Case.txt", b"A"), (b"case.txt", b"B")),
                                  ((b"bad-\xff.txt", b"extra"),)],
                         ids=["ordinary", "backslash", "case-collision", "nonutf8"])
def test_unrelated_legacy_paths_do_not_activate_portable_validation(tmp_path, extra):
    repo = repository(tmp_path, extra=extra)
    before = protected(repo)
    assert observe_committed_portable(repo, "graphify-out") is False
    assert protected(repo) == before


def test_observer_normalizes_legacy_dot_selector(tmp_path):
    repo = repository(tmp_path)
    before = protected(repo)
    assert observe_committed_portable(repo, "./graphify-out") is False
    assert protected(repo) == before


def test_unclassifiable_legacy_graph_retains_the_configured_reader_bound(tmp_path, monkeypatch):
    repo = repository(tmp_path, graph=LEGACY + b" " * 128)
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", "96")
    before = protected(repo)
    with pytest.raises(MergeFinalizeError, match="reader limit"):
        observe_committed_portable(repo, "graphify-out")
    assert protected(repo) == before


def test_unborn_head_has_no_committed_portable_authority(tmp_path):
    repo = repository(tmp_path, unborn=True)
    before = protected(repo)
    assert observe_committed_portable(repo, "graphify-out") is False
    assert protected(repo) == before


@pytest.mark.parametrize("graph,envelope,output,extra", [
    (None, b"{}", "graphify-out", ()),
    (PORTABLE, b"{}", "graphify-out", ()),
    (PORTABLE, None, "graphify-out", ()),
    (PORTABLE, b"{}", "./graphify-out", ()),
    (PORTABLE, b"{}", "graphify-out", ((b"legal\\unix.txt", b"extra"),)),
], ids=["orphan-envelope", "malformed-envelope", "marker-only", "dot-portable", "portable-bad-path"])
def test_known_portable_signal_suppresses_rebuild_even_on_refusal(tmp_path, graph, envelope, output, extra):
    repo = repository(tmp_path, graph=graph, envelope=envelope, extra=extra)
    before = protected(repo)
    assert observe_committed_portable(repo, output) is True
    assert protected(repo) == before


def test_marker_only_above_selected_cap_remains_portable(tmp_path, monkeypatch):
    repo = repository(tmp_path, graph=PORTABLE)
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", "96")
    before = protected(repo)
    with pytest.raises(MergeFinalizeError, match="reader limit"):
        observe_committed_portable(repo, "graphify-out")
    assert protected(repo) == before


@pytest.mark.parametrize("portable", [False, True], ids=["legacy", "malformed-portable"])
def test_actual_post_checkout_reaches_only_legacy_launcher(tmp_path, portable):
    repo = repository(tmp_path, graph=PORTABLE if portable else LEGACY,
                      envelope=b"{}" if portable else None,
                      extra=((b"legal\\unix.txt", b"extra"),))
    body = hooks._CHECKOUT_SCRIPT.replace("__PINNED_PYTHON__", shlex.quote(sys.executable))
    body = body.replace("__GRAPHIFY_OUTPUT__", "graphify-out")
    body = body.replace(hooks._detached_launch(hooks._REBUILD_BODY_CHECKOUT),
                        'printf "legacy-launcher-reached\\n"\n')
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)
    before_output = {p.name: p.read_bytes() for p in (repo / "graphify-out").iterdir()}
    result = git(repo, "checkout", "-b", "observer-proof", skip_hooks=False)
    assert ("legacy-launcher-reached" in result.stdout + result.stderr) is not portable
    assert {p.name: p.read_bytes() for p in (repo / "graphify-out").iterdir()} == before_output
