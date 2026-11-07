"""Pure evidence-contract checks; pytest never builds, signs, or runs the probe."""
from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest

pytest.importorskip("fcntl", reason="pure descriptor checks require Unix fcntl")
from tools import app_sandbox_fd_probe as probe  # noqa: E402


@pytest.mark.parametrize("failure,outcome,stdout,stderr", [
    (subprocess.TimeoutExpired(["fake-command"], 30, output=b"partial output", stderr=b"partial error"),
     "timed-out", "partial ", "partial "),
    (subprocess.TimeoutExpired(["fake-command"], 30, output="partial output", stderr="partial error"),
     "timed-out", "partial ", "partial "),
    (FileNotFoundError("fake command missing"), "launch-failed", "", ""),
])
def test_command_failure_keeps_bounded_diagnostics(monkeypatch, failure, outcome, stdout, stderr):
    commands = []

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(probe.subprocess, "run", fail)
    monkeypatch.setattr(probe, "MAX_OUTPUT", 8)
    with pytest.raises(type(failure)) as raised:
        probe._command(["fake-command"], commands, {})
    assert raised.value is failure
    assert len(commands) == 1
    assert commands[0]["argv"] == ["fake-command"]
    assert commands[0]["returncode"] is None
    assert commands[0]["outcome"] == outcome
    assert commands[0]["stdout"] == stdout
    assert commands[0]["stderr"] == stderr
    assert commands[0]["error"] == repr(failure)


@pytest.fixture
def evidence():
    return {
        "controller_pid": 100,
        "spawned": {"broker_pid": 200, "coordinator_pid": 300},
        "sender_ready": {"pid": 300, "ppid": 200, "pgid": 200,
                         "allowed_write": True, "allowed_errno": 0, "outside_errno": errno.EPERM,
                         "device": 1, "inode": 42},
        "receiver_ready": {"pid": 400, "ppid": 100, "outside_errno": 0},
        "sender_sent": {"sent": True, "errno": 0},
        "receiver_received": {"received": True, "device": 1, "inode": 42,
                              "access_mode": os.O_RDWR, "pathname_errno": errno.EACCES},
        "coordinator_reaped": {"coordinator_pid": 300, "returncode": -signal.SIGKILL},
        "broker_returncode": -signal.SIGKILL,
        "receiver_returncode": 0,
        "receiver_write": {"wrote": True, "count": 32, "errno": 0},
        "marker_length": 32,
        "marker_observed": True,
        "canary_identity": [1, 42],
        "canary_mode_at_transfer": 0,
        "verifier_read_only": True,
        "verifier_access_mode": os.O_RDONLY,
        "cleanup": [{"role": "broker", "pid": 200, "returncode": -signal.SIGKILL},
                    {"role": "receiver", "pid": 400, "returncode": 0},
                    {"role": "coordinator", "pid": 300, "returncode": -signal.SIGKILL}],
        "before_gate_sha256": "a" * 64,
        "after_sha256": "b" * 64,
        "order": ["canary_pathname_revoked", "coordinator_reaped", "broker_reaped",
                  "receiver_gate_released", "receiver_write", "observation_recorded", "canary_mode_restored"],
    }


def test_default_opt_out_has_no_process_or_filesystem_actions(monkeypatch, capsys):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected command"))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected actor"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: pytest.fail("unexpected directory"))
    assert probe.main([]) == 2
    assert '"qualified": false' in capsys.readouterr().out
    assert probe.main(["--_role", "broker", "--_spec", "{}"]) == 2


def test_counterexample_requires_actual_post_death_effect(evidence):
    assert probe.classify_case(evidence, sandboxed=True) == "fd-write-after-death"
    evidence["marker_observed"] = False
    with pytest.raises(probe.ProbeError, match="effect is unproved"):
        probe.classify_case(evidence, sandboxed=True)


def test_partial_observed_write_is_already_a_counterexample(evidence):
    evidence["receiver_write"]["count"] = 4
    evidence["receiver_write"]["sync_errno"] = errno.EIO
    assert probe.classify_case(evidence, sandboxed=True) == "fd-write-after-death"


