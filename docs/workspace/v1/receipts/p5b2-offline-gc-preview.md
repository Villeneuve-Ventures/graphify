# P5B2 bounded offline-GC preview accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 bounded offline-GC preview` (unnumbered)

Accepted at live refresh: `2026-07-28T15:25:47Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

This surface accepts exactly the public read-only command:

```text
graphify workspace gc --dry-run --request-stdin
```

The exact argv loads and composes installed runtime authority before consuming
one bounded canonical CLI-v1 request. The caller supplies the repo UUID;
expected registry, active-source, operation, migration, and pointer revisions;
`timeout_ms`; the complete `CapacityPolicy`; and all six `GcProtection`
classes. The request parser infers no capacity, protection, path, provider, or
environment-backed value. The unchanged installed-authority locator selects
the external state root through its existing `XDG_STATE_HOME` / `HOME`
contract before the request read; that locator is not a request default and
creates nothing.

The composed runtime uses the existing read-only registry/workspace
coordination and generation-lock probes and requires two matching reachability
snapshots. Success emits exactly one deterministic canonical unfenced
`graphify.workspace.gc_preview_result` v1 object containing the candidates,
stable protection reasons, observed revisions, and capacity-policy SHA-256. It
creates no `LeaseGrant`, fence, or executable `GcPlan`.

The preview makes zero durable writes on success and every failure. It creates
or cleans no leases, recovery state, temporary files, directories, modes,
locks, registry records, intents, quarantine records, receipts, query logs,
source data, or Git data and leaves `HOME`, `XDG_STATE_HOME`, and `CODEX_HOME`
unchanged. Existing fenced `GcStore.plan()`, `execute()`, `reconcile()`, and
`purge()` semantics remain unchanged and outside this public command.

This acceptance makes no performance or resource qualification and no bounded
pre-enumeration traversal claim. The published CLI-v1 capacity-policy fields
remain frozen; any compatibility change requires separate versioned review.
GC mutation, quarantine, reconciliation, purge, repair, migrate, semantic
sync, broader mutation or query authority, production installation,
service/watch, candidate publication, H3, P6+, and cleanup remain excluded.

## Delivery evidence

- Pull request:
  [#35](https://github.com/Villeneuve-Ventures/graphify/pull/35), merged into
  `workspace/v1` at `2026-07-28T15:04:11Z`.
- Exact delivery head:
  `b32503e0aabf802970d9d7032a07e0a322f41c28`.
- Merge/current canonical commit:
  `864a3e77a66f83a45e3ee9395180dc511b4bf059`.
- Delivery-head, merge, and current canonical tree:
  `1104ac8a74b4abd1bf2e46cb1439cc3d29d6639a`.
- The delivery head is the second direct parent of the merge commit; the first
  parent is `129e4d561a10061f2629780b5f5c221c0f19449b`.
- The delivery changed exactly four Workspace documents, five product/runtime
  modules, two CLI-v1 schemas, five test/helper files, and one artifact-builder
  file.

## Exact-head validation

- GitHub Actions run
  [30367007598](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30367007598)
  was associated with exact delivery head
  `b32503e0aabf802970d9d7032a07e0a322f41c28`, checked out synthetic merge
  commit `8d3b5ecad5cbd433e3c03a5d3052b41f6764a00b`, and completed successfully.
- The synthetic merge has parents
  `129e4d561a10061f2629780b5f5c221c0f19449b` and
  `b32503e0aabf802970d9d7032a07e0a322f41c28`; its tree
  `1104ac8a74b4abd1bf2e46cb1439cc3d29d6639a` exactly matches the delivery,
  merge, and current canonical tree.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The hosted Python 3.14 full-suite job reported `4,818 passed, 4 skipped, 5
  warnings`.

## Governance-closeout preflight

- The canonical checkout was clean on `workspace/v1` at
  `864a3e77a66f83a45e3ee9395180dc511b4bf059`, tree
  `1104ac8a74b4abd1bf2e46cb1439cc3d29d6639a`. Live GitHub and
  `origin/workspace/v1` were at the same commit and tree, at local divergence
  `0/0`.
- The canonical checkout was the only worktree. No delivery worktree,
  competing governance worktree, or open fork pull request existed.
- GitHub reported one review thread, resolved, and zero unresolved threads.
- Observed support baseline: CPython `3.14.3` and uv `0.11.30`.

## Governance-closeout validation

- `uv lock --check` passed against the frozen lock.
- `uv run --frozen --all-extras pytest -q tests/test_workspace_gc.py tests/test_workspace_gc_cli.py`:
  `174 passed, 1 warning`.
- `uv run --frozen python -m tools.skillgen --check` matched all `134`
  committed artifacts.
- `uv run --frozen pre-commit run --all-files` passed both repository hooks.
- The advisory documentation-diff audit confirmed docs-only scope, the
  working-tree manifest contained exactly the four allowlisted files, and
  `git diff --check` passed.

## Excluded effects

This governance-only closeout changes no product code, tests, schemas,
dependencies, workflows, configuration, generated graph, external portfolio
plan, production state, or downstream implementation. It performs no real
`HOME`, XDG, or `CODEX_HOME` write and changes no GC mutation, quarantine,
reconciliation, purge, repair, migrate, semantic-sync, provider,
service/watch, installation, publication, performance/resource, H3, or P6+
authority.

## Closeout disposition

The P5B2 bounded offline-GC preview surface alone transitions to `COMPLETE`.
Parent P5 and P5B2 remain `IN_PROGRESS`; every remaining P5B2 command remains
`WAITING`; broad P5C and all remaining P5C concerns remain `WAITING`; H3
remains `DEFERRED` and non-blocking; and P6-P12 remain `WAITING`. No later
child is promoted to `READY`.

This receipt accepts only PR #35's bounded offline-GC preview. It does not
authorize another implementation, publication, merge, canonical fast-forward,
branch/worktree cleanup, graph refresh, external-plan mutation,
real-user-state write, or GitHub comment or review-thread mutation.
