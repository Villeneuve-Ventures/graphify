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
    ObservationUnavailable,
    ObservationUnstable,
    ObservationUnsupported,
    QueryRejected,
    QueryRequest,
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


def _init_git_repo(root: Path, *tracked: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", *(tracked or (".",))], cwd=root, check=True)
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
        cwd=root,
        check=True,
    )


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
    graph_path = output / "graphify-out" / "graph.json"
    graph = json.loads(graph_path.read_bytes())
    assert len(graph["nodes"]) == result.node_count
    assert len(graph["links"]) == result.edge_count
    assert adapter.query_structural(
        output / "graphify-out",
        QueryRequest(question="answer"),
    )
    assert not (source / "graphify-out").exists()
    assert _tree_bytes(source) == before


def test_0916_structural_build_keeps_xaml_resolution_anchored_to_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "staging"
    shutil.copytree(FIXTURES.parents[1] / "xaml_viewmodel", source)
    before = _tree_bytes(source)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    adapter.build_structural(source, output_root=output)

    graph = json.loads((output / "graphify-out" / "graph.json").read_bytes())
    nodes = {node["id"]: node for node in graph["nodes"]}
    view_model_edges = [
        edge
        for edge in graph["links"]
        if edge["relation"] == "references" and edge.get("context") == "view_model"
    ]
    assert any(
        nodes[edge["target"]]["label"] == "MainViewModel"
        and nodes[edge["target"]]["source_file"].endswith("MainViewModel.cs")
        for edge in view_model_edges
    )
    assert not (source / "graphify-out").exists()
    assert _tree_bytes(source) == before


def test_0916_structural_build_rejects_incomplete_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")

    def incomplete_detection(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "files": {
                "code": [str(source / "app.py")],
                "document": [],
                "paper": [],
                "image": [],
                "video": [],
            },
            "walk_errors": ["source/private: permission denied"],
        }

    def forbidden_extract(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("incomplete detection reached extraction")

    monkeypatch.setattr(
        "graphify.workspace.adapters.v0_9_16.detect",
        incomplete_detection,
    )
    monkeypatch.setattr(
        "graphify.workspace.adapters.v0_9_16.extract",
        forbidden_extract,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnavailable, match="enumeration was incomplete"):
        adapter.build_structural(source, output_root=output)

    assert not (output / "graphify-out" / "graph.json").exists()


def test_observer_rejects_in_place_change_after_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source.py"
    source.write_bytes(b"before\n")
    original_lstat = Path.lstat
    lstat_calls = 0

    def racing_lstat(path: Path) -> os.stat_result:
        nonlocal lstat_calls
        if path == source:
            lstat_calls += 1
            if lstat_calls == 3:
                source.write_bytes(b"changed after descriptor hashing\n")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", racing_lstat)

    with pytest.raises(ObservationUnstable, match="after hashing"):
        adapter_module._read_regular_once(source)


def test_observer_rejects_named_pipe_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable on this platform")

    import sys

    source = tmp_path / "blocked.py"
    os.mkfifo(source)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from graphify.workspace.adapters import ObservationUnsupported",
                    "from graphify.workspace.adapters.v0_9_16 import _read_regular_once",
                    "try:",
                    "    _read_regular_once(Path(sys.argv[1]))",
                    "except ObservationUnsupported:",
                    "    raise SystemExit(0)",
                    "raise SystemExit('named pipe was accepted')",
                )
            ),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("remove_no_follow", [False, True])
def test_observer_rejects_symlink_before_reading_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remove_no_follow: bool,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    target = tmp_path / "target.py"
    target.write_bytes(b"target bytes must not be read\n")
    source = tmp_path / "alias.py"
    try:
        source.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not support symlinks")
    if remove_no_follow:
        monkeypatch.delattr(adapter_module.os, "O_NOFOLLOW", raising=False)
    original_read = adapter_module.os.read
    reads = 0

    def tracked_read(descriptor: int, length: int) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(descriptor, length)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)

    with pytest.raises(ObservationUnsupported, match="singular regular file"):
        adapter_module._read_regular_once(source)

    assert reads == 0


