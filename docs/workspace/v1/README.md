# Graphify workspace contract v1

Implemented contract scope through the one-step P5B2 exact-last-good rollback
CLI, the unnumbered P5B2 active-source activation CLI, the unnumbered P5B2
identity-maintenance CLI, P5B2c one-shot certified workspace query, the
bounded P5B2 GC preview CLI, the public fenced offline-GC lifecycle, and the
accepted bounded public fenced pointer-repair lifecycle,
P5C1 candidate-bound canonical runtime authority generation and isolated atomic
installation/compensation proof, P5B2b provider-neutral code-only structural
sync, P5B2b0 staged structural-build recovery, P5B2a initial workspace
registration, P5B1 read-only workspace status/doctor, P5A semantic queue, P4
adapter, and observed-current library runtime for
`graphifyy 0.9.16+workspace.1`. Durable state schema v1 and runtime-manifest
format v1 remain frozen; public status JSON is schema v2.

The contract-only future P5B2 host-agent semantic-worker transport is the sole
READY child. Its exact `graphify workspace semantic-worker --stdio` boundary is
frozen in [Host-agent semantic-worker contract](semantic-sync.md), but no
runtime command, schema, receipt, or implementation evidence exists. Full
semantic sync and every explicit backend remain waiting.

This directory defines the first version of Graphify's workspace control-plane
contracts. P2 provides a library surface for external durable registry state,
operator-authorized UUID/source binding, explicit active-source selection, and
fenced lease allocation. P3 adds caller-supplied generation staging and
certification, a framed segmented journal, atomic pointer movement and repair,
retained coordination locks, explicit capacity preflight, and offline GC. P4
adds the sole `0.9.16` engine adapter, the no-write comparison seam, and
two-sided observed-current release. P5A adds only the durable semantic desired-
work queue, fenced worker claims, exact reconciliation evidence, and the
generation-certification binding to one stable queue watermark. It does not
provide a retained-state import path. P5B1 adds only the production composition
root plus versioned read-only `graphify workspace status --json` and
`graphify workspace doctor` inspection. With no explicit library inputs, those
commands read the canonical, versioned `runtime-manifest.json` authority from
the external state root selected by `XDG_STATE_HOME` or `HOME`; missing,
malformed, unsafe, or unsupported authority fails closed without creating or
repairing state. P5B1 only consumes that file. P5C1 now generates its canonical
candidate-bound bytes, binds them to the immutable candidate trust set, and
proves atomic installation and compensation only beneath disposable
external-state roots while preserving P5B1's loader unchanged. It supplies no
production installation, publication, service/watch, provider, or performance
authority.
P5C retains long-lived service/watch and broader query authority. P5B2a adds only
`graphify workspace register enroll` for initial
enrollment and `graphify workspace register adopt` for an already-enrolled
verified clone or fork whose retained history includes a root recorded at
enrollment. A shallow clone that omits that root fails the shared-history proof
until sufficient history is fetched. Both forms require the repo UUID, expected
registry revision, and a matching `OperatorAuthorization` JSON object on
standard input. The command requires the current working directory itself to be
the Git top level, ignores local Git replacement refs and legacy graft files,
cross-checks its bounded no-follow `.graphify/workspace.toml`, and never infers
adoption. ADOPT also rejects a source whose persisted Git common-directory
device/inode identity belongs to another repo UUID before new source or
identity-action evidence is persisted or the requested registry mutation is
committed, while same-UUID retained-inode adoption remains allowed. It emits
one canonical redacted receipt and writes only the existing P2 registry,
workspace, lock, and evidence records beneath the configured external state
root. The receipt's normative machine-readable schema is
`graphify/workspace/schemas/cli/v1/registration.schema.json`.
The narrow identity-maintenance slice extends the same argv family with
`graphify workspace register rebind` and
`graphify workspace register rotate`. Both require the same explicit UUID,
expected registry revision, installed authority, Git-top-level source proof,
and bounded matching authorization. Rebind delegates shared-history or enrolled
Git-common-directory policy to `RegistryStore.rebind()`; rotate requires both an
explicit binding and continuity with the immutable enrollment history root or
enrolled Git common-directory identity before the requested source evidence,
identity-action evidence, or registry revision is persisted.
Active-source resolution independently repeats that immutable continuity check.
Rebind rejects a source identity persisted under a different UUID before
new source or identity-action evidence is persisted or the requested registry
mutation is committed. Neither operation changes `active_source` or
`active_source_revision`, and both emit the separate CLI-v1 receipt defined by
`graphify/workspace/schemas/cli/v1/identity-maintenance.schema.json`.
The separate active-source activation slice exposes only standalone
`graphify workspace activate` with the explicit repo UUID and registry,
active-source, operation, and migration CAS values. It loads and composes
installed runtime authority before consuming one canonical, duplicate-free,
16 KiB `ACTIVATE` authorization object; rediscovers and exactly revalidates the
current Git-top-level checkout; derives the trusted lease owner, UTC timestamp,
monotonic timestamp, and 30-second TTL internally; and delegates exactly once
to the existing `RegistryStore.activate_source()` policy. A target already
selected as the active source is rejected under the registry lock before lease,
evidence, or revision mutation. The canonical redacted receipt is defined by
`graphify/workspace/schemas/cli/v1/activation.schema.json` and never exposes
authorization, source paths, lease-owner identity, or raw errors. The existing
`register activate` remains invalid.
The exact-last-good rollback slice exposes only
`graphify workspace rollback --request-stdin`. It loads and composes installed
runtime authority before reading one canonical, duplicate-free request of at
most 16 KiB. The request binds the repo UUID, registry, active-source,
operation, migration, and pointer revisions, the current receipt, the exact
target generation and receipt recorded as the visible pointer's `last_good`,
the target source epoch, and canonical `ROLLBACK` authorization. The command
preflights and then revalidates that exact pointer target around acquisition of
one trusted 30-second `ROLLBACK` lease, derives the accepted fence and operation
authority from the grant, bounds post-acquisition target verification by the
lease liveness deadline, samples monotonic time again at the mutation boundary,
and delegates once to `PointerStore.rollback()`. That deadline is rechecked
after mutation locks are held and immediately before beginning the durable
pointer/journal commit.
Success writes the canonical `graphify.workspace.rollback` v1 receipt; conflict
and invalid outcomes write only redacted receipts. Commit uncertainty preserves
the existing pointer-recovery barrier, release cannot mask the primary error,
and `InjectedFault` is re-raised when it is the primary error or the only
release error. The request and receipt schemas are
`graphify/workspace/schemas/cli/v1/rollback-request.schema.json` and
`rollback-receipt.schema.json`.
Migrate, broader mutation/query commands, watch/service, performance
certification, and candidate publication remain later P5 work. The bounded GC
preview, explicit fenced offline-GC lifecycle, and public fenced pointer-repair
lifecycle below remain narrowly frozen public surfaces with separately accepted
governance receipts.
P5B2b0 adds the internal request-bound staged-build and stale-abandonment
recovery contract described in [State contracts](state-contract.md). P5B2b
exposes only `graphify workspace sync --code-only --request-stdin`, using a
bounded canonical JSON request, external generation-owned staging, the existing
fenced lifecycle, and one canonical redacted receipt. Status and doctor now
surface nonterminal staged-build recovery barriers through status schema v2.
P5B2c exposes only the one-shot certified
`graphify workspace query --request-stdin` transport described below. It loads
and composes installed runtime authority before consuming standard input, then
calls the existing `WorkspaceRuntime.freshness.query()` authority once; it does
not perform an advisory status probe. Provider selection, networking, semantic
execution, mutation, retained service/watch, and every broader query authority
remain deferred.

