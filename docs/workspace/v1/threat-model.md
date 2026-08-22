# V1 support and threat model

## Supported boundary

V1 targets one non-elevated user on macOS with lifecycle state on local APFS.
State, staging, generations, pointer temporaries, and recovery records must use
supported same-filesystem persistence primitives.

Detectable unsupported conditions fail closed:

- NFS, SMB, FUSE, and other network state roots;
- Windows or Linux lifecycle operation;
- root/elevated execution;
- shared multi-user state; and
- pre-login/system-daemon operation.

Watch supervision is login-scoped. Automatic online GC and historical certified
query are deferred. P3 requires an explicit validated runtime `CapacityPolicy`
for every allocation and GC operation. It has no defaults and adds no public
config field; operators must supply global/per-workspace byte and generation
limits plus a free-space reserve threshold.

## Durability claim

The P2/P3 persistence implementation handles process death,
injected short writes,
`EINTR`, `ENOSPC`, `EDQUOT`, `EIO`, failed sync, failed rename, and clean reboot
after a successful durable-write completion. Later lifecycle records must reuse
the same boundary. Sudden hardware power-loss durability is not claimed.

P3 executes generation, journal, pointer, and explicit offline-GC transitions.
P4 adds read-only adapter/freshness operations and external-output structural
builds. P5A adds only durable semantic desired-work reconciliation, fenced
claims, bounded retries/dead letters, and stable-watermark certification.
P5B2a adds only the initial enrollment and verified clone/fork adoption command.
P5B2b0 adds internal request-bound staged-build recovery and terminal stale
abandonment. P5B2b adds only provider-neutral structural
`workspace sync --code-only`. Separately accepted P5B2 children expose only the
bounded commands listed in the [workspace contract](README.md), including the
host-agent semantic worker. Full semantic sync, every unlisted public workspace
command, service, installation, route, publication, and live-cutover transitions
remain absent.

The accepted unnumbered P5B2 semantic-result handoff and sealed-input
finalization child adds no public command or release route. It preserves already
accepted worker evidence in one private immutable handoff, copies those exact
bytes into request-bound generation staging, completes the existing payload
manifest, and binds that digest to the exact queue reconciliation. Its evidence
is bound by the
[P5B2 semantic-result handoff receipt](receipts/p5b2-semantic-result-handoff.md),
and it stops before certification or any content release.

The unnumbered P5B2 semantic-generation certification-finalization child is
implemented and accepted only at its frozen internal boundary, with evidence
in the [accepted completion receipt](receipts/p5b2-semantic-generation-certification-finalization.md).
It composes the accepted handoff's reopened request-bound
staged `COMPLETE` state and equal queue sealed-input digest with existing
semantic certification-view, immutable binding, generation certification,
staged-recovery, and lease authorities. The accepted implementation adds no
public command, schema, runtime receipt, release route, or successor-readiness
claim. Its stop is the same target durably proved staged `CERTIFIED` and
verified against its exact binding, receipt, journal, reservation, pointer, and
released recovery grant; it still performs no content release, promotion, or
pointer movement.

Enrollment creates the durable per-workspace fence floor. Losing all initialized
workspace records is treated as corruption, never as permission to restart the
counter. Lease ownership is bound to OS-owned boot and process-start identity;
caller-provided identity cannot assert a reboot or reuse a live PID.

## Protected risks

V1 is designed to protect against accidental corruption, crashes, stale and
concurrent processes, untrusted corpus contents, path tricks, secret leakage,
and artifact mismatch/substitution relative to a locally frozen trusted
manifest.

For installed executable operation, the accepted semantic-release trust-root
prerequisite also treats package-local bytecode caches as untrusted. The private
source-executed `_graphify-semantic-authority` and
`_graphify-mcp-semantic-authority` scripts are the pre-import
bootstrap: their POSIX shell prelude resolves the installed script target and
executes the installed Python with Python environment configuration ignored,
site initialization disabled, automatic user-site startup imports disabled,
safe-path mode enabled, and bytecode writes disabled before Python startup hooks
can run; then the Python body sets a fresh private package-external
`sys.pycache_prefix`, keeps bytecode writes disabled, and may add the installed
script-prefix package root, or a PEP 610 editable source root recorded by a
`graphifyy` direct URL in that same script prefix, explicitly before any
Graphify module import. Direct library imports that bypass this bootstrap are
not semantic-release decision authority. Across every supported script-prefix
layout, the bootstrap requires exactly one real-path-distinct physical or PEP
610 editable Graphify owner; a missing owner cannot fall through to an
interpreter installation, and multiple owners are ambiguous and fail closed.

This does not make the package its own first-instruction trust anchor. The
CPython executable and standard library, an installer that verifies the
selected wheel and its `RECORD`, the installed bootstrap source, and
post-install filesystem protection of bootstrap and package-source bytes are
explicit trusted-computing-base prerequisites. The wheel `RECORD` binds the
bootstrap and classifier source. Hostile package-local bytecode is excluded;
hostile source, launcher, interpreter, or standard-library substitution must be
rejected by that external installation/runtime integrity boundary.

The public `graphify` and `graphify-mcp` console entry points remain ordinary
cross-platform commands and are never semantic-release decision authority. On
the supported POSIX boundary, authority installation or requalification uses
`uv tool install --force --reinstall --link-mode copy graphifyy` and verifies
the resulting wheel/`RECORD`, bytes, modes, and link counts. Hardlinked or
wrong-mode final files fail closed rather than weakening the single-link and
exact-mode trust root.

State-root policy requires expected ownership, `0700` directories, `0600`
mutable records, exclusive creation, safe umask/ACL behavior, descriptor-relative
no-follow traversal, regular-file-only payloads, path containment, and rejection
of links and special files.

Registration requires an explicit `enroll` or `adopt` verb, a canonical repo
UUID, an expected registry revision, and action-matching operator authorization
on standard input. Source discovery scrubs ambient `GIT_*` overrides and reads
the repo policy through descriptor-relative no-follow traversal. The command
cross-checks the requested UUID before registry mutation, exposes no source or
state path, authorization detail, credential, or exception text, and maps stale
CAS, collision, authority, corruption, and runtime failures to stable codes.