def test_same_route_control_must_have_unsandboxed_path_access(evidence):
    with pytest.raises(probe.ProbeError, match="control evidence"):
        probe.classify_case(evidence, sandboxed=False)
    evidence["sender_ready"]["outside_errno"] = 0
    assert probe.classify_case(evidence, sandboxed=False) == "fd-write-after-death"
    with pytest.raises(probe.ProbeError, match="sandbox/control evidence"):
        probe.classify_case(evidence, sandboxed=True)


@pytest.mark.parametrize("field", ["sender_ready", "coordinator_reaped", "receiver_received", "order"])
def test_missing_evidence_never_becomes_a_containment_result(evidence, field):
    del evidence[field]
    with pytest.raises(probe.ProbeError, match="incomplete"):
        probe.classify_case(evidence, sandboxed=True)


@pytest.mark.parametrize("change", [
    ("sender_ready", "allowed_write", False),
    ("sender_ready", "ppid", 999),
    ("sender_ready", "pgid", 999),
    ("sender_ready", "inode", 999),
    ("receiver_received", "inode", 999),
    ("receiver_received", "access_mode", os.O_RDONLY),
    ("receiver_received", "pathname_errno", 0),
    ("receiver_ready", "outside_errno", errno.EPERM),
    ("coordinator_reaped", "returncode", 0),
])
def test_invalid_identity_controls_or_retirement_refuse(evidence, change):
    group, field, value = change
    evidence[group][field] = value
    with pytest.raises(probe.ProbeError):
        probe.classify_case(evidence, sandboxed=True)


@pytest.mark.parametrize("field,value", [
    ("broker_returncode", 0), ("receiver_returncode", -signal.SIGKILL),
    ("canary_mode_at_transfer", 0o600), ("verifier_read_only", False),
])
def test_no_path_or_controller_write_substitute_for_transferred_authority(evidence, field, value):
    evidence[field] = value
    with pytest.raises(probe.ProbeError):
        probe.classify_case(evidence, sandboxed=True)


@pytest.mark.parametrize("change", ["missing-broker", "missing-receiver", "error", "pid", "wait"])
def test_cleanup_uncertainty_never_yields_a_result(evidence, change):
    if change.startswith("missing-"):
        role = change.removeprefix("missing-")
        evidence["cleanup"] = [item for item in evidence["cleanup"] if item["role"] != role]
    elif change == "error":
        evidence["cleanup"][0]["error"] = "wait timed out"
    elif change == "pid":
        evidence["cleanup"][0]["pid"] = 999
    else:
        evidence["cleanup"][0]["returncode"] = 0
    with pytest.raises(probe.ProbeError, match="cleanup"):
        probe.classify_case(evidence, sandboxed=True)


@pytest.mark.parametrize("field", ["coordinator_reaped", "broker_reaped", "receiver_gate_released",
                                   "canary_pathname_revoked", "observation_recorded"])
def test_reordered_gate_or_mode_evidence_refuses(evidence, field):
    evidence["order"].remove(field)
    evidence["order"].append(field)
    with pytest.raises(probe.ProbeError, match="preceded"):
        probe.classify_case(evidence, sandboxed=True)


def test_denied_export_is_only_a_bounded_observation(evidence):
    evidence["sender_sent"] = {"sent": False, "errno": errno.EPERM}
    evidence["receiver_received"] = {"received": False, "device": None, "inode": None,
                                     "access_mode": None, "pathname_errno": errno.EACCES}
    evidence["receiver_write"] = {"wrote": False, "count": 0, "errno": 0}
    evidence["after_sha256"] = evidence["before_gate_sha256"]
    evidence["marker_observed"] = False
    assert probe.classify_case(evidence, sandboxed=True) == "export-blocked-observation"
    evidence["sender_sent"]["errno"] = errno.EBADF
    with pytest.raises(probe.ProbeError, match="valid denial"):
        probe.classify_case(evidence, sandboxed=True)


