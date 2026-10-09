"""Read-only, committed-tree portable graph contract, version 1.

The envelope proves byte integrity and correspondence to the declared source
projection. It grants no local publication authority or extraction trust.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import unicodedata
from types import MappingProxyType
from typing import Any, NoReturn

from graphify import transaction as tx

PORTABLE_FILE = ".graphify_portable.json"
PORTABLE_MARKER = {"schema": 2, "protocol_epoch": 2, "state": "portable_complete"}
SOURCE_SELECTOR = "tracked-python-v1"
EXTRACTION_CONTRACT = "python-ast-v1"
_FILES = frozenset({PORTABLE_FILE, "graph.json", "manifest.json"})
_MAX_METADATA = 8 * 1024 * 1024
_MAX_BLOB = 8 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024
_MAX_SOURCES = 4096
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ENVELOPE_FIELDS = frozenset({
    "format", "version", "output", "graph_format", "manifest_format",
    "source_selector", "source_projection_digest", "extraction_contract",
    "minimum_reader", "artifacts", "content_id",
})


class PortableGraphError(tx.PendingTransactionError):
    """The bundle cannot be admitted without weakening portable authority."""


def _fail(reason: str) -> NoReturn:
    raise PortableGraphError(reason)


def _json_values(value: Any, depth: int = 0) -> None:
    if depth > 64:
        _fail("portable JSON nesting exceeds bounds")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            _fail("portable JSON integer exceeds bounds")
    elif type(value) is float:
        if not math.isfinite(value):
            _fail("portable JSON contains a non-finite number")
    elif type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            _fail("portable JSON contains a surrogate")
    elif type(value) is list or type(value) is tuple:
        for child in value:
            _json_values(child, depth + 1)
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                _fail("portable JSON object keys must be strings")
            _json_values(key, depth + 1)
            _json_values(child, depth + 1)
    else:
        _fail("portable JSON contains an unsupported type")


def canonical_json(value: Any) -> bytes:
    """Compact sorted-key Python JSON, UTF-8, no BOM or trailing newline."""
    _json_values(value)
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise PortableGraphError("portable JSON cannot be serialized") from exc


def parse_json(payload: bytes, *, canonical: bool = False) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("portable JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail("portable JSON non-finite number"))
        _json_values(value)
        if canonical and canonical_json(value) != payload:
            _fail("portable metadata is not canonical JSON")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PortableGraphError("portable JSON is malformed") from exc


def _path(value: Any) -> str:
    if type(value) is not str:
        _fail("portable path must be a string")
    try:
        tx._validated_relative_name(value)
    except tx.PendingTransactionError as exc:
        raise PortableGraphError(str(exc)) from exc
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or ":" in value:
        _fail("portable path contains unsafe characters")
    for part in value.split("/"):
        if part.endswith((" ", ".")) or part.casefold() == ".git":
            _fail("portable path has an unsafe component")
        stem = part.split(".", 1)[0].casefold()
        if stem in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(?:com|lpt)[1-9]", stem):
            _fail("portable path is a reserved device name")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("portable path is not UTF-8")
    return value


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _alias_key(path: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).casefold())


def _validate_tree_inventory(entries: Mapping[str, tuple[str, str]], output: str) -> None:
    """Apply the same tree-size and output-parent admission on both sides."""
    _path(output)
    parts = output.split("/")
    prefixes = {_alias_key("/".join(parts[:depth])): "/".join(parts[:depth])
                for depth in range(1, len(parts) + 1)}
    size = 0
    for path, (mode, oid) in entries.items():
        kind = "commit" if mode == "160000" else "blob"
        size += len(f"{mode} {kind} {oid}\t{path}\0".encode("utf-8"))
        if size > _MAX_METADATA:
            _fail("portable tree inventory exceeds reader bounds")
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            original = prefixes.get(_alias_key(prefix))
            if original is not None and (prefix != original or depth == len(parts)):
                _fail("repository path collides with a portable output directory")


def _source_records(sources: Sequence[Mapping[str, Any]], output: str) -> tuple[dict[str, str], ...]:
    """Retain exact UTF-8 paths; reject case/Unicode aliases, including parents."""
    _path(output)
    if len(sources) > _MAX_SOURCES:
        _fail("portable source count exceeds bounds")
    result: list[dict[str, str]] = []
    for record in sources:
        if not isinstance(record, Mapping) or set(record) != {"path", "mode", "sha256"}:
            _fail("portable source record has unsupported fields")
        path = _path(record["path"])
        if not path.endswith(".py") or path == output or path.startswith(output + "/"):
            _fail("portable source record is outside the source selector")
        if type(record["mode"]) is not str or record["mode"] not in {"100644", "100755"}:
            _fail("portable source mode is unsupported")
        if type(record["sha256"]) is not str or not _HEX.fullmatch(record["sha256"]):
            _fail("portable source digest is malformed")
        result.append(dict(record))
    paths = [r["path"] for r in result]
    if len(set(paths)) != len(paths):
        _fail("portable source paths collide")
    prefixes: dict[str, str] = {}
    output_prefixes: set[str] = set()
    for path in [output, *paths]:
        parts = path.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            # Normalization is only an alias-detection key. Source records and
            # byte sorting retain the original, unnormalized Git path.
            key = _alias_key(prefix)
            if path == output:
                output_prefixes.add(key)
            elif depth == len(parts) and key in output_prefixes:
                _fail("portable source file collides with an output directory")
            previous = prefixes.setdefault(key, prefix)
            if previous != prefix:
                _fail("portable source paths collide by case or Unicode normalization")
    if paths != sorted(paths, key=lambda p: p.encode("utf-8")):
        _fail("portable source records are not sorted")
    return tuple(result)


def source_records_from_blobs(
    entries: Mapping[str, tuple[str, bytes]], output: str,
) -> tuple[dict[str, str], ...]:
    """Select the complete tracked Python projection from immutable Git blobs."""
    _path(output)
    records: list[dict[str, str]] = []
    total = 0
    for path, (mode, payload) in entries.items():
        if path == output or path.startswith(output + "/"):
            continue
        if mode == "160000":
            _fail("portable source selector does not support submodules")
        if not path.endswith(".py"):
            continue
        _path(path)
        if mode not in {"100644", "100755"}:
            _fail("portable source selector requires regular Python files")
        total += len(payload)
        if len(payload) > _MAX_BLOB or total > _MAX_TOTAL:
            _fail("portable source bytes exceed bounds")
        records.append({"path": path, "mode": mode, "sha256": _digest(payload)})
    records.sort(key=lambda r: r["path"].encode("utf-8"))
    return _source_records(records, output)


def _git(root: Path, *args: str, limit: int | None = None) -> bytes:
    """Read local Git objects with a bounded pipe; never fetch or replace objects."""
    from graphify._git_io import GitReadError, git_stdout

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")
    command = ["git", "--no-replace-objects", "-C", str(root), "-c",
               "core.fsmonitor=false", *args]
    try:
        return b"".join(git_stdout(command, env, _MAX_METADATA if limit is None else limit))
    except GitReadError as exc:
        raise PortableGraphError(f"portable {exc}") from exc


def _tree_entries(root: Path, revision: str) -> tuple[str, dict[str, tuple[str, str]]]:
    if os.name != "posix":
        _fail("portable Git reader requires the qualified POSIX host profile")
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip())
        if top != root:
            _fail("portable reader requires the repository root")
        commit = _git(root, "rev-parse", "--verify", "--end-of-options",
                      revision + "^{commit}").decode("ascii").strip()
        raw = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
        entries: dict[str, tuple[str, str]] = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            header, path_bytes = item.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split(" ")
            path = path_bytes.decode("utf-8")
            if path in entries or kind not in {"blob", "commit"}:
                _fail("portable Git tree is malformed")
            entries[path] = (mode, oid)
        return commit, entries
    except (ValueError, UnicodeError, OSError, subprocess.TimeoutExpired) as exc:
        raise PortableGraphError("portable Git tree cannot be inspected") from exc


def _records_for_entries(root: Path, entries: Mapping[str, tuple[str, str]], output: str
                         ) -> tuple[dict[str, str], ...]:
    _validate_tree_inventory(entries, output)
    selected: dict[str, tuple[str, bytes]] = {}
    total = 0
    for path, (mode, oid) in entries.items():
        if path == output or path.startswith(output + "/"):
            continue
        if mode == "160000":
            _fail("portable source selector does not support submodules")
        if path.endswith(".py"):
            _path(path)
            if mode not in {"100644", "100755"}:
                _fail("portable source selector requires regular Python files")
            if len(selected) >= _MAX_SOURCES:
                _fail("portable source count exceeds bounds")
            payload = _git(root, "cat-file", "blob", oid, limit=_MAX_BLOB)
            total += len(payload)
            if total > _MAX_TOTAL:
                _fail("portable source bytes exceed bounds")
            selected[path] = (mode, payload)
    return source_records_from_blobs(selected, output)


def source_records_from_tree(root: Path | str, output: str, revision: str = "HEAD"
                             ) -> tuple[str, tuple[dict[str, str], ...]]:
    _path(output)
    root_path = Path(root).absolute()
    commit, entries = _tree_entries(root_path, revision)
    return commit, _records_for_entries(root_path, entries, output)


def _manifest(sources: Sequence[Mapping[str, Any]], output: str) -> dict[str, Any]:
    return {"format": "graphify.portable.sources", "version": 1,
            "source_selector": SOURCE_SELECTOR, "sources": list(_source_records(sources, output))}


def make_bundle(graph: Mapping[str, Any], sources: Sequence[Mapping[str, Any]],
                output: str) -> dict[str, bytes]:
    """Prepare the exact three-file profile without touching disk or the index."""
    graph_data = dict(graph)
    metadata = graph_data.get("graph", {})
    if type(metadata) is not dict:
        _fail("portable graph metadata must be an object")
    graph_data["graph"] = {**metadata, tx.GRAPH_WATERMARK_KEY: dict(PORTABLE_MARKER)}
    manifest = _manifest(sources, output)
    payloads = {"graph.json": canonical_json(graph_data),
                "manifest.json": canonical_json(manifest)}
    envelope: dict[str, Any] = {
        "format": "graphify.portable", "version": 1, "output": output,
        "graph_format": "networkx.node-link-v1", "manifest_format": "graphify.portable.sources-v1",
        "source_selector": SOURCE_SELECTOR, "source_projection_digest": _digest(payloads["manifest.json"]),
        "extraction_contract": EXTRACTION_CONTRACT, "minimum_reader": 1,
        "artifacts": [{"path": path, "mode": "100644", "size": len(payloads[path]),
                       "sha256": _digest(payloads[path])} for path in sorted(payloads)],
    }
    envelope["content_id"] = _digest(b"graphify.portable.v1\0" + canonical_json(envelope))
    payloads[PORTABLE_FILE] = canonical_json(envelope)
    validate_bundle(payloads, sources, output)
    return payloads


@dataclass(frozen=True)
class PortableGraphSnapshot:
    payload: bytes
    manifest_payload: bytes
    content_id: str
    source_projection_digest: str
    artifacts: Mapping[str, bytes] = field(repr=False)
    graph_path: Path | None = None
    revision: str | None = None

    @property
    def data(self) -> dict[str, Any]:
        """Return caller-owned graph data; retained snapshot bytes stay immutable."""
        return parse_json(self.payload)


def validate_bundle(payloads: Mapping[str, bytes], sources: Sequence[Mapping[str, Any]],
                    output: str, modes: Mapping[str, str] | None = None
                    ) -> PortableGraphSnapshot:
    _path(output)
    if set(payloads) != _FILES or any(type(p) is not bytes for p in payloads.values()):
        _fail("portable bundle requires the exact three-file closure")
    if modes is not None and (set(modes) != _FILES or set(modes.values()) != {"100644"}):
        _fail("portable artifact Git modes must be 100644")
    from graphify.security import _max_graph_file_bytes
    if (len(payloads[PORTABLE_FILE]) > _MAX_METADATA
            or len(payloads["manifest.json"]) > _MAX_METADATA
            or len(payloads["graph.json"]) > min(_max_graph_file_bytes(), _MAX_TOTAL)
            or sum(map(len, payloads.values())) > _MAX_TOTAL):
        _fail("portable bundle exceeds byte bounds")
    envelope = parse_json(payloads[PORTABLE_FILE], canonical=True)
    if type(envelope) is not dict or set(envelope) != _ENVELOPE_FIELDS:
        _fail("portable envelope has unsupported fields")
    expected = {"format": "graphify.portable", "version": 1, "output": output,
                "graph_format": "networkx.node-link-v1",
                "manifest_format": "graphify.portable.sources-v1",
                "source_selector": SOURCE_SELECTOR, "extraction_contract": EXTRACTION_CONTRACT,
                "minimum_reader": 1}
    if any(type(envelope.get(k)) is not type(v) or envelope.get(k) != v
           for k, v in expected.items()):
        _fail("portable envelope version, selector, or reader contract is unsupported")
    content_id = envelope["content_id"]
    body = {k: v for k, v in envelope.items() if k != "content_id"}
    if type(content_id) is not str or content_id != _digest(b"graphify.portable.v1\0" + canonical_json(body)):
        _fail("portable content identifier does not match")
    inventory = envelope["artifacts"]
    expected_inventory = [{"path": path, "mode": "100644", "size": len(payloads[path]),
                           "sha256": _digest(payloads[path])}
                          for path in ("graph.json", "manifest.json")]
    if (type(inventory) is not list or inventory != expected_inventory
            or any(type(r) is not dict or type(r.get("size")) is not int for r in inventory)):
        _fail("portable artifact inventory does not match")
    manifest = parse_json(payloads["manifest.json"], canonical=True)
    if (type(manifest) is not dict
            or payloads["manifest.json"] != canonical_json(_manifest(sources, output))):
        _fail("portable manifest does not match the complete committed source projection")
    if envelope["source_projection_digest"] != _digest(payloads["manifest.json"]):
        _fail("portable source projection digest does not match")
    graph = parse_json(payloads["graph.json"])
    metadata = graph.get("graph") if type(graph) is dict else None
    if type(metadata) is not dict:
        _fail("portable graph watermark is unsupported")
    marker = metadata.get(tx.GRAPH_WATERMARK_KEY)
    if (type(marker) is not dict or set(marker) != set(PORTABLE_MARKER)
            or any(type(marker.get(k)) is not type(v) or marker.get(k) != v
                   for k, v in PORTABLE_MARKER.items())):
        _fail("portable graph watermark is unsupported")
    if any(key in metadata for key in ("_learning_overlay", "_idf_cache", "_trigram_index")):
        _fail("portable graph contains unsupported persisted query runtime metadata")
    if type(graph.get("nodes")) is not list or type(graph.get("links")) is not list:
        _fail("portable graph must contain node-link arrays")
    if type(graph.get("directed")) is not bool or type(graph.get("multigraph")) is not bool:
        _fail("portable graph must declare its node-link flags")
    node_ids: set[str] = set()
    for node in graph["nodes"]:
        if (type(node) is not dict or type(node.get("id")) is not str
                or node["id"] in node_ids):
            _fail("portable graph node identifiers are malformed or duplicated")
        if any(key in node and type(node[key]) is not str
               for key in ("label", "norm_label", "source_file")):
            _fail("portable graph node metadata must use strings when present")
        node_ids.add(node["id"])
    for link in graph["links"]:
        if (type(link) is not dict
                or type(link.get("source")) is not str
                or type(link.get("target")) is not str
                or link["source"] not in node_ids or link["target"] not in node_ids):
            _fail("portable graph has an invalid or dangling link")
        if "context" in link and type(link["context"]) is not str:
            _fail("portable graph edge context must be a string when present")
        if graph["multigraph"] and "key" in link and type(link["key"]) not in {str, int}:
            _fail("portable graph multigraph key must be a string or integer")
    return PortableGraphSnapshot(payloads["graph.json"], payloads["manifest.json"], content_id,
                                 envelope["source_projection_digest"],
                                 MappingProxyType(dict(payloads)))


def open_portable_graph_snapshot(root: Path | str, output: str, revision: str = "HEAD"
                                 ) -> PortableGraphSnapshot:
    """Admit exact working-copy bundle bytes against one immutable committed tree."""
    _path(output)
    root_path = Path(root).absolute()
    if root_path.resolve(strict=True) != root_path:
        _fail("portable repository root must not use symlink aliases")
    commit, entries = _tree_entries(root_path, revision)
    selected = {p[len(output) + 1:]: (mode, oid) for p, (mode, oid) in entries.items()
                if p.startswith(output + "/")}
    if set(selected) != _FILES:
        _fail("committed portable output does not have the exact closure")
    sources = _records_for_entries(root_path, entries, output)
    with ExitStack() as stack:
        capabilities = [stack.enter_context(tx.pin_output(root_path, mutation=False))]
        current = root_path
        for part in output.split("/"):
            current /= part
            capabilities.append(stack.enter_context(tx.pin_output(current, mutation=False)))
        capability = capabilities[-1]
        stack.enter_context(tx._locked(capability))
        def check_namespace() -> None:
            for parent in capabilities:
                parent.validate()
            for parent in capabilities[:-1]:
                if tx._managed_authority_present(parent):
                    _fail("portable output is nested beneath managed authority")
            if tx._coordination_present(capability, ignored_names=frozenset({PORTABLE_FILE})):
                _fail("portable output contains mixed local authority")
            if set(tx._list_entries(capability)) != _FILES:
                _fail("portable output contains unsupported siblings or missing artifacts")
        check_namespace()
        payloads: dict[str, bytes] = {}
        identities: dict[str, tx.ManagedEntryIdentity] = {}
        from graphify.security import _max_graph_file_bytes
        for name in sorted(_FILES):
            mode, oid = selected[name]
            if mode != "100644":
                _fail("portable artifact Git modes must be 100644")
            info = tx._entry_stat(capability, name)
            if info is None or info.st_mode & 0o111:
                _fail("portable artifact filesystem mode is unsafe")
            limit = min(_max_graph_file_bytes(), _MAX_TOTAL) if name == "graph.json" else _MAX_METADATA
            payload, identity = tx._read_managed_bytes(capability, name, limit)
            if payload != _git(root_path, "cat-file", "blob", oid, limit=limit):
                _fail("portable artifact differs from the selected committed tree")
            payloads[name], identities[name] = payload, identity
        snapshot = validate_bundle(payloads, sources, output,
                                   {name: selected[name][0] for name in selected})
        check_namespace()
        for name, identity in identities.items():
            if tx._managed_entry_identity(capability, name) != identity:
                _fail("portable artifact identity changed during admission")
        return replace(snapshot, revision=commit, graph_path=current / "graph.json")
