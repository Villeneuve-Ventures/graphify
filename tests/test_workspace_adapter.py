from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any

import pytest

from graphify.detect import detect
from graphify.workspace.adapters import (
    AdapterIntent,
    CompatibilityLane,
    CompatibilityTuple,
    ObservationTimeout,
    ObservationUnavailable,
    ObservationUnstable,
    ObservationUnsupported,
    QueryRejected,
    QueryRequest,
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


def _stable_metadata(path: Path) -> tuple[int, int, int, int]:
    details = path.lstat()
    return (
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


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


def test_adapter_surface_has_no_pre_workspace_state_import_lane() -> None:
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    assert "IMPORT" not in AdapterIntent.__members__
    assert not hasattr(adapter, "read_retained_state")


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
        ("engine_baseline", "0.9.15"),
        ("extractor_cache_abi", "graphify-0.9.15"),
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


@pytest.mark.parametrize(
    "question,depth,token_budget,context_filters,detail",
    [
        ("bounded", 9, 2_000, (), "depth must not exceed 8"),
        ("bounded", 2, 32_769, (), "token_budget must not exceed 32768"),
        ("x" * 4_097, 2, 2_000, (), "question must not exceed 4096 UTF-8 bytes"),
        ("x" * 257, 2, 2_000, (), "question must not exceed 256 non-space term units"),
        ("bounded", 2, 2_000, ("call",) * 17, "context filters must not exceed 16"),
        (
            "bounded",
            2,
            2_000,
            ("x" * 129,),
            "each context filter must not exceed 128 UTF-8 bytes",
        ),
        (
            "bounded",
            2,
            2_000,
            ("x" * 65,) * 16,
            "context filters must not exceed 1024 aggregate UTF-8 bytes",
        ),
    ],
)
def test_query_request_rejects_work_beyond_the_workspace_bound(
    question: str,
    depth: int,
    token_budget: int,
    context_filters: tuple[str, ...],
    detail: str,
) -> None:
    with pytest.raises(QueryRejected, match=detail):
        QueryRequest(
            question=question,
            depth=depth,
            token_budget=token_budget,
            context_filters=context_filters,
        )


def test_query_request_copies_context_filters_before_validation() -> None:
    filters: Any = ["call"]

    request = QueryRequest(question="bounded", context_filters=filters)
    filters.extend(["x" * 129] * 20)

    assert request.context_filters == ("call",)


@pytest.mark.parametrize(
    "context_filters",
    ["call", b"call", ["call", 7]],
)
def test_query_request_rejects_non_sequence_or_non_string_context_filters(
    context_filters: Any,
) -> None:
    with pytest.raises(QueryRejected, match="sequence of strings"):
        QueryRequest(question="bounded", context_filters=context_filters)


@pytest.mark.parametrize("question", [None, 123, b"bounded"])
def test_query_request_rejects_non_string_question(question: Any) -> None:
    with pytest.raises(QueryRejected, match="question must be a string"):
        QueryRequest(question=question)


@pytest.mark.parametrize("mode", [None, [], 1, b"bfs"])
def test_query_request_rejects_non_string_mode(mode: Any) -> None:
    with pytest.raises(QueryRejected, match="mode must be a string"):
        QueryRequest(question="bounded", mode=mode)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("depth", None),
        ("depth", "2"),
        ("depth", 2.0),
        ("depth", True),
        ("token_budget", None),
        ("token_budget", "2000"),
        ("token_budget", 2000.0),
        ("token_budget", True),
    ],
)
def test_query_request_rejects_non_integer_bounds(field: str, value: Any) -> None:
    with pytest.raises(QueryRejected, match=rf"{field} must be an integer"):
        QueryRequest(question="bounded", **{field: value})


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


def test_0916_structural_build_rejects_output_inside_source_checkout(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "src"
    output = repo / "staging"
    source.mkdir(parents=True)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(repo, "src/app.py")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="external to source checkout"):
        adapter.build_structural(source, output_root=output)

    assert not output.exists()


def test_0916_structural_build_rechecks_checkout_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    repo = tmp_path / "repo"
    source = repo / "src"
    output = repo / "staging"
    source.mkdir(parents=True)
    output.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_normalize = adapter_module._normalize_structural_output

    def normalize_then_create_checkout(engine_output: Path) -> None:
        original_normalize(engine_output)
        (repo / ".git").mkdir()

    monkeypatch.setattr(
        adapter_module,
        "_normalize_structural_output",
        normalize_then_create_checkout,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="policy root changed"):
        adapter.build_structural(source, output_root=output)

    assert list(output.iterdir()) == []


