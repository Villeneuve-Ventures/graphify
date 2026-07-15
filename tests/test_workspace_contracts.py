from __future__ import annotations

import copy
import json
from pathlib import Path
import re

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry as SchemaRegistry, Resource

from graphify.workspace import (
    CompensationPlan,
    ContractError,
    InstallerTransaction,
    JournalEvent,
    OfflineRollback,
    UnsupportedContractVersion,
    WORKSPACE_SCHEMA_FILES,
    WorkspaceConfig,
    canonical_json_bytes,
    canonical_sha256,
    decode_journal_frame,
    encode_journal_frame,
    load_schema,
    parse_contract,
    validate_installer_compensation,
)


FIXTURES = Path(__file__).parent / "fixtures" / "workspace" / "v1"
SCHEMAS = Path(__file__).parents[1] / "graphify" / "workspace" / "schemas" / "v1"


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.fixture(scope="module")
def schema_registry() -> SchemaRegistry:
    registry = SchemaRegistry()
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        contents = _json(path)
        registry = registry.with_resource(contents["$id"], Resource.from_contents(contents))
    return registry


def _validate_schema(value: dict, schema_registry: SchemaRegistry) -> None:
    schema = load_schema(value["contract"])
    Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    ).validate(value)


def _registry_with_activation_acceptance() -> dict:
    value = _json(FIXTURES / "positive" / "registry.json")
    value["workspaces"][0]["active_source_evidence"].update(
        {"operation_epoch": 7, "fence_token": 11}
    )
    return value


def test_workspace_toml_positive_round_trip_and_schema(schema_registry: SchemaRegistry) -> None:
    path = FIXTURES / "positive" / "workspace.toml"
    config = WorkspaceConfig.from_toml(path.read_bytes())

    assert config.contract == "graphify.workspace.config"
    assert config.to_dict()["repo_uuid"] == "11111111-1111-4111-8111-111111111111"
    _validate_schema(config.to_dict(), schema_registry)
    assert WorkspaceConfig.from_json(config.canonical).canonical == config.canonical


def test_every_positive_json_fixture_passes_schema_model_and_round_trip(
    schema_registry: SchemaRegistry,
) -> None:
    paths = sorted((FIXTURES / "positive").glob("*.json"))
    assert len(paths) == 14

    for path in paths:
        value = _json(path)
        _validate_schema(value, schema_registry)
        document = parse_contract(value)
        assert document.canonical.endswith(b"\n"), path.name
        assert parse_contract(document.canonical).canonical == document.canonical, path.name
        assert document.to_dict() == json.loads(document.canonical), path.name


@pytest.mark.parametrize(
    "name",
    [
        "registry-missing-active-source.json",
        "registry-noncanonical-remote-alias.json",
        "registry-zero-activation-fence.json",
        "generation-receipt-symlink.json",
        "generation-receipt-outside-root.json",
        "freshness-linearizable-claim.json",
        "installer-mutate-generations.json",
    ],
)
def test_negative_json_fixtures_fail_schema_and_reference_model(
    name: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "negative" / name)
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value)), name
    with pytest.raises(ContractError):
        parse_contract(value)


def test_repo_config_rejects_lifecycle_path_override() -> None:
    text = (FIXTURES / "negative" / "workspace-lifecycle-override.toml").read_text()

    with pytest.raises(ContractError, match="unexpected field.*state_root"):
        WorkspaceConfig.from_toml(text)


def test_unknown_schema_version_is_rejected_before_event_fields() -> None:
    value = _json(FIXTURES / "version-rejection" / "journal-event-v2.json")

    with pytest.raises(UnsupportedContractVersion, match="expected 1, got 2"):
        parse_contract(value)


def test_unsupported_engine_and_schema_tuple_is_rejected() -> None:
    value = _json(FIXTURES / "version-rejection" / "compatibility-future.json")

    with pytest.raises(ContractError, match="unsupported workspace candidate"):
        parse_contract(value)


