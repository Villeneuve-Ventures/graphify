"""P5B2 private semantic-release policy-authority store contract."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest

import graphify.workspace as workspace
from graphify.workspace.contracts import canonical_json_bytes
from graphify.workspace.persistence import (
    CommitUnknown,
    InjectedFault,
    PosixSyscalls,
    StateCorrupt,
    StatePathError,
)
from graphify.workspace.semantic_release import (
    CORE_SECRETS_PROFILE,
    load_installed_semantic_release_bundle,
)
import graphify.workspace.semantic_release_policy as policy_module
from graphify.workspace.semantic_release_policy import (
    POLICY_AUTHORITY_CURRENT,
    POLICY_AUTHORITY_PENDING,
    POLICY_AUTHORITY_PREVIOUS,
    POLICY_AUTHORITY_RECORD_MAX_BYTES,
    POLICY_AUTHORITY_TRANSACTION_PEAK_BYTES,
    POLICY_SELECTION_AUTHORIZATION_MAX_BYTES,
    SELECT_SEMANTIC_RELEASE_POLICY,
    SemanticReleaseCoverageSufficiency,
    SemanticReleasePairDisposition,
    SemanticReleasePolicy,
    SemanticReleasePolicyAuthorityConflict,
    SemanticReleasePolicyAuthorityInvalid,
    SemanticReleasePolicyAuthorityRecord,
    SemanticReleasePolicyAuthorityRecoveryRequired,
    SemanticReleasePolicyAuthorityStore,
    SemanticReleasePolicyProfile,
    SemanticReleasePolicySelection,
    SemanticReleasePolicySelectionAuthorization,
)
from tests.workspace_p3_helpers import REPO_UUID, SUPPORTED, RuntimeHarness, create_harness


def _store(
    harness: RuntimeHarness,
    *,
    fault_hook=None,
    syscalls=None,
) -> SemanticReleasePolicyAuthorityStore:
    return SemanticReleasePolicyAuthorityStore(
        harness.state_root,
        harness.registry,
        capabilities=SUPPORTED,
        fault_hook=fault_hook,
        syscalls=syscalls,
    )


def _profile(profile_id: str = CORE_SECRETS_PROFILE.profile_id) -> SemanticReleasePolicyProfile:
    bundle = load_installed_semantic_release_bundle()
    artifact = next(
        item
        for item in bundle.artifacts
        if item.artifact_kind == "profile" and item.artifact_id == profile_id
    )
    return SemanticReleasePolicyProfile(
        profile_id=artifact.artifact_id,
        profile_version=artifact.artifact_version,
        profile_sha256=artifact.sha256,
    )


def _selection(
    *,
    expected_revision: int = 0,
    expected_sha256: str | None = None,
    nonce: str = "policy-1",
    release_context: str = "public_release.v1",
    profiles: tuple[SemanticReleasePolicyProfile, ...] | None = None,
    coverage_state: str = "SUFFICIENT",
    policy_id: str = "graphify.semantic_release.test_policy.v1",
) -> SemanticReleasePolicySelection:
    bundle = load_installed_semantic_release_bundle()
    selected = (_profile(),) if profiles is None else profiles
    coverage = SemanticReleaseCoverageSufficiency(
        release_context=release_context,
        selected_profiles=selected,
        coverage_state=coverage_state,
    )
    core_category = next(
        category
        for profile in bundle.profiles
        if profile.coordinate == CORE_SECRETS_PROFILE
        for category in profile.category_ids
    )
    pairs = (
        (
            SemanticReleasePairDisposition("node_label", core_category, "REJECT_RELEASE"),
            SemanticReleasePairDisposition("node_rationale", core_category, "OMIT_RATIONALE"),
            SemanticReleasePairDisposition("hyperedge_label", core_category, "REJECT_RELEASE"),
        )
        if selected
        else ()
    )
    policy = SemanticReleasePolicy(
        policy_id=policy_id,
        policy_version=1,
        release_context=release_context,
        coverage_sufficiency_sha256=coverage.sha256,
        pair_dispositions=pairs,
        reduction_precedence=("REJECT_RELEASE", "OMIT_RATIONALE", "ALLOW_FIELD"),
    )
    return SemanticReleasePolicySelection(
        repo_uuid=REPO_UUID,
        expected_authority_revision=expected_revision,
        expected_authority_sha256=expected_sha256,
        release_context=release_context,
        bundle_manifest_sha256=bundle.manifest_sha256,
        selected_profiles=selected,
        coverage_sufficiency=coverage,
        policy_id=policy_id,
        policy_version=1,
        policy=policy,
        authorization=SemanticReleasePolicySelectionAuthorization(
            action=SELECT_SEMANTIC_RELEASE_POLICY,
            issued_at="2026-08-15T15:00:00Z",
            nonce=nonce,
            operator_id="operator:semantic-release-test",
            reason="select exact test policy",
        ),
    )


def _authority_directory(harness: RuntimeHarness) -> Path:
    return harness.state_root / "workspaces" / REPO_UUID


def _authority_paths(harness: RuntimeHarness) -> tuple[Path, Path, Path]:
    directory = _authority_directory(harness)
    return (
        directory / POLICY_AUTHORITY_CURRENT,
        directory / POLICY_AUTHORITY_PREVIOUS,
        directory / POLICY_AUTHORITY_PENDING,
    )


def _authority_metadata(harness: RuntimeHarness) -> dict[str, tuple[int, int, int, str]]:
    result: dict[str, tuple[int, int, int, str]] = {}
    for path in _authority_paths(harness):
        if path.exists():
            details = path.stat()
            result[path.name] = (
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return result


def _rewrite_record(
    record: SemanticReleasePolicyAuthorityRecord,
    **changes: object,
) -> bytes:
    value = record.to_dict()
    value.update(changes)
    body = {
        key: item
        for key, item in value.items()
        if key not in {"selection_authorization", "selection_authorization_sha256"}
    }
    envelope = cast(dict[str, object], value["selection_authorization"])
    envelope["authority_body_sha256"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    value["selection_authorization"] = envelope
    value["selection_authorization_sha256"] = hashlib.sha256(
        canonical_json_bytes(envelope)
    ).hexdigest()
    return canonical_json_bytes(value)


def test_store_is_private_and_constructor_provisions_no_live_authority(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)

    assert store.read(REPO_UUID) is None
    assert store.project_recovery(REPO_UUID).phase == "ABSENT"
    assert not any(path.exists() for path in _authority_paths(harness))
    assert not hasattr(workspace, "SemanticReleasePolicyAuthorityStore")
    assert not hasattr(store, "revoke")
    assert not hasattr(store, "reactivate")
    assert not hasattr(store, "rollback")
    assert not hasattr(store, "delete")
    assert not hasattr(store, "gc")


def test_genesis_derives_exact_body_envelope_and_complete_record_preimages(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    request = _selection()

    record = store.select(request)
    current, previous, pending = _authority_paths(harness)
    raw = current.read_bytes()
    value = cast(dict[str, object], json.loads(raw))
    body = {
        key: item
        for key, item in value.items()
        if key not in {"selection_authorization", "selection_authorization_sha256"}
    }
    envelope = cast(dict[str, object], value["selection_authorization"])

    assert record.authority_revision == 1
    assert record.previous_authority_sha256 is None
    assert record.state == "ACTIVE"
    assert raw == record.canonical == canonical_json_bytes(value)
    assert record.sha256 == hashlib.sha256(raw).hexdigest()
    assert (
        envelope["authority_body_sha256"] == hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    )
    assert (
        value["selection_authorization_sha256"]
        == hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
    )
    assert "selection_authorization" not in body
    assert "selection_authorization_sha256" not in body
    assert "selection_authorization_sha256" not in envelope
    assert record.selection_authorization.authorization.to_dict() == {
        "action": SELECT_SEMANTIC_RELEASE_POLICY,
        "issued_at": "2026-08-15T15:00:00Z",
        "nonce": "policy-1",
        "operator_id": "operator:semantic-release-test",
        "reason": "select exact test policy",
    }
    assert len(raw) <= POLICY_AUTHORITY_RECORD_MAX_BYTES
    assert len(record.selection_authorization.canonical) <= (
        POLICY_SELECTION_AUTHORIZATION_MAX_BYTES
    )
    assert current.stat().st_mode & 0o777 == 0o600
    assert current.stat().st_nlink == 1
    assert not previous.exists()
    assert not pending.exists()
    assert store.read(REPO_UUID) == record


def test_each_body_and_operator_mutation_changes_exact_digest_layers() -> None:
    predecessor = "a" * 64
    request = _selection(expected_revision=1, expected_sha256=predecessor)
    baseline = policy_module._build_record(request)
    changed_profile = replace(_profile(), profile_sha256="0" * 64)
    body_variants = {
        "repository": replace(
            request,
            repo_uuid="00000000-0000-4000-8000-000000000001",
        ),
        "context": _selection(
            expected_revision=1,
            expected_sha256=predecessor,
            release_context="candidate_release.v1",
        ),
        "bundle": replace(request, bundle_manifest_sha256="1" * 64),
        "profile": _selection(
            expected_revision=1,
            expected_sha256=predecessor,
            profiles=(changed_profile,),
        ),
        "coverage": _selection(
            expected_revision=1,
            expected_sha256=predecessor,
            coverage_state="INSUFFICIENT",
        ),
        "policy": _selection(
            expected_revision=1,
            expected_sha256=predecessor,
            policy_id="graphify.semantic_release.changed_policy.v1",
        ),
        "revision": replace(request, expected_authority_revision=2),
        "predecessor": replace(request, expected_authority_sha256="b" * 64),
    }

    for label, variant in body_variants.items():
        changed = policy_module._build_record(variant)
        assert (
            changed.selection_authorization.authority_body_sha256
            != baseline.selection_authorization.authority_body_sha256
        ), label
        assert (
            changed.selection_authorization.canonical != baseline.selection_authorization.canonical
        )
        assert changed.selection_authorization_sha256 != baseline.selection_authorization_sha256
        assert changed.canonical != baseline.canonical
        assert changed.sha256 != baseline.sha256

    operator_variants = {
        "issued_at": replace(request.authorization, issued_at="2026-08-15T15:00:01Z"),
        "nonce": replace(request.authorization, nonce="changed-nonce"),
        "operator_id": replace(request.authorization, operator_id="operator:changed"),
        "reason": replace(request.authorization, reason="changed selection reason"),
    }
    for label, authorization in operator_variants.items():
        changed = policy_module._build_record(replace(request, authorization=authorization))
        assert (
            changed.selection_authorization.authority_body_sha256
            == baseline.selection_authorization.authority_body_sha256
        ), label
        assert (
            changed.selection_authorization.canonical != baseline.selection_authorization.canonical
        )
        assert changed.selection_authorization_sha256 != baseline.selection_authorization_sha256
        assert changed.canonical != baseline.canonical
        assert changed.sha256 != baseline.sha256


def test_advancement_is_revision_plus_one_with_exact_predecessor_and_replay_is_no_write(
    tmp_path: Path,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    first = store.select(_selection())
    request = _selection(
        expected_revision=first.authority_revision,
        expected_sha256=first.sha256,
        nonce="policy-2",
        policy_id="graphify.semantic_release.test_policy.v2",
    )

    second = store.select(request)
    current, previous, pending = _authority_paths(harness)

    assert second.authority_revision == 2
    assert second.previous_authority_sha256 == first.sha256
    assert previous.read_bytes() == first.canonical
    assert current.read_bytes() == second.canonical
    assert not pending.exists()
    before = _authority_metadata(harness)
    replay = store.select(request)
    after = _authority_metadata(harness)

    assert replay == second
    assert before == after


def test_exact_replay_is_no_write_even_without_transaction_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    request = _selection()
    committed = store.select(request)
    before = _authority_metadata(harness)
    actual = os.statvfs(_authority_directory(harness))

    monkeypatch.setattr(
        policy_module.os,
        "fstatvfs",
        lambda _descriptor: SimpleNamespace(f_bavail=0, f_frsize=actual.f_frsize),
    )

    assert store.select(request) == committed
    assert _authority_metadata(harness) == before


@pytest.mark.parametrize(
    ("revision_delta", "digest"),
    [
        (0, "0" * 64),
        (1, None),
        (2, "f" * 64),
    ],
)
def test_advancement_rejects_revision_or_digest_cas_without_mutation(
    tmp_path: Path,
    revision_delta: int,
    digest: str | None,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    first = store.select(_selection())
    requested_digest = first.sha256 if digest is None else digest
    request = _selection(
        expected_revision=first.authority_revision + revision_delta,
        expected_sha256=requested_digest,
        nonce=f"bad-cas-{revision_delta}",
    )
    before = _authority_metadata(harness)

    with pytest.raises(
        (SemanticReleasePolicyAuthorityConflict, SemanticReleasePolicyAuthorityInvalid)
    ):
        store.select(request)

    assert _authority_metadata(harness) == before
    assert not _authority_paths(harness)[2].exists()


def test_closed_selection_input_directly_rejects_ambient_and_shopping_fields() -> None:
    request = _selection()
    selection_signature = inspect.signature(SemanticReleasePolicySelection)
    select_signature = inspect.signature(SemanticReleasePolicyAuthorityStore.select)
    forbidden = {
        "ambient_default",
        "environment",
        "provider",
        "model",
        "credential",
        "network",
        "catalogue",
        "newest",
        "newest_record",
        "policies",
        "policy_candidates",
        "record_bytes",
        "path",
    }

    assert forbidden.isdisjoint(selection_signature.parameters)
    assert forbidden.isdisjoint(select_signature.parameters)
    assert set(select_signature.parameters) == {"self", "request", "deadline_ns"}
    with pytest.raises(TypeError):
        cast(Any, SemanticReleasePolicySelection)(
            **request.__dict__,
            provider="ambient-provider",
        )
    with pytest.raises(TypeError):
        cast(Any, SemanticReleasePolicyAuthorityStore.select)(
            object(),
            request,
            environment={"GRAPHIFY_POLICY": "newest"},
        )


def test_selection_requires_exact_action_and_active_only() -> None:
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="action must be"):
        replace(_selection().authorization, action="REVOKE_SEMANTIC_RELEASE_POLICY")
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="action must be"):
        replace(_selection().authorization, action="REACTIVATE_SEMANTIC_RELEASE_POLICY")


def test_decoder_recognizes_revoked_but_selection_cannot_advance_it(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    active = store.select(_selection())
    revoked_bytes = _rewrite_record(active, state="REVOKED")
    revoked = SemanticReleasePolicyAuthorityRecord.from_json(revoked_bytes)
    current, _previous, _pending = _authority_paths(harness)
    current.write_bytes(revoked_bytes)
    current.chmod(0o600)

    assert revoked.state == "REVOKED"
    reopened = store.read(REPO_UUID)
    assert reopened is not None and reopened.state == "REVOKED"
    with pytest.raises(SemanticReleasePolicyAuthorityConflict):
        store.select(
            _selection(
                expected_revision=revoked.authority_revision,
                expected_sha256=revoked.sha256,
                nonce="no-reactivation",
            )
        )


@pytest.mark.parametrize(
    "extra_field",
    [
        "environment",
        "provider",
        "model",
        "credential",
        "network",
        "live_catalogue",
        "policy_candidates",
    ],
)
def test_canonical_record_decoder_rejects_additional_ambient_members(
    tmp_path: Path,
    extra_field: str,
) -> None:
    harness = create_harness(tmp_path)
    record = _store(harness).select(_selection())
    value = record.to_dict()
    value[extra_field] = "forbidden"

    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="members must be exactly"):
        SemanticReleasePolicyAuthorityRecord.from_json(canonical_json_bytes(value))


def test_profiles_coverage_and_policy_are_exact_manifest_bound_input(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    core = _profile()
    provider = _profile("provider_credentials.v1")

    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="utf8_lex_v1"):
        _selection(profiles=(provider, core))
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="duplicated"):
        _selection(profiles=(core, core))
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="core_secrets"):
        store.select(_selection(profiles=(provider,)))
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="installed manifest bytes"):
        store.select(
            _selection(
                profiles=(replace(core, profile_sha256="0" * 64),),
            )
        )

    insufficient = _selection(profiles=(), coverage_state="INSUFFICIENT")
    record = store.select(insufficient)
    assert record.coverage_sufficiency.coverage_state == "INSUFFICIENT"


def test_policy_pair_order_duplicates_and_omit_scope_fail_closed() -> None:
    request = _selection()
    pairs = request.policy.pair_dispositions
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="canonical"):
        replace(request.policy, pair_dispositions=tuple(reversed(pairs)))
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="duplicated"):
        replace(request.policy, pair_dispositions=(pairs[0], pairs[0]))
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="only for node_rationale"):
        SemanticReleasePairDisposition(
            "node_label",
            pairs[0].category_id,
            "OMIT_RATIONALE",
        )


def test_fixed_namespace_and_filesystem_reserve_fail_before_pending_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    directory = _authority_directory(harness)
    unexpected = directory / "semantic-release-policy-authority.newest.json"
    unexpected.write_bytes(b"forbidden")
    unexpected.chmod(0o600)

    with pytest.raises(StatePathError, match="unexpected"):
        store.select(_selection())
    assert not _authority_paths(harness)[2].exists()
    unexpected.unlink()

    real_fstatvfs = os.fstatvfs

    def no_reserve(descriptor: int):
        actual = real_fstatvfs(descriptor)
        return SimpleNamespace(f_bavail=0, f_frsize=actual.f_frsize)

    monkeypatch.setattr(policy_module.os, "fstatvfs", no_reserve)
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="filesystem reserve"):
        store.select(_selection())
    assert not _authority_paths(harness)[2].exists()
    assert POLICY_AUTHORITY_TRANSACTION_PEAK_BYTES == 4 * POLICY_AUTHORITY_RECORD_MAX_BYTES


def test_store_uses_shared_workspace_lock_for_reads_and_registry_then_workspace_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    harness = create_harness(tmp_path, fault_hook=events.append)
    store = _store(harness, fault_hook=events.append)
    request = _selection()
    store.select(request)

    assert events.index("lock:registry:acquired") < events.index("lock:workspace:acquired")
    calls: list[bool] = []
    original = store.state.existing_lock

    @contextmanager
    def existing_lock(*args, **kwargs) -> Iterator[None]:
        calls.append(bool(kwargs["exclusive"]))
        with original(*args, **kwargs):
            yield

    monkeypatch.setattr(store.state, "existing_lock", existing_lock)
    store.read(REPO_UUID)
    store.project_recovery(REPO_UUID)

    assert calls == [False, False]


@pytest.mark.parametrize(
    ("event", "expected_phase"),
    [
        ("semantic-release-policy-authority:pending_durable", "PENDING_BEFORE_RETENTION"),
        ("semantic-release-policy-authority:previous_durable", "PENDING_AFTER_RETENTION"),
        ("semantic-release-policy-authority:current_durable", "PENDING_CURRENT"),
        ("semantic-release-policy-authority:pending_cleared", "STABLE"),
    ],
)
def test_commit_unknown_advancement_recovers_only_exact_durable_prefix(
    tmp_path: Path,
    event: str,
    expected_phase: str,
) -> None:
    harness = create_harness(tmp_path)
    stable_store = _store(harness)
    first = stable_store.select(_selection())
    request = _selection(
        expected_revision=1,
        expected_sha256=first.sha256,
        nonce=f"fault-{expected_phase}",
        policy_id="graphify.semantic_release.test_policy.v2",
    )

    def failpoint(actual: str) -> None:
        if actual == event:
            raise InjectedFault(actual)

    uncertain_store = _store(harness, fault_hook=failpoint)
    with pytest.raises(CommitUnknown):
        uncertain_store.select(request)

    projection = stable_store.project_recovery(REPO_UUID)
    assert projection.phase == expected_phase
    if expected_phase == "STABLE":
        assert not projection.requires_recovery
    else:
        assert projection.requires_recovery
        with pytest.raises(SemanticReleasePolicyAuthorityRecoveryRequired):
            stable_store.read(REPO_UUID)
    recovered = stable_store.recover(REPO_UUID)

    assert recovered is not None
    assert recovered.authority_revision == 2
    assert recovered.previous_authority_sha256 == first.sha256
    assert stable_store.read(REPO_UUID) == recovered


def test_failure_before_pending_visibility_is_definite_no_commit(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)

    class FailFirstReplace(PosixSyscalls):
        def __init__(self) -> None:
            self.failed = False

        def replace_at(
            self,
            source: str,
            destination: str,
            *,
            source_dir_fd: int,
            destination_dir_fd: int,
        ) -> None:
            if not self.failed:
                self.failed = True
                raise OSError(errno.EIO, "injected replace before visibility")
            super().replace_at(
                source,
                destination,
                source_dir_fd=source_dir_fd,
                destination_dir_fd=destination_dir_fd,
            )

    store = _store(harness, syscalls=FailFirstReplace())
    with pytest.raises(OSError, match="before visibility"):
        store.select(_selection())

    assert not any(path.exists() for path in _authority_paths(harness))


def test_post_commit_proof_failure_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    installed_bundle = store._bundle
    calls = 0

    def fail_final_bundle_proof():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("post-commit bundle proof failed")
        return installed_bundle()

    monkeypatch.setattr(store, "_bundle", fail_final_bundle_proof)
    with pytest.raises(CommitUnknown) as failure:
        store.select(_selection())

    current, _previous, pending = _authority_paths(harness)
    assert isinstance(failure.value.__cause__, RuntimeError)
    assert current.exists()
    assert not pending.exists()


def test_post_commit_workspace_lock_release_failure_is_commit_unknown(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)

    def failpoint(event: str) -> None:
        if event == "lock:workspace:released":
            raise InjectedFault(event)

    with pytest.raises(CommitUnknown) as failure:
        _store(harness, fault_hook=failpoint).select(_selection())

    current, _previous, pending = _authority_paths(harness)
    assert isinstance(failure.value.__cause__, InjectedFault)
    assert current.exists()
    assert not pending.exists()


def test_genesis_commit_unknown_projects_recovers_and_clears_pending(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)

    def failpoint(event: str) -> None:
        if event == "semantic-release-policy-authority:pending_durable":
            raise InjectedFault(event)

    with pytest.raises(CommitUnknown):
        _store(harness, fault_hook=failpoint).select(_selection())

    store = _store(harness)
    projection = store.project_recovery(REPO_UUID)
    assert projection.phase == "PENDING_GENESIS"
    assert projection.requires_recovery
    recovered = store.recover(REPO_UUID)

    assert recovered is not None and recovered.authority_revision == 1
    assert not _authority_paths(harness)[2].exists()
    assert store.read(REPO_UUID) == recovered


def test_post_recovery_proof_failure_is_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)

    def failpoint(event: str) -> None:
        if event == "semantic-release-policy-authority:pending_durable":
            raise InjectedFault(event)

    with pytest.raises(CommitUnknown):
        _store(harness, fault_hook=failpoint).select(_selection())

    store = _store(harness)
    installed_bundle = store._bundle
    calls = 0

    def fail_final_bundle_proof():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("post-recovery bundle proof failed")
        return installed_bundle()

    monkeypatch.setattr(store, "_bundle", fail_final_bundle_proof)
    with pytest.raises(CommitUnknown) as failure:
        store.recover(REPO_UUID)

    current, _previous, pending = _authority_paths(harness)
    assert isinstance(failure.value.__cause__, RuntimeError)
    assert current.exists()
    assert not pending.exists()
    assert _store(harness).read(REPO_UUID) is not None


def test_recovery_rejects_skipped_newest_pending_and_never_policy_shops(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    current = store.select(_selection())
    skipped_request = _selection(
        expected_revision=current.authority_revision + 1,
        expected_sha256=current.sha256,
        nonce="skip-revision",
        policy_id="graphify.semantic_release.newest_policy.v9",
    )
    skipped = policy_module._build_record(skipped_request)
    pending_path = _authority_paths(harness)[2]
    pending_path.write_bytes(skipped.canonical)
    pending_path.chmod(0o600)
    before = _authority_metadata(harness)

    with pytest.raises(StateCorrupt, match="does not advance the exact current"):
        store.project_recovery(REPO_UUID)
    with pytest.raises(StateCorrupt, match="does not advance the exact current"):
        store.recover(REPO_UUID)

    assert _authority_metadata(harness) == before
    assert _authority_paths(harness)[0].read_bytes() == current.canonical


def test_recovery_rejects_revoked_pending_without_cleanup(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    current = store.select(_selection())
    candidate = policy_module._build_record(
        _selection(
            expected_revision=1,
            expected_sha256=current.sha256,
            nonce="revoked-pending",
        )
    )
    pending_bytes = _rewrite_record(candidate, state="REVOKED")
    pending = _authority_paths(harness)[2]
    pending.write_bytes(pending_bytes)
    pending.chmod(0o600)
    before = _authority_metadata(harness)

    with pytest.raises(StateCorrupt, match="not an ACTIVE selection"):
        store.recover(REPO_UUID)

    assert _authority_metadata(harness) == before


def test_bounded_orphan_temporary_cleanup_precedes_recovery_projection(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    record = store.select(_selection())
    directory = _authority_directory(harness)
    temporary = directory / (f".{POLICY_AUTHORITY_CURRENT}.tmp-{os.getpid()}-{'a' * 32}")
    temporary.write_bytes(b"orphan")
    temporary.chmod(0o600)
    before = _authority_metadata(harness)

    projection = store.project_recovery(REPO_UUID)
    assert projection.orphan_temporary
    assert projection.requires_recovery
    assert store.recover(REPO_UUID) == record

    assert not temporary.exists()
    assert _authority_metadata(harness) == before


def test_multiple_or_unsafe_authority_temporaries_fail_closed(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    directory = _authority_directory(harness)
    first = directory / f".{POLICY_AUTHORITY_CURRENT}.tmp-1-{'a' * 32}"
    second = directory / f".{POLICY_AUTHORITY_PENDING}.tmp-1-{'b' * 32}"
    first.write_bytes(b"one")
    first.chmod(0o600)
    second.write_bytes(b"two")
    second.chmod(0o600)

    with pytest.raises(StatePathError, match="multiple"):
        store.project_recovery(REPO_UUID)
    second.unlink()
    first.unlink()
    first.symlink_to(directory / "workspace.lock")

    with pytest.raises(StatePathError, match="unsafe"):
        store.project_recovery(REPO_UUID)


@pytest.mark.parametrize("mode", [0o644, 0o400])
def test_authority_record_mode_drift_fails_closed(tmp_path: Path, mode: int) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    store.select(_selection())
    current = _authority_paths(harness)[0]
    current.chmod(mode)

    with pytest.raises(StateCorrupt):
        store.read(REPO_UUID)


def test_hardlinked_authority_record_fails_closed(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    store.select(_selection())
    current = _authority_paths(harness)[0]
    alias = tmp_path / "authority-hardlink"
    os.link(current, alias)

    with pytest.raises(StateCorrupt):
        store.read(REPO_UUID)


def test_stable_read_revalidates_exact_snapshot_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    first = store.select(_selection())
    original = store._read_snapshot
    calls = 0

    def racing_snapshot(repo_uuid: str, *, deadline_ns: int | None):
        nonlocal calls
        calls += 1
        snapshot = original(repo_uuid, deadline_ns=deadline_ns)
        if calls == 1:
            current = _authority_paths(harness)[0]
            changed = _rewrite_record(first, release_context="changed_context.v1")
            current.write_bytes(changed)
            current.chmod(0o600)
        return snapshot

    monkeypatch.setattr(store, "_read_snapshot", racing_snapshot)
    with pytest.raises(StateCorrupt):
        store.read(REPO_UUID)


def test_record_decoder_rejects_noncanonical_and_recursive_digest_shapes(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    record = _store(harness).select(_selection())
    compact_without_newline = record.canonical.removesuffix(b"\n")
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="not canonical"):
        SemanticReleasePolicyAuthorityRecord.from_json(compact_without_newline)

    value = record.to_dict()
    envelope = cast(dict[str, object], value["selection_authorization"])
    envelope["selection_authorization_sha256"] = "0" * 64
    value["selection_authorization"] = envelope
    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="members must be exactly"):
        SemanticReleasePolicyAuthorityRecord.from_json(canonical_json_bytes(value))


def test_policy_authority_record_limit_rejects_before_store_mutation(tmp_path: Path) -> None:
    harness = create_harness(tmp_path)
    store = _store(harness)
    request = _selection()
    oversized_reason = "r" * POLICY_AUTHORITY_RECORD_MAX_BYTES
    oversized = replace(
        request,
        authorization=replace(request.authorization, reason=oversized_reason),
    )

    with pytest.raises(SemanticReleasePolicyAuthorityInvalid, match="exceeds"):
        store.select(oversized)

    assert not any(path.exists() for path in _authority_paths(harness))