def test_0916_structural_build_rechecks_source_identity_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    parked = tmp_path / "parked-source"
    output = tmp_path / "staging"
    source.mkdir()
    output.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    original_normalize = adapter_module._normalize_structural_output

    def normalize_then_replace_source(engine_output: Path) -> None:
        original_normalize(engine_output)
        source.rename(parked)
        source.mkdir()
        (source / ".git").mkdir()
        (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(
        adapter_module,
        "_normalize_structural_output",
        normalize_then_replace_source,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source directory changed"):
        adapter.build_structural(source, output_root=output)

    assert list(output.iterdir()) == []


def test_0916_structural_build_revalidates_output_contents_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    output.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_publish = adapter_module._publish_structural_output

    def publish_after_intruder(engine_output: Path, destination_descriptor: int) -> None:
        (output / "intruder").write_text("unexpected\n", encoding="utf-8")
        original_publish(engine_output, destination_descriptor)

    monkeypatch.setattr(
        adapter_module,
        "_publish_structural_output",
        publish_after_intruder,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="output root contents changed"):
        adapter.build_structural(source, output_root=output)


def test_0916_structural_build_normalizes_snapshot_write_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_observe = adapter_module._PinnedSourceReader.observe

    def fail_snapshot_write(
        reader: Any,
        path: Path,
        **kwargs: Any,
    ) -> Any:
        if kwargs.get("chunk_consumer") is not None:
            raise OSError(errno.ENOSPC, "synthetic snapshot filesystem full")
        return original_observe(reader, path, **kwargs)

    monkeypatch.setattr(
        adapter_module._PinnedSourceReader,
        "observe",
        fail_snapshot_write,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnavailable, match="snapshot could not be staged"):
        adapter.build_structural(source, output_root=output)


def test_0916_structural_build_normalizes_payload_modes_under_group_umask(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()
    previous_umask = os.umask(0o002)
    try:
        adapter.build_structural(source, output_root=output)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for path in sorted(output.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_dir():
            assert mode == 0o755, path
        else:
            assert path.is_file(), path
            assert mode == 0o644, path


def test_0916_structural_build_rejects_nonempty_output_before_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "staging"
    external = tmp_path / "external"
    source.mkdir()
    output.mkdir()
    external.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (output / "graphify-out").symlink_to(external, target_is_directory=True)
    before = _tree_bytes(external)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="output root must be empty"):
        adapter.build_structural(source, output_root=output)

    assert _tree_bytes(external) == before
    assert not (external / "graph.json").exists()


def test_0916_structural_build_rejects_symlinked_output_ancestor_without_external_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    external = tmp_path / "external"
    alias = tmp_path / "alias"
    source.mkdir()
    external.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (external / "sentinel").write_text("unchanged\n", encoding="utf-8")
    alias.symlink_to(external, target_is_directory=True)
    before_tree = _tree_bytes(external)
    before_metadata = _stable_metadata(external)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.build_structural(source, output_root=alias / "staging")

    assert _tree_bytes(external) == before_tree
    assert _stable_metadata(external) == before_metadata
    assert not (external / "staging").exists()


def test_0916_structural_build_normalizes_output_second_open_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_open = adapter_module.os.open
    output_opens = 0

    def raced_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal output_opens
        if path == output.name and dir_fd is not None:
            output_opens += 1
            if output_opens == 2:
                raise PermissionError(errno.EACCES, "injected output-open race")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        adapter_module.os,
        "supports_dir_fd",
        {*adapter_module.os.supports_dir_fd, raced_open},
    )
    monkeypatch.setattr(adapter_module.os, "open", raced_open)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnavailable, match="cannot be accessed safely"):
        adapter.build_structural(source, output_root=output)

    assert output_opens == 2


def test_0916_structural_build_normalizes_output_binding_stat_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    output.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_stat = adapter_module.os.stat
    injected = False

    def raced_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal injected
        if path == output.name and dir_fd is not None and not follow_symlinks:
            injected = True
            raise FileNotFoundError(errno.ENOENT, "injected output-stat race")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(
        adapter_module.os,
        "supports_dir_fd",
        {*adapter_module.os.supports_dir_fd, raced_stat},
    )
    monkeypatch.setattr(
        adapter_module.os,
        "supports_follow_symlinks",
        {*adapter_module.os.supports_follow_symlinks, raced_stat},
    )
    monkeypatch.setattr(adapter_module.os, "stat", raced_stat)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="disappeared while opening"):
        adapter.build_structural(source, output_root=output)

    assert injected is True


def test_0916_structural_build_normalizes_output_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_publish = adapter_module._publish_structural_output
    original_write = adapter_module.os.write
    publishing = False

    def no_space(descriptor: int, payload: bytes) -> int:
        if publishing:
            raise OSError(errno.ENOSPC, "injected full staging filesystem")
        return original_write(descriptor, payload)

    def publish_without_space(engine_output: Path, output_descriptor: int) -> None:
        nonlocal publishing
        publishing = True
        try:
            original_publish(engine_output, output_descriptor)
        finally:
            publishing = False

    monkeypatch.setattr(adapter_module.os, "write", no_space)
    monkeypatch.setattr(
        adapter_module,
        "_publish_structural_output",
        publish_without_space,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnavailable, match="output file write failed safely"):
        adapter.build_structural(source, output_root=output)


def test_0916_structural_build_normalizes_output_mode_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_fchmod = adapter_module.os.fchmod

    def fail_file_mode(descriptor: int, mode: int) -> None:
        if mode == 0o644:
            raise OSError(errno.EIO, "injected mode-finalization failure")
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(adapter_module.os, "fchmod", fail_file_mode)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnavailable, match="file mode could not be finalized"):
        adapter.build_structural(source, output_root=output)


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


def test_0916_structural_build_rejects_unsafe_code_entry_without_blocking(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable on this platform")

    import sys

    source = tmp_path / "source"
    output = tmp_path / "staging"
    source.mkdir()
    os.mkfifo(source / "blocked.py")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from graphify.workspace.adapters import AdapterIntent, ObservationUnsupported, SUPPORTED_COMPATIBILITY, select_adapter",
                    "adapter = select_adapter(SUPPORTED_COMPATIBILITY, intent=AdapterIntent.EXECUTE).require_adapter()",
                    "try:",
                    "    adapter.build_structural(Path(sys.argv[1]), output_root=Path(sys.argv[2]))",
                    "except ObservationUnsupported:",
                    "    raise SystemExit(0)",
                    "raise SystemExit('unsafe code entry was accepted')",
                )
            ),
            str(source),
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert not (source / "graphify-out").exists()


