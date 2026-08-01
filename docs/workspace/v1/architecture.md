# Architecture

## Boundary

Graphify remains one distribution and one graph engine. Native `0.9.16` owns
detection, structural and semantic extraction, caches, graph construction,
clustering, export, diagnostics, watch/update behavior, and traversal. The
workspace control plane will own external lifecycle state around that engine.

P1 freezes only these seams:

1. repo policy and stable UUID label;
2. registry and one explicit active source;
3. immutable-generation receipt and sealed query payload;
4. external framed/hash-linked journal events;
5. fenced leases and pointer records;
6. retained generation coordination-lock identity;
7. two-sided observation-based freshness release record;
8. compatibility and candidate artifact manifests; and
9. installer transaction, compensation, and offline rollback records.

P2 implements the registry writer, identity/source enrollment, explicit
active-source CAS, and lease allocator. P3 implements lifecycle mechanics for
caller-supplied staged generations, pointers, journals, and offline GC. P4
implements the sole `0.9.16` adapter, no-write comparison seam, and
observed-current release authority. P5A adds only the durable semantic desired-
work queue, fenced worker claims, reconciliation evidence, and the generation-
certification binding to one stable queue watermark. Pre-workspace state has no
import or promotion lane. P5B1 adds the production composition root and the
versioned read-only `graphify workspace status --json` and
`graphify workspace doctor` surface without repair or durable-state mutation.
The executable surface discovers only the standard external state root and
reads one bounded, canonical `runtime-manifest.json`; it never searches a
checkout, synthesizes a compatibility tuple or queue policy, or derives policy
from durable queue content. Missing or unusable authority fails closed.
P5B2a adds only explicit initial enrollment and explicit adoption through
`graphify workspace register`. The command composes that installed authority,
requires matching stdin authorization and an expected registry revision,
requires the current working directory itself to be the Git top level, then
delegates to the P2 registry CAS.
Adoption is never inferred: the operator must select `adopt`, and the existing
registry must verify shared history for the same UUID. The narrow remaining-
P5B2 identity-maintenance slice adds explicit `register rebind` and
`register rotate` forms on that same path. Installed authority is composed
before their bounded matching authorization is read, and the source checkout is
discovered and exactly revalidated before the expected-revision registry CAS.
The CLI delegates rebind history/common-directory policy and rotation binding
plus immutable-enrollment continuity to the existing `RegistryStore` methods.
Later active-source resolution independently applies the same history-root or
enrolled Git-common-directory identity rule. The CLI adds only a dedicated
versioned receipt contract; it does not change the durable registry format,
duplicate policy, or modify `active_source` or `active_source_revision`. The
separate unnumbered active-source slice adds only the standalone
`workspace activate` argv and a
CLI-v1 redacted receipt. It loads and composes installed authority before
reading canonical `ACTIVATE` authorization, reuses the same two-pass source and
Git checkout proof, derives lease identity and time inputs internally, and
calls `RegistryStore.activate_source()` exactly once with all four explicit CAS
values. Under the registry lock, activation requires both an explicit locator
binding and continuity with immutable enrollment identity through a recorded
history root or the enrolled Git common-directory device/inode. The standalone
CLI also requires the target to differ from the selected active source and
rejects reselection before lease, evidence, or revision mutation. The existing
registry/lease implementation continues to own fencing, recovery and reservation
barriers, alias changes, and semantic active-source authority. P5B2b0 adds the
internal request-bound staged structural-build lifecycle needed to recover
crashes and close requests made stale by durable authority drift. P5B2b exposes
only `graphify workspace sync --code-only --request-stdin`. The exact request
is installed before `BUILD` lease acquisition; request-bound `BUILD`,
`PROMOTE`, and `POINTER_RECOVERY` successors are constrained by the durable
lifecycle and the caller attempt digest. The command builds one structural
payload beneath external generation-owned staging, reconciles an empty semantic
set with `semantic_required=false`, certifies immutable bytes, promotes through
existing pointer CAS, and emits a canonical redacted receipt. It adds no provider
selection, network authority, pointer-policy change, or durable-state schema.
Status schema v2 makes staged recovery barriers visible. P5B2c exposes only
`graphify workspace query --request-stdin`: it loads and composes the installed
runtime authority before reading its canonical, bounded CLI-v1 request, then
calls `WorkspaceRuntime.freshness.query()` exactly once. It performs no
advisory status probe. The existing freshness authority owns query bounds,
locks, two-sided observation, and release; the CLI releases raw native UTF-8
output only for `release` / `observed_current` and otherwise withholds output.

