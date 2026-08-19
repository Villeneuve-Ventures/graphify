"""Private P5B2 semantic-release decision binding persistence.

This module owns only the request-addressed immutable binding namespace.  It
does not classify content, select policy, produce public receipts, or perform
GC mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast
from uuid import UUID

from graphify.workspace.contracts import (
    SEMANTIC_RELEASE_DECISION_BINDING_MAX_BYTES,
    SEMANTIC_RELEASE_DECISION_BINDINGS_PER_GENERATION,
    SEMANTIC_RELEASE_DECISION_BINDINGS_PER_WORKSPACE,
    SEMANTIC_RELEASE_DECISION_STAGING_MANIFEST_MAX_BYTES,
    CapacityPolicy,
    Registry,
    canonical_json_bytes,
)
from graphify.workspace.gc import GcError, GcStore
from graphify.workspace.generations import (
    CapacityExceeded,
    DecisionCapacityUsage,
    GenerationStore,
)
from graphify.workspace.leases import LeaseStore
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    FaultHook,
    RuntimeCapabilities,
    StateCorrupt,
    StatePathError,
    Syscalls,
    WorkspaceRuntimeError,
)
from graphify.workspace.registry import RegistryStore

SEMANTIC_RELEASE_DECISION_CONTRACT = (
    "graphify.workspace.semantic_release_decision.internal"
)
SEMANTIC_RELEASE_DECISION_FORMAT_VERSION = 1
DECISION_BINDING_MAX_BYTES = SEMANTIC_RELEASE_DECISION_BINDING_MAX_BYTES
DECISION_BINDINGS_PER_GENERATION = SEMANTIC_RELEASE_DECISION_BINDINGS_PER_GENERATION
DECISION_BINDINGS_PER_WORKSPACE = SEMANTIC_RELEASE_DECISION_BINDINGS_PER_WORKSPACE
DECISION_FIELD_RESULTS_MAX = 30_000
_STAGING_MANIFEST_MAX_BYTES = SEMANTIC_RELEASE_DECISION_STAGING_MANIFEST_MAX_BYTES
_STAGING_DIRECTORY_MODES = frozenset({0o700})
_STAGING_FILE_MODES = frozenset({0o600})
_STAGING_MANIFEST_MEMBERS = {
    "binding_bytes",
    "binding_sha256",
    "contract",
    "destination",
    "format_version",
    "generation_id",
    "publication_kind",
    "repo_uuid",
    "request_sha256",
}
_STAGING_CONTRACT = "graphify.workspace.semantic_release_decision.publication.internal"
_JSON_MAX_DEPTH = 64
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_GENERATION_RE = re.compile(r"gen-[a-z0-9][a-z0-9._-]{0,62}", re.ASCII)
_SEMANTIC_ENTITY_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,256}", re.ASCII)
_ENTITY_ORDER = {"node": 0, "hyperedge": 1}
_FIELD_ORDER = {"label": 0, "rationale": 1}
_CLASSIFIER_OUTCOMES = frozenset({"NO_MATCH", "MATCH", "INDETERMINATE"})
_DISPOSITIONS = frozenset({"ALLOW_FIELD", "OMIT_RATIONALE", "REJECT_RELEASE"})
_TERMINAL_OUTCOMES = frozenset(
    {"ALLOW_UNCHANGED", "ALLOW_WITH_OMISSIONS", "REJECTED"}
)
_TOP_LEVEL_MEMBERS = {
    "contract",
    "format_version",
    "repo_uuid",
    "target_generation_id",
    "decision_request_sha256",
    "promoted_entry_sha256",
    "bundle_manifest_sha256",
    "policy_authority_revision",
    "policy_authority_sha256",
    "semantic_input_byte_count",
    "semantic_input_sha256",
    "eligible_field_inventory_sha256",
    "taxonomy_sha256",
    "normalization_sha256",
    "classifier_implementation_sha256",
    "classifier_abi_sha256",
    "ruleset_sha256",
    "selected_profile_sha256s",
    "coverage_sufficiency_sha256",
    "policy_sha256",
    "counts",
    "field_results",
    "terminal_outcome",
    "full_result_sha256",
}
_COUNT_MEMBERS = {
    "node_label_count",
    "node_rationale_count",
    "hyperedge_label_count",
    "field_result_count",
    "matched_field_count",
}
_FIELD_RESULT_MEMBERS = {
    "entity_kind",
    "entity_id",
    "field_name",
    "field_value_sha256",
    "classifier_outcome",
    "category_ids",
    "rule_ids",
    "disposition",
}


class SemanticReleaseDecisionError(WorkspaceRuntimeError):
    """Base failure for private semantic-release decision persistence."""

    code = "semantic_release_decision_error"


class SemanticReleaseDecisionInvalid(SemanticReleaseDecisionError):
    """A binding, identity, or namespace snapshot is invalid."""

    code = "semantic_release_decision_invalid"


class SemanticReleaseDecisionConflict(SemanticReleaseDecisionError):
    """The request-derived path or captured capacity state changed."""

    code = "semantic_release_decision_conflict"


def _exact_members(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SemanticReleaseDecisionInvalid(
            f"{label} members must be exactly {', '.join(sorted(expected))}"
        )


def _mapping(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict or any(
        type(key) is not str for key in cast(dict[Any, Any], value)
    ):
        raise SemanticReleaseDecisionInvalid(f"{label} must be a plain object")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise SemanticReleaseDecisionInvalid(f"{label} must be an array")
    return cast(list[object], value)


def _plain_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise SemanticReleaseDecisionInvalid(f"{label} must be a non-empty string")
    text = cast(str, value)
    if text.strip() != text or unicodedata.normalize("NFC", text) != text:
        raise SemanticReleaseDecisionInvalid(f"{label} must be trimmed canonical NFC")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise SemanticReleaseDecisionInvalid(f"{label} contains a Unicode surrogate")
    return text


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(cast(str, value)) is None:
        raise SemanticReleaseDecisionInvalid(
            f"{label} must be 64 lowercase hexadecimal digits"
        )
    return cast(str, value)


def _repo_uuid(value: object) -> str:
    text = _plain_string(value, "repo_uuid")
    try:
        canonical = str(UUID(text))
    except ValueError as exc:
        raise SemanticReleaseDecisionInvalid("repo_uuid is not a UUID") from exc
    if text != canonical:
        raise SemanticReleaseDecisionInvalid("repo_uuid is not canonical")
    return text


def _generation_id(value: object) -> str:
    text = _plain_string(value, "target_generation_id")
    if _GENERATION_RE.fullmatch(text) is None:
        raise SemanticReleaseDecisionInvalid("target_generation_id is invalid")
    return text


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or cast(int, value) < minimum:
        raise SemanticReleaseDecisionInvalid(
            f"{label} must be an integer greater than or equal to {minimum}"
        )
    return cast(int, value)


def _ordered_unique_strings(
    value: object,
    label: str,
    *,
    maximum_utf8_bytes: int | None = None,
) -> list[str]:
    result = [_plain_string(item, label) for item in _array(value, label)]
    if maximum_utf8_bytes is not None and any(
        len(item.encode("utf-8")) > maximum_utf8_bytes for item in result
    ):
        raise SemanticReleaseDecisionInvalid(
            f"{label} exceeds {maximum_utf8_bytes} UTF-8 bytes"
        )
    if result != sorted(result, key=lambda item: item.encode("utf-8")):
        raise SemanticReleaseDecisionInvalid(f"{label} is not utf8_lex_v1 ordered")
    if len(result) != len(set(result)):
        raise SemanticReleaseDecisionInvalid(f"{label} contains duplicates")
    return result


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticReleaseDecisionInvalid(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SemanticReleaseDecisionInvalid(f"non-finite JSON number is forbidden: {value}")


def _preflight_json_depth(payload: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            if depth >= _JSON_MAX_DEPTH:
                raise SemanticReleaseDecisionInvalid(
                    "decision binding JSON nesting exceeds supported depth"
                )
            depth += 1
        elif byte in (0x5D, 0x7D) and depth:
            depth -= 1


def _validated_counts(value: object) -> dict[str, int]:
    counts = _mapping(value, "counts")
    _exact_members(counts, _COUNT_MEMBERS, "counts")
    return {
        name: _integer(counts[name], f"counts.{name}")
        for name in (
            "node_label_count",
            "node_rationale_count",
            "hyperedge_label_count",
            "field_result_count",
            "matched_field_count",
        )
    }


def _validated_field_results(value: object) -> list[dict[str, object]]:
    raw = _array(value, "field_results")
    if len(raw) > DECISION_FIELD_RESULTS_MAX:
        raise SemanticReleaseDecisionInvalid("field_results exceeds 30000 entries")
    results: list[dict[str, object]] = []
    keys: list[tuple[int, bytes, int]] = []
    for index, item in enumerate(raw):
        result = _mapping(item, f"field_results[{index}]")
        _exact_members(result, _FIELD_RESULT_MEMBERS, f"field_results[{index}]")
        entity_kind = _plain_string(result["entity_kind"], "entity_kind")
        entity_id = _plain_string(result["entity_id"], "entity_id")
        field_name = _plain_string(result["field_name"], "field_name")
        if entity_kind not in _ENTITY_ORDER:
            raise SemanticReleaseDecisionInvalid("entity_kind is unsupported")
        if (
            _SEMANTIC_ENTITY_ID_RE.fullmatch(entity_id) is None
            or ".." in entity_id
        ):
            raise SemanticReleaseDecisionInvalid("entity_id violates semantic ID grammar")
        if field_name not in _FIELD_ORDER or (
            entity_kind == "hyperedge" and field_name != "label"
        ):
            raise SemanticReleaseDecisionInvalid("entity_kind/field_name is unsupported")
        classifier_outcome = _plain_string(
            result["classifier_outcome"], "classifier_outcome"
        )
        disposition = _plain_string(result["disposition"], "disposition")
        if classifier_outcome not in _CLASSIFIER_OUTCOMES:
            raise SemanticReleaseDecisionInvalid("classifier_outcome is unsupported")
        if disposition not in _DISPOSITIONS:
            raise SemanticReleaseDecisionInvalid("disposition is unsupported")
        if disposition == "OMIT_RATIONALE" and not (
            entity_kind == "node" and field_name == "rationale"
        ):
            raise SemanticReleaseDecisionInvalid(
                "OMIT_RATIONALE is valid only for node rationale"
            )
        categories = _ordered_unique_strings(
            result["category_ids"],
            "category_ids",
            maximum_utf8_bytes=256,
        )
        rules = _ordered_unique_strings(
            result["rule_ids"],
            "rule_ids",
            maximum_utf8_bytes=256,
        )
        if len(categories) > 256 or len(rules) > 256:
            raise SemanticReleaseDecisionInvalid(
                "field result exceeds the category or rule bound"
            )
        if classifier_outcome == "NO_MATCH" and (categories or rules):
            raise SemanticReleaseDecisionInvalid(
                "NO_MATCH cannot carry category or rule matches"
            )
        if classifier_outcome == "NO_MATCH" and disposition not in {
            "ALLOW_FIELD",
            "REJECT_RELEASE",
        }:
            raise SemanticReleaseDecisionInvalid(
                "NO_MATCH must produce ALLOW_FIELD or REJECT_RELEASE"
            )
        if classifier_outcome == "MATCH" and not categories:
            raise SemanticReleaseDecisionInvalid(
                "MATCH must carry at least one category"
            )
        if classifier_outcome == "MATCH" and not rules:
            raise SemanticReleaseDecisionInvalid(
                "MATCH must carry at least one rule"
            )
        if classifier_outcome == "INDETERMINATE" and (categories or rules):
            raise SemanticReleaseDecisionInvalid(
                "INDETERMINATE cannot carry category or rule matches"
            )
        if classifier_outcome == "INDETERMINATE" and disposition != "REJECT_RELEASE":
            raise SemanticReleaseDecisionInvalid(
                "INDETERMINATE must produce REJECT_RELEASE"
            )
        validated = {
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "field_name": field_name,
            "field_value_sha256": _sha256(
                result["field_value_sha256"], "field_value_sha256"
            ),
            "classifier_outcome": classifier_outcome,
            "category_ids": categories,
            "rule_ids": rules,
            "disposition": disposition,
        }
        results.append(validated)
        keys.append(
            (
                _ENTITY_ORDER[entity_kind],
                entity_id.encode("utf-8"),
                _FIELD_ORDER[field_name],
            )
        )
    if keys != sorted(keys):
        raise SemanticReleaseDecisionInvalid("field_results are not in canonical order")
    if len(keys) != len(set(keys)):
        raise SemanticReleaseDecisionInvalid("field_results contain duplicate field locators")
    return results


@dataclass(frozen=True)
class SemanticReleaseDecisionBinding:
    """Validated immutable format-version-1 private decision binding."""

    _canonical: bytes
    repo_uuid: str
    target_generation_id: str
    decision_request_sha256: str

    @property
    def canonical(self) -> bytes:
        return self._canonical

    @property
    def binding_sha256(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self._canonical))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SemanticReleaseDecisionBinding:
        data = _mapping(value, "decision binding")
        _exact_members(data, _TOP_LEVEL_MEMBERS, "decision binding")
        if data["contract"] != SEMANTIC_RELEASE_DECISION_CONTRACT:
            raise SemanticReleaseDecisionInvalid("decision binding contract is unsupported")
        if (
            _integer(data["format_version"], "format_version", minimum=1)
            != SEMANTIC_RELEASE_DECISION_FORMAT_VERSION
        ):
            raise SemanticReleaseDecisionInvalid(
                "decision binding format version is unsupported"
            )
        repo_uuid = _repo_uuid(data["repo_uuid"])
        generation_id = _generation_id(data["target_generation_id"])
        decision_request_sha256 = _sha256(
            data["decision_request_sha256"], "decision_request_sha256"
        )
        for name in (
            "promoted_entry_sha256",
            "bundle_manifest_sha256",
            "policy_authority_sha256",
            "semantic_input_sha256",
            "eligible_field_inventory_sha256",
            "taxonomy_sha256",
            "normalization_sha256",
            "classifier_implementation_sha256",
            "classifier_abi_sha256",
            "ruleset_sha256",
            "coverage_sufficiency_sha256",
            "policy_sha256",
            "full_result_sha256",
        ):
            _sha256(data[name], name)
        _integer(data["policy_authority_revision"], "policy_authority_revision", minimum=1)
        _integer(data["semantic_input_byte_count"], "semantic_input_byte_count")
        profiles = _array(data["selected_profile_sha256s"], "selected_profile_sha256s")
        if len(profiles) > 64:
            raise SemanticReleaseDecisionInvalid(
                "selected_profile_sha256s exceeds 64 entries"
            )
        for index, digest in enumerate(profiles):
            _sha256(digest, f"selected_profile_sha256s[{index}]")
        counts = _validated_counts(data["counts"])
        field_results = _validated_field_results(data["field_results"])
        terminal_outcome = _plain_string(data["terminal_outcome"], "terminal_outcome")
        if terminal_outcome not in _TERMINAL_OUTCOMES:
            raise SemanticReleaseDecisionInvalid("terminal_outcome is unsupported")
        observed_counts = {
            "node_label_count": sum(
                1
                for item in field_results
                if item["entity_kind"] == "node" and item["field_name"] == "label"
            ),
            "node_rationale_count": sum(
                1
                for item in field_results
                if item["entity_kind"] == "node" and item["field_name"] == "rationale"
            ),
            "hyperedge_label_count": sum(
                1 for item in field_results if item["entity_kind"] == "hyperedge"
            ),
            "field_result_count": len(field_results),
            "matched_field_count": sum(
                1 for item in field_results if item["classifier_outcome"] == "MATCH"
            ),
        }
        if counts != observed_counts:
            raise SemanticReleaseDecisionInvalid(
                "counts do not agree with the canonical field results"
            )
        if any(counts[name] > 10_000 for name in _COUNT_MEMBERS - {"field_result_count", "matched_field_count"}):
            raise SemanticReleaseDecisionInvalid(
                "one semantic field-kind count exceeds 10000"
            )
        dispositions = {str(item["disposition"]) for item in field_results}
        if "REJECT_RELEASE" in dispositions and terminal_outcome != "REJECTED":
            raise SemanticReleaseDecisionInvalid(
                "terminal_outcome does not preserve REJECT_RELEASE"
            )
        if terminal_outcome == "REJECTED" and "REJECT_RELEASE" not in dispositions:
            raise SemanticReleaseDecisionInvalid(
                "terminal_outcome does not preserve the exact rejection reduction"
            )
        if terminal_outcome == "ALLOW_WITH_OMISSIONS" and (
            "REJECT_RELEASE" in dispositions or "OMIT_RATIONALE" not in dispositions
        ):
            raise SemanticReleaseDecisionInvalid(
                "ALLOW_WITH_OMISSIONS does not agree with field dispositions"
            )
        if terminal_outcome == "ALLOW_UNCHANGED" and dispositions - {"ALLOW_FIELD"}:
            raise SemanticReleaseDecisionInvalid(
                "ALLOW_UNCHANGED does not agree with field dispositions"
            )
        full_result = {
            "eligible_field_inventory_sha256": data[
                "eligible_field_inventory_sha256"
            ],
            "counts": counts,
            "field_results": field_results,
            "terminal_outcome": terminal_outcome,
        }
        expected_full_result = hashlib.sha256(canonical_json_bytes(full_result)).hexdigest()
        if data["full_result_sha256"] != expected_full_result:
            raise SemanticReleaseDecisionInvalid(
                "full_result_sha256 does not match its exact canonical preimage"
            )
        canonical = canonical_json_bytes(data)
        if not canonical or len(canonical) > DECISION_BINDING_MAX_BYTES:
            raise SemanticReleaseDecisionInvalid("decision binding exceeds 25 MiB")
        return cls(
            _canonical=canonical,
            repo_uuid=repo_uuid,
            target_generation_id=generation_id,
            decision_request_sha256=decision_request_sha256,
        )

    @classmethod
    def from_json(cls, payload: bytes) -> SemanticReleaseDecisionBinding:
        if type(payload) is not bytes or not payload or len(payload) > DECISION_BINDING_MAX_BYTES:
            raise SemanticReleaseDecisionInvalid(
                "decision binding must be between 1 byte and 25 MiB"
            )
        _preflight_json_depth(payload)
        try:
            value = json.loads(
                payload,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        except SemanticReleaseDecisionInvalid:
            raise
        except (json.JSONDecodeError, RecursionError, UnicodeDecodeError, ValueError) as exc:
            raise SemanticReleaseDecisionInvalid(
                f"decision binding is invalid JSON: {exc}"
            ) from exc
        binding = cls.from_mapping(_mapping(value, "decision binding"))
        if binding.canonical != payload:
            raise SemanticReleaseDecisionInvalid("decision binding bytes are not canonical")
        return binding


@dataclass(frozen=True)
class SemanticReleaseDecisionCapture:
    """Read-only locked namespace and capacity proof before classification."""

    repo_uuid: str
    generation_id: str
    decision_request_sha256: str
    capacity_policy_sha256: str
    registry_sha256: str
    usage_sha256: str
    global_bytes: int
    workspace_bytes: int
    unconsumed_reserved_bytes: int
    generation_binding_count: int
    workspace_binding_count: int
    existing_binding_sha256: str | None
    existing_binding_bytes: int


class SemanticReleaseDecisionStore:
    """Install one exact private binding under the frozen lock order."""

    def __init__(
        self,
        state_root: Path,
        registry: RegistryStore,
        leases: LeaseStore,
        generations: GenerationStore,
        gc: GcStore,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.registry = registry
        self.leases = leases
        self.generations = generations
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )
        if {
            self.state.root,
            registry.state.root,
            leases.state.root,
            generations.state.root,
            gc.state.root,
        } != {self.state.root}:
            raise SemanticReleaseDecisionInvalid(
                "decision store dependencies must share one external state root"
            )
        if gc.generations is not generations or gc.leases is not leases:
            raise SemanticReleaseDecisionInvalid(
                "decision store GC dependencies must use the same stores"
            )
        self._gc = gc

    @staticmethod
    def _workspace(repo_uuid: str) -> Path:
        return GenerationStore._workspace(_repo_uuid(repo_uuid))

    @classmethod
    def _generation_directory(cls, repo_uuid: str, generation_id: str) -> Path:
        return cls._workspace(repo_uuid) / "semantic-release-decisions" / generation_id

    @classmethod
    def _binding_path(
        cls,
        repo_uuid: str,
        generation_id: str,
        decision_request_sha256: str,
    ) -> Path:
        return cls._generation_directory(repo_uuid, generation_id) / (
            _sha256(decision_request_sha256, "decision_request_sha256") + ".json"
        )

    @classmethod
    def _staging_slot(cls, repo_uuid: str) -> Path:
        return cls._workspace(repo_uuid) / "semantic-release-decision-publication"

    def _directory_names(
        self,
        relative: Path,
        *,
        allow_missing: bool = False,
        maximum_entries: int = 3,
        deadline_ns: int | None = None,
    ) -> tuple[str, ...] | None:
        path = self.state.path(relative)
        with self.state._existing_private_directory(
            relative,
            allow_missing=allow_missing,
        ) as descriptor:
            if descriptor is None:
                return None
            return tuple(
                self.state._tree_entry_names_descriptor(
                    descriptor,
                    path,
                    deadline_ns=deadline_ns,
                    maximum_entries=maximum_entries,
                )
            )

    def _staging_manifest(
        self,
        relative: Path,
        *,
        allow_partial: bool,
        deadline_ns: int | None,
    ) -> dict[str, object] | None:
        payload = self.state._read_optional_existing_stable_bytes(
            relative,
            max_bytes=_STAGING_MANIFEST_MAX_BYTES,
            deadline_ns=deadline_ns,
        )
        if payload is None:
            return None
        try:
            value = _mapping(json.loads(payload), "decision publication manifest")
            _exact_members(
                value,
                _STAGING_MANIFEST_MEMBERS,
                "decision publication manifest",
            )
            if canonical_json_bytes(value) != payload:
                raise SemanticReleaseDecisionInvalid(
                    "decision publication manifest is not canonical"
                )
            if (
                value["contract"] != _STAGING_CONTRACT
                or type(value["format_version"]) is not int
                or value["format_version"] != 1
            ):
                raise SemanticReleaseDecisionInvalid(
                    "decision publication manifest contract is unsupported"
                )
            repo_uuid = _repo_uuid(value["repo_uuid"])
            generation_id = _generation_id(value["generation_id"])
            request_sha256 = _sha256(value["request_sha256"], "request_sha256")
            binding_sha256 = _sha256(value["binding_sha256"], "binding_sha256")
            binding_bytes = value["binding_bytes"]
            if (
                type(binding_bytes) is not int
                or binding_bytes < 1
                or binding_bytes > DECISION_BINDING_MAX_BYTES
            ):
                raise SemanticReleaseDecisionInvalid(
                    "decision publication binding byte count is invalid"
                )
            publication_kind = value["publication_kind"]
            if publication_kind not in {"root", "generation", "file"}:
                raise SemanticReleaseDecisionInvalid(
                    "decision publication kind is invalid"
                )
            destination = value["destination"]
            if type(destination) is not str:
                raise SemanticReleaseDecisionInvalid(
                    "decision publication destination is invalid"
                )
            expected_binding = self._binding_path(
                repo_uuid,
                generation_id,
                request_sha256,
            )
            expected_destination = {
                "root": self._workspace(repo_uuid) / "semantic-release-decisions",
                "generation": self._generation_directory(repo_uuid, generation_id),
                "file": expected_binding,
            }[cast(str, publication_kind)]
            if destination != expected_destination.as_posix():
                raise SemanticReleaseDecisionInvalid(
                    "decision publication destination differs from its identity"
                )
            value["repo_uuid"] = repo_uuid
            value["generation_id"] = generation_id
            value["request_sha256"] = request_sha256
            value["binding_sha256"] = binding_sha256
            return value
        except (
            SemanticReleaseDecisionInvalid,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            if allow_partial:
                return None
            raise SemanticReleaseDecisionInvalid(
                "decision publication manifest is invalid"
            ) from exc

    def _staged_binding_relative(
        self,
        state_relative: Path,
        manifest: Mapping[str, object],
    ) -> Path:
        payload = state_relative / "payload"
        generation_id = cast(str, manifest["generation_id"])
        request_name = f"{manifest['request_sha256']}.json"
        publication_kind = manifest["publication_kind"]
        if publication_kind == "root":
            return payload / generation_id / request_name
        if publication_kind == "generation":
            return payload / request_name
        return payload

    def _validate_staging_state(
        self,
        state_relative: Path,
        *,
        expected_repo_uuid: str,
        complete: bool,
        allow_partial_manifest: bool = True,
        deadline_ns: int | None,
    ) -> dict[str, object] | None:
        names = self._directory_names(
            state_relative,
            maximum_entries=2,
            deadline_ns=deadline_ns,
        )
        if names is None:  # pragma: no cover - tree_bytes proves presence
            raise SemanticReleaseDecisionInvalid("decision publication state is missing")
        if not set(names) <= {"manifest.json", "payload"}:
            raise SemanticReleaseDecisionInvalid(
                "decision publication state contains an unexpected entry"
            )
        manifest = self._staging_manifest(
            state_relative / "manifest.json",
            allow_partial=(
                allow_partial_manifest and not complete and "payload" not in names
            ),
            deadline_ns=deadline_ns,
        )
        if manifest is None:
            if complete or "payload" in names:
                raise SemanticReleaseDecisionInvalid(
                    "decision publication payload lacks a complete manifest"
                )
            return None
        if manifest["repo_uuid"] != expected_repo_uuid:
            raise SemanticReleaseDecisionInvalid(
                "decision publication manifest belongs to a different workspace"
            )
        staged_binding = self._staged_binding_relative(state_relative, manifest)
        publication_kind = manifest["publication_kind"]
        if publication_kind == "file":
            payload_names: tuple[str, ...] | None = None
        else:
            payload_names = self._directory_names(
                state_relative / "payload",
                allow_missing=True,
                maximum_entries=1,
                deadline_ns=deadline_ns,
            )
            if payload_names is not None:
                expected = (
                    cast(str, manifest["generation_id"])
                    if publication_kind == "root"
                    else f"{manifest['request_sha256']}.json"
                )
                if set(payload_names) not in (set(), {expected}):
                    raise SemanticReleaseDecisionInvalid(
                        "decision publication payload shape is invalid"
                    )
                if publication_kind == "root" and payload_names:
                    generation_names = self._directory_names(
                        state_relative / "payload" / cast(str, manifest["generation_id"]),
                        maximum_entries=1,
                        deadline_ns=deadline_ns,
                    )
                    if generation_names is None or set(generation_names) not in (
                        set(),
                        {f"{manifest['request_sha256']}.json"},
                    ):
                        raise SemanticReleaseDecisionInvalid(
                            "decision publication generation payload shape is invalid"
                        )
        staged = self.state._read_optional_existing_stable_bytes(
            staged_binding,
            max_bytes=cast(int, manifest["binding_bytes"]),
            deadline_ns=deadline_ns,
        )
        if staged is None:
            if complete:
                raise SemanticReleaseDecisionInvalid(
                    "decision publication binding is missing"
                )
            return manifest
        expected_bytes = cast(int, manifest["binding_bytes"])
        if len(staged) > expected_bytes or (complete and len(staged) != expected_bytes):
            raise SemanticReleaseDecisionInvalid(
                "decision publication binding length is invalid"
            )
        if len(staged) == expected_bytes:
            if hashlib.sha256(staged).hexdigest() != manifest["binding_sha256"]:
                raise SemanticReleaseDecisionInvalid(
                    "decision publication binding digest is invalid"
                )
            binding = SemanticReleaseDecisionBinding.from_json(staged)
            if (
                binding.repo_uuid != manifest["repo_uuid"]
                or binding.target_generation_id != manifest["generation_id"]
                or binding.decision_request_sha256 != manifest["request_sha256"]
            ):
                raise SemanticReleaseDecisionInvalid(
                    "decision publication binding identity is invalid"
                )
        elif complete:
            raise SemanticReleaseDecisionInvalid(
                "decision publication binding is incomplete"
            )
        return manifest

    def _prove_staging_destination(
        self,
        manifest: Mapping[str, object],
        *,
        deadline_ns: int | None,
    ) -> None:
        existing = self._read_binding(
            cast(str, manifest["repo_uuid"]),
            cast(str, manifest["generation_id"]),
            cast(str, manifest["request_sha256"]),
            deadline_ns=deadline_ns,
        )
        if existing is not None and (
            len(existing.canonical) != manifest["binding_bytes"]
            or existing.binding_sha256 != manifest["binding_sha256"]
        ):
            raise SemanticReleaseDecisionInvalid(
                "decision publication destination contains different bytes"
            )

    def _cleanup_staging(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None,
    ) -> None:
        slot = self._staging_slot(repo_uuid)
        names = self._directory_names(
            slot,
            allow_missing=True,
            maximum_entries=1,
            deadline_ns=deadline_ns,
        )
        if names is None or not names:
            return
        if len(names) != 1 or names[0] not in {"build", "ready", "cleanup"}:
            raise SemanticReleaseDecisionInvalid(
                "decision publication slot contains unexpected state"
            )
        state_name = names[0]
        state_relative = slot / state_name
        if state_name == "cleanup":
            self._validate_staging_state(
                state_relative,
                expected_repo_uuid=repo_uuid,
                complete=False,
                deadline_ns=deadline_ns,
            )
        else:
            state_entries = self._directory_names(
                state_relative,
                maximum_entries=2,
                deadline_ns=deadline_ns,
            )
            manifest = self._validate_staging_state(
                state_relative,
                expected_repo_uuid=repo_uuid,
                complete=state_name == "ready" and state_entries != ("manifest.json",),
                allow_partial_manifest=state_name != "ready",
                deadline_ns=deadline_ns,
            )
            if state_name == "ready" and manifest is not None:
                self._prove_staging_destination(
                    manifest,
                    deadline_ns=deadline_ns,
                )
            try:
                self.state.rename_exclusive_contained(
                    state_relative,
                    slot / "cleanup",
                    source_kind="directory",
                    label="semantic-release-decision:cleanup-transition",
                    deadline_ns=deadline_ns,
                )
            except CommitUnknown as exc:
                current = self._directory_names(
                    slot,
                    maximum_entries=1,
                    deadline_ns=deadline_ns,
                )
                if current != ("cleanup",):
                    raise
                raise SemanticReleaseDecisionConflict(
                    "decision publication cleanup transition is incomplete"
                ) from exc
            state_relative = slot / "cleanup"
            self._validate_staging_state(
                state_relative,
                expected_repo_uuid=repo_uuid,
                complete=False,
                deadline_ns=deadline_ns,
            )
        self.state.remove_private_tree(
            state_relative,
            allowed_directory_modes=_STAGING_DIRECTORY_MODES,
            allowed_file_modes=_STAGING_FILE_MODES,
            first_entry="payload",
            deadline_ns=deadline_ns,
        )

    def _publication_manifest(
        self,
        binding: SemanticReleaseDecisionBinding,
        publication_kind: str,
    ) -> tuple[dict[str, object], bytes]:
        destination = {
            "root": self._workspace(binding.repo_uuid) / "semantic-release-decisions",
            "generation": self._generation_directory(
                binding.repo_uuid,
                binding.target_generation_id,
            ),
            "file": self._binding_path(
                binding.repo_uuid,
                binding.target_generation_id,
                binding.decision_request_sha256,
            ),
        }[publication_kind]
        value: dict[str, object] = {
            "binding_bytes": len(binding.canonical),
            "binding_sha256": binding.binding_sha256,
            "contract": _STAGING_CONTRACT,
            "destination": destination.as_posix(),
            "format_version": 1,
            "generation_id": binding.target_generation_id,
            "publication_kind": publication_kind,
            "repo_uuid": binding.repo_uuid,
            "request_sha256": binding.decision_request_sha256,
        }
        canonical = canonical_json_bytes(value)
        if len(canonical) > _STAGING_MANIFEST_MAX_BYTES:
            raise SemanticReleaseDecisionInvalid(
                "decision publication manifest exceeds its byte bound"
            )
        return value, canonical

    def _remove_published_tomb(
        self,
        binding: SemanticReleaseDecisionBinding,
        expected_manifest: Mapping[str, object],
        *,
        deadline_ns: int | None,
    ) -> None:
        slot = self._staging_slot(binding.repo_uuid)
        ready = slot / "ready"
        if self._directory_names(
            ready,
            maximum_entries=1,
            deadline_ns=deadline_ns,
        ) != ("manifest.json",):
            raise SemanticReleaseDecisionInvalid(
                "published decision staging tomb is invalid"
            )
        manifest = self._validate_staging_state(
            ready,
            expected_repo_uuid=binding.repo_uuid,
            complete=False,
            allow_partial_manifest=False,
            deadline_ns=deadline_ns,
        )
        if manifest is None:  # pragma: no cover - complete manifest required above
            raise SemanticReleaseDecisionInvalid(
                "published decision staging tomb lacks a manifest"
            )
        if manifest != expected_manifest:
            raise SemanticReleaseDecisionInvalid(
                "published decision staging tomb differs from the installed binding"
            )
        installed = self._read_binding(
            binding.repo_uuid,
            binding.target_generation_id,
            binding.decision_request_sha256,
            deadline_ns=deadline_ns,
        )
        if installed is None or installed.canonical != binding.canonical:
            raise SemanticReleaseDecisionInvalid(
                "published decision staging tomb destination is not exact"
            )
        try:
            self.state.rename_exclusive_contained(
                ready,
                slot / "cleanup",
                source_kind="directory",
                label="semantic-release-decision:cleanup-transition",
                deadline_ns=deadline_ns,
            )
        except CommitUnknown:
            if self._directory_names(
                slot,
                maximum_entries=1,
                deadline_ns=deadline_ns,
            ) != ("cleanup",):
                raise
        self.state.remove_private_tree(
            slot / "cleanup",
            allowed_directory_modes=_STAGING_DIRECTORY_MODES,
            allowed_file_modes=_STAGING_FILE_MODES,
            first_entry="payload",
            deadline_ns=deadline_ns,
        )

    def _prepare_staging(
        self,
        binding: SemanticReleaseDecisionBinding,
        publication_kind: str,
        *,
        deadline_ns: int | None,
    ) -> tuple[Path, Mapping[str, object]]:
        slot = self._staging_slot(binding.repo_uuid)
        build = slot / "build"
        ready = slot / "ready"
        manifest, manifest_bytes = self._publication_manifest(
            binding,
            publication_kind,
        )
        self.state.ensure_directory(build)
        self.state.create_private_file_bytes(
            build / "manifest.json",
            manifest_bytes,
            label="semantic-release-decision:stage-manifest",
            deadline_ns=deadline_ns,
        )
        payload = build / "payload"
        binding_relative = self._staged_binding_relative(build, manifest)
        if publication_kind == "root":
            self.state.ensure_directory(payload / binding.target_generation_id)
        elif publication_kind == "generation":
            self.state.ensure_directory(payload)
        self.state.create_private_file_bytes(
            binding_relative,
            binding.canonical,
            label="semantic-release-decision:stage-binding",
            deadline_ns=deadline_ns,
        )
        self.state.fsync_contained_regular_file(
            build,
            binding_relative.relative_to(build),
            allowed_directory_modes=_STAGING_DIRECTORY_MODES,
            allowed_file_modes=_STAGING_FILE_MODES,
        )
        self.state.fault_hook(
            "semantic-release-decision:stage:binding_contained_durable"
        )
        if publication_kind == "root":
            self.state.fsync_contained_directory(
                build,
                Path("payload") / binding.target_generation_id,
                allowed_directory_modes=_STAGING_DIRECTORY_MODES,
            )
            self.state.fault_hook(
                "semantic-release-decision:stage:generation_directory_durable"
            )
        if publication_kind != "file":
            self.state.fsync_contained_directory(
                build,
                "payload",
                allowed_directory_modes=_STAGING_DIRECTORY_MODES,
            )
            self.state.fault_hook(
                "semantic-release-decision:stage:payload_directory_durable"
            )
        self.state.fsync_contained_directory(
            slot,
            "build",
            allowed_directory_modes=_STAGING_DIRECTORY_MODES,
        )
        self.state.fault_hook("semantic-release-decision:stage:build_directory_durable")
        self.state.fsync_contained_directory(
            self._workspace(binding.repo_uuid),
            "semantic-release-decision-publication",
            allowed_directory_modes=_STAGING_DIRECTORY_MODES,
        )
        self.state.fault_hook("semantic-release-decision:stage:slot_directory_durable")
        self._validate_staging_state(
            build,
            expected_repo_uuid=binding.repo_uuid,
            complete=True,
            deadline_ns=deadline_ns,
        )
        try:
            self.state.rename_exclusive_contained(
                build,
                ready,
                source_kind="directory",
                label="semantic-release-decision:stage",
                deadline_ns=deadline_ns,
            )
        except CommitUnknown as exc:
            if self._directory_names(
                slot,
                maximum_entries=1,
                deadline_ns=deadline_ns,
            ) != ("ready",):
                raise
            raise SemanticReleaseDecisionConflict(
                "decision publication staging transition is incomplete before canonical visibility"
            ) from exc
        self._validate_staging_state(
            ready,
            expected_repo_uuid=binding.repo_uuid,
            complete=True,
            deadline_ns=deadline_ns,
        )
        return ready, manifest

    def _publish_staging(
        self,
        ready: Path,
        manifest: Mapping[str, object],
        *,
        deadline_ns: int | None,
    ) -> Path:
        source = ready / "payload"
        destination = Path(cast(str, manifest["destination"]))
        source_kind = "regular" if manifest["publication_kind"] == "file" else "directory"
        published = self.state.rename_exclusive_contained(
            source,
            destination,
            source_kind=source_kind,
            label="semantic-release-decision:publish",
            deadline_ns=deadline_ns,
        )
        self.state.fault_hook("semantic-release-decision:installed")
        return published

    @staticmethod
    def _require_registered(document: Registry, repo_uuid: str) -> None:
        matches = [
            item for item in document.to_dict()["workspaces"] if item["repo_uuid"] == repo_uuid
        ]
        if len(matches) != 1:
            raise SemanticReleaseDecisionConflict(
                f"registry has no singular entry for {repo_uuid}"
            )

    def _registry_locked(self, *, deadline_ns: int | None) -> Registry:
        document = self.registry._load_locked(recover=False, deadline_ns=deadline_ns)
        if document is None:  # pragma: no cover - registry does not allow missing
            raise StateCorrupt("registry current record is missing")
        return document

    def _read_binding(
        self,
        repo_uuid: str,
        generation_id: str,
        request_sha256: str,
        *,
        deadline_ns: int | None,
    ) -> SemanticReleaseDecisionBinding | None:
        relative = self._binding_path(repo_uuid, generation_id, request_sha256)
        try:
            payload = self.state._read_optional_existing_stable_bytes(
                relative,
                max_bytes=DECISION_BINDING_MAX_BYTES,
                deadline_ns=deadline_ns,
            )
        except (StateCorrupt, StatePathError) as exc:
            raise SemanticReleaseDecisionInvalid(
                "decision binding cannot be opened safely"
            ) from exc
        if payload is None:
            return None
        try:
            binding = SemanticReleaseDecisionBinding.from_json(payload)
        except SemanticReleaseDecisionInvalid as exc:
            raise SemanticReleaseDecisionInvalid(
                f"installed decision binding is invalid: {exc}"
            ) from exc
        if (
            binding.repo_uuid != repo_uuid
            or binding.target_generation_id != generation_id
            or binding.decision_request_sha256 != request_sha256
        ):
            raise SemanticReleaseDecisionInvalid(
                "installed decision binding identity differs from its path"
            )
        return binding

    def _capture_locked(
        self,
        registry: Registry,
        repo_uuid: str,
        generation_id: str,
        request_sha256: str,
        *,
        capacity_policy: CapacityPolicy,
        deadline_ns: int | None,
    ) -> SemanticReleaseDecisionCapture:
        self._require_registered(registry, repo_uuid)
        if not self.state.private_directory_exists(
            self.generations._generation(repo_uuid, generation_id)
        ):
            raise SemanticReleaseDecisionConflict("target generation is not retained")
        self._require_gc_clear(repo_uuid, deadline_ns=deadline_ns)
        usage = self.generations.decision_capacity_usage_locked(
            repo_uuid,
            capacity_policy,
            deadline_ns=deadline_ns,
        )
        existing = self._read_binding(
            repo_uuid,
            generation_id,
            request_sha256,
            deadline_ns=deadline_ns,
        )
        return SemanticReleaseDecisionCapture(
            repo_uuid=repo_uuid,
            generation_id=generation_id,
            decision_request_sha256=request_sha256,
            capacity_policy_sha256=capacity_policy.sha256,
            registry_sha256=hashlib.sha256(registry.canonical).hexdigest(),
            usage_sha256=usage.state_sha256,
            global_bytes=usage.global_bytes,
            workspace_bytes=usage.workspace_bytes,
            unconsumed_reserved_bytes=usage.unconsumed_reserved_bytes,
            generation_binding_count=usage.generation_binding_count(generation_id),
            workspace_binding_count=usage.workspace_binding_count,
            existing_binding_sha256=(
                None if existing is None else existing.binding_sha256
            ),
            existing_binding_bytes=(0 if existing is None else len(existing.canonical)),
        )

    def _require_gc_clear(
        self,
        repo_uuid: str,
        *,
        deadline_ns: int | None,
    ) -> None:
        try:
            intent = self._gc.read_only_intent_locked(
                repo_uuid,
                deadline_ns=deadline_ns,
            )
        except GcError as exc:
            raise SemanticReleaseDecisionConflict(
                "GC eligibility state cannot be proved safe"
            ) from exc
        if intent is not None:
            raise SemanticReleaseDecisionConflict(
                "an unresolved GC intent blocks decision installation"
            )

    def capture(
        self,
        repo_uuid: str,
        generation_id: str,
        decision_request_sha256: str,
        *,
        capacity_policy: CapacityPolicy,
        deadline_ns: int | None = None,
    ) -> SemanticReleaseDecisionCapture:
        canonical_uuid = _repo_uuid(repo_uuid)
        canonical_generation = _generation_id(generation_id)
        canonical_request = _sha256(
            decision_request_sha256, "decision_request_sha256"
        )
        policy = self.generations._validated_capacity_policy(capacity_policy)
        with self.registry.read_only_snapshot(deadline_ns=deadline_ns) as registry:
            self._require_registered(registry, canonical_uuid)
            with self.leases.read_only_workspace_lock(
                canonical_uuid,
                deadline_ns=deadline_ns,
            ):
                with self.state.existing_generation_lock(
                    self.generations._lock(canonical_uuid, canonical_generation),
                    generation_id=canonical_generation,
                    exclusive=False,
                    deadline_ns=deadline_ns,
                ):
                    return self._capture_locked(
                        registry,
                        canonical_uuid,
                        canonical_generation,
                        canonical_request,
                        capacity_policy=policy,
                        deadline_ns=deadline_ns,
                    )

    @staticmethod
    def _validate_capture(
        capture: SemanticReleaseDecisionCapture,
        binding: SemanticReleaseDecisionBinding,
        policy: CapacityPolicy,
    ) -> None:
        if type(capture) is not SemanticReleaseDecisionCapture:
            raise SemanticReleaseDecisionInvalid(
                "capture must be a closed SemanticReleaseDecisionCapture"
            )
        if type(binding) is not SemanticReleaseDecisionBinding:
            raise SemanticReleaseDecisionInvalid(
                "binding must be a closed SemanticReleaseDecisionBinding"
            )
        reparsed = SemanticReleaseDecisionBinding.from_json(binding.canonical)
        if reparsed != binding:
            raise SemanticReleaseDecisionInvalid("binding object is not canonical")
        if (
            binding.repo_uuid != capture.repo_uuid
            or binding.target_generation_id != capture.generation_id
            or binding.decision_request_sha256 != capture.decision_request_sha256
        ):
            raise SemanticReleaseDecisionConflict(
                "binding identity differs from the captured request path"
            )
        if policy.sha256 != capture.capacity_policy_sha256:
            raise SemanticReleaseDecisionConflict("capacity policy changed after capture")

    def _require_capture_stable(
        self,
        capture: SemanticReleaseDecisionCapture,
        current_usage: DecisionCapacityUsage,
        current_binding: SemanticReleaseDecisionBinding | None,
        candidate: SemanticReleaseDecisionBinding,
    ) -> bool:
        if capture.existing_binding_sha256 is not None:
            if (
                current_binding is None
                or current_binding.binding_sha256 != capture.existing_binding_sha256
                or len(current_binding.canonical) != capture.existing_binding_bytes
                or current_usage.state_sha256 != capture.usage_sha256
            ):
                raise SemanticReleaseDecisionConflict(
                    "decision namespace changed after capture"
                )
            if current_binding.canonical != candidate.canonical:
                raise SemanticReleaseDecisionConflict(
                    "request-derived path already contains different bytes"
                )
            return True
        if current_binding is None:
            if current_usage.state_sha256 != capture.usage_sha256:
                raise SemanticReleaseDecisionConflict(
                    "decision namespace or capacity usage changed after capture"
                )
            return False
        if current_binding.canonical != candidate.canonical:
            raise SemanticReleaseDecisionConflict(
                "request-derived path already contains different bytes"
            )
        if (
            current_usage.state_sha256_without_binding(
                capture.repo_uuid,
                capture.generation_id,
                capture.decision_request_sha256,
                len(candidate.canonical),
                candidate.binding_sha256,
            )
            != capture.usage_sha256
        ):
            raise SemanticReleaseDecisionConflict(
                "concurrent replay included unrelated capacity drift"
            )
        return True

    def install(
        self,
        capture: SemanticReleaseDecisionCapture,
        binding: SemanticReleaseDecisionBinding,
        *,
        capacity_policy: CapacityPolicy,
        deadline_ns: int | None = None,
    ) -> SemanticReleaseDecisionBinding:
        policy = self.generations._validated_capacity_policy(capacity_policy)
        self._validate_capture(capture, binding, policy)
        with self.registry.existing_exclusive_lock(deadline_ns=deadline_ns):
            registry = self._registry_locked(deadline_ns=deadline_ns)
            self._require_registered(registry, capture.repo_uuid)
            if hashlib.sha256(registry.canonical).hexdigest() != capture.registry_sha256:
                raise SemanticReleaseDecisionConflict("registry changed after capture")
            with self.leases.workspace_lock(
                capture.repo_uuid,
                deadline_ns=deadline_ns,
            ):
                with self.state.existing_generation_lock(
                    self.generations._lock(capture.repo_uuid, capture.generation_id),
                    generation_id=capture.generation_id,
                    exclusive=False,
                    deadline_ns=deadline_ns,
                ):
                    if not self.state.private_directory_exists(
                        self.generations._generation(
                            capture.repo_uuid, capture.generation_id
                        )
                    ):
                        raise SemanticReleaseDecisionConflict(
                            "target generation changed after capture"
                        )
                    self._require_gc_clear(
                        capture.repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                    self._cleanup_staging(
                        capture.repo_uuid,
                        deadline_ns=deadline_ns,
                    )
                    current_usage = self.generations.decision_capacity_usage_locked(
                        capture.repo_uuid,
                        policy,
                        deadline_ns=deadline_ns,
                    )
                    try:
                        current_binding = self._read_binding(
                            capture.repo_uuid,
                            capture.generation_id,
                            capture.decision_request_sha256,
                            deadline_ns=deadline_ns,
                        )
                    except SemanticReleaseDecisionInvalid as exc:
                        raise SemanticReleaseDecisionConflict(
                            "decision namespace changed after capture"
                        ) from exc
                    replay = self._require_capture_stable(
                        capture,
                        current_usage,
                        current_binding,
                        binding,
                    )
                    additional_bytes = 0 if replay else len(binding.canonical)
                    self.generations.preflight_decision_install_locked(
                        capture.repo_uuid,
                        capture.generation_id,
                        candidate_bytes=len(binding.canonical),
                        additional_bytes=additional_bytes,
                        capacity_policy=policy,
                        usage=current_usage,
                    )
                    if replay:
                        replay_usage = self.generations.decision_capacity_usage_locked(
                            capture.repo_uuid,
                            policy,
                            deadline_ns=deadline_ns,
                        )
                        if capture.existing_binding_sha256 is not None:
                            stable_usage_sha256 = replay_usage.state_sha256
                        else:
                            stable_usage_sha256 = replay_usage.state_sha256_without_binding(
                                capture.repo_uuid,
                                capture.generation_id,
                                capture.decision_request_sha256,
                                len(binding.canonical),
                                binding.binding_sha256,
                            )
                        if stable_usage_sha256 != capture.usage_sha256:
                            raise SemanticReleaseDecisionConflict(
                                "decision namespace changed during replay proof"
                            )
                        if current_binding is None:  # pragma: no cover - replay proves presence
                            raise SemanticReleaseDecisionConflict(
                                "decision binding disappeared during replay proof"
                            )
                        return current_binding
                    try:
                        decision_root = self._workspace(
                            capture.repo_uuid
                        ) / "semantic-release-decisions"
                        if not self.state.private_directory_exists(decision_root):
                            publication_kind = "root"
                        elif not self.state.private_directory_exists(
                            self._generation_directory(
                                capture.repo_uuid,
                                capture.generation_id,
                            )
                        ):
                            publication_kind = "generation"
                        else:
                            publication_kind = "file"
                        try:
                            ready, manifest = self._prepare_staging(
                                binding,
                                publication_kind,
                                deadline_ns=deadline_ns,
                            )
                        except CommitUnknown as staging_exc:
                            raise SemanticReleaseDecisionConflict(
                                "decision publication staging is incomplete before canonical visibility"
                            ) from staging_exc
                        self._publish_staging(
                            ready,
                            manifest,
                            deadline_ns=deadline_ns,
                        )
                    except BaseException as exc:
                        try:
                            reopened = self._read_binding(
                                capture.repo_uuid,
                                capture.generation_id,
                                capture.decision_request_sha256,
                                deadline_ns=deadline_ns,
                            )
                        except BaseException as reopen_exc:
                            raise CommitUnknown(
                                "decision binding outcome is uncertain after possible visibility"
                            ) from reopen_exc
                        if reopened is not None and reopened.canonical == binding.canonical:
                            try:
                                adopted_usage = (
                                    self.generations.decision_capacity_usage_locked(
                                        capture.repo_uuid,
                                        policy,
                                        deadline_ns=deadline_ns,
                                    )
                                )
                                if (
                                    adopted_usage.state_sha256_without_binding(
                                        capture.repo_uuid,
                                        capture.generation_id,
                                        capture.decision_request_sha256,
                                        len(binding.canonical),
                                        binding.binding_sha256,
                                    )
                                    != capture.usage_sha256
                                ):
                                    raise CommitUnknown(
                                        "decision namespace changed across uncertain install"
                                    )
                            except CommitUnknown:
                                raise
                            except BaseException as usage_exc:
                                raise CommitUnknown(
                                    "decision namespace is ambiguous after uncertain install"
                                ) from usage_exc
                            return reopened
                        if reopened is not None and not isinstance(exc, CommitUnknown):
                            raise SemanticReleaseDecisionConflict(
                                "request-derived path already contains different bytes"
                            ) from exc
                        if isinstance(exc, CommitUnknown):
                            raise
                        try:
                            observed = self.generations.decision_capacity_usage_locked(
                                capture.repo_uuid,
                                policy,
                                deadline_ns=deadline_ns,
                            )
                        except BaseException as usage_exc:
                            raise CommitUnknown(
                                "decision namespace is ambiguous after install failure"
                            ) from usage_exc
                        if observed.state_sha256 != capture.usage_sha256:
                            raise CommitUnknown(
                                "decision namespace changed during failed install"
                            ) from exc
                        raise
                    try:
                        reopened = self._read_binding(
                            capture.repo_uuid,
                            capture.generation_id,
                            capture.decision_request_sha256,
                            deadline_ns=deadline_ns,
                        )
                        if reopened is None or reopened.canonical != binding.canonical:
                            raise CommitUnknown(
                                "decision install could not prove the exact completed binding"
                            )
                        final_usage = self.generations.decision_capacity_usage_locked(
                            capture.repo_uuid,
                            policy,
                            deadline_ns=deadline_ns,
                        )
                        if (
                            final_usage.state_sha256_without_binding(
                                capture.repo_uuid,
                                capture.generation_id,
                                capture.decision_request_sha256,
                                len(binding.canonical),
                                binding.binding_sha256,
                            )
                            != capture.usage_sha256
                        ):
                            raise CommitUnknown(
                                "decision capacity state changed across the install boundary"
                            )
                        self._remove_published_tomb(
                            binding,
                            manifest,
                            deadline_ns=deadline_ns,
                        )
                        return reopened
                    except CommitUnknown:
                        raise
                    except BaseException as exc:
                        raise CommitUnknown(
                            "decision install final proof is uncertain after visibility"
                        ) from exc


__all__ = [
    "DECISION_BINDINGS_PER_GENERATION",
    "DECISION_BINDINGS_PER_WORKSPACE",
    "DECISION_BINDING_MAX_BYTES",
    "SEMANTIC_RELEASE_DECISION_CONTRACT",
    "SEMANTIC_RELEASE_DECISION_FORMAT_VERSION",
    "SemanticReleaseDecisionBinding",
    "SemanticReleaseDecisionCapture",
    "SemanticReleaseDecisionConflict",
    "SemanticReleaseDecisionError",
    "SemanticReleaseDecisionInvalid",
    "SemanticReleaseDecisionStore",
]
