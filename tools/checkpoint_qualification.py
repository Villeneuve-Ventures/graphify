"""Opt-in A14 qualification in a disposable, administrator-provisioned macOS VM.

Install this reviewed standalone file outside the source checkout under protected
root-owned ancestors. Invoke with the reviewed installed Python using -I -B.
This runner never installs software, creates accounts, removes trial data, or
replaces existing configuration. --disposable is an operator assertion, not VM
attestation. The expected source revision is a build provenance assertion; wheel
bytes and every installed wheel payload are independently checked here.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Any

CONFIG = Path("/private/etc/graphify/recovery-authority.json")
SANDBOX = "/usr/bin/sandbox-exec"
PROFILE = "(version 1)(allow default)(deny network*)"
NETWORK_SCOPE = "candidate and fixture commands, including network probes; excludes fixed local OS privilege probe"
PRIVILEGE_PROBE = ["/usr/bin/sudo", "-n", "/usr/bin/true"]
SOURCE = hashlib.sha256(b"graphify A14 disposable qualification source v1").hexdigest()
MAX_OUTPUT = 64 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
REQUIRED_CHECKS = frozenset({
    "network_admin", "network_reader", "installed_candidate", "source_publication",
    "capture", "preview", "restore", "snapshot", "direct_writes_denied",
    "reader_capture_denied", "reader_revoke_denied", "unsafe_mode_denied",
    "unsafe_acl_denied", "malformed_head_denied", "tampered_artifact_denied",
    "stale_preview_denied", "reader_privilege_denied",
})


class QualificationError(RuntimeError):
    """A required native qualification condition failed."""


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False, "nonfinite JSON"))


def validate_spec(value: Any):
    """Validate dispatch data only; this function never provisions anything."""
    fields = {"schema", "disposable", "python", "harness", "harness_sha256", "workspace",
              "reader_uid", "reader_gid", "wheel", "wheel_sha256", "source_revision"}
    require(isinstance(value, dict) and set(value) == fields, "A14 spec fields differ")
    require(type(value["schema"]) is int and value["schema"] == 1
            and value["disposable"] is True, "explicit disposable A14 spec required")
    for field in ("python", "harness", "wheel", "workspace"):
        text = value[field]
        require(type(text) is str and text.startswith("/") and len(text) <= 4096
                and str(Path(text)) == text and os.path.normpath(text) == text
                and not text.startswith("//"), "invalid A14 " + field)
    require(re.fullmatch(r"/private/var/db/graphify-a14-[a-zA-Z0-9][a-zA-Z0-9-]{0,63}",
                         value["workspace"]) is not None, "invalid A14 workspace")
    for field in ("reader_uid", "reader_gid"):
        item = value[field]
        require(type(item) is int and 0 < item < 2**32, "invalid " + field)
    for field, length in (("harness_sha256", 64), ("wheel_sha256", 64), ("source_revision", 40)):
        item = value[field]
        require(type(item) is str
                and re.fullmatch("[0-9a-f]{" + str(length) + "}", item) is not None,
                "invalid " + field)
    return value


def validate_reader_policy(uid, gid, groups):
    require(type(uid) is int and type(gid) is int and 0 < uid < 2**32 and 0 < gid < 2**32
            and gid in groups, "reader must be a pre-existing account/member of the reader group")
    require(80 not in groups, "reader must not belong to the macOS admin group (GID 80)")


def reader_acl_entry(account_name):
    require(type(account_name) is str
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", account_name) is not None,
            "reader account short name is unsafe for an ACL descriptor")
    return "user:" + account_name + " allow read,write"


def bounded_file(path, limit):
    """Read one regular descriptor without following a replaced link or FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
                "A14 input must be a bounded regular unlinked file")
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        signature = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                  info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        require(remaining > 0 and limit + 1 - remaining == after.st_size
                and signature(before) == signature(after) == signature(named),
                "A14 input changed or exceeded its read bound")
        return b"".join(chunks)
    finally:
        os.close(fd)


def opt_in_spec(environment=None):
    """None means opt-out; any present but invalid opt-in is an error, never skip."""
    environment = os.environ if environment is None else environment
    if "GRAPHIFY_A14_SPEC" not in environment:
        return None
    path = Path(environment["GRAPHIFY_A14_SPEC"])
    require(path.is_absolute(), "GRAPHIFY_A14_SPEC must be an absolute JSON path")
    return validate_spec(strict_json(bounded_file(path, 16 * 1024)))


def command_for_spec(spec):
    validate_spec(spec)
    harness = Path(spec["harness"])
    require(sha(harness) == spec["harness_sha256"], "reviewed harness SHA256 differs")
    return [spec["python"], "-I", "-B", str(harness), "--disposable",
            "--workspace", spec["workspace"], "--reader-uid", str(spec["reader_uid"]),
            "--reader-gid", str(spec["reader_gid"]), "--wheel", spec["wheel"],
            "--expected-wheel-sha256", spec["wheel_sha256"],
            "--expected-source-revision", spec["source_revision"]]


