# State contracts

All hashed JSON uses UTF-8, NFC-normalized strings, lexicographically sorted
object keys, compact separators, and a final newline. Binary floating-point
values are forbidden. The only v1 non-integer JSON numbers are the exact bounded
fixed-point semantic `confidence_score` and `weight` slots frozen in
[`semantic-sync.md`](semantic-sync.md); every other hashed numeric field is an
integer.
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

## Public pointer-repair transport

The public repair forms introduce no durable document and do not change durable
state schema v1. Their four CLI-v1 schemas freeze a 16 KiB canonical request
and canonical redacted result for preview and execute. A preview request names
the workspace and expected registry, active-source, operation, and migration
authority; it intentionally has no caller-selected pointer revision or repair
candidate. A preview result contains the request digest, observed authority,
classification, and a bounded deterministic decision: candidate and last-good
references, selected source, prospective revision/action, projected journal
actions, a redacted digest binding the exact underlying pointer/journal/
generation evidence, and sorted quarantine IDs. It is the approval object, and its exact
canonical bytes including the required final newline are SHA-256 input for an
execute request.

An execute request adds only that approved-preview digest and a five-field
`OperatorAuthorization` whose action is `REPAIR_EXECUTE`. It has no durable
completion record: a fresh accepted `REPAIR` lease advances the shared
workspace-operation epoch. The execute implementation must recompute the same
decision under the existing mutation locks before it calls the pointer store;
this applies to `no_op` as well as `repairable`. Any authority, preview, plan,
or lease mismatch fails before pointer/journal mutation. A completed or
commit-unknown request is therefore never a license to replay the same request.
The recovery path is status inspection followed by a fresh preview and execute
request.

Repair may use only existing pointer, prior-pointer, pending-pointer, journal,
and generation evidence. It may replace or finalize a pointer and append or
recover the corresponding journal evidence through `PointerStore`; it may
quarantine only a corrupt generation absent from the repaired pointer set. It
does not create repair-specific state, repair registry/lease/path/staged/queue
state, reconcile GC intent, heal committed-journal corruption, repair arbitrary
generations reported by GC or rollback, or alter a sealed generation receipt or
payload.

## Staged structural-build recovery

`graphify.workspace.staged_build.internal` format version 1 is the bounded,
crash-durable publication record at
`workspaces/<repo_uuid>/staged-build.json`. Its stable current, previous, and
pending files use the existing commit-and-recovery protocol, and every read or
write is limited to 64 KiB. One record binds one generation to the exact
structural request, including registry/source/operation/migration/pointer CAS,
capacity and compatibility hashes, source epoch, and a frozen observation
summary. The summary retains the detector identity and observed-entry digest
needed to reconstruct the two-equal-observation evidence after restart; its
stored evidence hash must match that reconstructed pair.

The forward lifecycle is `REQUESTED` to `PUBLISHING` to `COMPLETE` to
`CERTIFIED` to `PROMOTED`. Exact retries may recover a durable boundary without
duplicating its revision. A nonterminal record is a recovery barrier: ordinary
workspace mutations cannot acquire around it, while request-bound `BUILD`,
`PROMOTE`, or `POINTER_RECOVERY` may resume only the operation allowed by the
current lifecycle. Each live staged lease additionally stores the caller's
attempt SHA-256 in the internal workspace lease envelope. Only that exact
attempt may recover a commit-unknown live lease; process-owner equality alone
does not let a second caller share the fence. Release removes the attempt
binding.

`ABANDONED` is the only terminal close that does not publish the staged bytes.
It requires canonical evidence that either the exact request is stale because
the active source, migration epoch, pointer CAS, compatibility, semantic source
epoch, or trusted source observation changed, or the staged payload inventory
exceeds the request's immutable byte reservation. Capacity-failure evidence
binds the observed payload byte count to the exact request. A durable
abandonment intent precedes cleanup so a crash can finish the same close under a
successor fence. The frozen request observation is sufficient when an
earlier-priority durable authority already proves staleness, including when the
selected source is no longer available. Source-only abandonment still requires
two fresh trusted observations and fails closed when they cannot be obtained.
Transient capacity rejection before publication retains the exact nonterminal
request for retry; only a completed oversized staged inventory is terminal.

P5B2b exposes the sole public mutation command
`graphify workspace sync --code-only --request-stdin`. Its canonical request is
limited to 16 KiB and explicitly binds workspace and generation identity,
registry and active-source revisions, operation and migration epochs, pointer
revision and current receipt, source epoch, semantic desired watermark,
expected payload bytes, and all five capacity-policy bounds. No value is
defaulted from state or ambient environment. The canonical request digest is
the staged logical-request identity. Success emits the canonical CLI-v1
`graphify.workspace.sync` receipt containing only the workspace, generation,
request, certified receipt, and pointer revision; exact terminal replay emits
identical bytes. Conflict and invalid outcomes use stable redacted reason/action
codes and exit 10 or 20.

Status and doctor emit public status schema v2. Its required `staged_build`
summary reports presence, blocking state, record revision, generation,
lifecycle, logical request digest, and internal request digest. `REQUESTED`,
`PUBLISHING`, `COMPLETE`, and `CERTIFIED` force `safe_to_query=false` with
`staged_build_recovery_required` / `resume_exact_workspace_sync`. `PROMOTED`
and `ABANDONED` are terminal and do not create a false barrier. Corrupt or
contradictory staged authority fails closed. Inspection uses only existing
locks and existing-only reads; it never creates, repairs, recovers, cleans, or
commits state.

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
before locks under one five-second monotonic deadline and a 64 KiB configuration
byte limit, then retains the current registry lock through the workspace mutation
and rechecks that identity plus a second bounded config read after checkout
verification. Checkpoint,
completion, failure, and compaction under a semantic grant retain the same
registry-through-workspace boundary, so activation cannot advance the active
source during any semantic mutation.

