# P5B2 active-source activation accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 active-source activation` (unnumbered)

Accepted at live refresh: `2026-07-26T17:55:23Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

The accepted public surface contains exactly this standalone form:

```text
graphify workspace activate --repo-uuid UUID --expected-registry-revision N --expected-active-source-revision N --expected-operation-epoch N --expected-migration-epoch N --authorization-stdin
```

The command:

- loads and composes installed runtime authority before consuming one bounded,
  canonical, action-matching `ACTIVATE` authorization;
- requires the explicit repo UUID plus registry, active-source, operation, and
  migration CAS values while deriving lease owner, wall time, monotonic time,
  and the 30-second lease TTL from trusted runtime inputs;
- requires the current working directory itself to be the Git top level and
  exactly revalidates the source across two discovery passes;
- requires an explicitly bound target that shares an immutable enrollment
  history root or retains the enrolled Git common-directory device/inode;
- rejects reselecting the current active source under the registry lock before
  lease, evidence, revision, or active-source mutation;
- delegates exactly once to the existing fenced
  `RegistryStore.activate_source()` policy; and
- emits one canonical redacted `graphify.workspace.activation` CLI-v1 receipt
  without exposing authorization, paths, lease-owner identity, or raw errors.

Registration v1 remains limited to `enroll` and `adopt`; `register activate`
remains invalid. Durable schema v1 and existing registry, workspace, lock,
lease, and evidence record shapes remain unchanged.

## Retained-source identity-continuity deferral

One focused P5B2 hardening follow-up covers two inherited registry nonclaims:

- `RegistryStore.rotate_enrollment_evidence()` requires an explicitly bound
  locator but does not independently repeat the immutable enrollment-history
  or enrolled Git-common-directory continuity check before rotating evidence;
  and
- `RegistryStore.resolve_active_source()` rediscovers the recorded active
  locator and compares its repo UUID and locator fields, but does not
  independently compare that source with immutable enrollment evidence.

The activation boundary itself performs the immutable enrollment-continuity
check before mutation, so these retained-source nonclaims do not broaden or
invalidate this receipt. The follow-up must preserve the accepted activation,
registration, and identity-maintenance contracts.

Additional sync modes, migrate, rollback, GC, repair, every mutation beyond
accepted activation, every query authority beyond P5B2c's one-shot transport,
production installation, watch/service, performance or resource qualification,
candidate publication, H3, P6+, and cleanup remain excluded.

## Delivery evidence

- Pull request:
  [#29](https://github.com/Villeneuve-Ventures/graphify/pull/29), merged into
  `workspace/v1` at `2026-07-26T17:40:11Z`.
- Exact delivery head:
  `9f3b3b8af1e1d712d25febf81665d75feff96637`.
- Merge/current canonical commit:
  `0f46a86c03281d6ab3b52243f2881d2d18f1fad6`.
- Delivery-head, merge, and current canonical tree:
  `2afc7a1f3610ac712db3cdbe5fd9adb8c9150141`.
- The delivery head is a direct parent of the merge commit.

## Exact-head validation

- GitHub Actions run
  [30212289087](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30212289087)
  ran at exact delivery head
  `9f3b3b8af1e1d712d25febf81665d75feff96637` and completed successfully.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The hosted Python 3.14 job reported `4,659 passed, 4 skipped`.

## Governance-closeout preflight

- The canonical checkout was clean on `workspace/v1` at
  `0f46a86c03281d6ab3b52243f2881d2d18f1fad6`, tree
  `2afc7a1f3610ac712db3cdbe5fd9adb8c9150141`, synchronized with
  `origin/workspace/v1` at divergence `0/0`.
- It was the only worktree before this isolated governance-only proposal was
  created; no competing governance lane or open fork pull request existed, and
  repository Issues were disabled.
- Observed support baseline: CPython `3.14.3` and uv `0.11.30`.

## Governance-closeout validation

- `uv lock --check` passed.
- The activation CLI command
  `uv run --frozen --all-extras pytest -q tests/test_workspace_activation_cli.py`
  reported `47 passed`.
- Three focused registry-runtime cases covering evidence rotation, activation
  CAS, active-source resolution, alias ambiguity, and missing evidence reported
  `3 passed`.
- `uv run --frozen python -m tools.skillgen --check` matched all `134`
  committed artifacts.
- `uv run --frozen pre-commit run --all-files` passed both repository hooks.
- The advisory documentation-diff audit confirmed docs-only scope, and
  `git diff --check` passed.

## Excluded effects

This governance-only closeout changes no product code, tests, schemas,
dependencies, workflows, configuration, generated graph, external portfolio
plan, production state, or downstream implementation. It performs no real-home
installation and changes no provider, service/watch, publication,
performance/resource, H3, or P6+ authority.

## Closeout disposition

The unnumbered P5B2 active-source activation surface alone transitions to
`COMPLETE`. The combined retained-source identity-continuity hardening
follow-up remains `DEFERRED`. Parent P5 and P5B2 remain `IN_PROGRESS`; every
remaining P5B2 command remains `WAITING`; broad P5C and all remaining P5C
concerns remain `WAITING`; H3 remains `DEFERRED` and non-blocking; and P6-P12
remain `WAITING`. No later child is promoted to `READY`.

This receipt accepts only PR #29's standalone active-source activation
behavior. It does not authorize another implementation, publication, merge,
canonical fast-forward, branch/worktree cleanup, or GitHub review-thread
mutation.