def test_canonicalization_is_key_order_and_whitespace_independent() -> None:
    ordered = _json(FIXTURES / "positive" / "journal-event.json")
    unordered = _json(FIXTURES / "canonical" / "journal-event-unordered.json")

    assert canonical_json_bytes(ordered) == canonical_json_bytes(unordered)
    assert parse_contract(ordered).sha256 == parse_contract(unordered).sha256


def test_canonicalization_normalizes_unicode_and_rejects_floats() -> None:
    assert canonical_json_bytes({"name": "Cafe\u0301"}) == canonical_json_bytes(
        {"name": "Caf\u00e9"}
    )
    with pytest.raises(ContractError, match="floating-point values are forbidden"):
        canonical_json_bytes({"timeout": 1.5})


def test_json_parsing_rejects_duplicate_keys_before_contract_validation() -> None:
    duplicate = b'{"contract":"graphify.workspace.config","contract":"graphify.workspace.config"}'

    with pytest.raises(ContractError, match="duplicate JSON key.*contract"):
        parse_contract(duplicate)
    with pytest.raises(ContractError, match="duplicate JSON key.*contract"):
        WorkspaceConfig.from_json(duplicate)


def test_journal_frame_round_trip_rejects_every_truncation_and_tamper() -> None:
    value = _json(FIXTURES / "positive" / "journal-event.json")
    event = parse_contract(value, expected=JournalEvent)
    frame = encode_journal_frame(event)

    assert decode_journal_frame(frame).canonical == event.canonical
    for cut in range(len(frame)):
        with pytest.raises(ContractError):
            decode_journal_frame(frame[:cut])

    tampered = bytearray(frame)
    tampered[-2] ^= 1
    with pytest.raises(ContractError, match="checksum mismatch"):
        decode_journal_frame(bytes(tampered))


def test_pointer_prior_record_requires_monotonic_replacement() -> None:
    value = _json(FIXTURES / "positive" / "prior-pointer.json")
    value["pointer_set"]["pointer_revision"] = 2
    value["replaced_by_revision"] = 2

    with pytest.raises(ContractError, match="must exceed retained pointer revision"):
        parse_contract(value)


def test_generation_payload_entries_are_sorted_unique_regular_files() -> None:
    value = _json(FIXTURES / "positive" / "generation-receipt.json")
    duplicate = copy.deepcopy(value["sealed_query_payload"]["entries"][0])
    value["sealed_query_payload"]["entries"].append(duplicate)

    with pytest.raises(ContractError, match="unique and sorted"):
        parse_contract(value)


@pytest.mark.parametrize(
    ("fixture", "field_path"),
    [
        ("generation-receipt.json", ("fence_token",)),
        ("journal-event.json", ("operation_epoch",)),
        ("journal-event.json", ("fence_token",)),
        ("pointer-set.json", ("fence_token",)),
        ("prior-pointer.json", ("pointer_set", "fence_token")),
        ("freshness-release.json", ("pre_observation", "fence_token")),
    ],
)
def test_accepted_fence_fields_are_required_by_schema_and_model(
    fixture: str,
    field_path: tuple[str, ...],
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / fixture)
    parent = value
    for part in field_path[:-1]:
        parent = parent[part]
    parent.pop(field_path[-1])
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="missing required field"):
        parse_contract(value)


@pytest.mark.parametrize(
    ("fixture", "field_path"),
    [
        ("generation-receipt.json", ("fence_token",)),
        ("journal-event.json", ("fence_token",)),
        ("pointer-set.json", ("fence_token",)),
        ("freshness-release.json", ("pre_observation", "fence_token")),
    ],
)
def test_stale_fence_tokens_fail_closed(
    fixture: str,
    field_path: tuple[str, ...],
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / fixture)
    parent = value
    for part in field_path[:-1]:
        parent = parent[part]
    parent[field_path[-1]] = 0
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="fence_token.*integer >= 1"):
        parse_contract(value)


def test_fence_token_and_operation_epoch_are_distinct_cas_fields() -> None:
    receipt = _json(FIXTURES / "positive" / "generation-receipt.json")
    pointer = _json(FIXTURES / "positive" / "pointer-set.json")
    receipt["operation_epoch"] = 7
    receipt["fence_token"] = 11
    pointer["operation_epoch"] = 7
    pointer["fence_token"] = 11

    assert parse_contract(receipt).to_dict()["operation_epoch"] == 7
    assert parse_contract(receipt).to_dict()["fence_token"] == 11
    assert parse_contract(pointer).to_dict()["operation_epoch"] == 7
    assert parse_contract(pointer).to_dict()["fence_token"] == 11


