"""Content-derived local fixture bundles, not release candidate certification.

The input inventory includes tracked and unignored untracked files, so a dirty
S2 diff is identified honestly. The base commit is provenance only. The wheel
must match every intended package member before any fixture output is created.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import csv
import hashlib
import io
import configparser
import os
import secrets
from pathlib import Path
import stat
import subprocess
import tomllib
import zipfile
import zlib

from graphify.workspace.composition import StructuralPolicy, WorkspaceRuntimeAuthority
from graphify.workspace.contracts import (
    ADAPTER_CONTRACT_VERSION, CompatibilityManifest, ContractError, DETECTOR_ID,
    DISTRIBUTION_VERSION, ENGINE_BASELINE, EXTRACTOR_CACHE_ABI, GRAPH_PAYLOAD_VERSION,
    INPUT_MANIFEST_VERSION, INSTALLATION_METADATA, SCHEMA_FILES, STATE_SCHEMA_VERSION,
    MAX_DOCUMENT_BYTES, MAX_ENTRIES, SUPPORTED_CONSOLE_SCRIPTS, canonical_json_bytes, canonical_sha256, input_label,
)


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_WHEEL_MEMBER_BYTES = MAX_INPUT_BYTES
MAX_WHEEL_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_FIXTURE_BUNDLE_BYTES = MAX_WHEEL_EXPANDED_BYTES
MAX_FIXTURE_BUNDLE_MEMBERS = MAX_ENTRIES
MAX_SOURCE_LIST_BYTES = MAX_DOCUMENT_BYTES


def _input_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path):
    return _capture(path)[0]


def _capture(path, *, max_bytes=None):
    """Return bytes and mode-bearing metadata from one validated descriptor."""
    limit = MAX_INPUT_BYTES if max_bytes is None else min(MAX_INPUT_BYTES, max_bytes)
    descriptor = None
    try:
        named = path.lstat()
        if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or named.st_size > limit):
            raise ContractError("fixture input must be a bounded singular regular file")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit):
            raise ContractError("fixture input must be a bounded singular regular file")
        if (_input_identity(named) != _input_identity(before)
                or _input_identity(path.lstat()) != _input_identity(before)):
            raise ContractError("fixture input changed before reading")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ContractError("fixture input exceeds byte limit")
        if (_input_identity(before) != _input_identity(os.fstat(descriptor))
                or _input_identity(before) != _input_identity(path.lstat())
                or size != before.st_size):
            raise ContractError("fixture input changed while reading")
        return b"".join(chunks), before
    except OSError as exc:
        raise ContractError("fixture input cannot be safely read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _source_directory_chain(repo, directory):
    """Bind source ancestors below the resolved checkout, without following links."""
    try:
        parts = directory.relative_to(repo).parts
        current, identities = repo, []
        for part in (None, *parts):
            if part is not None:
                current = current / part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ContractError("fixture source ancestor must be a real directory")
            identities.append((info.st_dev, info.st_ino, info.st_mode))
        return identities
    except (OSError, ValueError) as exc:
        raise ContractError("fixture source directory unavailable or outside checkout") from exc


def _read_source(repo, path, *, max_bytes=None):
    return _capture_source(repo, path, max_bytes=max_bytes)[0]


def _capture_source(repo, path, *, max_bytes=None):
    input_label(path.relative_to(repo).as_posix(), {"source"}, source_file=True)
    ancestors = _source_directory_chain(repo, path.parent)
    captured = _capture(path) if max_bytes is None else _capture(path, max_bytes=max_bytes)
    if _source_directory_chain(repo, path.parent) != ancestors:
        raise ContractError("fixture source ancestor changed while reading")
    return captured


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
    from packaging.utils import canonicalize_name

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
    return expected, {canonicalize_name(extra) for extra in optional}


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


def _project_document(repo, inventory=None):
    """Read one checked project document and optionally bind it to the inventory."""
    payload = _read_source(repo, repo / "pyproject.toml")
    if inventory is not None:
        recorded = inventory["files"].get("pyproject.toml")
        if (not isinstance(recorded, dict)
                or recorded.get("sha256") != hashlib.sha256(payload).hexdigest()):
            raise ContractError("project configuration differs from source inventory")
    try:
        return tomllib.loads(payload.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError("invalid project configuration") from exc


def package_members(repo, *, config=None):
    """Mirror explicit setuptools package/data selection, preserving all host data."""
    repo = Path(repo).resolve(strict=True)
    if config is None:
        try:
            config = _project_document(repo)["tool"]["setuptools"]
        except (KeyError, TypeError) as exc:
            raise ContractError("invalid setuptools package selection") from exc
    if type(config) is not dict:
        raise ContractError("invalid setuptools package selection")
    # This model handles only explicit packages and package-data globs. Other
    # selectors can change wheel contents without changing these two fields.
    if set(config) - {"packages", "package-data", "include-package-data"}:
        raise ContractError("unsupported setuptools package selection")
    packages = config.get("packages")
    package_data = config.get("package-data")
    if (type(packages) is not list or not packages
            or not all(type(package) is str and package
                       and all(part.isidentifier() for part in package.split("."))
                       for package in packages)
            or len(packages) != len(set(packages))
            or type(package_data) is not dict
            or not set(package_data).issubset(packages)):
        raise ContractError("invalid setuptools package selection")
    for package, patterns in package_data.items():
        if (type(package) is not str or type(patterns) is not list
                or not all(type(pattern) is str and pattern
                           and not Path(pattern).is_absolute()
                           and ".." not in Path(pattern).parts
                           for pattern in patterns)):
            raise ContractError("invalid setuptools package selection")
    # Setuptools defaults this to true for pyproject.toml, admitting data from
    # MANIFEST.in and plugins that this explicit member model does not expand.
    if config.get("include-package-data") is not False:
        raise ContractError("unsupported setuptools package selection: include-package-data must be explicitly false")
    members = set()
    for package in packages:
        directory = repo.joinpath(*package.split("."))
        _source_directory_chain(repo, directory)
        members.update(directory.glob("*.py"))
        # Setuptools includes these package-local typing files even when
        # include-package-data is false. Its glob skips dotfiles and directories.
        members.update(path for pattern in ("*.pyi", "py.typed")
                       for path in directory.glob(pattern)
                       if not path.name.startswith(".") and path.is_file())
    for package, patterns in package_data.items():
        directory = repo.joinpath(*package.split("."))
        for pattern in patterns:
            matches = set(directory.glob(pattern))
            if not matches:
                raise ContractError(f"empty package-data pattern: {pattern}")
            members.update(matches)
    return {p.relative_to(repo).as_posix(): hashlib.sha256(_read_source(repo, p)).hexdigest()
            for p in sorted(members)}


@contextmanager
def _wheel_archive(payload):
    """Translate archive corruption/decompression failures to the contract boundary."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            yield archive
    except (OSError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile,
            zipfile.LargeZipFile, zlib.error) as exc:
        raise ContractError("wheel archive cannot be safely read") from exc