The sole contract-only READY successor is the future
`graphify workspace semantic-worker --stdio` transport described in
[`semantic-sync.md`](semantic-sync.md). Unlike provider-owning extraction, it
does not invoke a model. One long-lived CLI process derives one trusted owner,
acquires one `SEMANTIC_CLAIM` lease, emits one verified source-relative desired
work identity to an already-active host agent, accepts bounded canonical
checkpoint and terminal frames, and completes or fails that item under the
same claim. Separate subprocesses cannot continue the claim because owner,
fence, source revision, and operation and migration epochs are exact.

On the completion path, the transport validates and sanitizes an `UPSERT`
fragment with lossless exact-decimal helper encoding or accepts the exact
`DELETE` tombstone, installs one canonical immutable result envelope in private
external workspace semantic staging, reopens and verifies its SHA-256, persists
that digest in the existing bounded claim checkpoint, revalidates the envelope
and source bytes under the live grant, and only then calls the existing queue
completion transition. A public `completed` frame follows only after the exact
semantic lease is provably released. The durable queue format is unchanged.
The transport stops before generation staging
finalization, `bind_sealed_inputs()`, certification, promotion, or pointer
mutation. No runtime implementation or completion receipt exists for this
READY contract.

The separate rollback slice exposes only
`graphify workspace rollback --request-stdin`. It composes installed runtime
authority before consuming one bounded canonical request, requires its target
to equal the visible pointer's exact verified `last_good`, acquires one fenced
`ROLLBACK` lease from caller-supplied pre-acquisition CAS, derives the accepted
operation epoch and fence token from the grant, and delegates once to
`PointerStore.rollback()`. The existing pointer, journal, lease, generation,
and recovery layers continue to own durable mutation and commit-unknown
barriers; the CLI adds no historical-generation selector or durable format.
The bounded GC preview slice remains `graphify workspace gc --dry-run
--request-stdin`. It composes installed authority before its one canonical
request and passes only caller-supplied identity revisions, timeout, complete
capacity policy, and six-class protection set to existing read-only GC
coordination. Two matching reachability snapshots produce one deterministic
unfenced preview result; the CLI creates no lease, fence, executable plan, or
durable state.

The public fenced lifecycle adds only exact `gc --execute --request-stdin`,
`gc --reconcile --request-stdin`, and `gc --purge --request-stdin` transports.
Each consumes a phase-specific canonical authorization and caller-supplied
identity CAS, capacity, and protections. Execute recomputes the frozen public
preview result, verifies its exact canonical-byte SHA-256 approval, acquires a
fresh trusted `GC` lease, and permits the existing P3 execution only when a
fresh plan matches the preview's semantically equivalent non-fence projection.
Redundant `shared_lock` detail on an otherwise protected generation does not
change that projection, while a sole lock reason remains material. One absolute
request deadline bounds planning, blocking generation-lock acquisition, and
fenced mutation. Reconcile mutates only an existing GC intent and can replay the
immutable completion indexed by a matching current operation epoch without a
lease; purge names one exact plan digest and rechecks pointer, protection, and
lock conditions. These transports expose redacted results, not raw durable
lifecycle records. They do not add automatic GC, online/service
behavior, installation, performance certification, candidate publication, or
live-cutover authority; retained production query/service authority remains
P5C work.

