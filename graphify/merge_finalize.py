"""Explicit, index-only publication for a v1-origin manual two-parent merge."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping

from graphify import transaction
from graphify.merge_guard import MergeGuardError, _git, _git_path
from graphify.security import _max_graph_file_bytes


class MergeFinalizeError(transaction.PendingTransactionError):
    """The bounded manual transition cannot be safely prepared."""


def _require_local_object_storage() -> None:
    if any(name in os.environ for name in (
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    )):
        raise MergeFinalizeError("relocated Git object storage is unsupported")


def _output_selector(output: str) -> str:
    path = PurePosixPath(output)
    if (not output or path == PurePosixPath(".") or path.is_absolute() or path.as_posix() != output
            or any(p in (".", "..", ".git") for p in path.parts)
            or "\\" in output or "\x00" in output):
        raise MergeFinalizeError("finalization requires a literal repository-relative output")
    return output


def _output_root(root: Path, output: str) -> None:
    if root != root.resolve():
        raise MergeFinalizeError("repository root aliases are unsupported")
    actual = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).rstrip("\n"))
    if actual != root:
        raise MergeFinalizeError("invoke finalization at the exact repository root")
    cursor = root
    for part in PurePosixPath(output).parts:
        cursor /= part
        info = cursor.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise MergeFinalizeError("output aliases and nondirectory selectors are unsupported")


def _git_identity(root: Path):
    git_dir = Path(os.fsdecode(_git(root, "rev-parse", "--absolute-git-dir")).rstrip("\n"))
    common_raw = Path(os.fsdecode(_git(root, "rev-parse", "--git-common-dir")).rstrip("\n"))
    common = common_raw if common_raw.is_absolute() else root / common_raw
    result = []
    for path in (root, git_dir, common.absolute(), _git_path(root, "index").parent):
        info = path.stat()
        result.append([str(path), info.st_dev, info.st_ino])
    result.append([str(_git_path(root, "index").absolute())])
    result.append([str(_git_path(root, "hooks").absolute())])
    pointer = root / ".git"
    pointer_info = pointer.lstat()
    if stat.S_ISLNK(pointer_info.st_mode):
        raise MergeFinalizeError("Git directory aliases are unsupported")
    result.append([pointer_info.st_dev, pointer_info.st_ino,
                   hashlib.sha256(pointer.read_bytes()).hexdigest() if pointer.is_file() else None])
    return result


def _output_attributes(root: Path, output: str):
    from graphify.portable import PORTABLE_FILE

    paths = [f"{output}/{name}" for name in ("graph.json", "manifest.json", PORTABLE_FILE)]
    attributes = ("filter", "text", "eol", "working-tree-encoding", "ident")
    observed = []
    for cached in (True, False):
        args = ("--cached",) if cached else ()
        raw = _git(root, "check-attr", *args, "-z", *attributes, "--", *paths)
        values = raw.split(b"\0")
        if len(values) != len(paths) * len(attributes) * 3 + 1:
            raise MergeFinalizeError("cannot classify exact output conversion attributes")
        if any(values[i] not in (b"unspecified", b"unset") for i in range(2, len(values) - 1, 3)):
            raise MergeFinalizeError("output filters, text conversion, and encodings are unsupported")
        observed.append(raw)
    return tuple(observed)


def _refresh_candidate(root: Path, candidate: Path, output: str):
    _output_attributes(root, output)
    query = subprocess.run(["git", "-C", str(root), "config", "--name-only", "--get-regexp",
                            r"^filter\..*\.(clean|process|smudge|required)$"],
                           capture_output=True, check=False, timeout=30)
    if query.returncode not in (0, 1):
        raise MergeFinalizeError("cannot safely disable output filters")
    drivers = {key.rsplit(".", 1)[0] for key in query.stdout.decode("utf-8").splitlines()}
    options = []
    for driver in sorted(drivers):
        for field in ("clean", "process", "smudge", "required"):
            options.extend(("-c", f"{driver}.{field}={'false' if field == 'required' else ''}"))
    from graphify.portable import PORTABLE_FILE
    names = [f"{output}/{name}" for name in ("graph.json", "manifest.json", PORTABLE_FILE)]
    # Refresh only restored literal output entries whose public bytes and Git
    # mode match. A mismatched worktree remains visible for operator resolution;
    # cancellation still restores its prior staged bytes without overwriting it.
    selectors = [f":(top,literal){name}" for name in names]
    selected = _candidate_git(root, candidate, "ls-files", "--stage", "-z", "--", *selectors)
    paths = []
    for record in selected.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, oid, stage = header.decode("ascii").split()
        name = raw_path.decode("utf-8")
        target = root / name
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                actual_mode = "100755" if info.st_mode & 0o111 else "100644"
                if not stat.S_ISREG(info.st_mode) or actual_mode != mode or info.st_size > _max_graph_file_bytes():
                    continue
                payload = stream.read(_max_graph_file_bytes() + 1)
            if stage == "0" and payload == _blob(root, oid):
                paths.append(name)
        except OSError:
            continue
    if paths:
        _candidate_git(root, candidate, *options, "update-index", "--", *paths)
    after = _candidate_git(root, candidate, "ls-files", "--stage", "-z", "--", *selectors)
    if after != selected:
        raise MergeFinalizeError("output stat refresh changed an object identity or mode")


def _entries(root: Path, revision: str | None = None) -> dict[str, tuple[str, str]]:
    records = (_git(root, "ls-files", "--stage", "-z") if revision is None
               else _git(root, "ls-tree", "-r", "-z", revision))
    entries: dict[str, tuple[str, str]] = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, third = header.decode("ascii").split()
        if revision is None:
            oid, stage = kind, third
            if stage != "0":
                raise MergeFinalizeError("resolve all unmerged index entries before finalization")
        else:
            oid = third
        path = raw_path.decode("utf-8", "strict")
        lexical = PurePosixPath(path)
        if (lexical.is_absolute() or lexical.as_posix() != path
                or any(p in (".", "..", ".git") for p in lexical.parts)
                or "\\" in path):
            raise MergeFinalizeError("unsupported repository path")
        if path in entries:
            raise MergeFinalizeError("duplicate index path")
        entries[path] = (mode, oid)
    if revision is None:
        empty = _git(root, "hash-object", "-t", "tree", "--stdin", input=b"").decode().strip()
        committed = _git(root, "diff-index", "--cached", "--ita-invisible-in-index", "--raw", "-z",
                         "-r", "--no-abbrev", "--no-ext-diff", "--no-textconv", "--no-renames",
                         "--no-relative", empty).split(b"\0")
        present = {os.fsdecode(committed[i]) for i in range(1, len(committed) - 1, 2)}
        if set(entries) != present:
            raise MergeFinalizeError("intent-to-add entries are unsupported")
    if len({p.casefold() for p in entries}) != len(entries):
        raise MergeFinalizeError("case-colliding repository paths are unsupported")
    return entries


def _blob(root: Path, oid: str) -> bytes:
    size = int(_git(root, "cat-file", "-s", oid))
    if size > _max_graph_file_bytes():
        raise MergeFinalizeError("selected Git blob exceeds the reader limit")
    payload = _git(root, "cat-file", "blob", oid)
    if len(payload) != size:
        raise MergeFinalizeError("Git blob size changed")
    return payload


def _source_blobs(root: Path, entries: Mapping[str, tuple[str, str]], output: str):
    blobs = {}
    total = 0
    for path, (mode, oid) in entries.items():
        if path.startswith(output + "/"):
            continue
        if mode == "160000" or (path.endswith(".py") and mode not in ("100644", "100755")):
            raise MergeFinalizeError("submodules and nonregular Python sources are unsupported")
        if path.endswith(".py"):
            payload = _blob(root, oid)
            total += len(payload)
            if total > _max_graph_file_bytes():
                raise MergeFinalizeError("source projection exceeds the aggregate reader limit")
            blobs[path] = (mode, payload)
    return blobs


def validate_index_bundle(root: Path, output: str, *, revision: str | None = None):
    """Validate portable closure against exact index or committed-tree objects."""
    from graphify.portable import PORTABLE_FILE, source_records_from_blobs, validate_bundle

    _require_local_object_storage()
    output = _output_selector(output)
    entries = _entries(root, revision)
    selected = {p[len(output) + 1:]: value for p, value in entries.items()
                if p.startswith(output + "/")}
    if set(selected) != {"graph.json", "manifest.json", PORTABLE_FILE}:
        raise MergeFinalizeError("portable output must contain its exact three-file closure")
    payloads = {p: _blob(root, oid) for p, (_mode, oid) in selected.items()}
    sources = source_records_from_blobs(_source_blobs(root, entries, output), output)
    return validate_bundle(payloads, sources, output,
                           modes={p: mode for p, (mode, _oid) in selected.items()})


def _observer_commit(root: Path) -> str | None:
    """Distinguish an unborn branch from an unreadable committed authority."""
    try:
        return _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    except MergeGuardError as exc:
        ref = _git(root, "symbolic-ref", "--quiet", "HEAD").strip()
        result = subprocess.run(
            ["git", "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "-c",
             "core.fsmonitor=false", "-C", str(root), "show-ref", "--verify", "--quiet", os.fsdecode(ref)],
            capture_output=True, check=False, timeout=30,
        )
        if result.returncode == 1:
            return None
        raise exc


def _observer_entries(root: Path, commit: str, output: str) -> dict[str, tuple[str, str]]:
    """Probe only literal committed envelope/graph paths before profile validation."""
    from graphify.portable import PORTABLE_FILE

    paths = (f"{output}/{PORTABLE_FILE}", f"{output}/graph.json")
    expected = {os.fsencode(path): path for path in paths}
    raw = _git(root, "--literal-pathspecs", "ls-tree", "-z", commit, "--", *paths)
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        if path not in expected or expected[path] in entries:
            raise MergeFinalizeError("cannot classify exact committed output paths")
        mode, _kind, oid = header.decode("ascii").split()
        entries[expected[path]] = mode, oid
    return entries


def observe_committed_portable(root: Path, output: str) -> bool:
    """Suppress legacy rebuilds on portable authority, including invalid bundles.

    No working-tree artifact, index, or local authority is changed. An invalid
    committed portable signal also suppresses rebuild: postevents cannot repair
    recorded bytes and must never turn portable content into local generations.
    Ordinary legacy outputs do not acquire portable source/path restrictions.
    """
    from graphify.portable import PORTABLE_FILE

    # Normal legacy hooks accept spellings such as ./graphify-out. Normalize
    # only their observer selector; producer enrollment remains strict.
    relative = PurePosixPath(output)
    if relative.is_absolute() or ".." in relative.parts:
        return False  # No in-repository committed selector exists for this path.
    output = relative.as_posix()
    commit = _observer_commit(root)
    if commit is None:
        return False  # An unborn branch has no committed portable authority.
    entries = _observer_entries(root, commit, output)
    present = output + "/" + PORTABLE_FILE in entries
    graph_entry = entries.get(output + "/graph.json")
    if graph_entry is not None and not present:
        try:
            graph = json.loads(_blob(root, graph_entry[1]).decode("utf-8"))
            marker = graph.get("graph", {}).get(transaction.GRAPH_WATERMARK_KEY)
            present = isinstance(marker, dict) and marker.get("schema") == 2
        except (ValueError, AttributeError, UnicodeError):
            return False
    if not present:
        return False
    try:
        snapshot = validate_index_bundle(root, output, revision=commit)
        message = f"committed portable bundle {snapshot.content_id} verified; local reconciliation deferred"
    except (MergeFinalizeError, ValueError, RuntimeError) as exc:
        message = f"committed portable bundle refused: {exc}; local reconciliation deferred"
    print(f"[graphify hook] {message}", file=sys.stderr)
    return True


def _merge_state(root: Path) -> tuple[str, bytes]:
    for name in ("rebase-merge", "rebase-apply", "sequencer", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if os.path.lexists(_git_path(root, name)):
            raise MergeFinalizeError("only an ordinary manual two-parent merge is supported")
    merge_path = _git_path(root, "MERGE_HEAD")
    if not merge_path.is_file() or merge_path.is_symlink():
        raise MergeFinalizeError("an uncommitted ordinary two-parent merge is required")
    merge = merge_path.read_bytes()
    lines = merge.decode("ascii").splitlines()
    if len(lines) != 1 or len(lines[0]) not in (40, 64):
        raise MergeFinalizeError("octopus and malformed merge state are unsupported")
    head = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if head == lines[0]:
        raise MergeFinalizeError("distinct two-parent merge operands are required")
    return head, merge


def _admit(root: Path, output: str, state):
    _output_root(root, output)
    _data, payload, admission = transaction._open_merge_pending_rebuild_snapshot(
        root / output / "graph.json")
    with transaction.pin_output(root / output, mutation=False) as capability, transaction._locked(capability):
        transaction._validate_merge_pending_admission_locked(capability, expected=admission)
        protocol = transaction._read_protocol(capability)
        if protocol is None or protocol.get("root") != str(root):
            raise MergeFinalizeError("predecessor authority belongs to another source root")
        receipt_payload = transaction._read_bytes(capability, transaction.RECEIPT_FILE)
    bases = _git(root, "merge-base", "--all", state[0], state[1].decode().strip()).splitlines()
    if len(bases) != 1:
        raise MergeFinalizeError("a unique v1 merge base is required")
    operands = (bases[0].decode(), state[0], state[1].decode().strip())
    digests = []
    operand_payloads = []
    graph_path = output + "/graph.json"
    for operand in operands:
        operand_entries = _entries(root, operand)
        if any(path.startswith(output + "/") and path not in (
            graph_path, output + "/manifest.json",
        ) for path in operand_entries):
            raise MergeFinalizeError("merge operand contains an unsupported tracked output sibling")
        entry = operand_entries.get(graph_path)
        if entry is None or entry[0] != "100644":
            raise MergeFinalizeError("all merge operands require regular tracked v1 graphs")
        blob = _blob(root, entry[1])
        # Detached admission validates the live pending union; bind its exact
        # original operand bytes to the current Git merge rather than trusting
        # a syntactically valid marker from another merge.
        data = json.loads(blob.decode("utf-8"))
        marker = data.get("graph", {}).get(transaction.GRAPH_WATERMARK_KEY)
        if not isinstance(marker, dict) or marker.get("schema") != 1 or marker.get("state") != "active":
            raise MergeFinalizeError("portable or legacy merge operands are unsupported")
        digests.append(hashlib.sha256(blob).hexdigest())
        operand_payloads.append(blob)
    if tuple(digests) != admission.input_digests:
        raise MergeFinalizeError("pending graph operand bindings do not match this merge")
    with tempfile.TemporaryDirectory(prefix="graphify-admission-", dir=_git_path(root, "index").parent) as directory:
        paths = [Path(directory) / name for name in ("ancestor.json", "current.json", "other.json")]
        for path, data in zip(paths, operand_payloads, strict=True):
            path.write_bytes(data)
        transaction.merge_detached_snapshots(*paths)
        if paths[1].read_bytes() != payload:
            raise MergeFinalizeError("pending content is not the canonical union of the merge operands")
    if hashlib.sha256(receipt_payload).hexdigest() != admission.predecessor_receipt_digest:
        raise MergeFinalizeError("predecessor receipt changed during admission")
    receipt = json.loads(receipt_payload)
    if set(receipt["required_artifacts"]) - {"graph.json", "manifest.json", ".graphify_root"}:
        raise MergeFinalizeError("predecessor has unsupported sibling artifacts")
    return payload, admission


_EXTRACT_SCRIPT = r'''
import json, sys
from pathlib import Path
from graphify.extract import extract_python
nodes, edges = [], []
for name in json.loads(sys.stdin.read()):
    result = extract_python(Path(name), strict=True)
    if "error" in result:
        raise RuntimeError(result["error"])
    nodes.extend(result["nodes"])
    edges.extend(result["edges"])
by_id = {}
for node in nodes:
    if node["id"] in by_id and by_id[node["id"]] != node:
        raise RuntimeError("colliding Python node identifiers are unsupported")
    by_id[node["id"]] = node
# Preserve unresolved AST references without presenting them as definitions.
# All extracted relation records remain; this deliberately avoids a DiGraph
# round trip that would collapse parallel relations between the same nodes.
for edge in edges:
    for endpoint in (edge["source"], edge["target"]):
        if endpoint not in by_id:
            by_id[endpoint] = {"id": endpoint, "label": endpoint}
unique_edges = {json.dumps(edge, sort_keys=True): edge for edge in edges}
json.dump({"directed": True, "multigraph": False, "graph": {},
           "nodes": [by_id[key] for key in sorted(by_id)],
           "links": [unique_edges[key] for key in sorted(unique_edges)]}, sys.stdout)
'''


def _extract(root: Path, blobs):
    from graphify.portable import _MAX_TOTAL

    with tempfile.TemporaryDirectory(prefix="graphify-finalize-", dir=_git_path(root, "index").parent) as directory:
        scratch = Path(directory)
        for name, (_mode, payload) in blobs.items():
            target = scratch / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("GRAPHIFY_", "GIT_"))}
        environment["PYTHONHASHSEED"] = "0"
        # Retain at most one bounded output before JSON decoding. A regular
        # input file also avoids blocking on stdin while the child emits output.
        with tempfile.TemporaryFile(dir=scratch) as source_list:
            source_list.write(json.dumps(sorted(blobs)).encode())
            source_list.seek(0)
            with subprocess.Popen(  # nosec B603 - fixed interpreter and script, no shell
                [sys.executable, "-E", "-P", "-B", "-c", _EXTRACT_SCRIPT],
                cwd=scratch, stdin=source_list, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=environment,
            ) as process:
                try:
                    if process.stdout is None or process.stderr is None:
                        raise MergeFinalizeError("frozen Python extraction pipes are unavailable")
                    output, errors = bytearray(), bytearray()
                    deadline = time.monotonic() + 300
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ,
                                          (output, min(_max_graph_file_bytes(), _MAX_TOTAL), "output"))
                        selector.register(process.stderr, selectors.EVENT_READ,
                                          (errors, 65536, "diagnostics"))
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            ready = selector.select(max(0, remaining))
                            if remaining <= 0 or not ready:
                                raise MergeFinalizeError("frozen Python extraction timed out")
                            for key, _event in ready:
                                buffer, limit, label = key.data
                                chunk = os.read(key.fd, min(65536, limit + 1 - len(buffer)))
                                if not chunk:
                                    selector.unregister(key.fd)
                                    continue
                                buffer.extend(chunk)
                                if len(buffer) > limit:
                                    raise MergeFinalizeError(f"frozen Python extraction {label} exceeds bounds")
                    if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                        raise MergeFinalizeError("frozen Python extraction refused: " + errors.decode(errors="replace"))
                    return json.loads(output)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()


def _candidate_git(root: Path, candidate: Path, *args: str, input: bytes | None = None):
    env = dict(os.environ, GIT_INDEX_FILE=str(candidate))
    result = subprocess.run(["git", "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks",
                             "-c", "core.fsmonitor=false", "-C", str(root), *args],
                            env=env, input=input, capture_output=True, check=True, timeout=30)
    return result.stdout


def _record_path(root: Path, output: str) -> Path:
    selector = hashlib.sha256(output.encode()).hexdigest()
    return _git_path(root, f"graphify-merge-finalize-{selector}.json")


def _read_record(path: Path):
    from graphify.portable import parse_json

    if not os.path.lexists(path):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > 65536
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise MergeFinalizeError("unsafe private finalization record")
        payload = stream.read(65537)
    record = parse_json(payload, canonical=True)
    fields = {"version", "output", "head", "merge_head", "pending_digest", "receipt_digest",
              "output_identity", "git_identity", "prior_entries", "prepared_entries", "content_id"}
    if (not isinstance(record, dict) or set(record) != fields
            or type(record["version"]) is not int or record["version"] != 1):
        raise MergeFinalizeError("malformed private finalization record")
    from graphify.portable import PORTABLE_FILE
    for field in ("prior_entries", "prepared_entries"):
        entries = record[field]
        if not isinstance(entries, dict) or set(entries) != {"graph.json", "manifest.json", PORTABLE_FILE}:
            raise MergeFinalizeError("malformed private finalization inventory")
        for entry in entries.values():
            if entry is None and field == "prior_entries":
                continue
            if (not isinstance(entry, list) or len(entry) != 2 or entry[0] != "100644"
                    or not isinstance(entry[1], str) or len(entry[1]) not in (40, 64)
                    or any(c not in "0123456789abcdef" for c in entry[1])):
                raise MergeFinalizeError("malformed private finalization object binding")
    return record, payload, info


def _record_binding(root, output, state, admission):
    return {"version": 1, "output": output, "head": state[0], "merge_head": state[1].decode("ascii"),
            "pending_digest": admission.pending_payload_digest,
            "receipt_digest": admission.predecessor_receipt_digest,
            "output_identity": admission.output_identity.json(), "git_identity": _git_identity(root)}


def _check_record(root, record, output, state, admission):
    if any(record.get(key) != value for key, value in _record_binding(root, output, state, admission).items()):
        raise MergeFinalizeError("private finalization record belongs to changed or foreign merge state")
    prior_graph = record["prior_entries"]["graph.json"]
    if prior_graph is None:
        raise MergeFinalizeError("private finalization record lacks the admitted pending predecessor")


def _index_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _replace_index(root, output, index, info, frozen, state, admission, candidate, before_publish, git_identity):
    lock = index.with_name(index.name + ".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    lock_info = os.fstat(fd)
    published = False
    try:
        if (_git_identity(root) != git_identity or _index_identity(index.lstat()) != _index_identity(info) or index.read_bytes() != frozen
                or _merge_state(root) != state or _admit(root, output, state)[1] != admission):
            raise MergeFinalizeError("finalizer inputs changed before index publication")
        before_publish()
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(candidate.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        if _git_identity(root) != git_identity:
            raise MergeFinalizeError("repository or worktree identity changed before publication")
        os.replace(lock, index)
        published = True
    finally:
        if fd >= 0:
            os.close(fd)
        if not published and os.path.lexists(lock):
            current_lock = lock.lstat()
            if (current_lock.st_dev, current_lock.st_ino) == (lock_info.st_dev, lock_info.st_ino):
                lock.unlink()


def _write_record(path: Path, record):
    from graphify.hook_installation import _rename
    from graphify.portable import canonical_json

    payload = canonical_json(record)
    if len(payload) > 65536:
        raise MergeFinalizeError("private finalization record exceeds bounds")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Use the existing exclusive rename primitive: a concurrent or
            # foreign record must never be overwritten by this preparation.
            _rename(directory, Path(temporary).name, directory, path.name)
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _same_record(path, captured):
    current = _read_record(path)
    return (current is not None and current[1] == captured[1]
            and _index_identity(current[2]) == _index_identity(captured[2]))


def _remove_record(path, captured):
    if not _same_record(path, captured):
        raise MergeFinalizeError("private finalization record changed; record retained")
    path.unlink()


def _require_full_index(root: Path) -> None:
    _require_local_object_storage()
    if os.name != "posix" or any(name in os.environ for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
        raise MergeFinalizeError("only the default full POSIX index is supported")
    for option in ("core.sparseCheckout", "core.splitIndex", "index.sparse"):
        result = subprocess.run(["git", "-C", str(root), "config", "--get", option], capture_output=True,
                                check=False, timeout=30)
        if result.returncode not in (0, 1):
            raise MergeFinalizeError("cannot classify supported index settings")
        if result.stdout.strip().lower() in (b"true", b"1", b"yes", b"on"):
            raise MergeFinalizeError("sparse and split indexes are unsupported")
    if _git(root, "rev-parse", "--shared-index-path").strip():
        raise MergeFinalizeError("split indexes are unsupported")


def _require_plain_output_entries(root: Path, output: str) -> None:
    records = _git(root, "ls-files", "-v", "-z", "--", f":(top,literal){output}")
    if any(record[:1].islower() or record[:1] == b"S" for record in records.split(b"\0") if record):
        raise MergeFinalizeError("assume-unchanged and skip-worktree output index flags are unsupported")


def _publication_hooks(root: Path, output: str):
    from graphify import hooks

    directory = _git_path(root, "hooks")
    post_scripts = hooks._post_event_scripts(output, output, sys.executable)
    markers = {
        "post-commit": (hooks._HOOK_MARKER, hooks._HOOK_MARKER_END),
        "post-checkout": (hooks._CHECKOUT_MARKER, hooks._CHECKOUT_MARKER_END),
        "post-merge": (hooks._POST_MERGE_MARKER, hooks._POST_MERGE_MARKER_END),
    }
    captured = {}
    for name in ("pre-commit", "pre-merge-commit", *post_scripts):
        hook = directory / name
        snapshot = None
        if os.path.lexists(hook):
            if hook.is_symlink() or not hook.is_file():
                raise MergeFinalizeError(f"unsupported {name} hook object")
            snapshot = hook.stat().st_mode, hook.read_bytes()
        captured[name] = snapshot
        if name not in post_scripts:
            expected = ("#!/bin/sh\n" + hooks._merge_guard_script(name, output, sys.executable)).encode()
            if snapshot is None or not os.access(hook, os.X_OK) or snapshot[1] != expected:
                raise MergeFinalizeError(
                    "current executable standalone merge-guard hooks are required; "
                    "run graphify hook install --merge-guard"
                )
        elif snapshot is not None and os.access(hook, os.X_OK):
            try:
                owned = hooks._owned_hook_span(snapshot[1], *markers[name], f"{name} hook")
            except RuntimeError as exc:
                raise MergeFinalizeError(str(exc)) from exc
            if owned is not None and snapshot[1][owned[0]:owned[1]] != post_scripts[name].encode():
                raise MergeFinalizeError(
                    f"stale managed {name} hook; run graphify hook install --merge-guard"
                )
    return captured


def validate_prepared_merge(root: Path, output: str) -> None:
    """Bind the current portable candidate to this merge's explicit preparation."""
    _publication_hooks(root, output)
    _output_attributes(root, output)
    snapshot = validate_index_bundle(root, output)
    state = _merge_state(root)
    _payload, admission = _admit(root, output, state)
    captured = _read_record(_record_path(root, output))
    if captured is None:
        raise MergeFinalizeError("portable staged bundle lacks its private finalization record")
    record = captured[0]
    _check_record(root, record, output, state, admission)
    selected = {p[len(output) + 1:]: list(value) for p, value in _entries(root).items()
                if p.startswith(output + "/")}
    if selected != record["prepared_entries"] or snapshot.content_id != record["content_id"]:
        raise MergeFinalizeError("staged bundle differs from the prepared finalization record")