Code-only sync accepts no provider, model, endpoint, credential, or path on
argv, stdin, or ambient configuration. A bounded canonical request must carry
every capacity and registry/source/lease/pointer CAS input explicitly;
duplicate, unknown, noncanonical, inferred, or stale authority fails before
sync mutation. The request digest binds the durable staged lifecycle. Engine
scratch and output remain beneath generation-owned external staging, provider
environment cannot select a backend, and network access is outside the
command's authority. Public receipts and classified failures omit exception
text, secrets, credentials, and absolute private paths.

P5B2b0 installs the exact staged request before `BUILD` lease acquisition and
treats every nonterminal staged record as a barrier to ordinary workspace
mutation. The request, lease, staged state, workspace, and generation identities
must agree before recovery consumes source payload content or commits lifecycle
state. Selected-source discovery may first read the recorded checkout and Git
metadata, but remains read-only. Each live staged lease also binds the caller
attempt SHA-256, so a second caller in the same process cannot inherit commit
authority from process-owner equality alone.

Recovery may finish exact independently durable certification. Terminal
abandonment instead requires canonical drift in the active source, migration
epoch, pointer CAS, compatibility, semantic source epoch, or trusted source
observation. Source unavailability alone is insufficient, and a durable
abandonment intent precedes destructive cleanup. Existing-only status/doctor
inspection exposes a bounded staged summary; every nonterminal lifecycle is a
visible recovery barrier with `safe_to_query=false` and an exact-resume action.
Terminal promoted or abandoned records do not create a false barrier, while
corrupt or contradictory staged state fails closed.

The public pointer-repair dry run is an existing-only, no-write inspection: it
opens no missing lock or state path and allocates no lease or fence. Its bounded
canonical output exposes only verified generation references, decision facts,
and redacted classifications; it excludes authorization, owner/fence data,
paths, raw records, environment values, and errors. Execute requires the
SHA-256 of those exact preview bytes (including the final newline), canonical
`REPAIR_EXECUTE` authorization, fresh CAS-bound `REPAIR` authority, and a
second exact decision under the required locks before mutation. This blocks a
stale preview, substituted candidate, authority drift, or post-preview pointer
change from selecting a repair target. `PointerStore` verifies receipts and
payloads, preserves monotonic revision evidence, and quarantines only corrupt
generations not referenced by the repaired pointer.

Repair is not recovery authority for every external-state fault. A valid GC
intent remains an explicit GC-reconcile barrier. Nonterminal/corrupt staged
state, semantic-queue corruption, registry or lease corruption, and unsafe
paths fail closed with their corresponding status action. A commit-unknown
repair does not permit replay: status inspection plus a fresh preview/request
pair is required. Doctor remains a read-only observer and cannot turn an
inspection action into a mutation.

Operational corpus processing will use a cleaned allowlisted environment,
network denial by default, bounded CPU/memory/file/time resources, read-only
source, staging-only writes, and host-agent instruction/data separation.
Backend endpoints and credentials are operator configuration, never repo policy;
secrets are excluded from argv and persisted state.

The accepted host-agent semantic-worker child narrows that boundary further.
Its public executable is `graphify`; its only frozen argument vector
after that executable is `workspace semantic-worker --stdio`, making the full
invocation `graphify workspace semantic-worker --stdio`. The caller must state
an already-active host agent explicitly; the transport passes no named backend
and never imports or calls `graphify.llm` provider discovery or dispatch.
Credentials, provider files, ambient backend variables, network availability,
and an absent API key are neither capability nor authority. Source bytes are
read-only untrusted data and are never returned in public result frames.

One long-lived process retains one OS-derived semantic owner and fence across
claim, bounded checkpoints, the terminal request and queue transition, and
release. This prevents separate subprocesses, a replaced caller, source
activation, migration, lease expiry, or a successor attempt from inheriting
semantic commit authority.
Successful `UPSERT` output is treated as untrusted until the existing semantic
fragment validator is surrounded by the worker's closed nested schema and the
validator receives retained exact decimals through a lossless canonical-number
encoder while the sanitizer runs through a preflighted linear `rationale_for`
index. Unknown keys, non-work-path provenance, noncanonical fixed-point scores,
oversized semantic text, dangling references, and projected rationale or payload
amplification, including duplicate hyperedge members, are rejected before
sanitizer allocation. `DELETE` accepts only the exact kind-only tombstone. An
immutable canonical result envelope is then
installed under private external workspace staging, its exact bytes and digest
are reopened and verified, and that digest is persisted in and revalidated
against the live claim checkpoint. The source digest or absence is checked a
final time immediately before queue completion, and the public success frame is
withheld until the exact semantic lease is provably released. Queue completion
before those checks is forbidden. Claim admission and every later queue mutation
project the maximum mandatory result checkpoint before enforcing canonical-byte
capacity, so an accepted claim cannot have its binding stranded by ordinary
queue growth.

The private envelope admits source-derived text only in the schema's bounded
semantic `label` and sanitizer-produced `rationale` fields; it has no separate
raw-source or arbitrary metadata field. That private `0600` payload is not
claimed to be secret-free or non-verbatim. Public receipts contain no semantic
text, credential, private absolute path, raw exception, provider/model data, or
lease owner or fence detail. Same-path different-byte installation, unsafe
paths, links, special files, oversized fragments, identifier/path tricks,
unknown fields, stale claims, and epoch drift fail closed. An exact
different-byte binding under a current claim is a non-retryable queue failure;
unreadable or ambiguous staging state is commit-unknown. Orphan result staging
is not cleanup authority. If queue completion may have committed before its
terminal public frame, including uncertainty during the intervening lease
release, the absence of a durable queue/result association is reported as
commit-unknown rather than inferred success.

