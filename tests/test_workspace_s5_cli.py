"""Public request boundaries, isolation, refusals and bounded process cleanup."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from graphify.workspace import cli
from graphify.workspace.cli_contracts import WorkspaceCommandRequest, response
from graphify.workspace.contracts import ContractError, canonical_json_bytes
from tests.test_workspace_contracts import compatibility
from tests.workspace_s3_helpers import REPO_UUID, tree_snapshot


@pytest.fixture
def command(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    return {"contract": "graphify.workspace.cli.request", "format_version": 1,
            "command": "query", "request_id": "public-boundary",
            "state_root": str(tmp_path.resolve()), "compatibility_manifest": compatibility().to_dict(),
            "timeout_ms": 60000, "parameters": {"repo_uuid": REPO_UUID,
                "question": "caller", "mode": "bfs", "depth": 2,
                "token_budget": 4096, "context_filters": []}}


@pytest.mark.parametrize("field,value", [
    ("question", ""), ("question", "a" * 257), ("question", 7),
    ("question", "\u4e2d" * 4097), ("depth", True), ("depth", -1), ("depth", 9),
    ("token_budget", 0), ("token_budget", 32769), ("mode", "semantic"),
    ("context_filters", "text"), ("context_filters", ["x"] * 17),
    ("context_filters", ["x" * 129]), ("repo_uuid", "../../outside"),
])
def test_request_bounds_precede_execution(command, monkeypatch, capsys, field, value):
    command["parameters"][field] = value
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(command))
    calls = []
    monkeypatch.setattr(cli, "_run_bounded", lambda *args: calls.append(args))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and json.loads(out.err)["error_code"] == "bad_request"
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("state_root", "relative"), ("state_root", "/root/../outside"),
    ("timeout_ms", 0), ("timeout_ms", True), ("timeout_ms", 300001),
    ("request_id", "operator secret\npath"), ("command", "gc"), ("format_version", True),
])
def test_envelope_bounds_and_redaction(command, monkeypatch, capsys, field, value):
    command[field] = value
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(command))
    monkeypatch.setattr(cli, "_run_bounded", lambda *args: pytest.fail("must not traverse"))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "secret" not in out.err and "outside" not in out.err
    assert out.err.encode() == response(error_code="bad_request").canonical


@pytest.mark.parametrize("raw", [b'{}', b'{}\n', b'{"a":1,"a":2}\n',
    b' \n{}\n', b'{"float":1.0}\n', b'x' * (1024 * 1024 + 1)])
def test_noncanonical_and_oversized_requests(command, monkeypatch, capsys, raw):
    monkeypatch.setattr(cli, "_read_request", lambda name: raw)
    monkeypatch.setattr(cli, "_run_bounded", lambda *args: pytest.fail("must not execute"))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 2
    assert capsys.readouterr().out == ""


def test_request_files_are_bounded_no_follow(command, tmp_path):
    request = tmp_path / "request.json"
    request.write_bytes(canonical_json_bytes(command))
    assert cli._read_request(str(request)) == request.read_bytes()
    link = tmp_path / "request-link"
    link.symlink_to(request)
    with pytest.raises(OSError):
        cli._read_request(str(link))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ContractError):
        cli._read_request(str(fifo))
    request.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ContractError):
        cli._read_request(str(request))


@pytest.mark.parametrize("system,fs,elevated,local", [
    ("Linux", "ext4", False, True), ("Windows", "ntfs", False, True),
    ("Darwin", "apfs", True, True), ("Darwin", "nfs", False, False),
    ("Darwin", "hfs", False, True),
])
def test_host_refusal_precedes_lifecycle_writes(command, tmp_path, monkeypatch,
                                              system, fs, elevated, local):
    from graphify.workspace import composition
    from graphify.workspace.persistence import RuntimeCapabilities, UnsupportedRuntime
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    auth = composition.WorkspaceRuntimeAuthority.from_mapping({
        "contract": "graphify.workspace.runtime_authority.internal", "format_version": 2,
        "compatibility_manifest": command["compatibility_manifest"], "structural_policy":
            composition.StructuralPolicy(8, 16384, 1, 8, 1048576).to_dict(),
    })
    path = state / "runtime-manifest.json"
    path.write_bytes(auth.canonical)
    path.chmod(0o600)
    monkeypatch.setattr(composition, "verify_installed_candidate", lambda expected: None)
    monkeypatch.setattr(RuntimeCapabilities, "detect", classmethod(
        lambda cls, path: RuntimeCapabilities(system, fs, elevated, local)))
    command["state_root"] = str(state)
    before = tree_snapshot(state)
    with pytest.raises(UnsupportedRuntime):
        cli._execute(command, time.monotonic_ns() + 10_000_000_000)
    assert tree_snapshot(state) == before


def test_lifecycle_failure_discards_all_output(command, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(command))
    def refused(*args):
        raise ValueError("/private/source API_KEY=private secret answer")
    monkeypatch.setattr(cli, "_run_bounded", refused)
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 4
    out = capsys.readouterr()
    assert out.out == "" and "private" not in out.err and "answer" not in out.err
    assert json.loads(out.err)["error_code"] == "execution_failed"


def test_unavailable_host_primitives_refuse_before_request_io(command, monkeypatch, capsys):
    monkeypatch.delattr(os, "O_NOFOLLOW")
    monkeypatch.setattr(cli, "_read_request", lambda name: pytest.fail("unsupported host must not read"))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 3
    out = capsys.readouterr()
    assert out.out == "" and json.loads(out.err)["error_code"] == "unsupported_runtime"


def test_unprotected_launch_is_refused(command, monkeypatch, capsys):
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(cli, "_read_request", lambda name: pytest.fail("launch guard must precede read"))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 3
    assert capsys.readouterr().out == ""


def test_wire_output_uses_canonical_bytes():
    from types import SimpleNamespace
    raw = io.BytesIO()
    stream = SimpleNamespace(buffer=raw, write=lambda value: pytest.fail("text newline translation"),
                             flush=lambda: None)
    reply = response(error_code="unsupported_runtime")
    cli._emit(reply, stream)
    assert raw.getvalue() == reply.canonical
    assert raw.getvalue().endswith(b"\n") and b"\r" not in raw.getvalue()


@pytest.mark.parametrize("text", ["private answer", '{"command":"query"}'])
def test_invalid_worker_response_is_withheld(command, monkeypatch, capsys, text):
    monkeypatch.setattr(cli, "_BOOTSTRAP", f"print({text!r})")
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(command))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 4
    out = capsys.readouterr()
    assert out.out == "" and "answer" not in out.err
    assert json.loads(out.err)["error_code"] == "execution_failed"


def test_response_command_mismatch_is_rejected(command, monkeypatch, capsys):
    command["command"] = "status"
    command["parameters"] = {"repo_uuid": REPO_UUID}
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(command))
    monkeypatch.setattr(cli, "_run_bounded", lambda *args: pytest.fail("mismatch"))
    assert cli.run_workspace_cli(["query", "--request", "-"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mutation", [False, True])
def test_supervisor_timeout_kills_descendants_and_reports_uncertainty(command, tmp_path,
                                                                     monkeypatch, capsys, mutation):
    marker = tmp_path / "descendant"
    if mutation:
        command["command"] = "register"
        command["parameters"] = {"operation": "enroll", "source_root": str(tmp_path.resolve()),
            "expected_registry_revision": 0, "authorization": {"action": "ENROLL",
                "operator_id": "fixture", "reason": "fixture", "issued_at": "2026-10-03T00:00:00Z",
                "nonce": "fixture"}}
    command["timeout_ms"] = 1000
    bootstrap = f'''import sys,subprocess,time
from pathlib import Path
child = subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time; time.sleep(60)'])
Path({str(marker)!r}).write_text(str(child.pid))
time.sleep(60)
'''
    monkeypatch.setattr(cli, "_BOOTSTRAP", bootstrap)
    monkeypatch.setattr(cli, "_read_request", lambda name: canonical_json_bytes(command))
    started = time.monotonic()
    assert cli.run_workspace_cli([command["command"], "--request", "-"]) == 5
    assert time.monotonic() - started < 5
    out = capsys.readouterr()
    assert out.out == ""
    assert json.loads(out.err)["error_code"] == ("execution_unknown" if mutation else "deadline_exceeded")
    pid = int(marker.read_text())
    probe = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=180)
    assert not probe.stdout.strip() or probe.stdout.strip().startswith("Z")


def test_nested_readonly_helpers_join_supervisor_group(command, tmp_path, monkeypatch):
    bootstrap = '''import sys,os,json,time
paths,cache,readonly,deadline=json.loads(sys.argv[1]);sys.path[:]=paths
from graphify.workspace import _readonly
from graphify.workspace.cli_contracts import response
_readonly._SUPERVISED_GROUP=os.getpgrp()
raw=_readonly.run_readonly("import os,sys;sys.stdout.write(str(os.getpgrp()))",b"",
    deadline_ns=deadline,max_input_bytes=1,max_output_bytes=1024)
assert int(raw)==os.getpgrp()
sys.stdout.buffer.write(response("query","public-boundary",result={"text":"same-group"}).canonical)
'''
    monkeypatch.setattr(cli, "_BOOTSTRAP", bootstrap)
    reply, code = cli._run_bounded(WorkspaceCommandRequest.from_mapping(command),
                                   time.monotonic_ns() + 10_000_000_000)
    assert code == 0 and reply.to_dict()["result"] == {"text": "same-group"}


@pytest.mark.parametrize("workspace", [False, True])
def test_lazy_help_preserves_ordinary_imports(workspace):
    code = '''import sys
sys.argv=['graphify', 'workspace', '--help'] if sys.argv[1]=='workspace' else ['graphify','--help']
import graphify.__main__ as entry
try: entry.main()
except SystemExit as exc: assert exc.code==0
if sys.argv[1]=='workspace':
    assert 'graphify.install' not in sys.modules and 'graphify.cli' not in sys.modules
else:
    assert not any(name.startswith('graphify.workspace') for name in sys.modules)
print('LAZY-HELP-OK')
'''
    result = subprocess.run([sys.executable, "-B", "-c", code,
                             "workspace" if workspace else "ordinary"], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("LAZY-HELP-OK\n")


def test_command_schema_and_model_agree(command):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from graphify.workspace.contracts import SCHEMA_FILES
    root = Path(__file__).resolve().parents[1] / "graphify/workspace/schemas"
    schemas = [json.loads((root / name).read_text()) for name in SCHEMA_FILES]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    schema = next(s for s in schemas if s["$id"].endswith("cli-request.schema.json"))
    validator = Draft202012Validator(schema, registry=registry)
    auth = {"action": "ENROLL", "operator_id": "fixture", "reason": "fixture",
            "issued_at": "2026-10-03T00:00:00.123Z", "nonce": "fixture"}
    register = {"operation": "enroll", "source_root": command["state_root"],
                "expected_registry_revision": 0, "authorization": auth}
    activate = {"source_root": command["state_root"], "authorization": dict(auth, action="ACTIVATE"),
                "expected_registry_revision": 1, "expected_active_source_revision": 1,
                "expected_operation_epoch": 1, "expected_migration_epoch": 0, "ttl_ns": 1000000}
    prepare = {"operation": "prepare", "repo_uuid": REPO_UUID, "generation_id": "gen-schema",
               "source_epoch": 1, "desired_watermark": 1, "expected_payload_bytes": 1048576}
    for name, params in (("query", command["parameters"]), ("status", {"repo_uuid": REPO_UUID}),
                         ("doctor", {"repo_uuid": REPO_UUID}), ("register", register),
                         ("activate", activate), ("sync", prepare)):
        value = dict(command, command=name, parameters=params)
        validator.validate(value)
        WorkspaceCommandRequest.from_mapping(value)
        broken = copy.deepcopy(value)
        broken["parameters"]["extra"] = "private"
        assert list(validator.iter_errors(broken))
        with pytest.raises(ContractError):
            WorkspaceCommandRequest.from_mapping(broken)


def test_redirected_regular_stdin_does_not_require_selector(tmp_path, monkeypatch):
    request = tmp_path / 'request'
    request.write_bytes(b'prefix{}\n')
    class RejectRegularFiles:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def register(self, *args): raise PermissionError('epoll rejects regular files')
    monkeypatch.setattr(cli.selectors, 'DefaultSelector', RejectRegularFiles)
    with request.open('rb', buffering=0) as stream:
        stream.read(6)
        monkeypatch.setattr(sys, 'stdin', stream)
        assert cli._read_request('-') == b'{}\n'
        assert not stream.closed


@pytest.mark.parametrize("expired", [False, True])
def test_redirected_regular_stdin_keeps_byte_and_acquisition_limits(tmp_path, monkeypatch, expired):
    request = tmp_path / "request"
    request.write_bytes(b"{}\n" if expired else b"x" * (cli.MAX_REQUEST_BYTES + 1))
    if expired:
        ticks = iter((0, 11))
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    with request.open("rb", buffering=0) as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        with pytest.raises(ContractError, match="acquisition expired" if expired else "regular request"):
            cli._read_request("-")
        assert not stream.closed


@pytest.mark.parametrize("imports", [False, True])
def test_legacy_exports_survive_workspace_argv_and_later_ordinary_dispatch(imports):
    code = '''import sys
mode=sys.argv[-1]
sys.argv=['graphify','workspace','--help',mode]
import graphify.__main__ as entry
if sys.argv[-1]=='imports':
    from graphify.__main__ import install, _StageTimer, _CLAUDE_MD_SECTION
    from graphify.install import install as implementation
    from graphify.cli import _StageTimer as timer
    assert install is implementation and _StageTimer is timer
    assert isinstance(_CLAUDE_MD_SECTION,str)
sys.argv=['graphify','--help']
entry.main()
print('LEGACY-OK')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, 'imports' if imports else 'dispatch'], capture_output=True,
                            text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('LEGACY-OK\n')


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin host qualification')
def test_readonly_worker_ignores_path_host_probe_shadows(command, tmp_path, monkeypatch):
    marker = tmp_path / 'side-effect'
    tools = tmp_path / 'shadow-tools'
    tools.mkdir()
    for name in ('df', 'diskutil'):
        helper = tools / name
        helper.write_text('#!/bin/sh\nprintf side-effect > ' + str(marker) + '\nexit 1\n')
        helper.chmod(0o700)
    monkeypatch.setenv('PATH', str(tools) + os.pathsep + os.environ['PATH'])
    # Retain the public worker's actual import isolation and audit hook, then
    # exercise the capability detector called before every public operation.
    tail = '''from graphify.workspace.persistence import RuntimeCapabilities
from graphify.workspace.cli_contracts import response
capabilities=RuntimeCapabilities.detect(__import__('pathlib').Path(''' + repr(str(tmp_path)) + '''))
sys.stdout.buffer.write(response('query','public-boundary',result={'filesystem':capabilities.filesystem}).canonical)
'''
    monkeypatch.setattr(cli, '_BOOTSTRAP', cli._BOOTSTRAP.replace(
        'from graphify.workspace.cli import _command_child\n_command_child(deadline_ns)', tail))
    reply, code = cli._run_bounded(WorkspaceCommandRequest.from_mapping(command),
                                   time.monotonic_ns() + 10_000_000_000)
    assert code == 0
    assert not marker.exists()
    assert reply.to_dict()['result']['filesystem'] == 'apfs'