def test_observer_rechecks_path_before_reading_when_no_follow_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source.py"
    target = tmp_path / "target.py"
    source.write_bytes(b"target bytes must not be read\n")
    symlink_probe = tmp_path / "symlink-probe"
    try:
        symlink_probe.symlink_to(source)
        symlink_probe.unlink()
    except (NotImplementedError, OSError):
        pytest.skip("filesystem does not support symlinks")
    original_open = adapter_module.os.open
    original_read = adapter_module.os.read
    reads = 0

    def swapping_open(path: Path, flags: int) -> int:
        source.rename(target)
        source.symlink_to(target)
        return original_open(path, flags)

    def tracked_read(descriptor: int, length: int) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(descriptor, length)

    monkeypatch.delattr(adapter_module.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(adapter_module.os, "open", swapping_open)
    monkeypatch.setattr(adapter_module.os, "read", tracked_read)

    with pytest.raises(ObservationUnstable, match="changed before hashing"):
        adapter_module._read_regular_once(source)

    assert reads == 0


@pytest.mark.parametrize("unsafe_name", ["script", "notes.md", ".gitignore"])
def test_read_only_observation_rejects_classifier_inputs_without_blocking(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable on this platform")

    import sys

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    os.mkfifo(source / unsafe_name)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from graphify.workspace.adapters import AdapterIntent, ObservationUnsupported, SUPPORTED_COMPATIBILITY, select_adapter",
                    "adapter = select_adapter(SUPPORTED_COMPATIBILITY, intent=AdapterIntent.QUERY).require_adapter()",
                    "try:",
                    "    adapter.observe(Path(sys.argv[1]))",
                    "except ObservationUnsupported:",
                    "    raise SystemExit(0)",
                    "raise SystemExit('unsafe classifier input was accepted')",
                )
            ),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("policy_kind", ["git_info_exclude", "ancestor_include"])
def test_observer_hashes_every_effective_policy_input(
    tmp_path: Path,
    policy_kind: str,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "source"
    repo.mkdir()
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    _init_git_repo(repo, "source/app.py")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()
    before = adapter.observe(source)
    if policy_kind == "git_info_exclude":
        policy = repo / ".git" / "info" / "exclude"
        policy.write_text(
            policy.read_text(encoding="utf-8")
            + "\n# policy changed without inventory drift\n",
            encoding="utf-8",
        )
    else:
        policy = repo / ".graphifyinclude"
        policy.write_text("# ancestor policy changed\n", encoding="utf-8")

    after = adapter.observe(source)

    assert after.source_commit == before.source_commit
    assert after.entries == before.entries
    assert after.inventory_sha256 == before.inventory_sha256
    assert after.policy_sha256 != before.policy_sha256


def test_0916_structural_query_rejects_malformed_graph(tmp_path: Path) -> None:
    payload = tmp_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text(
        '{"nodes": [], "links": null}\n',
        encoding="utf-8",
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(QueryRejected, match="graph payload cannot be queried"):
        adapter.query_structural(payload, QueryRequest(question="malformed graph"))


def test_0916_structural_query_preserves_native_directed_traversal(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "graphify-out"
    payload.mkdir()
    (payload / "graph.json").write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {},
                "nodes": [
                    {"id": "source", "label": "upstream"},
                    {"id": "target", "label": "target"},
                ],
                "links": [
                    {
                        "source": "source",
                        "target": "target",
                        "relation": "calls",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    result = adapter.query_structural(
        payload,
        QueryRequest(question="target", depth=1),
    )

    assert "NODE target" in result
    assert "upstream" not in result


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
    assert "comparison_policy_paths" not in detected
    assert sidecar.is_file()
    assert not (source / "graphify-out").exists()
    assert _tree_bytes(source) == before


def test_read_only_detection_requires_an_explicit_comparison_reader(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires a comparison reader"):
        detect(tmp_path, read_only=True)


def test_read_only_detection_suppresses_stat_cache_and_office_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("read only detection\n", encoding="utf-8")
    (source / "book.xlsx").write_bytes(b"synthetic office fixture")
    _init_git_repo(source)
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
