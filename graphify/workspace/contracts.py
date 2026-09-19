"""Bounded new-format contracts, independent of engine and platform imports.

Schemas describe wire shapes; the models also enforce cross-field invariants.
Canonical encoding follows the donor's NFC, sorted-key, newline-terminated JSON.
Input labels deliberately require NFC already: normalizing a filesystem name
could otherwise identify a different input. No document grants filesystem access.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import unicodedata

STATE_SCHEMA_VERSION = 2
ADAPTER_CONTRACT_VERSION = 2
INPUT_MANIFEST_VERSION = 1
GRAPH_PAYLOAD_VERSION = 1
DISTRIBUTION_VERSION = "0.10.0"
ENGINE_BASELINE = "git:7a6f667acf805b34e28f1658d9c69de16a282892"
EXTRACTOR_CACHE_ABI = "graphify-v8-structural-1"
DETECTOR_ID = "graphify-v8/workspace-observer-v2"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
ROOT_LABELS = frozenset({"source", "git", "policy"})
OUTCOME_STATUSES = frozenset({
    "success", "empty", "not_processed", "unsupported_extractor", "missing_parser",
    "failed_extraction", "source_error", "inconsistent_input", "unsupported_input",
    "failed_read", "partial_enumeration",
})
SCHEMA_FILES = ("compatibility.schema.json", "input-manifest.schema.json",
                "completion-binding.schema.json", "runtime-authority.schema.json",
                "state-root.schema.json")
INSTALLATION_METADATA = ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "licenses/LICENSE")


class ContractError(ValueError):
    """Malformed, unsupported, inconsistent or unbounded contract."""


def _normalise(value, depth=0):
    if depth > 32:
        raise ContractError("JSON nesting limit exceeded")
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ContractError("Unicode surrogates are forbidden")
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON keys must be strings")
            key = _normalise(key)
            if key in result:
                raise ContractError("duplicate normalized key")
            result[key] = _normalise(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalise(item, depth + 1) for item in value]
    raise ContractError("unsupported canonical JSON value")


def canonical_json_bytes(value):
    try:
        result = (json.dumps(_normalise(value), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise ContractError("invalid canonical JSON") from exc
    if len(result) > MAX_DOCUMENT_BYTES:
        raise ContractError("document byte limit exceeded")
    return result


def canonical_sha256(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def decode_canonical(payload, *, max_bytes=MAX_DOCUMENT_BYTES):
    if type(payload) is not bytes or len(payload) > max_bytes:
        raise ContractError("invalid or oversized JSON bytes")
    try:
        value = json.loads(payload, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid JSON") from exc
    if canonical_json_bytes(value) != payload:
        raise ContractError("noncanonical JSON")
    return value


def exact(value, keys):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ContractError("unexpected or missing fields")


def integer(value, maximum=2**63 - 1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError("integer outside contract bounds")
    return value


def digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError("expected lowercase SHA-256")
    return value


def input_label(value, roots, *, source_file=False):
    if (not isinstance(value, str) or not value
            or unicodedata.normalize("NFC", value) != value
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or "\\" in value):
        raise ContractError("invalid input label")
    if len(value.encode("utf-8")) > 4096:
        raise ContractError("input label byte limit exceeded")
    root, path = value.split(":", 1) if ":" in value else ("source", value)
    if value.startswith("source:"):
        raise ContractError("source labels must not have an alias prefix")
    if root not in roots or (source_file and root != "source"):
        raise ContractError("input label outside explicit root allowlist")
    if path == "." and not source_file:
        return value
    if any(part in {"", ".", ".."} or ":" in part for part in path.split("/")):
        raise ContractError("input label must be a canonical relative path")
    return value


def _identity(value, *, directory=False):
    if not isinstance(value, (list, tuple)) or len(value) != (3 if directory else 6):
        raise ContractError("invalid S1 filesystem identity")
    for item in value:
        integer(item)
    if directory and not stat.S_ISDIR(value[2]):
        raise ContractError("expected directory binding")
    return tuple(value)


def _evidence(records, roots):
    if not isinstance(records, (list, tuple)) or len(records) > MAX_ENTRIES:
        raise ContractError("evidence count limit exceeded")
    indexed, bindings, units, total = {}, {}, len(records), 0

    def bind(path, identity):
        if path in bindings and bindings[path] != identity:
            raise ContractError("inconsistent evidence binding")
        bindings[path] = identity

    for record in records:
        exact(record, {"operation", "path", "value"})
        op, path, value = record["operation"], record["path"], record["value"]
        input_label(path, roots)
        if not isinstance(op, str) or (op, path) in indexed:
            raise ContractError("invalid or duplicate evidence operation")
        if op == "directory":
            bind(path, _identity(value, directory=True))
        elif op == "probe":
            if value is None:
                bind(path, None)
            else:
                if not isinstance(value, (list, tuple)):
                    raise ContractError("invalid probe identity")
                identity = _identity(value, directory=len(value) == 3)
                if len(identity) == 6 and stat.S_ISDIR(identity[2]):
                    raise ContractError("directory probes use binding-only evidence")
                if stat.S_ISLNK(identity[2]):
                    raise ContractError("symlink probe is unsupported")
                bind(path, identity[:3])
        elif op in {"read", "list"}:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ContractError("invalid read/list evidence")
            identity = _identity(value[0], directory=op == "list")
            bind(path, identity[:3])
            if op == "read":
                if not stat.S_ISREG(identity[2]):
                    raise ContractError("read must identify a regular file")
                integer(identity[3], MAX_FILE_BYTES)
                total += identity[3]
                digest(value[1])
            else:
                members = value[1]
                if not isinstance(members, (list, tuple)):
                    raise ContractError("invalid directory members")
                units += len(members)
                if units > MAX_ENTRIES:
                    raise ContractError("aggregate evidence bound exceeded")
                names = []
                for member in members:
                    if not isinstance(member, (list, tuple)) or len(member) != 2:
                        raise ContractError("invalid directory member")
                    name, binding = member
                    input_label(name, {"source"}, source_file=True)
                    if "/" in name or ":" in name:
                        raise ContractError("directory member must be one component")
                    # Membership may include an excluded symlink; a probe/read may not.
                    if not isinstance(binding, (list, tuple)) or len(binding) != 3:
                        raise ContractError("invalid member binding")
                    for item in binding:
                        integer(item)
                    child = (path[:-1] + name if path.endswith(":.") else
                             name if path == "." else path + "/" + name)
                    bind(child, tuple(binding))
                    names.append(name)
                if names != sorted(set(names)):
                    raise ContractError("directory members must be sorted and unique")
        else:
            raise ContractError("unknown evidence operation")
        indexed[op, path] = record
    if units > MAX_ENTRIES or total > MAX_TOTAL_BYTES:
        raise ContractError("aggregate evidence bound exceeded")
    if list(indexed) != sorted(indexed):
        raise ContractError("evidence must be sorted by operation and path")
    for root in roots:
        if ("directory", "." if root == "source" else root + ":.") not in indexed:
            raise ContractError("missing pinned root binding")
    for (op, path), record in indexed.items():
        root, rel = path.split(":", 1) if ":" in path else ("source", path)
        parts = rel.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            parent = parent if root == "source" else root + ":" + parent
            if ("directory", parent) not in indexed:
                raise ContractError("missing traversed directory binding")
        if op in {"read", "list"}:
            probe = indexed.get(("probe", path))
            if probe is None or probe["value"] != record["value"][0]:
                raise ContractError("consumption requires matching S1 probe evidence")
    return indexed, bindings


@dataclass(frozen=True)
class Document:
    """Canonical immutable snapshot; returned dictionaries never alias the model."""
    canonical: bytes

    @staticmethod
    def validate(value):
        raise ContractError("use a concrete contract document")

    def __post_init__(self):
        value = decode_canonical(self.canonical)
        self.validate(value)

    @classmethod
    def from_mapping(cls, value):
        # Validate before normalization can alter filesystem labels.
        cls.validate(value)
        return cls(canonical_json_bytes(value))

    @classmethod
    def from_json(cls, payload):
        return cls(payload)

    def to_dict(self):
        return json.loads(self.canonical)

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical).hexdigest()


class CompatibilityManifest(Document):
    """S2 local fixture identity, never a committed or certified candidate."""
    @staticmethod
    def validate(value):
        constants = {
            "contract": "graphify.workspace.compatibility", "schema_version": 2,
            "distribution": "graphifyy", "distribution_version": DISTRIBUTION_VERSION,
            "engine_baseline": ENGINE_BASELINE, "extractor_cache_abi": EXTRACTOR_CACHE_ABI,
            "adapter_contract_version": 2, "state_schema_version": 2,
            "detector_id": DETECTOR_ID, "graph_payload_version": 1,
            "input_manifest_version": 1, "candidate_kind": "local-fixture",
            "certified": False,
        }
        exact(value, set(constants) | {"distribution_build", "source_manifest_sha256",
                                      "wheel_sha256", "package_members", "installation_metadata"})
        for key, expected in constants.items():
            if type(value[key]) is not type(expected) or value[key] != expected:
                raise ContractError(f"unsupported compatibility: {key}")
        source_digest = digest(value["source_manifest_sha256"])
        if value["distribution_build"] != "fixture:sha256:" + source_digest:
            raise ContractError("fixture build must bind its actual source inventory")
        digest(value["wheel_sha256"])
        exact(value["installation_metadata"], INSTALLATION_METADATA)
        for sha in value["installation_metadata"].values():
            digest(sha)
        members = value["package_members"]
        if not isinstance(members, Mapping) or not 1 <= len(members) <= 10_000:
            raise ContractError("invalid package member inventory")
        for path, sha in members.items():
            input_label(path, {"source"}, source_file=True)
            if not path.startswith("graphify/") or ":" in path:
                raise ContractError("package member outside graphify")
            digest(sha)
        required = {"graphify/__init__.py", "graphify/__main__.py", "graphify/source_io.py",
                    "graphify/workspace/contracts.py", "graphify/workspace/composition.py",
                    "graphify/workspace/__init__.py", "graphify/workspace/adapters/base.py",
                    "graphify/workspace/adapters/__init__.py"}
        required.update("graphify/workspace/schemas/" + name for name in SCHEMA_FILES)
        if not required <= set(members):
            raise ContractError("missing structural package members")


class InputManifest(Document):
    @staticmethod
    def validate(value):
        exact(value, {"contract", "format_version", "phase", "roots", "evidence",
                      "code_inputs", "outcomes", "failure"})
        if value["contract"] != "graphify.workspace.source-inputs" or type(value["format_version"]) is not int or value["format_version"] != 1:
            raise ContractError("unsupported input manifest format")
        roots = value["roots"]
        if (not isinstance(roots, (list, tuple)) or not all(isinstance(r, str) for r in roots)
                or list(roots) != sorted(set(roots)) or "source" not in roots
                or not set(roots) <= ROOT_LABELS):
            raise ContractError("invalid input root allowlist")
        indexed, bindings = _evidence(value["evidence"], roots)
        code = value["code_inputs"]
        if not isinstance(code, (list, tuple)) or len(code) > MAX_ENTRIES:
            raise ContractError("invalid code inventory")
        for path in code:
            input_label(path, roots, source_file=True)
        if list(code) != sorted(set(code)):
            raise ContractError("code inventory must be sorted and unique")
        outcomes = value["outcomes"]
        if not isinstance(outcomes, (list, tuple)):
            raise ContractError("invalid outcomes")
        if value["phase"] == "detection":
            if any(bindings.get(path) is None or not stat.S_ISREG(bindings[path][2])
                   for path in code):
                raise ContractError("code input requires regular-file identity evidence")
            if outcomes or value["failure"] is not None:
                raise ContractError("detection does not assert extraction outcomes")
        elif value["phase"] == "consumed":
            if len(outcomes) != len(code):
                raise ContractError("every admitted code input needs one disposition")
            for path, outcome in zip(code, outcomes):
                exact(outcome, {"path", "status"})
                if (outcome["path"] != path or not isinstance(outcome["status"], str)
                        or outcome["status"] not in OUTCOME_STATUSES):
                    raise ContractError("invalid per-input disposition")
                if outcome["status"] in {"success", "empty"} and ("read", path) not in indexed:
                    raise ContractError("successful extraction must have consumed the input")
            failure = value["failure"]
            if failure is not None and (not isinstance(failure, str) or failure not in OUTCOME_STATUSES - {"success", "empty", "not_processed"}):
                raise ContractError("invalid overall extraction failure")
        else:
            raise ContractError("unknown observation phase")

    @property
    def complete(self):
        value = self.to_dict()
        return (value["phase"] == "consumed" and value["failure"] is None
                and all(o["status"] in {"success", "empty"} for o in value["outcomes"]))

    @classmethod
    def from_engine(cls, source_io, *, phase, code_inputs, outcomes=(), failure=None):
        """Snapshot actual S1 evidence. Call inside its open scoped-I/O context.

        Failure details may contain absolute paths; retain bounded status codes only.
        A latched I/O failure or resolver-wide failure cannot become empty success.
        """
        if source_io.failure is None:
            source_io.check()
        else:
            failure = source_io.failure.code
        def label(path):
            path = Path(path)
            if path.is_absolute():
                try:
                    path = path.relative_to(source_io.root)
                except ValueError as exc:
                    raise ContractError("outcome outside source root") from exc
            return path.as_posix()
        dispositions = sorted(({"path": label(o["path"]), "status": o["status"]}
                               for o in outcomes), key=lambda o: o["path"])
        if isinstance(failure, Mapping):
            failure = failure["status"]
        return cls.from_mapping({
            "contract": "graphify.workspace.source-inputs", "format_version": 1,
            "phase": phase, "roots": sorted(source_io.roots),
            "evidence": source_io.evidence, "code_inputs": sorted(label(p) for p in code_inputs),
            "outcomes": dispositions, "failure": failure,
        })


class CompletionBinding(Document):
    @staticmethod
    def validate(value):
        exact(value, {"contract", "state_schema_version", "initial_detection_sha256",
                      "consumed_inputs_sha256", "compatibility_sha256", "graph_sha256"})
        if (value["contract"] != "graphify.workspace.structural-completion"
                or type(value["state_schema_version"]) is not int or value["state_schema_version"] != 2):
            raise ContractError("unsupported staged completion format")
        for key in set(value) - {"contract", "state_schema_version"}:
            digest(value[key])

    @classmethod
    def bind(cls, initial, consumed, *, compatibility, graph_sha256):
        if type(initial) is not InputManifest or type(consumed) is not InputManifest:
            raise ContractError("completion requires validated input manifests")
        if type(compatibility) is not CompatibilityManifest:
            raise ContractError("completion requires a compatibility manifest")
        a, b = initial.to_dict(), consumed.to_dict()
        if a["phase"] != "detection" or not consumed.complete:
            raise ContractError("completion requires initial detection and complete extraction")
        if a["roots"] != b["roots"] or a["code_inputs"] != b["code_inputs"]:
            raise ContractError("detection authority changed")
        final = {(e["operation"], e["path"]): e for e in b["evidence"]}
        if any(final.get((e["operation"], e["path"])) != e for e in a["evidence"]):
            raise ContractError("initial evidence missing or changed in consumed binding")
        return cls.from_mapping({
            "contract": "graphify.workspace.structural-completion", "state_schema_version": 2,
            "initial_detection_sha256": initial.sha256, "consumed_inputs_sha256": consumed.sha256,
            "compatibility_sha256": compatibility.sha256, "graph_sha256": graph_sha256,
        })


class StateRootMarker(Document):
    """Denial-only ownership sentinel format; S3 installs it under its fence."""
    @staticmethod
    def validate(value):
        exact(value, {"contract", "state_schema_version", "owner"})
        if (value["contract"] != "graphify.workspace.state-root"
                or type(value["state_schema_version"]) is not int or value["state_schema_version"] != 2
                or value["owner"] != "graphify.workspace"):
            raise ContractError("unknown managed state root")
