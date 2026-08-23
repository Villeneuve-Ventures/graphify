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

The accepted P5B2 host-agent child is the exact
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
mutation. Runtime implementation and completion evidence are limited to the
boundary bound by the
[P5B2 semantic-worker receipt](receipts/p5b2-semantic-worker.md); full semantic
sync and every successor remain outside it.

The accepted unnumbered P5B2 semantic-result handoff and sealed-input
finalization child composes only existing internal authorities. Its evidence is
bound by the [P5B2 semantic-result handoff receipt](receipts/p5b2-semantic-result-handoff.md),
and it has no public command or schema. The trusted lifecycle composition
admits a result only from
one exact exit-0 worker session ending in one schema-valid `completed` terminal,
or from identical format-version-1 evidence in the verified current source
generation for the same carried desired work. It snapshots the complete
semantic-required reconciliation, records the carried source separately from
the new target generation, installs one immutable
target-generation/request-bound handoff, and materializes that record's exact
bytes as target-generation-owned
`graphify-out/semantic-inputs.json` beside the
unchanged structural output. `GenerationStore` completes the staged payload
manifest and `SemanticQueueStore.bind_sealed_inputs()` binds that digest. The
child stops there; content release, graph projection, certification, promotion,
and pointer mutation remain separate.

The unnumbered P5B2 semantic-generation certification finalization child is
implemented and accepted only at its frozen internal boundary, with evidence
in the [P5B2 certification-finalization receipt](receipts/p5b2-semantic-generation-certification-finalization.md),
made canonical by merged PR #58.
It begins only from that accepted
handoff's reopened request-bound staged `COMPLETE` record, byte-exact payload
inventory and handoff copy, and equal semantic-required queue sealed-input
digest. It reacquires only the same request's `BUILD` recovery lane, reconstructs
the existing allocation and completion wrappers without resetting staging, and
uses the existing semantic certification view plus `GenerationStore.certify()`
to bind and seal the same target. Its terminal boundary is a verified staged
`CERTIFIED` record and released recovery lease; it has no public transport,
public success receipt, promotion, pointer, projection, or content-release
authority.

The unnumbered P5B2 semantic-generation promotion and pointer-finalization
child is implemented and accepted only at its frozen internal boundary, with
evidence in the
[P5B2 promotion and pointer-finalization receipt](receipts/p5b2-semantic-generation-promotion-finalization.md).
It composes no new store or format. Its fresh entry is the accepted
certification terminal: the same staged `CERTIFIED`
request/target/manifest/receipt, verified installed payload and immutable
semantic certification binding, matching `CERTIFIED` journal event at pointer
revision zero, absent target reservation, unchanged request pointer CAS, and
absent certification `BUILD` recovery grant. Only
`GenerationStore.acquire_staged_recovery()` may open the next lane. With no
pending pointer intent it must return `PROMOTE`, including for an exact
already-visible replay; only durable pending intent from the same exact
attempted move may select `POINTER_RECOVERY`.

Commit-unknown recovery may vary only the fresh-absence clauses. It may reopen
the exact persisted promotion attempt and live grant, or replace that same
attempt after expiry/reboot proof, and it may admit only matching target-bound
pending/visible pointer and `PROMOTED`/`REPAIRED` journal residue. The certified
target, receipt, binding, request, absent reservation, and absent certification
`BUILD` authority do not change.

Direct forward movement delegates the unchanged complete pointer CAS to
`PointerStore.promote()`. Recovery delegates to `PointerStore.recover()` only
after the recovery projection is bound to that same target and receipt and to
the exact pending or already-visible move; selection from an unrelated current,
prior, last-good, or arbitrary certified generation is inadmissible. The
existing registry-before-workspace and workspace-before-sorted-generation-lock
order remains unchanged. `GenerationStore.complete_staged_promotion()` may
then record `PROMOTED` only after the visible pointer, exact revision and
authoritative `PROMOTED` or `REPAIRED` journal event are durable and no pending
intent remains. Terminal release must prove the exact promotion owner/fence
absent. A retained terminal grant may be replaced after expiry or reboot only
by request/target-bound cleanup authority and may not rewrite terminal evidence.
The accepted child adds no operator execution authority, public transport,
schema, runtime receipt, content release, or graph/query projection. Its
acceptance changes no parent phase and activates no later successor.

