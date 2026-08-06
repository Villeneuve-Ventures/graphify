"""P5B2 internal semantic-generation promotion finalization coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

import pytest

import graphify.workspace.sync as workspace_sync
from graphify.workspace.generations import GenerationConflict
from tests.test_workspace_semantic_generation_certification_finalization import (
    GENERATION_ID,
    _complete_handoff,
)
from tests.workspace_p3_helpers import REPO_UUID


@pytest.mark.xfail(
    raises=GenerationConflict,
    reason="acquire_staged_recovery rejects PROMOTED terminal cleanup",
    strict=True,
)
def test_promoted_terminal_cleanup_can_replace_rebooted_exact_staged_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sole authorized acquisition must support terminal cleanup recovery."""

    _, runtime, request, _, _ = _complete_handoff(
        tmp_path,
        monkeypatch,
    )
    workspace_sync._finalize_semantic_generation_certification(runtime, request)
    certified = runtime.generations.recover_staged_build(REPO_UUID)
    assert certified is not None
    assert certified.lifecycle_state == "CERTIFIED"
    receipt = runtime.generations.verify_generation(REPO_UUID, GENERATION_ID)
    _, observations = workspace_sync._observe_structural_source(
        runtime,
        REPO_UUID,
    )
    attempt_sha256 = hashlib.sha256(b"promotion-terminal-cleanup").hexdigest()

    # Simulate process death after the terminal staged write but before release.
    monkeypatch.setattr(workspace_sync, "_release_grant", lambda *_args: None)
    promoted = workspace_sync._promote(
        runtime,
        request,
        certified.request,
        receipt,
        observations,
        attempt_sha256=attempt_sha256,
    )
    assert promoted is not None
    assert promoted.lifecycle_state == "PROMOTED"
    retained = runtime.leases.inspect(REPO_UUID)
    assert retained.staged_attempt_sha256 == attempt_sha256
    assert retained.leases.get("workspace") is not None

    original_owner = runtime.leases.current_owner()
    monkeypatch.setattr(
        runtime.leases,
        "current_owner",
        lambda: replace(original_owner, boot_id="rebooted-promotion-cleanup-owner"),
    )

    # Frozen contract: expired/rebooted terminal cleanup must replace only the
    # exact persisted request/target-bound attempt through this acquisition.
    cleanup = runtime.generations.acquire_staged_recovery(
        REPO_UUID,
        GENERATION_ID,
        certified.request,
        attempt_sha256=attempt_sha256,
        acquired_at=datetime.now(timezone.utc),
        monotonic_ns=time.monotonic_ns(),
        ttl_ns=60_000_000_000,
    )

    try:
        assert cleanup.state.canonical == promoted.canonical
        assert cleanup.grant.lease.to_dict()["operation"] == "PROMOTE"
    finally:
        runtime.leases.release(cleanup.grant)