Only a completed source observation can prove content drift; an incomplete
observation is retryable `source_unavailable`. Pre-mutation registry or lease
corruption uses existing invalid-state routes; post-mutation ambiguity is
commit-unknown. Catchable interruption after `complete` remains classifiable
until queue mutation begins; an accepted `fail` retains its caller classification.
A non-draining stdout reader cannot retain a live claim indefinitely: every
frame has a five-second delivery deadline, and `work` or `checkpointed` uses the
earlier absolute work deadline. Terminal delivery grants no mutation or lease
authority. A stdout record becomes a frame only after complete
newline-terminated delivery.
A partial trailing record is not a frame, and a `completed` terminal requires
exit 0 to become public authority.

The semantic-result handoff treats the captured session, original worker
staging, carried completion, queue, structural request, source evidence, and
generation staging as mutually untrusted until all exact bindings are
recomputed. It accepts one result per desired work only when the original
canonical begin request, complete stdout transcript, observed process exit 0,
one final completed terminal, and reopened result envelope agree. A carried
completion must retain the same format-version-1 evidence in the verified
current certified source generation selected by the structural request's
pointer/receipt CAS. That source is recorded separately from the new target
generation derived from the existing sync request. Source/target equality,
exchange, or mismatch, arbitrary history, and orphan scans are forbidden.
The new wrapper records carried provenance without changing the accepted begin,
session, or result-binding evidence copied from the source generation.
Missing, duplicate, stale, foreign, conflicting, extra, legacy-unindexed, or
manually inferred results fail before staged request creation.

The immutable handoff path is derived from the repository, target generation,
and structural-request digest and uses contained descriptor-relative no-follow
traversal, `0700` directories, and a single-link `0600` file. Its canonical
bytes include every admitted result payload, each result's fresh-or-carried
origin, and the optional carried-source generation, and are bounded by the
explicit request reservation and capacity policy. Same bytes with the same
source/target identities are idempotent; different, unreadable, unsafe, or
ambiguous bytes fail closed. Because the handoff is installed before the
staged-build request and remains outside target-generation staging, a successor
`BUILD` fence can discard interrupted unsealed staging and reconstruct it
without consuming an orphan result.

Retained handoffs may not escape capacity accounting. The shared trusted usage
scan counts every handoff byte under its repository/target-generation key for
this preflight and every later allocation, combines it with any staging,
generation, or quarantine bytes for that target, and counts the target once.
Handoff-only targets consume a generation slot. Unsafe or unstable scans fail
closed, and retention remains charged until authorized cleanup or GC.

Generation materialization stores the exact handoff as private
`graphify-out/semantic-inputs.json`. Deterministic per-path, ascending-revision
`UPSERT` replacement and `DELETE` removal prevent path-order or last-writer
ambiguity; operation/payload mismatch and any nonreproducible final set are
conflicts. This stage does not invoke entity deduplication, graph merge, query,
or a provider. Existing inventory rules reject links, special files, extra
roots, and payload drift before staged completion. The queue digest is bound
only after the completed manifest, handoff, generation copy, source authority,
and exact reconciliation are revalidated under the current `BUILD` grant.

Crash safety follows explicit durable boundaries. An uncertain handoff install,
staged completion, or queue bind is adopted only by an exact no-follow or locked
reread of the expected bytes/state. Proven unchanged pre-commit state may retry
only while its authority remains current; every other outcome is commit-unknown.
Target nonexistence is a first-install condition. Later target state is accepted
only as the exact request-bound `REQUESTED`, `PUBLISHING`, or `COMPLETE` recovery
for the same repository, target, and request; a certified or mismatched target
is not replay authority.
Cleanup occurs only after the handoff, generation copy, staged manifest, and
queue digest agree and never removes the last recovery copy. Conflicting, stale,
orphaned, legacy, or uncertain staging remains retained for separately
authorized inspection, repair, or GC.

The handoff and generation copy remain private and may contain bounded labels or
rationales derived from source content. Worker sanitization, canonicalization,
hashing, staged completion, and sealed-input binding do not prove that prose is
secret-free, non-verbatim, publication-safe, or query-safe. This child therefore
creates no content-release, certification, promotion, pointer, or public-output
authority.

The frozen certification-finalization contract treats the staged record,
payload inventory, handoff, generation-owned semantic-input copy, queue,
source observations, active-source state, structural request, pointer CAS,
capacity reservation, certification binding, generation receipt, journal, and
lease as mutually untrusted until their exact identities and digests are
reopened and cross-checked. A same-generation substitution therefore cannot
gain authority from a matching pathname, directory presence, watermark, sealed
digest, receipt, or journal head alone. The target must bind one complete
structural request, one exact payload inventory and manifest, one complete
semantic-required reconciliation, and the same handoff and generation copy.

Recovery fencing prevents an attacker or stale process from turning sealed
staging into mutable build input. Only the same request-bound `BUILD` recovery
grant may continue the exact `COMPLETE` target. The successor grant's current
operation epoch and fence replace mutation authority but do not rewrite the
historical `COMPLETE` record. `allocate()`, `prepare_staged_build()`, and
`complete_staged_build()` may reconstruct existing wrappers only; they cannot
reset staging, rerun the adapter, recopy or modify semantic input, append
payloads, select another target, or silently adopt different sealed bytes.
Registry-before-workspace and workspace-before-generation-lock ordering prevents
the recovery composition from weakening existing deadlock and authority
boundaries.

The semantic certification view closes the remaining queue/build
time-of-check/time-of-use window. Two fresh equal typed source observations,
`semantic_completeness="complete"`, the exact queue revision and canonical-state
hash, and the staged manifest's equal sealed digest must all agree. The store
then installs and reopens the immutable target/request/view/manifest binding
under the workspace lock before a generation lock or receipt can become
authority. A preseeded receipt, different binding bytes, foreign request,
changed queue before binding, incomplete reconciliation, or different manifest
cannot convert staged bytes into a certified generation.

