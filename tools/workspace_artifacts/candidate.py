"""Reproducible workspace candidate build and isolated-home proof orchestration."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import errno
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
# Fixed argv only; shell execution is never used.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import tomllib
from typing import Iterator, Mapping, cast
from urllib.parse import urlparse
from urllib.request import Request, url2pathname, urlopen
import uuid
import zipfile

from graphify.workspace import (
    ADAPTER_CONTRACT_VERSION,
    ArtifactManifest,
    CANDIDATE_DISTRIBUTION_VERSION,
    CLI_CONTRACT_VERSION,
    ENGINE_BASELINE,
    EXTRACTOR_CACHE_ABI,
    RUNTIME_AUTHORITY_FILENAME,
    STATE_SCHEMA_VERSION,
    UPSTREAM_BASELINE_COMMIT,
    CompatibilityManifest,
    ContractError,
    SemanticQueuePolicy,
    WorkspaceRuntimeAuthority,
    canonical_json_bytes,
    canonical_sha256,
    load_workspace_runtime_inputs,
)
from graphify.workspace.persistence import (
    CommitUnknown,
    DurableStateRoot,
    InjectedFault,
    PosixSyscalls,
    RuntimeCapabilities,
    StateCorrupt,
)
from tools.workspace_artifacts import (
    ArtifactError,
    FIXED_SOURCE_EPOCH,
    build_static_bundles,
    prove_independent_tamper_rejection,
    run_disposable_compensation_proof,
    sha256_file,
    snapshot_disposable_home,
    strict_tree_sha256,
    verify_trusted_manifest,
    write_proof,
    write_trusted_manifest,
)


UPSTREAM_COMMIT = UPSTREAM_BASELINE_COMMIT
UPSTREAM_TAG_OBJECT = "1ff8eeafc6ad18834bd4986558206b7deec188b9"
WHEEL_NAME = f"graphifyy-{CANDIDATE_DISTRIBUTION_VERSION}-py3-none-any.whl"
UPSTREAM_WHEEL_NAME = "graphifyy-0.9.16-py3-none-any.whl"
UPSTREAM_WHEEL_SHA256 = "24eefd6cd8e0f47eb8167671fbe3aceb31b49a6508b91fe1b60c4fd1978e32bc"
UPSTREAM_PYPI_METADATA_URL = "https://pypi.org/pypi/graphifyy/0.9.16/json"
CONTROLLED_UPSTREAM_INDEX = "https://pypi.org/simple"
CANDIDATE_UV_VERSION = "0.11.30"

# P5C1 isolated-proof authority only. These values are not a production default,
# publication authority, performance qualification, or public configuration.
_P5C1_ISOLATED_PROOF_QUEUE_POLICY = SemanticQueuePolicy(
    max_items=8,
    max_bytes=16_384,
    retry_budget=1,
)

_PACKAGE_SOURCE_ENVIRONMENT = {
    "PIP_CONFIG_FILE",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_INDEX_URL",
    "PIP_NO_INDEX",
    "PIP_TRUSTED_HOST",
    "UV_CONFIG_FILE",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_INDEX",
    "UV_INDEX_STRATEGY",
    "UV_INDEX_URL",
    "UV_NO_INDEX",
    "UV_OFFLINE",
}

_PRIOR_FILES = {
    "binary": b"#!/bin/sh\necho graphify 0.9.16\n",
    "runtime": b"graphifyy==0.9.16\n",
    "skill": b"# prior Graphify skill\n",
    "service": b"prior-login-service-fixture\n",
}
_CANARY = b'{"generation":"gen-canary","immutable":true}\n'


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    umask: int | None = None,
) -> subprocess.CompletedProcess[str]:
    # Arguments are explicit and subprocess never invokes a shell.
    result = subprocess.run(  # nosec B603
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        umask=-1 if umask is None else umask,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-4000:]
        raise ArtifactError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def _git(repo_root: Path, *arguments: str) -> str:
    return _run([_git_executable(), *arguments], cwd=repo_root).stdout.strip()


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ArtifactError("git is required for candidate artifact generation")
    return executable


def _assert_candidate_source(repo_root: Path) -> tuple[str, str]:
    if not repo_root.is_absolute() or not (repo_root / ".git").exists():
        raise ArtifactError(f"repo_root is not an absolute Git checkout: {repo_root}")
    head = _git(repo_root, "rev-parse", "HEAD")
    tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    git = _git_executable()
    for command in ([git, "diff", "--quiet"], [git, "diff", "--cached", "--quiet"]):
        # Fixed Git argv, with shell=False.
        result = subprocess.run(command, cwd=repo_root)  # nosec B603
        if result.returncode != 0:
            raise ArtifactError("candidate build requires a committed tracked tree")
    # Fixed Git argv with validated commit IDs.
    ancestor = subprocess.run(  # nosec B603
        [git, "merge-base", "--is-ancestor", UPSTREAM_COMMIT, head],
        cwd=repo_root,
    )
    if ancestor.returncode != 0:
        raise ArtifactError(f"candidate HEAD {head} is not based on exact {UPSTREAM_COMMIT}")
    epoch = int(_git(repo_root, "show", "-s", "--format=%ct", UPSTREAM_COMMIT))
    if epoch != FIXED_SOURCE_EPOCH:
        raise ArtifactError(
            f"fixed build epoch drift: expected {FIXED_SOURCE_EPOCH}, baseline reports {epoch}"
        )
    return head, tree


def _assert_safe_output_root(repo_root: Path, output_root: Path) -> None:
    try:
        relative = output_root.relative_to(repo_root)
    except ValueError:
        return
    if not relative.parts:
        raise ArtifactError("candidate output root cannot be the repository root")
    # Fixed Git argv, with shell=False.
    ignored = subprocess.run(  # nosec B603
        [_git_executable(), "check-ignore", "--quiet", "--", relative.as_posix()],
        cwd=repo_root,
    )
    if ignored.returncode != 0:
        raise ArtifactError("candidate output root must be external or Git-ignored")


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _p5c1_runtime_authority(
    compatibility_manifest: CompatibilityManifest,
) -> WorkspaceRuntimeAuthority:
    """Construct the explicit P5C1 isolated-proof runtime authority."""

    return WorkspaceRuntimeAuthority(
        compatibility_manifest=compatibility_manifest,
        semantic_queue_policy=_P5C1_ISOLATED_PROOF_QUEUE_POLICY,
    )


def _trusted_artifact_sha256(*, trusted_manifest: bytes, name: str) -> str:
    try:
        document = ArtifactManifest.from_json(trusted_manifest)
    except ContractError as exc:
        raise ArtifactError(f"trusted manifest is invalid: {exc}") from exc
    matches = [entry for entry in document.to_dict()["artifacts"] if entry["path"] == name]
    if len(matches) != 1:
        raise ArtifactError(f"trusted manifest must cover exactly one {name}")
    entry = matches[0]
    if entry["mode"] != "0644":
        raise ArtifactError(f"candidate {name} must be frozen with mode 0644")
    return str(entry["sha256"])


def _validate_candidate_runtime_authority(
    *,
    artifact_root: Path,
    trusted_manifest: bytes,
    expected_sha256: str | None = None,
) -> tuple[WorkspaceRuntimeAuthority, bytes, str]:
    """Verify candidate trust, digest binding, and strict authority round-trip."""

    verify_trusted_manifest(
        artifact_root=artifact_root,
        trusted_manifest=trusted_manifest,
    )
    trusted_sha256 = _trusted_artifact_sha256(
        trusted_manifest=trusted_manifest,
        name=RUNTIME_AUTHORITY_FILENAME,
    )
    if expected_sha256 is not None and expected_sha256 != trusted_sha256:
        raise ArtifactError("expected runtime authority digest is not candidate-trusted")
    try:
        payload = (artifact_root / RUNTIME_AUTHORITY_FILENAME).read_bytes()
        compatibility = CompatibilityManifest.from_json(
            (artifact_root / "compatibility.json").read_bytes()
        )
        authority = WorkspaceRuntimeAuthority.from_json(payload)
    except (OSError, ContractError, RuntimeError) as exc:
        raise ArtifactError(f"candidate runtime authority is invalid: {exc}") from exc
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != trusted_sha256:
        raise ArtifactError("candidate runtime authority digest does not match trusted manifest")
    if authority.compatibility_manifest.canonical != compatibility.canonical:
        raise ArtifactError("candidate runtime authority compatibility binding is invalid")
    if authority.semantic_queue_policy.canonical != _P5C1_ISOLATED_PROOF_QUEUE_POLICY.canonical:
        raise ArtifactError("candidate runtime authority proof policy is invalid")
    return authority, payload, actual_sha256


def _extract_git_archive(archive_bytes: bytes, destination: Path) -> None:
    seen: set[str] = set()
    with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:") as source:
        for member in source.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in member.name:
                raise ArtifactError(f"unsafe path in Git archive: {member.name}")
            if member.name in seen:
                raise ArtifactError(f"duplicate path in Git archive: {member.name}")
            seen.add(member.name)
            target = destination.joinpath(*pure.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ArtifactError(f"unsupported Git archive member: {member.name}")
            extracted = source.extractfile(member)
            if extracted is None:
                raise ArtifactError(f"cannot read Git archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def _extract_head(repo_root: Path, destination: Path) -> None:
    # Fixed Git argv, with shell=False.
    archive = subprocess.run(  # nosec B603
        [_git_executable(), "archive", "--format=tar", "HEAD"],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    _extract_git_archive(archive, destination)


def _build_wheel_once(repo_root: Path, destination: Path) -> Path:
    source = destination / "source"
    output = destination / "wheel"
    source.mkdir(parents=True)
    output.mkdir()
    _extract_head(repo_root, source)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(FIXED_SOURCE_EPOCH),
            "TZ": "UTC",
        }
    )
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
        ],
        cwd=source,
        env=env,
        umask=0o022,
    )
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != WHEEL_NAME:
        raise ArtifactError(f"expected one {WHEEL_NAME}, found {[path.name for path in wheels]}")
    return wheels[0]


def _build_reproducible_wheel(repo_root: Path, destination: Path) -> Path:
    with (
        tempfile.TemporaryDirectory(prefix="graphify-wheel-one-") as first_raw,
        tempfile.TemporaryDirectory(prefix="graphify-wheel-two-") as second_raw,
    ):
        first = _build_wheel_once(repo_root, Path(first_raw))
        second = _build_wheel_once(repo_root, Path(second_raw))
        if first.read_bytes() != second.read_bytes():
            raise ArtifactError("two fixed-epoch clean wheel builds were not byte-identical")
        destination.write_bytes(first.read_bytes())
    with zipfile.ZipFile(destination) as wheel:
        names = set(wheel.namelist())
    required = {
        "graphify/workspace/__init__.py",
        "graphify/workspace/contracts.py",
        "graphify/workspace/freshness.py",
        "graphify/workspace/gc.py",
        "graphify/workspace/generations.py",
        "graphify/workspace/identity.py",
        "graphify/workspace/journal.py",
        "graphify/workspace/leases.py",
        "graphify/workspace/persistence.py",
        "graphify/workspace/pointers.py",
        "graphify/workspace/registry.py",
        "graphify/workspace/rollback.py",
        "graphify/workspace/adapters/__init__.py",
        "graphify/workspace/adapters/base.py",
        "graphify/workspace/adapters/v0_9_16.py",
        "graphify/workspace/schemas/v1/common.schema.json",
        "graphify/workspace/schemas/cli/v1/rollback-request.schema.json",
        "graphify/workspace/schemas/cli/v1/rollback-receipt.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-preview-request.schema.json",
        "graphify/workspace/schemas/cli/v1/gc-preview-result.schema.json",
    }
    missing = required - names
    if missing:
        raise ArtifactError(f"candidate wheel omits workspace package data: {sorted(missing)}")
    return destination


def _render_codex_skill(source_root: Path) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    command = [
        sys.executable,
        "-m",
        "tools.skillgen",
        "--platform",
        "codex",
    ]
    _run([*command, "--check"], cwd=source_root, env=env)
    _run(command, cwd=source_root, env=env)


def _uv() -> str:
    executable = shutil.which("uv")
    if not executable:
        raise ArtifactError("uv is required for candidate artifact generation")
    reported = _run([executable, "--version"], cwd=Path.cwd()).stdout.strip()
    fields = reported.split()
    if len(fields) < 2 or fields[0] != "uv" or fields[1] != CANDIDATE_UV_VERSION:
        raise ArtifactError(
            f"candidate artifact generation requires uv {CANDIDATE_UV_VERSION}; "
            f"found {reported or 'unknown version'}"
        )
    return executable


def _normalize_cyclonedx(document: dict[str, object], runtime_lock_sha256: str) -> bytes:
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ArtifactError("uv CycloneDX SBOM omits object metadata")
    fixed_timestamp = datetime.fromtimestamp(
        FIXED_SOURCE_EPOCH,
        tz=timezone.utc,
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    metadata["timestamp"] = fixed_timestamp
    document["serialNumber"] = "urn:uuid:" + str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"graphifyy:{CANDIDATE_DISTRIBUTION_VERSION}:{runtime_lock_sha256}",
        )
    )
    return canonical_json_bytes(document)


def _controlled_upstream_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Remove ambient package sources and install only from the declared PyPI index."""
    env = dict(source)
    for name in _PACKAGE_SOURCE_ENVIRONMENT:
        env.pop(name, None)
    env.update(
        {
            "PIP_INDEX_URL": CONTROLLED_UPSTREAM_INDEX,
            "UV_DEFAULT_INDEX": CONTROLLED_UPSTREAM_INDEX,
            "UV_NO_CONFIG": "1",
        }
    )
    return env