def _source_names(repo):
    """Collect a bounded Git inventory before reading any source payload."""
    command = ["git", "-C", str(repo), "ls-files", "-z", "--cached",
               "--others", "--exclude-standard"]
    names, pending, size = set(), b"", 0
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(min(65536, MAX_SOURCE_LIST_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SOURCE_LIST_BYTES:
                    raise ContractError("source inventory byte limit exceeded")
                parts = (pending + chunk).split(b"\0")
                pending = parts.pop()
                for raw in parts:
                    try:
                        name = raw.decode("utf-8")
                    except UnicodeError as exc:
                        raise ContractError("source inventory paths must be UTF-8") from exc
                    input_label(name, {"source"}, source_file=True)
                    names.add(name)
                    if len(names) > MAX_ENTRIES:
                        raise ContractError("source inventory entry limit exceeded")
                if len(pending) > 4096:
                    raise ContractError("source inventory path byte limit exceeded")
            if pending:
                raise ContractError("unterminated source inventory path")
            if process.wait() != 0:
                raise ContractError("cannot enumerate source inventory")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return sorted(names)


def source_manifest(repo):
    repo = repo.resolve(strict=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                    ENGINE_BASELINE.removeprefix("git:"), head], check=True, capture_output=True)
    names = _source_names(repo)
    files = {}
    for name in names:
        input_label(name, {"source"}, source_file=True)
        path = repo / name
        # Explicit deletion is part of this local candidate; never silently skip it.
        if not path.exists() and not path.is_symlink():
            files[name] = None
        else:
            payload, info = _capture_source(repo, path)
            files[name] = {"sha256": hashlib.sha256(payload).hexdigest(),
                           "mode": stat.S_IMODE(info.st_mode)}
    return {"kind": "local-fixture", "base_commit": head, "files": files}


def _validate_build_inputs(repo, document):
    """Admit the declared backend surface modeled by this fixture builder."""
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

    build = document.get("build-system")
    if (type(build) is not dict or set(build) != {"requires", "build-backend"}
            or build["build-backend"] != "setuptools.build_meta"
            or type(build["requires"]) is not list or not build["requires"]):
        raise ContractError("unsupported build-system configuration")
    for raw in build["requires"]:
        try:
            if type(raw) is not str:
                raise TypeError("build requirement must be a string")
            requirement = Requirement(raw)
        except (InvalidRequirement, TypeError) as exc:
            raise ContractError("invalid build requirement") from exc
        if (canonicalize_name(requirement.name) != "setuptools"
                or requirement.extras or requirement.url or requirement.marker):
            raise ContractError("unsupported build requirement")
    project = document.get("project")
    if (type(project) is not dict
            or project.get("dynamic", []) != []
            or project.get("gui-scripts", {}) != {}
            or project.get("entry-points", {}) != {}):
        raise ContractError("unsupported dynamic project or entry-point configuration")
    for name in ("setup.py", "setup.cfg"):
        try:
            (repo / name).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ContractError("build configuration absence cannot be verified") from exc
        raise ContractError(f"unsupported build configuration: {name}")


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
    document = _project_document(repo, inventory)
    _validate_build_inputs(repo, document)
    try:
        package_config = document["tool"]["setuptools"]
        project = document["project"]
    except (KeyError, TypeError) as exc:
        raise ContractError("invalid project configuration") from exc
    members = package_members(repo, config=package_config)
    expected_requirements, expected_extras = _project_requirements(project)
    scripts = project.get("scripts")
    if (type(scripts) is not dict or set(scripts) != SUPPORTED_CONSOLE_SCRIPTS
            or not all(type(value) is str and value for value in scripts.values())):
        raise ContractError("project console scripts outside supported set")
    _validate_wheel_filename(wheel, project)
    wheel_bytes = _read(wheel)
    with _wheel_archive(wheel_bytes) as archive:
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
        entry_points.optionxform = lambda optionstr: optionstr
        try:
            entry_points.read_string(read_member(dist_info + "entry_points.txt").decode("utf-8"))
            if (entry_points.sections() != ["console_scripts"]
                    or dict(entry_points["console_scripts"]) != scripts):
                raise ContractError("wheel entry points differ from project")
        except (configparser.Error, UnicodeError) as exc:
            raise ContractError("invalid wheel entry points") from exc
        try:
            top_level = read_member(dist_info + "top_level.txt").decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise ContractError("invalid wheel top-level package metadata") from exc
        expected_top_level = {package.split(".")[0] for package in package_config["packages"]}
        if len(top_level) != len(expected_top_level) or set(top_level) != expected_top_level:
            raise ContractError("wheel top-level package metadata differs from project")
        if read_member(dist_info + "licenses/LICENSE") != _read_source(repo, repo / "LICENSE"):
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
    workspace_tests = _collect_bundle_sources(repo, bundle)
    # Recheck source after reading wheel/schemas/tests so mixed inputs refuse.
    if (source_manifest(repo) != inventory
            or package_members(repo, config=package_config) != members
            or sorted((repo / "tests").glob("test_workspace_*.py")) != workspace_tests
            or any(_read_source(repo, path, max_bytes=len(bundle["tests/" + path.name]))
                   != bundle["tests/" + path.name]
                   for path in workspace_tests)):
        raise ContractError("candidate changed during fixture collection")
    outer = canonical_json_bytes({"kind": "local-fixture", "certified": False,
                                  "members": {n: hashlib.sha256(b).hexdigest()
                                              for n, b in sorted(bundle.items())}})
    _validate_build_inputs(repo, _project_document(repo, inventory))
    _write_fixture(output, bundle, outer, repo=repo)
    return compatibility


def _collect_bundle_sources(repo, bundle):
    """Bound retained schemas/tests and preflight sizes before opening payloads."""
    sources = [("schemas/" + name, repo / "graphify/workspace/schemas" / name)
               for name in SCHEMA_FILES]
    tests = []
    for path in (repo / "tests").glob("test_workspace_*.py"):
        if len(bundle) + len(sources) + len(tests) >= MAX_FIXTURE_BUNDLE_MEMBERS:
            raise ContractError("fixture bundle member count exceeds limit")
        tests.append(path)
    tests.sort()
    sources.extend(("tests/" + path.name, path) for path in tests)
    if len(bundle) + len(sources) > MAX_FIXTURE_BUNDLE_MEMBERS:
        raise ContractError("fixture bundle member count exceeds limit")
    retained = sum(len(payload) for payload in bundle.values())
    declared = retained
    try:
        for _, path in sources:
            size = path.lstat().st_size
            if size > MAX_INPUT_BYTES:
                raise ContractError("fixture input exceeds byte limit")
            declared += size
            if declared > MAX_FIXTURE_BUNDLE_BYTES:
                raise ContractError("fixture bundle aggregate byte limit exceeded")
    except OSError as exc:
        raise ContractError("fixture bundle source unavailable") from exc
    if declared > MAX_FIXTURE_BUNDLE_BYTES:
        raise ContractError("fixture bundle aggregate byte limit exceeded")
    for name, path in sources:
        payload = _read_source(repo, path, max_bytes=MAX_FIXTURE_BUNDLE_BYTES - retained)
        retained += len(payload)
        if retained > MAX_FIXTURE_BUNDLE_BYTES:
            raise ContractError("fixture bundle aggregate byte limit exceeded")
        bundle[name] = payload
    return tests


def _write_fixture_members(open_member, bundle, outer):
    for name in ("source-manifest.json", "compatibility.json", "runtime-manifest.json"):
        with open_member(name) as stream:
            stream.write(bundle[name])
    with open_member("fixture-manifest.json") as stream:
        stream.write(outer)
    with open_member("fixture-bundle.zip") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(bundle.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 9, 19, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, payload)


def _entry_identity(directory_fd, name):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


class _FixtureMemberWriter:
    """Hash intended writes independently of disk; ZIP uses data descriptors."""
    def __init__(self, stream, identity):
        self.stream = stream
        self.identity = identity
        self.size = 0
        self.digest = hashlib.sha256()
        self.completed: tuple[int, ...] | None = None

    def write(self, payload):
        written = self.stream.write(payload)
        self.digest.update(payload[:written])
        self.size += written
        if written != len(payload):
            raise OSError("short fixture member write")
        return written

    def tell(self):
        return self.size

    def flush(self):
        self.stream.flush()


def _verify_fixture_member(stage_fd, name, member):
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=stage_fd)
        before = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino) != member.identity
                or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size != member.size
                or (member.completed is not None and _input_identity(before) != member.completed)):
            raise ContractError("fixture member identity or policy changed")
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, min(65536, member.size + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > member.size:
                raise ContractError("fixture member grew while reading")
            digest.update(chunk)
        if (size != member.size or digest.digest() != member.digest.digest()
                or _input_identity(before) != _input_identity(os.fstat(descriptor))
                or _input_identity(before) != _input_identity(os.stat(
                    name, dir_fd=stage_fd, follow_symlinks=False))):
            raise ContractError("fixture member contents changed")
        return before
    except OSError as exc:
        raise ContractError("fixture member cannot be safely verified") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_fixture_members(stage_fd, members):
    expected = {"source-manifest.json", "compatibility.json", "runtime-manifest.json",
                "fixture-manifest.json", "fixture-bundle.zip"}
    stage = os.fstat(stage_fd)
    if stage.st_uid != os.geteuid() or stat.S_IMODE(stage.st_mode) != 0o700:
        raise ContractError("fixture staging directory policy changed")
    if set(members) != expected or set(os.listdir(stage_fd)) != expected:
        raise ContractError("fixture member namespace changed")
    for name, member in members.items():
        if member.completed is None:
            raise ContractError("fixture member was not completed")
        _verify_fixture_member(stage_fd, name, member)
    if set(os.listdir(stage_fd)) != expected:
        raise ContractError("fixture member namespace changed during verification")


def _cleanup_fixture_stage(parent, stage, stage_fd, identity, members):
    # Never traverse a mutable stage name, nor delete files not created here.
    if _entry_identity(parent.fd, stage) != identity:
        return
    for name, member in members.items():
        try:
            _verify_fixture_member(stage_fd, name, member)
        except ContractError:
            continue
        os.unlink(name, dir_fd=stage_fd)
    if _entry_identity(parent.fd, stage) == identity and not os.listdir(stage_fd):
        os.rmdir(stage, dir_fd=parent.fd)


def _write_fixture(output, bundle, outer, *, repo=None):
    from graphify.transaction import pin_output
    with pin_output(output.parent, create=False) as parent:
        _validate_fixture_parent(output, parent, repo=repo)
        stage = ".graphify-fixture-" + secrets.token_hex(16)
        os.mkdir(stage, mode=0o700, dir_fd=parent.fd)
        created_identity = _entry_identity(parent.fd, stage)
        stage_fd, identity, members = None, None, {}
        admitted = False
        try:
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
            opened = os.fstat(stage_fd)
            identity = (opened.st_dev, opened.st_ino)
            if (identity != created_identity or _entry_identity(parent.fd, stage) != identity
                    or opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700):
                raise ContractError("fixture staging directory must remain owned and private")
            admitted = True

            intended = {name: bundle[name] for name in (
                "source-manifest.json", "compatibility.json", "runtime-manifest.json")}
            intended["fixture-manifest.json"] = outer

            @contextmanager
            def open_member(name):
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=stage_fd)
                opened_file = os.fstat(fd)
                with os.fdopen(fd, "wb") as stream:
                    member = _FixtureMemberWriter(stream, (opened_file.st_dev, opened_file.st_ino))
                    members[name] = member
                    completed = False
                    try:
                        yield member
                        completed = True
                    finally:
                        member.flush()
                        info = _verify_fixture_member(stage_fd, name, member)
                        if completed and name in intended and (
                                member.size != len(intended[name])
                                or member.digest.digest() != hashlib.sha256(intended[name]).digest()):
                            raise ContractError("fixture member differs from intended bytes")
                        member.completed = _input_identity(info)

            _write_fixture_members(open_member, bundle, outer)
            _publish_fixture(output.parent / stage, output, parent=parent,
                             expected_identity=identity, stage_fd=stage_fd, members=members)
            _verify_fixture_members(stage_fd, members)
            if _entry_identity(parent.fd, output.name) != identity:
                raise ContractError("published fixture directory identity changed")
            _validate_fixture_parent(output, parent)
        finally:
            if stage_fd is not None:
                try:
                    if admitted:
                        _cleanup_fixture_stage(parent, stage, stage_fd, identity, members)
                finally:
                    os.close(stage_fd)