## Contract-only host-agent semantic worker

The future worker's public executable is `graphify`. Its only exact argument
vector after that executable is `workspace semantic-worker --stdio`, producing
this full invocation:

```text
graphify workspace semantic-worker --stdio
```

One long-lived process owns one `SEMANTIC_CLAIM` lease through claim, optional
checkpoints, and terminal completion or classified failure. The canonical
begin frame requires `executor="host_agent"` and
`host_agent_active=true`; there is no backend, endpoint, model, credential, or
provider-fallback field. Separate phase subprocesses cannot share the exact
boot/PID/process-start lease owner and are not an allowed transport.

A successful `UPSERT` fragment or exact `DELETE` tombstone is bounded,
canonically staged in private external workspace state, reopened and SHA-256
verified, and bound to the live claim checkpoint before queue completion.
`UPSERT` fragments are also validated and sanitized. The terminal result is a
redacted digest receipt, not semantic content. The detailed request/result,
deadline, retry/dead-letter, race, crash, result-binding, and status-routing
contract is [semantic-sync.md](semantic-sync.md).

This is a READY contract only. It does not call `graphify.llm`, infer a
provider from credentials or environment, invoke a network or named backend,
finalize `bind_sealed_inputs()`, certify or promote a generation, move a
pointer, retain a service, or implement full semantic sync.

## P5B2c one-shot certified query

The exact and only P5B2c argv is `graphify workspace query --request-stdin`.
It accepts one CLI-v1 canonical JSON object on standard input. The object is
duplicate-free UTF-8, at most 32 KiB, and contains exactly `contract`,
`schema_version`, `cli_contract_version`, `repo_uuid`, the existing
`QueryRequest` fields (`question`, `mode`, `depth`, `token_budget`, and
`context_filters`), and `timeout_ms`. `repo_uuid` is explicit; `timeout_ms` is
an integer from 1 through 60000. The command reuses `QueryRequest` for runtime
enforcement of all query bounds. Because JSON Schema `maxLength` counts Unicode
code points rather than encoded bytes, the CLI-v1 schema publishes the frozen
question and context-filter byte ceilings as non-enforcing
`x-graphify-utf8-max-bytes` and `x-graphify-utf8-total-max-bytes` annotations.
Focused parity tests bind those annotations to the existing `QueryRequest`
UTF-8 behavior; changing the CLI-v1 bounds requires a new CLI contract version.
Malformed, noncanonical, extra-field, untrimmed, oversized, out-of-bound, or
unsupported-version input is rejected before freshness locking or query
execution.

