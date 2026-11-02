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
from typing import cast

from . import legacy_source as source
from . import transaction as tx

GUARD = ".graphify_replacement_guard"
PROVENANCE = ".graphify_replacement_origin.json"
PROFILE = "fresh-corpus-codex-v1"
MAX_BYTES = 128 * 1024 * 1024
# Root depth is zero; refuse deeper directories before opening them.
MAX_PRESERVATION_DEPTH = 128
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


def _forward_reserve(journal):
    if journal["state"] == "PREPARED":
        return _FORWARD_JOURNAL_RESERVE
    return _JOURNAL_TAIL_RESERVE + (tx._MAX_STATE_BYTES if journal["owner"] is None else 0)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _stable(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _safe_platform():
    _require(os.name == "posix"
             and all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY"))
             and os.open in os.supports_dir_fd,
             "safe replacement filesystem operations unavailable")


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


def _load(path, *, parent=None):
    """Decode bounded JSON, optionally relative to an already pinned directory."""
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate replacement JSON field")
            result[key] = value
        return result
    if parent is None:
        data = _read(path, limit=MAX_BYTES)
    elif os.name == "nt":
        data = tx._read_bytes(parent, path, MAX_BYTES)
    else:
        data = _read(path, limit=MAX_BYTES, parent_fd=parent.fd)
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: _require(False, "nonfinite replacement JSON"))


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


def _external_path(request, path, *, allow_equal=None):
    _require(path.is_absolute() and str(path) == os.path.abspath(path)
             and path.parent.resolve(strict=True) == path.parent
             and path.name not in {"", ".", ".."}, "noncanonical external replacement path")
    _require(path.name != GUARD and not _guard_backup_name(path.name), "reserved replacement guard name")
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
    _require(edge.get("relation") in relations, "invalid semantic relation")


def build_data(raw, fragments, *, _source_output=None):
    """Read fresh AST and host-Codex fragments without providers or caches.

    Semantic edge and hyperedge records must be objects and use their respective
    host-rubric relations and confidence kinds; hyperedges cannot be AMBIGUOUS.
    Invalid records raise PendingTransactionError before graph composition. This
    builds a sealed in-memory payload and does not publish filesystem authority.
    """
    import copy
    from .build import build_from_json
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
            ids = {n.get("id") for n in part_nodes if isinstance(n, dict)}
            _require(len(ids) == len(part_nodes) and all(isinstance(n, str) and n for n in ids),
                     "invalid semantic node identity")
            _require({n.get("source_file") for n in part_nodes} == set(hashes),
                     "semantic coverage is incomplete")
            for node in part_nodes:
                admit_identity(node)
                _require(node.get("file_type") in {"document", "paper", "image", "concept", "rationale"},
                         "invalid semantic file type")
            for edge in fragment.get("edges", []):
                _confidence(edge)
                _require(edge.get("source") in ids and edge.get("target") in ids
                         and edge["source"] != edge["target"]
                         and edge.get("source_file") in hashes, "invalid semantic edge")
            for group in fragment.get("hyperedges", []):
                _confidence(group, hyperedge=True)
                members = group.get("nodes")
                _require(isinstance(members, list)
                         and all(isinstance(member, str) and member for member in members)
                         and set(members) <= ids
                         and group.get("source_file") in hashes, "invalid semantic group")
            nodes.extend(part_nodes)
            edges.extend(fragment.get("edges", []))
            hyperedges.extend(fragment.get("hyperedges", []))
            covered.update(hashes)
        _require(covered == set(semantic), "selected semantic inputs are incomplete")
        graph = build_from_json(copy.deepcopy({"nodes": ast["nodes"] + nodes, "edges": ast["edges"] + edges,
                                 "hyperedges": hyperedges}), directed=request["directed"], root=root)
        _require(graph.number_of_nodes() > 0, "empty replacement graph")
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


def _tree(path):
    """Inventory opaque bytes without admitting old graph authority.

    The root is depth zero. Child directories through MAX_PRESERVATION_DEPTH are
    admitted; deeper directories raise PendingTransactionError before opening.
    Opened descriptors close on both success and refusal.
    """
    result, total = {}, 0
    with tx.pin_output(path, mutation=False) as cap:
        def walk(fd, prefix, depth):
            nonlocal total
            names = sorted(os.listdir(fd))
            for name in names:
                relative = f"{prefix}/{name}" if prefix else name
                _require(len(result) < 100_000, "preservation entry bound exceeded")
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                _require(stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode)
                         and info.st_nlink == 1), "unsafe preservation entry")
                record: dict[str, int | str] = {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode}
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
            _require(sorted(os.listdir(fd)) == names, "preservation membership changed")
        walk(cap.fd, "", 0)
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


