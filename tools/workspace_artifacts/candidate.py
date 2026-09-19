"""Content-derived local fixture bundles, not release candidate certification.

The input inventory includes tracked and unignored untracked files, so a dirty
S2 diff is identified honestly. The base commit is provenance only. The wheel
must match every intended package member before any fixture output is created.
"""
from __future__ import annotations

import hashlib
import io
import configparser
from pathlib import Path
import stat
import subprocess
import tomllib
import zipfile

from graphify.workspace.composition import StructuralPolicy, WorkspaceRuntimeAuthority
from graphify.workspace.contracts import (
    ADAPTER_CONTRACT_VERSION, CompatibilityManifest, ContractError, DETECTOR_ID,
    DISTRIBUTION_VERSION, ENGINE_BASELINE, EXTRACTOR_CACHE_ABI, GRAPH_PAYLOAD_VERSION,
    INPUT_MANIFEST_VERSION, INSTALLATION_METADATA, SCHEMA_FILES, STATE_SCHEMA_VERSION,
    canonical_json_bytes, canonical_sha256,
)


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024 * 1024:
        raise ContractError("fixture input must be a bounded singular regular file")
    return path.read_bytes()


def package_members(repo):
    """Mirror explicit setuptools package/data selection, preserving all host data."""
    config = tomllib.loads((repo / "pyproject.toml").read_text())["tool"]["setuptools"]
    members = set()
    for package in config["packages"]:
        directory = repo / package.replace(".", "/")
        members.update(directory.glob("*.py"))
    for package, patterns in config["package-data"].items():
        directory = repo / package.replace(".", "/")
        for pattern in patterns:
            matches = set(directory.glob(pattern))
            if not matches:
                raise ContractError(f"empty package-data pattern: {pattern}")
            members.update(matches)
    return {p.relative_to(repo).as_posix(): hashlib.sha256(_read(p)).hexdigest()
            for p in sorted(members)}


def source_manifest(repo):
    repo = repo.resolve(strict=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                    ENGINE_BASELINE.removeprefix("git:"), head], check=True, capture_output=True)
    names = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z", "--cached",
                                     "--others", "--exclude-standard"]).decode().split("\0")
    files = {}
    for name in sorted(set(names) - {""}):
        path = repo / name
        # Explicit deletion is part of this local candidate; never silently skip it.
        files[name] = (None if not path.exists() and not path.is_symlink() else
                       {"sha256": hashlib.sha256(_read(path)).hexdigest(),
                        "mode": stat.S_IMODE(path.lstat().st_mode)})
    return {"kind": "local-fixture", "base_commit": head, "files": files}


