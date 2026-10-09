"""Automatic guards retain the candidate seen before runtime discovery."""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from graphify import hooks
from tests.test_merge_commit_lifecycle import git, init_repo, isolated_authority  # noqa: F401


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX merge hook qualification")
GRAPH = "graphify-out/graph.json"


@pytest.mark.parametrize("replacement", ["active", "absent"])
def test_automatic_guard_refuses_captured_pending_after_external_index_change(tmp_path, replacement):
    repo = init_repo(tmp_path / "repo")
    graph = repo / GRAPH
    graph.parent.mkdir()
    pending = {"nodes": [], "links": [], "graph": {"_graphify_protocol": {
        "schema": 1, "protocol_epoch": 1, "generation": 1, "state": "merge_pending",
    }}}
    graph.write_text(json.dumps(pending))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("side")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "side")
    git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "main")
    old_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    real_git = shutil.which("git")
    active = json.loads(json.dumps(pending))
    active["graph"]["_graphify_protocol"]["state"] = "active"
    active_oid = subprocess.run(
        [real_git, "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=json.dumps(active), text=True, capture_output=True, check=True,
    ).stdout.strip()
    hook = repo / ".git/hooks/pre-merge-commit"
    hook.write_text("#!/bin/sh\n" + hooks._merge_guard_script(
        "pre-merge-commit", "graphify-out", sys.executable,
    ))
    hook.chmod(0o755)

    # Preserve the real Git result, but pause before returning the first shell
    # inventory observation. The mutating writer is this test process, not the
    # guard or its installed script. Python's later inspection uses -z.
    shim_dir = tmp_path / "git-shim"
    shim_dir.mkdir()
    observed = tmp_path / "observed"
    resume = tmp_path / "resume"
    shim = shim_dir / "git"
    shim.write_text("#!" + sys.executable + "\n" + f"""import os, subprocess, sys, time
args = sys.argv[1:]
if 'diff-index' in args and '-z' not in args:
    result = subprocess.run([{real_git!r}, *args], capture_output=True)
    with open({str(observed)!r}, 'wb') as stream:
        stream.write(result.stdout)
    deadline = time.monotonic() + 15
    while not os.path.exists({str(resume)!r}):
        if time.monotonic() > deadline:
            sys.exit(99)
        time.sleep(.01)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
os.execv({real_git!r}, [{real_git!r}, *args])
""")
    shim.chmod(0o755)
    env = {**os.environ, "GIT_EXEC_PATH": str(shim_dir),
           "PATH": str(shim_dir) + os.pathsep + os.environ["PATH"]}
    env.pop("GRAPHIFY_SKIP_HOOK", None)
    process = subprocess.Popen(
        [real_git, "-C", str(repo), "merge", "--no-edit", "side"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        deadline = time.monotonic() + 15
        while not observed.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert observed.exists(), "merge did not reach the shell candidate observation"
        assert GRAPH in observed.read_text()
        assert json.loads(git(repo, "show", ":" + GRAPH).stdout) == pending
        if replacement == "active":
            git(repo, "update-index", "--cacheinfo", "100644," + active_oid + "," + GRAPH)
        else:
            git(repo, "update-index", "--force-remove", GRAPH)
        external_index = (repo / ".git/index").read_bytes()
        worktree_graph = graph.read_bytes()
        resume.write_text("continue")
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    committed = json.loads(git(repo, "show", "HEAD:" + GRAPH).stdout)
    assert process.returncode != 0, (
        f"guard allowed automatic merge of {committed['graph']['_graphify_protocol']['state']} "
        f"after the index became {replacement}: {stdout} {stderr}"
    )
    assert "merge_pending" in stderr
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == old_head
    assert (repo / ".git/MERGE_HEAD").exists()
    assert (repo / ".git/index").read_bytes() == external_index
    assert graph.read_bytes() == worktree_graph
    if replacement == "active":
        assert json.loads(git(repo, "show", ":" + GRAPH).stdout) == active
    else:
        assert git(repo, "show", ":" + GRAPH, check=False).returncode != 0
