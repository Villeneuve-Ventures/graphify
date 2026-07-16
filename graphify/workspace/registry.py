"""Crash-durable workspace registry and operator-authorized identity mutations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, cast, Iterator, TYPE_CHECKING

from graphify.workspace.contracts import (
    Registry,
    WorkspaceLeaseState,
    canonical_json_bytes,
    canonical_sha256,
)
from graphify.workspace.identity import (
    IdentityAction,
    IdentityError,
    OperatorAuthorization,
    SourceAmbiguousError,
    SourceIdentity,
    UUIDCollisionError,
    discover_source,
    identity_evidence,
)
from graphify.workspace.persistence import (
    DurableStateRoot,
    FaultHook,
    REGISTRY_LOCK_RANK,
    RuntimeCapabilities,
    StateCorrupt,
    Syscalls,
    WORKSPACE_LOCK_RANK,
)

if TYPE_CHECKING:
    from datetime import datetime

    from graphify.workspace.leases import LeaseGrant, LeaseOwner, LeaseStore


class RegistryError(RuntimeError):
    """Base class for stable registry failures."""

    code = "registry_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class RevisionConflict(RegistryError):
    code = "revision_conflict"


@dataclass(frozen=True)
class ActivationResult:
    registry: Registry
    grant: "LeaseGrant"


def _source_key(source: dict[str, Any]) -> bytes:
    return canonical_json_bytes(source)


def _entry_for(document: Registry, repo_uuid: str) -> dict[str, Any]:
    matches = [
        entry for entry in document.to_dict()["workspaces"] if entry["repo_uuid"] == repo_uuid
    ]
    if len(matches) != 1:
        raise SourceAmbiguousError(f"registry has no singular entry for {repo_uuid}")
    return matches[0]


class RegistryStore:
    """Serialize registry mutations globally under a persistent ranked lock."""

    CURRENT = "registry.json"
    PREVIOUS = "registry.previous.json"
    PENDING = "registry.pending.json"
    LOCK = "registry.lock"

    def __init__(
        self,
        state_root: Path,
        *,
        capabilities: RuntimeCapabilities | None = None,
        fault_hook: FaultHook | None = None,
        syscalls: Syscalls | None = None,
    ) -> None:
        self.state = DurableStateRoot(
            state_root,
            capabilities=capabilities,
            fault_hook=fault_hook,
            syscalls=syscalls,
        )

    @contextmanager
    def exclusive_lock(self) -> Iterator[None]:
        with self.state.lock(
            self.LOCK,
            rank=REGISTRY_LOCK_RANK,
            name="registry",
        ):
            yield

    def _load_locked(self, *, allow_missing: bool = False) -> Registry | None:
        result = self.state.recover_record(
            label="registry",
            current=self.CURRENT,
            previous=self.PREVIOUS,
            pending=self.PENDING,
            decoder=self._decode_registry,
            revision=lambda document: int(document.to_dict()["revision"]),
            allow_missing=allow_missing,
        )
        return cast(Registry | None, result)

    def load(self) -> Registry:
        with self.exclusive_lock():
            document = self._load_locked()
        if document is None:  # pragma: no cover - narrowed by allow_missing=False
            raise StateCorrupt("registry current record is missing")
        return document

    def _read_current_unlocked(self) -> Registry:
        """Read one atomically installed current revision without recovery.

        Workspace operations use this only after a locked recovery snapshot and
        while holding their per-workspace lock. Registry writers install current
        with one atomic replacement; activation also needs that workspace lock.
        """

        document = cast(
            Registry | None,
            self.state.read_current(
                self.CURRENT,
                decoder=self._decode_registry,
                label="registry",
            ),
        )
        if document is None:  # pragma: no cover - allow_missing is false
            raise StateCorrupt("registry current record is missing")
        return document

    def _decode_registry(self, payload: bytes) -> Registry:
        document = cast(Registry, Registry.from_json(payload))
        self._validate_runtime_registry(document)
        return document

    def _check_revision(
        self,
        document: Registry | None,
        expected_revision: int | None,
    ) -> int:
        revision = 0 if document is None else int(document.to_dict()["revision"])
        if expected_revision is not None and expected_revision != revision:
            raise RevisionConflict(
                f"registry_revision expected {expected_revision}, found {revision}"
            )
        return revision

    def _persist_evidence(self, value: dict[str, Any]) -> str:
        payload = canonical_json_bytes(value)
        digest = hashlib.sha256(payload).hexdigest()
        self.state.write_once(Path("evidence") / f"{digest}.json", payload)
        return digest

    def _persist_source_evidence(self, source: SourceIdentity) -> None:
        for item in source.remote_evidence:
            digest = canonical_sha256(item)
            if not any(
                alias["evidence_sha256"] == digest
                for alias in source.registry_source["remote_aliases"]
            ):
                raise StateCorrupt("remote evidence digest does not match source record")
            self._persist_evidence(item)

    def read_evidence(self, digest: str) -> dict[str, Any]:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise StateCorrupt("evidence digest is not lowercase SHA-256")
        path = self.state.path(Path("evidence") / f"{digest}.json")
        try:
            payload = self.state.read_bytes(path.relative_to(self.state.root))
            value = json.loads(payload)
        except (OSError, ValueError) as exc:
            raise StateCorrupt(f"evidence {digest} is unreadable: {exc}") from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
            raise StateCorrupt(f"evidence {digest} is not canonical JSON")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise StateCorrupt(f"evidence {digest} does not match its content address")
        return value

    def _validate_identity_evidence(
        self,
        digest: str,
        *,
        repo_uuid: str,
        allowed_actions: set[str],
        maximum_registry_revision: int,
        bound_sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        evidence = self.read_evidence(digest)
        action = evidence.get("action")
        authorization = evidence.get("authorization")
        if (
            evidence.get("kind") != "graphify.workspace.identity_action"
            or action not in allowed_actions
            or evidence.get("repo_uuid") != repo_uuid
            or not isinstance(authorization, dict)
            or authorization.get("action") != action
            or evidence.get("source") not in bound_sources
        ):
            raise StateCorrupt(f"identity evidence {digest} is not bound to the registry entry")
        if set(authorization) != {"action", "issued_at", "nonce", "operator_id", "reason"}:
            raise StateCorrupt(f"identity evidence {digest} has incomplete authorization")
        if not all(
            isinstance(authorization[field], str)
            for field in ("issued_at", "nonce", "operator_id", "reason")
        ):
            raise StateCorrupt(f"identity evidence {digest} has invalid authorization fields")
        try:
            reconstructed = OperatorAuthorization(
                action=IdentityAction(str(action)),
                operator_id=authorization["operator_id"],
                reason=authorization["reason"],
                issued_at=authorization["issued_at"],
                nonce=authorization["nonce"],
            )
        except (ValueError, RuntimeError) as exc:
            raise StateCorrupt(f"identity evidence {digest} authorization is invalid") from exc
        if reconstructed.to_dict() != authorization:
            raise StateCorrupt(f"identity evidence {digest} authorization is not canonical")
        source = evidence["source"]
        if evidence.get("source_sha256") != canonical_sha256(source):
            raise StateCorrupt(f"identity evidence {digest} source hash does not match")
        for field in ("git_common_device", "git_common_inode"):
            identity_value = evidence.get(field)
            if (
                isinstance(identity_value, bool)
                or not isinstance(identity_value, int)
                or identity_value < 0
            ):
                raise StateCorrupt(f"identity evidence {digest} has an invalid {field}")
        registry_revision = evidence.get("registry_revision")
        if (
            isinstance(registry_revision, bool)
            or not isinstance(registry_revision, int)
            or not 1 <= registry_revision <= maximum_registry_revision
        ):
            raise StateCorrupt(f"identity evidence {digest} has an invalid registry revision")
        for field in ("active_source_revision", "operation_epoch", "fence_token"):
            value = evidence.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise StateCorrupt(f"identity evidence {digest} has an invalid {field}")
        return evidence

    def _validate_runtime_registry(self, document: Registry) -> None:
        value = document.to_dict()
        revision = int(value["revision"])
        workspaces = value["workspaces"]
        if [entry["repo_uuid"] for entry in workspaces] != sorted(
            entry["repo_uuid"] for entry in workspaces
        ):
            raise StateCorrupt("registry workspaces are not in canonical UUID order")
        source_paths: dict[str, str] = {}
        common_paths: dict[str, str] = {}
        common_identities: dict[tuple[int, int], str] = {}
        for entry in workspaces:
            repo_uuid = str(entry["repo_uuid"])
            active = entry["active_source"]
            aliases = entry["aliases"]
            alias_keys = [_source_key(alias) for alias in aliases]
            if alias_keys != sorted(alias_keys) or len(alias_keys) != len(set(alias_keys)):
                raise StateCorrupt(f"registry aliases are ambiguous for {repo_uuid}")
            if active in aliases:
                raise StateCorrupt(f"registry active source is also an alias for {repo_uuid}")
            bound_sources = [active, *aliases]
            entry_source_paths = {str(source["path"]) for source in bound_sources}
            entry_common_paths = {str(source["git_common_dir"]) for source in bound_sources}
            for path in entry_source_paths:
                prior_uuid = source_paths.setdefault(path, repo_uuid)
                if prior_uuid != repo_uuid:
                    raise StateCorrupt(f"source path is bound to multiple UUIDs: {path}")
            for path in entry_common_paths:
                prior_uuid = common_paths.setdefault(path, repo_uuid)
                if prior_uuid != repo_uuid:
                    raise StateCorrupt(f"Git common directory is bound to multiple UUIDs: {path}")
            for source in bound_sources:
                for remote in source["remote_aliases"]:
                    evidence = self.read_evidence(remote["evidence_sha256"])
                    if (
                        evidence.get("kind") != "graphify.workspace.remote_evidence"
                        or evidence.get("url") != remote["url"]
                    ):
                        raise StateCorrupt(
                            f"remote evidence is not bound to source {source['path']}"
                        )

            enrollment = entry["uuid_enrollment"]
            immutable = self._validate_identity_evidence(
                enrollment["immutable_evidence_sha256"],
                repo_uuid=repo_uuid,
                allowed_actions={"ENROLL"},
                maximum_registry_revision=revision,
                bound_sources=bound_sources,
            )
            current = self._validate_identity_evidence(
                enrollment["current_evidence_sha256"],
                repo_uuid=repo_uuid,
                allowed_actions={"ENROLL", "ADOPT", "REBIND", "ROTATE"},
                maximum_registry_revision=revision,
                bound_sources=bound_sources,
            )
            active_evidence = entry["active_source_evidence"]
            rebind = self._validate_identity_evidence(
                active_evidence["rebind_evidence_sha256"],
                repo_uuid=repo_uuid,
                allowed_actions={"ENROLL", "ACTIVATE"},
                maximum_registry_revision=revision,
                bound_sources=[active],
            )
            expected_active_hash = canonical_sha256(active)
            if (
                active_evidence["source_sha256"] != expected_active_hash
                or rebind["source_sha256"] != expected_active_hash
            ):
                raise StateCorrupt(f"active-source evidence is not source-bound for {repo_uuid}")
            for field in ("active_source_revision", "operation_epoch", "fence_token"):
                if rebind[field] != active_evidence[field]:
                    raise StateCorrupt(f"active-source evidence {field} is stale for {repo_uuid}")
            for evidence in (immutable, current, rebind):
                common_identity = (
                    int(evidence["git_common_device"]),
                    int(evidence["git_common_inode"]),
                )
                prior_uuid = common_identities.setdefault(common_identity, repo_uuid)
                if prior_uuid != repo_uuid:
                    raise StateCorrupt(
                        "Git common-directory identity is bound to multiple UUIDs: "
                        f"{common_identity}"
                    )

    def _commit_locked(self, value: dict[str, Any]) -> Registry:
        document = Registry.from_mapping(value)
        return cast(
            Registry,
            self.state.commit_record(
                label="registry",
                current=self.CURRENT,
                previous=self.PREVIOUS,
                pending=self.PENDING,
                payload=document.canonical,
                decoder=self._decode_registry,
            ),
        )

    @staticmethod
    def _normalized_entry(entry: dict[str, Any]) -> dict[str, Any]:
        active = entry["active_source"]
        unique = {_source_key(alias): alias for alias in entry["aliases"] if alias != active}
        entry["aliases"] = [unique[key] for key in sorted(unique)]
        return entry

    @staticmethod
    def _document_value(
        prior: Registry | None,
        revision: int,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del prior
        entries.sort(key=lambda item: item["repo_uuid"])
        return {
            "contract": "graphify.workspace.registry",
            "schema_version": 1,
            "revision": revision,
            "workspaces": entries,
        }

    def _authorized_evidence(
        self,
        source: SourceIdentity,
        authorization: OperatorAuthorization,
        *,
        registry_revision: int,
        active_source_revision: int,
        operation_epoch: int,
        fence_token: int,
    ) -> str:
        self._persist_source_evidence(source)
        return self._persist_evidence(
            {
                "kind": "graphify.workspace.identity_action",
                **identity_evidence(source, authorization),
                "registry_revision": registry_revision,
                "active_source_revision": active_source_revision,
                "operation_epoch": operation_epoch,
                "fence_token": fence_token,
            }
        )

    def _assert_source_identity_available(
        self,
        entries: list[dict[str, Any]],
        source: SourceIdentity,
    ) -> None:
        for entry in entries:
            bound_sources = [entry["active_source"], *entry["aliases"]]
            if any(
                item["path"] == str(source.root)
                or item["git_common_dir"] == source.registry_source["git_common_dir"]
                for item in bound_sources
            ):
                raise UUIDCollisionError(
                    "source or Git common directory is already enrolled under "
                    f"{entry['repo_uuid']}"
                )
            evidence_digests = {
                entry["uuid_enrollment"]["immutable_evidence_sha256"],
                entry["uuid_enrollment"]["current_evidence_sha256"],
                entry["active_source_evidence"]["rebind_evidence_sha256"],
            }
            for digest in evidence_digests:
                evidence = self.read_evidence(digest)
                if (
                    evidence.get("git_common_device") == source.git_common_device
                    and evidence.get("git_common_inode") == source.git_common_inode
                ):
                    raise UUIDCollisionError(
                        "Git common-directory identity is already enrolled under "
                        f"{entry['repo_uuid']}"
                    )

    def _initialize_workspace_state_locked(self, repo_uuid: str) -> None:
        directory = Path("workspaces") / WorkspaceLeaseState.canonical_repo_uuid(repo_uuid)
        current = directory / "workspace.json"
        previous = directory / "workspace.previous.json"
        pending = directory / "workspace.pending.json"
        initial = WorkspaceLeaseState(
            repo_uuid=repo_uuid,
            revision=1,
            fence_high_watermark=1,
            operation_epoch=1,
            migration_epoch=0,
            leases={},
            lease_epochs={},
        )
        with self.state.lock(
            directory / "workspace.lock",
            rank=WORKSPACE_LOCK_RANK,
            name="workspace",
        ):
            recovered = cast(
                WorkspaceLeaseState | None,
                self.state.recover_record(
                    label="workspace",
                    current=current,
                    previous=previous,
                    pending=pending,
                    decoder=WorkspaceLeaseState.from_json,
                    revision=lambda state: state.revision,
                    allow_missing=True,
                ),
            )
            if recovered is None:
                self.state.commit_record(
                    label="workspace",
                    current=current,
                    previous=previous,
                    pending=pending,
                    payload=initial.canonical,
                    decoder=WorkspaceLeaseState.from_json,
                )
            elif recovered.canonical != initial.canonical:
                raise StateCorrupt(
                    f"orphan workspace state for {repo_uuid} is not a fresh enrollment"
                )

    def enroll(
        self,
        source: SourceIdentity,
        authorization: OperatorAuthorization,
        *,
        expected_revision: int | None = None,
    ) -> Registry:
        authorization.require(IdentityAction.ENROLL)
        self.state.assert_external_to(source.root)
        with self.exclusive_lock():
            current = self._load_locked(allow_missing=True)
            revision = self._check_revision(current, expected_revision)
            entries = [] if current is None else current.to_dict()["workspaces"]
            if any(entry["repo_uuid"] == source.repo_uuid for entry in entries):
                raise UUIDCollisionError(
                    f"{source.repo_uuid} is already enrolled; use ADOPT or REBIND"
                )
            self._assert_source_identity_available(entries, source)
            evidence_digest = self._authorized_evidence(
                source,
                authorization,
                registry_revision=revision + 1,
                active_source_revision=1,
                operation_epoch=1,
                fence_token=1,
            )
            self._initialize_workspace_state_locked(source.repo_uuid)
            entries.append(
                {
                    "repo_uuid": source.repo_uuid,
                    "uuid_enrollment": {
                        "repo_uuid": source.repo_uuid,
                        "immutable_evidence_sha256": evidence_digest,
                        "current_evidence_sha256": evidence_digest,
                    },
                    "active_source_revision": 1,
                    "active_source": source.registry_source,
                    "active_source_evidence": {
                        "active_source_revision": 1,
                        "source_sha256": source.source_sha256,
                        "rebind_evidence_sha256": evidence_digest,
                        "operation_epoch": 1,
                        "fence_token": 1,
                    },
                    "aliases": [],
                }
            )
            return self._commit_locked(self._document_value(current, revision + 1, entries))

    def _related_to_enrollment(
        self,
        entry: dict[str, Any],
        source: SourceIdentity,
    ) -> bool:
        evidence = self.read_evidence(entry["uuid_enrollment"]["immutable_evidence_sha256"])
        same_common_dir = (
            evidence.get("git_common_device") == source.git_common_device
            and evidence.get("git_common_inode") == source.git_common_inode
        )
        prior_roots = set(evidence.get("history_roots", []))
        return same_common_dir or bool(prior_roots.intersection(source.history_roots))

    @staticmethod
    def _known_source(entry: dict[str, Any], source: SourceIdentity) -> bool:
        return source.registry_source == entry["active_source"] or any(
            source.registry_source == alias for alias in entry["aliases"]
        )

    def adopt(
        self,
        source: SourceIdentity,
        authorization: OperatorAuthorization,
        *,
        expected_revision: int | None = None,
    ) -> Registry:
        authorization.require(IdentityAction.ADOPT)
        self.state.assert_external_to(source.root)
        with self.exclusive_lock():
            current = self._load_locked()
            assert current is not None
            revision = self._check_revision(current, expected_revision)
            entries = current.to_dict()["workspaces"]
            entry = next(
                (item for item in entries if item["repo_uuid"] == source.repo_uuid),
                None,
            )
            if entry is None:
                raise UUIDCollisionError(f"{source.repo_uuid} has no enrollment to adopt")
            if self._known_source(entry, source):
                raise UUIDCollisionError("source is already bound")
            if not self._related_to_enrollment(entry, source):
                raise UUIDCollisionError("adoption requires shared history evidence")
            active_evidence = entry["active_source_evidence"]
            evidence_digest = self._authorized_evidence(
                source,
                authorization,
                registry_revision=revision + 1,
                active_source_revision=int(entry["active_source_revision"]),
                operation_epoch=int(active_evidence["operation_epoch"]),
                fence_token=int(active_evidence["fence_token"]),
            )
            entry["aliases"].append(source.registry_source)
            entry["uuid_enrollment"]["current_evidence_sha256"] = evidence_digest
            self._normalized_entry(entry)
            return self._commit_locked(self._document_value(current, revision + 1, entries))

    def rebind(
        self,
        source: SourceIdentity,
        authorization: OperatorAuthorization,
        *,
        expected_revision: int | None = None,
    ) -> Registry:
        authorization.require(IdentityAction.REBIND)
        self.state.assert_external_to(source.root)
        with self.exclusive_lock():
            current = self._load_locked()
            assert current is not None
            revision = self._check_revision(current, expected_revision)
            entries = current.to_dict()["workspaces"]
            entry = next(
                (item for item in entries if item["repo_uuid"] == source.repo_uuid),
                None,
            )
            if entry is None or not self._related_to_enrollment(entry, source):
                raise UUIDCollisionError(
                    "rebind requires the enrolled Git common directory or shared history evidence"
                )
            active_evidence = entry["active_source_evidence"]
            evidence_digest = self._authorized_evidence(
                source,
                authorization,
                registry_revision=revision + 1,
                active_source_revision=int(entry["active_source_revision"]),
                operation_epoch=int(active_evidence["operation_epoch"]),
                fence_token=int(active_evidence["fence_token"]),
            )
            if not self._known_source(entry, source):
                entry["aliases"].append(source.registry_source)
            entry["uuid_enrollment"]["current_evidence_sha256"] = evidence_digest
            self._normalized_entry(entry)
            return self._commit_locked(self._document_value(current, revision + 1, entries))

    def rotate_enrollment_evidence(
        self,
        source: SourceIdentity,
        authorization: OperatorAuthorization,
        *,
        expected_revision: int | None = None,
    ) -> Registry:
        authorization.require(IdentityAction.ROTATE)
        self.state.assert_external_to(source.root)
        with self.exclusive_lock():
            current = self._load_locked()
            assert current is not None
            revision = self._check_revision(current, expected_revision)
            entries = current.to_dict()["workspaces"]
            entry = next(
                (item for item in entries if item["repo_uuid"] == source.repo_uuid),
                None,
            )
            if entry is None or not self._known_source(entry, source):
                raise SourceAmbiguousError(
                    "evidence can rotate only for an explicitly bound source"
                )
            active_evidence = entry["active_source_evidence"]
            evidence_digest = self._authorized_evidence(
                source,
                authorization,
                registry_revision=revision + 1,
                active_source_revision=int(entry["active_source_revision"]),
                operation_epoch=int(active_evidence["operation_epoch"]),
                fence_token=int(active_evidence["fence_token"]),
            )
            entry["uuid_enrollment"]["current_evidence_sha256"] = evidence_digest
            return self._commit_locked(self._document_value(current, revision + 1, entries))

    def activate_source(
        self,
        source: SourceIdentity,
        authorization: OperatorAuthorization,
        *,
        leases: "LeaseStore",
        owner: "LeaseOwner",
        expected_registry_revision: int,
        expected_active_source_revision: int,
        expected_operation_epoch: int,
        expected_migration_epoch: int,
        acquired_at: "datetime",
        monotonic_ns: int,
        ttl_ns: int,
    ) -> ActivationResult:
        authorization.require(IdentityAction.ACTIVATE)
        self.state.assert_external_to(source.root)
        if leases.registry is not self:
            raise RegistryError("activation requires the LeaseStore bound to this registry")
        with self.exclusive_lock():
            current = self._load_locked()
            assert current is not None
            revision = self._check_revision(current, expected_registry_revision)
            entries = current.to_dict()["workspaces"]
            entry = next(
                (item for item in entries if item["repo_uuid"] == source.repo_uuid),
                None,
            )
            if entry is None or not self._known_source(entry, source):
                raise SourceAmbiguousError("activation target is not explicitly bound")
            actual_active_revision = int(entry["active_source_revision"])
            if actual_active_revision != expected_active_source_revision:
                raise RevisionConflict(
                    "active_source_revision expected "
                    f"{expected_active_source_revision}, found {actual_active_revision}"
                )
            grant = leases._acquire_under_registry_lock(
                current,
                source.repo_uuid,
                "ACTIVATE",
                owner,
                expected_registry_revision=expected_registry_revision,
                expected_active_source_revision=expected_active_source_revision,
                expected_operation_epoch=expected_operation_epoch,
                expected_migration_epoch=expected_migration_epoch,
                acquired_at=acquired_at,
                monotonic_ns=monotonic_ns,
                ttl_ns=ttl_ns,
                verify_active=False,
            )
            evidence_digest = self._authorized_evidence(
                source,
                authorization,
                registry_revision=revision + 1,
                active_source_revision=actual_active_revision + 1,
                operation_epoch=grant.operation_epoch,
                fence_token=int(grant.lease.to_dict()["fence_token"]),
            )
            prior_active = entry["active_source"]
            entry["active_source_revision"] = actual_active_revision + 1
            entry["active_source"] = source.registry_source
            entry["active_source_evidence"] = {
                "active_source_revision": actual_active_revision + 1,
                "source_sha256": source.source_sha256,
                "rebind_evidence_sha256": evidence_digest,
                "operation_epoch": grant.operation_epoch,
                "fence_token": grant.lease.to_dict()["fence_token"],
            }
            entry["aliases"].append(prior_active)
            self._normalized_entry(entry)
            committed = self._commit_locked(self._document_value(current, revision + 1, entries))
            leases._release_under_registry_lock(grant, committed, validate_active=False)
            return ActivationResult(registry=committed, grant=grant)

    def resolve_active_source(self, repo_uuid: str) -> SourceIdentity:
        document = self.load()
        entry = _entry_for(document, repo_uuid)
        recorded = entry["active_source"]
        try:
            source = discover_source(Path(recorded["path"]))
        except (OSError, IdentityError) as exc:
            raise SourceAmbiguousError(f"selected active source is unavailable: {exc}") from exc
        if source.repo_uuid != repo_uuid or source.registry_source != recorded:
            raise SourceAmbiguousError("selected active source no longer matches registry evidence")
        return source


__all__ = [
    "ActivationResult",
    "RegistryError",
    "RegistryStore",
    "RevisionConflict",
]
