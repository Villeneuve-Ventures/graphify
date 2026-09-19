"""S2 contracts against actual merged S1 evidence, including failed extraction."""
import json
from pathlib import Path

import pytest

from graphify.source_io import SourceIO
from graphify.extract import ExtractionIncomplete, extract
from graphify.workspace.contracts import (
    CompatibilityManifest, CompletionBinding, ContractError, InputManifest,
    INSTALLATION_METADATA, SCHEMA_FILES, StateRootMarker, canonical_json_bytes, decode_canonical,
)
from tools.workspace_artifacts.candidate import package_members

REPO = Path(__file__).resolve().parents[1]


def compatibility():
    from graphify.workspace.contracts import DETECTOR_ID, ENGINE_BASELINE, EXTRACTOR_CACHE_ABI
    return CompatibilityManifest.from_mapping({
        "contract": "graphify.workspace.compatibility", "schema_version": 2,
        "distribution": "graphifyy", "distribution_version": "0.10.0",
        "distribution_build": "fixture:sha256:" + "a" * 64,
        "source_manifest_sha256": "a" * 64, "wheel_sha256": "b" * 64,
        "engine_baseline": ENGINE_BASELINE, "extractor_cache_abi": EXTRACTOR_CACHE_ABI,
        "adapter_contract_version": 2, "state_schema_version": 2,
        "detector_id": DETECTOR_ID, "graph_payload_version": 1, "input_manifest_version": 1,
        "candidate_kind": "local-fixture", "certified": False,
        "package_members": package_members(REPO),
        "installation_metadata": {name: "b" * 64 for name in INSTALLATION_METADATA},
    })


def manifests(tmp_path):
    root = tmp_path.resolve()
    path = root / "empty.json"
    path.write_text("{}")
    with SourceIO(root) as inputs:
        inputs.listdir(root)
        inputs.probe(root / "optional.json")
        initial = InputManifest.from_engine(inputs, phase="detection", code_inputs=[path])
        result = extract([path], source_io=inputs, quiet=True, ambient_output=False)
        final = InputManifest.from_engine(inputs, phase="consumed", code_inputs=[path],
                                          outcomes=result["outcomes"])
    return initial, final


def test_actual_empty_extraction_and_two_phase_binding(tmp_path):
    initial, final = manifests(tmp_path)
    assert not initial.complete and final.complete
    assert final.to_dict()["outcomes"] == [{"path": "empty.json", "status": "empty"}]
    assert initial.sha256 != final.sha256
    result = CompletionBinding.bind(initial, final, compatibility=compatibility(), graph_sha256="c" * 64)
    assert result.to_dict()["initial_detection_sha256"] == initial.sha256
    assert result.to_dict()["consumed_inputs_sha256"] == final.sha256
    assert InputManifest.from_json(final.canonical) == final
    changed = final.to_dict()
    changed["outcomes"].clear()
    assert final.complete  # No mutable alias escapes the frozen model.


def test_supporting_reads_negative_probes_and_membership(tmp_path):
    root = tmp_path.resolve()
    (root / "main.ts").write_text("import {helper} from '@/helper'; helper();")
    (root / "helper.ts").write_text("export function helper() {}")
    (root / "tsconfig.json").write_text('{"compilerOptions":{"baseUrl":".","paths":{"@/*":["*"]}}}')
    paths = [root / "main.ts", root / "helper.ts"]
    with SourceIO(root) as inputs:
        inputs.listdir(root)
        result = extract(paths, source_io=inputs, quiet=True)
        manifest = InputManifest.from_engine(inputs, phase="consumed", code_inputs=paths,
                                             outcomes=result["outcomes"])
    evidence = manifest.to_dict()["evidence"]
    assert any(e["operation"] == "read" and e["path"] == "tsconfig.json" for e in evidence)
    assert any(e["operation"] == "probe" and e["value"] is None for e in evidence)
    assert any(e["operation"] == "list" for e in evidence)


def test_real_detection_with_explicit_git_policy_roots(tmp_path):
    from graphify.detect import detect
    root = tmp_path.resolve() / "source"
    root.mkdir()
    (root / "a.py").write_text("def one(): pass")
    (root / "notes.md").write_text("non-code evidence")
    git, policy = tmp_path.resolve() / "git", tmp_path.resolve() / "policy"
    git.mkdir(); policy.mkdir()
    (policy / "policy.json").write_text("{}")
    with SourceIO(root, extra_roots={"git": git, "policy": policy}) as inputs:
        inputs.read_bytes(policy / "policy.json")
        inputs.probe(git / "HEAD")
        result = detect(root, source_io=inputs, read_only=True, quiet=True, ambient_output=False)
        initial = InputManifest.from_engine(inputs, phase="detection", code_inputs=result["files"]["code"])
    records = {(e["operation"], e["path"]) for e in initial.to_dict()["evidence"]}
    assert ("read", "notes.md") in records
    assert ("probe", "git:HEAD") in records
    assert ("read", "policy:policy.json") in records