def validate_privilege_denial(stdout, stderr):
    require(not stdout and "sudo:" in stderr and any(reason in stderr for reason in (
        "a password is required", "not allowed", "not in the sudoers file")),
        "reader sudo refusal did not establish an authentication/policy denial")


def validate_report(report: Any, spec: dict[str, Any]):
    require(isinstance(report, dict) and report.get("schema") == "graphify.a14.v1"
            and report.get("status") == "passed", "A14 did not pass")
    require(report.get("checks") == sorted(REQUIRED_CHECKS), "A14 required matrix incomplete")
    require(report.get("inputs") == {key: spec[key] for key in (
        "workspace", "reader_uid", "reader_gid", "wheel", "wheel_sha256", "source_revision")},
        "A14 report does not bind expected inputs")
    require(report.get("harness_sha256") == spec["harness_sha256"], "A14 report harness differs")
    require(report.get("network_policy") == PROFILE and report.get("network_policy_scope") == NETWORK_SCOPE
            and report.get("commands")
            and report.get("preservation"), "A14 evidence incomplete")
    require(all(item.get("before") == item.get("after") for item in report["preservation"]),
            "A14 preservation evidence differs")
    probes = [item for item in report["commands"]
              if item.get("purpose") == "environment_privilege_probe"]
    require(len(probes) == 1, "A14 requires exactly one local environment privilege probe")
    probe = probes[0]
    require(probe.get("name") == "reader_privilege_denied" and probe.get("argv") == PRIVILEGE_PROBE
            and probe.get("role") == {"uid": spec["reader_uid"], "gid": spec["reader_gid"],
                                      "groups": [spec["reader_gid"]]}
            and probe.get("returncode") == 1 and probe.get("timed_out") is False,
            "A14 local environment privilege probe differs")
    validate_privilege_denial(probe.get("stdout"), probe.get("stderr", ""))
    require(all(item is probe or item.get("argv", [])[:3] == [SANDBOX, "-p", PROFILE]
                for item in report["commands"]), "A14 command escaped network denial")
    return report


def execute_spec(spec):
    """Dispatch only an explicit opt-in; a broken prerequisite is a test failure."""
    command = command_for_spec(spec)
    require(sys.platform == "darwin" and os.getuid() == os.geteuid() == 0,
            "opt-in A14 dispatch requires root in the disposable macOS VM")
    protected(Path(spec["python"]).resolve(strict=True))
    protected(Path(spec["harness"]))
    result = subprocess.run(command, cwd="/", stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=1800, check=False,
                            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/var/empty",
                                 "LC_ALL": "C"})
    require(result.returncode == 0, "native A14 failed: " + result.stdout[:4096] + result.stderr[:4096])
    require(not result.stderr, "A14 runner emitted unexpected stderr")
    outcome = strict_json(result.stdout)
    path = Path(spec["workspace"]) / "a14-report.json"
    require(outcome.get("status") == "passed" and outcome.get("workspace") == spec["workspace"]
            and outcome.get("report") == str(path), "A14 runner outcome differs")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 8 * 1024 * 1024,
            "unsafe/missing native A14 report")
    return validate_report(strict_json(bounded_file(path, 8 * 1024 * 1024)), spec)


