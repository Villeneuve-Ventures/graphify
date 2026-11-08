"""Fresh corpus replacement candidate, never recovery of failed authority.

Live cutover is deliberately unavailable until a maintenance backend is qualified.
The coordinator is exercised with explicitly modeled capabilities in fixture tests.
"""
from __future__ import annotations

import base64
import contextlib
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time
import unicodedata
from typing import cast

from . import legacy_source as source
from . import transaction as tx

GUARD = ".graphify_replacement_guard"
PROVENANCE = ".graphify_replacement_origin.json"
PROFILE = "fresh-corpus-codex-v1"
MAX_BYTES = 128 * 1024 * 1024
MAX_FRAGMENT_NESTING = 128
# Root depth is zero; refuse deeper directories before opening them.
MAX_PRESERVATION_DEPTH = 128
MAX_PRESERVATION_ENTRIES = 100_000
MAX_GUARD_SCAN_ENTRIES = 100_000
# Forward growth: one request copy, one container-path allowance, one bounded
# native owner, plus allocation/identity/binding/receipt fields and state names.
_FORWARD_JOURNAL_RESERVE = 3 * tx._MAX_STATE_BYTES + 64 * 1024
_JOURNAL_TAIL_RESERVE = 4096
_AUTHORITY: ContextVar[tuple[str, str] | None] = ContextVar("replacement_operation", default=None)


def _require(value, reason):
    if not value:
        raise tx.PendingTransactionError(reason)


