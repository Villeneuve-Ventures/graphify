# P5B2 identity-maintenance accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 identity maintenance` (unnumbered)

Accepted at live refresh: `2026-07-26T01:40:31Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

The accepted public surface contains exactly these two forms:

```text
graphify workspace register rebind --repo-uuid UUID --expected-registry-revision N --authorization-stdin
graphify workspace register rotate --repo-uuid UUID --expected-registry-revision N --authorization-stdin
```

Both forms:

- load and compose installed runtime authority before consuming authorization;
- require the explicit repo UUID, expected registry-revision CAS, Git-top-level
  source proof, and one bounded, canonical, action-matching authorization;
- reuse source discovery, exact Git revalidation, external-state containment,
  failure redaction, and the existing registry methods without adding a second
  persistence path;
- emit the separate canonical
  `graphify.workspace.identity_maintenance` CLI-v1 receipt; and
- preserve `active_source` and `active_source_revision`.

Rebind preserves the existing enrolled-common-directory or shared-history
policy and rejects a source whose persisted identity belongs to another repo
UUID before `_authorized_evidence()` and the requested rebind registry commit.
Rotate preserves the existing explicitly-bound-source requirement. Registry
lock acquisition and recovery may reconcile pre-existing state before this
policy check; this receipt makes no broader no-write claim.

Registration v1 remains limited to `enroll` and `adopt`; its schema remains
byte-identical to the delivery base with SHA-256
`dc7c80798fa77c746e256d87e1714cf5e04ccd0fc4f7c91f53c3e52bfc1ce1ce`.
Durable schema v1 and existing registry, workspace, lock, and evidence record
shapes remain unchanged.

Activation, additional sync modes, migrate, rollback, GC, repair, broader
mutation or query authority, production installation, watch/service,
performance or resource qualification, candidate publication, H3, P6+, and
cleanup remain excluded.

## Delivery evidence

- Pull request:
  [#26](https://github.com/Villeneuve-Ventures/graphify/pull/26), merged into
  `workspace/v1` at `2026-07-25T21:47:45Z`.
- Exact delivery head:
  `2e6da25323b78efd8bdc3ef78bb5180cf3c43053`.
- Merge commit: `50859ecb176c10a21f5caeb4112ccd7883b89d7d`.
- Delivery-head and merge tree:
  `1172478d370dba752b4ed277acd1caac6cbd839d`.
- The merge commit has sole parent
  `fde03397f16305a1cef8a5bf8d784dcd68f90b4e`, not the delivery head. This
  receipt freezes the delivery identity and equal delivery/merge trees without
  claiming that the delivery head is an ancestor of the merge.
- The later canonical head is
  `8b20216016a08da6620bd9fb9ad0a820c660a6ef`, with distinct later tree
  `3536da7475c3034b6c3c2666e9ff05c0a9c9dc30`; PR #26's tree is not presented
  as the current tree.

## Exact-head validation

- GitHub Actions run
  [30172919995](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30172919995)
  ran at exact delivery head
  `2e6da25323b78efd8bdc3ef78bb5180cf3c43053` and completed successfully.
- `skillgen-check`, `test (3.14)`, and `security-scan` succeeded; the separate
  `CodeRabbit` status context also succeeded.
- The thread-aware closeout preflight returned two review threads, both
  resolved, for `0` unresolved threads.

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

This governance-only closeout changes no product code, tests, schemas,
dependencies, workflows, configuration, generated graph, external portfolio
plan, production state, or downstream implementation. It performs no
real-home installation and grants no execution, publication, merge, canonical
fast-forward, GitHub mutation, or cleanup authority.

## Closeout disposition

The unnumbered P5B2 identity-maintenance surface alone transitions to
`COMPLETE`. Parent P5 and P5B2 remain `IN_PROGRESS`; P5B2a remains `COMPLETE`;
every remaining P5B2 command remains `WAITING`; broad P5C and all remaining
P5C concerns remain `WAITING`; H3 remains `DEFERRED` and non-blocking; and
P6-P12 remain `WAITING`. No later child is promoted to `READY`.

This receipt accepts only PR #26's rebind/rotate behavior. It does not
authorize another implementation or any excluded effect.
