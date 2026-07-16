# State contracts

All hashed JSON uses UTF-8, NFC-normalized strings, lexicographically sorted
object keys, compact separators, a final newline, and no floating-point values.
Paths inside payload manifests are normalized, non-escaping POSIX relative
paths. Payload arrays are sorted by path and contain unique regular files only.

## Repo config

`graphify.workspace.config` represents `.graphify/workspace.toml`. The UUID is
a stable label whose first enrollment and any adoption/rebind require separate
operator evidence in P2. The only v1 freshness policy is `current_only`.
Repo policy cannot override the state root, registry, pointer, or generation
layout.

## Registry and active source

`graphify.workspace.registry` has a monotonic registry revision. Each workspace
record has one required `active_source`, a monotonic
`active_source_revision`, immutable/current UUID-enrollment evidence, and
active-source evidence bound to both the revision and exact canonical active
source hash. Alias rebind authorization is recorded as the current identity
evidence without replacing active-source evidence. The active evidence also records the distinct positive operation epoch and
accepted fence token for the audited activation CAS; a registry commit with
missing, zero, or revision-stale activation evidence is invalid. Every source
records one or more normalized HTTPS/SSH remote aliases in canonical URL order,
with a SHA-256 evidence receipt for each. Workspace source paths, worktree
coordinates, and remotes are discovery aliases; `repo_uuid` is the authoritative
identity. The explicit singular field prevents a consumer from treating
multiple aliases as current. P2 serializes registry revisions globally, stores
content-addressed authorization and remote evidence externally, and requires
an expected registry revision plus expected active-source revision for
activation. Missing or mismatched active-source evidence fails closed; aliases
are never used as an implicit fallback.

## Receipt and sealed payload

`graphify.workspace.generation_receipt` represents only a `CERTIFIED`
generation. It binds repo/source/operation epochs, the accepted fence token,
source commit, policy and observation hashes, queue watermark, semantic
completeness, compatibility, the pre-created coordination lock, validation
names, and every query-visible payload byte. `operation_epoch` and
`fence_token` remain distinct: the former identifies the operation generation,
while the latter proves acceptance under the fencing domain. The external
journal and mutable pointers are excluded from the sealed query payload. After
certification, promotion, supersession, rollback, and repair may change only
external journal/pointer records.

Every payload entry is a regular file with path, size, SHA-256, and allowed
mode. The v1 root is exactly `graphify-out`, and every entry path must be a
strict descendant of that root. Extra files, links, special files, duplicate
paths, sibling paths, root-only entries, and path escapes are invalid. P3 will
implement durable validation and sealing.

## Journal event and frame

`graphify.workspace.journal_event` binds a sequence, lifecycle transition,
generation, previous-event hash, operation epoch, accepted fence token, and
(once a sealed receipt exists) the receipt hash and observed pointer revision.
`CERTIFIED` may record revision zero only as the no-current-pointer sentinel
before first promotion. `PROMOTED`, `SUPERSEDED`, `REPAIRED`, and `ROLLED_BACK`
require a positive installed or reconciled pointer revision.
Events are outside certified generations.
`ALLOCATED`, `STAGING`, `BUILT`, `VALIDATING`, and `FAILED` are
pre-certification transitions and therefore require null receipt and pointer
references. `CERTIFIED`, `PROMOTED`, `SUPERSEDED`, `REPAIRED`, and
`ROLLED_BACK` require both references. Only sequence 1 has a null previous-event
hash; every later event must name its predecessor. P1 freezes the rollback
journal record only; it does not implement a rollback operation.

The v1 reference frame is:

```text
magic "GWF1" | frame version u8 | payload length u64be |
SHA-256(payload) 32 bytes | canonical JSON payload
```

The frame detects truncation and substitution. Hash links make history
corruption-evident; v1 does not claim authenticity against a malicious same-UID
actor who can replace both state and trust anchors.

## Lease, pointer, and coordination lock

`graphify.workspace.fenced_lease` separates a crash-durable monotonic fence
token (safety) from boot/process/heartbeat/deadline data (liveness). Wall time
is never a safety primitive. The frozen shared operation identities include
activation and pointer recovery as well as build, migration, promotion,
rollback, repair, GC, and semantic claim work.

P2 durably initializes the fence floor during enrollment, then persists the
fence high-water mark, global operation allocator, migration epoch, current
lease records, and each live domain's accepted operation epoch under the
workspace. Missing initialized records fail closed rather than recreating the
floor. Allocation, heartbeat, acceptance, release, and inspection acquire the
registry lock, recover one stable registry snapshot, and keep that lock while
nesting the workspace lock. They never replace recovery with a current-only
re-read; activation uses the same order through its already-held registry-lock
path. Fence values advance before ownership is accepted, survive recovery and
clean reboot, and never reset through release or expiry. An expired lease, a
stale fence, a runtime owner/boot/process mismatch, a changed active-source
revision, an advanced operation epoch in the same lease domain, or an advanced
migration epoch cannot authorize a later commit. Runtime owner identity comes
from OS-owned boot and process-start facts rather than caller assertion. Wall
timestamps remain audit/liveness metadata; monotonic deadlines are the only
expiry input.
Release is cleanup rather than commit acceptance: the trusted current runtime
may remove only the exact current owner/fence record even after a source,
operation, or migration epoch invalidates that lease's commit authority.

`graphify.workspace.pointer_set` atomically represents current, verified
last-good, pointer revision, source/operation/schema epochs, and the distinct
accepted fence token used by a future compare-and-swap.
`graphify.workspace.prior_pointer` is a copied retained predecessor; its
replacement revision must be strictly greater. P3 will implement the one
same-filesystem atomic visible-pointer replacement and recovery semantics.

`graphify.workspace.generation_coordination_lock` freezes a small lock identity
installed before certification and retained in v1. Query will open it read-only
and take a kernel shared advisory lock. Offline GC will take the exclusive
counterpart and recheck reachability. P1 creates no lock files.

## Freshness release

`graphify.workspace.freshness_release` records complete pre-query and post-query
observations plus the release/withhold decision. A release is valid only for
`observed_current`. Both observations bind pointer, active-source,
operation/schema, accepted fence token, source commit/inventory, policy,
detector, receipt, and payload hashes and require two stable inventory passes.

This is an observation-based contract. It does not claim an atomic whole-tree
snapshot, strict source linearizability against non-cooperative writers,
detection of an ABA edit wholly between observations, or coverage of changes
after the documented release boundary.