def _bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def _bounded_bytes(value, *, reserve=0):
    """Serialize once and admit bytes plus pending growth to the reader limit."""
    data = _bytes(value)
    _require(len(data) + reserve <= MAX_BYTES, "replacement metadata exceeds bound")
    return data


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _stable(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(path, limit=MAX_BYTES, *, parent_fd=None, expected=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        _require(expected is None or _stable(info) == _stable(expected), "preservation entry replaced")
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
                 "unsafe or oversized replacement file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        _require(len(data) <= limit and _stable(os.fstat(fd)) == _stable(info), "replacement file changed")
        return data
    finally:
        os.close(fd)


def _decode_replacement_json(data):
    """Reject duplicate fields, nonfinite numbers and decoder stack exhaustion."""
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate replacement JSON field")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: _require(False, "nonfinite replacement JSON"))
    except RecursionError as exc:
        raise tx.PendingTransactionError("replacement JSON nesting exceeds decoder bound") from exc


def _load(path, *, parent=None):
    """Decode bounded JSON, optionally relative to an already pinned directory."""
    if parent is None:
        data = _read(path, limit=MAX_BYTES)
    elif os.name == "nt":
        data = tx._read_bytes(parent, path, MAX_BYTES)
    else:
        data = _read(path, limit=MAX_BYTES, parent_fd=parent.fd)
    return _decode_replacement_json(data)


def _directory(value):
    path = Path(value)
    _require(path.is_absolute() and str(path) == str(value)
             and path.resolve(strict=True) == path, "noncanonical replacement directory")
    with tx.pin_output(path, mutation=False) as cap:
        return cap.identity.json()


def _parent_paths(request):
    return {"canonical": Path(request["output"]).parent,
            "archive": Path(request["archive"]).parent,
            "control": Path(request["journal"]).parent,
            "stage": Path(request["stage"]).parent}


def _request_paths(raw):
    _require(isinstance(raw, dict) and all(isinstance(raw.get(field), str)
             and raw[field] and "\x00" not in raw[field]
             for field in ("root", "output", "archive", "stage", "journal")),
             "replacement paths must be nonempty strings without NUL")
    _require(isinstance(raw.get("extra_roots"), list) and len(raw["extra_roots"]) <= 2,
             "invalid replacement policy roots")
    for entry in raw["extra_roots"]:
        _require(isinstance(entry, dict) and set(entry) == {"label", "path"}
                 and isinstance(entry["label"], str)
                 and entry["label"] in {"git", "policy"}, "invalid policy root")
        _require(isinstance(entry["path"], str) and entry["path"]
                 and "\x00" not in entry["path"],
                 "replacement policy path must be a nonempty string without NUL")


def bind_request(raw):
    """Validate explicit string paths and freeze their initial parent identities.

    Existing bindings are checked, never refreshed. Malformed paths or changed
    bindings raise PendingTransactionError; filesystem admission can raise
    OSError. Binding does not publish graph data.
    """
    _request_paths(raw)
    if "parent_identities" in raw:
        return _request(raw)[0]
    bound = {**raw, "parent_identities": {name: _directory(path)
             for name, path in _parent_paths(raw).items()}}
    return _request(bound)[0]


def _validate_parents(request):
    expected = request.get("parent_identities")
    _require(isinstance(expected, dict) and set(expected) == set(_parent_paths(request)),
             "frozen replacement parent identities required")
    for name, path in _parent_paths(request).items():
        identity = tx._identity_from_json(expected[name])
        _require(identity is not None and _directory(path) == identity.json(),
                 "replacement parent identity changed")


def _coordinator_lock_path(journal):
    """Derive the persistent lock slot from the journal's parent and basename."""
    return journal.with_name(".replacement-lock-" + _sha(os.fsencode(journal.name)))


def _external_path(request, path, *, allow_equal=None):
    _require(path.is_absolute() and str(path) == os.path.abspath(path)
             and path.parent.resolve(strict=True) == path.parent
             and path.name not in {"", ".", ".."}, "noncanonical external replacement path")
    _require(path.name != GUARD and not _guard_backup_name(path.name), "reserved replacement guard name")
    lock = _coordinator_lock_path(Path(request["journal"]))
    if path.name.casefold() == lock.name:
        _require(not os.path.samefile(path.parent, lock.parent), "replacement evidence collides with coordinator lock")
    _directory(path.parent)
    roots = [Path(request[k]) for k in ("root", "output")]
    roots += [Path(entry["path"]) for entry in request["extra_roots"]]
    roots += [Path(request[k]) for k in ("archive", "stage", "journal") if k != allow_equal]
    _require(all(not path.is_relative_to(root) and not root.is_relative_to(path) for root in roots),
             "replacement evidence overlaps source/policy/control input")


def _same_filesystem(request, quarantine=None):
    parents = _parent_paths(request)
    device = _directory(parents["canonical"])["device"]
    for name in ("archive", "control"):
        _require(_directory(parents[name])["device"] == device,
                 "replacement rename parents must be on same filesystem")
    if quarantine is not None:
        _require(_directory(Path(quarantine).parent)["device"] == device,
                 "rollback quarantine must be on same filesystem")
    objects = [Path(request["output"]), Path(request["archive"])]
    if quarantine is not None:
        objects.append(Path(quarantine))
    for path in objects:
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _require(stat.S_ISDIR(info.st_mode) and info.st_dev == device,
                 "replacement object must be a directory on the same filesystem as its rename parents")
        _require(not os.path.ismount(path), "mounted replacement directory refused")


def _request(raw):
    fields = {"schema", "root", "output", "archive", "stage", "journal", "include", "exclude",
              "extra_roots", "directed", "extraction_profile", "artifact_profile", "parent_identities"}
    _require(isinstance(raw, dict) and set(raw) == fields and type(raw["schema"]) is int
             and raw["schema"] == 1 and type(raw["directed"]) is bool
             and raw["extraction_profile"] == PROFILE and raw["artifact_profile"] == "core-v1",
             "complete replacement request required")
    _request_paths(raw)
    output = Path(raw["output"])
    _require(output.name == "graphify-out", "replacement preserves configured graphify-out name")
    _directory(raw["root"])
    _directory(output.parent)
    root = Path(raw["root"])
    _require(not root.is_relative_to(output), "output contains source")
    for field in ("archive", "stage", "journal"):
        path = Path(raw[field])
        _require(path.is_absolute() and str(path) == raw[field]
                 and path.parent.resolve(strict=True) == path.parent
                 and path.name not in {"", ".", ".."}, "noncanonical external replacement path")
        _directory(path.parent)
        _require(not path.is_relative_to(root) and not root.is_relative_to(path)
                 and not path.is_relative_to(output), "replacement evidence overlaps source")
    _require(len({raw[k] for k in ("archive", "stage", "journal")}) == 3,
             "replacement evidence paths collide")
    # Reuse the existing bounded explicit-source request grammar, not its producer.
    structural = {k: raw[k] for k in ("schema", "root", "output", "include", "exclude", "extra_roots")}
    structural.update(extraction_profile="structural-v1", artifact_profile="core-v1")
    # Output can be absent after archival; this grammar's output check requires an
    # existing directory, so validate the original request before cutover only.
    source._patterns(raw["include"], "include", nonempty=True)
    source._patterns(raw["exclude"], "exclude")
    for entry in raw["extra_roots"]:
        _directory(entry["path"])
    for field in ("archive", "stage", "journal"):
        _external_path(raw, Path(raw[field]), allow_equal=field)
    _validate_parents(raw)

    return dict(raw), structural


def _confidence(edge, *, hyperedge=False):
    """Refuse records outside the selected edge or hyperedge host rubric."""
    import math
    _require(isinstance(edge, dict), "semantic relationship must be an object")
    kind, score = edge.get("confidence"), edge.get("confidence_score")
    _require(type(score) in {int, float} and math.isfinite(score)
             and ((kind == "EXTRACTED" and score == 1.0)
                  or (kind == "INFERRED" and score in {0.95, 0.85, 0.75, 0.65, 0.55})
                  or (not hyperedge and kind == "AMBIGUOUS" and 0.1 <= score <= 0.3)),
             "invalid semantic confidence or score")
    relations = {"participate_in", "implement", "form"} if hyperedge else {"calls", "implements", "references", "cites",
             "conceptually_related_to", "shares_data_with", "semantically_similar_to",
             "rationale_for"}
    _require(isinstance(edge.get("relation"), str) and edge["relation"] in relations,
             "invalid semantic relation")


def _admit_fragment_nesting(fragments):
    """Refuse over 128 nested containers, counting the outer fragment list as one.

    Walk dictionary values and list/tuple members without recursion or mutation.
    Iterator storage is bounded by depth; cycles also reach the depth refusal.
    Extra metadata is included because the complete fragments enter the seal.
    Return None on admission, or raise PendingTransactionError before copying.
    """
    pending = [iter((fragments,))]
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, (dict, list, tuple)):
            _require(len(pending) <= MAX_FRAGMENT_NESTING,
                     "semantic fragment nesting exceeds bound")
            pending.append(iter(value.values() if isinstance(value, dict) else value))


def build_data(raw, fragments, *, _source_output=None):
    """Read fresh AST and host-Codex fragments without providers or caches.

    Fragment containers must be lists; node records, IDs and source ownership
    are checked before iteration, hashing or membership operations. Complete
    fragment values admit at most 128 nested containers, counting the outer list
    as one. This iterative check precedes recursive composition and serialization,
    including for extra metadata retained in the seal. Edge and
    hyperedge records use their respective host-rubric relations and confidence
    kinds; hyperedges cannot be AMBIGUOUS and require at least three distinct
    members before and after graph canonicalization. Group IDs must be hashable,
    and every admitted group must survive composition, including anonymous and
    repeated groups. Composition uses the
    builder's simple Graph/DiGraph projection, which can collapse parallel
    relationships. Invalid records or final topology raise PendingTransactionError
    before a data seal is returned. This builds a sealed in-memory payload and
    does not publish filesystem authority.
    """
    import copy
    from .build import _hashable, build_from_json
    from .detect import detect
    from .extract import extract
    request, structural = _request(raw)
    if _source_output is not None:
        structural["output"] = str(_source_output)
    structural = source.validate_request(structural)
    root = Path(request["root"])
    selected = lambda p: (p != GUARD and not _guard_backup_name(Path(p).name) and any(PurePosixPath(p).full_match(x) for x in request["include"])
                          and not any(PurePosixPath(p).full_match(x) for x in request["exclude"]))
    with source._inputs(structural, source._BuildInputs) as inputs:
        detected = detect(root, source_io=inputs, strict=True, quiet=True,
                          read_only=True, ambient_output=False)
        _require(not detected["walk_errors"], "incomplete replacement detection")
        _require(not any(selected(str(Path(p).relative_to(root))) for p in detected["unclassified"]),
                 "unclassified selected replacement source")
        files = {kind: [p for p in paths if selected(str(Path(p).relative_to(root)))]
                 for kind, paths in detected["files"].items()}
        _require(not files.get("video"), "video replacement extraction is unsupported")
        ast = extract(files.get("code", []), source_io=inputs, source_root=root, strict=True,
                      parallel=False, quiet=True, ambient_output=False)
        semantic = sorted(p for kind in ("document", "paper", "image") for p in files.get(kind, []))
        _require(isinstance(fragments, list), "explicit Codex fragments required")
        _admit_fragment_nesting(fragments)
        nodes, edges, hyperedges, covered = [], [], [], set()
        owners = {}
        def admit_identity(node):
            identity = node.get("id")
            location = node.get("source_file", "")
            if location:
                location = str(Path(location).relative_to(root)) if Path(location).is_absolute() else location
            _require(identity not in owners or owners[identity] == location,
                     "cross-source node identity collision")
            owners[identity] = location
        for node in ast["nodes"]:
            admit_identity(node)
        for fragment in fragments:
            _require(isinstance(fragment, dict) and fragment.get("backend") == "codex_host_agent"
                     and fragment.get("network_egress") is False,
                     "only host-Codex fragments admitted")
            hashes = fragment.get("source_sha256")
            if not isinstance(hashes, dict) or not hashes:
                raise tx.PendingTransactionError("fragment source hashes required")
            _require(not (covered & hashes.keys()) and set(hashes) <= set(semantic),
                     "duplicate or unselected semantic scope")
            for path, digest in hashes.items():
                _require(_sha(inputs.read_bytes(path)) == digest, "semantic source changed")
            part_nodes = fragment.get("nodes", [])
            part_edges, part_groups = fragment.get("edges", []), fragment.get("hyperedges", [])
            _require(all(isinstance(part, list) for part in (part_nodes, part_edges, part_groups)),
                     "semantic fragment records must be lists")
            _require(all(isinstance(node, dict) and isinstance(node.get("id"), str)
                         and node["id"] for node in part_nodes), "invalid semantic node identity")
            ids = {node["id"] for node in part_nodes}
            _require(len(ids) == len(part_nodes),
                     "invalid semantic node identity")
            _require(all(isinstance(node.get("source_file"), str) for node in part_nodes),
                     "invalid semantic node source")
            _require({n.get("source_file") for n in part_nodes} == set(hashes),
                     "semantic coverage is incomplete")
            for node in part_nodes:
                admit_identity(node)
                _require(isinstance(node.get("file_type"), str)
                         and node["file_type"] in {"document", "paper", "image", "concept", "rationale"},
                         "invalid semantic file type")
            for edge in part_edges:
                _confidence(edge)
                _require(all(isinstance(edge.get(field), str) for field in ("source", "target", "source_file"))
                         and edge["source"] in ids and edge["target"] in ids
                         and edge["source"] != edge["target"]
                         and edge.get("source_file") in hashes, "invalid semantic edge")
            for group in part_groups:
                _confidence(group, hyperedge=True)
                _require(_hashable(group.get("id")), "invalid semantic group identity")
                members = group.get("nodes")
                _require(isinstance(members, list)
                         and all(isinstance(member, str) and member for member in members)
                         and len(set(members)) >= 3
                         and set(members) <= ids
                         and isinstance(group.get("source_file"), str)
                         and group["source_file"] in hashes, "invalid semantic group")
            nodes.extend(part_nodes)
            edges.extend(part_edges)
            hyperedges.extend(part_groups)
            covered.update(hashes)
        _require(covered == set(semantic), "selected semantic inputs are incomplete")
        graph = build_from_json(copy.deepcopy({"nodes": ast["nodes"] + nodes, "edges": ast["edges"] + edges,
                                 "hyperedges": hyperedges}), directed=request["directed"], root=root)
        _require(graph.number_of_nodes() > 0, "empty replacement graph")
        _require(len(graph.graph.get("hyperedges", [])) == len(hyperedges),
                 "semantic groups lost during graph canonicalization")
        _require(all(len(set(group["nodes"])) >= 3 for group in graph.graph.get("hyperedges", [])),
                 "semantic group lost minimum membership during graph canonicalization")
        surviving = {node.get("source_file") for _, node in graph.nodes(data=True)}
        _require({str(Path(p).relative_to(root)) for p in covered} <= surviving,
                 "semantic source coverage lost during graph canonicalization")
        source_hashes, manifest = {}, {}
        for path in sorted(p for paths in files.values() for p in paths):
            data = inputs.read_bytes(path)
            relative = str(Path(path).relative_to(root))
            source_hashes[relative] = _sha(data)
            info = cast(os.stat_result, inputs.probe(path))
            manifest[relative] = {"mtime": info.st_mtime, "ast_hash": "", "semantic_hash": ""}
            import hashlib
            key = "ast_hash" if path in files.get("code", []) else "semantic_hash"
            manifest[relative][key] = hashlib.md5(data, usedforsecurity=False).hexdigest()
        payloads = {"graph.json": source._graph_bytes(graph), "manifest.json": _bytes(manifest),
                    ".graphify_root": (str(root) + "\n").encode()}
        policy_reads = [r for r in inputs.evidence if r["operation"] == "read"]
    result = {"fragments": fragments, "policy_reads": policy_reads, "schema": 1, "request_sha256": _sha(_bytes(request)), "producer": PROFILE,
            "source_sha256": source_hashes, "semantic_completed": sorted(covered),
            "payloads": {k: base64.b64encode(v).decode() for k, v in payloads.items()}}
    _require(len(_bytes(result)) <= MAX_BYTES, "replacement data seal exceeds bound")
    return result


def _preservation_names(fd, limit):
    """Sort only an admitted directory listing, stopping at limit plus one names."""
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            _require(len(names) < limit, "preservation entry bound exceeded")
            names.append(entry.name)
    return sorted(names)


def _tree(path):
    """Inventory opaque bytes without admitting old graph authority.

    Every record binds identity, mode and ownership without timestamps
    changed by a rename. The root is depth zero and does not consume the child
    entry budget. Both membership passes stop at the remaining budget plus one
    names before sorting. Child directories through MAX_PRESERVATION_DEPTH are
    admitted; deeper directories raise PendingTransactionError before opening.
    Opened descriptors close on both success and refusal.
    """
    result, total = {}, 0
    with tx.pin_output(path, mutation=False) as cap:
        root = os.fstat(cap.fd)
        result["."] = {"device": root.st_dev, "inode": root.st_ino, "mode": root.st_mode,
                       "uid": root.st_uid, "gid": root.st_gid}
        def walk(fd, prefix, depth):
            nonlocal total
            budget = MAX_PRESERVATION_ENTRIES - (len(result) - 1)
            names = _preservation_names(fd, budget)
            for name in names:
                relative = f"{prefix}/{name}" if prefix else name
                _require(len(result) - 1 < MAX_PRESERVATION_ENTRIES, "preservation entry bound exceeded")
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                _require(stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode)
                         and info.st_nlink == 1), "unsafe preservation entry")
                record: dict[str, int | str] = {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
                                                "uid": info.st_uid, "gid": info.st_gid}
                result[relative] = record
                if stat.S_ISDIR(info.st_mode):
                    _require(depth < MAX_PRESERVATION_DEPTH,
                             "preservation directory depth bound exceeded")
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
                    try:
                        _require(_stable(os.fstat(child)) == _stable(info), "preservation directory replaced")
                        walk(child, relative, depth + 1)
                    finally:
                        os.close(child)
                else:
                    total += info.st_size
                    _require(total <= 2 * 1024**3, "preservation byte bound exceeded")
                    record["sha256"] = _sha(_read(name, parent_fd=fd, expected=info))
                _require(_stable(os.stat(name, dir_fd=fd, follow_symlinks=False)) == _stable(info),
                         "preservation source changed")
            _require(_preservation_names(fd, budget) == names, "preservation membership changed")
        walk(cap.fd, "", 0)
        _require(_stable(os.fstat(cap.fd)) == _stable(root), "preservation root changed")
        cap.validate()
    return result