def build_fixture(*, repo_root, wheel, output_root, policy: StructuralPolicy):
    """Write only a new explicit output directory after validating every input."""
    repo, wheel, output = map(Path, (repo_root, wheel, output_root))
    if not all(p.is_absolute() for p in (repo, wheel, output)):
        raise ContractError("fixture paths must be absolute")
    repo = repo.resolve(strict=True)
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(repo):
        raise ContractError("fixture output must be new and outside source checkout")
    if not isinstance(policy, StructuralPolicy):
        raise ContractError("explicit fixture policy is required")
    inventory = source_manifest(repo)
    members = package_members(repo)
    wheel_bytes = _read(wheel)
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ContractError("duplicate wheel members")
        actual = {name: hashlib.sha256(archive.read(name)).hexdigest()
                  for name in names if name.startswith("graphify/")}
        if actual != members:
            raise ContractError("wheel does not match the complete candidate package")
        dist_info = f"graphifyy-{DISTRIBUTION_VERSION}.dist-info/"
        allowed = set(members) | {dist_info + name for name in (
            "METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD", "licenses/LICENSE")}
        if set(names) != allowed:
            raise ContractError("unexpected whole-wheel members")
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                raise ContractError("wheel contains a non-regular member")
        project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]
        entry_points = configparser.ConfigParser()
        entry_points.read_string(archive.read(dist_info + "entry_points.txt").decode())
        if (entry_points.sections() != ["console_scripts"]
                or dict(entry_points["console_scripts"]) != project["scripts"]):
            raise ContractError("wheel entry points differ from project")
        if archive.read(dist_info + "licenses/LICENSE") != _read(repo / "LICENSE"):
            raise ContractError("wheel license differs from project")
        metadata_name = dist_info + "METADATA"
        from email.parser import BytesParser
        meta = BytesParser().parsebytes(archive.read(metadata_name))
        if (meta["Name"] != "graphifyy" or meta["Version"] != DISTRIBUTION_VERSION
                or meta["Requires-Python"] != "==3.14.*,>=3.14.2"):
            raise ContractError("wheel distribution identity mismatch")
        if set(meta.get_all("Provides-Extra", [])) != set(project["optional-dependencies"]):
            raise ContractError("wheel optional extras differ from project")
        # Use build-tooling packaging (already in the dev toolchain), not a new
        # runtime dependency. Preserve dependency markers and optional extras.
        from packaging.requirements import Requirement
        expected_requirements = set(project["dependencies"])
        for extra, requirements in project["optional-dependencies"].items():
            for requirement in requirements:
                base, separator, marker = requirement.partition(";")
                expected_requirements.add(base + "; " +
                    (f"({marker.strip()}) and " if separator else "") + f'extra == "{extra}"')
        if {str(Requirement(r)) for r in meta.get_all("Requires-Dist", [])} != {
                str(Requirement(r)) for r in expected_requirements}:
            raise ContractError("wheel dependencies differ from project")
        installation_metadata = {name: hashlib.sha256(archive.read(dist_info + name)).hexdigest()
                                 for name in INSTALLATION_METADATA}
    source_sha = canonical_sha256(inventory)
    compatibility = CompatibilityManifest.from_mapping({
        "contract": "graphify.workspace.compatibility", "schema_version": 2,
        "distribution": "graphifyy", "distribution_version": DISTRIBUTION_VERSION,
        "distribution_build": "fixture:sha256:" + source_sha,
        "engine_baseline": ENGINE_BASELINE, "extractor_cache_abi": EXTRACTOR_CACHE_ABI,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION, "state_schema_version": STATE_SCHEMA_VERSION,
        "detector_id": DETECTOR_ID, "graph_payload_version": GRAPH_PAYLOAD_VERSION,
        "input_manifest_version": INPUT_MANIFEST_VERSION,
        "candidate_kind": "local-fixture", "certified": False,
        "source_manifest_sha256": source_sha,
        "wheel_sha256": hashlib.sha256(wheel_bytes).hexdigest(), "package_members": members,
        "installation_metadata": installation_metadata,
    })
    authority = WorkspaceRuntimeAuthority.from_mapping({
        "contract": "graphify.workspace.runtime_authority.internal", "format_version": 2,
        "compatibility_manifest": compatibility.to_dict(), "structural_policy": policy.to_dict(),
    })
    bundle = {"source-manifest.json": canonical_json_bytes(inventory),
              "compatibility.json": compatibility.canonical,
              "runtime-manifest.json": authority.canonical}
    for name in SCHEMA_FILES:
        bundle["schemas/" + name] = _read(repo / "graphify/workspace/schemas" / name)
    for path in sorted((repo / "tests").glob("test_workspace_*.py")):
        bundle["tests/" + path.name] = _read(path)
    # Recheck source after reading wheel/schemas/tests so mixed inputs refuse.
    if source_manifest(repo) != inventory or package_members(repo) != members:
        raise ContractError("candidate changed during fixture collection")
    outer = canonical_json_bytes({"kind": "local-fixture", "certified": False,
                                  "members": {n: hashlib.sha256(b).hexdigest()
                                              for n, b in sorted(bundle.items())}})
    output.mkdir(mode=0o700)
    for name in ("source-manifest.json", "compatibility.json", "runtime-manifest.json"):
        (output / name).write_bytes(bundle[name])
    (output / "fixture-manifest.json").write_bytes(outer)
    with zipfile.ZipFile(output / "fixture-bundle.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(bundle.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 19, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)
    return compatibility