def test_rolled_back_journal_event_is_post_certification_and_framed(
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "journal-event-rolled-back.json")

    _validate_schema(value, schema_registry)
    event = parse_contract(value, expected=JournalEvent)
    assert event.to_dict()["transition"] == "ROLLED_BACK"
    assert decode_journal_frame(encode_journal_frame(event)).canonical == event.canonical


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("root", "other-root"),
        ("entry", "outside/graph.json"),
        ("entry", "graphify-out"),
    ],
)
def test_sealed_query_payload_is_strictly_contained_under_graphify_out(
    field: str,
    replacement: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "generation-receipt.json")
    if field == "root":
        value["sealed_query_payload"]["root"] = replacement
    else:
        value["sealed_query_payload"]["entries"][0]["path"] = replacement
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="graphify-out|strict descendant"):
        parse_contract(value)


def test_model_matches_schema_uniqueness_for_validations_and_backends(
    schema_registry: SchemaRegistry,
) -> None:
    receipt = _json(FIXTURES / "positive" / "generation-receipt.json")
    receipt["validations"] = ["payload_manifest", "payload_manifest"]
    config = WorkspaceConfig.from_toml(
        (FIXTURES / "positive" / "workspace.toml").read_bytes()
    ).to_dict()
    config["policy"]["semantic_mode"] = "explicit_backend"
    config["policy"]["headless_backends"] = ["gemini", "gemini"]

    for value, message in (
        (receipt, "validations.*unique"),
        (config, "headless_backends.*unique"),
    ):
        schema = load_schema(value["contract"])
        validator = Draft202012Validator(
            schema,
            registry=schema_registry,
            format_checker=FormatChecker(),
        )
        assert list(validator.iter_errors(value))
        with pytest.raises(ContractError, match=message):
            parse_contract(value)


def test_schema_catalog_is_complete_and_self_consistent(schema_registry: SchemaRegistry) -> None:
    positive_contracts = {
        _json(path)["contract"] for path in (FIXTURES / "positive").glob("*.json")
    }
    positive_contracts.add("graphify.workspace.config")
    wrapper_ids: set[str] = set()

    for contract in sorted(positive_contracts):
        schema = load_schema(contract)
        Draft202012Validator.check_schema(schema)
        wrapper_ids.add(schema["$id"])

    assert len(wrapper_ids) == 14
    assert schema_registry


def test_schema_catalog_has_an_explicit_frozen_member_set() -> None:
    assert WORKSPACE_SCHEMA_FILES == (
        "common.schema.json",
        "artifact-manifest.schema.json",
        "compatibility-manifest.schema.json",
        "compensation-plan.schema.json",
        "config.schema.json",
        "fenced-lease.schema.json",
        "freshness-release.schema.json",
        "generation-coordination-lock.schema.json",
        "generation-receipt.schema.json",
        "installer-transaction.schema.json",
        "journal-event.schema.json",
        "offline-rollback.schema.json",
        "pointer-set.schema.json",
        "prior-pointer.schema.json",
        "registry.schema.json",
    )
    assert {path.name for path in SCHEMAS.iterdir() if path.is_file()} == set(
        WORKSPACE_SCHEMA_FILES
    )


def test_observed_current_release_requires_identical_complete_observations() -> None:
    value = _json(FIXTURES / "positive" / "freshness-release.json")
    value["post_observation"]["inventory_sha256"] = "8" * 64

    with pytest.raises(ContractError, match="pre_observation.*post_observation"):
        parse_contract(value)