def test_0916_structural_build_rejects_ancestor_symlink_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    output = tmp_path / "staging"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "module.py").write_text("LOCAL = True\n", encoding="utf-8")
    external_file = external / "module.py"
    external_file.write_text("EXTERNAL_SECRET = True\n", encoding="utf-8")
    external_identity = external_file.stat()
    external_reads = 0
    swapped = False
    original_read = os.read
    original_relative_read = adapter_module._read_relative_regular_once

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def swap_then_read(
        root_descriptor: int,
        relative: Path,
        path: Path,
        *,
        collect: bool = False,
        max_bytes: int | None = None,
        size_cap: int | None = None,
        chunk_consumer: Callable[[bytes], object] | None = None,
        expected_path: adapter_module._PinnedRegularPath | None = None,
        deadline_ns: int | None = None,
    ) -> tuple[str, os.stat_result, bytes | None]:
        nonlocal swapped
        if not swapped:
            package.rename(parked)
            package.symlink_to(external, target_is_directory=True)
            swapped = True
        return original_relative_read(
            root_descriptor,
            relative,
            path,
            collect=collect,
            max_bytes=max_bytes,
            size_cap=size_cap,
            chunk_consumer=chunk_consumer,
            expected_path=expected_path,
            deadline_ns=deadline_ns,
        )

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(adapter_module, "_read_relative_regular_once", swap_then_read)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.build_structural(source, output_root=output)

    assert swapped is True
    assert external_reads == 0
    assert external_file.read_text(encoding="utf-8") == "EXTERNAL_SECRET = True\n"


def test_0916_structural_build_rejects_real_directory_replacement_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    replacement = tmp_path / "replacement"
    output = tmp_path / "staging"
    package.mkdir(parents=True)
    replacement.mkdir()
    (package / "module.py").write_text("LOCAL = True\n", encoding="utf-8")
    replacement_file = replacement / "module.py"
    replacement_file.write_text("REPLACEMENT_SECRET = True\n", encoding="utf-8")
    replacement_identity = replacement_file.stat()
    original_snapshot = adapter_module._snapshot_code_files
    original_read = adapter_module.os.read
    replacement_reads = 0
    swapped = False

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal replacement_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            replacement_identity.st_dev,
            replacement_identity.st_ino,
        ):
            replacement_reads += 1
        return original_read(descriptor, size)

    def replace_then_snapshot(
        code_files: tuple[str, ...],
        pinned_files: dict[Path, adapter_module._PinnedRegularPath],
        root: Path,
        snapshot_root: Path,
        reader: adapter_module._PinnedSourceReader,
        source_details: os.stat_result,
    ) -> tuple[Path, ...]:
        nonlocal swapped
        package.rename(parked)
        replacement.rename(package)
        swapped = True
        return original_snapshot(
            code_files,
            pinned_files,
            root,
            snapshot_root,
            reader,
            source_details,
        )

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(adapter_module, "_snapshot_code_files", replace_then_snapshot)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source directory changed"):
        adapter.build_structural(source, output_root=output)

    assert swapped is True
    assert replacement_reads == 0
    assert (package / "module.py").read_text(encoding="utf-8") == (
        "REPLACEMENT_SECRET = True\n"
    )
    assert not (output / "graphify-out").exists()


def test_0916_structural_build_pins_output_before_ancestor_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    output_parent = tmp_path / "output-parent"
    output = output_parent / "staging"
    parked = tmp_path / "parked-output-parent"
    external = tmp_path / "external"
    source.mkdir()
    output_parent.mkdir()
    external.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (external / "sentinel").write_text("unchanged\n", encoding="utf-8")
    before_tree = _tree_bytes(external)
    before_metadata = _stable_metadata(external)
    original_publish = adapter_module._publish_structural_output
    swapped = False

    def swap_then_publish(engine_output: Path, output_descriptor: int) -> None:
        nonlocal swapped
        output_parent.rename(parked)
        output_parent.symlink_to(external, target_is_directory=True)
        swapped = True
        original_publish(engine_output, output_descriptor)

    monkeypatch.setattr(
        adapter_module,
        "_publish_structural_output",
        swap_then_publish,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.build_structural(source, output_root=output)

    assert swapped is True
    assert (parked / "staging" / "graphify-out" / "graph.json").is_file()
    assert _tree_bytes(external) == before_tree
    assert _stable_metadata(external) == before_metadata
    assert not (external / "staging").exists()


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


def test_observer_checks_deadline_between_file_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    source.mkdir()
    large_file = source / "large.py"
    large_file.write_bytes(b"x" * (2 * 1024 * 1024))
    _init_git_repo(source, "large.py")
    large_identity = large_file.stat()
    original_read = adapter_module.os.read
    now = 0
    source_reads = 0

    def monotonic_ns() -> int:
        return now

    def advance_after_read(descriptor: int, size: int) -> bytes:
        nonlocal now, source_reads
        chunk = original_read(descriptor, size)
        details = os.fstat(descriptor)
        if chunk and (details.st_dev, details.st_ino) == (
            large_identity.st_dev,
            large_identity.st_ino,
        ):
            source_reads += 1
            now = 2
        return chunk

    monkeypatch.setattr(adapter_module.time, "monotonic_ns", monotonic_ns)
    monkeypatch.setattr(adapter_module.os, "read", advance_after_read)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationTimeout, match="exceeded its deadline"):
        adapter.observe(source, deadline_ns=1)

    assert source_reads == 1


def test_observer_normalizes_source_read_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    source.mkdir()
    app = source / "app.py"
    app.write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    app_identity = app.stat()
    original_read = adapter_module.os.read

    def failing_read(descriptor: int, size: int) -> bytes:
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            app_identity.st_dev,
            app_identity.st_ino,
        ):
            raise OSError(errno.EIO, "injected source read failure")
        return original_read(descriptor, size)

    monkeypatch.setattr(adapter_module.os, "read", failing_read)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnavailable, match="source file cannot be read safely"):
        adapter.observe(source)


def test_observer_normalizes_source_post_read_stat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    source_identity = source.stat()
    original_fstat = adapter_module.os.fstat
    source_fstat_calls = 0

    def failing_fstat(descriptor: int) -> os.stat_result:
        nonlocal source_fstat_calls
        details = original_fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        ):
            source_fstat_calls += 1
            if source_fstat_calls == 2:
                raise OSError(errno.EIO, "injected source stat failure")
        return details

    monkeypatch.setattr(adapter_module.os, "fstat", failing_fstat)

    with pytest.raises(
        ObservationUnavailable,
        match="source file cannot be inspected after hashing",
    ):
        adapter_module._read_regular_once(source)


