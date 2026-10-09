"""Read-only containment for tracked pending graphs at ordinary merge commits.

This is not a finalizer or a portable-receipt validator. Git supplies the
effective index through its environment; no working-tree graph is opened.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

from graphify.security import _max_graph_file_bytes

_EVENTS = ("pre-commit", "pre-merge-commit")


class MergeGuardError(RuntimeError):
    """The selected staged graph cannot safely pass ordinary merge containment."""


def _git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    # Inspect the literal committed object and exact path, while retaining Git's
    # effective repository/index selection (including GIT_INDEX_FILE).
    env = os.environ.copy()
    for name in ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
                 "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"):
        env.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "-c",
             "core.fsmonitor=false", "-C", str(root), *args],
            capture_output=True, check=True, timeout=30, env=env, input=input,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MergeGuardError("cannot inspect the effective Git index; no graph was changed") from exc
    return result.stdout


def _git_path(root: Path, name: str) -> Path:
    raw = os.fsdecode(_git(root, "rev-parse", "--git-path", name)).rstrip("\n")
    if not raw or "\n" in raw or "\r" in raw:
        raise MergeGuardError("cannot resolve the ordinary merge state")
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def check_merge_commit(output: str, event: str, *, root: Path = Path("."),
                       entry_header: str | None = None) -> None:
    """Reject a pending staged graph without changing Git or Graphify state.

Only ordinary merge boundaries are covered. Valid legacy/active graphs are
not qualified here; passing this guard does not establish clone readability.
"""
    if event not in _EVENTS:
        raise MergeGuardError("unsupported merge guard event")
    if entry_header is not None and event != "pre-merge-commit":
        raise MergeGuardError("a captured entry is only supported for automatic merge hooks")
    if os.environ.get("GRAPHIFY_SKIP_HOOK") == "1":
        return
    root = root.absolute()
    for name in ("rebase-merge", "rebase-apply", "sequencer", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if os.path.lexists(_git_path(root, name)):
            return
    if event == "pre-commit" and not os.path.lexists(_git_path(root, "MERGE_HEAD")):
        return
    relative = PurePosixPath(output)
    if (not output or relative.is_absolute() or ".." in relative.parts
            or "\\" in output or "\x00" in output or relative == PurePosixPath(".")):
        raise MergeGuardError("merge guard requires a repository-relative output")
    graph = (relative / "graph.json").as_posix()
    # ls-files exposes intent-to-add placeholders as empty blobs, although Git
    # omits them from the committed tree. Compare only the effective index with
    # the empty tree, including unchanged tracked files without reading HEAD or
    # the working tree. hash-object without -w does not publish an object.
    if entry_header is None:
        empty_tree = _git(root, "hash-object", "-t", "tree", "--stdin", input=b"").decode("ascii").strip()
        records = _git(root, "diff-index", "--cached", "--ita-invisible-in-index",
                       "--raw", "-z", "-r", "--no-abbrev", "--no-ext-diff",
                       "--no-textconv", "--no-renames", "--no-relative", empty_tree,
                       "--", f":(top,literal){graph}")
        if not records:
            return
    try:
        if entry_header is None:
            header, path, end = records.split(b"\0")
            if path != os.fsencode(graph) or end:
                raise ValueError("unsafe index entry")
        else:
            # The generated hook already selected the literal output path.
            # Keep its first mode/OID observation across interpreter discovery;
            # a later active or absent index cannot conceal a pending object.
            header = entry_header.encode("ascii")
        old_mode, mode, old_oid, oid, state = header.split()
        if (old_mode != b":000000" or any(c != ord("0") for c in old_oid)
                or mode not in (b"100644", b"100755") or state != b"A"):
            raise ValueError("unsafe index entry")
        object_id = oid.decode("ascii")
        if len(object_id) not in (40, 64) or any(c not in "0123456789abcdef" for c in object_id):
            raise ValueError("invalid object id")
        size = int(_git(root, "cat-file", "-s", object_id))
        if size < 1 or size > _max_graph_file_bytes():
            raise ValueError("unsupported graph size")
        payload = _git(root, "cat-file", "blob", object_id)
        if len(payload) != size:
            raise ValueError("graph size changed")
        # Match ordinary graph readers; bytes parsing would also admit BOMs and
        # UTF-16/UTF-32 streams that those readers reject.
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise ValueError("invalid graph shape")
        metadata = data.get("graph", {})
        if not isinstance(metadata, dict):
            raise ValueError("invalid graph metadata")
        watermark = metadata.get("_graphify_protocol")
        if "_graphify_protocol" in metadata:
            if (not isinstance(watermark, dict)
                    or type(watermark.get("schema")) is not int or watermark["schema"] != 1
                    or type(watermark.get("protocol_epoch")) is not int or watermark["protocol_epoch"] != 1
                    or watermark.get("state") not in ("active", "merge_pending")):
                raise ValueError("invalid graph watermark")
            if (watermark["state"] == "active"
                    and type(watermark.get("generation")) is not int):
                raise ValueError("invalid active graph watermark generation")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MergeGuardError(f"cannot classify staged {graph}: {exc}; merge commit refused") from exc
    if watermark is not None and watermark.get("state") == "merge_pending":
        raise MergeGuardError(
            f"staged {graph} is merge_pending; merge commit refused. "
            "This guard does not finalize graphs. Repeating git commit alone will not repair it. "
            "Keep the merge uncommitted for an explicit repair, or use Git's merge cancellation. "
            "No graph, receipt, or index was changed by Graphify."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=_EVENTS, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--entry-header", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        check_merge_commit(args.output, args.event, entry_header=args.entry_header)
    except MergeGuardError as exc:
        print(f"[graphify merge guard] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
