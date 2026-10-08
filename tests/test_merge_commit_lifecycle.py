"""Real-Git evidence for merge commit inputs and tracked graph generations."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

import pytest

from graphify import hooks, transaction


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX hook lifecycle proof")


@pytest.fixture(autouse=True)
def isolated_authority(monkeypatch):
    parent = os.environ.get("GRAPHIFY_TEST_AUTHORITY_PARENT") or Path.home()
    with tempfile.TemporaryDirectory(prefix=".graphify-merge-lifecycle-", dir=parent) as home:
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("XDG_STATE_HOME", str(Path(home) / "state"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
        monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
        for name in ("GRAPHIFY_OUT", "GRAPHIFY_OUTPUT_ROOT", "GRAPHIFY_SKIP_HOOK",
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            monkeypatch.delenv(name, raising=False)
        yield


def git(repo, *args, skip_hooks=True, check=True):
    env = dict(os.environ)
    if skip_hooks:
        env["GRAPHIFY_SKIP_HOOK"] = "1"
    result = subprocess.run(["git", "-C", str(repo), *args], env=env,
                            capture_output=True, text=True, check=False)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def init_repo(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "merge-lifecycle@example.invalid")
    git(path, "config", "user.name", "Merge Lifecycle Tests")
    git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "manual"])
def test_git_merge_hook_index_and_recorded_tree(tmp_path, manual):
    """Qualify the actual Git binary, independently of Graphify's hooks."""
    repo = init_repo(tmp_path / "repo")
    artifact = repo / "artifact.txt"
    artifact.write_text("candidate\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("side\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "side")
    git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "main")
    if manual:
        git(repo, "merge", "--no-commit", "side")
    hook = repo / ".git/hooks" / ("pre-commit" if manual else "pre-merge-commit")
    hook.write_text(
        "#!/bin/sh\nset -eu\n"
        "git show :artifact.txt > .git/hook-entry-artifact\n"
        "git write-tree > .git/hook-entry-tree\n"
        "printf 'hook-staged\\n' > artifact.txt\n"
        "git add -- artifact.txt\n"
        "git write-tree > .git/hook-exit-tree\n"
    )
    hook.chmod(0o755)
    if manual:
        git(repo, "commit", "--no-edit", skip_hooks=False)
    else:
        git(repo, "merge", "--no-edit", "side", skip_hooks=False)
    entry_tree = (repo / ".git/hook-entry-tree").read_text().strip()
    exit_tree = (repo / ".git/hook-exit-tree").read_text().strip()
    committed_tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    assert (repo / ".git/hook-entry-artifact").read_text() == "candidate\n"
    assert entry_tree != exit_tree
    assert git(repo, "show", ":artifact.txt").stdout == "hook-staged\n"
    assert committed_tree == (exit_tree if manual else entry_tree)
    assert git(repo, "show", "HEAD:artifact.txt").stdout == (
        "hook-staged\n" if manual else "candidate\n"
    )
    print(json.dumps({"git": git(repo, "--version").stdout.strip(),
                      "manual": manual, "entry_tree": entry_tree,
                      "exit_tree": exit_tree, "committed_tree": committed_tree}))


def rebuild(repo):
    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-c",
         "from pathlib import Path; from graphify.watch import _rebuild_code; "
         "raise SystemExit(0 if _rebuild_code(Path('.'), no_cluster=True, "
         "block_on_lock=True) else 1)"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def graph_repo(tmp_path, manual):
    repo = init_repo(tmp_path / "repo")
    (repo / "base.py").write_text("def base():\n    return 1\n")
    (repo / "conflict.txt").write_text("base\n")
    rebuild(repo)
    hooks.install(repo)
    graph_path = "graphify-out/graph.json"
    git(repo, "add", ".gitattributes", "base.py", "conflict.txt", graph_path)
    git(repo, "commit", "-m", "base")
    output = repo / "graphify-out"
    base_output = {p.relative_to(output): p.read_bytes()
                   for p in output.rglob("*") if p.is_file()}
    git(repo, "checkout", "-b", "side")
    (repo / "side.py").write_text("def side():\n    return 2\n")
    if manual:
        (repo / "conflict.txt").write_text("side\n")
    rebuild(repo)
    git(repo, "add", "side.py", "conflict.txt", graph_path)
    git(repo, "commit", "-m", "side")
    git(repo, "checkout", "main")
    # Restore the matching branch-local generation, preserving directory identity.
    for path in output.rglob("*"):
        if path.is_file():
            path.unlink()
    for relative, data in base_output.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (repo / "main.py").write_text("def main():\n    return 3\n")
    if manual:
        (repo / "conflict.txt").write_text("main\n")
    rebuild(repo)
    git(repo, "add", "main.py", "conflict.txt", graph_path)
    git(repo, "commit", "-m", "main")
    return repo


