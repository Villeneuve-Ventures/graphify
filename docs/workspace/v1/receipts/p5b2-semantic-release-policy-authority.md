# P5B2 semantic-release policy-authority provisioning accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 semantic-release policy-authority provisioning` (unnumbered
prerequisite)

Accepted at live refresh: `2026-08-17T05:38:16Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

This receipt accepts only the private provisioning mechanism frozen in
[`../semantic-sync.md`](../semantic-sync.md#p5b2-semantic-release-policy-authority-provisioning-prerequisite).
Acceptance requires PR #71's contract freeze, PR #72's implementation delivery,
and PR #74's lock-discipline correction together. PR #72 alone does not satisfy
the final accepted boundary.

The accepted surface is exactly:

- private `SemanticReleasePolicyAuthorityStore` ownership of the fixed
  current/previous/pending records;
- closed structured `SELECT_SEMANTIC_RELEASE_POLICY` input, canonical
  body/envelope/record digest preimages, exact revision-plus-one and predecessor
  CAS, and `ACTIVE`-only selection;
- shared-read and exclusive-write registry-then-workspace lock discipline,
  including one retained initialization-election lock;
- fixed three-record plus one-temporary namespace with a hard 256 KiB
  transaction peak preflighted before pending visibility;
- durable pending, previous, current, pending-clear commit and exact recovery;
  and
- commit-uncertainty, byte-identical replay, and fail-closed corruption rules.

Acceptance provisions no live policy-authority record and chooses no release
context, profile set, coverage declaration, policy mapping, or operator values.
It creates no decision binding, public CLI, public schema, runtime receipt,
provider/backend, publication, release, execution, parent completion, or
successor authority. `REVOKED` remains consumer-side fail-closed vocabulary;
this prerequisite cannot revoke or reactivate.

## Delivery evidence

### PR #71 contract freeze

- Pull request: [#71](https://github.com/Villeneuve-Ventures/graphify/pull/71),
  merged at `2026-08-15T13:43:21Z`.
- Exact base: `17505a5c03e8945c2d3be932ce85cc09b93883fe`.
- Exact head: `0e8ee7457089c7f58c1bf98c8fe89eb263c7b73b`.
- Merge commit: `5d534d0b769f1217ed0a1574fb54915504892b4c`.
- Head and merge tree: `bad5348abaa1a59f01f3eaa48a3126b58d1bbeb0`.
- Exact-head CI [31885426041](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31885426041)
  and exact-merge CI [31887969988](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31887969988)
  passed.
- Changed manifest: the seven maintained-current contract documents in
  `docs/workspace/v1/` and no code, tests, schemas, receipts, or generated
  output.

### PR #72 implementation delivery

- Pull request: [#72](https://github.com/Villeneuve-Ventures/graphify/pull/72),
  merged at `2026-08-15T21:55:17Z`.
- Exact base: `5d534d0b769f1217ed0a1574fb54915504892b4c`.
- Exact head: `5e6e91fdc7ab6c6cd764e4ee0a04f76e77f643ea`.
- Merge commit: `b88e81bae1bdfec9ab960199b42cd81e582e41b5`.
- Head and merge tree: `b046110da1ba7246d579a6d5bc39c9550d3b3b75`.
- Exact-head CI [31906370747](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31906370747)
  and exact-merge CI [31910754058](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31910754058)
  passed.
- Changed manifest: the seven maintained-current contract documents,
  `graphify/workspace/semantic_release_policy.py`, and
  `tests/test_workspace_semantic_release_policy.py`.

### PR #74 lock-discipline correction

- Pull request: [#74](https://github.com/Villeneuve-Ventures/graphify/pull/74),
  merged at `2026-08-17T03:57:47Z`.
- Exact base: `b88e81bae1bdfec9ab960199b42cd81e582e41b5`.
- Exact head: `9ed8e8a45582587c3226fd434e15b3a21bf5bc0c`.
- Merge/current canonical commit:
  `e28afc95f1f5b262b7673ef7b8c0ce9f7b1a4fa8`.
- Head, merge, and current canonical tree:
  `4b8d2faf95aef1caec02bcb95165cbb67b2983e1`.
- Exact-head CI [31992069990](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31992069990)
  and exact-merge CI [31992829074](https://github.com/Villeneuve-Ventures/graphify/actions/runs/31992829074)
  passed `skillgen-check`, `test (3.14)`, and `security-scan`.
- Changed manifest:

```text
graphify/workspace/leases.py
graphify/workspace/persistence.py
graphify/workspace/registry.py
graphify/workspace/semantic_release_policy.py
tests/test_workspace_runtime.py
tests/test_workspace_semantic_release_policy.py
```

## Exact-current-tree review disposition

The required bundled comment and thread review was performed for all three pull
requests. GitHub review state was inspected read-only and was not treated as a
substitute for exact-tree disposition:

- PR #71: three threads, all unresolved in GitHub UI;
- PR #72: two threads, both unresolved in GitHub UI; and
- PR #74: two threads, one resolved/outdated and one current unresolved.

All seven concerns are fixed in current canonical tree
`4b8d2faf95aef1caec02bcb95165cbb67b2983e1`:

| Pull request / thread | Concern | Exact-current-tree disposition |
|---|---|---|
| #71 `PRRT_kwDOTZvP8s6ZeaKL` | Governance narrative did not preserve the contract/readiness split. | Fixed: maintained-current governance now records PR #70 acceptance separately from PR #71's contract freeze and the later implementation/correction chain. |
| #71 `PRRT_kwDOTZvP8s6ZeaKP` | Direct rejection coverage was missing for ambient inputs, provider/network/catalogue shopping, newest selection, and extra structured members. | Fixed by the focused direct-rejection and closed-decoder tests in `tests/test_workspace_semantic_release_policy.py`. |
| #71 `PRRT_kwDOTZvP8s6ZeaKS` | Namespace and filesystem capacity proof had to fail before pending visibility. | Fixed by pre-pending namespace/reserve validation and `test_fixed_namespace_and_filesystem_reserve_fail_before_pending_visibility`. |
| #72 `PRRT_kwDOTZvP8s6Zh7aQ` | Deep JSON could raise `RecursionError` outside the state-error contract. | Fixed by bounded depth preflight and fail-closed canonical-object decoding, proved by `test_deep_authority_json_fails_closed_as_state_corrupt`. |
| #72 `PRRT_kwDOTZvP8s6Zh7aT` | Early bounded namespace failure could leak a `scandir` iterator. | Fixed by context-managed descriptor enumeration, proved by `test_bounded_namespace_scan_closes_iterator_on_early_failure`. |
| #74 `PRRT_kwDOTZvP8s6ZrreC` | Initializer election was split across replaceable lock objects. | Fixed by the retained initialization lock shared by enrollment and subsequent registry-lock acquisition, with concurrent initialization regressions. |
| #74 `PRRT_kwDOTZvP8s6ZrreH` | A regression test caught `BaseException`. | Fixed: the reviewed test now catches `Exception`; the separate multiprocessing helper's parent-process capture is outside this concern. |

GitHub's `reviewDecision` remained unset on all three merged pull requests, so
no formal approval is claimed. No comment, review, reply, thread, pull request,
check, branch, or other GitHub state was created, edited, resolved, submitted,
or otherwise mutated during this closeout.

## Local acceptance validation

The exact current canonical tree passed these required gates before the
governance-only edit:

- focused policy store: `56 passed`;
- predecessor/non-regression group, including workspace runtime:
  `492 passed`;
- wheel and semantic-release trust-root group: `362 passed`;
- `uv lock --check`: passed;
- `tools.skillgen --check`: all 134 artifacts matched;
- exact four-file `basedpyright`: zero errors, warnings, or notes; and
- full repository suite clean rerun: `5620 passed, 3 skipped, 3 warnings` in
  `1816.33s`.

The first full-suite attempt reported one deadline-sensitive failure in
`test_lease_acquisition_deadline_bounds_mutating_lock_wait[workspace]`: under
suite load, the 100 ms deadline expired during source discovery and raised
`SourceDiscoveryTimeout` before reaching the intended lock wait. The exact node
then passed once and passed ten consecutive isolated repetitions. The complete
suite rerun passed. This is recorded as a non-reproducing timing sensitivity,
not hidden or treated as an unqualified first-pass success.

The staged documentation validation also established:

- `uv run --frozen pre-commit run --all-files`: passed `skillgen --check` and
  `ruff`;
- tracked and new-receipt whitespace checks: passed;
- the documentation advisory audit completed with only expected
  source-verification prompts for the exact material claims and action-run IDs;
- the explicit nine-file relative-link and heading-anchor audit passed with 158
  headings, 195 local link targets, and 41 heading-anchor targets; and
- the final manifest is limited to the eight authorized maintained documents
  plus this one new receipt.

An independent exact-diff documentation, lifecycle, and evidence review first
identified one P2 contradiction in stale trust-root-only status wording in
`governance.md` and `semantic-sync.md`. Both sentences were corrected. The
reviewer then independently recomputed the corrected combined plain diff as
SHA-256 `1f4549295c28242dd56f3a6ff6b0d7a14fedc178161ad100c5546ceecd8c584b`
over 72,251 bytes and reported `CLEAR` with `P0=0`, `P1=0`, `P2=0`, and `P3=0`.
The final post-evidence-insertion digest and rereview are intentionally reported
at the local-diff boundary rather than embedded here, avoiding a self-referential
receipt digest.

## Accepted disposition

Only `P5B2 semantic-release policy-authority provisioning` transitions from
`IN_PROGRESS` to `COMPLETE` at this staged governance boundary. The accepted
trust-root prerequisite remains `COMPLETE`. P5 and P5B2 remain `IN_PROGRESS`;
the encompassing semantic-content release/DLP decision, live operator-policy
selection/provisioning, `SemanticReleaseDecisionStore`, capacity/GC
integration, classification composition, omission, projection, public
surfaces, provider/backend, publication, remaining P5B2 work, and P5C remain
`WAITING`; H3 remains `DEFERRED`; no later successor becomes `READY`.

No live operator policy-authority record is created by this acceptance. No
source, test, schema, fixture, dependency, configuration, workflow, generated
Graphify output, Git index, branch, worktree, commit, push, pull request, merge,
fast-forward, cleanup, or GitHub state is changed by this staged receipt.
