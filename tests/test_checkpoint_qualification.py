"""Harness unit checks only: these do not establish native A14 qualification."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import checkpoint_qualification as harness


def spec(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text("# reviewed runner fixture\n")
    return {"schema": 1, "disposable": True, "python": "/protected/bin/python3.14",
            "harness": str(runner), "harness_sha256": harness.sha(runner),
            "workspace": "/private/var/db/graphify-a14-unit-fixture", "reader_uid": 501,
            "reader_gid": 20, "wheel": "/protected/candidate.whl", "wheel_sha256": "a" * 64,
            "source_revision": "b" * 40}


def test_default_opt_out_never_dispatches_or_provisions(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unexpected dispatch"))
    assert harness.opt_in_spec({}) is None


@pytest.mark.parametrize("value", ["", "relative.json", "/missing/a14-spec.json"])
def test_explicit_opt_in_missing_prerequisite_fails_not_skips(value):
    with pytest.raises((harness.QualificationError, OSError)):
        harness.opt_in_spec({"GRAPHIFY_A14_SPEC": value})


@pytest.mark.parametrize("mutation", ["disposable", "uid", "gid", "workspace", "sha", "revision", "extra"])
def test_invalid_opt_in_spec_fails_without_mutation(tmp_path, mutation):
    value = spec(tmp_path)
    if mutation == "disposable": value["disposable"] = False
    elif mutation == "uid": value["reader_uid"] = 0
    elif mutation == "gid": value["reader_gid"] = True
    elif mutation == "workspace": value["workspace"] = "/private/var/db/../other"
    elif mutation == "sha": value["wheel_sha256"] = "Z" * 64
    elif mutation == "revision": value["source_revision"] = "label"
    else: value["extra"] = "unsupported"
    before = sorted(tmp_path.iterdir())
    with pytest.raises(harness.QualificationError):
        harness.validate_spec(value)
    assert sorted(tmp_path.iterdir()) == before


def test_spec_reader_rejects_duplicates_links_and_oversized(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text('{"schema":1,"schema":1}')
    for kind in ("duplicate", "hardlink", "oversized", "symlink"):
        target = path
        if kind == "hardlink":
            target = tmp_path / "hard"
            os.link(path, target)
        elif kind == "oversized":
            target = tmp_path / "big"
            target.write_bytes(b" " * (16 * 1024 + 1))
        elif kind == "symlink":
            target = tmp_path / "link"
            target.symlink_to(path)
        with pytest.raises((harness.QualificationError, OSError)):
            harness.opt_in_spec({"GRAPHIFY_A14_SPEC": str(target)})


def test_dispatch_binds_reviewed_harness_and_all_inputs(tmp_path):
    value = spec(tmp_path)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(value))
    assert harness.opt_in_spec({"GRAPHIFY_A14_SPEC": str(path)}) == value
    command = harness.command_for_spec(value)
    assert command[:5] == [value["python"], "-I", "-B", value["harness"], "--disposable"]
    assert command[-4:] == ["--expected-wheel-sha256", value["wheel_sha256"],
                            "--expected-source-revision", value["source_revision"]]
    Path(value["harness"]).write_text("changed")
    with pytest.raises(harness.QualificationError, match="SHA256"):
        harness.command_for_spec(value)


def complete_report(value):
    return {"schema": "graphify.a14.v1", "status": "passed",
            "checks": sorted(harness.REQUIRED_CHECKS), "harness_sha256": value["harness_sha256"],
            "inputs": {key: value[key] for key in ("workspace", "reader_uid", "reader_gid",
                       "wheel", "wheel_sha256", "source_revision")},
            "network_policy": harness.PROFILE, "network_policy_scope": harness.NETWORK_SCOPE,
            "commands": [{"argv": [harness.SANDBOX, "-p", harness.PROFILE, "fixture"]},
                         {"name": "reader_privilege_denied", "purpose": "environment_privilege_probe",
                          "argv": harness.PRIVILEGE_PROBE.copy(), "returncode": 1, "timed_out": False,
                          "role": {"uid": value["reader_uid"], "gid": value["reader_gid"],
                                   "groups": [value["reader_gid"]]},
                          "stdout": "", "stderr": "sudo: a password is required"}],
            "preservation": [{"before": {"sha": "same"}, "after": {"sha": "same"}}]}


def test_complete_report_unit_control_is_accepted(tmp_path):
    value = spec(tmp_path)
    report = complete_report(value)
    assert harness.validate_report(report, value) is report


@pytest.mark.parametrize("missing", ["matrix", "sha", "binding", "network", "preservation", "failed", "escaped"])
def test_report_rejects_incomplete_or_unbound_result(tmp_path, missing):
    value = spec(tmp_path)
    report = complete_report(value)
    if missing == "matrix": report["checks"].pop()
    elif missing == "sha": report["harness_sha256"] = "0" * 64
    elif missing == "binding": report["inputs"]["reader_uid"] += 1
    elif missing == "network": report["network_policy"] = "allow network"
    elif missing == "preservation": report["preservation"][0]["after"] = {"sha": "changed"}
    elif missing == "escaped": report["commands"][0]["argv"] = ["unsandboxed"]
    else: report["status"] = "failed"
    with pytest.raises(harness.QualificationError):
        harness.validate_report(report, value)


def trial_args(tmp_path, **changes):
    values = {"disposable": True, "workspace": str(tmp_path / "never-created"),
              "reader_uid": 501, "reader_gid": 20, "wheel": "/missing/candidate.whl",
              "expected_wheel_sha256": "a" * 64, "expected_source_revision": "b" * 40}
    return argparse.Namespace(**(values | changes))


def test_preflight_refuses_without_explicit_disposable_before_mutation(tmp_path, monkeypatch):
    trial = harness.Trial(trial_args(tmp_path, disposable=False))
    monkeypatch.setattr(os, "mkdir", lambda *a, **k: pytest.fail("unexpected provision"))
    with pytest.raises(harness.QualificationError, match="--disposable"):
        trial.preflight()
    assert not trial.provisioned


def test_failure_report_preserves_original_exception_and_never_runs_matrix(tmp_path, monkeypatch, capsys):
    calls = []
    def failed(self):
        calls.append("preflight")
        raise harness.QualificationError("prerequisite sentinel")
    monkeypatch.setattr(harness.Trial, "preflight", failed)
    monkeypatch.setattr(harness.Trial, "provision", lambda _: pytest.fail("provision ran"))
    monkeypatch.setattr(harness.Trial, "matrix", lambda _: pytest.fail("matrix ran"))
    result = harness.main(["--disposable", "--workspace", str(tmp_path / "absent"),
                           "--reader-uid", "501", "--reader-gid", "20", "--wheel", "/missing",
                           "--expected-wheel-sha256", "a" * 64,
                           "--expected-source-revision", "b" * 40])
    report = json.loads(capsys.readouterr().out)
    assert result == 1 and calls == ["preflight"]
    assert report["error"]["message"] == "prerequisite sentinel"
    assert report["evidence"]["status"] == "failed"
    assert not (tmp_path / "absent").exists()


def test_reader_exception_is_not_masked_by_preservation_failure(tmp_path, monkeypatch):
    trial = harness.Trial(trial_args(tmp_path))
    states = iter(({"state": "before"}, {"state": "after"}))
    monkeypatch.setattr(trial, "frozen", lambda: next(states))
    def operation():
        raise ValueError("original operation failure")
    with pytest.raises(ValueError, match="original operation"):
        trial.reader_operation("unit sentinel", operation)
    assert trial.report["preservation"][0]["before"] != trial.report["preservation"][0]["after"]


def test_reader_mutation_is_a_failure(tmp_path, monkeypatch):
    trial = harness.Trial(trial_args(tmp_path))
    states = iter(({"state": "before"}, {"state": "after"}))
    monkeypatch.setattr(trial, "frozen", lambda: next(states))
    with pytest.raises(harness.QualificationError, match="reader changed"):
        trial.reader_operation("unit sentinel", lambda: None)


def test_native_prerequisite_failure_exits_nonzero_without_provision(tmp_path):
    result = subprocess.run([sys.executable, "-I", "-B", str(Path(harness.__file__)),
                             "--workspace", str(tmp_path / "absent"), "--reader-uid", "501",
                             "--reader-gid", "20", "--wheel", "/missing",
                             "--expected-wheel-sha256", "a" * 64,
                             "--expected-source-revision", "b" * 40],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["message"] == "--disposable is required"
    assert not (tmp_path / "absent").exists()


def test_spec_fifo_refuses_promptly(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(harness.QualificationError, match="regular"):
        harness.opt_in_spec({"GRAPHIFY_A14_SPEC": str(path)})


def test_spec_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    value = spec(tmp_path)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(value))
    real_read = os.read
    changed = False
    def replace(fd, size):
        nonlocal changed
        if not changed:
            changed = True
            path.rename(tmp_path / "original-spec.json")
            os.mkfifo(path)
        return real_read(fd, size)
    monkeypatch.setattr(os, "read", replace)
    with pytest.raises(harness.QualificationError, match="changed"):
        harness.opt_in_spec({"GRAPHIFY_A14_SPEC": str(path)})


def origin_probe(site):
    names = ("graphify", "graphify.__main__", "graphify.checkpoint_restore",
             "graphify.recovery_authority", "graphify.transaction")
    payloads = {}
    modules = {}
    for name in names:
        relative = "graphify/__init__.py" if name == "graphify" else name.replace(".", "/") + ".py"
        payloads[relative] = "a" * 64
        modules[name] = {"origin": str(site / relative), "sha256": "a" * 64}
    return {"loaded_modules": modules}, payloads


def test_origin_binding_unit_control_and_second_install_refusal(tmp_path):
    probe, payloads = origin_probe(tmp_path)
    harness.validate_origins(probe, tmp_path, payloads)
    probe["loaded_modules"]["graphify.recovery_authority"]["origin"] = "/other/site/graphify/recovery_authority.py"
    with pytest.raises(harness.QualificationError, match="does not match"):
        harness.validate_origins(probe, tmp_path, payloads)


@pytest.mark.parametrize("mutation", ["incomplete", "digest", "extra"])
def test_origin_binding_rejects_missing_changed_or_extra_modules(tmp_path, mutation):
    probe, payloads = origin_probe(tmp_path)
    if mutation == "incomplete": probe["loaded_modules"].pop("graphify.transaction")
    elif mutation == "digest": probe["loaded_modules"]["graphify.transaction"]["sha256"] = "c" * 64
    else: probe["loaded_modules"]["graphify.injected"] = {"origin": str(tmp_path / "graphify/injected.py"), "sha256": "a" * 64}
    with pytest.raises(harness.QualificationError):
        harness.validate_origins(probe, tmp_path, payloads)


def test_reader_evidence_failure_keeps_original_exception(tmp_path, monkeypatch):
    trial = harness.Trial(trial_args(tmp_path))
    calls = 0
    def frozen():
        nonlocal calls
        calls += 1
        if calls == 1: return {"before": "retained"}
        raise OSError("evidence read failure")
    monkeypatch.setattr(trial, "frozen", frozen)
    def operation():
        raise ValueError("primary failure")
    with pytest.raises(ValueError, match="primary failure"):
        trial.reader_operation("unit sentinel", operation)
    assert trial.report["preservation"][0]["snapshot_error"] == "evidence read failure"


@pytest.mark.parametrize("groups", [[20, 80], [80, 20, 12]])
def test_reader_policy_rejects_actual_admin_membership(groups):
    with pytest.raises(harness.QualificationError, match="admin group"):
        harness.validate_reader_policy(501, 20, groups)


def test_reader_policy_accepts_standard_account_and_rejects_missing_membership():
    harness.validate_reader_policy(61047, 20, [12, 20])
    with pytest.raises(harness.QualificationError, match="member"):
        harness.validate_reader_policy(61047, 20, [12])
    with pytest.raises(harness.QualificationError, match="member"):
        harness.validate_reader_policy(0, 20, [20])


@pytest.mark.parametrize("reason", ["sudo: a password is required\n",
                                     "sudo: reader is not allowed to execute this command\n",
                                     "sudo: reader is not in the sudoers file\n"])
def test_reader_privilege_probe_requires_real_sudo_denial(tmp_path, monkeypatch, reason):
    trial = harness.Trial(trial_args(tmp_path))
    calls = []
    def run(name, command, **kwargs):
        calls.append((name, command, kwargs))
        return "", reason
    monkeypatch.setattr(trial, "run", run)
    trial.reader_privilege_probe()
    assert calls == [("reader_privilege_denied", ["/usr/bin/sudo", "-n", "/usr/bin/true"],
                      {"reader": True, "expected": 1, "environment_privilege_probe": True})]
    assert "reader_privilege_denied" in trial.checks
    assert "reader_privilege_denied" in harness.REQUIRED_CHECKS


@pytest.mark.parametrize("stdout,stderr", [("", "sudo: Operation not permitted\n"),
                                           ("", ""), ("unexpected", "sudo: a password is required")])
def test_reader_privilege_probe_rejects_unrelated_failure(tmp_path, monkeypatch, stdout, stderr):
    trial = harness.Trial(trial_args(tmp_path))
    monkeypatch.setattr(trial, "run", lambda *a, **k: (stdout, stderr))
    with pytest.raises(harness.QualificationError, match="policy denial"):
        trial.reader_privilege_probe()
    assert "reader_privilege_denied" not in trial.checks


def test_reader_privilege_probe_does_not_accept_passwordless_success(tmp_path, monkeypatch):
    trial = harness.Trial(trial_args(tmp_path))
    def run(*args, **kwargs):
        assert kwargs["expected"] == 1
        raise harness.QualificationError("reader_privilege_denied: unexpected exit 0")
    monkeypatch.setattr(trial, "run", run)
    with pytest.raises(harness.QualificationError, match="unexpected exit 0"):
        trial.reader_privilege_probe()
    assert "reader_privilege_denied" not in trial.checks


@pytest.mark.parametrize("reader", [False, True])
def test_child_identity_accepts_observed_darwin_access_and_kernel_groups(reader):
    uid, gid = (61047, 20) if reader else (0, 0)
    access = [20, 12, 61, 701, 100] if reader else [0, 1, 2, 3, 80, 20]
    identity = {"uid": uid, "euid": uid, "gid": gid, "egid": gid,
                "credential_groups": [gid], "access_groups": access}
    harness.validate_child_identity(identity, uid, gid, access if reader else None)


@pytest.mark.parametrize("field,value", [("uid", 0), ("euid", 0), ("gid", 0), ("egid", 0),
                                         ("credential_groups", [20, 80]),
                                         ("credential_groups", [0]),
                                         ("access_groups", [20, 80]),
                                         ("access_groups", [20])])
def test_reader_identity_rejects_changed_ids_or_groups(field, value):
    identity = {"uid": 61047, "euid": 61047, "gid": 20, "egid": 20,
                "credential_groups": [20], "access_groups": [20, 12, 61, 701, 100]}
    identity[field] = value
    with pytest.raises(harness.QualificationError):
        harness.validate_child_identity(identity, 61047, 20, [20, 12, 61, 701, 100])


def test_administrator_identity_rejects_extra_kernel_group():
    identity = {"uid": 0, "euid": 0, "gid": 0, "egid": 0,
                "credential_groups": [0, 80], "access_groups": [0, 1, 2, 3, 80, 20]}
    with pytest.raises(harness.QualificationError, match="kernel credential"):
        harness.validate_child_identity(identity, 0, 0)


@pytest.mark.parametrize("mutation", ["argv", "name", "role", "exit", "reason", "duplicate", "missing"])
def test_report_rejects_broadened_unsandboxed_privilege_probe(tmp_path, mutation):
    value = spec(tmp_path)
    report = complete_report(value)
    probe = report["commands"][1]
    if mutation == "argv": probe["argv"] = ["/usr/bin/id"]
    elif mutation == "name": probe["name"] = "arbitrary command"
    elif mutation == "role": probe["role"]["uid"] = 0
    elif mutation == "exit": probe["returncode"] = 0
    elif mutation == "reason": probe["stderr"] = "sudo: Operation not permitted"
    elif mutation == "duplicate": report["commands"].append(probe.copy())
    else: report["commands"].pop()
    with pytest.raises(harness.QualificationError):
        harness.validate_report(report, value)


@pytest.mark.parametrize("mutation", ["argv", "name", "role", "expected", "provisioned", "duplicate"])
def test_unsandboxed_probe_dispatch_refuses_any_broader_command(tmp_path, monkeypatch, mutation):
    trial = harness.Trial(trial_args(tmp_path))
    name, command, reader, expected = "reader_privilege_denied", harness.PRIVILEGE_PROBE, True, 1
    if mutation == "argv": command = ["/usr/bin/id"]
    elif mutation == "name": name = "other"
    elif mutation == "role": reader = False
    elif mutation == "expected": expected = 0
    elif mutation == "provisioned": trial.provisioned = True
    else: trial.report["commands"].append({"purpose": "environment_privilege_probe"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unexpected dispatch"))
    with pytest.raises(harness.QualificationError, match="only the fixed"):
        trial.run(name, command, reader=reader, expected=expected, environment_privilege_probe=True)



def test_volume_qualification_queries_resolved_device_and_checks_record(tmp_path, monkeypatch):
    import plistlib
    trial = harness.Trial(trial_args(tmp_path))
    volume = {"DeviceIdentifier": "disk2s5", "DeviceNode": "/dev/disk2s5",
              "FilesystemType": "apfs", "GlobalPermissionsEnabled": True,
              "MountPoint": "/System/Volumes/Data"}
    calls = []
    def run(name, command, **kwargs):
        calls.append((name, command))
        return ("disk2s5\n", "") if len(calls) == 1 else (plistlib.dumps(volume).decode(), "")
    monkeypatch.setattr(trial, "run", run)
    assert trial.qualify_volume() == {"device": "disk2s5", "record": volume}
    assert calls == [("APFS containing device", ["/usr/bin/stat", "-f", "%Sd", str(tmp_path)]),
                     ("APFS volume", ["/usr/sbin/diskutil", "info", "-plist", "/dev/disk2s5"])]


@pytest.mark.parametrize("device", ["", "disk2s5\ndisk3s1", "../disk2s5", "/dev/disk2s5", "disk2; id", "disk2s"])
def test_volume_qualification_refuses_non_disk_tokens_before_diskutil(tmp_path, monkeypatch, device):
    trial = harness.Trial(trial_args(tmp_path))
    calls = []
    def run(name, command, **kwargs):
        calls.append(command)
        return device, ""
    monkeypatch.setattr(trial, "run", run)
    with pytest.raises(harness.QualificationError, match="containing disk"):
        trial.qualify_volume()
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("DeviceIdentifier", "disk3s1"),
                                         ("DeviceNode", "/dev/disk3s1"),
                                         ("FilesystemType", "hfs"),
                                         ("GlobalPermissionsEnabled", False)])
def test_volume_qualification_requires_matching_apfs_ownership_record(tmp_path, monkeypatch, field, value):
    import plistlib
    trial = harness.Trial(trial_args(tmp_path))
    volume = {"DeviceIdentifier": "disk2s5", "DeviceNode": "/dev/disk2s5",
              "FilesystemType": "apfs", "GlobalPermissionsEnabled": True}
    volume[field] = value
    def run(name, command, **kwargs):
        return ("disk2s5\n", "") if "stat" in command[0] else (plistlib.dumps(volume).decode(), "")
    monkeypatch.setattr(trial, "run", run)
    with pytest.raises(harness.QualificationError, match="must match APFS"):
        trial.qualify_volume()



def test_reader_acl_descriptor_uses_validated_account_short_name():
    assert harness.reader_acl_entry("graphify_a14_reader") == "user:graphify_a14_reader allow read,write"
    assert harness.reader_acl_entry("admin") == "user:admin allow read,write"


@pytest.mark.parametrize("name", ["501", "", "user:admin", "reader allow write", "reader\nadmin",
                                  "reader\x00", "reader:" , "a" * 65, ".reader"])
def test_reader_acl_descriptor_rejects_numeric_or_grammar_injection(name):
    with pytest.raises(harness.QualificationError, match="short name"):
        harness.reader_acl_entry(name)


def test_write_open_control_succeeds_without_writing_or_replacing_file(tmp_path):
    path = tmp_path / "disposable-config-unit-fixture"
    path.write_bytes(b"preserve exact config bytes")
    path.chmod(0o600)
    def frozen():
        info = path.stat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns, path.read_bytes())
    before = frozen()
    result = subprocess.run([sys.executable, "-I", "-B", "-c", harness.WRITE_OPEN_CONTROL, str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    opened = json.loads(result.stdout)
    assert opened["status"] == "write_open_permitted"
    assert (opened["device"], opened["inode"]) == before[:2]
    assert opened["uid"] == opened["euid"] == os.getuid()
    assert before == frozen()
