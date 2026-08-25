from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import platform
import signal
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, cast, Iterable, Sequence

import pytest


RUN_SCHEMA = "GraphifyPytestGate.v1"
EVIDENCE_SCHEMA = "GraphifyPytestEvidence.v1"
GATE_RECEIPT_SCHEMA = "GraphifyGateReceipt.v1"
LOCAL_GATE_EVIDENCE_SCHEMA = "GraphifyLocalGateEvidence.v1"
REVIEW_EVIDENCE_SCHEMA = "GraphifyIndependentReviewEvidence.v1"
HOSTED_CI_EVIDENCE_SCHEMA = "GraphifyHostedCIEvidence.v1"
HOSTED_VARIANCE_EVIDENCE_SCHEMA = "GraphifyHostedVarianceEvidence.v1"
_PLUGIN_ARTIFACT_ENV = "GRAPHIFY_PYTEST_GATE_PLUGIN_ARTIFACT"
_EVALUATOR_DIGEST_ENV = "GRAPHIFY_PYTEST_GATE_EVALUATOR_SHA256"
_SAFE_ENV_KEYS = (
    "CI",
    "GITHUB_ACTIONS",
    "RUNNER_ARCH",
    "RUNNER_OS",
    "LANG",
    "LC_ALL",
    "PYTHONHASHSEED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTEST_PLUGINS",
)
_OUTER_XDIST_ENV_KEYS = (
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
)
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_PROCESS_TERMINATE = 0x0001
_WINDOWS_PROCESS_SET_QUOTA = 0x0100
_WINDOWS_JOB_TERMINATION_EXIT_CODE = 124
_WINDOWS_JOB_LAUNCHER = (
    "import json, subprocess, sys\n"
    "try:\n"
    "    command = json.loads(sys.stdin.readline())\n"
    "    if not isinstance(command, list) or not command:\n"
    "        raise ValueError('invalid command')\n"
    "    if not all(isinstance(item, str) and item for item in command):\n"
    "        raise ValueError('invalid command')\n"
    "except (EOFError, json.JSONDecodeError, ValueError):\n"
    "    raise SystemExit(125)\n"
    "raise SystemExit(subprocess.run(command, check=False).returncode)\n"
)
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".graphify",
        ".hypothesis",
        ".mypy_cache",
        ".omx",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "env",
        "graphify-out",
        "venv",
    }
)
_EXCLUDED_FILE_SUFFIXES = (".pyc",)
_FORBIDDEN_PYTEST_FLAGS = frozenset(
    {
        "-k",
        "-m",
        "-x",
        "--continue-on-collection-errors",
        "--deselect",
        "--exitfirst",
        "--failed-first",
        "--ff",
        "--ignore",
        "--ignore-glob",
        "--last-failed",
        "--lf",
        "--maxfail",
        "--new-first",
        "--nf",
        "--stepwise",
        "--sw",
    }
)
_BASELINE_PYTEST_PLUGIN_ENTRYPOINTS = [
    {
        "name": "anyio",
        "value": "anyio.pytest_plugin",
        "distribution": "anyio",
        "version": "4.13.0",
    },
    {
        "name": "hypothesispytest",
        "value": "_hypothesis_pytestplugin",
        "distribution": "hypothesis",
        "version": "6.153.0",
    },
    {
        "name": "pytest_cov",
        "value": "pytest_cov.plugin",
        "distribution": "pytest-cov",
        "version": "7.1.0",
    },
]
_CANDIDATE_PYTEST_PLUGIN_ENTRYPOINTS = [
    *_BASELINE_PYTEST_PLUGIN_ENTRYPOINTS,
    {
        "name": "xdist",
        "value": "xdist.plugin",
        "distribution": "pytest-xdist",
        "version": "3.8.0",
    },
    {
        "name": "xdist.looponfail",
        "value": "xdist.looponfail",
        "distribution": "pytest-xdist",
        "version": "3.8.0",
    },
]
_ALLOWED_SNAPSHOT_TRANSITION_FILES = [
    ".github/workflows/ci.yml",
    "AGENTS.md",
    "README.md",
]
_REVIEWED_PATHS = [
    ".github/workflows/ci.yml",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "tests/test_pytest_parallel_gate.py",
    "tests/test_workspace_adapter.py",
    "tests/test_workspace_status.py",
    "tools/pytest_parallel_gate.py",
    "tools/pytest_parallel_hazard_cohort.txt",
    "uv.lock",
]
_LOCAL_GATE_COMMANDS: dict[str, list[list[str]]] = {
    "lock": [["uv", "lock", "--check"]],
    "focused": [
        [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
            "--dist=loadfile",
            "--max-worker-restart=0",
            "tests/test_pytest_parallel_gate.py",
            "tests/test_workspace_status.py::test_status_emits_no_transient_filesystem_write_events",
            "tests/test_workspace_adapter.py::test_git_fixture_disables_automatic_maintenance",
            "tests/test_workspace_adapter.py::test_read_only_detection_suppresses_stat_cache_and_office_sidecars",
            "tests/test_workspace_adapter.py::test_read_only_observation_omits_legacy_graphify_memory",
            "tests/test_workspace_adapter.py::test_read_only_observation_skips_extensionless_keyword_non_shebang",
        ]
    ],
    "static": [
        [
            "uv",
            "run",
            "--frozen",
            "ruff",
            "check",
            "tools/pytest_parallel_gate.py",
            "tests/test_pytest_parallel_gate.py",
            "tests/test_workspace_status.py",
            "tests/test_workspace_adapter.py",
        ],
        [
            "uv",
            "run",
            "--frozen",
            "pyright",
            "tools/pytest_parallel_gate.py",
            "tests/test_pytest_parallel_gate.py",
            "tests/test_workspace_status.py",
            "tests/test_workspace_adapter.py",
        ],
        ["git", "diff", "--check"],
    ],
    "graph_refresh": [["graphify", "update", "."]],
}


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsJobBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@dataclass
class _WindowsJob:
    kernel32: Any
    handle: Any
    closed: bool = False
