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

P5A does not revise that frozen receipt. Every new certification requires a
semantic queue authority and derives the queue watermark and semantic-
completeness value from a stable internal queue view. An empty desired set is
represented by an exact durable `not_required` reconciliation at a positive
watermark; queue absence or scalar watermark zero is not new-certification
authority. Historical queue-zero receipts remain readable. The captured queue
revision and canonical-state hash are revalidated under the workspace lock
before the generation lock is taken. Certification then installs an immutable,
store-owned binding of generation ID, request digest, queue view, and sealed
staged-input manifest before sealing any receipt. That binding is the queue-
authority recovery boundary even if later work advances the current queue; a
caller-preseeded staged receipt cannot replace it. A durable staged or installed
receipt remains the idempotent recovery boundary for generation installation
and the journal after the binding exists.

Every payload entry is a regular file with path, size, SHA-256, and allowed
mode. The v1 root is exactly `graphify-out`, and every entry path must be a
strict descendant of that root. Extra files, links, special files, duplicate
paths, sibling paths, root-only entries, and path escapes are invalid. P3
implements durable validation and sealing around caller-supplied staged
payloads; it does not invoke Graphify extraction or query logic.

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

## Semantic desired-work queue

`graphify.workspace.semantic_queue.internal` format version 1 is one canonical
per-workspace durable record. It contains the workspace UUID, active-source
revision that produced the desired work, record revision, desired and completed
watermarks, compaction epoch, last-served operation, explicit queue policy,
exact reconciliation evidence, and deterministically sorted queue items. The
explicit policy has item, byte, and retry bounds; no capacity or provider default
is inferred from the environment. Activation makes retained desired work
ineligible until a newer exact reconciliation binds it to the new source revision.
Capability decisions are advisory reports, not claim authority. The claim
mutation boundary resolves the registry-selected active source, safely reads
and validates its workspace configuration, and requires the canonically
revalidated caller configuration to match it exactly. Only then does it derive
availability from explicit live host-agent and named-backend inputs; a matching
repository UUID alone is not policy authority. Claim resolves source identity
before locks, then retains the current registry lock through the workspace
mutation and rechecks that identity plus a second safe config read after checkout
verification. Checkpoint,
completion, failure, and compaction under a semantic grant retain the same
registry-through-workspace boundary, so activation cannot advance the active
source during any semantic mutation.

Each desired-work identity binds a positive source epoch, policy SHA-256,
`UPSERT` or `DELETE`, canonical contained relative path, content SHA-256, and
positive desired revision. Coalescing is deterministic by source epoch, policy,
operation, and path. A newer desired revision replaces older work for that key;
an exact retry of the current item is idempotent, as is an exact retry of
compacted completed work retained by the reconciliation proof. Every mutation
preflights both item and canonical-byte limits before committing, so capacity
failure leaves the stable record unchanged.

One exact reconciliation binds the current desired watermark to the source
epoch, policy hash, two equal typed `SourceObservation` summaries, semantic-
required bit, sorted desired set, and desired-set hash. Each typed summary binds
the source commit, inventory and policy hashes, detector, its adapter-proved two
stable inventory passes, and the observed entries digest. The pair has its own
canonical evidence hash. Queue emptiness or a caller-supplied pass count is never
certification evidence.

After semantic completion, a separate durable transition binds that exact
reconciliation and watermark to one sealed staged-payload manifest digest.
Rebinding the same watermark to different staged bytes fails closed. Semantic
certification requires the observation pair, durable staged-input binding, and
a completed watermark equal to the desired watermark with no retained
incomplete item. Compaction may remove completed item tombstones only after that
equality holds; it retains the watermarks, reconciliation, observation pair, and
staged-input binding.

A claimed item additionally binds the workspace UUID and exact desired work to
the `SEMANTIC_CLAIM` owner, fence token, operation epoch, migration epoch,
active-source revision, positive attempt number, deterministic claim ID, and
optional bounded checkpoint. A failed or expired attempt increments durable
failure state so a retry under the same lease has a different attempt and claim
ID. One
semantic lease owns at most one active claim. Stale or expired claims cannot
checkpoint, complete, fail, or overwrite a newer desired revision. Successor
claim recovery increments the failure count and either retries or dead-letters
according to the explicit budget. Non-retryable or exhausted work is durable
dead-letter state and prevents completion of its reconciled watermark.

The queue record uses the existing durable current/previous/pending commit and
recovery protocol at `workspaces/<repo_uuid>/queue/semantic*.jsonl`. Malformed,
noncanonical, cross-workspace, policy-mismatched, or ambiguous state fails
closed. Read-only inspection takes shared registry and workspace locks and does
not create missing queue paths.

Each new certification additionally installs one canonical immutable internal
record at
`workspaces/<repo_uuid>/queue/certifications/<generation_id>.json`. It binds the
workspace and generation identities, the full certification-request digest,
and the stable queue view, including its revision, canonical-state hash, typed
observation evidence, watermarks, completeness, and sealed-input manifest. An
exact retry may reuse that record after queue advancement; any request or
payload conflict fails closed. The record is installed under the workspace lock
after request-contract and staged-manifest validation plus queue revalidation,
and before the generation lock or receipt. Rejected malformed input creates no
binding, so a corrected retry of the reserved generation remains possible.
P5A certification requests and receipts carry the schema-valid
`stable_semantic_queue` validation marker. Verification requires the immutable
binding only when that marker is present; pre-P5A receipts without it, including
legacy positive-watermark receipts permitted by the frozen schema, remain
readable without being retroactively treated as P5A authority.

`graphify.workspace.pointer_set` atomically represents current, verified
last-good, pointer revision, source/operation/schema epochs, and the distinct
accepted fence token used by a future compare-and-swap.
`graphify.workspace.prior_pointer` is a copied retained predecessor; its
replacement revision must be strictly greater. P3 implements one
same-filesystem atomic visible-pointer replacement and monotonic repair.

`graphify.workspace.generation_coordination_lock` freezes a small lock identity
installed before certification and retained in v1. Query will open it read-only
and take a kernel shared advisory lock. Offline GC will take the exclusive
counterpart and recheck reachability. P3 installs each lock identity durably
before `CERTIFIED` and retains it across quarantine and purge.

## Freshness release

`graphify.workspace.freshness_release` records complete pre-query and post-query
observations plus the release/withhold decision. A release is valid only for
`observed_current`. Both observations bind pointer, active-source,
operation/schema, accepted fence token, source commit/inventory, policy,
detector, receipt, and payload hashes and require two stable inventory passes.

P5A consumes a separate exact observation-manifest digest and an explicit count
of two stable passes when producing a semantic certification view. It does not
change or weaken the frozen freshness-release schema.

This is an observation-based contract. It does not claim an atomic whole-tree
snapshot, strict source linearizability against non-cooperative writers,
detection of an ABA edit wholly between observations, or coverage of changes
after completion of the post-query source observation. That completion is the
source release-observation boundary; later registry, pointer, receipt, and
release checks do not extend source linearizability.