The command writes raw native query output, encoded as UTF-8 without a wrapper,
to standard output only when freshness returns `decision=release` and
`reason=observed_current`. It also writes one canonical redacted
`graphify.workspace.query_result` v1 control record to standard error. On
release that record binds the explicit repo UUID and nested `output` metadata:
`stream`, `encoding`, `bytes`, and `sha256`. Every other result leaves standard
output empty and omits the repo UUID and `output` metadata from the control
record.

Consumers must treat captured standard output as committed only when the
process exits 0, standard error contains exactly one canonical schema-valid
CLI-v1 control record reporting `release` / `observed_current` output on
UTF-8 standard output, and that record's byte count and SHA-256 digest match the
captured standard-output bytes. If any condition fails, the captured output is
uncertified and must be discarded. This commit rule verifies the deliberately
split streams; it does not make their delivery atomic.

| Exit | Result | Standard output |
|---:|---|---|
| 0 | `released` | Exact native output only |
| 10 | `drifted`, `timed_out`, or other retryable `withheld` result | Empty |
| 20 | `unsupported` or `invalid` | Empty |
| 64 | Any argv other than the exact invocation | Empty |

Existing freshness behavior classifies `LockTimeout` contention as `timeout`;
the CLI reports that truthfully as `timed_out` rather than inventing a separate
contention result. The path creates no query log and writes nothing to the
source checkout, Git metadata, workspace state, `HOME`, or `CODEX_HOME`.

## Identity-maintenance CLI

The exact identity-maintenance forms are:

```text
graphify workspace register rebind --repo-uuid UUID --expected-registry-revision N --authorization-stdin
graphify workspace register rotate --repo-uuid UUID --expected-registry-revision N --authorization-stdin
```

Malformed argv exits 64 with the deterministic workspace usage text on standard
error before authority loading, source discovery, or standard-input reads. For
valid argv, installed runtime authority is loaded and composed before the
authorization object is consumed. Authorization is duplicate-free UTF-8 JSON,
bounded to 16 KiB, canonically encodable, and must name exactly `REBIND` or
`ROTATE` for the selected lowercase verb. Source discovery, exact revalidation,
external-state checks, and expected-revision CAS are identical to initial
registration; policy remains solely in the existing registry methods.

Success writes one canonical `graphify.workspace.identity_maintenance` v1
receipt to standard output and exits 0. A revision or identity-policy conflict
writes one redacted receipt to standard error and exits 10; an invalid authority,
authorization, source, state, runtime, or uncertain commit writes one redacted
receipt to standard error and exits 20. Failure receipts omit the repo UUID and
include the observed registry revision only for a safe deterministic revision
conflict. Registration v1 remains limited to `enroll` and `adopt` and does not
admit either maintenance action. Rebind's cross-UUID persisted-source-identity
check runs before new source or identity-action evidence is persisted or the
requested registry mutation is committed; rotate retains the existing
explicitly-bound-source requirement. Both preserve `active_source` and
`active_source_revision`.

## Active-source activation CLI

The exact activation form is:

```text
graphify workspace activate --repo-uuid UUID --expected-registry-revision N --expected-active-source-revision N --expected-operation-epoch N --expected-migration-epoch N --authorization-stdin
```

Malformed argv exits 64 before authority loading, standard-input reads, source
discovery, or state access. Valid argv causes installed runtime authority to be
loaded and composed before standard input is consumed. The authorization bytes
must be canonical JSON, duplicate-free UTF-8, no larger than 16 KiB, and contain
exactly the five existing operator-authorization fields with `action` set to
`ACTIVATE`. Owner identity, wall time, monotonic time, and the bounded 30-second
activation lease TTL are trusted runtime inputs and cannot be supplied by the
caller.

The current working directory must be the exact Git top level, its configured
UUID must match `--repo-uuid`, and its source identity must remain stable across
the existing two discovery passes and checkout revalidation. The source must
already be explicitly bound in the registry and must still match immutable
enrollment identity by sharing a recorded history root or retaining the
enrolled Git common-directory device/inode. It must differ from the selected
active source. The CLI calls `RegistryStore.activate_source()` once with all
four caller-supplied CAS values; that method rejects active-source reselection
under the registry lock before lease, evidence, or revision mutation and retains
fencing, reservation and recovery barriers, alias normalization, and semantic
active-source authority.

