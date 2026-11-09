"""Prove local retained-evidence admission and refusal without launching gates."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time

import pytest

from tools import import_cleanup_gate as gate
from tools import validation_route as route


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def bind(path):
    return {"path": str(path), "sha256": gate.sha256(path.read_bytes())}


def save(path, value):
    path.write_text(json.dumps(value))
    return bind(path)


def install(repo, interpreter):
    env = repo / ".venv"
    site = env / "lib/python3.14/site-packages"
    site.mkdir(parents=True)
    (env / "bin").mkdir()
    (env / "bin/python").symlink_to(interpreter)
    (env / "pyvenv.cfg").write_text(
        "version_info = 3.14.10\ninclude-system-site-packages = false\n"
    )
    project = site / "graphifyy-0.1.dist-info"
    dependency = site / "example-1.dist-info"
    project.mkdir()
    dependency.mkdir()
    save(project / "direct_url.json", {"url": repo.as_uri(), "dir_info": {"editable": True}})
    (site / "_graphifyy.pth").write_text(str(repo))
    (site / "example.py").write_text("VALUE = 1\n")
    launcher = env / "bin/example"
    launcher.write_text(f"#!{repo}/.venv/bin/python\nprint('example')\n")
    for dist, paths in [(project, [project / "direct_url.json", site / "_graphifyy.pth"]),
                        (dependency, [site / "example.py", launcher])]:
        text = io.StringIO()
        writer = csv.writer(text)
        for path in paths:
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            relative = "../../../bin/example" if path == launcher else path.relative_to(site).as_posix()
            writer.writerow([relative, f"sha256={digest}", len(data)])
        writer.writerow([f"{dist.name}/RECORD", "", ""])
        (dist / "RECORD").write_text(text.getvalue())


@pytest.fixture
def case(tmp_path, monkeypatch):
    for name in ("CI", "GITHUB_ACTIONS", "CIRCLECI"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "objects"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / ".gitignore").write_text(".venv/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "tests").mkdir()
    (root / "graphify").mkdir()
    (root / "graphify/__init__.py").write_text("VALUE = 1\n")
    (root / "uv.lock").write_text("frozen fixture\n")
    for name in gate.TEST_PATHS:
        (root / name).write_text("import os\n\ndef test_contract():\n    assert True\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "original feature")
    old = git(root, "rev-parse", "HEAD")
    old_repo = tmp_path / "original"
    subprocess.run(["git", "clone", "-q", str(root), str(old_repo)], check=True)
    (root / gate.TEST_PATHS[0]).write_text("def test_contract():\n    assert True\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "reviewed import cleanup")
    head = git(root, "rev-parse", "HEAD")
    new_repo = tmp_path / "candidate"
    subprocess.run(["git", "clone", "-q", str(root), str(new_repo)], check=True)
    interpreter = tmp_path / "python3.14"
    interpreter.write_bytes(b"fixture interpreter; never execute")
    for repo in (old_repo, new_repo):
        install(repo, interpreter)
    full = {"head": old, "base": base, "tree": git(root, "rev-parse", f"{old}^{{tree}}"),
            "checkout": str(old_repo), "checkout_identity": gate.identity(old_repo),
            "venv_identity": gate.identity(old_repo / ".venv"),
            "lock_sha256": gate.sha256((old_repo / "uv.lock").read_bytes()),
            "passed": True, "exit_code": 0, "complete_full_route": True,
            "postcheck_environment_admitted": True,
            "command": ["make", f"PYTHON={old_repo}/.venv/bin/python", f"BASE={base}", "fast-feedback"]}
    env = {"python_version": [3, 14, 10], "actual_package_origin": str(old_repo / "graphify/__init__.py"),
           "editable_origin": str(old_repo), "normal_prefix": str(old_repo / ".venv"),
           "isolated_prefix": str(old_repo / ".venv")}
    log = tmp_path / "full.log"
    log.write_text("validation_environment_receipt=" + json.dumps(env)
                   + "\nvalidation_route_status=complete; selected_lane=full\n")
    full["make_log_sha256"] = gate.sha256(log.read_bytes())
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuites><testsuite tests="7" failures="0" errors="0" skipped="0">'
                     + "".join(f'<testcase classname="{p[:-3].replace("/", ".")}" name="test_contract"/>'
                               for p in gate.TEST_PATHS) + "</testsuite></testsuites>")
    focus_log = tmp_path / "focus.log"
    focus_log.write_text("7 passed\n")
    binding = {"version": "3.14.10", "prefix": str(new_repo / ".venv"),
               "package": str(new_repo / "graphify/__init__.py"),
               "direct_url": {"url": new_repo.as_uri(), "dir_info": {"editable": True}}}
    focused = {**full, "head": head, "tree": git(root, "rev-parse", "HEAD^{tree}"),
               "checkout": str(new_repo), "checkout_identity": gate.identity(new_repo),
               "venv_identity": gate.identity(new_repo / ".venv"), "full_route": False,
               "import_bindings": [binding, copy.deepcopy(binding)],
               "tested_modules": [p[:-3].replace("/", ".") for p in gate.TEST_PATHS],
               "junit_totals": {"tests": 7, "errors": 0, "failures": 0, "skipped": 0},
               "command": [str(new_repo / ".venv/bin/python"), "-B", "-m", "pytest", *gate.TEST_PATHS,
                           "-q", "--tb=short", f"--junitxml={junit}"],
               "pytest_log_sha256": gate.sha256(focus_log.read_bytes())}
    producer = tmp_path / "reviewed-producer.py"
    producer.write_text("# fixture producer, reviewed outside candidate branch\n")
    row = {"pr": 277, "old_head": old, "old_base": base, "head": head, "base": base,
           "full_result": save(tmp_path / "full.json", full),
           "focused_result": save(tmp_path / "focused.json", focused),
           "full_log": bind(log), "focused_log": bind(focus_log), "junit": bind(junit),
           "delta_sha256": gate.sha256(gate.delta(root, old, head)),
           "parent_delta_sha256": gate.sha256(b""),
           "interpreter_sha256": gate.sha256(interpreter.read_bytes()),
           "dependency_fingerprint": gate.dependency_inventory(old_repo, time.monotonic() + 10, {})}
    data = {"schema": gate.SCHEMA, "repository": "Villeneuve-Ventures/graphify", "scope": "local-only",
            "enabled": True, "producers": {"full": bind(producer), "focused": bind(producer)}, "rows": [row]}
    approval = tmp_path / "operator-approval.json"
    save(approval, data)
    return root, approval, data, row


def verify(case, **kwargs):
    root, approval, data, _ = case
    save(approval, data)
    return gate.verify_approval(root, approval, gate.sha256(approval.read_bytes()), **kwargs)


def change_result(case, name, mutate):
    row = case[3]
    path = Path(row[name]["path"])
    result = json.loads(path.read_text())
    mutate(result)
    row[name] = save(path, result)


def test_complete_reuses_bound_test_identities_without_executing_children(case, monkeypatch):
    real = gate.subprocess.check_output
    commands = []

    def read_only(argv, **kwargs):
        commands.append(argv)
        assert argv[0] == "git"
        return real(argv, **kwargs)

    monkeypatch.setattr(gate.subprocess, "check_output", read_only)
    before = git(case[0], "show-ref")
    receipt = verify(case)
    assert receipt["status"] == "complete"
    assert receipt["claim"] == "baseline-plus-reviewed-import-delta"
    assert receipt["executed_test_runs"] == receipt["full_gate_launches"] == 0
    assert receipt["reused_test_executions"] == 7
    assert receipt["prefixes"][0]["changed_import_modules"] == [gate.TEST_PATHS[0]]
    assert receipt["prefixes"][0]["complete_pr_diff_sha256"]
    assert commands and before == git(case[0], "show-ref")


def test_dry_run_never_claims_completed_validation(case):
    assert verify(case, dry_run=True)["status"] == "printed"


@pytest.mark.parametrize("key,value", [("enabled", False), ("scope", "hosted"), ("schema", "unaccepted")])
def test_profile_or_disabled_route_refuses(case, key, value):
    case[2][key] = value
    with pytest.raises(gate.ProofRefused):
        verify(case)


@pytest.mark.parametrize("host", ["CI", "GITHUB_ACTIONS", "CIRCLECI"])
def test_local_proof_cannot_authorize_ci(case, monkeypatch, host):
    monkeypatch.setenv(host, "true")
    with pytest.raises(gate.ProofRefused, match="hosted"):
        verify(case)


def test_missing_or_changed_approval_is_not_trusted(case):
    with pytest.raises(gate.ProofRefused, match="digest"):
        gate.verify_approval(case[0], case[1], "0" * 64)
    with pytest.raises(FileNotFoundError):
        gate.verify_approval(case[0], case[1].with_name("absent.json"), "0" * 64)


def test_branch_supplied_approval_cannot_authorize_itself(case):
    target = case[0] / "approval.json"
    save(target, case[2])
    with pytest.raises(gate.ProofRefused, match="outside"):
        gate.verify_approval(case[0], target, gate.sha256(target.read_bytes()))


@pytest.mark.parametrize("field,value", [("passed", False), ("exit_code", 1), ("complete_full_route", False),
                                        ("postcheck_environment_admitted", False), ("head", "0" * 40),
                                        ("base", "0" * 40), ("command", ["true"])])
def test_incomplete_failed_or_wrong_baseline_refuses(case, field, value):
    change_result(case, "full_result", lambda result: result.update({field: value}))
    with pytest.raises(gate.ProofRefused):
        verify(case)


def test_changed_raw_log_is_not_accepted(case):
    Path(case[3]["full_log"]["path"]).write_text("invented success\n")
    with pytest.raises(gate.ProofRefused, match="digest"):
        verify(case)


@pytest.mark.parametrize("surface", ["unstaged", "staged", "untracked", "committed"])
def test_retained_candidate_complete_inventory_is_required(case, surface):
    repo = Path(json.loads(Path(case[3]["focused_result"]["path"]).read_text())["checkout"])
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    path = repo / ("unexpected.py" if surface == "untracked" else gate.TEST_PATHS[0])
    path.write_text("raise RuntimeError('changed')\n")
    if surface in {"staged", "committed"}:
        git(repo, "add", ".")
    if surface == "committed":
        git(repo, "commit", "-qm", "unqualified work")
    with pytest.raises(gate.ProofRefused, match="dirty|head/tree"):
        verify(case)


@pytest.mark.parametrize("content", ["def test_contract():\n    assert False\n", "import subprocess\n\ndef test_contract():\n    assert True\n",
                                     "from os import *\n\ndef test_contract():\n    assert True\n"])
def test_approved_delta_hash_does_not_admit_changed_bodies_or_new_imports(case, content):
    root, _, _, row = case
    (root / gate.TEST_PATHS[0]).write_text(content)
    git(root, "add", ".")
    git(root, "commit", "-qm", "unsupported change")
    head = git(root, "rev-parse", "HEAD")
    digest = gate.sha256(gate.delta(root, row["old_head"], head))
    with pytest.raises(gate.ProofRefused):
        gate.check_import_delta(root, row["old_head"], head, digest)


@pytest.mark.parametrize("kind", ["product", "executable", "symlink"])
def test_product_or_mode_changes_refuse_even_with_a_new_pin(case, kind):
    root, _, _, row = case
    path = root / gate.TEST_PATHS[0]
    if kind == "product":
        (root / "graphify/__init__.py").write_text("VALUE = 2\n")
    elif kind == "executable":
        path.chmod(0o755)
    else:
        path.unlink()
        path.symlink_to("../graphify/__init__.py")
    git(root, "add", ".")
    git(root, "commit", "-qm", "unsupported mode or source")
    head = git(root, "rev-parse", "HEAD")
    with pytest.raises(gate.ProofRefused):
        gate.check_import_delta(root, row["old_head"], head,
                                gate.sha256(gate.delta(root, row["old_head"], head)))


def test_missing_lower_prefix_is_not_qualified_by_top_proof(case):
    case[3]["base"] = case[3]["old_head"]
    with pytest.raises(gate.ProofRefused, match="first prefix"):
        verify(case)


@pytest.mark.parametrize("mutation", ["payload", "extra-pth", "interpreter", "python-pin"])
def test_changed_environment_invalidates_evidence(case, mutation):
    result = json.loads(Path(case[3]["focused_result"]["path"]).read_text())
    repo = Path(result["checkout"])
    if mutation == "payload":
        (repo / ".venv/lib/python3.14/site-packages/example.py").write_text("VALUE = 2\n")
    elif mutation == "extra-pth":
        (repo / ".venv/lib/python3.14/site-packages/foreign.pth").write_text("/foreign/source\n")
    elif mutation == "interpreter":
        (repo / ".venv/bin/python").resolve().write_bytes(b"different interpreter")
    else:
        cfg = repo / ".venv/pyvenv.cfg"
        cfg.write_text(cfg.read_text().replace("3.14.10", "3.14.9"))
    with pytest.raises(gate.ProofRefused):
        verify(case)


def test_normal_and_isolated_bindings_are_both_required(case):
    change_result(case, "focused_result", lambda result: result["import_bindings"][1].update(package="/foreign/graphify.py"))
    with pytest.raises(gate.ProofRefused, match="binding"):
        verify(case)


@pytest.mark.parametrize("mutation", ["empty", "failure", "error", "skipped", "duplicate", "wrong-module"])
def test_collection_and_execution_proof_must_be_complete(case, mutation):
    path = Path(case[3]["junit"]["path"])
    text = path.read_text()
    if mutation == "empty":
        text = "<testsuites/>"
    elif mutation in {"failure", "error", "skipped"}:
        text = text.replace('/>', f'><{mutation}/></testcase>', 1)
    elif mutation == "duplicate":
        text = text.replace("tests.test_replacement_record_and_lock_admission", "tests.test_replacement_boundary_repairs")
    else:
        text = text.replace("tests.test_replacement_review20", "tests.foreign")
    path.write_text(text)
    case[3]["junit"] = bind(path)
    with pytest.raises(gate.ProofRefused):
        verify(case)


def test_lastfailed_selection_cannot_replace_reviewed_command(case):
    change_result(case, "focused_result", lambda result: result["command"].append("--lf"))
    with pytest.raises(gate.ProofRefused, match="command"):
        verify(case)


def test_drift_before_final_observation_blocks_completion(case, monkeypatch):
    real = gate.check_row

    def drift(*args, **kwargs):
        result = real(*args, **kwargs)
        Path(case[3]["focused_log"]["path"]).write_text("changed while checking\n")
        return result

    monkeypatch.setattr(gate, "check_row", drift)
    with pytest.raises(gate.ProofRefused, match="digest"):
        verify(case)


@pytest.mark.parametrize("mutation", ["replacement", "retarget"])
def test_interpreter_drift_before_final_observation_blocks_completion(case, monkeypatch, mutation):
    real = gate.check_row
    result = json.loads(Path(case[3]["focused_result"]["path"]).read_text())
    python = Path(result["checkout"]) / ".venv/bin/python"

    def drift(*args, **kwargs):
        checked = real(*args, **kwargs)
        if mutation == "replacement":
            python.resolve().write_bytes(b"changed interpreter after initial verification")
        else:
            other = case[1].with_name("different-interpreter")
            other.write_bytes(python.read_bytes())
            python.unlink()
            python.symlink_to(other)
        return checked

    monkeypatch.setattr(gate, "check_row", drift)
    with pytest.raises(gate.ProofRefused, match="interpreter"):
        verify(case)


def test_git_timeout_emits_blocked_receipt_without_fallback(case, monkeypatch, capsys):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 20)

    monkeypatch.setattr(gate, "git", timeout)
    monkeypatch.setattr(gate, "ROOT", case[0])
    assert gate.main(["--approval", str(case[1]), "--approval-sha256", gate.sha256(case[1].read_bytes())]) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "blocked"
    assert receipt["full_gate_launches"] == 0


def test_ordinary_full_dispatch_is_unchanged():
    classification = route.Classification(route.FULL, (), ())
    commands = route.build_plan(classification, "HEAD", False, None, 1)
    assert len(commands) == 1 and commands[0].argv[-1] == "review-ready"
