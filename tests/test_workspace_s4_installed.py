"""S4 installed-wheel library launch, including cold queries with no writes.

This is a disposable candidate proof, not S5 CLI or S7 release certification.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

import pytest

from graphify.workspace.composition import StructuralPolicy
from tests.workspace_s3_helpers import create_repo, tree_snapshot, REPO_UUID
from tools.workspace_artifacts.candidate import build_fixture


_LAUNCH = r'''
import sys, os, json, time, stat
from pathlib import Path
mode, state, bundle, source, repo_uuid = sys.argv[1:]
if mode == 'query':
    query_child_code = 'from graphify.workspace.adapters.v8 import _query_child; _query_child()'
    def audit(event, args):
        mutation = event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod',
                            'os.link', 'os.symlink', 'os.truncate', 'os.utime'}
        if event == 'open':
            path, _, flags = args
            mutation = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            # Popen wraps anonymous IPC pipe descriptors; these are not durable writes.
            if type(path) is int and stat.S_ISFIFO(os.fstat(path).st_mode):
                mutation = False
            if isinstance(path, (str, bytes)) and 'jieba.cache' in os.fsdecode(path):
                raise AssertionError('ambient tokenizer cache access')
        if mutation or event in {'socket.connect', 'socket.bind'}:
            raise AssertionError('unexpected durable side effect: ' + event)
        if event == 'subprocess.Popen':
            command = args[1]
            allowed = (command[0] in {'git', 'df', 'diskutil'}
                       or command[:4] == [sys.executable, '-I', '-S', '-B']
                       or (len(command) == 6 and command[:5] == [sys.executable, '-I', '-B', '-c', query_child_code]))
            if not allowed:
                raise AssertionError('unexpected subprocess: ' + str(command))
    sys.addaudithook(audit)
assert sys.dont_write_bytecode
from graphify.workspace.contracts import CompatibilityManifest
from graphify.workspace.composition import load_workspace_runtime_inputs, compose_workspace_runtime
from graphify.workspace.persistence import RuntimeCapabilities
# Portable runs use explicit injected filesystem capabilities. Darwin exercises
# native APFS admission; it must not silently fall back to injected support.
if sys.platform != 'darwin':
    RuntimeCapabilities.detect = classmethod(lambda cls, path: cls.supported_test_fixture())
expected = CompatibilityManifest.from_json((Path(bundle) / 'compatibility.json').read_bytes())
inputs = load_workspace_runtime_inputs(state_root=Path(state), expected=expected)
runtime = compose_workspace_runtime(inputs).require_runtime()
if mode == 'build':
    from graphify.workspace.identity import discover_source, OperatorAuthorization, IdentityAction
    from graphify.workspace.sync import prepare_structural_sync, synchronize_structural
    runtime.stores.registry.enroll(discover_source(Path(source)), OperatorAuthorization(
        action=IdentityAction.ENROLL, operator_id='operator:s4-fixture', reason='disposable installed proof',
        issued_at='2026-09-27T00:00:00Z', nonce='installed-s4'), expected_revision=0)
    request = prepare_structural_sync(runtime, repo_uuid=repo_uuid, generation_id='gen-installed',
        source_epoch=1, desired_watermark=1, expected_payload_bytes=1024*1024)
    result = synchronize_structural(runtime, request, attempt_sha256='a'*64)
    assert result.pointer_revision == 1
    print('INSTALLED-S4-BUILT')
else:
    from graphify.workspace.adapters.base import QueryRequest
    from graphify.workspace.query import query_structural
    print(json.dumps([query_structural(runtime, repo_uuid, QueryRequest(q), deadline_ns=time.monotonic_ns() + 60_000_000_000)
                      for q in ('caller', '南京市长江大桥')], ensure_ascii=False))
'''


def test_installed_candidate_cold_query_no_writes(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("installed candidate proof requires the repository uv toolchain")
    root = tmp_path.resolve()
    repo_root = Path(__file__).resolve().parents[1]
    source = create_repo(root / "source")
    (source / "main.py").write_text(
        "def leaf(): return 42\ndef caller(): return leaf()\n"
        "def 南京市长江大桥(): return caller()\n", encoding="utf-8")
    wheel_root = root / "wheels"
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("API_KEY") and k not in {"GOOGLE_APPLICATION_CREDENTIALS", "PYTHONPATH", "VIRTUAL_ENV"}}
    for name in ("HOME", "CODEX_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        path = root / name.lower()
        path.mkdir(mode=0o700)
        env[name] = str(path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    # Reuse the dependency cache, but put every environment/install beneath root.
    env["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))
    def run(args, **kwargs):
        result = subprocess.run(args, env=env, cwd=root, capture_output=True, text=True, timeout=180, **kwargs)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout
    run([uv, "build", "--wheel", "--offline", "--out-dir", str(wheel_root), str(repo_root)])
    wheel, = wheel_root.glob("*.whl")
    bundle = root / "bundle"
    build_fixture(repo_root=repo_root, wheel=wheel, output_root=bundle,
                  policy=StructuralPolicy(8, 16384, 1, 8, 1024*1024))
    venv = root / "venv"
    run([uv, "venv", "--python", sys.executable, str(venv)])
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)])
    purelib = Path(run([str(python), "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip())
    # Dependencies are shared read-only; the installed candidate precedes them.
    (purelib / "s4-fixture-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    state = root / "state"
    state.mkdir(mode=0o700)
    authority = state / "runtime-manifest.json"
    authority.write_bytes((bundle / "runtime-manifest.json").read_bytes())
    authority.chmod(0o600)
    args = [str(python), "-B", "-c", _LAUNCH]
    tail = [str(state), str(bundle), str(source), REPO_UUID]
    assert run([*args, "build", *tail]).strip() == "INSTALLED-S4-BUILT"
    before = tree_snapshot(state), tree_snapshot(source), tree_snapshot(venv)
    first = json.loads(run([*args, "query", *tail]))
    assert "leaf" in first[0] and "南京市长江大桥" in first[1]
    import marshal
    cache = Path(env["TMPDIR"]) / "jieba.cache"
    cache.write_bytes(marshal.dumps(({"南京市长江大桥": 999}, 999)))
    second = json.loads(run([*args, "query", *tail]))
    assert first == second
    assert (tree_snapshot(state), tree_snapshot(source), tree_snapshot(venv)) == before
