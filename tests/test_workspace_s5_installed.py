"""S5 public transports from a disposable, genuinely installed candidate.

Native lifecycle proof uses macOS/APFS, never injected host capabilities. The
console uses PYTHONDONTWRITEBYTECODE=1; module and audit launches use -B.
CPython can cache the package initializer before package code can suppress it.
"""
import hashlib
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

import pytest

from graphify.workspace.composition import StructuralPolicy
from graphify.workspace.contracts import canonical_json_bytes
from graphify.workspace.lifecycle_contracts import decode_journal_frame
from tests.test_workspace_packed_refs import packed_fixture, change_checkpoint
from tests.workspace_s3_helpers import create_repo, git_output, tree_snapshot, REPO_UUID
from tools.workspace_artifacts.candidate import build_fixture


_AUDITED_CONSOLE = r'''
import sys, os, stat, runpy

def audit(event, args):
    mutation = event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod',
                        'os.link', 'os.symlink', 'os.truncate', 'os.utime'}
    if event == 'open':
        path, _, flags = args
        mutation = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if type(path) is int and stat.S_ISFIFO(os.fstat(path).st_mode):
            mutation = False
        if isinstance(path, (str, bytes)) and 'jieba.cache' in os.fsdecode(path):
            raise AssertionError('ambient tokenizer cache access')
    if mutation or event in {'socket.connect', 'socket.bind'}:
        raise AssertionError('unexpected durable side effect: ' + event)
    if event == 'subprocess.Popen':
        command = args[1]
        if not (command[0] in {'df', 'diskutil'} or os.path.basename(command[0]) == 'git'
                or command[:4] == [sys.executable, '-I', '-S', '-B']
                or command[:3] == [sys.executable, '-I', '-B']):
            raise AssertionError('unexpected subprocess: ' + str(command))
sys.addaudithook(audit)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


@pytest.fixture(scope="module")
def installed_candidate(tmp_path_factory):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("installed candidate proof requires the repository uv toolchain")
    root = tmp_path_factory.mktemp("s5-installed").resolve()
    repo_root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if not key.endswith("API_KEY") and key not in {
               "GOOGLE_APPLICATION_CREDENTIALS", "PYTHONPATH", "VIRTUAL_ENV",
               "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}}
    for name in ("HOME", "CODEX_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        path = root / name.lower()
        path.mkdir(mode=0o700)
        env[name] = str(path)
    env["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def setup(args):
        result = subprocess.run(args, env=env, cwd=root, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    wheels = root / "wheels"
    setup([uv, "build", "--wheel", "--offline", "--out-dir", str(wheels), str(repo_root)])
    wheel, = wheels.glob("*.whl")
    bundle = root / "bundle"
    build_fixture(repo_root=repo_root, wheel=wheel, output_root=bundle,
                  policy=StructuralPolicy(8, 16384, 1, 8, 1024 * 1024))
    venv = root / "venv"
    setup([uv, "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    setup([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)])
    purelib = Path(setup([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]))
    (purelib / "s5-fixture-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    console = python.parent / ("graphify.exe" if os.name == "nt" else "graphify")
    expected = json.loads((bundle / "compatibility.json").read_bytes())
    return root, env, python, console, bundle, expected


def authorization(action):
    return {"action": action, "operator_id": "operator:s5-installed",
            "reason": "disposable public entry-point proof", "issued_at": "2026-10-03T00:00:00Z",
            "nonce": "s5-" + action.lower()}


class PublicClient:
    def __init__(self, installed, state):
        self.root, self.env, self.python, self.console, self.bundle, self.expected = installed
        self.state = state

    def request(self, command, parameters):
        return {"contract": "graphify.workspace.cli.request", "format_version": 1,
                "command": command, "request_id": "installed-s5", "state_root": str(self.state),
                "compatibility_manifest": self.expected, "timeout_ms": 120000,
                "parameters": parameters}

    def run(self, command, parameters, *, module=False, audit=False, request=None, file=False,
            exit_code=0, cwd=None):
        value = self.request(command, parameters) if request is None else request
        raw = canonical_json_bytes(value)
        if audit:
            launch = [str(self.python), "-E", "-P", "-B", "-c", _AUDITED_CONSOLE, str(self.console)]
        elif module:
            launch = [str(self.python), "-E", "-P", "-B", "-m", "graphify"]
        else:
            launch = [str(self.console)]
        request_path = self.root / "public-request.json"
        if file:
            request_path.write_bytes(raw)
        result = subprocess.run([*launch, "workspace", command, "--request",
                                 str(request_path) if file else "-"],
                                input=None if file else raw, env=self.env, cwd=self.root if cwd is None else cwd,
                                capture_output=True, timeout=180)
        assert result.returncode == exit_code, (result.stdout, result.stderr)
        output = result.stdout if exit_code == 0 else result.stderr
        assert (result.stderr if exit_code == 0 else result.stdout) == b""
        response = json.loads(output)
        assert output == canonical_json_bytes(response)
        assert set(response) == {"contract", "format_version", "command", "request_id",
                                 "outcome", "result", "error_code"}
        assert response["contract"] == "graphify.workspace.cli.response"
        assert response["format_version"] == 1
        assert response["outcome"] == ("ok" if exit_code == 0 else "refused")
        if exit_code == 0:
            assert response["command"] == command and response["request_id"] == "installed-s5"
            assert response["error_code"] is None and isinstance(response["result"], dict)
        else:
            assert response["result"] is None and isinstance(response["error_code"], str)
        return response["result"] if exit_code == 0 else response

    def snapshots(self, *sources):
        paths = (self.state, self.python.parent.parent, *sources,
                 *(Path(self.env[name]) for name in ("HOME", "CODEX_HOME", "XDG_STATE_HOME",
                                                    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR")))
        return tuple(tree_snapshot(path) for path in paths)


def test_installed_public_native_round_trip_and_cold_reads(installed_candidate):
    if sys.platform != "darwin":
        pytest.skip("native S5 public lifecycle requires macOS/APFS; no injected fallback")
    root, _, _, _, bundle, _ = installed_candidate
    source = create_repo(root / "source-a")
    (source / "main.py").write_text(
        "def leaf(): return 42\ndef caller(): return leaf()\n"
        "def 南京市长江大桥(): return caller()\n", encoding="utf-8")
    git_output(source, "add", "main.py")
    git_output(source, "commit", "--quiet", "--no-gpg-sign", "-m", "structural fixture")
    _, commit, checkpoint_tree = packed_fixture(source)
    linked = root / "source-b"
    git_output(source, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    clone = root / "clone"
    git_output(root, "clone", "--quiet", str(source), str(clone))
    state = root / "state"
    state.mkdir(mode=0o700)
    authority = state / "runtime-manifest.json"
    authority.write_bytes((bundle / "runtime-manifest.json").read_bytes())
    authority.chmod(0o600)
    client = PublicClient(installed_candidate, state)
    source_before = (tree_snapshot(source), tree_snapshot(linked), tree_snapshot(clone))
    before = client.snapshots(source, linked, clone)
    wrong_candidate = client.request("status", {"repo_uuid": REPO_UUID})
    wrong_candidate["compatibility_manifest"] = dict(client.expected, wheel_sha256="f" * 64)
    client.run("status", {}, request=wrong_candidate, audit=True, exit_code=3)
    old_tuple = client.request("status", {"repo_uuid": REPO_UUID})
    old_tuple["compatibility_manifest"] = dict(client.expected, adapter_contract_version=2)
    client.run("status", {}, request=old_tuple, audit=True, exit_code=2)
    assert client.snapshots(source, linked, clone) == before
    enrolled = client.run("register", {"operation": "enroll", "source_root": str(source),
        "expected_registry_revision": 0, "authorization": authorization("ENROLL")}, file=True)
    assert enrolled["registry_revision"] == 1
    adopted = client.run("register", {"operation": "adopt", "source_root": str(linked),
        "expected_registry_revision": 1, "authorization": authorization("ADOPT")}, module=True)
    assert adopted["registry_revision"] == 2
    active = client.run("activate", {"source_root": str(linked), "authorization": authorization("ACTIVATE"),
        "expected_registry_revision": 2, "expected_active_source_revision": 1,
        "expected_operation_epoch": 1, "expected_migration_epoch": 0, "ttl_ns": 300_000_000_000})
    assert active["active_source_revision"] == 2
    before = client.snapshots(source, linked, clone)
    client.run("activate", {"source_root": str(linked), "authorization": authorization("ACTIVATE"),
        "expected_registry_revision": active["registry_revision"], "expected_active_source_revision": 2,
        "expected_operation_epoch": active["operation_epoch"], "expected_migration_epoch": 0,
        "ttl_ns": 300_000_000_000}, exit_code=4)
    client.run("register", {"operation": "enroll", "source_root": str(clone),
        "expected_registry_revision": active["registry_revision"], "authorization": authorization("ENROLL")}, exit_code=4)
    assert client.snapshots(source, linked, clone) == before
    revision = active["registry_revision"]
    for operation in ("rebind", "rotate"):
        parameters = {"operation": operation, "source_root": str(linked),
                      "expected_registry_revision": revision - 1,
                      "authorization": authorization(operation.upper())}
        before = client.snapshots(source, linked, clone)
        client.run("register", parameters, exit_code=4)
        assert client.snapshots(source, linked, clone) == before
        parameters["expected_registry_revision"] = revision
        result = client.run("register", parameters, module=operation == "rotate")
        revision += 1
        assert result == {"registry_revision": revision, "active_source_revision": 2,
                          "repo_uuid": REPO_UUID}
        assert (tree_snapshot(source), tree_snapshot(linked), tree_snapshot(clone)) == source_before
    before = client.snapshots(source, linked, clone)
    prepared = client.run("sync", {"operation": "prepare", "repo_uuid": REPO_UUID,
        "generation_id": "gen-public-installed", "source_epoch": 1,
        "desired_watermark": 1, "expected_payload_bytes": 1024 * 1024})
    assert client.snapshots(source, linked, clone) == before
    synced = client.run("sync", {"operation": "execute", "sync_request": prepared["sync_request"],
        "attempt_sha256": "a" * 64}, module=True)
    assert synced["pointer_revision"] == 1 and len(synced["receipt_sha256"]) == 64
    workspace = state / "workspaces" / REPO_UUID
    receipt_bytes = (workspace / "generations/gen-public-installed/receipt.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    assert hashlib.sha256(receipt_bytes).hexdigest() == synced["receipt_sha256"]
    assert receipt["active_source_revision"] == 2
    assert receipt["semantic_completeness"] == "not_required"
    pointer = json.loads((workspace / "pointers.json").read_bytes())
    assert pointer["pointer_revision"] == synced["pointer_revision"]
    assert pointer["current"] == {"generation_id": "gen-public-installed",
                                 "receipt_sha256": synced["receipt_sha256"]}
    events = [decode_journal_frame(path.read_bytes()).to_dict()
              for path in sorted((workspace / "journal/segments").glob("*.gwf"))]
    assert [event["transition"] for event in events][-2:] == ["CERTIFIED", "PROMOTED"]
    assert all(event["receipt_sha256"] == synced["receipt_sha256"] for event in events[-2:])
    assert (tree_snapshot(source), tree_snapshot(linked), tree_snapshot(clone)) == source_before
    query = {"repo_uuid": REPO_UUID, "question": "caller", "mode": "bfs", "depth": 2,
             "token_budget": 4096, "context_filters": []}
    before = client.snapshots(source, linked, clone)
    assert client.run("sync", {"operation": "execute", "sync_request": prepared["sync_request"],
        "attempt_sha256": "b" * 64}) == synced
    assert client.snapshots(source, linked, clone) == before
    first = client.run("query", query)["text"]
    assert "leaf" in first
    assert client.run("query", query, module=True)["text"] == first
    hostile = root / "hostile-cwd"
    hostile.mkdir()
    forged = hostile / "graphify"
    forged.mkdir()
    (forged / "__init__.py").write_text("raise SystemExit('FORGED-PACKAGE')\n")
    assert client.run("query", query, module=True, cwd=hostile)["text"] == first
    assert client.run("query", query, audit=True, cwd=hostile)["text"] == first
    assert client.run("query", query, audit=True)["text"] == first
    chinese = dict(query, question="南京市长江大桥")
    cold = client.run("query", chinese, audit=True)["text"]
    assert "南京市长江大桥" in cold
    for command in ("status", "doctor"):
        client.run(command, {"repo_uuid": REPO_UUID}, audit=True)
    assert client.snapshots(source, linked, clone) == before
    cache = Path(client.env["TMPDIR"]) / "jieba.cache"
    cache.write_bytes(marshal.dumps(({"南京市长江大桥": 999}, 999)))
    before = client.snapshots(source, linked, clone)
    assert client.run("query", chinese, audit=True)["text"] == cold
    assert client.snapshots(source, linked, clone) == before
    change_checkpoint(source, checkpoint_tree)
    before = client.snapshots(source, linked, clone)
    assert client.run("query", query, audit=True)["text"] == first
    assert client.snapshots(source, linked, clone) == before
    git_dir = Path(git_output(linked, "rev-parse", "--absolute-git-dir"))
    (git_dir / "HEAD").write_text("ref: refs/heads/selected-alias\n")
    (source / ".git/refs/heads/selected-alias").write_text(commit + "\n")
    before = client.snapshots(source, linked, clone)
    client.run("query", query, audit=True, exit_code=4)
    assert client.snapshots(source, linked, clone) == before
    # A missing retained lock is a diagnostic failure, never repair authority.
    generation_lock = workspace / "locks/generations/gen-public-installed.lock"
    assert generation_lock.is_file()
    generation_lock.unlink()
    before = client.snapshots(source, linked, clone)
    for command in ("status", "doctor"):
        result = client.run(command, {"repo_uuid": REPO_UUID}, audit=True)
        assert result == {"state": "unavailable", "safe_to_query": False,
                          "reason_code": "diagnostic_unavailable"}
        assert client.snapshots(source, linked, clone) == before
    assert not generation_lock.exists()


@pytest.mark.parametrize("timeout_ms", [0, True, 300001])
def test_installed_invalid_request_precedes_state_access(installed_candidate, timeout_ms):
    root, *_ = installed_candidate
    client = PublicClient(installed_candidate, root / "must-not-exist")
    request = client.request("query", {"repo_uuid": REPO_UUID, "question": "secret-question",
        "mode": "bfs", "depth": 2, "token_budget": 4096, "context_filters": []})
    request["timeout_ms"] = timeout_ms
    before = client.snapshots()
    response = client.run("query", {}, request=request, audit=True, exit_code=2)
    assert "secret-question" not in json.dumps(response)
    assert not client.state.exists()
    assert client.snapshots() == before