_HOSTED_CI_REPOSITORY = "Villeneuve-Ventures/graphify"
_HOSTED_CI_WORKFLOW = "CI"
_HOSTED_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
_HOSTED_CI_JOB_NAMES = ["security-scan", "skillgen-check", "test (3.14)"]
_HAZARD_COHORT_PATH = "tools/pytest_parallel_hazard_cohort.txt"
_PR81_HEAD_REF = "codex/p5b2-decision-store-capacity-gc-acceptance"
_PR81_HEAD_SHA = "46fbd515a3694c3759aec8b0723cabcd90877f0e"
_PR81_RUN_ID = 32687650577
_PR81_JOB_ID = 97315530217
_PR81_RUNNER_IMAGE = "ubuntu-24.04"
_PR81_JOB_STARTED_AT = "2026-08-24T03:47:05+00:00"
_PR81_JOB_COMPLETED_AT = "2026-08-24T04:00:30+00:00"
_PR81_SETUP_STARTED_AT = "2026-08-24T03:47:13+00:00"
_PR81_SETUP_COMPLETED_AT = "2026-08-24T03:47:20+00:00"
_PR81_TEST_STARTED_AT = "2026-08-24T03:47:20+00:00"
_PR81_TEST_COMPLETED_AT = "2026-08-24T04:00:26+00:00"
_NORMALIZED_OUTCOMES = frozenset(
    {
        "passed",
        "skipped",
        "xfailed",
        "xpassed",
        "xpassed_strict_failed",
        "failed",
        "error",
        "incomplete",
    }
)
_PASSING_NORMALIZED_OUTCOMES = frozenset({"passed", "skipped", "xfailed", "xpassed"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = _canonical_json_bytes(value) + b"\n"
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _run_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _gh_api_json(endpoint: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", endpoint],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("GitHub API response must be a JSON object")
    return value


def _gh_job_log(run_id: int, job_id: int) -> str:
    result = subprocess.run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "--repo",
            _HOSTED_CI_REPOSITORY,
            "--job",
            str(job_id),
            "--log",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return result.stdout


def _nul_paths(payload: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in payload.split(b"\0") if item]


def _tracked_index_modes(repo_root: Path) -> dict[str, str]:
    records = _run_git(repo_root, "ls-files", "-s", "-z").stdout.split(b"\0")
    modes: dict[str, str] = {}
    for record in records:
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        modes[raw_path.decode("utf-8", "surrogateescape")] = mode
    return modes


def _path_entry(repo_root: Path, relative: str, index_mode: str | None) -> dict[str, Any]:
    path = repo_root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "path": relative,
            "tracked": index_mode is not None,
            "index_mode": index_mode,
            "entry_type": "missing",
            "mode": None,
        }

    mode = stat.S_IMODE(metadata.st_mode)
    common: dict[str, Any] = {
        "path": relative,
        "tracked": index_mode is not None,
        "index_mode": index_mode,
        "mode": f"{mode:04o}",
    }
    if stat.S_ISLNK(metadata.st_mode):
        common.update(entry_type="symlink", target=os.readlink(path))
    elif stat.S_ISREG(metadata.st_mode):
        common.update(entry_type="file", size=metadata.st_size, sha256=_sha256_file(path))
    elif stat.S_ISDIR(metadata.st_mode):
        common.update(entry_type="directory")
    else:
        common.update(entry_type="other")
    return common


def repository_manifest(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    tracked_modes = _tracked_index_modes(repo_root)
    paths: set[str] = set()
    for current_root, directory_names, file_names in os.walk(repo_root, topdown=True):
        current = Path(current_root)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            path = current / name
            relative = path.relative_to(repo_root).as_posix()
            if name in _EXCLUDED_DIRECTORY_NAMES:
                continue
            if path.is_symlink():
                paths.add(relative)
            else:
                paths.add(relative)
                kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(repo_root).as_posix()
            if relative == ".git" or name.endswith(_EXCLUDED_FILE_SUFFIXES):
                continue
            paths.add(relative)
    paths.update(tracked_modes)
    entries = [
        _path_entry(repo_root, relative, tracked_modes.get(relative))
        for relative in sorted(paths)
    ]
    result = {
        "repo_root": str(repo_root),
        "enumeration": "filesystem walk plus tracked-missing reconciliation",
        "excluded_directory_names": sorted(_EXCLUDED_DIRECTORY_NAMES),
        "excluded_file_suffixes": list(_EXCLUDED_FILE_SUFFIXES),
        "entries": entries,
    }
    result["manifest_sha256"] = _sha256_bytes(_canonical_json_bytes(result))
    return result


def _safe_environment(environment: dict[str, str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for key in _SAFE_ENV_KEYS:
        value = environment.get(key)
        if value is not None and (len(value) > 128 or any(ord(character) < 32 for character in value)):
            values[key] = f"sha256:{_sha256_bytes(value.encode('utf-8', 'surrogateescape'))}"
        else:
            values[key] = value
    values["TMPDIR_sha256"] = (
        _sha256_bytes(environment["TMPDIR"].encode("utf-8", "surrogateescape"))
        if environment.get("TMPDIR")
        else None
    )
    values["GRAPHIFY_OUT_present"] = "yes" if environment.get("GRAPHIFY_OUT") else "no"
    values["PYTHONPATH_policy"] = "evaluator-only"
    return values


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {
        "python": sys.version.split()[0],
        "pytest": pytest.__version__,
    }
    for distribution, key in (
        ("pytest-xdist", "pytest_xdist"),
        ("execnet", "execnet"),
        ("anyio", "anyio"),
        ("hypothesis", "hypothesis"),
        ("openai", "openai"),
        ("pytest-cov", "pytest_cov"),
        ("coverage", "coverage"),
    ):
        try:
            versions[key] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[key] = None
    return versions


def _pytest_plugin_entrypoints() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for entrypoint in importlib.metadata.entry_points(group="pytest11"):
        distribution = entrypoint.dist
        if distribution is None:
            raise RuntimeError(f"pytest plugin entry point lacks a distribution: {entrypoint.name}")
        result.append(
            {
                "name": entrypoint.name,
                "value": entrypoint.value,
                "distribution": str(distribution.metadata["Name"]),
                "version": distribution.version,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["name"],
            item["value"],
            item["distribution"],
            item["version"],
        ),
    )


def _normalize_uv_version(value: str) -> str:
    tokens = value.split()
    if len(tokens) < 2 or tokens[0] != "uv":
        raise ValueError("unexpected uv --version output")
    return " ".join(tokens[:2])


def _git_identity(repo_root: Path) -> dict[str, Any]:
    head = _run_git(repo_root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    branch = _run_git(repo_root, "branch", "--show-current").stdout.decode("utf-8").strip()
    status = _run_git(
        repo_root, "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout.decode("utf-8", "surrogateescape").splitlines()
    origin = _run_git(repo_root, "remote", "get-url", "origin").stdout.strip()
    repository_fingerprint = _sha256_bytes(origin)
    host_material = "\0".join(
        (platform.system(), platform.machine(), socket.gethostname())
    ).encode("utf-8", "surrogateescape")
    return {
        "head": head,
        "branch": branch,
        "status": status,
        "repository_fingerprint": repository_fingerprint,
        "host_fingerprint": _sha256_bytes(host_material),
    }


def _phase_record(report: pytest.TestReport) -> dict[str, Any]:
    wasxfail = getattr(report, "wasxfail", None)
    return {
        "when": report.when,
        "outcome": report.outcome,
        "wasxfail_present": wasxfail is not None,
        "strict_xpass": (
            report.when == "call"
            and report.failed
            and str(report.longrepr or "").lstrip().startswith("[XPASS(strict)]")
        ),
    }


def normalize_node_reports(reports: Iterable[dict[str, Any]]) -> str:
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        by_phase.setdefault(str(report["when"]), []).append(report)
    if any(len(values) != 1 for values in by_phase.values()):
        return "incomplete"

    setup = by_phase.get("setup", [None])[0]
    call = by_phase.get("call", [None])[0]
    teardown = by_phase.get("teardown", [None])[0]
    if setup is None or teardown is None:
        return "incomplete"
    if teardown["outcome"] == "failed" or setup["outcome"] == "failed":
        return "error"
    if teardown["outcome"] == "skipped":
        return "incomplete"
    if setup["outcome"] == "skipped":
        return "xfailed" if setup.get("wasxfail_present") else "skipped"
    if call is None:
        return "incomplete"

    outcome = call["outcome"]
    wasxfail_present = bool(call.get("wasxfail_present"))
    if outcome == "skipped" and wasxfail_present:
        return "xfailed"
    if outcome == "passed" and wasxfail_present:
        return "xpassed"
    if outcome == "failed" and (wasxfail_present or call.get("strict_xpass")):
        return "xpassed_strict_failed"
    return {"passed": "passed", "skipped": "skipped", "failed": "failed"}.get(
        str(outcome), "incomplete"
    )


_PLUGIN_STATE: dict[str, Any] = {
    "collect_only": False,
    "plugins": [],
    "serial_collection": [],
    "worker_collections": {},
    "reports": {},
    "worker_errors": [],
}


def _is_xdist_worker() -> bool:
    return bool(os.environ.get("PYTEST_XDIST_WORKER"))


def pytest_configure(config: pytest.Config) -> None:
    if not _is_xdist_worker():
        _PLUGIN_STATE["collect_only"] = bool(config.getoption("collectonly"))
        loaded_entrypoints = [
            entrypoint["name"]
            for entrypoint in _pytest_plugin_entrypoints()
            if config.pluginmanager.hasplugin(entrypoint["name"])
        ]
        _PLUGIN_STATE["plugins"] = sorted([Path(__file__).stem, *loaded_entrypoints])


def pytest_collection_finish(session: pytest.Session) -> None:
    if not _is_xdist_worker():
        _PLUGIN_STATE["serial_collection"] = [item.nodeid for item in session.items]


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: Sequence[str]) -> None:
    worker_id = str(getattr(getattr(node, "gateway", None), "id", "unknown"))
    _PLUGIN_STATE["worker_collections"][worker_id] = list(ids)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    if error:
        worker_id = str(getattr(getattr(node, "gateway", None), "id", "unknown"))
        _PLUGIN_STATE["worker_errors"].append({"worker": worker_id, "error": str(error)})


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _is_xdist_worker():
        return
    _PLUGIN_STATE["reports"].setdefault(report.nodeid, []).append(_phase_record(report))


def _resolved_collection() -> tuple[list[str], bool, dict[str, list[str]]]:
    worker_collections: dict[str, list[str]] = _PLUGIN_STATE["worker_collections"]
    if worker_collections:
        ordered = [worker_collections[key] for key in sorted(worker_collections)]
        first = ordered[0]
        return first, all(item == first for item in ordered[1:]), worker_collections
    serial = list(_PLUGIN_STATE["serial_collection"])
    return serial, True, {}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    if _is_xdist_worker():
        return
    artifact_value = os.environ.get(_PLUGIN_ARTIFACT_ENV)
    if not artifact_value:
        return
    collection, consistent, worker_collections = _resolved_collection()
    duplicate_collection = len(collection) != len(set(collection))
    reports: dict[str, list[dict[str, Any]]] = _PLUGIN_STATE["reports"]
    collect_only = bool(_PLUGIN_STATE["collect_only"])
    outcomes = (
        {}
        if collect_only
        else {nodeid: normalize_node_reports(reports.get(nodeid, [])) for nodeid in collection}
    )
    extra_reports = sorted(set(reports) - set(collection))
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    complete = (
        bool(collection)
        and consistent
        and not duplicate_collection
        and not extra_reports
        and not _PLUGIN_STATE["worker_errors"]
        and (collect_only or all(outcome != "incomplete" for outcome in outcomes.values()))
    )
    payload = {
        "schema": RUN_SCHEMA,
        "evaluator_sha256": os.environ.get(_EVALUATOR_DIGEST_ENV),
        "collection_order": collection,
        "collection_consistent": consistent,
        "worker_collections": worker_collections,
        "duplicate_collection": duplicate_collection,
        "outcomes": outcomes,
        "counts": counts,
        "extra_reports": extra_reports,
        "worker_errors": _PLUGIN_STATE["worker_errors"],
        "collect_only": collect_only,
        "child_versions": _package_versions(),
        "pytest_plugin_entrypoints": _pytest_plugin_entrypoints(),
        "plugin_names": _PLUGIN_STATE["plugins"],
        "pytest_exitstatus": int(exitstatus),
        "complete": complete,
        "passed": complete and int(exitstatus) == 0,
    }
    _atomic_write_json(Path(artifact_value), payload)


def _is_executable_token(token: str, executable: str) -> bool:
    return Path(token).name in {executable, f"{executable}.exe"}


def _inject_plugin(command: Sequence[str], plugin_name: str) -> list[str]:
    result = list(command)
    injected = ["-p", plugin_name]
    for index, token in enumerate(result):
        if _is_executable_token(token, "pytest"):
            return [*result[: index + 1], *injected, *result[index + 1 :]]
    raise ValueError("pytest executable/module token not found in command")


def _cohort_paths(cohort_file: Path | None) -> tuple[list[str], str | None]:
    if cohort_file is None:
        return [], None
    payload = cohort_file.read_bytes()
    paths = [
        line.strip()
        for line in payload.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("hazard cohort must contain unique nonempty paths")
    for path in paths:
        if Path(path).is_absolute() or ".." in Path(path).parts or not path.startswith("tests/"):
            raise ValueError(f"invalid hazard cohort path: {path}")
    return paths, _sha256_bytes(payload)


def _validate_measurement_command(
    command: Sequence[str],
    artifact_root: Path,
    *,
    kind: str,
    cohort_paths: Sequence[str],
) -> None:
    if kind == "integration":
        if not any(_is_executable_token(token, "pytest") for token in command):
            raise ValueError("integration command must invoke pytest")
        return
    if (
        len(command) < 4
        or not _is_executable_token(command[0], "uv")
        or command[1:3] != ["run", "--frozen"]
        or not _is_executable_token(command[3], "pytest")
    ):
        raise ValueError("canonical commands must start with: uv run --frozen pytest")
    if any(token.split("=", 1)[0] in _FORBIDDEN_PYTEST_FLAGS for token in command):
        raise ValueError("pytest selectors, early-exit flags, and deselection are forbidden")

    values_with_argument = {"-p", "--basetemp", "-n", "--dist", "--max-worker-restart", "--tb"}
    selections: list[str] = []
    cache_disabled = False
    basetemp: Path | None = None
    collect_only = False
    quiet = False
    traceback_short = False
    index = 4
    while index < len(command):
        token = command[index]
        if token in values_with_argument:
            if index + 1 >= len(command):
                raise ValueError(f"missing value for {token}")
            value = command[index + 1]
            if token == "-p":
                if value != "no:cacheprovider":
                    raise ValueError("only -p no:cacheprovider is allowed")
                cache_disabled = True
            elif token == "--basetemp":
                basetemp = Path(value).resolve()
            elif token == "-n" and value not in {"2", "4"}:
                raise ValueError("worker count must be 2 or 4")
            elif token == "--dist" and value not in {"loadfile", "load"}:
                raise ValueError("distribution must be loadfile or load")
            elif token == "--max-worker-restart" and value != "0":
                raise ValueError("worker restarts must be zero")
            elif token == "--tb" and value != "short":
                raise ValueError("traceback style must be short")
            elif token == "--tb":
                traceback_short = True
            index += 2
            continue
        if token == "-q" or token == "--collect-only":
            quiet = quiet or token == "-q"
            collect_only = collect_only or token == "--collect-only"
            index += 1
            continue
        if token.startswith("--basetemp="):
            basetemp = Path(token.split("=", 1)[1]).resolve()
        elif token.startswith("--dist="):
            if token.split("=", 1)[1] not in {"loadfile", "load"}:
                raise ValueError("distribution must be loadfile or load")
        elif token.startswith("--max-worker-restart="):
            if token.split("=", 1)[1] != "0":
                raise ValueError("worker restarts must be zero")
        elif token.startswith("--tb="):
            if token.split("=", 1)[1] != "short":
                raise ValueError("traceback style must be short")
            traceback_short = True
        elif token.startswith("-n") and token != "-n":
            if token[2:] not in {"2", "4"}:
                raise ValueError("worker count must be 2 or 4")
        elif token.startswith("-"):
            raise ValueError(f"unapproved pytest flag: {token}")
        else:
            selections.append(token)
        index += 1

    if not cache_disabled:
        raise ValueError("measurement commands require -p no:cacheprovider")
    if not quiet or not traceback_short:
        raise ValueError("measurement commands require -q --tb=short")
    if basetemp is None or not basetemp.is_relative_to(artifact_root.resolve()):
        raise ValueError("measurement --basetemp must be inside the external artifact root")
    expected_collect = kind.startswith("collect-")
    if collect_only != expected_collect:
        raise ValueError("collect-only flag does not match the run kind")
    expected_selections = ["tests/"] if kind in {"collect-full", "full"} else list(cohort_paths)
    if selections != expected_selections:
        raise ValueError("pytest selection differs from the exact run-kind selection")
    has_workers = any(token == "-n" or token.startswith("-n") for token in command)
    if expected_collect and has_workers:
        raise ValueError("preflight collection must remain serial")
    has_distribution = any(token == "--dist" or token.startswith("--dist=") for token in command)
    has_restart_policy = any(
        token == "--max-worker-restart" or token.startswith("--max-worker-restart=")
        for token in command
    )
    if has_workers:
        _parallel_config(command)
    elif has_distribution or has_restart_policy:
        raise ValueError("serial commands cannot carry xdist scheduler settings")


def _assert_windows_job_abi() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    expected_basic_size = {4: 48, 8: 64}.get(pointer_size)
    expected_extended_size = {4: 112, 8: 144}.get(pointer_size)
    if (
        expected_basic_size is None
        or expected_extended_size is None
        or ctypes.sizeof(_WindowsJobBasicLimitInformation) != expected_basic_size
        or ctypes.sizeof(_WindowsJobExtendedLimitInformation) != expected_extended_size
        or _WindowsJobBasicLimitInformation.LimitFlags.offset != 16
    ):
        raise OSError("unsupported Windows Job Object ABI layout")


def _windows_kernel32() -> Any:
    _assert_windows_job_abi()
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows Job Objects require ctypes.WinDLL")
    kernel32 = loader("kernel32", use_last_error=True)
    handle_type = ctypes.c_void_p
    bool_type = ctypes.c_int32
    dword_type = ctypes.c_uint32

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = handle_type
    kernel32.SetInformationJobObject.argtypes = [
        handle_type,
        ctypes.c_int32,
        ctypes.c_void_p,
        dword_type,
    ]
    kernel32.SetInformationJobObject.restype = bool_type
    kernel32.OpenProcess.argtypes = [dword_type, bool_type, dword_type]
    kernel32.OpenProcess.restype = handle_type
    kernel32.AssignProcessToJobObject.argtypes = [handle_type, handle_type]
    kernel32.AssignProcessToJobObject.restype = bool_type
    kernel32.TerminateJobObject.argtypes = [handle_type, dword_type]
    kernel32.TerminateJobObject.restype = bool_type
    kernel32.CloseHandle.argtypes = [handle_type]
    kernel32.CloseHandle.restype = bool_type
    return kernel32


def _windows_native_error(operation: str) -> OSError:
    get_last_error = getattr(ctypes, "get_last_error", None)
    error_code = cast(Callable[[], int], get_last_error)() if callable(get_last_error) else 0
    return OSError(error_code, f"{operation} failed")


def _close_windows_job(job: _WindowsJob) -> None:
    if job.closed:
        return
    if not job.kernel32.CloseHandle(job.handle):
        raise _windows_native_error("CloseHandle(job)")
    job.closed = True


def _create_windows_job() -> _WindowsJob:
    kernel32 = _windows_kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise _windows_native_error("CreateJobObjectW")
    job = _WindowsJob(kernel32=kernel32, handle=handle)
    limits = _WindowsJobExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        configuration_error = _windows_native_error("SetInformationJobObject")
        try:
            _close_windows_job(job)
        except OSError as close_error:
            raise close_error from configuration_error
        raise configuration_error
    return job


def _assign_windows_process(job: _WindowsJob, process_id: int) -> None:
    process_handle = job.kernel32.OpenProcess(
        _WINDOWS_PROCESS_SET_QUOTA | _WINDOWS_PROCESS_TERMINATE,
        False,
        process_id,
    )
    if not process_handle:
        raise _windows_native_error("OpenProcess")
    assignment_error = (
        None
        if job.kernel32.AssignProcessToJobObject(job.handle, process_handle)
        else _windows_native_error("AssignProcessToJobObject")
    )
    close_error = (
        None
        if job.kernel32.CloseHandle(process_handle)
        else _windows_native_error("CloseHandle(process)")
    )
    if close_error is not None:
        if assignment_error is not None:
            raise close_error from assignment_error
        raise close_error
    if assignment_error is not None:
        raise assignment_error


def _terminate_windows_job(job: _WindowsJob) -> None:
    if not job.kernel32.TerminateJobObject(
        job.handle,
        _WINDOWS_JOB_TERMINATION_EXIT_CODE,
    ):
        raise _windows_native_error("TerminateJobObject")


def _wait_for_windows_helper(process: subprocess.Popen[Any]) -> None:
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise OSError("Windows job helper did not terminate") from exc


def _stop_unassigned_windows_helper(process: subprocess.Popen[Any]) -> None:
    stream = process.stdin
    close_error: BaseException | None = None
    if stream is not None and not getattr(stream, "closed", False):
        try:
            stream.close()
        except BaseException as exc:
            close_error = exc
    try:
        if process.poll() is None:
            process.terminate()
        _wait_for_windows_helper(process)
    except BaseException as stop_error:
        if close_error is not None:
            raise stop_error from close_error
        raise
    if close_error is not None:
        raise close_error


def _execute_windows(
    command: Sequence[str],
    repo_root: Path,
    environment: dict[str, str],
    timeout: float,
) -> tuple[int, bool]:
    job = _create_windows_job()
    process: subprocess.Popen[str] | None = None
    assigned = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _WINDOWS_JOB_LAUNCHER],
            cwd=repo_root,
            env=environment,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        _assign_windows_process(job, process.pid)
        assigned = True
        if process.stdin is None:
            raise OSError("Windows job helper stdin is unavailable")
        try:
            process.stdin.write(json.dumps(list(command), separators=(",", ":")) + "\n")
            process.stdin.flush()
        finally:
            process.stdin.close()
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            _terminate_windows_job(job)
            _wait_for_windows_helper(process)
            return 124, True
    except BaseException as operation_error:
        if process is not None:
            try:
                if assigned:
                    _terminate_windows_job(job)
                    _wait_for_windows_helper(process)
                else:
                    _stop_unassigned_windows_helper(process)
            except BaseException as cleanup_error:
                raise cleanup_error from operation_error
        raise
    finally:
        _close_windows_job(job)


def _process_group_signalable(process_group_id: int) -> bool | None:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def _verify_process_group_gone_after_reap(process: subprocess.Popen[Any]) -> None:
    process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while _process_group_signalable(process.pid) is not False:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("POSIX process group cleanup could not be verified")
        time.sleep(min(0.05, remaining))


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    deadline = time.monotonic() + 5
    while True:
        group_signalable = _process_group_signalable(process.pid)
        if group_signalable is False:
            process.wait(timeout=5)
            return
        if group_signalable is None:
            _verify_process_group_gone_after_reap(process)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    _verify_process_group_gone_after_reap(process)


def _execute(command: Sequence[str], repo_root: Path, environment: dict[str, str], timeout: float) -> tuple[int, bool]:
    if os.name == "nt":
        return _execute_windows(command, repo_root, environment, timeout)
    process = subprocess.Popen(
        list(command),
        cwd=repo_root,
        env=environment,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        return 124, True


def run_pytest(
    repo_root: Path,
    artifact_root: Path,
    command: Sequence[str],
    *,
    kind: str = "integration",
    timeout_seconds: float = 300.0,
    preflight_artifact: Path | None = None,
    cohort_file: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    artifact_root = artifact_root.resolve()
    if artifact_root.is_relative_to(repo_root):
        raise ValueError("artifact root must be outside the repository")
    if artifact_root.exists() and any(artifact_root.iterdir()):
        raise ValueError("artifact root must be new or empty")
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be finite and positive")
    for variable in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTHONPATH",
    ):
        if os.environ.get(variable):
            raise ValueError(f"ambient {variable} is forbidden")
    cohort_paths, cohort_sha256 = _cohort_paths(cohort_file)
    if kind in {"collect-hazard", "hazard"} and not cohort_paths:
        raise ValueError("hazard runs require a cohort file")
    if kind not in {"collect-hazard", "hazard"} and cohort_paths:
        raise ValueError("cohort file is valid only for hazard run kinds")
    _validate_measurement_command(
        command,
        artifact_root,
        kind=kind,
        cohort_paths=cohort_paths,
    )

    evaluator_path = Path(__file__).resolve()
    evaluator_digest = _sha256_file(evaluator_path)
    plugin_path = artifact_root / "pytest-plugin.json"
    final_path = artifact_root / "run.json"
    environment = os.environ.copy()
    for variable in _OUTER_XDIST_ENV_KEYS:
        environment.pop(variable, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTEST_PLUGINS"] = ""
    environment.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    environment[_PLUGIN_ARTIFACT_ENV] = str(plugin_path)
    environment[_EVALUATOR_DIGEST_ENV] = evaluator_digest
    environment["PYTHONPATH"] = str(evaluator_path.parent)
    executed_command = _inject_plugin(command, evaluator_path.stem)
    uv_version = None
    if kind != "integration" and _is_executable_token(command[0], "uv"):
        uv_result = subprocess.run(
            [command[0], "--version"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
        uv_version = _normalize_uv_version(uv_result.stdout)

    before_manifest = repository_manifest(repo_root)
    identity_before = _git_identity(repo_root)
    preflight: dict[str, Any] | None = None
    preflight_sha256: str | None = None
    if kind in {"full", "hazard"}:
        if preflight_artifact is None:
            raise ValueError("scored runs require a collect preflight artifact")
        preflight_sha256 = _sha256_file(preflight_artifact)
        preflight = _load_json(preflight_artifact)
        expected_preflight_kind = "collect-full" if kind == "full" else "collect-hazard"
        if (
            preflight.get("schema") != RUN_SCHEMA
            or preflight.get("kind") != expected_preflight_kind
            or preflight.get("evaluator_sha256") != evaluator_digest
            or not preflight.get("complete")
            or not preflight.get("passed")
            or preflight.get("manifest_before", {}).get("manifest_sha256")
            != before_manifest.get("manifest_sha256")
            or preflight.get("git_before") != identity_before
        ):
            raise ValueError("collect preflight does not bind to the scored run")
    started_at = _utc_now()
    wall_start = time.perf_counter()
    process_start = os.times()
    child_exit_code, timed_out = _execute(
        executed_command,
        repo_root,
        environment,
        timeout_seconds,
    )
    process_end = os.times()
    wall_seconds = time.perf_counter() - wall_start
    completed_at = _utc_now()
    after_manifest = repository_manifest(repo_root)
    identity_after = _git_identity(repo_root)

    plugin_payload: dict[str, Any]
    if plugin_path.is_file():
        try:
            plugin_payload = json.loads(plugin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            plugin_payload = {"complete": False, "passed": False, "artifact_error": str(exc)}
    else:
        plugin_payload = {
            "schema": RUN_SCHEMA,
            "evaluator_sha256": evaluator_digest,
            "complete": False,
            "passed": False,
            "artifact_error": "pytest plugin artifact missing",
        }

    manifest_stable = before_manifest == after_manifest
    identity_stable = identity_before == identity_after
    wrapper_complete = (
        plugin_payload.get("schema") == RUN_SCHEMA
        and plugin_payload.get("evaluator_sha256") == evaluator_digest
        and bool(plugin_payload.get("complete"))
    )
    payload = {
        **plugin_payload,
        "schema": RUN_SCHEMA,
        "evaluator_sha256": evaluator_digest,
        "kind": kind,
        "requested_argv": list(command),
        "executed_argv": executed_command,
        "repo_root": str(repo_root),
        "artifact_root": str(artifact_root),
        "git_before": identity_before,
        "git_after": identity_after,
        "manifest_before": before_manifest,
        "manifest_after": after_manifest,
        "manifest_stable": manifest_stable,
        "identity_stable": identity_stable,
        "environment": _safe_environment(environment),
        "wrapper_versions": _package_versions(),
        "uv_version": uv_version,
        "started_at": started_at,
        "completed_at": completed_at,
        "wall_seconds": wall_seconds,
        "user_seconds": process_end.children_user - process_start.children_user,
        "system_seconds": process_end.children_system - process_start.children_system,
        "timing_capability": "os.times children_user/children_system",
        "child_exit_code": child_exit_code,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "preflight_artifact_sha256": preflight_sha256,
        "cohort_sha256": cohort_sha256,
        "cohort_paths": cohort_paths,
        "complete": wrapper_complete,
        "passed": (
            wrapper_complete
            and bool(plugin_payload.get("passed"))
            and child_exit_code == 0
            and not timed_out
            and manifest_stable
            and identity_stable
            and (
                preflight is None
                or preflight.get("collection_order") == plugin_payload.get("collection_order")
            )
        ),
    }
    _atomic_write_json(final_path, payload)
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _resolve_artifact_path(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else manifest_path.parent / path


def _load_artifact_ref(manifest_path: Path, reference: Any) -> tuple[dict[str, Any], str, Path]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError("artifact reference must contain exactly path and sha256")
    path = _resolve_artifact_path(manifest_path, str(reference["path"])).resolve()
    expected_sha256 = str(reference["sha256"])
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"artifact digest mismatch: {path}")
    return _load_json(path), actual_sha256, path


def _finite_positive(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validate_repository_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("repository manifest must be an object")
    stored_sha256 = manifest.get("manifest_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    if stored_sha256 != _sha256_bytes(_canonical_json_bytes(unsigned)):
        raise ValueError("repository manifest digest mismatch")
    if manifest.get("excluded_directory_names") != sorted(_EXCLUDED_DIRECTORY_NAMES):
        raise ValueError("repository directory exclusion policy mismatch")
    if manifest.get("excluded_file_suffixes") != list(_EXCLUDED_FILE_SUFFIXES):
        raise ValueError("repository file exclusion policy mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("repository manifest entries missing")
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("repository manifest entry is malformed")
        paths.append(entry["path"])
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("repository manifest paths are not unique and sorted")


def _manifest_file_digest(manifest: dict[str, Any], relative_path: str) -> str:
    matches = [
        entry
        for entry in manifest["entries"]
        if entry.get("path") == relative_path
    ]
    if len(matches) != 1:
        raise ValueError(f"repository manifest lacks exact file entry: {relative_path}")
    entry = matches[0]
    digest = entry.get("sha256")
    if (
        entry.get("entry_type") != "file"
        or entry.get("tracked") is not True
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise ValueError(f"repository manifest file entry is not committed: {relative_path}")
    return digest


def _committed_hazard_cohort(
    repo_root: Path, candidate_manifest: dict[str, Any]
) -> tuple[list[str], str]:
    cohort_paths, cohort_sha256 = _cohort_paths(repo_root / _HAZARD_COHORT_PATH)
    if cohort_sha256 is None:
        raise ValueError("committed hazard cohort digest is missing")
    if cohort_sha256 != _manifest_file_digest(
        candidate_manifest, _HAZARD_COHORT_PATH
    ):
        raise ValueError("active hazard cohort bytes differ from the committed candidate")
    return cohort_paths, cohort_sha256


def _validate_run_artifact(
    run: dict[str, Any],
    evaluator_digest: str,
    *,
    passed: bool = True,
) -> None:
    required_keys = {
        "schema",
        "evaluator_sha256",
        "kind",
        "requested_argv",
        "executed_argv",
        "repo_root",
        "artifact_root",
        "git_before",
        "git_after",
        "manifest_before",
        "manifest_after",
        "manifest_stable",
        "identity_stable",
        "started_at",
        "completed_at",
        "wall_seconds",
        "child_exit_code",
        "timed_out",
        "complete",
        "passed",
        "collection_order",
        "collection_consistent",
        "worker_collections",
        "duplicate_collection",
        "outcomes",
        "counts",
        "extra_reports",
        "worker_errors",
        "pytest_exitstatus",
        "child_versions",
        "plugin_names",
        "pytest_plugin_entrypoints",
        "environment",
        "uv_version",
        "collect_only",
        "preflight_artifact_sha256",
        "cohort_sha256",
        "cohort_paths",
    }
    if not required_keys.issubset(run):
        raise ValueError("run artifact is missing required schema fields")
    if run.get("schema") != RUN_SCHEMA:
        raise ValueError("run schema mismatch")
    if run.get("evaluator_sha256") != evaluator_digest:
        raise ValueError("evaluator digest mismatch")
    if not run.get("complete"):
        raise ValueError("run artifact incomplete")
    if passed and not run.get("passed"):
        raise ValueError("required run did not pass")
    if not run.get("manifest_stable") or not run.get("identity_stable"):
        raise ValueError("repository manifest or identity changed during run")
    if run.get("timed_out") or int(run.get("child_exit_code", -1)) != 0:
        raise ValueError("required run timed out or exited nonzero")
    _validate_repository_manifest(run["manifest_before"])
    _validate_repository_manifest(run["manifest_after"])
    if run["manifest_before"] != run["manifest_after"]:
        raise ValueError("repository manifest changed during run")
    if run.get("git_before") != run.get("git_after"):
        raise ValueError("Git identity changed during run")
    wall = _finite_positive(run["wall_seconds"], "run wall time")
    started = _parse_utc(run["started_at"])
    completed = _parse_utc(run["completed_at"])
    timestamp_wall = (completed - started).total_seconds()
    if timestamp_wall <= 0 or abs(timestamp_wall - wall) > max(5.0, wall * 0.01):
        raise ValueError("monotonic and UTC wall durations disagree")
    collection = run.get("collection_order")
    outcomes = run.get("outcomes")
    counts = run.get("counts")
    if (
        not isinstance(collection, list)
        or not all(isinstance(nodeid, str) and nodeid for nodeid in collection)
        or len(collection) != len(set(collection))
    ):
        raise ValueError("collection order is invalid or duplicated")
    if (
        run.get("collection_consistent") is not True
        or run.get("duplicate_collection") is not False
        or run.get("extra_reports") != []
        or run.get("worker_errors") != []
        or int(run.get("pytest_exitstatus", -1)) != 0
    ):
        raise ValueError("pytest plugin lifecycle evidence is incomplete or inconsistent")
    worker_collections = run.get("worker_collections")
    if not isinstance(worker_collections, dict):
        raise ValueError("worker collection evidence is missing")
    parallel = any(
        token == "-n" or token.startswith("-n") for token in run.get("requested_argv", [])
    )
    if parallel:
        workers, _, _ = _parallel_config(run["requested_argv"])
        if len(worker_collections) != workers or any(
            worker_collection != collection
            for worker_collection in worker_collections.values()
        ):
            raise ValueError("xdist workers did not collect the exact ordered node list")
    elif worker_collections:
        raise ValueError("serial run unexpectedly contains xdist worker collections")
    if not isinstance(outcomes, dict) or not isinstance(counts, dict):
        raise ValueError("outcomes/counts are invalid")
    if run.get("kind", "").startswith("collect-"):
        if outcomes or counts or not run.get("collect_only"):
            raise ValueError("collect preflight contains execution outcomes")
    else:
        if set(outcomes) != set(collection):
            raise ValueError("per-node outcomes do not cover the exact collection")
        recomputed_counts: dict[str, int] = {}
        for outcome in outcomes.values():
            if not isinstance(outcome, str) or outcome not in _NORMALIZED_OUTCOMES:
                raise ValueError("per-node outcome is outside the frozen normalization vocabulary")
            if outcome == "incomplete" or (passed and outcome not in _PASSING_NORMALIZED_OUTCOMES):
                raise ValueError("required passing artifact contains a non-passing node outcome")
            recomputed_counts[str(outcome)] = recomputed_counts.get(str(outcome), 0) + 1
        if counts != recomputed_counts:
            raise ValueError("aggregate outcome counts do not match per-node outcomes")
    kind = str(run["kind"])
    cohort_paths = run.get("cohort_paths") or []
    _validate_measurement_command(
        run["requested_argv"],
        Path(str(run["artifact_root"])),
        kind=kind,
        cohort_paths=cohort_paths,
    )
    if run["executed_argv"] != _inject_plugin(
        run["requested_argv"], Path(__file__).resolve().stem
    ):
        raise ValueError("executed command does not match deterministic plugin injection")
    child_versions = run.get("child_versions")
    if not isinstance(child_versions, dict) or not child_versions.get("python") or not child_versions.get("pytest"):
        raise ValueError("executed pytest process versions are missing")


def _parallel_config(argv: Sequence[str]) -> tuple[int, str, int]:
    workers: int | None = None
    distribution: str | None = None
    max_restarts: int | None = None
    for index, token in enumerate(argv):
        if token == "-n" and index + 1 < len(argv):
            workers = int(argv[index + 1])
        elif token.startswith("-n") and token != "-n":
            workers = int(token[2:])
        elif token == "--dist" and index + 1 < len(argv):
            distribution = argv[index + 1]
        elif token.startswith("--dist="):
            distribution = token.split("=", 1)[1]
        elif token == "--max-worker-restart" and index + 1 < len(argv):
            max_restarts = int(argv[index + 1])
        elif token.startswith("--max-worker-restart="):
            max_restarts = int(token.split("=", 1)[1])
    if workers is None or distribution is None or max_restarts is None:
        raise ValueError("parallel command lacks explicit worker/distribution/restart settings")
    return workers, distribution, max_restarts


def _assert_serial_command(argv: Sequence[str]) -> None:
    forbidden = (
        "-n",
        "--dist",
        "--max-worker-restart",
    )
    for token in argv:
        if any(token == flag or token.startswith(f"{flag}=") for flag in forbidden):
            raise ValueError("serial control contains xdist execution settings")
        if token.startswith("-n") and token != "-n":
            raise ValueError("serial control contains xdist execution settings")


def _status_paths(status: Any) -> set[str]:
    if not isinstance(status, list) or not all(isinstance(line, str) for line in status):
        raise ValueError("expected Git status must be a list of porcelain-v1 lines")
    paths: set[str] = set()
    for line in status:
        if len(line) < 4 or line[2] != " ":
            raise ValueError("malformed porcelain-v1 status line")
        path = line[3:]
        if " -> " in path or not path:
            raise ValueError("renamed or empty status paths are unsupported")
        paths.add(path)
    return paths


def _require_clean_candidate_status(status: Any) -> None:
    if status != []:
        raise ValueError("final candidate status must be exactly clean")


def _assert_added_node_files(
    allowed_added: set[str],
    allowed_files: Any,
    expected_status: Any,
) -> None:
    if (
        not isinstance(allowed_files, list)
        or allowed_files != sorted(set(allowed_files))
        or not all(isinstance(path, str) for path in allowed_files)
    ):
        raise ValueError("allowed added test files must be a sorted unique list")
    file_set = set(allowed_files)
    node_files = {nodeid.split("::", 1)[0] for nodeid in allowed_added}
    if node_files != file_set:
        raise ValueError("allowed added nodes are not bound to the exact added test files")
    changed_paths = _status_paths(expected_status)
    for path in file_set:
        candidate = Path(path)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or not path.startswith("tests/test_")
            or candidate.suffix != ".py"
            or path not in changed_paths
        ):
            raise ValueError("allowed added node file is not an in-scope changed test file")


def _assert_snapshot_transition(
    matrix_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    allowed_transition_files: Any,
) -> None:
    if allowed_transition_files != _ALLOWED_SNAPSHOT_TRANSITION_FILES:
        raise ValueError("matrix-to-candidate transition allowlist differs from the protocol")
    allowed = set(_ALLOWED_SNAPSHOT_TRANSITION_FILES)

    def normalized(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in manifest["entries"]:
            path = str(entry["path"])
            if path in allowed:
                continue
            result[path] = {
                key: value
                for key, value in entry.items()
                if key not in {"tracked", "index_mode"}
            }
        return result

    matrix_paths = {str(entry["path"]) for entry in matrix_manifest["entries"]}
    candidate_paths = {str(entry["path"]) for entry in candidate_manifest["entries"]}
    if matrix_paths != candidate_paths:
        raise ValueError("matrix and candidate snapshots contain different repository paths")
    if not allowed.issubset(matrix_paths):
        raise ValueError("matrix-to-candidate transition files are missing")
    if normalized(matrix_manifest) != normalized(candidate_manifest):
        raise ValueError("matrix and candidate source/test bytes differ")


def _assert_environment_and_versions(
    run: dict[str, Any],
    *,
    expected_environment: Any,
    expected_versions: Any,
    expected_plugin_entrypoints: Any,
    expected_uv_version: str,
) -> None:
    if not isinstance(expected_environment, dict) or run.get("environment") != expected_environment:
        raise ValueError("run environment differs from the frozen safe environment")
    if (
        expected_environment.get("PYTHONDONTWRITEBYTECODE") != "1"
        or expected_environment.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") is not None
        or expected_environment.get("PYTEST_PLUGINS") != ""
        or expected_environment.get("PYTHONPATH_policy") != "evaluator-only"
    ):
        raise ValueError("frozen environment does not isolate pytest plugins and PYTHONPATH")
    versions = run.get("child_versions")
    if (
        not isinstance(expected_versions, dict)
        or not isinstance(versions, dict)
        or versions != expected_versions
    ):
        raise ValueError("pytest child versions differ from the frozen environment")
    if (
        not _is_supported_python_version(versions.get("python"))
        or versions.get("pytest") != "9.0.3"
        or run.get("uv_version") != expected_uv_version
        or expected_uv_version != "uv 0.11.30"
    ):
        raise ValueError("Python, pytest, or uv version differs from the evaluator contract")
    if run.get("pytest_plugin_entrypoints") != expected_plugin_entrypoints:
        raise ValueError("installed pytest plugin entry points differ from the frozen lock")
    expected_plugin_names = sorted(
        [Path(__file__).stem, *(item["name"] for item in expected_plugin_entrypoints)]
    )
    if run.get("plugin_names") != expected_plugin_names:
        raise ValueError("loaded pytest plugin identity differs from the canonical environment")


def _is_supported_python_version(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    if len(parts) != 3 or parts[:2] != ["3", "14"]:
        return False
    patch = parts[2]
    return (
        patch.isascii()
        and patch.isdecimal()
        and (patch == "0" or not patch.startswith("0"))
    )


def _assert_comparable_python_versions(
    baseline_versions: Any,
    candidate_versions: Any,
) -> None:
    baseline_python = (
        baseline_versions.get("python") if isinstance(baseline_versions, dict) else None
    )
    candidate_python = (
        candidate_versions.get("python") if isinstance(candidate_versions, dict) else None
    )
    if (
        baseline_python != candidate_python
        or not _is_supported_python_version(baseline_python)
    ):
        raise ValueError("baseline and candidate must use the same supported Python patch")


def _assert_parity(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    allowed_added: set[str],
) -> None:
    baseline_order = list(baseline["collection_order"])
    candidate_order = list(candidate["collection_order"])
    baseline_set = set(baseline_order)
    candidate_set = set(candidate_order)
    if baseline_set - candidate_set:
        raise ValueError("candidate is missing baseline node IDs")
    if candidate_set - baseline_set != allowed_added:
        raise ValueError("candidate added-node set differs from frozen allowlist")
    if [nodeid for nodeid in candidate_order if nodeid not in allowed_added] != baseline_order:
        raise ValueError("candidate changed the exact baseline collection order")
    for nodeid in baseline_order:
        if baseline["outcomes"].get(nodeid) != candidate["outcomes"].get(nodeid):
            raise ValueError(f"per-node outcome mismatch: {nodeid}")
    for nodeid in allowed_added:
        if candidate["outcomes"].get(nodeid) != "passed":
            raise ValueError(f"task-added node did not pass: {nodeid}")


def _assert_hazard_outcome_parity(
    hazards: Sequence[dict[str, Any]], winner: dict[str, Any]
) -> None:
    collection = hazards[0]["collection_order"]
    expected = {nodeid: winner["outcomes"].get(nodeid) for nodeid in collection}
    if any(run["outcomes"] != expected for run in hazards):
        raise ValueError("hazard outcomes differ across runs or from the winning full suite")


def _config_improvement(
    run: dict[str, Any], baseline: dict[str, Any], final_serial: dict[str, Any]
) -> tuple[float, float]:
    wall = _finite_positive(run["wall_seconds"], "matrix wall time")
    return (
        1.0 - wall / _finite_positive(baseline["wall_seconds"], "baseline wall time"),
        1.0 - wall / _finite_positive(final_serial["wall_seconds"], "serial control wall time"),
    )


def _validate_gate_context(
    artifact: dict[str, Any],
    gate_name: str,
    candidate_head: str,
    candidate_manifest_sha256: str,
) -> None:
    if (
        artifact.get("gate") != gate_name
        or artifact.get("status") != "pass"
        or artifact.get("candidate_head") != candidate_head
        or artifact.get("candidate_manifest_sha256") != candidate_manifest_sha256
    ):
        raise ValueError(f"gate evidence is not bound to the candidate: {gate_name}")


def _validate_local_gate_evidence(
    artifact: dict[str, Any],
    gate_name: str,
    candidate_head: str,
    candidate_manifest_sha256: str,
) -> None:
    if set(artifact) != {
        "schema",
        "gate",
        "status",
        "candidate_head",
        "candidate_manifest_sha256",
        "results",
    } or artifact.get("schema") != LOCAL_GATE_EVIDENCE_SCHEMA:
        raise ValueError(f"local gate evidence schema is invalid: {gate_name}")
    _validate_gate_context(
        artifact, gate_name, candidate_head, candidate_manifest_sha256
    )
    results = artifact.get("results")
    expected_commands = _LOCAL_GATE_COMMANDS[gate_name]
    if not isinstance(results, list) or len(results) != len(expected_commands):
        raise ValueError(f"local gate result count is invalid: {gate_name}")
    for result, expected_argv in zip(results, expected_commands, strict=True):
        if not isinstance(result, dict) or set(result) != {
            "argv",
            "exit_code",
            "conclusion",
            "started_at",
            "completed_at",
            "output",
            "output_sha256",
        }:
            raise ValueError(f"local gate result schema is invalid: {gate_name}")
        output = result.get("output")
        if (
            result.get("argv") != expected_argv
            or result.get("exit_code") != 0
            or result.get("conclusion") != "success"
            or not isinstance(output, str)
            or result.get("output_sha256")
            != _sha256_bytes(output.encode("utf-8", "surrogateescape"))
        ):
            raise ValueError(f"local gate command did not pass exactly: {gate_name}")
        if _parse_utc(result["completed_at"]) < _parse_utc(result["started_at"]):
            raise ValueError(f"local gate timestamps are reversed: {gate_name}")


def _validate_review_evidence(
    artifact: dict[str, Any],
    candidate_head: str,
    candidate_manifest_sha256: str,
) -> None:
    gate_name = "independent_review"
    if set(artifact) != {
        "schema",
        "gate",
        "status",
        "candidate_head",
        "candidate_manifest_sha256",
        "reviewed_paths",
        "code_reviewer",
        "architect",
    } or artifact.get("schema") != REVIEW_EVIDENCE_SCHEMA:
        raise ValueError("independent review evidence schema is invalid")
    _validate_gate_context(
        artifact, gate_name, candidate_head, candidate_manifest_sha256
    )
    if artifact.get("reviewed_paths") != _REVIEWED_PATHS:
        raise ValueError("independent review did not cover the exact candidate paths")
    code_reviewer = artifact.get("code_reviewer")
    architect = artifact.get("architect")
    lane_keys = {"lane_id", "agent_type", "verdict", "findings", "report", "report_sha256"}
    if (
        not isinstance(code_reviewer, dict)
        or set(code_reviewer) != lane_keys
        or code_reviewer.get("agent_type") != "code-reviewer"
        or code_reviewer.get("verdict") != "APPROVE"
        or code_reviewer.get("findings") != []
    ):
        raise ValueError("code-reviewer lane did not approve cleanly")
    if (
        not isinstance(architect, dict)
        or set(architect) != lane_keys
        or architect.get("agent_type") != "architect"
        or architect.get("verdict") != "CLEAR"
        or architect.get("findings") != []
    ):
        raise ValueError("architect lane did not clear the candidate")
    lane_ids = [code_reviewer.get("lane_id"), architect.get("lane_id")]
    if any(not isinstance(value, str) or not value for value in lane_ids) or len(set(lane_ids)) != 2:
        raise ValueError("independent review lanes are missing or not distinct")
    for lane in (code_reviewer, architect):
        report = lane.get("report")
        if (
            not isinstance(report, str)
            or not report
            or lane.get("report_sha256")
            != _sha256_bytes(report.encode("utf-8", "surrogateescape"))
        ):
            raise ValueError("independent review report digest is invalid")


def _validate_hosted_ci_evidence(
    artifact: dict[str, Any],
    candidate_head: str,
    candidate_manifest_sha256: str,
) -> float:
    gate_name = "hosted_ci"
    if set(artifact) != {
        "schema",
        "gate",
        "status",
        "candidate_head",
        "candidate_manifest_sha256",
        "repository",
        "workflow_name",
        "workflow_path",
        "event",
        "head_branch",
        "pull_request_number",
        "pull_request_url",
        "pull_request_state",
        "pull_request_is_draft",
        "pull_request_base_ref",
        "pull_request_head_ref",
        "pull_request_head_sha",
        "run_pull_request_number",
        "python_version",
        "run_id",
        "run_attempt",
        "run_url",
        "run_inventory_query",
        "workflow_runs_for_head",
        "started_at",
        "completed_at",
        "conclusion",
        "jobs",
    } or artifact.get("schema") != HOSTED_CI_EVIDENCE_SCHEMA:
        raise ValueError("hosted CI evidence schema is invalid")
    _validate_gate_context(
        artifact, gate_name, candidate_head, candidate_manifest_sha256
    )
    pull_request_number = artifact.get("pull_request_number")
    run_id = artifact.get("run_id")
    run_attempt = artifact.get("run_attempt")
    repository_url = f"https://github.com/{_HOSTED_CI_REPOSITORY}"
    if (
        isinstance(pull_request_number, bool)
        or not isinstance(pull_request_number, int)
        or pull_request_number <= 0
    ):
        raise ValueError("hosted CI field pull_request_number is invalid")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise ValueError("hosted CI field run_id is invalid")
    if artifact.get("pull_request_base_ref") != "workspace/v1":
        raise ValueError("hosted CI field pull_request_base_ref is invalid")
    if (
        isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt != 1
    ):
        raise ValueError("hosted CI field run_attempt is invalid")
    if artifact.get("run_url") != f"{repository_url}/actions/runs/{run_id}":
        raise ValueError("hosted CI field run_url is invalid")
    if (
        artifact.get("repository") != _HOSTED_CI_REPOSITORY
        or artifact.get("workflow_name") != _HOSTED_CI_WORKFLOW
        or artifact.get("workflow_path") != _HOSTED_CI_WORKFLOW_PATH
        or artifact.get("event") != "pull_request"
        or artifact.get("head_branch") != "codex/pytest-parallel-gate"
        or artifact.get("pull_request_url")
        != f"{repository_url}/pull/{pull_request_number}"
        or artifact.get("pull_request_state") != "OPEN"
        or artifact.get("pull_request_is_draft") is not True
        or artifact.get("pull_request_head_ref") != "codex/pytest-parallel-gate"
        or artifact.get("pull_request_head_sha") != candidate_head
        or artifact.get("run_pull_request_number") != pull_request_number
        or artifact.get("python_version") != "3.14"
        or artifact.get("conclusion") != "success"
        or artifact.get("run_inventory_query")
        != [
            "gh",
            "run",
            "list",
            "--repo",
            _HOSTED_CI_REPOSITORY,
            "--workflow",
            _HOSTED_CI_WORKFLOW,
            "--commit",
            candidate_head,
        ]
    ):
        raise ValueError("hosted CI run is not the exact successful first attempt")
    run_started = _parse_utc(artifact["started_at"])
    run_completed = _parse_utc(artifact["completed_at"])
    if run_completed < run_started:
        raise ValueError("hosted CI run timestamps are reversed")
    inventory = artifact.get("workflow_runs_for_head")
    expected_inventory = {
        "run_id": run_id,
        "run_attempt": 1,
        "event": "pull_request",
        "workflow_name": _HOSTED_CI_WORKFLOW,
        "head_sha": candidate_head,
        "status": "completed",
        "conclusion": "success",
        "url": f"{repository_url}/actions/runs/{run_id}",
    }
    if (
        not isinstance(inventory, list)
        or len(inventory) != 1
        or not isinstance(inventory[0], dict)
        or set(inventory[0])
        != {*expected_inventory, "created_at", "updated_at"}
        or isinstance(inventory[0].get("run_attempt"), bool)
        or any(inventory[0].get(key) != value for key, value in expected_inventory.items())
        or _parse_utc(inventory[0]["created_at"]) > run_started
        or _parse_utc(inventory[0]["updated_at"]) < run_completed
    ):
        raise ValueError("hosted CI inventory does not prove the sole final-SHA first run")
    jobs = artifact.get("jobs")
    if (
        not isinstance(jobs, list)
        or not all(isinstance(job, dict) and isinstance(job.get("name"), str) for job in jobs)
        or sorted(str(job["name"]) for job in jobs) != _HOSTED_CI_JOB_NAMES
    ):
        raise ValueError("hosted CI job set differs from the blocking workflow")
    job_ids: list[int] = []
    test_seconds: float | None = None
    for job in jobs:
        if not isinstance(job, dict) or set(job) != {
            "database_id",
            "name",
            "status",
            "conclusion",
            "started_at",
            "completed_at",
            "url",
            "runner_os",
            "runner_arch",
            "runner_image",
            "steps",
        }:
            raise ValueError("hosted CI job evidence schema is invalid")
        database_id = job.get("database_id")
        if (
            not isinstance(database_id, int)
            or database_id <= 0
            or job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("url")
            != f"{repository_url}/actions/runs/{run_id}/job/{database_id}"
        ):
            raise ValueError("hosted CI contains an unsuccessful or invalid job")
        job_ids.append(database_id)
        job_started = _parse_utc(job["started_at"])
        job_completed = _parse_utc(job["completed_at"])
        if job_started < run_started or job_completed > run_completed or job_completed < job_started:
            raise ValueError("hosted CI job timestamps fall outside the workflow run")
        if job["name"] == "test (3.14)":
            if (
                job.get("runner_os") != "Linux"
                or job.get("runner_arch") != "X64"
                or not isinstance(job.get("runner_image"), str)
                or not job["runner_image"]
            ):
                raise ValueError("hosted test job runner identity is invalid")
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError("hosted test job step evidence is missing")
            step_names: list[str] = []
            for step in steps:
                if not isinstance(step, dict) or set(step) != {
                    "number",
                    "name",
                    "status",
                    "conclusion",
                    "started_at",
                    "completed_at",
                }:
                    raise ValueError("hosted test job step schema is invalid")
                step_started = _parse_utc(step["started_at"])
                step_completed = _parse_utc(step["completed_at"])
                if (
                    not isinstance(step.get("number"), int)
                    or int(step["number"]) <= 0
                    or not isinstance(step.get("name"), str)
                    or not step["name"]
                    or step.get("status") != "completed"
                    or step.get("conclusion") != "success"
                    or step_started < job_started
                    or step_completed > job_completed
                    or step_completed < step_started
                ):
                    raise ValueError("hosted test job contains invalid or unsuccessful steps")
                step_names.append(str(step["name"]))
            if len(step_names) != len(set(step_names)) or not {
                "Install dependencies",
                "Run tests",
            }.issubset(step_names):
                raise ValueError("hosted test job lacks exact setup or test-step evidence")
            test_seconds = (job_completed - job_started).total_seconds()
    if len(job_ids) != len(set(job_ids)) or test_seconds is None or test_seconds <= 0:
        raise ValueError("hosted CI job identity or test timing is invalid")
    return test_seconds


def _verify_live_github_hosted(
    artifact: dict[str, Any],
    *,
    api_loader: Callable[[str], dict[str, Any]] | None = None,
    job_log_loader: Callable[[int, int], str] | None = None,
) -> None:
    api_loader = api_loader or _gh_api_json
    job_log_loader = job_log_loader or _gh_job_log
    repository_api = f"repos/{_HOSTED_CI_REPOSITORY}"
    candidate_head = str(artifact["candidate_head"])
    pull_number = int(artifact["pull_request_number"])
    run_id = int(artifact["run_id"])

    pull = api_loader(f"{repository_api}/pulls/{pull_number}")
    if (
        pull.get("number") != pull_number
        or pull.get("html_url") != artifact.get("pull_request_url")
        or pull.get("state") != "open"
        or pull.get("draft") is not True
        or not isinstance(pull.get("base"), dict)
        or pull["base"].get("ref") != "workspace/v1"
        or not isinstance(pull.get("head"), dict)
        or pull["head"].get("ref") != "codex/pytest-parallel-gate"
        or pull["head"].get("sha") != candidate_head
    ):
        raise ValueError("live GitHub pull request differs from hosted evidence")

    run = api_loader(f"{repository_api}/actions/runs/{run_id}")
    associated_pulls = run.get("pull_requests")
    repository_api_url = f"https://api.github.com/{repository_api}"
    if not isinstance(associated_pulls, list) or len(associated_pulls) != 1:
        raise ValueError("live GitHub workflow run lacks one exact pull request association")
    associated_pull = associated_pulls[0]
    if (
        not isinstance(associated_pull, dict)
        or associated_pull.get("number") != pull_number
        or associated_pull.get("url") != f"{repository_api_url}/pulls/{pull_number}"
        or not isinstance(associated_pull.get("head"), dict)
        or associated_pull["head"].get("ref") != "codex/pytest-parallel-gate"
        or associated_pull["head"].get("sha") != candidate_head
        or not isinstance(associated_pull["head"].get("repo"), dict)
        or associated_pull["head"]["repo"].get("url") != repository_api_url
        or not isinstance(associated_pull.get("base"), dict)
        or associated_pull["base"].get("ref") != "workspace/v1"
        or not isinstance(associated_pull["base"].get("repo"), dict)
        or associated_pull["base"]["repo"].get("url") != repository_api_url
    ):
        raise ValueError("live GitHub workflow run is associated with another pull request")
    if (
        run.get("id") != run_id
        or isinstance(run.get("run_attempt"), bool)
        or run.get("run_attempt") != 1
        or run.get("event") != "pull_request"
        or run.get("name") != _HOSTED_CI_WORKFLOW
        or run.get("path") != _HOSTED_CI_WORKFLOW_PATH
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != "codex/pytest-parallel-gate"
        or run.get("head_sha") != candidate_head
        or run.get("html_url") != artifact.get("run_url")
        or _parse_utc(run.get("created_at")) != _parse_utc(artifact["started_at"])
        or _parse_utc(run.get("updated_at")) != _parse_utc(artifact["completed_at"])
    ):
        raise ValueError("live GitHub workflow run differs from hosted evidence")

    inventory_response = api_loader(
        f"{repository_api}/actions/runs?head_sha={candidate_head}&event=pull_request&per_page=100"
    )
    workflow_runs = inventory_response.get("workflow_runs")
    if (
        not isinstance(workflow_runs, list)
        or inventory_response.get("total_count") != len(workflow_runs)
    ):
        raise ValueError("live GitHub final-SHA workflow inventory is missing")
    matching_runs = [
        item
        for item in workflow_runs
        if isinstance(item, dict)
        and item.get("name") == _HOSTED_CI_WORKFLOW
        and item.get("path") == _HOSTED_CI_WORKFLOW_PATH
    ]
    if len(matching_runs) != 1 or matching_runs[0].get("id") != run_id:
        raise ValueError("live GitHub does not show one sole final-SHA CI run")
    inventory = artifact["workflow_runs_for_head"][0]
    if (
        isinstance(matching_runs[0].get("run_attempt"), bool)
        or matching_runs[0].get("run_attempt") != inventory.get("run_attempt")
        or matching_runs[0].get("event") != inventory.get("event")
        or matching_runs[0].get("head_sha") != inventory.get("head_sha")
        or matching_runs[0].get("status") != inventory.get("status")
        or matching_runs[0].get("conclusion") != inventory.get("conclusion")
        or matching_runs[0].get("html_url") != inventory.get("url")
        or _parse_utc(matching_runs[0].get("created_at"))
        != _parse_utc(inventory["created_at"])
        or _parse_utc(matching_runs[0].get("updated_at"))
        != _parse_utc(inventory["updated_at"])
    ):
        raise ValueError("live GitHub final-SHA inventory differs from hosted evidence")

    jobs_response = api_loader(f"{repository_api}/actions/runs/{run_id}/jobs?per_page=100")
    live_jobs = jobs_response.get("jobs")
    if jobs_response.get("total_count") != len(_HOSTED_CI_JOB_NAMES) or not isinstance(
        live_jobs, list
    ):
        raise ValueError("live GitHub blocking job inventory is incomplete")
    evidence_jobs = {int(job["database_id"]): job for job in artifact["jobs"]}
    live_ids: set[int] = set()
    test_job_id: int | None = None
    for live_job in live_jobs:
        if not isinstance(live_job, dict) or not isinstance(live_job.get("id"), int):
            raise ValueError("live GitHub job evidence is malformed")
        live_id = int(live_job["id"])
        live_ids.add(live_id)
        evidence_job = evidence_jobs.get(live_id)
        if evidence_job is None:
            raise ValueError("live GitHub job is absent from hosted evidence")
        live_steps = [
            {
                "number": step.get("number"),
                "name": step.get("name"),
                "status": step.get("status"),
                "conclusion": step.get("conclusion"),
                "started_at": step.get("started_at"),
                "completed_at": step.get("completed_at"),
            }
            for step in live_job.get("steps", [])
            if isinstance(step, dict)
        ]
        if (
            live_job.get("name") != evidence_job.get("name")
            or live_job.get("status") != evidence_job.get("status")
            or live_job.get("conclusion") != evidence_job.get("conclusion")
            or live_job.get("html_url") != evidence_job.get("url")
            or _parse_utc(live_job.get("started_at"))
            != _parse_utc(evidence_job["started_at"])
            or _parse_utc(live_job.get("completed_at"))
            != _parse_utc(evidence_job["completed_at"])
            or live_steps != evidence_job.get("steps")
        ):
            raise ValueError("live GitHub job or steps differ from hosted evidence")
        if live_job.get("name") == "test (3.14)":
            if live_job.get("labels") != ["ubuntu-latest"]:
                raise ValueError("live GitHub test job runner label is unexpected")
            test_job_id = live_id
    if live_ids != set(evidence_jobs) or test_job_id is None:
        raise ValueError("live GitHub and hosted job identities differ")
    images = {
        line.split("Image: ", 1)[1].strip()
        for line in job_log_loader(run_id, test_job_id).splitlines()
        if "Image: " in line
    }
    if images != {evidence_jobs[test_job_id]["runner_image"]}:
        raise ValueError("live GitHub runner image differs from hosted evidence")


def _validate_hosted_variance_evidence(
    manifest_path: Path,
    reference: Any,
    hosted_artifact: dict[str, Any],
    hosted_seconds: float,
) -> None:
    artifact, _, _ = _load_artifact_ref(manifest_path, reference)
    if set(artifact) != {
        "schema",
        "repository",
        "workflow_path",
        "job_name",
        "python_version",
        "runner_os",
        "runner_arch",
        "runner_image",
        "source_pull_request_number",
        "source_pull_request_url",
        "source_pull_request_state",
        "source_pull_request_base_ref",
        "source_pull_request_head_ref",
        "source_pull_request_head_sha",
        "source_run_id",
        "source_run_url",
        "source_run_attempt",
        "source_run_event",
        "source_run_workflow_name",
        "source_run_status",
        "source_run_conclusion",
        "source_run_pull_request_number",
        "source_job_id",
        "source_job_url",
        "source_job_status",
        "source_job_conclusion",
        "source_head_sha",
        "source_job_started_at",
        "source_job_completed_at",
        "source_setup_started_at",
        "source_setup_completed_at",
        "source_test_step_started_at",
        "source_test_step_completed_at",
        "variance_explanation",
    } or artifact.get("schema") != HOSTED_VARIANCE_EVIDENCE_SCHEMA:
        raise ValueError("hosted runner-variance evidence schema is invalid")
    source_run_id = artifact.get("source_run_id")
    source_job_id = artifact.get("source_job_id")
    repository_url = f"https://github.com/{_HOSTED_CI_REPOSITORY}"
    if source_run_id != _PR81_RUN_ID:
        raise ValueError("hosted variance field source_run_id is invalid")
    if artifact.get("source_job_conclusion") != "success":
        raise ValueError("hosted variance field source_job_conclusion is invalid")
    job_started = _parse_utc(artifact["source_job_started_at"])
    job_completed = _parse_utc(artifact["source_job_completed_at"])
    setup_started = _parse_utc(artifact["source_setup_started_at"])
    setup_completed = _parse_utc(artifact["source_setup_completed_at"])
    test_started = _parse_utc(artifact["source_test_step_started_at"])
    test_completed = _parse_utc(artifact["source_test_step_completed_at"])
    serial_seconds = (job_completed - job_started).total_seconds()
    variance_explanation = artifact.get("variance_explanation")
    candidate_test_job = next(
        (
            job
            for job in hosted_artifact.get("jobs", [])
            if isinstance(job, dict) and job.get("name") == "test (3.14)"
        ),
        None,
    )
    if not isinstance(candidate_test_job, dict):
        raise ValueError("candidate hosted test job is missing from variance comparison")
    candidate_steps = {
        step["name"]: step
        for step in candidate_test_job.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }
    candidate_setup = candidate_steps.get("Install dependencies")
    candidate_test = candidate_steps.get("Run tests")
    expected_explanation = (
        f"Candidate and PR #81 both used {artifact.get('runner_image')} on Linux X64; "
        "whole-job timing includes setup and Run tests step variance."
    )
    if (
        artifact.get("repository") != _HOSTED_CI_REPOSITORY
        or artifact.get("workflow_path") != _HOSTED_CI_WORKFLOW_PATH
        or artifact.get("job_name") != "test (3.14)"
        or artifact.get("python_version") != "3.14"
        or artifact.get("runner_os") != "Linux"
        or artifact.get("runner_arch") != "X64"
        or not isinstance(artifact.get("runner_image"), str)
        or not artifact["runner_image"]
        or artifact.get("source_pull_request_number") != 81
        or artifact.get("source_pull_request_url") != f"{repository_url}/pull/81"
        or artifact.get("source_pull_request_state") != "MERGED"
        or artifact.get("source_pull_request_base_ref") != "workspace/v1"
        or artifact.get("source_pull_request_head_ref") != _PR81_HEAD_REF
        or artifact.get("source_pull_request_head_sha") != _PR81_HEAD_SHA
        or artifact.get("source_run_url")
        != f"{repository_url}/actions/runs/{source_run_id}"
        or artifact.get("source_run_attempt") != 1
        or artifact.get("source_run_event") != "pull_request"
        or artifact.get("source_run_workflow_name") != _HOSTED_CI_WORKFLOW
        or artifact.get("source_run_status") != "completed"
        or artifact.get("source_run_conclusion") != "success"
        or artifact.get("source_run_pull_request_number") != 81
        or source_job_id != _PR81_JOB_ID
        or artifact.get("source_job_url")
        != f"{repository_url}/actions/runs/{source_run_id}/job/{source_job_id}"
        or artifact.get("source_head_sha") != _PR81_HEAD_SHA
        or artifact.get("source_job_status") != "completed"
        or artifact.get("runner_image") != _PR81_RUNNER_IMAGE
        or candidate_test_job.get("runner_os") != artifact.get("runner_os")
        or candidate_test_job.get("runner_arch") != artifact.get("runner_arch")
        or candidate_test_job.get("runner_image") != artifact.get("runner_image")
        or hosted_artifact.get("python_version") != artifact.get("python_version")
        or not isinstance(candidate_setup, dict)
        or not isinstance(candidate_test, dict)
        or job_started != _parse_utc(_PR81_JOB_STARTED_AT)
        or job_completed != _parse_utc(_PR81_JOB_COMPLETED_AT)
        or setup_started != _parse_utc(_PR81_SETUP_STARTED_AT)
        or setup_completed != _parse_utc(_PR81_SETUP_COMPLETED_AT)
        or test_started != _parse_utc(_PR81_TEST_STARTED_AT)
        or test_completed != _parse_utc(_PR81_TEST_COMPLETED_AT)
        or serial_seconds != 805
        or not (job_started <= setup_started <= setup_completed <= test_started)
        or not (test_started < test_completed <= job_completed)
        or variance_explanation != expected_explanation
    ):
        raise ValueError("hosted variance evidence is not a comparable 20 percent improvement")
    if hosted_seconds * 5 > serial_seconds * 4:
        raise ValueError("hosted variance field hosted_seconds does not prove 20 percent improvement")


def _validate_gate_receipt(
    manifest_path: Path,
    gate_name: str,
    reference: Any,
    candidate_head: str,
    candidate_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    receipt, _, _ = _load_artifact_ref(manifest_path, reference)
    if set(receipt) != {
        "schema",
        "gate",
        "status",
        "candidate_head",
        "candidate_manifest_sha256",
        "evidence_artifacts",
    } or receipt.get("schema") != GATE_RECEIPT_SCHEMA:
        raise ValueError(f"gate receipt schema is invalid: {gate_name}")
    _validate_gate_context(
        receipt, gate_name, candidate_head, candidate_manifest_sha256
    )
    evidence_artifacts = receipt.get("evidence_artifacts")
    if not isinstance(evidence_artifacts, list) or len(evidence_artifacts) != 1:
        raise ValueError(f"gate receipt requires one exact evidence artifact: {gate_name}")
    artifact, _, _ = _load_artifact_ref(manifest_path, evidence_artifacts[0])
    if gate_name in _LOCAL_GATE_COMMANDS:
        _validate_local_gate_evidence(
            artifact, gate_name, candidate_head, candidate_manifest_sha256
        )
    elif gate_name == "independent_review":
        _validate_review_evidence(
            artifact, candidate_head, candidate_manifest_sha256
        )
    elif gate_name == "hosted_ci":
        _validate_hosted_ci_evidence(
            artifact, candidate_head, candidate_manifest_sha256
        )
    else:
        raise ValueError(f"unsupported gate receipt: {gate_name}")
    return artifact, evidence_artifacts[0]


def verify_evidence(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    evidence = _load_json(manifest_path)
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError("evidence schema mismatch")
    evaluator_digest = str(evidence["evaluator_sha256"])
    if _sha256_file(Path(__file__).resolve()) != evaluator_digest:
        raise ValueError("active evaluator bytes differ from the evidence digest")

    baseline, baseline_sha, _ = _load_artifact_ref(manifest_path, evidence["baseline"])
    final_serial, final_serial_sha, _ = _load_artifact_ref(manifest_path, evidence["final_serial"])
    matrix_loaded = [_load_artifact_ref(manifest_path, ref) for ref in evidence.get("matrix_runs", [])]
    winner_loaded = [_load_artifact_ref(manifest_path, ref) for ref in evidence.get("winner_runs", [])]
    hazard_loaded = [_load_artifact_ref(manifest_path, ref) for ref in evidence.get("hazard_runs", [])]
    preflight_loaded = [
        _load_artifact_ref(manifest_path, ref) for ref in evidence.get("preflight_runs", [])
    ]
    matrix = [item[0] for item in matrix_loaded]
    winners = [item[0] for item in winner_loaded]
    hazards = [item[0] for item in hazard_loaded]
    if len(matrix) not in {2, 3}:
        raise ValueError("matrix requires two loadfile runs and at most one load challenger")
    if len(winners) != 2:
        raise ValueError("exactly two winning full-suite runs are required")
    if len(hazards) != 3:
        raise ValueError("exactly three hazard-cohort runs are required")
    for run in [baseline, final_serial, *matrix, *winners, *hazards, *(item[0] for item in preflight_loaded)]:
        _validate_run_artifact(run, evaluator_digest)

    baseline_identity = baseline["git_before"]
    expected_base_head = str(evidence["expected_base_head"])
    expected_candidate_head = str(evidence["expected_candidate_head"])
    expected_repository = str(evidence["expected_repository_fingerprint"])
    expected_host = str(evidence["expected_host_fingerprint"])
    if (
        baseline_identity.get("head") != expected_base_head
        or baseline_identity.get("status") != []
        or baseline_identity.get("repository_fingerprint") != expected_repository
        or baseline_identity.get("host_fingerprint") != expected_host
    ):
        raise ValueError("baseline is not clean or not bound to the expected base/repository/host")
    baseline_manifest_sha = str(evidence["baseline_manifest_sha256"])
    matrix_manifest_sha = str(evidence["matrix_manifest_sha256"])
    candidate_manifest_sha = str(evidence["candidate_manifest_sha256"])
    if baseline["manifest_before"].get("manifest_sha256") != baseline_manifest_sha:
        raise ValueError("baseline manifest does not match frozen evidence")
    expected_matrix_head = str(evidence["expected_matrix_head"])
    expected_matrix_status = evidence.get("expected_matrix_status")
    for run in matrix:
        identity = run["git_before"]
        if (
            identity.get("head") != expected_matrix_head
            or identity.get("status") != expected_matrix_status
            or identity.get("repository_fingerprint") != expected_repository
            or identity.get("host_fingerprint") != expected_host
            or run["manifest_before"].get("manifest_sha256") != matrix_manifest_sha
        ):
            raise ValueError("matrix run is not bound to the frozen intermediate snapshot")
    expected_candidate_status = evidence.get("expected_candidate_status")
    _require_clean_candidate_status(expected_candidate_status)
    candidate_runs = [final_serial, *winners, *hazards]
    for run in candidate_runs:
        identity = run["git_before"]
        if (
            identity.get("head") != expected_candidate_head
            or identity.get("status") != expected_candidate_status
            or identity.get("repository_fingerprint") != expected_repository
            or identity.get("host_fingerprint") != expected_host
            or run["manifest_before"].get("manifest_sha256") != candidate_manifest_sha
        ):
            raise ValueError("candidate run is not bound to the frozen repository/host/revision/manifest")
    _assert_snapshot_transition(
        matrix[0]["manifest_before"],
        final_serial["manifest_before"],
        evidence.get("allowed_snapshot_transition_files"),
    )

    preflights_by_sha = {sha: run for run, sha, _ in preflight_loaded}
    scored = [baseline, final_serial, *matrix, *winners, *hazards]
    if len(preflights_by_sha) != len(scored):
        raise ValueError("every scored run requires one unique collect preflight")
    for run in scored:
        preflight_sha = run.get("preflight_artifact_sha256")
        if not isinstance(preflight_sha, str):
            raise ValueError("scored run lacks a preflight digest")
        preflight = preflights_by_sha.get(preflight_sha)
        if preflight is None:
            raise ValueError("scored run is not bound to a provided collect preflight")
        expected_kind = "collect-hazard" if run["kind"] == "hazard" else "collect-full"
        preflight_gap = (
            _parse_utc(run["started_at"]) - _parse_utc(preflight["completed_at"])
        ).total_seconds()
        if (
            preflight.get("kind") != expected_kind
            or preflight.get("collection_order") != run.get("collection_order")
            or preflight.get("manifest_before", {}).get("manifest_sha256")
            != run.get("manifest_before", {}).get("manifest_sha256")
            or preflight.get("git_before") != run.get("git_before")
            or preflight_gap < 0
            or preflight_gap > 300
        ):
            raise ValueError("collect preflight chronology or selection does not match scored run")

    _assert_serial_command(baseline["requested_argv"])
    _assert_serial_command(final_serial["requested_argv"])
    expected_environment = evidence.get("expected_environment")
    expected_baseline_versions = evidence.get("expected_baseline_versions")
    expected_candidate_versions = evidence.get("expected_candidate_versions")
    expected_baseline_plugins = evidence.get("expected_baseline_pytest_plugin_entrypoints")
    expected_candidate_plugins = evidence.get("expected_candidate_pytest_plugin_entrypoints")
    expected_uv_version = str(evidence.get("expected_uv_version"))
    if (
        not isinstance(expected_baseline_versions, dict)
        or expected_baseline_versions.get("pytest_xdist") is not None
        or expected_baseline_versions.get("execnet") is not None
        or expected_baseline_versions.get("anyio") != "4.13.0"
        or expected_baseline_versions.get("hypothesis") != "6.153.0"
        or expected_baseline_versions.get("openai") != "2.36.0"
        or expected_baseline_versions.get("pytest_cov") != "7.1.0"
        or expected_baseline_versions.get("coverage") != "7.14.0"
        or not isinstance(expected_candidate_versions, dict)
        or expected_candidate_versions.get("pytest_xdist") != "3.8.0"
        or expected_candidate_versions.get("execnet") != "2.1.2"
        or expected_candidate_versions.get("anyio") != "4.13.0"
        or expected_candidate_versions.get("hypothesis") != "6.153.0"
        or expected_candidate_versions.get("openai") != "2.36.0"
        or expected_candidate_versions.get("pytest_cov") != "7.1.0"
        or expected_candidate_versions.get("coverage") != "7.14.0"
        or expected_baseline_plugins != _BASELINE_PYTEST_PLUGIN_ENTRYPOINTS
        or expected_candidate_plugins != _CANDIDATE_PYTEST_PLUGIN_ENTRYPOINTS
    ):
        raise ValueError("frozen baseline/candidate dependency versions are invalid")
    _assert_comparable_python_versions(
        expected_baseline_versions,
        expected_candidate_versions,
    )
    _assert_environment_and_versions(
        baseline,
        expected_environment=expected_environment,
        expected_versions=expected_baseline_versions,
        expected_plugin_entrypoints=expected_baseline_plugins,
        expected_uv_version=expected_uv_version,
    )
    for run in matrix:
        _assert_environment_and_versions(
            run,
            expected_environment=expected_environment,
            expected_versions=expected_candidate_versions,
            expected_plugin_entrypoints=expected_candidate_plugins,
            expected_uv_version=expected_uv_version,
        )
    _assert_environment_and_versions(
        final_serial,
        expected_environment=expected_environment,
        expected_versions=expected_candidate_versions,
        expected_plugin_entrypoints=expected_candidate_plugins,
        expected_uv_version=expected_uv_version,
    )
    for run in [*winners, *hazards]:
        _assert_environment_and_versions(
            run,
            expected_environment=expected_environment,
            expected_versions=expected_candidate_versions,
            expected_plugin_entrypoints=expected_candidate_plugins,
            expected_uv_version=expected_uv_version,
        )
    for scored_run in scored:
        preflight = preflights_by_sha[str(scored_run["preflight_artifact_sha256"])]
        expected_versions = (
            expected_baseline_versions
            if scored_run is baseline
            else expected_candidate_versions
        )
        expected_plugins = (
            expected_baseline_plugins
            if scored_run is baseline
            else expected_candidate_plugins
        )
        _assert_environment_and_versions(
            preflight,
            expected_environment=expected_environment,
            expected_versions=expected_versions,
            expected_plugin_entrypoints=expected_plugins,
            expected_uv_version=expected_uv_version,
        )

    if len(final_serial.get("collection_order", [])) < 5750:
        raise ValueError("final serial control collected fewer than 5750 tests")
    allowed_added = set(evidence.get("allowed_added_node_ids", []))
    _assert_added_node_files(
        allowed_added,
        evidence.get("allowed_added_test_files"),
        expected_matrix_status,
    )
    _assert_parity(baseline, final_serial, allowed_added)
    for winner in [*matrix, *winners]:
        _assert_parity(baseline, winner, allowed_added)
    candidate_collection = final_serial["collection_order"]
    if any(
        run["collection_order"] != candidate_collection
        for run in [*matrix, *winners]
    ):
        raise ValueError("candidate full-suite collection order differs across scored runs")

    matrix_configs = [_parallel_config(run["requested_argv"]) for run in matrix]
    if matrix_configs[0] != (2, "loadfile", 0) or matrix_configs[1] != (4, "loadfile", 0):
        raise ValueError("matrix does not start with 2-worker then 4-worker loadfile")
    if len(matrix) == 3:
        faster_loadfile_workers = matrix_configs[
            0 if float(matrix[0]["wall_seconds"]) <= float(matrix[1]["wall_seconds"]) else 1
        ][0]
        if matrix_configs[2] != (faster_loadfile_workers, "load", 0):
            raise ValueError("load challenger does not use the faster loadfile worker count")
    winner_configs = [_parallel_config(run["requested_argv"]) for run in winners]
    if winner_configs[0] != winner_configs[1]:
        raise ValueError("winning parallel configurations differ")
    workers, distribution, max_restarts = winner_configs[0]
    if workers not in {2, 4} or distribution not in {"loadfile", "load"} or max_restarts != 0:
        raise ValueError("winning parallel configuration is outside the bounded plan")

    loadfile_qualifiers = [
        config
        for config, run in zip(matrix_configs[:2], matrix[:2], strict=True)
        if all(value >= 0.30 for value in _config_improvement(run, baseline, final_serial))
    ]
    if loadfile_qualifiers:
        selected_config = min(loadfile_qualifiers, key=lambda value: value[0])
        if len(matrix) != 2:
            raise ValueError("load challenger was run despite a qualifying loadfile configuration")
    else:
        if len(matrix) != 3 or not all(
            value >= 0.30 for value in _config_improvement(matrix[2], baseline, final_serial)
        ):
            raise ValueError("no matrix candidate satisfies the frozen performance threshold")
        selected_config = matrix_configs[2]
    if winner_configs[0] != selected_config:
        raise ValueError("winning command does not follow the deterministic matrix rule")

    winner_median = sum(
        _finite_positive(run["wall_seconds"], "winner wall time") for run in winners
    ) / 2.0
    baseline_wall = _finite_positive(baseline["wall_seconds"], "baseline wall time")
    final_serial_wall = _finite_positive(final_serial["wall_seconds"], "serial control wall time")
    improvement_baseline = 1.0 - winner_median / baseline_wall
    improvement_control = 1.0 - winner_median / final_serial_wall
    if improvement_baseline < 0.30 or improvement_control < 0.30:
        raise ValueError("parallel median does not improve by 30 percent against both controls")

    candidate_repo_root = Path(str(final_serial["repo_root"])).resolve()
    evaluator_repo_root = Path(__file__).resolve().parents[1]
    if candidate_repo_root != evaluator_repo_root:
        raise ValueError("candidate runs are not bound to the active evaluator repository")
    cohort_paths_ordered, hazard_cohort_sha = _committed_hazard_cohort(
        candidate_repo_root, final_serial["manifest_before"]
    )
    hazard_collections = [run["collection_order"] for run in hazards]
    if not all(run.get("kind") == "hazard" for run in hazards):
        raise ValueError("hazard evidence contains a non-hazard run")
    if any(_parallel_config(run["requested_argv"]) != winner_configs[0] for run in hazards):
        raise ValueError("hazard configuration differs from the winning command")
    if any(run.get("cohort_sha256") != hazard_cohort_sha for run in hazards):
        raise ValueError("hazard cohort digest mismatch")
    if not all(collection == hazard_collections[0] for collection in hazard_collections[1:]):
        raise ValueError("hazard collections differ across consecutive runs")
    if any(run.get("cohort_paths") != cohort_paths_ordered for run in hazards):
        raise ValueError("hazard paths differ from the committed cohort order")
    cohort_paths = set(cohort_paths_ordered)
    collected_files = {str(nodeid).split("::", 1)[0] for nodeid in hazard_collections[0]}
    if collected_files != cohort_paths:
        raise ValueError("hazard collection does not cover the exact cohort files")
    _assert_hazard_outcome_parity(hazards, winners[0])

    scored_loaded = [
        (baseline, baseline_sha),
        *((run, sha) for run, sha, _ in matrix_loaded),
        (final_serial, final_serial_sha),
        *((run, sha) for run, sha, _ in winner_loaded),
        *((run, sha) for run, sha, _ in hazard_loaded),
    ]
    previous_scored_completed: datetime | None = None
    for run, _run_sha in scored_loaded:
        preflight = preflights_by_sha[str(run["preflight_artifact_sha256"])]
        if (
            previous_scored_completed is not None
            and _parse_utc(preflight["started_at"]) < previous_scored_completed
        ):
            raise ValueError("collect preflight overlaps the preceding scored trial")
        if _parse_utc(preflight["completed_at"]) > _parse_utc(run["started_at"]):
            raise ValueError("collect preflight overlaps its scored trial")
        previous_scored_completed = _parse_utc(run["completed_at"])
    expected_sequence = [
        digest
        for run, run_sha in scored_loaded
        for digest in (str(run["preflight_artifact_sha256"]), run_sha)
    ]
    if evidence.get("trial_sequence_sha256") != expected_sequence:
        raise ValueError("scored trial order/retention differs from the frozen protocol")
    chronological = [baseline, *matrix, final_serial, *winners, *hazards]
    if any(
        _parse_utc(left["completed_at"]) > _parse_utc(right["started_at"])
        for left, right in zip(chronological, chronological[1:])
    ):
        raise ValueError("scored trials overlap or are out of order")

    required_gates = (
        "lock",
        "focused",
        "static",
        "graph_refresh",
        "independent_review",
        "hosted_ci",
    )
    gates = evidence.get("gates", {})
    if set(gates) != set(required_gates):
        raise ValueError("gate receipt set is incomplete or unexpected")
    gate_artifacts: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for gate_name in required_gates:
        gate_artifacts[gate_name] = _validate_gate_receipt(
            manifest_path,
            gate_name,
            gates[gate_name],
            expected_candidate_head,
            candidate_manifest_sha,
        )
    hosted = evidence.get("hosted", {})
    if not isinstance(hosted, dict) or set(hosted) not in (
        {"jobs_artifact"},
        {"jobs_artifact", "comparable_variance_evidence"},
    ):
        raise ValueError("hosted evidence reference schema is invalid")
    hosted_artifact, hosted_reference = gate_artifacts["hosted_ci"]
    if hosted.get("jobs_artifact") != hosted_reference:
        raise ValueError("hosted timing artifact differs from the hosted CI gate receipt")
    hosted_seconds = _validate_hosted_ci_evidence(
        hosted_artifact, expected_candidate_head, candidate_manifest_sha
    )
    _verify_live_github_hosted(hosted_artifact)
    if hosted_seconds > 644:
        raise ValueError("hosted test job misses both timing thresholds")
    if hosted_seconds > 600:
        if "comparable_variance_evidence" not in hosted:
            raise ValueError("601-644 second hosted result lacks comparable variance evidence")
        _validate_hosted_variance_evidence(
            manifest_path,
            hosted["comparable_variance_evidence"],
            hosted_artifact,
            hosted_seconds,
        )
    elif "comparable_variance_evidence" in hosted:
        raise ValueError("hosted variance evidence is forbidden when the job is at most 600 seconds")

    return {
        "status": "pass",
        "evaluator_sha256": evaluator_digest,
        "collected_baseline": len(baseline["collection_order"]),
        "added_nodes": len(allowed_added),
        "workers": workers,
        "distribution": distribution,
        "winner_median_seconds": winner_median,
        "baseline_seconds": baseline_wall,
        "final_serial_seconds": final_serial_wall,
        "improvement_vs_baseline": improvement_baseline,
        "improvement_vs_final_serial": improvement_control,
        "hosted_test_job_seconds": hosted_seconds,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Graphify full-pytest performance evaluator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--artifact-root", type=Path, required=True)
    run_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    run_parser.add_argument(
        "--kind",
        choices=("collect-full", "full", "collect-hazard", "hazard", "integration"),
        required=True,
    )
    run_parser.add_argument("--timeout-seconds", type=float, required=True)
    run_parser.add_argument("--preflight-artifact", type=Path)
    run_parser.add_argument("--cohort-file", type=Path)
    run_parser.add_argument("pytest_argv", nargs=argparse.REMAINDER)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--evidence-manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        command = list(args.pytest_argv)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise SystemExit("pytest command required after --")
        try:
            result = run_pytest(
                args.repo_root,
                args.artifact_root,
                command,
                kind=args.kind,
                timeout_seconds=args.timeout_seconds,
                preflight_artifact=args.preflight_artifact,
                cohort_file=args.cohort_file,
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
            return 2
        print(json.dumps({
            "status": "pass" if result["passed"] else "fail",
            "artifact": str(Path(result["artifact_root"]) / "run.json"),
            "wall_seconds": result["wall_seconds"],
            "counts": result.get("counts", {}),
        }, sort_keys=True))
        return 0 if result["passed"] else int(result.get("child_exit_code") or 1)
    try:
        result = verify_evidence(args.evidence_manifest)
    except (
        KeyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