def _fetch_url(url: str) -> tuple[bytes, str]:
    parsed = urlparse(url)
    trusted = url == UPSTREAM_PYPI_METADATA_URL or (
        parsed.scheme == "https"
        and parsed.hostname == "files.pythonhosted.org"
        and parsed.path.endswith(f"/{UPSTREAM_WHEEL_NAME}")
    )
    if not trusted:
        raise ArtifactError(f"untrusted upstream provenance URL: {url}")
    request = Request(url, headers={"User-Agent": "graphify-workspace-p1-proof/1"})
    try:
        # The URL is restricted to the two frozen HTTPS hosts above.
        with urlopen(request, timeout=30) as response:  # nosec B310
            status = getattr(response, "status", None)
            if status is None:
                raise ArtifactError(f"upstream provenance response is missing an explicit HTTP status: {url}")
            if status != 200:
                raise ArtifactError(f"upstream provenance fetch returned HTTP {status}: {url}")
            return response.read(), response.geturl()
    except OSError as exc:
        raise ArtifactError(f"upstream provenance fetch failed: {url}: {exc}") from exc


def _select_upstream_wheel(metadata: object) -> str:
    if not isinstance(metadata, dict):
        raise ArtifactError("PyPI upstream metadata is not an object")
    info = metadata.get("info")
    urls = metadata.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise ArtifactError("PyPI upstream metadata omits info or urls")
    if info.get("name") != "graphifyy" or info.get("version") != ENGINE_BASELINE:
        raise ArtifactError("PyPI upstream metadata identifies an unexpected distribution")
    candidates = [
        item
        for item in urls
        if isinstance(item, dict)
        and item.get("filename") == UPSTREAM_WHEEL_NAME
        and item.get("packagetype") == "bdist_wheel"
    ]
    if len(candidates) != 1:
        raise ArtifactError(
            f"PyPI metadata must name exactly one {UPSTREAM_WHEEL_NAME}: {len(candidates)}"
        )
    candidate = candidates[0]
    digests = candidate.get("digests")
    if not isinstance(digests, dict) or digests.get("sha256") != UPSTREAM_WHEEL_SHA256:
        raise ArtifactError("PyPI upstream wheel digest does not match the frozen baseline")
    url = candidate.get("url")
    if not isinstance(url, str):
        raise ArtifactError("PyPI upstream wheel URL is missing")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org":
        raise ArtifactError(f"PyPI upstream wheel URL uses an untrusted source: {url}")
    if not parsed.path.endswith(f"/{UPSTREAM_WHEEL_NAME}"):
        raise ArtifactError(f"PyPI upstream wheel URL has an unexpected filename: {url}")
    return url