The unnumbered P5B2 semantic-release bundle and deterministic-classifier
trust-root prerequisite is implemented and accepted only at the frozen
boundary recorded in the
[acceptance receipt](receipts/p5b2-semantic-release-trust-root.md). Its
architecture remains repo-owned installed package data plus the existing
installed executable bootstrap: one canonical manifest inventories and
digest-binds the deterministic classifier implementation, byte-defined ABI,
closed taxonomy, normalization contract, ordered ruleset, required
`core_secrets.v1`, and every selectable profile, while the private source-executed
`_graphify-semantic-authority` and `_graphify-mcp-semantic-authority` scripts
start installed Python under isolation
flags including `-S` before Python startup hooks can run, establish a fresh
package-external pycache prefix, and suppress `.pth`, `sitecustomize`, and
automatic user-site startup imports before importing Graphify. The public
cross-platform console entry points are outside semantic-release authority.
Plain user script
installs may add the installed script-prefix package root, or a PEP 610 editable
source root recorded by a `graphifyy` direct URL in that same script prefix,
explicitly after that startup boundary. The installed-root and
descriptor-relative no-follow boundary rejects path, link, mode, size, digest,
identity, version, limit, or ABI ambiguity. The byte ABI
produces only factual `NO_MATCH`, `MATCH`, or `INDETERMINATE` results over
explicit already-canonical bounded UTF-8 bytes. It owns no workspace policy
selection, durable state, capacity/GC integration, release disposition,
omission, projection, new public command/schema/receipt, provider/backend, or
publication behavior. Its acceptance grants none of those excluded behaviors
and activates no successor.

The separate internal unnumbered P5B2 semantic-release policy-authority
provisioning prerequisite is implemented and accepted as `COMPLETE` at the
frozen private boundary. Completion evidence is the
[`P5B2 semantic-release policy-authority` receipt](receipts/p5b2-semantic-release-policy-authority.md),
which binds PR #71, PR #72, and PR #74.
`SemanticReleasePolicyAuthorityStore` owns one per-workspace private
current/previous/pending record set and reuses the existing canonical durable
state-record protocol. A closed structured selection call supplies exact
current revision/digest CAS and policy material; the store validates the
installed bundle, derives canonical bytes and nonrecursive body/envelope/record digests,
and may create or advance only state `ACTIVE` under the sole internal action
`SELECT_SEMANTIC_RELEASE_POLICY`. `REVOKED` remains decodable consumer-side
fail-closed vocabulary; the prerequisite has no revocation or reactivation
operation. Stable reads and read-only recovery projections hold the existing
shared registry lock and then shared workspace lock. Both revalidate the
applicable current/previous/pending snapshot before returning; stable read also
requires pending absent and an exact current/previous chain.

Mutation holds exclusive registry then exclusive workspace locks. Genesis is
revision `1` from three-path absence and revision `0`/digest-null CAS;
advancement is exactly current plus one and binds the reopened current digest
as predecessor while retaining exact revision-minus-one previous. The store
then delegates exact candidate bytes to `DurableStateRoot`: pending, previous,
current, pending clear. The three capped 64 KiB records plus one atomic
temporary have a fixed 256 KiB peak. Any uncertainty after pending visibility
is `CommitUnknown` and permits only exact original-transaction recovery under
the same locks. Byte-identical completed replay is no-write; divergent or
`REVOKED` state blocks. The architecture adds no lease, generation, pointer,
journal, queue, decision-store, public transport, live policy choice, GC, or
successor authority. Acceptance provisions no live record.