def test_observer_normalizes_source_pre_read_stat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    source.mkdir()
    app = source / "app.py"
    app.write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    app_identity = app.stat()
    original_fstat = adapter_module.os.fstat
    injected = False

    def failing_fstat(descriptor: int) -> os.stat_result:
        nonlocal injected
        details = original_fstat(descriptor)
        if not injected and (details.st_dev, details.st_ino) == (
            app_identity.st_dev,
            app_identity.st_ino,
        ):
            injected = True
            raise OSError(errno.EIO, "injected source stat failure")
        return details

    monkeypatch.setattr(adapter_module.os, "fstat", failing_fstat)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(
        ObservationUnavailable,
        match="source file cannot be inspected before hashing",
    ):
        adapter.observe(source)

    assert injected is True


def test_observer_normalizes_source_root_stat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    source_identity = source.stat()
    original_fstat = adapter_module.os.fstat
    source_fstat_calls = 0

    def failing_fstat(descriptor: int) -> os.stat_result:
        nonlocal source_fstat_calls
        details = original_fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        ):
            source_fstat_calls += 1
            if source_fstat_calls == 2:
                raise OSError(errno.EIO, "injected source-root stat failure")
        return details

    monkeypatch.setattr(adapter_module.os, "fstat", failing_fstat)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(
        ObservationUnavailable,
        match="source directory cannot be inspected while opening",
    ):
        adapter.observe(source)


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


@pytest.mark.parametrize("unsafe_kind", ["fifo", "symlink"])
def test_read_only_observation_skips_ignored_unsafe_inputs(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    if unsafe_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable on this platform")

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    (source / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _init_git_repo(source, "app.py", ".gitignore")
    ignored = source / "ignored.py"
    if unsafe_kind == "fifo":
        os.mkfifo(ignored)
    else:
        external = tmp_path / "external.py"
        external.write_text("external_secret = 1\n", encoding="utf-8")
        ignored.symlink_to(external)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    observation = adapter.observe(source)

    assert {entry.path for entry in observation.entries} == {"app.py"}


@pytest.mark.parametrize(
    "policy_name",
    [".gitignore", ".graphifyignore", ".graphifyinclude"],
)
def test_read_only_policy_probe_pins_before_path_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_name: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    external = tmp_path / "external-policy"
    external.write_text("*.py\n", encoding="utf-8")
    policy = source / policy_name
    policy.symlink_to(external)
    original_exists = Path.exists
    policy_exists_calls = 0

    def forbid_policy_exists(path: Path) -> bool:
        nonlocal policy_exists_calls
        if path == policy:
            policy_exists_calls += 1
            raise AssertionError("read-only policy probe followed Path.exists")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", forbid_policy_exists)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="singular regular file"):
        adapter.observe(source)

    assert policy_exists_calls == 0


def test_descriptor_directory_enumeration_checks_deadline_between_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "one.py").write_text("one = 1\n", encoding="utf-8")
    (source / "two.py").write_text("two = 2\n", encoding="utf-8")
    reader = adapter_module._PinnedSourceReader.open(source)
    reader.deadline_ns = 10
    ticks = iter((0, 10))
    monkeypatch.setattr(
        adapter_module.time,
        "monotonic_ns",
        lambda: next(ticks),
    )

    try:
        with pytest.raises(ObservationTimeout):
            reader.list_directory(
                source,
                expected=None,
                expected_identities=None,
            )
    finally:
        reader.close()


def test_observer_rejects_detected_ancestor_swaps_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "module.py").write_text("LOCAL = True\n", encoding="utf-8")
    external_file = external / "module.py"
    external_file.write_text("EXTERNAL_SECRET = True\n", encoding="utf-8")
    _init_git_repo(source, "pkg/module.py")
    external_identity = external_file.stat()
    original_read = adapter_module.os.read
    external_reads = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def toggle_after_detection(event: str, _details: object) -> None:
        if event != "inventory_detected":
            return
        if package.is_symlink():
            package.unlink()
            parked.rename(package)
        else:
            package.rename(parked)
            package.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source directory changed"):
        adapter.observe(
            source,
            max_inventory_passes=4,
            hook=toggle_after_detection,
        )

    if package.is_symlink():
        package.unlink()
        parked.rename(package)
    assert package.is_dir() and not package.is_symlink()
    assert external_reads == 0
    assert external_file.read_text(encoding="utf-8") == "EXTERNAL_SECRET = True\n"


def test_observer_rejects_classifier_ancestor_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.detect as detect_module
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    script = package / "script"
    script.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    external_file = external / "script"
    external_file.write_text("#!/bin/sh\necho external-secret\n", encoding="utf-8")
    _init_git_repo(source, "pkg/script")
    external_identity = external_file.stat()
    original_classify = detect_module.classify_file
    original_read = adapter_module.os.read
    external_reads = 0
    swapped = False

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def swap_then_classify(
        path: Path,
        *,
        comparison_reader: Callable[[Path, int | None], bytes] | None = None,
    ) -> object:
        nonlocal swapped
        if path == script and not swapped:
            package.rename(parked)
            package.symlink_to(external, target_is_directory=True)
            swapped = True
        return original_classify(path, comparison_reader=comparison_reader)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(detect_module, "classify_file", swap_then_classify)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.observe(source)

    assert swapped is True
    assert external_reads == 0
    assert external_file.read_text(encoding="utf-8") == (
        "#!/bin/sh\necho external-secret\n"
    )


