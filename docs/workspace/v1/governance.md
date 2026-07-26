# Workspace governance

Ledger refresh: `2026-07-26T17:55:23Z`

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
| Canonical checkout | Repository root; `workspace/v1` at `0f46a86c03281d6ab3b52243f2881d2d18f1fad6`, tree `2afc7a1f3610ac712db3cdbe5fd9adb8c9150141`; clean and synchronized with `origin/workspace/v1` at upstream divergence `0/0` |
| Worktrees | The canonical checkout was the only worktree at the fail-closed pre-edit preflight. One isolated governance-only proposal worktree now exists on local branch `codex/workspace-p5b2-activation-governance-closeout`; no delivery worktree or competing governance lane exists. |
| GitHub | PR [#29](https://github.com/Villeneuve-Ventures/graphify/pull/29) is merged; the fork repository has no open pull requests. |
| P5B2 active-source activation delivery | PR #29 exact delivery head `9f3b3b8af1e1d712d25febf81665d75feff96637`; merge/current `0f46a86c03281d6ab3b52243f2881d2d18f1fad6`; delivery/merge/current tree `2afc7a1f3610ac712db3cdbe5fd9adb8c9150141`. The delivery head is a direct parent of the merge. Exact-head CI `30212289087` passed `skillgen-check`, `test (3.14)`, and `security-scan`; the separate `CodeRabbit` status context succeeded. |
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
| P5B2 | P5B1 | IN_PROGRESS | P5B2a, P5B2b0, P5B2b, P5B2c, identity maintenance, and active-source activation are complete; retained-source identity-continuity hardening is deferred and all other commands remain waiting. |
| P5B2a | P5B1 | COMPLETE | Initial operator-authorized enrollment and explicit verified adoption remain closed. Accepted corrective receipt: [`P5B2a ADOPT pre-write correction`](receipts/p5b2a-adopt-prewrite-correction.md). |
| P5B2 identity maintenance | P5B2a | COMPLETE | Accepted receipt: [`P5B2 identity maintenance`](receipts/p5b2-identity-maintenance.md). Rebind and rotation only. |
| P5B2 active-source activation | P5B2a | COMPLETE | Accepted receipt: [`P5B2 active-source activation`](receipts/p5b2-active-source-activation.md). Standalone fenced `workspace activate` only. |
| P5B2 retained-source identity continuity | P5B2 identity maintenance, P5B2 active-source activation | DEFERRED | `rotate_enrollment_evidence()` and `resolve_active_source()` do not independently re-prove immutable enrollment continuity. One focused registry-hardening follow-up owns both nonclaims; it does not block the accepted activation boundary. |
| P5B2b0 | P5B2a | COMPLETE | Request-bound staged-build recovery prerequisite closed. |
| P5B2b | P5B2b0 | COMPLETE | Accepted receipt: [`P5B2b`](receipts/p5b2b.md). |
| P5B2c | P5C1 | COMPLETE | Accepted receipt: [`P5B2c`](receipts/p5b2c.md). One-shot certified `workspace query --request-stdin` transport only. |
| Remaining P5B2 commands | P5B2 | WAITING | Semantic sync, migrate, rollback, GC, repair, every mutation beyond accepted activation, every query authority beyond P5B2c's one-shot transport, and every other command require separate review. |
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
the requested registry mutation is committed; rotate requires an explicitly
bound source. Neither changes `active_source` or `active_source_revision`.
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
CLI-v1 receipt; denied, stale, and invalid paths preserve their frozen result
and redaction behavior, while injected faults remain internal and are re-raised.

This acceptance does not claim that identity-evidence rotation or later
active-source resolution independently re-proves immutable enrollment
continuity. Those two retained-source nonclaims remain one deferred P5B2
registry-hardening follow-up. Additional sync modes, migrate, rollback, GC,
repair, broader mutation or query authority, production installation,
watch/service, performance or resource qualification, candidate publication,
H3, P6+, and cleanup remain excluded. No later child is promoted to `READY`.

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
