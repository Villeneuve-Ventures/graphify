# Workspace governance

Ledger refresh: `2026-07-28T02:00:53Z`

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

`READY` means that dependencies and the recorded live preflight permit a
bounded prompt to be reviewed. It does not authorize implementation. A
document edit cannot grant execution authority, and an implementation change
cannot accept its own completion receipt. Conflicts fail closed and require a
fresh governance-only reconciliation from the canonical branch.

## Current live snapshot

| Surface | State at refresh |
|---|---|
| Canonical checkout | Repository root; clean `workspace/v1` at `670cd633bf02691d7463361c139b9d8cdbe80006`, tree `779fa9f3fe203b31f2d75bfa0f23b49b447f1101`; local upstream divergence `0/2`, representing the PR #33 delivery commit and its merge. The checkout was not fast-forwarded. |
| Current canonical branch | Live GitHub and `origin/workspace/v1` at merge/current commit `5c1168cb29cdc1529852289692fb9ed5bda1ea0c`, current tree `a6412546e944e9400e664561686229d22a11820f`. |
| Worktrees | The canonical checkout was the only worktree at the fail-closed pre-edit preflight. One isolated governance-only proposal worktree now exists from the verified current commit on local branch `codex/workspace-p5b2-retained-source-continuity-governance`; no delivery worktree or competing governance lane exists. |
| GitHub | PR [#33](https://github.com/Villeneuve-Ventures/graphify/pull/33) is merged; the fork repository has no open pull requests. |
| P5B2 retained-source identity-continuity delivery | PR #33 exact delivery head `4444d8206604d84ce648aded8ee6467d3a603f4b`; merge/current commit `5c1168cb29cdc1529852289692fb9ed5bda1ea0c`; delivery/merge/current tree `a6412546e944e9400e664561686229d22a11820f`. The delivery head is the second direct parent of the merge. PR-head-associated CI [30318514569](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30318514569) checked out synthetic merge commit `bcb351eab95becf187643f8056bd8d49fbb252fa`, whose tree exactly matched the delivery/merge/current tree, and passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` status context succeeded. |
| PR #33 review state | One review thread, resolved; zero unresolved threads. The resolved duplicate-evidence-read finding is fixed at the exact delivery head. The remaining advisory comments were inapplicable or non-blocking, and no valid unfixed delivery defect remained. |
| Support baseline | Observed host CPython `3.14.3`; uv `0.11.30` |

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
| P5 | P4, H1, H2 | IN_PROGRESS | P5A and delivered P5B children are complete; remaining P5B2 and the broad P5C gate are not. |
| P5A | P4, H1, H2 | COMPLETE | Durable semantic queue and stable certification watermark closed. |
| P5B1 | P5A | COMPLETE | Production composition, versioned read-only status, and read-only doctor closed. |
| P5B2 | P5B1 | IN_PROGRESS | P5B2a, P5B2b0, P5B2b, P5B2c, identity maintenance, active-source activation, exact-last-good rollback, and retained-source identity continuity are complete; all other commands remain waiting. |
| P5B2a | P5B1 | COMPLETE | Initial operator-authorized enrollment and explicit verified adoption remain closed. Accepted corrective receipt: [`P5B2a ADOPT pre-write correction`](receipts/p5b2a-adopt-prewrite-correction.md). |
| P5B2 identity maintenance | P5B2a | COMPLETE | Accepted receipt: [`P5B2 identity maintenance`](receipts/p5b2-identity-maintenance.md). Rebind and rotation only. |
| P5B2 active-source activation | P5B2a | COMPLETE | Accepted receipt: [`P5B2 active-source activation`](receipts/p5b2-active-source-activation.md). Standalone fenced `workspace activate` only. |
| P5B2 exact-last-good rollback | P5B2 | COMPLETE | Accepted receipt: [`P5B2 exact-last-good rollback`](receipts/p5b2-exact-last-good-rollback.md). One-step fenced `workspace rollback --request-stdin` to the visible pointer's exact `last_good` only. |
| P5B2 retained-source identity continuity | P5B2 identity maintenance, P5B2 active-source activation | COMPLETE | Accepted receipt: [`P5B2 retained-source identity continuity`](receipts/p5b2-retained-source-identity-continuity.md). `rotate_enrollment_evidence()` and `resolve_active_source()` independently require a shared immutable enrollment history root or the enrolled Git common-directory identity. Rejected rotation occurs before the requested source evidence, identity-action evidence, or registry revision is persisted. |
| P5B2b0 | P5B2a | COMPLETE | Request-bound staged-build recovery prerequisite closed. |
| P5B2b | P5B2b0 | COMPLETE | Accepted receipt: [`P5B2b`](receipts/p5b2b.md). |
| P5B2c | P5C1 | COMPLETE | Accepted receipt: [`P5B2c`](receipts/p5b2c.md). One-shot certified `workspace query --request-stdin` transport only. |
| Remaining P5B2 commands | P5B2 | WAITING | Semantic sync, migrate, GC, repair, every other mutation, every query authority beyond P5B2c's one-shot transport, and every other command require separate review. |
| P5C | P5B2 | WAITING | The broad service, installation, performance/resource, and publication parent is unchanged and is not promoted by the child split below. |
| P5C1 | P5B2b | COMPLETE | Accepted receipt: [`P5C1`](receipts/p5c1.md). Candidate-bound canonical runtime authority generation and isolated atomic installation/compensation proof only. |
| Remaining P5C concerns | P5C | WAITING | Watch/service, performance, shared-lock/root-traversal optimization, publication, retained query/service authority, and all other P5C work remain unchanged. |

P6-P12 are intentionally absent from this Graphify-local ledger. Their
cross-repository ordering remains in the external portfolio plan, and all
remain waiting at this handoff.

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
later child is `READY`.

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
[`P5B2c` receipt](receipts/p5b2c.md); no later child is `READY`.

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
excluded. This acceptance promotes no later child to `READY`.

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
H3, P6+, and cleanup remain excluded. No later child is promoted to `READY`.

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
authority, and no later child is promoted to `READY`.

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
