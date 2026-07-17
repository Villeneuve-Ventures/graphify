"""Two-sided, no-write observed-current output release authority."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Generic, Mapping, TypeVar, cast

from graphify.workspace.adapters import (
    AdapterIntent,
    CompatibilityTuple,
    ObservationHook,
    ObservationTimeout,
    ObservationUnavailable,
    ObservationUnstable,
    ObservationUnsupported,
    QueryRejected,
    QueryRequest,
    SourceObservation,
    UnsupportedCompatibility,
    select_adapter,
)
from graphify.workspace.contracts import (
    CompatibilityManifest,
    FreshnessRelease,
    canonical_json_bytes,
)
from graphify.workspace.identity import (
    IdentityError,
    SourceAmbiguousError,
    SourceDiscoveryError,
    SourceIdentity,
    discover_source,
)
from graphify.workspace.persistence import StateCorrupt, StatePathError
from graphify.workspace.pointers import (
    GenerationRead,
    PointerConflict,
    PointerError,
    PointerStore,
)
from graphify.workspace.registry import RegistryStore


OutputT = TypeVar("OutputT")
FreshnessHook = Callable[[str, Mapping[str, object]], None]
@dataclass(frozen=True)
class FreshnessResult(Generic[OutputT]):
    decision: str
    reason: str
    output: OutputT | None
    release: FreshnessRelease | None
    query_executed: bool


@dataclass(frozen=True)
class _AuthoritySnapshot:
    source: SourceIdentity
    active_source_revision: int
    entry_canonical: bytes


def _emit(hook: FreshnessHook | None, event: str, **details: object) -> None:
    if hook is not None:
        hook(event, details)


class FreshnessAuthority:
    """Release query output only after equal pre/post source observations."""

    def __init__(
        self,
        registry: RegistryStore,
        pointers: PointerStore,
        *,
        compatibility_manifest: CompatibilityManifest,
        max_inventory_passes: int = 6,
    ) -> None:
        # Tuple selection deliberately precedes every registry, pointer, source,
        # staging, or promotion access.
        compatibility = CompatibilityTuple.from_manifest(compatibility_manifest)
        selection = select_adapter(compatibility, intent=AdapterIntent.QUERY)
        self.adapter = selection.require_adapter()
        if max_inventory_passes < 2:
            raise ValueError("max_inventory_passes must be at least two")
        self.registry = registry
        self.pointers = pointers
        self.compatibility = compatibility
        self.compatibility_sha256 = compatibility_manifest.sha256
        self.max_inventory_passes = max_inventory_passes

    @staticmethod
    def _entry(document: Any, repo_uuid: str) -> dict[str, Any]:
        matches = [
            entry
            for entry in document.to_dict()["workspaces"]
            if entry["repo_uuid"] == repo_uuid
        ]
        if len(matches) != 1:
            raise SourceAmbiguousError(f"registry has no singular entry for {repo_uuid}")
        return cast(dict[str, Any], matches[0])

    def _authority_snapshot(self, document: Any, repo_uuid: str) -> _AuthoritySnapshot:
        entry = self._entry(document, repo_uuid)
        recorded = cast(dict[str, Any], entry["active_source"])
        active_source_revision = int(entry["active_source_revision"])
        entry_canonical = canonical_json_bytes(entry)
        try:
            source = discover_source(Path(str(recorded["path"])))
        except OSError as exc:
            raise SourceDiscoveryError(
                f"selected active source is unavailable: {recorded['path']}"
            ) from exc
        if source.repo_uuid != repo_uuid or source.registry_source != recorded:
            raise SourceAmbiguousError(
                "selected active source no longer matches the read-only registry snapshot"
            )
        return _AuthoritySnapshot(
            source=source,
            active_source_revision=active_source_revision,
            entry_canonical=entry_canonical,
        )

    def _observe(
        self,
        source: SourceIdentity,
        *,
        phase: str,
        deadline_ns: int | None,
        hook: FreshnessHook | None,
    ) -> SourceObservation:
        def adapter_hook(event: str, details: Mapping[str, object]) -> None:
            _emit(hook, f"freshness:{phase}:{event}", **dict(details))

        return self.adapter.observe(
            source.root,
            max_inventory_passes=self.max_inventory_passes,
            deadline_ns=deadline_ns,
            hook=adapter_hook if hook is not None else None,
        )

    @staticmethod
    def _observation_value(
        authority: _AuthoritySnapshot,
        reading: GenerationRead,
        observation: SourceObservation,
    ) -> dict[str, object]:
        pointer = reading.pointer.to_dict()
        receipt = reading.receipt.to_dict()
        payload = cast(dict[str, Any], receipt["sealed_query_payload"])
        return {
            "pointer_revision": pointer["pointer_revision"],
            "active_source_revision": authority.active_source_revision,
            "operation_epoch": pointer["operation_epoch"],
            "fence_token": pointer["fence_token"],
            "state_schema_version": pointer["state_schema_version"],
            "source_commit": observation.source_commit,
            "inventory_sha256": observation.inventory_sha256,
            "policy_sha256": observation.policy_sha256,
            "detector_id": observation.detector_id,
            "receipt_sha256": reading.receipt.sha256,
            "payload_manifest_sha256": payload["manifest_sha256"],
            "stable_inventory_passes": observation.stable_inventory_passes,
        }

    def _sealed_mismatch(
        self,
        authority: _AuthoritySnapshot,
        reading: GenerationRead,
        observation: SourceObservation,
    ) -> str | None:
        pointer = reading.pointer.to_dict()
        receipt = reading.receipt.to_dict()
        if int(pointer["state_schema_version"]) != self.compatibility.state_schema_version:
            return "unsupported"
        if receipt["compatibility_sha256"] != self.compatibility_sha256:
            return "unsupported"
        if receipt["semantic_completeness"] == "pending_rejected":
            return "unsupported"
        expected = (
            (int(pointer["active_source_revision"]), authority.active_source_revision),
            (int(receipt["active_source_revision"]), authority.active_source_revision),
            (str(receipt["source_commit"]), observation.source_commit),
            (str(receipt["observation_manifest_sha256"]), observation.inventory_sha256),
            (str(receipt["policy_sha256"]), observation.policy_sha256),
        )
        return "drift" if any(left != right for left, right in expected) else None

    @staticmethod
    def _release_document(
        pre: Mapping[str, object],
        post: Mapping[str, object],
        *,
        decision: str,
        reason: str,
    ) -> FreshnessRelease:
        return cast(
            FreshnessRelease,
            FreshnessRelease.from_mapping(
                {
                    "contract": "graphify.workspace.freshness_release",
                    "schema_version": 1,
                    "policy": "current_only",
                    "pre_observation": dict(pre),
                    "post_observation": dict(post),
                    "release_decision": decision,
                    "reason": reason,
                    "limitations": {
                        "strict_source_linearizability": False,
                        "inter_observation_aba_detection": False,
                        "post_boundary_changes": "out_of_scope",
                    },
                }
            ),
        )

    @staticmethod
    def _without_observation(
        reason: str,
        *,
        query_executed: bool = False,
    ) -> FreshnessResult[Any]:
        return FreshnessResult(
            decision="withhold",
            reason=reason,
            output=None,
            release=None,
            query_executed=query_executed,
        )

    def _run(
        self,
        repo_uuid: str,
        query: Callable[[Path], OutputT],
        *,
        timeout_ns: int | None = None,
        hook: FreshnessHook | None = None,
    ) -> FreshnessResult[OutputT]:
        if timeout_ns is not None and timeout_ns <= 0:
            return cast(FreshnessResult[OutputT], self._without_observation("timeout"))
        deadline_ns = None if timeout_ns is None else time.monotonic_ns() + timeout_ns
        query_executed = False
        try:
            # Registry rank precedes generation rank. Keeping this existing
            # shared lock open makes active-source selection stable for the
            # complete two-sided observation and query window.
            with self.registry.read_only_snapshot() as document:
                authority_pre = self._authority_snapshot(document, repo_uuid)
                with self.pointers.read_current(repo_uuid) as reading:
                    pre = self._observe(
                        authority_pre.source,
                        phase="pre",
                        deadline_ns=deadline_ns,
                        hook=hook,
                    )
                    pre_value = self._observation_value(authority_pre, reading, pre)
                    _emit(hook, "freshness:pre_observed")
                    mismatch = self._sealed_mismatch(authority_pre, reading, pre)
                    if mismatch is not None:
                        release = self._release_document(
                            pre_value,
                            pre_value,
                            decision="withhold",
                            reason=mismatch,
                        )
                        return FreshnessResult(
                            decision="withhold",
                            reason=mismatch,
                            output=None,
                            release=release,
                            query_executed=False,
                        )
                    _emit(hook, "freshness:before_query")
                    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                        release = self._release_document(
                            pre_value,
                            pre_value,
                            decision="withhold",
                            reason="timeout",
                        )
                        return FreshnessResult(
                            decision="withhold",
                            reason="timeout",
                            output=None,
                            release=release,
                            query_executed=False,
                        )
                    query_executed = True
                    output = query(reading.generation_path / "graphify-out")
                    _emit(hook, "freshness:after_query")
                    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                        release = self._release_document(
                            pre_value,
                            pre_value,
                            decision="withhold",
                            reason="timeout",
                        )
                        return FreshnessResult(
                            decision="withhold",
                            reason="timeout",
                            output=None,
                            release=release,
                            query_executed=True,
                        )
                    authority_post = self._authority_snapshot(document, repo_uuid)
                    post = self._observe(
                        authority_post.source,
                        phase="post",
                        deadline_ns=deadline_ns,
                        hook=hook,
                    )
                    _emit(hook, "freshness:post_observed")
                    authority_release = self._authority_snapshot(document, repo_uuid)
                    self.pointers.revalidate_read(repo_uuid, reading)
                    post_value = self._observation_value(authority_release, reading, post)
                    mismatch = self._sealed_mismatch(authority_release, reading, post)
                    if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                        release = self._release_document(
                            pre_value,
                            post_value,
                            decision="withhold",
                            reason="timeout",
                        )
                        return FreshnessResult(
                            decision="withhold",
                            reason="timeout",
                            output=None,
                            release=release,
                            query_executed=True,
                        )
                    if (
                        mismatch is None
                        and authority_pre.entry_canonical == authority_release.entry_canonical
                        and authority_pre.source == authority_release.source
                        and pre_value == post_value
                    ):
                        release = self._release_document(
                            pre_value,
                            post_value,
                            decision="release",
                            reason="observed_current",
                        )
                        _emit(hook, "freshness:release_boundary")
                        return FreshnessResult(
                            decision="release",
                            reason="observed_current",
                            output=output,
                            release=release,
                            query_executed=True,
                        )
                    reason = mismatch or "drift"
                    release = self._release_document(
                        pre_value,
                        post_value,
                        decision="withhold",
                        reason=reason,
                    )
                    return FreshnessResult(
                        decision="withhold",
                        reason=reason,
                        output=None,
                        release=release,
                        query_executed=True,
                    )
        except ObservationUnstable:
            return cast(
                FreshnessResult[OutputT],
                self._without_observation("unstable", query_executed=query_executed),
            )
        except ObservationTimeout:
            return cast(
                FreshnessResult[OutputT],
                self._without_observation("timeout", query_executed=query_executed),
            )
        except (ObservationUnavailable, SourceAmbiguousError, SourceDiscoveryError, IdentityError):
            return cast(
                FreshnessResult[OutputT],
                self._without_observation(
                    "source_unavailable",
                    query_executed=query_executed,
                ),
            )
        except (
            ObservationUnsupported,
            QueryRejected,
            UnsupportedCompatibility,
            StateCorrupt,
            StatePathError,
        ):
            return cast(
                FreshnessResult[OutputT],
                self._without_observation("unsupported", query_executed=query_executed),
            )
        except (PointerConflict, PointerError):
            return cast(
                FreshnessResult[OutputT],
                self._without_observation("drift", query_executed=query_executed),
            )

    def query(
        self,
        repo_uuid: str,
        request: QueryRequest,
        *,
        timeout_ns: int | None = None,
        hook: FreshnessHook | None = None,
    ) -> FreshnessResult[str]:
        """Execute the adapter's no-log native query inside the release protocol."""

        return self._run(
            repo_uuid,
            lambda payload_root: self.adapter.query_structural(payload_root, request),
            timeout_ns=timeout_ns,
            hook=hook,
        )


__all__ = ["FreshnessAuthority", "FreshnessHook", "FreshnessResult"]
