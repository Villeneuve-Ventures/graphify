"""Graphify 0.9.16 engine adapter and read-only source observer."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version as distribution_version
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Mapping

from networkx.exception import NetworkXException
from networkx.readwrite import json_graph

# Engine-private imports are deliberately confined to this versioned adapter.
from graphify.build import build_from_json
from graphify.detect import _is_noise_dir, detect
from graphify.extract import extract
from graphify.security import check_graph_file_size_cap
from graphify.workspace.contracts import CANDIDATE_DISTRIBUTION_VERSION, canonical_json_bytes

from .base import (
    LegacyManifestEntry,
    LegacyStateSnapshot,
    ObservationHook,
    ObservationTimeout,
    ObservationUnavailable,
    ObservationUnstable,
    ObservationUnsupported,
    QueryRejected,
    QueryRequest,
    RetainedFile,
    RetainedStateInvalid,
    SourceEntry,
    SourceObservation,
    StructuralBuild,
    UnsupportedCompatibility,
)


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CACHE_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_LEGACY_HASH_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
_POLICY_NAMES = frozenset({".gitignore", ".graphifyignore", ".graphifyinclude"})


@dataclass(frozen=True)
class _InventoryPass:
    source_commit: str
    inventory_sha256: str
    policy_sha256: str
    entries: tuple[SourceEntry, ...]


def _emit(
    hook: ObservationHook | None,
    event: str,
    **details: object,
) -> None:
    if hook is not None:
        hook(event, details)


def _deadline(deadline_ns: int | None) -> None:
    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
        raise ObservationTimeout("source observation exceeded its deadline")


def _read_regular_once(
    path: Path,
    *,
    collect: bool = False,
) -> tuple[str, os.stat_result, bytes | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ObservationUnstable(f"source file disappeared before open: {path}") from exc
    except OSError as exc:
        raise ObservationUnavailable(f"source file cannot be opened safely: {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ObservationUnsupported(f"source entry is not a singular regular file: {path}")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if collect else None
        while True:
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise ObservationUnstable(f"source file changed while hashing: {path}")
    try:
        installed = path.lstat()
    except FileNotFoundError as exc:
        raise ObservationUnstable(f"source file disappeared after hashing: {path}") from exc
    if (
        not stat.S_ISREG(installed.st_mode)
        or installed.st_nlink != 1
        or (installed.st_dev, installed.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise ObservationUnstable(f"source file was replaced while hashing: {path}")
    payload = None if chunks is None else b"".join(chunks)
    return digest.hexdigest(), after, payload


def _entry(path: Path, root: Path, file_type: str) -> SourceEntry:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ObservationUnsupported(f"detected source escapes the active root: {path}") from exc
    digest, details, _payload = _read_regular_once(path)
    return SourceEntry(
        path=relative,
        file_type=file_type,
        size=details.st_size,
        sha256=digest,
        mode=f"{stat.S_IMODE(details.st_mode):04o}",
    )


def _git_head(root: Path) -> str:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(  # nosec B603
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or _COMMIT_RE.fullmatch(value) is None:
        detail = result.stderr.strip() or result.stdout.strip() or "Git HEAD is unavailable"
        raise ObservationUnavailable(detail)
    return value


def _policy_paths(root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    workspace = root / ".graphify" / "workspace.toml"
    if workspace.exists() or workspace.is_symlink():
        paths.add(workspace)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name != ".graphify" and not _is_noise_dir(name, directory)
        ]
        for name in filenames:
            if name in _POLICY_NAMES:
                paths.add(directory / name)
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def _excluded_label(value: str, root: Path) -> str:
    path_text, marker, detail = value.partition(" [")
    try:
        relative = Path(path_text).relative_to(root).as_posix()
    except ValueError:
        relative = hashlib.sha256(path_text.encode("utf-8", errors="replace")).hexdigest()
    return relative if not marker else f"{relative} [{detail}"


class Graphify0916Adapter:
    """The sole executable v1 adapter, pinned to published Graphify 0.9.16."""

    adapter_id = "graphify-0.9.16/workspace-adapter-v1"
    engine_baseline = "0.9.16"
    detector_id = "graphify-0.9.16/workspace-observer-v1"

    def __init__(self) -> None:
        try:
            installed = distribution_version("graphifyy")
        except PackageNotFoundError as exc:
            raise UnsupportedCompatibility("graphifyy distribution is unavailable") from exc
        if installed != CANDIDATE_DISTRIBUTION_VERSION:
            raise UnsupportedCompatibility(
                "adapter requires distribution "
                f"{CANDIDATE_DISTRIBUTION_VERSION}, found {installed}"
            )

    def build_structural(self, source_root: Path, *, output_root: Path) -> StructuralBuild:
        root = Path(source_root).resolve(strict=True)
        output = Path(output_root).resolve()
        if output == root or root in output.parents:
            raise ObservationUnsupported("engine output root must be external to source")
        output.mkdir(parents=True, exist_ok=True)
        detection = detect(root, cache_root=output, read_only=True)
        code_files = tuple(str(path) for path in detection["files"].get("code", []))
        omitted = tuple(
            sorted(
                Path(path).relative_to(root).as_posix()
                for file_type, paths in detection["files"].items()
                if file_type != "code"
                for path in paths
            )
        )
        extraction = extract([Path(path) for path in code_files], cache_root=output)
        graph = build_from_json(extraction, root=root)
        return StructuralBuild(
            engine_baseline=self.engine_baseline,
            node_count=graph.number_of_nodes(),
            edge_count=graph.number_of_edges(),
            detected_code_files=tuple(
                Path(path).relative_to(root).as_posix() for path in code_files
            ),
            omitted_dispatched_files=omitted,
        )

    def query_structural(self, payload_root: Path, request: QueryRequest) -> str:
        """Run the published 0.9.16 traversal without logs or side effects."""

        from graphify.serve import _query_graph_text

        try:
            root = Path(payload_root).resolve(strict=True)
            graph_path = root / "graph.json"
            check_graph_file_size_cap(graph_path)
            _digest, _details, payload = _read_regular_once(graph_path, collect=True)
            assert payload is not None
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise QueryRejected("graph payload must be an object")
            if "links" not in raw and "edges" in raw:
                raw = dict(raw, links=raw["edges"])
            graph = json_graph.node_link_graph(raw, edges="links")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
            NetworkXException,
        ) as exc:
            raise QueryRejected(f"graph payload cannot be queried: {exc}") from exc
        try:
            return _query_graph_text(
                graph,
                request.question,
                mode=request.mode,
                depth=request.depth,
                token_budget=request.token_budget,
                context_filters=list(request.context_filters),
            )
        except (KeyError, TypeError, ValueError, NetworkXException) as exc:
            raise QueryRejected(f"graph traversal failed safely: {exc}") from exc

    def _inventory_pass(
        self,
        root: Path,
        *,
        pass_index: int,
        deadline_ns: int | None,
        hook: ObservationHook | None,
    ) -> _InventoryPass:
        _deadline(deadline_ns)
        commit_before = _git_head(root)
        detection = detect(root, read_only=True, google_workspace=False)
        _emit(hook, "inventory_detected", pass_index=pass_index)
        if detection.get("walk_errors"):
            raise ObservationUnavailable("source directory enumeration was incomplete")
        if detection.get("comparison_unsupported"):
            raise ObservationUnsupported(
                "Google Workspace shortcuts require an unsupported remote comparison"
            )

        entries: list[SourceEntry] = []
        seen: set[str] = set()
        for file_type, paths in sorted(detection["files"].items()):
            for raw_path in paths:
                path = Path(raw_path)
                source_entry = _entry(path, root, str(file_type))
                if source_entry.path in seen:
                    raise ObservationUnsupported(
                        f"detector returned duplicate source path: {source_entry.path}"
                    )
                seen.add(source_entry.path)
                entries.append(source_entry)
                _emit(
                    hook,
                    "inventory_file_hashed",
                    pass_index=pass_index,
                    path=source_entry.path,
                )
                _deadline(deadline_ns)
        entries.sort(key=lambda item: item.path)

        policy_entries = tuple(
            _entry(path, root, "policy").to_dict() for path in _policy_paths(root)
        )
        excluded = sorted(
            _excluded_label(value, root)
            for bucket in ("skipped_sensitive", "unclassified")
            for value in detection.get(bucket, [])
        )
        commit_after = _git_head(root)
        if commit_before != commit_after:
            raise ObservationUnstable("Git HEAD changed during source observation")
        inventory_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "detector_id": self.detector_id,
                    "entries": [entry.to_dict() for entry in entries],
                    "excluded": excluded,
                }
            )
        ).hexdigest()
        policy_sha256 = hashlib.sha256(
            canonical_json_bytes({"entries": list(policy_entries)})
        ).hexdigest()
        result = _InventoryPass(
            source_commit=commit_after,
            inventory_sha256=inventory_sha256,
            policy_sha256=policy_sha256,
            entries=tuple(entries),
        )
        _emit(
            hook,
            "inventory_complete",
            pass_index=pass_index,
            inventory_sha256=inventory_sha256,
        )
        return result

    def observe(
        self,
        source_root: Path,
        *,
        max_inventory_passes: int = 6,
        deadline_ns: int | None = None,
        hook: ObservationHook | None = None,
    ) -> SourceObservation:
        root = Path(source_root).resolve(strict=True)
        if max_inventory_passes < 2:
            raise ObservationUnstable("at least two complete inventory passes are required")
        previous: _InventoryPass | None = None
        last_unstable: ObservationUnstable | None = None
        for pass_index in range(1, max_inventory_passes + 1):
            _deadline(deadline_ns)
            try:
                current = self._inventory_pass(
                    root,
                    pass_index=pass_index,
                    deadline_ns=deadline_ns,
                    hook=hook,
                )
            except ObservationUnstable as exc:
                previous = None
                last_unstable = exc
                continue
            if previous is not None and current == previous:
                return SourceObservation(
                    source_commit=current.source_commit,
                    inventory_sha256=current.inventory_sha256,
                    policy_sha256=current.policy_sha256,
                    detector_id=self.detector_id,
                    stable_inventory_passes=2,
                    entries=current.entries,
                )
            previous = current
        detail = "source inventory did not produce two consecutive equal passes"
        if last_unstable is not None:
            detail = f"{detail}: {last_unstable}"
        raise ObservationUnstable(detail)

    @staticmethod
    def _manifest_entry(path: str, raw: object) -> LegacyManifestEntry:
        if isinstance(raw, bool):
            raise RetainedStateInvalid(f"legacy manifest entry is invalid: {path}")
        if isinstance(raw, (int, float)):
            mtime: int | float = raw
            ast_hash = ""
            semantic_hash = ""
        elif isinstance(raw, Mapping):
            mtime_value = raw.get("mtime", 0)
            if isinstance(mtime_value, bool) or not isinstance(mtime_value, (int, float)):
                raise RetainedStateInvalid(f"legacy manifest mtime is invalid: {path}")
            mtime = mtime_value
            ast_hash = raw.get("ast_hash", raw.get("hash", ""))
            semantic_hash = raw.get("semantic_hash", "")
        else:
            raise RetainedStateInvalid(f"legacy manifest entry is invalid: {path}")
        if not isinstance(ast_hash, str) or not isinstance(semantic_hash, str):
            raise RetainedStateInvalid(f"legacy manifest hash is invalid: {path}")
        for value in (ast_hash, semantic_hash):
            if value and _LEGACY_HASH_RE.fullmatch(value) is None:
                raise RetainedStateInvalid(f"legacy manifest hash is invalid: {path}")
        return LegacyManifestEntry(
            path=path,
            mtime=mtime,
            ast_hash=ast_hash,
            semantic_hash=semantic_hash,
        )

    def read_retained_state(
        self,
        retained_root: Path,
        *,
        source_version: str,
    ) -> LegacyStateSnapshot:
        if source_version != "0.9.12":
            raise UnsupportedCompatibility(
                f"unsupported retained state version: {source_version}"
            )
        root = Path(retained_root).resolve(strict=True)
        output = root / "graphify-out"
        if not output.is_dir() or output.is_symlink():
            raise RetainedStateInvalid("retained graphify-out is missing or unsafe")
        files: list[RetainedFile] = []
        payloads: dict[str, bytes] = {}
        for path in sorted(output.rglob("*")):
            details = path.lstat()
            if stat.S_ISDIR(details.st_mode):
                continue
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise RetainedStateInvalid(f"retained entry is not a regular file: {path}")
            digest, observed, payload = _read_regular_once(path, collect=True)
            assert payload is not None
            relative = path.relative_to(root).as_posix()
            payloads[relative] = payload
            files.append(
                RetainedFile(
                    path=relative,
                    size=observed.st_size,
                    sha256=digest,
                )
            )

        manifest_path = "graphify-out/manifest.json"
        try:
            manifest_raw = json.loads(payloads[manifest_path])
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetainedStateInvalid("legacy manifest is missing or invalid") from exc
        if not isinstance(manifest_raw, dict) or not all(
            isinstance(path, str) for path in manifest_raw
        ):
            raise RetainedStateInvalid("legacy manifest must be a string-keyed object")
        manifest_entries = tuple(
            self._manifest_entry(path, manifest_raw[path]) for path in sorted(manifest_raw)
        )

        cache_entries: list[str] = []
        for relative, payload in sorted(payloads.items()):
            parts = Path(relative).parts
            if len(parts) < 4 or parts[:2] != ("graphify-out", "cache"):
                continue
            if parts[2] not in {"ast", "semantic"}:
                continue
            if _CACHE_NAME_RE.fullmatch(parts[-1]) is None:
                raise RetainedStateInvalid(f"legacy cache entry has an invalid name: {relative}")
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RetainedStateInvalid(f"legacy cache entry is invalid: {relative}") from exc
            if not isinstance(value, dict) or not isinstance(value.get("nodes"), list) or not isinstance(
                value.get("edges"), list
            ):
                raise RetainedStateInvalid(f"legacy cache entry has an unsupported shape: {relative}")
            cache_entries.append(relative)

        graph_path = "graphify-out/graph.json"
        try:
            graph = json.loads(payloads[graph_path])
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetainedStateInvalid("legacy graph artifact is missing or invalid") from exc
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(
            graph.get("links"), list
        ) or any(
            not isinstance(item, Mapping)
            for item in (*graph["nodes"], *graph["links"])
        ):
            raise RetainedStateInvalid("legacy graph artifact has an unsupported shape")

        report_path = "graphify-out/GRAPH_REPORT.md"
        try:
            report = payloads[report_path].decode("utf-8")
        except (KeyError, UnicodeDecodeError) as exc:
            raise RetainedStateInvalid("legacy graph report is missing or invalid") from exc
        if not report.startswith("# Graph Report - ") or "\n## Corpus Check\n" not in report:
            raise RetainedStateInvalid("legacy graph report has an unsupported shape")
        artifact_entries = tuple(
            relative
            for relative in sorted(payloads)
            if relative != manifest_path and "/cache/" not in relative
        )
        return LegacyStateSnapshot(
            source_version=source_version,
            manifest_entries=manifest_entries,
            cache_entries=tuple(cache_entries),
            artifact_entries=artifact_entries,
            files=tuple(files),
        )


__all__ = ["Graphify0916Adapter"]