def _download_verified_upstream_wheel(destination: Path) -> dict[str, str]:
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise ArtifactError(f"upstream download root must be a real directory: {destination}")
    metadata_bytes, metadata_source = _fetch_url(UPSTREAM_PYPI_METADATA_URL)
    if metadata_source != UPSTREAM_PYPI_METADATA_URL:
        raise ArtifactError(f"PyPI metadata redirected unexpectedly: {metadata_source}")
    try:
        metadata = json.loads(metadata_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"PyPI upstream metadata is invalid JSON: {exc}") from exc
    source_url = _select_upstream_wheel(metadata)
    wheel_bytes, resolved_source = _fetch_url(source_url)
    if resolved_source != source_url:
        raise ArtifactError(f"PyPI wheel redirected unexpectedly: {resolved_source}")
    actual_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
    if actual_sha256 != UPSTREAM_WHEEL_SHA256:
        raise ArtifactError(
            "downloaded upstream wheel digest mismatch: "
            f"{actual_sha256} != {UPSTREAM_WHEEL_SHA256}"
        )
    wheel = destination / UPSTREAM_WHEEL_NAME
    wheel.write_bytes(wheel_bytes)
    wheel.chmod(0o644)
    return {
        "logical_requirement": f"graphifyy=={ENGINE_BASELINE}",
        "metadata_url": UPSTREAM_PYPI_METADATA_URL,
        "source_url": source_url,
        "filename": UPSTREAM_WHEEL_NAME,
        "sha256": actual_sha256,
        "path": str(wheel),
    }


def _assert_hashed_requirements(requirements: Path, *, label: str) -> None:
    """Require every logical requirement to carry valid SHA-256 artifact hashes."""
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtifactError(f"cannot read {label} requirements: {requirements}: {exc}") from exc

    entry: list[str] = []
    entry_line = 0
    entry_count = 0
    for line_number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not entry:
            entry_line = line_number
        continued = stripped.endswith("\\")
        entry.append(stripped[:-1].rstrip() if continued else stripped)
        if continued:
            continue

        logical_requirement = " ".join(entry)
        requirement_token = logical_requirement.split(maxsplit=1)[0]
        if requirement_token.startswith(("-", ";")):
            raise ArtifactError(
                f"{label} requirements lack a requirement specifier at line "
                f"{entry_line}: {requirements}"
            )
        hashes = re.findall(r"(?<!\S)--hash=([^\s]+)", logical_requirement)
        if not hashes or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in hashes
        ):
            raise ArtifactError(
                f"{label} requirements lack valid SHA-256 hashes at line "
                f"{entry_line}: {requirements}"
            )
        entry_count += 1
        entry = []

    if entry:
        raise ArtifactError(
            f"{label} requirements end with an unterminated continuation at line "
            f"{entry_line}: {requirements}"
        )
    if entry_count == 0:
        raise ArtifactError(f"{label} requirements contain no locked entries: {requirements}")


def _export_runtime(repo_root: Path, requirements: Path, sbom: Path) -> None:
    env = _controlled_upstream_environment(os.environ)
    uv = _uv()
    requirements_result = _run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--format",
            "requirements.txt",
        ],
        cwd=repo_root,
        env=env,
    )
    requirements.write_text(requirements_result.stdout, encoding="utf-8")
    _assert_hashed_requirements(requirements, label="candidate runtime")
    sbom_result = _run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "cyclonedx1.5",
        ],
        cwd=repo_root,
        env=env,
    )
    try:
        parsed = json.loads(sbom_result.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"uv emitted an invalid CycloneDX SBOM: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ArtifactError("uv emitted a non-object CycloneDX SBOM")
    sbom.write_bytes(_normalize_cyclonedx(parsed, sha256_file(repo_root / "uv.lock")))


def _audit_requirements(
    requirements: Path,
    *,
    cwd: Path,
    label: str,
    expected_dependency_count: int | None = None,
) -> dict[str, int]:
    """Audit one hashed lock export without inspecting the local environment."""
    if not requirements.is_absolute() or requirements.is_symlink() or not requirements.is_file():
        raise ArtifactError(f"{label} requirements must be an absolute regular file: {requirements}")
    _assert_hashed_requirements(requirements, label=label)
    result = _run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--strict",
            "--require-hashes",
            "--no-deps",
            "--disable-pip",
            "--progress-spinner",
            "off",
            "--desc",
            "off",
            "--format",
            "json",
            "--requirement",
            str(requirements),
        ],
        cwd=cwd,
        env=_controlled_upstream_environment(os.environ),
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{label} pip-audit result is invalid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("dependencies"), list):
        raise ArtifactError(f"{label} pip-audit result omits dependencies")
    dependencies = document["dependencies"]
    if expected_dependency_count is not None and len(dependencies) != expected_dependency_count:
        raise ArtifactError(
            f"{label} audited {len(dependencies)} of {expected_dependency_count} locked records"
        )
    if any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("name"), str)
        or not isinstance(entry.get("version"), str)
        or not isinstance(entry.get("vulns"), list)
        for entry in dependencies
    ):
        raise ArtifactError(f"{label} pip-audit result contains an unresolved dependency")
    vulnerabilities = sum(
        len(entry.get("vulns", []))
        for entry in dependencies
        if isinstance(entry, dict) and isinstance(entry.get("vulns", []), list)
    )
    if vulnerabilities:
        raise ArtifactError(f"{label} has {vulnerabilities} known vulnerability records")
    return {
        "dependency_count": len(dependencies),
        "vulnerability_count": vulnerabilities,
    }


def _locked_registry_requirement_files(
    repo_root: Path,
    destination: Path,
) -> list[tuple[Path, int]]:
    """Render marker-free hashed cohorts covering every registry record in uv.lock."""
    lock_path = repo_root / "uv.lock"
    try:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactError(f"cannot read candidate lock for complete audit: {exc}") from exc
    packages = document.get("package")
    if not isinstance(packages, list):
        raise ArtifactError("candidate lock omits package records")

    records: dict[str, dict[str, set[str]]] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ArtifactError("candidate lock contains a non-object package record")
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        if not isinstance(name, str) or not isinstance(version, str) or not isinstance(source, dict):
            raise ArtifactError("candidate lock contains an incomplete package identity")
        registry = source.get("registry")
        if registry is None:
            if name == "graphifyy" and source.get("editable") == ".":
                continue
            raise ArtifactError(f"locked package {name} {version} is not registry-auditable")
        if registry != CONTROLLED_UPSTREAM_INDEX:
            raise ArtifactError(f"locked package {name} {version} uses untrusted registry {registry}")

        hashes: set[str] = set()
        sdist = package.get("sdist")
        if isinstance(sdist, dict) and isinstance(sdist.get("hash"), str):
            hashes.add(sdist["hash"])
        wheels = package.get("wheels")
        if isinstance(wheels, list):
            hashes.update(
                wheel["hash"]
                for wheel in wheels
                if isinstance(wheel, dict) and isinstance(wheel.get("hash"), str)
            )
        if not hashes or any(re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None for item in hashes):
            raise ArtifactError(f"locked package {name} {version} lacks valid SHA-256 artifacts")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        records.setdefault(normalized, {}).setdefault(version, set()).update(hashes)

    if not records:
        raise ArtifactError("candidate lock contains no registry packages to audit")
    destination.mkdir(parents=True, exist_ok=True)
    version_sets = {name: sorted(versions.items()) for name, versions in records.items()}
    cohort_count = max(len(versions) for versions in version_sets.values())
    cohorts: list[tuple[Path, int]] = []
    for index in range(cohort_count):
        lines: list[str] = []
        for name in sorted(version_sets):
            versions = version_sets[name]
            if index >= len(versions):
                continue
            version, hashes = versions[index]
            hash_arguments = " ".join(f"--hash={item}" for item in sorted(hashes))
            lines.append(f"{name}=={version} {hash_arguments}")
        path = destination / f"locked-registry-{index + 1}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cohorts.append((path, len(lines)))
    return cohorts


