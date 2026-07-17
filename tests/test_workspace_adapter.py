from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from graphify.detect import detect
from graphify.workspace.adapters import (
    AdapterIntent,
    CompatibilityLane,
    CompatibilityTuple,
    RetainedStateInvalid,
    SUPPORTED_COMPATIBILITY,
    UnsupportedCompatibility,
    select_adapter,
)


FIXTURES = Path(__file__).parent / "fixtures" / "workspace" / "v1"


def _tree_bytes(root: Path) -> dict[str, tuple[int, int, str | None]]:
    result: dict[str, tuple[int, int, str | None]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        details = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        result[relative] = (stat.S_IFMT(details.st_mode), details.st_size, digest)
    return result


def test_supported_tuple_selects_the_only_versioned_adapter() -> None:
    selection = select_adapter(SUPPORTED_COMPATIBILITY, intent=AdapterIntent.EXECUTE)

    assert selection.lane is CompatibilityLane.SUPPORTED
    assert selection.promotable is True
    assert selection.adapter is not None
    assert selection.adapter.adapter_id == "graphify-0.9.16/workspace-adapter-v1"
    assert selection.adapter.engine_baseline == "0.9.16"


def test_exact_adapter_rejects_a_different_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "graphify.workspace.adapters.v0_9_16.distribution_version",
        lambda _name: "0.9.17+workspace.1",
    )

    with pytest.raises(UnsupportedCompatibility, match="requires distribution"):
        select_adapter(SUPPORTED_COMPATIBILITY, intent=AdapterIntent.EXECUTE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("distribution", "other"),
        ("distribution_version", "0.9.16"),
        ("engine_baseline", "0.9.12"),
        ("extractor_cache_abi", "graphify-0.9.12"),
        ("adapter_contract_version", 2),
        ("state_schema_version", 2),
    ],
)
@pytest.mark.parametrize("intent", [AdapterIntent.STAGE, AdapterIntent.PROMOTE])
def test_mixed_or_unknown_tuple_fails_before_staging_or_promotion(
    field: str,
    value: str | int,
    intent: AdapterIntent,
) -> None:
    candidate = replace(SUPPORTED_COMPATIBILITY, **{field: value})

    with pytest.raises(UnsupportedCompatibility, match="unsupported compatibility tuple"):
        select_adapter(candidate, intent=intent)


def test_future_whole_artifact_tuple_is_non_promoting_probe_only() -> None:
    future = CompatibilityTuple(
        distribution="graphifyy",
        distribution_version="0.9.17+workspace.1",
        engine_baseline="0.9.17",
        extractor_cache_abi="graphify-0.9.17",
        adapter_contract_version=1,
        state_schema_version=1,
    )

    selection = select_adapter(future, intent=AdapterIntent.PROBE)

    assert selection.lane is CompatibilityLane.NON_PROMOTING
    assert selection.promotable is False
    assert selection.adapter is None
    with pytest.raises(UnsupportedCompatibility, match="non-promoting"):
        select_adapter(future, intent=AdapterIntent.PROMOTE)


def test_retained_0912_manifest_cache_and_artifacts_import_without_mutation() -> None:
    retained = FIXTURES / "legacy" / "graphify-0.9.12"
    before = _tree_bytes(retained)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.IMPORT,
    ).require_adapter()

    imported = adapter.read_retained_state(retained, source_version="0.9.12")

    assert imported.source_version == "0.9.12"
    graph = json.loads((retained / "graphify-out/graph.json").read_bytes())
    assert "links" in graph
    assert "edges" not in graph
    assert imported.manifest_entries[0].path == "/legacy/repo/README.md"
    assert imported.manifest_entries[0].ast_hash == "22222222222222222222222222222222"
    assert imported.manifest_entries[1].path == "/legacy/repo/app.py"
    assert imported.manifest_entries[1].ast_hash == ""
    assert imported.manifest_entries[2].path == "/legacy/repo/docs/guide.md"
    assert imported.manifest_entries[2].semantic_hash == "33333333333333333333333333333333"
    assert imported.cache_entries == (
        "graphify-out/cache/ast/v0.9.12/"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        "graphify-out/cache/semantic/"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
    )
    assert "graphify-out/graph.json" in imported.artifact_entries
    assert _tree_bytes(retained) == before


def test_retained_0912_rejects_nonexport_graph_shape(tmp_path: Path) -> None:
    retained = tmp_path / "graphify-0.9.12"
    shutil.copytree(FIXTURES / "legacy" / "graphify-0.9.12", retained)
    graph_path = retained / "graphify-out/graph.json"
    graph = json.loads(graph_path.read_bytes())
    graph["edges"] = graph.pop("links")
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.IMPORT,
    ).require_adapter()

    with pytest.raises(RetainedStateInvalid, match="unsupported shape"):
        adapter.read_retained_state(retained, source_version="0.9.12")


