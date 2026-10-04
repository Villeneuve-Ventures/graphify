"""Bounded disposable computation for read-only workspace operations."""
from __future__ import annotations

import json
import os
import selectors
import signal
import site
import stat
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

from .persistence import LockTimeout, require_before_deadline


class ReadOnlyFailure(RuntimeError):
    pass


# Set only by the public command worker, which is its process-group leader.
# Nested helpers must remain in that group so the public supervisor can reap
# the entire computation even if it has to terminate a blocked command worker.
_SUPERVISED_GROUP = None


# -S prevents site/.pth execution before the audit hook. The worker receives
# paths from the loaded package and interpreter installation, not caller cwd or
# arbitrary sys.path entries. Runtime-owned operations revalidate the package.
_CHILD_BOOTSTRAP = r'''
import sys, os, stat
git_executable = None
def audit(event, args):
    mutation = event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod',
                        'os.link', 'os.symlink', 'os.truncate', 'os.utime'}
    if event == 'open':
        path, _, flags = args
        mutation = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if type(path) is int and stat.S_ISFIFO(os.fstat(path).st_mode):
            mutation = False
        if isinstance(path, (str, bytes)) and 'jieba.cache' in os.fsdecode(path):
            raise RuntimeError('ambient tokenizer cache access')
    if mutation or event in {'socket.connect', 'socket.bind', 'os.system', 'os.fork', 'os.forkpty'}:
        raise RuntimeError('read-only computation attempted a side effect')
    if event == 'subprocess.Popen':
        command = args[1]
        # Source discovery and existing bytecode validation are the only
        # subprocess owners. Their calls use sanitized environments and pipes.
        if not (isinstance(command, (list, tuple)) and
                ((git_executable is not None and args[0] == git_executable and
                  len(command) >= 2 and command[0] == git_executable and command[1] in
                  {'rev-parse', 'config', 'remote', 'rev-list', 'check-ref-format'}) or
                 (args[0] == sys.executable and
                  command[:5] == [sys.executable, '-I', '-S', '-B', '-c']))):
            raise RuntimeError('unexpected read-only helper')
sys.addaudithook(audit)
import json
paths, cache_prefix, git_executable = json.loads(sys.argv[1])
sys.path[:] = paths
sys.pycache_prefix = cache_prefix
if git_executable is not None:
    import graphify.workspace.identity as identity
    identity._PINNED_GIT_EXECUTABLE = git_executable
operation = sys.argv[2]
sys.argv = [sys.argv[0], *sys.argv[3:]]
exec(operation, {'__name__': '__main__'})
'''


