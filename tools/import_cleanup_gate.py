"""Verify an explicitly approved local import-cleanup case using retained proof.

This pilot reads evidence and Git objects. It never runs a test, setup command,
archived producer, or full gate. Missing proof blocks acceptance. The external
approval digest is an operator trust input, not independent authentication.
"""

from __future__ import annotations

import argparse
import ast
import base64
from collections import Counter
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from tools.validation_environment import git_environment


SCHEMA = "graphify-import-cleanup-approval-v1"
TEST_PATHS = (
    "tests/test_replacement_boundary_repairs.py",
    "tests/test_replacement_record_and_lock_admission.py",
    "tests/test_replacement_replay_admission.py",
    "tests/test_replacement_review12.py",
    "tests/test_replacement_review14.py",
    "tests/test_replacement_review16.py",
    "tests/test_replacement_review20.py",
)
ROOT = Path(__file__).resolve().parents[1]


class ProofRefused(ValueError):
    """The selected local case lacks required, unchanged approval or proof."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProofRefused(reason)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def git(root: Path, *args: str) -> bytes:
    """Read physical Git state with fsmonitor hooks, optional locks and replacements disabled."""
    env = git_environment()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.check_output(
        ["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false",
         "-C", str(root), *args],
        env=env, stderr=subprocess.PIPE, timeout=20,
    )


def identity(path: Path) -> dict:
    """Return device, inode and owner for a retained non-symlink directory."""
    info = path.stat()
    require(not path.is_symlink(), f"aliased retained directory: {path}")
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}


def checked_file(binding: dict) -> bytes:
    """Read an absolute, non-symlink evidence file and require its pinned digest."""
    path = Path(binding["path"])
    require(path.is_absolute() and not path.is_symlink(), f"invalid evidence path: {path}")
    data = path.read_bytes()
    require(sha256(data) == binding["sha256"], f"evidence digest changed: {path}")
    return data


def delta(root: Path, before: str, after: str) -> bytes:
    return git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
               "--binary", "--full-index", before, after, "--")


def check_import_delta(root: Path, before: str, after: str, expected: str) -> list[str]:
    """Allow only the exact reviewed removal of top-level test imports.

All other AST nodes, files and modes must match. Whole PR changes remain bound
separately; this comparison is against the original fully validated head.
"""
    require(sha256(delta(root, before, after)) == expected, "unreviewed cleanup delta")
    raw = git(root, "diff", "--raw", "-z", "--no-renames", before, after, "--")
    parts = raw.split(b"\0")
    changed = []
    for index in range(0, len(parts) - 1, 2):
        fields = parts[index].decode().split()
        path = parts[index + 1].decode()
        require(fields[0] == ":100644" and fields[1] == "100644" and fields[-1] == "M",
                f"unsupported cleanup mode or status: {path}")
        require(path in TEST_PATHS, f"non-pilot path changed: {path}")
        trees = [ast.parse(git(root, "show", f"{ref}:{path}")) for ref in (before, after)]
        imports = []
        for tree in trees:
            entries = Counter()
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        require(alias.name != "*", f"wildcard import: {path}")
                        entries[(type(node).__name__, getattr(node, "module", None),
                                 getattr(node, "level", 0), alias.name, alias.asname)] += 1
            imports.append(entries)
            tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        require(not (imports[1] - imports[0]), f"new import binding: {path}")
        require(ast.dump(trees[0]) == ast.dump(trees[1]), f"non-import test behavior changed: {path}")
        changed.append(path)
    return changed


def dependency_inventory(repo: Path, deadline: float, cache: dict) -> str:
    """Hash and check installed dependency RECORD files, without importing them.

    Project source is checked separately against Git. Only exact editable paths
    and a launcher's environment-specific shebang are normalized.
