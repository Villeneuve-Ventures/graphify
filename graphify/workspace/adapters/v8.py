"""The sole workspace bridge to v8 detection, extraction, serialization and query.

Observations are repeated bounded reads, not atomic snapshots. Git discovery
selects an explicit common-directory root; corpus metadata cannot widen it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

from graphify.source_io import SourceIO, SourceError, SourceChanged, SourceUnsupported
from graphify.workspace.contracts import (
    DETECTOR_ID, MAX_TOTAL_BYTES, InputManifest, ContractError, input_label, integer,
)
from graphify.workspace.identity import (
    discover_source, _git, SourceDiscoveryError, SourceDiscoveryTimeout,
)
from graphify.workspace.lifecycle_observation import SourceObservation as LifecycleObservation
from graphify.workspace.persistence import LockTimeout, require_before_deadline
from .base import PayloadBudgetExceeded, QueryRejected, QueryRequest, SourceObservation, StructuralBuild


def _deadline(deadline_ns):
    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        raise SourceError("source observation deadline expired")


def _absolute(root):
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise SourceUnsupported("explicit absolute source root required")
    return root


def _optional(inputs, path):
    return None if inputs.probe(path) is None else inputs.read_bytes(path)


def _git_inputs(inputs, source, *, deadline_ns=None):
    """Bind local routing/ref files, refusing Git modes with unaccounted readers.

    Only ordinary files-based repositories and standard linked worktrees are
    supported. Git's object/history validation stays with source discovery.
    """
    root = inputs.root
    common = inputs.roots["git"]
    marker = inputs.probe(root / ".git")
    if marker is None:
        raise SourceUnsupported("missing Git routing")
    if stat.S_ISDIR(marker.st_mode):
        git_dir = root / ".git"
        if git_dir != common:
            raise SourceUnsupported("Git common directory routing mismatch")
    else:
        raw = inputs.read_bytes(root / ".git").decode("utf-8").strip()
        if not raw.startswith("gitdir: "):
            raise SourceUnsupported("unsupported Git routing")
        git_dir = Path(os.path.abspath(root / raw[8:]))
        expected = common / "worktrees" / source.registry_source["worktree_id"]
        if git_dir != expected:
            raise SourceUnsupported("unsupported linked Git routing")
        cd = inputs.read_bytes(git_dir / "commondir").decode("utf-8").strip()
        if Path(os.path.abspath(git_dir / cd)) != common:
            raise SourceUnsupported("Git common directory changed")
        back = inputs.read_bytes(git_dir / "gitdir").decode("utf-8").strip()
        if Path(back) != root / ".git":
            raise SourceUnsupported("Git worktree backlink changed")
    for path in (common / "config", git_dir / "config.worktree"):
        config = _optional(inputs, path)
        if config is not None and re.search(
            rb'(?im)^\s*\[\s*(?:include|includeif|extensions)(?:\s|\])', config,
        ):
            raise SourceUnsupported("Git includes/extensions need unsupported input authority")
    for name in ("objects/info/alternates", "objects/info/http-alternates", "shallow", "info/grafts"):
        if inputs.probe(common / name) is not None:
            raise SourceUnsupported("unsupported Git object/history routing")
    packed = _optional(inputs, common / "packed-refs")
    head = inputs.read_bytes(git_dir / "HEAD").removesuffix(b"\n")
    seen = set()
    while head.startswith(b"ref: "):
        ref_bytes = head[5:]
        ref = os.fsdecode(ref_bytes)
        if (not ref.startswith("refs/")
                or ref in seen or len(seen) >= 8):
            raise SourceUnsupported("unsupported Git reference")
        try:
            _git(root, "check-ref-format", ref, deadline_ns=deadline_ns)
        except SourceDiscoveryTimeout:
            raise
        except SourceDiscoveryError as exc:
            raise SourceUnsupported("unsupported Git reference") from exc
        # Git refs are bytes, but retained input paths use the S2 canonical UTF-8
        # label contract. Do not replace undecodable bytes or invent path aliases.
        try:
            input_label("git:" + ref, {"git"})
        except ContractError as exc:
            raise SourceUnsupported("Git reference requires a canonical UTF-8 input label") from exc
        seen.add(ref)
        raw = _optional(inputs, common / ref)
        if raw is None:
            rows = [] if packed is None else packed.split(b"\n")
            matches = [line.split(b" ")[0] for line in rows if line.endswith(b" " + ref_bytes)]
            if len(matches) != 1:
                raise SourceUnsupported("Git reference is unavailable")
            head = matches[0]
        else:
            head = raw.removesuffix(b"\n")
    if head != source.head_commit.encode("ascii"):
        raise SourceChanged("Git HEAD changed during observation")
    policy = inputs.read_bytes(root / ".graphify" / "workspace.toml")
    if hashlib.sha256(policy).hexdigest() != source.config_sha256:
        raise SourceChanged("workspace policy changed during observation")
    return head


def _replay(inputs, manifest):
    value = manifest.to_dict()
    if value["roots"] != sorted(inputs.roots):
        raise SourceUnsupported("input roots differ from selected source authority")
    for record in value["evidence"]:
        label = record["path"]
        root, relative = label.split(":", 1) if ":" in label else ("source", label)
        path = inputs.roots[root] / relative
        operation = record["operation"]
        if operation == "read":
            inputs.read_bytes(path)
        elif operation == "probe":
            inputs.probe(path)
        elif operation == "list":
            inputs.listdir(path)
        # Directory records are reproduced by the rooted operations themselves.
    replayed = InputManifest.from_engine(
        inputs, phase="consumed", code_inputs=value["code_inputs"], outcomes=value["outcomes"],
    )
    if replayed != manifest:
        raise SourceChanged("consumed input evidence changed")
    return replayed


def read_payload_file(payload_fd, name, *, max_bytes=MAX_TOTAL_BYTES):
    """Read one bounded singular file through an already protected descriptor."""
    if name not in {"graph.json", "input-manifest.json"}:
        raise QueryRejected("unsupported payload member")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=payload_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in {0o600, 0o400}
                or before.st_size > max_bytes):
            raise QueryRejected("unsafe or oversized payload member")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(max_bytes + 1)
        after = os.stat(name, dir_fd=payload_fd, follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (len(raw) != before.st_size or len(raw) > max_bytes
                or identity(before) != identity(os.fstat(fd)) or identity(before) != identity(after)):
            raise QueryRejected("payload member changed while reading")
        return raw
    finally:
        os.close(fd)


class _BoundedGraphBuffer(io.BytesIO):
    """Accept the existing text serializer without buffering beyond its budget."""

    def __init__(self, max_bytes, manifest):
        super().__init__()
        self.max_bytes = max_bytes
        self.manifest = manifest

    def _exceeded(self, size):
        raise PayloadBudgetExceeded(len(self.manifest.canonical) + self.tell() + size,
                                    self.manifest)

    def write(self, text):
        remaining = self.max_bytes - self.tell()
        if len(text) > remaining:
            self._exceeded(len(text))
        raw = text.encode("utf-8")
        if len(raw) > remaining:
            self._exceeded(len(raw))
        super().write(raw)
        return len(text)


# Only the pinned payload descriptor is inherited, never workspace/GC lock FDs.
_QUERY_CHILD_CODE = "from graphify.workspace.adapters.v8 import _query_child; _query_child()"


def _query_child():
    """Run the existing engine with bytecode, durable writes and network disabled."""
    def audit(event, args):
        mutation = event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod",
                            "os.link", "os.symlink", "os.truncate", "os.utime"}
        if event == "open":
            path, _, flags = args
            mutation = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if isinstance(path, (str, bytes)) and "jieba.cache" in os.fsdecode(path):
                raise QueryRejected("ambient tokenizer cache access")
        if mutation or event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system"}:
            raise QueryRejected("query computation attempted a durable or external side effect")
    sys.addaudithook(audit)
    raw = sys.stdin.buffer.read(65_537)
    if len(raw) > 65_536:
        raise QueryRejected("query computation request exceeds byte limit")
    request = QueryRequest(**json.loads(raw))
    text = V8Adapter().query_structural(int(sys.argv[1]), request)
    sys.stdout.buffer.write(text.encode("utf-8"))


def _query_with_deadline(payload_fd, request, deadline_ns):
    """Kill and reap computation at expiry, including native JSON/tokenizer work.

    Loop callbacks alone cannot interrupt JSON decoding or dependency code. The
    parent retains all locks until this disposable child exits, and never exposes
    its buffered output before the caller's final freshness checks.
    """
    def remaining():
        require_before_deadline(deadline_ns, "query deadline expired")
        return max(0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)

    remaining()
    request_bytes = json.dumps(asdict(request), ensure_ascii=False).encode("utf-8")
    command = [sys.executable, "-I", "-B", "-c", _QUERY_CHILD_CODE, str(payload_fd)]
    with subprocess.Popen(command, pass_fds=(payload_fd,), close_fds=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
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
                                sent += os.write(key.fd, request_bytes[sent:])
                            except BrokenPipeError:
                                sent = len(request_bytes)
                            if sent == len(request_bytes):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        chunk = os.read(key.fd, min(65536, MAX_TOTAL_BYTES - total + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_TOTAL_BYTES:
                            raise QueryRejected("query computation output exceeds byte limit")
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            process.wait(timeout=remaining())
            remaining()
        except subprocess.TimeoutExpired:
            raise LockTimeout("query deadline expired") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode != 0:
            # Tracebacks may contain source content; they are never query output.
            raise QueryRejected("invalid structural graph or query computation failed")
        try:
            return output.decode("utf-8")
        except UnicodeDecodeError:
            raise QueryRejected("query computation returned invalid UTF-8") from None


class V8Adapter:
    adapter_id = "graphify-v8/structural-v2"
    detector_id = DETECTOR_ID

    @contextmanager
    def _inputs(self, source_root, deadline_ns=None):
        from graphify.detect import detect

        root = _absolute(source_root)
        _deadline(deadline_ns)
        # Pin the spelling before discovery, whose ordinary API resolves paths.
        with SourceIO(root):
            source = discover_source(root, deadline_ns=deadline_ns)
        common = Path(source.registry_source["git_common_dir"])
        with SourceIO(root, extra_roots={"git": common}) as inputs:
            _git_inputs(inputs, source, deadline_ns=deadline_ns)
            detection = detect(root, source_io=inputs, quiet=True)
            if detection.get("walk_errors"):
                raise SourceUnsupported("incomplete source enumeration")
            code = detection["files"]["code"]
            initial = InputManifest.from_engine(inputs, phase="detection", code_inputs=code)
            yield inputs, initial, source, code
            _deadline(deadline_ns)
            _git_inputs(inputs, source, deadline_ns=deadline_ns)
            if discover_source(root, deadline_ns=deadline_ns) != source:
                raise SourceChanged("source identity changed during observation")

    def _observe(self, source_root, *, input_manifest=None, max_inventory_passes=6, deadline_ns=None):
        if type(max_inventory_passes) is not int or not 2 <= max_inventory_passes <= 6:
            raise ContractError("two to six inventory passes required")
        if input_manifest is not None and (type(input_manifest) is not InputManifest
                                           or not input_manifest.complete):
            raise ContractError("complete validated consumed manifest required")
        previous = None
        for _ in range(max_inventory_passes):
            with self._inputs(source_root, deadline_ns) as (inputs, initial, source, _code):
                consumed = None
                if input_manifest is not None:
                    # Replay is a separate bounded observation. Detection already
                    # consumed the source bytes; sharing its budget charges twice.
                    with SourceIO(inputs.root,
                                  extra_roots={k: v for k, v in inputs.roots.items() if k != "source"},
                                  max_file_bytes=inputs.max_file_bytes,
                                  max_total_bytes=inputs.max_total_bytes,
                                  max_entries=inputs.max_entries) as replay:
                        consumed = _replay(replay, input_manifest)
                current = (initial, consumed, source.head_commit, source.config_sha256)
            if current == previous:
                return SourceObservation(initial, consumed, 2), source
            previous = current
        raise SourceChanged("source lacks two agreeing complete observations")

    def observe(self, source_root, **kwargs):
        return self._observe(source_root, **kwargs)[0]

    def observe_lifecycle(self, source_root, **kwargs):
        structural, source = self._observe(source_root, **kwargs)
        return LifecycleObservation(source.head_commit, source.config_sha256, structural)

    def build_structural(self, source_root, *, payload_fd, scratch_fd, initial_detection,
                         write_guard=None, max_payload_bytes=MAX_TOTAL_BYTES):
        from contextlib import nullcontext
        from graphify.extract import extract
        from graphify.build import build_from_json
        from graphify.export import write_json

        if type(initial_detection) is not InputManifest or initial_detection.to_dict()["phase"] != "detection":
            raise ContractError("validated initial detection required")
        integer(max_payload_bytes, minimum=1)
        max_payload_bytes = min(max_payload_bytes, MAX_TOTAL_BYTES)
        with self._inputs(source_root) as (inputs, initial, source, code):
            if initial != initial_detection:
                raise SourceChanged("initial detection changed before build")
            # Detection and extraction are separately bounded passes. Retain both
            # sets of evidence, rejecting disagreements instead of overwriting them.
            with SourceIO(inputs.root,
                          extra_roots={k: v for k, v in inputs.roots.items() if k != "source"},
                          max_file_bytes=inputs.max_file_bytes,
                          max_total_bytes=inputs.max_total_bytes,
                          max_entries=inputs.max_entries) as extraction_inputs:
                extraction = extract(code, source_io=extraction_inputs, source_root=source_root,
                                     quiet=True, ambient_output=False)
                value = InputManifest.from_engine(extraction_inputs, phase="consumed", code_inputs=code,
                                                  outcomes=extraction["outcomes"]).to_dict()
            evidence = {(e["operation"], e["path"]): e for e in initial.to_dict()["evidence"]}
            for record in value["evidence"]:
                key = record["operation"], record["path"]
                if key in evidence and evidence[key] != record:
                    raise SourceChanged("source evidence changed between detection and extraction")
                evidence[key] = record
            value["evidence"] = [evidence[key] for key in sorted(evidence)]
            consumed = InputManifest.from_mapping(value)
            SourceObservation(initial, consumed, 2)
            manifest_raw = consumed.canonical
            graph_budget = max_payload_bytes - len(manifest_raw)
            if graph_budget <= 0:
                raise PayloadBudgetExceeded(len(manifest_raw) + 1, consumed)
            graph = build_from_json(extraction, directed=True, root=source_root)
            with _BoundedGraphBuffer(graph_budget, consumed) as stream:
                write_json(graph, {}, stream, built_at_commit=source.head_commit)
                raw = stream.getvalue()
        # The lifecycle supplies a fence/descriptor binding guard. Low-level
        # callers own their descriptors; the adapter never allocates authority.
        with write_guard() if write_guard is not None else nullcontext():
            for fd in (payload_fd, scratch_fd):
                info = os.fstat(fd)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise SourceUnsupported("owned private output descriptors required")
            if os.listdir(payload_fd):
                raise SourceUnsupported("structural payload destination is not empty")
            for name, data in (("graph.json", raw), ("input-manifest.json", manifest_raw)):
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=payload_fd)
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
        return StructuralBuild(consumed, hashlib.sha256(raw).hexdigest(),
                               graph.number_of_nodes(), graph.number_of_edges())

    def query_structural(self, payload_fd, request, *, deadline_ns=None):
        if type(request) is not QueryRequest:
            raise QueryRejected("validated query request required")
        request = replace(request)
        if deadline_ns is not None:
            if type(deadline_ns) is not int or deadline_ns <= 0:
                raise QueryRejected("positive monotonic deadline_ns required")
            return _query_with_deadline(payload_fd, request, deadline_ns)

        from networkx.readwrite import json_graph
        from graphify.serve import _query_graph_text, memory_query_segmenter

        try:
            data = json.loads(read_payload_file(payload_fd, "graph.json"))
            if data.get("directed") is not True or data.get("multigraph") is not False:
                raise QueryRejected("directed simple structural graph required")
            graph = json_graph.node_link_graph(data, edges="links")
            return _query_graph_text(graph, request.question, mode=request.mode,
                                     depth=request.depth, token_budget=request.token_budget,
                                     context_filters=list(request.context_filters),
                                     segmenter=memory_query_segmenter())
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryRejected("invalid structural graph or traversal") from exc