def test_observer_rejects_classifier_real_directory_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.detect as detect_module
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    script = package / "script"
    script.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    external_file = external / "script"
    external_file.write_text("#!/bin/sh\necho external-secret\n", encoding="utf-8")
    _init_git_repo(source, "pkg/script")
    external_identity = external_file.stat()
    original_classify = detect_module.classify_file
    original_read = adapter_module.os.read
    external_reads = 0
    swaps = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def swap_then_classify(
        path: Path,
        *,
        comparison_reader: Callable[[Path, int | None], bytes] | None = None,
    ) -> object:
        nonlocal swaps
        if path == script:
            if swaps % 2 == 0:
                package.rename(parked)
                external.rename(package)
            else:
                package.rename(external)
                parked.rename(package)
            swaps += 1
        return original_classify(path, comparison_reader=comparison_reader)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(detect_module, "classify_file", swap_then_classify)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source ancestor changed before open"):
        adapter.observe(source, max_inventory_passes=2)

    if package.exists() and swaps % 2 == 1:
        package.rename(external)
        parked.rename(package)
    assert package.is_dir() and not package.is_symlink()
    assert external_reads == 0
    assert external_file.read_text(encoding="utf-8") == (
        "#!/bin/sh\necho external-secret\n"
    )


def test_observer_rejects_directory_swap_between_enumeration_and_file_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.detect as detect_module
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "container" / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    script = package / "script"
    script.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    external_file = external / "script"
    external_file.write_text("#!/bin/sh\necho external-secret\n", encoding="utf-8")
    _init_git_repo(source, "container/pkg/script")
    external_identity = external_file.stat()
    original_walk = detect_module._comparison_walk
    original_read = adapter_module.os.read
    external_reads = 0
    swaps = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def swap_after_enumeration(*args: Any, **kwargs: Any) -> Any:
        nonlocal swaps
        for row in original_walk(*args, **kwargs):
            if Path(row[0]) == package:
                if swaps % 2 == 0:
                    package.rename(parked)
                    external.rename(package)
                else:
                    package.rename(external)
                    parked.rename(package)
                swaps += 1
            yield row

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(detect_module, "_comparison_walk", swap_after_enumeration)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source directory changed"):
        adapter.observe(source, max_inventory_passes=2)

    if package.exists() and swaps % 2 == 1:
        package.rename(external)
        parked.rename(package)
    assert package.is_dir() and not package.is_symlink()
    assert external_reads == 0
    assert external_file.read_text(encoding="utf-8") == (
        "#!/bin/sh\necho external-secret\n"
    )


def test_observer_rejects_persistent_swap_before_child_listing_without_external_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "container" / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "script").write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    external_file = external / "script"
    external_file.write_text("#!/bin/sh\necho external-secret\n", encoding="utf-8")
    _init_git_repo(source, "container/pkg/script")
    external_identity = external.stat()
    original_lister = adapter_module._PinnedReadAuthority.list_comparison_directory
    original_scandir = adapter_module.os.scandir
    external_scans = 0
    swapped = False

    def tracked_scandir(path: Any) -> Any:
        nonlocal external_scans
        if isinstance(path, int):
            details = os.fstat(path)
            if (details.st_dev, details.st_ino) == (
                external_identity.st_dev,
                external_identity.st_ino,
            ):
                external_scans += 1
        return original_scandir(path)

    def swap_before_child_listing(self: Any, path: Path) -> tuple[list[str], list[str]]:
        nonlocal swapped
        if path == package and not swapped:
            package.rename(parked)
            external.rename(package)
            swapped = True
        return original_lister(self, path)

    monkeypatch.setattr(adapter_module.os, "scandir", tracked_scandir)
    monkeypatch.setattr(
        adapter_module._PinnedReadAuthority,
        "list_comparison_directory",
        swap_before_child_listing,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source ancestor changed before open"):
        adapter.observe(source, max_inventory_passes=4)

    package.rename(external)
    parked.rename(package)
    assert swapped is True
    assert external_scans == 0
    assert external_file.read_text(encoding="utf-8") == (
        "#!/bin/sh\necho external-secret\n"
    )


def test_observer_does_not_promote_replacement_after_pinned_child_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "container" / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "script").write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    external_file = external / "script"
    external_file.write_text("#!/bin/sh\necho external-secret\n", encoding="utf-8")
    _init_git_repo(source, "container/pkg/script")
    external_directory_identity = external.stat()
    external_file_identity = external_file.stat()
    original_lister = adapter_module._PinnedReadAuthority.list_comparison_directory
    original_scandir = adapter_module.os.scandir
    original_read = adapter_module.os.read
    external_scans = 0
    external_reads = 0
    attempts = 0

    def tracked_scandir(path: Any) -> Any:
        nonlocal external_scans
        if isinstance(path, int):
            details = os.fstat(path)
            if (details.st_dev, details.st_ino) == (
                external_directory_identity.st_dev,
                external_directory_identity.st_ino,
            ):
                external_scans += 1
        return original_scandir(path)

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_file_identity.st_dev,
            external_file_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def disappear_then_install(
        self: Any,
        path: Path,
    ) -> tuple[list[str], list[str]]:
        nonlocal attempts
        if path != package:
            return original_lister(self, path)
        attempts += 1
        if attempts == 1:
            package.rename(parked)
            try:
                return original_lister(self, path)
            except ObservationUnstable:
                external.rename(package)
                raise
        return original_lister(self, path)

    monkeypatch.setattr(adapter_module.os, "scandir", tracked_scandir)
    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(
        adapter_module._PinnedReadAuthority,
        "list_comparison_directory",
        disappear_then_install,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="ancestor disappeared before open"):
        adapter.observe(source, max_inventory_passes=4)

    package.rename(external)
    parked.rename(package)
    assert attempts == 1
    assert external_scans == 0
    assert external_reads == 0
    assert external_file.read_text(encoding="utf-8") == (
        "#!/bin/sh\necho external-secret\n"
    )