def test_retained_0912_requires_published_report_shape(tmp_path: Path) -> None:
    retained = tmp_path / "graphify-0.9.12"
    shutil.copytree(FIXTURES / "legacy" / "graphify-0.9.12", retained)
    (retained / "graphify-out/GRAPH_REPORT.md").write_text(
        "# synthetic report\n",
        encoding="utf-8",
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.IMPORT,
    ).require_adapter()

    with pytest.raises(RetainedStateInvalid, match="report has an unsupported shape"):
        adapter.read_retained_state(retained, source_version="0.9.12")


def test_retained_import_rejects_other_versions_without_touching_fixture() -> None:
    retained = FIXTURES / "legacy" / "graphify-0.9.12"
    before = _tree_bytes(retained)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.IMPORT,
    ).require_adapter()

    with pytest.raises(UnsupportedCompatibility, match="retained state version"):
        adapter.read_retained_state(retained, source_version="0.9.11")

    assert _tree_bytes(retained) == before


def test_0916_structural_build_redirects_engine_outputs_outside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (source / "notes.md").write_text("dispatched later\n", encoding="utf-8")
    (source / "book.xlsx").write_bytes(b"synthetic office fixture")
    before = _tree_bytes(source)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    result = adapter.build_structural(source, output_root=output)

    assert result.engine_baseline == "0.9.16"
    assert result.node_count >= 1
    assert result.edge_count >= 0
    assert result.detected_code_files == ("app.py",)
    assert result.omitted_dispatched_files == ("book.xlsx", "notes.md")
    assert (output / "graphify-out" / "cache").is_dir()
    assert not (source / "graphify-out").exists()
    assert _tree_bytes(source) == before


def test_detection_redirects_conversion_sidecars_to_explicit_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    office = source / "book.xlsx"
    office.write_bytes(b"synthetic office fixture")
    before = _tree_bytes(source)

    def convert(path: Path, converted_dir: Path) -> Path:
        assert path == office
        target = converted_dir / "book.md"
        target.parent.mkdir(parents=True)
        target.write_text("converted outside source\n", encoding="utf-8")
        return target

    monkeypatch.setattr("graphify.detect.convert_office_file", convert)
    monkeypatch.setattr("graphify.cache.cached_word_count", lambda *_args, **_kwargs: 0)

    detected = detect(source, cache_root=output)

    sidecar = output / "graphify-out" / "converted" / "book.md"
    assert detected["files"]["document"] == [str(sidecar)]
    assert sidecar.is_file()
    assert not (source / "graphify-out").exists()
    assert _tree_bytes(source) == before


def test_read_only_detection_suppresses_stat_cache_and_office_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("read only detection\n", encoding="utf-8")
    (source / "book.xlsx").write_bytes(b"synthetic office fixture")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Graphify Test",
            "-c",
            "user.email=test@example.invalid",
            "add",
            ".",
        ],
        cwd=source,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Graphify Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=source,
        check=True,
    )
    before = _tree_bytes(source)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("read-only detection attempted a persistent helper")

    monkeypatch.setattr("graphify.cache.cached_word_count", forbidden)
    monkeypatch.setattr("graphify.detect.convert_office_file", forbidden)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    observation = adapter.observe(source)

    assert observation.stable_inventory_passes == 2
    assert {entry.path for entry in observation.entries} == {"book.xlsx", "notes.md"}
    assert not (source / "graphify-out").exists()
    assert _tree_bytes(source) == before


def test_engine_private_imports_are_confined_to_versioned_adapter_package() -> None:
    workspace = Path(__file__).parents[1] / "graphify" / "workspace"
    private_roots = {
        "graphify.build",
        "graphify.cache",
        "graphify.detect",
        "graphify.export",
        "graphify.extract",
        "graphify.security",
        "graphify.serve",
        "graphify.watch",
    }
    violations: list[str] = []
    for path in sorted(workspace.rglob("*.py")):
        relative = path.relative_to(workspace)
        if relative.parts[:1] == ("adapters",):
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            normalized = line.strip()
            if any(
                normalized.startswith(f"from {module} import")
                or normalized == f"import {module}"
                for module in private_roots
            ):
                violations.append(f"{relative}:{line_number}:{normalized}")

    assert violations == []


def test_compatibility_tuple_serialization_is_stable() -> None:
    encoded = SUPPORTED_COMPATIBILITY.canonical

    assert json.loads(encoded) == {
        "adapter_contract_version": 1,
        "distribution": "graphifyy",
        "distribution_version": "0.9.16+workspace.1",
        "engine_baseline": "0.9.16",
        "extractor_cache_abi": "graphify-0.9.16",
        "state_schema_version": 1,
    }
    assert encoded.endswith(b"\n")