Success writes one canonical `graphify.workspace.activation` v1 receipt to
standard output and exits 0. A stale CAS, already active, unbound or mismatched
source, live lease, or recovery barrier writes one redacted conflict receipt to
standard error and exits 10. Invalid authority, authorization, source, runtime,
state, or uncertain commit writes one redacted invalid receipt to standard error
and exits 20. Only success includes the repo UUID and resulting active-source
and epoch values; a safely observed stale registry revision may appear in its
dedicated conflict result. No receipt includes authorization, paths, owner
identity, or a raw exception. `InjectedFault` remains an internal test signal
and is re-raised.

## Exact last-good rollback CLI

The exact rollback form is:

```text
graphify workspace rollback --request-stdin
```

Malformed argv exits 64 before authority loading or standard-input reads. Valid
argv loads and composes installed authority before consuming one canonical
`graphify.workspace.rollback_request` v1 object. The request is bounded to
16 KiB, uses canonical UTF-8 JSON with no duplicate or extra fields, includes
all pre-acquisition lease and pointer CAS values, and carries the existing five
operator-authorization fields with `action` fixed to `ROLLBACK`. It can name
only the visible pointer's exact non-null `last_good` generation and receipt;
arbitrary historical selection and current-generation reselection are not
accepted.

The runtime derives lease owner identity, UTC and monotonic timestamps, and the
30-second TTL. It acquires one `ROLLBACK` lease, bounds post-acquisition target
verification by the grant's liveness deadline, and samples monotonic time again
immediately before mutation. The same deadline is rechecked under the mutation
locks and immediately before beginning the durable pointer/journal commit.
`PointerCAS` takes accepted active-source, operation, migration, and fence
values from the grant; the frozen runtime
`STATE_SCHEMA_VERSION` supplies the schema value; and the request supplies
pointer and target evidence. The orchestration calls `PointerStore.rollback()`
once. The existing pointer implementation owns
generation and journal verification, `ROLLED_BACK` persistence, crash recovery,
and commit-unknown barriers. Success exits 0 with one canonical receipt binding
the request SHA-256, repo UUID, target generation and receipt, and resulting
pointer revision. Stale authority or contention exits 10; invalid, corrupt,
unsupported, or commit-uncertain state exits 20. Failure receipts omit all
request identity, authorization, paths, owner data, environment values, and raw
errors. Governance completion is accepted separately under the
[`P5B2 exact-last-good rollback` receipt](receipts/p5b2-exact-last-good-rollback.md);
this implementation text alone does not expand live authority.

## Public fenced pointer-repair CLI

The only repair argv forms are:

```text
graphify workspace repair --dry-run --request-stdin
graphify workspace repair --execute --request-stdin
```

Malformed, reordered, repeated, or extended argv exits 64 before installed
authority loading or standard-input reads. Each accepted form loads and composes
installed authority before it consumes one duplicate-free, canonical UTF-8
CLI-v1 request of at most 16 KiB. The preview request binds `repo_uuid` and the
expected registry, active-source, operation, and migration revisions plus a
1--60,000 ms `timeout_ms`. The execute request adds the SHA-256 of the exact
canonical preview-result bytes, including its final newline, and the five-field
canonical `OperatorAuthorization` with `action` fixed to `REPAIR_EXECUTE`.
The four normative schemas are
`repair-preview-request.schema.json`, `repair-preview-result.schema.json`,
`repair-execute-request.schema.json`, and `repair-execute-result.schema.json`
under `graphify/workspace/schemas/cli/v1/`.

Dry-run is an existing-only inspection. It takes only the existing registry
shared lock, workspace exclusive lock, and existing generation locks needed to
verify pointer references; it creates no coordination object. It performs no
lease allocation, fence allocation, recovery, durable write, temporary cleanup,
directory creation, quarantine, or state repair on either success or failure.
Its canonical redacted result classifies the bounded pointer/journal/generation
decision as `no_op`, `repairable`, or `irreparable`, and contains only the
verified candidate and last-good references, selected source, prospective
pointer revision/action, projected journal actions, the redacted exact-decision
digest, and sorted corrupt generation IDs eligible for quarantine. It never exposes source/state paths,
authorization, owner/fence values, raw durable records, environment values, or
raw errors.

Execute first recomputes that same in-lock public preview and requires its
exact canonical bytes to match `approved_preview_sha256`. It then accepts a
fresh `REPAIR` lease through the existing CAS/fence authority, recomputes the
private exact repair plan after the required locks are held, requires its
redacted decision digest to match the approved preview, and permits writes
only when that plan equals the approved preview decision. This fresh fence and
in-lock comparison also apply to an approved `no_op`; execute never returns a
no-op result from the pre-fence preview alone. One absolute request deadline is
derived at execute entry and shared by preview, lease acquisition, and the
in-lock mutation decision; no phase receives a reset timeout. `PointerStore`
remains the sole authority for journal recovery, pointer replacement/finalization, and
quarantine of only corrupt generations excluded from the repaired pointer.
The bounded public scope is pointer, journal, and generation corruption only;
it neither selects arbitrary history nor repairs registry, lease, state-root,
or source authority.