def _validate_fixture_parent(output, parent, *, repo=None):
    parent.validate()
    if output.parent.resolve(strict=True) != parent.path:
        raise ContractError("fixture output parent changed")
    if repo is not None and parent.path.is_relative_to(repo):
        raise ContractError("fixture output must be outside source checkout")


def _publish_fixture(payload_root, output, *, parent=None, expected_identity=None,
                     stage_fd=None, members=None):
    # Tooling-only import: preserve the workspace composition cold boundary.
    from graphify.transaction import _atomic_rename_no_replace, pin_output
    if parent is None:
        with pin_output(output.parent, create=False) as pinned:
            _publish_fixture(payload_root, output, parent=pinned, expected_identity=expected_identity,
                             stage_fd=stage_fd, members=members)
        return
    _validate_fixture_parent(output, parent)
    source = str(payload_root.relative_to(output.parent))
    current = _entry_identity(parent.fd, source)
    expected_identity = current if expected_identity is None else expected_identity
    if expected_identity is None or current != expected_identity:
        raise ContractError("fixture staging directory identity changed")
    if stage_fd is not None:
        _verify_fixture_members(stage_fd, members)
    _atomic_rename_no_replace(parent, source, output.name)
    if _entry_identity(parent.fd, output.name) != expected_identity:
        raise ContractError("published fixture directory identity changed")
    # Refuse detached-parent success while preserving any published artifact.
    _validate_fixture_parent(output, parent)