def finalize_merge(root: Path, output: str) -> str:
    """Stage a validated portable closure; do not commit or rewrite local output."""
    from graphify.portable import PORTABLE_FILE, make_bundle, source_records_from_blobs, validate_bundle

    root = root.absolute()
    output = _output_selector(output)
    try:
        _require_full_index(root)
        _output_root(root, output)
        _require_plain_output_entries(root, output)
        hook_inputs = _publication_hooks(root, output)
        git_identity = _git_identity(root)
        attributes = _output_attributes(root, output)
        index = _git_path(root, "index")
        info = index.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MergeFinalizeError("a regular full index is required")
        frozen = index.read_bytes()
        state = _merge_state(root)
        entries = _entries(root)
        if any(mode == "040000" for mode, _oid in entries.values()):
            raise MergeFinalizeError("sparse indexes are unsupported")
        closure = {"graph.json", "manifest.json", PORTABLE_FILE}
        selected = {p[len(output)+1:]: value for p, value in entries.items() if p.startswith(output + "/")}
        if set(selected) - closure or any(mode != "100644" for mode, _oid in selected.values()):
            raise MergeFinalizeError("tracked output contains unsupported siblings or modes")
        payload, admission = _admit(root, output, state)
        record_path = _record_path(root, output)
        captured_record = _read_record(record_path)
        if captured_record is not None:
            _check_record(root, captured_record[0], output, state, admission)
        blobs = _source_blobs(root, entries, output)
        sources = source_records_from_blobs(blobs, output)
        if PORTABLE_FILE in selected:
            if captured_record is None:
                raise MergeFinalizeError("prepared output lacks this operation's private finalization record")
            snapshot = validate_index_bundle(root, output)
            # A retry still needs unchanged merge operands and live pending
            # authority. Recompute exact extraction below before accepting it.
        elif selected.get("graph.json") is None or _blob(root, selected["graph.json"][1]) != payload:
            raise MergeFinalizeError("stage the exact admitted pending graph before finalization")
        bundle = make_bundle(_extract(root, blobs), sources, output)
        snapshot = validate_bundle(bundle, sources, output)
        if PORTABLE_FILE in selected:
            assert captured_record is not None
            if captured_record[0]["content_id"] != snapshot.content_id:
                raise MergeFinalizeError("private finalization content binding changed")
            if any(tuple(captured_record[0]["prepared_entries"][name]) != value
                   for name, value in selected.items()):
                raise MergeFinalizeError("prepared output object bindings changed")
            if any(_blob(root, selected[name][1]) != data for name, data in bundle.items()):
                raise MergeFinalizeError("prepared bundle inputs changed; cancellation or fresh admission required")
            if _git_identity(root) != git_identity or index.read_bytes() != frozen or _merge_state(root) != state or _admit(root, output, state)[1] != admission:
                raise MergeFinalizeError("finalizer inputs changed during retry")
            return snapshot.content_id
        if _git_identity(root) != git_identity:
            raise MergeFinalizeError("repository or worktree identity changed during preparation")
        with tempfile.TemporaryDirectory(prefix="graphify-index-", dir=index.parent) as directory:
            candidate = Path(directory) / "index"
            candidate.write_bytes(frozen)
            prepared_entries = {}
            for name, data in bundle.items():
                oid = _git(root, "hash-object", "-w", "--stdin", input=data).decode().strip()
                prepared_entries[name] = ["100644", oid]
                _candidate_git(root, candidate, "update-index", "--add", "--cacheinfo", f"100644,{oid},{output}/{name}")
            record = dict(_record_binding(root, output, state, admission),
                          prior_entries={name: list(selected[name]) if name in selected else None for name in closure},
                          prepared_entries=prepared_entries, content_id=snapshot.content_id)
            if captured_record is not None and captured_record[0] != record:
                raise MergeFinalizeError("interrupted preparation inputs changed; explicit cancellation required")
            def before_publish():
                if _output_attributes(root, output) != attributes:
                    raise MergeFinalizeError("output conversion attributes changed")
                if _publication_hooks(root, output) != hook_inputs:
                    raise MergeFinalizeError("hook inputs changed before publication")
                if captured_record is None:
                    _write_record(record_path, record)
                elif not _same_record(record_path, captured_record):
                    raise MergeFinalizeError("private finalization record changed")
            _replace_index(root, output, index, info, frozen, state, admission, candidate, before_publish, git_identity)
        return snapshot.content_id
    except (MergeGuardError, transaction.PendingTransactionError, OSError, ValueError,
            subprocess.SubprocessError) as exc:
        raise MergeFinalizeError(str(exc)) from exc