Valid unresolved GC intent routes to `run_workspace_gc_reconcile`, rather than
repair. A nonterminal staged build routes to `resume_exact_workspace_sync`; a
corrupt staged build and registry/lease corruption route to
`inspect_workspace_state`; semantic-queue corruption routes to
`inspect_semantic_queue`; and unsafe state paths route to
`configure_safe_state_root`. Existing query, sync, rollback, and GC CLI-v1
reason/action pairs remain frozen for compatibility; more specific guidance for
those already-published results requires a new contract version. None of the
unsupported classes is repair authority. `graphify workspace doctor` remains
existing-only and read-only; it does not invoke either form.

Success exits 0 with a canonical redacted `repaired` or `no_op` result bound to
the request and approved-preview digests. Stale authority or contention exits
10; malformed, unsupported, unsafe, corrupt, irreparable, or commit-unknown
state exits 20 after valid argv. Commit uncertainty is not a replay token:
inspect status, then produce a fresh preview and a fresh execute request. The
same refresh is required when a timeout occurs after the fresh repair fence has
advanced the operation epoch; only pre-acquisition contention is retryable. A
completed execute advances the operation epoch, so its exact request cannot
apply a second repair.

This lifecycle does not add automatic repair, a service/online repair path,
GC reconciliation, staged-build/semantic/registry/lease repair, arbitrary
generation selection, source-checkout mutation, or a new durable completion
index. Governance completion is accepted separately under the
[`P5B2 public fenced pointer-repair lifecycle` receipt](receipts/p5b2-pointer-repair.md);
the remaining boundaries stay separately owned.

## Public offline-GC CLI

The frozen read-only preview argv is:

```text
graphify workspace gc --dry-run --request-stdin
```

Malformed, reordered, repeated, or extended argv exits 64 before authority
loading or standard-input reads. For the exact argv, the CLI loads and composes
installed runtime authority before consuming one bounded canonical CLI-v1
request. The caller supplies the repo UUID; expected registry, active-source,
operation, migration, and pointer revisions; `timeout_ms`; the entire
`CapacityPolicy`; and every `GcProtection` class: active-lease generations,
fixture generations, migration sources, proof generations, rollback-artifact
generations, and rollback sources. It infers no capacity, protection, path,
provider, or environment-backed value while parsing that request. Before the
request read, the unchanged installed-authority locator selects external state
through its existing `XDG_STATE_HOME`/`HOME` contract; that read-only location
authority is not a request default and creates nothing.

The composed runtime uses existing read-only registry/workspace coordination and
generation-lock probes, then requires two matching reachability snapshots. On
success it writes exactly one canonical deterministic unfenced
`graphify.workspace.gc_preview_result` v1 object to standard output. The result
contains candidates, protected generations with stable reasons, observed
revisions, and the capacity-policy SHA-256. It does not create a `LeaseGrant`, a
fence, or an executable `GcPlan`.

The preview makes zero durable writes on success and failure: it does not create
or clean leases, recovery state, temporary files, directories, modes, locks,
registry records, intents, quarantine records, receipts, query logs, sources, or
Git data. It also leaves `HOME`, `XDG_STATE_HOME`, and `CODEX_HOME` untouched.
The preview result bytes are the canonical approval object for execute. They
remain exactly the `graphify.workspace.gc_preview_result` v1 bytes described
above; adding lifecycle commands does not add a field, a lease, a fence, a
plan, or a durable write to preview.

| Exit | Result | Standard output |
|---:|---|---|
| 0 | Stable preview | One canonical preview result |
| 10 | Stale authority, unstable observation, or coordination contention | Empty; a canonical redacted failure result is written to standard error |
| 20 | Invalid, unsupported, unsafe, corrupt, or recovery-required state | Empty; a canonical redacted failure result is written to standard error |
| 64 | Invalid argv | Empty; deterministic usage text is written to standard error |

### Explicit fenced lifecycle

The exact lifecycle argv forms are:

```text
graphify workspace gc --execute --request-stdin
graphify workspace gc --reconcile --request-stdin
graphify workspace gc --purge --request-stdin
```

Each form accepts one duplicate-free, canonical CLI-v1 request of at most
128 KiB after installed runtime authority has loaded and composed. Every
request carries the explicit repo UUID; expected registry, active-source,
operation, migration, and pointer revisions; `timeout_ms`; the complete
`CapacityPolicy`; the complete six-class `GcProtection` set; and a matching
canonical `OperatorAuthorization`. The required authorization actions are
`GC_EXECUTE`, `GC_RECONCILE`, and `GC_PURGE`, respectively. The request and
result schemas are `gc-execute-request.schema.json`,
`gc-execute-result.schema.json`, `gc-reconcile-request.schema.json`,
`gc-reconcile-result.schema.json`, `gc-purge-request.schema.json`, and
`gc-purge-result.schema.json` under `graphify/workspace/schemas/cli/v1/`.