def test_compatibility_requires_pinned_upstream_and_complete_artifact_tuple(
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "compatibility-manifest.json")
    required = {
        "graphifyy-0.9.16+workspace.1-py3-none-any.whl",
        "skill-bundle.zip",
        "contract-bundle.zip",
        "fixture-bundle.zip",
        "fixture-manifest.json",
        "runtime-bundle.zip",
        "runtime-requirements.txt",
        "sbom.cdx.json",
        "offline-rollback.zip",
        "provenance.json",
    }
    value["artifacts"] = {name: "a" * 64 for name in sorted(required)}
    value["artifacts"]["skill-bundle.zip"] = value["skill_bundle_sha256"]
    value["artifacts"]["contract-bundle.zip"] = value["contract_bundle_sha256"]
    value["artifacts"]["fixture-manifest.json"] = value["fixture_manifest_sha256"]
    value["artifacts"]["provenance.json"] = value["provenance_sha256"]
    value["artifacts"]["sbom.cdx.json"] = value["sbom_sha256"]

    wrong_upstream = copy.deepcopy(value)
    wrong_upstream["upstream_commit"] = "b" * 40
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors(wrong_upstream))
    with pytest.raises(ContractError, match="exact upstream baseline"):
        parse_contract(wrong_upstream)

    incomplete = copy.deepcopy(value)
    incomplete["artifacts"].pop("offline-rollback.zip")
    assert list(validator.iter_errors(incomplete))
    with pytest.raises(ContractError, match="artifact tuple"):
        parse_contract(incomplete)


def test_installer_items_must_be_contained_by_declared_home_roots() -> None:
    value = _json(FIXTURES / "positive" / "installer-transaction.json")
    value["items"][0]["path"] = "/tmp/unrelated/bin/graphify"

    with pytest.raises(ContractError, match="outside declared HOME/CODEX_HOME"):
        parse_contract(value)


def test_registry_remote_aliases_are_normalized_unique_and_sorted() -> None:
    value = _json(FIXTURES / "positive" / "registry.json")
    aliases = value["workspaces"][0]["active_source"]["remote_aliases"]
    aliases.append(
        {
            "url": aliases[0]["url"],
            "evidence_sha256": "9" * 64,
        }
    )

    with pytest.raises(ContractError, match="remote_aliases.*unique and sorted"):
        parse_contract(value)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("active_source_revision", 2, "must match workspace active_source_revision"),
        ("source_sha256", "9" * 64, "must match active_source"),
    ],
)
def test_registry_active_source_evidence_is_revision_and_source_bound(
    field: str,
    replacement: object,
    message: str,
) -> None:
    value = _json(FIXTURES / "positive" / "registry.json")
    value["workspaces"][0]["active_source_evidence"][field] = replacement

    with pytest.raises(ContractError, match=message):
        parse_contract(value)


def test_registry_activation_acceptance_round_trips_distinct_epoch_and_fence(
    schema_registry: SchemaRegistry,
) -> None:
    value = _registry_with_activation_acceptance()

    _validate_schema(value, schema_registry)
    document = parse_contract(value)

    evidence = document.to_dict()["workspaces"][0]["active_source_evidence"]
    assert evidence["operation_epoch"] == 7
    assert evidence["fence_token"] == 11
    assert parse_contract(document.canonical).canonical == document.canonical


@pytest.mark.parametrize("field", ["operation_epoch", "fence_token"])
@pytest.mark.parametrize("mutation", ["missing", "zero"])
def test_registry_activation_acceptance_fails_closed(
    field: str,
    mutation: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _registry_with_activation_acceptance()
    evidence = value["workspaces"][0]["active_source_evidence"]
    if mutation == "missing":
        evidence.pop(field)
    else:
        evidence[field] = 0
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(
        ContractError,
        match=f"(?:missing required field.*{field}|{field}.*integer >= 1)",
    ):
        parse_contract(value)


def test_registry_activation_evidence_has_canonical_fixture() -> None:
    ordered = _registry_with_activation_acceptance()
    unordered = _json(FIXTURES / "canonical" / "registry-unordered.json")

    assert canonical_json_bytes(ordered) == canonical_json_bytes(unordered)
    assert parse_contract(ordered).sha256 == parse_contract(unordered).sha256


def test_registry_stale_activation_evidence_is_rejected_by_normative_model(
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "negative" / "registry-stale-activation-evidence.json")

    _validate_schema(value, schema_registry)
    with pytest.raises(ContractError, match="must match workspace active_source_revision"):
        parse_contract(value)


def test_registry_uuid_enrollment_evidence_is_bound_to_authoritative_identity() -> None:
    value = _json(FIXTURES / "positive" / "registry.json")
    value["workspaces"][0]["uuid_enrollment"]["repo_uuid"] = (
        "22222222-2222-4222-8222-222222222222"
    )

    with pytest.raises(ContractError, match="must match workspace repo_uuid"):
        parse_contract(value)


@pytest.mark.parametrize("mutation", ["no-actions", "duplicate", "no-offline-artifacts"])
def test_compensation_plan_requires_unique_actions_and_offline_artifacts(
    mutation: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "compensation-plan.json")
    if mutation == "no-actions":
        value["restore_order"] = []
        value["remove_if_created"] = []
    elif mutation == "duplicate":
        value["restore_order"].append(value["restore_order"][0])
    else:
        value["required_offline_artifacts"] = []
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="action|required_offline_artifacts|unique"):
        parse_contract(value)


