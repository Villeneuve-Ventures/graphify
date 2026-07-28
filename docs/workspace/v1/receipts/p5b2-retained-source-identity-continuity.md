# P5B2 retained-source identity-continuity accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 retained-source identity continuity` (unnumbered)

Accepted at live refresh: `2026-07-28T02:00:53Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

This registry-hardening surface adds no new public command. It closes exactly
two inherited retained-source identity-continuity nonclaims within the existing
identity-maintenance and active-source-resolution paths:

- `RegistryStore.rotate_enrollment_evidence()` still requires an explicitly
  bound source and now reads its immutable enrollment evidence once to require
  either a shared history root or the enrolled Git common-directory
  device/inode before the requested source evidence, identity-action evidence,
  or registry revision is persisted;
- `RegistryStore.resolve_active_source()` still rediscovers the recorded active
  locator and checks its repo UUID and locator fields, then independently
  requires the same immutable enrollment continuity before returning the
  source; and
- locator-compatible replacements with unrelated history and a different Git
  common directory fail closed through the existing `SourceAmbiguousError`
  boundary without adding a CLI reason code or receipt schema.

For a settled external-state fixture, the rejected-rotation regression keeps
registry, evidence, workspace, and source-checkout bytes unchanged. That claim
covers only the requested rotation: registry lock acquisition and recovery may
reconcile pre-existing state before this policy check. Durable schema v1,
registration and identity-action receipt schemas, authorization actions,
active-source selection semantics, and the existing ADOPT, REBIND, and
ACTIVATE boundaries remain unchanged. ROTATE's only accepted change is this
additional retained-source continuity precondition.

Semantic sync, migrate, GC, repair, every other mutation, broader query
authority, production installation, watch/service, performance or resource
qualification, candidate publication, provider work, H3, P6+, and cleanup
remain excluded.

## Delivery evidence

- Pull request:
  [#33](https://github.com/Villeneuve-Ventures/graphify/pull/33), merged into
  `workspace/v1` at `2026-07-28T01:41:46Z`.
- Exact delivery head:
  `4444d8206604d84ce648aded8ee6467d3a603f4b`.
- Merge/current canonical commit:
  `5c1168cb29cdc1529852289692fb9ed5bda1ea0c`.
- Delivery-head, merge, and current canonical tree:
  `a6412546e944e9400e664561686229d22a11820f`.
- The delivery head is the second direct parent of the merge commit; the first
  parent is `670cd633bf02691d7463361c139b9d8cdbe80006`.
- The delivery changed exactly the Workspace README, architecture, and
  verification documents, the registry implementation, and the registration
  CLI and runtime test files.

## Exact-head validation

- GitHub Actions run
  [30318514569](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30318514569)
  was associated with exact delivery head
  `4444d8206604d84ce648aded8ee6467d3a603f4b`, checked out synthetic merge
  commit `bcb351eab95becf187643f8056bd8d49fbb252fa`, and completed successfully.
- The synthetic merge has parents
  `670cd633bf02691d7463361c139b9d8cdbe80006` and
  `4444d8206604d84ce648aded8ee6467d3a603f4b`; its tree
  `a6412546e944e9400e664561686229d22a11820f` exactly matches the delivery,
  merge, and current canonical tree.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The hosted Python 3.14 full-suite job reported `4,753 passed, 4 skipped, 5
  warnings`.
- Hosted `skillgen-check` matched all `134` committed artifacts; the security
  job's Bandit, byte-identical-candidate build, and wheel/locked-dependency
  audit steps all succeeded.

## Governance-closeout preflight

- The canonical checkout was clean on `workspace/v1` at
  `670cd633bf02691d7463361c139b9d8cdbe80006`, tree
  `779fa9f3fe203b31f2d75bfa0f23b49b447f1101`. Live GitHub and
  `origin/workspace/v1` were exactly
  `5c1168cb29cdc1529852289692fb9ed5bda1ea0c`, tree
  `a6412546e944e9400e664561686229d22a11820f`, at local divergence `0/2`.
  The canonical checkout was not fast-forwarded.
- It was the only worktree before this isolated governance-only proposal was
  created. No retained-source governance branch, worktree, pull request,
  competing governance lane, or open fork pull request existed.
- GitHub reported one review thread, resolved, and zero unresolved threads.
  Its duplicate-evidence-read finding was fixed at the exact delivery head.
  The separate PR-Agent common-directory advisory does not apply because
  discovered source identities carry concrete Git common-directory device and
  inode values; the CodeRabbit docstring-coverage warning was not an enforced
  repository gate. No still-valid unfixed review issue remained.
- Observed support baseline: CPython `3.14.3` and uv `0.11.30`.

## Governance-closeout validation

- `uv lock --check` passed against the frozen lock.
- The registration CLI command
  `uv run --frozen --all-extras pytest -q tests/test_workspace_registration_cli.py`
  reported `77 passed`.
- The focused runtime command
  `uv run --frozen --all-extras pytest -q tests/test_workspace_runtime.py -k 'adopt or rebind or rotate or resolve_active_source'`
  reported `13 passed, 67 deselected`.
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
`HOME`, XDG, or `CODEX_HOME` write and changes no next-command, semantic-sync,
migrate, GC, repair, provider, service/watch, installation, publication,
performance/resource, H3, or P6+ authority.

## Closeout disposition

The P5B2 retained-source identity-continuity surface alone transitions to
`COMPLETE`. Parent P5 and P5B2 remain `IN_PROGRESS`; every remaining P5B2
command remains `WAITING`; broad P5C and all remaining P5C concerns remain
`WAITING`; H3 remains `DEFERRED` and non-blocking; and P6-P12 remain `WAITING`.
No later child is promoted to `READY`.

This receipt accepts only PR #33's retained-source identity-continuity
hardening. It does not authorize another implementation, publication, merge,
canonical fast-forward, branch/worktree cleanup, graph refresh, external-plan
mutation, real-user-state write, or GitHub comment or review-thread mutation.