Execute additionally carries `approved_preview_sha256`: the SHA-256 of the
exact canonical preview-result bytes, including the terminating newline. It
first recomputes the read-only preview from the request's explicit authority,
capacity, and protection values. Only an exact digest match can continue. It
then acquires a fresh trusted `GC` lease, creates a fresh fenced plan, and
requires the preview and plan to match on their non-fence projection: repo UUID,
registry revision, active-source revision, migration epoch, pointer revision,
capacity-policy SHA-256, candidates, and protected generations/reasons. The
comparison ignores `shared_lock` only when another reason already protects the
same generation; a sole `shared_lock` remains material. The operation epoch and
fence are intentionally excluded because the new lease advances lifecycle
authority. The request's absolute timeout remains in force through lease
acquisition, planning, blocking generation-lock acquisition, and fenced store
mutation. A successful execute
quarantines only the approved plan's candidates and returns a canonical redacted
receipt with request SHA-256, approved-preview SHA-256, plan SHA-256, and the
quarantined generation IDs.

Reconcile is never automatic. It uses a fresh `GC` lease only to reconcile an
already-existing durable GC intent. Each durable completion is also indexed by
its operation epoch before the intent is cleared. If execute completed but its
public receipt was lost, a reconcile request matching that still-current epoch
returns the immutable completion without a lease or write. With neither an
intent nor a matching current-epoch completion, the result is
`nothing_to_reconcile`; reconcile does not invent a plan or mutate an unrelated
lifecycle phase.

Purge is likewise explicit and idempotent. Its request carries
`expected_plan_sha256`. First-time deletion uses a fresh `GC` lease and rechecks
the requested pointer revision, protection set, and generation locks before
removing one completed plan's quarantined content. Exact terminal replay
returns its durable no-write receipt without those first-time-deletion checks.
The success receipt contains only the request SHA-256, plan SHA-256, and purged
generation IDs. No public lifecycle receipt exposes authorization data, raw
intent/completion/purge documents, lease owner or fence data, paths,
timestamps, operation epochs, environment values, or raw errors.

For all four GC forms, malformed, reordered, repeated, or extended argv exits
64 before authority loading or standard-input reads. Lifecycle success exits 0;
stale authority or contention exits 10; invalid, unsupported, unsafe, corrupt,
or commit-uncertain state exits 20. Lifecycle failures are canonical redacted
results on standard error. Status and doctor report a valid unresolved GC
intent with `run_workspace_gc_reconcile`; the command remains operator-driven.

The public bound on candidates and protected generation IDs is 4096. Generation
enumeration uses descriptor-relative no-follow validation and reads at most one
additional directory entry (4097 total) to detect overflow before materializing
the generation set. This is a traversal safety bound, not a performance or
resource certification.

This delivery excludes automatic GC, online or service GC, migration, semantic
sync, publication, performance or resource proof, H3, P6+, and
governance acceptance. A governance or receipt closeout, if any, remains
separate from this implementation documentation.

## Governance and deferred work ownership

The publication gate in [Workspace governance](governance.md) requires one
published `workspace/v1` commit to add the governance ledger and receipts and
update this ownership map. Until that gate activates, the external execution
checklist and global plan retain Graphify-local status, readiness, and receipt
authority, and these repository files are proposed migration records. After
activation, Workspace governance owns Graphify-local live phase/readiness
state, its [accepted receipts](receipts/) own completion evidence, and the
external plans retain only cross-repository dependencies and P6-P12 portfolio
sequencing. Direct operator instruction alone owns execution authorization.