The separate internal unnumbered P5B2 semantic-release decision-store and
capacity/GC prerequisite is implemented and accepted as `COMPLETE` only at its
frozen internal boundary. Completion evidence is the
[`P5B2 semantic-release decision-store and capacity/GC` receipt](receipts/p5b2-semantic-release-decision-store-capacity-gc.md),
binding PR #76, PR #77, PR #79, and PR #83. PR #79 corrects held
generation-directory rebinding; PR #83 separately corrects top-level
`semantic-release-decisions` namespace rebinding.
`SemanticReleaseDecisionStore` is the sole owner of the external private
`semantic-release-decisions/<generation_id>/<decision_request_sha256>.json`
namespace. Mode-`0700` directories, one single-link mode-`0600` canonical binding
file, descriptor-relative no-follow traversal, and fail-closed bounded
enumeration prevent caller-selected paths, aliasing, unsafe entry types, and
generation-only overwrite. The binding's exact closed member sets, canonical
field-result order, nonrecursive full-result and completed-binding digest
preimages, and request-derived identity remain those frozen in the canonical
semantic contract.

The prerequisite caps one binding at 25 MiB, one generation at 64 bindings, and
one workspace at 4,096 bindings. Those count caps are independent store limits,
not new or repurposed `CapacityPolicy` fields. Decision-store bytes are charged
against existing global/workspace byte ceilings and filesystem reserve while
existing unconsumed durable byte reservations remain charged in that arithmetic.
The store performs bounded capacity enumeration before classification may begin
and again immediately before installation. Capture uses existing shared
registry, exclusive workspace, then shared target-generation locks. Final
global/workspace byte capacity, reservation, reserve, request-path,
candidate-byte, mode, size, and digest revalidation uses exclusive registry,
exclusive workspace, then the same shared generation lock and retains that
composition through install-once and reopen.
Byte-identical replay is no-write success; same-path different bytes conflict;
partial, unsafe, unreadable, different, or ambiguous state is commit-unknown.

Nonempty decision state aborts the shared workspace reachability proof before a
successful GC preview or plan and therefore blocks downstream execute,
reconcile, and purge. It adds no token to the current public protection-reason
vocabulary and grants no GC mutation or canonical decision-state deletion
authority. This prerequisite owns no decision-request creation,
classifier or policy composition, terminal release decision, live policy
selection, omission, projection, public CLI/schema/runtime receipt,
provider/backend, network, cleanup, deletion, quarantine, repair, rollback,
publication, parent completion, or successor
authority.

The encompassing unnumbered P5B2 semantic-content release/DLP decision child is
contract-frozen only and remains `WAITING`. Its sole entry is the accepted exact
promoted visible-current terminal plus the separately accepted trust-root,
policy-authority provisioning mechanism, and decision-store/capacity/GC
prerequisites. It still requires a separately provisioned stable current
`ACTIVE` operator policy-authority record. It captures the private target-owned
semantic inputs under existing read authority, classifies only node labels,
optional node rationales, and hyperedge labels outside the coordination locks,
then reacquires the locks and rejects any authority or byte drift before one
install-once write.
The decision request stores only the locked semantic-input byte count and
SHA-256; it never embeds semantic-input content. Classification uses the exact
captured bytes, and final locked reread must reproduce both values.

Classification, policy provisioning, and the release decision remain separate
authorities. The decision
composition consumes but cannot alter or override the repo-owned installed
trust root above. The separately accepted durable operator policy-authority
store can select
one stable current `ACTIVE` revision; older-revision bindings are
historical candidates only. The authority embeds a closed version-1
coverage-sufficiency declaration whose release context and exact selected-profile set
must match the authority, and whose digest is included in the policy bytes and
decision request. The deterministic-pattern-only classifier reports only
`NO_MATCH`, `MATCH` with `utf8_lex_v1`-sorted unique stable category IDs, or
`INDETERMINATE`. `utf8_lex_v1` compares unsigned canonical UTF-8 bytes with a
shorter exact prefix first and governs profile, category, rule, and canonical
result-array ordering without locale or runtime collation.
Every allow-capable policy requires `core_secrets.v1`; jurisdiction, domain,
and organization profiles are active only when selected by current authority.
That policy maps `(field_type, category_id)` pairs
to `ALLOW_FIELD`, `OMIT_RATIONALE`, or `REJECT_RELEASE`. `INDETERMINATE`
rejects; `NO_MATCH` allows a field only under the exact coverage-sufficiency
declaration; unknown or unmapped `MATCH` pairs reject. It may produce only
`ALLOW_UNCHANGED`,
`ALLOW_WITH_OMISSIONS`, or `REJECTED`. No environment, provider, model, network,
credential, live catalogue, or fallback selects authority.