def test_observer_keeps_listing_bound_when_path_swaps_before_scandir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "script").write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    external_file = external / "script"
    external_file.write_text("#!/bin/sh\necho external-secret\n", encoding="utf-8")
    _init_git_repo(source, "pkg/script")
    package_identity = package.stat()
    external_identity = external.stat()
    original_scandir = adapter_module.os.scandir
    external_scans = 0
    swapped = False

    def swap_before_scandir(path: Any) -> Any:
        nonlocal external_scans, swapped
        if isinstance(path, int):
            details = os.fstat(path)
            identity = (details.st_dev, details.st_ino)
            if identity == (external_identity.st_dev, external_identity.st_ino):
                external_scans += 1
            if (
                identity == (package_identity.st_dev, package_identity.st_ino)
                and not swapped
            ):
                package.rename(parked)
                external.rename(package)
                swapped = True
        return original_scandir(path)

    monkeypatch.setattr(adapter_module.os, "scandir", swap_before_scandir)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source directory changed"):
        adapter.observe(source, max_inventory_passes=4)

    package.rename(external)
    parked.rename(package)
    assert swapped is True
    assert external_scans == 0
    assert external_file.read_text(encoding="utf-8") == (
        "#!/bin/sh\necho external-secret\n"
    )


def test_observer_rejects_policy_ancestor_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.detect as detect_module
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "app.py").write_text("LOCAL = True\n", encoding="utf-8")
    policy = package / ".gitignore"
    policy.write_text("local-only\n", encoding="utf-8")
    external_policy = external / ".gitignore"
    external_policy.write_text("external-secret\n", encoding="utf-8")
    _init_git_repo(source, "pkg/app.py", "pkg/.gitignore")
    external_identity = external_policy.stat()
    original_detector_read = detect_module._read_detector_text
    original_read = adapter_module.os.read
    external_reads = 0
    swapped = False

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def swap_then_read_policy(
        path: Path,
        comparison_reader: Callable[[Path, int | None], bytes] | None,
        *,
        max_bytes: int | None = None,
    ) -> str:
        nonlocal swapped
        if path == policy and not swapped:
            package.rename(parked)
            package.symlink_to(external, target_is_directory=True)
            swapped = True
        return original_detector_read(
            path,
            comparison_reader,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(detect_module, "_read_detector_text", swap_then_read_policy)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.observe(source)

    assert swapped is True
    assert external_reads == 0
    assert external_policy.read_text(encoding="utf-8") == "external-secret\n"


def test_observer_rejects_policy_real_directory_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.detect as detect_module
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    package = source / "pkg"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    package.mkdir(parents=True)
    external.mkdir()
    (package / "app.py").write_text("LOCAL = True\n", encoding="utf-8")
    policy = package / ".gitignore"
    policy.write_text("local-only\n", encoding="utf-8")
    external_policy = external / ".gitignore"
    external_policy.write_text("external-secret\n", encoding="utf-8")
    (external / "app.py").write_text("EXTERNAL = True\n", encoding="utf-8")
    _init_git_repo(source, "pkg/app.py", "pkg/.gitignore")
    external_identity = external_policy.stat()
    original_detector_read = detect_module._read_detector_text
    original_read = adapter_module.os.read
    external_reads = 0
    swaps = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def swap_then_read_policy(
        path: Path,
        comparison_reader: Callable[[Path, int | None], bytes] | None,
        *,
        max_bytes: int | None = None,
    ) -> str:
        nonlocal swaps
        if path == policy:
            if swaps % 2 == 0:
                package.rename(parked)
                external.rename(package)
            else:
                package.rename(external)
                parked.rename(package)
            swaps += 1
        return original_detector_read(
            path,
            comparison_reader,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(detect_module, "_read_detector_text", swap_then_read_policy)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnstable, match="source ancestor changed before open"):
        adapter.observe(source, max_inventory_passes=2)

    if package.exists() and swaps % 2 == 1:
        package.rename(external)
        parked.rename(package)
    assert package.is_dir() and not package.is_symlink()
    assert external_reads == 0
    assert external_policy.read_text(encoding="utf-8") == "external-secret\n"


def test_observer_supports_linked_worktree_policy_roots_without_unapproved_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(repo, "app.py")
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)],
        cwd=repo,
        check=True,
    )
    (linked / "ignored.py").write_text("IGNORED = True\n", encoding="utf-8")
    git_file = linked / ".git"
    git_dir = Path(
        git_file.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
    )
    commondir_file = git_dir / "commondir"
    commondir = commondir_file.read_text(encoding="utf-8").strip()
    common_dir = Path(os.path.abspath(git_dir / commondir))
    exclude = common_dir / "info" / "exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "\nignored.py\n",
        encoding="utf-8",
    )
    head = git_dir / "HEAD"
    head_ref = head.read_text(encoding="ascii").strip().removeprefix("ref:").strip()
    loose_ref = common_dir / head_ref
    assert loose_ref.is_file()
    allowed_paths = (
        linked / "app.py",
        git_file,
        commondir_file,
        head,
        loose_ref,
        exclude,
    )
    allowed_identities = {
        (details.st_dev, details.st_ino)
        for details in (path.stat() for path in allowed_paths)
    }
    original_read = adapter_module.os.read
    unexpected_reads: list[tuple[int, int]] = []

    def tracked_read(descriptor: int, size: int) -> bytes:
        details = os.fstat(descriptor)
        identity = (details.st_dev, details.st_ino)
        if stat.S_ISREG(details.st_mode) and identity not in allowed_identities:
            unexpected_reads.append(identity)
        return original_read(descriptor, size)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    before = adapter.observe(linked)
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "# policy-only change\n",
        encoding="utf-8",
    )
    after = adapter.observe(linked)

    assert tuple(entry.path for entry in after.entries) == ("app.py",)
    assert after.inventory_sha256 == before.inventory_sha256
    assert after.policy_sha256 != before.policy_sha256
    assert unexpected_reads == []