def cancel_merge(root: Path, output: str) -> None:
    """Restore only this operation's original staged output before Git cancellation."""
    root = root.absolute()
    output = _output_selector(output)
    try:
        _require_full_index(root)
        _require_plain_output_entries(root, output)
        state = _merge_state(root)
        _payload, admission = _admit(root, output, state)
        path = _record_path(root, output)
        captured = _read_record(path)
        if captured is None:
            raise MergeFinalizeError("no private finalization record exists for this output")
        record = captured[0]
        git_identity = _git_identity(root)
        _check_record(root, record, output, state, admission)
        index = _git_path(root, "index")
        info = index.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MergeFinalizeError("a regular full index is required")
        frozen = index.read_bytes()
        entries = _entries(root)
        selected = {p[len(output)+1:]: list(value) for p, value in entries.items() if p.startswith(output + "/")}
        prepared = record["prepared_entries"]
        prior = {name: value for name, value in record["prior_entries"].items() if value is not None}
        if selected not in (prepared, prior):
            raise MergeFinalizeError("staged output changed; cancellation refuses to overwrite it")
        if hashlib.sha256(_blob(root, prior["graph.json"][1])).hexdigest() != admission.pending_payload_digest:
            raise MergeFinalizeError("private original graph binding changed")
        with tempfile.TemporaryDirectory(prefix="graphify-cancel-", dir=index.parent) as directory:
            candidate = Path(directory) / "index"
            candidate.write_bytes(frozen)
            for name, entry in record["prior_entries"].items():
                if entry is None:
                    _candidate_git(root, candidate, "update-index", "--force-remove", "--", f"{output}/{name}")
                else:
                    mode, oid = entry
                    _blob(root, oid)
                    _candidate_git(root, candidate, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{output}/{name}")
            _refresh_candidate(root, candidate, output)
            def before_publish():
                _output_attributes(root, output)
                if not _same_record(path, captured):
                    raise MergeFinalizeError("private finalization record changed")
            _replace_index(root, output, index, info, frozen, state, admission, candidate, before_publish, git_identity)
            _remove_record(path, captured)
    except (MergeGuardError, transaction.PendingTransactionError, OSError, ValueError,
            subprocess.SubprocessError) as exc:
        raise MergeFinalizeError(str(exc)) from exc


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    output = args.output
    if args.observe and Path(output).is_absolute():
        try:
            output = Path(output).relative_to(root).as_posix()
        except ValueError:
            return 0
    try:
        if args.observe:
            return 10 if observe_committed_portable(root, output) else 0
        if args.cancel:
            cancel_merge(root, output)
            print("[graphify] original output staging restored; use git merge --abort to cancel the merge")
            return 0
        content = finalize_merge(root, output)
        print(f"[graphify] staged portable bundle {content}; commit remains manual; local reconciliation deferred")
    except (MergeFinalizeError, MergeGuardError, ValueError, RuntimeError) as exc:
        print(f"[graphify merge finalize] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