def preview(raw, sealed):
    """Validate fresh sealed data and return a read-only replacement plan.

    A non-object seal raises PendingTransactionError before field access. Preview
    does not admit old graph authority or change selected output membership.
    """
    request, _ = _request(raw)
    _require(isinstance(sealed, dict), "replacement data seal must be a JSON object")
    _require(sealed.get("schema") == 1 and sealed.get("producer") == PROFILE
             and sealed.get("request_sha256") == _sha(_bytes(request)), "data seal/request mismatch")
    root, output = Path(request["root"]), Path(request["output"])
    for field in ("archive", "journal"):
        _require(not os.path.lexists(request[field]), "replacement destination occupied")
    _same_filesystem(request)
    _source_current(request, sealed)
    payloads = {n: base64.b64decode(v, validate=True) for n, v in sealed["payloads"].items()}
    _require(sum(map(len, payloads.values())) <= MAX_BYTES, "replacement payload bound exceeded")
    _require(len(_bytes(request)) + 4096 <= tx._MAX_STATE_BYTES, "replacement origin exceeds bound")
    plan = {"schema": 1, "request": request, "old_identity": _directory(output),
            "parent_identity": _directory(output.parent), "archive_parent_identity": _directory(Path(request["archive"]).parent),
            "old_tree": _tree(output), "seal_sha256": _sha(_bytes(sealed)),
            "inventory": {n: {"size": len(v), "sha256": _sha(v)} for n, v in payloads.items()}}
    result = {**plan, "preview_sha256": _sha(_bytes(plan))}
    initial = {**result, "request_digest": _sha(_bytes(request)), "state": "PREPARED",
               "new_identity": None, "container": None, "owner": None}
    _bounded_bytes(initial, reserve=_FORWARD_JOURNAL_RESERVE)
    return result


@contextlib.contextmanager
def _maintenance(request):
    raise tx.PendingTransactionError("native replacement maintenance backend is not qualified")
    yield  # pragma: no cover -- default never grants a capability


def _guard_backup_name(name):
    prefix = "." + GUARD + ".graphify-merge-backup."
    token = name.removeprefix(prefix)
    return name.startswith(prefix) and len(token) == 32 and all(c in "0123456789abcdef" for c in token)


def _source_current(request, sealed):
    _require(isinstance(sealed, dict), "replacement data seal must be a JSON object")
    output = Path(request["output"])
    archive = Path(request["archive"])
    source_output = output if output.exists() else archive
    rebuilt = build_data(request, sealed.get("fragments", []), _source_output=source_output)
    _require(_bytes(rebuilt) == _bytes(sealed), "replacement source or sealed data changed")