The decision child may consume only an exact binding installed by the separate
prerequisite at the request-derived path. That binding commits to exact promoted
authority, installed bundle, current policy revision, inputs, complete bounded
field results, and terminal outcome without duplicating semantic prose. A
redacted internal proof
contains only coordinates, authority/result/binding digests, counts, and
outcome; omission locators remain exclusively in the mode-`0600` binding. The
lifecycle journal, staged-build state, generation receipt, public schemas, and
runtime receipts do not become release authority. The freeze stops before
omission execution, graph construction, projection, query, public semantic sync,
publication, readiness of the encompassing child, or acceptance of that child.
P5 and P5B2 remain `IN_PROGRESS`; the bounded trust-root and policy-authority
provisioning prerequisites and the decision-store/capacity/GC prerequisite are
accepted `COMPLETE`; live operator policy selection, classification composition,
the encompassing release/DLP decision, remaining P5B2 work, and P5C remain
`WAITING`; H3 remains `DEFERRED`; no later successor is `READY`.

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

The accepted policy-authority provisioning prerequisite reserves only this
fixed external workspace namespace:

```text
<external_state_root>/workspaces/<repo_uuid>/
  semantic-release-policy-authority.json
  semantic-release-policy-authority.previous.json
  semantic-release-policy-authority.pending.json
```

All three names contain the same canonical internal authority-record format;
pending is the exact candidate, not a separate intent schema. The private store
alone constructs those bytes from closed structured input and owns recovery.
Existing shared registry-before-workspace locking stabilizes reads and
read-only recovery projection; the exclusive pair serializes mutation and the
fixed 256 KiB peak. Stable read never recovers; exact recovery may finish
only the original `ACTIVE` selection transaction. This namespace does not
create a public record, receipt, lifecycle journal, decision binding, or live
policy and does not authorize revocation, reactivation, rollback, arbitrary
repair, deletion, or GC.

The accepted `COMPLETE` decision-store and capacity/GC prerequisite owns this
additional external workspace namespace:

```text
<external_state_root>/workspaces/<repository_uuid>/semantic-release-decisions/
  <generation_id>/<decision_request_sha256>.json
```

The private store alone owns the mode-`0700` directory tree and canonical
single-link mode-`0600` binding. Generation and request-digest components are
validated canonical identity, not caller paths. The namespace is outside the
sealed generation, is included in existing capacity and reserve accounting, and
blocks workspace-wide GC preview and planning whenever any decision state is
nonempty. A
safely observed absent top-level namespace is the zero-binding initial state and
may be created only by an exclusive first-boundary rename from the fixed
`semantic-release-decision-publication` slot. That separate mode-`0700` slot is
non-authoritative publication construction, not canonical decision state and not
lifecycle `staging`; it contains at most one bounded build/ready/cleanup state,
with a 4 KiB manifest and a 256 KiB physical-reserve allowance. Validated retry
cleanup is confined to this slot and never removes canonical state. No canonical cleanup,
repair, deletion, quarantine, rollback, public reason token, or public exposure
is authorized by reserving the namespace.