def test_installer_compensation_fixtures_are_canonically_linked() -> None:
    transaction = parse_contract(
        _json(FIXTURES / "positive" / "installer-transaction.json"),
        expected=InstallerTransaction,
    )
    plan = parse_contract(
        _json(FIXTURES / "positive" / "compensation-plan.json"),
        expected=CompensationPlan,
    )
    rollback = parse_contract(
        _json(FIXTURES / "positive" / "offline-rollback.json"),
        expected=OfflineRollback,
    )

    evidence = validate_installer_compensation(transaction, plan, rollback)

    assert transaction.to_dict()["compensation_plan_sha256"] == canonical_sha256(
        plan.to_dict()
    )
    assert plan.to_dict()["remove_if_created"] == []
    assert evidence == {
        "compensation_plan_sha256": plan.sha256,
        "installer_item_count": 2,
        "offline_rollback_sha256": rollback.sha256,
        "remove_action_count": 0,
        "required_offline_artifact_count": 2,
        "restore_mapping_count": 2,
        "restore_action_count": 2,
        "transaction_id": "33333333-3333-4333-8333-333333333333",
        "validated": True,
    }


@pytest.mark.parametrize("mutation", ["mapping-order", "mapping-digest"])
def test_installer_compensation_restore_mapping_is_ordered_and_digest_bound(
    mutation: str,
) -> None:
    transaction = _json(FIXTURES / "positive" / "installer-transaction.json")
    plan = _json(FIXTURES / "positive" / "compensation-plan.json")
    rollback = _json(FIXTURES / "positive" / "offline-rollback.json")
    transaction["items"][0]["before_sha256"] = rollback["entries"][0]["sha256"]
    transaction["items"][1]["before_sha256"] = rollback["entries"][1]["sha256"]
    plan["restore_artifacts"] = [
        {
            "path": plan["restore_order"][0],
            "offline_artifact": plan["required_offline_artifacts"][0],
        },
        {
            "path": plan["restore_order"][1],
            "offline_artifact": plan["required_offline_artifacts"][1],
        },
    ]
    if mutation == "mapping-order":
        plan["restore_artifacts"].reverse()
    else:
        plan["restore_artifacts"][0]["offline_artifact"] = plan["required_offline_artifacts"][1]
    transaction["compensation_plan_sha256"] = canonical_sha256(plan)

    with pytest.raises(ContractError, match="mapping|digest|restore"):
        validate_installer_compensation(transaction, plan, rollback)