def test_observer_pins_git_head_across_checkout_ancestor_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    container = tmp_path / "container"
    source = container / "source"
    parked = tmp_path / "parked"
    external = tmp_path / "external"
    external_source = external / "source"
    source.mkdir(parents=True)
    external_source.mkdir(parents=True)
    local_payload = b"LOCAL = True\n"
    (source / "app.py").write_bytes(local_payload)
    (external_source / "app.py").write_bytes(b"EXTERNAL_SECRET = True\n")
    _init_git_repo(source, "app.py")
    _init_git_repo(external_source, "app.py")
    local_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    external_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=external_source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert local_commit != external_commit
    external_identities = {
        (details.st_dev, details.st_ino)
        for details in (
            path.stat()
            for path in (external_source / ".git").rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    }
    external_identities.add(
        (
            (external_source / "app.py").stat().st_dev,
            (external_source / "app.py").stat().st_ino,
        )
    )
    original_read = adapter_module.os.read
    original_git_head = adapter_module._PinnedReadAuthority.git_head
    external_reads = 0
    git_head_calls = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) in external_identities:
            external_reads += 1
        return original_read(descriptor, size)

    def swap_around_git_head(
        authority: adapter_module._PinnedReadAuthority,
    ) -> str:
        nonlocal git_head_calls
        git_head_calls += 1
        container.rename(parked)
        container.symlink_to(external, target_is_directory=True)
        try:
            return original_git_head(authority)
        finally:
            container.unlink()
            parked.rename(container)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(
        adapter_module._PinnedReadAuthority,
        "git_head",
        swap_around_git_head,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    observation = adapter.observe(source)

    assert git_head_calls == 4
    assert external_reads == 0
    assert observation.source_commit == local_commit
    assert observation.source_commit != external_commit
    assert tuple(entry.path for entry in observation.entries) == ("app.py",)
    assert observation.entries[0].sha256 == hashlib.sha256(local_payload).hexdigest()


def test_observer_revalidates_absent_loose_git_ref_before_packed_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    subprocess.run(
        ["git", "pack-refs", "--all", "--prune"],
        cwd=source,
        check=True,
    )
    old_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ref_name = (source / ".git" / "HEAD").read_text(encoding="ascii").strip()
    assert ref_name.startswith("ref: ")
    loose_ref = source / ".git" / ref_name.removeprefix("ref: ")
    assert not loose_ref.exists()
    new_commit = "f" * 40 if old_commit != "f" * 40 else "e" * 40
    original_git_payload = adapter_module._PinnedReadAuthority._git_payload
    loose_lookups = 0

    def create_loose_ref_after_final_absence_check(
        authority: adapter_module._PinnedReadAuthority,
        reader: adapter_module._PinnedSourceReader,
        path: Path,
        *,
        size_cap: int,
        allow_missing: bool = False,
    ) -> bytes | None:
        nonlocal loose_lookups
        payload = original_git_payload(
            authority,
            reader,
            path,
            size_cap=size_cap,
            allow_missing=allow_missing,
        )
        if allow_missing and path == loose_ref:
            loose_lookups += 1
            if loose_lookups == 4 and payload is None:
                loose_ref.parent.mkdir(parents=True, exist_ok=True)
                loose_ref.write_text(f"{new_commit}\n", encoding="ascii")
        return payload

    monkeypatch.setattr(
        adapter_module._PinnedReadAuthority,
        "_git_payload",
        create_loose_ref_after_final_absence_check,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    observation = adapter.observe(source, max_inventory_passes=4)

    assert loose_lookups >= 4
    assert observation.source_commit == new_commit


def test_0916_structural_build_supports_linked_worktree_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    output = tmp_path / "staging"
    repo.mkdir()
    (repo / "app.py").write_text("def linked_answer():\n    return 42\n", encoding="utf-8")
    _init_git_repo(repo, "app.py")
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-build", str(linked)],
        cwd=repo,
        check=True,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.EXECUTE,
    ).require_adapter()

    result = adapter.build_structural(linked, output_root=output)

    assert result.detected_code_files == ("app.py",)
    assert (output / "graphify-out" / "graph.json").is_file()


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


def test_observer_revalidates_missing_git_info_exclude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphify.detect as detect_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    exclude = source / ".git" / "info" / "exclude"
    exclude.unlink()
    original_git_info_exclude = detect_module._git_info_exclude
    missing_resolutions = 0

    def create_exclude_after_second_missing_resolution(
        vcs_root: Path,
        **kwargs: Any,
    ) -> Path | None:
        nonlocal missing_resolutions
        resolved = original_git_info_exclude(vcs_root, **kwargs)
        if resolved is None:
            missing_resolutions += 1
            if missing_resolutions == 2:
                exclude.write_text("# appeared after absence check\n", encoding="utf-8")
        return resolved

    monkeypatch.setattr(
        detect_module,
        "_git_info_exclude",
        create_exclude_after_second_missing_resolution,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    raced = adapter.observe(source, max_inventory_passes=4)
    stable = adapter.observe(source)

    assert missing_resolutions == 2
    assert raced.policy_sha256 == stable.policy_sha256


def test_observer_revalidates_workspace_policy_pinned_before_detection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    workspace_config = source / ".graphify" / "workspace.toml"
    workspace_config.parent.mkdir(parents=True)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace_config.write_text("schema_version = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py", ".graphify/workspace.toml")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    def delete_after_detection(event: str, _details: object) -> None:
        if event == "inventory_detected":
            workspace_config.unlink(missing_ok=True)

    with pytest.raises(ObservationUnstable, match="workspace.toml"):
        adapter.observe(
            source,
            max_inventory_passes=2,
            hook=delete_after_detection,
        )

    assert not workspace_config.exists()


@pytest.mark.parametrize("policy_kind", ["symlink", "directory", "hardlink"])
def test_observer_rejects_unsafe_workspace_policy_at_initial_pin(
    tmp_path: Path,
    policy_kind: str,
) -> None:
    source = tmp_path / "source"
    workspace_config = source / ".graphify" / "workspace.toml"
    workspace_config.parent.mkdir(parents=True)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    external = tmp_path / "external.toml"
    external.write_text("schema_version = 1\n", encoding="utf-8")
    if policy_kind == "symlink":
        workspace_config.symlink_to(external)
    elif policy_kind == "directory":
        workspace_config.mkdir()
    else:
        os.link(external, workspace_config)
    _init_git_repo(source, "app.py")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="workspace.toml"):
        adapter.observe(source)


@pytest.mark.parametrize("remove_before_revalidation", [False, True])
def test_observer_rejects_workspace_policy_appearing_after_absent_pin(
    tmp_path: Path,
    remove_before_revalidation: bool,
) -> None:
    source = tmp_path / "source"
    workspace_config = source / ".graphify" / "workspace.toml"
    workspace_config.parent.mkdir(parents=True)
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _init_git_repo(source, "app.py")
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    def appear_during_each_pass(event: str, _details: object) -> None:
        if event == "inventory_detected":
            workspace_config.write_text("schema_version = 1\n", encoding="utf-8")
            if remove_before_revalidation:
                workspace_config.unlink()
        elif event == "inventory_complete":
            workspace_config.unlink(missing_ok=True)

    with pytest.raises(ObservationUnstable, match="workspace.toml"):
        adapter.observe(
            source,
            max_inventory_passes=2,
            hook=appear_during_each_pass,
        )


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


def test_0916_structural_query_rejects_initially_symlinked_payload_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    external_payload = tmp_path / "external" / "graphify-out"
    external_payload.mkdir(parents=True)
    external_graph = external_payload / "graph.json"
    external_graph.write_text(
        '{"nodes":[{"id":"secret","label":"external-secret"}],"links":[]}\n',
        encoding="utf-8",
    )
    payload = tmp_path / "payload"
    payload.symlink_to(external_payload, target_is_directory=True)
    external_identity = external_graph.stat()
    original_read = adapter_module.os.read
    external_reads = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.query_structural(payload, QueryRequest(question="external-secret"))

    assert external_reads == 0


def test_0916_structural_query_rejects_payload_ancestor_swap_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    payload_parent = tmp_path / "payload-parent"
    payload = payload_parent / "graphify-out"
    parked = tmp_path / "parked-payload-parent"
    external = tmp_path / "external"
    external_payload = external / "graphify-out"
    payload.mkdir(parents=True)
    external_payload.mkdir(parents=True)
    local_graph = payload / "graph.json"
    external_graph = external_payload / "graph.json"
    local_graph.write_text(
        '{"nodes":[{"id":"local","label":"local"}],"links":[]}\n',
        encoding="utf-8",
    )
    external_graph.write_text(
        '{"nodes":[{"id":"secret","label":"external-secret"}],"links":[]}\n',
        encoding="utf-8",
    )
    external_identity = external_graph.stat()
    original_pin = adapter_module._PinnedSourceReader.pin_regular
    original_read = adapter_module.os.read
    external_reads = 0
    swapped = False

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal external_reads
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        ):
            external_reads += 1
        return original_read(descriptor, size)

    def pin_then_swap(
        reader: Any,
        path: Path,
    ) -> Any:
        nonlocal swapped
        pinned = original_pin(reader, path)
        if path == local_graph and not swapped:
            payload_parent.rename(parked)
            payload_parent.symlink_to(external, target_is_directory=True)
            swapped = True
        return pinned

    monkeypatch.setattr(adapter_module.os, "read", tracked_read)
    monkeypatch.setattr(
        adapter_module._PinnedSourceReader,
        "pin_regular",
        pin_then_swap,
    )
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="ancestor is not a real directory"):
        adapter.query_structural(payload, QueryRequest(question="local"))

    assert swapped is True
    assert external_reads == 0
    assert external_graph.read_text(encoding="utf-8").endswith(
        '"label":"external-secret"}],"links":[]}\n'
    )


