# Workspace governance

Ledger refresh: `2026-08-01T03:54:13Z`

This document became the canonical live ledger for Graphify-local phases
P1-P5C, H1-H3, their readiness state, and accepted completion receipts only
after the one-time migration publication gate was satisfied. That gate required
one commit reachable from the published
`Villeneuve-Ventures/graphify@workspace/v1` branch to add this file,
[`receipts/README.md`](receipts/README.md), and
[`receipts/p5b2b.md`](receipts/p5b2b.md), and to update
[`README.md`](README.md) to identify this repository authority set. Later
receipts do not expand or reopen that initial gate: each becomes repo-local
accepted evidence only when its separate governance-only commit is published
and merged, and [`receipts/README.md`](receipts/README.md) is the current
accepted-receipt inventory. If the initial gate cannot be verified, authority
fails closed to the external execution checklist and global plan. After
activation, those external plans retain authority only for cross-repository
dependencies and P6-P12 portfolio sequencing.

## Authority precedence

The precedence below applies after the publication gate activates. Before
activation, the external plans retain Graphify-local status, readiness, and
receipt authority under the same execution-authorization limit.

1. A direct operator instruction owns execution authorization.
2. Repository schemas, reference models, and implementation documents own
   product contracts.
3. This document owns current Graphify-local phase and readiness state.
4. Accepted files under [`receipts/`](receipts/) own Graphify completion
   evidence.
5. External portfolio plans own only cross-repository dependencies and P6-P12
   sequencing.
6. Historical snapshots never override a higher-precedence current source.

Host-local or ignored `.omx/` artifacts are historical execution evidence, not
current Graphify-local status or execution authority. A newly activated
workflow may use its own bounded plan state, but that state cannot override a
direct operator instruction, this ledger, an accepted receipt, or a frozen
product contract.

`READY` means that dependencies and the recorded live preflight permit a
bounded prompt to be reviewed. It does not authorize implementation. A
document edit cannot grant execution authority, and an implementation change
cannot accept its own completion receipt. Conflicts fail closed and require a
fresh governance-only reconciliation from the canonical branch.

## Current live snapshot

