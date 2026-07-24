# Workspace governance

Ledger refresh: `2026-07-24T14:55:10Z`

This document becomes the canonical live ledger for Graphify-local phases
P1-P5C, H1-H3, their readiness state, and accepted completion receipts only
after the publication gate is satisfied. That gate requires one commit
reachable from the published `Villeneuve-Ventures/graphify@workspace/v1` branch
to add this file, [`receipts/README.md`](receipts/README.md), and
[`receipts/p5b2b.md`](receipts/p5b2b.md), and to update
[`README.md`](README.md) to identify this repository authority set. Until then,
the external execution checklist and global plan remain the active
Graphify-local authority, and these files are proposed migration records. If
the publication gate cannot be verified, authority fails closed to those
external plans. After activation, the external plans retain authority only for
cross-repository dependencies and P6-P12 portfolio sequencing.

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
| Canonical checkout | `/Users/lisrel.claw/graphify`; `workspace/v1` at `ac5ac55bbd93e23c727aa1fd946d194f8729930e`, tree `ec6d9e8dd2106472d595d2d7059b45b2c9d51517`; working-tree changes were limited to the four `docs/workspace/v1` documentation changes in this governance batch; upstream divergence `0/0` |
| Worktrees | Canonical checkout only |
| GitHub | No open pull requests; repository Issues are disabled |
| P5B2b delivery | PR [#14](https://github.com/Villeneuve-Ventures/graphify/pull/14); exact reviewed head `156797507c84bcad7e2ff0689e6e4ba6d3afa23c`; merge `513e6a6a5287362e62d8763213179149592e0368`; merge-head CI `30037483038` passed all four jobs; current `workspace/v1` descends from both reviewed head and merge |
| Support baseline | CPython `3.14+`; observed host CPython `3.14.6`; uv `0.11.30` |

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
| P5B2 | P5B1 | IN_PROGRESS | P5B2a, P5B2b0, and P5B2b are complete; all other commands remain waiting. |
| P5B2a | P5B1 | COMPLETE | Initial operator-authorized enrollment and explicit verified adoption closed. |
| P5B2b0 | P5B2a | COMPLETE | Request-bound staged-build recovery prerequisite closed. |
| P5B2b | P5B2b0 | COMPLETE | Accepted receipt: [`P5B2b`](receipts/p5b2b.md). |
| Remaining P5B2 commands | P5B2 | WAITING | Semantic sync, query, migrate, rollback, GC, repair, rebind, rotation, activation, and every other command require separate review. |
| P5C | P5B2 | WAITING | The broad service, installation, performance/resource, and publication parent is unchanged and is not promoted by the child split below. |
| P5C1 | P5B2b | READY | Candidate-bound canonical runtime authority generation and isolated atomic installation/compensation proof. This is the sole ready child. |
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
P6+; and every remaining P5B2 command. Product implementation has not started.

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

The checklist path and its pre-staging hash are repeated in the receipt index.
Both external files retain their prior historical starting SHAs, worktree
records, and execution evidence below their conditional migration notices.
