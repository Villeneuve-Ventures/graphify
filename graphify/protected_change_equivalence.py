"""Compare pinned review inputs across a commit; never grant approval or authority.

Acquisition of complete immutable manifests, receipt authenticity, and the decision
that all required checks are reusable remain the protected policy owner's duties.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time

from graphify.protected_change_verifier import (
    SUPPORTED_MODES,
    _acquire_regular_file,
    _is_reserved_git_admin_component,
    canonical_json,
)


VERSION = "graphify.protected-change-equivalence.v1"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PATHS = 20_000
_INPUTS = {"approved", "committed", "commit_object", "validation_before", "validation_after"}


def _require(condition: bool, invariant: str) -> None:
    if not condition:
        raise ValueError(invariant)


def _keys(value, keys: set[str]) -> None:
    _require(type(value) is dict and value.keys() == keys, "schema.keys")


def _digest(value, length: int = 64) -> None:
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value)
             is not None and value != "0" * length, "schema.digest")


def _label(value) -> None:
    _require(type(value) is str and 0 < len(value) <= 256 and not value.isspace(), "schema.label")


def _number(value) -> None:
    _require(type(value) is int and 0 <= value <= 2**53 - 1, "schema.integer")


def _decode(raw: bytes):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_INPUT_BYTES, "input.size")
    try:
        value = json.loads(raw)
        _require(canonical_json(value) == raw, "input.canonical")
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError("input.canonical") from exc


def _content(value, *, git: bool) -> None:
    if value is None:
        return
    keys = {"bytes", "mode", "sha256", "type"} | ({"oid"} if git else set())
    _keys(value, keys)
    _number(value["bytes"])
    _digest(value["sha256"])
    _require(value["mode"] in SUPPORTED_MODES, "content.mode")
    _require(value["type"] == ("symlink" if value["mode"] == "120000" else "blob"),
             "content.type")
    if git:
        _digest(value["oid"], 40)


def _manifest(raw: bytes):
    value = _decode(raw)
    _keys(value, {"schema", "acceptance_packet", "policy", "base_oid", "head_oid", "paths",
                  "status_porcelain_v2_z_base64", "tracked_binary_diff_sha256"})
    _require(value["schema"] == "graphify.protected-change-review.candidate.v2", "manifest.schema")
    for key in ("base_oid", "head_oid"):
        _digest(value[key], 40)
    _digest(value["tracked_binary_diff_sha256"])
    _keys(value["policy"], {"version", "sha256"})
    _require(value["policy"]["version"] == "graphify.protected-change-review.policy.v3", "policy.version")
    _digest(value["policy"]["sha256"])
    packet = value["acceptance_packet"]
    _keys(packet, {"schema_version", "schema_sha256", "version", "sha256"})
    for key in ("schema_version", "version"):
        _label(packet[key])
    for key in ("schema_sha256", "sha256"):
        _digest(packet[key])
    status = value["status_porcelain_v2_z_base64"]
    _require(type(status) is str, "status.encoding")
    try:
        decoded = base64.b64decode(status, validate=True)
        _require(base64.b64encode(decoded).decode() == status, "status.encoding")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("status.encoding") from exc
    # Full status grammar/completeness is checked during policy-owned acquisition.
    _require(not decoded or (decoded.endswith(b"\0") and b"\0\0" not in decoded), "status.framing")
    rows = value["paths"]
    _require(type(rows) is list and 0 < len(rows) <= MAX_PATHS, "paths.size")
    previous = b""
    layer_paths = {layer: set() for layer in ("base", "head", "index", "worktree")}
    for row in rows:
        _keys(row, {"path", "base", "head", "index", "worktree"})
        path = row["path"]
        _require(type(path) is str and len(path) <= 4096, "path.size")
        parts = path.split("/")
        _require(0 < len(parts) <= 64 and all(p not in {"", ".", ".."} and "\0" not in p
                 and not _is_reserved_git_admin_component(p) for p in parts), "path.shape")
        encoded = path.encode("utf-8")
        _require(encoded > previous, "path.order")
        previous = encoded
        _require(any(row[layer] is not None for layer in layer_paths), "path.empty")
        for layer, paths in layer_paths.items():
            item = row[layer]
            _content(item, git=layer != "worktree")
            if item is not None:
                _require(all("/".join(parts[:i]) not in paths for i in range(1, len(parts))),
                         "path.conflict")
                paths.add(path)
    return value


def _tree_oid(rows: list[dict]) -> str:
    root = {}
    for row in rows:
        item = row["head"]
        if item is None:
            continue
        parts = row["path"].split("/")
        current = root
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = (item["mode"].lstrip("0"), item["oid"])

    def digest(tree):
        entries = []
        for name, item in tree.items():
            directory = type(item) is dict
            mode, oid = ("40000", digest(item)) if directory else item
            raw_name = name.encode("utf-8")
            entries.append((raw_name + (b"/" if directory else b"\0"),
                            mode.encode() + b" " + raw_name + b"\0" + bytes.fromhex(oid)))
        data = b"".join(record for _, record in sorted(entries))
        return hashlib.sha1(b"tree " + str(len(data)).encode() + b"\0" + data).hexdigest()

    return digest(root)


def _validation(raw: bytes, candidate_digest: str):
    value = _decode(raw)
    _keys(value, {"schema", "candidate_sha256", "inputs_sha256", "environment_sha256",
                  "runtime_sha256", "checks"})
    _require(value["schema"] == "graphify.protected-change-review.validation-context.v1", "validation.schema")
    _require(value["candidate_sha256"] == candidate_digest, "validation.candidate")
    for key in ("inputs_sha256", "environment_sha256", "runtime_sha256"):
        _digest(value[key])
    checks = value["checks"]
    _require(type(checks) is list and 0 < len(checks) <= 256, "validation.checks")
    names = set()
    for check in checks:
        _keys(check, {"name", "command_sha256", "receipt_sha256", "result",
                      "head_sensitive", "valid_until"})
        _label(check["name"])
        _require(check["name"] not in names, "validation.duplicate")
        names.add(check["name"])
        _digest(check["command_sha256"])
        _digest(check["receipt_sha256"])
        _number(check["valid_until"])
        _require(check["valid_until"] > time.time(), "validation.expired")
        _require(check["result"] == "passed" and check["head_sensitive"] is False,
                 "validation.not_reusable")
    return {key: item for key, item in value.items() if key != "candidate_sha256"}


def verify_commit_equivalence(*, approved: bytes, committed: bytes, commit_object: bytes,
                              validation_before: bytes, validation_after: bytes,
                              expected_digests: dict[str, str]) -> bytes:
    """Return comparison evidence for externally pinned, independently observed inputs.

    This does not acquire a repository, authenticate receipts/reviewers, establish
    an exhaustive validation plan, or authorize carrying an approval forward.
    """
    inputs = {"approved": approved, "committed": committed, "commit_object": commit_object,
              "validation_before": validation_before, "validation_after": validation_after}
    _keys(expected_digests, _INPUTS)
    for name, raw in inputs.items():
        limit = 1024 * 1024 if name == "commit_object" else MAX_INPUT_BYTES
        _require(type(raw) is bytes and 0 < len(raw) <= limit, "input.size")
        _digest(expected_digests[name])
        _require(hashlib.sha256(raw).hexdigest() == expected_digests[name], "input.digest")
    before, after = _manifest(approved), _manifest(committed)
    for key in ("base_oid", "policy", "acceptance_packet"):
        _require(before[key] == after[key], "transition.contract")
    _require(before["head_oid"] != after["head_oid"], "transition.same_head")
    _require(after["status_porcelain_v2_z_base64"] == "", "transition.dirty")
    _require(len(before["paths"]) == len(after["paths"]), "transition.inventory")
    for old, new in zip(before["paths"], after["paths"], strict=True):
        for key in ("path", "base", "worktree", "index"):
            _require(old[key] == new[key], "transition.content")
        _require(new["head"] == old["index"], "transition.staged")
        if old["index"] is not None:
            _require({k: v for k, v in old["index"].items() if k != "oid"} == old["worktree"],
                     "transition.partial_staging")
    _require(len(commit_object) <= 1024 * 1024 and b"\n\n" in commit_object, "commit.framing")
    headers = commit_object.split(b"\n\n", 1)[0].split(b"\n")
    parents = [line[7:] for line in headers if line.startswith(b"parent ")]
    trees = [line[5:] for line in headers if line.startswith(b"tree ")]
    _require(parents == [before["head_oid"].encode()], "commit.parent")
    tree = _tree_oid(after["paths"])
    _require(trees == [tree.encode()] and headers[0] == b"tree " + tree.encode(), "commit.tree")
    oid = hashlib.sha1(b"commit " + str(len(commit_object)).encode() + b"\0" + commit_object).hexdigest()
    _require(oid == after["head_oid"], "commit.oid")
    old_context = _validation(validation_before, expected_digests["approved"])
    new_context = _validation(validation_after, expected_digests["committed"])
    _require(old_context == new_context, "validation.changed")
    return canonical_json({"schema": VERSION, "result": "equivalent", "approval_granted": False,
                           "inputs": dict(expected_digests), "parent_oid": before["head_oid"],
                           "head_oid": after["head_oid"], "tree_oid": tree})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in sorted(_INPUTS | {"digests"}):
        parser.add_argument("--" + name.replace("_", "-"), required=True, metavar="PATH")
    args = vars(parser.parse_args(argv))
    try:
        raw = {name: _acquire_regular_file(path, maximum_bytes=(1024 * 1024
                                         if name == "commit_object" else MAX_INPUT_BYTES),
                                         chunk_bytes=1024 * 1024)[0]
               for name, path in args.items()}
        result = verify_commit_equivalence(**{k: v for k, v in raw.items() if k != "digests"},
                                           expected_digests=_decode(raw["digests"]))
    except (ValueError, RuntimeError, OSError, TypeError, RecursionError):
        sys.stdout.buffer.write(canonical_json({"schema": VERSION, "result": "rejected",
                                                "approval_granted": False}))
        return 1
    sys.stdout.buffer.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
