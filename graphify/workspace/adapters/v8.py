"""The sole workspace bridge to v8 detection, extraction, serialization and query.

Observations are repeated bounded reads, not atomic snapshots. Git discovery
selects an explicit common-directory root; corpus metadata cannot widen it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import time

from graphify.source_io import SourceIO, SourceError, SourceChanged, SourceUnsupported
from graphify.workspace.contracts import (
    DETECTOR_ID, MAX_TOTAL_BYTES, InputManifest, ContractError, input_label,
)
from graphify.workspace.identity import (
    discover_source, _git, SourceDiscoveryError, SourceDiscoveryTimeout,
)
from graphify.workspace.lifecycle_observation import SourceObservation as LifecycleObservation
from .base import QueryRejected, QueryRequest, SourceObservation, StructuralBuild


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
                         write_guard=None):
        from contextlib import nullcontext
        from graphify.extract import extract
        from graphify.build import build_from_json
        from graphify.export import write_json

        if type(initial_detection) is not InputManifest or initial_detection.to_dict()["phase"] != "detection":
            raise ContractError("validated initial detection required")
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
            graph = build_from_json(extraction, directed=True, root=source_root)
            stream = io.StringIO()
            write_json(graph, {}, stream, built_at_commit=source.head_commit)
            raw = stream.getvalue().encode("utf-8")
        if len(raw) > MAX_TOTAL_BYTES:
            raise SourceUnsupported("structural graph exceeds payload bound")
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
            for name, data in (("graph.json", raw), ("input-manifest.json", consumed.canonical)):
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=payload_fd)
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
        return StructuralBuild(consumed, hashlib.sha256(raw).hexdigest(),
                               graph.number_of_nodes(), graph.number_of_edges())

    def query_structural(self, payload_fd, request):
        if type(request) is not QueryRequest:
            raise QueryRejected("validated query request required")
        request = replace(request)

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