Crash recovery is phase-sensitive and non-destructive. Before the immutable
binding exists, any queue, source, policy, pointer, epoch, operation,
compatibility, request, target, handoff, inventory, manifest, sealed-input, or
observation drift blocks certification. After the exact binding exists, a later
queue or policy state is never substituted into it; existing generation
recovery may only finish the already-bound request and view. Receipt install,
staging-to-final movement, journal append, reservation clear, staged
`COMPLETE`-to-`CERTIFIED` transition, and lease release are adopted only through
exact durable reread. Both/neither generation locations, mismatched or
unreadable binding/receipt/journal state, an unrelated reservation, a
replacement lease, or any ambiguous suffix is commit-unknown rather than
inferred success.

A process may die after durable `CERTIFIED` and before releasing its paired
`BUILD` grant. The next invocation may not treat that retained lease as forward
certification authority, but it also must not self-deadlock forever. A dedicated
cleanup-only recovery requires the exact terminal proof, exact request-bound
`BUILD` operation, and paired persisted staged-attempt digest. It reopens the
same live grant only for the current OS owner or replaces it only after normal
expiry/reboot proof, then verifies, releases, and rereads absence. Live foreign
ownership, changed attempt or operation, replacement ambiguity, and unreadable
state fail closed. A cleanup replacement's epoch and fence are never written
into the certified receipt or staged record.

Exact same-byte/state replay is idempotent. A caller starting from the full
exact terminal proof with cleanup-grant absence may only verify it read-only; a
different certified target or any promoted target is not mutable replay
authority. No recovery path
deletes or resets staging, the handoff, semantic-input copy, immutable binding,
receipt, journal evidence, or installed generation, and none compacts or
rewrites the queue. The final proof still leaves retained labels and rationales
private and untrusted: certification establishes integrity and internal
completeness, not DLP clearance, query safety, publication fitness, or semantic
correctness.

P5A treats semantic work and its outputs as untrusted until exact reconciliation
and generation sealing. A worker cannot claim work without an accepted
`SEMANTIC_CLAIM` lease and an explicit live capability decision. At the claim
mutation boundary, the queue resolves the registry-selected active source,
safely reads and validates its workspace configuration, and requires the
canonically revalidated caller configuration to match it exactly. The decision
is then derived from that active-source policy and the caller-stated live host-
agent/named-backend inputs. A foreign or same-UUID relabeled policy and an
arbitrary decision object are never authority. Host-agent use must be stated by
the caller; a headless backend must be explicitly named, policy-allowlisted, and
permitted network egress. Ambient provider or credential environment variables
are not capability or authority. Semantic-grant mutations retain the current
registry lock while nesting the workspace lock. Lifecycle queue mutations use
the normal stable-registry-snapshot then workspace-lock path, where the live
lifecycle lease excludes activation. An activation before claim validation
therefore makes the old lease stale instead of authorizing work from retired
policy. The active-source revision that produced desired work is persisted with
the queue, and retained work requires a new exact reconciliation after activation.

Claim IDs bind the workspace UUID, desired work, owner, fence, source revision,
and operation and migration epochs. This prevents a stale worker, expired
claim, cross-workspace collision, or replaced desired revision from committing
semantic completion. Deterministic operation
rotation prevents one operation class from starving the other. Explicit queue
item/byte limits, retry budgets, stable error classifications, and durable dead-
letter state bound poison-work and capacity amplification. Compaction retains
the reconciliation and watermark proof, so tombstone deletion cannot turn an
empty queue into false completeness.

Certification defends against a queue/build time-of-check/time-of-use race by
requiring two equal typed source observations, durably binding the completed
watermark to the exact staged-payload manifest, capturing the queue revision and
canonical-state hash, then revalidating both under the workspace lock before
generation sealing. A changed queue, mismatched observation pair, or different
staged manifest blocks certification. Before any receipt is accepted, the store
installs an immutable internal binding from the generation and request to that
revalidated queue view and manifest. Request and staged-manifest validation run
before that immutable boundary, so malformed input cannot poison a reserved
generation. A caller-controlled staged receipt without the binding cannot
bypass queue authority or cause the binding to be created. The binding remains
recoverable after later queue advancement, while the receipt separately recovers
generation installation and journaling. The claim remains local crash durability
and stale-process fencing; it does not authenticate an uncompromised same-UID
worker or semantic backend.

The semantic-generation promotion and pointer-finalization child is implemented
and accepted only at its frozen internal boundary, with evidence in the
[accepted completion receipt](receipts/p5b2-semantic-generation-promotion-finalization.md).
Its frozen boundary protects the accepted certified target from substitution
during pointer movement. Fresh entry requires the predecessor's complete cross-record
`CERTIFIED` proof, unchanged request pointer CAS, absent target reservation, no
pending pointer intent, and absent certification `BUILD` authority. Only the
same request-bound staged-recovery path may acquire
`PROMOTE` for a new exact move or exact already-visible replay, or
`POINTER_RECOVERY` when durable pending intent remains from that same move.
Commit-unknown recovery may reopen only the exact persisted
promotion attempt and live grant, or replace that same attempt after
expiry/reboot proof, together with matching target-bound pointer and journal
residue. The pointer CAS binds target and receipt together with registry,
active-source, source, operation, migration, schema and fence authority.

Pointer recovery is not an arbitrary-history selector. Its locked plan must
derive the exact target and receipt from the matching pending or visible move,
have no quarantine or generic repair action, and rederive identically before
mutation. An unrelated current, prior, last-good, arbitrary certified, newer,
or substituted generation is not admissible even if individually valid.
Corrupt, stale, incompatible, or ambiguous pointer residue remains a barrier.
Existing registry-before-workspace and workspace-before-sorted-generation-lock
ordering prevents the contract from creating an inverse acquisition path.