Each desired-work identity binds a positive source epoch, policy SHA-256,
`UPSERT` or `DELETE`, canonical contained relative path, content SHA-256, and
positive desired revision. Coalescing is deterministic by source epoch, policy,
operation, and path. A newer desired revision replaces older work for that key;
one exact desired set cannot assign both operations to the same path and desired
revision. An exact retry of the current item is idempotent, as is an exact retry of
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
staged-input binding. A later same-source reconciliation recovers only exact
desired-work identities from that retained completed reconciliation as completed;
new or changed identities remain pending, and unfinished lower predecessors still
invalidate carried completion.

A claimed item additionally binds the workspace UUID and exact desired work to
the `SEMANTIC_CLAIM` owner, fence token, operation epoch, migration epoch,
active-source revision, positive attempt number, deterministic claim ID, and
optional bounded checkpoint. A failed or expired attempt increments durable
failure state so a retry under the same lease has a different attempt and claim
ID. One
semantic lease owns at most one active claim. Stale or expired claims cannot
checkpoint, complete, fail, or overwrite a newer desired revision. Successor
claim recovery increments the failure count and either retries or dead-letters
according to the explicit budget. Retryability is accepted only as an actual
Boolean. Non-retryable or exhausted work is durable dead-letter state and
prevents completion of its reconciled watermark.

The queue record uses the existing durable current/previous/pending commit and
recovery protocol at `workspaces/<repo_uuid>/queue/semantic*.jsonl`. Malformed,
noncanonical, cross-workspace, policy-mismatched, or ambiguous state fails
closed. Read-only inspection takes the shared registry lock and the pre-existing
exclusive workspace lock; it does not create missing queue paths.

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

## Accepted host-agent result staging

The accepted host-agent semantic-worker transport is frozen in
[`semantic-sync.md`](semantic-sync.md) and bound to the
[P5B2 semantic-worker receipt](receipts/p5b2-semantic-worker.md). The accepted
staging does not revise `graphify.workspace.semantic_queue.internal` or any
public durable schema. The
exact command is `graphify workspace semantic-worker --stdio`, and one process
must retain the same OS-derived `SEMANTIC_CLAIM` owner from claim through
optional checkpoints, the terminal request and queue transition, and release.

The single request/result families are
`graphify.workspace.semantic_worker_request` version 1 and
`graphify.workspace.semantic_worker_result` version 1. A canonical begin frame
binds explicit registry, active-source, operation, migration, queue, and
watermark CAS plus an absolute deadline, `executor="host_agent"`, and the
Boolean `host_agent_active=true`. There is no backend, provider, network,
credential, model, endpoint, or fallback field.

