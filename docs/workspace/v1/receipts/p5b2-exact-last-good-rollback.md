# P5B2 exact-last-good rollback accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 exact-last-good rollback` (unnumbered)

Accepted at live refresh: `2026-07-27T19:30:47Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

The accepted public surface contains exactly this standalone form:

```text
graphify workspace rollback --request-stdin
```

The command:

- loads and composes installed runtime authority before reading one canonical,
  duplicate-free rollback request of at most 16 KiB;
- binds the repo UUID, registry, active-source, operation, migration, and
  pointer revisions, the current receipt, the target source epoch, the visible
  pointer's exact non-null `last_good` generation and receipt, and canonical
  action-matching `ROLLBACK` authorization;
- rejects current-generation reselection, an arbitrary historical generation,
  a missing target, stale authority, or a target outside the accepted source
  and pointer evidence;
- preflights the exact target, acquires one trusted 30-second `ROLLBACK` lease,
  and revalidates the pointer and target under the accepted grant;
- derives accepted operation, migration, active-source, and fence authority
  from the grant, threads its exact liveness deadline through post-acquisition
  reads, generation locks, journal recovery, and the durable write boundary,
  and delegates exactly once to `PointerStore.rollback()`;
- preserves exact-`last_good` eligibility when a stale promotion recorded that
  retained target as `SUPERSEDED`, without admitting any other superseded or
  historical target; and
- emits one canonical `graphify.workspace.rollback` CLI-v1 receipt on success
  while conflict and invalid outcomes remain redacted.

The existing pointer, generation, journal, lease, recovery, and durable-state
formats retain ownership of the `ROLLED_BACK` transition. The request and
receipt schemas remain the canonical CLI-v1 structural authority. Recovery is
performed only through the existing fenced policy: the lease deadline is
checked before any journal-recovery write and immediately before the durable
pointer/journal commit. Commit uncertainty preserves the pointer-recovery
barrier, best-effort lease release cannot mask the primary failure, and
`InjectedFault` is re-raised when it is the primary error or the only release
error.

Semantic sync, migrate, GC, repair, arbitrary historical selection, broader
mutation or query authority, production installation, watch/service,
performance or resource qualification, candidate publication, retained-source
identity-continuity hardening, H3, P6+, and cleanup remain excluded.

## Delivery evidence

- Pull request:
  [#31](https://github.com/Villeneuve-Ventures/graphify/pull/31), merged into
  `workspace/v1` at `2026-07-27T15:14:57Z`.
- Exact delivery head:
  `d0af2809ea0a79fb9b041e0b814b4b50799520f6`.
- Merge/current canonical commit:
  `179c12de34db9cbfbfa731e594413653f2118a15`.
- Delivery-head, merge, and current canonical tree:
  `782049cc2c52dfde83c0946f8298d35f932ab3c6`.
- The delivery head is the second direct parent of the merge commit; the first
  parent is `68f7c697883896f05c0b4b83c57ee9b9cef3a054`.

## Exact-head validation

- GitHub Actions run
  [30273434230](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30273434230)
  ran at exact delivery head
  `d0af2809ea0a79fb9b041e0b814b4b50799520f6` and completed successfully.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The hosted Python 3.14 job reported `4,749 passed, 4 skipped, 5 warnings`.

## Governance-closeout preflight

- The canonical checkout was clean on `workspace/v1` at
  `179c12de34db9cbfbfa731e594413653f2118a15`, tree
  `782049cc2c52dfde83c0946f8298d35f932ab3c6`, synchronized with
  `origin/workspace/v1` at divergence `0/0`.
- It was the only worktree before this isolated governance-only proposal was
  created. No rollback-governance branch, worktree, pull request, competing
  governance lane, or open fork pull request existed, and repository Issues
  were disabled.
- GitHub reported `20` review threads, `8` unresolved, and `7` both current and
  unresolved. Six current behavior findings were implemented at the exact
  delivery head: generation-lock deadline propagation, post-acquisition
  timeout reclassification, current/`last_good` distinctness, journal-recovery
  deadline enforcement, terminal pointer-revision rejection, and retained
  exact-`last_good` eligibility after stale promotion. The remaining S106
  suggestion does not apply because Ruff selects only `E9`, `F63`, `F7`, and
  `F82`. No still-valid unfixed review issue remained.
- Observed support baseline: CPython `3.14.3` and uv `0.11.30`.

## Governance-closeout validation

- `uv lock --check` passed against the frozen lock.
- The required rollback CLI, pointer, and journal recovery command reported
  `81 passed`:

  ```text
  uv run --frozen --all-extras pytest -q \
    tests/test_workspace_rollback_cli.py \
    tests/test_workspace_pointers.py::test_rollback_preserves_exact_last_good_after_stale_promotion \
    tests/test_workspace_pointers.py::test_rollback_rechecks_exact_lease_deadline_before_durable_write \
    tests/test_workspace_pointers.py::test_rollback_threads_deadline_through_generation_locks_and_receipt_read \
    tests/test_workspace_journal.py::test_journal_recovery_does_not_discard_torn_tail_after_deadline \
    tests/test_workspace_journal.py::test_journal_recovery_head_commit_honors_deadline
  ```
- `uv run --frozen python -m tools.skillgen --check` matched all `134`
  committed artifacts.
- `uv run --frozen pre-commit run --all-files` passed both repository hooks.
- The documentation-diff audit confirmed exactly the four allowlisted files,
  and `git diff --check` passed.

## Excluded effects

This governance-only closeout changes no product code, tests, schemas,
dependencies, workflows, configuration, generated graph, external portfolio
plan, production state, or downstream implementation. It performs no real
`HOME`, XDG, or `CODEX_HOME` write and changes no semantic-sync, migrate, GC,
repair, arbitrary-history, provider, service/watch, installation, publication,
performance/resource, retained-source hardening, H3, or P6+ authority.

## Closeout disposition

The unnumbered P5B2 exact-last-good rollback surface alone transitions to
`COMPLETE`. Parent P5 and P5B2 remain `IN_PROGRESS`; retained-source identity
continuity remains `DEFERRED`; every remaining P5B2 command remains `WAITING`;
broad P5C and all remaining P5C concerns remain `WAITING`; H3 remains
`DEFERRED` and non-blocking; and P6-P12 remain `WAITING`. No later child is
promoted to `READY`.

This receipt accepts only PR #31's one-step exact-last-good rollback behavior.
It does not authorize another implementation, publication, merge, canonical
fast-forward, branch/worktree cleanup, graph refresh, external-plan mutation,
real-user-state write, or GitHub comment or review-thread mutation.