Acquisition, pending intent, visible pointer, journal, staged `PROMOTED`
transition, and lease release are separate commit-unknown boundaries. Each is
accepted only through exact canonical reread, same-attempt live-grant recovery,
or exact persisted-attempt replacement after expiry/reboot. A visible target
without authoritative `PROMOTED` or exact recovery `REPAIRED` journal evidence,
a journal event with unresolved intent, a staged marker without the same visible
revision, or an absent lease after replacement authority is not success.
Terminal proof also reopens unchanged installed payload, receipt, handoff and
immutable semantic binding, so pointer movement cannot silently rewrite or
recertify semantic content. This contract makes no content-level DLP, release,
query-safety, correctness, or publication claim.

## Semantic-release bundle and deterministic-classifier trust-root threats

The unnumbered P5B2 semantic-release bundle and deterministic-classifier
trust-root prerequisite is implemented and accepted only at the frozen boundary
recorded in the
[acceptance receipt](receipts/p5b2-semantic-release-trust-root.md). Its bounded
threat surface is installed repo-owned package data plus the existing installed
executable bootstrap, not a workspace, operator, provider, network, or release
authority.

The trust root rejects caller or ambient substitution by anchoring one installed
`graphify/workspace/semantic_release_manifest.json` beneath the canonical
installed package root and digest-binding the classifier implementation,
byte-defined ABI, taxonomy, normalization, ruleset, required
`core_secrets.v1`, and every selectable profile. Unique sorted
relative-normal-form paths, descriptor-relative no-follow traversal,
single-link regular-file proof, exact read-only-compatible mode, size, digest,
and hard limits reject absolute, dot/dotdot, alias, symlink, hard-link,
special-file, unsafe-mode, oversized, foreign, duplicated, or unlisted
artifacts before use. Exact kind-specific ID/version inventory members also
reject a profile whose `profile_id`, terminal `.vN`, `profile_version`, or
manifest-bound digest disagrees with the selected coordinate.

Host-runtime disagreement is contained by the deterministic-pattern-only byte
ABI: exact pinned grammar and dictionaries, `utf8_lex_v1`, explicit ASCII fold
only for syntax names, and frozen comparison, match ordering, duplicate
reduction, and error semantics. Runtime Unicode categories, locale,
renormalization, ML, entropy scoring, provider output, and contextual judgment
cannot silently change a result. Unknown or unexecutable ABI, taxonomy,
normalization, ruleset, profile, category, or limit state produces
`INDETERMINATE`; `NO_MATCH` remains only a factual classifier outcome, never a
release-safety claim.

Taxonomy and selectable-profile identities remain distinct so installation of
a profile never activates it. `core_secrets.v1` is present as the required
explicit-evidence-only base profile, while jurisdictional, domain, and
organization profiles remain inert until a later operator authority selects
them. This subchild owns no policy selection, field composition, disposition,
decision binding, capacity/GC accounting, omission, projection, new public
transport, provider/backend, or publication behavior. Acceptance adds none of
those excluded behaviors and activates no successor.

## Semantic-release policy-authority provisioning threats

This separate internal unnumbered P5B2 prerequisite is implemented and accepted
as `COMPLETE` at the frozen private boundary. Completion evidence is the
[`P5B2 semantic-release policy-authority` receipt](receipts/p5b2-semantic-release-policy-authority.md),
binding PR #71, PR #72, and PR #74. Acceptance provisions no live policy
authority. The threat boundary is
`SemanticReleasePolicyAuthorityStore`, the exact operator-selected
structured input, and the three fixed private current/previous/pending records;
it is not release, decision, projection, provider, or publication authority.

Caller path or byte substitution is excluded because the store alone derives
canonical record bytes and owns three fixed descriptor-relative no-follow
mode-`0600` paths beneath the private workspace directory. Each stable record
is capped at 64 KiB, the authorization envelope at 16 KiB, and the fixed three
records plus one atomic temporary at a 256 KiB transaction peak. Unexpected
entries, unsafe links or modes, ambiguous enumeration, insufficient reserve,
oversized input, or inability to prove the peak fails before pending becomes
visible.

Policy substitution is contained by independent digest preimages. The body
digest excludes the authorization envelope and sibling envelope digest; the
completed envelope binds that body digest to the exact five operator fields and
sole `SELECT_SEMANTIC_RELEASE_POLICY` action; the sibling hashes the completed
envelope; and the complete-record digest remains external. Reusing operator
fields for different policy, coverage, profile, context, revision, predecessor,
or bundle bytes cannot preserve the same envelope or record digest.

Rollback and fork attacks fail through exact revision/digest CAS,
revision-plus-one advancement, predecessor-digest chaining, and the retained
exact revision-minus-one previous record. Genesis is possible only from
three-path absence.
Advancement is possible only from stable current `ACTIVE`; same-revision
different bytes, skips, missing or substituted previous, historical selection,
and `REVOKED` current all block. `REVOKED` remains recognized so consumers fail
closed, but selection authority cannot create revocation or reactivation and no
revocation token exists in this prerequisite.

Read races are contained by shared registry-then-workspace locks and final
current/previous/pending snapshot revalidation before stable read or read-only
recovery projection returns; stable read additionally requires pending absent.
Transaction tearing is contained by the exclusive forms of the same lock pair
and the existing durable order: pending, previous, current, pending clear.
Failure after pending may be visible is `CommitUnknown`; exact recovery must
revalidate the original authorization, installed bundle, candidate, CAS, and
one monotonic chain. A stale, corrupt, lower, skipped, divergent, foreign, or
`REVOKED` pending record is not cleanup authority. Only an exact pending
successor or byte-identical already-current candidate may be adopted and
cleared. Bounded orphan-temporary cleanup occurs under the same locks before
recovery projection; current and previous are never deleted or rewritten as
repair.

Byte-identical completed replay converges without a write; apparently equal
policy meaning with different canonical bytes or authorization conflicts. No
ambient default, environment, provider, model, credential, network, live
catalogue, newest-record enumeration, rollback, repair, GC, or caller-supplied
path may choose authority. The accepted prerequisite stops before live policy
provisioning, decision binding, classification composition, omission,
projection, public surfaces, publication, or successor activation.

## Semantic-release decision-store and capacity/GC threats