| Area | Owner/status | Stable boundary |
|---|---|---|
| Host-agent semantic worker | P5B2 host-agent semantic-worker transport (`READY`, contract-only) | [`semantic-sync.md`](semantic-sync.md) freezes only one long-lived `workspace semantic-worker --stdio` host-agent queue lifecycle with a verified staged-result binding before completion. No runtime implementation or receipt exists. |
| Additional sync modes | Remaining P5B2 | Only provider-neutral structural `sync --code-only` is implemented. Full semantic sync, named/headless backends, and every broader mode require separately reviewed authority, redaction, recovery, and result-consumption contracts. |
| Certified one-shot query | P5B2c (`COMPLETE`) | Only `workspace query --request-stdin` is public: installed authority precedes input, one freshness query can release exact output after `observed_current`, and every other path withholds it. |
| Identity maintenance | P5B2 identity maintenance (`COMPLETE`) | Accepted receipt: [`P5B2 identity maintenance`](receipts/p5b2-identity-maintenance.md). `workspace register rebind` and `rotate` expose only the existing registry policy with explicit UUID, revision CAS, matching authorization, cross-UUID rebind rejection before new source or identity-action evidence and the requested registry commit, unchanged active-source state, and a dedicated receipt schema. |
| Active-source activation | Unnumbered P5B2 activation (`COMPLETE`) | Accepted receipt: [`P5B2 active-source activation`](receipts/p5b2-active-source-activation.md). `workspace activate` alone exposes the existing fenced active-source CAS with explicit UUID and four-part CAS, canonical `ACTIVATE` authorization, internally derived lease inputs, an immutable-enrollment continuity check, and one redacted CLI-v1 receipt. |
| Exact last-good rollback | P5B2 exact-last-good rollback (`COMPLETE`) | Accepted receipt: [`P5B2 exact-last-good rollback`](receipts/p5b2-exact-last-good-rollback.md). `workspace rollback --request-stdin` exposes one fenced move to the visible pointer's exact `last_good` reference with an explicit canonical request and redacted receipt. It does not authorize arbitrary historical selection or any later command. |
| Public fenced pointer repair | P5B2 public fenced pointer-repair lifecycle (`COMPLETE`) | Accepted receipt: [`P5B2 public fenced pointer-repair lifecycle`](receipts/p5b2-pointer-repair.md). `workspace repair --dry-run --request-stdin` is existing-only inspection; `--execute --request-stdin` requires exact approved preview bytes, `REPAIR_EXECUTE`, a fresh `REPAIR` lease, and an in-lock exact-plan match before the existing `PointerStore` may mutate pointer/journal state or quarantine eligible corrupt generations. Broader repair and every other mutation/query authority remain outside this accepted surface. |
| Bounded GC preview | P5B2 bounded offline-GC preview (`COMPLETE`) | Accepted receipt: [`P5B2 bounded offline-GC preview`](receipts/p5b2-offline-gc-preview.md). `workspace gc --dry-run --request-stdin` exposes only an unfenced, read-only, canonical preview with explicit authority, capacity, and protection inputs. It does not authorize GC mutation or make performance/resource or bounded pre-enumeration traversal claims. The published CLI-v1 capacity-policy fields remain frozen; any compatibility change requires separate versioned review. |
| Public fenced offline-GC lifecycle | P5B2 public fenced offline-GC lifecycle (`COMPLETE`) | Accepted receipt: [`P5B2 public fenced offline-GC lifecycle`](receipts/p5b2-offline-gc-lifecycle.md). `workspace gc --execute`, `--reconcile`, and `--purge` each require `--request-stdin` and phase-specific authorization. Execute and first-time reconcile or purge mutation acquire fresh fenced `GC` authority; matching current-epoch completion recovery, reconcile with no recovery state, and exact terminal purge replay are no-write results. Execute binds an approved exact preview-result SHA-256 to a fresh non-fence-equivalent plan; reconcile and purge remain explicit-only. Automatic, online, service, migrate, semantic-sync, publication, and performance/resource authority remain outside this frozen boundary. |
| Retained-source identity continuity | P5B2 registry hardening (`COMPLETE`) | `rotate_enrollment_evidence()` and `resolve_active_source()` now independently require a shared immutable enrollment history root or the enrolled Git common-directory identity. Rejected rotation occurs before the requested source evidence, identity-action evidence, or registry revision is persisted. Accepted receipt: [`P5B2 retained-source identity continuity`](receipts/p5b2-retained-source-identity-continuity.md). |
| Remaining workspace commands | Remaining P5B2/P5C | Migrate, every mutation beyond the delivered explicit GC and pointer-repair lifecycles, and all query authority beyond P5B2c's one-shot certified transport require separately reviewed contracts and explicit operator intent. |
| Candidate runtime authority | P5C1 (`COMPLETE`) | Generates canonical `runtime-manifest.json` from the existing compatibility manifest plus explicit `SemanticQueuePolicy`, binds its exact bytes/hash to the immutable candidate, installs it atomically only in isolated external-state fixtures, proves deterministic-failure compensation, and preserves P5B1's read-only loader unchanged. |
| Service, release, and resource proof | Remaining P5C | Watch/service supervision, publication, representative-corpus performance and resource accounting, record admission budgets, retained production query/service authority beyond the P5B2c one-shot transport, and any shared workspace read-lock optimization remain waiting outside P5C1. |
| Static-analysis baseline | H3 | Inherited full-repository Pyright and medium-severity Bandit debt remains deferred and non-blocking after H2 established blocking high-severity and dependency-audit gates. |
| Portfolio migration and cutover | P6-P12 | Shadow migrations precede the P9 global installation and stable-route activation; legacy pruning remains separately authorized after the observation window. |
| Semantic capability selection | P5B2 host-agent semantic-worker transport (`READY`, contract-only) | The sole frozen child requires an already-active host agent to be stated explicitly and never infers capability from an absent credential. Direct headless Codex OAuth fallback, named backends, network access, and provider discovery remain unimplemented and waiting. |
| Contract and support horizon | Unranked future contract decisions | Possible v2 sorted-array admission, broader host/filesystem support, sudden-power-loss claims, automatic online GC, historical-generation query, and upstreaming are retained decisions or nonclaims, not current phase gates. |
| Extraction diagnostics | Outside workspace phase gates | Zero-node fixture notices and missing optional SQL/DM parser extras remain non-blocking diagnostics unless a later requirement explicitly promotes their corpus coverage. |

