#!/usr/bin/env python3
"""Classify the complete repository change set and run fail-closed validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from tools.validation_environment import (
    EnvironmentRefused, ValidationEnvironment, check_uv_argv, git_environment,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "origin/v8"
PYTHON_ARGV = (sys.executable,)
BASE_UNSET = "__AUTO_FEEDBACK_BASE_UNSET__"

EDITORIAL_DOCS = "editorial-docs"
CONTRACT_DOCS = "contract-docs"
FULL = "full"

DOCUMENT_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
FULL_GATE_EXACT_PATHS = {
    ".gitignore", ".pr_agent.toml", "Makefile", "pyproject.toml",
    "docs/protected-change-review.md",
}
FULL_GATE_PREFIXES = (".github/workflows/", "configs/", "scripts/", "src/", "tests/")
FULL_GATE_EXTENSIONS = {
    ".bash",
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".ipynb",
    ".js",
    ".json",
    ".jsonl",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".yaml",
    ".yml",
    ".zsh",
}
ROOT_DOCUMENTS = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "LICENSE.md",
    "README.md",
    "SECURITY.md",
}
CONTRACT_DOCUMENTS = {
    ".github/pull_request_template.md",
    "AGENTS.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "LICENSE.md",
    "README.md",
    "SECURITY.md",
    "docs/CODEBASE_MAP.md",
    "docs/ROADMAP.md",
    "docs/local-resource-retention.md",
}
CONTRACT_NAME_TOKENS = {
    "api",
    "apis",
    "architecture",
    "architectures",
    "authority",
    "authorities",
    "cli",
    "clis",
    "contract",
    "contracts",
    "decision",
    "decisions",
    "governance",
    "operator",
    "operators",
    "playbook",
    "playbooks",
    "policy",
    "policies",
    "procedure",
    "procedures",
    "prompt",
    "prompts",
    "roadmap",
    "roadmaps",
    "runbook",
    "runbooks",
    "schema",
    "schemas",
    "security",
    "spec",
    "specs",
    "validation",
    "review",
    "command",
    "workflow",
    "workflows",
}
COMMAND_PATTERN = (
    r"(?:(?:\.\.?/|/)[^\s`]+|bash|cd|cp|curl|docker(?:-compose)?|export|gh|git|"
    r"graphify|just|kubectl|make|mv|npm|npx|omx|pip\d*|pnpm|pre-commit|pytest|"
    r"python(?:\d+(?:\.\d+)*)?|rm|ruff|sh|source|tox|uv|wget|yarn)"
)
CONTRACT_CONTENT = re.compile(
    rf"(?m)(?:^(?:[ \t]*```|[ \t]*~~~|[ \t]*\.\.[ \t]+(?i:code-block|code)::|"
    rf"[ \t]*\[(?i:source)(?:,[^\]]*)?\][ \t]*$|[ \t]*----[ \t]*$|"
    rf"(?: {{4,}}|\t+)\S|#!)|(?i:<script\b|<iframe\b)|"
    rf"`[^`\n]*(?<![a-z0-9_-]){COMMAND_PATTERN}(?=[\s`.,;:!?)]|$)[^`\n]*`|"
    rf"(?i:\b(?:execute|invoke|rerun|run|use))[ \t]+`?{COMMAND_PATTERN}"
    rf"(?=[\s`.,;:!?)]|$)|"
    rf"^[ \t]*(?:[-*+][ \t]+)?(?:\$[ \t]+)?(?:"
    rf"[A-Z_][A-Z0-9_]*=[^ \t\n]+(?:[ \t]+|$)|{COMMAND_PATTERN}(?=[ \t]|$))|"
    rf"\b{COMMAND_PATTERN}[ \t]+[a-z0-9_./:=@-]+)"
)
GIT_MODE_LINE = re.compile(
    rb"(?m)^(?:old mode|new mode|new file mode|deleted file mode) ([0-7]{6})$"
)
GIT_BINARY_MARKERS = (b"GIT binary patch", b"Binary files ")
MAX_DOCUMENT_BYTES = 1_000_000

# Only maintained documentation qualifies. Runtime skills, generated Markdown,
# corpora, test fixtures, and arbitrary Markdown paths retain full validation.
CONTRACT_TESTS_BY_PATH = {
    "AGENTS.md": ("tests/test_readme_policy.py",),
    "README.md": ("tests/test_readme_policy.py", "tests/test_python314_policy.py"),
    ".github/pull_request_template.md": (
        "tests/test_validation_route.py", "tests/test_validation_contracts.py",
    ),
}
CONTRACT_TESTS_BY_PREFIX = ()


@dataclass(frozen=True)
class Change:
    status: str
    old_path: Path | None
    new_path: Path | None
    source: str

    def paths(self) -> tuple[Path, ...]:
        return tuple(path for path in (self.old_path, self.new_path) if path is not None)


@dataclass(frozen=True)
class Classification:
    lane: str
    changes: tuple[Change, ...]
    changed_paths: tuple[Path, ...]
    fallback_reasons: tuple[str, ...] = ()
    fingerprint: str = ""


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    reason: str
    env: tuple[tuple[str, str], ...] = ()

    def shell(self) -> str:
        prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in self.env)
        command = shlex.join(self.argv)
        if prefix:
            return f"{prefix} {command}"
        return command


def _run_rooted_git(args: list[str], *, binary: bool):
    env = git_environment()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, env=env,
        text=True, encoding="utf-8", errors="surrogateescape", capture_output=True, check=False,
    )
    if top.returncode or Path(top.stdout.rstrip("\r\n")).resolve() != ROOT.resolve():
        error = top.stderr if top.returncode else "Git top-level does not match inspected root\n"
        return subprocess.CompletedProcess(
            ["git", *args], top.returncode or 128, b"" if binary else "",
            error.encode("utf-8", errors="surrogateescape") if binary else error,
        )
    options = {} if binary else {"text": True, "encoding": "utf-8", "errors": "surrogateescape"}
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        **options,
    )


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run_rooted_git(args, binary=False)


def run_git_bytes(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return _run_rooted_git(args, binary=True)


def ref_exists(ref: str) -> bool:
    try:
        return run_git(["rev-parse", "--verify", "--quiet", ref]).returncode == 0
    except OSError:
        return False


def has_head() -> bool:
    return ref_exists("HEAD")


def _git_failure(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stderr or result.stdout).strip() or "no output"
    return f"git {' '.join(args)} failed:\n{output}"


def _git_bytes_failure(args: list[str], result: subprocess.CompletedProcess[bytes]) -> str:
    raw_output = result.stderr or result.stdout
    output = raw_output.decode("utf-8", errors="replace").strip() or "no output"
    return f"git {' '.join(args)} failed:\n{output}"


def _collect_diff_changes(args: list[str], source: str, errors: list[str]) -> list[Change]:
    command = [
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies-harder",
        "--diff-filter=ACDMRTUXB",
        *args,
    ]
    try:
        result = run_git(command)
    except OSError as exc:
        errors.append(f"git {' '.join(command)} failed:\n{exc}")
        return []
    if result.returncode != 0:
        errors.append(_git_failure(command, result))
        return []

    fields = result.stdout.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        status_text = fields[index]
        index += 1
        if not status_text:
            errors.append(f"git {' '.join(command)} returned an empty status field")
            break
        kind = status_text[0]
        path_count = 2 if kind in {"R", "C"} else 1
        if index + path_count > len(fields):
            errors.append(f"git {' '.join(command)} returned malformed name-status output")
            break
        raw_paths = fields[index : index + path_count]
        index += path_count
        if path_count == 2:
            changes.append(Change(status_text, Path(raw_paths[0]), Path(raw_paths[1]), source))
        elif kind == "D":
            changes.append(Change(status_text, Path(raw_paths[0]), None, source))
        else:
            changes.append(Change(status_text, None, Path(raw_paths[0]), source))
    return changes


def _collect_untracked_changes(errors: list[str]) -> list[Change]:
    command = ["ls-files", "-z", "--others", "--exclude-standard"]
    try:
        result = run_git(command)
    except OSError as exc:
        errors.append(f"git {' '.join(command)} failed:\n{exc}")
        return []
    if result.returncode != 0:
        errors.append(_git_failure(command, result))
        return []
    return [
        Change("?", None, Path(path), "untracked")
        for path in result.stdout.split("\0")
        if path
    ]


def _base_to_head_diff_args(base: str) -> list[str]:
    object_type = run_git(["cat-file", "-t", base])
    if object_type.returncode == 0 and object_type.stdout.strip() == "tree":
        return [base, "HEAD"]
    return [f"{base}...HEAD"]


def collect_changes(base: str | None) -> tuple[list[Change], list[str]]:
    """Collect committed, staged, unstaged, untracked, deleted, and rename/copy changes."""

    errors: list[str] = []
    changes: list[Change] = []
    if not base:
        errors.append("a base ref is required to prove the complete change set")
    if base:
        if ref_exists(base):
            changes.extend(
                _collect_diff_changes(
                    _base_to_head_diff_args(base),
                    "base-to-head",
                    errors,
                )
            )
        elif has_head():
            errors.append(
                f"base ref {base!r} does not exist. Fetch the base branch before fast feedback, "
                "or pass --base '' only for intentional local bootstrap checks."
            )
        else:
            errors.append(f"base ref {base!r} cannot be inspected because the repository has no HEAD")

    changes.extend(_collect_diff_changes([], "unstaged", errors))
    changes.extend(_collect_diff_changes(["--cached"], "staged", errors))
    changes.extend(_collect_untracked_changes(errors))

    seen: set[tuple[str, Path | None, Path | None, str]] = set()
    unique: list[Change] = []
    for change in changes:
        key = (change.status, change.old_path, change.new_path, change.source)
        if key not in seen:
            seen.add(key)
            unique.append(change)
    return unique, errors


def _surface_diff_args(source: str, base: str | None) -> list[str] | None:
    common = [
        "diff",
        "--no-color",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
    ]
    if source == "base-to-head":
        return [*common, *_base_to_head_diff_args(base)] if base else None
    if source == "staged":
        return [*common, "--cached"]
    if source == "unstaged":
        return common
    return None


def _surface_check_args(source: str, base: str | None) -> list[str] | None:
    common = ["diff", "--check", "--no-color", "--no-ext-diff"]
    if source == "base-to-head":
        return [*common, *_base_to_head_diff_args(base)] if base else None
    if source == "staged":
        return [*common, "--cached"]
    if source == "unstaged":
        return common
    return None


def _collect_surface_patches(
    changes: list[Change], base: str | None
) -> tuple[dict[str, bytes], list[str]]:
    patches: dict[str, bytes] = {}
    errors: list[str] = []
    for source in sorted({change.source for change in changes}):
        args = _surface_diff_args(source, base)
        if args is None:
            if source != "untracked":
                errors.append(f"no snapshot command is defined for change source {source!r}")
            continue
        try:
            result = run_git_bytes(args)
        except OSError as exc:
            errors.append(f"git {' '.join(args)} failed:\n{exc}")
            continue
        if result.returncode != 0:
            errors.append(_git_bytes_failure(args, result))
            continue
        if not result.stdout:
            errors.append(f"git {' '.join(args)} returned an empty patch for reported {source} changes")
            continue
        patches[source] = result.stdout
    return patches, errors


def _safe_repo_path(path: Path) -> bool:
    text = path.as_posix()
    pure = PurePosixPath(text)
    return bool(text) and not pure.is_absolute() and ".." not in pure.parts and not any(
        ord(character) < 32 or 0xDC80 <= ord(character) <= 0xDCFF for character in text
    )


def _document_path_kind(path: Path) -> str | None:
    text = path.as_posix()
    if not _safe_repo_path(path):
        return None
    if path.name in {"AGENTS.md", "AGENTS.override.md"} and (
        path.parent == Path(".") or text.startswith("docs/")
    ):
        return CONTRACT_DOCS
    if text in CONTRACT_DOCUMENTS:
        return CONTRACT_DOCS
    if text in ROOT_DOCUMENTS:
        return EDITORIAL_DOCS
    if text.startswith((".github/ISSUE_TEMPLATE/", ".github/PULL_REQUEST_TEMPLATE/")):
        return CONTRACT_DOCS if path.suffix in DOCUMENT_EXTENSIONS else None
    if not text.startswith("docs/") or path.suffix not in DOCUMENT_EXTENSIONS:
        return None
    tokens = set(re.split(r"[^a-z0-9]+", text.lower()))
    if tokens & CONTRACT_NAME_TOKENS:
        return CONTRACT_DOCS
    return EDITORIAL_DOCS


def _mode_is_regular_nonexecutable(mode: str) -> bool:
    try:
        numeric = int(mode, 8)
    except ValueError:
        return False
    return stat.S_IFMT(numeric) == stat.S_IFREG and not numeric & 0o111


def _historical_document(path: Path, base: str | None) -> tuple[bytes | None, str | None]:
    text = path.as_posix()
    candidates = [(f":{text}", "index"), (f"HEAD:{text}", "HEAD")]
    if base:
        candidates.append((f"{base}:{text}", base))
    for object_name, ref in candidates:
        try:
            blob = run_git(["show", object_name])
        except OSError as exc:
            return None, f"could not inspect {text}: {exc}"
        if blob.returncode != 0:
            continue
        if ref == "index":
            mode_result = run_git(["ls-files", "--stage", "--", text])
        else:
            mode_result = run_git(["ls-tree", ref, "--", text])
        if mode_result.returncode != 0:
            return None, _git_failure(["mode inspection", ref, "--", text], mode_result)
        mode_line = next((line for line in mode_result.stdout.splitlines() if line), "")
        mode = mode_line.split(maxsplit=1)[0] if mode_line else ""
        if not _mode_is_regular_nonexecutable(mode):
            return None, f"documentation path is executable, non-regular, or has unknown mode: {text}"
        return blob.stdout.encode("utf-8", errors="surrogateescape"), None
    return None, f"documentation content could not be inspected: {text}"


def _inspect_document(path: Path, base: str | None) -> tuple[str | None, str | None]:
    text = path.as_posix()
    # Full-gate ownership takes precedence over documentation names.
    if (
        text in FULL_GATE_EXACT_PATHS
        or text.startswith(FULL_GATE_PREFIXES)
        or path.suffix in FULL_GATE_EXTENSIONS
    ):
        return None, f"full-gate-path:{text}"
    kind = _document_path_kind(path)
    if kind is None:
        return None, f"unknown-path-or-type:{text}"

    absolute = ROOT / path
    if absolute.is_symlink():
        return None, f"documentation path is a symlink:{path.as_posix()}"
    if absolute.exists():
        if not absolute.is_file():
            return None, f"documentation path is not a regular file:{path.as_posix()}"
        try:
            mode = absolute.stat().st_mode
            if mode & 0o111:
                return None, f"documentation path is executable:{path.as_posix()}"
            data = absolute.read_bytes()
        except OSError as exc:
            return None, f"documentation path could not be inspected:{path.as_posix()}:{exc}"
    else:
        data, error = _historical_document(path, base)
        if error:
            return None, error
        assert data is not None

    if len(data) > MAX_DOCUMENT_BYTES:
        return None, f"documentation path exceeds inspection limit:{path.as_posix()}"
    if b"\0" in data:
        return None, f"documentation path contains binary content:{path.as_posix()}"
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"documentation path is not UTF-8 text:{path.as_posix()}"
    if kind == EDITORIAL_DOCS and CONTRACT_CONTENT.search(content):
        kind = CONTRACT_DOCS
    return kind, None


def _inspect_surface_patches(
    patches: dict[str, bytes], document_count: int
) -> tuple[bool, list[str]]:
    contract_content_changed = False
    errors: list[str] = []
    inspection_limit = max(1, document_count) * (MAX_DOCUMENT_BYTES + 64 * 1024)
    for source, patch in sorted(patches.items()):
        if len(patch) > inspection_limit:
            errors.append(f"documentation {source} patch exceeds inspection limit")
            continue
        if any(marker in patch for marker in GIT_BINARY_MARKERS):
            errors.append(f"documentation {source} patch contains binary content")
            continue
        for raw_mode in GIT_MODE_LINE.findall(patch):
            mode = raw_mode.decode("ascii")
            if not _mode_is_regular_nonexecutable(mode):
                errors.append(f"unsafe-document-mode:{source}:{mode}")
        try:
            text = patch.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"documentation {source} patch is not UTF-8 text")
            continue
        changed_content = "\n".join(
            line[1:] for line in text.splitlines() if line.startswith(("+", "-"))
        )
        if CONTRACT_CONTENT.search(changed_content):
            contract_content_changed = True
    return contract_content_changed, errors


def _snapshot_fingerprint(
    changes: list[Change],
    changed_paths: tuple[Path, ...],
    surface_patches: dict[str, bytes] | None = None,
) -> str:
    digest = hashlib.sha256()
    for change in sorted(
        changes,
        key=lambda item: (
            item.source,
            item.status,
            item.old_path.as_posix() if item.old_path else "",
            item.new_path.as_posix() if item.new_path else "",
        ),
    ):
        fields = (
            change.source,
            change.status,
            change.old_path.as_posix() if change.old_path else "",
            change.new_path.as_posix() if change.new_path else "",
        )
        digest.update("\0".join(fields).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0\0")

    if surface_patches is not None:
        for source, patch in sorted(surface_patches.items()):
            digest.update(f"surface:{source}\0".encode())
            digest.update(patch)
            digest.update(b"\0\0")

    for path in changed_paths:
        text = path.as_posix()
        digest.update(text.encode("utf-8", errors="surrogateescape"))
        absolute = ROOT / path
        try:
            file_stat = absolute.lstat()
        except OSError as exc:
            digest.update(f"\0missing-or-unreadable:{type(exc).__name__}\0".encode())
            continue
        digest.update(f"\0mode:{stat.S_IFMT(file_stat.st_mode):o}:{stat.S_IMODE(file_stat.st_mode):o}\0".encode())
        if stat.S_ISLNK(file_stat.st_mode):
            try:
                digest.update(os.readlink(absolute).encode("utf-8", errors="surrogateescape"))
            except OSError as exc:
                digest.update(f"unreadable-link:{type(exc).__name__}".encode())
        elif stat.S_ISREG(file_stat.st_mode):
            try:
                with absolute.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                digest.update(f"unreadable-file:{type(exc).__name__}".encode())
        digest.update(b"\0\0")
    return digest.hexdigest()


def classify_changes(
    changes: list[Change],
    base: str | None,
    errors: list[str] | None = None,
    surface_patches: dict[str, bytes] | None = None,
) -> Classification:
    changed = tuple(
        sorted({path for change in changes for path in change.paths()}, key=lambda path: path.as_posix())
    )
    fallback: list[str] = []
    if errors:
        fallback.extend(f"repository-inspection-error:{error}" for error in errors)
    if not changes:
        fallback.append("no-changed-paths")

    path_kinds: dict[Path, str | None] = {}
    for path in changed:
        kind, error = _inspect_document(path, base)
        path_kinds[path] = kind
        if error:
            fallback.append(error)

    historical_contract = False
    if surface_patches is not None and path_kinds and all(
        kind is not None for kind in path_kinds.values()
    ):
        historical_contract, patch_errors = _inspect_surface_patches(
            surface_patches, len(path_kinds)
        )
        fallback.extend(patch_errors)

    for change in changes:
        if change.status[:1] not in {"A", "C", "D", "M", "R", "?"}:
            fallback.append(f"unknown-change-status:{change.status}")
        if change.status.startswith(("R", "C")):
            assert change.old_path is not None and change.new_path is not None
            old_kind = path_kinds.get(change.old_path)
            new_kind = path_kinds.get(change.new_path)
            if old_kind != new_kind:
                fallback.append(
                    f"category-crossing-{change.status[:1]}:{change.old_path.as_posix()}->{change.new_path.as_posix()}"
                )

    if fallback or any(kind is None for kind in path_kinds.values()):
        lane = FULL
    elif historical_contract or any(kind == CONTRACT_DOCS for kind in path_kinds.values()):
        lane = CONTRACT_DOCS
    else:
        lane = EDITORIAL_DOCS
    return Classification(
        lane,
        tuple(changes),
        changed,
        tuple(dict.fromkeys(fallback)),
        _snapshot_fingerprint(changes, changed, surface_patches),
    )


def classify_repository(base: str | None) -> Classification:
    changes, errors = collect_changes(base)
    surface_patches, surface_errors = _collect_surface_patches(changes, base)
    route = classify_changes(
        changes,
        base=base,
        errors=[*errors, *surface_errors],
        surface_patches=surface_patches,
    )
    identity = run_git(["rev-parse", "HEAD", base or "HEAD"])
    if identity.returncode:
        return escalate_to_full(route, "repository-identity-unavailable")
    fingerprint = hashlib.sha256(
        (route.fingerprint + "\0" + identity.stdout).encode("utf-8", errors="surrogateescape")
    ).hexdigest()
    return replace(route, fingerprint=fingerprint)


def escalate_to_full(route: Classification, reason: str) -> Classification:
    if route.lane == FULL:
        return route
    return replace(route, lane=FULL, fallback_reasons=(*route.fallback_reasons, reason))


def make_command(target: str, reason: str | None = None, base: str | None = BASE_UNSET,
                 validation_environment: Path | None = None) -> Command:
    """Select recursive Make's base explicitly while retaining policy context.

    BASE_UNSET leaves Make's default intact; None selects an empty base.
    The context environment remains available without overriding Make's BASE.
    """
    argv = ["make", f"PYTHON={shlex.join(PYTHON_ARGV)}"]
    if base != BASE_UNSET:
        argv.append(f"BASE={base or ''}")
    env = (("GRAPHIFY_VALIDATION_BASE", base or ""),) if base != BASE_UNSET else ()
    if validation_environment is not None:
        argv.append(f"VALIDATION_ENVIRONMENT={validation_environment}")
    argv.append(target)
    return Command(tuple(argv), reason or "", env)


def docs_whitespace_commands(route: Classification, base: str | None) -> list[Command]:
    if not route.changes:
        return []
    argv = [*PYTHON_ARGV, "-m", "tools.validation_route", "--check-change-set-whitespace"]
    argv.extend(("--base", base or ""))
    return [
        Command(
            tuple(argv),
            "Check Git whitespace rules and complete conflict-marker sets across the full snapshot.",
        )
    ]


def focused_contract_test_paths(route: Classification) -> list[str]:
    selected: set[str] = set()
    for path in route.changed_paths:
        text = path.as_posix()
        selected.update(CONTRACT_TESTS_BY_PATH.get(text, ()))
        for prefix, tests in CONTRACT_TESTS_BY_PREFIX:
            if text.startswith(prefix):
                selected.update(tests)
    return sorted(selected)


def focused_pytest_command(
    test_paths: list[str], durations: int | None, maxfail: int
) -> Command:
    argv = ["uv", "run", "--frozen", "python", "-m", "pytest", "-q", f"--maxfail={maxfail}"]
    if durations is not None:
        argv.append(f"--durations={durations}")
    argv.extend(test_paths)
    return Command(
        tuple(argv),
        "Run only tests directly bound to the changed documentation contracts.",
    )


def build_plan(
    route: Classification,
    base: str | None,
    full: bool,
    durations: int | None,
    maxfail: int,
    validation_environment: Path | None = None,
) -> list[Command]:
    if full or route.lane == FULL:
        return [
            make_command("review-ready", "Run the full readiness gate, including complete-set preflight.",
                         base=base, validation_environment=validation_environment),
        ]

    commands = [
        make_command(
            "docs-check",
            "Reclassify the complete change set and validate documentation whitespace/conflict markers.",
            base=base,
        )
    ]
    commands.append(Command(
        ("uv", "sync", "--frozen", "--inexact"),
        "Prepare documentation checks without removing installed optional packages.",
    ))
    commands.append(Command(
        ("uv", "run", "--frozen", "python", "-m", "pytest", "tests/test_readme_policy.py", "-q", "--tb=short"),
        "Keep the complete Git inventory under the README naming policy.",
    ))
    if route.lane == CONTRACT_DOCS:
        contract_tests = [path for path in focused_contract_test_paths(route)
                          if path != "tests/test_readme_policy.py"]
        if contract_tests:
            commands.append(focused_pytest_command(contract_tests, durations, maxfail))
    return dedupe(commands)


def dedupe(commands: list[Command]) -> list[Command]:
    seen: set[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = set()
    unique: list[Command] = []
    for command in commands:
        key = (command.argv, command.env)
        if key not in seen:
            unique.append(command)
            seen.add(key)
    return unique


def skipped_checks(route: Classification, *, docs_check_mode: bool = False) -> list[tuple[str, str]]:
    if route.lane == FULL:
        return []
    skips = [("lint", f"{route.lane} contains no executable Python changes")]
    if route.lane == EDITORIAL_DOCS:
        skips.append(
            (
                "full-suite-and-lastfailed-pytest",
                "editorial-docs runs README naming only; ignores pytest lastfailed state",
            )
        )
        skips.append(("contract-focused-checks", "editorial-docs selects whitespace/conflicts and README naming only"))
    else:
        contract_tests = focused_contract_test_paths(route)
        if contract_tests:
            skips.append(
                (
                    "full-suite-and-lastfailed-pytest",
                    "contract-docs runs only path-bound tests and ignores pytest lastfailed state",
                )
            )
        else:
            skips.append(
                (
                    "full-suite-and-lastfailed-pytest",
                    "contract-docs runs README naming only and ignores pytest lastfailed state",
                )
            )
        if docs_check_mode:
            skips.append(("contract-focused-checks", "docs-check is low-level; make fast-feedback owns final contract-doc routing"))
    return skips


def route_fingerprint(route: Classification) -> str:
    if route.fingerprint:
        return route.fingerprint
    return _snapshot_fingerprint(list(route.changes), ())


def same_route_snapshot(left: Classification, right: Classification) -> bool:
    return (
        left.lane == right.lane
        and left.changes == right.changes
        and left.changed_paths == right.changed_paths
        and left.fallback_reasons == right.fallback_reasons
        and route_fingerprint(left) == route_fingerprint(right)
    )


def print_receipt(
    route: Classification,
    commands: list[Command],
    *,
    docs_check_mode: bool = False,
) -> None:
    receipt_kind = "validation_check_receipt" if docs_check_mode else "validation_route_receipt"
    print(f"{receipt_kind}=v1")
    print(f"repo_root={json.dumps(str(ROOT), ensure_ascii=True)}")
    print(f"selected_lane={route.lane}")
    print(f"change_set_fingerprint=sha256:{route_fingerprint(route)}")
    if route.changed_paths:
        print("changed_paths:")
        for path in route.changed_paths:
            print(f"  - {json.dumps(path.as_posix(), ensure_ascii=True)}")
    else:
        print("changed_paths=none")
    if commands:
        print("commands:")
        for index, command in enumerate(commands, start=1):
            print(f"  {index}. {json.dumps(command.shell(), ensure_ascii=True)}")
            print(f"     reason: {json.dumps(command.reason, ensure_ascii=True)}")
    else:
        print("commands=none")
    skips = skipped_checks(route, docs_check_mode=docs_check_mode)
    if skips:
        print("skipped_checks:")
        for check, reason in skips:
            print(f"  - {check}: {json.dumps(reason, ensure_ascii=True)}")
    else:
        print("skipped_checks=none")
    if route.fallback_reasons:
        print("fallback_reasons:")
        for reason in route.fallback_reasons:
            print(f"  - {json.dumps(reason, ensure_ascii=True)}")
    else:
        print("fallback_reason=none")
    sys.stdout.flush()


def check_untracked_whitespace(paths: list[str]) -> int:
    failures: list[str] = []
    for raw_path in paths:
        path = ROOT / raw_path
        if path.is_symlink():
            failures.append(f"{raw_path}: symlinks are not eligible for docs-check")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            failures.append(f"{raw_path}: could not read file: {exc}")
            continue
        if b"\0" in data:
            failures.append(f"{raw_path}: binary content is not eligible for docs-check")
            continue

        text = data.decode("utf-8", errors="replace")
        failures.extend(
            _text_whitespace_failures(
                raw_path,
                text.splitlines(),
                new_blank_at_eof=_has_new_blank_line_at_eof(data),
            )
        )

    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


def _is_git_conflict_marker(line: str, marker: str) -> bool:
    return line == marker or line.startswith(f"{marker} ")


def _diagnostic_label(label: str) -> str:
    """Keep one logical path label on one physical diagnostic line."""
    return json.dumps(label, ensure_ascii=True)[1:-1]


def _text_conflict_failures(
    label: str, lines: list[str], *, require_complete_set: bool = False,
) -> list[str]:
    """Report marker locations, optionally requiring a complete conflict set.

    Existing file content needs an opener, separator and closer to distinguish
    conflicts from unchanged literals. Added lines and new files remain strict.
    """
    failures: list[str] = []
    opener = any(_is_git_conflict_marker(line, "<<<<<<<") for line in lines)
    separator = any(line == "=======" for line in lines)
    closer = any(_is_git_conflict_marker(line, ">>>>>>>") for line in lines)
    conflict_set = opener and separator and closer
    if require_complete_set and not conflict_set:
        return failures
    for line_number, line in enumerate(lines, start=1):
        if (
            _is_git_conflict_marker(line, "<<<<<<<")
            or _is_git_conflict_marker(line, "|||||||")
            or _is_git_conflict_marker(line, ">>>>>>>")
            or (conflict_set and line == "=======")
        ):
            failures.append(f"{label}:{line_number}: conflict marker")
    return failures


def _text_whitespace_failures(
    label: str,
    lines: list[str],
    *,
    new_blank_at_eof: bool = False,
) -> list[str]:
    failures: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if line.rstrip(" \t") != line:
            failures.append(f"{label}:{line_number}: trailing whitespace")
        indent = line[: len(line) - len(line.lstrip(" \t"))]
        if " \t" in indent:
            failures.append(f"{label}:{line_number}: space before tab in indent")
    if new_blank_at_eof:
        failures.append(f"{label}:{max(len(lines), 1)}: new blank line at EOF")
    failures.extend(_text_conflict_failures(label, lines))
    return failures


def _has_new_blank_line_at_eof(data: bytes) -> bool:
    return data.replace(b"\r\n", b"\n").endswith(b"\n\n")


def _patch_conflict_failures(source: str, patch: bytes) -> list[str]:
    sections = re.split(rb"(?m)(?=^diff --git )", patch)
    failures: list[str] = []
    for raw_section in sections:
        if not raw_section or any(marker in raw_section for marker in GIT_BINARY_MARKERS):
            continue
        text = raw_section.decode("utf-8", errors="surrogateescape")
        lines = text.splitlines()
        if not lines:
            continue
        section = source
        if lines[0].startswith("diff --git "):
            section = f"{source}:{lines[0].removeprefix('diff --git ')}"
        added_lines = [
            line[1:]
            for line in lines
            if line.startswith("+") and not line.startswith("+++ ")
        ]
        failures.extend(_text_conflict_failures(section, added_lines))
    return failures


def _surface_text_conflict_failures(
    changes: list[Change],
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    errors: list[str] = []
    surfaces = sorted(
        {
            (change.source, change.new_path.as_posix())
            for change in changes
            if change.new_path is not None
            and change.source in {"base-to-head", "staged", "unstaged"}
        }
    )
    for source, raw_path in surfaces:
        if source == "base-to-head":
            args = ["show", f"HEAD:{raw_path}"]
            try:
                result = run_git_bytes(args)
            except OSError as exc:
                errors.append(f"git {' '.join(args)} failed:\n{exc}")
                continue
            if result.returncode != 0:
                errors.append(_git_bytes_failure(args, result))
                continue
            data = result.stdout
        elif source == "staged":
            args = ["show", f":{raw_path}"]
            try:
                result = run_git_bytes(args)
            except OSError as exc:
                errors.append(f"git {' '.join(args)} failed:\n{exc}")
                continue
            if result.returncode != 0:
                errors.append(_git_bytes_failure(args, result))
                continue
            data = result.stdout
        else:
            path = ROOT / raw_path
            if path.is_symlink():
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                errors.append(f"could not inspect {source} file {raw_path}: {exc}")
                continue

        if b"\0" in data:
            continue
        lines = data.decode("utf-8", errors="surrogateescape").splitlines()
        failures.extend(
            _text_conflict_failures(
                f"{source}:{_diagnostic_label(raw_path)}",
                lines,
                require_complete_set=True,
            )
        )
    return failures, errors


def _git_check_diagnostics(
    label: str,
    args: list[str],
    *,
    clean_returncodes: set[int],
    diagnostic_returncodes: set[int],
) -> tuple[list[str], list[str]]:
    try:
        result = run_git(args)
    except OSError as exc:
        return [], [f"git {' '.join(args)} failed:\n{exc}"]
    if result.returncode in clean_returncodes:
        return [], []
    if result.returncode not in diagnostic_returncodes or result.stderr.strip():
        return [], [_git_failure(args, result)]

    failures: list[str] = []
    diagnostics = 0
    for line in result.stdout.splitlines():
        match = re.match(r"^.+:\d+: (?P<message>.+)$", line)
        if not match:
            continue
        diagnostics += 1
        failures.append(f"{label}:{line}")
    if diagnostics == 0:
        return [], [_git_failure(args, result)]
    return failures, []


def check_change_set_whitespace(base: str | None) -> int:
    changes, errors = collect_changes(base)
    patches, patch_errors = _collect_surface_patches(changes, base)
    errors.extend(patch_errors)
    if errors:
        print("change-set whitespace check failed during repository inspection:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    failures: list[str] = []
    for source in sorted({change.source for change in changes}):
        args = _surface_check_args(source, base)
        if args is None:
            continue
        source_failures, source_errors = _git_check_diagnostics(
            source,
            args,
            clean_returncodes={0},
            diagnostic_returncodes={2},
        )
        failures.extend(source_failures)
        errors.extend(source_errors)
    for source, patch in sorted(patches.items()):
        failures.extend(_patch_conflict_failures(source, patch))
    surface_conflicts, surface_errors = _surface_text_conflict_failures(changes)
    failures.extend(surface_conflicts)
    errors.extend(surface_errors)
    untracked = sorted(
        {
            path.as_posix()
            for change in changes
            if change.source == "untracked"
            for path in change.paths()
        }
    )
    for raw_path in untracked:
        path = ROOT / raw_path
        if path.is_symlink() or not path.is_file():
            continue
        diagnostic_path = _diagnostic_label(raw_path)
        path_failures, path_errors = _git_check_diagnostics(
            diagnostic_path,
            [
                "diff",
                "--no-index",
                "--check",
                "--no-color",
                "--no-ext-diff",
                "--",
                os.devnull,
                raw_path,
            ],
            clean_returncodes={0, 1},
            diagnostic_returncodes={2, 3},
        )
        failures.extend(path_failures)
        errors.extend(path_errors)
        try:
            data = path.read_bytes()
        except OSError as exc:
            failures.append(f"{diagnostic_path}: could not read file: {exc}")
            continue
        if b"\0" in data:
            continue
        failures.extend(
            _text_conflict_failures(
                diagnostic_path,
                data.decode("utf-8", errors="replace").splitlines(),
            )
        )

    if errors:
        print("change-set whitespace check failed during repository inspection:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    for failure in dict.fromkeys(failures):
        print(failure, file=sys.stderr)
    return 1 if failures else 0


def run_commands(commands: list[Command], admission: ValidationEnvironment | None = None) -> int:
    if admission is None:
        for command in commands:
            if command.argv[0] == "uv":
                raise EnvironmentRefused("uv validation requires environment admission")
            print(f"\n$ {command.shell()}", flush=True)
            result = subprocess.run(command.argv, cwd=ROOT, env=git_environment(), check=False)
            if result.returncode:
                return result.returncode
        return 0
    setup_done = False
    for command in commands:
        check_uv_argv(command.argv)
        is_pytest = command.argv[:6] == ("uv", "run", "--frozen", "python", "-m", "pytest")
        if is_pytest and not setup_done:
            raise EnvironmentRefused("pytest requires completed admitted setup")
        if is_pytest:
            admission.before_tests()
        else:
            admission.verify(allow_absent=admission.new_local)
        env = admission.child_environment()
        if any(key != "GRAPHIFY_VALIDATION_BASE" for key, _ in command.env):
            raise EnvironmentRefused("unsupported child environment override")
        env.update(dict(command.env))
        print(f"\n$ {command.shell()}", flush=True)
        result = subprocess.run(command.argv, cwd=ROOT, env=env, check=False)
        if result.returncode != 0:
            return result.returncode
        if command.argv[:2] == ("uv", "sync"):
            admission.proof = admission.verify()
            admission.record("postsetup")
            setup_done = True
        if is_pytest:
            admission.verify()
    admission.proof = admission.verify()
    admission.record("postcheck")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE, help="base ref for complete change-set inspection")
    parser.add_argument("--dry-run", action="store_true", help="print the validation receipt without running commands")
    parser.add_argument("--full", action="store_true", help="escalate the inferred lane to the unchanged full gate")
    parser.add_argument("--durations", type=int, help="retained for CLI compatibility; full pytest is owned by review-ready")
    parser.add_argument("--maxfail", type=int, default=1, help="retained for CLI compatibility")
    parser.add_argument("--ci-plan", action="store_true", help="run eligible docs checks; delegate full validation to existing CI jobs")
    parser.add_argument("--validation-environment", type=Path,
                        help="explicit existing, owned environment bound to this checkout and running Python")
    parser.add_argument("--full-checks", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--github-output", type=Path, help="write successful CI routing outputs to this GitHub output file")
    parser.add_argument("--docs-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-change-set-whitespace", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-untracked-whitespace", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("check_paths", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.check_paths and not args.check_untracked_whitespace:
        parser.error(f"unexpected arguments: {' '.join(args.check_paths)}")
    if args.durations is not None and args.durations < 0:
        parser.error("--durations must be non-negative")
    if args.maxfail < 1:
        parser.error("--maxfail must be at least 1")
    if args.github_output and not args.ci_plan:
        parser.error("--github-output requires --ci-plan")
    if args.ci_plan and (args.docs_check or args.dry_run):
        parser.error("--ci-plan cannot be combined with --docs-check or --dry-run")
    if args.full and args.docs_check:
        parser.error("--full and --docs-check cannot be combined")
    if args.full_checks and (args.full or args.docs_check or args.dry_run or args.ci_plan):
        parser.error("--full-checks cannot be combined with other routing modes")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run admitted checks and return 3 if their repository snapshot changes."""
    args = parse_args(argv)
    if args.check_change_set_whitespace:
        return check_change_set_whitespace(args.base or None)
    if args.check_untracked_whitespace:
        return check_untracked_whitespace(args.check_paths)

    admission = None
    if not (args.docs_check or args.dry_run):
        try:
            admission = ValidationEnvironment(ROOT, args.validation_environment)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"validation_route_status=failed; reason=validation-environment; {exc}", file=sys.stderr)
            return 2

    if args.full_checks:
        before = classify_repository(args.base or None)
        result = check_change_set_whitespace(args.base or None)
        if result:
            return result
        try:
            result = run_commands([
                Command(("uv", "sync", "--all-extras", "--frozen"), "Match the existing full CI environment."),
                Command(("uv", "run", "--frozen", "python", "-m", "pytest", "tests/", "-q", "--tb=short"),
                        "Run the entire existing pytest suite."),
            ], admission)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"validation_check_status=failed; reason=validation-environment; {exc}", file=sys.stderr)
            return 2
        if result:
            return result
        result = check_change_set_whitespace(args.base or None)
        if result:
            return result
        if not same_route_snapshot(classify_repository(args.base or None), before):
            print(
                "validation_check_status=invalidated; check=full; "
                "reason=change-set-changed-during-validation", file=sys.stderr,
            )
            return 3
        return 0

    base = args.base or None
    status_kind = "validation_check_status" if args.docs_check else "validation_route_status"
    route = classify_repository(base)
    if args.docs_check:
        commands = docs_whitespace_commands(route, base) if route.lane != FULL else []
        print_receipt(route, commands, docs_check_mode=True)
        if route.lane == FULL:
            print(f"{status_kind}=failed; check=docs-check", file=sys.stderr)
            print("docs-check=failed; reason=complete change set is not proven documentation-only", file=sys.stderr)
            return 2
    else:
        if args.full:
            route = escalate_to_full(route, "forced-by---full")
        commands = build_plan(
            route,
            base=base,
            full=False,
            durations=args.durations,
            maxfail=args.maxfail,
            validation_environment=admission.path if admission is not None and args.validation_environment
            else args.validation_environment,
        )
        print_receipt(route, commands)

    if args.dry_run:
        print(f"{status_kind}=printed; selected_lane={route.lane}")
        return 0
    if args.ci_plan and route.lane == FULL:
        print("validation_route_status=deferred; selected_lane=full; reason=existing-ci-jobs")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write("lane=full\nstatus=deferred\n")
        return 0
    try:
        result = run_commands(commands, admission) if admission is not None else run_commands(commands)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{status_kind}=failed; reason=validation-environment; {exc}", file=sys.stderr)
        return 2
    if result != 0:
        check_suffix = "; check=docs-check" if args.docs_check else ""
        print(f"{status_kind}=failed; exit={result}{check_suffix}", file=sys.stderr)
        return result
    fresh_route = classify_repository(base)
    if args.full:
        fresh_route = escalate_to_full(fresh_route, "forced-by---full")
    if not same_route_snapshot(fresh_route, route):
        print(
            f"{status_kind}=invalidated; reason=change-set-changed-during-validation",
            file=sys.stderr,
        )
        print_receipt(fresh_route, [], docs_check_mode=args.docs_check)
        return 3
    if args.docs_check:
        print(f"validation_check_status=complete; check=docs-check; selected_lane={route.lane}")
    else:
        print(f"validation_route_status=complete; selected_lane={route.lane}")
    if args.ci_plan and args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"lane={route.lane}\nstatus=complete\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