This separate internal unnumbered P5B2 prerequisite is implemented and accepted
as `COMPLETE` only at its frozen internal boundary. Completion evidence is the
[`P5B2 semantic-release decision-store and capacity/GC` receipt](receipts/p5b2-semantic-release-decision-store-capacity-gc.md),
binding PR #76, PR #77, and PR #79. Its threat boundary is limited to the private
`SemanticReleaseDecisionStore`, bounded
capacity and filesystem-reserve integration, install-once/replay behavior,
commit-uncertainty handling, and nonempty-state generation protection. It is not
operator policy, classification, terminal release-decision, omission,
projection, public, provider/backend, network, or publication authority.

Pre-visibility crash residue is confined to one fixed mode-`0700`
`semantic-release-decision-publication` slot outside both canonical decision
state and lifecycle `staging`. A 4 KiB manifest, one payload, one-state geometry,
and a fixed 256 KiB transient reserve allowance bound amplification. Only a
fully durable ready payload can reach the first missing canonical boundary, and
only through an exclusive no-overwrite rename. Retry rejects unknown names,
types, links, modes, identities, digests, sizes, or drift and removes only a
validated non-authoritative prefix/suffix after proving its destination absent
or byte-identical; no recovery path deletes or repairs canonical state.

Caller path and alias substitution are excluded because the store alone derives
the path from validated repository, generation, and complete canonical
decision-request-digest identity. Descriptor-relative no-follow traversal beneath
mode-`0700` directories admits only one single-link mode-`0600` canonical binding
at the terminal path. Absolute, empty, dot/dotdot, repeated-separator, backslash,
alternate, symlink, hard-link, special-file, unsafe-mode, foreign, duplicated,
or unexpected entries and unprovable containment or ambiguous enumeration fail
closed. Request addressing rather than generation-only or newest-file selection
prevents one authority revision or request from overwriting or masquerading as
another.

Canonical substitution is contained by the closed binding and nested member
sets, fixed `utf8_lex_v1` and field-result order, and nonrecursive digest
preimages. `full_result_sha256` excludes both digest members; the binding carries
that completed result digest but never its own digest; and external
`binding_sha256` hashes the completed canonical binding bytes. A field-value
digest covers only the exact captured UTF-8 field bytes. Alternate JSON quoting,
newline, salt, prefix, normalization, case conversion, member shape, or ordering
cannot preserve the same canonical identity. Raw semantic prose, matched
substrings, generated explanations, confidence scores, public source locations,
provider responses, and credentials are forbidden. Private entity/field
locators and unkeyed field-value digests remain an offline oracle risk if
same-UID mode-`0600` confidentiality is lost; private placement and no-follow
access reduce exposure but do not claim cryptographic secrecy against that actor.

Capacity amplification is bounded at 25 MiB per binding, 64 bindings per
generation, and 4,096 per workspace. Bounded no-follow enumeration stops at the
applicable maximum plus one and runs before classification and immediately before
install. Binding counts are governed by the fixed store caps, while
decision-store bytes participate in existing global/workspace byte ceilings and
filesystem-reserve calculation; existing unconsumed durable byte
reservations remain charged in that arithmetic. No count cap is mapped onto a
`CapacityPolicy` generation field. Unsafe or unstable usage, an exceeded cap, or
inability to prove namespace shape, counts, bytes, reservations, or reserve fails
closed rather than falling back to partial accounting.

The pre-classification snapshot uses shared registry, exclusive workspace, then
shared generation locks. Final install uses exclusive registry, exclusive
workspace, then the same shared generation lock so global accounting cannot race
across workspaces. Under that retained composition every request-path,
candidate-byte/digest, namespace, global/workspace count and byte total,
capacity, reservation, reserve, and GC eligibility state is revalidated before
install-once and exact reopen. Identical requests converge; same-path different
bytes conflict; distinct requests use distinct paths. Byte-identical completed
replay is no-write success. After a possible install fault, only exact existing
bytes adopt the commit. Proven absence is retryable only while request, bytes,
authority, and capacity proof remain exact; partial, unsafe, unreadable,
different, or ambiguous state is commit-unknown and fails closed.

Safely observed absence of the top-level namespace is the zero-binding initial
state; once it or an expected request path is present, unsafe, unreadable,
ambiguous, missing-after-visibility, or snapshot-drifted state fails closed.
Nonempty decision state aborts the shared workspace reachability proof before a
successful GC preview or plan and therefore blocks downstream execute,
reconcile, and purge. No public protection-reason token is added.
This retention rule prevents orphaned audit/projection evidence but grants no
deletion, cleanup, quarantine, repair, rollback, compaction, or GC mutation
authority. No failure or replay path may mutate a binding or any
semantic input, handoff, generation, receipt, journal, staged record, pointer,
policy, or other durable state.

P5 and P5B2 remain `IN_PROGRESS`; the trust-root, policy-authority provisioning,
and decision-store/capacity/GC prerequisites remain `COMPLETE`. Live operator
policy selection/provisioning, the encompassing release/DLP decision,
classification composition, omission execution, projection, public surfaces,
provider/backend, publication, remaining P5B2 work, and P5C remain `WAITING`;
H3 remains `DEFERRED`; no later successor is `READY`.

## Semantic-content release/DLP decision threats

The encompassing proposed unnumbered P5B2 semantic-content release/DLP decision
child is contract-frozen only and remains `WAITING`. The trust-root,
policy-authority provisioning, and decision-store/capacity/GC prerequisites
above are accepted, but no stable current `ACTIVE` operator record is
provisioned; classification composition and the other decision prerequisites
remain absent. The
decision child begins only from the complete
accepted promotion terminal for the exact visible-current generation. A
promoted generation, visible pointer, staged marker, receipt, handoff, semantic
input, certification binding, or historical release-decision binding is not
entry or release authority by itself. Missing, foreign, substituted,
incompatible, drifted, unsafe, unreadable, or ambiguous evidence fails closed.