The public pointer-repair lifecycle adds only exact
`workspace repair --dry-run --request-stdin` and
`workspace repair --execute --request-stdin` transports. Dry-run is outside the
fenced operation domain: it uses existing-only registry/workspace/generation
locks to inspect the bounded pointer, journal, and generation evidence, creates
no coordination object, and performs no recovery, cleanup, lease, or durable
write. Execute binds `REPAIR_EXECUTE` authorization to the SHA-256 of the exact
canonical preview bytes (including the final newline), acquires a fresh `REPAIR`
lease through existing CAS authority, and recomputes the exact plan while the
same mutation locks are held. Only a plan equal to the approved decision may
enter `PointerStore`; that store remains the sole writer of pointer/journal
repair and may quarantine only excluded corrupt generations. The transport does
so under one absolute request deadline carried from preview through lease
acquisition and the in-lock mutation decision; the timeout is not renewed
between phases. It does not repair GC, staged-build, semantic queue, registry,
lease, or unsafe-path
state, committed-journal corruption, or arbitrary generations discovered by GC
or rollback. Even an approved no-op obtains the fresh fence and passes the
in-lock exact-plan comparison. The transport does not choose arbitrary history
and adds no completion index or automatic/service repair authority.

## Authority split

Repo-owned `.graphify/workspace.toml` contains the required
`contract = "graphify.workspace.config"` discriminator, `schema_version = 1`,
the immutable `repo_uuid` label, and policy only. It cannot select lifecycle
paths, pointers, generations, or global state. Enrollment remains
operator-authorized; possession of a copied UUID is not proof of identity.

The P2 global registry has one singular `active_source` per workspace plus
a monotonic `active_source_revision`, UUID-enrollment evidence, and
revision/source-bound active-source evidence. The active-source evidence stamps
the distinct operation epoch and accepted workspace-operation fence token that
the activation CAS revalidates. Paths, worktree coordinates, and
normalized remote URLs remain discovery aliases, not stable identity. A query
never guesses among aliases.
Semantic claim capability is derived only from the validated configuration read
through that registry-selected active source; a caller configuration must match
it canonically, because a matching UUID label alone is not policy authority.

P2 writes only this external lifecycle state:

```text
~/.local/state/graphify/
  registry.json
  registry.previous.json
  registry.pending.json
  registry.lock
  evidence/<sha256>.json
  workspaces/<repo_uuid>/
    workspace.json
    workspace.previous.json
    workspace.pending.json
    workspace.lock
```

P5B1 additionally reserves `runtime-manifest.json` at the root above as an
internal format-version-1 read authority containing the complete frozen
compatibility manifest and explicit semantic-queue policy. Status, doctor,
P5B2a registration, identity maintenance, P5B2b code-only sync, P5B2c one-shot
query, exact-last-good rollback, bounded GC preview, and public pointer-repair
authority loading read
it through the same
private-directory,
singular-regular-file, 0600, no-follow, bounded-read rules used for durable
state. Registration and identity maintenance then write only the P2 paths
already shown above. P5C owns creating and atomically installing the candidate-
backed authority; these consumers do not create it or guess its contents.

P3 now owns generations, lifecycle journals, coordination locks, pointers,
capacity reservations, and explicit offline-GC records. None of those paths is
written inside a source checkout.

P5B2b0 adds this bounded internal record under the same external lifecycle root:

```text
~/.local/state/graphify/workspaces/<repo_uuid>/
  staged-build.json
  staged-build.previous.json
  staged-build.pending.json
```

The current, previous, and pending records use the existing canonical durable-
record protocol, cap each read or write at 64 KiB, and bind one generation to
one exact structural request and frozen observation summary. They remain an
internal format under durable state schema v1. Public status schema v2 exposes
only a bounded summary and never recovers or mutates the record.

P5A adds this per-workspace state under the same external lifecycle root:

```text
~/.local/state/graphify/workspaces/<repo_uuid>/queue/
  semantic.jsonl
  semantic.previous.jsonl
  semantic.pending.jsonl
  certifications/<generation_id>.json
```