def _export_audit_scope(
    repo_root: Path,
    destination: Path,
    *,
    arguments: tuple[str, ...],
) -> None:
    result = _run(
        [
            _uv(),
            "export",
            "--locked",
            *arguments,
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--format",
            "requirements.txt",
        ],
        cwd=repo_root,
        env=_controlled_upstream_environment(os.environ),
    )
    destination.write_text(result.stdout, encoding="utf-8")
    _assert_hashed_requirements(destination, label=destination.stem)


def _wheel_metadata(wheel: Path) -> dict[str, str]:
    if not wheel.is_absolute() or wheel.is_symlink() or not wheel.is_file():
        raise ArtifactError(f"candidate wheel must be an absolute regular file: {wheel}")
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ArtifactError(
                f"candidate wheel must contain one METADATA file: {metadata_names}"
            )
        message = BytesParser(policy=policy.default).parsebytes(archive.read(metadata_names[0]))
    name = message.get("Name")
    version = message.get("Version")
    if name != "graphifyy" or version != CANDIDATE_DISTRIBUTION_VERSION:
        raise ArtifactError(f"candidate wheel metadata identifies {name!r} {version!r}")
    return {"distribution": name, "version": version}


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_graphify(venv: Path) -> Path:
    return venv / ("Scripts/graphify.exe" if os.name == "nt" else "bin/graphify")


def _verify_noneditable_candidate_install(
    *,
    wheel: Path,
    requirements: Path,
    work_root: Path,
) -> dict[str, object]:
    metadata = _wheel_metadata(wheel)
    _assert_hashed_requirements(requirements, label="candidate runtime")
    uv = _uv()
    env = _controlled_upstream_environment(os.environ)
    env.pop("PYTHONPATH", None)
    venv = work_root / "candidate-venv"
    _run(
        [uv, "venv", "--no-project", "--python", sys.executable, str(venv)],
        cwd=work_root,
        env=env,
    )
    python = _venv_python(venv)
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--require-hashes",
            "--requirements",
            str(requirements),
        ],
        cwd=work_root,
        env=env,
    )
    _run(
        [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        cwd=work_root,
        env=env,
    )
    _run([uv, "pip", "check", "--python", str(python)], cwd=work_root, env=env)
    graphify = _venv_graphify(venv)
    installed_version = _run([str(graphify), "--version"], cwd=work_root, env=env).stdout.strip()
    if installed_version != f"graphify {metadata['version']}":
        raise ArtifactError(f"candidate wheel console reports unexpected version: {installed_version}")
    _run([str(graphify), "--help"], cwd=work_root, env=env)
    inspection = _run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m,json;"
                "import graphify;"
                "from pathlib import Path;"
                "d=m.distribution('graphifyy');"
                "u=json.loads(d.read_text('direct_url.json') or '{}');"
                "print(json.dumps({'version':d.version,'direct_url':u,"
                "'module_file':str(Path(graphify.__file__).resolve())},sort_keys=True))"
            ),
        ],
        cwd=work_root,
        env=env,
    )
    try:
        installed = json.loads(inspection.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"candidate installation inspection is invalid JSON: {exc}") from exc
    direct_url = installed.get("direct_url") if isinstance(installed, dict) else None
    source_url = direct_url.get("url") if isinstance(direct_url, dict) else None
    parsed_source = urlparse(source_url) if isinstance(source_url, str) else None
    installed_archive = (
        Path(url2pathname(parsed_source.path)).resolve()
        if parsed_source is not None
        and parsed_source.scheme == "file"
        and parsed_source.netloc in {"", "localhost"}
        else None
    )
    if (
        not isinstance(direct_url, dict)
        or installed_archive != wheel.resolve()
        or not isinstance(direct_url.get("archive_info"), dict)
        or "dir_info" in direct_url
    ):
        raise ArtifactError("candidate installation is not bound to the non-editable wheel archive")
    if installed.get("version") != metadata["version"]:
        raise ArtifactError("installed candidate version does not match wheel metadata")
    module_file = installed.get("module_file")
    if not isinstance(module_file, str) or not Path(module_file).is_relative_to(venv.resolve()):
        raise ArtifactError("candidate import did not resolve from the isolated wheel environment")
    return {
        **metadata,
        "editable": False,
        "source": source_url,
        "module_file": module_file,
        "console_version": installed_version,
        "dependencies_consistent": True,
    }


def audit_candidate(*, repo_root: Path, artifact_root: Path) -> dict[str, object]:
    """Verify and audit the exact candidate wheel plus all locked dependency scopes."""
    repo_root = repo_root.resolve()
    artifact_root = artifact_root.resolve()
    uv = _uv()
    head, tree = _assert_candidate_source(repo_root)
    try:
        trusted = (artifact_root / "trusted-manifest.json").read_bytes()
        verify_trusted_manifest(artifact_root=artifact_root, trusted_manifest=trusted)
        compatibility_data = json.loads(
            (artifact_root / "compatibility.json").read_text(encoding="utf-8")
        )
        provenance = json.loads((artifact_root / "provenance.json").read_text(encoding="utf-8"))
        compatibility_document = CompatibilityManifest.from_mapping(compatibility_data)
        compatibility = compatibility_document.to_dict()
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        raise ArtifactError(f"candidate audit cannot validate artifact identity: {exc}") from exc
    _, _, runtime_authority_sha256 = _validate_candidate_runtime_authority(
        artifact_root=artifact_root,
        trusted_manifest=trusted,
    )
    expected_identity = {
        "fork_commit": head,
        "runtime_lock_sha256": sha256_file(repo_root / "uv.lock"),
    }
    if any(compatibility.get(name) != value for name, value in expected_identity.items()):
        raise ArtifactError("candidate compatibility does not match the audited checkout and lock")
    if provenance.get("fork_commit") != head or provenance.get("fork_tree") != tree:
        raise ArtifactError("candidate provenance does not match the audited checkout tree")

    wheel = artifact_root / WHEEL_NAME
    runtime_requirements = artifact_root / "runtime-requirements.txt"
    with tempfile.TemporaryDirectory(prefix="graphify-candidate-audit-") as raw:
        work_root = Path(raw)
        optional_requirements = work_root / "all-extras-requirements.txt"
        dev_requirements = work_root / "dev-requirements.txt"
        locked_registry = _locked_registry_requirement_files(repo_root, work_root / "lock-audit")
        _export_audit_scope(
            repo_root,
            optional_requirements,
            arguments=("--no-dev", "--all-extras"),
        )
        _export_audit_scope(
            repo_root,
            dev_requirements,
            arguments=("--only-dev",),
        )
        installation = _verify_noneditable_candidate_install(
            wheel=wheel,
            requirements=runtime_requirements,
            work_root=work_root,
        )
        audits = {
            "runtime": _audit_requirements(
                runtime_requirements,
                cwd=repo_root,
                label="candidate runtime",
            ),
            "all_extras": _audit_requirements(
                optional_requirements,
                cwd=repo_root,
                label="locked runtime plus all extras",
            ),
            "dev": _audit_requirements(
                dev_requirements,
                cwd=repo_root,
                label="locked development dependencies",
            ),
        }
        locked_results = [
            _audit_requirements(
                requirements,
                cwd=repo_root,
                label=f"complete locked registry cohort {index}",
                expected_dependency_count=expected_count,
            )
            for index, (requirements, expected_count) in enumerate(locked_registry, start=1)
        ]
        audits["all_locked_registry_records"] = {
            "cohort_count": len(locked_results),
            "dependency_count": sum(result["dependency_count"] for result in locked_results),
            "vulnerability_count": sum(
                result["vulnerability_count"] for result in locked_results
            ),
        }
    pip_audit = _run([sys.executable, "-m", "pip_audit", "--version"], cwd=repo_root)
    return {
        "artifact_root": str(artifact_root),
        "fork_commit": head,
        "fork_tree": tree,
        "wheel_sha256": sha256_file(wheel),
        "runtime_manifest_sha256": runtime_authority_sha256,
        "runtime_requirements_sha256": sha256_file(runtime_requirements),
        "sbom_sha256": sha256_file(artifact_root / "sbom.cdx.json"),
        "installation": installation,
        "audits": audits,
        "pip_audit": pip_audit.stdout.strip(),
        "uv": _run([uv, "--version"], cwd=repo_root).stdout.strip(),
    }