| Surface | State at refresh |
|---|---|
| Canonical base | The pre-edit checkout and live `origin/workspace/v1` both resolved to `5d730fe6e7d781c4d44f87989bf148ab2fdb63e3`, tree `27f7259fc3d716a78a3b28417204b1968c05d421`, with divergence `0/0` and a clean working tree. |
| Worktrees | One worktree exists. It is now on `codex/workspace-followup-governance`, created directly from the canonical base above. No competing delivery or governance worktree existed at preflight. |
| GitHub | PR [#43](https://github.com/Villeneuve-Ventures/graphify/pull/43) is merged at the canonical base above; the fork repository had no open pull requests before this branch was created. |
| P5B2 host-agent semantic-worker contract delivery | PR #43 exact head `1f202c9134ee0993e4bba40482fa8113f598920a`; merge/current commit `5d730fe6e7d781c4d44f87989bf148ab2fdb63e3`; merge/current tree `27f7259fc3d716a78a3b28417204b1968c05d421`. Exact-head CI [30681324681](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30681324681) passed `skillgen-check`, `test (3.14)`, and `security-scan`. |
| Follow-up-governance preflight | The canonical repository, branch, HEAD, tree, cleanliness, divergence, worktree inventory, merged PR #43, and empty open-PR inventory were revalidated with GitHub calls pinned to `Villeneuve-Ventures/graphify`. No GitHub review thread was replied to, resolved, or otherwise mutated. |
| Support baseline | Observed host CPython `3.14.6`; uv `0.11.30` |

Every later status transition must refresh this snapshot. A stale snapshot is
orientation only and cannot justify execution.

## Current Graphify ledger

| ID or surface | Depends on | Status | Current boundary/evidence |
|---|---|---|---|
| P1 | R0 | COMPLETE | Fork bootstrap and contract freeze closed. |
| P2 | P1 | COMPLETE | Registry, UUID identity, active source, and fenced leases closed. |
| P3 | P2 | COMPLETE | Immutable generations, journal, pointers, and recovery closed. |
| P4 | P3 | COMPLETE | Engine adapter, update compatibility, and read-only freshness closed. |
| P4F | P4 | COMPLETE | Post-merge comparison-authority repair closed. |
| H1 | P4F | COMPLETE | Inherited labeling-order test stabilization closed. |
| H2 | H1 | COMPLETE | Candidate packaging, dependency, and blocking security hygiene closed. |
| H3 | H2 | DEFERRED | Full-repository Pyright and medium-severity Bandit debt remains non-blocking. |
| P5 | P4, H1, H2 | IN_PROGRESS | P5A and delivered P5B children are complete; one contract-only P5B2 child is READY, while its implementation, remaining P5B2, and the broad P5C gate are not. |
| P5A | P4, H1, H2 | COMPLETE | Durable semantic queue and stable certification watermark closed. |
| P5B1 | P5A | COMPLETE | Production composition, versioned read-only status, and read-only doctor closed. |
| P5B2 | P5B1 | IN_PROGRESS | Delivered children remain complete. The contract-only host-agent semantic-worker transport is the sole READY child; its implementation, full semantic sync, explicit backend integration, migrate, broader repair, broader mutation or query authority, and all other commands remain waiting. |
| P5B2a | P5B1 | COMPLETE | Initial operator-authorized enrollment and explicit verified adoption remain closed. Accepted corrective receipt: [`P5B2a ADOPT pre-write correction`](receipts/p5b2a-adopt-prewrite-correction.md). |
| P5B2 identity maintenance | P5B2a | COMPLETE | Accepted receipt: [`P5B2 identity maintenance`](receipts/p5b2-identity-maintenance.md). Rebind and rotation only. |
| P5B2 active-source activation | P5B2a | COMPLETE | Accepted receipt: [`P5B2 active-source activation`](receipts/p5b2-active-source-activation.md). Standalone fenced `workspace activate` only. |
| P5B2 exact-last-good rollback | P5B2 | COMPLETE | Accepted receipt: [`P5B2 exact-last-good rollback`](receipts/p5b2-exact-last-good-rollback.md). One-step fenced `workspace rollback --request-stdin` to the visible pointer's exact `last_good` only. |
| P5B2 retained-source identity continuity | P5B2 identity maintenance, P5B2 active-source activation | COMPLETE | Accepted receipt: [`P5B2 retained-source identity continuity`](receipts/p5b2-retained-source-identity-continuity.md). `rotate_enrollment_evidence()` and `resolve_active_source()` independently require a shared immutable enrollment history root or the enrolled Git common-directory identity. Rejected rotation occurs before the requested source evidence, identity-action evidence, or registry revision is persisted. |
| P5B2 bounded offline-GC preview | P5B2 | COMPLETE | Accepted receipt: [`P5B2 bounded offline-GC preview`](receipts/p5b2-offline-gc-preview.md). Exact public `workspace gc --dry-run --request-stdin` read-only, unfenced preview only; mutation remains outside this preview receipt. |
| P5B2 public fenced offline-GC lifecycle | P5B2 bounded offline-GC preview | COMPLETE | Accepted receipt: [`P5B2 public fenced offline-GC lifecycle`](receipts/p5b2-offline-gc-lifecycle.md). Exact explicit `workspace gc --execute`, `--reconcile`, and `--purge` forms with `--request-stdin` only; automatic, online, service, repair, migrate, and semantic-sync authority remain excluded. |
| P5B2 public fenced pointer-repair lifecycle | P5B2 | COMPLETE | Accepted receipt: [`P5B2 public fenced pointer-repair lifecycle`](receipts/p5b2-pointer-repair.md). Exact `workspace repair --dry-run --request-stdin` existing-state-only preview and `workspace repair --execute --request-stdin` fenced execution only; broader repair, semantic sync, migrate, GC reconciliation, and every other mutation/query authority remain excluded. |
| P5B2b0 | P5B2a | COMPLETE | Request-bound staged-build recovery prerequisite closed. |
| P5B2b | P5B2b0 | COMPLETE | Accepted receipt: [`P5B2b`](receipts/p5b2b.md). |
| P5B2c | P5C1 | COMPLETE | Accepted receipt: [`P5B2c`](receipts/p5b2c.md). One-shot certified `workspace query --request-stdin` transport only. |
| P5B2 host-agent semantic-worker transport | P5A, P5C1 | READY | Contract-only exact `workspace semantic-worker --stdio` host-agent lifecycle in [`semantic-sync.md`](semantic-sync.md). P5A directly supplies queue semantics; P5C1 supplies installed runtime authority and transitively includes P5B1. No implementation or completion receipt exists; READY does not authorize implementation. |
| Remaining P5B2 commands | P5B2 | WAITING | Full semantic sync, named/headless backend integration, migrate, every repair mode beyond the accepted public fenced pointer-repair lifecycle, every mutation beyond the accepted explicit GC and pointer-repair lifecycles, every query authority beyond P5B2c's one-shot transport, and every other command require separate review. |
| P5C | P5B2 | WAITING | The broad service, installation, performance/resource, and publication parent is unchanged and is not promoted by the child split below. |
| P5C1 | P5B2b | COMPLETE | Accepted receipt: [`P5C1`](receipts/p5c1.md). Candidate-bound canonical runtime authority generation and isolated atomic installation/compensation proof only. |
| Remaining P5C concerns | P5C | WAITING | Watch/service, performance, shared-lock/root-traversal optimization, publication, retained query/service authority, and all other P5C work remain unchanged. |

P6-P12 are intentionally absent from this Graphify-local ledger. Their
cross-repository ordering remains in the external portfolio plan, and all
remain waiting at this handoff.

Statements in accepted boundary freezes below that a receipt promoted no later
child describe that receipt's authority. They do not override the sole current
READY row above, which is established only by this contract-only batch and
carries no implementation or acceptance evidence.

## Justified out-of-scope follow-up register

This register indexes only findings that were independently supported and
justified, but outside the delivery that surfaced them. It does not own phase
state, change an accepted receipt, authorize implementation, or turn a review
thread's unresolved UI state into technical debt.

The tracking states below are local to this register and are not Graphify phase
statuses. `JOS` means `justified out-of-scope`:

- `DOCUMENTED_NONCLAIM` points to an already-frozen boundary and creates no new
  work;
- `TRIGGER_GATED` remains inactive until its exact compatibility trigger occurs;
- `OPPORTUNISTIC` permits a behavior-preserving cleanup only when adjacent
  authorized work already touches the named surface;
- `GUARDED` records that an executable guard exists and must be revalidated when
  its dependency changes; and
- `SEPARATE_AUTHORIZATION_REQUIRED` identifies a bounded successor candidate but
  grants no implementation or acceptance authority.

| ID | Source | Tracking state | Justification and scope boundary | Activation trigger and closure evidence |
|---|---|---|---|---|
| `JOS-GC-CAPACITY-V1` | PR [#35](https://github.com/Villeneuve-Ventures/graphify/pull/35) and governance PR [#36](https://github.com/Villeneuve-Ventures/graphify/pull/36) | `DOCUMENTED_NONCLAIM` | The published capacity-policy representation is compatibility-sensitive, while the accepted preview makes no performance/resource or bounded pre-enumeration claim. Those are legitimate future boundaries, but the delivery and receipt were limited to the read-only preview. | Index only. The boundary remains owned by the [workspace ownership map](README.md#governance-and-deferred-work-ownership) and the [accepted preview receipt](receipts/p5b2-offline-gc-preview.md). Activate only through a separately authorized versioned compatibility change or qualification batch. |
| `JOS-GC-PREVIEW-VERSION` | PR [#37](https://github.com/Villeneuve-Ventures/graphify/pull/37) | `TRIGGER_GATED` | [`gc_preview_result_bytes()`](../../../graphify/workspace/gc_command.py) selects `GC_LIFECYCLE_SCHEMA_VERSION`, so a future lifecycle-v2 change could drift frozen preview-v1 bytes. Current v1 behavior is not defective, and the lifecycle delivery did not authorize a speculative version refactor. | Activate before changing either lifecycle or preview schema versioning. Closure must independently freeze preview-version selection and prove preview-v1 canonical bytes remain unchanged across a lifecycle-version change. |
| `JOS-BOUNDED-INPUT-READERS` | PR [#39](https://github.com/Villeneuve-Ventures/graphify/pull/39) | `OPPORTUNISTIC` | Repeated bounded-input readers across established commands are justified maintainability debt, but consolidating them was a cross-command refactor outside the pointer-repair PR's surgical behavior fixes. | Activate only when an authorized batch adds another `--request-stdin` command or changes more than one established reader. Closure requires regression-locked parser behavior and must preserve each command's bounds, canonicality, error mapping, authority-loading order, and deadline semantics. |
| `JOS-PR-AGENT-DRAIN` | PR [#40](https://github.com/Villeneuve-Ventures/graphify/pull/40) | `GUARDED` | The pinned PR-Agent revision relies on a private drain-aware runner, which creates a real pin-upgrade compatibility risk. The current exact pin imports and executes it successfully, so this is not a defect in the delivered workflow. | The [policy regression](../../../tests/test_pr_agent_policy.py) requires the exact import and call. Reactivate on any PR-Agent pin or runner-import change. Closure requires an exact-import smoke test and proof that review/description completion remains drain-aware with no silent legacy fallback. |
| `JOS-SEMANTIC-WORKER-CONFORMANCE` | Contract PR [#43](https://github.com/Villeneuve-Ventures/graphify/pull/43) | `SEPARATE_AUTHORIZATION_REQUIRED` | The frozen prose contract has no machine-readable request/result schema, equivalent closed runtime validator, conformance suite, or implementation. PR #43 was contract-only and explicitly excluded that work. | Activate only through explicit operator authorization for the implementation successor. Closure requires schemas or equivalent closed validators, conformance tests, and implementation of the frozen exact-decimal, checkpoint-capacity, uncertainty-recovery, and deadline-aware delivery behavior in [`semantic-sync.md`](semantic-sync.md). Full semantic sync and every explicit backend remain excluded, and completion still requires a separate governance-only receipt. |

PR #37's separate note about a `.codex` parent path and 100 ms timing cases is
not registered as justified debt because it does not identify a reproducible,
independently actionable packet. It may enter only after exact tests, commands,
environment, and failure evidence are reproduced; path portability and timing
stability must receive separate IDs if both survive that gate. Generic
docstring coverage, disproven workflow-syntax warnings, fixed findings, and
addressed workflow residue are likewise absent by design.

## P5C1 boundary freeze

P5C1 is limited to all of the following as one reviewable proof boundary:

- generate canonical `runtime-manifest.json` from the existing compatibility
  manifest plus an explicit `SemanticQueuePolicy`;
- bind the manifest's exact bytes and SHA-256 to the immutable candidate;
- install it atomically only in isolated external-state fixtures;
- prove compensation after deterministic installation failures; and
- preserve P5B1's read-only loader unchanged.

P5C1 excludes real `HOME`, XDG, and `CODEX_HOME` state; watch/service work;
performance or resource qualification; shared-lock or root-traversal
optimization; publication; retained production query/service authority; H3;
P6+; and every remaining P5B2 command. The bounded candidate/proof
implementation is complete under the accepted
[`P5C1` receipt](receipts/p5c1.md); none of those exclusions changed, and no
later child was promoted by that receipt.

## P5B2c boundary freeze

P5B2c is limited to the exact
`graphify workspace query --request-stdin` transport:

- accept one bounded canonical CLI-v1 request and reuse the existing
  `QueryRequest` validation;
- load and compose installed runtime authority before consuming standard
  input;
- call the existing freshness query authority exactly once, without an
  advisory status probe;
- emit native UTF-8 output only for `release` / `observed_current`, with one
  canonical redacted result certificate binding its byte count and SHA-256;
  and
- create no query log and write nothing to source, Git, workspace state,
  `HOME`, or `CODEX_HOME`.

Provider selection, networking, semantic execution, mutation, retained
service/watch, publication, performance/resource qualification, H3, P6+, and
every broader query or workspace-command authority remain excluded. The
bounded delivery is complete under the accepted
[`P5B2c` receipt](receipts/p5b2c.md); that receipt promoted no later child.

## P5B2 host-agent semantic-worker contract freeze

The sole current READY child is the contract-only future transport:

```text
graphify workspace semantic-worker --stdio
```

The exact protocol and lifecycle are frozen in
[`semantic-sync.md`](semantic-sync.md). One long-lived process owns one
`SEMANTIC_CLAIM` lease from claim through optional checkpoints and terminal
completion or classified failure. The caller must state `host_agent_active`
as the Boolean `true`; the transport passes no explicit backend and performs no
ambient provider or credential discovery.

Before queue completion, a successful host-agent fragment must pass the
worker-specific closed validation and bounded indexed sanitization around the
existing semantic helpers, be installed as one canonical private immutable
result envelope under external workspace semantic staging, be reopened and
cryptographically verified, and be bound to the live claim's existing
checkpoint. The current queue state machine is not redesigned.
Commit uncertainty after queue completion begins is not replay or success
authority because the completed queue item does not retain that result digest.

This READY record does not authorize implementation and is not completion
evidence. It adds no named/headless backend, network, `graphify.llm` provider
discovery, API-key handling, automatic fallback, full semantic sync,
`bind_sealed_inputs()` finalization, generation certification, promotion,
pointer mutation, migrate, repair, GC, service/watch, publication, or cleanup
authority. P5 and P5B2 remain `IN_PROGRESS`; all other remaining P5B2 and P5C
surfaces remain `WAITING` or deferred as recorded above.

## P5B2 identity-maintenance boundary freeze

The unnumbered P5B2 identity-maintenance surface is limited to the exact
`graphify workspace register rebind` and `graphify workspace register rotate`
forms accepted by the
[`P5B2 identity-maintenance receipt`](receipts/p5b2-identity-maintenance.md).
Both reuse installed authority, explicit UUID and registry-revision CAS,
bounded action-matching authorization, Git-top-level source proof, and the
existing registry policy. Rebind rejects a source identity persisted under a
different UUID before new source or identity-action evidence is persisted or
the requested registry mutation is committed. Rotate requires an explicitly
bound source and, under the separately accepted
[`P5B2 retained-source identity-continuity receipt`](receipts/p5b2-retained-source-identity-continuity.md),
independently requires either a shared immutable enrollment history root or the
enrolled Git common-directory identity before the requested evidence or
registry write.
Later active-source resolution independently repeats that continuity check.
Neither operation changes `active_source` or `active_source_revision`.
Registration v1 remains limited to `enroll` and `adopt`, and durable schema v1
remains unchanged. This ordering governs the requested mutation only; registry
lock acquisition and recovery may reconcile pre-existing state first.

Activation, additional sync, migrate, rollback, GC, repair, broader mutation
or query authority, production installation, watch/service, performance or
resource qualification, candidate publication, H3, P6+, and cleanup remain
excluded. This acceptance itself promoted no later child.

## P5B2 active-source activation boundary freeze

The unnumbered P5B2 active-source activation surface is limited to standalone
`graphify workspace activate` under the accepted
[`P5B2 active-source activation receipt`](receipts/p5b2-active-source-activation.md).
It loads installed authority before one bounded canonical `ACTIVATE`
authorization, requires the explicit repo UUID and four caller-supplied CAS
values, derives lease identity and timing internally, revalidates the exact Git
top-level source twice, and delegates once to the existing fenced registry
policy. The target must be explicitly bound, must still share an immutable
enrollment history root or retain the enrolled Git common-directory identity,
and must differ from the currently selected source. Success emits one redacted
CLI-v1 receipt. Denied, stale, and invalid paths preserve the documented exit
and redaction behavior; injected faults remain internal and are re-raised.

The separately accepted
[`P5B2 retained-source identity-continuity receipt`](receipts/p5b2-retained-source-identity-continuity.md)
closes the prior rotation and later-resolution nonclaims without reopening or
broadening this activation receipt. Additional sync modes, migrate, rollback,
GC, repair, broader mutation or query authority, production installation,
watch/service, performance or resource qualification, candidate publication,
H3, P6+, and cleanup remain excluded. That acceptance promoted no later child.

## P5B2 exact-last-good rollback boundary freeze

The unnumbered P5B2 exact-last-good rollback surface is limited to
`graphify workspace rollback --request-stdin` under the accepted
[`P5B2 exact-last-good rollback receipt`](receipts/p5b2-exact-last-good-rollback.md).
It loads and composes installed authority before consuming one bounded
canonical request; requires the explicit repo UUID, every caller-supplied
pre-acquisition registry, active-source, operation, migration, and pointer CAS
value, the current receipt, the visible pointer's exact non-null `last_good`
generation and receipt, its source epoch, and canonical `ROLLBACK`
authorization; and rejects current-generation reselection or arbitrary
historical selection.

One trusted 30-second `ROLLBACK` lease supplies the accepted operation and
fence authority. The same liveness deadline bounds the post-acquisition
pointer/receipt checks, generation locks, journal recovery, and durable
pointer/journal boundary. The orchestration delegates exactly once to
`PointerStore.rollback()`, while the existing pointer, generation, journal,
lease, recovery, and commit-unknown policies retain durable-state ownership.
Success emits one canonical receipt; every failure receipt remains redacted,
release cannot mask the primary error, and injected faults remain internal.

This acceptance adds no arbitrary historical selector, semantic sync,
migrate, GC, repair, broader mutation or query authority, production
installation, watch/service, performance or resource qualification, candidate
publication, H3, P6+, or cleanup authority. The separately accepted
retained-source identity-continuity receipt does not broaden rollback
authority, and that acceptance promoted no later child.

## P5B2 bounded offline-GC preview boundary freeze

The unnumbered bounded offline-GC preview surface is limited to
`graphify workspace gc --dry-run --request-stdin` under the accepted
[`P5B2 bounded offline-GC preview receipt`](receipts/p5b2-offline-gc-preview.md).
It loads and composes installed authority before consuming one bounded
canonical CLI-v1 request. The caller supplies the repo UUID, every expected
registry, active-source, operation, migration, and pointer revision,
`timeout_ms`, the complete `CapacityPolicy`, and all six `GcProtection`
classes. The request parser infers none of those values.

The existing read-only GC preview seam uses registry/workspace coordination
and generation-lock probes, requires two matching reachability snapshots, and
emits one deterministic canonical unfenced result. It creates no `LeaseGrant`,
fence, or executable `GcPlan` and makes zero durable writes on success or
failure. Existing fenced `GcStore.plan()`, `execute()`, `reconcile()`, and
`purge()` behavior remains unchanged and outside the public preview command.

GC mutation, quarantine, repair, migrate, semantic sync, broader query or
mutation authority, production installation, service/watch, publication, H3,
and P6+ remain excluded from this preview receipt. This acceptance makes no
performance or resource qualification and no bounded pre-enumeration traversal
claim. The published CLI-v1 capacity-policy fields remain frozen; any
compatibility change requires separate versioned review. No later child is
promoted to `READY`.

## P5B2 public fenced offline-GC lifecycle boundary freeze

The unnumbered public fenced offline-GC lifecycle is limited to the exact
commands accepted by the
[`P5B2 public fenced offline-GC lifecycle receipt`](receipts/p5b2-offline-gc-lifecycle.md):

```text
graphify workspace gc --execute --request-stdin
graphify workspace gc --reconcile --request-stdin
graphify workspace gc --purge --request-stdin
```

The frozen product contract remains in
[`README.md`](README.md#explicit-fenced-lifecycle). Execute binds the SHA-256 of
the exact canonical preview-result bytes to a fresh fenced `GC` plan before
quarantine. Reconcile remains explicit and is limited to an existing durable
intent or matching current-epoch completion recovery; a matching completion
replay and the no-recovery-state result are no-write. Purge remains explicit,
exact-plan-bound, and idempotent; first-time deletion rechecks fresh lease,
protection, pointer, and generation-lock authority, while exact terminal replay
is no-write. The request deadline remains in force through fenced mutation and
recursive deletion. The 4096 public generation bound is enforced by reading at
most one additional no-follow directory entry; this is a traversal-safety bound,
not performance or resource qualification.

This acceptance adds no automatic, online, or service GC; semantic sync;
migrate; repair; mutation beyond this exact lifecycle; broader query authority;
production installation; watch/service; publication; performance or resource
proof; H3; P6+; or cleanup authority. P5 and P5B2 remain `IN_PROGRESS`. This
receipt did not promote a later child; the separately reviewed host-agent
semantic-worker contract is the sole current READY child.

## P5B2 public fenced pointer-repair lifecycle boundary freeze

The unnumbered public fenced pointer-repair lifecycle is limited to the exact
commands accepted by the
[`P5B2 public fenced pointer-repair lifecycle receipt`](receipts/p5b2-pointer-repair.md):

```text
graphify workspace repair --dry-run --request-stdin
graphify workspace repair --execute --request-stdin
```

The frozen product contract remains in
[`README.md`](README.md#public-fenced-pointer-repair-cli). Dry-run is
existing-state-only, read-only inspection: it allocates no lease or fence,
performs no recovery or cleanup, and writes no state. Execute binds the exact
canonical preview-result bytes, including their terminating newline, to
`approved_preview_sha256`; requires canonical `REPAIR_EXECUTE` authorization;
acquires one fresh `REPAIR` lease; and requires the private in-lock plan to
match the approved preview decision before `PointerStore` may mutate pointer,
journal, or eligible corrupt-generation state. The one absolute request
deadline spans preview, source verification, lease acquisition, locked
revalidation, mutation, and release.

Unsafe state paths remain outside repair authority. In particular, an unsafe
semantic certification-binding path propagates as `unsafe_state_path` /
`configure_safe_state_root` without writes; it is not downgraded to ordinary
generation corruption or quarantine authority. GC intent, nonterminal or
corrupt staged builds, semantic-queue, registry, lease, source-authority, and
broader journal failures retain their separately owned operator guidance.

This acceptance adds no semantic sync; migrate; broader repair; arbitrary
generation selection; GC reconciliation; mutation or query authority beyond
the two exact forms; production installation; watch/service; publication;
performance or resource proof; H3; P6+; or cleanup authority. P5 and P5B2
remain `IN_PROGRESS`, P5C and its remaining concerns remain `WAITING`, and no
later child was promoted by that acceptance.

## P5B2a ADOPT correction boundary freeze

The append-only
[`P5B2a corrective receipt`](receipts/p5b2a-adopt-prewrite-correction.md)
freezes only the PR #27 cross-UUID persisted Git common-directory device/inode
check before new source or identity-action evidence is persisted or the
requested ADOPT registry mutation is committed. Registry lock acquisition and
recovery may reconcile pre-existing state first; no broader no-write guarantee
is made. P5B2a remains `COMPLETE` without reopening or new authority. Explicit
ADOPT authorization, expected-revision CAS, existing binding and shared-history
policy, and same-UUID retained-inode adoption are unchanged.

## Migration provenance

The external document-level authority remains active until the publication
gate is satisfied. The hashes below identify its bytes before the
migration-staging notices were added. At activation, the external files retain
their historical evidence and P6-P12 portfolio authority but cease to own
Graphify-local status, readiness, or receipts.

| Historical source | Pre-staging SHA-256 |
|---|---|
| `graphify-workspace-execution-checklist.md` | `ddf873a889ec5ad43b35762ea372605555a322f10e61467dba0a57271c9c2d51` |
| `graphify-workspace-global-plan.md` | `28ec1c3ea527857fb6f03687e540d11c8755b5b139b60263bfff8eeea09dbe6b` |

The checklist name and its pre-staging hash are repeated in the receipt index.
Both external files retain their prior historical starting SHAs, worktree
records, and execution evidence below their conditional migration notices.