Despite the retained `.jsonl` path name, each file is one canonical internal
JSON record handled by the existing current/previous/pending durable-record
protocol. The record is not a new public v1 schema and does not change the
frozen receipt schema. Its active-source revision, monotonically increasing
desired and completed watermarks, exact desired-set reconciliation, claim state,
retry/dead-letter state, typed repeated-source-observation evidence, sealed
staged-input binding, and compaction epoch are external lifecycle state, never
source-checkout content. A source activation requires a newer exact
reconciliation before retained desired work is eligible again.
Each certification file is a separate immutable, store-owned internal record
binding one generation and certification request to the revalidated queue view
and exact sealed staged-input manifest.

The contract-only host-agent worker reserves this future private staging
layout relative to the configured external state root:

```text
<external_state_root>/workspaces/<repo_uuid>/semantic-staging/
  <begin_request_sha256>/result.json
```

Each `result.json` is a canonical immutable internal binding installed through
the existing no-follow durable-state primitives. It binds one begin request,
one exact semantic claim and desired work item, the accepted source and epoch
authority, and the sanitized fragment or delete-tombstone bytes and digest. It
is not a queue record, generation payload, certification binding, or public
receipt. A later
full semantic-sync contract must own consumption and cleanup; this first child
does neither.

## Ordering

P2 enforces global registry lock before the exclusive fenced workspace-operation
lock whenever an operation needs both. Activation holds both in that order.
Every public lease transition holds the registry lock while recovering one
stable snapshot, then nests the per-workspace lock through validation and any
lease-state commit. A durable pending registry revision therefore cannot be
skipped between recovery and workspace CAS. The locks are released before the
long-lived operation begins, so builds remain serialized only for their short
lease transition rather than for extraction work. P3 extends the nested order
with generation coordination locks in lexical generation-ID order and pointer
validation/CAS. P4 queries hold the existing registry lock shared, then the
pre-created generation lock shared; they do not acquire a writer-operation
lease and create no coordination object.

P5A queue mutation reuses the registry-before-workspace order through the
existing lease authority. Desired-work reconciliation uses a lifecycle lease;
claim, checkpoint, completion, failure, and semantic-lease compaction use the
reserved `SEMANTIC_CLAIM` domain while retaining the current registry revision
through the workspace mutation. Claim resolves the selected source before those
locks, then compares that identity and a fresh safe config read against the
still-locked registry entry, so activation cannot authorize work from a retired
policy. After exact reconciliation and semantic completion, a lifecycle lease
binds the queue watermark to the exact staged-payload manifest. Certification
first captures that queue view from two equal typed source observations under
the accepted build or migration lease, then revalidates the exact queue revision
and canonical-state hash while `GenerationStore` holds the workspace lock and
before it takes the generation lock. It then durably installs an immutable
store-owned certification binding before the generation lock or staged receipt
can become authority. A queue transition between capture and binding therefore
blocks certification instead of producing a receipt against mixed evidence.
Once the binding is durable, the exact request and payload can recover even if
newer desired work later advances the queue; a staged receipt alone is never
queue authority. The receipt remains the idempotent boundary for the subsequent
generation install and journal transition.