Historical deferrals for the labeling-order test, candidate packaging warnings,
vulnerable optional/development dependencies, high-severity Bandit findings,
and Gemini model-override test isolation are closed by later merged work and are
not carried forward as debt. Deliberately rejected review suggestions likewise
do not become backlog merely because their GitHub threads remain unresolved.

Authorization standard input is one JSON object with exactly the five string
fields shown here; `action` is the uppercase operator intent, not the lowercase
CLI verb:

```json
{"action":"ENROLL","issued_at":"2026-07-16T15:00:00Z","nonce":"example-nonce","operator_id":"operator:example","reason":"initial workspace enrollment"}
```

For `register adopt`, `rebind`, or `rotate`, replace `ENROLL` with `ADOPT`,
`REBIND`, or `ROTATE` respectively. For standalone `workspace activate`, use
`ACTIVATE` and canonicalize the complete JSON bytes. `issued_at` must be a real
RFC 3339 UTC timestamp ending in `Z`; the other values must be non-empty and
trimmed. Extra fields, duplicate fields, non-string values, and an action that
does not match the explicit CLI verb are rejected before mutation.

The existing Graphify `0.9.16` extraction, cache, build, watch, export, and
query implementation remains the only graph engine. A workspace-enabled build
is one `graphifyy` distribution based on the exact upstream commit
`a0e4a1c6bd3a99edfdd84ad30927003f51face6a`; the workspace layer does not copy
or fork engine logic inside the package.

## Contract authority

- JSON Schemas under `graphify/workspace/schemas/v1/`,
  `graphify/workspace/schemas/cli/v1/`, and
  `graphify/workspace/schemas/cli/v2/` are normative for the structural shape
  of durable documents, CLI requests and receipts, versioned status output,
  and the TOML-to-object representation of repo policy. Registration, identity-
  maintenance, active-source activation, sync request/receipt, one-shot query
  request/result, GC preview request/result, and pointer-repair preview/execute
  request/result contracts are CLI v1; status JSON is schema v2.
- `graphify.workspace` supplies dependency-free canonical reference models,
  exact v1 rejection, SHA-256 inputs, and journal frame encoding. Its
  cross-field and cross-document validation is normative where JSON Schema
  cannot express relational invariants such as keyed ordering, digest binding,
  revision binding, or exact compensation coverage.
- `graphify.workspace.persistence`, `.identity`, `.registry`, `.leases`,
  `.generations`, `.journal`, `.pointers`, and `.gc` implement the P2/P3
  runtime boundary. Within that boundary, `.generations` and `.leases` also
  own P5B2b0's bounded internal staged-build, successor-lease, and stale-
  abandonment recovery. `.sync` composes those existing APIs for the sole
  public code-only structural sync path. `.adapters` and `.freshness` implement
  the bounded P4 read-only engine/query boundary. `.semantic_queue` implements the bounded
  P5A durable queue and certification boundary. `.composition` owns the
  bounded, no-follow read of installed runtime authority and wires the existing
  stores without duplicating their persistence behavior. `.cli` exposes the
  P5B2a registration command plus the bounded rebind/rotation identity-
  maintenance slice and standalone active-source activation with bounded
  authority, authorization, policy, Git-discovery, and four-part CAS inputs,
  the P5B2b sync request/receipt transport, the P5B2c one-shot query transport,
  the bounded read-only GC preview transport, and the bounded public
  pointer-repair transport; it reuses `.identity`, `.registry`, `.sync`,
  `.freshness`, `.gc`, `.pointers`, `.journal`, and existing lease authority
  without adding another persistence path. Lifecycle mutation fails closed outside
  non-elevated macOS on local APFS; tests use an explicit injected capability
  seam and disposable external state roots.
- Fixtures under `tests/fixtures/workspace/v1/` freeze positive, negative,
  canonicalization, version-rejection, compensation, and rollback examples.
- Candidate artifacts are built by `python -m tools.workspace_artifacts build`
  with a fixed source epoch and explicit output root.

## Documents

- [Workspace governance](governance.md)
- [Accepted governance receipts](receipts/)
- [Architecture](architecture.md)
- [State contracts](state-contract.md)
- [P3 runtime](p3-runtime.md)
- [P4 adapter and freshness](p4-adapter-freshness.md)
- [Host-agent semantic-worker contract](semantic-sync.md)
- [Compatibility and artifacts](compatibility.md)
- [Installation and rollback](installation.md)
- [Migration boundary](migration.md)
- [Threat and support model](threat-model.md)
- [Verification](verification.md)

Any change to a v1 field, enum, canonical encoding, or invariant requires a new
schema version or an explicitly compatible additive revision. Unknown versions
fail before a state-changing operation. P3 adds internal canonical envelopes
for capacity reservations, journal heads, and GC recovery without adding a
public v1 schema member or field.
