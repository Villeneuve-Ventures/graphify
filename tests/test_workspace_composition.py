"""Pure composition and existing-only authority refusal before any state write."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from graphify.workspace.adapters import AdapterIntent, CompatibilityTuple, select_adapter
from graphify.workspace.composition import (
    StructuralPolicy, WorkspaceAuthorityInvalid, WorkspaceRuntimeAuthority,
    WorkspaceRuntimeInputs, compose_workspace_runtime, load_workspace_runtime_inputs,
)
from graphify.workspace.contracts import ContractError
from tests.test_workspace_contracts import compatibility


def authority():
    return WorkspaceRuntimeAuthority.from_mapping({
        "contract": "graphify.workspace.runtime_authority.internal", "format_version": 2,
        "compatibility_manifest": compatibility().to_dict(),
        "structural_policy": StructuralPolicy(8, 16384, 1, 4, 1048576).to_dict(),
    })


@pytest.mark.parametrize("intent", list(AdapterIntent))
def test_exact_selection_never_executes_or_promotes(intent):
    candidate = CompatibilityTuple(compatibility())
    if intent is AdapterIntent.PROBE:
        selection = select_adapter(candidate, expected=candidate, intent=intent)
        assert not selection.executable and not selection.promotable
        with pytest.raises(ContractError): selection.require_adapter()
    else:
        with pytest.raises(ContractError): select_adapter(candidate, expected=candidate, intent=intent)


def test_different_wheel_same_version_is_not_same_tuple():
    from graphify.workspace.contracts import CompatibilityManifest
    a = compatibility()
    b = a.to_dict(); b["wheel_sha256"] = "0" * 64
    with pytest.raises(ContractError):
        select_adapter(CompatibilityTuple(a), expected=CompatibilityTuple(CompatibilityManifest.from_mapping(b)), intent=AdapterIntent.PROBE)


def test_composition_does_not_create_missing_state(tmp_path):
    root = tmp_path / "absent"
    auth = authority()
    result = compose_workspace_runtime(WorkspaceRuntimeInputs(root, auth, auth.compatibility))
    with pytest.raises(WorkspaceAuthorityInvalid, match="S3"):
        result.require_runtime()
    assert not root.exists()
    with pytest.raises(WorkspaceAuthorityInvalid):
        load_workspace_runtime_inputs(state_root=root, expected=auth.compatibility)
    assert not root.exists()


@pytest.mark.parametrize("damage", ["none", "mode", "root-mode", "root-mode-race", "symlink", "ancestor-link", "hardlink", "truncated", "duplicate", "oversized", "wrong-tuple", "no-policy"])
def test_authority_load_refuses_without_mutation(tmp_path, monkeypatch, damage):
    import graphify.workspace.composition as composition
    root = tmp_path.resolve() / "state"
    root.mkdir(mode=0o700)
    auth = authority()
    path = root / "runtime-manifest.json"
    payload = auth.canonical
    if damage == "truncated": payload = payload[:-4]
    if damage == "duplicate": payload = b'{"x":1,"x":1}\n'
    if damage == "oversized": payload = b" " * (composition.RUNTIME_AUTHORITY_MAX_BYTES + 1)
    if damage == "no-policy":
        data = auth.to_dict(); del data["structural_policy"]
        from graphify.workspace.contracts import canonical_json_bytes
        payload = canonical_json_bytes(data)
    path.write_bytes(payload); path.chmod(0o600)
    if damage == "mode": path.chmod(0o644)
    if damage == "root-mode": root.chmod(0o755)
    if damage == "symlink":
        target = root / "target"; path.rename(target); path.symlink_to(target)
    if damage == "hardlink": os.link(path, root / "alias")
    if damage == "ancestor-link":
        alias = tmp_path.resolve() / "alias"; alias.symlink_to(root, target_is_directory=True); root = alias
    expected = auth.compatibility
    if damage == "wrong-tuple":
        from graphify.workspace.contracts import CompatibilityManifest
        data = expected.to_dict(); data["wheel_sha256"] = "0" * 64
        expected = CompatibilityManifest.from_mapping(data)
    if damage == "root-mode-race":
        real_stat = composition.os.stat

        def raced_stat(name, *args, **kwargs):
            info = real_stat(name, *args, **kwargs)
            if name == root.name and kwargs.get("dir_fd") is not None:
                return os.stat_result((info.st_mode | 0o077, info.st_ino, info.st_dev,
                                       info.st_nlink, info.st_uid, info.st_gid,
                                       info.st_size, info.st_atime, info.st_mtime,
                                       info.st_ctime))
            return info

        monkeypatch.setattr(composition.os, "stat", raced_stat)
    before = [(p.name, p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_mtime_ns) for p in root.iterdir()]
    # Installed-package verification is independently tested through real wheels.
    monkeypatch.setattr(composition, "verify_installed_candidate", lambda expected: None)
    if damage == "none":
        result = load_workspace_runtime_inputs(state_root=root, expected=expected)
        assert result.authority == auth
    else:
        with pytest.raises(ContractError): load_workspace_runtime_inputs(state_root=root, expected=expected)
    assert before == [(p.name, p.lstat().st_ino, p.lstat().st_mode, p.lstat().st_mtime_ns) for p in root.iterdir()]
    assert path.read_bytes() == payload


@pytest.mark.parametrize("values", [(0, 1, 1, 1, 1), (True, 2, 1, 1, 1), (1, 2, 2, 1, 1)])
def test_policy_has_no_defaults_or_invalid_limits(values):
    with pytest.raises(ContractError): StructuralPolicy(*values)
    with pytest.raises(ContractError): StructuralPolicy.from_mapping({})


def test_cold_imports_have_no_platform_semantic_or_state_side_effects(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "CODEX_HOME": str(tmp_path / "codex"),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(repo)}
    code = '''
import sys
class Guard:
    def find_spec(self, name, *args):
        if name in {"fcntl", "graphify.extract", "graphify.serve", "graphify.llm"} or "semantic" in name:
            raise RuntimeError("eager import: " + name)
sys.meta_path.insert(0, Guard())
def audit(event, args):
    if event == "open" and args[2] & (64 | 512 | 1024 | 1 | 2):
        raise RuntimeError("write during import")
    if event in {"os.mkdir", "os.rename", "os.remove", "socket.connect"}:
        raise RuntimeError(event)
sys.addaudithook(audit)
import graphify
import graphify.workspace.composition
import graphify.workspace.adapters
print("S2-IMPORT-OK")
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "S2-IMPORT-OK"
    assert list(tmp_path.iterdir()) == []