@pytest.mark.parametrize("resolver_failure", [False, True])
def test_failed_batch_cannot_be_relabelled_empty(tmp_path, monkeypatch, resolver_failure):
    import importlib
    engine = importlib.import_module("graphify.extract")
    root = tmp_path.resolve()
    paths = [root / "good.py", root / "bad.py", root / "later.py"]
    for p in paths:
        p.write_text("def okay(): pass")
    with SourceIO(root) as inputs:
        if resolver_failure:
            def fail(*args, **kwargs):
                raise RuntimeError("resolver failed after successful per-file extraction")
            monkeypatch.setattr(engine, "run_language_resolvers", fail)
        else:
            original = inputs.read_bytes
            def fail(path, **kwargs):
                if Path(path).name == "bad.py":
                    inputs.refuse("fixture failure")
                return original(path, **kwargs)
            monkeypatch.setattr(inputs, "read_bytes", fail)
        with pytest.raises(ExtractionIncomplete) as caught:
            extract(paths, source_io=inputs, quiet=True)
        final = InputManifest.from_engine(inputs, phase="consumed", code_inputs=paths,
                                          outcomes=caught.value.outcomes, failure=caught.value.failure)
    assert not final.complete
    assert final.to_dict()["failure"] is not None
    assert len(final.to_dict()["outcomes"]) == 3


@pytest.mark.parametrize("field,value", [
    ("state_schema_version", 1), ("adapter_contract_version", True),
    ("distribution_version", "0.10.1"), ("distribution_build", "git:" + "0" * 40),
    ("detector_id", "old"), ("certified", True), ("candidate_kind", "release"),
])
def test_unknown_tuple_refuses(field, value):
    document = compatibility().to_dict()
    document[field] = value
    with pytest.raises(ContractError):
        CompatibilityManifest.from_mapping(document)


@pytest.mark.parametrize("mutation", ["omitted", "duplicate", "escape", "unknown-root", "absolute", "missing-read", "extra", "old-format", "bad-probe", "bool-identity", "changed-list", "nfc"])
def test_malformed_input_manifest_refuses(tmp_path, mutation):
    _, final = manifests(tmp_path)
    data = final.to_dict()
    if mutation == "omitted": data["outcomes"] = []
    elif mutation == "duplicate": data["evidence"].append(data["evidence"][0])
    elif mutation in {"escape", "unknown-root", "absolute", "nfc"}:
        data["evidence"][0]["path"] = {"escape":"../oops", "unknown-root":"scratch:.", "absolute":"/tmp/input", "nfc":"e\u0301"}[mutation]
    elif mutation == "missing-read": data["evidence"] = [r for r in data["evidence"] if r["operation"] != "read"]
    elif mutation == "extra": data["surprise"] = True
    elif mutation == "old-format": data["format_version"] = 0
    elif mutation == "bad-probe": next(r for r in data["evidence"] if r["operation"] == "probe")["value"] = 4
    elif mutation == "bool-identity": data["evidence"][0]["value"][0] = True
    elif mutation == "changed-list":
        next(r for r in data["evidence"] if r["operation"] == "list")["value"][1][0][1][1] += 1
    with pytest.raises(ContractError): InputManifest.from_mapping(data)


def test_aggregate_bounds_and_missing_initial_evidence(tmp_path, monkeypatch):
    import graphify.workspace.contracts as contracts
    initial, final = manifests(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(contracts, "MAX_ENTRIES", 2)
        with pytest.raises(ContractError): InputManifest.from_mapping(final.to_dict())
    changed = final.to_dict()
    changed["evidence"] = [r for r in changed["evidence"] if r["path"] != "optional.json"]
    with pytest.raises(ContractError, match="initial evidence"):
        CompletionBinding.bind(initial, InputManifest.from_mapping(changed),
                               compatibility=compatibility(), graph_sha256="a" * 64)


@pytest.mark.parametrize("payload", [b'{"x":1,"x":1}\n', b'{"x":NaN}\n', b'{"x":1}', b'{ "x":1}\n', b'[]' * 100])
def test_noncanonical_and_duplicate_json_refused(payload):
    with pytest.raises(ContractError): decode_canonical(payload)


def test_canonicalization_bounds_and_normalized_collision():
    assert canonical_json_bytes({"z": "e\u0301", "a": 1}) == b'{"a":1,"z":"\xc3\xa9"}\n'
    with pytest.raises(ContractError): canonical_json_bytes({"é": 1, "e\u0301": 2})
    with pytest.raises(ContractError): canonical_json_bytes({"float": 1.0})
    with pytest.raises(ContractError): decode_canonical(b'{}\n', max_bytes=2)
    value = []
    for _ in range(34): value = [value]
    with pytest.raises(ContractError): canonical_json_bytes(value)


def test_schemas_positive_and_negative_wire_fixtures(tmp_path):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from graphify.workspace.composition import WorkspaceRuntimeAuthority, StructuralPolicy
    schemas = [json.loads((REPO / "graphify/workspace/schemas" / n).read_text()) for n in SCHEMA_FILES]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    initial, final = manifests(tmp_path)
    comp = compatibility()
    authority = WorkspaceRuntimeAuthority.from_mapping({
        "contract": "graphify.workspace.runtime_authority.internal", "format_version": 2,
        "compatibility_manifest": comp.to_dict(), "structural_policy": StructuralPolicy(8, 16384, 1, 4, 1048576).to_dict(),
    })
    marker = StateRootMarker.from_mapping({"contract": "graphify.workspace.state-root", "state_schema_version": 2, "owner": "graphify.workspace"})
    binding = CompletionBinding.bind(initial, final, compatibility=comp, graph_sha256="b" * 64)
    documents = [comp, final, binding, authority, marker]
    for schema, document in zip(schemas, documents):
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=registry)
        validator.validate(document.to_dict())
        broken = document.to_dict(); broken["unexpected"] = True
        assert list(validator.iter_errors(broken))
        with pytest.raises(ContractError): type(document).from_mapping(broken)
    assert len(documents) == len(SCHEMA_FILES)
