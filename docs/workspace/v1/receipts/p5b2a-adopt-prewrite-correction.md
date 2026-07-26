# P5B2a ADOPT pre-write corrective receipt

Receipt status: `ACCEPTED`

Surface: `P5B2a` corrective evidence

Accepted at live refresh: `2026-07-26T01:40:31Z`

Repository authority state: `STAGED`. This append-only governance receipt
proposal becomes repo-local accepted evidence only when its commit is
separately published and merged into
`Villeneuve-Ventures/graphify@workspace/v1`. Until then, the published
canonical branch remains authoritative.

## Frozen correction

PR #27 changes only `RegistryStore.adopt()`'s cross-UUID persisted
source-identity precondition:

- after registry lock acquisition and normal recovery, ADOPT compares the
  discovered Git common-directory device/inode identity with identities
  persisted under every other repo UUID;
- the check completes before `_authorized_evidence()` and the requested
  ADOPT registry commit;
- a cross-UUID match fails with `UUIDCollisionError`; and
- in the focused stable-state regression, the rejected attempt leaves registry
  revision and entries, evidence files, the complete external-state snapshot,
  and both source checkouts unchanged.

Lock acquisition and recovery may reconcile pre-existing state before the
policy check; this correction makes no broader no-write claim.

All other P5B2a behavior is preserved: explicit ADOPT authorization,
expected-registry-revision CAS, already-bound/path/current-common-directory
checks, retained enrollment-root shared-history policy, and same-UUID
retained-inode adoption. Registration-v1 request/receipt behavior and schemas,
ENROLL, rebind, rotate, active-source state, and every durable record shape are
unchanged. This correction does not reopen P5B2a or grant new command authority.

## Delivery evidence

- Pull request:
  [#27](https://github.com/Villeneuve-Ventures/graphify/pull/27), merged into
  `workspace/v1` at `2026-07-26T00:58:28Z`.
- Exact delivery head:
  `d9cc4a72f1df3a8b938595eb3dcbd7daef4d0f82`.
- Merge/current canonical commit:
  `8b20216016a08da6620bd9fb9ad0a820c660a6ef`.
- Delivery-head, merge, and current canonical tree:
  `3536da7475c3034b6c3c2666e9ff05c0a9c9dc30`.
- The delivery head is a direct parent of the merge commit.

## Exact-head validation

- GitHub Actions run
  [30181132421](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30181132421)
  ran at exact delivery head
  `d9cc4a72f1df3a8b938595eb3dcbd7daef4d0f82` and completed successfully.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The thread-aware closeout preflight returned no review threads, for `0`
  unresolved threads.

## Governance-closeout preflight

- The canonical checkout was clean on `workspace/v1` at
  `8b20216016a08da6620bd9fb9ad0a820c660a6ef`, tree
  `3536da7475c3034b6c3c2666e9ff05c0a9c9dc30`, synchronized with
  `origin/workspace/v1` at divergence `0/0`.
- It was the only worktree before this isolated governance-only proposal was
  created; no competing governance lane or open fork pull request existed.
- Observed support baseline: CPython `3.14.3` and uv `0.11.30`.

## Governance-closeout validation

- `uv lock --check` passed.
- The registration CLI command
  `uv run --frozen --all-extras pytest -q tests/test_workspace_registration_cli.py`
  reported `76 passed`.
- The focused runtime command
  `uv run --frozen --all-extras pytest -q tests/test_workspace_runtime.py -k 'adopt or rebind or rotate'`
  reported `10 passed, 58 deselected`.
- `uv run --frozen python -m tools.skillgen --check` matched all `134`
  committed artifacts.
- `uv run --frozen pre-commit run --all-files` passed both repository hooks.
- `git diff --check` passed.

## Excluded effects

The correction changes no other P5B2a policy, public schema, receipt shape,
command surface, active-source state, or durable storage format. This
governance-only closeout changes no product code, tests, dependencies,
workflows, configuration, generated graph, external plan, production state, or
downstream implementation and grants no execution, publication, merge,
canonical fast-forward, GitHub mutation, or cleanup authority.

## Closeout disposition

P5B2a remains `COMPLETE`; this corrective receipt appends evidence without
reopening the phase. The unnumbered P5B2 identity-maintenance surface is
accepted separately. Parent P5 and P5B2 remain `IN_PROGRESS`; every remaining
P5B2 command remains `WAITING`; broad P5C and all remaining P5C concerns remain
`WAITING`; H3 remains `DEFERRED` and non-blocking; and P6-P12 remain `WAITING`.
No later child is promoted to `READY`.

This receipt accepts only PR #27's cross-UUID persisted Git common-directory
identity check before new source or identity-action evidence and the requested
ADOPT registry commit. It grants no new implementation authority.