def _identity_at(path):
    try:
        return _directory(path)
    except (FileNotFoundError, tx.PendingTransactionError):
        return None


@contextlib.contextmanager
def _guard_parent(parent):
    """Pin an existing guard ancestor without recursively invoking output admission.

    Missing ancestors yield None so their existing parents can still fence an
    absent target. Other admission failures propagate. Directory aliases retain
    their selected physical mapping through the read; no mutation is performed.
    """
    try:
        resolved = parent.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = tx._open_windows_read_directory(resolved) if os.name == "nt" else os.open(resolved, flags)
    except FileNotFoundError:
        yield None
        return
    try:
        info = os.fstat(fd)
        cap = tx.OutputCapability(resolved, tx.OutputIdentity(info.st_dev, info.st_ino), fd)
    except BaseException:
        os.close(fd)
        raise
    with cap:
        cap.validate()
        _require(parent.resolve(strict=True) == resolved, "replacement guard parent changed")
        yield cap
        cap.validate()
        _require(parent.resolve(strict=True) == resolved, "replacement guard parent changed")


def _guard_entry(cap):
    """Observe a marker with host filesystem primitives, independent of mutation adapters."""
    if os.name == "nt":
        return tx._entry_stat(cap, GUARD)
    try:
        info = os.stat(GUARD, dir_fd=cap.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info


def _guard_observation(info):
    return None if info is None else (_stable(info), info.st_uid, info.st_gid, info.st_nlink)


def _guard_backup_name(name):
    prefix = "." + GUARD + ".graphify-merge-backup."
    token = name.removeprefix(prefix)
    return name.startswith(prefix) and len(token) == 32 and all(c in "0123456789abcdef" for c in token)


def _guard_recovery_entries(cap, issuers):
    """Hold POSIX admission for trusted displaced guards without reading payloads.

    Enumerate at most MAX_GUARD_SCAN_ENTRIES names per ancestor. Foreign backup
    metadata is rechecked; trusted or uncertain evidence requires explicit
    recovery. Windows retains its existing marker-only admission.
    """
    if os.name == "nt":
        return {}
    observed = {}
    with os.scandir(cap.fd) as entries:
        for count, entry in enumerate(entries, 1):
            _require(count <= MAX_GUARD_SCAN_ENTRIES, "replacement guard recovery scan exceeds bound")
            if not _guard_backup_name(entry.name):
                continue
            before = os.stat(entry.name, dir_fd=cap.fd, follow_symlinks=False)
            _require(issuers is not None and before.st_uid not in issuers,
                     "replacement guard update requires explicit recovery")
            after = os.stat(entry.name, dir_fd=cap.fd, follow_symlinks=False)
            _require(_guard_observation(before) == _guard_observation(after),
                     "foreign replacement guard recovery evidence changed")
            observed[entry.name] = _guard_observation(before)
    cap.validate()
    return observed


def _guard_path(path, *, mutation=True, issuer_policy=None):
    """Read ancestor registrations through pinned directories, including absent targets."""
    path = Path(os.path.abspath(path))
    for parent in path.parents:
        with _guard_parent(parent) as cap:
            issuers = None if issuer_policy is None else issuer_policy.trusted_issuers(
                parent if cap is None else cap.path,
                None if cap is None else (cap.identity.device, cap.identity.inode), selected_parent=parent)
            if cap is None:
                continue
            recovery = _guard_recovery_entries(cap, issuers)
            before = _guard_entry(cap)
            try:
                if before is None:
                    continue
                if issuers is not None and before.st_uid not in issuers:
                    _require(_guard_observation(_guard_entry(cap)) == _guard_observation(before),
                             "foreign replacement guard changed")
                    cap.validate()
                    continue
                _require(stat.S_ISREG(before.st_mode), "unsafe non-regular replacement guard")
                _require(before.st_nlink == 1 and before.st_size <= MAX_BYTES,
                         "unsafe or oversized replacement file")
                record = _load(GUARD, parent=cap)
                after = _guard_entry(cap)
                _require(_guard_observation(after) == _guard_observation(before), "replacement guard changed")
                cap.validate()
                _require(isinstance(record, dict) and record.get("schema") == 1
                         and isinstance(record.get("entries"), dict), "malformed replacement guard")
                name = path.relative_to(parent).parts[0]
                if name not in record["entries"]:
                    continue
                entry = record["entries"][name]
                _require(isinstance(entry, dict) and set(entry) == {"journal", "digest", "role"}
                         and isinstance(entry["role"], str) and entry["role"] in {"target", "archive"}
                         and isinstance(entry["journal"], str) and Path(entry["journal"]).is_absolute()
                         and isinstance(entry["digest"], str) and bool(entry["digest"]),
                         "malformed replacement guard entry")
                journal = _load(entry["journal"])
                _require(isinstance(journal, dict) and journal.get("request_digest") == entry["digest"],
                         "replacement guard changed")
                if _AUTHORITY.get() == (entry["journal"], entry["digest"]):
                    continue
                if entry.get("role") == "archive":
                    _require(not mutation, "preserved graph archive is read-only")
                else:
                    _require(journal.get("state") in {"COMMITTED", "ROLLED_BACK_UNRECOVERED"},
                             "canonical replacement is incomplete")
            finally:
                _require(_guard_recovery_entries(cap, issuers) == recovery,
                         "replacement guard recovery evidence changed")
                _require(_guard_observation(_guard_entry(cap)) == _guard_observation(before),
                         "foreign replacement guard changed" if before is not None
                         and issuers is not None and before.st_uid not in issuers
                         else "replacement guard changed")
                cap.validate()


def guard_output(path, *, mutation=True):
    """Refuse disallowed replacement/archive access without filesystem mutation.

    Both lexical and resolved aliases are checked, including descendants and
    absent targets. Markers are read relative to pinned ancestors and revalidated;
    A protected, namespace-scoped issuer snapshot classifies POSIX marker owners
    before payload admission. Only proved-foreign entries are ignored; trusted
    malformed markers, trusted displaced-guard backups and uncertain/changed
    policy raise PendingTransactionError. POSIX ancestor scans are bounded.
    Windows retains its existing admission without Unix issuer filtering.
    Return None if admission succeeds; mutation=False permits archive reads.
    This preflight does not grant maintenance authority or fence arbitrary writers.
    """
    try:
        absolute = os.path.abspath(path)
        resolved = os.path.realpath(absolute)
        from .replacement_guard_issuers import issuer_snapshot
        policy_context = issuer_snapshot() if os.name == "posix" else contextlib.nullcontext(None)
        with policy_context as policy:
            for candidate in dict.fromkeys((absolute, resolved)):
                _guard_path(candidate, mutation=mutation, issuer_policy=policy)
        _require(os.path.realpath(absolute) == resolved, "replacement guard path changed")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise tx.PendingTransactionError("malformed or unavailable replacement guard") from exc


@contextlib.contextmanager
def _operation(journal_path, digest):
    token = _AUTHORITY.set((str(journal_path), digest))
    binding = "canonical-replacement-v1:" + digest
    other = tx._REPLACEMENT_AUTHORITY.set(binding)
    try:
        yield binding
    finally:
        tx._REPLACEMENT_AUTHORITY.reset(other)
        _AUTHORITY.reset(token)


@contextlib.contextmanager
def _coordinator_lock(request):
    """Bind the inode and parents, then fully write/read the lock before work.

    Short writes are completed; zero progress or a changed binding refuses
    before yielding. Incomplete lock evidence is retained for explicit recovery.
    """
    import fcntl
    deadline = time.monotonic() + 30
    journal_slot = Path(request["journal"]).name
    _validate_parents(request)
    path = Path(request["journal"]).parent / (".replacement-lock-" + _sha(os.fsencode(journal_slot)))
    with tx.pin_output(path.parent, mutation=True) as parent:
        _require(parent.identity.json() == request["parent_identities"]["control"],
                 "replacement control parent changed")
        parent.validate()
        _require(time.monotonic() < deadline, "replacement operation lock deadline")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path.name, flags | os.O_CREAT, 0o600, dir_fd=parent.fd)
        except FileNotFoundError:
            # Concurrent first creation can report ENOENT on macOS/APFS.
            # Reopen the peer's lock; never create a replacement during recovery.
            while True:
                _require(time.monotonic() < deadline, "replacement operation lock deadline")
                _validate_parents(request)
                parent.validate()
                try:
                    fd = os.open(path.name, flags, dir_fd=parent.fd)
                    break
                except FileNotFoundError:
                    remaining = deadline - time.monotonic()
                    _require(remaining > 0, "replacement operation lock deadline")
                    time.sleep(min(0.01, remaining))
        try:
            _validate_parents(request)
            parent.validate()
            while True:
                _require(time.monotonic() < deadline, "replacement operation lock deadline")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    _require(remaining > 0, "replacement operation lock deadline")
                    time.sleep(min(0.01, remaining))
            _require(time.monotonic() < deadline, "replacement operation lock deadline")
            _validate_parents(request)
            info = os.fstat(fd)
            parent.validate()
            named = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and info.st_uid == os.getuid() and (info.st_dev, info.st_ino) == (named.st_dev, named.st_ino),
                     "replacement operation lock changed")
            payload = ("replacement-lock-v1:" + _sha(os.fsencode(journal_slot))).encode()
            if info.st_size == 0:
                view = memoryview(payload)
                while view:
                    count = os.write(fd, view)
                    _require(0 < count <= len(view), "replacement operation lock write made no progress")
                    view = view[count:]
                os.fsync(fd)
                with tx.pin_output(path.parent, mutation=False) as cap:
                    os.fsync(cap.fd)
            written = os.fstat(fd)
            _require(_read(path.name, parent_fd=parent.fd, limit=1024, expected=written) == payload,
                     "replacement operation lock binding changed")
            _validate_parents(request)
            parent.validate()
            named = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
            _require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino)
                     and named.st_nlink == 1 and named.st_uid == os.getuid(),
                     "replacement operation lock changed")
            yield
            parent.validate()
            named = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
            _require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino), "replacement lock retargeted")
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _persist(path, value):
    data = _bounded_bytes(value)
    with tx.pin_output(Path(path).parent) as cap:
        tx._replace_bytes(cap, Path(path).name, data)


