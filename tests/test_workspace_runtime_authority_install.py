from __future__ import annotations

import hashlib
from pathlib import Path
import stat

import pytest

from graphify.workspace import ArtifactManifest, WorkspaceRuntimeAuthority, canonical_json_bytes
from tests.workspace_p3_helpers import COMPATIBILITY_MANIFEST
from tools.workspace_artifacts import (
    ArtifactError,
    prove_independent_tamper_rejection,
    write_trusted_manifest,
)
import tools.workspace_artifacts.candidate as candidate


def _minimal_candidate(artifact_root: Path) -> tuple[bytes, bytes, bytes]:
    """Create the smallest frozen candidate that can authorize the P5C1 proof."""

    artifact_root.mkdir()
    authority = candidate._p5c1_runtime_authority(COMPATIBILITY_MANIFEST)
    compatibility = COMPATIBILITY_MANIFEST.canonical
    runtime_authority = authority.canonical
    (artifact_root / "compatibility.json").write_bytes(compatibility)
    (artifact_root / "runtime-manifest.json").write_bytes(runtime_authority)
    trusted = write_trusted_manifest(
        artifact_root=artifact_root,
        artifact_names=["compatibility.json", "runtime-manifest.json"],
    )
    return compatibility, runtime_authority, trusted


def _assert_within(root: Path, value: str) -> None:
    assert Path(value).resolve().is_relative_to(root.resolve())


def test_p5c1_candidate_authority_is_canonical_trusted_and_tamper_rejecting(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "candidate"
    compatibility, payload, trusted = _minimal_candidate(artifact_root)

    authority = WorkspaceRuntimeAuthority.from_json(payload)
    assert authority.canonical == payload
    assert authority.compatibility_manifest.canonical == compatibility
    assert authority.semantic_queue_policy.to_dict() == {
        "contract": "graphify.workspace.semantic_queue_policy.internal",
        "format_version": 1,
        "max_items": 8,
        "max_bytes": 16_384,
        "retry_budget": 1,
    }

    document = ArtifactManifest.from_json(trusted).to_dict()
    entries = {entry["path"]: entry for entry in document["artifacts"]}
    assert set(entries) == {"compatibility.json", "runtime-manifest.json"}
    for name, expected in (
        ("compatibility.json", compatibility),
        ("runtime-manifest.json", payload),
    ):
        assert entries[name]["mode"] == "0644"
        assert entries[name]["sha256"] == hashlib.sha256(expected).hexdigest()
        assert stat.S_IMODE((artifact_root / name).stat().st_mode) == 0o644

    rejected = prove_independent_tamper_rejection(
        artifact_root=artifact_root,
        trusted_manifest=trusted,
        artifact_names=("compatibility.json", "runtime-manifest.json"),
    )
    assert set(rejected) == {"compatibility.json", "runtime-manifest.json"}
    assert all("trusted artifact digest mismatch" in reason for reason in rejected.values())
    assert (artifact_root / "compatibility.json").read_bytes() == compatibility
    assert (artifact_root / "runtime-manifest.json").read_bytes() == payload


def test_runtime_authority_rejects_untrusted_expected_digest_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "candidate"
    _compatibility, _payload, trusted = _minimal_candidate(artifact_root)
    proof_root = tmp_path / "proof"

    def unexpected_install(**_kwargs: object) -> object:
        raise AssertionError("installation must not start with an untrusted digest")

    monkeypatch.setattr(
        candidate, "_prove_successful_runtime_authority_install", unexpected_install
    )

    with pytest.raises(ArtifactError, match="expected runtime authority digest"):
        candidate._prove_runtime_authority_installation(
            artifact_root=artifact_root,
            trusted_manifest=trusted,
            expected_sha256="0" * 64,
            proof_root=proof_root,
        )

    assert not proof_root.exists()


def test_runtime_authority_installation_proof_is_isolated_and_compensates_failures(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "candidate"
    _compatibility, payload, trusted = _minimal_candidate(artifact_root)
    digest = hashlib.sha256(payload).hexdigest()
    proof_root = tmp_path / "proof"

    proof = candidate._prove_runtime_authority_installation(
        artifact_root=artifact_root,
        trusted_manifest=trusted,
        expected_sha256=digest,
        proof_root=proof_root,
    )

    installed = proof_root / "success/xdg-state-home/graphify/runtime-manifest.json"
    assert installed.read_bytes() == payload
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert proof["authority_sha256"] == digest
    assert proof["canonical_round_trip"] is True
    assert proof["proof_policy"] == {
        "contract": "graphify.workspace.semantic_queue_policy.internal",
        "format_version": 1,
        "max_items": 8,
        "max_bytes": 16_384,
        "retry_budget": 1,
    }

    success = proof["success"]
    assert isinstance(success, dict)
    assert success["installed_mode"] == "0600"
    assert success["same_byte_retry_inode_preserved"] is True
    assert success["different_byte_retry_rejected"] is True
    assert success["loader_read_only"] is True
    preexisting_conflict = proof["preexisting_conflict"]
    assert isinstance(preexisting_conflict, dict)
    assert preexisting_conflict["different_bytes_rejected"] is True
    assert preexisting_conflict["prior_inode_preserved"] is True
    prior_payload = canonical_json_bytes(
        {
            "contract": "graphify.workspace.runtime_authority.proof_conflict",
            "format_version": 1,
        }
    )
    assert preexisting_conflict["prior_state"] == {
        "present": True,
        "sha256": hashlib.sha256(prior_payload).hexdigest(),
        "size": len(prior_payload),
        "mode": "0600",
    }
    assert proof["absent_target_compensation"] is True
    assert proof["preexisting_target_preserved_without_mutation"] is True
    assert proof["generation_trees_unchanged"] is True

    failures = proof["failures"]
    assert isinstance(failures, list)
    assert [failure["stage"] for failure in failures] == [
        "write",
        "temporary_fsync",
        "replace",
        "installed_hook",
        "parent_fsync",
    ]
    for failure in failures:
        assert failure["restored_state"] == {"present": False}
        assert failure["generation_tree_sha256"] == success["generation_tree_sha256"]
        assert failure["candidate_visible_before_compensation"] is (
            failure["stage"] in {"installed_hook", "parent_fsync"}
        )

    for section in [success, preexisting_conflict, *failures]:
        assert isinstance(section, dict)
        environment = section["environment"]
        assert isinstance(environment, dict)
        for name in ("HOME", "XDG_STATE_HOME", "CODEX_HOME"):
            _assert_within(proof_root, environment[name])