Validated successful output is staged only at the derived external path
`workspaces/<repo_uuid>/semantic-staging/<begin_request_sha256>/result.json`.
The file is one immutable canonical
`graphify.workspace.semantic_result_binding.internal` format-version-1
envelope with exactly the closed field set and object grammar frozen in
[`semantic-sync.md`](semantic-sync.md#result-validation-and-binding). It binds
the begin-request digest, repository UUID, claim ID, attempt, exact desired work
and its canonical digest, active-source revision, operation and migration
epochs, and the canonical payload object, byte count, and SHA-256. An `UPSERT`
payload is exactly the whole
`{"kind":"semantic_fragment","fragment":SANITIZED_FRAGMENT}` object after
worker-specific closed nested-schema, lossless exact-decimal helper encoding,
fixed-point, pairwise-distinct hyperedge-member, and bounded indexed-sanitizer
validation. A `DELETE` payload is
exactly the kind-only
`{"kind":"delete_tombstone"}` object. The payload byte count and digest cover
that whole canonical object including its final newline, and the envelope stores
the same object once. Existing no-follow
install-once semantics apply: exact same bytes are idempotent, different bytes
at the same derived path are a conflict, and a reopened regular `0600` file must
match before it can be referenced. Under a provably current claim, an exact
different-byte conflict is the non-retryable
`semantic_result_binding_conflict=false` failure. One queue failure transition
dead-letters the item; unreadable or ambiguous staging state is commit-unknown.

Claim admission and every later queue mutation preserve exact canonical-byte
headroom for the mandatory `result:<64-lowercase-hex>` checkpoint by projecting
that value into the live claim before applying `max_bytes`. This is capacity
accounting, not a new durable reservation field. Admission that cannot preserve
the headroom installs no current-session claim and returns
`semantic_checkpoint_capacity_unavailable` / `inspect_semantic_queue`; existing
deterministic predecessor `claim_expired` recovery remains attributable to the
predecessor attempt.

Queue completion requires the live claim to persist
`result:<result_binding_sha256>` in its existing bounded checkpoint, reopen and
rehash the envelope, and require that reopened SHA-256 to equal the checkpointed
`result_binding_sha256`. The envelope's `begin_request_sha256` must equal the
captured digest of the accepted canonical begin frame, and its `repo_uuid` must
equal that request field. Its `claim_id`, `attempt`, and exact desired work must
equal the live claim. The same source revision, owner, fence, operation epoch,
and migration epoch must be revalidated, followed by one final no-follow source
reopen that proves the claimed `UPSERT` content digest or `DELETE` absence
immediately before `complete()`. After the completion return is observed, the
exact semantic lease must be released and that owner/fence proved absent before
the `completed` frame is emitted. A complete frame, an installed envelope, a
checkpoint, or a queue completion without that released-lease proof is not
public completion authority. The terminal result contains digests and queue
watermarks, never the fragment, source content, private paths, owner/fence data,
secrets, or exception text.

Before every optional progress or mandatory result checkpoint, the worker
retains the exact live claim and prior checkpoint. Post-commit uncertainty
adopts only the same live claim with the requested value, retries only from the
exact retained pre-call claim while both deadlines remain, and otherwise is
commit-unknown. Optional public checkpoint codes cannot use the reserved
`result:` prefix.

The staging file is neither a queue record nor a generation payload,
certification binding, or completion index. A successor claim ignores an older
session directory, and this first child performs no cleanup. Because the
existing `complete()` transition clears the claim/checkpoint and stores no
result digest, uncertainty after completion begins or during its pre-terminal
lease release cannot be recovered as a successful public receipt without a
later durable-schema decision. It remains `commit_unknown`; manual durable-state
inspection is required, and downstream semantic sync must not consume the staged
result without the exact exit-0 terminal receipt. This direct session outcome
adds no status reason or action.

A completed source observation proving different content or presence is
`source_content_changed=false`; an incomplete observation is
`source_unavailable=true`. Pre-mutation registry or lease-state corruption uses
the existing `registry_invalid` or `workspace_state_invalid` route; ambiguity
after a possible mutation remains commit-unknown. Result output uses
deadline-aware write-all plus flush. Every frame has a five-second delivery
deadline; `work` and `checkpointed` use the earlier absolute work deadline, and
terminal delivery grants no mutation or lease authority. Partial bytes are not
a frame. Work-deadline expiry while emitting `work` or `checkpointed` closes a
live claim through `host_agent_timeout`; delivery-deadline expiry while work
time remains, or another delivery failure, uses `host_agent_interrupted`. A
lost `completed` terminal is not public completion authority.

The worker stops before `bind_sealed_inputs()`, generation staging completion,
certification, promotion, pointer mutation, and full semantic sync. The accepted
implementation and evidence are limited to the exact transport bound by the
[P5B2 semantic-worker receipt](receipts/p5b2-semantic-worker.md); no excluded
successor authority is added.

## Semantic-result handoff and sealed-input finalization

The accepted unnumbered P5B2 child adds one internal immutable record, not a
public schema, runtime receipt, queue-format revision, or staged-build-format
revision. Its implementation and completion evidence are bound by the
[P5B2 semantic-result handoff receipt](receipts/p5b2-semantic-result-handoff.md).
Its exact contract is
`graphify.workspace.semantic_result_handoff.internal`, format version 1, at:

```text
workspaces/<repo_uuid>/semantic-staging/handoffs/
  <target_generation_id>/<structural_request_sha256>.json
```

The path is derived from the canonical new target generation identity in the
existing `SyncRequest` and the SHA-256 of the complete canonical
`StructuralBuildRequest`. The sync-request digest must equal the structural
request's `logical_request_sha256`. The path is reached by descriptor-relative
contained no-follow traversal through `0700` directories and is one single-link
regular `0600` file. Same canonical bytes are idempotent; different bytes at the
same path conflict. Reads are bounded first by the request's positive
`expected_payload_bytes`; no ambient or implicit limit may increase that value.

`GenerationStore` remains the shared-capacity owner. Its trusted usage scan for
this preflight and every later allocation must include every retained canonical
handoff file, adding its exact bytes to the usage key formed by the repository
and target generation. A target present only through a handoff consumes one
generation slot; if the same target is also in staging, generation, or quarantine
storage, their bytes are summed under one slot. Exact replay does not add a
second slot or second copy of already visible bytes. Unsafe, unreadable,
overflowing, or repeatedly changing handoff usage makes capacity uncertain and
fails closed. This accounting remains until separately authorized cleanup or GC
removes the retained handoff.

The record uses the semantic worker's exact canonical JSON encoder so the two
accepted fixed-point slots remain unquoted exact decimal tokens. Its top-level
fields are exactly `contract`, `format_version`, `repo_uuid`,
`target_generation_id`, `carried_source_generation_id`, `structural_request`,
`structural_request_sha256`, `queue`, `results`, and `materialized`.

Format version 1 is closed: the request compatibility digest must equal the
current existing `GenerationStore` compatibility digest, the accepted worker
objects must retain their version-1 grammar, and unknown contracts, fields,
versions, encoders, or compatibility digests are rejected. No in-place rewrite
or legacy migration is part of this child.

- `target_generation_id` is the exact `SyncRequest.generation_id`; the same
  value is passed unchanged through staged request, request-bound acquisition,
  allocation, staging, completion, and replay. It must not equal the current
  certified source. On first handoff installation it must not identify any
  existing staging or certified generation. Once the exact handoff exists,
  target staging is admissible only through the exact matching request-bound
  `REQUESTED`, `PUBLISHING`, or `COMPLETE` recovery lifecycle; a certified target
  or any other existing target state conflicts.
- `carried_source_generation_id` is null when every result is fresh. Otherwise
  it is the distinct current certified generation selected by the structural
  request's pointer/receipt CAS. Its receipt digest equals
  `expected_current_receipt_sha256`, every carried result originates in its
  verified semantic-input file, and source/target equality or exchange is
  invalid.
- `structural_request` is the complete existing request and binds registry,
  active-source, operation and migration epochs, pointer CAS, source commit and
  epoch, policy and observation evidence, expected payload bytes, capacity
  policy, compatibility, and logical request identity. The outer record
  supplies the repository plus distinct target and optional carried-source
  generation identities missing from that request object.
- `queue` binds the exact active-source revision, record revision,
  canonical-state SHA-256, completed and desired watermarks, compaction epoch,
  complete queue policy, and complete semantic-required reconciliation. The
  reconciliation contains the source-observation pair, desired set and digest,
  and a null sealed-input digest at installation.
- every `results` entry contains exactly one `origin` value of
  `fresh_worker_session` or `carried_current_generation`; the canonical accepted
  begin object and its digest; a complete ordered worker-result transcript plus
  byte count, SHA-256, and observed integer process exit 0; and the complete
  reopened result-binding envelope plus byte count and SHA-256. The transcript
  contains one matching work frame, no more than eight matching checkpoint
  frames, and one final and only terminal whose outcome is `completed`. `origin`
  is hop-local wrapper metadata and is not part of that immutable worker
  evidence.
- every `materialized` entry contains exactly one final path slot's desired work
  and digest, payload and digest/byte count, and source result-binding digest.
  It is recomputed from `results`, never accepted as caller authority.

The result array is a bijection with the reconciliation's exact desired set.
Each entry's repository UUID equals the outer record; its active-source revision
and migration epoch equal the captured current queue/request authority; and its
work retains the current reconciliation's source epoch and policy hash. Fresh
begin active-source, migration, and desired-watermark expectations equal the
captured values; its original global registry coordinate is retained while the
same repository entry is revalidated at the current registry revision. Carried
evidence may retain older registry, worker-operation, queue, and watermark
coordinates, but those original values are never rewritten and its repository,
active source, migration epoch, and desired identity must still match the
current handoff. At least one carried origin requires the non-null recorded
source generation, all such entries use that one source, and a record without a
carried origin requires the source field to be null.

Its deterministic application order is NFC-normalized path in lexicographic
UTF-8 byte order, then ascending desired revision, operation, content digest,
and result-binding digest. Starting from an empty path map, `UPSERT` replaces
one path slot with its exact `semantic_fragment`; `DELETE` removes the path slot
with its exact kind-only tombstone. An absent delete is idempotent. Tombstones
remain in `results`; `materialized` contains only the final UPSERT slots in path
order. Operation/payload mismatch, nonascending same-path revision, duplicate
work, multiple final slots, or a nonreproducible materialized array is invalid.

Fresh result evidence is accepted only from exact process exit 0 plus the one
schema-valid completed terminal and reopened envelope described above. An exact
carried completion sets its new wrapper origin to `carried_current_generation`
and must copy the source entry's complete begin request, session, result-binding
envelope, byte counts, and digests without change for the same
`SemanticDesiredWork` identity from the verified
`graphify-out/semantic-inputs.json` in the exact current certified source
generation bound by `carried_source_generation_id` and the structural request's
pointer/receipt CAS. Its inventory entry and bytes must agree with that
generation receipt's payload manifest. Arbitrary historical or orphan handoff
discovery is forbidden. Legacy completion, queue status,
watermark, cleared checkpoint, worker staging file, generation receipt alone,
manual inspection, or synthesized terminal is not association authority.
Missing, duplicate, stale, foreign, conflicting, or extra evidence leaves no
valid record.

The canonical handoff is installed and reopened before the existing
`request_staged_build()` transition. That transition and every later generation
operation receive the unchanged `target_generation_id`. Under the later exact
request-bound `BUILD` grant, the same bytes are installed and reopened as the
sole `graphify-out/semantic-inputs.json` file beneath target-generation-owned
staging. The structural output is otherwise unchanged. Existing generation
inventory and mode/path rules include that regular file when
`complete_staged_build()` commits the exact `graphify-out` payload manifest.

Only after staged `COMPLETE` is reopened and its manifest recomputed may the same
current `BUILD` grant call `bind_sealed_inputs()` with that digest. The complete
captured pre-bind queue revision, hash, policy, compaction epoch, watermarks, and
reconciliation plus the request, source authority, handoff, and generation copy
are revalidated immediately before the call. Null-to-digest is the only forward
transition; exact replay may also adopt the deterministic post-bind state whose
reconciliation differs only by that digest and whose revision/hash are the
result of the one queue commit. A different existing digest or unrelated queue
advance is a conflict. A reopened queue record proving that exact digest is this
child's terminal durable boundary.

Handoff-install uncertainty adopts only an exact no-follow reread with the same
target and carried-source identities; a different target, source/target swap,
or different carried source is a conflict. Proven absence may retry only from
the exact retained authority snapshot. Once staged state exists, it must bind
the same repository, target, and structural request in the exact request-bound
`REQUESTED`, `PUBLISHING`, or `COMPLETE` lifecycle; a certified target or any
other target state conflicts. Staged recovery remains owned by
`graphify.workspace.staged_build.internal`: a
successor fence may reset only unsealed target-generation staging and recopy the
retained handoff, while `COMPLETE` adopts only the exact recorded inventory.
Bind uncertainty adopts an exact expected post-bind reread, or retries from the
exact unchanged pre-bind state under the same live grant. Every other state is
commit-unknown.

The trusted lifecycle composition owns best-effort deletion of an original
worker envelope only after the external handoff, generation copy, staged
manifest, and queue binding are all reopened and agree. Deletion is not part of
the commit. The accepted worker, semantic queue, and `GenerationStore` do not
delete other semantic staging. The handoff remains retained through later
certification or terminal abandonment. Conflicting, stale, foreign, extra,
orphaned, legacy-unindexed, or commit-unknown semantic staging is not adopted or
automatically deleted; a separately authorized semantic-staging repair or GC
lifecycle owns any later deletion. Cleanup never destroys the only remaining
recovery evidence.

The target-generation-owned semantic-input copy may retain bounded semantic
`label` and
sanitizer-produced `rationale` text. Staged completion and sealed-input binding
are integrity boundaries, not content-level DLP or release decisions. This child
stops before certification, promotion, pointer movement, query projection, or
any public semantic-sync lifecycle. Its accepted status adds no authority beyond
that exact stop boundary.

## Semantic-generation certification finalization

The unnumbered P5B2 child is implemented and accepted only as the frozen
composition of existing durable formats and authorities below. Its
[accepted completion receipt](receipts/p5b2-semantic-generation-certification-finalization.md)
was made canonical by merged PR #58 and binds the corrected PR #56 plus PR #57
delivery chain. The child introduces no schema, format version, record kind,
runtime receipt kind, public command, or migration.
Its entry is the accepted semantic-result handoff's exact target in reopened
staged `COMPLETE` state plus a reopened semantic queue whose complete
reconciliation binds the same non-null sealed-input digest as that staged
manifest. Its terminal state is the same staged record durably advanced to
`CERTIFIED` and cross-checked against the installed generation, immutable
semantic certification binding, generation receipt and journal, cleared target
reservation, unchanged pointer, and released recovery grant.

Entry authority is one composite proof, not any individual record. It contains:

- the accepted target generation and complete canonical `StructuralBuildRequest`,
  whose logical request digest equals the accepted `SyncRequest`;
- the same request-bound staged record in `COMPLETE`, with the exact allocation,
  sorted payload inventory, payload-manifest SHA-256, and no abandonment or
  pointer evidence;
- the retained immutable handoff and the byte-identical sole
  `graphify-out/semantic-inputs.json` entry in target-generation-owned staging,
  both matching that inventory and manifest;
- the exact complete semantic-required reconciliation and reopened queue state,
  including repository, active-source revision, queue revision and canonical
  state hash, desired/completed watermark, compaction epoch, source epoch and
  commit, policy and observation digests, and the staged manifest as its equal
  non-null sealed-input digest; and
- current registry, active-source, migration, pointer, operation, policy,
  compatibility, capacity, and two-equal-source-observation evidence. The old
  `COMPLETE` operation epoch and fence remain historical state; they are not
  reused as current mutation authority.

A merely complete watermark, handoff, staged directory, staged `COMPLETE`
record, equal digest, receipt, final directory, or journal head is insufficient.
Foreign repositories, targets, requests, handoffs, queues, manifests,
inventories, source observations, or compatibility digests conflict. A
`REQUESTED`, `PUBLISHING`, `ABANDONED`, unrelated `CERTIFIED`, `PROMOTED`,
missing, duplicate-location, or otherwise ambiguous target is not forward
certification entry authority. An invocation that begins with the full exact
terminal `CERTIFIED` proof and no retained cleanup grant may perform read-only
replay verification only.

One exact post-certification exception exists for terminal cleanup. When the
same `CERTIFIED` record, receipt, binding, journal, reservation absence, pointer
boundary, request, target, and manifest are durable but their paired
request-bound `BUILD` lease and staged-attempt digest remain, the store may
adopt that persisted digest solely to recover the cleanup grant. The same live
grant is reusable only by its current OS owner; an expired or rebooted grant may
be replaced under the normal non-resetting fence rules. The cleanup grant may
only prove the terminal state, release itself, and prove absence. Its newer
epoch or fence, if any, is cleanup authority and must not replace the
certification epoch or fence already recorded in the staged state or receipt.
A live foreign owner, non-`BUILD` lease, unpaired or changed attempt digest, or
ambiguous/replaced state grants no cleanup authority.

Forward certification may reacquire mutation authority only through
`GenerationStore.acquire_staged_recovery()` for the same repository, target,
and structural request. The resulting grant is the request-bound `BUILD`
recovery lane with a new caller attempt digest and current operation epoch and
fence. It may not call `request_staged_build()` again, allocate a different
target, acquire an unbound operation, or use `MIGRATE`, `PROMOTE`,
`POINTER_RECOVERY`, repair, or abandonment authority. Registry-before-workspace
ordering remains mandatory. Existing generation operations retain
workspace-before-pre-created-generation-lock ordering; a generation lock never
wraps registry or workspace acquisition.

Under that grant, `GenerationStore.allocate()`, `prepare_staged_build()`, and
`complete_staged_build()` may only recover the exact capacity reservation,
allocation, completion wrapper, inventory, and manifest already represented by
the `COMPLETE` record. The `COMPLETE` path never resets or deletes staging,
reruns the adapter, rewrites payload bytes, recopies the handoff, appends an
entry, changes the manifest, or silently adopts different sealed staging. A
missing reservation is recoverable only through the existing exact
interrupted-certification rules; any other absence or mismatch is a conflict.

The exact semantic certification view is obtained through existing
`SemanticQueueStore.certification_view()` authority with two fresh equal source
observations and the staged manifest. It must reproduce the entry queue state
and report `semantic_completeness="complete"`; `not_required`, incomplete,
unsealed, unequal, scalar-watermark-only, or differently revised evidence is
inadmissible. The exact existing `CertificationRequest` derives its source
commit and epoch, policy digest, observation-manifest digest, queue watermark,
and complete semantic status from that view; uses the current request-selected
compatibility digest; and contains exactly the existing validations
`coordination_lock_precreated`, `payload_manifest`, and
`stable_semantic_queue`. The declared entries are the exact reconstructed
completion inventory.

`GenerationStore.certify()` remains the only forward certification mutation.
Its durable order is fixed:

1. validate the exact allocation, structural request, staged `COMPLETE` state,
   declared inventory, manifest, certification request, and any target-derived
   existing semantic certification binding under registry/workspace authority;
2. if no binding exists, recapture the exact semantic certification view and
   under the workspace lock revalidate its queue revision and canonical-state
   SHA-256 against current durable queue state;
3. install and reopen the immutable target-derived semantic certification
   binding of repository, target, certification-request digest, complete queue
   view, and sealed manifest before any generation lock or receipt becomes
   authority;
4. take the pre-created target generation lock and use existing certification
   recovery to reopen or advance the generation journal through `BUILT` and
   `VALIDATING`, inventory and sync the exact payload, install or reopen the
   canonical generation receipt, move exact staging to the final generation
   location when required, verify the installed generation and semantic
   binding, and append or reopen the matching `CERTIFIED` journal event with
   pointer revision zero;
5. clear only the exact target capacity reservation under registry/workspace
   authority and reopen the generation and receipt; then
6. under the existing generation lock, durably advance the same staged record
   by one revision from `COMPLETE` to `CERTIFIED`, preserving repository,
   target, structural request, and manifest while recording the exact receipt
   digest and the receipt's current certification operation epoch and fence.

Same canonical binding, receipt, journal, generation, reservation state, and
staged state are idempotent. Before the immutable binding exists, any registry,
active-source, operation, migration, pointer, source, policy, compatibility,
queue, request, target, handoff, generation-copy, inventory, manifest, sealed
digest, or observation drift blocks new certification. Proven absence may retry
only while the entire pre-binding authority remains exact. A different,
unreadable, unsafe, or ambiguous binding is commit-unknown.

Once the exact immutable binding is durable, it is the commit boundary for the
captured queue view. A later queue, source, policy, or compatibility change is
never adopted into that certification. Existing `GenerationStore` recovery may
finish only the already-bound request and view. Exact reread similarly governs
uncertain receipt installation, staging-to-final movement, generation
verification, journal append, reservation clear, staged-state transition, and
lease release. An exact installed receipt bound to the exact payload, binding,
and matching `VALIDATING` or `CERTIFIED` history may be recovered; a preseeded
receipt without the binding, different bytes, both or neither generation
locations, unrelated reservation state, replacement lease, revision jump, or
ambiguous durable suffix is never inferred success.

Terminal proof requires one reopened staged `CERTIFIED` record with the same
request, target, and manifest and the exact verified receipt digest;
`GenerationStore.verify_generation()` success for exactly one final target with
the same inventory, manifest, coordination lock, complete semantic receipt, and
immutable binding; the matching journal `CERTIFIED` event with pointer revision
zero; durable absence of only the exact target reservation; an unchanged visible
pointer at the entry revision/current-receipt boundary; and, after release, a
locked reread proving the exact recovery owner/fence absent. Release uncertainty
may retry or adopt only that exact durable state. No path performs destructive
cleanup or infers terminal success from one absent or present artifact.

That proof is the stop boundary. It grants no content-release or DLP decision,
semantic graph construction/merge/query projection, promotion, pointer
movement, public semantic-sync command, provider/backend/model choice,
credential or network authority, migrate, repair, GC, service/watch,
publication, P5C, H3, P6+, parent completion, successor readiness, governance
acceptance, or merge authority.

## Semantic-generation promotion and pointer-finalization

The unnumbered P5B2 child is implemented and accepted only as the frozen
composition of existing durable formats and authorities below. Its
[accepted completion receipt](receipts/p5b2-semantic-generation-promotion-finalization.md)
binds the exact PR #59 through PR #64 chain. The child adds no new durable record
contract, format version, schema, runtime receipt, public command, or operator
execution authority. It composes the
existing staged-build, generation receipt, semantic certification binding,
journal, pointer, prior-pointer, lease, and coordination-lock records only.

Its sole entry is the accepted certification terminal as one exact composite
state: staged `CERTIFIED` for the same request, target, payload manifest and
receipt; one verified installed target with unchanged semantic certification
binding; the matching `CERTIFIED` journal event at pointer revision zero;
durable absence of the target reservation; visible pointer equality with the
request's revision/current-receipt CAS; no pending pointer intent; and durable
absence of the certification `BUILD` grant and staged-attempt digest. Current
Durable registry, active-source, certified-source, migration, operation,
policy, compatibility, and state-schema authority must still agree. No
constituent record alone is entry authority.

That composite is fresh forward entry. After acquisition or an attempted move,
a recovery invocation may replace only the fresh-absence clauses with exact
operation-bound residue: the persisted promotion attempt and its live or
expiry/reboot-replaceable grant, the matching target-bound `PROMOTED` or
`REPAIRED` journal evidence when already durable, and the exact pending or
visible pointer evidence. The target reservation and certification `BUILD`
grant remain absent, and the same staged record, installed generation, receipt,
binding, request and target remain unchanged. A new direct move requires two
fresh equal source observations. Exact pointer recovery or already-visible
finalization does not require the selected checkout to remain observable and
never adopts newer source evidence; it relies on the durable move plus current
registry and active-source authority.

`GenerationStore.acquire_staged_recovery()` is the only acquisition path. For
staged `CERTIFIED`, it classifies the next exact lane from durable pointer state:
`PROMOTE` when no pointer intent exists and `POINTER_RECOVERY` when one exists.
The request, target, structural request, and attempt digest bind the grant. A
fresh acquisition creates one digest; commit-unknown retry under the current OS
owner reopens only the same live grant and digest. After exact expiry or reboot
proof, request/target-bound acquisition may replace the grant with the same
persisted digest and the operation selected from current durable pointer state.
A different digest while the persisted attempt remains is not recovery. The
composition may not reopen `BUILD`, allocate another target, call
`request_staged_build()`, or use generic mutation, repair, migration, GC,
rollback, or abandonment authority.

Direct `PROMOTE` uses one complete `PointerCAS` whose expected pointer revision
and current receipt come from the staged request; active-source revision,
operation epoch, migration epoch and fence come from the accepted grant;
source epoch and candidate receipt come from the exact certified receipt; and
state-schema version remains frozen. `PointerStore.promote()` verifies the
candidate generation and `CERTIFIED` journal eligibility, then preserves the
existing durable order: retained prior, exact pending intent, exact visible
pointer, authoritative `PROMOTED` journal event, and pending-intent unlink. A
new direct move advances the pointer revision by exactly one. If the exact
target/receipt is already visible with complete journal proof and no pending
intent, no new pointer move occurs; any different advanced current is stale
CAS, not replay success.

When durable pointer intent selects `POINTER_RECOVERY`, it may reconcile only
pending or visible residue from that same target/receipt move. Its locked plan
must select that exact target from the exact pending or visible evidence,
retain valid prior/revision relationships, have an empty quarantine set, and
rederive identically under the mutation locks. It may not select an unrelated
current, prior, last-good, arbitrary certified, newer, or substituted
generation. The store may preserve
the residue's revision or re-emit the same target/receipt at the later monotonic
revision required to close visible commit uncertainty; that exact `REPAIRED`
event is recovery evidence, not generic repair or newer-target authority.
Corrupt, stale, incompatible, foreign, or ambiguous intent remains a barrier.

Registry-before-workspace and workspace-before-sorted-generation-lock ordering
remains fixed. Under that order, `GenerationStore.complete_staged_promotion()`
does not move a pointer. It requires no pending intent, byte-equal visible
pointer input, exact target/receipt/current-source binding, a pointer revision
greater than the request CAS, a verified unchanged installed generation, and a
matching authoritative `PROMOTED` or `REPAIRED` journal event at the exact
revision, operation epoch and fence. It then advances only the same staged
record by one revision from `CERTIFIED` to `PROMOTED`, preserving repository,
target, structural request, manifest and receipt and recording the exact pointer
revision and pointer-authority epoch/fence. Exact `PROMOTED` replay is
idempotent; a revision jump, different manifest/receipt/request, abandonment
evidence, or unmatched pointer is a conflict.

Commit uncertainty is resolved separately at acquisition, pending intent,
visible pointer, journal, staged transition, and lease release. Each boundary
adopts only exact canonical reread, same-attempt live-grant recovery, or the
same persisted-attempt request/target-bound replacement after expiry/reboot.
The visible target is not success without journal authority and pending-intent
resolution; the staged marker is not success without the exact pointer and
installed evidence; and an absent lease is not success after replacement
authority. Release may retry only the exact unchanged live grant or adopt a
locked reread proving that exact owner/fence and staged-attempt digest absent.
A retained exact grant may
be cleaned up only after full terminal proof and grants no pointer or staged
mutation. The same live grant is reusable only by its current OS owner; an
expired or rebooted terminal grant may be replaced only by exact
request/target-bound cleanup authority, never a generic unbound acquisition.
The cleanup epoch/fence is not copied into pointer, journal, receipt, or staged
`PROMOTED` evidence.

Terminal proof is staged `PROMOTED` for the same request, target, manifest and
receipt; visible current bound to that target and receipt; equal staged/visible
pointer revision plus matching authoritative promotion/recovery journal;
absence of unresolved pointer intent or journal recovery; unchanged installed
payload, receipt, coordination lock, retained handoff and semantic
certification binding; and durable absence of the exact promotion owner/fence
after release. Only this exact promoted current generation may later serve as
carried semantic-result evidence for a separately authorized handoff. That
fact grants no content release, DLP, graph/query projection, public semantic
sync, runtime receipt, provider, networking, repair, GC, publication, execution,
or later-successor readiness. Parent P5 and P5B2 remain `IN_PROGRESS`, remaining
P5B2 work and P5C work remain `WAITING`, and H3 remains `DEFERRED`.

## Semantic-content release/DLP decision

The proposed unnumbered P5B2 semantic-content release/DLP decision child is a
contract freeze at `WAITING`. It may later add one private internal decision
binding but no lifecycle transition, staged-build state, journal event,
generation receipt, public schema, runtime receipt, or public result.

Its exact entry is the accepted promotion terminal reopened as one state: the
same staged `PROMOTED` request/target/manifest/receipt and pointer authority;
the visible current plus matching authoritative `PROMOTED` or admissible
`REPAIRED` journal event; no pending pointer or journal recovery; unchanged
installed inventory, handoff, target-owned semantic input, coordination lock,
and immutable semantic certification binding; exact promotion-grant and
staged-attempt absence; and unchanged registry, active-source, migration,
operation, queue-policy, compatibility, state-schema, and source authority; the
exact installed semantic-release bundle manifest; and the stable current
`ACTIVE` operator policy-authority record.
One coordinate alone, a historical promoted generation, or drift is never
decision authority.

The trusted bundle is the future repo-owned installed package-data file
`graphify/workspace/semantic_release_manifest.json`, loaded through
installed-package authority rather than caller input. It is at most 1 MiB and inventories
the classifier implementation, byte-defined ABI, taxonomy, ruleset,
normalization contract, and selectable profiles by package-relative path,
regular-file mode, byte count, and digest. Total referenced artifact bytes are
at most 25 MiB.
Paths are unique sorted UTF-8 POSIX relative-normal-form beneath the canonical
installed `graphify` package root. Absolute, empty/`.`/`..`, repeated-separator,
backslash, NUL/control, and alias paths are invalid. Descriptor-relative
no-follow opens must prove every component contained and each artifact a
single-link regular file of exact bytes/size/digest and mode `0444` or `0644`,
with no execute or group/other write bit.

The separate future `SemanticReleasePolicyAuthorityStore` owns these private
stable paths:

- `workspaces/<repository_uuid>/semantic-release-policy-authority.json`;
- `workspaces/<repository_uuid>/semantic-release-policy-authority.previous.json`;
  and
- `workspaces/<repository_uuid>/semantic-release-policy-authority.pending.json`.

Its at-most-64-KiB format-version-1 current record binds repository UUID, named
release context, positive monotonic revision, predecessor-record digest or
genesis marker, `ACTIVE` or `REVOKED`, bundle-manifest digest, selected profile
IDs/digests, canonical policy ID/version/bytes/digest, and the existing-shaped
operator authorization nested inside an at-most-16-KiB version-1
policy-selection envelope. The envelope explicitly adds internal action
`SELECT_SEMANTIC_RELEASE_POLICY` and contains `authority_body_sha256`, computed
over the authority-body projection excluding both the whole envelope and its
sibling `selection_authorization_sha256`, plus the five fields `action`,
`issued_at`, `nonce`, `operator_id`, and `reason`. The selection digest hashes
the completed envelope bytes and is stored only as that sibling; the
complete-record digest is computed externally over completed record bytes. No digest
preimage contains its own digest. Missing or
pending state, bad predecessor chain, rollback, revocation, invalid
authorization, or bundle/policy/profile disagreement fails closed. This child
consumes but never provisions, advances, repairs, revokes, or cleans that state.

The canonical decision request contains or binds by digest:

- repository, generation, staged request, manifest, receipt, pointer revision,
  pointer operation epoch/fence, and authoritative journal identity;
- complete payload inventory, retained handoff, semantic-input, certification
  binding, and eligible-field-inventory digests;
- installed bundle-manifest identity/digest and current policy-authority
  revision/complete-record digest;
- distinct taxonomy, normalization, classifier implementation/ABI, ruleset,
  and selected coverage-profile IDs, versions, and manifest-bound SHA-256
  values; and
- one authority-selected policy ID, version, SHA-256, named release context,
  and exact coverage-sufficiency declaration.

Canonical bytes produce `decision_request_sha256`. Policy authority is explicit
and non-ambient. No environment, provider, model, credential, network, live
catalogue, or fallback may supply a missing value. Any unsupported version,
unknown category or profile, digest disagreement, missing mapping, or
insufficient coverage is fail-closed release rejection.
The canonical decision request is at most 64 KiB. A caller may request only the
exact current authority and cannot supply policy or bundle bytes.

The eligible field inventory contains exactly every required node label,
present optional node rationale, and required hyperedge label. Each entry uses
exactly one `field_type`: `node_label`, `node_rationale`, or `hyperedge_label`,
and is ordered by entity kind, UTF-8 entity ID, and field name. There is no
hyperedge-rationale slot. Each field retains the accepted 16 KiB UTF-8 limit and
exact canonical normalization. The inventory commits to every entity/field
identity and exact value SHA-256 without copying field values.

The version-1 classifier ABI operates on exact captured UTF-8 bytes and cannot
use runtime Unicode categories, locale, renormalization, or host-dependent text
behavior. Only syntax-defined ASCII names may use the ABI-defined ASCII fold;
value bytes remain exact. Its grammar, dictionary encoding, comparison,
ordering, duplicate reduction, and error behavior are manifest-bound.

Hard independent limits are 64 selected profiles, 4,096 categories, 4,096
rules, 256 UTF-8 bytes for any classifier-related ID, and 30,000 eligible
fields. Every field has exactly one result record and at most 256 category IDs
and 256 private rule IDs. Exceeding a limit is `INDETERMINATE` and rejects; no
bundle, request, or environment may enlarge one.

The closed classifier outcome is `NO_MATCH`, `MATCH`, or `INDETERMINATE`.
`INDETERMINATE` rejects. `NO_MATCH` produces `ALLOW_FIELD` only under the exact
coverage-sufficiency declaration; otherwise it rejects. `MATCH` preserves all
sorted unique category IDs and sorted private rule IDs, and policy maps every
`(field_type, category_id)` pair. An unknown or unmapped pair rejects; otherwise
the pair actions reduce to `ALLOW_FIELD`, `OMIT_RATIONALE`, or `REJECT_RELEASE`
with rejection over omission over allow. A label cannot receive
`OMIT_RATIONALE`. The final decision is `ALLOW_UNCHANGED`,
`ALLOW_WITH_OMISSIONS`, or `REJECTED`. `NO_MATCH` alone never proves sufficient
coverage or release safety.

The future `SemanticReleaseDecisionStore` owns the private canonical
`graphify.workspace.semantic_release_decision.internal` format-version-1
binding at
`workspaces/<repository_uuid>/semantic-release-decisions/<generation_id>/<decision_request_sha256>.json`.
The namespace is outside the sealed generation and uses descriptor-relative
no-follow access, mode-`0700` directories, and one single-link regular
mode-`0600` binding. Unexpected entries, unsafe modes, symlinks, special files,
or ambiguous enumeration fail closed. Request addressing permits separate
authority revisions for one generation without overwriting or substitution.
Install-once equal bytes are idempotent; same-path different bytes conflict.

The binding contains only entry and authority digests; bundle and current
policy-authority coordinates; exact input and eligible-field-inventory digests;
taxonomy, normalization, classifier/ABI/ruleset, profile, and policy
coordinates; scanned-field counts; every ordered field-result record; terminal
outcome; and `full_result_sha256`. A field-result record has entity kind,
private entity ID, field name, field-value SHA-256, classifier outcome, sorted
category IDs, sorted private rule IDs, and field disposition. The full-result
digest preimage is the canonical result object containing inventory digest,
counts, all field-result records, and outcome, excluding both digest members.
The binding never contains its own digest; external `binding_sha256` is
computed over completed canonical binding bytes. Raw semantic prose, matched
substrings, generated explanations, confidence scores, public source locations,
provider responses, and credentials are forbidden.

The binding is at most 25 MiB. Bounded no-follow enumeration permits at most 64
bindings per generation and 4,096 per workspace. All decision-store bytes are
charged against existing `CapacityPolicy` global/workspace byte ceilings and
reserve, and the authoritative usage scanner must include this namespace before
the child can be `READY`. Limit, enumeration, or capacity uncertainty rejects.

The write boundary uses capture-classify-revalidate-install. Under the existing
shared registry lock, exclusive workspace lock, then target-generation shared
lock, the implementation captures the exact promoted proof, installed bundle,
current policy authority, decision-store counts/capacity, and bounded private
semantic-input bytes. Classification occurs without coordination locks or
durable writes. Final revalidation acquires the exclusive registry lock,
exclusive workspace lock, then shared generation lock; every coordinate, byte
count, digest, store count, capacity ceiling, and filesystem reserve is
revalidated, and the binding is installed once and reopened before those final
locks are released. The exclusive registry lock serializes global capacity and
reserve accounting across workspaces. Any drift discards the computed result.
It is never rebased onto newer authority.

After install uncertainty, exact reopened bytes adopt the commit. Proven
absence may retry only while the complete decision request remains exact.
Different, unreadable, unsafe, partially present, or ambiguous state is
commit-unknown. Identical concurrent requests converge; different requests use
different derived paths. No failure or replay deletes, rewrites, or repairs a
binding, semantic input, handoff, generation, receipt, journal, staged record,
pointer, or policy. A later pointer or authority change makes a prior binding
historical evidence only.

This child never deletes decision state. Until separately accepted GC
integration exists, a nonempty decision directory is a protected reason that
blocks purge of its generation. Any later removal or quarantine must be
authorized and coordinated with the same generation; no such authority is
granted here.

Terminal proof is derived, not separately persisted. Under the existing lock
order it reopens the still-current promoted terminal, exact decision request,
and immutable binding and proves equal input, taxonomy, normalization,
classifier/ABI/ruleset, profile, policy, result, and binding digests plus
bounded counts and one exact terminal outcome. The proof contains no private
entity/field locator, field-value digest, category ID, or rule ID; an authorized
projection consumer must reopen the mode-`0600` binding for omissions. No new
lease or lifecycle/journal authority participates.

A consumer must name the exact
`(generation_id, decision_request_sha256, binding_sha256)` tuple and prove the
request-bound policy-authority revision remains stable current `ACTIVE`.
Enumeration, newest-file choice, historical fallback, or caller-selected
binding substitution is not authority.

That proof stops before omission execution, deterministic redaction, label or
entity removal, ID remapping, graph construction/merge/query projection,
`query_structural()` changes, public semantic sync, provider/backend/model
selection, networking, public/runtime schemas or receipts, status, repair,
migrate, GC or binding cleanup, service/watch, publication, P5C, H3, P6+,
implementation, readiness, acceptance, parent completion, execution, or later
successor authority.

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