def _register(path, journal, digest, role):
    from .replacement_guard_issuers import issuer_snapshot

    path = Path(path)
    with _fd_locks([path.parent], time.monotonic() + 30) as caps, \
            tx.pin_output(path.parent, mutation=True) as cap, issuer_snapshot() as policy:
        _require(cap.identity == caps[0].identity, "replacement registration parent changed")
        issuers = policy.trusted_issuers(cap.path, (cap.identity.device, cap.identity.inode))
        before = _guard_entry(cap)
        _require(before is None or before.st_uid in issuers, "foreign replacement guard registration collision")
        _require(before is None or stat.S_ISREG(before.st_mode), "unsafe non-regular replacement guard")
        previous = _read(GUARD, parent_fd=cap.fd, expected=before) if before is not None else None
        value = _load(GUARD, parent=cap) if before is not None else {"schema": 1, "entries": {}}
        _require(_guard_observation(_guard_entry(cap)) == _guard_observation(before),
                 "replacement registration marker changed")
        entry = {"journal": str(journal), "digest": digest, "role": role}
        _require(isinstance(value, dict) and value.get("schema") == 1 and isinstance(value.get("entries"), dict),
                 "malformed replacement guard")
        _require(value["entries"].get(path.name, entry) == entry, "replacement slot already registered")
        value["entries"][path.name] = entry
        policy.validate()
        cap.validate()
        if before is None:
            tx._create_bytes(cap, GUARD, _bounded_bytes(value))
        else:
            def validate_existing(name):
                info = os.stat(name, dir_fd=cap.fd, follow_symlinks=False)
                _require((info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink)
                         == (before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_mode, 1),
                         "replacement registration entry changed")
                _require(_read(name, parent_fd=cap.fd, expected=info) == previous,
                         "replacement registration content changed")

            tx._replace_bytes(cap, GUARD, _bounded_bytes(value),
                              expected_identity=(before.st_dev, before.st_ino), validate_existing=validate_existing)