RECORD hashes are checked against current payload bytes. Bytecode and RECORD's
own unhashed entry carry no installed-payload assertion.
"""
    site = repo / ".venv/lib/python3.14/site-packages"
    require(site.is_dir() and site.resolve() == site, "missing or aliased dependency environment")
    inventory, recorded = {}, set()
    for distribution in sorted(site.glob("*.dist-info")):
        require(time.monotonic() < deadline, "bounded verification deadline exceeded")
        entries = []
        for relative, encoded, size in csv.reader(io.StringIO((distribution / "RECORD").read_text())):
            require(time.monotonic() < deadline, "bounded verification deadline exceeded")
            path = site / relative
            require(path.resolve().is_relative_to(repo / ".venv"), "dependency payload escapes environment")
            recorded.add(path.resolve())
            if not encoded:
                require(path.name == "RECORD" or path.suffix == ".pyc",
                        f"unhashed installed payload: {relative}")
                continue
            require(stat.S_ISREG(path.lstat().st_mode), f"non-regular dependency payload: {relative}")
            info = path.stat()
            key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if key not in cache:
                data = path.read_bytes()
                actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                first, separator, body = data.partition(b"\n") if data.startswith(b"#!") else (None, b"", b"")
                cache[key] = (actual, len(data), first, sha256(body) if separator else None)
            actual, actual_size, first, body_hash = cache[key]
            require(encoded.startswith("sha256="), "unsupported installed RECORD hash")
            require(encoded == f"sha256={actual}" and actual_size == int(size),
                    f"installed dependency changed: {relative}")
            if relative.startswith("../../../bin/") and first == f"#!{repo}/.venv/bin/python".encode():
                encoded = "sha256:" + body_hash
            if distribution.name.startswith("graphifyy-"):
                if path.suffix == ".pth" and path.read_text().strip() == str(repo):
                    encoded = "editable-project-path"
                elif path.name.startswith("__editable___") and path.suffix == ".py":
                    tree = ast.parse(path.read_bytes())
                    mappings = [node for node in tree.body if isinstance(node, ast.AnnAssign)
                                and isinstance(node.target, ast.Name) and node.target.id == "MAPPING"]
                    require(len(mappings) == 1 and ast.literal_eval(mappings[0].value)
                            == {"graphify": str(repo / "graphify")}, "foreign editable finder")
                    mappings[0].value = ast.Constant(value="approved-checkout/graphify")
                    encoded = "sha256:" + sha256(ast.dump(tree).encode())
                elif path.name == "direct_url.json":
                    # check_checkout validates the exact URL and editable mode.
                    encoded = "editable-project-origin"
                elif path.name == "uv_cache.json":
                    value = json.loads(path.read_text())
                    value.pop("timestamp", None)  # installer freshness, not runtime payload
                    encoded = "sha256:" + sha256(canonical(value))
            entries.append((relative, encoded))
        inventory[distribution.name] = sorted(entries)
    require(bool(inventory), "empty dependency inventory")
    for path in site.rglob("*"):
        require(time.monotonic() < deadline, "bounded verification deadline exceeded")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        require(not path.is_symlink(), "aliased installed payload")
        if path.is_file():
            require(path in recorded, f"unrecorded installed payload: {path.relative_to(site)}")
    return sha256(canonical(inventory))


def check_checkout(result: dict) -> Path:
    """Require the recorded clean checkout, environment identity and editable origin.

    Return its absolute path; read Git and metadata without running its Python.
    Missing files or Git errors propagate; mismatched proof raises ProofRefused."""
    repo = Path(result["checkout"])
    require(repo.is_absolute() and repo.resolve() == repo, "aliased retained checkout")
    require(git(repo, "rev-parse", "--show-toplevel").decode().strip() == str(repo), "foreign Git root")
    require(git(repo, "rev-parse", "HEAD", "HEAD^{tree}").decode().splitlines()
            == [result["head"], result["tree"]], "retained checkout head/tree changed")
    require(not git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            "retained committed/staged/unstaged/untracked snapshot is dirty")
    require(identity(repo) == result["checkout_identity"], "retained checkout identity changed")
    require(identity(repo / ".venv") == result["venv_identity"], "retained environment identity changed")
    require(sha256((repo / "uv.lock").read_bytes()) == result["lock_sha256"], "retained lock changed")
    cfg = (repo / ".venv/pyvenv.cfg").read_text()
    require("version_info = 3.14.10\n" in cfg and "include-system-site-packages = false\n" in cfg,
            "pilot requires checkout-local CPython 3.14.10 without system packages")
    direct = list((repo / ".venv/lib/python3.14/site-packages").glob("graphifyy-*.dist-info/direct_url.json"))
    require(len(direct) == 1, "missing or duplicate editable metadata")
    origin = json.loads(direct[0].read_text())
    require(origin == {"url": repo.as_uri(), "dir_info": {"editable": True}}, "editable origin changed")
    return repo


def check_bindings(bindings: list, repo: Path) -> None:
    """Require both recorded import observations to name the retained environment."""
    require(len(bindings) == 2, "normal and isolated import proof required")
    for binding in bindings:
        url = urlparse(binding["direct_url"]["url"])
        require(binding["version"] == "3.14.10" and binding["prefix"] == str(repo / ".venv")
                and binding["package"] == str(repo / "graphify/__init__.py")
                and url.scheme == "file" and not url.netloc and not url.query and not url.fragment
                and unquote(url.path) == str(repo)
                and binding["direct_url"]["dir_info"]["editable"] is True,
                "normal/isolated package binding mismatch")


def check_interpreter(row: dict, old_repo: Path, new_repo: Path) -> None:
    """Recheck retained interpreter targets and bytes at each observation."""
    require((old_repo / ".venv/bin/python").resolve() == (new_repo / ".venv/bin/python").resolve(),
            "different retained base interpreter")
    require(sha256((old_repo / ".venv/bin/python").read_bytes()) == row["interpreter_sha256"],
            "retained interpreter changed")


def check_row(root: Path, row: dict, deadline: float, cache: dict) -> dict:
    """Verify one retained full baseline and reviewed cleanup; return prefix proof.

    Read bound files, Git objects and installed payloads without running tests.
    Reuse the caller's payload cache across rows until the monotonic deadline.
    Refuse mismatched proof; file, parse and subprocess errors propagate."""
    full_raw = checked_file(row["full_result"])
    focused_raw = checked_file(row["focused_result"])
    full, focused = json.loads(full_raw), json.loads(focused_raw)
    for result, head, base in [(full, row["old_head"], row["old_base"]),
                               (focused, row["head"], row["base"])]:
        require(result["head"] == head and result["base"] == base, "receipt head/base mismatch")
        require(result["passed"] is True and result["exit_code"] == 0, "failed or incomplete prior proof")
        require(git(root, "rev-parse", f"{head}^{{tree}}").decode().strip() == result["tree"],
                "receipt tree mismatch")
        require(git(root, "merge-base", head, base).decode().strip() == base, "non-ancestor PR base")
        git(root, "diff", "--check", base, head, "--")
    require(full["complete_full_route"] is True and full["postcheck_environment_admitted"] is True,
            "baseline is not a complete admitted full pass")
    require(focused["full_route"] is False, "focused proof has wrong route identity")
    old_repo, new_repo = check_checkout(full), check_checkout(focused)
    full_log = checked_file(row["full_log"])
    require(sha256(full_log) == full["make_log_sha256"], "full log binding mismatch")
    require(b"validation_route_status=complete; selected_lane=full" in full_log,
            "missing full-route completion")
    require(full["command"] == ["make", f"PYTHON={old_repo}/.venv/bin/python",
                                f"BASE={row['old_base']}", "fast-feedback"], "unexpected full command")
    env_receipts = [json.loads(line.split(b"=", 1)[1]) for line in full_log.splitlines()
                    if line.startswith(b"validation_environment_receipt=")]
    require(bool(env_receipts), "missing baseline environment admission")
    for env in env_receipts:
        require(env["python_version"] == [3, 14, 10]
                and env["actual_package_origin"] == str(old_repo / "graphify/__init__.py")
                and env["editable_origin"] == str(old_repo)
                and env["normal_prefix"] == env["isolated_prefix"] == str(old_repo / ".venv"),
                "baseline environment binding mismatch")
    check_bindings(focused["import_bindings"], new_repo)
    require(full["lock_sha256"] == focused["lock_sha256"], "dependency lock changed")
    check_interpreter(row, old_repo, new_repo)
    for repo in (old_repo, new_repo):
        require(dependency_inventory(repo, deadline, cache) == row["dependency_fingerprint"],
                "dependency inventory changed")
    changed = check_import_delta(root, row["old_head"], row["head"], row["delta_sha256"])
    check_import_delta(root, row["old_base"], row["base"], row["parent_delta_sha256"])
    log = checked_file(row["focused_log"])
    require(sha256(log) == focused["pytest_log_sha256"], "focused log binding mismatch")
    junit = ET.fromstring(checked_file(row["junit"]))
    cases = junit.findall(".//testcase")
    identities = [(case.get("classname"), case.get("name")) for case in cases]
    expected_modules = {p.removesuffix(".py").replace("/", ".") for p in TEST_PATHS}
    require(set(focused["tested_modules"]) == expected_modules
            and {c[0] for c in identities} == expected_modules, "wrong test module selection")
    require(cases and len(set(identities)) == len(identities), "empty or duplicate test identities")
    require(not junit.findall(".//failure") and not junit.findall(".//error")
            and not junit.findall(".//skipped"), "focused tests failed or skipped")
    totals = focused["junit_totals"]
    require(totals == {"tests": len(cases), "errors": 0, "failures": 0, "skipped": 0},
            "focused execution count mismatch")
    require({key: sum(int(s.get(key, "0")) for s in junit.findall(".//testsuite")) for key in totals}
            == totals, "JUnit suite counters disagree with executed identities")
    expected_command = [str(new_repo / ".venv/bin/python"), "-B", "-m", "pytest", *TEST_PATHS,
                        "-q", "--tb=short", f"--junitxml={row['junit']['path']}"]
    require(focused["command"] == expected_command, "unexpected focused command or collection filter")
    return {"pr": row["pr"], "original_head": row["old_head"], "original_base": row["old_base"],
            "candidate_head": row["head"], "candidate_tree": focused["tree"], "stack_parent": row["base"],
            "complete_pr_diff_sha256": sha256(delta(root, row["base"], row["head"])),
            "original_pr_diff_sha256": sha256(delta(root, row["old_base"], row["old_head"])),
            "cleanup_delta_sha256": row["delta_sha256"], "changed_import_modules": changed,
            "baseline": row["full_result"], "focused": row["focused_result"], "junit": row["junit"],
            "dependency_fingerprint": row["dependency_fingerprint"],
            "collected_identities_sha256": sha256(canonical(identities)), "reused_test_executions": len(cases)}


def verify_approval(root: Path, approval: Path, expected: str, *, dry_run: bool = False) -> dict:
    """Verify an external, operator-pinned local case and return a bounded receipt.