def test_0916_structural_query_does_not_path_stat_graph_after_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphify.workspace.adapters import v0_9_16 as adapter_module

    payload = tmp_path / "graphify-out"
    payload.mkdir()
    graph_path = payload / "graph.json"
    parked = payload / "graph.parked"
    external_graph = tmp_path / "external-graph.json"
    graph_path.write_text(
        '{"nodes":[{"id":"local","label":"local"}],"links":[]}\n',
        encoding="utf-8",
    )
    external_graph.write_text(
        '{"nodes":[{"id":"secret","label":"external-secret"}],"links":[]}\n',
        encoding="utf-8",
    )
    original_pin = adapter_module._PinnedSourceReader.pin_regular
    original_stat = Path.stat
    swapped = False
    path_stat_calls = 0

    def pin_then_swap(
        reader: Any,
        path: Path,
    ) -> Any:
        nonlocal swapped
        pinned = original_pin(reader, path)
        if path == graph_path and not swapped:
            graph_path.rename(parked)
            graph_path.symlink_to(external_graph)
            swapped = True
        return pinned

    def forbid_graph_path_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal path_stat_calls
        if path == graph_path:
            path_stat_calls += 1
            raise AssertionError("query path-statted graph.json after pinning")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        adapter_module._PinnedSourceReader,
        "pin_regular",
        pin_then_swap,
    )
    monkeypatch.setattr(Path, "stat", forbid_graph_path_stat)
    adapter = select_adapter(
        SUPPORTED_COMPATIBILITY,
        intent=AdapterIntent.QUERY,
    ).require_adapter()

    with pytest.raises(ObservationUnsupported, match="singular regular file"):
        adapter.query_structural(payload, QueryRequest(question="local"))

    assert swapped is True
    assert path_stat_calls == 0
    assert external_graph.read_text(encoding="utf-8").endswith(
        '"label":"external-secret"}],"links":[]}\n'
    )


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
