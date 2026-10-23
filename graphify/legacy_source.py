"""Pure structural payload preparation for an explicitly requested legacy build.

This module grants no output or workspace lifecycle authority. It returns fresh
bytes, consumes only rooted SourceIO inputs, and does not lock source writers or
claim an atomic snapshot. Reobservation repeats the exact consumed operations.
The fixed limits below also apply to detection, including inputs later filtered
out of structural extraction. No caller option can loosen them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat

from graphify.source_io import SourceChanged, SourceIO, SourceUnsupported, engine_inputs
from graphify.workspace.contracts import (
    InputManifest, MAX_ENTRIES, MAX_FILE_BYTES, MAX_TOTAL_BYTES,
)


BOUNDS = {
    "max_file_bytes": MAX_FILE_BYTES,
    "max_total_bytes": MAX_TOTAL_BYTES,
    "max_entries": MAX_ENTRIES,
    "max_payload_bytes": 128 * 1024 * 1024,
}
_REQUEST_FIELDS = {
    "schema", "root", "output", "include", "exclude", "extra_roots",
    "extraction_profile", "artifact_profile",
}


@dataclass(frozen=True)
class SourceBuild:
    payloads: dict[str, bytes]
    evidence: dict
    evidence_digest: str
    selected: tuple[str, ...]
    outcomes: list[dict]
    uncovered: dict


def _directory(value, field, *, require_exists=True):
    if (not isinstance(value, str) or not value or "//" in value
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise ValueError(f"{field} must be an absolute canonical directory")
    path = Path(value)
    try:
        valid = (path.is_absolute() and str(path) == value
                 and os.path.abspath(value) == value)
        if valid and require_exists:
            valid = path.resolve(strict=True) == path and path.is_dir()
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        raise ValueError(f"{field} must be an absolute canonical directory")
    return value


def _patterns(value, field, *, nonempty=False):
    if (not isinstance(value, list) or len(value) > 1000
            or (nonempty and not value)):
        raise ValueError(f"{field} must be a bounded relative glob list")
    for pattern in value:
        if (not isinstance(pattern, str) or not pattern or len(pattern) > 4096
                or pattern.startswith("/") or "\\" in pattern or ":" in pattern
                or any(c in pattern for c in "\x00\r\n")
                or any(part in {"", ".", ".."} for part in pattern.split("/"))):
            raise ValueError(f"{field} must contain relative globs")
        # Validate the glob syntax before any source is consumed.
        PurePosixPath("probe").full_match(pattern)
    return sorted(set(value))


def validate_request(request, *, require_sources: bool = True) -> dict:
    """Copy and validate the complete declarative request; infer no roots.

    Globs are anchored PurePosixPath.full_match patterns: ``*.py`` matches root
    files and ``**/*.py`` also matches descendants. ``extra_roots`` is an explicit
    list of {label, path} directories, with labels ``git`` and/or ``policy``.

    ``require_sources=False`` is internal validation for byte-only sealed replay:
    source and extra-root strings retain exact lexical syntax validation without
    reopening or dereferencing those directories. Output validation stays live.
    This does not authorize any source read; build and reobserve use the default.
    """
    if type(require_sources) is not bool:
        raise ValueError("require_sources must be a boolean")
    if (not isinstance(request, dict)
            or set(request) not in (_REQUEST_FIELDS, _REQUEST_FIELDS | {"bounds"})):
        raise ValueError("explicit complete structural request required")
    if type(request["schema"]) is not int or request["schema"] != 1:
        raise ValueError("unsupported request schema")
    if (request["extraction_profile"] != "structural-v1"
            or request["artifact_profile"] != "core-v1"):
        raise ValueError("unsupported source or artifact profile")
    if "bounds" in request and (
        not isinstance(request["bounds"], dict) or request["bounds"] != BOUNDS
        or any(type(n) is not int for n in request["bounds"].values())
    ):
        raise ValueError("source bounds are fixed")
    extra = request["extra_roots"]
    if not isinstance(extra, list) or len(extra) > 2:
        raise ValueError("explicit bounded extra_roots list required")
    roots = []
    for entry in extra:
        if (not isinstance(entry, dict) or set(entry) != {"label", "path"}
                or not isinstance(entry["label"], str)
                or entry["label"] not in {"git", "policy"}):
            raise ValueError("invalid extra root declaration")
        roots.append({"label": entry["label"], "path": _directory(
            entry["path"], "extra root", require_exists=require_sources)})
    if (len({entry["label"] for entry in roots}) != len(roots)
            or len({entry["path"] for entry in roots}) != len(roots)):
        raise ValueError("duplicate extra root")
    root = _directory(request["root"], "root", require_exists=require_sources)
    output = _directory(request["output"], "output")
    if Path(root).is_relative_to(Path(output)):
        raise ValueError("output must not equal or contain the source root")
    return {
        "schema": 1,
        "root": root,
        "output": output,
        "include": _patterns(request["include"], "include", nonempty=True),
        "exclude": _patterns(request["exclude"], "exclude"),
        "extra_roots": sorted(roots, key=lambda entry: entry["label"]),
        "extraction_profile": "structural-v1", "artifact_profile": "core-v1",
        "bounds": dict(BOUNDS),
    }


class _OperationalInputs(SourceIO):
    """Exclude the selected output and the caller's pinned preparation workspace.

    This is not request-declared source exclusion. The trusted transaction caller
    must first validate ownership with ``_pin_prepared_workspace`` and retain its
    output lock. These identities merely preserve that exact exclusion during
    observation; this module cannot confer or discover transaction ownership.
    Validation uses no-follow descriptors and adds no source-consumption records.
    The selected output retains its parent-list binding, but its contents are
    outside the source-input namespace, including optional metadata probes.
    """

    def __init__(self, *args, output, operational_dirs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.output = Path(output)
        self.output_binding = self._directory_binding(self.output, "output")
        if operational_dirs is None:
            operational_dirs = {}
        if not isinstance(operational_dirs, Mapping) or len(operational_dirs) > MAX_ENTRIES:
            raise ValueError("invalid pinned operational directories")
        self.operational_dirs = {}
        for path, identity in operational_dirs.items():
            if (not isinstance(path, Path) or not path.is_absolute()
                    or str(path) != os.path.abspath(path)
                    or not isinstance(identity, tuple) or len(identity) != 2
                    or any(type(value) is not int or value < 0 for value in identity)):
                raise ValueError("invalid pinned operational directory identity")
            self.operational_dirs[path] = identity

    def _directory_binding(self, path, kind):
        fd = None
        try:
            fd = os.open(path.anchor, self._dir_flags())
            for part in path.parts[1:]:
                child = os.open(part, self._dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                self.refuse(f"{kind} directory is unsafe", SourceChanged)
            return info.st_dev, info.st_ino, info.st_mode
        except OSError:
            self.refuse(f"{kind} directory is unavailable or unsafe", SourceChanged)
        finally:
            if fd is not None:
                os.close(fd)

    def _check_operational_dirs(self):
        for path, expected in self.operational_dirs.items():
            if self._directory_binding(path, "operational")[:2] != expected:
                self.refuse("operational directory identity changed", SourceChanged)

    def _check_output(self):
        if self._directory_binding(self.output, "output") != self.output_binding:
            self.refuse("output directory identity changed", SourceChanged)

    def _absolute_input(self, path):
        path = Path(path)
        return Path(os.path.abspath(path if path.is_absolute() else self.root / path))

    def _in_output(self, path):
        return self._absolute_input(path).is_relative_to(self.output)

    def probe(self, path):
        self.check()
        if self._in_output(path):
            self._check_output()
            # Excluded namespace, not evidence of physical file absence. This
            # also keeps detect's operational-metadata observer out of output.
            return None
        return super().probe(path)

    def read_bytes(self, path, *, max_bytes=None):
        if self._in_output(path):
            self.refuse("selected output is outside source inputs")
        return super().read_bytes(path, max_bytes=max_bytes)

    def _record(self, operation, key, value):
        if operation == "list":
            label, relative = key.split(":", 1) if ":" in key else ("source", key)
            directory = self.roots[label] / relative
            members = []
            for name, identity in value[1]:
                child = directory / name
                if child == self.output and identity != self.output_binding:
                    self.refuse("listed output directory identity changed", SourceChanged)
                expected = self.operational_dirs.get(child)
                if expected is None:
                    members.append((name, identity))
                elif (identity[:2] != expected or not stat.S_ISDIR(identity[2])):
                    self.refuse("listed operational directory identity changed", SourceChanged)
            value = (value[0], tuple(members))
        return super()._record(operation, key, value)

    def listdir(self, path):
        if self._in_output(path):
            self.refuse("selected output is outside source inputs")
        self._check_output()
        self._check_operational_dirs()
        entries = super().listdir(path)
        self._check_operational_dirs()
        self._check_output()
        return [(child, mode) for child, mode in entries
                if self._absolute_input(child) not in self.operational_dirs
                and self._absolute_input(child) != self.output]


class _BuildInputs(_OperationalInputs):
    """Retain only legacy manifest hashes from bytes the engine actually read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy_hashes = {}

    def read_bytes(self, path, *, max_bytes=None):
        raw = super().read_bytes(path, max_bytes=max_bytes)
        _, _, key = self._locate(path)
        # Legacy incremental manifests use MD5 for change detection, not trust.
        self.legacy_hashes[key] = hashlib.md5(raw, usedforsecurity=False).hexdigest()
        return raw


def _inputs[T: _OperationalInputs](request, cls: type[T], *, operational_dirs=None) -> T:
    return cls(
        request["root"],
        extra_roots={entry["label"]: entry["path"] for entry in request["extra_roots"]},
        max_file_bytes=BOUNDS["max_file_bytes"],
        max_total_bytes=BOUNDS["max_total_bytes"], max_entries=BOUNDS["max_entries"],
        operational_dirs=operational_dirs,
        output=request["output"],
    )


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


class _GraphBuffer(io.StringIO):
    def __init__(self):
        super().__init__()
        self.byte_count = 0

    def write(self, value):
        self.byte_count += len(value.encode("utf-8"))
        if self.byte_count > BOUNDS["max_payload_bytes"]:
            raise SourceUnsupported("structural graph payload byte limit exceeded")
        return super().write(value)


def _graph_bytes(graph):
    from graphify.export import write_json

    with _GraphBuffer() as stream:
        write_json(graph, {}, stream)
        value = json.loads(stream.getvalue())
    # Engine traversal order is not part of the payload contract.
    for field in ("nodes", "links", "hyperedges"):
        value[field].sort(key=_json_bytes)
    if "hyperedges" in value["graph"]:
        value["graph"]["hyperedges"].sort(key=_json_bytes)
    return _json_bytes(value)


def build(request, *, operational_dirs: Mapping[Path, tuple[int, int]] | None = None) -> SourceBuild:
    """Detect the full ordinary corpus, explicitly select, and return fresh bytes.

    The exact requested output is outside the source corpus. Detection otherwise
    retains the existing ignore/sensitive/noise policy. Request globs
    filter that complete detection result afterwards. Selected unclassified or
    unsupported code inputs fail closed. Selected docs/media are reported as
    uncovered, never given structural completion stamps.

    ``operational_dirs`` is reserved for the trusted transaction caller's exact
    ownership-validated live workspace. It is not part of the request schema.
    """
    from graphify.build import build_from_json
    from graphify.detect import detect
    from graphify.extract import extract

    request = validate_request(request)
    root = Path(request["root"])

    def relative(path):
        return Path(path).relative_to(root).as_posix()

    def selected(path):
        name = PurePosixPath(relative(path))
        return (any(name.full_match(pattern) for pattern in request["include"])
                and not any(name.full_match(pattern) for pattern in request["exclude"]))

    with _inputs(request, _BuildInputs, operational_dirs=operational_dirs) as inputs, engine_inputs(inputs, quiet=True):
        detection = detect(root, source_io=inputs, strict=True, quiet=True,
                           read_only=True, ambient_output=False)
        if detection["walk_errors"]:
            raise SourceUnsupported("incomplete source enumeration")
        unknown = [relative(path) for path in detection["unclassified"] if selected(path)]
        if unknown:
            raise SourceUnsupported("selected unclassified inputs: " + ", ".join(unknown))
        code = sorted(path for path in detection["files"]["code"] if selected(path))
        extraction = extract(code, source_io=inputs, source_root=root, strict=True,
                             parallel=False, quiet=True, ambient_output=False)
        consumed = InputManifest.from_engine(inputs, phase="consumed", code_inputs=code,
                                             outcomes=extraction["outcomes"])
        if not consumed.complete:
            raise SourceUnsupported("structural extraction incomplete")
        evidence = consumed.to_dict()
        observed_reads = {record["path"]: record["value"]
                          for record in evidence["evidence"] if record["operation"] == "read"}
        manifest = {
            relative(path): {
                "mtime": observed_reads[relative(path)][0][4] / 1_000_000_000,
                "ast_hash": inputs.legacy_hashes[relative(path)], "semantic_hash": "",
            }
            for path in code
        }
        graph = build_from_json(extraction, directed=True, root=root)
        payloads = {
            "graph.json": _graph_bytes(graph), "manifest.json": _json_bytes(manifest),
            ".graphify_root": (request["root"] + "\n").encode("utf-8"),
            ".graphify_build.json": _json_bytes({
                "excludes": request["exclude"], "include": request["include"],
                "extraction_profile": request["extraction_profile"],
                "artifact_profile": request["artifact_profile"],
            }),
        }
        if sum(map(len, payloads.values())) > BOUNDS["max_payload_bytes"]:
            raise SourceUnsupported("structural payload byte limit exceeded")
        uncovered = {
            "selected_noncode": [
                {"path": relative(path), "type": kind, "reason": "structural-v1 excludes noncode"}
                for kind, paths in sorted(detection["files"].items()) if kind != "code"
                for path in paths if selected(path)
            ],
            "not_selected": sorted(
                relative(path) for paths in detection["files"].values() for path in paths
                if not selected(path)
            ),
            "unclassified_not_selected": sorted(
                relative(path) for path in detection["unclassified"] if not selected(path)
            ),
            "ignored": sorted(detection["ignored"]),
            "skipped_sensitive": sorted(detection["skipped_sensitive"]),
        }
    return SourceBuild(payloads, evidence, consumed.sha256, tuple(relative(path) for path in code),
                       evidence["outcomes"], uncovered)


def reobserve(request, evidence, *,
              operational_dirs: Mapping[Path, tuple[int, int]] | None = None) -> None:
    """Fail if any exact consumed read/list/probe, root, or binding changed.

    Directory records are reproduced by the rooted operations. This bounded
    repeated observation cannot prevent a later write by a source producer.
    The optional operational exclusion has the same internal ownership contract
    as ``build`` and grants no general source-exclusion authority.
    """
    request = validate_request(request)
    manifest = InputManifest.from_mapping(evidence)
    if not manifest.complete:
        raise ValueError("complete consumed source evidence required")
    value = manifest.to_dict()
    with _inputs(request, _OperationalInputs, operational_dirs=operational_dirs) as inputs:
        if value["roots"] != sorted(inputs.roots):
            raise SourceUnsupported("consumed source roots differ from request")
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
            elif operation != "directory":
                raise SourceUnsupported("unsupported source evidence operation")
        observed = InputManifest.from_engine(inputs, phase="consumed",
            code_inputs=value["code_inputs"], outcomes=value["outcomes"])
        if observed != manifest:
            raise SourceChanged("consumed source evidence changed")
