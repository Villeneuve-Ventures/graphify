"""Request and seal admission use disposable roots, never live maintenance."""
import json
import os
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement


@pytest.fixture
def request_case(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("def alpha(): return 1\n")
    output = root / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_bytes(b'{"old":true}')
    for name in ("archive-parent", "stage-parent", "control-parent", "policy"):
        (tmp_path / name).mkdir()
    request = {"schema": 1, "root": str(root), "output": str(output),
               "archive": str(tmp_path / "archive-parent/archive"),
               "stage": str(tmp_path / "stage-parent/stage.json"),
               "journal": str(tmp_path / "control-parent/journal.json"),
               "include": ["**/*"], "exclude": [],
               "extra_roots": [{"label": "policy", "path": str(tmp_path / "policy")}],
               "directed": True, "extraction_profile": replacement.PROFILE,
               "artifact_profile": "core-v1"}
    return replacement.bind_request(request)


def invoke(tmp_path, request, command="bind-request"):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    args = [command, "--request", str(path)]
    if command == "build-data":
        fragments = tmp_path / "fragments.json"
        fragments.write_text("[]")
        args += ["--fragments", str(fragments)]
    return replacement.main(args)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("field", ["root", "output", "archive", "stage", "journal", "policy"])
@pytest.mark.parametrize("value", [None, [], {}, 17, True, "", "\x00"])
def test_invalid_path_is_structured_refusal(request_case, tmp_path, capsys, bound, field, value):
    request = request_case
    if not bound:
        del request["parent_identities"]
    if field == "policy":
        request["extra_roots"][0]["path"] = value
    else:
        request[field] = value
    assert invoke(tmp_path, request) == 1
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "path" in response["error"]
    assert not (tmp_path / "stage-parent/stage.json").exists()


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY"])
@pytest.mark.parametrize("command", ["bind-request", "build-data", "preview", "status"])
def test_unsafe_platform_refuses_before_request_read(tmp_path, monkeypatch, capsys, flag, command):
    def unexpected_read(*args, **kwargs):
        pytest.fail("unsupported platform must refuse before request reads")
    monkeypatch.delattr(os, flag)
    monkeypatch.setattr(replacement, "_load", unexpected_read)
    args = [command, "--request", str(tmp_path / "absent.json")]
    if command == "build-data":
        args += ["--fragments", str(tmp_path / "absent-fragments.json")]
    assert replacement.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked", "error": "safe replacement filesystem operations unavailable"}


def test_missing_descriptor_open_support_refuses_before_request_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.open})
    assert replacement.main(["bind-request", "--request", str(tmp_path / "absent.json")]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked", "error": "safe replacement filesystem operations unavailable"}


@pytest.mark.parametrize("command", ["preview", "execute", "resume"])
def test_invalid_stage_refuses_before_seal_read(request_case, tmp_path, capsys, command):
    request_case["stage"] = []
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_case))
    args = [command, "--request", str(path)]
    if command != "preview":
        args += ["--preview-sha256", "unused"]
    if command == "resume":
        args += ["--journal-sha256", "unused"]
    assert replacement.main(args) == 1
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "path" in response["error"]


def test_seal_success_and_occupied_stage_preservation(request_case, tmp_path, capsys):
    assert invoke(tmp_path, request_case, "build-data") == 0
    response = json.loads(capsys.readouterr().out)
    stage = Path(request_case["stage"])
    before = stage.read_bytes()
    assert response == {"status": "data-sealed", "seal_sha256": replacement._sha(before)}
    assert json.loads(before)["producer"] == replacement.PROFILE
    assert invoke(tmp_path, request_case, "build-data") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert stage.read_bytes() == before


def test_stage_creation_refuses_registered_archive(request_case, tmp_path, capsys):
    stage_parent = Path(request_case["stage"]).parent
    journal = tmp_path / "other-operation.json"
    journal.write_text(json.dumps({"request_digest": "other-operation"}))
    (tmp_path / replacement.GUARD).write_text(json.dumps({
        "schema": 1, "entries": {stage_parent.name: {
            "journal": str(journal), "digest": "other-operation", "role": "archive"}}}))
    retained = stage_parent / "retained.json"
    retained.write_bytes(b'{"preserved":true}')
    # The parent is valid for reading; only its attempted stage write must fail.
    assert replacement.build_data(request_case, [])["producer"] == replacement.PROFILE
    assert invoke(tmp_path, request_case, "build-data") == 1
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "archive" in response["error"]
    assert list(stage_parent.iterdir()) == [retained]
    assert retained.read_bytes() == b'{"preserved":true}'


def test_parent_replacement_after_build_never_receives_seal(request_case, tmp_path, monkeypatch, capsys):
    original = replacement.build_data
    stage_parent = Path(request_case["stage"]).parent
    retained = tmp_path / "retained-stage-parent"
    def retarget(*args, **kwargs):
        sealed = original(*args, **kwargs)
        stage_parent.rename(retained)
        stage_parent.mkdir()
        return sealed
    monkeypatch.setattr(replacement, "build_data", retarget)
    assert invoke(tmp_path, request_case, "build-data") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert not (stage_parent / "stage.json").exists()
    assert not (retained / "stage.json").exists()
    assert (Path(request_case["output"]) / "graph.json").read_bytes() == b'{"old":true}'


@pytest.mark.parametrize("boundary", ["create", "file-sync", "parent-sync"])
def test_parent_retarget_during_seal_refuses_and_retains_hold(
        request_case, tmp_path, monkeypatch, capsys, boundary):
    stage_parent = Path(request_case["stage"]).parent
    retained = tmp_path / "retained-stage-parent"
    original_open, original_fsync = os.open, os.fsync
    stage_fd = None
    changed = False
    def retarget():
        nonlocal changed
        stage_parent.rename(retained)
        stage_parent.mkdir()
        changed = True
    def open_stage(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal stage_fd
        is_stage = Path(path).name == "stage.json" and flags & os.O_CREAT
        if is_stage and boundary == "create":
            retarget()
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if is_stage:
            stage_fd = fd
        return fd
    def sync_stage(fd):
        if stage_fd is not None and not changed and (
                (boundary == "file-sync" and fd == stage_fd)
                or (boundary == "parent-sync" and fd != stage_fd)):
            retarget()
        return original_fsync(fd)
    monkeypatch.setattr(os, "open", open_stage)
    monkeypatch.setattr(os, "fsync", sync_stage)
    # Preserve the modeled platform's real descriptor-open capability.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_stage})
    assert invoke(tmp_path, request_case, "build-data") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert changed
    assert not (stage_parent / "stage.json").exists()
    hold = retained / "stage.json"
    assert hold.exists()
    before = hold.read_bytes()
    assert invoke(tmp_path, request_case, "build-data") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert hold.read_bytes() == before
    assert (Path(request_case["output"]) / "graph.json").read_bytes() == b'{"old":true}'


def test_failed_seal_sync_retains_exclusive_stage(request_case, tmp_path, monkeypatch, capsys):
    def fail_sync(fd):
        raise OSError("injected seal sync failure")
    monkeypatch.setattr(os, "fsync", fail_sync)
    assert invoke(tmp_path, request_case, "build-data") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    stage = Path(request_case["stage"])
    before = stage.read_bytes()
    assert before
    assert invoke(tmp_path, request_case, "build-data") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert stage.read_bytes() == before
