"""Content-derived local fixture bundles, not release candidate certification.

The input inventory includes tracked and unignored untracked files, so a dirty
S2 diff is identified honestly. The base commit is provenance only. The wheel
must match every intended package member before any fixture output is created.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import configparser
import os
import tempfile
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


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_WHEEL_MEMBER_BYTES = MAX_INPUT_BYTES
MAX_WHEEL_EXPANDED_BYTES = 256 * 1024 * 1024


def _input_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path):
    descriptor = None
    try:
        named = path.lstat()
        if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or named.st_size > MAX_INPUT_BYTES):
            raise ContractError("fixture input must be a bounded singular regular file")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_INPUT_BYTES):
            raise ContractError("fixture input must be a bounded singular regular file")
        if (_input_identity(named) != _input_identity(before)
                or _input_identity(path.lstat()) != _input_identity(before)):
            raise ContractError("fixture input changed before reading")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_INPUT_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_INPUT_BYTES:
                raise ContractError("fixture input exceeds byte limit")
        if (_input_identity(before) != _input_identity(os.fstat(descriptor))
                or _input_identity(before) != _input_identity(path.lstat())
                or size != before.st_size):
            raise ContractError("fixture input changed while reading")
        return b"".join(chunks)
    except OSError as exc:
        raise ContractError("fixture input cannot be safely read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_wheel_metadata(payload):
    """Require the supported pure-Python wheel envelope before fixture identity."""
    from email import policy
    from email.parser import BytesParser

    wheel = BytesParser(policy=policy.compat32).parsebytes(payload)
    if wheel.defects:
        raise ContractError("malformed WHEEL metadata")

    def one(name):
        values = wheel.get_all(name, [])
        if len(values) != 1:
            raise ContractError(f"WHEEL metadata requires one {name}")
        return values[0].strip()

    if one("Wheel-Version") != "1.0":
        raise ContractError("unsupported wheel metadata version")
    if one("Root-Is-Purelib").lower() != "true":
        raise ContractError("wheel must install as pure Python")
    if [value.strip() for value in wheel.get_all("Tag", [])] != ["py3-none-any"]:
        raise ContractError("unsupported wheel compatibility tag")


def _validate_core_metadata(payload, project):
    """Validate complete core metadata and bind it to the captured project."""
    from packaging.metadata import Metadata
    from packaging.specifiers import SpecifierSet
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    try:
        metadata = Metadata.from_email(payload, validate=True)
        project_name = canonicalize_name(project["name"])
        project_version = Version(project["version"])
        project_python = SpecifierSet(project["requires-python"])
    except Exception as exc:
        raise ContractError("invalid project or core metadata") from exc
    if project_name != canonicalize_name("graphifyy") or project_version != Version(DISTRIBUTION_VERSION):
        raise ContractError("project distribution identity outside compatibility contract")
    if (canonicalize_name(metadata.name) != project_name
            or metadata.version != project_version
            or metadata.requires_python != project_python):
        raise ContractError("wheel distribution identity differs from project")
    return metadata


def _validate_wheel_filename(path, project):
    """Bind the installer-facing wheel name to the captured project identity."""
    from packaging.tags import Tag
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version

    try:
        name, version, build, tags = parse_wheel_filename(path.name)
        project_name = canonicalize_name(project["name"])
        project_version = Version(project["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("invalid wheel filename or project identity") from exc
    if (name != project_name or version != project_version or build
            or tags != {Tag("py3", "none", "any")}):
        raise ContractError("wheel filename differs from supported project identity")


def _project_requirements(project):
    """Validate project dependency containers and return canonical requirements."""
    from packaging.requirements import InvalidRequirement, Requirement

    try:
        dependencies = project["dependencies"]
        optional = project["optional-dependencies"]
        if (type(dependencies) is not list
                or not all(type(requirement) is str for requirement in dependencies)
                or type(optional) is not dict
                or not all(type(extra) is str and type(requirements) is list
                           and all(type(requirement) is str for requirement in requirements)
                           for extra, requirements in optional.items())):
            raise TypeError("dependency declarations must contain strings")
        expected = {str(Requirement(requirement)) for requirement in dependencies}
        for extra, requirements in optional.items():
            for requirement in requirements:
                base, separator, marker = requirement.partition(";")
                combined = (base + "; "
                            + (f"({marker.strip()}) and " if separator else "")
                            + f'extra == "{extra}"')
                expected.add(str(Requirement(combined)))
    except (KeyError, TypeError, AttributeError, InvalidRequirement) as exc:
        raise ContractError("invalid project dependency declarations") from exc
    return expected, set(optional)


def _validate_wheel_record(payload, names, record_name, read_member):
    """Require RECORD to describe the accepted archive exactly and correctly."""
    try:
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8"), newline=""), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ContractError("malformed wheel RECORD") from exc
    if (any(len(row) != 3 for row in rows) or len(rows) != len(names)
            or len({row[0] for row in rows}) != len(rows)
            or {row[0] for row in rows} != set(names)):
        raise ContractError("wheel RECORD namespace differs from archive")
    for path, recorded_hash, recorded_size in rows:
        if path == record_name:
            if recorded_hash or recorded_size:
                raise ContractError("wheel RECORD must omit its own hash and size")
            continue
        member = read_member(path)
        expected_hash = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(member).digest()
        ).rstrip(b"=").decode("ascii")
        if recorded_hash != expected_hash or recorded_size != str(len(member)):
            raise ContractError("wheel RECORD member identity mismatch")


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
    if type(policy) is not StructuralPolicy:
        raise ContractError("explicit fixture policy is required")
    inventory = source_manifest(repo)
    members = package_members(repo)
    project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]
    expected_requirements, expected_extras = _project_requirements(project)
    _validate_wheel_filename(wheel, project)
    wheel_bytes = _read(wheel)
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        dist_info = f"graphifyy-{DISTRIBUTION_VERSION}.dist-info/"
        allowed = set(members) | {dist_info + name for name in (
            "METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD", "licenses/LICENSE")}
        if len(names) != len(set(names)):
            raise ContractError("duplicate wheel members")
        if len(names) != len(allowed) or set(names) != allowed:
            raise ContractError("unexpected whole-wheel members")
        # Reject namespace, count, type, and declared expansion before opening
        # any compressed member, including metadata that is not otherwise used.
        expanded = 0
        for info in infos:
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                raise ContractError("wheel contains a non-regular member")
            if not 0 <= info.file_size <= MAX_WHEEL_MEMBER_BYTES:
                raise ContractError("wheel member exceeds expanded byte limit")
            expanded += info.file_size
            if expanded > MAX_WHEEL_EXPANDED_BYTES:
                raise ContractError("wheel exceeds total expanded byte limit")

        def read_member(name):
            info = archive.getinfo(name)
            with archive.open(info) as stream:
                payload = stream.read(min(info.file_size, MAX_WHEEL_MEMBER_BYTES) + 1)
            if len(payload) != info.file_size:
                raise ContractError("wheel expanded member size mismatch")
            return payload

        actual = {name: hashlib.sha256(read_member(name)).hexdigest() for name in members}
        if actual != members:
            raise ContractError("wheel does not match the complete candidate package")
        record_name = dist_info + "RECORD"
        _validate_wheel_record(read_member(record_name), names, record_name, read_member)
        entry_points = configparser.ConfigParser()
        entry_points.read_string(read_member(dist_info + "entry_points.txt").decode())
        if (entry_points.sections() != ["console_scripts"]
                or dict(entry_points["console_scripts"]) != project["scripts"]):
            raise ContractError("wheel entry points differ from project")
        if read_member(dist_info + "licenses/LICENSE") != _read(repo / "LICENSE"):
            raise ContractError("wheel license differs from project")
        _validate_wheel_metadata(read_member(dist_info + "WHEEL"))
        metadata_name = dist_info + "METADATA"
        meta = _validate_core_metadata(read_member(metadata_name), project)
        if set(meta.provides_extra or []) != expected_extras:
            raise ContractError("wheel optional extras differ from project")
        if {str(requirement) for requirement in (meta.requires_dist or [])} != expected_requirements:
            raise ContractError("wheel dependencies differ from project")
        installation_metadata = {name: hashlib.sha256(read_member(dist_info + name)).hexdigest()
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
    _write_fixture(output, bundle, outer)
    return compatibility


def _write_fixture(output, bundle, outer):
    # Keep cleanup private: no failure path recursively removes the public name.
    # Publication never replaces a destination that appeared after preflight.
    with tempfile.TemporaryDirectory(prefix=".graphify-fixture-", dir=output.parent) as temporary:
        payload_root = Path(temporary) / "payload"
        payload_root.mkdir(mode=0o700)
        for name in ("source-manifest.json", "compatibility.json", "runtime-manifest.json"):
            (payload_root / name).write_bytes(bundle[name])
        (payload_root / "fixture-manifest.json").write_bytes(outer)
        with zipfile.ZipFile(payload_root / "fixture-bundle.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(bundle.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 9, 19, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, payload)
        _publish_fixture(payload_root, output)


def _publish_fixture(payload_root, output):
    if os.name == "nt":
        # Windows rename refuses an existing destination, including directories.
        os.rename(payload_root, output)
    else:
        # Tooling-only import: preserve the workspace composition cold boundary.
        from graphify.transaction import _atomic_rename_no_replace, pin_output
        with pin_output(output.parent, create=False) as parent:
            parent.validate()
            _atomic_rename_no_replace(parent, str(payload_root.relative_to(output.parent)), output.name)
            # A rename through the pinned descriptor can succeed after its
            # directory was detached. Refuse success, preserving that artifact.
            parent.validate()