The accepted host-agent worker uses this private staging
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
receipt. The accepted worker does not consume or clean it. The accepted
[handoff boundary](semantic-sync.md#p5b2-semantic-result-handoff-and-sealed-input-finalization)
owns only the bounded successor behavior below.

The accepted handoff adds this private layout under the same external root:

```text
<external_state_root>/workspaces/<repo_uuid>/semantic-staging/
  handoffs/<target_generation_id>/<structural_request_sha256>.json
<external_state_root>/workspaces/<repo_uuid>/staging/<target_generation_id>/
  graphify-out/semantic-inputs.json
```

The handoff is one install-once, canonical
`graphify.workspace.semantic_result_handoff.internal` format-version-1 record.
It embeds the exact accepted begin request, complete canonical worker transcript,
observed process exit, reopened result-binding envelope, structural request,
the new target generation, the distinct current certified source generation
when any completion is carried, queue/reconciliation snapshot, and deterministic
path-keyed materialization for every desired work item. Each result records
whether it is fresh or carried as hop-local wrapper provenance; carried begin,
session, and result-binding evidence remains byte-identical. The
target-generation-owned file is an exact byte-for-byte copy. Both are private
`0600` regular files reached through
no-follow contained `0700` directories. The handoff remains recovery evidence
outside generation staging so a successor fence may safely reset an interrupted
staging tree.

`GenerationStore` remains the shared-capacity owner. Its trusted usage scan must
include every retained handoff for this preflight and all later allocations,
coalescing handoff bytes with generation, staging, or quarantine bytes under the
same repository/target-generation key and counting that target once. A
handoff-only target consumes one generation slot. Unsafe or unstable handoff
usage fails closed, and the bytes remain counted until separately authorized
cleanup or GC removes them.

`UPSERT` and `DELETE` apply from an empty path map in normalized-path byte order
and ascending desired revision. `UPSERT` replaces the exact path slot with its
validated payload; `DELETE` removes the slot but remains in the result evidence.
The record rejects missing, duplicate, stale, foreign, conflicting, or extra
results and recomputes the final materialized array. It does not merge entities,
deduplicate IDs, invoke the graph engine, or make the content query-visible.

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

The accepted certification-finalization composition narrows that existing P5A/P3
order to one exact staged successor. Before acquisition it reopens the canonical
`SyncRequest`, target generation, `StructuralBuildRequest`, staged `COMPLETE`
record, payload inventory and manifest, handoff and target-owned semantic-input
bytes, equal queue sealed-input digest, pointer CAS, compatibility, and current
registry/source/operation/policy evidence. A staged state other than exact `COMPLETE` is
not admitted to the forward-mutating lane. An exact already-`CERTIFIED` replay
with no retained cleanup grant may only return the same proof through read-only
verification; `PROMOTED`, foreign, mismatched, or ambiguous state is outside
this child.

`acquire_staged_recovery()` is the only lease entry and must return the
request-bound `BUILD` operation. Its accepted grant supplies the new current
operation epoch and fence; the prior `COMPLETE` epoch and fence remain frozen
staged-completion evidence and are not copied forward. `allocate()`,
`prepare_staged_build()`, and `complete_staged_build()` may only reconstruct the
same reservation/allocation/completion authority. The `COMPLETE` branch performs
inventory adoption; it never resets the staging tree, invokes the adapter, or
rewrites `graphify-out/semantic-inputs.json`.

Two fresh equal typed source observations must match the structural request and
sealed reconciliation. `SemanticQueueStore.certification_view()` must bind the
same manifest and report `semantic_completeness="complete"`; its values form the
exact `CertificationRequest` with the selected compatibility and the existing
three validation markers. `GenerationStore.certify()` then reobserves source,
revalidates that view under the workspace lock, installs or reopens the immutable
generation/request/view/manifest binding, and only afterward takes the existing
generation lock. Under that lock it recovers or installs the existing receipt,
final generation, and `CERTIFIED` journal transition. It next clears the exact
reservation, reopens the installed generation, and commits the staged
`COMPLETE` to `CERTIFIED` transition with the same request, manifest, and receipt
digest.

Before an immutable binding exists, any queue, source, policy, pointer, epoch,
request, manifest, inventory, target, or compatibility drift blocks new
certification. A durable exact binding freezes its older queue view; a durable
exact receipt freezes the subsequent generation/journal authority. Later state
may not be adopted into either, but the existing recovery paths may finish those
same bytes. Binding, receipt, staged-state, reservation-clear, and lease-release
uncertainty resolves only through exact durable reread. No recovery path infers
success, abandons the target, resets staging, or deletes handoff evidence.

The only post-`CERTIFIED` mutation is terminal grant cleanup. If the exact
terminal proof exists except that its paired request-bound `BUILD` lease and
staged-attempt digest remain, a dedicated cleanup acquisition adopts that
persisted digest. It reopens the same live grant for the current OS owner or,
only after expiry or reboot, advances the non-resetting fence to a replacement
cleanup grant. The composition verifies under that grant, releases it, and
rereads absence. It does not re-enter `GenerationStore.certify()`, and a
replacement cleanup epoch or fence never changes the receipt or staged
certification evidence. Foreign live ownership, a different operation or
attempt, or ambiguous authority fails closed.

The final proof reopens the staged `CERTIFIED` record, installed receipt and
payload, immutable semantic binding, matching `CERTIFIED` journal event,
reservation absence, unchanged pointer boundary, and exact lease-owner/fence
absence. The child stops after that proof. Promotion, pointer movement, content
release, graph/query projection, public semantic sync, service, publication,
and every successor remain separately owned.

The accepted semantic worker retains the registry-before-workspace order
for each queue mutation but keeps one process alive between mutations so the
same OS-derived owner and fence remain current. Its begin request supplies an
absolute deadline and explicit registry, source, operation, migration, queue,
and watermark CAS. The deadline begins after the canonical begin frame is
accepted and bounds `work`/`checkpointed` stdout delivery plus both the start
and observed return of completion or a caller-requested failure. Every result
frame also has a five-second delivery deadline; terminal delivery grants no
mutation or lease authority. After expiry, checkpoints and completion are
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

The accepted handoff first validates every worker session and result envelope,
then takes registry before workspace lock and captures one exact current
structural request, the existing sync request's new target generation, the
distinct current certified source generation when carried evidence is used, and
the completed semantic-required reconciliation. It installs and reopens the
immutable target-generation-bound handoff before `request_staged_build()`. A
fault before that install leaves no consumption authority; a lost terminal
remains unconsumable under the accepted worker contract. Exact same-byte replay
with the same source/target identities is idempotent. A different target,
source/target swap, different carried source, or different, unreadable, unsafe,
or ambiguous bytes are a conflict or commit-unknown and block staged request
creation. Target nonexistence is required only for first handoff installation;
after exact installation, an existing target is admissible only through the
same request-bound `REQUESTED`, `PUBLISHING`, or `COMPLETE` staged state. A
certified target or any unbound or mismatched target remains a conflict.

After the request is durable, the ordinary request-bound `BUILD` acquisition,
allocation, and staging preparation receive the same target generation
unchanged. The structural adapter writes its output, the exact handoff bytes are
installed and reopened as
`graphify-out/semantic-inputs.json`, and two fresh equal source observations
precede `complete_staged_build()`. Under the same current grant, the composition
recomputes the returned sorted payload manifest, revalidates the exact handoff,
queue, generation, request, source, and epochs, and calls
`bind_sealed_inputs()` once. Exact same-digest replay is idempotent; a different
existing digest fails closed. The reopened queue binding is the terminal boundary
for this child.

`REQUESTED` and `PUBLISHING` recovery reuse the existing staged barrier for the
same repository, target, and structural request and may reset only unsealed
target-generation staging under a successor fence; the external handoff survives
and is deterministically recopied. `COMPLETE` recovery adopts only that exact
durable inventory manifest. Bind uncertainty adopts only a
locked reread proving the deterministic post-bind queue state, or retries from
the entire exact unchanged pre-bind queue snapshot under the same live grant.
The lifecycle composition owns only best-effort deletion of an original
consumed worker envelope after that reopened binding. The worker, queue, and
generation store do not clean other semantic staging; stale, conflicting,
orphaned, legacy, or commit-unknown evidence remains retained for a separately
authorized semantic-staging repair or GC lifecycle. No cleanup can delete the
last transcript, envelope, handoff, generation copy, manifest, or queue-binding
evidence.

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

The accepted decision-store prerequisite adds no lease domain. Its
pre-classification capacity snapshot uses shared registry, exclusive workspace,
then shared target-generation locks. After that snapshot, the caller releases
all three locks and performs classification outside them. Its install boundary
then reacquires exclusive registry, exclusive workspace, and shared
target-generation locks in that order, revalidates global/workspace counts and
bytes, durable reservations, filesystem reserve, GC eligibility,
request-derived identity, and candidate bytes, and retains that lock composition
through install-once reopen. Classification itself remains outside the store
prerequisite.