def _worker_import_paths(deadline_ns):
    """Select package, standard library and installed dependency roots."""
    package = sys.modules["graphify"]
    package_root = Path(package.__file__).resolve().parent.parent
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    ziplib = Path(sys.base_prefix) / "lib" / (
        f"python{sys.version_info.major}{sys.version_info.minor}.zip")
    paths = [str(package_root), str(ziplib), str(stdlib), str(stdlib / "lib-dynload")]

    # Only installation-owned .pth path records supply shared dependencies.
    # Import statements in .pth files are never executed in the worker.
    for site_root in map(Path, site.getsitepackages()):
        require_before_deadline(deadline_ns, 'query deadline expired')
        try:
            with os.scandir(site_root) as entries:
                pth_paths = []
                for entry in entries:
                    require_before_deadline(deadline_ns, 'query deadline expired')
                    if entry.name.endswith(".pth"):
                        pth_paths.append(site_root / entry.name)
                        if len(pth_paths) > 128:
                            raise ReadOnlyFailure('too many installation path files')
            resolved_site = site_root.resolve(strict=True)
        except FileNotFoundError:
            continue  # Interpreter prefixes can include uncreated site directories.
        except OSError as exc:
            raise ReadOnlyFailure('cannot read installation site directory') from exc
        paths.append(str(resolved_site))
        for pth in sorted(pth_paths):
            require_before_deadline(deadline_ns, 'query deadline expired')
            try:
                fd = os.open(pth, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                        raise ReadOnlyFailure('unsafe installation path file')
                    content = os.read(fd, 65537)
                    if len(content) > 65536:
                        raise ReadOnlyFailure('unsafe installation path file')
                finally:
                    os.close(fd)
                lines = content.decode('utf-8').splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                raise ReadOnlyFailure('unsafe installation path file') from exc
            require_before_deadline(deadline_ns, 'query deadline expired')
            for line in lines:
                entry = line.strip()
                if not entry or entry.startswith("#") or entry.startswith("import "):
                    continue
                dependency = Path(entry)
                if (dependency.is_absolute() and dependency.name in
                        {"site-packages", "dist-packages"} and dependency.is_dir()):
                    paths.append(str(dependency.resolve()))
    return list(dict.fromkeys(paths))


def run_readonly(code, request, *, deadline_ns, max_output_bytes,
                 max_input_bytes, arguments=(), pass_fds=(), git_source_root=None):
    """Retain caller locks until a private process group is killed and reaped.

    Only explicit descriptors cross the boundary. Helpers remain in the child's
    new process group so expiry also terminates Git/bytecode comparisons. This
    audit boundary is not an OS sandbox against hostile installed code.
    """
    def remaining():
        require_before_deadline(deadline_ns, 'query deadline expired')
        return max(0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)

    remaining()
    if len(request) > max_input_bytes:
        raise ReadOnlyFailure('read-only request exceeds byte limit')
    paths = _worker_import_paths(deadline_ns)
    git_executable = None
    helper_path = os.defpath
    if git_source_root is not None:
        from .identity import (
            SourceDiscoveryError, SourceDiscoveryTimeout, _git_executable, _git_routing,
            _git_search_path,
        )
        try:
            source_root = Path(git_source_root).resolve(strict=True)
            _git_dir, common = _git_routing(source_root, deadline_ns=deadline_ns)
            git_executable = _git_executable(source_root, deadline_ns=deadline_ns,
                                             git_common_dir=common)
            helper_path = _git_search_path(source_root, common)
        except SourceDiscoveryTimeout:
            raise LockTimeout('query deadline expired') from None
        except (SourceDiscoveryError, OSError) as exc:
            raise ReadOnlyFailure('cannot select read-only Git executable') from exc
    startup = json.dumps([paths, sys.pycache_prefix, git_executable])
    command = [sys.executable, '-I', '-S', '-B', '-c', _CHILD_BOOTSTRAP,
               startup, code, *map(str, arguments)]
    supervised = _SUPERVISED_GROUP == os.getpgrp()
    with subprocess.Popen(command, pass_fds=pass_fds, close_fds=True,
                          start_new_session=not supervised, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env={'PATH': helper_path,
                               'LANG': 'C', 'LC_ALL': 'C'}) as process:
        output = bytearray()
        total = sent = 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream, event in ((process.stdin, selectors.EVENT_WRITE),
                                      (process.stdout, selectors.EVENT_READ),
                                      (process.stderr, selectors.EVENT_READ)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event)
                while selector.get_map():
                    events = selector.select(remaining())
                    remaining()
                    for key, _ in events:
                        if key.fileobj is process.stdin:
                            try:
                                sent += os.write(key.fd, request[sent:])
                            except BrokenPipeError:
                                sent = len(request)
                            if sent == len(request):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        chunk = os.read(key.fd, min(65536, max_output_bytes - total + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > max_output_bytes:
                            raise ReadOnlyFailure('read-only output exceeds byte limit')
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            process.wait(timeout=remaining())
            remaining()
        except subprocess.TimeoutExpired:
            raise LockTimeout('query deadline expired') from None
        finally:
            # Kill the whole owned group even if its leader failed first. No
            # helper inherits a workspace/generation lock or survives expiry.
            try:
                if supervised:
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The process group has already exited; continue cleanup.
                pass
            process.wait()
        if process.returncode != 0:
            raise ReadOnlyFailure('read-only computation failed')
        return bytes(output)