@pytest.mark.parametrize(
    "mutation",
    [
        "transaction-id",
        "plan-hash",
        "duplicate-item",
        "wrong-action-class",
        "unrelated-action",
        "missing-offline-artifact",
    ],
)
def test_installer_compensation_cross_document_invariants_fail_closed(
    mutation: str,
) -> None:
    transaction = _json(FIXTURES / "positive" / "installer-transaction.json")
    plan = _json(FIXTURES / "positive" / "compensation-plan.json")
    rollback = _json(FIXTURES / "positive" / "offline-rollback.json")
    if mutation == "transaction-id":
        plan["transaction_id"] = "44444444-4444-4444-8444-444444444444"
    elif mutation == "plan-hash":
        transaction["compensation_plan_sha256"] = "9" * 64
    elif mutation == "duplicate-item":
        transaction["items"].append(copy.deepcopy(transaction["items"][0]))
    elif mutation == "wrong-action-class":
        path = plan["restore_order"].pop()
        plan["remove_if_created"].append(path)
    elif mutation == "unrelated-action":
        plan["restore_order"].append("/tmp/graphify-home/unrelated")
    else:
        plan["required_offline_artifacts"] = ["prior/missing"]
    if mutation not in {"plan-hash", "duplicate-item"}:
        transaction["compensation_plan_sha256"] = canonical_sha256(plan)

    with pytest.raises(ContractError):
        validate_installer_compensation(transaction, plan, rollback)


@pytest.mark.parametrize(
    ("fixture", "field"),
    [
        ("pointer-set.json", "state_schema_version"),
        ("artifact-manifest.json", "manifest_version"),
        ("offline-rollback.json", "bundle_version"),
    ],
)
def test_nested_contract_versions_fail_closed(fixture: str, field: str) -> None:
    value = _json(FIXTURES / "positive" / fixture)
    value[field] = 2

    with pytest.raises(UnsupportedContractVersion, match=f"{field}.*expected 1, got 2"):
        parse_contract(value)


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "incomplete"])
def test_offline_rollback_restore_order_is_nonempty_unique_and_exact(
    mutation: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "offline-rollback.json")
    if mutation == "empty":
        value["restore_order"] = []
    elif mutation == "duplicate":
        value["restore_order"].append(value["restore_order"][0])
    else:
        value["restore_order"].pop()
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    if mutation != "incomplete":
        assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="restore_order.*every entry exactly once"):
        parse_contract(value)


def test_noninitial_journal_event_requires_prior_event_hash(
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "journal-event.json")
    value["sequence"] = 2
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="noninitial event requires"):
        parse_contract(value)


@pytest.mark.parametrize("operation", ["ACTIVATE", "POINTER_RECOVERY"])
def test_fenced_lease_includes_every_pointer_moving_operation(
    operation: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "fenced-lease.json")
    value["operation"] = operation

    _validate_schema(value, schema_registry)
    assert parse_contract(value).to_dict()["operation"] == operation


@pytest.mark.parametrize(
    "transition",
    ["ALLOCATED", "STAGING", "BUILT", "VALIDATING", "FAILED"],
)
def test_precertification_journal_events_have_no_receipt_or_pointer(
    transition: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "journal-event.json")
    value["transition"] = transition
    value["receipt_sha256"] = None
    value["pointer_revision"] = None

    _validate_schema(value, schema_registry)
    assert parse_contract(value).to_dict()["receipt_sha256"] is None


@pytest.mark.parametrize("field", ["receipt_sha256", "pointer_revision"])
def test_precertification_journal_events_reject_sealed_state_references(
    field: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "journal-event.json")
    value["transition"] = "STAGING"
    value["receipt_sha256"] = None
    value["pointer_revision"] = None
    value[field] = "6" * 64 if field == "receipt_sha256" else 0
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="precertification"):
        parse_contract(value)


@pytest.mark.parametrize("transition", ["CERTIFIED", "PROMOTED", "SUPERSEDED", "REPAIRED"])
@pytest.mark.parametrize("field", ["receipt_sha256", "pointer_revision"])
def test_certified_and_later_journal_events_require_receipt_and_pointer(
    transition: str,
    field: str,
    schema_registry: SchemaRegistry,
) -> None:
    value = _json(FIXTURES / "positive" / "journal-event.json")
    value["transition"] = transition
    value[field] = None
    schema = load_schema(value["contract"])
    validator = Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )

    assert list(validator.iter_errors(value))
    with pytest.raises(ContractError, match="certified journal event"):
        parse_contract(value)


def test_ci_runs_for_pull_requests_targeting_baseline_branches() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert re.search(r"pull_request:\n\s+branches:.*\"baseline/\*\*\"", workflow)