The contract-only semantic worker retains the registry-before-workspace order
for each queue mutation but keeps one process alive between mutations so the
same OS-derived owner and fence remain current. Its begin request supplies an
absolute deadline and explicit registry, source, operation, migration, queue,
and watermark CAS. The deadline begins after the canonical begin frame is
accepted and bounds both the start and observed return of completion or a
caller-requested failure. After expiry, checkpoints and completion are
forbidden and heartbeats stop; while the claim remains live, the only exception
is one transport-owned `host_agent_timeout=true` failure and lease release
before the unchanged lease liveness deadline. A checkpoint or terminal
transition repeats the existing claim validation, so source activation or
migration between protocol frames withholds the stale session. Result staging
is verified before the checkpoint and again before completion; the source
content digest or absence is checked a final time immediately before
`complete()`. A proven completion releases the exact semantic lease before any
success frame. Claim admission and later queue mutation reserve canonical-byte
headroom for the mandatory result checkpoint without a durable reservation
field. Optional and mandatory
checkpoint uncertainty uses an exact live-claim reread: adopt the requested
value, retry the exact prior value within both deadlines, otherwise
commit-unknown. Source I/O unavailability, pre-mutation registry or lease
corruption, catchable pre-mutation interruption, and stdout delivery failure
each have a frozen fail-closed route; only a completed source observation proves
content drift. An unobserved completion or failure return is commit-unknown. So
is a lost `completed` terminal, because completion clears the result association.
Neither permits inferred success.

P5B2b reuses the P5B2b0 registry-before-workspace order and installs the exact
`REQUESTED` record before acquiring its request-bound `BUILD` lease. A
nonterminal staged record blocks ordinary workspace mutation. Only the exact
request and caller attempt may acquire or recover the lifecycle-permitted
`BUILD`, `PROMOTE`, or `POINTER_RECOVERY` successor, so process-owner equality
alone cannot share a live fence. Exact durable certification may finish through
recovery; otherwise canonical source, migration, pointer, compatibility,
semantic-source, or trusted-observation drift may install an abandonment intent
before cleanup. A staged payload that exceeds its immutable reservation uses
the same intent-first terminal close so a corrected request is not blocked by
an impossible exact replay; transient pre-publication capacity failures remain
recoverable. Source unavailability alone is not stale evidence. The CLI
accepts the exact authority and capacity inputs through canonical bounded stdin,
renews the `BUILD` lease only while the synchronous structural adapter runs,
joins that renewal before staged completion, and emits only the stable receipt
after held leases have been released. Renewal failure leaves the exact
`PUBLISHING` barrier recoverable and cannot authorize completion.

Activation, migration, promotion, rollback, repair, pointer recovery, and GC
share one fenced workspace-operation domain. `SEMANTIC_CLAIM` has its reserved
semantic domain. Each live domain retains its own accepted operation epoch, so
allocating a semantic claim cannot invalidate or strand an otherwise-current
workspace lease. Migration may invalidate semantic commit authority, but the
exact trusted owner/fence can still release that stale record. P2 allocates and
validates these leases; P3 consumes only the lifecycle-operation subset. P4
owns adapter and freshness use. P5A consumes the semantic domain and binds
stable semantic completion to certification. P5B2b owns only code-only
structural orchestration. P5B2c owns only the one-shot query transport: it
forwards the validated existing `QueryRequest` and deadline to freshness, then
emits a redacted control record. It does not add a query log, state write,
service, watch loop, or retained query authority. The rollback slice owns only the
one-shot exact-`last_good` transport and reuses the existing fenced pointer
domain; it adds no retained service, repair, GC, migration, or arbitrary
historical selection authority. Later P5 slices remain responsible for all
other orchestration, concurrent in-process services, commands, installation,
and publication. The GC preview slice is outside that fenced operation domain:
it performs only bounded read-only coordination and two matching reachability
snapshots and produces no `LeaseGrant`, fence, or `GcPlan`. The separate public
lifecycle transport is inside the existing P3 fenced domain: it creates a
fresh `GC` lease only after execute has revalidated the exact public preview
bytes, and delegates plan, execute, reconcile, and purge to the existing store.
Its public comparison intentionally excludes the newly granted operation epoch
and fence; all other authority and candidate/protection facts must match.
The repair preview likewise has no lease or new lock identity. Its execute
counterpart acquires a fresh lifecycle operation only after matching exact
preview bytes, then compares its in-lock repair decision before `PointerStore`
can recover a journal, clean a temporary, replace/finalize a pointer, or
quarantine a generation. A completed repair advances lifecycle authority; an
uncertain caller must inspect status and start a new preview/request pair rather
than replay the old execute request.