Raise ProofRefused for absent, stale, failed, mixed or unapproved evidence. A
caller must select the approval digest from reviewed evidence outside the
candidate branch. This function supplies neither producer authentication nor CI
skip authority. It observes endpoint stability; it does not isolate other chats.
"""
    require(not any(os.environ.get(k) for k in ("CI", "GITHUB_ACTIONS", "CIRCLECI")),
            "local approval cannot authorize hosted validation")
    root = root.resolve()
    require(approval.is_absolute() and not approval.resolve().is_relative_to(root),
            "approval must be outside the candidate repository")
    binding = {"path": str(approval), "sha256": expected}
    raw = checked_file(binding)
    require(len(raw) <= 2_000_000, "oversized approval manifest")
    data = json.loads(raw)
    require(data["schema"] == SCHEMA and data["scope"] == "local-only"
            and data["repository"] == "Villeneuve-Ventures/graphify", "unaccepted approval profile")
    require(data["enabled"] is True, "pilot route disabled; ordinary routing remains required")
    require(1 <= len(data["rows"]) <= 8, "invalid pilot prefix count")
    require(len({r["pr"] for r in data["rows"]}) == len(data["rows"]), "duplicate pilot prefix")
    verifier = {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).read_bytes())}
    for producer in ("full", "focused"):
        checked_file(data["producers"][producer])
    deadline, cache, results = time.monotonic() + 900, {}, []
    previous = None
    for row in data["rows"]:
        for key in ("head", "base", "old_head", "old_base"):
            require(bool(re.fullmatch(r"[0-9a-f]{40}", row[key])), "full commit identities required")
        require(row["head"] != row["base"] and row["old_head"] != row["old_base"], "empty PR prefix")
        if previous is None:
            require(row["base"] == row["old_base"], "first prefix requires the original PR base")
        else:
            require(row["old_base"] == previous["old_head"] and row["base"] == previous["head"],
                    "missing or reordered prefix proof")
        results.append(check_row(root, row, deadline, cache))
        previous = row
    # Recheck proof and endpoint state after verification without invoking any
    # archived Python, setup, pytest, graph output or runtime-state operation.
    checked_file(binding)
    checked_file(verifier)
    for producer in data["producers"].values():
        checked_file(producer)
    for row in data["rows"]:
        retained = {}
        for name in ("full_result", "focused_result", "full_log", "focused_log", "junit"):
            payload = checked_file(row[name])
            if name in {"full_result", "focused_result"}:
                repo = check_checkout(json.loads(payload))
                retained[name] = repo
                require(dependency_inventory(repo, deadline, cache) == row["dependency_fingerprint"],
                        "dependency inventory changed during verification")
        check_interpreter(row, retained["full_result"], retained["focused_result"])
    return {"schema": "graphify-import-cleanup-receipt-v1", "route": "import-cleanup",
            "scope": "local-only", "status": "printed" if dry_run else "complete",
            "claim": "baseline-plus-reviewed-import-delta", "approval": binding,
            "verifier": verifier,
            "producer_bindings": data["producers"], "prefixes": results,
            "executed_test_runs": 0, "full_gate_launches": 0,
            "reused_test_executions": sum(r["reused_test_executions"] for r in results),
            "skipped_checks": {"full-suite": "original exact-head full pass plus qualified cleanup proof",
                               "hosted-validation": "local pilot grants no hosted acceptance"},
            "review": "operator-reviewed approval; implementation review is separate",
            "source_stability": "retained checkout/evidence endpoints checked; no continuous isolation claim"}


def main(argv: list[str] | None = None) -> int:
    """Print one JSON receipt; blocked proof exits 2 and launches no fallback."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = verify_approval(ROOT, args.approval, args.approval_sha256, dry_run=args.dry_run)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema": "graphify-import-cleanup-receipt-v1", "route": "import-cleanup",
                          "scope": "local-only", "status": "blocked", "reason": str(exc),
                          "executed_test_runs": 0, "full_gate_launches": 0}, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