def _write_prior_home(home: Path, codex_home: Path) -> None:
    targets = {
        "binary": home / ".local/bin/graphify",
        "runtime": home / ".local/state/graphify/runtime-manifest.json",
        "skill": codex_home / "skills/graphify/SKILL.md",
        "service": home / "Library/LaunchAgents/com.graphify.fixture.plist",
    }
    for name, path in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PRIOR_FILES[name])
        mode = 0o755 if name == "binary" else 0o600 if name == "runtime" else 0o644
        path.chmod(mode)
    skill_root = targets["skill"].parent
    version = skill_root / ".graphify_version"
    version.write_bytes(b"0.9.16")
    version.chmod(0o644)
    prior_reference = skill_root / "references/prior.md"
    prior_reference.parent.mkdir(parents=True)
    prior_reference.write_bytes(b"# prior Graphify reference\n")
    prior_reference.chmod(0o644)
    canary = home / ".local/state/graphify/workspaces/fixture/generations/gen-canary/receipt.json"
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_bytes(_CANARY)
    canary.chmod(0o644)
    graph = canary.parent / "graphify-out/graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_bytes(b'{"nodes":[]}\n')
    graph.chmod(0o644)
    last_good = canary.parents[1] / "gen-last-good/receipt.json"
    last_good.parent.mkdir(parents=True)
    last_good.write_bytes(b'{"generation":"gen-last-good","immutable":true}\n')
    last_good.chmod(0o644)


def _build_offline_rollback(destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="graphify-rollback-home-") as raw:
        home = Path(raw) / "home"
        codex_home = home / ".codex"
        _write_prior_home(home, codex_home)
        snapshot_disposable_home(
            home=home,
            codex_home=codex_home,
            rollback_bundle=destination,
        )