The child protects against caller-supplied classifier, taxonomy, profile,
policy, input, and field-inventory substitution through two independent trust
roots: a repo-owned installed bundle manifest that inventories exact package
artifacts, and a stable current `ACTIVE` operator policy-authority record with a
monotonic revision and predecessor digest. Both are bound into the request.
Manifest artifacts use unique relative-normal-form paths beneath one canonical
installed package root plus descriptor-relative no-follow traversal, exact
single-link regular-file modes, sizes, and digests, preventing dot/dotdot,
absolute, alias, symlink, hard-link, and special-file substitution. Policy
authority embeds a versioned selection envelope whose body digest and completed
envelope digest bind the existing five operator fields to that exact policy
record rather than replaying them across bodies.
The authority also embeds one closed version-1 coverage-sufficiency declaration
whose release context and selected-profile coordinates must equal the
surrounding record exactly. Its digest is bound into both the policy and
decision request; duplicate, mismatched, unknown, invalid, or `INSUFFICIENT`
coverage therefore rejects rather than selecting a second interpretation.
Missing, pending, revoked, rolled-back, or invalid authority rejects. Neither
the decision child nor the provisioning prerequisite authorizes revocation or
reactivation; `REVOKED` is consumer-side fail-closed vocabulary only. Older
bindings are historical candidates only, and future consumers must name an
exact generation/request/binding-digest tuple and revalidate the same current
authority. Enumeration, newest-file selection, and policy shopping cannot
select release authority. Classification
reports only `NO_MATCH`, `MATCH`, or `INDETERMINATE`; operator policy separately
maps every closed `(field_type, category_id)` pair to `ALLOW_FIELD`,
`OMIT_RATIONALE`, or `REJECT_RELEASE`. `INDETERMINATE` rejects. `NO_MATCH`
produces `ALLOW_FIELD` only under the exact coverage-sufficiency declaration;
it is not a claim that content is safe. Unknown categories, undefined policy
mappings, unavailable required profiles, classifier or policy failure,
normalization disagreement, and coverage drift reject release. For multiple
matches, rejection outranks rationale omission, which outranks allow.

Version 1 is deterministic-pattern-only: locally pinned closed patterns, exact
grammars, and exact dictionaries with a digest-bound evaluation order. ML,
embeddings, statistical or entropy scores, opaque vendor detectors, generated
inference, and contextual semantic judgments are outside this child.
Its manifest-bound ABI scans exact UTF-8 bytes and freezes grammar, dictionary,
comparison, ordering, duplicate reduction, and error behavior. It does not use
runtime Unicode categories, locale, or host-dependent text normalization, so a
runtime or Unicode-library change cannot silently change classification.
The closed `utf8_lex_v1` comparator orders canonical identifiers by unsigned
UTF-8 bytes, shorter-prefix-first, without locale, Unicode collation, or host
runtime ordering. It governs selected profiles, category IDs, private rule IDs,
and the canonical field-result order, preventing implementations from deriving
different result or request digests from the same facts.

