"""Commit equivalence is evidence about pinned inputs, never an approval."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from graphify.protected_change_verifier import canonical_json
from graphify import protected_change_equivalence as equivalence


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    return subprocess.run(["git", "-C", str(repo), *args], input=data, env=env,
                          check=True, capture_output=True).stdout


def _layers(repo: Path, ref: str) -> dict:
    result = {}
    for row in _git(repo, "ls-tree", "-rz", ref).split(b"\0"):
        if not row:
            continue
        prefix, path = row.split(b"\t", 1)
        mode, _kind, oid = prefix.decode().split()
        raw = _git(repo, "cat-file", "blob", oid)
        result[path.decode()] = {"mode": mode, "oid": oid, "bytes": len(raw),
                                "sha256": _sha(raw),
                                "type": "symlink" if mode == "120000" else "blob"}
    return result


@pytest.fixture
def inputs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--object-format=sha1")
    (repo / "old.txt").write_bytes(b"old")
    (repo / "remove.txt").write_bytes(b"remove")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD").decode().strip()
    base = _layers(repo, "HEAD")
    (repo / "old.txt").write_bytes(b"updated")
    (repo / "remove.txt").unlink()
    (repo / "nested").mkdir()
    (repo / "nested" / "new.txt").write_bytes(b"new")
    (repo / "link").symlink_to("old.txt")
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "old.txt")
    staged_tree = _git(repo, "write-tree").decode().strip()
    staged = _layers(repo, staged_tree)
    status = base64.b64encode(_git(repo, "status", "--porcelain=v2", "-z")).decode()
    paths = []
    for name in sorted(base.keys() | staged.keys()):
        item = staged.get(name)
        raw = {k: v for k, v in item.items() if k != "oid"} if item else None
        paths.append({"path": name, "base": base.get(name), "head": base.get(name),
                      "index": item, "worktree": raw})
    # Ignored inventory participates in equivalence but is not staged.
    paths.append({"path": "z-ignored", "base": None, "head": None, "index": None,
                  "worktree": {"bytes": 5, "mode": "100644", "type": "blob",
                               "sha256": _sha(b"local")}})
    before = {"schema": "graphify.protected-change-review.candidate.v2",
              "base_oid": parent, "head_oid": parent, "paths": paths,
              "policy": {"version": "graphify.protected-change-review.policy.v3",
                         "sha256": "a" * 64},
              "acceptance_packet": {"schema_version": "acceptance.v1",
                                    "schema_sha256": "b" * 64, "version": "1",
                                    "sha256": "c" * 64},
              "status_porcelain_v2_z_base64": status,
              "tracked_binary_diff_sha256": "d" * 64}
    _git(repo, "commit", "-qm", "reviewed change")
    commit = _git(repo, "cat-file", "commit", "HEAD")
    after = copy.deepcopy(before)
    after["head_oid"] = _git(repo, "rev-parse", "HEAD").decode().strip()
    after["status_porcelain_v2_z_base64"] = ""
    after["tracked_binary_diff_sha256"] = "e" * 64
    for row in after["paths"]:
        row["head"] = row["index"]
    context = {"schema": "graphify.protected-change-review.validation-context.v1",
               "candidate_sha256": _sha(canonical_json(before)),
               "inputs_sha256": "1" * 64, "environment_sha256": "2" * 64,
               "runtime_sha256": "3" * 64,
               "checks": [{"name": "full suite", "command_sha256": "4" * 64,
                           "receipt_sha256": "5" * 64, "result": "passed",
                           "head_sensitive": False, "valid_until": 4_102_444_800}]}
    current = copy.deepcopy(context)
    current["candidate_sha256"] = _sha(canonical_json(after))
    return {"approved": before, "committed": after, "commit_object": commit,
            "validation_before": context, "validation_after": current}


def _encode(inputs):
    raw = {k: v if isinstance(v, bytes) else canonical_json(v) for k, v in inputs.items()}
    return raw, {k: _sha(v) for k, v in raw.items()}


def _verify(inputs):
    raw, pins = _encode(inputs)
    return json.loads(equivalence.verify_commit_equivalence(**raw, expected_digests=pins))


def test_real_git_staged_commit_equivalence(inputs):
    result = _verify(inputs)
    assert result["result"] == "equivalent"
    assert result["approval_granted"] is False
    assert result["inputs"]["approved"] == _sha(canonical_json(inputs["approved"]))


@pytest.mark.parametrize("field", ["base_oid", "policy", "acceptance_packet"])
def test_contract_drift(inputs, field):
    value = inputs["committed"][field]
    if isinstance(value, dict):
        value["sha256"] = "f" * 64
    else:
        inputs["committed"][field] = "f" * 40
    with pytest.raises(ValueError):
        _verify(inputs)


@pytest.mark.parametrize("field,value", [("sha256", "f" * 64), ("mode", "100644"),
                                        ("type", "symlink"), ("bytes", 999)])
def test_raw_content_drift(inputs, field, value):
    row = next(r for r in inputs["committed"]["paths"] if r["path"] == "old.txt")
    row["worktree"][field] = value
    with pytest.raises(ValueError):
        _verify(inputs)


@pytest.mark.parametrize("case", ["ignored", "extra-stage", "partial-stage", "duplicate",
                                  "path", "dirty", "unknown", "merge", "amend", "tree"])
def test_invalid_transition(inputs, case):
    before, after = inputs["approved"], inputs["committed"]
    if case == "ignored":
        after["paths"][-1]["worktree"]["sha256"] = "f" * 64
    elif case == "extra-stage":
        after["paths"][-1]["index"] = copy.deepcopy(after["paths"][0]["index"])
    elif case == "partial-stage":
        before["paths"][0]["index"]["sha256"] = "f" * 64
    elif case == "duplicate":
        after["paths"].append(copy.deepcopy(after["paths"][-1]))
    elif case == "path":
        after["paths"][-1]["path"] = "renamed"
    elif case == "dirty":
        after["status_porcelain_v2_z_base64"] = base64.b64encode(b"? x\0").decode()
    elif case == "unknown":
        after["unknown"] = True
    else:
        commit = inputs["commit_object"]
        if case == "merge":
            commit = commit.replace(b"\nauthor ", b"\nparent " + b"f" * 40 + b"\nauthor ")
        elif case == "amend":
            commit = commit.replace(before["head_oid"].encode(), b"f" * 40)
        else:
            commit = b"tree " + b"f" * 40 + commit[45:]
        inputs["commit_object"] = commit
        after["head_oid"] = hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
    # Rebind contexts so the transition itself is exercised.
    for key, name in [("validation_before", "approved"), ("validation_after", "committed")]:
        inputs[key]["candidate_sha256"] = _sha(canonical_json(inputs[name]))
    with pytest.raises(ValueError):
        _verify(inputs)


@pytest.mark.parametrize("case", ["empty", "missing", "unknown", "runtime", "environment",
                                  "inputs", "command", "receipt", "expired", "head", "failed"])
def test_validation_cannot_transfer(inputs, case):
    current = inputs["validation_after"]
    if case == "empty":
        current["checks"] = []
    elif case == "missing":
        del current["runtime_sha256"]
    elif case == "unknown":
        current["optional"] = "unchecked"
    elif case in {"runtime", "environment", "inputs"}:
        current[case + "_sha256"] = "f" * 64
    elif case in {"command", "receipt"}:
        current["checks"][0][case + "_sha256"] = "f" * 64
    elif case == "expired":
        for key in ("validation_before", "validation_after"):
            inputs[key]["checks"][0]["valid_until"] = 1
    elif case == "head":
        current["checks"][0]["head_sensitive"] = True
    else:
        current["checks"][0]["result"] = "failed"
    with pytest.raises(ValueError):
        _verify(inputs)


@pytest.mark.parametrize("case", ["digest", "duplicate-key", "noncanonical", "oversize", "deep"])
def test_invalid_input_bytes(inputs, case):
    raw, pins = _encode(inputs)
    if case == "digest":
        pins["approved"] = "0" * 64
    else:
        raw["approved"] = {"duplicate-key": b'{"x":1,"x":2}',
                           "noncanonical": raw["approved"] + b"\n",
                           "oversize": b" " * (equivalence.MAX_INPUT_BYTES + 1),
                           "deep": b"[" * 2000 + b"]" * 2000}[case]
        pins["approved"] = _sha(raw["approved"])
    with pytest.raises(ValueError):
        equivalence.verify_commit_equivalence(**raw, expected_digests=pins)


@pytest.mark.parametrize("optimized", [False, True])
def test_cli_reads_pinned_files(inputs, tmp_path, optimized):
    raw, pins = _encode(inputs)
    policy = (Path(__file__).parents[1] / "docs" / "protected-change-review.md").read_text()
    section = policy.split("### Optional committed-content equivalence", 1)[1]
    command = shlex.split(section.split("```sh\n", 1)[1].split("```", 1)[0].replace("\\\n", ""))
    assert command[0] == "/absolute/pinned-runtime/bin/python"
    command[0] = sys.executable
    if optimized:
        command.insert(1, "-O")
    for key, value in {**raw, "digests": canonical_json(pins)}.items():
        path = tmp_path / key
        path.write_bytes(value)
        command[command.index("--" + key.replace("_", "-")) + 1] = str(path)
    shadow = tmp_path / "shadow"
    (shadow / "graphify").mkdir(parents=True)
    (shadow / "graphify" / "__init__.py").write_text("raise RuntimeError('untrusted-shadow')")
    env = {**os.environ, "PYTHONPATH": str(shadow)}
    # Prove the hostile fixture would otherwise be selected.
    ambient = subprocess.run([sys.executable, "-c", "import graphify"],
                             cwd=shadow, env=env, capture_output=True)
    assert ambient.returncode != 0 and b"untrusted-shadow" in ambient.stderr
    result = subprocess.run(command, cwd=shadow, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == "equivalent"
    (tmp_path / "commit_object").write_bytes(b"changed")
    result = subprocess.run(command, cwd=shadow, env=env, capture_output=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["result"] == "rejected"
