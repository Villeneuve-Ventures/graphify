"""Explicit-root P1 artifact and isolated compensation proof helpers.

This module is build/test tooling.  It does not install into the real user home
and is not imported by Graphify's production CLI or installer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import cast
import uuid
import zipfile

from graphify.workspace import (
    ArtifactManifest,
    CANDIDATE_DISTRIBUTION_VERSION,
    CompensationPlan,
    ContractError,
    InstallerTransaction,
    OfflineRollback,
    WORKSPACE_SCHEMA_FILES,
    canonical_json_bytes,
    canonical_sha256,
    validate_installer_compensation,
)


FIXED_SOURCE_EPOCH = 1784069832
_ZIP_TIME = datetime.fromtimestamp(FIXED_SOURCE_EPOCH, tz=timezone.utc).timetuple()[:6]
_REQUIRED_TAMPER_ARTIFACTS = (
    f"graphifyy-{CANDIDATE_DISTRIBUTION_VERSION}-py3-none-any.whl",
    "skill-bundle.zip",
    "contract-bundle.zip",
    "fixture-manifest.json",
)


class ArtifactError(RuntimeError):
    """A deterministic artifact or isolated compensation proof failed."""


@dataclass(frozen=True)
class _ZipMember:
    name: str
    data: bytes
    mode: int = 0o644


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_tree_manifest(
    root: Path,
    *,
    exclude: Iterable[str] = (),
) -> list[dict[str, object]]:
    """Inventory every regular file and reject links, hardlinks, and special nodes."""
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ArtifactError(f"strict tree root is unavailable: {root}: {exc}") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise ArtifactError(f"strict tree root must be a real directory: {root}")
    excluded = set(exclude)
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ArtifactError(f"cannot inspect tree node {relative}: {exc}") from exc
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ArtifactError(f"forbidden filesystem node in strict tree: {relative}")
        if relative in excluded:
            continue
        entries.append(
            {
                "path": relative,
                "size": metadata.st_size,
                "sha256": sha256_file(path),
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            }
        )
    return entries


def strict_tree_sha256(root: Path, *, exclude: Iterable[str] = ()) -> str:
    return canonical_sha256(strict_tree_manifest(root, exclude=exclude))


def _ensure_absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ArtifactError(f"{label} must be an absolute path: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ArtifactError(f"{label} must be a real directory: {path}")
    return path


def _relative_file(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ArtifactError(f"artifact input escapes explicit root {root}: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise ArtifactError(f"artifact input must be a regular non-symlink file: {path}")
    name = relative.as_posix()
    if ".." in PurePosixPath(name).parts:
        raise ArtifactError(f"artifact input path escapes root: {name}")
    return name


def _input_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ArtifactError(f"{label} must be an absolute regular non-symlink file: {path}")
    return path


def _write_deterministic_zip(path: Path, members: Iterable[_ZipMember]) -> None:
    ordered = sorted(members, key=lambda member: member.name)
    names = [member.name for member in ordered]
    if len(names) != len(set(names)):
        raise ArtifactError("deterministic ZIP contains duplicate member names")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for member in ordered:
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in member.name:
                raise ArtifactError(f"unsafe ZIP member path: {member.name}")
            info = zipfile.ZipInfo(member.name, date_time=_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | member.mode) << 16
            archive.writestr(info, member.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _tree_members(root: Path, base: Path, archive_prefix: str) -> list[_ZipMember]:
    if not base.is_dir() or base.is_symlink():
        raise ArtifactError(f"required bundle directory missing or unsafe: {base}")
    members: list[_ZipMember] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        _relative_file(root, path)
        suffix = path.relative_to(base).as_posix()
        members.append(_ZipMember(f"{archive_prefix}/{suffix}", path.read_bytes()))
    if not members:
        raise ArtifactError(f"bundle directory contains no regular files: {base}")
    return members


def _artifact_entry(root: Path, path: Path) -> dict[str, object]:
    relative = _relative_file(root, path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode not in {0o600, 0o644, 0o755}:
        raise ArtifactError(f"artifact input has unsupported mode {mode:04o}: {path}")
    return {
        "path": relative,
        "file_type": "regular_file",
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "mode": f"{mode:04o}",
    }


def build_static_bundles(
    *,
    repo_root: Path,
    output_root: Path,
    wheel: Path,
    runtime_manifest: Path,
) -> dict[str, Path]:
    """Build deterministic P1 static bundles under an explicit output root."""
    repo_root = _ensure_absolute_directory(repo_root, "repo_root")
    output_root = _ensure_absolute_directory(output_root, "output_root")
    wheel_name = f"graphifyy-{CANDIDATE_DISTRIBUTION_VERSION}-py3-none-any.whl"
    if wheel.name != wheel_name:
        raise ArtifactError(f"wheel must be named {wheel_name}, got {wheel.name}")
    wheel = _input_file(wheel, "wheel")
    runtime_manifest = _input_file(runtime_manifest, "runtime_manifest")

    wheel_output = output_root / wheel_name
    shutil.copyfile(wheel, wheel_output)

    skill_members = [
        _ZipMember("skill/SKILL.md", (repo_root / "graphify" / "skill-codex.md").read_bytes()),
        _ZipMember("skill/.graphify_version", CANDIDATE_DISTRIBUTION_VERSION.encode("utf-8")),
    ]
    skill_members.extend(
        _tree_members(
            repo_root,
            repo_root / "graphify" / "skills" / "codex" / "references",
            "skill/references",
        )
    )
    skill_bundle = output_root / "skill-bundle.zip"
    _write_deterministic_zip(skill_bundle, skill_members)

    schema_root = repo_root / "graphify" / "workspace" / "schemas" / "v1"
    actual_schema_files = {
        path.name for path in schema_root.iterdir() if path.is_file() and not path.is_symlink()
    }
    if actual_schema_files != set(WORKSPACE_SCHEMA_FILES):
        missing = sorted(set(WORKSPACE_SCHEMA_FILES) - actual_schema_files)
        extra = sorted(actual_schema_files - set(WORKSPACE_SCHEMA_FILES))
        raise ArtifactError(
            f"workspace schema set mismatch: missing={missing}, extra={extra}"
        )
    contract_members = _tree_members(
        repo_root,
        repo_root / "docs" / "workspace" / "v1",
        "docs/workspace/v1",
    )
    contract_members.extend(
        _tree_members(
            repo_root,
            schema_root,
            "schemas/v1",
        )
    )
    contract_members.extend(
        [
            _ZipMember(
                "reference/graphify/workspace/__init__.py",
                (repo_root / "graphify" / "workspace" / "__init__.py").read_bytes(),
            ),
            _ZipMember(
                "reference/graphify/workspace/contracts.py",
                (repo_root / "graphify" / "workspace" / "contracts.py").read_bytes(),
            ),
        ]
    )
    contract_bundle = output_root / "contract-bundle.zip"
    _write_deterministic_zip(contract_bundle, contract_members)

    fixture_root = repo_root / "tests" / "fixtures" / "workspace" / "v1"
    fixture_entries = [
        _artifact_entry(fixture_root, path)
        for path in sorted(fixture_root.rglob("*"))
        if path.is_file()
    ]
    fixture_manifest_data = {
        "format_version": 1,
        "source_epoch": FIXED_SOURCE_EPOCH,
        "sanitization": "synthetic-contract-fixtures-no-repository-content",
        "representative_scope": (
            "p1-contract-only; representative MTR/mac-mini/Aletheia corpus tiers "
            "are authority-required P5 verification inputs and are not copied in P1"
        ),
        "golden_outcomes": [
            "accept-positive",
            "reject-negative",
            "canonical-round-trip",
            "reject-future-version",
            "offline-compensation",
        ],
        "entries": fixture_entries,
    }
    fixture_manifest = output_root / "fixture-manifest.json"
    fixture_manifest.write_bytes(canonical_json_bytes(fixture_manifest_data))
    fixture_bundle = output_root / "fixture-bundle.zip"
    _write_deterministic_zip(
        fixture_bundle,
        _tree_members(fixture_root, fixture_root, "fixtures/v1"),
    )

    runtime_members = [
        _ZipMember("runtime/uv.lock", (repo_root / "uv.lock").read_bytes()),
        _ZipMember(f"runtime/{runtime_manifest.name}", runtime_manifest.read_bytes()),
    ]
    runtime_bundle = output_root / "runtime-bundle.zip"
    _write_deterministic_zip(runtime_bundle, runtime_members)

    return {
        wheel_output.name: wheel_output,
        skill_bundle.name: skill_bundle,
        contract_bundle.name: contract_bundle,
        fixture_bundle.name: fixture_bundle,
        fixture_manifest.name: fixture_manifest,
        runtime_bundle.name: runtime_bundle,
    }


def write_trusted_manifest(
    *,
    artifact_root: Path,
    artifact_names: Sequence[str],
    destination: Path | None = None,
) -> bytes:
    artifact_root = _ensure_absolute_directory(artifact_root, "artifact_root")
    destination = destination or artifact_root / "trusted-manifest.json"
    artifact_paths = [artifact_root / name for name in sorted(artifact_names)]
    if not artifact_paths:
        raise ArtifactError("trusted manifest must cover at least one artifact")
    for path in artifact_paths:
        _relative_file(artifact_root, path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ArtifactError(f"artifact input must be a single-link regular file: {path}")
        path.chmod(0o644)
    entries = [_artifact_entry(artifact_root, path) for path in artifact_paths]
    data = {
        "contract": "graphify.workspace.artifact_manifest",
        "schema_version": 1,
        "manifest_version": 1,
        "artifacts": entries,
    }
    document = ArtifactManifest.from_mapping(data)
    destination.write_bytes(document.canonical)
    destination.chmod(0o644)
    return document.canonical


def verify_trusted_manifest(*, artifact_root: Path, trusted_manifest: bytes) -> None:
    artifact_root = _ensure_absolute_directory(artifact_root, "artifact_root")
    try:
        document = ArtifactManifest.from_json(trusted_manifest)
    except ContractError as exc:
        raise ArtifactError(f"trusted manifest is invalid: {exc}") from exc
    actual_entries = {
        str(entry["path"]): entry
        for entry in strict_tree_manifest(
            artifact_root,
            exclude={"trusted-manifest.json"},
        )
    }
    expected: set[str] = set()
    for entry in document.to_dict()["artifacts"]:
        name = str(entry["path"])
        expected.add(name)
        actual_entry = actual_entries.get(name)
        if actual_entry is None:
            raise ArtifactError(f"trusted artifact is missing or not a regular file: {name}")
        if actual_entry["sha256"] != entry["sha256"]:
            raise ArtifactError(f"trusted artifact digest mismatch: {name}")
        if actual_entry["size"] != entry["size"]:
            raise ArtifactError(f"trusted artifact size mismatch: {name}")
        actual_mode = actual_entry["mode"]
        if actual_mode != entry["mode"]:
            raise ArtifactError(
                f"trusted artifact mode mismatch: {name}: {actual_mode} != {entry['mode']}"
            )
    actual_names = set(actual_entries)
    if actual_names != expected:
        missing = sorted(expected - actual_names)
        extra = sorted(actual_names - expected)
        raise ArtifactError(f"artifact set mismatch: missing={missing}, extra={extra}")


def prove_independent_tamper_rejection(
    *,
    artifact_root: Path,
    trusted_manifest: bytes,
    artifact_names: Sequence[str] = _REQUIRED_TAMPER_ARTIFACTS,
) -> dict[str, str]:
    """Tamper each required artifact independently, verify rejection, and restore it."""
    results: dict[str, str] = {}
    verify_trusted_manifest(artifact_root=artifact_root, trusted_manifest=trusted_manifest)
    try:
        document = ArtifactManifest.from_json(trusted_manifest)
    except ContractError as exc:
        raise ArtifactError(f"trusted manifest is invalid: {exc}") from exc
    trusted_names = {str(entry["path"]) for entry in document.to_dict()["artifacts"]}
    for name in artifact_names:
        pure = PurePosixPath(name)
        if (
            not name
            or pure.is_absolute()
            or ".." in pure.parts
            or str(pure) != name
            or "\\" in name
        ):
            raise ArtifactError(f"invalid tamper artifact name: {name!r}")
        if name not in trusted_names:
            raise ArtifactError(f"tamper artifact is not covered by trusted manifest: {name}")
        path = artifact_root.joinpath(*pure.parts)
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\x00")
            try:
                verify_trusted_manifest(
                    artifact_root=artifact_root,
                    trusted_manifest=trusted_manifest,
                )
            except ArtifactError as exc:
                results[name] = str(exc)
            else:
                raise ArtifactError(f"tampered artifact was accepted: {name}")
        finally:
            path.write_bytes(original)
    verify_trusted_manifest(artifact_root=artifact_root, trusted_manifest=trusted_manifest)
    return results


_HOME_TARGETS = {
    "binary": Path(".local/bin/graphify"),
    "runtime": Path(".local/state/graphify/runtime-manifest.json"),
    "service": Path("Library/LaunchAgents/com.graphify.fixture.plist"),
}
_CODEX_TARGETS = {"skill": Path("skills/graphify/SKILL.md")}


def _target_paths(home: Path, codex_home: Path) -> dict[str, Path]:
    targets = {name: home / relative for name, relative in _HOME_TARGETS.items()}
    targets.update({name: codex_home / relative for name, relative in _CODEX_TARGETS.items()})
    return targets


def _file_snapshot(paths: Mapping[str, Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ArtifactError(f"snapshot target must be a regular non-symlink file: {path}")
        result[name] = sha256_file(path)
    return result


def snapshot_disposable_home(
    *,
    home: Path,
    codex_home: Path,
    rollback_bundle: Path,
) -> bytes:
    home = _ensure_absolute_directory(home, "HOME")
    codex_home = _ensure_absolute_directory(codex_home, "CODEX_HOME")
    targets = _target_paths(home, codex_home)
    members: list[_ZipMember] = []
    entries: list[dict[str, object]] = []

    def add(archive_name: str, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ArtifactError(f"rollback input must be a regular non-symlink file: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode not in {0o600, 0o644, 0o755}:
            raise ArtifactError(f"rollback input has unsupported mode {mode:04o}: {path}")
        members.append(_ZipMember(archive_name, path.read_bytes(), mode=mode))
        entries.append(
            {
                "path": archive_name,
                "file_type": "regular_file",
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "mode": f"{mode:04o}",
            }
        )

    restore_order: list[str] = []
    for name in ("binary", "runtime"):
        archive_name = f"snapshot/{name}"
        add(archive_name, targets[name])
        restore_order.append(archive_name)
    skill_root = targets["skill"].parent
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise ArtifactError(f"rollback skill root must be a real directory: {skill_root}")
    for path in sorted(skill_root.rglob("*")):
        if path.is_symlink():
            raise ArtifactError(f"rollback skill tree cannot contain symlinks: {path}")
        if path.is_file():
            archive_name = f"snapshot/skill/{path.relative_to(skill_root).as_posix()}"
            add(archive_name, path)
            restore_order.append(archive_name)
    service_archive = "snapshot/service"
    add(service_archive, targets["service"])
    restore_order.append(service_archive)
    entries.sort(key=lambda entry: str(entry["path"]))
    rollback = {
        "contract": "graphify.workspace.offline_rollback",
        "schema_version": 1,
        "bundle_version": 1,
        "offline": True,
        "entries": entries,
        "restore_order": restore_order,
        "generation_disposition": "preserve_untouched",
    }
    document = OfflineRollback.from_mapping(rollback)
    members.append(_ZipMember("rollback.json", document.canonical))
    _write_deterministic_zip(rollback_bundle, members)
    return document.canonical


def _optional_file_sha256(path: Path) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ArtifactError(f"transaction item must be a regular non-symlink file: {path}")
    return sha256_file(path)


def _snapshot_file_roots(roots: Sequence[Path]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for root in sorted(set(roots), key=str):
        for entry in strict_tree_manifest(root):
            path = root / str(entry["path"])
            absolute = str(path)
            if absolute in snapshot:
                raise ArtifactError(f"overlapping compensation audit roots include {path}")
            snapshot[absolute] = canonical_sha256(entry)
    return snapshot


def _stage_transaction_item(*, target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _prune_empty_parents(*, start: Path, home: Path, codex_home: Path) -> None:
    boundary = codex_home if start == codex_home or codex_home in start.parents else home
    current = start
    while current != boundary:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _validate_compensation_execution(
    plan: CompensationPlan,
    execution: Mapping[str, object],
) -> None:
    plan_data = plan.to_dict()
    if execution.get("remove_if_created") != plan_data["remove_if_created"]:
        raise ArtifactError("compensation executor removal order diverged from plan")
    if execution.get("restore_order") != plan_data["restore_order"]:
        raise ArtifactError("compensation executor restore order diverged from plan")
    if execution.get("restore_artifacts") != plan_data["restore_artifacts"]:
        raise ArtifactError("compensation executor restore mapping diverged from plan")


def _restore_offline(
    *,
    home: Path,
    codex_home: Path,
    rollback_bundle: Path,
    transaction: InstallerTransaction,
    plan: CompensationPlan,
    rollback: OfflineRollback,
) -> dict[str, object]:
    with zipfile.ZipFile(rollback_bundle) as archive:
        document = cast(OfflineRollback, OfflineRollback.from_json(archive.read("rollback.json")))
        if document.canonical != rollback.canonical:
            raise ArtifactError("rollback document changed after contract validation")
        validate_installer_compensation(transaction, plan, document)
        entry_by_path = {
            str(entry["path"]): entry for entry in document.to_dict()["entries"]
        }
        plan_data = plan.to_dict()
        removed: list[str] = []
        for raw_path in plan_data["remove_if_created"]:
            target = Path(str(raw_path))
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                raise ArtifactError(f"compensation remove action is not a file: {target}")
            removed.append(str(target))
            _prune_empty_parents(
                start=target.parent,
                home=home,
                codex_home=codex_home,
            )
        mapping_by_path = {
            str(mapping["path"]): str(mapping["offline_artifact"])
            for mapping in plan_data["restore_artifacts"]
        }
        restored: list[str] = []
        used_mappings: list[dict[str, str]] = []
        for raw_target in plan_data["restore_order"]:
            target = Path(str(raw_target))
            archive_name = mapping_by_path[str(target)]
            expected = entry_by_path[archive_name]
            data = archive.read(archive_name)
            if hashlib.sha256(data).hexdigest() != expected["sha256"]:
                raise ArtifactError(f"rollback bundle digest mismatch: {archive_name}")
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                raise ArtifactError(f"compensation restore target is not a file: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(int(str(expected["mode"]), 8))
            restored.append(str(target))
            used_mappings.append(
                {"path": str(target), "offline_artifact": archive_name}
            )
    return {
        "remove_if_created": removed,
        "restore_order": restored,
        "restore_artifacts": used_mappings,
    }


def run_disposable_compensation_proof(
    *,
    home: Path,
    codex_home: Path,
    rollback_bundle: Path,
    candidate_files: Mapping[str, bytes],
    fail_after: str = "skill",
) -> dict[str, object]:
    """Stage fixture state, inject failure, then compensate without network access."""
    home = _ensure_absolute_directory(home, "HOME")
    codex_home = _ensure_absolute_directory(codex_home, "CODEX_HOME")
    if os.environ.get("HOME") != str(home) or os.environ.get("CODEX_HOME") != str(codex_home):
        raise ArtifactError("HOME and CODEX_HOME must match the explicit disposable roots")
    targets = _target_paths(home, codex_home)
    if set(candidate_files) != set(targets):
        raise ArtifactError(f"candidate_files must contain exactly {sorted(targets)}")
    before = _file_snapshot(targets)
    skill_root = targets["skill"].parent
    skill_before = strict_tree_sha256(skill_root)
    generations_root = home / ".local/state/graphify/workspaces/fixture/generations"
    if not strict_tree_manifest(generations_root):
        raise ArtifactError("generations fixture tree must contain at least one regular file")
    generations_before = strict_tree_sha256(generations_root)
    stage_order = ("binary", "runtime", "skill", "service")
    if fail_after not in stage_order:
        raise ArtifactError(f"unknown failpoint {fail_after!r}")
    staged_items = [
        (
            "binary",
            "binary",
            targets["binary"],
            candidate_files["binary"],
            "snapshot/binary",
        ),
        (
            "runtime",
            "runtime",
            targets["runtime"],
            candidate_files["runtime"],
            "snapshot/runtime",
        ),
        (
            "skill",
            "skill",
            targets["skill"],
            candidate_files["skill"],
            "snapshot/skill/SKILL.md",
        ),
        (
            "skill",
            "skill-version",
            skill_root / ".graphify_version",
            CANDIDATE_DISTRIBUTION_VERSION.encode("utf-8"),
            "snapshot/skill/.graphify_version",
        ),
        (
            "skill",
            "skill-reference",
            skill_root / "references/candidate.md",
            b"candidate-only\n",
            "snapshot/skill/references/candidate.md",
        ),
        (
            "service",
            "service",
            targets["service"],
            candidate_files["service"],
            "snapshot/service",
        ),
    ]
    before_by_path = {
        str(target): _optional_file_sha256(target)
        for _group, _label, target, _data, _artifact in staged_items
    }
    with zipfile.ZipFile(rollback_bundle) as archive:
        rollback_document = cast(
            OfflineRollback,
            OfflineRollback.from_json(archive.read("rollback.json")),
        )
    candidate_digests = {
        label: hashlib.sha256(data).hexdigest()
        for _group, label, _target, data, _artifact in staged_items
    }
    transaction_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "graphify.workspace.install:"
            f"{rollback_document.sha256}:{canonical_sha256(candidate_digests)}",
        )
    )
    restore_items = [
        (target, artifact)
        for _group, _label, target, _data, artifact in staged_items
        if before_by_path[str(target)] is not None
    ]
    remove_items = [
        target
        for _group, _label, target, _data, _artifact in staged_items
        if before_by_path[str(target)] is None
    ]
    plan_document = cast(
        CompensationPlan,
        CompensationPlan.from_mapping(
            {
                "contract": "graphify.workspace.compensation_plan",
                "schema_version": 1,
                "transaction_id": transaction_id,
                "restore_order": [str(target) for target, _artifact in restore_items],
                "remove_if_created": [str(target) for target in remove_items],
                "restore_artifacts": [
                    {"path": str(target), "offline_artifact": artifact}
                    for target, artifact in restore_items
                ],
                "required_offline_artifacts": [artifact for _target, artifact in restore_items],
                "generation_disposition": "preserve_untouched",
            }
        ),
    )
    transaction_document = cast(
        InstallerTransaction,
        InstallerTransaction.from_mapping(
            {
                "contract": "graphify.workspace.installer_transaction",
                "schema_version": 1,
                "transaction_id": transaction_id,
                "phase": "PREPARED",
                "home": str(home),
                "codex_home": str(codex_home),
                "candidate_manifest_sha256": canonical_sha256(candidate_digests),
                "items": [
                    {
                        "path": str(target),
                        "before_sha256": before_by_path[str(target)],
                        "after_sha256": candidate_digests[label],
                    }
                    for _group, label, target, _data, _artifact in staged_items
                ],
                "compensation_plan_sha256": plan_document.sha256,
                "generation_disposition": "preserve_untouched",
            }
        ),
    )
    pair_validation = validate_installer_compensation(
        transaction_document,
        plan_document,
        rollback_document,
    )
    transaction_paths = {
        str(item["path"]) for item in transaction_document.to_dict()["items"]
    }
    audit_roots = (
        targets["binary"].parent,
        targets["runtime"].parent,
        skill_root,
        targets["service"].parent,
    )
    install_snapshot_before = _snapshot_file_roots(audit_roots)
    failpoint_triggered = False
    execution: dict[str, object] | None = None
    try:
        for group in stage_order:
            for item_group, _label, target, data, _artifact in staged_items:
                if item_group == group:
                    _stage_transaction_item(target=target, data=data)
            if group == fail_after:
                failpoint_triggered = True
                raise ArtifactError(f"injected failure after {group}")
    except ArtifactError:
        install_snapshot_after = _snapshot_file_roots(audit_roots)
        changed_paths = {
            path
            for path in set(install_snapshot_before) | set(install_snapshot_after)
            if install_snapshot_before.get(path) != install_snapshot_after.get(path)
        }
        untracked_paths = sorted(changed_paths - transaction_paths)
        execution = _restore_offline(
            home=home,
            codex_home=codex_home,
            rollback_bundle=rollback_bundle,
            transaction=transaction_document,
            plan=plan_document,
            rollback=rollback_document,
        )
        _validate_compensation_execution(plan_document, execution)
        if untracked_paths:
            raise ArtifactError(
                f"transaction has untracked created or mutated path(s): {untracked_paths}"
            )
    if not failpoint_triggered:
        raise ArtifactError("configured failpoint was not triggered")
    if execution is None:
        raise ArtifactError("compensation executor did not produce an execution record")
    after = _file_snapshot(targets)
    skill_after = strict_tree_sha256(skill_root)
    generations_after = strict_tree_sha256(generations_root)
    if before != after:
        raise ArtifactError(f"compensation did not restore the prior tuple: {before} != {after}")
    if skill_before != skill_after:
        raise ArtifactError("compensation did not restore the complete prior skill tree")
    if generations_before != generations_after:
        raise ArtifactError("compensation changed the generations tree")
    if (targets["skill"].parent / "references/candidate.md").exists():
        raise ArtifactError("compensation left a candidate-only skill reference behind")
    return {
        "failpoint": fail_after,
        "failpoint_triggered": True,
        "offline": True,
        "contract_pair_validation": pair_validation,
        "installer_transaction_sha256": transaction_document.sha256,
        "compensation_plan_sha256": plan_document.sha256,
        "offline_rollback_sha256": rollback_document.sha256,
        "installer_transaction_preimage": transaction_document.to_dict(),
        "compensation_plan_preimage": plan_document.to_dict(),
        "compensation_execution": execution,
        "restored": before,
        "skill_tree_restored": True,
        "generations_tree_sha256_before": generations_before,
        "generations_tree_sha256_after": generations_after,
        "generations_unchanged": True,
    }


def write_proof(path: Path, proof: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(proof))


__all__ = [
    "ArtifactError",
    "FIXED_SOURCE_EPOCH",
    "build_static_bundles",
    "prove_independent_tamper_rejection",
    "run_disposable_compensation_proof",
    "sha256_file",
    "snapshot_disposable_home",
    "strict_tree_manifest",
    "strict_tree_sha256",
    "verify_trusted_manifest",
    "write_proof",
    "write_trusted_manifest",
]