def build_candidate(*, repo_root: Path, output_root: Path) -> dict[str, object]:
    """Build the exact committed workspace candidate and its frozen manifest."""
    repo_root = repo_root.resolve()
    output_root = output_root.resolve()
    _uv()
    head, tree = _assert_candidate_source(repo_root)
    _assert_safe_output_root(repo_root, output_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise ArtifactError(f"candidate output root must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="graphify-candidate-inputs-") as raw:
        inputs = Path(raw)
        source = inputs / "source"
        source.mkdir()
        _extract_head(repo_root, source)
        _render_codex_skill(source)
        wheel = _build_reproducible_wheel(repo_root, inputs / WHEEL_NAME)
        requirements = inputs / "runtime-requirements.txt"
        sbom = inputs / "sbom.cdx.json"
        _export_runtime(source, requirements, sbom)
        artifacts = build_static_bundles(
            repo_root=source,
            output_root=output_root,
            wheel=wheel,
            runtime_manifest=requirements,
        )
        requirements_output = output_root / requirements.name
        requirements_output.write_bytes(requirements.read_bytes())
        artifacts[requirements_output.name] = requirements_output
        sbom_output = output_root / sbom.name
        sbom_output.write_bytes(sbom.read_bytes())
        artifacts[sbom_output.name] = sbom_output

    rollback = output_root / "offline-rollback.zip"
    _build_offline_rollback(rollback)
    artifacts[rollback.name] = rollback

    provenance_data = {
        "format_version": 1,
        "distribution": "graphifyy",
        "distribution_version": CANDIDATE_DISTRIBUTION_VERSION,
        "engine_baseline": ENGINE_BASELINE,
        "extractor_cache_abi": EXTRACTOR_CACHE_ABI,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "platform": platform.platform(),
        "uv": _run([_uv(), "--version"], cwd=repo_root).stdout.strip(),
        "upstream_tag_object": UPSTREAM_TAG_OBJECT,
        "upstream_commit": UPSTREAM_COMMIT,
        "fork_commit": head,
        "fork_tree": tree,
        "source_date_epoch": FIXED_SOURCE_EPOCH,
        "wheel_builds": 2,
        "wheel_byte_identical": True,
        "skillgen_command": "python -m tools.skillgen --platform codex",
        "skillgen_committed_render_match": True,
        "skill_bundle_uses_rendered_bytes": True,
        "runtime_lock_sha256": sha256_file(repo_root / "uv.lock"),
        "rollback_scope": "synthetic-isolated-home-fixture",
        "production_install_performed": False,
    }
    provenance = output_root / "provenance.json"
    provenance.write_bytes(canonical_json_bytes(provenance_data))
    artifacts[provenance.name] = provenance

    artifact_hashes = {name: sha256_file(path) for name, path in sorted(artifacts.items())}
    compatibility_data = {
        "contract": "graphify.workspace.compatibility_manifest",
        "schema_version": 1,
        "distribution": "graphifyy",
        "distribution_version": CANDIDATE_DISTRIBUTION_VERSION,
        "distribution_build": f"git:{head}",
        "engine_baseline": ENGINE_BASELINE,
        "upstream_commit": UPSTREAM_COMMIT,
        "fork_commit": head,
        "extractor_cache_abi": EXTRACTOR_CACHE_ABI,
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "platform": platform.platform(),
        "state_schema_version": STATE_SCHEMA_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "cli_contract_version": CLI_CONTRACT_VERSION,
        "runtime_lock_sha256": sha256_file(repo_root / "uv.lock"),
        "skill_bundle_sha256": artifact_hashes["skill-bundle.zip"],
        "contract_bundle_sha256": artifact_hashes["contract-bundle.zip"],
        "fixture_manifest_sha256": artifact_hashes["fixture-manifest.json"],
        "provenance_sha256": artifact_hashes["provenance.json"],
        "sbom_sha256": artifact_hashes["sbom.cdx.json"],
        "artifacts": artifact_hashes,
    }
    compatibility_document = cast(
        CompatibilityManifest,
        CompatibilityManifest.from_mapping(compatibility_data),
    )
    compatibility = output_root / "compatibility.json"
    compatibility.write_bytes(compatibility_document.canonical)
    artifacts[compatibility.name] = compatibility

    runtime_authority = output_root / RUNTIME_AUTHORITY_FILENAME
    runtime_authority.write_bytes(_p5c1_runtime_authority(compatibility_document).canonical)
    # Reject any accidental serializer drift before the outer trust anchor is frozen.
    WorkspaceRuntimeAuthority.from_json(runtime_authority.read_bytes())
    artifacts[runtime_authority.name] = runtime_authority

    trusted = write_trusted_manifest(
        artifact_root=output_root,
        artifact_names=sorted(artifacts),
    )
    verify_trusted_manifest(artifact_root=output_root, trusted_manifest=trusted)
    files = _file_hashes(output_root)
    return {
        "artifact_root": str(output_root),
        "fork_commit": head,
        "fork_tree": tree,
        "artifact_count": len(files),
        "runtime_manifest_sha256": sha256_file(runtime_authority),
        "trusted_manifest_sha256": sha256_file(output_root / "trusted-manifest.json"),
        "artifacts": files,
    }


def compare_candidate_roots(*, first: Path, second: Path) -> dict[str, object]:
    """Verify and compare every file digest in two complete candidate roots."""
    first = first.resolve()
    second = second.resolve()
    for root in (first, second):
        trusted = (root / "trusted-manifest.json").read_bytes()
        verify_trusted_manifest(artifact_root=root, trusted_manifest=trusted)
    first_files = _file_hashes(first)
    second_files = _file_hashes(second)
    if first_files != second_files:
        names = sorted(set(first_files) | set(second_files))
        drift = [name for name in names if first_files.get(name) != second_files.get(name)]
        raise ArtifactError(f"candidate build digest drift: {drift}")
    return {
        "byte_identical": True,
        "file_count": len(first_files),
        "files": first_files,
    }


def build_and_compare_candidates(
    *,
    repo_root: Path,
    output_root: Path,
    comparison_output_root: Path,
) -> dict[str, object]:
    """Build two clean candidate roots and require complete digest equality."""
    output_root = output_root.resolve()
    comparison_output_root = comparison_output_root.resolve()
    if (
        output_root == comparison_output_root
        or output_root in comparison_output_root.parents
        or comparison_output_root in output_root.parents
    ):
        raise ArtifactError("candidate comparison roots must be distinct and non-nested")
    first = build_candidate(repo_root=repo_root, output_root=output_root)
    second = build_candidate(repo_root=repo_root, output_root=comparison_output_root)
    comparison = compare_candidate_roots(first=output_root, second=comparison_output_root)
    return {"first": first, "second": second, "comparison": comparison}


def _isolated_environment(home: Path, codex_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    path_components = [str(home / ".local/bin")]
    ambient_path = os.environ.get("PATH")
    if ambient_path:
        path_components.extend(
            component for component in ambient_path.split(os.pathsep) if component
        )
    env.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "UV_CACHE_DIR": str(home / ".cache/uv"),
            "UV_TOOL_DIR": str(home / ".local/share/uv/tools"),
            "UV_TOOL_BIN_DIR": str(home / ".local/bin"),
            "UV_NO_CONFIG": "1",
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_DOWNLOADS": "never",
            "PYTHONNOUSERSITE": "1",
            "PATH": os.pathsep.join(path_components),
        }
    )
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    return _controlled_upstream_environment(env)


def skill_bundle_tree_sha256(
    skill_bundle: Path,
    *,
    installed_root: Path | None = None,
) -> str:
    """Return the canonical installed-tree digest encoded by skill-bundle.zip."""
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    with zipfile.ZipFile(skill_bundle) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            pure = Path(info.filename)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or len(pure.parts) < 2
                or pure.parts[0] != "skill"
            ):
                raise ArtifactError(f"unsafe skill bundle member: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) != stat.S_IFREG:
                raise ArtifactError(f"skill bundle member is not a regular file: {info.filename}")
            relative = Path(*pure.parts[1:]).as_posix()
            if relative in seen:
                raise ArtifactError(f"duplicate skill bundle member: {relative}")
            seen.add(relative)
            data = archive.read(info)
            entries.append(
                {
                    "path": relative,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "mode": f"{stat.S_IMODE(mode):04o}",
                }
            )
    if not entries:
        raise ArtifactError("skill bundle contains no installed files")
    expected = canonical_sha256(entries)
    if installed_root is not None:
        actual = strict_tree_sha256(installed_root)
        if actual != expected:
            raise ArtifactError(
                f"installed skill tree does not match skill-bundle.zip: {actual} != {expected}"
            )
    return expected


def _install_clean_home(
    *,
    home: Path,
    artifact_root: Path,
    dependency_output: Path,
) -> dict[str, str]:
    if home.exists() and any(home.iterdir()):
        raise ArtifactError(f"isolated home must start empty: {home}")
    home.mkdir(parents=True, exist_ok=True)
    codex_home = home / ".codex"
    codex_home.mkdir()
    env = _isolated_environment(home, codex_home)
    wheel = artifact_root / WHEEL_NAME
    requirements = artifact_root / "runtime-requirements.txt"
    _run(
        [
            _uv(),
            "tool",
            "install",
            "--force",
            "--python",
            sys.executable,
            "--with-requirements",
            str(requirements),
            str(wheel),
        ],
        cwd=home,
        env=env,
    )
    binary = home / ".local/bin/graphify"
    version = _run([str(binary), "--version"], cwd=home, env=env).stdout.strip()
    if version != f"graphify {CANDIDATE_DISTRIBUTION_VERSION}":
        raise ArtifactError(f"isolated binary resolved unexpected version: {version}")
    _run([str(binary), "install", "--platform", "codex"], cwd=home, env=env)
    tool_python_candidates = list((home / ".local/share/uv/tools").glob("*/bin/python"))
    if len(tool_python_candidates) != 1:
        raise ArtifactError(f"cannot identify isolated tool interpreter: {tool_python_candidates}")
    freeze = _run(
        [
            _uv(),
            "pip",
            "freeze",
            "--strict",
            "--python",
            str(tool_python_candidates[0]),
        ],
        cwd=home,
        env=env,
    ).stdout
    normalized = "\n".join(sorted(line for line in freeze.splitlines() if line.strip())) + "\n"
    dependency_output.parent.mkdir(parents=True, exist_ok=True)
    dependency_output.write_text(normalized, encoding="utf-8")
    skill_root = codex_home / "skills/graphify"
    if not (skill_root / "SKILL.md").is_file():
        raise ArtifactError("isolated graphify install did not refresh the Codex skill")
    return {
        "home": str(home),
        "binary": str(binary),
        "version": version,
        "dependency_manifest_sha256": sha256_file(dependency_output),
        "skill_tree_sha256": strict_tree_sha256(skill_root),
    }


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    prior = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class _P5C1InstallFaultSyscalls(PosixSyscalls):
    """Deterministic syscall failures for isolated runtime-authority proof fixtures."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.fsync_calls = 0
        self.fired = False

    def _fail(self, stage: str) -> None:
        if self.stage == stage and not self.fired:
            self.fired = True
            raise OSError(errno.EIO, f"injected runtime authority {stage} failure")

    def write(self, descriptor: int, data: memoryview) -> int:
        self._fail("write")
        return super().write(descriptor, data)

    def fsync(self, descriptor: int) -> None:
        self.fsync_calls += 1
        if self.fsync_calls == 1:
            self._fail("temporary_fsync")
        elif self.fsync_calls == 2:
            self._fail("parent_fsync")
        super().fsync(descriptor)

    def replace_at(
        self,
        source: str,
        destination: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        self._fail("replace")
        super().replace_at(
            source,
            destination,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )


class _P5C1CrashAt:
    def __init__(self, event: str) -> None:
        self.event = event
        self.fired = False

    def __call__(self, event: str) -> None:
        if event == self.event and not self.fired:
            self.fired = True
            raise InjectedFault(event)


def _runtime_target_snapshot(path: Path) -> dict[str, object]:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return {"present": False}
    if path.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ArtifactError(f"runtime authority proof target is unsafe: {path}")
    return {
        "present": True,
        "sha256": sha256_file(path),
        "size": details.st_size,
        "mode": f"{stat.S_IMODE(details.st_mode):04o}",
    }


def _proof_tree_snapshot(root: Path) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        details = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ArtifactError(f"P5C1 proof tree contains a symbolic link: {path}")
        entry: dict[str, object] = {
            "mode": f"{stat.S_IMODE(details.st_mode):04o}",
            "mtime_ns": details.st_mtime_ns,
            "size": details.st_size,
            "type": stat.S_IFMT(details.st_mode),
        }
        if stat.S_ISREG(details.st_mode):
            entry["sha256"] = sha256_file(path)
        snapshot[relative] = entry
    return snapshot


def _p5c1_fixture_environment(fixture_root: Path) -> tuple[dict[str, str], Path, Path]:
    if not fixture_root.is_absolute():
        raise ArtifactError("P5C1 fixture root must be absolute")
    if fixture_root.exists() and any(fixture_root.iterdir()):
        raise ArtifactError(f"P5C1 fixture root must be empty: {fixture_root}")
    fixture_root.mkdir(parents=True, exist_ok=True)
    home = fixture_root / "home"
    state_home = fixture_root / "xdg-state-home"
    codex_home = fixture_root / "codex-home"
    for root in (home, state_home, codex_home):
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    environ = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "CODEX_HOME": str(codex_home),
    }
    return environ, state_home / "graphify", codex_home


def _p5c1_state_fixture(
    fixture_root: Path,
) -> tuple[dict[str, str], DurableStateRoot, Path, Path, str]:
    environ, state_root, codex_home = _p5c1_fixture_environment(fixture_root)
    state = DurableStateRoot(
        state_root,
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    generation_relative = Path("workspaces/fixture/generations/gen-canary/receipt.json")
    state.install_once_bytes(
        generation_relative,
        _CANARY,
        label="p5c1-generation-canary",
    )
    generation_root = state_root / "workspaces/fixture/generations"
    return environ, state, state_root, codex_home, strict_tree_sha256(generation_root)


def _prove_successful_runtime_authority_install(
    *,
    fixture_root: Path,
    payload: bytes,
    payload_sha256: str,
    authority: WorkspaceRuntimeAuthority,
) -> dict[str, object]:
    environ, state, state_root, codex_home, generations_before = _p5c1_state_fixture(fixture_root)
    installed = state.install_once_bytes(
        RUNTIME_AUTHORITY_FILENAME,
        payload,
        label="p5c1-runtime-authority",
    )
    installed_snapshot = _runtime_target_snapshot(installed)
    if installed_snapshot != {
        "present": True,
        "sha256": payload_sha256,
        "size": len(payload),
        "mode": "0600",
    }:
        raise ArtifactError("installed runtime authority does not match candidate bytes and mode")
    inode = installed.stat().st_ino
    retried = state.install_once_bytes(
        RUNTIME_AUTHORITY_FILENAME,
        payload,
        label="p5c1-runtime-authority-retry",
    )
    if retried.stat().st_ino != inode:
        raise ArtifactError("same-byte runtime authority retry replaced the inode")
    before_conflict = _runtime_target_snapshot(installed)
    try:
        state.install_once_bytes(
            RUNTIME_AUTHORITY_FILENAME,
            payload + b"\x00",
            label="p5c1-runtime-authority-conflict",
        )
    except StateCorrupt:
        pass
    else:
        raise ArtifactError("different-byte runtime authority retry did not fail closed")
    if _runtime_target_snapshot(installed) != before_conflict or installed.stat().st_ino != inode:
        raise ArtifactError("different-byte retry changed installed runtime authority state")

    roots_before_loader = {
        "home": _proof_tree_snapshot(Path(environ["HOME"])),
        "state_home": _proof_tree_snapshot(Path(environ["XDG_STATE_HOME"])),
        "codex_home": _proof_tree_snapshot(codex_home),
    }
    inputs = load_workspace_runtime_inputs(
        environ=environ,
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    if inputs is None:
        raise ArtifactError("P5B1 loader did not read the installed runtime authority")
    if inputs.compatibility_manifest.canonical != authority.compatibility_manifest.canonical:
        raise ArtifactError("P5B1 loader changed candidate compatibility authority")
    if inputs.semantic_queue_policy.canonical != authority.semantic_queue_policy.canonical:
        raise ArtifactError("P5B1 loader changed candidate semantic queue policy")
    roots_after_loader = {
        "home": _proof_tree_snapshot(Path(environ["HOME"])),
        "state_home": _proof_tree_snapshot(Path(environ["XDG_STATE_HOME"])),
        "codex_home": _proof_tree_snapshot(codex_home),
    }
    if roots_after_loader != roots_before_loader:
        raise ArtifactError("P5B1 loader wrote to an isolated external-state root")
    generation_root = state_root / "workspaces/fixture/generations"
    if strict_tree_sha256(generation_root) != generations_before:
        raise ArtifactError("runtime authority installation changed generation fixtures")
    return {
        "installed_sha256": payload_sha256,
        "installed_mode": "0600",
        "same_byte_retry_inode_preserved": True,
        "different_byte_retry_rejected": True,
        "loader_read_only": True,
        "generation_tree_sha256": generations_before,
        "environment": environ,
    }


def _prove_preexisting_runtime_authority_conflict(
    *,
    fixture_root: Path,
    payload: bytes,
) -> dict[str, object]:
    environ, state, state_root, _codex_home, generations_before = _p5c1_state_fixture(fixture_root)
    prior_payload = canonical_json_bytes(
        {
            "contract": "graphify.workspace.runtime_authority.proof_conflict",
            "format_version": 1,
        }
    )
    target = state.install_once_bytes(
        RUNTIME_AUTHORITY_FILENAME,
        prior_payload,
        label="p5c1-preexisting-runtime-authority",
    )
    prior = _runtime_target_snapshot(target)
    prior_inode = target.stat().st_ino
    try:
        state.install_once_bytes(
            RUNTIME_AUTHORITY_FILENAME,
            payload,
            label="p5c1-runtime-authority-conflict",
        )
    except StateCorrupt:
        pass
    else:
        raise ArtifactError("candidate authority replaced a different pre-existing target")
    if _runtime_target_snapshot(target) != prior or target.stat().st_ino != prior_inode:
        raise ArtifactError("different pre-existing runtime authority was not preserved exactly")
    generation_root = state_root / "workspaces/fixture/generations"
    if strict_tree_sha256(generation_root) != generations_before:
        raise ArtifactError("pre-existing authority conflict changed generation fixtures")
    return {
        "different_bytes_rejected": True,
        "prior_state": prior,
        "prior_inode_preserved": True,
        "generation_tree_sha256": generations_before,
        "environment": environ,
    }


def _prove_failed_runtime_authority_install(
    *,
    fixture_root: Path,
    payload: bytes,
    stage: str,
) -> dict[str, object]:
    environ, state, state_root, _codex_home, generations_before = _p5c1_state_fixture(fixture_root)
    target = state_root / RUNTIME_AUTHORITY_FILENAME
    prior = _runtime_target_snapshot(target)
    syscalls = _P5C1InstallFaultSyscalls(stage)
    crash = _P5C1CrashAt("p5c1-runtime-authority:installed")
    failing_state = DurableStateRoot(
        state_root,
        capabilities=RuntimeCapabilities.supported_test_fixture(),
        fault_hook=crash if stage == "installed_hook" else None,
        syscalls=syscalls,
    )
    try:
        failing_state.install_once_bytes(
            RUNTIME_AUTHORITY_FILENAME,
            payload,
            label="p5c1-runtime-authority",
        )
    except Exception as exc:
        failure = exc
    else:
        raise ArtifactError(f"runtime authority failpoint did not fire: {stage}")

    pre_visibility = stage in {"write", "temporary_fsync", "replace"}
    if pre_visibility:
        if not isinstance(failure, OSError) or not syscalls.fired:
            raise ArtifactError(
                f"runtime authority pre-visibility failure was misclassified: {stage}"
            )
    elif not isinstance(failure, CommitUnknown):
        raise ArtifactError(f"runtime authority visibility failure was not commit-unknown: {stage}")
    elif stage == "installed_hook" and not crash.fired:
        raise ArtifactError("runtime authority installed-hook failpoint did not fire")
    elif stage == "parent_fsync" and not syscalls.fired:
        raise ArtifactError("runtime authority parent-fsync failpoint did not fire")

    visible = state.read_optional_existing_bytes(RUNTIME_AUTHORITY_FILENAME)
    if pre_visibility and visible is not None:
        raise ArtifactError(f"pre-visibility failure left a new runtime authority: {stage}")
    if not pre_visibility and visible != payload:
        raise ArtifactError(f"commit-unknown state lacks exact candidate authority: {stage}")
    if visible is not None:
        state.unlink_and_sync(
            RUNTIME_AUTHORITY_FILENAME,
            label="p5c1-runtime-authority-compensation",
        )
    if _runtime_target_snapshot(target) != prior:
        raise ArtifactError(f"runtime authority compensation did not restore absence: {stage}")
    generation_root = state_root / "workspaces/fixture/generations"
    if strict_tree_sha256(generation_root) != generations_before:
        raise ArtifactError(f"runtime authority compensation changed generations: {stage}")
    return {
        "stage": stage,
        "failure": type(failure).__name__,
        "pre_visibility": pre_visibility,
        "candidate_visible_before_compensation": visible is not None,
        "prior_state": prior,
        "restored_state": _runtime_target_snapshot(target),
        "generation_tree_sha256": generations_before,
        "environment": environ,
    }


def _prove_runtime_authority_installation(
    *,
    artifact_root: Path,
    trusted_manifest: bytes,
    expected_sha256: str,
    proof_root: Path,
) -> dict[str, object]:
    """Prove P5C1 installation only beneath disposable external-state roots."""

    authority, payload, payload_sha256 = _validate_candidate_runtime_authority(
        artifact_root=artifact_root,
        trusted_manifest=trusted_manifest,
        expected_sha256=expected_sha256,
    )
    if not proof_root.is_absolute():
        raise ArtifactError("runtime authority proof root must be absolute")
    if proof_root.exists() and any(proof_root.iterdir()):
        raise ArtifactError(f"runtime authority proof root must be empty: {proof_root}")
    proof_root.mkdir(parents=True, exist_ok=True)
    success = _prove_successful_runtime_authority_install(
        fixture_root=proof_root / "success",
        payload=payload,
        payload_sha256=payload_sha256,
        authority=authority,
    )
    preexisting_conflict = _prove_preexisting_runtime_authority_conflict(
        fixture_root=proof_root / "preexisting-conflict",
        payload=payload,
    )
    stages = ("write", "temporary_fsync", "replace", "installed_hook", "parent_fsync")
    failures = [
        _prove_failed_runtime_authority_install(
            fixture_root=proof_root / f"failure-{stage}",
            payload=payload,
            stage=stage,
        )
        for stage in stages
    ]
    return {
        "authority_sha256": payload_sha256,
        "trusted_expected_sha256": expected_sha256,
        "canonical_round_trip": authority.canonical == payload,
        "proof_policy": authority.semantic_queue_policy.to_dict(),
        "scope": "p5c1-isolated-proof-authority-only",
        "production_default": False,
        "publication_authority": False,
        "performance_qualification": False,
        "success": success,
        "preexisting_conflict": preexisting_conflict,
        "failures": failures,
        "absent_target_compensation": True,
        "preexisting_target_preserved_without_mutation": True,
        "generation_trees_unchanged": True,
    }


def prove_candidate(*, artifact_root: Path, proof_root: Path) -> dict[str, object]:
    """Run isolated clean-home, provenance, tamper, and compensation proofs."""
    artifact_root = artifact_root.resolve()
    proof_root = proof_root.resolve()
    _uv()
    if proof_root == artifact_root or artifact_root in proof_root.parents:
        raise ArtifactError("proof_root must be outside the frozen artifact root")
    if proof_root.exists() and any(proof_root.iterdir()):
        raise ArtifactError(f"proof output root must be empty: {proof_root}")
    proof_root.mkdir(parents=True, exist_ok=True)
    trusted = (artifact_root / "trusted-manifest.json").read_bytes()
    verify_trusted_manifest(artifact_root=artifact_root, trusted_manifest=trusted)
    expected_runtime_authority_sha256 = _trusted_artifact_sha256(
        trusted_manifest=trusted,
        name=RUNTIME_AUTHORITY_FILENAME,
    )
    runtime_authority_installation = _prove_runtime_authority_installation(
        artifact_root=artifact_root,
        trusted_manifest=trusted,
        expected_sha256=expected_runtime_authority_sha256,
        proof_root=proof_root / "runtime-authority-installation",
    )
    write_proof(
        proof_root / "runtime-authority-installation-proof.json",
        runtime_authority_installation,
    )

    tamper = prove_independent_tamper_rejection(
        artifact_root=artifact_root,
        trusted_manifest=trusted,
    )
    tamper_proof = {"all_rejected": True, "cases": tamper}
    write_proof(proof_root / "tamper-proof.json", tamper_proof)

    first = _install_clean_home(
        home=proof_root / "home-one",
        artifact_root=artifact_root,
        dependency_output=proof_root / "home-one-dependencies.txt",
    )
    second = _install_clean_home(
        home=proof_root / "home-two",
        artifact_root=artifact_root,
        dependency_output=proof_root / "home-two-dependencies.txt",
    )
    if first["dependency_manifest_sha256"] != second["dependency_manifest_sha256"]:
        raise ArtifactError("two clean homes resolved different dependency manifests")
    if first["skill_tree_sha256"] != second["skill_tree_sha256"]:
        raise ArtifactError("two clean homes installed different skill trees")
    expected_skill_tree = skill_bundle_tree_sha256(artifact_root / "skill-bundle.zip")
    for installed in (first, second):
        if installed["skill_tree_sha256"] != expected_skill_tree:
            raise ArtifactError("clean-home skill tree does not match skill-bundle.zip")
    clean_home_proof = {
        "identical_dependency_manifests": True,
        "identical_skill_trees": True,
        "matches_skill_bundle": True,
        "controlled_index": CONTROLLED_UPSTREAM_INDEX,
        "ambient_index_and_find_links_scrubbed": True,
        "skill_bundle_tree_sha256": expected_skill_tree,
        "home_one": first,
        "home_two": second,
    }
    write_proof(proof_root / "clean-home-proof.json", clean_home_proof)

    upstream_home = proof_root / "upstream-home"
    upstream_home.mkdir()
    upstream_record = _download_verified_upstream_wheel(proof_root / "upstream-distribution")
    upstream_env = _isolated_environment(upstream_home, upstream_home / ".codex")
    upstream_wheel = str(upstream_record["path"])
    upstream = _run(
        [
            _uv(),
            "tool",
            "run",
            "--isolated",
            "--no-cache",
            "--default-index",
            CONTROLLED_UPSTREAM_INDEX,
            "--from",
            upstream_wheel,
            "graphify",
            "--version",
        ],
        cwd=upstream_home,
        env=upstream_env,
    ).stdout.strip()
    if upstream != "graphify 0.9.16":
        raise ArtifactError(f"isolated upstream probe returned {upstream!r}")
    provenance_proof = {
        "command": (
            "uv tool run --isolated --no-cache --default-index https://pypi.org/simple "
            f"--from {upstream_wheel} graphify --version"
        ),
        "from_spec": upstream_wheel,
        "logical_requirement": upstream_record["logical_requirement"],
        "distribution_source": upstream_record["source_url"],
        "distribution_filename": upstream_record["filename"],
        "distribution_sha256": upstream_record["sha256"],
        "controlled_index": CONTROLLED_UPSTREAM_INDEX,
        "ambient_index_and_find_links_scrubbed": True,
        "stdout": upstream,
        "local_candidate_version": CANDIDATE_DISTRIBUTION_VERSION,
        "distinct_from_local_candidate": True,
    }
    write_proof(proof_root / "upstream-provenance-proof.json", provenance_proof)

    compensation_home = proof_root / "compensation-home"
    compensation_codex = compensation_home / ".codex"
    _write_prior_home(compensation_home, compensation_codex)
    candidate_binary = (proof_root / "home-one/.local/bin/graphify").read_bytes()
    candidate_skill = (proof_root / "home-one/.codex/skills/graphify/SKILL.md").read_bytes()
    with _temporary_environment(
        {
            "HOME": str(compensation_home),
            "XDG_STATE_HOME": str(compensation_home / ".local/state"),
            "CODEX_HOME": str(compensation_codex),
        }
    ):
        compensation = run_disposable_compensation_proof(
            home=compensation_home,
            codex_home=compensation_codex,
            rollback_bundle=artifact_root / "offline-rollback.zip",
            candidate_files={
                "binary": candidate_binary,
                "runtime": (artifact_root / RUNTIME_AUTHORITY_FILENAME).read_bytes(),
                "skill": candidate_skill,
                "service": b"candidate-login-service-fixture\n",
            },
            fail_after="skill",
        )
    write_proof(proof_root / "compensation-proof.json", compensation)
    restored_modes = compensation.get("restored_modes")
    if not isinstance(restored_modes, Mapping) or restored_modes.get("runtime") != "0600":
        raise ArtifactError("runtime authority compensation did not restore prior mode 0600")
    restored = compensation.get("restored")
    if (
        not isinstance(restored, Mapping)
        or restored.get("runtime") != hashlib.sha256(_PRIOR_FILES["runtime"]).hexdigest()
    ):
        raise ArtifactError("runtime authority compensation did not restore prior bytes")

    summary = {
        "artifact_manifest_sha256": sha256_file(artifact_root / "trusted-manifest.json"),
        "runtime_manifest_sha256": expected_runtime_authority_sha256,
        "runtime_authority_installation": True,
        "absent_target_compensation": runtime_authority_installation["absent_target_compensation"],
        "preexisting_target_compensation": compensation["runtime_target_restored"],
        "clean_homes": True,
        "upstream_provenance": True,
        "tamper_cases": len(tamper),
        "offline_compensation": True,
        "generations_unchanged": compensation["generations_unchanged"],
    }
    write_proof(proof_root / "proof-summary.json", summary)
    return summary


__all__ = [
    "CONTROLLED_UPSTREAM_INDEX",
    "UPSTREAM_COMMIT",
    "UPSTREAM_TAG_OBJECT",
    "UPSTREAM_WHEEL_NAME",
    "UPSTREAM_WHEEL_SHA256",
    "WHEEL_NAME",
    "audit_candidate",
    "build_and_compare_candidates",
    "build_candidate",
    "compare_candidate_roots",
    "prove_candidate",
    "skill_bundle_tree_sha256",
]
