# P5B2 public fenced offline-GC lifecycle accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 public fenced offline-GC lifecycle` (unnumbered)

Accepted at live refresh: `2026-07-29T02:54:25Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

This surface accepts exactly the public fenced offline-GC lifecycle commands:

```text
graphify workspace gc --execute --request-stdin
graphify workspace gc --reconcile --request-stdin
graphify workspace gc --purge --request-stdin
```

The frozen product contract remains in
[`../README.md`](../README.md#explicit-fenced-lifecycle). Each exact form loads
and composes installed runtime authority before consuming one bounded canonical
CLI-v1 request. The request carries the explicit repo UUID; expected registry,
active-source, operation, migration, and pointer revisions; `timeout_ms`; the
complete `CapacityPolicy`; all six `GcProtection` classes; and a matching
canonical `GC_EXECUTE`, `GC_RECONCILE`, or `GC_PURGE` authorization.

Execute additionally binds `approved_preview_sha256` to the SHA-256 of the exact
canonical preview-result bytes, including their terminating newline. It
recomputes those read-only bytes, requires an exact digest match, acquires a
fresh trusted `GC` lease, and requires the fresh fenced plan's non-fence
projection to match the approved preview before quarantining only that plan's
candidates. Its public success receipt is canonical and redacted.

Reconcile remains explicit. It mutates only an already-existing durable GC
intent under fresh fenced `GC` authority. A request matching a still-current
operation epoch can replay its immutable completion without acquiring a lease
or writing state; with neither an intent nor a matching completion, it returns
the no-write `nothing_to_reconcile` result. It never invents a plan or mutates an
unrelated lifecycle phase.

Purge remains explicit, exact-plan-bound, and idempotent. First-time deletion
uses fresh fenced `GC` authority and rechecks pointer revision, protection, and
generation locks. Exact terminal replay returns the durable no-write purge
receipt. The public receipts expose no authorization, raw internal state, lease
owner or fence data, paths, timestamps, operation epochs, environment values,
or raw errors.

The request's absolute deadline remains in force through lease acquisition,
planning, generation locks, fenced mutation, and recursive deletion. The public
4096-generation bound is enforced through descriptor-relative no-follow
enumeration that reads at most one additional entry before materializing the
set. This is a traversal-safety bound, not performance or resource
qualification.

Automatic, online, or service GC; repair; migrate; semantic sync; mutation
beyond this exact lifecycle; broader query authority; production installation;
watch/service; publication; performance or resource proof; H3; P6+; cleanup;
and real-user-state writes remain excluded.

## Delivery evidence

- Pull request:
  [#37](https://github.com/Villeneuve-Ventures/graphify/pull/37), merged into
  `workspace/v1` at `2026-07-29T02:36:46Z`.
- Exact delivery head:
  `b2454ea78ce80b0e3aa25c7c73d2a073da4ca38a`.
- Merge/current canonical commit:
  `e1a5ebfe936f75f56a1309e35fff29c7aa6d0646`.
- Delivery-head, merge, and current canonical tree:
  `02bb3582b055bec478d3f1caea31baf797417889`.
- The delivery head is the second direct parent of the merge commit; the first
  parent is `1af466d58e91541fc95b3af66c3c18a2ce0b70a6`.
- The delivery changed exactly 29 files: four Workspace documents, ten
  product/runtime modules, eight schemas, six test/helper files, and one
  artifact-builder file.

## Exact-head hosted validation

- GitHub Actions run
  [30416810686](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30416810686)
  was associated with exact delivery head
  `b2454ea78ce80b0e3aa25c7c73d2a073da4ca38a`, checked out synthetic merge
  commit `1889b1cd3c92500b190d37b53b7baa6644b0e5d8`, and completed successfully.
- The synthetic merge has parents
  `1af466d58e91541fc95b3af66c3c18a2ce0b70a6` and
  `b2454ea78ce80b0e3aa25c7c73d2a073da4ca38a`; its tree
  `02bb3582b055bec478d3f1caea31baf797417889` exactly matches the delivery,
  merge, and current canonical tree.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The hosted Python 3.14 full-suite job reported `4,897 passed, 4 skipped, 5
  warnings`.

## Current review-thread state

- GitHub reported 20 review threads: ten resolved and ten unresolved records.
  No unresolved record was replied to or resolved during this closeout.
- The latest submitted review was Codex `COMMENTED` at
  `2026-07-29T02:13:16Z` against pre-delivery commit
  `cb20c283265bde9a3cb0166b355fffc0ea366021`. Final delivery commit
  `b2454ea78ce80b0e3aa25c7c73d2a073da4ca38a` bounded the completion read and
  classified a mismatched reconcile capacity policy as a request conflict, with
  focused regressions for both findings.
- The other eight unresolved records were also rechecked against final code and
  tests. The merged delivery routes irreconcilable intents to repair, preserves
  completed execute/reconcile receipts across release uncertainty, retries an
  uncertain purge with the exact purge request, replays current-epoch
  completions, rejects nonexistent authorization dates, starts lease liveness
  from a fresh post-preflight clock value, propagates the absolute request
  deadline through fenced mutation, and checks it during recursive deletion.
- The focused thread-regression selection passed `19` tests. All ten unresolved
  records are addressed workflow residue; none remains technically actionable.

## Focused local validation

- `uv lock --check` passed against the frozen lock and resolved `166` packages.
- `uv run --frozen --all-extras pytest -q tests/test_workspace_gc.py tests/test_workspace_gc_cli.py`:
  `253 passed, 1 warning`.
- `uv run --frozen python -m tools.skillgen --check` matched all `134`
  committed artifacts.
- `uv run --frozen pre-commit run --all-files` passed both repository hooks.

## Historical broader validation

- The delivery PR records a complete GC-suite run of `253 passed` and a prior
  controlled committed-tree baseline of `4,873 passed, 3 skipped`.
- It also records a post-review full-repository run with `4,882 passed, 3
  skipped` and three non-GC failures isolated to a hidden-parent worktree
  assertion and two transient cases whose exact reruns passed. These historical
  observations are corroboration, not substitutes for the successful exact-head
  hosted run above.

## Governance-closeout preflight

- The canonical checkout was clean on `workspace/v1` at
  `e1a5ebfe936f75f56a1309e35fff29c7aa6d0646`, tree
  `02bb3582b055bec478d3f1caea31baf797417889`. Live GitHub and
  `origin/workspace/v1` were at the same commit and tree, at local divergence
  `0/0`.
- The canonical checkout was the only worktree. No delivery worktree,
  competing governance worktree, open fork pull request, or other closeout owner
  existed.
- Delivery ancestry and tree parity were proved locally; exact-head hosted CI
  and the separate CodeRabbit status were green.
- All 20 review threads and the latest submitted review were re-fetched. Ten
  records remained unresolved in GitHub, but every one was verified as addressed
  workflow residue against merged code and focused tests.
- Observed support baseline: CPython `3.14.6` and uv `0.11.30`.

## Governance-closeout validation

- The advisory documentation-diff audit confirmed docs-only scope; its material
  claim notices were verified against live Git, GitHub, code, tests, and current
  repository authority.
- The working-tree manifest contained exactly the four allowlisted files,
  repository-relative receipt links and the implementation-document anchor
  resolved, and `git diff --check` passed.
- The independent docs-only review's only finding was this section's
  then-pending validation placeholder. It reported no inaccurate scope, stale
  evidence, status overclaim, or host-local path; the placeholder was corrected
  before commit.

## Excluded effects

This governance-only closeout changes no product code, tests, schemas,
dependencies, workflows, configuration, generated graph, external portfolio
plan, production state, or downstream implementation. It performs no real
`HOME`, XDG, or `CODEX_HOME` write and adds no automatic, online, or service GC;
repair; migrate; semantic-sync; broader mutation or query; installation;
publication; performance/resource; H3; or P6+ authority.

## Closeout disposition

The P5B2 public fenced offline-GC lifecycle surface alone transitions to
`COMPLETE`. Parent P5 and P5B2 remain `IN_PROGRESS`; semantic sync, migrate,
repair, broader mutation/query authority, and every other undelivered P5B2
command remain `WAITING`; broad P5C and all remaining P5C concerns remain
`WAITING`; H3 remains `DEFERRED` and non-blocking; and P6-P12 remain `WAITING`.
The current authority set freezes no later child boundary, so no later child is
promoted to `READY`.

This receipt accepts only PR #37's public fenced offline-GC lifecycle. It does
not authorize another implementation, publication, merge, canonical
fast-forward, branch/worktree cleanup, graph refresh, external-plan mutation,
real-user-state write, or GitHub comment or review-thread mutation.
