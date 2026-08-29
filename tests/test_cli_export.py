"""Integration tests for graphify export subcommands and CLI commands.

Each test builds a minimal graph in a temp dir, runs the CLI command as a subprocess,
and asserts the expected output file exists and is non-empty / valid.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_issue89_cli_destination_resolver_covers_graph_forms(tmp_path, monkeypatch):
    from graphify.cli import _resolve_transaction_destination

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    external = tmp_path / "archive" / "custom.json"
    external.parent.mkdir()
    for argv in (
        ["graphify", "cluster-only", str(corpus), "--graph", str(external)],
        ["graphify", "cluster-only", str(corpus), f"--graph={external}"],
        ["graphify", "cluster-only", "--graph", str(external), "--", str(corpus)],
    ):
        monkeypatch.setattr("sys.argv", argv)
        destination = _resolve_transaction_destination("cluster-only")
        assert destination.graph == external.resolve()
        assert destination.output == external.parent.resolve()


@pytest.mark.parametrize(
    "argv",
    [
        ["graphify", "cluster-only", ".", "--graph"],
        ["graphify", "cluster-only", ".", "--graph="],
        ["graphify", "cluster-only", ".", "--unknown", "value"],
    ],
)
def test_issue89_cli_destination_rejects_ambiguous_options(argv, monkeypatch):
    from graphify.cli import _CliArgumentError, _resolve_transaction_destination

    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(_CliArgumentError):
        _resolve_transaction_destination("cluster-only")


def test_extract_routing_preserves_repeatable_excludes_and_options_before_path(
    tmp_path, monkeypatch
):
    from graphify.cli import _canonical_extract_argv, _resolve_extract_destination

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "graphify",
            "extract",
            "--exclude",
            "vendor",
            "--exclude=generated",
            "--",
            str(corpus),
        ],
    )
    destination = _resolve_extract_destination()
    assert destination.root == corpus.resolve()
    assert _canonical_extract_argv(destination.root) == [
        "graphify",
        "extract",
        str(corpus.resolve()),
        "--exclude",
        "vendor",
        "--exclude=generated",
    ]


PYTHON = sys.executable
FIXTURES = Path(__file__).parent / "fixtures"


def _run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "graphify"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_graph(tmp_path: Path) -> Path:
    """Build a minimal graph.json + analysis/labels files in tmp_path/graphify-out/."""
    out = tmp_path / "graphify-out"
    out.mkdir()

    extraction = json.loads((FIXTURES / "extraction.json").read_text())
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.analyze import god_nodes, surprising_connections
    from graphify.export import to_json

    G = build_from_json(extraction)
    communities = cluster(G)
    cohesion = score_all(G, communities)
    gods = god_nodes(G)
    surprises = surprising_connections(G, communities)
    labels = {cid: f"Community {cid}" for cid in communities}

    to_json(G, communities, str(out / "graph.json"))

    analysis = {
        "communities": {str(k): v for k, v in communities.items()},
        "cohesion": {str(k): v for k, v in cohesion.items()},
        "gods": gods,
        "surprises": surprises,
    }
    (out / ".graphify_analysis.json").write_text(json.dumps(analysis))
    (out / ".graphify_labels.json").write_text(
        json.dumps({str(k): v for k, v in labels.items()})
    )
    return out


def _assert_transactional_export(out: Path) -> None:
    protocol = json.loads((out / ".graphify_protocol.json").read_text())
    receipt = json.loads((out / ".graphify_generation.json").read_text())
    graph = json.loads((out / "graph.json").read_text())
    watermark = graph["graph"]["_graphify_protocol"]
    assert protocol["state"] == "COMPLETE"
    assert receipt["generation"] == protocol["generation"]
    assert watermark["generation"] == receipt["generation"]


def test_full_build_token_runner_exports_before_final_commit(tmp_path):
    from graphify.transaction import (
        begin_transaction,
        finalize_prepared_transaction,
        run_token,
        stage_transaction_handoff,
    )

    out = _make_graph(tmp_path)
    transaction = begin_transaction("full", tmp_path, output=out)
    token = stage_transaction_handoff(transaction)
    run_token(
        token.path,
        [
            "-c",
            "import sys; from graphify.cli import dispatch_command; "
            f"sys.argv=['graphify','export','html','--graph',{str(out / 'graph.json')!r}]; "
            "dispatch_command('export')",
        ],
    )
    assert (out / "graph.html").is_file()
    assert (out / ".graphify_transaction.json").is_file()
    run_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import prepared_workspace_path; "
            "(prepared_workspace_path() / 'graphify-out' / 'manifest.json').write_text('{}')",
        ],
    )
    run_token(
        token.path,
        [
            "-c",
            "from graphify.transaction import finalize_prepared_transaction; "
            "finalize_prepared_transaction()",
        ],
    )
    _assert_transactional_export(out)


def test_rendered_style_prepare_detect_export_finalize_cleanup(tmp_path):
    from graphify.transaction import begin_transaction, stage_transaction_handoff

    out = _make_graph(tmp_path)
    (out / "manifest.json").write_text("{}", encoding="utf-8")
    token = stage_transaction_handoff(
        begin_transaction("full", tmp_path, output=out)
    )
    script = r'''
set -eu
WORKSPACE=$("$PYTHON_BIN" -E -P -B -m graphify.transaction run-token "$TOKEN_PATH" -- -c 'from graphify.transaction import prepared_workspace_path; print(prepared_workspace_path())')
cd "$WORKSPACE"
"$PYTHON_BIN" -E -P -B -m graphify.transaction run-token "$TOKEN_PATH" -- -c 'from pathlib import Path; Path("graphify-out/.graphify_detect.json").write_text("{}")'
test -f graphify-out/.graphify_detect.json
"$PYTHON_BIN" -E -P -B -m graphify.transaction run-token "$TOKEN_PATH" -- -m graphify export html
"$PYTHON_BIN" -E -P -B -m graphify.transaction run-token "$TOKEN_PATH" -- -c 'from graphify.transaction import finalize_prepared_transaction; finalize_prepared_transaction()'
'''
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHON_BIN": sys.executable,
            "TOKEN_PATH": str(token.path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (out / "graph.html").is_file()
    assert not (out.parent / f".graphify-prepare-{token.id}").exists()


def test_direct_export_transfers_close_race_to_claimable_successor(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module
    from graphify.cli import dispatch_command

    out = _make_graph(tmp_path)
    original_close = transaction_module.close_if_queue_empty

    def raced_close(transaction, *, receipt_digest, failpoint=None):
        transaction_module.queue_rebuild(
            "update", tmp_path, output=out, changed_paths=["late.py"]
        )
        return original_close(
            transaction, receipt_digest=receipt_digest, failpoint=failpoint
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(transaction_module, "close_if_queue_empty", raced_close)
    monkeypatch.setattr(sys, "argv", ["graphify", "export", "html"])
    dispatch_command("export")
    assert not (out / ".graphify_transaction.json").exists()
    assert (out / ".graphify_rebuild_queue.jsonl").read_text().strip()
    successor = transaction_module.begin_transaction(
        "runtime", tmp_path, output=out
    )
    claim = transaction_module.claim_rebuild_queue(
        successor, successor.drainer
    )
    assert [item["changed_paths"] for item in claim.items] == [["late.py"]]


def test_extract_rejects_pending_existing_graph_before_dispatch(
    tmp_path, monkeypatch
):
    import graphify.cli as cli_module
    from graphify.transaction import PendingTransactionError, begin_transaction

    out = _make_graph(tmp_path)
    begin_transaction("runtime", tmp_path, output=out)
    dispatched = False

    def unexpected_dispatch(_command):
        nonlocal dispatched
        dispatched = True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "_dispatch_command", unexpected_dispatch)
    monkeypatch.setattr(sys, "argv", ["graphify", "extract", str(tmp_path)])
    with pytest.raises(PendingTransactionError, match="protocol"):
        cli_module.dispatch_command("extract")
    assert not dispatched


@pytest.mark.parametrize(
    ("arguments", "artifact"),
    [
        (["export", "html"], "graph.html"),
        (["export", "svg"], "graph.svg"),
        (["export", "graphml"], "graph.graphml"),
        (["export", "neo4j"], "cypher.txt"),
        (["export", "falkordb"], "cypher.txt"),
    ],
)
def test_issue89_managed_file_exports_commit_generation(tmp_path, arguments, artifact):
    out = _make_graph(tmp_path)
    result = _run(arguments, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (out / artifact).exists()
    _assert_transactional_export(out)


@pytest.mark.parametrize(
    ("arguments", "artifact"),
    [
        (["export", "wiki"], "wiki/index.md"),
        (["export", "obsidian"], "obsidian/graph.canvas"),
    ],
)
def test_issue89_managed_directory_exports_commit_generation(
    tmp_path, arguments, artifact
):
    out = _make_graph(tmp_path)
    result = _run(arguments, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (out / artifact).exists()
    _assert_transactional_export(out)


def test_issue89_managed_export_does_not_republish_unrelated_nested_files(tmp_path):
    out = _make_graph(tmp_path)
    unrelated = out / "private" / "note.txt"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"user-owned")
    unrelated.chmod(0o644)

    result = _run(["export", "html"], tmp_path)

    assert result.returncode == 0, result.stderr
    assert unrelated.read_bytes() == b"user-owned"
    assert unrelated.stat().st_mode & 0o777 == 0o644
    _assert_transactional_export(out)


def test_issue89_external_obsidian_destination_stays_unmanaged(tmp_path):
    out = _make_graph(tmp_path)
    external = tmp_path / "external-vault"
    result = _run(["export", "obsidian", "--dir", str(external)], tmp_path)
    assert result.returncode == 0, result.stderr
    assert (external / "graph.canvas").exists()
    assert not (out / ".graphify_protocol.json").exists()


def test_issue89_managed_callflow_export_commits_generation(tmp_path):
    out = _make_graph(tmp_path)
    (out / "GRAPH_REPORT.md").write_text(
        "# Graph Report\n\n## Architecture\n\nAlpha calls Beta.\n", encoding="utf-8"
    )
    result = _run(["export", "callflow-html", "--graph", str(out / "graph.json")], tmp_path)
    assert result.returncode == 0, result.stderr
    assert list(out.glob("*callflow.html"))
    _assert_transactional_export(out)


def test_issue89_extract_manifest_failure_leaves_recoverable_owner(
    tmp_path, monkeypatch, capsys
):
    import graphify.__main__ as mainmod
    import graphify.detect as detectmod

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        detectmod,
        "save_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("manifest failed")),
    )
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graphify", "extract", str(corpus), "--code-only", "--no-cluster"],
    )
    with pytest.raises(OSError, match="manifest failed"):
        mainmod.main()
    out = corpus / "graphify-out"
    assert (out / ".graphify_transaction.json").exists()
    assert not (out / ".graphify_generation.json").exists()
    captured = capsys.readouterr()
    assert "success" not in captured.out.lower()


# ── graphify export html ─────────────────────────────────────────────────────

def test_export_html_creates_file(tmp_path):
    _make_graph(tmp_path)
    r = _run(["export", "html"], tmp_path)
    assert r.returncode == 0, r.stderr
    html = tmp_path / "graphify-out" / "graph.html"
    assert html.exists()
    assert html.stat().st_size > 0


def test_export_html_no_viz_removes_file(tmp_path):
    out = _make_graph(tmp_path)
    (out / "graph.html").write_text("<html/>")
    r = _run(["export", "html", "--no-viz"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (out / "graph.html").exists()


def test_export_html_error_without_graph(tmp_path):
    r = _run(["export", "html"], tmp_path)
    assert r.returncode != 0


# ── graphify export obsidian ─────────────────────────────────────────────────

def test_export_obsidian_creates_vault(tmp_path):
    _make_graph(tmp_path)
    r = _run(["export", "obsidian"], tmp_path)
    assert r.returncode == 0, r.stderr
    vault = tmp_path / "graphify-out" / "obsidian"
    assert vault.exists()
    md_files = list(vault.glob("*.md"))
    assert len(md_files) > 0


def test_export_obsidian_custom_dir(tmp_path):
    _make_graph(tmp_path)
    custom = tmp_path / "my-vault"
    r = _run(["export", "obsidian", "--dir", str(custom)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert custom.exists()
    assert len(list(custom.glob("*.md"))) > 0


# ── graphify export wiki ─────────────────────────────────────────────────────

def test_export_wiki_creates_articles(tmp_path):
    _make_graph(tmp_path)
    r = _run(["export", "wiki"], tmp_path)
    assert r.returncode == 0, r.stderr
    wiki = tmp_path / "graphify-out" / "wiki"
    assert wiki.exists()
    assert (wiki / "index.md").exists()


def test_export_wiki_accepts_edges_only_graph_json(tmp_path):
    out = _make_graph(tmp_path)
    graph_path = out / "graph.json"
    data = json.loads(graph_path.read_text())
    data["edges"] = data.pop("links")
    graph_path.write_text(json.dumps(data))

    r = _run(["export", "wiki"], tmp_path)

    assert r.returncode == 0, r.stderr
    assert (out / "wiki" / "index.md").exists()


# ── graphify export graphml ──────────────────────────────────────────────────

def test_export_graphml_creates_file(tmp_path):
    _make_graph(tmp_path)
    r = _run(["export", "graphml"], tmp_path)
    assert r.returncode == 0, r.stderr
    gml = tmp_path / "graphify-out" / "graph.graphml"
    assert gml.exists()
    assert gml.stat().st_size > 0
    content = gml.read_text()
    assert "<graphml" in content


# ── graphify export neo4j (cypher) ───────────────────────────────────────────

def test_export_neo4j_creates_cypher(tmp_path):
    _make_graph(tmp_path)
    r = _run(["export", "neo4j"], tmp_path)
    assert r.returncode == 0, r.stderr
    cypher = tmp_path / "graphify-out" / "cypher.txt"
    assert cypher.exists()
    assert cypher.stat().st_size > 0
    content = cypher.read_text()
    assert "MERGE" in content or "CREATE" in content


# ── graphify export falkordb (cypher) ────────────────────────────────────────

def test_export_falkordb_creates_cypher(tmp_path):
    _make_graph(tmp_path)
    r = _run(["export", "falkordb"], tmp_path)
    assert r.returncode == 0, r.stderr
    cypher = tmp_path / "graphify-out" / "cypher.txt"
    assert cypher.exists()
    assert cypher.stat().st_size > 0
    content = cypher.read_text()
    assert "MERGE" in content or "CREATE" in content


# ── graphify query ───────────────────────────────────────────────────────────

def test_query_returns_output(tmp_path):
    _make_graph(tmp_path)
    r = _run(["query", "test"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert len(r.stdout) > 0


def test_query_dfs_flag(tmp_path):
    _make_graph(tmp_path)
    r = _run(["query", "test", "--dfs"], tmp_path)
    assert r.returncode == 0, r.stderr


def test_query_budget_flag(tmp_path):
    _make_graph(tmp_path)
    r = _run(["query", "test", "--budget", "500"], tmp_path)
    assert r.returncode == 0, r.stderr


def test_query_missing_graph_fails(tmp_path):
    r = _run(["query", "anything"], tmp_path)
    assert r.returncode != 0


def test_query_rejects_pending_managed_generation_before_graph_use(tmp_path):
    from graphify.transaction import begin_transaction

    out = _make_graph(tmp_path)
    begin_transaction("runtime", tmp_path, output=out)
    r = _run(["query", "anything"], tmp_path)
    assert r.returncode != 0
    assert "without a graph receipt" in r.stderr.lower()


def test_query_uses_graphify_out_env(tmp_path):
    out = _make_graph(tmp_path)
    custom_out = tmp_path / "custom-graph"
    out.rename(custom_out)
    env = os.environ.copy()
    env["GRAPHIFY_OUT"] = custom_out.name

    r = _run(["query", "test"], tmp_path, env=env)

    assert r.returncode == 0, r.stderr
    assert len(r.stdout) > 0


def test_extract_writes_to_graphify_out_env(tmp_path):
    """#1423: `graphify extract` honours GRAPHIFY_OUT for where it WRITES, not only
    where readers look — previously it hardcoded graphify-out/ and ignored the
    override. Code-only corpus, so no LLM backend is needed."""
    (tmp_path / "m.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
    env = os.environ.copy()
    env["GRAPHIFY_OUT"] = "custom-out"

    r = _run(["extract", "."], tmp_path, env=env)

    assert r.returncode == 0, r.stderr
    assert (tmp_path / "custom-out" / "graph.json").exists(), r.stdout
    assert (tmp_path / "custom-out" / "manifest.json").exists()
    # The default dir must NOT be created when the override is set.
    assert not (tmp_path / "graphify-out").exists(), "extract ignored GRAPHIFY_OUT and wrote graphify-out/"
    # Manifest keys are relative to the scan root (portable) — #1417.
    keys = list(json.loads((tmp_path / "custom-out" / "manifest.json").read_text()).keys())
    assert keys == ["m.py"], keys


# ── graphify path ────────────────────────────────────────────────────────────

def test_path_runs_without_error(tmp_path):
    _make_graph(tmp_path)
    r = _run(["path", "Transformer", "LayerNorm"], tmp_path)
    # May find or not find a path — either is valid, should not crash
    assert r.returncode == 0, r.stderr


def test_path_missing_graph_fails(tmp_path):
    r = _run(["path", "a", "b"], tmp_path)
    assert r.returncode != 0


def test_path_uses_graphify_out_env(tmp_path):
    out = _make_graph(tmp_path)
    custom_out = tmp_path / "custom-graph"
    out.rename(custom_out)
    env = os.environ.copy()
    env["GRAPHIFY_OUT"] = custom_out.name

    r = _run(["path", "Transformer", "LayerNorm"], tmp_path, env=env)

    assert r.returncode == 0, r.stderr


# ── graphify explain ─────────────────────────────────────────────────────────

def test_explain_runs_without_error(tmp_path):
    _make_graph(tmp_path)
    r = _run(["explain", "test"], tmp_path)
    assert r.returncode == 0, r.stderr


def test_explain_missing_graph_fails(tmp_path):
    r = _run(["explain", "anything"], tmp_path)
    assert r.returncode != 0


def test_explain_uses_graphify_out_env(tmp_path):
    out = _make_graph(tmp_path)
    custom_out = tmp_path / "custom-graph"
    out.rename(custom_out)
    env = os.environ.copy()
    env["GRAPHIFY_OUT"] = custom_out.name

    r = _run(["explain", "test"], tmp_path, env=env)

    assert r.returncode == 0, r.stderr


# ── graphify export unknown format ───────────────────────────────────────────

def test_export_unknown_format_fails(tmp_path):
    r = _run(["export", "pdf"], tmp_path)
    assert r.returncode != 0


def test_update_no_cluster_writes_raw_graph(tmp_path):
    src = tmp_path / "sample.py"
    src.write_text("def f():\n    return 1\n", encoding="utf-8")

    r = _run(["update", ".", "--no-cluster"], tmp_path)
    assert r.returncode == 0, r.stderr

    graph_path = tmp_path / "graphify-out" / "graph.json"
    assert graph_path.exists()
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    assert "nodes" in data and "links" in data
    assert all("community" not in node for node in data["nodes"])


# Regression test for #934 - cluster-only crashes when graphify-out/ doesn't exist

def test_cluster_only_creates_output_dir_when_missing(tmp_path):
    """cluster-only must not crash with FileNotFoundError when graphify-out/ is absent (#934)."""
    # Build graph.json somewhere other than the default graphify-out/ location
    # so we can point --graph at it while graphify-out/ doesn't exist yet.
    graph_src = tmp_path / "backup" / "graph.json"
    graph_src.parent.mkdir()

    out_dir = _make_graph(tmp_path)
    graph_json = out_dir / "graph.json"
    # Simulate user archiving the output dir before re-clustering
    import shutil
    shutil.copy(graph_json, graph_src)
    shutil.rmtree(out_dir)

    assert not (tmp_path / "graphify-out").exists()

    r = _run(["cluster-only", ".", "--graph", str(graph_src), "--no-viz"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert (graph_src.parent / "GRAPH_REPORT.md").exists()
    assert (graph_src.parent / ".graphify_generation.json").exists()
    protocol = json.loads(
        (graph_src.parent / ".graphify_protocol.json").read_text(encoding="utf-8")
    )
    assert protocol["state"] == "COMPLETE"
    watermark = json.loads(graph_src.read_text(encoding="utf-8"))["graph"][
        "_graphify_protocol"
    ]
    assert watermark["state"] == "active"
    assert watermark["generation"] == protocol["generation"]
    assert not (tmp_path / "graphify-out").exists()


def test_cluster_only_graph_in_graphify_out_writes_beside_it(tmp_path):
    """#1747 Case 2: `cluster-only --graph <elsewhere>/graphify-out/graph.json`
    must write GRAPH_REPORT.md and the re-clustered graph beside that graph, not
    into a stray graphify-out/ in the CWD."""
    project = tmp_path / "project"
    project.mkdir()
    out_dir = _make_graph(project)  # project/graphify-out/graph.json

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    r = _run(
        ["cluster-only", ".", "--graph", str(out_dir / "graph.json"), "--no-viz", "--no-label"],
        cwd,
    )
    assert r.returncode == 0, r.stderr
    assert (out_dir / "GRAPH_REPORT.md").exists()          # beside --graph
    assert not (cwd / "graphify-out").exists()             # no CWD pollution


def test_extract_out_does_not_pollute_corpus(tmp_path):
    """#1747 Case 1: `extract <corpus> --out <elsewhere>` must not leave a stray
    graphify-out/ (cache, stat-index) inside the scanned corpus."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.py").write_text("def main():\n    return 1\n")
    out = tmp_path / "scratch"

    r = _run(
        ["extract", str(corpus), "--out", str(out), "--no-cluster", "--code-only"],
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert (out / "graphify-out" / "graph.json").exists()   # graph in --out
    receipt = out / "graphify-out" / ".graphify_generation.json"
    assert receipt.exists()
    generation = json.loads(receipt.read_text(encoding="utf-8"))["generation"]
    graph = json.loads(
        (out / "graphify-out" / "graph.json").read_text(encoding="utf-8")
    )
    assert graph["graph"]["_graphify_protocol"]["generation"] == generation
    assert not (corpus / "graphify-out").exists()           # corpus untouched


# Regression test for #1027 - cluster-only must remap labels via node overlap

def test_cluster_only_persists_analysis_sidecar(tmp_path):
    """cluster-only must refresh .graphify_analysis.json alongside graph.json.

    Downstream export commands use the sidecar for community membership and
    should not see stale or missing community analysis after a recluster.
    """
    out = _make_graph(tmp_path)
    analysis_path = out / ".graphify_analysis.json"
    analysis_path.unlink()

    r = _run(["cluster-only", ".", "--no-viz"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert analysis_path.exists()

    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert analysis["communities"]
    assert analysis["cohesion"]
    assert "gods" in analysis
    assert "surprises" in analysis
    assert "questions" in analysis

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    graph_cids = {
        str(node["community"])
        for node in graph.get("nodes", [])
        if node.get("community") is not None
    }
    assert graph_cids == set(analysis["communities"])


def test_cluster_only_remaps_labels_to_previous_cids(tmp_path):
    """cluster-only must invoke remap_communities_to_previous so the existing
    .graphify_labels.json keeps tracking the same conceptual communities after
    re-clustering. Without the remap call, Leiden's size-descending cid order
    re-applies labels by raw index and they silently misalign with cluster
    contents (#1027). Mirror of the watch/update fix from #822.
    """
    out = _make_graph(tmp_path)
    graph_json = out / "graph.json"
    labels_json = out / ".graphify_labels.json"

    # Tag every node with an out-of-band community id and write a labels file
    # keyed on those ids. After cluster-only, at least one of those sentinel
    # ids must survive in the labels file (= remap succeeded by node overlap).
    # If the cluster-only branch skips remap, Leiden returns small ints
    # (0, 1, ...) and the sentinel keys disappear entirely.
    g = json.loads(graph_json.read_text(encoding="utf-8"))
    nodes = g.get("nodes", [])
    assert len(nodes) >= 4, "fixture must have enough nodes to form 2+ communities"
    sentinel_a, sentinel_b = 4242, 9999
    half = len(nodes) // 2
    for i, n in enumerate(nodes):
        n["community"] = sentinel_a if i < half else sentinel_b
    graph_json.write_text(json.dumps(g), encoding="utf-8")
    labels_json.write_text(
        json.dumps({str(sentinel_a): "First Group", str(sentinel_b): "Second Group"}),
        encoding="utf-8",
    )

    r = _run(["cluster-only", ".", "--no-viz"], tmp_path)
    assert r.returncode == 0, r.stderr

    # Real signal: labels.json keys must align with the community ids actually
    # written to graph.json's per-node community attribute. Without remap,
    # Leiden returns small cids (0, 1, ...) but labels.json still carries the
    # old sentinel keys, so the intersection is empty and labels are orphaned.
    final_graph = json.loads(graph_json.read_text(encoding="utf-8"))
    final_labels = json.loads(labels_json.read_text(encoding="utf-8"))
    actual_cids = {n.get("community") for n in final_graph.get("nodes", [])}
    label_cids = {int(k) for k in final_labels.keys()}
    overlap = actual_cids & label_cids
    assert overlap, (
        f"After cluster-only with prior labels keyed on cids {label_cids}, at "
        f"least one of those cids must still appear in graph.json's community "
        f"attribute ({actual_cids}). Without remap_communities_to_previous "
        f"(#1027) Leiden renumbers communities to 0,1,... and the prior labels "
        f"become orphaned. Final labels: {final_labels}"
    )


# ── communities-fallback when .graphify_analysis.json is absent ──────────────
# The watch / post-commit rebuild path only writes graph.json + GRAPH_REPORT.md;
# it does NOT regenerate .graphify_analysis.json. The full `graphify extract`
# pipeline also removes its temp files at the end of the run on some skill
# workflows. In both cases the per-node `community` attribute is intact on
# every node in graph.json — that's the source of truth `to_json` writes.
# Without these tests, `graphify export html|obsidian|wiki|svg|graphml|neo4j`
# silently bails or generates a degraded artifact whenever the sidecar is
# missing, even though the data is right there.

def test_export_html_falls_back_to_node_community_attribute(tmp_path):
    """When .graphify_analysis.json is absent, export html should reconstruct
    communities from the per-node attribute in graph.json rather than bailing
    out with 'Single community - aggregated view not useful.'.
    """
    out = _make_graph(tmp_path)
    # Simulate the watch-rebuild / cleanup case: graph.json + labels survive,
    # analysis sidecar is gone.
    (out / ".graphify_analysis.json").unlink()

    r = _run(["export", "html"], tmp_path)
    assert r.returncode == 0, r.stderr
    html = out / "graph.html"
    assert html.exists(), "graph.html should be generated from the fallback"
    assert html.stat().st_size > 0
    # The success message comes from to_html — confirm we're not hitting the
    # "Single community" bail-out path.
    assert "Single community" not in r.stdout
    assert "Single community" not in r.stderr


def test_export_html_fallback_recovers_multiple_communities(tmp_path):
    """Stronger assertion: the reconstructed `communities` dict should have the
    SAME community count as the analysis sidecar would, so downstream code
    (aggregation thresholds, member counts) sees identical input.
    """
    out = _make_graph(tmp_path)

    # Read the canonical community count from the analysis sidecar
    analysis = json.loads((out / ".graphify_analysis.json").read_text(encoding="utf-8"))
    expected_count = len(analysis["communities"])

    # And the count we'd reconstruct from graph.json's node attributes
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    reconstructed_cids = {
        n["community"] for n in graph.get("nodes", [])
        if n.get("community") is not None
    }
    assert len(reconstructed_cids) == expected_count, (
        f"reconstruction would lose communities: sidecar={expected_count} vs "
        f"graph.json={len(reconstructed_cids)}"
    )

    # Now remove the sidecar and confirm the CLI still succeeds end-to-end.
    (out / ".graphify_analysis.json").unlink()
    r = _run(["export", "html"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert (out / "graph.html").exists()


def test_export_html_no_community_data_at_all_still_succeeds(tmp_path):
    """If a graph.json was somehow written without any per-node `community`
    attribute (older versions of to_json, hand-built graphs), the fallback
    should produce an empty communities dict and the renderer should still
    not crash. Whether the aggregated view is useful is a separate question.
    """
    out = _make_graph(tmp_path)
    (out / ".graphify_analysis.json").unlink()

    # Strip the community attribute from every node
    graph_path = out / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    for n in graph.get("nodes", []):
        n.pop("community", None)
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

    r = _run(["export", "html"], tmp_path)
    # Should NOT crash. It may print a warning and skip rendering, but exit
    # code stays clean — same behaviour as the pre-fallback empty-communities
    # path, just no longer silently failing on the common case.
    assert r.returncode == 0, r.stderr


def test_graph_json_node_ids_are_portable_across_checkout_paths(tmp_path):
    """#1789: the committed graph.json's node ids must be relative to the scan
    root — not embed the absolute path — so the same repo yields identical ids
    on any machine/checkout and leaks no local username/home."""
    def _build(root: Path):
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "mod.py").write_text("def f(): return 1\n")
        (root / "pkg" / "app.py").write_text("from pkg.mod import f\ndef g(): return f()\n")
        r = _run(["extract", ".", "--code-only", "--no-cluster"], root)
        assert r.returncode == 0, r.stderr
        data = json.loads((root / "graphify-out" / "graph.json").read_text())
        return sorted(n["id"] for n in data["nodes"])

    a = _build(tmp_path / "alice_home" / "proj")
    b = _build(tmp_path / "bob_elsewhere" / "checkout" / "proj")
    assert a == b, f"node ids differ across checkout paths: {a} vs {b}"
    leak = {"alice_home", "bob_elsewhere", "checkout", "tmp", "private", "users", "home", "var"}
    assert not any(part in leak for ident in a for part in ident.split("_")), \
        f"node id embeds an absolute-path component: {a}"