def _acl_safe(fd):
    """Inspect native ACLs before importing any candidate product code."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    libc.acl_get_tag_type.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    libc.acl_free.argtypes = (ctypes.c_void_p,)
    acl = libc.acl_get_fd_np(fd, 0x100)
    if not acl:
        require(ctypes.get_errno() == errno.ENOENT, "cannot inspect native ACL")
        return True
    try:
        for index in range(1000):
            entry, tag = ctypes.c_void_p(), ctypes.c_int()
            if libc.acl_get_entry(acl, index, ctypes.byref(entry)):
                require(ctypes.get_errno() == errno.EINVAL, "cannot inspect ACL entry")
                return True
            require(libc.acl_get_tag_type(entry, ctypes.byref(tag)) == 0,
                    "cannot inspect ACL tag")
            if tag.value != 2:
                return False
        return False
    finally:
        libc.acl_free(acl)


def protected(path):
    """Check an existing canonical root-owned path and every ancestor, no links."""
    require(path.is_absolute() and path.resolve(strict=True) == path, "noncanonical trusted path")
    for item in (path, *path.parents):
        info = item.lstat()
        require(info.st_uid == 0 and not info.st_mode & 0o7022
                and (stat.S_ISDIR(info.st_mode) or
                     (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)),
                "unsafe trusted path: " + str(item))
        fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            require(_acl_safe(fd), "unsafe trusted ACL: " + str(item))
        finally:
            os.close(fd)


def _limit_child_output():
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def snapshot(path):
    """Bounded hashes and identities; no access-time fields or link traversal."""
    result, total = {}, 0
    paths = [path]
    if path.is_dir():
        for item in path.rglob("*"):
            paths.append(item)
            require(len(paths) <= 1000, "snapshot entry bound exceeded")
        paths.sort()
    require(len(paths) <= 1000, "snapshot entry bound exceeded")
    for item in paths:
        info = item.lstat()
        require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                "unexpected snapshot entry type")
        record = {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
                  "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                  "nlink": info.st_nlink, "flags": info.st_flags,
                  "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
        if item.is_file():
            total += info.st_size
            require(total <= MAX_TREE_BYTES, "snapshot byte bound exceeded")
            record.update(size=info.st_size, sha256=sha(item))
        result[str(item.relative_to(path))] = record
    return result


def validate_origins(probe: dict[str, Any], site: Path, payloads: dict[str, str]):
    modules = probe.get("loaded_modules", {})
    required = {"graphify", "graphify.__main__", "graphify.checkpoint_restore",
                "graphify.recovery_authority", "graphify.transaction"}
    require(isinstance(modules, dict) and required <= modules.keys(), "candidate origin probe incomplete")
    for name, value in modules.items():
        require(name == "graphify" or name.startswith("graphify."), "unexpected module probe name")
        possible = [name.replace(".", "/") + ".py", name.replace(".", "/") + "/__init__.py"]
        matched = [relative for relative in possible
                   if relative in payloads and value.get("origin") == str(site / relative)
                   and value.get("sha256") == payloads[relative]]
        require(len(matched) == 1, "loaded module does not match installed wheel: " + name)


CANDIDATE_PROBE = '''import hashlib,importlib.metadata,json,os,pathlib,sys
import graphify,graphify.__main__,graphify.checkpoint_restore,graphify.transaction
from graphify import recovery_authority as authority
if os.getuid()==0:
    authority._qualify_runtime()
d=importlib.metadata.distribution("graphifyy")
assert not json.loads(d.read_text("direct_url.json") or "{}").get("dir_info",{}).get("editable",False)
modules={}
for name,module in tuple(sys.modules.items()):
    if name=="graphify" or name.startswith("graphify."):
        path=pathlib.Path(module.__file__).resolve(strict=True)
        modules[name]={"origin":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
networkx=__import__("networkx")
print(json.dumps({"version":d.version,"site":str(pathlib.Path(d.locate_file("")).resolve()),
 "uid":os.getuid(),"sys_path":sys.path,"loaded_modules":modules,
 "networkx_version":importlib.metadata.version("networkx"),"networkx_origin":networkx.__file__,
 "networkx_sha256":hashlib.sha256(pathlib.Path(networkx.__file__).read_bytes()).hexdigest()}))
'''


def validate_child_identity(identity, uid, gid, reader_account_groups=None):
    require(identity["uid"] == identity["euid"] == uid
            and identity["gid"] == identity["egid"] == gid,
            "child role identity differs")
    require(sorted(set(identity["credential_groups"])) == [gid],
            "child kernel credential groups differ")
    if reader_account_groups is not None:
        require(set(identity["access_groups"]) == set(reader_account_groups)
                and gid in identity["access_groups"] and 80 not in identity["access_groups"],
                "reader account access groups differ or include macOS admin")


IDENTITY = '''import ctypes,json,os,sys
# On Darwin os.getgroups() reports directory-service access groups. Read the
# kernel credential groups independently to verify the actual setgroups drop.
libc=ctypes.CDLL(None,use_errno=True)
libc.getgroups.argtypes=(ctypes.c_int,ctypes.POINTER(ctypes.c_uint32))
libc.getgroups.restype=ctypes.c_int
count=libc.getgroups(0,None)
assert 0<=count<=1024, ("invalid kernel group count",count,ctypes.get_errno())
values=(ctypes.c_uint32*count)()
actual=libc.getgroups(count,values)
assert 0<=actual<=count, ("cannot inspect kernel groups",actual,ctypes.get_errno())
print(json.dumps({"uid":os.getuid(),"euid":os.geteuid(),"gid":os.getgid(),
                  "egid":os.getegid(),"access_groups":os.getgroups(),
                  "credential_groups":list(values[:actual]),"python":sys.executable}))
'''
NETWORK = '''import errno, json, socket
s=socket.socket()
try:
    s.connect(("127.0.0.1",9))
except OSError as exc:
    assert exc.errno in (errno.EPERM,errno.EACCES), repr(exc)
    print(json.dumps({"network_errno":exc.errno}))
else:
    raise AssertionError("sandbox permitted network")
finally:
    s.close()
'''
PUBLISH = '''import json, pathlib, sys
from graphify import transaction as tx
root,output=map(pathlib.Path,sys.argv[1:])
root.mkdir(mode=0o700)
(root/"file.py").write_bytes(b"x = 1\\n")
owner=tx.begin_transaction("full",root,output=output)
graph={"directed":False,"multigraph":False,"graph":{
 tx.GRAPH_WATERMARK_KEY:{"schema":1,"protocol_epoch":1,"generation":1,"state":"active"}},
 "nodes":[{"id":"saved","weight":0.75}],"links":[]}
tx.commit_publication_plan(owner,tx.PublicationPlan({"graph.json":json.dumps(graph).encode(),
 "manifest.json":b"{}","nested/report.txt":b"saved report"}))
tx.finish_transaction(owner)
assert tx.open_graph_snapshot(output/"graph.json",purpose="A14 source").generation==1
print(json.dumps({"status":"closed","generation":1}))
'''
VERIFY = '''import json,pathlib,sys
from graphify import transaction as tx
original,output=map(pathlib.Path,sys.argv[1:])
snap=tx.open_graph_snapshot(output/"graph.json",purpose="A14 restored")
assert snap.generation==1
assert snap.data["nodes"]==[{"id":"saved","weight":0.75}]
assert snap.data["links"]==[]
graph=json.loads((original/"graph.json").read_bytes())
graph["graph"][tx.GRAPH_WATERMARK_KEY]={"schema":1,"protocol_epoch":1,"generation":1,"state":"active"}
assert snap.data==graph
assert (output/"graph.json").read_bytes()==json.dumps(graph,sort_keys=True,ensure_ascii=True,
 separators=(",",":"),allow_nan=False).encode("ascii")
for name in ("manifest.json","nested/report.txt"):
    assert (original/name).read_bytes()==(output/name).read_bytes()
assert (output.stat().st_dev,output.stat().st_ino)!=(original.stat().st_dev,original.stat().st_ino)
print(json.dumps({"status":"verified","generation":snap.generation,
 "output_identity":{"device":output.stat().st_dev,"inode":output.stat().st_ino},
 "graph_sha256":__import__("hashlib").sha256((output/"graph.json").read_bytes()).hexdigest()}))
'''
WRITE_OPEN_CONTROL = '''import json,os,stat,sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
try:
    info=os.fstat(fd)
    assert stat.S_ISREG(info.st_mode)
finally:
    os.close(fd)
print(json.dumps({"status":"write_open_permitted","device":info.st_dev,"inode":info.st_ino,
                  "uid":os.getuid(),"euid":os.geteuid(),"gid":os.getgid(),"egid":os.getegid()}))
'''
DENIED_WRITES = '''import errno,json,os,sys
results=[]
for path in sys.argv[1:-1]:
    try:
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
    except OSError as exc:
        assert exc.errno in (errno.EACCES,errno.EPERM), (path,exc)
        results.append({"path":path,"errno":exc.errno})
    else:
        os.close(fd)
        raise AssertionError("reader opened protected file for writing: "+path)
try:
    fd=os.open(sys.argv[-1],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
except OSError as exc:
    assert exc.errno in (errno.EACCES,errno.EPERM), repr(exc)
    results.append({"path":sys.argv[-1],"errno":exc.errno})
else:
    os.close(fd)
    raise AssertionError("reader created a protected-store entry")
print(json.dumps({"denied":results}))
'''


class Trial:
    def __init__(self, args):
        self.args = args
        self.workspace = Path(args.workspace)
        self.store = self.workspace / "authority"
        self.reader = self.workspace / "reader"
        self.root = self.reader / "corpus"
        self.original = self.reader / "original"
        self.checks = set()
        self.report = {"schema": "graphify.a14.v1", "status": "running", "checks": [],
                       "commands": [], "preservation": [], "network_policy": PROFILE,
                       "network_policy_scope": NETWORK_SCOPE,
                       "inputs": {"workspace": args.workspace, "reader_uid": args.reader_uid,
                                  "reader_gid": args.reader_gid, "wheel": args.wheel,
                                  "wheel_sha256": args.expected_wheel_sha256,
                                  "source_revision": args.expected_source_revision},
                       "harness_sha256": sha(Path(__file__)), "started_ns": time.time_ns()}
        self.provisioned = False

    def run(self, name, command, *, reader=False, expected=0, environment_privilege_probe=False):
        role = {"uid": self.args.reader_uid if reader else 0,
                "gid": self.args.reader_gid if reader else 0,
                "groups": [self.args.reader_gid] if reader else []}
        if environment_privilege_probe:
            require(name == "reader_privilege_denied" and command == PRIVILEGE_PROBE
                    and reader and expected == 1 and not self.provisioned
                    and not os.path.lexists(self.workspace) and not os.path.lexists(CONFIG)
                    and not any(item.get("purpose") == "environment_privilege_probe"
                                for item in self.report["commands"]),
                    "only the fixed preprovision reader OS privilege probe may run outside sandbox")
            argv = list(PRIVILEGE_PROBE)
        else:
            argv = [SANDBOX, "-p", PROFILE, *command]
        # File-backed capture avoids an unbounded pipe buffer from a broken child.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            start = time.monotonic()
            timeout_error = None
            try:
                result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    cwd="/", env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                                  "HOME": "/var/empty", "LC_ALL": "C"},
                                    user=role["uid"], group=role["gid"], extra_groups=role["groups"],
                                        timeout=150, check=False, preexec_fn=_limit_child_output)
                returncode = result.returncode
            except subprocess.TimeoutExpired as exc:
                timeout_error = exc
                returncode = None
            sizes = [stream.tell() for stream in (out, err)]
            for stream in (out, err):
                stream.seek(0)
            stdout, stderr = (stream.read(MAX_OUTPUT + 1).decode("utf-8", "replace")
                              for stream in (out, err))
        record = {"name": name, "role": role, "argv": argv, "returncode": returncode,
                  "purpose": "environment_privilege_probe" if environment_privilege_probe else "sandboxed_command",
                  "timed_out": timeout_error is not None, "stdout": stdout[:MAX_OUTPUT], "stderr": stderr[:MAX_OUTPUT],
                  "output_sizes": sizes, "seconds": time.monotonic() - start}
        self.report["commands"].append(record)
        require(timeout_error is None, name + ": command exceeded 150 second deadline")
        require(max(sizes) <= MAX_OUTPUT, name + ": command output exceeds bound")
        require(returncode == expected,
                name + ": unexpected exit " + str(returncode) + ": " + stderr[:2048])
        return stdout, stderr

    def python(self, name, code, *args, reader=False):
        stdout, stderr = self.run(name, [sys.executable, "-I", "-B", "-c", code, *map(str, args)],
                             reader=reader)
        require(not stderr, name + ": unexpected stderr")
        return strict_json(stdout)

    def frozen(self):
        return {str(path): snapshot(path) for path in
                (CONFIG, self.store, self.root, self.original)}

    def reader_operation(self, name, operation):
        before = self.frozen()
        try:
            result = operation()
        except BaseException:
            # Preserve the original error even if the evidence read also fails.
            try:
                after = self.frozen()
            except Exception as evidence_error:
                self.report["preservation"].append({"name": name, "before": before,
                                                    "snapshot_error": str(evidence_error)[:1024]})
            else:
                self.report["preservation"].append({"name": name, "before": before, "after": after})
            raise
        after = self.frozen()
        self.report["preservation"].append({"name": name, "before": before, "after": after})
        require(before == after, name + ": reader changed source or authority state")
        return result

    def cli(self, name, *args, reader=False, refused=False, reason=None):
        def call():
            stdout, stderr = self.run(name, [sys.executable, "-I", "-B", "-m",
                                             "graphify.checkpoint_restore", *map(str, args)],
                                      reader=reader, expected=2 if refused else 0)
            result = strict_json(stderr if refused else stdout)
            if refused:
                require(not stdout and result.get("status") == "refused"
                        and type(result.get("reason")) is str and result["reason"],
                        name + ": missing structured refusal")
                require(reason is not None and reason in result["reason"],
                        name + ": wrong refusal boundary: " + result["reason"])
            else:
                require(not stderr, name + ": unexpected stderr")
            return result
        return self.reader_operation(name, call) if reader else call()

    def request(self, name):
        output = self.reader / name
        request = self.workspace / (name + ".json")
        payload = {"schema": 1, "source_id": SOURCE, "root": str(self.root), "output": str(output)}
        self.new_file(request, json.dumps(payload).encode(), 0o644, 0)
        return request, output

    def new_file(self, path, payload, mode, gid):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.fchown(fd, 0, gid)
            os.fchmod(fd, mode)
            with os.fdopen(os.dup(fd), "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)

    def qualify_volume(self):
        device_output, stderr = self.run("APFS containing device", ["/usr/bin/stat", "-f", "%Sd",
                                                                   str(self.workspace.parent)])
        device = device_output.strip()
        require(not stderr and re.fullmatch(r"disk[0-9]+(?:s[0-9]+)*", device) is not None,
                "cannot resolve a strict containing disk identifier")
        node = "/dev/" + device
        payload, stderr = self.run("APFS volume", ["/usr/sbin/diskutil", "info", "-plist", node])
        require(not stderr, "diskutil emitted unexpected stderr")
        import plistlib
        volume = plistlib.loads(payload.encode())
        require(isinstance(volume, dict) and volume.get("DeviceIdentifier") == device
                and volume.get("DeviceNode") == node and volume.get("FilesystemType") == "apfs"
                and volume.get("GlobalPermissionsEnabled") is True,
                "resolved trial device must match APFS with ownership enabled")
        return {"device": device, "record": volume}

    def reader_privilege_probe(self):
        # macOS Seatbelt refuses SUID exec. This exact local OS policy probe runs
        # before private fixture/config creation; it receives no candidate data.
        stdout, stderr = self.run("reader_privilege_denied", PRIVILEGE_PROBE,
                                  reader=True, expected=1, environment_privilege_probe=True)
        validate_privilege_denial(stdout, stderr)
        self.checks.add("reader_privilege_denied")

    def preflight(self):
        require(self.args.disposable, "--disposable is required")
        require(sys.platform == "darwin" and os.getuid() == os.geteuid() == 0,
                "A14 requires real/effective UID 0 on native macOS")
        require(sys.implementation.name == "cpython" and sys.version_info.releaselevel == "final"
                and (3, 14, 2) <= sys.version_info[:3] < (3, 15, 0)
                and sys.flags.isolated and sys.flags.dont_write_bytecode,
                "A14 requires installed CPython 3.14.2+ with -I -B")
        require(re.fullmatch(r"/private/var/db/graphify-a14-[a-zA-Z0-9][a-zA-Z0-9-]{0,63}",
                             self.args.workspace) is not None, "invalid workspace")
        for text, length in ((self.args.expected_source_revision, 40),
                             (self.args.expected_wheel_sha256, 64)):
            require(re.fullmatch("[0-9a-f]{" + str(length) + "}", text) is not None,
                    "invalid expected provenance hash")
        require(not os.path.lexists(self.workspace) and not os.path.lexists(CONFIG),
                "workspace and fixed authority config must both be absent")
        import grp
        import pwd
        account = pwd.getpwuid(self.args.reader_uid)
        self.reader_acl = reader_acl_entry(account.pw_name)
        grp.getgrgid(self.args.reader_gid)
        reader_groups = os.getgrouplist(account.pw_name, account.pw_gid)
        validate_reader_policy(self.args.reader_uid, self.args.reader_gid, reader_groups)
        for path in (self.workspace.parent, CONFIG.parent if CONFIG.parent.exists() else CONFIG.parent.parent,
                     Path(__file__), Path(sys.executable).resolve(strict=True), Path(self.args.wheel)):
            protected(path)
        for module in tuple(sys.modules.values()):
            origin = getattr(module, "__file__", None)
            if origin:
                protected(Path(origin).resolve(strict=True))
        for entry in sys.path:
            require(entry and Path(entry).is_absolute(), "unsafe parent import path")
            path = Path(entry)
            protected(path if path.exists() else path.parent)
        require(sha(Path(self.args.wheel)) == self.args.expected_wheel_sha256,
                "candidate wheel SHA256 differs")
        require(Path(SANDBOX).is_file(), "sandbox-exec is required; no network downgrade allowed")
        self.report["environment"] = {"platform": platform.platform(), "uname": list(platform.uname()),
                                      "python": sys.version, "executable": sys.executable,
                                      "runtime_sha256": sha(Path(sys.executable)),
                                      "reader_account": account.pw_name, "reader_account_groups": reader_groups}
        for reader in (False, True):
            identity = self.python("reader_identity" if reader else "administrator_identity",
                                   IDENTITY, reader=reader)
            validate_child_identity(identity, self.args.reader_uid if reader else 0,
                                    self.args.reader_gid if reader else 0,
                                    reader_groups if reader else None)
            self.python("network_reader" if reader else "network_admin", NETWORK, reader=reader)
            self.checks.add("network_reader" if reader else "network_admin")
        self.reader_privilege_probe()
        volume = self.qualify_volume()
        self.report["environment"]["volume_device"] = volume["device"]
        self.report["environment"]["volume"] = volume["record"]
        # This uses the unchanged installed product's actual runtime qualification.
        candidate = self.python("installed administrator runtime", CANDIDATE_PROBE)
        site = Path(candidate["site"])
        protected(site)
        compared = {}
        wheel_total = 0
        with zipfile.ZipFile(self.args.wheel) as wheel:
            infos = wheel.infolist()
            require(len(infos) <= 3000 and len({i.filename for i in infos}) == len(infos),
                    "wheel inventory bound/uniqueness failed")
            for info in infos:
                if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
                    continue
                wheel_total += info.file_size
                require(wheel_total <= 128 * 1024 * 1024, "wheel byte bound exceeded")
                name = Path(info.filename)
                require(not name.is_absolute() and ".." not in name.parts and ".data" not in name.parts
                        and info.file_size <= MAX_TREE_BYTES, "unsupported wheel payload")
                installed = site / name
                protected(installed)
                expected = hashlib.sha256(wheel.read(info)).hexdigest()
                require(sha(installed) == expected, "installed payload differs from candidate wheel: " + str(name))
                compared[str(name)] = expected
        require("graphify/checkpoint_restore.py" in compared and "graphify/recovery_authority.py" in compared,
                "wheel omits candidate checkpoint modules")
        installed_code = set()
        for path in (site / "graphify").rglob("*"):
            require(not path.is_symlink(), "symlink in installed graphify package")
            if path.is_file():
                require(path.suffix != ".pyc", "candidate install must omit cached bytecode (--no-compile)")
                installed_code.add(str(path.relative_to(site)))
                require(len(installed_code) <= 3000, "installed package inventory bound exceeded")
        require(installed_code == {name for name in compared if name.startswith("graphify/")},
                "installed graphify contains extra/missing wheel payloads")
        validate_origins(candidate, site, compared)
        reader_candidate = self.python("installed reader runtime", CANDIDATE_PROBE, reader=True)
        validate_origins(reader_candidate, site, compared)
        require(reader_candidate["loaded_modules"] == candidate["loaded_modules"]
                and reader_candidate["site"] == candidate["site"]
                and reader_candidate["sys_path"] == candidate["sys_path"],
                "administrator/reader installed candidate origins differ")
        self.candidate, self.reader_candidate = candidate, reader_candidate
        self.report["candidate"] = {**candidate, "reader_probe": reader_candidate, "installed_payloads": compared,
                                    "source_revision_binding": "operator build provenance assertion"}
        self.checks.add("installed_candidate")

    def provision(self):
        # Repeat absence checks immediately before the first exclusive mutation.
        require(not os.path.lexists(self.workspace) and not os.path.lexists(CONFIG),
                "provisioning targets changed after preflight")
        os.mkdir(self.workspace, 0o755)
        self.provisioned = True
        os.chmod(self.workspace, 0o755)
        os.mkdir(self.store, 0o750)
        os.chown(self.store, 0, self.args.reader_gid)
        os.chmod(self.store, 0o750)
        os.mkdir(self.reader, 0o700)
        os.chown(self.reader, self.args.reader_uid, self.args.reader_gid)
        os.chmod(self.reader, 0o700)
        created_parent = False
        if not CONFIG.parent.exists():
            os.mkdir(CONFIG.parent, 0o755)
            os.chmod(CONFIG.parent, 0o755)
            created_parent = True
        protected(CONFIG.parent)
        config = {"schema": "graphify.recovery-authority.v1", "backend": "macos-apfs-v1",
                  "administrator_uid": 0, "reader_uids": [self.args.reader_uid],
                  "reader_gid": self.args.reader_gid, "store_path": str(self.store),
                  "custody": "disposable A14 administrator approval",
                  "failure_domain": "same disposable VM storage"}
        payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self.new_file(CONFIG, payload, 0o640, self.args.reader_gid)
        self.new_file(self.store / ".authority.lock", b"", 0o640, self.args.reader_gid)
        self.report["provisioning"] = {"config": str(CONFIG), "config_parent_created": created_parent,
                                       "config_sha256": sha(CONFIG), "retention": "preserve all; no cleanup"}

    def control_start(self, name, path):
        record = {"name": name, "path": str(path), "before": snapshot(path)}
        self.report.setdefault("negative_controls", []).append(record)
        return record

    def control_injected(self, record, path):
        record["injected"] = snapshot(path)

    def control_restored(self, record, path):
        record["restored"] = snapshot(path)
        # Content, identity, owner, permissions, links and flags must survive.
        # Change timestamps are expected to record the administrator injection.
        stable = lambda records: {name: {key: value for key, value in item.items()
                                        if key not in {"mtime_ns", "ctime_ns"}}
                                  for name, item in records.items()}
        require(stable(record["before"]) == stable(record["restored"]),
                record["name"] + ": negative control was not restored")

    def matrix(self):
        self.python("source_publication", PUBLISH, self.root, self.original, reader=True)
        self.checks.add("source_publication")
        source = snapshot(self.original)
        approved = self.cli("capture", "capture", "--source-id", SOURCE, "--expected-head", "absent",
                            "--root", self.root, "--output", self.original)
        require(approved.get("status") == "approved" and snapshot(self.original) == source,
                "capture result/source preservation failed")
        self.checks.add("capture")
        request, output = self.request("restored")
        preview = self.cli("preview", "preview", "--request", request, reader=True)
        require(preview.get("status") == "preview", "preview status differs")
        require(not output.exists(), "preview created the destination")
        self.checks.add("preview")
        restored = self.cli("restore", "restore", "--request", request, "--preview-sha256",
                            preview["preview_sha256"], reader=True)
        require(restored.get("status") == "complete", "restore did not complete")
        self.checks.add("restore")
        verified = self.reader_operation("snapshot", lambda: self.python(
            "snapshot", VERIFY, self.original, output, reader=True))
        require(verified["output_identity"] == restored["output_identity"], "restore identity differs")
        self.report["restored"] = {"result": restored, "snapshot": verified, "tree": snapshot(output)}
        self.checks.add("snapshot")
        head = self.store / "heads" / (SOURCE + ".json")
        checkpoint = self.store / "checkpoints" / approved["checkpoint_sha256"]
        metadata = checkpoint / "checkpoint.json"
        artifact = checkpoint / "artifacts/graph.json"
        self.reader_operation("direct_writes_denied", lambda: self.python(
            "direct_writes_denied", DENIED_WRITES, CONFIG, head, metadata, artifact,
            self.store / ".authority.lock", self.store / "reader-must-not-create", reader=True))
        self.checks.add("direct_writes_denied")
        for action in ("capture", "revoke"):
            extra = ["--root", self.root, "--output", self.original] if action == "capture" else []
            self.cli("reader_" + action + "_denied", action, "--source-id", SOURCE,
                     "--expected-head", approved["head_sha256"], *extra, reader=True, refused=True, reason="Capture requires UID 0")
            self.checks.add("reader_" + action + "_denied")
        negative, absent = self.request("must-stay-absent")
        valid_negative = self.cli("negative control preview", "preview", "--request", negative, reader=True)
        def refusal(name, reason):
            self.cli(name, "restore", "--request", negative, "--preview-sha256",
                     valid_negative["preview_sha256"], reader=True, refused=True, reason=reason)
            require(not os.path.lexists(absent), name + ": refused restore created destination")
            self.checks.add(name)
        # Mode/ACL changes target only this newly created disposable config.
        control = self.control_start("unsafe config mode", CONFIG)
        CONFIG.chmod(0o660)
        self.control_injected(control, CONFIG)
        refusal("unsafe_mode_denied", "Unsafe authority owner, mode, type, links or ACL")
        CONFIG.chmod(0o640)
        self.control_restored(control, CONFIG)
        control = self.control_start("unsafe config ACL", CONFIG)
        self.run("add unsafe config ACL", ["/bin/chmod", "+a#", "0",
                                          self.reader_acl, str(CONFIG)])
        self.control_injected(control, CONFIG)
        opened = self.reader_operation("unsafe ACL write-open control", lambda: self.python(
            "unsafe ACL write-open control", WRITE_OPEN_CONTROL, CONFIG, reader=True))
        config_info = CONFIG.stat()
        require(opened.get("status") == "write_open_permitted"
                and opened.get("device") == config_info.st_dev and opened.get("inode") == config_info.st_ino
                and opened.get("uid") == opened.get("euid") == self.args.reader_uid
                and opened.get("gid") == opened.get("egid") == self.args.reader_gid,
                "injected ACL did not grant reader write-open on the exact disposable config")
        self.report["acl_write_open_control"] = opened
        refusal("unsafe_acl_denied", "Unsafe authority owner, mode, type, links or ACL")
        self.run("remove injected config ACL", ["/bin/chmod", "-a#", "0", str(CONFIG)])
        protected(CONFIG)
        self.control_restored(control, CONFIG)
        control = self.control_start("malformed head", head)
        original_head = head.read_bytes()
        head.write_bytes(b"{\"schema\":\"malformed\"}")
        self.control_injected(control, head)
        refusal("malformed_head_denied", "malformed protected checkpoint head")
        head.write_bytes(original_head)
        self.control_restored(control, head)
        control = self.control_start("tampered artifact", artifact)
        original_artifact = artifact.read_bytes()
        artifact.write_bytes(b"tampered A14 graph artifact")
        self.control_injected(control, artifact)
        refusal("tampered_artifact_denied", "checkpoint artifact changed")
        artifact.write_bytes(original_artifact)
        self.control_restored(control, artifact)
        # Re-preview this exact request before revocation so request mismatch
        # cannot explain the final stale-preview refusal.
        stale = self.cli("preview before revoke", "preview", "--request", negative, reader=True)
        revoked = self.cli("revoke", "revoke", "--source-id", SOURCE,
                           "--expected-head", approved["head_sha256"])
        require(revoked.get("status") == "revoked", "revocation did not complete")
        self.cli("stale_preview_denied", "restore", "--request", negative,
                 "--preview-sha256", stale["preview_sha256"], reader=True, refused=True, reason="no enabled approved checkpoint")
        require(not os.path.lexists(absent), "stale preview created destination")
        self.checks.add("stale_preview_denied")
        for reader in (False, True):
            final = self.python("final reader origins" if reader else "final administrator origins",
                                CANDIDATE_PROBE, reader=reader)
            require(final == (self.reader_candidate if reader else self.candidate),
                    "installed runtime changed during native commands")
        require(self.checks == REQUIRED_CHECKS, "required A14 matrix incomplete")

    def finish(self, error=None):
        self.report.update(status="failed" if error else "passed", checks=sorted(self.checks),
                           finished_ns=time.time_ns())
        if error:
            self.report["error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
        if self.provisioned:
            path = self.workspace / "a14-report.json"
            # Exactly one report is created in our exclusively allocated trial.
            self.new_file(path, json.dumps(self.report, sort_keys=True, default=str).encode(), 0o600, 0)
        print(json.dumps({"status": self.report["status"], "workspace": str(self.workspace),
                          "report": str(self.workspace / "a14-report.json") if self.provisioned else None,
                          "error": self.report.get("error"),
                          "evidence": self.report if not self.provisioned else None},
                         sort_keys=True, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--reader-uid", type=int, required=True)
    parser.add_argument("--reader-gid", type=int, required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--expected-wheel-sha256", required=True)
    parser.add_argument("--expected-source-revision", required=True)
    args = parser.parse_args(argv)
    trial = Trial(args)
    try:
        trial.preflight()
        trial.provision()
        trial.matrix()
    except Exception as exc:
        try:
            trial.finish(exc)
        except Exception as reporting_error:
            print(json.dumps({"status": "failed", "error": str(exc)[:4096],
                              "reporting_error": str(reporting_error)[:1024]}), file=sys.stderr)
        return 1
    trial.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