@contextlib.contextmanager
def _fd_locks(paths, deadline):
    """Locks survive rename; the coordinator validates each current name mapping."""
    import fcntl
    caps = []
    try:
        for path in sorted(set(map(Path, paths)), key=str):
            cap = tx.pin_output(path, mutation=False)
            caps.append(cap)
            while True:
                try:
                    fcntl.flock(cap.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _require(time.monotonic() < deadline, "replacement lock deadline")
                    time.sleep(0.01)
            cap.validate()
        yield caps
    finally:
        for cap in reversed(caps):
            with contextlib.suppress(OSError):
                fcntl.flock(cap.fd, fcntl.LOCK_UN)
            cap.close()


def _rename(source_path, target_path):
    """Cross-parent no-replace rename; no copy/delete or overwrite fallback."""
    import ctypes
    import sys
    source_path, target_path = Path(source_path), Path(target_path)
    with tx.pin_output(source_path.parent, mutation=False) as left, tx.pin_output(target_path.parent, mutation=False) as right:
        _require(left.identity.device == right.identity.device, "cross-filesystem replacement refused")
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renameatx_np" if sys.platform == "darwin" else "renameat2", None)
        if function is None or not (sys.platform == "darwin" or sys.platform.startswith("linux")):
            raise tx.PendingTransactionError("no-replace namespace operation unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        left.validate()
        right.validate()
        if function(left.fd, os.fsencode(source_path.name), right.fd, os.fsencode(target_path.name),
                    4 if sys.platform == "darwin" else 1):
            raise OSError(ctypes.get_errno(), "replacement rename refused")
        os.fsync(left.fd)
        os.fsync(right.fd)
        left.validate()
        right.validate()


def _state(path, journal, state, failpoint=None):
    journal["state"] = state
    _persist(path, journal)
    if failpoint:
        failpoint(state)


def _source_current(request, sealed):
    _require(isinstance(sealed, dict), "replacement data seal must be a JSON object")
    output = Path(request["output"])
    archive = Path(request["archive"])
    source_output = output if output.exists() else archive
    rebuilt = build_data(request, sealed.get("fragments", []), _source_output=source_output)
    _require(_bytes(rebuilt) == _bytes(sealed), "replacement source or sealed data changed")


def execute(raw, sealed, preview_sha256, *, failpoint=None):
    """Start or exactly retry an operation with qualified maintenance (none shipped)."""
    return _execute("execute", raw, sealed, preview_sha256, failpoint=failpoint)


def resume(raw, sealed, preview_sha256, *, failpoint=None):
    """Retry an existing exact operation with qualified maintenance (none shipped).

    Journal presence is required under the coordinator lock. An absent journal
    raises PendingTransactionError without initializing an operation.
    """
    return _execute("resume", raw, sealed, preview_sha256, failpoint=failpoint)


def _execute(action, raw, sealed, preview_sha256, *, failpoint=None):
    import secrets
    request, _ = _request(raw)
    journal_path, output, archive = map(Path, (request["journal"], request["output"], request["archive"]))
    digest = _sha(_bytes(request))
    _same_filesystem(request)
    with _maintenance(request), _operation(journal_path, digest) as binding, _coordinator_lock(request):
        _require(action != "resume" or journal_path.exists(), "existing replacement journal required")
        if journal_path.exists():
            journal = _load(journal_path)
            _require(journal.get("request") == request and journal.get("request_digest") == digest
                     and journal.get("preview_sha256") == preview_sha256
                     and journal.get("seal_sha256") == _sha(_bytes(sealed)), "replacement resume request changed")
        else:
            plan = preview(request, sealed)
            _require(plan["preview_sha256"] == preview_sha256, "replacement preview changed")
            journal = {**plan, "request_digest": digest, "state": "PREPARED", "new_identity": None,
                       "container": None, "owner": None}
            data = _bounded_bytes(journal, reserve=_FORWARD_JOURNAL_RESERVE)
            with tx.pin_output(journal_path.parent) as journal_parent:
                tx._create_bytes(journal_parent, journal_path.name, data)
        _require(journal["state"] != "ROLLED_BACK_UNRECOVERED", "rollback is terminal")
        if journal["state"] == "COMMITTED":
            _require(_identity_at(output) == journal["new_identity"] and _tree(archive) == journal["old_tree"],
                     "completed replacement objects changed")
            with tx.pin_output(output, mutation=False) as cap, tx._locked(cap):
                _, current, _ = tx._validate_receipt_locked(cap, require_closed=True)
                _require(current == journal["receipt_sha256"], "completed replacement generation changed")
            return {"status": "complete", "receipt_sha256": journal["receipt_sha256"]}
        _bounded_bytes(journal, reserve=_forward_reserve(journal))
        _register(output, journal_path, digest, "target")
        _register(archive, journal_path, digest, "archive")
        _source_current(request, sealed)
        _require(_identity_at(output.parent) == journal["parent_identity"]
                 and _identity_at(archive.parent) == journal["archive_parent_identity"], "replacement parent changed")
        old_path = archive if archive.exists() else output
        lock_paths = [output.parent, archive.parent, old_path]
        with _fd_locks(lock_paths, time.monotonic() + 30):
            _require(_identity_at(old_path) == journal["old_identity"]
                     and _tree(old_path) == journal["old_tree"], "preserved graph changed")
            if journal["state"] == "PREPARED":
                container = journal_path.parent / (".replacement-container-" + secrets.token_hex(16))
                os.mkdir(container, 0o700)
                if failpoint:
                    failpoint("after_container_mkdir")
                allocation = {"schema": 1, "request_digest": digest, "nonce": secrets.token_hex(32)}
                _persist(container / ".graphify_replacement_allocation.json", allocation)
                with tx.pin_output(container, mutation=False) as cap:
                    os.fsync(cap.fd)
                    journal.update(container=str(container), new_identity=cap.identity.json(), allocation=allocation)
                provenance = {"schema": 1, "generation": 1, "mode": "canonical-replacement",
                    "output_identity": journal["new_identity"], "request": request,
                    "seal_sha256": journal["seal_sha256"],
                    "old_inventory_sha256": _sha(_bytes(journal["old_tree"]))}
                journal["provenance"] = provenance
                journal["binding"] = "canonical-replacement-v1:" + _sha(_bytes(provenance))
                _bounded_bytes({**journal, "state": "ALLOCATED"},
                               reserve=tx._MAX_STATE_BYTES + _JOURNAL_TAIL_RESERVE)
                _state(journal_path, journal, "ALLOCATED", failpoint)
            if journal["state"] == "ALLOCATED":
                _state(journal_path, journal, "ARCHIVE_INTENT", failpoint)
            if journal["state"] == "ARCHIVE_INTENT":
                _source_current(request, sealed)
                if _identity_at(output) == journal["old_identity"] and not os.path.lexists(archive):
                    _rename(output, archive)
                _require(_identity_at(archive) == journal["old_identity"] and not os.path.lexists(output),
                         "unknown archive transition")
                _state(journal_path, journal, "OLD_ARCHIVED", failpoint)
            if journal["state"] == "OLD_ARCHIVED":
                _state(journal_path, journal, "INSTALL_INTENT", failpoint)
            if journal["state"] == "INSTALL_INTENT":
                container = Path(journal["container"])
                if _identity_at(container) == journal["new_identity"] and not os.path.lexists(output):
                    _require(_load(container / ".graphify_replacement_allocation.json") == journal["allocation"],
                             "allocation claim changed")
                    _rename(container, output)
                _require(_identity_at(output) == journal["new_identity"], "unknown installation transition")
                _state(journal_path, journal, "NEW_INSTALLED", failpoint)
            _require(_identity_at(output) == journal["new_identity"], "new output changed")
            _source_current(request, sealed)
            binding = journal["binding"]
            tx._REPLACEMENT_AUTHORITY.set(binding)
            if journal["state"] == "NEW_INSTALLED":
                _bounded_bytes(journal, reserve=tx._MAX_STATE_BYTES + _JOURNAL_TAIL_RESERVE)
                owner = tx.begin_transaction("full", request["root"], output=output, replacement_binding=binding)
                owner_record = tx._transaction_json(owner, phase=owner.phase)
                _require(len(_bytes(owner_record)) <= tx._MAX_STATE_BYTES,
                         "replacement owner exceeds reserved bound")
                journal["owner"] = owner_record
                _state(journal_path, journal, "PUBLISHING", failpoint)
            _require(journal["state"] == "PUBLISHING", "unknown replacement state")
            with tx.pin_output(output) as cap, tx._locked(cap):
                protocol = tx._read_protocol(cap)
                if (protocol is None or protocol.get("generation") != 1
                        or protocol.get("replacement_binding") != binding
                        or protocol.get("transaction_id") != journal["owner"]["id"]):
                    raise tx.PendingTransactionError("replacement generation/owner changed")
                complete = protocol["state"] == "COMPLETE"
            if not complete:
                owner = tx.resume_transaction(journal["owner"]["id"], request["root"], output=output)
                authority = tx._AUTHORITY.set(tx._authority_for(owner))
                try:
                    payloads = {name: base64.b64decode(body, validate=True) for name, body in sealed["payloads"].items()}
                    graph = json.loads(payloads["graph.json"])
                    graph.setdefault("graph", {})[tx.GRAPH_WATERMARK_KEY] = {
                        "schema": 1, "protocol_epoch": 1, "generation": 1, "state": "active"}
                    payloads["graph.json"] = _bytes(graph)
                    payloads[PROVENANCE] = _bytes(journal["provenance"])
                    tx._commit_publication_plan(owner, tx.PublicationPlan(payloads), failpoint=failpoint)
                    tx._finish_transaction(owner, failpoint=failpoint)
                finally:
                    tx._AUTHORITY.reset(authority)
            else:
                with tx.pin_output(output) as cap, tx._locked(cap):
                    tx._validate_receipt_locked(cap)
                    drainer = tx._read_drainer(cap)
                    if drainer is None:
                        raise tx.PendingTransactionError("replacement closure drainer missing")
                    if drainer[1] == "CLOSE_PENDING":
                        tx.recover_close(output)
                    elif drainer[1] != "complete":
                        owner = tx._read_transaction(cap)
                        if owner is None:
                            raise tx.PendingTransactionError("replacement closure owner missing")
                        token = tx._AUTHORITY.set(tx._authority_for(owner))
                        try:
                            tx._finish_transaction(owner, failpoint=failpoint)
                        finally:
                            tx._AUTHORITY.reset(token)
            with tx.pin_output(output) as cap, tx._locked(cap):
                tx._retire_prepared_locked(cap)
                receipt, receipt_digest, _ = tx._validate_receipt_locked(cap, require_closed=True)
            _require(receipt["generation"] == 1 and _tree(archive) == journal["old_tree"],
                     "replacement closure changed")
            journal["receipt_sha256"] = receipt_digest
            _state(journal_path, journal, "COMMITTED", failpoint)
            return {"status": "complete", "receipt_sha256": receipt_digest}


def validate_allocation(capability, binding):
    context = _AUTHORITY.get()
    if context is None:
        raise tx.PendingTransactionError("allocation requires exact replacement owner")
    journal = _load(context[0])
    _require(journal.get("request_digest") == context[1] and journal.get("binding") == binding
             and journal.get("state") == "NEW_INSTALLED"
             and capability.path == Path(journal["request"]["output"])
             and capability.identity.json() == journal["new_identity"], "allocation owner changed")
    _require(tx._list_entries(capability) == [".graphify_replacement_allocation.json"]
             and tx._load_json(capability, ".graphify_replacement_allocation.json") == journal["allocation"],
             "allocation claim changed")


def status(raw):
    """Read the selected operation state without changing its journal or graphs.

    A non-object journal or changed request raises PendingTransactionError;
    filesystem and JSON admission errors propagate to the CLI's blocked result.
    """
    request, _ = _request(raw)
    journal = _load(request["journal"])
    _require(isinstance(journal, dict), "replacement status journal must be an object")
    _require(journal.get("request") == request, "replacement status request changed")
    return {"state": journal["state"], "output": request["output"], "archive": request["archive"],
            "new_tree_sha256": _sha(_bytes(_tree(request["output"]))) if journal["state"] == "COMMITTED" else None}


def rollback(raw, expected_journal_sha256, quarantine, *, expected_new_tree=None, failpoint=None):
    """Restore the exact old tree and persist the retained replacement location.

    Installed data stays at the selected quarantine; a pre-install allocation
    stays at its recorded container. Interrupted retries use the persisted tree
    selector rather than the restored output name. Only a quarantine selected
    to retain an installed tree receives an archive guard. Rollback is terminal
    and does not repair the old authority's identity failure.
    """
    request, _ = _request(raw)
    journal_path = Path(request["journal"])
    digest = _sha(_bytes(request))
    quarantine = Path(quarantine)
    _external_path(request, quarantine)
    _same_filesystem(request, quarantine)
    with _maintenance(request), _operation(journal_path, digest), _coordinator_lock(request):
        journal = _load(journal_path)
        _require(journal.get("request") == request, "rollback request changed")
        if journal["state"] == "ROLLBACK_INTENT":
            _require(journal.get("rollback_preview") == expected_journal_sha256
                     and journal.get("rollback_quarantine") == str(quarantine), "rollback resume changed")
        else:
            _require(_sha(_bytes(journal)) == expected_journal_sha256
                     and journal["state"] != "ROLLED_BACK_UNRECOVERED", "rollback request changed")
            _require(not os.path.lexists(quarantine), "rollback retention destination occupied")
        output, archive = Path(request["output"]), Path(request["archive"])
        old_path = archive if archive.exists() else output
        quarantine_parent = _directory(quarantine.parent)
        if journal["state"] == "ROLLBACK_INTENT":
            _require(journal.get("rollback_parent_identity") == quarantine_parent,
                     "rollback retention parent changed")
            projected = dict(journal)
            projected["rollback_retained"] = (journal.get("container") if journal["rollback_new_tree"] is None
                                               else journal["rollback_quarantine"])
        elif old_path == output:
            projected = {**journal, "rollback_retained": journal.get("container")}
        else:
            new_tree = _tree(output) if output.exists() else None
            projected = {**journal, "rollback_quarantine": str(quarantine), "rollback_new_tree": new_tree,
                         "rollback_preview": expected_journal_sha256, "rollback_parent_identity": quarantine_parent,
                         "rollback_retained": journal.get("container") if new_tree is None else str(quarantine)}
        _bounded_bytes({**projected, "state": "ROLLED_BACK_UNRECOVERED"})
        retained_container = (projected["rollback_retained"]
                              if projected.get("rollback_new_tree") is None else None)
        _require(retained_container is None or _identity_at(retained_container) == journal["new_identity"],
                 "retained replacement allocation changed")
        if journal["state"] == "COMMITTED":
            _require(expected_new_tree == _sha(_bytes(projected["rollback_new_tree"])),
                     "explicit current new-graph selector required")
        if projected.get("rollback_new_tree") is not None:
            _register(quarantine, journal_path, digest, "archive")
        lock_paths = [output.parent, archive.parent, quarantine.parent, old_path]
        if retained_container is not None:
            lock_paths.append(Path(retained_container))
        with _fd_locks(lock_paths, time.monotonic() + 30):
            _require(_directory(quarantine.parent) == quarantine_parent, "rollback retention parent changed")
            _require(_identity_at(old_path) == journal["old_identity"]
                     and _tree(old_path) == journal["old_tree"], "rollback archive changed")
            _require(retained_container is None or _identity_at(retained_container) == journal["new_identity"],
                     "retained replacement allocation changed")
            if journal["state"] != "ROLLBACK_INTENT":
                if old_path == output:
                    journal["rollback_retained"] = journal.get("container")
                    _state(journal_path, journal, "ROLLED_BACK_UNRECOVERED")
                    return {"status": "rolled-back-unrecovered", "new_graph_retained": journal["rollback_retained"]}
                _require(_identity_at(output) in (None, journal["new_identity"]), "unknown canonical occupant")
                new_tree = _tree(output) if output.exists() else None
                _require(new_tree == projected["rollback_new_tree"], "rollback new graph changed")
                if journal["state"] == "COMMITTED":
                    _require(expected_new_tree == _sha(_bytes(new_tree)), "explicit current new-graph selector required")
                journal.update(rollback_quarantine=str(quarantine), rollback_new_tree=new_tree,
                               rollback_preview=expected_journal_sha256, rollback_parent_identity=quarantine_parent,
                               rollback_retained=journal.get("container") if new_tree is None else str(quarantine))
                _state(journal_path, journal, "ROLLBACK_INTENT", failpoint)
            journal["rollback_retained"] = (journal.get("container") if journal["rollback_new_tree"] is None
                                            else journal["rollback_quarantine"])
            if _identity_at(output) == journal["new_identity"]:
                _require(_tree(output) == journal["rollback_new_tree"], "rollback new graph changed")
                _rename(output, quarantine)
                if failpoint:
                    failpoint("after_rollback_quarantine")
            if journal["rollback_new_tree"] is not None:
                _require(_identity_at(quarantine) == journal["new_identity"]
                         and _tree(quarantine) == journal["rollback_new_tree"], "retained replacement changed")
            if _identity_at(archive) == journal["old_identity"] and not os.path.lexists(output):
                _rename(archive, output)
                if failpoint:
                    failpoint("after_rollback_restore")
            _require(_identity_at(output) == journal["old_identity"] and _tree(output) == journal["old_tree"],
                     "rollback preservation changed")
            _state(journal_path, journal, "ROLLED_BACK_UNRECOVERED", failpoint)
            return {"status": "rolled-back-unrecovered", "new_graph_retained": journal["rollback_retained"]}


def _seal_data(request, sealed):
    """Create the stage under its frozen parent; retain incomplete files on refusal."""
    data = _bounded_bytes(sealed)
    path = Path(request["stage"])
    with tx.pin_output(path.parent, mutation=True) as parent:
        _require(parent.identity.json() == request["parent_identities"]["stage"],
                 "replacement stage parent changed")
        parent.validate()
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent.fd)
        try:
            parent.validate()
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                _require(count > 0, "exclusive seal write made no progress")
                view = view[count:]
            parent.validate()
            os.fsync(fd)
            parent.validate()
            os.fsync(parent.fd)
            parent.validate()
        finally:
            os.close(fd)


def main(argv=None):
    """Return JSON success or blocked status for supported replacement commands.

    Safe filesystem features are required before reading any request. Build-data
    creates a seal exclusively under the frozen stage parent; interrupted writes
    stay in place and cannot be adopted or overwritten by a retry.
    """
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("bind-request", "build-data", "preview", "execute", "resume", "status", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("--request", required=True)
        if name == "build-data":
            command.add_argument("--fragments", required=True)
        if name in {"execute", "resume"}:
            command.add_argument("--preview-sha256", required=True)
        if name == "rollback":
            command.add_argument("--journal-sha256", required=True)
            command.add_argument("--quarantine", required=True)
            command.add_argument("--new-tree-sha256")
    args = parser.parse_args(argv)
    try:
        _safe_platform()
        request = _load(args.request)
        _request_paths(request)
        if args.command == "bind-request":
            result = bind_request(request)
        elif args.command == "build-data":
            sealed = build_data(request, _load(args.fragments))
            _seal_data(request, sealed)
            result = {"status": "data-sealed", "seal_sha256": _sha(_bytes(sealed))}
        elif args.command == "status":
            result = status(request)
        elif args.command == "rollback":
            result = rollback(request, args.journal_sha256, args.quarantine, expected_new_tree=args.new_tree_sha256)
        else:
            sealed = _load(request["stage"])
            if args.command == "preview":
                result = preview(request, sealed)
            elif args.command == "resume":
                result = resume(request, sealed, args.preview_sha256)
            else:
                result = execute(request, sealed, args.preview_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, tx.PendingTransactionError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