def test_transferred_descriptor_write_denial_requires_unchanged_bytes(evidence):
    evidence["receiver_write"] = {"wrote": False, "count": 0, "errno": errno.EPERM}
    evidence["after_sha256"] = evidence["before_gate_sha256"]
    evidence["marker_observed"] = False
    assert probe.classify_case(evidence, sandboxed=True) == "write-blocked-observation"
    evidence["after_sha256"] = "c" * 64
    with pytest.raises(probe.ProbeError, match="unrelated"):
        probe.classify_case(evidence, sandboxed=True)


def test_fresh_output_resolves_aliases_and_refuses_graph_or_source_overlap(tmp_path, monkeypatch):
    parent = tmp_path / "outside"
    parent.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(probe, "REPOSITORY", source)
    candidate = parent / "new"
    assert probe.validate_output_root(candidate) == parent.resolve() / "new"
    assert not candidate.exists()
    for path in (source / "child", source, parent):
        with pytest.raises(probe.ProbeError):
            probe.validate_output_root(path)
    graph = parent / "graphify-out"
    graph.mkdir()
    with pytest.raises(probe.ProbeError, match="graph output"):
        probe.validate_output_root(graph / "new")
    alias = parent / "alias"
    alias.symlink_to(graph, target_is_directory=True)
    with pytest.raises(probe.ProbeError, match="graph output"):
        probe.validate_output_root(alias / "new")


def test_entitlements_have_only_the_explicit_task_directory(tmp_path):
    actual = probe.entitlements_for(tmp_path)
    assert actual == {"com.apple.security.app-sandbox": True,
                      "com.apple.security.temporary-exception.files.absolute-path.read-write":
                          [str(tmp_path.resolve()) + "/"]}


def test_read_only_verifier_rejects_a_writable_descriptor(tmp_path):
    path = tmp_path / "canary"
    path.write_bytes(b"test")
    readonly = os.open(path, os.O_RDONLY)
    writable = os.open(path, os.O_RDWR)
    try:
        assert probe._read_verifier(readonly) == b"test"
        with pytest.raises(probe.ProbeError, match="read-only"):
            probe._read_verifier(writable)
    finally:
        os.close(readonly)
        os.close(writable)


def test_raw_evidence_remains_unchanged_by_classification(evidence):
    before = deepcopy(evidence)
    probe.classify_case(evidence, sandboxed=True)
    assert before == evidence


@pytest.mark.parametrize("failing_case", ["control", "sandbox"])
def test_classification_failure_retains_raw_case_in_receipt(tmp_path, monkeypatch, evidence, failing_case):
    root = tmp_path / "probe-output"
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.os, "getuid", lambda: 1000)
    monkeypatch.setattr(probe.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(probe.platform, "platform", lambda: "mocked macOS")
    monkeypatch.setattr(probe, "_child_env", lambda root: {})
    monkeypatch.setattr(probe, "_command", lambda *args: b"mocked OS version")
    monkeypatch.setattr(probe, "_build", lambda *args: {
        "control": {"executable": "mock-control"}, "sandbox": {"executable": "mock-sandbox"}})
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: pytest.fail("native command"))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **kw: pytest.fail("native actor"))
    observed = {}

    def raw_case(root, executable, *, sandboxed, env):
        name = "sandbox" if sandboxed else "control"
        case = deepcopy(evidence)
        if not sandboxed:
            case["sender_ready"]["outside_errno"] = 0
        if name == failing_case:
            del case["order"]
        observed[name] = deepcopy(case)
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "case.json").write_text(json.dumps(case))
        return case

    monkeypatch.setattr(probe, "_run_case", raw_case)
    with pytest.raises(probe.ProbeError, match="incomplete"):
        probe.run_probe(root)

    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["status"] == "blocked-or-harness-invalid"
    assert receipt["exit_code"] == 2
    assert receipt["qualified"] is False
    assert receipt["live_replacement_enabled"] is False
    assert receipt["cases"][failing_case] == observed[failing_case]
    assert "classification" not in receipt["cases"][failing_case]
    assert json.loads((root / failing_case / "case.json").read_text()) == observed[failing_case]
    assert set(receipt["cases"]) == set(observed)
    if failing_case == "sandbox":
        assert receipt["cases"]["control"]["classification"] == "fd-write-after-death"