def wait_for_recovery(repo):
    graph = repo / "graphify-out/graph.json"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            snapshot = transaction.open_graph_snapshot(graph, purpose="merge-commit-proof")
            state = transaction.transaction_status(graph.parent)
        except transaction.PendingTransactionError:
            time.sleep(0.1)
            continue
        if ({"base_base", "main_main", "side_side"} <=
                {node["id"] for node in snapshot.data["nodes"]}
                and all(state[name] is None for name in
                        ("transaction", "prepared_creation", "token_transition"))):
            return snapshot
        time.sleep(0.1)
    log = Path.home() / ".cache/graphify-rebuild.log"
    pytest.fail(log.read_text() if log.exists() else "recovery timed out")


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "conflicted-manual"])
def test_baseline_merge_records_pending_graph_and_clone_refuses(tmp_path, manual):
    """Baseline defect proof: local recovery cannot repair the recorded commit."""
    repo = graph_repo(tmp_path, manual)
    merged = git(repo, "merge", "--no-edit", "side", skip_hooks=False, check=False)
    if manual:
        assert merged.returncode != 0
        assert "CONFLICT" in merged.stdout + merged.stderr
        (repo / "conflict.txt").write_text("resolved\n")
        git(repo, "add", "conflict.txt")
        merged = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    committed = json.loads(git(repo, "show", "HEAD:graphify-out/graph.json").stdout)
    assert committed["graph"][transaction.GRAPH_WATERMARK_KEY]["state"] == "merge_pending"
    recovered = wait_for_recovery(repo)
    assert recovered.data["graph"][transaction.GRAPH_WATERMARK_KEY]["state"] == "active"
    assert git(repo, "diff", "--name-only").stdout.splitlines() == ["graphify-out/graph.json"]
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--no-local", str(repo), str(clone))
    before = git(clone, "status", "--porcelain=v1").stdout
    with pytest.raises(transaction.PendingTransactionError) as refusal:
        transaction.open_graph_snapshot(clone / "graphify-out/graph.json", purpose="fresh-clone-proof")
    assert git(clone, "status", "--porcelain=v1").stdout == before == ""
    print(json.dumps({"manual": manual, "committed_state": "merge_pending",
                      "working_state": "active", "clone_refusal": str(refusal.value),
                      "commit": git(repo, "rev-parse", "HEAD").stdout.strip()}))


def instrument_guard(repo, event):
    """Observe only the guard's effects, separately from Git's merge staging."""
    hook = repo / ".git/hooks" / event
    original = repo / ".git" / (event + "-under-test")
    original.write_bytes(hook.read_bytes())
    original.chmod(0o755)
    hook.write_text(
        "#!/bin/sh\n"
        'index=${GIT_INDEX_FILE:-$(git rev-parse --git-path index)}\n'
        'cp "$index" .git/guard-entry-index\n'
        "cp graphify-out/graph.json .git/guard-entry-graph\n"
        + shlex.quote(str(original)) + '\nresult=$?\n'
        'cp "$index" .git/guard-exit-index\n'
        "cp graphify-out/graph.json .git/guard-exit-graph\n"
        'exit "$result"\n'
    )
    return repo / ".git"


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "conflicted-manual"])
def test_opt_in_guard_veto_preserves_candidate_index_and_output(tmp_path, manual):
    repo = graph_repo(tmp_path, manual)
    hooks.install(repo, merge_guard=True)
    before_head = git(repo, "rev-parse", "HEAD").stdout
    event = "pre-commit" if manual else "pre-merge-commit"
    evidence = instrument_guard(repo, event)
    result = git(repo, "merge", "--no-edit", "side", skip_hooks=False, check=False)
    if manual:
        assert result.returncode != 0
        assert "CONFLICT" in result.stdout + result.stderr
        (repo / "conflict.txt").write_text("resolved\n")
        git(repo, "add", "conflict.txt")
        result = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
    assert result.returncode != 0
    assert "merge_pending" in result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == before_head
    assert (evidence / "guard-entry-index").read_bytes() == (evidence / "guard-exit-index").read_bytes()
    assert (evidence / "guard-entry-graph").read_bytes() == (evidence / "guard-exit-graph").read_bytes()
    assert not (Path.home() / ".cache/graphify-rebuild.log").exists()
    # Exact retries remain refusals; they do not rebuild or record another commit.
    retried = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
    assert retried.returncode != 0
    assert "merge_pending" in retried.stdout + retried.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == before_head


@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "manual"])
@pytest.mark.parametrize("bypass", ["no-verify", "environment"])
def test_opt_in_guard_explicit_bypass_still_records_pending(tmp_path, monkeypatch, manual, bypass):
    repo = graph_repo(tmp_path, manual)
    hooks.install(repo, merge_guard=True)
    if bypass == "environment":
        monkeypatch.setenv("GRAPHIFY_SKIP_HOOK", "1")
    options = ["--no-verify"] if bypass == "no-verify" else []
    result = git(repo, "merge", "--no-edit", *options, "side", skip_hooks=False, check=False)
    if manual:
        assert result.returncode != 0
        (repo / "conflict.txt").write_text("resolved\n")
        git(repo, "add", "conflict.txt")
        result = git(repo, "commit", "--no-edit", *options, skip_hooks=False, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    committed = json.loads(git(repo, "show", "HEAD:graphify-out/graph.json").stdout)
    assert committed["graph"][transaction.GRAPH_WATERMARK_KEY]["state"] == "merge_pending"
    if bypass == "no-verify":
        wait_for_recovery(repo)
    else:
        assert not (Path.home() / ".cache/graphify-rebuild.log").exists()
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--no-local", str(repo), str(clone))
    with pytest.raises(transaction.PendingTransactionError):
        transaction.open_graph_snapshot(clone / "graphify-out/graph.json", purpose="bypassed-clone-proof")