Every allow-capable policy requires the exact `core_secrets.v1` profile. That
profile recognizes only the complete explicit formats enumerated in the
[canonical contract](semantic-sync.md#closed-classification-and-coverage-contracts).
It excludes entropy-only evidence, bare hashes, UUIDs, arbitrary Base64, vague
secret-looking prose, and automatic example or fixture exemptions. Optional
jurisdictional, domain, and organization profiles require exact IDs and
digests. Contextual judgments remain deferred, and the operator must declare
the selected coverage sufficient for the named release context.

The scan surface is limited to required node labels, present node rationales,
and required hyperedge labels under the accepted 16 KiB UTF-8 and NFC bounds.
Edges, IDs, relations, confidence values, paths, locations, and metadata remain
outside classification. Global NFKC, whitespace rewrite, transliteration, and
case fold are forbidden; only syntax-defined names may use ASCII
case-insensitive comparison. Any surface, encoding, normalization, or bound
disagreement is `INDETERMINATE` and rejects release.
The decision request embeds no semantic content: it binds only the exact
target-owned semantic-input byte count and digest. Classification uses the same
captured bytes, and final locked revalidation must reproduce both values, so a
same-path content swap cannot be classified under one input and installed under
another.

Restricted node or hyperedge labels reject the entire release. Restricted
optional node rationales may produce `ALLOW_WITH_OMISSIONS`; the decision child
does not perform the omission. It performs no redaction, label removal,
pruning, ID remap, topology rewrite, graph construction, merge, query
projection, or publication. A later projection must independently reopen the
complete proof and may consume only the exact private omitted-rationale
locators under separate authority.

The encompassing child consumes only the separate prerequisite's immutable
private decision binding addressed by the complete
decision-request digest, not generation alone, in an external workspace
namespace outside the sealed generation. Descriptor-relative no-follow access,
mode-`0700` directories, a single-link mode-`0600` file, bounded enumeration,
and fail-closed unexpected-entry handling protect its shape. It commits the full eligible-field
inventory and scan result while storing only bounded counts and complete
field-result records containing private entity/field locators, value digests, category IDs,
private rule IDs, and dispositions. Raw semantic prose, matched substrings,
generated explanations, confidence scores, public source locations, provider
responses, and credentials are excluded. A derived redacted internal terminal
proof does not become a public receipt or content-release surface and never
carries entity/field locators or value digests. Those stable locators and
unkeyed hashes remain exclusively inside the private binding. If mode-`0600`
binding confidentiality is lost, low-entropy field guesses remain an offline
oracle risk; private placement, no-follow access, capacity limits, and retention
reduce exposure but do not claim cryptographic secrecy against same-UID
compromise. A separately authorized projection consumer must reopen the binding.

The separate prerequisite enforces the binding, per-generation binding, and
per-workspace binding caps. The encompassing child retains the concrete request,
manifest, artifact, profile, category, rule, ID, field, and per-field match
caps. Together those independent limits reject oversized inputs before unbounded
parsing or classification. Binding
bytes are charged to the existing global/workspace capacity policy and reserve.
The binding excludes its own digest; the external digest is over completed
canonical bytes, preventing recursive or divergent digest preimages.
Policy authority, selection envelope, decision request, full result, binding,
and field-value digests each use the canonical contract's closed member sets
and exact preimages. A field-value digest covers only the exact captured UTF-8
value bytes, so JSON quoting, final-newline, salt, prefix, normalization, or
case-conversion variants cannot silently create a competing identifier.

Classification uses captured bounded private bytes outside coordination locks,
without provider, model, network, ambient input, or durable write. Existing
shared-registry, exclusive-workspace, then shared-generation locks are used for
capture. Final revalidation, install, and reopen instead use exclusive registry,
exclusive workspace, then shared generation locks, serializing global capacity
and reserve accounting across workspaces while every authority coordinate,
byte count, store count, capacity value, and digest is revalidated. Identical concurrent requests converge on
one install-once binding; same-path different bytes conflict; distinct requests
use distinct paths. After an install fault, only exact existing bytes are
idempotent success. Proven absence is retryable only while all authority remains
exact; partial, unreadable, different, unsafe, or ambiguous state is
commit-unknown and fails closed. No new lease, lifecycle state, journal transition,
cleanup, deletion, rollback, or destructive recovery is introduced.

The accepted decision-store/capacity/GC prerequisite makes nonempty decision
state block GC eligibility and purge. This prevents orphaning the audit/projection
evidence; deletion, quarantine, cleanup, repair, and GC mutation remain
separately unauthorized.

A later pointer or authority change makes an exact binding historical evidence
only. It cannot be repaired into current authority. `REJECTED`,
`ALLOW_WITH_OMISSIONS`, and `ALLOW_UNCHANGED` are private terminal decisions for
one exact request; only the two allow outcomes can be offered to a separately
authorized consumer, and neither publishes or projects content by itself.
Before exposing that derived proof, the consumer takes shared registry,
exclusive workspace, then target-generation shared locks; reopens the exact
current promotion terminal, request, current `ACTIVE` policy authority, and
binding; and revalidates every entry, authority, input, result, and binding
coordinate before releasing the locks.

P5 and P5B2 remain `IN_PROGRESS`; the trust-root and policy-authority
provisioning prerequisites are accepted `COMPLETE`. The encompassing
release/DLP decision, live operator-policy selection/provisioning,
classification composition, omission execution, projection,
public surfaces, provider/backend, publication, remaining P5B2 work, and P5C
remain `WAITING`; the decision-store and capacity/GC prerequisite is accepted
`COMPLETE`. H3 remains `DEFERRED`; no later successor is `READY`.

## Explicit non-claims

V1 does not resist a compromised source-control or CI system, or a malicious
same-UID actor able to replace both artifacts and the trusted manifest. The
journal is corruption-evident, not cryptographically authenticated against such
an actor. Cross-platform support, network filesystems, pre-login service,
automatic online GC, strict source-linearizable query, and inter-observation ABA
detection are deferred.

V1 does not claim automatic, online, service, arbitrary-history, registry,
lease, path, staged-build, semantic-queue, or GC repair from the public pointer
repair lifecycle. It adds no durable repair completion index and makes no
governance or receipt-acceptance claim.

The accepted host-agent semantic-worker implementation and
[receipt](receipts/p5b2-semantic-worker.md) are authority only for the exact
transport boundary. They do not claim named/headless backend execution, network
access, API-key handling, provider fallback, durable post-completion receipt
recovery, staging cleanup, sealed-input finalization, generation certification
or promotion, pointer mutation, retained service/watch behavior, full semantic
sync, or content-level DLP classification of admitted semantic prose.

The accepted handoff child makes no claim of recovering a lost
worker `completed` terminal, adopting legacy completion without a version-1
handoff, automatically cleaning ambiguous staging, merging semantic entities
into `graph.json`, releasing semantic prose, certifying or promoting a
generation, moving a pointer, exposing a public full-semantic-sync command, or
granting provider/backend, network, service/watch, repair, GC, migrate,
publication, production/runtime installation authority, performance,
parent-phase completion, or successor authority.

The accepted certification-finalization implementation and
[receipt](receipts/p5b2-semantic-generation-certification-finalization.md) are
authority only for the exact internal stop boundary. Merged PR #58 records that
corrected acceptance as canonical, and neither the implementation nor its
receipt grants publication, merge, execution, or successor authority.
Even after the exact internal stop boundary, they grant no content-release
or DLP decision, graph/query projection, promotion, pointer movement, public
semantic-sync command, provider/backend/model or credential authority,
networking, migrate, repair, GC, semantic-staging cleanup, service/watch,
publication, production/runtime installation, performance qualification, P5C,
H3, P6+, parent-phase completion, or successor authority.

The accepted semantic-generation promotion and pointer-finalization
implementation and
[receipt](receipts/p5b2-semantic-generation-promotion-finalization.md) are
authority only for the exact internal stop boundary. Neither grants operator
execution authority, content-release/DLP, graph/query projection, a
`query_structural()` change, public semantic sync, schema/format, runtime
receipt, provider, credential, network, migrate, repair, GC, service/watch,
publication, P5C, H3, P6+, parent completion, or later-successor authority. The
terminal pointer may make only the exact promoted current generation eligible
to be considered as carried semantic-result evidence by a later separately
authorized handoff; it does not run or accept that handoff.

The accepted semantic-release trust-root implementation and
[receipt](receipts/p5b2-semantic-release-trust-root.md) are authority only for
the exact installed manifest, inventory, deterministic classifier, and private
installed executable bootstrap boundary. They do not claim policy selection,
release safety from `NO_MATCH`, field composition, decision persistence,
capacity/GC integration, omission, projection, a new public surface,
provider/backend behavior, publication, parent completion, execution, or
successor readiness.

Release channels are `dev`, `shadow`, `candidate`, `stable`, and `rollback` and
must promote identical digests. Later P5 work implements candidate publication
and login-service integration; P9 alone owns the real stable switch. P5A alone
does not make P5 complete.
